# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data logging dialog: live measuring-block browsing, cell selection,
logging to the native (KWHack-layout) CSV, a live strip chart, and a log
viewer with VCDS export. The ECU side is :mod:`ovtsuite.flash.diag`.

The session runs in one QThread (:class:`LogWorker`) that mirrors KWHack's
``RunConnectedSession``: connect (EDC16: 10 89 + optional link-speed raise;
EDC15: KW1281), then loop -- while browsing read one group and report its
cells; while logging read the distinct groups of the selected cells, write
one row, report the values; while paused send a keep-alive; on a K-line
error reconnect at the same baud for up to 10 s. Names for cells come from
the loaded binary (:class:`ChannelNamer`) so columns carry A2L signal
identities, falling back to "Group N / Cell M".
"""
import bisect
import os
import threading
import time
import weakref
from datetime import datetime
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..flash import diag as _diag
from ..flash import j2534 as _j2534
from ..flash import keepawake as _keepawake
from ..flash.transport import KLine, KLineError
from ..flash.gui_flash import (list_ports, kline_devices, is_j2534, open_j2534,
                               no_device_hint,
                               cable_allows_high)
from . import derived as _derived
from . import logfile as _lf
from .channels import ChannelNamer
from . import msbcatalogue as _msbcat
from . import logview as _lv
from .. import branding

SPEED_LABELS = ["Low (10400)", "Medium (124800)", "High (244898)"]
SPEED_KEYS = ["Low", "Medium", "High"]

#: Every ECU the logger can address, and over what.
#:
#: EDC15 is K-line only -- it speaks KW1281 and has no TP2.0 presence. The ZF
#: 6HP TCU is the mirror image: no K-line on any car seen, CAN only. Keeping
#: the pairing here rather than in ``_transport_changed`` is what stopped the
#: old "CAN/TP2.0 is EDC16 only" line from having to be a hard-coded index.
ECUS = ("EDC16", "EDC15", "ZF6HP", "ZF6HP-UDS", "Aisin TRx")


def _binary_platform(session):
    """"EDC15" / "EDC16" for a session whose measuring blocks the engine
    namers read, else None."""
    if session is None:
        return None
    try:
        from ..msblock import has_measuring_blocks
        if not has_measuring_blocks(session):
            return None
    except Exception:
        return None
    return "EDC15" if getattr(session, "is_edc15", False) else "EDC16"


def _identity_note(session, ecu: str, identity: str, namer) -> str:
    """A warning when the open file is not the software the ECU reported --
    its names are then only as good as the two calibrations' agreement."""
    if not identity:
        return ""
    try:
        if ecu == "EDC16":
            from .. import fileinfo
            got = _msbcat.edc16_identity(identity)
            ident = fileinfo.ident16(bytes(session.binfile.data))
            if got and ident.get("part") and got[0] != ident["part"]:
                return ("The ECU reported %s but the file is %s, so some names "
                        "may not match this car." % (got[0], ident["part"]))
        elif ecu == "EDC15" and getattr(namer, "dataset", None) is None:
            return ("The ECU's ident matches none of the file's datasets, so "
                    "cells are named from its first dataset.")
    except Exception:
        return ""
    return ""
CAN_ECUS = ("EDC16", "ZF6HP", "ZF6HP-UDS", "Aisin TRx")
KLINE_ECUS = ("EDC16", "EDC15")
PALETTE = [QtGui.QColor(c) for c in ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
                                      "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f")]


def kline_over_j2534(dev, ecu, fast_init, log):
    """The K-line link for ``ecu`` on an opened J2534 device.

    EDC15 speaks KW1281, which acknowledges every byte with its complement:
    it needs the byte-stream link (KWHack's PassThruKLineInterface port),
    woken by the device's own 5-baud init -- never fast init, which an EDC15
    must not be sent. Everything else on K-line is KWP2000, whose frames the
    message link carries."""
    if ecu == "EDC15":
        from ..flash.kline_j2534_raw import J2534RawKLine
        return J2534RawKLine(dev, baud=_diag.EDC15_BAUD, log=log)
    from ..flash.kline_j2534 import J2534KLine
    return J2534KLine(dev, log=log, fast_init=fast_init)


# ------------------------------------------------------------ thread lifetime
class _Reaper(QtCore.QObject):
    """Keeps a worker QThread alive until it has really finished.

    Crash fix (2026-10-02, logging over CAN): the dialog dropped its only
    reference to the worker as soon as the worker said it had ended -- but
    that signal is sent from INSIDE the thread, which still has to unwind
    (release the keep-awake hold, return from run). When Python's reference
    went first, PySide deleted the C++ QThread while it was still running,
    which Qt answers with a fatal abort: the whole app vanished on Disconnect.
    The slower the close (CAN: stopCommunication, TP 2.0 disconnect, J2534
    close, often through the 32-bit bridge) the wider that window. The same
    could happen to a fault-code task whose dialog closed before its
    connection was torn down.

    Lives in the GUI thread, so ``finished`` reaches it queued; a thread is
    released only once ``isRunning()`` is false.
    """

    def __init__(self):
        super().__init__()
        self._held = set()

    def hold(self, thread):
        if thread is None or not thread.isRunning():
            return
        if thread not in self._held:
            self._held.add(thread)
            thread.finished.connect(self._release)

    @QtCore.Slot()
    def _release(self):
        for t in list(self._held):
            if not t.isRunning():
                self._held.discard(t)
        if self._held:
            QtCore.QTimer.singleShot(50, self._release)


_reaper: Optional[_Reaper] = None


def retire_thread(thread):
    """Let go of ``thread`` without destroying it while it still runs."""
    global _reaper
    if _reaper is None:
        _reaper = _Reaper()
    _reaper.hold(thread)


# ------------------------------------------------------------------ worker
#: Read timeout for live data once a session is open. The links connect with
#: 8 s -- the slow init and the flasher need it -- but a measuring block
#: answers in tens of milliseconds, and with 8 s a dead line held every row,
#: and a Disconnect, for 8 s per request. Set after every (re)connect.
LIVE_READ_TIMEOUT = 1.5

#: Seconds the worker's own teardown (stopCommunication, channel and device
#: close) may take: the 32-bit bridge gets what is left of it to exit before
#: it is killed.
TEARDOWN_BUDGET = 2.5

#: After Disconnect (or closing the window) the worker gets this long to wind
#: down by itself; then the transport is cut under it (abort_transport).
#: Never on Stop -- Stop leaves the connection alone.
DISCONNECT_GRACE_MS = 8000

#: Closing the window: one shared wait for every connection, not one each.
CLOSE_WAIT_S = 5.0
#: ... and after cutting the transport, how long to wait before leaving the
#: thread to the reaper.
ABORT_WAIT_S = 1.5


class LogWorker(QtCore.QThread):
    connected = QtCore.Signal(str, int)              # identity, baud
    browsed = QtCore.Signal(int, list)               # group, [(field, text, value)]
    #: generation, stamp, texts, values -- the generation is the recording the
    #: row belongs to (start_logging's return), so a row that was already on
    #: its way when Stop was pressed is recognised and dropped
    logged = QtCore.Signal(int, object, list, list)
    status = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    ended = QtCore.Signal()
    #: (works, explanation) -- whether 0x23 answered, settled once on connect
    mem_probed = QtCore.Signal(bool, str)

    def __init__(self, port, ecu, speed, cap_high, transport="kline", can_rate=500000, can_dest=0x01,
                 fast_init=False):
        super().__init__()
        self._port, self._ecu, self._speed, self._cap = port, ecu, speed, cap_high
        self._transport, self._can_rate, self._can_dest = transport, can_rate, can_dest
        self._fast_init = fast_init
        self._can = None
        self._lock = threading.Lock()
        self._browse_group = 1
        self._channels: List[_lf.Channel] = []
        self._writer: Optional[_lf.KwhackWriter] = None
        self._fh = None
        self._logging = False
        self._paused = False
        self._stop = False
        self._gen = 0                   # recording generation (see ``logged``)
        self._link = None               # for abort_transport
        self.rows = 0

    # -- controls (any thread) --
    def set_browse_group(self, g):
        with self._lock:
            self._browse_group = int(g)

    def start_logging(self, channels, path):
        """Open ``path`` and start writing rows; returns the recording's
        generation, which every ``logged`` row of it carries."""
        with self._lock:
            self._channels = list(channels)
            self._fh = open(path, "w", encoding="utf-8", newline="")
            self._writer = _lf.KwhackWriter(self._fh, self._channels)
            self._logging = True
            self._paused = False
            self.rows = 0
            self._gen += 1
            return self._gen

    def stop_logging(self):
        """Close the file and go back to browsing. The connection stays up."""
        with self._lock:
            self._logging = False
            self._paused = False
            self._gen += 1
            self._close_file()

    def pause(self, on):
        with self._lock:
            self._paused = bool(on)

    def stop(self):
        self._stop = True

    def abort_transport(self):
        """Cut the link under a worker that has not wound down by itself --
        the last resort after Disconnect, from the GUI thread. Only for a
        transport whose blocked call can be ended from outside:

        * the 32-bit J2534 bridge: kill the process; the worker's pending
          read sees EOF and unwinds with a J2534Error;
        * a serial K-line cable: cancel the pending read/write;
        * a J2534 DLL loaded in-process: nothing can interrupt a call into
          it, so the thread is left to finish (the reaper holds it).

        Returns what was done ("bridge", "serial") or None."""
        if not self._stop:
            return None                 # only ever after Disconnect / close
        can = self._can
        if isinstance(can, _j2534.BridgePassThru):
            can.kill()
            return "bridge"
        link = self._link
        if isinstance(link, KLine):
            link.cancel()
            return "serial"
        return None

    def _close_file(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._writer = None

    # -- thread body --
    def run(self):
        # A drive log is many minutes with nobody touching the laptop -- what
        # the OS idle timer waits for. Once the screen blanks and the machine
        # suspends, the USB cable stops being serviced and the session drops
        # (the flasher hit exactly this, see flash/keepawake.py). Hold the
        # computer awake for as long as this worker is connected or trying to
        # be; released the moment it ends. Entered HERE, on the worker thread:
        # the Windows hold belongs to the thread that asks for it.
        try:
            with _keepawake.prevent_sleep(
                    "%s is logging from a control unit" % branding.APP_NAME):
                self._run_session()
        finally:
            # last thing the thread does: everything is closed and released
            self.ended.emit()

    def _run_session(self):
        link = None
        sess = None
        try:
            speed, cap = self._speed, self._cap
            if self._ecu == "ZF6HP-UDS":
                # The 6HP28 does not answer TP 2.0: UDS over ISO-TP 0x7E1 ->
                # 0x7E9, routed by the gateway (the flasher's route). The
                # session opens and owns that route itself.
                self._can = open_j2534(self._port)
                speed, cap = "Low", False
            elif self._transport == "can":
                from ..flash import j2534 as _j
                from ..flash.tp20_transport import CanKLine
                self._can = open_j2534(self._port)
                if isinstance(self._can, _j.BridgePassThru):
                    tp = self._can.open_tp20(dest=self._can_dest, baud=self._can_rate,
                                             params={"block_size": 0x0F})
                    link = CanKLine(tp=tp, dest=self._can_dest, log=self.status.emit)
                else:
                    ch = self._can.connect(_j.CAN, self._can_rate)
                    ch.pass_all()
                    ch.clear_rx()
                    link = CanKLine(ch, dest=self._can_dest, log=self.status.emit)
                speed, cap = "Low", False        # no elevated K-line baud over CAN
            elif is_j2534(self._port):
                self._can = open_j2534(self._port)
                link = kline_over_j2534(self._can, self._ecu, self._fast_init, self.status.emit)
            else:
                link = KLine(self._port, baud=10400,
                             prefer_fast_init=self._fast_init and self._ecu != "EDC15")
            self._link = link
            if self._ecu == "ZF6HP-UDS":
                from .zf6hp_uds_live import Zf6hpUdsLiveSession
                sess = Zf6hpUdsLiveSession.over_can(self._can, self._can_rate,
                                                    self.status.emit, self._ecu)
            else:
                sess = _diag.DiagSession(link, self._ecu, self.status.emit)
                sess.fast_kw1281 = True   # KW1281 fast timing, as KWHack's live data uses
            try:
                sess.connect(speed, cap, is_stop=lambda: self._stop)
            except Exception as ex:
                if not self._stop:      # a Disconnect while connecting is no error
                    self.failed.emit("Could not connect: %s" % ex)
                return
            self._live_timeout(link)
            self.connected.emit(sess.identity, sess.baud)
            # Only when an address-read channel is actually ticked. The AST
            # chain preset that ticked them was removed (2026-10-10, Bear: it
            # never returned anything), and probing on every connect cost a
            # round of requests for nothing.
            if any(getattr(c, "is_memory", False) for c in self._channels):
                self._probe_memory(sess)
            while not self._stop:
                try:
                    with self._lock:
                        logging_ = self._logging
                        paused = self._paused
                        group = self._browse_group
                        chans = list(self._channels)
                        gen = self._gen
                    if paused:
                        sess.keepalive()
                        time.sleep(0.7)
                        continue
                    if logging_:
                        groups = sorted({c.group for c in chans
                                         if not c.is_memory and not c.is_derived})
                        res = (sess.read_groups(groups, is_stop=lambda: self._stop)
                               if groups else {})
                        texts, values = [], []
                        for c in chans:
                            if c.is_derived:     # worked out below
                                texts.append("N/A")
                                values.append(None)
                                continue
                            if c.is_memory:
                                t, v = self._read_mem(sess, c)
                                texts.append(t)
                                values.append(v)
                                continue
                            cells = res.get(c.group)
                            if cells and c.field < len(cells):
                                cell = cells[c.field]
                                texts.append(cell.text)
                                values.append(cell.value)
                            else:
                                texts.append("N/A")
                                values.append(None)
                        _derived.fill(chans, texts, values)
                        stamp = datetime.now()
                        with self._lock:
                            # Stopped (or restarted) while this row was being
                            # read: it belongs to no file, and must not reach
                            # the chart after Stop either.
                            current = self._logging and self._gen == gen
                            if current and self._writer is not None:
                                self._writer.row(stamp, texts)
                                self.rows += 1
                        if current:
                            self.logged.emit(gen, stamp, texts, values)
                    else:
                        cells = sess.read_group(group)
                        out = []
                        if cells:
                            for f, cell in enumerate(cells):
                                out.append((f, cell.text, cell.value))
                        self.browsed.emit(group, out)
                except _diag.StopRequested:
                    break
                except (KLineError, OSError, _j2534.J2534Error) as ex:
                    if self._stop:
                        break           # the read was cut short by Disconnect
                    self.status.emit("Lost the ECU (%s) - reconnecting for up to 10 s..." % ex)
                    if not self._reconnect(sess):
                        # Disconnect during the retries is not a failure to report
                        if not self._stop:
                            self.failed.emit("Could not reconnect within 10 seconds.")
                        break
                    self._live_timeout(link)
        finally:
            self._teardown(sess, link)

    def _live_timeout(self, link):
        """Short reads for live data (see LIVE_READ_TIMEOUT); the connect
        paths put the link back to their own 8 s on every wake-up."""
        try:
            if link is not None and getattr(link, "read_timeout", None) is not None:
                link.read_timeout = LIVE_READ_TIMEOUT
        except Exception:
            pass

    def _teardown(self, sess, link):
        """Close session, link and device within TEARDOWN_BUDGET. Each step is
        short by itself (DiagSession.close drops the read timeout and skips
        the TP 2.0 stopCommunication; the link close is one channel
        disconnect); the bridge gets what is left of the budget to exit before
        it is killed. A call that hangs inside a DLL anyway is for the GUI's
        Disconnect watchdog (abort_transport)."""
        deadline = time.monotonic() + TEARDOWN_BUDGET
        with self._lock:
            self._close_file()
        try:
            if sess is not None:
                sess.close()
        except Exception:
            pass
        if link is not None:
            try:
                link.close()
            except Exception:
                pass
        if self._can is not None:
            try:
                if isinstance(self._can, _j2534.BridgePassThru):
                    self._can.close(timeout=max(0.5, deadline - time.monotonic()))
                else:
                    self._can.close()
            except Exception:
                pass

    def _probe_memory(self, sess):
        """Settle whether this ECU serves readMemoryByAddress, and at which
        address width, before anything depends on it.

        The decode is proved against a signal a measuring block also reports,
        because an address read that silently returns the wrong thing -- an
        echoed address, a two-byte read of a one-byte signal -- yields numbers
        that look entirely reasonable. Without the control there is no way to
        tell a working read from a plausible one.
        """
        from . import memchan
        self.mem_bytes = 0
        if not getattr(sess, "supports_memory_read", True):
            self.mem_status = "This controller serves no readMemoryByAddress."
            self.mem_probed.emit(False, self.mem_status)
            return
        if not sess.is_kwp2000:
            self.mem_status = "KW1281 has no readMemoryByAddress."
            self.mem_probed.emit(False, self.mem_status)
            return
        name, addr, width, signed, grp, slot = memchan.CONTROLS[0]
        for w in (3, 4, 2):
            try:
                raw = sess.read_memory(addr, width, addr_bytes=w)
            except ValueError:
                continue                     # address will not fit that width
            except (KLineError, OSError):
                raw = None
            if raw is None:
                continue
            by_addr = int.from_bytes(raw, "big", signed=signed)
            cells = None
            try:
                cells = sess.read_group(grp)
            except (KLineError, OSError):
                pass
            by_block = cells[slot].value if cells and len(cells) > slot else None
            if by_block is None:
                continue
            for sc in (1.0, 0.1, 0.01):
                if abs(by_addr * sc - by_block) <= max(2.0, abs(by_block) * 0.02):
                    self.mem_bytes = w
                    self.mem_status = ("Memory reads verified against %s (block %d.%d): "
                                       "%d-byte addressing." % (name, grp, slot + 1, w))
                    self.mem_probed.emit(True, self.mem_status)
                    return
        self.mem_status = ("This ECU does not serve readMemoryByAddress for these "
                           "addresses, so the AST channels cannot be read.")
        self.mem_probed.emit(False, self.mem_status)

    def _read_mem(self, sess, c):
        from . import memchan
        if not getattr(self, "mem_bytes", 0):
            return "N/A", None
        sig = memchan.signal_for(c)
        try:
            raw = sess.read_memory(c.addr, c.width, addr_bytes=self.mem_bytes)
        except (ValueError, KLineError, OSError):
            raw = None
        v = memchan.decode(sig, raw)
        return memchan.text(sig, v), v

    def _reconnect(self, sess):
        deadline = time.time() + 10.0
        while time.time() < deadline and not self._stop:
            try:
                sess.reconnect(is_stop=lambda: self._stop)
                self.status.emit("Reconnected at %d baud." % sess.baud)
                return True
            except Exception:
                time.sleep(0.5)
        return False


# ------------------------------------------------------------------- chart
class StripChart(QtWidgets.QWidget):
    """Time on X, every channel normalised to its own min..max, one colour
    per channel; click sets a cursor (``cursorChanged`` gives the row)."""
    cursorChanged = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self.times: List[float] = []
        self.series: List[List[Optional[float]]] = []
        self.names: List[str] = []
        self.units: List[str] = []
        self.window = 0.0            # seconds shown (0 = all)
        self.cursor = -1
        self.hidden = set()
        self.setMouseTracking(False)

    def set_data(self, times, series, names, units=None):
        self.times, self.series, self.names = list(times), [list(s) for s in series], list(names)
        self.units = list(units or [""] * len(names))
        self.update()

    def append(self, t, values):
        self.times.append(t)
        for i, v in enumerate(values):
            if i < len(self.series):
                self.series[i].append(v)
        if self.window and len(self.times) > 20000:
            self.times = self.times[-10000:]
            self.series = [s[-10000:] for s in self.series]
        self.update()

    def mousePressEvent(self, ev):
        if not self.times:
            return
        x0, x1 = self._xrange()
        w = max(1, self.width() - 12)
        t = x0 + (ev.position().x() - 6) / w * (x1 - x0)
        i = min(range(len(self.times)), key=lambda k: abs(self.times[k] - t))
        self.cursor = i
        self.cursorChanged.emit(i)
        self.update()

    def _xrange(self):
        if not self.times:
            return 0.0, 1.0
        x1 = self.times[-1]
        x0 = self.times[0] if not self.window else max(self.times[0], x1 - self.window)
        if x1 <= x0:
            x1 = x0 + 1.0
        return x0, x1

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        W, H = self.width(), self.height()
        if not self.times or not self.series:
            p.setPen(self.palette().text().color())
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "No data")
            return
        x0, x1 = self._xrange()
        left, top, right, bottom = 6, 18, W - 6, H - 16
        gw, gh = right - left, bottom - top
        pen = QtGui.QPen(QtGui.QColor(128, 128, 128, 70))
        p.setPen(pen)
        for k in range(1, 4):
            y = top + gh * k / 4
            p.drawLine(left, y, right, y)
        # first sample on screen; times only grow, so bisect rather than walk
        # from the start on every repaint of a long log
        start = min(bisect.bisect_left(self.times, x0), len(self.times) - 1)
        legend_x = left
        for i, s in enumerate(self.series):
            col = PALETTE[i % len(PALETTE)]
            if i in self.hidden:
                continue
            vals = [v for v in s[start:] if v is not None]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            if hi <= lo:
                hi = lo + 1.0
            path = QtGui.QPainterPath()
            started = False
            for k in range(start, len(self.times)):
                v = s[k] if k < len(s) else None
                if v is None:
                    started = False
                    continue
                x = left + (self.times[k] - x0) / (x1 - x0) * gw
                y = bottom - (v - lo) / (hi - lo) * gh
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            p.setPen(QtGui.QPen(col, 1.5))
            p.drawPath(path)
            name = self.names[i] if i < len(self.names) else "ch%d" % i
            at = self.cursor if 0 <= self.cursor < len(s) else len(s) - 1
            last = s[at] if s and s[at] is not None else None
            txt = "%s %s" % (name.split(" (")[0], ("%g" % last) if last is not None else "")
            p.setPen(col)
            fm = p.fontMetrics()
            p.drawText(legend_x, 13, txt)
            legend_x += fm.horizontalAdvance(txt) + 14
        if 0 <= self.cursor < len(self.times):
            x = left + (self.times[self.cursor] - x0) / (x1 - x0) * gw
            p.setPen(QtGui.QPen(self.palette().text().color(), 1, QtCore.Qt.DashLine))
            p.drawLine(x, top, x, bottom)
        p.setPen(self.palette().text().color())
        p.drawText(left, H - 3, "%.1f s" % x0)
        t = "%.1f s" % x1
        p.drawText(right - p.fontMetrics().horizontalAdvance(t), H - 3, t)


# ------------------------------------------------------------ connection bar
class ConnectionBar(QtWidgets.QWidget):
    """Port / ECU / speed pickers shared by the logging and DTC dialogs.

    ``scope`` separates one bar's remembered selections from another's. Dual
    logging needs it: with a single flat settings group both panes restored the
    same keys, so the transmission pane came up holding the engine's cable and
    whichever connected last overwrote the other's transport. Each module slot
    gets its own scope, and within a scope the cable is remembered per
    (controller, transport) -- switching K-line to CAN brings back the device
    last used on CAN rather than leaving a serial port selected.
    """

    def __init__(self, window=None, parent=None, scope="a"):
        super().__init__(parent)
        self._scope = str(scope)
        self._restoring = False
        # Nothing may be persisted while __init__ is still assembling the bar.
        # This is what defeated the per-scope keying added on 2026-10-09: the
        # construction-time ``_ecu_changed()`` below runs BEFORE
        # ``restore_settings()`` and used to call ``save_settings()`` with the
        # default transport, default controller and default port -- so every
        # dialog open overwrote the stored selections, and the restore two
        # lines later read back the defaults it had just written. The keys were
        # right; the data was already gone.
        self._ready = False
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.transport_box = QtWidgets.QComboBox()
        self.transport_box.addItem("K-line", "kline")
        self.transport_box.addItem("CAN", "can")
        self.transport_box.currentIndexChanged.connect(self._transport_changed)
        h.addWidget(self.transport_box)
        self.port_lbl = QtWidgets.QLabel("Port:")
        h.addWidget(self.port_lbl)
        self.port_box = QtWidgets.QComboBox()
        self._last_port_key = None
        h.addWidget(self.port_box, 1)
        rb = QtWidgets.QPushButton("Refresh")
        rb.clicked.connect(self.refresh_ports)
        h.addWidget(rb)
        self.can_rate = QtWidgets.QComboBox()
        for r in (500000, 250000, 125000):
            self.can_rate.addItem("%d" % r, r)
        self.can_rate.setVisible(False)
        h.addWidget(self.can_rate)
        h.addWidget(QtWidgets.QLabel("  ECU:"))
        self.ecu_box = QtWidgets.QComboBox()
        self.ecu_box.addItems(list(ECUS))
        self.ecu_box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.ecu_box.setMinimumContentsLength(7)
        h.addWidget(self.ecu_box)
        self.speed_lbl = QtWidgets.QLabel("  Speed:")
        h.addWidget(self.speed_lbl)
        self.speed_box = QtWidgets.QComboBox()
        self.speed_box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        h.addWidget(self.speed_box)
        self.fast_disable_chk = QtWidgets.QCheckBox("Disable Fast Init")
        self.fast_disable_chk.setChecked(False)
        self.fast_disable_chk.setToolTip(
            "By default K-line uses ISO 14230 fast init (break pulse on a serial cable; "
            "hardware on a J2534 device), which the later CAN-init-only EDC16 ECUs need. "
            "Check this to force the 5-baud slow init instead.")
        h.addWidget(self.fast_disable_chk)
        self.refresh_ports()
        self.port_box.currentIndexChanged.connect(self._port_changed)
        self.ecu_box.currentIndexChanged.connect(self._ecu_changed)
        # Persist on change rather than only on connect. Picking a transport
        # or a cable and then not connecting used to be forgotten entirely.
        self.can_rate.currentIndexChanged.connect(lambda *_: self.save_settings())
        self.speed_box.currentIndexChanged.connect(lambda *_: self.save_settings())
        self._ecu_changed()
        self.restore_settings()                      # last-used selections become defaults
        # ...and then the open calibration overrides them. The tab you are in is
        # a stronger signal than what you last logged: open the logger from a TCU
        # tab and you mean the TCU. This must run AFTER restore_settings, which is
        # what was wrong before -- the EDC15 default this replaces was applied
        # before the restore and so was silently undone by it. restore_settings
        # also deliberately never restores ZF6HP, so without this the TCU could
        # only ever be selected by hand.
        self.default_from_platform(getattr(window, "primary", None))
        # Assembly done: user changes from here on are real choices worth
        # remembering. Everything above was a default being applied.
        self._ready = True

    def set_ecu_quietly(self, name) -> bool:
        """Select a controller WITHOUT persisting it.

        For a choice the dialog makes on the user's behalf -- the second dual
        pane taking the controller the first one is not using. Persisting that
        would let an automatic default overwrite the controller the user picked
        by hand last time.
        """
        i = self.ecu_box.findText(name)
        if i < 0 or i == self.ecu_box.currentIndex():
            return False
        was, self._ready = self._ready, False
        try:
            self.ecu_box.setCurrentIndex(i)
        finally:
            self._ready = was
        return True

    def _remember_port(self, *_):
        from ..flash.gui_flash import _port_key
        self._last_port_key = _port_key(self.port_box.currentData())
        self.save_settings()

    def transport_kind(self):
        tb = getattr(self, "transport_box", None)
        return tb.currentData() if tb is not None else "kline"

    def is_can(self):
        return self.transport_kind() == "can"

    def _transport_changed(self, *_):
        kind = self.transport_kind()
        can = kind == "can"
        self.port_lbl.setText("Device:" if can else "Port:")
        self.can_rate.setVisible(can)
        # Which ECUs this transport can reach. The box used to be forced to
        # EDC16 and disabled on CAN, which is how every CAN log session ended
        # up opening TP2.0 to logical address 0x01 whatever the user wanted.
        allowed = CAN_ECUS if can else KLINE_ECUS
        want = self.ecu_box.currentText()
        self.ecu_box.blockSignals(True)
        self.ecu_box.clear()
        self.ecu_box.addItems(list(allowed))
        self.ecu_box.setCurrentText(want if want in allowed else allowed[0])
        self.ecu_box.blockSignals(False)
        self.ecu_box.setEnabled(True)
        self.fast_disable_chk.setVisible(not can and self.ecu() != "EDC15")  # K-line, not EDC15
        self.refresh_ports()                         # rebuilds ports -> _port_changed
        if hasattr(self, "speed_box"):
            self._ecu_changed()                      # signals were blocked above
        # The cable is per (controller, transport): switching to CAN should
        # bring back the J2534 device last used there, not leave the serial
        # port that K-line was on.
        self._restore_port_for(self.ecu(), kind)
        self.save_settings()

    def _port_changed(self, *_):
        self._remember_port()
        if not hasattr(self, "fast_disable_chk"):
            return
        self._apply_fast_init_default()
        self._rebuild_speeds()

    def _apply_fast_init_default(self):
        """Fast-init default follows the ECU (and cable): EDC15 uses 5-baud SLOW init
        on any cable; EDC16 uses fast init, and a J2534 device forces it."""
        if not hasattr(self, "fast_disable_chk") or self.is_can():
            return
        # EDC15 is always 5-baud slow init (KW1281 has no fast init, and an
        # EDC15 must not be sent one) -- no option shown, as in the flasher.
        self.fast_disable_chk.setVisible(self.ecu() != "EDC15")
        if self.ecu() == "EDC15":
            self.fast_disable_chk.setChecked(True)
            self.fast_disable_chk.setEnabled(False)
        elif is_j2534(self.port_box.currentData()):
            self.fast_disable_chk.setChecked(False)  # J2534 hardware fast init, required
            self.fast_disable_chk.setEnabled(False)
        else:
            self.fast_disable_chk.setChecked(False)  # EDC16 serial: fast init default
            self.fast_disable_chk.setEnabled(True)

    def is_tcu(self) -> bool:
        """True when the transmission controller is the logging target.

        Its measuring blocks are fixed by the controller software -- there is
        no block map in a .6hp calibration to read them from -- so the dialog
        names channels and plans presets from msblock6hp instead of from
        whatever file happens to be open."""
        return self.ecu() == "ZF6HP"

    def _ecu_changed(self):
        # A different controller has its own remembered cable, CAN rate and
        # speed -- switching EDC16 to ZF6HP should not keep the engine's.
        if not self._restoring:
            self._recall_module(QtCore.QSettings(*branding.QSETTINGS), self.ecu())
            self.save_settings()
        # The speed picker raises the K-LINE baud, so it belongs to neither
        # EDC15 (fixed KW1281 rate) nor anything reached over CAN.
        fixed = self.ecu() in ("EDC15", "ZF6HP", "ZF6HP-UDS", "Aisin TRx")
        self.speed_box.setVisible(not fixed and not self.is_can())
        self.speed_lbl.setVisible(not fixed and not self.is_can())
        self._rebuild_speeds()
        self._apply_fast_init_default()              # EDC15 -> slow init default

    def _rebuild_speeds(self):
        """Speed list for the current cable: drop High (244898) on a CH340/CH341 or
        J2534 cable (Medium-capped)."""
        labels, keys = list(SPEED_LABELS), list(SPEED_KEYS)
        if not cable_allows_high(self.port_box.currentData()):
            labels, keys = labels[:2], keys[:2]
        prev = self.speed_box.currentData()
        self.speed_box.blockSignals(True)
        self.speed_box.clear()
        for lbl, key in zip(labels, keys):
            self.speed_box.addItem(lbl, key)
        idx = next((i for i in range(self.speed_box.count())
                    if self.speed_box.itemData(i) == prev), 1 if len(keys) > 1 else 0)
        self.speed_box.setCurrentIndex(min(idx, self.speed_box.count() - 1))
        self.speed_box.blockSignals(False)

    def refresh_ports(self):
        from ..flash.gui_flash import _port_key, _can_devices
        prev = getattr(self, "_last_port_key", None) or _port_key(self.port_box.currentData())
        self.port_box.blockSignals(True)
        self.port_box.clear()
        if self.transport_kind() == "can":
            devs, empty = _can_devices(), "(no J2534 device or OpenPort found)"
        else:
            devs, empty = kline_devices(), "(no serial ports or J2534 devices found)"
        if not devs:
            hint = no_device_hint()
            self.port_box.addItem(empty[:-1] + " -- OpenPort: %s)" % hint if hint else empty, None)
        for data, label in devs:
            self.port_box.addItem(label, data)
        if prev is not None:
            for i in range(self.port_box.count()):
                if _port_key(self.port_box.itemData(i)) == prev:
                    self.port_box.setCurrentIndex(i)
                    break
        self.port_box.blockSignals(False)
        self._last_port_key = _port_key(self.port_box.currentData())
        self._port_changed()

    def port(self):
        return self.port_box.currentData()

    def ecu(self):
        return self.ecu_box.currentText()

    def can_dest(self):
        """TP2.0 logical address for the selected ECU.

        Read from the flash side's ``_CAN_DEST`` rather than copied, so the
        two cannot drift apart. 0x01 stays the fallback: it is the engine ECU
        and what every caller assumed before this existed.
        """
        from ..flash.gui_flash import _CAN_DEST
        return _CAN_DEST.get(self.ecu(), 0x01)

    def speed(self):
        return "Low" if self.ecu() == "EDC15" else (self.speed_box.currentData() or "Medium")

    def fast_init(self):
        """K-line fast init: always on for a J2534 device (hardware); the checkbox
        for a serial cable.  Meaningless over CAN."""
        if self.is_can() or self.ecu() == "EDC15":
            return False                 # EDC15: always 5-baud slow init
        return is_j2534(self.port()) or not self.fast_disable_chk.isChecked()

    def cap_high(self):
        p = self.port()
        if self.transport_kind() != "kline" or not isinstance(p, str):
            return False                             # ch34x quirk only applies to a serial cable
        return bool(p) and _diag.cable_is_ch34x(p)

    def set_enabled_all(self, on):
        for w in (self.port_box, self.ecu_box, self.speed_box, self.transport_box,
                  self.can_rate, self.fast_disable_chk):
            w.setEnabled(on)
        if on:
            self._transport_changed()

    #: calibration platform -> the entry in ecu_box that matches it. "hvac" is
    #: absent on purpose: the Climatronic is not a logging target here, so a
    #: session on one leaves whatever was restored alone.
    PLATFORM_ECU = {"zf6hp": "ZF6HP", "edc15": "EDC15", "edc16": "EDC16"}

    def default_from_platform(self, session) -> bool:
        """Point the ECU box at whatever the open calibration actually is.

        Returns True when it changed the selection. Falls back to the legacy
        ``is_edc15`` flag if ``platform`` is missing, so a restored snapshot
        that predates the attribute still gets the EDC15 default it used to.
        """
        plat = (getattr(session, "platform", None) or "").lower()
        name = self.PLATFORM_ECU.get(plat)
        if name is None and getattr(session, "is_edc15", False):
            name = "EDC15"
        if name is None or name == self.ecu_box.currentText():
            return False
        # (on the transport that controller was last used on -- a ZF6HP is
        # not even in the K-line list, so this used to leave it unselected)
        return self.select_module(name)

    # ---- persisted settings (shared by the logging + DTC dialogs) ----
    def _key(self, *parts):
        return "conn/%s/%s" % (self._scope, "/".join(str(x) for x in parts))

    def save_settings(self):
        """Remember the selections. Called on every change, not only on
        connect: a transport picked and then not connected was forgotten,
        which is most of why it never seemed to stick."""
        if self._restoring or not self._ready:
            return
        from ..flash.gui_flash import _port_key
        s = QtCore.QSettings(*branding.QSETTINGS)
        tr, ecu = self.transport_kind(), self.ecu()
        s.setValue(self._key("transport"), tr)
        s.setValue(self._key("ecu"), ecu)
        # The cable belongs to the (controller, transport) pair, not to the bar.
        key = _port_key(self.port_box.currentData())
        if key is not None:
            s.setValue(self._key("port", ecu, tr), repr(key))
        s.setValue(self._key("speed", ecu), self.speed_box.currentData() or "Medium")
        s.setValue(self._key("can_rate"), self.can_rate.currentData() or 500000)
        # and each controller's own: the engine on K-line and the gearbox on
        # CAN come back as they were, whichever was used last
        s.setValue(self._key("transport_of", ecu), tr)
        if tr == "can":
            s.setValue(self._key("can_rate_of", ecu),
                       self.can_rate.currentData() or 500000)
        s.sync()

    def select_module(self, name) -> bool:
        """Point this bar at controller ``name`` with ITS remembered setup:
        the transport it was last used on (or one that reaches it), its CAN
        rate, cable and K-line speed. True when it is selected."""
        s = QtCore.QSettings(*branding.QSETTINGS)
        reach = [t for t, lst in (("kline", KLINE_ECUS), ("can", CAN_ECUS))
                 if name in lst]
        if not reach:
            return False
        want_tr = s.value(self._key("transport_of", name))
        if want_tr not in reach:
            want_tr = self.transport_kind() if self.transport_kind() in reach \
                else reach[0]
        was = self._restoring
        self._restoring = True
        try:
            if want_tr != self.transport_kind():
                i = self.transport_box.findData(want_tr)
                if i >= 0:
                    self.transport_box.setCurrentIndex(i)   # rebuilds the list
            j = self.ecu_box.findText(name)
            if j < 0:
                return False
            if j != self.ecu_box.currentIndex():
                self.ecu_box.setCurrentIndex(j)
            self._recall_module(s, name)
        finally:
            self._restoring = was
        self.save_settings()
        return True

    def _recall_module(self, s, name):
        cr = s.value(self._key("can_rate_of", name))
        if cr not in (None, ""):
            try:
                j = self.can_rate.findData(int(cr))
            except (TypeError, ValueError):
                j = -1
            if j >= 0:
                self.can_rate.setCurrentIndex(j)
        sp = s.value(self._key("speed", name))
        if sp:
            j = self.speed_box.findData(sp)
            if j >= 0:
                self.speed_box.setCurrentIndex(j)
        self._restore_port_for(name, self.transport_kind())

    def _restore_port_for(self, ecu, transport):
        """Select the cable last used for this controller on this transport."""
        from ..flash.gui_flash import _port_key
        s = QtCore.QSettings(*branding.QSETTINGS)
        want = s.value(self._key("port", ecu, transport))
        if not want:
            return False
        for i in range(self.port_box.count()):
            if repr(_port_key(self.port_box.itemData(i))) == want:
                if i != self.port_box.currentIndex():
                    self.port_box.setCurrentIndex(i)
                self._last_port_key = _port_key(self.port_box.currentData())
                return True
        return False

    def restore_settings(self):
        s = QtCore.QSettings(*branding.QSETTINGS)
        self._migrate_flat_settings(s)
        tr = s.value(self._key("transport"))
        ecu = s.value(self._key("ecu"))
        cr = s.value(self._key("can_rate"))
        self._restoring = True
        try:
            # Transport FIRST: it rebuilds the controller list and the port
            # list, so choosing it afterwards would discard both.
            if tr in ("kline", "can"):
                i = self.transport_box.findData(tr)
                if i >= 0:
                    self.transport_box.setCurrentIndex(i)
            if ecu and self.ecu_box.findText(ecu) >= 0:
                self.ecu_box.setCurrentText(ecu)
            if cr not in (None, ""):
                try:
                    j = self.can_rate.findData(int(cr))
                except (TypeError, ValueError):
                    j = -1
                if j >= 0:
                    self.can_rate.setCurrentIndex(j)
            sp = s.value(self._key("speed", self.ecu()))
            if sp:
                j = self.speed_box.findData(sp)
                if j >= 0:
                    self.speed_box.setCurrentIndex(j)
            self._restore_port_for(self.ecu(), self.transport_kind())
        finally:
            self._restoring = False
        # fast/slow init is derived from the ECU (EDC15 -> slow), not restored

    def _migrate_flat_settings(self, s):
        """Carry the old single flat ``conn/*`` group into this scope once, so
        an upgrade does not lose the cable the user had selected."""
        if s.value(self._key("migrated")):
            return
        s.setValue(self._key("migrated"), True)
        tr = s.value("conn/transport")
        ecu = s.value("conn/ecu")
        port = s.value("conn/port")
        if tr in ("kline", "can") and not s.value(self._key("transport")):
            s.setValue(self._key("transport"), tr)
        if ecu and not s.value(self._key("ecu")):
            s.setValue(self._key("ecu"), ecu)
        if port and ecu and tr and not s.value(self._key("port", ecu, tr)):
            s.setValue(self._key("port", ecu, tr), port)
        for k in ("speed", "can_rate"):
            v = s.value("conn/%s" % k)
            if v in (None, ""):
                continue
            if k == "can_rate":
                if not s.value(self._key("can_rate")):
                    s.setValue(self._key("can_rate"), v)
            elif ecu and not s.value(self._key("speed", ecu)):
                s.setValue(self._key("speed", ecu), v)


# ----------------------------------------------------------------- dialog
# ------------------------------------------------- one controller's own side
class ConnPane(QtWidgets.QWidget):
    """Everything that belongs to ONE controller: its cable and transport, its
    measuring-block map, the cells picked for logging, the worker and the file.

    Two of these run side by side for dual logging. They share nothing: the
    engine on K-line and the transmission on CAN are separate physical layers,
    so neither contends with the other and each keeps its own sample rate. The
    only shared things are the status line and the log viewer, which the dialog
    owns.

    Each pane binds to the workspace tab holding ITS controller's binary rather
    than to whichever tab happens to be in front -- see :meth:`_workspace`.
    """

    #: status text for the dialog's shared line, already labelled
    status_msg = QtCore.Signal(str)
    #: a finished log file, for the viewer tab
    log_finished = QtCore.Signal(str)
    #: connected / logging state changed, so the master buttons can follow
    state_changed = QtCore.Signal()

    #: ConnectionBar's controller names to ``TunerSession.platform``
    PLATFORM_OF_ECU = {"ZF6HP": "zf6hp", "EDC15": "edc15", "EDC16": "edc16"}

    #: every live pane, so default log names never collide between them
    _panes_alive = weakref.WeakSet()

    def __init__(self, window, default_ecu=None, parent=None, scope="a"):
        super().__init__(parent)
        ConnPane._panes_alive.add(self)
        self._window = window
        self._scope = scope
        self._worker: Optional[LogWorker] = None
        self._channels: List[_lf.Channel] = []
        # The recording in progress: set by Start, cleared the moment it is
        # stopped -- so a session that ends later (Disconnect after Stop)
        # does not hand the same log on a second time.
        self._log_path = ""
        self._rec_gen = 0               # LogWorker generation of that recording
        self._t0 = None
        self._live = _lf.DataLog()
        self._logging = False           # real state, not read back off a button
        self._mem_state = None          # what the 0x23 probe said, once connected
        #: controller -> the identity it reported on its last connect. Kept
        #: after Disconnect so the cells stay named; with no binary open, it
        #: is what the measuring-block catalogue is looked up by.
        self._ecu_ident: dict = {}

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.conn = ConnectionBar(window, scope=scope)
        if default_ecu:
            # Quietly: this is the dialog pairing the two panes, not the user
            # choosing, so it must not overwrite the remembered controller.
            self.conn.set_ecu_quietly(default_ecu)
        v.addWidget(self.conn)
        row = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        row.addWidget(self.connect_btn)
        self.ident = QtWidgets.QLabel("Not connected.")
        self.ident.setStyleSheet("color: gray")
        row.addWidget(self.ident, 1)
        v.addLayout(row)

        self._namer = self._namer_for()
        self._namer_sig_cur = self._namer_sig()
        v.addWidget(self._build_body(), 1)
        self.conn.ecu_box.currentIndexChanged.connect(lambda *_: self._sync_namer())
        # EDC16 <-> EDC15 can keep the same namer key, so the presets follow
        # the controller directly.
        self.conn.ecu_box.currentIndexChanged.connect(
            lambda *_: (self._fill_presets(), self._say_presets()))
        self._sync_namer()

    # ---- identity -----------------------------------------------------
    def title(self) -> str:
        """The tab label: the controller selected right now, so the tab says
        what it is without being opened."""
        return self.conn.ecu() or "Controller"

    def is_connected(self) -> bool:
        return self._worker is not None

    def is_logging(self) -> bool:
        """Whether this connection is writing rows.

        Was derived from ``stop_btn.isEnabled()``, which is a view of the
        state and not the state: ``_set_logging_ui`` flips the button before
        the rest of the stop has run, so a master Stop iterating the panes saw
        the first one as already stopped while it still held an open file."""
        return self._worker is not None and self._logging

    def _say(self, msg):
        self.status_msg.emit(msg)

    # ---- which binary names the cells ---------------------------------
    def _workspace(self):
        """The open tab whose binary belongs to THIS pane's controller.

        The old single-connection dialog followed ``window.primary`` -- the tab
        in front -- which was right when there was one connection and wrong as
        soon as there are two: the transmission pane would rename its cells
        from the engine file merely because that tab was selected. Matching on
        ``platform`` binds each pane to its own file instead, and it is also
        what names the log (one file per binary, as asked).
        """
        want = self.PLATFORM_OF_ECU.get(self.conn.ecu())
        try:
            spaces = self._window.workspaces()
        except Exception:
            spaces = []
        for ws in spaces:
            prim = getattr(ws, "primary", None)
            plat = (getattr(prim, "platform", None) or "").lower() if prim is not None else ""
            if want and plat == want:
                return ws
        # No tab holds that controller's file: fall back to the active tab, so
        # hand-picked cells and manual group numbers still work unnamed.
        return getattr(self._window, "current", None)

    def _primary(self):
        ws = self._workspace()
        if ws is not None:
            return getattr(ws, "primary", None)
        return getattr(self._window, "primary", None)

    def _is_tcu(self) -> bool:
        return bool(getattr(self, "conn", None)) and self.conn.is_tcu()

    def _recipes(self):
        """The preset list for the selected controller."""
        from ..logtune.roles import RECIPES, TCU_RECIPES
        ecu = self.conn.ecu()
        if ecu == "ZF6HP":
            return TCU_RECIPES
        if ecu == "EDC16":
            return RECIPES
        if ecu == "EDC15":
            # the engine recipes, where this ECU's blocks carry their needs
            # (message-number keys -- see edc15.msblock15.MESSAGE_KEYS)
            from ..logtune.roles import recipes_for
            return recipes_for(getattr(self, "_namer", None), RECIPES)
        return []                        # see _no_presets_why

    def _no_presets_why(self) -> str:
        """Why the selected controller has no presets ("" when it has some).
        Presets are planned from the signal identities the namer resolves; the
        engine recipes are EDC16 A2L signals, which no other controller carries."""
        ecu = self.conn.ecu()
        if ecu == "EDC15":
            if self._recipes():
                return ""
            why = ("none of its named cells carries what the engine presets need "
                   "(engine speed, injected quantity and the like are keyed on the "
                   "ECU's message numbers, so open the car's file or connect to "
                   "name them)")
        elif ecu == "ZF6HP-UDS":
            why = "no preset has been written for the 6HP28 DIDs yet"
        elif ecu == "Aisin TRx":
            why = "its measuring-block map is not known yet, so its cells are unnamed"
        else:
            return ""
        return "No presets for %s: %s. Tick the cells to log by hand." % (ecu, why)

    def _namer_for(self):
        if self._is_tcu():
            from .msblock6hp import Blocks6hp
            return Blocks6hp()
        if self.conn.ecu() == "ZF6HP-UDS":
            from .zf6hp_uds_live import Blocks6hpUds
            return Blocks6hpUds()
        if self.conn.ecu() == "Aisin TRx":
            # No 09D measuring-block map exists in the binary yet (the firmware
            # descriptor table still has to be located), so name columns
            # generically ("Group N / Cell M") rather than off whatever
            # unrelated calibration happens to be open. A ChannelNamer with no
            # session reports available=False, which gives exactly that.
            return ChannelNamer(None)
        self._binary_note = ""
        ecu = self.conn.ecu()
        prim = self._primary()
        plat = _binary_platform(prim)
        n = ChannelNamer(None)       # nothing names the cells: "Group N / Cell M"
        if plat is not None and plat != ecu:
            # an EDC15 file cannot name EDC16 cells, nor the reverse: the
            # group numbers mean different things on the two
            self._binary_note = ("The open file is an %s binary, so it does not name "
                                 "this %s's cells." % (plat, ecu))
        elif plat is not None:
            ident = self._ecu_identity()
            n = ChannelNamer(prim, identity=ident if ecu == "EDC15" else "")
            if n.available:
                self._binary_note = _identity_note(prim, ecu, ident, n)
                return n                 # the binary always wins
        # No binary that can name these blocks: the catalogue -- the entry for
        # the software the ECU reported, else its part number's, else the
        # platform's generic layout (also before connecting)
        cat = _msbcat.namer_for(self.conn.ecu(), self._ecu_identity())
        return cat if cat is not None else n

    def _ecu_identity(self) -> str:
        """What the selected controller reported on its last connect ("")."""
        return self._ecu_ident.get(self.conn.ecu(), "")

    def _namer_sig(self):
        """What the namer depends on, so a change rebuilds it and nothing else
        does. A fixed-catalogue controller (6HP19, 6HP28, Aisin) keys on the
        controller alone; a binary-named one keys on which binary is in front."""
        ecu = self.conn.ecu()
        if ecu in ("ZF6HP", "ZF6HP-UDS", "Aisin TRx"):
            return (ecu,)
        # ...and on the reported identity, which names them when no binary can
        return ("binary", id(self._primary()), ecu, self._ecu_identity())

    def _sync_namer(self):
        """Follow this pane's own controller and binary. Not while logging:
        the running log's columns were named from the binary it started with.
        Connected but not recording is fine -- that is when the identity
        arrives, and the cells are only handed over at Start."""
        if self._logging:
            return False
        want = self._namer_sig()
        prev = getattr(self, "_namer_sig_cur", None)
        if want == prev:
            return False
        self._namer = self._namer_for()
        self._namer_sig_cur = want
        # the two must never disagree -- and EDC15's presets depend on what
        # this namer's cells carry, so refill on every namer change
        self._fill_presets()
        self._fill_groups()
        self._channels = [c if c.is_derived else self._namer.channel(c.group, c.field)
                          for c in self._channels]
        self._refresh_sel()
        self._fill_browse(self._current_group(), None)
        self._set_default_path()
        self.state_changed.emit()        # the tab label follows the controller
        return True

    # ---- the body -----------------------------------------------------
    def _build_body(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # browse
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        gr = QtWidgets.QHBoxLayout()
        gr.addWidget(QtWidgets.QLabel("Group:"))
        self.group_box = QtWidgets.QComboBox()
        self.group_box.setEditable(False)
        gr.addWidget(self.group_box, 1)
        self.group_spin = QtWidgets.QSpinBox()
        self.group_spin.setRange(1, 255)
        self.group_spin.setToolTip("Any group number, including ones the binary lists as empty")
        gr.addWidget(self.group_spin)
        lv.addLayout(gr)
        self.browse = QtWidgets.QTableWidget(0, 3)
        self.browse.setHorizontalHeaderLabels(["Log", "Cell", "Value"])
        self.browse.verticalHeader().setVisible(False)
        self.browse.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.browse.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lv.addWidget(self.browse, 1)
        self.src_hint = QtWidgets.QLabel("")
        self.src_hint.setStyleSheet("color: gray")
        self.src_hint.setWordWrap(True)
        lv.addWidget(self.src_hint)
        split.addWidget(left)
        # Every widget the fill touches now exists -- _fill_groups ends in
        # _update_src_hint, which needs src_hint above. Signals are connected
        # only after the fill, so populating the combo does not fire
        # _group_changed into a half-built pane.
        self._fill_groups()
        self.group_spin.setValue(self._current_group())
        self._fill_browse(self._current_group(), None)   # the first group's cells, on open
        self.group_box.currentIndexChanged.connect(self._group_changed)
        self.group_spin.valueChanged.connect(self._group_spun)
        self.browse.itemChanged.connect(self._browse_checked)

        # selection + logging
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        pr = QtWidgets.QHBoxLayout()
        pr.addWidget(QtWidgets.QLabel("Preset:"))
        self.preset_box = QtWidgets.QComboBox()
        self._fill_presets()
        self.preset_box.setToolTip("Tick the fewest measuring blocks in THIS calibration that "
                                   "carry what Tools > Tune from logs needs for that analysis.")
        self.preset_box.activated.connect(self._apply_preset)
        pr.addWidget(self.preset_box, 1)
        rv.addLayout(pr)
        self.preset_hint = QtWidgets.QLabel("")
        self.preset_hint.setWordWrap(True)
        self.preset_hint.setStyleSheet("color: gray")
        self.preset_hint.setText(self._no_presets_why())
        rv.addWidget(self.preset_hint)
        rv.addWidget(QtWidgets.QLabel("Selected cells:"))
        self.sel = QtWidgets.QTableWidget(0, 3)
        self.sel.setHorizontalHeaderLabels(["Cell", "Signal", "Value"])
        self.sel.verticalHeader().setVisible(False)
        self.sel.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.sel.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.sel.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        rv.addWidget(self.sel, 1)
        sb = QtWidgets.QHBoxLayout()
        rm = QtWidgets.QPushButton("Remove")
        rm.clicked.connect(self._remove_selected)
        sb.addWidget(rm)
        cl = QtWidgets.QPushButton("Clear")
        cl.clicked.connect(self._clear_selected)
        sb.addWidget(cl)
        sb.addStretch(1)
        rv.addLayout(sb)
        # where the log goes: into the project of this pane's binary by
        # default (exported from the Project panel when a file is wanted)
        self.keep_in_project = QtWidgets.QCheckBox()
        self.keep_in_project.setChecked(True)
        self.keep_in_project.setVisible(False)
        self.keep_in_project.toggled.connect(self._dest_changed)
        rv.addWidget(self.keep_in_project)
        fr = QtWidgets.QHBoxLayout()
        self.path_label = QtWidgets.QLabel("Log file:")
        fr.addWidget(self.path_label)
        self.path_edit = QtWidgets.QLineEdit()
        fr.addWidget(self.path_edit, 1)
        br = self.path_btn = QtWidgets.QPushButton("...")
        br.setMaximumWidth(32)
        br.clicked.connect(self._pick_path)
        fr.addWidget(br)
        rv.addLayout(fr)
        cb = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start logging")
        self.start_btn.clicked.connect(self._start)
        cb.addWidget(self.start_btn)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause)
        cb.addWidget(self.pause_btn)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_logging)
        cb.addWidget(self.stop_btn)
        cb.addStretch(1)
        self.rate = QtWidgets.QLabel("")
        cb.addWidget(self.rate)
        rv.addLayout(cb)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)

        self.chart = StripChart()
        self.chart.window = 30.0
        v.addWidget(self.chart)
        self._set_logging_ui(False)
        self._set_default_path()
        return w

    def _fill_presets(self):
        keep = self.preset_box.currentData()
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        self.preset_box.addItem("(choose cells by hand)", None)
        try:
            for rec in self._recipes():
                self.preset_box.addItem(rec.title, rec.key)
        except Exception:
            pass
        idx = next((i for i in range(self.preset_box.count())
                    if self.preset_box.itemData(i) == keep), 0)
        self.preset_box.setCurrentIndex(idx)
        self.preset_box.blockSignals(False)
        self.preset_box.setEnabled(not self._no_presets_why())

    def _say_presets(self):
        """After a controller change: say why there are no presets, or drop
        a stale hint when nothing is chosen."""
        why = self._no_presets_why()
        if why or self.preset_box.currentIndex() == 0:
            self.preset_hint.setText(why)

    def _fill_groups(self):
        """(Re)list the groups this pane's binary knows, keeping the selected
        group when it is still there."""
        keep = self.group_box.currentData() if self.group_box.count() else None
        # EDC16 serves groups 1..127 only (rows above are their cells 5-8)
        top = 128 if self.conn.ecu() == "EDC16" else 256
        groups = self._namer.populated_groups() if self._namer.available else list(range(1, top))
        if not groups:
            groups = list(range(1, top))
        self.group_box.blockSignals(True)
        self.group_box.clear()
        label = getattr(self._namer, "group_label", None) if self._namer.available else None
        for g in groups:
            if label is not None:           # e.g. the 6HP28's hex DIDs
                self.group_box.addItem(label(g), g)
                continue
            summ = self._namer.group_summary(g) if self._namer.available else ""
            self.group_box.addItem(("%d - %s" % (g, summ)) if summ else "Group %d" % g, g)
        i = self.group_box.findData(keep) if keep is not None else -1
        self.group_box.setCurrentIndex(i if i >= 0 else 0)
        self.group_box.blockSignals(False)
        self._update_src_hint()

    def _update_src_hint(self):
        """Say which file is naming the cells. With two panes open, 'the loaded
        binary' is ambiguous and the wrong one is a silent mis-label."""
        prim = self._primary()
        name = getattr(getattr(prim, "binfile", None), "name", "")
        if self._is_tcu():
            self.src_hint.setText("Tick a cell to log it. Transmission cells are named from "
                                  "the 6HP block catalogue.")
        elif self.conn.ecu() == "ZF6HP-UDS":
            self.src_hint.setText("Tick a cell to log it. 6HP28 values are UDS DIDs from the "
                                  "transmission's own DID table: the known signals are "
                                  "listed first; unnamed ones are raw counts.")
        elif isinstance(self._namer, _msbcat.CatalogueNamer):
            n = self._namer
            if n.match == "exact":
                tail = "if this car's measuring blocks were edited, open its file to name them from it"
            else:
                tail = "names can differ on this software, so open the car's file to name them exactly"
            note = getattr(self, "_binary_note", "")
            self.src_hint.setText("Tick a cell to log it. Cells named from %s; %s.%s"
                                  % (n.describe(), tail, (" " + note) if note else ""))
        elif name and getattr(self._namer, "available", False):
            note = getattr(self, "_binary_note", "")
            ds = getattr(self._namer, "dataset", None)
            self.src_hint.setText("Tick a cell to log it. Cells named from %s%s.%s"
                                  % (name, (" (dataset %s)" % ds) if ds else "",
                                     (" " + note) if note else ""))
        else:
            where = ("with Open project or binary..." if getattr(self._window, "standalone", False)
                     else "in a tab")
            if self.conn.ecu() in _msbcat.PLATFORMS:
                # (with the catalogue installed the namer is never this one)
                self.src_hint.setText("Tick a cell to log it. Cells are unnamed: no "
                                      "measuring-block map for this controller is open and "
                                      "the %s module's measuring-block catalogue is not "
                                      "installed. Open the car's file %s to name them."
                                      % (self.conn.ecu(), where))
            else:
                self.src_hint.setText("Tick a cell to log it. No measuring-block map for this "
                                      "controller is open, so cells are unnamed; open the "
                                      "car's file %s to name them." % where)

    # ---- connection ---------------------------------------------------
    def _toggle_connect(self):
        if self._worker is not None:
            self.disconnect_now()
            return
        self.connect_now()

    def connect_now(self, quiet=False):
        """Open the session. ``quiet`` suppresses the pop-up, for the master
        button: with two panes a modal per pane would stack up."""
        if self._worker is not None:
            return True
        port = self.conn.port()
        can = self.conn.is_can()
        if port is None:
            why = ("Select a J2534 device for %s." if can else
                   "Select a K-line cable or J2534 device for %s.") % self.title()
            if quiet:
                self._say(why)
            else:
                QtWidgets.QMessageBox.warning(self, "Data logging", why)
            return False
        self.conn.save_settings()
        self._worker = LogWorker(
            port, self.conn.ecu(), self.conn.speed(), self.conn.cap_high(),
            transport=("can" if can else "kline"),
            can_rate=self.conn.can_rate.currentData() or 500000,
            can_dest=self.conn.can_dest(),
            fast_init=self.conn.fast_init())
        self._worker.connected.connect(self._on_connected)
        self._worker.browsed.connect(self._on_browsed)
        self._worker.logged.connect(self._on_logged)
        self._worker.status.connect(self._say)
        self._worker.failed.connect(self._on_failed)
        self._worker.ended.connect(self._on_ended)
        self._worker.mem_probed.connect(self._on_mem_probed)
        self._worker.set_browse_group(self._current_group())
        self.connect_btn.setText("Connecting...")
        self.connect_btn.setEnabled(False)
        self.conn.set_enabled_all(False)
        self._worker.start()
        self.state_changed.emit()
        return True

    def disconnect_now(self):
        if self._worker is not None:
            w = self._worker
            already = w._stop
            w.stop()
            self.connect_btn.setText("Disconnecting...")
            self.connect_btn.setEnabled(False)
            self.state_changed.emit()
            if not already:
                # The worker winds down by itself within a couple of seconds;
                # a call stuck in the cable's driver is cut after the grace.
                QtCore.QTimer.singleShot(DISCONNECT_GRACE_MS, self,
                                         lambda w=w: self._disconnect_overdue(w))

    def _disconnect_overdue(self, w):
        """Disconnect's watchdog: the worker is still running after the grace
        period, so cut the transport under it (see LogWorker.abort_transport).
        Only Disconnect and closing the window arm this -- never Stop."""
        if not w.isRunning():
            return
        how = w.abort_transport()
        if how == "bridge":
            self._say("The J2534 device did not let go; its bridge was closed.")
        elif how == "serial":
            self._say("The K-line cable did not let go; its read was cancelled.")
        else:
            self._say("Still waiting for the J2534 driver to close the device...")

    def _on_connected(self, identity, baud):
        self.ident.setText("%s   (%d baud)" % (identity or "connected", baud))
        if identity:
            self._ecu_ident[self.conn.ecu()] = identity
            self._sync_namer()           # names from the catalogue when no binary can
            self._update_src_hint()
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)
        self._set_logging_ui(False)
        self.state_changed.emit()

    def _on_failed(self, msg):
        QtWidgets.QMessageBox.critical(self, "Data logging", "%s: %s" % (self.title(), msg))

    def _on_ended(self):
        retire_thread(self._worker)     # never destroy a still-running QThread
        self._worker = None
        self._logging = False
        self.connect_btn.setText("Connect")
        self.connect_btn.setEnabled(True)
        self.conn.set_enabled_all(True)
        self.ident.setText("Not connected.")
        self._set_logging_ui(False, connected=False)
        # Only a recording the session ended in the middle of: one already
        # stopped was handed on then, and _log_path was cleared with it.
        path, self._log_path = self._log_path, ""
        if path and os.path.exists(path):
            self._finish_log(path, self._live.rows, ended=True)
        self.state_changed.emit()

    def request_stop(self):
        """Ask the worker to wind up, without waiting for it."""
        self._logging = False
        if self._worker is not None:
            self._worker.stop()

    def shutdown(self):
        """Stop the worker for good; the dialog is closing (or the dual pane
        is being taken away)."""
        ConnPane.wind_down([self])

    @staticmethod
    def wind_down(panes):
        """Disconnect every pane's worker for good, in bounded time.

        All are asked first, then waited on against ONE shared deadline
        (CLOSE_WAIT_S) -- not five seconds each, one after the other. Whatever
        is still running then has its transport cut (LogWorker.abort_transport)
        and gets ABORT_WAIT_S more; anything left after that (a call inside an
        in-process J2534 DLL) is handed to the reaper and finishes on its own."""
        panes = [p for p in panes if getattr(p, "_worker", None) is not None]
        for p in panes:
            p.request_stop()
        deadline = time.monotonic() + CLOSE_WAIT_S
        for p in panes:
            p._wait_worker(deadline)
        late = [p for p in panes if p._worker.isRunning()]
        for p in late:
            p._abort_worker()
        deadline = time.monotonic() + ABORT_WAIT_S
        for p in late:
            p._wait_worker(deadline)
        for p in panes:
            retire_thread(p._worker)    # never destroy a still-running QThread
            p._worker = None

    def _wait_worker(self, deadline):
        """Wait for the worker until ``deadline`` (time.monotonic)."""
        w = self._worker
        if w is not None and w.isRunning():
            w.wait(max(0, int((deadline - time.monotonic()) * 1000)))

    def _abort_worker(self):
        w = self._worker
        if w is not None and w.isRunning():
            w.abort_transport()

    # ---- browsing -----------------------------------------------------
    def _current_group(self):
        g = self.group_box.currentData()
        return int(g) if g is not None else self.group_spin.value()

    def _group_changed(self):
        g = self._current_group()
        self.group_spin.blockSignals(True)
        self.group_spin.setValue(g)
        self.group_spin.blockSignals(False)
        self._fill_browse(g, None)
        if self._worker is not None:
            self._worker.set_browse_group(g)

    def _group_spun(self, g):
        i = self.group_box.findData(g)
        self.group_box.blockSignals(True)
        self.group_box.setCurrentIndex(i)
        self.group_box.blockSignals(False)
        self._fill_browse(g, None)
        if self._worker is not None:
            self._worker.set_browse_group(g)

    def _fill_browse(self, group, cells):
        """cells: [(field, text, value)] from the ECU, or None (binary only)."""
        self.browse.blockSignals(True)
        n = len(cells) if cells else (self._namer.slots(group) if self._namer.available else 4)
        self.browse.setRowCount(n)
        selected = {c.key for c in self._channels}
        for f in range(n):
            ch = self._namer.channel(group, f)
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            chk.setCheckState(QtCore.Qt.Checked if ch.key in selected else QtCore.Qt.Unchecked)
            chk.setData(QtCore.Qt.UserRole, (group, f))
            self.browse.setItem(f, 0, chk)
            self.browse.setItem(f, 1, QtWidgets.QTableWidgetItem(ch.title))
            val = ""
            if cells:
                for ff, text, _v in cells:
                    if ff == f:
                        val = text
            self.browse.setItem(f, 2, QtWidgets.QTableWidgetItem(val))
        self.browse.blockSignals(False)

    def _on_browsed(self, group, cells):
        if group != self._current_group():
            return
        if not cells:
            self._say("Group %d: not available on this ECU." % group)
            return
        if self.browse.rowCount() != len(cells):
            self._fill_browse(group, cells)
            return
        for f, text, _v in cells:
            it = self.browse.item(f, 2)
            if it is not None:
                it.setText(text)

    def _browse_checked(self, item):
        if item.column() != 0:
            return
        group, f = item.data(QtCore.Qt.UserRole)
        if item.checkState() == QtCore.Qt.Checked:
            if not any(c.key == (group, f) for c in self._channels):
                self._channels.append(self._namer.channel(group, f))
                if _derived.can_add_eoi(self._channels):
                    # both start and duration ticked: log the end of injection
                    # too (Remove takes it out again)
                    self._channels.append(_derived.eoi_channel())
        else:
            self._channels = [c for c in self._channels if c.key != (group, f)]
            if None in _derived.sources(self._channels):
                # its start or duration cell went: nothing to work it out from
                self._channels = [c for c in self._channels
                                  if c.derived != _derived.EOI]
        self._refresh_sel()

    # ---- presets ------------------------------------------------------
    def _apply_preset(self, *_):
        key = self.preset_box.currentData()
        if not key:
            self.preset_hint.setText("")
            return
        from ..logtune.roles import recipe_groups
        recipes = self._recipes()
        rec = next(r for r in recipes if r.key == key)
        self._sync_namer()
        if not self._namer.available:
            prim = self._primary()
            why = ("no file is open for %s" % self.title() if prim is None else
                   "%s has no measuring-block map %s can read"
                   % (getattr(getattr(prim, "binfile", None), "name", "the current file"),
                      branding.APP_NAME))
            self.preset_hint.setText("No cells: %s. Presets are resolved from the controller's "
                                     "own calibration, so open that file in a tab and pick the "
                                     "preset again." % why)
            return
        from .served import unserved_for
        avoid = unserved_for(self._namer)
        groups, missing = recipe_groups(self._namer, rec, rec.max_groups, avoid=avoid)
        self._channels = [self._namer.channel(g, f) for g in groups
                          for f in range(self._namer.slots(g))]
        if {"soi", "dur"} <= set(rec.needs) | set(rec.wants) and \
                _derived.can_add_eoi(self._channels):
            # end of injection, worked out from the row's SOI and duration
            self._channels.append(_derived.eoi_channel())
        self._refresh_sel()
        self._fill_browse(self._current_group(), None)
        text = "Blocks %s. %s" % (", ".join(map(str, groups)) or "-", rec.how)
        if missing:
            from ..logtune.roles import ROLE_LABELS
            text = ("Not in any block this ECU serves: %s. "
                    % ", ".join(ROLE_LABELS.get(r, r) for r in missing)) + text
        self.preset_hint.setText(text)
        path = self.path_edit.text().strip()
        if path:
            d, b = os.path.split(path)
            stem, ext = os.path.splitext(b)
            for r in recipes:                       # swap, don't stack, preset tags
                if stem.endswith("-" + r.key):
                    stem = stem[:-len(r.key) - 1]
            self.path_edit.setText(os.path.join(d, stem + "-" + key + (ext or ".csv")))

    def _on_mem_probed(self, works, why):
        """Say so once, on connect, rather than after a drive of N/A columns."""
        self._mem_state = why
        self._say(why)
        if not works and any(c.is_memory for c in self._channels):
            QtWidgets.QMessageBox.warning(
                self, "Data logging",
                "%s\n\nThe AST channels will log as N/A. Everything else still "
                "works." % why)

    def _refresh_sel(self):
        self.sel.setRowCount(len(self._channels))
        for r, c in enumerate(self._channels):
            self.sel.setItem(r, 0, QtWidgets.QTableWidgetItem(
                "calc" if c.is_derived else "G%d/%d" % (c.group, c.field + 1)))
            self.sel.setItem(r, 1, QtWidgets.QTableWidgetItem(c.title))
            self.sel.setItem(r, 2, QtWidgets.QTableWidgetItem(""))
        self.sel.resizeColumnsToContents()
        self.sel.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.sel.selectedItems()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self._channels):
                del self._channels[r]
        self._refresh_sel()
        self._fill_browse(self._current_group(), None)

    def _clear_selected(self):
        self._channels = []
        self._refresh_sel()
        self._fill_browse(self._current_group(), None)

    # ---- logging ------------------------------------------------------
    def _project_ref(self):
        """The project of the tab this pane's binary is open in, if any."""
        ws = self._workspace()
        return getattr(ws, "project_ref", None), ws

    def _sync_destination(self):
        """Offer "Keep in project" when this pane's binary is in a project."""
        ref, _ws = self._project_ref()
        if ref is None:
            self.keep_in_project.setVisible(False)
        else:
            self.keep_in_project.setText(
                "Keep the log in the project %s (%s) -- export it from the "
                "Project panel" % (ref.doc.title, ref.set.name))
            self.keep_in_project.setVisible(True)
        self._dest_changed()

    def _into_project(self) -> bool:
        # isHidden, not isVisible: this also runs before the dialog is shown
        return not self.keep_in_project.isHidden() and \
            self.keep_in_project.isChecked() and self._project_ref()[0] is not None

    def _dest_changed(self, *_a):
        to_file = not self._into_project()
        for w in (self.path_label, self.path_edit, self.path_btn):
            w.setEnabled(to_file)

    def _set_default_path(self):
        """Name the file after THIS pane's binary, so two concurrent logs never
        land on the same path."""
        from .. import recent as _recent
        ident = ""
        prim = self._primary()
        # Only a binary of THIS controller names the file: _primary() falls
        # back to the tab in front, whose name belongs to another controller.
        want = self.PLATFORM_OF_ECU.get(self.conn.ecu())
        plat = (getattr(prim, "platform", None) or "").lower() if prim is not None else ""
        if prim is not None and want and plat == want:
            ref = self._project_ref()[0]
            if ref is not None:
                # a project's binary: "<project> - <set>" (its label is
                # "<project> ▸ <set> · <branch> (<version>)", not a file name)
                ident = "%s - %s" % (ref.doc.title, ref.set.name)
            else:
                ident = os.path.splitext(prim.binfile.name)[0].replace(
                    " (unsaved)", "")
        if not ident:
            ident = (self.conn.ecu() or "").lower()
        path = os.path.join(_recent.get_last_dir("log"), _lf.default_name(ident))
        # Two panes named in the same second (or the same controller twice)
        # must still get two files.
        taken = {p.path_edit.text() for p in list(ConnPane._panes_alive)
                 if p is not self and hasattr(p, "path_edit")}
        stem, ext = os.path.splitext(path)
        n = 2
        while path in taken or os.path.exists(path):
            path = "%s-%d%s" % (stem, n, ext)
            n += 1
        self.path_edit.setText(path)
        self._sync_destination()

    def _pick_path(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Log file for %s" % self.title(), self.path_edit.text(),
            "CSV data log (*.csv);;All files (*)")
        if path:
            if not os.path.splitext(path)[1]:
                path += ".csv"
            self.path_edit.setText(path)

    def _set_logging_ui(self, logging_, connected=True):
        self.start_btn.setEnabled(connected and not logging_)
        self.pause_btn.setEnabled(logging_)
        self.stop_btn.setEnabled(logging_)
        if not logging_:
            self.pause_btn.setChecked(False)
        self.state_changed.emit()

    def _start(self, quiet=False):
        if self._worker is None:
            return False
        if not self._channels:
            why = "Tick at least one cell to log for %s." % self.title()
            if quiet:
                self._say(why)
            else:
                QtWidgets.QMessageBox.information(self, "Data logging", why)
            return False
        self._sync_destination()
        self._keep_ref = None
        path = self.path_edit.text().strip()
        if self._into_project():
            # recorded to a working copy; it goes into the project on Stop
            ref, ws = self._project_ref()
            work = os.path.join(ref.doc.tmpdir, "logs-live")
            os.makedirs(work, exist_ok=True)
            path = os.path.join(work, os.path.basename(path)
                                or _lf.default_name(self.title()))
            self._keep_ref = (ws, path)
        elif not path:
            self._pick_path()
            path = self.path_edit.text().strip()
            if not path:
                return False
        try:
            self._rec_gen = self._worker.start_logging(self._channels, path)
        except OSError as ex:
            QtWidgets.QMessageBox.critical(self, "Data logging",
                                           "Cannot write %s: %s" % (path, ex))
            return False
        from .. import recent as _recent
        if self._keep_ref is None:
            _recent.set_last_dir(os.path.dirname(path), "log")
        self._log_path = path
        self._t0 = None
        self._live = _lf.DataLog(channels=[_lf.Channel(c.group, c.field, c.label, c.unit, c.signal,
                                                       derived=c.derived)
                                           for c in self._channels])
        self.chart.set_data([], [[] for _ in self._channels],
                            [c.title for c in self._channels], [c.unit for c in self._channels])
        self._logging = True
        self._set_logging_ui(True)
        self._say("Logging to %s" % path)
        return True

    def _keep_log(self, path, done=None):
        """Put a finished log into the project it was recorded for (once).
        True / False when settled here; None when the standalone logger is
        writing it into the .ovt on a worker thread -- ``done(kept)`` follows
        on the GUI thread."""
        keep = getattr(self, "_keep_ref", None)
        if not keep or keep[1] != path:
            return False
        self._keep_ref = None
        ws, path = keep
        try:
            later = getattr(self._window, "keep_log_async", None)
            if later is not None:           # the standalone logger
                got = later(ws, path, self, done)
                return None if got is None else bool(got)
            own = getattr(self._window, "keep_log", None)
            if own is not None:
                return bool(own(ws, path, self))
            from .. import gui_project as _gp
            return _gp.keep_log(ws.win, ws, path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                self, "Data logging", "The log could not be put into the "
                "project (%s); it is at %s" % (ex, path))
            return False

    def _pause(self, on):
        if self._worker is not None:
            self._worker.pause(on)
            self._say("Paused (session kept alive)." if on else "Logging resumed.")

    def _stop_logging(self, finish=True):
        """Stop. Recording ends, the connection stays up and goes back to
        browsing -- nothing here touches the link. The cheap part runs now
        (close the file, flip the buttons); keeping the log and loading it
        into the viewer follow from the event loop, after the buttons have
        repainted. ``finish=False`` returns ``(path, rows)`` instead, for
        the master Stop to finish every pane's log in one go."""
        if not self._logging and not self._log_path:
            return None
        self._logging = False
        if self._worker is not None:
            self._worker.stop_logging()
        self._set_logging_ui(False)
        path, self._log_path = self._log_path, ""
        rows = self._live.rows
        if not path:
            return None
        self._set_default_path()
        self._say("Logging stopped. %d rows." % rows)
        if not finish:
            return path, rows
        QtCore.QTimer.singleShot(0, self, lambda: self._finish_log(path, rows))
        return None

    def _finish_log(self, path, rows, ended=False):
        """Keep a stopped (or cut-off) log and hand it to the viewer."""
        head = "Session ended." if ended else "Logging stopped."

        def said(kept):
            try:
                if kept:
                    self._say("%s %d rows kept in the project (saved with it)." % (head, rows))
                else:
                    self._say("%s %d rows written to %s" % (head, rows, path))
            except RuntimeError:
                pass                    # the window closed while it was kept
        kept = self._keep_log(path, done=said)
        if kept is None:
            self._say("%s %d rows; putting them into the project..." % (head, rows))
        else:
            said(kept)
        self.log_finished.emit(path)

    def _on_logged(self, gen, stamp, texts, values):
        if not self._logging or gen != self._rec_gen:
            return                      # a row from before Stop, still in flight
        if self._t0 is None:
            self._t0 = stamp
        t = (stamp - self._t0).total_seconds()
        self._live.append(stamp, texts)
        self.chart.append(t, values)
        for r, txt in enumerate(texts):
            it = self.sel.item(r, 2)
            if it is not None:
                it.setText(txt)
        n = self._live.rows
        if n >= 2 and t > 0:
            self.rate.setText("%d rows, %.1f rows/s" % (n, (n - 1) / t))

#: Rows sampled to pick column widths, instead of scanning all of them.
VIEW_WIDTH_SAMPLE = 150



#: QSettings key: the Dual logging box as it was last left
DUAL_KEY = "datalog/dual"


def _prefs():
    """Where the logger's own switches are kept (tests point it elsewhere)."""
    return QtCore.QSettings(*branding.QSETTINGS)


def _remembered_dual() -> bool:
    try:
        return str(_prefs().value(DUAL_KEY, False)).lower() in ("true", "1")
    except Exception:
        return False


def _remember_dual(on: bool) -> None:
    try:
        _prefs().setValue(DUAL_KEY, bool(on))
    except Exception:
        pass

class DataLogDialog(QtWidgets.QDialog):
    """Live logging for one controller, or for two at once.

    Dual logging exists because the interesting questions span both boxes: the
    transmission commands a shift and the engine answers it, and a shift start
    inferred from a TCU ratio change says nothing about what the engine did in
    the same hundred milliseconds. The engine on K-line and the transmission on
    CAN are separate physical layers, so there is no bus contention and each
    side keeps its own full sample rate; both workers stamp from the same clock,
    which is what makes the two files line up afterwards.

    One tab per controller, each with its own cable, blocks, cells and file,
    plus the master row that connects and logs them together.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent or (window if isinstance(window, QtWidgets.QWidget) else None))
        self._window = window
        self._viewed: Optional[_lf.DataLog] = None
        self._pending_view = None        # path waiting for the viewer
        self._view_queued = False
        self.setWindowTitle("Data logging")
        try:
            from ..gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        self.resize(1020, 760)
        v = QtWidgets.QVBoxLayout(self)

        # The shared status line is built first even though it sits at the
        # bottom: adding a pane connects its status_msg straight away, so the
        # label has to exist before any pane does.
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)

        # -- binaries row (standalone only) --------------------------------
        # With no main window there are no tabs to borrow a binary from, so the
        # files that name the cells are opened here -- the flasher's File row.
        if getattr(window, "standalone", False):
            v.addLayout(self._build_bin_row())

        # -- master row ---------------------------------------------------
        mr = QtWidgets.QHBoxLayout()
        self.dual_chk = QtWidgets.QCheckBox("Dual logging")
        self.dual_chk.setToolTip(
            "Log a second controller at the same time -- engine on K-line and transmission\n"
            "on CAN use different pins, so neither slows the other down. Each writes its own\n"
            "file, named after the binary open in its tab.")
        self.dual_chk.toggled.connect(self._dual_toggled)
        mr.addWidget(self.dual_chk)
        mr.addSpacing(12)
        self.m_connect = QtWidgets.QPushButton("Connect")
        self.m_connect.clicked.connect(self._master_connect)
        mr.addWidget(self.m_connect)
        self.m_start = QtWidgets.QPushButton("Start logging")
        self.m_start.clicked.connect(self._master_start)
        mr.addWidget(self.m_start)
        self.m_pause = QtWidgets.QPushButton("Pause")
        self.m_pause.setCheckable(True)
        self.m_pause.toggled.connect(self._master_pause)
        mr.addWidget(self.m_pause)
        self.m_stop = QtWidgets.QPushButton("Stop")
        self.m_stop.clicked.connect(self._master_stop)
        mr.addWidget(self.m_stop)
        mr.addStretch(1)
        v.addLayout(mr)

        # -- one tab per controller, plus the viewer ----------------------
        self.tabs = QtWidgets.QTabWidget()
        v.addWidget(self.tabs, 1)

        self._panes: List[ConnPane] = []
        first = ConnPane(window, scope="a")
        first.conn.default_from_platform(getattr(window, "primary", None))
        self._add_pane(first)
        # The second pane defaults to the other side of the pair, so turning
        # dual logging on is usually the only click needed.
        second = ConnPane(window, default_ecu=("EDC16" if first.conn.is_tcu() else "ZF6HP"),
                          scope="b")
        self._second = second
        second.status_msg.connect(lambda m, p=second: self._pane_said(p, m))
        second.log_finished.connect(self._load_view)
        second.state_changed.connect(self._refresh_master)

        self.viewer = self._build_viewer()
        self.tabs.addTab(self.viewer, "Log file")

        v.addWidget(self.status)
        self._refresh_master()
        # dual logging as it was left last time (the first pane in front)
        if _remembered_dual():
            self.dual_chk.setChecked(True)
            self.tabs.setCurrentIndex(0)

    # ---- binaries (standalone) ----------------------------------------
    def _build_bin_row(self):
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(QtWidgets.QLabel("Binaries:"))
        self.bin_lbl = QtWidgets.QLabel()
        self.bin_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        brow.addWidget(self.bin_lbl, 1)
        self.bin_btn = QtWidgets.QPushButton("Open project or binary...")
        self.bin_btn.setToolTip(
            "Open the car's project (.ovt) or its ECU or TCU file so its measuring-block\n"
            "cells are named. A log recorded against a project goes into it.\n"
            "One file per controller: an engine file and a transmission file can both\n"
            "be open for dual logging; opening another for the same controller replaces it.")
        self.bin_btn.clicked.connect(self._pick_binary)
        brow.addWidget(self.bin_btn)
        self._update_bin_label()
        return brow

    def _update_bin_label(self):
        names = self._window.names()
        self.bin_lbl.setText(", ".join(names) if names else
                             "none -- cells are unnamed until a binary is open")

    def _pick_binary(self):
        from .. import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open a binary to name the cells", _recent.get_last_dir(),
            "Projects and binaries (*.ovt *.bin *.ori *.sgo *.dim *.frf *.edc16 "
            "*.edc15 *.6hp *.8hp *.hvac *.vdo);;OVT projects (*.ovt);;"
            "All files (*)")
        if path:
            self.open_binary(path)

    def open_binary(self, path):
        """Standalone: open ``path`` to name cells from, and retarget a pane to
        its controller when none is on it yet. True when it loaded."""
        if not self._window.open_path(path, self):
            return False
        self._update_bin_label()
        prim = self._window.primary
        want = ConnectionBar.PLATFORM_ECU.get((getattr(prim, "platform", None) or "").lower())
        panes = self._panes + ([self._second] if self._second not in self._panes else [])
        # Point the front pane at this file's controller -- unless a pane is
        # already on it (dual logging set up by hand) or the front pane is live.
        if want and not any(p.conn.ecu() == want for p in self._panes) \
                and not self.pane.is_connected():
            self.pane.conn.default_from_platform(prim)
        for p in panes:
            p._sync_namer()
            p._update_src_hint()
        return True

    # ---- panes --------------------------------------------------------
    def _add_pane(self, pane):
        pane.status_msg.connect(lambda m, p=pane: self._pane_said(p, m))
        pane.log_finished.connect(self._load_view)
        pane.state_changed.connect(self._refresh_master)
        self._panes.append(pane)
        self.tabs.insertTab(len(self._panes) - 1, pane, pane.title())
        return pane

    @property
    def panes(self):
        """The connections in use: one, or two with dual logging on."""
        return list(self._panes)

    @property
    def pane(self) -> "ConnPane":
        """The pane in front, or the first one when the viewer tab is up. The
        single-connection attributes below all read through this."""
        w = self.tabs.currentWidget()
        return w if isinstance(w, ConnPane) else self._panes[0]

    # Kept so callers and tests written against the single-connection dialog
    # keep working: they mean "the connection in front".
    @property
    def conn(self):
        return self.pane.conn

    @property
    def preset_box(self):
        return self.pane.preset_box

    @property
    def preset_hint(self):
        return self.pane.preset_hint

    @property
    def path_edit(self):
        return self.pane.path_edit

    @property
    def group_box(self):
        return self.pane.group_box

    @property
    def _namer(self):
        return self.pane._namer

    @property
    def _channels(self):
        return self.pane._channels

    @property
    def _worker(self):
        return self.pane._worker

    def _apply_preset(self, *a):
        return self.pane._apply_preset(*a)

    def _pane_said(self, pane, msg):
        """One shared status line. With two connections an unlabelled message
        is worse than none -- 'Group 213: not available' needs to say which
        box refused."""
        self.status.setText(msg if len(self._panes) < 2 else "%s: %s" % (pane.title(), msg))

    def _dual_toggled(self, on):
        _remember_dual(on)
        if on:
            if self._second not in self._panes:
                self._add_pane(self._second)
            self.tabs.setCurrentWidget(self._second)
        else:
            if self._second in self._panes:
                if self._second.is_connected():
                    self._second.shutdown()
                i = self.tabs.indexOf(self._second)
                if i >= 0:
                    self.tabs.removeTab(i)
                self._second.setParent(None)
                self._panes.remove(self._second)
        self._retitle_tabs()
        self._refresh_master()

    def _retitle_tabs(self):
        for p in self._panes:
            i = self.tabs.indexOf(p)
            if i >= 0 and self.tabs.tabText(i) != p.title():
                self.tabs.setTabText(i, p.title())

    # ---- master controls ----------------------------------------------
    def _refresh_master(self):
        self._retitle_tabs()
        conn = [p for p in self._panes if p.is_connected()]
        logging_ = [p for p in self._panes if p.is_logging()]
        many = len(self._panes) > 1
        self.m_connect.setText(("Disconnect all" if many else "Disconnect") if conn
                               else ("Connect all" if many else "Connect"))
        self.m_start.setEnabled(bool(conn) and not logging_)
        self.m_pause.setEnabled(bool(logging_))
        self.m_stop.setEnabled(bool(logging_))
        if not logging_ and self.m_pause.isChecked():
            self.m_pause.blockSignals(True)
            self.m_pause.setChecked(False)
            self.m_pause.blockSignals(False)

    def _master_connect(self):
        if any(p.is_connected() for p in self._panes):
            for p in self._panes:
                p.disconnect_now()
            return
        for p in self._panes:
            p.connect_now(quiet=len(self._panes) > 1)

    def _master_start(self):
        """Start every connected pane. Each file is its own, so a pane that
        cannot start (no cells ticked) does not stop the other."""
        started = [p for p in self._panes if p.is_connected() and p._start(quiet=len(self._panes) > 1)]
        if not started:
            return
        if len(started) < len([p for p in self._panes if p.is_connected()]):
            self.status.setText("Logging %s only -- the other connection had nothing to log."
                                % ", ".join(p.title() for p in started))

    def _master_pause(self, on):
        for p in self._panes:
            if p.is_logging():
                p.pause_btn.setChecked(on)

    def _master_stop(self):
        """Stop every recording first -- all files closed and all buttons
        flipped before anything slow -- then keep the logs together from one
        deferred call, after the window has repainted."""
        stopped = []
        for p in self._panes:
            if p.is_logging():
                got = p._stop_logging(finish=False)
                if got:
                    stopped.append((p, got))
        if stopped:
            QtCore.QTimer.singleShot(0, self, lambda: self._finish_logs(stopped))

    def _finish_logs(self, stopped):
        for p, (path, rows) in stopped:
            p._finish_log(path, rows)

    # ---- window events ------------------------------------------------
    def showEvent(self, ev):
        super().showEvent(ev)
        for p in self._panes:
            p._sync_namer()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QtCore.QEvent.Type.ActivationChange and self.isActiveWindow():
            for p in self._panes:
                p._sync_namer()

    def closeEvent(self, ev):
        # Ask every worker to wind up BEFORE waiting on any, and wait against
        # one shared deadline: closing a dual session used to wait 5 s per
        # pane in turn and could sit there for ten seconds looking hung. A CAN
        # teardown is the slow one (TP 2.0 disconnect, J2534 close, often
        # through the 32-bit bridge), and both can do it at once. Past the
        # deadline the transport is cut, then the reaper takes over.
        for p in self._panes:
            p.request_stop()
        ConnPane.wind_down(self._panes)
        super().closeEvent(ev)

    # ---- viewer -------------------------------------------------------
    def _build_viewer(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        top = QtWidgets.QHBoxLayout()
        ob = self.open_log_btn = QtWidgets.QPushButton("Open log...")
        ob.setToolTip("A log kept in a project, or a CSV file")
        ob.clicked.connect(self._open_log)
        top.addWidget(ob)
        self.exp_vcds = QtWidgets.QPushButton("Export as VCDS CSV...")
        self.exp_vcds.clicked.connect(lambda: self._export("vcds"))
        top.addWidget(self.exp_vcds)
        self.exp_kw = QtWidgets.QPushButton("Export as KWHack CSV...")
        self.exp_kw.clicked.connect(lambda: self._export("kwhack"))
        top.addWidget(self.exp_kw)
        top.addStretch(1)
        self.view_info = QtWidgets.QLabel("No log loaded.")
        self.view_info.setStyleSheet("color: gray")
        top.addWidget(self.view_info)
        v.addLayout(top)
        self.view_chart = StripChart()
        self.view_chart.setMinimumHeight(220)
        self.view_chart.cursorChanged.connect(self._cursor_row)
        v.addWidget(self.view_chart, 2)
        # A view over the parsed rows, not one item per cell: every row of a
        # long drive log is shown and nothing is built up front.
        self.view_model = _lv.LogTableModel(self)
        self.view_table = QtWidgets.QTableView()
        self.view_table.setModel(self.view_model)
        self.view_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.view_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.view_table.verticalHeader().setDefaultSectionSize(
            self.view_table.fontMetrics().height() + 6)
        self.view_table.selectionModel().selectionChanged.connect(self._row_selected)
        v.addWidget(self.view_table, 3)
        self._loader = _lv.LogLoader(self)
        self._loader.loaded.connect(self._view_loaded)
        self._loader.failed.connect(self._view_failed)
        return w

    def _open_log(self):
        """Open log...: the logs kept in the open projects (newest first,
        under each project), or a CSV file. With no project logs it goes
        straight to the file picker."""
        from . import project_logs as _pl
        try:
            kept = _pl.kept_logs(self._window)
        except Exception:
            kept = []
        if not kept:
            return self._open_log_file()
        menu = QtWidgets.QMenu(self)
        last = None
        for k in kept:
            if k.project != last:
                if last is not None:
                    menu.addSeparator()
                head = menu.addAction("Kept in %s" % k.project)
                head.setEnabled(False)
                last = k.project
            act = menu.addAction(k.label + ("  (pinned)" if k.pinned else ""))
            act.triggered.connect(lambda _c=False, k=k: self.view_kept_log(k))
        menu.addSeparator()
        menu.addAction("From a file...", self._open_log_file)
        self._open_log_menu = menu                      # for the tests
        btn = getattr(self, "open_log_btn", None)
        pos = btn.mapToGlobal(btn.rect().bottomLeft()) if btn is not None \
            else QtGui.QCursor.pos()
        menu.popup(pos)

    def _open_log_file(self):
        from .. import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open data log", _recent.get_last_dir("log"),
            "CSV data logs (*.csv);;All files (*)")
        if path:
            _recent.set_last_dir(os.path.dirname(path), "log")
            self.view_file(path)

    def view_kept_log(self, kept):
        """View a log kept in a project (project_logs.KeptLog)."""
        from . import project_logs as _pl
        try:
            path = _pl.working_copy(kept)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(
                self, "Data log", "%s could not be read from %s: %s"
                % (kept.filename, kept.project, ex))
            return
        self.view_file(path)

    def view_file(self, path):
        """Show the log at ``path`` in the Log file tab, and bring the tab
        forward."""
        self._load_view(path)
        self.tabs.setCurrentWidget(self.viewer)

    def _load_view(self, path):
        """Queue the file for the viewer; do not read it here.

        Stop used to land straight in show_log, so the click handler did not
        return until a whole drive log had been turned into table items -- and
        with two connections it did that twice before the buttons even
        repainted. Queuing lets the stop finish, repaint and release the file
        first; coalescing means a dual stop loads the later log once rather
        than both in a row."""
        self._pending_view = path
        if not self._view_queued:
            self._view_queued = True
            QtCore.QTimer.singleShot(0, self._drain_view)

    def _drain_view(self):
        self._view_queued = False
        path, self._pending_view = self._pending_view, None
        if not path:
            return
        # parsed on a worker thread; _view_loaded shows it
        self.view_info.setText("Reading %s..." % os.path.basename(path))
        self._loader.load(path)

    def _view_loaded(self, got):
        self.show_log(got.log, got.secs, got.columns)

    def _view_failed(self, path, msg):
        self.view_info.setText("No log loaded.")
        QtWidgets.QMessageBox.critical(self, "Data log", "Cannot read %s: %s" % (path, msg))

    def show_log(self, log: _lf.DataLog, secs=None, columns=None):
        """Show ``log``: info line, chart and table. ``secs`` / ``columns``
        are the chart series when the loader already worked them out."""
        self._viewed = log
        # re-label channels from the binary where the file only has generic
        # names. With two connections open, the right map is the one whose
        # controller wrote this log, so pick the pane that can name its cells.
        namer = self._namer_for_log(log)
        for c in log.channels:
            if namer is not None and namer.available and not c.is_derived and \
                    (c.label.startswith("Group ") or not c.label):
                c2 = namer.channel(c.group, c.field)
                c.label, c.signal = c2.label, c2.signal
                c.unit = c.unit or c2.unit
        self.view_info.setText("%s - %d channels, %d rows, %.1f s (%s layout)" % (
            os.path.basename(log.path), len(log.channels), log.rows,
            (log.seconds()[-1] if log.rows else 0.0), log.layout))
        if secs is None:
            secs = log.seconds()
        if columns is None:
            columns = [log.column(i) for i in range(len(log.channels))]
        self.view_chart.window = 0.0
        self.view_chart.cursor = -1
        self.view_chart.set_data(secs, columns,
                                 [c.title for c in log.channels], [c.unit for c in log.channels])
        self.view_model.set_log(log, secs)
        self._size_view_columns(log)
        self.exp_vcds.setEnabled(True)
        self.exp_kw.setEnabled(True)

    def _namer_for_log(self, log):
        """Whichever open pane's map actually covers this log's groups. Naming
        a transmission log from the engine's block map would label every cell
        plausibly and wrongly."""
        want = {c.group for c in log.channels}
        best, best_hits = None, 0
        for p in self._panes:
            n = p._namer
            if not getattr(n, "available", False):
                continue
            hits = len(want & set(n.populated_groups()))
            if hits > best_hits:
                best, best_hits = n, hits
        if best is not None:
            return best
        return self.pane._namer

    def _size_view_columns(self, log):
        """Width each column from the header and a sample of rows.

        ``resizeColumnsToContents`` measures every cell in the table, which on
        a drive log is six figures' worth of text metrics and most of the
        freeze. A sample gives the same widths in practice, because the
        columns are numbers of near-constant length."""
        t, m = self.view_table, self.view_model
        fm = t.fontMetrics()
        rows = m.rowCount()
        step = max(1, rows // VIEW_WIDTH_SAMPLE)
        for c in range(m.columnCount()):
            head = m.headerData(c, QtCore.Qt.Horizontal)
            w = fm.horizontalAdvance(head) if head else 60
            for r in range(0, rows, step):
                w = max(w, fm.horizontalAdvance(m.text(r, c)))
            t.setColumnWidth(c, min(w + 24, 320))

    def _cursor_row(self, row):
        t = self.view_table
        if 0 <= row < self.view_model.rowCount():
            t.selectRow(row)
            t.scrollTo(self.view_model.index(row, 0))

    def _row_selected(self, *_a):
        rows = {i.row() for i in self.view_table.selectionModel().selectedIndexes()}
        if rows:
            self.view_chart.cursor = min(rows)
            self.view_chart.update()

    def _export(self, layout):
        if self._viewed is None:
            return
        from .. import recent as _recent
        base = os.path.splitext(os.path.basename(self._viewed.path or "log"))[0]
        suffix = "_vcds.csv" if layout == "vcds" else "_kwhack.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export data log", os.path.join(_recent.get_last_dir("log"), base + suffix),
            "CSV data log (*.csv);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"
        try:
            if layout == "vcds":
                _lf.write_vcds(path, self._viewed)
            else:
                _lf.write_kwhack(path, self._viewed)
        except OSError as ex:
            QtWidgets.QMessageBox.critical(self, "Export", str(ex))
            return
        _recent.set_last_dir(os.path.dirname(path), "log")
        self.status.setText("Exported %s layout to %s" % (layout, path))


# ------------------------------------------------------- one-shot ECU tasks
class EcuTask(QtCore.QThread):
    """Connect, run one callable on the session, disconnect."""
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    status = QtCore.Signal(str)

    def __init__(self, port, ecu, speed, cap_high, fn, fast_init=True,
                 transport="kline", can_rate=500000, can_dest=0x01):
        super().__init__()
        self._args = (port, ecu, speed, cap_high)
        self._fn = fn
        self._fast_init = fast_init
        self._transport = transport
        self._can_rate = can_rate
        self._can_dest = can_dest
        self._can = None

    def run(self):
        port, ecu, speed, cap = self._args
        link = None
        try:
            if self._transport == "can":
                # KWP2000 over TP 2.0, opened exactly as LogWorker opens it.
                from ..flash import j2534 as _j
                from ..flash.tp20_transport import CanKLine
                self._can = open_j2534(port)
                if isinstance(self._can, _j.BridgePassThru):
                    tp = self._can.open_tp20(dest=self._can_dest, baud=self._can_rate,
                                             params={"block_size": 0x0F})
                    link = CanKLine(tp=tp, dest=self._can_dest, log=self.status.emit)
                else:
                    ch = self._can.connect(_j.CAN, self._can_rate)
                    ch.pass_all()
                    ch.clear_rx()
                    link = CanKLine(ch, dest=self._can_dest, log=self.status.emit)
                speed, cap = "Low", False        # no elevated K-line baud over CAN
            elif is_j2534(port):
                self._can = open_j2534(port)
                link = kline_over_j2534(self._can, ecu, self._fast_init, self.status.emit)
            else:
                link = KLine(port, baud=10400,
                             prefer_fast_init=self._fast_init and ecu != "EDC15")
            sess = _diag.DiagSession(link, ecu, self.status.emit)
            sess.connect("Low" if ecu == "EDC15" else speed, cap)
            try:
                self.done.emit(self._fn(sess))
            finally:
                sess.close()
        except Exception as ex:
            self.failed.emit(str(ex))
        finally:
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass
            if self._can is not None:
                try:
                    self._can.close()
                except Exception:
                    pass


class EcuCodesDialog(QtWidgets.QDialog):
    """Read (and clear) fault codes from the ECU; returns the Dtc list."""

    def __init__(self, window, parent=None):
        super().__init__(parent or (window if isinstance(window, QtWidgets.QWidget) else None))
        self.setWindowTitle("Fault codes from ECU")
        self.resize(560, 220)
        self.codes: Optional[List[_diag.Dtc]] = None     # None until a read completed
        self._task = None
        v = QtWidgets.QVBoxLayout(self)
        # Its own scope: sharing "a" with the logger's first pane meant each
        # dialog overwrote the other's cable and transport.
        self.conn = ConnectionBar(window, scope="dtc")
        self.conn.speed_box.setCurrentIndex(0)      # a code read needs no speed raise
        v.addWidget(self.conn)
        self.status = QtWidgets.QLabel("Reads stored fault codes (KWP2000 18 02 FF 00 / KW1281 07).")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        b = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read codes")
        self.read_btn.clicked.connect(lambda: self._run("read"))
        b.addWidget(self.read_btn)
        self.clear_btn = QtWidgets.QPushButton("Clear codes on ECU")
        self.clear_btn.clicked.connect(lambda: self._run("clear"))
        b.addWidget(self.clear_btn)
        b.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        b.addWidget(close)
        v.addLayout(b)

    def _run(self, what):
        port = self.conn.port()
        can = self.conn.is_can()
        if not port:
            QtWidgets.QMessageBox.warning(
                self, "Fault codes", "Select a J2534 device." if can else
                "Select a K-line cable or J2534 device.")
            return
        if self.conn.ecu() == "ZF6HP-UDS":
            # DiagSession speaks KWP2000/KW1281 only; the 6HP28 is UDS.
            QtWidgets.QMessageBox.warning(
                self, "Fault codes", "Reading fault codes from a 6HP28 (UDS) is not supported here.")
            return
        self.conn.save_settings()
        if what == "clear" and QtWidgets.QMessageBox.question(
                self, "Clear fault codes", "Clear all stored fault codes on the ECU?") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def fn(sess):
            if what == "clear":
                sess.clear_dtcs()
            return sess.read_dtcs()
        self._task = EcuTask(port, self.conn.ecu(), "Low", self.conn.cap_high(), fn,
                             fast_init=self.conn.fast_init(),
                             transport=("can" if can else "kline"),
                             can_rate=self.conn.can_rate.currentData() or 500000,
                             can_dest=self.conn.can_dest())
        self._task.status.connect(self.status.setText)
        self._task.done.connect(self._done)
        self._task.failed.connect(self._failed)
        for w in (self.read_btn, self.clear_btn):
            w.setEnabled(False)
        self._task.start()

    def done(self, r):
        # the task signals its result before it has disconnected; the dialog
        # (and with it self._task) may go away now, the thread must not
        retire_thread(self._task)
        super().done(r)

    def _done(self, codes):
        self.codes = list(codes)
        for w in (self.read_btn, self.clear_btn):
            w.setEnabled(True)
        self.status.setText("%d fault code(s): %s" % (
            len(codes), ", ".join(c.text for c in codes) if codes else "none stored"))
        self.accept()

    def _failed(self, msg):
        for w in (self.read_btn, self.clear_btn):
            w.setEnabled(True)
        QtWidgets.QMessageBox.critical(self, "Fault codes", msg)
