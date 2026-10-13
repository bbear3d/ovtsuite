# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.ovt`` on disk vs the project in memory: noticing that another
program changed the file, and merging its changes with ours.

* A :class:`DiskState` is kept with every project that was loaded from or
  saved to a file: the file's size, time and sha256, plus the manifest and
  blobs as they were then -- the *base* of a three-way merge.
* :meth:`Project.save <ovtsuite.project.project.Project.save>` refuses
  (:class:`DiskChanged`) to write over a file that is no longer the one the
  project was loaded from or last saved to, unless told to (``force``).
* :func:`merge` combines the project in memory ("mine") with the file as it
  is now ("theirs") record by record: every record (set, member, checkpoint,
  branch, original, log, attachment, patch) has a stable id, checkpoints are
  append-only, and each blob belongs to one record. What only one side
  changed is taken from that side; a field both changed differently is a
  :class:`Conflict` the caller resolves (``choices``). A branch both moved
  keeps the other program's head, and ours goes on a new "<name> (mine)"
  branch.

No Qt here: the GUI layer is :mod:`ovtsuite.gui_disksync`.
"""

from __future__ import annotations

import copy
import json
import os
import posixpath
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from . import archive as _ar
from . import delta as _d
from .model import new_id, now

#: manifest keys that are not merged: the file's own bookkeeping
_NOT_MERGED = {"format", "version", "id", "requires", "app", "created",
               "modified", "entries", "damaged"}
#: per-record lists of a set, and the manifest's attachment lists
_SET_LISTS = ("members", "checkpoints", "branches", "originals")
_ATT_LISTS = ("logs", "attachments", "patches")
_KIND = {"members": "member", "checkpoints": "version", "branches": "branch",
         "originals": "original"}
#: suffix of a name given to our side of something both sides have
MINE = " (mine)"


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _file_sha(path: str) -> str:
    with open(path, "rb") as fh:
        return _ar.sha256(fh.read())


# ------------------------------------------------------------ disk state
class DiskChanged(_ar.ProjectError):
    """The file is no longer what the project was loaded from or saved to."""

    def __init__(self, path: str, deleted: bool = False):
        self.path = path
        self.deleted = deleted
        super().__init__(
            "%s was %s by another program since it was opened or last saved"
            % (os.path.basename(path), "deleted or moved" if deleted
               else "changed"))


class DiskState:
    """What the file was when the project was loaded from it or saved to it."""

    __slots__ = ("path", "size", "mtime_ns", "sha256", "manifest", "blobs")

    def __init__(self, path, size, mtime_ns, sha256, manifest, blobs):
        self.path = _norm(path)
        self.size = size
        self.mtime_ns = mtime_ns
        self.sha256 = sha256
        #: the manifest and blobs as they were -- the base of a merge
        self.manifest = manifest
        self.blobs = blobs

    @classmethod
    def of(cls, project, path: str, data: bytes) -> "DiskState":
        """``project`` was just loaded from / saved to ``path`` as ``data``."""
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime = len(data), 0
        return cls(path, size, mtime, _ar.sha256(data), project.manifest(),
                   dict(project._blobs))

    def is_for(self, path: Optional[str]) -> bool:
        return bool(path) and _norm(path) == self.path

    def looks_changed(self) -> Optional[str]:
        """Cheap check (size and time only): None, "changed" or "deleted"."""
        try:
            st = os.stat(self.path)
        except OSError:
            return "deleted"
        if (st.st_size, st.st_mtime_ns) == (self.size, self.mtime_ns):
            return None
        return "changed"

    def changed(self) -> Optional[str]:
        """Full check: None, "changed" or "deleted". A file only touched
        (same content, new time) counts as unchanged, and its new time is
        remembered."""
        try:
            st = os.stat(self.path)
            sha = _file_sha(self.path)
        except OSError:
            return "deleted"
        if sha != self.sha256:
            return "changed"
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns
        return None


def check(project, path: str) -> None:
    """Raise :class:`DiskChanged` when ``path`` is the file the project was
    loaded from or last saved to and it has changed since. A save to any
    other path is not checked (the user picked it in a save dialog)."""
    st = getattr(project, "_disk", None)
    if st is None or not st.is_for(path):
        return
    why = st.changed()
    if why is not None:
        raise DiskChanged(path, deleted=(why == "deleted"))


def changed_on_disk(project, quick: bool = False) -> Optional[str]:
    """None, "changed" or "deleted" for the file the project came from."""
    st = getattr(project, "_disk", None)
    if st is None:
        return None
    return st.looks_changed() if quick else st.changed()


def adopt_disk(project, path: str) -> None:
    """Take the file at ``path`` as it is now as what ``project`` was last
    saved to (crash recovery: the project came from a copy of it)."""
    from .project import Project
    project._disk = None
    if path and os.path.exists(path):
        try:
            project._disk = Project.load(path)._disk
        except Exception:
            project._disk = None


# --------------------------------------------------------------- merging
@dataclass
class Conflict:
    """A field both sides changed differently."""
    key: tuple
    label: str
    mine: str
    theirs: str


@dataclass
class MergeResult:
    project: object
    conflicts: List[Conflict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: ``(set id, branch id) -> branch id``: where our side of a branch now
    #: lives, when both sides moved it (the "(mine)" branch)
    moved: Dict[Tuple[str, str], str] = field(default_factory=dict)
    #: the merged project differs from the file (it needs saving)
    differs: bool = False


def _short(v) -> str:
    if v is MISSING or v is None:
        return "(none)"
    if isinstance(v, (bytes, bytearray)):
        return "%d bytes" % len(v)
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " / ")
    return s if len(s) <= 80 else s[:77] + "..."


class _Merger:
    def __init__(self, base: dict, mine: dict, theirs: dict, choices, default,
                 keep_sets, keep_branches, names):
        self.base, self.mine, self.theirs = base, mine, theirs
        self.choices = dict(choices or {})
        self.default = default
        self.keep_sets = set(keep_sets)
        self.keep_branches = set(keep_branches)
        self.names = names
        self.conflicts: List[Conflict] = []
        self.notes: List[str] = []
        self.moved: Dict[Tuple[str, str], str] = {}
        #: (set id, checkpoint id) -> "mine" / "theirs": whose record (and
        #: so whose delta / image) a checkpoint was taken from
        self.ck_side: Dict[Tuple[str, str], str] = {}

    # -- labels ----------------------------------------------------------
    def where(self, path: tuple) -> str:
        n = self.names
        if not path:
            return "Project"
        head = path[0]
        if head == "name":
            return "Project name"
        if head == "settings":
            return "Project setting %s" % ".".join(map(str, path[1:]))
        if head == "info":
            from .model import INFO_LABELS
            return "Project information: %s" % INFO_LABELS.get(path[1], path[1]) \
                if len(path) > 1 else "Project information"
        if head == "sets" and len(path) > 1:
            sid = path[1]
            out = "Set %s" % n.get(("set", sid), sid)
            rest = path[2:]
            if len(rest) >= 2 and rest[0] in _SET_LISTS:
                kind = _KIND[rest[0]]
                out += " ▸ %s %s" % (kind, n.get((kind, sid, rest[1]), rest[1]))
                rest = rest[2:]
            if rest:
                out += ": %s" % ".".join(map(str, rest))
            return out
        if head in _ATT_LISTS and len(path) > 1:
            out = "%s %s" % (head[:-1].capitalize(),
                             n.get(("att", path[1]), path[1]))
            if path[2:]:
                out += ": %s" % ".".join(map(str, path[2:]))
            return out
        if head == "blob":
            return "Stored file %s" % path[1]
        return ".".join(map(str, path))

    # -- values ----------------------------------------------------------
    def conflict(self, path: tuple, m, t):
        """Both changed ``path`` differently: the caller's choice."""
        self.conflicts.append(Conflict(path, self.where(path), _short(m),
                                       _short(t)))
        return m if self.choices.get(path, self.default) == "mine" else t

    def value(self, path: tuple, b, m, t):
        """Three-way merge of one value (dicts key by key)."""
        if m == t:
            return m
        if m == b:
            return t
        if t == b:
            return m
        if isinstance(m, dict) and isinstance(t, dict):
            bd = b if isinstance(b, dict) else {}
            out = {}
            for k in list(t) + [k for k in m if k not in t]:
                v = self.value(path + (k,), bd.get(k, MISSING), m.get(k, MISSING),
                               t.get(k, MISSING))
                if v is not MISSING:
                    out[k] = v
            return out
        return self.conflict(path, m, t)

    def fields(self, path: tuple, b, m, t, skip=()) -> dict:
        """A record's fields merged one by one (``skip``: done by the
        caller)."""
        bd = b if isinstance(b, dict) else {}
        out = {}
        for k in list(t) + [k for k in m if k not in t]:
            if k in skip:
                continue
            v = self.value(path + (k,), bd.get(k, MISSING), m.get(k, MISSING),
                           t.get(k, MISSING))
            if v is not MISSING:
                out[k] = v
        return out

    # -- lists of records --------------------------------------------------
    def records(self, path: tuple, B, M, T, item, keep=(), what="item"):
        """Merge lists of records keyed by ``id``. ``item(id, b, m, t)``
        returns the merged record(s) of one id, ``MISSING`` standing for a
        side that does not have it. ``keep``: ids not to be deleted (open
        in a tab with unsaved edits)."""
        bi = {r.get("id"): r for r in B or [] if isinstance(r, dict)}
        mi = {r.get("id"): r for r in M or [] if isinstance(r, dict)}
        ti = {r.get("id"): r for r in T or [] if isinstance(r, dict)}
        order = list(ti) + [i for i in mi if i not in ti]
        out = []
        for rid in order:
            b, m, t = bi.get(rid, MISSING), mi.get(rid, MISSING), ti.get(rid, MISSING)
            if m is MISSING:
                if b is not MISSING:
                    if t == b:
                        continue                    # we deleted it
                    self.notes.append("%s was deleted here but changed by the "
                                      "other program: it is kept."
                                      % self.where(path + (rid,)))
                got = item(rid, MISSING, MISSING, t)
            elif t is MISSING:
                if b is not MISSING:
                    if m == b and rid not in keep:
                        continue                    # they deleted it
                    self.notes.append("%s was deleted by the other program but "
                                      "%s here: it is kept."
                                      % (self.where(path + (rid,)),
                                         "is open with unsaved edits"
                                         if m == b else "changed"))
                got = item(rid, MISSING, m, MISSING)
            else:
                got = item(rid, b, m, t)
            out.extend(got if isinstance(got, list) else [got])
        return out

    def plain(self, path):
        def item(rid, b, m, t):
            if m is MISSING:
                return t
            if t is MISSING:
                return m
            return self.fields(path + (rid,), b, m, t)
        return item

    # -- sets -------------------------------------------------------------
    def a_set(self, sid, b, m, t):
        if m is MISSING or t is MISSING:
            side = "theirs" if m is MISSING else "mine"
            rec = t if m is MISSING else m
            for c in rec.get("checkpoints") or []:
                self.ck_side[(sid, c.get("id"))] = side
            return rec
        path = ("sets", sid)
        bd = b if isinstance(b, dict) else {}
        out = self.fields(path, b, m, t, skip=_SET_LISTS)
        keep_b = {bid for (s, bid) in self.keep_branches if s == sid}
        out["members"] = self.records(path + ("members",), bd.get("members"),
                                      m.get("members"), t.get("members"),
                                      self.plain(path + ("members",)))
        out["checkpoints"] = self.records(
            path + ("checkpoints",), bd.get("checkpoints"), m.get("checkpoints"),
            t.get("checkpoints"), lambda *a: self.checkpoint(sid, *a))
        out["branches"] = self.records(
            path + ("branches",), bd.get("branches"), m.get("branches"),
            t.get("branches"), lambda *a: self.branch(sid, *a), keep=keep_b)
        out["originals"] = self.records(path + ("originals",), bd.get("originals"),
                                        m.get("originals"), t.get("originals"),
                                        self.plain(path + ("originals",)))
        return out

    def checkpoint(self, sid, cid, b, m, t):
        """A checkpoint's content (parent, delta, sizes, log: what a merge
        of versions rewrites) comes whole from one side; its name merges."""
        def struct(r):
            return None if r is MISSING else {k: v for k, v in r.items()
                                              if k != "label"}
        if m is MISSING or t is MISSING:
            self.ck_side[(sid, cid)] = "theirs" if m is MISSING else "mine"
            return t if m is MISSING else m
        bs, ms, ts = struct(b), struct(m), struct(t)
        if ms == ts or ms == bs:
            side, rec = "theirs", dict(t)
        elif ts == bs:
            side, rec = "mine", dict(m)
        else:
            side, rec = "theirs", dict(t)
            self.notes.append("%s was changed in both (versions merged): the "
                              "other program's is kept."
                              % self.where(("sets", sid, "checkpoints", cid)))
        self.ck_side[(sid, cid)] = side
        label = self.value(("sets", sid, "checkpoints", cid, "label"),
                           MISSING if b is MISSING else b.get("label", MISSING),
                           m.get("label", MISSING), t.get("label", MISSING))
        if label is MISSING:
            rec.pop("label", None)
        else:
            rec["label"] = label
        return rec

    def branch(self, sid, bid, b, m, t):
        """A branch both sides moved keeps the other program's head; ours
        goes on a new branch, "<name> (mine)"."""
        if m is MISSING or t is MISSING:
            return t if m is MISSING else m
        path = ("sets", sid, "branches", bid)
        out = self.fields(path, b, m, t, skip=("head",))
        bh = MISSING if b is MISSING else b.get("head")
        mh, th = m.get("head"), t.get("head")
        if mh == th or mh == bh:
            out["head"] = th
            return out
        if th == bh:
            out["head"] = mh
            return out
        out["head"] = th
        mine = dict(m, id=new_id(), name=m.get("name", "Branch") + MINE, head=mh,
                    created=now())
        mine["note"] = ("Your work on %s, kept apart when another program "
                        "also saved to that branch." % m.get("name", "a branch"))
        self.moved[(sid, bid)] = mine["id"]
        self.notes.append("Branch %s of set %s was saved to by both: the "
                          "other program's version stays on it, yours is on "
                          "the new branch \"%s\"." % (m.get("name"),
                          self.names.get(("set", sid), sid), mine["name"]))
        return [out, mine]

    # -- the whole manifest --------------------------------------------
    def manifest(self) -> dict:
        b, m, t = self.base, self.mine, self.theirs
        out = dict(t)
        keys = [k for k in list(t) + [k for k in m if k not in t]
                if k not in _NOT_MERGED and k != "sets" and k not in _ATT_LISTS]
        for k in keys:
            v = self.value((k,), b.get(k, MISSING), m.get(k, MISSING),
                           t.get(k, MISSING))
            if v is MISSING:
                out.pop(k, None)
            else:
                out[k] = v
        out["sets"] = self.records(("sets",), b.get("sets"), m.get("sets"),
                                   t.get("sets"), self.a_set, keep=self.keep_sets)
        for k in _ATT_LISTS:
            got = self.records((k,), b.get(k), m.get(k), t.get(k), self.plain((k,)))
            if got or k != "patches":
                out[k] = got
            else:
                out.pop(k, None)
        return out

    # -- blobs ----------------------------------------------------------
    def blobs(self, manifest, B, M, T) -> Dict[str, bytes]:
        """Each stored file three-way, kept only while the record it belongs
        to is in the merged project; a checkpoint's delta / image comes from
        the side its record came from."""
        present = _owners(manifest)
        out: Dict[str, bytes] = {}
        for p in sorted(set(B) | set(M) | set(T)):
            owner = _owner(p)
            if owner is not None and owner not in present:
                continue
            b, m, t = B.get(p), M.get(p), T.get(p)
            if owner is not None and owner[0] == "ck":
                side = self.ck_side.get(owner[1:], "theirs")
                v = (m if side == "mine" else t)
                if v is None:
                    v = t if side == "mine" else m
            elif m is t or m == t:
                v = m
            elif m is b or m == b:
                v = t if t is not None else m       # never drop a live record's
            elif t is b or t == b:
                v = m if m is not None else t
            elif owner is not None and owner[0] == "cache":
                v = t if t is not None else m       # advisory: theirs
            else:
                pick = self.conflict(("blob", p), m if m is not None else MISSING,
                                     t if t is not None else MISSING)
                v = None if pick is MISSING else pick
            if v is not None:
                out[p] = v
        return out


def _owner(path: str):
    """The record a stored file belongs to (None: none known)."""
    parts = path.split("/")
    if parts[0] == "sets" and len(parts) >= 3:
        sid = parts[1]
        if parts[2] in ("images",) and len(parts) >= 5:
            return ("ck", sid, parts[3])
        if parts[2] == "deltas" and len(parts) == 4:
            return ("ck", sid, posixpath.splitext(parts[3])[0])
        if parts[2] == "originals" and len(parts) >= 5:
            return ("orig", sid, parts[3])
        if parts[2] == "defs" and len(parts) >= 5:
            return ("member", sid, parts[3])
        if parts[2] == "cache" and len(parts) == 4:
            return ("cache", sid, posixpath.splitext(parts[3])[0])
        return ("set", sid)
    if parts[0] in _ATT_LISTS and len(parts) >= 3:
        return ("att", parts[0], parts[1])
    return None


def _owners(manifest: dict) -> set:
    out = set()
    for s in manifest.get("sets") or []:
        sid = s.get("id")
        out.add(("set", sid))
        for c in s.get("checkpoints") or []:
            out.add(("ck", sid, c.get("id")))
        for o in s.get("originals") or []:
            out.add(("orig", sid, o.get("id")))
        for mm in s.get("members") or []:
            out.add(("member", sid, mm.get("id")))
        for br in s.get("branches") or []:
            out.add(("cache", sid, br.get("id")))
    for k in _ATT_LISTS:
        for a in manifest.get(k) or []:
            out.add(("att", k, a.get("id")))
    return out


def _names(*manifests) -> dict:
    """Display names of everything a conflict can be about."""
    n = {}
    for man in manifests:
        for s in man.get("sets") or []:
            sid = s.get("id")
            n.setdefault(("set", sid), s.get("name") or sid)
            for br in s.get("branches") or []:
                n.setdefault(("branch", sid, br.get("id")), br.get("name"))
            for c in s.get("checkpoints") or []:
                n.setdefault(("version", sid, c.get("id")),
                             "\"%s\" (%s)" % (c.get("label") or c.get("kind"),
                                              (c.get("ts") or "").replace("T", " ")))
            for mm in s.get("members") or []:
                n.setdefault(("member", sid, mm.get("id")),
                             mm.get("name") or mm.get("id"))
            for o in s.get("originals") or []:
                n.setdefault(("original", sid, o.get("id")), o.get("filename"))
        for k in _ATT_LISTS:
            for a in man.get(k) or []:
                n.setdefault(("att", a.get("id")), a.get("filename"))
    return n


def _comparable(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k not in _NOT_MERGED}


def merge(mine, theirs, choices: Optional[Dict[tuple, str]] = None, *,
          default: str = "theirs", keep_sets: Iterable[str] = (),
          keep_branches: Iterable[Tuple[str, str]] = ()) -> MergeResult:
    """Merge the project in memory (``mine``) with the file as it is now
    (``theirs``, freshly loaded) against what the file was when ``mine``
    was loaded or last saved. Returns a new project; neither input changes.

    ``choices`` maps a :class:`Conflict`'s key to "mine" or "theirs"
    (``default`` for any not in it): run once, show the conflicts, run again
    with the user's picks. ``keep_sets`` / ``keep_branches`` (``(set id,
    branch id)``) are not deleted even when the other program deleted them
    (they are open with unsaved edits)."""
    from .project import Project
    st = getattr(mine, "_disk", None)
    base_m = st.manifest if st is not None else {}
    base_b = st.blobs if st is not None else {}
    mine_m, theirs_m = mine.manifest(), theirs.manifest()
    mg = _Merger(base_m, mine_m, theirs_m, choices, default, keep_sets,
                 keep_branches, _names(mine_m, theirs_m, base_m))
    man = mg.manifest()
    blobs = mg.blobs(man, base_b, mine._blobs, theirs._blobs)
    res = Project.from_manifest(man, blobs, copy.copy(theirs.report))
    res.path = theirs.path or mine.path
    damaged = set()
    for p, v in blobs.items():
        if (p in theirs._damaged and theirs._blobs.get(p) is v) or \
                (p in mine._damaged and mine._blobs.get(p) is v):
            damaged.add(p)
    res._damaged = damaged
    res.report.damaged = sorted(damaged)
    _repair(res, mine, theirs, mg)
    # (a definition cache only we have is carried, but is no unsaved change:
    # it is advisory, and written on closing as usual)
    def content(blobs):
        return {p: v for p, v in blobs.items()
                if (_owner(p) or ("",))[0] != "cache"}
    differs = (_comparable(res.manifest()) != _comparable(theirs.manifest())
               or content(res._blobs) != content(theirs._blobs))
    res._disk = theirs._disk
    res._dirty = differs
    res.revision = max(mine.revision, theirs.revision) + 1
    if differs:
        res.modified = now()
    sig = theirs.report.signature
    if differs and sig.status == "valid":
        mg.notes.append("The file on disk was signed (%s); the merged project "
                        "is saved without a signature." % sig.describe())
    return MergeResult(res, mg.conflicts, mg.notes, mg.moved, differs)


def _repair(res, mine, theirs, mg: _Merger) -> None:
    """Keep the merged history whole: a checkpoint whose parent the other
    side merged away hangs from its nearest ancestor that is still there
    (its delta recomputed); a branch head that is gone moves back the same
    way; names stay unique; the active branch exists."""
    from .project import HistoryError
    for s in res.sets:
        have = {c.id for c in s.checkpoints}
        for c in list(s.checkpoints):
            if c.parent is None or c.parent in have:
                continue
            src = mine if mg.ck_side.get((s.id, c.id)) == "mine" else theirs
            try:
                ss = src.set(s.id)
                anc = next((a.id for a in ss.lineage(c.id)[1:] if a.id in have),
                           None)
                new = src.state(s.id, c.id)
                if anc is None:
                    c.parent = None
                    res._blobs.pop(res._delta_path(s.id, c.id), None)
                    res._store_images(s, c.id, new)
                else:
                    try:
                        old = src.state(s.id, anc)
                    except (KeyError, HistoryError):
                        old = theirs.state(s.id, anc)
                    recs = _d.diff(old, new)
                    c.parent = anc
                    c.n_bytes = _d.changed_bytes(recs)
                    res._blobs[res._delta_path(s.id, c.id)] = _d.encode(recs)
            except (KeyError, HistoryError, _d.DeltaError):
                mg.notes.append("Version %s of set %s could not be placed in "
                                "the merged history." % (c.label or c.id, s.name))
        for br in s.branches:
            if s.checkpoint(br.head) is not None:
                continue
            for src in (mine, theirs):
                try:
                    lin = src.set(s.id).lineage(br.head)
                except KeyError:
                    continue
                anc = next((a.id for a in lin if s.checkpoint(a.id)), None)
                if anc:
                    br.head = anc
                    break
            else:
                if s.checkpoints:
                    br.head = s.checkpoints[-1].id
        _unique(s.branches, lambda b: b.name, lambda b, n: setattr(b, "name", n))
        if s.active is None or s.branch(s.active_branch) is None:
            s.active_branch = s.branches[0].id if s.branches else None
    _unique(res.sets, lambda s: s.name, lambda s, n: setattr(s, "name", n))


def _unique(items, get, put) -> None:
    """Later duplicates (case-insensitive) get " (mine)" / " (mine 2)"."""
    taken = set()
    for it in items:
        name = get(it) or ""
        if name.lower() in taken:
            stem = name[:-len(MINE)] if name.endswith(MINE) else name
            cand, i = stem + MINE, 2
            while cand.lower() in taken:
                cand = "%s (mine %d)" % (stem, i)
                i += 1
            put(it, cand)
            name = cand
        taken.add(name.lower())
