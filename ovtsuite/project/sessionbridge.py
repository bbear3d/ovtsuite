# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Between a project's binary set and a :class:`~ovtsuite.session.TunerSession`.

A tab edits one branch of one set through an ordinary ``TunerSession``, so
every editor, checksum and tool works on it unchanged. The session is opened
from a *working file*: the set's state written as the platform's native
container (main image + MPC / EEPROM / loader / metadata, plus the branch's
definition cache so it opens fast), or a plain ``.bin`` for a platform that
has none. Saving the tab saves the session to that working file -- the usual
checksum correction and region folding happen there -- and the result is read
back and committed to the project as a checkpoint.

No Qt here: the GUI layer is :mod:`ovtsuite.gui_project`.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from .model import Member, ROLE_BASE
from .project import Project

Images = Dict[str, bytes]

#: member role -> native container block type name (``ovtsuite.container``)
_ROLE_BLOCK = {"flash": "BLOCK_EXTERNAL", "boot": "BLOCK_BOOT",
               "program": "BLOCK_PROGRAM", "cal": "BLOCK_CAL",
               "mpc": "BLOCK_MPC", "eeprom": "BLOCK_EEPROM",
               "loader": "BLOCK_LOADER"}

#: TunerSession region names -> member role
REGION_ROLE = {"MPC (internal flash)": "mpc", "EEPROM": "eeprom"}


def _blocks():
    from .. import container as _c
    return _c, {role: getattr(_c, name) for role, name in _ROLE_BLOCK.items()}


def _main_member(project: Project, sid: str):
    s = project.set(sid)
    for role in ("flash", "raw"):
        m = next((m for m in s.members if m.role == role), None)
        if m is not None:
            return m
    return s.members[0] if s.members else None


def set_platform(project: Project, sid: str, state: Images) -> str:
    """Set ``sid``'s platform, for its working file. A set stored as
    ``unknown`` -- imported before the base app had a detector for it -- is
    identified again from its main image; when that names a platform with a
    native container, the set adopts it (``Project.adopt_platform``), so it
    opens as that container and can carry a definition cache from then on."""
    from .. import container as _c
    from ..platforms import PLATFORMS
    s = project.set(sid)
    if s.platform in PLATFORMS and s.platform != "unknown":
        return s.platform
    m = _main_member(project, sid)
    data = state.get(m.id) if m is not None else None
    if not data:
        return s.platform
    from ..platforms.identify import identify
    try:
        ident = identify(bytes(data))
    except Exception:
        return s.platform
    if ident.known and _c.format_for_kind(ident.platform) is not None:
        project.adopt_platform(sid, ident.platform)
    return s.platform


def working_file(project: Project, sid: str, state: Images, *,
                 defn: Optional[bytes] = None,
                 log: Optional[str] = None) -> Tuple[str, bytes]:
    """``(extension, bytes)`` of a file a TunerSession opens as ``state`` of
    set ``sid``: its native container, or a plain image without one.

    ``log`` is the change-log text the session sees as this file's history
    (:func:`history_text`); a plain image's goes in a ``.log`` beside it
    (:func:`write_working_file`)."""
    _c, role_block = _blocks()
    s = project.set(sid)
    fmt = _c.format_for_kind(set_platform(project, sid, state))
    if fmt is None:
        m = _main_member(project, sid)
        return ".bin", bytes(state.get(m.id, b"")) if m else b""
    cont = _c.Container(fmt)
    if log:
        cont.add(_c.BLOCK_LOG, log.encode("utf-8"))
    for m in s.members:
        btype = role_block.get(m.role)
        if btype is None or m.id not in state:
            continue
        cont.add(btype, state[m.id], address=m.base)
        if m.role == "flash" and m.meta:
            cont.set_meta(m.meta)
    if defn:
        cont.add(_c.BLOCK_DEFN, defn)
    return fmt.ext, cont.to_bytes()


def definition_for(project: Project, sid: str, branch: str,
                   state: Images) -> Optional[bytes]:
    """The definition cache to open ``state`` of ``branch`` with: the
    branch's own, or -- when it has none, or its own would be rejected -- one
    of its sibling branches' (``Project.cache_candidates``). With a single
    candidate it is passed on unchecked (the session's own checks decide);
    with several, the first that ``ovtsuite.defn.usable`` accepts for this
    image, which costs far less than the matching it saves."""
    try:
        cands = project.cache_candidates(sid, branch)
    except (KeyError, AttributeError):
        return None
    if len(cands) <= 1:
        return cands[0] if cands else None
    m = _main_member(project, sid)
    data = state.get(m.id) if m is not None else None
    if data is None:
        return cands[0]
    from .. import defn as _defn
    platform = set_platform(project, sid, state)
    for blob in cands:
        if _defn.usable(blob, platform, bytes(data)) is None:
            return blob
    return cands[0]


def read_back(project: Project, sid: str, path: str) -> Tuple[Images, Optional[bytes]]:
    """The member images (and definition cache) of a working file a session
    just saved. A region the session added that the set has no member for
    yet (an EEPROM attached in the editor) becomes a new member."""
    _c, role_block = _blocks()
    s = project.set(sid)
    with open(path, "rb") as fh:
        data = fh.read()
    if _c.format_for_magic(data[:8]) is None:
        m = _main_member(project, sid)
        return {m.id: data}, None
    cont = _c.Container.from_bytes(data)
    block_role = {v: k for k, v in role_block.items()}
    images: Images = {}
    for blk in cont.blocks:
        role = block_role.get(blk.type)
        if role is None:
            continue
        m = next((m for m in s.members if m.role == role), None)
        if m is None:
            mm = _main_member(project, sid)
            m = project.add_member(s.id, Member(
                id=_free_id(s, role), role=role, platform=s.platform,
                module=dict(mm.module) if mm else {}, base=blk.address
                or ROLE_BASE.get(role, 0), endian=mm.endian if mm else "big"))
        images[m.id] = bytes(blk.data)
    flash = next((m for m in s.members if m.role == "flash"), None)
    meta = cont.meta()
    if flash is not None and meta and meta != flash.meta:
        project.update_member(s.id, flash.id, meta=meta)
    d = cont.get(_c.BLOCK_DEFN)
    return images, (bytes(d.data) if d is not None else None)


def _free_id(s, role):
    if s.member(role) is None:
        return role
    i = 2
    while s.member("%s%d" % (role, i)) is not None:
        i += 1
    return "%s%d" % (role, i)


def working_images(project: Project, sid: str, session,
                   eeprom: Optional[bytes] = None) -> Images:
    """The tab's bytes exactly as edited -- no checksum correction -- for
    crash recovery: the main image plus every region's working buffer."""
    s = project.set(sid)
    out: Images = {}
    main = _main_member(project, sid)
    if main is not None:
        out[main.id] = bytes(session.binfile.data)
    try:
        regions = session.region_buffers()
    except Exception:
        regions = []
    for name, buf in regions:
        role = REGION_ROLE.get(name)
        m = next((m for m in s.members if m.role == role), None) if role else None
        if m is not None:
            out[m.id] = bytes(buf)
    if eeprom is not None:
        m = next((m for m in s.members if m.role == "eeprom"), None)
        if m is not None:
            out[m.id] = bytes(eeprom)
    return out


def apply_working(project: Project, sid: str, session, images: Images) -> bool:
    """Make ``session`` hold ``images`` as UNSAVED edits (one undo step) --
    used to put recovered work back. True when anything changed."""
    s = project.set(sid)
    main = _main_member(project, sid)
    role_region = {v: k for k, v in REGION_ROLE.items()}
    changed = False
    with session.transaction("recovered edits"):
        for mid, data in images.items():
            m = s.member(mid)
            if m is None:
                continue
            if main is not None and mid == main.id:
                cur = bytes(session.binfile.data)
                for off, end in _runs(cur, data):
                    if session.edit_bytes(off, data[off:end]) is not None:
                        changed = True
            elif m.role in role_region:
                name = role_region[m.role]
                buf = session.region_edit_buffer(name)
                if buf is None:
                    continue
                for off, end in _runs(bytes(buf), data):
                    if session.edit_region(name, off, data[off:end]) is not None:
                        changed = True
    return changed


def _runs(a: bytes, b: bytes):
    n = min(len(a), len(b))
    i = 0
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            yield i, j
            i = j
        else:
            i += 1


def write_working_file(dirpath: str, stem: str, ext: str, data: bytes,
                       log: Optional[str] = None) -> str:
    """Write a working file under ``dirpath`` (a private temp folder); a plain
    image's change log goes in the ``.log`` beside it."""
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in stem).strip() \
        or "binary"
    path = os.path.join(dirpath, safe + ext)
    i = 2
    while os.path.exists(path):
        path = os.path.join(dirpath, "%s (%d)%s" % (safe, i, ext))
        i += 1
    with open(path, "wb") as fh:
        fh.write(data)
    if log and ext == ".bin":
        with open(os.path.splitext(path)[0] + ".log", "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(log)
    return path


# ------------------------------------------------- history <-> change log
VERSION = "VERSION "


def history_text(project: Project, sid: str, cid: str) -> str:
    """The branch history ending at checkpoint ``cid`` as change-log text --
    one ``VERSION`` record per checkpoint (id = the checkpoint's id, ranges
    from its delta) plus the lines it carries (``PATCH`` records, edit
    summaries). A session opened on it lists these as its saved versions,
    so Version history, Compare with saved version, Remove patch and
    Generate patch from history all work on a project tab unchanged.

    A checkpoint that changed a member's size (or added/removed one) cannot
    be written as ranges: the history the session sees starts after it."""
    import json
    s = project.set(sid)
    main = _main_member(project, sid)
    role_region = {v: k for k, v in REGION_ROLE.items()}
    chain = list(reversed(s.lineage(cid)))
    out: list = []
    for c in chain[1:]:
        recs = project.delta_records(sid, c.id)
        if recs is None:
            out = []
            continue
        ranges, regions, ok = [], {}, True
        for r in recs:
            m = s.member(r.member)
            if r.op != 1 or m is None:
                ok = False
                break
            item = [r.offset, bytes(r.old).hex(), bytes(r.new).hex()]
            if main is not None and r.member == main.id:
                ranges.append(item)
            elif m.role in role_region:
                regions.setdefault(role_region[m.role], []).append(item)
        if not ok:
            out = []                    # the session's history starts after it
            continue
        rec = {"id": c.id, "ts": c.ts, "label": c.label or c.kind,
               "ranges": ranges,
               "n_bytes": c.n_bytes}
        if regions:
            rec["regions"] = regions
        extra = getattr(c, "extra", None) or {}
        if extra.get("merged_from"):
            # the versions merged into this one, and itself: Version history
            # shows "Merged from N saves" and the first and last save's time
            rec["merged_from"] = list(extra["merged_from"]) + [c.id]
            if extra.get("ts_from"):
                rec["ts_from"] = extra["ts_from"]
        when = (c.ts or "").replace("T", " ")
        block = ["# %s  saved %s\n" % (when, c.label or c.kind)]
        block += ["  %s\n" % line for line in c.log
                  if not line.startswith(VERSION)]
        block.append("  %s%s\n" % (VERSION, json.dumps(rec, separators=(",", ":"))))
        out.append("".join(block))
    return "".join(out)


def _blocks_of(text: str):
    """[[lines]] -- one list per ``# ... saved ...`` block of a change log."""
    blocks, cur = [], None
    for raw in (text or "").split("\n"):
        if raw.startswith("# "):
            cur = []
            blocks.append(cur)
            continue
        line = raw[2:] if raw.startswith("  ") else raw.strip()
        if not line:
            continue
        if cur is None:
            cur = []
            blocks.append(cur)
        cur.append(line)
    return blocks


def read_log(project: Project, sid: str, cid: str, text: str):
    """What a session added to its working file's change log since it was
    opened on checkpoint ``cid``'s history: ``(new lines, label, renames)``.

    ``new lines`` are the entries of every save whose ``VERSION`` record is
    not a checkpoint of the branch (normally the one just made), without the
    ``VERSION`` records themselves (the project's delta is the byte record);
    ``label`` is that save's name if the user gave one; ``renames`` is
    ``{checkpoint id: name}`` for checkpoints renamed in Version history."""
    import json
    known = {c.id: c for c in project.set(sid).lineage(cid)}
    new, label, renames = [], "", {}
    for block in _blocks_of(text):
        ids, recs = [], []
        for line in block:
            if line.startswith(VERSION):
                try:
                    rec = json.loads(line[len(VERSION):])
                except ValueError:
                    continue
                recs.append(rec)
                ids.append(rec.get("id"))
        if ids and all(i in known for i in ids):
            for rec in recs:
                name = rec.get("name") or ""
                if name and name != known[rec["id"]].label:
                    renames[rec["id"]] = name
            continue
        new += [l for l in block if not l.startswith(VERSION)]
        for rec in recs:
            if rec.get("name"):
                label = rec["name"]
    return new, label, renames
