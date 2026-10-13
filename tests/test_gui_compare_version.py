# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Compare with saved version: one of the primary's own version-history
checkpoints as the tab's comparison.

Driven offscreen over a real LQ image saved a few times.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402
from ovtsuite import history as H  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
IDLE = "LIGov_nSetpointWarm_CUR"


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def ws(G, tmp_path):
    """A tab on a copy of the LQ saved twice: +10 rpm idle, then +20."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    path = str(tmp_path / "branch.bin")
    shutil.copy(LQ, path)
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    assert w.open_path(path)
    t = w.current
    e = next(e for e in t.primary.entities if e.name == IDLE)
    for label, d in (("idle +10", 10), ("idle +20", 20)):
        t.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + d)])
        t.primary.save(label=label, backup=False)
    yield t
    w.close_discarding()


def _idle(sess):
    return next(e for e in sess.entities if e.name == IDLE).cell(0, 0)


def test_a_saved_version_becomes_the_comparison(ws):
    first = [v for v in H.list_versions(ws.primary) if v["label"] == "idle +10"][0]
    assert ws.compare_with_version(first["id"])
    assert ws.comparison is not None and ws.cmp_link is None
    assert ws.cmp_version["id"] == first["id"]
    assert ws.comparison.binfile.name.endswith("@ idle +10")
    assert _idle(ws.primary) - _idle(ws.comparison) == pytest.approx(20)
    assert ws.diff_only.isEnabled()
    assert ws.comparison_path() is None          # no bogus temp path remembered
    ws.close_comparison()
    assert ws.cmp_version is None


def test_the_file_menu_lists_the_versions_newest_first(ws):
    win = ws.win
    menu = win.compare_version_menu
    win.fill_version_menu(menu)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert "idle +20" in texts[0] and "(last save)" in texts[0]
    assert "idle +10" in texts[1]
    assert texts[-1].startswith("Version history")
    menu.actions()[1].trigger()
    assert ws.cmp_version["label"] == "idle +10"
    win.fill_version_menu(menu)
    assert menu.actions()[1].isChecked()


def test_the_version_history_window_can_pick_the_comparison(ws, G):
    dlg = G.VersionHistoryDialog(ws.primary, ws, on_compare=ws.compare_with_version)
    assert dlg.compare_btn.isVisible() or not dlg.isVisible()
    dlg.tbl.selectRow(1)
    dlg._compare()
    assert ws.cmp_version["label"] == "idle +10"
    plain = G.VersionHistoryDialog(ws.primary, ws)
    assert plain.compare_btn.isHidden()


def test_a_remembered_tab_comes_back_comparing_the_same_version(ws, G):
    first = [v for v in H.list_versions(ws.primary) if v["label"] == "idle +10"][0]
    ws.compare_with_version(first["id"])
    saved = ws.win.tab_session()
    assert saved[0]["cmp_version"] == first["id"] and saved[0]["comparison"] is None
    fresh = G.MainWindow()
    try:
        ws.win.close_discarding()
        (t,) = fresh.restore_tabs(saved, [0])
        assert t.cmp_version is not None and t.cmp_version["id"] == first["id"]
    finally:
        fresh.close_discarding()
