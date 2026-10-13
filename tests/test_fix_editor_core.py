# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Regression tests for main-window / editor-core fixes: patch ranges carry
the comparison's bytes, refused saves have no side effects, a .frf is never
overwritten by Save, module load notes have their own title, the hex editor
works on the bytes on screen, F1 resolves docked panels, Edit > Scale
selection acts on the table on screen, the 3D Height slider starts where the
surface is drawn, Export > Flash from backup only for backups, and the ZF
program-area switch reaches every tab.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES, ZF6HP_REFERENCE, ROOT  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")
ZF8 = os.path.join(ROOT, "Reference", "ZF8HP", "OEM", "8K0927158K__1014.frf.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)


def _session(path):
    _need(path)
    from ovtsuite.session import TunerSession
    return TunerSession(path)


def _gap_addr(s):
    """An address in ``s``'s calibration region that no entity covers and
    that is not a checksum byte -- what diff_gaps reports as undefined data."""
    from ovtsuite import patch as P
    spans = [(e.addr, e.end) for e in s.entities]
    exempt = P._checksum_exempt(s)
    lo, hi = s.region
    for a in range(lo, hi - 4, 2):
        if any(x <= a < y + 4 for x, y in spans + exempt) or \
                any(x <= a + 3 < y for x, y in spans + exempt):
            continue
        return a
    pytest.skip("no undefined byte in the calibration region")


# -- 1: undefined-data ranges carry the comparison's bytes ------------------

def test_patch_block_carries_the_comparison_bytes():
    from ovtsuite import patch as P
    primary = _session(LQ)
    a = _gap_addr(primary)
    data = bytearray(primary.binfile.data)
    data[a:a + 4] = bytes(x ^ 0x5A for x in data[a:a + 4])
    comparison = primary.sibling(bytes(data), name="tuned.bin")

    gaps = P.diff_gaps(primary, comparison)
    gap = next(g for g in gaps if g[0] <= a < g[1])
    block = P.build_block_entry(primary, comparison, *gap)
    assert block.data == bytes(comparison.binfile.data[gap[0]:gap[1]])
    assert block.source_family == primary.family

    target = primary.sibling(bytes(primary.binfile.data), name="target.bin")
    pt = P.new_patch(primary, comparison, [], [block])
    res = P.apply_patch(pt, target, fix_checksum=False)
    assert res.applied == ["block@0x%X" % gap[0]]
    assert bytes(target.binfile.data[a:a + 4]) == bytes(data[a:a + 4])


def test_history_patch_block_carries_the_newer_bytes(tmp_path):
    from ovtsuite import history as H, patch as P
    s = _session(LQ)
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(str(tmp_path / "h.bin"))
    v1 = H.list_versions(s)[0]["id"]
    a = _gap_addr(s)
    new = bytes(x ^ 0xA5 for x in s.binfile.data[a:a + 2])
    s.edit_bytes(a, new)

    pt = P.diff_from_history(s, v1, None, include_gaps=True)
    blk = next(b for b in pt.blocks if b.addr <= a < b.addr + b.length)
    off = a - blk.addr
    assert blk.data[off:off + 2] == new


# -- 9 / 6: a refused save changes nothing -----------------------------------

def _edited(path, tmp_path, name):
    p = str(tmp_path / name)
    shutil.copyfile(path, p)
    s = _session(p)
    e = next(x for x in s.entities if x.rows * x.cols > 1)
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    return p, s


def test_save_as_another_platforms_container_has_no_side_effects(tmp_path):
    _p, s = _edited(LQ, tmp_path, "lq.bin")
    before = bytes(s.binfile.data)
    log = list(s.change_log)
    out = str(tmp_path / "lq.6hp")
    assert s.save_refusal(out)
    with pytest.raises(ValueError):
        s.save(out)
    assert bytes(s.binfile.data) == before, "checksums corrected anyway"
    assert s.change_log == log, "a version checkpoint was logged anyway"
    assert not os.path.exists(out)


def test_save_never_overwrites_a_frf(tmp_path):
    p, s = _edited(LQ, tmp_path, "lq.frf")
    raw = open(p, "rb").read()
    log = list(s.change_log)
    why = s.save_refusal()
    assert why and "Save As" in why
    with pytest.raises(ValueError):
        s.save()
    assert open(p, "rb").read() == raw
    assert not os.path.exists(p + ".bak")
    assert s.change_log == log
    out = s.save(str(tmp_path / "lq.bin"))      # Save As to a .bin still works
    assert os.path.exists(out)


# -- 13: a module's load note is not an .sgo warning -------------------------

def test_zf8hp_load_note_is_not_an_sgo_warning():
    s = _session(ZF8)
    assert s.platform == "zf8hp"
    assert s.load_note and "ZF 8HP" in s.load_note
    assert s.sgo_warning is None


# -- GUI ---------------------------------------------------------------------

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


def test_load_note_box_has_its_own_title(G, win):
    _need(ZF8)
    assert win.open_path(ZF8)
    titles = [t for t, _m in G._boxes]
    assert "ZF 8HP: notes on this file" in titles
    assert "Partially-decoded .sgo container" not in titles


def test_save_on_a_frf_goes_to_save_as(G, win, tmp_path, monkeypatch):
    _need(LQ)
    p = str(tmp_path / "x.frf")
    shutil.copyfile(LQ, p)
    raw = open(p, "rb").read()
    out = str(tmp_path / "x.ovt")
    asked = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(
        lambda *a, **k: asked.append(a) or (out, "OVT project (*.ovt)")))
    assert win.open_path(p)
    ws = win.current
    e = next(x for x in ws.primary.entities if x.rows * x.cols > 1)
    ws.primary.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    assert ws.save() is True
    assert asked, "Save did not ask where to save"
    # the .frf is never written: it became a project
    assert open(p, "rb").read() == raw and not os.path.exists(p + ".bak")
    assert os.path.exists(out) and ws.project_ref is not None


def _edc16_with_eeprom(tmp_path):
    from ovtsuite import container as C
    c = C.Container(C.format_for_kind("edc16"))
    c.add(C.BLOCK_EXTERNAL, open(LQ, "rb").read())
    c.add(C.BLOCK_EEPROM, bytes(2048))
    p = str(tmp_path / "lq.edc16")
    with open(p, "wb") as fh:
        fh.write(c.to_bytes())
    return p


def test_hex_paste_writes_into_the_region_on_screen(G, tmp_path):
    _need(LQ)
    s = _session(_edc16_with_eeprom(tmp_path))
    dlg = G.HexDialog(s)
    names = dlg.model.region_names()
    dlg.region_box.setCurrentIndex(names.index(s.EEPROM_REGION))
    assert dlg.model.editable
    ext_before = bytes(s.binfile.data[0x10:0x12])
    dlg._select_addr(0x10)
    QtWidgets.QApplication.clipboard().setText("11 22")
    dlg._paste()
    ee = dict(s.region_buffers())[s.EEPROM_REGION]
    assert ee[0x10:0x12] == b"\x11\x22"
    assert bytes(s.binfile.data[0x10:0x12]) == ext_before
    dlg.close()


def test_hex_find_copy_paste_with_the_comparison_shown(G):
    a_s, b_s = _session(LQ), _session(KN)
    a, b = bytes(a_s.binfile.data), bytes(b_s.binfile.data)
    pat = None
    for i in range(0x1C0000, len(b) - 8):
        if a[i] != b[i]:
            cand = b[i:i + 8]
            if b.find(cand) == i and a.find(cand) != i:
                pat, at = cand, i
                break
    assert pat is not None
    dlg = G.HexDialog(a_s, b_s)
    dlg.show_box.setCurrentIndex(1)                 # Comparison
    dlg.find_hex.setChecked(True)
    dlg.find.setText(pat.hex(" "))
    dlg._select_addr(0)
    dlg._find_next()
    assert dlg._addr_of(dlg.view.currentIndex()) == at

    dlg.view.selectionModel().select(
        dlg.view.currentIndex(), QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    dlg._copy()
    assert QtWidgets.QApplication.clipboard().text() == "%02X" % b[at]

    QtWidgets.QApplication.clipboard().setText("%02X" % (a[at] ^ 0xFF))
    dlg._paste()
    assert a_s.binfile.data[at] == a[at], "pasted into the primary"
    assert "read-only" in dlg.info.text()
    dlg.close()


def test_f1_resolves_a_docked_panel_with_its_own_page(app, monkeypatch):
    from ovtsuite import gui_help, helpdocs
    monkeypatch.setattr(helpdocs, "installed_modules",
                        lambda: list(helpdocs.known_modules()))
    monkeypatch.setattr(gui_help, "_window", None)

    class MainWindow(QtWidgets.QMainWindow):        # named like the real ones
        pass

    class SearchPanel(QtWidgets.QWidget):
        pass
    w = MainWindow()
    dock = QtWidgets.QDockWidget("Find", w)
    panel = SearchPanel()
    edit = QtWidgets.QLineEdit(panel)
    dock.setWidget(panel)
    w.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    other = QtWidgets.QLineEdit()
    w.setCentralWidget(other)
    monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget",
                        staticmethod(lambda: None))
    monkeypatch.setattr(QtWidgets.QApplication, "activeWindow",
                        staticmethod(lambda: w))
    f1 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F1,
                         QtCore.Qt.KeyboardModifier.NoModifier)
    filt = gui_help._F1Filter()
    try:
        monkeypatch.setattr(QtWidgets.QApplication, "focusWidget",
                            staticmethod(lambda: edit))
        assert filt.eventFilter(edit, f1) is True
        assert gui_help._window.current == "editor/search"
        # focus in the window itself: the window's page
        monkeypatch.setattr(QtWidgets.QApplication, "focusWidget",
                            staticmethod(lambda: other))
        assert filt.eventFilter(other, f1) is True
        assert gui_help._window.current == "interface"
    finally:
        if gui_help._window is not None:
            gui_help._window.close()
        w.close()


def test_scale_selection_acts_on_the_table_on_screen(G, win, monkeypatch):
    _need(LQ)
    win.open_path(LQ)
    ws = win.current
    e = next(x for x in ws.primary.entities
             if x.rows * x.cols > 4 and not getattr(x, "read_only", False)
             and x.cell(0, 0) != 0)
    ws.open_table(e)
    ws.tree.clearSelection()
    assert ws._current_entity() is None and ws.model.entity is e
    monkeypatch.setattr(QtWidgets.QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (2.0, True)))
    rev = ws.primary.undo_stack.revision
    ws.table.clearSelection()
    ws.scale()
    assert ws.primary.undo_stack.revision != rev, "nothing was scaled"


def test_3d_height_slider_starts_where_the_surface_is(G):
    vals = [[1.0, 2.0], [3.0, 4.0]]
    dlg = G.Surface3DDialog(vals, [0, 1], [0, 1], "t")
    assert dlg.height_slider.value() == round(dlg.view.zscale * 100)
    dlg.height_slider.setValue(dlg.height_slider.value())
    assert dlg.view.zscale == pytest.approx(0.9)
    dlg.close()



def test_program_area_switch_reaches_every_tab(G, win):
    _need(ZF, LQ)
    win.open_path(ZF)
    zf = win.current
    win.open_path(LQ, new_tab=True)
    assert win.current is not zf
    try:
        win._toggle_program_area(True)
        assert zf.primary.correct_program_area is True
        win._toggle_program_area(False)
        assert zf.primary.correct_program_area is False
    finally:
        G.set_program_area_pref(False)
