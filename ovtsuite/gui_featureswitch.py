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

One selector at the top (**Apply to coding / dataset**) picks the *copy* to
view and change -- an EDC15 dataset, the main or coding-variant copy on
EDC16 --, *All* (each copy's value shown, an edit going to every copy), or
with three or more copies some of them (*Choose ...*, a list to tick).
Every row Apply changes is blue (bold when changed by hand), a table or
fault path left as it is grey. An edit repaints its own row; the hooks'
answers and the fault-path table are kept per revision of the file, so
picking and switching features does not re-read the whole image. The left list is the
features this file carries. For the one selected the right side shows what
the file holds now (whether it fully, partly or does not match a preset), a
**Set to** choice -- *Keep as is* until the user picks a preset -- and a
preview of every value, table and fault path that will change. A
parameter's new value can be typed or picked, beyond the presets, and each
fault path switched on or off on its own.

Changes are held per feature until **Apply** or **OK** writes every feature
changed, as one undo step; **Cancel** closes without writing anything.
Checksums are corrected on save.
"""
from __future__ import annotations

import html
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .featureswitch import model as M, engine as E, ents as _E

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


def _absent_bundles(s) -> List[tuple]:
    """``[(bundle, why)]``: the features of the file's platform (and family)
    that its table hook will not offer here, with the hook's reason."""
    fam = getattr(s, "family", None)
    out = []
    for b in M.bundles_for(s):
        if b.families and fam not in b.families:
            continue
        why = E.absent_why(s, b)
        if why:
            out.append((b, why))
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


ALL = "__all__"                               # the copy selector: every copy
CHOOSE = "__choose__"                         # the copy selector: pick some
SOME = "__some__"                             # the copy selector: those picked
DIFFERS = "__differs__"                       # Set to: the copies differ


class _Copy:
    """One copy's changes to one feature: the preset picked (the table hook
    runs for it) and the parameter values to write."""

    def __init__(self, state=None, values=None):
        self.state: Optional[str] = state
        self.values: Dict[str, int] = dict(values or {})

    def live(self) -> bool:
        return self.state is not None or bool(self.values)


class _Pending:
    """One feature's changes, held until Apply / OK: per copy (dataset /
    coding-variant copy), plus the fault paths chosen one by one (file-wide)
    and the hook's own number."""

    def __init__(self, opts):
        self.copies: Dict[str, _Copy] = {}
        self.faults: Dict[str, Optional[int]] = {}    # path -> class (None: off)
        self.opts: Dict[str, float] = dict(opts)

    def prune(self):
        self.copies = {k: c for k, c in self.copies.items() if c.live()}

    def empty(self) -> bool:
        return not self.faults and not any(c.live() for c in self.copies.values())


def _num(v) -> str:
    return "%g" % round(float(v), 4)


def _dark() -> bool:
    app = QtWidgets.QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128


def _colors() -> Dict[str, str]:
    """The window's accent colours, readable on the theme in use: a change
    (blue), an unchanged table (grey), a note (amber), and a changed cell's
    background with its text."""
    if _dark():
        return {"changed": "#6cb6ff", "same": "#a0a0a0", "note": "#e0a040",
                "cell_bg": "#1f4f80", "cell_fg": "#ffffff", "axis": "#a0a0a0"}
    return {"changed": "#0060a0", "same": "#555555", "note": "#a06000",
            "cell_bg": "#cfe8ff", "cell_fg": "#000000", "axis": "#707070"}


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
    col = _colors()
    out = ["<b>%s</b>" % html.escape(row.get("name", ""))]
    if nr > limit or nc > limit:
        out.append("<br><i>%d x %d: showing %d x %d</i>"
                   % (nr, nc, min(nr, limit), min(nc, limit)))
    if not cells and row.get("new") and row.get("new") != "(unchanged)":
        out.append("<br><i>After the change: %s</i>" % html.escape(row["new"]))
    out.append("<table cellspacing='0' cellpadding='2' style='border-collapse:collapse'>")
    ax = "<td style='color:%s'><i>%%s</i></td>" % col["axis"]
    if xs and len(xs) == nc:
        out.append("<tr><td></td>" + "".join(ax % _num(x) for x in xs[:limit]) + "</tr>")
    for r in range(min(nr, limit)):
        line = ["<tr>"]
        if ys and len(ys) == nr:
            line.append(ax % _num(ys[r]))
        elif xs and len(xs) == nc:
            line.append("<td></td>")
        for c in range(min(nc, limit)):
            ch = cells.get((r, c))
            if ch is not None and ch[0] != ch[1]:
                # the highlight carries its own text colour: the tooltip's
                # (light in a dark theme) is unreadable on it
                line.append("<td style='background:%s;color:%s'><b>%s → %s</b></td>"
                            % (col["cell_bg"], col["cell_fg"], _num(ch[0]), _num(ch[1])))
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
        new = row.get("new") or ""
        info = QtWidgets.QLabel(
            "%d cell%s change" % (n, "" if n == 1 else "s") if cells
            else "After the change: %s" % new if new and new != "(unchanged)"
            else "This setting leaves it as it is")
        info.setStyleSheet("color: %s" % _colors()["same"])
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
        col = _colors()
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
                    it.setBackground(QtGui.QColor(col["cell_bg"]))
                    it.setForeground(QtGui.QColor(col["cell_fg"]))
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
        self._loading = False
        self._param_rows = 0
        ro = _view_only(ws)
        # the copies (EDC15 datasets, EDC16 main / coding-variant copies):
        # one selector for the whole window
        self.copies: List[str] = E.copies(self.session, self.bundles)
        #: ALL, one copy key, or a tuple of keys (some copies, not all)
        self.view = ALL if len(self.copies) > 1 else self.copies[0]
        self._hook_cache: Dict[tuple, object] = {}
        self._hook_rev = None
        self._companions: Dict[str, tuple] = {}
        self._some: List[str] = []

        root = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        codings = _E.has_variants(self.session)
        self._noun = ("coding", "codings") if codings else ("dataset", "datasets")
        self.copy_lbl = QtWidgets.QLabel("Apply to %s:" % self._noun[0])
        top.addWidget(self.copy_lbl)
        self.copy_sel = QtWidgets.QComboBox()
        self.copy_sel.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        if len(self.copies) > 1:
            self.copy_sel.addItem("All %s (%d)" % (self._noun[1], len(self.copies)), ALL)
        for c in self.copies:
            self.copy_sel.addItem(self._long(c), c)
        if len(self.copies) > 2:
            self.copy_sel.addItem("Choose %s..." % self._noun[1], CHOOSE)
        why = ("Codings are the car variants (gearbox, A/C ...) the ECU is "
               "coded for: some values have a separate copy that only some "
               "codings read." if codings else
               "Datasets are the ECU's separate calibrations (Manual, "
               "Automatic ...): each holds its own copy of the values.")
        self.copy_sel.setToolTip(
            "<p>%s</p><p>Pick which %s the whole window shows and changes. "
            "<b>All %s</b> shows every copy's value and changes them all; one "
            "changes only that one%s.</p>"
            % (why, self._noun[1], self._noun[1],
               "; <b>Choose %s...</b> picks several" % self._noun[1]
               if len(self.copies) > 2 else ""))
        self.copy_lbl.setToolTip(self.copy_sel.toolTip())
        self._view_index = 0
        self.copy_sel.currentIndexChanged.connect(self._view_changed)
        top.addWidget(self.copy_sel)
        top.addStretch(1)
        for w in (self.copy_lbl, self.copy_sel):
            w.setVisible(len(self.copies) > 1)
        root.addLayout(top)
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
        absent = _absent_bundles(self.session)
        self.absent = QtWidgets.QLabel("<b>Not available on this file</b><br>" + "<br>".join(
            "%s: %s." % (html.escape(b.title), html.escape(why)) for b, why in absent))
        self.absent.setWordWrap(True)
        self.absent.setMaximumWidth(max(320, self.list.minimumWidth()))
        self.absent.setStyleSheet("color: gray")
        self.absent.setVisible(bool(absent))
        left.addWidget(self.absent)
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
        # and its tick box (Cut when warm: grow the map, keep the OEM
        # breakpoints) -- disabled, with why, when the file cannot
        self.chk = QtWidgets.QCheckBox()
        self.chk.toggled.connect(self._opt_changed)
        self._chk_spec = None
        row.addWidget(self.chk)
        # and its choice (the dataset a table is copied from ...): shown
        # when the hook lists two or more
        self.choice_lbl = QtWidgets.QLabel()
        self.choice_sel = QtWidgets.QComboBox()
        self.choice_sel.currentIndexChanged.connect(self._opt_changed)
        self._choice_spec = None
        row.addWidget(self.choice_lbl)
        row.addWidget(self.choice_sel)
        right.addLayout(row)

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

        col = _colors()
        self.legend = QtWidgets.QLabel(
            "<span style='color:%s'><b>Blue</b></span>: changes on Apply"
            " &nbsp;·&nbsp; <span style='color:%s'>grey</span>: a table or "
            "fault path the feature uses, staying as it is &nbsp;·&nbsp; "
            "<b>bold</b>: changed by you" % (col["changed"], col["same"]))
        self.legend.setWordWrap(True)
        right.addWidget(self.legend)
        self.hook_info = QtWidgets.QLabel()
        self.hook_info.setWordWrap(True)
        self.hook_info.setStyleSheet("color:%s" % col["changed"])
        right.addWidget(self.hook_info)
        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color:%s" % col["note"])
        right.addWidget(self.notes)
        # the descriptions (part numbers, values) can be selected and copied
        sel = (QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
               | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard)
        for lab in (self.title, self.summary, self.cur_lbl, self.hook_info,
                    self.notes):
            lab.setTextInteractionFlags(sel)
            lab.setCursor(QtCore.Qt.CursorShape.IBeamCursor)
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

    def _bcopies(self, b) -> List[str]:
        """The feature's own copies standing for the window's copies, in
        order (a feature without coding-variant copies has one for both)."""
        out: List[str] = []
        for c in self.copies:
            g = E.copy_for(self.session, b, c)
            if g not in out:
                out.append(g)
        return out or [""]

    def _view_keys(self) -> List[str]:
        """The window's copies in view, in order."""
        if self.view == ALL:
            return list(self.copies)
        if isinstance(self.view, tuple):
            return [c for c in self.copies if c in self.view]
        return [self.view]

    def _view_copies(self, b) -> List[str]:
        """The copies of ``b`` the copy selector shows and edits."""
        if self.view == ALL:
            return self._bcopies(b)
        out: List[str] = []
        for c in self._view_keys():
            g = E.copy_for(self.session, b, c)
            if g not in out:
                out.append(g)
        return out or self._bcopies(b)[:1]

    def _short(self, c) -> str:
        return E.copy_label(self.session, c, short=True)

    def _long(self, c) -> str:
        """A copy as the selector lists it."""
        t = E.copy_label(self.session, c)
        if not _E.has_variants(self.session):
            return t or "The only %s" % self._noun[0]
        return t

    def _view_changed(self, *_a):
        data = self.copy_sel.currentData()
        if data == CHOOSE:
            keys = self._ask_copies(self._view_keys())
            if keys is None:                       # cancelled: as it was
                self.copy_sel.blockSignals(True)
                self.copy_sel.setCurrentIndex(self._view_index)
                self.copy_sel.blockSignals(False)
                return
            self.set_view(keys)
            return
        self.view = tuple(self._some) if data == SOME else data
        self._view_index = self.copy_sel.currentIndex()
        self._select()

    def set_view(self, keys):
        """Show and change the copies ``keys`` (window copy keys): every
        copy is All, one is that copy, several an entry of their own."""
        keys = [c for c in self.copies if c in set(keys)]
        if not keys:
            return
        # signals held throughout: the entry for several copies goes in
        # ahead of Choose..., the current one, which moves the current index
        # -- unheld, that ran _view_changed on Choose... again and asked twice
        self.copy_sel.blockSignals(True)
        try:
            i = self._view_entry(keys)
            self.copy_sel.setCurrentIndex(i)
        finally:
            self.copy_sel.blockSignals(False)
        self._view_index = i
        self.view = (ALL if len(keys) == len(self.copies) else
                     keys[0] if len(keys) == 1 else tuple(keys))
        self._select()

    def _view_entry(self, keys) -> int:
        """The selector's index for ``keys``, adding (or renaming) the entry
        for several copies."""
        if len(keys) == len(self.copies):
            return self.copy_sel.findData(ALL)
        if len(keys) == 1:
            return self.copy_sel.findData(keys[0])
        self._some = keys
        text = "%s: %s" % (self._noun[1].capitalize(),
                           ", ".join(self._short(c) for c in keys))
        i = self.copy_sel.findData(SOME)
        if i < 0:
            i = self.copy_sel.findData(CHOOSE)
            self.copy_sel.insertItem(i, text, SOME)
        else:
            self.copy_sel.setItemText(i, text)
        self.copy_sel.setItemData(
            i, "\n".join(self._long(c) for c in keys),
            QtCore.Qt.ItemDataRole.ToolTipRole)
        return i

    def _ask_copies(self, current) -> Optional[List[str]]:
        """The copies ticked in a list of them (None: cancelled)."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Choose %s" % self._noun[1])
        v = QtWidgets.QVBoxLayout(dlg)
        lab = QtWidgets.QLabel("Tick the %s to show and change. Edits go to "
                               "the ticked ones only." % self._noun[1])
        lab.setWordWrap(True)
        v.addWidget(lab)
        lst = QtWidgets.QListWidget()
        for c in self.copies:
            it = QtWidgets.QListWidgetItem(self._long(c))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, c)
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Checked if c in current
                             else QtCore.Qt.CheckState.Unchecked)
            lst.addItem(it)
        v.addWidget(lst)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)

        def ticked():
            return [lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                    for i in range(lst.count())
                    if lst.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        lst.itemChanged.connect(lambda *_a: ok.setEnabled(bool(ticked())))
        ok.setEnabled(bool(ticked()))
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return ticked() or None

    def _now_label(self, b) -> str:
        cps = self._view_copies(b)
        states = [E.current_state(self.session, b, c) for c in cps]
        if len(cps) == 1 or len(set(states)) == 1:
            t = self._now_text(b, cps[0])
            if len(cps) > 1:
                t += " in every copy" if self.view == ALL else " in each %s picked" % self._noun[0]
            elif self.view != ALL and self._view_keys() != cps:
                t += (" <span style='color:%s'>(this feature has one copy, read "
                      "by every coding)</span>" % _colors()["same"])
            return t
        return "Now: <b>differs per copy</b> &mdash; " + "; ".join(
            "%s: <b>%s</b>" % (self._short(c), b.label(st) if st else "no preset matches")
            for c, st in zip(cps, states))

    def _now_text(self, b, g) -> str:
        hook = self._hook(b)
        st = E.current_state(self.session, b, g)
        if st is not None:
            same = [st]
            if hook is None or not hasattr(hook, "state"):
                same += [x for x in E.full_matches(self.session, b, g) if x != st]
            return "Now: <b>%s</b>" % " or ".join(b.label(x) for x in same)
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
        self.cur_lbl.setText(self._now_label(b))
        # Set to: Keep as is, the presets, and Custom when edited
        self.state.blockSignals(True)
        self.state.clear()
        self.state.addItem("Keep as is (no change)", KEEP)
        if not b.readonly:
            for s in E.states(self.session, b):
                self.state.addItem(b.label(s), s)
        self.state.blockSignals(False)
        self._show_choice(b)
        self.state.setEnabled(not b.readonly and not _view_only(self.ws))
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
        chk = getattr(self._hook(b), "FS_CHECK", None)
        self._chk_spec = chk
        if chk:
            self.chk.blockSignals(True)
            self.chk.setText(chk.get("label", ""))
            self.chk.setChecked(bool(
                p.opts.get(chk["key"], chk.get("default", False))
                if p is not None else chk.get("default", False)))
            self.chk.blockSignals(False)
        self.chk.setVisible(bool(chk))
        self._load_choice(b, p)
        self._loading = False
        self._sync_buttons()
        self._refresh_preview()

    def _opts(self) -> Dict[str, float]:
        spec, chk = self._opt_spec, self._chk_spec
        out = {spec["key"]: self.opt.value()} if spec and spec.get("key") else {}
        if chk and chk.get("key"):
            # what was asked for: the hook falls back where it cannot
            out[chk["key"]] = self.chk.isChecked()
        cs = self._choice_spec
        if cs and cs.get("key") and self.choice_sel.currentData() is not None:
            out[cs["key"]] = self.choice_sel.currentData()
        return out

    def _load_choice(self, b, p):
        """The hook's choice (``FS_CHOICE``, its items from
        ``fs_choices``): shown only when there are two or more."""
        hook = self._hook(b)
        cs = getattr(hook, "FS_CHOICE", None)
        items = []
        if cs and hasattr(hook, "fs_choices"):
            try:
                items = list(hook.fs_choices(self.session) or [])
            except Exception:                             # noqa: BLE001
                items = []
        if len(items) < 2:
            cs = None
        self._choice_spec = cs
        self.choice_sel.blockSignals(True)
        self.choice_sel.clear()
        if cs:
            for key, text in items:
                self.choice_sel.addItem(text, key)
            want = p.opts.get(cs.get("key")) if p is not None else None
            self.choice_sel.setCurrentIndex(max(0, self.choice_sel.findData(want)))
            self.choice_sel.setToolTip(cs.get("tip", ""))
            self.choice_lbl.setText(cs.get("label", ""))
        self.choice_sel.blockSignals(False)
        self.choice_lbl.setVisible(bool(cs))
        self.choice_sel.setVisible(bool(cs))

    def _sync_choice(self, b):
        """The hook's choice is live only for the settings it applies to."""
        cs = self._choice_spec
        if not cs:
            return
        pend = self.pending.get(b.id)
        only = cs.get("states")
        on = not only or (pend is not None and any(
            pend.copies[c].state in only for c in self._view_copies(b)
            if c in pend.copies))
        self.choice_sel.setEnabled(on and not _view_only(self.ws))
        self.choice_lbl.setEnabled(on)

    def _ref(self):
        """The comparison (in a project tab, the file as first read): where a
        fault path that was off when this file was opened gets its class."""
        return getattr(self.ws, "comparison", None)

    # -- edits -------------------------------------------------------------
    def _choice(self, b):
        """What **Set to** shows for the copies in view: KEEP, a preset,
        CUSTOM (values edited), or DIFFERS (the copies hold different
        changes)."""
        pend = self.pending.get(b.id)
        cps = [pend.copies.get(c) if pend is not None else None
               for c in self._view_copies(b)]

        def one(cp):
            if cp is None or not cp.live():
                return KEEP
            if cp.state is not None and \
                    cp.values == E.preset_values(self.session, b, cp.state):
                return cp.state
            return CUSTOM
        ch = [one(cp) for cp in cps]
        if len(set(ch)) > 1:
            return DIFFERS
        if ch[0] == CUSTOM and any((x.state, x.values) != (cps[0].state, cps[0].values)
                                   for x in cps[1:]):
            return DIFFERS
        return ch[0]

    def _show_choice(self, b):
        st = self._choice(b)
        self.state.blockSignals(True)
        for extra in (CUSTOM, DIFFERS):
            i = self.state.findData(extra)
            if i >= 0 and extra != st:
                self.state.removeItem(i)
        if st in (CUSTOM, DIFFERS) and self.state.findData(st) < 0:
            self.state.addItem("Custom values" if st == CUSTOM
                               else "Differs per copy", st)
        i = 0 if st is KEEP else self.state.findData(st)
        self.state.setCurrentIndex(max(0, i))
        self.state.blockSignals(False)

    def _held(self, b) -> _Pending:
        pend = self.pending.get(b.id)
        return pend if pend is not None else _Pending(self._opts())

    def _settle(self, b, pend):
        pend.prune()
        if pend.empty():
            self.pending.pop(b.id, None)
        else:
            self.pending[b.id] = pend

    def _state_picked(self, *_a):
        if self._loading:
            return
        b = self._bundle()
        if b is None:
            return
        st = self.state.currentData()
        if st in (CUSTOM, DIFFERS):
            return
        pend = self._held(b)
        for c in self._view_copies(b):
            if st is KEEP:
                pend.copies.pop(c, None)
            else:
                pend.copies[c] = _Copy(st, E.preset_values(self.session, b, st))
        self._settle(b, pend)
        self._show_choice(b)
        self._changed(b)

    def _value_edited(self, b, p, value):
        """A parameter's New value picked or typed (None: keep the file's
        value) -- for every copy in view."""
        if self._loading:
            return
        pend = self._held(b)
        for c in self._view_copies(b):
            cp = pend.copies.setdefault(c, _Copy())
            if value is not None:
                cp.values[p.name] = int(value)
            elif cp.state is not None:
                # keep this copy's value although the preset sets another
                cur = E.read_values(self.session, b, c).get(p.name)
                if cur is None:
                    cp.values.pop(p.name, None)
                else:
                    cp.values[p.name] = int(cur)
            else:
                cp.values.pop(p.name, None)
        self._settle(b, pend)
        self._show_choice(b)
        self._mark(b)
        self._sync_buttons()
        # only this row changes -- unless a switch's EEPROM Type byte now
        # comes or goes with it: then the list, once the picker has returned
        if self._companion_rows(b) != self._companions.get(b.id, ()):
            QtCore.QTimer.singleShot(0, self._refresh_preview)
        else:
            self._restyle_param(b, p)

    def _restyle_param(self, b, p):
        """A parameter's row coloured for what its New value now does."""
        r = self._param_row.get(p.name)
        if r is None:
            return
        cps = self._view_copies(b)
        pend = self.pending.get(b.id)
        vals = [E.read_values(self.session, b, c).get(p.name) for c in cps]
        news = [pend.copies[c].values.get(p.name)
                if pend is not None and c in pend.copies else None for c in cps]
        self._paint_param(r, self._param_changes(vals, news))

    @staticmethod
    def _param_changes(curs, news) -> bool:
        return any(n is not None and n != c for c, n in zip(curs, news))

    def _paint_param(self, r, changed: bool):
        """A parameter row: blue and bold when Apply changes it, the theme's
        own text otherwise (a table or fault path it leaves is grey)."""
        col = _colors()
        for c in range(self.preview.columnCount()):
            it = self.preview.item(r, c)
            if it is None:
                continue
            if changed:
                it.setForeground(QtGui.QColor(col["changed"]))
            else:
                it.setData(QtCore.Qt.ItemDataRole.ForegroundRole, None)
            f = it.font()
            f.setBold(changed)
            it.setFont(f)
        w = self.preview.cellWidget(r, 4)
        if w is not None:
            w.setStyleSheet(self._widget_style(changed))

    @staticmethod
    def _widget_style(changed: bool, bold: Optional[bool] = None) -> str:
        if not changed and not bold:
            return ""
        out = ["font-weight: bold"] if (changed if bold is None else bold) else []
        if changed:
            out.append("color: %s" % _colors()["changed"])
        return "; ".join(out)

    def _companion_rows(self, b) -> tuple:
        """The EEPROM Type bytes the held values would switch (their
        addresses), for knowing when the list needs those rows."""
        pend = self.pending.get(b.id)
        if pend is None:
            return ()
        names = {p.name for p in b.params}
        out = []
        for g in self._view_copies(b):
            cp = pend.copies.get(g)
            if cp is None:
                continue
            changes, _w = E.plan(self.session, b, cp.state, [g], cp.values)
            out += [c.addr for c in changes
                    if c.param not in names and c.changed and c.addr not in out]
        return tuple(out)

    def _fault_edited(self, b, key, target, preset):
        """A fault path's New choice: ``target`` (a class, -1 for the table's
        own choice, None for off), held unless it is what the setting
        (``preset``) does anyway."""
        if self._loading:
            return
        pend = self._held(b)
        if target == preset:
            pend.faults.pop(key, None)
        else:
            pend.faults[key] = target
        self._settle(b, pend)
        self._mark(b)
        self._sync_buttons()
        self._restyle_fault(key, target, edited=target != preset)

    def _restyle_fault(self, key, choice, edited):
        """The fault path's row (every copy's, when listed per copy) after
        its own choice: no other row changes, so none is rebuilt."""
        col = _colors()
        for r in range(self._param_rows, self.preview.rowCount()):
            it = self.preview.item(r, 0)
            row = it.data(QtCore.Qt.ItemDataRole.UserRole + 1) if it is not None else None
            if not row or row.get("kind") != "fault" or row.get("key") != key:
                continue
            row = dict(row, choice=choice, edited=edited, new=E.fault_text(choice))
            it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, row)
            changed = choice != row.get("now")
            for c in range(self.preview.columnCount()):
                cell = self.preview.item(r, c)
                if cell is None:
                    continue
                cell.setForeground(QtGui.QColor(col["changed"] if changed else col["same"]))
                f = cell.font()
                f.setBold(edited)
                cell.setFont(f)
            w = self.preview.cellWidget(r, 4)
            if w is not None:
                w.setStyleSheet(self._widget_style(changed, edited or changed))

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
        self.preview.setUpdatesEnabled(False)
        try:
            self._fill_preview()
        finally:
            self.preview.setUpdatesEnabled(True)

    def _fill_preview(self):
        b = self._bundle()
        self.preview.setRowCount(0)
        self._param_row = {}
        if b is None:
            return
        self._loading = True
        cps = self._view_copies(b)
        every = self._bcopies(b)
        pend = self.pending.get(b.id)
        vals = {c: E.read_values(self.session, b, c) for c in every}
        multi = len(every) > 1
        params = E.present_params(self.session, b)
        self.preview.setRowCount(len(params))
        for r, p in enumerate(params):
            self._param_row[p.name] = r
            curs = [vals[c].get(p.name) for c in cps]
            same = len(set(curs)) == 1
            cur = curs[0] if same else None
            if same:
                now = p.value_text(cur)
                # one copy in view: the others' values when they differ
                others = [(o, vals[o].get(p.name)) for o in every
                          if o not in cps and vals[o].get(p.name) != cur]
                if others:
                    now += "  (%s)" % ", ".join("%s: %s" % (self._short(o), p.value_text(v))
                                                for o, v in others)
            else:
                now = " · ".join("%s: %s" % (self._short(c), p.value_text(v))
                                 for c, v in zip(cps, curs))
            tip = [p.meaning]
            if multi:
                for c in every:
                    e = _E.one(self.session, p.name, c)
                    tip.append("%s: %s%s" % (
                        E.copy_label(self.session, c), p.value_text(vals[c].get(p.name)),
                        " (the main copy: read by every coding)"
                        if _E.shared(self.session, p.name, c)
                        else " @0x%X" % e.addr if e is not None else ""))
            for c, text in enumerate([p.name, p.meaning, _PROV.get(p.prov, p.prov), now]):
                it = QtWidgets.QTableWidgetItem(text)
                it.setToolTip("\n".join(tip))
                self.preview.setItem(r, c, it)
            news = [pend.copies[c].values.get(p.name)
                    if pend is not None and c in pend.copies else None for c in cps]
            mixed = len(set(news)) > 1
            new = None if mixed else news[0]
            self.preview.setCellWidget(r, 4, self._value_widget(
                b, p, cur, new, differs=not same, mixed=mixed))
            self._paint_param(r, self._param_changes(curs, news))
        # a switch that follows the EEPROM coding (its Type byte 1) is set to
        # read the flash value written here: that byte changes too
        self._companions[b.id] = self._companion_rows(b)
        if pend is not None:
            names = {p.name for p in b.params}
            seen = set()
            for g in cps:
                cp = pend.copies.get(g)
                if cp is None:
                    continue
                changes, _w = E.plan(self.session, b, cp.state, [g], cp.values)
                for c in changes:
                    if c.param in names or not c.changed or c.addr in seen:
                        continue
                    seen.add(c.addr)
                    r = self.preview.rowCount()
                    self.preview.insertRow(r)
                    for col, text in enumerate([
                            c.param + (" [%s]" % self._short(g) if len(cps) > 1 else ""),
                            "Where the ECU reads the switch above: from the "
                            "EEPROM coding now; from this file's value after the change",
                            "ECU code", "%d (EEPROM coding)" % c.old,
                            "%d (this file's value)" % c.new]):
                        self.preview.setItem(r, col, QtWidgets.QTableWidgetItem(text))
                    self._paint_param(r, True)
        self._param_rows = self.preview.rowCount()
        self._loading = False
        self._refresh_hook_rows()

    def _value_widget(self, b, p, cur, new, differs=False, mixed=False):
        """The New picker: one entry per value, the file's own marked
        "— current" (choosing it keeps the file's value). ``differs``: the
        copies in view hold different values (the keep entry says so);
        ``mixed``: they are getting different new values."""
        ro = p.readonly or b.readonly or _view_only(self.ws)
        keep = ("Keep each copy's value" if differs
                else "%s — current" % (p.value_text(cur) or "(not read)"))
        if p.choices or ro:
            w = QtWidgets.QComboBox()
            keep_i = 0
            if ro or differs or cur is None or int(cur) not in p.choices:
                w.addItem(keep, None)
            if not ro:
                for v, lab in sorted(p.choices.items()):
                    if not differs and cur is not None and v == int(cur):
                        keep_i = w.count()
                        w.addItem("%d (%s) — current" % (v, lab), None)
                    else:
                        w.addItem("%d (%s)" % (v, lab), v)
            i = keep_i
            if new is not None and (differs or new != cur):
                i = w.findData(new)
                if i < 0:
                    w.addItem(str(new), new)
                    i = w.count() - 1
            if mixed:
                w.insertItem(0, "(differs per copy)", "__mixed__")
                i = 0
            w.setCurrentIndex(i)
            w.setEnabled(not ro)
            w.currentIndexChanged.connect(
                lambda _i, w=w: None if w.currentData() == "__mixed__"
                else self._value_edited(b, p, w.currentData()))
        else:
            lo, hi = E.value_range(p)
            hi = min(hi, 0x7FFFFFFF)            # a spin box holds 31 bits
            w = QtWidgets.QSpinBox()
            w.setRange(lo - 1, hi)
            w.setSpecialValueText("(differs per copy)" if mixed else keep)
            w.setValue(new if new is not None and (differs or new != cur) else lo - 1)
            w.setToolTip("Type a value from %d to %d, or step below %d to keep "
                         "the file's value" % (lo, hi, lo))
            w.valueChanged.connect(
                lambda v, lo=lo: self._value_edited(b, p, None if v < lo else v))
        return w

    def _sync_opt(self, b):
        """The hook's number is live only for the settings it applies to."""
        self._sync_choice(b)
        spec = self._opt_spec
        if not spec:
            return
        pend = self.pending.get(b.id)
        only = spec.get("states")
        on = not only or (pend is not None and any(
            pend.copies[c].state in only for c in self._view_copies(b)
            if c in pend.copies))
        self.opt.setEnabled(on and not _view_only(self.ws))
        self.opt_lbl.setEnabled(on)
        self._sync_check(b, pend)

    def _sync_check(self, b, pend):
        """The tick box is live for the settings it applies to, where the
        file allows it (the hook's ``fs_check_why``); its tooltip says why
        not otherwise."""
        chk = self._chk_spec
        if not chk:
            return
        only = chk.get("states")
        picked = [c for c in self._view_copies(b) if pend is not None
                  and c in pend.copies and (not only or pend.copies[c].state in only)]
        tip = "<p>%s</p>" % chk.get("tip", "")
        why = {}
        if picked and hasattr(self._hook(b), "fs_check_why"):
            for c in picked:
                try:
                    w = self._hooked(b, "fs_check_why", c,
                                     pend.copies[c].state, **self._opts()) or ""
                except Exception as ex:                   # noqa: BLE001
                    w = str(ex)
                if w:
                    why[c] = w
        ok = bool(picked) and len(why) < len(picked)
        self.chk.setEnabled(ok and not _view_only(self.ws))
        if why:
            multi = len(self._view_copies(b)) > 1
            lines = ["%s%s" % ("%s: " % self._short(c) if multi else "", w)
                     for c, w in why.items()]
            tip += "<p>%s: %s.</p>" % (
                "Not possible for some copies (their top breakpoints are "
                "raised instead)" if ok else "Not possible here, so the top "
                "breakpoints are raised instead", "; ".join(lines))
        self.chk.setToolTip(tip)

    #: widest each column but Meaning grows to its contents (pixels)
    _COL_CAPS = {0: 240, 2: 110, 3: 180, 4: 230}

    def _size_columns(self):
        for c, cap in self._COL_CAPS.items():
            self.preview.resizeColumnToContents(c)
            w = self.preview.columnWidth(c)
            if c == 4:
                w = max(w, 170)          # the value pickers are cell widgets
            self.preview.setColumnWidth(c, min(cap, w))

    def _add_hook_rows(self, rows, color, b=None):
        for row in rows:
            r = self.preview.rowCount()
            self.preview.insertRow(r)
            src = {"fault": "Fault path", "axis": "Table axis",
                   "table": "Table", "value": "Value"}.get(row.get("kind", ""), "")
            tip = _grid_tip(row)
            picker = b is not None and row.get("kind") == "fault" and "choice" in row
            for c, text in enumerate([row.get("name", ""), row.get("meaning", ""),
                                      src, row.get("current", ""),
                                      "" if picker else row.get("new", "")]):
                it = QtWidgets.QTableWidgetItem(text)
                it.setForeground(QtGui.QColor(color))
                it.setToolTip(tip or text)
                if c == 0:
                    it.setData(QtCore.Qt.ItemDataRole.UserRole, row.get("entity"))
                    it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, row)
                    if row.get("edited"):
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
                self.preview.setItem(r, c, it)
            if picker:
                self.preview.setCellWidget(r, 4, self._fault_widget(b, row))

    def _fault_widget(self, b, row):
        """A fault path's New picker: On (with the class it gets) or Off, the
        path's state now marked "— current"; held with the other changes."""
        w = QtWidgets.QComboBox()
        on_cls = row.get("on_class", -1)
        now_on = row.get("now") is not None
        w.addItem(E.fault_text(on_cls) + (" — current" if now_on else ""), "on")
        w.addItem("Off" + ("" if now_on else " — current"), "off")
        w.setCurrentIndex(0 if row.get("choice") is not None else 1)
        w.setEnabled(not b.readonly and not _view_only(self.ws))
        w.setToolTip("Switch this fault path on or off with the rest of the "
                     "changes (written on Apply, in the same undo step)")
        changed = row.get("choice") != row.get("now")
        w.setStyleSheet(self._widget_style(changed, bool(row.get("edited")) or changed))
        preset = row.get("preset")
        key = row.get("key")

        def picked(_i, w=w):
            on = w.currentData() == "on"
            if on:
                tgt = preset if preset is not None else on_cls
            else:
                tgt = None
            self._fault_edited(b, key, tgt, preset)
        w.currentIndexChanged.connect(picked)
        return w

    def _row_entity(self, r):
        """The table or parameter a preview row names: the hook's entity, or
        the parameter of that name in the copy in view (the first, with All
        copies)."""
        it = self.preview.item(r, 0)
        if it is None:
            return None
        ent = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if ent is not None:
            return ent
        name = it.text().split(" (", 1)[0].split(" [", 1)[0]
        b = self._bundle()
        g = self._view_copies(b)[0] if b is not None else ""
        hit = _E.one(self.session, name, g)
        if hit is not None:
            return hit
        named = _E.named(self.session, name)
        return named[0] if named else None

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

    def _hooked(self, b, what, *args, **kw):
        """A table hook's answer (``fs_preview`` / ``fs_describe`` /
        ``fs_involved``), kept until the file changes: the hooks read every
        table and fault path they work through, and the window asks again
        on every pick."""
        rev = E._revision(self.session)
        if rev != self._hook_rev:
            self._hook_cache.clear()
            self._hook_rev = rev
        ref = self._ref()
        key = (b.id, what, args, tuple(sorted(kw.items())), id(ref),
               E._revision(ref) if ref is not None else None)
        if rev is not None and key in self._hook_cache:
            return self._hook_cache[key]
        fn = getattr(self._hook(b), what, None)
        res = fn(self.session, *args, ref=ref, **kw) if fn is not None else None
        if rev is not None:
            self._hook_cache[key] = res
        return res

    def _refresh_hook_rows(self):
        self.preview.setUpdatesEnabled(False)
        try:
            self._fill_hook_rows()
        finally:
            self.preview.setUpdatesEnabled(True)

    def _fill_hook_rows(self):
        """The tables and fault paths the feature's hook will change, under
        the parameters; and its one-line note."""
        b = self._bundle()
        if b is None:
            return
        self._sync_opt(b)
        self.preview.setRowCount(self._param_rows)
        pend = self.pending.get(b.id)
        hook = self._hook(b)
        self.hook_info.clear()
        if hook is None:
            self._size_columns()
            return
        cps = self._view_copies(b)
        multi = len(cps) > 1
        picked = [(c, pend.copies[c].state) for c in cps
                  if pend is not None and c in pend.copies
                  and pend.copies[c].state is not None]
        rows, infos = [], []
        for i, (c, st) in enumerate(picked):
            try:
                rows += [self._tag(x, multi) for x in (self._hooked(
                    b, "fs_preview", c, st, first=i == 0, **self._opts()) or [])]
                info = self._hooked(b, "fs_describe", c, st, **self._opts()) or ""
            except Exception as ex:                       # noqa: BLE001
                info = "(Could not work out the table changes: %s)" % ex
            if info and info not in infos:
                infos.append(info)
        # every table and fault path the feature works through, all of the
        # time: those the setting (or a fault path's own choice) changes in
        # blue, with their new values; the rest grey, as they are
        inv = []
        if hasattr(hook, "fs_involved"):
            for c in cps:
                try:
                    inv += [self._tag(x, multi) for x in (self._hooked(
                        b, "fs_involved", c, **self._opts()) or [])]
                except Exception:                         # noqa: BLE001
                    pass
        changed: Dict[tuple, dict] = {}
        for r in rows:
            changed.setdefault(self._row_key(r), r)
        out, seen = [], set()
        for r in inv:
            k = self._row_key(r)
            if k in seen:
                continue
            seen.add(k)
            hit = changed.pop(k, None)
            out.append((hit, True) if hit is not None else (r, False))
        out += [(r, True) for r in changed.values()]
        col = _colors()
        faults = pend.faults if pend is not None else {}
        table = None
        for r, is_changed in out:
            if r.get("kind") == "fault" and r.get("key"):
                if table is None:
                    table = self._dtc()
                fp = table.paths_by_name.get(r["key"]) if table is not None else None
                if fp is not None:
                    now = (fp.current_class if table.is_enabled(fp) else None)
                    preset = r.get("target") if is_changed else now
                    choice = faults.get(r["key"], preset)
                    on_cls = (now if now is not None
                              else preset if preset is not None
                              else E.fault_on_class(self.session, table, fp, self._ref()))
                    r = dict(r, now=now, preset=preset, choice=choice,
                             edited=r["key"] in faults, on_class=on_cls,
                             new=E.fault_text(choice))
                    self._add_hook_rows([r], col["changed"] if choice != now
                                        else col["same"], b)
                    continue
            if is_changed:
                self._add_hook_rows([r], col["changed"], b)
            else:
                self._add_hook_rows([dict(r, new="(unchanged)" if picked else "")],
                                    col["same"], b)
        if picked:
            info = "  ".join(infos)
            if multi and len(picked) > 1:
                info += ("  Each copy's tables are listed (marked with the copy); a "
                         "table every copy shares is written once.")
            if not rows:
                info = ("No table or fault path needs to change. " + info).strip()
            self.hook_info.setText(info.strip())
        elif not b.readonly:
            self.hook_info.setText(
                "The tables and fault paths this feature works through. Pick a "
                "setting to see what it changes; a fault path can also be "
                "switched on or off on its own in the New column.")
        self._size_columns()

    def _tag(self, row, multi):
        """A hook row from one copy: named with its dataset when several are
        in view (EDC15 datasets share table names)."""
        ent = row.get("entity")
        g = _E.group_of(ent) if ent is not None else ""
        if multi and g:
            row = dict(row, name="%s [%s]" % (row.get("name", ""), g))
        return row

    @staticmethod
    def _row_key(row):
        if row.get("kind") == "fault":
            return ("f", row.get("key") or row.get("name"))
        ent = row.get("entity")
        if ent is not None:
            return ("e", id(ent), row.get("axis") or "")
        return ("n", row.get("name"))

    def _dtc(self):
        """The file's fault paths now (for the per-path choices), or None."""
        try:
            t = E.dtc_table(self.session)        # one per revision of the file
            if getattr(t, "paths_by_name", None) is None:
                t.paths_by_name = {fp.name: fp for fp in t.paths.values()}
            return t
        except Exception:                                 # noqa: BLE001
            return None

    # -- writing -----------------------------------------------------------
    def _collect(self):
        """``[(bundle, changes, warnings, hook, pending)]`` for every feature
        with changes."""
        out = []
        for b in self.bundles:
            pend = self.pending.get(b.id)
            if pend is None:
                continue
            order = self._bcopies(b)
            cps = sorted(pend.copies, key=lambda c: order.index(c) if c in order else 99)
            live, warns, seen = [], [], set()
            for g in cps:
                cp = pend.copies[g]
                changes, w = E.plan(self.session, b, cp.state, [g], cp.values)
                warns += [x for x in w if x not in warns]
                for c in changes:
                    if c.changed and c.addr not in seen:    # a shared value once
                        seen.add(c.addr)
                        live.append(c)
            hooked = [(g, pend.copies[g].state) for g in cps
                      if pend.copies[g].state is not None]
            out.append((b, live, warns, self._hook(b), pend, hooked))
        return out

    def _apply(self) -> bool:
        """Write every feature changed as one undo step (after a summary).
        True when written (or nothing was pending)."""
        if not self.pending:
            return True
        work = self._collect()
        lines, warns = [], []
        for b, live, w, hook, pend, hooked in work:
            warns += ["%s: %s" % (b.title, x) for x in w]
            bits = []
            if live:
                bits.append("%d value%s" % (len(live), "" if len(live) == 1 else "s"))
            tables, faults = set(), set(pend.faults)
            if hook is not None and hasattr(hook, "fs_preview"):
                for i, (g, st) in enumerate(hooked):
                    try:
                        rows = hook.fs_preview(self.session, g, st, first=i == 0,
                                               ref=self._ref(), **pend.opts) or []
                    except Exception:                     # noqa: BLE001
                        rows = []
                    tables |= {self._row_key(r) for r in rows
                               if r.get("kind") in ("table", "axis", "value")}
                    faults |= {r.get("key") or r.get("name") for r in rows
                               if r.get("kind") == "fault"}
            if tables:
                bits.append("%d table%s" % (len(tables), "" if len(tables) == 1 else "s"))
            if faults:
                bits.append("%d fault path%s" % (len(faults), "" if len(faults) == 1 else "s"))
            targets = []
            for g, cp in pend.copies.items():
                t = b.label(cp.state) if cp.state else "custom values"
                if cp.state and cp.values != E.preset_values(self.session, b, cp.state):
                    t += " (edited)"
                targets.append((g, t))
            if not targets:
                target = "fault paths"
            elif len({t for _g, t in targets}) == 1:
                target = targets[0][1]
            else:
                target = ", ".join("%s: %s" % (self._short(g), t) for g, t in targets)
            copies = ([self._short(g) for g in pend.copies]
                      if len(self.copies) > 1 and pend.copies else [])
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
        label = "Feature switches: " + ", ".join(w_[0].title for w_ in work)
        n = 0
        from . import resize as _rz
        mods_before = list(_rz.mods(self.session))
        try:
            with self.session.transaction(label):
                for b, live, _w, hook, pend, hooked in work:
                    n += E.apply(self.session, live, b.title) if live else 0
                    if hook is not None and hasattr(hook, "fs_apply"):
                        for i, (g, st) in enumerate(hooked):
                            n += hook.fs_apply(self.session, g, st, b.title,
                                               first=i == 0, ref=self._ref(),
                                               **pend.opts) or 0
                    # fault paths chosen one by one, over what the setting did
                    n += E.apply_fault_edits(self.session, pend.faults,
                                             "%s: fault paths" % b.title)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                self, "Feature switches", "Nothing was changed: %s" % ex)
            return False
        self.pending.clear()
        for b in self.bundles:
            self._mark(b)
        if _rz.mods(self.session) != mods_before:
            # a table was resized (Cut when warm, the smoke limiter's map):
            # its modifier goes with the project, the tree follows
            try:
                from .gui_resize import after_resize
                after_resize(self.ws)
            except Exception:                             # noqa: BLE001
                pass
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
        g = self._view_copies(b)[0]
        st = E.current_state(self.session, b, g)
        q = ("Look at the feature switch \"%s\" (feature id %s) of tab %s with the "
             "feature_switches tool. Explain what each of its settings does on this "
             "ECU, what the file is set to now (%s), and what changing it would do "
             "and risk. If I ask for a change, work it out with "
             "feature_switch_changes." % (b.title, b.id, h,
                                           b.label(st) if st else "no preset matches"))
        gui_ai.show_assistant(self.ws.win, q)

    def _after_apply(self, label: str = ""):
        """The main window shows what was written (saving it is the main
        window's Save)."""
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


__all__ = ["FeatureSwitchDialog", "availability", "open_for"]
