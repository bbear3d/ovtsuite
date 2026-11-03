# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Companions: an ECU's EEPROM and (EDC16) internal flash as sets of their own.

An EDC15/EDC16 EEPROM and an EDC16 internal flash (MPC) are not parts of the
flash's history: each is a set of its own -- its own versions and branches, its
own tab, exported on its own -- *linked* from the flash set it goes with. The
link is ``set.extra["companions"] = {"eeprom": <set id>, "mpc": <set id>}``
(unknown keys survive older builds, which show the companion as a plain set).

Only a matching companion links (``platforms.COMPANION_OF``: an EDC15 EEPROM to
an EDC15 flash, an EDC16 EEPROM or MPC to an EDC16 flash), one companion may
serve several flash sets, and a flash **follows the head of the companion's
active branch**: its working file is made with the companion's current bytes in
the EEPROM / MPC block, and an edit made to that block in the flash's tab is
saved as a new version of the companion, not of the flash.

Projects from before keep ``eeprom`` / ``mpc`` members in the flash set;
:func:`split` moves them out into companion sets (the old members stay in the
old versions' history and are no longer used).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .model import BinarySet, Member

KEY = "companions"
#: ``read_back`` key prefix of a companion's bytes in a saved working file
PREFIX = "@"


# ---------------------------------------------------------------- the links
def links(project, sid: str) -> Dict[str, BinarySet]:
    """``{role: companion set}`` of flash set ``sid`` (links to sets that no
    longer exist, or no longer match, are left out)."""
    from .. import platforms as P
    s = project.set(sid)
    out = {}
    for role, csid in dict((s.extra or {}).get(KEY) or {}).items():
        c = next((x for x in project.sets if x.id == csid), None)
        if c is None:
            continue
        want = P.COMPANION_OF.get(c.platform)
        if want == (s.platform, role):
            out[role] = c
    return out


def role_of(project, comp_sid: str, flash_sid: str) -> str:
    """The role companion ``comp_sid`` would take in flash set ``flash_sid``;
    ValueError when they do not match."""
    from .. import platforms as P
    c, s = project.set(comp_sid), project.set(flash_sid)
    want = P.COMPANION_OF.get(c.platform)
    if want is None or want[0] != s.platform:
        raise ValueError("%s is not an EEPROM / internal flash of %s"
                         % (c.name, s.name))
    return want[1]


def link(project, flash_sid: str, comp_sid: str) -> str:
    """Link companion set ``comp_sid`` to flash set ``flash_sid`` (replacing
    the companion it had in that role). Returns the role."""
    role = role_of(project, comp_sid, flash_sid)
    s = project.set(flash_sid)
    cur = dict(s.extra.get(KEY) or {})
    if cur.get(role) != comp_sid:
        cur[role] = comp_sid
        s.extra[KEY] = cur
        project._touch()
    return role


def unlink(project, flash_sid: str, role: str) -> None:
    s = project.set(flash_sid)
    cur = dict(s.extra.get(KEY) or {})
    if cur.pop(role, None) is not None:
        if cur:
            s.extra[KEY] = cur
        else:
            s.extra.pop(KEY, None)
        project._touch()


def flashes_using(project, comp_sid: str) -> List[BinarySet]:
    """The flash sets that link companion ``comp_sid``."""
    return [s for s in project.sets
            if comp_sid in dict((s.extra or {}).get(KEY) or {}).values()]


def unlinked_companions(project, ecu: str) -> List[BinarySet]:
    """Companion sets of control unit ``ecu`` that no flash links yet."""
    from .. import platforms as P
    return [c for c in project.sets
            if P.COMPANION_OF.get(c.platform, ("",))[0] == ecu
            and not flashes_using(project, c.id)]


# ------------------------------------------------------------- the bytes
def main_member(c: BinarySet) -> Optional[Member]:
    return next((m for m in c.members if m.role in ("eeprom", "mpc")),
                c.members[0] if c.members else None)


def head_bytes(project, c: BinarySet) -> Optional[bytes]:
    """The companion's bytes at the head of its active branch."""
    m = main_member(c)
    if m is None or c.active is None:
        return None
    return project.state(c.id, c.active.head).get(m.id)


def injected(project, sid: str) -> Dict[str, bytes]:
    """``{role: bytes}`` of the flash set's companions, for its working file."""
    out = {}
    for role, c in links(project, sid).items():
        data = head_bytes(project, c)
        if data is not None:
            out[role] = bytes(data)
    return out


def bytes_of(project, sid: str, role: str, branch: Optional[str] = None
             ) -> Optional[bytes]:
    """The ``role`` part of flash set ``sid``: its companion's head, else its
    own legacy member."""
    c = links(project, sid).get(role)
    if c is not None:
        return head_bytes(project, c)
    return project.member_bytes(sid, role, branch=branch) \
        if project.set(sid).member(role) is not None else None


# --------------------------------------------------------- saving edits
def commit_edits(project, sid: str, edits: Dict[str, bytes]) -> List[str]:
    """Save ``edits`` (``{role: bytes}`` taken from a flash tab's working file)
    as new versions of the companions. Returns the ids of the companion sets
    that changed."""
    from .. import gui_eeprompair  # noqa: F401  (names the version)
    touched = []
    for role, data in edits.items():
        c = links(project, sid).get(role)
        if c is None:
            continue
        old = head_bytes(project, c)
        if old is None or bytes(old) == bytes(data):
            continue
        label = "MPC edit"
        if role == "eeprom":
            from ..gui_eeprompair import describe_change
            label = describe_change(old, data)
        project.commit(c.id, None, {main_member(c).id: bytes(data)}, label=label)
        touched.append(c.id)
    return touched


# -------------------------------------------------------------- migration
def split(project, sid: str) -> List[BinarySet]:
    """Move the ``eeprom`` / ``mpc`` members of flash set ``sid`` out into
    companion sets (from the head of its active branch) and link them. Returns
    the sets made; nothing when there is nothing to move."""
    from .. import platforms as P
    from .importers import _member, _module_ref  # noqa: F401
    s = project.set(sid)
    if s.platform not in P.EEPROM_FOR or s.active is None:
        return []
    made = []
    state = project.state(s.id, s.active.head)
    for role in P.companion_roles(s.platform):
        m = next((m for m in s.members if m.role == role), None)
        if m is None or m.id not in state or links(project, sid).get(role):
            continue
        pid = next(p for p, v in P.COMPANION_OF.items() if v == (s.platform, role))
        c = project.add_set("%s %s" % (s.name, "EEPROM" if role == "eeprom" else "MPC"),
                            platform=pid)
        project.add_member(c.id, Member(
            id=role, role=role, platform=pid, endian="big",
            base=m.base if role == "mpc" else 0))
        project.add_root(c.id, {role: bytes(state[m.id])}, kind="import",
                         label="Split from %s" % s.name,
                         source={"split_from": s.id})
        link(project, sid, c.id)
        made.append(c)
    return made


def split_all(project) -> List[BinarySet]:
    out = []
    for s in list(project.sets):
        out += split(project, s.id)
    return out
