# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Open a log, patch or attachment kept in a project in another program,
without exporting it, and take the edits back.

Project panel > right-click > **Open externally** (default program for the
file type), **Open with...** (a program of your choice, remembered per file
type) or **Show copy in folder**; double-clicking an attachment opens it
externally.

The item is written to a temporary copy the user never has to name or clean
up (``<temp>/ovtsuite-external/<pid>-<n>/``). While it is open the app watches
the copy: when it is saved in the other program (size / time, then a hash,
checked every second -- editors that save by renaming a new file over the
old one are caught the same way) and the app is the active window, a prompt
offers **Update project copy** (replaces the item's contents, keeping its
name, set, pin and note) or **Not now**; "Always update" skips the prompt for
that file for the rest of the session. The panel marks an open item
"(open externally)" and an edited one "(edited externally)".

The copy is erased

* when the program closes, if the app launched it itself (**Open with...**:
  the process is followed; one that exits within :data:`HANDOFF_S` was only a
  launcher handing the file to a program already running, so that copy waits
  for the next rule);
* otherwise when the app closes, or the project closes, or the item is
  removed;
* by a sweep at the next start for what a crash or a locked file left behind.

An edit nobody has answered is asked about once before that: closing the
project or the app (either can be cancelled) offers **Update project copy**,
**Discard changes** or **Cancel**. The Update itself is one call,
``Project.replace_attachment_bytes``.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import gui_project as _gp

KINDS = ("log", "patch", "attachment")
POLL_MS = 1000
#: a launched program that ends sooner than this only handed the file on
HANDOFF_S = 5.0
ROOT_NAME = "ovtsuite-external"
_SETTINGS = "external/program/"
SB_NONE = QtWidgets.QMessageBox.StandardButton.NoButton


# ------------------------------------------------------------ temp folders
def root() -> str:
    return os.path.join(tempfile.gettempdir(), ROOT_NAME)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep(base: Optional[str] = None) -> int:
    """Erase the copies of runs that no longer exist (a crash, a copy the
    other program still held when the app closed). Returns how many folders
    went."""
    base = base or root()
    n = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        try:
            pid = int(name.split("-", 1)[0])
        except ValueError:
            continue
        if pid == os.getpid() or _alive(pid):
            continue
        shutil.rmtree(os.path.join(base, name), ignore_errors=True)
        n += not os.path.exists(os.path.join(base, name))
    return n


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ----------------------------------------------------------- one open copy
class OpenCopy:
    """One kept item written out and (possibly) open in another program."""

    def __init__(self, doc, kind: str, aid: str, folder: str, path: str,
                 data: bytes):
        self.doc, self.kind, self.aid = doc, kind, aid
        self.folder, self.path = folder, path
        self.sha = _sha(data)                    # what the project holds
        self.now = self.sha                      # the file, as last read
        self.size = len(data)
        self.sig = self._stat()
        self.settling: Optional[tuple] = None
        self.edited = False                      # differs from the project
        self.declined = ""                       # hash the user said "not now" to
        self.always = False
        self.proc: Optional[QtCore.QProcess] = None
        self.started = 0.0
        self.erase_failed = False

    def _stat(self) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def read(self) -> Optional[bytes]:
        try:
            with open(self.path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def name(self) -> str:
        return os.path.basename(self.path)

    def erase(self) -> bool:
        shutil.rmtree(self.folder, ignore_errors=True)
        self.erase_failed = os.path.exists(self.folder)
        return not self.erase_failed


class ExternalCopies(QtCore.QObject):
    """A main window's open copies (``win.__dict__["_ovt_external"]``)."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.copies: List[OpenCopy] = []
        self._seq = 0
        self._busy = False                       # a prompt is up
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        QtWidgets.QApplication.instance().applicationStateChanged.connect(
            self._state)

    # -- opening ------------------------------------------------------
    def find(self, doc, kind, aid) -> Optional[OpenCopy]:
        return next((o for o in self.copies
                     if o.doc is doc and o.kind == kind and o.aid == aid), None)

    def ensure(self, doc, kind, aid) -> Optional[OpenCopy]:
        """The item's temporary copy, written when it is not there yet."""
        o = self.find(doc, kind, aid)
        if o is not None and os.path.exists(o.path):
            return o
        if o is not None:
            self.copies.remove(o)
        try:
            a, raw = _gp._att(doc, kind, aid)
        except (KeyError, StopIteration):
            return None
        self._seq += 1
        folder = os.path.join(root(), "%d-%d" % (os.getpid(), self._seq))
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, a.filename)
            with open(path, "wb") as fh:
                fh.write(raw)
        except OSError as ex:
            QtWidgets.QMessageBox.warning(
                self.win, "Open externally",
                "The temporary copy could not be written:\n%s" % ex)
            shutil.rmtree(folder, ignore_errors=True)
            return None
        o = OpenCopy(doc, kind, aid, folder, path, raw)
        self.copies.append(o)
        self.timer.start()
        _gp.refresh_panel(self.win)
        return o

    def open_default(self, doc, kind, aid) -> bool:
        o = self.ensure(doc, kind, aid)
        if o is None:
            return False
        ok = QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(o.path))
        if not ok:
            QtWidgets.QMessageBox.information(
                self.win, "Open externally",
                "No program is set to open %s. Use Open with... to pick "
                "one." % o.name())
            self.drop(o)
        return ok

    def open_with(self, doc, kind, aid, program: str) -> bool:
        o = self.ensure(doc, kind, aid)
        if o is None:
            return False
        prog, args = launch_args(program, o.path)
        proc = QtCore.QProcess(self)
        proc.setProgram(prog)
        proc.setArguments(args)
        proc.finished.connect(lambda *_a, o=o: self._exited(o))
        proc.errorOccurred.connect(lambda _e, o=o: self._failed(o))
        o.proc, o.started = proc, time.monotonic()
        proc.start()
        return True

    def reveal(self, doc, kind, aid) -> bool:
        o = self.ensure(doc, kind, aid)
        if o is None:
            return False
        return QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(o.folder))

    # -- following the program ----------------------------------------
    def _failed(self, o: OpenCopy) -> None:
        proc = o.proc
        if proc is not None and proc.state() == QtCore.QProcess.ProcessState.NotRunning \
                and proc.exitStatus() == QtCore.QProcess.ExitStatus.NormalExit:
            return                              # a normal end: _exited has it
        QtWidgets.QMessageBox.warning(
            self.win, "Open with", "The program could not be started.")
        o.proc = None
        self.drop(o)

    def _exited(self, o: OpenCopy) -> None:
        """The program closed: a last look at the file, then the copy goes --
        unless that program only passed the file to one already running."""
        if o not in self.copies:
            return
        if time.monotonic() - o.started < HANDOFF_S:
            o.proc = None                       # a launcher: keep the copy
            return
        o.proc = None
        self.finish(o, can_cancel=False)

    # -- watching -----------------------------------------------------
    def _state(self, state) -> None:
        if state == QtCore.Qt.ApplicationState.ApplicationActive:
            QtCore.QTimer.singleShot(0, self.poll)

    def poll(self) -> None:
        """Look at every open copy once; offer the edits found."""
        for o in list(self.copies):
            if o.erase_failed:
                if o.erase():
                    self.copies.remove(o)
                continue
            self.look(o)
        for o in list(self.copies):
            if (o.edited and not self._busy and self.can_ask()
                    and o.declined != o.now):
                self.offer(o)
        if not self.copies:
            self.timer.stop()

    def look(self, o: OpenCopy, force: bool = False) -> bool:
        """Has the file changed from what the project holds? Sets
        ``o.edited``. A file still being written (its size or time moved
        since the last look) is judged at the next one, unless ``force``
        (the program has closed, or the app is)."""
        sig = o._stat()
        if sig is None:
            return o.edited
        if not force:
            if sig == o.sig and o.settling is None:
                return o.edited
            if sig != o.sig and o.settling != sig:
                o.settling = sig
                return o.edited
        data = o.read()
        if data is None:
            return o.edited                     # locked: try again
        o.sig, o.settling = sig, None
        o.now = _sha(data)
        o.edited = o.now != o.sha
        return o.edited

    def can_ask(self) -> bool:
        app = QtWidgets.QApplication.instance()
        return (not self._busy
                and app.applicationState()
                == QtCore.Qt.ApplicationState.ApplicationActive
                and app.activeModalWidget() is None)

    # -- asking -------------------------------------------------------
    def offer(self, o: OpenCopy) -> None:
        """A copy was saved in the other program."""
        if o.always:
            self.update(o)
            return
        self._busy = True
        try:
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Question,
                "Changed in another program",
                "%s was changed in another program.\n\nSaved %s, %s (was %s). "
                "Update the copy kept in the project?" % (
                    o.name(), _when(o), _size(o.sig[1] if o.sig else 0),
                    _size(o.size)),
                SB_NONE, self.win)
            up = box.addButton("Update project copy",
                               QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(up)
            always = QtWidgets.QCheckBox("Always update from now on for this file")
            box.setCheckBox(always)
            box.exec()
            if box.clickedButton() is up:
                o.always = always.isChecked()
                self.update(o)
            else:
                o.declined = o.now
        finally:
            self._busy = False

    def update(self, o: OpenCopy) -> bool:
        """Replace the project's copy with the file's contents."""
        data = o.read()
        if data is None:
            return False
        d, win = o.doc, self.win
        try:
            d.project.replace_attachment_bytes(o.kind, o.aid, data)
        except KeyError:                        # the item is gone
            self.drop(o)
            return False
        o.sha = o.now = _sha(data)
        o.size, o.edited, o.declined = len(data), False, ""
        o.sig = o._stat()
        for t in d.tabs(win):
            win._workspace_changed(t)
        _gp.refresh_panel(win)
        _gp.autosave_soon(win, d)
        try:
            win.statusBar().showMessage(
                "Updated %s in the project from the external copy." % o.name(),
                6000)
        except Exception:
            pass
        return True

    def finish(self, o: OpenCopy, can_cancel: bool = True) -> bool:
        """Settle ``o`` before it goes: a last look, one question about an
        edit nobody answered, then erase it. False when cancelled."""
        self.look(o, force=True)
        if o.edited:
            if o.always:
                self.update(o)
            else:
                r = self._ask_final(o, can_cancel)
                if r == "cancel":
                    return False
                if r == "update":
                    self.update(o)
        self.drop(o)
        return True

    def _ask_final(self, o: OpenCopy, can_cancel: bool) -> str:
        self._busy = True
        try:
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Question,
                "Changed in another program",
                "%s was changed in another program and the project does not "
                "have the changes yet. Update the copy kept in the "
                "project?" % o.name(), SB_NONE, self.win)
            up = box.addButton("Update project copy",
                               QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            drop = box.addButton("Discard changes",
                                 QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton(
                "Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole) \
                if can_cancel else None
            box.setDefaultButton(up)
            box.exec()
            b = box.clickedButton()
            if b is up:
                return "update"
            if b is drop or (b is not cancel and not can_cancel):
                return "discard"
            return "cancel"
        finally:
            self._busy = False

    # -- erasing ------------------------------------------------------
    def drop(self, o: OpenCopy) -> None:
        """Forget ``o`` and erase its copy (retried while the other program
        still holds it)."""
        if o.proc is not None:
            o.proc.blockSignals(True)
        if not o.erase():
            return                               # kept in the list, retried
        if o in self.copies:
            self.copies.remove(o)
        _gp.refresh_panel(self.win)

    def settle(self, doc=None) -> bool:
        """Before a project (``doc``) or the app closes: every unanswered
        edit asked about once. False when the user cancelled."""
        for o in [c for c in self.copies if doc is None or c.doc is doc]:
            self.look(o, force=True)
            if o.edited and not self.settle_one(o):
                return False
        return True

    def settle_one(self, o: OpenCopy) -> bool:
        if o.always:
            self.update(o)
            return True
        r = self._ask_final(o, True)
        if r == "cancel":
            return False
        if r == "update":
            self.update(o)
        else:
            o.edited = False
            o.sha = o.now                        # discarded: not asked again
        return True

    def erase_doc(self, doc) -> None:
        for o in [c for c in self.copies if c.doc is doc]:
            self.drop(o)

    def erase_item(self, doc, kind, aid) -> None:
        o = self.find(doc, kind, aid)
        if o is not None:
            self.drop(o)

    def erase_all(self) -> None:
        for o in list(self.copies):
            self.drop(o)
        self.timer.stop()
        try:
            os.rmdir(root())                    # when nothing is left in it
        except OSError:
            pass


# ------------------------------------------------------------------ helpers
def _when(o: OpenCopy) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(o.sig[0] / 1e9))
    except (TypeError, OSError, ValueError):
        return "just now"


def _size(n: int) -> str:
    return "%d B" % n if n < 1024 else "%.0f KB" % (n / 1024.0) \
        if n < 1024 * 1024 else "%.1f MB" % (n / 1048576.0)


def launch_args(program: str, path: str) -> Tuple[str, List[str]]:
    """The command that opens ``path`` in ``program``. A macOS .app bundle
    goes through ``open -W -a`` (which waits until the program quits, so the
    copy can be erased then)."""
    if sys.platform == "darwin" and program.rstrip("/").endswith(".app"):
        return "open", ["-W", "-a", program, path]
    return program, [path]


def program_for(path_or_name: str) -> str:
    """The program remembered for this file type (or "")."""
    ext = os.path.splitext(path_or_name)[1].lower() or "."
    return str(QtCore.QSettings().value(_SETTINGS + ext, "") or "")


def remember_program(name: str, program: str) -> None:
    ext = os.path.splitext(name)[1].lower() or "."
    QtCore.QSettings().setValue(_SETTINGS + ext, program)


# ---------------------------------------------------------------- the hooks
def manager(win) -> ExternalCopies:
    m = win.__dict__.get("_ovt_external")
    if m is None:
        m = win.__dict__["_ovt_external"] = ExternalCopies(win)
    return m


def existing(win) -> Optional[ExternalCopies]:
    return win.__dict__.get("_ovt_external")


def install(win) -> None:
    """Window start: leftovers of earlier runs go."""
    sweep()
    manager(win)


def open_default(win, doc, kind, aid) -> bool:
    return manager(win).open_default(doc, kind, aid)


def open_with(win, doc, kind, aid, choose: bool = True) -> bool:
    """Open in a program: the remembered one for the file type, or the one
    the user picks (``choose``; the pick is remembered)."""
    a, _raw = _gp._att(doc, kind, aid)
    prog = "" if choose else program_for(a.filename)
    if not prog:
        start = os.path.dirname(program_for(a.filename)) or ""
        flt = "Programs (*.exe *.bat *.cmd);;All files (*)" \
            if sys.platform == "win32" else "All files (*)"
        prog, _f = QtWidgets.QFileDialog.getOpenFileName(
            win, "Open %s with" % a.filename, start, flt)
        if not prog:
            return False
        remember_program(a.filename, prog)
    return manager(win).open_with(doc, kind, aid, prog)


def reveal(win, doc, kind, aid) -> bool:
    return manager(win).reveal(doc, kind, aid)


def badge(win, doc, kind, aid) -> str:
    """The panel's note after an item that has an open copy."""
    m = existing(win)
    o = m.find(doc, kind, aid) if m is not None else None
    if o is None:
        return ""
    return "  (edited externally)" if o.edited else "  (open externally)"


def settle(win, doc=None) -> bool:
    """Ask about edits not yet taken into ``doc`` (or any project); False
    when cancelled."""
    m = existing(win)
    return True if m is None else m.settle(doc)


def erase_doc(win, doc) -> None:
    m = existing(win)
    if m is not None:
        m.erase_doc(doc)


def erase_item(win, doc, kind, aid) -> None:
    m = existing(win)
    if m is not None:
        m.erase_item(doc, kind, aid)


def erase_all(win) -> None:
    m = existing(win)
    if m is not None:
        m.erase_all()
