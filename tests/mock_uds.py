# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A scripted UDS ECU behind a UdsLink: a handler maps a request (sid+body)
to a response, and the link hands it straight through (no CAN framing, so
the UDS services can be exercised without the ISO-TP engine)."""
from typing import Callable, Dict, List, Optional


class DirectUdsLink:
    """A UdsLink look-alike: ``request(payload)`` calls the handler. Used so
    tests drive UdsClient without the ISO-TP transport."""

    def __init__(self, handler: Callable[[bytes], bytes]):
        self.handler = handler
        self.log: List[bytes] = []
        self.is_stop = None

    def request(self, payload, timeout_ms=2000, overall_ms=20000):
        self.log.append(bytes(payload))
        resp = bytes(self.handler(bytes(payload)))
        if not resp:
            # every real transport (Zf6hpUdsClient.request, BridgeIsoTp) raises
            # on silence; a suppressed positive response is silence too
            from ovtsuite.flash.transport import KLineTimeout
            raise KLineTimeout("no response")
        return resp

    def close(self):
        pass


class UdsEcu:
    """A module's UDS behaviour: DIDs, DTCs, a coding record, routines, a
    security level with a scripted seed, and io-control state. ``handler``
    is the callable a DirectUdsLink (or the ISO-TP mock) drives."""

    def __init__(self, dids: Optional[Dict[int, bytes]] = None, dtcs=(), coding: bytes = b"",
                 seed: bytes = b"", key: bytes = b"", security_level: int = 3,
                 routines: Optional[Dict[int, bytes]] = None, writable=()):
        self.dids = dict(dids or {})
        self.dtcs = list(dtcs)                         # (3-byte code, status)
        self.coding = bytes(coding)
        if coding:
            self.dids.setdefault(0x0600, self.coding)
            self.dids.setdefault(0x0601, bytes([len(self.coding)]))
        self.seed = bytes(seed)
        self.key = bytes(key)
        self.security_level = security_level
        self.unlocked = False
        self.routines = dict(routines or {})
        self.writable = set(writable) | {0x0600}
        self.io_state: Dict[int, bytes] = {}
        self.session = 0x01
        self.reset_count = 0
        self.cleared = 0
        self.requests: List[bytes] = []

    def __call__(self, req: bytes) -> bytes:
        self.requests.append(bytes(req))
        sid, body = req[0], req[1:]
        nak = lambda nrc: bytes([0x7F, sid, nrc])          # noqa: E731
        if sid == 0x10:
            self.session = body[0]
            return bytes([0x50, body[0], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x3E:
            return b"\x7E" if not (body and body[0] & 0x80) else b""
        if sid == 0x11:
            self.reset_count += 1
            return bytes([0x51, body[0] if body else 1])
        if sid == 0x22:
            did = (body[0] << 8) | body[1]
            if did not in self.dids:
                return nak(0x31)
            return bytes([0x62, body[0], body[1]]) + self.dids[did]
        if sid == 0x2E:
            did = (body[0] << 8) | body[1]
            if did not in self.writable:
                return nak(0x31)
            if did in (0x0600,) and not self.unlocked and self.security_level:
                return nak(0x33)
            self.dids[did] = bytes(body[2:])
            if did == 0x0600:
                self.coding = bytes(body[2:])
            return bytes([0x6E, body[0], body[1]])
        if sid == 0x19:
            if body and body[0] == 0x02:
                out = bytearray([0x59, 0x02, 0xFF])
                for code, st in self.dtcs:
                    if st & body[1]:
                        out += bytes(code) + bytes([st])
                return bytes(out)
            return nak(0x12)
        if sid == 0x14:
            self.dtcs = []
            self.cleared += 1
            return bytes([0x54])
        if sid == 0x27:
            if body[0] == self.security_level:
                return bytes([0x67, body[0]]) + self.seed
            if body[0] == self.security_level + 1:
                if bytes(body[1:]) == self.key:
                    self.unlocked = True
                    return bytes([0x67, body[0]])
                return nak(0x35)
            return nak(0x12)
        if sid == 0x31:
            sub, rid = body[0], (body[1] << 8) | body[2]
            if rid not in self.routines:
                return nak(0x31)
            return bytes([0x71, sub, body[1], body[2]]) + self.routines[rid]
        if sid == 0x2F:
            did = (body[0] << 8) | body[1]
            mode = body[2]
            self.io_state[did] = bytes(body[3:]) if mode == 0x03 else b""
            return bytes([0x6F, body[0], body[1], mode]) + (bytes(body[3:]) if mode == 0x03 else b"")
        return nak(0x11)


class FakeIsoIo:
    """An ISO-TP io with the in-bridge ``request`` fast path, so UdsLink's
    Zf6hpUdsClient delegates straight to the handler (no CAN framing). Lets
    the session's UDS path be driven by a scripted UdsEcu."""

    def __init__(self, handler):
        self.handler = handler
        self.log = []

    def request(self, payload, timeout_ms=2000, p2_star_ms=5000, overall_ms=20000,
                fail_fast_on_silence=True):
        self.log.append(bytes(payload))
        return bytes(self.handler(bytes(payload)))

    def request_with_keepalive(self, payload, **kw):
        return self.request(payload)

    def close(self):
        pass
