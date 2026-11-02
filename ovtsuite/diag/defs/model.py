# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The definitions format v1 as objects: a pack, its documents, a match.

The format is claude/ovt_diag_definitions_schema.md. Documents stay the
dicts they were read as (validated by :mod:`.schema`); what is modelled here
is what the resolver needs -- the pack manifest, the ``match`` block of every
document, and how specific a match is for a module's served identity.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

FORMAT = 1

#: Every document kind the format defines, with the key its entries live under.
KINDS = {
    "addressing": "modules",
    "module": None,                 # one module's identity/variants/security: no entry list
    "faults": "codes",
    "measuring": "groups",
    "coding": None,                 # digits / values / bytes -- three lists, see schema
    "adaptation": "channels",
    "actuator_tests": "tests",
    "basic_settings": "groups",
    "dids": "dids",
    "routines": "routines",
    "procedure": None,
    "translations": "entries",
}

PROTOCOLS = ("kw1281", "kwp2000", "uds")

_ADDR = re.compile(r"^[0-9A-Fa-f]{2}$")
_PART = re.compile(r"^[0-9A-Za-z]{3,12}\*?$")
_SW = re.compile(r"^[0-9A-Za-z]{1,8}$")
_ENTRY_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")


def is_entry_id(s: Any) -> bool:
    """Dotted lower case: ``adaptation.ch02``, ``coding.byte1.bit3``."""
    return isinstance(s, str) and bool(_ENTRY_ID.match(s))


def part_matches(pattern: str, part: str) -> bool:
    """``4F0910930*`` matches ``4F0910930AF``; only a trailing ``*`` is a
    wildcard (the suffix letters), nothing else."""
    pattern = (pattern or "").upper().replace(" ", "")
    part = (part or "").upper().replace(" ", "")
    if not pattern or not part:
        return False
    if pattern.endswith("*"):
        return part.startswith(pattern[:-1])
    return part == pattern


@dataclass(frozen=True)
class Identity:
    """What a module serves about itself, and what the user knows about the
    car -- every key a definition may match on. Missing = unknown."""
    addr: Optional[str] = None          # "01", "17" (two hex digits, as the gateway lists)
    protocol: Optional[str] = None      # kw1281 | kwp2000 | uds
    part: Optional[str] = None          # VAG part number as served
    sw: Optional[str] = None            # software version as served
    platform: Optional[str] = None      # the suite's platform id (edc16, zf6hp, ...)
    generation: Optional[str] = None    # from addressing.generations (C6, PQ35, ...)

    def with_(self, **kw) -> "Identity":
        d = self.__dict__.copy()
        d.update(kw)
        return Identity(**d)


@dataclass
class Match:
    """A document's ``match`` block."""
    addr: Optional[str] = None
    protocol: Optional[str] = None
    part: Optional[str] = None
    sw: List[str] = field(default_factory=list)
    platform: Optional[str] = None
    generation: Optional[str] = None

    #: the raw ``addr`` / ``sw`` as the document wrote them, for
    #: :meth:`problems` -- YAML reads ``addr: 10`` as the integer ten (0x0A
    #: once normalised, not module 10), so an unquoted value is refused there
    #: while :func:`_norm_addr` stays tolerant for callers in code.
    raw_addr: Any = field(default=None, repr=False, compare=False)
    raw_sw: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_doc(cls, raw: Optional[Dict[str, Any]]) -> "Match":
        raw = raw or {}
        sw = raw.get("sw")
        if sw is None:
            sw = []
        elif not isinstance(sw, list):
            sw = [sw]
        return cls(addr=_norm_addr(raw.get("addr")), protocol=raw.get("protocol"),
                   part=raw.get("part"), sw=[str(s) for s in sw],
                   platform=raw.get("platform"), generation=raw.get("generation"),
                   raw_addr=raw.get("addr"), raw_sw=raw.get("sw"))

    def problems(self) -> List[str]:
        out = []
        if self.raw_addr is not None and not isinstance(self.raw_addr, str):
            out.append("match.addr %r is not text: quote the address (addr: \"%s\"), YAML reads a bare "
                       "number as decimal" % (self.raw_addr, self.addr))
        elif self.addr is not None and not _ADDR.match(self.addr):
            out.append("match.addr %r is not two hex digits" % self.addr)
        raw_sw = self.raw_sw if isinstance(self.raw_sw, list) else ([] if self.raw_sw is None else [self.raw_sw])
        for s in raw_sw:
            if not isinstance(s, str):
                out.append("match.sw %r is not text: quote the version (sw: \"%s\"), YAML drops a "
                           "leading zero from a bare number" % (s, s))
        if self.protocol is not None and self.protocol not in PROTOCOLS:
            out.append("match.protocol %r is not one of %s" % (self.protocol, "/".join(PROTOCOLS)))
        if self.part is not None and not _PART.match(str(self.part)):
            out.append("match.part %r: a part number, '*' only at the end" % self.part)
        for s in self.sw:
            if not _SW.match(s):
                out.append("match.sw %r is not a software version" % s)
        if self.sw and self.part is None:
            out.append("match.sw needs match.part")
        return out

    def specificity(self, ident: Identity) -> Optional[int]:
        """How specific this match is for ``ident`` -- higher wins -- or None
        when a present key does not match. Unknown identity fields never
        match a constrained key, except ``generation``, which is advisory
        (a module reached by probing may have no generation yet)."""
        score = 0
        if self.addr is not None:
            if ident.addr is None or _norm_addr(ident.addr) != self.addr:
                return None
        if self.protocol is not None:
            if ident.protocol != self.protocol:
                return None
            score += 1
        if self.generation is not None:
            if ident.generation is not None and ident.generation != self.generation:
                return None
            # known and equal: more specific; unknown: the document still
            # applies but ranks below a generic one, so a car scanned without
            # a generation is not given one generation's variants
            score += 2 if ident.generation is not None else -1
        if self.platform is not None:
            if ident.platform != self.platform:
                return None
            score += 4
        if self.part is not None:
            if ident.part is None or not part_matches(self.part, ident.part):
                return None
            score += 20 if self.part.endswith("*") else 40
            if self.sw:
                if ident.sw is None or str(ident.sw).upper() not in [s.upper() for s in self.sw]:
                    return None
                score += 10
        return score

    def to_doc(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in ("addr", "protocol", "part", "platform", "generation"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.sw:
            out["sw"] = list(self.sw)
        return out


def _norm_addr(a: Any) -> Optional[str]:
    if a is None:
        return None
    if isinstance(a, int):
        return "%02X" % a
    s = str(a).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s.zfill(2) if len(s) == 1 else s


@dataclass
class Doc:
    """One definition document: its kind, match and body (the whole dict)."""
    kind: str
    match: Match
    body: Dict[str, Any]
    path: str = ""                  # where it was read from, for messages

    @property
    def entries(self) -> List[Dict[str, Any]]:
        key = KINDS.get(self.kind)
        if key is None:
            return []
        v = self.body.get(key)
        return v if isinstance(v, list) else []


@dataclass
class Pack:
    id: str
    version: str = "0.0.0"
    title: str = ""
    publisher: str = ""
    license: str = ""
    requires: Dict[str, str] = field(default_factory=dict)
    depends: List[str] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    docs: List[Doc] = field(default_factory=list)
    path: str = ""
    #: "bundled" (ships with the app), "module" (an installed .ovtmod),
    #: "user" (the user's local packs) -- the resolver's layering order
    origin: str = "bundled"
    #: for a pack read from its compiled form: the hash of the sources it
    #: was compiled from (``load.source_hash``), so a compiled pack beside
    #: edited sources is known to be stale whatever the file times say
    source_hash: str = ""

    def docs_of(self, kind: str) -> List[Doc]:
        return [d for d in self.docs if d.kind == kind]

    def manifest(self) -> Dict[str, Any]:
        return {"format": FORMAT, "id": self.id, "version": self.version, "title": self.title,
                "publisher": self.publisher, "license": self.license,
                "requires": dict(self.requires), "depends": list(self.depends),
                "coverage": dict(self.coverage)}
