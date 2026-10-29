# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The End of injection window's table conventions, right-click menu, paste
and comparison grid (ovtsuite/gui_eoi.py, gui_eoi_compare.py) on a synthetic
injection model -- no reference binary, no EDC15 / EDC16 module: a 3 x 3
start-of-injection map, one duration table and a selector curve, behind the
same Table / Calculator / Plan code the real definitions go through."""
import os
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import eoi as _eoi  # noqa: E402
from ovtsuite.gui_cellmarks import CellDelegate, OUTLINE_ROLE  # noqa: E402

pytestmark = pytest.mark.slow

RPM = [1000.0, 2000.0, 3000.0]
Q = [10.0, 20.0, 30.0]


class Ent:
    """The slice of a definition entity the EOI code reads and writes."""

    def __init__(self, name, kind, xs, ys, vals, xu="rpm", yu="mg/stroke"):
        self.name, self.kind, self.group = name, kind, "G"
        self.x = NS(units=xu)
        self.y = NS(units=yu) if ys else None
        self._xs, self._ys, self.vals = xs, ys, [list(r) for r in vals]
        self.value_range = (-20.0, 40.0)

    rows = property(lambda self: len(self.vals))
    cols = property(lambda self: len(self.vals[0]))
    x_values = lambda self: list(self._xs)                    # noqa: E731
    y_values = lambda self: list(self._ys)                    # noqa: E731
    values = lambda self: [list(r) for r in self.vals]        # noqa: E731


class Session:
    def __init__(self, soi, dur, name):
        self.soi = Ent("SOI_MAP", "map", RPM, Q, soi)
        self.dur = Ent("DUR_MAP0", "map", RPM, Q, dur)
        sel = Ent("DUR_SEL", "curve", [-50.0, 50.0], [], [[0.0, 0.0]],
                  xu="deg", yu="")
        self.model = _eoi.InjectionModel(
            platform="edc16", group="G",
            soi=_eoi.TableSet([self.soi]),
            duration=_eoi.TableSet([self.dur], sel))
        self.platform = "edc16"
        self.binfile = NS(name=name)
        self.undo_steps = 0

    def transaction(self, label):
        import contextlib

        @contextlib.contextmanager
        def tx():
            self.undo_steps += 1
            yield
        return tx()

    def edit_cells(self, e, rows):
        for r, c, v in rows:
            e.vals[r][c] = v

    def variant_entities(self):
        return []


def _grid(base, bump=()):
    out = [[base + r + c for c in range(3)] for r in range(3)]
    for r, c, d in bump:
        out[r][c] += d
    return out


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def dlg(app, monkeypatch):
    from ovtsuite import gui_eoi
    prim = Session(_grid(8.0), _grid(12.0), "tuned.bin")
    comp = Session(_grid(8.0, [(0, 1, -2.0), (2, 2, 3.0)]), _grid(12.0), "stock.bin")
    monkeypatch.setattr(_eoi, "models", lambda s: [s.model])
    ws = QtWidgets.QWidget()
    ws.primary, ws.comparison, ws.view_only = prim, comp, None
    d = gui_eoi.EoiDialog(ws, parent=ws)
    d.show()
    app.processEvents()
    yield d
    d.close()
    ws.deleteLater()


def _texts(g):
    return [[g.item(r, c).text() for c in range(g.columnCount())]
            for r in range(g.rowCount())]


def test_grid_follows_the_app_table_conventions(dlg):
    for g in (dlg.grid, dlg.cmpv.grid):
        assert g.alternatingRowColors()
        assert isinstance(g.itemDelegate(), CellDelegate)
    # the explicit shading the view is designed around is still there
    assert dlg.grid.item(1, 1).background().style() != QtCore.Qt.BrushStyle.NoBrush


def test_comparison_grid_is_below_with_the_same_rows_and_outlines_the_differences(dlg):
    cv = dlg.cmpv
    assert cv.box.isVisibleTo(dlg) and dlg.prim_head.isVisibleTo(dlg)
    assert "tuned.bin" in dlg.prim_head.text() and "stock.bin" in cv.head.text()
    assert dlg.split.orientation() == QtCore.Qt.Orientation.Vertical
    assert dlg.split.widget(1) is cv.box                      # under the primary
    assert (cv.grid.rowCount(), cv.grid.columnCount()) == (3, 3)
    assert dlg.mode() == "eoi"
    # EOI = duration - SOI: primary 4 everywhere on the diagonal bands; the
    # comparison's SOI differs at (0, 1) and (2, 2)
    diff = {(r, c) for r in range(3) for c in range(3)
            if dlg.grid.item(r, c).text() != cv.grid.item(r, c).text()}
    assert diff == {(0, 1), (2, 2)}
    for r in range(3):
        for c in range(3):
            for g in (dlg.grid, cv.grid):
                assert (g.item(r, c).data(OUTLINE_ROLE) is True) == ((r, c) in diff)
                # the outline never replaces the band
                assert g.item(r, c).background().style() != QtCore.Qt.BrushStyle.NoBrush


def test_the_two_grids_select_together(dlg, app):
    dlg.grid.clearSelection()
    sm = dlg.grid.selectionModel()
    sel = QtCore.QItemSelection(dlg.grid.model().index(0, 0), dlg.grid.model().index(1, 2))
    sm.select(sel, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    app.processEvents()
    assert dlg._selected_cells(dlg.cmpv.grid) == dlg._selected_cells(dlg.grid)
    assert len(dlg._selected_cells(dlg.cmpv.grid)) == 6


def test_window_fits_a_laptop_screen(dlg):
    scr = dlg.screen().availableGeometry()
    assert dlg.height() <= scr.height() - 60 and dlg.width() <= scr.width() - 40
    assert dlg.height() >= 480


@pytest.mark.parametrize("w,h", [(1366, 728), (1536, 824), (1920, 1040)],
                         ids=["1366x768", "1536x864", "1920x1080"])
def test_the_window_with_the_comparison_is_not_taller_than_the_screen_allows(
        app, monkeypatch, w, h):
    """On a laptop screen the usable height is about 728 px; with the
    comparison shown the window takes at most 82 % of what a screen has, on
    any screen, and never less than it needs."""
    from ovtsuite import gui_eoi
    prim = Session(_grid(8.0), _grid(12.0), "tuned.bin")
    comp = Session(_grid(8.0, [(0, 1, -2.0)]), _grid(12.0), "stock.bin")
    monkeypatch.setattr(_eoi, "models", lambda s: [s.model])
    scr = QtCore.QRect(0, 0, w, h)
    monkeypatch.setattr(gui_eoi.EoiDialog, "screen", lambda self: type(
        "S", (), {"availableGeometry": lambda _s: scr})())
    ws = QtWidgets.QWidget()
    ws.primary, ws.comparison, ws.view_only = prim, comp, None
    d = gui_eoi.EoiDialog(ws, parent=ws)
    d.show()
    app.processEvents()
    try:
        assert d.cmpv.box.isVisibleTo(d)
        assert d.height() <= int(h * 0.82) + 1 and d.height() <= 720
        assert d.height() >= d.minimumSizeHint().height()
        assert d.width() <= w - 40
    finally:
        d.close()
        ws.deleteLater()


def test_the_switch_hides_the_comparison(dlg, app):
    dlg.cmpv.check.setChecked(False)
    app.processEvents()
    assert not dlg.cmpv.box.isVisibleTo(dlg) and not dlg.prim_head.isVisibleTo(dlg)
    assert all(dlg.grid.item(r, c).data(OUTLINE_ROLE) is None
               for r in range(3) for c in range(3))


def test_right_click_menu(dlg, app):
    dlg.grid.setCurrentCell(0, 1)
    dlg.grid.item(0, 1).setSelected(True)
    names = lambda m: [a.text() for a in m.actions() if a.text()]   # noqa: E731
    pm = dlg._build_menu(dlg.grid)
    got = names(pm)
    assert got[:2] == ["Copy", "Paste"]
    assert "Set target EOI for the selection..." in got
    assert "Go to SOI table (selected cells marked)" in got
    assert "Go to duration table" in got
    assert "Copy comparison → primary (1 cell)" in got
    assert "Copy primary − comparison (the difference)" in got
    cm = dlg._build_menu(dlg.cmpv.grid)       # the comparison grid: read only
    acts = {a.text(): a for a in cm.actions() if a.text()}
    assert not acts["Paste"].isEnabled()
    assert not acts["Set target EOI for the selection..."].isEnabled()
    assert acts["Copy primary − comparison (the difference)"].isEnabled()


def test_copy_primary_minus_comparison(dlg, app):
    dlg.grid.clearSelection()
    for rc in ((0, 1), (2, 2)):
        dlg.grid.item(*rc).setSelected(True)
    app.processEvents()
    menu = dlg._build_menu(dlg.cmpv.grid)
    act = next(a for a in menu.actions() if a.text().startswith("Copy primary"))
    act.trigger()
    text = QtWidgets.QApplication.clipboard().text()
    # EOI = duration - SOI: the comparison's SOI is 2 less at (0, 1), 3 more at
    # (2, 2), so its EOI is 2 more / 3 less and primary - comparison is -2 / +3;
    # the rectangle round the selection (columns 1-2), blank where unselected
    assert text == "-2\t\n\t\n\t3"


def test_copy_comparison_to_primary_is_one_undo_step(dlg, app):
    prim = dlg.session
    soi0 = prim.soi.vals[0][1]
    comp_soi = dlg.cmpv.session.soi.vals[0][1]
    assert soi0 != comp_soi
    dlg.grid.clearSelection()
    dlg.grid.item(0, 1).setSelected(True)
    dlg.grid.item(2, 2).setSelected(True)
    app.processEvents()
    n0 = prim.undo_steps
    menu = dlg._build_menu(dlg.grid)
    next(a for a in menu.actions() if a.text().startswith("Copy comparison")).trigger()
    app.processEvents()
    assert prim.undo_steps == n0 + 1
    assert prim.soi.vals[0][1] == pytest.approx(comp_soi, abs=1e-3)
    assert prim.soi.vals[2][2] == pytest.approx(dlg.cmpv.session.soi.vals[2][2], abs=1e-3)
    # now the same everywhere: no outlines left
    assert all(dlg.grid.item(r, c).data(OUTLINE_ROLE) is None
               for r in range(3) for c in range(3))


def test_paste_types_the_values_as_one_step(dlg, app):
    dlg.mode_group.button(1).click()                   # SOI view
    app.processEvents()
    prim = dlg.session
    dlg.grid.clearSelection()
    dlg.grid.setCurrentCell(0, 0)
    dlg.grid.item(0, 0).setSelected(True)
    QtWidgets.QApplication.clipboard().setText("1\t2\n3\t4")
    n0 = prim.undo_steps
    assert dlg._paste() == 4
    assert prim.undo_steps == n0 + 1
    assert [prim.soi.vals[0][0], prim.soi.vals[0][1],
            prim.soi.vals[1][0], prim.soi.vals[1][1]] == pytest.approx([1, 2, 3, 4])
    # one value over a selection fills it
    dlg.grid.setCurrentCell(2, 0)
    for rc in ((2, 0), (2, 1), (2, 2)):
        dlg.grid.item(*rc).setSelected(True)
    QtWidgets.QApplication.clipboard().setText("9")
    assert dlg._paste() >= 3
    assert prim.soi.vals[2] == pytest.approx([9, 9, 9])


def test_pasting_the_copied_table_skips_its_headers(dlg):
    from ovtsuite.gui_eoi import parse_paste
    text = "EOI deg at 90 C\t1000\t2000\n1.5\t4\t5\n2.5\t6\t7"
    assert parse_paste(text) == [[4.0, 5.0], [6.0, 7.0]]
    assert parse_paste("4\t5\n6\tx") == [[4.0, 5.0], [6.0, None]]
    assert parse_paste("") == []


def test_no_comparison_no_second_grid(app, monkeypatch):
    from ovtsuite import gui_eoi
    prim = Session(_grid(8.0), _grid(12.0), "tuned.bin")
    monkeypatch.setattr(_eoi, "models", lambda s: [s.model])
    ws = QtWidgets.QWidget()
    ws.primary, ws.comparison, ws.view_only = prim, None, None
    d = gui_eoi.EoiDialog(ws, parent=ws)
    d.show()
    app.processEvents()
    try:
        assert not d.cmpv.check.isVisibleTo(d) and not d.cmpv.box.isVisibleTo(d)
        menu = d._build_menu(d.grid)
        assert not any("comparison" in a.text().lower() for a in menu.actions())
        assert d.height() <= d.screen().availableGeometry().height() - 60
    finally:
        d.close()
        ws.deleteLater()
