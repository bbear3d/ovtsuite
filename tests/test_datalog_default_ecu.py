# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's ECU box defaults to whatever calibration the tab holds.

Open the logger from a TCU tab and you mean the TCU. Before this, the ECU box
came from QSettings -- whatever was logged last -- and `restore_settings`
deliberately never restores ZF6HP, so the TCU could only ever be chosen by hand.
There was an EDC15 default, but it was applied BEFORE restore_settings and so
was silently undone by it; that is the bug these pin.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite import gui as _g                                   # noqa: E402
from ovtsuite.flash.diag import decode_cell                      # noqa: E402
from ovtsuite.datalog import logfile as _lf                       # noqa: E402

_KEEP = []


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _bar():
    from PySide6 import QtWidgets
    from ovtsuite.datalog.gui_log import ConnectionBar
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent = QtWidgets.QWidget()
    bar = ConnectionBar(types.SimpleNamespace(primary=None), parent)
    _KEEP.extend((parent, bar))
    return bar


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
@pytest.mark.parametrize("platform,want", [("zf6hp", "ZF6HP"),
                                           ("edc15", "EDC15"),
                                           ("edc16", "EDC16")])
def test_the_box_follows_the_open_calibration(platform, want):
    bar = _bar()
    bar.ecu_box.setCurrentIndex(bar.ecu_box.findText("EDC16"))
    bar.default_from_platform(types.SimpleNamespace(platform=platform))
    assert bar.ecu_box.currentText() == want


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_a_snapshot_without_platform_still_gets_the_edc15_default():
    """Legacy fallback: restored snapshots predate the platform attribute."""
    bar = _bar()
    bar.ecu_box.setCurrentIndex(bar.ecu_box.findText("EDC16"))
    assert bar.default_from_platform(types.SimpleNamespace(is_edc15=True))
    assert bar.ecu_box.currentText() == "EDC15"


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_an_unknown_platform_changes_nothing():
    bar = _bar()
    bar.ecu_box.setCurrentIndex(bar.ecu_box.findText("EDC16"))
    assert not bar.default_from_platform(types.SimpleNamespace(platform="hvac"))
    assert not bar.default_from_platform(None)
    assert bar.ecu_box.currentText() == "EDC16"


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_the_tab_beats_the_remembered_setting():
    """The ordering bug itself: this must run after restore_settings, not before."""
    from PySide6 import QtCore, QtWidgets
    from ovtsuite import branding
    from ovtsuite.datalog.gui_log import ConnectionBar
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.beginGroup("conn")
    s.setValue("ecu", "EDC16")                      # "last time I logged the engine"
    s.endGroup()
    s.sync()
    parent = QtWidgets.QWidget()
    bar = ConnectionBar(types.SimpleNamespace(primary=types.SimpleNamespace(platform="zf6hp")),
                        parent)
    _KEEP.extend((parent, bar))
    assert bar.ecu_box.currentText() == "ZF6HP", \
        "the open TCU calibration must win over the remembered EDC16"


# ---- what made the torque log readable at all ----------------------------
def test_formula_177_is_offset_binary_nm():
    """16-bit across both operand bytes, centred on 0x8000, value in Nm.

    Derived from a 3776-sample GS19 log: a only ever took 127/128/129 while b
    ran 0..255, which is one 16-bit quantity rather than two operands. Read this
    way it correlates with pedal at r=+0.970 and spans -51..+300 Nm; read as
    separate operands, b alone correlates at -0.051. Confirmed structurally by
    KF_MEHAUPT*'s input-torque axis, which is Nm over -78..+300.
    """
    assert decode_cell(177, 128, 0)[:2] == (0.0, "Nm")
    assert decode_cell(177, 127, 205)[0] == -51.0
    assert decode_cell(177, 129, 44)[0] == 300.0
    assert decode_cell(177, 128, 255)[0] == 255.0
    assert decode_cell(177, 128, 0)[2] == "0 Nm", "integer Nm, not one decimal"


def test_the_reader_recovers_a_raw_triplet():
    """A log written before the formula existed must become readable, not stay
    lost -- the numbers were always there, only the interpretation was missing."""
    assert _lf.split_value_unit("(177 129 44)") == (300.0, "Nm")
    assert _lf.split_value_unit("832 rpm") == (832.0, "rpm")
    assert _lf.split_value_unit("(99 1 2)") == (None, ""), "still unknown -> no value"
    assert _lf.split_value_unit("") == (None, "")


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_each_controller_brings_back_its_own_transport():
    """Per-module logger settings: the engine left on K-line and the gearbox
    on CAN each come back on their own transport (a ZF6HP is not even in the
    K-line list, so picking it from the TCU's file must switch to CAN)."""
    from PySide6 import QtCore
    from ovtsuite import branding
    from ovtsuite.datalog.gui_log import ConnectionBar
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings(*branding.QSETTINGS).remove("conn/permod")
    parent = QtWidgets.QWidget()
    bar = ConnectionBar(types.SimpleNamespace(primary=None), parent, scope="permod")
    _KEEP.extend((parent, bar))
    bar._ready = True
    assert bar.select_module("EDC16")
    bar.transport_box.setCurrentIndex(bar.transport_box.findData("kline"))
    bar.save_settings()
    assert bar.select_module("ZF6HP")
    assert bar.transport_kind() == "can" and bar.ecu() == "ZF6HP"
    k = bar.can_rate.findData(125000)
    bar.can_rate.setCurrentIndex(k)
    bar.save_settings()
    assert bar.select_module("EDC16")
    assert bar.transport_kind() == "kline"
    assert bar.select_module("ZF6HP")
    assert bar.can_rate.currentData() == 125000
    QtCore.QSettings(*branding.QSETTINGS).remove("conn/permod")
