# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Feature switches... window (:mod:`ovtsuite.featureswitch`).

Turn an engine ECU's functions on and off as parameter bundles. The bundles
come from the platform's module (its ``features`` hook); a function that is
more than a switch names a *table hook*, which this window asks what it will
change (``fs_preview`` / ``fs_describe``) and has write it (``fs_apply``).

The left list is the features this file carries. For the one selected the
right side shows what the file holds now (whether it fully, partly or does
not match a preset), a **Set to** choice -- *Keep as is* until the user
picks a preset -- the copies (datasets / coding variants) to write, and a
preview of every value, table and fault path that will change. A
parameter's new value can be typed or picked, beyond the presets.

Changes are held per feature until **Apply** or **OK** writes every feature
changed, as one undo step; **Cancel** closes without writing anything.
Checksums are corrected on save.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .featureswitch import model as M, engine as E

KEEP = None                                  # Set to: Keep as is
CUSTOM = "__custom__"                         # Set to: values edited by hand


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


_PROV = {M.CODE: "ECU code", M.A2L: "A2L text", M.STOCK: "Stock files",
         M.DERIVED: "This file", M.USER: "You"}
_COLS = ["Parameter or table", "Meaning", "Source", "Now", "New"]


class _Pending:
    """One feature's changes, held until Apply / OK."""

    def __init__(self, state, values, groups, opts):
        self.state = state                  # the preset picked (the hook runs for it)
        self.values: Dict[str, int] = dict(values)   # parameter -> new raw value
        self.groups: List[str] = list(groups)
        self.opts: Dict[str, float] = dict(opts)


def _num(v) -> str:
    return "%g" % round(float(v), 4)


def _grid_tip(row, limit: int = 24) -> str:
    """A row's table as a grid for its tooltip: every cell, a changed one
    as old → new in bold. "" for a row with no table."""
    import html
    ent = row.get("entity")
    if ent is None or row.get("kind") not in ("table", "axis", "value"):
        return ""
    cells = row.get("cells") or {}
    try:
        if row.get("axis"):
            vals = [list(ent.x_values() if row["axis"] == "x" else ent.y_values())]
            xs, ys = [], []
        else:
            vals = ent.values()
            xs = list(ent.x_values()) if getattr(ent, "x", None) is not None else []
            ys = list(ent.y_values()) if getattr(ent, "y", None) is not None else []
    except Exception:
        return ""
    nr, nc = len(vals), len(vals[0]) if vals else 0
    out = ["<b>%s</b>" % html.escape(row.get("name", ""))]
    if nr > limit or nc > limit:
        out.append("<br><i>%d x %d: showing %d x %d</i>"
                   % (nr, nc, min(nr, limit), min(nc, limit)))
    out.append("<table cellspacing='0' cellpadding='2' style='border-collapse:collapse'>")
    if xs and len(xs) == nc:
        out.append("<tr><td></td>" + "".join(
            "<td style='color:gray'><i>%s</i></td>" % _num(x) for x in xs[:limit]) + "</tr>")
    for r in range(min(nr, limit)):
        line = ["<tr>"]
        if ys and len(ys) == nr:
            line.append("<td style='color:gray'><i>%s</i></td>" % _num(ys[r]))
        elif xs and len(xs) == nc:
            line.append("<td></td>")
        for c in range(min(nc, limit)):
            ch = cells.get((r, c))
            if ch is not None and ch[0] != ch[1]:
                line.append("<td style='background:#cfe8ff'><b>%s → %s</b></td>"
                            % (_num(ch[0]), _num(ch[1])))
            else:
                line.append("<td>%s</td>" % _num(vals[r][c]))
        out.append("".join(line) + "</tr>")
    out.append("</table>")
    if cells:
        out.append("<i>%d cell%s change</i>" % (len(cells), "" if len(cells) == 1 else "s"))
    return "".join(out)


class _TableView(QtWidgets.QDialog):
    """A table a feature works through, viewed from the Feature switches
    window: its cells now, or as the setting picked leaves them (changed
    cells shaded, the other value in their tooltip). Read only -- the
    window's setting is what writes them."""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.row = row
        ent = row["entity"]
        self.setWindowTitle(row.get("name") or ent.name)
        v = QtWidgets.QVBoxLayout(self)
        desc = (getattr(ent, "description", "") or "").split("|")
        lab = QtWidgets.QLabel("<b>%s</b>%s" % (
            row.get("name") or ent.name,
            (" &mdash; %s" % desc[1].strip()) if len(desc) > 1 else ""))
        lab.setWordWrap(True)
        v.addWidget(lab)
        top = QtWidgets.QHBoxLayout()
        self.now = QtWidgets.QRadioButton("Now")
        self.after = QtWidgets.QRadioButton("After the change")
        cells = row.get("cells") or {}
        self.after.setEnabled(bool(cells))
        (self.after if cells else self.now).setChecked(True)
        for b in (self.now, self.after):
            b.toggled.connect(self._fill)
            top.addWidget(b)
        top.addStretch(1)
        n = sum(1 for o, nw in cells.values() if o != nw)
        info = QtWidgets.QLabel("%d cell%s change" % (n, "" if n == 1 else "s")
                                if cells else "This setting leaves it as it is")
        info.setStyleSheet("color: gray")
        top.addWidget(info)
        v.addLayout(top)
        self.grid = QtWidgets.QTableWidget()
        self.grid.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.grid, 1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        v.addWidget(bb)
        self._fill()
        self.resize(min(1100, 90 + 74 * max(1, self.grid.columnCount())),
                    min(700, 160 + 26 * max(1, self.grid.rowCount())))

    def _fill(self, *_a):
        row, ent = self.row, self.row["entity"]
        cells = row.get("cells") or {}
        after = self.after.isChecked()
        try:
            if row.get("axis"):
                vals = [list(ent.x_values() if row["axis"] == "x" else ent.y_values())]
                xs, ys = [], []
            else:
                vals = [list(r) for r in ent.values()]
                xs = list(ent.x_values()) if getattr(ent, "x", None) is not None else []
                ys = list(ent.y_values()) if getattr(ent, "y", None) is not None else []
        except Exception:
            vals, xs, ys = [], [], []
        nr, nc = len(vals), len(vals[0]) if vals else 0
        self.grid.clear()
        self.grid.setRowCount(nr)
        self.grid.setColumnCount(nc)
        self.grid.setHorizontalHeaderLabels(
            [_num(x) for x in xs] if len(xs) == nc else [str(i + 1) for i in range(nc)])
        self.grid.setVerticalHeaderLabels(
            [_num(y) for y in ys] if len(ys) == nr else [str(i + 1) for i in range(nr)])
        for r in range(nr):
            for c in range(nc):
                ch = cells.get((r, c))
                val = vals[r][c]
                if ch is not None:
                    val = ch[1] if after else ch[0]
                it = QtWidgets.QTableWidgetItem(_num(val))
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                                    | QtCore.Qt.AlignmentFlag.AlignVCenter)
                if ch is not None and ch[0] != ch[1]:
                    it.setBackground(QtGui.QColor("#cfe8ff"))
                    it.setForeground(QtGui.QColor(20, 20, 20))
                    it.setToolTip("now %s, after the change %s" % (_num(ch[0]), _num(ch[1])))
                self.grid.setItem(r, c, it)
        self.grid.resizeColumnsToContents()


class FeatureSwitchDialog(QtWidgets.QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.setWindowTitle("Feature switches")
        from .gui_fit import fit_to_screen
        fit_to_screen(self, 1440, 720)
        self.bundles = _present_bundles(self.session)
        self.pending: Dict[str, _Pending] = {}
        self._copy_boxes: List[QtWidgets.QCheckBox] = []
        self._loading = False
        self._param_rows = 0
        ro = _view_only(ws)

        root = QtWidgets.QVBoxLayout(self)
        body = QtWidgets.QHBoxLayout()
        root.addLayout(body, 1)

        # -- left: the features ----------------------------------------------
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
        self.list.setMinimumWidth(min(320, self.list.sizeHintForColumn(0) + 40))
        self.list.currentRowChanged.connect(self._select)
        left.addWidget(self.list, 1)
        hint = QtWidgets.QLabel("● marks a feature with changes not applied yet.")
        hint.setStyleSheet("color: gray")
        hint.setWordWrap(True)
        left.addWidget(hint)
        body.addLayout(left, 0)

        # -- right: the selected feature -------------------------------------
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
        self.cur_lbl.setWordWrap(True)
        row.addWidget(self.cur_lbl, 1)
        row.addWidget(QtWidgets.QLabel("Set to:"))
        self.state = QtWidgets.QComboBox()
        self.state.setMinimumContentsLength(18)
        self.state.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.state.currentIndexChanged.connect(self._state_picked)
        row.addWidget(self.state)
        # a table hook's own number (the EGR cut temperature ...)
        self.opt_lbl = QtWidgets.QLabel()
        self.opt = QtWidgets.QDoubleSpinBox()
        self.opt.setDecimals(0)
        self.opt.valueChanged.connect(self._opt_changed)
        self._opt_spec = None
        row.addWidget(self.opt_lbl)
        row.addWidget(self.opt)
        right.addLayout(row)

        self.copies_box = QtWidgets.QGroupBox("Copies to change")
        self.copies_lay = QtWidgets.QHBoxLayout(self.copies_box)
        right.addWidget(self.copies_box)

        self.preview = QtWidgets.QTableWidget(0, len(_COLS))
        self.preview.setHorizontalHeaderLabels(_COLS)
        hh = self.preview.horizontalHeader()
        # Meaning takes the room left; the others are sized to what they
        # hold, within a cap, after each fill (_size_columns)
        RM = QtWidgets.QHeaderView.ResizeMode
        for c in range(len(_COLS)):
            hh.setSectionResizeMode(c, RM.Stretch if c == 1 else RM.Interactive)
        hh.setMinimumSectionSize(60)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setWordWrap(False)
        self.preview.setToolTip("Hover a table to see its cells (changes as "
                                "old → new); double-click to open it in the main "
                                "window; right-click to view it now and after "
                                "the change")
        self.preview.cellDoubleClicked.connect(self._open_row)
        self.preview.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._preview_menu)
        right.addWidget(self.preview, 1)

        self.hook_info = QtWidgets.QLabel()
        self.hook_info.setWordWrap(True)
        self.hook_info.setStyleSheet("color:#06a")
        right.addWidget(self.hook_info)
        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color:#a60")
        right.addWidget(self.notes)
        body.addLayout(right, 1)

        # -- buttons ------------------------------------------------------------
        btns = QtWidgets.QHBoxLayout()
        self.ask_btn = QtWidgets.QPushButton("Ask AI about this feature...")
        self.ask_btn.setToolTip("Open the AI assistant with a question about the "
                                "selected feature, its settings in this file and "
                                "what changing it does")
        self.ask_btn.clicked.connect(self._ask_ai)
        btns.addWidget(self.ask_btn)
        self.reset_btn = QtWidgets.QPushButton("Undo this feature's changes")
        self.reset_btn.setToolTip("Forget the changes made to this feature "
                                  "(nothing has been written yet)")
        self.reset_btn.clicked.connect(self._reset_feature)
        btns.addWidget(self.reset_btn)
        self.version = QtWidgets.QCheckBox("Save as project version")
        self.version.setChecked(True)
        self.version.setVisible(getattr(ws, "project_ref", None) is not None)
        btns.addWidget(self.version)
        btns.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.apply_btn = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.apply_btn.setToolTip("Write every feature changed, as one undo step")
        self.apply_btn.clicked.connect(self._apply)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        self.ok_btn = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        btns.addWidget(bb)
        root.addLayout(btns)
        if ro:
            self.state.setEnabled(False)

        if self.bundles:
            self.list.setCurrentRow(0)
        self._sync_buttons()

    # -- the selected feature -----------------------------------------------
    def _bundle(self) -> Optional[M.Bundle]:
        it = self.list.currentItem()
        if it is None:
            return None
        bid = it.data(QtCore.Qt.ItemDataRole.UserRole)
        return next((b for b in self.bundles if b.id == bid), None)

    def _hook(self, b):
        return M.hook(self.session, b.table_hook) if b is not None else None

    def _groups(self, b) -> List[str]:
        return E.groups(self.session, b) or [""]

    def _sel_groups(self, b) -> List[str]:
        gs = self._groups(b)
        if len(gs) <= 1:
            return gs
        return [g for g, cb in zip(gs, self._copy_boxes) if cb.isChecked()]

    def _now_text(self, b, g) -> str:
        hook = self._hook(b)
        st = E.current_state(self.session, b, g)
        if st is not None:
            return "Now: <b>%s</b>" % b.label(st)
        words = getattr(hook, "state_text", None)
        if words is not None:
            t = words(self.session, g)
            if t:
                return "Now: <b>%s</b> (no preset matches)" % t
        if hook is None or not hasattr(hook, "state"):
            best, ok, n = E.match_info(self.session, b, g)
            if best is not None and ok:
                return ("Now: <b>partly %s</b> (%d of %d values match; the "
                        "others are set differently)" % (b.label(best), ok, n))
        return "Now: <b>no preset matches</b> (the values are set by hand)"

    def _select(self, *_a):
        b = self._bundle()
        if b is None:
            return
        self._loading = True
        self.title.setText(b.title)
        self.summary.setText(b.summary)
        self.notes.setText("\n".join(b.notes))
        p = self.pending.get(b.id)
        # copies
        for cb in self._copy_boxes:
            cb.setParent(None)
        self._copy_boxes = []
        gs = self._groups(b)
        for g in gs:
            st = E.current_state(self.session, b, g)
            cb = QtWidgets.QCheckBox("%s (now: %s)" % (
                g or "The only copy", b.label(st) if st else "set by hand"))
            cb.setChecked(p is None or g in p.groups)
            cb.stateChanged.connect(self._copies_changed)
            self.copies_lay.addWidget(cb)
            self._copy_boxes.append(cb)
        self.copies_box.setVisible(len(gs) > 1)
        self.cur_lbl.setText(self._now_text(b, gs[0]))
        # Set to: Keep as is, the presets, and Custom when edited
        self.state.blockSignals(True)
        self.state.clear()
        self.state.addItem("Keep as is (no change)", KEEP)
        if not b.readonly:
            for s in b.states:
                self.state.addItem(b.label(s), s)
        if p is not None and p.values != E.preset_values(self.session, b, p.state or ""):
            self.state.addItem("Custom values", CUSTOM)
            self.state.setCurrentIndex(self.state.count() - 1)
        elif p is not None:
            self.state.setCurrentIndex(max(0, self.state.findData(p.state)))
        self.state.setEnabled(not b.readonly and not _view_only(self.ws))
        self.state.blockSignals(False)
        # the hook's own number
        spec = getattr(self._hook(b), "FS_OPTION", None)
        self._opt_spec = spec
        if spec:
            self.opt.blockSignals(True)
            self.opt.setRange(spec.get("min", 0.0), spec.get("max", 1e6))
            self.opt.setSingleStep(spec.get("step", 1.0))
            self.opt.setSuffix(spec.get("suffix", ""))
            self.opt.setValue(p.opts.get(spec["key"], spec.get("default", 0.0))
                              if p is not None and spec.get("key") else
                              spec.get("default", 0.0))
            self.opt.setToolTip(spec.get("tip", ""))
            self.opt_lbl.setText(spec.get("label", ""))
            self.opt.blockSignals(False)
        self.opt_lbl.setVisible(bool(spec))
        self.opt.setVisible(bool(spec))
        self._loading = False
        self._sync_buttons()
        self._refresh_preview()

    def _opts(self) -> Dict[str, float]:
        spec = self._opt_spec
        return {spec["key"]: self.opt.value()} if spec and spec.get("key") else {}

    def _ref(self):
        """The comparison (in a project tab, the file as first read): where a
        fault path that was off when this file was opened gets its class."""
        return getattr(self.ws, "comparison", None)

    # -- edits -------------------------------------------------------------
    def _state_picked(self, *_a):
        if self._loading:
            return
        b = self._bundle()
        if b is None:
            return
        st = self.state.currentData()
        if st is KEEP:
            self.pending.pop(b.id, None)
        elif st != CUSTOM:
            self.pending[b.id] = _Pending(st, E.preset_values(self.session, b, st),
                                          self._sel_groups(b), self._opts())
            i = self.state.findData(CUSTOM)
            if i >= 0:
                self.state.blockSignals(True)
                self.state.removeItem(i)
                self.state.blockSignals(False)
        self._changed(b)

    def _value_edited(self, b, p, value):
        if self._loading:
            return
        pend = self.pending.get(b.id)
        if pend is None:
            pend = self.pending[b.id] = _Pending(None, {}, self._sel_groups(b),
                                                 self._opts())
        if value is None:
            pend.values.pop(p.name, None)
        else:
            pend.values[p.name] = int(value)
        if not pend.values and pend.state is None:
            self.pending.pop(b.id, None)
        self.state.blockSignals(True)
        if b.id not in self.pending:
            self.state.setCurrentIndex(0)
        elif pend.values != E.preset_values(self.session, b, pend.state or ""):
            if self.state.findData(CUSTOM) < 0:
                self.state.addItem("Custom values", CUSTOM)
            self.state.setCurrentIndex(self.state.findData(CUSTOM))
        else:
            self.state.setCurrentIndex(max(0, self.state.findData(pend.state)))
        self.state.blockSignals(False)
        self._changed(b, rebuild=False)

    def _copies_changed(self, *_a):
        b = self._bundle()
        if b is not None and b.id in self.pending:
            self.pending[b.id].groups = self._sel_groups(b)
        self._refresh_preview()

    def _opt_changed(self, *_a):
        b = self._bundle()
        if b is not None and b.id in self.pending:
            self.pending[b.id].opts = self._opts()
        self._refresh_hook_rows()

    def _reset_feature(self):
        b = self._bundle()
        if b is not None:
            self.pending.pop(b.id, None)
            self._mark(b)
            self._select()

    def _changed(self, b, rebuild=True):
        self._mark(b)
        self._sync_buttons()
        if rebuild:
            self._refresh_preview()
        else:
            self._refresh_hook_rows()

    def _mark(self, b):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(QtCore.Qt.ItemDataRole.UserRole) == b.id:
                on = b.id in self.pending
                it.setText(("● " if on else "") + b.title)
                f = it.font()
                f.setBold(on)
                it.setFont(f)

    def _sync_buttons(self):
        n = len(self.pending)
        ro = _view_only(self.ws)
        self.apply_btn.setEnabled(bool(n) and not ro)
        self.apply_btn.setText("Apply (%d features)" % n if n > 1 else "Apply")
        b = self._bundle()
        self.reset_btn.setEnabled(b is not None and b.id in self.pending)

    # -- the preview table -------------------------------------------------
    def _refresh_preview(self, *_a):
        b = self._bundle()
        self.preview.setRowCount(0)
        if b is None:
            return
        self._loading = True
        sel = self._sel_groups(b) or self._groups(b)
        pend = self.pending.get(b.id)
        vals = E.read_values(self.session, b, sel[0])
        others = [E.read_values(self.session, b, g) for g in sel[1:]]
        for p in E.present_params(self.session, b):
            r = self.preview.rowCount()
            self.preview.insertRow(r)
            cur = vals.get(p.name)
            differs = any(o.get(p.name) != cur for o in others)
            now = p.value_text(cur) + (" (differs per copy)" if differs else "")
            for c, text in enumerate([p.name, p.meaning, _PROV.get(p.prov, p.prov), now]):
                self.preview.setItem(r, c, QtWidgets.QTableWidgetItem(text))
            new = pend.values.get(p.name) if pend is not None else None
            self.preview.setCellWidget(r, 4, self._value_widget(b, p, cur, new))
            if new is not None and new != cur:
                f = self.preview.item(r, 0).font()
                f.setBold(True)
                self.preview.item(r, 0).setFont(f)
        # a switch that follows the EEPROM coding (its Type byte 1) is set to
        # read the flash value written here: that byte changes too
        if pend is not None:
            names = {p.name for p in b.params}
            changes, _w = E.plan(self.session, b, pend.state, sel[:1], pend.values)
            for c in changes:
                if c.param in names or not c.changed:
                    continue
                r = self.preview.rowCount()
                self.preview.insertRow(r)
                for col, text in enumerate([
                        c.param, "Where the ECU reads the switch above: from the "
                        "EEPROM coding now; from this file's value after the change",
                        "ECU code", "%d (EEPROM coding)" % c.old,
                        "%d (this file's value)" % c.new]):
                    self.preview.setItem(r, col, QtWidgets.QTableWidgetItem(text))
        self._param_rows = self.preview.rowCount()
        self._loading = False
        self._refresh_hook_rows()

    def _value_widget(self, b, p, cur, new):
        ro = p.readonly or b.readonly or _view_only(self.ws)
        if p.choices or ro:
            w = QtWidgets.QComboBox()
            # the first entry keeps the file's value: shown as that value
            w.addItem(p.value_text(cur) or "(not read)", None)
            if not ro:
                for v, lab in sorted(p.choices.items()):
                    w.addItem("%d (%s)" % (v, lab), v)
            if new is not None:
                i = w.findData(new)
                if i < 0:
                    w.addItem(str(new), new)
                    i = w.count() - 1
                w.setCurrentIndex(i)
            w.setEnabled(not ro)
            w.currentIndexChanged.connect(
                lambda _i, w=w: self._value_edited(b, p, w.currentData()))
        else:
            lo, hi = E.value_range(p)
            hi = min(hi, 0x7FFFFFFF)            # a spin box holds 31 bits
            w = QtWidgets.QSpinBox()
            w.setRange(lo - 1, hi)
            w.setSpecialValueText(p.value_text(cur) or "(not read)")
            w.setValue(new if new is not None else lo - 1)
            w.setToolTip("Type a value from %d to %d, or step below %d to keep "
                         "the file's value" % (lo, hi, lo))
            w.valueChanged.connect(
                lambda v, lo=lo: self._value_edited(b, p, None if v < lo else v))
        if new is not None and new != cur:
            w.setStyleSheet("font-weight: bold")
        return w

    def _sync_opt(self, b):
        """The hook's number is live only for the settings it applies to."""
        spec = self._opt_spec
        if not spec:
            return
        pend = self.pending.get(b.id)
        only = spec.get("states")
        on = not only or (pend is not None and pend.state in only)
        self.opt.setEnabled(on and not _view_only(self.ws))
        self.opt_lbl.setEnabled(on)

    #: widest each column but Meaning grows to its contents (pixels)
    _COL_CAPS = {0: 240, 2: 110, 3: 180, 4: 230}

    def _size_columns(self):
        for c, cap in self._COL_CAPS.items():
            self.preview.resizeColumnToContents(c)
            w = self.preview.columnWidth(c)
            if c == 4:
                w = max(w, 170)          # the value pickers are cell widgets
            self.preview.setColumnWidth(c, min(cap, w))

    def _add_hook_rows(self, rows, color):
        for row in rows:
            r = self.preview.rowCount()
            self.preview.insertRow(r)
            src = {"fault": "Fault path", "axis": "Table axis",
                   "table": "Table", "value": "Value"}.get(row.get("kind", ""), "")
            tip = _grid_tip(row)
            for c, text in enumerate([row.get("name", ""), row.get("meaning", ""),
                                      src, row.get("current", ""), row.get("new", "")]):
                it = QtWidgets.QTableWidgetItem(text)
                it.setForeground(QtGui.QColor(color))
                it.setToolTip(tip or text)
                if c == 0:
                    it.setData(QtCore.Qt.ItemDataRole.UserRole, row.get("entity"))
                    it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, row)
                self.preview.setItem(r, c, it)

    def _row_entity(self, r):
        """The table or parameter a preview row names: the hook's entity, or
        the parameter of that name in the first ticked copy."""
        it = self.preview.item(r, 0)
        if it is None:
            return None
        ent = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if ent is not None:
            return ent
        name = it.text().split(" (", 1)[0]
        b = self._bundle()
        groups = (self._sel_groups(b) or self._groups(b)) if b is not None else [""]
        named = [e for e in self.session.entities if e.name == name]
        return next((e for e in named if (getattr(e, "group", "") or "") == groups[0]),
                    named[0] if named else None)

    def _open_row(self, r, _c=0):
        """Double-click: the row's table or parameter in the main window
        (this window stays open)."""
        ent = self._row_entity(r)
        show = getattr(self.ws, "show_entity", None)
        if ent is not None and show is not None:
            show(ent)

    def _view_row(self, r):
        """The row's table, now and after the change, in this tool's own
        viewer (the main window is left as it is)."""
        it = self.preview.item(r, 0)
        row = it.data(QtCore.Qt.ItemDataRole.UserRole + 1) if it is not None else None
        if not row or row.get("entity") is None:
            return
        dlg = _TableView(row, self)
        dlg.show()
        self._views = [v for v in getattr(self, "_views", []) if v.isVisible()] + [dlg]

    def _preview_menu(self, pos):
        r = self.preview.rowAt(pos.y())
        if r < 0:
            return
        menu = QtWidgets.QMenu(self)
        ent = self._row_entity(r)
        a = menu.addAction("Open in the main window", lambda: self._open_row(r))
        a.setEnabled(ent is not None and getattr(self.ws, "show_entity", None) is not None)
        it = self.preview.item(r, 0)
        row = it.data(QtCore.Qt.ItemDataRole.UserRole + 1) if it is not None else None
        v = menu.addAction("View now and after the change...", lambda: self._view_row(r))
        v.setEnabled(bool(row) and row.get("entity") is not None)
        menu.exec(self.preview.viewport().mapToGlobal(pos))

    def _refresh_hook_rows(self):
        """The tables and fault paths the feature's hook will change, under
        the parameters; and its one-line note."""
        b = self._bundle()
        if b is None:
            return
        self._sync_opt(b)
        self.preview.setRowCount(self._param_rows)
        pend = self.pending.get(b.id)
        hook = self._hook(b)
        sel = self._sel_groups(b) or self._groups(b)
        self.hook_info.clear()
        if hook is not None and pend is not None and pend.state is not None:
            try:
                rows = list(getattr(hook, "fs_preview", lambda *a, **k: [])(
                    self.session, sel[0], pend.state, first=True, ref=self._ref(),
                    **self._opts()) or [])
                info = getattr(hook, "fs_describe", lambda *a, **k: "")(
                    self.session, sel[0], pend.state, ref=self._ref(),
                    **self._opts()) or ""
            except Exception as ex:                       # noqa: BLE001
                rows, info = [], "(Could not work out the table changes: %s)" % ex
            if len(sel) > 1:
                info += ("  The tables are shown for %s; each ticked copy gets "
                         "the same change." % sel[0])
            if not rows:
                info = ("No table or fault path needs to change. " + info).strip()
            self.hook_info.setText(info.strip())
            # every table and fault path the feature works through, all of
            # the time: those this setting changes in blue, with their new
            # values; the rest grey, as they are
            inv = []
            if hasattr(hook, "fs_involved"):
                try:
                    inv = list(hook.fs_involved(self.session, sel[0], ref=self._ref(),
                                                **self._opts()) or [])
                except Exception:                         # noqa: BLE001
                    inv = []
            changed = {r.get("name"): r for r in rows}
            for r in inv:
                hit = changed.pop(r.get("name"), None)
                if hit is not None:
                    self._add_hook_rows([hit], "#06a")
                else:
                    self._add_hook_rows([dict(r, new="(unchanged)")], "#555")
            self._add_hook_rows(list(changed.values()), "#06a")
        elif hook is not None:
            rows = []
            inv = getattr(hook, "fs_involved", None)
            if inv is not None:
                try:
                    rows = list(inv(self.session, sel[0], ref=self._ref(),
                                    **self._opts()) or [])
                except Exception:                         # noqa: BLE001
                    rows = []
            self._add_hook_rows(rows, "#555")
            if not b.readonly:
                self.hook_info.setText(
                    "The tables and fault paths this feature works through. "
                    "Pick a setting to see what it changes.")
        self._size_columns()

    # -- writing -----------------------------------------------------------
    def _collect(self):
        """``[(bundle, changes, warnings, hook, pending)]`` for every feature
        with changes."""
        out = []
        for b in self.bundles:
            pend = self.pending.get(b.id)
            if pend is None:
                continue
            groups = pend.groups or self._groups(b)
            changes, warns = E.plan(self.session, b, pend.state, groups, pend.values)
            out.append((b, [c for c in changes if c.changed], warns, self._hook(b), pend))
        return out

    def _apply(self) -> bool:
        """Write every feature changed as one undo step (after a summary).
        True when written (or nothing was pending)."""
        if not self.pending:
            return True
        work = self._collect()
        lines, warns = [], []
        for b, live, w, hook, pend in work:
            warns += ["%s: %s" % (b.title, x) for x in w]
            bits = []
            if live:
                bits.append("%d value%s" % (len(live), "" if len(live) == 1 else "s"))
            if hook is not None and pend.state is not None and hasattr(hook, "fs_preview"):
                try:
                    rows = hook.fs_preview(self.session, (pend.groups or [""])[0],
                                           pend.state, first=True, ref=self._ref(),
                                           **pend.opts) or []
                except Exception:                         # noqa: BLE001
                    rows = []
                nt = sum(1 for r in rows if r.get("kind") in ("table", "axis"))
                nf = sum(1 for r in rows if r.get("kind") == "fault")
                if nt:
                    bits.append("%d table%s" % (nt, "" if nt == 1 else "s"))
                if nf:
                    bits.append("%d fault path%s" % (nf, "" if nf == 1 else "s"))
            target = b.label(pend.state) if pend.state else "custom values"
            if pend.state and pend.values != E.preset_values(self.session, b, pend.state):
                target += " (edited)"
            copies = pend.groups if len(self._groups(b)) > 1 else []
            lines.append("• %s → %s: %s%s" % (
                b.title, target, ", ".join(bits) or "nothing to change",
                (" [%s]" % ", ".join(copies)) if copies else ""))
        text = "Write these changes as one undo step?\n\n" + "\n".join(lines)
        if warns:
            text += "\n\nNot written:\n" + "\n".join("  " + w for w in dict.fromkeys(warns))
        if QtWidgets.QMessageBox.question(
                self, "Apply feature switches", text,
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
        label = "Feature switches: " + ", ".join(b.title for b, *_r in work)
        n = 0
        try:
            with self.session.transaction(label):
                for b, live, _w, hook, pend in work:
                    n += E.apply(self.session, live, b.title) if live else 0
                    if hook is not None and pend.state is not None and \
                            hasattr(hook, "fs_apply"):
                        for i, g in enumerate(pend.groups or [""]):
                            n += hook.fs_apply(self.session, g, pend.state, b.title,
                                               first=i == 0, ref=self._ref(),
                                               **pend.opts) or 0
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                self, "Feature switches", "Nothing was changed: %s" % ex)
            return False
        self.pending.clear()
        for b in self.bundles:
            self._mark(b)
        self._after_apply(label)
        self._select()
        try:
            self.ws.window().statusBar().showMessage(
                "%s: %d change%s. Edit > Undo takes them all back."
                % (label, n, "" if n == 1 else "s"), 8000)
        except Exception:
            pass
        return True

    def _ok(self):
        if self._apply():
            self.accept()

    def reject(self):
        """Cancel: close without writing anything held."""
        self.pending.clear()
        for b in self.bundles:
            self._mark(b)
        super().reject()

    def refresh_from_session(self):
        """An edit made elsewhere (undo, the grid, another tool): show the
        values as they are now -- called by the tab while this is open."""
        self._select()

    # -- AI ------------------------------------------------------------------
    def _ask_ai(self):
        b = self._bundle()
        if b is None:
            return
        try:
            from . import gui_ai
            lw = gui_ai.assistant_dock(self.ws.win)._workspace()
            h = lw.tab_for(self.ws)
        except Exception as ex:                           # noqa: BLE001
            QtWidgets.QMessageBox.information(
                self, "Feature switches", "The AI assistant is not available: %s" % ex)
            return
        g = (self._sel_groups(b) or self._groups(b))[0]
        st = E.current_state(self.session, b, g)
        q = ("Look at the feature switch \"%s\" (feature id %s) of tab %s with the "
             "feature_switches tool. Explain what each of its settings does on this "
             "ECU, what the file is set to now (%s), and what changing it would do "
             "and risk. If I ask for a change, work it out with "
             "feature_switch_changes." % (b.title, b.id, h,
                                           b.label(st) if st else "no preset matches"))
        gui_ai.show_assistant(self.ws.win, q)

    def _after_apply(self, label: str):
        ws = self.ws
        try:
            ws.model.refresh()
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
