# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Editing in the End of injection window the way the table editor does it
(:mod:`ovtsuite.gui_eoi`).

* **Keyboard shortcuts** -- the table editor's own actions (Edit > Cells, the
  ones Tools > Options > Keyboard shortcuts can change) are bound to the
  window's grids and 3-D surfaces too, and act on what the window shows:
  step, +/-1 %, interpolate, smooth, copy from the comparison, back to the
  as-read / last-saved values. The step is the shown view's (EOI and SOI
  0.5 deg, duration 0.1 deg; **Set step...** changes it). Ctrl+arrow jumps to
  the edge of the data, ``=`` opens a box for a sum over the selection, and
  Ctrl+Z / Ctrl+Y are the tab's undo and redo.
* **Modify selection** -- Set / Add / Subtract / Multiply / Divide, the Fill
  buttons and Smooth, as on the table editor's edit bar.
* **Reset conditions** -- coolant, SOI set, intake air, air pressure, fuel
  temperature and start quantity back to their defaults.
* **Heatmap** -- the table editor's colour ramp, over the grid and the
  comparison; off, the cells are plain.

Every operation reads the values the grid shows (in the shown EOI / SOI /
duration view, or the duration table's own cells), works out the new ones and
hands them to :meth:`EoiDialog.commit`, so each is typed-in values as far as
the tables go: one plan, one undo step, the same limiter and unreachable-target
reporting, the same question before the fuel quantity is touched.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding

Cells = Dict[Tuple[int, int], float]

#: Modify selection, as on the table editor's edit bar
OPS = (("Set =", "set"), ("Add +", "add"), ("Subtract −", "sub"),
       ("Multiply ×", "mul"), ("Divide ÷", "div"))

#: the step of Alt+Up / Alt+Down in each view (degrees); a step set with
#: **Set step...** is remembered under ``eoistep/<view>``
STEPS = {"eoi": 0.5, "soi": 0.5, "dur": 0.1, "tab": 0.1}
STEP_KEY = "eoistep"

#: remembered between runs: the Heatmap tick, the 3D surface
HEAT_KEY = "eoi/heatmap"


def _settings():
    return QtCore.QSettings(*branding.QSETTINGS)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def remembered(key: str, default: bool = False) -> bool:
    try:
        return _truthy(_settings().value(key, default))
    except Exception:                                   # noqa: BLE001
        return default


def remember(key: str, on: bool) -> None:
    try:
        s = _settings()
        s.setValue(key, bool(on))
        s.sync()
    except Exception:                                   # noqa: BLE001
        pass


def step_for(view: str) -> float:
    """The step of ``view`` ("eoi", "soi", "dur" or "tab"), in degrees."""
    try:
        v = float(_settings().value("%s/%s" % (STEP_KEY, view)))
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else STEPS.get(view, 0.1)


def set_step_for(view: str, value: Optional[float]) -> None:
    s = _settings()
    if value:
        s.setValue("%s/%s" % (STEP_KEY, view), float(value))
    else:
        s.remove("%s/%s" % (STEP_KEY, view))
    s.sync()


def heat_color(t: float) -> QtGui.QColor:
    """The table editor's heatmap ramp: blue (low) to red (high)."""
    from . import gui
    return gui.TableModel._heat(t)


# ------------------------------------------------------- the arithmetic
def apply_op(op: str, old: float, v: float) -> float:
    if op == "set":
        return v
    if op == "add":
        return old + v
    if op == "sub":
        return old - v
    if op == "mul":
        return old * v
    return old if v == 0 else old / v                   # dividing by 0: as it was


def rect_of(cells) -> Optional[Tuple[int, int, int, int]]:
    """``(top, bottom, left, right)`` of the block round ``cells``."""
    cells = list(cells)
    if not cells:
        return None
    rs = [r for r, _c in cells]
    cs = [c for _r, c in cells]
    return min(rs), max(rs), min(cs), max(cs)


def interpolated(vals: Cells, cells, mode: str) -> Cells:
    """Table editor's Fill: ``mode`` "h" (each row between the block's left
    and right edges), "v" (each column, top to bottom) or "2d" (bilinear from
    the four corners) over the block round ``cells``; the edge cells stay."""
    rect = rect_of(cells)
    if rect is None:
        return {}
    r0, r1, c0, c1 = rect
    out: Cells = {}
    if mode == "h":
        if c1 == c0:
            return {}
        for r in range(r0, r1 + 1):
            lo, hi = vals[(r, c0)], vals[(r, c1)]
            for c in range(c0 + 1, c1):
                out[(r, c)] = lo + (hi - lo) * (c - c0) / (c1 - c0)
    elif mode == "v":
        if r1 == r0:
            return {}
        for c in range(c0, c1 + 1):
            lo, hi = vals[(r0, c)], vals[(r1, c)]
            for r in range(r0 + 1, r1):
                out[(r, c)] = lo + (hi - lo) * (r - r0) / (r1 - r0)
    else:
        if r1 == r0 or c1 == c0:
            return {}
        v00, v01, v10, v11 = (vals[(r0, c0)], vals[(r0, c1)],
                              vals[(r1, c0)], vals[(r1, c1)])
        for r in range(r0, r1 + 1):
            tr = (r - r0) / (r1 - r0)
            for c in range(c0, c1 + 1):
                tc = (c - c0) / (c1 - c0)
                top = v00 + (v01 - v00) * tc
                bot = v10 + (v11 - v10) * tc
                out[(r, c)] = top + (bot - top) * tr
    return out


def smoothed(vals: Cells, cells) -> Cells:
    """One 3x3 neighbour-average pass over ``cells`` (reads the values as they
    were before the pass)."""
    out: Cells = {}
    for r, c in cells:
        tot, n = 0.0, 0
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                v = vals.get((r + dr, c + dc))
                if v is not None:
                    tot += v
                    n += 1
        if n:
            out[(r, c)] = tot / n
    return out


def edge(blank, nrows: int, ncols: int, r: int, c: int, dr: int, dc: int):
    """Where Ctrl+arrow lands from (r, c), as in Excel (``blank(r, c)``: no
    value there)."""
    def inside(rr, cc):
        return 0 <= rr < nrows and 0 <= cc < ncols
    if not inside(r + dr, c + dc):
        return r, c
    if not blank(r, c) and not blank(r + dr, c + dc):
        while inside(r + dr, c + dc) and not blank(r + dr, c + dc):
            r, c = r + dr, c + dc
        return r, c
    while inside(r + dr, c + dc):
        r, c = r + dr, c + dc
        if not blank(r, c):
            break
    return r, c


# --------------------------------------------------- Edit > Cells actions
#: Edit > Cells action text -> the method of :class:`EoiEditing` it runs
ACTION_METHODS = {
    "Step up": ("step", 1), "Step down": ("step", -1),
    "Step up x10": ("step", 10), "Step down x10": ("step", -10),
    "Increase 1%": ("percent", 1.0), "Decrease 1%": ("percent", -1.0),
    "Interpolate selection": ("interpolate", None),
    "Smooth selection": ("smooth", None),
    "Copy selected cells from comparison": ("from_comparison", None),
    "Revert to as-read values": ("revert", "as-read"),
    "Revert to last saved values": ("revert", "last saved"),
    "Set step for this map...": ("set_step", None),
}


class _GridKeys(QtCore.QObject):
    """Ctrl(+Shift)+arrow and ``=`` in a grid (a cell being edited keeps
    its keys)."""

    ARROWS = {QtCore.Qt.Key_Up: (-1, 0), QtCore.Qt.Key_Down: (1, 0),
              QtCore.Qt.Key_Left: (0, -1), QtCore.Qt.Key_Right: (0, 1)}

    def __init__(self, edit: "EoiEditing", grid):
        super().__init__(grid)
        self.edit, self.grid = edit, grid

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.KeyPress:
            return False
        g = self.grid
        if g.state() == QtWidgets.QAbstractItemView.EditingState:
            return False
        mods = ev.modifiers() & (QtCore.Qt.ControlModifier
                                 | QtCore.Qt.ShiftModifier
                                 | QtCore.Qt.AltModifier)
        if ev.key() in self.ARROWS and mods & QtCore.Qt.ControlModifier \
                and not mods & QtCore.Qt.AltModifier:
            return self.edit.jump(self.ARROWS[ev.key()],
                                  bool(mods & QtCore.Qt.ShiftModifier))
        if ev.text() == "=" and not mods & (QtCore.Qt.ControlModifier
                                            | QtCore.Qt.AltModifier):
            self.edit.math_box()
            return True
        return False


class EoiEditing(QtCore.QObject):
    """The editing side of one :class:`ovtsuite.gui_eoi.EoiDialog`."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.d = dlg
        self._build_bar()
        self.heat = QtWidgets.QCheckBox("Heatmap")
        self.heat.setToolTip("Shade cells by value (blue = low, red = high), "
                             "as the table editor does")
        self.heat.setChecked(remembered(HEAT_KEY))
        self.heat.toggled.connect(self._heat_toggled)
        self.reset = QtWidgets.QPushButton("Reset conditions")
        self.reset.setToolTip(
            "Back to the defaults: coolant at the warm temperature, the base "
            "SOI set, intake air 20 °C, air pressure 1013 hPa, fuel "
            "temperature Reference and start quantity 40 mg/stroke")
        self.reset.clicked.connect(self.reset_conditions)

    # ------------------------------------------------------- the widgets
    def _build_bar(self) -> None:
        bar = QtWidgets.QFrame()
        bar.setFrameShape(QtWidgets.QFrame.StyledPanel)
        # the table editor's wrapping edit bar: a group never splits
        from . import gui
        h = gui._GroupedBar(bar)
        h.setContentsMargins(6, 3, 6, 3)
        self.eb = h
        h.addWidget(QtWidgets.QLabel("Modify selection:"))
        self.op_box = QtWidgets.QComboBox()
        for lbl, key in OPS:
            self.op_box.addItem(lbl, key)
        self.op_box.setCurrentIndex(3)                  # Multiply, as there
        h.addWidget(self.op_box)
        self.op_val = QtWidgets.QLineEdit("1.05")
        self.op_val.setMaximumWidth(110)
        self.op_val.returnPressed.connect(self.apply_bar)
        h.addWidget(self.op_val)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.clicked.connect(self.apply_bar)
        h.addWidget(self.apply_btn)
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.VLine)
        h.addWidget(sep)
        h.addWidget(QtWidgets.QLabel("Fill:"))
        self.fill_btns = []
        for lbl, mode, tip in (
                ("↔", "h", "Interpolate each row between the left and "
                 "right edges of the selection"),
                ("↕", "v", "Interpolate each column between the top and "
                 "bottom edges of the selection"),
                ("▦", "2d", "Bilinear-interpolate the selected block "
                 "from its four corners")):
            b = QtWidgets.QPushButton(lbl)
            b.setMaximumWidth(34)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, m=mode: self.interpolate(m))
            h.addWidget(b)
            self.fill_btns.append(b)
        self.smooth_btn = QtWidgets.QPushButton("Smooth")
        self.smooth_btn.setToolTip("One neighbour-averaging smoothing pass "
                                   "over the selected cells")
        self.smooth_btn.clicked.connect(self.smooth)
        h.addWidget(self.smooth_btn)
        self.bar = bar

    def widgets(self) -> List[QtWidgets.QWidget]:
        return [self.apply_btn, self.smooth_btn, *self.fill_btns]

    def attach(self) -> None:
        """Bind the table editor's cell actions and keys to the grids (the
        dialog's, and later its surfaces') and give the window undo / redo."""
        d = self.d
        for g in (d.grid, d.cmpv.grid):
            g.cell_host = self
            g._eoi_keys = _GridKeys(self, g)
            g.installEventFilter(g._eoi_keys)
        self.bind_actions(d.grid)
        # the tab's undo and redo, as in the main window
        for seq, fn in (("Ctrl+Z", "undo"), ("Ctrl+Y", "redo"),
                        ("Ctrl+Shift+Z", "redo")):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), d)
            sc.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda fn=fn: self._undo(fn))

    def bind_actions(self, widget) -> None:
        """The window's Edit > Cells actions (so a shortcut changed in
        Options changes here too) on ``widget``."""
        widget.cell_host = self
        have = set(widget.actions())
        for a in getattr(getattr(self.d.ws, "win", None), "_cell_actions", ()) or ():
            if a not in have:
                widget.addAction(a)

    def _undo(self, which: str) -> None:
        d = self.d
        try:
            getattr(d.ws, which)()
        finally:
            d.refresh_from_session()
        d.say("Undone." if which == "undo" else "Redone.")

    # -------------------------------------------------- what is selected
    def cells(self, whole_if_none: bool = False) -> List[Tuple[int, int]]:
        d = self.d
        sel = [rc for rc in d._selected_cells() if rc in d._prim_vals]
        if not sel and d.grid.currentRow() >= 0:
            cur = (d.grid.currentRow(), d.grid.currentColumn())
            if cur in d._prim_vals:
                sel = [cur]
        if not sel and whole_if_none:
            sel = sorted(d._prim_vals)
        return sel

    def _ok(self) -> bool:
        """Whether the shown values can be changed now (else say why)."""
        d = self.d
        if d.calc is None:
            return False
        if d._read_only():
            d.say("This table is read-only.")
            return False
        if d.table_view() is None and d.mode() == "idx":
            d.say("Table no. is read-only: show EOI, SOI or Duration to edit.")
            return False
        if d.shows_difference():
            d.say("The primary table shows differences, so it is read only. "
                  "Set the Difference view to Values (or to show it in the "
                  "comparison block) to edit.")
            return False
        return True

    def run(self, values: Cells, what: str) -> int:
        """Hand ``values`` to the window's edit path; say what changed."""
        d = self.d
        cur = d._prim_vals
        changed = {rc: v for rc, v in values.items()
                   if rc in cur and abs(v - cur[rc]) > 1e-9}
        if not changed:
            d.say("%s: no change." % what)
            return 0
        n = d.commit(changed)
        if not n:
            return 0
        msg = "%s: %d cell(s)." % (what, len(changed))
        plan = getattr(d, "_last_plan", None)
        if plan is not None and plan.limited:
            msg += " %d point(s) stopped at the SOI limiter." % len(plan.limited)
        if plan is not None and plan.unreached:
            msg += " %d point(s) can't reach the target." % len(plan.unreached)
        d.say(msg + " Undo puts it back.")
        return n

    # ----------------------------------------------------------- actions
    def apply_bar(self) -> None:
        if not self._ok():
            return
        try:
            v = float(self.op_val.text().strip().replace(",", "."))
        except ValueError:
            self.d.say("Enter a number to apply.")
            return
        op = self.op_box.currentData()
        cur = self.d._prim_vals
        self.run({rc: apply_op(op, cur[rc], v)
                  for rc in self.cells(whole_if_none=True)},
                 "%s %s" % (self.op_box.currentText(), self.op_val.text().strip()))

    def view_key(self) -> str:
        return "tab" if self.d.table_view() is not None else self.d.mode()

    def step(self, k: float) -> None:
        if not self._ok():
            return
        st = step_for(self.view_key()) * k
        cur = self.d._prim_vals
        self.run({rc: cur[rc] + st for rc in self.cells()},
                 "%+g°" % st)

    def percent(self, pct: float) -> None:
        if not self._ok():
            return
        cur = self.d._prim_vals
        self.run({rc: cur[rc] * (1.0 + pct / 100.0) for rc in self.cells()},
                 "%+g %%" % pct)

    def interpolate(self, mode: Optional[str] = None) -> None:
        if not self._ok():
            return
        d = self.d
        sel = self.cells()
        if len(sel) < 2 or (mode is None and len(sel) < 3):
            d.say("Select a range of cells to interpolate (at least three "
                  "in a row, a column or a block).")
            return
        if mode is None:
            rows, cols = {r for r, _ in sel}, {c for _, c in sel}
            mode = "h" if len(rows) == 1 else "v" if len(cols) == 1 else "2d"
        self.run(interpolated(d._prim_vals, sel, mode), "Interpolated")

    def smooth(self) -> None:
        if not self._ok():
            return
        sel = self.cells()
        if len(sel) < 2:
            self.d.say("Select the cells to smooth.")
            return
        self.run(smoothed(self.d._prim_vals, sel), "Smoothed")

    def math_box(self) -> None:
        """``=``: a sum over the selection (+5, *1.05, =120, +3%, x*1.1+2)."""
        if not self._ok():
            return
        from . import gui_cellkeys as K
        text, ok = QtWidgets.QInputDialog.getText(
            self.d, "Apply to the selection",
            "A sum for every selected cell: +5, -5, *1.05, /2, =120, +3% or "
            "an expression in x (the cell's value):", text="=")
        if not ok or text.strip() in ("", "="):
            return
        try:
            fn = K.parse_expression(text)
        except (ValueError, SyntaxError, ZeroDivisionError):
            self.d.say("Not a sum: %r. Try +5, -5, *1.05, /2, =120, +3%% or "
                       "x*1.1+2." % text)
            return
        cur = self.d._prim_vals
        out = {}
        for rc in self.cells(whole_if_none=False):
            try:
                out[rc] = fn(cur[rc])
            except (ZeroDivisionError, OverflowError, ValueError):
                pass
        self.run(out, "Applied %s" % text.strip())

    def from_comparison(self) -> None:
        d = self.d
        if not self._ok():
            return
        if not (d.cmpv.wanted() and d._cmp_vals):
            d.say("No comparison is shown.")
            return
        self.run({rc: d._cmp_vals[rc] for rc in self.cells()
                  if rc in d._cmp_vals}, "Copied from the comparison")

    def set_step(self) -> None:
        view = self.view_key()
        names = {"eoi": "EOI", "soi": "SOI", "dur": "Duration",
                 "tab": "this duration table"}
        cur = step_for(view)
        v, ok = QtWidgets.QInputDialog.getDouble(
            self.d, "Step",
            "Alt+Up / Alt+Down change the selected cells of %s by this many "
            "degrees. 0 goes back to the default (%g)." % (
                names[view], STEPS[view]),
            cur, 0.0, 1000.0, 3)
        if ok:
            set_step_for(view, v or None)
            self.d.say("Step for %s: %g°." % (names[view], step_for(view)))

    # ------------------------------------------------------- jumping
    def jump(self, d_rc: Tuple[int, int], extend: bool) -> bool:
        g = self.d.grid
        r, c = g.currentRow(), g.currentColumn()
        if r < 0 or c < 0:
            return False

        def blank(rr, cc):
            it = g.item(rr, cc)
            return it is None or not it.text().strip()
        nr, nc = edge(blank, g.rowCount(), g.columnCount(), r, c, *d_rc)
        sm = g.selectionModel()
        target = g.model().index(nr, nc)
        if not extend:
            sm.setCurrentIndex(target, QtCore.QItemSelectionModel.ClearAndSelect)
        else:
            ar, ac = r, c
            for rng in sm.selection():
                if rng.top() <= r <= rng.bottom() and rng.left() <= c <= rng.right():
                    ar = rng.bottom() if r == rng.top() else rng.top()
                    ac = rng.right() if c == rng.left() else rng.left()
                    break
            sm.select(QtCore.QItemSelection(
                g.model().index(min(ar, nr), min(ac, nc)),
                g.model().index(max(ar, nr), max(ac, nc))),
                QtCore.QItemSelectionModel.ClearAndSelect)
            sm.setCurrentIndex(target, QtCore.QItemSelectionModel.NoUpdate)
        g.scrollTo(target)
        return True

    # ------------------------------------------------- the action dispatch
    def undo_stack(self):
        """The undo stack the window's edits go to (for the key-hold group)."""
        return getattr(self.d.session, "undo_stack", None)

    def cell_action(self, text: str) -> bool:
        """Run Edit > Cells action ``text`` on this window (True: it was
        ours to run -- the main table's handler must not also run)."""
        hit = ACTION_METHODS.get(text)
        if hit is None:
            return False
        name, arg = hit
        fn = getattr(self, name)
        fn() if arg is None else fn(arg)
        return True

    # ------------------------------------------------------------ revert
    def revert(self, which: str) -> None:
        """The selected cells back to their values in the file as it was read
        (``"as-read"``) or last saved (``"last saved"``): the tables are read
        from that image, the values calculated from them as the window does
        now, and written like typed values -- one undo step."""
        from . import gui_cellkeys as K
        d = self.d
        if not self._ok():
            return
        source = K._as_read if which == "as-read" else K._last_saved
        data, what = source(d.ws)
        if data is None:
            d.say("There is no original image to go back to." if which == "as-read"
                  else "The file has not been saved yet.")
            return
        sel = self.cells()
        if not sel:
            d.say("No cells selected.")
            return
        then = self._values_in(data, what)
        if then is None:
            return
        self.run({rc: then[rc] for rc in sel if rc in then},
                 "Back to the %s values" % what)

    def _involved(self):
        """Every table the shown model reads."""
        m = self.d.model
        sets = [m.soi, m.duration] + [x.set for x in m.soi_sets] + \
            [c.set for c in m.corrections]
        out = []
        for s in sets:
            out += [t for t in list(s.tables) + [s.selector] if t is not None]
        if m.limiter is not None:
            out.append(m.limiter)
        return out

    def _values_in(self, data: bytes, what: str) -> Optional[Cells]:
        """``{(r, c): value}`` of the shown cells as the image ``data`` has
        them, or None (with the reason said)."""
        from . import eoi as _eoi
        from . import resize as _resize
        from .gui_eoi import cell_value
        d = self.d
        s = d.session
        bad = [t for t in self._involved()
               if not _resize.lines_up(s, t, data)]
        if bad:
            d.say("%s was resized or moved since the %s version, so its values "
                  "no longer line up with its cells: nothing was changed. Undo "
                  "the resize (Edit > Undo, or the version history) to go back "
                  "to it." % (bad[0].name, what))
            return None
        bf = copy.copy(s.binfile)
        bf.data = bytearray(data)
        shadow = copy.copy(s)
        shadow.binfile = bf
        shadow.entities = [
            dataclasses.replace(e, binfile=bf) if e.binfile is s.binfile else e
            for e in s.entities]
        try:
            models = _eoi.models(shadow)
            mine = d.model
            m = next((x for x in models if (x.group or "") == (mine.group or "")),
                     None)
            if m is None or m.missing:
                raise ValueError("the %s file has no complete %s dataset"
                                 % (what, mine.label))
            calc = _eoi.Calculator(m, d.conditions(), d.soi_key())
            cool = d.coolant.value()
            k = d.table_view()
            out: Cells = {}
            if k is not None:
                t = calc.dur_t[k]
                for r in range(len(t.vals)):
                    for c in range(len(t.vals[0])):
                        out[(r, c)] = t.vals[r][c]
                return out
            mem = calc.soi_members(cool)
            mode = d.mode()
            for rc, cell in d._cells.items():
                out[rc] = cell_value(mode, calc.cell(cool, cell.rpm, cell.q, mem))[0]
            return out
        except Exception as ex:                          # noqa: BLE001
            d.say("Could not read the %s values: %s" % (what, ex))
            return None

    # ------------------------------------------------ reset / heat
    def defaults(self) -> Dict[str, object]:
        from . import eoi as _eoi
        d = self.d
        out: Dict[str, object] = {"coolant": d.model.coolant_warm, "set": 0}
        for key, (_l, _u, dflt, lo, _hi) in _eoi.CONDITIONS.items():
            out[key] = (lo - 1.0) if dflt is None else dflt
        return out

    def is_default(self) -> bool:
        d = self.d
        df = self.defaults()
        if d.coolant.value() != df["coolant"] or d.soi_box.currentIndex() != 0:
            return False
        return all(sp.value() == df[k] for k, (_l, sp) in d.cond.items())

    def reset_conditions(self) -> None:
        """Coolant, SOI set and every condition back to their defaults."""
        d = self.d
        df = self.defaults()
        boxes = [d.coolant, d.soi_box] + [sp for _l, sp in d.cond.values()]
        for b in boxes:
            b.blockSignals(True)
        try:
            d.coolant.setValue(df["coolant"])
            d.soi_box.setCurrentIndex(0)
            for k, (_l, sp) in d.cond.items():
                sp.setValue(df[k])
        finally:
            for b in boxes:
                b.blockSignals(False)
        cond = d.conditions()
        for c in d._calcs.values():
            c.conditions.update(cond)
        d._set_changed()
        self.refresh_reset()
        d.say("Conditions are back to their defaults.")

    def refresh_reset(self) -> None:
        """Enabled while something differs from the defaults; the boxes that
        do are in bold."""
        d = self.d
        df = self.defaults()
        changed = {"coolant": d.coolant.value() != df["coolant"]}
        for k, (_l, sp) in d.cond.items():
            changed[k] = sp.value() != df[k]
        widgets = {"coolant": d.coolant, **{k: sp for k, (_l, sp) in d.cond.items()}}
        for k, w in widgets.items():
            f = w.font()
            f.setBold(changed[k])
            w.setFont(f)
        f = d.soi_box.font()
        f.setBold(d.soi_box.currentIndex() != 0)
        d.soi_box.setFont(f)
        self.reset.setEnabled(not self.is_default())

    def _heat_toggled(self, on: bool) -> None:
        remember(HEAT_KEY, on)
        self.d.rebuild()
