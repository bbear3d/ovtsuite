# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The assistant panel without a file open: the transcript is redrawn only
where it changed, every chat is saved after its answer and reopens (also at
start-up), History lists and manages the chats, and the size cap applies.
Offscreen, against a scripted local service."""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Fake:
    """An OpenAI-compatible service that answers every question "Answer n"."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.requests = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.requests.append(body)
                out = json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": "Answer %d" % len(me.requests)},
                    "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_AI_DIR", str(tmp_path / "ai"))
    monkeypatch.setenv("OVT_RECOVERY_DIR", str(tmp_path / "rec"))
    from ovtsuite import gui, gui_ai, gui_ai_chats
    ini = str(tmp_path / "ai-settings.ini")
    monkeypatch.setattr(gui_ai, "_settings",
                        lambda: QtCore.QSettings(ini, QtCore.QSettings.IniFormat))
    monkeypatch.setattr(gui_ai, "_show_notice", lambda parent: True)
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(gui_ai_chats, "_reopened", [False])
    from ovtsuite.ai import config as C
    fake = Fake()
    C.update(enabled=True, service="custom", base_urls={"custom": fake.url},
             models={"custom": "m-1"}, stream=False)
    wins = []

    def window():
        w = gui.MainWindow()
        w.clear_closed()
        w.show()
        wins.append(w)
        return w
    yield window, fake
    fake.close()
    for w in wins:
        gui_ai.state(w).stop_bridge()
        w.close_discarding()


def _wait(dock, secs=30):
    end = time.time() + secs
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if dock.job is None:
            return
        time.sleep(0.01)
    raise AssertionError("the assistant did not finish")


def _ask(d, q):
    d.ask(q)
    _wait(d)
    d.history.store.flush()
    QtWidgets.QApplication.processEvents()


def test_transcript_redraws_only_what_changed(env):
    from ovtsuite import gui_ai
    window, _fake = env
    d = gui_ai.show_assistant(window())
    d.chat = [("you", "hi *there*"), ("ai", "**bold**\n\n- a\n- b")]
    d._render()
    first = d.view.document().firstBlock().text()
    pos = list(d._pos)
    d.chat.append(("ai_live", "being *written"))
    d._render()
    assert d._pos[:2] == pos and d._shown[-1] == ("being *written", True)
    assert d.view.toPlainText().endswith("being *written")       # plain while live
    d.chat[-1] = ("ai", "being *written* now")
    d._render()
    assert "*written* now" in d.view.toMarkdown()                 # Markdown once done
    assert d.view.document().firstBlock().text() == first
    # scrolled up: the view stays where the user put it
    d.chat += [("notice", "line %d" % i) for i in range(200)]
    d._render()
    sb = d.view.verticalScrollBar()
    assert sb.value() == sb.maximum()
    sb.setValue(0)
    d.chat.append(("notice", "one more"))
    d._render()
    assert sb.value() == 0


def test_chats_are_saved_and_reopen(env):
    from ovtsuite import gui_ai
    window, fake = env
    w = window()
    d = gui_ai.show_assistant(w)
    _ask(d, "First question about boost")
    _ask(d, "And a follow-up")
    ms = d.history.store.list()
    assert len(ms) == 1 and ms[0]["title"] == "First question about boost"
    assert ms[0]["messages"] == 4 and ms[0]["model"] == "m-1"
    first = ms[0]["id"]
    d.new_chat()
    assert d.chat == [] and d.history.cid is None
    _ask(d, "Something else")
    assert len(d.history.store.list()) == 2
    # reopen the first: the transcript, the conversation and a notice
    assert d.history.open(first)
    kinds = [k for k, _v in d.chat]
    assert kinds == ["you", "ai", "you", "ai", "restored"]
    assert len(d.assistant.messages) == 4 and d.assistant.restored
    assert "reopened" in d.view.toPlainText()
    n = len(fake.requests)
    _ask(d, "Carry on")
    assert len(fake.requests[n]["messages"]) >= 6    # the earlier turns went too
    assert [m["title"] for m in d.history.store.list()][0] == "First question about boost"
    saved = d.history.store.load(first)
    assert all(k != "restored" for k, _v in saved["chat"])
    # the History menu lists both, the open one ticked
    d.history._fill_menu()
    acts = [a for a in d.history.menu.actions() if a.isCheckable()]
    assert len(acts) == 2 and [a.isChecked() for a in acts] == [True, False]


def test_last_chat_reopens_at_start(env):
    from ovtsuite import gui_ai, gui_ai_chats
    window, _fake = env
    d = gui_ai.show_assistant(window())
    _ask(d, "Remember me")
    gui_ai_chats._reopened[0] = False                  # as if the app started again
    d2 = gui_ai.show_assistant(window())
    assert [k for k, _v in d2.chat] == ["you", "ai", "restored"]
    assert d2.history.cid == d.history.cid


def test_history_dialog_rename_delete_clear(env, monkeypatch):
    from ovtsuite import gui_ai
    from ovtsuite.gui_ai_chats import ChatHistoryDialog
    window, _fake = env
    d = gui_ai.show_assistant(window())
    _ask(d, "one")
    d.new_chat()
    _ask(d, "two")
    dlg = ChatHistoryDialog(d.history)
    dlg.reload()
    assert dlg.list.count() == 2
    cid = dlg._current_id()
    assert cid == d.history.cid                        # the open chat is selected
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Renamed", True)))
    dlg._rename()
    assert "Renamed" in dlg.list.item(0).text()
    dlg._delete()                                      # the open one: the panel starts afresh
    assert dlg.list.count() == 1 and d.chat == [] and d.history.cid is None
    dlg._clear()
    assert dlg.list.count() == 0 and d.history.store.list() == []


def test_cap_option_applies(env, monkeypatch):
    from ovtsuite import gui_ai, gui_ai_chats, options
    window, _fake = env
    assert options.get("ai/chat_history_mb") == 50
    monkeypatch.setattr(gui_ai_chats, "cap_bytes", lambda: 1)
    d = gui_ai.show_assistant(window())
    _ask(d, "one")
    d.new_chat()
    _ask(d, "two")
    # over the cap: only the open chat is left
    assert [m["title"] for m in d.history.store.list()] == ["two"]
