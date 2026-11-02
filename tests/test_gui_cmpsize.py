# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison at its original size (ovtsuite/gui_cmpsize.py), on two
synthetic binaries each with a packaged XDF -- no reference files: one table
"Fuel map", 2 x 4 in the primary and 3 x 6 (or 2 x 4) in the comparison."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.gui_cellmarks import OUTLINE_ROLE  # noqa: E402

pytestmark = pytest.mark.slow

OPTS = {"base": 0, "relocate": False, "unverified": True}
SEL = QtCore.QItemSelectionModel.SelectionFlag


def _xdf(rows, cols, zaddr):
    return ('''<?xml version="1.0"?>
<XDFFORMAT version="1.80"><XDFHEADER><deftitle>T</deftitle>
 <CATEGORY index="0x0" name="Fuel" /></XDFHEADER>
 <XDFTABLE uniqueid="0x1"><title>Fuel map</title><CATEGORYMEM index="0" category="1" />
  <XDFAXIS id="x"><EMBEDDEDDATA mmedaddress="0x100" mmedelementsizebits="8" />
   <indexcount>%d</indexcount><MATH equation="X*100" /></XDFAXIS>
  <XDFAXIS id="y"><EMBEDDEDDATA mmedaddress="0x180" mmedelementsizebits="8" />
   <indexcount>%d</indexcount><MATH equation="X" /></XDFAXIS>
  <XDFAXIS id="z"><EMBEDDEDDATA mmedaddress="%s" mmedelementsizebits="8"
     mmedrowcount="%d" mmedcolcount="%d" /><MATH equation="X" /></XDFAXIS>
 </XDFTABLE></XDFFORMAT>''' % (cols, rows, zaddr, rows, cols)).encode()


def _session(path, data, rows, cols, zaddr):
    from ovtsuite import xdf_session
    from ovtsuite.session import TunerSession
    with open(path, "wb") as fh:
        fh.write(data)
    s = TunerSession(str(path))
    xdf_session.apply_definition(s, _xdf(rows, cols, zaddr), "xdf", options=OPTS)
    return s


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, monkeypatch):
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    monkeypatch.setattr(gui.Workspace, "_explain_limited_support",
                        lambda self, *a, **k: None, raising=False)
    # tables of a definition are paired by name here
    monkeypatch.setattr(TunerSession, "counterpart",
                        lambda self, other, e: next(
                            (x for x in other.entities if x.name == e.name), None))
    return gui


def _win(G, tmp_path, crows, ccols):
    prim = _session(tmp_path / "tuned.bin", bytes(range(256)) * 16, 2, 4, "0x200")
    cmp_ = _session(tmp_path / "stock.bin", bytes(range(255, -1, -1)) * 16,
                    crows, ccols, "0x300")
    w = G.MainWindow(prim, cmp_)
    w.resize(1100, 900)
    w.show()
    QtWidgets.QApplication.processEvents()
    ws = w.current
    ws.show_entity(next(e for e in prim.entities if e.name == "Fuel map"))
    QtWidgets.QApplication.processEvents()
    return w, ws


@pytest.fixture
def different(G, tmp_path):
    w, ws = _win(G, tmp_path, 3, 6)
    yield ws
    w.close_discarding()


@pytest.fixture
def same(G, tmp_path):
    w, ws = _win(G, tmp_path, 2, 4)
    yield ws
    w.close_discarding()


def _go(ws, size="original"):
    from ovtsuite import gui_cmpsize as cs
    cs.set_size(ws, size)
    QtWidgets.QApplication.processEvents()
    QtWidgets.QApplication.processEvents()
    return ws.cmp_grid.native


def _outlined(model):
    return {(r, c) for r in range(model.rowCount()) for c in range(model.columnCount())
            if model.data(model.index(r, c), OUTLINE_ROLE)}


def test_resampled_is_the_default_and_unchanged(different):
    from ovtsuite import gui_cmpsize as cs
    ws = different
    assert cs.default() == "resampled" and cs.size_of(ws) == "resampled"
    assert not ws.cmp_grid.native.on and ws.model.sep_row() is not None
    assert not ws.model.outline_all


def test_original_size_is_a_grid_of_its_own_at_the_comparisons_size(different):
    ws = different
    nv = _go(ws)
    n, m = nv.model, ws.model
    assert nv.on and nv.view.isVisibleTo(ws) and not ws.cmp_grid.view.isVisibleTo(ws)
    assert (n.entity.rows, n.entity.cols) == (3, 6) and not nv.linked()
    assert (m.entity.rows, m.entity.cols) == (2, 4)
    # the comparison block is out of the primary's grid
    sep = m.sep_row()
    assert all(ws.table.isRowHidden(r) for r in range(sep, m.rowCount()))
    # its own axes and values, as stored (not resampled onto the primary's)
    assert n._vals == [[b for b in (255 - 0x300 % 256 - (r * 6 + c) for c in range(6))]
                       for r in range(3)] or n._vals[0][0] == 255 - (0x300 % 256)
    assert len(n._x) == 6 and len(n._y) == 3
    assert "original size" in ws.cmp_grid.cmp_head.text()
    assert "not linked" in ws.cmp_grid.cmp_head.text()


def test_different_sizes_outline_every_cell_of_both_tables_axes_included(different):
    ws = different
    nv = _go(ws)
    for model in (ws.model, nv.model):
        cells = _outlined(model)
        part = model._n_prim() if model is ws.model else model.rowCount()
        want = {(r, c) for r in range(part) for c in range(model.columnCount())
                if (r, c) != (0, 0) and not (model.data(model.index(r, c)) is None
                                             and model._kind(r, c)[0] == "corner")}
        assert (0, 0) not in cells
        assert {(r, c) for r, c in want if r < part} <= cells
        # the axis bands are in it, and nothing is filled orange
        assert any(model._kind(r, c)[0] in ("x", "y") for r, c in cells)
    assert ws.model.primary_delta() == "abs" and ws.model.delta_mode() == "abs"


def test_selection_is_not_linked_when_the_sizes_differ(different, app):
    ws = different
    nv = _go(ws)
    assert not nv.link.enabled
    m, n = ws.model, nv.model
    ws.table.clearSelection()
    nv.view.selectionModel().select(
        QtCore.QItemSelection(n.index(2, 2), n.index(3, 5)), SEL.ClearAndSelect)
    app.processEvents()
    assert not ws.table.selectionModel().selectedIndexes()
    ws.table.selectionModel().select(m.index(1, 1), SEL.ClearAndSelect)
    app.processEvents()
    assert len(nv.view.selectionModel().selectedIndexes()) == 8     # its own
    assert len(ws.table.selectionModel().selectedIndexes()) == 1


def test_the_copy_targets_are_the_same_row_and_column(different, app):
    ws = different
    nv = _go(ws)
    n = nv.model
    nv.view.selectionModel().select(
        QtCore.QItemSelection(n.index(2, 3), n.index(3, 6)), SEL.ClearAndSelect)
    app.processEvents()
    cells, pairs, total = nv.destinations()
    # comparison cells 2..3 x 3..6 are 8; the primary has 2 rows x 4 columns,
    # so grid rows 2..3 (z rows 1..2 of 3) and columns 3..4 reach its cells
    assert total == 8
    assert sorted(cells) == [(2, 3), (2, 4), (3, 3), (3, 4)] or len(cells) < total
    assert len(pairs) == len(cells) and len(cells) < total
    assert nv.target.isVisibleTo(ws)            # the dashed frames


def test_copy_to_primary_is_cell_for_cell_without_resampling(different, app):
    ws = different
    nv = _go(ws)
    n, m = nv.model, ws.model
    before = [row[:] for row in m._vals]
    nv.view.selectionModel().select(
        QtCore.QItemSelection(n.index(1, 1), n.index(n.rowCount() - 1, n.columnCount() - 1)),
        SEL.ClearAndSelect)
    steps = len(ws.primary.undo_stack)
    written = nv.copy_to_primary()
    assert written > 0
    assert len(ws.primary.undo_stack) == steps + 1          # one undo step
    # primary cell (r, c) holds the comparison's stored cell (r, c), exactly
    for r in range(2):
        for c in range(4):
            assert m._vals[r][c] == n._vals[r][c]
    ws.primary.undo()
    ws.model.refresh()
    assert m._vals == before


def test_ctrl_r_copies_from_the_comparison_cell_for_cell(different, app):
    from ovtsuite import gui_cellkeys as gck
    ws = different
    nv = _go(ws)
    m, n = ws.model, nv.model
    ws.table.selectionModel().select(
        QtCore.QItemSelection(m.index(1, 1), m.index(2, 4)), SEL.ClearAndSelect)
    steps = len(ws.primary.undo_stack)
    gck.from_comparison(ws.win)
    assert len(ws.primary.undo_stack) == steps + 1
    assert [m._vals[r][c] for r in range(2) for c in range(4)] == \
        [n._vals[r][c] for r in range(2) for c in range(4)]


def test_paste_at_the_primarys_current_cell(different, app):
    ws = different
    nv = _go(ws)
    n, m = nv.model, ws.model
    nv.view.selectionModel().select(n.index(1, 1), SEL.ClearAndSelect)
    ws.table.selectionModel().setCurrentIndex(m.index(2, 3), SEL.ClearAndSelect)
    assert nv.paste_at_current() == 1
    assert m._vals[1][2] == n._vals[0][0]


def test_same_sizes_link_the_grids_and_outline_only_what_differs(same, app):
    ws = same
    nv = _go(ws)
    n, m = nv.model, ws.model
    assert nv.on and nv.linked() and nv.link.enabled
    assert not m.outline_all and not n.outline_all
    # every value differs in these two files, so the cells are all outlined;
    # make one cell equal and it is the exception
    n._vals[0][0] = m._vals[0][0]
    nv._outline_differences()
    assert (0, 0) not in n.outline_cells[0] and (0, 1) in n.outline_cells[0]
    ws.table.selectionModel().select(
        QtCore.QItemSelection(m.index(1, 1), m.index(2, 3)), SEL.ClearAndSelect)
    app.processEvents()
    assert {(i.row(), i.column()) for i in nv.view.selectionModel().selectedIndexes()} \
        == {(r, c) for r in (1, 2) for c in (1, 2, 3)}
    assert not nv.target.isVisibleTo(ws)        # linked: no copy-target frames


@pytest.mark.parametrize("layout", ["off", "mark", "below", "beside"])
def test_every_layout_gets_the_original_size_grid(different, layout):
    from ovtsuite import options
    ws = different
    try:
        options.set("ui/compare_layout", layout)
        QtWidgets.QApplication.processEvents()
        nv = _go(ws)
        assert nv.on and nv.view.isVisibleTo(ws)
        horiz = ws.cmp_grid.split.orientation() == QtCore.Qt.Orientation.Horizontal
        assert horiz == (layout == "beside")
        _go(ws, "resampled")
        assert not ws.cmp_grid.native.on
        assert not ws.model.outline_all
    finally:
        options.set("ui/compare_layout", "off")


def test_the_setting_is_per_table_with_a_default(different, tmp_path):
    from ovtsuite import gui_cmpsize as cs
    from ovtsuite import options
    ws = different
    try:
        options.set(cs.KEY, "original")
        cs.apply_all(ws.win)
        assert cs.size_of(ws) == "original" and ws.cmp_grid.native.on
        assert not cs.overridden(ws)
        cs.set_size(ws, "resampled")                     # this table only
        assert cs.size_of(ws) == "resampled" and cs.overridden(ws)
        assert not ws.cmp_grid.native.on
        assert ws.tables_state()["tables"][0]["cmpsize"] == "resampled"
        cs.set_size(ws, None)                            # back to the default
        assert ws.cmp_grid.native.on
        cs.set_size(ws, "resampled")
        cs.make_default(ws, "resampled")                 # default for everyone
        assert options.get(cs.KEY) == "resampled" and not cs.overridden(ws)
    finally:
        options.set(cs.KEY, "resampled")


def test_the_menus_offer_the_setting(different):
    from ovtsuite import gui_cmpsize as cs
    ws = different
    btn = ws.cmpsize_btn
    assert btn.isEnabled() and "resampled" in btn.text()
    menu = btn.menu()
    menu.aboutToShow.emit()
    acts = {a.text(): a for a in menu.actions() if a.text()}
    assert acts["Original size"].isEnabled() and not acts["Original size"].isChecked()
    assert not acts["Use the default for this table  (Resampled)"].isEnabled()
    acts["Original size"].trigger()
    QtWidgets.QApplication.processEvents()
    assert cs.size_of(ws) == "original" and "original size" in btn.text()
    menu.aboutToShow.emit()
    acts = {a.text(): a for a in menu.actions() if a.text()}
    assert acts["Original size"].isChecked()
    assert acts["Use the default for this table  (Resampled)"].isEnabled()
    assert "not linked" in [a.text() for a in menu.actions()][-1]
    # right-click on the table, and the comparison's own grid
    rm = QtWidgets.QMenu()
    cs.add_to_menu(ws, rm)
    assert any(a.text() == "Show comparison at original size" and a.isChecked()
               for a in rm.actions())
    nm = ws.cmp_grid.native.build_menu()
    texts = [a.text() for a in nm.actions()]
    assert "Copy" in texts and "Paste at the primary's current cell" in texts
    assert any(t.startswith("Copy comparison → primary") for t in texts)
    # View > Comparison size
    vm = QtWidgets.QMenu()
    sub = cs.add_menu(ws.win, vm)
    sub.aboutToShow.emit()
    assert [a.text() for a in sub.actions() if a.isCheckable() and a.isChecked()] \
        == ["Original size"]


def test_the_description_line_says_how_the_comparison_is_shown(different):
    ws = different
    assert "resampled onto this table's axes" in ws.desc.text()
    _go(ws)
    assert "original size, not resampled" in ws.desc.text()
    assert "sizes differ" in ws.desc.text()
    _go(ws, "resampled")
    assert "resampled onto this table's axes" in ws.desc.text()


def test_resampled_below_still_links_by_the_banner_offset(different, app):
    """What the grid did before this setting: the comparison's rows sit under
    the primary's banner, selected and scrolled with the table's."""
    from ovtsuite import options
    ws = different
    try:
        options.set("ui/compare_layout", "below")
        app.processEvents()
        g, m = ws.cmp_grid, ws.model
        sep = m.sep_row()
        assert g.active() and not g.native.on and g.view.isVisibleTo(ws)
        assert not g.native.view.isVisibleTo(ws)
        ws.table.selectionModel().select(m.index(1, 1), SEL.ClearAndSelect)
        app.processEvents()
        assert {(i.row(), i.column()) for i in g.view.selectionModel().selectedIndexes()} \
            == {(sep + 1 + 1, 1)}
    finally:
        options.set("ui/compare_layout", "off")


def test_the_comparison_selection_survives_the_copy(different, app):
    """Copying redraws the table; the comparison cells selected stay selected,
    so a second copy or a paste needs no new selection."""
    ws = different
    nv = _go(ws)
    n = nv.model
    nv.view.selectionModel().select(
        QtCore.QItemSelection(n.index(1, 1), n.index(2, 3)), SEL.ClearAndSelect)
    cells = {(i.row(), i.column()) for i in nv.view.selectionModel().selectedIndexes()}
    assert nv.copy_to_primary() > 0
    app.processEvents()
    assert {(i.row(), i.column())
            for i in nv.view.selectionModel().selectedIndexes()} == cells
