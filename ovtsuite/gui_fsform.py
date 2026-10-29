# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The form a Feature switches hook can ask the window for (``FS_FORM``).

A table hook whose setting is several numbers the user types -- the gear
ratios of a gearbox -- describes them as a spec and the window shows them
in a box under **Set to**; the values reach the hook's ``fs_preview`` /
``fs_describe`` / ``fs_apply`` as keyword arguments, like ``FS_OPTION``'s.

    FS_FORM = {
        "title": "Gear ratios",                 # the box's title
        "states": ("set",),                     # the settings it applies to
        "fields": [
            {"key": "g1", "label": "1st gear", "kind": "number",
             "min": 0.0, "max": 20.0, "decimals": 3, "step": 0.01,
             "default": 0.0, "special": "leave as is", "suffix": "", "tip": ""},
            {"key": "vn", "label": "Also set the V/N bands", "kind": "check",
             "default": True, "tip": ""},
        ],
    }

A number at its minimum shows ``special`` instead of a value (the hook reads
the minimum as "not given"). ``fs_form_defaults(session, group)`` (optional)
returns ``{key: value}`` to start the form from what the file holds. Two
number fields share a row; a tick box takes its own.
"""
from __future__ import annotations

from typing import Dict

from PySide6 import QtCore, QtWidgets


class FormBox(QtWidgets.QGroupBox):
    """The box. ``changed`` fires when the user edits a field."""

    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QtWidgets.QGridLayout(self)
        self._widgets: Dict[str, QtWidgets.QWidget] = {}
        self._spec = None
        self._labels = []
        self.setVisible(False)

    def keys(self):
        return list(self._widgets)

    def load(self, spec, values=None) -> None:
        """Show ``spec`` (or hide the box for None) with ``values`` in it."""
        values = values or {}
        if spec is not self._spec:
            self._build(spec)
        for key, w in self._widgets.items():
            w.blockSignals(True)
            if isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(values.get(key, w.property("default"))))
            else:
                w.setValue(float(values.get(key, w.property("default"))))
            w.blockSignals(False)
        self.setVisible(bool(spec))

    def _build(self, spec) -> None:
        for w in list(self._widgets.values()) + self._labels:
            self._grid.removeWidget(w)
            w.deleteLater()
        self._widgets, self._labels, self._spec = {}, [], spec
        if not spec:
            return
        self.setTitle(spec.get("title", ""))
        row = col = 0
        for f in spec.get("fields", ()):
            if f.get("kind") == "check":
                if col:
                    row, col = row + 1, 0
                w = QtWidgets.QCheckBox(f.get("label", ""))
                w.setProperty("default", bool(f.get("default", False)))
                w.toggled.connect(self.changed)
                self._grid.addWidget(w, row, 0, 1, 4)
                row += 1
            else:
                lab = QtWidgets.QLabel(f.get("label", ""))
                w = QtWidgets.QDoubleSpinBox()
                w.setDecimals(int(f.get("decimals", 3)))
                w.setRange(f.get("min", 0.0), f.get("max", 1e6))
                w.setSingleStep(f.get("step", 0.01))
                w.setSuffix(f.get("suffix", ""))
                if f.get("special"):
                    w.setSpecialValueText(f["special"])
                w.setProperty("default", float(f.get("default", f.get("min", 0.0))))
                w.valueChanged.connect(self.changed)
                lab.setToolTip(f.get("tip", ""))
                self._labels.append(lab)
                self._grid.addWidget(lab, row, col * 2)
                self._grid.addWidget(w, row, col * 2 + 1)
                col += 1
                if col == 2:
                    row, col = row + 1, 0
            w.setToolTip(f.get("tip", ""))
            self._widgets[f["key"]] = w

    def values(self) -> dict:
        out = {}
        for key, w in self._widgets.items():
            out[key] = w.isChecked() if isinstance(w, QtWidgets.QCheckBox) else w.value()
        return out

    def set_live(self, on: bool) -> None:
        for w in list(self._widgets.values()) + self._labels:
            w.setEnabled(on)


__all__ = ["FormBox"]
