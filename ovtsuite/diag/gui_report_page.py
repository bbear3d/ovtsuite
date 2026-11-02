# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The scan report page (docs/OVT Diag Interface Options.pdf, page 4): a
breadcrumb, four summary cards, and a table of modules with each module's
faults laid out under it as cards, the change since a compared scan, and
the actions to export or copy it.

This is a page of the OVT Diag window (a ``QStackedWidget`` sibling of the
module workspace), not a dialog. It holds no hardware and no garage: the
window loads the scans and hands them over through :meth:`show_report`;
the page tells the window what the user asked for through its signals.
Qt only.

THE CONTRACT with :class:`ovtsuite.diag.gui_diag.DiagWindow` (keep it stable;
the window is written against it):

signals
    ``backRequested()``            the vehicle crumb was clicked: show the workspace
    ``scanChanged()``              the user picked another scan to show
    ``compareChanged()``           the user picked another scan to compare with
    ``exportRequested(str)``       ``"html"`` | ``"pdf"`` | ``"text"``
    ``openGroupRequested(str,int)`` module address and measuring group
                                   (the fault card's "open live data"; group 0 =
                                   just open the module's Live data tab)
    ``moduleActivated(str)``       a module row was activated (Enter / double click):
                                   select it in the workspace
    ``traceRequested(str)``        the trace file link was clicked (path)

methods
    ``set_scans(items, current_key, compare_key)``
        ``items``: ``[(label, key)]`` newest first (the garage's scan names);
        fills the two combos without emitting the change signals.
    ``selected_scan_key() / compare_key()``  -> the keys, or None
    ``show_report(vehicle, scan, before, journal)``
        ``vehicle``: :class:`~ovtsuite.diag.model.Vehicle` or None; ``scan``: the
        :class:`~ovtsuite.diag.model.ScanResult` to show or None (then an empty
        state "No scan yet. Press Scan all"); ``before``: the scan compared
        with or None; ``journal``: ``[journal.Entry]`` written since ``before``
    ``text() -> str``                the plain-text report (``report.text_report``)

How it looks: the module table is a real ``QTableWidget`` drawn to the
suite's table standard (:mod:`ovtsuite.gui_cellmarks`: alternating rows,
``CellDelegate``, ``DIFF_BG`` orange on the CHANGE cell of a module that
changed). The status dot is the cell's icon, so a selected row keeps it
legible; a module's faults are a full-width row under it holding one painted
:class:`~ovtsuite.diag.gui_widgets.Card` per fault.
"""
from __future__ import annotations

import html
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .. import branding
from .. import gui_cellmarks as CM
from .gui_widgets import (Card, Chip, SummaryCard, _Restyler, mono_font, muted_label, scaled_font,
                          style_button, style_page, style_panel, tokens)

Qt = QtCore.Qt

#: table columns
C_ADDR, C_MODULE, C_PART, C_CODING, C_STATUS, C_CHANGE = range(6)
#: an item's status kind ("ok" / "warn" / "bad" / "neutral") -- what the dot is
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
#: an item's module address
ADDR_ROLE = Qt.ItemDataRole.UserRole + 2
_ROW_H = 38


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _plural(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


def _vin_short(vin: str) -> str:
    vin = (vin or "").strip()
    return vin if len(vin) <= 13 else vin[:8] + "…" + vin[-4:]


def _duration(s: float) -> str:
    s = int(round(max(0.0, float(s or 0))))
    return "%d s" % s if s < 60 else "%d min %02d s" % (s // 60, s % 60)


def _short_status(text: str) -> str:
    """``"0x60 -, present, stored"`` -> ``"present · stored"``."""
    parts = []
    for p in text.replace("·", ",").split(","):
        p = p.strip()
        low = p.lower().replace(" ", "")
        if not p or p == "-" or (low.startswith("0x") and all(c in "0123456789abcdefx-" for c in low)):
            continue
        parts.append(p)
    return " · ".join(parts) if parts else text


def change_text(md) -> str:
    """The CHANGE cell of a :class:`~ovtsuite.diag.model.ModuleDiff`: a line per change."""
    lines = []
    for f in md.new_faults:
        lines.append("+ %s %s (new)" % (f.sae or f.display_code, f.text or ""))
    for f in md.cleared_faults:
        lines.append("- %s %s (cleared)" % (f.sae or f.display_code, f.text or ""))
    lines += list(md.changes)
    if md.newly_reached:
        lines.append("newly reached")
    if md.lost:
        lines.append("no longer answers")
    return "\n".join(" ".join(l.split()) for l in lines)


# ----------------------------------------------------------------- the table
class _ReportTable(QtWidgets.QTableWidget):
    """The module table; tells the page when its width changed (the wrapped
    rows and the fault cards then need their heights again)."""
    resized = QtCore.Signal()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.resized.emit()


class _FaultCards(QtWidgets.QWidget):
    """The cell widget of a fault row: one :class:`Card` per fault of the
    module, indented under the module's name. Transparent: the row's own
    colour shows round the cards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QtWidgets.QVBoxLayout(self)
        self._lay.setContentsMargins(60, 2, 18, 12)
        self._lay.setSpacing(8)
        self.cards: List[Card] = []

    def set_indent(self, left: int) -> None:
        m = self._lay.contentsMargins()
        if m.left() != left:
            self._lay.setContentsMargins(left, m.top(), m.right(), m.bottom())

    def add(self, card: Card) -> None:
        self._lay.addWidget(card)
        self.cards.append(card)

    def height_for(self, width: int) -> int:
        return max(40, self._lay.heightForWidth(max(200, width)))


class ReportPage(QtWidgets.QWidget):
    backRequested = QtCore.Signal()
    scanChanged = QtCore.Signal()
    compareChanged = QtCore.Signal()
    exportRequested = QtCore.Signal(str)
    openGroupRequested = QtCore.Signal(str, int)
    moduleActivated = QtCore.Signal(str)
    traceRequested = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        style_page(self, "diagReportPage")
        self._scans: List[Tuple[str, str]] = []
        self._current: Optional[str] = None
        self._compare: Optional[str] = None
        self._args: Optional[tuple] = None
        self._trace_path = ""
        self._rows: Dict[int, str] = {}            # table row -> module address (module rows only)
        self._fault_rows: Dict[int, _FaultCards] = {}
        self._rendering = False

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self._content = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self._content)
        cl.setContentsMargins(20, 18, 20, 12)
        cl.setSpacing(14)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        self.cards: List[SummaryCard] = []
        for _ in range(4):
            c = SummaryCard()
            self.cards.append(c)
            row.addWidget(c, 1)
        cl.addLayout(row)

        self.banner = Card(radius=6)
        self.banner.setEdge("warn")
        bl = QtWidgets.QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 8, 14, 8)
        self.banner_label = QtWidgets.QLabel()
        self.banner_label.setWordWrap(True)
        bl.addWidget(self.banner_label)
        self.banner.hide()
        cl.addWidget(self.banner)

        self.table = self._build_table()
        cl.addWidget(self.table, 1)

        self.journal_box = QtWidgets.QWidget()
        jl = QtWidgets.QVBoxLayout(self.journal_box)
        jl.setContentsMargins(0, 0, 0, 0)
        jl.setSpacing(4)
        self.journal_title = QtWidgets.QLabel()
        jl.addWidget(self.journal_title)
        self._journal_scroll = QtWidgets.QScrollArea()
        self._journal_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._journal_scroll.setWidgetResizable(True)
        self._journal_scroll.viewport().setAutoFillBackground(False)
        self._journal_scroll.setStyleSheet("QScrollArea{background:transparent;}"
                                           "QScrollArea > QWidget > QWidget{background:transparent;}")
        self._journal_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.journal_label = QtWidgets.QLabel()
        self.journal_label.setTextFormat(Qt.TextFormat.RichText)
        self.journal_label.setWordWrap(True)
        self.journal_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._journal_scroll.setWidget(self.journal_label)
        jl.addWidget(self._journal_scroll)
        self.journal_box.hide()
        cl.addWidget(self.journal_box)

        foot = QtWidgets.QHBoxLayout()
        self.footer = muted_label("Every scan is saved with the vehicle. Fault text comes from the module's own "
                                  "definitions, else the shared code catalogue.")
        self.footer.setWordWrap(True)
        foot.addWidget(self.footer, 1)
        self.trace_label = QtWidgets.QLabel()
        self.trace_label.setTextFormat(Qt.TextFormat.RichText)
        self.trace_label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                                 | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.trace_label.linkActivated.connect(lambda _h: self.traceRequested.emit(self._trace_path))
        self.trace_label.hide()
        foot.addWidget(self.trace_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        cl.addLayout(foot)
        outer.addWidget(self._content, 1)

        self.empty_label = QtWidgets.QLabel("No scan yet. Press Scan all.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setFont(scaled_font(self.empty_label.font(), 1.1))
        self.empty_label.setStyleSheet("color: palette(placeholder-text);")
        outer.addWidget(self.empty_label, 1)

        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_rows)
        self._theme_timer = QtCore.QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.setInterval(0)
        self._theme_timer.timeout.connect(self._render)
        self._render()

    # ------------------------------------------------------------- building
    def _build_header(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        style_panel(bar, "diagReportBar", "bottom")
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(20, 10, 20, 10)
        h.setSpacing(4)
        brand = QtWidgets.QLabel(branding.TOOL_NAMES.get("diag", "OVT Diag"))
        brand.setFont(scaled_font(brand.font(), 1.15, True))
        h.addWidget(brand)
        h.addSpacing(6)
        h.addWidget(self._sep())
        self.vehicle_button = QtWidgets.QToolButton()
        self.vehicle_button.setToolTip("Back to the vehicle")
        self.vehicle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vehicle_button.clicked.connect(lambda *_: self.backRequested.emit())
        _Restyler(self.vehicle_button, lambda: self._crumb_qss(False))
        h.addWidget(self.vehicle_button)
        self._vehicle_sep = self._sep()
        h.addWidget(self._vehicle_sep)
        self.scan_button = QtWidgets.QToolButton()
        self.scan_button.setToolTip("Other saved scans")
        self.scan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.scan_menu = QtWidgets.QMenu(self.scan_button)
        _Restyler(self.scan_button, lambda: self._crumb_qss(True))
        h.addWidget(self.scan_button)
        h.addStretch(1)
        h.addWidget(muted_label("Compare with"))
        self.compare_combo = QtWidgets.QComboBox()
        self.compare_combo.setMinimumWidth(260)
        self.compare_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.compare_combo.currentIndexChanged.connect(self._compare_picked)
        h.addWidget(self.compare_combo)
        h.addSpacing(8)
        self.export_button = style_button(QtWidgets.QPushButton("Export HTML / PDF"), "outline")
        self.export_menu = QtWidgets.QMenu(self.export_button)
        self.act_html = self.export_menu.addAction("HTML")
        self.act_pdf = self.export_menu.addAction("PDF")
        self.act_html.triggered.connect(lambda *_: self.exportRequested.emit("html"))
        self.act_pdf.triggered.connect(lambda *_: self.exportRequested.emit("pdf"))
        self.export_button.setMenu(self.export_menu)
        h.addWidget(self.export_button)
        self.copy_button = style_button(QtWidgets.QPushButton("Copy as text"), "outline")
        self.copy_button.clicked.connect(lambda *_: self.exportRequested.emit("text"))
        h.addWidget(self.copy_button)
        return bar

    @staticmethod
    def _sep() -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel("›")
        lab.setStyleSheet("color: palette(placeholder-text);")
        lab.setContentsMargins(2, 0, 2, 0)
        return lab

    @staticmethod
    def _crumb_qss(strong: bool) -> str:
        t = tokens()
        c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
        return ("QToolButton{background:transparent; border:1px solid transparent; border-radius:6px;"
                " padding:4px 8px; color:%s; %s}"
                "QToolButton:hover{background:%s; color:%s;}"
                % (c(t.text if strong else t.muted), "font-weight:600;" if strong else "",
                   c(t.card_hover), c(t.text)))

    def _build_table(self) -> QtWidgets.QTableWidget:
        t = _ReportTable(0, 6)
        t.setObjectName("diagReportTable")
        t.setAlternatingRowColors(True)                 # the suite's table standard (gui_cellmarks)
        t.setItemDelegate(CM.CellDelegate(t))
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.setShowGrid(False)
        t.setWordWrap(True)
        t.setTextElideMode(Qt.TextElideMode.ElideRight)
        t.setIconSize(QtCore.QSize(14, 14))
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        hh = t.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionsClickable(False)
        hh.setStretchLastSection(True)
        hh.setFixedHeight(34)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col, w in ((C_ADDR, 64), (C_MODULE, 190), (C_PART, 290), (C_CODING, 130), (C_STATUS, 130)):
            hh.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(col, w)
        _Restyler(hh, self._header_qss)
        t.activated.connect(self._activated)
        t.resized.connect(self._schedule_fit)
        hh.sectionResized.connect(lambda *_: self._schedule_fit())
        return t

    @staticmethod
    def _header_qss() -> str:
        t = tokens()
        c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
        return ("QHeaderView::section{background:transparent; border:0; border-bottom:1px solid %s;"
                " padding:0 8px; color:%s;}" % (c(t.border), c(t.muted)))

    # ------------------------------------------------------------ the combos
    def set_scans(self, items: Sequence[Tuple[str, str]], current_key: Optional[str] = None,
                  compare_key: Optional[str] = None) -> None:
        self._scans = [(str(a), b) for a, b in items]
        self._current = current_key
        self._compare = compare_key
        self._fill_scan_menu()
        self._fill_compare()
        scan = self._args[1] if self._args else None
        self._set_scan_crumb(scan)

    def _label_of(self, key) -> str:
        for lab, k in self._scans:
            if k == key:
                return lab
        return ""

    def _fill_scan_menu(self) -> None:
        self.scan_menu.clear()
        grp = QtGui.QActionGroup(self.scan_menu)
        grp.setExclusive(True)
        for lab, key in self._scans:
            a = self.scan_menu.addAction(lab)
            a.setCheckable(True)
            a.setChecked(key == self._current)
            a.setData(key)
            grp.addAction(a)
            a.triggered.connect(lambda _c=False, k=key: self._scan_picked(k))
        self.scan_button.setMenu(self.scan_menu if self._scans else None)

    def _fill_compare(self) -> None:
        cb = self.compare_combo
        cb.blockSignals(True)
        try:
            cb.clear()
            cb.addItem("No comparison", None)
            idx = 0
            for lab, key in self._scans:
                if key != self._current:
                    cb.addItem(lab, key)
                    if key == self._compare:
                        idx = cb.count() - 1
            cb.setCurrentIndex(idx)
            if idx == 0:
                self._compare = None
        finally:
            cb.blockSignals(False)

    def _set_scan_crumb(self, scan) -> None:
        if scan is not None:
            text = "Scan · " + _when(scan.when)
        elif self._current is not None and self._label_of(self._current):
            text = "Scan · " + self._label_of(self._current)
        else:
            text = "Scan"
        self.scan_button.setText(text)

    def selected_scan_key(self) -> Optional[str]:
        return self._current

    def compare_key(self) -> Optional[str]:
        return self.compare_combo.currentData()

    def _scan_picked(self, key) -> None:
        if key == self._current:
            return
        self._current = key
        self._fill_compare()
        self._set_scan_crumb(None)
        self.scanChanged.emit()

    def _compare_picked(self, _i: int) -> None:
        self._compare = self.compare_combo.currentData()
        self.compareChanged.emit()

    # ------------------------------------------------------------ the report
    def show_report(self, vehicle, scan, before, journal: Optional[List] = None) -> None:
        self._args = (vehicle, scan, before, list(journal or []))
        self._render()

    def text(self) -> str:
        if self._args is None:
            return ""
        vehicle, scan, before, journal = self._args
        if vehicle is None or scan is None:
            return ""
        from . import report as R
        return R.text_report(vehicle, scan, before, journal)

    def changeEvent(self, ev):
        if ev.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            if not self._rendering:
                self._theme_timer.start()
        super().changeEvent(ev)

    def _render(self) -> None:
        if self._rendering:
            return
        self._rendering = True
        self.setUpdatesEnabled(False)
        try:
            vehicle, scan, before, journal = self._args or (None, None, None, [])
            have = scan is not None
            self._content.setVisible(have)
            self.empty_label.setVisible(not have)
            self.export_button.setEnabled(have and vehicle is not None)
            self.copy_button.setEnabled(have and vehicle is not None)
            if vehicle is not None:
                lab = vehicle.label
                if vehicle.vin and vehicle.vin != lab:
                    lab += " · " + _vin_short(vehicle.vin)
                self.vehicle_button.setText(lab)
            self.vehicle_button.setVisible(vehicle is not None)
            self._vehicle_sep.setVisible(vehicle is not None)
            self._set_scan_crumb(scan)
            if have:
                self._render_scan(scan, before, journal)
            else:
                self._rows, self._fault_rows = {}, {}
                self.table.clearSpans()
                self.table.setRowCount(0)
        finally:
            self.setUpdatesEnabled(True)
            self._rendering = False
        if have:
            self._fit_rows()
            self._schedule_fit()

    def _render_scan(self, scan, before, journal) -> None:
        from .model import diff_scans
        t = tokens(self.palette())
        diff = diff_scans(before, scan) if before is not None else None
        mds = {m.addr: m for m in diff.modules} if diff is not None else {}
        self._fill_cards(scan, before, diff)
        # a stopped scan: unmistakable
        not_tried = len(scan.modules) - len(scan.visited)
        self.banner.setVisible(bool(scan.stopped))
        if scan.stopped:
            self.banner_label.setText(
                '<span style="color:%s"><b>Scan stopped:</b> %s not tried. The rows below show only what '
                'was visited before it stopped.</span>'
                % (t.warn.name(), _plural(not_tried, "module", "modules")))
        self._fill_table(scan, before, mds, t)
        self._fill_journal(before, journal, t)
        self._trace_path = scan.trace_path or ""
        self.trace_label.setVisible(bool(self._trace_path))
        if self._trace_path:
            self.trace_label.setText('<span style="color:%s">Trace of this scan: </span>'
                                     '<a href="trace" style="color:%s; text-decoration:none">%s</a>'
                                     % (t.muted.name(), t.accent.name(),
                                        html.escape(os.path.basename(self._trace_path))))

    def _fill_cards(self, scan, before, diff) -> None:
        c1, c2, c3, c4 = self.cards
        if scan.basis == "gateway":
            suffix = "/ %d in gateway list" % len(scan.modules)
        else:
            suffix = "/ %d tried" % len(scan.visited)
        lines = []
        if scan.stopped:
            lines.append(("Scan stopped: %s not tried"
                          % _plural(len(scan.modules) - len(scan.visited), "module", "modules"), "warn"))
        c1.set("Modules reached", str(len(scan.reached)), suffix, "", lines)
        c1.setEdge("warn" if scan.stopped else "")
        n_faults = scan.fault_total
        n_mods = sum(1 for m in scan.modules if m.reached and m.faults)
        c2.set("Faults now", str(n_faults),
               "in %s" % _plural(n_mods, "module", "modules") if n_faults else "none stored",
               "bad" if n_faults else "ok")
        if diff is None:
            c3.set("No comparison", "", "", "", [("Pick a scan in Compare with", "muted")])
        else:
            # a module the scan did not try is not "no longer answering"
            tried = {m.addr for m in scan.modules if m.reached is not None}
            dmods = [m for m in diff.modules if m.addr in tried]

            def _n(pred):
                return sum(1 for m in dmods if pred(m))
            lines = []
            new = sum(len(m.new_faults) for m in dmods)
            cleared = sum(len(m.cleared_faults) for m in dmods)
            if new:
                lines.append(("+%d new fault%s" % (new, "" if new == 1 else "s"), "bad"))
            if cleared:
                lines.append(("-%d cleared" % cleared, "ok"))
            k = _n(lambda m: any(c.startswith("coding") for c in m.changes))
            if k:
                lines.append(("%s changed" % _plural(k, "coding", "codings"), "warn"))
            k = _n(lambda m: any(c.startswith(("software", "part")) for c in m.changes))
            if k:
                lines.append(("%s changed" % _plural(k, "software version", "software versions"), "warn"))
            k = _n(lambda m: m.newly_reached)
            if k:
                lines.append(("%d newly reached" % k, "ok"))
            k = _n(lambda m: m.lost)
            if k:
                lines.append(("%d no longer answer%s" % (k, "s" if k == 1 else ""), "bad"))
            if not lines:
                lines.append(("No changes", "ok"))
            c3.set("Since " + _day(before.when), "", "", "", lines)
        tr = (scan.transport or "").lower()
        note = {"can": "modules queried in parallel on CAN",
                "kline": "one module at a time on K-line"}.get(tr, "")
        c4.set("Scan took", _duration(scan.duration_s), "", "", [(note, "muted")] if note else [])
        h = max(c.sizeHint().height() for c in self.cards)
        for c in self.cards:
            c.setFixedHeight(h)

    def _dot_icon(self, kind: str, t) -> QtGui.QIcon:
        col = t.faint if kind == "neutral" else t.kind_colors(kind)[0]
        dpr = self.devicePixelRatioF()
        size = 14
        icon = QtGui.QIcon()
        for mode in (QtGui.QIcon.Mode.Normal, QtGui.QIcon.Mode.Selected):
            pm = QtGui.QPixmap(int(size * dpr), int(size * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setBrush(col)
            if mode == QtGui.QIcon.Mode.Selected:        # a ring so it shows on the solid selection
                p.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.HighlightedText), 1.4))
            else:
                p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QtCore.QRectF(2.5, 2.5, size - 5, size - 5))
            p.end()
            icon.addPixmap(pm, mode)
        return icon

    def _fill_table(self, scan, before, mds, t) -> None:
        tb = self.table
        prev = self._rows.get(tb.currentRow()) if tb.currentRow() >= 0 else None
        tb.clearSpans()
        tb.setRowCount(0)
        self._rows, self._fault_rows = {}, {}
        mono = mono_font(tb.font(), 0.95)
        diff_on = before is not None
        heads = ["ADDR", "MODULE", "PART · SOFTWARE", "CODING", "STATUS",
                 ("CHANGE SINCE " + _day(before.when)) if diff_on else "CHANGE"]
        hf = scaled_font(tb.font(), 0.8)
        hf.setLetterSpacing(QtGui.QFont.SpacingType.PercentageSpacing, 104)
        for i, hd in enumerate(heads):
            it = QtWidgets.QTableWidgetItem(hd)
            it.setFont(hf)
            it.setForeground(QtGui.QBrush(t.muted))
            tb.setHorizontalHeaderItem(i, it)
        before_mods = {m.addr: m for m in before.modules} if diff_on else {}
        dim = QtGui.QBrush(t.muted)

        r = 0
        tb.setRowCount(len(scan.modules) + sum(1 for m in scan.modules if m.reached and m.faults))
        for m in scan.modules:
            tried = m.reached is not None
            if m.reached is None:
                status, kind = "not tried", "neutral"
            elif not m.reached:
                status, kind = "no answer", "warn"
            elif m.faults:
                status = _plural(len(m.faults), "fault", "faults")
                kind = "bad" if any(not f.intermittent for f in m.faults) else "warn"
            else:
                status, kind = "no faults", "ok"
            part = " · ".join(x for x in (m.part, m.sw) if x) or (m.identity_text if m.reached else "")
            coding = (m.coding_text or "—") if m.reached else ""
            md = mds.get(m.addr)
            changed = md is not None and not md.empty
            if not diff_on or not tried:
                change = ""
            elif changed:
                change = change_text(md)
            else:
                change = "—" if m.reached else ""
            cells = [(m.addr, mono), (m.name or "?", None), (part, mono), (coding, mono),
                     (status, None), (change, None)]
            for col, (text, font) in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                it.setData(ADDR_ROLE, m.addr)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if font is not None:
                    it.setFont(font)
                if col in (C_ADDR, C_PART) or not tried or (col == C_CHANGE and not changed):
                    it.setForeground(dim)
                if col == C_STATUS:
                    it.setData(KIND_ROLE, kind)
                    it.setIcon(self._dot_icon(kind, t))
                    tip = "\n".join("%s %s" % (f.display_code, f.text) for f in m.faults) if m.faults else m.error
                    if tip:
                        it.setToolTip(tip)
                if col == C_CHANGE and changed:
                    CM.mark_differs(it)             # DIFF_BG, dark text
                    it.setToolTip(change)
                tb.setItem(r, col, it)
            self._rows[r] = m.addr
            r += 1
            if m.reached and m.faults:
                cards = _FaultCards()
                bm = before_mods.get(m.addr)
                old = {f.code for f in bm.faults} if (bm is not None and bm.reached) else set()
                for f in m.faults:
                    cards.add(self._fault_card(m, f, _day(before.when) if (diff_on and f.code in old) else "", t))
                tb.setSpan(r, 0, 1, 6)
                head = QtWidgets.QTableWidgetItem("")
                head.setFlags(Qt.ItemFlag.NoItemFlags)          # not selectable: Up/Down skip it
                tb.setItem(r, 0, head)
                tb.setCellWidget(r, 0, cards)
                self._fault_rows[r] = cards
                r += 1
        # keep the selection on the same module across a refresh
        if prev is not None:
            for row, a in self._rows.items():
                if a == prev:
                    tb.setCurrentCell(row, 0)
                    break
        elif self._rows:
            tb.selectionModel().setCurrentIndex(tb.model().index(min(self._rows), 0),
                                                QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)

    def _fault_card(self, m, f, also_in: str, t) -> Card:
        card = Card(radius=6)
        card.setObjectName("faultCard")
        v = QtWidgets.QVBoxLayout(card)
        v.setContentsMargins(14, 9, 14, 9)
        v.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(10)
        code = QtWidgets.QLabel(f.display_code)
        code.setFont(mono_font(code.font(), 0.98))
        code.setObjectName("faultCode")
        top.addWidget(code, 0, Qt.AlignmentFlag.AlignTop)
        txt = QtWidgets.QLabel(f.text or "(no text)")
        txt.setObjectName("faultText")
        txt.setWordWrap(True)
        top.addWidget(txt, 1)
        if f.intermittent:
            chip = Chip("intermittent", "warn")
        elif f.status_text:
            low = f.status_text.lower()
            present = ("present" in low or "active" in low) and "not present" not in low
            chip = Chip(_short_status(f.status_text), "bad" if present else "neutral")
        else:
            chip = None
        if chip is not None:
            chip.setObjectName("faultChip")
            if f.status_text:
                chip.setToolTip(f.status_text)
            top.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
        if also_in:
            ai = muted_label("also in %s scan" % also_in)
            ai.setObjectName("faultAlso")
            top.addWidget(ai, 0, Qt.AlignmentFlag.AlignTop)
        link = QtWidgets.QLabel('<a href="live" style="color:%s; text-decoration:none">open live data</a>'
                                % t.accent.name())
        link.setObjectName("faultLink")
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                     | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        link.linkActivated.connect(lambda _h, a=m.addr: self.openGroupRequested.emit(a, 0))
        v.addLayout(top)
        if f.freeze:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(12)
            fr = muted_label("Freeze frame:  " + "     ".join("%s = %s" % (k, val) for k, val in f.freeze.items()))
            fr.setObjectName("faultFreeze")
            fr.setWordWrap(True)
            row.addWidget(fr, 1)
            row.addWidget(link, 0, Qt.AlignmentFlag.AlignTop)
            v.addLayout(row)
        else:
            top.addWidget(link, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _fill_journal(self, before, journal, t) -> None:
        show = before is not None
        self.journal_box.setVisible(show)
        if not show:
            return
        self.journal_title.setText('<span style="color:%s"><b>Written to the car since %s</b></span>'
                                   % (t.muted.name(), _day(before.when)))
        if journal:
            rows = "".join('<span style="color:%s">%s</span> &nbsp; %s<br>'
                           % (t.muted.name(), html.escape(_when(e.when)), html.escape(e.summary))
                           for e in journal)
        else:
            rows = '<span style="color:%s">Nothing was written to the car since then.</span>' % t.muted.name()
        self.journal_label.setText(rows)
        n = max(1, len(journal))
        fm = QtGui.QFontMetrics(self.journal_label.font())
        self._journal_scroll.setFixedHeight(min(150, n * (fm.lineSpacing() + 4) + 10))

    # ------------------------------------------------------------- sizing
    def _schedule_fit(self) -> None:
        if not self._rendering:
            self._fit_timer.start()

    def _fit_rows(self) -> None:
        """Row heights: a module row to its wrapped CHANGE text, a fault row
        to its cards at the width the table has now."""
        tb = self.table
        if tb.rowCount() == 0:
            return
        w = tb.viewport().width()
        indent = tb.columnWidth(C_ADDR) - 4
        tb.setUpdatesEnabled(False)
        try:
            for r in range(tb.rowCount()):
                fc = self._fault_rows.get(r)
                if fc is not None:
                    fc.set_indent(indent)
                    tb.setRowHeight(r, fc.height_for(w))
                else:
                    tb.resizeRowToContents(r)
                    tb.setRowHeight(r, max(_ROW_H, tb.rowHeight(r) + 4))
        finally:
            tb.setUpdatesEnabled(True)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._schedule_fit()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._schedule_fit()

    # --------------------------------------------------------------- input
    def _activated(self, index) -> None:
        addr = self._rows.get(index.row())
        if addr:
            self.moduleActivated.emit(addr)


__all__ = ["ReportPage", "change_text", "C_ADDR", "C_MODULE", "C_PART", "C_CODING", "C_STATUS", "C_CHANGE",
           "KIND_ROLE"]
