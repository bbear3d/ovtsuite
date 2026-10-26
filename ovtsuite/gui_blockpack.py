# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Build logging blocks...: lay the binary's measuring blocks out so
each logging preset reads fewer groups per row (docs/LOGGING_BLOCKS_PLAN.md).

One group is one round trip to the controller, so fewer groups is more rows
a second. For each preset -- the shipped ones and yours -- the window shows
how many groups it reads today and how few it could, by filling empty cells
of the file's own groups or by using groups the file leaves unused. A preset
that cannot be read in fewer groups is listed but cannot be ticked.

Generate writes the ticked presets' cells as one undo step and records them
in the project (and in your settings, under the file's identity, so the
logger finds them with no file open). The logger then uses them by default,
after reading them once from the ECU to make sure the flashed software has
them.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from .datalog.blockpack import service as _svc
from .datalog.blockpack.model import NEW, NoGain, PresetPlan
from .datalog.blockpack.provider import build_layout, provider_for

FILL_KEY = "blockpack/fill_stock_groups"

COLS = ["", "Preset", "Today", "Optimised", "Rate", "Changes", "In the file"]
C_TICK, C_NAME, C_TODAY, C_OPT, C_RATE, C_CHANGES, C_STATE = range(7)

STATE_TEXT = {"current": "Built", "stale": "Out of date: the preset changed",
              "broken": "Changed since (undo or a hand edit)",
              _svc.ORPHAN: "Built for a deleted preset"}


def _prefs():
    return QtCore.QSettings(*branding.QSETTINGS)


def _project_set(ws):
    ref = getattr(ws, "project_ref", None)
    try:
        return ref.set if ref is not None else None
    except Exception:
        return None


def availability(ws) -> Optional[str]:
    """None when the window can open for tab ``ws``, else why not."""
    s = getattr(ws, "primary", None)
    if s is None:
        return "No file open"
    if getattr(s, "support", "full") != "full":
        return "Needs the platform's module and a definition for this file"
    if provider_for(s) is None:
        try:
            from .msblock import has_measuring_blocks
            mapped = has_measuring_blocks(s)
        except Exception:
            mapped = True
        if not mapped and getattr(s, "platform", "") in _svc.ECU_OF_PLATFORM:
            return ("The definition for this file does not place its measuring-block table, "
                    "so its blocks cannot be read or laid out")
        return ("This file's controller has no measuring blocks %s can lay out "
                "(EDC15 and EDC16 files)" % branding.APP_NAME)
    return None


def open_for(ws):
    why = availability(ws)
    if why:
        QtWidgets.QMessageBox.information(ws, "Build logging blocks", why)
        return None
    return ws.open_tool("blockpack", lambda: BlockPackDialog(ws))


def refresh_loggers() -> None:
    """Let open data loggers relabel their presets (optimised or not)."""
    try:
        from .datalog import gui_log
        gui_log.refresh_optimised()
    except Exception:
        pass


class BlockPackDialog(QtWidgets.QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.setWindowTitle("Build logging blocks")
        try:
            from .icons import icon
            self.setWindowIcon(icon("logging-blocks"))
        except Exception:
            pass
        self._entries: List[_svc.PresetEntry] = []
        self._preview: Dict[str, object] = {}
        self._filling = False

        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "The logger reads one measuring block per round trip, so a preset that "
            "reads fewer blocks logs more rows a second. Tick the presets to lay out "
            "in this file, then save it and write it to the ECU.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.room = QtWidgets.QLabel("")
        self.room.setStyleSheet("color: gray")
        v.addWidget(self.room)
        #: what limits the analysis of this file (tables the definition leaves
        #: out, reserved blocks unknown, cells without names); hidden when nothing
        self.limits = QtWidgets.QLabel("")
        self.limits.setWordWrap(True)
        self.limits.setTextFormat(QtCore.Qt.PlainText)
        self.limits.setVisible(False)
        v.addWidget(self.limits)
        self.fill_cb = QtWidgets.QCheckBox("Fill empty cells in stock groups")
        self.fill_cb.setToolTip(
            "Put a preset's values into the empty cells of groups the file already "
            "uses, before using groups it leaves empty. The file's own cells are never "
            "changed either way.")
        try:
            self.fill_cb.setChecked(str(_prefs().value(FILL_KEY, "true")).lower() != "false")
        except Exception:
            self.fill_cb.setChecked(True)
        self.fill_cb.toggled.connect(self._fill_toggled)
        v.addWidget(self.fill_cb)

        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(C_CHANGES, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeaderItem(C_TODAY).setToolTip(
            "The blocks the logger reads for this preset with the file as it is")
        self.table.horizontalHeaderItem(C_OPT).setToolTip(
            "The blocks it would read once laid out")
        self.table.horizontalHeaderItem(C_RATE).setToolTip(
            "Rows a second, roughly: one block takes about %.0f ms" %
            _svc.PER_GROUP_MS.get(self.session.platform, 100.0))
        self.table.itemChanged.connect(self._ticked)
        self.table.itemSelectionChanged.connect(self._show_detail)
        v.addWidget(self.table, 2)

        v.addWidget(QtWidgets.QLabel("Cells of the preset selected:"))
        self.detail = QtWidgets.QTreeWidget()
        self.detail.setHeaderLabels(["Block / cell", "Signal", ""])
        self.detail.setRootIsDecorated(True)
        self.detail.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.detail.header().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        v.addWidget(self.detail, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QtWidgets.QHBoxLayout()
        self.remove_btn = QtWidgets.QPushButton("Remove generated")
        self.remove_btn.setToolTip("Empty again the cells built for the ticked presets "
                                   "(only where the file still holds what was built), "
                                   "as one undo step")
        self.remove_btn.clicked.connect(self._remove)
        bb.addWidget(self.remove_btn)
        bb.addStretch(1)
        self.gen_btn = QtWidgets.QPushButton("Generate")
        self.gen_btn.setDefault(True)
        self.gen_btn.setToolTip("Write the ticked presets' cells into this file as one "
                                "undo step, and record them for the logger")
        self.gen_btn.clicked.connect(self._generate)
        bb.addWidget(self.gen_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        bb.addWidget(close)
        v.addLayout(bb)
        self.resize(860, 620)
        self.refresh_from_session()

    # ---- building the view ----
    def _builder(self) -> _svc.Builder:
        return _svc.Builder(self.session, _project_set(self.ws), _prefs())

    def refresh_from_session(self):
        """Plan every preset again (after a generate, an undo, an edit made
        elsewhere): what each could gain, and what the file holds now."""
        b = self._builder()
        self._b = b
        if not b.available:
            self.status.setText("This file's measuring blocks cannot be read.")
            self.gen_btn.setEnabled(False)
            return
        try:
            limits = b.limits()
        except Exception:                             # noqa: BLE001
            limits = []
        self.limits.setText("\n".join(limits))
        self.limits.setVisible(bool(limits))
        self._entries = b.entries()
        layout = build_layout(b.provider, b.namer, b.manifest, release=[])
        empties = sum(len(layout.fillable(g)) for g in layout.cells)
        ds = ""
        if self.session.platform == "edc15":
            ds = "; each block is written in every dataset"
        self.room.setText("%d unused group%s, %d empty cell%s in groups the file uses%s."
                          % (len(layout.free), "" if len(layout.free) == 1 else "s",
                             empties, "" if empties == 1 else "s", ds))
        try:
            alloc = b.plan([e.key for e in self._entries if not e.is_orphan],
                           fill=self.fill_cb.isChecked())
        except Exception as ex:                       # noqa: BLE001
            self.status.setText("Could not plan: %s" % ex)
            alloc = None
        self._preview = {}
        if alloc is not None:
            for p in alloc.plans:
                self._preview[p.key] = p
            for s in alloc.skipped:
                self._preview[s.key] = s
        self._fill_table()

    def _fill_table(self):
        self._filling = True
        keep = self._ticked_keys()
        rows: List[Optional[_svc.PresetEntry]] = []
        shipped = [e for e in self._entries if not e.is_user and not e.is_orphan]
        mine = [e for e in self._entries if e.is_user]
        gone = [e for e in self._entries if e.is_orphan]
        rows += shipped
        heads = {}
        for title, part in (("Your presets", mine), ("Presets no longer there", gone)):
            if part:
                heads[len(rows)] = title
                rows.append(None)
                rows += part
        self._rows = rows
        self.table.setRowCount(len(rows))
        plat = self.session.platform
        for r, e in enumerate(rows):
            if e is None:
                head = QtWidgets.QTableWidgetItem(heads.get(r, ""))
                f = head.font()
                f.setBold(True)
                head.setFont(f)
                head.setFlags(QtCore.Qt.ItemIsEnabled)
                self.table.setItem(r, C_NAME, head)
                for c in (C_TICK, C_TODAY, C_OPT, C_RATE, C_CHANGES, C_STATE):
                    it = QtWidgets.QTableWidgetItem("")
                    it.setFlags(QtCore.Qt.ItemIsEnabled)
                    self.table.setItem(r, c, it)
                continue
            p = self._preview.get(e.key)
            tick = QtWidgets.QTableWidgetItem("")
            tick.setData(QtCore.Qt.UserRole, e.key)
            state = self._b.status(e)
            gain = isinstance(p, PresetPlan)
            flags = QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
            if gain or state:
                flags |= QtCore.Qt.ItemIsUserCheckable
            tick.setFlags(flags)
            on = e.key in keep if keep is not None else (gain and state != "current")
            tick.setCheckState(QtCore.Qt.Checked if on and (gain or state) else QtCore.Qt.Unchecked)
            self.table.setItem(r, C_TICK, tick)
            name = QtWidgets.QTableWidgetItem(e.title)
            self.table.setItem(r, C_NAME, name)
            base = p.baseline if p is not None else []
            self.table.setItem(r, C_TODAY, QtWidgets.QTableWidgetItem(
                "%d  (%s)" % (len(base), ", ".join(map(str, base))) if base else "-"))
            if gain:
                self.table.setItem(r, C_OPT, QtWidgets.QTableWidgetItem(
                    "%d  (%s)" % (len(p.groups), ", ".join(map(str, p.groups)))))
                self.table.setItem(r, C_RATE, QtWidgets.QTableWidgetItem(
                    "~%.1f -> ~%.1f rows/s" % (_svc.estimate_hz(plat, len(base)),
                                               _svc.estimate_hz(plat, len(p.groups)))))
                ch = QtWidgets.QTableWidgetItem(p.describe())
            else:
                self.table.setItem(r, C_OPT, QtWidgets.QTableWidgetItem("-"))
                self.table.setItem(r, C_RATE, QtWidgets.QTableWidgetItem(""))
                if e.is_orphan:
                    ch = QtWidgets.QTableWidgetItem(
                        "The preset was deleted; Remove generated empties the cells built "
                        "for it")
                else:
                    why = p.reason if isinstance(p, NoGain) else "nothing to plan"
                    if why.startswith("not analysed"):
                        ch = QtWidgets.QTableWidgetItem(why[0].upper() + why[1:])
                    else:
                        ch = QtWidgets.QTableWidgetItem("No gain: %s" % why)
                ch.setForeground(QtGui.QBrush(self.palette().color(
                    QtGui.QPalette.Disabled, QtGui.QPalette.Text)))
                name.setForeground(ch.foreground())
            self.table.setItem(r, C_CHANGES, ch)
            st = QtWidgets.QTableWidgetItem(STATE_TEXT.get(state, ""))
            self.table.setItem(r, C_STATE, st)
        self._filling = False
        self._sync_buttons()
        if self.table.currentRow() < 0 and rows:
            self.table.selectRow(0)
        self._show_detail()

    def _ticked_keys(self) -> Optional[List[str]]:
        if not getattr(self, "_rows", None):
            return None
        out = []
        for r, e in enumerate(self._rows):
            it = self.table.item(r, C_TICK)
            if e is not None and it is not None and it.checkState() == QtCore.Qt.Checked:
                out.append(e.key)
        return out

    def _sync_buttons(self):
        keys = self._ticked_keys() or []
        gains = [k for k in keys if isinstance(self._preview.get(k), PresetPlan)]
        built = [e.key for e in self._entries if e.key in keys and self._b.status(e)]
        self.gen_btn.setEnabled(bool(gains))
        self.remove_btn.setEnabled(bool(built))

    def _ticked(self, item):
        if not self._filling and item.column() == C_TICK:
            self._sync_buttons()

    def _fill_toggled(self, on):
        try:
            _prefs().setValue(FILL_KEY, "true" if on else "false")
        except Exception:
            pass
        self.refresh_from_session()

    def _show_detail(self):
        self.detail.clear()
        r = self.table.currentRow()
        e = self._rows[r] if 0 <= r < len(getattr(self, "_rows", [])) else None
        if e is None:
            return
        p = self._preview.get(e.key)
        if not isinstance(p, PresetPlan):
            return
        written = {(w.group, w.slot): w for w in p.writes}
        named = self._b.named()
        for g in p.groups:
            kind = ("new block" if any(w.group == g and w.kind == NEW for w in p.writes)
                    else "cells added" if any(w.group == g for w in p.writes) else "as it is")
            top = QtWidgets.QTreeWidgetItem(["Block %d" % g, "", kind])
            self.detail.addTopLevelItem(top)
            slots = sorted(set(named.cell_fields(g)) | {s for (gg, s) in written if gg == g})
            for s in slots:
                w = written.get((g, s))
                label = w.source.label or w.source.signal if w is not None \
                    else named.resolve(g, s)[0]
                QtWidgets.QTreeWidgetItem(top, ["Cell %d" % (s + 1), label,
                                                "added" if w is not None else ""])
            top.setExpanded(True)

    # ---- actions ----
    def _after_change(self, msg: str):
        ws = self.ws
        try:
            ws._changed_cache = None
        except Exception:
            pass
        try:
            ws.model.refresh()
        except Exception:
            pass
        # the grid, the status line and the tab's other tool windows (the
        # measuring-block window shows the new cells)
        for meth in ("_populate", "_status", "_refresh_tools"):
            try:
                getattr(ws, meth)()
            except Exception:
                pass
        ref = getattr(self.ws, "project_ref", None)
        if ref is not None:
            try:
                from . import gui_project as _gp
                win = self.ws.win
                for t in ref.doc.tabs(win):
                    win._workspace_changed(t)
                _gp.refresh_panel(win)
                _gp.autosave_soon(win, ref.doc)
            except Exception:
                pass
        try:
            self.ws.window().statusBar().showMessage(msg, 10000)
        except Exception:
            pass
        self.status.setText(msg)
        refresh_loggers()
        self.refresh_from_session()

    def _generate(self):
        keys = [k for k in (self._ticked_keys() or [])
                if isinstance(self._preview.get(k), PresetPlan)]
        if not keys:
            return
        b = self._builder()
        alloc = b.plan(keys, fill=self.fill_cb.isChecked())
        if not alloc.plans:
            self.status.setText("Nothing to build: %s" % "; ".join(
                "%s: %s" % (s.title, s.reason) for s in alloc.skipped))
            return
        try:
            b.generate(alloc)
        except Exception as ex:                       # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Build logging blocks",
                                          "Could not write the blocks: %s" % ex)
            return
        cells = len(alloc.writes)
        names = ", ".join(p.title for p in alloc.plans)
        msg = ("Built logging blocks for %s: %d cell%s. Save the file and write it to the "
               "ECU; the logger reads the blocks once before using them. Edit > Undo takes "
               "this back." % (names, cells, "" if cells == 1 else "s"))
        if alloc.skipped:
            msg += " Not built: %s." % "; ".join("%s (%s)" % (s.title, s.reason)
                                                 for s in alloc.skipped)
        self._after_change(msg)

    def _remove(self):
        keys = self._ticked_keys() or []
        b = self._builder()
        keys = [e.key for e in b.entries() if e.key in keys and b.status(e)]
        if not keys:
            return
        n = b.remove(keys)
        self._after_change("Removed the logging blocks of %d preset%s: %d cell%s emptied. "
                           "Edit > Undo takes this back."
                           % (len(keys), "" if len(keys) == 1 else "s", n, "" if n == 1 else "s"))


__all__ = ["availability", "open_for", "refresh_loggers", "BlockPackDialog"]
