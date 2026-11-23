# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An import into a flash set that already links an EEPROM / MPC: compare first.

When a file imported into an existing flash set holds the EEPROM and/or MPC
part (a PCMFlash or one-file read, a container, a WinOLS project) and the set
already links a companion for that part, the import keeps the linked one
(:func:`companions.split` skips a linked role). This module lets the caller
**ask**: :func:`compare` gives a verdict per part against the linked
companion's current head; :func:`apply` carries out the choice per differing
part. No Qt here; ``gui_importparts`` is the dialog on top.

Choices (per part that differs; an identical part has none):

* :data:`KEEP` -- the linked one stays (the default; nothing changes);
* :data:`VERSION` -- the file's part becomes a new version of the linked
  companion (``Imported from <file>``; offered only when the sizes match);
* :data:`SEPARATE` -- the file's part becomes a companion set of its own and
  this flash set links it; the old companion stays in the project as a set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

KEEP, VERSION, SEPARATE = "keep", "version", "separate"

IDENTICAL, DIFFERS, IDENTITY, SIZE = "identical", "differs", "identity", "size"

#: identity facts that tell one EEPROM / MPC from another
_KEYS = ("vin", "part", "software", "ident")


@dataclass
class PartVerdict:
    role: str                       # "eeprom" / "mpc"
    data: bytes                     # the file's part
    linked: object                  # the linked companion set
    kind: str                       # IDENTICAL / DIFFERS / IDENTITY / SIZE
    text: str                       # "identical" / "differs in 12 bytes" ...
    changed: int = 0                # bytes that differ (same size)
    identity: str = "unreadable"    # "same" / "different" / "unreadable"
    new_facts: Dict[str, str] = field(default_factory=dict)
    old_facts: Dict[str, str] = field(default_factory=dict)
    can_version: bool = False       # sizes match
    others: List[str] = field(default_factory=list)   # other flashes sharing it
    warn_immobiliser: bool = False  # EEPROM whose identity differs

    @property
    def differs(self) -> bool:
        return self.kind != IDENTICAL

    @property
    def label(self) -> str:
        return "MPC (internal flash)" if self.role == "mpc" else "EEPROM"

    def choices(self) -> List[str]:
        """The choices offered; none for an identical part."""
        if not self.differs:
            return []
        return [KEEP] + ([VERSION] if self.can_version else []) + [SEPARATE]


@dataclass
class Applied:
    role: str
    how: str                        # KEEP / VERSION / SEPARATE / "same"
    companion: str = ""             # companion set id now linked
    touched: List[str] = field(default_factory=list)  # companions whose head moved
    flashes: List[str] = field(default_factory=list)  # flash sets to refresh


# ------------------------------------------------------------------ gather
def held_linked(project, sid: str, branch=None) -> Dict[str, bytes]:
    """``{role: bytes}`` of the EEPROM / MPC parts the head of ``branch`` (the
    one an import just added; default the active one) of flash set ``sid``
    holds for a role the set already links."""
    from .. import platforms as P
    from . import companions as _co
    s = project.set(sid)
    branch = branch or s.active
    if s.platform not in P.EEPROM_FOR or branch is None:
        return {}
    linked = _co.links(project, sid)
    state = project.state(s.id, branch.head)
    out = {}
    for role in P.companion_roles(s.platform):
        m = next((m for m in s.members if m.role == role), None)
        if role in linked and m is not None and m.id in state:
            out[role] = bytes(state[m.id])
    return out


def _facts(data: bytes) -> Dict[str, str]:
    from ..platforms.identify import identify
    try:
        f = identify(bytes(data)).facts
    except Exception:
        return {}
    return {k: str(f[k]) for k in _KEYS if f.get(k)}


def _same_identity(new: Dict[str, str], old: Dict[str, str]) -> str:
    common = [k for k in new if k in old]
    if not common:
        return "unreadable"
    return "same" if all(new[k] == old[k] for k in common) else "different"


def _describe_facts(f: Dict[str, str]) -> str:
    return ", ".join("%s %s" % (k.upper() if k == "vin" else k, f[k])
                     for k in _KEYS if k in f) or "no identity"


def compare(project, sid: str, held: Dict[str, bytes]) -> List[PartVerdict]:
    """A verdict for each held part against the head of the companion flash
    set ``sid`` links for that role (parts with no linked companion are left
    out)."""
    from . import companions as _co
    out = []
    here = project.set(sid)
    for role, data in held.items():
        c = _co.links(project, sid).get(role)
        if c is None:
            continue
        old = _co.head_bytes(project, c)
        if old is None:
            continue
        data, old = bytes(data), bytes(old)
        v = PartVerdict(role, data, c, DIFFERS, "")
        v.others = [f.name for f in _co.flashes_using(project, c.id)
                    if f.id != sid]
        v.new_facts, v.old_facts = _facts(data), _facts(old)
        v.identity = _same_identity(v.new_facts, v.old_facts)
        v.can_version = len(data) == len(old)
        if v.can_version:
            v.changed = sum(1 for a, b in zip(data, old) if a != b)
        if v.can_version and v.changed == 0:
            v.kind, v.text = IDENTICAL, "identical, nothing to do"
        elif not v.can_version:
            v.kind = SIZE
            v.text = ("different size: the file's is %d bytes, %s is %d; it "
                      "cannot become a version of the linked one"
                      % (len(data), c.name, len(old)))
        elif v.identity == "different":
            v.kind = IDENTITY
            v.text = "different identity (%s, linked: %s); differs in %d bytes" % (
                _describe_facts(v.new_facts), _describe_facts(v.old_facts),
                v.changed)
        else:
            v.kind = DIFFERS
            v.text = "differs in %d bytes" % v.changed
            if v.identity == "unreadable":
                v.text += "; identity not readable"
        if v.kind == SIZE and v.identity == "unreadable":
            v.text += "; identity not readable"
        v.warn_immobiliser = role == "eeprom" and v.kind != IDENTICAL \
            and v.identity == "different"
        out.append(v)
    return out


def pending(project, sid: str, held: Dict[str, bytes]) -> List[PartVerdict]:
    """The verdicts worth asking about: the parts that differ."""
    return [v for v in compare(project, sid, held) if v.differs]


# ------------------------------------------------------------------- apply
def apply(project, sid: str, verdicts: List[PartVerdict],
          choices: Dict[str, str], filename: str = "") -> List[Applied]:
    """Carry out ``choices`` (``{role: KEEP|VERSION|SEPARATE}``; a role not
    named keeps) for ``verdicts``. ValueError for a version of another size
    (nothing done for that part)."""
    from . import companions as _co
    done = []
    for v in verdicts:
        how = choices.get(v.role, KEEP)
        if how not in (VERSION, SEPARATE) or not v.differs:
            done.append(Applied(v.role, KEEP))
            continue
        label = "Imported from %s" % (filename or "file")
        flashes_before = [f.id for f in _co.flashes_using(project, v.linked.id)]
        if how == VERSION:
            got = _co.take(project, sid, v.role, v.data, label=label)
            if got == "version":
                done.append(Applied(v.role, VERSION, v.linked.id,
                                    [v.linked.id], flashes_before))
            else:
                done.append(Applied(v.role, "same", v.linked.id))
        else:
            c = _co.attach(project, sid, v.role, v.data,
                           base=0x400000 if v.role == "mpc" else 0,
                           label=label, source={"file": filename,
                                                "format": "import"})
            done.append(Applied(v.role, SEPARATE, c.id, [], [sid]))
    return done
