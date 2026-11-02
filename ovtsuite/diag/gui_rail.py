# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The module rail of the OVT Diag window (docs/OVT Diag Interface Options.pdf,
page 1): the left column listing the vehicle's modules as cards -- address,
name, what the scan learned (part number, protocol the module answered
with), a fault badge -- under a ``Scan all`` button, with the generic OBD-II
and the garage at the foot.

A view only: it holds no link and no garage. The window hands it the
address-table rows, what is known of each module and which are live
(:meth:`ModuleRail.set_modules`); it reports the user's choices through its
signals. Before a scan the list is an empty state, not the address table:
the table's rows sit behind ``All known addresses`` (plain dim cards) so a
module can still be picked and connected by hand, and ``Connect by
address...`` reaches one the table does not list. Qt only.

API (what :class:`ovtsuite.diag.gui_diag.DiagWindow` uses and the tests drive)::

    set_modules(rows, infos, lives, absent, tips=None)
        rows    the address-table rows (dicts with ``addr``, ``name``, ...)
        infos   {addr: ModuleInfo} of the modules visited (reached or not)
        lives   addresses with an open session
        absent  addresses the gateway list says are not installed
        tips    {addr: tooltip}, optional
    set_summary(text) / set_scanning(bool) / set_footer(obd, vehicles, scans)
    select(addr)  /  current_addr()  /  filter(text)  /  card_addrs()

signals: ``selected(str)`` (a click / :meth:`select`; "" = nothing),
``connectRequested(str)`` (double click or Enter), ``connectByAddressRequested()``,
``scanRequested()``, ``stopRequested()``, ``obdRequested()``, ``garageRequested()``.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .defs.schema import as_int
from .gui_widgets import (button_qss, restyle, Chip, ClickableFrame, CountBadge, Dot, ElidedLabel, Tokens, mono_font, muted_label,
                          scaled_font, style_button, style_panel, tokens)

Qt = QtCore.Qt

RAIL_WIDTH = 285
EMPTY_TEXT = "No scan yet. Press Scan all to find the car's modules."

_TRANSPORT_NAMES = {"kline": "K-line", "can": "CAN", "uds": "UDS"}


# --------------------------------------------------------------- helpers
def protocol_chip(m) -> str:
    """The short protocol tag of a module card: only what the module
    answered with (``KWP-TP2.0``, ``KW1281 @ 9600``, ``KWP @ 10400``,
    ``UDS``); "" before it answered."""
    if m is None or not getattr(m, "reached", None) or not getattr(m, "protocol", ""):
        return ""
    if m.protocol == "uds":
        return "UDS"
    short = "KWP" if m.protocol == "kwp2000" else m.protocol.upper()
    if getattr(m, "transport", "") == "can":
        return "%s-TP2.0" % short
    baud = as_int((getattr(m, "extra", None) or {}).get("baud"), None)
    return "%s @ %d" % (short if m.protocol == "kwp2000" else "KW1281", baud) if baud else short


def address_tags(row: dict) -> List[str]:
    """``K-line 0x17``, ``TP 2.0 0x07``, ``UDS 0x7E0``: the addresses a table
    row knows (not a protocol any module has confirmed)."""
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


def _all_intermittent(m) -> bool:
    return bool(m.faults) and all(getattr(f, "intermittent", False) for f in m.faults)


# ------------------------------------------------------------------ card
class ModuleCard(ClickableFrame):
    """One module of the rail. ``kind``: ``module`` (answered), ``absent``
    (visited, did not answer) or ``known`` (an address-table row only)."""
    navigate = QtCore.Signal(int)

    def __init__(self, addr: str, name: str, sub: str = "", chip: str = "", badge=None, dim: bool = False,
                 kind: str = "module", tip: str = "", chip_tip: str = "", hay: str = "", parent=None):
        super().__init__(parent, radius=8)
        self.addr, self.kind, self.hay = addr, kind, hay.lower()
        self.setObjectName("module_card_" + addr)
        self.setMinimumHeight(54)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(8)
        self.addr_label = QtWidgets.QLabel(addr)
        self.addr_label.setFont(mono_font(self.addr_label.font(), 0.95))
        self.addr_label.setStyleSheet("color: palette(placeholder-text);")
        self.addr_label.setFixedWidth(22)
        lay.addWidget(self.addr_label)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(1)
        self.name_label = ElidedLabel(name)
        self.sub_label = ElidedLabel(sub)
        self.sub_label.setFont(mono_font(self.sub_label.font(), 0.82))
        self.sub_label.setStyleSheet("color: palette(placeholder-text);")
        if dim:
            self.name_label.setStyleSheet("color: palette(placeholder-text);")
        col.addStretch(1)
        col.addWidget(self.name_label)
        col.addWidget(self.sub_label)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.chip: Optional[Chip] = None
        if chip:
            self.chip = Chip(chip, "neutral", mono=True)
            self.chip.setToolTip(chip_tip)
            lay.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.badge: Optional[QtWidgets.QWidget] = None
        slot = QtWidgets.QWidget()                   # a fixed column, so the protocol chips line up
        slot.setFixedWidth(26)
        sl = QtWidgets.QHBoxLayout(slot)
        sl.setContentsMargins(0, 0, 0, 0)
        if badge is not None:
            if badge[0] == "count":
                self.badge = CountBadge(badge[1], badge[2])
            else:
                self.badge = Dot("ok", 9)
            sl.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignCenter)
        slot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(slot, 0, Qt.AlignmentFlag.AlignVCenter)
        for w in (self.addr_label, self.name_label, self.sub_label, self.chip, self.badge):
            if w is not None:
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setToolTip(tip)

    # what the tests read
    def name(self) -> str:
        return self.name_label.fullText()

    def sub(self) -> str:
        return self.sub_label.fullText()

    def chip_text(self) -> str:
        return self.chip.text() if self.chip is not None else ""

    def badge_count(self) -> int:
        return self.badge._n if isinstance(self.badge, CountBadge) else 0

    def badge_kind(self) -> str:
        if isinstance(self.badge, CountBadge):
            return self.badge._kind
        return "ok" if isinstance(self.badge, Dot) else ""

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.navigate.emit(1 if ev.key() == Qt.Key.Key_Down else -1)
            return
        super().keyPressEvent(ev)


class _FlatRow(ClickableFrame):
    """A footer row of the rail: no border, a hover fill."""

    def paintEvent(self, _ev):
        if self._hover or self.hasFocus():
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tokens(self.palette()).card_hover)
            p.drawRoundedRect(QtCore.QRectF(self.rect()), 6, 6)


class _GroupHeader(QtWidgets.QPushButton):
    """``> Not answering (3)``: a collapsible group's header."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title = ""
        self.setFlat(True)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QPushButton{text-align:left; border:0; padding:4px 2px; color:palette(placeholder-text);}"
                           "QPushButton:hover{color:palette(window-text);}")
        self.toggled.connect(lambda _on: self._refresh())

    def setTitle(self, text: str):
        self._title = text
        self._refresh()

    def _refresh(self):
        self.setText(("▾ " if self.isChecked() else "▸ ") + self._title)


# ------------------------------------------------------------------ rail
class ModuleRail(QtWidgets.QWidget):
    selected = QtCore.Signal(str)
    connectRequested = QtCore.Signal(str)
    connectByAddressRequested = QtCore.Signal()
    scanRequested = QtCore.Signal()
    stopRequested = QtCore.Signal()
    obdRequested = QtCore.Signal()
    garageRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        style_panel(self, "diag_rail", "right")
        self.setFixedWidth(RAIL_WIDTH)
        self._rows: List[dict] = []
        self._infos: dict = {}
        self._lives: set = set()
        self._absent: set = set()
        self._tips: Dict[str, str] = {}
        self._cards: Dict[str, ModuleCard] = {}
        self._current = ""
        self._filter = ""
        self._scanning = False
        self._summary = ""
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(8)
        head = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel("Vehicle")
        self.title.setFont(scaled_font(self.title.font(), 1.05, True))
        head.addWidget(self.title)
        head.addStretch(1)
        self.scan_btn = QtWidgets.QPushButton("Scan all")
        self.scan_btn.setToolTip("Identify every module of the table and read its faults (F5)")
        style_button(self.scan_btn, "primary")
        self.scan_btn.clicked.connect(self._scan_clicked)
        head.addWidget(self.scan_btn)
        lay.addLayout(head)
        self.summary = muted_label("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        # the scrolling list
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{background:transparent; border:0;}"
                                   "QScrollArea>QWidget>QWidget{background:transparent;}")
        body = QtWidgets.QWidget()
        self._body = QtWidgets.QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(6)
        self.empty_label = QtWidgets.QLabel(EMPTY_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: palette(placeholder-text);")
        self.empty_label.setContentsMargins(2, 6, 2, 6)
        self._body.addWidget(self.empty_label)
        self._main_lay = QtWidgets.QVBoxLayout()
        self._main_lay.setSpacing(6)
        self._body.addLayout(self._main_lay)
        self.absent_head = _GroupHeader()
        self.absent_head.toggled.connect(self._group_toggled)
        self._absent_box = QtWidgets.QWidget()
        self._absent_lay = QtWidgets.QVBoxLayout(self._absent_box)
        self._absent_lay.setContentsMargins(0, 0, 0, 0)
        self._absent_lay.setSpacing(6)
        self._body.addWidget(self.absent_head)
        self._body.addWidget(self._absent_box)
        self.known_head = _GroupHeader()
        self.known_head.toggled.connect(self._group_toggled)
        self._known_box = QtWidgets.QWidget()
        self._known_lay = QtWidgets.QVBoxLayout(self._known_box)
        self._known_lay.setContentsMargins(0, 0, 0, 0)
        self._known_lay.setSpacing(6)
        self._body.addWidget(self.known_head)
        self._body.addWidget(self._known_box)
        self._body.addStretch(1)
        self._scroll.setWidget(body)
        lay.addWidget(self._scroll, 1)
        self.connect_addr_btn = QtWidgets.QPushButton("+ Connect by address...")
        self.connect_addr_btn.setToolTip("Open a session on a module that is not in the list: type its address")
        self.connect_addr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        restyle(self.connect_addr_btn, lambda: button_qss("ghost") + "QPushButton{text-align:left;}")
        self.connect_addr_btn.clicked.connect(self.connectByAddressRequested)
        lay.addWidget(self.connect_addr_btn)
        # the foot: generic OBD-II and the garage
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
        line.setStyleSheet("color: palette(midlight);")
        lay.addWidget(line)
        self.obd_row = _FlatRow(radius=6)
        self.obd_row.setObjectName("rail_obd")
        ol = QtWidgets.QHBoxLayout(self.obd_row)
        ol.setContentsMargins(8, 6, 8, 6)
        tag = QtWidgets.QLabel("OBD")
        tag.setFont(mono_font(tag.font(), 0.9))
        tag.setStyleSheet("color: palette(placeholder-text);")
        ol.addWidget(tag)
        ol.addWidget(QtWidgets.QLabel("Generic-II"))
        ol.addStretch(1)
        self.obd_chip = Chip("ISO 9141", "neutral", mono=True)
        ol.addWidget(self.obd_chip)
        for w in (tag, self.obd_chip):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        ol.itemAt(1).widget().setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.obd_row.setToolTip("Generic emissions diagnostics (OBD-II)")
        self.obd_row.clicked.connect(self.obdRequested)
        lay.addWidget(self.obd_row)
        self.garage_row = _FlatRow(radius=6)
        self.garage_row.setObjectName("rail_garage")
        gl = QtWidgets.QHBoxLayout(self.garage_row)
        gl.setContentsMargins(8, 6, 8, 6)
        self.garage_label = QtWidgets.QLabel("Garage")
        self.garage_scans = QtWidgets.QLabel("")
        self.garage_scans.setStyleSheet("color: palette(placeholder-text);")
        gl.addWidget(self.garage_label)
        gl.addStretch(1)
        gl.addWidget(self.garage_scans)
        for w in (self.garage_label, self.garage_scans):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.garage_row.setToolTip("The vehicles and scans this copy of OVT Diag has saved")
        self.garage_row.clicked.connect(self.garageRequested)
        lay.addWidget(self.garage_row)
        self.set_footer("ISO 9141", 0, 0)
        self.set_modules([], {}, set(), set())

    # ------------------------------------------------------------ content
    def set_modules(self, rows: Sequence[dict], infos: dict, lives, absent, tips: Optional[dict] = None):
        self._rows = list(rows)
        self._infos = dict(infos or {})
        self._lives = set(lives or ())
        self._absent = set(absent or ())
        self._tips = dict(tips or {})
        self._rebuild()

    def _clear(self, layout: QtWidgets.QLayout):
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self):
        for lay in (self._main_lay, self._absent_lay, self._known_lay):
            self._clear(lay)
        self._cards = {}
        main, absent, known = [], [], []
        seen = set()
        for row in self._rows:
            addr = row["addr"]
            seen.add(addr)
            info = self._infos.get(addr)
            if info is not None and info.reached:
                main.append((row, info))
            elif info is not None and info.reached is False:
                absent.append((row, info))
            else:
                known.append((row, info))
        for addr, info in self._infos.items():          # a module connected by address, not in the table
            if addr in seen:
                continue
            row = {"addr": addr, "name": info.name or "Module %s" % addr}
            if info.reached:
                main.append((row, info))
            elif info.reached is False:
                absent.append((row, info))
        for row, info in main:
            self._add(self._main_lay, self._module_card(row, info))
        for row, info in absent:
            self._add(self._absent_lay, self._absent_card(row, info))
        self.empty_label.setVisible(not main and not absent)
        self.absent_head.setTitle("Not answering (%d)" % len(absent))
        self.absent_head.setVisible(bool(absent))
        self._n_known = len(known)
        self._known_rows = known
        self.known_head.setTitle("All known addresses (%d)" % len(known))
        self.known_head.setVisible(bool(known))
        self._build_known()
        if self._current and self._current not in self._cards and not self._is_known_addr(self._current):
            self._current = ""
        for c in self._cards.values():
            c.setSelected(c.addr == self._current)
        self._apply_filter()

    def _is_known_addr(self, addr: str) -> bool:
        return any(r["addr"] == addr for r, _i in getattr(self, "_known_rows", []))

    def _build_known(self):
        self._clear(self._known_lay)
        for addr in [a for a, c in self._cards.items() if c.kind == "known"]:
            self._cards.pop(addr, None)
        if not (self.known_head.isChecked() or self._filter):
            return
        for row, info in self._known_rows:
            self._add(self._known_lay, self._known_card(row, info))
        for c in self._cards.values():
            c.setSelected(c.addr == self._current)

    def _add(self, layout, card: ModuleCard):
        card.clicked.connect(lambda a=card.addr: self.select(a))
        card.doubleClicked.connect(lambda a=card.addr: (self.select(a), self.connectRequested.emit(a)))
        card.navigate.connect(lambda d, a=card.addr: self._navigate(a, d))
        self._cards[card.addr] = card
        layout.addWidget(card)

    def _module_card(self, row: dict, info) -> ModuleCard:
        addr = row["addr"]
        sub = [info.part or info.identity_text]
        if info.platform and info.platform.upper() not in (info.part or "").upper():
            sub.append(info.platform)
        if addr in self._lives:
            sub.append("live")
        n = info.fault_count
        badge = ("count", n, "warn" if _all_intermittent(info) else "bad") if n else ("ok",)
        hay = " ".join([addr, row.get("name", ""), info.name, info.part, info.identity_text, info.platform,
                        " ".join("%s %s" % (f.display_code, f.text) for f in info.faults)])
        tip = self._tips.get(addr, "")
        return ModuleCard(addr, row.get("name") or info.name, " · ".join(x for x in sub if x), protocol_chip(info),
                          badge, False, "module", tip, tip, hay)

    def _absent_card(self, row: dict, info) -> ModuleCard:
        addr = row["addr"]
        reason = info.error or "no answer"
        return ModuleCard(addr, row.get("name") or info.name, reason, "", None, True, "absent",
                          self._tips.get(addr) or ("Did not answer: " + reason), "",
                          " ".join([addr, row.get("name", ""), reason]))

    def _known_card(self, row: dict, info) -> ModuleCard:
        addr = row["addr"]
        tags = address_tags(row)
        if addr in self._absent:
            sub = "not installed (gateway list)"
        else:
            sub = ", ".join(t.replace("K-line ", "K ").replace("TP 2.0 ", "TP ") for t in tags)
        tip = self._tips.get(addr) or (("Addresses known: " + ", ".join(tags)) if tags else "")
        if info is not None and info.reached is None and info.error:
            tip = (tip + "\n" if tip else "") + info.error
        return ModuleCard(addr, row.get("name", ""), sub, "", None, True, "known", tip, "",
                          " ".join([addr, row.get("name", ""), sub]))

    def set_summary(self, text: str):
        self._summary = text
        self.summary.setText(text)

    def summary_text(self) -> str:
        return self._summary

    def set_scanning(self, on: bool):
        self._scanning = bool(on)
        self.scan_btn.setText("Stop" if on else "Scan all")

    def scanning(self) -> bool:
        return self._scanning

    def _scan_clicked(self):
        (self.stopRequested if self._scanning else self.scanRequested).emit()

    def set_footer(self, obd_chip: str, vehicles: int, scans: int):
        self.obd_chip.setText(obd_chip)
        self.garage_label.setText("Garage · %d vehicle%s" % (vehicles, "" if vehicles == 1 else "s"))
        self.garage_scans.setText("%d scan%s" % (scans, "" if scans == 1 else "s"))

    # ---------------------------------------------------------- selection
    def select(self, addr: str):
        """Highlight ``addr`` (a card of the list, or of the known-address
        group, which opens for it) and tell the window; "" clears."""
        if addr and addr not in self._cards and self._is_known_addr(addr):
            self.known_head.setChecked(True)          # builds the group
        card = self._cards.get(addr)
        if card is not None and card.kind == "absent":
            self.absent_head.setChecked(True)
        if addr and addr not in self._cards:
            return
        changed = addr != self._current
        self._current = addr
        for c in self._cards.values():
            c.setSelected(c.addr == addr)
        if addr:
            self._scroll.ensureWidgetVisible(self._cards[addr])
        if changed:
            self.selected.emit(addr)

    def current_addr(self) -> str:
        return self._current

    def _navigate(self, addr: str, step: int):
        order = [a for a in self.visible_addrs()]
        if addr in order:
            nxt = order[(order.index(addr) + step) % len(order)]
            self.select(nxt)
            self._cards[nxt].setFocus()

    def _group_toggled(self, _on: bool):
        self._build_known()
        self._apply_filter()

    # ------------------------------------------------------------- filter
    def filter(self, text: str):
        self._filter = (text or "").strip().lower()
        self._build_known()
        self._apply_filter()

    def _apply_filter(self):
        t = self._filter
        for c in self._cards.values():
            c.setVisible(not t or t in c.hay)
        absent_has = any(c.kind == "absent" and c.isVisibleTo(self._absent_box) for c in self._cards.values())
        known_has = any(c.kind == "known" and c.isVisibleTo(self._known_box) for c in self._cards.values())
        self.absent_head.setVisible(self._absent_lay.count() > 0 and (not t or absent_has))
        self._absent_box.setVisible(absent_has and (self.absent_head.isChecked() or bool(t)))
        n_known = getattr(self, "_n_known", 0)
        self.known_head.setVisible(n_known > 0 and (not t or self._known_has_match()))
        self._known_box.setVisible(known_has and (self.known_head.isChecked() or bool(t)))

    def _known_has_match(self) -> bool:
        return any(self._filter in " ".join([r["addr"], r.get("name", "")] + address_tags(r)).lower()
                   for r, _i in getattr(self, "_known_rows", []))

    # ------------------------------------------------------------- inspect
    def card_addrs(self, kind: Optional[str] = None) -> List[str]:
        """The addresses that have a card (built), in list order; ``kind``
        narrows to ``module`` / ``absent`` / ``known``."""
        out = []
        for lay in (self._main_lay, self._absent_lay, self._known_lay):
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if isinstance(w, ModuleCard) and (kind is None or w.kind == kind):
                    out.append(w.addr)
        return out

    def visible_addrs(self) -> List[str]:
        out = []
        for a in self.card_addrs():
            c = self._cards[a]
            if c.isVisibleTo(self) and c.parentWidget().isVisibleTo(self):
                out.append(a)
        return out

    def card(self, addr: str) -> Optional[ModuleCard]:
        return self._cards.get(addr)

    def known_count(self) -> int:
        return getattr(self, "_n_known", 0)

    def absent_count(self) -> int:
        return self._absent_lay.count()

    def is_empty_state(self) -> bool:
        return self.empty_label.isVisibleTo(self)


# ----------------------------------------------------- connect by address
class ConnectByAddressDialog(QtWidgets.QDialog):
    """Ask for a module's address (two hex digits) -- and, where the
    current transport needs it, the second address: the TP 2.0 destination
    over CAN, the ISO-TP request / response ids over UDS. :meth:`row`
    is the address-table row to connect with."""

    def __init__(self, transport: str, parent=None, known_rows: Sequence[dict] = ()):
        super().__init__(parent)
        self.transport = transport
        self._known = {r["addr"]: r for r in known_rows}
        self.setWindowTitle("Connect by address")
        f = QtWidgets.QFormLayout(self)
        hexre = QtCore.QRegularExpression("[0-9A-Fa-f]{1,2}")
        self.addr = QtWidgets.QLineEdit()
        self.addr.setPlaceholderText("e.g. 17")
        self.addr.setMaxLength(2)
        self.addr.setValidator(QtGui.QRegularExpressionValidator(hexre, self.addr))
        f.addRow("Module address (hex):", self.addr)
        self.dest = QtWidgets.QLineEdit()
        self.req = QtWidgets.QLineEdit()
        self.resp = QtWidgets.QLineEdit()
        id_re = QtGui.QRegularExpressionValidator(QtCore.QRegularExpression("[0-9A-Fa-f]{1,3}"), self)
        if transport == "can":
            self.dest.setValidator(QtGui.QRegularExpressionValidator(hexre, self.dest))
            self.dest.setPlaceholderText("same as the address")
            f.addRow("TP 2.0 destination (hex):", self.dest)
        elif transport == "uds":
            for e, ph in ((self.req, "e.g. 7E0"), (self.resp, "e.g. 7E8")):
                e.setValidator(id_re)
                e.setPlaceholderText(ph)
            f.addRow("ISO-TP request id (hex):", self.req)
            f.addRow("ISO-TP response id (hex):", self.resp)
        how = {"kline": "K-line (the address is the number the module is woken with)",
               "can": "CAN (TP 2.0)", "uds": "UDS over ISO-TP"}.get(transport, transport)
        note = muted_label("Over " + how + ". The protocol is whatever the module answers with.")
        note.setWordWrap(True)
        f.addRow(note)
        self.bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        f.addRow(self.bb)
        self.addr.textChanged.connect(self._validate)
        self.dest.textChanged.connect(self._validate)
        self.req.textChanged.connect(self._validate)
        self.resp.textChanged.connect(self._validate)
        self._validate()

    def _ok(self) -> bool:
        if not self.addr.text().strip():
            return False
        if self.transport == "uds":
            return bool(self.req.text().strip() and self.resp.text().strip())
        return True

    def _validate(self):
        self.bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(self._ok())

    def row(self) -> Optional[dict]:
        """The row for what was typed, or None while it is incomplete. A
        module the table lists is that row, so what is known about it
        (header, bus) comes along; any other is ``Module XX``."""
        if not self._ok():
            return None
        n = int(self.addr.text().strip(), 16)
        def num(edit):
            return int(edit.text().strip(), 16) if edit.text().strip() else None
        return row_for_address(self.transport, n, num(self.dest), num(self.req), num(self.resp), self._known)


def row_for_address(transport: str, number: int, dest: Optional[int] = None, request: Optional[int] = None,
                    response: Optional[int] = None, known: Optional[dict] = None) -> dict:
    """The row a connection by address uses for ``transport``: the table's
    own when ``known`` has the address, else a synthetic ``Module XX``
    carrying the address the transport needs."""
    addr = "%02X" % number
    base = (known or {}).get(addr)
    if base is not None:
        row = dict(base)
    else:
        row = {"addr": addr, "name": "Module %s" % addr}
    if transport == "kline":
        if base is None or not isinstance(row.get("kline"), dict):
            row["kline"] = {"address": number, "init": "slow"}
    elif transport == "can":
        d = number if dest is None else dest
        if base is None or (row.get("tp20") or {}).get("dest") is None or dest is not None:
            row["tp20"] = dict(row.get("tp20") or {}, dest=d)
    else:
        if request is not None and response is not None:
            row["isotp"] = dict(row.get("isotp") or {}, request=request, response=response)
    return row


__all__ = ["ModuleRail", "ModuleCard", "ConnectByAddressDialog", "row_for_address", "protocol_chip",
           "address_tags", "RAIL_WIDTH", "EMPTY_TEXT"]
