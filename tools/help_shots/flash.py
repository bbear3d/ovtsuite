# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the ECU flasher pages (data/help/flashing*, data/help/cables*).

No cable is attached when these run, so after the dialog's own device scan has
finished each recipe puts ONE example entry in the Port / Device list, in the
exact label format the flasher itself builds for that kind of cable (FTDI
direct, OpenPort over USB). Nothing is opened or sent: the entry only makes the
picture look like a dialog with a cable plugged in rather than an empty one.
"""
from help_screenshots import shot

FOLDER = "data/help"


def _flash_dialog(ctx, window=None, ecu="EDC16", transport="kline",
                  port="ftdi", advanced=False, standalone=False, primary=None,
                  project=None):
    """The flasher, from ``window`` (a main window) or on its own -- then
    with ``primary`` as the file to write, or ``project`` (an .ovt) opened
    the way **Open file...** opens one."""
    from ovtsuite.flash import gui_flash
    from ovtsuite.flash.standalone import StandaloneFlashHost
    if window is None:
        window = StandaloneFlashHost()
        window.primary = primary
        if project is not None and not window.open_project(project):
            from help_screenshots import Skip
            raise Skip("the flasher could not open %s" % project)
        ctx.keep(window)
    d = gui_flash.FlashDialog(window, "read")
    if standalone:
        from ovtsuite.gui_about import add_tool_buttons
        add_tool_buttons(d, "flasher")
    ctx.settle(500)                                   # let the device scan finish
    if ecu:
        d.ecu_box.setCurrentText(ecu)
    i = d.transport_box.findData(transport)
    if i >= 0 and d.transport_box.currentData() != transport:
        d.transport_box.setCurrentIndex(i)
    ctx.settle(500)                                   # the rescan the switch started
    _example_port(d, port)
    d.adv_toggle.setChecked(bool(advanced))
    ctx.settle(100)
    return ctx.keep(d)


def zf6hp28_file(name="A6 4.2 \u25b8 TCU \u00b7 Stock (as read)",
                 path="A6 4.2.ovt"):
    """A 6HP28 calibration as the flasher sees one opened from a project (the
    session's KB family; the label Open file... gives it). There is no 6HP28
    reference file to open; the flasher only looks at the family and the
    size to pick UDS, so this is all it needs."""
    import types
    return types.SimpleNamespace(
        platform="zf6hp", family="zf6hp28", path=path,
        binfile=types.SimpleNamespace(data=bytearray(0x80000), name=name))


def _example_port(d, kind):
    """Replace the (empty) scan result with one example entry."""
    from ovtsuite.flash import gui_flash
    entries = {
        "ftdi": ("FTDI:A50285BI", "FTDI:A50285BI — FT232R USB UART, S/N A50285BI (FTDI direct)"),
        "openport": (gui_flash.OPENPORT_USB, "OpenPort 2.0 bus 1 addr 6 S/N 0001A2B3 (USB direct)"),
    }
    if kind not in entries:
        return
    data, label = entries[kind]
    d.port_box.blockSignals(True)
    d.port_box.clear()
    d.port_box.addItem(label, data)
    d.port_box.setCurrentIndex(0)
    d.port_box.blockSignals(False)
    d._port_changed()


@shot("flash-dialog", FOLDER, size=(820, 300),
      what="ECU read / write with an EDC15 file open, K-line and an FTDI cable")
def flash_dialog(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    return _flash_dialog(ctx, w, "EDC15", "kline", "ftdi")


@shot("flash-standalone", FOLDER, size=(840, 335),
      what="The OVT Flasher (no main window): the File row and Help / About")
def flash_standalone(ctx):
    return _flash_dialog(ctx, None, "EDC15", "kline", "ftdi", standalone=True,
                         project=ctx.project(ctx.ref("edc15_bd")))


@shot("flash-edc15", FOLDER, size=(820, 340),
      what="EDC15 selected, Advanced expanded (boot mode, internal ROM)")
def flash_edc15(ctx):
    w = ctx.main_window(ctx.ref("edc15_bd"))
    return _flash_dialog(ctx, w, "EDC15", "kline", "ftdi", advanced=True)


@shot("flash-edc16", FOLDER, size=(820, 440),
      what="EDC16 with a file open: the Blocks row, the block write and full "
           "read, Advanced expanded (the diagnostic probes and the GPT test)")
def flash_edc16(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    return _flash_dialog(ctx, w, "EDC16", "kline", "ftdi", advanced=True)


@shot("flash-zf6hp", FOLDER, size=(820, 300),
      what="ZF6HP with a 6HP19 file open: TP 2.0, Force full write")
def flash_zf6hp(ctx):
    w = ctx.main_window(ctx.ref("zf6hp19"))
    return _flash_dialog(ctx, w, "ZF6HP", "can", "openport")


@shot("flash-zf6hp-uds", FOLDER, size=(820, 330),
      what="ZF6HP with a 6HP28 file open: UDS, Calibration only")
def flash_zf6hp_uds(ctx):
    return _flash_dialog(ctx, None, "ZF6HP", "can", "openport", primary=zf6hp28_file())


@shot("flash-zf6hp-transport", FOLDER, size=(820, 330),
      what="ZF6HP with Advanced open: the Transport override (Automatic)")
def flash_zf6hp_transport(ctx):
    w = ctx.main_window(ctx.ref("zf6hp19"))
    return _flash_dialog(ctx, w, "ZF6HP", "can", "openport", advanced=True)


@shot("flash-climatronic", FOLDER, size=(820, 270),
      what="Climatronic: write only (no OBD read)")
def flash_climatronic(ctx):
    return _flash_dialog(ctx, None, "Climatronic", "can", "openport")


@shot("flash-vdo", FOLDER, size=(820, 280),
      what="The VDO K-line cluster entry with a cluster EEPROM open")
def flash_vdo(ctx):
    w = ctx.main_window(ctx.ref("vdo_vwk501"))
    return _flash_dialog(ctx, w, "Cluster (VDO K-line)", "kline", "ftdi")


@shot("flash-pair-head-unit", FOLDER, size=(560, 400),
      what="Pair a head unit: address, port, PIN and a name")
def flash_pair(ctx):
    from ovtsuite.flash.gui_pair import PairHeadUnitDialog
    return ctx.keep(PairHeadUnitDialog(None))
