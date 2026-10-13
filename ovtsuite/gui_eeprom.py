# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > EEPROM editor: one editor for every ECU EEPROM a module knows.

The editor is the base app's; what an EEPROM holds is the module's. A module
that knows an EEPROM layout names it in its ``ModuleSpec.eeprom`` hook, which
returns a *layout* object (Bosch EDC15: ``ovtsuite.edc15.eeprom_layout``,
EDC16: ``ovtsuite.edc16.eeprom_layout``). The layout protocol, duck-typed:

``name``            "Bosch EDC16"
``size``            image size in bytes (512, 4096)
``flasher_ecu``     the flasher's ECU entry that writes it, or None
``fields``          objects with ``key name offset length kind note`` and
                    ``locations()`` (the primary copy first, then mirrors)
``identify(data)``  how sure it is that ``data`` is its layout: 0 (no) .. 2
                    (its own checksums agree); 1 = right size only
``get(data, f)``    the field's value as shown
``mirrors_agree(data, f)``
``apply(data, f, text)``  new bytes with the value written to every copy and
                    the checksums corrected (ValueError with the reason)
``checksum_text(data)``   one line on the checksum state
``actions``         ``[(label, tooltip, question, fn(data) -> bytes)]`` --
                    e.g. EDC15 Virginize, EDC16 Immobiliser off

A 512-byte image opens as EDC15 and a 4 KB one as EDC16 when their modules
are installed; when two layouts fit, the one whose checksums agree wins and
the other stays one click away (Layout).
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

_CHANGED_BG = QtGui.QColor(255, 214, 120)
_CHANGED_FG = QtGui.QColor(20, 20, 20)
_MISMATCH_BG = QtGui.QColor(240, 150, 150)
FILTER = "EEPROM images (*.bin *.eep *.rom);;All files (*)"


def layouts() -> List[object]:
    """The EEPROM layouts of the installed modules."""
    from .modules import registry as _registry
    return [lay for _spec, lay in _registry.eeprom_layouts()]


def available() -> bool:
    return bool(layouts())


def layouts_for(data) -> List[object]:
    """The layouts ``data`` fits, the most likely first."""
    fits = []
    for lay in layouts():
        try:
            score = int(lay.identify(bytes(data)))
        except Exception:
            score = 0
        if score > 0:
            fits.append((score, lay))
    fits.sort(key=lambda t: -t[0])
    return [lay for _s, lay in fits]


def _icon():
    try:
        from .gui import app_icon
        return app_icon()
    except Exception:
        return QtGui.QIcon()


class EepromEditor(QtWidgets.QDialog):
    """The fields of the EEPROM (editable; mirrors and checksums follow), a
    hex view with the edited bytes marked, and Save / Save As / the layout's
    own actions / Write to ECU."""

    def __init__(self, parent, data, path=None, name=None, on_save=None,
                 layout=None):
        super().__init__(parent)
        data = bytes(data)
        self._window = parent
        self._on_save = on_save          # held by a project / container: Save keeps it there
        self.path = path
        self._name = name or (os.path.basename(path) if path else
                              "ECU EEPROM (unsaved)")
        cands = [layout] if layout is not None else layouts_for(data)
        if not cands:
            raise ValueError(self._no_layout_text(len(data)))
        self.layout = cands[0]
        self._cands = cands if layout is None else [layout]
        self.original = data
        self.data = data
        self._updating = False
        self.setWindowIcon(_icon())
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry() \
            if QtGui.QGuiApplication.primaryScreen() else QtCore.QRect(0, 0, 1280, 720)
        self.resize(min(880, avail.width() - 40), min(660, int(avail.height() * 0.85)))

        v = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Layout:"))
        self.layout_box = QtWidgets.QComboBox()
        for lay in self._cands:
            self.layout_box.addItem("%s (%d bytes)" % (lay.name, lay.size))
        self.layout_box.setEnabled(len(self._cands) > 1)
        self.layout_box.setToolTip("Which control unit's EEPROM this is -- chosen "
                                   "from its size and its own checksums")
        self.layout_box.currentIndexChanged.connect(self._layout_changed)
        top.addWidget(self.layout_box)
        top.addStretch(1)
        v.addLayout(top)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Field", "Value", "Address", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(self.table)
        self.hex = QtWidgets.QTextEdit()
        self.hex.setReadOnly(True)
        self.hex.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))
        self.hex.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.NoWrap)
        self.hex.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                               QtWidgets.QSizePolicy.Policy.Expanding)
        split.addWidget(self.hex)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

        self.status = QtWidgets.QLabel()
        self.status.setStyleSheet("color:gray")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                  QtWidgets.QSizePolicy.Policy.Preferred)
        v.addWidget(self.status)

        btns = QtWidgets.QHBoxLayout()
        self.buttons = {}
        for text, slot, tip in (
                ("Open...", self.open_file, "Open another EEPROM image"),
                ("Save", self.save, ""),
                ("Save As...", self.save_as, "Write the image to a .bin file"),
                ("Revert", self.revert, "Undo every edit since it was opened")):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
            self.buttons[text] = b
        self.save_btn = self.buttons["Save"]
        self._sync_save_btn()
        self._action_btns = []
        self._action_row = btns
        btns.addStretch(1)
        self.write_btn = QtWidgets.QPushButton("Write to ECU...")
        self.write_btn.clicked.connect(self.write_to_ecu)
        btns.addWidget(self.write_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        v.addLayout(btns)

        self.table.itemChanged.connect(self._item_changed)
        self._build_layout()

    # ---- layout ---------------------------------------------------------
    @staticmethod
    def _no_layout_text(n):
        names = ["%s (%d bytes)" % (lay.name, lay.size) for lay in layouts()]
        if not names:
            return ("No installed module knows an ECU EEPROM layout (the EDC15 "
                    "and EDC16 modules do).")
        return ("A %d-byte image is no EEPROM this editor knows: %s."
                % (n, ", ".join(names)))

    def _layout_changed(self, i):
        if 0 <= i < len(self._cands):
            self.layout = self._cands[i]
            self._build_layout()

    def _build_layout(self):
        lay = self.layout
        for b in self._action_btns:
            self._action_row.removeWidget(b)
            b.deleteLater()
        self._action_btns = []
        for i, (label, tip, question, fn) in enumerate(getattr(lay, "actions", ())):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, l=label, q=question, f=fn:
                              self._run_action(l, q, f))
            self._action_row.insertWidget(4 + i, b)
            self._action_btns.append(b)
        self.write_btn.setVisible(bool(getattr(lay, "flasher_ecu", None)))
        self.table.setRowCount(len(lay.fields))
        self._populate()

    # ---- view -------------------------------------------------------------
    def _title(self):
        mod = " *" if self.data != self.original else ""
        self.setWindowTitle("EEPROM editor (%s) - %s%s"
                            % (self.layout.name, self._name, mod))

    def _populate(self):
        lay = self.layout
        self._updating = True
        try:
            for row, f in enumerate(lay.fields):
                try:
                    val = lay.get(self.data, f)
                    ok = lay.mirrors_agree(self.data, f)
                except Exception as ex:          # a field past this image's end
                    val, ok = "(unreadable: %s)" % ex, True
                name = QtWidgets.QTableWidgetItem(f.name)
                name.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, 0, name)
                it = QtWidgets.QTableWidgetItem(str(val))
                it.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled
                            | QtCore.Qt.ItemFlag.ItemIsEditable)
                if any(self.data[o:o + n] != self.original[o:o + n]
                       for o, n in _spans(f)):
                    it.setBackground(_CHANGED_BG)
                    it.setForeground(_CHANGED_FG)
                self.table.setItem(row, 1, it)
                locs = [o for o, _n in _spans(f)]
                addr = "0x%03X" % locs[0]
                if len(locs) > 1:
                    addr += " (+" + ", ".join("0x%03X" % m for m in locs[1:]) + ")"
                a = QtWidgets.QTableWidgetItem(addr)
                a.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                if not ok:
                    a.setText(addr + "  copies differ")
                    a.setBackground(_MISMATCH_BG)
                    a.setForeground(_CHANGED_FG)
                self.table.setItem(row, 2, a)
                n = QtWidgets.QTableWidgetItem(f.note)
                n.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                n.setToolTip(f.note)
                self.table.setItem(row, 3, n)
        finally:
            self._updating = False
        self._render_hex()
        self._title()
        nchg = sum(1 for a, b in zip(self.data, self.original) if a != b)
        try:
            cks = lay.checksum_text(self.data)
        except Exception as ex:
            cks = "checksums could not be checked (%s)" % ex
        self.status.setText("%d byte(s) changed. %s Edits update every copy of a "
                            "field and the checksums." % (nchg, cks))

    def _render_hex(self):
        fg = self.palette().color(QtGui.QPalette.ColorRole.Text).name()
        lines = []
        for off in range(0, len(self.data), 16):
            cells = []
            for i in range(off, min(off + 16, len(self.data))):
                h = "%02X" % self.data[i]
                if i >= len(self.original) or self.data[i] != self.original[i]:
                    h = '<span style="background:%s;color:%s">%s</span>' % (
                        _CHANGED_BG.name(), _CHANGED_FG.name(), h)
                cells.append(h)
            asc = "".join(chr(c) if 32 <= c < 127 else "."
                          for c in self.data[off:off + 16])
            asc = asc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append('<span style="color:gray">%03X</span>  %s  %s'
                         % (off, " ".join(cells), asc))
        self.hex.setHtml('<pre style="color:%s">%s</pre>' % (fg, "\n".join(lines)))

    # ---- edits ------------------------------------------------------------
    def _item_changed(self, item):
        if self._updating or item.column() != 1:
            return
        f = self.layout.fields[item.row()]
        try:
            self.data = bytes(self.layout.apply(self.data, f, item.text().strip()))
        except (ValueError, TypeError) as ex:
            QtWidgets.QMessageBox.warning(self, "EEPROM editor", str(ex))
        self._populate()

    def _run_action(self, label, question, fn):
        if question:
            if QtWidgets.QMessageBox.question(self, label.rstrip("."), question) \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        try:
            self.data = bytes(fn(self.data))
        except (ValueError, TypeError) as ex:
            QtWidgets.QMessageBox.warning(self, label.rstrip("."), str(ex))
        self._populate()

    def revert(self):
        self.data = self.original
        self._populate()

    # ---- files ------------------------------------------------------------
    def open_file(self):
        from . import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open an EEPROM image", _recent.get_last_dir("eeprom"), FILTER)
        if not path:
            return
        with open(path, "rb") as fh:
            data = fh.read()
        cands = layouts_for(data)
        if not cands:
            QtWidgets.QMessageBox.critical(self, "EEPROM editor",
                                           self._no_layout_text(len(data)))
            return
        _recent.set_last_dir(os.path.dirname(path), "eeprom")
        self._on_save = None             # a file of its own now
        self._sync_save_btn()
        self.original = self.data = data
        self.path = path
        self._name = os.path.basename(path)
        self._cands = cands
        self.layout = cands[0]
        self.layout_box.blockSignals(True)
        self.layout_box.clear()
        for lay in cands:
            self.layout_box.addItem("%s (%d bytes)" % (lay.name, lay.size))
        self.layout_box.setEnabled(len(cands) > 1)
        self.layout_box.blockSignals(False)
        self._build_layout()

    def _sync_save_btn(self):
        """**Apply** for an EEPROM a tab holds (its edits go into the tab, as
        one undoable edit; File > Save keeps them in the project), **Save**
        for an image of its own on disk."""
        btn = getattr(self, "save_btn", None)
        if btn is None:
            return
        if self._on_save is not None:
            btn.setText("Apply")
            btn.setToolTip("Put the edits into the tab, as one undoable "
                           "edit; File > Save keeps them in the project")
        else:
            btn.setText("Save")
            btn.setToolTip("Write the edits to the image's file")

    def save(self):
        if self._on_save is not None:
            self._on_save(self.data)
            self.original = self.data
            self._populate()
            return
        if not self.path:
            return self.save_as()
        with open(self.path, "wb") as fh:
            fh.write(self.data)
        self.original = self.data
        self._populate()

    def save_as(self):
        """A standalone .bin of the image (a project's EEPROM is kept by Save)."""
        from . import recent as _recent
        base = self.path or os.path.join(
            _recent.get_last_dir("eeprom"),
            os.path.splitext(self._name.split(" [")[0])[0] + "_EEPROM.bin")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save EEPROM image as", base,
            "EEPROM images (*.bin);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".bin"
        with open(path, "wb") as fh:
            fh.write(self.data)
        _recent.set_last_dir(os.path.dirname(path), "eeprom")
        if self._on_save is None:
            self.path = path
            self._name = os.path.basename(path)
            self.original = self.data
        self._populate()

    def write_to_ecu(self):
        ecu = getattr(self.layout, "flasher_ecu", None)
        if not ecu:
            return
        try:
            from .flash.gui_flash import FlashDialog
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
            return
        dlg = FlashDialog(self._window, "eeprom", eeprom_image=self.data)
        i = dlg.ecu_box.findText(ecu)
        if i >= 0:
            dlg.ecu_box.setCurrentIndex(i)
        dlg.exec()


def _spans(f):
    """``[(offset, length)]`` of every copy of field ``f`` (a field split
    over two blocks lists each part)."""
    sp = getattr(f, "spans", None)
    if sp is not None:
        return list(sp())
    return [(o, f.length) for o in f.locations()]


def open_editor(parent, data, **kw) -> Optional[EepromEditor]:
    """The editor on ``data``, or None (said) when no layout fits."""
    try:
        return EepromEditor(parent, data, **kw)
    except ValueError as ex:
        QtWidgets.QMessageBox.information(parent, "EEPROM editor", str(ex))
        return None


__all__ = ["EepromEditor", "open_editor", "layouts", "layouts_for", "available",
           "FILTER"]
