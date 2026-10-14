# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Crash recovery for ``.ovt`` projects (spec section 9).

While a project is open, an :class:`AutoSaver` keeps a *recovery entry* in
the user's config directory with everything needed to rebuild the unsaved
state after a crash:

* the working bytes of every open branch, as a delta against that branch's
  saved head checkpoint -- **raw bytes exactly as edited, no checksum
  correction**, so a snapshot is cheap and never alters the data;
* a copy of the project itself (``base.ovt``) only while the project has
  structural changes that are not on disk (never saved, new branches or
  checkpoints, ...).

The writing happens on a background thread; the caller only freezes the
bytes and never waits: snapshots taken while one is written coalesce into
the newest, written next. The entry is held by an OS file lock for as long as its owner runs,
so a live session (this process or another instance) is never offered for
recovery -- only an entry whose owner died.

Lifecycle (the GUI drives it):

* snapshot periodically (:meth:`AutoSaver.maybe_snapshot` skips when nothing
  changed);
* after a successful save -> :meth:`AutoSaver.saved` (nothing is unsaved);
* on a proper exit, whether the user saved or **refused to save** ->
  :meth:`AutoSaver.discard` (the recovery data is deleted);
* at startup -> :func:`pending`; offer :func:`recover` or :func:`discard`.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import archive as _ar
from . import delta as _d
from .model import new_id, now
from .project import HistoryError, Project

Images = Dict[str, bytes]
Working = Dict[Tuple[str, str], Images]        # (set id, branch id) -> images

ENV_DIR = "OVT_RECOVERY_DIR"
ENTRY = "entry.json"
LOCK = "lock"
BASE = "base.ovt"
#: marks an entry its owner discarded while a snapshot was still being
#: written: removed at the next start, never offered
DISCARDED = "discarded"
ENTRY_VERSION = 1
#: how long a save or an exit waits for a snapshot being written (seconds);
#: past that the snapshot -- out of date anyway -- removes itself when done
WAIT_S = 2.0


def recovery_root() -> str:
    """``<config dir>/recovery`` (``%APPDATA%\\OVT-Suite\\recovery`` on
    Windows, ``~/.config/ovtsuite/recovery`` elsewhere); ``OVT_RECOVERY_DIR``
    overrides it."""
    override = os.environ.get(ENV_DIR)
    if override:
        return override
    from ..recent import _config_dir
    return os.path.join(_config_dir(), "recovery")


# ------------------------------------------------------------------ locking
class _Lock:
    """An exclusive, non-blocking OS lock on a file, held while open."""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _file_sha(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return _ar.sha256(fh.read())


# ---------------------------------------------------------------- the saver
class _Cancelled(Exception):
    pass


class _Frozen:
    """What one snapshot writes, frozen on the thread that owns the project
    (it has no locks): everything else happens on the writer thread."""

    __slots__ = ("heads", "parts", "need_base", "rev", "info", "sha", "path",
                 "gen", "seed")

    def __init__(self, heads, parts, need_base, rev, info, sha, path, gen,
                 seed):
        self.heads = heads          # (sid, bid, head, label, base, new)
        self.parts = parts          # (manifest, blobs) for base.ovt, or None
        self.need_base = need_base
        self.rev = rev              # project.revision when frozen
        self.info = info
        self.sha = sha              # the .ovt's recorded sha256, if known...
        self.path = path            # ...else the writer hashes this file
        self.gen = gen              # AutoSaver._gen when frozen
        self.seed = seed            # the project's pack cache, to start ours


class _Job:
    """A snapshot being written: ``cancel`` stops it while it is still
    computing (a newer snapshot supersedes it, or a save made it out of
    date) -- never once it has started writing files, so the entry on disk
    is always one whole snapshot; with ``discard`` it also removes the entry
    when it stops."""

    def __init__(self, frozen: _Frozen):
        self.frozen = frozen
        self.cancel = threading.Event()
        self.discard = False
        self.thread: Optional[threading.Thread] = None

    def check(self) -> None:
        if self.cancel.is_set():
            raise _Cancelled()


#: a newer snapshot cancels one still computing at most this many times in a
#: row; after that the one running finishes first (the newest is written
#: next), so a project slower to write than snapshots come is still written
MAX_SUPERSEDE = 2


class AutoSaver:
    """Keeps the recovery entry of one open project.

    Snapshots never wait for each other: there is one writer thread at a
    time, and snapshots taken while it runs coalesce into a single pending
    one that it writes next."""

    def __init__(self, project: Project, root: Optional[str] = None):
        self.project = project
        self.root = root or recovery_root()
        self.dir = os.path.join(self.root, new_id())
        self._lock: Optional[_Lock] = None
        self._token = None
        self._base_rev = None             # project.revision of base.ovt on disk
        self._started = now()
        self._job: Optional[_Job] = None  # the one being written
        self._pending: Optional[_Frozen] = None   # the newest one waiting
        self._superseded = 0              # running jobs cancelled in a row
        #: bumped by saved/discard/reset: a job frozen before no longer
        #: records its _base_rev or error
        self._gen = 0
        self._qlock = threading.Lock()    # _job, _pending, _superseded, _gen
        self._mutex = threading.Lock()    # the entry's files
        self._zcache: _ar.PackCache = {}  # base.ovt's entries, between writes
        self.last_error: Optional[str] = None

    # -- state the GUI asks about --------------------------------------
    @property
    def active(self) -> bool:
        return os.path.isdir(self.dir)

    def _ensure_dir(self) -> None:
        if self._lock is not None:
            return
        os.makedirs(self.dir, exist_ok=True)
        lock = _Lock(os.path.join(self.dir, LOCK))
        if not lock.acquire():                       # pragma: no cover
            raise OSError("recovery entry %s is locked" % self.dir)
        self._lock = lock

    # -- snapshots -------------------------------------------------------
    def maybe_snapshot(self, working: Callable[[], Working], token) -> bool:
        """Snapshot unless ``token`` (e.g. the open sessions' revisions plus
        the project's) equals the last snapshot's. ``working`` is called only
        when a snapshot is taken. Returns True when one was started."""
        token = (token, self.project.revision, self.project.path)
        if token == self._token:
            return False
        self.snapshot(working())
        self._token = token
        return True

    def snapshot(self, working: Working, *, wait: bool = False) -> None:
        """Freeze the unsaved state now and write it on a background thread;
        returns at once (with ``wait``: once it is written).

        ``working`` maps ``(set id, branch id)`` to the edited member images
        of that branch (members left out are unchanged). The bytes are taken
        exactly as given -- no checksum correction."""
        f = self._freeze(working)
        with self._qlock:
            job = self._job
            if job is None:
                self._start(_Job(f))
            else:
                old = self._pending
                if f.parts is None and old is not None \
                        and old.parts is not None and old.rev == f.rev:
                    f.parts, f.seed = old.parts, old.seed
                self._pending = f       # written next, replacing any other
                if not job.cancel.is_set() \
                        and self._superseded < MAX_SUPERSEDE:
                    self._superseded += 1
                    job.cancel.set()    # out of date: stops if it still can
        if wait:
            self.flush()

    def _freeze(self, working: Working) -> _Frozen:
        """The part on the project's thread: only what reads the project."""
        p = self.project
        heads = []
        for (sid, bid), images in working.items():
            s = p.set(sid)
            b = s.branch(bid)
            if b is None:
                continue
            try:
                base = p.state(s.id, b.head)
            except HistoryError:
                base = {}
            new = dict(base)
            new.update({k: bytes(v) for k, v in images.items()})
            heads.append((s.id, b.id, b.head, "%s / %s" % (s.name, b.name),
                          base, new))
        need_base = p.path is None or p.modified_since_save
        parts = seed = None
        if need_base and self._base_rev != p.revision:
            parts = p.snapshot_parts()
            if not self._zcache:
                seed = dict(getattr(p, "_zcache", None) or {})
        sha = path = None
        if not need_base:
            disk = getattr(p, "_disk", None)
            if disk is not None and disk.is_for(p.path):
                sha = disk.sha256           # as loaded or last saved
            else:
                path = p.path
        info = {"version": ENTRY_VERSION, "app": p.app or _ar_app(),
                "pid": os.getpid(), "started": self._started,
                "updated": now(), "project_name": p.name,
                "project_path": os.path.abspath(p.path) if p.path else None,
                "base_sha256": None}
        return _Frozen(heads, parts, need_base, p.revision, info, sha, path,
                       self._gen, seed)

    def _start(self, job: _Job) -> None:
        self._job = job
        job.thread = threading.Thread(target=self._run, args=(job,),
                                      name="ovt-autosave", daemon=True)
        job.thread.start()

    def _run(self, job: _Job) -> None:
        """The writer thread: this job, then the pending one, until none."""
        try:
            while True:
                self._write(job)
                with self._qlock:
                    f, self._pending = self._pending, None
                    if f is None:
                        self._job = None
                        return
                    nxt = _Job(f)
                    nxt.thread = job.thread
                    self._job = job = nxt
        except BaseException:                      # pragma: no cover
            with self._qlock:
                self._job = None
            raise

    def _write(self, job: _Job) -> None:
        f = job.frozen
        try:
            with self._mutex:
                try:
                    self._write_entry(job, f)
                    with self._qlock:
                        self._superseded = 0
                except _Cancelled:
                    pass
                finally:
                    if job.discard:     # saved or closed meanwhile
                        self._remove_dir()
        except Exception as ex:                    # never take the app down
            with self._qlock:
                if f.gen == self._gen:
                    self.last_error = str(ex)

    def _write_entry(self, job: _Job, f: _Frozen) -> None:
        # computing -- stops here when out of date
        job.check()
        working, files = [], []
        for sid, bid, head, label, base, new in f.heads:
            recs = _d.diff(base, new)
            job.check()
            if not recs:
                continue
            name = "w_%s_%s.ovd" % (sid, bid)
            working.append({"set": sid, "branch": bid, "head": head,
                            "label": label, "delta": name,
                            "n_bytes": _d.changed_bytes(recs)})
            files.append((name, _d.encode(recs)))
        base_path = os.path.join(self.dir, BASE)
        base_data = None
        if f.need_base and f.parts is not None and not (
                self._base_rev == f.rev and os.path.exists(base_path)):
            if f.seed and not self._zcache:
                self._zcache.update(f.seed)
            manifest, blobs = f.parts
            base_data = _ar.write(manifest, blobs, cache=self._zcache)
            job.check()
        sha = None if f.need_base else (f.sha or _file_sha(f.path))
        job.check()
        # writing -- the whole snapshot, never stopped half way
        if not working and not f.need_base:
            self._remove_dir()          # nothing unsaved
            return
        keep = {ENTRY, LOCK}
        if job.discard:
            keep.add(DISCARDED)
        self._ensure_dir()
        for name, data in files:
            keep.add(name)
            _write_atomic(os.path.join(self.dir, name), data)
        base_rev = None
        if f.need_base:
            keep.add(BASE)
            if base_data is not None:
                _write_atomic(base_path, base_data)
                base_rev = f.rev
            elif os.path.exists(base_path):
                base_rev = self._base_rev
        info = dict(f.info, base=BASE if f.need_base else None,
                    working=working, base_sha256=sha)
        _write_atomic(os.path.join(self.dir, ENTRY),
                      json.dumps(info, indent=1).encode("utf-8"))
        for name in os.listdir(self.dir):     # stale deltas / base
            if name not in keep and not name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass
        with self._qlock:
            if f.gen == self._gen:      # not saved or replaced meanwhile
                self._base_rev = base_rev
                self.last_error = None

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait for the snapshots being written or pending (at most
        ``timeout`` seconds). True when none is running any more."""
        with self._qlock:
            job = self._job
        if job is None or job.thread is None:
            return True
        job.thread.join(timeout)
        return not job.thread.is_alive()

    def _outdate(self, discard: bool) -> None:
        """Snapshots frozen until now are out of date: the pending one is
        dropped, the running one stopped if it still can be."""
        with self._qlock:
            self._gen += 1
            self._pending = None
            self._superseded = 0
            job = self._job
            if job is not None:
                if discard:
                    job.discard = True
                job.cancel.set()

    # -- lifecycle -------------------------------------------------------
    def saved(self, wait: float = WAIT_S) -> None:
        """The project was just saved: nothing is unsaved any more. A
        snapshot being written is out of date: it is stopped, and waited for
        at most ``wait`` seconds -- past that it removes the entry itself
        when it stops (and the next start removes it if the app is gone by
        then)."""
        self._outdate(discard=True)
        self.flush(wait)
        if self._mutex.acquire(timeout=max(wait, 0.01)):
            try:
                self._remove_dir()
            finally:
                self._mutex.release()
        else:                                     # pragma: no cover - slow disk
            try:
                with open(os.path.join(self.dir, DISCARDED), "wb"):
                    pass
            except OSError:
                pass
        self._token = None
        self._base_rev = None

    def discard(self, wait: float = WAIT_S) -> None:
        """Proper exit (saved, or the user refused to save): delete the
        recovery data (see :meth:`saved`)."""
        self.saved(wait)

    close = discard

    def reset(self, project: Project) -> None:
        """``project`` replaces the one kept (merged with what another
        program changed): the next snapshot starts over, without waiting for
        one being written."""
        self._outdate(discard=False)
        self.project = project
        self._token = None
        self._base_rev = None

    def _remove_dir(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        shutil.rmtree(self.dir, ignore_errors=True)


def _ar_app() -> str:
    from .project import _app_string
    return _app_string()


# ----------------------------------------------------------------- recovery
@dataclass
class Recovery:
    """An entry left behind by a session that did not exit properly."""
    dir: str
    info: dict

    @property
    def updated(self) -> str:
        return str(self.info.get("updated", ""))

    @property
    def project_path(self) -> Optional[str]:
        return self.info.get("project_path")

    def describe(self) -> str:
        name = self.info.get("project_name") or (
            os.path.basename(self.project_path) if self.project_path
            else "Untitled project")
        parts = [w.get("label", "") for w in self.info.get("working") or []]
        what = ", ".join(p for p in parts if p) or "project changes"
        return "%s -- unsaved %s (autosaved %s)" % (name, what, self.updated)


def pending(root: Optional[str] = None) -> List[Recovery]:
    """Entries whose owner is gone (its lock can be taken), newest first."""
    root = root or recovery_root()
    out: List[Recovery] = []
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        lock = _Lock(os.path.join(d, LOCK))
        try:
            if not lock.acquire():
                continue                          # its owner is still running
        except OSError:
            continue
        try:
            if os.path.exists(os.path.join(d, DISCARDED)):
                raise ValueError("discarded by its owner")
            with open(os.path.join(d, ENTRY), "rb") as fh:
                info = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
            info = None
        finally:
            lock.release()
        if info is None:
            # a crash before the first complete snapshot: nothing to offer
            shutil.rmtree(d, ignore_errors=True)
            continue
        out.append(Recovery(d, info))
    out.sort(key=lambda r: r.updated, reverse=True)
    return out


def recover(rec: Recovery) -> Tuple[Project, Working, List[str]]:
    """``(project, working, warnings)``: the project as it was, plus the
    unsaved images of every branch, to be opened as UNSAVED edits (the user
    decides whether to save -- and a save then corrects checksums as usual).
    """
    warnings: List[str] = []
    info = rec.info
    path = info.get("project_path")
    if info.get("base"):
        proj = Project.load(os.path.join(rec.dir, info["base"]))
        proj.path = path
        proj._dirty = True                        # its structure was unsaved
        # the next save checks against the real file as it is now, not
        # against the recovery copy it was rebuilt from
        from .disksync import adopt_disk
        adopt_disk(proj, path)
    else:
        if not path or not os.path.exists(path):
            raise FileNotFoundError(
                "the project this recovery belongs to is gone: %s" % path)
        proj = Project.load(path)
        if info.get("base_sha256") and _file_sha(path) != info["base_sha256"]:
            warnings.append("%s changed on disk after the autosave; the "
                            "recovered edits are applied to the branch heads "
                            "they were made on" % os.path.basename(path))
    working: Working = {}
    for w in info.get("working") or []:
        try:
            s = proj.set(w["set"])
            if s.branch(w["branch"]) is None:
                raise KeyError(w["branch"])
            base = proj.state(s.id, w["head"])
            with open(os.path.join(rec.dir, w["delta"]), "rb") as fh:
                recs = _d.decode(fh.read())
            working[(s.id, w["branch"])] = _d.apply_forward(base, recs)
        except (KeyError, OSError, HistoryError, _d.DeltaError) as ex:
            warnings.append("could not recover %s: %s" % (w.get("label", "?"), ex))
    return proj, working, warnings


def discard(rec: Recovery) -> None:
    shutil.rmtree(rec.dir, ignore_errors=True)
