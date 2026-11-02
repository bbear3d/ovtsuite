# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pack-level validation: what ``ovtsuite diag-defs check`` and the
definitions repository's CI run, and what the Module Manager shows.

Document-level field checks are :mod:`.schema`; this adds the rules that
span documents and packs: entry ids unique within a pack, match blocks
well-formed, references resolvable (a procedure's ``requires``, a
``requires: security`` against the module's security block), units from the
controlled list, and conflicts between installed packs.
"""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set, Tuple

from . import schema
from .model import Doc, Pack, is_entry_id

#: The unit spellings a definition may use. The suite's logger has its own
#: unit table for formula ids; these are for definitions that name a unit
#: themselves (adaptation channels, DIDs).
UNITS = {"", "km", "mi", "m", "km/h", "mph", "rpm", "°C", "°F", "K", "mbar", "bar", "kPa", "hPa",
         "psi", "%", "V", "mV", "A", "mA", "ms", "s", "min", "h", "days", "l", "l/h", "l/100km",
         "mg/str", "mg/stroke", "g/s", "kg/h", "°", "°KW", "°CRK", "Nm", "kW", "hp", "PS", "Hz",
         "ohm", "kohm", "count", "bit", "text"}


@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, msg: str, where: str = ""):
        prefix = ("%s: " % where) if where else ""
        if msg.startswith("?"):
            self.warnings.append(prefix + msg[1:])
        else:
            self.errors.append(prefix + msg)

    def text(self) -> str:
        lines = ["error: " + e for e in self.errors] + ["warning: " + w for w in self.warnings]
        return "\n".join(lines) if lines else "ok"


def entry_ids(doc: Doc) -> List[str]:
    """Every entry id a document defines (its kind's entry list, a coding
    document's values and bits, a procedure's own id)."""
    out = []
    if doc.kind == "procedure":
        pid = doc.body.get("id")
        if is_entry_id(pid):
            out.append(pid)
        return out
    if doc.kind == "coding":
        for v in doc.body.get("values") or []:
            if isinstance(v, dict) and is_entry_id(v.get("id")):
                out.append(v["id"])
        for b in doc.body.get("bytes") or []:
            for bit in (b.get("bits") or []) if isinstance(b, dict) else []:
                if isinstance(bit, dict) and is_entry_id(bit.get("id")):
                    out.append(bit["id"])
        return out
    for e in doc.entries:
        if isinstance(e, dict) and is_entry_id(e.get("id")):
            out.append(e["id"])
    return out


def validate_pack(pack: Pack) -> Report:
    rep = Report()
    if not pack.id or not all(c.isalnum() or c in "-_" for c in pack.id):
        rep.add("pack id %r: letters, digits, '-' and '_' only" % pack.id)
    if not pack.license and pack.origin != "user":
        rep.add("?pack.yaml: no license")
    seen: Dict[Tuple[str, str], str] = {}        # (id, match signature) -> path
    for doc in pack.docs:
        where = doc.path or doc.kind
        for p in doc.match.problems():
            rep.add(p, where)
        for m in schema.check_doc(doc.body, where):
            rep.add(m)
        sig = repr(sorted(doc.match.to_doc().items()))
        for eid in entry_ids(doc):
            key = (eid, sig)
            if key in seen:
                rep.add("entry id %r defined twice for the same match (also in %s)"
                        % (eid, seen[key]), where)
            seen[key] = where
        _check_units(doc, rep, where)
        _check_requires(doc, pack, rep, where)
    _check_procedures(pack, rep)
    return rep


def _checked_items(doc: Doc) -> List[Tuple[str, dict]]:
    """``(label, mapping)`` for every entry-like mapping a document holds
    that may name a unit or a requirement: the kind's entry list, plus the
    nested lists the flat list misses -- a ``dids`` document's routines, a
    coding document's values and bits, a measuring document's cells."""
    out: List[Tuple[str, dict]] = []

    def lab(e, fallback):
        return str(e.get("id") or e.get("name") or fallback)

    for e in doc.entries:
        if isinstance(e, dict):
            out.append((lab(e, "entry"), e))
    body = doc.body
    if doc.kind == "dids":
        for i, r in enumerate(body.get("routines") or []):
            if isinstance(r, dict):
                out.append((lab(r, "routines[%d]" % i), r))
    elif doc.kind == "coding":
        for i, v in enumerate(body.get("values") or []):
            if isinstance(v, dict):
                out.append((lab(v, "values[%d]" % i), v))
        for b in body.get("bytes") or []:
            if not isinstance(b, dict):
                continue
            for j, bit in enumerate(b.get("bits") or []):
                if isinstance(bit, dict):
                    out.append((lab(bit, "byte %s bit[%d]" % (b.get("index"), j)), bit))
    elif doc.kind == "measuring":
        for g, info in (body.get("groups") or {}).items():
            if not isinstance(info, dict):
                continue
            for i, c in enumerate(info.get("cells") or []):
                if isinstance(c, dict):
                    out.append(("group %s cell %d" % (g, i + 1), c))
    return out


def _check_units(doc: Doc, rep: Report, where: str):
    for label, e in _checked_items(doc):
        u = e.get("unit")
        if u is None:
            continue
        if u not in UNITS:
            rep.add("?%s: unit %r is not in the controlled list" % (label, u), where)
        sc = e.get("scale")
        if isinstance(sc, dict) and sc.get("unit") not in (None, u) and sc.get("unit") not in UNITS:
            rep.add("?%s: scale.unit %r is not in the controlled list" % (label, sc.get("unit")), where)


def _check_requires(doc: Doc, pack: Pack, rep: Report, where: str):
    """``requires: login|security|none`` on an entry (or on a coding
    document itself) must be one of the three levels; an empty string is
    the same as leaving it out."""
    items = _checked_items(doc)
    if doc.kind == "coding":
        items.append((doc.kind, doc.body))
    for label, e in items:
        r = e.get("requires")
        if r is None or r == "":
            continue
        if r not in ("login", "security", "none"):
            rep.add("%s: requires %r is not login, security or none" % (label, r), where)


def _check_procedures(pack: Pack, rep: Report):
    ids: Set[str] = set()
    for d in pack.docs:
        ids.update(entry_ids(d))
    for d in pack.docs_of("procedure"):
        applies = d.body.get("applies") or {}
        for ref in applies.get("requires") or []:
            if ref not in ids:
                rep.add("?requires %r is not defined in this pack (may come from another)" % ref,
                        d.path or d.body.get("id", "procedure"))
        for i, st in enumerate(d.body.get("steps") or []):
            if not isinstance(st, dict) or len(st) != 1:
                continue
            k, v = next(iter(st.items()))
            if k == "read" and isinstance(v, dict):
                for ref in v.get("channels") or []:
                    if ref not in ids:
                        rep.add("?steps[%d]: read refers to %r, not defined in this pack" % (i, ref),
                                d.path or d.body.get("id", "procedure"))


def check_conflicts(packs: Iterable[Pack]) -> List[str]:
    """Two packs of the same origin defining the same entry id for the same
    match: the resolver will not pick one silently, so say so."""
    seen: Dict[Tuple[str, str, str], str] = {}
    out = []
    for p in packs:
        for d in p.docs:
            sig = repr(sorted(d.match.to_doc().items()))
            for eid in entry_ids(d):
                key = (p.origin, eid, sig)
                if key in seen and seen[key] != p.id:
                    out.append("%s and %s both define %s for %s" % (seen[key], p.id, eid, sig))
                seen.setdefault(key, p.id)
    return out
