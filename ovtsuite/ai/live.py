# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The live workspace: the app's open tabs, plus files an AI opens from disk.

Each tab with a binary is a target with a stable handle (``t1``, ``t2`` ...)
for as long as the tab is open. Its session IS the tab's session, so tools see
unsaved edits exactly as the user does; the comparison is whatever the tab is
comparing against. Every call that touches a tab runs on the GUI thread (see
:mod:`.bridge`), so nothing here locks the session.

Duck-typed on the window (``workspaces()``, the ``current`` tab) and its tabs
(``primary``, ``comparison``, ``project_ref``, ``tab_label()``,
``is_dirty()``, ``_current_entity()``, ``tables()``) so it has no Qt import
and is tested without a GUI.
"""

from __future__ import annotations

import itertools
import os
import threading
import weakref
from typing import List, Optional

from .describe import Index
from .tools import ToolError
from .workspace import FileWorkspace, file_sha256


class TabTarget:
    def __init__(self, handle: str, ws):
        self.handle = handle
        self._ws = weakref.ref(ws)
        #: (key, Index): one attribute, so another thread swaps it whole
        self._index_pair = None
        #: the comparison's pairing for diff / changed_only (see pairs())
        self._pairs = None
        self._cps = None
        self._search = None
        self.notes: List[str] = []

    @property
    def ws(self):
        ws = self._ws()
        if ws is None or getattr(ws, "primary", None) is None:
            raise ToolError("tab %s has been closed" % self.handle)
        return ws

    # -- the Target interface (see workspace.Target) -----------------------------
    @property
    def ref(self):
        return getattr(self.ws, "project_ref", None)

    @property
    def kind(self) -> str:
        return "project-set" if self.ref is not None else "file"

    @property
    def project(self):
        r = self.ref
        return r.doc.project if r is not None else None

    @property
    def sid(self) -> str:
        r = self.ref
        return r.sid if r is not None else ""

    @property
    def branch(self) -> str:
        r = self.ref
        return r.bid if r is not None else ""

    @property
    def path(self) -> str:
        r = self.ref
        if r is not None:
            return r.doc.path or ""
        return getattr(self.ws.primary, "path", "") or ""

    @property
    def label(self) -> str:
        ws = self.ws
        try:
            return ws.tab_label()
        except Exception:
            return ws.primary.binfile.name

    @property
    def session(self):
        return self.ws.primary

    @property
    def comparison(self):
        return getattr(self.ws, "comparison", None)

    @property
    def cmp_label(self) -> str:
        c = self.comparison
        if c is None:
            return ""
        ver = getattr(self.ws, "cmp_version", None)
        if ver:
            return "saved version %s" % (ver.get("label") or ver.get("id", ""))
        return getattr(c.binfile, "name", "") or "comparison"

    def _index_key(self, s):
        return (id(s), id(s.entities), len(s.entities))

    @property
    def index(self) -> Index:
        s = self.session
        key = self._index_key(s)
        pair = self._index_pair
        if pair is None or pair[0] != key:
            pair = self._index_pair = (key, Index(s))
        return pair[1]

    def index_snapshot(self):
        """GUI thread: ``(index, entities, make)``. ``index`` is the current
        index or None; then ``make()`` builds it on any thread from
        ``entities`` (taken now) and keeps it."""
        s = self.session
        key = self._index_key(s)
        pair = self._index_pair
        if pair is not None and pair[0] == key:
            return pair[1], pair[1].ordered, None
        from .describe import all_entities
        ents = all_entities(s)

        def make():
            idx = Index(s, ents)
            self._index_pair = (key, idx)
            return idx
        return None, ents, make

    def _paired(self, slot, cmp, ents, pair):
        s = self.session
        key = (id(s), id(cmp), id(s.entities), id(cmp.entities), len(s.entities),
               len(cmp.entities), len(ents))
        hit = getattr(self, slot)
        if hit is not None and hit[0] == key:
            return hit[1]
        out = []
        for e in ents:
            try:
                cp = pair(e)
            except Exception:
                cp = None
            out.append((e, cp))
        setattr(self, slot, (key, out))
        return out

    def pairs(self, cmp):
        """GUI thread: ``[(comparison entity, its counterpart here or None)]``
        -- what ``diff`` compares. The pairing follows the definitions, not
        the values, so it is kept until either side's parameters change."""
        return self._paired("_pairs", cmp, list(cmp.entities),
                            lambda e: cmp.counterpart(self.session, e))

    def counterparts(self, cmp, ents):
        """GUI thread: ``[(e, its counterpart in the comparison or None)]``
        for this session's ``ents`` (``list_maps changed_only``)."""
        return self._paired("_cps", cmp, ents, lambda e: self.session.counterpart(cmp, e))

    def search_source(self):
        """GUI thread: a stand-in session for the search engine -- the entity
        lists, taken now -- kept per tab so the engine's index of it is
        reused while the parameters stay the same."""
        from .describe import all_entities
        s = self.session
        if self._search is None or self._search.of is not s:
            self._search = _Entities(s)
        ents = all_entities(s)
        self._search.entities = ents[:len(s.entities)]
        self._search.variants = ents[len(s.entities):]
        return self._search

    def counterpart(self, e):
        cmp = self.comparison
        if cmp is None:
            return None
        try:
            return self.session.counterpart(cmp, e)
        except Exception:
            return None

    def set_comparison(self, path):
        raise ToolError("%s is a tab in the app: change its comparison there "
                        "(File > Import as comparison)" % self.handle)

    def identity(self) -> dict:
        d = {"kind": self.kind, "path": self.path, "label": self.label, "tab": True}
        r = self.ref
        if r is not None:
            s, b = r.set, r.branch
            d.update(project_uid=r.doc.project.uid, set_id=r.sid, set_name=s.name,
                     project_name=r.doc.project.name or getattr(r.doc, "title", ""),
                     branch_id=b.id, branch_name=b.name, base_checkpoint=b.head)
        elif d["path"] and os.path.isfile(d["path"]):
            d["sha256"] = file_sha256(d["path"])
        d["unsaved_edits"] = bool(self.ws.is_dirty())
        return d

    def describe(self) -> dict:
        ws = self.ws
        d = {"handle": self.handle, "label": self.label, "kind": self.kind,
             "path": self.path, "tab": True, "loaded": True,
             "unsaved_edits": bool(ws.is_dirty()),
             "platform": getattr(ws.primary, "platform", ""),
             "comparison": self.cmp_label or None}
        r = self.ref
        if r is not None:
            d["set"] = r.set.name
            d["branch"] = r.branch.name
        return d


class _Entities:
    """What :class:`~ovtsuite.search.SearchEngine` reads of a session."""

    def __init__(self, session):
        self.of = session
        self.entities = []
        self.variants = []

    def variant_entities(self):
        return self.variants


class LiveWorkspace:
    """Open tabs (``t<n>``) first, then files opened from disk (``h<n>``)."""

    live = True

    def __init__(self, win):
        self.win = win
        self.files = FileWorkspace()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._tabs = {}                       # handle -> TabTarget
        self._handle_of = weakref.WeakKeyDictionary()
        #: what acts on the window (show_map ...): set by the GUI (gui_ai)
        self.ui = None

    # -- tabs -------------------------------------------------------------------
    def _sync(self) -> List[TabTarget]:
        """Targets for the tabs open now, in tab order; gone tabs dropped.
        GUI thread only (it walks the window)."""
        with self._lock:
            out = []
            for ws in self.win.workspaces():
                if getattr(ws, "primary", None) is None:
                    continue
                h = self._handle_of.get(ws)
                if h is None:
                    h = "t%d" % next(self._ids)
                    self._handle_of[ws] = h
                    self._tabs[h] = TabTarget(h, ws)
                out.append(self._tabs[h])
            live = {t.handle for t in out}
            for h in [h for h in self._tabs if h not in live]:
                del self._tabs[h]
            return out

    def is_tab(self, handle: str) -> bool:
        return isinstance(handle, str) and handle.startswith("t")

    def tab_for(self, ws) -> Optional[str]:
        self._sync()
        return self._handle_of.get(ws)

    # -- the workspace interface --------------------------------------------------
    def open(self, path, **kw):
        return self.files.open(path, **kw)

    def get(self, handle: str):
        if self.is_tab(handle):
            self._sync()
            t = self._tabs.get(handle)
            if t is None:
                raise ToolError("no open tab %r; open ones: %s" % (
                    handle, ", ".join(self._tabs) or "none"))
            return t
        if handle in self.files.targets:
            return self.files.get(handle)
        known = list(self._tabs) + list(self.files.targets)
        raise ToolError("no open target %r; open ones: %s"
                        % (handle, ", ".join(known) or "none"))

    def all(self):
        return list(self._sync()) + self.files.all()

    def view(self) -> dict:
        """What the user is looking at: the active tab and its parameter."""
        tabs = self._sync()
        cur = getattr(self.win, "current", None)     # a property on MainWindow
        if callable(cur) and not hasattr(cur, "primary"):
            cur = cur()
        h = self._handle_of.get(cur) if cur is not None else None
        d = {"live": True, "active_tab": h, "tabs": [t.handle for t in tabs]}
        if h is not None:
            t = self._tabs[h]
            e = None
            try:
                e = cur._current_entity()
            except Exception:
                pass
            if e is None:
                try:
                    tb = [x for x in cur.tables() if not getattr(x, "pinned", True)] \
                        or cur.tables()
                    e = getattr(tb[0], "entity", None) if tb else None
                except Exception:
                    e = None
            if e is not None:
                d["selected_map"] = t.index.eid(e)
            try:
                d["open_tables"] = [t.index.eid(x.entity) for x in cur.tables()
                                    if getattr(x, "entity", None) is not None]
            except Exception:
                pass
        return d
