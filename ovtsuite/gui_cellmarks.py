# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cell colours the item-based editing windows share -- the fault-code
windows (:mod:`ovtsuite.gui_dtc`, :mod:`ovtsuite.gui_tcu_dtc`), the
measuring blocks (:mod:`ovtsuite.gui_msblock`) and the shift / lock-up
editors (:mod:`ovtsuite.gui_gearbox_compare`) -- so they read like the main
table editor:

* **differs from the comparison**: an orange outline (:data:`DIFF_OUTLINE`)
  round the cell, in the primary's cell and the comparison's copy, the same
  in either theme (:func:`mark_differs`). It is a frame, not a fill, so it
  sits on top of whatever else the cell says -- a heat-map or band shade,
  the direction tint, the pending yellow, a selection -- and none of them
  has to give way to it. Every delegate that draws a table of the app
  calls :func:`draw_outline` (the model side answers :data:`OUTLINE_ROLE`);
* **changed, not applied yet**: :data:`PENDING_BG` (pale yellow), bold, dark
  text (:func:`mark_pending`). It takes the place of orange in the primary;
  the comparison's copy stays orange where it differs;
* **a difference tinted by direction** (an option of the difference view,
  :mod:`ovtsuite.compare_delta`): ``gui.DELTA_UP_BG`` (primary higher) and
  ``gui.DELTA_DOWN_BG`` (lower), opaque light fills with ``gui.CELL_DARK_FG``
  text, the same in either theme; the main table's model draws them, under
  the outline;
* **stored on the ECU** (the fault-code windows, after reading the codes
  from the car): :data:`STORED_BG` (light violet), dark text -- a meaning the
  rest of the app has no colour for, a hue apart from orange and yellow;
  below the other two in a primary cell.

The tables using these turn on alternating row colours: the Windows 11 style
then draws the selection in the solid accent colour, which shows over a
filled cell (its faint default selection does not). Their delegate
(:class:`CellDelegate`, or :func:`selection_text` in a delegate of their own)
writes a selected cell's text in a colour that stays legible on the
selection actually drawn -- with and without focus, in either theme: the
selected-text colour where it contrasts with the highlight of the state
the view is in (focused / not focused, :func:`selected_text_color`), the
cell's own text where the style draws a faint selection -- and draws a
selected cell's check box (the fault windows' "On" column) on a chip of the
unselected base colour, so the accent selection does not swallow it
(:func:`check_on_selection`).

A primary table and its read-only comparison share rows and columns: the
comparison's current cell and its whole selection follow the primary's
(:func:`follow_current`, :func:`link_selection`).

Kept light (Qt only): ``gui`` is imported when a colour is first asked for.
"""
from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

#: the frame of a cell that differs from the comparison
DIFF_OUTLINE = QtGui.QColor(255, 140, 0)
#: its width in pixels (drawn inside the cell, over a selection too)
OUTLINE_WIDTH = 2
#: item data / model role: True on a cell that differs from the comparison
OUTLINE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 0x0D17
#: a cell changed in the window, not applied yet (bold, dark text)
PENDING_BG = QtGui.QColor(255, 236, 170)
#: a fault the car's ECU has stored (read with Read codes from ECU...)
STORED_BG = QtGui.QColor(218, 202, 255)
#: text of a switched-off fault / an empty slot (either theme)
OFF_FG = QtGui.QColor(150, 150, 150)


def dark_fg() -> QtGui.QColor:
    from . import gui as _g
    return _g.CELL_DARK_FG


def mark_differs(item: QtWidgets.QTableWidgetItem) -> None:
    """``item`` as differing from the comparison: the orange outline, which
    the table's delegate draws (the cell's own fill and text stay)."""
    item.setData(OUTLINE_ROLE, True)


def differs(index) -> bool:
    """Is the model cell ``index`` marked as differing from the comparison?"""
    return bool(index.data(OUTLINE_ROLE))


def draw_outline(painter: QtGui.QPainter, option, index) -> None:
    """Frame the cell in :data:`DIFF_OUTLINE` when it differs from the
    comparison (:func:`mark_differs`, or a model answering
    :data:`OUTLINE_ROLE`). Call it last in a delegate's ``paint``, so the
    frame lies over the fill, the text and the selection."""
    if not index.data(OUTLINE_ROLE):
        return
    w = OUTLINE_WIDTH
    painter.save()
    painter.setClipRect(option.rect)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.setPen(QtGui.QPen(DIFF_OUTLINE, w, QtCore.Qt.PenStyle.SolidLine,
                              QtCore.Qt.PenCapStyle.FlatCap,
                              QtCore.Qt.PenJoinStyle.MiterJoin))
    painter.drawRect(option.rect.adjusted(w // 2, w // 2,
                                          -(w + 1) // 2, -(w + 1) // 2))
    painter.restore()


def mark_pending(item: QtWidgets.QTableWidgetItem, font: QtGui.QFont) -> None:
    """``item`` as changed but not applied: :data:`PENDING_BG`, bold, dark
    text (``font`` is the table's; a bold copy is set)."""
    item.setBackground(QtGui.QBrush(PENDING_BG))
    item.setForeground(QtGui.QBrush(dark_fg()))
    bold = QtGui.QFont(font)
    bold.setBold(True)
    item.setFont(bold)


def mark_stored(item: QtWidgets.QTableWidgetItem) -> None:
    """``item`` on a fault the ECU has stored: :data:`STORED_BG`, dark text."""
    item.setBackground(QtGui.QBrush(STORED_BG))
    item.setForeground(QtGui.QBrush(dark_fg()))


def _lum(c: QtGui.QColor) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast(a: QtGui.QColor, b: QtGui.QColor) -> float:
    """The WCAG contrast ratio of two colours (1 to 21)."""
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


#: the contrast a selected cell's text needs to count as legible
MIN_CONTRAST = 3.0


def _group(opt) -> "QtGui.QPalette.ColorGroup":
    """The palette group the cell is drawn with: Disabled, Inactive (the view
    without focus, or its window in the background), else Active -- as the
    item-view drawing picks it."""
    S = QtWidgets.QStyle.StateFlag
    G = QtGui.QPalette.ColorGroup
    if not opt.state & S.State_Enabled:
        return G.Disabled
    return G.Active if opt.state & S.State_Active else G.Inactive


def _faint_selection(opt) -> bool:
    """Does the style draw this view's selection faint (a light wash over the
    cell, not the accent)? The Windows 11 style does on a view without
    alternating row colours; the cell's own text is right on it."""
    w = opt.widget
    if w is None or not hasattr(w, "alternatingRowColors"):
        return False
    st = w.style()
    while isinstance(st, QtWidgets.QProxyStyle) and st.baseStyle() is not None:
        st = st.baseStyle()
    return (st.name() or "").lower() == "windows11" and not w.alternatingRowColors()


def selected_text_color(opt: QtWidgets.QStyleOptionViewItem) -> Optional[QtGui.QColor]:
    """The text colour of a selected cell drawn with ``opt``, or None to keep
    the cell's own. The selected-text colour of the group the cell is drawn
    in (focused or not) where it contrasts with that group's highlight;
    otherwise whichever of it, the normal text, black or white reads best --
    against the highlight of the drawn group and of the palette's current
    one, so a style that keeps the focused colours still gets legible text."""
    if not opt.state & QtWidgets.QStyle.StateFlag.State_Selected:
        return None
    if _faint_selection(opt):
        return None
    P = QtGui.QPalette
    pal = opt.palette
    cg = _group(opt)
    bgs = [pal.color(cg, P.ColorRole.Highlight)]
    cur = pal.color(pal.currentColorGroup(), P.ColorRole.Highlight)
    if cur != bgs[0]:
        bgs.append(cur)
    want = pal.color(cg, P.ColorRole.HighlightedText)
    if all(contrast(want, b) >= MIN_CONTRAST for b in bgs):
        return want
    cands = [want, pal.color(cg, P.ColorRole.Text),
             QtGui.QColor(0, 0, 0), QtGui.QColor(255, 255, 255)]
    return max(cands, key=lambda c: min(contrast(c, b) for b in bgs))


def selection_text(opt: QtWidgets.QStyleOptionViewItem) -> None:
    """A selected cell's text legible on the selection drawn, whatever colour
    the cell gives it otherwise: the dark text of a shaded cell or the
    comparison's quiet grey would be hard to read on the solid accent, and
    the white selected-text colour vanishes on a light selection (a view
    without focus in a palette whose inactive highlight is light, or the
    Windows 11 style's faint one) -- :func:`selected_text_color`."""
    col = selected_text_color(opt)
    if col is None:
        return
    for role in (QtGui.QPalette.ColorRole.Text, QtGui.QPalette.ColorRole.HighlightedText):
        opt.palette.setColor(role, col)            # every group


def check_on_selection(opt: QtWidgets.QStyleOptionViewItem, index):
    """``(chip, indicator)`` for a selected cell with a check box, else
    ``None``: ``chip`` is the rectangle filled with the widget's own
    (unselected) base colour behind the box, ``indicator`` the style option
    the box is drawn with -- not selected, the widget's own palette -- so the
    tick keeps its normal contrast instead of a pale tick on the solid
    accent (the Windows 11 style fills a ticked box in the accent too)."""
    if not opt.state & QtWidgets.QStyle.StateFlag.State_Selected:
        return None
    cs = index.data(QtCore.Qt.ItemDataRole.CheckStateRole)
    if cs is None:
        return None
    w = opt.widget
    style = w.style() if w is not None else QtWidgets.QApplication.style()
    r = style.subElementRect(
        QtWidgets.QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, w)
    if not r.isValid():
        return None
    S = QtWidgets.QStyle.StateFlag
    ind = QtWidgets.QStyleOptionViewItem(opt)
    ind.rect = r
    ind.palette = w.palette() if w is not None else QtGui.QPalette()
    cs = QtCore.Qt.CheckState(cs)
    mark = (S.State_On if cs == QtCore.Qt.CheckState.Checked else
            S.State_NoChange if cs == QtCore.Qt.CheckState.PartiallyChecked
            else S.State_Off)
    ind.state = (opt.state & ~(S.State_Selected | S.State_HasFocus
                               | S.State_On | S.State_Off | S.State_NoChange)
                 ) | mark
    return r.adjusted(-2, -2, 2, 2), ind


class CellDelegate(QtWidgets.QStyledItemDelegate):
    """The tables' delegate: :func:`selection_text` on the stock drawing, a
    selected cell's check box redrawn readable (:func:`check_on_selection`),
    and the outline of a cell that differs from the comparison
    (:func:`draw_outline`)."""

    def initStyleOption(self, opt, index):
        super().initStyleOption(opt, index)
        selection_text(opt)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        got = check_on_selection(opt, index)
        if got is None:
            draw_outline(painter, option, index)
            return
        chip, ind = got
        w = opt.widget
        style = w.style() if w is not None else QtWidgets.QApplication.style()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(ind.palette.color(QtGui.QPalette.ColorRole.Base))
        painter.drawRoundedRect(QtCore.QRectF(chip), 3, 3)
        painter.restore()
        style.drawPrimitive(
            QtWidgets.QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
            ind, painter, w)
        draw_outline(painter, option, index)


class OutlineDelegate(QtWidgets.QStyledItemDelegate):
    """The stock drawing plus the outline of a cell that differs from the
    comparison (:func:`draw_outline`): what a table that needs nothing
    else of its delegate uses."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        draw_outline(painter, option, index)


def follow_current(other, r, c) -> None:
    """``other``'s current cell moves to ``(r, c)`` without touching its
    selection (that is :func:`link_selection`'s), no signals from ``other``
    and no scrolling: the two tables scroll together, so scrolling a shorter
    comparison to the cell would move the primary's rows under the mouse."""
    if other is None or r < 0 or c < 0:
        return
    if (other.currentRow(), other.currentColumn()) != (r, c):
        auto = other.hasAutoScroll()
        other.setAutoScroll(False)
        other.blockSignals(True)
        try:
            other.selectionModel().setCurrentIndex(
                other.model().index(r, c),
                QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            other.blockSignals(False)
            other.setAutoScroll(auto)


def copy_selection(src, dst) -> None:
    """``dst`` selects the cells ``src`` has selected, by row and column
    (the two tables hold the same rows), clipped to ``dst``'s size."""
    nr, nc = dst.rowCount(), dst.columnCount()
    sel = QtCore.QItemSelection()
    m = dst.model()
    for rg in src.selectionModel().selection():
        top, bottom = rg.top(), min(rg.bottom(), nr - 1)
        left, right = rg.left(), min(rg.right(), nc - 1)
        if top > bottom or left > right:
            continue
        sel.select(m.index(top, left), m.index(bottom, right))
    dst.selectionModel().select(
        sel, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)


def link_selection(a, b) -> None:
    """Either table's selection, all of it, is mirrored in the other."""
    busy = []

    def mirror(src, dst):
        if busy:
            return
        busy.append(1)
        try:
            copy_selection(src, dst)
        finally:
            busy.pop()
    a.selectionModel().selectionChanged.connect(lambda *_: mirror(a, b))
    b.selectionModel().selectionChanged.connect(lambda *_: mirror(b, a))


def cmp_fg(widget: QtWidgets.QWidget) -> QtGui.QColor:
    """The read-only comparison table's text: the theme's text colour a third
    of the way to the background -- quieter than the primary's, louder than
    :data:`OFF_FG`, in either theme."""
    pal = widget.palette()
    t = pal.color(QtGui.QPalette.ColorRole.Text)
    b = pal.color(QtGui.QPalette.ColorRole.Base)
    f = 0.3
    return QtGui.QColor(round(t.red() + (b.red() - t.red()) * f),
                        round(t.green() + (b.green() - t.green()) * f),
                        round(t.blue() + (b.blue() - t.blue()) * f))


def selection_tsv(table) -> str:
    """The selected cells of ``table`` as tab-separated text, the main table
    editor's Copy (``gui.EntityModel.copy_tsv``): the rectangle around the
    selection, a row per line, each cell's shown text, empty where a cell in
    the rectangle is not selected."""
    idx = table.selectedIndexes()
    if not idx:
        return ""
    sel = {(i.row(), i.column()) for i in idx}
    rows = [r for r, _c in sel]
    cols = [c for _r, c in sel]
    out = []
    for r in range(min(rows), max(rows) + 1):
        line = []
        for c in range(min(cols), max(cols) + 1):
            it = table.item(r, c) if (r, c) in sel else None
            line.append(it.text() if it is not None else "")
        out.append("\t".join(line))
    return "\n".join(out)
