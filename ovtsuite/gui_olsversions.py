# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which binaries of a WinOLS project to import.

A WinOLS project (``.ols``) can embed several versions of its binary: the
original and what was made from it. :func:`choose` asks, only when there is
more than one, whether to import the original, every version (as versions of
one set) or a single one, and returns what ``import_file(...,
ols_versions=...)`` takes.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6 import QtCore, QtWidgets

#: returned by :func:`choose` when the user cancels the import
CANCEL = "cancel"


class OlsVersionsDialog(QtWidgets.QDialog):
    def __init__(self, parent, name: str, versions):
        super().__init__(parent)
        self.setWindowTitle("Import WinOLS project")
        self.resize(560, 420)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(
            "%s holds %d versions of its binary. Import:" % (name, len(versions))))
        self.original = QtWidgets.QRadioButton("The original only")
        self.all = QtWidgets.QRadioButton(
            "All %d versions (the original, then each version made from it)"
            % len(versions))
        self.some = QtWidgets.QRadioButton("The versions ticked below:")
        self.original.setChecked(True)
        for r in (self.original, self.all, self.some):
            lay.addWidget(r)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(["Version", "Changed", "Size"])
        Item = QtWidgets.QTreeWidgetItem
        Checked = QtCore.Qt.CheckState
        for i, v in enumerate(versions):
            vn, comment, size = v[0], v[1], v[2]
            changed = v[3] if len(v) > 3 else 0
            it = Item([vn, time.strftime("%Y-%m-%d %H:%M", time.localtime(changed))
                       if changed else "?", "%d bytes" % size])
            it.setData(0, QtCore.Qt.ItemDataRole.UserRole, i)
            it.setToolTip(0, comment.strip())
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Checked.Checked if i == 0 else Checked.Unchecked)
            self.tree.addTopLevelItem(it)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tree.setEnabled(False)
        self.some.toggled.connect(self.tree.setEnabled)
        lay.addWidget(self.tree, 1)
        self.bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

    def picked(self):
        out = []
        for k in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(k)
            if it.checkState(0) == QtCore.Qt.CheckState.Checked:
                out.append(it.data(0, QtCore.Qt.ItemDataRole.UserRole))
        return out

    def value(self):
        if self.all.isChecked():
            return "all"
        if self.some.isChecked():
            return self.picked() or None
        return None


def choose(parent, path: str, data: Optional[bytes] = None):
    """The ``ols_versions`` to import ``path`` with: None (the original),
    ``"all"`` or ``[index]``; :data:`CANCEL` when the user backs out. A file
    that is not a project with several versions is not asked about."""
    if not path.lower().endswith(".ols"):
        return None
    from .project import importers as _imp
    if data is None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
    versions = _imp.ols_versions(data)
    if len(versions) < 2:
        return None
    dlg = OlsVersionsDialog(parent, os.path.basename(path), versions)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return CANCEL
    return dlg.value()
