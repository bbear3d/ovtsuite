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

Each step is one device call (``kline_xfer``: what is waiting, the write, the
first reply), which through the 32-bit bridge is one round trip; a bridge
build without it gets the same reads and writes one call each. A write the
caller says a read follows (:meth:`write_before_read` -- a KW1281 byte and
its complement, an acknowledgement and the next byte, a KWP2000 request and
its reply) goes out with that read, in the same call. What comes back is
still a byte stream: KW1281 blocks, loader and EEPROM bytes are not KWP
frames, so nothing is reassembled into one, and the channel timing is left
as it was.
"""
import time
from typing import Callable, Optional

from . import j2534 as J
from .transport import (KLineError, KLineTimeout, LinkStopped, adjust_parity,
                        check_init_stop)

#: Reads wait in slices this long, so a stop request lands within one.
READ_SLICE_MS = 50

#: Channel timing that keeps the device out of the byte handshake
#: (J2534 units: P1/P3/P4 in 0.5 ms).
_RAW_TIMING = (("P1_MAX", 2), ("P3_MIN", 0), ("P4_MIN", 0), ("LOOPBACK", 0))

#: FIVE_BAUD_MOD 1: the device sends ~KB2 and does not wait for ~address.
FIVE_BAUD_TESTER_COMPLEMENT_ONLY = 1

#: Shorter timing for the EDC15 flash / EEPROM session on this link
#: (:class:`~ovtsuite.flash.edc15.Edc15Flash`), as the EDC16 J2534 write got.
#: Not yet bench-tested: set False to put back exactly the timing and calls
#: used before (the serial KKL path never uses it either way). The channel
#: timing above is the same both ways -- it is already P1_MAX 1 ms, P3_MIN 0,
#: P4_MIN 0, the values EDC16's ``set_session_timing`` moves to -- and EDC15
#: sends no accessTimingParameters for it to match. What is shortened is the
#: pacing done in Python that was there for a serial adapter.
EDC15_J2534_FAST_TIMING = True

#: What :data:`EDC15_J2534_FAST_TIMING` changes (the old value in brackets):
EDC15_J2534_TIMING = {
    # KW1281 identification at 9600: R6 1 ms, the spec minimum, and no 25 ms
    # block lead-in -- the logger's timing on the same ECU (R6 2 ms + 25 ms)
    "kw1281_fast": True,
    # KWP2000 requests in one write, the device's P4_MIN (0) between bytes
    # (byte by byte, one device call and a 5 ms sleep each); P3 55 ms kept.
    # Off: on the bench (2026-10-25, Scanmatik) the ECU answered the first
    # whole-frame request (10 89) with a garbled negative reply and the read
    # never started -- its KWP2000 handler wants the gap between bytes.
    "kwp_batched": False,
    # extra wait between loader TransferData frames, on top of P3 (5 ms)
    "transfer_gap": 0.0,
    # post-loader handshake slices in one write each (byte by byte, 5 ms
    # apart at 10400). Not yet reached on the bench (the 10 89 failure came
    # first); to be tried next.
    "handshake_batched": True,
    # a loader command goes out in the device call that reads its reply
    # (a write call, then reads)
    "loader_write_with_read": True,
}


def edc15_timing() -> Optional[dict]:
    """The EDC15 session timing this link uses: a copy of
    :data:`EDC15_J2534_TIMING`, or None for the timing used before."""
    return dict(EDC15_J2534_TIMING) if EDC15_J2534_FAST_TIMING else None


class J2534RawKLine:
    """Byte-stream K-line over an already-opened J2534 device (``PassThru``,
    ``BridgePassThru`` or the direct ``OpenPort``). The device stays the
    caller's: :meth:`close` disconnects the channel, not the device."""

    is_kline = True
    direct = True
    prefer_fast_init = False             # the init is the device's 5-baud one
    #: line silence already behind a received byte when it is handed over:
    #: the device ends a message after P1_MAX (1 ms, :data:`_RAW_TIMING`) of
    #: it, so that much of KW1281's R6 has passed before the reply can be
    #: written (:class:`~ovtsuite.flash.kwp.Kw1281` waits only the rest)
    rx_settle_s = 0.001

    def __init__(self, dev, baud: int = 10400, log: Optional[Callable[[str], None]] = None,
                 protocol: int = J.ISO9141, on_trace=None):
        self._dev = dev
        self._trace = on_trace             # optional (dir, bytes) wire-log hook
        self._proto = protocol
        self._baud = int(baud)
        self._log = log or (lambda s: None)
        self._ch = None
        self._rx = bytearray()
        self._held: Optional[bytes] = None   # a write that goes out with the next read
        self._timeout = 8.0
        self._data_rate_by_config: Optional[bool] = None
        self._parity_changed = False
        #: W4 (ms) to give the channel for a 5-baud init, or None for the device's own
        self._w4: Optional[int] = None
        self.five_baud_mod_ok = False
        self.last_sync = -1
        #: optional callable()->bool, checked between read slices and init tries
        self.is_stop = None
        #: optional callable()->bool, checked between init tries only
        self.init_is_stop = None
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
            # A prior Audi C5 unlock on this device (see VdoCluster.set_parity)
            # leaves its UART at even parity; some firmwares keep that setting
            # across a channel close/reopen instead of resetting it, so a KW1281
            # wake on a fresh channel can silently inherit it and fail. Force
            # it back to none explicitly rather than assume a clean default.
            self._ch.set_config(PARITY=0)
        except Exception:
            pass
        try:
            self._ch.set_config(FIVE_BAUD_MOD=FIVE_BAUD_TESTER_COMPLEMENT_ONLY)
            self.five_baud_mod_ok = True
        except Exception:
            self.five_baud_mod_ok = False
        if self._w4 is not None:
            try:
                self._ch.set_config(W4=self._w4)
            except Exception:
                pass
        if refused:
            self._log("J2534 K-line: the device kept its default for %s; byte timing "
                      "may be slower." % ", ".join(refused))

    def close(self):
        try:
            self._flush_held()
        except Exception:
            pass
        if self._parity_changed and self._ch is not None:
            try:
                self._ch.set_config(PARITY=0)      # see set_parity: never leave it at even
            except Exception:
                pass
            self._parity_changed = False
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
    def edc15_timing(self) -> Optional[dict]:
        """See :func:`edc15_timing` (read by the EDC15 flasher)."""
        return edc15_timing()

    @property
    def read_timeout(self) -> float:
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds: float):
        self._timeout = float(seconds)

    def set_five_baud_w4(self, ms: int):
        """How long after the ECU's key bytes the device sends its inverted
        key byte 2 in a 5-baud init (J2534 W4, ISO 14230 allows 25-50 ms).
        The default of 50 ms is too late for the EDC15's KWP2000 boot stub (it
        then never sends the address complement or answers anything); about
        35 ms is what a serial cable's host timing gives.  Kept across a channel
        reopen (a baud change reopens it on the OpenPort)."""
        self._w4 = int(ms)
        if self._ch is not None:
            try:
                self._ch.set_config(W4=self._w4)
            except Exception:
                pass

    def set_baud(self, baud: int):
        """DATA_RATE first; a device that only takes the rate when a channel
        opens (the OpenPort 2.0) gets its channel reopened at the new rate --
        a few ms of idle line, well inside any ECU's P3max."""
        baud = int(baud)
        if baud == self._baud and self._ch is not None:
            return
        self._flush_held()                   # a held write goes at the old rate
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

    def set_parity(self, p: str):
        """Audi C5-family VDO clusters switch to even parity after login
        (vdo_cluster.VdoCluster._set_parity); J2534's PARITY SCONFIG param is
        0/1/2 for none/odd/even. Tracked so close() can put a device that
        never got asked back to "N" (a crash, a cancel, anything that skips
        VdoCluster.end()) back the way it found it -- left at even parity, a
        device's firmware can carry that into whoever opens it next, with no
        hint why its OWN unrelated 5-baud init then fails (confirmed on the
        bench: a fresh process, same device, same cluster, "ERR_FAILED" on
        every wake attempt until parity was forced back to none)."""
        value = {"N": 0, "O": 1, "E": 2}.get(p.upper())
        if value is None:
            raise KLineError("unknown parity %r" % p)
        if self._ch is None:
            raise KLineError("J2534 K-line: channel closed")
        try:
            self._ch.set_config(PARITY=value)
            self._parity_changed = value != 0
        except Exception as ex:
            raise KLineError("this J2534 device would not switch to %s parity: %s"
                             % ({"N": "no", "O": "odd", "E": "even"}[p.upper()], ex))

    def set_break(self, on: bool):
        """The line belongs to the device; its init does what a break did."""
        return None

    def clear_rx(self):
        self._flush_held()
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
            check_init_stop(self)
            try:
                return self._wake_up_once(addr)
            except LinkStopped:
                raise
            except Exception as ex:      # J2534Error from the ioctl, short key bytes
                last = ex
                self.last_sync = -1
                if attempt < tries:
                    self._wait(1.0)
        msg = "J2534 5-baud init to 0x%02X failed: %s" % (addr, last)
        if not self.five_baud_mod_ok:
            msg += (" (this device refused FIVE_BAUD_MOD 1, so it waits for an "
                    "inverted address a KW1281 controller never sends; use a KKL "
                    "cable for this ECU)")
        raise KLineError(msg)

    def _wake_up_once(self, addr: int) -> int:
        self._held = None                    # a new init: a held byte is moot
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
        if self._trace:
            self._trace("tx", bytes([addr]))            # the 5-baud address
            self._trace("rx", kb)                       # the key bytes the device returned
        self._log("J2534 5-baud init 0x%02X -> key bytes %02X %02X (KW %d)"
                  % (addr, kb1, kb2, version))
        # The device sent ~KB2. A KWP2000 controller answers it with ~address;
        # in mode 1 the device leaves that in the stream, in its default mode
        # it consumed it already -- in THEORY. On the bench (a Tactrix
        # OpenPort, direct libusb, EDC15) the ECU plainly never sends it:
        # sniffed on the wire, our own ~KB2 goes out clean and nothing comes
        # back, ever, confirmed 3/3 and mode-independent (forcing the
        # channel to mode 0 and redoing the whole wake made no difference --
        # same silence). Whatever reads it on an SM2 isn't universal, so a
        # missing complement here is a warning, not a reason to fail the
        # whole wake: nothing downstream of wake_up() actually depends on
        # having read it.
        if version >= 2000 and self.five_baud_mod_ok:
            # Capped at 1s rather than the full self._timeout (8s): tried on
            # the theory that burning 8s here before the first real request
            # was blowing some ECU-side session window, which turned out
            # not to be it (shortening this changed nothing downstream) --
            # kept anyway since there is no reason to wait out the full
            # timeout for a byte already known not to be coming.
            saved = self._timeout
            self._timeout = 1.0
            try:
                self.read_complement(addr)
            except KLineError as ex:
                self._log("J2534 5-baud init: no ~address complement from the "
                          "ECU (%s) -- continuing without it" % ex)
            finally:
                self._timeout = saved
        return version

    # ---- device steps -------------------------------------------------------
    def _xfer(self, data: Optional[bytes], wait_ms: int, drain: bool) -> bytes:
        """One device call: what is waiting (``drain``), the write, the first
        reply within ``wait_ms``. What came before the write goes to the read
        buffer here, so it stays ahead of the write's echo; what came after is
        returned, for the caller to put behind one."""
        if self._ch is None:
            raise KLineError("J2534 K-line: channel closed")
        budget = 0
        if data is not None:
            budget = max(1000, 1000 + len(data) * 20000 // max(self._baud, 300))
        xfer = getattr(self._ch, "kline_xfer", None)
        if self._trace and data:
            self._trace("tx", bytes(data))
        try:
            if xfer is not None:
                before, after = xfer(data, budget, wait_ms, drain)
            else:                            # e.g. the direct OpenPort channel
                before, after = J.kline_xfer(self._ch, data, budget, wait_ms, drain)
        except J.J2534Error as ex:
            raise KLineError("J2534 K-line %s failed: %s"
                             % ("write" if data is not None else "read", ex))
        self._take(before)
        tail = bytearray()
        for m in after:
            if not getattr(m, "is_tx_echo", False):
                tail += bytes(getattr(m, "payload", b"") or b"")
        if self._trace and tail:
            self._trace("rx", bytes(tail))
        return bytes(tail)

    def _flush_held(self):
        held, self._held = self._held, None
        if held is not None and self._ch is not None:
            self._rx += self._xfer(held, 0, True)

    # ---- writes -------------------------------------------------------------
    def _write(self, data: bytes, echo: bool = False):
        self._flush_held()
        self._rx += self._xfer(bytes(data), 0, True)
        if echo:
            self._rx += data

    def write_raw(self, data: bytes):
        """Write and leave the (synthesised) echo to be read back, as a serial
        K-line would."""
        data = bytes(data)
        if not data:
            return
        self._write(data, echo=True)

    def write_byte(self, b: int):
        """Write one byte; its echo is discarded (``KLine.write_byte``)."""
        self._write(bytes([b & 0xFF]))

    def write_bytes(self, data: bytes):
        """Write a whole frame; echoes discarded (``KLine.write_bytes``)."""
        data = bytes(data)
        if data:
            self._write(data)

    def write_before_read(self, data: bytes):
        """Write ``data`` (echo discarded) together with the next read: for a
        caller that reads straight after, so both are one device call.
        Anything else done on the link first (a write, a baud change,
        clear_rx, close) sends it on its own first, so it is not lost or
        reordered; only a new init drops it."""
        data = bytes(data)
        if not data:
            return
        self._flush_held()
        self._held = data

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
                if self._trace:
                    self._trace("rx", data)
        return got

    def _drain(self):
        if self._ch is None:
            return
        try:
            self._xfer(None, 0, True)
        except KLineError:
            return

    def _fill(self):
        """Wait for the next received message, sending a held write first in
        the same call. A blocking PassThruReadMsgs returns only when it has
        ALL the messages asked for or its timeout runs out, so asking for 32
        with a 50 ms timeout held every read for the full 50 ms -- every
        KW1281 complement and every packet ack. The wait asks for one message
        (back the moment it lands), then whatever else has arrived is
        collected without waiting (:func:`~ovtsuite.flash.j2534.kline_xfer`)."""
        deadline = time.monotonic() + self._timeout
        while True:
            self._check_stop()
            if self._ch is None:
                raise KLineError("J2534 K-line: channel closed")
            left = deadline - time.monotonic()
            slice_ms = int(max(0, min(left * 1000, READ_SLICE_MS)))
            held, self._held = self._held, None
            n = len(self._rx)
            self._rx += self._xfer(held, max(1, slice_ms), held is not None)
            if len(self._rx) > n:
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

    def _wait(self, seconds: float):
        """Sleep ``seconds`` between init tries, giving up early on
        :attr:`is_stop` or :attr:`init_is_stop`."""
        if self.is_stop is None and self.init_is_stop is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while True:
            check_init_stop(self)
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))


__all__ = ["J2534RawKLine"]
