# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Debug wire-trace: CAN frames tee to a sniffer-loadable capture; K-line bytes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import tracelog as TL  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from ovtsuite.sniff import capture as C  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402


def test_can_trace_reloads_in_sniffer():
    ecu = MockTp20Ecu(lambda req: b"\x50\x89" if req == b"\x10\x89" else b"\x7e")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "trace.log")
    w = TL.open_writer(path, "unit test")
    tap = TL.FrameTap(ecu, w)
    ch = TT.Tp20Channel(tap, dest=0x01)
    ch.open()
    ch.send(b"\x10\x89")
    assert ch.recv() == b"\x50\x89"
    ch.close()
    w.close()
    recs = C.read(path)                              # loads in the sniffer's reader
    assert recs and all(r.bus == "can" for r in recs)
    # both directions were captured: our setup TX on 0x200 and an ECU frame
    assert any(r.tx and r.can_id == 0x200 for r in recs)
    assert any(r.can_id == 0x300 for r in recs)


def test_kline_trace_callback():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "k.log")
    w = TL.open_writer(path, "kline")
    cb = TL.kline_trace(w)
    cb("tx", b"\x81\x10\xf1\x3e")
    cb("rx", b"\x83\xf1\x10\x7e")
    w.close()
    recs = C.read(path)
    assert [(r.bus, r.tx, r.data.hex()) for r in recs] == [
        ("kraw", True, "8110f13e"), ("kraw", False, "83f1107e")]
