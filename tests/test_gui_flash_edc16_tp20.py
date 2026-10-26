# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flasher's CAN link for EDC16 takes edc16.edc16_tp20_params() (pace at
the ECU's advertised T3), through the bridge and in-process, and no other ECU
does. See tests/test_tp20_edc16_timing.py for the pacing itself."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.flash import edc16 as E  # noqa: E402
from ovtsuite.flash import gui_flash  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Bridge(J.BridgePassThru):
    """A BridgePassThru that only records what open_tp20 is given."""

    def __init__(self):                    # no bridge process
        self.got = None

    def open_tp20(self, dest, baud, listen_id=0x300, params=None, log_path=None):
        self.got = dict(params or {})
        return object()


class _Local:
    """An in-process J2534 device (not the bridge)."""

    class _Ch:
        def pass_all(self):
            pass

        def clear_rx(self):
            pass

    def connect(self, protocol, baud):
        return self._Ch()


def _link(monkeypatch, ecu, dev, op="write"):
    monkeypatch.setattr(gui_flash, "open_j2534", lambda d: dev)
    w = gui_flash._Worker(op, ecu, None, "Medium", transport="can", can_dev="dummy")
    return w._open_link()


@pytest.mark.parametrize("fast", [True, False])
def test_edc16_bridge_link_params(app, monkeypatch, fast):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", fast)
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "ecu")
    dev = _Bridge()
    _link(monkeypatch, "EDC16", dev)
    assert dev.got == ({"block_size": 0x0F, "pace_ecu": True} if fast
                       else {"block_size": 0x0F})


def test_edc16_read_link_advertises_1ms(app, monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "ecu")
    dev = _Bridge()
    _link(monkeypatch, "EDC16", dev, op="read")
    assert dev.got == {"block_size": 0x0F, "pace_ecu": True, "t3_ms": 1.0}


def test_edc16_level_reaches_the_link(app, monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "fast")
    dev = _Bridge()
    _link(monkeypatch, "EDC16", dev)
    assert dev.got == {"block_size": 0x0F, "pace_ecu": True, "pace_from_start": True,
                       "backoff_on_flow": True}


def test_edc16_local_link_params(app, monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "ecu")
    link = _link(monkeypatch, "EDC16", _Local())
    assert link.tp.params.pace_ecu is True
    assert link.tp.params.t3_ms == 5.0                # the A0 we advertise: unchanged


def test_other_ecus_untouched(app, monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    dev = _Bridge()
    _link(monkeypatch, "ZF6HP", dev)
    assert dev.got == {"block_size": 0x0F, "t3_ms": 0.5, "ignore_ecu_floor": True}
    dev = _Bridge()
    _link(monkeypatch, "Cluster", dev)
    assert dev.got == {"block_size": 0x0F}
