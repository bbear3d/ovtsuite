# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
﻿# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch files dragged into a project (a menu: keep / apply / both) and the
project's kept files dragged out (ovtsuite.gui_patchdrop)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402
from ovtsuite import gui_patchdrop as pd  # noqa: E402
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


def _patch_json():
    return json.dumps({"version": 1, "created": "", "source_family": "edc16",
                       "entries": [], "blocks": []})


@pytest.fixture
def patch_file(tmp_path):
    p = tmp_path / "boost.patch"
    p.write_text(_patch_json(), encoding="utf-8")
    return str(p)


# ------------------------------------------------------------ recognising
def test_a_patch_is_recognised_by_extension_or_shape(tmp_path, patch_file):
    assert pd.is_patch_file(patch_file)
    odd = tmp_path / "boost.json"
    odd.write_text(_patch_json(), encoding="utf-8")
    assert pd.is_patch_file(str(odd))
    other = tmp_path / "data.json"
    other.write_text('{"a": 1}', encoding="utf-8")
    assert not pd.is_patch_file(str(other))
    binf = tmp_path / "x.bin"
    binf.write_bytes(b"\x00\x01" * 100)
    assert not pd.is_patch_file(str(binf))
    assert not pd.is_patch_file(str(tmp_path / "missing.patch"))


def test_split_keeps_order(tmp_path, patch_file):
    b = tmp_path / "a.bin"
    b.write_bytes(b"\xff" * 8)
    assert pd.split_patches([str(b), patch_file]) == ([patch_file], [str(b)])


def test_an_unreadable_patch_is_a_plain_error(tmp_path):
    p = tmp_path / "bad.patch"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        pd.read_patch(str(p))


# ------------------------------------------------------------- dragging out
def test_a_kept_file_is_written_for_a_drag_out_and_not_taken_back(tmp_path):
    from ovtsuite import gui_files as gf
    from ovtsuite import gui_project as gp
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class _Doc:
        class project:
            patches = [type("A", (), {"id": "p1", "filename": "boost.patch"})()]
            logs = attachments = []

            log_bytes = attachment_bytes = None

            @staticmethod
            def patch_bytes(pid):
                return b'{"entries": []}'
    tree = pd.DragTree()
    it = QtWidgets.QTreeWidgetItem(["boost"])
    it.setData(0, gp._ROLE, ("patch", _Doc, "p1"))
    tree.addTopLevelItem(it)
    other = QtWidgets.QTreeWidgetItem(["a set"])
    other.setData(0, gp._ROLE, ("set", _Doc, "s"))
    assert pd.drag_mime([other]) is None
    mime = pd.drag_mime([it])
    (url,) = mime.urls()
    path = url.toLocalFile()
    assert os.path.basename(path) == "boost.patch"
    with open(path, "rb") as fh:
        assert fh.read() == b'{"entries": []}'
    assert pd.is_own_drag(path)
    assert gf.drop_paths(mime) == []            # dropped back: ignored
    assert app is not None


# ---------------------------------------------------------------- the drop
@pytest.fixture
def win(app_window):
    yield app_window


@pytest.fixture
def app_window(tmp_path, monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    w = gui.MainWindow()
    w.clear_closed()
    w.show()
    from ovtsuite import gui_files as gf
    assert gf.import_files(w, [LQ], target="new")
    yield w
    w.close_discarding()
    assert app is not None


def _choose(monkeypatch, prefix):
    seen = []

    def fake(self, *a, **k):
        seen.extend(x.text() for x in self.actions())
        for x in self.actions():
            if x.text().startswith(prefix):
                return x
        return None
    monkeypatch.setattr(pd, "_exec_menu", lambda menu, pos: fake(menu))
    return seen


def test_dropping_a_patch_keeps_it_in_the_project(win, patch_file, monkeypatch):
    from ovtsuite import gui_files as gf
    seen = _choose(monkeypatch, "Keep in")
    applied = []
    monkeypatch.setattr(pd, "apply_to", lambda *a: applied.append(a))
    assert gf.handle_drop(win, [patch_file])
    doc = win.current.project_ref.doc
    assert [a.filename for a in doc.project.patches] == ["boost.patch"]
    assert doc.project.patches[0].set == win.current.project_ref.sid
    assert not applied
    assert any(t.startswith("Keep in") and "apply" in t for t in seen)
    assert "Apply to this tab" in seen


def test_keep_and_apply_does_both(win, patch_file, monkeypatch):
    from ovtsuite import gui_files as gf
    _choose(monkeypatch, "Keep in")
    # the second "Keep in ..." entry is the combined one
    monkeypatch.setattr(pd, "_exec_menu", lambda menu, pos: next(
        x for x in menu.actions() if "and apply" in x.text()))
    applied = []
    monkeypatch.setattr(pd, "apply_to", lambda ws, pt, name: applied.append(name))
    assert gf.handle_drop(win, [patch_file])
    assert applied == ["boost.patch"]
    assert len(win.current.project_ref.doc.project.patches) == 1


def test_apply_only_keeps_nothing(win, patch_file, monkeypatch):
    from ovtsuite import gui_files as gf
    _choose(monkeypatch, "Apply to this tab")
    applied = []
    monkeypatch.setattr(pd, "apply_to", lambda ws, pt, name: applied.append(name))
    assert gf.handle_drop(win, [patch_file])
    assert applied == ["boost.patch"]
    assert win.current.project_ref.doc.project.patches == []


def test_a_cancelled_menu_does_nothing_and_a_binary_still_imports(
        win, patch_file, monkeypatch):
    from ovtsuite import gui_files as gf
    monkeypatch.setattr(pd, "_exec_menu", lambda menu, pos: None)
    assert not gf.handle_drop(win, [patch_file])
    assert win.current.project_ref.doc.project.patches == []
