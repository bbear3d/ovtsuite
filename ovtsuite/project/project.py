# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The in-memory ``.ovt`` project and every operation on it.

See docs/OVT_CONTAINER.md. A :class:`Project` holds the structural records
(:mod:`.model`) plus a path -> bytes blob store mirroring the archive. All
history is append-only: checkpoints are never modified, and the as-read root
of a set can never be deleted.
"""

from __future__ import annotations

import os
import posixpath
import threading
from collections import deque
from typing import Dict, Iterable, List, Optional, Tuple

from . import archive as _ar
from . import delta as _d
from . import disksync as _ds
from .model import (Attachment, BinarySet, Branch, Checkpoint, INFO_FIELDS,
                    INFO_LABELS, Member, Original, PLATFORM_SET_KIND, ROLE_BASE,
                    ROLE_NAMES, new_id, now)

Images = Dict[str, bytes]

FORMAT_VERSION = 1


class HistoryError(ValueError):
    """A history state cannot be reconstructed (damaged/missing data)."""


def _safe_name(filename: str) -> str:
    """A filename safe as the last component of an archive path."""
    base = os.path.basename(filename.replace("\\", "/")) or "file"
    return "".join(c if c not in '<>:"|?*\x00' else "_" for c in base)


def _app_string() -> str:
    try:
        from .. import branding
        return "%s %s" % (branding.APP_NAME, branding.__version__)
    except Exception:                                    # pragma: no cover
        return ""


class Project:
    """An ``.ovt`` project in memory."""

    def __init__(self, name: str = ""):
        self.name = name
        #: stable identity of the project across saves, copies and renames
        #: (the app keys per-user state -- which tabs were open -- on it)
        self.uid = new_id()
        self.created = now()
        self.modified = self.created
        self.app = _app_string()
        self.settings: Dict[str, object] = {}
        #: what the project is about -- make, model, chassis, VIN ...
        #: (:data:`.model.INFO_FIELDS`); strings, empty ones left out
        self.info: Dict[str, str] = {}
        self.sets: List[BinarySet] = []
        self.logs: List[Attachment] = []
        self.attachments: List[Attachment] = []
        #: patches kept with the project (``.patch`` JSON, ovtsuite.patch)
        self.patches: List[Attachment] = []
        self.extra: Dict[str, object] = {}
        self.path: Optional[str] = None
        self.report = _ar.LoadReport()
        self._blobs: Dict[str, bytes] = {}
        #: each blob as last stored in the zip: a save copies the unchanged
        #: ones instead of hashing and compressing them again
        self._zcache: _ar.PackCache = {}
        self._damaged: set = set()
        self._states: Dict[Tuple[str, str], Images] = {}
        self._dirty = False
        #: the file as it was when loaded / last saved (:mod:`.disksync`)
        self._disk: Optional[_ds.DiskState] = None
        #: held while the file is written (a save, or :mod:`.cachewrite`
        #: writing definition caches on another thread)
        self._io_lock = threading.RLock()
        #: bumped on every structural change (crash recovery uses it to
        #: decide when the project itself needs re-snapshotting)
        self.revision = 0

    # ================================================================ paths
    @staticmethod
    def _img_path(sid, cid, mid):
        return "sets/%s/images/%s/%s.bin" % (sid, cid, mid)

    @staticmethod
    def _delta_path(sid, cid):
        return "sets/%s/deltas/%s.ovd" % (sid, cid)

    @staticmethod
    def _orig_path(sid, oid, filename):
        return "sets/%s/originals/%s/%s" % (sid, oid, _safe_name(filename))

    @staticmethod
    def _cache_path(sid, bid):
        return "sets/%s/cache/%s.defn" % (sid, bid)

    @property
    def modified_since_save(self) -> bool:
        return self._dirty

    def _touch(self):
        self._dirty = True
        self.revision += 1
        self.modified = now()

    # ================================================================= sets
    def set(self, sid_or_name: str) -> BinarySet:
        for s in self.sets:
            if s.id == sid_or_name:
                return s
        for s in self.sets:
            if s.name.lower() == (sid_or_name or "").lower():
                return s
        raise KeyError("no binary set %r" % sid_or_name)

    def add_set(self, name: str, platform: str = "unknown",
                kind: Optional[str] = None,
                identity: Optional[dict] = None) -> BinarySet:
        name = self._unique_set_name(name or "Binary")
        s = BinarySet(id=new_id(), name=name, platform=platform or "unknown",
                      kind=kind or PLATFORM_SET_KIND.get(platform, "other"),
                      identity=dict(identity or {}))
        self.sets.append(s)
        self._touch()
        return s

    def _unique_set_name(self, name):
        taken = {s.name.lower() for s in self.sets}
        if name.lower() not in taken:
            return name
        i = 2
        while ("%s %d" % (name, i)).lower() in taken:
            i += 1
        return "%s %d" % (name, i)

    def rename_set(self, sid: str, name: str) -> None:
        s = self.set(sid)
        if name and name != s.name:
            s.name = self._unique_set_name(name)
            self._touch()

    def remove_set(self, sid: str) -> None:
        s = self.set(sid)
        self.sets.remove(s)
        prefix = "sets/%s/" % s.id
        for p in [p for p in self._blobs if p.startswith(prefix)]:
            del self._blobs[p]
        for lst in (self.logs, self.attachments, self.patches):
            for a in lst:
                if a.set == s.id:
                    a.set = None
        self._states = {k: v for k, v in self._states.items() if k[0] != s.id}
        self._touch()

    def add_member(self, sid: str, member: Member) -> Member:
        s = self.set(sid)
        if s.member(member.id) is not None:
            raise ValueError("set %s already has a member %r" % (s.name, member.id))
        if not member.name:
            member.name = ROLE_NAMES.get(member.role, member.id)
        if not member.base and member.role in ROLE_BASE:
            member.base = ROLE_BASE[member.role]
        s.members.append(member)
        self._touch()
        return member

    def update_member(self, sid: str, mid: str, **fields) -> Member:
        """Change a member's structural description (name, base, endian,
        word, platform, module, meta)."""
        m = self.set(sid).member(mid)
        if m is None:
            raise KeyError(mid)
        for k, v in fields.items():
            if k in ("id",) or not hasattr(m, k):
                raise AttributeError(k)
            setattr(m, k, v)
        self._touch()
        return m

    def adopt_platform(self, sid: str, platform: str) -> BinarySet:
        """A set stored as ``unknown`` (imported by an app that had no
        detector for it) that the base app now identifies as ``platform``:
        describe it as an import would today -- the set's platform and kind,
        its unidentified members', and a ``raw`` main image becomes ``flash``.

        It is a correction, not an edit, so the project is NOT marked
        modified: it is written with the next save, and until then every
        open simply identifies the set again."""
        from .importers import _endian, _module_ref
        from .model import PLATFORM_SET_KIND
        s = self.set(sid)
        old = s.platform
        s.platform = platform
        if s.kind == PLATFORM_SET_KIND.get(old, "other"):
            s.kind = PLATFORM_SET_KIND.get(platform, s.kind)
        for m in s.members:
            if m.platform in ("", "unknown", old):
                m.platform = platform
                m.module = _module_ref(platform)
                m.endian = _endian(platform)
        raw = next((m for m in s.members if m.role == "raw"), None)
        if raw is not None and not any(m.role == "flash" for m in s.members):
            raw.role = "flash"
        return s

    # ============================================================== history
    def _check_images(self, s: BinarySet, images: Images):
        for mid in images:
            if s.member(mid) is None:
                raise KeyError("set %s has no member %r (add it first)"
                               % (s.name, mid))

    def _store_images(self, s: BinarySet, cid: str, images: Images):
        for mid, data in images.items():
            self._blobs[self._img_path(s.id, cid, mid)] = bytes(data)

    def add_root(self, sid: str, images: Images, *, kind: str = "import",
                 label: str = "", source: Optional[dict] = None,
                 branch: str = "Stock") -> Tuple[Checkpoint, Branch]:
        """A new independent root (full image stored) and a branch on it.

        The first root of a set is normally the as-read original. A later
        root is e.g. a stock file imported next to a read of a tuned car."""
        s = self.set(sid)
        self._check_images(s, images)
        c = Checkpoint(id=new_id(), parent=None, kind=kind, ts=now(),
                       label=label or ("Read" if kind == "read" else "Imported"),
                       source=dict(source or {}),
                       sizes={m: len(b) for m, b in images.items()})
        s.checkpoints.append(c)
        self._store_images(s, c.id, images)
        self._states[(s.id, c.id)] = {k: bytes(v) for k, v in images.items()}
        b = self._new_branch(s, branch, c.id)
        if s.active_branch is None:
            s.active_branch = b.id
        self._touch()
        return c, b

    def _new_branch(self, s: BinarySet, name: str, head: str) -> Branch:
        name = name or "Branch"
        taken = {b.name.lower() for b in s.branches}
        base, i = name, 2
        while name.lower() in taken:
            name = "%s %d" % (base, i)
            i += 1
        b = Branch(id=new_id(), name=name, head=head, created=now())
        s.branches.append(b)
        return b

    def head(self, sid: str, branch: Optional[str] = None) -> Checkpoint:
        s = self.set(sid)
        return s.checkpoint(self._branch(s, branch).head)

    def _branch(self, s: BinarySet, branch: Optional[str]) -> Branch:
        b = s.branch(branch) if branch else s.active
        if b is None:
            raise KeyError("set %s has no branch %r" % (s.name, branch))
        return b

    def _write_checkpoint(self, s: BinarySet, b: Branch, new: Images, *,
                          label: str, kind: str, source: Optional[dict],
                          log=None) -> Optional[Checkpoint]:
        """Make ``new`` (the complete member set) the head of ``b``."""
        parent = self.state(s.id, b.head)
        recs = _d.diff(parent, new)
        if not recs:
            return None
        c = Checkpoint(id=new_id(), parent=b.head, kind=kind, ts=now(),
                       label=label or kind, source=dict(source or {}),
                       sizes={m: len(v) for m, v in new.items()},
                       n_bytes=_d.changed_bytes(recs), log=list(log or []))
        self._blobs[self._delta_path(s.id, c.id)] = _d.encode(recs)
        s.checkpoints.append(c)
        b.head = c.id
        self._states[(s.id, c.id)] = dict(new)
        self._touch()
        return c

    def commit(self, sid: str, branch: Optional[str], images: Images, *,
               label: str = "", kind: str = "save",
               source: Optional[dict] = None, log=None) -> Optional[Checkpoint]:
        """Record ``images`` as the new head of ``branch`` (default: the
        active branch). Members left out of ``images`` keep their current
        content. Returns None when nothing changed."""
        s = self.set(sid)
        b = self._branch(s, branch)
        self._check_images(s, images)
        new = self.state(s.id, b.head)
        new.update({k: bytes(v) for k, v in images.items()})
        return self._write_checkpoint(s, b, new, label=label, kind=kind,
                                      source=source, log=log)

    def remove_member_from_branch(self, sid: str, branch: Optional[str],
                                  mid: str, label: str = "") -> Optional[Checkpoint]:
        """A checkpoint in which member ``mid`` no longer exists."""
        s = self.set(sid)
        b = self._branch(s, branch)
        new = {k: v for k, v in self.state(s.id, b.head).items() if k != mid}
        return self._write_checkpoint(s, b, new, kind="save", source=None,
                                      label=label or "removed %s" % mid)

    def revert(self, sid: str, branch: Optional[str], to: str,
               label: str = "") -> Optional[Checkpoint]:
        """A new checkpoint on ``branch`` whose content equals checkpoint
        ``to`` exactly (history stays append-only)."""
        s = self.set(sid)
        b = self._branch(s, branch)
        return self._write_checkpoint(
            s, b, self.state(s.id, to), kind="revert", source={"to": to},
            label=label or "revert to %s" % self._ck_label(s, to))

    def merge_checkpoints(self, sid: str, cids, label: str = "") -> Checkpoint:
        """Merge adjacent saved versions into one: ``cids`` must be a run of
        consecutive checkpoints on one line of history (none of them a root
        -- the original stays as it is). The newest keeps its id (branch
        heads, comparisons and flash records that name it stay valid) and
        now holds the net change from the oldest one's parent; the others
        go. Refused (ValueError) when a checkpoint inside the run is a
        branch head, has another branch forking from it, or is what an
        original file or a flash record refers to: removing it would lose
        a version something else points at."""
        s = self.set(sid)
        picked = []
        for cid in cids:
            c = s.checkpoint(cid)
            if c is None:
                raise KeyError(cid)
            if c not in picked:
                picked.append(c)
        if len(picked) < 2:
            raise ValueError("Pick at least two versions to merge.")
        if any(c.parent is None for c in picked):
            raise ValueError("The original (the first version) cannot be "
                             "merged with later ones.")
        ids = {c.id for c in picked}
        # the newest: the one no other picked version descends from
        newest = next((c for c in picked
                       if not any(o.parent == c.id and o.id in ids
                                  for o in picked)), None)
        chain = []
        for c in s.lineage(newest.id):
            if c.id not in ids:
                break
            chain.append(c)
        if len(chain) != len(picked):
            raise ValueError("Only versions that follow each other on one "
                             "branch can be merged.")
        chain.reverse()                          # oldest first
        inner = chain[:-1]
        heads = {b.head: b.name for b in s.branches}
        refs = {o.checkpoint for o in s.originals}
        flashed = [r for r in [s.extra.get("flashed")] +
                   list(s.extra.get("flash_log") or []) if isinstance(r, dict)]
        refs |= {r.get("checkpoint") for r in flashed}
        for c in inner:
            if c.id in heads:
                raise ValueError("\"%s\" is the latest version of branch %s; "
                                 "it cannot be merged into a later one."
                                 % (c.label or c.kind, heads[c.id]))
            kids = s.children(c.id)
            if len(kids) != 1:
                raise ValueError("Another branch starts at \"%s\"; it cannot "
                                 "be merged away." % (c.label or c.kind))
            if c.id in refs:
                raise ValueError("\"%s\" is the version an original file or a "
                                 "flash record refers to; it cannot be merged "
                                 "away." % (c.label or c.kind))
        base = self.state(s.id, chain[0].parent)
        top = self.state(s.id, newest.id)
        recs = _d.diff(base, top)
        self._blobs[self._delta_path(s.id, newest.id)] = _d.encode(recs)
        log = []
        for c in chain:
            log.extend(c.log)
        merged_from = [c.id for c in inner] + list(
            newest.extra.get("merged_from") or [])
        newest.parent = chain[0].parent
        newest.n_bytes = _d.changed_bytes(recs)
        newest.log = log
        newest.kind = "merge"
        newest.extra["merged_from"] = merged_from
        newest.extra.setdefault("ts_from", chain[0].ts)
        if label:
            newest.label = label
        for c in inner:
            s.checkpoints.remove(c)
            self._blobs.pop(self._delta_path(s.id, c.id), None)
            self._states.pop((s.id, c.id), None)
        self._states[(s.id, newest.id)] = top
        self._touch()
        return newest

    def set_checkpoint_label(self, sid: str, cid: str, label: str) -> None:
        """Name a checkpoint (its content never changes; its name may)."""
        c = self.set(sid).checkpoint(cid)
        if c is None:
            raise KeyError(cid)
        if label and label != c.label:
            c.label = label
            self._touch()

    def delta_records(self, sid: str, cid: str):
        """The decoded delta of checkpoint ``cid`` (None for a root, or one
        whose delta is damaged)."""
        s = self.set(sid)
        if s.checkpoint(cid) is None or s.checkpoint(cid).parent is None:
            return None
        return self._delta_of(s, cid)

    @staticmethod
    def _ck_label(s, cid):
        c = s.checkpoint(cid)
        return "%s (%s)" % (c.label, c.ts) if c else cid

    def fork(self, sid: str, name: str, at: Optional[str] = None) -> Branch:
        """A new branch whose head is checkpoint ``at`` (default: the active
        branch's head). No checkpoint is created."""
        s = self.set(sid)
        if at is None:
            at = s.active.head
        if s.checkpoint(at) is None:
            raise KeyError("no checkpoint %r" % at)
        self.state(s.id, at)               # refuse a state that can't be rebuilt
        # the definition cache of the branch it comes from goes with it, so
        # the new branch opens without matching (its own save replaces it)
        src = next((o for o in s.branches if o.head == at
                    and o.id == s.active_branch), None) or next(
            (o for o in s.branches if o.head == at), s.active)
        defn = self.best_cache(s.id, src.id) if src is not None else None
        b = self._new_branch(s, name, at)
        if defn is not None:
            self.set_cache(s.id, b.id, defn)
        self._touch()
        return b

    def rename_branch(self, sid: str, branch: str, name: str) -> None:
        s = self.set(sid)
        b = s.branch(branch)
        if b is None:
            raise KeyError(branch)
        if name and name.lower() != b.name.lower():
            if any(o.name.lower() == name.lower() for o in s.branches):
                raise ValueError("there is already a branch called %r" % name)
            b.name = name
            self._touch()

    def delete_branch(self, sid: str, branch: str) -> None:
        """Remove a branch pointer. Its checkpoints stay in the history (and
        every root, including the as-read original, stays forever)."""
        s = self.set(sid)
        b = s.branch(branch)
        if b is None:
            raise KeyError(branch)
        if len(s.branches) == 1:
            raise ValueError("a set needs at least one branch")
        s.branches.remove(b)
        self._blobs.pop(self._cache_path(s.id, b.id), None)
        if s.active_branch == b.id:
            s.active_branch = s.branches[0].id
        self._touch()

    def set_active(self, sid: str, branch: str) -> Branch:
        s = self.set(sid)
        b = s.branch(branch)
        if b is None:
            raise KeyError(branch)
        if s.active_branch != b.id:
            s.active_branch = b.id
            self._touch()
        return b

    def _image_points(self, s: BinarySet) -> set:
        """Where full images are kept: the roots only (spec 2.2)."""
        return {c.id for c in s.roots()}

    # ------------------------------------------------------- reconstruction
    def _image_at(self, s: BinarySet, c: Checkpoint) -> Optional[Images]:
        out = {}
        for mid in c.sizes:
            p = self._img_path(s.id, c.id, mid)
            data = self._blobs.get(p)
            if data is None or p in self._damaged or len(data) != c.sizes[mid]:
                return None
            out[mid] = data
        return out

    def _delta_of(self, s: BinarySet, cid: str):
        p = self._delta_path(s.id, cid)
        data = self._blobs.get(p)
        if data is None or p in self._damaged:
            return None
        try:
            return _d.decode(data)
        except _d.DeltaError:
            return None

    def state(self, sid: str, cid: str) -> Images:
        """Every member's bytes at checkpoint ``cid`` (spec 4.2)."""
        s = self.set(sid)
        key = (s.id, cid)
        if key in self._states:
            return dict(self._states[key])
        if s.checkpoint(cid) is None:
            raise KeyError("no checkpoint %r in set %s" % (cid, s.name))
        bad_edges: set = set()
        while True:
            path = self._find_path(s, cid, bad_edges)
            if path is None:
                raise HistoryError(
                    "the state '%s' of %s can't be rebuilt -- the data it needs"
                    " is damaged or missing" % (self._ck_label(s, cid), s.name))
            src, steps = path
            images = self._states.get((s.id, src)) or \
                self._image_at(s, s.checkpoint(src))
            try:
                for frm, to in steps:
                    tc = s.checkpoint(to)
                    if tc.parent == frm:                 # parent -> child
                        images = _d.apply_forward(images, self._delta_of(s, to))
                    else:                                # child -> parent
                        images = _d.apply_backward(images, self._delta_of(s, frm))
                    self._states.setdefault((s.id, to), dict(images))
                return dict(images)
            except _d.DeltaError:
                # the path crossed a delta that does not fit: try another
                bad_edges.add(frozenset((frm, to)))

    def _find_path(self, s: BinarySet, target: str, bad_edges: set):
        """Nearest checkpoint (by edges) with an intact full image, and the
        steps from it to ``target``; None when none is reachable."""
        def usable(a, b):
            if frozenset((a, b)) in bad_edges:
                return False
            child = b if s.checkpoint(b).parent == a else a
            return self._delta_of(s, child) is not None

        prev = {target: None}
        q = deque([target])
        while q:
            cur = q.popleft()
            c = s.checkpoint(cur)
            if (s.id, cur) in self._states or self._image_at(s, c) is not None:
                steps = []
                n = cur
                while prev[n] is not None:
                    steps.append((n, prev[n]))
                    n = prev[n]
                return cur, steps
            nbrs = [x.id for x in s.children(cur)]
            if c.parent:
                nbrs.append(c.parent)
            for nb in nbrs:
                if nb not in prev and usable(cur, nb):
                    prev[nb] = cur
                    q.append(nb)
        return None

    def head_state(self, sid: str, branch: Optional[str] = None) -> Images:
        return self.state(sid, self.head(sid, branch).id)

    def member_bytes(self, sid: str, mid: str, *, branch: Optional[str] = None,
                     checkpoint: Optional[str] = None) -> Optional[bytes]:
        cid = checkpoint or self.head(sid, branch).id
        return self.state(sid, cid).get(mid)

    def unreadable_checkpoints(self, sid: str) -> List[str]:
        """Checkpoints whose state can't be rebuilt from intact data."""
        s = self.set(sid)
        bad = []
        for c in s.checkpoints:
            try:
                self.state(s.id, c.id)
            except HistoryError:
                bad.append(c.id)
        return bad

    # ============================================================ originals
    def add_original(self, sid: str, filename: str, fmt: str, data: bytes, *,
                     checkpoint: Optional[str] = None,
                     members: Iterable[str] = ()) -> Original:
        s = self.set(sid)
        o = Original(id=new_id(), filename=_safe_name(filename), format=fmt,
                     checkpoint=checkpoint, members=list(members))
        s.originals.append(o)
        self._blobs[self._orig_path(s.id, o.id, o.filename)] = bytes(data)
        self._touch()
        return o

    def original_bytes(self, sid: str, oid: str) -> bytes:
        s = self.set(sid)
        o = s.original(oid)
        if o is None:
            raise KeyError(oid)
        return self._blobs[self._orig_path(s.id, o.id, o.filename)]

    # ========================================================== definitions
    def add_definition(self, sid: str, mid: str, filename: str, data: bytes,
                       type_: Optional[str] = None,
                       instead_of_module: bool = False) -> Member:
        """Package a third-party definition (XDF / A2L / KP / OLS / DAM) with
        a member. A raw member with a definition opens in the main view; a
        member a module defines keeps the module's definition unless
        ``instead_of_module`` (``definition["use"] == "instead"``)."""
        s = self.set(sid)
        m = s.member(mid)
        if m is None:
            raise KeyError(mid)
        name = _safe_name(filename)
        path = "sets/%s/defs/%s/%s" % (s.id, m.id, name)
        if m.definition and m.definition.get("path") != path:
            self._blobs.pop(m.definition.get("path"), None)
        self._blobs[path] = bytes(data)
        m.definition = {"path": path,
                        "type": (type_ or posixpath.splitext(name)[1][1:]).lower()}
        if instead_of_module:
            m.definition["use"] = "instead"
        self._touch()
        return m

    def definition_bytes(self, sid: str, mid: str) -> Optional[bytes]:
        m = self.set(sid).member(mid)
        if m is None or not m.definition:
            return None
        return self._blobs.get(m.definition.get("path", ""))

    def remove_definition(self, sid: str, mid: str) -> None:
        m = self.set(sid).member(mid)
        if m is not None and m.definition:
            self._blobs.pop(m.definition.get("path", ""), None)
            m.definition = None
            self._touch()

    # ================================================== definition cache
    def set_cache(self, sid: str, branch: str, data: Optional[bytes]) -> None:
        """Advisory definition snapshot for a branch (``ovtsuite.defn``)."""
        s = self.set(sid)
        b = s.branch(branch)
        p = self._cache_path(s.id, b.id)
        if data is None:
            self._blobs.pop(p, None)
        else:
            self._blobs[p] = bytes(data)

    def cache(self, sid: str, branch: str) -> Optional[bytes]:
        s = self.set(sid)
        b = s.branch(branch)
        p = self._cache_path(s.id, b.id)
        return None if p in self._damaged else self._blobs.get(p)

    def cache_candidates(self, sid: str, branch: str) -> List[bytes]:
        """Definition caches that may serve ``branch``: its own first, then
        the other branches' of the same set, the most recently saved head
        first. Every branch of a set is the same software, so another
        branch's snapshot usually fits; ``ovtsuite.defn``'s checks (image
        structure included) still decide whether it is used."""
        s = self.set(sid)
        b = s.branch(branch)
        own = self.cache(s.id, b.id)
        out = [own] if own is not None else []

        def ts(o):
            c = s.checkpoint(o.head)
            return c.ts if c is not None else ""
        for o in sorted((o for o in s.branches if o.id != b.id),
                        key=ts, reverse=True):
            d = self.cache(s.id, o.id)
            if d is not None and d not in out:
                out.append(d)
        return out

    def best_cache(self, sid: str, branch: str) -> Optional[bytes]:
        """The branch's own definition cache, else a sibling branch's
        (:meth:`cache_candidates`), else None."""
        c = self.cache_candidates(sid, branch)
        return c[0] if c else None

    # =================================================== logs, attachments
    @property
    def max_logs(self) -> int:
        try:
            return max(0, int(self.settings.get("max_logs", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @max_logs.setter
    def max_logs(self, n: int) -> None:
        self.settings["max_logs"] = max(0, int(n or 0))
        self.enforce_log_cap()
        self._touch()

    def _att_path(self, kind: str, a: Attachment) -> str:
        return "%s/%s/%s" % (kind, a.id, _safe_name(a.filename))

    def _add_att(self, kind, lst, filename, data, set_id, note, pinned=False):
        if set_id is not None:
            set_id = self.set(set_id).id
        a = Attachment(id=new_id(), filename=_safe_name(filename), added=now(),
                       set=set_id, pinned=pinned, note=note)
        lst.append(a)
        self._blobs[self._att_path(kind, a)] = bytes(data)
        self._touch()
        return a

    def add_log(self, filename: str, data: bytes, *, set_id: Optional[str] = None,
                note: str = "", pinned: bool = False) -> Attachment:
        a = self._add_att("logs", self.logs, filename, data, set_id, note, pinned)
        self.enforce_log_cap()
        return a

    def enforce_log_cap(self) -> List[Attachment]:
        """Delete the oldest unpinned logs beyond ``max_logs``; returns them.
        Pinned logs are never deleted and do not count toward the cap."""
        cap = self.max_logs
        if not cap:
            return []
        unpinned = [a for a in self.logs if not a.pinned]
        drop = unpinned[:max(0, len(unpinned) - cap)]       # list order = age
        for a in drop:
            self._remove_att("logs", self.logs, a.id)
        return drop

    def pin_log(self, lid: str, pinned: bool = True) -> None:
        a = next(a for a in self.logs if a.id == lid)
        if a.pinned != pinned:
            a.pinned = pinned
            self._touch()
            if not pinned:
                self.enforce_log_cap()

    def _remove_att(self, kind, lst, aid):
        a = next((a for a in lst if a.id == aid), None)
        if a is None:
            raise KeyError(aid)
        lst.remove(a)
        self._blobs.pop(self._att_path(kind, a), None)
        self._touch()

    def remove_log(self, lid: str) -> None:
        self._remove_att("logs", self.logs, lid)

    def log_bytes(self, lid: str) -> bytes:
        a = next(a for a in self.logs if a.id == lid)
        return self._blobs[self._att_path("logs", a)]

    def add_attachment(self, filename: str, data: bytes, *,
                       set_id: Optional[str] = None, note: str = "") -> Attachment:
        return self._add_att("attachments", self.attachments, filename, data,
                             set_id, note)

    def remove_attachment(self, aid: str) -> None:
        self._remove_att("attachments", self.attachments, aid)

    def attachment_bytes(self, aid: str) -> bytes:
        a = next(a for a in self.attachments if a.id == aid)
        return self._blobs[self._att_path("attachments", a)]

    # ======================================================== flashing
    def mark_flashed(self, sid: str, bid: Optional[str], cid: Optional[str], *,
                     unsaved: bool = False, tool: str = "",
                     sha1: str = "") -> dict:
        """Record that set ``sid`` was written to a control unit: from branch
        ``bid`` at version ``cid`` (None when the tab held unsaved edits --
        ``unsaved``). The last write is ``set.extra["flashed"]``; the ten
        before it are kept in ``set.extra["flash_log"]``."""
        s = self.set(sid)
        rec = {"ts": now(), "branch": bid, "checkpoint": cid,
               "unsaved": bool(unsaved), "tool": tool, "sha1": sha1}
        old = s.extra.get("flashed")
        if old:
            s.extra["flash_log"] = ([old] + list(s.extra.get("flash_log") or
                                                 []))[:10]
        s.extra["flashed"] = rec
        self._touch()
        return rec

    def last_flashed(self, sid: str) -> Optional[dict]:
        rec = self.set(sid).extra.get("flashed")
        return dict(rec) if isinstance(rec, dict) else None

    def add_patch(self, filename: str, data: bytes, *,
                  set_id: Optional[str] = None, note: str = "") -> Attachment:
        return self._add_att("patches", self.patches, filename, data,
                             set_id, note)

    def remove_patch(self, pid: str) -> None:
        self._remove_att("patches", self.patches, pid)

    def patch_bytes(self, pid: str) -> bytes:
        a = next(a for a in self.patches if a.id == pid)
        return self._blobs[self._att_path("patches", a)]

    # ============================================================ info
    def set_info(self, info: Dict[str, str], *, replace: bool = False) -> bool:
        """Update the project information (``replace``: the whole of it).
        Values are stripped strings; an empty one removes its key. True when
        anything changed."""
        new = {} if replace else dict(self.info)
        for k, v in (info or {}).items():
            k = str(k).strip()
            v = "" if v is None else str(v).strip()
            if not k:
                continue
            if v:
                new[k] = v
            else:
                new.pop(k, None)
        if new == self.info:
            return False
        self.info = new
        self._touch()
        return True

    def info_rows(self) -> List[Tuple[str, str]]:
        """``[(label, value)]`` of the filled-in information, known fields in
        their usual order, then any others under their own names."""
        known = [k for k, _l, _h in INFO_FIELDS]
        rows = [(INFO_LABELS[k], self.info[k]) for k in known if self.info.get(k)]
        rows += [(k, v) for k, v in self.info.items() if k not in INFO_LABELS and v]
        return rows

    def vehicle(self) -> str:
        """A one-line description from the information ("Volkswagen Golf
        (1K) 2006"), or empty."""
        i = self.info
        parts = [i.get("make", ""), i.get("model", "")]
        if i.get("chassis"):
            parts.append("(%s)" % i["chassis"])
        parts.append(i.get("year", ""))
        return " ".join(p for p in parts if p)

    # ======================================================== serialising
    def manifest(self) -> dict:
        out = dict(self.extra)
        out.update({
            "format": "ovt-project",
            "version": FORMAT_VERSION,
            "id": self.uid,
            "requires": sorted(_ar.FEATURES),
            "app": _app_string(),
            "name": self.name,
            "created": self.created,
            "modified": self.modified,
            "settings": dict(self.settings),
            "info": dict(self.info),
            "sets": [s.to_dict() for s in self.sets],
            "logs": [a.to_dict() for a in self.logs],
            "attachments": [a.to_dict() for a in self.attachments],
        })
        if self.patches:
            out["patches"] = [a.to_dict() for a in self.patches]
        else:
            out.pop("patches", None)
        damaged = sorted(p for p in self._damaged if p in self._blobs)
        if damaged:
            out["damaged"] = damaged
        else:
            out.pop("damaged", None)
        return out

    def snapshot_parts(self) -> Tuple[dict, Dict[str, bytes]]:
        """``(manifest, blobs)`` frozen now, for writing on another thread
        (crash recovery): the blob dict is copied, its bytes are immutable."""
        self._ensure_point_images()
        return self.manifest(), dict(self._blobs)

    def _ensure_point_images(self):
        """A full image at every root -- rebuilt (from a full image elsewhere
        in the tree) only when one is missing. A damaged root image is left in
        place and stays flagged: there is nothing to rebuild it from."""
        for s in self.sets:
            for cid in self._image_points(s):
                c = s.checkpoint(cid)
                if c is None or self._image_at(s, c) is not None:
                    continue
                if any(self._img_path(s.id, cid, m) in self._damaged
                       for m in c.sizes):
                    continue
                try:
                    self._store_images(s, cid, self.state(s.id, cid))
                except HistoryError:
                    pass

    def to_bytes(self, *, sign_key: Optional[str] = None, signer: str = "") -> bytes:
        # A damaged entry is carried as-is but stays listed as damaged, so
        # re-saving never launders corruption into a "good" hash.
        self._ensure_point_images()        # a full image at every root
        return _ar.write(self.manifest(), self._blobs, sign_key=sign_key,
                         signer=signer, cache=self._zcache)

    def save(self, path: Optional[str] = None, *, sign_key: Optional[str] = None,
             signer: str = "", force: bool = False) -> str:
        """Write the project. Over the file it was loaded from or last saved
        to, only while that file is unchanged: if another program changed
        or removed it, :class:`~.disksync.DiskChanged` (nothing written)
        unless ``force``."""
        job = self.prepare_save(path, sign_key=sign_key, signer=signer,
                                force=force)
        job.run()
        return job.finish()

    def prepare_save(self, path: Optional[str] = None, *,
                     sign_key: Optional[str] = None, signer: str = "",
                     force: bool = False) -> "SaveJob":
        """:meth:`save` in three steps, so the slow middle one can run on
        another thread: this one freezes what is written (on the thread that
        owns the project), :meth:`SaveJob.run` writes it (any thread; the
        project must not change meanwhile), :meth:`SaveJob.finish` (back on
        the owning thread) records the save."""
        path = path or self.path
        if not path:
            raise ValueError("no path to save the project to")
        if not path.lower().endswith(_ar.EXT):
            path += _ar.EXT
        self._ensure_point_images()        # a full image at every root
        return SaveJob(self, path, self.manifest(), dict(self._blobs),
                       sign_key, signer, force)

    def changed_on_disk(self, quick: bool = False) -> Optional[str]:
        """None, "changed" or "deleted": the file this project was loaded
        from or last saved to, as it is now. ``quick``: size and time only."""
        return _ds.changed_on_disk(self, quick)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Project":
        cache: _ar.PackCache = {}
        manifest, blobs, report = _ar.read(data, cache=cache)
        p = cls.from_manifest(manifest, blobs, report)
        p._zcache = cache
        return p

    @classmethod
    def from_manifest(cls, manifest: dict, blobs: Dict[str, bytes],
                      report: Optional[_ar.LoadReport] = None) -> "Project":
        report = report if report is not None else _ar.LoadReport()
        p = cls.__new__(cls)
        Project.__init__(p)
        known = {"format", "version", "id", "requires", "app", "name", "created",
                 "modified", "settings", "info", "sets", "logs", "attachments",
                 "patches", "entries", "damaged"}
        p.extra = {k: v for k, v in manifest.items() if k not in known}
        p.name = str(manifest.get("name", ""))
        p.uid = str(manifest.get("id") or p.uid)
        p.created = str(manifest.get("created", ""))
        p.modified = str(manifest.get("modified", ""))
        p.app = str(manifest.get("app", ""))
        p.settings = dict(manifest.get("settings") or {})
        info = manifest.get("info")
        p.info = {str(k): str(v) for k, v in info.items()
                  if isinstance(v, (str, int, float)) and str(v)} \
            if isinstance(info, dict) else {}
        p.sets = [BinarySet.from_dict(d) for d in manifest.get("sets") or []]
        p.logs = [Attachment.from_dict(d) for d in manifest.get("logs") or []]
        p.attachments = [Attachment.from_dict(d)
                         for d in manifest.get("attachments") or []]
        p.patches = [Attachment.from_dict(d)
                     for d in manifest.get("patches") or []]
        p._blobs = blobs
        p._damaged = set(report.damaged) | set(manifest.get("damaged") or [])
        report.damaged = sorted(p._damaged)
        p.report = report
        return p

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "rb") as fh:
            data = fh.read()
        p = cls.from_bytes(data)
        p.path = path
        p._disk = _ds.DiskState.of(p, path, data)
        return p

    # ============================================================ summary
    def summary(self) -> str:
        rows = ["%s  (%d set%s)" % (self.name or "Untitled project", len(self.sets),
                                    "" if len(self.sets) == 1 else "s")]
        for label, value in self.info_rows():
            rows.append("  %s: %s" % (label, value.replace("\n", " / ")))
        for s in self.sets:
            rows.append("  [%s] %s  platform=%s" % (s.kind.upper(), s.name, s.platform))
            for m in s.members:
                size = None
                try:
                    size = len(self.member_bytes(s.id, m.id) or b"")
                except HistoryError:
                    pass
                rows.append("    %-8s %-22s @0x%06X  %s bytes%s" % (
                    m.role, m.label, m.base, size if size is not None else "?",
                    "  def=" + m.definition["type"] if m.definition else ""))
            for b in s.branches:
                star = "*" if s.active and b.id == s.active.id else " "
                depth = len(s.lineage(b.head))
                rows.append("   %sbranch %-16s %d checkpoint%s" % (
                    star, b.name, depth, "" if depth == 1 else "s"))
            for o in s.originals:
                rows.append("    original %s (%s)" % (o.filename, o.format))
        if self.logs:
            rows.append("  logs: %d%s" % (len(self.logs),
                        " (cap %d)" % self.max_logs if self.max_logs else ""))
        if self.attachments:
            rows.append("  attachments: %d" % len(self.attachments))
        return "\n".join(rows)


class SaveJob:
    """A :meth:`Project.save` in progress (:meth:`Project.prepare_save`)."""

    def __init__(self, project, path, manifest, blobs, sign_key, signer, force):
        self.project = project
        self.path = path
        self.manifest = manifest
        self.blobs = blobs
        self.sign_key = sign_key
        self.signer = signer
        self.force = force
        self.revision = project.revision
        self._done = None

    def run(self) -> None:
        """Serialise, check the file on disk, write (fsync'd, atomic). Raises
        :class:`~.disksync.DiskChanged` (nothing written) unless forced.
        Touches nothing of the project but its pack cache and disk record."""
        p = self.project
        data = _ar.write(self.manifest, self.blobs, sign_key=self.sign_key,
                         signer=self.signer, cache=p._zcache)
        with p._io_lock:
            if not self.force:
                _ds.check(p, self.path)    # as late as possible before writing
            _ar.atomic_write(self.path, data)
            self._done = _ds.DiskState.after_write(self.path, data,
                                                   self.manifest, self.blobs)

    def finish(self) -> str:
        """Record the save in the project (its owning thread)."""
        if self._done is None:
            raise RuntimeError("the save was not written")
        p = self.project
        p.path = self.path
        if p.revision == self.revision:
            p._dirty = False
        p._disk = self._done
        return self.path
