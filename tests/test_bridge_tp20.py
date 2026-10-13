# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""In-bridge TP 2.0: the whole channel runs in the bridge, one IPC per KWP
message.  Drives BridgeTp20 -> serve() -> Tp20Channel -> mock ECU over pipes."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash import j2534_bridge as B  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402


class FakeDev:
    """Stands in for j2534.PassThru inside the bridge: connect() returns a
    scripted TP 2.0 mock ECU as the CAN channel."""
    instances = []

    def __init__(self, dll):
        self.device_id = J.c_uint32(1)
        FakeDev.instances.append(self)
        self.ecu = MockTp20Ecu(self._handle, dest=0x01)

    @staticmethod
    def _handle(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req[:1] == b"\x1a":
            return b"\x5a" + req[1:] + b"03G906016GC  9970"[:16]
        if req == b"\x21\x01":
            return bytes.fromhex("6101") + bytes.fromhex("010102 040506 190a0b 250000")
        return b"\x7f" + bytes([req[0], 0x11])

    def connect(self, protocol, baud, flags=0):
        return self.ecu

    def close(self):
        pass


class PipeProc:
    def __init__(self, dll):
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.stdin = os.fdopen(w1, "wb")
        self.stdout = os.fdopen(r2, "rb")
        self.stderr = open(os.devnull, "rb")
        srv_in, srv_out = os.fdopen(r1, "rb"), os.fdopen(w2, "wb")

        def run():
            try:
                B.serve(dll, srv_in, srv_out)
            finally:
                srv_out.close()
                srv_in.close()
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()

    def wait(self, timeout=None):
        self.t.join(timeout)

    def kill(self):
        pass


def test_bridge_tp20_live_data(monkeypatch):
    monkeypatch.setattr(B.J, "PassThru", FakeDev)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    tp = dev.open_tp20(dest=0x01, baud=500000, params={"block_size": 0x0F})
    link = TT.CanKLine(tp=tp, dest=0x10)
    from ovtsuite.flash.diag import DiagSession
    sess = DiagSession(link, ecu="EDC16")
    sess.connect(speed="Low", cap_high=False)
    assert tp.tx_id == 0x740 and tp.rx_id == 0x300     # setup ran inside the bridge
    cells = sess.read_group(1)
    assert len(cells) == 3 and cells[0].a == 0x01
    sess.close()
    dev.close()


def test_bridge_tp20_frame_log(tmp_path, monkeypatch):
    monkeypatch.setattr(B.J, "PassThru", FakeDev)
    path = str(tmp_path / "bridgetrace.log")
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    tp = dev.open_tp20(dest=0x01, baud=500000, params={"block_size": 0x0F}, log_path=path)
    link = TT.CanKLine(tp=tp, dest=0x10)
    link.wake_up()                                     # channel setup, logged in-bridge
    frame = b"\x82\x10\xf1\x10\x89"
    link.write_bytes(frame + bytes([sum(frame) & 0xFF]))   # 10 89 exchange over TP
    tp.close()
    dev.close()
    from ovtsuite.sniff import capture as C
    recs = C.read(path)                                # bridge wrote a sniffer-loadable log
    assert recs and any(r.tx and r.can_id == 0x200 for r in recs)
    assert any(r.can_id == 0x300 for r in recs)
