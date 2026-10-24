# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Transforms between reading and writing: filter, merge, diff.

All of them work on definitions already placed in image offsets (see
:func:`ovtsuite.convert.prepare`), so tables from different files compare by
where they are.
"""

from __future__ import annotations

import copy
import re
from typing import Dict, List, Optional, Tuple

from ..definitions.model import Definition, TableDef


def _span(t: TableDef) -> Tuple[int, int]:
    a = t.z.addr or 0
    return a, a + max(1, t.rows * t.cols) * max(1, t.z.bits // 8)


def filter_tables(tables: List[TableDef], f: Dict) -> List[TableDef]:
    """Tables matching every filter given: ``categories`` (any of),
    ``pattern`` (a regular expression on the name, symbol or description;
    with ``wildcard``, ``*`` / ``?`` wildcards instead, text without them
    matching anywhere),
    ``start`` / ``end`` (image offsets the table lies within), ``kinds``,
    ``names`` (exact names, e.g. the tables that differ between two
    binaries)."""
    cats = set(f.get("categories") or [])
    pat = None
    if f.get("pattern"):
        p = f["pattern"]
        if f.get("wildcard"):
            rx = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
                         for ch in p)
            p = "^%s$" % rx if ("*" in p or "?" in p) else re.escape(p)
        pat = re.compile(p, re.I)
    kinds = set(f.get("kinds") or [])
    names = set(f.get("names") or [])
    start, end = f.get("start"), f.get("end")
    out = []
    for t in tables:
        if cats and not cats.intersection(t.categories):
            continue
        if kinds and t.kind not in kinds:
            continue
        if names and t.name not in names and t.symbol not in names:
            continue
        if pat and not (pat.search(t.name) or pat.search(t.symbol or "")
                        or pat.search(t.description or "")):
            continue
        a, b = _span(t)
        if start is not None and a < start:
            continue
        if end is not None and b > end:
            continue
        out.append(t)
    return out


def _key(t: TableDef) -> tuple:
    return (t.z.addr, t.z.bits, t.kind)


def merge(defs: List[Definition], warnings: Optional[List[str]] = None) -> Definition:
    """One definition from several (in image offsets). A table at the same
    address with the same width and kind is one table: the first source's,
    with names and descriptions the later ones add kept in its description.
    Two different tables overlapping is reported."""
    warnings = warnings if warnings is not None else []
    out = copy.copy(defs[0])
    out.tables = []
    out.checksums = [c for d in defs for c in d.checksums]
    out.patches = [p for d in defs for p in getattr(d, "patches", [])]
    seen: Dict[tuple, TableDef] = {}
    for d in defs:
        for t in d.tables:
            k = _key(t)
            if k in seen:
                first = seen[k]
                if t.name != first.name and t.name not in first.description:
                    first.description = (first.description + " | also: " + t.name
                                         ).strip(" |")
                if not first.x and t.x:
                    first.x = t.x
                if not first.y and t.y:
                    first.y = t.y
                continue
            from . import copy_table
            t = copy_table(t)
            seen[k] = t
            out.tables.append(t)
    spans = sorted((_span(t) + (t.name,) for t in out.tables if t.z.addr is not None))
    for (a1, b1, n1), (a2, b2, n2) in zip(spans, spans[1:]):
        if a2 < b1:
            warnings.append("%s and %s overlap at 0x%X" % (n1, n2, a2))
    return out


#: the kinds of difference :func:`diff` reports, in the order it lists them
DIFF_KINDS = ("added", "removed", "renamed", "moved", "axis moved", "resized",
              "rescaled", "retyped")


def diff(a: Definition, b: Definition) -> List[dict]:
    """What changed from ``a`` to ``b`` (both in image offsets), matched by
    name. Rows ``{"name", "change", "old", "new"}``, ``change`` one of
    :data:`DIFF_KINDS`: a table in one only is added or removed -- unless
    the other has one at the same place, size and width under another name
    (renamed); a table in both may have moved (its values), had an axis
    moved, been resized, rescaled (conversion or units) or retyped (width,
    sign, float)."""
    rows = []
    ia, ib = _keyed(a.tables, b.tables)
    gone = sorted(ia.keys() - ib.keys())
    new = sorted(ib.keys() - ia.keys())

    def where(t):
        return (t.z.addr, t.rows, t.cols, t.z.bits)
    at_b = {}
    for n in new:
        if ib[n].z.addr is not None:
            at_b.setdefault(where(ib[n]), n)
    renamed = {}
    for n in gone:
        m = at_b.get(where(ia[n])) if ia[n].z.addr is not None else None
        if m is not None and m not in renamed.values():
            renamed[n] = m
    for n in gone:
        if n in renamed:
            rows.append({"name": n, "change": "renamed", "old": n, "new": renamed[n]})
        else:
            rows.append({"name": n, "change": "removed", "old": _desc(ia[n]), "new": ""})
    taken = set(renamed.values())
    for n in new:
        if n not in taken:
            rows.append({"name": n, "change": "added", "old": "", "new": _desc(ib[n])})
    for n in sorted(ia.keys() & ib.keys()):
        x, y = ia[n], ib[n]
        if x.z.addr != y.z.addr:
            rows.append({"name": n, "change": "moved",
                         "old": _hex(x.z.addr), "new": _hex(y.z.addr)})
        for tag in ("x", "y"):
            ax, ay = getattr(x, tag), getattr(y, tag)
            pa = ax.values.addr if ax is not None and ax.values is not None else None
            pb = ay.values.addr if ay is not None and ay.values is not None else None
            if pa != pb and x.z.addr == y.z.addr:
                rows.append({"name": n, "change": "axis moved",
                             "old": "%s %s" % (tag.upper(), _hex(pa)),
                             "new": "%s %s" % (tag.upper(), _hex(pb))})
        if (x.rows, x.cols) != (y.rows, y.cols):
            rows.append({"name": n, "change": "resized", "old": "%dx%d" % (x.rows, x.cols),
                         "new": "%dx%d" % (y.rows, y.cols)})
        if (x.z.bits, bool(x.z.signed), bool(x.z.float)) != (y.z.bits, bool(y.z.signed),
                                                             bool(y.z.float)):
            rows.append({"name": n, "change": "retyped", "old": _type(x.z), "new": _type(y.z)})
        if x.z.equation_text() != y.z.equation_text() or (x.z.units or "") != (y.z.units or ""):
            rows.append({"name": n, "change": "rescaled",
                         "old": ("%s %s" % (x.z.equation_text(), x.z.units or "")).strip(),
                         "new": ("%s %s" % (y.z.equation_text(), y.z.units or "")).strip()})
    order = {k: i for i, k in enumerate(DIFF_KINDS)}
    rows.sort(key=lambda r: (order.get(r["change"], 99), r["name"]))
    return rows


def _keyed(ta, tb):
    """``({key: table}, {key: table})`` for two definitions' tables, one key
    per table even where a name repeats (XDFs often reuse a title): a name
    that repeats is paired first with the table at the same place in the
    other definition, then in order, and keyed ``name [n]``. A key-by-name
    dict would keep one of them and leave the rest out of the comparison."""
    def groups(ts):
        g = {}
        for t in ts:
            g.setdefault(t.symbol or t.name, []).append(t)
        return g
    ga, gb = groups(ta), groups(tb)
    ia, ib = {}, {}
    for name in set(ga) | set(gb):
        la, lb = list(ga.get(name, [])), list(gb.get(name, []))
        if len(la) <= 1 and len(lb) <= 1:
            if la:
                ia[name] = la[0]
            if lb:
                ib[name] = lb[0]
            continue
        pairs = []
        for x in list(la):                   # the same place first
            y = next((y for y in lb if y.z.addr is not None and y.z.addr == x.z.addr), None)
            if y is not None:
                pairs.append((x, y))
                la.remove(x)
                lb.remove(y)
        while la and lb:                     # then in order
            pairs.append((la.pop(0), lb.pop(0)))
        pairs += [(x, None) for x in la] + [(None, y) for y in lb]
        pairs.sort(key=lambda p: min(t.z.addr if t is not None and t.z.addr is not None
                                     else 1 << 62 for t in p))
        for i, (x, y) in enumerate(pairs, 1):
            k = "%s [%d]" % (name, i)
            if x is not None:
                ia[k] = x
            if y is not None:
                ib[k] = y
    return ia, ib


def _hex(v) -> str:
    return "" if v is None else "0x%X" % v


def _type(st) -> str:
    return "%s%d-bit" % ("float " if st.float else ("signed " if st.signed else ""), st.bits)


def _desc(t: TableDef) -> str:
    return "%s 0x%X %dx%d" % (t.kind, t.z.addr or 0, t.rows, t.cols)
