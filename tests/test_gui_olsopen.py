# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A WinOLS project opened as stored shows its own maps INSTEAD of the module's:
the module's family search and parameter tree are not built (they were built
and thrown away, seconds on every open), and File > Load the module's
built-in maps builds them afterwards."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from conftest import BINARIES  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")      # an EDC16 image a module defines


@pytest.fixture
def G(isolated_qsettings, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: 0))
    return gui


def _ols(tmp_path, image):
    from test_definitions import make_ols
    p = tmp_path / "car.ols"
    p.write_bytes(make_ols(597, image=image))
    return str(p)


def _image():
    path = os.environ.get("OVT_TEST_EDC16_IMAGE") or LQ
    if not os.path.exists(path):
        pytest.skip("reference binary missing: %s" % path)
    with open(path, "rb") as fh:
        return fh.read()


def test_a_stored_winols_project_opens_without_building_the_modules_tree(G, tmp_path, monkeypatch):
    from ovtsuite import options as _opt
    from ovtsuite import session as S
    assert _opt.get("ols/open") == "stored"            # the default
    calls = []
    real = S.TunerSession.__init__

    def spy(self, path, *a, **k):
        calls.append(k.get("definition_only", False))
        return real(self, path, *a, **k)
    monkeypatch.setattr(S.TunerSession, "__init__", spy)
    win = G.MainWindow()
    try:
        win.clear_closed()
        assert win.open_path(_ols(tmp_path, _image()))
        ws = next(w for w in win.workspaces() if w.project_ref is not None)
        assert True in calls                         # the module's tree was not asked for
        assert ws.primary.support == "definition"
        assert len(ws.primary.entities) < 50         # the OLS maps, not ~10,000
        from ovtsuite import gui_project as gp
        from ovtsuite import gui_paramenu as pm
        assert gp.builtin_maps_hidden(win, ws)
        acts = [a.text() for a in pm.build_menu(ws, None).actions()]
        assert "Load the module's built-in maps" in acts
        # and the checksum support is still the module's
        assert ws.primary.checksum_status() is not None
        # the tree's right-click Load the module's built-in maps: the module builds its tree
        assert gp.load_builtin_maps(win, ws)
        ws = next(w for w in win.workspaces() if w.project_ref is not None)
        assert ws.primary.support == "full" and len(ws.primary.entities) > 1000
        assert not gp.builtin_maps_hidden(win, ws)
        assert "Load the module's built-in maps" not in [
            a.text() for a in pm.build_menu(ws, None).actions()]
    finally:
        win.close_discarding()


def test_the_trees_menu_offers_it_only_when_the_maps_are_hidden_and_runs_it(G, monkeypatch):
    from PySide6 import QtWidgets
    from ovtsuite import gui_paramenu as pm
    from ovtsuite import gui_project as gp
    called = []
    monkeypatch.setattr(gp, "load_builtin_maps", lambda win, ws: called.append(ws) or True)
    ws = type("WS", (), {})()
    ws.primary, ws.win, ws.view_only, ws.comparison = object(), object(), None, None
    ws.tree = QtWidgets.QTreeWidget()
    monkeypatch.setattr(gp, "builtin_maps_hidden", lambda win, w: False)
    assert "Load the module's built-in maps" not in [
        a.text() for a in pm.build_menu(ws, None).actions()]
    monkeypatch.setattr(gp, "builtin_maps_hidden", lambda win, w: True)
    act = next(a for a in pm.build_menu(ws, None).actions()
               if a.text() == "Load the module's built-in maps")
    assert act.isEnabled()
    act.trigger()
    assert called == [ws]
    # not in a read-only tab
    ws.view_only = True
    act = next(a for a in pm.build_menu(ws, None).actions()
               if a.text() == "Load the module's built-in maps")
    assert not act.isEnabled()
