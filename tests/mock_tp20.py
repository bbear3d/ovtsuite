# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A scripted VW TP 2.0 mock ECU as a CAN channel, for exercising the CAN
transport with the real edc16.py / diag.py state machines."""
from ovtsuite.flash import j2534 as J


class _Msg:
    def __init__(self, can_id, payload):
        self.can_id = can_id
        self.payload = bytes(payload)
        self.protocol = J.CAN
        self.rx_status = 0
        self.tx_flags = 0
        self.timestamp = 0


class MockTp20Ecu:
    """Emulates channel setup, params, and a KWP request->response handler over
    TP 2.0.  ``handler(payload) -> response`` maps a bare KWP request (service+
    data) to a bare KWP response.  Frames the ECU sends carry a 7-byte data
    field like a real device."""

    def __init__(self, handler, dest=0x01, ecu_tx=0x300, ecu_rx=0x740, interframe_byte=0x01):
        self.handler = handler
        self.dest = dest
        self.ecu_tx = ecu_tx           # id the ECU sends on (tester listens)
        self.ecu_rx = ecu_rx           # id the ECU listens on (tester sends)
        self.interframe_byte = interframe_byte
        self.out = []                  # frames queued for the tester
        self._in = bytearray()         # reassembly of tester->ECU data
        self._in_expected = None
        self.tx_seq = 0
        self.opened = False
        self.inject_keepalive = False       # set to make the ECU send A3 before each reply
        self.a1_replies = 0                 # times the tester answered our channel test
        self.got_break = False              # tester sent A4 (would abort a real ECU)
        # live-bus broadcast noise: ids the vehicle floods that are NOT our
        # channel (powertrain CAN 0x280/0x380/... ).  When set, read() pads every
        # batch with these so the transport must discard them, not buffer them.
        self.flood_ids = ()
        self.allowed_ids = None             # device hardware filter (None = pass all)
        self.pending_once = set()           # services that answer 7F svc 78 (once) then the real reply

    # -- CAN channel surface --
    def pass_all(self, extended=False):
        self.allowed_ids = None

    def pass_ids(self, ids, extended=False):
        # model a J2534 hardware filter: only these ids are buffered/delivered
        self.allowed_ids = set(int(i) for i in ids)

    def clear_filters(self):
        self.allowed_ids = None

    def set_config(self, **kw):
        pass

    def set_baud(self, baud):
        pass

    def clear_rx(self):
        pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        payload = bytes(payload)
        if can_id == 0x200:                                  # channel setup
            # payload: dest C0 00 10 listen_lo listen_hi app
            # a real ECU re-establishing the channel (e.g. after entering the
            # programming session) restarts its transport sequence numbers
            self.setup_count = getattr(self, "setup_count", 0) + 1
            self.tx_seq = 0
            self._in = bytearray()
            self._in_expected = None
            self.out = []
            self.out.append(_Msg(0x200 + self.dest, bytes([
                0x00, 0xD0, self.ecu_tx & 0xFF, (self.ecu_tx >> 8) & 0xFF,
                self.ecu_rx & 0xFF, (self.ecu_rx >> 8) & 0xFF, 0x01])))
            self.opened = True
            return
        if can_id != self.ecu_rx:
            return
        op = payload[0] >> 4
        b0 = payload[0]
        if b0 == 0xA0:                                       # params request -> response
            self.out.append(_Msg(self.ecu_tx, bytes([0xA1, 0x0F, 0x8A, 0xFF, self.interframe_byte, 0xFF])))
            return
        if b0 == 0xA1:                                       # tester's answer to our A3 channel test
            self.a1_replies += 1
            return
        if b0 == 0xA4:                                       # break -- would abort a real ECU
            self.got_break = True
            return
        if b0 == 0xA8:                                       # disconnect
            self.opened = False
            return
        if op == 0xB:                                        # tester ACK of our data
            return
        if op in (0x0, 0x1, 0x2, 0x3):                       # inbound data frame
            self._in += payload[1:]
            if self._in_expected is None and len(self._in) >= 2:
                self._in_expected = int.from_bytes(self._in[:2], "big")
            ack_expected = op in (0x0, 0x1)
            last = op in (0x1, 0x3)
            if ack_expected:
                self.out.append(_Msg(self.ecu_tx, bytes([(0xB << 4) | ((payload[0] & 0x0F) + 1 & 0x0F)])))
            if last:
                req = bytes(self._in[2:2 + (self._in_expected or 0)])
                self._in = bytearray()
                self._in_expected = None
                if req and req[0] in self.pending_once:
                    # ECU stalls: send responsePending now, the real reply follows
                    self.pending_once.discard(req[0])
                    self._respond(bytes([0x7F, req[0], 0x78]))
                self._respond(self.handler(req))
            return

    def read(self, max_msgs=16, timeout_ms=100):
        real, self.out = self.out[:max_msgs], self.out[max_msgs:]
        if not self.flood_ids:
            return real
        # model a busy bus: interleave broadcast noise ahead of our real frames,
        # but only ids the device filter admits (a hardware filter drops the rest)
        noise = [_Msg(cid, bytes(7)) for cid in self.flood_ids
                 if self.allowed_ids is None or cid in self.allowed_ids]
        return (noise + real)[:max_msgs]

    # -- ECU -> tester segmentation --
    def _respond(self, resp):
        if resp is None:
            return
        if self.inject_keepalive:                            # ECU pokes an A3 mid-stream
            self.out.append(_Msg(self.ecu_tx, bytes([0xA3])))
        body = len(resp).to_bytes(2, "big") + bytes(resp)
        frames = [body[i:i + 7] for i in range(0, len(body), 7)] or [b""]
        for i, chunk in enumerate(frames):
            last = i == len(frames) - 1
            op = 0x1 if last else 0x0                        # always ask for ACK (simple)
            self.out.append(_Msg(self.ecu_tx, bytes([(op << 4) | (self.tx_seq & 0x0F)]) + chunk))
            self.tx_seq = (self.tx_seq + 1) & 0x0F
