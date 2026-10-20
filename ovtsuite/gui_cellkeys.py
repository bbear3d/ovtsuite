# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Keyboard editing in the map table.

**Edit > Cells** -- menu actions, so every shortcut can be changed in
Tools > Options > Keyboard shortcuts. They act while a map table has the
focus (they are bound to the tables, not the window: Alt+Down still opens a
combo box elsewhere) and each is one undo step over the selected Z cells:

=====================  ===================================================
Alt+Up / Alt+Down       one step up / down -- the map's own step
Alt+Shift+Up / Down     ten steps
Alt+PgUp / Alt+PgDn     +1 % / -1 %
Ctrl+Shift+L            interpolate: along a row, down a column, or a block
                        from its four corners, by the selection's shape
Ctrl+Shift+M            smooth (one 3x3 pass)
Ctrl+R                  copy the selected cells from the comparison
Ctrl+Shift+R            put the selected cells back to the as-read values
F11                     put the selected cells back to the last saved values
=====================  ===================================================

**Set step for this map...** sets the step. The default is one raw step --
the smallest change the map can hold -- and a step set for a map is
remembered for that map (by platform and symbol).

In the table itself (the conventions of a spreadsheet, so not menu items):

* Ctrl+arrow jumps to the edge of the data, Ctrl+Shift+arrow selects to it
  (as Excel does: through a run of values to its end, or across blanks to
  the next value). Each band -- the axes, the map, the comparison -- is its
  own region.
* ``=`` opens a box for a sum over the selection: ``+5``, ``-5``, ``*1.05``,
  ``/2``, ``=120``, ``+3%`` or any expression in ``x``, the cell's value
  (``x*1.1+2``). Values are as shown: in a ZF converted view, engine rpm.
* While a cell is being edited, Ctrl+Enter writes the value into every
  selected cell, and Left at the start of the text / Right at its end keeps
  the value and moves to the next cell.
"""

from __future__ import annotations

import ast
import copy
import operator
from typing import Callable, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding

#: QSettings group the per-map steps live in: ``cellstep/<platform>/<map>``
STEP_KEY = "cellstep"

_Z = ("z",)


# ---------------------------------------------------------------- the step
def _map_key(ws, e) -> str:
    plat = getattr(ws.primary, "platform", "") or "any"
    name = e.name or e.label or "@%X" % e.addr
    return "%s/%s/%s" % (STEP_KEY, plat, name.replace("/", "_"))


def default_step(e) -> float:
    """One raw step of ``e`` in its own units: the smallest change it can
    hold (1 for a float or an unscaled map)."""
    try:
        eq = e.eq
        if getattr(e, "is_float", False):
            return 1.0
        st = abs(float(eq.decode(1)) - float(eq.decode(0)))
        return st if st > 0 else 1.0
    except Exception:
        return 1.0


def map_step(ws, e) -> Tuple[float, bool]:
    """(step for ``e``, whether it was set for this map)."""
    s = QtCore.QSettings(*branding.QSETTINGS)
    v = s.value(_map_key(ws, e))
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    if v > 0:
        return v, True
    return default_step(e), False


def set_map_step(ws, e, value: Optional[float]) -> None:
    """Remember ``value`` as ``e``'s step; None (or 0) goes back to the
    default."""
    s = QtCore.QSettings(*branding.QSETTINGS)
    if value:
        s.setValue(_map_key(ws, e), float(value))
    else:
        s.remove(_map_key(ws, e))
    s.sync()


# ---------------------------------------------------------------- helpers
def _ws(win):
    ws = getattr(win, "current", None)
    if ws is None or getattr(ws, "model", None) is None \
            or ws.model.entity is None:
        return None
    return ws


def _say(ws, text: str):
    try:
        ws.statusBar().showMessage(text, 5000)
    except Exception:
        pass


def _editable(ws) -> bool:
    m = ws.model
    if m.readonly or getattr(m.entity, "read_only", False):
        _say(ws, "This table is read-only.")
        return False
    return True


def _z_cells(ws) -> List[Tuple[int, int]]:
    """The Z cells (r, c) the selection covers; the current cell when there
    is no selection."""
    m = ws.model
    sel = ws.table.selectionModel()
    idxs = list(sel.selectedIndexes()) if sel is not None else []
    if not idxs and ws.table.currentIndex().isValid():
        idxs = [ws.table.currentIndex()]
    out, seen = [], set()
    for ix in idxs:
        z = m.z_at(ix)
        if z is not None and z not in seen:
            seen.add(z)
            out.append(z)
    return out


def _write(ws, cells, what: str) -> int:
    """One undoable edit of ``[(r, c, stored value)]``, then redraw."""
    if not cells:
        _say(ws, "No map cells selected.")
        return 0
    m = ws.model
    m._edit_cells(cells)
    m.refresh()
    _say(ws, "%s: %d cell(s)." % (what, len(cells)))
    try:
        ws._status()
    except Exception:
        pass
    return len(cells)


def _per_cell(ws, fn: Callable[[float, float], Optional[float]], what: str,
              skip_off: bool = True) -> int:
    """Apply ``fn(stored value, display factor) -> new stored value`` (None
    leaves the cell) to every selected Z cell."""
    if not _editable(ws):
        return 0
    m = ws.model
    cells = []
    for r, c in _z_cells(ws):
        old = m._at(m._vals, r, c)
        if old is None or (skip_off and m._off(old)):
            continue                    # "off" is no number to change
        nv = fn(old, m._zf(c) or 1.0)
        if nv is not None and nv != old:
            cells.append((r, c, nv))
    return _write(ws, cells, what)


# ---------------------------------------------------------------- actions
def step(win, k: int):
    ws = _ws(win)
    if ws is None:
        return
    st, own = map_step(ws, ws.model.entity)
    unit = (ws.model.entity.units or "").strip()
    _per_cell(ws, lambda v, f: v + k * st,
              "%+g %s%s" % (k * st, unit, "" if own else " (one raw step)"))


def percent(win, pct: float):
    ws = _ws(win)
    if ws is None:
        return
    _per_cell(ws, lambda v, f: v * (1.0 + pct / 100.0), "%+g %%" % pct)


def set_step(win):
    ws = _ws(win)
    if ws is None:
        return
    e = ws.model.entity
    st, own = map_step(ws, e)
    unit = (e.units or "").strip()
    v, ok = QtWidgets.QInputDialog.getDouble(
        ws, "Step for this map",
        "Alt+Up / Alt+Down change the selected cells of\n%s\nby this much "
        "(%s). 0 goes back to one raw step (%g)." % (
            e.name or e.label, unit or "its units", default_step(e)),
        st if own else 0.0, 0.0, 1e9, 6)
    if ok:
        set_map_step(ws, e, v or None)
        st, own = map_step(ws, e)
        _say(ws, "Step for this map: %g %s%s." % (
            st, unit, "" if own else " (one raw step)"))


def interpolate(win):
    ws = _ws(win)
    if ws is None or not _editable(ws):
        return
    cells = _z_cells(ws)
    if len(cells) < 3:
        _say(ws, "Select at least three cells in a row, a column or a block.")
        return
    rows = {r for r, _ in cells}
    cols = {c for _, c in cells}
    mode = "h" if len(rows) == 1 else "v" if len(cols) == 1 else "2d"
    ws._interp(mode)


def smooth(win):
    ws = _ws(win)
    if ws is not None and _editable(ws):
        ws._smooth_sel()


def from_comparison(win):
    ws = _ws(win)
    if ws is None or not _editable(ws):
        return
    m = ws.model
    if not m._cmp:
        _say(ws, "No comparison is open for this table.")
        return
    cells = []
    for r, c in _z_cells(ws):
        v = m._at(m._cmp, r, c)
        if v is not None and v != m._at(m._vals, r, c):
            cells.append((r, c, v))
    note = ""
    if m.overlay is not None and m.overlay.mode == "resampled":
        note = " (resampled onto this map's axes)"
    _write(ws, cells, "Copied from the comparison" + note)


def _as_read(ws) -> Tuple[Optional[bytes], str]:
    """The image the tab's cells go back to: the branch's as-read original
    in a project, else the file as it was opened."""
    p = ws.primary
    ref = getattr(ws, "project_ref", None)
    if ref is not None:
        try:
            from .project import sessionbridge as _sb
            s = ref.set
            root = s.root_of(ref.branch.head)
            if root is not None:
                main = _sb._main_member(ref.doc.project, ref.sid)
                state = ref.doc.project.state(ref.sid, root.id)
                data = state.get(main.id) if main is not None else None
                if data is None and main is not None:
                    data = state.get(getattr(main, "name", ""))
                if data is not None and len(data) == len(p.binfile.data):
                    return bytes(data), "as read"
        except Exception:
            pass
    orig = getattr(p, "original", None)
    return (bytes(orig), "as opened") if orig is not None else (None, "")


def _last_saved(ws) -> Tuple[Optional[bytes], str]:
    """The image as last saved (in a project: the last save of the tab,
    which is what its history holds), else as opened."""
    p = ws.primary
    data = getattr(p, "_last_saved_snapshot", None)
    if data is None:
        data = getattr(p, "original", None)
    if data is None or len(data) != len(p.binfile.data):
        return None, ""
    return bytes(data), "last saved"


def revert(win):
    _revert_to(_ws(win), _as_read, "There is no original image to go back to.")


def revert_saved(win):
    """Edit > Cells > Revert to last saved values (F11)."""
    _revert_to(_ws(win), _last_saved, "The file has not been saved yet.")


def _revert_to(ws, source, missing: str):
    """Put the selected cells back to their values in the image
    ``source(ws)`` gives, as one undo step."""
    if ws is None or not _editable(ws):
        return
    data, what = source(ws)
    if data is None:
        _say(ws, missing)
        return
    m = ws.model
    from . import resize as _resize
    if m.entity is not None and not _resize.lines_up(ws.primary, m.entity, data):
        # resized or moved since then: the old image's bytes at these cells
        # are other cells, axis points or another table -- never write them
        _say(ws, "%s was resized or moved since the %s version, so its %s "
                 "values no longer line up with its cells: nothing was changed. "
                 "Undo the resize (Edit > Undo, or the version history) to go "
                 "back to it." % (m.entity.name, what, what))
        return
    e = m.entity
    then = copy.copy(e)                  # the same cells, read from `data`
    bf = copy.copy(e.binfile)
    bf.data = bytearray(data)
    then.binfile = bf
    cells = []
    for r, c in _z_cells(ws):
        try:
            v = then.cell(r, c)
        except Exception:
            continue
        if v != m._at(m._vals, r, c):
            cells.append((r, c, v))
    if not cells:
        _say(ws, "The selected cells already hold their %s values." % what)
        return
    _write(ws, cells, "Back to the %s values" % what)


# ---------------------------------------------------------------- maths box
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow}


def _eval(node, x: float) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, x)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in ("x", "X"):
        return x
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, x)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, x), _eval(node.right, x))
    raise ValueError("not a sum")


def parse_expression(text: str) -> Callable[[float], float]:
    """``"+5"``, ``"*1.05"``, ``"=120"``, ``"+3%"``, ``"x*1.1+2"`` -> a
    function of the cell's (shown) value. Raises ValueError."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        raise ValueError("empty")
    if t.startswith("="):
        t = t[1:].strip()
        if "x" not in t.lower():
            v = _eval(ast.parse(t, mode="eval"), 0.0)
            return lambda x: v
    if t.endswith("%") and t[0] in "+-":
        p = float(t[:-1])
        return lambda x: x * (1.0 + p / 100.0)
    if t[0] in "+-*/":
        t = "x" + t
    tree = ast.parse(t, mode="eval")
    _eval(tree, 1.0)                       # reject anything but a sum now
    return lambda x: _eval(tree, x)


def apply_expression(ws, text: str) -> int:
    try:
        fn = parse_expression(text)
    except (ValueError, SyntaxError, ZeroDivisionError):
        _say(ws, "Not a sum: %r. Try +5, -5, *1.05, /2, =120, +3%% or x*1.1+2."
             % text)
        return 0
    setting = text.strip().startswith("=") and "x" not in text.lower()

    def one(v, f):
        try:
            return fn(v * f) / f            # as shown -> stored
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
    return _per_cell(ws, one, "Applied %s" % text.strip(),
                     skip_off=not setting)


def _math_box(ws):
    """A one-line box over the current cell; Enter applies, Esc cancels."""
    if not _editable(ws):
        return
    t = ws.table
    box = QtWidgets.QLineEdit(t.viewport())
    box.setPlaceholderText("+5  *1.05  =120  +3%  x*1.1+2")
    box.setToolTip("A sum for every selected cell: +5, -5, *1.05, /2, =120, "
                   "+3% or an expression in x (the cell's value).\n"
                   "Enter applies it, Esc cancels.")
    rect = t.visualRect(t.currentIndex())
    w = max(rect.width(), box.fontMetrics().horizontalAdvance(
        box.placeholderText()) + 16)
    box.setGeometry(rect.x(), rect.y(), w, max(rect.height(), 22))
    box.setText("=")
    done = []

    def finish(apply_it):
        if done:
            return
        done.append(1)
        text = box.text()
        box.deleteLater()
        t.setFocus()
        if apply_it and text.strip() not in ("", "="):
            apply_expression(ws, text)

    box.returnPressed.connect(lambda: finish(True))
    box.editingFinished.connect(lambda: finish(False))

    class _Esc(QtCore.QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QtCore.QEvent.KeyPress and \
                    ev.key() == QtCore.Qt.Key_Escape:
                finish(False)
                return True
            return False
    box._esc = _Esc(box)
    box.installEventFilter(box._esc)
    box.show()
    box.setFocus()
    box.setCursorPosition(1)


# ---------------------------------------------------------------- navigation
def _blank(m, r, c) -> bool:
    v = m.data(m.index(r, c), QtCore.Qt.DisplayRole)
    return v is None or str(v).strip() == ""


def edge(m, r: int, c: int, dr: int, dc: int) -> Tuple[int, int]:
    """Where Ctrl+arrow lands from (r, c), as in Excel, inside the region
    (axis band, map, comparison) the cell belongs to."""
    kind = m._kind(r, c)[0]
    nr, nc = m.rowCount(), m.columnCount()

    def inside(rr, cc):
        return 0 <= rr < nr and 0 <= cc < nc and m._kind(rr, cc)[0] == kind

    if not inside(r + dr, c + dc):
        return r, c
    if not _blank(m, r, c) and not _blank(m, r + dr, c + dc):
        while inside(r + dr, c + dc) and not _blank(m, r + dr, c + dc):
            r, c = r + dr, c + dc
        return r, c
    while inside(r + dr, c + dc):
        r, c = r + dr, c + dc
        if not _blank(m, r, c):
            break
    return r, c


_ARROWS = {QtCore.Qt.Key_Up: (-1, 0), QtCore.Qt.Key_Down: (1, 0),
           QtCore.Qt.Key_Left: (0, -1), QtCore.Qt.Key_Right: (0, 1)}


class _TableKeys(QtCore.QObject):
    """Ctrl(+Shift)+arrow and ``=`` on a map table."""

    def __init__(self, ws):
        super().__init__(ws.table)
        self.ws = ws

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.KeyPress:
            return False
        t = self.ws.table
        if t.state() == QtWidgets.QAbstractItemView.EditingState:
            return False
        mods = ev.modifiers() & (QtCore.Qt.ControlModifier
                                 | QtCore.Qt.ShiftModifier
                                 | QtCore.Qt.AltModifier)
        if ev.key() in _ARROWS and mods & QtCore.Qt.ControlModifier \
                and not mods & QtCore.Qt.AltModifier:
            return self._jump(_ARROWS[ev.key()],
                              bool(mods & QtCore.Qt.ShiftModifier))
        if ev.text() == "=" and not mods & (QtCore.Qt.ControlModifier
                                            | QtCore.Qt.AltModifier):
            if self.ws.model.entity is not None:
                _math_box(self.ws)
                return True
        return False

    def _jump(self, d, extend: bool) -> bool:
        t, m = self.ws.table, self.ws.model
        cur = t.currentIndex()
        if not cur.isValid() or m.entity is None:
            return False
        r, c = edge(m, cur.row(), cur.column(), *d)
        target = m.index(r, c)
        sm = t.selectionModel()
        if not extend:
            # the target alone: setCurrentIndex(target) would take its flags
            # from the keys held -- Ctrl, so it added the cell to the selection
            sm.setCurrentIndex(target, QtCore.QItemSelectionModel.ClearAndSelect)
            t.scrollTo(target)
            return True
        # the anchor: the corner of the selected block opposite the cursor
        ar, ac = cur.row(), cur.column()
        for rng in sm.selection():
            if rng.top() <= cur.row() <= rng.bottom() and \
                    rng.left() <= cur.column() <= rng.right():
                ar = rng.bottom() if cur.row() == rng.top() else rng.top()
                ac = rng.right() if cur.column() == rng.left() else rng.left()
                break
        sel = QtCore.QItemSelection(
            m.index(min(ar, r), min(ac, c)), m.index(max(ar, r), max(ac, c)))
        sm.select(sel, QtCore.QItemSelectionModel.ClearAndSelect)
        sm.setCurrentIndex(target, QtCore.QItemSelectionModel.NoUpdate)
        t.scrollTo(target)
        return True


class _EditorKeys(QtCore.QObject):
    """In a cell's line editor: Ctrl+Enter fills the selection; Left at the
    start / Right at the end keeps the value and moves to the next cell."""

    def __init__(self, editor, view):
        super().__init__(editor)
        self.editor, self.view = editor, view

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.KeyPress:
            return False
        ed, v = self.editor, self.view
        key, mods = ev.key(), ev.modifiers()
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) \
                and mods & QtCore.Qt.ControlModifier:
            self._fill()
            return True
        if mods & (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier
                   | QtCore.Qt.AltModifier):
            return False
        if ed.hasSelectedText():
            return False
        if key == QtCore.Qt.Key_Left and ed.cursorPosition() == 0:
            self._leave(0, -1)
            return True
        if key == QtCore.Qt.Key_Right and ed.cursorPosition() == len(ed.text()):
            self._leave(0, 1)
            return True
        return False

    def _close(self, commit: bool):
        d = self.view.itemDelegate()
        if commit:
            d.commitData.emit(self.editor)
            d.closeEditor.emit(self.editor,
                               QtWidgets.QAbstractItemDelegate.NoHint)
        else:
            d.closeEditor.emit(self.editor,
                               QtWidgets.QAbstractItemDelegate.RevertModelCache)

    def _leave(self, dr, dc):
        v = self.view
        cur = v.currentIndex()
        self._close(True)
        m = v.model()
        r, c = cur.row() + dr, cur.column() + dc

        def move():
            if 0 <= r < m.rowCount() and 0 <= c < m.columnCount():
                nb = m.index(r, c)
                if nb.flags() & QtCore.Qt.ItemIsSelectable:
                    v.setCurrentIndex(nb)
            v.setFocus()
        QtCore.QTimer.singleShot(0, move)

    def _fill(self):
        text = self.editor.text()
        v = self.view
        ws = getattr(v, "_cellkeys_ws", None)
        self._close(False)
        if ws is None:
            return
        QtCore.QTimer.singleShot(0, lambda: fill(ws, text))


def fill(ws, text: str) -> int:
    """Write ``text`` -- as typed into one cell -- into every selected cell,
    as one undo step (Ctrl+Enter)."""
    if not _editable(ws):
        return 0
    m = ws.model
    cells = []
    for a, b in _z_cells(ws):
        one = m._text_cells(a, b, text)
        if one is None:
            one = m._off_cell(a, b, text)
        if one is None:
            try:
                val = float(text.strip().replace(",", "."))
            except ValueError:
                _say(ws, "Not a number: %r." % text)
                return 0
            f = m._zf(b)
            one = [(a, b, val / f if f else val)]
        cells.extend(one)
    n = _write(ws, cells, "Filled with %s" % text.strip())
    ws.table.setFocus()
    return n


# ---------------------------------------------------------------- wiring
#: (menu text, default shortcut, handler(win))
ACTIONS = [
    ("Step up", "Alt+Up", lambda w: step(w, 1)),
    ("Step down", "Alt+Down", lambda w: step(w, -1)),
    ("Step up x10", "Alt+Shift+Up", lambda w: step(w, 10)),
    ("Step down x10", "Alt+Shift+Down", lambda w: step(w, -10)),
    ("Increase 1%", "Alt+PgUp", lambda w: percent(w, 1.0)),
    ("Decrease 1%", "Alt+PgDown", lambda w: percent(w, -1.0)),
    (None, None, None),
    ("Interpolate selection", "Ctrl+Shift+L", interpolate),
    ("Smooth selection", "Ctrl+Shift+M", smooth),
    (None, None, None),
    ("Copy from comparison", "Ctrl+R", from_comparison),
    ("Revert to as-read values", "Ctrl+Shift+R", revert),
    ("Revert to last saved values", "F11", revert_saved),
    (None, None, None),
    ("Set step for this map...", None, set_step),
]


def add_menu(win, edit_menu) -> QtWidgets.QMenu:
    """Edit > Cells. The actions are bound to each map table (``attach``),
    so they act only while a table has the focus."""
    sub = edit_menu.addMenu("Ce&lls")
    acts = win.__dict__.setdefault("_cell_actions", [])
    for text, keys, fn in ACTIONS:
        if text is None:
            sub.addSeparator()
            continue
        a = QtGui.QAction(text, win)
        if keys:
            a.setShortcut(keys)
        a.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        a.triggered.connect(lambda _c=False, f=fn: f(win))
        sub.addAction(a)
        acts.append(a)
        win._needs_file.append(a)
    win._needs_file.append(sub.menuAction())
    for ws in getattr(win, "workspaces", lambda: [])():
        attach(ws)
    return sub


#: The cell actions the table's right-click menu repeats.
MENU_ACTIONS = ("Revert to last saved values", "Revert to as-read values")


def add_to_menu(win, menu):
    """The revert actions in a table's right-click menu (the same actions,
    so they show their shortcuts and follow Options)."""
    acts = {a.text(): a for a in getattr(win, "_cell_actions", ())}
    have = [acts[t] for t in MENU_ACTIONS if t in acts]
    if have:
        menu.addSeparator()
        for a in have:
            menu.addAction(a)


def attach(ws):
    """Give a workspace's map table the cell actions and its keys."""
    t = getattr(ws, "table", None)
    if t is None or getattr(t, "_cellkeys_ws", None) is ws:
        return
    t._cellkeys_ws = ws
    for a in getattr(ws.win, "_cell_actions", ()):
        t.addAction(a)
    t._cellkeys = _TableKeys(ws)
    t.installEventFilter(t._cellkeys)


def editor_created(editor, view):
    """Called by the table's delegate for each cell editor it makes."""
    if isinstance(editor, QtWidgets.QLineEdit):
        editor._cellkeys = _EditorKeys(editor, view)
        editor.installEventFilter(editor._cellkeys)
    return editor
