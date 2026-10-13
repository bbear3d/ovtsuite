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
import types

from PySide6 import QtWidgets

from .. import recent as _recent


class StandaloneLogHost:
    """Stands in for the main window as DataLogDialog's ``window``."""

    standalone = True

    def __init__(self):
        self._spaces = []           # one tab-like holder per platform, newest last

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
    def keep_log(self, ws, path, parent=None):
        """Put the log at ``path`` (a working copy) into the project ``ws``
        came from. True when it went in; otherwise it is copied beside the
        project so it is not lost, and the user is told where."""
        ref = getattr(ws, "project_ref", None)
        if ref is None or not path or not os.path.exists(path):
            return False
        from .. import gui_ovt_file as _of
        with open(path, "rb") as fh:
            data = fh.read()
        name = os.path.basename(path)
        done = _of.update_project(
            ref.doc.path, lambda p: p.add_log(name, data, set_id=ref.sid),
            parent, title="Data logging", what="the log")
        if done:
            return True
        keep = os.path.join(os.path.dirname(ref.doc.path), name)
        try:
            import shutil
            shutil.copyfile(path, keep)
            QtWidgets.QMessageBox.information(
                parent, "Data logging", "The log was saved as %s instead."
                % keep)
        except OSError:
            pass
        return False
