# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the data logger, bus sniffer and bench ECU simulator pages.

Hardware-free states only: every dialog is shown as a user sees it before a
cable is connected, with real binaries opened to name the cells and the real
6HP19 drive log (``log_6hp19``) loaded into the log viewer.
"""
from help_screenshots import Skip, shot

LOG = "data/help"


# ------------------------------------------------------------------ helpers
def _transport(conn, kind):
    """Pick K-line or CAN explicitly: the bar restores the last choice, and
    the recipes share one settings store while they run."""
    conn.transport_box.setCurrentIndex(conn.transport_box.findData(kind))


def _cable(*conns):
    """One example cable in each bar's Port / Device list, as the flasher
    recipes do (no cable is attached here; nothing is opened). Last, once
    the ECU is picked: picking one rescans the ports."""
    for conn in conns:
        _one_cable(conn)


def _one_cable(conn):
    from ovtsuite.flash import gui_flash
    data, label = (
        (gui_flash.OPENPORT_USB, "OpenPort 2.0 bus 1 addr 6 S/N 0001A2B3 (USB direct)")
        if conn.transport_kind() == "can" else
        ("FTDI:A50285BI", "FTDI:A50285BI \u2014 FT232R USB UART, S/N A50285BI (FTDI direct)"))
    conn.port_box.blockSignals(True)
    conn.port_box.clear()
    conn.port_box.addItem(label, data)
    conn.port_box.setCurrentIndex(0)
    conn.port_box.blockSignals(False)


def _log_dialog(ctx, path=None, ecu=None, transport="kline"):
    """The main window with ``path`` open, and the logger opened from it."""
    from ovtsuite.datalog.gui_log import DataLogDialog
    w = ctx.main_window(path)
    dlg = ctx.keep(DataLogDialog(w))
    _transport(dlg.pane.conn, transport)
    if ecu:
        dlg.pane.conn.ecu_box.setCurrentText(ecu)
    dlg.pane._sync_namer()
    _cable(dlg.pane.conn)
    return dlg


def _preset(dlg, key):
    pane = dlg.pane
    i = pane.preset_box.findData(key)
    if i < 0:
        raise Skip("no preset %r for %s" % (key, pane.conn.ecu()))
    pane.preset_box.setCurrentIndex(i)
    pane._apply_preset()


def _group(dlg, g):
    pane = dlg.pane
    i = pane.group_box.findData(g)
    if i >= 0:
        pane.group_box.setCurrentIndex(i)


# ------------------------------------------------------------------- logger
@shot("log-dialog", LOG, size=(1180, 760),
      what="The logger with an EDC16 file open and the boost preset applied")
def log_dialog(ctx):
    dlg = _log_dialog(ctx, ctx.ref("edc16_lq"), "EDC16")
    _preset(dlg, "boost")
    _group(dlg, 11)
    return dlg


@shot("log-connection", LOG, size=(1180, 120),
      what="The connection row on K-line with an EDC16 selected",
      crop=lambda d: d.pane.conn)
def log_connection(ctx):
    dlg = _log_dialog(ctx, ctx.ref("edc16_lq"), "EDC16")
    return dlg


@shot("log-tcu-preset", LOG, size=(1180, 760),
      what="Logging a ZF 6HP19 over CAN with the shift-map discovery preset")
def log_tcu_preset(ctx):
    dlg = _log_dialog(ctx, ctx.ref("zf6hp19"), "ZF6HP", "can")
    _preset(dlg, "discover")
    _group(dlg, 1)
    return dlg


@shot("log-dual", LOG, size=(1180, 760),
      what="Dual logging: an engine tab and a transmission tab")
def log_dual(ctx):
    from ovtsuite.datalog.gui_log import DataLogDialog
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ctx.open_project(w, ctx.ref("zf6hp19"))
    dlg = ctx.keep(DataLogDialog(w))
    _transport(dlg.pane.conn, "kline")
    dlg.pane.conn.ecu_box.setCurrentText("EDC16")
    dlg.pane._sync_namer()
    dlg.dual_chk.setChecked(True)
    second = dlg._second
    conn = second.conn
    _transport(conn, "can")
    conn.ecu_box.setCurrentText("ZF6HP")
    second._sync_namer()
    i = second.preset_box.findData("shift")
    if i >= 0:
        second.preset_box.setCurrentIndex(i)
        second._apply_preset()
    dlg.tabs.setCurrentWidget(second)
    _cable(dlg.pane.conn, conn)
    return dlg


@shot("log-viewer", LOG, size=(1180, 760),
      what="The Log file tab with a real 6HP19 drive log")
def log_viewer(ctx):
    from ovtsuite.datalog import logfile
    dlg = _log_dialog(ctx, None)
    log = logfile.load(ctx.ref("log_6hp19"))
    dlg.show_log(log)
    dlg.tabs.setCurrentWidget(dlg.viewer)
    row = min(420, max(0, log.rows - 1))
    dlg.view_chart.cursor = row
    dlg._cursor_row(row)
    return dlg


@shot("log-standalone", LOG, size=(1180, 760),
      what="OVT Logger on its own, with a project opened in it")
def log_standalone(ctx):
    from ovtsuite.datalog.gui_log import DataLogDialog
    from ovtsuite.datalog.standalone import StandaloneLogHost
    dlg = ctx.keep(DataLogDialog(StandaloneLogHost()))
    _transport(dlg.pane.conn, "kline")
    if not dlg.open_binary(ctx.project(ctx.ref("edc16_lq"))):
        raise Skip("could not open the EDC16 project")
    dlg.pane.conn.ecu_box.setCurrentText("EDC16")
    dlg.pane._sync_namer()
    _cable(dlg.pane.conn)
    return dlg


@shot("log-codes", LOG, size=(640, 170),
      what="Reading fault codes from the ECU")
def log_codes(ctx):
    from ovtsuite.datalog.gui_log import EcuCodesDialog
    w = ctx.main_window(ctx.ref("edc16_lq"))
    dlg = ctx.keep(EcuCodesDialog(w))
    _transport(dlg.conn, "kline")
    dlg.conn.ecu_box.setCurrentText("EDC16")
    _cable(dlg.conn)
    return dlg


# ------------------------------------------------------------------ sniffer
def _sample_session():
    """A short TP 2.0 diagnostic session (the same frames the decoder's own
    tests use): channel setup, parameters, an identification request and its
    multi-frame answer, a keep-alive and the disconnect."""
    from ovtsuite.sniff import capture as C
    t = 1760000000.0
    out = []

    def add(cid, hexs, dt=0.002):
        nonlocal t
        t += dt
        out.append(C.Record(t, "can", cid, bytes.fromhex(hexs.replace(" ", ""))))
    add(0x200, "01 C0 00 10 00 03 01")
    add(0x201, "00 D0 00 03 40 07 01")
    add(0x740, "A0 0F 8A FF 32 FF")
    add(0x300, "A1 0F 8A FF 4A FF")
    add(0x740, "10 00 02 1A 9B")
    add(0x300, "B1")
    add(0x300, "20 00 13 5A 9B 30 33 38")
    add(0x300, "21 39 30 36 30 31 36 41")
    add(0x300, "12 42 20 20 34 39 39 39")
    add(0x740, "B3")
    add(0x280, "08 1C 0C 0C 00 1A 1A 00", 0.004)
    add(0x740, "11 00 02 21 01", 0.01)
    add(0x300, "B2")
    add(0x300, "21 00 0C 61 01 01 C8 1A")
    add(0x300, "12 25 00 0A 05 8A 10 00")
    add(0x740, "B3")
    add(0x740, "A3", 0.5)
    add(0x300, "A1 0F 8A FF 4A FF")
    add(0x740, "A8", 0.2)
    return out


@shot("sniff-dialog", LOG, size=(1180, 720),
      what="The bus sniffer showing a decoded TP 2.0 session")
def sniff_dialog(ctx):
    from ovtsuite.sniff.gui_sniff import SnifferDialog
    dlg = ctx.keep(SnifferDialog(None))
    dlg._clear()
    dlg._parse_filter()
    dlg._ingest(_sample_session(), live=False)
    dlg.status.setText("sample session: %d records, %d rows shown, %d TP 2.0 messages decoded"
                       % (len(dlg._records), dlg.model.rowCount(), dlg.msgs.rowCount()))
    return dlg


@shot("sniff-serial", LOG, size=(1000, 420),
      what="The sniffer set to a passive serial K-line tap")
def sniff_serial(ctx):
    from ovtsuite.sniff.gui_sniff import SnifferDialog
    dlg = ctx.keep(SnifferDialog(None))
    dlg.src_box.setCurrentIndex(1)
    return dlg


# ---------------------------------------------------------------- simulator
def _sim(ctx):
    from ovtsuite.flash.gui_sim import SimulatorDialog
    dlg = ctx.keep(SimulatorDialog(None))
    dlg.transport_box.setCurrentIndex(dlg.transport_box.findData("tp20"))
    i = dlg.profile_box.findData("6hp28")
    if i >= 0:
        dlg.profile_box.setCurrentIndex(i)
    dlg.path_edit.setText("")
    for sp in (dlg.cfgap_spin, dlg.latency_spin, dlg.jitter_spin,
               dlg.dropcf_spin, dlg.dropresp_spin, dlg.erase_spin):
        sp.setValue(0.0)
    dlg.faithful_chk.setChecked(False)
    dlg.strict_chk.setChecked(False)
    return dlg


def _sample_exchange(dlg, image_path):
    """What the log shows when a tool identifies a ZF 6HP28, unlocks it and
    reads memory: the simulator's own engine answering a short request list
    (no bus involved), its lines put in the dialog's log."""
    from ovtsuite.flash import crypto
    from ovtsuite.flash import ecu_sim as s
    prof = s.profile("6hp28")
    lines = []
    resp = s.EcuResponder(prof, image=s._load_image(image_path), log=lines.append)

    class _Io:
        rx_id, tx_id, func_id = prof.rx_id, prof.tx_id, prof.func_id
        last_rx_id, timing, frames_seen = None, None, 0

        def send(self, _b):
            pass
    sim = s.Simulator(_Io(), resp, log=lines.append)
    lines.append("%s simulator listening (rx 0x%03X / func 0x%03X -> tx 0x%03X); %s reads"
                 % (prof.title, prof.rx_id, prof.func_id, prof.tx_id, resp.read_mode))
    for req in ("10 03", "22 F1 87", "22 F1 89", "27 03"):
        sim.step(bytes.fromhex(req))
    sim.step(bytes.fromhex("27 04") + crypto.zf6hp_read_key(prof.seed_values[3]))
    sim.step(bytes.fromhex("23 14 00 00 40 00 20"))
    for ln in lines:
        dlg._log(ln)


@shot("sim-dialog", LOG, size=(1060, 560),
      what="The bench ECU simulator answering as a ZF 6HP28")
def sim_dialog(ctx):
    dlg = _sim(ctx)
    i = dlg.profile_box.findData("6hp28")
    if i >= 0:
        dlg.profile_box.setCurrentIndex(i)
    img = ctx.ref("zf6hp28_tdi")
    dlg.path_edit.setText(img)
    _sample_exchange(dlg, img)
    dlg.status.setText("Listening -- waiting for the tool...")
    return dlg


@shot("sim-kline", LOG, size=(1060, 260),
      what="The simulator set to answer as an Aisin TRx over K-line")
def sim_kline(ctx):
    dlg = _sim(ctx)
    i = dlg.profile_box.findData("aisin09d")
    if i < 0:
        raise Skip("no Aisin TRx profile")
    dlg.profile_box.setCurrentIndex(i)
    dlg.transport_box.setCurrentIndex(dlg.transport_box.findData("kline"))
    return dlg
