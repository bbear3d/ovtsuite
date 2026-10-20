# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Automatic version history for a TunerSession's saved binary.

Every :meth:`TunerSession.save` call logs one compact record of every byte
range that changed since the file was last saved to this path (or, for the
first save, since it was loaded) -- reusing the SAME change-log file the
patch-apply/revert machinery already writes to (see
:mod:`ovtsuite.patch`'s ``PATCH_LOG_MARKER``), under its own marker so the
two record kinds never collide. Nothing new is invented for storage: it is
the same change log already written with a saved binary -- inside the file
for an ``.edc16`` container (its ``history`` block), in the ``.log`` beside
it for anything else (see :mod:`ovtsuite.changelog`).

"Revert to an earlier version" walks that chain backward -- undoing every
LATER save's recorded changes, newest first -- restoring exactly the byte
state right after the target version's own save, using nothing but the log
beside the binary. No separate version-history file, and no full-binary
snapshot is ever kept: each record only stores the bytes that actually
changed that save.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from .diff import _byte_ranges

#: Prefix marking an automatic save checkpoint inside a change-log line --
#: distinct from ``ovtsuite.patch.PATCH_LOG_MARKER`` so the two record kinds
#: can share one log file without colliding.
VERSION_LOG_MARKER = "VERSION "


def _log_path_for(session) -> str:
    """The sidecar ``.log`` -- only where the log is NOT inside the file
    (see :mod:`ovtsuite.changelog`; an ``.edc16`` carries it as a block)."""
    return os.path.splitext(session.path)[0] + ".log"


def _saved_lines(session):
    from . import changelog as _changelog
    return _changelog.read_lines(getattr(session, "path", None))


def _iter_version_records(lines):
    for line in lines:
        line = line.strip()
        if line.startswith(VERSION_LOG_MARKER):
            try:
                yield json.loads(line[len(VERSION_LOG_MARKER):])
            except ValueError:
                continue


def region_changes(session):
    """``{region name: (saved bytes, edited bytes)}`` for every non-main
    region (EEPROM, MPC internal flash) whose working buffer differs from
    what was last saved -- the hex editor's edits to them."""
    out = {}
    for name, buf in (getattr(session, "_region_edits", None) or {}).items():
        base = session.region_saved(name)
        if base is not None and bytes(base) != bytes(buf):
            out[name] = (bytes(base), bytes(buf))
    return out


def _ranges_of(old: bytes, new: bytes):
    n = min(len(old), len(new))
    return [[s, old[s:e].hex(), new[s:e].hex()]
            for s, e in _byte_ranges(bytes(old), bytes(new), 0, n)]


def log_version(session, old: bytes, new: bytes, label: str = "",
                regions=None) -> Optional[dict]:
    """Append one checkpoint recording every byte range that differs between
    ``old`` (the bytes as last saved, or as loaded) and ``new`` (the bytes
    about to be written). Called automatically from
    :meth:`TunerSession.save`; returns None (and logs nothing) when nothing
    actually changed.

    ``regions`` is ``{name: (old, new)}`` for the container's other regions
    (EEPROM, MPC internal flash, see :func:`region_changes`); their changes
    ride in the same record under ``"regions"``, so one save is one version
    whichever editor made the edit.
    """
    ranges = _ranges_of(old, new)
    reg = {}
    for name, (ro, rn) in sorted((regions or {}).items()):
        r = _ranges_of(ro, rn)
        if r:
            reg[name] = r
    if not ranges and not reg:
        return None
    rec = {"id": uuid.uuid4().hex[:10],
          "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "label": label or "save",
          "ranges": ranges,
          "n_bytes": _span(ranges) + sum(_span(r) for r in reg.values())}
    if reg:
        rec["regions"] = reg
    session.log(VERSION_LOG_MARKER + json.dumps(rec, separators=(",", ":")))
    return rec


def _span(ranges) -> int:
    return sum(len(bytes.fromhex(o)) for _s, o, _n in ranges)


def list_versions(session) -> List[dict]:
    """Every save checkpoint found for ``session``, most recent first.

    Reads both the saved change log (inside an ``.edc16``, else the ``.log``
    beside ``session.path`` -- see :mod:`ovtsuite.changelog`; versions from
    a previous, saved session) and ``session.change_log`` in memory
    (checkpoints logged this session but not yet flushed by a save -- these
    disappear if the session is closed without saving). Each dict gets an
    extra ``"pending"`` key.
    """
    disk: List[dict] = list(_iter_version_records(_saved_lines(session)))
    for r in disk:
        r["pending"] = False
    pending = list(_iter_version_records(session.change_log))
    for r in pending:
        r["pending"] = True
    combined = disk + pending
    combined.reverse()
    return combined


#: a ``version_id`` for :func:`bytes_at_version` / :func:`regions_at_version`:
#: the state before the oldest checkpoint -- the binary as it was first
#: opened (in a project tab, the original as read)
ORIGINAL = "@original"


def _to_undo(versions, version_id):
    """The checkpoints newer than ``version_id`` (all of them for
    :data:`ORIGINAL`), newest first."""
    if version_id == ORIGINAL:
        return list(versions)
    ids = [v["id"] for v in versions]
    if version_id not in ids:
        raise ValueError("no version %r found in this binary's history" % version_id)
    return versions[:ids.index(version_id)]


def bytes_at_version(session, version_id: str) -> bytes:
    """The full binary image exactly as it was right after the save that
    logged ``version_id`` -- a pure, non-mutating counterpart to
    :func:`revert_to_version`: it walks the same checkpoint chain backward
    and restores the same byte ranges, but on a private copy of the current
    bytes, so ``session`` itself is left untouched (no transaction, no new
    log record, no drift warnings). Used to reconstruct historical states
    for comparison -- see ``ovtsuite.patch.diff_from_history``.
    """
    versions = list_versions(session)          # newest first
    to_undo = _to_undo(versions, version_id)    # strictly newer, newest first

    # Anchor on the state as of the LAST checkpoint actually logged (by this
    # session or an earlier one that saved to the same path), not on the
    # live binary -- any edit made since that save but not yet saved again
    # was never logged as a checkpoint, so it must not leak into a
    # historical reconstruction. `_last_saved_snapshot` tracks exactly that
    # (see TunerSession.__init__/save()); a plain object without it (e.g. a
    # test double) falls back to the live bytes.
    data = bytearray(getattr(session, "_last_saved_snapshot", session.binfile.data))
    for v in to_undo:
        for addr, old_hex, new_hex in v["ranges"]:
            old_b = bytes.fromhex(old_hex)
            data[addr:addr + len(old_b)] = old_b
    return bytes(data)


def regions_at_version(session, version_id: str) -> dict:
    """``{region name: bytes}`` of the EEPROM / MPC regions right after the
    save that logged ``version_id`` -- the counterpart of
    :func:`bytes_at_version` for what the hex editor changes there. Only
    regions the history ever touched, or that exist now, are returned."""
    to_undo = _to_undo(list_versions(session), version_id)
    out = {}
    for name in session.region_names():
        base = session.region_saved(name)
        if base is not None:
            out[name] = bytearray(base)
    for v in to_undo:
        for name, rngs in (v.get("regions") or {}).items():
            buf = out.get(name)
            if buf is None:
                continue
            for addr, old_hex, _new in rngs:
                old_b = bytes.fromhex(old_hex)
                buf[addr:addr + len(old_b)] = old_b
    return {k: bytes(v) for k, v in out.items()}


@dataclass
class RevertResult:
    n_versions: int
    n_bytes: int
    warnings: List[str] = field(default_factory=list)


def revert_to_version(session, version_id: str) -> RevertResult:
    """Restore ``session``'s binary to the state it was in right after the
    save that logged ``version_id`` -- undoing every LATER checkpoint in
    order, newest first, using only the change log; no separate
    version-history file or full-image backup is needed.

    Every range from every checkpoint being undone is written back through
    :meth:`TunerSession.edit_bytes` inside one :meth:`TunerSession.transaction`,
    so the whole revert is a single undo step in this session. The state it
    restores is ITSELF logged as a new checkpoint (labelled "revert to
    version <id>"), so a revert can be found by :func:`list_versions` and,
    if unwanted, reverted right back.

    A range that was written again after its checkpoint (by a later manual
    edit, patch apply, or another revert) still gets restored to the older
    value -- flagged in ``RevertResult.warnings``, not blocked, since a
    silent refusal would leave no way to back out a bad sequence of changes
    at all.
    """
    versions = list_versions(session)          # newest first
    to_undo = _to_undo(versions, version_id)    # strictly newer, newest first
    if not to_undo:
        return RevertResult(0, 0, ["already at this version"])

    before = bytes(session.binfile.data)
    regions_before = {n: bytes(b) for n, b in session.region_buffers()
                      if n != "External flash"}
    warnings: List[str] = []
    n_bytes = 0
    with session.transaction("revert to version %s" % version_id):
        for v in to_undo:
            for name, rngs in (v.get("regions") or {}).items():
                for addr, old_hex, new_hex in rngs:
                    old_b, new_b = bytes.fromhex(old_hex), bytes.fromhex(new_hex)
                    buf = session.region_edit_buffer(name)
                    if buf is None:
                        warnings.append("%s @0x%X: this file no longer has "
                                        "that region; not restored"
                                        % (name, addr))
                        continue
                    if bytes(buf[addr:addr + len(new_b)]) != new_b:
                        warnings.append(
                            "%s @0x%X: changed again after checkpoint %s; "
                            "reverting still restores the older value there"
                            % (name, addr, v["id"]))
                    session.edit_region(name, addr, old_b)
                    n_bytes += len(old_b)
            for addr, old_hex, new_hex in v["ranges"]:
                old_b, new_b = bytes.fromhex(old_hex), bytes.fromhex(new_hex)
                cur = bytes(session.binfile.data[addr:addr + len(new_b)])
                if cur != new_b:
                    warnings.append(
                        "0x%X: changed again after checkpoint %s; reverting "
                        "still restores the older value there"
                        % (addr, v["id"]))
                session.edit_bytes(addr, old_b)
                n_bytes += len(old_b)

    # a resized table takes the size its record holds again (ovtsuite.resize)
    follow = getattr(session, "follow_layout", None)
    if follow is not None:
        follow()
    regions_after = {n: (regions_before[n], bytes(b))
                     for n, b in session.region_buffers()
                     if n in regions_before}
    log_version(session, before, bytes(session.binfile.data),
               label="revert to version %s" % version_id,
               regions=regions_after)
    return RevertResult(n_versions=len(to_undo), n_bytes=n_bytes, warnings=warnings)


# -- names and merging ------------------------------------------------------
#
# Both rewrite the version records in place -- in the ``.log`` beside the
# binary for saved checkpoints, in ``session.change_log`` for ones still
# pending -- and leave every other line of the log exactly as it was.


def display_label(rec: dict) -> str:
    """What a checkpoint is called: the name the user gave it, else the
    automatic label ("save", "revert to version ...", "merged N saves")."""
    return rec.get("name") or rec.get("label") or ""


def _dump(rec: dict) -> str:
    return VERSION_LOG_MARKER + json.dumps(rec, separators=(",", ":"))


def _rewrite(lines, fn):
    """Apply ``fn(rec) -> rec | None`` to every version record among
    ``lines`` (None drops it). Returns (new_lines, n_touched); a record
    ``fn`` hands back unchanged keeps its original text."""
    out, touched = [], 0
    for line in lines:
        body = line.lstrip()
        if not body.startswith(VERSION_LOG_MARKER):
            out.append(line)
            continue
        try:
            rec = json.loads(body[len(VERSION_LOG_MARKER):])
        except ValueError:
            out.append(line)
            continue
        new = fn(dict(rec))
        if new is None:
            touched += 1
            continue
        if new != rec:
            touched += 1
            out.append(line[:len(line) - len(body)] + _dump(new))
        else:
            out.append(line)
    return out, touched


def _apply(session, fn) -> int:
    """Rewrite ``session``'s version records, on disk and pending alike."""
    from . import changelog as _changelog
    touched = 0
    path = getattr(session, "path", None)
    text = _changelog.read_text(path)
    if text:
        lines, n = _rewrite(text.split("\n"), fn)
        if n:
            # atomic either way: never a half-written history (inside an
            # .edc16 only its history block is replaced)
            _changelog.rewrite(path, "\n".join(lines))
            touched += n
    pending, n = _rewrite(list(session.change_log), fn)
    if n:
        session.change_log[:] = pending
        touched += n
    return touched


def set_label(session, version_id: str, name: str) -> None:
    """Give checkpoint ``version_id`` a name ("" clears it, back to its
    automatic label). A saved checkpoint is renamed in the log at once; a
    pending one is written with the next save."""
    name = (name or "").strip()

    def fn(rec):
        if rec.get("id") != version_id:
            return rec
        if name:
            rec["name"] = name
        else:
            rec.pop("name", None)
        return rec
    if version_id not in {v["id"] for v in list_versions(session)}:
        raise ValueError("no version %r found in this binary's history"
                         % version_id)
    _apply(session, fn)


def squash_versions(session, version_ids, name: str = "") -> dict:
    """Merge adjacent checkpoints into one, in the history only -- the
    binary is not touched.

    The merged checkpoint records the NET change of the run: every byte that
    differs between the state before the oldest of them and the state after
    the newest, old and new. That is exactly what reverting past or to it
    needs, so reverting still lands on the same bytes as before the merge
    (a change made and then undone within the run simply drops out). It
    keeps the newest one's id and time, remembers the ids and first time it
    absorbed, and takes ``name`` -- or, when none is given, the names the
    merged checkpoints had.

    Only adjacent checkpoints can merge (one in between would be relative to
    a state that no longer exists), and only saved ones: a pending
    checkpoint lives in memory until the next save.
    """
    ids = set(version_ids)
    versions = list_versions(session)          # newest first
    idx = [i for i, v in enumerate(versions) if v["id"] in ids]
    if len(idx) < 2:
        raise ValueError("Select at least two versions to merge.")
    if len(idx) != len(ids):
        raise ValueError("Some of those versions are not in this history.")
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise ValueError("Only versions next to each other can be merged -- "
                         "a version in between records its change relative "
                         "to the ones around it.")
    run = [versions[i] for i in idx]           # newest first
    if any(v.get("pending") for v in run):
        raise ValueError("Save first: an unsaved checkpoint is only in memory "
                         "until the file is saved.")
    newest, oldest = run[0], run[-1]
    after = bytes_at_version(session, newest["id"])
    before = bytearray(bytes_at_version(session, oldest["id"]))
    for addr, old_hex, _new_hex in oldest["ranges"]:
        old_b = bytes.fromhex(old_hex)
        before[addr:addr + len(old_b)] = old_b
    before = bytes(before)
    n = min(len(before), len(after))
    ranges = _byte_ranges(before, after, 0, n)
    merged_from = []
    for v in reversed(run):                    # oldest first
        merged_from.extend(v.get("merged_from") or [v["id"]])
    # the other regions merge the same way: net change of the run
    reg = {}
    names = {n for v in run for n in (v.get("regions") or {})}
    for name in sorted(names):
        base = session.region_saved(name)
        if base is None:
            continue
        def undo_to(vid_stop, name=name):
            buf = bytearray(base)
            for v in versions:
                if v["id"] == vid_stop:
                    break
                for addr, old_hex, _n in (v.get("regions") or {}).get(name, ()):
                    ob = bytes.fromhex(old_hex)
                    buf[addr:addr + len(ob)] = ob
            return buf
        r_after = bytes(undo_to(newest["id"]))
        r_before = undo_to(oldest["id"])
        for addr, old_hex, _n in (oldest.get("regions") or {}).get(name, ()):
            ob = bytes.fromhex(old_hex)
            r_before[addr:addr + len(ob)] = ob
        r = _ranges_of(bytes(r_before), r_after)
        if r:
            reg[name] = r
    rec = {"id": newest["id"],
           "ts": newest.get("ts", ""),
           "ts_from": oldest.get("ts_from") or oldest.get("ts", ""),
           "label": "merged %d saves" % len(merged_from),
           "merged_from": merged_from,
           "ranges": [[s, before[s:e].hex(), after[s:e].hex()]
                      for s, e in ranges],
           "n_bytes": sum(e - s for s, e in ranges)
           + sum(_span(r) for r in reg.values())}
    if reg:
        rec["regions"] = reg
    name = (name or "").strip() or "; ".join(
        v["name"] for v in reversed(run) if v.get("name"))
    if name:
        rec["name"] = name
    drop = {v["id"] for v in run[1:]}

    def fn(r):
        if r.get("id") == newest["id"]:
            return dict(rec)
        if r.get("id") in drop:
            return None
        return r
    _apply(session, fn)
    return rec
