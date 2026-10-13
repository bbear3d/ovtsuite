# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""File > File information (Ctrl+I): what the binary in a tab is, and -- for
a project tab -- where it lives in the project.

Shown on demand, and when a file is opened (an import, a project opened, a
read) unless "Show this when a file is opened" is unticked; that choice is
remembered (``ui/file_info_on_open``, on by default).
"""

from __future__ import annotations

import os
from typing import List, Tuple

from PySide6 import QtCore, QtWidgets

from . import branding

SETTING = "ui/file_info_on_open"


def show_on_open() -> bool:
    s = QtCore.QSettings(*branding.QSETTINGS)
    v = s.value(SETTING, True)
    return v not in (False, "false", "0", 0)


def set_show_on_open(on: bool) -> None:
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.setValue(SETTING, bool(on))
    s.sync()


#: rows of fileinfo.rows_for_session that describe a project tab's working
#: copy rather than the binary
_WORKING_FILE_ROWS = ("File", "Container", "Definition")


def _size(n: int) -> str:
    return "%d KB" % (n // 1024) if n >= 1024 else "%d bytes" % n


def _when(ts: str) -> str:
    return (ts or "").replace("T", " ")[:16]


def project_rows(ws, session=None) -> List[Tuple[str, str]]:
    """Rows for a project tab: the project file, set, branch, the version
    the tab started from, unsaved state, originals, definition, last
    flashed. Empty for a tab outside a project or for its comparison."""
    ref = getattr(ws, "project_ref", None)
    if ref is None or (session is not None and session is not ws.primary):
        return []
    from . import gui_project as _gp
    doc, proj, s = ref.doc, ref.doc.project, ref.set
    rows: List[Tuple[str, str]] = [("Project", doc.title)]
    rows.append(("Project file", doc.path or "not saved yet"))
    info = getattr(proj, "info", {}) or {}
    car = " ".join(info.get(k, "") for k in ("make", "model", "chassis", "year")
                   if info.get(k))
    if car:
        rows.append(("Vehicle", car))
    rows.append(("Set", "%s (%s)" % (s.name, s.platform)))
    b = s.branch(ref.bid)
    rows.append(("Branch", b.name if b is not None else "?"))
    head = s.checkpoint(b.head) if b is not None else None
    if head is not None:
        n = len(list(s.lineage(head.id)))
        rows.append(("Saved version", "%s -- %s (%d version%s on this branch)"
                     % (head.label or head.kind, _when(head.ts), n,
                        "" if n == 1 else "s")))
    rows.append(("Unsaved changes", "yes" if ws.is_dirty() else "no"))
    roots = [c for c in s.roots()]
    if roots:
        r = roots[0]
        rows.append(("Original", "%s -- %s" % (r.label or r.kind, _when(r.ts))))
    for o in s.originals:
        rows.append(("Original file", "%s (%s, kept byte for byte)"
                     % (o.filename, o.format)))
    main = _gp._sb._main_member(proj, s.id)
    if main is not None and main.definition:
        use = main.definition.get("use") == "instead"
        rows.append(("Definition file", "%s (%s)%s" % (
            os.path.basename(main.definition.get("path", "")),
            str(main.definition.get("type", "")).upper(),
            " -- used instead of the module's" if use else "")))
    sizes = dict(head.sizes) if head is not None else {}
    members = ", ".join("%s %s" % (m.id, _size(sizes[m.id]))
                        for m in s.members if sizes.get(m.id))
    if members:
        rows.append(("Parts", members))
    fl = _gp.flashed_text(proj, s.id)
    if fl:
        rows.append(("Flashed", fl[len("Last flashed "):]
                     if fl.startswith("Last flashed ") else fl))
    logs = [lg for lg in proj.logs if lg.set in (None, s.id)]
    if logs or proj.patches or proj.attachments:
        rows.append(("Kept in the project", "%d log%s, %d patch%s, %d attachment%s"
                     % (len(logs), "" if len(logs) == 1 else "s",
                        len(proj.patches), "" if len(proj.patches) == 1 else "es",
                        len(proj.attachments),
                        "" if len(proj.attachments) == 1 else "s")))
    return rows


class FileInfoDialog(QtWidgets.QDialog):
    """Two sections -- the project (when there is one) and the binary -- and
    the "show when a file is opened" choice."""

    def __init__(self, parent, title, project, binary):
        super().__init__(parent)
        self.setWindowTitle(title)
        try:
            from .gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        v = QtWidgets.QVBoxLayout(self)
        rows = []
        if project:
            rows.append(("__head__", "Project"))
            rows += project
            rows.append(("__head__", "Binary"))
        rows += binary
        self.table = tbl = QtWidgets.QTableWidget(len(rows), 2)
        tbl.setHorizontalHeaderLabels(["Item", "Value"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for r, (k, val) in enumerate(rows):
            if k == "__head__":
                it = QtWidgets.QTableWidgetItem(val)
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                tbl.setItem(r, 0, it)
                tbl.setSpan(r, 0, 1, 2)
                continue
            tbl.setItem(r, 0, QtWidgets.QTableWidgetItem(k))
            it = QtWidgets.QTableWidgetItem(str(val))
            it.setToolTip(str(val))
            tbl.setItem(r, 1, it)
        tbl.resizeRowsToContents()
        v.addWidget(tbl)
        row = QtWidgets.QHBoxLayout()
        self.on_open = QtWidgets.QCheckBox("Show this when a file is opened")
        self.on_open.setToolTip("Untick to stop this window appearing after "
                                "each import or project opened; File > File "
                                "information (Ctrl+I) still shows it")
        self.on_open.setChecked(show_on_open())
        self.on_open.toggled.connect(set_show_on_open)
        row.addWidget(self.on_open)
        row.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        row.addWidget(bb)
        v.addLayout(row)
        self.resize(680, min(680, 90 + 22 * (len(rows) + 1)))


def show(ws, session=None, loaded=False):
    """The dialog for ``session`` (the tab's primary by default, or its
    comparison). ``loaded`` = shown because a file was just opened: skipped
    when the user turned that off, or while tabs are being reopened."""
    sess = session if session is not None else ws.primary
    if sess is None:
        return None
    if loaded and (getattr(ws, "_quiet", False) or not show_on_open()):
        return None
    from .fileinfo import rows_for_session
    eeprom = getattr(ws, "_eeprom", None) if sess is ws.primary else None
    try:
        binary = rows_for_session(sess, eeprom)
    except Exception as ex:
        binary = [("Info", "unavailable: %s" % ex)]
    try:
        project = project_rows(ws, sess)
    except Exception as ex:
        project = [("Project", "unavailable: %s" % ex)]
    if project:
        # the tab edits a working copy of its branch: its file name, its
        # container and that container's definition cache are plumbing
        binary = [(k, v) for k, v in binary if k not in _WORKING_FILE_ROWS]
    which = "Comparison" if sess is getattr(ws, "comparison", None) else "File"
    lead = "%s loaded" % which if loaded else "%s information" % which
    name = ws.display_name() if sess is ws.primary and project else sess.binfile.name
    # one at a time, owned by the main window (not the tab: a tab page is
    # hidden whenever another tab is in front)
    owner = getattr(ws, "win", None) or ws
    old = owner.__dict__.get("_file_info_dlg")
    if old is not None:
        try:
            old.close()
        except RuntimeError:
            pass
    dlg = FileInfoDialog(owner, "%s - %s" % (lead, name), project, binary)
    dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
    owner.__dict__["_file_info_dlg"] = dlg
    dlg.destroyed.connect(lambda *_a, o=owner: o.__dict__.pop("_file_info_dlg", None))
    dlg.show()                  # modeless: never blocks the window or the caller
    dlg.raise_()
    return dlg


__all__ = ["show", "show_on_open", "set_show_on_open", "project_rows",
           "FileInfoDialog", "SETTING"]
