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
the minimum as "not given"). A ``"kind": "tyre"`` field picks a tyre by its
marking -- width / aspect ratio R rim -- and gives the hook the rolling
circumference in metres under its key (0: not set); with ``fs_form_defaults``
giving that key (the circumference the file implies) the picker can also
"keep the file's tyre". Its choices travel as ``<key>_mode`` / ``_w`` / ``_a``
/ ``_r``, which the hook ignores. ``fs_form_defaults(session, group)`` (optional)
returns ``{key: value}`` to start the form from what the file holds. Two
number fields share a row; a tick box takes its own.
"""
from __future__ import annotations

from typing import Dict

from PySide6 import QtCore, QtWidgets


class TyreEdit(QtWidgets.QWidget):
    """A tyre picked by its marking: ``[mode] [195] / [65] R [15] = 1.987 m``.
    Modes: not set, the tyre the file's own V/N bands imply (when known), or a
    size. :meth:`value` is the rolling circumference in metres, 0 for not set."""

    changed = QtCore.Signal()
    NOT_SET, FILE, SIZE = "off", "file", "size"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.mode = QtWidgets.QComboBox()
        lay.addWidget(self.mode)
        self.w = QtWidgets.QSpinBox()
        self.w.setRange(125, 395)
        self.w.setSingleStep(5)
        self.w.setValue(195)
        self.w.setSuffix(" mm")
        self.a = QtWidgets.QSpinBox()
        self.a.setRange(25, 85)
        self.a.setSingleStep(5)
        self.a.setValue(65)
        self.r = QtWidgets.QSpinBox()
        self.r.setRange(12, 24)
        self.r.setValue(15)
        self.r.setPrefix("R")
        self.r.setSuffix(" in")
        self.parts = [self.w, QtWidgets.QLabel("/"), self.a, QtWidgets.QLabel("%"),
                      self.r]
        for p in self.parts:
            lay.addWidget(p)
        self.out = QtWidgets.QLabel()
        self.out.setStyleSheet("font-weight:600")
        lay.addWidget(self.out)
        lay.addStretch(1)
        self._file = 0.0
        self._set_modes(0.0)
        self.mode.currentIndexChanged.connect(self._edited)
        for sb in (self.w, self.a, self.r):
            sb.valueChanged.connect(self._edited)

    def _set_modes(self, file_circ: float, keep=None):
        self._file = float(file_circ or 0.0)
        keep = keep or self.mode.currentData() or self.NOT_SET
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItem("Not set", self.NOT_SET)
        if self._file > 0:
            self.mode.addItem("The file's tyre (%.3f m)" % self._file, self.FILE)
        self.mode.addItem("Tyre size:", self.SIZE)
        i = self.mode.findData(keep)
        self.mode.setCurrentIndex(i if i >= 0 else 0)
        self.mode.blockSignals(False)
        self._sync()

    def size_circ(self) -> float:
        from .gears import tyre_circumference
        return tyre_circumference(self.w.value(), self.a.value(), self.r.value())

    def value(self) -> float:
        m = self.mode.currentData()
        return (self._file if m == self.FILE else
                self.size_circ() if m == self.SIZE else 0.0)

    def _sync(self):
        size = self.mode.currentData() == self.SIZE
        for p in self.parts:
            p.setVisible(size)
        v = self.value()
        self.out.setText("= %.3f m rolling circumference" % self.size_circ()
                         if size else "")
        self.setToolTip("The tyre's marking (205/55 R16: width, aspect ratio, rim) "
                        "gives the rolling circumference the V/N bands are worked "
                        "out from%s." % (": %.3f m" % v if v else ""))

    def _edited(self, *_a):
        self._sync()
        self.changed.emit()

    def extra(self, key: str) -> dict:
        return {key + "_mode": self.mode.currentData(), key + "_w": self.w.value(),
                key + "_a": self.a.value(), key + "_r": self.r.value()}

    def load(self, key: str, values: dict):
        """Show what ``values`` hold: the choices a previous edit left, else the
        file's tyre when ``values[key]`` (the circumference the file implies) is
        given, else not set."""
        for sb, k, d in ((self.w, "_w", 195), (self.a, "_a", 65), (self.r, "_r", 15)):
            sb.blockSignals(True)
            sb.setValue(int(values.get(key + k, d)))
            sb.blockSignals(False)
        file_c = float(values.get(key + "_file", values.get(key, 0.0)) or 0.0)
        mode = values.get(key + "_mode")
        self._set_modes(file_c, mode or (self.FILE if file_c > 0 else self.NOT_SET))


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
            if isinstance(w, TyreEdit):
                w.load(key, values)
            elif isinstance(w, QtWidgets.QCheckBox):
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
            if f.get("kind") == "tyre":
                if col:
                    row, col = row + 1, 0
                lab = QtWidgets.QLabel(f.get("label", ""))
                w = TyreEdit()
                w.setProperty("default", 0.0)
                w.changed.connect(self.changed)
                lab.setToolTip(f.get("tip", ""))
                self._labels.append(lab)
                self._grid.addWidget(lab, row, 0)
                self._grid.addWidget(w, row, 1, 1, 3)
                row += 1
            elif f.get("kind") == "check":
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
            if isinstance(w, TyreEdit):
                out[key] = w.value()
                out.update(w.extra(key))
            else:
                out[key] = w.isChecked() if isinstance(w, QtWidgets.QCheckBox) else w.value()
        return out

    def set_live(self, on: bool) -> None:
        for w in list(self._widgets.values()) + self._labels:
            w.setEnabled(on)


__all__ = ["FormBox", "TyreEdit"]
