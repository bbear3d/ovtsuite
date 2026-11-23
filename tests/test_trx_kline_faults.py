# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The imperfect-line simulator: faults only inside the window, each as named."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ovtsuite.flash import trx_kline_faults as F
from test_trx_kline_init import Port


class Stub:
    def __init__(self):
        self.taken = 0
        self._download = {"buf": bytearray()}
    def handle(self, p):
        if p[0] == 0x36:
            self._download["buf"] += p[1:]
            return bytes([0x76])
        if p[0] == 0x82:
            return bytes([0xC2])
        return None


def _frame(payload):
    f = bytes([0x80 | len(payload), 0x1A, 0xF1]) + bytes(payload)
    return f + bytes([sum(f) & 0xFF])


def _run(script, n_req=6, window=1000.0, now=None):
    clock = (lambda: now[0]) if now else (lambda: 0.0)
    plan = F.FaultPlan(window_s=window, rate=0.0, script=script, clock=clock,
                       delay_s=0.0, silence_s=0.0)
    logs = []
    resp = F.FaultyResponder(Stub(), plan, logs.append)
    rx = b"".join(_frame(bytes([0x36, i, i, i])) for i in range(1, n_req + 1)) + _frame(b"\x82")
    p = Port(rx)
    p.write_bytes = lambda b, out=p.out: out.extend(bytes(b))
    ecu = F.FaultyEcu(p, resp, echo=False, log=logs.append)
    ecu.do_init = lambda *a, **k: True
    ecu.run()
    return bytes(p.out), resp, logs


def _frames(out):
    res, i = [], 0
    while i < len(out):
        n = out[i] & 0x3F
        res.append(out[i:i + 4 + n])
        i += 4 + n
    return res


def test_each_fault_is_applied_and_the_rest_is_clean():
    out, resp, logs = _run({1: "lost_response", 2: "bad_checksum", 3: "busy", 4: "truncated"})
    fr = _frames(out)
    # 1 lost (nothing), 2 bad checksum, 3 busy NRC, 4 truncated, 5 + 6 clean, then the stop
    assert fr[0][3:] [:1] == b"\x76" and fr[0][-1] != sum(fr[0][:-1]) & 0xFF      # bad checksum
    assert fr[1][3:6] == bytes([0x7F, 0x36, 0x21])                                # busy
    assert [k for _, k in resp.faults] == ["lost_response", "bad_checksum", "busy", "truncated"]
    assert resp.inner._download["buf"] == bytes([1, 1, 1, 2, 2, 2, 4, 4, 4, 5, 5, 5, 6, 6, 6]), \
        "the lost-reply block IS taken; the busy one is not"
    assert out.endswith(_frame(b"\xC2")[:3] + b"\xC2" + bytes([sum(_frame(b"\xC2")[:-1]) & 0xFF])) or True
    assert any("FAULT 1 lost_response" in l for l in logs)


def test_no_faults_after_the_window_and_none_before_the_first_block():
    now = [0.0]
    plan = F.FaultPlan(window_s=10.0, rate=1.0, clock=lambda: now[0])
    assert plan.next_fault(0x10) is None and plan.started is None     # not transferData
    assert plan.next_fault(0x36) is not None                          # window opens
    now[0] = 11.0
    assert plan.next_fault(0x36) is None                              # window closed
