# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""From installed packs to "what applies to this module": the resolver.

Resolution is per entry id, not per document (schema doc §3):

1. the user's local packs beat module packs beat the bundled pack;
2. within an origin the most specific match wins:
   part+sw > part > part* > addr+platform > addr;
3. two entries at the same origin and specificity from different packs are a
   conflict, reported and never silently picked.

Names derived from a loaded binary are layered on top of this by the caller
(the session), not here: the resolver only knows packs.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import Doc, Identity, Pack, _norm_addr
from .schema import as_int
from .validate import entry_ids

ORIGIN_RANK = {"bundled": 0, "module": 1, "user": 2}


@dataclass
class Hit:
    doc: Doc
    pack: Pack
    specificity: int

    @property
    def rank(self) -> Tuple[int, int]:
        return (ORIGIN_RANK.get(self.pack.origin, 0), self.specificity)


@dataclass
class Resolved:
    """One entry as resolved: the entry itself and where it came from."""
    entry: Dict[str, Any]
    pack_id: str
    origin: str
    specificity: int
    path: str = ""

    @property
    def confidence(self) -> str:
        c = self.entry.get("confidence")
        if c:
            return c
        src = self.entry.get("source")
        if src in ("binary", "odx", "capture", "bench"):
            return "verified"
        if src in ("vendor-doc", "convention"):
            return "likely"
        return "unverified"


@dataclass
class View:
    """Everything of one kind that applies to an identity."""
    kind: str
    entries: Dict[str, Resolved] = field(default_factory=dict)      # by entry id
    top: Dict[str, Any] = field(default_factory=dict)               # merged non-entry fields
    conflicts: List[str] = field(default_factory=list)
    hits: List[Hit] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.entries and not self.top


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Resolver:
    def __init__(self, packs: Iterable[Pack]):
        self.packs: List[Pack] = list(packs)

    # ------------------------------------------------------------- matching
    def hits(self, kind: str, ident: Identity) -> List[Hit]:
        """Documents of ``kind`` that apply to ``ident``, least to most
        authoritative, so a later one overrides an earlier one."""
        out = []
        for p in self.packs:
            for d in p.docs:
                if d.kind != kind:
                    continue
                s = d.match.specificity(ident)
                if s is None:
                    continue
                out.append(Hit(d, p, s))
        out.sort(key=lambda h: h.rank)
        return out

    def coding_doc(self, ident: Identity) -> Dict[str, Any]:
        """The most authoritative ``coding`` document's full body for an
        identity (keeping the ``digits`` / ``values`` / ``bytes`` lists the
        :meth:`view` top drops), or ``{}``. A ``values`` list is merged across
        all applicable docs so named codings from several packs all show."""
        hits = self.hits("coding", ident)
        if not hits:
            return {}
        body = dict(hits[-1].doc.body)
        values = []
        seen = set()
        for h in hits:
            for v in h.doc.body.get("values") or []:
                vid = v.get("id") or v.get("value")
                if vid not in seen:
                    seen.add(vid)
                    values.append(v)
        if values:
            body["values"] = values
        body["_pack"] = hits[-1].pack.id
        return body

    def view(self, kind: str, ident: Identity) -> View:
        v = View(kind=kind)
        v.hits = self.hits(kind, ident)
        ranks: Dict[str, Tuple[Tuple[int, int], str]] = {}
        for h in v.hits:
            body = {k: val for k, val in h.doc.body.items()
                    if k not in ("kind", "match") and not isinstance(val, list)}
            v.top = _deep_merge(v.top, body)
            for e, eid in _entries_with_ids(h.doc):
                prev = ranks.get(eid)
                if prev is not None and prev[0] == h.rank and prev[1] != h.pack.id:
                    v.conflicts.append("%s: %s and %s both define it at the same level"
                                       % (eid, prev[1], h.pack.id))
                ranks[eid] = (h.rank, h.pack.id)
                v.entries[eid] = Resolved(e, h.pack.id, h.pack.origin, h.specificity, h.doc.path)
        return v

    # ------------------------------------------------------------- addressing
    def generations(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for h in self.hits("addressing", Identity()):
            for g, info in (h.doc.body.get("generations") or {}).items():
                out[g] = _deep_merge(out.get(g, {}), info if isinstance(info, dict) else {})
        return out

    def addressing(self, generation: Optional[str] = None) -> List[Dict[str, Any]]:
        """The module address table, merged by address across packs; a
        module listing ``generations`` is kept only for one of them when
        ``generation`` is given. Sorted by address.

        Addressing documents carry no match of their own, so within an
        origin they would merge in file order; instead the rows that name no
        generation (the convention table) go in first and the
        generation-specific rows (a car's own gateway list) on top of them,
        whichever file holds which -- so a captured name or source is never
        overwritten by the generic row's. Without a requested generation a
        generation-specific row still contributes its facts (the TP 2.0 dest
        of a module only one generation lists), but its ``generations`` list
        is not copied onto the merged generic row."""
        by_addr: Dict[str, Dict[str, Any]] = {}
        rows: List[Tuple[int, int, Dict[str, Any], str]] = []    # (has generations, hit order, row, pack)
        for order, h in enumerate(self.hits("addressing", Identity(generation=generation))):
            for m in h.doc.body.get("modules") or []:
                if not isinstance(m, dict):
                    continue
                gens = m.get("generations")
                specific = isinstance(gens, list) and bool(gens)
                if generation and specific and generation not in gens:
                    continue
                rows.append((1 if specific else 0, order, m, h.pack.id))
        rows.sort(key=lambda r: (r[0], r[1]))
        for specific, _order, m, pack_id in rows:
            addr = _norm_addr(m.get("addr"))
            if addr is None:
                continue
            base = by_addr.get(addr)
            row = m
            if specific and not generation and base is not None:
                row = {k: v for k, v in m.items() if k != "generations"}
                if isinstance(base.get("generations"), list):
                    # two generation tables listing the address: both apply
                    row["generations"] = base["generations"] + [g for g in m["generations"]
                                                                if g not in base["generations"]]
            merged = _deep_merge(base or {}, row)
            merged["addr"] = addr
            merged.setdefault("_pack", pack_id)
            by_addr[addr] = merged
        return [by_addr[a] for a in sorted(by_addr)]

    def module_address(self, addr: str, generation: Optional[str] = None) -> Optional[Dict[str, Any]]:
        addr = _norm_addr(addr)
        for m in self.addressing(generation):
            if m["addr"] == addr:
                return m
        return None

    # ----------------------------------------------------------------- kinds
    def module(self, ident: Identity) -> Dict[str, Any]:
        """``identification`` / ``variants`` / ``security`` merged for the
        module, most authoritative last."""
        return self.view("module", ident).top

    def fault(self, ident: Identity, vag: Optional[int] = None, sae: Optional[str] = None
              ) -> Optional[Resolved]:
        """The fault entry for a code, by VAG number or SAE word, else None.
        Entries are keyed by id, so two packs may label the same code under
        different ids: the most authoritative one (user over module over
        bundled, then the more specific match) is the answer, not the first
        one merged."""
        v = self.view("faults", ident)
        ranked = sorted(v.entries.values(), key=lambda r: (ORIGIN_RANK.get(r.origin, 0), r.specificity),
                        reverse=True)
        for r in ranked:
            e = r.entry
            if vag is not None and as_int(e.get("vag")) == vag:
                return r
            if sae and str(e.get("sae", "")).upper() == sae.upper():
                return r
        return None

    def fault_text(self, ident: Identity, vag: Optional[int] = None, sae: Optional[str] = None) -> str:
        """A fault's text: the pack's, else the bundled standard P-code
        description (``ovtsuite.dtc``), else empty."""
        r = self.fault(ident, vag, sae)
        if r is not None:
            return str(r.entry.get("text", ""))
        if sae:
            try:
                from ... import dtc as _dtc
                return _dtc._load_pdesc().get(sae.upper(), "")
            except Exception:
                return ""
        return ""

    def measuring_names(self, ident: Identity) -> Dict[int, List[Optional[str]]]:
        """``{group: [cell name or None, ...]}`` from the measuring documents
        that apply (hand-written packs; the per-platform catalogue built from
        binaries is layered by the session, which has the identity fields
        that catalogue keys on)."""
        out: Dict[int, List[Optional[str]]] = {}
        for h in self.hits("measuring", ident):
            for g, info in (h.doc.body.get("groups") or {}).items():
                gn = as_int(g)
                if gn is None or not isinstance(info, dict):
                    continue
                names = []
                for c in info.get("cells") or []:
                    if isinstance(c, dict):
                        names.append(c.get("name"))
                    else:
                        names.append(c)
                out[gn] = names
        return out

    def coverage(self, ident: Identity) -> Dict[str, int]:
        """How much the packs know about this module, per kind: entry counts
        (for the UI's "definitions: 12 adaptation channels, 0 codings")."""
        out = {}
        for kind in ("faults", "adaptation", "actuator_tests", "basic_settings", "dids", "routines"):
            out[kind] = len(self.view(kind, ident).entries)
        out["measuring"] = len(self.measuring_names(ident))
        cod = self.view("coding", ident)
        out["coding"] = len(cod.entries) + (1 if cod.top.get("digits") else 0)
        out["module"] = 1 if self.module(ident) else 0
        return out

    def procedures(self, ident: Optional[Identity] = None) -> List[Resolved]:
        """Every procedure (optionally those whose ``applies.addr`` is the
        identity's address)."""
        out = []
        for p in self.packs:
            for d in p.docs_of("procedure"):
                applies = d.body.get("applies") or {}
                if ident is not None and ident.addr is not None and applies.get("addr") is not None:
                    if _norm_addr(applies.get("addr")) != _norm_addr(ident.addr):
                        continue
                out.append(Resolved(d.body, p.id, p.origin, 0, d.path))
        return out


def _entries_with_ids(doc: Doc) -> List[Tuple[Dict[str, Any], str]]:
    """``(entry, id)`` for every id-bearing entry, including a coding
    document's values and bits (which are not one flat list)."""
    out = []
    if doc.kind == "coding":
        for v in doc.body.get("values") or []:
            if isinstance(v, dict) and v.get("id"):
                out.append((v, v["id"]))
        for b in doc.body.get("bytes") or []:
            if not isinstance(b, dict):
                continue
            for bit in b.get("bits") or []:
                if isinstance(bit, dict) and bit.get("id"):
                    e = dict(bit)
                    e["byte"] = b.get("index")
                    out.append((e, bit["id"]))
        return out
    if doc.kind == "procedure":
        if doc.body.get("id"):
            out.append((doc.body, doc.body["id"]))
        return out
    ids = set(entry_ids(doc))
    for e in doc.entries:
        if isinstance(e, dict) and e.get("id") in ids:
            out.append((e, e["id"]))
    return out
