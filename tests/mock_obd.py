# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scripted OBD-II emissions modules: as ISO 15765-4 responders on a CAN
channel object, and as an ISO 9141-2 module behind the K-line facade."""
from typing import Dict, List, Optional

from ovtsuite.flash import j2534 as J
from ovtsuite.flash.transport import KLineTimeout


class _Msg:
    def __init__(self, can_id, payload):
        self.can_id = can_id
        self.payload = bytes(payload)
        self.protocol = J.CAN
        self.rx_status = 0
        self.tx_flags = 0
        self.timestamp = 0


class ObdModule:
    """One emissions module: supported PIDs, live values, codes, info."""

    def __init__(self, pids: Dict[int, bytes], dtcs: Optional[List[bytes]] = None, pending=(), permanent=(),
                 vin: str = "", cal_ids=(), ecu_name: str = "", mil=False, compression=False):
        self.pids = dict(pids)
        self.dtcs = list(dtcs or [])
        self.pending = list(pending)
        self.permanent = list(permanent)
        self.vin = vin
        self.cal_ids = list(cal_ids)
        self.ecu_name = ecu_name
        self.mil = mil
        self.compression = compression
        self.cleared = 0
        self.requests = []

    def _bitmap(self, mode, base):
        keys = self.pids if mode in (0x01, 0x02) else {}
        bits = 0
        for pid in keys:
            if base < pid <= base + 0x20:
                bits |= 1 << (31 - (pid - base - 1))
        if any(p > base + 0x20 for p in keys):
            bits |= 1
        return bits.to_bytes(4, "big")

    def answer(self, req: bytes, can: bool) -> Optional[bytes]:
        self.requests.append(bytes(req))
        mode = req[0]
        if mode in (0x01, 0x02):
            pid = req[1]
            if pid & 0x1F == 0:
                return bytes([0x40 + mode, pid]) + (bytes([req[2]]) if mode == 0x02 else b"") + self._bitmap(mode, pid)
            if pid == 0x01 and mode == 0x01:
                a = (0x80 if self.mil else 0) | len(self.dtcs)
                b = 0x07 | (0x08 if self.compression else 0) | 0x10          # misfire incomplete
                return bytes([0x41, 0x01, a, b, 0x2D, 0x24])
            if pid not in self.pids:
                return None
            if mode == 0x02:
                frame = req[2]
                if pid == 0x02:
                    return bytes([0x42, 0x02, frame]) + (self.dtcs[0] if self.dtcs else b"\x00\x00")
                return bytes([0x42, pid, frame]) + self.pids[pid]
            return bytes([0x41, pid]) + self.pids[pid]
        if mode in (0x03, 0x07, 0x0A):
            codes = {0x03: self.dtcs, 0x07: self.pending, 0x0A: self.permanent}[mode]
            if can:
                return bytes([0x40 + mode, len(codes)]) + b"".join(codes)
            body = b"".join(codes[:3]).ljust(6, b"\x00")
            return bytes([0x40 + mode]) + body
        if mode == 0x04:
            self.dtcs, self.pending = [], []
            self.cleared += 1
            return b"\x44"
        if mode == 0x09:
            t = req[1]
            if t == 0x02 and self.vin:
                if can:
                    return b"\x49\x02\x01" + self.vin.encode()
                # K-line: one message per 4-byte chunk, each with a sequence
                # byte; the first chunk is padded in front (SAE J1979: 17
                # characters in 5 x 4 bytes, three 0x00 first)
                v = self.vin.encode()
                padded = b"\x00" * (-len(v) % 4) + v
                return [b"\x49\x02" + bytes([i + 1]) + padded[i * 4:(i + 1) * 4]
                        for i in range(len(padded) // 4)]
            if t == 0x04 and self.cal_ids:
                data = b"".join(c.encode().ljust(16, b"\0") for c in self.cal_ids)
                if can:
                    return b"\x49\x04" + bytes([len(self.cal_ids)]) + data
                return [b"\x49\x04" + bytes([i + 1]) + data[i * 4:(i + 1) * 4] for i in range(len(data) // 4)]
            if t == 0x0A and self.ecu_name:
                data = self.ecu_name.encode().ljust(20, b"\0")
                if can:
                    return b"\x49\x0A\x01" + data
                return [b"\x49\x0A" + bytes([i + 1]) + data[i * 4:(i + 1) * 4] for i in range(len(data) // 4)]
            return b"\x7F\x09\x12"
        return b"\x7F" + bytes([mode, 0x11])


class MockObdCan:
    """Several ObdModules answering functional requests on 0x7DF, each on
    its own 0x7E8+n id, multi-frame when the answer needs it (flow control
    expected on 0x7E0+n)."""

    def __init__(self, modules: Dict[int, ObdModule]):
        self.modules = modules              # response id -> module
        self.out: List[_Msg] = []
        self._cf = {}                       # response id -> remaining consecutive frames
        self.log = []

    def pass_all(self, extended=False):
        pass

    def pass_ids(self, ids, extended=False):
        pass

    def clear_rx(self):
        self.out = []

    def set_config(self, **kw):
        pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        payload = bytes(payload)
        self.log.append((can_id, payload))
        pci = payload[0] >> 4
        if can_id == 0x7DF and pci == 0:
            n = payload[0] & 0x0F
            req = payload[1:1 + n]
            for rid, mod in sorted(self.modules.items()):
                resp = mod.answer(req, can=True)
                if resp is None:
                    continue
                if len(resp) <= 7:
                    self.out.append(_Msg(rid, bytes([len(resp)]) + resp + b"\x55" * (7 - len(resp))))
                else:
                    self.out.append(_Msg(rid, bytes([0x10 | (len(resp) >> 8), len(resp) & 0xFF]) + resp[:6]))
                    rest = resp[6:]
                    self._cf[rid] = [bytes([0x20 | ((i + 1) & 0x0F)]) + rest[i * 7:(i + 1) * 7].ljust(7, b"\x55")
                                     for i in range((len(rest) + 6) // 7)]
            return
        if pci == 3:                        # flow control to a request id -> release that module's CFs
            rid = can_id + 8
            for f in self._cf.pop(rid, []):
                self.out.append(_Msg(rid, f))

    def read(self, max_msgs=16, timeout_ms=100):
        real, self.out = self.out[:max_msgs], self.out[max_msgs:]
        return real


class MockObdKLine:
    """An ISO 9141-2 module (key bytes 08 08) behind the K-line facade."""

    def __init__(self, module: ObdModule, src=0x10, version=1032):
        self.module = module
        self.src = src
        self.version = version
        self.read_timeout = 1.0
        self.prefer_fast_init = False
        self.rx = bytearray()
        self.woken = []
        self.frames = []

    def set_baud(self, baud):
        self.baud = baud

    def wake_up(self, address, even_parity=False, tries=3):
        self.woken.append(address)
        self.rx += b"\xCC"                  # the inverted address the ECU sends after our ~KB2
        return self.version

    def clear_rx(self):
        self.rx = bytearray()

    def write_bytes(self, data):
        data = bytes(data)
        self.frames.append(data)
        assert data[:3] == b"\x68\x6A\xF1" and (sum(data[:-1]) & 0xFF) == data[-1]
        resp = self.module.answer(data[3:-1], can=False)
        if resp is None:
            return
        for one in (resp if isinstance(resp, list) else [resp]):
            msg = bytearray([0x48, 0x6B, self.src]) + one
            msg.append(sum(msg) & 0xFF)
            self.rx += msg

    def read_byte(self):
        if not self.rx:
            raise KLineTimeout("no data")
        return self.rx.pop(0)

    def read(self, n):
        if len(self.rx) < n:
            raise KLineTimeout("short read")
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def close(self):
        pass
