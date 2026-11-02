# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Compiling a pack: YAML documents -> one ``pack.json.gz`` (what ships in
an ``.ovtmod`` and what the frozen app reads without PyYAML), and the
``measuring`` documents -> rows of the logger's format-2 catalogue.

The compiled form holds the manifest and every document body verbatim
(plus its source path, for messages), so compiling loses nothing and a
compiled pack validates exactly like its source.
"""
import gzip
import json
import os
from typing import Any, Dict, List, Optional

from .load import COMPILED_NAME, load_pack, load_sources, source_hash
from .model import Pack
from .schema import as_int
from .validate import Report, validate_pack


def compiled_document(pack: Pack) -> Dict[str, Any]:
    """The compiled form: manifest, documents, and the hash of the sources
    they came from (``load.source_hash``), by which a loader tells a
    compiled pack beside edited sources from a current one."""
    out = {"pack": pack.manifest(),
           "docs": [{"path": d.path, "body": d.body} for d in pack.docs]}
    sh = pack.source_hash or (source_hash(pack.path) if pack.path and os.path.isdir(pack.path) else "")
    if sh:
        out["source_hash"] = sh
    return out


def compile_pack(src: str, out: Optional[str] = None, check: bool = True) -> str:
    """Compile the pack at ``src`` (its source documents, never a compiled
    form already beside them) to ``out`` (default: ``<src>/pack.json.gz``).
    Refuses an invalid pack when ``check`` is set. Returns the output path."""
    pack = load_sources(src, "user") if os.path.isdir(src) else load_pack(src, "user")
    if check:
        rep = validate_pack(pack)
        if not rep.ok:
            raise ValueError("pack %s does not validate:\n%s" % (pack.id, rep.text()))
    out = out or os.path.join(src, COMPILED_NAME)
    doc = compiled_document(pack)
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if out.endswith(".gz"):
        # mtime 0 and no embedded name: the same sources give the same bytes,
        # so a module package's content fingerprint only changes when the
        # definitions do (the build_modules pre-pass recompiles every time)
        with open(out, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
                fh.write(data.encode("utf-8"))
    else:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(data)
    return out


def catalogue_rows(pack: Pack) -> List[Dict[str, Any]]:
    """The ``measuring`` documents as rows the logger's catalogue builder
    (``datalog.msbcatalogue.Builder``) takes: one per document, keyed by its
    match (part / sw), ``{group: [name or None, ...]}``. Names only -- the
    protocol stream carries the formula."""
    rows = []
    for d in pack.docs_of("measuring"):
        groups: Dict[int, List[Optional[str]]] = {}
        for g, info in (d.body.get("groups") or {}).items():
            gn = as_int(g)
            if gn is None or not isinstance(info, dict):
                continue
            cells = []
            for c in info.get("cells") or []:
                cells.append(c.get("name") if isinstance(c, dict) else c)
            groups[gn] = cells
        rows.append({"match": d.match.to_doc(), "platform": d.match.platform,
                     "groups": groups, "source": d.path, "pack": pack.id})
    return rows


def check_and_report(src: str) -> Report:
    pack = load_pack(src, "user")
    return validate_pack(pack)
