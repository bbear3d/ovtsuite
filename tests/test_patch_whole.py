# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patches of whole tables: tables the user picks, taken with every cell and
breakpoint whether or not they differ (patch.whole_entry), the searchable list
they are picked from (gui_patchtables), the Generate Patch window's default
direction (this tab's values laid over the comparison) and the tree's
"Create patch from the selected tables".

Slow: each case loads a full TunerSession."""
import os

import pytest

from ovtsuite import patch as P
from ovtsuite.session import TunerSession

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
PRIMARY, COMPARISON = "038906016AB.bin", "038997016R_remap.bin"


def _session(name):
    path = os.path.join(BIN, name)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % name)
    return TunerSession(path)


@pytest.fixture(scope="module")
def a():
    return _session(PRIMARY)


@pytest.fixture(scope="module")
def b():
    return _session(COMPARISON)


def _unchanged_map(a, b):
    """A map both files hold, the same in both, with an axis."""
    for e in a.entities:
        if e.kind == "map" and e.rows > 2 and e.cols > 2 and e.x_values():
            cp = a.counterpart(b, e)
            if cp is not None and (cp.rows, cp.cols) == (e.rows, e.cols) \
                    and cp.values() == e.values():
                return e, cp
    pytest.skip("no unchanged map shared by the two files")


def test_a_whole_entry_carries_every_cell_and_breakpoint_though_nothing_differs(a, b):
    e, cp = _unchanged_map(a, b)
    assert P._candidate(e, cp) is None              # the diff does not offer it
    w = P.whole_entry(cp, e)
    assert w.whole and w.selected and w.name == cp.name
    assert (w.rows, w.cols) == (cp.rows, cp.cols) and w.values == cp.values()
    assert w.x_axis == cp.x_values() and w.y_axis == cp.y_values()
    assert w.changed_cells == 0 and "unchanged" in w.first_change
    assert P.PatchEntry.from_dict(w.to_dict()).whole


def test_a_whole_table_is_written_into_a_target_it_differs_in(a, b, tmp_path):
    """Hand-edit one cell and one breakpoint of a copy of A; the patch of that table, whole,
    from B puts both back -- a changed-only diff of B over A would have carried the same,
    but the whole table also works when the target already matches."""
    e, cp = _unchanged_map(a, b)
    t = a.sibling(bytes(a.binfile.data), name="target")
    te = next(x for x in t.entities if x.name == e.name)
    v = te.values()
    t.edit_cells(te, [(0, 0, v[0][0] + 1)])
    t.edit_axis(te.x, 0, te.x_values()[0] + 1)
    assert te.cell(0, 0) != e.cell(0, 0)
    w = P.whole_entry(cp, te)
    assert w.changed_cells == 1 and w.changed_axes == 1
    res = P.apply_patch(P.new_patch(t, b, [w]), t, default_disposition=P.APPLY)
    assert w.name in res.applied
    te2 = next(x for x in t.entities if x.name == e.name)
    assert te2.values() == cp.values() and te2.x_values() == cp.x_values()


def test_a_whole_table_writes_its_breakpoints_even_when_none_is_marked_changed(a, b):
    e, cp = _unchanged_map(a, b)
    t = a.sibling(bytes(a.binfile.data), name="target")
    te = next(x for x in t.entities if x.name == e.name)
    t.edit_axis(te.x, 1, te.x_values()[1] + 2)
    w = P.whole_entry(cp)                           # no counterpart: nothing is marked
    assert w.changed_axes == 0 and w.whole
    P.apply_patch(P.new_patch(t, b, [w]), t, default_disposition=P.APPLY)
    assert next(x for x in t.entities if x.name == e.name).x_values() == cp.x_values()


def test_a_constant_is_taken_whole_too(a):
    c = next(e for e in a.entities if e.kind == "constant")
    w = P.whole_entry(c)
    assert w.whole and w.kind == "constant" and w.values == [[c.cell(0, 0)]]


# ------------------------------------------------------------------ the picker
@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_picker_narrows_by_every_word_and_offers_what_is_not_taken(app, a):
    from ovtsuite import gui_patchtables as GT
    ents = list(a.entities)
    taken = {ents[0].name}
    d = GT.TablePicker(ents, taken=taken)
    try:
        assert ents[0].name not in {i.data(0, GT._ROLE).name for i in d._items}
        n_all = len(d.shown())
        word = next(e for e in ents[1:50] if e.name).name.split("_")[0].lower()
        d.search.setText(word)
        d._filter()
        shown = d.shown()
        assert 0 < len(shown) <= n_all
        assert all(word in d._text[d._items.index(i)] for i in shown)
        d.search.setText(word + " zzzzqqqq")
        d._filter()
        assert not d.shown()
        d.search.setText("")
        d._filter()
        assert len(d.shown()) == n_all
        d.search.setText(word)
        d._filter()
        d.check_shown(True)
        assert {e.name for e in d.checked()} == {
            i.data(0, GT._ROLE).name for i in d.shown()}
        d.uncheck_all()
        assert not d.checked()
    finally:
        d.close()


# ------------------------------------------------------------ the windows
def test_the_generate_patch_window_carries_this_tabs_values_by_default(app, a, b):
    from ovtsuite import gui
    w = gui.MainWindow(a, b)
    try:
        seen = {}
        orig = gui.PatchBuilderDialog.exec
        gui.PatchBuilderDialog.exec = lambda self: seen.setdefault("d", self)
        try:
            w.current.open_patch_builder()
        finally:
            gui.PatchBuilderDialog.exec = orig
        d = seen["d"]
        assert d.primary is b and d.comparison is a           # carries A's values over B's
        assert "values over" in d.direction.text()
    finally:
        w.close_discarding()


def test_whole_tables_join_the_list_and_a_listed_one_is_only_checked(app, a, b):
    from PySide6 import QtCore
    from ovtsuite import gui
    e, cp = _unchanged_map(a, b)
    d = gui.PatchBuilderDialog(b, a)                         # carries A's values over B's
    try:
        n = len(d._entries)
        d.add_whole([e])
        assert len(d._entries) == n + 1 and d._entries[-1].whole
        # raw ranges share the table now (gui_differences): count the entry rows
        assert sum(1 for k, _i in d._rows if k == "entry") == len(d._entries)
        assert "added whole" in d.head_lbl.text()
        d.add_whole([e])                                     # already there: no second row
        assert len(d._entries) == n + 1
        d.tbl.item(n, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        d.add_whole([e])
        assert d.tbl.item(n, 0).checkState() == QtCore.Qt.CheckState.Checked
    finally:
        d.close()


def test_a_patch_of_tables_without_a_comparison(app, a):
    from ovtsuite import gui
    e = next(x for x in a.entities if x.kind == "map")
    d = gui.PatchBuilderDialog(a, a, solo=True, preselect=[e])
    try:
        assert d.solo and [x.name for x in d._entries] == [e.name]
        assert d._entries[0].whole and not d._gaps
        assert "Whole tables from" in d.direction.text()
    finally:
        d.close()


def test_the_tree_selects_several_parameters_and_its_menu_offers_the_patch(app, a, b):
    from PySide6 import QtWidgets
    from ovtsuite import gui, gui_paramenu as PM
    w = gui.MainWindow(a, b)
    try:
        ws = w.current
        assert ws.tree.selectionMode() == \
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        leaves = []

        def walk(it):
            for k in range(it.childCount()):
                c = it.child(k)
                if PM.entity_of(c) is not None:
                    leaves.append(c)
                walk(c)
        walk(ws.tree.invisibleRootItem())
        assert len(leaves) > 3
        for it in leaves[:3]:
            it.setSelected(True)
        QtWidgets.QApplication.processEvents()
        menu = PM.build_menu(ws, leaves[0])
        texts = [x.text() for x in menu.actions()]
        assert any(t.startswith("Create patch from 3 selected tables") for t in texts)
        # one table clicked among no selection: this table
        ws.tree.clearSelection()
        assert [x.text() for x in PM.build_menu(ws, leaves[5]).actions()
                if x.text().startswith("Create patch")] == ["Create patch from this table..."]
    finally:
        w.close_discarding()


def _found_map(d):
    """A map the patch found changed (not whole), with its row."""
    for i, pe in enumerate(d._entries):
        if pe.kind == "map" and not pe.whole and pe.changed_cells:
            return i, pe
    pytest.skip("no map found changed between the two files")


def test_a_table_found_changed_is_offered_marked_and_picked_whole(app, a, b):
    """Add tables... lists a table found changed (it used to be left out),
    noting how much changed; picking it makes its row whole in place."""
    from ovtsuite import gui
    from ovtsuite import gui_patchtables as GT
    d = gui.PatchBuilderDialog(b, a)                         # A's values over B's
    try:
        i, pe = _found_map(d)
        names = GT.values_names(d.primary, d.comparison, d._entries)
        ent = next(e for e in a.entities if names.get(e.name) == i)
        p = GT.TablePicker([ent], partial={ent.name: GT.changed_note(pe)})
        try:
            assert [it.text(3) for it in p._items] == [GT.changed_note(pe)]
        finally:
            p.close()
        n = len(d._entries)
        d.add_whole([ent])
        assert len(d._entries) == n                          # no second row
        w = d._entries[i]
        assert w.whole and w.name == pe.name and w.values == pe.values
        assert "whole table" in d.tbl.item(i, 2).text()
        assert "added whole" not in d.head_lbl.text()        # still a found one
    finally:
        d.close()


def test_rows_go_whole_and_back_as_found(app, a, b):
    from ovtsuite import gui
    d = gui.PatchBuilderDialog(b, a)
    try:
        i, pe = _found_map(d)
        assert d._set_whole(i, True) and d._entries[i].whole
        assert d._set_whole(i, False) and d._entries[i] is pe
        e, _cp = _unchanged_map(a, b)
        d.add_whole([e])                                     # added whole, never found
        assert not d._set_whole(len(d._entries) - 1, False)
    finally:
        d.close()


# ------------------------------------------------- one Differences window
def test_differences_and_generate_patch_are_one_window(app, a, b):
    """Show differences opens the window as a list; Make patch shows the
    same rows with the same counts, plus the check boxes; Compare offers
    the comparison and the history; Show adds raw rows nobody can check;
    Swap turns the pair round in place."""
    from PySide6 import QtCore
    from ovtsuite import gui
    from ovtsuite import gui_differences as GD
    w = gui.MainWindow(a, b)
    try:
        ws = w.current
        ws.show_differences()
        d = ws._diff_dlg
        assert isinstance(d, GD.DifferencesWindow) and d.mode == GD.LIST
        assert d.tbl.isColumnHidden(GD.C_CHECK) and "Differences" in d.windowTitle()
        assert d.primary is ws.comparison and d.comparison is ws.primary
        listed = [(d.tbl.item(r, GD.C_NAME).text(), d.tbl.item(r, GD.C_CHANGED).text())
                  for r, (k, _i) in enumerate(d._rows) if k == "entry"]
        assert listed
        d.set_mode(GD.PATCH)
        assert not d.tbl.isColumnHidden(GD.C_CHECK) and "Generate Patch" in d.windowTitle()
        assert [(d.tbl.item(r, GD.C_NAME).text(), d.tbl.item(r, GD.C_CHANGED).text())
                for r, (k, _i) in enumerate(d._rows) if k == "entry"] == listed
        kinds = [d.source.itemData(i) for i in range(d.source.count())]
        assert kinds[0] == "comparison" and "history" in kinds
        d.blk_prog.setChecked(True)
        for r, (k, _i) in enumerate(d._rows):
            if k == "raw":
                assert not (d.tbl.item(r, GD.C_CHECK).flags()
                            & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        p, c = d.primary, d.comparison
        d.swap()
        assert d.primary is c and d.comparison is p
        d.close()
    finally:
        w.close_discarding()


def test_one_count_for_both_views(app, a, b):
    """The window's counts come from summarize -- the list's count -- so a
    table shows the same number however the window is opened."""
    from ovtsuite import gui_differences as GD
    d = GD.DifferencesWindow(b, a)
    try:
        by = {e.name: e for e in b.entities}
        for pe in d._entries[:40]:
            e = by.get(pe.name)
            if e is None:
                continue
            n, first = GD.summarize(e, b.counterpart(a, e))
            assert (n, first) == (pe.changed_cells, pe.first_change)
    finally:
        d.close()
