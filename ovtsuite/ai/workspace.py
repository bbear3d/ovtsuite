# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the AI tools act on.

:class:`FileWorkspace` opens files from disk -- a binary, a delivery container
(.sgo/.dim/.frf), a native container, or an ``.ovt`` project (one target per
binary set). It never writes any of them: the only thing an AI can produce is
a proposal in the inbox (:mod:`.proposals`).

The live workspace (the running app's open tabs) implements the same small
interface; see docs/AI_PLAN.md, phase 2.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import threading
from typing import Dict, List, Optional

from .describe import Index
from .tools import ToolError


class Target:
    """One thing the tools can look at: a file, or one set of a project.

    The session and the comparison load on first use: opening a project with
    four sets costs nothing until a tool asks about one of them."""

    def __init__(self, handle: str, *, kind: str, path: str, label: str,
                 project=None, sid: str = "", branch: str = "",
                 compare_path: Optional[str] = None, workspace=None):
        self.handle = handle
        self.kind = kind                 # "file" | "project-set"
        self.path = path
        self.label = label
        self.project = project
        self.sid = sid
        self.branch = branch             # branch id (project sets)
        self.compare_path = compare_path
        self._ws = workspace
        self._session = None
        self._cmp = None
        self._cmp_loaded = False
        self._index = None
        self.cmp_label = ""
        self.notes: List[str] = []

    # -- loading ---------------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            self._session = self._ws._load(self)
        return self._session

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = Index(self.session)
        return self._index

    @property
    def comparison(self):
        """The comparison session, or None. A project set compares against its
        as-read original (Bear's rule for project tabs); a file against the
        file given as ``compare_with``."""
        if not self._cmp_loaded:
            self._cmp_loaded = True
            self._cmp = self._ws._load_comparison(self)
        return self._cmp

    def counterpart(self, e):
        cmp = self.comparison
        if cmp is None:
            return None
        try:
            return self.session.counterpart(cmp, e)
        except Exception:
            return None

    def set_comparison(self, path: Optional[str]) -> None:
        self.compare_path = path
        self._cmp, self._cmp_loaded, self.cmp_label = None, False, ""

    # -- identity (what a proposal is tied to) ---------------------------------
    def identity(self) -> dict:
        d = {"kind": self.kind, "path": self.path, "label": self.label}
        if self.kind == "project-set":
            s = self.project.set(self.sid)
            b = s.branch(self.branch)
            d.update(project_uid=self.project.uid, set_id=self.sid,
                     project_name=self.project.name,
                     set_name=s.name, branch_id=b.id, branch_name=b.name,
                     base_checkpoint=b.head)
        else:
            d["sha256"] = file_sha256(self.path)
        return d

    def describe(self) -> dict:
        d = {"handle": self.handle, "label": self.label, "kind": self.kind,
             "path": self.path, "loaded": self._session is not None}
        if self.kind == "project-set":
            s = self.project.set(self.sid)
            d["set"] = s.name
            d["branch"] = s.branch(self.branch).name
            d["platform"] = s.platform
            d["comparison"] = "as-read original"
        elif self.compare_path:
            d["comparison"] = self.compare_path
        else:
            d["comparison"] = None
        return d


#: (path) -> ((size, mtime_ns), sha256): a proposal's target is matched on
#: the hash of the file's bytes, and the app asks after every open tab
_SHA_CACHE: Dict[str, tuple] = {}
_SHA_LOCK = threading.Lock()


def file_sha256(path: str) -> str:
    """SHA-256 of the file, worked out once per version (size + mtime)."""
    try:
        st = os.stat(path)
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = None
    key = os.path.normcase(os.path.abspath(path))
    if stamp is not None:
        with _SHA_LOCK:
            hit = _SHA_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    out = h.hexdigest()
    if stamp is not None:
        with _SHA_LOCK:
            if len(_SHA_CACHE) > 256:
                _SHA_CACHE.clear()
            _SHA_CACHE[key] = (stamp, out)
    return out


class FileWorkspace:
    """Targets opened from disk, read-only."""

    live = False

    def __init__(self, progress=None):
        self.targets: Dict[str, Target] = {}
        self._n = 0
        self._tmp = None
        self._progress = progress

    # -- public ----------------------------------------------------------------
    def open(self, path: str, *, set_name: Optional[str] = None,
             branch: Optional[str] = None,
             compare_with: Optional[str] = None) -> List[Target]:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise ToolError("no such file: %s" % path)
        if compare_with:
            compare_with = os.path.abspath(os.path.expanduser(compare_with))
            if not os.path.isfile(compare_with):
                raise ToolError("no such comparison file: %s" % compare_with)
        # already open: hand back the same target(s)
        same = [t for t in self.targets.values() if t.path == path
                and (set_name is None or self._set_matches(t, set_name))]
        if same and not compare_with:
            return same
        from ..project import archive as _ar
        if _ar.is_project(path):
            return self._open_project(path, set_name, branch)
        if set_name or branch:
            raise ToolError("set/branch only apply to .ovt projects")
        t = Target(self._handle(), kind="file", path=path,
                   label=os.path.basename(path), compare_path=compare_with,
                   workspace=self)
        self.targets[t.handle] = t
        return [t]

    def get(self, handle: str) -> Target:
        t = self.targets.get(handle)
        if t is None:
            if not self.targets:
                raise ToolError("nothing is open: call open_file first")
            raise ToolError("no open target %r; open ones: %s"
                            % (handle, ", ".join(self.targets)))
        return t

    def all(self) -> List[Target]:
        return list(self.targets.values())

    def close(self, handle: str) -> None:
        self.targets.pop(handle, None)

    def view(self) -> dict:
        """No app window: there is no "current" tab."""
        return {"live": False}

    # -- internals -------------------------------------------------------------
    def _handle(self) -> str:
        self._n += 1
        return "h%d" % self._n

    @staticmethod
    def _set_matches(t: Target, name: str) -> bool:
        if t.kind != "project-set":
            return False
        s = t.project.set(t.sid)
        return name in (s.id, s.name) or s.name.lower() == name.lower()

    def _tmpdir(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="ovt-ai-")
            atexit.register(shutil.rmtree, self._tmp, True)
        return self._tmp

    def _open_project(self, path, set_name, branch) -> List[Target]:
        from ..project.project import Project
        try:
            proj = Project.load(path)
        except Exception as ex:
            raise ToolError("could not open project %s: %s" % (path, ex))
        sets = proj.sets
        if set_name:
            sets = [s for s in sets if set_name in (s.id, s.name)
                    or s.name.lower() == set_name.lower()]
            if not sets:
                raise ToolError("project has no set %r; sets: %s" % (
                    set_name, ", ".join(s.name for s in proj.sets)))
        out = []
        for s in sets:
            b = s.branch(branch) if branch else s.active
            if b is None:
                if branch:
                    raise ToolError("set %s has no branch %r; branches: %s" % (
                        s.name, branch, ", ".join(x.name for x in s.branches)))
                continue
            t = Target(self._handle(), kind="project-set", path=path,
                       label="%s / %s [%s]" % (proj.name or os.path.basename(path),
                                               s.name, b.name),
                       project=proj, sid=s.id, branch=b.id, workspace=self)
            self.targets[t.handle] = t
            out.append(t)
        if not out:
            raise ToolError("the project has no binary sets")
        return out

    def _session_for_path(self, path: str):
        from ..session import TunerSession
        try:
            return TunerSession(path, progress=self._progress)
        except Exception as ex:
            raise ToolError("could not open %s: %s" % (os.path.basename(path), ex))

    def _load(self, t: Target):
        if t.kind == "file":
            return self._session_for_path(t.path)
        from ..project import sessionbridge as _sb
        proj, sid = t.project, t.sid
        s = proj.set(sid)
        b = s.branch(t.branch)
        state = proj.state(sid, b.head)
        log = _sb.history_text(proj, sid, b.head)
        ext, data = _sb.working_file(
            proj, sid, state, defn=_sb.definition_for(proj, sid, b.id, state),
            log=log)
        work = _sb.write_working_file(self._tmpdir(), s.name, ext, data, log=log)
        session = self._session_for_path(work)
        note = apply_definition(proj, sid, session)
        if note:
            t.notes.append(note)
        return session

    def _load_comparison(self, t: Target):
        if t.kind == "file":
            if not t.compare_path:
                return None
            t.cmp_label = os.path.basename(t.compare_path)
            return self._session_for_path(t.compare_path)
        # project set: the as-read original of the branch's lineage
        from ..patch import _session_from_bytes
        from ..project import sessionbridge as _sb
        proj, sid = t.project, t.sid
        s = proj.set(sid)
        b = s.branch(t.branch)
        root = s.root_of(b.head)
        if root is None or root.id == b.head:
            t.cmp_label = "as-read original (same as current)"
        else:
            t.cmp_label = "as-read original"
        main = _sb._main_member(proj, sid)
        data = proj.state(sid, root.id if root else b.head).get(main.id) if main else None
        if not data:
            return None
        try:
            return _session_from_bytes(bytes(data), t.session)
        except Exception as ex:
            t.notes.append("comparison (as-read original) could not be loaded: %s" % ex)
            return None


def apply_definition(proj, sid, session) -> str:
    """A packaged third-party definition (XDF ...) on the set's main member,
    then the tables resized in the set, then the user's own parameters,
    applied the way the app's project tabs apply them (gui_project) -- so
    the assistant reads and writes a resized table at its real size.
    Returns a note, "" when there is none."""
    note = _apply_packaged(proj, sid, session)
    from .. import resize as _rz
    try:
        lay = _rz.apply_from_project(proj, sid, session)
    except Exception as ex:
        lay = "the resized tables could not be read: %s" % ex
    from .. import userparams as _up
    try:
        more = _up.apply_from_project(proj, sid, session)
    except Exception as ex:
        more = "the user parameters could not be read: %s" % ex
    return "; ".join(x for x in (note, lay, more) if x)


def _apply_packaged(proj, sid, session) -> str:
    """The packaged definition, placed with the options and reference image
    the project keeps. Returns a note: why it could not be applied, or --
    when some of its tables moved or are left out as unverified -- how it
    was placed; "" otherwise."""
    from ..project import sessionbridge as _sb
    note = _sb.apply_packaged_definition(proj, sid, session)
    pl = getattr(session, "definition_placement", None)
    if pl is None:
        return note                     # none, or it could not be read
    c = pl.counts()
    if c["unverified"] or c["moved"] or c["region"]:
        return "definition placed: " + pl.summary()
    return ""
