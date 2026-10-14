# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only tabs: a saved version of a project set, opened to look at.

Project panel > a version > Open this version (read-only) opens it in a tab
of its own that is not part of the project: its tables can't be edited, the
editing and saving actions are off while it is in front, it never asks about
unsaved changes, and it is not added to Open Recent nor remembered with the
open tabs. To change it, revert a branch to it or start a branch from it.

The window code reaches this module through a few one-line hooks
(``Workspace.view_only``; ``MainWindow.adopt_session(..., view_only=...)``;
``restrict_actions`` from ``_refresh_enabled``).
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

#: Menu actions that change the binary or save it: off on a read-only tab.
EDIT_ACTIONS = frozenset((
    "Save", "Save this branch only", "Save As...", "Undo", "Redo",
    "Scale selection...", "Paste cells", "Apply patch...", "Remove patch...",
    "Version history...", "Generate patch from history...",
    "Definition file for this binary (XDF, A2L, KP, OLS, DAM)...",
    "DTC / Fault-Path editor...", "Gear ratios...", "VSS pulse settings...",
    "Hex editor...", "EEPROM editor...", "Tune from logs...",
))


def is_view_only(ws) -> bool:
    return bool(getattr(ws, "view_only", None))


def _text(action) -> str:
    return action.text().split("\t")[0].replace("&&", "\0").replace(
        "&", "").replace("\0", "&").strip()


def restrict_actions(win, ws) -> None:
    """After the window enabled its actions for tab ``ws``: switch the
    editing ones off when it is read-only (and back on, if they are not
    the window's to manage, once another tab is in front)."""
    managed = set(getattr(win, "_needs_file", []))
    managed |= {a for a, _c in getattr(win, "_conditional", [])}
    for a in win.__dict__.pop("_vo_disabled", []):
        if a not in managed:
            a.setEnabled(True)
    if not is_view_only(ws):
        return
    off = []
    for a in win.actions():
        if _text(a) in EDIT_ACTIONS and a.isEnabled():
            a.setEnabled(False)
            off.append(a)
    win.__dict__["_vo_disabled"] = off


def refuse_save(ws) -> bool:
    """Save on a read-only tab: say why not. Always False."""
    QtWidgets.QMessageBox.information(
        ws, "Read-only version",
        "%s is a saved version, opened read-only: it can't be saved or "
        "changed.\n\nTo work on it, use Revert the branch to this version or "
        "New branch from this version in the Project panel; to keep a copy, "
        "use File > Export." % ws.display_name())
    return False


def title(doc, s, c) -> str:
    """``<project> – <set> @ <version> (read-only)``."""
    ver = ("as read" if c.kind == "read" and c.parent is None
           else c.label or c.kind)
    return "%s – %s @ %s (read-only)" % (doc.title, s.name, ver)


def open_version(win, doc, sid, bid, cid):
    """Open checkpoint ``cid`` of set ``sid`` in a read-only tab of its own.
    Returns the tab, or None."""
    from . import gui_project as _gp
    from .project import sessionbridge as _sb
    proj = doc.project
    s = proj.set(sid)
    c = s.checkpoint(cid)
    if c is None:
        return None
    name = title(doc, s, c)
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        state = proj.state(sid, cid)
        ext, blob = _sb.working_file(proj, sid, state, defn=proj.cache(sid, bid))
        work = _sb.write_working_file(doc.tmpdir, "%s @ %s" % (s.name, cid[:8]),
                                      ext, blob)
    except Exception as ex:
        QtWidgets.QApplication.restoreOverrideCursor()
        QtWidgets.QMessageBox.critical(
            win, "Open version", "That version could not be rebuilt: %s" % ex)
        return None
    QtWidgets.QApplication.restoreOverrideCursor()
    prev = win.current
    host = prev if prev is not None else win._add_workspace()
    try:
        loaded = host._load_session(work, title="Opening %s" % name)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "Open version", str(ex))
        return None
    _gp._apply_definition(proj, sid, loaded)
    loaded.binfile.name = name
    info = {"project": doc.path, "set": sid, "branch": bid, "checkpoint": cid,
            "workfile": work}
    ws = win.adopt_session(loaded, work, view_only=info)
    if prev is not None and prev is not ws and prev.is_empty():
        win._remove_workspace(prev)
    win.show_workspace(ws)
    ws.statusBar().showMessage(
        "Opened %s read-only -- nothing in this tab can be saved." % name, 8000)
    return ws
