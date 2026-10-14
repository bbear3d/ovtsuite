# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Using an .ovt project from a standalone tool (the OVT Flasher, the OVT
Logger), with no main window.

Both tools open one binary of a project -- which set, branch and version is
asked when there is a choice (:class:`gui_files.VersionPicker`) -- and both
write something back into the file: the flasher a "last flashed" mark, the
logger a finished data log. A project open in the main app is locked by it
(gui_files.lock_project); writing to the file under it would be overwritten
by its next save, so :func:`update_project` refuses while that lock is held
and says so.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import types
from typing import Callable, Optional

from PySide6 import QtCore, QtWidgets

from . import branding


def open_version(path, parent=None, title="Open project", text=None,
                 branches_only=False):
    """Load ``path``, pick a set / branch / version (``branches_only``: a
    branch, at its last saved version) and open it as a session. Returns a
    namespace (``project``, ``sid``, ``bid``, ``cid``, ``set``, ``session``,
    ``state``, ``label``, ``path``, ``tmpdir``) or None when it failed
    (said) or was cancelled."""
    from .gui_files import VersionPicker
    from .project import Project
    from .project import sessionbridge as _sb
    from .session import TunerSession
    try:
        proj = Project.load(path)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(parent, title, "%s could not be "
                                       "opened:\n%s" % (os.path.basename(path), ex))
        return None
    pick = VersionPicker.only_choice(proj, branches_only=branches_only)
    if pick is None:
        dlg = VersionPicker(proj, "%s -- %s" % (
            title, proj.name or os.path.basename(path)), parent,
            branches_only=branches_only, text=text or "")
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dlg.choice:
            return None
        pick = dlg.choice
    sid, bid, cid = pick
    try:
        s = proj.set(sid)
        state = proj.state(sid, cid)
        ext, blob = _sb.working_file(proj, sid, state, defn=proj.cache(sid, bid))
        tmpdir = tempfile.mkdtemp(prefix="ovt-tool-")
        work = _sb.write_working_file(tmpdir, s.name, ext, blob)
        session = TunerSession(work)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(parent, title, "That version could not "
                                       "be rebuilt:\n%s" % ex)
        return None
    b = s.branch(bid)
    c = s.checkpoint(cid)
    ver = ("as read" if c.kind == "read" and c.parent is None
           else c.label or c.kind)
    label = "%s ▸ %s · %s (%s)" % (
        proj.name or os.path.splitext(os.path.basename(path))[0],
        s.name, b.name, ver)
    session.path = path
    session.binfile.name = label
    return types.SimpleNamespace(project=proj, sid=sid, bid=bid, cid=cid,
                                 session=session, state=state, label=label,
                                 path=os.path.abspath(path), tmpdir=tmpdir,
                                 set=s)


def _lock_file(path):
    from .gui_files import _lock_dir
    key = os.path.normcase(os.path.abspath(path))
    lf = QtCore.QLockFile(os.path.join(
        _lock_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".lock"))
    lf.setStaleLockTime(0)
    return lf


def is_open_elsewhere(path) -> bool:
    """True while the main app (another process) has the project open."""
    lf = _lock_file(path)
    if lf.tryLock(0):
        lf.unlock()
        return False
    return True


def update_project_quiet(path, change: Callable[[object], object]):
    """:func:`update_project` without any dialog, for a worker thread: returns
    ``(True, "")`` when saved, ``(None, "")`` when the main app has the
    project open, ``(False, reason)`` when it failed. The lock is taken and
    released on the calling thread."""
    from .project import Project
    lf = _lock_file(path)
    if not lf.tryLock(0):
        return None, ""
    try:
        proj = Project.load(path)
        change(proj)
        proj.save(path)
        return True, ""
    except Exception as ex:
        return False, str(ex)
    finally:
        lf.unlock()


def say_update_result(result, path, parent=None, title="Project",
                      what="the change"):
    """Tell the user why :func:`update_project_quiet` did not write (GUI
    thread). ``result`` is its ``(done, reason)``."""
    done, reason = result
    if done is None:
        QtWidgets.QMessageBox.information(
            parent, title, "%s is open in %s, so %s was not written to it "
            "here." % (os.path.basename(path), branding.APP_NAME, what))
    elif not done:
        QtWidgets.QMessageBox.warning(parent, title, "%s could not be written "
                                      "to %s:\n%s" % (what[:1].upper() + what[1:],
                                                      os.path.basename(path), reason))


def update_project(path, change: Callable[[object], object], parent=None,
                   title="Project", what="the change") -> Optional[bool]:
    """Load the .ovt at ``path``, apply ``change(project)`` and save it.
    True when saved; None when the main app has it open (said: ``what`` is
    not written); False when it failed (said)."""
    result = update_project_quiet(path, change)
    say_update_result(result, path, parent, title, what)
    return result[0]


__all__ = ["open_version", "update_project", "update_project_quiet",
           "say_update_result", "is_open_elsewhere"]
