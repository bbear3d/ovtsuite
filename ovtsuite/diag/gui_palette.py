# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The command palette overlay (Ctrl+K), docs/OVT Diag Interface Options.pdf
page 2: the workspace dims behind a centred box with one search field,
filter chips with counts (``All . 9`` ``Live values . 4`` ...), results grouped
under small headings, and a footer of key hints.

An overlay *child* of the OVT Diag window (not a separate window): it covers
its parent, dims it, and gives the keyboard to the search field. Qt only; the
searching is :mod:`ovtsuite.diag.palette_index`.

THE CONTRACT with :class:`ovtsuite.diag.gui_diag.DiagWindow` (keep it stable):

``CommandPalette(parent)``
signals
    ``activated(object, bool)``   the chosen :class:`PaletteItem` and whether
                                  Shift+Enter was used ("add to live view without leaving")
    ``dismissed()``               closed without a choice (Esc, click on the dim)
methods
    ``popup(items, prefill="")``  fill with ``items``, show over the parent,
                                  focus the field (optionally pre-typed)
    ``set_items(items)``          replace the items while open (live values changed)
    ``set_values(mapping)``       ``{(addr, kind, key): text}`` updating the right-hand
                                  value of matching items without re-searching
    ``close_palette()``           hide
    ``is_open() -> bool``
Keys: Up/Down move, Enter activates, Shift+Enter activates with ``add=True``,
Esc closes, Tab / Shift+Tab step through the filter chips; the footer shows
``move   Enter open   Shift+Enter add to live view without leaving`` and on the
right the prefix help ``Prefixes: 01: module   P0299 code   > action``.

How it behaves beyond the contract:

* An activation (Enter, Shift+Enter, a click on a row) closes the palette
  first and then emits ``activated``; the window re-opens it if it wants it
  to stay. ``close_palette()`` itself emits nothing.
* ``set_values`` keys: ``(addr, kind, key)`` where ``key`` is the item's
  ``payload["key"]`` (or ``payload["name"]``), else its title, else
  ``(group, cell)`` / ``"group:cell"`` taken from the payload.
* The rows are one model painted by one delegate, so a thousand results are
  a thousand list rows, not widgets; each group shows at most
  :data:`GROUP_CAP` hits and a muted ``N more...`` row for the rest.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .gui_widgets import _mix, _Themed, mono_font, muted_label, restyle, scaled_font, tokens
from .palette_index import GROUPS, Hit, PaletteItem, Result, search

Qt = QtCore.Qt

GROUP_CAP = 60                      # hits shown per group before "N more..."
BOX_WIDTH = 840
_PAGE = 8                           # rows per Page Up / Page Down

# the filter chips, in the order the mockup lists them
CHIP_ORDER = ("live", "fault", "module", "coding", "adaptation", "actuator", "procedure", "action")
CHIP_LABELS = {"all": "All", "live": "Live values", "fault": "Faults", "module": "Modules",
               "coding": "Coding", "adaptation": "Adaptation", "actuator": "Actuator tests",
               "procedure": "Procedures", "action": "Actions"}
_CHIP_KIND = {"active": "bad", "history": "ok", "warn": "warn", "stored": "warn"}
_ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _c(col: QtGui.QColor) -> str:
    return col.name(QtGui.QColor.NameFormat.HexArgb)


# --------------------------------------------------------------------- model
class _Row:
    """One line of the list: a heading, a hit, the ``N more...`` row or the
    empty-result message."""
    __slots__ = ("type", "text", "hit", "value")

    def __init__(self, type_: str, text: str = "", hit: Optional[Hit] = None, value: str = ""):
        self.type, self.text, self.hit, self.value = type_, text, hit, value


def _chip_kind(item: PaletteItem) -> str:
    return item.chip_kind or _CHIP_KIND.get((item.chip or "").lower(), "neutral")


def _right_text(row: _Row) -> str:
    it = row.hit.item
    return it.chip or row.value or it.target_text().replace(" >", "")


def _row_text(row: _Row) -> str:
    if row.type != "hit":
        return row.text
    it = row.hit.item
    parts = [it.addr or "", it.title, it.subtitle, _right_text(row)]
    return ", ".join(p for p in parts if p)


class _Model(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: List[_Row] = []

    def set_rows(self, rows: List[_Row]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):                    # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        row = self.rows[index.row()]
        if role == _ROW_ROLE:
            return row
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return _row_text(row)
        return None

    def flags(self, index):
        if index.isValid() and self.rows[index.row()].type == "hit":
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.NoItemFlags


# ------------------------------------------------------------------ delegate
def _runs(title: str, spans: Sequence[Tuple[int, int]], limit: int) -> List[Tuple[str, bool]]:
    """``title[:limit]`` split into ``(text, highlighted)`` runs."""
    out: List[Tuple[str, bool]] = []
    pos = 0
    for a, b in spans:
        a, b = min(a, limit), min(b, limit)
        if a > pos:
            out.append((title[pos:a], False))
        if b > a:
            out.append((title[a:b], True))
        pos = max(pos, b)
    if pos < limit:
        out.append((title[pos:limit], False))
    return out


class _Delegate(QtWidgets.QStyledItemDelegate):
    """Paints the rows (mockup page 2): module address, title with the match
    in the accent colour, muted subtitle, and on the right a live value, a
    status chip or the target tab."""

    PAD_L, ADDR_W, PAD_R = 18, 34, 20

    def __init__(self, view: QtWidgets.QListView):
        super().__init__(view)
        self._view = view

    # fonts follow the view's font so a larger application font scales the rows
    def _fonts(self):
        base = self._view.font()
        return (base, scaled_font(base, 0.86), mono_font(base, 0.85), mono_font(base, 1.0),
                scaled_font(base, 0.86), scaled_font(base, 0.80))

    def height_for(self, row: _Row) -> int:
        base = self._view.font()
        fm = QtGui.QFontMetrics(base)
        if row.type == "hit":
            return fm.height() + QtGui.QFontMetrics(scaled_font(base, 0.86)).height() + 17
        if row.type == "empty":
            return fm.height() * 4
        return fm.height() + 16 if row.type == "head" else fm.height() + 10

    def sizeHint(self, option, index):                                  # noqa: N802
        row = index.data(_ROW_ROLE)
        return QtCore.QSize(option.rect.width(), self.height_for(row) if row else 24)

    def paint(self, p: QtGui.QPainter, option, index):
        row: _Row = index.data(_ROW_ROLE)
        if row is None:
            return
        t = tokens(option.palette)
        r = QtCore.QRectF(option.rect)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setClipRect(r)
        base, small, mono_s, mono, chipf, hintf = self._fonts()
        if row.type == "head":
            f = scaled_font(base, 0.78, True)
            f.setLetterSpacing(QtGui.QFont.SpacingType.PercentageSpacing, 104)
            p.setFont(f)
            p.setPen(t.muted)
            p.drawText(QtCore.QRectF(r.left() + self.PAD_L, r.top() + 6, r.width() - 2 * self.PAD_L, r.height() - 6),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, row.text.upper())
        elif row.type in ("more", "empty"):
            p.setFont(small if row.type == "more" else base)
            p.setPen(t.muted)
            if row.type == "more":
                p.drawText(r.adjusted(self.PAD_L + self.ADDR_W, 0, -self.PAD_R, 0),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, row.text)
            else:
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, row.text)
        else:
            self._paint_hit(p, option, r, row, t, (base, small, mono_s, mono, chipf, hintf))
        p.restore()

    def _paint_hit(self, p, option, r, row: _Row, t, fonts):
        base, small, mono_s, mono, chipf, hintf = fonts
        it = row.hit.item
        current = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        body = r.adjusted(8, 1, -8, -1)
        if current:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_mix(t.card, t.accent, 0.16 if t.dark else 0.10))
            p.drawRoundedRect(body, 6, 6)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.card_hover)
            p.drawRoundedRect(body, 6, 6)

        rx = r.right() - self.PAD_R
        # ---- the right-hand block: hint, then value / chip / target
        if it.chip:
            p.setFont(chipf)
            fm = QtGui.QFontMetricsF(chipf)
            w, h = fm.horizontalAdvance(it.chip) + 16, fm.height() + 6
            cr = QtCore.QRectF(rx - w, r.center().y() - h / 2, w, h)
            fg, fill, edge = t.kind_colors(_chip_kind(it))
            p.setBrush(fill)
            p.setPen(QtGui.QPen(edge, 1))
            p.drawRoundedRect(cr.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            p.setPen(fg)
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, it.chip)
            rx -= w
        else:
            if current and it.kind == "live":
                p.setFont(hintf)
                p.setPen(t.muted)
                hint = "Enter · add to view"
                w = QtGui.QFontMetricsF(hintf).horizontalAdvance(hint)
                p.drawText(QtCore.QRectF(rx - w, r.top(), w, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, hint)
                rx -= w + 14
            val = row.value or ("–" if it.kind == "live" else "")
            if val:
                p.setFont(mono)
                p.setPen(t.text if row.value else t.faint)
                w = QtGui.QFontMetricsF(mono).horizontalAdvance(val)
                p.drawText(QtCore.QRectF(rx - w, r.top(), w, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, val)
                rx -= w
            else:
                tgt = it.target_text()
                if tgt.endswith(" >"):
                    tgt = tgt[:-2] + " ›"
                if tgt:
                    p.setFont(chipf)
                    p.setPen(t.muted)
                    w = QtGui.QFontMetricsF(chipf).horizontalAdvance(tgt)
                    p.drawText(QtCore.QRectF(rx - w, r.top(), w, r.height()),
                               Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, tgt)
                    rx -= w

        # ---- the module address
        p.setFont(mono_s)
        p.setPen(t.faint)
        p.drawText(QtCore.QRectF(r.left() + self.PAD_L, r.top(), self.ADDR_W, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, it.addr or "–")

        # ---- title (match highlighted) over subtitle
        x = r.left() + self.PAD_L + self.ADDR_W
        avail = max(20.0, rx - 18 - x)
        tfm, sfm = QtGui.QFontMetricsF(base), QtGui.QFontMetricsF(small)
        top = r.top() + (r.height() - (tfm.height() + sfm.height() + 1)) / 2
        title = it.title
        shown = QtGui.QFontMetricsF(base).elidedText(title, Qt.TextElideMode.ElideRight, avail)
        limit = len(title) if shown == title else max(0, len(shown) - 1)
        p.setFont(base)
        cx, by = x, top + tfm.ascent()
        for text, hot in _runs(title, row.hit.spans, limit):
            p.setPen(t.accent if hot else t.text)
            p.drawText(QtCore.QPointF(cx, by), text)
            cx += tfm.horizontalAdvance(text)
        if shown != title:
            p.setPen(t.text)
            p.drawText(QtCore.QPointF(cx, by), "…")
        if it.subtitle:
            p.setFont(small)
            p.setPen(t.muted)
            p.drawText(QtCore.QPointF(x, top + tfm.height() + 1 + sfm.ascent()),
                       sfm.elidedText(it.subtitle, Qt.TextElideMode.ElideRight, avail))


# ------------------------------------------------------- small painted parts
class _Magnifier(_Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(t.muted, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QtCore.QPointF(11.5, 11.5), 7.0, 7.0)
        p.drawLine(QtCore.QPointF(16.6, 16.6), QtCore.QPointF(21.5, 21.5))


class _KeyCap(_Themed):
    """A small bordered key name (``Esc``)."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def _font(self):
        return mono_font(self.font(), 0.82)

    def sizeHint(self):                                                  # noqa: N802
        fm = QtGui.QFontMetrics(self._font())
        return QtCore.QSize(fm.horizontalAdvance(self._text) + 16, fm.height() + 8)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(t.border_strong, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 4, 4)
        p.setFont(self._font())
        p.setPen(t.muted)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)


class _Sep(_Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), tokens(self.palette()).border)


class _ChipBar(_Themed):
    """The filter chips: ``All . 9``, ``Live values . 4`` ...; the current one
    filled. Only kinds with matches are offered."""

    changed = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys: List[str] = ["all"]
        self._counts: Dict[str, int] = {}
        self._current = "all"
        self._rects: List[Tuple[str, QtCore.QRectF]] = []
        self._hover = ""
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Filter")

    def set_state(self, counts: Dict[str, int], current: str):
        self._counts = dict(counts)
        self._keys = ["all"] + [k for k in CHIP_ORDER if counts.get(k)]
        self._keys += [k for k in sorted(counts) if k != "all" and k not in self._keys and counts[k]]
        self._current = current if current in self._keys else "all"
        self.setAccessibleDescription(", ".join(self.labels()))
        self.update()

    def keys(self) -> List[str]:
        return list(self._keys)

    def current(self) -> str:
        return self._current

    def label(self, key: str) -> str:
        return "%s · %d" % (CHIP_LABELS.get(key, key.title()), self._counts.get(key, 0))

    def labels(self) -> List[str]:
        return [self.label(k) for k in self._keys]

    def _font(self):
        return scaled_font(self.font(), 0.95)

    def _layout(self):
        fm = QtGui.QFontMetricsF(self._font())
        x, out = 16.0, []
        h = fm.height() + 10
        y = (self.height() - h) / 2
        for k in self._keys:
            w = fm.horizontalAdvance(self.label(k)) + 22
            out.append((k, QtCore.QRectF(x, y, w, h)))
            x += w + 4
        self._rects = out

    def _key_at(self, pos) -> str:
        for k, r in self._rects:
            if r.contains(QtCore.QPointF(pos)):
                return k
        return ""

    def mouseMoveEvent(self, ev):                                        # noqa: N802
        k = self._key_at(ev.position())
        if k != self._hover:
            self._hover = k
            self.setCursor(Qt.CursorShape.PointingHandCursor if k else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, ev):                                            # noqa: N802
        self._hover = ""
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):                                       # noqa: N802
        k = self._key_at(ev.position())
        if k and ev.button() == Qt.MouseButton.LeftButton:
            self.changed.emit(k)
        ev.accept()

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        self._layout()
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setFont(self._font())
        for k, r in self._rects:
            on = k == self._current
            if on:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_mix(t.card, t.accent, 0.30 if t.dark else 0.16))
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            p.setPen(t.text if on or k == self._hover else t.muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.label(k))


class _Footer(_Themed):
    """``move   Enter open   Shift+Enter add to live view without leaving`` on
    the left, ``Prefixes: 01: module   P0299 code   > action`` on the right
    (the key names in the monospace face)."""

    LEFT = [("↑↓", False), (" move", False), ("      ", False), ("Enter", True), (" open", False),
            ("      ", False), ("Shift Enter", True), (" add to live view without leaving", False)]
    RIGHT = [("Prefixes: ", False), ("01:", True), (" module  ·  ", False), ("P0299", True),
             (" code  ·  ", False), (">", True), (" action", False)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)

    def _width(self, segs, fm, mfm):
        return sum((mfm if mono else fm).horizontalAdvance(s) for s, mono in segs)

    def _draw(self, p, x, y, segs, fm, mfm, f, mf, col):
        p.setPen(col)
        for s, mono in segs:
            p.setFont(mf if mono else f)
            p.drawText(QtCore.QPointF(x, y), s)
            x += (mfm if mono else fm).horizontalAdvance(s)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        f, mf = scaled_font(self.font(), 0.88), mono_font(self.font(), 0.82)
        fm, mfm = QtGui.QFontMetricsF(f), QtGui.QFontMetricsF(mf)
        y = (self.height() + fm.ascent() - fm.descent()) / 2
        lw, rw = self._width(self.LEFT, fm, mfm), self._width(self.RIGHT, fm, mfm)
        self._draw(p, 18, y, self.LEFT, fm, mfm, f, mf, t.muted)
        if lw + rw + 60 < self.width():
            self._draw(p, self.width() - 18 - rw, y, self.RIGHT, fm, mfm, f, mf, t.muted)


class _Box(_Themed):
    """The rounded, bordered box in the middle."""

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(t.card)
        p.setPen(QtGui.QPen(t.border_strong, 1))
        p.drawRoundedRect(r, 12, 12)

    def mousePressEvent(self, ev):                                       # noqa: N802
        ev.accept()                                  # a click in the box is not a click on the dim


class _ResultView(QtWidgets.QListView):
    """The list; keeps the keyboard in the search field."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setUniformItemSizes(False)
        self.setAccessibleName("Results")


# -------------------------------------------------------------- the palette
class CommandPalette(QtWidgets.QFrame):
    activated = QtCore.Signal(object, bool)
    dismissed = QtCore.Signal()

    HEADER_H, CHIPS_H = 58, 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self._open = False
        self._items: List[PaletteItem] = []
        self._kind = "all"
        self._result = Result()
        self._ovr: Dict[int, str] = {}                  # id(item) -> live value text
        self._keymap: Optional[Dict[tuple, PaletteItem]] = None
        self._watched: Optional[QtWidgets.QWidget] = None
        self.setObjectName("CommandPalette")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._box = _Box(self)
        lay = QtWidgets.QVBoxLayout(self._box)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        head = QtWidgets.QWidget(self._box)
        head.setFixedHeight(self.HEADER_H)
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(18, 0, 16, 0)
        hl.setSpacing(12)
        self._edit = QtWidgets.QLineEdit(head)
        self._edit.setPlaceholderText("Search modules, readings, faults, tests, procedures…")
        self._edit.setClearButtonEnabled(False)
        self._edit.setFont(scaled_font(self.font(), 1.4))
        self._edit.setAccessibleName("Search")
        restyle(self._edit, lambda: "QLineEdit{background:transparent; border:0; padding:0; color:%s;"
                                    " selection-background-color:%s; selection-color:%s;}"
                % (_c(tokens().text), _c(tokens().accent), "#ffffff"))
        hl.addWidget(_Magnifier(head))
        hl.addWidget(self._edit, 1)
        hl.addWidget(_KeyCap("Esc", head))
        lay.addWidget(head)
        lay.addWidget(_Sep(self._box))

        self._chips = _ChipBar(self._box)
        self._chips.setFixedHeight(self.CHIPS_H)
        lay.addWidget(self._chips)
        lay.addWidget(_Sep(self._box))

        self._model = _Model(self)
        self._view = _ResultView(self._box)
        self._view.setModel(self._model)
        self._delegate = _Delegate(self._view)
        self._view.setItemDelegate(self._delegate)
        lay.addWidget(self._view, 1)
        lay.addWidget(_Sep(self._box))
        lay.addWidget(_Footer(self._box))

        # (no caption under the box: over the dimmed workspace it ran into
        # whatever the window shows there; the box says what it searches in
        # its placeholder and its footer)
        self.setFocusProxy(self._edit)
        self._edit.installEventFilter(self)
        self._edit.textChanged.connect(lambda _t: self._research())
        self._chips.changed.connect(self._set_kind)
        self._view.clicked.connect(self._on_clicked)
        self._view.viewport().installEventFilter(self)
        self.hide()

    # ---------------------------------------------------------- the contract
    def popup(self, items: Sequence[PaletteItem], prefill: str = "") -> None:
        self._items = list(items)
        self._keymap = None
        self._kind = "all"
        self._watch_parent()
        self._edit.blockSignals(True)
        self._edit.setText(prefill)
        self._edit.blockSignals(False)
        self._open = True
        self._research()
        self._fit()
        self.show()
        self.raise_()
        self._edit.setFocus(Qt.FocusReason.PopupFocusReason)
        self._edit.setCursorPosition(len(prefill))

    def set_items(self, items: Sequence[PaletteItem]) -> None:
        self._items = list(items)
        self._keymap = None
        if self._open:
            self._research(keep_current=True)

    def set_values(self, mapping: Dict[tuple, str]) -> None:
        if not mapping:
            return
        if self._keymap is None:
            self._keymap = self._build_keymap()
        changed = False
        for key, text in mapping.items():
            item = self._keymap.get(self._norm_key(key))
            if item is None:
                continue
            text = "" if text is None else str(text)
            if self._ovr.get(id(item), item.value) != text:
                self._ovr[id(item)] = text
                changed = True
        if not changed:
            return
        for row in self._model.rows:
            if row.type == "hit":
                row.value = self._ovr.get(id(row.hit.item), row.hit.item.value)
        self._view.viewport().update()

    def close_palette(self) -> None:
        self._open = False
        self.hide()

    def is_open(self) -> bool:
        return self._open

    # ------------------------------------------------------ helpers for tests
    def search_field(self) -> QtWidgets.QLineEdit:
        return self._edit

    def query(self) -> str:
        return self._edit.text()

    def kind_filter(self) -> str:
        return self._kind

    def chip_labels(self) -> List[str]:
        return self._chips.labels()

    def current_item(self) -> Optional[PaletteItem]:
        row = self._current_row()
        return row.hit.item if row is not None else None

    def row_texts(self) -> List[str]:
        """Every list line (headings, hits, ``N more...``) as text."""
        return [_row_text(r) for r in self._model.rows]

    def hit_items(self) -> List[PaletteItem]:
        return [r.hit.item for r in self._model.rows if r.type == "hit"]

    def box_geometry(self) -> QtCore.QRect:
        return self._box.geometry()

    # ------------------------------------------------------------- searching
    def _research(self, keep_current: bool = False) -> None:
        keep = self._row_key(self._current_row()) if keep_current else None
        kind = None if self._kind == "all" else self._kind
        res = search(self._items, self._edit.text(), kind)
        if kind and not res.counts.get(kind):
            self._kind = "all"
            res = search(self._items, self._edit.text(), None)
        self._result = res
        self._chips.set_state(res.counts, self._kind)

        by_kind: Dict[str, List[Hit]] = {}
        for h in res.hits:
            by_kind.setdefault(h.item.kind, []).append(h)
        rows: List[_Row] = []
        seen = set()
        for title, kinds in GROUPS:
            seen.update(kinds)
            hits = sorted((h for k in kinds for h in by_kind.get(k, [])), key=lambda h: -h.score) \
                if len(kinds) > 1 else by_kind.get(kinds[0], [])
            if not hits:
                continue
            rows.append(_Row("head", title))
            for h in hits[:GROUP_CAP]:
                rows.append(_Row("hit", hit=h, value=self._ovr.get(id(h.item), h.item.value)))
            if len(hits) > GROUP_CAP:
                rows.append(_Row("more", "%d more… keep typing to narrow" % (len(hits) - GROUP_CAP)))
        rest = [h for k, hs in by_kind.items() if k not in seen for h in hs]       # unknown kinds
        if rest:
            rows.append(_Row("head", "OTHER"))
            rows.extend(_Row("hit", hit=h, value=h.item.value) for h in rest[:GROUP_CAP])
        if not rows:
            q = self._edit.text().strip()
            rows.append(_Row("empty", "Nothing matches “%s”." % q if q else "Nothing to show."))
        self._model.set_rows(rows)

        target = None
        if keep is not None:
            target = next((i for i, r in enumerate(rows) if r.type == "hit" and self._row_key(r) == keep), None)
        if target is None:
            target = next((i for i, r in enumerate(rows) if r.type == "hit"), None)
        if target is not None:
            self._select(target, top=keep is None)
        else:
            self._view.setCurrentIndex(QtCore.QModelIndex())
        self._describe()
        if self._open:
            self._fit()

    @staticmethod
    def _row_key(row: Optional[_Row]):
        if row is None or row.type != "hit":
            return None
        it = row.hit.item
        return (it.kind, it.addr, it.title)

    def _set_kind(self, key: str) -> None:
        if key != self._kind:
            self._kind = key
            self._research()

    def _cycle_chip(self, step: int) -> None:
        keys = self._chips.keys()
        if len(keys) < 2:
            return
        cur = keys.index(self._kind) if self._kind in keys else 0
        self._set_kind(keys[(cur + step) % len(keys)])

    # ------------------------------------------------------------- selection
    def _current_row(self) -> Optional[_Row]:
        i = self._view.currentIndex()
        if not i.isValid():
            return None
        row = self._model.rows[i.row()]
        return row if row.type == "hit" else None

    def _hit_rows(self) -> List[int]:
        return [i for i, r in enumerate(self._model.rows) if r.type == "hit"]

    def _select(self, row: int, top: bool = False) -> None:
        idx = self._model.index(row)
        self._view.setCurrentIndex(idx)
        if top and row == (self._hit_rows() or [-1])[0]:
            self._view.verticalScrollBar().setValue(0)
        else:
            self._view.scrollTo(idx, QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
        self._describe()

    def _move(self, step: int, page: bool = False, edge: str = "") -> None:
        hits = self._hit_rows()
        if not hits:
            return
        cur = self._view.currentIndex().row()
        pos = hits.index(cur) if cur in hits else -1
        if edge == "home":
            new = 0
        elif edge == "end":
            new = len(hits) - 1
        elif page:
            new = max(0, min(len(hits) - 1, pos + step * _PAGE if pos >= 0 else (0 if step > 0 else len(hits) - 1)))
        elif pos < 0:
            new = 0 if step > 0 else len(hits) - 1
        else:
            new = (pos + step) % len(hits)
        self._select(hits[new], top=new == 0)

    def _describe(self) -> None:
        row = self._current_row()
        self._view.setAccessibleDescription(_row_text(row) if row else "")

    def _activate(self, add: bool) -> None:
        item = self.current_item()
        if item is None:
            return
        self.close_palette()
        self.activated.emit(item, add)

    def _dismiss(self) -> None:
        self.close_palette()
        self.dismissed.emit()

    def _on_clicked(self, index) -> None:
        row = self._model.rows[index.row()] if index.isValid() else None
        if row is None or row.type != "hit":
            return
        self._view.setCurrentIndex(index)
        self._activate(bool(QtWidgets.QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier))

    def _on_hover(self, pos) -> None:
        """The mouse moved over the list: the row under it becomes current. (Only real mouse
        moves count: a list scrolled by the keyboard under a resting mouse must not jump.)"""
        index = self._view.indexAt(pos)
        if index.isValid() and self._model.rows[index.row()].type == "hit"                 and index != self._view.currentIndex():
            self._view.setCurrentIndex(index)
            self._describe()

    # -------------------------------------------------------------- keyboard
    def _handle_key(self, ev: QtGui.QKeyEvent) -> bool:
        k, mods = ev.key(), ev.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if k == Qt.Key.Key_Escape or (ctrl and k == Qt.Key.Key_K):
            self._dismiss()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate(bool(mods & Qt.KeyboardModifier.ShiftModifier))
        elif k == Qt.Key.Key_Down:
            self._move(1)
        elif k == Qt.Key.Key_Up:
            self._move(-1)
        elif k == Qt.Key.Key_PageDown:
            self._move(1, page=True)
        elif k == Qt.Key.Key_PageUp:
            self._move(-1, page=True)
        elif k == Qt.Key.Key_Home and not ctrl:
            self._move(0, edge="home")
        elif k == Qt.Key.Key_End and not ctrl:
            self._move(0, edge="end")
        elif k == Qt.Key.Key_Tab:
            self._cycle_chip(1)
        elif k == Qt.Key.Key_Backtab:
            self._cycle_chip(-1)
        else:
            return False
        return True

    def eventFilter(self, obj, ev):                                      # noqa: N802
        t = ev.type()
        if obj is self._edit:
            if t == QtCore.QEvent.Type.KeyPress and self._handle_key(ev):
                return True
            if t == QtCore.QEvent.Type.ShortcutOverride:
                k = ev.key()
                if k == Qt.Key.Key_Escape or (k == Qt.Key.Key_K
                                              and ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
                    ev.accept()                    # keep a window-level Esc / Ctrl+K from stealing it
                    return True
        elif obj is self._view.viewport():
            if t == QtCore.QEvent.Type.MouseMove:
                self._on_hover(ev.position().toPoint())
        elif obj is self._watched and t == QtCore.QEvent.Type.Resize and self._open:
            self._fit()
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, ev):                                         # noqa: N802
        if not self._handle_key(ev):
            QtWidgets.QApplication.sendEvent(self._edit, ev)             # typing goes to the field

    def mousePressEvent(self, ev):                                       # noqa: N802
        if not self._box.geometry().contains(ev.position().toPoint()):
            self._dismiss()
        ev.accept()

    def wheelEvent(self, ev):                                            # noqa: N802
        ev.accept()                                                      # the dim eats scrolling

    # -------------------------------------------------------------- painting
    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 150 if t.dark else 95))

    def changeEvent(self, ev):                                           # noqa: N802
        if ev.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            self.update()
            self._view.viewport().update()
        super().changeEvent(ev)

    # ---------------------------------------------------------------- layout
    def _watch_parent(self) -> None:
        par = self.parentWidget()
        if par is not self._watched:
            if self._watched is not None:
                self._watched.removeEventFilter(self)
            self._watched = par
            if par is not None:
                par.installEventFilter(self)

    def _content_height(self) -> int:
        return sum(self._delegate.height_for(r) for r in self._model.rows)

    def _fit(self) -> None:
        par = self.parentWidget()
        if par is not None:
            self.setGeometry(par.rect())
        W, H = self.width(), self.height()
        bw = max(320, min(BOX_WIDTH, W - 32))
        chrome = self.HEADER_H + self.CHIPS_H + 38 + 3 + 2                 # header, chips, footer, rules, border
        max_h = max(chrome + 90, int(H * 0.80))
        list_h = max(90, min(self._content_height() + 6, max_h - chrome))
        bh = chrome + list_h
        top = max(12, min(int(H * 0.12), H - bh - 56))
        self._box.setGeometry((W - bw) // 2, top, bw, bh)

    # ---------------------------------------------------------- live values
    @staticmethod
    def _norm_addr(addr) -> str:
        a = str(addr or "").strip().lower()
        try:
            return "%02x" % int(a, 16)
        except ValueError:
            return a

    def _norm_key(self, key: tuple) -> tuple:
        addr, kind, k = (tuple(key) + ("", "", ""))[:3]
        return (self._norm_addr(addr), kind, k)

    def _build_keymap(self) -> Dict[tuple, PaletteItem]:
        out: Dict[tuple, PaletteItem] = {}
        for it in self._items:
            pl = it.payload or {}
            keys = [pl.get("key"), pl.get("name"), it.title]
            if "group" in pl and "cell" in pl:
                g, c = pl["group"], pl["cell"]
                keys += [(g, c), "%s:%s" % (g, c)]
            a = self._norm_addr(it.addr)
            for k in keys:
                if k is not None and k != "":
                    out.setdefault((a, it.kind, k), it)
        return out
