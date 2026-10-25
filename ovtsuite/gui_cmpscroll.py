# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table in a grid of its own, scrolled with the table.

A table's comparison is normally part of the same grid: the primary's rows,
a banner, then the comparison's rows (``TableModel``), so one set of
scrollers serves both and a tall map pushes the comparison out of sight.
**Tools > Options > Editor > Comparison table** can instead show it

``off``     in the same grid, under the table (as it always was)
``below``   in a read-only grid of its own under the table
``beside``  in a read-only grid of its own to the right of the table
``mark``    in the same grid, with the cells matching the selection marked
            in the other copy (a frame, so the orange difference fill stays)

and in ``below`` / ``beside`` the two grids are kept in step the way the
Measuring blocks dialog keeps its two tables: scrolling (either way, from
either grid), column widths, the current cell and the selection.

Both grids show the SAME model -- the primary grid hides the banner and the
comparison rows, the comparison grid hides the primary rows and the banner
-- so values, the difference fill, the heatmap and axis bands are drawn by
the one model exactly as in ``off``. The comparison is already reconciled to
the primary's shape by ``transfer.build_overlay`` (resampled when the sizes
differ, cell for cell when only the breakpoints do), so primary grid row
``r`` is comparison grid row ``sep + 1 + r`` and columns are shared;
:class:`ViewLink` itself takes any row / column mapping.

:func:`attach` puts ``ws.table`` in a splitter, ``ws.table_area``, which the
panels around the table (:mod:`gui_surface3d`, :mod:`gui_panelayout`) lay out
in its place. In ``off`` and ``mark`` that splitter holds the table alone.

**Ctrl+J** (:data:`JUMP_KEY`) jumps to the matching cells in the other copy
and back: in ``mark`` within the grid, in ``below`` / ``beside`` to the
other grid. In ``off`` it does nothing.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import options as _opt

KEY = "ui/compare_layout"
MODES = (("off", "In the same grid, under the table"),
         ("below", "Below the table, scrolled with it"),
         ("beside", "Beside the table, scrolled with it"),
         ("mark", "In the same grid, matching cells marked"))
JUMP_KEY = "Ctrl+J"

Map = Callable[[int], Optional[int]]
_V, _H = QtCore.Qt.Orientation.Vertical, QtCore.Qt.Orientation.Horizontal
_SEL = QtCore.QItemSelectionModel.SelectionFlag


def mode() -> str:
    m = _opt.get(KEY)
    return m if m in dict(MODES) else "off"


def _same(i: int) -> Optional[int]:
    return i


# ------------------------------------------------------------- the link
class ViewLink(QtCore.QObject):
    """Keeps two item views over models of related shape in step, both ways:
    scroll position (by the section at the top / left edge, so hidden rows
    and different row counts are fine), section sizes, current index and
    selection.

    ``row_map`` takes a row of ``a`` to the row of ``b`` showing the same
    point (None: none does) and ``row_unmap`` back; ``col_map`` /
    ``col_unmap`` likewise for columns (default: the same column)."""

    def __init__(self, a: QtWidgets.QTableView, b: QtWidgets.QTableView,
                 row_map: Map, row_unmap: Map,
                 col_map: Map = _same, col_unmap: Map = _same, parent=None):
        super().__init__(parent or a)
        self.a, self.b = a, b
        self._maps = {(a, _V): row_map, (b, _V): row_unmap,
                      (a, _H): col_map, (b, _H): col_unmap}
        self._busy = False
        self.enabled = True
        for src, dst in ((a, b), (b, a)):
            src.verticalScrollBar().valueChanged.connect(
                lambda _v, s=src, d=dst: self._scrolled(s, d, _V))
            src.horizontalScrollBar().valueChanged.connect(
                lambda _v, s=src, d=dst: self._scrolled(s, d, _H))
            src.horizontalHeader().sectionResized.connect(
                lambda i, _o, w, s=src, d=dst: self._resized(s, d, i, w, _H))
            src.verticalHeader().sectionResized.connect(
                lambda i, _o, w, s=src, d=dst: self._resized(s, d, i, w, _V))
            sm = src.selectionModel()
            sm.currentChanged.connect(
                lambda cur, _p, s=src, d=dst: self._current(s, d, cur))
            sm.selectionChanged.connect(
                lambda *_x, s=src, d=dst: self._selected(s, d))

    # -- mapping ------------------------------------------------------
    def _map(self, src, orient, i: int) -> Optional[int]:
        dst = self.b if src is self.a else self.a
        j = self._maps[(src, orient)](i)
        n = (dst.model().rowCount() if orient == _V
             else dst.model().columnCount())
        return j if j is not None and 0 <= j < n else None

    @staticmethod
    def _header(view, orient) -> QtWidgets.QHeaderView:
        return view.verticalHeader() if orient == _V else view.horizontalHeader()

    @staticmethod
    def _bar(view, orient) -> QtWidgets.QScrollBar:
        return (view.verticalScrollBar() if orient == _V
                else view.horizontalScrollBar())

    @staticmethod
    def _per_item(view, orient) -> bool:
        m = (view.verticalScrollMode() if orient == _V
             else view.horizontalScrollMode())
        return m == QtWidgets.QAbstractItemView.ScrollMode.ScrollPerItem

    # -- scrolling ----------------------------------------------------
    def align(self, src, dst, orient) -> None:
        """Scroll ``dst`` so the section at ``src``'s leading edge (or the
        first one after it that maps) is at ``dst``'s."""
        hs, hd = self._header(src, orient), self._header(dst, orient)
        first = hs.logicalIndexAt(0)
        if first < 0:
            return
        target = None
        for vis in range(hs.visualIndex(first), hs.count()):
            li = hs.logicalIndex(vis)
            if hs.isSectionHidden(li):
                continue
            target = self._map(src, orient, li)
            if target is not None:
                break
        if target is None or hd.isSectionHidden(target):
            return
        bar = self._bar(dst, orient)
        if self._per_item(dst, orient):
            # a per-item scroll bar counts the sections that are shown
            value = sum(1 for v in range(hd.visualIndex(target))
                        if not hd.isSectionHidden(hd.logicalIndex(v)))
        else:
            off = 0
            if li == first:     # keep a part-scrolled section part-scrolled
                off = -hs.sectionViewportPosition(first)
                size = max(1, hs.sectionSize(first))
                off = off * hd.sectionSize(target) // size
            value = hd.sectionPosition(target) + off
        bar.setValue(value)

    def _scrolled(self, src, dst, orient) -> None:
        if self._busy or not self.enabled:
            return
        self._busy = True
        try:
            self.align(src, dst, orient)
        finally:
            self._busy = False

    def sync_all(self) -> None:
        """Bring ``b`` in step with ``a``: sizes, scroll, selection."""
        if not self.enabled:
            return
        self._busy = True
        try:
            for orient in (_H, _V):
                ha, hb = self._header(self.a, orient), self._header(self.b, orient)
                hb.setDefaultSectionSize(ha.defaultSectionSize())
                for i in range(ha.count()):
                    if ha.isSectionHidden(i):
                        continue
                    j = self._map(self.a, orient, i)
                    if j is not None and hb.sectionSize(j) != ha.sectionSize(i):
                        hb.resizeSection(j, ha.sectionSize(i))
            self._mirror(self.a, self.b)
            self.align(self.a, self.b, _V)
            self.align(self.a, self.b, _H)
        finally:
            self._busy = False

    # -- sizes --------------------------------------------------------
    def _resized(self, src, dst, i, w, orient=_H) -> None:
        if self._busy or not self.enabled or w == 0:   # 0: being hidden
            return
        j = self._map(src, orient, i)
        if j is None:
            return
        self._busy = True
        try:
            hd = self._header(dst, orient)
            if hd.sectionSize(j) != w:
                hd.resizeSection(j, w)
        finally:
            self._busy = False

    # -- current cell and selection ----------------------------------
    def _mapped_index(self, src, idx) -> QtCore.QModelIndex:
        if not idx.isValid():
            return QtCore.QModelIndex()
        r = self._map(src, _V, idx.row())
        c = self._map(src, _H, idx.column())
        dst = self.b if src is self.a else self.a
        if r is None or c is None:
            return QtCore.QModelIndex()
        return dst.model().index(r, c)

    def _mirror(self, src, dst) -> None:
        """``dst``'s selection and current cell := ``src``'s, mapped."""
        out = QtCore.QItemSelection()
        m = dst.model()
        for rng in src.selectionModel().selection():
            rows = [self._map(src, _V, r)
                    for r in range(rng.top(), rng.bottom() + 1)]
            cols = [self._map(src, _H, c)
                    for c in range(rng.left(), rng.right() + 1)]
            rows = [r for r in rows if r is not None]
            cols = [c for c in cols if c is not None]
            if rows and cols:
                out.select(m.index(min(rows), min(cols)),
                           m.index(max(rows), max(cols)))
        dsm = dst.selectionModel()
        dsm.select(out, _SEL.ClearAndSelect)
        cur = self._mapped_index(src, src.currentIndex())
        if cur.isValid():
            dsm.setCurrentIndex(cur, _SEL.NoUpdate)

    def _current(self, src, dst, cur) -> None:
        if self._busy or not self.enabled:
            return
        idx = self._mapped_index(src, cur)
        if not idx.isValid():
            return
        self._busy = True
        try:
            dst.selectionModel().setCurrentIndex(idx, _SEL.NoUpdate)
            # the follower may have scrolled to its new current cell; the
            # grid the user is in decides where both are
            self.align(src, dst, _V)
            self.align(src, dst, _H)
        finally:
            self._busy = False

    def _selected(self, src, dst) -> None:
        if self._busy or not self.enabled:
            return
        self._busy = True
        try:
            self._mirror(src, dst)
        finally:
            self._busy = False


# ------------------------------------------------------- the tab's grids
class CompareGrid(QtCore.QObject):
    """A file tab's table area: ``ws.table`` and, in ``below`` / ``beside``,
    the comparison's own grid linked to it."""

    def __init__(self, ws):
        super().__init__(ws.table)
        self.ws = ws
        t = ws.table
        self.split = QtWidgets.QSplitter(_V)
        self.split.setChildrenCollapsible(False)
        self.split.setObjectName("compareSplit")

        self.prim_box, self.prim_head = self._box(t)
        self.view = QtWidgets.QTableView()
        self.view.setObjectName("compareView")
        self.view.setModel(ws.model)
        self.view.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setAlternatingRowColors(t.alternatingRowColors())
        self.view.horizontalHeader().setMinimumSectionSize(
            t.horizontalHeader().minimumSectionSize())
        self.view.setToolTip("")
        self.view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, self.view,
                        self.copy, context=QtCore.Qt.ShortcutContext
                        .WidgetWithChildrenShortcut)
        self.cmp_box, self.cmp_head = self._box(self.view)
        self.split.addWidget(self.prim_box)
        self.split.addWidget(self.cmp_box)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 1)
        self.cmp_box.hide()

        self.link = ViewLink(t, self.view, self._down, self._up, parent=self)
        self.mode = "off"
        self._shown = False         # the comparison grid is in use
        ws.model.modelReset.connect(self.refresh)
        ws.model.dataChanged.connect(self._heading)     # cells differing
        t.verticalHeader().geometriesChanged.connect(self._row_heads)
        self.view.verticalHeader().geometriesChanged.connect(self._row_heads)
        t.installEventFilter(self)
        # mark: a frame over the matching cells, on top of the table's cells
        self.marks = _Marks(self)
        t.viewport().installEventFilter(self)
        sm = t.selectionModel()
        sm.selectionChanged.connect(self._remark)
        sm.currentChanged.connect(self._remark)
        for sig in (t.verticalScrollBar().valueChanged,
                    t.horizontalScrollBar().valueChanged,
                    t.horizontalHeader().sectionResized,
                    t.verticalHeader().sectionResized):
            sig.connect(self._remark)
        self.jump_sc = []
        for view in (t, self.view):
            sc = QtGui.QShortcut(QtGui.QKeySequence(JUMP_KEY), view, self.jump,
                                 context=QtCore.Qt.ShortcutContext
                                 .WidgetWithChildrenShortcut)
            sc.setEnabled(False)
            self.jump_sc.append(sc)
        self.set_mode(mode())

    @staticmethod
    def _box(view):
        box = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        head = QtWidgets.QLabel()
        head.setTextFormat(QtCore.Qt.TextFormat.RichText)
        # a long banner must not widen the grid: it is cut off instead
        head.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                           QtWidgets.QSizePolicy.Policy.Preferred)
        head.hide()
        lay.addWidget(head)
        lay.addWidget(view, 1)
        return box, head

    # -- row mapping (the overlay is on the primary's shape) ----------
    def _sep(self):
        return self.ws.model.sep_row()

    def _down(self, r):
        sep = self._sep()
        return None if sep is None or r >= sep else sep + 1 + r

    def _up(self, r):
        sep = self._sep()
        return None if sep is None or r <= sep else r - sep - 1

    # -- mode ---------------------------------------------------------
    def set_mode(self, m: str) -> None:
        self.mode = m if m in dict(MODES) else "off"
        self.split.setOrientation(_H if self.mode == "beside" else _V)
        self.refresh()

    def active(self) -> bool:
        """The comparison is in its own grid now."""
        return self._shown

    def refresh(self) -> None:
        """After a new table, a transpose, a comparison loaded or dropped, or
        a mode change: which rows each grid shows, and the headings."""
        t, v, m = self.ws.table, self.view, self.ws.model
        sep = m.sep_row()
        on = self.mode in ("below", "beside") and sep is not None
        marking = self.mode == "mark" and sep is not None
        self.marks.setVisible(marking)
        self.marks.setGeometry(t.viewport().rect())
        for sc in self.jump_sc:
            sc.setEnabled(on or marking)
        was = self._shown
        if on or was:       # off and staying off: nothing is hidden
            n = m.rowCount()
            t.setUpdatesEnabled(False)
            try:
                for r in range(n):
                    t.setRowHidden(r, on and r >= sep)
                    v.setRowHidden(r, not on or r <= sep)
            finally:
                t.setUpdatesEnabled(True)
        self._shown = on
        spans = getattr(self.ws, "_apply_spans", None)
        if spans is not None and on != was:
            spans()             # the banner row's span goes with its row
        self.link.enabled = on
        if was and not on:
            v.clearSelection()      # nothing stale for the next time
        self.cmp_box.setVisible(on)
        beside = on and self.mode == "beside"
        v.horizontalHeader().setVisible(beside)
        self._heading()
        self.cmp_head.setVisible(on)
        prim = getattr(getattr(getattr(self.ws, "primary", None),
                               "binfile", None), "name", "")
        self.prim_head.setText("<b>This file:</b> %s" % _esc(prim))
        self.prim_head.setVisible(beside)    # keeps the rows level
        if on and not was:
            self.split.setSizes([1000, 1000])
        self._row_heads()
        if on:
            self._sync_fonts()
            QtCore.QTimer.singleShot(0, self._fit)

    def _heading(self, *_a) -> None:
        """The comparison grid's heading: the banner it hides (file name,
        cells differing, resampled / axes differ), read only."""
        if not self._shown:
            return
        m = self.ws.model
        sep = m.sep_row()
        text = m.data(m.index(sep, 0)) if sep is not None else None
        if not text:
            text = "Comparison: %s" % (getattr(m, "cmp_name", "")
                                       or "comparison")
        self.cmp_head.setText("<b>%s</b> (read only)" % _esc(text))

    def _row_heads(self) -> None:
        """Below: both row-label columns one width, so the cells line up."""
        t, v = self.ws.table.verticalHeader(), self.view.verticalHeader()
        if self._shown and self.mode == "below":
            w = max(t.sizeHint().width(), v.sizeHint().width())
            for h in (t, v):
                if h.minimumWidth() != w:
                    h.setMinimumWidth(w)
        else:
            for h in (t, v):
                if h.minimumWidth():
                    h.setMinimumWidth(0)

    def _fit(self) -> None:
        """Widen a column the comparison's values need more room in than
        the primary's (the table sizes its columns to its own rows), then
        line the comparison grid up with the table."""
        try:
            if not self._shown:
                return
            t = self.ws.table
            for c in range(self.ws.model.columnCount()):
                need = self.view.sizeHintForColumn(c)
                if need > t.columnWidth(c):
                    t.setColumnWidth(c, need)
            self.link.sync_all()
        except RuntimeError:            # the tab is gone
            pass

    def _sync_fonts(self) -> None:
        t, v = self.ws.table, self.view
        v.setFont(t.font())
        v.horizontalHeader().setFont(t.horizontalHeader().font())
        v.verticalHeader().setFont(t.verticalHeader().font())

    def eventFilter(self, obj, ev):
        if obj is not self.ws.__dict__.get("table") and \
                ev.type() == QtCore.QEvent.Type.Resize:
            try:                    # the table's viewport (marks cover it)
                self.marks.setGeometry(obj.rect())
            except RuntimeError:    # the tab is being torn down
                pass
            return False
        if ev.type() == QtCore.QEvent.Type.FontChange and self._shown:
            # Ctrl+wheel zoom (gui_zoom) sets the headers' fonts just after
            QtCore.QTimer.singleShot(0, self._after_zoom)
        return False

    def _after_zoom(self):
        try:
            self._sync_fonts()
            self._row_heads()
            self._fit()
        except RuntimeError:
            pass

    # -- copy ---------------------------------------------------------
    def _menu(self, pos) -> None:
        """Right-click in the comparison grid: copy its cells as text, or
        into the same cells of this file."""
        from . import gui_cellkeys as _gck
        menu = QtWidgets.QMenu(self.view)
        menu.addAction("Copy", self.copy)
        to = menu.addAction("Copy cells to primary",
                            lambda: _gck.to_primary(self.ws, self.view))
        to.setEnabled(bool(_gck.comparison_cells(self.ws, self.view)))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def copy(self) -> None:
        """Ctrl+C in the comparison grid: its selection as tab-separated
        text, as the table copies its own."""
        v = self.view
        idxs = [i for i in v.selectionModel().selectedIndexes()
                if not v.isRowHidden(i.row())]
        if not idxs:
            return
        text = self.ws.model.copy_tsv(idxs)
        if text:
            QtWidgets.QApplication.clipboard().setText(text)


    # -- mark: the matching cells in the same grid --------------------
    def counterpart_row(self, r: int) -> Optional[int]:
        """The grid row showing the same point in the other copy (primary
        row ``r`` <-> comparison row ``sep + 1 + r``), or None."""
        m = self.ws.model
        sep = m.sep_row()
        if sep is None or r == sep:
            return None
        j = sep + 1 + r if r < sep else r - sep - 1
        return j if 0 <= j < m.rowCount() else None

    def marking(self) -> bool:
        return self.mode == "mark" and self.ws.model.sep_row() is not None

    @staticmethod
    def _ranges(view, row_of):
        """``view``'s selection (or its current cell) mapped through
        ``row_of``: [(top, left, bottom, right)] in the model's grid."""
        out = []
        rngs = [(r.top(), r.left(), r.bottom(), r.right())
                for r in view.selectionModel().selection()]
        cur = view.currentIndex()
        if not rngs and cur.isValid():
            rngs = [(cur.row(), cur.column(), cur.row(), cur.column())]
        for top, left, bottom, right in rngs[:64]:
            rows = [row_of(r) for r in range(top, bottom + 1)]
            rows = [r for r in rows if r is not None]
            if rows:
                out.append((min(rows), left, max(rows), right))
        return out

    def mark_ranges(self):
        """The cells marked now: [(top, left, bottom, right)]."""
        if not self.marking():
            return []
        return self._ranges(self.ws.table, self.counterpart_row)

    def _remark(self, *_a) -> None:
        if self.marks.isVisible():
            self.marks.update()

    def jump(self) -> None:
        """Ctrl+J: select the cells matching the selection in the other
        copy and scroll to them -- again to come back."""
        t, v, m = self.ws.table, self.view, self.ws.model
        if self.marking():
            src, dst, row_of = t, t, self.counterpart_row
        elif self._shown:
            src = v if v.hasFocus() else t
            dst = t if src is v else v
            row_of = self._up if src is v else self._down
        else:
            return
        cur = src.currentIndex()
        cur_row = row_of(cur.row()) if cur.isValid() else None
        rngs = self._ranges(src, row_of)
        if cur_row is None and not rngs:
            return
        target = (m.index(cur_row, cur.column()) if cur_row is not None
                  else m.index(rngs[0][0], rngs[0][1]))
        dsm = dst.selectionModel()
        self.link.enabled = False       # one move, not an echo of each step
        try:
            dsm.setCurrentIndex(target, _SEL.ClearAndSelect)
            for top, left, bottom, right in rngs:
                dsm.select(QtCore.QItemSelection(m.index(top, left),
                                                 m.index(bottom, right)),
                           _SEL.Select)
        finally:
            self.link.enabled = self._shown
        dst.scrollTo(target)
        dst.setFocus()
        self._remark()


class _Marks(QtWidgets.QWidget):
    """A frame round each marked range, drawn over the table's viewport:
    a line in the text colour (dark on a light theme, light on a dark one,
    and either way clear of the orange difference fill and the heatmap)
    with the highlight colour inside it. It takes no mouse events."""

    def __init__(self, grid: "CompareGrid"):
        super().__init__(grid.ws.table.viewport())
        self.grid = grid
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.hide()

    def rects(self):
        t, m = self.grid.ws.table, self.grid.ws.model
        out = []
        for top, left, bottom, right in self.grid.mark_ranges():
            r = t.visualRect(m.index(top, left)).united(
                t.visualRect(m.index(bottom, right)))
            if r.isValid() and r.intersects(self.rect()):
                out.append(r)
        return out

    def paintEvent(self, _ev):
        rects = self.rects()
        if not rects:
            return
        pal = self.palette()
        p = QtGui.QPainter(self)
        try:
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            for r in rects:
                p.setPen(QtGui.QPen(pal.color(QtGui.QPalette.ColorRole.Text), 2))
                p.drawRect(r.adjusted(1, 1, -1, -1))
                p.setPen(QtGui.QPen(
                    pal.color(QtGui.QPalette.ColorRole.Highlight), 2))
                p.drawRect(r.adjusted(3, 3, -3, -3))
        finally:
            p.end()


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------- hooks
def attach(ws) -> QtWidgets.QSplitter:
    """Give ``ws`` its table area (``ws.table_area``, the splitter to lay
    out where the table goes) and the comparison grid (``ws.cmp_grid``)."""
    g = CompareGrid(ws)
    ws.cmp_grid = g
    ws.table_area = g.split
    return g.split


def apply_all(win, new: Optional[str] = None) -> None:
    """The option changed: every open tab of ``win`` follows it now."""
    new = new or mode()
    try:
        tabs = list(win.workspaces())
    except Exception:
        return
    for ws in tabs:
        g = getattr(ws, "cmp_grid", None)
        if g is None:
            continue
        try:
            g.set_mode(new)
        except RuntimeError:            # a tab being closed
            pass
