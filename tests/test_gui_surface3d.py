# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The 3-D surface panel (ovtsuite.gui_surface3d): in the tab, above the
table, shown and hidden by the 3D surface toggle (Ctrl+3), remembered for
new tabs; live and editable -- a point picked on the surface is the table's
selection, the keys of Edit > Cells work in it, a Ctrl+drag raises the
selected points as one undo step, and an undo made anywhere redraws it.

Offscreen, on a synthetic map (no reference binaries needed).
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtTest, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the main window

ROWS, COLS = 4, 5


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """The map step and window state go to a file of the test's own."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, monkeypatch):
    from ovtsuite import gui
    for name in ("show_file_info", "_explain_limited_support"):
        monkeypatch.setattr(gui.Workspace, name, lambda self, *a, **k: None)
    return gui


def _map(binfile):
    from ovtsuite.model import Axis, Entity
    e = Entity(name="SurfTest_MAP", kind="map", tier=1, categories=["Misc"],
               description="", units="Nm", addr=0x1000, rows=ROWS, cols=COLS,
               bits=16, signed=False, equation="X*0.5", source="verified")
    e.binfile = binfile
    e.x = Axis(addr=0x800, count=COLS, bits=16)
    e.y = Axis(addr=0x900, count=ROWS, bits=16)
    return e


@pytest.fixture
def ws(G, app, tmp_path):
    from ovtsuite.session import TunerSession
    img = bytearray(0x4000)
    for i in range(ROWS * COLS):                 # a slope: 10, 12, 14 ...
        img[0x1000 + 2 * i:0x1002 + 2 * i] = (20 + 4 * i).to_bytes(2, "big")
    for i in range(COLS):
        img[0x800 + 2 * i:0x802 + 2 * i] = (1000 * i).to_bytes(2, "big")
    for i in range(ROWS):
        img[0x900 + 2 * i:0x902 + 2 * i] = (10 * i).to_bytes(2, "big")
    path = tmp_path / "synthetic.bin"
    path.write_bytes(bytes(img))
    s = TunerSession(str(path))
    e = _map(s.binfile)
    s.entities.append(e)
    w = G.MainWindow(s)
    w.resize(1100, 700)
    w.show()
    app.processEvents()
    tab = w.current
    tab.open_table(e)
    app.processEvents()
    yield tab
    w.close_discarding()


def _open(ws, app):
    panel = ws.open_surface()
    assert panel is not None and panel is ws.surface
    ws.surface_split.setSizes([420, 260])
    app.processEvents()
    panel.view.repaint()               # lays the points out for picking
    return panel


def _z_selection(ws):
    m = ws.model
    return {m.z_at(ix) for ix in ws.table.selectionModel().selectedIndexes()}


def _cur(ws):
    return ws.model.z_at(ws.table.currentIndex())


def test_toggled_in_the_tab_above_the_table(ws, app):
    from ovtsuite import gui_surface3d as S
    panel, split = ws.surface, ws.surface_split
    assert split.orientation() == QtCore.Qt.Vertical
    assert split.widget(0) is panel and split.widget(1) is ws.table, \
        "the surface sits above the table, in one splitter"
    assert panel.isHidden() and not ws.surf_btn.isChecked(), "off at first"
    assert ws.surf_btn.isCheckable()
    ws.surf_btn.click()                                  # on
    app.processEvents()
    assert panel.isVisible() and ws.surf_btn.isChecked()
    assert panel.window() is ws.window(), "not a window of its own"
    assert panel.geometry().bottom() < ws.table.mapTo(
        panel.parentWidget(), QtCore.QPoint(0, 0)).y()
    assert panel.view.rows == ROWS and panel.view.cols == COLS
    assert not panel.cmp_view.isVisibleTo(panel), "no comparison loaded"
    assert ws.open_surface() is panel, "Ctrl+3 from a menu: the same one"
    ws.surf_btn.click()                                  # off
    assert panel.isHidden() and not ws.surf_btn.isChecked()
    ws.surf_btn.click()
    panel.close_btn.click()                              # the x
    assert panel.isHidden() and not ws.surf_btn.isChecked()
    assert not S.is_shown(ws)


def test_ctrl_3_is_the_toggle(ws, app):
    # (shortcut dispatch itself is Qt's: offscreen it is not delivered)
    btn = ws.surf_btn
    assert btn.shortcut().toString() == "Ctrl+3" and btn.isCheckable()
    btn.toggle()                                         # what Ctrl+3 does
    app.processEvents()
    assert not ws.surface.isHidden()
    assert ws.surface.view.hasFocus() or not ws.window().isActiveWindow()
    btn.toggle()
    assert ws.surface.isHidden()


def test_hidden_it_reads_nothing(ws, app):
    panel = _open(ws, app)
    e = ws.model.entity
    old = e.cell(0, 0)
    ws.surf_btn.click()                                  # hidden
    calls = []
    real = panel._shown
    panel._shown = lambda g: calls.append(1) or real(g)
    ws.model._edit_cells([(0, 0, old + 20)])
    ws.poll()
    app.processEvents()
    assert not calls, "no reload while hidden"
    ws.surf_btn.click()                                  # shown: catches up
    app.processEvents()
    assert calls and panel.view.vals[0][0] == pytest.approx(old + 20)


def test_state_and_sizes_are_remembered(ws, app, G):
    from ovtsuite import gui_surface3d as S
    _open(ws, app)
    ws.surface_split.setSizes([300, 380])
    S._save_sizes(ws)                    # as a drag of the handle does
    saved = S.saved_sizes()
    assert saved and saved[0] == ws.surface_split.sizes()[0]
    win = ws.win
    new = win._add_workspace()           # a new tab starts the same way
    app.processEvents()
    assert S.is_shown(new) and new.surf_btn.isChecked()
    ws.surf_btn.click()                  # off, and remembered off
    other = win._add_workspace()
    assert not S.is_shown(other) and not other.surf_btn.isChecked()


def test_view_only_tab_shows_but_does_not_edit(ws, app):
    panel = _open(ws, app)
    ws.model.readonly = True             # as a saved version's tab is
    panel.reload()
    e = ws.model.entity
    ed = panel.view.editor
    assert not ed.can_edit()
    assert "read-only" in panel.title.text()
    ed.select_one(1, 1)
    before = e.cell(1, 1)
    assert ed.drag_commit(50.0) == 0
    assert e.cell(1, 1) == before and not ws.primary.modified
    assert panel.view.vals, "still drawn"


def test_comparison_side_by_side(ws, app):
    panel = _open(ws, app)
    m = ws.model
    m._cmp = [[v + 1 for v in row] for row in m._vals]
    m.cmp_name = "other.bin"
    panel.reload()
    app.processEvents()
    assert panel.cmp_view.isVisibleTo(panel)
    assert panel.cmp_view.rows == ROWS and panel.cmp_view.editor is None
    assert (panel.view.vmin, panel.view.vmax) == \
        (panel.cmp_view.vmin, panel.cmp_view.vmax), "one height scale"
    assert panel.cmp_view.mapTo(panel, QtCore.QPoint(0, 0)).x() > \
        panel.view.mapTo(panel, QtCore.QPoint(0, 0)).x(), "side by side"
    panel.view.orbit(0.3, 0.1)
    assert panel.cmp_view.yaw == pytest.approx(panel.view.yaw), "linked"
    assert "other.bin" in panel.cmp_label.text()


def test_picking_a_point_selects_its_cell_in_the_table(ws, app):
    panel = _open(ws, app)
    v = panel.view
    pt = v.screen_point(2, 3).toPoint()
    QtTest.QTest.mouseClick(v, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, pt)
    assert _cur(ws) == (2, 3)
    assert _z_selection(ws) == {(2, 3)}
    # Shift+click: the block from the current cell to the point
    QtTest.QTest.mouseClick(v, QtCore.Qt.LeftButton, QtCore.Qt.ShiftModifier,
                            v.screen_point(3, 4).toPoint())
    assert _z_selection(ws) == {(2, 3), (2, 4), (3, 3), (3, 4)}
    # Ctrl+click takes one out again
    QtTest.QTest.mouseClick(v, QtCore.Qt.LeftButton,
                            QtCore.Qt.ControlModifier,
                            v.screen_point(3, 4).toPoint())
    assert _z_selection(ws) == {(2, 3), (2, 4), (3, 3)}
    # and a selection made in the table shows on the surface
    sm = ws.table.selectionModel()
    sm.setCurrentIndex(ws.model.index_of("z", 0, 0),
                       QtCore.QItemSelectionModel.ClearAndSelect)
    assert panel.view.editor.selected() == {(0, 0)}
    assert panel.view.editor.current() == (0, 0)


def _screen_dir(v, step):
    """Where a (row, col) step points on the screen (x right, y down), on
    the surface's base plane."""
    x1, _d, z2 = v._rot(step[1], step[0], 0.0)
    return x1, -z2


def test_arrows_follow_the_screen_however_it_is_turned(ws, app):
    panel = _open(ws, app)
    v = panel.view
    seen = {}
    for turn in (0.0, math.pi / 2, math.pi):
        v.reset_view()
        v.orbit(turn, 0.0)
        for key, (wx, wy) in ((QtCore.Qt.Key_Up, (0, -1)),
                              (QtCore.Qt.Key_Down, (0, 1)),
                              (QtCore.Qt.Key_Left, (-1, 0)),
                              (QtCore.Qt.Key_Right, (1, 0))):
            panel.view.editor.select_one(2, 2)
            QtTest.QTest.keyClick(v, key)
            r, c = _cur(ws)
            step = (r - 2, c - 2)
            assert abs(step[0]) + abs(step[1]) == 1
            sx, sy = _screen_dir(v, step)
            assert sx * wx + sy * wy > 0, "the arrow's way on the screen"
            seen[(turn, key)] = step
    # turned half round, the same key walks the grid the other way
    up0, up180 = seen[(0.0, QtCore.Qt.Key_Up)], seen[(math.pi, QtCore.Qt.Key_Up)]
    assert up180 == (-up0[0], -up0[1])


def test_shift_extends_ctrl_jumps_and_brackets_turn(ws, app):
    panel = _open(ws, app)
    v = panel.view
    e = ws.model.entity
    panel.view.editor.select_one(2, 2)
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_Right, QtCore.Qt.ShiftModifier)
    assert len(_z_selection(ws)) == 2
    yaw, pitch = v.yaw, v.pitch
    panel.view.editor.select_one(2, 2)
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_Right, QtCore.Qt.ControlModifier)
    assert (v.yaw, v.pitch) == (yaw, pitch), "Ctrl+arrow no longer turns"
    r, c = _cur(ws)
    assert (r, c) != (2, 2) and (r in (0, e.rows - 1) or c in (0, e.cols - 1))
    assert len(_z_selection(ws)) == 1, "the landing cell alone"
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_BracketRight)
    assert v.yaw == pytest.approx(yaw + 0.1)
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_PageUp)
    assert v.pitch == pytest.approx(pitch + 0.08)
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_Home)
    assert v.yaw == pytest.approx(-0.7)


def test_the_cell_keys_edit_from_the_surface(ws, app):
    from ovtsuite import gui_cellkeys as K
    panel = _open(ws, app)
    e = ws.model.entity
    panel.view.editor.select_one(1, 1)
    before = e.cell(1, 1)
    step = K.default_step(e)
    panel.view.setFocus()
    act = next(a for a in panel.cell_actions if a.text() == "Step up")
    assert act.shortcut().toString() == "Alt+Up"
    assert act in panel.actions(), "bound to the surface panel too"
    act.trigger()
    assert e.cell(1, 1) == pytest.approx(before + step)
    app.processEvents()
    assert panel.view.vals[1][1] == pytest.approx(before + step), "redrawn"
    ws.primary.undo()
    assert e.cell(1, 1) == pytest.approx(before)


def test_ctrl_drag_raises_the_selection_as_one_undo_step(ws, app):
    panel = _open(ws, app)
    v = panel.view
    e = ws.model.entity
    cells = [(1, 1), (1, 2), (2, 1)]
    ed = v.editor
    ed.select_one(*cells[0])
    for c in cells[1:]:
        ed.toggle(*c)
    assert ed.selected() == set(cells)
    before = {c: e.cell(*c) for c in cells}
    other = e.cell(3, 3)
    rev = ws.primary.undo_stack.revision
    start = v.screen_point(1, 1).toPoint()
    ctrl = QtCore.Qt.ControlModifier
    QtTest.QTest.mousePress(v, QtCore.Qt.LeftButton, ctrl, start)
    QtTest.QTest.mouseMove(v, start - QtCore.QPoint(0, 40))
    app.processEvents()
    assert v.preview, "a live preview while dragging"
    assert e.cell(1, 1) == before[(1, 1)], "nothing written yet"
    QtTest.QTest.mouseRelease(v, QtCore.Qt.LeftButton, ctrl,
                              start - QtCore.QPoint(0, 40))
    app.processEvents()
    assert not v.preview
    after = {c: e.cell(*c) for c in cells}
    assert all(after[c] > before[c] for c in cells), "raised"
    deltas = {round(after[c] - before[c], 6) for c in cells}
    assert len(deltas) == 1, "all by the same amount"
    assert e.cell(3, 3) == other, "unselected cells stay"
    assert ws.primary.undo_stack.revision != rev
    ws.primary.undo()                                  # ONE undo step
    assert {c: e.cell(*c) for c in cells} == before


def test_escape_cancels_a_drag(ws, app):
    panel = _open(ws, app)
    v = panel.view
    e = ws.model.entity
    v.editor.select_one(2, 2)
    before = e.cell(2, 2)
    start = v.screen_point(2, 2).toPoint()
    ctrl = QtCore.Qt.ControlModifier
    QtTest.QTest.mousePress(v, QtCore.Qt.LeftButton, ctrl, start)
    QtTest.QTest.mouseMove(v, start - QtCore.QPoint(0, 50))
    assert v.preview
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_Escape)
    assert not v.preview and panel.isVisible()
    QtTest.QTest.mouseRelease(v, QtCore.Qt.LeftButton, ctrl,
                              start - QtCore.QPoint(0, 50))
    assert e.cell(2, 2) == before
    assert not ws.primary.modified


def test_follows_an_edit_and_an_undo_made_elsewhere(ws, app):
    panel = _open(ws, app)
    e = ws.model.entity
    old = e.cell(0, 4)
    ws.model._edit_cells([(0, 4, old + 50)])        # as the grid would
    ws.poll()
    app.processEvents()
    assert panel.view.vals[0][4] == pytest.approx(old + 50)
    assert panel.view.vmax >= old + 50, "ranges recomputed"
    top = panel.view.vmax
    ws.primary.undo()
    ws.poll()                                       # the window's timer
    app.processEvents()
    assert panel.view.vals[0][4] == pytest.approx(old)
    # the scale stays as wide while the same map is shown: an edited point
    # moves on it, the scale does not move with it
    assert panel.view.vmax == pytest.approx(top)
    QtTest.QTest.keyClick(panel.view, QtCore.Qt.Key_Home)   # Reset view
    app.processEvents()
    assert panel.view.vmax == pytest.approx(
        max(v for row in panel.view.vals for v in row))


def test_a_curve_is_not_drawn(ws, app):
    from ovtsuite.model import Entity
    e = Entity(name="Curve", kind="curve", tier=1, categories=["Misc"],
               description="", units="", addr=0x1200, rows=1, cols=6,
               bits=16, signed=False, equation="X", source="verified")
    e.binfile = ws.primary.binfile
    ws.primary.entities.append(e)
    panel = _open(ws, app)
    ws.open_table(e)                               # the table moves on
    app.processEvents()
    assert panel.view.rows == 0, "shows 'Not a 2-D map', no stale surface"
    assert "Curve" in panel.title.text()


def test_turned_with_the_table(ws, app):
    panel = _open(ws, app)
    ws.model.set_transposed(True)                  # axes swapped on screen
    app.processEvents()
    v = panel.view
    assert (v.rows, v.cols) == (COLS, ROWS)
    v.repaint()
    QtTest.QTest.mouseClick(v, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                            v.screen_point(3, 1).toPoint())
    assert _cur(ws) == (1, 3), "view (row, col) is the grid's, not stored"
    assert v.vals[3][1] == pytest.approx(ws.model.entity.cell(1, 3))
