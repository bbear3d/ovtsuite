# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag's window: the vehicle workspace.

Top bar: the interface and the vehicle as chips (each opens its pickers),
the search field that opens the command palette (Ctrl+K: modules, measuring
values, faults, coding bits, procedures, actions), the trace switch, the
definitions menu. Left, the module
rail: the vehicle's modules as cards, built from what a scan or a connect
learned (``gui_rail``). Right, the selected module: its header and session
state, and one tab per function (faults, live data, adaptation, coding,
actuator tests, basic settings, identification, procedures). Below, the bus
trace and the write journal; at the very bottom the status strip. Two other
pages share the window's stack: the scan report (``gui_report_page``) and
the vehicle tools (gateway list, vehicle live data, OBD-II, journal,
garage). Mockups: docs/OVT Diag Interface Options.pdf; plan: docs/OVT_DIAG.md.

Two kinds of worker, never both on the link at once: a one-shot job (the
scan) and the **live session** -- one module, connected, kept alive between
commands (a KW1281 module drops the link after a second of silence), every
function tab posting its commands to it. The window never touches a link.
"""
import html
import os
import queue
import time
from typing import Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import branding
from ..gui_cellmarks import OFF_FG, PENDING_BG, CellDelegate, dark_fg, mark_pending
from . import defs as D
from . import garage as G
from . import link as L
from . import report as R
from . import scan as SC
from .defs.schema import as_int
from . import palette_items as PI
from .gui_live_view import GroupEntry, LiveDataView, Reading
from .gui_palette import CommandPalette
from .gui_procedures_page import ProceduresPage
from .gui_rail import ConnectByAddressDialog, ModuleRail
from .gui_report_page import ReportPage
from .gui_widgets import (Chip, ChipButton, button_qss, CountBadge, ElidedLabel, mono_font, muted_label, restyle, scaled_font,
                          style_button, style_input, style_page, style_panel, style_tabs, tokens)
from .journal import Journal
from .kw1281 import Refused
from .model import Fault, ModuleInfo, ScanResult, Vehicle, diff_scans
from .session import ModuleSession, NotSupported

TOOL = "diag"
SETTINGS_GROUP = "diag"
#: how long a live session idles between keep-alives, by protocol -- and,
#: over CAN (TP 2.0), by transport: the module sends an A3 channel test after
#: about 0.55 s of tester silence and the channel answers it only while
#: polling, so the idle must stay under that (the logger uses 0.7 s)
KEEPALIVE_S = {"kw1281": 0.5, "kwp2000": 2.0, "uds": 1.0, "can": 0.6}


def keepalive_idle(sess) -> float:
    """The idle for ``sess`` (a :class:`ModuleSession`): the CAN transport's
    when its link is TP 2.0, else its protocol's (1.0 s unknown)."""
    link = getattr(sess, "link", None)
    if getattr(link, "transport", "") == "can":
        return KEEPALIVE_S["can"]
    return KEEPALIVE_S.get(getattr(sess, "protocol", ""), 1.0)


def _icon(name: str) -> QtGui.QIcon:
    try:
        from ..icons import icon
        return icon(name)
    except Exception:
        return QtGui.QIcon()


# ------------------------------------------------------------------ workers
class _Ctx:
    """What a one-shot job gets: a logger, a progress callback, a stop flag."""

    def __init__(self, worker):
        self._w = worker

    def log(self, msg: str):
        self._w.log.emit(str(msg))

    def progress(self, msg: str, done: int, total: int):
        self._w.progress.emit(str(msg), int(done), int(total))

    def is_stop(self) -> bool:
        return self._w._stop


class _Worker(QtCore.QThread):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(str, int, int)
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, job: Callable, parent=None):
        super().__init__(parent)
        self._job = job
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self.done.emit(self._job(_Ctx(self)))
        except Exception as ex:
            self.failed.emit(str(ex))


_STOP = object()


class LiveSession(QtCore.QThread):
    """One connected module. Commands are functions of the session, posted
    from the window and run here in order; between them the module is kept
    alive. ``result(op, payload)`` / ``failed(op, message)`` answer each."""
    log = QtCore.Signal(str)
    connected = QtCore.Signal(object)            # ModuleInfo
    disconnected = QtCore.Signal(str)            # reason ("" = asked to)
    result = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, str)

    def __init__(self, iface: L.Interface, transport: str, row: dict, resolver, generation: str,
                 can_rate: int, parent=None, bus: Optional[L.Bus] = None):
        super().__init__(parent)
        self.iface, self.transport, self.row = iface, transport, row
        self.resolver, self.generation, self.can_rate = resolver, generation, can_rate
        self.bus = bus                      # a shared CAN connection: one more TP 2.0 channel on it
        self._q: "queue.Queue" = queue.Queue()
        self._stop = False
        self.sess: Optional[ModuleSession] = None
        self.protocol = ""
        self.since = 0.0                    # when the session opened (window clock)
        #: the prompter of a procedure running on this thread, while one does:
        #: the engine blocks in it waiting for the user, so stop() must
        #: release it or the thread never sees the stop
        self.prompter = None
        self.logged_in = False              # a login accepted on this session (ends with it)

    @property
    def addr(self) -> str:
        return self.row.get("addr", "")

    def post(self, op: str, fn: Callable[[ModuleSession], object]):
        self._q.put((op, fn))

    def stop(self):
        self._stop = True
        p = self.prompter
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass
        self._q.put(_STOP)

    def run(self):
        reason = ""
        try:
            if self.bus is not None:
                link = self.bus.link_for(self.row, self.log.emit)
            else:
                link = L.open_link(self.iface, self.transport, self.row, self.log.emit, can_rate=self.can_rate)
        except Exception as ex:
            self.disconnected.emit("could not open the link: %s" % ex)
            return
        sess = ModuleSession(link, self.row, self.resolver, self.log.emit, generation=self.generation)
        self.sess = sess
        try:
            sess.connect(is_stop=lambda: self._stop)
            self.protocol = sess.protocol
            self.connected.emit(sess.info)
            idle = keepalive_idle(sess)
            while not self._stop:
                try:
                    item = self._q.get(timeout=idle)
                except queue.Empty:
                    try:
                        sess.keepalive()
                    except Exception as ex:
                        reason = "link lost: %s" % ex
                        break
                    continue
                if item is _STOP:
                    break
                op, fn = item
                try:
                    self.result.emit(op, fn(sess))
                except (Refused, NotSupported) as ex:
                    self.failed.emit(op, str(ex))
                except Exception as ex:
                    self.failed.emit(op, str(ex))
                    if _link_dead(ex):
                        reason = "link lost: %s" % ex
                        break
        except Exception as ex:
            reason = str(ex)
        finally:
            try:
                sess.close()
            except Exception:
                pass
            self.sess = None
            self.disconnected.emit(reason)


def _link_dead(ex: Exception) -> bool:
    from ..flash.transport import KLineError, KLineTimeout, LinkStopped
    return isinstance(ex, (KLineTimeout, LinkStopped)) or (
        isinstance(ex, KLineError) and "counter" in str(ex))


# ------------------------------------------------------------------ helpers
def _popover_qss() -> str:
    t = tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    return "#diag_popover{background:%s; border:1px solid %s; border-radius:8px;}" % (c(t.panel), c(t.border_strong))


def _progress_line_qss() -> str:
    t = tokens()
    return ("QProgressBar{background:transparent; border:0;}"
            "QProgressBar::chunk{background:%s;}" % t.accent.name(QtGui.QColor.NameFormat.HexArgb))


class _Popover(QtWidgets.QFrame):
    """A panel that hangs under a top-bar chip and holds ordinary widgets
    (the interface pickers, the vehicle list); a click outside closes it."""

    def __init__(self, parent):
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self.setObjectName("diag_popover")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        restyle(self, _popover_qss)
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(8)

    def popup_below(self, anchor: QtWidgets.QWidget):
        self.adjustSize()
        pos = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        scr = anchor.screen()
        if scr is not None:
            avail = scr.availableGeometry()
            pos.setX(max(avail.left(), min(pos.x(), avail.right() - self.width())))
        self.move(pos)
        self.show()


def _small_button(btn: QtWidgets.QPushButton, kind: str, pad: int) -> QtWidgets.QPushButton:
    """A compact button of the suite look (one style, kept right on a theme change)."""
    btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    restyle(btn, lambda: button_qss(kind) + "QPushButton{padding:2px %dpx;}" % pad)
    return btn


class _SearchEdit(QtWidgets.QLineEdit):
    """The top bar's search field. It looks like a field but acts as a
    button: a click on it, or tabbing into it, asks the window to open the
    command palette (``openRequested``); text typed into it while it has
    focus goes to the palette as its first characters. Escape clears it,
    then leaves it."""
    openRequested = QtCore.Signal()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        if ev.reason() in (QtCore.Qt.FocusReason.MouseFocusReason, QtCore.Qt.FocusReason.TabFocusReason,
                           QtCore.Qt.FocusReason.BacktabFocusReason):
            QtCore.QTimer.singleShot(0, self.openRequested.emit)

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        self.openRequested.emit()

    def keyPressEvent(self, ev):
        if ev.key() == QtCore.Qt.Key.Key_Escape:
            if self.text():
                self.clear()
            else:
                self.clearFocus()
            return
        super().keyPressEvent(ev)


#: where a value of a unit sits in its usual range, for the live tiles' bars
#: (unit lower-cased, degree sign dropped): ``(low, high)``
_UNIT_RANGES = {"%": (0.0, 100.0), "c": (-40.0, 215.0), "rpm": (0.0, 6000.0), "mbar": (0.0, 3000.0),
                "hpa": (0.0, 3000.0), "kpa": (0.0, 300.0), "bar": (0.0, 3.0), "v": (0.0, 16.0),
                "km/h": (0.0, 250.0), "a": (0.0, 100.0), "mg/str": (0.0, 600.0), "mg/h": (0.0, 600.0),
                "ms": (0.0, 20.0), "nm": (0.0, 500.0), "kw": (0.0, 200.0), "kg/h": (0.0, 600.0)}
UDS_GROUP = 1            # the one pseudo-group a UDS module's live DIDs are listed as


def _unit_frac(unit: str, value) -> Optional[float]:
    """0..1: where ``value`` sits in the usual range of ``unit``; None when
    the unit has no range here (no bar)."""
    if value is None or not unit:
        return None
    key = "".join(ch for ch in unit.lower() if ch not in "°� ").strip()
    rng = _UNIT_RANGES.get(key)
    if rng is None:
        return None
    lo, hi = rng
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def _strip_unit(text: str, unit: str) -> str:
    """``text`` without its trailing ``unit`` (the tile shows the unit apart)."""
    if unit and text.endswith(unit):
        return text[:-len(unit)].rstrip()
    return text


# ------------------------------------------------------------- new vehicle
class NewVehicleDialog(QtWidgets.QDialog):
    def __init__(self, generations: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New vehicle")
        f = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("Audi A6 (4F) 2006")
        self.vin = QtWidgets.QLineEdit()
        self.vin.setPlaceholderText("optional")
        self.gen = QtWidgets.QComboBox()
        self.gen.addItem("(not set: every known module)", "")
        for g in generations:
            self.gen.addItem(g, g)
        f.addRow("Name:", self.name)
        f.addRow("VIN:", self.vin)
        f.addRow("Generation:", self.gen)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return self.name.text().strip(), self.vin.text().strip().upper(), self.gen.currentData() or ""


def _table(headers: List[str], stretch: int = 1) -> QtWidgets.QTableWidget:
    t = QtWidgets.QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(stretch, QtWidgets.QHeaderView.ResizeMode.Stretch)
    t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    t.verticalHeader().setVisible(False)
    # the app's table standard (gui_cellmarks): alternating rows so the
    # Windows 11 style draws the selection solid, the delegate keeps a
    # selected cell's text legible with and without focus, in both themes
    t.setAlternatingRowColors(True)
    t.setItemDelegate(CellDelegate(t))
    return t


def _pending_style(on: bool) -> str:
    """A style sheet marking an edit field or label as changed-not-written:
    :data:`PENDING_BG`, bold, dark text (the tables' pending mark); "" clears."""
    if not on:
        return ""
    return "background: %s; color: %s; font-weight: bold" % (PENDING_BG.name(), dark_fg().name())


def _addr_tags(row: dict) -> List[str]:
    """How the address table says the module can be reached -- "K-line 0x17",
    "TP 2.0 0x07", "UDS 0x7E0": addresses known, not a protocol the module
    has confirmed."""
    out = []
    k = row.get("kline")
    if k:
        a = as_int(k.get("address"), None) if isinstance(k, dict) else None
        out.append("K-line 0x%02X" % a if a is not None else "K-line")
    d = as_int((row.get("tp20") or {}).get("dest"), None)
    if d is not None:
        out.append("TP 2.0 0x%02X" % d)
    q = as_int((row.get("isotp") or {}).get("request"), None)
    if q is not None:
        out.append("UDS 0x%03X" % q)
    return out


def _protocol_text(m: Optional[ModuleInfo]) -> str:
    """What the module actually answered with: ``KW1281 @ 9600``,
    ``KWP2000 @ 10400`` (the baud when the session recorded it),
    ``KWP2000 (TP 2.0)`` over CAN, ``UDS``; "" before it answered."""
    if m is None or not m.reached or not m.protocol:
        return ""
    p = m.protocol.upper()
    if m.protocol == "uds":
        return "UDS"
    if m.transport == "can":
        return "%s (TP 2.0)" % p
    baud = as_int((m.extra or {}).get("baud"), None)
    return "%s @ %d" % (p, baud) if baud else p


def _bold(item: QtWidgets.QTableWidgetItem, on: bool = True) -> None:
    """Emphasis without a colour of its own, so a selected row stays legible
    in either theme (a fault count, an incomplete monitor)."""
    f = item.font()
    f.setBold(on)
    item.setFont(f)


def _row(t: QtWidgets.QTableWidget, *cells) -> int:
    r = t.rowCount()
    t.insertRow(r)
    for i, c in enumerate(cells):
        t.setItem(r, i, QtWidgets.QTableWidgetItem("" if c is None else str(c)))
    return r


def _cells_rows(t: QtWidgets.QTableWidget, group: int, cells, names=()):
    t.setRowCount(0)
    for i, cell in enumerate(cells or [], 1):
        value, unit, text = cell.decoded          # text is the display string, unit included
        name = names[i - 1] if i - 1 < len(names) and names[i - 1] else ""
        _row(t, "%03d.%d" % (group, i), name, text, "%02X %02X %02X" % (cell.fid, cell.a, cell.b))


# ------------------------------------------------------------------ window
class DiagWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(branding.TOOL_NAMES.get(TOOL, "OVT Diag"))
        try:
            from ..gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMinMaxButtonsHint)
        self._fit_to_screen(1380, 860)
        self._worker: Optional[_Worker] = None
        self._lives: Dict[str, LiveSession] = {}      # live sessions by module address (several on CAN)
        self._bus: Optional[L.Bus] = None              # the shared CAN connection the sessions ride on
        self._multi_running = False
        self._multi_items: List[tuple] = []            # (addr, group, row) of the vehicle live table
        self._multi_values: Dict[tuple, object] = {}
        self._multi_idx: Dict[str, int] = {}
        self._multi_reads = 0
        self._errors: list = []
        self._packs = D.load_all(self._errors)
        self._res = D.Resolver(self._packs)
        self._garage = G.Garage()
        self._vehicle: Optional[Vehicle] = None
        self._scan: Optional[ScanResult] = None
        self._modules: List[ModuleInfo] = []
        self._trace_file = None
        self._basic_running = False
        self._pending_adapt = None
        self._transport_idx = 0                        # the transport in use, to put back when a change is refused
        self._busy_what = ""                           # the running one-shot job's label
        self._closing = False                          # closeEvent under way: reject() asks nothing more
        self._long_coding_base: Optional[bytes] = None  # the module's long coding as read, for the pending marks
        self._pending_ops: Dict[str, list] = {}        # (op, fn) waiting for a session being opened, by address
        self._coding_flags = (False, False, False)     # (long coding, uds, kwp) of the shown module
        self._extra_rows: Dict[str, dict] = {}         # rows made by Connect by address (not in the table)
        self._garage_counts = (0, 0)                   # vehicles, scans: the rail's footer
        self._namers: dict = {}                        # catalogue namers by (platform, identity)
        # the Live data tab's reads (see _live_start)
        self._live_running = False
        self._live_gen = 0                             # a result of an older run is dropped
        self._live_idx = 0
        self._live_rounds: List[float] = []
        self._live_t0 = 0.0
        self._live_groups: List[int] = []              # the ticked groups of the shown module
        self._live_entries: List[GroupEntry] = []
        self._live_checked: Dict[str, List[int]] = {}  # ticked groups remembered per module
        self._live_manual: Dict[str, List[GroupEntry]] = {}
        self._live_addr = ""
        self._live_sig = None
        self._live_filling = False
        # the command palette's data (see the palette section)
        self._palette_readings: Dict[tuple, tuple] = {}   # (addr, group, cell from 1) -> (text, unit) flowing now
        self._palette_cache: Dict[str, tuple] = {}        # addr -> (key, names, signals)
        self._palette_no_connect = False                  # ticking a group for the palette must not connect
        self._proc_ctx_sig = None                         # what the Procedures page was last told
        self._proc_list_sig = None                        # ... and which procedures
        self._build()
        self._restore()
        # the workspace wants the whole screen: maximized when _run_tool
        # exec()s it; _fit_to_screen above is the size it restores to
        self.setWindowState(self.windowState() | QtCore.Qt.WindowState.WindowMaximized)
        self._refresh_vehicles()
        self._rebuild_rail()
        self._update_connect_button()
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        for path, msg in self._errors:
            self._log("Definitions pack skipped: %s (%s)" % (path, msg))
        self._log("%d definition pack%s loaded: %s" % (
            len(self._packs), "" if len(self._packs) == 1 else "s",
            ", ".join("%s %s" % (p.id, p.version) for p in self._packs) or "none"))

    # ------------------------------------------------------------- layout
    def _build(self):
        style_page(self, "diag_window")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._session_btns: List[QtWidgets.QAbstractButton] = []    # enabled only with a session on the module
        self._connect_hints: List[QtWidgets.QLabel] = []            # shown while there is none
        self._pages: Dict[str, QtWidgets.QWidget] = {}
        self._tool_pages: Dict[str, QtWidgets.QWidget] = {}
        self.stack = QtWidgets.QStackedWidget()
        self.tools_tabs = QtWidgets.QTabWidget()
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self._build_topbar())
        self.progress_line = QtWidgets.QProgressBar()
        self.progress_line.setFixedHeight(3)
        self.progress_line.setTextVisible(False)
        self.progress_line.setRange(0, 1)
        self.progress_line.setValue(0)
        restyle(self.progress_line, _progress_line_qss)
        root.addWidget(self.progress_line)
        self.stack.addWidget(self._build_workspace())              # 0: the module workspace
        self.report_page = ReportPage()
        self.stack.addWidget(self.report_page)                     # 1: the scan report
        self.stack.addWidget(self._build_tools_page())             # 2: vehicle tools
        root.addWidget(self.stack, 1)
        root.addWidget(self._build_statusbar())
        self.palette = CommandPalette(self)                        # the Ctrl+K overlay, over the whole window
        self.palette.activated.connect(self._palette_activated)
        self.palette.dismissed.connect(self._palette_closed)
        self._wire_report_page()
        self._style_buttons()
        # a status line in a row of controls gives way instead of forcing the window wider
        self.multi_hint.setWordWrap(True)
        for lab in (self.gw_summary, self.multi_state, self.obd_proto, self.garage_summary, self.act_cur,
                    self.basic_state):
            lab.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.stack.currentChanged.connect(lambda *_: self._live_update())
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.ClickFocus)
        self.module_stack.currentChanged.connect(lambda *_: self._live_update())
        for page in list(self._pages.values()) + list(self._tool_pages.values()):
            lay = page.layout()
            if lay is not None and page not in (self.live_view, self.proc_page):
                lay.setContentsMargins(18, 14, 18, 12)
        # keyboard: Ctrl+K command palette, F5 scan all, Ctrl+L connect / disconnect the selected module
        for keys, fn in (("Ctrl+K", self._toggle_palette), ("F5", self.scan_all), ("Ctrl+L", self.toggle_connect)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(keys), self)
            sc.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)

    # --- top bar
    def _build_topbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        style_panel(bar, "diag_topbar", "bottom")
        bar.setFixedHeight(50)
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(14, 6, 14, 6)
        h.setSpacing(10)
        logo = QtWidgets.QLabel()
        logo.setPixmap(_icon("radio").pixmap(20, 20))
        h.addWidget(logo)
        name = QtWidgets.QLabel(branding.TOOL_NAMES.get(TOOL, "OVT Diag"))
        name.setFont(scaled_font(name.font(), 1.15, True))
        h.addWidget(name)
        h.addSpacing(8)
        # the link: interface, transport, CAN rate -- in a popover behind the chip
        self.transport = QtWidgets.QComboBox()
        self.transport.addItem("K-line", "kline")
        self.transport.addItem("CAN", "can")
        self.transport.addItem("UDS", "uds")
        self.transport.currentIndexChanged.connect(self._transport_changed)
        self.iface = QtWidgets.QComboBox()
        self.iface.setMinimumWidth(240)
        self.iface.currentIndexChanged.connect(lambda *_: self._update_chips())
        self.can_rate = QtWidgets.QComboBox()
        for r in (500000, 250000, 125000):
            self.can_rate.addItem("%d" % r, r)
        self.can_rate.currentIndexChanged.connect(lambda *_: self._update_chips())
        self.iface_chip = ChipButton("", "ok", "")
        self.iface_chip.clicked.connect(self._open_link_popover)
        h.addWidget(self.iface_chip)
        self.link_pop = _Popover(self)
        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.addWidget(muted_label("Interface"), 0, 0)
        form.addWidget(self.iface, 0, 1)
        rb = QtWidgets.QPushButton("Refresh")
        rb.clicked.connect(self.refresh_interfaces)
        form.addWidget(rb, 0, 2)
        form.addWidget(muted_label("Transport"), 1, 0)
        form.addWidget(self.transport, 1, 1)
        self._can_rate_label = muted_label("CAN rate")
        form.addWidget(self._can_rate_label, 2, 0)
        form.addWidget(self.can_rate, 2, 1)
        self.link_pop.body.addLayout(form)
        self.link_note = muted_label("")
        self.link_note.setWordWrap(True)
        self.link_pop.body.addWidget(self.link_note)
        self._sync_can_rate()
        # the vehicle: its list, New vehicle..., the generation
        self.vehicle_box = QtWidgets.QComboBox()
        self.vehicle_box.setMinimumWidth(260)
        self.vehicle_box.currentIndexChanged.connect(self._vehicle_changed)
        self.vehicle_chip = ChipButton("No vehicle", None, "", mono_tail=True)
        self.vehicle_chip.clicked.connect(lambda: self.veh_pop.popup_below(self.vehicle_chip))
        h.addWidget(self.vehicle_chip)
        self.veh_pop = _Popover(self)
        vf = QtWidgets.QGridLayout()
        vf.setHorizontalSpacing(10)
        vf.setVerticalSpacing(8)
        vf.addWidget(muted_label("Vehicle"), 0, 0)
        vf.addWidget(self.vehicle_box, 0, 1)
        nb = QtWidgets.QPushButton("New vehicle...")
        nb.clicked.connect(self.new_vehicle)
        vf.addWidget(nb, 0, 2)
        self.gen_box = QtWidgets.QComboBox()
        self.gen_box.setToolTip("Vehicle generation: which rows of the module address table apply")
        self.gen_box.addItem("All modules", "")
        self.gen_box.setItemData(0, "Every known module address, whatever the car", QtCore.Qt.ItemDataRole.ToolTipRole)
        for g, info in sorted(self._res.generations().items()):
            self.gen_box.addItem(g, g)
            if info.get("description"):
                self.gen_box.setItemData(self.gen_box.count() - 1, info["description"],
                                         QtCore.Qt.ItemDataRole.ToolTipRole)
        self.gen_box.currentIndexChanged.connect(self._generation_changed)
        vf.addWidget(muted_label("Generation"), 1, 0)
        vf.addWidget(self.gen_box, 1, 1)
        self.gen_desc = muted_label("")
        self.gen_desc.setWordWrap(True)
        vf.addWidget(self.gen_desc, 2, 1, 1, 2)
        self.veh_pop.body.addLayout(vf)
        # search: a field in look, a button in use -- it opens the command palette (Ctrl+K)
        self.search = _SearchEdit()
        self.search.setPlaceholderText("Search modules, measuring blocks, coding bits, DTCs, procedures...")
        self.search.setToolTip("Search everything in the window (Ctrl+K)")
        self.search.setMinimumWidth(180)
        style_input(self.search, "diag_search")
        sl = QtWidgets.QHBoxLayout(self.search)
        sl.setContentsMargins(0, 0, 8, 0)
        sl.addStretch(1)
        hint = Chip("Ctrl K", "neutral", mono=True)
        hint.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        sl.addWidget(hint)
        self.search.setTextMargins(0, 0, 58, 0)
        self.search.openRequested.connect(self._open_palette)
        self.search.textChanged.connect(lambda t: self._open_palette() if t else None)    # typed: carries over
        h.addWidget(self.search, 1)
        # the trace: a chip that is the switch
        self.record_chk = QtWidgets.QCheckBox("Record trace to file", bar)
        self.record_chk.setVisible(False)
        self.record_chk.toggled.connect(lambda *_: self._update_chips())
        self.trace_chip = ChipButton("Trace off", "neutral", "")
        self.trace_chip.setToolTip("Record every session and scan to a trace file (click to switch)")
        self.trace_chip.clicked.connect(lambda: self.record_chk.setChecked(not self.record_chk.isChecked()))
        h.addWidget(self.trace_chip)
        # the gear: definition packs and the vehicle tools
        db = QtWidgets.QToolButton()
        db.setIcon(_icon("cog"))
        db.setText("Menu")
        db.setToolTip("Definition packs and vehicle tools")
        db.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        db.setStyleSheet("QToolButton::menu-indicator{image:none;} QToolButton{padding:6px; border:1px solid transparent;"
                         " border-radius:6px;} QToolButton:hover{border-color:palette(placeholder-text);}")
        dm = QtWidgets.QMenu(db)
        dm.addAction("Install pack...", self.install_pack)
        dm.addAction("Installed packs...", self.show_packs)
        dm.addAction("Open definitions folder", self.open_defs_folder)
        dm.addAction("Reload definitions", self.reload_definitions)
        dm.addSeparator()
        dm.addAction("Vehicle tools...", lambda: self._show_tools())
        db.setMenu(dm)
        self.defs_btn = db
        h.addWidget(db)
        return bar

    def _sync_can_rate(self):
        on = self.transport.currentData() in ("can", "uds")
        self.can_rate.setVisible(on)
        self._can_rate_label.setVisible(on)

    def _open_link_popover(self):
        busy = self._link_busy()
        self.link_note.setText("Locked while sessions are open or an operation is running." if busy else "")
        self.link_note.setVisible(busy)
        self.link_pop.popup_below(self.iface_chip)


    def _update_chips(self):
        it = self._current_iface()
        t = self.transport.currentData() or "kline"
        rate = self.can_rate.currentData() or 500000
        tail = {"kline": "K-line", "can": "CAN %dk" % (rate // 1000), "uds": "UDS %dk" % (rate // 1000)}.get(t, t)
        self.iface_chip.setTexts(it.label if it is not None else "No interface", "· " + tail)
        self.iface_chip.setDot("ok" if it is not None else "warn")
        self.iface_chip.setToolTip("Interface and transport: click to change" if it is not None else
                                   "No interface found: plug in a cable or J2534 device, then Refresh")
        v = self._vehicle
        if v is None:
            self.vehicle_chip.setTexts("No vehicle", "")
            self.vehicle_chip.setToolTip("Scans are not saved without a vehicle: click to pick or add one")
        else:
            vin = v.vin or ""
            short = (vin[:8] + "…" + vin[-4:]) if len(vin) > 14 else vin
            self.vehicle_chip.setTexts(v.label, short if v.name else "")
            self.vehicle_chip.setToolTip("%s%s%s" % (v.label, ("  " + vin) if vin else "",
                                                     ("  (" + v.generation + ")") if v.generation else ""))
        self.vehicle_chip.setDot(None)
        rec = self.record_chk.isChecked()
        self.trace_chip.setTexts("Recording trace" if rec else "Trace off")
        self.trace_chip.setDot("bad" if rec else "neutral")
        self.trace_chip.setMutedText(not rec)
        g = self.generation
        info = self._res.generations().get(g, {}) if g else {}
        self.gen_desc.setText(info.get("description", "") if g else "Every known module address")
        if hasattr(self, "rail"):
            self._update_footer()

    # --- workspace
    def _build_workspace(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        style_page(page, "diag_workspace")
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.vsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.vsplit.setChildrenCollapsible(True)
        self.vsplit.setHandleWidth(3)
        top = QtWidgets.QWidget()
        th = QtWidgets.QHBoxLayout(top)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(0)
        self.rail = ModuleRail()
        self.scan_btn = self.rail.scan_btn
        self.rail.selected.connect(self._rail_selected)
        self.rail.connectRequested.connect(self._rail_connect)
        self.rail.connectByAddressRequested.connect(self.connect_by_address)
        self.rail.scanRequested.connect(self.scan_all)
        self.rail.stopRequested.connect(self.stop)
        self.rail.obdRequested.connect(lambda: self._show_tools(self._tool_pages["OBD-II"]))
        self.rail.garageRequested.connect(lambda: self._show_tools(self._tool_pages["Garage"]))
        th.addWidget(self.rail)
        self.module_stack = QtWidgets.QStackedWidget()
        empty = QtWidgets.QLabel("Select a module from the list, or press Scan all to find the car's modules.")
        empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        empty.setStyleSheet("color: palette(placeholder-text);")
        self.module_stack.addWidget(empty)
        self.module_stack.addWidget(self._build_module_view())
        th.addWidget(self.module_stack, 1)
        self.vsplit.addWidget(top)
        self.vsplit.addWidget(self._build_bottom())
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 0)
        self.vsplit.setCollapsible(0, False)
        self.vsplit.setCollapsible(1, True)
        self.vsplit.setSizes([640, 160])
        v.addWidget(self.vsplit, 1)
        return page

    def _build_module_view(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(w)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        head = QtWidgets.QWidget()
        hv = QtWidgets.QVBoxLayout(head)
        hv.setContentsMargins(22, 14, 22, 4)
        hv.setSpacing(2)
        r1 = QtWidgets.QHBoxLayout()
        r1.setSpacing(10)
        self.mod_title = QtWidgets.QLabel("")
        self.mod_title.setFont(scaled_font(self.mod_title.font(), 1.6, True))
        r1.addWidget(self.mod_title)
        r1.addStretch(1)
        # the session state, always in view: connect once, every function
        # tab works on that session until Disconnect
        self.session_state = QtWidgets.QLabel("Not connected")
        self.session_state.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.session_state.setStyleSheet("color: palette(placeholder-text);")
        r1.addWidget(self.session_state)
        self.connect_btn = QtWidgets.QPushButton(_icon("radio"), "Connect")
        self.connect_btn.setToolTip("Open a session on this module and keep it open; the function tabs work on it (Ctrl+L)")
        self.connect_btn.clicked.connect(self.toggle_connect)
        r1.addWidget(self.connect_btn)
        hv.addLayout(r1)
        r2 = QtWidgets.QHBoxLayout()
        r2.setSpacing(8)
        self.mod_sub = ElidedLabel("")
        self.mod_sub.setFont(mono_font(self.mod_sub.font(), 0.92))
        self.mod_sub.setStyleSheet("color: palette(placeholder-text);")
        r2.addWidget(self.mod_sub, 1)
        self.login_btn = QtWidgets.QPushButton(_icon("shield-check"), "Login...")
        self.login_btn.setToolTip("Send the module's login code (needed before some adaptation saves and codings)")
        self.login_btn.clicked.connect(self.login)
        r2.addWidget(self.login_btn)
        self.security_btn = QtWidgets.QPushButton(_icon("lock-up"), "Security access...")
        self.security_btn.setToolTip("KWP2000 security access (27 seed/key): what an EDC16 wants before "
                                     "adaptation, actuator tests and basic settings -- a different thing from the login")
        self.security_btn.clicked.connect(self.security_access)
        r2.addWidget(self.security_btn)
        self.disc_all_btn = QtWidgets.QPushButton("Disconnect all")
        self.disc_all_btn.setToolTip("Close every open session (several modules can be live at once over CAN)")
        self.disc_all_btn.clicked.connect(self.disconnect_all)
        self.disc_all_btn.setVisible(False)
        r2.addWidget(self.disc_all_btn)
        for b in (self.login_btn, self.security_btn, self.disc_all_btn):
            style_button(b, "ghost")
        hv.addLayout(r2)
        cv.addWidget(head)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setElideMode(QtCore.Qt.TextElideMode.ElideNone)
        style_tabs(self.tabs)
        self._build_tabs()
        self.journal_btn = QtWidgets.QPushButton("Journal · 0 writes")
        self.journal_btn.setToolTip("The write journal of this vehicle: every change OVT Diag made")
        self.journal_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        restyle(self.journal_btn, lambda: button_qss("ghost") + "QPushButton{padding:7px 10px; border-radius:4px;}")
        self.journal_btn.clicked.connect(lambda: self._show_tools(self._tool_pages["Journal"]))
        self.tabs.setCornerWidget(self.journal_btn, QtCore.Qt.Corner.TopRightCorner)
        self.tabs.currentChanged.connect(lambda *_: self._live_update())
        cv.addWidget(self.tabs, 1)
        return w

    def _build_bottom(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        style_panel(w, "diag_bottom", "top")
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(18)
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(4)
        th = QtWidgets.QHBoxLayout()
        self.trace_title = QtWidgets.QLabel("")
        th.addWidget(self.trace_title, 1)
        sb = QtWidgets.QPushButton("Save...")
        sb.clicked.connect(self.save_trace)
        th.addWidget(sb)
        cb = QtWidgets.QPushButton("Clear")
        cb.clicked.connect(lambda: self.trace.clear())
        th.addWidget(cb)
        for b in (sb, cb):
            _small_button(b, "ghost", 8)
        left.addLayout(th)
        self.trace = QtWidgets.QPlainTextEdit()
        self.trace.setReadOnly(True)
        self.trace.setMaximumBlockCount(5000)
        self.trace.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.trace.setFont(mono_font(None, 0.92))
        self.trace.setStyleSheet("QPlainTextEdit{background:transparent;}")
        left.addWidget(self.trace, 1)
        h.addLayout(left, 3)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(4)
        self.journal_title = QtWidgets.QLabel("")
        right.addWidget(self.journal_title)
        self.journal_mini = QtWidgets.QLabel("")
        self.journal_mini.setWordWrap(True)
        self.journal_mini.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.journal_mini.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self.journal_mini)
        links = QtWidgets.QHBoxLayout()
        for text, fn in (("Open journal", lambda: self._show_tools(self._tool_pages["Journal"])),
                         ("Export report", self.export_html)):
            b = QtWidgets.QPushButton(text)
            _small_button(b, "ghost", 6)
            b.clicked.connect(fn)
            links.addWidget(b)
        links.addStretch(1)
        right.addLayout(links)
        right.addStretch(1)
        h.addLayout(right, 2)
        self._update_trace_title()
        return w

    def _build_statusbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        style_panel(bar, "diag_status", "top")
        bar.setFixedHeight(32)
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(16, 3, 12, 3)
        h.setSpacing(10)
        self.status = QtWidgets.QLabel("")
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        h.addWidget(self.status, 1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(220)
        self.progress.setMaximumHeight(12)
        self.progress.setTextVisible(False)
        h.addWidget(self.progress)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.stop)
        h.addWidget(self.stop_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)      # asks first while sessions are open
        h.addWidget(close)
        _small_button(self.stop_btn, "danger", 12)
        _small_button(close, "ghost", 12)
        return bar

    def _build_tools_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        style_page(page, "diag_tools")
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(20, 12, 20, 8)
        v.setSpacing(6)
        crumb = QtWidgets.QHBoxLayout()
        back = QtWidgets.QPushButton("‹ Modules")
        back.setToolTip("Back to the module workspace (Esc)")
        style_button(back, "ghost")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        crumb.addWidget(back)
        title = QtWidgets.QLabel("Vehicle tools")
        title.setFont(scaled_font(title.font(), 1.25, True))
        crumb.addWidget(title)
        crumb.addStretch(1)
        v.addLayout(crumb)
        self.tools_tabs.setDocumentMode(True)
        self.tools_tabs.tabBar().setUsesScrollButtons(True)
        style_tabs(self.tools_tabs)
        for name in ("Gateway list", "Vehicle live data", "OBD-II", "Journal", "Garage"):
            self.tools_tabs.addTab(self._tool_pages[name], name)
        v.addWidget(self.tools_tabs, 1)
        return page

    def _show_tools(self, page: Optional[QtWidgets.QWidget] = None):
        if page is not None:
            self.tools_tabs.setCurrentWidget(page)
        self.stack.setCurrentIndex(2)

    def _style_buttons(self):
        """The suite look for every action button of the function tabs and
        the vehicle tools (a button that carries a style already -- the Live
        data view's, the rail's -- is left alone)."""
        danger = {id(b) for b in (self.clear_btn, self.obd_clear_btn)}
        pages = [p for p in self._pages.values() if p not in (self.live_view, self.proc_page)] + list(self._tool_pages.values())
        pages += [self.connect_btn.parentWidget(), self.link_pop, self.veh_pop]
        for page in pages:
            for b in page.findChildren(QtWidgets.QPushButton):
                if hasattr(b, "_restyler") or b.styleSheet():
                    continue
                if b is self.connect_btn:
                    style_button(b, "primary")
                else:
                    style_button(b, "danger" if id(b) in danger else "outline")

    def _connect_hint(self, layout) -> QtWidgets.QLabel:
        """The line a function tab shows while the module has no session."""
        lab = QtWidgets.QLabel("Connect to the module to use these functions (Connect, or Identify / "
                               "Read faults opens the session and keeps it)")
        lab.setStyleSheet("color: palette(placeholder-text)")
        lab.setWordWrap(True)
        layout.addWidget(lab)
        self._connect_hints.append(lab)
        return lab

    def _build_tabs(self):
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        self._session_btns.append(self.login_btn)
        # --- Identification
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.id_labels = {}
        for key, label in (("part", "Part number"), ("sw", "Software"), ("hw", "Hardware"), ("system", "System name"),
                           ("coding", "Coding"), ("wsc", "Workshop code"), ("text", "Identification"),
                           ("protocol", "Protocol"), ("transport", "Transport"), ("gateway", "Gateway entry"),
                           ("defs", "Definitions"), ("error", "Last error")):
            lab = QtWidgets.QLabel("-")
            lab.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            self.id_labels[key] = lab
            f.addRow(label + ":", lab)
        row = QtWidgets.QHBoxLayout()
        self.ident_btn = QtWidgets.QPushButton(_icon("cpu"), "Identify")
        self.ident_btn.clicked.connect(lambda: self.module_op("identify"))
        row.addWidget(self.ident_btn)
        row.addStretch(1)
        f.addRow(row)
        self._pages["Identification"] = w
        # --- Faults
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        self.fault_table = _table(["Code", "Text", "Status", "Freeze frame"])
        lv.addWidget(self.fault_table, 1)
        row = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton(_icon("triangle-alert"), "Read faults")
        self.read_btn.clicked.connect(lambda: self.module_op("faults"))
        self.clear_btn = QtWidgets.QPushButton(_icon("eraser"), "Clear faults")
        self.clear_btn.clicked.connect(self.clear_faults)
        self.copy_btn = QtWidgets.QPushButton(_icon("copy"), "Copy")
        self.copy_btn.clicked.connect(self.copy_faults)
        row.addWidget(self.read_btn)
        row.addWidget(self.clear_btn)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        lv.addLayout(row)
        self._pages["Faults"] = w
        # --- Live data (the LiveDataView; the window runs its reads on the session)
        self.live_view = LiveDataView()
        self.live_view.selectionChanged.connect(self._live_selection_changed)
        self.live_view.manualGroupRequested.connect(self._live_manual_group)
        self.live_view.otherModuleActivated.connect(lambda a: self.rail.select(a))
        self.live_view.splitViewRequested.connect(lambda: self._show_tools(self._tool_pages["Vehicle live data"]))
        self._pages["Live data"] = self.live_view
        # --- Adaptation
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        split = QtWidgets.QSplitter()
        self.adapt_defs = _table(["Channel", "Name", "Unit", "Source"], stretch=1)
        self.adapt_defs.itemSelectionChanged.connect(self._adapt_def_picked)
        split.addWidget(self.adapt_defs)
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Channel:"))
        self.adapt_ch = QtWidgets.QSpinBox()
        self.adapt_ch.setRange(0, 255)
        row.addWidget(self.adapt_ch)
        self.adapt_read_btn = QtWidgets.QPushButton("Read")
        self.adapt_read_btn.clicked.connect(self.adapt_read)
        row.addWidget(self.adapt_read_btn)
        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Value:"))
        self.adapt_val = QtWidgets.QSpinBox()
        self.adapt_val.setRange(0, 65535)
        row.addWidget(self.adapt_val)
        self.adapt_test_btn = QtWidgets.QPushButton("Test")
        self.adapt_test_btn.setToolTip("The module applies the value until it is saved or the session ends")
        self.adapt_test_btn.clicked.connect(self.adapt_test)
        row.addWidget(self.adapt_test_btn)
        self.adapt_save_btn = QtWidgets.QPushButton(_icon("save"), "Save")
        self.adapt_save_btn.clicked.connect(self.adapt_save)
        row.addWidget(self.adapt_save_btn)
        self._session_btns += [self.adapt_read_btn, self.adapt_test_btn, self.adapt_save_btn]
        row.addStretch(1)
        rv.addLayout(row)
        self._connect_hint(rv)
        self.adapt_name = QtWidgets.QLabel("")
        self.adapt_name.setWordWrap(True)
        rv.addWidget(self.adapt_name)
        self.adapt_current = QtWidgets.QLabel("Current value: -")
        self.adapt_current.setFont(mono)
        rv.addWidget(self.adapt_current)
        self.adapt_cells = _table(["Cell", "Name", "Value", "Raw"])
        rv.addWidget(self.adapt_cells, 1)
        split.addWidget(right)
        split.setSizes([360, 600])
        lv.addWidget(split, 1)
        self._pages["Adaptation"] = w
        # --- Coding
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Current coding:"))
        self.coding_cur = QtWidgets.QLabel("-")
        self.coding_cur.setFont(mono)
        row.addWidget(self.coding_cur)
        row.addSpacing(16)
        row.addWidget(QtWidgets.QLabel("New coding:"))
        self.coding_new = QtWidgets.QSpinBox()
        self.coding_new.setRange(0, 32767)
        self.coding_new.valueChanged.connect(self._coding_edited)
        row.addWidget(self.coding_new)
        row.addWidget(QtWidgets.QLabel("Workshop code:"))
        self.wsc_edit = QtWidgets.QSpinBox()
        self.wsc_edit.setRange(0, 99999)
        self.wsc_edit.setToolTip("Written with the coding (and with adaptation saves); defaults to the module's current one")
        row.addWidget(self.wsc_edit)
        self.coding_write_btn = QtWidgets.QPushButton(_icon("save"), "Write coding")
        self.coding_write_btn.clicked.connect(self.coding_write)
        row.addWidget(self.coding_write_btn)
        row.addStretch(1)
        lv.addLayout(row)
        self._connect_hint(lv)
        self.coding_defs = _table(["Position / id", "Meaning", "Options", "Source"], stretch=2)
        lv.addWidget(self.coding_defs, 1)
        # long-coding (UDS / kwp_dids) byte+bit editor
        lcrow = QtWidgets.QHBoxLayout()
        self.long_coding_edit = QtWidgets.QLineEdit()
        self.long_coding_edit.setFont(mono)
        self.long_coding_edit.setPlaceholderText("long coding bytes, hex (e.g. 00 10 02)")
        self.long_coding_edit.textEdited.connect(self._long_coding_edited)
        lcrow.addWidget(QtWidgets.QLabel("Long coding:"))
        lcrow.addWidget(self.long_coding_edit, 1)
        self.long_coding_write_btn = QtWidgets.QPushButton(_icon("save"), "Write long coding")
        self.long_coding_write_btn.setToolTip("Write the coding bytes over UDS (2E 06 00); the exact change is "
                                              "confirmed, journalled and the identity re-read")
        self.long_coding_write_btn.clicked.connect(self.long_coding_write)
        lcrow.addWidget(self.long_coding_write_btn)
        self.long_coding_row = QtWidgets.QWidget()
        self.long_coding_row.setLayout(lcrow)
        self.long_coding_row.setVisible(False)
        lv.addWidget(self.long_coding_row)
        self.long_coding_bits = _table(["Byte.bit", "Meaning", "Value / options", "Source"], stretch=1)
        self.long_coding_bits.itemDoubleClicked.connect(self._long_coding_bit_toggled)
        self.long_coding_bits.setVisible(False)
        lv.addWidget(self.long_coding_bits, 1)
        self.coding_hint = QtWidgets.QLabel("")
        self.coding_hint.setStyleSheet("color: palette(placeholder-text)")
        self.coding_hint.setWordWrap(True)
        lv.addWidget(self.coding_hint)
        self._pages["Coding"] = w
        # --- Actuator tests
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.act_next_btn = QtWidgets.QPushButton(_icon("toggle-right"), "Start / next actuator")
        self.act_next_btn.setToolTip("Each press moves the module to the next actuator of its sequence; "
                                     "the module keeps the current one active until the next press")
        self.act_next_btn.clicked.connect(self.actuator_next)
        row.addWidget(self.act_next_btn)
        self.act_stop_btn = QtWidgets.QPushButton("Stop")
        self.act_stop_btn.setToolTip("Leave the actuator test (the module switches its outputs back)")
        self.act_stop_btn.clicked.connect(self.actuator_stop)
        row.addWidget(self.act_stop_btn)
        self.act_cur = QtWidgets.QLabel("")
        row.addWidget(self.act_cur, 1)
        lv.addLayout(row)
        self._session_btns += [self.act_next_btn, self.act_stop_btn]
        self._connect_hint(lv)
        self.act_table = _table(["#", "Id", "Name", "Source"], stretch=2)
        lv.addWidget(self.act_table, 1)
        self.act_defs = _table(["Id", "Name", "Precondition", "Source"], stretch=1)
        lv.addWidget(QtWidgets.QLabel("Defined actuator tests for this module:"))
        lv.addWidget(self.act_defs, 1)
        self._pages["Actuator tests"] = w
        # --- Basic settings
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Group:"))
        self.basic_group = QtWidgets.QSpinBox()
        self.basic_group.setRange(0, 255)
        row.addWidget(self.basic_group)
        self.basic_btn = QtWidgets.QPushButton(_icon("sliders-horizontal"), "Start basic setting")
        self.basic_btn.setToolTip("Reads the group in basic-setting mode until stopped; the module "
                                  "performs the setting while the reads continue")
        self.basic_btn.clicked.connect(self.basic_toggle)
        row.addWidget(self.basic_btn)
        self.basic_state = QtWidgets.QLabel("")
        row.addWidget(self.basic_state, 1)
        lv.addLayout(row)
        self._session_btns.append(self.basic_btn)
        self._connect_hint(lv)
        self.basic_cells = _table(["Cell", "Name", "Value", "Raw"])
        lv.addWidget(self.basic_cells, 1)
        self.basic_defs = _table(["Group", "Name", "Precondition", "Source"], stretch=1)
        self.basic_defs.itemSelectionChanged.connect(self._basic_def_picked)
        lv.addWidget(QtWidgets.QLabel("Defined basic settings for this module:"))
        lv.addWidget(self.basic_defs, 1)
        self._pages["Basic settings"] = w
        # --- Procedures: the gallery of guided procedures and the runner (ProceduresPage)
        self.proc_page = ProceduresPage()
        self.proc_page.runRequested.connect(self._start_procedure)
        self.proc_page.newProcedureRequested.connect(self._new_procedure)
        self.proc_page.traceRequested.connect(self._show_bus_trace)
        self._pages["Procedures"] = self.proc_page
        # --- Journal
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        self.journal_table = _table(["When", "What", "Verified"])
        lv.addWidget(self.journal_table, 1)
        self._tool_pages["Journal"] = w
        # --- Gateway list (vehicle level)
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.gw_read_btn = QtWidgets.QPushButton(_icon("network"), "Read list from gateway")
        self.gw_read_btn.setToolTip("Ask the CAN gateway which modules the car has and at which TP 2.0 addresses "
                                    "(22 04 A1); a scan over CAN does this first by itself")
        self.gw_read_btn.clicked.connect(self.read_gateway_list)
        row.addWidget(self.gw_read_btn)
        self.gw_save_btn = QtWidgets.QPushButton(_icon("save"), "Save new addresses to my local pack")
        self.gw_save_btn.setToolTip("Record the modules the gateway lists that the bundled address table does not "
                                    "know, as addressing rows in your local definitions pack")
        self.gw_save_btn.clicked.connect(self.save_gateway_addresses)
        self.gw_save_btn.setEnabled(False)
        row.addWidget(self.gw_save_btn)
        self.gw_summary = QtWidgets.QLabel("")
        self.gw_summary.setStyleSheet("color: palette(placeholder-text)")
        row.addWidget(self.gw_summary, 1)
        lv.addLayout(row)
        self.gw_table = _table(["Addr", "Module", "TP 2.0", "Bus", "Status", "Last scan"], stretch=1)
        lv.addWidget(self.gw_table, 1)
        self._tool_pages["Gateway list"] = w
        # --- Vehicle live data (several modules at once, CAN)
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Module:"))
        self.multi_module = QtWidgets.QComboBox()
        self.multi_module.setMinimumWidth(220)
        row.addWidget(self.multi_module)
        row.addWidget(QtWidgets.QLabel("Group:"))
        self.multi_group = QtWidgets.QSpinBox()
        self.multi_group.setRange(0, 255)
        self.multi_group.setValue(1)
        row.addWidget(self.multi_group)
        b = QtWidgets.QPushButton("Add")
        b.clicked.connect(self.multi_add)
        row.addWidget(b)
        b = QtWidgets.QPushButton("Remove selected")
        b.clicked.connect(self.multi_remove)
        row.addWidget(b)
        row.addSpacing(12)
        self.multi_btn = QtWidgets.QPushButton(_icon("gauge"), "Start")
        self.multi_btn.setToolTip("Open a TP 2.0 channel to each module in the list and keep reading its groups, "
                                  "all channels at once")
        self.multi_btn.clicked.connect(self.multi_toggle)
        row.addWidget(self.multi_btn)
        self.multi_state = QtWidgets.QLabel("")
        self.multi_state.setStyleSheet("color: palette(placeholder-text)")
        row.addWidget(self.multi_state, 1)
        lv.addLayout(row)
        self.multi_table = _table(["Module", "Group", "Cell", "Name", "Value", "Raw"], stretch=3)
        lv.addWidget(self.multi_table, 1)
        self.multi_hint = QtWidgets.QLabel("Needs the CAN transport: each module gets its own TP 2.0 channel on the "
                                           "one connection. On K-line one module at a time (Live data tab).")
        self.multi_hint.setStyleSheet("color: palette(placeholder-text)")
        lv.addWidget(self.multi_hint)
        self._tool_pages["Vehicle live data"] = w
        # --- Generic OBD-II (vehicle level)
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.obd_read_btn = QtWidgets.QPushButton(_icon("leaf"), "Read OBD-II")
        self.obd_read_btn.setToolTip("Generic emissions diagnostics (SAE J1979): readiness, emissions codes, the "
                                     "standard live values and the VIN -- over 11-bit CAN (0x7DF) or K-line (0x33)")
        self.obd_read_btn.clicked.connect(self.obd_read)
        row.addWidget(self.obd_read_btn)
        self.obd_clear_btn = QtWidgets.QPushButton(_icon("eraser"), "Clear emissions codes")
        self.obd_clear_btn.clicked.connect(self.obd_clear)
        row.addWidget(self.obd_clear_btn)
        cb = QtWidgets.QPushButton(_icon("copy"), "Copy")
        cb.clicked.connect(self.copy_obd)
        row.addWidget(cb)
        self.obd_proto = QtWidgets.QLabel("")
        self.obd_proto.setStyleSheet("color: palette(placeholder-text)")
        row.addWidget(self.obd_proto, 1)
        lv.addLayout(row)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.obd_status = _table(["Module", "MIL", "Codes", "Monitor", "Complete"], stretch=3)
        split.addWidget(self.obd_status)
        self.obd_dtcs = _table(["Code", "Module", "Kind", "Text"], stretch=3)
        split.addWidget(self.obd_dtcs)
        self.obd_pids = _table(["Module", "PID", "Name", "Value"], stretch=2)
        split.addWidget(self.obd_pids)
        split.setSizes([150, 120, 300])
        lv.addWidget(split, 1)
        self.obd_info = QtWidgets.QLabel("")
        self.obd_info.setWordWrap(True)
        self.obd_info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        lv.addWidget(self.obd_info)
        self._tool_pages["OBD-II"] = w
        # --- Garage (vehicle level)
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        nb = QtWidgets.QPushButton("New vehicle...")
        nb.clicked.connect(self.new_vehicle)
        row.addWidget(nb)
        self.garage_use_btn = QtWidgets.QPushButton("Use this vehicle")
        self.garage_use_btn.setToolTip("Make the selected vehicle the one the workspace shows and scans are saved to")
        self.garage_use_btn.clicked.connect(self.garage_use)
        row.addWidget(self.garage_use_btn)
        self.garage_del_btn = QtWidgets.QPushButton("Delete vehicle...")
        self.garage_del_btn.setToolTip("Delete the selected vehicle with its scans and journal (asks first)")
        self.garage_del_btn.clicked.connect(self.garage_delete)
        row.addWidget(self.garage_del_btn)
        self.garage_summary = QtWidgets.QLabel("")
        self.garage_summary.setStyleSheet("color: palette(placeholder-text)")
        row.addWidget(self.garage_summary, 1)
        lv.addLayout(row)
        self.garage_table = _table(["Vehicle", "VIN", "Generation", "Scans", "Last scan"], stretch=0)
        self.garage_table.itemDoubleClicked.connect(lambda *_: self.garage_use())
        lv.addWidget(self.garage_table, 1)
        self._tool_pages["Garage"] = w
        # --- the order of the tabs
        self.faults_badge = CountBadge(0, "bad")
        for name in ("Faults", "Live data", "Adaptation", "Coding", "Actuator tests", "Basic settings",
                     "Identification", "Procedures"):
            self.tabs.addTab(self._pages[name], name)
        self.tabs.tabBar().setTabButton(0, QtWidgets.QTabBar.ButtonPosition.RightSide, self.faults_badge)
        self.faults_badge.setVisible(False)


    def _fit_to_screen(self, w, h):
        scr = self.screen() or QtWidgets.QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(w, int(avail.width() * 0.95))
            h = min(h, int(avail.height() * 0.90))
        self.resize(w, h)

    # ------------------------------------------------------------ settings
    def _settings(self) -> QtCore.QSettings:
        s = QtCore.QSettings(branding.ORG, branding.APP_NAME)
        s.beginGroup(SETTINGS_GROUP)
        return s

    def _restore(self):
        s = self._settings()
        t = s.value("transport", "kline")
        i = self.transport.findData(t)
        if i >= 0:
            self.transport.setCurrentIndex(i)
        self._last_iface = s.value("interface", "")
        self._last_vehicle = s.value("vehicle", "")
        self.record_chk.setChecked(str(s.value("record", "false")).lower() == "true")
        self.refresh_interfaces()

    def _save(self):
        s = self._settings()
        s.setValue("transport", self.transport.currentData())
        s.setValue("interface", self.iface.currentText())
        s.setValue("vehicle", self._vehicle.id if self._vehicle else "")
        s.setValue("record", self.record_chk.isChecked())

    # ---------------------------------------------------------- interfaces
    def refresh_interfaces(self):
        want = self.iface.currentText() or getattr(self, "_last_iface", "")
        self.iface.clear()
        can = self.transport.currentData() in ("can", "uds")
        for it in L.interfaces():
            if can and not it.can_capable:
                continue
            self.iface.addItem(it.label, it)
        i = self.iface.findText(want)
        if i >= 0:
            self.iface.setCurrentIndex(i)
        if self.iface.count() == 0:
            self.iface.addItem("(no interface found - plug in a cable and Refresh)", None)
        self._update_chips()

    def _link_busy(self) -> bool:
        """Live sessions open, or a one-shot job on the link."""
        return bool(self._lives) or (self._worker is not None and self._worker.isRunning())

    def _set_link_controls(self, enabled: bool):
        """The transport, interface and CAN-rate pickers: locked while the
        link is in use (the sessions ride on what they chose)."""
        for w in (self.transport, self.iface, self.can_rate):
            w.setEnabled(enabled)

    def _one_session_note(self) -> str:
        return "one session at a time on %s" % ("UDS" if self.transport.currentData() == "uds" else "K-line")

    def _transport_changed(self):
        if self._link_busy():
            # the open sessions ride on this transport's link: put the picker
            # back (no second round through here) and say why
            self.transport.blockSignals(True)
            self.transport.setCurrentIndex(self._transport_idx)
            self.transport.blockSignals(False)
            self._sync_can_rate()
            self.status.setText("Disconnect the live sessions and let the running operation finish "
                                "before changing the transport")
            return
        self._transport_idx = self.transport.currentIndex()
        self._sync_can_rate()
        self._close_bus()
        self.refresh_interfaces()
        self._fill_multi_modules()
        self._update_chips()

    def _addr_ok(self, row: dict, transport: str) -> bool:
        if transport == "can":
            return (row.get("tp20") or {}).get("dest") is not None
        if transport == "uds":
            return (row.get("isotp") or {}).get("request") is not None
        return True

    def _no_addr_message(self, addr: str, transport: str):
        what = "a TP 2.0 address" if transport == "can" else "ISO-TP ids (isotp.request/response)"
        QtWidgets.QMessageBox.information(self, self.windowTitle(),
                                          "Module %s has no %s in the definitions; use another transport, "
                                          "or add it to your local pack once known." % (addr, what))

    def _current_iface(self) -> Optional[L.Interface]:
        it = self.iface.currentData()
        return it if isinstance(it, L.Interface) else None

    # ------------------------------------------------------------ vehicles
    def _refresh_vehicles(self, select: Optional[str] = None):
        self.vehicle_box.blockSignals(True)
        self.vehicle_box.clear()
        self.vehicle_box.addItem("(no vehicle - scans are not saved)", None)
        for v in self._garage.vehicles():
            self.vehicle_box.addItem(v.label, v.id)
        want = select or getattr(self, "_last_vehicle", "")
        i = self.vehicle_box.findData(want) if want else -1
        self.vehicle_box.setCurrentIndex(i if i >= 0 else 0)
        self.vehicle_box.blockSignals(False)
        self._vehicle_changed()

    def _vehicle_changed(self):
        vid = self.vehicle_box.currentData()
        self._vehicle = self._garage.vehicle(vid) if vid else None
        if self._vehicle is not None:
            i = self.gen_box.findData(self._vehicle.generation or "")
            self.gen_box.blockSignals(True)
            self.gen_box.setCurrentIndex(i if i >= 0 else 0)
            self.gen_box.blockSignals(False)
            self._scan = self._garage.latest_scan(self._vehicle.id)
            self._modules = list(self._scan.modules) if self._scan else []
        else:
            self._scan = None
            self._modules = []
        self._rebuild_rail()
        self._refresh_scans()
        self._refresh_journal()
        self._fill_gateway_table()
        self._refresh_garage()
        self._update_chips()

    def _generation_changed(self):
        if self._vehicle is not None:
            self._vehicle.generation = self.gen_box.currentData() or ""
            self._garage.save_vehicle(self._vehicle)
        self._rebuild_rail()
        self._fill_multi_modules()
        self._update_chips()
        self._fill_live_groups()

    def new_vehicle(self):
        dlg = NewVehicleDialog(sorted(self._res.generations()), self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        name, vin, gen = dlg.values()
        if not name and not vin:
            return
        v = self._garage.new_vehicle(name or vin, vin, gen)
        self._refresh_vehicles(select=v.id)

    @property
    def generation(self) -> str:
        return self.gen_box.currentData() or ""

    # ----------------------------------------------------------------- rail
    def _rows(self) -> List[dict]:
        return self._res.addressing(self.generation or None)

    def _all_rows(self) -> List[dict]:
        """The generation's address table plus the rows made by Connect by
        address."""
        rows = list(self._rows())
        have = {r["addr"] for r in rows}
        return rows + [r for a, r in self._extra_rows.items() if a not in have]

    def _rebuild_rail(self):
        before = self._current_addr()
        rows = self._all_rows()
        infos = {m.addr: m for m in self._modules}
        tips = {}
        for r in rows:
            bits = []
            pt = _protocol_text(infos.get(r["addr"]))
            if pt:
                bits.append("Answered with " + pt)
            tags = _addr_tags(r)
            if tags:
                bits.append("Addresses known: " + ", ".join(tags))
            if bits:
                tips[r["addr"]] = "\n".join(bits)
        self.rail.set_modules(rows, infos, set(self._lives), self._gateway_absent(), tips)
        self.rail.set_summary(self._rail_summary())
        self._update_footer()
        if hasattr(self, "multi_module"):
            self._fill_multi_modules()
        if self._current_addr() != before:
            self._show_module()

    def _rail_summary(self) -> str:
        visited = [m for m in self._modules if m.reached is not None]
        reached = [m for m in visited if m.reached]
        with_faults = sum(1 for m in reached if m.fault_count)
        tail = (" · %d with faults" % with_faults) if with_faults else ""
        scan = self._scan
        if scan is None:
            if not visited:
                return "No scan yet"
            return "Direct connections · %d of %d reached%s" % (len(reached), len(visited), tail)
        if scan.stopped:
            return "Scan stopped: %d not tried" % sum(1 for m in scan.modules if m.reached is None)
        counts = "%d of %d reached%s" % (len(reached), len(visited), tail)
        if scan.basis == "gateway":
            return "From gateway list · " + counts
        names = {"kline": "K-line", "can": "CAN", "uds": "UDS"}
        return "Scanned %s on %s · %s" % (time.strftime("%H:%M", time.localtime(scan.when)),
                                              names.get(scan.transport, scan.transport or "?"), counts)

    def _update_footer(self):
        can = self.transport.currentData() in ("can", "uds")
        n_veh, n_scans = getattr(self, "_garage_counts", (0, 0))
        self.rail.set_footer("ISO 15765" if can else "ISO 9141", n_veh, n_scans)

    def _gateway_absent(self) -> set:
        """Addresses the loaded scan's gateway list has as not installed."""
        g = self._scan.gateway if (self._scan is not None and self._scan.gateway) else None
        if not g:
            return set()
        return {e.get("addr") for e in g.get("entries") or [] if not e.get("status")}

    def _current_addr(self) -> str:
        return self.rail.current_addr() if hasattr(self, "rail") else ""

    def _row_for(self, addr: str) -> Optional[dict]:
        for r in self._all_rows():
            if r["addr"] == addr:
                return r
        m = self._module_info(addr)
        return {"addr": addr, "name": m.name} if m is not None else None

    def _current_row(self) -> Optional[dict]:
        addr = self._current_addr()
        return self._row_for(addr) if addr else None

    def _module_info(self, addr: str) -> Optional[ModuleInfo]:
        return next((m for m in self._modules if m.addr == addr), None)

    def _store_module(self, info: ModuleInfo):
        self._modules = [m for m in self._modules if m.addr != info.addr] + [info]
        self._modules.sort(key=lambda m: m.addr)
        self._rebuild_rail()
        if self._current_addr() == info.addr:
            self._show_module()

    def _rail_selected(self, addr: str):
        self._show_module()
        self._live_update()

    def _rail_connect(self, addr: str):
        """A module card double-clicked: open its session (the card was
        selected by the first click)."""
        if addr == self._current_addr() and addr not in self._lives:
            self.toggle_connect()

    def connect_by_address(self):
        """``+ Connect by address...``: a module that is not in the list."""
        dlg = ConnectByAddressDialog(self.transport.currentData() or "kline", self, self._rows())
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        row = dlg.row()
        if row is None:
            return
        if row["addr"] not in {r["addr"] for r in self._rows()}:
            self._extra_rows[row["addr"]] = row
        self._rebuild_rail()
        self.rail.select(row["addr"])
        self.connect_module(row)

    def _open_module(self, addr: str):
        """Show the workspace on ``addr`` (a report row was activated)."""
        self.stack.setCurrentIndex(0)
        self.rail.select(addr)

    def _open_group(self, addr: str, group: int):
        """The report's ``open live group 011``: the module's Live data tab,
        with that group ticked."""
        self._open_module(addr)
        self.tabs.setCurrentWidget(self.live_view)
        if group:
            self._live_manual_group(int(group))

    # ------------------------------------------------------- command palette
    # Ctrl+K (or a click on the top bar's search field): the CommandPalette
    # overlay over the whole window. The items are built fresh on every open
    # from what the window already knows (palette_items.build_items); a choice
    # comes back to _palette_activated, which navigates by the item's kind.
    # Nothing here connects to a module: a target that needs a session just
    # opens its tab.
    def _palette_context(self) -> "PI.Context":
        sel = self._current_addr()
        return PI.Context(
            resolver=self._res, generation=self.generation, modules=list(self._modules),
            rows_by_addr={r["addr"]: r for r in self._all_rows()}, lives=set(self._lives),
            readings=dict(self._palette_readings),
            names_for=self._palette_names_for, signals_for=self._palette_signals_for,
            procedures_for=self._palette_procedures_for, selected_addr=sel,
            recording=self.record_chk.isChecked(),
            scanning=bool(self._worker is not None and self._worker.isRunning()
                          and self._busy_what.lower().startswith("scanning")))

    def _palette_items(self) -> list:
        return PI.build_items(self._palette_context())

    def _palette_cells(self, addr: str):
        """``(names, signals)`` of the module's measuring cells for the palette:
        what the Live data tab lists (definitions first, else the factory
        catalogue of its part number); remembered per module until the
        identity or the definitions change."""
        m = self._module_info(addr)
        if m is None or not m.reached:
            return {}, {}
        key = (id(self._res), self.generation, m.platform, m.identity_text, m.part, m.protocol)
        hit = self._palette_cache.get(addr)
        if hit is not None and hit[0] == key:
            return hit[1], hit[2]
        entries, _note = self._live_entries_for(addr)
        names = {e.group: list(e.cells) for e in entries if any(e.cells)}
        lv = self._lives.get(addr)
        sess = lv.sess if lv is not None else None
        if sess is not None:                       # a live module: the session's own naming fills what is missing
            for e in entries:
                if not any(e.cells) and e.group != UDS_GROUP:
                    try:
                        got = sess.group_names(e.group)
                    except Exception:
                        got = []
                    if any(got):
                        names[e.group] = list(got)
        signals: Dict[tuple, str] = {}
        namer = self._catalogue_namer(m)
        if namer is not None:
            named = self._res.measuring_names(self._ident_for(addr))
            for g in namer.populated_groups():
                if any(named.get(g, [])):
                    continue                       # the definitions named this group, not the catalogue
                for f in range(namer.slots(g)):
                    label, sig, _unit = namer.resolve(g, f)
                    if sig and not label.startswith("Group "):
                        signals[(g, f + 1)] = sig
        self._palette_cache[addr] = (key, names, signals)
        return names, signals

    def _palette_names_for(self, addr: str):
        return self._palette_cells(addr)[0]

    def _palette_signals_for(self, addr: str):
        return self._palette_cells(addr)[1]

    def _palette_procedures_for(self, addr: str):
        from . import procedures as PR
        return PR.available(self._res, self._ident_for(addr))

    def _open_palette(self, prefill: Optional[str] = None):
        """Open the palette over the window; the top field's text, if any,
        goes in as the first characters (and the field goes back to empty)."""
        if self.palette.is_open():
            return
        if prefill is None:
            prefill = self.search.text()
        if self.search.text():
            self.search.clear()
        self.palette.popup(self._palette_items(), prefill)

    def _toggle_palette(self):
        if self.palette.is_open():
            self.palette.close_palette()
            self._palette_closed()
        else:
            self._open_palette()

    def _palette_closed(self):
        """Closed: the keyboard goes back to the window, not to the search
        field (which would open the palette again)."""
        self.search.clearFocus()
        self.setFocus()

    def _palette_live_value(self, addr: str, group: int, cells):
        """Remember the readings of one group (the palette shows them beside
        the live items) and update an open palette."""
        for k in [k for k in self._palette_readings if k[0] == addr and k[1] == group]:
            del self._palette_readings[k]
        upd = {}
        for i, r in enumerate(cells, 1):
            self._palette_readings[(addr, group, i)] = (r.text, r.unit)
            upd[(addr, "live", "%d.%d" % (group, i))] = (r.text + (" " + r.unit if r.unit else "")).strip()
        if upd and self.palette.is_open():
            self.palette.set_values(upd)

    def _palette_activated(self, item, add: bool = False):
        """The palette's choice. ``add`` (Shift+Enter) only adds a live item's
        group to the module's live view and goes nowhere; every other kind
        navigates: module, tab, row."""
        kind = item.kind
        adding = add and kind == "live"
        if not adding:
            self.palette.close_palette()
            self._palette_closed()
        payload = item.payload or {}
        try:
            if kind == "action":
                self._palette_action(payload.get("action", ""))
            elif adding:
                self._palette_tick(item.addr, int(payload.get("group", 0)), None, navigate=False)
                # the palette closed itself on the choice: it comes back with the same query, so
                # several values can be added in a row, and the window stays where it was
                self.palette.popup(self._palette_items(), self.palette.query())
            else:
                handler = getattr(self, "_palette_go_" + kind, None)
                if handler is None:
                    return
                self.stack.setCurrentIndex(0)
                handler(item, payload)
        except Exception as ex:                    # a choice must never break the window
            self._log("Palette: %s: %s" % (item.title, ex))
            self.status.setText("Could not open %s: %s" % (item.title, ex))

    def _palette_open_tab(self, item, page: QtWidgets.QWidget):
        """The item's module selected and one of its function tabs showing."""
        self._open_module(item.addr)
        self.tabs.setCurrentWidget(page)

    def _palette_go_module(self, item, payload):
        self._open_module(item.addr)

    def _palette_go_live(self, item, payload):
        self._palette_open_tab(item, self.live_view)
        self._palette_tick(item.addr, int(payload.get("group", 0)), payload.get("cell"), navigate=True)

    def _palette_tick(self, addr: str, group: int, cell, navigate: bool):
        """Tick a measuring group of ``addr`` in its live view. Navigating
        (Enter) ticks it the usual way (so the session opens, as for any
        tick on the tab) and flashes the cell's tile; adding (Shift+Enter)
        ticks it without a session or a tab change: on the shown module the
        view takes it now, on another one it is remembered for when that
        module is shown."""
        if not group:
            return
        if addr == self._current_addr():
            row = self.live_view.row(group)
            if row is not None and row.isEnabled():
                if not row.isChecked():
                    self._palette_no_connect = not navigate
                    try:
                        row.checkbox().setChecked(True)
                    finally:
                        self._palette_no_connect = False
            elif navigate:
                self._live_manual_group(group)
            if navigate and cell:
                self._flash_tile(group, int(cell))
        else:
            checked = self._live_checked.setdefault(addr, [])
            if group not in checked:
                checked.append(group)
        if not navigate:
            self.status.setText("%s group %03d added to the live view" % (addr, group))

    def _flash_tile(self, group: int, cell: int):
        """Draw attention to one tile of the live view for a moment (selected
        look) and scroll it into view."""
        lv = self.live_view
        try:
            tile = lv.tiles()[lv.tile_groups().index((group, cell - 1))]
        except (ValueError, IndexError):
            return
        area = tile.parentWidget()
        while area is not None and not isinstance(area, QtWidgets.QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.ensureWidgetVisible(tile)
        tile.setSelected(True)

        def off():
            try:
                tile.setSelected(False)
            except RuntimeError:                   # the tile went with a group untick
                pass
        QtCore.QTimer.singleShot(1500, off)

    def _select_row(self, t: QtWidgets.QTableWidget, row: int):
        if 0 <= row < t.rowCount():
            t.selectRow(row)
            t.scrollToItem(t.item(row, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def _palette_go_fault(self, item, payload):
        addr = payload.get("addr") or item.addr
        self._open_module(addr)
        self.tabs.setCurrentWidget(self._pages["Faults"])
        m = self._module_info(addr)
        faults = m.faults if m is not None else []
        idx = payload.get("index")
        if not (isinstance(idx, int) and 0 <= idx < len(faults) and faults[idx].code == payload.get("code")):
            idx = next((i for i, f in enumerate(faults) if f.code == payload.get("code")), -1)
        self._select_row(self.fault_table, idx)

    def _palette_go_coding(self, item, payload):
        self._open_module(item.addr)
        self.tabs.setCurrentWidget(self._pages["Coding"])
        byte, bit = payload.get("byte"), payload.get("bit")
        t = self.long_coding_bits
        for r in range(t.rowCount()):
            meta = t.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if meta and meta[0] == byte and bit in meta[1]:
                self._select_row(t, r)
                return
        t = self.coding_defs                       # the long coding is not read yet: the definition's own row
        for r in range(t.rowCount()):
            if t.item(r, 1).text() == item.title:
                self._select_row(t, r)
                return

    def _palette_go_adaptation(self, item, payload):
        self._open_module(item.addr)
        self.tabs.setCurrentWidget(self._pages["Adaptation"])
        t = self.adapt_defs
        for r in range(t.rowCount()):
            if t.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole) == payload.get("id"):
                self._select_row(t, r)
                break
        self.adapt_ch.setValue(int(payload.get("channel", 0)))

    def _palette_go_actuator(self, item, payload):
        self._open_module(item.addr)
        self.tabs.setCurrentWidget(self._pages["Actuator tests"])
        t = self.act_defs
        for r in range(t.rowCount()):
            if t.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole) == payload.get("id"):
                self._select_row(t, r)
                break

    def _palette_go_procedure(self, item, payload):
        self._open_module(payload.get("addr") or item.addr)
        self.tabs.setCurrentWidget(self.proc_page)
        self.proc_page.select(payload.get("id", ""))

    def _palette_action(self, name: str):
        tools = self._tool_pages
        actions = {
            "scan_all": self.scan_all, "stop_scan": self.stop,
            "connect": lambda: self._live is None and self.toggle_connect(),
            "disconnect": self.disconnect_live, "disconnect_all": self.disconnect_all,
            "open_journal": lambda: self._show_tools(tools["Journal"]),
            "export_html": lambda: self._export("html"), "export_pdf": lambda: self._export("pdf"),
            "export_text": lambda: self._export("text"),
            "open_tools": lambda: self._show_tools(), "open_obd": lambda: self._show_tools(tools["OBD-II"]),
            "open_garage": lambda: self._show_tools(tools["Garage"]),
            "open_gateway": lambda: self._show_tools(tools["Gateway list"]),
            "trace_toggle": lambda: self.record_chk.setChecked(not self.record_chk.isChecked()),
            "install_pack": self.install_pack, "reload_definitions": self.reload_definitions,
            "open_definitions_folder": self.open_defs_folder,
        }
        fn = actions.get(name)
        if fn is not None:
            fn()

    # ----------------------------------------------------------- module view
    def _ident_for(self, addr: str) -> D.Identity:
        m = self._module_info(addr)
        return (m.identity if m else D.Identity(addr=addr)).with_(generation=self.generation or None)

    def _show_module(self):
        row = self._current_row()
        if row is None:
            self.module_stack.setCurrentIndex(0)             # nothing selected: the empty state
            self._update_connect_button()
            self._live_update()
            return
        self.module_stack.setCurrentIndex(1)
        addr = row["addr"]
        m = self._module_info(addr)
        self.mod_title.setText("%s  %s" % (addr, row.get("name", "")))
        sub = []
        if m is not None and m.reached:
            sub.append(m.part or m.identity_text)
            desc = (m.extra or {}).get("system_name") or m.identity_text
            if desc and desc != m.part:
                sub.append(desc)
            if m.sw:
                sub.append("SW " + m.sw)
            if m.coding_text:
                sub.append("coding " + m.coding_text)
            if m.wsc is not None:
                sub.append("WSC %05d" % m.wsc)
        elif m is not None and m.reached is False:
            sub.append("not reached: " + (m.error or "no answer"))
        else:
            sub.append("not scanned yet")
        if not (m is not None and m.reached):
            tags = _addr_tags(row)
            sub.append(("Addresses known: " + ", ".join(tags)) if tags else "no address in the definitions")
        self.mod_sub.setText(" \u00b7 ".join(x for x in sub if x))
        lab = self.id_labels
        lab["part"].setText(m.part if m and m.part else "-")
        lab["sw"].setText(m.sw if m and m.sw else "-")
        hw = " ".join(x for x in (m.extra.get("hw_part", ""), m.extra.get("hw_version", "")) if x) if m else ""
        lab["hw"].setText(hw or "-")
        lab["system"].setText((m.extra.get("system_name") or "-") if m else "-")
        lab["coding"].setText((m.coding_text + ("  (long coding, %d bytes)" % (len(m.coding_bytes) // 2)
                                                if m.coding_bytes else "")) if m and m.coding_text else "-")
        if m and m.extra.get("gateway_entry"):
            g = m.extra["gateway_entry"]
            lab["gateway"].setText("TP 2.0 dest 0x%02X, status 0x%02X, bus 0x%02X" % (
                int(g.get("dest", 0)), int(g.get("status", 0)), int(g.get("bus", 0))))
        elif m and m.extra.get("installation_list"):
            lab["gateway"].setText("this module serves the installation list (%d entries)"
                                   % len(m.extra["installation_list"].get("entries") or []))
        else:
            lab["gateway"].setText("-")
        lab["wsc"].setText(("%05d" % m.wsc + (" (importer %s, equipment %s)" % (
            m.extra.get("importer"), m.extra.get("equipment")) if m.extra.get("importer") is not None else ""))
                           if m and m.wsc is not None else "-")
        lab["text"].setText(m.identity_text if m and m.identity_text else "-")
        lab["protocol"].setText(m.protocol.upper() if m and m.protocol else "-")
        lab["transport"].setText(m.transport.upper() if m and m.transport else "-")
        lab["error"].setText(m.error if m and m.error else "-")
        ident = self._ident_for(addr)
        cov = self._res.coverage(ident)
        lab["defs"].setText(", ".join("%d %s" % (n, k.replace("_", " ")) for k, n in cov.items() if n)
                            or "nothing yet for this module")
        self._fill_faults(m.faults if m else [])
        self._fill_definition_tabs(ident, m)
        self._fill_live_groups()
        self._update_connect_button()
        self._live_update()

    def _fill_faults(self, faults: List[Fault]):
        t = self.fault_table
        t.setRowCount(0)
        for f in faults:
            _row(t, f.display_code, f.text or "(no text in the definitions)",
                 f.status_text or "0x%02X" % f.status,
                 " | ".join(str(v) for v in f.freeze.values()) if f.freeze else "")
        t.resizeColumnToContents(0)
        n = len(faults)
        self.faults_badge.setCount(n, "warn" if (n and all(f.intermittent for f in faults)) else "bad")
        self.faults_badge.setVisible(bool(n))

    def _fill_definition_tabs(self, ident: D.Identity, m: Optional[ModuleInfo]):
        # adaptation channels
        t = self.adapt_defs
        t.setRowCount(0)
        v = self._res.view("adaptation", ident)
        for eid, r in sorted(v.entries.items(), key=lambda kv: as_int(kv[1].entry.get("channel"), 0)):
            e = r.entry
            rr = _row(t, "%03d" % as_int(e.get("channel"), 0), e.get("name", ""), e.get("unit", ""),
                      "%s (%s)" % (r.pack_id, r.confidence))
            t.item(rr, 0).setData(QtCore.Qt.ItemDataRole.UserRole, eid)
        # coding
        self.coding_cur.setText(m.coding_text if m and m.coding_text else "-")
        kwp = bool(m and m.protocol == "kwp2000")
        long_coding = bool(m and m.coding_bytes)
        self.coding_new.setMaximum(16777215 if kwp else 32767)
        self.coding_new.setEnabled(not long_coding)
        if m and m.coding is not None and not long_coding and m.coding <= self.coding_new.maximum():
            self.coding_new.setValue(m.coding)
        self._coding_edited(self.coding_new.value())
        if m and m.wsc is not None:
            self.wsc_edit.setValue(min(m.wsc, 99999))
        t = self.coding_defs
        t.setRowCount(0)
        cdoc = self._res.coding_doc(ident)
        for d in cdoc.get("digits") or []:
            opts = ", ".join("%s=%s" % kv for kv in (d.get("options") or {}).items())
            _row(t, "digit %s" % d.get("position"), d.get("name", ""), opts, cdoc.get("_pack", ""))
        for v in cdoc.get("values") or []:
            _row(t, v.get("id", v.get("value", "")), v.get("name", ""), v.get("value", ""), cdoc.get("_pack", ""))
        for b in cdoc.get("bytes") or []:
            for bit in b.get("bits") or []:
                opts = ", ".join("%s=%s" % kv for kv in (bit.get("options") or {}).items()) or bit.get("type", "")
                _row(t, bit.get("id", "byte %s" % b.get("index")), bit.get("name", ""), opts, cdoc.get("_pack", ""))
        # long coding editor (UDS modules can write it; kwp_dids ones are read-only)
        uds = bool(m and m.protocol == "uds")
        self._coding_flags = (long_coding, uds, kwp)        # _update_session_ui gates the write buttons
        self.long_coding_row.setVisible(long_coding)
        self.long_coding_bits.setVisible(long_coding)
        self.long_coding_write_btn.setToolTip("Write the coding bytes over UDS (2E 06 00)" if uds else
                                              "This module serves its coding as bytes but OVT Diag has no write path "
                                              "for it yet (KWP2000 long coding); UDS modules can be written")
        if long_coding and m:
            self._long_coding_base = bytes.fromhex(m.coding_bytes)      # as read: edits differing are pending
            self.long_coding_edit.setText(_hex_spaced(m.coding_bytes))
            self._fill_long_coding_bits(ident, self._long_coding_base)
        else:
            self._long_coding_base = None
            self.long_coding_edit.clear()
            self.long_coding_bits.setRowCount(0)
        self.coding_hint.setText(
            ("Long coding (UDS): the coding bytes read with 22 06 00. Double-click a bit/enum row to flip it, or edit "
             "the hex directly, then Write long coding (2E 06 00) -- the change is confirmed, journalled and re-read."
             if uds else
             "Long coding: this module serves its coding as bytes (22 06 00); OVT Diag reads it but has no write path "
             "for a KWP2000 long coding yet.") if long_coding else
            "Short coding (KW1281): the number as the module reports it, 0..32767; the workshop code is written with it."
            if (m and m.protocol == "kw1281") else
            "KWP2000 coding: the 7-digit number written in the 0x9A record with the module's software version and "
            "workshop field; an EDC16 re-initialises afterwards.")
        # actuator tests
        t = self.act_defs
        t.setRowCount(0)
        for eid, r in self._res.view("actuator_tests", ident).entries.items():
            e = r.entry
            tid = as_int(e.get("tester_id"), None)
            rr = _row(t, "0x%04X" % tid if tid is not None else eid, e.get("name", ""),
                      _pre_text(e.get("precondition")), "%s (%s)" % (r.pack_id, r.confidence))
            t.item(rr, 0).setData(QtCore.Qt.ItemDataRole.UserRole, eid)
        # basic settings
        t = self.basic_defs
        t.setRowCount(0)
        for eid, r in sorted(self._res.view("basic_settings", ident).entries.items(),
                             key=lambda kv: as_int(kv[1].entry.get("group"), 0)):
            e = r.entry
            _row(t, "%03d" % as_int(e.get("group"), 0), e.get("name", ""), _pre_text(e.get("precondition")),
                 "%s (%s)" % (r.pack_id, r.confidence))
        self._fill_procedures(ident)

    def _fill_procedures(self, ident: Optional[D.Identity] = None):
        """The Procedures page's gallery: every procedure the packs hold, with
        where each came from (bundled / module / user) and which of them apply
        to the shown module (the others are listed dimmed)."""
        addr = self._current_addr()
        if ident is None:
            if not addr:
                return
            ident = self._ident_for(addr)
        from . import procedures as PR
        docs, origins = [], {}
        for r in self._res.procedures(None):
            d = dict(r.entry)
            d["_pack"] = r.pack_id
            docs.append(d)
            origins[str(d.get("id") or d.get("name") or "")] = r.origin
        applicable = {str(d.get("id") or d.get("name") or "") for d in PR.available(self._res, ident)}
        sig = (id(self._res), tuple(sorted(origins.items())), frozenset(applicable))
        if sig != self._proc_list_sig:                  # the same list again would only rebuild the gallery
            self._proc_list_sig = sig
            self.proc_page.set_procedures(docs, origins, applicable)
        self._proc_ctx_sig = None
        self._sync_procedures_page()

    def _sync_procedures_page(self):
        """Tell the Procedures page which module it would run on and whether
        that module has a session (it disables Run without one); only when it
        changed, because the clock calls this every second."""
        addr = self._current_addr()
        if not addr or not hasattr(self, "proc_page"):
            return
        m = self._module_info(addr)
        row = self._row_for(addr) or {}
        live = self._lives.get(addr)
        sig = (addr, (m.name if m else "") or row.get("name", ""), PI.protocol_label(m) if m else "",
               bool(live is not None and live.sess is not None), bool(live is not None and live.logged_in),
               (m.part or m.identity_text) if m else "")
        if sig == self._proc_ctx_sig:
            return
        self._proc_ctx_sig = sig
        self.proc_page.set_context(*sig[:5], part=sig[5])

    def _adapt_def_picked(self):
        items = self.adapt_defs.selectedItems()
        if items:
            self.adapt_ch.setValue(int(self.adapt_defs.item(items[0].row(), 0).text()))
            self.adapt_name.setText(self.adapt_defs.item(items[0].row(), 1).text())

    def _basic_def_picked(self):
        items = self.basic_defs.selectedItems()
        if items:
            self.basic_group.setValue(int(self.basic_defs.item(items[0].row(), 0).text()))

    def _actuator_name(self, aid: int) -> str:
        ident = self._ident_for(self._current_addr())
        for r in self._res.view("actuator_tests", ident).entries.values():
            if as_int(r.entry.get("tester_id"), -1) == aid:
                return r.entry.get("name", "")
        return "VAG %05d" % aid if 0 <= aid < 100000 else ""

    # ------------------------------------------------------------- journal
    def _journal(self) -> Optional[Journal]:
        return Journal(self._vehicle.id) if self._vehicle else None

    def _refresh_journal(self):
        t = self.journal_table
        t.setRowCount(0)
        j = self._journal()
        entries = j.entries() if j is not None else []
        for e in reversed(entries):
            _row(t, time.strftime("%Y-%m-%d %H:%M", time.localtime(e.when)),
                 e.summary + (" (reverted)" if e.reverted else ""),
                 "" if e.verified is None else ("yes" if e.verified else "NO"))
        t.resizeColumnToContents(0)
        n = len(entries)
        self.journal_btn.setText("Journal \u00b7 %d write%s" % (n, "" if n == 1 else "s"))
        self.journal_title.setText("<b>Write journal</b> \u00b7 this vehicle" if self._vehicle is not None
                                   else "<b>Write journal</b> \u00b7 no vehicle")
        tk = tokens()
        lines = []
        for e in reversed(entries[-4:]):
            tick = ""
            if e.verified is True:
                tick = " <span style='color:%s'>\u2713 verified</span>" % tk.ok.name()
            elif e.verified is False:
                tick = " <span style='color:%s'>\u2717 not verified</span>" % tk.bad.name()
            lines.append("<span style='color:%s'>%s</span>&nbsp; %s%s" % (
                tk.muted.name(), time.strftime("%H:%M", time.localtime(e.when)),
                html.escape(e.summary + (" (reverted)" if e.reverted else "")), tick))
        self.journal_mini.setText("<br>".join(lines) if lines else
                                  "<span style='color:%s'>No writes yet.</span>" % tk.muted.name())

    def _journal_record(self, **kw):
        j = self._journal()
        if j is not None:
            j.record(**kw)
            self._refresh_journal()

    # ---------------------------------------------------- one-shot jobs
    def _busy(self, on: bool, what: str = ""):
        scanning = on and what.lower().startswith("scanning")
        self.rail.set_scanning(scanning)
        self.scan_btn.setEnabled(scanning or (not on and not self._lives))     # Stop while scanning
        self.stop_btn.setEnabled(on)
        self.stop_btn.setVisible(on)
        self.progress.setVisible(on)
        if on:
            self.progress.setRange(0, 0)
            self.progress_line.setRange(0, 0)
        else:
            self.progress_line.setRange(0, 1)
            self.progress_line.setValue(0)
        self._set_link_controls(not on and not self._lives)
        self.status.setText(what)

    def _start(self, job: Callable, on_done: Callable, what: str):
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("Busy: %s" % (self._busy_what or "an operation is still running"))
            return
        if self._lives and self.transport.currentData() != "can":
            self.status.setText("Disconnect the live session first (%s)" % self._one_session_note())
            return
        if self.record_chk.isChecked():
            self._open_trace()
        w = _Worker(job, self)
        w.log.connect(self._log)
        w.progress.connect(self._on_progress)
        w.done.connect(lambda res: self._finished(on_done, res))
        w.failed.connect(self._failed)
        self._worker = w
        self._busy_what = what
        self._busy(True, what)
        w.start()

    def _release_worker(self):
        """The finished job's thread: waited for (its run() is returning when
        its last signal arrives) and released, so the next job can start at
        once and nothing is left to Qt to abort on exit."""
        w, self._worker = self._worker, None
        self._busy_what = ""
        if w is not None:
            w.wait(2000)
            w.deleteLater()

    def _finished(self, on_done, res):
        self._release_worker()
        self._busy(False, "")
        self._close_trace()
        try:
            on_done(res)
        except Exception as ex:
            self._log("Error: %s" % ex)

    def _failed(self, msg):
        self._release_worker()
        self._busy(False, "")
        self._close_trace()
        self._log("Failed: %s" % msg)
        self.status.setText("Failed: %s" % msg)

    def _on_progress(self, msg, done, total):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_line.setRange(0, total)
            self.progress_line.setValue(done)
        self.status.setText(msg)

    def stop(self):
        if self._worker is not None:
            self._worker.stop()
            self.status.setText("Stopping...")

    def _need_iface(self) -> Optional[L.Interface]:
        it = self._current_iface()
        if it is None:
            QtWidgets.QMessageBox.information(self, self.windowTitle(),
                                              "No interface selected. Plug in a cable or J2534 device and press Refresh.")
        return it

    def scan_all(self):
        it = self._need_iface()
        if it is None:
            return
        transport = self.transport.currentData()
        rate = self.can_rate.currentData() or 500000
        vehicle = self._vehicle or Vehicle(id="", name="(unsaved)", generation=self.generation)
        vehicle.generation = self.generation
        if self._lives:
            self.status.setText("Disconnect the live sessions first; a scan opens its own channel to every module")
            return
        rows = self._all_rows()
        res = self._res
        bus = self._ensure_bus() if transport == "can" else None
        previous = self._garage.latest_scan(self._vehicle.id) if self._vehicle is not None else self._scan

        def job(ctx):
            return SC.scan_vehicle(it, transport, vehicle, res, modules=rows, log=ctx.log,
                                   progress=ctx.progress, is_stop=ctx.is_stop, can_rate=rate, bus=bus)

        def done(result: ScanResult):
            self._scan = result
            self._modules = list(result.modules)
            self._rebuild_rail()
            if self._vehicle is not None:
                p = self._garage.save_scan(result)
                self._log("Scan saved: %s" % p)
                self._refresh_scans(select_latest=True)
                self._refresh_garage()
            else:
                self._render_report(scan=result)
            self._fill_gateway_table()
            self.stack.setCurrentWidget(self.report_page)          # the scan report
            msg = "Scan done: %d of %d modules answered, %d faults, %.0f s" % (
                len(result.reached), len(result.modules), result.fault_total, result.duration_s)
            if result.basis == "gateway":
                msg += " (the gateway's installed modules)"
            if previous is not None:
                d = diff_scans(previous, result)
                msg += " · since %s: +%d new, -%d cleared, %d changed" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(previous.when)), d.new_faults, d.cleared_faults,
                    len(d.changed))
            self.status.setText(msg)

        self._start(job, done, "Scanning...")

    # ------------------------------------------------------- live sessions
    @property
    def _live(self) -> Optional[LiveSession]:
        """The live session on the selected module, if any."""
        return self._lives.get(self._current_addr())

    def _ensure_bus(self) -> Optional[L.Bus]:
        """The shared CAN connection for this interface, opened on first use;
        None when the interface cannot be a bus (a paired head unit) -- the
        sessions then open their own link, one at a time."""
        it = self._current_iface()
        if it is None or self.transport.currentData() != "can":
            return None
        if self._bus is not None and self._bus.label == it.label:
            return self._bus
        self._close_bus()
        try:
            self._bus = L.open_bus(it, self.can_rate.currentData() or 500000, self._log)
        except Exception as ex:
            self._log("No shared CAN bus on %s (%s): one TP 2.0 channel at a time" % (it.label, ex))
            self._bus = None
        return self._bus

    def _close_bus(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def _update_connect_button(self):
        live = self._live
        self.connect_btn.setText("Disconnect" if live is not None else "Connect")
        self.connect_btn.setIcon(_icon("circle-x" if live is not None else "radio"))
        self.disc_all_btn.setVisible(len(self._lives) > 1)
        self._update_session_ui()

    def _update_session_ui(self):
        """The state line in the module header and the function tabs'
        action buttons: connect once, then every function works on that
        session -- so the buttons are live exactly when the session is."""
        live = self._live
        on = live is not None and live.sess is not None
        for b in self._session_btns:
            b.setEnabled(on)
        long_coding, uds, kwp = self._coding_flags
        self.security_btn.setEnabled(on and kwp)
        self.coding_write_btn.setEnabled(on and not long_coding)
        self.long_coding_write_btn.setEnabled(on and uds)
        self._sync_procedures_page()
        for h in self._connect_hints:
            h.setVisible(not on)
        if live is None:
            txt = "Not connected"
        elif not on:
            txt = "Connecting..."
        else:
            bits = ["Session open"]
            if live.since:
                n = int(time.time() - live.since)
                bits[0] += " %02d:%02d:%02d" % (n // 3600, (n // 60) % 60, n % 60)
            bits.append(_protocol_text(self._module_info(live.addr)) or live.protocol.upper())
            if live.logged_in:
                bits.append("logged in")
            if len(self._lives) > 1:
                bits.append("%d sessions open" % len(self._lives))
            txt = " \u00b7 ".join(b for b in bits if b)
        self.session_state.setText(txt)

    def _tick(self):
        self._update_session_ui()

    def toggle_connect(self):
        if self._live is not None:
            self.disconnect_live()
            return
        row = self._current_row()
        if row is None:
            return
        self.connect_module(row)

    def connect_module(self, row: dict) -> Optional[LiveSession]:
        """Open a live session on ``row``'s module. Over CAN it joins the
        sessions already open (its own TP 2.0 channel on the shared bus); on
        K-line there is one session at a time."""
        it = self._need_iface()
        if it is None:
            return None
        addr = row["addr"]
        if addr in self._lives:
            return self._lives[addr]
        transport = self.transport.currentData()
        if transport in ("can", "uds") and not self._addr_ok(row, transport):
            self._no_addr_message(addr, transport)
            return None
        if transport != "can" and self._lives:
            # K-line is one wire; a UDS link holds the J2534 / OpenPort device
            # itself until UDS links share the bus: one session at a time
            other = next(iter(self._lives))
            self.status.setText("Connected to %s; disconnect it first (%s)" % (other, self._one_session_note()))
            return None
        if self.record_chk.isChecked():
            self._open_trace()
        bus = self._ensure_bus() if transport == "can" else None
        live = LiveSession(it, transport, row, self._res, self.generation,
                           self.can_rate.currentData() or 500000, self, bus=bus)
        live.log.connect(self._log)
        live.connected.connect(lambda info, lv=live: self._live_connected(lv, info))
        live.disconnected.connect(lambda reason, lv=live: self._live_disconnected(lv, reason))
        live.result.connect(lambda op, payload, lv=live: self._live_result(lv, op, payload))
        live.failed.connect(lambda op, msg, lv=live: self._live_failed(lv, op, msg))
        self._lives[addr] = live
        self.scan_btn.setEnabled(False)
        self._set_link_controls(False)
        if addr == self._current_addr():
            self.connect_btn.setEnabled(False)
        self.status.setText("Connecting to %s %s..." % (addr, row.get("name", "")))
        live.start()
        return live

    def disconnect_live(self, addr: Optional[str] = None):
        live = self._lives.get(addr) if addr else self._live
        if live is None:
            return
        if live.addr == self._current_addr():
            self._basic_running = False
            self._live_stop()
        if live.prompter is not None:
            self.proc_page.abort()                  # a question waiting in the page must not hold the stop
        live.stop()
        self.status.setText("Disconnecting %s..." % live.addr)

    def disconnect_all(self):
        self._multi_running = False
        for addr in list(self._lives):
            self.disconnect_live(addr)

    def _live_connected(self, live: LiveSession, info: ModuleInfo):
        live.since = time.time()
        self._store_module(info)
        baud = as_int((info.extra or {}).get("baud"), None)
        if info.transport == "kline" and baud:
            # the rate the module answered at goes on its row in the local
            # pack: the next connection tries it first (no reload needed)
            try:
                from . import learned as LN
                _path, changed = LN.record_kline_baud(info.addr, int(baud), self.generation, info.name)
                if changed:
                    self._log("K-line baud %d recorded in your local pack for %s" % (baud, info.addr))
            except Exception as ex:
                self._log("Could not record the K-line baud: %s" % ex)
        if live.addr == self._current_addr():
            self.connect_btn.setEnabled(True)
        self._update_connect_button()
        self._tick()
        self._refresh_live_markers()
        self._live_update()                    # the ticked groups start reading now the session is open
        self.status.setText("%s %s: %s" % (info.addr, info.name, info.identity_text or "connected"))
        live.post("faults", lambda s: (s.read_faults(), s.info))
        # what the user asked for while the session was opening (a faults
        # read is already on its way)
        for op, fn in self._pending_ops.pop(live.addr, []):
            if op != "faults":
                live.post(op, fn)
        if self._multi_running:
            self._multi_schedule(live)

    def _live_disconnected(self, live: LiveSession, reason: str):
        if self._lives.get(live.addr) is live:
            del self._lives[live.addr]
        dropped = self._pending_ops.pop(live.addr, [])
        if dropped:
            self._log("%s: %d queued operation(s) dropped, the session did not open" % (live.addr, len(dropped)))
        live.logged_in = False
        self._palette_readings = {k: v for k, v in self._palette_readings.items() if k[0] != live.addr}
        if live.addr == self._current_addr():
            self._basic_running = False
            self.basic_btn.setText("Start basic setting")
            self.basic_state.setText("")
        self.connect_btn.setEnabled(True)
        if live.prompter is not None:
            # a procedure was waiting on this session: release its dialog
            try:
                live.prompter.stop()
            except Exception:
                pass
            live.prompter = None
            self.proc_page.abort()
        if not self._lives:
            idle = self._worker is None or not self._worker.isRunning()
            self.scan_btn.setEnabled(idle)
            self._set_link_controls(idle)
            self._close_trace()
            if self._multi_running:
                self._multi_running = False
                self.multi_btn.setText("Start")
                self.multi_state.setText("stopped: no session left")
        self._update_connect_button()
        self._refresh_live_markers()
        self._live_update()
        self.status.setText("Disconnected from %s" % live.addr + (": %s" % reason if reason else ""))
        if reason:
            self._log("Session on %s ended: %s" % (live.addr, reason))
        live.wait(3000)
        live.deleteLater()

    def _refresh_live_markers(self):
        self._rebuild_rail()
        self._live_others()

    def _live_post(self, op: str, fn) -> bool:
        live = self._live
        if live is None or live.sess is None:
            return False
        live.post(op, fn)
        return True

    def _live_result(self, live: LiveSession, op: str, payload):
        if op == "multi":
            self._on_multi(live, payload)
            return
        if op == "procedure":
            self._on_procedure(payload, live)
            return
        if live.addr != self._current_addr() and op not in ("identify", "faults", "clear", "adapt_save", "coding",
                                                            "gateway_list", "procedure", "long_coding"):
            return                              # a display result for a module that is not on screen
        handler = getattr(self, "_on_" + op, None)
        if handler is not None:
            try:
                handler(payload)
            except Exception as ex:
                self._log("Error handling %s: %s" % (op, ex))

    def _live_failed(self, live: LiveSession, op: str, msg: str):
        self.status.setText("%s %s: %s" % (live.addr, op.replace("_", " "), msg))
        self._log("%s %s failed: %s" % (live.addr, op, msg))
        if op == "multi":
            self._multi_failed(live, msg)
            return
        if op == "procedure":
            live.prompter = None
            self.proc_page.abort()
            self._sync_procedures_page()
        if live.addr != self._current_addr():
            return
        if op == "basic":
            self._basic_running = False
            self.basic_btn.setText("Start basic setting")
            self.basic_state.setText(msg)
        if op in ("group_live", "uds_live"):
            self._live_stop()

    # ---------------------------------------- operations on the selected module
    def _same_module_live(self) -> bool:
        live = self._live
        return live is not None and live.sess is not None

    def _can_open_here(self) -> bool:
        """May a session on the selected module be opened now? Not while
        another module holds the one K-line / UDS link."""
        if self._lives and self.transport.currentData() != "can":
            self.status.setText("Connected to %s; disconnect first, then connect to %s (%s)"
                                % (next(iter(self._lives)), self._current_addr(), self._one_session_note()))
            return False
        return True

    def _require_live(self) -> bool:
        """The functions that ask the user something first (login, security
        access, a save, a coding, a procedure) need the session open before
        they ask: when it is not, it is opened now -- connect once, every
        function on it -- and the user presses again once it is."""
        if self._same_module_live():
            return True
        if not self._can_open_here():
            return False
        row = self._current_row()
        if row is not None and row["addr"] not in self._lives and self.connect_module(row) is not None:
            self.status.setText("Connecting to %s; use the function once the session is open" % row["addr"])
        elif row is not None and row["addr"] in self._lives:
            self.status.setText("Still connecting to %s; use the function once the session is open" % row["addr"])
        else:
            self.status.setText("Connect to the module first")
        return False

    def _ensure_live(self, op: str, fn) -> bool:
        """Run ``fn`` on the selected module's session: posted when it is
        open; otherwise the session is opened (connect once: it stays until
        Disconnect, a link error or the window closes) and ``fn`` runs as soon
        as it is. False when nothing could be started."""
        if self._same_module_live():
            return self._live_post(op, fn)
        row = self._current_row()
        if row is None:
            return False
        addr = row["addr"]
        if addr not in self._lives:
            if not self._can_open_here() or self.connect_module(row) is None:
                return False
        self._pending_ops.setdefault(addr, []).append((op, fn))
        return True

    def module_op(self, what: str):
        """Identify / read faults, on the module's live session -- opened
        first when there is none, and kept open: the function tabs then work
        on it without reconnecting."""
        if what == "identify":
            self._ensure_live("identify", lambda s: s.info)
        elif what == "faults":
            self._ensure_live("faults", lambda s: (s.read_faults(), s.info))

    def _on_identify(self, info: ModuleInfo):
        old = self._module_info(info.addr)
        if old is not None:
            info.faults = old.faults
        self._store_module(info)

    def _on_faults(self, payload):
        faults, info = payload
        self._store_module(info)
        self.status.setText("%s: %d fault%s" % (info.name, len(faults), "" if len(faults) == 1 else "s"))

    # ------------------------------------------------------------ live data
    # The Live data tab is the LiveDataView; this window runs its reads: the
    # ticked groups in rotation on the module's session, one queued read per
    # group, the next posted when the result arrives.
    def _catalogue_namer(self, m: Optional[ModuleInfo]):
        """The factory catalogue's namer for the module's part number, or None."""
        if m is None or not m.platform:
            return None
        key = (m.platform, m.identity_text)
        if key not in self._namers:
            namer = None
            try:
                from ..datalog import msbcatalogue as _cat
                namer = _cat.namer_for(m.platform.upper(), m.identity_text)
                if namer is not None and not namer.available:
                    namer = None
            except Exception:
                namer = None
            self._namers[key] = namer
        return self._namers[key]

    def _group_titles(self, ident: D.Identity) -> Dict[int, str]:
        out: Dict[int, str] = {}
        try:
            for h in self._res.hits("measuring", ident):
                for g, info in (h.doc.body.get("groups") or {}).items():
                    gn = as_int(g, None)
                    title = (info.get("title") or info.get("name")) if isinstance(info, dict) else None
                    if gn is not None and title:
                        out[gn] = str(title)
        except Exception:
            pass
        return out

    def _live_entries_for(self, addr: str):
        """``([GroupEntry], source note)`` for the module's Live data list."""
        m = self._module_info(addr)
        ident = self._ident_for(addr)
        lv = self._lives.get(addr)
        uds = (lv.protocol == "uds") if (lv is not None and lv.sess is not None) else bool(m and m.protocol == "uds")
        if uds:
            names = []
            for r in self._res.view("dids", ident).entries.values():
                e = r.entry
                if e.get("live") and as_int(e.get("did"), None) is not None:
                    names.append(e.get("name") or "DID 0x%04X" % as_int(e.get("did")))
            if not names:
                return [], "No names for this module"
            return [GroupEntry(UDS_GROUP, "Live values", names)], "Named from the definition packs"
        names = self._res.measuring_names(ident)
        titles = self._group_titles(ident)
        namer = self._catalogue_namer(m)
        populated = list(namer.populated_groups()) if namer is not None else []
        entries = []
        for g in sorted(set(names) | set(populated)):
            cells = list(names.get(g, []))
            if not any(cells) and namer is not None and g in populated:
                cells = []
                for f in range(namer.slots(g)):
                    label = namer.resolve(g, f)[0]
                    cells.append(None if label.startswith("Group ") else label)
            entries.append(GroupEntry(g, titles.get(g) or "Group %03d" % g, cells))
        if names and any(any(c) for c in names.values()):
            note = "Named from the definition packs"
        elif populated:
            note = "Named from the factory catalogue of %s" % ((m.part or m.identity_text) if m else "this module")
        else:
            note = "No names for this module"
        if not entries and m is not None and m.reached:
            entries = [GroupEntry(g, "Group %03d" % g, []) for g in range(1, 11)]
        return entries, note

    def _fill_live_groups(self, force: bool = False):
        """Show the selected module's groups in the Live data tab (names from
        the definitions or the catalogue); another module forgets the readings."""
        addr = self._current_addr()
        if not addr or not hasattr(self, "live_view"):
            return
        entries, note = self._live_entries_for(addr)
        sig = (addr, note, [(e.group, e.title, tuple(e.cells)) for e in entries])
        if sig == self._live_sig and not force:
            return
        if addr != self._live_addr:
            self._live_stop()
            self.live_view.clear()
        self._live_addr, self._live_sig = addr, sig
        if hasattr(self.live_view, "set_module_addr"):
            self.live_view.set_module_addr(addr)
        have = {e.group for e in entries}
        entries = entries + [e for e in self._live_manual.get(addr, []) if e.group not in have]
        self._live_entries = entries
        checked = [g for g in self._live_checked.get(addr, []) if g in {e.group for e in entries}]
        self._live_filling = True
        try:
            self.live_view.set_groups(entries, checked)
            self.live_view.set_source_note(note)
        finally:
            self._live_filling = False
        self._live_groups = list(self.live_view.checked_groups())
        self._live_others()

    def _live_selection_changed(self, groups):
        if self._live_filling:
            return
        addr = self._current_addr()
        self._live_groups = [int(g) for g in groups]
        if addr:
            self._live_checked[addr] = list(self._live_groups)
        if self._palette_readings:
            self._palette_readings = {k: v for k, v in self._palette_readings.items()
                                      if k[0] != addr or k[1] in self._live_groups}
        if self._live_groups and not self._same_module_live() and not self._palette_no_connect:
            row = self._current_row()
            if row is not None and row["addr"] not in self._lives and self._can_open_here():
                self.connect_module(row)           # connect once; the reads start when it is open
        self._live_update()

    def _live_manual_group(self, group: int):
        """A group number typed in (or opened from the report): add it to the
        list when the module's names do not have it, and tick it."""
        addr = self._current_addr()
        if not addr:
            return
        lv = self._live
        if lv is not None and lv.protocol == "uds":
            self.status.setText("UDS modules have data identifiers, not numbered measuring groups")
            return
        if group not in {e.group for e in self._live_entries}:
            e = GroupEntry(group, "Group %03d" % group, [], note="added by hand")
            self._live_entries.append(e)
            self._live_manual.setdefault(addr, []).append(e)
        checked = list(self._live_groups)
        if group not in checked:
            checked.append(group)
        self._live_filling = True
        try:
            self.live_view.set_groups(self._live_entries, checked)
        finally:
            self._live_filling = False
        self._live_selection_changed(checked)

    def _live_wanted(self) -> bool:
        """Reads run while the selected module's Live data tab is on screen,
        the module has a session and the window is not minimized."""
        return bool(self._live_groups and not self._closing and self.isVisible() and not self.isMinimized()
                    and self.stack.currentIndex() == 0 and self.module_stack.currentIndex() == 1
                    and self.tabs.currentWidget() is self.live_view and self._same_module_live())

    def _live_update(self):
        if not hasattr(self, "live_view"):
            return
        want = self._live_wanted()
        if want and not self._live_running:
            self._live_start()
        elif not want and self._live_running:
            self._live_stop()
        elif not want:
            self.live_view.set_running(False)

    def _live_start(self):
        self._live_gen += 1
        self._live_running = True
        self._live_idx = 0
        self._live_rounds = []
        self._live_t0 = time.monotonic()
        self.live_view.set_running(True)
        self._live_next()

    def _live_stop(self):
        was = self._live_running
        self._live_running = False
        self._live_gen += 1                       # results still on their way are dropped
        if hasattr(self, "live_view"):
            self.live_view.set_running(False)
            if was:
                self.live_view.set_rate(0.0)

    def _live_next(self):
        if not self._live_running:
            return
        live = self._live
        if live is None or live.sess is None or not self._live_groups:
            self._live_stop()
            return
        gen = self._live_gen
        if live.protocol == "uds":
            live.post("uds_live", lambda s: (gen, UDS_GROUP, s.uds_read_dids([d["did"] for d in s.uds_live_dids()]), None))
            return
        g = self._live_groups[self._live_idx % len(self._live_groups)]
        live.post("group_live", lambda s: (gen, g, s.read_group_full(g), s.group_names(g)))

    def _on_group_live(self, payload):
        gen, g, reading, names = payload
        if gen != self._live_gen or not self._live_running:
            return
        self._live_deliver(g, reading, names)
        self._live_idx += 1
        if self._live_groups and self._live_idx % len(self._live_groups) == 0:
            self._live_round()
        self._live_next()

    _on_uds_live = _on_group_live

    def _live_round(self):
        """A round (every ticked group read once) is done: the rate over the last 2 s."""
        now = time.monotonic()
        self._live_rounds = [t for t in self._live_rounds if now - t <= 2.0] + [now]
        r = self._live_rounds
        rate = (len(r) - 1) / (r[-1] - r[0]) if len(r) >= 2 and r[-1] > r[0] else 1.0 / max(0.25, now - self._live_t0)
        self.live_view.set_rate(rate)

    def _live_deliver(self, g: int, reading, names):
        """One group's result as the tiles' ``Reading``s."""
        if isinstance(reading, list):                          # UDS: DidReadings
            cells = []
            for r in reading:
                v = r.value if isinstance(r.value, (int, float)) and not isinstance(r.value, bool) else None
                cells.append(Reading(r.name or "DID 0x%04X" % r.did, r.text, "", v, None, ""))
            self.live_view.set_readings(g, cells)
            self._palette_live_value(self._current_addr(), g, cells)
            return
        if reading is None or not reading.available:
            self._live_not_served(g)
            return
        out: List[Reading] = []
        if reading.cells is not None:
            for i, cell in enumerate(reading.cells):
                value, unit, text = cell.decoded
                name = names[i] if names and i < len(names) and names[i] else "Cell %d" % (i + 1)
                out.append(Reading(name, _strip_unit(text, unit), unit, value, _unit_frac(unit, value), ""))
        elif reading.raw is not None:
            out.append(Reading("raw", " ".join("%02X" % b for b in reading.raw), "", None, None, ""))
        elif reading.text is not None:
            out.append(Reading("text block", reading.text.decode("latin-1", "replace"), "", None, None, ""))
        self.live_view.set_readings(g, out)
        self._palette_live_value(self._current_addr(), g, out)

    def _live_not_served(self, g: int):
        self._log("Group %03d: not served by this module" % g)
        self.status.setText("Group %03d: not available" % g)
        for e in self._live_entries:
            if e.group == g:
                e.served, e.note = False, "Not served by this module"
        self._live_groups = [x for x in self._live_groups if x != g]
        addr = self._current_addr()
        if addr:
            self._live_checked[addr] = list(self._live_groups)
        self._live_filling = True
        try:
            self.live_view.set_groups(self._live_entries, self._live_groups)
        finally:
            self._live_filling = False
        if not self._live_groups:
            self._live_stop()

    def _live_others(self):
        """The strip under the tiles: the other modules that are live too."""
        if not hasattr(self, "live_view"):
            return
        rows = []
        for addr in self._lives:
            if addr == self._current_addr():
                continue
            m = self._module_info(addr)
            row = self._row_for(addr) or {}
            name = ("%s %s" % (addr, (m.name if m else "") or row.get("name", ""))).strip()
            vals = []
            for (a, _g), cells in self._multi_values.items():
                if a == addr and isinstance(cells, list):
                    vals += [(n or "cell", t) for n, t, _r in cells[:3]]
            rows.append((addr, name, vals[:3]))
        self.live_view.set_others(rows)

    # ---- faults
    def clear_faults(self):
        row = self._current_row()
        if row is None:
            return
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            "Clear the stored faults of %s %s?\n\nThe codes present now are kept in the vehicle's journal."
            % (row["addr"], row.get("name", "")))
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def op(s: ModuleSession):
            before = [f.to_dict() for f in s.read_faults()]
            s.clear_faults()
            after = s.read_faults()
            return {"info": s.info, "before": before, "after": after}

        self._ensure_live("clear", op)      # on the session, opened first when there is none

    def _on_clear(self, out):
        info = out["info"]
        self._store_module(info)
        self._journal_record(addr=info.addr, module=info.name, function="clear_faults",
                             before=[b.get("vag") or b.get("code") for b in out["before"]],
                             after=[f.vag or f.code for f in out["after"]],
                             verified=not out["after"], note="%d code(s) before" % len(out["before"]))
        self.status.setText("%s: %d fault(s) cleared%s" % (
            info.name, len(out["before"]), "" if not out["after"] else ", %d still present" % len(out["after"])))

    def copy_faults(self):
        m = self._module_info(self._current_addr())
        if not m:
            return
        lines = ["%s %s - %s" % (m.addr, m.name, m.identity_text)]
        lines += ["  %s  %s  [%s]" % (f.display_code, f.text, f.status_text) for f in m.faults] or ["  no faults"]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("Faults copied")

    # ---- login
    def login(self):
        if not self._require_live():
            return
        code, ok = QtWidgets.QInputDialog.getInt(self, "Login", "Login code for %s:" % self._current_addr(),
                                                 0, 0, 65535)
        if not ok:
            return
        wsc = self.wsc_edit.value()
        self._live_post("login", lambda s: s.login(code, wsc))

    def _on_login(self, ok: bool):
        live = self._live
        if ok and live is not None:
            live.logged_in = True           # the state line says so until the session ends
        self._update_session_ui()
        self.status.setText("Login accepted" if ok else "Login refused")

    # ---- adaptation
    def adapt_read(self):
        ch = self.adapt_ch.value()
        self._ensure_live("adapt_read", lambda s: (ch, s.adaptation_read(ch), s.group_names(0)))

    def _show_adaptation(self, ch, a):
        self.adapt_current.setText("Channel %03d: %d  (0x%04X)" % (ch, a.value, a.value))
        self.adapt_val.setValue(a.value)
        _cells_rows(self.adapt_cells, ch, a.cells)

    def _on_adapt_read(self, payload):
        ch, a, _ = payload
        self._show_adaptation(ch, a)
        self.adapt_current.setStyleSheet(_pending_style(False))
        self.status.setText("Adaptation channel %03d read: %d" % (ch, a.value))

    def adapt_test(self):
        ch, val = self.adapt_ch.value(), self.adapt_val.value()
        self._ensure_live("adapt_test", lambda s: (ch, s.adaptation_test(ch, val)))

    def _on_adapt_test(self, payload):
        ch, a = payload
        self._show_adaptation(ch, a)
        self.adapt_current.setText(self.adapt_current.text() + "  test value, not saved")
        self.adapt_current.setStyleSheet(_pending_style(True))        # applied on the module, not written
        self.status.setText("Channel %03d test value %d applied (not saved)" % (ch, a.value))

    def adapt_save(self):
        if not self._require_live():
            return
        ch, val, wsc = self.adapt_ch.value(), self.adapt_val.value(), self.wsc_edit.value()
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            "Save adaptation channel %03d = %d on %s (workshop code %05d)?\n\n"
            "The value before the write is read first and kept in the journal." % (ch, val, self._current_addr(), wsc))
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        entry_id = self._adapt_entry_id(ch)

        def op(s: ModuleSession):
            before = s.adaptation_read(ch).value
            s.adaptation_save(ch, val, wsc)
            after = s.adaptation_read(ch).value
            return (ch, before, after, val, entry_id, s.info)

        self._live_post("adapt_save", op)

    def _adapt_entry_id(self, ch: int) -> str:
        ident = self._ident_for(self._current_addr())
        for eid, r in self._res.view("adaptation", ident).entries.items():
            if as_int(r.entry.get("channel"), -1) == ch:
                return eid
        return "adaptation.ch%02d" % ch

    def _on_adapt_save(self, payload):
        ch, before, after, wanted, entry_id, info = payload
        self._journal_record(addr=info.addr, module=info.name, function="adaptation", entry_id=entry_id,
                             before=before, after=after, verified=(after == wanted))
        self.adapt_current.setText("Channel %03d: %d  (was %d)" % (ch, after, before))
        self.adapt_current.setStyleSheet(_pending_style(False))
        self.status.setText("Channel %03d saved: %d -> %d%s" % (
            ch, before, after, "" if after == wanted else "  (module reports %d, not %d)" % (after, wanted)))

    # ---- coding
    def _coding_edited(self, value: int):
        """The short coding field reads as pending while it differs from
        what the module reported."""
        m = self._module_info(self._current_addr())
        pending = bool(m is not None and m.coding is not None and not m.coding_bytes and value != m.coding)
        self.coding_new.setStyleSheet(_pending_style(pending))

    def coding_write(self):
        if not self._require_live():
            return
        m = self._module_info(self._current_addr())
        fmt = "%05d" if (m and m.protocol == "kw1281") else "%07d"
        coding, wsc = self.coding_new.value(), self.wsc_edit.value()
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            ("Write coding " + fmt + " (workshop code %05d) to %s?\n\nThe current coding is kept in the journal.")
            % (coding, wsc, self._current_addr()))
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def op(s: ModuleSession):
            before = (s.info.coding, s.info.wsc)
            ok, cod, w = s.set_coding(coding, wsc)
            return (ok, before, (cod, w), coding, s.info)

        self._live_post("coding", op)

    def _on_coding(self, payload):
        ok, before, after, wanted, info = payload
        self._store_module(info)
        self._journal_record(addr=info.addr, module=info.name, function="coding", entry_id="coding.short",
                             before={"coding": before[0], "wsc": before[1]},
                             after={"coding": after[0], "wsc": after[1]},
                             verified=bool(ok and after[0] == wanted))
        fmt = "%05d" if info.protocol == "kw1281" else "%07d"
        self.status.setText("Coding %s: module now reports %s" % (
            "written" if ok else "REFUSED", fmt % after[0] if after[0] is not None else "-"))

    # ---- long coding (UDS)
    def _long_coding_bytes(self):
        try:
            return bytes.fromhex(self.long_coding_edit.text().replace(" ", ""))
        except ValueError:
            return None

    def _long_coding_edited(self, _text):
        data = self._long_coding_bytes()
        if data is not None:
            self._fill_long_coding_bits(self._ident_for(self._current_addr()), data)

    def _long_coding_defs(self, ident):
        """The ``coding`` definition's byte/bit rows for a long coding, as a
        flat list of ``(byte_index, bit_or_bits, name, kind, options)``."""
        cdoc = self._res.coding_doc(ident)
        rows = []
        for b in cdoc.get("bytes") or []:
            idx = as_int(b.get("index"), 0)
            for bit in b.get("bits") or []:
                bits = bit.get("bits") or ([bit.get("bit")] if bit.get("bit") is not None else [])
                rows.append((idx, [as_int(x, 0) for x in bits], bit.get("name", ""),
                             bit.get("type", "bool"), bit.get("options") or {}))
        return rows

    def _fill_long_coding_bits(self, ident, data: bytes):
        t = self.long_coding_bits
        t.setRowCount(0)
        for idx, bits, name, kind, options in self._long_coding_defs(ident):
            if idx < 1 or idx > len(data):
                continue
            byte = data[idx - 1]
            raw = 0
            for i, b in enumerate(sorted(bits)):
                if byte & (1 << b):
                    raw |= 1 << i
            if kind == "bool":
                shown = "on" if raw else "off"
            elif kind == "enum":
                shown = str(options.get(raw, options.get(str(raw), raw)))
            else:
                shown = str(raw)
            opts = ", ".join("%s=%s" % kv for kv in options.items()) if options else ("on/off" if kind == "bool" else "")
            rr = _row(t, "byte %d bit %s" % (idx, ",".join(str(b) for b in bits)), name, "%s   (%s)" % (shown, opts) if opts else shown, "")
            t.item(rr, 0).setData(QtCore.Qt.ItemDataRole.UserRole, (idx, bits, kind, options))
            base = self._long_coding_base
            if base is not None and idx <= len(base):
                was = 0
                for i, b in enumerate(sorted(bits)):
                    if base[idx - 1] & (1 << b):
                        was |= 1 << i
                if was != raw:
                    mark_pending(t.item(rr, 2), t.font())          # changed here, not written yet
        t.resizeColumnToContents(0)

    def _long_coding_bit_toggled(self, item):
        data = self._long_coding_bytes()
        if data is None:
            return
        meta = self.long_coding_bits.item(item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if not meta:
            return
        idx, bits, kind, options = meta
        if idx < 1 or idx > len(data):
            return
        buf = bytearray(data)
        byte = buf[idx - 1]
        cur = 0
        for i, b in enumerate(sorted(bits)):
            if byte & (1 << b):
                cur |= 1 << i
        if kind == "bool":
            nxt = 0 if cur else 1
        elif kind == "enum" and options:
            keys = sorted(int(k) for k in options)
            nxt = keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0]
        else:
            return
        for i, b in enumerate(sorted(bits)):
            if nxt & (1 << i):
                byte |= 1 << b
            else:
                byte &= ~(1 << b)
        buf[idx - 1] = byte
        self.long_coding_edit.setText(_hex_spaced(bytes(buf).hex()))
        self._fill_long_coding_bits(self._ident_for(self._current_addr()), bytes(buf))

    def long_coding_write(self):
        if not self._require_live():
            return
        if self._live.protocol != "uds":
            self.status.setText("Long coding is written over UDS; connect the module with the UDS transport")
            return
        data = self._long_coding_bytes()
        if data is None:
            self.status.setText("The long coding is not valid hex")
            return
        before = self._module_info(self._current_addr())
        before_hex = before.coding_bytes if before else ""
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            "Write long coding %s to %s?\n\nWas: %s\n\nThe current coding is kept in the journal and the "
            "identity is re-read afterwards." % (_hex_spaced(data.hex()), self._current_addr(),
                                                 _hex_spaced(before_hex) or "-"))
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def op(s: ModuleSession):
            ok = s.write_long_coding(data)
            return (ok, before_hex, s.info)

        self._live_post("long_coding", op)

    def _on_long_coding(self, payload):
        ok, before_hex, info = payload
        self._store_module(info)
        self._journal_record(addr=info.addr, module=info.name, function="long_coding", entry_id="coding.long",
                             before=before_hex, after=info.coding_bytes, verified=bool(ok))
        self.status.setText("Long coding %s: module now reports %s" % (
            "written" if ok else "REFUSED", _hex_spaced(info.coding_bytes) or "-"))

    # ---- actuator tests
    def actuator_next(self):
        self._ensure_live("actuator", lambda s: s.actuator_next())

    def _on_actuator(self, step):
        name = "End of sequence" if step.ended else self._actuator_name(step.id)
        n = self.act_table.rowCount() + 1
        _row(self.act_table, n, "0x%04X" % step.id if step.id >= 0 else " ".join("%02X" % b for b in step.raw),
             name, "definitions" if name and not name.startswith("VAG ") and not step.ended else "")
        self.act_table.scrollToBottom()
        self.act_cur.setText(("Sequence ended" if step.ended else "Active: %s" % (name or "0x%04X" % step.id)))
        self.status.setText(self.act_cur.text())

    def actuator_stop(self):
        if self._same_module_live():
            self._live_post("actuator_stop", lambda s: s.actuator_stop())
            self.act_cur.setText("Actuator test stopped")

    # ---- security access (KWP2000)
    def security_access(self):
        if not self._require_live():
            return
        code, ok = QtWidgets.QInputDialog.getInt(self, "Security access",
                                                 "Security access code for %s:" % self._current_addr(), 0, 0, 2 ** 31 - 1)
        if not ok:
            return
        self._live_post("security", lambda s: s.security_access(code))

    def _on_security(self, ok: bool):
        self.status.setText("Security access granted" if ok else "Security access denied")

    # ---- basic settings
    def basic_toggle(self):
        if self._basic_running:
            self._basic_running = False
            self.basic_btn.setText("Start basic setting")
            self.basic_state.setText("stopped")
            self._live_post("basic_stop", lambda s: s.basic_setting_stop())
            return
        g = self.basic_group.value()
        if not self._ensure_live("basic", lambda s: (g, s.basic_setting_read(g), s.group_names(g))):
            return
        self._basic_running = True
        self.basic_btn.setText("Stop")
        self.basic_state.setText("running group %03d" % g)

    def _on_basic(self, payload):
        g, reading, names = payload
        if reading is None or not reading.available:
            self._basic_running = False
            self.basic_btn.setText("Start basic setting")
            self.basic_state.setText("group %03d not available in basic-setting mode" % g)
            return
        if reading.cells is not None:
            _cells_rows(self.basic_cells, g, reading.cells, names)
        elif reading.raw is not None:
            self.basic_cells.setRowCount(0)
            _row(self.basic_cells, "%03d" % g, "raw", " ".join("%02X" % b for b in reading.raw), "")
        if self._basic_running and self._same_module_live():
            self._live_post("basic", lambda s: (g, s.basic_setting_read(g), s.group_names(g)))

    # --------------------------------------------------------- gateway list
    def _gateway_list(self):
        """The installation list on hand: the loaded scan's, else one a
        gateway session read (kept in that module's info)."""
        from .gateway import InstallationList
        src = self._scan.gateway if (self._scan is not None and self._scan.gateway) else None
        if src is None:
            m = next((m for m in self._modules if m.extra.get("installation_list")), None)
            src = m.extra["installation_list"] if m is not None else None
        return InstallationList.from_dict(src) if src else None

    def _unknown_gateway_entries(self, lst):
        """Installed entries whose address the address table lacks, or has
        without a TP 2.0 dest."""
        rows = {r["addr"]: r for r in self._rows()}
        out = []
        for e in lst.installed:
            r = rows.get(e.addr)
            if r is None or as_int((r.get("tp20") or {}).get("dest"), None) is None:
                out.append(e)
        return out

    def _fill_gateway_table(self):
        t = self.gw_table
        t.setRowCount(0)
        lst = self._gateway_list()
        if lst is None:
            self.gw_summary.setText("No installation list yet: scan over CAN, or press Read list from gateway.")
            self.gw_save_btn.setEnabled(False)
            return
        scan_mods = {m.addr: m for m in self._modules}
        for e in sorted(lst.entries, key=lambda x: (not x.installed, x.addr)):
            m = scan_mods.get(e.addr)
            last = ""
            if e.installed and m is not None and m.reached is not None:
                last = ("%s%s" % (m.part or m.identity_text or "answered",
                                  (", %d fault%s" % (m.fault_count, "" if m.fault_count == 1 else "s")) if m.fault_count else "")
                        if m.reached else "no answer")
            r = _row(t, e.addr, e.name or "", "0x%02X" % e.dest, e.bus_name, e.status_text, last)
            if not e.installed:
                for c in range(6):
                    t.item(r, c).setForeground(QtGui.QBrush(OFF_FG))
        unknown = self._unknown_gateway_entries(lst)
        self.gw_summary.setText("Gateway %s: %d modules installed of %d listed%s" % (
            lst.gateway_addr, len(lst.installed), len(lst.entries),
            ("; %d not in the address table" % len(unknown)) if unknown else ""))
        self.gw_save_btn.setEnabled(bool(unknown))
        t.resizeColumnToContents(0)

    def read_gateway_list(self):
        it = self._need_iface()
        if it is None:
            return
        if self.transport.currentData() != "can":
            self.status.setText("The installation list is read from the gateway over CAN")
            return
        gw = SC.gateway_row(self._rows())
        if gw is None:
            self.status.setText("No gateway with an installation list in the address table for this generation")
            return
        if gw["addr"] in self._lives:
            self._lives[gw["addr"]].post("gateway_list", lambda s: (s.installation_list(), s.info))
            return
        rate, res, gen = self.can_rate.currentData() or 500000, self._res, self.generation
        bus = self._ensure_bus()

        def job(ctx):
            link = L.open_link(it, "can", gw, ctx.log, can_rate=rate, bus=bus)
            sess = ModuleSession(link, gw, res, ctx.log, generation=gen)
            try:
                sess.connect(is_stop=ctx.is_stop)
                return (sess.installation_list(), sess.info)
            finally:
                sess.close()

        self._start(job, self._on_gateway_list, "Reading the gateway's installation list...")

    def _on_gateway_list(self, payload):
        lst, info = payload
        self._store_module(info)
        self._fill_gateway_table()
        self._show_tools(self._tool_pages["Gateway list"])
        self.status.setText("Gateway %s lists %d modules installed of %d" % (info.addr, len(lst.installed), len(lst.entries)))

    def save_gateway_addresses(self):
        lst = self._gateway_list()
        if lst is None:
            return
        unknown = self._unknown_gateway_entries(lst)
        if not unknown:
            return
        from . import learned as LN
        entries = [{"addr": e.addr, "name": e.name or "Module %s (gateway list)" % e.addr, "dest": e.dest,
                    "bus": e.bus_name if e.bus in (0x00, 0x01, 0x04) else ""} for e in unknown]
        try:
            path, n = LN.record_addresses(entries, self.generation,
                                          "Recorded by OVT Diag from the gateway's installation list (%s, %s)"
                                          % (self._vehicle.label if self._vehicle else "unsaved vehicle",
                                             time.strftime("%Y-%m-%d")))
        except Exception as ex:
            self._log("Could not write the local pack: %s" % ex)
            self.status.setText("Could not write the local pack: %s" % ex)
            return
        self._log("%d address row%s written to %s" % (n, "" if n == 1 else "s", path))
        self.reload_definitions(quiet=True)
        self.status.setText("%d module address%s saved to your local pack (%s)" % (n, "" if n == 1 else "es", path))

    # ------------------------------------------------------- definition packs
    def reload_definitions(self, quiet: bool = False):
        """Re-read every pack (bundled, module, user) and refresh what shows
        them: the module tree, the vehicle live-data module list and the
        gateway table. Connected sessions keep their resolver until they
        reconnect."""
        self._errors = []
        self._packs = D.load_all(self._errors)
        self._res = D.Resolver(self._packs)
        for pth, msg in self._errors:
            self._log("Definitions pack skipped: %s (%s)" % (pth, msg))
        self._namers.clear()
        self._rebuild_rail()
        self._fill_multi_modules()
        self._fill_gateway_table()
        self._fill_live_groups(force=True)
        self._palette_cache.clear()
        self._fill_procedures()
        if not quiet:
            msg = "%d definition pack%s loaded" % (len(self._packs), "" if len(self._packs) == 1 else "s")
            self._log(msg + ": " + (", ".join("%s %s" % (p.id, p.version) for p in self._packs) or "none"))
            self.status.setText(msg)

    def install_pack(self):
        """Install a definitions pack (a folder with pack.yaml, a .zip of one,
        or a compiled pack.json.gz) into the user's definitions folder. The
        pack is validated first; one with errors is refused."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Install definitions pack", "",
            "Definition packs (*.zip pack.yaml pack.json pack.json.gz);;All files (*)")
        if not path:
            return
        if os.path.basename(path) in ("pack.yaml", "pack.yml", "pack.json"):
            path = os.path.dirname(path)          # the manifest picked: install its folder
        try:
            pack, dest, warnings = D.install_pack(path)
        except D.InstallError as ex:
            QtWidgets.QMessageBox.warning(self, "Pack not installed", str(ex))
            self._log("Pack not installed: %s" % ex)
            return
        self._log("Installed pack %s %s -> %s" % (pack.id, pack.version, dest))
        for w in warnings:
            self._log("  warning: %s" % w)
        self.reload_definitions(quiet=True)
        self.status.setText("Installed %s %s (%d document%s)%s" % (
            pack.id, pack.version, len(pack.docs), "" if len(pack.docs) == 1 else "s",
            " - %d warning%s in the log" % (len(warnings), "" if len(warnings) == 1 else "s") if warnings else ""))

    def show_packs(self):
        """The loaded packs by layer, with where each came from."""
        rows = ["<table cellpadding='3'><tr><th align='left'>Layer</th><th align='left'>Pack</th>"
                "<th align='left'>Version</th><th align='left'>Documents</th><th align='left'>Location</th></tr>"]
        for p in self._packs:
            rows.append("<tr><td>%s</td><td><b>%s</b></td><td>%s</td><td>%d</td><td><small>%s</small></td></tr>"
                        % (p.origin, p.id, p.version, len(p.docs), p.path))
        rows.append("</table>")
        if self._errors:
            rows.append("<p><b>Skipped:</b><br>" + "<br>".join("%s - %s" % (pth, msg) for pth, msg in self._errors) + "</p>")
        rows.append("<p>Your packs folder: <code>%s</code><br>Packs there win over the modules' and the bundled "
                    "ones for the same module and specificity.</p>" % D.user_dir())
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Installed definition packs")
        box.setTextFormat(QtCore.Qt.TextFormat.RichText)
        box.setText("".join(rows))
        box.exec()

    def open_defs_folder(self):
        d = D.user_dir()
        os.makedirs(d, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    # ------------------------------------------------------ vehicle live data
    def _fill_multi_modules(self):
        can = self.transport.currentData() == "can"
        cur = self.multi_module.currentData()
        self.multi_module.clear()
        for row in self._rows():
            if as_int((row.get("tp20") or {}).get("dest"), None) is not None:
                self.multi_module.addItem("%s  %s" % (row["addr"], row.get("name", "")), row)
        if cur:
            i = next((k for k in range(self.multi_module.count())
                      if (self.multi_module.itemData(k) or {}).get("addr") == cur.get("addr")), -1)
            if i >= 0:
                self.multi_module.setCurrentIndex(i)
        self.multi_btn.setEnabled(can)
        self.multi_hint.setVisible(not can)

    def multi_add(self):
        row = self.multi_module.currentData()
        if not row:
            return
        key = (row["addr"], self.multi_group.value())
        if key in [(a, g) for a, g, _r in self._multi_items]:
            return
        self._multi_items.append((row["addr"], self.multi_group.value(), row))
        self._multi_rebuild_table()
        live = self._lives.get(row["addr"])
        if self._multi_running:
            if live is None:
                self.connect_module(row)
            elif len([1 for a, g, _r in self._multi_items if a == row["addr"]]) == 1:
                self._multi_schedule(live)

    def multi_remove(self):
        rows = {i.row() for i in self.multi_table.selectedItems()}
        keys = set()
        for r in rows:
            # every row of a group's block carries its (addr, group) key;
            # the module label shows on the first row only
            it = self.multi_table.item(r, 0)
            key = it.data(QtCore.Qt.ItemDataRole.UserRole) if it is not None else None
            if key:
                keys.add(tuple(key))
        self._multi_items = [it for it in self._multi_items if (it[0], it[1]) not in keys]
        for k in keys:
            self._multi_values.pop(k, None)
        self._multi_rebuild_table()

    def _multi_rebuild_table(self):
        t = self.multi_table
        t.setRowCount(0)
        values = getattr(self, "_multi_values", {})

        def add(key, *cells):
            r = _row(t, *cells)
            t.item(r, 0).setData(QtCore.Qt.ItemDataRole.UserRole, key)     # multi_remove keys on it

        for addr, g, row in self._multi_items:
            cells = values.get((addr, g))
            label = "%s %s" % (addr, row.get("name", ""))
            if not cells:
                add((addr, g), label, "%03d" % g, "", "", "(waiting)" if self._multi_running else "", "")
                continue
            if isinstance(cells, str):
                add((addr, g), label, "%03d" % g, "", "", cells, "")
                continue
            for i, (name, text, raw) in enumerate(cells, 1):
                add((addr, g), label if i == 1 else "", "%03d.%d" % (g, i), str(i), name or "", text, raw)
        t.resizeColumnToContents(0)

    def multi_toggle(self):
        if self._multi_running:
            self._multi_running = False
            self.multi_btn.setText("Start")
            self.multi_state.setText("stopped (sessions stay open; Disconnect all closes them)")
            return
        if self.transport.currentData() != "can":
            self.status.setText("Vehicle live data needs the CAN transport")
            return
        if not self._multi_items:
            self.status.setText("Add a module and group first")
            return
        self._multi_values = {}
        self._multi_idx: Dict[str, int] = {}
        self._multi_reads = 0
        self._multi_running = True
        self.multi_btn.setText("Stop")
        self.multi_state.setText("connecting...")
        self._multi_rebuild_table()
        for addr in dict.fromkeys(a for a, _g, _r in self._multi_items):
            row = next(r for a, _g, r in self._multi_items if a == addr)
            live = self._lives.get(addr)
            if live is None:
                self.connect_module(row)
            elif live.sess is not None:
                self._multi_schedule(live)

    def _multi_schedule(self, live: LiveSession):
        if not self._multi_running:
            return
        groups = [g for a, g, _r in self._multi_items if a == live.addr]
        if not groups:
            return
        idx = self._multi_idx.get(live.addr, 0) % len(groups)
        self._multi_idx[live.addr] = idx + 1
        g = groups[idx]
        live.post("multi", lambda s: (g, s.read_group_full(g), s.group_names(g)))

    def _on_multi(self, live: LiveSession, payload):
        g, reading, names = payload
        key = (live.addr, g)
        if reading is None or not reading.available:
            self._multi_values[key] = "not available"
        elif reading.cells is not None:
            self._multi_values[key] = [((names[i] if i < len(names) else None) or "", c.decoded[2],
                                        "%02X %02X %02X" % (c.fid, c.a, c.b)) for i, c in enumerate(reading.cells)]
        elif reading.raw is not None:
            self._multi_values[key] = [("raw", " ".join("%02X" % b for b in reading.raw), "")]
        else:
            self._multi_values[key] = "no data"
        self._multi_reads += 1
        self._multi_rebuild_table()
        self.multi_state.setText("%d sessions · %d reads" % (len(self._lives), self._multi_reads))
        self._multi_schedule(live)

    def _multi_failed(self, live: LiveSession, msg: str):
        for a, g, _r in self._multi_items:
            if a == live.addr and (a, g) not in self._multi_values:
                self._multi_values[(a, g)] = "error: %s" % msg
        self._multi_rebuild_table()
        if self._multi_running and live.sess is not None:
            QtCore.QTimer.singleShot(1000, lambda: self._multi_schedule(live))

    # --------------------------------------------------------- procedures
    def _start_procedure(self, doc: dict):
        """The Procedures page asked to run ``doc`` on the selected module:
        the engine runs on its live session, its questions go to the page
        through the prompter ``begin_run`` hands out. One procedure at a
        time per session."""
        if not doc or not self._require_live():
            return
        live = self._live
        if live.prompter is not None:
            self.status.setText("A procedure is already running on %s; finish or stop it first" % live.addr)
            return
        from .procedures import ProcedureRun
        res, wsc = self._res, self.wsc_edit.value() or None
        journal = Journal(self._vehicle.id).record if self._vehicle is not None else None
        prompter = self.proc_page.begin_run(doc)
        live.prompter = prompter                  # LiveSession.stop() releases the waiting engine through it
        self._sync_procedures_page()
        live.post("procedure", lambda s: ProcedureRun(doc, s, res, prompter, journal=journal,
                                                      log=lambda m: live.log.emit(m), wsc=wsc).run())

    def _on_procedure(self, result, live: Optional[LiveSession] = None):
        if live is not None:
            live.prompter = None
        self.proc_page.finish_run(result)
        self._sync_procedures_page()
        self._refresh_journal()
        self.status.setText("Procedure %s: %s%s" % (result.name or result.procedure_id, result.status,
                                                   (" - " + result.message) if result.message else ""))

    def _new_procedure(self):
        """``+ New procedure``: procedures are definitions, so the local
        definitions folder opens and the help page on writing one is named."""
        self.open_defs_folder()
        QtWidgets.QMessageBox.information(
            self, self.windowTitle(),
            "A procedure is a definition file. Your local definitions folder is open: add a document of kind "
            "'procedure' to your pack there, then press Reload definitions.\n\n"
            "The Procedures page of the help (F1) describes the steps and the schema.")

    def _show_bus_trace(self):
        """``Show trace while writing``: the bottom pane open and the trace
        recorded to a file."""
        sizes = self.vsplit.sizes()
        if len(sizes) > 1 and sizes[1] < 80:
            total = sum(sizes)
            self.vsplit.setSizes([max(100, total - 160), 160])
        self.record_chk.setChecked(True)

    # ------------------------------------------------------------- OBD-II
    def _obd_job(self, what: str):
        it = self._need_iface()
        if it is None:
            return None
        transport, rate = self.transport.currentData(), self.can_rate.currentData() or 500000
        bus = self._ensure_bus() if transport == "can" else None
        if transport != "can" and self._lives:
            self.status.setText("Disconnect the live session first (%s)" % self._one_session_note())
            return None

        def job(ctx):
            from . import obd as O
            t, owners = L.open_obd(it, transport, ctx.log, can_rate=rate, bus=bus)
            sess = O.ObdSession(t, ctx.log)
            try:
                sess.connect()
                if what == "clear":
                    return {"cleared": sess.clear(), "dtcs": sess.dtcs(), "protocol": sess.protocol}
                return sess.snapshot()
            finally:
                sess.close()
                for o in owners:
                    try:
                        o.close()
                    except Exception:
                        pass
        return job

    def obd_read(self):
        job = self._obd_job("read")
        if job is not None:
            self._start(job, self._on_obd, "Reading generic OBD-II...")

    def obd_clear(self):
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            "Clear the emissions fault codes and freeze frames of every emissions module (OBD-II mode 04)?\n\n"
            "Readiness monitors start over; the codes present now are kept in the vehicle's journal.")
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        job = self._obd_job("clear")
        if job is not None:
            self._start(job, self._on_obd_clear, "Clearing emissions codes...")

    def _on_obd_clear(self, out):
        self._journal_record(addr="OBD", module="OBD-II", function="clear_emissions_codes",
                             before=[d.code for d in (getattr(self, "_obd_snap", None).dtcs if getattr(self, "_obd_snap", None) else [])],
                             after=[d.code for d in out["dtcs"]], verified=not out["dtcs"],
                             note="%d module(s) confirmed" % len(out["cleared"]))
        self.status.setText("Emissions codes cleared by %d module(s)%s" % (
            len(out["cleared"]), "" if not out["dtcs"] else "; %d still reported" % len(out["dtcs"])))
        # the re-read from the event loop, after this job's worker is released
        QtCore.QTimer.singleShot(0, self.obd_read)

    def _on_obd(self, snap):
        from . import obd as O
        from .. import dtc as _dtc
        self._obd_snap = snap
        self.obd_proto.setText("%s · %d module%s" % (snap.protocol, len(snap.modules), "" if len(snap.modules) == 1 else "s"))
        t = self.obd_status
        t.setRowCount(0)
        for m, st in sorted(snap.status.items()):
            label = O.ObdReply(m, 1, 1, b"").module_label
            first = True
            for mon in st.get("monitors") or [{"name": "(no monitors reported)", "complete": None}]:
                _row(t, label if first else "", ("ON" if st.get("mil") else "off") if first else "",
                     str(st.get("dtc_count", "")) if first else "", mon["name"],
                     "" if mon["complete"] is None else ("yes" if mon["complete"] else "not yet"))
                if mon["complete"] is False:
                    _bold(t.item(t.rowCount() - 1, 4))
                first = False
        t.resizeColumnToContents(0)
        t = self.obd_dtcs
        t.setRowCount(0)
        pdesc = _dtc._load_pdesc()
        for d in snap.dtcs:
            _row(t, d.code, O.ObdReply(d.module, 3, None, b"").module_label, d.kind, pdesc.get(d.code, ""))
        t = self.obd_pids
        t.setRowCount(0)
        for m, vals in sorted(snap.pids.items()):
            label = O.ObdReply(m, 1, 1, b"").module_label
            for pid, v in sorted(vals.items()):
                p = O.PIDS.get(pid)
                _row(t, label, "%02X" % pid, p.name if p else "", O.format_value(pid, v))
        t.resizeColumnToContents(0)
        bits = []
        if snap.vin:
            bits.append("VIN %s" % snap.vin)
        for m, info in sorted(snap.info.items()):
            label = O.ObdReply(m, 9, None, b"").module_label
            for k, v in info.items():
                if k == "VIN":
                    continue
                bits.append("%s %s: %s" % (label, k, ", ".join(v) if isinstance(v, list) else v))
        self.obd_info.setText("  ·  ".join(bits))
        self._show_tools(self._tool_pages["OBD-II"])
        self.status.setText("OBD-II: %d module(s), %d code(s), %d value(s)" % (
            len(snap.modules), len(snap.dtcs), sum(len(v) for v in snap.pids.values())))
        if self._vehicle is not None and snap.vin and not self._vehicle.vin:
            self._vehicle.vin = snap.vin
            self._garage.save_vehicle(self._vehicle)
            self._log("Vehicle VIN recorded from OBD-II: %s" % snap.vin)

    def copy_obd(self):
        snap = getattr(self, "_obd_snap", None)
        if snap is None:
            return
        from . import obd as O
        lines = ["OBD-II %s" % snap.protocol]
        if snap.vin:
            lines.append("VIN %s" % snap.vin)
        for m, st in sorted(snap.status.items()):
            lines.append("%s: MIL %s, %d code(s); monitors: %s" % (
                O.ObdReply(m, 1, 1, b"").module_label, "on" if st.get("mil") else "off", st.get("dtc_count", 0),
                ", ".join("%s %s" % (x["name"], "ok" if x["complete"] else "incomplete") for x in st.get("monitors") or [])))
        for d in snap.dtcs:
            lines.append("  %s  %s  %s" % (d.code, d.kind, O.ObdReply(d.module, 3, None, b"").module_label))
        for m, vals in sorted(snap.pids.items()):
            for pid, v in sorted(vals.items()):
                p = O.PIDS.get(pid)
                lines.append("  %s  %02X %s: %s" % (O.ObdReply(m, 1, 1, b"").module_label, pid, p.name if p else "", O.format_value(pid, v)))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("OBD-II data copied")

    # --------------------------------------------------------------- report
    def _wire_report_page(self):
        rp = self.report_page
        rp.backRequested.connect(lambda: self.stack.setCurrentIndex(0))
        rp.scanChanged.connect(lambda: self._render_report())
        rp.compareChanged.connect(lambda: self._render_report())
        rp.exportRequested.connect(self._export)
        rp.openGroupRequested.connect(self._open_group)
        rp.moduleActivated.connect(self._open_module)
        rp.traceRequested.connect(self._open_trace_file)

    def _refresh_scans(self, select_latest: bool = False):
        items = []
        if self._vehicle is not None:
            items = [(G.scan_label(name), name) for name in self._garage.scans(self._vehicle.id)]
        self.report_page.set_scans(items, items[0][1] if items else None, items[1][1] if len(items) > 1 else None)
        self._render_report()

    def _selected_scans(self):
        if self._vehicle is None:
            return self._scan, None
        name = self.report_page.selected_scan_key()
        cmp_ = self.report_page.compare_key()
        scan = self._garage.load_scan(self._vehicle.id, name) if name else self._scan
        before = self._garage.load_scan(self._vehicle.id, cmp_) if cmp_ and cmp_ != name else None
        return scan, before

    def _render_report(self, *_, scan: Optional[ScanResult] = None):
        if scan is None:
            scan, before = self._selected_scans()
        else:
            before = None
        vehicle = self._vehicle or Vehicle(id="", name="(unsaved vehicle)")
        j = self._journal()
        entries = j.since(before.when) if (j and before) else (j.entries() if j else [])
        self.report_page.show_report(vehicle, scan, before, entries)

    def _export(self, kind: str):
        if kind == "pdf":
            self.export_pdf()
        elif kind == "text":
            self.copy_report()
        else:
            self.export_html()

    def export_html(self):
        scan, before = self._selected_scans()
        if scan is None:
            self.status.setText("No scan to export yet")
            return
        vehicle = self._vehicle or Vehicle(id="", name="(unsaved vehicle)")
        name = "%s-scan-%s.html" % (G.slug(vehicle.label), time.strftime("%Y%m%d-%H%M", time.localtime(scan.when)))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export scan report", name, "HTML (*.html)")
        if not path:
            return
        j = self._journal()
        entries = j.since(before.when) if (j and before) else []
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(R.html_report(vehicle, scan, before, entries))
        except Exception as ex:
            self._log("Could not write the report: %s" % ex)
            self.status.setText("Could not write %s: %s" % (path, ex))
            return
        self.status.setText("Report written: %s" % path)

    def export_pdf(self):
        scan, before = self._selected_scans()
        if scan is None:
            self.status.setText("No scan to export yet")
            return
        vehicle = self._vehicle or Vehicle(id="", name="(unsaved vehicle)")
        name = "%s-scan-%s.pdf" % (G.slug(vehicle.label), time.strftime("%Y%m%d-%H%M", time.localtime(scan.when)))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export scan report as PDF", name, "PDF (*.pdf)")
        if not path:
            return
        j = self._journal()
        entries = j.since(before.when) if (j and before) else []
        try:
            write_pdf(path, R.html_report(vehicle, scan, before, entries))
        except Exception as ex:
            self._log("Could not write the PDF: %s" % ex)
            self.status.setText("Could not write %s: %s" % (path, ex))
            return
        self.status.setText("PDF written: %s" % path)

    def copy_report(self):
        QtWidgets.QApplication.clipboard().setText(self.report_page.text())
        self.status.setText("Report copied as text")

    def _open_trace_file(self, path: str):
        if path and os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        else:
            self.status.setText("The trace file is not there any more: %s" % path)

    # ---------------------------------------------------------------- garage
    def _refresh_garage(self):
        """The Garage tab and the rail's ``Garage . N vehicles`` line."""
        vehicles = self._garage.vehicles()
        t = self.garage_table
        t.setRowCount(0)
        total = 0
        for v in vehicles:
            names = self._garage.scans(v.id)
            total += len(names)
            last = G.scan_label(names[0]) if names else ""
            r = _row(t, v.label, v.vin, v.generation or "(not set)", len(names), last)
            t.item(r, 0).setData(QtCore.Qt.ItemDataRole.UserRole, v.id)
            if self._vehicle is not None and v.id == self._vehicle.id:
                for c in range(t.columnCount()):
                    _bold(t.item(r, c))
        self._garage_counts = (len(vehicles), total)
        self.garage_summary.setText("%d vehicle%s, %d scan%s" % (
            len(vehicles), "" if len(vehicles) == 1 else "s", total, "" if total == 1 else "s"))
        self._update_footer()

    def _garage_selected_id(self) -> str:
        items = self.garage_table.selectedItems()
        if not items:
            return ""
        return self.garage_table.item(items[0].row(), 0).data(QtCore.Qt.ItemDataRole.UserRole) or ""

    def garage_use(self):
        vid = self._garage_selected_id()
        if not vid:
            self.status.setText("Select a vehicle first")
            return
        self._refresh_vehicles(select=vid)
        self.stack.setCurrentIndex(0)

    def garage_delete(self):
        vid = self._garage_selected_id()
        if not vid:
            self.status.setText("Select a vehicle first")
            return
        v = self._garage.vehicle(vid)
        if v is None:
            return
        ans = QtWidgets.QMessageBox.question(
            self, self.windowTitle(),
            "Delete the vehicle %s with its %d scan(s) and its write journal?\n\nThis cannot be undone."
            % (v.label, len(self._garage.scans(vid))),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._garage.delete_vehicle(vid)
        self._log("Vehicle deleted: %s" % v.label)
        self._last_vehicle = ""
        keep = self._vehicle.id if (self._vehicle is not None and self._vehicle.id != vid) else None
        self._refresh_vehicles(select=keep)

    # ---------------------------------------------------------------- trace
    def _log(self, msg: str):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        self.trace.appendPlainText(line)
        if self._trace_file is not None:
            try:
                self._trace_file.write(line + "\n")
            except OSError:
                pass

    def _open_trace(self):
        if self._trace_file is not None:
            return
        d = os.path.join(G.root(), "..", "traces")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".log")
        try:
            self._trace_file = open(path, "a", encoding="utf-8")
            self._trace_path = os.path.abspath(path)
            self._log("Recording trace to %s" % self._trace_path)
        except OSError as ex:
            self._log("Cannot record trace: %s" % ex)
        self._update_trace_title()

    def _update_trace_title(self):
        path = getattr(self, "_trace_path", "") if self._trace_file is not None else ""
        self.trace_title.setText("<b>Bus trace</b> \u00b7 decoded" + (
            " \u00b7 recording to %s" % html.escape(os.path.basename(path)) if path else ""))

    def _close_trace(self):
        if self._trace_file is not None:
            try:
                self._trace_file.close()
            finally:
                self._trace_file = None
                self._update_trace_title()

    def save_trace(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save session trace",
                                                        time.strftime("ovtdiag-%Y%m%d-%H%M.log"), "Log (*.log)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.trace.toPlainText())
        except Exception as ex:
            self._log("Could not save the trace: %s" % ex)
            self.status.setText("Could not write %s: %s" % (path, ex))
            return
        self.status.setText("Trace saved: %s" % path)

    # ---------------------------------------------------------------- close
    def showEvent(self, ev):
        super().showEvent(ev)
        if not getattr(self, "_shown_once", False):
            self._shown_once = True
            self.setFocus()                    # no chip or button starts highlighted
            h = self.vsplit.height()           # the bottom panes: 160 px, less on a small screen
            bottom = 160 if h >= 700 else 120
            self.vsplit.setSizes([max(100, h - bottom), bottom])

    def keyPressEvent(self, ev):
        """Esc on the report or the vehicle tools goes back to the modules
        (on the workspace it is the Close button: it asks while busy)."""
        if ev.key() == QtCore.Qt.Key.Key_Escape and self.stack.currentIndex() != 0:
            self.stack.setCurrentIndex(0)
            return
        super().keyPressEvent(ev)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QtCore.QEvent.Type.WindowStateChange and hasattr(self, "live_view"):
            self._live_update()                # minimized: the live reads pause

    def closeEvent(self, ev):
        self._closing = True
        self._live_stop()
        self.live_view.stop_log()
        self._multi_running = False
        self._basic_running = False
        self.palette.close_palette()
        self.proc_page.abort()              # a question waiting in the page must not hold the close
        lives = list(self._lives.values())
        for live in lives:
            live.stop()                     # releases a waiting prompter too (LiveSession.stop)
        for live in lives:
            live.wait(5000)
        self._lives.clear()
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(5000)
        self._close_bus()
        self._close_trace()
        self._save()
        super().closeEvent(ev)

    def reject(self):
        """Escape and the Close button: with sessions open or an operation
        running, the user is asked first (default No); a No leaves the window
        as it is. closeEvent (the title bar's X, :func:`gui._run_tool`) does
        not ask: once there, the window is going."""
        if self.palette.is_open():
            self.palette.close_palette()
            self._palette_closed()
            return
        if self._closing:
            # QDialog.closeEvent calls reject() and keeps the window when it
            # is still visible afterwards: hide it the plain way here
            super().reject()
            return
        busy = []
        if self._lives:
            busy.append("Sessions are open")
        if self._worker is not None and self._worker.isRunning():
            busy.append("an operation is running" + (" (%s)" % self._busy_what.rstrip(".")
                                                     if self._busy_what else ""))
        elif self._basic_running or self._multi_running:
            busy.append("a basic setting is running" if self._basic_running else "vehicle live data is running")
        if busy:
            text = " and ".join(busy)
            ans = QtWidgets.QMessageBox.question(
                self, self.windowTitle(),
                "%s. Close %s?" % (text[:1].upper() + text[1:], self.windowTitle()),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.close()


def write_pdf(path: str, html: str):
    """The HTML report as a PDF (Qt's own writer: no extra dependency, every
    platform)."""
    writer = QtGui.QPdfWriter(path)
    writer.setPageSize(QtGui.QPageSize(QtGui.QPageSize.PageSizeId.A4))
    writer.setPageMargins(QtCore.QMarginsF(15, 15, 15, 15))
    doc = QtGui.QTextDocument()
    doc.setHtml(html)
    doc.print_(writer)


def _hex_spaced(h: str) -> str:
    h = (h or "").replace(" ", "")
    return " ".join(h[i:i + 2] for i in range(0, len(h), 2)).upper()


def _pre_text(pre) -> str:
    if pre is None:
        return ""
    items = pre if isinstance(pre, list) else [pre]
    out = []
    for p in items:
        if isinstance(p, dict):
            out += ["%s %s" % kv for kv in p.items()]
        else:
            out.append(str(p))
    return ", ".join(out)
