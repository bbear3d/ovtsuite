# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table as a difference (ovtsuite.compare_delta, the model's
delta view and its menu in gui_compare_delta)."""
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
        assert "difference (primary - comparison)" in m.sep_text()

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

    def test_differing_cells_are_outlined_not_filled(model):
        """A cell that differs carries the outline role in both halves and
        every kind of cell (axis breakpoints too); the fill is left to the
        heat map and the tint, so they show through."""
        from ovtsuite import gui
        from ovtsuite.gui_cellmarks import OUTLINE_ROLE
        m = model
        P = lambda r, c: m.index(1 + r, 1 + c)                          # noqa: E731
        C = lambda r, c: m.index(m.sep_row() + 2 + r, 1 + c)            # noqa: E731
        for ix in (C(0, 1), C(1, 0), C(1, 2)):                          # 12/10, 20/25, 24/0
            assert m.data(ix, OUTLINE_ROLE) is True
        assert m.data(C(0, 0), OUTLINE_ROLE) is None                    # equal
        assert m.data(P(0, 1), OUTLINE_ROLE) is True
        assert m.data(P(0, 0), OUTLINE_ROLE) is None
        assert m.data(C(0, 1), QtCore.Qt.BackgroundRole) is None        # no orange fill
        assert m.data(C(0, 1), QtCore.Qt.ForegroundRole) is None
        m._ax_diff = {("x", 1)}
        ax = m.index(0, 2)
        assert m.data(ax, OUTLINE_ROLE) is True
        assert m.data(ax, QtCore.Qt.BackgroundRole) == gui.AXIS_BG
        # the heat map keeps its fill under the outline
        m.set_heatmap(True)
        assert m.data(C(0, 1), OUTLINE_ROLE) is True
        assert m.data(C(0, 1), QtCore.Qt.BackgroundRole) is not None
        assert m.data(C(0, 1), QtCore.Qt.BackgroundRole) != gui.DELTA_UP_BG

    def test_tint_works_on_plain_values_and_means_primary_higher(model):
        """The tint is no longer tied to a difference mode: with both tables
        on Values it fills a differing cell red where the PRIMARY is higher
        and blue where it is lower, in either half, and the heat map wins."""
        from ovtsuite import gui
        m = model
        m.set_delta("abs", tint=True)
        P = lambda r, c: m.index(1 + r, 1 + c)                          # noqa: E731
        C = lambda r, c: m.index(m.sep_row() + 2 + r, 1 + c)            # noqa: E731
        B = QtCore.Qt.BackgroundRole
        assert m.data(P(0, 1), B) == gui.DELTA_UP_BG       # 12 vs 10
        assert m.data(C(0, 1), B) == gui.DELTA_UP_BG       # the same cell, other half
        assert m.data(P(1, 0), B) == gui.DELTA_DOWN_BG     # 20 vs 25
        assert m.data(C(1, 0), B) == gui.DELTA_DOWN_BG
        assert m.data(P(0, 0), B) is None
        # the direction is the primary's, whatever difference is shown
        m.set_delta("d2", tint=True)
        assert m.data(C(0, 1), B) == gui.DELTA_UP_BG
        assert m.data(C(0, 1), QtCore.Qt.DisplayRole) == "-2.0"
        m.set_heatmap(True)
        assert m.data(C(0, 1), B) != gui.DELTA_UP_BG

    def test_this_files_table_as_a_difference_is_read_only(model):
        from ovtsuite import gui
        m = model
        D = QtCore.Qt.DisplayRole
        blocked = []
        m.edit_blocked.connect(lambda: blocked.append(1))
        cell = lambda r, c: m.data(m.index(1 + r, 1 + c), D)   # noqa: E731
        assert [cell(0, c) for c in range(3)] == ["10.0", "12.0", "14.0"]
        assert m.flags(m.index(1, 1)) & QtCore.Qt.ItemIsEditable
        m.set_delta("abs", primary="d1")
        assert [cell(0, c) for c in range(3)] == ["0", "+2.0", "0"]
        assert [cell(1, c) for c in range(3)] == ["-5.0", "0", "+24.0"]
        m.set_delta("abs", primary="pct")
        assert cell(1, 2) == cd.NONE_TEXT                 # comparison is 0
        m.set_delta("abs", blank=True, primary="d2")
        assert [cell(0, c) for c in range(3)] == ["", "-2.0", ""]
        # the comparison half is untouched, and says so on the banner
        assert _row(m, 0) == ["10.0", "10.0", "14.0"]
        assert "primary: difference" in m.sep_text()
        # no typing, no pasting, no selection arithmetic
        assert not m.flags(m.index(1, 1)) & QtCore.Qt.ItemIsEditable
        m._edit_cells([(0, 0, 99.0)])
        assert blocked and m._vals[0][0] == 10
        # tint, and a copy gives the real values
        m.set_delta("abs", tint=True, primary="d1")
        assert m.data(m.index(1, 2), QtCore.Qt.BackgroundRole) == gui.DELTA_UP_BG
        assert m.copy_tsv([m.index(1, 2)]) == "12.0"
        # back to values: editable again
        m.set_delta("abs")
        assert m.flags(m.index(1, 1)) & QtCore.Qt.ItemIsEditable
        assert cell(0, 1) == "12.0"

    def test_hex_maps_show_the_difference_as_hex(model):
        m = model
        m.entity.text_kind = "hex"
        m.entity.read_only = False
        m._vals = [[0x6C, 0x60, 0x10], [0x00, 0xFF, 0x20]]
        m._cmp = [[0x6C, 0x6A, 0x10], [0x04, 0xF0, 0x20]]
        assert _row(m, 0) == ["6C", "6A", "10"]
        m.set_delta("d1")
        assert _row(m, 0) == ["00", "-0A", "00"]
        assert _row(m, 1) == ["-04", "+0F", "00"]
        m.set_delta("d2", blank=True)
        assert _row(m, 0) == ["", "+0A", ""]
        m.set_delta("pct")
        assert _row(m, 1)[0] == "-100.0%"
        tip = m.data(m.index(m.sep_row() + 2, 2), QtCore.Qt.ToolTipRole)
        assert "60" in tip and "6A" in tip
        # this file's own table, as a difference: signed hex, read only
        m.set_delta("abs", primary="d1")
        D = QtCore.Qt.DisplayRole
        assert [m.data(m.index(1, c), D) for c in (1, 2, 3)] == ["00", "-0A", "00"]
        assert [m.data(m.index(2, c), D) for c in (1, 2, 3)] == ["-04", "+0F", "00"]
        assert not m.flags(m.index(1, 1)) & QtCore.Qt.ItemIsEditable

    def test_ascii_maps_stay_absolute(model):
        m = model
        m.entity.text_kind = "ascii"
        m._vals = [[65, 66, 67], [68, 69, 70]]
        m._cmp = [[65, 67, 67], [68, 69, 71]]
        m.set_delta("d1")
        assert _row(m, 0) == ["A", "C", "C"]

    def test_the_menu_names_the_files_and_sets_what_and_where(
            model, isolated_qsettings_module):
        from types import SimpleNamespace as NS
        from ovtsuite import gui_compare_delta as gcd, options
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        assert app
        ws = NS(model=model,
                primary=NS(binfile=NS(name="tuned.bin")),
                comparison=NS(binfile=NS(name="stock.bin")))
        menu = gcd.fill_menu(QtWidgets.QMenu(), lambda: ws)
        menu.aboutToShow.emit()
        texts = [a.text() for a in menu.actions()]
        assert "Primary: tuned.bin" in texts and "Comparison: stock.bin" in texts
        sections = [a.text() for a in menu.actions() if a.isSeparator()]
        assert sections[:2] == ["Show as", "Show it in"]
        acts = {a.text(): a for a in menu.actions() if a.text()}
        where = {w: acts[t] for w, t in gcd.WHERE}
        try:
            assert not any(a.isEnabled() for a in where.values())   # off: nothing to place
            acts["Difference: primary - comparison"].trigger()
            assert options.get("ui/compare_delta") == "d1"
            menu.aboutToShow.emit()
            assert all(a.isEnabled() for a in where.values())
            assert where["cmp"].isChecked() and gcd.mode() == "d1"
            assert gcd.primary_mode() == "abs"                  # the default: comparison
            where["both"].trigger()
            assert gcd.mode() == "d1" and gcd.primary_mode() == "d1"
            where["primary"].trigger()
            assert gcd.mode() == "abs" and gcd.primary_mode() == "d1"
            acts["Tint by direction"].trigger()
            acts["Blank the cells that do not differ"].trigger()
            assert options.get("ui/compare_delta_tint") is True
            assert options.get("ui/compare_delta_blank") is True
            menu.aboutToShow.emit()                 # reads the options back
            assert where["primary"].isChecked() and gcd.active()
            acts["Values"].trigger()
            assert not gcd.active()
        finally:
            for k in gcd.KEYS:
                options.set(k, options.BY_KEY[k].default)

    def test_the_edit_bar_button_follows_the_options_and_the_comparison(
            model, isolated_qsettings_module):
        from types import SimpleNamespace as NS
        from ovtsuite import gui_compare_delta as gcd, options
        ws = NS(model=model)
        btn = gcd.make_button(ws)
        try:
            assert btn.text() == "Difference view" and btn.isEnabled()
            options.set("ui/compare_delta", "pct")
            options.set("ui/compare_delta_in", "primary")
            gcd.apply_all(type("W", (), {"workspaces": lambda self: [ws]})())
            assert ws.model.primary_delta() == "pct"
            assert ws.model.delta_mode() == "abs"
            assert btn.text() == "Difference view (on)"
            assert "primary table" in btn.toolTip()
            ws.model._cmp = []                  # no comparison loaded
            gcd.refresh_button(ws)
            assert not btn.isEnabled() and btn.text() == "Difference view"
        finally:
            for k in gcd.KEYS:
                options.set(k, options.BY_KEY[k].default)
