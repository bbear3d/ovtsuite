# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The fault-code report window: what a fault-memory read found.

Shown by the **Fault codes from ECU** window (``datalog.gui_log.
EcuCodesDialog``) after each read or clear; also opened from the Project
panel for a read kept in the project. It does not block the app (the
assistant panel can answer Ask AI beside it), one per tab or set: a new read
replaces the report still open (:func:`present`). Its "Show in fault paths"
hands the codes to the fault-path editor that asked for the read
(``ReportContext.on_show_paths``). The records, text, CSV, history and the questions it asks are
:mod:`ovtsuite.dtc_report`'s.

The two per-code actions are small module functions (:func:`search_web`,
:func:`ask_ai`), so either can be swapped without touching the window.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import dtc_report as R
from .gui_cellmarks import OFF_FG, CellDelegate, mark_differs

#: QSettings key of the last site "Search the web" used (a SearchSite id)
SEARCH_KEY = "dtc_report/search_site"

try:                                            # not in every PySide6 build
    from PySide6 import QtPrintSupport as _QP   # noqa: F401
    HAVE_PRINT = True
except Exception:                               # pragma: no cover
    HAVE_PRINT = False


class ReportContext:
    """What the window that asked for the read knows.

    ``ws``: the tab of the open file (project, AI handle); ``paths_for(code)``
    / ``describe(code, paths)``: the fault-path lookups of that window
    (:func:`ovtsuite.dtc_report.build_read`); ``part``: the file's ECU part
    number, for web searches; ``engine``: an engine ECU (the AI's
    fault_codes tools apply); ``encoding``: how the file's fault paths
    store their codes (``dtc_report.ENC_SAE`` / ``ENC_VAG``, "" unknown) --
    the ECU reports the same words. ``on_show_paths()``: called by the
    report's "Show in fault paths" (the editor switches to the ECU's codes);
    the report does not block, so this comes after the read window closed."""

    def __init__(self, ws=None, paths_for=None, describe=None, part: str = "",
                 engine: bool = True, file: str = "", encoding: str = "",
                 on_show_paths: Optional[Callable[[], None]] = None):
        self.ws = ws
        self.paths_for = paths_for
        self.describe = describe
        self.part = part
        self.engine = engine
        self.file = file
        self.encoding = encoding
        self.on_show_paths = on_show_paths

    def project_ref(self):
        return getattr(self.ws, "project_ref", None)

    def set_name(self) -> str:
        ref = self.project_ref()
        if ref is None:
            return ""
        try:
            return ref.doc.project.set(ref.sid).name
        except Exception:
            return ""


def intermittent_tip(c: R.CodeEntry) -> str:
    """The Intermittent column's tooltip: what the tick means for the
    protocol the code was read with."""
    si = c.status_info
    if si.proto == R.PROTO_KW1281:
        return ("Sporadic (status bit 7, the scan tool's -10)" if si.intermittent
                else "Not sporadic (status bit 7 clear)")
    return ("Stored, not present at the time of the read (status bit 6 clear)"
            if si.intermittent else "Present at the time of the read (status bit 6)")


def file_part(session) -> str:
    """The open file's ECU part number, best effort ('' if none found)."""
    try:
        from . import fileinfo
        return fileinfo.ident16(bytes(session.binfile.data)).get("part", "") or ""
    except Exception:
        return ""


# ------------------------------------------------------------------ settings
def search_choice() -> R.SearchSite:
    """The site last searched (Google at first)."""
    try:
        from .appsettings import settings
        return R.search_site(str(settings().value(SEARCH_KEY, "") or ""))
    except Exception:
        return R.SEARCH_SITES[0]


def set_search_choice(site_id: str) -> None:
    try:
        from .appsettings import settings
        settings().setValue(SEARCH_KEY, R.search_site(site_id).id)
    except Exception:
        pass


# ------------------------------------------------------------------ actions
def search_web(parent, entries: Sequence[R.CodeEntry], read: R.Read,
               ctx: Optional[ReportContext], site: Optional[R.SearchSite] = None) -> str:
    """Open the user's browser on ``site`` (the last one chosen) searching
    for ``entries``; returns the URL. Nothing is sent anywhere by the app:
    the browser asks the site."""
    part = R.part_number(read.ecu_ident) or (ctx.part if ctx is not None else "")
    url = R.site_url(site or search_choice(), entries, part)
    QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
    return url


def ai_available() -> Tuple[bool, str]:
    """``(usable, why not)`` for the AI assistant."""
    try:
        from . import gui_ai
        if not getattr(gui_ai, "HAVE_QT", False):
            return False, "The AI assistant is not part of this build."
        from .ai import config as _C
        if not _C.load().get("enabled"):
            return False, ("The AI assistant is off. Turn it on, and choose a service, "
                           "in the assistant panel's AI settings.")
    except Exception as ex:                                 # noqa: BLE001
        return False, "The AI assistant is not available: %s" % ex
    return True, ""


def _assistant(parent, ctx):
    """``(gui_ai, ws, handle)`` for a question, or None (a message says why)."""
    ok, why = ai_available()
    ws = ctx.ws if ctx is not None else None
    if ok and ws is None:
        ok, why = False, "Open the file in a tab to ask the assistant about its codes."
    if not ok:
        QtWidgets.QMessageBox.information(parent, "Fault code report", why)
        return None
    try:
        from . import gui_ai
        lw = gui_ai.assistant_dock(ws.win)._workspace()
        h = lw.tab_for(ws)
    except Exception as ex:                                 # noqa: BLE001
        QtWidgets.QMessageBox.information(
            parent, "Fault code report", "The AI assistant is not available: %s" % ex)
        return None
    if h is None:
        # the report does not block: its tab may have been closed since
        QtWidgets.QMessageBox.information(
            parent, "Fault code report",
            "The file's tab has been closed. Open it again to ask the assistant "
            "about its codes.")
        return None
    return gui_ai, ws, h


def ask_ai(parent, entries: Sequence[R.CodeEntry], read: R.Read,
           ctx: Optional[ReportContext]) -> Optional[str]:
    """Hand the assistant a question about ``entries`` (the pattern of
    ``gui_featureswitch``'s Ask AI); returns the question, None when the
    assistant cannot take it (a message says why)."""
    got = _assistant(parent, ctx)
    if got is None:
        return None
    gui_ai, ws, h = got
    q = R.ai_question(entries, h, read, engine=ctx.engine)
    gui_ai.show_assistant(ws.win, q)
    return q


def ask_ai_report(parent, read: R.Read, reads: Sequence[R.Read],
                  ctx: Optional[ReportContext]) -> Optional[str]:
    """Hand the assistant the whole report with the set's read history
    (``reads``, newest first), asking it to interpret the pattern."""
    got = _assistant(parent, ctx)
    if got is None:
        return None
    gui_ai, ws, h = got
    q = R.ai_report_question(read, reads, h, engine=ctx.engine)
    gui_ai.show_assistant(ws.win, q)
    return q


# ------------------------------------------------------------------ project
def keep_read(ctx: Optional[ReportContext], read: R.Read):
    """Keep ``read`` in the project of the context's tab; the attachment, or
    None (no project open)."""
    ref = ctx.project_ref() if ctx is not None else None
    if ref is None:
        return None
    a = R.keep(ref.doc.project, ref.sid, read)
    read.kept = ref.doc.title
    win = getattr(ctx.ws, "win", None)
    if win is not None:
        try:
            from . import gui_project as _gp
            _gp.refresh_panel(win)
            _gp.autosave_soon(win, ref.doc)
        except Exception:
            pass
    return a


def history_of(ctx: Optional[ReportContext]) -> List[Tuple[object, R.Read]]:
    ref = ctx.project_ref() if ctx is not None else None
    if ref is None:
        return []
    try:
        return R.history(ref.doc.project, ref.sid)
    except Exception:
        return []


# ------------------------------------------------------------------ window
COLS = ("VAG code", "OBD-II", "Description", "Status", "Fault type", "Intermittent",
        "Fault paths", "Change")


class FaultCodeReport(QtWidgets.QDialog):
    """The report of one read, with the earlier reads of the same set.

    ``items``: ``(attachment or None, Read)``, newest first; ``index`` the
    one shown. ``after_read``: opened by a read (the default button then
    hands the codes to the fault-path editor; Close keeps them out of its
    view)."""

    search_action: Callable = staticmethod(search_web)
    ai_action: Callable = staticmethod(ask_ai)
    ai_report_action: Callable = staticmethod(ask_ai_report)

    def __init__(self, items: Sequence[Tuple[object, R.Read]], ctx: Optional[ReportContext] = None,
                 parent=None, index: int = 0, after_read: bool = True, kept_note: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Fault code report")
        self.resize(980, 520)
        self.items = list(items)
        self.ctx = ctx
        self.after_read = after_read
        self.kept_note = kept_note
        self.read: R.Read = self.items[index][1]
        self.show_paths = False
        v = QtWidgets.QVBoxLayout(self)

        self.header = QtWidgets.QLabel()
        self.header.setWordWrap(True)
        self.header.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.header)

        h = QtWidgets.QHBoxLayout()
        h.addWidget(QtWidgets.QLabel("History:"))
        self.history = QtWidgets.QComboBox()
        self.history.setToolTip("Earlier reads of this set kept in the project, newest "
                                "first. Codes new since the read before are marked "
                                "\"new\", codes it had that are gone \"gone\".")
        for i, (att, r) in enumerate(self.items):
            text = "%s -- %s" % (r.when[:16], r.summary())
            if i == 0 and after_read:
                text = "This read: " + text
            self.history.addItem(text)
        self.history.setCurrentIndex(index)
        self.history.currentIndexChanged.connect(self._show)
        h.addWidget(self.history, 1)
        v.addLayout(h)
        self.kept = QtWidgets.QLabel()
        self.kept.setStyleSheet("color: gray")
        self.kept.setWordWrap(True)
        v.addWidget(self.kept)

        self.empty = QtWidgets.QLabel()
        f = self.empty.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.empty.setFont(f)
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty, 1)

        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(CellDelegate(self.table))
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        v.addWidget(self.table, 4)

        acts = QtWidgets.QHBoxLayout()
        self.search_btn = QtWidgets.QToolButton()
        self.search_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        m = QtWidgets.QMenu(self.search_btn)
        self.site_actions = {}
        for site in R.SEARCH_SITES:
            a = m.addAction(site.label)
            a.triggered.connect(lambda _=False, sid=site.id: self._search(sid))
            self.site_actions[site.id] = a
        self.search_btn.setMenu(m)
        self.search_btn.clicked.connect(lambda: self._search())
        acts.addWidget(self.search_btn)
        self.ai_btn = QtWidgets.QPushButton("Ask AI")
        self.ai_btn.clicked.connect(self._ask)
        acts.addWidget(self.ai_btn)
        acts.addStretch(1)
        self.ai_report_btn = QtWidgets.QPushButton("Ask AI about this report")
        self.ai_report_btn.clicked.connect(self._ask_report)
        acts.addWidget(self.ai_report_btn)
        v.addLayout(acts)
        self._tooltips()

        b = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the report as text")
        self.copy_btn.clicked.connect(self._copy)
        b.addWidget(self.copy_btn)
        self.save_btn = QtWidgets.QPushButton("Save as...")
        self.save_btn.setToolTip("Save the report as a text or CSV file")
        self.save_btn.clicked.connect(self._save)
        b.addWidget(self.save_btn)
        self.print_btn = None
        if HAVE_PRINT:
            self.print_btn = QtWidgets.QPushButton("Print...")
            self.print_btn.clicked.connect(self._print)
            b.addWidget(self.print_btn)
        b.addStretch(1)
        self.show_btn = QtWidgets.QPushButton("Show in fault paths")
        self.show_btn.setToolTip("Close the report and highlight the codes in the "
                                 "fault-path list")
        self.show_btn.clicked.connect(self._show_in_paths)
        self.show_btn.setVisible(after_read)
        b.addWidget(self.show_btn)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        b.addWidget(self.close_btn)
        v.addLayout(b)
        for w in (self.copy_btn, self.save_btn, self.close_btn, self.print_btn):
            if w is not None:
                w.setAutoDefault(False)
        (self.show_btn if after_read else self.close_btn).setDefault(True)
        self._show(index)

    # -- showing ----------------------------------------------------------
    def previous(self) -> Optional[R.Read]:
        """The read before the one shown (for new / gone)."""
        i = self.history.currentIndex()
        return self.items[i + 1][1] if 0 <= i < len(self.items) - 1 else None

    def _tooltips(self):
        site = search_choice()
        self.search_btn.setText("Search %s" % site.label)
        self.search_btn.setToolTip(
            "Opens your web browser on %s, searching for the selected code(s) -- all "
            "codes when none is selected. The app sends nothing anywhere; your browser "
            "asks the site.\nThe arrow picks another site (remembered)." % site.label)
        ok, why = ai_available()
        self.ai_btn.setToolTip(
            "Hand the assistant a question about the selected code(s): likely causes, "
            "checks to make on the car, and whether disabling the fault path is "
            "appropriate. The report stays open beside the assistant." if ok else why)
        self.ai_report_btn.setToolTip(
            "Hand the assistant the whole report with this set's earlier reads (codes "
            "new, gone, recurring or back after a clear) and ask it to interpret the "
            "pattern. The report stays open beside the assistant." if ok else why)

    def _show(self, i: int):
        self.read = self.items[i][1]
        r, prev = self.read, self.previous()
        new, gone = R.diff(prev, r)
        lines = R.header_lines(r)
        self.header.setText("\n".join(lines))
        att = self.items[i][0]
        if i == 0 and self.after_read:
            self.kept.setText(self.kept_note or (
                "Kept in project %s (Project panel > Attachments)." % r.kept if r.kept else
                "Not kept: no project is open for this file, so this read is not in any "
                "history."))
        else:
            self.kept.setText("A read kept in the project%s." % (
                " (%s)" % att.filename if att is not None else ""))
        rows = [(c, "new" if c.code in new else "") for c in r.codes]
        if prev is not None:
            rows += [(c, "gone") for c in prev.codes if c.code in gone]
        self.empty.setText(
            "" if r.codes else
            ("Codes cleared -- the ECU now reports no stored fault codes."
             if r.action == "clear" else "No fault codes stored"))
        self.empty.setVisible(not r.codes)
        self.table.setVisible(bool(rows))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row, (c, change) in enumerate(rows):
            self._fill(row, c, change)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._sel_changed()

    def _fill(self, row: int, c: R.CodeEntry, change: str):
        cells = [c.vag, c.pcode or "-", c.desc or "", c.status_code, c.fault_type, "",
                 c.paths_text(), change]
        for col, text in enumerate(cells):
            it = QtWidgets.QTableWidgetItem(text)
            it.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, (c.code, change))
            if col in (3, 4):
                si = c.status_info
                it.setToolTip("\n".join(x for x in (si.text, si.note) if x))
            if col == 5:
                it.setData(QtCore.Qt.ItemDataRole.CheckStateRole,
                           QtCore.Qt.CheckState.Checked if c.intermittent
                           else QtCore.Qt.CheckState.Unchecked)
                it.setToolTip(intermittent_tip(c))
            if col == 6:
                tips = ["%s: %s" % (p.name, "enabled" if p.enabled else "disabled")
                        for p in c.paths]
                if c.matched_known and not c.paths:
                    mark_differs(it)
                    tips = ["No fault path of the open file sets this code."]
                it.setToolTip("\n".join(tips))
            if change == "gone":
                it.setForeground(QtGui.QBrush(OFF_FG))
                it.setToolTip("Stored at the read before, not in this one")
            elif change == "new" and col == 7:
                mark_differs(it)
                it.setToolTip("Not stored at the read before")
            self.table.setItem(row, col, it)

    # -- selection ----------------------------------------------------------
    def selected_entries(self) -> List[R.CodeEntry]:
        """The selected codes of the read shown; every code when none is."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        by = {c.code: c for c in self.read.codes}
        prev = self.previous()
        if prev is not None:
            for c in prev.codes:
                by.setdefault(c.code, c)
        out = []
        for r in rows:
            it = self.table.item(r, 0)
            code = it.data(QtCore.Qt.ItemDataRole.UserRole)[0] if it is not None else None
            if code in by and by[code] not in out:
                out.append(by[code])
        return out or list(self.read.codes)

    def _sel_changed(self):
        has = bool(self.read.codes) or bool(self.table.selectedIndexes())
        self.search_btn.setEnabled(has)
        self.ai_btn.setEnabled(has)

    def _menu(self, pos):
        m = QtWidgets.QMenu(self)
        sub = m.addMenu("Search the web")
        for site in R.SEARCH_SITES:
            sub.addAction(site.label, lambda sid=site.id: self._search(sid))
        m.addAction("Ask AI", self._ask)
        m.addAction("Ask AI about this report", self._ask_report)
        m.addSeparator()
        m.addAction("Copy report", self._copy)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # -- actions ------------------------------------------------------------
    def _search(self, site_id: Optional[str] = None):
        """Search the selected codes on ``site_id`` (then remembered), else
        on the site used last."""
        if site_id is not None:
            set_search_choice(site_id)
            self._tooltips()
        es = self.selected_entries()
        if es:
            site = R.search_site(site_id) if site_id else search_choice()
            return type(self).search_action(self, es, self.read, self.ctx, site)
        return None

    def _ask(self):
        es = self.selected_entries()
        if not es:
            return None
        # the report does not block: it stays open beside the assistant
        return type(self).ai_action(self, es, self.read, self.ctx)

    def reads_from_shown(self) -> List[R.Read]:
        """The read shown and the ones before it, newest first."""
        i = max(0, self.history.currentIndex())
        return [r for _a, r in self.items[i:]]

    def _ask_report(self):
        return type(self).ai_report_action(self, self.read, self.reads_from_shown(),
                                           self.ctx)

    def text(self) -> str:
        return R.to_text(self.read, self.previous())

    def _copy(self):
        QtWidgets.QApplication.clipboard().setText(self.text())

    def default_name(self) -> str:
        return "fault-codes-%s" % (self.read.ts or "").replace(":", "").replace("T", "-")

    def _save(self):
        try:
            from .recent import get_last_dir
            start = get_last_dir("report")
        except Exception:
            start = ""
        path, flt = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save fault code report", os.path.join(start, self.default_name() + ".txt"),
            "Text (*.txt);;CSV (*.csv)")
        if not path:
            return None
        return self.save_to(path, csv=path.lower().endswith(".csv") or "CSV" in (flt or ""))

    def save_to(self, path: str, csv: bool = False) -> str:
        if csv and not path.lower().endswith(".csv"):
            path = os.path.splitext(path)[0] + ".csv"
        data = R.to_csv(self.read, self.previous()) if csv else self.text()
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        try:
            from .recent import set_last_dir
            set_last_dir(path, "report")
        except Exception:
            pass
        return path

    def _print(self):
        from PySide6 import QtPrintSupport
        printer = QtPrintSupport.QPrinter()
        dlg = QtPrintSupport.QPrintDialog(printer, self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        doc = QtGui.QTextDocument()
        doc.setDefaultFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))
        doc.setPlainText(self.text())
        doc.print_(printer)

    def _show_in_paths(self):
        self.show_paths = True
        cb = getattr(self.ctx, "on_show_paths", None)
        if cb is not None:
            try:
                cb()
            except RuntimeError:            # the editor was closed meanwhile
                pass
        self.accept()


#: the reports shown without blocking, by :func:`report_key` -- the
#: reference that keeps each alive until it closes
_OPEN: dict = {}


def report_key(ctx: Optional[ReportContext], doc=None, sid=None):
    """Which open report a new one replaces: the same set of a project, else
    the same tab (or the one window without either)."""
    ref = ctx.project_ref() if ctx is not None else None
    if ref is not None:
        doc, sid = ref.doc, ref.sid
    if doc is not None and sid is not None:
        return ("set", id(doc), sid)
    ws = ctx.ws if ctx is not None else None
    return ("tab", id(ws)) if ws is not None else ("none",)


def open_report(key) -> Optional[FaultCodeReport]:
    """The report open for ``key``, or None."""
    d = _OPEN.get(key)
    try:
        if d is not None and d.isVisible():
            return d
    except RuntimeError:                    # deleted by Qt
        pass
    _OPEN.pop(key, None)
    return None


def present(dlg: FaultCodeReport, key) -> FaultCodeReport:
    """Show ``dlg`` without blocking (non-modal), in place of the report
    open for ``key`` (at its place on screen), kept referenced until it
    closes."""
    old = open_report(key)
    if old is not None and old is not dlg:
        geo = old.geometry()
        old.close()
        dlg.setGeometry(geo)
    _OPEN[key] = dlg

    def _gone(*_a, k=key, d=dlg):
        if _OPEN.get(k) is d:
            del _OPEN[k]
    dlg.finished.connect(_gone)
    dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    dlg.show()
    _front(dlg)
    # shown from the read window, which closes right after: the window it
    # belongs to (the fault-path editor) is activated then and would cover
    # the report -- it comes forward again once that has happened
    QtCore.QTimer.singleShot(0, dlg, lambda d=dlg: _front(d))
    return dlg


def _front(dlg) -> None:
    try:
        if dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
    except RuntimeError:                    # deleted by Qt
        pass


def bring_to_front(win, ws=None) -> None:
    """Show ``win`` (a tab's tool window, the fault-path editor) to the user
    for the report's "Show in fault paths": its tab ``ws`` made the current
    one when it is not (the main window then brings back the tab's tool
    windows), the main window and ``win`` restored when minimised, ``win``
    shown when hidden, raised and activated."""
    try:
        main = getattr(ws, "win", None)
        tabs = getattr(main, "tabs", None)
        if isinstance(tabs, QtWidgets.QTabWidget) and isinstance(ws, QtWidgets.QWidget)                 and tabs.indexOf(ws) >= 0 and tabs.currentWidget() is not ws:
            tabs.setCurrentWidget(ws)
        if isinstance(main, QtWidgets.QWidget) and main.isMinimized():
            main.showNormal()
        if win.isMinimized():
            win.showNormal()
        elif not win.isVisible():
            win.show()
        win.raise_()
        win.activateWindow()
    except RuntimeError:                    # closed meanwhile
        pass


def _owner(parent, ctx):
    """The report's parent: the main window of the file's tab, else the top
    window above ``parent`` (the read window itself closes on the result)."""
    win = getattr(getattr(ctx, "ws", None), "win", None)
    if isinstance(win, QtWidgets.QWidget):
        return win
    top = parent.parentWidget() if isinstance(parent, QtWidgets.QWidget) else None
    while top is not None and top.parentWidget() is not None:
        top = top.parentWidget()
    return top


def show_after_read(parent, read: R.Read, ctx: Optional[ReportContext]) -> FaultCodeReport:
    """Keep ``read`` in the project (when one is open) and show its report.
    It does not block (:func:`present`): ``show_paths`` is still False when
    this returns; the report's "Show in fault paths" calls the context's
    ``on_show_paths`` later."""
    try:
        att = keep_read(ctx, read)
    except Exception as ex:                                 # noqa: BLE001
        att, read.kept = None, ""
        note = "Could not keep this read in the project: %s" % ex
    else:
        note = ""
    items = [(a, read if a is att else r)          # this read as made, not as stored
             for a, r in (history_of(ctx) if att is not None else [])]
    if att is None or not items or items[0][0] is not att:
        items = [(att, read)] + [x for x in items if x[0] is not att]
    dlg = FaultCodeReport(items, ctx, _owner(parent, ctx), kept_note=note)
    return present(dlg, report_key(ctx))


def open_kept(win, doc, aid: str) -> bool:
    """The Project panel: show a kept read (with its set's history). False
    when ``aid`` is not a fault-code read."""
    a = next((x for x in doc.project.attachments if x.id == aid), None)
    if a is None or not R.is_read(a):
        return False
    items = R.history(doc.project, a.set)
    index = next((i for i, (x, _r) in enumerate(items) if x is a), None)
    if index is None:
        return False
    ws = next((t for t in doc.tabs(win) if t.project_ref.sid == a.set), None)
    ctx = ReportContext(ws=ws)
    dlg = FaultCodeReport(items, ctx, win, index=index, after_read=False)
    present(dlg, report_key(ctx, doc, a.set))
    return True
