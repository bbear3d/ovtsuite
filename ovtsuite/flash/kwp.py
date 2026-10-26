# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""KWP2000 (ISO 14230) and KW1281 dialogs over a :class:`KLine`.

Ported from KWHack's ``KW2000Dialog``/``Kwp2000Message`` and the parts of
``KW1281Dialog`` the EDC15 flash/EEPROM paths need (Connect = receive the
ECU's identification blocks, acknowledging each; EndCommunication).

KWP2000 frame: ``[fmt][dest][src][len?][service][body...][cks]`` where
``fmt = 0x80 | (1+len(body))`` for addressed messages (dest 0x01, src 0xF1) or
just ``1+len(body)`` address-less; a length byte follows when the count is
over 63. The checksum is the 8-bit sum of everything before it.
"""
import time

from .transport import KLineError, KLineTimeout, check_stop

TESTER = 0xF1


def _pause(seconds):
    """``time.sleep``, skipped for nothing to wait (a zero R6)."""
    if seconds > 0:
        time.sleep(seconds)


def _write_then_read(link):
    """The link's write for a byte the caller reads straight after: one
    device call with that read where the link can (the J2534 byte-stream
    link's ``write_before_read``); otherwise its ``write_byte`` for one byte
    and ``write_bytes`` for a frame, as before."""
    w = getattr(link, "write_before_read", None)
    if w is not None:
        return w
    return lambda data: (link.write_byte(data[0]) if len(data) == 1
                         else link.write_bytes(data))


# service ids
SVC_START_DIAG = 0x10
SVC_SECURITY = 0x27
SVC_REQ_DOWNLOAD = 0x34
SVC_TRANSFER = 0x36
SVC_START_ROUTINE_LID = 0x31
NRC_PENDING = 0x78
#: responsePending frames one request may draw before it is given up on -- the
#: same bound the CAN transports use, so a stuck ECU cannot loop forever.
MAX_PENDING = 64


class NegativeResponse(KLineError):
    def __init__(self, service, nrc):
        self.service = service
        self.nrc = nrc
        super().__init__("KWP2000 NAK for service 0x%02X: NRC 0x%02X" % (service, nrc))


class Message:
    __slots__ = ("fmt", "dest", "src", "length", "service", "body")

    def __init__(self, fmt, dest, src, length, service, body):
        self.fmt = fmt
        self.dest = dest
        self.src = src
        self.length = length
        self.service = service
        self.body = bytes(body)

    def is_positive(self, service):
        return (service | 0x40) == self.service

    def __repr__(self):
        return "Message(svc=0x%02X body=%s)" % (self.service, self.body.hex())


def build_frame(service, body, exclude_addresses=False, dest=0x01, src=TESTER):
    body = bytes(body)
    n = len(body) + 1
    fmt = 0 if n > 63 else n
    out = bytearray()
    if exclude_addresses:
        out.append(fmt)
    else:
        out.append(fmt | 0x80)
        out.append(dest)
        out.append(src)
    if n > 63:
        out.append(n & 0xFF)
    out.append(service)
    out += body
    out.append(sum(out) & 0xFF)
    return bytes(out)


class Kwp2000:
    """Minimal KW2000Dialog: send_receive with responsePending handling."""

    P3 = 0.055          # inter-message gap
    P4 = 0.005          # inter-byte gap

    def __init__(self, link, controller=0x01, log=None):
        self.k = link
        self.controller = controller
        self._log = log or (lambda s: None)
        # fast timing (live data): the whole frame in one write, echoes
        # drained together -- KWHack's "batched send"
        self.fast = False
        #: optional callable()->bool; a responsePending wait gives up when true
        self.is_stop = None

    def send(self, service, body=b"", exclude_addresses=False, _then_read=False):
        frame = build_frame(service, body, exclude_addresses, dest=self.controller)
        time.sleep(self.P3)
        if self.fast:
            (_write_then_read(self.k) if _then_read else self.k.write_bytes)(frame)
            return
        for b in frame[:-1]:
            self.k.write_byte(b)
            time.sleep(self.P4)
        self.k.write_byte(frame[-1])

    def receive(self):
        fmt = self.k.read_byte()
        dest = src = length = None
        if fmt & 0x80:
            dest = self.k.read_byte()
            src = self.k.read_byte()
        if (fmt & 63) == 0:
            length = self.k.read_byte()
        body_len = (length if length is not None else (fmt & 63)) - 1
        service = self.k.read_byte()
        body = self.k.read(body_len) if body_len > 0 else b""
        self.k.read_byte()                              # checksum (not enforced, as KWHack)
        return Message(fmt, dest, src, length, service, body)

    def send_receive(self, service, body=b"", exclude_addresses=False):
        self.send(service, body, exclude_addresses, _then_read=True)
        msg = self.receive()
        pending = 0
        while True:
            if msg.src is not None:
                if msg.src != self.controller:
                    raise KLineError("Unexpected SrcAddress 0x%02X" % msg.src)
                if msg.dest != TESTER:
                    raise KLineError("Unexpected DestAddress 0x%02X" % msg.dest)
            if msg.service == 0x7F:
                if len(msg.body) >= 2 and msg.body[0] == service and msg.body[1] == NRC_PENDING:
                    pending += 1
                    if pending > MAX_PENDING:
                        raise KLineTimeout("KWP2000: ECU stayed in responsePending "
                                           "to service 0x%02X" % service)
                    check_stop(getattr(self, "is_stop", None))
                    msg = self.receive()
                    continue
                raise NegativeResponse(service, msg.body[1] if len(msg.body) > 1 else 0)
            if not msg.is_positive(service):
                raise KLineError("Unexpected response 0x%02X to service 0x%02X"
                                 % (msg.service, service))
            return msg


# ---------------------------------------------------------------- KW1281
BT_ACK = 0x09
BT_NAK = 0x0A
BT_END = 0x06

R6 = 0.002              # inter-byte delay
BLOCK_LEAD = 0.025
#: KWHack's KW1281 "fast timing" (live data): R6 at the 1 ms spec minimum
#: (SAE J2818 allows 1-50 ms) and no block lead-in -- the 25 ms lead is for
#: one picky cluster (4D0919035AJ); engine ECUs do not need it.
R6_FAST = 0.001


class Kw1281:
    """The KW1281 block dialog: connect (receive the identification blocks),
    blocks both ways, and a clean end (which flips an EDC15 into its KWP2000
    boot stub on the next slow-init)."""

    def __init__(self, link, log=None, fast=False):
        self.k = link
        self._log = log or (lambda s: None)
        self._counter = None
        self._connected = False
        #: trims the inter-byte and block lead-in delays (KWHack's
        #: Kw1281FastTiming, always on for its live data)
        self.fast = bool(fast)

    @property
    def _r6(self):
        """The wait before answering a byte. Fast timing: R6 at its 1 ms
        minimum, less the line silence the link already waited before
        handing the byte over (its ``rx_settle_s``: the J2534 byte-stream
        link's P1_MAX), so it is not waited twice. Otherwise R6 as always."""
        if not self.fast:
            return R6
        return max(0.0, R6_FAST - float(getattr(self.k, "rx_settle_s", 0.0) or 0.0))

    def connect(self):
        """Receive the ECU's identification blocks (ACKing each) until it
        sends an ACK/NAK. Returns the list of raw blocks."""
        self._connected = True
        return self._receive_blocks()

    def end_communication(self):
        if self._connected:
            self.send_block([BT_END])
            self._connected = False

    # ---- blocks ----
    def send_block(self, payload):
        if not self.fast:
            time.sleep(BLOCK_LEAD)
        block = bytearray([len(payload) + 2, self._counter & 0xFF]) + bytearray(payload)
        self._counter = (self._counter + 1) & 0xFF
        r6 = self._r6
        _pause(r6)
        put = _write_then_read(self.k)
        for b in block:
            put(bytes([b]))                             # with its complement's read
            self.k.read_complement(b)
            _pause(r6)
        self.k.write_byte(0x03)                         # block end, not ACKed

    def _receive_blocks(self):
        blocks = []
        while True:
            blk = self.receive_block()
            blocks.append(blk)
            if blk[2] in (BT_ACK, BT_NAK):
                break
            self.send_block([BT_ACK])
        return blocks

    def _read_and_ack(self):
        b = self.k.read_byte()
        _pause(self._r6)
        _write_then_read(self.k)(bytes([(~b) & 0xFF]))  # a read always follows
        return b

    def _read_and_ack_first(self, count=0):
        if count > 5:
            raise KLineError("KW1281: cannot sync (repeated 0x55)")
        b = self.k.read_byte()
        if b == 0x55:                                   # sync repeated: re-do keyword ack
            self.k.read_byte()
            msb = self.k.read_byte()
            time.sleep(0.025)
            _write_then_read(self.k)(bytes([(~msb) & 0xFF]))
            return self._read_and_ack_first(count + 1)
        _pause(self._r6)
        _write_then_read(self.k)(bytes([(~b) & 0xFF]))
        return b

    def receive_block(self):
        length = self._read_and_ack_first()
        counter = self._read_and_ack()
        if self._counter is None:
            self._counter = counter
        elif counter != self._counter:
            raise KLineError("KW1281: block counter 0x%02X, expected 0x%02X"
                             % (counter, self._counter))
        self._counter = (self._counter + 1) & 0xFF
        title = self._read_and_ack()
        data = bytearray()
        for _ in range(max(0, length - 3)):
            data.append(self._read_and_ack())
        end = self.k.read_byte()
        if end != 0x03:
            raise KLineError("KW1281: block end 0x%02X, expected 0x03" % end)
        return bytes([length, counter, title]) + bytes(data)
