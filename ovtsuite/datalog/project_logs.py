# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data logs kept in projects, for the logger's Log file tab.

A log recorded with "Keep the log in the project" lives inside the ``.ovt``
(project.logs), not as a file the user can browse to. The Log file tab lists
the logs of every project the logger can see -- the projects open in the main
window (including logs added since the last save), or, in the OVT Logger on
its own, the projects opened in it (read from disk, so a log it just wrote
into the file is there) -- and views one from a working copy.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Callable, List

_VIEW_DIR = None


@dataclass
class KeptLog:
    project: str                    # the project's title
    set_name: str                   # the set it was recorded against, or ""
    filename: str
    added: str                      # ISO time it was added, or ""
    pinned: bool
    read: Callable[[], bytes]       # the log's bytes
    key: str                        # unique per project and log

    @property
    def label(self) -> str:
        bits = [self.filename]
        if self.set_name:
            bits.append("(%s)" % self.set_name)
        if self.added:
            bits.append("-- %s" % self.added.replace("T", " ")[:16])
        return " ".join(bits)


def _logs_of(title: str, proj, key: str) -> List[KeptLog]:
    names = {s.id: s.name for s in proj.sets}
    out = []
    for a in proj.logs:
        out.append(KeptLog(title, names.get(a.set, "") if a.set else "",
                           a.filename, a.added or "", bool(a.pinned),
                           (lambda p=proj, i=a.id: p.log_bytes(i)),
                           "%s/%s" % (key, a.id)))
    out.sort(key=lambda k: k.added, reverse=True)       # newest first
    return out


def kept_logs(window) -> List[KeptLog]:
    """Every log kept in a project ``window`` (the main window, or the OVT
    Logger's host) has open, grouped by project, newest first in each."""
    out: List[KeptLog] = []
    try:
        from .. import gui_project as _gp
        docs = list(_gp.docs(window)) if window is not None else []
    except Exception:                   # no main window (standalone)
        docs = []
    for d in docs:
        out += _logs_of(d.title, d.project, "doc%d" % id(d))
    if docs:
        return out
    # the OVT Logger: the projects its binaries came from, as on disk
    seen = set()
    spaces = getattr(window, "workspaces", None)
    for ws in (spaces() if callable(spaces) else []):
        ref = getattr(ws, "project_ref", None)
        path = getattr(getattr(ref, "doc", None), "path", None)
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        try:
            from ..project import Project
            proj = Project.load(path)
        except Exception:
            continue
        out += _logs_of(ref.doc.title, proj, "file%d" % len(seen))
    return out


def working_copy(log: KeptLog) -> str:
    """The log written to a temporary file the viewer can read (named as it
    is in the project); its path."""
    global _VIEW_DIR
    if _VIEW_DIR is None or not os.path.isdir(_VIEW_DIR):
        _VIEW_DIR = tempfile.mkdtemp(prefix="ovt-logs-")
        import atexit
        import shutil
        atexit.register(shutil.rmtree, _VIEW_DIR, True)
    folder = os.path.join(_VIEW_DIR, log.key.replace("/", "_"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, os.path.basename(log.filename) or "log.csv")
    with open(path, "wb") as fh:
        fh.write(log.read())
    return path


__all__ = ["KeptLog", "kept_logs", "working_copy"]
