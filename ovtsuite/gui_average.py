# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Average with the comparison: move the selected cells part of the way to the
comparison's values.

The number is a **weight, in percent, of the comparison**: 0 keeps the cell as it is, 50
averages the two, 100 is the comparison's value (``new = old + weight / 100 x (comparison -
old)``). It is on the **Modify selection** bar (*Average with comparison*, the box holding
the weight, 50 to begin with), in the table's right-click menu and in **Edit > Cells**
(*Average with comparison* -- 50 %, and *Average with comparison...*, which asks for the
weight), and on selected comparison cells (*Average cells into primary*).

It is only offered when each primary cell is compared with the comparison's value at the same
operating point: the comparison on the primary's axes -- identical axes, or put onto them
(a different size, or *Aligned to the primary's axes*, ``gui_cmpsize``). It is off when the
comparison is the same size on other breakpoints and not aligned (a cell would be averaged
with another operating point's value), when it is shown at its original size, and for tables
whose cells are not numbers (text, lists of choices); hex output tables are averaged byte
for byte, rounded to a whole byte. A cell the comparison has no value
for, or either side's "off", is left as it is.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from . import branding

KEY = "average/weight"
DEFAULT_WEIGHT = 50.0
OP_TEXT = "Average with comparison"
WEIGHT_TIP = ("Percent of the comparison: 0 keeps your values, 50 is halfway, "
              "100 is the comparison's.")


def blend(old: float, other: float, weight: float) -> float:
    """``old`` moved ``weight`` percent of the way to ``other``."""
    return old + weight / 100.0 * (other - old)


def valid_weight(w: float) -> bool:
    return 0.0 <= w <= 100.0


def state(ws) -> Tuple[bool, str]:
    """Whether the table in front can be averaged with its comparison, and if not, why."""
    m = getattr(ws, "model", None)
    e = getattr(m, "entity", None)
    if m is None or e is None:
        return False, "No table is open."
    if not m._cmp or m.compare is None:
        return False, "No comparison is loaded for this table."
    if getattr(e, "text_kind", "") == "ascii" or getattr(e, "options", None):
        return False, ("The cells of this table are text or a list of choices, "
                       "not numbers that can be averaged.")
    if getattr(m, "primary_delta", lambda: "abs")() != "abs":
        return False, ("The primary table shows differences, so it is read only. "
                       "Set the Difference view to Values to average.")
    if m.outline_all:
        return False, ("The comparison is shown at its original size, so no cell is "
                       "compared with another.")
    ov = m.overlay
    if ov is not None and ov.mode == "axis-mismatch":
        return False, ("The comparison is the same size on other breakpoints, so a "
                       "cell would be averaged with another operating point's value. "
                       "Put it on this table's axes first: Comparison > Aligned to "
                       "the primary's axes.")
    return True, ""


def _ws(win):
    from . import gui_cellkeys as K
    return K._ws(win)


def _say(ws, text: str) -> None:
    from . import gui_cellkeys as K
    K._say(ws, text)


def cells_for(ws, cells, weight: float) -> List[tuple]:
    """``[(r, c, new stored value)]`` for ``cells`` (primary ``(r, c)``): those the
    comparison has a value for that is not the cell's own already, neither side "off"."""
    m = ws.model
    out = []
    for r, c in cells:
        old, other = m._at(m._vals, r, c), m._at(m._cmp, r, c)
        if old is None or other is None or m._off(old) or m._off(other):
            continue
        nv = blend(old, other, weight)
        if abs(nv - old) > 1e-9:
            out.append((r, c, nv))
    return out


def run(ws, cells, weight: float, what: str = "") -> int:
    """Average ``cells`` with the comparison at ``weight`` %, as one undoable edit."""
    from . import gui_cellkeys as K
    ok, why = state(ws)
    if not ok:
        _say(ws, why)
        return 0
    if not valid_weight(weight):
        _say(ws, "The weight is a percentage of the comparison: 0 to 100.")
        return 0
    if not K._editable(ws):
        return 0
    todo = cells_for(ws, cells, weight)
    if not todo:
        _say(ws, "Nothing to average: the selected cells already match the comparison, "
                 "or it has no value for them.")
        return 0
    return K._write(ws, todo, what or "Averaged %g %% toward the comparison" % weight)


def average(win, weight: float = DEFAULT_WEIGHT) -> int:
    """Edit > Cells > Average with comparison: the selected cells (the current one when
    none is selected)."""
    ws = _ws(win)
    if ws is None:
        return 0
    from . import gui_cellkeys as K
    return run(ws, K._z_cells(ws), weight)


def last_weight() -> float:
    try:
        w = float(QtCore.QSettings(*branding.QSETTINGS).value(KEY, DEFAULT_WEIGHT))
    except (TypeError, ValueError):
        w = DEFAULT_WEIGHT
    return w if valid_weight(w) else DEFAULT_WEIGHT


def ask(win) -> int:
    """Edit > Cells > Average with comparison...: ask for the weight."""
    ws = _ws(win)
    if ws is None:
        return 0
    ok, why = state(ws)
    if not ok:
        _say(ws, why)
        return 0
    w, got = QtWidgets.QInputDialog.getDouble(
        ws, OP_TEXT, "Percent of the comparison in the result:\n" + WEIGHT_TIP,
        last_weight(), 0.0, 100.0, 1)
    if not got:
        return 0
    QtCore.QSettings(*branding.QSETTINGS).setValue(KEY, w)
    return average(win, w)


def to_primary(ws, view, weight: float = DEFAULT_WEIGHT) -> int:
    """Right-click on selected comparison cells > Average cells into primary."""
    from . import gui_cellkeys as K
    return run(ws, K.comparison_cells(ws, view), weight)


def refresh_actions(win) -> None:
    """Grey the Edit > Cells averaging actions while the table cannot be averaged (and say
    why in their tooltip)."""
    ws = _ws(win)
    ok, why = state(ws) if ws is not None else (False, "No table is open.")
    for a in getattr(win, "_cell_actions", ()):
        if a.text().startswith(OP_TEXT):
            a.setEnabled(ok)
            a.setToolTip(why)
