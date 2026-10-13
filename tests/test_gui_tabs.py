# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tabbed workspaces: several primary/comparison sets open at once.

Driven offscreen. What is pinned is what a user relies on without looking:
where a file opens, that one file is never open in two tabs, that a live
comparison follows its tab and freezes when that tab goes, that an ECU and a
TCU never pair, that tool windows belong to their tab, and that closing asks
before throwing edits away.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    """The gui module with its modal popups answered, in private settings."""
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a) or 0))
    gui._told = told
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    # Qt fixes the settings path at its first use in a process, so every
    # test here shares one private settings file: start each from no history
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _labels(w):
    return [w.tabs.tabText(i) for i in range(w.tabs.count())]


def _entity(ws, name):
    return next(e for e in ws.primary.entities if e.name == name)


def _differing_map(a, b):
    """A 2-D parameter present in both sessions, same shape, whose values
    differ -- something worth copying."""
    for e in a.primary.entities:
        if e.kind != "map" or e.rows < 2 or e.cols < 2:
            continue
        cp = a.primary.counterpart(b.primary, e)
        if cp is None or (cp.rows, cp.cols) != (e.rows, e.cols):
            continue
        if e.x_values() != cp.x_values() or e.y_values() != cp.y_values():
            continue
        if e.values() != cp.values():
            return e, cp
    pytest.skip("no differing same-shape map between the two images")


# -- where files open -------------------------------------------------------

def test_the_first_file_fills_the_blank_tab_the_next_gets_its_own(win):
    _need(LQ, KN)
    assert _labels(win) == ["Start"]
    assert win.open_path(LQ)
    assert win.tabs.count() == 1
    assert win.open_path(KN, new_tab=True)
    assert win.tabs.count() == 2
    assert win.current.primary.binfile.name == "03G906016KN.bin"


def test_a_file_already_open_just_brings_its_tab_forward(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    assert win.open_path(LQ)
    assert win.tabs.count() == 2, "one file must never be open in two tabs"
    assert win.current.primary.binfile.name == "03G906016LQ.bin"


def test_open_primary_uses_this_tab_by_default(win):
    _need(LQ, KN)
    win.open_path(LQ)
    assert win.open_path(KN)
    assert win.tabs.count() == 1
    assert win.current.primary.binfile.name == "03G906016KN.bin"


def test_a_read_from_a_module_opens_in_a_new_tab(win):
    _need(LQ)
    win.open_path(LQ)
    with open(LQ, "rb") as fh:
        data = fh.read()
    assert win.load_from_image(data, "ECU-read-test")
    assert win.tabs.count() == 2
    # a read is a new, untitled project: nothing on disk until it is saved
    assert win.current.project_ref is not None
    assert win.current.primary.binfile.name == "ECU-read-test - ECU"
    assert win.current.source_path() is None


def test_a_read_replaces_the_blank_startup_tab(win):
    _need(LQ)
    with open(LQ, "rb") as fh:
        data = fh.read()
    win.load_from_image(data, "ECU-read-test")
    assert win.tabs.count() == 1, "no empty tab left beside the read"


def test_closing_the_last_tab_leaves_an_empty_one(win):
    _need(LQ)
    win.open_path(LQ)
    assert win.close_tab()
    assert win.tabs.count() == 1 and win.current.is_empty()


# -- how tabs look ----------------------------------------------------------

def test_a_tab_says_what_it_holds_and_is_tinted_by_platform(win, G):
    _need(LQ, KN)
    win.open_path(LQ)
    ws = win.current
    assert _labels(win) == ["ECU  03G906016LQ.bin"]
    assert win.tabs.tabBar().tabData(0) == G.platform_badge("edc16")[2]
    assert "EDC16" in win.tabs.tabToolTip(0)
    # an edit shows as a dirty marker once the tab notices it
    e = _entity(ws, _differing_map_name(ws))
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    win._tick()
    assert _labels(win) == ["ECU  03G906016LQ.bin*"]


def _differing_map_name(ws):
    return next(e.name for e in ws.primary.entities
                if e.kind == "map" and e.rows > 1 and e.cols > 1)


def test_ecu_and_tcu_tints_differ(G):
    assert G.platform_badge("edc16")[0] == "ECU"
    assert G.platform_badge("zf6hp")[0] == "TCU"
    assert G.platform_badge("edc16")[2] != G.platform_badge("zf6hp")[2]


# -- comparisons across tabs ------------------------------------------------

def test_a_live_comparison_follows_its_tab_and_freezes_when_it_closes(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    assert kn.link_comparison(lq)
    assert kn.comparison is lq.primary and kn.cmp_link is lq
    assert "↔  03G906016LQ.bin" in _labels(win)[1]

    e, _cp = _differing_map(kn, lq)
    kn.show_entity(e)
    src = kn.primary.counterpart(lq.primary, e)
    before = kn.model.overlay.values[0][0]
    lq.primary.edit_cells(src, [(0, 0, src.cell(0, 0) + 5)])
    win._tick()
    after = kn.model.overlay.values[0][0]
    assert after != before, "the comparison did not follow the other tab"

    lq.primary.undo_stack.mark_clean()      # close it without the prompt
    win.close_tab(lq)
    assert kn.cmp_link is None and kn.cmp_frozen
    assert kn.comparison is not None, "the comparison stays as it last was"
    assert _labels(win)[0].endswith("(closed tab)")


def test_the_other_tab_opening_another_file_freezes_the_link(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    kn.link_comparison(lq)
    win.show_workspace(lq)
    assert lq._load_primary(LQ)         # a fresh load: a different session
    win._tick()
    assert kn.cmp_frozen and kn.cmp_link is None


def test_an_ecu_and_a_tcu_never_pair(win, G):
    _need(LQ, ZF)
    win.open_path(LQ)
    win.open_path(ZF, new_tab=True)
    ecu, tcu = win.workspaces()
    assert "same platform" in ecu.pair_problem(tcu.primary)
    assert not ecu.link_comparison(tcu)
    assert ecu.comparison is None


def test_compare_menu_lists_other_platforms_disabled(win):
    _need(LQ, KN, ZF)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    win.open_path(ZF, new_tab=True)
    win.show_workspace(win.workspaces()[0])
    menu = QtWidgets.QMenu()
    win.fill_compare_menu(menu)
    acts = {a.text(): a.isEnabled() for a in menu.actions()}
    assert acts["ECU  03G906016KN.bin"] is True
    zf = [t for t in acts if "5.0TFSI" in t]
    assert zf and acts[zf[0]] is False and "different platform" in zf[0]


# -- copying between tabs ---------------------------------------------------

def test_copy_from_tab_is_one_undoable_step(win, monkeypatch):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    e, cp = _differing_map(kn, lq)
    kn.show_entity(e)
    kn.table.clearSelection()
    n = kn.copy_from_tab(lq)
    assert n > 0
    assert e.values() == cp.values()
    kn.undo()
    assert e.values() != cp.values()


def test_copy_from_tab_can_be_limited_to_the_selection(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    e, cp = _differing_map(kn, lq)
    kn.show_entity(e)
    diffs = [(r, c) for r in range(e.rows) for c in range(e.cols)
             if e.cell(r, c) != cp.cell(r, c)]
    r, c = diffs[0]
    m = kn.model
    kn.table.clearSelection()
    ix = next(m.index(gr, gc) for gr in range(m.rowCount())
              for gc in range(m.columnCount()) if m.z_at(m.index(gr, gc)) == (r, c))
    kn.table.selectionModel().select(
        ix, QtCore.QItemSelectionModel.SelectionFlag.Select)
    assert kn.copy_from_tab(lq) == 1
    assert e.cell(r, c) == cp.cell(r, c)
    if len(diffs) > 1:
        r2, c2 = diffs[1]
        assert e.cell(r2, c2) != cp.cell(r2, c2), "copied outside the selection"


def test_copy_menu_explains_why_a_tab_cannot_be_used(win):
    _need(LQ, ZF)
    win.open_path(LQ)
    win.open_path(ZF, new_tab=True)
    ecu = win.workspaces()[0]
    win.show_workspace(ecu)
    ecu.show_entity(ecu.primary.entities[0])
    menu = QtWidgets.QMenu()
    win.fill_copy_from_menu(menu, ecu)
    (a,) = menu.actions()
    assert not a.isEnabled() and "different platform" in a.text()


# -- tool windows belong to their tab ---------------------------------------

def test_tool_windows_hide_with_their_tab_and_close_with_it(win):
    _need(LQ, KN)
    win.open_path(LQ)
    lq = win.current
    lq.open_hex()
    dlg = lq.tool("hex")
    assert dlg is not None and dlg.isVisible()
    assert not dlg.isModal(), "the grid must stay usable beside it"
    win.open_path(KN, new_tab=True)
    assert not dlg.isVisible(), "hidden while another tab is active"
    win.show_workspace(lq)
    assert dlg.isVisible(), "back when its tab is"
    win.close_tab(lq)
    assert lq.tool("hex") is None


def test_one_tool_window_of_a_kind_per_tab(win):
    _need(LQ)
    win.open_path(LQ)
    ws = win.current
    ws.open_hex()
    first = ws.tool("hex")
    ws.open_hex()
    assert ws.tool("hex") is first


def test_the_grid_follows_an_edit_made_elsewhere(win):
    _need(LQ)
    win.open_path(LQ)
    ws = win.current
    e = _entity(ws, _differing_map_name(ws))
    ws.show_entity(e)
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 3)])   # e.g. a tool window
    win._tick()
    assert ws.model._vals[0][0] == e.cell(0, 0)


# -- closing and reopening --------------------------------------------------

def test_closing_the_window_asks_about_unsaved_tabs(win, G, monkeypatch):
    _need(LQ)
    win.open_path(LQ)
    ws = win.current
    e = _entity(ws, _differing_map_name(ws))
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    asked = []

    def cancel(self):
        asked.append([w.display_name() for w in self.selected()])
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(G.UnsavedDialog, "exec", cancel)
    assert not win.close()
    assert asked == [["03G906016LQ.bin"]]
    assert win.isVisible()


def test_tabs_are_remembered_and_reopened_with_their_links(win, G):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    kn.link_comparison(lq)
    e = _entity(kn, _differing_map_name(kn))
    kn.show_entity(e)
    saved = win.tab_session()
    assert [os.path.basename(s["primary"]) for s in saved] == [
        "03G906016LQ.bin", "03G906016KN.bin"]
    assert saved[1]["link"] == 0 and saved[1]["active"]

    fresh = G.MainWindow()
    try:
        made = fresh.restore_tabs(saved, [0, 1])
        assert len(made) == 2 and fresh.tabs.count() == 2
        lq2, kn2 = fresh.workspaces()
        assert kn2.cmp_link is lq2, "the live link comes back"
        assert kn2.model.entity is not None and kn2.model.entity.name == e.name
        assert fresh.current is kn2
    finally:
        fresh.close_discarding()


def test_only_the_ticked_tabs_are_reopened(win, G):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    saved = win.tab_session()
    fresh = G.MainWindow()
    try:
        fresh.restore_tabs(saved, [1])
        assert _labels(fresh) == ["ECU  03G906016KN.bin"]
    finally:
        fresh.close_discarding()


def test_the_restore_list_cannot_tick_a_missing_file(G, app):
    dlg = G.RestoreProjectsDialog([
        {"primary": "/nowhere/gone.bin", "platform": "edc16", "label": "gone"},
        {"primary": None, "platform": "edc16", "label": "a read"},
    ])
    assert dlg.chosen() == []
    for i in range(dlg.list.count()):
        assert not dlg.list.item(i).flags() & QtCore.Qt.ItemFlag.ItemIsEnabled


# -- a new primary and the tab's comparison ---------------------------------

Q = os.path.join(BINARIES, "038906016Q.Bin")


def test_a_new_primary_keeps_the_comparison_of_its_platform(win):
    _need(LQ, KN, Q)
    win.open_path(LQ)
    ws = win.current
    assert ws._load_comparison(KN)
    comp = ws.comparison
    assert win.open_path(Q)
    assert ws.primary.binfile.name == "038906016Q.Bin"
    assert ws.comparison is comp, "the comparison was dropped"
    assert _labels(win)[0].endswith("↔  03G906016KN.bin")
    e = next(e for e in ws.primary.entities
             if ws.primary.counterpart(comp, e) is not None
             and e.kind == "map")
    ws.show_entity(e)
    assert ws.model.overlay is not None, "not laid over the new primary"


def test_a_new_primary_keeps_a_live_link(win):
    _need(LQ, KN, Q)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    kn.link_comparison(lq)
    assert win.open_path(Q)
    assert kn.primary.binfile.name == "038906016Q.Bin"
    assert kn.cmp_link is lq and kn.comparison is lq.primary


def test_a_new_primary_of_another_platform_drops_the_comparison(win):
    _need(LQ, KN, ZF)
    win.open_path(LQ)
    ws = win.current
    ws._load_comparison(KN)
    assert win.open_path(ZF)
    assert ws.primary.platform == "zf6hp"
    assert ws.comparison is None and ws.cmp_link is None
    assert not ws.diff_only.isEnabled()
    assert "03G906016KN.bin closed" in win.statusBar().currentMessage()
    assert "↔" not in _labels(win)[0]



# -- open in this tab / new tab ----------------------------------------------

def test_open_primary_in_new_tab_leaves_this_one_alone(win):
    _need(LQ, KN)
    win.open_path(LQ)
    assert win.open_path(KN, new_tab=True)
    assert [w.display_name() for w in win.workspaces()] == [
        "03G906016LQ.bin", "03G906016KN.bin"]


def test_the_plus_button_opens_the_import_menu(win, monkeypatch):
    _need(LQ)
    win.open_path(LQ)
    shown = []
    monkeypatch.setattr(type(win.import_menu), "popup",
                        lambda self, pos, *a: shown.append(self))
    win._plus_btn.click()
    assert shown == [win.import_menu] and win.tabs.count() == 1


def test_the_tab_list_offers_every_tab(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    menu = win._tab_list_btn.menu()
    win._fill_tab_list(menu)
    acts = menu.actions()
    assert [a.isChecked() for a in acts] == [False, True]
    acts[0].trigger()
    assert win.tabs.currentIndex() == 0


def test_replacing_an_edited_file_can_be_cancelled(win, monkeypatch):
    _need(LQ, KN)
    win.open_path(LQ)
    ws = win.current
    e = _entity(ws, _differing_map_name(ws))
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec",
                        lambda self: QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not win.open_path(KN)
    assert ws.primary.binfile.name == "03G906016LQ.bin" and ws.is_dirty()


# -- restore tab from history -------------------------------------------------

_MENUS = []          # a QMenu that is collected takes its actions with it


def _history_menu(win):
    menu = QtWidgets.QMenu()
    _MENUS.append(menu)
    win.fill_history_menu(menu)
    return [a for a in menu.actions() if not a.isSeparator()]


def test_a_closed_tab_goes_into_the_history(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    win.close_tab(win.workspaces()[0])
    (ent,) = win.closed_history()
    assert os.path.basename(ent["primary"]) == "03G906016LQ.bin"
    acts = _history_menu(win)
    assert acts[0].text().startswith("ECU  03G906016LQ.bin")
    assert acts[-1].text() == "Clear history"


def test_a_file_opened_over_goes_into_the_history(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN)                       # replaces LQ in this tab
    assert [os.path.basename(d["primary"]) for d in win.closed_history()] \
        == ["03G906016LQ.bin"]


def test_restoring_brings_back_the_comparison_and_the_parameter(win):
    _need(LQ, KN)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    lq, kn = win.workspaces()
    kn.link_comparison(lq)
    e = _entity(kn, _differing_map_name(kn))
    kn.show_entity(e)
    win.close_tab(kn)
    ws = win.restore_closed(win.closed_history()[0])
    assert ws.display_name() == "03G906016KN.bin"
    assert ws.cmp_link is lq, "re-linked to the tab holding that file"
    assert ws.model.entity is not None and ws.model.entity.name == e.name
    assert win.closed_history() == [], "a restored tab leaves the history"


def test_reopen_last_closed_takes_the_newest(win):
    _need(LQ, KN, Q)
    win.open_path(LQ)
    win.open_path(KN, new_tab=True)
    win.open_path(Q, new_tab=True)
    win.close_tab(win.workspaces()[1])      # KN
    win.close_tab(win.workspaces()[1])      # Q, newest
    ws = win.reopen_last_closed()
    assert ws.display_name() == "038906016Q.Bin"
    assert [os.path.basename(d["primary"]) for d in win.closed_history()] \
        == ["03G906016KN.bin"]


def test_history_keeps_one_entry_per_file_newest_first_and_is_capped(win, G):
    for i in range(G.CLOSED_MAX + 5):
        win.push_closed({"primary": "/x/f%d.bin" % i, "label": str(i)})
    win.push_closed({"primary": "/x/f3.bin", "label": "3 again"})
    items = win.closed_history()
    assert len(items) == G.CLOSED_MAX
    assert items[0]["label"] == "3 again"
    assert sum(1 for d in items if d["primary"] == "/x/f3.bin") == 1


def test_a_missing_file_is_listed_but_cannot_be_picked(win):
    win.push_closed({"primary": "/nowhere/gone.bin", "label": "gone",
                     "platform": "edc16"})
    (a,) = _history_menu(win)[:1]
    assert not a.isEnabled() and "file not found" in a.text()


def test_restoring_into_a_blank_tab_uses_it(win):
    _need(LQ)
    win.open_path(LQ)
    win.close_tab()                         # the window keeps one blank tab
    assert win.tabs.count() == 1 and win.current.is_empty()
    win.restore_closed(win.closed_history()[0])
    assert win.tabs.count() == 1
    assert win.current.display_name() == "03G906016LQ.bin"


# -- menus --------------------------------------------------------------------

def test_exports_live_in_one_submenu(win):
    _need(LQ)
    file_menu = win.menuBar().actions()[0].menu()
    top = [a.text() for a in file_menu.actions()]
    assert not [t for t in top if t.startswith("Export ")], top
    assert "&Export" in top
    names = [a.text() for a in win.export_menu.actions() if not a.isSeparator()]
    assert "Definition as XDF..." in names and "Binary (.bin)..." in names
    assert "Data log..." in names and "Patch..." in names
    assert not win.export_menu.menuAction().isEnabled(), "nothing loaded yet"
    win.open_path(LQ)
    assert win.export_menu.menuAction().isEnabled()


# -- the "*" after saving -------------------------------------------------------

def _edit_one(ws):
    e = next(e for e in ws.primary.entities
             if e.kind == "map" and not getattr(e, "text_kind", ""))
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])


@pytest.mark.parametrize("src", [LQ, ZF], ids=["edc16", "zf6hp"])
def test_saving_clears_the_star(win, tmp_path, src, monkeypatch):
    """A ZF 6HP save raised after writing (its checksum report has no
    ``slots``) and never redrew the tab, so the "*" stayed. (A plain file's
    first save makes it a project.)"""
    _need(src)
    import shutil
    from ovtsuite import gui_project
    monkeypatch.setattr(gui_project, "_ask_path",
                        lambda *_a: str(tmp_path / "saved.ovt"))
    path = str(tmp_path / os.path.basename(src))
    shutil.copy(src, path)
    win.open_path(path)
    ws = win.current
    _edit_one(ws)
    win._tick()
    assert _labels(win)[0].endswith("*")
    assert ws.save()
    assert not _labels(win)[0].endswith("*")
    assert "Saved" in win.statusBar().currentMessage()


def test_the_star_follows_the_saved_state_whoever_saves(win):
    """A save made anywhere marks the file clean without changing its bytes;
    the tab must notice that too, not only edits."""
    _need(LQ)
    win.open_path(LQ)
    ws = win.current
    _edit_one(ws)
    win._tick()
    assert _labels(win)[0].endswith("*")
    ws.primary.undo_stack.mark_clean()      # e.g. a save from another dialog
    win._tick()
    assert not _labels(win)[0].endswith("*")


# -- where File > Open starts ---------------------------------------------------

def test_only_a_picked_file_moves_the_open_folder(win, G, tmp_path, monkeypatch):
    """Bear: File > Open kept starting in a folder he had not opened in
    days. Reopening tabs at startup, Open Recent and the tab history all went
    through open_path, which recorded each file's folder -- so the tabs
    reopened at launch decided where Open started, not the last pick."""
    _need(LQ, KN)
    from ovtsuite import recent
    # recent.py keeps its own JSON settings: never the user's real ones here
    monkeypatch.setattr(recent, "_config_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(recent, "_legacy_dirs", lambda: [])
    picked = tmp_path / "picked"
    picked.mkdir()
    import shutil
    pick = str(picked / "pick.bin")
    shutil.copy(KN, pick)
    recent.set_last_dir(pick)                       # the last file picked
    win.open_path(LQ)                               # e.g. Reopen tabs / Recent
    win.restore_closed({"primary": KN, "platform": "edc16"})
    assert recent.get_last_dir() == str(picked), \
        "a file opened without the picker moved the picker's folder"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    chosen = str(elsewhere / "x.bin")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (chosen, "")))
    assert win._ask_path("Open primary binary") == chosen
    assert recent.get_last_dir() == str(elsewhere), "a pick must move it"


def test_reopening_a_tab_with_its_comparison_leaves_the_open_folder(
        win, tmp_path, monkeypatch):
    """The case Bear hit: every launch, Reopen tabs re-loaded his 6HP tab's
    comparison from a folder he had not picked from in days, and loading a
    comparison recorded its folder -- so File > Open started there."""
    _need(LQ, KN)
    import shutil
    from ovtsuite import recent
    monkeypatch.setattr(recent, "_config_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(recent, "_legacy_dirs", lambda: [])
    old = tmp_path / "old-comparisons"
    old.mkdir()
    cmp_path = str(old / "comparison.bin")
    shutil.copy(KN, cmp_path)
    picked = tmp_path / "picked"
    picked.mkdir()
    recent.set_last_dir(str(picked))
    win.restore_tabs([{"primary": LQ, "platform": "edc16",
                       "comparison": cmp_path, "active": True}], [0])
    assert win.current.comparison is not None, "comparison not reopened"
    assert recent.get_last_dir() == str(picked)


def test_tabs_are_painted_between_arrows_at_both_ends(win):
    """Windows 11 puts one scroll arrow at each end of the tab bar: keeping
    the tabs out of the arrows must not blank everything between them."""
    from PySide6 import QtGui
    for i in range(12):
        win._add_workspace()
    for i in range(win.tabs.count()):
        win.tabs.setTabText(i, "ECU  Project %d > ECU - Stage 1" % i)
    win.resize(700, 400)
    QtWidgets.QApplication.processEvents()
    bar = win.tabs.tabBar()
    arrows = [b for b in bar.findChildren(QtWidgets.QToolButton)
              if b.isVisible() and b.parent() is bar]
    if len(arrows) < 2:
        pytest.skip("this style shows no scroll arrows")
    arrows[0].move(0, arrows[0].y())
    reg = bar._arrow_region()
    covered = sum(r.width() for r in reg)
    assert covered < bar.width() / 3
    assert not reg.contains(QtCore.QPoint(bar.width() // 2, bar.height() // 2))
