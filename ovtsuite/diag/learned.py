# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the car taught us, kept in the user's local pack.

A gateway's installation list names modules the bundled table does not
know (an address with its TP 2.0 dest); the user can have those recorded
as addressing rows in a pack of their own beside the app's settings
(``<config>/diag/definitions/my-local``), where the resolver layers them
over the bundled table on the next start -- and from where they can be
contributed. Qt-free.
"""
import os
import time
from typing import Any, Dict, Iterable, List, Tuple

from .defs import yamlio
from .defs.load import user_dir

LOCAL_PACK_ID = "my-local"
LEARNED_DOC = "addressing-learned"


def local_pack_dir() -> str:
    return os.path.join(user_dir(), LOCAL_PACK_ID)


def ensure_local_pack() -> str:
    """The user's own pack directory with a manifest, created on first use."""
    d = local_pack_dir()
    os.makedirs(d, exist_ok=True)
    man = os.path.join(d, "pack.yaml" if yamlio.HAVE_YAML else "pack.json")
    if not any(os.path.isfile(os.path.join(d, n)) for n in ("pack.yaml", "pack.json")):
        body = {"format": 1, "id": LOCAL_PACK_ID, "version": "0.1.0",
                "title": "My local definitions", "publisher": "", "license": "CC-BY-SA-4.0",
                "requires": {"ovtdiag": ">=0.1"}, "depends": [], "coverage": {}}
        _write(man, body)
    return d


def _doc_path(d: str) -> str:
    for ext in (".yaml", ".yml", ".json"):
        p = os.path.join(d, LEARNED_DOC + ext)
        if os.path.isfile(p):
            return p
    return os.path.join(d, LEARNED_DOC + (".yaml" if yamlio.HAVE_YAML else ".json"))


def _write(path: str, body: Any):
    if path.endswith((".yaml", ".yml")):
        text = yamlio.dump_yaml(body)
    else:
        import json
        text = json.dumps(body, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def record_addresses(entries: Iterable[Dict[str, Any]], generation: str = "",
                     note: str = "") -> Tuple[str, int]:
    """Add or update addressing rows -- ``{"addr", "name", "dest", "bus"?}``
    -- in the learned document; returns ``(path, rows added or changed)``.
    A row is keyed by address and generation; its TP 2.0 dest is replaced
    by the newer value."""
    d = ensure_local_pack()
    path = _doc_path(d)
    body: Dict[str, Any] = {"kind": "addressing", "modules": []}
    if os.path.isfile(path):
        try:
            loaded = yamlio.read(path)
            if isinstance(loaded, dict) and isinstance(loaded.get("modules"), list):
                body = loaded
        except Exception:
            pass
    body["kind"] = "addressing"
    rows: List[Dict[str, Any]] = body["modules"]
    changed = 0
    stamp = time.strftime("%Y-%m-%d")
    for e in entries:
        addr = str(e.get("addr", "")).upper()
        if not addr:
            continue
        gens = [generation] if generation else []
        row = next((r for r in rows if str(r.get("addr", "")).upper() == addr
                    and sorted(r.get("generations") or []) == sorted(gens)), None)
        if row is None:
            row = {"addr": addr, "name": str(e.get("name") or "Module %s" % addr)}
            if gens:
                row["generations"] = gens
            rows.append(row)
            changed += 1
        new_dest = e.get("dest")
        if new_dest is not None:
            tp = dict(row.get("tp20") or {})
            if tp.get("dest") != int(new_dest):
                if row.get("source"):           # an existing row, not the one just added
                    changed += 1
                tp["dest"] = int(new_dest)
                row["tp20"] = tp
        if e.get("bus"):
            row["bus"] = str(e["bus"])
        if e.get("name") and not row.get("name"):
            row["name"] = str(e["name"])
        row["source"] = "capture"
        row["note"] = note or "Recorded by OVT Diag from the gateway's installation list, %s" % stamp
    if changed or rows:
        _write(path, body)
    return path, changed


def record_kline_baud(addr: str, baud: int, generation: str = "", name: str = "") -> Tuple[str, bool]:
    """Remember the K-line baud rate ``addr`` answered at (``kline.baud`` on
    its learned row), so the next connection tries it first instead of
    walking the rates. Returns ``(path, changed)``."""
    addr = str(addr or "").upper()
    if not addr or not baud:
        return "", False
    d = ensure_local_pack()
    path = _doc_path(d)
    body: Dict[str, Any] = {"kind": "addressing", "modules": []}
    if os.path.isfile(path):
        try:
            loaded = yamlio.read(path)
            if isinstance(loaded, dict) and isinstance(loaded.get("modules"), list):
                body = loaded
        except Exception:
            pass
    body["kind"] = "addressing"
    rows: List[Dict[str, Any]] = body["modules"]
    gens = [generation] if generation else []
    row = next((r for r in rows if str(r.get("addr", "")).upper() == addr
                and sorted(r.get("generations") or []) == sorted(gens)), None)
    if row is None:
        row = {"addr": addr, "name": str(name or "Module %s" % addr)}
        if gens:
            row["generations"] = gens
        rows.append(row)
    kline = dict(row.get("kline") or {})
    if kline.get("baud") == int(baud):
        return path, False
    kline["baud"] = int(baud)
    row["kline"] = kline
    row.setdefault("source", "capture")
    row["note"] = "K-line baud %d recorded by OVT Diag, %s" % (int(baud), time.strftime("%Y-%m-%d"))
    _write(path, body)
    return path, True


__all__ = ["record_addresses", "record_kline_baud", "ensure_local_pack", "local_pack_dir", "LOCAL_PACK_ID"]
