# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""AI in the window: sharing open tabs with AI apps, and reviewing what they
propose. See docs/AI_PLAN.md.

* **Tools > AI > Share open tabs with AI apps** (off until turned on): runs
  the live bridge (``ovtsuite.ai.bridge``) so an MCP client started with
  ``ovtsuite mcp`` / ``OVTMcp`` sees the open tabs, the selected parameter and
  unsaved edits. Without it the MCP server still works on files from disk.
* **Tools > AI > Proposals...**: what AI apps have proposed. Each one can be
  previewed as the tab's comparison (the grid marks every changed cell),
  accepted -- applied as one undo step and, on a project tab with no other
  unsaved edits, saved as its own version "AI: <title>" -- or rejected.
* A status-bar button shows the number of pending proposals for open tabs.
* **Tools > AI > Settings...**: the service the app's own assistant uses
  (a local Ollama / LM Studio model by default -- free, offline; or Claude,
  OpenAI, Gemini, Groq, OpenRouter with the user's key), the key (kept in the
  OS keychain), the model, redaction, offline fallback, token use.
* **Tools > AI > What was sent...**: every request sent to an AI service this
  session, exactly as sent (never the key).

Nothing an AI does reaches a binary except through Accept here.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

from . import branding

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    HAVE_QT = True
except Exception:                                   # pragma: no cover
    HAVE_QT = False

#: QSettings key: share open tabs with AI apps (opt-in)
SHARE_KEY = "ai/share_tabs"
POLL_MS = 2000
#: the inbox is watched (QFileSystemWatcher); a full look this often anyway,
#: for file systems that do not report changes
FALLBACK_POLL_S = 30

if HAVE_QT:
    from .ai import proposals as P

    def _settings():
        from .appsettings import settings
        return settings()

    def share_enabled() -> bool:
        v = _settings().value(SHARE_KEY, False)
        return v in (True, "true", "1", 1)

    # ------------------------------------------------------------ GUI thread
    class _GuiRunner(QtCore.QObject):
        """Runs a callable on the GUI thread for the bridge's worker threads
        and hands back its result (or exception)."""

        job = QtCore.Signal(object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.job.connect(self._exec, QtCore.Qt.ConnectionType.QueuedConnection)

        @QtCore.Slot(object)
        def _exec(self, job):
            fn, ev, box = job
            try:
                box["r"] = fn()
            except BaseException as ex:               # handed back to the caller
                box["e"] = ex
            finally:
                ev.set()

        def __call__(self, fn):
            if QtCore.QThread.currentThread() is self.thread():
                return fn()
            ev, box = threading.Event(), {}
            self.job.emit((fn, ev, box))
            if not ev.wait(600):
                raise TimeoutError("the window did not answer")
            if "e" in box:
                raise box["e"]
            return box.get("r")

    # ------------------------------------------------------------ state
    class AiState(QtCore.QObject):
        """Per window: the bridge, the status button, the proposals dialog."""

        def __init__(self, win):
            super().__init__(win)
            self.win = win
            self.bridge = None
            self.workspace = None
            self.runner = _GuiRunner(self)
            self.dialog = None
            self.last_activity = None             # (client, tool, time)
            self._stamp = None
            self._count = 0
            btn = self.button = QtWidgets.QToolButton()
            btn.setAutoRaise(True)
            btn.clicked.connect(self.open_proposals)
            btn.hide()
            win.statusBar().addPermanentWidget(btn)
            # the button's "working" line ages on this; the inbox itself is
            # watched, and only looked through when it changes
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(POLL_MS)
            self.timer.timeout.connect(self._tick)
            self.timer.start()
            self._last_full = 0.0
            self._recount = QtCore.QTimer(self)
            self._recount.setSingleShot(True)
            self._recount.setInterval(250)        # a burst of changes: one look
            self._recount.timeout.connect(self.poll)
            self.watcher = None
            try:
                self.watcher = QtCore.QFileSystemWatcher([P.inbox_dir()], self)
                self.watcher.directoryChanged.connect(lambda *_: self._recount.start())
            except Exception:
                self.watcher = None
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.stop_bridge)
            win.destroyed.connect(lambda *_: self.stop_bridge())
            if share_enabled():
                self.start_bridge(quiet=True)
            self.poll()

        # -- bridge ------------------------------------------------------------
        def start_bridge(self, quiet=False) -> bool:
            if self.bridge is not None:
                return True
            from .ai.bridge import BridgeServer
            from .ai.live import LiveWorkspace
            try:
                self.workspace = LiveWorkspace(self.win)
                self.workspace.ui = WindowUI(self.win)
                self.bridge = BridgeServer(self.workspace, run_on_gui=self.runner,
                                           on_activity=self._activity)
                self.bridge.start()
            except Exception as ex:
                self.bridge = None
                if not quiet:
                    QtWidgets.QMessageBox.warning(
                        self.win, "AI apps",
                        "Could not start sharing with AI apps:\n%s" % ex)
                return False
            self._update_button()
            return True

        def stop_bridge(self):
            b, self.bridge = self.bridge, None
            if b is not None:
                try:
                    b.stop()
                except Exception:
                    pass
            try:
                self._update_button()
            except RuntimeError:
                pass                              # the window is going

        def set_share(self, on: bool):
            if on and not confirm_notice(self.win):
                return                      # the notice was declined: stay off
            _settings().setValue(SHARE_KEY, bool(on))
            if on:
                self.start_bridge()
            else:
                self.stop_bridge()

        def _activity(self, client, tool):
            self.last_activity = (client, tool, time.time())  # bridge thread: no Qt

        # -- polling -----------------------------------------------------------
        def identities(self):
            """(tab, identity) for every open tab with a binary."""
            from .ai.live import TabTarget
            out = []
            for ws in self.win.workspaces():
                if getattr(ws, "primary", None) is None:
                    continue
                try:
                    out.append((ws, TabTarget("", ws).identity()))
                except Exception:
                    continue
            return out

        def pending_for_tabs(self):
            ids = self.identities()
            out = []
            for p in P.all_pending():
                for ws, ident in ids:
                    ok, note = P.match(p, ident)
                    if ok:
                        out.append((p, ws, note))
                        break
            return out

        def _tick(self):
            now = time.time()
            if self.watcher is None or now - self._last_full >= FALLBACK_POLL_S:
                self.poll()
            else:
                self._update_button()

        def poll(self):
            self._last_full = time.time()
            try:
                st = P.stamp()
            except Exception:
                st = 0.0
            if st != self._stamp:
                self._stamp = st
                try:
                    self._count = len(self.pending_for_tabs())
                except Exception:
                    self._count = 0
                if self.dialog is not None and self.dialog.isVisible():
                    self.dialog.reload()
            self._update_button()

        def _update_button(self):
            btn = self.button
            live = self.bridge is not None
            act = self.last_activity
            recent = act is not None and time.time() - act[2] < 8
            if self._count:
                btn.setText("AI: %d proposal%s" % (self._count, "" if self._count == 1 else "s"))
            elif recent:
                btn.setText("AI: %s working" % act[0])
            elif live:
                btn.setText("AI: sharing tabs")
            else:
                btn.setText("")
            tip = ["Proposals from AI apps wait here for you to accept or reject."]
            if live:
                tip.append("Open tabs are shared with AI apps (Tools > AI).")
            if act is not None:
                tip.append("Last request: %s by %s, %s." % (
                    act[1], act[0], time.strftime("%H:%M:%S", time.localtime(act[2]))))
            btn.setToolTip("\n".join(tip))
            btn.setVisible(bool(btn.text()))

        # -- dialogs -----------------------------------------------------------
        def open_proposals(self):
            if self.dialog is None:
                self.dialog = ProposalsDialog(self)
            self.dialog.reload()
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()

        def open_connect(self):
            ConnectDialog(self).exec()

    def state(win) -> "AiState":
        st = win.__dict__.get("_ai_state")
        if st is None:
            st = win.__dict__["_ai_state"] = AiState(win)
        return st

    def install(win) -> "AiState":
        """Called once by the main window."""
        st = state(win)
        try:
            add_find_button(win)
        except Exception:
            pass
        assistant_dock(win)                 # the panel on the right (folded at first)
        try:
            _add_help_item(win)
        except Exception:
            pass
        return st

    def _add_help_item(win):
        """Help > Ask the AI about the app..."""
        for act in win.menuBar().actions():
            m = act.menu()
            if m is not None and act.text().replace("&", "") == "Help":
                first = m.actions()[0] if m.actions() else None
                a = QtGui.QAction("Ask the AI about the app...", win)
                a.setToolTip("Questions about using %s, answered from its help pages"
                             % branding.APP_NAME)
                a.triggered.connect(lambda: show_assistant(win).input.setPlaceholderText(
                    "How do I ...?   (answered from the help pages; Ctrl+Enter to send)"))
                after = m.actions()[1] if len(m.actions()) > 1 else None
                m.insertAction(after, a)
                return a

    def add_menu(win, menu):
        """Tools > AI."""
        sub = menu.addMenu("AI")
        asst = sub.addAction("Assistant")
        asst.setShortcut("Ctrl+Shift+A")
        asst.setToolTip("Ask the AI about the open calibration, a map, a log or the app")
        asst.triggered.connect(lambda: assistant_dock(win).toggle())
        win.addAction(asst)
        a = sub.addAction("Proposals...")
        a.setToolTip("Changes AI apps have proposed: preview, accept or reject")
        a.triggered.connect(lambda: state(win).open_proposals())
        share = sub.addAction("Share open tabs with AI apps")
        share.setCheckable(True)
        share.setToolTip("Let AI apps connected to %s (MCP) see the open tabs, "
                         "the selected parameter and unsaved edits. Off: they "
                         "can still open files from disk." % branding.APP_NAME)
        sub.aboutToShow.connect(lambda: share.setChecked(state(win).bridge is not None))
        share.toggled.connect(lambda on: (
            state(win).set_share(on) if on != (state(win).bridge is not None) else None))
        sub.addSeparator()
        st_act = sub.addAction("Settings...")
        st_act.setToolTip("Choose the AI service the app uses itself, its key and model; "
                          "privacy and usage")
        st_act.triggered.connect(lambda: AiSettingsDialog(state(win)).exec())
        log_act = sub.addAction("What was sent...")
        log_act.setToolTip("Every request sent to an AI service this session, as sent")
        log_act.triggered.connect(lambda: SentLogDialog(win).exec())
        c = sub.addAction("Connect an AI app...")
        c.setToolTip("How to add %s to an AI app (Claude, ChatGPT, Cursor, VS Code, "
                     "LM Studio ...)" % branding.APP_NAME)
        c.triggered.connect(lambda: state(win).open_connect())
        return sub

    # ------------------------------------------------------------ connect
    class ConnectDialog(QtWidgets.QDialog):
        def __init__(self, st: AiState):
            super().__init__(st.win)
            self.st = st
            self.setWindowTitle("Connect an AI app")
            self.resize(620, 420)
            from .ai.mcp_server import client_config
            lay = QtWidgets.QVBoxLayout(self)
            intro = QtWidgets.QLabel(
                "%s works with any AI app that supports MCP (Model Context "
                "Protocol). Add the entry below to the app's MCP server "
                "settings, then restart it. The AI can then open binaries and "
                "projects, read maps, compare and propose changes. It cannot "
                "save, flash or change a file: every change waits in "
                "Tools > AI > Proposals for you." % branding.APP_NAME)
            intro.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            intro.setWordWrap(True)
            lay.addWidget(intro)
            text = json.dumps(client_config(), indent=2)
            box = QtWidgets.QPlainTextEdit(text)
            box.setReadOnly(True)
            f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
            box.setFont(f)
            lay.addWidget(box, 1)
            row = QtWidgets.QHBoxLayout()
            copy = QtWidgets.QPushButton("Copy")
            copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(text),
                                          copy.setText("Copied")))
            row.addWidget(copy)
            row.addStretch(1)
            lay.addLayout(row)
            self.share = QtWidgets.QCheckBox("Share open tabs with AI apps "
                                             "(the selected parameter and unsaved edits too)")
            self.share.setChecked(st.bridge is not None)
            self.share.toggled.connect(self._toggle)
            lay.addWidget(self.share)
            self.state_lbl = QtWidgets.QLabel()
            self.state_lbl.setWordWrap(True)
            lay.addWidget(self.state_lbl)
            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(self.reject)
            bb.accepted.connect(self.accept)
            lay.addWidget(bb)
            self._show_state()

        def _toggle(self, on):
            self.st.set_share(on)
            self.share.setChecked(self.st.bridge is not None)
            self._show_state()

        def _show_state(self):
            if self.st.bridge is not None:
                self.state_lbl.setText(
                    "Sharing: AI apps see this window's tabs while it is open "
                    "(local connection only, this user only).")
            else:
                self.state_lbl.setText(
                    "Not sharing: AI apps work only on files they open from disk.")

    # ------------------------------------------------------------ proposals
    class ProposalsDialog(QtWidgets.QDialog):
        """Pending AI proposals: preview, accept, reject."""

        def __init__(self, st: AiState):
            super().__init__(st.win)
            self.st = st
            self.win = st.win
            self.setWindowTitle("AI proposals")
            self.setModal(False)
            self.resize(980, 600)
            self._props = []                    # (prop, ws or None, note)
            self._preview = None                # (ws, old comparison, old link, pid)
            lay = QtWidgets.QVBoxLayout(self)
            top = QtWidgets.QHBoxLayout()
            top.addWidget(QtWidgets.QLabel("Show:"))
            self.scope = QtWidgets.QComboBox()
            self.scope.addItems(["For open tabs", "All pending"])
            self.scope.currentIndexChanged.connect(self.reload)
            top.addWidget(self.scope)
            top.addStretch(1)
            lay.addLayout(top)
            split = QtWidgets.QSplitter()
            self.list = QtWidgets.QListWidget()
            self.list.currentRowChanged.connect(self._show)
            split.addWidget(self.list)
            right = QtWidgets.QWidget()
            rl = QtWidgets.QVBoxLayout(right)
            rl.setContentsMargins(6, 0, 0, 0)
            self.head = QtWidgets.QLabel()
            self.head.setWordWrap(True)
            self.head.setTextFormat(QtCore.Qt.TextFormat.RichText)
            rl.addWidget(self.head)
            self.why = QtWidgets.QTextBrowser()
            self.why.setPlaceholderText("Rationale")
            rl.addWidget(self.why, 1)
            self.maps = QtWidgets.QTableWidget(0, 4)
            self.maps.setHorizontalHeaderLabels(["Parameter", "Cells", "First change", "Notes"])
            self.maps.horizontalHeader().setStretchLastSection(True)
            self.maps.setColumnWidth(0, 300)
            self.maps.setColumnWidth(1, 70)
            self.maps.setColumnWidth(2, 180)
            self.maps.verticalHeader().hide()
            self.maps.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.maps.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.maps.cellDoubleClicked.connect(self._show_map)
            self.maps.setToolTip("Untick a parameter to leave it out when accepting. "
                                 "Double-click to show it in its tab.")
            rl.addWidget(self.maps, 2)
            split.addWidget(right)
            split.setStretchFactor(0, 1)
            split.setStretchFactor(1, 3)
            split.setSizes([280, 700])
            lay.addWidget(split, 1)
            row = QtWidgets.QHBoxLayout()
            self.preview_btn = QtWidgets.QPushButton("Preview as comparison")
            self.preview_btn.setCheckable(True)
            self.preview_btn.setToolTip(
                "Show the tab compared against the result of this proposal: every "
                "cell it would change is marked. Untick to put your comparison back.")
            self.preview_btn.toggled.connect(self._toggle_preview)
            row.addWidget(self.preview_btn)
            row.addStretch(1)
            self.reject_btn = QtWidgets.QPushButton("Reject")
            self.reject_btn.clicked.connect(self._reject)
            row.addWidget(self.reject_btn)
            self.reject_plan_btn = QtWidgets.QPushButton("Reject plan")
            self.reject_plan_btn.setToolTip("Reject every pending step of this plan")
            self.reject_plan_btn.clicked.connect(lambda: self._reject(whole_plan=True))
            row.addWidget(self.reject_plan_btn)
            self.accept_plan_btn = QtWidgets.QPushButton("Accept plan")
            self.accept_plan_btn.setToolTip("Accept every pending step of this plan, in order; "
                                            "each becomes its own version on a project tab")
            self.accept_plan_btn.clicked.connect(lambda: self._accept(whole_plan=True))
            row.addWidget(self.accept_plan_btn)
            self.accept_btn = QtWidgets.QPushButton("Accept")
            self.accept_btn.setDefault(True)
            self.accept_btn.clicked.connect(self._accept)
            row.addWidget(self.accept_btn)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.close)
            row.addWidget(close)
            lay.addLayout(row)

        # -- list -----------------------------------------------------------------
        def reload(self):
            cur = self._current_id()
            if self.scope.currentIndex() == 0:
                self._props = self.st.pending_for_tabs()
            else:
                ids = self.st.identities()
                out = []
                for p in P.all_pending():
                    hit = next(((ws, n) for ws, ident in ids
                                for ok, n in [P.match(p, ident)] if ok), (None, ""))
                    out.append((p, hit[0], hit[1]))
                self._props = out
            self.list.blockSignals(True)
            self.list.clear()
            sel = 0
            for i, (p, ws, _n) in enumerate(self._props):
                t = p.get("target", {})
                it = QtWidgets.QListWidgetItem("%s\n%s\n%s" % (
                    p.get("title", "AI proposal"), _target_text(t), _when(p.get("created", ""))))
                if ws is None:
                    it.setForeground(QtGui.QBrush(QtGui.QColor(128, 128, 128)))
                    it.setToolTip("Its file or project is not open")
                self.list.addItem(it)
                if p.get("id") == cur:
                    sel = i
            self.list.blockSignals(False)
            if self._props:
                self.list.setCurrentRow(sel)
                self._show(sel)
            else:
                self._show(-1)

        def _current(self):
            i = self.list.currentRow()
            return self._props[i] if 0 <= i < len(self._props) else (None, None, "")

        def _current_id(self):
            p = self._current()[0]
            return p.get("id") if p else None

        def _show(self, i):
            p, ws, note = self._current() if i >= 0 else (None, None, "")
            has = p is not None
            for w in (self.accept_btn, self.reject_btn):
                w.setEnabled(has)
            self.preview_btn.setEnabled(has and ws is not None)
            self.accept_btn.setEnabled(has and ws is not None)
            pv = getattr(self.st, "preview", None)
            if pv is not None and (not has or pv[3] != p.get("id")):
                self.preview_btn.setChecked(False)
            upto = plan_steps(p) if has else []
            whole = plan_steps(p, upto=False) if has else []
            self.accept_btn.setText("Accept steps %s" % _step_list(upto) if len(upto) > 1
                                    else "Accept")
            in_plan = has and bool(p.get("plan")) and len(whole) > 1
            self.accept_plan_btn.setVisible(in_plan)
            self.reject_plan_btn.setVisible(in_plan)
            self.accept_plan_btn.setEnabled(in_plan and ws is not None)
            if not has:
                self.head.setText("<i>No pending proposals.</i>" if not self._props else "")
                self.why.clear()
                self.maps.setRowCount(0)
                return
            target = _esc(_target_text(p.get("target", {})))
            lines = ["<b>%s</b>" % _esc(p.get("title", "")),
                     "From %s, %s" % (_esc(_client(p.get("client", ""))), _when(p.get("created", ""))),
                     "For %s" % target]
            if ws is None:
                lines.append("<span style='color:#b36b00'>Open this %s to preview or accept it.</span>"
                             % ("project" if p.get("target", {}).get("kind") == "project-set"
                                else "file"))
            if note:
                lines.append("<span style='color:#b36b00'>Note: %s.</span>" % _esc(note))
            pl = p.get("plan")
            if pl:
                lines.insert(1, "Step %d of %d of the plan <b>%s</b>" % (
                    pl.get("step", 0), pl.get("steps", 0), _esc(pl.get("title", ""))))
            self.head.setText("<br>".join(lines))
            why = p.get("rationale", "")
            if pl and pl.get("goal"):
                why = "Plan goal: %s\n\n%s" % (pl["goal"], why)
            probs = [c for c in p.get("checks", []) if c.get("severity") in ("warning", "note")]
            if probs:
                why += "\n\nSafety checks (passed, with remarks):\n" + "\n".join(
                    "- %s: %s%s" % (c["severity"], c["message"],
                                    " [%s]" % c["where"] if c.get("where") else "")
                    for c in probs[:30])
            if pl and len(upto) > 1:
                why += ("\n\nAccepting this step also accepts the pending steps before it: "
                        "later steps build on earlier ones.")
            self.why.setPlainText(why)
            rows = P.entry_rows(p)
            self.maps.setRowCount(len(rows))
            for r, row in enumerate(rows):
                it = QtWidgets.QTableWidgetItem(row["title"])
                it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.CheckState.Checked)
                it.setData(QtCore.Qt.ItemDataRole.UserRole, row["name"])
                it.setToolTip(row["id"])
                self.maps.setItem(r, 0, it)
                self.maps.setItem(r, 1, QtWidgets.QTableWidgetItem(
                    "%s / %s" % (row["changed_cells"], row["total_cells"])))
                self.maps.setItem(r, 2, QtWidgets.QTableWidgetItem(row["first_change"]))
                notes = []
                if not row["trusted"]:
                    notes.append("placement not established - asks before writing")
                if row["adjusted"]:
                    notes.append("%d value(s) outside the allowed range, held at its "
                                     "limit" % row["adjusted"])
                n = QtWidgets.QTableWidgetItem("; ".join(notes))
                if not row["trusted"]:
                    n.setForeground(QtGui.QBrush(QtGui.QColor(179, 107, 0)))
                self.maps.setItem(r, 3, n)

        def _selected_names(self):
            out = []
            for r in range(self.maps.rowCount()):
                it = self.maps.item(r, 0)
                if it.checkState() == QtCore.Qt.CheckState.Checked:
                    out.append(it.data(QtCore.Qt.ItemDataRole.UserRole))
            return out

        def _show_map(self, r, _c=0):
            p, ws, _n = self._current()
            if ws is None:
                return
            name = self.maps.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            from .session import find_by_ref
            from . import patch as _patch
            ref = p.get("refs", {}).get(name)
            e = find_by_ref(ws.primary, ref) if ref else None
            e = e or _patch.find_entity(ws.primary, name)
            if e is None:
                return
            self.win.show_workspace(ws)
            ws.show_entity(e)

        # -- preview --------------------------------------------------------------
        def _toggle_preview(self, on):
            if on:
                p, ws, _n = self._current()
                if p is None or ws is None:
                    self.preview_btn.setChecked(False)
                    return
                try:
                    start_preview(self.st, p, self._selected_names())
                except Exception as ex:
                    self.preview_btn.blockSignals(True)
                    self.preview_btn.setChecked(False)
                    self.preview_btn.blockSignals(False)
                    QtWidgets.QMessageBox.warning(self, "Preview",
                                                  "This tab cannot show a preview: %s" % ex)
                    return
                self.preview_btn.setText("Previewing - click to restore comparison")
            else:
                self._end_preview()

        def _end_preview(self):
            self.preview_btn.blockSignals(True)
            self.preview_btn.setChecked(False)
            self.preview_btn.setText("Preview as comparison")
            self.preview_btn.blockSignals(False)
            end_preview(self.st)

        def closeEvent(self, ev):
            self._end_preview()
            super().closeEvent(ev)

        def _accept(self, whole_plan=False):
            p, ws, _note = self._current()
            if p is None or ws is None:
                return
            names = self._selected_names()
            if not names and not whole_plan:
                QtWidgets.QMessageBox.information(self, "Accept", "No parameter is ticked.")
                return
            self._end_preview()
            msg = accept(self.st, p, self, names=None if whole_plan else names,
                         whole_plan=whole_plan)
            if msg:
                QtWidgets.QMessageBox.information(self, "Accept", msg)
            self.st.poll()
            self.reload()

        def _reject(self, whole_plan=False):
            p, _ws, _n = self._current()
            if p is None:
                return
            what = ("all pending steps of the plan \"%s\"" % p["plan"]["title"]
                    if whole_plan and p.get("plan") else "\"%s\"" % p.get("title", ""))
            if QtWidgets.QMessageBox.question(self, "Reject", "Reject %s?" % what) != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self._end_preview()
            reject(self.st, p, whole_plan=whole_plan)
            self.st.poll()
            self.reload()

    # ------------------------------------------------------------ review actions
    # Shared by the Proposals dialog and the assistant panel's inline links.
    def find_tab(st, prop):
        for ws, ident in st.identities():
            ok, note = P.match(prop, ident)
            if ok:
                return ws, note
        return None, ""

    def plan_steps(prop, upto=True):
        """The pending steps of ``prop``'s plan, in order -- up to and
        including ``prop`` (``upto``) or all of them. ``[prop]`` outside a plan."""
        pl = prop.get("plan")
        if not pl:
            return [prop]
        steps = [q for q in P.all_pending() if (q.get("plan") or {}).get("id") == pl.get("id")]
        steps.sort(key=lambda q: q["plan"].get("step", 0))
        if upto:
            steps = [q for q in steps if q["plan"].get("step", 0) <= pl.get("step", 0)]
        return steps or [prop]

    def _step_list(steps) -> str:
        nums = [q["plan"]["step"] for q in steps if q.get("plan")]
        if nums and nums == list(range(nums[0], nums[-1] + 1)):
            return "%d-%d" % (nums[0], nums[-1]) if len(nums) > 1 else str(nums[0])
        return ", ".join(str(n) for n in nums)

    def _sel(q, prop, names):
        return P.with_selection(q, names) if names is not None and q is prop else q

    def start_preview(st, prop, names=None):
        """Show the tab compared against what ``prop`` (with the plan steps
        before it) would make of it. Nothing is written."""
        end_preview(st)
        ws, _n = find_tab(st, prop)
        if ws is None:
            raise ValueError("open its file or project first")
        prev = ws.primary.sibling(bytes(ws.primary.binfile.data),
                                  name="AI proposal - %s" % prop.get("title", ""))
        for q in plan_steps(prop):
            P.apply_to_session(_sel(q, prop, names), prev, confirm_untrusted=True)
        st.preview = (ws, ws.comparison, ws.cmp_link, prop.get("id"))
        ws.set_comparison(prev)
        st.win.show_workspace(ws)

    def end_preview(st):
        pv, st.preview = getattr(st, "preview", None), None
        if pv is None:
            return
        ws, old, link, _pid = pv
        try:
            if getattr(ws, "primary", None) is not None:
                ws.set_comparison(old, link=link)
        except RuntimeError:
            pass                                # the tab was closed

    def accept(st, prop, parent, names=None, whole_plan=False) -> str:
        """Apply ``prop`` (and the pending plan steps before it, or the whole
        plan) to its tab. Each step is one undo step; on a project tab with no
        other unsaved edits each is saved as its own version "AI: <title>".
        Returns what happened, "" when cancelled."""
        ws, _n = find_tab(st, prop)
        if ws is None:
            return "Open its file or project first."
        steps = plan_steps(prop, upto=not whole_plan)
        untrusted = []
        for q in steps:
            for r in P.entry_rows(_sel(q, prop, names)):
                if not r["trusted"] and (names is None or q is not prop or r["name"] in names):
                    untrusted.append(r["title"])
        confirm = False
        if untrusted:
            ans = QtWidgets.QMessageBox.question(
                parent, "Accept",
                "The address of these parameters was not established (it was "
                "inferred, not anchored):\n\n  %s\n\nWrite them anyway?"
                % "\n  ".join(sorted(set(untrusted))[:15]),
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.No)
            if ans == QtWidgets.QMessageBox.StandardButton.Cancel:
                return ""
            confirm = ans == QtWidgets.QMessageBox.StandardButton.Yes
        end_preview(st)
        clean = not ws.is_dirty()
        lines, saved_n, applied_n = [], 0, 0
        for q in steps:
            try:
                res = P.apply_to_session(_sel(q, prop, names), ws.primary,
                                         confirm_untrusted=confirm)
            except Exception as ex:
                lines.append("%s: not applied (%s)" % (q.get("title", ""), ex))
                break
            try:
                ws.model.refresh()
                ws._status()
            except Exception:
                pass
            if not res.applied:
                lines.append("%s: nothing applied%s" % (q.get("title", ""), "".join(
                    "; %s: %s" % s_ for s_ in res.skipped[:5])))
                continue
            applied_n += 1
            if ws.project_ref is not None and clean and ws.save(tab_only=True):
                saved_n += 1
            P.resolve(q["id"], "accepted", "applied: %s" % ", ".join(res.applied))
            line = "%s: %d parameter%s" % (q.get("title", ""), len(res.applied),
                                           "" if len(res.applied) == 1 else "s")
            if res.skipped:
                line += "; skipped %s" % "; ".join("%s (%s)" % s_ for s_ in res.skipped[:5])
            lines.append(line)
        if saved_n:
            lines.append("Saved as %d version%s named \"AI: ...\"." % (
                saved_n, "" if saved_n == 1 else "s"))
        elif applied_n:
            lines.append("Applied as %s (Edit > Undo). Save to keep %s." % (
                "one undo step" if applied_n == 1 else "%d undo steps" % applied_n,
                "it" if applied_n == 1 else "them"))
        st.poll()
        return "\n".join(lines)

    def reject(st, prop, whole_plan=False) -> None:
        end_preview(st)
        for q in (plan_steps(prop, upto=False) if whole_plan else [prop]):
            try:
                P.resolve(q["id"], "rejected")
            except Exception:
                pass
        st.poll()

    def _target_text(t: dict) -> str:
        """"A6 C6 BHW - ECU, branch Stage 1" / "038906016Q.Bin"."""
        import os
        if t.get("kind") == "project-set":
            proj = t.get("project_name") or os.path.splitext(
                os.path.basename(t.get("path", "")))[0] or "project"
            return "%s - %s, branch %s" % (proj, t.get("set_name", "?"),
                                           t.get("branch_name", "?"))
        return os.path.basename(t.get("path", "")) or t.get("label", "?")

    def _esc(s) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _client(c: str) -> str:
        c = c or "an AI app"
        return c.split(":", 1)[1] if c.startswith("mcp:") else c

    def _when(ts: str) -> str:
        return ts.replace("T", " ")[:16] if ts else ""


if HAVE_QT:
    from .ai import config as _C

    #: bump when the notice says something new that people must see again
    NOTICE_VERSION = 1
    NOTICE_TITLE = "AI can make mistakes"
    NOTICE_TEXT = (
        "<p><b>AI can make mistakes.</b></p>"
        "<p>An AI can misread a map, misunderstand what you asked, or suggest a "
        "change that is wrong for your car &mdash; and sound sure of itself while "
        "doing it. The safety checks catch some mistakes, not all of them.</p>"
        "<p><b>Always review every change an AI makes or proposes yourself before "
        "you flash it:</b> preview it against the original and check the values. "
        "You are responsible for what is written to a control unit.</p>")

    def notice_accepted() -> bool:
        return int(_C.load().get("notice_accepted") or 0) >= NOTICE_VERSION

    def _show_notice(parent) -> bool:
        """The notice itself; True when acknowledged. (Tests replace it.)"""
        box = QtWidgets.QMessageBox(parent)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle(NOTICE_TITLE)
        box.setTextFormat(QtCore.Qt.TextFormat.RichText)
        box.setText(NOTICE_TEXT)
        ok = box.addButton("I understand", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(ok)
        box.exec()
        return box.clickedButton() is ok

    def confirm_notice(parent) -> bool:
        """Before AI is first used: the notice, once. True to go ahead."""
        if notice_accepted():
            return True
        if not _show_notice(parent):
            return False
        _C.update(notice_accepted=NOTICE_VERSION)
        return True
    from .ai import keystore as _K
    from .ai import providers as _PV

    class _Job(QtCore.QObject):
        """Run ``fn`` on a worker thread; ``done(result, error)`` on the GUI
        thread. Kept alive by its parent until it reports."""

        finished = QtCore.Signal(object, object)

        def __init__(self, parent, fn, done):
            super().__init__(parent)
            self.finished.connect(lambda r, e: (done(r, e), self.deleteLater()))
            threading.Thread(target=self._run, args=(fn,), daemon=True).start()

        def _run(self, fn):
            try:
                r, e = fn(), None
            except Exception as ex:                     # reported, not raised
                r, e = None, ex
            try:
                self.finished.emit(r, e)
            except RuntimeError:
                pass                                    # the dialog was closed

    # ------------------------------------------------------------ settings
    class AiSettingsDialog(QtWidgets.QDialog):
        """Tools > AI > Settings..."""

        AUTO = ""

        def __init__(self, st: "AiState"):
            super().__init__(st.win)
            self.st = st
            self.setWindowTitle("AI settings")
            self.resize(640, 680)
            self.cfg = _C.load()
            self._keys = {}                     # service -> key typed this time
            self._cleared = set()
            lay = QtWidgets.QVBoxLayout(self)

            self.enabled = QtWidgets.QCheckBox("Use AI inside %s" % branding.APP_NAME)
            self.enabled.setToolTip("Off: nothing is sent to any AI service by the app "
                                    "itself. AI apps connected over MCP are separate.")
            self.enabled.setChecked(bool(self.cfg.get("enabled")))
            self.enabled.toggled.connect(self._enabled_toggled)
            lay.addWidget(self.enabled)

            box = QtWidgets.QGroupBox("Service")
            form = QtWidgets.QFormLayout(box)
            self.service = QtWidgets.QComboBox()
            self.service.addItem("Automatic: a model on this PC (Ollama / LM Studio)", self.AUTO)
            for sid, spec in _PV.SPECS.items():
                self.service.addItem(spec.label, sid)
            i = self.service.findData(self.cfg.get("service") or self.AUTO)
            self.service.setCurrentIndex(max(0, i))
            form.addRow("Service:", self.service)
            self.note = QtWidgets.QLabel()
            self.note.setWordWrap(True)
            self.note.setOpenExternalLinks(True)
            self.note.setTextFormat(QtCore.Qt.TextFormat.RichText)
            form.addRow("", self.note)
            krow = QtWidgets.QHBoxLayout()
            self.key = QtWidgets.QLineEdit()
            self.key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            self.key.textEdited.connect(self._key_edited)
            krow.addWidget(self.key, 1)
            self.show_key = QtWidgets.QToolButton()
            self.show_key.setText("Show")
            self.show_key.setCheckable(True)
            self.show_key.toggled.connect(lambda on: self.key.setEchoMode(
                QtWidgets.QLineEdit.EchoMode.Normal if on else
                QtWidgets.QLineEdit.EchoMode.Password))
            krow.addWidget(self.show_key)
            self.forget = QtWidgets.QToolButton()
            self.forget.setText("Remove")
            self.forget.setToolTip("Remove the stored key for this service")
            self.forget.clicked.connect(self._forget_key)
            krow.addWidget(self.forget)
            self.key_row = QtWidgets.QWidget()
            self.key_row.setLayout(krow)
            krow.setContentsMargins(0, 0, 0, 0)
            form.addRow("API key:", self.key_row)
            self.key_where = QtWidgets.QLabel()
            self.key_where.setWordWrap(True)
            form.addRow("", self.key_where)
            self.base = QtWidgets.QLineEdit()
            form.addRow("Address:", self.base)
            mrow = QtWidgets.QHBoxLayout()
            mrow.setContentsMargins(0, 0, 0, 0)
            self.model = QtWidgets.QComboBox()
            self.model.setEditable(True)
            self.model.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                     QtWidgets.QSizePolicy.Policy.Fixed)
            # left empty, the assistant takes the first model the service
            # lists (the newest, for the online services)
            self.model.lineEdit().setPlaceholderText("automatic - the first model the "
                                                     "service lists")
            mrow.addWidget(self.model, 1)
            self.refresh = QtWidgets.QPushButton("List models")
            self.refresh.clicked.connect(self._list_models)
            mrow.addWidget(self.refresh)
            mw = QtWidgets.QWidget()
            mw.setLayout(mrow)
            form.addRow("Model:", mw)
            self.free_only = QtWidgets.QCheckBox("Free models only")
            self.free_only.toggled.connect(lambda _on: self._fill_models(self._models))
            form.addRow("", self.free_only)
            trow = QtWidgets.QHBoxLayout()
            trow.setContentsMargins(0, 0, 0, 0)
            self.test = QtWidgets.QPushButton("Test connection")
            self.test.clicked.connect(self._test)
            trow.addWidget(self.test)
            self.detect = QtWidgets.QPushButton("Find models on this PC")
            self.detect.clicked.connect(self._detect)
            trow.addWidget(self.detect)
            trow.addStretch(1)
            tw = QtWidgets.QWidget()
            tw.setLayout(trow)
            form.addRow("", tw)
            self.status = QtWidgets.QLabel()
            self.status.setWordWrap(True)
            form.addRow("", self.status)
            lay.addWidget(box)

            priv = QtWidgets.QGroupBox("Privacy")
            pl = QtWidgets.QVBoxLayout(priv)
            self.redact = QtWidgets.QCheckBox(
                "Remove VIN and immobiliser data from everything sent to an online service")
            self.redact.setChecked(bool(self.cfg.get("redact", True)))
            pl.addWidget(self.redact)
            self.fallback = QtWidgets.QCheckBox(
                "When offline, answer with a model on this PC if one is running")
            self.fallback.setChecked(bool(self.cfg.get("fallback_local", True)))
            pl.addWidget(self.fallback)
            sent = QtWidgets.QPushButton("What was sent...")
            sent.clicked.connect(lambda: SentLogDialog(self).exec())
            row = QtWidgets.QHBoxLayout()
            row.addWidget(sent)
            row.addStretch(1)
            pl.addLayout(row)
            lay.addWidget(priv)

            use = QtWidgets.QGroupBox("Usage (tokens, all sessions)")
            ul = QtWidgets.QVBoxLayout(use)
            self.usage = QtWidgets.QTableWidget(0, 6)
            self.usage.setHorizontalHeaderLabels(["Service", "Model", "Requests",
                                                  "Input", "Output", "From cache"])
            self.usage.verticalHeader().hide()
            hh = self.usage.horizontalHeader()
            for c in range(6):
                hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.Stretch if c < 2
                                        else QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            self.usage.setWordWrap(False)
            self.usage.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            ul.addWidget(self.usage)
            reset = QtWidgets.QPushButton("Reset counts")
            reset.clicked.connect(self._reset_usage)
            r2 = QtWidgets.QHBoxLayout()
            r2.addStretch(1)
            r2.addWidget(reset)
            ul.addLayout(r2)
            lay.addWidget(use, 1)

            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(self._ok)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

            self._models = []
            self._sid = None
            self.service.currentIndexChanged.connect(self._service_changed)
            self._service_changed()
            self._show_usage()

        # -- the selected service -----------------------------------------------------
        def _spec(self):
            return _PV.SPECS.get(self.service.currentData() or "")

        def _remember_current(self):
            """Keep what was typed for the service being left."""
            sid = self._sid
            if not sid:
                return
            self.cfg.setdefault("models", {})[sid] = self.model.currentText().strip()
            b = self.base.text().strip()
            spec = _PV.SPECS[sid]
            if b and b.rstrip("/") != spec.base_url.rstrip("/"):
                self.cfg.setdefault("base_urls", {})[sid] = b
            else:
                self.cfg.setdefault("base_urls", {}).pop(sid, None)

        def _service_changed(self, *_):
            self._remember_current()
            spec = self._spec()
            self._sid = spec.id if spec else None
            self.status.clear()
            self._models = []
            if spec is None:
                self.note.setText(
                    "Uses Ollama or LM Studio when one is running on this PC: free, "
                    "works offline, and nothing leaves the PC. Install one and download "
                    "a model that supports tools, then click <i>Find models on this PC</i>. "
                    "<a href='https://ollama.com/download'>Ollama</a> &middot; "
                    "<a href='https://lmstudio.ai'>LM Studio</a>")
                for w in (self.key_row, self.base, self.model, self.refresh, self.test,
                          self.free_only):
                    w.setEnabled(False)
                self.key.clear()
                self.key.setPlaceholderText("not needed")
                self.key_where.clear()
                self.base.clear()
                self.base.setPlaceholderText("found automatically")
                self.model.clear()
                self.free_only.setVisible(False)
                return
            for w in (self.base, self.model, self.refresh, self.test, self.free_only):
                w.setEnabled(True)
            note = _esc(spec.note)
            if spec.key_url:
                note += " <a href='%s'>%s</a>" % (
                    spec.key_url, "Get a key" if spec.needs_key else "Download")
            self.note.setText(note)
            self.key_row.setEnabled(spec.needs_key or spec.id == "custom")
            self.key.clear()
            src = _K.key_source(spec.id) if spec.id not in self._cleared else ""
            if spec.id in self._keys:
                self.key.setText(self._keys[spec.id])
                self.key_where.setText("Will be stored in %s." % _K.DESCRIBE[_K.backend()])
            elif src == "stored":
                self.key.setPlaceholderText("stored - type to replace")
                self.key_where.setText("Stored in %s." % _K.DESCRIBE[_K.backend()])
            elif src == "environment":
                self.key.setPlaceholderText("from %s" % spec.env)
                self.key_where.setText("Using the %s environment variable." % spec.env)
            else:
                self.key.setPlaceholderText("not needed" if not spec.needs_key else "paste your key")
                self.key_where.setText("" if not spec.needs_key else
                                       "Kept in %s, never in the settings file."
                                       % _K.DESCRIBE[_K.backend()])
            self.base.setPlaceholderText(spec.base_url or "http://host:port/v1")
            self.base.setText(_C.base_url_for(self.cfg, spec.id))
            self.model.clear()
            m = _C.model_for(self.cfg, spec.id)
            if m:
                self.model.addItem(m)
            self.model.setEditText(m)
            self.free_only.setVisible(spec.id == "openrouter")

        def _key_edited(self, text):
            spec = self._spec()
            if spec is not None:
                self._keys[spec.id] = text.strip()
                self._cleared.discard(spec.id)

        def _forget_key(self):
            spec = self._spec()
            if spec is None:
                return
            self._keys.pop(spec.id, None)
            self._cleared.add(spec.id)
            self.key.clear()
            self.key.setPlaceholderText("removed when you click OK")
            self.key_where.setText("")

        def _provider(self):
            spec = self._spec()
            key = self._keys.get(spec.id)
            if key is None and spec.id not in self._cleared:
                key = _K.get_key(spec.id) or ""
            from .ai.assistant import _log_sent
            http = _PV.HttpClient(timeout=20, on_send=lambda r: _log_sent(spec.id, r))
            return _PV.make(spec.id, key=key or "", model=self.model.currentText().strip(),
                            base_url=self.base.text().strip(), http=http)

        def _busy(self, on, text=""):
            for w in (self.refresh, self.test, self.detect):
                w.setEnabled(not on)
            if text:
                self.status.setText(text)

        def _list_models(self):
            spec = self._spec()
            if spec is None:
                return
            if spec.needs_key and not (self._keys.get(spec.id) or _K.get_key(spec.id)):
                self.status.setText("Enter the API key first.")
                return
            p = self._provider()
            self._busy(True, "Asking %s for its models..." % spec.label)

            def done(models, err):
                self._busy(False)
                if err is not None:
                    self.status.setText(_err_text(err))
                    return
                self._fill_models(models or [])
                self.status.setText("%d models." % len(models or []))
            _Job(self, p.list_models, done)

        def _fill_models(self, models):
            self._models = list(models)
            cur = self.model.currentText().strip()
            shown = [m for m in models if not self.free_only.isChecked()
                     or m.endswith(":free")]
            self.model.clear()
            self.model.addItems(shown)
            if cur and cur in shown:
                self.model.setCurrentText(cur)
            elif shown and not cur:
                self.model.setCurrentIndex(0)
            else:
                self.model.setEditText(cur)

        def _test(self):
            spec = self._spec()
            if spec is None:
                return
            p = self._provider()
            self._busy(True, "Connecting to %s..." % spec.label)

            def run():
                msg = p.check()
                if p.model:
                    t = p.complete("Reply with the single word OK.",
                                   [{"role": "user", "text": "Test"}], [], max_tokens=16)
                    msg += "; %s answered: %s" % (p.model, (t.text or "").strip()[:40] or "(empty)")
                return msg

            def done(msg, err):
                self._busy(False)
                self.status.setText(_err_text(err) if err is not None else msg[:1].upper() + msg[1:] + ".")
            _Job(self, run, done)

        def _detect(self):
            self._busy(True, "Looking for Ollama and LM Studio on this PC...")

            def done(found, err):
                self._busy(False)
                if err is not None or not found:
                    self.status.setText("No local model server is running. Start Ollama or "
                                        "LM Studio's local server, then try again.")
                    return
                sid, models = found[0]
                i = self.service.findData(sid)
                self.service.setCurrentIndex(i)
                self._fill_models(models)
                self.status.setText("Found %s with %d model%s." % (
                    _PV.SPECS[sid].label, len(models), "" if len(models) == 1 else "s"))
            _Job(self, _PV.detect_local, done)

        # -- usage ---------------------------------------------------------------------
        def _show_usage(self):
            rows = []
            for sid, models in (self.cfg.get("usage") or {}).items():
                for m, u in models.items():
                    rows.append((_PV.SPECS[sid].label if sid in _PV.SPECS else sid, m, u))
            self.usage.setRowCount(len(rows))
            for r, (svc, m, u) in enumerate(rows):
                for c, v in enumerate((svc, m, u.get("requests", 0), u.get("input", 0),
                                       u.get("output", 0), u.get("cached", 0))):
                    it = QtWidgets.QTableWidgetItem("{:,}".format(v) if isinstance(v, int) else v)
                    if isinstance(v, int):
                        it.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight
                                                | QtCore.Qt.AlignmentFlag.AlignVCenter))
                    self.usage.setItem(r, c, it)

        def _reset_usage(self):
            self.cfg["usage"] = {}
            self._show_usage()

        # -- save ----------------------------------------------------------------------
        def _enabled_toggled(self, on):
            """Turning AI on: the notice first, the first time."""
            if not on:
                return
            if confirm_notice(self):
                self.cfg["notice_accepted"] = NOTICE_VERSION   # saved with the rest
            else:
                self.enabled.blockSignals(True)
                self.enabled.setChecked(False)
                self.enabled.blockSignals(False)

        def _ok(self):
            self._remember_current()
            spec = self._spec()
            self.cfg["service"] = spec.id if spec else ""
            self.cfg["enabled"] = self.enabled.isChecked()
            self.cfg["redact"] = self.redact.isChecked()
            self.cfg["fallback_local"] = self.fallback.isChecked()
            if not self.cfg["redact"] and spec is not None and not spec.local:
                q = QtWidgets.QMessageBox.question(
                    self, "AI settings",
                    "VINs and immobiliser data will be sent to %s as they are. Continue?"
                    % spec.label)
                if q != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            fresh = _C.load()                       # counts added meanwhile
            if self.cfg.get("usage") != {}:
                self.cfg["usage"] = fresh.get("usage", {})
            where = []
            try:
                for sid in self._cleared:
                    _K.delete_key(sid)
                for sid, key in self._keys.items():
                    if key:
                        where.append(_K.set_key(sid, key))
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, "AI settings",
                                              "The key could not be stored: %s" % ex)
                return
            _C.save(self.cfg)
            # an open assistant panel follows at once, whichever way the
            # dialog was opened (the menu or the panel's own button)
            d = getattr(self.st, "dock", None)
            if d is not None:
                d.settings_changed()
            if "file" in where and _K.backend() != "file":
                QtWidgets.QMessageBox.information(
                    self, "AI settings", "The system keychain did not accept the key, so it "
                    "is kept in a private file in your settings folder instead.")
            self.accept()

    def _err_text(err) -> str:
        if isinstance(err, _PV.ProviderError):
            hint = {_PV.AUTH: " Check the API key.",
                    _PV.OFFLINE: " Check the address and your internet connection.",
                    _PV.RATE: " Wait a moment, or check your plan's limits.",
                    _PV.BLOCKED: " Try again later or from another network; if it keeps "
                                 "happening, the service is blocking this app."}.get(err.kind, "")
            return str(err)[:1].upper() + str(err)[1:] + "." + hint
        return "Failed: %s" % err

    # ------------------------------------------------------------ sent log
    class SentLogDialog(QtWidgets.QDialog):
        """Tools > AI > What was sent..."""

        def __init__(self, parent):
            super().__init__(parent)
            self.setWindowTitle("What was sent to AI services")
            self.resize(980, 620)
            from .ai.assistant import sent_log
            self.entries = list(reversed(sent_log()))
            lay = QtWidgets.QVBoxLayout(self)
            intro = QtWidgets.QLabel(
                "Every request %s sent to an AI service this session, newest first, "
                "exactly as sent (API keys are never shown or logged). AI apps connected "
                "over MCP send their own requests; see those apps." % branding.APP_NAME)
            intro.setWordWrap(True)
            lay.addWidget(intro)
            split = QtWidgets.QSplitter()
            self.list = QtWidgets.QListWidget()
            for e in self.entries:
                status = e.get("status") or e.get("error", "")[:30] or ""
                it = QtWidgets.QListWidgetItem("%s  %s\n%s %s  %s" % (
                    e.get("time", "")[11:19], _PV.SPECS[e["service"]].label
                    if e.get("service") in _PV.SPECS else e.get("service", ""),
                    e.get("method", ""), _path(e.get("url", "")), status))
                self.list.addItem(it)
            self.list.currentRowChanged.connect(self._show)
            split.addWidget(self.list)
            self.body = QtWidgets.QPlainTextEdit()
            self.body.setReadOnly(True)
            self.body.setFont(QtGui.QFontDatabase.systemFont(
                QtGui.QFontDatabase.SystemFont.FixedFont))
            split.addWidget(self.body)
            split.setSizes([300, 680])
            lay.addWidget(split, 1)
            if not self.entries:
                self.body.setPlainText("Nothing has been sent this session.")
            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)
            if self.entries:
                self.list.setCurrentRow(0)

        #: entries bigger than this are formatted on a worker thread
        BIG = 256 << 10
        #: and shown no further than this (the rest is cut, with a note)
        SHOW_MAX = 4 << 20

        def _show(self, i):
            if not 0 <= i < len(self.entries):
                return
            e = self.entries[i]
            self._want = i

            def fmt():
                t = json.dumps(e, indent=2, ensure_ascii=False, default=str)
                if len(t) > self.SHOW_MAX:
                    t = t[:self.SHOW_MAX] + "\n\n... (%s more characters not shown)" % (
                        "{:,}".format(len(t) - self.SHOW_MAX))
                return t
            if int(e.get("size") or 0) < self.BIG:
                self.body.setPlainText(fmt())
                return
            self.body.setPlainText("Formatting a large request (%s bytes)..."
                                   % "{:,}".format(int(e.get("size") or 0)))

            def done(text, err, i=i):
                if getattr(self, "_want", None) == i:
                    self.body.setPlainText(text if err is None else "Could not show it: %s" % err)
            _Job(self, fmt, done)

    def _path(url: str) -> str:
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            return u.netloc + u.path
        except Exception:
            return url


# ====================================================================== phase 4
if HAVE_QT:
    from urllib.parse import quote as _q, unquote as _uq

    MAP_SCHEME, PROP_SCHEME, ACT_SCHEME = "ovtmap", "ovtprop", "ovtact"

    class _AskJob(QtCore.QObject):
        """One question on a worker thread; events and the reply come back
        as queued signals."""

        event = QtCore.Signal(object)
        done = QtCore.Signal(object, object)

        def __init__(self, parent, assistant, question, context):
            super().__init__(parent)
            self.assistant = assistant
            assistant.on_event = lambda ev: self._emit(self.event, ev)
            threading.Thread(target=self._run, args=(question, context), daemon=True).start()

        def _emit(self, sig, *a):
            try:
                sig.emit(*a)
            except RuntimeError:
                pass

        def _run(self, q, ctx):
            try:
                r, e = self.assistant.ask(q, ctx), None
            except Exception as ex:
                r, e = None, ex
            self._emit(self.done, r, e)

    #: what the activity line says while a tool runs
    _TOOL_WORDS = {"get_map": "Reading", "find_maps": "Searching for", "list_maps": "Listing",
                   "diff": "Comparing", "identify": "Identifying", "history": "Reading history of",
                   "log_summary": "Summarising log", "log_window": "Reading log",
                   "search_help": "Searching help for", "read_help": "Reading help page",
                   "propose_change": "Writing a proposal:", "checksum_status": "Checking checksums of"}

    PANEL_KEY = "ai/panel_"
    #: at most this often (ms) the reply being written is redrawn
    RENDER_MS = 250
    PANEL_WIDTH, PANEL_MIN, RAIL_WIDTH = 380, 260, 26

    class _Rail(QtWidgets.QWidget):
        """The folded panel: a slim strip with the title written upwards.
        A click opens the panel again."""

        def __init__(self, text, on_click):
            super().__init__()
            self.text, self.on_click = text, on_click
            self.setFixedWidth(RAIL_WIDTH)
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Open the AI assistant (Ctrl+Shift+A)")
            self._hover = False

        def enterEvent(self, ev):
            self._hover = True
            self.update()

        def leaveEvent(self, ev):
            self._hover = False
            self.update()

        def mousePressEvent(self, ev):
            self.on_click()

        def paintEvent(self, ev):
            pt = QtGui.QPainter(self)
            pal = self.palette()
            pt.fillRect(self.rect(), pal.color(QtGui.QPalette.ColorRole.AlternateBase
                                               if self._hover else QtGui.QPalette.ColorRole.Window))
            pt.setPen(pal.color(QtGui.QPalette.ColorRole.Mid))
            pt.drawLine(0, 0, 0, self.height())
            pt.setPen(pal.color(QtGui.QPalette.ColorRole.WindowText))
            f = pt.font()
            f.setBold(True)
            pt.setFont(f)
            pt.translate(self.width() / 2 + pt.fontMetrics().ascent() / 2 - 1, 16)
            pt.rotate(90)
            pt.drawText(0, 0, "\u25c2  " + self.text)
            pt.end()

    class _TitleBar(QtWidgets.QWidget):
        """The open panel's title: name, fold arrow, close."""

        def __init__(self, dock):
            super().__init__()
            lay = QtWidgets.QHBoxLayout(self)
            lay.setContentsMargins(6, 2, 2, 2)
            lbl = QtWidgets.QLabel("AI assistant")
            f = lbl.font()
            f.setBold(True)
            lbl.setFont(f)
            lay.addWidget(lbl)
            lay.addStretch(1)
            fold = QtWidgets.QToolButton()
            fold.setAutoRaise(True)
            fold.setArrowType(QtCore.Qt.ArrowType.RightArrow)
            fold.setToolTip("Fold the panel to the edge (Ctrl+Shift+A)")
            fold.clicked.connect(dock.collapse)
            lay.addWidget(fold)
            close = QtWidgets.QToolButton()
            close.setAutoRaise(True)
            close.setIcon(self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_TitleBarCloseButton))
            close.setToolTip("Hide the panel (Tools > AI > Assistant brings it back)")
            close.clicked.connect(dock.hide)
            lay.addWidget(close)

    class AssistantDock(QtWidgets.QDockWidget):
        """Tools > AI > Assistant (Ctrl+Shift+A): chat about the open tabs."""

        def __init__(self, st: "AiState"):
            super().__init__("AI assistant", st.win)
            self.st = st
            self.win = st.win
            self.setObjectName("ai_assistant_dock")
            # always on the right; the splitter beside it resizes it, the
            # arrow in its title bar folds it to a slim strip
            self.setAllowedAreas(QtCore.Qt.DockWidgetArea.RightDockWidgetArea)
            self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable)
            self.collapsed = False
            self._width = PANEL_WIDTH
            self.assistant = None
            self.job = None
            self.chat = []                    # (kind, text) -- the transcript shown
            self.tokens = 0
            self._activity = ""
            w = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(w)
            lay.setContentsMargins(6, 6, 6, 6)
            # the service line on its own row, the buttons under it: a
            # narrow panel must not squeeze it into a column of words
            self.status = QtWidgets.QLabel()
            self.status.setWordWrap(True)
            self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                      QtWidgets.QSizePolicy.Policy.Preferred)
            lay.addWidget(self.status)
            top = QtWidgets.QHBoxLayout()
            top.addStretch(1)
            new = QtWidgets.QToolButton()
            new.setText("New chat")
            new.setAutoRaise(True)
            new.clicked.connect(self.new_chat)
            top.addWidget(new)
            sett = QtWidgets.QToolButton()
            sett.setText("Settings")
            sett.setAutoRaise(True)
            sett.clicked.connect(self._settings)
            top.addWidget(sett)
            lay.addLayout(top)
            self.banner = QtWidgets.QLabel()
            self.banner.setWordWrap(True)
            self.banner.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.banner.linkActivated.connect(lambda _l: self._settings())
            self.banner.setStyleSheet("QLabel{background:palette(alternate-base);"
                                      "border:1px solid palette(mid);padding:6px;}")
            lay.addWidget(self.banner)
            self.view = QtWidgets.QTextBrowser()
            self.view.setOpenLinks(False)
            self.view.anchorClicked.connect(self._link)
            lay.addWidget(self.view, 1)
            ctx = QtWidgets.QVBoxLayout()
            ctx.setSpacing(2)
            ctx.addWidget(QtWidgets.QLabel("Include:"))
            self.use_tab = QtWidgets.QCheckBox()
            self.use_tab.setChecked(True)
            self.use_map = QtWidgets.QCheckBox()
            self.use_map.setChecked(True)
            for c in (self.use_tab, self.use_map):
                c.setToolTip("Tell the assistant what you are looking at, so you can "
                             "ask about \"this map\" or \"this file\"")
                # long names must not widen the panel: they are cut instead
                c.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                QtWidgets.QSizePolicy.Policy.Fixed)
                ctx.addWidget(c)
            lay.addLayout(ctx)
            self.input = QtWidgets.QPlainTextEdit()
            self.input.setPlaceholderText("Ask about this calibration, a map, a log or "
                                          "the app...   (Ctrl+Enter to send)")
            self.input.setFixedHeight(72)
            self.input.installEventFilter(self)
            lay.addWidget(self.input)
            row = QtWidgets.QHBoxLayout()
            self.activity = QtWidgets.QLabel()
            self.activity.setWordWrap(True)
            self.activity.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                        QtWidgets.QSizePolicy.Policy.Preferred)
            row.addWidget(self.activity, 1)
            self.stop = QtWidgets.QPushButton("Stop")
            self.stop.clicked.connect(self._stop)
            self.stop.hide()
            row.addWidget(self.stop)
            self.send = QtWidgets.QPushButton("Send")
            self.send.clicked.connect(lambda: self.ask(self.input.toPlainText()))
            row.addWidget(self.send)
            lay.addLayout(row)
            self.content = w
            self.rail = _Rail("AI assistant", self.expand)
            self.stack = QtWidgets.QStackedWidget()
            self.stack.addWidget(w)
            self.stack.addWidget(self.rail)
            self.setWidget(self.stack)
            self.title = _TitleBar(self)
            self.setTitleBarWidget(self.title)
            self._blank_title = QtWidgets.QWidget()
            self.visibilityChanged.connect(self._visibility)
            self.refresh_context()
            self._render()
            self._update_status()

        # -- collapsing ------------------------------------------------------------------
        def _live(self) -> bool:
            """Sizes are the user's only once the window is up."""
            return self.win.isVisible() and not getattr(self, "_restoring", False)

        def collapse(self):
            if self.collapsed:
                return
            if self._live() and self.width() > RAIL_WIDTH + 20:
                self._width = self.width()
            self.collapsed = True
            self.stack.setCurrentWidget(self.rail)
            self.setTitleBarWidget(self._blank_title)
            self.setMinimumWidth(RAIL_WIDTH)
            self.setMaximumWidth(RAIL_WIDTH)
            self._save_state()

        def expand(self):
            if not self.isVisible():
                self.show()
            if not self.collapsed:
                return
            # the width to go back to, taken before the minimum-width change
            # below resizes the dock (and resizeEvent would record that)
            w = max(PANEL_MIN, self._width)
            self.collapsed = False
            self.stack.setCurrentWidget(self.content)
            self.setTitleBarWidget(self.title)
            self.setMinimumWidth(PANEL_MIN)
            self.setMaximumWidth(16777215)
            self._width = w
            self.refresh_context()
            self.win.resizeDocks([self], [w], QtCore.Qt.Orientation.Horizontal)
            # once more after the layout has settled (the first may be clipped
            # while the rail's fixed width is still in force)
            QtCore.QTimer.singleShot(0, lambda: self._settle(w))
            self._save_state()

        def _settle(self, w):
            self.win.resizeDocks([self], [w], QtCore.Qt.Orientation.Horizontal)
            self._width = w

        def toggle(self):
            """Ctrl+Shift+A: hidden -> open; folded -> open; open -> folded."""
            if not self.isVisible():
                self.show()
                self.expand()
                self.input.setFocus()
            elif self.collapsed:
                self.expand()
                self.input.setFocus()
            else:
                self.collapse()

        def _visibility(self, _vis):
            QtCore.QTimer.singleShot(0, self._save_state)

        def resizeEvent(self, ev):
            super().resizeEvent(ev)
            if self._live() and not self.collapsed and self.width() > RAIL_WIDTH + 20:
                self._width = self.width()

        def _save_state(self):
            if getattr(self, "_restoring", False):
                return
            if not self.win.isVisible():
                return                        # starting up or closing: not a choice
            try:
                st = _settings()
                st.setValue(PANEL_KEY + "visible", bool(self.isVisible()))
                st.setValue(PANEL_KEY + "collapsed", bool(self.collapsed))
                st.setValue(PANEL_KEY + "width", int(self._width))
            except RuntimeError:
                pass

        # -- state ---------------------------------------------------------------------
        def eventFilter(self, obj, ev):
            if obj is self.input and ev.type() == QtCore.QEvent.Type.KeyPress:
                if ev.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and \
                        ev.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                    self.ask(self.input.toPlainText())
                    return True
            return super().eventFilter(obj, ev)

        def _settings(self):
            AiSettingsDialog(self.st).exec()

        def settings_changed(self):
            """Service, model or privacy settings were saved: the next question
            uses them, and the chat so far is carried on. While an answer is
            being written, the switch waits until it is done."""
            if self.job is not None:
                self._settings_pending = True
                return
            self._settings_pending = False
            self._config(fresh=True)
            if self.assistant is not None:
                self.assistant.use_settings()
            self._update_status()

        def _config(self, fresh=False):
            """The AI settings, read from disk once and again only when they
            were saved (settings_changed) or a question is asked."""
            if fresh or getattr(self, "_cfg", None) is None:
                self._cfg = _C.load()
            return self._cfg

        def _update_status(self):
            cfg = self._config()
            if not cfg.get("enabled"):
                self.banner.setText("The assistant is off. Turn it on, and choose a service, "
                                    "in <a href='#'>AI settings</a>. A model on this PC "
                                    "(Ollama / LM Studio) is free and keeps everything here.")
                self.banner.show()
            else:
                self.banner.hide()
            sid = cfg.get("service") or ""
            if self.assistant is not None and self.assistant._provider is not None:
                p = self.assistant._provider
                svc = "%s · %s" % (p.label, p.model)
            elif sid in _PV.SPECS:
                svc = "%s%s" % (_PV.SPECS[sid].label,
                                " · " + _C.model_for(cfg, sid) if _C.model_for(cfg, sid) else "")
            else:
                svc = "A model on this PC (automatic)"
            self.status.setText("%s%s" % (svc, " · %s tokens this chat" % "{:,}".format(self.tokens)
                                          if self.tokens else ""))

        def _context_tick(self):
            """The timer's look at what the user has selected: only while the
            panel is open (folded or hidden it shows nothing of it)."""
            if self.isVisible() and not self.collapsed and self.job is None:
                self.refresh_context()

        def refresh_context(self):
            ws = self.win.current
            e = None
            if ws is not None and getattr(ws, "primary", None) is not None:
                tab = ("this tab (%s)" % _short(ws.tab_label() if hasattr(ws, "tab_label")
                                                else ws.primary.binfile.name, 40), True)
                try:
                    e = ws._current_entity() or (ws.model.entity if hasattr(ws, "model") else None)
                except Exception:
                    e = None
            else:
                tab = ("this tab (none open)", False)
            if e is not None:
                if getattr(self, "_ctx_entity", (None,))[0] is not e:
                    from .ai.describe import title as _title
                    self._ctx_entity = (e, _title(e))     # the title costs a lookup
                mp = ("the selected parameter (%s)" % _short(self._ctx_entity[1], 40), True)
            else:
                mp = ("the selected parameter (none)", False)
            for box, (text, on) in ((self.use_tab, tab), (self.use_map, mp)):
                if box.text() != text:                     # no relayout when unchanged
                    box.setText(text)
                if box.isEnabled() != on:
                    box.setEnabled(on)

        def _context_text(self) -> str:
            ws = self.win.current
            if ws is None or getattr(ws, "primary", None) is None or not self.use_tab.isChecked():
                return "No tab is shared with you; the user may still name files or ask about the app."
            lw = self._workspace()
            h = lw.tab_for(ws)
            t = lw.get(h)
            lines = ["The user is looking at tab %s (%s): %s." % (
                h, t.label, getattr(ws.primary, "platform", ""))]
            if t.comparison is not None:
                lines.append("It is compared against: %s." % (t.cmp_label or "a comparison"))
            if ws.is_dirty():
                lines.append("It has unsaved edits (you see them).")
            if self.use_map.isChecked() and self.use_map.isEnabled():
                try:
                    e = ws._current_entity() or ws.model.entity
                except Exception:
                    e = None
                if e is not None:
                    from .ai.describe import title as _title
                    lines.append("Selected parameter: \"%s\" (id %s). \"This map\" means it."
                                 % (_title(e), t.index.eid(e)))
            others = [x.handle for x in lw.all() if x.handle != h]
            if others:
                lines.append("Other open targets: %s." % ", ".join(others))
            return " ".join(lines)

        def _workspace(self):
            if getattr(self, "_lw", None) is None:
                from .ai.live import LiveWorkspace
                self._lw = LiveWorkspace(self.win)
                self._lw.ui = WindowUI(self.win)
            return self._lw

        def _make_assistant(self):
            from .ai.assistant import Assistant
            from .ai.bridge import GUI_TOOLS, split_tool
            lw = self._workspace()
            a = Assistant(lw, cfg=_C.load())
            runner = self.st.runner

            def call(name, args):
                if name not in GUI_TOOLS and split_tool(name):
                    # a snapshot of the tab on the GUI thread, the work here
                    return a.registry.call(name, a.ctx, args)
                if name in GUI_TOOLS or (name != "open_file" and lw.is_tab(args.get("handle", ""))):
                    return runner(lambda: a.registry.call(name, a.ctx, args))
                return a.registry.call(name, a.ctx, args)
            a.call_tool = call
            a.ctx.client = "assistant"
            a.ctx.on_gui = runner
            return a

        # -- asking ----------------------------------------------------------------------
        def ask(self, question: str):
            question = (question or "").strip()
            if not question or self.job is not None:
                return
            cfg = self._config(fresh=True)
            if not cfg.get("enabled"):
                self._update_status()
                self.banner.show()
                return
            # turned on before the notice existed (or in the settings file):
            # it is still seen before the first question goes out
            if not confirm_notice(self):
                self.chat.append(("notice", "Nothing was sent. The AI can make mistakes; "
                                  "every change it suggests has to be reviewed by you "
                                  "before it is flashed."))
                self._render()
                return
            if self.assistant is None:
                self.assistant = self._make_assistant()
            self.refresh_context()
            context = self._context_text()
            self.input.clear()
            self.chat.append(("you", question))
            self._activity = "Thinking..."
            self._busy(True)
            self._render()
            self.job = _AskJob(self, self.assistant, question, context)
            self.job.event.connect(self._event)
            self.job.done.connect(self._done)

        def _busy(self, on):
            self.send.setEnabled(not on)
            self.stop.setVisible(on)
            self.activity.setText(self._activity if on else "")

        def _event(self, ev):
            if ev.kind == "delta":
                if self.chat and self.chat[-1][0] == "ai_live":
                    self.chat[-1] = ("ai_live", self.chat[-1][1] + ev.text)
                else:
                    self.chat.append(("ai_live", ev.text))
                if self._activity != "Writing...":
                    self._activity = "Writing..."
                    self.activity.setText(self._activity)
                if not getattr(self, "_render_due", False):
                    self._render_due = True       # at most 4 redraws a second
                    QtCore.QTimer.singleShot(RENDER_MS, self._render_now)
                return
            if self.chat and self.chat[-1][0] == "ai_live" and ev.kind in ("tool", "text"):
                # the streamed text of a turn that goes on to use tools stays
                self.chat[-1] = ("ai_partial", self.chat[-1][1])
                if ev.kind == "text" and ev.data.get("partial"):
                    self._render()
                    return
            if ev.kind == "tool":
                args = ev.data.get("args", {})
                what = args.get("map") or args.get("query") or args.get("title") or \
                    args.get("log") or args.get("page") or args.get("handle") or ""
                self._activity = "%s %s..." % (_TOOL_WORDS.get(ev.text, "Using %s" % ev.text),
                                               _short(str(what), 50))
                self.activity.setText(self._activity)
            elif ev.kind == "text" and ev.data.get("partial"):
                self.chat.append(("ai_partial", ev.text))
                self._render()
            elif ev.kind == "notice":
                self.chat.append(("notice", ev.text))
                self._render()

        def _done(self, reply, err):
            self.job = None
            self._busy(False)
            if getattr(self, "_settings_pending", False):
                self.settings_changed()
            if self.chat and self.chat[-1][0] == "ai_live" and err is not None:
                self.chat[-1] = ("ai_partial", self.chat[-1][1])
            if err is not None:
                from .ai.assistant import NotConfigured
                if isinstance(err, NotConfigured):
                    self.chat.append(("error", str(err)))
                elif isinstance(err, _PV.ProviderError) and err.kind == "cancelled":
                    self.chat.append(("notice", "Stopped."))
                else:
                    self.chat.append(("error", _err_text(err)))
                self._render()
                self._update_status()
                return
            self.tokens += reply.usage.input + reply.usage.output
            if self.chat and self.chat[-1][0] in ("ai_live", "ai_partial") and \
                    self.chat[-1][1].strip() == (reply.text or "").strip():
                self.chat[-1] = ("ai", reply.text)      # streamed: already shown
            else:
                if self.chat and self.chat[-1][0] == "ai_live":
                    self.chat.pop()
                self.chat.append(("ai", reply.text or "(no answer)"))
            for h, mid, title in reply.maps:
                self.chat.append(("map", (h, mid, title)))
            seen_plans = set()
            for pid in reply.proposals:
                if not pid:
                    continue
                try:
                    prop = P.load(pid)
                except Exception:
                    continue
                pl = prop.get("plan")
                if pl:
                    if pl["id"] in seen_plans:
                        continue
                    seen_plans.add(pl["id"])
                    steps = plan_steps(prop, upto=False)
                    self.chat.append(("plan", (pl["title"], len(steps), steps[0]["id"],
                                               steps[-1]["id"])))
                else:
                    self.chat.append(("proposal", (prop.get("title", ""), pid)))
            if reply.redacted:
                self.chat.append(("notice", "%d VIN / immobiliser value(s) were removed "
                                  "before sending." % reply.redacted))
            self._render()
            self._update_status()
            self.st.poll()

        def _stop(self):
            if self.assistant is not None:
                self.assistant.stop()             # a reply being written ends now
            self._activity = "Stopping..."
            self.activity.setText(self._activity)

        def _act(self, what, pid):
            """The links under a proposal in the chat."""
            try:
                prop = P.load(pid)
            except Exception:
                self.chat.append(("notice", "That proposal is no longer pending."))
                self._render()
                return
            if what == "prev":
                pv = getattr(self.st, "preview", None)
                if pv is not None and pv[3] == pid:
                    end_preview(self.st)
                    self.chat.append(("notice", "Preview ended: your comparison is back."))
                else:
                    try:
                        start_preview(self.st, prop)
                        self.chat.append(("notice", "Previewing \"%s\" as the comparison: the "
                                          "table marks every cell it would change. Click "
                                          "Preview again to end it." % prop.get("title", "")))
                    except Exception as ex:
                        self.chat.append(("error", "No preview: %s" % ex))
            elif what in ("acc", "accplan"):
                msg = accept(self.st, prop, self, whole_plan=(what == "accplan"))
                if msg:
                    self.chat.append(("notice", msg))
            elif what in ("rej", "rejplan"):
                reject(self.st, prop, whole_plan=(what == "rejplan"))
                self.chat.append(("notice", "Rejected."))
            self._render()

        def new_chat(self):
            if self.job is not None:
                return
            if self.assistant is not None:
                self.assistant.reset()
            self.chat = []
            self.tokens = 0
            self._render()
            self._update_status()

        # -- showing -----------------------------------------------------------------------
        def _parts(self):
            """The transcript as ``[(text, live)]``, one per block shown:
            Markdown, or -- ``live`` -- the reply being written, as plain text
            (it is shown as Markdown once it is done)."""
            parts = []
            maps = []
            for kind, text in self.chat:
                if kind != "map" and maps:
                    parts.append(("Parameters: " + " · ".join(maps), False))
                    maps = []
                if kind == "you":
                    parts.append(("**You:** " + _md_escape(text), False))
                elif kind == "ai_live":
                    parts.append((text, True))
                elif kind in ("ai", "ai_partial"):
                    parts.append((text, False))
                elif kind == "map":
                    h, mid, title = text
                    maps.append("[%s](%s:%s/%s)" % (_md_escape(title or mid), MAP_SCHEME,
                                                     _q(h, safe=""), _q(mid, safe="")))
                elif kind == "proposal":
                    title, pid = text
                    parts.append(("**Proposal:** %s  \n[Preview](%s:prev/%s) · [Accept](%s:acc/%s)"
                                  " · [Reject](%s:rej/%s) · [Review...](%s:%s)" % (
                                      _md_escape(title), ACT_SCHEME, pid, ACT_SCHEME, pid,
                                      ACT_SCHEME, pid, PROP_SCHEME, pid), False))
                elif kind == "plan":
                    title, n, first, last = text
                    parts.append(("**Plan:** %s (%d step%s)  \n[Preview result](%s:prev/%s) · "
                                  "[Accept plan](%s:accplan/%s) · [Reject plan](%s:rejplan/%s) · "
                                  "[Review steps...](%s:%s)" % (
                                      _md_escape(title), n, "" if n == 1 else "s",
                                      ACT_SCHEME, last, ACT_SCHEME, last, ACT_SCHEME, last,
                                      PROP_SCHEME, first), False))
                elif kind == "notice":
                    parts.append(("_%s_" % _md_escape(text), False))
                elif kind == "error":
                    parts.append(("**Problem:** %s" % _md_escape(text), False))
            if maps:
                parts.append(("Parameters: " + " · ".join(maps), False))
            if not parts:
                parts.append(("_Ask about the open calibration (\"what does this map do?\", "
                              "\"what changed against the original?\", \"is the smoke limiter "
                              "holding back fuel above 3000 rpm?\"), a data log, or how to do "
                              "something in %s. Changes the assistant suggests wait in "
                              "Tools > AI > Proposals for you._" % branding.APP_NAME, False))
            return parts

        def _render(self, full=False):
            """Bring the view up to date with the transcript. Only what
            changed is redrawn: blocks already shown stay, the reply being
            written is replaced as plain text, and the view follows the end
            only when the user had not scrolled up."""
            parts = self._parts()
            shown = self.__dict__.setdefault("_shown", [])
            pos = self.__dict__.setdefault("_pos", [])
            k = 0
            if not full:
                n = min(len(parts), len(shown))
                while k < n and parts[k] == shown[k]:
                    k += 1
                if k == len(parts) == len(shown):
                    return
            sb = self.view.verticalScrollBar()
            at_end = sb.value() >= sb.maximum() - 4
            doc = self.view.document()
            cur = QtGui.QTextCursor(doc)
            if k == 0:
                doc.clear()
            elif k < len(shown):
                cur.setPosition(pos[k])
                cur.movePosition(QtGui.QTextCursor.MoveOperation.End,
                                 QtGui.QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
            del shown[k:], pos[k:]
            for text, live in parts[k:]:
                cur.movePosition(QtGui.QTextCursor.MoveOperation.End)
                pos.append(cur.position())
                if shown:                       # a rule between blocks
                    cur.insertBlock(QtGui.QTextBlockFormat(), QtGui.QTextCharFormat())
                    rule = QtGui.QTextBlockFormat()
                    rule.setProperty(QtGui.QTextFormat.Property.BlockTrailingHorizontalRulerWidth,
                                     QtGui.QTextLength(QtGui.QTextLength.Type.PercentageLength, 100))
                    cur.setBlockFormat(rule)
                    cur.insertBlock(QtGui.QTextBlockFormat(), QtGui.QTextCharFormat())
                if live:
                    cur.insertText(text, QtGui.QTextCharFormat())
                else:
                    md = QtGui.QTextDocument()
                    md.setMarkdown(text)
                    cur.insertFragment(QtGui.QTextDocumentFragment(md))
                shown.append((text, live))
            if at_end:
                sb.setValue(sb.maximum())

        def _render_now(self):
            self._render_due = False
            self._render()

        def _link(self, url):
            s = url.toString()
            if s.startswith(MAP_SCHEME + ":"):
                h, _, mid = s[len(MAP_SCHEME) + 1:].partition("/")
                try:
                    t = self._workspace().get(_uq(h))
                    e = t.index.resolve(_uq(mid))
                    ws = t.ws
                except Exception:
                    return
                self.win.show_workspace(ws)
                ws.show_entity(e)
            elif s.startswith(ACT_SCHEME + ":"):
                self._act(*s[len(ACT_SCHEME) + 1:].split("/", 1))
            elif s.startswith(PROP_SCHEME + ":"):
                pid = s[len(PROP_SCHEME) + 1:]
                self.st.open_proposals()
                dlg = self.st.dialog
                for i, (p, _ws, _n) in enumerate(dlg._props):
                    if p.get("id") == pid:
                        dlg.list.setCurrentRow(i)
                        break
            else:
                QtGui.QDesktopServices.openUrl(url)

    def _short(s: str, n: int) -> str:
        s = str(s)
        return s if len(s) <= n else s[:n - 1] + "…"

    def _md_escape(s: str) -> str:
        return "".join("\\" + ch if ch in "\\`*_[]<>#|" else ch for ch in str(s))

    # ------------------------------------------------------------ entry points
    def assistant_dock(win) -> "AssistantDock":
        st = state(win)
        if getattr(st, "dock", None) is None:
            d = st.dock = AssistantDock(st)
            win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, d)
            st.timer.timeout.connect(d._context_tick)
            cfg = _settings()
            width = int(cfg.value(PANEL_KEY + "width", PANEL_WIDTH) or PANEL_WIDTH)
            folded = _truthy(cfg.value(PANEL_KEY + "collapsed", True))
            visible = _truthy(cfg.value(PANEL_KEY + "visible", True))
            d._restoring = True               # nothing saved while putting it back
            d._width = width
            win.resizeDocks([d], [width], QtCore.Qt.Orientation.Horizontal)
            if folded:
                d.collapse()
            if not visible:
                d.hide()
            d._restoring = False
        return st.dock

    def _truthy(v) -> bool:
        return v in (True, "true", "1", 1)

    def show_assistant(win, question: str = ""):
        d = assistant_dock(win)
        d.show()
        d.expand()
        d.raise_()
        if question:
            d.ask(question)
        else:
            d.input.setFocus()
        return d

    def _entity_ids(ws, e):
        from .ai.describe import title as _title
        lw = assistant_dock(ws.win)._workspace()
        h = lw.tab_for(ws)
        return h, lw.get(h).index.eid(e), _title(e)

    def explain(ws, e):
        h, mid, title = _entity_ids(ws, e)
        show_assistant(ws.win, "Explain the parameter \"%s\" (id %s, tab %s): what it controls, "
                               "its axes and units, how the ECU uses it, and what is notable about "
                               "its current values%s." % (title, mid, h,
                                                          ", compared with the comparison"
                                                          if ws.comparison is not None else ""))

    def what_changed(ws, e=None):
        win = ws.win
        lw = assistant_dock(win)._workspace()
        h = lw.tab_for(ws)
        if e is not None:
            _h, mid, title = _entity_ids(ws, e)
            show_assistant(win, "What changed in \"%s\" (id %s, tab %s) compared with the "
                                "comparison, and what is the likely effect on the car?"
                                % (title, mid, h))
        else:
            show_assistant(win, "Summarise what differs between tab %s and its comparison: "
                                "group the changes by system, explain the significant ones and "
                                "point out any risks." % h)

    def _ai_actions(ws, menu, e):
        menu.addSeparator()
        a = menu.addAction("Ask AI: explain this parameter",
                           lambda: explain(ws, e))
        a.setEnabled(e is not None)
        b = menu.addAction("Ask AI: what changed in it",
                           lambda: what_changed(ws, e))
        b.setEnabled(e is not None and ws.comparison is not None)
        c = menu.addAction("Ask AI: what changed in this file",
                           lambda: what_changed(ws))
        c.setEnabled(ws.comparison is not None)
        if ws.comparison is None:
            for x in (b, c):
                x.setToolTip("Load a comparison first")

    def attach_tree(ws):
        """The parameter tree's right-click menu (it had none)."""
        tree = ws.tree
        tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)

        def menu_at(pos):
            item = tree.itemAt(pos)
            e = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
            if e is not None and not hasattr(e, "rows"):
                e = None
            if getattr(ws, "primary", None) is None:
                return
            m = QtWidgets.QMenu(tree)
            if e is not None:
                m.addAction("Open in a new table tab", lambda: ws.open_table(e, new_tab=True))
            _ai_actions(ws, m, e)
            m.exec(tree.viewport().mapToGlobal(pos))
        tree.customContextMenuRequested.connect(menu_at)

    def extend_menu(ws, menu, e):
        """The table's right-click menu gets the AI actions too."""
        if getattr(ws, "primary", None) is not None:
            _ai_actions(ws, menu, e)

    def summarise_log(win, doc, aid):
        """Project panel: right-click a log > Summarise with AI."""
        a = next((x for x in doc.project.logs if x.id == aid), None)
        if a is None:
            return
        tab = next((t for t in doc.tabs(win) if getattr(t, "primary", None) is not None), None)
        if tab is not None:
            h = assistant_dock(win)._workspace().tab_for(tab)
            q = ("Summarise the data log \"%s\" (log id %s, in the project of tab %s): what was "
                 "driven, the ranges of the key signals, and anything abnormal." % (a.filename, aid, h))
        else:
            import os
            import tempfile
            d = tempfile.mkdtemp(prefix="ovt-ai-log-")
            path = os.path.join(d, os.path.basename(a.filename) or "log.csv")
            with open(path, "wb") as fh:
                fh.write(doc.project.log_bytes(aid))
            q = ("Summarise the data log at path %s: what was driven, the ranges of the key "
                 "signals, and anything abnormal." % path)
        show_assistant(win, q)

    def add_find_button(win):
        """Find panel: "Ask AI" -- the query as a description, not a name."""
        try:
            panel = win.search_dock.panel
            btn_go = panel.btn_go
            lay = btn_go.parentWidget().layout()
        except Exception:
            return None
        b = QtWidgets.QPushButton("Ask AI")
        b.setAutoDefault(False)
        b.setToolTip("Describe what you are looking for in your own words; the AI "
                     "assistant finds the parameters")

        def go():
            q = panel.edit.text().strip()
            if not q:
                show_assistant(win)
                return
            show_assistant(win, "Find the parameters in the open tab that match this "
                                "description: \"%s\". Use find_maps with several phrasings and "
                                "list_categories as needed. Reply with a short list: each "
                                "parameter's title and id, and one line on why it matches." % q)
        b.clicked.connect(go)

        def place(layout):
            if isinstance(layout, QtWidgets.QBoxLayout):
                i = layout.indexOf(btn_go)
                if i >= 0:
                    layout.insertWidget(i + 1, b)
                    return True
            for k in range(layout.count()):
                sub = layout.itemAt(k).layout()
                if sub is not None and place(sub):
                    return True
            if isinstance(layout, QtWidgets.QGridLayout):
                idx = layout.indexOf(btn_go)
                if idx >= 0:
                    r, c, _rs, _cs = layout.getItemPosition(idx)
                    layout.addWidget(b, r, layout.columnCount())
                    return True
            return False
        if not place(lay):
            b.deleteLater()
            return None
        return b


if HAVE_QT:
    import re as _re

    class WindowUI:
        """What the AI may do to the window: show things. Never edits.
        Called on the GUI thread (see bridge.GUI_TOOLS)."""

        def __init__(self, win):
            self.win = win

        def show_map(self, target, e, new_tab=False, cells=()):
            ws = target.ws
            self.win.show_workspace(ws)
            if new_tab:
                ws.open_table(e, new_tab=True)
            ws.show_entity(e)
            if cells:
                self._select(ws, e, cells)

        @staticmethod
        def _select(ws, e, cells):
            want = {(int(r), int(c)) for r, c in cells if isinstance(r, (int, float))}
            model, view = ws.model, ws.table
            if model.entity is not e:
                return
            sel = QtCore.QItemSelection()
            first = None
            for row in range(model.rowCount()):
                for col in range(model.columnCount()):
                    ix = model.index(row, col)
                    if model.z_at(ix) in want:
                        sel.select(ix, ix)
                        first = first or ix
            if first is not None:
                view.selectionModel().select(
                    sel, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
                view.scrollTo(first)

        @staticmethod
        def _label(item) -> str:
            return _re.sub(r"\s*\(\d+\)\s*$", "", item.text(0)).strip()

        def show_category(self, target, category: str):
            ws = target.ws
            tree = ws.tree
            parts = [p.strip().lower() for p in _re.split(r"\s*[/>]\s*", category) if p.strip()]
            if not parts:
                return None
            hits = []

            def walk(item, path):
                for i in range(item.childCount()):
                    ch = item.child(i)
                    if ch.childCount() == 0:
                        continue                    # a parameter, not a category
                    p2 = path + [self._label(ch)]
                    low = [x.lower() for x in p2]
                    if low[-len(parts):] == parts:
                        hits.append((ch, p2))
                    walk(ch, p2)
            walk(tree.invisibleRootItem(), [])
            if not hits:                            # the last part alone, loosely
                def loose(item, path):
                    for i in range(item.childCount()):
                        ch = item.child(i)
                        if ch.childCount() == 0:
                            continue
                        p2 = path + [self._label(ch)]
                        if parts[-1] in p2[-1].lower():
                            hits.append((ch, p2))
                        loose(ch, p2)
                loose(tree.invisibleRootItem(), [])
            if not hits:
                return None
            item, path = hits[0]
            self.win.show_workspace(ws)
            tree.collapseAll()
            p = item
            while p is not None:
                p.setExpanded(True)
                p = p.parent()
            tree.scrollToItem(item, QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
            return " / ".join(path)

        def filter_tree(self, target, text: str) -> int:
            ws = target.ws
            self.win.show_workspace(ws)
            ws.filter.setText(text or "")
            n = 0
            it = QtWidgets.QTreeWidgetItemIterator(ws.tree)
            while it.value() is not None:
                item = it.value()
                if item.childCount() == 0 and not item.isHidden():
                    n += 1
                it += 1
            return n
