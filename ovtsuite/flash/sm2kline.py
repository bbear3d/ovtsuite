# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Raw K-line (ISO 9141) on a Scanmatik SM2 over libusb, for KW1281.

:class:`Sm2KLineDevice` is the ``connect(protocol, baud, flags)`` object
:class:`ovtsuite.flash.kline_j2534_raw.J2534RawKLine` wants, and
:class:`Sm2KLineChannel` the channel it drives, so ``VdoCluster`` / ``Kw1281``
run on an SM2 through ``J2534RawKLine(Sm2KLineDevice(sm2))`` unchanged.

Read from traces of Scanmatik's DLL on a bench VDO cluster
(``docs/SM2_USB_PROTOCOL.md``, "K-line"); the same traces through the vendor
driver and through libusb are byte-identical.

* connect: ``0x88``, three zero blocks, then block 1 for ISO 9141 (offsets
  below), blocks 2 / 3 zero, ``0x53`` mask ``0x0003``.  A pass filter sends nothing.
* ``SET_CONFIG`` of ``P1_MAX`` / ``P3_MIN`` / ``P4_MIN`` changes one byte of
  block 1 (in ms; the J2534 value is in 0.5 ms) and the DLL resends blocks 1-3.
* ``CLEAR_RX_BUFFER``: ``0x53`` mask ``0x0002``.  Close: ``0x53`` mask ``0x0001``.
* ISO 14230 (``Sm2KLineDevice.connect(J.ISO14230, ...)``) is the same channel with
  two differences: ``SET_CONFIG P3_MIN=0`` gives 20 ms in block 1 (ISO 9141: 1 ms), and
  the hardware fast init
  (:meth:`Sm2KLineChannel.fast_init`) is one command, ``0x10``: u16 LE words 300, 25, 50,
  0x1020, then the StartCommunication frame *with its checksum* (the DLL adds none); the
  reply payload is the ECU's answer frame.  The DLL then sends one poll with ``f1`` in
  its second byte (``00 f1 00 02``) and polls as usual after that.
* 5-baud init, one command: ``0x04`` with ``00 00 00 00 | address | 08 00 | 12c |
  1f4 | 12c | 12c | 19 | 32`` (u16 LE timing words); the reply carries the key
  bytes at payload offset 4.  **The address must already carry its parity bit**
  (``J2534RawKLine`` does that).  An unanswered init comes back as status ``0x0B``
  after about 2.8 s.
* poll ``0x1D``: ``ack 00 00 02`` (``ack`` = 1 after a reply that held a message boundary); write ``0x1D``: ``01 00 00 02`` + each byte followed by ``0x80``.  Every
  reply is the 0x14-byte status block, then 2-byte records ``byte, flag``: flag
  ``0x80`` is a received byte, ``0x10`` a message boundary (P1_MAX after the last byte); a
  message is handed over at its boundary, as the DLL does.  No TX echo.

Not verified: parity other than none, a baud change after connect, writes
longer than a few bytes (they are cut into :data:`WRITE_CHUNK`), KWP2000 on
this channel, anything that is not ISO 9141.
"""
import struct
import threading
import time
from collections import deque
from typing import Deque, List, Optional

from .. import realtime as _realtime
from . import j2534 as J
from .sm2can import (CFG1_LEN, CFG2_LEN, CFG3_LEN, CMD_APPLY, CMD_CFG1, CMD_CFG2, CMD_CFG3,
                     CMD_CHANNEL_INIT, STATUS_LEN)
from .sm2usb import Sm2, Sm2Error

CMD_KLINE_POLL = 0x1D
CMD_FIVE_BAUD = 0x04
MASK_CONNECT, MASK_CLEAR_RX, MASK_CLOSE = 0x0003, 0x0002, 0x0001
FLAG_DATA, FLAG_BOUNDARY = 0x80, 0x10
WRITE_CHUNK = 16
PENDING_FLUSH_S = 0.05
FIVE_BAUD_TIMING = (8, 300, 500, 300, 300, 25, 50)         # u16 LE words, as the DLL sends them
FIVE_BAUD_TIMEOUT_MS = 6000

#: block 1 for ISO 9141 as the DLL sends it on connect: payload offset -> value;
#: offsets 18-21 are the baud rate (u32 LE; 124800 = 80 e7 01 00), 26 / 28 / 39 are P4_MIN / P3_MIN / P1_MAX in ms.
_B1_CONSTANTS = {17: 1, 22: 7, 23: 7, 25: 3, 32: 8, 35: 0x64, 36: 0x19, 37: 0x2C, 38: 0x01}
OFF_BAUD, OFF_P4, OFF_P3, OFF_P1 = 18, 26, 28, 39
DEFAULT_P1_MS, DEFAULT_P3_MS, DEFAULT_P4_MS = 20, 55, 5
OFF_LOOPBACK = 17
P3_FLOOR_MS = {J.ISO9141: 1, J.ISO14230: 20}          # what the DLL turns P3_MIN=0 into
FAST_INIT_WORDS = (300, 25, 50, 0x1020)
CMD_FAST_INIT = 0x10
TESTER_ADDRESS = 0xF1


def config_block1(baud: int, p1_ms: int = DEFAULT_P1_MS, p3_ms: int = DEFAULT_P3_MS,
                  p4_ms: int = DEFAULT_P4_MS, loopback: int = 1) -> bytes:
    b = bytearray(CFG1_LEN)
    for off, val in _B1_CONSTANTS.items():
        b[off] = val
    b[OFF_LOOPBACK] = loopback
    struct.pack_into("<I", b, OFF_BAUD, baud)
    b[OFF_P4], b[OFF_P3], b[OFF_P1] = (_ms(p4_ms, False, what="P4_MIN"), _ms(p3_ms, False, what="P3_MIN"),
                                      _ms(p1_ms, False, what="P1_MAX"))
    return bytes(b)


def five_baud_request(address: int) -> bytes:
    return bytes(4) + bytes([address & 0xFF]) + b"".join(struct.pack("<H", w) for w in FIVE_BAUD_TIMING)


def parse_records(payload: bytes):
    """The ``(byte, flag)`` records in a ``0x1D`` reply (after the status block)."""
    p = bytes(payload)
    return [(p[off], p[off + 1]) for off in range(STATUS_LEN, len(p) - 1, 2)]


def _ms(value: int, units_half: bool = True, floor: int = 0, what: str = "value") -> int:
    """Milliseconds for a one-byte field of block 1 from a J2534 value (0.5 ms units
    when ``units_half``).  The field is a byte: a value that does not fit is refused
    rather than wrapped (300 ms would have become 44 ms)."""
    ms = max(floor, int(value) // 2 if units_half else int(value))
    if ms > 255:
        raise Sm2Error("SM2 K-line: %s of %d ms does not fit the device (at most 255 ms)"
                       % (what, ms))
    return ms


class Sm2KLineChannel:
    """An ISO 9141 channel on an open :class:`Sm2`."""

    def __init__(self, dev: Sm2, baud: int = 10400, protocol: int = J.ISO9141,
                 background: bool = True):
        self.dev = dev
        self.baud = int(baud)
        self.protocol = protocol
        self.loopback = 1
        self._tester = 0                    # second poll byte: TESTER_ADDRESS after a fast init
        self.p1_ms, self.p3_ms, self.p4_ms = DEFAULT_P1_MS, DEFAULT_P3_MS, DEFAULT_P4_MS
        self._rx = bytearray()
        self._pending = bytearray()         # received bytes whose message has not ended yet
        self._pending_since = 0.0
        self._ack = False
        self.id = 1
        # The vendor DLL polls the device from a thread of its own, about 4000 times a second,
        # whatever the caller is doing; ``background`` does the same so no received byte waits for
        # the caller to come back and ask (False: the caller's reads poll, as before).
        self._cv = threading.Condition()
        self._thread = None
        self._stop = threading.Event()
        self._err = None
        self._writers = 0                   # callers waiting for the device: the poller steps aside
        self._open()
        if background:
            self._thread = threading.Thread(target=self._run, name="sm2-kline-poll", daemon=True)
            self._thread.start()

    def _run(self):
        _realtime.begin_thread()
        try:
            self._poll_loop()
        finally:
            _realtime.end_thread()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as ex:                          # noqa: BLE001 - handed to the reader
                with self._cv:
                    self._err = ex
                    self._cv.notify_all()
                return                                       # no retry loop: a dead cable cannot spin
            if self._writers:
                time.sleep(0.0003)                           # let the waiting caller have the device
            elif self._pending and time.monotonic() - self._pending_since > PENDING_FLUSH_S:
                with self._cv:
                    self._rx += self._pending                # a boundary that never came
                    self._pending.clear()
                    self._cv.notify_all()

    class _Exclusive:
        """Commands the poller must not get between."""

        def __init__(self, ch):
            self.ch = ch

        def __enter__(self):
            self.ch._writers += 1
            self.ch.dev.lock.acquire()

        def __exit__(self, *exc):
            self.ch.dev.lock.release()
            self.ch._writers -= 1

    def _exclusive(self):
        return self._Exclusive(self)

    def _cmd(self, cmd: int, payload: bytes = b"", timeout_ms: int = 2000):
        pkt = self.dev.command(cmd, payload, timeout_ms)
        if not pkt.ok:
            raise Sm2Error("SM2 refused command 0x%02X (status 0x%02X)" % (cmd, pkt.status))
        return pkt

    def _blocks(self):
        self._cmd(CMD_CFG1, config_block1(self.baud, self.p1_ms, self.p3_ms, self.p4_ms, self.loopback))
        self._cmd(CMD_CFG2, bytes(CFG2_LEN))
        self._cmd(CMD_CFG3, bytes(CFG3_LEN))

    def _open(self):
        self._cmd(CMD_CHANNEL_INIT)
        self._cmd(CMD_CFG1, bytes(CFG1_LEN))
        self._cmd(CMD_CFG2, bytes(CFG2_LEN))
        self._cmd(CMD_CFG3, bytes(CFG3_LEN))
        self._blocks()
        self._cmd(CMD_APPLY, struct.pack("<H", MASK_CONNECT))
        self._poll()                                    # the DLL polls straight after connecting

    # ---- configuration ----
    def pass_all(self, extended: bool = False):
        """The device passes everything and the DLL's filter sent it nothing."""

    def set_config(self, **params):
        changed = False
        for name, val in params.items():
            pid = getattr(J, name)
            val = int(val)
            if pid == J.P1_MAX:
                self.p1_ms, changed = _ms(val, floor=1, what="P1_MAX"), True
            elif pid == J.P3_MIN:
                self.p3_ms, changed = _ms(val, floor=P3_FLOOR_MS[self.protocol], what="P3_MIN"), True
            elif pid == J.P4_MIN:
                self.p4_ms, changed = _ms(val, what="P4_MIN"), True
            elif pid == J.DATA_RATE:
                self.baud, changed = val, True
            elif pid in (J.LOOPBACK, J.FIVE_BAUD_MOD):
                pass                        # the DLL keeps these to itself (no block is resent)
            elif pid == J.PARITY:
                if val:
                    raise Sm2Error("parity other than none is not traced on the SM2 yet")
            else:
                raise Sm2Error("SM2 K-line: config parameter 0x%02X not traced" % pid)
        if changed:
            with self._exclusive():
                self._blocks()

    def set_baud(self, baud: int):
        self.set_config(DATA_RATE=baud)

    # ---- init ----
    def five_baud_init(self, address: int) -> bytes:
        """The key bytes. ``address`` is sent as given: it must carry its parity bit."""
        with self._exclusive():
            pkt = self.dev.command(CMD_FIVE_BAUD, five_baud_request(address), FIVE_BAUD_TIMEOUT_MS)
        if pkt.status == 0x0B:
            raise Sm2Error("5-baud init: the controller at 0x%02X did not answer" % address)
        if not pkt.ok or len(pkt.payload) < 6:
            raise Sm2Error("5-baud init failed (status 0x%02X)" % pkt.status)
        with self._cv:
            self._rx.clear()
            self._pending.clear()
        return bytes(pkt.payload[4:6])

    def fast_init(self, request: bytes) -> bytes:
        """ISO 14230 hardware fast init: ``request`` is the whole StartCommunication frame
        with its checksum; returns the ECU's answer frame."""
        if self.protocol != J.ISO14230:
            raise Sm2Error("fast init needs an ISO 14230 channel")
        body = b"".join(struct.pack("<H", w) for w in FAST_INIT_WORDS) + bytes(request)
        with self._exclusive():
            pkt = self.dev.command(CMD_FAST_INIT, body, FIVE_BAUD_TIMEOUT_MS)
        if not pkt.ok or not pkt.payload:
            raise Sm2Error("fast init: no answer from the controller (status 0x%02X)" % pkt.status)
        with self._cv:
            self._rx.clear()
            self._pending.clear()
            self._ack = False
            self._tester = TESTER_ADDRESS
        return bytes(pkt.payload)

    # ---- bytes ----
    def _take(self, pkt):
        """Records are bytes (flag 0x80) and message boundaries (flag 0x10, P1_MAX of silence
        after the last byte).  Like the DLL, a message is handed over when its boundary
        arrives, and the next poll acknowledges it."""
        with self._cv:
            self._ack = False
            for value, flag in parse_records(pkt.payload):
                if flag & FLAG_DATA:
                    if not self._pending:
                        self._pending_since = time.monotonic()
                    self._pending.append(value)
                elif flag & FLAG_BOUNDARY:
                    self._rx += self._pending
                    self._pending.clear()
                    self._ack = True
            if self._rx:
                self._cv.notify_all()

    def _poll(self, timeout_ms: int = 200):
        with self._cv:
            tester, self._tester = self._tester, 0      # the DLL sends it on one poll only
            ack = self._ack
        pkt = self.dev.command(CMD_KLINE_POLL, bytes([1 if ack else 0, tester, 0, 2]), timeout_ms)
        if not pkt.ok:
            raise Sm2Error("SM2 K-line poll failed (status 0x%02X)" % pkt.status)
        self._take(pkt)

    def write(self, data: bytes, flags: int = 0, timeout_ms: int = 100):
        data = bytes(data)
        for i in range(0, len(data), WRITE_CHUNK):
            body = bytes([1, 0, 0, 2]) + b"".join(bytes([b, FLAG_DATA]) for b in data[i:i + WRITE_CHUNK])
            with self._exclusive():
                pkt = self.dev.command(CMD_KLINE_POLL, body, max(timeout_ms, 500))
            if not pkt.ok:
                raise Sm2Error("SM2 K-line write failed (status 0x%02X)" % pkt.status)
            self._take(pkt)

    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[J.Msg]:
        """The bytes received so far as one message (J2534 ``PassThruReadMsgs``
        shape); waits up to ``timeout_ms`` for the first byte."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        if self._thread is None:
            while True:
                self._poll()
                if self._rx or time.monotonic() >= deadline:
                    break
                if self._pending and time.monotonic() - self._pending_since > PENDING_FLUSH_S:
                    self._rx += self._pending   # a boundary that never came: hand the bytes over
                    self._pending.clear()
                    break
        with self._cv:
            while self._thread is not None and not self._rx:
                if self._err is not None:
                    raise self._err
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.05))
            if not self._rx:
                return []
            data, self._rx = bytes(self._rx), bytearray()
        return [J.Msg(J.ISO9141, 0, 0, int(time.monotonic() * 1e6), data)]

    def clear_rx(self):
        with self._exclusive():
            self._cmd(CMD_APPLY, struct.pack("<H", MASK_CLEAR_RX), 1000)
        with self._cv:
            self._rx.clear()
            self._pending.clear()
            self._ack = False

    def clear_tx(self):
        pass

    def disconnect(self):
        if self.id is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        try:
            self._cmd(CMD_APPLY, struct.pack("<H", MASK_CLOSE), 1000)
            self._cmd(CMD_CFG1, bytes(CFG1_LEN))
            self._cmd(CMD_CFG2, bytes(CFG2_LEN))
            self._cmd(CMD_CFG3, bytes(CFG3_LEN))
        finally:
            self.id = None

    close = disconnect


class Sm2KLineDevice:
    """What :class:`J2534RawKLine` calls ``dev``: opens K-line channels."""

    def __init__(self, sm2: Sm2, background: bool = True):
        self.sm2 = sm2
        self.background = background
        self.channels: List[Sm2KLineChannel] = []

    def connect(self, protocol: int, baud: int, flags: int = 0) -> Sm2KLineChannel:
        if protocol not in (J.ISO9141, J.ISO14230):
            raise Sm2Error("the SM2 K-line layer handles ISO 9141 and ISO 14230 only")
        ch = Sm2KLineChannel(self.sm2, baud, protocol, self.background)
        self.channels.append(ch)
        return ch

    def battery_voltage(self) -> Optional[float]:
        return self.sm2.battery_voltage()

    def close(self):
        for ch in self.channels:
            try:
                ch.disconnect()
            except Exception:
                pass
        self.sm2.close()


__all__ = ["Sm2KLineChannel", "Sm2KLineDevice", "config_block1", "five_baud_request",
           "parse_records"]
