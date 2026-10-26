# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Save as... and Edit... of a data-logger preset of your own (see
:mod:`ovtsuite.datalog.userpresets`): the name and, editing, whether to take
the cells selected now. Laid out like Injector setup's preset dialog."""
from __future__ import annotations

from typing import Optional

from PySide6 import QtWidgets


class UserPresetDialog(QtWidgets.QDialog):
    def __init__(self, parent, title: str, ecu: str, selected: int, name: str = "",
                 preset=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        f = QtWidgets.QFormLayout(self)
        p = preset
        self.name = QtWidgets.QLineEdit(p.name if p is not None else name)
        self.name.selectAll()
        f.addRow("Name:", self.name)
        self.retake: Optional[QtWidgets.QCheckBox] = None
        cells = "%d cell%s" % (selected, "" if selected == 1 else "s")
        if p is None:
            f.addRow("", QtWidgets.QLabel(
                "Keeps the %s selected now, for %s." % (cells, ecu or "this controller")))
        else:
            self.retake = QtWidgets.QCheckBox(
                "Take the cells selected now (%s; it has %d)" % (cells, len(p.cells)))
            self.retake.setToolTip("Replace the preset's cells with the cells ticked in "
                                   "the logger now")
            self.retake.setEnabled(selected > 0)
            f.addRow("", self.retake)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        # an empty name is never accepted
        self.name.textChanged.connect(lambda t: self.ok.setEnabled(bool(t.strip())))
        self.ok.setEnabled(bool(self.name.text().strip()))
        self.resize(420, self.sizeHint().height())

    def name_text(self) -> str:
        return self.name.text().strip()

    def retake_cells(self) -> bool:
        return self.retake is not None and self.retake.isChecked()


__all__ = ["UserPresetDialog"]
