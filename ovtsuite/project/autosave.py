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
bytes. The entry is held by an OS file lock for as long as its owner runs,
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
ENTRY_VERSION = 1


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
class AutoSaver:
    """Keeps the recovery entry of one open project."""

    def __init__(self, project: Project, root: Optional[str] = None):
        self.project = project
        self.root = root or recovery_root()
        self.dir = os.path.join(self.root, new_id())
        self._lock: Optional[_Lock] = None
        self._token = None
        self._base_rev = None             # project.revision of base.ovt on disk
        self._started = now()
        self._job: Optional[threading.Thread] = None
        self._mutex = threading.Lock()
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
        """Freeze the unsaved state now and write it on a background thread.

        ``working`` maps ``(set id, branch id)`` to the edited member images
        of that branch (members left out are unchanged). The bytes are taken
        exactly as given -- no checksum correction."""
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
        parts = None
        if need_base and self._base_rev != p.revision:
            parts = p.snapshot_parts()
        info = {"version": ENTRY_VERSION, "app": p.app or _ar_app(),
                "pid": os.getpid(), "started": self._started,
                "updated": now(), "project_name": p.name,
                "project_path": os.path.abspath(p.path) if p.path else None,
                "base_sha256": None if need_base else _file_sha(p.path)}
        rev = p.revision
        self.flush()
        job = threading.Thread(target=self._write, name="ovt-autosave",
                               args=(heads, parts, need_base, rev, info),
                               daemon=True)
        self._job = job
        job.start()
        if wait:
            self.flush()

    def _write(self, heads, parts, need_base, rev, info) -> None:
        try:
            with self._mutex:
                working = []
                keep = {ENTRY, LOCK}
                for sid, bid, head, label, base, new in heads:
                    recs = _d.diff(base, new)
                    if not recs:
                        continue
                    name = "w_%s_%s.ovd" % (sid, bid)
                    keep.add(name)
                    working.append({"set": sid, "branch": bid, "head": head,
                                    "label": label, "delta": name,
                                    "n_bytes": _d.changed_bytes(recs)})
                    self._ensure_dir()
                    _write_atomic(os.path.join(self.dir, name), _d.encode(recs))
                if not working and not need_base:
                    self._remove_dir()          # nothing unsaved
                    return
                self._ensure_dir()
                if need_base:
                    keep.add(BASE)
                    if parts is not None:
                        manifest, blobs = parts
                        _write_atomic(os.path.join(self.dir, BASE),
                                      _ar.write(manifest, blobs))
                        self._base_rev = rev
                info = dict(info, base=BASE if need_base else None,
                            working=working)
                _write_atomic(os.path.join(self.dir, ENTRY),
                              json.dumps(info, indent=1).encode("utf-8"))
                for f in os.listdir(self.dir):     # stale deltas / base
                    if f not in keep and not f.endswith(".tmp"):
                        try:
                            os.remove(os.path.join(self.dir, f))
                        except OSError:
                            pass
                if not need_base:
                    self._base_rev = None
                self.last_error = None
        except Exception as ex:                    # never take the app down
            self.last_error = str(ex)

    def flush(self) -> None:
        """Wait for a snapshot in progress."""
        job = self._job
        if job is not None and job.is_alive():
            job.join()

    # -- lifecycle -------------------------------------------------------
    def saved(self) -> None:
        """The project was just saved: nothing is unsaved any more."""
        self.flush()
        with self._mutex:
            self._remove_dir()
        self._token = None
        self._base_rev = None

    def discard(self) -> None:
        """Proper exit (saved, or the user refused to save): delete the
        recovery data."""
        self.saved()

    close = discard

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
