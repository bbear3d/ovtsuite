# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the transmission modules' pages: ZF 6HP, ZF 8HP, Aisin TR-xx."""
from help_screenshots import Skip, shot

ZF6 = "zf6hp/help"
ZF8 = "zf8hp/help"
AIS = "aisin/help"


# -- helpers ------------------------------------------------------------------

def _find(window, pred, what):
    for e in window.current.primary.entities:
        if pred(e):
            return e
    raise Skip("no %s in this file" % what)


def _by_label(window, start):
    return _find(window, lambda e: (getattr(e, "label", "") or "").startswith(start),
                 "parameter labelled %r" % start)


def _show(ctx, window, e, zmode=None, advanced=False):
    ws = window.current
    ws.advanced.setChecked(advanced)
    ctx.settle(100)
    ws.open_table(e, pin=True)
    ctx.settle(200)
    if zmode is not None:
        i = ws.zmode_box.findData(zmode)
        if i < 0:
            raise Skip("no display mode %r for this table" % zmode)
        ws.zmode_box.setCurrentIndex(i)
        ctx.settle(200)
    return e


def _reveal(ctx, window, e):
    """Expand the parameter tree down to ``e`` and select it."""
    ws = window.current
    tree = getattr(ws, "tree", None)
    if tree is None:
        return
    from PySide6 import QtCore
    it = tree.invisibleRootItem()
    stack = [it]
    while stack:
        node = stack.pop()
        for i in range(node.childCount()):
            ch = node.child(i)
            if ch.data(0, QtCore.Qt.ItemDataRole.UserRole) is e:
                p = ch.parent()
                while p is not None:
                    p.setExpanded(True)
                    p = p.parent()
                tree.setCurrentItem(ch)
                tree.scrollToItem(ch)
                return
            stack.append(ch)


def _flash_dialog(ctx, window, ecu):
    from ovtsuite.flash.gui_flash import FlashDialog
    dlg = FlashDialog(window, "read")
    i = dlg.ecu_box.findText(ecu)
    if i < 0:
        raise Skip("the flasher has no %s entry" % ecu)
    dlg.ecu_box.setCurrentIndex(i)
    ctx.settle(100)
    dlg._ecu_changed()
    return ctx.keep(dlg)


# -- ZF 6HP ---------------------------------------------------------------------

@shot("zf6hp-shift-engine", ZF6, size=(1280, 760),
      what="A 6HP19 shift schedule shown as engine speed")
def zf6hp_shift_engine(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    e = _by_label(w, "Shift schedule SKL 10.0")
    _show(ctx, w, e, "engine")
    _reveal(ctx, w, e)
    return w


@shot("zf6hp-shift-output", ZF6, size=(1280, 760),
      what="The same shift schedule as stored (output-shaft speed)")
def zf6hp_shift_output(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    e = _by_label(w, "Shift schedule SKL 10.0")
    _show(ctx, w, e, "output")
    _reveal(ctx, w, e)
    return w


@shot("zf6hp-tcc", ZF6, size=(1280, 640),
      what="A TCC (lock-up) schedule with its gear-group sub-columns")
def zf6hp_tcc(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    e = _by_label(w, "TCC schedule WK 2.0")
    _show(ctx, w, e, "output")
    _reveal(ctx, w, e)
    return w


@shot("zf6hp-tip-limits", ZF6, size=(1280, 640),
      what="A manual-mode speed limit, with Copy to other bank")
def zf6hp_tip_limits(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    e = _find(w, lambda e: e.name == "TIPAN_BGRO_27_0", "TIPAN_BGRO_27_0")
    _show(ctx, w, e, "engine")
    _reveal(ctx, w, e)
    return w


@shot("zf6hp-vehicle", ZF6, size=(420, 240),
      what="The Vehicle dialog for the road-speed modes")
def zf6hp_vehicle(ctx):
    from PySide6 import QtWidgets
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    e = _by_label(w, "Shift schedule SKL 10.0")
    _show(ctx, w, e, "kmh")
    ws = w.current
    before = set(ws.findChildren(QtWidgets.QDialog))
    ws.open_vehicle()
    new = [d for d in ws.findChildren(QtWidgets.QDialog) if d not in before
           and d.windowTitle() == "Vehicle"]
    if not new:
        raise Skip("the Vehicle dialog did not open")
    return ctx.keep(new[-1])


@shot("zf6hp-dtc", ZF6, size=(900, 560),
      what="The ZF fault-code editor")
def zf6hp_dtc(ctx):
    from ovtsuite import gui
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    dlg = gui.Zf6hpDtcDialog(w.current.primary, w)
    return ctx.keep(dlg)


@shot("zf6hp-fileinfo", ZF6, size=(900, 400),
      what="File information for a 6HP19 file")
def zf6hp_fileinfo(ctx):
    from PySide6 import QtWidgets
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    dlg = _ORIG_INFO(w.current)             # owned by the main window
    if not isinstance(dlg, QtWidgets.QDialog):
        raise Skip("the file information window did not open")
    return ctx.keep(dlg)


@shot("zf6hp-state-tables", ZF6, size=(1280, 700),
      what="The shift state tables, behind Advanced")
def zf6hp_state_tables(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))
    ws = w.current
    ws.advanced.setChecked(True)
    ctx.settle(200)
    e = _find(w, lambda e: e.name == "STDAT_kprt_Drv_0", "STDAT_kprt_Drv_0")
    _show(ctx, w, e)
    _reveal(ctx, w, e)
    return w


@shot("zf6hp-flash-tp20", ZF6, size=(720, 330),
      what="ZF6HP with a 6HP19 file open (TP 2.0), Advanced open: Transport")
def zf6hp_flash_tp20(ctx):
    w = ctx.main_window(ctx.ref("zf6hp28_tdi"))     # despite the key, a 6HP19
    dlg = _flash_dialog(ctx, w, "ZF6HP")
    dlg.adv_toggle.setChecked(True)
    ctx.settle(100)
    return dlg


@shot("zf6hp-flash-uds", ZF6, size=(720, 320),
      what="ZF6HP with a 6HP28 file open (UDS): Calibration only")
def zf6hp_flash_uds(ctx):
    from ovtsuite.flash.standalone import StandaloneFlashHost
    from help_shots.flash import zf6hp28_file
    host = ctx.keep(StandaloneFlashHost())
    host.primary = zf6hp28_file()
    dlg = _flash_dialog(ctx, host, "ZF6HP")
    dlg.adv_toggle.setChecked(False)                # the tp20 shot left it open
    ctx.settle(100)
    return dlg


# -- ZF 8HP ---------------------------------------------------------------------

@shot("zf8hp-shift", ZF8, size=(1280, 720),
      what="An 8HP shift program shown as engine speed")
def zf8hp_shift(ctx):
    w = ctx.main_window(ctx.ref("zf8hp"))
    e = _by_label(w, "Shift program #13 ")
    _show(ctx, w, e, "engine")
    _reveal(ctx, w, e)
    return w


@shot("zf8hp-wk", ZF8, size=(1280, 640),
      what="An 8HP converter-clutch (WK) table")
def zf8hp_wk(ctx):
    w = ctx.main_window(ctx.ref("zf8hp"))
    e = _by_label(w, "WK characteristics 1")
    _show(ctx, w, e, "output")
    _reveal(ctx, w, e)
    return w


# -- Aisin --------------------------------------------------------------------

@shot("aisin-limited", AIS, size=(560, 220),
      what="Opening a 09D file: identified, checksum only")
def aisin_limited(ctx):
    from PySide6 import QtWidgets
    w = ctx.main_window(ctx.ref("aisin09d"))
    boxes = [b for b in w.findChildren(QtWidgets.QMessageBox)
             if b.windowTitle().startswith("Limited support")]
    if not boxes:
        raise Skip("no limited-support message for this file")
    return ctx.keep(boxes[-1])


@shot("aisin-flash", AIS, size=(720, 250),
      what="The flasher set to Aisin TRx")
def aisin_flash(ctx):
    w = ctx.main_window(ctx.ref("aisin09d"))
    return _flash_dialog(ctx, w, "Aisin TRx")


# The framework answers the file-information pop-up for every recipe; this
# one wants it, so keep the real method from before that happens.
def _orig_info():
    from ovtsuite import gui
    return gui.Workspace.show_file_info


try:
    _ORIG_INFO = _orig_info()
except Exception:                       # pragma: no cover - no Qt here
    _ORIG_INFO = None
