# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT definition JSON (``.ovtdef.json``): the definition model, whole.

Plain JSON with a small fixed vocabulary -- never pickle -- so a shared file
cannot run anything, and two versions diff line by line. It holds every field
of :class:`~ovtsuite.definitions.model.Definition` that is data; what a
reader computes from an image (a table whose sizes the image stores, a base
found in the image) is written as it was resolved.
"""

from __future__ import annotations

import json
from typing import Optional

from ..definitions.model import AxisDef, Definition, DefinitionError, Storage, TableDef

MAGIC = "ovt-definition"
VERSION = 1


def is_json(data: bytes) -> bool:
    head = bytes(data[:512]).lstrip()
    return head.startswith(b"{") and MAGIC.encode() in head


def _st(st: Optional[Storage]):
    if st is None:
        return None
    d = {"addr": st.addr, "bits": st.bits, "signed": st.signed,
         "float": st.float, "endian": st.endian, "factor": st.factor,
         "offset": st.offset, "equation": st.equation, "units": st.units}
    return {k: v for k, v in d.items() if v not in ("", None) or k == "addr"}


def _ax(ad: Optional[AxisDef]):
    if ad is None:
        return None
    d = {"count": ad.count, "values": _st(ad.values), "fixed": ad.fixed,
         "labels": ad.labels, "name": ad.name}
    return {k: v for k, v in d.items() if v not in (None, "")}


def dumps(defn: Definition, base: Optional[int] = None,
          image_size: Optional[int] = None) -> str:
    tables = []
    for t in defn.tables:
        d = {"name": t.name, "kind": t.kind, "z": _st(t.z), "rows": t.rows,
             "cols": t.cols, "description": t.description,
             "categories": t.categories, "order": t.order, "x": _ax(t.x),
             "y": _ax(t.y), "decimals": t.decimals, "note": t.note,
             "text_kind": t.text_kind, "record": t.record, "symbol": t.symbol,
             "originals": t.originals}
        tables.append({k: v for k, v in d.items() if v not in (None, "", [], {})})
    doc = {"format": MAGIC, "version": VERSION, "type": defn.type,
           "title": defn.title,
           "base": defn.base if base is None else base,
           "image_size": image_size,
           "segments": [list(s) for s in defn.segments],
           "info": defn.info, "checksums": [
               {k: v for k, v in c.items()} for c in defn.checksums],
           "tables": tables}
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _rst(d) -> Optional[Storage]:
    if d is None:
        return None
    st = Storage()
    for k, v in d.items():
        if hasattr(st, k):
            setattr(st, k, v)
    return st


def _rax(d) -> Optional[AxisDef]:
    if d is None:
        return None
    return AxisDef(int(d.get("count", 1)), values=_rst(d.get("values")),
                   fixed=d.get("fixed"), labels=d.get("labels"),
                   name=d.get("name", ""))


def loads(data: bytes) -> Definition:
    try:
        doc = json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DefinitionError("not an OVT definition JSON: %s" % exc)
    if doc.get("format") != MAGIC:
        raise DefinitionError("not an OVT definition JSON")
    if int(doc.get("version", 0)) > VERSION:
        from .. import branding
        raise DefinitionError("written by a newer %s (format %s)"
                              % (branding.APP_NAME, doc.get("version")))
    defn = Definition(type="json", title=doc.get("title", ""),
                      base=int(doc.get("base") or 0),
                      segments=[tuple(s) for s in doc.get("segments", [])],
                      info=dict(doc.get("info", {})),
                      checksums=list(doc.get("checksums", [])))
    defn.info.setdefault("source_type", doc.get("type", ""))
    if doc.get("image_size"):
        defn.info["image_size"] = str(doc["image_size"])
    for d in doc.get("tables", []):
        defn.tables.append(TableDef(
            name=d["name"], kind=d.get("kind", "constant"),
            z=_rst(d.get("z")) or Storage(), rows=int(d.get("rows", 1)),
            cols=int(d.get("cols", 1)), description=d.get("description", ""),
            categories=list(d.get("categories", [])),
            order=d.get("order", "row"), x=_rax(d.get("x")), y=_rax(d.get("y")),
            decimals=d.get("decimals"), note=d.get("note", ""),
            text_kind=d.get("text_kind", ""), record=d.get("record"),
            symbol=d.get("symbol", ""), originals=dict(d.get("originals", {}))))
    return defn
