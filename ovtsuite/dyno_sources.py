# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a dyno run comes from (:mod:`ovtsuite.gui_dyno`).

A :class:`Source` is one curve beside the tab's own: a version of this file,
another open tab, a branch or version of any open project, a file on disk,
the tab's own curve pinned at a moment, or a data log. :class:`Resolver`
turns each into a session the dyno model reads (once, kept until its file
changes), and the ``*_choices`` functions list what the window's **Add**
menu offers.

A version of THIS file reuses the tab's definition (``dyno.session_of``);
anything else -- another software, perhaps the same size -- is read with
its own (the open tab's session, or the file loaded afresh), never this
file's.
"""

from __future__ import annotations

import itertools
import os
import tempfile
import time
import weakref
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import dyno as _dy

#: source kinds
VERSION, TAB, PROJECT, FILE, PINNED, LOG = (
    "version", "tab", "project", "file", "pinned", "log")
#: kinds kept with a project (the others name things that do not last)
LASTING = (VERSION, PROJECT, FILE, LOG)

#: at most this many runs besides the tab's own and its reference (the
#: chart has eight colours, :data:`ovtsuite.gui_dyno.RUN_COLOURS`)
MAX_SOURCES = 6

_ids = itertools.count(1)


@dataclass
class Source:
    """One run beside the tab's own curve."""
    kind: str
    label: str
    #: a line on where it comes from (tooltip, export)
    detail: str = ""
    #: its colour (an index into the run colours; kept when others go)
    slot: int = 2
    shown: bool = True
    key: str = field(default_factory=lambda: "s%d" % next(_ids))
    # -- what it names (by kind) --
    vid: str = ""                    # VERSION: a version of this file
    tab: object = None               # TAB: weakref to the tab
    path: str = ""                   # PROJECT / FILE / LOG (a log file)
    sid: str = ""                    # PROJECT: set, branch, checkpoint
    bid: str = ""                    #   ("" checkpoint = the branch head)
    cid: str = ""
    data: Optional[bytes] = None     # PINNED: the bytes as they were
    log_id: str = ""                 # LOG kept in a project (with path)

    def to_dict(self) -> Optional[dict]:
        """What a project keeps of it (None for one that does not last)."""
        if self.kind not in LASTING:
            return None
        d = {"kind": self.kind, "label": self.label, "detail": self.detail,
             "slot": self.slot, "shown": self.shown}
        for k in ("vid", "path", "sid", "bid", "cid", "log_id"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Optional["Source"]:
        try:
            if d.get("kind") not in LASTING:
                return None
            return cls(kind=str(d["kind"]), label=str(d.get("label", "")),
                       detail=str(d.get("detail", "")),
                       slot=int(d.get("slot", 2)), shown=bool(d.get("shown", True)),
                       **{k: str(d.get(k, "")) for k in
                          ("vid", "path", "sid", "bid", "cid", "log_id")})
        except (KeyError, TypeError, ValueError):
            return None


def free_slot(sources, reserved=(0, 1)) -> Optional[int]:
    """The first colour no run has (None when all eight are taken)."""
    taken = set(reserved) | {s.slot for s in sources}
    return next((i for i in range(8) if i not in taken), None)


# ============================================================= resolving
class Resolver:
    """Sessions for the sources of tab ``ws``, kept while their file is the
    same."""

    def __init__(self, ws=None):
        self._ws = weakref.ref(ws) if ws is not None else (lambda: None)
        self._cache: Dict[str, Tuple[object, object, str]] = {}
        self._projects: Dict[str, Tuple[float, object]] = {}
        self._runs: Dict[str, Tuple[object, list]] = {}

    def clear(self):
        self._cache.clear()
        self._runs.clear()

    def forget(self, key: str):
        self._cache.pop(key, None)
        self._runs.pop(key, None)

    # -- live state, for change tracking ------------------------------------
    @staticmethod
    def stamp(src: Source):
        """What changes when the source's bytes may have: an open tab's
        session and revision; a file's modification time."""
        if src.kind == TAB:
            w = src.tab() if src.tab is not None else None
            p = getattr(w, "primary", None)
            return (id(p), getattr(p, "revision", None))
        if src.kind in (FILE, LOG) and src.path and not src.log_id:
            try:
                return os.path.getmtime(src.path)
            except OSError:
                return None
        return None

    # -- sessions -----------------------------------------------------------
    def session(self, src: Source, primary, win=None) -> Tuple[object, str]:
        """``(session or None, why not)`` for ``src``; ``primary`` is the
        tab's own session (a version or a pin shares its definition)."""
        if src.kind == TAB:
            w = src.tab() if src.tab is not None else None
            p = getattr(w, "primary", None)
            return (p, "") if p is not None else (None, "the tab is closed")
        st = self.stamp(src)
        hit = self._cache.get(src.key)
        if hit is not None and hit[0] == st and hit[1] is not None:
            return hit[1], hit[2]
        try:
            sess, why = self._load(src, primary, win)
        except Exception as ex:          # a file that no longer reads
            sess, why = None, str(ex) or type(ex).__name__
        self._cache[src.key] = (st, sess, why)
        return sess, why

    def _load(self, src, primary, win):
        if src.kind == VERSION:
            data = _dy.version_bytes(primary, src.vid)
            if data is None:
                return None, "the version could not be rebuilt"
            return _dy.session_of(data, primary), ""
        if src.kind == PINNED:
            return _dy.session_of(src.data, primary), ""
        if src.kind == FILE:
            from .session import TunerSession
            return TunerSession(src.path), ""
        if src.kind == PROJECT:
            return self._project_session(src, primary, win)
        return None, "not a binary"

    def project(self, path, win=None):
        """The project at ``path``: the open one, else read from disk (kept
        until the file changes)."""
        doc = _open_doc(win, path)
        if doc is not None:
            return doc.project
        mt = os.path.getmtime(path)
        hit = self._projects.get(path)
        if hit is None or hit[0] != mt:
            from .project import Project
            hit = self._projects[path] = (mt, Project.load(path))
        return hit[1]

    def _project_session(self, src, primary, win):
        proj = self.project(src.path, win)
        from .project import sessionbridge as _sb
        main = _sb._main_member(proj, src.sid)
        if main is None:
            return None, "the set has no binary"
        cid = src.cid or proj.head(src.sid, src.bid or None).id
        state = proj.state(src.sid, cid)
        data = state.get(main.id)
        if data is None:
            return None, "the version holds no binary"
        ref = getattr(self._ws(), "project_ref", None)
        if ref is not None and ref.doc.path and os.path.normcase(ref.doc.path) == \
                os.path.normcase(os.path.abspath(src.path)) and ref.sid == src.sid:
            return _dy.session_of(bytes(data), primary), ""   # this set: same file
        ext, blob = _sb.working_file(proj, src.sid, state)
        fd, tmp = tempfile.mkstemp(suffix=ext, prefix="ovt-dyno-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            from .session import TunerSession
            return TunerSession(tmp), ""
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- logs ---------------------------------------------------------------
    def runs(self, src: Source, primary, win=None) -> Tuple[list, str]:
        """``(runs, why not)`` of a LOG source (:mod:`ovtsuite.logtune`)."""
        st = self.stamp(src)
        hit = self._runs.get(src.key)
        if hit is not None and hit[0] == st:
            return hit[1], ""
        try:
            path = src.path
            if src.log_id:
                proj = self.project(src.path, win)
                data = proj.log_bytes(src.log_id)
                a = next((x for x in proj.logs if x.id == src.log_id), None)
                d = tempfile.mkdtemp(prefix="ovt-dyno-log-")
                path = os.path.join(d, os.path.basename(getattr(a, "filename", "")
                                                        or "log.csv"))
                with open(path, "wb") as fh:
                    fh.write(data)
            namer = None
            try:
                from .datalog.channels import ChannelNamer
                namer = ChannelNamer(primary)
            except Exception:
                namer = None
            from .logtune.samples import runs_from_paths
            runs = runs_from_paths([path], namer)
        except Exception as ex:
            return [], str(ex) or type(ex).__name__
        self._runs[src.key] = (st, runs)
        return runs, ""


def _open_doc(win, path):
    if win is None or not path:
        return None
    try:
        from . import gui_project as _gp
        key = os.path.normcase(os.path.abspath(path))
        return next((d for d in _gp.docs(win) if d.path
                     and os.path.normcase(d.path) == key), None)
    except Exception:
        return None


# ============================================================== choices
def tab_choices(win, ws) -> List[Tuple[str, object]]:
    """``[(label, tab)]``: the other open tabs holding a file."""
    out = []
    for t in (win.workspaces() if win is not None else []):
        if t is ws or getattr(t, "primary", None) is None:
            continue
        try:
            label = t.tab_label() if hasattr(t, "tab_label") else t.primary.binfile.name
        except Exception:
            label = t.primary.binfile.name
        out.append((label.strip(), t))
    return out


def branch_versions(proj, sid: str, bid: str) -> List[object]:
    """The checkpoints of branch ``bid``, newest first."""
    s = proj.set(sid)
    b = s.branch(bid)
    out, cid, seen = [], b.head if b is not None else None, set()
    while cid and cid not in seen:
        seen.add(cid)
        c = s.checkpoint(cid)
        if c is None:
            break
        out.append(c)
        cid = c.parent
    return out


def project_choices(win) -> List[Tuple[str, str, object]]:
    """``[(title, path, project)]``: the open projects saved to a file."""
    out = []
    try:
        from . import gui_project as _gp
        for d in _gp.docs(win):
            if d.path:
                out.append((d.title, d.path, d.project))
    except Exception:
        pass
    return out


def log_choices(win) -> List[Tuple[str, str, str]]:
    """``[(label, project path, log id)]``: the logs the open projects keep,
    newest first in each."""
    out = []
    for title, path, proj in project_choices(win):
        names = {s.id: s.name for s in proj.sets}
        for a in sorted(proj.logs, key=lambda x: x.added or "", reverse=True):
            bits = [a.filename]
            if a.set and names.get(a.set):
                bits.append("(%s)" % names[a.set])
            out.append(("%s: %s" % (title, " ".join(bits)), path, a.id))
    return out


def checkpoint_label(c) -> str:
    when = (c.ts or "").replace("T", " ")[:16]
    name = c.label or c.kind
    return ("%s  %s" % (when, name)).strip()


def make_pin(session, when: Optional[float] = None) -> Source:
    t = time.strftime("%H:%M:%S", time.localtime(when or time.time()))
    return Source(PINNED, "Pinned %s" % t, "This file as it was at %s" % t,
                  data=bytes(session.binfile.data))


def make_tab(t) -> Source:
    label = t.primary.binfile.name
    return Source(TAB, label, "Open tab: %s (live)" % label, tab=weakref.ref(t))


__all__ = ["Source", "Resolver", "VERSION", "TAB", "PROJECT", "FILE", "PINNED",
           "LOG", "LASTING", "MAX_SOURCES", "free_slot", "tab_choices",
           "project_choices", "log_choices", "branch_versions",
           "checkpoint_label", "make_pin", "make_tab"]
