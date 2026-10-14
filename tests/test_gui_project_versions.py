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
