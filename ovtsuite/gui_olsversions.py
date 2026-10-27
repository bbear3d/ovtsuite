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
from typing import Optional

from PySide6 import QtWidgets

#: returned by :func:`choose` when the user cancels the import
CANCEL = "cancel"


class OlsVersionsDialog(QtWidgets.QDialog):
    def __init__(self, parent, name: str, versions):
        super().__init__(parent)
        self.setWindowTitle("Import WinOLS project")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(
            "%s holds %d versions of its binary. Import:" % (name, len(versions))))
        self.original = QtWidgets.QRadioButton("The original only")
        self.all = QtWidgets.QRadioButton(
            "All %d versions (the original, then each version made from it)"
            % len(versions))
        self.one = QtWidgets.QRadioButton("Only one version:")
        self.original.setChecked(True)
        self.combo = QtWidgets.QComboBox()
        for i, (vn, _c, size) in enumerate(versions):
            self.combo.addItem("%s (%d bytes)" % (vn, size), i)
        self.combo.setEnabled(False)
        self.one.toggled.connect(self.combo.setEnabled)
        lay.addWidget(self.original)
        lay.addWidget(self.all)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.one)
        row.addWidget(self.combo, 1)
        lay.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def value(self):
        if self.all.isChecked():
            return "all"
        if self.one.isChecked():
            return [self.combo.currentData()]
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
