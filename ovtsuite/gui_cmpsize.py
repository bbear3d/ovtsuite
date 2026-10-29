# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table at its original size.

By default the comparison is put onto the primary's rows and columns
(:func:`ovtsuite.transfer.build_overlay`: cell for cell when the two have the
same shape, resampled onto the primary's axes when they do not). **Comparison
size** shows it as it is stored instead: its own rows and columns, axes,
labels and units, in a read-only grid of its own (:class:`NativeView`) with
its own table model. It is a setting like the heat map: each table tab has its
own, and where it is not set the default applies (Tools > Options > Editor >
*Comparison values*, ``ui/compare_size``).

It is changed in four places, none of them a header over a grid (a long table
title would run under it):

* the edit bar's **Comparison** button, beside *Difference view*;
* right-click on a table: **Show comparison at original size**;
* **View > Comparison size**;
* Tools > Options > Editor, for the default; the menus' *Make this the default
  for all tables* writes it and clears every table's own choice.

The grid goes where the layout puts a comparison's own grid (below or beside
the table); the two *same grid* layouts (under the table, matching cells
marked) put it under the table, since one grid cannot hold two shapes.

What the two tables do together depends on whether they have the same size:

* **same rows and columns**: linked as the resampled grid is -- scroll,
  column widths, current cell and every selected cell
  (:class:`ovtsuite.gui_cmpscroll.ViewLink`) -- and a cell that differs has the
  orange outline, in both;
* **different sizes**: nothing is compared with anything, so nothing is linked
  and *every* cell of both tables, the axes included, has the outline
  (``TableModel.outline_all``); the difference view and the tint are off. Each
  table keeps its own selection. Comparison cells selected show where they
  would be copied to: a dashed frame (:class:`TargetFrames`) on the primary's
  cell at the same row and column.

Copying never resamples. **Copy comparison -> primary** writes each selected
comparison cell, as stored, into the primary's cell at the same row and column
of the grid (the cells the primary has no counterpart for are left out and
counted); **Paste at the primary's current cell** puts the selection's text
there as Paste does; **Ctrl+R** (*Copy selected cells from comparison*) is the
same copy the other way round. Each is one undo step.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import options as _opt
from .gui_cellmarks import OutlineDelegate

KEY = "ui/compare_size"
SIZES = (("resampled", "Resampled onto the primary's axes"),
         ("original", "Original size"))
LABELS = dict(SIZES)
_SEL = QtCore.QItemSelectionModel.SelectionFlag


# ------------------------------------------------------------- the setting
def default() -> str:
    return _opt.get(KEY)


def _tab(ws):
    return getattr(ws, "_tt_cur", None)


def size_of(ws) -> str:
    """The comparison's size in ws's showing table: its own setting, else the
    default."""
    t = _tab(ws)
    s = getattr(t, "cmpsize", None) if t is not None else None
    return s if s in LABELS else default()


def overridden(ws) -> bool:
    t = _tab(ws)
    return t is not None and getattr(t, "cmpsize", None) in LABELS


def available(ws) -> bool:
    """A comparison is loaded and has a table to show for this one."""
    m = getattr(ws, "model", None)
    return bool(m is not None and m.entity is not None and m.compare is not None
                and getattr(ws, "comparison", None) is not None)


def original_active(ws) -> bool:
    """The comparison is showing at its original size now."""
    g = getattr(ws, "cmp_grid", None)
    return bool(g is not None and g.native.on)


def set_size(ws, size: Optional[str]) -> None:
    """This table's own size (None: back to the default)."""
    t = _tab(ws)
    if t is None:
        return
    t.cmpsize = size if size in LABELS else None
    refresh(ws)


def make_default(ws, size: Optional[str] = None) -> None:
    """``size`` (default: this table's) becomes the default for every table,
    and no table keeps a choice of its own."""
    size = size or size_of(ws)
    win = getattr(ws, "win", None)
    tabs = []
    for w in (list(win.workspaces()) if win is not None else [ws]):
        try:
            tabs += list(w.tables())
        except Exception:                               # noqa: BLE001
            pass
    for t in tabs:
        t.cmpsize = None
    _opt.set(KEY, size)
    if win is not None:
        apply_all(win)


def refresh(ws) -> None:
    """Show the table again with the size in force (the table is drawn anew,
    as it was left: scroll position and selection kept; its description line
    says what the comparison is)."""
    t = _tab(ws)
    if t is not None and ws.model.entity is not None:
        ws._save_view()
        ws._show_table(t)
    else:
        g = getattr(ws, "cmp_grid", None)
        if g is not None:
            g.refresh()
    refresh_button(ws)


def apply_all(win) -> None:
    """The default changed: every tab follows (unless it has its own)."""
    try:
        tabs = list(win.workspaces())
    except Exception:                                   # noqa: BLE001
        tabs = []
    for ws in tabs:
        try:
            refresh(ws)
        except RuntimeError:                            # a tab being closed
            pass


# --------------------------------------------------------------- the menus
def _note(ws) -> str:
    if not available(ws):
        return "No comparison is loaded for this table."
    if size_of(ws) == "resampled":
        return ("Resampled: the comparison is put onto the primary's rows "
                "and columns.")
    g = getattr(ws, "cmp_grid", None)
    if g is not None and g.native.on and g.native.linked():
        return "Original size: the sizes match, so the two tables are linked."
    return ("Original size: the sizes differ, so the tables are not linked and "
            "every cell is outlined.")


def fill_menu(menu: QtWidgets.QMenu, get_ws: Callable[[], object]
              ) -> QtWidgets.QMenu:
    """The setting, as radio items that read it each time the menu opens."""
    menu.setToolTipsVisible(True)
    head = menu.addAction("Comparison")
    head.setEnabled(False)
    menu.addSection("Show the comparison at")
    group = QtGui.QActionGroup(menu)
    group.setExclusive(True)
    radios = {}
    for size, text in SIZES:
        a = menu.addAction(text)
        a.setCheckable(True)
        group.addAction(a)
        a.triggered.connect(
            lambda _c=False, size=size: set_size(get_ws(), size)
            if get_ws() is not None else None)
        radios[size] = a
    menu.addSeparator()
    use_default = menu.addAction("Use the default for this table")
    use_default.triggered.connect(
        lambda: set_size(get_ws(), None) if get_ws() is not None else None)
    make = menu.addAction("Make this the default for all tables")
    make.triggered.connect(
        lambda: make_default(get_ws()) if get_ws() is not None else None)
    menu.addSeparator()
    note = menu.addAction("")
    note.setEnabled(False)

    def sync():
        ws = get_ws()
        ok = ws is not None and available(ws)
        name = ""
        if ok:
            name = getattr(getattr(ws.comparison, "binfile", None), "name", "")
        head.setText("Comparison: %s" % name if name else "Comparison")
        cur = size_of(ws) if ws is not None else default()
        for size, a in radios.items():
            a.setEnabled(ok)
            a.setChecked(size == cur)
        use_default.setText("Use the default for this table  (%s)"
                            % LABELS[default()].split(" ")[0])
        use_default.setEnabled(ok and overridden(ws))
        make.setEnabled(ok)
        note.setText(_note(ws) if ws is not None else "")
    menu.aboutToShow.connect(sync)
    sync()
    return menu


def add_menu(win, view_menu: QtWidgets.QMenu) -> QtWidgets.QMenu:
    """View > Comparison size (also what the toolbar can offer)."""
    sub = view_menu.addMenu("Comparison size")
    fill_menu(sub, lambda: getattr(win, "current", None))
    return sub


def add_to_menu(ws, menu: QtWidgets.QMenu) -> None:
    """The right-click menu of a table or of the comparison: the switch."""
    if not available(ws):
        return
    menu.addSeparator()
    orig = menu.addAction("Show comparison at original size")
    orig.setCheckable(True)
    orig.setChecked(size_of(ws) == "original")
    orig.setToolTip(_note(ws))
    orig.triggered.connect(
        lambda on: set_size(ws, "original" if on else "resampled"))
    if overridden(ws):
        menu.addAction("Use the default for this table  (%s)"
                       % LABELS[default()].split(" ")[0],
                       lambda: set_size(ws, None))


# ------------------------------------------------------- the edit bar button
def make_button(ws) -> QtWidgets.QPushButton:
    """The **Comparison** button of a tab's edit bar: it says what the
    comparison is showing at, and its menu is the setting."""
    btn = QtWidgets.QPushButton("Comparison")
    btn.setObjectName("comparisonSizeButton")
    btn.setMenu(fill_menu(QtWidgets.QMenu(btn), lambda: ws))
    ws.cmpsize_btn = btn
    ws.model.modelReset.connect(lambda: refresh_button(ws))
    refresh_button(ws)
    return btn


def refresh_button(ws) -> None:
    btn = getattr(ws, "cmpsize_btn", None)
    if btn is None:
        return
    try:
        ok = available(ws)
        btn.setEnabled(ok)
        if ok:
            size = size_of(ws)
            btn.setText("Comparison: %s" % ("original size" if size == "original"
                                            else "resampled"))
            f = btn.font()
            f.setBold(size == "original")
            btn.setFont(f)
            btn.setToolTip("How the comparison table is shown: resampled onto "
                           "the primary's rows and columns, or at its own "
                           "size in a grid of its own.\n" + _note(ws))
        else:
            btn.setText("Comparison")
            btn.setToolTip("Load a comparison to choose how its table is "
                           "shown.")
    except RuntimeError:                # the tab is going away
        pass


# ---------------------------------------------------------- the target frames
class TargetFrames(QtWidgets.QWidget):
    """Dashed frames over the primary's cells that the selected comparison
    cells would be copied to (the same row and column), drawn on the table's
    viewport. It takes no mouse events."""

    def __init__(self, native: "NativeView"):
        super().__init__(native.ws.table.viewport())
        self.native = native
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, _ev):
        n = self.native
        t, m = n.ws.table, n.ws.model
        cells = n.destinations()[0]
        if not cells:
            return
        p = QtGui.QPainter(self)
        try:
            col = t.palette().color(QtGui.QPalette.ColorRole.Highlight)
            p.setPen(QtGui.QPen(col, 2, QtCore.Qt.PenStyle.DashLine))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            for r, c in cells:
                rc = t.visualRect(m.index(r, c))
                if rc.isValid() and rc.intersects(self.rect()):
                    p.drawRect(rc.adjusted(2, 2, -3, -3))
        finally:
            p.end()


# ------------------------------------------------------------ the grid itself
class NativeView(QtCore.QObject):
    """The comparison's table at its original size: its own model and a
    read-only grid, in the comparison box of :class:`CompareGrid`."""

    def __init__(self, grid):
        super().__init__(grid)
        from . import gui as _g
        from .gui_cmpscroll import ViewLink
        self.grid = grid
        self.ws = grid.ws
        t = self.ws.table
        self.on = False
        self.model = _g.TableModel(None, readonly=True)
        v = self.view = QtWidgets.QTableView()
        v.setObjectName("compareNativeView")
        v.setModel(self.model)
        v.setItemDelegate(OutlineDelegate(v))
        v.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setAlternatingRowColors(t.alternatingRowColors())
        v.horizontalHeader().setMinimumSectionSize(
            t.horizontalHeader().minimumSectionSize())
        v.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        v.customContextMenuRequested.connect(self._menu)
        QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, v, self.copy,
                        context=QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        v.hide()
        same = lambda i: i                                  # noqa: E731
        self.link = ViewLink(t, v, same, same, parent=self)
        self.link.enabled = False
        self.target = TargetFrames(self)
        t.viewport().installEventFilter(self)
        v.selectionModel().selectionChanged.connect(self._retarget)
        v.selectionModel().currentChanged.connect(self._retarget)
        for sig in (t.verticalScrollBar().valueChanged,
                    t.horizontalScrollBar().valueChanged,
                    t.horizontalHeader().sectionResized,
                    t.verticalHeader().sectionResized):
            sig.connect(self._retarget)
        self.ws.model.dataChanged.connect(self._primary_changed)

    # -- state ---------------------------------------------------------
    def wanted(self) -> bool:
        return size_of(self.ws) == "original" and available(self.ws)

    def linked(self) -> bool:
        """Same rows and columns, as stored and as shown: cell for cell."""
        m, n = self.ws.model, self.model
        e, c = m.entity, m.compare
        if e is None or c is None or n.entity is None:
            return False
        return (e.rows == c.rows and e.cols == c.cols
                and n.rowCount() == m._n_prim()
                and n.columnCount() == m.columnCount())

    def update(self) -> bool:
        """Bring the original-size table in line with the primary's (a new
        table, a transpose, a Z view, the heat map, the comparison) or, when
        it is not wanted, put the primary's own state back. True when it is
        showing."""
        ws, m, n = self.ws, self.ws.model, self.model
        was_all = m.outline_all
        if not self.wanted():
            self.on = False
            m.outline_all = False
            if was_all:
                self._repaint(m)
            self.link.enabled = False
            self.target.hide()
            return False
        keep = self._selection()
        n.session = ws.comparison
        n.set_entity(m.compare, None)
        n.beginResetModel()
        n.transposed = m.transposed
        n.zmode = m.zmode
        n.vehicle = m.vehicle
        n.endResetModel()
        n.set_heatmap(m._heatmap)
        self.on = True
        linked = self.linked()
        if linked:
            n._vmin, n._vmax = m._vmin, m._vmax
        m.outline_all = not linked
        n.outline_all = not linked
        n.outline_cells = None
        if linked:
            self._outline_differences()
        if m.outline_all != was_all:
            self._repaint(m)
        self._reselect(keep)
        return True

    def _selection(self):
        """``(shape, ranges, current)`` of the grid, to put back after the
        model is read again (an edit redraws everything; the comparison cells
        you selected should still be selected to copy them)."""
        v, n = self.view, self.model
        sm = v.selectionModel()
        rngs = [(r.top(), r.left(), r.bottom(), r.right())
                for r in sm.selection()] if sm is not None else []
        cur = v.currentIndex()
        return ((n.rowCount(), n.columnCount()), rngs,
                (cur.row(), cur.column()) if cur.isValid() else None)

    def _reselect(self, keep) -> None:
        shape, rngs, cur = keep
        n, sm = self.model, self.view.selectionModel()
        if shape != (n.rowCount(), n.columnCount()) or not (rngs or cur):
            return
        sel = QtCore.QItemSelection()
        for top, left, bottom, right in rngs:
            sel.select(n.index(top, left), n.index(bottom, right))
        sm.select(sel, _SEL.ClearAndSelect)
        if cur is not None:
            sm.setCurrentIndex(n.index(*cur), _SEL.NoUpdate)

    def _repaint(self, model) -> None:
        if model.rowCount() and model.columnCount():
            model.dataChanged.emit(model.index(0, 0),
                                   model.index(model.rowCount() - 1,
                                               model.columnCount() - 1))

    def _outline_differences(self) -> None:
        """Same shape: the cells (as the comparison table's entity cells) and
        axis points that differ from the primary's, compared where they are
        shown."""
        m, n = self.ws.model, self.model
        z, ax = set(), set()
        for gr in range(n.rowCount()):
            for gc in range(n.columnCount()):
                kn, an, bn = n._kind(gr, gc)
                km, am, bm = m._kind(gr, gc)
                if kn != km:
                    continue
                if kn == "z":
                    pv, cv = m._at(m._vals, am, bm), n._at(n._vals, an, bn)
                    if pv is not None and cv is not None and abs(pv - cv) > 1e-6:
                        z.add((an, bn))
                elif kn in ("x", "y"):
                    pa = {"x": m._x, "y": m._y}[kn]
                    ca = {"x": n._x, "y": n._y}[kn]
                    if an < len(pa) and an < len(ca) and abs(pa[an] - ca[an]) > 1e-6:
                        ax.add((kn, an))
        n.outline_cells = (frozenset(z), frozenset(ax))

    def _primary_changed(self, *_a) -> None:
        """The primary was edited: which cells differ moves with it."""
        if not self.on or not self.linked():
            return
        self._outline_differences()
        self._repaint(self.model)

    # -- showing -------------------------------------------------------
    def fit(self) -> None:
        """Columns: linked, the primary's are widened where this table needs
        more room and the two kept in step; unlinked, each its own."""
        v, t = self.view, self.ws.table
        if self.linked():
            for c in range(self.ws.model.columnCount()):
                need = v.sizeHintForColumn(c)
                if need > t.columnWidth(c):
                    t.setColumnWidth(c, need)
            self.link.enabled = True
            self.link.sync_all()
        else:
            self.link.enabled = False
            v.resizeColumnsToContents()
            v.clearSelection()
        self._retarget()

    def heading(self) -> str:
        """The comparison label's text (no control shares the line with it)."""
        from . import gui as _g
        ws, m, n = self.ws, self.ws.model, self.model
        name = getattr(m, "cmp_name", "") or getattr(
            getattr(ws.comparison, "binfile", None), "name", "") or "comparison"
        text = "Comparison - %s (read only)" % name
        c, e = m.compare, m.entity
        if c is not None and e is not None:
            text += " · %s, original size" % _g._shape_text(c, n.transposed)
            if not self.linked():
                text += " · not linked: the sizes differ (primary %s)" % \
                    _g._shape_text(e, m.transposed)
        return text

    # -- the copy targets ----------------------------------------------
    def _selected(self):
        sm = self.view.selectionModel()
        idxs = list(sm.selectedIndexes()) if sm is not None else []
        if not idxs and self.view.currentIndex().isValid():
            idxs = [self.view.currentIndex()]
        return idxs

    def destinations(self):
        """``(cells, pairs, total)``: ``cells`` the primary's grid cells that
        the selected comparison cells line up with, ``pairs`` ``[(r, c,
        stored value)]`` the writes they make (the primary's own cell
        coordinates), ``total`` how many comparison cells are selected. A
        comparison cell with no cell at the same row and column in the primary
        is left out."""
        if not self.on:
            return [], [], 0
        m, n = self.ws.model, self.model
        cells, pairs, total = [], [], 0
        for ix in self._selected():
            k, a, b = n._kind(ix.row(), ix.column())
            if k != "z":
                continue
            total += 1
            gr, gc = ix.row(), ix.column()
            if gr >= m._n_prim() or gc >= m.columnCount():
                continue
            pk, pa, pb = m._kind(gr, gc)
            if pk != "z" or m._at(m._vals, pa, pb) is None:
                continue
            v = n._at(n._vals, a, b)
            if v is None:
                continue
            cells.append((gr, gc))
            pairs.append((pa, pb, v))
        return cells, pairs, total

    def _retarget(self, *_a) -> None:
        show = self.on and not self.linked() and bool(self._selected())
        self.target.setVisible(show)
        if show:
            self.target.setGeometry(self.ws.table.viewport().rect())
            self.target.raise_()
            self.target.update()

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Resize and \
                obj is self.ws.table.viewport():
            try:
                self.target.setGeometry(obj.rect())
            except RuntimeError:        # the tab is being torn down
                pass
        return False

    # -- copying ---------------------------------------------------------
    def copy(self) -> None:
        """Ctrl+C: the selected cells as tab-separated text, as the table
        copies its own."""
        idxs = self._selected()
        if not idxs:
            return
        text = self.model.copy_tsv(idxs)
        if text:
            QtWidgets.QApplication.clipboard().setText(text)

    def copy_to_primary(self) -> int:
        """Copy comparison -> primary: the selected cells, as stored, into the
        cells at the same row and column of the primary; one undo step."""
        from . import gui_cellkeys as _gck
        if not _gck._editable(self.ws):
            return 0
        _cells, pairs, total = self.destinations()
        m = self.ws.model
        todo = [(r, c, v) for r, c, v in pairs if v != m._at(m._vals, r, c)]
        out = total - len(pairs)
        if not todo:
            _gck._say(self.ws, "Nothing to copy: the selected cells already "
                      "match%s." % ("" if not out else
                                    " (%d have no cell in the primary)" % out))
            return 0
        n = _gck._write(self.ws, todo, "Copied to the primary")
        if out:
            _gck._say(self.ws, "Copied %d of %d selected cell(s) to the "
                      "primary; %d have no cell there." % (n, total, out))
        return n

    def paste_at_current(self) -> int:
        """Paste at the primary's current cell: the selection's text goes in
        as a Paste of it would."""
        ws = self.ws
        from . import gui_cellkeys as _gck
        if not _gck._editable(ws):
            return 0
        idxs = self._selected()
        if not idxs:
            return 0
        text = self.model.copy_tsv(idxs)
        sel = ws.table.selectionModel().selectedIndexes()
        anchor = (min(sel, key=lambda i: (i.row(), i.column())) if sel
                  else ws.table.currentIndex())
        if anchor is None or not anchor.isValid():
            anchor = ws.model.index(0, 0)
        n = ws.model.paste_tsv(anchor, text)
        if n:
            _gck._say(ws, "Pasted %d value(s) at the primary's current cell." % n)
            try:
                ws._status()
            except Exception:                           # noqa: BLE001
                pass
        return n

    def copy_from_comparison(self) -> int:
        """Ctrl+R: the primary's selected cells from the comparison's cells at
        the same row and column, as stored; one undo step."""
        from . import gui_cellkeys as _gck
        ws, m, n = self.ws, self.ws.model, self.model
        sm = ws.table.selectionModel()
        idxs = list(sm.selectedIndexes()) if sm is not None else []
        if not idxs and ws.table.currentIndex().isValid():
            idxs = [ws.table.currentIndex()]
        todo, none, total = [], 0, 0
        for ix in idxs:
            if ix.row() >= m._n_prim():
                continue
            pk, pa, pb = m._kind(ix.row(), ix.column())
            if pk != "z":
                continue
            total += 1
            if ix.row() >= n.rowCount() or ix.column() >= n.columnCount():
                none += 1
                continue
            k, a, b = n._kind(ix.row(), ix.column())
            v = n._at(n._vals, a, b) if k == "z" else None
            if v is None:
                none += 1
            elif v != m._at(m._vals, pa, pb):
                todo.append((pa, pb, v))
        if not todo:
            _gck._say(ws, "The selected cells already match the comparison%s."
                      % ("" if not none else
                         " (%d have no cell there)" % none))
            return 0
        done = _gck._write(ws, todo, "Copied from the comparison")
        if none:
            _gck._say(ws, "Copied %d of %d selected cell(s); %d have no cell "
                      "in the comparison." % (done, total, none))
        return done

    # -- the menu ---------------------------------------------------------
    def _menu(self, pos) -> None:
        menu = self.build_menu()
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self.view)
        ke = QtGui.QKeySequence.StandardKey
        sel = self._selected()
        cp = menu.addAction("Copy", self.copy)
        cp.setShortcut(QtGui.QKeySequence(ke.Copy))
        cp.setEnabled(bool(sel))
        _cells, pairs, total = self.destinations()
        ed = not (self.ws.model.readonly
                  or getattr(self.ws.model.entity, "read_only", False))
        to = menu.addAction(
            "Copy comparison → primary  (%d of %d cell%s)"
            % (len(pairs), total, "" if total == 1 else "s"),
            self.copy_to_primary)
        to.setEnabled(ed and bool(pairs))
        pa = menu.addAction("Paste at the primary's current cell",
                            self.paste_at_current)
        pa.setEnabled(ed and bool(sel))
        add_to_menu(self.ws, menu)
        return menu
