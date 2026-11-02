# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The look of OVT Diag: the small widgets the interface plan
(docs/OVT Diag Interface Options.pdf) is made of -- chips, count badges,
status dots, cards, value tiles, summary cards -- and the style helpers for
buttons and tabs.

Everything here is *painted* from the application's palette (``tokens()``),
so it follows the suite's light / dark / system theme (:mod:`ovtsuite.gui_theme`)
with no stylesheet to go stale when the theme changes. The plan's four
semantic colours (accent teal, fault red, warning amber, ok green) are fixed
hues, nudged for contrast on a light base. Tables are NOT drawn here: they
use the suite's standard (:mod:`ovtsuite.gui_cellmarks`).

Qt only; no diagnostics logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

KINDS = ("neutral", "ok", "warn", "bad", "accent")


# ------------------------------------------------------------------ tokens
@dataclass
class Tokens:
    dark: bool
    window: QtGui.QColor          # the page behind everything
    panel: QtGui.QColor           # rails, bars, the bottom panes
    card: QtGui.QColor            # a card / tile
    card_selected: QtGui.QColor   # the selected module card
    card_hover: QtGui.QColor
    border: QtGui.QColor
    border_strong: QtGui.QColor
    text: QtGui.QColor
    muted: QtGui.QColor           # secondary text
    faint: QtGui.QColor           # tertiary text, disabled
    accent: QtGui.QColor
    accent_bg: QtGui.QColor       # a filled accent button / checked row
    ok: QtGui.QColor
    warn: QtGui.QColor
    bad: QtGui.QColor

    def kind_colors(self, kind: str) -> Tuple[QtGui.QColor, QtGui.QColor, QtGui.QColor]:
        """``(text, fill, border)`` for a chip / badge of ``kind``."""
        base = {"ok": self.ok, "warn": self.warn, "bad": self.bad, "accent": self.accent}.get(kind)
        if base is None:
            return self.muted, QtGui.QColor(0, 0, 0, 0), self.border_strong
        fill = QtGui.QColor(base)
        fill.setAlpha(46 if self.dark else 34)
        edge = QtGui.QColor(base)
        edge.setAlpha(120)
        return base, fill, edge


def _mix(a: QtGui.QColor, b: QtGui.QColor, t: float) -> QtGui.QColor:
    """``a`` moved ``t`` (0..1) of the way to ``b``."""
    return QtGui.QColor(round(a.red() + (b.red() - a.red()) * t),
                        round(a.green() + (b.green() - a.green()) * t),
                        round(a.blue() + (b.blue() - a.blue()) * t))


def tokens(palette: Optional[QtGui.QPalette] = None) -> Tokens:
    pal = palette or QtWidgets.QApplication.palette()
    R = QtGui.QPalette.ColorRole
    win, base, text = pal.color(R.Window), pal.color(R.Base), pal.color(R.WindowText)
    dark = win.lightness() < 128
    black, white = QtGui.QColor(0, 0, 0), QtGui.QColor(255, 255, 255)
    if dark:
        window = _mix(win, black, 0.30)
        panel = _mix(win, black, 0.12)
        card = _mix(win, white, 0.035)
        card_sel = _mix(win, white, 0.10)
        hover = _mix(win, white, 0.07)
        border = _mix(win, white, 0.13)
        strong = _mix(win, white, 0.24)
        muted = _mix(text, win, 0.38)
        faint = _mix(text, win, 0.60)
        accent, ok, warn, bad = (QtGui.QColor("#3ab3c1"), QtGui.QColor("#4cbf91"),
                                 QtGui.QColor("#e2a83a"), QtGui.QColor("#e07269"))
        accent_bg = _mix(win, accent, 0.28)
    else:
        window = _mix(win, white, 0.45)
        panel = _mix(win, white, 0.70)
        card = QtGui.QColor(white)
        card_sel = _mix(win, black, 0.05)
        hover = _mix(win, black, 0.03)
        border = _mix(win, black, 0.14)
        strong = _mix(win, black, 0.26)
        muted = _mix(text, win, 0.42)
        faint = _mix(text, win, 0.62)
        accent, ok, warn, bad = (QtGui.QColor("#0e7c89"), QtGui.QColor("#1f7a4d"),
                                 QtGui.QColor("#a8650a"), QtGui.QColor("#b23b2a"))
        accent_bg = _mix(white, accent, 0.14)
    return Tokens(dark, window, panel, card, card_sel, hover, border, strong, text, muted, faint,
                  accent, accent_bg, ok, warn, bad)


def mono_font(base: Optional[QtGui.QFont] = None, scale: float = 0.95) -> QtGui.QFont:
    f = QtGui.QFont(base) if base is not None else QtGui.QFont(QtWidgets.QApplication.font())
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    f.setFamilies(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Menlo", "monospace"])
    if f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * scale)
    return f


def scaled_font(base: QtGui.QFont, scale: float, bold: bool = False) -> QtGui.QFont:
    f = QtGui.QFont(base)
    if f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * scale)
    f.setBold(bold)
    return f


class _Themed(QtWidgets.QWidget):
    """A widget that repaints when the palette changes (a theme switch)."""

    def changeEvent(self, ev):
        if ev.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            self.update()
        super().changeEvent(ev)


# ------------------------------------------------------- chips, dots, badges
class Chip(_Themed):
    """A small label in a rounded outline: a protocol tag (``KWP-TP2.0``),
    a status (``stored · active``), a safety class. ``pill`` for fully
    rounded ends, ``mono`` for a code."""

    def __init__(self, text: str = "", kind: str = "neutral", pill: bool = False, mono: bool = False,
                 parent=None):
        super().__init__(parent)
        self._text, self._kind, self._pill, self._mono = text, kind, pill, mono
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def setText(self, text: str):
        self._text = text
        self.updateGeometry()
        self.update()

    def setKind(self, kind: str):
        self._kind = kind
        self.update()

    def _font(self) -> QtGui.QFont:
        return mono_font(self.font(), 0.82) if self._mono else scaled_font(self.font(), 0.86)

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self._font())
        return QtCore.QSize(fm.horizontalAdvance(self._text) + 16, fm.height() + 6)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        fg, fill, edge = t.kind_colors(self._kind)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(fill)
        p.setPen(QtGui.QPen(edge, 1))
        rad = r.height() / 2 if self._pill else 4
        p.drawRoundedRect(r, rad, rad)
        p.setFont(self._font())
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)


class Dot(_Themed):
    """A status dot: ok (green), warn, bad, accent, or neutral."""

    def __init__(self, kind: str = "ok", size: int = 9, parent=None):
        super().__init__(parent)
        self._kind, self._size = kind, size
        self.setFixedSize(size + 2, size + 2)

    def setKind(self, kind: str):
        self._kind = kind
        self.update()

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        col = t.kind_colors(self._kind)[0] if self._kind != "neutral" else t.faint
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        c = self.rect().center()
        r = self._size / 2
        p.drawEllipse(QtCore.QPointF(c.x() + 0.5, c.y() + 0.5), r, r)


class CountBadge(_Themed):
    """A filled pill with a number: faults on a module card or a tab."""

    def __init__(self, n: int = 0, kind: str = "bad", parent=None):
        super().__init__(parent)
        self._n, self._kind = n, kind
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def setCount(self, n: int, kind: Optional[str] = None):
        self._n = n
        if kind:
            self._kind = kind
        self.updateGeometry()
        self.update()

    def _font(self):
        return scaled_font(self.font(), 0.86, True)

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self._font())
        w = max(fm.horizontalAdvance(str(self._n)) + 12, fm.height() + 4)
        return QtCore.QSize(w, fm.height() + 4)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        base = {"warn": t.warn, "ok": t.ok, "accent": t.accent}.get(self._kind, t.bad)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(self._font())
        p.setPen(QtGui.QColor(20, 20, 24) if t.dark else QtGui.QColor(255, 255, 255))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(self._n))


# ------------------------------------------------------------------- cards
class Card(_Themed):
    """A rounded panel on the page. Put a layout on it, or use one of the
    painted subclasses. ``edge`` ("warn" / "bad" / "accent" / "") colours
    the border; ``selected`` fills it as the selected item."""

    def __init__(self, parent=None, radius: int = 8):
        super().__init__(parent)
        self._radius = radius
        self._edge = ""
        self._selected = False
        self._hover = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def setEdge(self, kind: str):
        self._edge = kind
        self.update()

    def setSelected(self, on: bool):
        self._selected = bool(on)
        self.update()

    def _paint_card(self, p: QtGui.QPainter, t: Tokens):
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = t.card_selected if self._selected else (t.card_hover if self._hover else t.card)
        edge = t.border
        if self._edge:
            edge = t.kind_colors(self._edge)[0]
        elif self._selected:
            edge = t.accent
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(fill)
        p.setPen(QtGui.QPen(edge, 1.4 if (self._edge or self._selected) else 1))
        p.drawRoundedRect(r, self._radius, self._radius)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        self._paint_card(p, tokens(self.palette()))

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)


class ValueTile(Card):
    """One live value: its name, the value large with its unit, and a bar
    showing where it sits in its range. ``state`` "warn" / "bad" outlines
    the tile and colours the value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name, self._value, self._unit = "", "", ""
        self._frac: Optional[float] = None
        self._state = ""
        self.setMinimumSize(150, 84)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(190, 92)

    def set_value(self, name: str, value: str, unit: str = "", frac: Optional[float] = None,
                  state: str = ""):
        if (name, value, unit, frac, state) == (self._name, self._value, self._unit, self._frac, self._state):
            return
        self._name, self._value, self._unit, self._frac, self._state = name, value, unit, frac, state
        self.setEdge(state if state in ("warn", "bad") else "")
        self.update()

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        self._paint_card(p, t)
        r = self.rect().adjusted(14, 10, -14, -10)
        small = scaled_font(self.font(), 0.88)
        p.setFont(small)
        p.setPen(t.muted)
        fm = QtGui.QFontMetrics(small)
        p.drawText(QtCore.QRect(r.left(), r.top(), r.width(), fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self._name, Qt.TextElideMode.ElideRight, r.width()))
        big = scaled_font(self.font(), 1.9)
        big.setWeight(QtGui.QFont.Weight.DemiBold)
        big.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        bfm = QtGui.QFontMetrics(big)
        top = r.top() + fm.height() + 2
        p.setFont(big)
        p.setPen({"warn": t.warn, "bad": t.bad}.get(self._state, t.text))
        p.drawText(r.left(), top + bfm.ascent(), self._value)
        if self._unit:
            p.setFont(small)
            p.setPen(t.muted)
            p.drawText(r.left() + bfm.horizontalAdvance(self._value) + 8, top + bfm.ascent(), self._unit)
        if self._frac is not None:
            bar = QtCore.QRectF(r.left(), r.bottom() - 4, r.width(), 4)
            p.setPen(Qt.PenStyle.NoPen)
            track = QtGui.QColor(t.border_strong)
            track.setAlpha(110)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setBrush(track)
            p.drawRoundedRect(bar, 2, 2)
            f = max(0.0, min(1.0, float(self._frac)))
            if f > 0:
                fill = {"warn": t.warn, "bad": t.bad}.get(self._state, t.accent)
                p.setBrush(fill)
                p.drawRoundedRect(QtCore.QRectF(bar.left(), bar.top(), max(4.0, bar.width() * f), 4), 2, 2)


class SummaryCard(Card):
    """A headline figure with context: a title, an optional large value
    (with a muted suffix), and lines of text, each with a colour kind
    (``bad`` for new faults, ``ok`` for cleared, ``warn`` for changes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title, self._value, self._suffix = "", "", ""
        self._value_kind = ""
        self._lines: List[Tuple[str, str]] = []
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def set(self, title: str, value: str = "", suffix: str = "", value_kind: str = "",
            lines: Sequence[Tuple[str, str]] = ()):
        self._title, self._value, self._suffix, self._value_kind = title, value, suffix, value_kind
        self._lines = [(str(a), str(b)) for a, b in lines]
        self.updateGeometry()
        self.update()

    def _fonts(self):
        base = self.font()
        return scaled_font(base, 0.9), scaled_font(base, 2.2, True), scaled_font(base, 1.0)

    def sizeHint(self):
        small, big, norm = self._fonts()
        h = 14 + QtGui.QFontMetrics(small).height() + 6
        if self._value:
            h += QtGui.QFontMetrics(big).height()
        h += len(self._lines) * (QtGui.QFontMetrics(norm).height() + 2)
        return QtCore.QSize(220, h + 14)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        self._paint_card(p, t)
        small, big, norm = self._fonts()
        x, y = 16, 14
        fm = QtGui.QFontMetrics(small)
        p.setFont(small)
        p.setPen(t.muted)
        p.drawText(x, y + fm.ascent(), self._title)
        y += fm.height() + 6
        if self._value:
            bfm = QtGui.QFontMetrics(big)
            p.setFont(big)
            p.setPen({"bad": t.bad, "warn": t.warn, "ok": t.ok}.get(self._value_kind, t.text))
            p.drawText(x, y + bfm.ascent(), self._value)
            if self._suffix:
                p.setFont(norm)
                p.setPen(t.muted)
                p.drawText(x + bfm.horizontalAdvance(self._value) + 10, y + bfm.ascent(), self._suffix)
            y += bfm.height()
        nfm = QtGui.QFontMetrics(norm)
        p.setFont(norm)
        for text, kind in self._lines:
            p.setPen({"bad": t.bad, "warn": t.warn, "ok": t.ok, "muted": t.muted}.get(kind, t.text))
            p.drawText(x, y + nfm.ascent(), text)
            y += nfm.height() + 2


# ----------------------------------------------------------- small helpers
def section_label(text: str, parent=None) -> QtWidgets.QLabel:
    """A small, muted, upper-case heading (``NAMED FROM THE LOADED CALIBRATION``)."""
    lab = QtWidgets.QLabel(text.upper(), parent)
    f = scaled_font(lab.font(), 0.78, True)
    f.setLetterSpacing(QtGui.QFont.SpacingType.PercentageSpacing, 104)
    lab.setFont(f)
    lab.setStyleSheet("color: palette(placeholder-text);")
    return lab


def muted_label(text: str = "", parent=None, scale: float = 0.92) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text, parent)
    lab.setFont(scaled_font(lab.font(), scale))
    lab.setStyleSheet("color: palette(placeholder-text);")
    return lab


def mono_label(text: str = "", parent=None, scale: float = 0.95) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text, parent)
    lab.setFont(mono_font(lab.font(), scale))
    return lab


_BUTTON_KINDS = ("outline", "primary", "ghost", "danger")


def button_qss(kind: str, t: Optional[Tokens] = None) -> str:
    """Stylesheet for a QPushButton of ``kind``: outline (the plan's
    ``Graph`` / ``Gauges``), primary (a filled accent button), ghost (text
    only), danger. Colours come from the palette at call time; call again
    after a theme change (:func:`style_button` does so on its own)."""
    t = t or tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    if kind == "primary":
        body = ("background:%s; color:%s; border:1px solid %s;" % (c(t.accent_bg), c(t.accent), c(t.accent)))
        hover = "background:%s;" % c(_mix(t.accent_bg, t.accent, 0.18))
    elif kind == "danger":
        body = "background:transparent; color:%s; border:1px solid %s;" % (c(t.bad), c(t.bad))
        hover = "background:%s;" % c(_with_alpha(t.bad, 40))
    elif kind == "ghost":
        body = "background:transparent; color:%s; border:1px solid transparent;" % c(t.accent)
        hover = "background:%s;" % c(t.card_hover)
    else:
        body = "background:transparent; color:%s; border:1px solid %s;" % (c(t.text), c(t.border_strong))
        hover = "background:%s; border-color:%s;" % (c(t.card_hover), c(t.muted))
    return ("QPushButton{%s border-radius:6px; padding:5px 12px;}"
            "QPushButton:hover{%s}"
            "QPushButton:disabled{color:%s; border-color:%s; background:transparent;}"
            % (body, hover, c(t.faint), c(t.border)))


def _with_alpha(col: QtGui.QColor, a: int) -> QtGui.QColor:
    c = QtGui.QColor(col)
    c.setAlpha(a)
    return c


class _Restyler(QtCore.QObject):
    """Re-applies a widget's stylesheet when the palette changes.

    One restyler per widget: a second one replaces the first. (Two of them,
    or one plus a stray ``setStyleSheet``, answer each other's style change
    with another palette change and multiply without end.)"""

    def __init__(self, widget, make_qss):
        super().__init__(widget)
        prev = getattr(widget, "_restyle_active", None)
        if prev is not None and prev is not self:
            try:
                widget.removeEventFilter(prev)
            except RuntimeError:
                pass
            prev._w = None
        widget._restyle_active = self
        self._w, self._make = widget, make_qss
        widget.installEventFilter(self)
        self._apply()

    def _apply(self):
        w = getattr(self, "_w", None)
        if w is None:
            return
        try:
            qss = self._make()
            if w.styleSheet() != qss:
                w.setStyleSheet(qss)
        except RuntimeError:            # the widget is already gone
            self._w = None

    def eventFilter(self, obj, ev):
        w = getattr(self, "_w", None)
        if w is not None and obj is w and ev.type() in (QtCore.QEvent.Type.PaletteChange,
                                                        QtCore.QEvent.Type.ApplicationPaletteChange):
            QtCore.QTimer.singleShot(0, self._apply)
        return False


def style_button(btn: QtWidgets.QPushButton, kind: str = "outline") -> QtWidgets.QPushButton:
    if kind not in _BUTTON_KINDS:
        kind = "outline"
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn._restyler = _Restyler(btn, lambda: button_qss(kind))
    return btn


def tab_qss(t: Optional[Tokens] = None) -> str:
    """A ``QTabWidget`` / ``QTabBar`` look with an accent underline on the
    current tab and no frame, as in the plan's module tabs."""
    t = t or tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    return ("QTabWidget::pane{border:0; border-top:1px solid %(border)s;}"
            "QTabBar{background:transparent;}"
            "QTabBar::tab{background:transparent; color:%(muted)s; padding:8px 12px; margin-right:4px;"
            " border:0; border-bottom:2px solid transparent;}"
            "QTabBar::tab:selected{color:%(text)s; border-bottom:2px solid %(accent)s;}"
            "QTabBar::tab:hover:!selected{color:%(text)s;}"
            "QTabBar::tab:disabled{color:%(faint)s;}"
            % {"border": c(t.border), "muted": c(t.muted), "text": c(t.text), "accent": c(t.accent),
               "faint": c(t.faint)})


def style_tabs(tabs: QtWidgets.QTabWidget) -> QtWidgets.QTabWidget:
    tabs.setDocumentMode(True)
    tabs._restyler = _Restyler(tabs, tab_qss)
    return tabs


def panel_qss(object_name: str, side: str = "", t: Optional[Tokens] = None) -> str:
    """A flat panel (the rail, the top bar, the bottom panes) in the panel
    colour with a hairline on ``side`` ("top" / "bottom" / "left" / "right")."""
    t = t or tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    edge = ("border-%s:1px solid %s;" % (side, c(t.border))) if side else ""
    return "#%s{background:%s; border:0; %s}" % (object_name, c(t.panel), edge)


def style_panel(widget: QtWidgets.QWidget, object_name: str, side: str = "") -> QtWidgets.QWidget:
    """Make ``widget`` a flat panel; the widget needs ``WA_StyledBackground``
    (set here) and a unique ``object_name``."""
    widget.setObjectName(object_name)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget._restyler = _Restyler(widget, lambda: panel_qss(object_name, side))
    return widget


def page_qss(object_name: str, t: Optional[Tokens] = None) -> str:
    t = t or tokens()
    return "#%s{background:%s;}" % (object_name, t.window.name(QtGui.QColor.NameFormat.HexArgb))


def style_page(widget: QtWidgets.QWidget, object_name: str) -> QtWidgets.QWidget:
    widget.setObjectName(object_name)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget._restyler = _Restyler(widget, lambda: page_qss(object_name))
    return widget


class ClickableFrame(Card):
    """A card that emits ``clicked`` (a module card, a procedure card)."""
    clicked = QtCore.Signal()
    doubleClicked = QtCore.Signal()

    def __init__(self, parent=None, radius: int = 8):
        super().__init__(parent, radius)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(ev)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        t = tokens(self.palette())
        self._paint_card(p, t)
        if self.hasFocus() and not self._selected:
            p.setPen(QtGui.QPen(t.accent, 1, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5), 6, 6)


__all__ = ["Tokens", "tokens", "mono_font", "scaled_font", "Chip", "Dot", "CountBadge", "Card", "ValueTile",
           "SummaryCard", "ClickableFrame", "section_label", "muted_label", "mono_label", "style_button",
           "style_tabs", "style_panel", "style_page", "button_qss", "tab_qss", "panel_qss", "page_qss", "KINDS"]


# ------------------------------------------------- additions for the window
class ElidedLabel(QtWidgets.QLabel):
    """A one-line label that cuts its text with an ellipsis instead of
    asking for the width (a module name, a part number in a narrow rail);
    the full text is the tooltip unless one is set."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self._own_tip = False
        self.setText(text)

    def setText(self, text: str):                                       # noqa: N802
        self._full = text or ""
        super().setText(self._full)
        self.update()

    def fullText(self) -> str:                                          # noqa: N802
        return self._full

    def setToolTip(self, tip: str):                                     # noqa: N802
        self._own_tip = bool(tip)
        super().setToolTip(tip)

    def minimumSizeHint(self):                                          # noqa: N802
        return QtCore.QSize(10, super().minimumSizeHint().height())

    def sizeHint(self):                                                 # noqa: N802
        fm = QtGui.QFontMetrics(self.font())
        return QtCore.QSize(fm.horizontalAdvance(self._full) + 2, fm.height())

    def paintEvent(self, _ev):                                          # noqa: N802
        p = QtGui.QPainter(self)
        fm = QtGui.QFontMetrics(self.font())
        shown = fm.elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        if not self._own_tip:
            QtWidgets.QLabel.setToolTip(self, self._full if shown != self._full else "")
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        p.drawText(self.rect(), int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), shown)


class ChipButton(ClickableFrame):
    """A rounded button holding a status dot and a line of text (the top
    bar's interface and vehicle chips, the trace chip). ``clicked`` as any
    :class:`ClickableFrame`; the dot is hidden with ``setDot(None)``."""

    def __init__(self, text: str = "", dot: Optional[str] = "ok", muted: str = "", mono_tail: bool = False,
                 parent=None):
        super().__init__(parent, radius=6)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(7)
        self._dot = Dot(dot or "ok")
        self._dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._dot.setVisible(dot is not None)
        lay.addWidget(self._dot)
        self._text = QtWidgets.QLabel(text)
        self._text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(self._text)
        self._tail = QtWidgets.QLabel(muted)
        self._tail.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._tail.setFont(mono_font(self._tail.font(), 0.88) if mono_tail else scaled_font(self._tail.font(), 0.95))
        self._tail.setStyleSheet("color: palette(placeholder-text);")
        self._tail.setVisible(bool(muted))
        lay.addWidget(self._tail)

    def text(self) -> str:
        return self._text.text()

    def tail(self) -> str:
        return self._tail.text()

    def setTexts(self, text: str, muted: str = ""):                      # noqa: N802
        self._text.setText(text)
        self._tail.setText(muted)
        self._tail.setVisible(bool(muted))
        self.updateGeometry()

    def setDot(self, kind: Optional[str]):                              # noqa: N802
        self._dot.setVisible(kind is not None)
        if kind is not None:
            self._dot.setKind(kind)

    def dotKind(self) -> str:                                           # noqa: N802
        return self._dot._kind

    def setMutedText(self, on: bool):                                   # noqa: N802
        self._text.setStyleSheet("color: palette(placeholder-text);" if on else "")


def restyle(widget: QtWidgets.QWidget, make_qss) -> None:
    """Apply ``make_qss()`` to ``widget`` now and again after every theme
    change (``make_qss`` reads :func:`tokens` when called)."""
    widget._restyler = _Restyler(widget, make_qss)


def input_qss(object_name: str, t: Optional[Tokens] = None) -> str:
    t = t or tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    return ("#%(n)s{background:%(card)s; border:1px solid %(border)s; border-radius:6px; padding:5px 10px;"
            " color:%(text)s;}"
            "#%(n)s:focus{border:1px solid %(accent)s;}"
            % {"n": object_name, "card": c(t.card), "border": c(t.border_strong), "text": c(t.text),
               "accent": c(t.accent)})


def style_input(edit: QtWidgets.QLineEdit, object_name: str) -> QtWidgets.QLineEdit:
    """A rounded search / text field in the plan's look."""
    edit.setObjectName(object_name)
    restyle(edit, lambda: input_qss(object_name))
    return edit


__all__ += ["ElidedLabel", "ChipButton", "restyle", "input_qss", "style_input"]
