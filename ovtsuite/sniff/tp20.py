# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VW TP 2.0 (transport protocol over CAN) decoder, plus KWP2000 service
names, for annotating captures and reassembling the KWP messages that
diagnostic tools exchange with CAN-only EDC16s.

Frame classes (first payload byte = opcode nibble | sequence nibble):

  channel setup     CAN id 0x200 (tester -> ECU logical address in byte 0)
                    [dest, C0, xx, 1x, lo, hi, app]: the second id pair is
                    the id the tester will LISTEN on (0x300 as VCDS uses),
                    the first pair is left invalid (bit 4 of the high byte)
                    answer on 0x200 + dest: [00, D0 ok / D6..D8 refused,
                    lo, hi, lo, hi, app]: first pair = the id the ECU sends
                    on (= tester's listen id, echoed), second pair = the id
                    the ECU listens on (0x740 typically), which the tester
                    must use from now on
  parameters        A0 request [bs, T1, T2, T3, T4] / A1 response, A3 test
                    (keep-alive), A4 break, A8 disconnect
  data              0x = more follows, ACK expected at block end
                    1x = last frame, ACK expected
                    2x = more follows, no ACK    3x = last frame, no ACK
  ack               Bx = ready for next (x = sequence expected)
                    9x = ack, not ready

A KWP message spans frames: the first data frame carries a 16-bit big-endian
length, then the KWP bytes (service + data, no K-line header/checksum).

The decoder is a pure function of the frame sequence, so it serves the live
sniffer and offline capture files alike; nothing here is bench-verified yet
-- the sniffer exists to check exactly these rules against a real session.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SETUP_ID = 0x200
KWP_SERVICES = {
    0x10: "startDiagnosticSession", 0x11: "ecuReset", 0x14: "clearDiagnosticInformation",
    0x17: "readStatusOfDTC", 0x18: "readDTCByStatus", 0x1A: "readEcuIdentification",
    0x20: "stopDiagnosticSession", 0x21: "readDataByLocalId", 0x22: "readDataByCommonId",
    0x23: "readMemoryByAddress", 0x27: "securityAccess", 0x2C: "dynamicallyDefineLocalId",
    0x30: "ioControlByLocalId", 0x31: "startRoutineByLocalId", 0x32: "stopRoutineByLocalId",
    0x33: "requestRoutineResultsByLocalId", 0x34: "requestDownload", 0x35: "requestUpload",
    0x36: "transferData", 0x37: "requestTransferExit", 0x3B: "writeDataByLocalId",
    0x3D: "writeMemoryByAddress", 0x3E: "testerPresent", 0x81: "startCommunication",
    0x82: "stopCommunication", 0x83: "accessTimingParameter", 0x7F: "negativeResponse",
}
NRC = {0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
       0x21: "busy-repeatRequest", 0x22: "conditionsNotCorrect", 0x23: "routineNotComplete",
       0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x35: "invalidKey",
       0x36: "exceedNumberOfAttempts", 0x37: "requiredTimeDelayNotExpired",
       0x40: "downloadNotAccepted", 0x71: "transferSuspended", 0x78: "responsePending",
       0x80: "serviceNotSupportedInActiveSession"}


def kwp_name(svc: int) -> str:
    if svc == 0x7F:
        return "negativeResponse"
    base = (svc & 0xBF) if svc & 0x40 else svc      # positive response = request + 0x40
    name = KWP_SERVICES.get(base)
    if name is None:
        return "service 0x%02X" % svc
    return name + (" +" if svc & 0x40 and svc != 0x7F else "")


def describe_kwp(msg: bytes) -> str:
    """One-line human reading of a KWP2000 message body."""
    if not msg:
        return ""
    svc = msg[0]
    if svc == 0x7F and len(msg) >= 3:
        return "NAK %s: %s" % (kwp_name(msg[1]), NRC.get(msg[2], "NRC 0x%02X" % msg[2]))
    s = kwp_name(svc)
    if len(msg) > 1:
        s += " " + msg[1:min(len(msg), 9)].hex(" ")
        if len(msg) > 9:
            s += " ... (%d bytes)" % (len(msg) - 1)
    return s


@dataclass
class Frame:
    t: float                      # seconds
    can_id: int
    data: bytes
    tx: bool = False              # sent by us (echo) rather than seen on the bus


@dataclass
class Event:
    """A decoded thing: a frame classification or a reassembled message."""
    t: float
    can_id: int
    kind: str                     # setup | setup-ack | param | ack | data | message | raw
    text: str
    payload: bytes = b""
    channel: str = ""             # "T->E" / "E->T" or the id pair


@dataclass
class _Side:
    """Reassembly state for one direction of a channel."""
    buf: bytearray = field(default_factory=bytearray)
    expect: Optional[int] = None  # declared message length
    seq: Optional[int] = None


@dataclass
class TpChannel:
    ecu: int                      # logical address (0x01 engine)
    tester_rx: int                # id the tester listens on (ECU sends here)
    ecu_rx: int                   # id the ECU listens on (tester sends here)
    sides: Dict[int, _Side] = field(default_factory=dict)
    bs: Optional[int] = None
    t1: Optional[int] = None


class Tp20Decoder:
    """Feed frames in bus order; collect :class:`Event` objects."""

    def __init__(self):
        self.channels: Dict[int, TpChannel] = {}      # by can id (either direction)
        self.events: List[Event] = []
        self._pending_setup: Dict[int, Tuple[float, int]] = {}

    # ---- helpers ----
    def _side_name(self, ch: TpChannel, can_id: int) -> str:
        return "T->E" if can_id == ch.ecu_rx else "E->T"

    def _emit(self, ev: Event):
        self.events.append(ev)
        return ev

    # ---- feed ----
    def feed(self, f: Frame) -> List[Event]:
        start = len(self.events)
        d = f.data
        if f.can_id == SETUP_ID and len(d) >= 7 and d[1] == 0xC0:
            dest = d[0]
            listen = d[4] | ((d[5] & 0x0F) << 8)
            self._pending_setup[dest] = (f.t, listen)
            self._emit(Event(f.t, f.can_id, "setup",
                             "channel setup -> ECU 0x%02X: tester will listen on 0x%03X%s, app %d"
                             % (dest, listen, "" if (d[5] & 0x10) == 0 else " (invalid)", d[6]), d))
        elif (f.can_id & 0x700) == SETUP_ID and f.can_id != SETUP_ID and len(d) >= 7 \
                and d[1] in (0xD0, 0xD6, 0xD7, 0xD8):
            dest = f.can_id - SETUP_ID
            ecu_tx = d[2] | ((d[3] & 0x0F) << 8)  # ECU sends here (the tester's listen id)
            ecu_rx = d[4] | ((d[5] & 0x0F) << 8)  # ECU listens here (tester sends to it)
            if d[1] != 0xD0:
                self._emit(Event(f.t, f.can_id, "setup-ack",
                                 "channel setup refused by ECU 0x%02X (%02X)" % (dest, d[1]), d))
            else:
                ch = TpChannel(dest, ecu_tx, ecu_rx)
                self.channels[ecu_rx] = ch
                self.channels[ecu_tx] = ch
                self._emit(Event(f.t, f.can_id, "setup-ack",
                                 "channel open: tester sends on 0x%03X, ECU sends on 0x%03X"
                                 % (ecu_rx, ecu_tx), d))
        elif f.can_id in self.channels and d:
            ch = self.channels[f.can_id]
            side = self._side_name(ch, f.can_id)
            op, seq = d[0] >> 4, d[0] & 0x0F
            if op == 0xA:
                sub = d[0]
                if sub == 0xA0 and len(d) >= 6:
                    self._emit(Event(f.t, f.can_id, "param",
                                     "parameters request: BS %d, T1 %s, T2 %s, T3 %s, T4 %s"
                                     % (d[1], _tval(d[2]), _tval(d[3]), _tval(d[4]), _tval(d[5])),
                                     d, side))
                elif sub == 0xA1 and len(d) >= 6:
                    ch.bs, ch.t1 = d[1], d[2]
                    self._emit(Event(f.t, f.can_id, "param",
                                     "parameters response: BS %d, T1 %s, T3 %s"
                                     % (d[1], _tval(d[2]), _tval(d[4])), d, side))
                elif sub == 0xA3:
                    self._emit(Event(f.t, f.can_id, "param", "channel test (keep-alive)", d, side))
                elif sub == 0xA4:
                    self._emit(Event(f.t, f.can_id, "param", "break", d, side))
                elif sub == 0xA8:
                    self._emit(Event(f.t, f.can_id, "param", "disconnect", d, side))
                    for k in (ch.ecu_rx, ch.tester_rx):
                        self.channels.pop(k, None)
                else:
                    self._emit(Event(f.t, f.can_id, "param", "control 0x%02X" % sub, d, side))
            elif op in (0xB, 0x9):
                self._emit(Event(f.t, f.can_id, "ack",
                                 "ACK%s, next seq %d" % ("" if op == 0xB else " (not ready)", seq),
                                 d, side))
            elif op in (0x0, 0x1, 0x2, 0x3):
                st = ch.sides.setdefault(f.can_id, _Side())
                body = d[1:]
                if st.expect is None:
                    if len(body) >= 2:
                        st.expect = (body[0] << 8) | body[1]
                        st.buf = bytearray(body[2:])
                    else:
                        st.buf = bytearray(body)
                else:
                    st.buf += body
                last = op in (0x1, 0x3)
                self._emit(Event(f.t, f.can_id, "data",
                                 "data seq %d%s%s" % (seq, ", last" if last else "",
                                                      ", ack expected" if op in (0, 1) else ""),
                                 d, side))
                if last or (st.expect is not None and len(st.buf) >= st.expect):
                    msg = bytes(st.buf[:st.expect] if st.expect is not None else st.buf)
                    self._emit(Event(f.t, f.can_id, "message", describe_kwp(msg), msg, side))
                    ch.sides[f.can_id] = _Side()
            else:
                self._emit(Event(f.t, f.can_id, "raw", "opcode 0x%X" % op, d, side))
        else:
            self._emit(Event(f.t, f.can_id, "raw", "", d))
        return self.events[start:]


def _tval(b: int) -> str:
    """TP2.0 timing byte: top 2 bits scale (0.1 ms, 1 ms, 10 ms, 100 ms) x low 6 bits."""
    scale = (0.1, 1.0, 10.0, 100.0)[b >> 6]
    return "%g ms" % (scale * (b & 0x3F))


def decode_kline(frame: bytes) -> Tuple[str, str]:
    """(direction, text) for an ISO 14230 frame as a J2534 device delivers it
    (header bytes included, checksum stripped or not)."""
    if not frame:
        return "", ""
    fmt = frame[0]
    i = 1
    dest = src = None
    if (fmt & 0xC0) in (0x80, 0xC0) and len(frame) >= 3:
        dest, src = frame[1], frame[2]
        i = 3
    if (fmt & 0x3F) == 0 and i < len(frame):
        i += 1                                  # separate length byte
    body = frame[i:]
    direction = ""
    if src is not None:
        direction = "T->E" if src == 0xF1 else ("E->T" if dest == 0xF1 else "%02X->%02X" % (src, dest))
    return direction, describe_kwp(body)
