# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Standalone bench ECU simulator tool.

A window of its own (like the Flasher / Sniffer / Logger): pick a J2534 device,
a module profile and an image, press Start, and this device answers on the CAN
bus as that module so a third-party tool's command/read sequence can be
captured. It never flashes anything -- it only replies and logs. The engine is
:mod:`ovtsuite.flash.ecu_sim`.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import branding
from .. import appsettings as _appsettings
from . import ecu_sim as _sim
from . import trx_sim as _trx
from . import trx_kline_sim as _kline
from . import j2534 as _j
from .gui_flash import _can_devices, open_j2534, kline_devices, is_j2534


def _as_bool(v, default=False):
    """Coerce a QSettings value (which may be a str like 'true') to bool."""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

_RATES = [500000, 250000, 125000, 1000000, 100000, 83333]
#: K-line init bauds (the sim answers at this rate until a baud-raise request).
_KLINE_BAUDS = [10400, 9600, 4800, 57600, 115200]

# Modules whose wire stack is KWP2000 over TP 2.0 / K-line (the trx_sim engine),
# rather than UDS over ISO-TP (the ecu_sim engine). Keyed by profile name.
_TRX_PROFILES = set(_trx.profile_names())
#: Profiles that can be simulated over K-line (serial) via trx_kline_sim, mapped
#: to the make_responder ECU kind. The 09D (trx) profiles and the ZF 6HP19
#: ("gs19", an ecu_sim profile) both reuse their existing TP2.0/CAN responders.
_KLINE_ECUS = {name: "aisin09d" for name in _TRX_PROFILES}
if "gs19" in set(_sim.profile_names()):
    _KLINE_ECUS["gs19"] = "gs19"


def _is_trx(profile_name: str) -> bool:
    return profile_name in _TRX_PROFILES


def _kline_ok(profile_name: str) -> bool:
    """True if this module can run over the K-line serial sim."""
    return profile_name in _KLINE_ECUS


class SimWorker(QtCore.QThread):
    """Runs the ISO-TP responder on the chosen J2534 device until stopped."""
    line = QtCore.Signal(str)
    started_ok = QtCore.Signal()
    stopped = QtCore.Signal(int)
    failed = QtCore.Signal(str)

    def __init__(self, dev, rate, profile_name, image_path, faithful, strict_key,
                 erase_delay=0.0, erase_pending=True, transport="iso-tp",
                 timing=None, kline_ecu=None, echo=True):
        super().__init__()
        self._dev = dev                 # J2534 device (CAN) or, for kline, a serial-port name
        self._rate = rate
        self._profile_name = profile_name
        self._image_path = image_path
        self._faithful = faithful
        self._strict_key = strict_key
        self._erase_delay = erase_delay
        self._erase_pending = erase_pending
        self._timing = timing           # ecu_sim.SimTiming (real-car delays/gaps) or None
        self._transport = transport     # "iso-tp" (ecu_sim) | "tp20" (trx CAN) | "kline" (serial)
        self._kline_ecu = kline_ecu     # "aisin09d" | "gs19" (which responder over K-line)
        self._echo = echo               # half-duplex K-line wire echoes our TX
        self._stop = threading.Event()
        self._can = None
        self.log_path = None

    def request_stop(self):
        self._stop.set()

    def _open_logfile(self):
        import datetime as _dt
        try:
            from .. import recent as _recent
            d = _recent.get_last_dir("log")
        except Exception:
            d = ""
        if not d or not os.path.isdir(d):
            d = os.path.expanduser("~")
        path = os.path.join(d, "ecu-sim-%s.log" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        fh = open(path, "w", encoding="utf-8")
        self.log_path = path
        return fh

    def run(self):
        ch = None
        logf = None
        try:
            logf = self._open_logfile()
        except Exception:
            logf = None

        def emit(s):
            self.line.emit(s)
            if logf is not None:
                try:
                    logf.write(s + "\n")
                    logf.flush()
                except Exception:
                    pass

        try:
            if self.log_path:
                emit("Session log: %s" % self.log_path)
            if self._transport == "kline":
                self._run_kline(emit)
                return
            if _is_trx(self._profile_name):
                self._run_trx(emit)
                return
            prof = _sim.profile(self._profile_name)
            image = b""
            if self._image_path:
                try:
                    image = _sim._load_image(self._image_path)
                    emit("Serving image: %s (%d bytes)"
                         % (os.path.basename(self._image_path), len(image)))
                except Exception as ex:
                    emit("(could not load image, serving zeros: %s)" % ex)
            emit("Opening CAN on the selected J2534 device (ISO-TP ECU responder)...")
            self._can = open_j2534(self._dev)
            ch = self._can.connect(_j.CAN, self._rate)
            emit("Channel: %s @ %d baud" % (type(ch).__name__, self._rate))
            # Set the RX acceptance filters robustly: only the in-process vendor
            # Channel has pass_ids; the 32-bit bridge and the OpenPort channel do
            # not, so we must not rely on it (that was why the first attempt
            # received nothing). set_rx_filters picks whatever the channel offers.
            # First: does this device hear ANY bus traffic? Distinguishes a
            # silent sim from a device that is not on the bus. Then the filter.
            _sim.bus_check(ch, log=emit)
            _sim.set_rx_filters(ch, [prof.rx_id, prof.func_id], log=emit)
            try:
                ch.clear_rx()
            except Exception:
                pass
            io = _sim.IsoTpEcu(ch, rx_id=prof.rx_id, tx_id=prof.tx_id,
                               func_id=prof.func_id, log=emit, timing=self._timing)
            if self._timing is not None and self._timing.active():
                emit("Real-car timing: cf_gap=%.0fms latency=%.0fms jitter=%.0fms "
                     "drop_cf=%.0f%% drop_resp=%.0f%%"
                     % (self._timing.cf_gap_ms, self._timing.resp_latency_ms,
                        self._timing.jitter_ms, self._timing.drop_cf_prob * 100,
                        self._timing.drop_resp_prob * 100))
            responder = _sim.EcuResponder(
                prof, image=image,
                read_mode=("faithful" if self._faithful else "serve"),
                strict_key=self._strict_key, log=emit,
                capture_dir=(os.path.dirname(self.log_path) if self.log_path else None),
                erase_delay_s=self._erase_delay, erase_pending=self._erase_pending)
            sim = _sim.Simulator(io, responder, log=emit)
            self.started_ok.emit()
            sim.run(is_stop=self._stop.is_set)
            emit("Stopped -- %d request(s) answered, %d frame(s) seen on matched ids."
                 % (sim.count, io.frames_seen))
            self.stopped.emit(sim.count)
        except Exception as ex:
            emit("ERROR: %s" % ex)
            self.failed.emit(str(ex))
        finally:
            try:
                if ch is not None:
                    ch.disconnect()
            except Exception:
                pass
            # Close the PassThru / 32-bit bridge too -- disconnecting the channel
            # alone leaves the bridge runner holding the device open, so the next
            # run (or another tool) cannot acquire it.
            try:
                if self._can is not None:
                    self._can.close()
            except Exception:
                pass
            self._can = None
            if logf is not None:
                try:
                    logf.close()
                except Exception:
                    pass

    def _run_trx(self, emit):
        """Aisin TRx (09D) responder: KWP2000 over VW TP 2.0 (CAN) or K-line.
        Owns its own channel lifecycle (run()'s finally closes self._can)."""
        prof = _trx.profile(self._profile_name)
        image = b""
        if self._image_path:
            try:
                image = _trx.load_image(self._image_path)
                emit("Serving image: %s (%d bytes)"
                     % (os.path.basename(self._image_path), len(image)))
            except Exception as ex:
                emit("(could not load image, serving zeros: %s)" % ex)
        responder = _trx.Trx09dResponder(
            prof, image=image,
            read_mode=("faithful" if self._faithful else "serve"),
            strict_key=self._strict_key, log=emit,
            capture_dir=(os.path.dirname(self.log_path) if self.log_path else None),
            erase_delay_s=self._erase_delay, erase_pending=self._erase_pending)
        # (K-line is handled by _run_kline via run()'s dispatch; _run_trx is
        # only reached for the CAN/TP2.0 transport.)
        ch = None
        io = None
        ecu_rx = 0x740
        try:
            emit("Opening CAN on the selected J2534 device (TRx KWP2000/TP2.0 responder)...")
            self._can = open_j2534(self._dev)
            # Over the 32-bit bridge, run the TP2.0 transport IN-BRIDGE: one IPC
            # per KWP message instead of ~40 per-frame round-trips, so a full read
            # dump isn't throttled to a crawl by per-frame IPC latency. The
            # responder (image, security) still runs here. Falls back to the
            # client-side loop if this bridge build has no trx_sim.
            in_bridge = isinstance(self._can, _j.BridgePassThru)
            if in_bridge:
                try:
                    io = self._can.open_tp20_ecu(baud=self._rate, ecu_rx=ecu_rx,
                                                 dest=prof.tp20_dest, timing=self._timing)
                    emit("TP2.0 responder running IN-BRIDGE (one IPC per message); "
                         "waiting for a channel-setup on 0x200 (any address)...")
                except _j.J2534Error as ex:
                    emit("(in-bridge responder unavailable: %s -- using client loop)" % ex)
                    io = None
            if io is None:
                ch = self._can.connect(_j.CAN, self._rate)
                emit("Channel: %s @ %d baud" % (type(ch).__name__, self._rate))
                _sim.bus_check(ch, log=emit)
                # admit the TP2.0 setup broadcast (0x200) and the id the tester
                # sends channel data to us on (our assigned ecu_rx). dest=None
                # answers a setup for ANY logical address and logs which was used.
                _sim.set_rx_filters(ch, [_trx._SETUP_ID, ecu_rx], log=emit)
                try:
                    ch.clear_rx()
                except Exception:
                    pass
                io = _trx.Tp20Ecu(ch, dest=prof.tp20_dest, ecu_rx_id=ecu_rx, log=emit,
                                  timing=self._timing)
                emit("TP2.0 responder ready: waiting for a channel-setup on 0x200 "
                     "(any logical address)...")
            sim = _trx.Simulator(io, responder, log=emit)
            self.started_ok.emit()
            sim.run(is_stop=self._stop.is_set)
            emit("Stopped -- %d request(s) answered, %d frame(s) seen."
                 % (sim.count, getattr(io, "frames_seen", 0)))
            self.stopped.emit(sim.count)
        finally:
            try:
                if io is not None and hasattr(io, "close") and isinstance(io, _j.BridgeTp20Ecu):
                    io.close()
            except Exception:
                pass
            try:
                if ch is not None:
                    ch.disconnect()
            except Exception:
                pass

    def _run_kline(self, emit):
        """K-line (serial) responder for the 09D or 6HP19, via trx_kline_sim.
        ``self._dev`` is a serial-port name (COMx / ttyUSBx / FTDI:<serial>) and
        ``self._rate`` is the init baud. Reuses the module's existing TP2.0/CAN
        responder over K-line framing (see trx_kline_sim.make_responder)."""
        ecu = self._kline_ecu or _KLINE_ECUS.get(self._profile_name, "aisin09d")
        image = b""
        if self._image_path:
            try:
                if ecu == "aisin09d":
                    image, var = _kline.load_09d_image(self._image_path)
                    emit("Serving 09D variant %s: %s (%d bytes, cal 0x%X..0x%X)"
                         % (var.name, os.path.basename(self._image_path), len(image),
                            var.rng[0], var.rng[1]))
                else:
                    image = open(self._image_path, "rb").read()
                    emit("Serving %s: %s (%d bytes)"
                         % (ecu, os.path.basename(self._image_path), len(image)))
            except Exception as ex:
                emit("(could not load image, serving zeros: %s)" % ex)
        responder, ctrl = _kline.make_responder(
            ecu, image, read_mode=("faithful" if self._faithful else "serve"), log=emit)
        port = None
        try:
            from .transport import KLine
            emit("Opening K-line on %s @ %d baud (ECU responder, %s wire)..."
                 % (self._dev, self._rate, "shared/echo" if self._echo else "full-duplex"))
            port = KLine(self._dev, baud=self._rate)
            ecu_io = _kline.KLineEcu(port, responder, controller=ctrl, echo=self._echo,
                                     baud_raise=_kline.BaudRaise(), log=emit)
            self.started_ok.emit()
            ecu_io.run()
            emit("Stopped -- %d request(s) answered." % ecu_io.count)
            self.stopped.emit(ecu_io.count)
        finally:
            try:
                if port is not None and hasattr(port, "close"):
                    port.close()
            except Exception:
                pass


class SimulatorDialog(QtWidgets.QDialog):
    """The simulator's own window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(branding.TOOL_NAMES.get("simulator", "OVT Simulator"))
        self.resize(760, 520)
        self._worker: Optional[SimWorker] = None

        v = QtWidgets.QVBoxLayout(self)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Device:"))
        self.dev_box = QtWidgets.QComboBox()
        self.dev_box.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.dev_box, 1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_devices)
        row.addWidget(self.refresh_btn)
        row.addWidget(QtWidgets.QLabel("Baud:"))
        self.rate_box = QtWidgets.QComboBox()
        for r in _RATES:
            self.rate_box.addItem("%d" % r, r)
        row.addWidget(self.rate_box)
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.clicked.connect(self._toggle)
        row.addWidget(self.start_btn)
        v.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Module:"))
        self.profile_box = QtWidgets.QComboBox()
        for name in _sim.profile_names():               # ecu_sim (UDS/ISO-TP)
            self.profile_box.addItem(_sim.profile(name).title, name)
        for name in _trx.profile_names():               # trx_sim (KWP2000)
            self.profile_box.addItem(_trx.profile(name).title, name)
        self.profile_box.currentIndexChanged.connect(self._on_module_changed)
        row2.addWidget(self.profile_box, 1)
        row2.addWidget(QtWidgets.QLabel("Transport:"))
        self.transport_box = QtWidgets.QComboBox()
        self.transport_box.addItem("CAN", "tp20")
        self.transport_box.addItem("K-line (serial)", "kline")
        # Name only the modules that really have a K-line responder.
        kline_titles = [(_trx.profile(n) if _is_trx(n) else _sim.profile(n)).title
                        for n in sorted(_KLINE_ECUS)]
        self.transport_box.setToolTip(
            "Wire stack. CAN for the TP2.0 (Aisin TRx) and UDS (ZF 6HP28) modules. "
            "K-line (serial, FTDI/COM) for %s -- the device box then lists serial "
            "ports. Strict key, erase delay, responsePending and real-car timing "
            "apply on CAN only." % (", ".join(kline_titles) or "no module"))
        self.transport_box.currentIndexChanged.connect(self._on_module_changed)
        row2.addWidget(self.transport_box)
        self.echo_chk = QtWidgets.QCheckBox("Shared K-line (echo)")
        self.echo_chk.setChecked(True)
        self.echo_chk.setToolTip(
            "K-line only. On: a shared K-line wire echoes the sim's own TX (two "
            "cables on one K-line) -- the sim swallows its echo. Off: a full-duplex "
            "virtual COM pair (e.g. com0com) with no echo.")
        self.echo_chk.setVisible(False)
        row2.addWidget(self.echo_chk)
        self.faithful_chk = QtWidgets.QCheckBox("Faithful (refuse reads)")
        self.faithful_chk.setToolTip(
            "Off (default): serve reads from the image so the tool completes its "
            "read loop. On: refuse reads exactly as the real module does, to see "
            "where a tool stalls.")
        row2.addWidget(self.faithful_chk)
        self.strict_chk = QtWidgets.QCheckBox("Strict key")
        self.strict_chk.setToolTip(
            "Reject a security key that fails the profile's seed/key algorithm "
            "(default: accept any key and just log match/mismatch).")
        row2.addWidget(self.strict_chk)
        row2.addWidget(QtWidgets.QLabel("Erase delay (s):"))
        self.erase_spin = QtWidgets.QDoubleSpinBox()
        self.erase_spin.setRange(0.0, 120.0)
        self.erase_spin.setSingleStep(0.5)
        self.erase_spin.setDecimals(1)
        self.erase_spin.setValue(0.0)
        self.erase_spin.setToolTip(
            "Hold each eraseMemory (31 01 FF00) reply this many seconds, to see "
            "how the tool under test tolerates a slow erase. 0 = answer at once.")
        row2.addWidget(self.erase_spin)
        self.pending_chk = QtWidgets.QCheckBox("responsePending")
        self.pending_chk.setChecked(True)
        self.pending_chk.setToolTip(
            "During the erase delay, emit 7F 31 78 responsePending keepalives "
            "(on) vs stay silent (off) -- the two ways a tool's wait can be "
            "tested.")
        row2.addWidget(self.pending_chk)
        v.addLayout(row2)

        # --- Real-car timing (bench): reproduce the slow / lossy TCU behaviour ---
        tgb = QtWidgets.QGroupBox("Real-car timing (bench)")
        tgb.setToolTip("Reproduce the slow / lossy behaviour measured on the real "
                       "TCU over the gateway, to exercise a flashing tool against it.")
        tg = QtWidgets.QHBoxLayout(tgb)

        def _tspin(label, lo, hi, step, decimals, tip):
            tg.addWidget(QtWidgets.QLabel(label))
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(decimals)
            sp.setValue(0.0)
            sp.setToolTip(tip)
            tg.addWidget(sp)
            return sp

        self.cfgap_spin = _tspin(
            "CF gap ms", 0.0, 100.0, 1.0, 0,
            "The ECU's own minimum delay between consecutive frames, regardless of "
            "the STmin the tester asks for. The real car held ~10 ms/frame, which is "
            "why a 512 KiB read took ~12 min.")
        self.latency_spin = _tspin(
            "Latency ms", 0.0, 5000.0, 5.0, 0,
            "Delay before each response's first frame (per-request processing time).")
        self.jitter_spin = _tspin(
            "Jitter ms", 0.0, 100.0, 1.0, 0,
            "+/- random jitter applied to the CF gap and the latency.")
        self.dropcf_spin = _tspin(
            "Drop CF %", 0.0, 100.0, 0.5, 1,
            "Chance to drop ONE consecutive frame mid-transfer, forcing a "
            "consecutive-frame timeout in the tester.")
        self.dropresp_spin = _tspin(
            "Drop resp %", 0.0, 100.0, 0.5, 1,
            "Chance to drop an ENTIRE response so the tester sees no reply to a "
            "request -- the gap that aborted a real read at 91%.")
        car_btn = QtWidgets.QPushButton("Match real car")
        car_btn.setToolTip("Set the measured car pacing: 10 ms/frame with 2 ms jitter.")
        car_btn.clicked.connect(lambda: (self.cfgap_spin.setValue(10.0),
                                         self.jitter_spin.setValue(2.0)))
        tg.addWidget(car_btn)
        tg.addStretch(1)
        v.addWidget(tgb)
        self.timing_box = tgb

        row3 = QtWidgets.QHBoxLayout()
        row3.addWidget(QtWidgets.QLabel("Image:"))
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("optional .frf / .6hp / .bin to serve reads from (blank = zeros)")
        row3.addWidget(self.path_edit, 1)
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row3.addWidget(browse)
        v.addLayout(row3)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))
        v.addWidget(self.log, 1)

        btns = QtWidgets.QHBoxLayout()
        self.clear_btn = QtWidgets.QPushButton("Clear log")
        self.clear_btn.clicked.connect(self.log.clear)
        btns.addWidget(self.clear_btn)
        self.save_btn = QtWidgets.QPushButton("Save log...")
        self.save_btn.clicked.connect(self._save_log)
        btns.addWidget(self.save_btn)
        btns.addStretch(1)
        self.status = QtWidgets.QLabel("Idle. (The session is also auto-saved to a log file.)")
        btns.addWidget(self.status)
        v.addLayout(btns)

        self._refresh_devices()
        self._load_settings()
        self._on_module_changed()

    #: QSettings group for this dialog's last-used selections.
    _SKEY = "simulator"

    def _save_settings(self):
        """Remember the current selections so the next launch starts where the
        last one left off."""
        try:
            s = _appsettings.settings()
            s.beginGroup(self._SKEY)
            s.setValue("device", self.dev_box.currentText())
            s.setValue("baud", self.rate_box.currentData())
            s.setValue("module", self.profile_box.currentData())
            s.setValue("transport", self.transport_box.currentData())
            s.setValue("faithful", self.faithful_chk.isChecked())
            s.setValue("strict", self.strict_chk.isChecked())
            s.setValue("erase_delay", self.erase_spin.value())
            s.setValue("response_pending", self.pending_chk.isChecked())
            s.setValue("cf_gap_ms", self.cfgap_spin.value())
            s.setValue("latency_ms", self.latency_spin.value())
            s.setValue("jitter_ms", self.jitter_spin.value())
            s.setValue("drop_cf_pct", self.dropcf_spin.value())
            s.setValue("drop_resp_pct", self.dropresp_spin.value())
            s.setValue("image", self.path_edit.text().strip())
            s.endGroup()
            s.sync()
        except Exception:
            pass

    def _load_settings(self):
        """Restore the last-used selections. Each is best-effort: a saved device
        or module that is no longer present is simply skipped."""
        try:
            s = _appsettings.settings()
            s.beginGroup(self._SKEY)
            dev = s.value("device")
            if dev:
                i = self.dev_box.findText(dev)
                if i >= 0:
                    self.dev_box.setCurrentIndex(i)
            baud = s.value("baud")
            if baud is not None:
                try:
                    i = self.rate_box.findData(int(baud))
                    if i >= 0:
                        self.rate_box.setCurrentIndex(i)
                except (TypeError, ValueError):
                    pass
            module = s.value("module")
            if module:
                i = self.profile_box.findData(module)
                if i >= 0:
                    self.profile_box.setCurrentIndex(i)
            transport = s.value("transport")
            if transport:
                i = self.transport_box.findData(transport)
                if i >= 0:
                    self.transport_box.setCurrentIndex(i)
            self.faithful_chk.setChecked(_as_bool(s.value("faithful")))
            self.strict_chk.setChecked(_as_bool(s.value("strict")))
            ed = s.value("erase_delay")
            if ed is not None:
                try:
                    self.erase_spin.setValue(float(ed))
                except (TypeError, ValueError):
                    pass
            self.pending_chk.setChecked(_as_bool(s.value("response_pending"), True))
            for key, spin in (("cf_gap_ms", self.cfgap_spin),
                              ("latency_ms", self.latency_spin),
                              ("jitter_ms", self.jitter_spin),
                              ("drop_cf_pct", self.dropcf_spin),
                              ("drop_resp_pct", self.dropresp_spin)):
                val = s.value(key)
                if val is not None:
                    try:
                        spin.setValue(float(val))
                    except (TypeError, ValueError):
                        pass
            img = s.value("image")
            if img:
                self.path_edit.setText(img)
            s.endGroup()
        except Exception:
            pass

    def _on_module_changed(self, *_):
        """Enable the transport selector for modules that offer a choice (the
        Aisin TRx: CAN TP2.0 vs K-line; the 09D / ZF 6HP19: CAN vs K-line serial).
        Pure UDS modules (ZF 6HP28 etc.) are CAN/ISO-TP only. When K-line is
        chosen, the device box lists serial ports and the baud box switches to
        K-line bauds."""
        prof = self.profile_box.currentData() or ""
        can_choose = _is_trx(prof) or _kline_ok(prof)
        self.transport_box.setEnabled(can_choose)
        is_kline = can_choose and (self.transport_box.currentData() == "kline")
        self._set_io_mode(is_kline)
        # The K-line responder (trx_kline_sim) takes none of these: grey them
        # out rather than let them look as if they did something.
        for w in (self.strict_chk, self.erase_spin, self.pending_chk, self.timing_box):
            w.setEnabled(not is_kline)

    def _fill_rates(self, values, default):
        cur = self.rate_box.currentData()
        self.rate_box.blockSignals(True)
        self.rate_box.clear()
        for r in values:
            self.rate_box.addItem("%d" % r, r)
        i = self.rate_box.findData(cur if cur in values else default)
        if i >= 0:
            self.rate_box.setCurrentIndex(i)
        self.rate_box.blockSignals(False)

    def _set_io_mode(self, kline: bool):
        """Point the Device + Baud boxes at serial ports / K-line bauds (K-line)
        or J2534 CAN devices / CAN rates (otherwise)."""
        if getattr(self, "_io_kline", None) == kline:
            return                                   # already in this mode
        self._io_kline = kline
        self.echo_chk.setVisible(kline)
        self.dev_box.clear()
        if kline:
            # The SAME K-line picker the flasher uses, minus J2534 devices (the
            # sim needs raw serial for ECU-mode, which a J2534 device can't give).
            try:
                for data, label in kline_devices():
                    if not is_j2534(data):
                        self.dev_box.addItem(label, data)
            except Exception as ex:
                self._log("(could not list K-line cables: %s)" % ex)
            if self.dev_box.count() == 0:
                self.dev_box.addItem("(no serial / FTDI cable found)", None)
            self._fill_rates(_KLINE_BAUDS, 10400)
        else:
            self._refresh_devices()
            self._fill_rates(_RATES, 500000)

    def _refresh_devices(self):
        self.dev_box.clear()
        try:
            for data, label in _can_devices():
                self.dev_box.addItem(label, data)
        except Exception as ex:
            self._log("(could not list devices: %s)" % ex)
        if self.dev_box.count() == 0:
            self.dev_box.addItem("(no J2534 device found)", None)

    def _browse(self):
        from .. import recent as _recent
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose an image to serve", _recent.get_last_dir(),
            "ECU images (*.frf *.6hp *.sgo *.09d *.bin);;All files (*)")
        if path:
            _recent.set_last_dir(os.path.dirname(path))
            self.path_edit.setText(path)

    def _log(self, s: str):
        self.log.appendPlainText(s)

    def _save_log(self):
        from .. import recent as _recent
        import datetime as _dt
        default = os.path.join(_recent.get_last_dir("log"),
                               "ecu-sim-%s.txt" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save simulator log", default, "Text files (*.txt);;All files (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.log.toPlainText() + "\n")
                _recent.set_last_dir(os.path.dirname(path), "log")
                self.status.setText("Saved %s" % os.path.basename(path))
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, "Save log", "Could not save: %s" % ex)

    def _set_running(self, running: bool):
        self.start_btn.setText("Stop" if running else "Start")
        for w in (self.dev_box, self.refresh_btn, self.rate_box, self.profile_box,
                  self.transport_box, self.echo_chk, self.faithful_chk, self.strict_chk,
                  self.path_edit, self.erase_spin, self.pending_chk, self.timing_box):
            w.setEnabled(not running)       # read once at Start; changes later do nothing
        if not running:
            self._on_module_changed()        # re-apply transport enable rule

    def _toggle(self):
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("Stopping...")
            self._worker.request_stop()
            return
        mode = "faithful (reads refused)" if self.faithful_chk.isChecked() else "serve (reads answered)"
        profile_name = self.profile_box.currentData() or "6hp28"
        kline_cap = _is_trx(profile_name) or _kline_ok(profile_name)
        tdata = self.transport_box.currentData() if kline_cap else None
        if tdata == "kline":
            transport = "kline"
        elif _is_trx(profile_name):
            transport = "tp20"
        else:
            transport = "iso-tp"
        kline_ecu = None
        if transport == "kline":
            dev = self.dev_box.currentData()
            if dev is None:
                QtWidgets.QMessageBox.warning(
                    self, "Simulator",
                    "Select a serial / FTDI cable for the K-line sim.")
                return
            kline_ecu = _KLINE_ECUS.get(profile_name, "aisin09d")
            msg = ("This serial port will ANSWER as %s over K-line, in %s mode, so "
                   "the flasher's K-line path can be developed against it. It is a "
                   "responder, not a real module -- nothing is flashed.\n\n"
                   "Wire the sim's K-line to the tester (one K-line wire + ground, "
                   "12 V present), or use a crossed virtual COM pair and uncheck "
                   "'Shared K-line'. Start now?"
                   % (self.profile_box.currentText(), mode))
        else:
            dev = self.dev_box.currentData()
            if dev is None:
                QtWidgets.QMessageBox.warning(self, "Simulator", "Select a J2534 device.")
                return
            wire = {"tp20": "CAN (TP 2.0)"}.get(transport, "CAN (ISO-TP)")
            msg = ("This device will ANSWER on the bus as %s over %s, in %s mode, so "
                   "another tool's sequence can be captured. It is a responder, not "
                   "a real module -- nothing is flashed.\n\n"
                   "Put this device on the same CAN as the tool (CANH/CANL, 120 ohm "
                   "at each end) with 12 V present. Start now?"
                   % (self.profile_box.currentText(), wire, mode))
        if QtWidgets.QMessageBox.question(
                self, "Start simulator", msg,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes) != QtWidgets.QMessageBox.Yes:
            return
        from . import ecu_sim as _sim
        timing = _sim.SimTiming(
            cf_gap_ms=self.cfgap_spin.value(),
            resp_latency_ms=self.latency_spin.value(),
            jitter_ms=self.jitter_spin.value(),
            drop_cf_prob=self.dropcf_spin.value() / 100.0,
            drop_resp_prob=self.dropresp_spin.value() / 100.0)
        self._worker = SimWorker(
            dev, self.rate_box.currentData() or (10400 if transport == "kline" else 500000),
            profile_name,
            self.path_edit.text().strip() or None,
            self.faithful_chk.isChecked(), self.strict_chk.isChecked(),
            erase_delay=self.erase_spin.value(),
            erase_pending=self.pending_chk.isChecked(),
            transport=transport,
            timing=timing, kline_ecu=kline_ecu, echo=self.echo_chk.isChecked())
        self._save_settings()                # remember these selections
        self._worker.line.connect(self._log)
        self._worker.started_ok.connect(
            lambda: self.status.setText("Listening -- waiting for the tool..."))
        self._worker.stopped.connect(self._on_stopped)
        self._worker.failed.connect(self._on_failed)
        self._set_running(True)
        self.status.setText("Starting...")
        self._worker.start()

    def _on_stopped(self, count: int):
        self._set_running(False)
        self.status.setText("Stopped -- %d request(s) answered." % count)

    def _on_failed(self, msg: str):
        self._set_running(False)
        self.status.setText("Error.")
        QtWidgets.QMessageBox.critical(self, "Simulator", "Could not run the simulator:\n\n%s" % msg)

    def closeEvent(self, ev):
        self._save_settings()                # persist the latest selections
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(3000)
        super().closeEvent(ev)
