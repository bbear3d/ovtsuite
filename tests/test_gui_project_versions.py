# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A project's versions in the Project panel (gui_project): renaming one,
with synthetic binaries -- no reference material needed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.project import autosave as _as  # noqa: E402

pytestmark = pytest.mark.slow          # the main window

READ = bytes(range(256)) * 64                   # 16 KB, no platform


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(gui.Workspace, "_explain_limited_support",
                        lambda self, *a, **k: None)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


@pytest.fixture
def tab(win):
    """A read in a new project, with one saved edit on top of it."""
    from ovtsuite import gui_project as gp
    assert win.load_from_image(READ, "read")
    ws = win.current
    ws.primary.edit_bytes(0x10, b"\x55")
    assert gp._commit(ws)
    return ws


def test_renaming_a_version_reaches_version_history(win, tab):
    from ovtsuite import changelog as _cl
    from ovtsuite import gui_project as gp
    from ovtsuite import history as _h
    ref = tab.project_ref
    cid = ref.branch.head
    auto = ref.set.checkpoint(cid).label
    gp.rename_version(win, ref.doc, ref.sid, cid, "Stage 1")
    assert ref.set.checkpoint(cid).label == "Stage 1"
    assert "Stage 1" in _cl.read_text(ref.workfile)
    names = {v["id"]: _h.display_label(v) for v in _h.list_versions(tab.primary)}
    assert names[cid] == "Stage 1"
    # cleared in Version history: back to its automatic name in the project
    _h.set_label(tab.primary, cid, "")
    gp.after_history_dialog(tab)
    assert ref.set.checkpoint(cid).label == auto
    # and an empty name from the panel does the same
    gp.rename_version(win, ref.doc, ref.sid, cid, "Stage 2")
    gp.rename_version(win, ref.doc, ref.sid, cid, "")
    assert ref.set.checkpoint(cid).label == auto


def test_renaming_the_compared_version_renames_the_comparison(win, tab):
    from ovtsuite import gui_project as gp
    ref = tab.project_ref
    cid = ref.branch.head
    assert gp.compare_with_checkpoint(tab, cid, quiet=True)
    gp.rename_version(win, ref.doc, ref.sid, cid, "Stage 1")
    assert tab.comparison.version_of["label"] == "Stage 1"


def test_the_panel_offers_rename_on_a_version(win, tab, monkeypatch):
    from ovtsuite import gui_project as gp
    panel = gp.panel(win)
    panel.refresh()
    ref = tab.project_ref
    cid = ref.branch.head
    item = None
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value() is not None:
        data = it.value().data(0, gp._ROLE)
        if data and data[0] == "ck" and data[4] == cid:
            item = it.value()
        it += 1
    assert item is not None
    panel.tree.setCurrentItem(item)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Via F2", True)))
    panel._f2()
    assert ref.set.checkpoint(cid).label == "Via F2"


# -- export / open any version ------------------------------------------------

def test_export_a_version_writes_that_versions_bytes(win, tab, monkeypatch,
                                                    tmp_path):
    from ovtsuite import gui_project as gp
    ref = tab.project_ref
    root = ref.set.roots()[0].id
    out = str(tmp_path / "v.bin")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (out, "")))
    assert gp.export_version(win, ref.doc, ref.sid, root) == out
    assert open(out, "rb").read() == READ
    assert gp.export_version(win, ref.doc, ref.sid, ref.branch.head) == out
    data = open(out, "rb").read()
    assert data != READ and data[0x10] == 0x55


def test_open_a_version_read_only(win, tab, monkeypatch):
    from ovtsuite import gui_project as gp
    from ovtsuite import gui_viewonly as vo
    from ovtsuite import recent
    added = []
    monkeypatch.setattr(recent, "add_recent", lambda p: added.append(p))
    ref = tab.project_ref
    root = ref.set.roots()[0].id
    ws = vo.open_version(win, ref.doc, ref.sid, ref.bid, root)
    assert ws is not None and ws is win.current and ws is not tab
    assert vo.is_view_only(ws) and ws.project_ref is None
    assert ws.display_name().endswith("(read-only)")
    assert "@ as read" in ws.display_name()
    assert bytes(ws.primary.binfile.data) == READ
    assert ws.model.readonly
    assert added == [] and ws.source_path() is None
    # nothing to save, nothing asked on close
    ws.primary.edit_bytes(0, b"\x01")
    assert not ws.is_dirty()
    assert ws.save() is False and ws.save_as() is False
    assert gp.docs(win)[0].tabs(win) == [tab]       # still one project tab
    # the editing actions are off while it is in front, back on after
    save = next(a for a in win.actions() if vo._text(a) == "Save")
    win._refresh_enabled()
    assert not save.isEnabled()
    win.show_workspace(tab)
    win._refresh_enabled()
    assert save.isEnabled()
    # closing it asks nothing, and leaves nothing to reopen
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec",
                        lambda *a: pytest.fail("asked on close"))
    assert win.close_tab(ws)
    assert win.closed_entry(ws) is None


# -- kept files newest first ----------------------------------------------------

def test_kept_files_are_listed_newest_first(win, tab, monkeypatch):
    from ovtsuite import gui_project as gp
    proj = tab.project_ref.doc.project
    for n in ("a.csv", "b.csv", "c.csv"):
        proj.add_log(n, b"t,rpm\n0,800\n", set_id=tab.project_ref.sid)
    proj.logs[0].added = "2026-01-02T00:00:00"      # a, then b and c tied
    proj.logs[1].added = proj.logs[2].added = "2026-01-01T00:00:00"
    stored = [a.filename for a in proj.logs]
    assert [a.filename for a in gp.newest_first(proj.logs)] == \
        ["a.csv", "c.csv", "b.csv"]
    assert [a.filename for a in proj.logs] == stored     # never reordered
    panel = gp.panel(win)
    panel.refresh()
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    shown = []
    while it.value() is not None:
        data = it.value().data(0, gp._ROLE)
        if data and data[0] == "log":
            shown.append(next(a.filename for a in proj.logs if a.id == data[2]))
        it += 1
    assert shown == ["a.csv", "c.csv", "b.csv"]
    # File > Export > Data log offers the newest first, and picks it by default
    offered = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", staticmethod(
        lambda *a, **k: offered.append((a[3], a[4])) or ("", False)))
    gp.export_kept(win, "log")
    names, default = offered[0]
    assert names[0].startswith("a.csv") and default == 0


def test_a_new_project_compares_with_nothing_until_its_first_save(win):
    """A project that is still only its original has nothing to compare: the
    tab compared the original with itself (and a coding-copy pairing bug made
    a stock Touran 03G906021MP show 116 changed tables). The first save brings
    the comparison with the original."""
    from ovtsuite import gui_project as gp
    assert win.load_from_image(READ, "read")
    ws = win.current
    assert ws.project_ref is not None
    assert ws.comparison is None
    ws.primary.edit_bytes(0x10, b"\x55")
    assert gp._commit(ws)
    ref = ws.project_ref
    root = ref.set.root_of(ref.branch.head)
    assert ws.comparison is not None
    assert ws.comparison.version_of["id"] == root.id != ref.branch.head
