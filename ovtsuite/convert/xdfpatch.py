# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TunerPro XDF patches (XDFPATCH) made from two binaries.

A TunerPro patch is byte-literal: at an address, the bytes to write
(``patchdata``) and the bytes they replace (``basedata``, so TunerPro can
tell whether it is applied and take it out again). Made here from the
original binary and a changed one of the same software: every changed run
of bytes becomes an entry, named after the definition table it lies in, so
the patch reads as the list of maps it changes.

Runs a few bytes apart inside one table are joined (the bytes between are
the same in both, so the entry still applies exactly); an entry never
crosses from one table into another. Entries are at most :data:`CHUNK`
bytes, the size TunerPro's own patches use.
"""

from __future__ import annotations

import bisect
from typing import Dict, List, Optional, Tuple

from ..definitions.model import Definition

#: unchanged bytes inside one table that still join two changed runs
JOIN = 8
#: the most bytes in one entry
CHUNK = 0x100
#: the category the generated patches are put in
CATEGORY = "Patches"


class PatchError(ValueError):
    pass


def _owners(defn: Definition) -> Tuple[List[int], List[Tuple[int, int, str]]]:
    """Every table's (and stored axis') bytes, sorted: ``(starts, [(start,
    end, name)])``, addresses as image offsets."""
    spans = []
    for t in defn.tables:
        name = t.symbol or t.name
        for st, n in ((t.z, max(1, t.rows * t.cols)),
                      (t.x.values if t.x else None, t.x.count if t.x else 0),
                      (t.y.values if t.y else None, t.y.count if t.y else 0)):
            if st is not None and st.addr is not None and n:
                spans.append((st.addr, st.addr + n * st.size,
                              name if st is t.z else "%s (axis)" % name))
    spans.sort()
    return [s[0] for s in spans], spans


def _owner(starts, spans, addr: int) -> Optional[str]:
    i = bisect.bisect_right(starts, addr) - 1
    # tables can overlap (an axis shared, a block inside a map): look back
    # over the few starting before it that could still cover the address
    for k in range(i, max(-1, i - 64), -1):
        a, b, name = spans[k]
        if a <= addr < b:
            return name
    return None


def changed_runs(old: bytes, new: bytes) -> List[Tuple[int, int]]:
    """``[(start, end)]`` of the bytes that differ."""
    runs = []
    i, n = 0, len(old)
    while i < n:
        if old[i] != new[i]:
            j = i
            while j < n and old[j] != new[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def build(defn: Definition, old: bytes, new: bytes, title: str,
          split: str = "one", description: str = "",
          warnings: Optional[List[str]] = None, outside: bool = True) -> List[dict]:
    """XDFPATCH blocks (:attr:`Definition.patches` form, image offsets) that
    turn ``old`` into ``new``. ``split``: ``"one"`` patch with every change,
    or ``"table"`` -- one patch per changed table (and one for the bytes
    outside any table), each switched on and off on its own in TunerPro.
    ``outside=False`` leaves out the bytes outside every table (checksums
    TunerPro's plugin recomputes, code)."""
    warnings = warnings if warnings is not None else []
    if len(old) != len(new):
        raise PatchError("The two binaries differ in size (0x%X and 0x%X bytes): a "
                         "patch needs two images of the same software."
                         % (len(old), len(new)))
    runs = changed_runs(old, new)
    if not runs:
        warnings.append("The two binaries are identical: no patch written.")
        return []
    changed = sum(b - a for a, b in runs)
    if changed > len(old) // 4:
        warnings.append("%d%% of the bytes differ: the two binaries may not be the same "
                        "software, and a patch of them would not apply to it."
                        % (100 * changed // len(old)))
    starts, spans = _owners(defn)
    # a run crossing a table's start or end is split there, so every piece
    # lies in one table (or in none)
    edges = sorted({x for a, b, _n in spans for x in (a, b)})
    pieces = []
    for a, b in runs:
        i = bisect.bisect_right(edges, a)
        while i < len(edges) and edges[i] < b:
            pieces.append((a, edges[i]))
            a = edges[i]
            i += 1
        pieces.append((a, b))
    owned = [(a, b, _owner(starts, spans, a)) for a, b in pieces]
    # join runs a few bytes apart in the same table
    joined: List[List] = []
    for a, b, who in owned:
        if joined and who is not None and joined[-1][2] == who and a - joined[-1][1] <= JOIN:
            joined[-1][1] = b
        else:
            joined.append([a, b, who])
    entries_by: Dict[str, List[dict]] = {}
    order: List[str] = []
    for a, b, who in joined:
        key = who or ""
        if key not in entries_by:
            entries_by[key] = []
            order.append(key)
        for c in range(a, b, CHUNK):
            d = min(b, c + CHUNK)
            entries_by[key].append({
                "name": who or "0x%X (no table)" % c, "address": c,
                "patch": bytes(new[c:d]), "base": bytes(old[c:d])})
    n_out = sum(len(entries_by[k]) for k in order if not k)
    if not outside and "" in entries_by:
        del entries_by[""]
        order.remove("")
        if not order:
            warnings.append("Every change lies outside the definition's tables: no patch "
                            "written.")
            return []
    if split == "table":
        patches = []
        for k in order:
            es = entries_by[k]
            for n, e in enumerate(es, 1):
                e["name"] = str(n)
            patches.append({"title": "%s: %s" % (title, k or "outside the tables"),
                            "description": description, "categories": [CATEGORY],
                            "entries": es})
    else:
        es = [e for k in sorted(order, key=lambda k: entries_by[k][0]["address"])
              for e in entries_by[k]]
        patches = [{"title": title, "description": description,
                    "categories": [CATEGORY], "entries": es}]
    if n_out:
        warnings.append("%d changed runs lie outside every table of the definition (code, "
                        "checksums or unmapped data); %s." % (
                            n_out, "they are in the patch, named by address" if outside
                            else "left out of the patch"))
    return patches


def rebased(patches: List[dict], base: int) -> List[dict]:
    """Copies with every address minus ``base`` (definition address ->
    image offset)."""
    return [dict(p, entries=[dict(e, address=e["address"] - base) for e in p["entries"]])
            for p in patches]


def apply(patches: List[dict], image: bytes) -> bytes:
    """``image`` with every patch written (what TunerPro does): a check."""
    out = bytearray(image)
    for p in patches:
        for e in p["entries"]:
            a = e["address"]
            out[a:a + len(e["patch"])] = e["patch"]
    return bytes(out)
