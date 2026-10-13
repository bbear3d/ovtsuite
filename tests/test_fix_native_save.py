# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saving writes .ovt projects; .edc15bak is read-only.

* A .dim saved as .edc16 keeps its internal flash and EEPROM (a native
  container made from a non-container source carries every region the source
  had, edited or not) -- the session level, which a project tab's working
  file relies on.
* Save / Save As offer an .ovt project (raw and native formats are exports),
  for a plain read too.
* A KWHack .edc15bak opens; Save makes a project of it (flash + EEPROM) and
  never writes .edc15bak.
* The flasher's Full backup opens as one read in a project (flash + EEPROM),
  and the EEPROM a .edc15 carries is offered to Write flash + EEPROM.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
BD = os.path.join(BINARIES, "EDC15", "038906012BD.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)


# ---- session: a .dim's regions come along into a .edc16 ---------------------

def test_dim_saved_as_edc16_keeps_internal_flash_and_eeprom(tmp_path):
    _need(LQ)
    from test_dim import _make_dim
    from ovtsuite import container as C
    from ovtsuite.session import TunerSession
    external = open(LQ, "rb").read()
    internal = os.urandom(0x81000)
    eeprom = os.urandom(0x1000)
    raw, _ = _make_dim({1: (1, 100, 200, "bin", internal),
                        2: (1, 300, 400, "bin", external),
                        3: (1, 500, 600, "bin", eeprom)})
    src = tmp_path / "car.dim"
    src.write_bytes(raw)
    s = TunerSession(str(src))
    out = str(tmp_path / "car.edc16")
    s.save(out, fix_checksum=False, backup=False, write_log=False)
    c = C.Container.load(out)
    assert c.eeprom() == eeprom
    assert c.mpc() is not None


# ---- GUI ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, kind, staticmethod(
            lambda *a, **k: boxes.append(a[1:3]) or 0))
    gui._boxes = boxes
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _save_dialog(monkeypatch, answer):
    """Record what Save As offers; answer with ``answer`` (path, filter)."""
    asked = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(
        lambda *a, **k: asked.append(a) or answer))
    return asked


def test_save_as_offers_an_ovt_project(G, win, monkeypatch):
    _need(LQ)
    asked = _save_dialog(monkeypatch, ("", ""))
    win.load_from_image(open(LQ, "rb").read(), "ECU-read")
    win.current.save_as()
    start, filters = asked[-1][2], asked[-1][3]
    assert start.endswith(".ovt")
    assert filters == "OVT project (*.ovt)"


def test_an_edc15bak_saves_as_a_project_with_its_eeprom(G, win, monkeypatch,
                                                        tmp_path):
    _need(BD)
    from ovtsuite import project as ovt
    from ovtsuite.flash import backup15
    flash = open(BD, "rb").read()
    ee = bytes(range(256)) * 2
    old = str(tmp_path / "old.edc15bak")
    with open(old, "wb") as fh:
        fh.write(backup15.build(flash, ee))
    raw_old = open(old, "rb").read()
    out = str(tmp_path / "old.ovt")
    _save_dialog(monkeypatch, (out, "OVT project (*.ovt)"))
    assert win.open_path(old)
    ws = win.current
    assert ws.save() is True                  # a .edc15bak is never saved over
    assert open(old, "rb").read() == raw_old
    p = ovt.Project.load(out)
    s = p.sets[0]
    eem = next(m for m in s.members if m.role == "eeprom")
    assert p.member_bytes(s.id, eem.id) == ee
    assert ws.project_ref is not None


def test_save_as_never_writes_edc15bak(G, win, monkeypatch, tmp_path):
    _need(BD)
    out = str(tmp_path / "x.edc15bak")
    _save_dialog(monkeypatch, (out, "All files (*)"))
    win.load_backup(open(BD, "rb").read(), bytes(512))
    win.current.save_as()
    assert not os.path.exists(out)            # it became x.edc15bak.ovt


def test_no_edc15bak_save_action(G, win):
    from PySide6 import QtGui
    names = [a.text() for a in win.findChildren(QtGui.QAction)]
    assert not any(n.startswith("Save") and ".edc15bak" in n for n in names)


def test_flasher_full_backup_opens_as_one_read(G, win, monkeypatch, tmp_path):
    _need(BD)
    from ovtsuite.flash import gui_flash
    flash = open(BD, "rb").read()
    ee = bytes(range(256)) * 2
    d = gui_flash.FlashDialog(win, "read")
    d._finish_backup(flash, ee)
    ws = win.current
    ref = ws.project_ref
    assert ref is not None and ref.doc.path is None      # untitled: Save asks
    root = ref.set.roots()[0]
    assert root.kind == "read"
    state = ref.doc.project.state(ref.sid, root.id)
    assert state["flash"] == flash and state["eeprom"] == ee
    assert ws.primary.has_eeprom()            # Write flash + EEPROM is offered


def test_eeprom_in_an_edc15_offers_write_flash_and_eeprom(G, win, tmp_path):
    _need(BD)
    from ovtsuite import container as C
    from ovtsuite.flash import gui_flash
    c = C.Container(C.format_for_kind("edc15"))
    c.add(C.BLOCK_EXTERNAL, open(BD, "rb").read())
    c.add(C.BLOCK_EEPROM, bytes(512))
    p = str(tmp_path / "bd.edc15")
    c.save(p)
    assert win.open_path(p)
    d = gui_flash.FlashDialog(win, "read")
    d.ecu_box.setCurrentText("EDC15")
    assert d._window_eeprom() == bytes(512)
    assert d.restore_btn.isVisibleTo(d)


# ---- the OVT Flasher (no main window) ---------------------------------------

def test_standalone_read_saves_a_project(app, monkeypatch, tmp_path):
    _need(LQ)
    from ovtsuite import project as ovt
    from ovtsuite.flash import standalone
    asked = []

    def pick(parent, title, start, filters):
        asked.append((start, filters))
        return str(tmp_path / "read"), filters.split(";;")[0]
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(pick))
    data = open(LQ, "rb").read()
    assert standalone.StandaloneFlashHost().load_from_image(data, "ECU-read")
    start, filters = asked[-1]
    assert start.endswith(".ovt") and filters.startswith("OVT project")
    p = ovt.Project.load(str(tmp_path / "read.ovt"))
    s = p.sets[0]
    assert s.roots()[0].kind == "read"
    assert p.member_bytes(s.id, "flash") == data


def test_standalone_open_edc15_holds_its_eeprom(app, tmp_path):
    _need(BD)
    from ovtsuite import container as C
    from ovtsuite.flash import standalone
    c = C.Container(C.format_for_kind("edc15"))
    c.add(C.BLOCK_EXTERNAL, open(BD, "rb").read())
    c.add(C.BLOCK_EEPROM, bytes(range(256)) * 2)
    p = str(tmp_path / "bd.edc15")
    c.save(p)
    host = standalone.StandaloneFlashHost()
    assert host.open_path(p)
    assert host._eeprom == bytes(range(256)) * 2
