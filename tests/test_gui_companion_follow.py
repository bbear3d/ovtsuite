# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A flash tab follows its companion (EEPROM / MPC) without ever putting an
old copy back (ovtsuite.gui_project: ``comp_base``, ``sync_companions``)."""
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
from test_eeprom_pairing import eeprom16  # noqa: E402
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402
from ovtsuite.project import companions as co  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch, isolated_qsettings):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    # (an EEPROM set opens its modal editor; nothing here is driving it)
    monkeypatch.setattr(gui.Workspace, "open_own_eeprom_editor",
                        lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def world(G, tmp_path):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    from ovtsuite import gui_project as gp
    proj, _ = ovt.import_file(LQ)
    path = str(tmp_path / "w.ovt")
    proj.save(path)
    win = G.MainWindow()
    win.clear_closed()
    win.show()
    doc = gp.ProjectDoc(proj, path)
    gp.docs(win).append(doc)
    yield win, doc, proj, gp
    win.close_discarding()


def _open(win, doc, gp, s):
    return gp.open_branch(win, doc, s.id, s.active.id)


def _poke(ws, off=0x100):
    b = ws.primary.binfile.data[off]
    ws.primary.edit_bytes(off, bytes([b ^ 0xFF]))


def _same_but_checksums(a, b):
    """``a`` is ``b`` but for a few checksum words: the edits (at 0x200 and
    0x300, here) are the same in both."""
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(a) == len(b) and len(diff) <= 64 and 0x200 not in diff \
        and 0x300 not in diff


def _edit_eeprom_tab(ws, off=0x200):
    b = ws.primary.binfile.data[off]
    ws.primary.edit_bytes(off, bytes([b ^ 0x55]))


def test_a_dirty_flash_tab_takes_a_companion_saved_meanwhile(world):
    win, doc, proj, gp = world
    fl = proj.sets[0]
    comp = co.attach(proj, fl.id, "eeprom", eeprom16())
    t_flash = _open(win, doc, gp, fl)
    _poke(t_flash)
    unsaved = bytes(t_flash.primary.binfile.data)
    t_ee = _open(win, doc, gp, comp)
    _edit_eeprom_tab(t_ee)
    assert gp.save(t_ee, tab_only=True)
    head = co.head_bytes(proj, comp)
    assert head != eeprom16()
    n = len(comp.checkpoints)
    # the flash tab still has its unsaved edit, and the new EEPROM
    assert t_flash.is_dirty()
    assert bytes(t_flash.primary.binfile.data) == unsaved
    assert gp._tab_companion(t_flash, "eeprom") == head
    # saving it does not put the old EEPROM back
    assert gp.save(t_flash, tab_only=True)
    # (a flash's save corrects the EEPROM's checksums, which is all it may
    # change in it: the edit made in the EEPROM's tab stays)
    assert _same_but_checksums(co.head_bytes(proj, comp), head)
    assert len(comp.checkpoints) <= n + 1


def test_two_flashes_sharing_a_companion(world):
    win, doc, proj, gp = world
    a = proj.sets[0]
    comp = co.attach(proj, a.id, "eeprom", eeprom16())
    b = ovt.import_file(LQ, proj)[1]
    assert b.id != a.id
    co.link(proj, b.id, comp.id)
    ta, tb = _open(win, doc, gp, a), _open(win, doc, gp, b)
    t_ee = _open(win, doc, gp, comp)
    _edit_eeprom_tab(t_ee)
    # B is dirty (another edit), A is clean; the EEPROM is saved from A's tab
    _poke(tb)
    _poke(ta, 0x300)
    # an EEPROM edit made in A's own tab (the EEPROM part of its working file)
    head0 = co.head_bytes(proj, comp)
    edited = bytearray(head0)
    edited[0x300] ^= 0x33
    assert gp._inject_companion(ta, "eeprom", bytes(edited))
    ta.project_ref.comp_base = {"eeprom": head0}
    assert gp.save(ta, tab_only=True)
    head1 = co.head_bytes(proj, comp)
    assert head1 != head0
    # B (dirty) follows without losing its edit, and saving it keeps head1
    tb = next(t for t in doc.tabs(win) if t.project_ref.sid == b.id)
    assert tb.is_dirty()
    assert gp._tab_companion(tb, "eeprom") == head1
    n = len(comp.checkpoints)
    assert gp.save(tb, tab_only=True)
    assert _same_but_checksums(co.head_bytes(proj, comp), head1)
    assert len(comp.checkpoints) <= n + 1


def test_a_dirty_flash_tab_when_a_different_eeprom_is_paired(world):
    win, doc, proj, gp = world
    fl = proj.sets[0]
    old = co.attach(proj, fl.id, "eeprom", eeprom16())
    other = bytearray(eeprom16())
    other[0x400] ^= 0x7E
    new = co.attach(proj, fl.id, "eeprom", bytes(other))     # replaces the link
    co.link(proj, fl.id, old.id)
    t = _open(win, doc, gp, fl)
    _poke(t)
    unsaved = bytes(t.primary.binfile.data)
    co.link(proj, fl.id, new.id)                              # pair the other
    gp.sync_companions(t)
    assert t.is_dirty() and bytes(t.primary.binfile.data) == unsaved
    assert gp._tab_companion(t, "eeprom") == co.head_bytes(proj, new)
    n_old, n_new = len(old.checkpoints), len(new.checkpoints)
    assert gp.save(t, tab_only=True)
    assert len(old.checkpoints) == n_old and len(new.checkpoints) <= n_new + 1
    assert co.head_bytes(proj, old) == eeprom16()
    assert co.head_bytes(proj, new)[0x400] == other[0x400]


# ------------------------------------------------- the WinOLS versions dialog
def test_the_versions_question_is_asked_for_an_olsx_too(tmp_path, app):
    from test_definitions import _ols_with_versions
    from test_olsx import _export
    from ovtsuite import gui_olsversions as ov
    asked = []

    class Dlg:
        def __init__(self, parent, name, versions):
            asked.append((name, len(versions)))

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Rejected
    path = tmp_path / "many.olsx"
    path.write_bytes(_export(_ols_with_versions()))
    old = ov.OlsVersionsDialog
    ov.OlsVersionsDialog = Dlg
    try:
        assert ov.choose(None, str(path)) == ov.CANCEL
    finally:
        ov.OlsVersionsDialog = old
    assert asked == [("many.olsx", 3)]


def test_the_ticked_versions_with_nothing_ticked_warns(app, monkeypatch):
    from ovtsuite import gui_olsversions as ov
    said = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    dlg = ov.OlsVersionsDialog(None, "m.ols", [("A", "", 1, 0), ("B", "", 1, 0)])
    dlg.some.setChecked(True)
    dlg.tree.topLevelItem(0).setCheckState(0, QtCore.Qt.CheckState.Unchecked)
    dlg.accept()
    assert said and dlg.result() != QtWidgets.QDialog.DialogCode.Accepted
    dlg.tree.topLevelItem(1).setCheckState(0, QtCore.Qt.CheckState.Checked)
    dlg.accept()
    assert dlg.value() == [1]


# ------------------------------------------------------------------ exports
def test_export_every_part_asks_before_replacing_and_has_the_companion(
        world, tmp_path, monkeypatch):
    from ovtsuite.project import exporters as ex
    win, doc, proj, gp = world
    fl = proj.sets[0]
    comp = co.attach(proj, fl.id, "eeprom", eeprom16())
    new = bytearray(eeprom16())
    new[0x500] ^= 0x42
    proj.commit(comp.id, None, {"eeprom": bytes(new)}, label="edited")
    ws = _open(win, doc, gp, fl)
    ref = ws.project_ref
    members = ex.parts(proj, fl.id)
    assert {m.role for m in members} >= {"flash", "eeprom"}
    out = tmp_path / "out"
    out.mkdir()
    ee_file = out / gp._member_file(ref, fl, next(m for m in members
                                                  if m.role == "eeprom"))
    ee_file.write_bytes(b"precious")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    answers = [QtWidgets.QMessageBox.StandardButton.Cancel,
               QtWidgets.QMessageBox.StandardButton.Yes]
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(a[2])
                                     or answers.pop(0)))
    gp._export_all_members(ws, ref, fl, members)
    assert ee_file.read_bytes() == b"precious" and "EEPROM" in asked[0]
    gp._export_all_members(ws, ref, fl, members)
    assert ee_file.read_bytes() == bytes(new)


def test_dropping_a_project_settles_edits_made_elsewhere_first(world, monkeypatch):
    win, doc, proj, gp = world
    from ovtsuite import gui_external as ext
    calls = []
    monkeypatch.setattr(ext, "settle", lambda w, d=None: calls.append("settle") or True)
    monkeypatch.setattr(ext, "erase_doc", lambda w, d: calls.append("erase"))
    gp._drop_doc(win, doc)
    assert calls == ["settle", "erase"]
