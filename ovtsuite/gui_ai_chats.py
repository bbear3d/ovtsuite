# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The assistant panel's chat history: every chat is saved after each
answer and can be reopened and carried on (:mod:`.ai.chats` keeps them).

* **History** beside **New chat**: the recent chats (those about the active
  tab's project marked), and **All chats...** -- open, rename, delete, or
  clear them all.
* The last chat reopens when the app starts.
* Options > AI > **Maximum saved chat history**: above it the chats used
  longest ago are deleted, never the open one.
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from .ai import chats as _CH

#: chats listed in the History menu itself (the rest: All chats...)
MENU_MAX = 15
#: what a reopened chat says first
RESTORED = ("Chat reopened. Tabs it referred to before may no longer be open; "
            "the assistant looks them up again when it needs them.")

_reopened = [False]           # the last chat reopens in the first window only


def cap_bytes() -> int:
    try:
        from . import options as _opt
        mb = int(_opt.get("ai/chat_history_mb"))
    except Exception:
        mb = _CH.DEFAULT_CAP_MB
    return max(1, mb) << 20


def _when(ts: str) -> str:
    """"14:05" today, "12 Oct 14:05" this year, else the date."""
    try:
        t = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return ts or ""
    now = time.localtime()
    if t[:3] == now[:3]:
        return time.strftime("%H:%M", t)
    if t.tm_year == now.tm_year:
        return time.strftime("%d %b %H:%M", t)
    return time.strftime("%d %b %Y", t)


def _model(m: dict) -> str:
    from .ai import providers as _PV
    sid = m.get("service") or ""
    label = _PV.SPECS[sid].label if sid in _PV.SPECS else sid
    return " · ".join(x for x in (label, m.get("model") or "") if x)


def describe(m: dict) -> str:
    return "%s  —  %s%s" % (m.get("title") or "Chat", _when(m.get("updated", "")),
                                  ("  ·  " + _model(m)) if _model(m) else "")


class ChatHistory(QtCore.QObject):
    """Per panel: the open chat's identity, saving it, reopening others."""

    saved = QtCore.Signal(object, object)         # (meta, removed ids) -- GUI thread

    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.cid = None                            # the open chat, once saved
        self.created = ""
        self.title = ""
        self.project = ("", "")                    # (uid, name) it is about
        self.dialog = None
        b = self.button = QtWidgets.QToolButton()
        b.setText("History")
        b.setAutoRaise(True)
        b.setToolTip("Reopen an earlier chat, or rename and delete them")
        b.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QtWidgets.QMenu(b)
        self.menu.aboutToShow.connect(self._fill_menu)
        b.setMenu(self.menu)
        self.saved.connect(self._saved, QtCore.Qt.ConnectionType.QueuedConnection)

    @property
    def store(self):
        return _CH.store()

    # -- what the active tab is about ----------------------------------------------
    def current_project(self):
        ws = getattr(self.dock.win, "current", None)
        ref = getattr(ws, "project_ref", None) if ws is not None else None
        if ref is None:
            return "", ""
        try:
            p = ref.doc.project
            return p.uid or "", p.name or getattr(ref.doc, "title", "") or ""
        except Exception:
            return "", ""

    # -- the open chat ---------------------------------------------------------------
    def begin(self, question: str) -> None:
        """A question is being asked: the first of a chat names it."""
        if self.cid is None:
            self.cid = _CH.new_id()
            self.created = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.title = _CH.title_of(question)
            self.project = self.current_project()

    def forget_current(self) -> None:
        """New chat: the next question starts another saved chat."""
        self.cid = None
        self.created = self.title = ""
        self.project = ("", "")

    def save(self) -> None:
        """After an answer (or a stop / an error): the chat as it is now,
        written on a worker thread; old chats over the size cap go."""
        d = self.dock
        if self.cid is None or not any(k == "you" for k, _v in d.chat):
            return
        a = d.assistant
        service = model = ""
        p = getattr(a, "_provider", None) if a is not None else None
        if p is not None:
            service, model = p.spec.id, p.model or ""
        else:
            service, model = getattr(self, "_svc", ("", ""))
        self._svc = (service, model)
        chat = [(k, v) for k, v in d.chat if k not in ("restored", "ai_live")]
        rec = _CH.record(a.messages if a is not None else [], chat, cid=self.cid,
                         title=self.title, created=self.created, service=service,
                         model=model, project_uid=self.project[0],
                         project_name=self.project[1], tokens=d.tokens)
        self.store.save_async(rec, cap_bytes=cap_bytes(), keep=[self.cid],
                              done=lambda m, removed, err: self.saved.emit(m, removed))

    def _saved(self, meta, removed):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.reload()

    def open(self, cid: str) -> bool:
        d = self.dock
        if d.job is not None:
            return False
        try:
            self.store.flush()
            rec = self.store.load(cid)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(d, "Chat history",
                                          "That chat could not be opened:\n%s" % ex)
            return False
        if d.assistant is None:
            d.assistant = d._make_assistant()
        a = d.assistant
        a.messages[:] = _CH.messages_from_json(rec.get("messages"))
        a.restored = True
        a.seen_maps = {}
        d.chat = _CH.chat_from_json(rec.get("chat")) + [("restored", RESTORED)]
        d.tokens = int(rec.get("tokens") or 0)
        self.cid = cid
        self.created = rec.get("created") or ""
        self.title = rec.get("title") or "Chat"
        self.project = (rec.get("project_uid") or "", rec.get("project_name") or "")
        self._svc = (rec.get("service") or "", rec.get("model") or "")
        d._render(full=True)
        d._update_status()
        return True

    def open_last(self) -> None:
        """At start-up: the chat used last, as it was left."""
        if _reopened[0]:
            return
        _reopened[0] = True
        try:
            m = self.store.last()
        except Exception:
            m = None
        if m is not None:
            self.open(m["id"])

    # -- the menu ------------------------------------------------------------------
    def _fill_menu(self):
        menu = self.menu
        menu.clear()
        try:
            ms = self.store.list()
        except Exception:
            ms = []
        uid = self.current_project()[0]
        if not ms:
            a = menu.addAction("No saved chats yet")
            a.setEnabled(False)
        for m in ms[:MENU_MAX]:
            text = describe(m)
            if uid and m.get("project_uid") == uid:
                text = "★ " + text                # about this project
            a = menu.addAction(text.replace("&", "&&"))
            a.setCheckable(True)
            a.setChecked(m["id"] == self.cid)
            a.setEnabled(self.dock.job is None)
            a.triggered.connect(lambda _c=False, cid=m["id"]: self.open(cid))
        menu.addSeparator()
        menu.addAction("All chats...", self.show_dialog)

    def show_dialog(self):
        if self.dialog is None:
            self.dialog = ChatHistoryDialog(self)
        self.dialog.reload()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


class ChatHistoryDialog(QtWidgets.QDialog):
    """History > All chats...: open, rename, delete, clear."""

    def __init__(self, hist: ChatHistory):
        super().__init__(hist.dock)
        self.hist = hist
        self.setWindowTitle("Chat history")
        self.resize(640, 460)
        lay = QtWidgets.QVBoxLayout(self)
        self.only = QtWidgets.QCheckBox()
        self.only.toggled.connect(self.reload)
        lay.addWidget(self.only)
        self.list = QtWidgets.QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._open())
        self.list.currentRowChanged.connect(lambda _r: self._enable())
        lay.addWidget(self.list, 1)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        row = QtWidgets.QHBoxLayout()
        self.b_open = QtWidgets.QPushButton("Open")
        self.b_open.clicked.connect(self._open)
        self.b_rename = QtWidgets.QPushButton("Rename...")
        self.b_rename.clicked.connect(self._rename)
        self.b_delete = QtWidgets.QPushButton("Delete")
        self.b_delete.clicked.connect(self._delete)
        self.b_clear = QtWidgets.QPushButton("Clear all chats...")
        self.b_clear.clicked.connect(self._clear)
        for b in (self.b_open, self.b_rename, self.b_delete):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.b_clear)
        lay.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._chats = []

    def reload(self):
        h = self.hist
        uid, name = h.current_project()
        self.only.setText("Only chats about this project (%s)" % name if uid
                          else "Only chats about this project (the active tab is not in one)")
        self.only.setEnabled(bool(uid))
        cur = self._current_id() or h.cid
        try:
            ms = h.store.list(uid if (uid and self.only.isChecked()) else None)
            total = h.store.total_size()
        except Exception:
            ms, total = [], 0
        self._chats = ms
        self.list.clear()
        for m in ms:
            text = describe(m)
            if m.get("project_name"):
                text += "\n    about %s" % m["project_name"]
            if m["id"] == h.cid:
                text = "▸ " + text + "  (open)"
            it = QtWidgets.QListWidgetItem(text)
            it.setToolTip("%d messages, %s KB" % (m.get("messages", 0),
                                                  "{:,}".format(int(m.get("size", 0)) // 1024 or 1)))
            self.list.addItem(it)
        ids = [m["id"] for m in ms]
        self.list.setCurrentRow(ids.index(cur) if cur in ids else (0 if ids else -1))
        self.info.setText("%d saved chat%s, %.1f MB of the %d MB kept (Options > AI)." % (
            len(ms), "" if len(ms) == 1 else "s", total / (1 << 20), cap_bytes() >> 20))
        self._enable()

    def _current_id(self):
        i = self.list.currentRow()
        return self._chats[i]["id"] if 0 <= i < len(self._chats) else None

    def _enable(self):
        has = self._current_id() is not None
        idle = self.hist.dock.job is None
        self.b_open.setEnabled(has and idle)
        self.b_rename.setEnabled(has)
        self.b_delete.setEnabled(has and (idle or self._current_id() != self.hist.cid))
        self.b_clear.setEnabled(bool(self._chats) and idle)

    def _open(self):
        cid = self._current_id()
        if cid and self.hist.open(cid):
            self.accept()

    def _rename(self):
        cid = self._current_id()
        if not cid:
            return
        m = next(m for m in self._chats if m["id"] == cid)
        text, ok = QtWidgets.QInputDialog.getText(self, "Rename chat", "Name:",
                                                  text=m.get("title", ""))
        if not ok or not text.strip():
            return
        self.hist.store.flush()
        self.hist.store.rename(cid, text)
        if cid == self.hist.cid:
            self.hist.title = _CH.title_of(text, 200)
        self.reload()

    def _delete(self):
        cid = self._current_id()
        if not cid:
            return
        self.hist.store.flush()
        self.hist.store.delete(cid)
        if cid == self.hist.cid:
            self.hist.dock.new_chat()                # the open one is gone: start afresh
        self.reload()

    def _clear(self):
        if QtWidgets.QMessageBox.question(
                self, "Clear all chats",
                "Delete every saved chat? This cannot be undone.") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.hist.store.flush()
        self.hist.store.clear()
        self.hist.dock.new_chat()
        self.reload()
