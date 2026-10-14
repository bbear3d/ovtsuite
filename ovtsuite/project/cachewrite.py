# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Writing only a project's definition caches into its ``.ovt``.

A branch's definition snapshot (``sets/<sid>/cache/<bid>.defn``, see
:meth:`Project.set_cache <ovtsuite.project.project.Project.set_cache>`) that
was rebuilt -- the stored one was missing or no longer fit, so the matcher
ran -- saves the next open that work. It should not wait for the user's next
save: the user may close without saving, have other unsaved changes, or the
app may crash. So the snapshots alone go into the file:

* The file is taken as it is on disk (every entry copied as stored, nothing
  of the project's other unsaved changes goes in) and only cache entries are
  added or replaced -- each for a branch whose head on disk is the head the
  snapshot was built from.
* Only while the file is still the one the project was loaded from or last
  saved to (its sha256): a file another program changed is left alone (the
  cache is advisory). Nor a signed file (a new manifest would void its
  signature -- only the user's own save drops it), nor one with damaged or
  missing entries (re-hashing would launder them).
* Afterwards the project's disk record (:class:`~.disksync.DiskState`) is
  the new file, so the next save does not see "changed on disk"; whether the
  project has unsaved changes (``modified_since_save``) does not change.

:meth:`CacheJob.run` may run on another thread (no Qt here); it and
:meth:`SaveJob.run <ovtsuite.project.project.SaveJob.run>` take the
project's write lock, so they never write the file at the same time.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from . import archive as _ar
from . import disksync as _ds


def _is_cache(path: str) -> Optional[Tuple[str, str]]:
    """``(set id, branch id)`` of a definition cache entry, else None."""
    got = _ds._owner(path)
    if got is None or got[0] != "cache":
        return None
    return got[1], got[2]


def _heads(manifest: dict) -> Dict[Tuple[str, str], str]:
    out = {}
    for s in manifest.get("sets") or []:
        for b in s.get("branches") or []:
            out[(s.get("id"), b.get("id"))] = b.get("head")
    return out


def pending(project) -> Dict[str, bytes]:
    """The project's cache entries the file does not hold yet: each for a
    branch whose head is still the one on disk (the snapshot describes the
    saved bytes)."""
    st = getattr(project, "_disk", None)
    if st is None:
        return {}
    disk_heads = _heads(st.manifest)
    out = {}
    for s in project.sets:
        for b in s.branches:
            p = project._cache_path(s.id, b.id)
            blob = project._blobs.get(p)
            if blob is None or p in project._damaged:
                continue
            if disk_heads.get((s.id, b.id)) != b.head:
                continue
            have = st.blobs.get(p)
            if have is blob or have == blob:
                continue
            out[p] = blob
    return out


class CacheJob:
    """Writing a project's pending definition caches (:func:`prepare`)."""

    def __init__(self, project, path, base, caches, heads):
        self.project = project
        self.path = path
        self.base = base            # the DiskState it was prepared against
        self.caches = caches
        self.heads = heads
        #: what happened: "" (not run), "written", or why it was skipped
        self.outcome = ""
        self.written: Dict[str, bytes] = {}

    def run(self) -> bool:
        """Write the caches into the file (any thread). True when written;
        else :attr:`outcome` says why not. Never raises for a file it
        cannot or should not write."""
        p = self.project
        with p._io_lock:
            try:
                return self._run(p)
            except (OSError, _ar.ProjectError, ValueError) as ex:
                self.outcome = "failed: %s" % ex
                return False

    def _run(self, p) -> bool:
        if p._disk is not self.base:
            # saved meanwhile -- that save wrote the caches it had
            self.outcome = "saved meanwhile"
            return False
        with open(self.path, "rb") as fh:
            data = fh.read()
        if _ar.sha256(data) != self.base.sha256:
            self.outcome = "changed on disk"
            return False
        # every entry as stored, so the write below copies it as it is
        pc: _ar.PackCache = {}
        manifest, blobs, report = _ar.read(data, cache=pc)
        if report.signature.status != "unsigned":
            self.outcome = "signed"
            return False
        if not report.ok or manifest.get("damaged"):
            self.outcome = "damaged"
            return False
        heads = _heads(manifest)
        new = dict(blobs)
        for path, blob in self.caches.items():
            key = _is_cache(path)
            if key is None or heads.get(key) != self.heads.get(key):
                continue
            if new.get(path) != blob:
                new[path] = blob
                self.written[path] = blob
        if not self.written:
            self.outcome = "nothing to write"
            return False
        manifest = {k: v for k, v in manifest.items() if k != "entries"}
        out = _ar.write(manifest, new, cache=pc)
        _ar.atomic_write(self.path, out)
        # the file as now written is the project's base: the next save
        # does not see it as changed by another program
        p._disk = _ds.DiskState.after_write(self.path, out, manifest, new)
        self.outcome = "written"
        return True


def prepare(project) -> Optional[CacheJob]:
    """A job writing ``project``'s pending definition caches into the file
    it was loaded from or last saved to -- None when there is nothing to
    write, no such file, or it is read-only. Call on the thread that owns
    the project; :meth:`CacheJob.run` may then run on any thread."""
    st = getattr(project, "_disk", None)
    path = project.path
    if not path or st is None or not st.is_for(path):
        return None
    if not os.access(path, os.W_OK) or \
            not os.access(os.path.dirname(os.path.abspath(path)) or ".",
                          os.W_OK):
        return None
    caches = pending(project)
    if not caches:
        return None
    heads = {}
    for path_ in caches:
        sid, bid = _is_cache(path_)
        heads[(sid, bid)] = project.set(sid).branch(bid).head
    return CacheJob(project, path, st, caches, heads)


def persisted(project) -> bool:
    """Every definition cache the project holds is in its file too (as far
    as its disk record knows)."""
    return not pending(project)
