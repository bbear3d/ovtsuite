# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Relative patch entries in the GUI (:mod:`ovtsuite.patch_relative`).

Two places use this:

* **Apply patch** (``gui.PatchApplyDialog``): a **Change** column (the
  values an entry carries, or the change it makes, in words), a **Copies
  here** column (how many tables of this file it reaches -- a relative entry
  reaches every coding-variant copy), a preview of what the selected row
  would change in THIS file, and a question before a patch whose entries add
  or multiply goes on a file it was already applied to, since a second
  apply adds again.
* **Generate patch** (``gui_differences.DifferencesWindow``): a **Carry
  as** button turning the selected rows into a change -- by difference
  (the same degrees / Nm / hPa added to any target) or by percentage (the
  same ratio, so it scales with a target's own values) -- or back into
  values.

Everything here works on plain patch objects and sessions; the windows only
call it, so it is testable without them.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

VALUES, CHANGE_ABS, CHANGE_PCT = "values", "change", "percent"
CARRY_LABELS = {
    VALUES: "Values (set the table to this file's values)",
    CHANGE_ABS: "Change by difference (add the same amount to any target)",
    CHANGE_PCT: "Change by percentage (scale any target by the same ratio)",
}
#: ops whose result depends on what the table held before -- applying them
#: twice changes the table twice
STACKING = ("add", "scale", "add_grid", "scale_grid")


# ---------------------------------------------------------------- reading
def carried_as(pe) -> str:
    """:data:`VALUES`, :data:`CHANGE_ABS` or :data:`CHANGE_PCT`."""
    if not getattr(pe, "relative", False):
        return VALUES
    if any(op.get("op") in ("scale", "scale_grid") for op in pe.ops or ()):
        return CHANGE_PCT
    return CHANGE_ABS


def change_text(pe) -> str:
    """What an entry does, for a **Change** column."""
    from . import patch_relative as R
    if not getattr(pe, "relative", False):
        if getattr(pe, "whole", False):
            return "values (whole table)"
        return "values"
    text = R.describe(pe.ops)
    if getattr(pe, "only_coding", ""):
        text += "  [only %s]" % pe.only_coding
    return text


def stacks(pe) -> bool:
    """True when applying ``pe`` twice changes the table twice."""
    return bool(getattr(pe, "relative", False)) and any(
        op.get("op") in STACKING for op in pe.ops or ())


def tables_here(pe, target) -> list:
    """The tables of ``target`` that ``pe`` writes."""
    from . import patch as P
    from . import patch_relative as R
    if getattr(pe, "relative", False):
        return R.targets_for(target, pe, P.find_entity, P._variants)
    te = P.find_entity(target, pe.name, getattr(pe, "coding", ""))
    return [te] if te is not None else []


def copies_text(pe, target) -> str:
    n = len(tables_here(pe, target))
    if n == 0:
        if getattr(pe, "only_coding", ""):
            return "none (no %s copy)" % pe.only_coding
        return "none"
    return "%d" % n


def preview(pe, target, limit: int = 12) -> str:
    """What applying ``pe`` would change in ``target``, in plain text: per
    table, how many cells and the first few ``old -> new``."""
    from . import patch as P
    from . import patch_relative as R
    from .model import values_equal
    tables = tables_here(pe, target)
    if not tables:
        return "%s: not in this file -- nothing would change." % pe.name
    lines = []
    for te in tables:
        try:
            if getattr(pe, "relative", False):
                cells = R.cells_for(te, pe.ops or [])
            elif (te.rows, te.cols) == (pe.rows, pe.cols) or pe.kind == "constant":
                cells = [(r, c, v) for r, c, v in P._reconcile(te, pe, target)
                         if not values_equal(te.cell(r, c), v, 1e-9)]
            else:
                lines.append("%s: %dx%d here, %dx%d in the patch -- fitted "
                             "onto this file's axes on apply"
                             % (te.name, te.rows, te.cols, pe.rows, pe.cols))
                continue
        except Exception as exc:                        # noqa: BLE001
            lines.append("%s: cannot be worked out (%s)" % (te.name, exc))
            continue
        head = "%s%s: %d cell(s) would change" % (
            te.name, "  [%s]" % te.coding if getattr(te, "coding", "") else "",
            len(cells))
        lines.append(head)
        xs = _safe_axis(te, "x")
        ys = _safe_axis(te, "y")
        for r, c, v in cells[:limit]:
            where = []
            if ys and r < len(ys):
                where.append("y %g" % ys[r])
            if xs and c < len(xs):
                where.append("x %g" % xs[c])
            lines.append("    %s: %s -> %s" % (
                ", ".join(where) or "[%d,%d]" % (r, c),
                _fmt(te.cell(r, c)), _fmt(v)))
        if len(cells) > limit:
            lines.append("    ...and %d more" % (len(cells) - limit))
    return "\n".join(lines)


def _safe_axis(e, which):
    try:
        return list((e.x_values() if which == "x" else e.y_values()) or [])
    except Exception:                                   # noqa: BLE001
        return []


def _fmt(v) -> str:
    return ("%.4g" % v) if isinstance(v, float) else str(v)


def applied_before(pt, target, title: str) -> int:
    """How many times a patch logged under ``title`` is on ``target`` now:
    applies minus reverts of them, from its change log."""
    from . import patch as P
    try:
        recs = P.list_applied_patches(target)
    except Exception:                                   # noqa: BLE001
        return 0
    applied = sum(1 for r in recs if r.get("source") == title)
    reverted = sum(1 for r in recs if r.get("source") == "revert of %s" % title)
    return max(0, applied - reverted)


def reapply_warning(pt, target, title: str) -> str:
    """The question to ask before applying ``pt`` again, or ``""`` when
    there is none to ask (never applied here, or nothing in it stacks)."""
    stacking = [e.name for e in pt.entries if e.selected and stacks(e)]
    if not stacking:
        return ""
    n = applied_before(pt, target, title)
    if not n:
        return ""
    return ("%s is already applied to this file%s. %d of its checked entries "
            "add or multiply (%s%s), so applying it again changes those tables "
            "a second time.\n\nApply it again anyway?"
            % (title, "" if n == 1 else " (%d times)" % n, len(stacking),
               ", ".join(stacking[:4]), "..." if len(stacking) > 4 else ""))


# ------------------------------------------------------------ making
def as_carried(primary, comparison, pe, how: str):
    """``pe`` (an entry found by the diff of ``primary`` -> ``comparison``)
    carried ``how``: a relative entry by difference or percentage, or ``None``
    when it cannot be (a constant has a difference too; a table whose two
    sides differ in shape, or whose only change is in its breakpoints, has
    none). :data:`VALUES` is the caller's own stashed value entry."""
    from . import patch as P
    from . import patch_relative as R
    base = pe.name.split(" (variant @", 1)[0]
    a = P.find_entity(primary, pe.name, getattr(pe, "coding", "")) \
        or P.find_entity(primary, base)
    b = P.find_entity(comparison, pe.name, getattr(pe, "coding", "")) \
        or P.find_entity(comparison, base)
    if a is None or b is None:
        return None
    rel = R.delta_entry(a, b, percent=(how == CHANGE_PCT), variants=False)
    if rel is None:
        return None
    rel.name = pe.name                      # the copy the diff found
    rel.coding = getattr(pe, "coding", "")
    rel.tier = getattr(pe, "tier", 0)
    rel.categories = list(getattr(pe, "categories", []) or [])
    rel.source_grade = getattr(pe, "source_grade", "")
    rel.changed_cells = getattr(pe, "changed_cells", 0)
    rel.total_cells = getattr(pe, "total_cells", 0)
    rel.selected = getattr(pe, "selected", True)
    return rel


def carry_rows(window, rows: List[int], how: str) -> Tuple[int, List[str]]:
    """Turn entries ``rows`` of a Differences window into ``how``. Value
    entries are kept on the window (``_values_of``) so **Values** brings
    them back. Returns ``(changed, names that could not be)``."""
    stash = window.__dict__.setdefault("_values_of", {})
    done, refused = 0, []
    for r in rows:
        cur = window._entries[r]
        if carried_as(cur) == how:
            continue
        if how == VALUES:
            back = stash.pop(cur.name, None)
            if back is None:
                refused.append(cur.name)
                continue
            back.selected = cur.selected
            window._entries[r] = back
            done += 1
            continue
        source = stash.get(cur.name, cur if not getattr(cur, "relative", False) else None)
        if source is None or getattr(source, "whole", False) and \
                source.changed_cells in (None, 0):
            refused.append(cur.name)
            continue
        rel = as_carried(window.primary, window.comparison, source, how)
        if rel is None:
            refused.append(cur.name)
            continue
        stash.setdefault(cur.name, source)
        window._entries[r] = rel
        done += 1
    return done, refused


def install_carry_button(window, layout):
    """Put the **Carry as** button on a Differences window's patch row."""
    from PySide6 import QtWidgets
    btn = QtWidgets.QToolButton(window)
    btn.setText("Carry as")
    btn.setToolTip(
        "How the selected rows go into the patch: as values (the target's "
        "table set to this file's), or as a change -- the difference, added "
        "to whatever the target holds, or the percentage, scaling it. A "
        "change applies to other software versions whose stock tables "
        "differ, and reaches every coding-variant copy.")
    btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QtWidgets.QMenu(btn)
    for how in (VALUES, CHANGE_ABS, CHANGE_PCT):
        act = menu.addAction(CARRY_LABELS[how])
        act.triggered.connect(lambda _c=False, h=how: _carry_selected(window, h))
    btn.setMenu(menu)
    layout.addWidget(btn)
    window.carry_btn = btn
    return btn


def selected_entry_rows(window) -> List[int]:
    rows = sorted({i.row() for i in window.tbl.selectedIndexes()})
    return [window._rows[r][1] for r in rows
            if r < len(window._rows) and window._rows[r][0] == "entry"]


def _carry_selected(window, how):
    from PySide6 import QtWidgets
    window._sync_checks()
    rows = selected_entry_rows(window)
    if not rows:
        QtWidgets.QMessageBox.information(
            window, "Carry as", "Select the rows to change first.")
        return
    _done, refused = carry_rows(window, rows, how)
    for r in rows:
        window._fill_row(r)
    window._heading()
    if refused:
        QtWidgets.QMessageBox.information(
            window, "Carry as",
            "These stay as they were -- a change needs the same table "
            "shape on both sides and a cell that differs (a table whose "
            "only change is in its breakpoints has none):\n\n  "
            + "\n  ".join(refused[:20]))


def changed_cell_text(pe) -> Optional[str]:
    """The **Cells changed** text for a relative entry, or ``None`` for a
    value entry (the window's own text stands)."""
    how = carried_as(pe)
    if how == VALUES:
        return None
    n = pe.changed_cells
    head = "%d/%d" % (n, pe.total_cells) if n is not None and pe.total_cells else ""
    word = "change, %" if how == CHANGE_PCT else "change, +/-"
    return "%s %s" % (head, word) if head else word
