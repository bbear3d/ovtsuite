# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bus sniffer: raw CAN (all ids) or K-line through a J2534 device (a
registered PassThru DLL on Windows, or a Tactrix OpenPort 2.0 / clone driven
over USB on any OS -- :mod:`ovtsuite.flash.openport`), or a passive K-line
tap through any FTDI/KKL serial cable (a K+DCAN cable in its K position is
exactly that), with live TP 2.0 / KWP2000 decoding, capture files that
replay offline, and a send box for poking an ECU by hand.

The serial tap never writes to the port -- it only reads the bytes both
sides put on the shared K-line and frames them (:mod:`kline`), following a
negotiated link-speed change structurally: when it sees ``50 <session>
<speed>`` it re-opens the port at the rate the speed byte encodes, and drops
back to the start rate after a long silence.  KWHack's ``kline_sniffer.py``
logs load as raw bytes and are framed the same way.

What a J2534 device can and cannot see: on CAN a pass-all filter delivers
every frame on the bus (both directions of another tool's session included);
on K-line the device shares the wire with the other tester so it receives
both directions too, but only as *framed* messages -- the 5-baud init pulse
and bytes that don't form a valid frame at the configured baud are invisible,
and when the other tool raises the baud the sniffer has to follow (the baud
box applies live).  Own transmissions show up as ``-tx`` rows when loopback
is on.
"""
import os
import queue
import threading
import time
from datetime import datetime
from typing import List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..flash import j2534 as J
from . import capture as C
from . import kline as KL
from . import tp20 as T
from .. import branding

OPENPORT_USB = "openport-usb"                 # dev_box data for the pyusb OpenPort route
CAN_RATES = [500000, 250000, 125000, 100000, 83333, 50000, 33333]
KLINE_RATES = [10400, 9600, 38400, 124800, 244898]
_KIND_BG = {"setup": QtGui.QColor(255, 236, 179), "setup-ack": QtGui.QColor(255, 236, 179),
            "param": QtGui.QColor(220, 235, 255), "ack": QtGui.QColor(235, 235, 235),
            "data": QtGui.QColor(225, 245, 225), "message": QtGui.QColor(200, 240, 200),
            "init": QtGui.QColor(255, 236, 179), "kw1281": QtGui.QColor(225, 245, 225)}
_DARK_FG = QtGui.QColor(20, 20, 20)


# ------------------------------------------------------------------ worker
class SnifferWorker(QtCore.QThread):
    frames = QtCore.Signal(list)                 # [capture.Record]
    opened = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    ended = QtCore.Signal()

    def __init__(self, dll, mode, rate, extended=False, loopback=True, protocol=J.ISO14230):
        super().__init__()
        self.dll, self.mode, self.rate = dll, mode, rate
        self.extended, self.loopback, self.protocol = extended, loopback, protocol
        self._stop = False
        self._cmds: "queue.Queue" = queue.Queue()
        self.t0 = None

    def stop(self):
        self._stop = True

    def send(self, can_id: Optional[int], data: bytes):
        self._cmds.put(("send", can_id, bytes(data)))

    def set_rate(self, rate: int):
        self._cmds.put(("rate", rate, None))

    def run(self):
        dev = None
        try:
            try:
                if self.dll == OPENPORT_USB:
                    from ..flash.openport import OpenPort
                    dev = OpenPort()
                elif isinstance(self.dll, J.Device):
                    dev = J.open_device(self.dll)     # in-process or 32-bit bridge
                else:
                    dev = J.PassThru(self.dll)
                if self.mode == "can":
                    flags = J.CAN_ID_BOTH if self.extended else 0
                    ch = dev.connect(J.CAN, self.rate, flags)
                    if self.loopback:
                        ch.set_config(LOOPBACK=1)
                    ch.pass_all(extended=False)
                    if self.extended:
                        ch.pass_all(extended=True)
                else:
                    ch = dev.connect(self.protocol, self.rate,
                                     J.ISO9141_NO_CHECKSUM if self.protocol == J.ISO9141 else 0)
                    if self.loopback:
                        ch.set_config(LOOPBACK=1)
                    ch.pass_all()
                ch.clear_rx()
            except J.J2534Error as ex:
                self.failed.emit(str(ex))
                return
            vb = dev.battery_voltage()
            self.opened.emit("%s at %d%s%s" % (
                "CAN" if self.mode == "can" else "K-line", self.rate,
                (", battery %.1f V" % vb) if vb else "",
                (" via " + dev.label) if hasattr(dev, "label") else ""))
            self.t0 = None
            while not self._stop:
                try:
                    while True:
                        cmd, a, b = self._cmds.get_nowait()
                        if cmd == "send":
                            if self.mode == "can":
                                ch.write_can(a, b, self.extended and a > 0x7FF)
                            else:
                                ch.write(b)
                        elif cmd == "rate":
                            ch.set_baud(a)
                            self.rate = a
                except queue.Empty:
                    pass
                try:
                    msgs = ch.read(64, 50)
                except J.J2534Error as ex:
                    self.failed.emit(str(ex))
                    break
                if not msgs:
                    continue
                now = time.time()
                recs = []
                for m in msgs:
                    if m.rx_status & (J.START_OF_MESSAGE | J.TX_DONE):
                        continue                          # indications, not data
                    if self.t0 is None:
                        self.t0 = (now, m.timestamp)
                    t = self.t0[0] + (m.timestamp - self.t0[1]) / 1e6
                    if self.mode == "can":
                        cid = m.can_id
                        if cid is None:
                            continue
                        recs.append(C.Record(t, "can", cid, m.payload, m.is_tx_echo,
                                             extended=bool(m.tx_flags & J.CAN_29BIT_ID)))
                    else:
                        recs.append(C.Record(t, "kline", 0, m.data, m.is_tx_echo))
                if recs:
                    self.frames.emit(recs)
        finally:
            if dev is not None:
                dev.close()
            self.ended.emit()


# ------------------------------------------------------- serial K-line tap
class SerialTapWorker(QtCore.QThread):
    """Read-only listener on a serial K-line cable. Emits raw byte bursts as
    ``kraw`` records; framing happens in the dialog so offline logs and the
    live tap share one path."""
    frames = QtCore.Signal(list)
    opened = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    baud_changed = QtCore.Signal(int)
    ended = QtCore.Signal()
    IDLE_REVERT_S = 8.0

    def __init__(self, port, baud, follow=True):
        super().__init__()
        self.port_name, self.baud, self.follow = port, int(baud), follow
        self._stop = False
        self._want_baud = None
        self._lock = threading.Lock()

    def stop(self):
        self._stop = True

    def set_baud(self, baud):
        with self._lock:
            self._want_baud = int(baud)

    def run(self):
        from ..flash import ftdi
        direct = ftdi.is_ftdi_name(self.port_name)
        if not direct:
            try:
                import serial
            except Exception:
                self.failed.emit("pyserial is not installed.")
                self.ended.emit()
                return
        ser = None
        try:
            try:
                if direct:                     # 2 ms latency timer, no driver tweaks
                    ser = ftdi.open_port(self.port_name, baudrate=self.baud, timeout=0.005)
                else:
                    ftdi.try_lower_sysfs_latency(self.port_name)
                    ser = serial.Serial(self.port_name, self.baud, timeout=0.005,
                                        rtscts=False, dsrdtr=False)
                try:
                    ser.dtr = False            # never drive anything
                    ser.rts = False
                except Exception:
                    pass
            except Exception as ex:
                self.failed.emit("Cannot open %s: %s" % (self.port_name, ex))
                return
            start_baud = self.baud
            self.opened.emit("serial tap on %s at %d baud%s" % (
                self.port_name, self.baud, " (following speed changes)" if self.follow else ""))
            last_rx = time.time()
            while not self._stop:
                with self._lock:
                    want, self._want_baud = self._want_baud, None
                if want and want != self.baud:
                    ser.baudrate = want
                    self.baud = want
                    self.baud_changed.emit(want)
                data = ser.read(ser.in_waiting or 1)
                now = time.time()
                if data:
                    last_rx = now
                    self.frames.emit([C.Record(now, "kraw", 0, bytes(data))])
                elif self.follow and self.baud != start_baud and now - last_rx > self.IDLE_REVERT_S:
                    ser.baudrate = start_baud
                    self.baud = start_baud
                    self.baud_changed.emit(start_baud)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self.ended.emit()


def _kwp_body(frame: bytes) -> bytes:
    """Service + data of an ISO 14230 frame (header stripped, no checksum)."""
    if not frame:
        return b""
    fmt = frame[0]
    i = 3 if fmt & 0xC0 else 1
    if (fmt & 0x3F) == 0 and i < len(frame):
        i += 1
    return frame[i:]


# --------------------------------------------------------------- frame model
class FrameModel(QtCore.QAbstractTableModel):
    COLS = ["Time", "dt ms", "Bus", "ID", "Len", "Data", "Decoded"]
    CAP = 50000

    def __init__(self):
        super().__init__()
        self.rows: List[tuple] = []          # (Record, text, kind)
        self.t0 = None
        self.last_t = None

    def rowCount(self, _=QtCore.QModelIndex()):
        return len(self.rows)

    def columnCount(self, _=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return self.COLS[sec]
        return None

    def add(self, items):
        if not items:
            return
        if len(self.rows) + len(items) > self.CAP:
            drop = len(self.rows) + len(items) - self.CAP
            self.beginRemoveRows(QtCore.QModelIndex(), 0, drop - 1)
            del self.rows[:drop]
            self.endRemoveRows()
        self.beginInsertRows(QtCore.QModelIndex(), len(self.rows), len(self.rows) + len(items) - 1)
        self.rows.extend(items)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.t0 = self.last_t = None
        self.endResetModel()

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        if not idx.isValid():
            return None
        rec, text, kind, dt = self.rows[idx.row()]
        c = idx.column()
        if role == QtCore.Qt.DisplayRole:
            if c == 0:
                return "%.4f" % ((rec.t - self.t0) if self.t0 is not None else rec.t)
            if c == 1:
                return "%.1f" % (dt * 1000) if dt is not None else ""
            if c == 2:
                return {"kmeta": "k-line", "kraw": "k raw"}.get(rec.bus, rec.bus) + (" tx" if rec.tx else "")
            if c == 3:
                return ("%08X" % rec.can_id if rec.extended else "%03X" % rec.can_id) \
                    if rec.bus == "can" else ""
            if c == 4:
                return str(len(rec.data))
            if c == 5:
                return rec.data.hex(" ").upper()
            if c == 6:
                return text
        if role == QtCore.Qt.BackgroundRole and kind in _KIND_BG:
            return _KIND_BG[kind]
        if role == QtCore.Qt.ForegroundRole and kind in _KIND_BG:
            return _DARK_FG
        if role == QtCore.Qt.FontRole and c == 5:
            f = QtGui.QFont("Consolas" if os.name == "nt" else "Monospace")
            return f
        return None


# ------------------------------------------------------------------ dialog
class SnifferDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bus sniffer (J2534)")
        try:
            from ..gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        self._fit_to_screen(1180, 760)
        self._worker: Optional[SnifferWorker] = None
        self._writer: Optional[C.Writer] = None
        self._records: List[C.Record] = []
        self._decoder = T.Tp20Decoder()
        self._pending: List[tuple] = []
        self._id_filter = None
        v = QtWidgets.QVBoxLayout(self)

        # -- device row, then bus-settings row. These were one row, and its
        # minimum width (~1430 px) set the whole window's: wider than a
        # 1366 px laptop screen, so Start sat off the right edge.
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Source:"))
        self.src_box = QtWidgets.QComboBox()
        self.src_box.addItems(["J2534 device", "Serial K-line tap"])
        self.src_box.setItemData(1, "An FTDI cable, or a K+DCAN cable switched to K, "
                                    "listening on the K-line", QtCore.Qt.ItemDataRole.ToolTipRole)
        self.src_box.currentIndexChanged.connect(self._source_changed)
        row.addWidget(self.src_box)
        self.dev_lbl = QtWidgets.QLabel("  Device:")
        row.addWidget(self.dev_lbl)
        self.dev_box = QtWidgets.QComboBox()
        # a long device name must not widen the window; it elides instead
        self.dev_box.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dev_box.setMinimumContentsLength(24)
        row.addWidget(self.dev_box, 1)
        rb = QtWidgets.QPushButton("Refresh")
        rb.clicked.connect(self._refresh_devices)
        row.addWidget(rb)
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.clicked.connect(self._toggle)
        row.addWidget(self.start_btn)
        v.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.follow_baud = QtWidgets.QCheckBox("Follow speed changes")
        self.follow_baud.setChecked(True)
        self.follow_baud.setToolTip("Re-open the tap at the rate a 50 <session> <speed> response "
                                    "negotiates (ISO 14230-2 speed byte), back after 8 s of silence")
        self.raw_chk = QtWidgets.QCheckBox("Show raw bytes")
        self.raw_chk.setToolTip("Also list the unframed byte bursts of a serial tap")
        self.raw_chk.toggled.connect(self._rebuild_view)
        self.mode_lbl = QtWidgets.QLabel("Mode:")
        row.addWidget(self.mode_lbl)
        self.mode_box = QtWidgets.QComboBox()
        self.mode_box.addItems(["CAN", "K-line"])
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode_box)
        self.rate_lbl = QtWidgets.QLabel("  Bit rate:")
        row.addWidget(self.rate_lbl)
        self.rate_box = QtWidgets.QComboBox()
        self.rate_box.setEditable(True)
        row.addWidget(self.rate_box)
        self.ext_chk = QtWidgets.QCheckBox("29-bit ids")
        row.addWidget(self.ext_chk)
        self.proto_box = QtWidgets.QComboBox()
        self.proto_box.addItems(["ISO 14230", "ISO 9141"])
        row.addWidget(self.proto_box)
        self.loop_chk = QtWidgets.QCheckBox("Show own frames")
        self.loop_chk.setChecked(True)
        row.addWidget(self.loop_chk)
        row.addWidget(self.follow_baud)
        row.addWidget(self.raw_chk)
        row.addStretch(1)
        v.addLayout(row)

        # -- capture row
        row2 = QtWidgets.QHBoxLayout()
        self.rec_chk = QtWidgets.QCheckBox("Record to:")
        self.rec_chk.setChecked(True)
        row2.addWidget(self.rec_chk)
        self.path_edit = QtWidgets.QLineEdit()
        row2.addWidget(self.path_edit, 1)
        pb = QtWidgets.QPushButton("...")
        pb.setMaximumWidth(32)
        pb.clicked.connect(self._pick_path)
        row2.addWidget(pb)
        ob = QtWidgets.QPushButton("Open capture...")
        ob.clicked.connect(self._open_capture)
        row2.addWidget(ob)
        cb = QtWidgets.QPushButton("Clear")
        cb.clicked.connect(self._clear)
        row2.addWidget(cb)
        v.addLayout(row2)

        # -- filter row
        row3 = QtWidgets.QHBoxLayout()
        row3.addWidget(QtWidgets.QLabel("ID filter:"))
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("e.g. 200, 201, 300-3FF  (blank = all)")
        self.filter_edit.editingFinished.connect(self._rebuild_view)
        row3.addWidget(self.filter_edit, 1)
        self.tp_only = QtWidgets.QCheckBox("TP 2.0 traffic only")
        self.tp_only.toggled.connect(self._rebuild_view)
        row3.addWidget(self.tp_only)
        self.follow_chk = QtWidgets.QCheckBox("Follow")
        self.follow_chk.setChecked(True)
        row3.addWidget(self.follow_chk)
        v.addLayout(row3)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.model = FrameModel()
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        for c, w in ((0, 90), (1, 60), (2, 60), (3, 80), (4, 40), (5, 230)):
            self.table.setColumnWidth(c, w)
        split.addWidget(self.table)

        self.msgs = QtWidgets.QTableWidget(0, 5)
        self.msgs.setHorizontalHeaderLabels(["Time", "Channel", "Service", "Len", "KWP message (hex)"])
        self.msgs.verticalHeader().setVisible(False)
        self.msgs.verticalHeader().setDefaultSectionSize(20)
        self.msgs.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        for c, w in ((0, 90), (1, 80), (2, 260), (3, 50)):
            self.msgs.setColumnWidth(c, w)
        self.msgs.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.msgs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        split.addWidget(self.msgs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

        # -- send row
        row4 = QtWidgets.QHBoxLayout()
        row4.addWidget(QtWidgets.QLabel("Send:"))
        self.send_id = QtWidgets.QLineEdit("200")
        self.send_id.setMaximumWidth(90)
        self.send_id.setToolTip("CAN id (hex); ignored on K-line")
        row4.addWidget(self.send_id)
        self.send_data = QtWidgets.QLineEdit("01 C0 00 03 00 03 01")
        self.send_data.setToolTip("Data bytes (hex). K-line: the whole frame incl. header, "
                                  "without checksum (the device appends it)")
        row4.addWidget(self.send_data, 1)
        sb = QtWidgets.QPushButton("Send")
        sb.clicked.connect(self._send)
        row4.addWidget(sb)
        self.send_btn = sb
        tpb = QtWidgets.QPushButton("TP 2.0 setup template")
        tpb.setToolTip("Fill the send box with a channel-setup request to logical ECU 0x01 "
                       "(engine), tester listening on 0x300")
        tpb.clicked.connect(lambda: (self.send_id.setText("200"),
                                     self.send_data.setText("01 C0 00 10 00 03 01")))
        row4.addWidget(tpb)
        self.tp_tpl_btn = tpb
        v.addLayout(row4)

        self.status = QtWidgets.QLabel("Idle.")
        self.status.setStyleSheet("color: gray")
        v.addWidget(self.status)

        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start(150)
        self._framer = KL.Framer()
        self._refresh_devices()
        self._mode_changed()
        self._source_changed()
        self._set_default_path()

    def _serial(self) -> bool:
        return self.src_box.currentIndex() == 1

    def _source_changed(self):
        ser = self._serial()
        self.dev_lbl.setText("  Port:" if ser else "  Device:")
        self.follow_baud.setVisible(ser)
        self.raw_chk.setVisible(ser)
        self.mode_box.setVisible(not ser)
        self.mode_lbl.setVisible(not ser)
        self.loop_chk.setVisible(not ser)
        if ser:
            self.mode_box.setCurrentIndex(1)           # K-line rates in the box
        self._mode_changed()
        self.send_id.setEnabled(not ser and self.mode_box.currentText() == "CAN")
        self.send_data.setEnabled(not ser)
        # The serial tap only listens (it never writes to the port): nothing to send through.
        self.send_btn.setEnabled(not ser)
        self.tp_tpl_btn.setEnabled(not ser)
        self.send_btn.setToolTip("The serial K-line tap only listens; sending needs a J2534 device"
                                 if ser else "Send the frame through the J2534 device")
        self._refresh_devices()

    # ---- setup ----
    def _refresh_devices(self):
        self.dev_box.clear()
        if self._serial():
            from ..flash.gui_flash import list_ports
            ports = list_ports()
            if not ports:
                self.dev_box.addItem("(no serial ports found)", None)
            for dev, label in ports:
                self.dev_box.addItem(label, dev)
            return
        devs = J.list_devices()
        for d in devs:
            if d.loadable:
                note = ""
            elif J.usable(d):
                note = "  (via 32-bit bridge)"
            else:
                note = "  - 32-bit DLL: needs a 32-bit Python (py -3-32) or j2534bridge32.exe"
            self.dev_box.addItem(d.label + note, d)
            if not J.usable(d):
                self.dev_box.model().item(self.dev_box.count() - 1).setEnabled(False)
        # OpenPort 2.0 over USB (pyusb) -- the route on Linux / macOS
        try:
            from ..flash import openport
            ops = openport.find_devices()
        except Exception:
            ops = []
        for label in ops:
            self.dev_box.addItem(label + " (USB direct)", OPENPORT_USB)
        if not devs and not ops:
            self.dev_box.addItem("(no J2534 device registered or OpenPort on USB)", None)

    def _mode_changed(self):
        can = self.mode_box.currentText() == "CAN"
        ser = self._serial() if hasattr(self, "src_box") else False
        self.rate_box.clear()
        self.rate_box.addItems([str(r) for r in (CAN_RATES if can else KLINE_RATES)])
        self.rate_lbl.setText("  Bit rate:" if can else "  Baud:")
        self.ext_chk.setVisible(can and not ser)
        self.proto_box.setVisible(not can and not ser)
        self.send_id.setEnabled(can)
        if not can:
            self.send_data.setText("81 10 F1 3E")
        else:
            self.send_data.setText("01 C0 00 10 00 03 01")

    def _rate(self):
        try:
            return int(self.rate_box.currentText().strip())
        except ValueError:
            return CAN_RATES[0] if self.mode_box.currentText() == "CAN" else KLINE_RATES[0]

    def _set_default_path(self):
        from .. import recent as _recent
        self.path_edit.setText(os.path.join(
            _recent.get_last_dir("log"), "capture_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")))

    def _pick_path(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Capture file", self.path_edit.text(), "Capture logs (*.log);;All files (*)")
        if path:
            self.path_edit.setText(path)

    # ---- run ----
    def _toggle(self):
        if self._worker is not None:
            self._worker.stop()
            self.start_btn.setEnabled(False)
            return
        dev = self.dev_box.currentData()
        serial_tap = self._serial()
        if serial_tap:
            if not dev:
                QtWidgets.QMessageBox.warning(self, "Sniffer", "Pick the serial port of the tap cable.")
                return
        elif dev is None or (dev != OPENPORT_USB and not J.usable(dev)):
            QtWidgets.QMessageBox.warning(
                self, "Sniffer", "Pick a J2534 device this app can open. A 32-bit-only DLL needs the "
                "32-bit bridge: install a 32-bit Python (so 'py -3-32' works) or ship j2534bridge32.exe.")
            return
        openport = dev == OPENPORT_USB
        can = (not serial_tap) and self.mode_box.currentText() == "CAN"
        if self.rec_chk.isChecked():
            path = self.path_edit.text().strip()
            try:
                self._writer = C.Writer(path, (branding.APP_NAME + " sniffer  %s  %s  %s %d") % (
                    datetime.now().isoformat(timespec="seconds"),
                    dev if (serial_tap or openport) else dev.name,
                    "serial K-line tap" if serial_tap else ("CAN" if can else "K-line"), self._rate()))
            except OSError as ex:
                QtWidgets.QMessageBox.critical(self, "Sniffer", "Cannot write %s: %s" % (path, ex))
                return
        if serial_tap:
            self._framer = KL.Framer()
            self._worker = SerialTapWorker(dev, self._rate(), self.follow_baud.isChecked())
            self._worker.frames.connect(self._on_frames)
            self._worker.opened.connect(lambda s: self.status.setText("Listening: " + s))
            self._worker.baud_changed.connect(self._on_tap_baud)
            self._worker.failed.connect(self._failed)
            self._worker.ended.connect(self._ended)
            self.rate_box.currentTextChanged.connect(self._rate_changed)
            self.start_btn.setText("Stop")
            self._worker.start()
            return
        self._worker = SnifferWorker(
            OPENPORT_USB if openport else dev, "can" if can else "kline", self._rate(),
            self.ext_chk.isChecked(),
            self.loop_chk.isChecked(),
            J.ISO14230 if self.proto_box.currentIndex() == 0 else J.ISO9141)
        self._worker.frames.connect(self._on_frames)
        self._worker.opened.connect(lambda s: self.status.setText("Listening: " + s))
        self._worker.failed.connect(self._failed)
        self._worker.ended.connect(self._ended)
        self.rate_box.currentTextChanged.connect(self._rate_changed)
        self.start_btn.setText("Stop")
        self._worker.start()

    def _rate_changed(self, _txt):
        if self._worker is None:
            return
        if isinstance(self._worker, SerialTapWorker):
            self._worker.set_baud(self._rate())
        elif self.mode_box.currentText() != "CAN":
            self._worker.set_rate(self._rate())

    def _on_tap_baud(self, baud):
        self.status.setText("Tap now listening at %d baud" % baud)
        self.rate_box.blockSignals(True)
        self.rate_box.setEditText(str(baud))
        self.rate_box.blockSignals(False)

    def _failed(self, msg):
        QtWidgets.QMessageBox.critical(self, "Sniffer", msg)

    def _ended(self):
        self._worker = None
        try:
            self.rate_box.currentTextChanged.disconnect(self._rate_changed)
        except Exception:
            pass
        if self._writer is not None:
            self._writer.close()
            self.status.setText("Stopped. %d frames written to %s" % (
                self._writer.count, self.path_edit.text()))
            self._writer = None
            self._set_default_path()
        else:
            self.status.setText("Stopped.")
        self.start_btn.setText("Start")
        self.start_btn.setEnabled(True)

    def _send(self):
        if self._worker is None:
            QtWidgets.QMessageBox.information(self, "Sniffer", "Start the sniffer first.")
            return
        if not isinstance(self._worker, SnifferWorker):
            QtWidgets.QMessageBox.information(
                self, "Sniffer", "The serial K-line tap only listens. Sending needs a J2534 device.")
            return
        try:
            data = bytes.fromhex(self.send_data.text().replace(" ", "").replace(",", ""))
            cid = int(self.send_id.text().strip(), 16) if self.mode_box.currentText() == "CAN" else None
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sniffer", "Enter the id and data as hex.")
            return
        self._worker.send(cid, data)

    # ---- data flow ----
    def _on_frames(self, recs):
        self._pending.extend(recs)

    def _flush(self):
        if not self._pending:
            return
        recs, self._pending = self._pending, []
        self._ingest(recs, live=True)

    def _ingest(self, recs, live):
        items = []
        for r in recs:
            self._records.append(r)
            if self._writer is not None and live:
                self._writer.add(r)
            shown = [r]
            if r.bus == "kraw":
                # frame the tap's bytes; the raw burst itself is optional
                shown = [] if not self.raw_chk.isChecked() else [r]
                for fr in self._framer.feed(r.t, r.data):
                    shown.append(C.Record(fr.t, "kline" if fr.kind in ("kwp", "kw1281") else "kmeta",
                                          0, fr.data, extended=False))
                    shown[-1].note = fr.note if fr.kind != "kwp" or fr.note else ""
                    shown[-1].kind = fr.kind
                if live and self._framer.new_baud and self.follow_baud.isChecked() \
                        and isinstance(self._worker, SerialTapWorker):
                    self._worker.set_baud(self._framer.new_baud)
                    self._framer.new_baud = None
            for rr in shown:
                text, kind = self._decode(rr)
                dt = (rr.t - self.model.last_t) if self.model.last_t is not None else None
                if self.model.t0 is None:
                    self.model.t0 = rr.t
                self.model.last_t = rr.t
                if self._passes(rr, kind):
                    items.append((rr, text, kind, dt))
        self.model.add(items)
        if self.follow_chk.isChecked() and items:
            self.table.scrollToBottom()
        n = len(self._records)
        if live:
            self.status.setText("%d frames%s" % (n, ("  (recording)" if self._writer else "")))

    def _decode(self, r: C.Record):
        if r.bus == "kraw":
            return "raw bytes", "raw"
        if r.bus == "kmeta":
            return getattr(r, "note", ""), "init" if getattr(r, "kind", "") == "init" else "raw"
        if r.bus == "kline":
            if getattr(r, "kind", "") == "kw1281":
                title = r.data[2] if len(r.data) > 2 else -1
                return ("KW1281 block 0x%02X (counter %d)" % (title, r.data[1]) if len(r.data) > 2
                        else "KW1281 block"), "kw1281"
            direction, text = T.decode_kline(r.data)
            note = getattr(r, "note", "")
            body = _kwp_body(r.data)
            if body:
                self._add_message(T.Event(r.t, 0, "message", text, body, direction))
            return ((direction + "  " if direction else "") + text + ("   [" + note + "]" if note else "")), "kline"
        evs = self._decoder.feed(T.Frame(r.t, r.can_id, r.data, r.tx))
        kind, text = "raw", ""
        for e in evs:
            if e.kind == "message":
                self._add_message(e)
            else:
                kind, text = e.kind, ("%s  %s" % (e.channel, e.text) if e.channel else e.text)
        return text, kind

    def _add_message(self, e: T.Event):
        row = self.msgs.rowCount()
        self.msgs.insertRow(row)
        t = (e.t - self.model.t0) if self.model.t0 is not None else e.t
        cells = ["%.4f" % t, e.channel, e.text.split(" ")[0] if e.text else "",
                 str(len(e.payload)), e.payload.hex(" ").upper()]
        for i, c in enumerate(cells):
            it = QtWidgets.QTableWidgetItem(c)
            it.setToolTip(e.text)
            self.msgs.setItem(row, i, it)
        if self.follow_chk.isChecked():
            self.msgs.scrollToBottom()

    def _passes(self, r: C.Record, kind: str) -> bool:
        if self.tp_only.isChecked() and r.bus == "can" and kind == "raw":
            return False
        if self._id_filter and r.bus == "can":
            return any(lo <= r.can_id <= hi for lo, hi in self._id_filter)
        return True

    def _parse_filter(self):
        txt = self.filter_edit.text().strip()
        if not txt:
            self._id_filter = None
            return
        out = []
        for part in txt.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "-" in part:
                    a, b = part.split("-", 1)
                    out.append((int(a, 16), int(b, 16)))
                else:
                    v = int(part, 16)
                    out.append((v, v))
            except ValueError:
                continue
        self._id_filter = out or None

    def _rebuild_view(self):
        """Re-run the decode over everything held (filter or decoder change)."""
        self._parse_filter()
        recs = list(self._records)
        self._records = []
        self._decoder = T.Tp20Decoder()
        self._framer = KL.Framer()
        self.model.clear()
        self.msgs.setRowCount(0)
        self._ingest(recs, live=False)

    def _clear(self):
        self._records = []
        self._decoder = T.Tp20Decoder()
        self._framer = KL.Framer()
        self.model.clear()
        self.msgs.setRowCount(0)
        self.status.setText("Cleared.")

    def _open_capture(self):
        from .. import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open capture", _recent.get_last_dir("log"), "Capture logs (*.log *.txt);;All files (*)")
        if not path:
            return
        _recent.set_last_dir(os.path.dirname(path), "log")
        try:
            recs = C.read(path)
        except OSError as ex:
            QtWidgets.QMessageBox.critical(self, "Sniffer", str(ex))
            return
        self._clear()
        self._parse_filter()
        self._ingest(recs, live=False)
        self.status.setText("%s: %d records, %d rows shown, %d TP 2.0 messages decoded" % (
            os.path.basename(path), len(recs), self.model.rowCount(), self.msgs.rowCount()))

    def _fit_to_screen(self, w, h):
        """Open at ``w`` x ``h``, or smaller when the screen is: a laptop's
        1366x768 (or 1280x720 at 150 % scaling) leaves less than that once the
        taskbar and title bar are taken out."""
        scr = self.screen() or QtWidgets.QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(w, int(avail.width() * 0.95))
            h = min(h, int(avail.height() * 0.90))
        self.resize(w, h)

    def closeEvent(self, ev):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        super().closeEvent(ev)
