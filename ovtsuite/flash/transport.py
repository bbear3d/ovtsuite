# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line serial transport: pyserial port + KWP2000 5-baud slow init.

Ports KWHack's ``IInterface`` + ``KwpCommon`` behaviour to pyserial (which gives
the same primitives -- arbitrary baud, break-line control for the 5-baud
bit-bang, per-read timeout, batched read/write -- on Windows, Linux and macOS).
FTDI cables can also be driven directly (``FTDI:<serial>`` / ``FTDIIDX:<n>`` /
``ftdi://...`` port names, see :mod:`ovtsuite.flash.ftdi`), which sets the
chip's latency timer to 2 ms instead of leaving the driver's 16 ms default.

The ECU echoes every byte the tester sends on the half-duplex K-line, so a
"write" is always a write-then-read-back-the-echo; batched writes drain the whole
echo in one read. Reads raise :class:`KLineTimeout` on silence so the higher
layers can poll.
"""
import time

try:
    import serial                                   # pyserial
except Exception:                                   # pragma: no cover
    serial = None

from . import ftdi                                  # direct FTDI (D2XX / pyftdi)


class KLineError(Exception):
    pass


class KLineTimeout(KLineError):
    pass


class LinkStopped(KLineError):
    """The caller asked the link to give up (``is_stop`` came true) while it was
    retrying or waiting. A KLineError so the existing handlers unwind it."""
    pass


def check_stop(is_stop):
    """Raise :class:`LinkStopped` when ``is_stop`` (an optional callable) says so."""
    if is_stop is not None and is_stop():
        raise LinkStopped("stop requested")


def check_init_stop(link):
    """:func:`check_stop` for a link's init retries: its ``is_stop``, or its
    ``init_is_stop`` -- a stop the flasher gives only the init, so Cancel
    while connecting lands without letting it cut a read mid-write."""
    check_stop(getattr(link, "is_stop", None))
    check_stop(getattr(link, "init_is_stop", None))


def _wait_init(link, seconds: float) -> None:
    """Sleep between init tries, giving up early on Cancel."""
    if getattr(link, "is_stop", None) is None and getattr(link, "init_is_stop", None) is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        check_init_stop(link)
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 0.05))


def adjust_parity(b: int, even_parity: bool) -> int:
    """KWHack Utils.AdjustParity: set bit7 so the byte carries the requested
    parity across bits 0..6 (odd parity when even_parity is False)."""
    parity = not even_parity
    for i in range(7):
        if (b >> i) & 1:
            parity = not parity
    return (b | 0x80) if parity else (b & 0x7F)


def _busy_wait(seconds: float):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


_FAST_INIT_TRIES = 4                # bit-banged fast init is probabilistic; retry
_TWUP_S = 0.050                    # ISO 14230 Twup: StartCommunication's first bit
                                   # must fall 50 ms after the low pulse STARTS


class KLine:
    """A K-line connection over a serial port. Not thread-safe; drive it from one
    worker thread."""

    def __init__(self, port: str, baud: int = 10400, timeout: float = 8.0, on_trace=None,
                 prefer_fast_init: bool = False):
        self._timeout = timeout
        self._trace = on_trace                      # optional (dir, bytes) debug hook
        self.prefer_fast_init = prefer_fast_init     # try ISO14230 fast init before 5-baud
        self.direct = False                         # FTDI chip driven directly
        if ftdi.is_ftdi_name(port):
            try:
                self.port = ftdi.open_port(port, baudrate=baud, timeout=timeout,
                                           write_timeout=timeout)
            except ftdi.FtdiError as ex:
                raise KLineError(str(ex))
            self.direct = True
        else:
            if serial is None:
                raise KLineError(
                    "pyserial is not installed; flashing needs the 'pyserial' package.")
            ftdi.try_lower_sysfs_latency(port)      # Linux ftdi_sio: best effort
            self.port = serial.Serial(
                port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=timeout, write_timeout=timeout, rtscts=False, dsrdtr=False)
        try:
            self.port.dtr = True
            self.port.rts = False
        except Exception:
            pass
        self.last_sync = -1
        #: optional callable()->bool; the wake-up retry loops give up when true
        self.is_stop = None

    # -- lifecycle --------------------------------------------------
    def close(self):
        try:
            self.port.dtr = False
        except Exception:
            pass
        try:
            self.port.close()
        except Exception:
            pass

    def cancel(self):
        """Unblock a read or write in progress on another thread -- the last
        resort when a logger Disconnect has waited too long. pyserial has
        ``cancel_read``/``cancel_write``; a port without them is closed, which
        fails the blocked call instead."""
        port = self.port
        cancelled = False
        for name in ("cancel_read", "cancel_write"):
            fn = getattr(port, name, None)
            if fn is not None:
                try:
                    fn()
                    cancelled = True
                except Exception:
                    pass
        if not cancelled:
            try:
                port.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- port control ----------------------------------------------
    @property
    def read_timeout(self) -> float:
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds: float):
        self._timeout = seconds
        self.port.timeout = seconds

    def set_baud(self, baud: int):
        self.port.baudrate = baud

    def set_break(self, on: bool):
        self.port.break_condition = on

    def clear_rx(self):
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass

    # -- byte I/O --------------------------------------------------
    def read_byte(self) -> int:
        b = self.port.read(1)
        if not b:
            raise KLineTimeout("K-line read timed out")
        if self._trace:
            self._trace("rx", b)
        return b[0]

    def read(self, n: int) -> bytes:
        """Read exactly n bytes (raises KLineTimeout if the port goes silent)."""
        out = bytearray()
        while len(out) < n:
            chunk = self.port.read(n - len(out))
            if not chunk:
                raise KLineTimeout("K-line read timed out (%d/%d)" % (len(out), n))
            out.extend(chunk)
        if self._trace:
            self._trace("rx", bytes(out))
        return bytes(out)

    def write_raw(self, data: bytes):
        if self._trace:
            self._trace("tx", bytes(data))
        self.port.write(data)
        self.port.flush()

    def write_byte(self, b: int):
        """Write one byte and discard its K-line echo."""
        self.write_raw(bytes([b]))
        try:
            self.port.read(1)                       # echo (best-effort discard)
        except Exception:
            pass

    def write_bytes(self, data: bytes):
        """Write a whole frame, then drain all its echoes in one read."""
        self.write_raw(data)
        got = 0
        while got < len(data):
            chunk = self.port.read(len(data) - got)
            if not chunk:
                break                               # echo lost; not fatal
            got += len(chunk)

    def read_complement(self, b: int):
        expected = (~b) & 0xFF
        actual = self.read_byte()
        if actual != expected:
            raise KLineError(
                "Received complement 0x%02X but expected 0x%02X" % (actual, expected))

    # -- 5-baud slow init (KWP2000) --------------------------------
    def _bitbang_5baud(self, address: int, even_parity: bool):
        b = adjust_parity(address, even_parity)
        ms_per_bit = 0.200
        self.set_break(False)                       # first SetBreak is slow; warm it up

        def bit(one: bool):
            _busy_wait(ms_per_bit)
            self.set_break(not one)                 # break asserted = logic 0

        bit(False)                                  # start bit
        for _ in range(8):
            bit((b & 1) == 1)
            b >>= 1
        bit(True)                                   # stop bit
        _busy_wait(ms_per_bit)

    def wake_up(self, address: int = 0x01, even_parity: bool = False,
                tries: int = 3) -> int:
        """KWP2000 init; returns the protocol version. Tries ISO 14230 fast init
        first when requested (later CAN-init-only ECUs need it), then falls back
        to the 5-baud slow init, retrying a few times like KWHack."""
        if self.prefer_fast_init:
            for attempt in range(1, _FAST_INIT_TRIES + 1):
                check_init_stop(self)
                try:
                    return self._fast_init(address)
                except KLineError:
                    pass                            # bit-banged fast init is flaky; retry,
                    # each _fast_init re-idles the bus (W5) before its pulse
            # every fast-init attempt missed; fall back to the 5-baud slow init
        last = None
        for attempt in range(1, tries + 1):
            check_init_stop(self)
            try:
                return self._wake_up_once(address, even_parity)
            except KLineError as ex:
                last = ex
                if attempt < tries:
                    _wait_init(self, 1.0)
        raise last or KLineError("Controller did not wake up")

    def _read_burst(self, max_bytes: int = 48, first: float = 0.4, gap: float = 0.03) -> bytes:
        """Read everything that comes back after a frame -- our own half-duplex echo
        AND the ECU's reply -- into one buffer, until ``gap`` s of silence or
        ``max_bytes``.  The caller pattern-matches the reply inside it, so the read
        never has to be aligned to the echo boundary (draining the echo positionally
        was what left the stray ``03`` and forced a drain that broke Twup)."""
        out = bytearray()
        saved = self._timeout
        self.read_timeout = first
        try:
            chunk = self.port.read(1)                   # block a little for the first byte
            if chunk:
                out += chunk
                self.read_timeout = gap
                while len(out) < max_bytes:
                    chunk = self.port.read(max_bytes - len(out))
                    if not chunk:
                        break                           # `gap` s of silence -> done
                    out += chunk
        finally:
            self.read_timeout = saved
        if self._trace and out:
            self._trace("rx", bytes(out))
        return bytes(out)

    def _tinit_low_pulse(self) -> float:
        """Drive K-line low for ISO 14230 TiniL = 25 ms via the break condition and
        return the ``perf_counter`` timestamp of the falling edge, so the caller can
        place StartCommunication at Twup = 50 ms after it.

        Break is the method: it wakes every cable tested (FTDI DCAN, FTDI KKL and a
        CH340).  The 25 ms width is ``perf_counter``-timed; the few ms of USB latency
        on each edge is what the retries cover."""
        t0 = time.perf_counter()
        self.set_break(True)
        _busy_wait(0.025)
        self.set_break(False)
        self.clear_rx()                             # echo/garbage is skipped by the reply scan
        return t0

    def _fast_init(self, address: int = 0x01) -> int:
        """ISO 14230 fast initialisation: a 25 ms low pulse, then StartCommunication
        (81 <addr> F1 81) placed at Twup = 50 ms after the pulse START, at 10400.
        Bit-banging over USB-serial is inherently less reliable than a J2534 device
        (see ovtsuite.flash.kline_j2534), but a hardware-timed low pulse
        (:meth:`_tinit_low_pulse`), correct Twup, and retries make it usable on an
        FTDI cable."""
        self.read_timeout = 2.0
        try:
            self.set_break(False)
            time.sleep(0.3)                         # W5: bus idle high >= 300 ms
            t0 = self._tinit_low_pulse()            # 25 ms low; t0 = pulse start
            while time.perf_counter() - t0 < _TWUP_S:   # StartComm first bit at Twup
                pass
            # StartCommunication targets the KWP controller 0x10 (like MPPS's
            # 81 10 F1 81), not the 5-baud init address -- 0x01 gets no reply.
            self.clear_rx()                          # last purge just before the frame
            req = bytes([0x81, 0x10, 0xF1, 0x81])
            req = req + bytes([sum(req) & 0xFF])
            self.write_raw(req)                      # send; echo comes back on the line
            # Read the echo + reply together and SCAN for the StartCommunication
            # positive response (StartComm SID 0x81 | 0x40 = 0xC1, then the two
            # keyword bytes): 83 F1 10 C1 EF 8F C3.  Pattern-matching on 0xC1 skips
            # our own echo (81 10 F1 81 03, no 0xC1) and any pulse-echo garbage, so
            # a one-byte misalignment can't turn a real reply into a failure.
            buf = self._read_burst(48)
            idx = buf.find(0xC1)
            if idx < 0 or idx + 2 >= len(buf):
                raise KLineError("fast init: no StartCommunication response (%s)"
                                 % (buf.hex() or "(silent)"))
            kw_lsb, kw_msb = buf[idx + 1], buf[idx + 2]
            version = ((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)
            self.last_sync = 0x55
            return version if version >= 2000 else 0x2000
        finally:
            self.read_timeout = 8.0

    def _wake_up_once(self, address: int, even_parity: bool) -> int:
        self.read_timeout = 2.0
        try:
            time.sleep(0.3)
            self._bitbang_5baud(address, even_parity)
            self.clear_rx()
            sync = self.read_byte()
            self.last_sync = sync
            if sync != 0x55:
                raise KLineError("Unexpected sync byte: expected 0x55, got 0x%02X" % sync)
            kw_lsb = self.read_byte()
            kw_msb = self.read_byte()
            version = ((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)
            time.sleep(0.025)
            self.write_byte((~kw_msb) & 0xFF)       # complement of keyword msb
            if version >= 2000:
                self.read_complement(adjust_parity(address, even_parity))
            return version
        finally:
            self.read_timeout = 8.0
