# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The **Fitted hardware** part of File > Project information...: the
records of :mod:`ovtsuite.project.hardware` (pump, nozzles, injectors fitted
to the car), to read, add by hand, correct and remove.

The box edits a copy; :meth:`HardwareBox.write` puts it into the project when
the information window's OK is pressed.

Also what the injector setup windows (VE pump and PD) share:
:func:`pick_project_logs` (the Add menu of their From logs lists, the
project's logs marked before / after the recorded change) and
:func:`ask_fitted` (Record as fitted...).
"""

from __future__ import annotations

import copy
import html
import os
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .project import hardware as H
from .project.model import now


def _ck_text(c) -> str:
    return "'%s' (%s)" % (c.label or c.kind, (c.ts or "").replace("T", " ")[:16])


class RecordDialog(QtWidgets.QDialog):
    """One record: what (words; fixed for a record a tool made, since its
    parts say exactly what), when it was fitted, the version calibrated for
    it, a note."""

    def __init__(self, project, sids: List[str], rec: Optional[dict] = None,
                 sid: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fitted hardware")
        self.setMinimumWidth(480)
        self.project = project
        self.rec = dict(rec or {})
        made_by_tool = bool(self.rec.get("parts"))
        f = QtWidgets.QFormLayout(self)
        self.set_box = QtWidgets.QComboBox()
        for s_id in sids:
            self.set_box.addItem(project.set(s_id).name, s_id)
        if sid:
            self.set_box.setCurrentIndex(max(0, self.set_box.findData(sid)))
        self.set_box.setEnabled(rec is None and len(sids) > 1)
        self.set_box.currentIndexChanged.connect(self._fill_versions)
        if len(sids) > 1:
            f.addRow("Control unit:", self.set_box)
        self.kind = QtWidgets.QComboBox()
        for k, label in H.KIND_LABELS.items():
            self.kind.addItem(label, k)
        k = self.rec.get("kind") or H.VE_PUMP
        if self.kind.findData(k) < 0:
            self.kind.addItem(k, k)
        self.kind.setCurrentIndex(self.kind.findData(k))
        self.kind.setEnabled(not made_by_tool)
        f.addRow("Part:", self.kind)
        self.summary = QtWidgets.QLineEdit(self.rec.get("summary", ""))
        self.summary.setPlaceholderText("e.g. 11 mm pump head, DSLA150P764 nozzles")
        self.summary.setReadOnly(made_by_tool)
        if made_by_tool:
            self.summary.setToolTip("Written by Tools > Injector setup, which knows exactly "
                                    "what it set the calibration up for")
        f.addRow("What:", self.summary)
        self.when = QtWidgets.QDateEdit()
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd")
        d = QtCore.QDate.fromString((self.rec.get("since") or now())[:10], "yyyy-MM-dd")
        self.when.setDate(d if d.isValid() else QtCore.QDate.currentDate())
        f.addRow("Fitted on:", self.when)
        self.version = QtWidgets.QComboBox()
        f.addRow("Calibration:", self.version)
        self.note = QtWidgets.QLineEdit(self.rec.get("note", ""))
        f.addRow("Note:", self.note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._fill_versions()

    @property
    def sid(self) -> str:
        return self.set_box.currentData()

    def _fill_versions(self):
        self.version.clear()
        self.version.addItem("Not recorded (no version is set up for it yet)", (None, False))
        s = self.project.set(self.sid)
        b = s.active
        seen, cps = set(), []
        for br in [b] + [x for x in s.branches if x is not b]:
            if br is None:
                continue
            for c in s.lineage(br.head):
                if c.id not in seen:
                    seen.add(c.id)
                    cps.append(c)
        cur = (self.rec.get("checkpoint"), bool(self.rec.get("pending")))
        c = s.checkpoint(cur[0]) if cur[0] else None
        if c is not None and c.id not in seen:      # off every branch now: keep it
            cps.append(c)
        cps.sort(key=lambda c: c.ts or "", reverse=True)
        for c in cps:
            if cur == (c.id, True):
                self.version.addItem("Set up after version %s" % _ck_text(c), (c.id, True))
            self.version.addItem("Calibrated in version %s" % _ck_text(c), (c.id, False))
        i = next((i for i in range(self.version.count())
                  if tuple(self.version.itemData(i)) == cur), 0)
        self.version.setCurrentIndex(i)

    def _ok(self):
        if not self.summary.text().strip():
            QtWidgets.QMessageBox.information(self, "Fitted hardware",
                                              "Say what is fitted.")
            return
        cid, pending = self.version.currentData()
        day = self.when.date().toString("yyyy-MM-dd")
        since = self.rec.get("since") or ""
        if since[:10] != day:
            since = day
        self.rec.update(kind=self.kind.currentData(), summary=self.summary.text().strip(),
                        since=since, checkpoint=cid, pending=bool(pending),
                        note=self.note.text().strip())
        self.rec.setdefault("id", H.new_id())
        self.rec.setdefault("parts", {})
        self.rec.setdefault("source", H.FROM_USER)
        self.rec.setdefault("recorded", now())
        self.rec.setdefault("branch", None)
        self.accept()


class HardwareBox(QtWidgets.QGroupBox):
    """The records of every set of ``project``, newest first."""

    def __init__(self, project, parent=None):
        super().__init__("Fitted hardware", parent)
        self.project = project
        self.sids = [s.id for s in project.sets]
        self.work: Dict[str, list] = {
            sid: copy.deepcopy(list(project.set(sid).extra.get(H.KEY) or []))
            for sid in self.sids}
        self._orig = copy.deepcopy(self.work)
        v = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QTreeWidget()
        many = len(self.sids) > 1
        self.list.setHeaderLabels((["Control unit"] if many else []) +
                                  ["Part", "What", "Fitted", "Calibration"])
        self.list.setRootIsDecorated(False)
        self.list.setMinimumHeight(90)
        self.list.itemDoubleClicked.connect(lambda *_a: self._edit())
        v.addWidget(self.list)
        hint = QtWidgets.QLabel(
            "What is fitted to the car, from when. Tools > Injector setup records the "
            "build it sets the calibration up for; the dyno chart and the log tools "
            "use it to tell versions and logs for different hardware apart.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Add...", self._add), ("Edit...", self._edit),
                         ("Remove", self._remove)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._fill()

    def _records(self):
        out = []
        for sid in self.sids:
            for d in self.work[sid]:
                try:
                    out.append((sid, H.Fitted.from_dict(d)))
                except (ValueError, TypeError):
                    continue
        out.sort(key=lambda x: (x[1].since or "", x[1].recorded or ""), reverse=True)
        return out

    def _fill(self):
        self.list.clear()
        many = len(self.sids) > 1
        for sid, r in self._records():
            s = self.project.set(sid)
            cal = ""
            c = s.checkpoint(r.checkpoint) if r.checkpoint else None
            if c is not None:
                cal = ("after %s" if r.pending else "in %s") % _ck_text(c)
            cols = ([s.name] if many else []) + [
                r.kind_label, r.summary, (r.since or "").replace("T", " ")[:10], cal]
            it = QtWidgets.QTreeWidgetItem(cols)
            it.setData(0, QtCore.Qt.UserRole, (sid, r.id))
            if r.note:
                it.setToolTip(len(cols) - 3, r.note)
            self.list.addTopLevelItem(it)
        for i in range(self.list.columnCount()):
            self.list.resizeColumnToContents(i)

    def _selected(self):
        it = self.list.currentItem()
        return it.data(0, QtCore.Qt.UserRole) if it is not None else None

    def _add(self):
        dlg = RecordDialog(self.project, self.sids, None,
                           self.sids[0] if self.sids else "", self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.work[dlg.sid].append(dlg.rec)
            self._fill()

    def _edit(self):
        sel = self._selected()
        if sel is None:
            return
        sid, rid = sel
        lst = self.work[sid]
        i = next(i for i, d in enumerate(lst) if isinstance(d, dict) and d.get("id") == rid)
        dlg = RecordDialog(self.project, self.sids, lst[i], sid, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            lst[i] = dlg.rec
            self._fill()

    def _remove(self):
        sel = self._selected()
        if sel is None:
            return
        sid, rid = sel
        self.work[sid] = [d for d in self.work[sid]
                          if not (isinstance(d, dict) and d.get("id") == rid)]
        self._fill()

    def write(self) -> bool:
        """Put the edited records into the project; True when anything changed."""
        if self.work == self._orig:
            return False
        for sid in self.sids:
            self.project.set(sid).extra[H.KEY] = self.work[sid]
        self.project._touch()
        return True


# ------------------------------------------------------- the setup windows
def project_log_path(doc, a) -> str:
    """A kept log as a file the log readers can open (in the project's
    working folder; written once)."""
    path = os.path.join(doc.tmpdir, "logs", a.id, a.filename)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(doc.project.log_bytes(a.id))
    return path


def _exec_menu(menu):
    return menu.exec(QtGui.QCursor.pos())


def pick_project_logs(parent, ref, kind: str, before: bool = False):
    """The Add... menu of a From logs list in a project tab: "every log
    added since (``before``: before) the recorded change" first, then each
    kept log -- this set's first, newest first -- marked with the hardware the
    car had when it was added, then "From a file...".

    Returns a list of paths, ``"file"`` for From a file..., or None
    (dismissed). With no project or no kept logs: ``"file"``."""
    try:
        doc = ref.doc
        logs = list(doc.project.logs) if ref is not None else []
    except Exception:                                   # noqa: BLE001
        return "file"
    if not logs:
        return "file"
    rec, after, older = None, [], []
    try:
        rec, after, older = H.split_logs(doc.project, ref.sid, kind)
    except Exception:                                   # noqa: BLE001
        pass
    menu = QtWidgets.QMenu(parent)
    group = older if before else after
    group_act = None
    if rec is not None and group:
        group_act = menu.addAction(
            ("Every log added before the change on %s (%d)" if before else
             "Every log added since the change on %s (%d)")
            % (rec.since.replace("T", " ")[:10], len(group)))
        group_act.setToolTip(rec.summary)
        menu.addSeparator()
    # newest first, then (a stable sort) this set's ahead of the others'
    order = [a for _i, a in sorted(enumerate(logs), key=lambda p: (p[1].added or "", p[0]),
                                   reverse=True)]
    order.sort(key=lambda a: a.set not in (None, ref.sid))
    for a in order:
        tag = ""
        if rec is not None and a.added:
            had = H.at(doc.project, ref.sid, a.added, kind)
            tag = ("   - with %s" % had[0].summary) if had else "   - before the change"
        act = menu.addAction("%s   (%s)%s" % (a.filename,
                                              (a.added or "").replace("T", " ")[:16], tag))
        act.setData(a.id)
    menu.addSeparator()
    other = menu.addAction("From a file...")
    got = _exec_menu(menu)
    if got is None:
        return None
    if got is other:
        return "file"
    picks = group if got is group_act else [next(x for x in logs if x.id == got.data())]
    return [project_log_path(doc, a) for a in picks]


def ask_fitted(parent, title: str, summary: str, calibrated: Optional[bool]):
    """Record as fitted...: confirm ``summary`` and the day it was fitted.
    ``calibrated``: this file is (True) / is not (False) / may be (None) set
    up for it already. The fitted date as ISO text, or None (cancelled)."""
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(title)
    f = QtWidgets.QFormLayout(dlg)
    f.addRow(QtWidgets.QLabel("Record in the project that the car has:"))
    f.addRow("", QtWidgets.QLabel("<b>%s</b>" % html.escape(summary)))
    when = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
    when.setCalendarPopup(True)
    when.setDisplayFormat("yyyy-MM-dd")
    f.addRow("Fitted on:", when)
    if calibrated:
        text = "This file is set up for it, so this version is recorded as calibrated for it."
    elif calibrated is None:
        text = ("This file may already be set up for it; it is recorded without a "
                "calibration. If it is not set up yet, Apply does that, and the record "
                "then points at that version.")
    else:
        text = ("This file is not set up for it yet: Apply does that, and the record then "
                "points at that version.")
    note = QtWidgets.QLabel(text)
    note.setObjectName("calibrated_note")
    note.setWordWrap(True)
    f.addRow("", note)
    bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                    | QtWidgets.QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    f.addRow(bb)
    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None
    day = when.date().toString("yyyy-MM-dd")
    return now() if day == QtCore.QDate.currentDate().toString("yyyy-MM-dd") else day


__all__ = ["HardwareBox", "RecordDialog", "pick_project_logs", "project_log_path",
           "ask_fitted"]
