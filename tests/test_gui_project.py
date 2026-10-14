# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
""".ovt projects in the main window (ovtsuite.gui_project).

Driven offscreen with a real ECU + TCU project: how a project opens (every set
the first time, the last-used tabs after that), that a set's comparison is its
as-read original, Save vs Save tab only, opening a set that is not open,
closing tabs, and crash recovery (raw edits back as unsaved changes; a proper
exit leaves nothing behind).
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
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
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
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    for name in ("information", "warning"):
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
def car(tmp_path):
    """An ECU + TCU project on disk."""
    _need(LQ, ZF)
    proj, _ecu = ovt.import_file(LQ)
    ovt.import_file(ZF, proj)
    path = str(tmp_path / "car.ovt")
    proj.name = "A6"
    proj.save(path)
    return path


def _project_tabs(w):
    return [ws for ws in w.workspaces() if ws.project_ref is not None]


def _poke(ws, off=0x100):
    """Flip one byte of the tab's main image (an unsaved edit)."""
    b = ws.primary.binfile.data[off]
    ws.primary.edit_bytes(off, bytes([b ^ 0xFF]))
    return b ^ 0xFF


def test_first_open_shows_every_set_compared_with_its_original(win, car):
    assert win.open_path(car)
    tabs = _project_tabs(win)
    assert sorted(t.project_ref.set.name for t in tabs) == ["ECU", "TCU"]
    assert {t.platform() for t in tabs} == {"edc16", "zf6hp"}
    for t in tabs:
        assert "A6 ▸ " in win.tabs.tabText(win.tabs.indexOf(t))
        root = t.project_ref.set.roots()[0]
        assert t.cmp_version["id"] == root.id          # the as-read original
    assert win.open_path(car)                          # already open: no new tabs
    assert len(_project_tabs(win)) == 2


def test_save_tab_only_and_save_all(win, car):
    win.open_path(car)
    ecu, tcu = sorted(_project_tabs(win), key=lambda t: t.project_ref.set.name)
    v_e = _poke(ecu)
    _poke(tcu, 0x200)
    win.show_workspace(ecu)
    assert ecu.save(tab_only=True)
    assert not ecu.is_dirty() and tcu.is_dirty()
    p = ovt.Project.load(car)
    assert p.member_bytes(p.set("ECU").id, "flash")[0x100] == v_e
    assert len(p.set("TCU").checkpoints) == 1          # untouched on disk
    assert tcu.save()                                  # Save = whole project
    p = ovt.Project.load(car)
    assert len(p.set("TCU").checkpoints) == 2


def test_last_used_tabs_and_opening_the_rest(G, win, car):
    win.open_path(car)
    tcu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "TCU")
    assert win.close_tab(tcu)
    win.close_discarding()
    w2 = G.MainWindow()
    try:
        w2.open_path(car)
        assert [t.project_ref.set.name for t in _project_tabs(w2)] == ["ECU"]
        menu = QtWidgets.QMenu()
        from ovtsuite import gui_project as gp
        gp.fill_open_menu(w2, menu)
        tcu_act = next(a for a in menu.actions() if "TCU" in a.text())
        tcu_act.trigger()
        assert sorted(t.project_ref.set.name for t in _project_tabs(w2)) == \
            ["ECU", "TCU"]
    finally:
        w2.close_discarding()


def test_closing_the_last_tab_closes_the_project(win, car):
    from ovtsuite import gui_project as gp
    win.open_path(car)
    for t in _project_tabs(win):
        assert win.close_tab(t)
    assert gp.docs(win) == []


def test_crash_recovery_and_a_proper_exit(G, win, car):
    from ovtsuite import gui_project as gp
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    v = _poke(ecu, 0x1234)
    gp.autosave(win)
    doc = ecu.project_ref.doc
    doc.saver.flush()
    assert _as.pending() == []                         # its owner is alive
    doc.saver._lock.release()                          # the app "crashes"
    doc.saver._lock = None
    win.__dict__["_ovt_docs"] = []                     # ...and is gone
    recs = _as.pending()
    assert len(recs) == 1
    w2 = G.MainWindow()
    try:
        assert gp.recover(w2, recs[0])
        got = next(t for t in _project_tabs(w2) if t.project_ref.set.name == "ECU")
        assert got.is_dirty()
        assert got.primary.binfile.data[0x1234] == v   # raw, as edited
        w2.close_discarding()                          # a proper exit
        assert _as.pending() == []
    finally:
        w2.close_discarding()


def test_kept_logs_autosave_once_after_returning(win, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ovtsuite import gui_project as gp
    proj = ovt.Project("car")
    s = proj.add_set("ECU", platform="unknown")
    doc = gp.ProjectDoc(proj, None)
    gp.docs(win).append(doc)
    calls = []
    monkeypatch.setattr(gp, "autosave", lambda w, d=None: calls.append(d))
    try:
        for i in range(2):                   # both panes of a logger stop
            ws = SimpleNamespace(project_ref=SimpleNamespace(doc=doc,
                                                             sid=s.id))
            log = tmp_path / ("drive%d.csv" % i)
            log.write_text("t,rpm\n0,800\n")
            assert gp.keep_log(win, ws, str(log))
        assert calls == []                   # not in the middle of Stop
        assert len(proj.logs) == 2
        QtWidgets.QApplication.processEvents()
        assert calls == [doc]                # one snapshot for both
        QtWidgets.QApplication.processEvents()
        assert calls == [doc]
    finally:
        gp.docs(win).remove(doc)
        doc.close()


def test_save_as_project_from_a_plain_file(win, tmp_path, monkeypatch):
    _need(ZF)
    from ovtsuite import gui_project as gp
    assert win.open_path(ZF)
    ws = win.current
    _poke(ws, 0x300)
    out = str(tmp_path / "tcu.ovt")
    monkeypatch.setattr(gp, "_ask_path", lambda *_a: out)
    assert ws.save()                        # a plain file's Save makes a project
    assert ws.project_ref is not None and os.path.exists(out)
    p = ovt.Project.load(out)
    s = p.sets[0]
    assert s.platform == "zf6hp"
    # the root is the file as it is on disk; the edit is the version on top
    with open(ZF, "rb") as fh:
        disk = fh.read()
    assert p.state(s.id, s.roots()[0].id)["flash"] == disk
    assert p.head_state(s.id)["flash"][0x300] != disk[0x300]


def test_a_read_opens_as_a_project_with_the_read_as_its_original(win, G):
    _need(ZF)
    from ovtsuite import gui_project as gp
    with open(ZF, "rb") as fh:
        data = fh.read()
    assert win.load_from_image(data, "TCU-read")
    ws = win.current
    assert ws.project_ref is not None and ws.project_ref.doc.path is None
    root = ws.project_ref.set.roots()[0]
    assert root.kind == "read"
    assert ws.project_ref.doc.project.state(ws.project_ref.sid, root.id)["flash"] \
        == data
    ws.project_ref.doc.saver.flush()
    assert gp.docs(win)[0].saver.active          # protected before any save


def test_a_raw_binary_with_a_packaged_xdf_opens_in_the_main_view(win, tmp_path):
    raw = bytes(range(256)) * 16                 # 4 KB, no platform
    xdf = b"""<?xml version="1.0"?>
<XDFFORMAT version="1.80">
 <XDFHEADER><deftitle>Bench test</deftitle>
  <CATEGORY index="0x0" name="Fuel" /></XDFHEADER>
 <XDFTABLE uniqueid="0x1">
  <title>Fuel map</title><CATEGORYMEM index="0" category="1" />
  <XDFAXIS id="x"><EMBEDDEDDATA mmedaddress="0x100" mmedelementsizebits="8" />
   <indexcount>4</indexcount><MATH equation="X" /></XDFAXIS>
  <XDFAXIS id="y"><EMBEDDEDDATA mmedaddress="0x104" mmedelementsizebits="8" />
   <indexcount>2</indexcount><MATH equation="X" /></XDFAXIS>
  <XDFAXIS id="z"><EMBEDDEDDATA mmedaddress="0x200" mmedelementsizebits="16"
     mmedrowcount="2" mmedcolcount="4" /><MATH equation="X*0.5" /></XDFAXIS>
 </XDFTABLE>
 <XDFCONSTANT uniqueid="0x2"><title>Rev limit</title>
  <EMBEDDEDDATA mmedaddress="0x300" mmedelementsizebits="16" />
  <MATH equation="X" /></XDFCONSTANT>
</XDFFORMAT>"""
    (tmp_path / "bench.bin").write_bytes(raw)
    (tmp_path / "bench.xdf").write_bytes(xdf)
    proj, s = ovt.import_file(str(tmp_path / "bench.bin"))
    path = str(tmp_path / "bench.ovt")
    proj.save(path)
    assert win.open_path(path)
    ws = win.current
    assert ws.primary.support == "definition"
    names = sorted(e.name for e in ws.primary.entities)
    assert names == ["Fuel map", "Rev limit"]
    fuel = next(e for e in ws.primary.entities if e.name == "Fuel map")
    assert fuel.cell(0, 0) == (((raw[0x200] << 8) | raw[0x201]) * 0.5)


def test_a_definition_can_replace_the_modules_maps(win, car, tmp_path,
                                                   monkeypatch):
    """A binary a module defines asks whose maps to show; "Use" shows the
    file's, and removing it goes back to the module's."""
    from ovtsuite import gui_project as gp
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    assert ecu.primary.support == "full"
    xdf = tmp_path / "mine.xdf"
    xdf.write_bytes(b"""<?xml version="1.0"?><XDFFORMAT version="1.80">
 <XDFHEADER><deftitle>Mine</deftitle></XDFHEADER>
 <XDFCONSTANT uniqueid="0x2"><title>My constant</title>
  <EMBEDDEDDATA mmedaddress="0x300" mmedelementsizebits="16" />
  <MATH equation="X" /></XDFCONSTANT></XDFFORMAT>""")
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec",
                        lambda self: asked.append(self.text()) or 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton",
                        lambda self: next(b for b in self.buttons()
                                          if b.text() == "Use them instead"))
    ref = ecu.project_ref
    assert gp.attach_definition(win, ref.doc, ref.sid, str(xdf))
    assert asked and "defined by its module" in asked[0]
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    assert ecu.primary.support == "definition"
    assert [e.name for e in ecu.primary.entities] == ["My constant"]
    main = gp._sb._main_member(ref.doc.project, ref.sid)
    assert main.definition["use"] == "instead"


def test_a_definition_beside_the_modules_maps(win, car, tmp_path, monkeypatch):
    """The default: the file's tables join the module's tree in a group of
    their own; the module's stay, and its stored definition leaves them out."""
    from ovtsuite import defn as _defn
    from ovtsuite import gui_project as gp
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    n_module = len(ecu.primary.entities)
    xdf = tmp_path / "mine.xdf"
    xdf.write_bytes(b"""<?xml version="1.0"?><XDFFORMAT version="1.80">
 <XDFHEADER><deftitle>Mine</deftitle></XDFHEADER>
 <XDFCONSTANT uniqueid="0x2"><title>My constant</title>
  <EMBEDDEDDATA mmedaddress="0x300" mmedelementsizebits="16" />
  <MATH equation="X" /></XDFCONSTANT></XDFFORMAT>""")
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton",
                        lambda self: self.defaultButton())
    ref = ecu.project_ref
    assert gp.attach_definition(win, ref.doc, ref.sid, str(xdf))
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    assert ecu.primary.support == "full"
    mine = [e for e in ecu.primary.entities if e.source == "definition"]
    assert [e.name for e in mine] == ["My constant"]
    assert mine[0].category_path[0] == "From mine.xdf"
    assert len(ecu.primary.entities) == n_module + 1
    import json
    snap = json.loads(_defn.build(ecu.primary, bytes(ecu.primary.binfile.data)))
    assert snap["n_main"] == n_module


def test_the_comparison_shares_the_tabs_definition(win, car):
    """The as-read comparison is a sibling of the tab's session -- no second
    identification and match."""
    win.open_path(car)
    for t in _project_tabs(win):
        assert t.comparison.defn_state == "shared"


def test_a_patch_survives_the_project_and_can_be_removed(G, win, car):
    from ovtsuite import patch as _patch
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    s = ecu.primary
    addr = 0x1C0000
    before = bytes(s.binfile.data[addr:addr + 4])
    mod = s.sibling(bytes(s.binfile.data), "mod")
    mod.edit_bytes(addr, bytes(b ^ 0x5A for b in before))
    pt = _patch.new_patch(s, mod, [], [_patch.build_block_entry(s, mod, addr,
                                                                addr + 4)])
    _patch.apply_patch(pt, s, default_disposition=_patch.APPLY,
                       fix_checksum=False, source_label="stage1.patch")
    assert bytes(s.binfile.data[addr:addr + 4]) != before
    assert ecu.save(tab_only=True)
    p = ovt.Project.load(car)
    head = p.head(p.set("ECU").id)
    assert head.kind == "patch" and any(l.startswith("PATCH ") for l in head.log)
    win.close_discarding()
    w2 = G.MainWindow()
    try:
        w2.open_path(car)
        e2 = next(t for t in _project_tabs(w2) if t.project_ref.set.name == "ECU")
        recs = _patch.list_applied_patches(e2.primary)
        assert len(recs) == 1 and not recs[0]["pending"]
        _patch.revert_applied_patch(e2.primary, recs[0])
        assert bytes(e2.primary.binfile.data[addr:addr + 4]) == before
        assert e2.save()
        # the version history the session sees is the project's
        from ovtsuite import history as _history
        ids = [v["id"] for v in _history.list_versions(e2.primary)]
        branch = ovt.Project.load(car).set("ECU")
        assert ids[:2] == [c.id for c in branch.lineage(branch.active.head)][:2]
    finally:
        w2.close_discarding()


# --------------------------------------------- project information, WinOLS
def test_an_ols_opens_as_a_new_project_with_its_maps_and_details(win, tmp_path):
    from test_definitions import make_ols
    from ovtsuite import gui_project as gp
    path = tmp_path / "car.ols"
    path.write_bytes(make_ols(597))
    assert win.open_path(str(path))
    ws = win.current
    ref = ws.project_ref
    assert ref is not None and ref.doc.path is None          # not saved yet
    assert ws.primary.support == "definition"
    names = {e.name for e in ws.primary.entities}
    assert {"VAL", "map without a name"} <= names
    assert ref.doc.project.info["make"] == "Audi"
    assert "Vehicle: Audi A4 2002" in ws.tab_tooltip()
    assert ref.set.checkpoints[0].label == "WinOLS: Original"
    assert gp.docs(win) == [ref.doc]


def test_the_first_save_of_a_new_project_asks_for_its_information(
        win, tmp_path, monkeypatch):
    from test_definitions import make_ols
    from ovtsuite import gui_project as gp
    path = tmp_path / "car.ols"
    path.write_bytes(make_ols(479))
    assert win.open_path(str(path))
    ws = win.current
    seen = {}

    def fake_exec(dlg):
        seen["make"] = dlg.fields["make"].text()      # prefilled from WinOLS
        dlg.fields["chassis"].setText("8E")
        dlg.fields["vin"].setText("wauzzz8e12a000001")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.delenv("OVT_NO_INFO_PROMPT", raising=False)
    monkeypatch.setattr(gp.ProjectInfoDialog, "exec", fake_exec)
    out = str(tmp_path / "car.ovt")
    monkeypatch.setattr(gp, "_ask_path", lambda *_a: out)
    assert ws.save()
    assert seen["make"] == "Audi"
    p = ovt.Project.load(out)
    assert p.info["chassis"] == "8E"
    assert p.info["vin"] == "WAUZZZ8E12A000001"         # upper-cased as typed
    assert p.info["make"] == "Audi"
    # the second save does not ask again
    monkeypatch.setattr(gp.ProjectInfoDialog, "exec",
                        lambda dlg: pytest.fail("asked twice"))
    _poke(ws, 0x80)
    assert ws.save()


def test_project_information_from_the_file_menu(win, tmp_path, monkeypatch):
    from test_definitions import make_ols
    from ovtsuite import gui_project as gp
    path = tmp_path / "car.ols"
    path.write_bytes(make_ols(597))
    assert win.open_path(str(path))
    doc = win.current.project_ref.doc

    def fake_exec(dlg):
        dlg.name.setText("Ben's A4")
        dlg.fields["odometer"].setText("182 000 km")
        dlg.notes.setPlainText("stage 1 next")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(gp.ProjectInfoDialog, "exec", fake_exec)
    assert gp.edit_current_info(win)
    assert doc.project.name == "Ben's A4" and doc.title == "Ben's A4"
    assert doc.project.info["odometer"] == "182 000 km"
    assert doc.project.info["notes"] == "stage 1 next"
    assert doc.is_dirty(win)


def test_a_definition_attached_to_a_raw_set_gives_it_a_tree(G, win, tmp_path,
                                                            monkeypatch):
    from test_definitions import A2L, _a2l_image
    from ovtsuite import gui_project as gp
    # the raw binary's "Limited support" box would wait for a click forever
    monkeypatch.setattr(G.Workspace, "_explain_limited_support",
                        lambda self, *a, **k: None)
    (tmp_path / "bench.bin").write_bytes(_a2l_image())
    proj, s = ovt.import_file(str(tmp_path / "bench.bin"))
    out = str(tmp_path / "bench.ovt")
    proj.save(out)
    assert win.open_path(out)
    ws = win.current
    assert ws.primary.support != "definition"
    (tmp_path / "bench.a2l").write_text(A2L)
    ref = ws.project_ref
    assert gp.attach_definition(win, ref.doc, ref.sid, str(tmp_path / "bench.a2l"))
    ws = win.current
    assert ws.primary.support == "definition"
    assert {"KF_q", "C_on", "KL_c"} <= {e.name for e in ws.primary.entities}
    assert ref.doc.project.set(ref.sid).members[0].definition["type"] == "a2l"


def test_startup_offers_projects_not_tabs(G, win, car, tmp_path):
    """Last session's tabs come back as the projects they belong to: one line
    for the A6 with both its tabs, a loose file on a line of its own."""
    win.open_path(car)
    loose = str(tmp_path / "loose.bin")
    with open(LQ, "rb") as src, open(loose, "wb") as dst:
        dst.write(src.read())
    win.open_path(loose, new_tab=True)
    saved = win.tab_session()
    groups = G._gp.session_groups(saved)
    assert [g["kind"] for g in groups] == ["project", "file"]
    a6 = groups[0]
    assert a6["title"] == "A6" and len(a6["indexes"]) == 2 and a6["ok"]
    assert sorted(a6["tabs"]) == ["ECU · Stock", "TCU · Stock"]
    dlg = G.RestoreProjectsDialog(saved)
    assert dlg.list.count() == 2
    dlg.list.item(1).setCheckState(QtCore.Qt.CheckState.Unchecked)
    fresh = G.MainWindow()
    try:
        win.close_discarding()          # (one window per project)
        fresh.show()
        made = fresh.restore_tabs(saved, dlg.chosen())
        assert sorted(t.project_ref.set.name for t in made) == ["ECU", "TCU"]
        # the Project panel comes back with them (it was missing)
        assert G._gp.panel(fresh).isVisible()
    finally:
        fresh.close_discarding()


def test_a_reopened_project_uses_its_stored_definitions(G, win, car):
    """Identification and matching run once: closing the project stores each
    tab's definition (nothing else unsaved, so nothing else is written), and
    the next open reads it instead of building it again."""
    from ovtsuite import gui_project as gp
    before = os.path.getmtime(car)
    win.open_path(car)
    tabs = _project_tabs(win)
    assert tabs and all(t.primary.defn_state != "used" for t in tabs)
    gp.close_project(win, gp.docs(win)[0])
    assert os.path.getmtime(car) >= before
    p = ovt.Project.load(car)
    assert all(p.cache(s.id, s.active.id) for s in p.sets)
    assert not p.sets[0].checkpoints[1:]           # no new version
    win.open_path(car)
    assert all(t.primary.defn_state == "used" for t in _project_tabs(win))


def test_an_edited_and_saved_tab_reopens_from_its_stored_definition(G, win, car):
    from ovtsuite import gui_project as gp
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    e = next(e for e in ecu.primary.entities
             if e.kind == "map" and not getattr(e, "text_kind", ""))
    ecu.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    assert ecu.save()
    assert gp.close_project(win, gp.docs(win)[0])
    assert win.open_path(car), [d.path for d in gp.docs(win)]
    names = [t.project_ref.set.name for t in _project_tabs(win)]
    assert "ECU" in names, (names, [t.display_name() for t in win.workspaces()])
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    assert ecu.primary.defn_state == "used", ecu.primary.defn_state


def _until(cond, secs=60):
    import time
    end = time.monotonic() + secs
    while not cond():
        assert time.monotonic() < end, "timed out"
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def _info_on_disk(path):
    return ovt.Project.load(path).info


def test_a_rebuilt_definition_goes_into_the_file_alone(G, win, car,
                                                       monkeypatch):
    """Rebuilt snapshots are written into the .ovt soon after the tabs open,
    by themselves: the project's other unsaved change stays unsaved, and the
    next save is not taken for a file changed by another program."""
    from ovtsuite import gui_defncache as dc
    from ovtsuite import gui_disksync as dsg
    from ovtsuite import gui_project as gp
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 0)
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    assert doc.project.set_info({"make": "VW"})     # unsaved, the project's
    tabs = _project_tabs(win)
    _until(lambda: all(t.primary.defn_state == "used" for t in tabs)
           and not dc.running(doc) and "_defn_timer" not in doc.__dict__)
    p = ovt.Project.load(car)
    assert all(p.cache(t.project_ref.sid, t.project_ref.bid) for t in tabs)
    assert "make" not in p.info
    assert doc.project.modified_since_save          # still offered on close
    assert [e.doc for e in gp.unsaved_entries(win)] == [doc]
    assert not doc.__dict__.get("_cache_dirty")
    monkeypatch.setattr(dsg, "resolve",
                        lambda *a: pytest.fail("taken as changed on disk"))
    assert gp.save_selection(win, doc, [])
    assert _info_on_disk(car)["make"] == "VW"
    assert not doc.project.modified_since_save


def test_closing_keeps_a_rebuilt_definition_whatever_else_is_unsaved(
        G, win, car, monkeypatch):
    """Close the binary, or take the tab away without it going into the
    history: the snapshot is kept and written, the project's other
    unsaved change is not."""
    from ovtsuite import gui_defncache as dc
    from ovtsuite import gui_project as gp
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 1 << 30)   # not while idle
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.project.set_info({"make": "VW"})
    ecu, tcu = sorted(_project_tabs(win), key=lambda t: t.project_ref.set.name)
    refs = [(t.project_ref.sid, t.project_ref.bid) for t in (ecu, tcu)]
    assert ecu.primary.defn_state != "used"
    ecu.close_primary()
    dc.finish(doc)
    p = ovt.Project.load(car)
    assert p.cache(*refs[0]) and not p.cache(*refs[1])
    win._remove_workspace(tcu, remember=False)     # the project's last tab
    assert doc not in gp.docs(win)
    dc.finish(doc)
    p = ovt.Project.load(car)
    assert p.cache(*refs[0]) and p.cache(*refs[1])
    assert "make" not in p.info


def test_dont_save_still_writes_the_rebuilt_definitions(G, win, car,
                                                        monkeypatch):
    from ovtsuite import gui_defncache as dc
    from ovtsuite import gui_project as gp
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.project.set_info({"make": "VW"})
    refs = [(t.project_ref.sid, t.project_ref.bid) for t in _project_tabs(win)]
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Discard))
    assert gp.close_project(win, doc)
    dc.finish(doc)
    p = ovt.Project.load(car)
    assert all(p.cache(*r) for r in refs) and "make" not in p.info


def test_versions_merge_from_the_history_dialog_and_the_panel(G, win, car,
                                                               monkeypatch):
    from ovtsuite import gui_project as gp
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    e = next(e for e in ecu.primary.entities
             if e.kind == "map" and not getattr(e, "text_kind", ""))
    for i in range(3):
        ecu.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
        assert ecu.save(tab_only=True)
    ref = ecu.project_ref
    s = ref.set
    chain = [c.id for c in s.lineage(ref.branch.head)]
    assert len(chain) == 4                       # 3 saves + the original
    head_bytes = ref.doc.project.state(ref.sid, chain[0])
    got = gp.merge_versions(win, ref.doc, ref.sid, chain[:2], "two saves")
    assert got == chain[0]
    assert [c.id for c in s.lineage(ref.branch.head)] == [chain[0]] + chain[2:]
    assert ref.doc.project.state(ref.sid, chain[0]) == head_bytes
    # the working file's history follows: the dialog lists the merged one
    from ovtsuite import history
    vers = history.list_versions(ecu.primary)
    ids = [v["id"] for v in vers]
    assert chain[1] not in ids and chain[0] in ids
    # ...as merged: both saves' times, and how many saves it holds
    merged = next(v for v in vers if v["id"] == chain[0])
    assert len(merged["merged_from"]) == 2 and merged.get("ts_from")
    # the panel: two versions selected -> "Merge 2 versions into one..."
    panel = gp.panel(win)
    panel.refresh()
    items = []

    def walk(it):
        for i in range(it.childCount()):
            ch = it.child(i)
            x = ch.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if x and x[0] == "ck" and x[2] == ref.sid and x[4] in (chain[0], chain[2]):
                items.append(ch)
            walk(ch)
    walk(panel.tree.invisibleRootItem())
    for it in items:
        it.setSelected(True)
    assert sorted(panel._picked_versions(items[0], ref.doc, ref.sid)) == \
        sorted([chain[0], chain[2]])
    with pytest.raises(ValueError):              # the original can't merge
        gp.merge_versions(win, ref.doc, ref.sid, [chain[0], chain[3]])


def test_find_in_definition_searches_the_definition_file_only(win, car, tmp_path,
                                                             monkeypatch):
    from ovtsuite import gui_project as gp
    from ovtsuite.groups import DEFINITION
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    xdf = tmp_path / "mine.xdf"
    xdf.write_bytes(b"""<?xml version="1.0"?><XDFFORMAT version="1.80">
 <XDFHEADER><deftitle>Mine</deftitle></XDFHEADER>
 <XDFCONSTANT uniqueid="0x2"><title>My constant</title>
  <EMBEDDEDDATA mmedaddress="0x300" mmedelementsizebits="16" />
  <MATH equation="X" /></XDFCONSTANT></XDFFORMAT>""")
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton",
                        lambda self: self.defaultButton())
    ref = ecu.project_ref
    assert gp.attach_definition(win, ref.doc, ref.sid, str(xdf))
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    win.show_workspace(ecu)
    assert ecu.primary.has_definition_file
    win.search_dock.open_in_definition()
    assert win.search_dock.panel.opts.tier == DEFINITION
    assert win.search_dock.isVisible()


def test_closing_a_projects_last_tab_is_close_project(G, win, car, monkeypatch):
    """One question for the whole project (the tab's edits and its own
    changes); Save writes the project; the project goes into the history
    whole."""
    from ovtsuite import gui_project as gp
    win.open_path(car)
    tabs = _project_tabs(win)
    tcu = next(t for t in tabs if t.project_ref.set.name == "TCU")
    win.close_tab(tcu)                               # not the last: no question
    ecu = _project_tabs(win)[0]
    e = next(e for e in ecu.primary.entities
             if e.kind == "map" and not getattr(e, "text_kind", ""))
    ecu.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda parent, title, text, *a, **k: asked.append(title) or
        QtWidgets.QMessageBox.StandardButton.Save))
    win.close_tab(ecu)
    assert asked == ["Close project"]
    assert not gp.docs(win)
    p = ovt.Project.load(car)
    assert p.head(p.set("ECU").id).kind == "save"


def test_patches_from_the_tabs_own_history_start_at_the_original(G, win, car,
                                                                 monkeypatch):
    """Generate patch from history offers the original as read (the usual
    start: everything the branch changed), and Generate patch against the
    default comparison -- the original -- carries the tab's changes, not the
    way back to stock."""
    from ovtsuite import history, patch
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    assert ecu.comparison is not None and ecu.comparison.version_of
    assert ecu.comparison.binfile.name.endswith("ECU - original")
    e = next(e for e in ecu.primary.entities
             if e.kind == "map" and not getattr(e, "text_kind", ""))
    before = e.cell(0, 0)
    ecu.primary.edit_cells(e, [(0, 0, before + 1)])
    assert ecu.save(tab_only=True)
    dlg = G.HistoryPatchDialog(ecu.primary, ecu)
    assert dlg.from_box.itemData(0) == history.ORIGINAL
    assert dlg.from_box.itemText(0) == "The original as read"
    older, newer = patch.sessions_from_history(ecu.primary, history.ORIGINAL)
    assert bytes(older.binfile.data) == bytes(ecu.comparison.binfile.data)
    seen = []
    monkeypatch.setattr(G.PatchBuilderDialog, "exec",
                        lambda self: seen.append(self) or 0)
    ecu.open_patch_builder()
    b = seen[-1]
    assert b.primary is ecu.comparison and b.comparison is ecu.primary
    entry = next(x for x in b._entries if x.name == e.name)
    assert entry.values[0][0] == pytest.approx(e.cell(0, 0))
    ecu.open_patch_builder(swap=False)           # Swap direction
    assert seen[-1].primary is ecu.primary


def test_a_status_message_hides_the_status_line_under_it(win):
    bar = win.statusBar()
    bar.showMessage("Opened something")
    assert not win._status_label.isVisible()
    bar.clearMessage()
    assert win._status_label.isVisible()


def test_a_log_kept_in_the_project_greys_the_file_row_from_the_start(win, car):
    """The logger opened from a project tab: "Keep the log in the project" is
    ticked, so the Log file row is greyed out before the dialog is ever shown,
    and the file it would write is named after the project and set."""
    from ovtsuite.datalog.gui_log import DataLogDialog
    win.open_path(car)
    ecu = next(t for t in _project_tabs(win) if t.project_ref.set.name == "ECU")
    win.show_workspace(ecu)
    dlg = DataLogDialog(win)
    try:
        pane = dlg.pane
        pane.conn.ecu_box.setCurrentText("EDC16")
        pane._sync_destination()
        assert not pane.keep_in_project.isHidden()
        assert pane.keep_in_project.isChecked()
        assert not pane.path_edit.isEnabled()
        pane._set_default_path()
        assert os.path.basename(pane.path_edit.text()).startswith("A6-ECU_")
    finally:
        dlg.close()


def test_a_binary_no_module_supports_says_so_once_per_set(G, win, tmp_path,
                                                          monkeypatch):
    """Limited support is shown for a project tab too -- once per set, so a
    second tab of the same set (another branch) does not ask again."""
    from ovtsuite import gui_project as gp
    from ovtsuite.project import Project
    blob = bytes(range(256)) * 1024             # 256 KB nothing recognises
    proj = Project("Bench")
    from ovtsuite.project import importers as imp
    s = imp.import_bytes(proj, blob, "mystery.bin")
    proj.fork(s.id, "Second")
    path = str(tmp_path / "bench.ovt")
    proj.save(path)
    told = []
    monkeypatch.setattr(G.Workspace, "_explain_limited_support",
                        lambda self, loaded: None if getattr(self, "_quiet", False)
                        else told.append(loaded))
    assert gp.open_project(win, path)
    QtWidgets.QApplication.processEvents()
    ref = win.current.project_ref
    if getattr(win.current.primary, "support", "full") in ("full", "definition"):
        pytest.skip("this binary is supported here")
    assert len(told) == 1
    other = next(b for b in ref.set.branches if b.id != ref.bid)
    gp.open_branch(win, ref.doc, ref.sid, other.id)
    QtWidgets.QApplication.processEvents()
    assert len(told) == 1


def test_a_save_without_a_definition_drops_the_branchs_old_one(G, win,
                                                               tmp_path,
                                                               monkeypatch):
    """A save that produced no definition snapshot (here: a plain image,
    which carries none) must not leave the branch's previous one behind."""
    from ovtsuite import gui_project as gp
    monkeypatch.setattr(G.Workspace, "_explain_limited_support",
                        lambda self, loaded: None)
    from ovtsuite.project import Project
    from ovtsuite.project import importers as imp
    proj = Project("Bench")
    s = imp.import_bytes(proj, bytes(range(256)) * 1024, "mystery.bin")
    path = str(tmp_path / "bench.ovt")
    proj.save(path)
    assert gp.open_project(win, path)
    ws = win.current
    ref = ws.project_ref
    ref.doc.project.set_cache(ref.sid, ref.bid, b"stale")
    _poke(ws)
    assert gp._commit(ws)
    assert ref.doc.project.cache(ref.sid, ref.bid) is None


def _kept_log_bytes(tmp_path):
    from datetime import datetime, timedelta
    from ovtsuite.datalog import logfile as L
    log = L.DataLog(channels=[L.Channel(1, 0, "Engine Speed (Eng_nAvrg)", "rpm")])
    t0 = datetime(2026, 10, 12, 14, 0, 0)
    for i in range(5):
        log.append(t0 + timedelta(milliseconds=200 * i), ["%d rpm" % (800 + 50 * i)])
    p = str(tmp_path / "pull.csv")
    L.write_kwhack(p, log)
    with open(p, "rb") as fh:
        return fh.read()


def test_logs_kept_in_a_project_can_be_viewed(G, win, car, tmp_path, monkeypatch):
    """A log kept in the project opens in the logger's Log file tab: from
    the Project panel (double-click, or Open in Data logging) and from the
    logger's own Open log... -- in the OVT Logger too, from the .ovt."""
    from ovtsuite import gui_project as gp
    from ovtsuite.datalog import project_logs as pl
    from ovtsuite.project import Project
    data = _kept_log_bytes(tmp_path)
    proj = Project.load(car)
    ecu = next(s for s in proj.sets if s.name == "ECU")
    proj.add_log("pull.csv", data, set_id=ecu.id)
    proj.save(car)
    assert gp.open_project(win, car)
    doc = win.current.project_ref.doc
    aid = doc.project.logs[0].id
    panel = gp.panel(win)
    panel._view_log(("log", doc, aid))
    dlg = win._datalog_dlg
    QtWidgets.QApplication.processEvents()
    assert dlg.tabs.currentWidget() is dlg.viewer
    assert dlg._viewed is not None and dlg._viewed.rows == 5
    # the logger's Open log...: the project's logs, then From a file...
    kept = pl.kept_logs(win)
    assert [(k.project, k.set_name, k.filename) for k in kept] == \
        [("A6", "ECU", "pull.csv")]
    dlg._viewed = None
    dlg.tabs.setCurrentIndex(0)
    dlg._open_log()
    menu = dlg._open_log_menu
    acts = [a for a in menu.actions() if a.text().startswith("pull.csv")]
    assert len(acts) == 1 and menu.actions()[-1].text() == "From a file..."
    menu.close()
    acts[0].trigger()
    QtWidgets.QApplication.processEvents()
    assert dlg.tabs.currentWidget() is dlg.viewer and dlg._viewed.rows == 5
    dlg.close()
    # the OVT Logger: the project opened in it, read from the .ovt
    from ovtsuite.datalog.standalone import StandaloneLogHost
    host = StandaloneLogHost()
    host._spaces.append(type("WS", (), {
        "primary": None, "win": None,
        "project_ref": type("R", (), {"doc": type("D", (), {
            "title": "A6", "path": car})()})()})())
    assert [k.filename for k in pl.kept_logs(host)] == ["pull.csv"]
    assert open(pl.working_copy(pl.kept_logs(host)[0]), "rb").read() == data
