# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An open project's ``.ovt`` changed on disk (another program -- a script,
an AI assistant, a second tool -- wrote it): notice, and reload or merge
instead of overwriting it (:mod:`ovtsuite.project.disksync`).

* Every save of an open project goes through :func:`write`: when the file is
  no longer what the project was loaded from or last saved to, the user
  picks **Reload & merge** (the other program's changes come in, ours stay,
  then the save goes ahead), **Overwrite** (theirs are lost), **Save mine as
  a copy** or **Cancel**. Nothing is written without that answer -- not on
  exit either (the background definition write just skips).
* :func:`install` watches the open projects' files while the app runs: one
  with nothing unsaved is reloaded quietly when it changes; one with unsaved
  work asks whether to merge now (or at the next save).
* Merging keeps unsaved tab edits: a tab whose branch the other program
  moved gets its edits re-applied on top of the new head, or -- where they
  overlap -- keeps them on a new "<branch> (mine)" branch.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6 import QtCore, QtWidgets

from . import gui_project as _gp
from .project import Project
from .project import archive as _ar
from .project import delta as _d
from .project import disksync as _ds
from .project import sessionbridge as _sb
from .project.project import HistoryError

#: how often the open projects' files are looked at (size and time only)
POLL_MS = 2000

OVERWRITE, MERGE, COPY, CANCEL = "overwrite", "merge", "copy", "cancel"


def _busy(win) -> bool:
    return bool(win.__dict__.get("_disksync_busy"))


class _Busy:
    """No watcher prompt while a save or a merge is asking something."""

    def __init__(self, win):
        self.win = win

    def __enter__(self):
        self.win.__dict__["_disksync_busy"] = \
            self.win.__dict__.get("_disksync_busy", 0) + 1

    def __exit__(self, *exc):
        self.win.__dict__["_disksync_busy"] -= 1


# ------------------------------------------------------------- saving
def write(win, doc, path: Optional[str] = None, force: bool = False) -> bool:
    """``doc.project.save`` to ``path`` (default ``doc.path``) -- asking
    first when the file changed on disk (``force``: the user already said
    Overwrite). True once written. The writing runs on a worker thread
    while the window waits (:mod:`ovtsuite.gui_saving`)."""
    from . import gui_saving as _sv
    with _Busy(win):
        while True:
            target = path or doc.path
            try:
                job = doc.project.prepare_save(target, force=force)
                _sv.run(win, job.run)
                job.finish()
                return True
            except _ds.DiskChanged as ex:
                what = resolve(win, doc, ex.deleted)
                if what == CANCEL:
                    return False
                force = what == OVERWRITE


def ensure_current(win, doc) -> Optional[bool]:
    """Before a save commits anything: when the file changed on disk, ask
    (as :func:`write` would) so a Cancel leaves everything as it was. None =
    cancel the save; else whether to write with ``force`` (Overwrite)."""
    if not doc.path or doc.project._disk is None:
        return False
    with _Busy(win):
        why = doc.project.changed_on_disk()
        if why is None:
            return False
        what = resolve(win, doc, why == "deleted")
        return None if what == CANCEL else what == OVERWRITE


def resolve(win, doc, deleted: bool) -> str:
    """Ask what to do about the changed (or gone) file. MERGE and COPY are
    done here; the caller then saves (OVERWRITE: with ``force``). Returns
    OVERWRITE, MERGE, COPY or CANCEL."""
    while True:
        what = DiskChangedDialog.ask(win, doc, deleted)
        if what == MERGE:
            if merge_from_disk(win, doc):
                return MERGE
            continue                        # could not merge: ask again
        if what == COPY:
            if save_copy_path(win, doc):
                return COPY
            continue
        return what


def save_copy_path(win, doc) -> bool:
    """Save mine as a copy: the project now lives at a new path (the next
    save writes there; the changed file is left alone)."""
    path = _gp._ask_path(win, doc.title + " (mine)", None)
    if not path:
        return False
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(doc.path or ""):
        QtWidgets.QMessageBox.information(
            win, "Save a copy", "Pick another name: that is the file the other "
            "program changed. Use Overwrite to replace it.")
        return False
    from .gui_files import lock_project, unlock_project
    if not lock_project(win, path):
        return False
    unlock_project(win, doc.path)
    doc.path = os.path.abspath(path)
    doc.project.path = doc.path
    return True


class DiskChangedDialog(QtWidgets.QMessageBox):
    """The project's file changed (or went) since it was opened or saved."""

    @classmethod
    def build(cls, win, doc, deleted: bool):
        """``(box, {button: answer})``, not shown yet."""
        box = cls(win)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Project changed on disk")
        name = os.path.basename(doc.path or doc.title)
        if deleted:
            box.setText("%s was deleted or moved by another program since you "
                        "opened or last saved it." % name)
            box.setInformativeText("Save it again where it was, or somewhere "
                                   "else?")
            again = box.addButton("Save it again",
                                  QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            copy = box.addButton("Save as...",
                                 QtWidgets.QMessageBox.ButtonRole.ActionRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(again)
            return box, {again: OVERWRITE, copy: COPY}
        box.setText("%s was changed by another program since you opened or "
                    "last saved it." % name)
        box.setInformativeText(
            "Reload & merge brings its changes in and keeps yours, then "
            "saves. Overwrite replaces the file with your version: the other "
            "program's changes are lost.")
        merge = box.addButton("Reload && merge",
                              QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        over = box.addButton("Overwrite",
                             QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        copy = box.addButton("Save mine as a copy...",
                             QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(merge)
        return box, {merge: MERGE, over: OVERWRITE, copy: COPY}

    @classmethod
    def ask(cls, win, doc, deleted: bool) -> str:
        box, answers = cls.build(win, doc, deleted)
        box.exec()
        return answers.get(box.clickedButton(), CANCEL)

# ------------------------------------------------------------- merging
def _load_disk(win, doc, quiet=False) -> Optional[Project]:
    try:
        return Project.load(doc.path)
    except _ar.UnsupportedFeature as ex:
        why = "%s. It can't be merged; Overwrite or Save mine as a copy." % ex
    except Exception as ex:
        if quiet:
            return None                     # maybe still being written
        why = ("it could not be read (%s) -- maybe the other program is still "
               "writing it. Try again in a moment." % ex)
    if not quiet:
        QtWidgets.QMessageBox.warning(win, "Reload project", "%s: %s"
                                      % (os.path.basename(doc.path), why))
    return None


def merge_from_disk(win, doc, quiet: bool = False) -> bool:
    """Bring the file's changes into the open project, keeping ours (see
    the module doc). ``quiet``: nothing to ask (the watcher reloading a
    project with nothing unsaved). True when done."""
    with _Busy(win):
        theirs = _load_disk(win, doc, quiet)
        if theirs is None:
            return False
        mine = doc.project
        if theirs.uid != mine.uid:
            if not quiet:
                QtWidgets.QMessageBox.warning(
                    win, "Reload project",
                    "%s is now a different project (another program replaced "
                    "it), so it can't be merged with this one. Overwrite it, "
                    "or save yours as a copy." % os.path.basename(doc.path))
            return False
        tabs = doc.tabs(win)
        dirty = [t for t in tabs if t.is_dirty()]
        res = _ds.merge(mine, theirs, default="mine",
                        keep_sets={t.project_ref.sid for t in dirty},
                        keep_branches={(t.project_ref.sid, t.project_ref.bid)
                                       for t in dirty})
        if res.conflicts:
            if quiet:
                return False
            dlg = MergeConflictDialog(res.conflicts, doc.title, win)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return False
            res = _ds.merge(mine, theirs, dlg.choices(), default="mine",
                            keep_sets={t.project_ref.sid for t in dirty},
                            keep_branches={(t.project_ref.sid, t.project_ref.bid)
                                           for t in dirty})
        notes = list(res.notes)
        notes += _install(win, doc, res, mine, tabs)
        if notes and not quiet:
            QtWidgets.QMessageBox.information(
                win, "Merged", "%s: the other program's changes are in.\n\n%s"
                % (doc.title, "\n\n".join(notes)))
        win.statusBar().showMessage(
            "%s was changed by another program: %s." % (
                doc.title, "reloaded" if not res.differs else
                "its changes are merged with yours (unsaved)"), 10000)
        return True


def _install(win, doc, res, mine, tabs) -> list:
    """Make the merged project the open one and bring every tab in line.
    Returns notes for the user."""
    notes = []
    heads: Dict[object, Optional[str]] = {}
    for t in tabs:
        try:
            heads[t] = mine.set(t.project_ref.sid).branch(t.project_ref.bid).head
        except (KeyError, AttributeError):
            heads[t] = None
    new = res.project
    doc.project = new
    doc.saver.reset(new)
    gone = []
    for t in tabs:
        ref = t.project_ref
        sid, bid = ref.sid, ref.bid
        moved = res.moved.get((sid, bid))
        if moved:
            ref.bid = moved                 # our head lives there now
            _gp._resync_log(t)
            continue
        try:
            br = new.set(sid).branch(bid)
        except KeyError:
            br = None
        if br is None:
            gone.append(t)
            continue
        if br.head == heads[t]:
            _gp._resync_log(t)
            continue
        if not t.is_dirty():
            _gp.reload_tab(t)
            continue
        notes.append(_rebase(win, doc, t, mine, heads[t], br))
    for t in gone:
        win._remove_workspace(t)
    if gone:
        notes.append("Closed %d tab%s showing a branch the other program "
                     "deleted." % (len(gone), "" if len(gone) == 1 else "s"))
    for t in doc.tabs(win):
        _gp._name(t)
        win._workspace_changed(t)
    _gp.remember(win, doc)
    _gp.refresh_panel(win)
    _gp.autosave(win, doc)
    return [n for n in notes if n]


def _rebase(win, doc, t, mine, old_head, br) -> str:
    """A tab with unsaved edits whose branch the other program moved: its
    edits on top of the new head, or (they overlap) on a new branch."""
    ref = t.project_ref
    new = doc.project
    s = new.set(ref.sid)
    try:
        edited = _sb.working_images(mine, ref.sid, t.primary)
        recs = _d.diff(mine.state(ref.sid, old_head), edited)
        moved = _d.apply_forward(new.state(ref.sid, br.head), recs)
    except (_d.DeltaError, HistoryError, KeyError):
        moved = None
    if moved is not None and _gp.reload_tab(t) and t.primary is not None:
        _sb.apply_working(new, ref.sid, t.primary, moved)
        t.model.refresh()
        return ("%s ▸ %s: your unsaved edits are now on top of the other "
                "program's latest version (undo starts again from here)."
                % (s.name, br.name))
    try:
        b = new.fork(ref.sid, br.name + _ds.MINE, at=old_head)
    except (KeyError, HistoryError):
        return ("%s ▸ %s: the other program changed this branch; your unsaved "
                "edits stay in the tab." % (s.name, br.name))
    ref.bid = b.id
    _gp._resync_log(t)
    return ("%s ▸ %s: the other program changed the same bytes you are "
            "editing, so your unsaved edits are now on the new branch \"%s\" "
            "(the tab shows it)." % (s.name, br.name, b.name))


class MergeConflictDialog(QtWidgets.QDialog):
    """Both sides changed the same things: pick whose to keep, one by one."""

    def __init__(self, conflicts, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge changes")
        self.conflicts = conflicts
        v = QtWidgets.QVBoxLayout(self)
        lead = QtWidgets.QLabel(
            "%s was changed both here and by another program. Pick which "
            "version to keep of each:" % title)
        lead.setWordWrap(True)
        v.addWidget(lead)
        self.table = QtWidgets.QTableWidget(len(conflicts), 4)
        self.table.setHorizontalHeaderLabels(["What", "Yours", "Other program's",
                                              "Keep"])
        self.table.verticalHeader().hide()
        self.combos = []
        for row, c in enumerate(conflicts):
            for col, text in enumerate((c.label, c.mine, c.theirs)):
                it = QtWidgets.QTableWidgetItem(text)
                it.setToolTip(text)
                it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, it)
            box = QtWidgets.QComboBox()
            box.addItems(["Yours", "Other program's"])
            self.table.setCellWidget(row, 3, box)
            self.combos.append(box)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            hh.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.
                                    ResizeToContents)
        v.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        for text, i in (("All yours", 0), ("All the other program's", 1)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _c=False, i=i: [x.setCurrentIndex(i)
                                                     for x in self.combos])
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Merge", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(760, 220 + 28 * min(len(conflicts), 12))

    def choices(self) -> dict:
        return {c.key: ("mine" if box.currentIndex() == 0 else "theirs")
                for c, box in zip(self.conflicts, self.combos)}


# ------------------------------------------------------------- watching
def poll(win) -> None:
    """Look at every open project's file (size and time; the content only
    when those changed and have settled for one poll)."""
    from . import options as _opt
    if not _opt.get("projects/watch_disk"):
        return                              # saving still checks
    if _busy(win) or QtWidgets.QApplication.activeModalWidget() is not None:
        return
    for doc in list(_gp.docs(win)):
        st = doc.project._disk
        if not doc.path or st is None or not st.is_for(doc.path):
            continue
        try:
            stat = os.stat(doc.path)
            stamp = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp is not None and stamp == (st.size, st.mtime_ns):
            doc.__dict__.pop("_disk_seen", None)
            continue
        if doc.__dict__.get("_disk_told") == stamp:
            continue                        # asked already; the save asks again
        if doc.__dict__.get("_disk_seen") != stamp:
            doc.__dict__["_disk_seen"] = stamp      # let a write finish first
            continue
        why = doc.project.changed_on_disk()
        if why is None:
            continue                        # touched only
        _changed(win, doc, why, stamp)


def _changed(win, doc, why, stamp) -> None:
    name = os.path.basename(doc.path)
    if why == "deleted":
        doc.__dict__["_disk_told"] = stamp
        win.statusBar().showMessage(
            "%s was deleted or moved by another program; saving will ask "
            "where to put it." % name, 15000)
        return
    from . import options as _opt
    if not doc.is_dirty(win) and _opt.get("projects/auto_reload"):
        if merge_from_disk(win, doc, quiet=True):
            return
        doc.__dict__["_disk_told"] = stamp
        return
    with _Busy(win):
        SB = QtWidgets.QMessageBox.StandardButton
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Question, "Project changed on disk",
            "%s was changed by another program.%s\n\nMerge the other program's "
            "changes in now? (If not, saving will ask.)" % (
                name, " You have unsaved changes in it." if doc.is_dirty(win)
                else ""), SB.NoButton, win)
        merge = box.addButton("Reload && merge",
                              QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(merge)
        box.exec()
        if box.clickedButton() is not merge or not merge_from_disk(win, doc):
            doc.__dict__["_disk_told"] = stamp


def install(win):
    """Watch the open projects' files while the window lives."""
    t = QtCore.QTimer(win)
    from . import options as _opt
    t.setInterval(_opt.get("projects/watch_s") * 1000)      # POLL_MS
    t.timeout.connect(lambda: poll(win))
    t.start()
    app = QtWidgets.QApplication.instance()
    if app is not None:
        def active(state, win=win):
            if state == QtCore.Qt.ApplicationState.ApplicationActive:
                QtCore.QTimer.singleShot(0, lambda: poll(win))
        app.applicationStateChanged.connect(active)
        win.destroyed.connect(lambda *_a: _disconnect(app, active))
    win.__dict__["_ovt_disksync_timer"] = t
    return t


def _disconnect(app, slot):
    try:
        app.applicationStateChanged.disconnect(slot)
    except (RuntimeError, TypeError):
        pass
