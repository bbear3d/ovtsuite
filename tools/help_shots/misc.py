# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the VDO Cluster and 4F/4L HVAC module pages, the Module
Manager, and the About dialog."""
import os

from help_screenshots import Skip, shot


# ------------------------------------------------------------------ VDO
@shot("vdo-main-window", "vdo/help", size=(1240, 640),
      what="A VWK501 cluster EEPROM open, the speedometer needle map showing")
def vdo_main(ctx):
    ctx.need_module("vdo")
    w = ctx.main_window(ctx.ref("vdo_vwk501"))
    ctx.open_table(w, "Coding: country", pin=True)
    ctx.open_table(w, "Speedometer calibration", pin=True)
    return w


@shot("vdo-sweep", "vdo/help", size=(820, 600),
      what="Tools > Needle sweep... on a VWK501MH 01.10 EEPROM")
def vdo_sweep(ctx):
    ctx.need_module("vdo")
    from ovtsuite import gui
    w = ctx.main_window(ctx.ref("vdo_vwk501"))
    dlg = gui.SweepDialog(w.current.primary, w.current)
    # pick the hayperek LED sweep: its details show the factory-fix note
    tree = dlg.tree
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        if it.text(0).startswith("hayperek"):
            tree.setCurrentItem(it)
            break
    return ctx.keep(dlg)


@shot("vdo-speed", "vdo/help", size=(620, 620),
      what="Tools > VSS pulse settings... on a cluster EEPROM (Speed & distance)")
def vdo_speed(ctx):
    ctx.need_module("vdo")
    from ovtsuite import gui
    w = ctx.main_window(ctx.ref("vdo_vwk501"))
    dlg = gui.VssDialog(w.current.primary, w.current)
    return ctx.keep(dlg)


def _flash_dialog(ctx, w, ecu):
    from ovtsuite.flash.gui_flash import FlashDialog
    dlg = FlashDialog(w, "read")
    i = dlg.ecu_box.findText(ecu)
    if i < 0:
        raise Skip("no %r entry in the ECU list" % ecu)
    dlg.ecu_box.setCurrentIndex(i)
    ctx.settle(200)
    return ctx.keep(dlg)


@shot("vdo-flash", "vdo/help", size=(900, 250),
      what="Tools > ECU read / write... with Cluster (VDO K-line) chosen")
def vdo_flash(ctx):
    w = ctx.main_window(ctx.ref("vdo_vwk501"))
    return _flash_dialog(ctx, w, "Cluster (VDO K-line)")


# ------------------------------------------------------------------ HVAC
@shot("hvac-main-window", "hvac/help", size=(1240, 700),
      what="A 4F0910043B Climatronic .hvac open, the heated-seat table showing")
def hvac_main(ctx):
    ctx.need_module("hvac")
    w = ctx.main_window(ctx.ref("hvac_4f"))
    ctx.open_table(w, "AC-compressor bypass (accode12)", pin=True)
    ctx.open_table(w, "Heated-seat setpoints", pin=True)
    return w


@shot("hvac-flash", "hvac/help", size=(900, 250),
      what="Tools > ECU read / write... with Climatronic chosen")
def hvac_flash(ctx):
    w = ctx.main_window(ctx.ref("hvac_4f"))
    return _flash_dialog(ctx, w, "Climatronic")


# ------------------------------------------------------------------ modules
@shot("modules-manager", "data/help", size=(900, 400),
      what="Tools > Modules... (the Module Manager)")
def modules_manager(ctx):
    from ovtsuite.gui_modules import ModuleManagerDialog
    dlg = ModuleManagerDialog(None)
    for r in range(dlg.table.rowCount()):
        if dlg.table.item(r, 0).text() == "VDO Cluster":
            dlg.table.selectRow(r)
            break
    return ctx.keep(dlg)


# ------------------------------------------------------------------ about
@shot("about-dialog", "data/help", size=(900, 620),
      what="Help > About OVT-Suite...")
def about_dialog(ctx):
    from ovtsuite.gui_about import AboutDialog, ABOUT
    return ctx.keep(AboutDialog(None, ABOUT))


@shot("about-credits", "data/help", size=(900, 620),
      what="Help > Credits & licences... with a cluster patch selected")
def about_credits(ctx):
    from ovtsuite.gui_about import AboutDialog, CREDITS
    dlg = AboutDialog(None, CREDITS)
    tree = dlg.tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        for j in range(top.childCount()):
            if top.child(j).text(0).startswith("hayperek"):
                tree.setCurrentItem(top.child(j))
    return ctx.keep(dlg)


@shot("converter", "data/help", size=(1100, 780),
      what="The Converter with an XDF and its binary added, TunerPro XDF to "
           "ASAP2 A2L, the preview filled in")
def converter(ctx):
    import shutil
    import tempfile
    from ovtsuite import gui_convert
    # copies in a short, neutral folder (the paths show in the window), else
    # the temporary folder
    d = os.path.join(os.path.splitdrive(tempfile.gettempdir())[0] + os.sep,
                     "OVT-Suite help", "Converter")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(tempfile.gettempdir(), "OVT-Suite help", "Converter")
        os.makedirs(d, exist_ok=True)
    import atexit
    atexit.register(shutil.rmtree, os.path.dirname(d), True)
    xdf = os.path.join(d, "03G906016LQ.xdf")
    binary = os.path.join(d, "03G906016LQ.bin")
    shutil.copyfile(ctx.ref("xdf_lq"), xdf)
    shutil.copyfile(ctx.ref("edc16_lq"), binary)
    dlg = gui_convert.ConverterDialog(None, sources=[xdf], binary=binary)
    ctx.settle(1500)                    # the files are read in the background
    i = dlg.target.findText("A2L", ctx.QtCore.Qt.MatchFlag.MatchContains)
    if i >= 0:
        dlg.target.setCurrentIndex(i)
    dlg.preview()
    ctx.settle(4000)                    # the preview runs in the background too
    return ctx.keep(dlg)
