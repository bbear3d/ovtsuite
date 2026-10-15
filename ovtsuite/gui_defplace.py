# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Placing a definition file (A2L, DAMOS, WinOLS, XDF) on a binary: the
dialog shown when one is attached, and again from the Project panel.

A definition gives addresses for the software it was made for. The app
places each table where evidence in this file confirms it
(:mod:`ovtsuite.definitions.place`) and this dialog says what it found: how
many tables sit where the definition puts them, how many moved and how they
were found, how many nothing confirms (hidden unless asked for). The offset
can be given instead of found, and the image the definition was made for
(an A2L's HEX) can be added so moved tables are confirmed by their bytes.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6 import QtCore, QtWidgets

#: what each grade means, in the words the dialog uses
GRADE_TEXT = {
    "exact": ("Where the definition puts them",
              "Confirmed at the definition's own address (at the offset "
              "below): the format's record header holds there, or the bytes "
              "of the original image are there."),
    "moved": ("Found moved",
              "Confirmed somewhere else than the definition says: the "
              "original image's bytes found once nearby, or the table laid "
              "out right next to a confirmed one, its header holding there."),
    "region": ("Moved with the tables around them",
               "The record header holds at the offset its neighbours moved by."),
    "bracketed": ("Between confirmed tables",
                  "Nothing of its own to check (a single value, a table with "
                  "fixed axes), but the confirmed tables on both sides "
                  "moved alike."),
    "stated": ("Where the definition puts them (nothing to check)",
               "This definition type states nothing about its records but "
               "their axes, so its tables are shown where it puts them."),
    "unverified": ("Unverified",
                   "Nothing confirms these tables anywhere in this file. They "
                   "are left out unless you choose to show them -- then in a "
                   "group of their own. Check them before writing."),
}


def _hex(v: int) -> str:
    return ("-0x%X" % -v) if v < 0 else ("0x%X" % v)


def _parse_hex(text: str) -> Optional[int]:
    t = text.strip().replace("_", "").replace(" ", "")
    if not t:
        return None
    neg = t.startswith("-")
    t = t.lstrip("+-")
    try:
        v = int(t, 16) if not t.lower().startswith("0x") else int(t, 0)
    except ValueError:
        return None
    return -v if neg else v


class PlacementDialog(QtWidgets.QDialog):
    """Review (and adjust) how a definition is placed on an image.

    ``options`` / ``reference`` (``(name, bytes)``) are what the project
    keeps for it; :meth:`result_options` / :meth:`result_reference` what the
    user settled on."""

    def __init__(self, parent, *, name: str, kind: str, data: bytes, image: bytes,
                 target: str, options: Optional[dict] = None,
                 reference: Optional[Tuple[str, bytes]] = None, accept_text: str = "OK",
                 builtin: Optional[bool] = None):
        super().__init__(parent)
        self.setWindowTitle("Place definition")
        self._name, self._kind, self._data = name, (kind or "").lower(), bytes(data)
        self._image = bytes(image)
        self._ref = reference
        self._opts = dict(options or {})
        self._pl = None
        self._dirty = False
        self.setMinimumWidth(560)

        lay = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QLabel(
            "<b>%s</b> (%s) on <b>%s</b>" % (_esc(name), self._kind.upper(), _esc(target)))
        head.setWordWrap(True)
        lay.addWidget(head)
        intro = QtWidgets.QLabel(
            "Each table is placed where evidence in this file confirms it. "
            "Tables nothing confirms are left out unless you choose to show them.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # ---- what was found
        box = QtWidgets.QGroupBox("Result")
        self._form = QtWidgets.QFormLayout(box)
        self._form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        lay.addWidget(box)
        self._advice = QtWidgets.QLabel()
        self._advice.setWordWrap(True)
        self._advice.setVisible(False)
        self._advice.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); border-radius: 4px; padding: 6px; }")
        lay.addWidget(self._advice)

        self._details_btn = QtWidgets.QToolButton()
        self._details_btn.setText("Regions")
        self._details_btn.setCheckable(True)
        self._details_btn.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._details_btn.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self._details_btn.toggled.connect(self._toggle_details)
        lay.addWidget(self._details_btn)
        self._regions = QtWidgets.QTreeWidget()
        self._regions.setHeaderLabels(["Definition addresses", "Moved by", "Tables"])
        self._regions.setRootIsDecorated(False)
        self._regions.setVisible(False)
        self._regions.setMinimumHeight(140)
        lay.addWidget(self._regions)

        # ---- options
        opt = QtWidgets.QGroupBox("Options")
        og = QtWidgets.QGridLayout(opt)
        self._auto = QtWidgets.QRadioButton("Find the offset")
        self._fixed = QtWidgets.QRadioButton("Use offset")
        self._offset = QtWidgets.QLineEdit()
        self._offset.setPlaceholderText("e.g. 0x800000")
        self._offset.setToolTip(
            "Definition address minus file offset: a table the definition "
            "puts at 0x8D1000 is read at file offset 0xD1000 with 0x800000.")
        grp = QtWidgets.QButtonGroup(self)
        grp.addButton(self._auto)
        grp.addButton(self._fixed)
        og.addWidget(self._auto, 0, 0, 1, 2)
        og.addWidget(self._fixed, 1, 0)
        og.addWidget(self._offset, 1, 1)
        self._relocate = QtWidgets.QCheckBox("Look for tables that moved")
        self._relocate.setToolTip(
            "On another software version parts of the calibration move. "
            "Off: every table is checked at the one offset only.")
        og.addWidget(self._relocate, 2, 0, 1, 2)
        self._show = QtWidgets.QCheckBox(
            "Also show unverified tables (in a group of their own)")
        og.addWidget(self._show, 3, 0, 1, 2)
        # a binary a module defines: its built-in maps beside these, or not
        self._hide_builtin = QtWidgets.QCheckBox(
            "Hide the module's built-in maps (show only this definition's)")
        self._hide_builtin.setToolTip(
            "The module still identifies the binary and corrects its "
            "checksums; its tuning tools that rely on its own map list are "
            "unavailable while its maps are hidden.")
        self._hide_builtin.setVisible(builtin is not None)
        self._hide_builtin.setChecked(bool(builtin))
        og.addWidget(self._hide_builtin, 5, 0, 1, 2)
        og.addWidget(QtWidgets.QLabel("Original image:"), 4, 0)
        refrow = QtWidgets.QHBoxLayout()
        self._ref_label = QtWidgets.QLabel()
        self._ref_label.setWordWrap(True)
        refrow.addWidget(self._ref_label, 1)
        self._ref_pick = QtWidgets.QPushButton("Choose...")
        self._ref_pick.setToolTip(
            "The image the definition was made for -- an A2L's HEX file, or "
            "the binary it describes. With it, tables that moved in this "
            "file are confirmed by their bytes.")
        self._ref_pick.clicked.connect(self._choose_reference)
        self._ref_clear = QtWidgets.QPushButton("Remove")
        self._ref_clear.clicked.connect(self._clear_reference)
        refrow.addWidget(self._ref_pick)
        refrow.addWidget(self._ref_clear)
        og.addLayout(refrow, 4, 1)
        lay.addWidget(opt)

        bb = QtWidgets.QDialogButtonBox()
        self._recalc = bb.addButton("Recalculate", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self._recalc.clicked.connect(self._compute)
        self._ok = bb.addButton(accept_text, QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        # initial state
        base = self._opts.get("base")
        self._auto.setChecked(base is None)
        self._fixed.setChecked(base is not None)
        if base is not None:
            self._offset.setText(_hex(int(base)))
        self._relocate.setChecked(bool(self._opts.get("relocate", True)))
        self._show.setChecked(bool(self._opts.get("unverified")))
        self._offset.setEnabled(base is not None)
        self._auto.toggled.connect(lambda on: self._offset.setEnabled(not on))
        for sig in (self._auto.toggled, self._relocate.toggled):
            sig.connect(self._changed)
        self._offset.textEdited.connect(self._changed)
        self._show.toggled.connect(self._show_changed)
        self._update_reference_label()

    # ------------------------------------------------------------ results
    def result_options(self) -> dict:
        out = {}
        if self._fixed.isChecked():
            v = _parse_hex(self._offset.text())
            if v is not None:
                out["base"] = v
        if not self._relocate.isChecked():
            out["relocate"] = False
        if self._show.isChecked():
            out["unverified"] = True
        return out

    def result_reference(self) -> Optional[Tuple[str, bytes]]:
        return self._ref

    @property
    def placement(self):
        return self._pl

    # ------------------------------------------------------------ work
    def compute(self) -> bool:
        """Place the definition with the options shown (before :meth:`exec`)."""
        return self._compute()

    def _compute(self) -> bool:
        from . import xdf_session
        opts = self.result_options()
        if self._fixed.isChecked() and "base" not in opts:
            QtWidgets.QMessageBox.warning(
                self, "Place definition", "The offset is not a number (hex, "
                "e.g. 0x800000).")
            return False
        dlg = QtWidgets.QProgressDialog("Placing %s..." % self._name, None, 0, 0,
                                        self.parentWidget() or self)
        dlg.setWindowTitle("Place definition")
        dlg.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.show()

        def report(text):
            dlg.setLabelText("%s\n%s" % (self._name, text))
            QtWidgets.QApplication.processEvents()

        report("Reading the definition")
        try:
            _defn, pl = xdf_session.placed_definition(
                self._data, self._kind, self._image, opts,
                self._ref[1] if self._ref else None, progress=report)
        except Exception as ex:
            dlg.close()
            QtWidgets.QMessageBox.critical(self, "Place definition",
                                           "The definition could not be placed: %s" % ex)
            return False
        dlg.close()
        self._pl = pl
        self._dirty = False
        self._show_result()
        return True

    def _accept(self):
        if self._dirty and not self._compute():
            return
        self.accept()

    def _changed(self, *_a):
        self._dirty = True
        self._recalc.setDefault(True)

    def _show_changed(self, *_a):
        if self._pl is not None:
            self._show_result()

    # ------------------------------------------------------------ showing
    def _show_result(self):
        pl = self._pl
        while self._form.rowCount():
            self._form.removeRow(0)
        c = pl.counts()
        shown = sum(v for g, v in c.items() if g != "unverified")
        for g in ("exact", "moved", "region", "bracketed", "stated", "unverified"):
            if not c[g] and g not in ("exact",):
                continue
            label, tip = GRADE_TEXT[g]
            text = "%d" % c[g]
            if g == "unverified":
                text += " (shown, in their own group)" if self._show.isChecked() \
                    else " (left out)"
            v = QtWidgets.QLabel(text)
            k = QtWidgets.QLabel(label + ":")
            for w in (k, v):
                w.setToolTip(tip)
            if g == "unverified" and c[g]:
                f = v.font()
                f.setBold(True)
                v.setFont(f)
            self._form.addRow(k, v)
        self._form.addRow(QtWidgets.QLabel("Tables shown:"),
                          QtWidgets.QLabel("<b>%d</b> of %d" % (
                              shown + (c["unverified"] if self._show.isChecked() else 0),
                              len(pl.items))))
        off = QtWidgets.QLabel("%s%s" % (_hex(pl.base),
                                         " (given)" if pl.override else " (found)"))
        off.setToolTip("Definition address minus file offset.")
        self._form.addRow(QtWidgets.QLabel("Offset:"), off)
        if pl.checkable:
            fit = QtWidgets.QLabel("%d of %d checked tables (%d%%)" % (
                pl.confirmed, pl.checkable, round(100 * pl.fit)))
            fit.setToolTip("Tables with something to check -- a record header "
                           "the format states, stored axes, or the original "
                           "image's bytes -- confirmed at this offset.")
            self._form.addRow(QtWidgets.QLabel("Confirmed at the offset:"), fit)
        if not self._fixed.isChecked():
            self._offset.setText(_hex(pl.base))
        self._advice.setText(advice(pl, self._ref is not None, self._kind))
        self._advice.setVisible(bool(self._advice.text()))
        self._regions.clear()
        for r in pl.regions:
            it = QtWidgets.QTreeWidgetItem([
                "%s - %s" % (_hex(r.start), _hex(r.end - 1)),
                "%+d bytes" % r.shift if r.shift else "-", str(r.tables)])
            self._regions.addTopLevelItem(it)
        for i in range(3):
            self._regions.resizeColumnToContents(i)
        self._details_btn.setText("Regions (%d)" % len(pl.regions))

    def _toggle_details(self, on):
        self._regions.setVisible(on)
        self._details_btn.setArrowType(QtCore.Qt.ArrowType.DownArrow if on
                                       else QtCore.Qt.ArrowType.RightArrow)
        self.adjustSize()

    # ------------------------------------------------------------ reference
    def _update_reference_label(self):
        carried = self._kind == "ols"
        if self._ref is not None:
            self._ref_label.setText(self._ref[0])
        elif carried:
            self._ref_label.setText("carried by the WinOLS project")
        else:
            self._ref_label.setText("none")
        self._ref_clear.setEnabled(self._ref is not None)

    def _choose_reference(self):
        from .recent import get_last_dir, set_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "The image %s was made for" % self._name,
            get_last_dir("definition"),
            "Images (*.hex *.ihex *.bin *.ori);;All files (*)")
        if not path:
            return
        set_last_dir(path, "definition")
        with open(path, "rb") as fh:
            self._ref = (os.path.basename(path), fh.read())
        self._update_reference_label()
        self._changed()
        self._compute()

    def _clear_reference(self):
        self._ref = None
        self._update_reference_label()
        self._changed()
        self._compute()


def advice(pl, have_reference: bool, kind: str) -> str:
    """One paragraph on what the result means, or "" when nothing needs saying."""
    c = pl.counts()
    total = len(pl.items) or 1
    if not pl.established:
        if kind in ("xdf", "kp") and not have_reference:
            return ("The tables' stored axes do not rise at this offset any "
                    "more often than by chance: the definition does not seem "
                    "to be for this file. Its tables are left out unless you "
                    "show unverified tables; give the offset if you know it.")
        return ("The definition's record headers hold at this offset no more "
                "often than by chance: it does not seem to be for this file "
                "(another ECU, or another generation of its software). Only "
                "what its own evidence places is shown.")
    if c["stated"] and not c["moved"]:
        return ""
    moved = c["moved"] + c["region"]
    if c["unverified"] > total * 0.2 or moved:
        if not have_reference and kind != "ols":
            return ("Parts of this file differ from the software the "
                    "definition was made for; moved tables were followed by "
                    "their structure alone. Add the original image (the "
                    "A2L's HEX file, or the binary it was made for) to "
                    "confirm them by their bytes and place more of them.")
        if c["unverified"] > total * 0.2:
            return ("Parts of this file differ from the software the definition "
                    "was made for: %d tables could not be found. They are "
                    "left out unless you show unverified tables." % c["unverified"])
    return ""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def nothing_to_review(pl) -> bool:
    """Every table placed where the definition puts it (the definition is for
    this very software): no reason to stop and ask."""
    c = pl.counts()
    return pl.established and not (c["unverified"] or c["moved"] or c["region"])


def review(parent, *, name, kind, data, image, target, options=None, reference=None,
           accept_text="OK", always=False, builtin=None):
    """Place the definition and show the dialog -- unless it placed every
    table where the definition puts it and ``always`` is False. Returns
    ``(options, reference)`` when accepted (or not asked), else None
    (``reference``: ``(name, bytes)`` or None). ``builtin`` (a binary a module
    defines): whether the module's built-in maps are hidden; the choice comes
    back as ``options["hide_builtin"]``."""
    dlg = PlacementDialog(parent, name=name, kind=kind, data=data, image=image,
                          target=target, options=options, reference=reference,
                          accept_text=accept_text, builtin=builtin)
    if not dlg.compute():
        return None

    def result():
        opts = dlg.result_options()
        if builtin is not None:
            opts["hide_builtin"] = dlg._hide_builtin.isChecked()
        return opts, dlg.result_reference()
    # (unattended runs -- the test suite -- take what was found)
    if (not always and nothing_to_review(dlg.placement)) or \
            os.environ.get("OVT_NO_PLACE_PROMPT"):
        return result()
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return result()


__all__ = ["PlacementDialog", "review", "nothing_to_review", "advice", "GRADE_TEXT"]
