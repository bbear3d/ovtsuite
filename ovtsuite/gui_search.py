# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Find panel: a dock on the main window that searches parameters.

One panel serves every tab, which is what lets a search run across all the open
files. It is a ``QDockWidget``, so it docks, floats and closes like any other
panel, and everything in it -- the query, the options, the results, the
history, which row is selected -- lives in widgets that are only hidden, never
destroyed, so collapsing or closing it and opening it again brings back
exactly what was there.

The searching itself is :mod:`ovtsuite.search` (Qt-free); this module is the
window around it: a worker thread per search that a newer search cancels,
live results as you type for the cheap searches, a result list whose rows
select the parameter in the tree (and optionally pin it in its own table tab),
and highlighting of the matching cells in the grid.

``install(win, ...)`` is the only thing the main window calls.
"""

from __future__ import annotations

import json
import threading
from typing import Callable, List, Optional

from . import search as S

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    HAVE_QT = True
except Exception:                                   # pragma: no cover
    HAVE_QT = False

#: settings keys (all under the app's QSettings scope)
K_OPTS = "search/options"
K_HIST = "search/history"
K_SCOPE = "search/scope"
K_MODE = "search/mode"
K_QUERIES = "search/queries"
K_COLLAPSED = "search/collapsed"

#: rows shown at most; the engine stops collecting here
RESULT_CAP = 2000
LIVE_DELAY_MS = 160
DOCK_HEIGHT = 300

MODE_LABELS = (("text", "Parameters"), ("value", "Values"),
               ("address", "Address"), ("bytes", "Bytes in parameter"))
SCOPE_LABELS = (("tab", "This tab"), ("pair", "This tab + comparison"),
                ("all", "All open tabs"))
FIELD_LABELS = (("symbol", "Symbol"), ("title", "Name"),
                ("category", "Category / tier"), ("description", "Description"),
                ("units", "Units"))

PLACEHOLDERS = {
    "text": "Find a parameter -- words in any order, e.g. boost limit",
    "value": "A value, range or comparison: 1500   10..20   >=5   !=0",
    "address": "An address: 0x1A2B3C   1A2B3C-1A2BFF   0x1A2B3C+16",
    "hex": "Hex bytes, ?? for any byte: DE AD ?? EF",
    "ascii": "Text stored in the parameter",
    "int": "A whole number, as stored",
    "float": "A decimal number, as stored (IEEE-754)",
}

if HAVE_QT:
    _LIVE = set()                   # running threads, kept alive until done
    _ENGINE = {"e": None}
    _ENGINE_LOCK = threading.Lock()

    def _get_engine(title_fn, tier_name):
        """One engine for the whole app, so every tab shares its caches. Made
        on first use -- on the worker thread, since loading the shipped label
        index is the one slow part."""
        with _ENGINE_LOCK:
            if _ENGINE["e"] is None:
                _ENGINE["e"] = S.SearchEngine(
                    title_fn, tier_name, S.load_shipped_titles())
            return _ENGINE["e"]

    class _SearchThread(QtCore.QThread):
        done = QtCore.Signal(int, object, object)       # gen, hits, error
        progressed = QtCore.Signal(int, int, int)       # gen, done, total

        def __init__(self, gen, job, cancel):
            super().__init__()
            self.gen, self.job, self.cancel = gen, job, cancel

        def run(self):
            try:
                hits = self.job(self.cancel, lambda d, t:
                                self.progressed.emit(self.gen, d, t))
                self.done.emit(self.gen, hits, None)
            except S.Cancelled:
                self.done.emit(self.gen, None, "cancelled")
            except Exception as ex:                     # QueryError or a bug
                self.done.emit(self.gen, None, ex)

    #: how long quitting waits for running searches, all of them together
    JOIN_MS = 3000

    def _join_all():
        """On quit: cancel every running search first, then wait for them
        within one shared deadline."""
        live = list(_LIVE)
        for t in live:
            try:
                t.cancel.set()
            except RuntimeError:
                pass
        deadline = QtCore.QDeadlineTimer(JOIN_MS)
        for t in live:
            try:
                t.wait(deadline)
            except RuntimeError:
                pass

    def _hook_quit():
        """Connect :func:`_join_all` to the application's quit, once."""
        app = QtWidgets.QApplication.instance()
        if app is not None and not app.property("_ovt_search_join"):
            app.aboutToQuit.connect(_join_all)
            app.setProperty("_ovt_search_join", True)

    class SearchPanel(QtWidgets.QWidget):
        """The contents of the dock."""

        def __init__(self, win, dock, title_fn: Callable, tier_name: Callable,
                     settings: Callable):
            super().__init__()
            self.win, self.dock = win, dock
            self.title_fn, self.tier_name = title_fn, tier_name
            self._settings = settings
            self.opts = self._load_opts()
            self.history = S.History.loads(self._get(K_HIST, "[]"))
            self._queries = self._load_queries()
            self._gen = 0
            self._cancel: Optional[S.Cancel] = None
            self._busy = False
            self._select_first = False
            self._revs = {}
            self._ran_for = None
            self._expanded_h = DOCK_HEIGHT
            self._live_timer = QtCore.QTimer(self, singleShot=True,
                                             interval=LIVE_DELAY_MS)
            self._live_timer.timeout.connect(self.run)
            self._poll_timer = QtCore.QTimer(self, interval=800)
            self._poll_timer.timeout.connect(self._poll)
            self._build()
            self._restore()
            _hook_quit()
            self._poll_timer.start()

        # -- settings ------------------------------------------------------
        def _get(self, key, default):
            try:
                v = self._settings().value(key, default)
                return default if v is None else v
            except Exception:
                return default

        def _put(self, key, value):
            try:
                self._settings().setValue(key, value)
            except Exception:
                pass

        def _load_opts(self) -> S.SearchOptions:
            try:
                d = json.loads(self._get(K_OPTS, "{}"))
            except (ValueError, TypeError):
                d = {}
            o = S.SearchOptions.from_dict(d if isinstance(d, dict) else {})
            o.max_results = RESULT_CAP
            return o

        def _load_queries(self) -> dict:
            try:
                d = json.loads(self._get(K_QUERIES, "{}"))
                return {k: str(v) for k, v in d.items() if k in S.MODES}
            except (ValueError, TypeError, AttributeError):
                return {}

        def _save(self):
            self._put(K_OPTS, json.dumps(self.opts.to_dict()))
            self._put(K_HIST, self.history.dumps())
            self._put(K_SCOPE, self.scope.currentData())
            self._put(K_MODE, self.opts.mode)
            self._put(K_QUERIES, json.dumps(self._queries))
            self._put(K_COLLAPSED, "1" if self.collapsed else "0")

        # -- construction --------------------------------------------------
        def _build(self):
            Q = QtWidgets
            self.collapsed = False

            self.btn_fold = Q.QToolButton()
            self.btn_fold.setAutoRaise(True)
            self.btn_fold.setArrowType(QtCore.Qt.ArrowType.DownArrow)
            self.btn_fold.setToolTip("Collapse or expand the results "
                                     "(the search stays as it is)")
            self.btn_fold.clicked.connect(self.toggle_collapsed)

            self.mode = Q.QComboBox()
            for key, label in MODE_LABELS:
                self.mode.addItem(label, key)
            self.mode.setToolTip(
                "Parameters: names, descriptions, categories, units\n"
                "Values: parameters whose data contains a value\n"
                "Address: which parameter owns an address (also mid-table)\n"
                "Bytes: a byte pattern inside the parameter shown in the table"
                "\n(searching the whole image is the hex editor's job)")
            self.mode.currentIndexChanged.connect(self._mode_changed)

            self.query = Q.QComboBox()
            self.query.setEditable(True)
            self.query.setInsertPolicy(Q.QComboBox.InsertPolicy.NoInsert)
            self.query.setCompleter(None)
            self.query.setSizePolicy(Q.QSizePolicy.Policy.Expanding,
                                     Q.QSizePolicy.Policy.Fixed)
            self.query.setMinimumWidth(240)
            self.edit = self.query.lineEdit()
            self.edit.setClearButtonEnabled(True)
            self.edit.textChanged.connect(self._text_changed)
            self.edit.installEventFilter(self)
            self.query.activated.connect(lambda _i: self.run(commit=True))

            self.btn_opts = Q.QToolButton()
            self.btn_opts.setText("Options")
            self.btn_opts.setPopupMode(
                Q.QToolButton.ToolButtonPopupMode.InstantPopup)
            self.btn_opts.setToolButtonStyle(
                QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
            self.menu = Q.QMenu(self.btn_opts)
            self.btn_opts.setMenu(self.menu)
            self.menu.aboutToShow.connect(self._refresh_menu)
            self._build_menu()

            self.scope = Q.QComboBox()
            for key, label in SCOPE_LABELS:
                self.scope.addItem(label, key)
            self.scope.setToolTip("Which files to search. Searches the tab "
                                  "you are looking at unless you widen this.")
            self.scope.currentIndexChanged.connect(self._scope_changed)

            self.btn_go = Q.QPushButton("Find")
            self.btn_go.setDefault(False)
            self.btn_go.setAutoDefault(False)
            self.btn_go.clicked.connect(self._go_or_stop)

            self.btn_prev = Q.QToolButton()
            self.btn_prev.setArrowType(QtCore.Qt.ArrowType.UpArrow)
            self.btn_prev.setAutoRaise(True)
            self.btn_prev.setToolTip("Previous result  (Shift+F3)")
            self.btn_prev.clicked.connect(lambda: self.step(-1))
            self.btn_next = Q.QToolButton()
            self.btn_next.setArrowType(QtCore.Qt.ArrowType.DownArrow)
            self.btn_next.setAutoRaise(True)
            self.btn_next.setToolTip("Next result  (F3)")
            self.btn_next.clicked.connect(lambda: self.step(1))

            self.status = Q.QLabel("")
            self.status.setSizePolicy(Q.QSizePolicy.Policy.Ignored,
                                      Q.QSizePolicy.Policy.Preferred)
            self.status.setMinimumWidth(80)

            top = Q.QHBoxLayout()
            top.setContentsMargins(4, 2, 4, 0)
            for w, st in ((self.btn_fold, 0), (self.mode, 0), (self.query, 1),
                          (self.btn_opts, 0), (self.scope, 0),
                          (self.btn_go, 0), (self.btn_prev, 0),
                          (self.btn_next, 0)):
                top.addWidget(w, st)
            top.addWidget(self.status, 1)

            # -- mode-specific row ------------------------------------------
            self.rowbox = Q.QStackedWidget()
            self.summary = Q.QLabel("")
            self.summary.setEnabled(False)
            self.rowbox.addWidget(self._wrap(self.summary))     # text
            self.rowbox.addWidget(self._build_value_row())      # value
            self.rowbox.addWidget(self._build_address_row())    # address
            self.rowbox.addWidget(self._build_bytes_row())      # bytes

            # -- results -----------------------------------------------------
            self.results = Q.QTreeWidget()
            self.results.setRootIsDecorated(False)
            self.results.setUniformRowHeights(True)
            self.results.setAlternatingRowColors(True)
            self.results.setHeaderLabels(["Parameter", "Symbol", "Match",
                                          "Address", "File"])
            hd = self.results.header()
            hd.setStretchLastSection(False)
            hd.setSectionResizeMode(2, Q.QHeaderView.ResizeMode.Stretch)
            for i, w in enumerate((300, 220, 0, 90, 160)):
                if w:
                    self.results.setColumnWidth(i, w)
            self.results.setColumnHidden(4, True)
            self.results.setMinimumHeight(90)
            self.results.currentItemChanged.connect(self._picked)
            self.results.itemDoubleClicked.connect(
                lambda it, _c: self.open_in_tab(it))
            self.results.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.results.customContextMenuRequested.connect(self._result_menu)
            self.results.installEventFilter(self)

            self.body = Q.QWidget()
            bl = Q.QVBoxLayout(self.body)
            bl.setContentsMargins(4, 0, 4, 4)
            bl.addWidget(self.rowbox)
            bl.addWidget(self.results, 1)

            lay = Q.QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(2)
            lay.addLayout(top)
            lay.addWidget(self.body, 1)

        @staticmethod
        def _wrap(w):
            box = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(box)
            h.setContentsMargins(2, 0, 2, 0)
            h.addWidget(w)
            h.addStretch(1)
            return box

        def _build_value_row(self):
            Q = QtWidgets
            box = Q.QWidget()
            h = Q.QHBoxLayout(box)
            h.setContentsMargins(2, 0, 2, 0)
            self.chk_raw = Q.QCheckBox("Raw stored value")
            self.chk_raw.setToolTip(
                "Compare the number as stored in the file, before the "
                "parameter's scaling equation (0x... also accepted)")
            self.chk_raw.toggled.connect(lambda v: self._set_opt("raw_values", v))
            self.spin_tol = Q.QDoubleSpinBox()
            self.spin_tol.setDecimals(6)
            self.spin_tol.setRange(0.0, 1e9)
            self.spin_tol.setPrefix("± ")
            self.spin_tol.setToolTip("How close an exact number must be")
            self.spin_tol.valueChanged.connect(
                lambda v: self._set_opt("tolerance", float(v)))
            self.chk_axes_v = Q.QCheckBox("Include axes")
            self.chk_axes_v.toggled.connect(
                lambda v: self._set_opt("include_axes", v))
            for w in (self.chk_raw, Q.QLabel("Tolerance"), self.spin_tol,
                      self.chk_axes_v):
                h.addWidget(w)
            h.addStretch(1)
            return box

        def _build_address_row(self):
            Q = QtWidgets
            box = Q.QWidget()
            h = Q.QHBoxLayout(box)
            h.setContentsMargins(2, 0, 2, 0)
            self.base = Q.QComboBox()
            self.base.addItem("Hex (default)", 16)
            self.base.addItem("Decimal", 10)
            self.base.setToolTip("How a bare number is read. 0x... and $... "
                                 "are always hex.")
            self.base.currentIndexChanged.connect(
                lambda _i: self._set_opt("base", self.base.currentData()))
            h.addWidget(Q.QLabel("Numbers are"))
            h.addWidget(self.base)
            self.addr_note = Q.QLabel(
                "An address inside a table resolves to the exact cell.")
            self.addr_note.setEnabled(False)
            h.addWidget(self.addr_note)
            h.addStretch(1)
            return box

        def _build_bytes_row(self):
            Q = QtWidgets
            box = Q.QWidget()
            h = Q.QHBoxLayout(box)
            h.setContentsMargins(2, 0, 2, 0)
            self.kind = Q.QComboBox()
            for key, label in (("hex", "Hex bytes"), ("ascii", "ASCII text"),
                               ("int", "Integer"), ("float", "Float")):
                self.kind.addItem(label, key)
            self.kind.currentIndexChanged.connect(self._kind_changed)
            self.width = Q.QComboBox()
            self.width.currentIndexChanged.connect(
                lambda _i: self.width.currentData() is not None
                and self._set_opt("width", self.width.currentData()))
            self.chk_signed = Q.QCheckBox("Signed")
            self.chk_signed.toggled.connect(lambda v: self._set_opt("signed", v))
            self.endian = Q.QComboBox()
            self.endian.addItem("Endian: as file", "auto")
            self.endian.addItem("Big", "big")
            self.endian.addItem("Little", "little")
            self.endian.currentIndexChanged.connect(
                lambda _i: self._set_opt("endian", self.endian.currentData()))
            self.chk_axes_b = Q.QCheckBox("Include axes")
            self.chk_axes_b.toggled.connect(
                lambda v: self._set_opt("include_axes", v))
            self.target = Q.QLabel("")
            self.target.setEnabled(False)
            for w in (self.kind, self.width, self.chk_signed, self.endian,
                      self.chk_axes_b, self.target):
                h.addWidget(w)
            h.addStretch(1)
            return box

        def _build_menu(self):
            Q = QtWidgets
            m = self.menu
            self._grp_match = QtGui.QActionGroup(m)
            self._match_acts = {}
            for key, label in (
                    ("fuzzy", "Fuzzy -- words in any order, best first"),
                    ("exact", "Exact text"),
                    ("regex", "Regular expression")):
                a = m.addAction(label)
                a.setCheckable(True)
                a.setActionGroup(self._grp_match)
                a.triggered.connect(lambda _c=False, k=key:
                                    self._set_opt("match", k))
                self._match_acts[key] = a
            m.addSeparator()
            self.act_case = m.addAction("Match case")
            self.act_case.setCheckable(True)
            self.act_case.toggled.connect(
                lambda v: self._set_opt("case_sensitive", v))
            self.act_word = m.addAction("Whole words only")
            self.act_word.setCheckable(True)
            self.act_word.setToolTip("An underscore separates words, so "
                                     "'InjCrv' is a whole word in "
                                     "InjCrv_tPreTrbn_C")
            self.act_word.toggled.connect(
                lambda v: self._set_opt("whole_word", v))
            m.addSection("Search in")
            self._field_acts = {}
            for key, label in FIELD_LABELS:
                a = m.addAction(label)
                a.setCheckable(True)
                a.toggled.connect(lambda _v, k=key: self._field_toggled(k))
                self._field_acts[key] = a
            m.addSection("Group")
            self._grp_tier = QtGui.QActionGroup(m)
            self._tier_acts = {}
            a = m.addAction("Any group")
            a.setCheckable(True)
            a.setActionGroup(self._grp_tier)
            a.triggered.connect(lambda _c=False: self._set_opt("tier", 0))
            self._tier_acts[0] = a
            for t in sorted(S_TIERS()):
                a = m.addAction(self.tier_name(t))
                a.setCheckable(True)
                a.setActionGroup(self._grp_tier)
                a.triggered.connect(lambda _c=False, t=t:
                                    self._set_opt("tier", t))
                self._tier_acts[t] = a
            m.addSeparator()
            m.addAction("Clear search history").triggered.connect(
                self._clear_history)

        # -- restoring what was saved ---------------------------------------
        def _restore(self):
            o = self.opts
            self._quiet = True
            try:
                self.mode.setCurrentIndex(max(0, self.mode.findData(o.mode)))
                i = self.scope.findData(str(self._get(K_SCOPE, "tab")))
                self.scope.setCurrentIndex(max(0, i))
                self.chk_raw.setChecked(o.raw_values)
                self.spin_tol.setValue(o.tolerance)
                self.chk_axes_v.setChecked(o.include_axes)
                self.chk_axes_b.setChecked(o.include_axes)
                self.base.setCurrentIndex(max(0, self.base.findData(o.base)))
                self.kind.setCurrentIndex(
                    max(0, self.kind.findData(o.bytes_kind)))
                self._fill_widths()
                self.chk_signed.setChecked(o.signed)
                self.endian.setCurrentIndex(
                    max(0, self.endian.findData(o.endian)))
            finally:
                self._quiet = False
            self._mode_changed()
            self._refresh_menu()
            if str(self._get(K_COLLAPSED, "0")) == "1":
                self.set_collapsed(True)

        # -- option plumbing --------------------------------------------------
        _quiet = False

        def _set_opt(self, key, value):
            if self._quiet:
                return
            setattr(self.opts, key, value)
            if key == "include_axes":
                self._quiet = True
                self.chk_axes_v.setChecked(bool(value))
                self.chk_axes_b.setChecked(bool(value))
                self._quiet = False
            self._save()
            self._update_summary()
            self._changed()

        def _field_toggled(self, key):
            if self._quiet:
                return
            on = tuple(k for k, _ in FIELD_LABELS
                       if self._field_acts[k].isChecked())
            if not on:                      # never search in nothing
                self._quiet = True
                self._field_acts[key].setChecked(True)
                self._quiet = False
                on = (key,)
            self.opts.fields = on
            self._save()
            self._update_summary()
            self._changed()

        def _refresh_menu(self):
            o = self.opts
            self._quiet = True
            try:
                for k, a in self._match_acts.items():
                    a.setChecked(o.match == k)
                    a.setEnabled(o.mode == "text")
                text = o.mode == "text"
                self.act_case.setChecked(o.case_sensitive)
                self.act_case.setEnabled(
                    text or (o.mode == "bytes" and o.bytes_kind == "ascii"))
                self.act_word.setChecked(o.whole_word)
                self.act_word.setEnabled(text and o.match != "regex")
                for k, a in self._field_acts.items():
                    a.setChecked(k in o.fields)
                    a.setEnabled(text)
                for t, a in self._tier_acts.items():
                    a.setChecked(o.tier == t)
                    a.setEnabled(o.mode != "bytes")
            finally:
                self._quiet = False

        def _update_summary(self):
            o = self.opts
            names = dict(FIELD_LABELS)
            bits = [{"fuzzy": "Fuzzy", "exact": "Exact",
                     "regex": "Regular expression"}[o.match],
                    "match case" if o.case_sensitive else "any case"]
            if o.whole_word and o.match != "regex":
                bits.append("whole words")
            bits.append("in " + ", ".join(names[f].lower() for f in o.fields))
            bits.append("group: %s" % (self.tier_name(o.tier) if o.tier
                                       else "any"))
            self.summary.setText("  ·  ".join(bits))

        def _fill_widths(self):
            kind = self.opts.bytes_kind
            self.width.blockSignals(True)
            self.width.clear()
            if kind == "int":
                for w in (8, 16, 32, 64):
                    self.width.addItem("%d-bit" % w, w)
            elif kind == "float":
                for w in (32, 64):
                    self.width.addItem("%d-bit" % w, w)
            else:
                self.width.addItem("-", None)
            i = self.width.findData(self.opts.width)
            self.width.setCurrentIndex(i if i >= 0 else 0)
            self.width.blockSignals(False)
            numeric = kind in ("int", "float")
            self.width.setEnabled(numeric)
            self.endian.setEnabled(numeric)
            self.chk_signed.setEnabled(kind == "int")

        def _kind_changed(self, _i=0):
            if self._quiet:
                return
            self.opts.bytes_kind = self.kind.currentData()
            self._fill_widths()
            if self.width.currentData() is not None:
                self.opts.width = self.width.currentData()
            self._set_placeholder()
            self._save()
            self._changed()

        def _set_placeholder(self):
            m = self.opts.mode
            key = self.opts.bytes_kind if m == "bytes" else m
            self.edit.setPlaceholderText(PLACEHOLDERS[key])

        # -- mode / scope -----------------------------------------------------
        def _mode_changed(self, _i=0):
            if self._quiet:
                return
            old = self.opts.mode
            if old in S.MODES and hasattr(self, "_shown_mode"):
                self._queries[self._shown_mode] = self.edit.text()
            new = self.mode.currentData()
            self.opts.mode = new
            self._shown_mode = new
            self.rowbox.setCurrentIndex([k for k, _ in MODE_LABELS].index(new))
            self._set_placeholder()
            self._reload_history()
            self._quiet = True
            self.edit.setText(self._queries.get(new, ""))
            self._quiet = False
            self.clear_results()
            self._update_summary()
            self._refresh_target()
            self._save()
            if (new in ("text", "address") and self.edit.text().strip()
                    and self.dock.isVisible()):
                self._live_timer.start()

        def _scope_changed(self, _i=0):
            if self._quiet:
                return
            self.results.setColumnHidden(4, self.scope.currentData() == "tab")
            self._save()
            self._changed(force=True)

        def _update_scope_choices(self):
            ws = self.win.current
            has_cmp = ws is not None and getattr(ws, "comparison", None) is not None
            item = self.scope.model().item(1)
            if item is not None:
                item.setEnabled(bool(has_cmp))
            if not has_cmp and self.scope.currentData() == "pair":
                self.scope.setCurrentIndex(0)

        def _refresh_target(self):
            ws = self.win.current
            e = getattr(getattr(ws, "model", None), "entity", None) if ws else None
            if e is getattr(self, "_target_shown", 0):
                return                          # unchanged; skip the relabel
            self._target_shown = e
            if e is None:
                self.target.setText("Open a table first -- the search looks "
                                    "inside it")
            else:
                self.target.setText("inside:  %s" % (self.title_fn(e) or e.name))

        def _reload_history(self):
            # Rebuilding the list clears and re-sets the line edit, whose
            # textChanged would otherwise read as the person emptying the
            # query -- and clear the results they are looking at.
            text = self.edit.text()
            was, self._quiet = self._quiet, True
            self.query.blockSignals(True)
            try:
                self.query.clear()
                self.query.addItems(self.history.for_mode(self.opts.mode))
                self.query.setEditText(text)
            finally:
                self.query.blockSignals(False)
                self._quiet = was

        def _clear_history(self):
            self.history.clear()
            self._reload_history()
            self._save()

        # -- running ---------------------------------------------------------------
        def _text_changed(self, _t=""):
            if self._quiet:
                return
            self._queries[self.opts.mode] = self.edit.text()
            self._changed()

        def _changed(self, force=False):
            """An input changed. Cheap searches follow the typing; the rest
            wait for Enter or Find."""
            live = self.opts.mode in ("text", "address")
            if not self.edit.text().strip():
                self._cancel_running()
                self.clear_results()
                self._set_status("")
                return
            if live or force and self.results.topLevelItemCount():
                self._live_timer.start()

        def _go_or_stop(self):
            if self._busy:
                self._cancel_running()
                self._set_status("Stopped.")
            else:
                self.run(commit=True)

        def _cancel_running(self):
            self._live_timer.stop()
            if self._cancel is not None:
                self._cancel.set()
            self._gen += 1
            self._set_busy(False)

        def _set_busy(self, busy: bool):
            self._busy = busy
            self.btn_go.setText("Stop" if busy else "Find")

        def _sources(self) -> List[S.Source]:
            scope = self.scope.currentData()
            out: List[S.Source] = []

            def add(ws, with_cmp):
                if ws is None or ws.primary is None:
                    return
                out.append(S.Source(ws.display_name(), ws.primary, ws,
                                    "primary"))
                if with_cmp and ws.comparison is not None:
                    out.append(S.Source(
                        "%s (comparison)" % ws.comparison.binfile.name,
                        ws.comparison, ws, "comparison"))
            if scope == "all":
                for ws in self.win.workspaces():
                    add(ws, False)
            else:
                add(self.win.current, scope == "pair")
            return out

        def _target(self):
            ws = self.win.current
            m = getattr(ws, "model", None) if ws is not None else None
            return getattr(m, "entity", None)

        def run(self, commit: bool = False):
            """Start a search with what is in the panel now."""
            query = self.edit.text()
            self._live_timer.stop()
            if not query.strip():
                self.clear_results()
                self._set_status("")
                return
            self._update_scope_choices()
            sources = self._sources()
            if not sources:
                self._set_status("Open a file to search it.")
                return
            if commit:
                self._select_first = True
            if self._cancel is not None:
                self._cancel.set()
            cancel = S.Cancel()
            self._cancel = cancel
            self._gen += 1
            gen = self._gen
            opts = S.SearchOptions.from_dict(self.opts.to_dict())
            opts.max_results = RESULT_CAP
            target = self._target() if opts.mode == "bytes" else None
            self._ran_for = {"mode": opts.mode, "query": query,
                             "tab": self.win.current}
            self._revs = {id(s.session): s.session.revision for s in sources}
            title_fn, tier_name = self.title_fn, self.tier_name

            def job(cancel, progress):
                eng = _get_engine(title_fn, tier_name)
                hits = eng.run(query, opts, sources, cancel, progress, target)
                if not hits and opts.mode == "address":
                    self._hint = eng.nearest_hint(query, opts, sources)
                else:
                    self._hint = ""
                return hits

            th = _SearchThread(gen, job, cancel)
            th.done.connect(self._on_done)
            th.progressed.connect(self._on_progress)
            _LIVE.add(th)
            th.finished.connect(lambda t=th: _LIVE.discard(t))
            th.finished.connect(th.deleteLater)
            self._set_busy(True)
            self._set_status("Searching…")
            th.start()

        _hint = ""

        def _on_progress(self, gen, done, total):
            if gen == self._gen and total:
                self._set_status("Searching… %d%%" % (100 * done // total))

        def _on_done(self, gen, hits, err):
            if gen != self._gen:
                return                          # a newer search superseded it
            self._set_busy(False)
            if err is not None:
                if err == "cancelled":
                    return
                self.clear_results()
                if isinstance(err, S.QueryError):
                    self._set_status(str(err), error=True)
                else:
                    self._set_status("Search failed: %s" % err, error=True)
                return
            self._fill(hits)
            n = len(hits)
            mode = self.opts.mode
            if n == 0:
                msg = self._hint or {
                    "text": "No parameter matches.",
                    "value": "No parameter holds that value.",
                    "address": "Nothing covers that address.",
                    "bytes": "Pattern not found in this parameter.",
                }[mode]
                self._set_status(msg)
            else:
                if mode == "bytes":
                    noun = "match" if n == 1 else "matches"
                else:
                    noun = "result" if n == 1 else "results"
                self._set_status("%d%s %s" % (
                    n, "+" if n >= RESULT_CAP else "", noun))
            if self._select_first and n:
                self.results.setCurrentItem(self.results.topLevelItem(0))
            self._select_first = False

        def _fill(self, hits):
            self.results.blockSignals(True)
            self.results.setUpdatesEnabled(False)
            try:
                self.results.clear()
                for h in hits:
                    e = h.entity
                    title = ""
                    try:
                        title = self.title_fn(e)
                    except Exception:
                        pass
                    detail = h.detail
                    if self._ran_for and self._ran_for["mode"] == "text":
                        detail = "%s: %s" % (h.where, h.detail)
                    a = h.addr if h.addr is not None else e.addr
                    it = QtWidgets.QTreeWidgetItem([
                        title or e.name, e.name, detail,
                        "0x%X" % a if a is not None else "", h.source.label])
                    it.setData(0, QtCore.Qt.ItemDataRole.UserRole, h)
                    it.setToolTip(2, detail)
                    self.results.addTopLevelItem(it)
            finally:
                self.results.setUpdatesEnabled(True)
                self.results.blockSignals(False)

        def clear_results(self):
            self.results.blockSignals(True)
            self.results.clear()
            self.results.blockSignals(False)
            self._clear_highlight()

        def _set_status(self, text, error=False):
            self.status.setText(text)
            self.status.setToolTip(text)
            pal = self.status.palette()
            pal.setColor(QtGui.QPalette.ColorRole.WindowText,
                         QtGui.QColor("#d9534f") if error
                         else self.palette().color(
                             QtGui.QPalette.ColorRole.WindowText))
            self.status.setPalette(pal)

        # -- picking a result ------------------------------------------------------------
        def step(self, delta: int):
            n = self.results.topLevelItemCount()
            if not n:
                if self.edit.text().strip() and not self._busy:
                    self._select_first = True
                    self.run()
                return
            cur = self.results.currentItem()
            i = self.results.indexOfTopLevelItem(cur) if cur else (
                -1 if delta > 0 else 0)
            self.results.setCurrentItem(
                self.results.topLevelItem((i + delta) % n))

        def _picked(self, cur, _prev):
            if cur is None:
                return
            self._commit_history()
            self._reveal(cur.data(0, QtCore.Qt.ItemDataRole.UserRole),
                         pin=False)

        def open_in_tab(self, item=None):
            item = item or self.results.currentItem()
            if item is None:
                return
            self._commit_history()
            self._reveal(item.data(0, QtCore.Qt.ItemDataRole.UserRole),
                         pin=True)

        def _commit_history(self):
            self.history.push(self.opts.mode, self.edit.text())
            self._reload_history()
            self._save()

        def _result_menu(self, pos):
            it = self.results.itemAt(pos)
            if it is None:
                return
            m = QtWidgets.QMenu(self)
            m.addAction("Show in tree").triggered.connect(
                lambda: self._reveal(it.data(0, QtCore.Qt.ItemDataRole.UserRole),
                                     pin=False))
            m.addAction("Open in new table tab").triggered.connect(
                lambda: self.open_in_tab(it))
            m.exec(self.results.viewport().mapToGlobal(pos))

        def _reveal(self, hit, pin=False):
            ws = hit.source.owner
            if ws is None or ws not in self.win.workspaces():
                self._set_status("That tab has been closed.", error=True)
                return
            e = hit.entity
            if hit.source.role == "comparison":
                e = ws.comparison.counterpart(ws.primary, e)
                if e is None:
                    self._set_status("That parameter has no counterpart in the "
                                     "tab's primary file.", error=True)
                    return
            if self.win.current is not ws:
                self.win.show_workspace(ws)
            if pin:
                ws.open_table(e, pin=True)
            else:
                ws.show_entity(e)
            self._highlight(ws, e, hit)

        def _highlight(self, ws, e, hit):
            self._clear_highlight()
            model = ws.model
            if not hit.cells or model.entity is not e:
                return
            model.set_highlight(hit.cells)
            self._hl_ws = ws
            idx = model.index_of(*hit.cells[0])
            if idx.isValid():
                ws.table.scrollTo(
                    idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

        _hl_ws = None

        def _clear_highlight(self):
            ws, self._hl_ws = self._hl_ws, None
            if ws is not None:
                try:
                    ws.model.set_highlight(())
                except RuntimeError:
                    pass

        # -- keyboard --------------------------------------------------------------------------
        def eventFilter(self, obj, ev):
            if ev.type() == QtCore.QEvent.Type.KeyPress:
                K = QtCore.Qt.Key
                key, mods = ev.key(), ev.modifiers()
                if obj is self.edit:
                    if key in (K.Key_Return, K.Key_Enter):
                        if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
                            self.open_in_tab()
                        elif self.results.topLevelItemCount() \
                                and self._ran_for \
                                and self._ran_for["query"] == self.edit.text() \
                                and self._ran_for["mode"] == self.opts.mode:
                            self.step(-1 if mods & QtCore.Qt.KeyboardModifier
                                      .ShiftModifier else 1)
                        else:
                            self.run(commit=True)
                        return True
                    if key == K.Key_Down:
                        self.step(1)
                        return True
                    if key == K.Key_Up:
                        self.step(-1)
                        return True
                    if key == K.Key_Escape:
                        self.dock.hide()
                        return True
                elif obj is self.results:
                    if key in (K.Key_Return, K.Key_Enter):
                        self.open_in_tab()
                        return True
                    if key == K.Key_Escape:
                        self.edit.setFocus()
                        return True
            return super().eventFilter(obj, ev)

        # -- collapse / close / refresh --------------------------------------------------------------
        def toggle_collapsed(self):
            self.set_collapsed(not self.collapsed)

        def set_collapsed(self, on: bool):
            if on == self.collapsed and self.body.isHidden() == on:
                return
            if on and not self.dock.isFloating():
                self._expanded_h = max(self.dock.height(), 160)
            self.collapsed = on
            self.body.setVisible(not on)
            self.btn_fold.setArrowType(QtCore.Qt.ArrowType.RightArrow if on
                                       else QtCore.Qt.ArrowType.DownArrow)
            if not self.dock.isFloating():
                h = 1 if on else self._expanded_h
                self.win.resizeDocks([self.dock], [h],
                                     QtCore.Qt.Orientation.Vertical)
            self._save()

        def focus_query(self):
            self._update_scope_choices()
            self._refresh_target()
            if self.collapsed:
                self.set_collapsed(False)
            self.edit.setFocus()
            self.edit.selectAll()
            if (self.opts.mode in ("text", "address") and self.edit.text().strip()
                    and not self.results.topLevelItemCount()
                    and not self._busy):
                self._live_timer.start()    # restored query, nothing run yet

        def on_visibility(self, visible: bool):
            if not visible:
                self._clear_highlight()
            elif not self.dock.isFloating() and not self.collapsed:
                self.win.resizeDocks([self.dock], [self._expanded_h],
                                     QtCore.Qt.Orientation.Vertical)

        def on_tab_changed(self):
            self._update_scope_choices()
            self._refresh_target()
            if not self.dock.isVisible() or not self._ran_for:
                return
            scope = self.scope.currentData()
            if scope != "all" and self._ran_for["tab"] is not self.win.current \
                    and self.edit.text().strip():
                if self.opts.mode in ("text", "address"):
                    self._live_timer.start()
                else:
                    self._set_status("These results are for another tab -- "
                                     "press Find to search this one.")

        def _poll(self):
            """Notice when what the results describe has changed under them."""
            if not self.dock.isVisible():
                return
            self._refresh_target()
            ran = self._ran_for
            if (not ran or self._busy
                    or not self.results.topLevelItemCount()
                    or ran["mode"] not in ("value", "bytes")):
                return
            for s in self._sources():
                if self._revs.get(id(s.session), s.session.revision) \
                        != s.session.revision:
                    self._set_status("The file was edited since this search "
                                     "-- press Find to refresh.")
                    return

    def S_TIERS():
        from .tiers import TIER_NAMES
        return TIER_NAMES

    class SearchDock(QtWidgets.QDockWidget):
        """The dock the main window owns."""

        def __init__(self, win, title_fn, tier_name, settings):
            super().__init__("Find", win)
            self.setObjectName("FindDock")
            self.setFeatures(
                QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.panel = SearchPanel(win, self, title_fn, tier_name, settings)
            self.setWidget(self.panel)
            win.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea,
                              self)
            self.visibilityChanged.connect(self.panel.on_visibility)
            self._on_screen = False
            self.visibilityChanged.connect(
                lambda v: setattr(self, "_on_screen", bool(v)))
            win.tabs.currentChanged.connect(
                lambda _i: self.panel.on_tab_changed())
            self.hide()                 # opened with Ctrl+F

        def open(self):
            """Show the panel, restore whatever was in it, focus the query."""
            self.show()
            self.raise_()
            self.panel.focus_query()

        def showing(self) -> bool:
            """On screen: shown, and not behind another dock tabbed with
            it (``isVisible`` is still True there: Qt only says so with
            ``visibilityChanged``)."""
            return not self.isHidden() and self._on_screen

        def toggle(self):
            """The toolbar's Find button: open the panel, or close it when
            it is already on screen (behind another tabbed dock: raise it)."""
            if self.showing():
                self.hide()
            else:
                self.open()

        def toggle_action(self) -> QtGui.QAction:
            """The toolbar's Find button (Edit > Find and Ctrl+F always
            open): checkable, pressed while the panel is on screen."""
            a = self.__dict__.get("_toggle_act")
            if a is None:
                a = QtGui.QAction("Find panel", self)
                a.setCheckable(True)
                a.setToolTip("Show or hide the Find panel")
                # (triggered flips the check mark first: put it right, the
                # visibility signal then keeps it in step)
                a.triggered.connect(lambda _on: self.toggle())
                a.triggered.connect(lambda _on: a.setChecked(self.showing()))
                self.visibilityChanged.connect(a.setChecked)
                a.setChecked(self.showing())
                self.__dict__["_toggle_act"] = a
            return a

        def step(self, delta):
            if not self.isVisible():
                self.open()
            self.panel.step(delta)

        def open_in_definition(self):
            """Edit > Find in definition: the panel searching the parameters
            of the tab's definition file only (the "From the definition
            file" group -- an imported WinOLS project's maps, an attached
            XDF / A2L / DAMOS), by name, symbol or description."""
            from .groups import DEFINITION
            p = self.panel
            p.mode.setCurrentIndex(max(0, p.mode.findData("text")))
            act = p._tier_acts.get(DEFINITION)
            if act is not None:
                act.setChecked(True)
            p._set_opt("tier", DEFINITION)
            self.open()

    def install(win, title_fn: Callable, tier_name: Callable,
                settings: Callable) -> "SearchDock":
        """Create the Find dock on ``win`` (hidden until opened)."""
        return SearchDock(win, title_fn, tier_name, settings)
