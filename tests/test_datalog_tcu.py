# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Logging a ZF 6HP TCU over CAN/TP2.0.

Two things these pin, both of which were broken or absent before:

* ``DiagSession`` can open a session on the TCU at all -- it used to run the
  EDC16 path unconditionally, which does a K-line slow init the TCU has no
  answer to and asks for session 0x89 instead of 0x86.
* The connection is made to the TCU's own logical address. The dialog never
  passed ``can_dest``, so every CAN log session opened TP2.0 to 0x01 (the
  engine ECU) whatever was selected -- a defect that also affected EDC16
  logging, whose CAN path had no test at all.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import diag as _diag                      # noqa: E402
from ovtsuite.flash import j2534 as _j2534                    # noqa: E402
from ovtsuite.flash import zf6hp as Z                         # noqa: E402
from ovtsuite.flash.tp20_transport import CanKLine, Tp20Channel  # noqa: E402
from mock_tp20 import MockTp20Ecu                             # noqa: E402
from ovtsuite import gui as _g                                # noqa: E402


#: What the mock TCU answers, keyed by the exact request bytes.
REPLIES = {
    b"\x10\x86": b"\x50\x86",
    b"\x1a\x9b": b"\x5a\x9b" + b"4F9910156A  " + b"\x00" * 4,
    b"\x21\x01": b"\x61\x01\x12\x34\x56",
}


def _link(dest=Z.DEST, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(bytes(req))
        return REPLIES.get(bytes(req).lower())

    ecu = MockTp20Ecu(handler, dest=dest, ecu_tx=0x300 + dest, ecu_rx=0x740)
    tp = Tp20Channel(ecu, dest=dest, listen_id=Z.LISTEN_ID)
    tp.open()
    return CanKLine(tp=tp, dest=dest), ecu


def test_tcu_session_uses_86_and_no_kline_init():
    seen = []
    link, _ecu = _link(seen=seen)
    s = _diag.DiagSession(link, "ZF6HP")
    s.connect()
    assert seen[0] == b"\x10\x86", "TCU wants session 0x86, not the EDC16 0x89"
    assert s.baud == 0, "no K-line baud applies over TP2.0"
    assert "4F9910156A" in s.identity


def test_probe_groups_skips_what_the_ecu_refuses():
    link, _ecu = _link()
    s = _diag.DiagSession(link, "ZF6HP")
    s.connect()
    got = s.probe_groups(first=1, last=3)
    assert [g for g, _ in got] == [1], "only the group the ECU answers comes back"


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_worker_carries_the_selected_can_dest():
    """The regression that mattered: dest must reach the transport."""
    from ovtsuite.datalog.gui_log import LogWorker, CAN_ECUS
    assert "ZF6HP" in CAN_ECUS
    w = LogWorker("dev", "ZF6HP", "Low", False, transport="can", can_dest=Z.DEST)
    assert w._can_dest == Z.DEST


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_can_dest_table_is_shared_with_the_flash_side():
    from ovtsuite.flash.gui_flash import _CAN_DEST
    assert _CAN_DEST["ZF6HP"] == Z.DEST
    assert _CAN_DEST["EDC16"] != _CAN_DEST["ZF6HP"]


# ---------------------------------------------------------------------------
# Opening the TP 2.0 channel.
#
# The helper above calls ``tp.open()`` itself, which is exactly why every test
# here passed while the logging dialog could not connect at all: the dialog
# builds a CanKLine straight from ``open_tp20()`` / ``connect()`` and never
# opens it, because ``_connect_tcu`` documented the channel as "already open
# by the time this runs". It is not, on either backend:
#
#   * 32-bit bridge -- BridgeTp20 is a lazy handle, so the bridge's own TP
#     state was still None and it answered "'NoneType' object has no
#     attribute 'send'" ("Could not connect: ..." in the dialog);
#   * direct DLL / OpenPort -- tx_id was never negotiated, so the first frame
#     was written to CAN id None.
#
# These three pin the fix at the layer that has to hold for both.


def _unopened_link(dest=Z.DEST, seen=None):
    """A link built the way the logging dialog builds one: never opened."""
    def handler(req):
        if seen is not None:
            seen.append(bytes(req))
        return REPLIES.get(bytes(req).lower())

    ecu = MockTp20Ecu(handler, dest=dest, ecu_tx=0x300 + dest, ecu_rx=0x740)
    tp = Tp20Channel(ecu, dest=dest, listen_id=Z.LISTEN_ID)
    return CanKLine(tp=tp, dest=dest), ecu


def test_connect_opens_a_channel_nobody_opened():
    seen = []
    link, _ecu = _unopened_link(seen=seen)
    assert not link.tp.opened, "precondition: the dialog's link starts closed"
    s = _diag.DiagSession(link, "ZF6HP")
    s.connect()
    assert link.tp.opened, "connect() must negotiate the channel"
    assert link.tp.tx_id is not None, "a frame to CAN id None goes nowhere"
    assert seen[0] == b"\x10\x86"
    assert "4F9910156A" in s.identity


class _RecordingBridge:
    """Stands in for BridgePassThru: records the ops a BridgeTp20 sends."""

    def __init__(self):
        self.ops = []

    def _call(self, op, **kw):
        self.ops.append(op)
        if op == "tp_open":
            return {"tx_id": 0x740, "rx_id": 0x300,
                    "ecu_block_size": 0x0F, "ecu_interframe_ms": 0.0}
        if op == "tp_recv":
            raise _j2534.J2534Error("timed out")      # end the probe here
        return None


def test_bridge_opens_before_it_sends():
    """The bridge answers tp_send with AttributeError when tp_open never ran."""
    b = _RecordingBridge()
    tp = _j2534.BridgeTp20(b, dest=Z.DEST, baud=500000)
    link = CanKLine(tp=tp, dest=Z.DEST)
    with pytest.raises(Exception):
        _diag.DiagSession(link, "ZF6HP").connect()    # tp_recv ends it
    assert "tp_send" in b.ops
    assert b.ops.index("tp_open") < b.ops.index("tp_send")


def test_an_open_channel_is_left_alone():
    """The flash path opens the channel itself; do not re-negotiate under it."""
    b = _RecordingBridge()
    tp = _j2534.BridgeTp20(b, dest=Z.DEST, baud=500000)
    tp.opened, tp.tx_id = True, 0x740                 # as flash/zf6hp.py leaves it
    link = CanKLine(tp=tp, dest=Z.DEST)
    with pytest.raises(Exception):
        _diag.DiagSession(link, "ZF6HP").connect()
    assert "tp_open" not in b.ops, "an already-open channel must not be reset"
