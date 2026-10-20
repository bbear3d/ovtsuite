# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Duplicate as user parameter (ovtsuite.userparams.from_entity): a copy of
a detected parameter that reads exactly what it reads, kept with the
project, standing in for the original -- which is hidden from the tree and
from searches until the copy lets it go."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import BINARIES  # noqa: E402
from ovtsuite import userparams as up  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
MAP = "AccPed_trqEngLim_MAP"


@pytest.fixture(scope="module")
def session():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    from ovtsuite.session import TunerSession
    return TunerSession(LQ)


def _cells(e):
    return [e.cell(r, c) for r in range(e.rows) for c in range(e.cols)]


def test_the_copy_reads_what_the_original_reads(session):
    e = next(x for x in session.entities if x.name == MAP)
    spec = up.from_entity(e, session)
    assert spec["name"] == MAP + "_edit" and spec["hide"]
    assert spec["replaces"] == {"name": MAP, "group": ""}
    assert not up.validate(spec, len(session.binfile.data),
                           {x.name for x in session.entities})
    try:
        assert up.apply(session, [spec]) == 1
        dup = up.find(session, spec["name"])
        assert _cells(dup) == pytest.approx(_cells(e))
        assert dup.x_values() == e.x_values() and dup.y_values() == e.y_values()
        # standing in for it: the original hidden, the copy in its place
        assert up.hidden(e) and not up.hidden(dup)
        assert (dup.tier, dup.category_path) == (e.tier, e.category_path)
        from ovtsuite.search import SearchEngine
        found = SearchEngine.entities_of(session)
        assert e not in found and dup in found
        # neither the copy nor the hiding goes into the stored definition:
        # the copy is added on open, the mark is not a field a snapshot keeps
        import dataclasses
        from ovtsuite import defn
        from ovtsuite.model import Entity
        assert dup.source in defn._ADDED_ON_OPEN
        assert "_hidden_by" not in {f.name for f in dataclasses.fields(Entity)}
        # hide off, or the copy gone: the original is back
        up.apply(session, [dict(spec, hide=False)])
        assert not up.hidden(e)
        up.apply(session, [spec])
        assert up.hidden(e)
    finally:
        up.apply(session, [])
    assert not up.hidden(e)


def test_kept_in_the_project_and_applied_on_open(session, tmp_path):
    from ovtsuite import project as ovt
    proj, _s = ovt.import_file(LQ)
    sid = proj.sets[0].id
    e = next(x for x in session.entities if x.name == MAP)
    proj.set_user_params(sid, [up.from_entity(e, session)])
    path = str(tmp_path / "p.ovt")
    proj.save(path)
    p2 = ovt.Project.load(path)
    try:
        assert up.apply_from_project(p2, sid, session) == ""
        assert up.hidden(e) and up.find(session, MAP + "_edit") is not None
    finally:
        up.apply(session, [])


@pytest.mark.slow
def test_duplicate_from_the_tree_menu(tmp_path, isolated_qsettings, monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui, gui_paramenu as pm
    from ovtsuite.session import TunerSession
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    w = gui.MainWindow(TunerSession(LQ))
    w.show()
    try:
        ws = w.current
        e = next(x for x in ws.primary.entities if x.name == MAP)
        menu = QtWidgets.QMenu()
        pm._entity_actions(ws, menu, e, False)
        assert "Duplicate as user parameter..." in [a.text() for a in menu.actions()]
        seen = {}

        def accept(dlg):
            seen["title"] = dlg.windowTitle()
            seen["hide"] = (dlg.hide_orig.isVisible() or not dlg.isVisible(),
                            dlg.hide_orig.isChecked())
            dlg.title.setText("My pedal limit")
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(pm.UserParamDialog, "exec", accept)
        dup = pm.duplicate_parameter(ws, e)
        assert seen["title"] == "Duplicate as user parameter"
        assert seen["hide"][1], "hide the original: on by default"
        assert dup is not None and up.hidden(e)
        assert id(e) not in ws._item_of and id(dup) in ws._item_of, "the tree"
        assert ws.primary._user_params[0]["replaces"]["name"] == MAP
        # the copy edited to show the original again
        spec = dict(dup.user_spec, hide=False)
        monkeypatch.setattr(pm.UserParamDialog, "spec", lambda self: up.normalise(spec))
        monkeypatch.setattr(pm.UserParamDialog, "exec",
                            lambda self: QtWidgets.QDialog.DialogCode.Accepted)
        pm.edit_parameter(ws, dup)
        assert not up.hidden(e) and id(e) in ws._item_of
    finally:
        w.close_discarding()
