# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A WinOLS project or map pack converted to a map pack: passed through.

The records of the source go into the map pack as they are -- every map
with its own name, description, units, scaling, axes and folder, and the
source's folder list (names, ids, order) -- not rebuilt from the converted
definition, which groups an EDC15 project's maps by code block and keeps
only what OVT-Suite's model holds. Translation, when chosen, is the one
change: it rewrites the texts (descriptions, value and axis labels, units,
folder names), never a map's name, which is its identifier.

:func:`applies` says whether a job is written this way; :func:`write` does
it."""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

#: the separator the definition joins a record's description and its value
#: label with (``winols.to_definition``)
_Z = " | Z: "


def applies(defn, job) -> bool:
    """True when ``job`` writes the records of one WinOLS file (OLS / KP)
    as they are: a single source of that kind, not ported onto another
    binary."""
    return (len(job.sources) == 1
            and defn.info.get("source_type") in ("ols", "kp")
            and not job.options.get("port"))


def _source(job):
    from ..definitions import winols as W
    with open(job.sources[0], "rb") as fh:
        return W.parse(fh.read())


def _kept(defn, wf, filtered: bool) -> List[int]:
    """Indexes of the records written: every one, or with a filter only
    those whose table it kept."""
    if not filtered:
        return list(range(len(wf.records)))
    keep = {getattr(t, "ols_record", None) for t in defn.tables}
    return [i for i in range(len(wf.records)) if i in keep]


# ------------------------------------------------------------- translation
def _texts(wf, idx: List[int]):
    """The record texts as a definition :func:`translate.plan` reads (one
    table per record; names are identifiers: never offered), with the
    folder names as categories."""
    tables = []
    for i in idx:
        r = wf.records[i]
        axes = []
        for a in (r.x, r.y):
            axes.append(SimpleNamespace(name=a.label, labels=None,
                                        values=SimpleNamespace(units=a.units)))
        tables.append(SimpleNamespace(
            name=str(i), symbol=str(i), description=r.description,
            z=SimpleNamespace(units=r.units), x=axes[0], y=axes[1],
            categories=[]))
    if tables:
        tables[0].categories = [n for _f, res, n in wf.folder_list
                                if res not in (9000, 9001, 9002)]
    return SimpleNamespace(tables=tables)


def _reviewed(rows) -> Dict[Tuple[str, str], str]:
    """``{(kind, source text): result}`` from translation rows planned on
    the converted definition (the review grid's): a description that holds
    the value label (``desc | Z: label``) gives both parts too."""
    out: Dict[Tuple[str, str], str] = {}
    for r in rows or []:
        src, res = r.get("source") or "", r.get("result")
        if res is None or res == src:
            continue
        kind = r.get("kind") or ("units" if r.get("field", "").endswith("units")
                                 else "category" if r.get("field") == "category"
                                 else "text")
        out.setdefault((kind, src), res)
        if kind == "text" and _Z in src and _Z in res:
            (s1, s2), (t1, t2) = src.split(_Z, 1), res.split(_Z, 1)
            out.setdefault(("text", s1), t1)
            out.setdefault(("text", s2), t2)
        elif kind == "text" and src.startswith("Z: ") and res.startswith("Z: "):
            out.setdefault(("text", src[3:]), res[3:])
    return out


def _translations(wf, idx, opts: Dict, warnings: List[str]):
    """``{(kind, text): translation}`` for the records' texts: the rows
    reviewed in the window when there are some, else a fresh plan."""
    from . import translate
    if opts.get("rows") is not None:
        return _reviewed(opts["rows"])
    rows = translate.plan(_texts(wf, idx), opts, warnings)
    return {(r["kind"], r["source"]): r["result"] for r in rows
            if r["result"] != r["source"]}


def _translated(wf, idx, opts: Dict, warnings: List[str]):
    """Copies of the records (and the folder list) with their texts
    translated as ``opts`` says (mode ``replace``, ``bilingual`` or
    ``keep``, as :func:`translate.apply`); and how many texts changed."""
    tr = _translations(wf, idx, opts, warnings)
    mode = opts.get("mode", "replace")
    n = [0]

    def put(text, kind="text", desc=False):
        if not text or not text.strip():
            return text
        new = tr.get((kind, text))
        if new is None:
            new = tr.get((kind, text.strip()))
        if new is None or new == text:
            return text
        n[0] += 1
        if kind in ("units", "category"):
            return new
        if mode == "bilingual":
            return "%s / %s" % (text, new)
        if mode == "keep" and desc:
            return new + " | Original: " + text
        return new

    recs = []
    for i in idx:
        r = copy.copy(wf.records[i])
        r.description = put(r.description, desc=True)
        r.label = put(r.label)
        r.units = put(r.units, "units")
        for k in ("x", "y"):
            a = copy.copy(getattr(r, k))
            a.label = put(a.label)
            a.units = put(a.units, "units")
            setattr(r, k, a)
        recs.append(r)
    folders = [(f, res, n0 if res in (9000, 9001, 9002) else put(n0, "category"))
               for f, res, n0 in wf.folder_list]
    return recs, folders, n[0]


# ------------------------------------------------------------------ writing
def write(defn, job, size: int, warnings: List[str],
          filtered: bool = False) -> Tuple[bytes, int]:
    """The map pack of ``job``'s source with its records passed through:
    ``(bytes, records written)``. ``warnings`` gets what format 292 cannot
    hold."""
    from ..definitions.winols_write import write_kp_records
    wf = _source(job)
    idx = _kept(defn, wf, filtered)
    if job.translate:
        recs, folders, _n = _translated(wf, idx, job.translate, warnings)
    else:
        recs, folders = [wf.records[i] for i in idx], list(wf.folder_list)
    if wf.parents:
        warnings.append("%d folders are inside other folders; a map pack of "
                        "format 292 lists every folder at the top"
                        % len(wf.parents))
    image_size = size or len(wf.image)
    title = defn.title or os.path.splitext(os.path.basename(job.sources[0]))[0]
    data = write_kp_records(recs, folders, image_size, title, warnings)
    return data, sum(1 for r in recs if r.type != 1)
