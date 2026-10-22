# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line as a plain byte stream over a J2534 device, for KW1281.

:class:`~ovtsuite.flash.kline_j2534.J2534KLine` hands the device whole
KWP2000 frames, which is right for KWP2000 and useless for KW1281: there every
byte of a block is acknowledged by its complement before the next one is sent,
so nothing ever forms a frame for it to flush. EDC15 measuring blocks and
fault codes are KW1281, so on a J2534 cable they need this instead.

A port of KWHack's ``PassThruKLineInterface`` (the path its EDC15 live data
runs on over an OpenPort 2.0):

* the channel is a raw **ISO 9141** channel with ``NO_CHECKSUM | K_LINE_ONLY``
  -- the device sends exactly the bytes it is given and adds nothing;
* ``P1_MAX`` = 1 ms, so a received message ends after 1 ms of silence and a
  lone byte (the ECU's half of the complement handshake) arrives at once
  instead of after the 20 ms default; ``P3_MIN`` = ``P4_MIN`` = 0, so the
  device puts no gap of its own before or inside a write;
* ``LOOPBACK`` = 0, and the half-duplex echo a serial K-line produces is
  generated here (:meth:`write_raw`), so it does not depend on how a device
  reports its own transmissions;
* the 5-baud init is the device's ``FIVE_BAUD_INIT`` with ``FIVE_BAUD_MOD`` = 1:
  the device sends ~KB2 itself and does not wait for ~address -- which KW1281
  never sends. A device that refuses mode 1 still inits a KWP2000 controller
  (its ISO 9141-2 default consumes ~address), but a KW1281 one will not
  answer it, and :meth:`wake_up` says so.

It presents the same face as :class:`~ovtsuite.flash.transport.KLine`
(``wake_up`` / ``read_byte`` / ``write_byte`` / ``read_complement`` ...), so
:class:`~ovtsuite.flash.kwp.Kw1281` and :class:`~ovtsuite.flash.kwp.Kwp2000`
run on it unchanged. What it cannot do is anything that needs the line itself
(a break, a bit-banged address): the device owns the init.
"""
import time
from typing import Callable, Optional

from . import j2534 as J
from .transport import KLineError, KLineTimeout, LinkStopped, adjust_parity

#: Reads wait in slices this long, so a stop request lands within one.
READ_SLICE_MS = 50

#: Channel timing that keeps the device out of the byte handshake
#: (J2534 units: P1/P3/P4 in 0.5 ms).
_RAW_TIMING = (("P1_MAX", 2), ("P3_MIN", 0), ("P4_MIN", 0), ("LOOPBACK", 0))

#: FIVE_BAUD_MOD 1: the device sends ~KB2 and does not wait for ~address.
FIVE_BAUD_TESTER_COMPLEMENT_ONLY = 1


class J2534RawKLine:
    """Byte-stream K-line over an already-opened J2534 device (``PassThru``,
    ``BridgePassThru`` or the direct ``OpenPort``). The device stays the
    caller's: :meth:`close` disconnects the channel, not the device."""

    is_kline = True
    direct = True
    prefer_fast_init = False             # the init is the device's 5-baud one

    def __init__(self, dev, baud: int = 10400, log: Optional[Callable[[str], None]] = None,
                 protocol: int = J.ISO9141):
        self._dev = dev
        self._proto = protocol
        self._baud = int(baud)
        self._log = log or (lambda s: None)
        self._ch = None
        self._rx = bytearray()
        self._timeout = 8.0
        self._data_rate_by_config: Optional[bool] = None
        self.five_baud_mod_ok = False
        self.last_sync = -1
        #: optional callable()->bool, checked between read slices and init tries
        self.is_stop = None
        self._open()

    # ---- channel ----------------------------------------------------------
    def _open(self):
        self._ch = self._dev.connect(self._proto, self._baud,
                                     J.ISO9141_NO_CHECKSUM | J.ISO9141_K_LINE_ONLY)
        try:
            self._ch.pass_all()
        except Exception:
            pass                         # some devices pass K-line traffic unfiltered
        refused = []
        for name, val in _RAW_TIMING:
            try:
                self._ch.set_config(**{name: val})
            except Exception:
                refused.append("%s=%d" % (name, val))
        try:
            self._ch.set_config(FIVE_BAUD_MOD=FIVE_BAUD_TESTER_COMPLEMENT_ONLY)
            self.five_baud_mod_ok = True
        except Exception:
            self.five_baud_mod_ok = False
        if refused:
            self._log("J2534 K-line: the device kept its default for %s; byte timing "
                      "may be slower." % ", ".join(refused))

    def close(self):
        ch, self._ch = self._ch, None
        if ch is not None:
            try:
                ch.disconnect()
            except Exception:
                pass

    def cancel(self):
        """Nothing to cancel from outside: a read waits in short slices and
        sees :attr:`is_stop`."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- knobs ------------------------------------------------------------
    @property
    def read_timeout(self) -> float:
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds: float):
        self._timeout = float(seconds)

    def set_baud(self, baud: int):
        """DATA_RATE first; a device that only takes the rate when a channel
        opens (the OpenPort 2.0) gets its channel reopened at the new rate --
        a few ms of idle line, well inside any ECU's P3max."""
        baud = int(baud)
        if baud == self._baud and self._ch is not None:
            return
        if self._ch is not None and self._data_rate_by_config is not False:
            try:
                self._ch.set_config(DATA_RATE=baud)
                self._data_rate_by_config = True
                self._baud = baud
                return
            except Exception:
                self._data_rate_by_config = False
        self._drain()
        self.close()
        self._baud = baud
        self._open()

    def set_break(self, on: bool):
        """The line belongs to the device; its init does what a break did."""
        return None

    def clear_rx(self):
        self._rx.clear()
        if self._ch is not None:
            try:
                self._ch.clear_rx()
            except Exception:
                pass

    # ---- init ---------------------------------------------------------------
    def wake_up(self, address: int = 0x01, even_parity: bool = False, tries: int = 3) -> int:
        """Hardware 5-baud init; returns the protocol version from the key
        bytes (1281 for KW1281, >= 2000 for KWP2000), ready for the first
        block exactly as after a serial wake-up."""
        addr = adjust_parity(address, even_parity)
        last = None
        for attempt in range(1, tries + 1):
            self._check_stop()
            try:
                return self._wake_up_once(addr)
            except LinkStopped:
                raise
            except Exception as ex:      # J2534Error from the ioctl, short key bytes
                last = ex
                self.last_sync = -1
                if attempt < tries:
                    time.sleep(1.0)
        msg = "J2534 5-baud init to 0x%02X failed: %s" % (addr, last)
        if not self.five_baud_mod_ok:
            msg += (" (this device refused FIVE_BAUD_MOD 1, so it waits for an "
                    "inverted address a KW1281 controller never sends; use a KKL "
                    "cable for this ECU)")
        raise KLineError(msg)

    def _wake_up_once(self, addr: int) -> int:
        time.sleep(0.3)
        self._rx.clear()
        try:
            self._ch.clear_rx()
        except Exception:
            pass
        kb = bytes(self._ch.five_baud_init(addr))
        # J2534 returns the key bytes; tolerate a device that includes the sync
        first = 1 if len(kb) >= 3 and kb[0] == 0x55 else 0
        if len(kb) - first < 2:
            raise KLineError("5-baud init returned %r; expected two key bytes"
                             % kb.hex(" "))
        kb1, kb2 = kb[first], kb[first + 1]
        version = ((kb2 & 0x7F) << 7) + (kb1 & 0x7F)
        self.last_sync = 0x55
        self._log("J2534 5-baud init 0x%02X -> key bytes %02X %02X (KW %d)"
                  % (addr, kb1, kb2, version))
        # The device sent ~KB2. A KWP2000 controller answers it with ~address;
        # in mode 1 the device leaves that in the stream, in its default mode
        # it consumed it already.
        if version >= 2000 and self.five_baud_mod_ok:
            self.read_complement(addr)
        return version

    # ---- writes -------------------------------------------------------------
    def _write(self, data: bytes):
        if self._ch is None:
            raise KLineError("J2534 K-line: channel closed")
        self._drain()                    # what came before this write stays before its echo
        budget = max(1000, 1000 + len(data) * 20000 // max(self._baud, 300))
        try:
            self._ch.write(bytes(data), timeout_ms=budget)
        except J.J2534Error as ex:
            raise KLineError("J2534 K-line write failed: %s" % ex)

    def write_raw(self, data: bytes):
        """Write and leave the (synthesised) echo to be read back, as a serial
        K-line would."""
        data = bytes(data)
        if not data:
            return
        self._write(data)
        self._rx += data

    def write_byte(self, b: int):
        """Write one byte; its echo is discarded (``KLine.write_byte``)."""
        self._write(bytes([b & 0xFF]))

    def write_bytes(self, data: bytes):
        """Write a whole frame; echoes discarded (``KLine.write_bytes``)."""
        data = bytes(data)
        if data:
            self._write(data)

    # ---- reads --------------------------------------------------------------
    def _take(self, msgs) -> bool:
        got = False
        for m in msgs:
            if getattr(m, "is_tx_echo", False):
                continue                 # the device's own transmission
            data = bytes(getattr(m, "payload", b"") or b"")
            if data:
                self._rx += data
                got = True
        return got

    def _drain(self):
        if self._ch is None:
            return
        for _ in range(64):
            try:
                msgs = self._ch.read(32, 0)
            except J.J2534Error:
                return
            if not msgs:
                return
            self._take(msgs)

    def _fill(self):
        deadline = time.monotonic() + self._timeout
        while True:
            self._check_stop()
            if self._ch is None:
                raise KLineError("J2534 K-line: channel closed")
            left = deadline - time.monotonic()
            slice_ms = int(max(0, min(left * 1000, READ_SLICE_MS)))
            try:
                msgs = self._ch.read(32, max(1, slice_ms))
            except J.J2534Error as ex:
                raise KLineError("J2534 K-line read failed: %s" % ex)
            if self._take(msgs):
                return
            if time.monotonic() >= deadline:
                raise KLineTimeout("K-line read timed out")

    def read_byte(self) -> int:
        if not self._rx:
            self._fill()
        return self._rx.pop(0)

    def read(self, n: int) -> bytes:
        while len(self._rx) < n:
            self._fill()
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def read_complement(self, b: int):
        expected = (~b) & 0xFF
        actual = self.read_byte()
        if actual != expected:
            raise KLineError("Received complement 0x%02X but expected 0x%02X"
                             % (actual, expected))

    def _check_stop(self):
        stop = self.is_stop
        if stop is not None and stop():
            raise LinkStopped("stop requested")


__all__ = ["J2534RawKLine"]
