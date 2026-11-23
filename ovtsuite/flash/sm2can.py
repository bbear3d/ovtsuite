# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CAN channel on a Scanmatik SM2 over libusb (:class:`ovtsuite.flash.sm2usb.Sm2`).

Presents the same surface as :class:`ovtsuite.flash.openport.OpenPortChannel`
(``write_can`` / ``read`` / ``pass_all`` / ``pass_ids``), so
:class:`ovtsuite.flash.tp20_transport.Tp20Channel` runs on it unchanged.

Everything here is read from a trace of Scanmatik's DLL on a live bus
(``docs/SM2_USB_PROTOCOL.md``, "Live CAN on the bench"):

* open: ``0x88``, then the three configuration blocks ``0x50`` (0x130 bytes),
  ``0x51`` / ``0x52`` (0xB8 each), all zero.
* connect: the same three blocks with block 1 carrying the bit rate (u32 LE at
  payload offset 124; checked at 125 / 250 / 500 kbit/s) and the constants the DLL
  sends; the ``0x52`` reply is a status block; ``0x53`` with mask ``0x0030``
  answers with the channel handle (2 bytes, ``80 02``).
* pass-all filter: block 1 again with one 59-byte entry appended and the count at
  offset 132, then blocks 2 and 3.
* transmit ``0x1C``: ``C0 | handle | dlc u32 | id u32 LE | data``; poll ``0x1C``:
  ``op | handle`` with op ``0x80``, or ``0xC0`` when the previous reply carried frames.
* every ``0x1C`` reply is a 0x14-byte status block followed by one 16-byte record
  per received frame (``dlc u32 | id u32 | 8 data bytes``).

Not verified on hardware: 29-bit identifiers (refused here), hardware id filters
(``pass_ids`` filters in software), ISO 15765 and the non-CAN protocols.  The
DLL's ``0x12`` authentication is not sent; the bench showed it is not needed
for the battery read (CAN without it: see the bench notes in the protocol doc).
"""
import struct
import threading
import time
from collections import deque
from typing import Deque, Iterable, List, Optional

from .. import realtime as _realtime
from . import j2534 as J
from .sm2usb import Sm2, Sm2Error

CMD_CHANNEL_INIT = 0x88
CMD_CFG1, CMD_CFG2, CMD_CFG3 = 0x50, 0x51, 0x52
CMD_APPLY = 0x53
CMD_CAN = 0x1C

CFG1_LEN, CFG2_LEN, CFG3_LEN = 0x130, 0xB8, 0xB8
FILTER_ENTRY_LEN = 0x3B
STATUS_LEN = 0x14
RECORD_LEN = 0x10
MASK_CONNECT, MASK_CLOSE = 0x0030, 0x0010
OFF_BAUD, OFF_FILTERS = 124, 132
#: bytes the DLL sets in block 1 on connect that are not named yet (offset, value)
CFG1_CONSTANTS = ((120, 1), (121, 2), (122, 1), (134, 0x0A), (136, 0x0A))
PASS_ENTRY_WORD = 0x80000000
MAX_DLC = 8
POLL_BACKOFF_S = 0.005      # pause after a failed poll, so a dead cable cannot spin the thread
POLL_GIVE_UP = 100          # consecutive failed polls (about 0.5 s plus the timeouts) before the poller stops


def config_block1(baud: int, filters: int = 0) -> bytes:
    """Block 1 as the DLL builds it for a CAN channel: ``filters`` 59-byte
    pass-all entries are appended (the DLL never sent more than one)."""
    b = bytearray(CFG1_LEN)
    for off, val in CFG1_CONSTANTS:
        b[off] = val
    struct.pack_into("<I", b, OFF_BAUD, baud)
    b[OFF_FILTERS] = filters
    for _ in range(filters):
        e = bytearray(FILTER_ENTRY_LEN)
        struct.pack_into("<I", e, 0, PASS_ENTRY_WORD)
        b += e
    return bytes(b)


def parse_status(payload: bytes):
    """Split a ``0x1C`` reply into its frames: ``[(can_id, data), ...]``.
    Anything after the 0x14-byte status block is a run of 16-byte records."""
    out = []
    p = bytes(payload)
    for off in range(STATUS_LEN, len(p) - RECORD_LEN + 1, RECORD_LEN):
        dlc, cid = struct.unpack_from("<II", p, off)
        out.append((cid, p[off + 8:off + 8 + min(dlc, MAX_DLC)]))
    return out


class Sm2CanChannel:
    """A CAN channel on an open :class:`Sm2`."""

    def __init__(self, dev: Sm2, baud: int = 500000, extended: bool = False,
                 background: bool = False):
        if extended:
            raise Sm2Error("29-bit CAN is not verified on the SM2 yet")
        self.dev = dev
        self.baud = baud
        self.handle = b"\x80\x02"
        self._rx: Deque = deque()               # (can_id, data, timestamp_us)
        self._ack = False                       # last reply carried frames
        self._ids: Optional[frozenset] = None   # software id filter; None = pass all
        self.id = None
        self._open()
        # The vendor DLL polls the device from a thread of its own whatever the caller
        # is doing. Without that, a pause in the caller (TP 2.0 waits 0.25 s before it
        # sets a channel up again) leaves the device's receive queue to fill with bus
        # traffic, and it stops accepting USB writes until it is re-plugged.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sm2-can-poll", daemon=True)
        if background:
            self._thread.start()

    def _run(self):
        _realtime.begin_thread()
        try:
            fails = 0
            while not self._stop.is_set():
                try:
                    self._poll(100)
                    fails = 0
                except Sm2Error:
                    # A slow reply: Sm2.command resynchronises (drain + ping) before the
                    # next command, so a late reply is never taken for the next poll's.
                    # Keep failing and the cable is dead: back off, then give up (the
                    # caller's next read polls itself and reports the error).
                    fails += 1
                    if fails >= POLL_GIVE_UP:
                        return
                    self._stop.wait(POLL_BACKOFF_S)
                    continue
                except Exception:                 # noqa: BLE001 - the caller's next command reports it
                    return
                self._stop.wait(0.001)
        finally:
            _realtime.end_thread()

    # ---- set-up ----
    def _blocks(self, filters: int):
        d = self.dev
        d.command(CMD_CFG1, config_block1(self.baud, filters), 2000)
        d.command(CMD_CFG2, bytes(CFG2_LEN), 2000)
        return d.command(CMD_CFG3, bytes(CFG3_LEN), 2000)

    def _open(self):
        d = self.dev
        for cmd, n in ((CMD_CHANNEL_INIT, 0), (CMD_CFG1, CFG1_LEN), (CMD_CFG2, CFG2_LEN),
                       (CMD_CFG3, CFG3_LEN)):
            pkt = d.command(cmd, bytes(n), 2000)
            if not pkt.ok:
                raise Sm2Error("SM2 refused command 0x%02X (status 0x%02X)" % (cmd, pkt.status))
        pkt = self._blocks(0)
        if not pkt.ok or len(pkt.payload) != STATUS_LEN:
            raise Sm2Error("SM2 channel set-up: unexpected reply to block 3 (%s)" % pkt.payload.hex())
        pkt = d.command(CMD_APPLY, struct.pack("<H", MASK_CONNECT), 2000)
        if not pkt.ok or len(pkt.payload) < 2:
            raise Sm2Error("SM2 did not open the channel (status 0x%02X)" % pkt.status)
        self.handle = pkt.payload[:2]
        self.id = int.from_bytes(self.handle, "little")
        pkt = self._blocks(1)                   # pass-all filter, as the DLL starts it
        if not pkt.ok:
            raise Sm2Error("SM2 refused the pass filter (status 0x%02X)" % pkt.status)

    # ---- filters ----
    def pass_all(self, extended: bool = False):
        self._ids = None

    def pass_ids(self, ids: Iterable[int]):
        """Software filter: the device passes everything, frames on other ids are dropped here."""
        self._ids = frozenset(int(i) for i in ids)

    def clear_filters(self):
        self._ids = None

    # ---- messages ----
    def _take(self, pkt):
        frames = parse_status(pkt.payload)
        self._ack = bool(frames)
        ts = int(time.monotonic() * 1e6)
        for cid, data in frames:
            if self._ids is None or cid in self._ids:
                self._rx.append((cid, data, ts))

    def _poll(self, timeout_ms: int = 100):
        op = 0xC0 if self._ack else 0x80
        pkt = self.dev.command(CMD_CAN, bytes([op]) + self.handle, timeout_ms)
        if not pkt.ok:
            raise Sm2Error("SM2 poll failed (status 0x%02X)" % pkt.status)
        self._take(pkt)

    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        """Queue one frame. Does not wait for the bus acknowledge."""
        if extended:
            raise Sm2Error("29-bit CAN is not verified on the SM2 yet")
        payload = bytes(payload)
        if len(payload) > MAX_DLC:
            raise Sm2Error("CAN frame of %d bytes" % len(payload))
        body = b"\xC0" + self.handle + struct.pack("<II", len(payload), can_id) + payload
        pkt = self.dev.command(CMD_CAN, body, max(timeout_ms, 100))
        if not pkt.ok:
            raise Sm2Error("SM2 refused the frame (status 0x%02X)" % pkt.status)
        self._take(pkt)

    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[J.Msg]:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if self._thread.is_alive():
                if self._rx or time.monotonic() >= deadline:
                    break                          # the poller fills _rx; a 0 ms read just looks
                time.sleep(0.0005)
                continue
            self._poll(max(1, min(timeout_ms, 100)))
            if self._rx or time.monotonic() >= deadline:
                break
        out = []
        while self._rx and len(out) < max_msgs:
            cid, data, ts = self._rx.popleft()
            out.append(J.Msg(J.CAN, 0, 0, ts, cid.to_bytes(4, "big") + data))
        return out

    def clear_rx(self):
        self._rx.clear()

    def clear_tx(self):
        pass

    def disconnect(self):
        if self.id is None:
            return
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()
            if self._thread.is_alive() and self._thread is not threading.current_thread():
                self._thread.join(1.0)
        try:
            self.dev.command(CMD_APPLY, struct.pack("<H", MASK_CLOSE), 1000)
        finally:
            self.id = None

    close = disconnect


__all__ = ["Sm2CanChannel", "config_block1", "parse_status"]
