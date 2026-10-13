# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The structural records of an ``.ovt`` project (see docs/OVT_CONTAINER.md).

Every record keeps the keys it does not understand in ``extra`` and writes
them back unchanged, so a file written by a newer build survives a round trip
through an older one (forward compatibility, spec section 6).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

#: Member roles. ``flash`` is the platform's main image whatever the platform
#: calls it (external flash, TCU flash, cluster EEPROM image, ...).
ROLES = ("flash", "mpc", "eeprom", "boot", "program", "cal", "loader", "raw")

#: Checkpoint kinds.
KINDS = ("read", "import", "save", "patch", "revert", "merge")

#: Set kinds (UI badge / tint).
SET_KINDS = ("ecu", "tcu", "cluster", "hvac", "other")

#: Platform -> set kind, for a new set's default.
PLATFORM_SET_KIND = {
    "edc16": "ecu", "edc15": "ecu",
    "zf6hp": "tcu", "zf8hp": "tcu", "aisin": "tcu",
    "vdo": "cluster", "hvac": "hvac",
}

#: Default member display names and base addresses per role.
ROLE_NAMES = {
    "flash": "Flash", "mpc": "MPC (internal flash)", "eeprom": "EEPROM",
    "boot": "Boot", "program": "Program", "cal": "Calibration",
    "loader": "Flash loader", "raw": "Binary",
}
ROLE_BASE = {"mpc": 0x400000}

#: The project information fields the app knows: ``(key, label, hint)``.
#: Stored in the manifest's ``info`` object (strings, empty ones left out);
#: a key the app does not know (a later version's, or one an import brought
#: in) is kept and shown under its own name.
INFO_FIELDS = (
    ("make", "Make", "Volkswagen"),
    ("model", "Model", "Golf V 2.0 TDI"),
    ("chassis", "Chassis code", "1K"),
    ("year", "Model year", "2006"),
    ("vin", "VIN", "17 characters"),
    ("engine", "Engine code", "BKD"),
    ("gearbox", "Gearbox", "09G / 6HP19"),
    ("power", "Stock power", "103 kW / 320 Nm"),
    ("odometer", "Odometer", "182 000 km"),
    ("ecu", "Control unit", "Bosch EDC16C34"),
    ("customer", "Owner / customer", ""),
    ("notes", "Notes", ""),
)
INFO_LABELS = {k: label for k, label, _h in INFO_FIELDS}


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class _Record:
    """Dict round-trip with unknown keys preserved in ``extra``."""

    #: field name -> manifest key, where they differ
    _KEYS: Dict[str, str] = {}

    @classmethod
    def _names(cls):
        return [f.name for f in fields(cls) if f.name != "extra"]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict(self.extra)
        for n in self._names():
            out[self._KEYS.get(n, n)] = self._encode(n, getattr(self, n))
        return out

    def _encode(self, name, value):
        return value

    @classmethod
    def _decode(cls, name, value):
        return value

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        if not isinstance(d, dict):
            raise ValueError("%s: expected an object" % cls.__name__)
        kw: Dict[str, Any] = {}
        known = set()
        for n in cls._names():
            key = cls._KEYS.get(n, n)
            known.add(key)
            if key in d:
                kw[n] = cls._decode(n, d[key])
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(extra=extra, **kw)


@dataclass
class Member(_Record):
    """One memory region of a control unit. Structural, trusted on reopen."""
    id: str
    role: str = "flash"
    name: str = ""
    platform: str = "unknown"
    module: Dict[str, str] = field(default_factory=dict)   # {"id", "version"}
    base: int = 0
    endian: str = "big"
    word: int = 1
    definition: Optional[Dict[str, str]] = None            # {"path", "type"}
    meta: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or ROLE_NAMES.get(self.role, self.id)

    @property
    def module_id(self) -> str:
        return (self.module or {}).get("id", "")


@dataclass
class Checkpoint(_Record):
    """One immutable point in a set's history."""
    id: str
    parent: Optional[str] = None
    kind: str = "save"
    ts: str = ""
    label: str = ""
    source: Dict[str, Any] = field(default_factory=dict)
    sizes: Dict[str, int] = field(default_factory=dict)   # member id -> size
    n_bytes: int = 0                                      # bytes changed vs parent
    #: the change-log lines of this save (edit summaries, ``PATCH`` records
    #: -- what Remove patch reads); the byte changes are the delta itself
    log: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_root(self) -> bool:
        return self.parent is None


@dataclass
class Branch(_Record):
    id: str
    name: str
    head: str
    created: str = ""
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Original(_Record):
    """An original delivery container, kept byte-for-byte."""
    id: str
    filename: str
    format: str                                   # "sgo" | "dim" | "frf" | "edc16" ...
    checkpoint: Optional[str] = None              # the state it decodes to
    members: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BinarySet(_Record):
    id: str
    name: str
    kind: str = "other"
    platform: str = "unknown"
    identity: Dict[str, str] = field(default_factory=dict)
    active_branch: Optional[str] = None
    members: List[Member] = field(default_factory=list)
    checkpoints: List[Checkpoint] = field(default_factory=list)
    branches: List[Branch] = field(default_factory=list)
    originals: List[Original] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    _LISTS = {"members": Member, "checkpoints": Checkpoint,
              "branches": Branch, "originals": Original}

    def _encode(self, name, value):
        if name in self._LISTS:
            return [v.to_dict() for v in value]
        return value

    @classmethod
    def _decode(cls, name, value):
        if name in cls._LISTS:
            return [cls._LISTS[name].from_dict(v) for v in (value or [])]
        return value

    # -- lookups -----------------------------------------------------------
    def member(self, mid: str) -> Optional[Member]:
        return next((m for m in self.members if m.id == mid), None)

    def checkpoint(self, cid: str) -> Optional[Checkpoint]:
        return next((c for c in self.checkpoints if c.id == cid), None)

    def branch(self, bid_or_name: str) -> Optional[Branch]:
        for b in self.branches:
            if b.id == bid_or_name:
                return b
        for b in self.branches:
            if b.name.lower() == (bid_or_name or "").lower():
                return b
        return None

    def original(self, oid: str) -> Optional[Original]:
        return next((o for o in self.originals if o.id == oid), None)

    @property
    def active(self) -> Optional[Branch]:
        return self.branch(self.active_branch) if self.active_branch else (
            self.branches[0] if self.branches else None)

    def roots(self) -> List[Checkpoint]:
        return [c for c in self.checkpoints if c.parent is None]

    def children(self, cid: str) -> List[Checkpoint]:
        return [c for c in self.checkpoints if c.parent == cid]

    def lineage(self, cid: str) -> List[Checkpoint]:
        """``cid`` and its ancestors, newest first."""
        out: List[Checkpoint] = []
        seen = set()
        c = self.checkpoint(cid)
        while c is not None and c.id not in seen:
            out.append(c)
            seen.add(c.id)
            c = self.checkpoint(c.parent) if c.parent else None
        return out

    def root_of(self, cid: str) -> Optional[Checkpoint]:
        chain = self.lineage(cid)
        return chain[-1] if chain else None


@dataclass
class Attachment(_Record):
    """A datalog (``logs``) or any other attachment (``attachments``)."""
    id: str
    filename: str
    added: str = ""
    set: Optional[str] = None
    pinned: bool = False
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
