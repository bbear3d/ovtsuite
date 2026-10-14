# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Entities as JSON a language model can read, and finding them again.

Everything here is read from the entity the module built: its symbol, the
catalogue name, axes, units, the equation-decoded values, the allowed range
and the placement grade. Nothing is inferred from the bytes.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..model import TRUSTED_PLACEMENT, placement_trusted, values_equal
from .tools import ToolError

#: Most cells a single get_map returns; bigger tables are asked for in parts.
MAX_CELLS = 4096


def num(v):
    """A display value as JSON: 6 significant digits, ints as ints."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if math.isnan(f) or math.isinf(f):
        return None
    if f == int(f) and abs(f) < 1e15:
        return int(f)
    return float("%.6g" % f)


def title(e) -> str:
    """The name the app's tree shows: the catalogue name, else the label,
    else the description, else the symbol."""
    try:
        from .. import catalogue
        cur = catalogue.display_name(e)
        if cur:
            return cur
    except Exception:
        pass
    stored = getattr(e, "_ui_label", None)
    if stored:
        return stored
    # the description's first real segment -- not the "A2L: <symbol>" stamp
    segs = [x.strip() for x in (e.description or "").split("|")
            if x.strip() and not x.strip().startswith("A2L:")]
    for cand in (getattr(e, "label", ""), segs[0] if segs else "", e.name):
        cand = (cand or "").strip()
        if cand:
            return cand
    return "@0x%X" % e.addr


def all_entities(session) -> list:
    ents = list(session.entities)
    try:
        ents += list(session.variant_entities())
    except Exception:
        pass
    return ents


class Index:
    """Stable ids for one session's entities.

    The id is the symbol (what patches and the KB key on); an entity without
    one -- a structurally found ZF schedule -- uses its label. Where two share
    an id, each gets ``@0x<addr>`` appended."""

    def __init__(self, session, entities=None):
        """``entities``: ``all_entities(session)`` already taken (on the GUI
        thread), so the index can be built on another."""
        self.session = session
        ents = list(entities) if entities is not None else all_entities(session)
        base: Dict[str, list] = {}
        for e in ents:
            base.setdefault(self._base_id(e), []).append(e)
        self.by_id: Dict[str, object] = {}
        self.id_of: Dict[int, str] = {}
        for bid, group in base.items():
            for e in group:
                eid = bid if len(group) == 1 else "%s@0x%X" % (bid, e.addr)
                self.by_id[eid] = e
                self.id_of[id(e)] = eid
        self.ordered = ents

    @staticmethod
    def _base_id(e) -> str:
        return (e.name or getattr(e, "label", "") or "").strip() or "@0x%X" % e.addr

    def eid(self, e) -> str:
        return self.id_of.get(id(e)) or self._base_id(e)

    def resolve(self, key: str):
        """The entity ``key`` names: an id, a symbol, or the exact title
        (case-insensitive). Ambiguity is an error listing the candidates --
        never a guess."""
        key = (key or "").strip()
        if not key:
            raise ToolError("no map id given")
        if key in self.by_id:
            return self.by_id[key]
        low = key.lower()
        hits = [e for e in self.ordered
                if (e.name or "").lower() == low
                or (getattr(e, "label", "") or "").lower() == low
                or title(e).lower() == low]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolError("%r matches %d parameters; use one of these ids: %s"
                            % (key, len(hits), ", ".join(self.eid(e) for e in hits[:20])))
        raise ToolError("no parameter %r in this binary. Use find_maps to "
                        "search by name or description." % key)


def axis_json(e, ax, values) -> Optional[dict]:
    if ax is None and not values:
        return None
    d = {"values": [num(v) for v in values]}
    if ax is not None:
        if getattr(ax, "units", ""):
            d["units"] = ax.units
        labels = getattr(ax, "labels", None)
        if labels:
            d["labels"] = list(labels)
    return d


def placement(e) -> dict:
    src = getattr(e, "source", "") or ""
    return {"grade": src, "trusted": placement_trusted(e),
            "note": "" if placement_trusted(e) else
            "address not established (%s); edits need explicit confirmation"
            % (src or "unknown")}


def summary(index: Index, e, cmp_entity=None, cmp_session=None) -> dict:
    d = {"id": index.eid(e), "title": title(e), "symbol": e.name or "",
         "kind": e.kind, "shape": [e.rows, e.cols]}
    if e.units:
        d["units"] = e.units
    path = list(getattr(e, "category_path", None) or e.categories or [])
    if path:
        d["category"] = " / ".join(path)
    if not placement_trusted(e):
        d["placement"] = getattr(e, "source", "") or "unknown"
    if cmp_session is not None:
        d["changed"] = changed(e, cmp_entity)
    return d


def changed(e, cp) -> Optional[bool]:
    """True/False against the comparison's counterpart; None when it has no
    counterpart (the comparison does not define this parameter)."""
    if cp is None:
        return None
    if (e.rows, e.cols) != (cp.rows, cp.cols):
        return True
    try:
        a, b = e.values(), cp.values()
    except Exception:
        return None
    return any(not values_equal(a[r][c], b[r][c], 1e-9)
               for r in range(e.rows) for c in range(e.cols))


#: parameter roles that identify a car or open it: never shown to an AI
PROTECTED_ROLES = {"vin", "immo_id", "immo_number", "immo", "skc", "login"}


def protected(e) -> bool:
    """A VIN, immobiliser ID/number or SKC (a VDO cluster's EEPROM carries
    them as parameters). Their values are never sent to an AI, nor changed by
    one: the immobiliser is not tuning."""
    if getattr(e, "role", "") in PROTECTED_ROLES:
        return True
    from .redact import KEY
    return any(KEY.search(x or "") for x in (e.name, getattr(e, "label", ""), title(e)))


def detail(index: Index, e, cp=None, *, with_comparison: bool = True) -> dict:
    if protected(e):
        raise ToolError("%s holds identity / immobiliser data (VIN, immobiliser, SKC): its "
                        "value is not shared with AI services" % index.eid(e))
    if e.n_cells > MAX_CELLS:
        raise ToolError("%s has %d cells (limit %d per request)"
                        % (index.eid(e), e.n_cells, MAX_CELLS))
    d = summary(index, e)
    if e.description:
        d["description"] = e.description
    for k in ("note", "coding", "selector", "set_note"):
        v = getattr(e, k, "")
        if v:
            d[k] = v
    lo, hi = e.value_range
    d["allowed_range"] = [num(lo), num(hi)]
    d["placement"] = placement(e)
    vals = e.values()
    d["values"] = [[num(v) for v in row] for row in vals]
    xs, ys = e.x_values(), e.y_values()
    x = axis_json(e, e.x, xs)
    y = axis_json(e, e.y, ys)
    if x:
        d["x_axis"] = x
    if y:
        d["y_axis"] = y
    layout = "values[row][col]"
    if xs and len(xs) == e.cols:
        layout += "; x_axis has one entry per column"
    if ys and len(ys) == e.rows:
        layout += "; y_axis has one entry per row"
    d["layout"] = layout
    if with_comparison:
        if cp is None:
            d["comparison"] = None
        else:
            cv = cp.values()
            c = {"values": [[num(v) for v in row] for row in cv]}
            if (cp.rows, cp.cols) != (e.rows, e.cols):
                c["note"] = "comparison shape %dx%d differs" % (cp.rows, cp.cols)
            else:
                diffs = [[r, cc, num(cv[r][cc]), num(vals[r][cc])]
                         for r in range(e.rows) for cc in range(e.cols)
                         if not values_equal(vals[r][cc], cv[r][cc], 1e-9)]
                c["changed_cells"] = len(diffs)
                if diffs:
                    c["changes"] = {"columns": ["row", "col", "comparison", "current"],
                                    "rows": diffs[:200]}
                    if len(diffs) > 200:
                        c["changes"]["truncated"] = True
            cxs = cp.x_values()
            if cxs and [num(v) for v in cxs] != [num(v) for v in xs]:
                c["x_axis"] = [num(v) for v in cxs]
            cys = cp.y_values()
            if cys and [num(v) for v in cys] != [num(v) for v in ys]:
                c["y_axis"] = [num(v) for v in cys]
            d["comparison"] = c
    return d


def checksum_json(rep) -> Optional[dict]:
    if rep is None:
        return None
    ok = getattr(rep, "ok", None)
    if callable(ok):
        ok = ok()
    text = ""
    desc = getattr(rep, "describe", None)
    if callable(desc):
        try:
            text = desc()
        except Exception:
            text = ""
    return {"ok": bool(ok) if ok is not None else None, "report": text}
