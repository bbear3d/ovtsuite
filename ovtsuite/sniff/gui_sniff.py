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
from . import merge as M
from .. import branding

OPENPORT_USB = "openport-usb"                 # dev_box data for the pyusb OpenPort route
CAN_RATES = [500000, 250000, 125000, 100000, 83333, 50000, 33333]
KLINE_RATES = [10400, 9600, 38400, 124800, 244898]
_KIND_BG = {"setup": QtGui.QColor(255, 236, 179), "setup-ack": QtGui.QColor(255, 236, 179),
            "param": QtGui.QColor(220, 235, 255), "ack": QtGui.QColor(235, 235, 235),
            "data": QtGui.QColor(225, 245, 225), "message": QtGui.QColor(200, 240, 200),
            "init": QtGui.QColor(255, 236, 179), "kw1281": QtGui.QColor(225, 245, 225)}
_DARK_FG = QtGui.QColor(20, 20, 20)
# one tint per capture source (the Source column); orange is the app's
# "differs" outline and yellow "pending", so neither is used here
_SRC_BG = [QtGui.QColor(200, 225, 245), QtGui.QColor(215, 240, 225),
           QtGui.QColor(245, 215, 230), QtGui.QColor(230, 225, 245)]
SRC_IDS = "ABCDEF"
KIND_CAN, KIND_KLINE, KIND_SERIAL = "can", "kline", "serial"


def src_color(src: str) -> QtGui.QColor:
    i = SRC_IDS.find(src[:1]) if src else 0
    return _SRC_BG[max(i, 0) % len(_SRC_BG)]


def _session(why: str):
    """Real-time session for a capture thread (keeps the machine awake and
    the thread time-critical); falls back to sleep prevention alone."""
    try:
        from ..realtime import realtime_session
    except ImportError:
        from ..flash.keepawake import prevent_sleep
        return prevent_sleep(why)
    return realtime_session(why=why)


# ------------------------------------------------------------------ worker
class SnifferWorker(QtCore.QThread):
    frames = QtCore.Signal(list)                 # [capture.Record]
    opened = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    ended = QtCore.Signal()

    def __init__(self, dll, mode, rate, extended=False, loopback=True, protocol=J.ISO14230,
                 clock=None, src=""):
        super().__init__()
        self.clock = clock or M.SharedClock()
        self.src = src
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
        with _session("Bus sniffer"):
            self._run()

    def _run(self):
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
            devclock = M.DeviceClock()
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
                now = self.clock.now()
                recs = []
                for m in msgs:
                    if m.rx_status & (J.START_OF_MESSAGE | J.TX_DONE):
                        continue                          # indications, not data
                    t = devclock.map(m.timestamp, now)    # on the shared clock, drift-corrected
                    if self.mode == "can":
                        cid = m.can_id
                        if cid is None:
                            continue
                        recs.append(C.Record(t, "can", cid, m.payload, m.is_tx_echo,
                                             extended=bool(m.tx_flags & J.CAN_29BIT_ID),
                                             src=self.src))
                    else:
                        recs.append(C.Record(t, "kline", 0, m.data, m.is_tx_echo, src=self.src))
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

    def __init__(self, port, baud, follow=True, clock=None, src=""):
        super().__init__()
        self.clock = clock or M.SharedClock()
        self.src = src
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
        with _session("Bus sniffer (serial tap)"):
            self._run()

    def _run(self):
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
            last_rx = self.clock.now()
            tracker = KL.Framer("kwp")
            while not self._stop:
                with self._lock:
                    want, self._want_baud = self._want_baud, None
                if want and want != self.baud:
                    ser.baudrate = want
                    self.baud = want
                    self.baud_changed.emit(want)
                data = ser.read(ser.in_waiting or 1)
                now = self.clock.now()
                if data:
                    last_rx = now
                    self.frames.emit([C.Record(now, "kraw", 0, bytes(data), src=self.src)])
                    if self.follow:
                        # Follow link-speed changes HERE, in the reader thread: the
                        # GUI only sees the bytes after its 150 ms flush timer, by
                        # which time the ECU's reply (~35 ms after the request, at
                        # the new rate) has already been missed.
                        tracker.feed(now, bytes(data))
                        nb, tracker.new_baud = tracker.new_baud, None
                        if nb and nb != self.baud:
                            ser.baudrate = nb
                            self.baud = nb
                            self.baud_changed.emit(nb)
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
    COLS = ["Time", "dt ms", "Source", "Bus", "ID", "Len", "Data", "Decoded"]
    C_SRC, C_DATA, C_DECODED = 2, 6, 7
    CAP = 50000

    def __init__(self):
        super().__init__()
        self.rows: List[tuple] = []          # (Record, text, kind, dt)
        self.t0 = None
        self.last_t = None
        self.src_names = {}                  # "A" -> "J2534 CAN 500000", for the tooltip

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
            if c == self.C_SRC:
                return rec.src
            if c == 3:
                return {"kmeta": "k-line", "kraw": "k raw"}.get(rec.bus, rec.bus) + (" tx" if rec.tx else "")
            if c == 4:
                return ("%08X" % rec.can_id if rec.extended else "%03X" % rec.can_id) \
                    if rec.bus == "can" else ""
            if c == 5:
                return str(len(rec.data))
            if c == self.C_DATA:
                return rec.data.hex(" ").upper()
            if c == self.C_DECODED:
                return text
        if role == QtCore.Qt.ToolTipRole and c == self.C_SRC and rec.src:
            return self.src_names.get(rec.src)
        if role == QtCore.Qt.BackgroundRole:
            if c == self.C_SRC and rec.src:        # the source's own tint, whatever the row kind
                return src_color(rec.src)
            if kind in _KIND_BG:
                return _KIND_BG[kind]
        if role == QtCore.Qt.ForegroundRole and (kind in _KIND_BG or (c == self.C_SRC and rec.src)):
            return _DARK_FG
        if role == QtCore.Qt.FontRole and c == self.C_DATA:
            f = QtGui.QFont("Consolas" if os.name == "nt" else "Monospace")
            return f
        return None


# ------------------------------------------------------------ device lists
def _device_choices(serial: bool):
    """[(label, data, enabled)] for the device / port boxes."""
    out = []
    if serial:
        from ..flash.gui_flash import list_ports
        ports = list_ports()
        if not ports:
            return [("(no serial ports found)", None, True)]
        return [(label, dev, True) for dev, label in ports]
    devs = J.list_devices()
    for d in devs:
        if d.loadable:
            note = ""
        elif J.usable(d):
            note = "  (via 32-bit bridge)"
        else:
            note = "  - 32-bit DLL: needs a 32-bit Python (py -3-32) or j2534bridge32.exe"
        out.append((d.label + note, d, J.usable(d)))
    # OpenPort 2.0 over USB (pyusb) -- the route on Linux / macOS
    try:
        from ..flash import openport
        ops = openport.find_devices()
    except Exception:
        ops = []
    for label in ops:
        out.append((label + " (Direct-USB)", OPENPORT_USB, True))
    if not devs and not ops:
        out.append(("(no J2534 device registered or OpenPort on USB)", None, True))
    return out


def _fill_device_box(box: QtWidgets.QComboBox, serial: bool):
    box.clear()
    for label, data, enabled in _device_choices(serial):
        box.addItem(label, data)
        if not enabled:
            box.model().item(box.count() - 1).setEnabled(False)


def _device_key(spec):
    """What must not be opened twice: the serial port, or the J2534 device."""
    dev = spec["dev"]
    if spec["kind"] == KIND_SERIAL:
        return ("serial", str(dev))
    return ("j2534", dev if isinstance(dev, str) else getattr(dev, "name", str(dev)))


def _source_title(spec) -> str:
    return {KIND_CAN: "J2534 CAN", KIND_KLINE: "J2534 K-line",
            KIND_SERIAL: "serial K-line tap"}[spec["kind"]]


def _source_label(sp) -> str:
    dev = sp["dev"]
    where = dev if isinstance(dev, str) and dev != OPENPORT_USB else getattr(dev, "name", "")
    if dev == OPENPORT_USB:
        where = "OpenPort USB"
    return "%s %d%s" % (_source_title(sp), sp["rate"], (" (%s)" % where) if where else "")


class SourceRow(QtWidgets.QWidget):
    """One additional capture source: kind, device, rate and options."""
    changed = QtCore.Signal()
    removed = QtCore.Signal(object)
    rate_edited = QtCore.Signal(object)

    def __init__(self, sid: str, parent=None):
        super().__init__(parent)
        self.sid = sid
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.lbl = QtWidgets.QLabel("<b>%s</b>" % sid)
        h.addWidget(self.lbl)
        self.kind_box = QtWidgets.QComboBox()
        for label, k in (("J2534 CAN", KIND_CAN), ("J2534 K-line", KIND_KLINE),
                         ("Serial K-line tap", KIND_SERIAL)):
            self.kind_box.addItem(label, k)
        self.kind_box.currentIndexChanged.connect(self._kind_changed)
        h.addWidget(self.kind_box)
        self.dev_box = QtWidgets.QComboBox()
        self.dev_box.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dev_box.setMinimumContentsLength(20)
        h.addWidget(self.dev_box, 1)
        self.rate_lbl = QtWidgets.QLabel("Rate:")
        h.addWidget(self.rate_lbl)
        self.rate_box = QtWidgets.QComboBox()
        self.rate_box.setEditable(True)
        self.rate_box.currentTextChanged.connect(lambda _t: self.rate_edited.emit(self))
        h.addWidget(self.rate_box)
        self.ext_chk = QtWidgets.QCheckBox("29-bit ids")
        h.addWidget(self.ext_chk)
        self.proto_box = QtWidgets.QComboBox()
        self.proto_box.addItems(["ISO 14230", "ISO 9141"])
        h.addWidget(self.proto_box)
        self.loop_chk = QtWidgets.QCheckBox("Show own frames")
        self.loop_chk.setChecked(True)
        h.addWidget(self.loop_chk)
        self.follow_chk = QtWidgets.QCheckBox("Follow speed changes")
        self.follow_chk.setChecked(True)
        h.addWidget(self.follow_chk)
        self.rm_btn = QtWidgets.QPushButton("Remove")
        self.rm_btn.clicked.connect(lambda: self.removed.emit(self))
        h.addWidget(self.rm_btn)
        self.kind_box.setCurrentIndex(2)       # the usual second source: a KKL tap
        self._kind_changed()

    def kind(self) -> str:
        return self.kind_box.currentData()

    def _kind_changed(self):
        k = self.kind()
        can = k == KIND_CAN
        self.rate_box.blockSignals(True)
        self.rate_box.clear()
        self.rate_box.addItems([str(r) for r in (CAN_RATES if can else KLINE_RATES)])
        self.rate_box.blockSignals(False)
        self.rate_lbl.setText("Bit rate:" if can else "Baud:")
        self.ext_chk.setVisible(can)
        self.proto_box.setVisible(k == KIND_KLINE)
        self.loop_chk.setVisible(k != KIND_SERIAL)
        self.follow_chk.setVisible(k == KIND_SERIAL)
        _fill_device_box(self.dev_box, k == KIND_SERIAL)
        self.changed.emit()

    def refresh_devices(self):
        _fill_device_box(self.dev_box, self.kind() == KIND_SERIAL)

    def set_running(self, running: bool):
        for w in (self.kind_box, self.dev_box, self.ext_chk, self.proto_box,
                  self.loop_chk, self.follow_chk, self.rm_btn):
            w.setEnabled(not running)

    def rate(self) -> int:
        try:
            return int(self.rate_box.currentText().strip())
        except ValueError:
            return CAN_RATES[0] if self.kind() == KIND_CAN else KLINE_RATES[0]

    def spec(self) -> dict:
        return dict(id=self.sid, kind=self.kind(), dev=self.dev_box.currentData(),
                    rate=self.rate(), ext=self.ext_chk.isChecked(),
                    loop=self.loop_chk.isChecked(),
                    proto=J.ISO14230 if self.proto_box.currentIndex() == 0 else J.ISO9141,
                    follow=self.follow_chk.isChecked())


# ------------------------------------------------------------------ dialog
class SnifferDialog(QtWidgets.QDialog):
    PRIMARY = "A"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bus sniffer (J2534)")
        try:
            from ..gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        self._fit_to_screen(1180, 760)
        self._workers: dict = {}                  # source id -> running worker
        self._worker_specs: dict = {}             # source id -> the spec it was started from
        self._open_msgs: dict = {}
        self._writer: Optional[C.Writer] = None
        self._last_written = 0
        self._records: List[C.Record] = []
        self._decoders: dict = {}                 # per source: TP 2.0 state
        self._framers: dict = {}                  # per source: K-line framing
        self._last_t: dict = {}                   # per source: for dt
        self._pending: List[C.Record] = []
        self._id_filter = None
        self._clock = M.SharedClock()
        self._merge = False
        self._reorder = M.Reorderer()
        self._rows: List[SourceRow] = []
        self._src_checks: dict = {}               # source id -> "show" checkbox
        v = QtWidgets.QVBoxLayout(self)

        # -- device row, then bus-settings row. These were one row, and its
        # minimum width (~1430 px) set the whole window's: wider than a
        # 1366 px laptop screen, so Start sat off the right edge.
        row = QtWidgets.QHBoxLayout()
        self.src_lbl = QtWidgets.QLabel("Source:")
        row.addWidget(self.src_lbl)
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
        self.add_src_btn = QtWidgets.QPushButton("Add source")
        self.add_src_btn.setToolTip("Capture a second bus at the same time (for example a K-line "
                                    "cable and a CAN device); records are merged by time")
        self.add_src_btn.clicked.connect(self._add_source)
        row.addWidget(self.add_src_btn)
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
        self.rate_box.currentTextChanged.connect(lambda _t: self._apply_rate(self.PRIMARY))
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

        # extra sources (B, C, ...) sit under the first one
        self.extra_layout = QtWidgets.QVBoxLayout()
        self.extra_layout.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self.extra_layout)

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
        self.src_filter_lbl = QtWidgets.QLabel("Show sources:")
        self.src_filter_lbl.setVisible(False)
        row3.addWidget(self.src_filter_lbl)
        self.src_filter_layout = QtWidgets.QHBoxLayout()
        row3.addLayout(self.src_filter_layout)
        self.follow_chk = QtWidgets.QCheckBox("Follow")
        self.follow_chk.setChecked(True)
        row3.addWidget(self.follow_chk)
        v.addLayout(row3)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.model = FrameModel()
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        # the app's table standard: alternating rows, so a selection is the
        # solid accent colour, and a delegate that keeps its text legible
        from ..gui_cellmarks import CellDelegate
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(CellDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(FrameModel.C_DECODED, QtWidgets.QHeaderView.Stretch)
        for c, w in ((0, 90), (1, 60), (2, 56), (3, 60), (4, 80), (5, 40), (6, 230)):
            self.table.setColumnWidth(c, w)
        self.table.setColumnHidden(FrameModel.C_SRC, True)     # shown once sources are tagged
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
        self.msgs.setAlternatingRowColors(True)
        self.msgs.setItemDelegate(CellDelegate(self.msgs))
        split.addWidget(self.msgs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

        # -- send row
        row4 = QtWidgets.QHBoxLayout()
        row4.addWidget(QtWidgets.QLabel("Send:"))
        self.send_src = QtWidgets.QComboBox()
        self.send_src.setToolTip("Which source transmits")
        self.send_src.currentIndexChanged.connect(self._send_target_changed)
        self.send_src.setVisible(False)
        row4.addWidget(self.send_src)
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
        self._refresh_devices()
        self._mode_changed()
        self._source_changed()
        self._set_default_path()

    # ---- the first source's widgets, as a worker would be asked for it ----
    @property
    def _worker(self):
        """The first source's worker (None when it is not running)."""
        return self._workers.get(self.PRIMARY)

    @_worker.setter
    def _worker(self, w):
        if w is None:
            self._workers.pop(self.PRIMARY, None)
        else:
            self._workers[self.PRIMARY] = w

    def _serial(self) -> bool:
        return self.src_box.currentIndex() == 1

    def _primary_spec(self) -> dict:
        ser = self._serial()
        can = (not ser) and self.mode_box.currentText() == "CAN"
        return dict(id=self.PRIMARY, kind=KIND_SERIAL if ser else (KIND_CAN if can else KIND_KLINE),
                    dev=self.dev_box.currentData(), rate=self._rate(),
                    ext=self.ext_chk.isChecked(), loop=self.loop_chk.isChecked(),
                    proto=J.ISO14230 if self.proto_box.currentIndex() == 0 else J.ISO9141,
                    follow=self.follow_baud.isChecked())

    def _specs(self) -> List[dict]:
        return [self._primary_spec()] + [r.spec() for r in self._rows]

    def _next_id(self) -> Optional[str]:
        used = {self.PRIMARY} | {r.sid for r in self._rows}
        for ch in SRC_IDS:
            if ch not in used:
                return ch
        return None

    def _add_source(self):
        if self._workers:
            return
        sid = self._next_id()
        if sid is None:
            QtWidgets.QMessageBox.information(self, "Sniffer", "That is the most sources there is room for.")
            return
        row = SourceRow(sid, self)
        row.changed.connect(self._refresh_send_targets)
        row.removed.connect(self._remove_source)
        row.rate_edited.connect(lambda r: self._apply_rate(r.sid))
        self._rows.append(row)
        self.extra_layout.addWidget(row)
        self.src_lbl.setText("Source A:")
        self._refresh_send_targets()
        return row

    def _remove_source(self, row):
        if self._workers:
            return
        self._rows.remove(row)
        self.extra_layout.removeWidget(row)
        row.deleteLater()
        if not self._rows:
            self.src_lbl.setText("Source:")
        self._refresh_send_targets()

    def _source_changed(self):
        ser = self._serial()
        self.dev_lbl.setText("  Port:" if ser else "  Device:")
        self.follow_baud.setVisible(ser)
        self.raw_chk.setVisible(ser or any(r.kind() == KIND_SERIAL for r in self._rows))
        self.mode_box.setVisible(not ser)
        self.mode_lbl.setVisible(not ser)
        self.loop_chk.setVisible(not ser)
        if ser:
            self.mode_box.setCurrentIndex(1)           # K-line rates in the box
        self._mode_changed()
        self._refresh_send_targets()
        self._refresh_devices()

    def _refresh_send_targets(self):
        """The Send row's source box: every J2534 source (the serial tap only
        listens, so there is nothing to send through)."""
        self.raw_chk.setVisible(self._serial() or any(r.kind() == KIND_SERIAL for r in self._rows))
        cur = self.send_src.currentData()
        self.send_src.blockSignals(True)
        self.send_src.clear()
        for sp in self._specs():
            if sp["kind"] != KIND_SERIAL:
                self.send_src.addItem("%s: %s" % (sp["id"], _source_title(sp)), sp["id"])
        i = self.send_src.findData(cur)
        self.send_src.setCurrentIndex(max(i, 0))
        self.send_src.blockSignals(False)
        can_send = self.send_src.count() > 0
        self.send_src.setVisible(self.send_src.count() > 1)
        self.send_btn.setEnabled(can_send)
        self.tp_tpl_btn.setEnabled(can_send)
        self.send_data.setEnabled(can_send)
        self._send_target_changed()
        self.send_btn.setToolTip("Send the frame through the J2534 device" if can_send else
                                 "The serial K-line tap only listens; sending needs a J2534 device")

    _SEND_TPL = {False: "81 10 F1 3E", True: "01 C0 00 10 00 03 01"}

    def _send_target_changed(self):
        sid = self.send_src.currentData()
        spec = next((s for s in self._specs() if s["id"] == sid), None)
        self.send_id.setEnabled(spec is not None and spec["kind"] == KIND_CAN)
        if spec is not None and spec["kind"] != KIND_SERIAL:
            # the template follows the chosen source's bus, unless it was edited
            can = spec["kind"] == KIND_CAN
            if self.send_data.text().strip() in self._SEND_TPL.values():
                self.send_data.setText(self._SEND_TPL[can])

    # ---- setup ----
    def _refresh_devices(self):
        _fill_device_box(self.dev_box, self._serial())
        for r in self._rows:
            if not self._workers:
                r.refresh_devices()

    def _mode_changed(self):
        can = self.mode_box.currentText() == "CAN"
        ser = self._serial() if hasattr(self, "src_box") else False
        self.rate_box.blockSignals(True)       # a list rebuild is not a rate change
        self.rate_box.clear()
        self.rate_box.addItems([str(r) for r in (CAN_RATES if can else KLINE_RATES)])
        self.rate_box.blockSignals(False)
        self.rate_lbl.setText("  Bit rate:" if can else "  Baud:")
        self.ext_chk.setVisible(can and not ser)
        self.proto_box.setVisible(not can and not ser)
        if not can:
            self.send_data.setText("81 10 F1 3E")
        else:
            self.send_data.setText("01 C0 00 10 00 03 01")
        if hasattr(self, "send_src"):
            self._refresh_send_targets()

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
    def _check_spec(self, sp) -> Optional[str]:
        dev = sp["dev"]
        who = ("Source %s: " % sp["id"]) if self._rows else ""
        if sp["kind"] == KIND_SERIAL:
            if not dev:
                return who + "Pick the serial port of the tap cable."
        elif dev is None or (dev != OPENPORT_USB and not J.usable(dev)):
            return (who + "Pick a J2534 device this app can open. A 32-bit-only DLL needs the "
                    "32-bit bridge: install a 32-bit Python (so 'py -3-32' works) or ship j2534bridge32.exe.")
        return None

    def _check_exclusive(self, specs) -> Optional[str]:
        """One device or port cannot be opened by two sources."""
        seen = {}
        for sp in specs:
            k = _device_key(sp)
            if k in seen:
                return ("Source %s and source %s are both set to %s. A device or port can only be "
                        "opened once: pick a different one for one of them." % (
                            seen[k], sp["id"], "the same %s" % (
                                "port" if sp["kind"] == KIND_SERIAL else "device")))
            seen[k] = sp["id"]
        return None

    def _make_worker(self, sp, tag):
        if sp["kind"] == KIND_SERIAL:
            return SerialTapWorker(sp["dev"], sp["rate"], sp["follow"], clock=self._clock, src=tag)
        return SnifferWorker(sp["dev"], "can" if sp["kind"] == KIND_CAN else "kline", sp["rate"],
                             sp["ext"], sp["loop"], sp["proto"], clock=self._clock, src=tag)

    def _toggle(self):
        if self._workers:
            for w in list(self._workers.values()):
                w.stop()
            self.start_btn.setEnabled(False)
            return
        specs = self._specs()
        for sp in specs:
            err = self._check_spec(sp)
            if err:
                QtWidgets.QMessageBox.warning(self, "Sniffer", err)
                return
        err = self._check_exclusive(specs)
        if err:
            QtWidgets.QMessageBox.warning(self, "Sniffer", err)
            return
        multi = len(specs) > 1
        names = {sp["id"]: _source_label(sp) for sp in specs}
        if self.rec_chk.isChecked():
            path = self.path_edit.text().strip()
            try:
                if multi:
                    header = branding.APP_NAME + " sniffer  %s  %d sources\n" % (
                        datetime.now().isoformat(timespec="seconds"), len(specs)) + "\n".join(
                            "source %s: %s" % (k, n) for k, n in names.items())
                else:
                    sp = specs[0]
                    dev = sp["dev"]
                    header = (branding.APP_NAME + " sniffer  %s  %s  %s %d") % (
                        datetime.now().isoformat(timespec="seconds"),
                        dev if (sp["kind"] == KIND_SERIAL or dev == OPENPORT_USB) else dev.name,
                        "serial K-line tap" if sp["kind"] == KIND_SERIAL else (
                            "CAN" if sp["kind"] == KIND_CAN else "K-line"), sp["rate"])
                self._writer = C.Writer(path, header)
            except OSError as ex:
                QtWidgets.QMessageBox.critical(self, "Sniffer", "Cannot write %s: %s" % (path, ex))
                return
        self._clock = M.SharedClock()
        self._merge = multi
        self._reorder = M.Reorderer()
        self.model.src_names = dict(names) if multi else {}
        if multi:
            self.src_filter_lbl.setVisible(True)
            for sp in specs:
                self._note_src(sp["id"])
        self._open_msgs = {}
        self._worker_specs = {sp["id"]: sp for sp in specs}
        for sp in specs:
            tag = sp["id"] if multi else ""
            self._framers[tag] = KL.Framer()
            w = self._make_worker(sp, tag)
            sid = sp["id"]
            self._workers[sid] = w
            w.frames.connect(self._on_frames)
            w.opened.connect(lambda s, sid=sid: self._opened(sid, s))
            if hasattr(w, "baud_changed"):
                w.baud_changed.connect(lambda b, sid=sid: self._on_tap_baud(b, sid))
            w.failed.connect(lambda m, sid=sid: self._failed(m, sid))
            w.ended.connect(lambda sid=sid: self._ended(sid))
        self._set_locked(True)
        self.start_btn.setText("Stop")
        for w in list(self._workers.values()):
            w.start()

    def _set_locked(self, running: bool):
        for r in self._rows:
            r.set_running(running)
        self.add_src_btn.setEnabled(not running)

    def _opened(self, sid, msg):
        self._open_msgs[sid] = msg
        if len(self._worker_specs) > 1:
            self.status.setText("Listening: " + "; ".join(
                "%s: %s" % (k, m) for k, m in sorted(self._open_msgs.items())))
        else:
            self.status.setText("Listening: " + msg)

    def _apply_rate(self, sid):
        """A rate box changed while its source runs: apply it live."""
        w = self._workers.get(sid)
        if w is None:
            return
        sp = self._worker_specs.get(sid) or {}
        row = next((r for r in self._rows if r.sid == sid), None)
        rate = row.rate() if row is not None else self._rate()
        if isinstance(w, SerialTapWorker):
            w.set_baud(rate)
        elif sp.get("kind") == KIND_KLINE:
            w.set_rate(rate)

    def _on_tap_baud(self, baud, sid=None):
        sid = sid or self.PRIMARY
        self.status.setText("Tap %snow listening at %d baud" % (
            ("%s " % sid) if len(self._worker_specs) > 1 else "", baud))
        row = next((r for r in self._rows if r.sid == sid), None)
        box = row.rate_box if row is not None else self.rate_box
        box.blockSignals(True)
        box.setEditText(str(baud))
        box.blockSignals(False)

    def _failed(self, msg, sid=None):
        multi = len(self._worker_specs) > 1
        QtWidgets.QMessageBox.critical(
            self, "Sniffer", ("Source %s: %s" % (sid, msg)) if multi and sid else msg)

    def _ended(self, sid=None):
        sid = sid or self.PRIMARY
        self._workers.pop(sid, None)
        if self._workers:
            return                                  # the others keep capturing
        # the last source is done: flush what the merge window still holds
        if len(self._reorder) or self._pending:
            self._reorder.add(self._pending)
            self._pending = []
            self._ingest(self._reorder.drain(), live=True)
        self._merge = False
        self._worker_specs = {}
        self._set_locked(False)
        if self._writer is not None:
            self._close_writer()
            self.status.setText("Stopped. %d frames written to %s" % (
                self._last_written, self.path_edit.text()))
            self._set_default_path()
        else:
            self.status.setText("Stopped.")
        self.start_btn.setText("Start")
        self.start_btn.setEnabled(True)

    def _close_writer(self):
        """Close the capture file. It is written in release order as the
        records arrive (so a crash loses little); a record later than the merge
        window can land out of time order, so when that happened and nothing was
        cleared meanwhile the file is rewritten sorted from the held records."""
        w, self._writer = self._writer, None
        if w is None:
            return
        w.close()
        self._last_written = w.count
        if w.unsorted and w.count == len(self._records):
            try:
                C.write(w.fh.name, self._records, w.header)
            except OSError:
                pass

    def _send(self):
        sid = self.send_src.currentData() or self.PRIMARY
        w = self._workers.get(sid)
        if not self._workers:
            QtWidgets.QMessageBox.information(self, "Sniffer", "Start the sniffer first.")
            return
        if w is None:
            QtWidgets.QMessageBox.information(
                self, "Sniffer", "Source %s has stopped; pick a source that is still capturing." % sid)
            return
        if not isinstance(w, SnifferWorker):
            QtWidgets.QMessageBox.information(
                self, "Sniffer", "The serial K-line tap only listens. Sending needs a J2534 device.")
            return
        try:
            data = bytes.fromhex(self.send_data.text().replace(" ", "").replace(",", ""))
            cid = int(self.send_id.text().strip(), 16) if w.mode == "can" else None
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sniffer", "Enter the id and data as hex.")
            return
        w.send(cid, data)

    # ---- source filter ----
    def _note_src(self, src):
        """A source id seen (live or in a loaded file): show the Source column
        and give it a show / hide box."""
        if not src or src in self._src_checks:
            return
        cb = QtWidgets.QCheckBox(src)
        cb.setChecked(True)
        cb.setToolTip("Show records from source %s" % src)
        cb.toggled.connect(lambda _c: self._rebuild_view())
        self._src_checks[src] = cb
        self.src_filter_layout.addWidget(cb)
        self.src_filter_lbl.setVisible(True)
        self.table.setColumnHidden(FrameModel.C_SRC, False)

    # ---- data flow ----
    def _on_frames(self, recs):
        self._pending.extend(recs)

    def _flush(self):
        if not self._pending and not len(self._reorder):
            return
        recs, self._pending = self._pending, []
        if self._merge:
            # sources deliver in bursts at different delays: hold a short
            # window and release in time order
            self._reorder.add(recs)
            recs = self._reorder.release(self._clock.now())
        self._ingest(recs, live=True)

    def _store(self, r):
        """Keep ``_records`` sorted by time (a late record from a slower source
        goes back where it belongs)."""
        recs = self._records
        if not recs or r.t >= recs[-1].t:
            recs.append(r)
            return
        # a late record: binary search for its place (after equal times, so
        # arrival order holds among ties) rather than walking back from the end
        lo, hi = 0, len(recs)
        t = r.t
        while lo < hi:
            mid = (lo + hi) // 2
            if recs[mid].t > t:
                hi = mid
            else:
                lo = mid + 1
        recs.insert(lo, r)

    def _framer_for(self, src):
        f = self._framers.get(src)
        if f is None:
            f = self._framers[src] = KL.Framer()
        return f

    def _ingest(self, recs, live):
        items = []
        retime = False
        for r in recs:
            self._store(r)
            if r.src:
                self._note_src(r.src)
            if self._writer is not None and live:
                self._writer.add(r)
            shown = [r]
            if r.bus == "kraw":
                # frame the tap's bytes; the raw burst itself is optional
                framer = self._framer_for(r.src)
                shown = [] if not self.raw_chk.isChecked() else [r]
                for fr in framer.feed(r.t, r.data):
                    shown.append(C.Record(fr.t, "kline" if fr.kind in ("kwp", "kw1281") else "kmeta",
                                          0, fr.data, extended=False, src=r.src))
                    shown[-1].note = fr.note if fr.kind != "kwp" or fr.note else ""
                    shown[-1].kind = fr.kind
                w = self._workers.get(r.src or self.PRIMARY)
                if live and framer.new_baud and isinstance(w, SerialTapWorker) \
                        and self._worker_specs.get(r.src or self.PRIMARY, {}).get("follow", True) \
                        and self.follow_baud.isChecked():
                    w.set_baud(framer.new_baud)
                    framer.new_baud = None
            for rr in shown:
                # Time zero is the earliest stamp seen: a record that arrives
                # later than the merge window but is stamped earlier moves it
                # back, rather than showing a negative Time.
                if self.model.t0 is None or rr.t < self.model.t0:
                    moved = self.model.t0 is not None
                    self.model.t0 = rr.t
                    if moved:
                        retime = True
                text, kind = self._decode(rr)
                last = self._last_t.get(rr.src)
                dt = (rr.t - last) if last is not None else None
                self._last_t[rr.src] = rr.t
                self.model.last_t = rr.t
                if self._passes(rr, kind):
                    items.append((rr, text, kind, dt))
        self.model.add(items)
        if retime:
            self._retime_views()
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
                self._add_message(T.Event(r.t, 0, "message", text, body, direction), r.src)
            return ((direction + "  " if direction else "") + text + ("   [" + note + "]" if note else "")), "kline"
        dec = self._decoders.get(r.src)
        if dec is None:                       # TP 2.0 state is per source: two buses never mix
            dec = self._decoders[r.src] = T.Tp20Decoder()
        evs = dec.feed(T.Frame(r.t, r.can_id, r.data, r.tx))
        kind, text = "raw", ""
        for e in evs:
            if e.kind == "message":
                self._add_message(e, r.src)
            else:
                kind, text = e.kind, ("%s  %s" % (e.channel, e.text) if e.channel else e.text)
        return text, kind

    def _retime_views(self):
        """Time zero moved earlier: redraw both tables' Time columns."""
        n = self.model.rowCount()
        if n:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(n - 1, 0))
        t0 = self.model.t0
        for row in range(self.msgs.rowCount()):
            it = self.msgs.item(row, 0)
            raw = it.data(QtCore.Qt.UserRole) if it is not None else None
            if raw is not None:
                it.setText("%.4f" % (raw - t0))

    def _add_message(self, e: T.Event, src=""):
        if src:                              # a hidden source's messages are hidden too
            cb = self._src_checks.get(src)
            if cb is not None and not cb.isChecked():
                return
        row = self.msgs.rowCount()
        self.msgs.insertRow(row)
        t = (e.t - self.model.t0) if self.model.t0 is not None else e.t
        chan = ("%s " % src if src else "") + e.channel
        cells = ["%.4f" % t, chan, e.text.split(" ")[0] if e.text else "",
                 str(len(e.payload)), e.payload.hex(" ").upper()]
        for i, c in enumerate(cells):
            it = QtWidgets.QTableWidgetItem(c)
            it.setToolTip(e.text)
            if i == 0:
                it.setData(QtCore.Qt.UserRole, e.t)
            self.msgs.setItem(row, i, it)
        if self.follow_chk.isChecked():
            self.msgs.scrollToBottom()

    def _passes(self, r: C.Record, kind: str) -> bool:
        if r.src:
            cb = self._src_checks.get(r.src)
            if cb is not None and not cb.isChecked():
                return False
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

    def _reset_decode(self):
        self._decoders = {}
        self._framers = {}
        self._last_t = {}

    def _rebuild_view(self):
        """Re-run the decode over everything held (filter or decoder change)."""
        self._parse_filter()
        recs = list(self._records)
        self._records = []
        self._reset_decode()
        self.model.clear()
        self.msgs.setRowCount(0)
        self._ingest(recs, live=False)

    def _clear_src_checks(self):
        for cb in self._src_checks.values():
            self.src_filter_layout.removeWidget(cb)
            cb.deleteLater()
        self._src_checks = {}
        self.src_filter_lbl.setVisible(False)
        self.table.setColumnHidden(FrameModel.C_SRC, True)

    def _clear(self):
        self._records = []
        self._reset_decode()
        self._clear_src_checks()
        self._reorder = M.Reorderer()
        self._pending = []
        self.model.clear()
        self.msgs.setRowCount(0)
        self.status.setText("Cleared.")
        if self._worker_specs and len(self._worker_specs) > 1:
            for sid in self._worker_specs:       # a running multi-source capture goes on
                self._note_src(sid)

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
        self._ingest(sorted(recs, key=lambda r: r.t), live=False)
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
        for w in list(self._workers.values()):
            try:
                w.stop()
                w.wait(3000)
            except Exception:
                pass
        self._workers = {}
        self._close_writer()
        super().closeEvent(ev)
