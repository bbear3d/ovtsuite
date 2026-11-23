# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The VSS dialog's read-vs-actual corrector: any row, or every row."""
import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtWidgets          # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Sess:
    def __init__(self):
        self.txn = []
        self.binfile = type("B", (), {"data": bytearray(64)})()

    @contextmanager
    def transaction(self, label):
        self.txn.append(label)
        yield

    def edit_bytes(self, addr, data, coalesce=False):
        self.binfile.data[addr:addr + len(data)] = data
        return object()


def _raw(km):
    return round(3.6e9 / (km / 1000.0)).to_bytes(4, "big")


def _dialog(rows):
    from ovtsuite import gui
    d = gui.VssDialog.__new__(gui.VssDialog)
    QtWidgets.QDialog.__init__(d)
    d.session = _Sess()
    d._rows16 = rows
    d._loading = False
    d._reload = lambda: None
    d._after_write = lambda: None
    d._role_labels16 = lambda r: ["Automatic · main block", "Manual"][:len(r)]
    d.target = QtWidgets.QComboBox()
    for i in range(len(rows)):
        d.target.addItem("row %d" % i, i)
    d.target.addItem("All rows", "all")
    d.read = QtWidgets.QDoubleSpinBox()
    d.read.setRange(1, 400)
    d.actual = QtWidgets.QDoubleSpinBox()
    d.actual.setRange(1, 400)
    d.result = QtWidgets.QLabel()
    d.apply_btn = QtWidgets.QPushButton()
    for i, r in enumerate(rows):
        d.session.binfile.data[r["addr"]:r["addr"] + 4] = _raw(r["imp_km"])
    return d


ROWS = [{"addr": 0x10, "imp_km": 4000, "primary": True},
        {"addr": 0x14, "imp_km": 5000, "primary": False}]


def test_corrects_a_variant_from_its_own_count(app):
    d = _dialog([dict(r) for r in ROWS])
    d.target.setCurrentIndex(1)
    d.read.setValue(110.0)
    d.actual.setValue(100.0)
    d._recalc()
    assert d.apply_btn.isEnabled()
    assert "5500" in d.result.text() and "Manual" in d.result.text()
    d._apply()
    data = d.session.binfile.data
    assert bytes(data[0x14:0x18]) == _raw(5500)
    assert bytes(data[0x10:0x14]) == _raw(4000)        # the primary stays


def test_all_rows_scale_each_from_its_own_count_in_one_step(app):
    d = _dialog([dict(r) for r in ROWS])
    d.target.setCurrentIndex(2)
    d.read.setValue(90.0)
    d.actual.setValue(100.0)
    d._recalc()
    d._apply()
    data = d.session.binfile.data
    assert bytes(data[0x10:0x14]) == _raw(3600)
    assert bytes(data[0x14:0x18]) == _raw(4500)
    assert d.session.txn == ["VSS: read-vs-actual correction"]


def test_equal_readings_change_nothing(app):
    d = _dialog([dict(r) for r in ROWS])
    d.target.setCurrentIndex(2)
    d.read.setValue(100.0)
    d.actual.setValue(100.0)
    d._recalc()
    assert not d.apply_btn.isEnabled()
    d._apply()
    assert d.session.txn == []
