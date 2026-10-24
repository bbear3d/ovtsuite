# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The 3-D surface of a map (Ctrl+3), live and editable.

It is a panel of the tab itself, above the table in a splitter
(:func:`attach`): **3D surface** on the edit bar, Ctrl+3 or the parameter
menu's *Show 3D surface* shows and hides it, and whether it is shown and how
the splitter is set are remembered for new tabs and the next run. Hidden, it
does nothing -- no drawing, no reading of the map -- and it catches up when
it is shown again.

It follows the table -- another map selected, an edit, undo/redo, the axes
swapped or the Z view converted all redraw it -- and its selection IS the
table's: a point picked on the surface selects that cell in the grid and a
selection made in the grid is marked on the surface. Edits go through the
same model calls as the table (``gui_cellkeys``), so each is one undo step
and "off" cells and converted Z views behave as they do in the grid. On a
read-only tab (a saved version) it turns and selects but does not edit.

Mouse, on the primary surface (the comparison only turns and zooms):

=======================  ==================================================
click a point            select that cell (in the table too)
Shift+click / drag       select the block from the current cell to the point
Ctrl+click               add / remove the point
Ctrl+drag up/down        raise / lower every selected point (snapped to the
                         map's step); Esc while dragging cancels, release
                         writes it as one undo step
drag elsewhere           rotate;  wheel: zoom
=======================  ==================================================

Keys (the surface has the focus):

=======================  ==================================================
arrows                   move the current point the way the arrow points
                         on the screen, however the surface is turned
Shift+arrows             extend the selection
Ctrl(+Shift)+arrows      to the edge of the data, as in the table
[ / ]                    rotate left / right
PgUp / PgDn              tilt up / down
+ / -                    zoom in / out
Home                     reset the view (and the height scale)
Alt+Up/Down ... Ctrl+R   Edit > Cells, exactly as in the table
Ctrl+Z / Ctrl+Y          undo / redo (the window's own)
=======================  ==================================================
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

#: Picking: the nearest point within this many pixels of the click.
PICK_PX = 24
#: A Ctrl+press that moves less than this is a click (toggle), not a drag.
DRAG_PX = 3

_ARROWS = {QtCore.Qt.Key_Up: (-1, 0), QtCore.Qt.Key_Down: (1, 0),
           QtCore.Qt.Key_Left: (0, -1), QtCore.Qt.Key_Right: (0, 1)}
#: Screen direction of each arrow key (x right, y down).
_SCREEN = {QtCore.Qt.Key_Up: (0.0, -1.0), QtCore.Qt.Key_Down: (0.0, 1.0),
           QtCore.Qt.Key_Left: (-1.0, 0.0), QtCore.Qt.Key_Right: (1.0, 0.0)}


def _gui():
    from . import gui                 # loaded by the time a panel is made
    return gui


def _heat(t: float) -> QtGui.QColor:
    return _gui().TableModel._heat(t)


def _fmt(v: float, dec: int = 3) -> str:
    return "%.*f" % (dec, v)


class Surface3DView(QtWidgets.QWidget):
    """A rotatable 3-D surface of a map, drawn with QPainter (no external
    deps, so it survives the frozen build). Height and colour both encode
    the cell value; drag to orbit, wheel to zoom.

    ``vals`` is in display orientation: row ``i`` is a grid row of the
    table, column ``j`` a grid column. With an ``editor`` (see
    :class:`_Editor`) points can be picked, selected and dragged; without
    one the view only turns and zooms."""

    def __init__(self, vals, xs, ys, zunits="", parent=None, zrange=None):
        super().__init__(parent)
        self.xs = xs
        self.ys = ys
        self.zunits = zunits
        self.zrange = zrange
        self.linked = []                    # sibling views that follow
        self.editor = None                  # _Editor, primary view only
        self.preview: Dict[Tuple[int, int], float] = {}
        self.yaw = -0.7
        self.pitch = 0.62
        self.zoom = 1.0
        self.zscale = 0.9
        self.wire = False
        self.dec = 3
        self._last = None
        self._press = None                  # (pos, buttons, modifiers)
        self._drag = None                   # Ctrl+drag state
        self._pts: List[List[QtCore.QPointF]] = []
        self._scale = 1.0
        self.on_reset = None                # called by reset_view (the panel)
        self.set_values(vals, zrange)
        self.setMinimumSize(420, 320)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    # -- data --------------------------------------------------------
    def set_values(self, vals, zrange=None):
        """New values (and, for linked views, a shared range); repaint."""
        self.vals = vals or []
        self.rows = len(self.vals)
        self.cols = len(self.vals[0]) if self.vals else 0
        flat = [v for row in self.vals for v in row]
        self.vmin = min(flat) if flat else 0.0
        self.vmax = max(flat) if flat else 1.0
        self.zrange = zrange
        if zrange is not None:              # shared with a sibling view
            self.vmin, self.vmax = zrange
        self._pts = []
        self.update()

    def value(self, i, j) -> float:
        v = self.preview.get((i, j))
        return self.vals[i][j] if v is None else v

    # -- view --------------------------------------------------------
    def reset_view(self):
        self.yaw, self.pitch, self.zoom = -0.7, 0.62, 1.0
        self._moved()
        if self.on_reset is not None:
            self.on_reset()

    def follow(self, other):
        """Adopt another view's orbit and zoom (no signal, no loop)."""
        self.yaw, self.pitch, self.zoom = other.yaw, other.pitch, other.zoom
        self.update()

    def _moved(self):
        self.update()
        for v in self.linked:
            v.follow(self)

    def orbit(self, dyaw, dpitch):
        self.yaw += dyaw
        self.pitch = max(0.05, min(1.55, self.pitch + dpitch))
        self._moved()

    def zoom_by(self, f):
        self.zoom = max(0.3, min(4.0, self.zoom * f))
        self._moved()

    def set_zscale(self, z):
        self.zscale = z
        self.update()

    def set_wire(self, on):
        self.wire = bool(on)
        self.update()

    # -- projection --------------------------------------------------
    def _rot(self, x, y, z):
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        x1 = x * cy - y * sy
        y1 = x * sy + y * cy
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        y2 = y1 * cp - z * sp
        z2 = y1 * sp + z * cp
        return x1, y2, z2          # x1 screen-x, z2 screen-up, y2 depth

    def point_at(self, pos) -> Optional[Tuple[int, int]]:
        """The (row, col) of the drawn point nearest ``pos``, within
        PICK_PX, or None."""
        best, bd = None, PICK_PX * PICK_PX
        for i, row in enumerate(self._pts):
            for j, p in enumerate(row):
                d = (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2
                if d <= bd:
                    best, bd = (i, j), d
        return best

    def screen_point(self, i, j) -> Optional[QtCore.QPointF]:
        """Where point (i, j) was last drawn (for tests and picking)."""
        try:
            return self._pts[i][j]
        except IndexError:
            return None

    def grid_step(self, key) -> Tuple[int, int]:
        """The (row, col) step that moves the way arrow ``key`` points on
        the screen as the surface is turned now: of the four neighbours,
        the one whose drawn direction is closest to the arrow's."""
        want = _SCREEN[key]
        ac, ar = self._rot(1.0, 0.0, 0.0), self._rot(0.0, 1.0, 0.0)
        cands = []
        for (di, dj), (vx, vz) in (((0, 1), (ac[0], ac[2])),
                                   ((0, -1), (-ac[0], -ac[2])),
                                   ((1, 0), (ar[0], ar[2])),
                                   ((-1, 0), (-ar[0], -ar[2]))):
            sx, sy = vx, -vz                # screen-up is -y
            n = math.hypot(sx, sy) or 1.0
            cands.append(((sx * want[0] + sy * want[1]) / n, (di, dj)))
        return max(cands)[1]

    def value_per_px(self) -> float:
        """Display units one pixel of vertical drag stands for, so a
        dragged point follows the mouse."""
        span = (self.vmax - self.vmin) or 1.0
        lift = self.zscale * self._scale * max(0.25, math.cos(self.pitch))
        return span / (lift or 1.0)

    # -- mouse -------------------------------------------------------
    def mousePressEvent(self, ev):
        pos = ev.position()
        self._last = pos
        self._drag = None
        ed = self.editor
        if ev.button() != QtCore.Qt.LeftButton or ed is None:
            return
        mods = ev.modifiers()
        hit = self.point_at(pos)
        if hit is None:
            return                          # empty space: orbit
        if mods & QtCore.Qt.ControlModifier:
            was = hit in ed.selected()
            if not was:
                ed.toggle(*hit)
            self._drag = {"y": pos.y(), "hit": hit, "was": was,
                          "moved": False}
            self._last = None
        elif mods & QtCore.Qt.ShiftModifier:
            ed.extend_to(*hit)
            self._last = None
            self._drag = {"extend": True}
        else:
            ed.select_one(*hit)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self._drag
        if d is not None and self.editor is not None:
            if d.get("extend"):
                hit = self.point_at(pos)
                if hit is not None:
                    self.editor.extend_to(*hit)
                return
            dy = pos.y() - d["y"]
            if not d["moved"] and abs(dy) < DRAG_PX:
                return
            d["moved"] = True
            self.editor.drag_preview(-dy * self.value_per_px())
            return
        if self._last is not None and ev.buttons() & QtCore.Qt.LeftButton:
            dd = pos - self._last
            self._last = pos
            self.orbit(dd.x() * 0.01, -dd.y() * 0.01)

    def mouseReleaseEvent(self, ev):
        d, self._drag = self._drag, None
        self._last = None
        if d is None or self.editor is None or d.get("extend"):
            return
        if d.get("cancelled"):
            return
        if d["moved"]:
            dy = ev.position().y() - d["y"]
            self.editor.drag_commit(-dy * self.value_per_px())
        elif d["was"]:
            self.editor.toggle(*d["hit"])   # a Ctrl+click on a selected point

    def wheelEvent(self, ev):
        self.zoom_by(1.0 + (ev.angleDelta().y() / 1200.0))

    # -- keys --------------------------------------------------------
    def keyPressEvent(self, ev):
        key, mods = ev.key(), ev.modifiers()
        ctrl = bool(mods & QtCore.Qt.ControlModifier)
        shift = bool(mods & QtCore.Qt.ShiftModifier)
        if key == QtCore.Qt.Key_Escape and self._drag is not None:
            if self.editor is not None and not self._drag.get("extend"):
                self.editor.drag_cancel()
            self._drag = {"cancelled": True}
            return
        if key in _ARROWS and not mods & QtCore.Qt.AltModifier:
            if self.editor is None:         # the comparison: turn it
                di, dj = _ARROWS[key]
                self.orbit(dj * 0.1, -di * 0.08)
            elif ctrl:
                self.editor.jump(self.grid_step(key), shift)
            else:
                self.editor.move(*self.grid_step(key), shift)
            return
        if not mods & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier):
            turn = {QtCore.Qt.Key_BracketLeft: (-0.1, 0.0),
                    QtCore.Qt.Key_BracketRight: (0.1, 0.0),
                    QtCore.Qt.Key_PageUp: (0.0, 0.08),
                    QtCore.Qt.Key_PageDown: (0.0, -0.08)}.get(key)
            if turn is not None:
                self.orbit(*turn)
                return
        if key in (QtCore.Qt.Key_Plus, QtCore.Qt.Key_Equal):
            self.zoom_by(1.15)
            return
        if key in (QtCore.Qt.Key_Minus, QtCore.Qt.Key_Underscore):
            self.zoom_by(1 / 1.15)
            return
        if key == QtCore.Qt.Key_Home:
            self.reset_view()
            return
        super().keyPressEvent(ev)

    # -- drawing -----------------------------------------------------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        if self.rows < 2 or self.cols < 2:
            self._pts = []
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "Not a 2-D map")
            return
        W, H = self.width(), self.height()
        span = self.vmax - self.vmin or 1.0
        R, C = self.rows, self.cols
        # model points, normalised into a unit-ish cube
        P = [[None] * C for _ in range(R)]
        for r in range(R):
            for c in range(C):
                x = (c / (C - 1) - 0.5)
                y = (r / (R - 1) - 0.5)
                z = ((self.value(r, c) - self.vmin) / span - 0.5) \
                    * self.zscale
                P[r][c] = self._rot(x, y, z)
        scale = min(W, H) * 0.62 * self.zoom
        self._scale = scale
        cx, cy = W * 0.5, H * 0.52
        # centre on the cube, not the data: a linked pair sits at the same
        # height, and a point being dragged does not move the whole surface
        corners = [self._rot(x, y, z) for x in (-0.5, 0.5)
                   for y in (-0.5, 0.5)
                   for z in (-0.5 * self.zscale, 0.5 * self.zscale)]
        xs2 = [c[0] for c in corners]
        zs2 = [c[2] for c in corners]
        offx = (max(xs2) + min(xs2)) / 2
        offz = (max(zs2) + min(zs2)) / 2

        def scr(pt):
            return QtCore.QPointF(cx + (pt[0] - offx) * scale,
                                  cy - (pt[2] - offz) * scale)
        self._pts = [[scr(P[r][c]) for c in range(C)] for r in range(R)]
        light = (0.4, -0.5, 0.75)
        ln = math.sqrt(sum(v * v for v in light))
        light = tuple(v / ln for v in light)
        quads = []
        for r in range(R - 1):
            for c in range(C - 1):
                a, b = P[r][c], P[r][c + 1]
                d, e = P[r + 1][c], P[r + 1][c + 1]
                depth = (a[1] + b[1] + d[1] + e[1]) / 4
                hv = (self.value(r, c) + self.value(r, c + 1)
                      + self.value(r + 1, c) + self.value(r + 1, c + 1)) / 4
                quads.append((depth, a, b, e, d, hv))
        quads.sort(key=lambda q: q[0], reverse=True)   # far first
        for depth, a, b, e, d, hv in quads:
            # face normal (in rotated space) for simple shading
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = d[0] - a[0], d[1] - a[1], d[2] - a[2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            bright = 0.55 + 0.45 * abs((nx * light[0] + ny * light[1]
                                        + nz * light[2]) / nl)
            col = _heat((hv - self.vmin) / span)
            col = QtGui.QColor.fromHsvF(
                col.hueF(), col.saturationF(),
                max(0.0, min(1.0, col.valueF() * bright)))
            poly = QtGui.QPolygonF([scr(a), scr(b), scr(e), scr(d)])
            if self.wire:
                p.setBrush(QtCore.Qt.NoBrush)
                p.setPen(QtGui.QPen(col, 1))
            else:
                p.setBrush(col)
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 60), 0.4))
            p.drawPolygon(poly)
        self._draw_marks(p)
        self._draw_hint(p, W, H)

    def _draw_marks(self, p):
        ed = self.editor
        if ed is None:
            return
        sel = ed.selected()
        cur = ed.current()
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 1.2))
        p.setBrush(QtGui.QColor(255, 255, 255))
        for i, j in sel:
            pt = self.screen_point(i, j)
            if pt is not None:
                p.drawEllipse(pt, 3.5, 3.5)
        if cur is not None:
            pt = self.screen_point(*cur)
            if pt is not None:
                p.setBrush(QtCore.Qt.NoBrush)
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 2.4))
                p.drawEllipse(pt, 7, 7)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1.2))
                p.drawEllipse(pt, 7, 7)

    def _draw_hint(self, p, W, H):
        p.setPen(self.palette().text().color())
        fm = p.fontMetrics()
        line = fm.height()
        ed = self.editor
        if ed is not None:
            cur = ed.current()
            if cur is not None:
                n = len(ed.selected())
                txt = ed.describe(*cur) + "   %s" % _fmt(self.value(*cur),
                                                       self.dec)
                if n > 1:
                    txt += "   (%d selected)" % n
                p.drawText(8, 4 + line, txt)
        p.drawText(8, H - 10 - (line if ed is not None else 0),
                   "Z %s..%s %s   X:%d  Y:%d   (drag to rotate, wheel or "
                   "+/- to zoom, Home resets)"
                   % (_fmt(self.vmin, self.dec), _fmt(self.vmax, self.dec),
                      self.zunits, self.cols, self.rows))
        if ed is not None:
            p.drawText(8, H - 10,
                       "Click a point: select   Shift: extend   Ctrl+click: "
                       "add/remove   Ctrl+drag: raise/lower   arrows: move"
                       if ed.can_edit() else
                       "Click a point: select   Shift: extend   Ctrl+click: "
                       "add/remove   (read-only)")


class _Editor:
    """Joins the primary view to a tab's table: selection is the table's,
    edits go through the table's model (one undo step each)."""

    def __init__(self, panel, ws):
        self.panel = panel
        self.ws = ws
        self.anchor: Optional[Tuple[int, int]] = None
        self.base: Dict[Tuple[int, int], float] = {}   # drag start values

    # -- coordinates: view (i, j) is the grid's row/col order -----------
    @property
    def m(self):
        return self.ws.model

    def to_cell(self, i, j) -> Tuple[int, int]:
        return (j, i) if self.m.transposed else (i, j)

    def to_view(self, r, c) -> Tuple[int, int]:
        return (c, r) if self.m.transposed else (r, c)

    def _ix(self, i, j):
        return self.m.index_of("z", *self.to_cell(i, j))

    def describe(self, i, j) -> str:
        r, c = self.to_cell(i, j)
        return "row %d, col %d" % (r + 1, c + 1)

    def can_edit(self) -> bool:
        m = self.m
        return not (m.readonly or getattr(m.entity, "read_only", False))

    # -- selection -------------------------------------------------------
    def _sm(self):
        return self.ws.table.selectionModel()

    def selected(self) -> Set[Tuple[int, int]]:
        from . import gui_cellkeys as K
        if self.m.entity is None:
            return set()
        return {self.to_view(r, c) for r, c in K._z_cells(self.ws)}

    def current(self) -> Optional[Tuple[int, int]]:
        z = self.m.z_at(self.ws.table.currentIndex())
        return self.to_view(*z) if z is not None else None

    def select_one(self, i, j):
        ix = self._ix(i, j)
        if not ix.isValid():
            return
        self._sm().setCurrentIndex(
            ix, QtCore.QItemSelectionModel.ClearAndSelect)
        self.anchor = (i, j)
        self.ws.table.scrollTo(ix)

    def toggle(self, i, j):
        ix = self._ix(i, j)
        if not ix.isValid():
            return
        sm = self._sm()
        sm.select(ix, QtCore.QItemSelectionModel.Toggle)
        sm.setCurrentIndex(ix, QtCore.QItemSelectionModel.NoUpdate)
        self.anchor = (i, j)

    def extend_to(self, i, j):
        a = self.anchor or self.current() or (i, j)
        lo = self._ix(min(a[0], i), min(a[1], j))
        hi = self._ix(max(a[0], i), max(a[1], j))
        if not (lo.isValid() and hi.isValid()):
            return
        sm = self._sm()
        sm.select(QtCore.QItemSelection(lo, hi),
                  QtCore.QItemSelectionModel.ClearAndSelect)
        ix = self._ix(i, j)
        sm.setCurrentIndex(ix, QtCore.QItemSelectionModel.NoUpdate)
        self.anchor = a
        self.ws.table.scrollTo(ix)

    def move(self, di, dj, extend: bool):
        v = self.panel.view
        cur = self.current()
        if cur is None:
            cur = (0, 0)
            di = dj = 0
        i = max(0, min(v.rows - 1, cur[0] + di))
        j = max(0, min(v.cols - 1, cur[1] + dj))
        if extend:
            if self.anchor is None:
                self.anchor = cur
            self.extend_to(i, j)
        else:
            self.select_one(i, j)

    def jump(self, d, extend: bool):
        """Ctrl(+Shift)+arrow: to the edge of the data, as the table does
        (``d`` in the grid's row/col order, as the table's)."""
        keys = getattr(self.ws.table, "_cellkeys", None)
        if keys is not None:
            keys._jump(d, extend)
            if not extend:
                self.anchor = self.current()

    # -- Ctrl+drag ------------------------------------------------------
    def _targets(self):
        """[(i, j, r, c, stored value)] the drag moves: every selected
        cell that holds a number ("off" is none)."""
        m = self.m
        out = []
        for i, j in sorted(self.selected()):
            r, c = self.to_cell(i, j)
            old = m._at(m._vals, r, c)
            if old is None or m._off(old):
                continue
            out.append((i, j, r, c, old))
        return out

    def _new_values(self, delta: float):
        """[(i, j, r, c, new stored, new shown)] for a drag of ``delta``
        display units, snapped to the map's step."""
        from . import gui_cellkeys as K
        m = self.m
        st, _own = K.map_step(self.ws, m.entity)
        out = []
        for i, j, r, c, old in self._targets():
            f = m._zf(c) or 1.0
            d = delta / f
            if st > 0:
                d = round(d / st) * st
            if d == 0:
                continue
            out.append((i, j, r, c, old + d, (old + d) * f))
        return out

    def drag_preview(self, delta: float):
        if not self.can_edit():
            return
        self.panel.view.preview = {(i, j): shown for i, j, _r, _c, _n, shown
                                   in self._new_values(delta)}
        self.panel.view.update()

    def drag_cancel(self):
        self.panel.view.preview = {}
        self.panel.view.update()
        self.panel.say("Height change cancelled.")

    def drag_commit(self, delta: float) -> int:
        self.panel.view.preview = {}
        from . import gui_cellkeys as K
        if not K._editable(self.ws):
            self.panel.view.update()
            return 0
        cells = [(r, c, new) for _i, _j, r, c, new, _s
                 in self._new_values(delta)]
        if not cells:
            self.panel.view.update()
            return 0
        unit = (self.m.entity.units or "").strip()
        n = K._write(self.ws, cells, "Dragged %+g %s" % (delta, unit))
        self.panel.reload()
        self.panel.say("%s: %d cell(s) moved, Ctrl+Z undoes it."
                       % ("Raised" if delta > 0 else "Lowered", n))
        return n


#: Settings: whether the panel is shown, and the splitter's sizes
#: ([surface, table], in pixels) -- for new tabs and the next run.
SHOWN_KEY = "surface3d/shown"
SIZES_KEY = "surface3d/sizes"

_NOTHING = object()                     # "no map read yet" (None is a map)


def _settings():
    from .appsettings import settings
    return settings()


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class SurfacePanel(QtWidgets.QFrame):
    """The 3-D surface panel above a tab's table: the primary surface (and,
    with a comparison loaded, the comparison beside it -- read-only, on one
    height and colour scale, orbit and zoom linked) under a row of controls.

    Made with ``ws`` it follows that tab's table and edits it; without one
    it is a picture of what :meth:`set_values` gives it."""

    def __init__(self, ws=None, parent=None):
        super().__init__(parent)
        self.ws = ws
        self._entity = _NOTHING
        self._zkeep = None                  # (shape, lo, hi): the height scale
        self._pending = False
        self._sel_model = None
        self.cell_actions: List[QtGui.QAction] = []
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 4)
        v.setSpacing(2)
        # -- the control row ---------------------------------------------
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)
        self.title = QtWidgets.QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        self.msg = QtWidgets.QLabel()
        for lb in (self.title, self.msg):        # never widen the window
            lb.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                             QtWidgets.QSizePolicy.Preferred)
            lb.setMinimumWidth(0)
        bar.addWidget(self.title, 2)
        bar.addWidget(self.msg, 3)
        reset = QtWidgets.QToolButton()
        reset.setText("Reset view")
        reset.setToolTip("Back to the starting angle and zoom (Home)")
        bar.addWidget(reset)
        self.wire_chk = QtWidgets.QCheckBox("Wireframe")
        self.wire_chk.setToolTip("Draw only the grid lines")
        bar.addWidget(self.wire_chk)
        bar.addWidget(QtWidgets.QLabel("Height:"))
        hs = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        hs.setRange(10, 200)
        hs.setToolTip("Stretch or flatten the surface (display only)")
        hs.setMaximumWidth(120)
        self.height_slider = hs
        bar.addWidget(hs)
        self.close_btn = QtWidgets.QToolButton()
        self.close_btn.setText("×")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setToolTip("Hide the 3D surface (Ctrl+3)")
        bar.addWidget(self.close_btn)
        v.addLayout(bar)
        # -- the surfaces --------------------------------------------------
        self.view = Surface3DView([], [], [], "", self)
        self.cmp_view = Surface3DView([], [], [], "", self)
        for vw in (self.view, self.cmp_view):
            vw.setMinimumSize(160, 110)          # fits a small screen
        self.view.linked.append(self.cmp_view)
        self.cmp_view.linked.append(self.view)
        self.views = [self.view, self.cmp_view]
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self._cols = []
        self.cmp_label = QtWidgets.QLabel()
        for vw, cap in ((self.view, QtWidgets.QLabel("Primary")),
                        (self.cmp_view, self.cmp_label)):
            box = QtWidgets.QWidget(self)
            col = QtWidgets.QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            cap.setStyleSheet("font-weight: bold;")
            cap.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                              QtWidgets.QSizePolicy.Preferred)
            col.addWidget(cap)
            col.addWidget(vw, 1)
            row.addWidget(box, 1)
            self._cols.append((box, cap))
        v.addLayout(row, 1)
        self._show_cmp(False)
        # start where the surface is drawn, so the first move is no jump
        hs.setValue(round(self.view.zscale * 100))
        reset.clicked.connect(self.view.reset_view)
        self.view.on_reset = self._reset_range
        self.wire_chk.toggled.connect(
            lambda on: [vw.set_wire(on) for vw in self.views])
        hs.valueChanged.connect(
            lambda x: [vw.set_zscale(x / 100.0) for vw in self.views])
        self.close_btn.clicked.connect(self._close_clicked)
        if ws is not None:
            self.view.editor = _Editor(self, ws)
            m = ws.model
            for sig in (m.dataChanged, m.modelReset, m.layoutChanged):
                sig.connect(self._schedule)

    # -- helpers ----------------------------------------------------------
    def _zrange(self, vals, cmp_vals):
        """The height scale: the values' range (with the comparison's),
        only ever widened while the same map is shown -- so a stepped
        point moves on the scale instead of the scale moving with it.
        A new map, a new shape or Reset view starts it again."""
        flat = [x for row in vals for x in row]
        if cmp_vals:
            flat += [x for row in cmp_vals for x in row]
        if not flat:
            return None
        lo, hi = min(flat), max(flat)
        shape = (len(vals), len(vals[0]) if vals else 0)
        keep = self._zkeep
        if keep is not None and keep[0] == shape:
            lo, hi = min(lo, keep[1]), max(hi, keep[2])
        self._zkeep = (shape, lo, hi)
        return (lo, hi)

    def _reset_range(self):
        self._zkeep = None
        self._schedule()

    def _show_cmp(self, on: bool, name: str = ""):
        self.cmp_label.setText("Comparison - %s" % (name or ""))
        self._cols[1][0].setVisible(on)
        self._cols[0][1].setVisible(on)      # "Primary" only beside another

    def _close_clicked(self):
        if self.ws is not None:
            set_shown(self.ws, False)        # turns the toggle off too
        else:
            self.hide()

    def say(self, text: str):
        self.msg.setText(text)

    def set_values(self, vals, cmp_vals=None, zunits="", cmp_name=""):
        """A picture of ``vals`` (no tab): for previews and tests."""
        zr = self._zrange(vals, cmp_vals)
        for vw in self.views:
            vw.zunits = zunits
        self.view.set_values(vals, zr)
        self.cmp_view.set_values(cmp_vals or [], zr)
        self._show_cmp(bool(cmp_vals), cmp_name)

    # -- following the tab --------------------------------------------------
    def showEvent(self, ev):
        super().showEvent(ev)
        if self.ws is not None:
            self._add_actions()
            self.reload()                   # catch up with what it missed

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if self.view.preview or self.view._drag is not None:
            self.view.preview = {}          # a drag cut short writes nothing
            self.view._drag = None

    def _hook_selection(self):
        sm = self.ws.table.selectionModel()
        if sm is self._sel_model or sm is None:
            return
        self._sel_model = sm
        sm.selectionChanged.connect(self._sel_changed)
        sm.currentChanged.connect(self._sel_changed)

    def _sel_changed(self, *_a):
        if self.isVisible():
            self.view.update()

    def _add_actions(self):
        """Edit > Cells while the surface has the focus: the window's own
        actions (bound to the tables) are bound to the surface as well, so a
        shortcut changed in Options changes here too. They act on the tab in
        front -- this one -- through its table's selection, which is the
        surface's. Undo / Redo are the window's and work as they are."""
        acts = list(getattr(getattr(self.ws, "win", None), "_cell_actions",
                            ()) or ())
        # on the panel, so its controls (the height slider, which would
        # take Alt+Up / PgUp for itself) and the comparison have them too
        have = set(self.actions())
        for a in acts:
            if a not in have:
                self.addAction(a)
        self.cell_actions = acts

    def _schedule(self, *_a):
        """Coalesce a burst of model signals into one reload -- none at all
        while the panel is hidden (it reloads when shown)."""
        if not self._pending and self.isVisible():
            self._pending = True
            QtCore.QTimer.singleShot(0, self._deferred)

    def _deferred(self):
        self._pending = False
        try:
            self.reload()
        except RuntimeError:                # the panel or tab is gone
            pass

    def reload(self):
        """Read the table's map again: values, comparison, shape, title.
        Nothing while hidden."""
        ws = self.ws
        self._pending = False
        if ws is None or not self.isVisible():
            return
        self._hook_selection()
        m = ws.model
        e = m.entity
        ed = self.view.editor
        if e is not self._entity:
            self._entity = e
            self._zkeep = None
            ed.anchor = None
            self.view.preview = {}
            self.msg.setText("")
        name = e.title if e is not None else "Select a parameter"
        if e is not None and not ed.can_edit():
            name += "  (read-only)"
        self.title.setText(name)
        self.title.setToolTip(name)
        if e is None or e.rows < 2 or e.cols < 2:
            for vw in self.views:
                vw.set_values([])
            self._show_cmp(False)
            return
        vals = self._shown(m._vals)
        cmp_vals = self._shown(m._cmp) if m._cmp else None
        if cmp_vals and (len(cmp_vals) != len(vals)
                         or any(len(a) != len(b)
                                for a, b in zip(cmp_vals, vals))):
            cmp_vals = None
        zrange = self._zrange(vals, cmp_vals)
        dec = getattr(m, "_dec", 3)
        for vw in self.views:
            vw.zunits = (e.units or "").strip()
            vw.dec = dec
        self.view.set_values(vals, zrange)
        self.cmp_view.set_values(cmp_vals or [], zrange)
        self._show_cmp(bool(cmp_vals), getattr(m, "cmp_name", ""))

    def _shown(self, grid):
        """``grid`` (the entity's own (r, c) order, stored units) as the
        table shows it: in the Z view's units, turned with the table. An
        "off" cell or a gap takes the highest value, so it stands out as a
        plateau rather than a spike."""
        m = self.ws.model
        rows = []
        for r, row in enumerate(grid):
            out = []
            for c, v in enumerate(row):
                if v is None or m._off(v):
                    out.append(None)
                else:
                    out.append(v * (m._zf(c) or 1.0))
            rows.append(out)
        nums = [v for row in rows for v in row if v is not None]
        top = max(nums) if nums else 0.0
        rows = [[top if v is None else v for v in row] for row in rows]
        if m.transposed:
            rows = [list(col) for col in zip(*rows)] if rows else []
        return rows


# ------------------------------------------------------------ the tab's side
def attach(ws) -> QtWidgets.QSplitter:
    """Put a surface panel above ``ws.table``; returns the splitter that
    holds both, for the tab to lay out where the table went. Makes the edit
    bar's **3D surface** button (``ws.surf_btn``) the panel's toggle."""
    split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
    split.setChildrenCollapsible(False)
    panel = SurfacePanel(ws)
    panel.hide()
    split.addWidget(panel)
    split.addWidget(getattr(ws, "table_area", ws.table))   # gui_cmpscroll
    split.setStretchFactor(0, 0)
    split.setStretchFactor(1, 1)
    ws.surface = panel
    ws.surface_split = split
    split.splitterMoved.connect(lambda *_a: _save_sizes(ws))
    btn = getattr(ws, "surf_btn", None)
    if btn is not None:
        btn.setCheckable(True)
        btn.setToolTip("Show or hide the rotatable, editable 3-D surface of "
                       "the map above the table (with the comparison "
                       "alongside when one is loaded)  Ctrl+3")
        btn.toggled.connect(lambda on: set_shown(ws, on))
    if _truthy(_settings().value(SHOWN_KEY, False)):
        set_shown(ws, True, remember=False, focus=False)
    return split


def is_shown(ws) -> bool:
    """Whether ``ws``'s panel is switched on (it may still be out of sight
    while another tab is in front)."""
    p = getattr(ws, "surface", None)
    return p is not None and not p.isHidden()


def set_shown(ws, on: bool, remember: bool = True, focus: bool = True):
    """Show or hide ``ws``'s surface panel -- the toggle's one way in.
    ``remember``: new tabs and the next run start the same way. Returns the
    panel."""
    panel = getattr(ws, "surface", None)
    if panel is None:
        return None
    on = bool(on)
    if on != is_shown(ws):
        if on:
            panel.show()
            _restore_sizes(ws)
            if focus:
                panel.view.setFocus()
        else:
            fw = QtWidgets.QApplication.focusWidget()
            if fw is not None and panel.isAncestorOf(fw):
                ws.table.setFocus()         # the keys go back to the grid
            panel.hide()
    btn = getattr(ws, "surf_btn", None)
    if btn is not None and btn.isChecked() != on:
        btn.blockSignals(True)
        btn.setChecked(on)
        btn.blockSignals(False)
    if remember:
        s = _settings()
        s.setValue(SHOWN_KEY, on)
        s.sync()
    return panel


def _save_sizes(ws):
    from . import gui_panelayout as _gpl    # (another layout keeps its own)
    if not is_shown(ws) or not _gpl.is_stacked(ws):
        return
    sizes = ws.surface_split.sizes()
    if len(sizes) == 2 and all(x > 0 for x in sizes):
        s = _settings()
        s.setValue(SIZES_KEY, ",".join(str(int(x)) for x in sizes))
        s.sync()


def saved_sizes() -> Optional[List[int]]:
    """The splitter's remembered [surface, table] sizes, or None."""
    raw = _settings().value(SIZES_KEY, "")
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    try:
        sizes = [int(x) for x in str(raw or "").split(",") if x.strip()]
    except ValueError:
        return None
    return sizes if len(sizes) == 2 and all(x > 0 for x in sizes) else None


def _restore_sizes(ws):
    from . import gui_panelayout as _gpl
    if not _gpl.is_stacked(ws):
        return
    split = ws.surface_split
    sizes = saved_sizes()
    if sizes is None:
        total = sum(split.sizes()) or split.height() or 600
        top = int(total * 0.45)
        sizes = [top, max(1, total - top)]
    split.setSizes(sizes)


def open_for(ws):
    """Ctrl+3 / the parameter menu: show this tab's surface (it follows
    whichever map the table shows). Returns the panel."""
    return set_shown(ws, True)


