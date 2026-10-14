# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Snapshots: what a tool computes from, taken from an open tab in an
instant so the work itself runs off the GUI thread.

A tab's session belongs to the GUI thread: the user edits it there. Tools
that read a lot of it (``diff`` over every parameter, ``list_maps
changed_only``, a checksum verify) used to run there whole and froze the
window. Now they take a :class:`Frozen` on the GUI thread -- the entity list
and an immutable copy of the bytes, which costs a memory copy -- and decode
and compare on the caller's thread, against copies of the entities that read
the copied bytes. Edits made meanwhile are not seen (the tool answers for
the moment it was asked), and nothing the GUI thread owns is written.
"""

from __future__ import annotations

import copy
import threading
from typing import Dict, Iterable


class Frozen:
    """Entities re-pointed at immutable copies of their binaries' bytes.

    :meth:`add` runs on the GUI thread (it reads the live bytes); everything
    else on any thread."""

    def __init__(self):
        self._bfs: Dict[int, object] = {}         # id(live binfile) -> copy
        self._ents: Dict[int, object] = {}        # id(live entity) -> copy
        self._keep = []                           # the live objects, so ids stay theirs
        self._lock = threading.Lock()

    # -- GUI thread ---------------------------------------------------------------
    def add_binfile(self, bf) -> None:
        if bf is None or id(bf) in self._bfs:
            return
        nb = copy.copy(bf)
        nb.data = bytes(bf.data)                  # immutable: a write would raise
        self._bfs[id(bf)] = nb
        self._keep.append(bf)

    def add(self, entities: Iterable) -> None:
        """The binaries ``entities`` read from (usually one)."""
        for e in entities:
            if e is not None:
                self.add_binfile(getattr(e, "binfile", None))

    # -- any thread -----------------------------------------------------------------
    def binfile(self, live_bf):
        return self._bfs.get(id(live_bf), live_bf)

    def entity(self, e):
        """A copy of ``e`` reading the snapshot's bytes (``e`` itself for
        None or a binary that was not added)."""
        if e is None:
            return None
        with self._lock:
            n = self._ents.get(id(e))
            if n is None:
                bf = self._bfs.get(id(getattr(e, "binfile", None)))
                if bf is None:
                    return e
                n = copy.copy(e)
                n.binfile = bf
                self._ents[id(e)] = n
                self._keep.append(e)
            return n


def on_gui(ctx, handle, fn):
    """Run ``fn`` where ``handle``'s target may be touched: through
    ``ctx.on_gui`` when it is a tab in the app (and the tool runs off the
    GUI thread), else right here."""
    run = getattr(ctx, "on_gui", None)
    is_tab = getattr(ctx.workspace, "is_tab", None)
    if run is not None and handle and is_tab is not None and is_tab(handle):
        return run(fn)
    return fn()
