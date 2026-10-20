# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the engine-ECU pages (ids ``edc16-...``, ``edc15-...``) and the
parameter tools in the Tools menu (ids ``tools-...``):
edc16/help/*.md, edc15/help/*.md and data/help/tools/*.md.

Everything opens a reference file read-only; nothing is saved.
"""
from help_screenshots import Skip, shot

try:                                    # the real File information dialog --
    from ovtsuite import gui as _gui    # the tool swaps it out for a no-op
    _SHOW_FILE_INFO = _gui.Workspace.show_file_info
except Exception:                       # pragma: no cover - no Qt
    _SHOW_FILE_INFO = None

E16 = "edc16/help"
E15 = "edc15/help"
BASE = "data/help"                      # tools/*.md show ../images/<id>.png


# -- helpers ------------------------------------------------------------------
def _split(ws, left):
    from PySide6 import QtWidgets
    sp = ws.findChild(QtWidgets.QSplitter)
    if sp is not None:
        sp.setSizes([left, 2000])


def _expand(ws, *names):
    """Expand the tree headings whose text starts with each of ``names``."""
    node = ws.tree.invisibleRootItem()
    for name in names:
        for i in range(node.childCount()):
            ch = node.child(i)
            if ch.text(0).startswith(name):
                ch.setExpanded(True)
                node = ch
                break
    return node


def _group(ws, name):
    """Pick ``name`` in the tree's group drop-down."""
    box = ws.tier_box
    for i in range(box.count()):
        if box.itemText(i).startswith(name):
            box.setCurrentIndex(i)
            return
    raise Skip("no group %r in this file" % name)


def _entity(ws, name, group=None):
    for e in ws.primary.entities:
        if e.name == name and (group is None or getattr(e, "group", None) == group):
            return e
    raise Skip("no parameter %r in this file" % name)


def _file_info(ctx, w):
    from PySide6 import QtWidgets
    if _SHOW_FILE_INFO is None:
        raise Skip("no Qt")
    dlg = _SHOW_FILE_INFO(w.current)        # owned by the main window
    if not isinstance(dlg, QtWidgets.QDialog):
        raise Skip("the file information window did not open")
    return ctx.keep(dlg)


def _tool(ctx, w, opener, kind):
    """Open a Tools-menu window the way the menu does, and return it."""
    ws = w.current
    getattr(ws, opener)()
    dlg = ws.tool(kind)
    if dlg is None:
        raise Skip("the %s window did not open" % kind)
    ctx.settle(200)
    return dlg


# -- EDC16 --------------------------------------------------------------------
@shot("edc16-main", E16, size=(1280, 780),
      what="An EDC16 file: the Power group and a smoke-limit map")
def edc16_main(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = w.current
    ws.advanced.setChecked(False)
    _split(ws, 560)
    _expand(ws, "Power", "Smoke limit")
    _expand(ws, "Power", "Boost")
    ctx.open_table(w, "FlMng_qAFSCDSmoke_MAP")
    return w


@shot("edc16-fileinfo", E16, size=(760, 560),
      what="File information for an EDC16 file")
def edc16_fileinfo(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    return _file_info(ctx, w)


@shot("edc16-coding", E16, size=(1280, 640),
      what="The Coding variants group of a file with an automatic/manual block")
def edc16_coding(ctx):
    w = ctx.main_window(ctx.ref("edc16_q"))
    ws = w.current
    ws.advanced.setChecked(False)
    _split(ws, 760)
    _group(ws, "Coding variants")
    ctx.settle(200)
    _expand(ws, "Coding variants", "Power", "Driver")
    _expand(ws, "Coding variants", "Vehicle", "Gearbox")
    _expand(ws, "Coding variants", "Vehicle")
    return w


@shot("edc16-logtune", E16, size=(1100, 760),
      what="Tune from logs after Analyse with no logs (calibration checks only)")
def edc16_logtune(ctx):
    from PySide6 import QtWidgets
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = w.current
    ws.open_logtune()
    dlg = ws.tool("logtune")
    if dlg is None:
        raise Skip("Tune from logs did not open")
    QtWidgets.QApplication.setOverrideCursor = lambda *a, **k: None
    QtWidgets.QApplication.restoreOverrideCursor = lambda *a, **k: None
    dlg._analyse()
    ctx.settle(200)
    return dlg


# -- EDC15 --------------------------------------------------------------------
@shot("edc15-main", E15, size=(1280, 780),
      what="An EDC15 file: the Power group, one copy per dataset")
def edc15_main(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    ws = w.current
    ws.advanced.setChecked(False)
    _split(ws, 520)
    _expand(ws, "Power", "Smoke limit")
    _expand(ws, "Power", "Smoke limit", "Manual")
    e = _entity(ws, "mrwBRA_KF", group="Manual")
    ws.open_table(e)
    ctx.settle(200)
    return w


@shot("edc15-fileinfo", E15, size=(760, 400),
      what="File information for an EDC15 file: its datasets")
def edc15_fileinfo(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    return _file_info(ctx, w)


def _sample_eeprom():
    """A made-up 512-byte EDC15 EEPROM with a programmed immobiliser record
    (sample VIN and numbers, not from any car)."""
    from ovtsuite.edc15 import eeprom15 as E
    buf = bytes([0xFF]) * E.EEPROM_SIZE
    F = E.FIELD_BY_KEY
    for key, val in (("immo_flag", 0x80), ("vin", "WVWZZZ1JZ1W000001"),
                     ("immo_number", "VWZ7Z0A0000001"),
                     ("immo_id", "11 22 33 44 55 66 77"), ("skc", 12345),
                     ("immo_x152", "00 00 00"), ("immo_status", "73"),
                     ("odometer", "123456.78"), ("dsv", 2), ("sta", 0),
                     ("bgm", 0), ("bga", 0), ("llr", 0), ("arf", 0), ("vgk", 0),
                     ("sbr", 0), ("hgb", 0), ("fun", 0), ("start_count", 17),
                     ("reinit", "2F 2F"), ("init_1ff", 0)):
        buf = E.apply(buf, F[key], val)
    return buf


@shot("tools-eeprom", "data/help", size=(900, 640),
      what="Tools > EEPROM editor on an EDC16 EEPROM (sample data)")
def edc16_eeprom(ctx):
    ctx.need_module("edc16")
    from ovtsuite.edc16 import eeprom16 as E16
    from ovtsuite.edc16.eeprom_layout import LAYOUT
    from ovtsuite.gui_eeprom import EepromEditor
    buf = bytearray(E16.E16_SIZE)
    for base, _c in E16.CANON_TABLE:
        buf[base:base + 30] = bytes((base + i) & 0x7F for i in range(30))
    E16.calc_checksum(buf)
    data = bytes(buf)
    F = {f.key: f for f in LAYOUT.fields}
    for key, val in (("vin", "WAUZZZ4F06N012345"), ("skc", "01234"),
                     ("immo_id", "VWZ7Z0F1234567"), ("hw_part", "0281012345"),
                     ("hw_ver", "1.6"), ("info", "03G906016LQ  R4 2,0L EDC  0159"),
                     ("date1", "14-03-06"), ("date2", "14-03-06"),
                     ("vmax", "250.00"), ("mileage", "184320.500"),
                     ("immo", "on")):
        data = LAYOUT.apply(data, F[key], val)
    # as Tools > EEPROM editor opens a project set's EEPROM: Apply puts the
    # edits into the tab
    dlg = EepromEditor(None, data, name="A6 BHW swap \u25b8 ECU \u00b7 Stock [EEPROM]",
                       layout=LAYOUT, on_save=lambda _d: None)
    dlg.data = LAYOUT.apply(dlg.data, F["mileage"], "120000.000")
    dlg._populate()
    return ctx.keep(dlg)


@shot("edc15-eeprom", E15, size=(900, 640),
      what="The EDC15 EEPROM editor (sample data)")
def edc15_eeprom(ctx):
    from ovtsuite.edc15.eeprom_gui import EepromDialog
    data = _sample_eeprom()
    dlg = EepromDialog(None, data, name="sample_EEPROM.bin")
    # one edit, so the changed-field and changed-byte highlights show
    from ovtsuite.edc15 import eeprom15 as E
    dlg.data = E.apply(dlg.data, E.FIELD_BY_KEY["odometer"], "120000.00")
    dlg._populate()
    # show the end of the hex view, where the changed odometer bytes are
    from PySide6 import QtCore
    sb = dlg.hex.verticalScrollBar()
    QtCore.QTimer.singleShot(300, lambda: sb.setValue(sb.maximum()))
    return ctx.keep(dlg)


# -- Tools menu ---------------------------------------------------------------
@shot("tools-dtc", BASE, size=(960, 600),
      what="DTC / Fault-Path editor on an EDC16 file")
def tools_dtc(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    dlg = _tool(ctx, w, "open_dtc", "dtc")
    dlg.filter.setText("EGR")
    ctx.settle(100)
    return dlg


@shot("tools-dtc-edc15", BASE, size=(960, 420),
      what="DTC / Fault-Path editor on an EDC15 file (one row per dataset)")
def tools_dtc_edc15(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    dlg = _tool(ctx, w, "open_dtc", "dtc")
    dlg.filter.setText("P0380")
    ctx.settle(100)
    return dlg


@shot("tools-msblock", BASE, size=(900, 680),
      what="Measuring blocks on an EDC16 file")
def tools_msblock(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    dlg = _tool(ctx, w, "open_msblock", "msblock")
    dlg.pop_only.setChecked(True)
    ctx.settle(100)
    t = dlg.table
    if t.rowCount() > 2:
        t.setCurrentCell(2, 1)
    return dlg


@shot("tools-gears", BASE, size=(620, 720),
      what="Gear ratios on an EDC16 file with automatic and manual copies")
def tools_gears(ctx):
    w = ctx.main_window(ctx.ref("edc16_q"))
    return _tool(ctx, w, "open_gears", "gears")


@shot("tools-gears-edc15", BASE, size=(620, 600),
      what="Gear ratios on an EDC15 file (v/n thresholds per dataset)")
def tools_gears_edc15(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    return _tool(ctx, w, "open_gears", "gears")


@shot("tools-vss", BASE, size=(560, 600),
      what="VSS pulse settings on an EDC16 file")
def tools_vss(ctx):
    w = ctx.main_window(ctx.ref("edc16_q"))
    return _tool(ctx, w, "open_vss", "vss")


@shot("tools-vss-edc15", BASE, size=(700, 640),
      what="VSS pulse settings on an EDC15 file (FGG speed blocks)")
def tools_vss_edc15(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    return _tool(ctx, w, "open_vss", "vss")


def _eoi(ctx, ref, rpm=2000, q=20):
    """Tools > End of injection on ``ref``, the cell nearest (rpm, q) picked so
    the breakdown under the grid is filled in."""
    from ovtsuite import gui_eoi
    w = ctx.main_window(ctx.ref(ref))
    ws = w.current
    if gui_eoi.availability(ws):
        raise Skip(gui_eoi.availability(ws))
    dlg = gui_eoi.open_for(ws)           # as Tools > End of injection does
    if dlg is None:
        raise Skip("the End of injection window did not open")
    calc = dlg.calc                     # (a property)
    t = calc.reference(dlg.coolant.value())
    near = lambda axis, v: min(range(len(axis)), key=lambda i: abs(axis[i] - v))
    r, c = t.cell_of(near(t.rpm_axis(), rpm), near(t.q_axis(), q))
    dlg.grid.setCurrentCell(r, c)
    ctx.settle(200)
    return dlg


@shot("tools-eoi", BASE, size=(1040, 600),
      what="End of injection on an EDC16 PD file, one cell's breakdown shown")
def tools_eoi(ctx):
    return _eoi(ctx, "edc16_lq")
