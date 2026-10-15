# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Project panel > right-click a branch or version > **Compare with
project**: a binary version from ANOTHER project as that branch's
comparison.

The submenu lists the other open projects and the recent ones, plus
"Browse...". Picking one opens the branch's tab (if it is not open) and
hands over to :func:`ovtsuite.gui_files.compare_with_file`, which asks which
of the project's binaries and versions (same kind of binary only) and loads
it -- live, when that branch is open in a tab at its latest version."""

from __future__ import annotations

import os
from typing import List, Tuple

from PySide6 import QtWidgets


def candidates(win, doc) -> List[Tuple[str, str]]:
    """``[(label, path)]`` of projects to offer: the open ones first, then
    recent ones, never ``doc`` itself, each once."""
    from . import gui_project as _gp
    from .recent import load_recent_projects
    own = os.path.normcase(os.path.abspath(doc.path)) if doc.path else None
    seen, out = set(), []

    def add(label, path):
        key = os.path.normcase(os.path.abspath(path))
        if key == own or key in seen:
            return
        seen.add(key)
        out.append((label, path))

    for d in _gp.docs(win):
        if d is not doc and d.path:
            add("%s  (open)" % d.title, d.path)
    try:
        recent = load_recent_projects()
    except Exception:
        recent = []
    for p in recent:
        if os.path.isfile(p):
            add(os.path.splitext(os.path.basename(p))[0], p)
    return out


def compare(win, doc, sid, bid, path) -> bool:
    """Open branch ``bid`` of set ``sid`` (or bring its tab forward) and
    compare it with a version from the project at ``path``."""
    from . import gui_files as _gf
    from . import gui_project as _gp
    ws = _gp.open_branch(win, doc, sid, bid)
    if ws is None:
        return False
    return _gf.compare_with_file(win, path, ws=ws)


def browse(win, doc, sid, bid) -> bool:
    from . import gui_files as _gf
    paths = _gf._pick(win, "Compare with project", "Projects (*.ovt)")
    return bool(paths) and compare(win, doc, sid, bid, paths[0])


def add_menu(menu: QtWidgets.QMenu, win, doc, sid, bid) -> QtWidgets.QMenu:
    """The **Compare with project** submenu, for branch ``bid`` of ``sid``."""
    sub = menu.addMenu("Compare with project")
    sub.setToolTipsVisible(True)
    for label, path in candidates(win, doc)[:15]:
        a = sub.addAction(label.replace("&", "&&"),
                          lambda p=path: compare(win, doc, sid, bid, p))
        a.setToolTip(path)
    if sub.actions():
        sub.addSeparator()
    sub.addAction("Browse...", lambda: browse(win, doc, sid, bid))
    return sub
