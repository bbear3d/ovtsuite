# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The record of what was written for logging, and where it is kept.

A :class:`LayoutManifest` lists every cell the planner wrote -- group, slot,
the value written (the old one is always the empty id), its signal, label,
unit, fingerprint and kind -- and, per preset, the groups it reads, the groups
it replaced, and a content key of what it asked for.

**Validity is always checked against the binary.** A cell counts only while
the binary still holds exactly the value recorded, so an undo, a redo or a
hand edit switches it off and on with no bookkeeping; a preset counts only
while every recorded cell in its groups does, and while its content key still
matches the preset (editing your preset makes its layout stale).

Two stores, tried in this order by the logger:

1. the open binary's project set, ``BinarySet.extra["logging_blocks"]``
   (:func:`load_extra` / :func:`store_extra`) -- travels with the ``.ovt``;
2. the per-user settings, keyed by the identity the ECU reports
   (:func:`load_for_identity` / :func:`save_for_identity`) -- lets the presets
   use the optimised blocks with no binary open; only ever used once the
   probe has confirmed the ECU serves them.

No Qt: the settings object is passed in, as :mod:`..userpresets` does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .model import NEW, Allocation, CellWrite

FORMAT = 1
#: ``BinarySet.extra`` key
EXTRA_KEY = "logging_blocks"
#: settings key of the per-user store: ``{identity key: manifest}``
PREFS_KEY = "datalog/logging_blocks"
#: most identities the per-user store keeps (oldest dropped first)
PREFS_LIMIT = 64


def canon(value: Any) -> Any:
    """A value in one comparable, JSON-safe form: EDC15's
    ``((dataset, index), ...)`` and ``{dataset: index}`` both become a dict
    with string keys; numbers stay numbers."""
    if isinstance(value, Mapping):
        return {str(k): int(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (tuple, list)) and value and all(
            isinstance(p, (tuple, list)) and len(p) == 2 for p in value):
        return {str(k): int(v) for k, v in sorted(value, key=lambda kv: str(kv[0]))}
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@dataclass
class CellRecord:
    group: int
    slot: int
    value: Any
    signal: str
    label: str = ""
    unit: str = ""
    fid: int = 0
    a: Optional[int] = None
    kind: str = NEW
    presets: List[str] = field(default_factory=list)

    @property
    def where(self) -> Tuple[int, int]:
        return (self.group, self.slot)

    @property
    def fingerprint(self) -> Tuple[int, Optional[int]]:
        return (self.fid, self.a)

    def to_json(self) -> dict:
        d = {"group": self.group, "slot": self.slot, "value": canon(self.value),
             "signal": self.signal, "fid": self.fid, "kind": self.kind,
             "presets": list(self.presets)}
        if self.a is not None:
            d["a"] = self.a
        if self.label:
            d["label"] = self.label
        if self.unit:
            d["unit"] = self.unit
        return d

    @classmethod
    def from_json(cls, d) -> "CellRecord":
        return cls(int(d["group"]), int(d["slot"]), canon(d["value"]), str(d.get("signal", "")),
                   str(d.get("label", "")), str(d.get("unit", "")), int(d.get("fid", 0)),
                   None if d.get("a") is None else int(d["a"]), str(d.get("kind", NEW)),
                   [str(p) for p in d.get("presets", [])])

    @classmethod
    def from_write(cls, w: CellWrite) -> "CellRecord":
        s = w.source
        return cls(w.group, w.slot, canon(s.value), s.signal, s.label, s.unit,
                   int(s.fingerprint[0]), s.fingerprint[1], w.kind, [])


@dataclass
class PresetRecord:
    key: str
    title: str
    content: str
    groups: List[int]
    baseline: List[int]

    def to_json(self) -> dict:
        return {"key": self.key, "title": self.title, "content": self.content,
                "groups": list(self.groups), "baseline": list(self.baseline)}

    @classmethod
    def from_json(cls, d) -> "PresetRecord":
        return cls(str(d["key"]), str(d.get("title", "")), str(d.get("content", "")),
                   [int(g) for g in d.get("groups", [])],
                   [int(g) for g in d.get("baseline", [])])


ValueAt = Callable[[int, int], Any]


@dataclass
class LayoutManifest:
    platform: str
    identity: List[str] = field(default_factory=list)
    cells: List[CellRecord] = field(default_factory=list)
    presets: Dict[str, PresetRecord] = field(default_factory=dict)
    created: str = ""
    app: str = ""
    #: (formula, A) the platform sends for an empty slot -- the probe needs it
    #: when no binary is open to ask (KWP2000 25 00, KW1281 11 20)
    empty: List[int] = field(default_factory=list)

    # ---- lookup ----
    def cell(self, group: int, slot: int) -> Optional[CellRecord]:
        return next((c for c in self.cells if c.group == group and c.slot == slot), None)

    def cells_in(self, groups: Iterable[int]) -> List[CellRecord]:
        gs = set(int(g) for g in groups)
        return [c for c in self.cells if c.group in gs]

    def preset_cells(self, key: str) -> List[CellRecord]:
        p = self.presets.get(key)
        return self.cells_in(p.groups) if p is not None else []

    @property
    def groups(self) -> List[int]:
        return sorted({c.group for c in self.cells})

    # ---- validity ----
    def valid_cells(self, value_at: ValueAt) -> Set[Tuple[int, int]]:
        """(group, slot) of every recorded cell the binary still holds."""
        out = set()
        for c in self.cells:
            try:
                now = canon(value_at(c.group, c.slot))
            except Exception:
                continue
            if now == canon(c.value):
                out.add(c.where)
        return out

    def valid_presets(self, value_at: ValueAt,
                      contents: Optional[Mapping[str, str]] = None) -> Set[str]:
        """Presets whose recorded cells are all still in the binary (and,
        with ``contents``, whose content key still matches)."""
        ok = self.valid_cells(value_at)
        out = set()
        for key, p in self.presets.items():
            if contents is not None and key in contents and contents[key] != p.content:
                continue
            mine = self.cells_in(p.groups)
            if all(c.where in ok for c in mine):
                out.add(key)
        return out

    def stale(self, key: str, content: str) -> bool:
        p = self.presets.get(key)
        return p is not None and p.content != content

    def owned_only_by(self, keys: Iterable[str]) -> List[CellRecord]:
        """Recorded cells no preset outside ``keys`` reads -- what releasing
        (regenerating or removing) those presets may change."""
        ks = set(keys)
        return [c for c in self.cells if set(c.presets) <= ks]

    # ---- building ----
    @classmethod
    def from_allocation(cls, alloc: Allocation, platform: str, identity: Sequence[str],
                        contents: Mapping[str, str], app: str = "",
                        previous: Optional["LayoutManifest"] = None,
                        keep: Optional[Set[Tuple[int, int]]] = None,
                        empty: Sequence[int] = ()) -> "LayoutManifest":
        """The manifest after ``alloc`` was written.

        ``previous``'s presets that were not planned again are carried over
        with the cells they read (only those in ``keep`` -- the cells the
        binary still holds -- when given). Every cell lists the presets that
        read its group."""
        m = cls(platform, list(identity), [], {}, datetime.now().isoformat(timespec="seconds"), app,
                [int(x) for x in (empty or (previous.empty if previous is not None else ()))])
        planned = {p.key for p in alloc.plans}
        by_where: Dict[Tuple[int, int], CellRecord] = {}
        if previous is not None:
            for key, pr in previous.presets.items():
                if key in planned:
                    continue
                cells = previous.cells_in(pr.groups)
                if keep is not None and any(c.where not in keep for c in cells):
                    continue                        # broken since: dropped
                m.presets[key] = PresetRecord(pr.key, pr.title, pr.content,
                                              list(pr.groups), list(pr.baseline))
                for c in cells:
                    by_where.setdefault(c.where, CellRecord.from_json(c.to_json()))
        for w in alloc.writes:
            by_where[(w.group, w.slot)] = CellRecord.from_write(w)
        for p in alloc.plans:
            m.presets[p.key] = PresetRecord(p.key, p.title, contents.get(p.key, ""),
                                            list(p.groups), list(p.baseline))
        for c in by_where.values():
            c.presets = sorted(k for k, pr in m.presets.items() if c.group in pr.groups)
        m.cells = sorted((c for c in by_where.values() if c.presets),
                         key=lambda c: (c.group, c.slot))
        return m

    def without(self, keys: Iterable[str]) -> "LayoutManifest":
        """This manifest with ``keys`` removed, and the cells only they read."""
        ks = set(keys)
        m = LayoutManifest(self.platform, list(self.identity), [], {}, self.created, self.app,
                           list(self.empty))
        m.presets = {k: p for k, p in self.presets.items() if k not in ks}
        for c in self.cells:
            left = [k for k in c.presets if k not in ks]
            if left:
                cc = CellRecord.from_json(c.to_json())
                cc.presets = left
                m.cells.append(cc)
        return m

    def renamed(self, old: str, new: str, title: str = "") -> bool:
        """Re-key preset ``old`` as ``new`` (your preset was renamed: its key
        is its name). False when there was nothing to rename."""
        if old == new or old not in self.presets:
            return False
        pr = self.presets.pop(old)
        pr.key = new
        if title:
            pr.title = title
        self.presets[new] = pr
        for c in self.cells:
            c.presets = sorted({new if k == old else k for k in c.presets})
        return True

    # ---- JSON ----
    def to_json(self) -> dict:
        return {"format": FORMAT, "platform": self.platform, "identity": list(self.identity),
                "created": self.created, "app": self.app, "empty": list(self.empty),
                "cells": [c.to_json() for c in self.cells],
                "presets": [p.to_json() for p in self.presets.values()]}

    @classmethod
    def from_json(cls, d) -> Optional["LayoutManifest"]:
        """None for anything that is not a manifest this code can read."""
        try:
            if not isinstance(d, Mapping) or int(d.get("format", 0)) != FORMAT:
                return None
            m = cls(str(d["platform"]), [str(x) for x in d.get("identity", [])], [], {},
                    str(d.get("created", "")), str(d.get("app", "")),
                    [int(x) for x in d.get("empty", [])])
            for c in d.get("cells", []):
                try:
                    m.cells.append(CellRecord.from_json(c))
                except Exception:
                    continue
            for p in d.get("presets", []):
                try:
                    pr = PresetRecord.from_json(p)
                    m.presets[pr.key] = pr
                except Exception:
                    continue
            return m
        except Exception:
            return None

    def dumps(self) -> str:
        return json.dumps(self.to_json(), separators=(",", ":"))


# ---------------------------------------------------------------- stores
def load_extra(extra: Optional[Mapping]) -> Optional[LayoutManifest]:
    """The manifest kept in a project set's ``extra`` (None when there is none)."""
    if not extra:
        return None
    return LayoutManifest.from_json(extra.get(EXTRA_KEY))


def store_extra(extra: dict, manifest: Optional[LayoutManifest]) -> None:
    """Keep ``manifest`` in a project set's ``extra``; None (or one with no
    presets left) removes the field."""
    if manifest is None or not manifest.presets:
        extra.pop(EXTRA_KEY, None)
    else:
        extra[EXTRA_KEY] = manifest.to_json()


def _squash(text: str) -> str:
    return "".join((text or "").split()).upper()


def identity_key(platform: str, ident: str) -> str:
    """How an identity is filed: the platform and the reported ident with
    whitespace removed (KW1281 sends EDC15 idents in fixed-length blocks that
    may split a word -- ``msbcatalogue`` compares them the same way)."""
    return "%s:%s" % ((platform or "").lower(), _squash(ident))


def _read_store(prefs) -> Dict[str, Any]:
    try:
        raw = prefs.value(PREFS_KEY, "")
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_for_identity(prefs, platform: str, ident: str) -> Optional[LayoutManifest]:
    """The manifest last generated for a binary with this identity.

    ``ident`` is what the ECU reported (EDC16: the catalogue key "PART SW",
    see ``service.report_key``). An exact match wins; otherwise the longest
    filed ident the report starts with -- KW1281 sends the coded dataset's
    ident followed by more text, as ``msblock15.dataset_for_identity``
    matches it."""
    store = _read_store(prefs)
    want = identity_key(platform, ident)
    d = store.get(want)
    if d is None:
        prefix = "%s:" % (platform or "").lower()
        have = want[len(prefix):]
        best = None
        for k, v in store.items():
            if not k.startswith(prefix):
                continue
            filed = k[len(prefix):]
            if filed and have.startswith(filed) and (best is None or len(filed) > len(best[0])):
                best = (filed, v)
        d = best[1] if best else None
    return LayoutManifest.from_json(d) if d is not None else None


def save_for_identity(prefs, manifest: LayoutManifest) -> None:
    """File ``manifest`` under each of its identities (replacing what was
    there); a manifest with no presets removes them."""
    store = _read_store(prefs)
    keys = [identity_key(manifest.platform, i) for i in manifest.identity if _squash(i)]
    for k in keys:
        store.pop(k, None)
        if manifest.presets:
            store[k] = manifest.to_json()
    while len(store) > PREFS_LIMIT:
        store.pop(next(iter(store)))
    prefs.setValue(PREFS_KEY, json.dumps(store, separators=(",", ":")))
    try:
        prefs.sync()
    except Exception:
        pass


def rename_preset(platform: str, old: str, new: str, title: str = "",
                  extras: Iterable[dict] = (), prefs=None) -> int:
    """Your preset ``old`` is now ``new``: carry its optimised layout over in
    each project-set ``extra`` given and in the per-user store (every
    identity of ``platform``). Without this the renamed preset would lose its
    blocks, and the cells built for it could no longer be removed (the
    window lists presets by their current key). Returns how many records
    changed."""
    n = 0
    for extra in extras:
        if extra is None:
            continue
        m = load_extra(extra)
        if m is not None and m.platform == platform and m.renamed(old, new, title):
            store_extra(extra, m)
            n += 1
    if prefs is not None:
        store = _read_store(prefs)
        prefix = "%s:" % (platform or "").lower()
        changed = False
        for k, d in list(store.items()):
            if not k.startswith(prefix):
                continue
            m = LayoutManifest.from_json(d)
            if m is not None and m.renamed(old, new, title):
                store[k] = m.to_json()
                changed = True
                n += 1
        if changed:
            prefs.setValue(PREFS_KEY, json.dumps(store, separators=(",", ":")))
            try:
                prefs.sync()
            except Exception:
                pass
    return n


__all__ = ["FORMAT", "EXTRA_KEY", "PREFS_KEY", "canon", "CellRecord", "PresetRecord",
           "LayoutManifest", "load_extra", "store_extra", "identity_key",
           "load_for_identity", "save_for_identity", "rename_preset"]
