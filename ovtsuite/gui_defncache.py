# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Writing rebuilt definition snapshots into an open project's ``.ovt`` in
the background (:mod:`ovtsuite.project.cachewrite`) -- silently: no
"Saving..." box, nothing asked, nothing said when it is skipped. The
project's other unsaved changes stay unsaved."""

from __future__ import annotations

import threading

from PySide6 import QtCore

from . import gui_saving as _sv
from .project import cachewrite as _cw

#: how often a running write is looked at, to record it once done (ms)
POLL_MS = 50


def running(doc) -> bool:
    t = doc.__dict__.get("_defn_thread")
    return t is not None and t.is_alive()


def persist(win, doc, wait: bool = False) -> bool:
    """Write ``doc``'s definition caches the file lacks into it, on a worker
    thread (``wait``: here, now -- at exit). True when a write started (or,
    with ``wait``, was done)."""
    if not doc.path or getattr(doc.project, "_disk", None) is None:
        return False
    if running(doc):
        if wait:
            finish(doc)
        else:
            doc.__dict__["_defn_again"] = (win, )   # once this one is done
            return False
    if not wait and _sv.saving(win):
        return False                    # the save under way writes them
    job = _cw.prepare(doc.project)
    if job is None:
        _record(doc)
        return False
    if wait:
        job.run()
        _record(doc)
        return job.outcome == "written"
    t = threading.Thread(target=job.run, name="ovt-defn")   # not a daemon
    doc.__dict__["_defn_thread"] = t
    doc.__dict__["_defn_job"] = job
    doc.__dict__["_defn_win"] = win
    t.start()
    timer = QtCore.QTimer()
    timer.setInterval(POLL_MS)

    def poll():
        if t.is_alive():
            return
        timer.stop()
        timer.deleteLater()
        finish(doc)
    timer.timeout.connect(poll)
    timer.start()
    doc.__dict__["_defn_timer"] = timer     # (kept alive while it polls)
    return True


def finish(doc) -> None:
    """Wait for ``doc``'s write (if any) and record it."""
    t = doc.__dict__.pop("_defn_thread", None)
    if t is not None:
        t.join()
    job = doc.__dict__.pop("_defn_job", None)
    win = doc.__dict__.pop("_defn_win", None)
    doc.__dict__.pop("_defn_timer", None)
    _record(doc)
    if job is not None and win is not None and job.outcome == "written":
        _resnapshot(win, doc)
    again = doc.__dict__.pop("_defn_again", None)
    if again is not None:
        persist(again[0], doc)


def _resnapshot(win, doc) -> None:
    """A recovery entry made before the write names the file's old hash, so
    recovering it would warn that the file changed: take a fresh one."""
    saver = getattr(doc, "saver", None)
    if saver is None or getattr(saver, "_token", None) is None:
        return                          # no snapshot taken yet
    saver._token = None
    from . import gui_project as _gp
    _gp.autosave_soon(win, doc)


def _record(doc) -> None:
    """The file holds every snapshot it can: nothing left to write for one
    (``write_definitions``)."""
    if _cw.persisted(doc.project):
        doc.__dict__["_cache_dirty"] = False
