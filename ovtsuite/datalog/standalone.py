# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Host for the data logger when it runs on its own (``ovtsuite --logger``).

DataLogDialog was written against the main window: each connection pane names
its measuring-block cells from the open tab whose binary matches its controller
(``workspaces()``, matched on ``platform``), falling back to the tab in front
(``current`` / ``primary``). This object provides that same small surface
without a main window. Binaries are opened in the dialog (Open binary...) and
held one per platform -- the same rule the panes match by -- so an engine file
and a transmission file can name a dual log's two panes at once, and opening a
second file for the same controller replaces the first rather than leaving the
pane to pick between them.

An ``.ovt`` project opens too: which binary (and branch) is asked when it
holds more than one, and a log recorded against it goes into that project
when logging stops -- unless the main app has the project open, in which
case the log stays a file (and that is said).
"""

import os
import threading
import types

from PySide6 import QtCore, QtWidgets

from .. import recent as _recent


class _KeepRelay(QtCore.QObject):
    """Carries a finished keep from its worker thread to the GUI thread."""
    finished = QtCore.Signal(object, object)        # job, (done, reason, saved_as)

    def __init__(self):
        super().__init__()
        self.finished.connect(self._deliver)

    @QtCore.Slot(object, object)
    def _deliver(self, job, result):
        job(result)


class StandaloneLogHost:
    """Stands in for the main window as DataLogDialog's ``window``."""

    standalone = True

    def __init__(self):
        self._spaces = []           # one tab-like holder per platform, newest last
        # One .ovt rewrite at a time: a dual stop keeps two logs, often into
        # the same project, and each update loads, changes and rewrites the
        # whole file -- run side by side, the second would drop the first.
        self._keep_lock = threading.Lock()
        self._relay = None

    # -- what the panes read -------------------------------------------------
    def workspaces(self):
        return list(self._spaces)

    @property
    def current(self):
        return self._spaces[-1] if self._spaces else None

    @property
    def primary(self):
        ws = self.current
        return ws.primary if ws is not None else None

    def names(self):
        """The open binaries' file names, oldest first, for the dialog's label."""
        return [ws.primary.binfile.name for ws in self._spaces]

    # -- opening a binary ----------------------------------------------------
    def open_path(self, path, parent=None):
        """Load ``path`` to name cells from. True when it loaded; on failure the
        reason is shown and whatever was open before stays open."""
        from ..session import TunerSession
        from ..project import is_project
        ref = None
        if is_project(path):
            from .. import gui_ovt_file as _of
            got = _of.open_version(
                path, parent, title="Name the cells from a project",
                text="Which binary of the project names the cells? (A log "
                     "recorded against it goes into the project.)",
                branches_only=True)
            if got is None:
                return False
            session = got.session
            ref = types.SimpleNamespace(
                doc=types.SimpleNamespace(
                    title=got.project.name or os.path.splitext(
                        os.path.basename(path))[0],
                    tmpdir=got.tmpdir, path=got.path),
                set=got.set, sid=got.sid, bid=got.bid)
        else:
            try:
                session = TunerSession(path)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(parent, "Open binary",
                                               "%s could not be loaded:\n%s"
                                               % (os.path.basename(path), ex))
                return False
        plat = (getattr(session, "platform", None) or "").lower()
        self._spaces = [ws for ws in self._spaces
                        if (getattr(ws.primary, "platform", None) or "").lower() != plat]
        self._spaces.append(types.SimpleNamespace(primary=session,
                                                  project_ref=ref, win=None))
        _recent.set_last_dir(os.path.dirname(os.path.abspath(path)))
        return True

    # -- a finished log --------------------------------------------------------
    def _keep_now(self, ref, path):
        """The keep itself, no dialogs (any thread): ``(done, reason,
        saved_as)``; when not done the log is copied beside the project and
        ``saved_as`` names the copy."""
        from .. import gui_ovt_file as _of
        with open(path, "rb") as fh:
            data = fh.read()
        name = os.path.basename(path)
        with self._keep_lock:
            done, reason = _of.update_project_quiet(
                ref.doc.path, lambda p: p.add_log(name, data, set_id=ref.sid))
        saved_as = None
        if not done:
            keep = os.path.join(os.path.dirname(ref.doc.path), name)
            try:
                import shutil
                shutil.copyfile(path, keep)
                saved_as = keep
            except OSError:
                pass
        return done, reason, saved_as

    def _say_keep(self, ref, result, parent):
        from .. import gui_ovt_file as _of
        done, reason, saved_as = result
        if done:
            return True
        _of.say_update_result((done, reason), ref.doc.path, parent,
                              title="Data logging", what="the log")
        if saved_as:
            QtWidgets.QMessageBox.information(
                parent, "Data logging", "The log was saved as %s instead."
                % saved_as)
        return False

    def keep_log_async(self, ws, path, parent=None, done=None):
        """:meth:`keep_log` on a worker thread, so Stop returns at once: the
        whole .ovt is loaded and rewritten, which on a big project is
        seconds. ``done(kept)`` is called on the GUI thread afterwards (any
        message box first). Returns False when there is nothing to keep (and
        ``done`` is not called), else None: the answer comes later."""
        ref = getattr(ws, "project_ref", None)
        if ref is None or not path or not os.path.exists(path):
            return False
        if self._relay is None:
            self._relay = _KeepRelay()

        def finish(result):
            owner = parent
            try:
                import shiboken6
                if owner is not None and not shiboken6.isValid(owner):
                    owner = None            # the logger closed meanwhile
            except ImportError:
                pass
            kept = self._say_keep(ref, result, owner)
            if done is not None:
                done(kept)

        def work():
            try:
                result = self._keep_now(ref, path)
            except Exception as ex:                 # unreadable working copy
                result = (False, str(ex), None)
            self._relay.finished.emit(finish, result)

        # not a daemon: a log half-written into the .ovt must be finished
        threading.Thread(target=work, name="ovt-keep-log").start()
        return None

    def keep_log(self, ws, path, parent=None):
        """Put the log at ``path`` (a working copy) into the project ``ws``
        came from. True when it went in; otherwise it is copied beside the
        project so it is not lost, and the user is told where."""
        ref = getattr(ws, "project_ref", None)
        if ref is None or not path or not os.path.exists(path):
            return False
        return self._say_keep(ref, self._keep_now(ref, path), parent)
