# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table as a difference (ovtsuite.compare_delta, the model's
delta view and the banner / toolbar controls in gui_compare_delta)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ovtsuite import compare_delta as cd  # noqa: E402


def test_delta_arithmetic_and_text():
    assert cd.delta("d1", 12, 10) == 2 and cd.delta("d2", 12, 10) == -2
    assert cd.delta("pct", 12, 10) == pytest.approx(20.0)
    assert cd.delta("pct", 5, -10) == pytest.approx(150.0)
    assert cd.delta("ratio", 12, 10) == pytest.approx(1.2)
    assert cd.delta("pct", 3, 0) is None and cd.delta("ratio", 3, 0) is None
    assert cd.text("d1", 2.0, True, 1) == "+2.0"
    assert cd.text("d1", -2.0, True, 0) == "-2"
    assert cd.text("pct", 20.0, True) == "+20.0%"
    assert cd.text("ratio", 1.2, True) == "1.200"
    assert cd.text("pct", None, True) == cd.NONE_TEXT
    assert cd.text("d1", 0.0, False) == "0"
    assert cd.text("pct", 0.0, False) == "0%"
    assert cd.text("ratio", 1.0, False) == "1"
    assert cd.text("d1", 0.0, False, blank=True) == ""
    assert (cd.sign("d1", 2), cd.sign("d1", -2), cd.sign("ratio", 1.2),
            cd.sign("ratio", .8), cd.sign("pct", None)) == (1, -1, 1, -1, 0)
    assert cd.valid("nonsense") == "abs"


try:
    from PySide6 import QtCore, QtWidgets
except ImportError:                                     # no Qt here
    QtCore = None

if QtCore is not None:
    @pytest.fixture
    def model():
        from types import SimpleNamespace as NS
        from ovtsuite import gui
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        assert app
        m = gui.TableModel(None)
        m.entity = NS(rows=2, cols=3, text_kind="", options=None, name="t")
        m._vals = [[10, 12, 14], [20, 22, 24]]
        m._cmp = [[10, 10, 14], [25, 22, 0]]
        m._x, m._y, m._cx, m._cy = [1, 2, 3], [1, 2], [1, 2, 3], [1, 2]
        m._roff = m._coff = 1
        m._ax_diff = set()
        m.zmode = "raw"
        m._dec = m._cmp_dec = 1
        return m

    def _row(m, r):
        D = QtCore.Qt.DisplayRole
        r += m.sep_row() + 2            # banner, the comparison's band
        return [m.data(m.index(r, c), D) for c in range(1, 4)]

    def test_model_shows_the_difference(model):
        m = model
        assert _row(m, 0) == ["10.0", "10.0", "14.0"]
        m.set_delta("d1")
        assert _row(m, 0) == ["0", "+2.0", "0"]
        assert _row(m, 1) == ["-5.0", "0", "+24.0"]
        m.set_delta("d2")
        assert _row(m, 1) == ["+5.0", "0", "-24.0"]
        m.set_delta("pct")
        assert _row(m, 0) == ["0%", "+20.0%", "0%"]
        assert _row(m, 1) == ["-20.0%", "0%", cd.NONE_TEXT]  # comparison 0
        m.set_delta("ratio")
        assert _row(m, 0) == ["1", "1.200", "1"]
        m.set_delta("d1", blank=True)
        assert _row(m, 0) == ["", "+2.0", ""]
        assert "difference (this file - comparison)" in m.sep_text()

    def test_tint_is_opaque_with_dark_text_and_copy_is_absolute(model):
        from ovtsuite import gui
        m = model
        m.set_delta("d1", tint=True)
        up = m.index(m.sep_row() + 2, 2)        # 12 vs 10: this file higher
        down = m.index(m.sep_row() + 3, 1)      # 20 vs 25: lower
        for ix, col in ((up, gui.DELTA_UP_BG), (down, gui.DELTA_DOWN_BG)):
            assert m.data(ix, QtCore.Qt.BackgroundRole) == col
            assert col.alpha() == 255
            assert m.data(ix, QtCore.Qt.ForegroundRole) == gui.CELL_DARK_FG
        # a cell that does not differ keeps no tint
        same = m.index(m.sep_row() + 2, 1)
        assert m.data(same, QtCore.Qt.BackgroundRole) is None
        # the real value is what copies, and the table shows deltas again after
        assert m.copy_tsv([up]) == "10.0"
        assert m.data(up, QtCore.Qt.DisplayRole) == "+2.0"
        # the tooltip keeps both absolute values
        tip = m.data(up, QtCore.Qt.ToolTipRole)
        assert "12.0" in tip and "10.0" in tip

    def test_text_maps_stay_absolute(model):
        m = model
        m.entity.text_kind = "hex"
        m.set_delta("d1")
        assert _row(m, 0) == ["0A", "0A", "0E"]

    @pytest.fixture
    def grid(model, isolated_qsettings_module):
        from types import SimpleNamespace as NS
        from ovtsuite import gui_cmpscroll as GC
        t = QtWidgets.QTableView()
        t.setModel(model)
        ws = NS(table=t, model=model, primary=None)
        g = GC.CompareGrid(ws)
        ws.cmp_grid = g
        host = QtWidgets.QMainWindow()
        host.setCentralWidget(g.split)
        host.resize(900, 500)
        host.show()
        model.modelReset.emit()
        QtWidgets.QApplication.processEvents()
        yield g, ws
        from ovtsuite import options
        for k in ("ui/compare_layout", "ui/compare_delta",
                  "ui/compare_delta_control"):
            options.set(k, options.BY_KEY[k].default)
        host.close()

    def _pump():
        for _ in range(3):
            QtWidgets.QApplication.processEvents()

    def test_banner_control_follows_the_layout_and_the_option(grid):
        from ovtsuite import gui_compare_delta as gcd, options
        g, ws = grid
        ctl = g.delta_ctl
        # same grid: on the table's banner row
        assert ctl.wanted() and ctl.cb.isVisible()
        assert ctl.cb.parent() is ws.table.viewport()
        # own grid: on the grid's heading
        options.set("ui/compare_layout", "below")
        g.set_mode("below")
        _pump()
        assert g.active() and ctl.cb.parent() is g.cmp_head
        assert ctl.cb.isVisible()
        # picking a mode sets the option, and the model follows
        i = ctl.cb.findData("pct")
        ctl.cb.setCurrentIndex(i)
        ctl.cb.activated.emit(i)
        assert options.get("ui/compare_delta") == "pct"
        gcd.apply_all(type("W", (), {"workspaces": lambda self: [ws]})())
        _pump()
        assert ws.model.delta_mode() == "pct"
        assert "percent change" in g.cmp_head.text()
        # not offered on the banner
        options.set("ui/compare_delta_control", "toolbar")
        gcd.apply_all(type("W", (), {"workspaces": lambda self: [ws]})())
        assert not ctl.cb.isVisible()
