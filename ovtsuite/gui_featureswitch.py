# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Feature switches... window (:mod:`ovtsuite.featureswitch`).

Turn an engine ECU's functions on and off as parameter bundles. The bundles
come from the platform's module (its ``features`` hook); a function that is
more than a switch names a *table hook*, which this window asks what it will
do (``fs_describe``) and has write it (``fs_apply``). The left list
is the features this file carries; the right pane shows a feature's current
state, a target state to set, which copies (datasets / coding variants) to
write, and a preview of every value that will change. Apply writes the
ticked copies as one undo step; checksums are corrected on save.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6 import QtCore, QtWidgets

from .featureswitch import model as M, engine as E


def _g():
    from . import gui
    return gui


def _view_only(ws) -> bool:
    return bool(getattr(ws, "view_only", None))


def availability(ws) -> Optional[str]:
    """None when the window can open for tab ``ws``, else why not. Asked on
    every Tools-menu refresh: the answer is kept per entity list."""
    s = getattr(ws, "primary", None)
    if s is not None:
        ents = getattr(s, "entities", None)
        hit = getattr(s, "_fsw_avail", None)
        if hit is not None and hit[0] is ents:
            return hit[1]
        why = _availability(s)
        try:
            s._fsw_avail = (ents, why)
        except Exception:
            pass
        return why
    return "No file open"


def _availability(s) -> Optional[str]:
    if s is None:
        return "No file open"
    if getattr(s, "support", "full") != "full":
        return "Needs the platform's module and a definition for this file"
    if not M.feature_set(s).bundles:
        return ("The installed module for this file has no feature switches "
                "(EDC15 and EDC16 engine ECUs have them)")
    if not _present_bundles(s):
        return "This file's definition places none of the feature parameters"
    return None


def _present_bundles(s) -> List[M.Bundle]:
    """Bundles whose parameters this file actually carries, family-filtered."""
    fam = getattr(s, "family", None)
    out = []
    for b in M.bundles_for(s):
        if b.families and fam not in b.families:
            continue
        if E.is_present(s, b):
            out.append(b)
    return out


def open_for(ws):
    why = availability(ws)
    if why:
        QtWidgets.QMessageBox.information(ws, "Feature switches", why)
        return None
    return ws.open_tool("featureswitch", lambda: FeatureSwitchDialog(ws))


_PROV = {M.CODE: "code", M.A2L: "A2L", M.STOCK: "stock",
         M.DERIVED: "derived", M.USER: "user"}


class FeatureSwitchDialog(QtWidgets.QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.setWindowTitle("Feature switches")
        try:
            self.setWindowIcon(_g().app_icon())
        except Exception:
            pass
        from .gui_fit import fit_to_screen
        fit_to_screen(self, 980, 640)
        self.bundles = _present_bundles(self.session)
        self._copy_boxes: List[QtWidgets.QCheckBox] = []

        root = QtWidgets.QHBoxLayout(self)

        # -- left: the feature list ---------------------------------------
        left = QtWidgets.QVBoxLayout()
        fam = getattr(self.session, "family", "") or ""
        left.addWidget(QtWidgets.QLabel(
            "<b>%s</b> &nbsp; <span style='color:gray'>%s</span>"
            % (getattr(self.session, "platform", "").upper(), fam)))
        self.list = QtWidgets.QListWidget()
        for b in self.bundles:
            it = QtWidgets.QListWidgetItem(b.title)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, b.id)
            if b.readonly:
                it.setForeground(QtCore.Qt.GlobalColor.gray)
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._select)
        left.addWidget(self.list, 1)
        root.addLayout(left, 0)

        # -- right: the detail pane ---------------------------------------
        right = QtWidgets.QVBoxLayout()
        self.title = QtWidgets.QLabel()
        self.title.setStyleSheet("font-weight:bold;font-size:14px")
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:gray")
        right.addWidget(self.title)
        right.addWidget(self.summary)

        row = QtWidgets.QHBoxLayout()
        self.cur_lbl = QtWidgets.QLabel()
        row.addWidget(self.cur_lbl)
        row.addStretch(1)
        row.addWidget(QtWidgets.QLabel("Set to:"))
        self.state = QtWidgets.QComboBox()
        self.state.currentIndexChanged.connect(self._refresh_preview)
        row.addWidget(self.state)
        # a table hook's own number (the dynamic-EGR warm point ...), shown
        # for the feature whose hook declares one (FS_OPTION)
        self.opt_lbl = QtWidgets.QLabel()
        self.opt = QtWidgets.QDoubleSpinBox()
        self.opt.setDecimals(0)
        self.opt.valueChanged.connect(self._refresh_preview)
        self._opt_spec = None
        row.addWidget(self.opt_lbl)
        row.addWidget(self.opt)
        right.addLayout(row)

        self.copies_box = QtWidgets.QGroupBox("Copies to write")
        self.copies_lay = QtWidgets.QVBoxLayout(self.copies_box)
        right.addWidget(self.copies_box)

        self.preview = QtWidgets.QTableWidget(0, 5)
        self.preview.setHorizontalHeaderLabels(
            ["Parameter", "Meaning", "Source", "Current", "→ New"])
        self.preview.horizontalHeader().setStretchLastSection(True)
        self.preview.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        right.addWidget(self.preview, 1)

        self.hook_info = QtWidgets.QLabel()
        self.hook_info.setWordWrap(True)
        self.hook_info.setStyleSheet("color:#06a")
        right.addWidget(self.hook_info)

        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color:#a60")
        right.addWidget(self.notes)

        btns = QtWidgets.QHBoxLayout()
        self.version = QtWidgets.QCheckBox("Save as project version")
        self.version.setChecked(True)
        self.version.setVisible(getattr(ws, "project_ref", None) is not None)
        btns.addWidget(self.version)
        btns.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.clicked.connect(self._apply)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        btns.addWidget(self.apply_btn)
        btns.addWidget(close)
        right.addLayout(btns)
        root.addLayout(right, 1)

        if self.bundles:
            self.list.setCurrentRow(0)

    # -- selection / preview ---------------------------------------------
    def _bundle(self) -> Optional[M.Bundle]:
        it = self.list.currentItem()
        if it is None:
            return None
        bid = it.data(QtCore.Qt.ItemDataRole.UserRole)
        return next((b for b in self.bundles if b.id == bid), None)

    def _select(self, *_a):
        b = self._bundle()
        if b is None:
            return
        self.title.setText(b.title)
        self.summary.setText(b.summary)
        self.notes.setText("  •  ".join(b.notes))
        # copies
        for cb in self._copy_boxes:
            cb.setParent(None)
        self._copy_boxes = []
        gs = E.groups(self.session, b)
        for g in gs:
            st = E.current_state(self.session, b, g)
            cb = QtWidgets.QCheckBox(
                "%s   —   now: %s" % (g or "(only copy)", st or "custom"))
            cb.setChecked(True)
            cb.stateChanged.connect(self._refresh_preview)
            self.copies_lay.addWidget(cb)
            self._copy_boxes.append(cb)
        self.copies_box.setVisible(len(gs) > 1)
        # states
        self.state.blockSignals(True)
        self.state.clear()
        self.state.setEnabled(not b.readonly and not _view_only(self.ws))
        if not b.readonly:
            self.state.addItems(b.states)
            cur = E.current_state(self.session, b, gs[0] if gs else "")
            if cur in b.states:
                self.state.setCurrentIndex(b.states.index(cur))
        self.state.blockSignals(False)
        spec = getattr(self._hook(b), "FS_OPTION", None)
        if spec != self._opt_spec:
            self._opt_spec = spec
            if spec:
                self.opt.blockSignals(True)
                self.opt.setRange(spec.get("min", 0.0), spec.get("max", 1e6))
                self.opt.setSingleStep(spec.get("step", 1.0))
                self.opt.setSuffix(spec.get("suffix", ""))
                self.opt.setValue(spec.get("default", 0.0))
                self.opt.setToolTip(spec.get("tip", ""))
                self.opt_lbl.setText(spec.get("label", ""))
                self.opt.blockSignals(False)
        self.opt_lbl.setVisible(bool(spec))
        self.opt.setVisible(bool(spec))
        self.apply_btn.setEnabled(not b.readonly and not _view_only(self.ws))
        self._refresh_preview()

    def _sel_groups(self) -> List[str]:
        gs = E.groups(self.session, self._bundle())
        if len(gs) <= 1:
            return gs
        return [g for g, cb in zip(gs, self._copy_boxes) if cb.isChecked()]

    def _refresh_preview(self, *_a):
        b = self._bundle()
        self.preview.setRowCount(0)
        if b is None:
            return
        gs = E.groups(self.session, b)
        self.cur_lbl.setText("Current: <b>%s</b>" % (
            E.current_state(self.session, b, gs[0] if gs else "") or "custom"))
        params = E.present_params(self.session, b)
        target = self.state.currentText() if not b.readonly else None
        sel = self._sel_groups()
        # one row per (param); show first selected copy's current -> new
        g0 = sel[0] if sel else (gs[0] if gs else "")
        vals = E.read_values(self.session, b, g0)
        for p in params:
            r = self.preview.rowCount()
            self.preview.insertRow(r)
            cur = vals.get(p.name)
            new = None if (b.readonly or target is None) else p.target(target)
            cells = [p.name, p.meaning, _PROV.get(p.prov, p.prov),
                     "" if cur is None else str(cur),
                     "" if new is None else str(int(new))]
            for c, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if c == 4 and new is not None and cur is not None and int(new) != cur:
                    item.setForeground(QtCore.Qt.GlobalColor.red)
                self.preview.setItem(r, c, item)
        self.preview.resizeColumnsToContents()
        self._hook_info(b, target, sel)

    def _hook(self, b):
        return M.hook(self.session, b.table_hook) if b is not None else None

    def _opts(self) -> Dict[str, float]:
        spec = self._opt_spec
        return {spec["key"]: self.opt.value()} if spec and spec.get("key") else {}

    def _describe(self, b, target, group) -> str:
        h = self._hook(b)
        fn = getattr(h, "fs_describe", None)
        if fn is None:
            return ""
        try:
            return fn(self.session, group, target, **self._opts()) or ""
        except Exception as ex:                         # noqa: BLE001
            return "(%s)" % ex

    def _hook_info(self, b, target, sel):
        """Say what a table-hook feature's extra table edit will do."""
        self.hook_info.setText(self._describe(b, target, sel[0] if sel else ""))

    # -- apply ------------------------------------------------------------
    def _apply(self):
        b = self._bundle()
        if b is None or b.readonly:
            return
        target = self.state.currentText()
        sel = self._sel_groups()
        if not sel:
            QtWidgets.QMessageBox.information(
                self, "Feature switches", "Tick at least one copy to write.")
            return
        all_changes: List[E.Change] = []
        warns: List[str] = []
        for g in sel:
            ch, w = E.plan(self.session, b, target, [g])
            all_changes += ch
            warns += w
        live = [c for c in all_changes if c.changed]
        # the table-hook note from plan() is surfaced by the dialog itself
        warns = [w for w in warns if not b.table_hook or b.table_hook not in w]

        hook = self._hook(b)
        apply_hook = getattr(hook, "fs_apply", None)
        about = self._describe(b, target, sel[0]) if hook is not None else ""

        if warns:
            QtWidgets.QMessageBox.warning(
                self, "Feature switches", "\n".join(dict.fromkeys(warns)))
        if not live and apply_hook is None:
            QtWidgets.QMessageBox.information(
                self, "Feature switches",
                "Already '%s' on the ticked copies — nothing to change." % target)
            return
        ncop = len(sel)
        label = "%s → %s%s" % (
            b.title, target, "" if ncop == 1 else " (%d copies)" % ncop)
        preview = "\n".join(
            "  %s [%s]: %s → %s" % (c.param, c.group or "only", c.old, c.new)
            for c in live)
        extra = ("\n\n" + about) if about else ""
        if QtWidgets.QMessageBox.question(
                self, "Apply feature switch",
                "%s\n\n%d value%s will change:\n%s%s\n\nApply (one undo step)?"
                % (label, len(live), "" if len(live) == 1 else "s",
                   preview or "  (none)", extra),
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            with self.session.transaction(label):
                n = E.apply(self.session, live, label) if live else 0
                if apply_hook is not None:
                    # file-wide parts (fault paths) go with the first copy
                    for i, g in enumerate(sel):
                        n += apply_hook(self.session, g, target, label,
                                        first=i == 0, **self._opts()) or 0
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                self, "Feature switches", "Nothing was changed: %s" % ex)
            return
        self._after_apply(label)
        self._select()                      # refresh current state + preview
        try:
            self.ws.window().statusBar().showMessage(
                "%s — %d change%s. Undo puts it back."
                % (label, n, "" if n == 1 else "s"), 8000)
        except Exception:
            pass

    def refresh_from_session(self):
        """An edit made elsewhere (undo, the grid, another tool): show the
        states as they are now -- called by the tab while this is open."""
        self._select()

    def _after_apply(self, label: str):
        ws = self.ws
        for fn in ("model",):
            try:
                getattr(ws, fn).refresh()
            except Exception:
                pass
        for meth in ("_populate", "_status"):
            try:
                getattr(ws, meth)()
            except Exception:
                pass
        try:
            for t in ws.tables():
                if t.entity is not None:
                    ws._dress_table_tab(ws._table_index(t.entity))
        except Exception:
            pass
        try:
            ws._refresh_tools()             # an open DTC window shows the change
        except Exception:
            pass
        ref = getattr(ws, "project_ref", None)
        if ref is not None and self.version.isChecked():
            try:
                from . import gui_project as _gp
                _gp.save(ws, tab_only=True, label=label)
            except Exception as ex:
                QtWidgets.QMessageBox.warning(
                    self, "Feature switches",
                    "Applied, but the project version could not be saved: %s" % ex)


__all__ = ["FeatureSwitchDialog", "availability", "open_for"]
