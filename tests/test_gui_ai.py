# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > AI in the window: the pending-proposal button, previewing a
proposal as the tab's comparison, accepting it into a project as its own
version, rejecting, and sharing open tabs over the bridge. Offscreen."""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

BIN = os.path.join(BINARIES, "038906016Q.Bin")
SMOKE = "FlMng_qPresSmoke_MAP"


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    if not os.path.exists(BIN):
        pytest.skip("reference binary missing: %s" % BIN)
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv("OVT_AI_DIR", str(tmp_path / "ai"))
    monkeypatch.setenv("OVT_RECOVERY_DIR", str(tmp_path / "rec"))
    from ovtsuite import gui, gui_ai
    ini = str(tmp_path / "ai-settings.ini")
    monkeypatch.setattr(gui_ai, "_settings",
                        lambda: QtCore.QSettings(ini, QtCore.QSettings.IniFormat))
    # the "AI can make mistakes" notice is modal: answered "I understand"
    # unless a test says otherwise, and every showing is recorded
    notices = []
    gui._notices = notices
    gui._notice_answer = [True]
    monkeypatch.setattr(gui_ai, "_show_notice",
                        lambda parent: notices.append(parent) or gui._notice_answer[0])
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a) or 0))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    gui._told = told
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    from ovtsuite import gui_ai
    gui_ai.state(w).stop_bridge()
    w.close_discarding()


def _project(tmp_path):
    from ovtsuite.project.importers import import_file
    proj, _s = import_file(BIN)
    return proj.save(str(tmp_path / "car.ovt"))


def _propose(win, title="smoke +1", add=1.0):
    from ovtsuite.ai.live import LiveWorkspace
    from ovtsuite.ai.tools import Context, default_registry
    ctx = Context(LiveWorkspace(win), client="mcp:pytest")
    ok, r = default_registry().call("propose_change", ctx, {
        "handle": "t1", "title": title, "rationale": "because",
        "changes": [{"map": SMOKE, "add": add}]})
    assert ok, r
    return r["proposal"]


def _smoke(ws):
    return next(e for e in ws.primary.entities if e.name == SMOKE)


def test_accept_into_project_is_its_own_version(win, tmp_path):
    from ovtsuite import gui_ai
    from ovtsuite.ai import proposals as P
    assert win.open_path(_project(tmp_path))
    ws = win.current
    assert ws.project_ref is not None
    before = _smoke(ws).values()
    _propose(win)
    st = gui_ai.state(win)
    st.poll()
    assert st.button.isVisibleTo(win) and st.button.text() == "AI: 1 proposal"
    st.open_proposals()
    dlg = st.dialog
    assert dlg.list.count() == 1 and dlg.accept_btn.isEnabled()
    assert dlg.maps.rowCount() == 1

    old = ws.comparison
    dlg.preview_btn.setChecked(True)
    assert ws.comparison is not old
    assert ws.comparison.binfile.name.startswith("AI proposal")
    pe = next(e for e in ws.comparison.entities if e.name == SMOKE)
    assert pe.values()[0][0] == pytest.approx(before[0][0] + 1, abs=0.02)
    assert _smoke(ws).values() == before          # previewing writes nothing
    dlg.preview_btn.setChecked(False)
    assert ws.comparison is old

    dlg._accept()
    assert _smoke(ws).values()[0][0] == pytest.approx(before[0][0] + 1, abs=0.02)
    assert not ws.is_dirty()                      # saved as a version
    s = ws.project_ref.set
    head = s.checkpoint(ws.project_ref.branch.head)
    assert head.label == "AI: smoke +1"
    assert P.all_pending() == []
    st.poll()
    assert not st.button.isVisibleTo(win)


def test_reject_and_unticked_maps(win, tmp_path):
    from ovtsuite import gui_ai
    from ovtsuite.ai import proposals as P
    assert win.open_path(BIN)
    _propose(win, "first")
    st = gui_ai.state(win)
    st.open_proposals()
    dlg = st.dialog
    dlg._reject()
    assert P.all_pending() == []
    assert os.listdir(os.path.join(P.inbox_dir(), "rejected"))
    # a plain-file tab: accepted as an unsaved edit, not saved anywhere
    _propose(win, "second")
    st.poll()
    dlg.reload()
    before = _smoke(win.current).values()
    dlg.maps.item(0, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
    dlg._accept()                                  # nothing ticked: refused
    assert _smoke(win.current).values() == before and P.all_pending()
    dlg.maps.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
    dlg._accept()
    assert win.current.is_dirty()
    win.current.primary.undo()
    assert _smoke(win.current).values() == before


def test_sharing_tabs_over_the_bridge(win):
    from ovtsuite import gui_ai
    from ovtsuite.ai import bridge as B
    assert win.open_path(BIN)
    st = gui_ai.state(win)
    assert st.bridge is None and B.read_live() is None   # opt-in
    st.set_share(True)
    assert B.read_live() is not None
    out = {}

    def client():
        c = B.BridgeClient(client="pytest")
        out["view"] = c.call("current_view", {})
        out["open"] = c.call("list_open", {})
        c.close()
    th = threading.Thread(target=client)
    th.start()
    for _ in range(600):                            # the GUI thread answers
        QtWidgets.QApplication.processEvents()
        th.join(0.02)
        if not th.is_alive():
            break
    assert not th.is_alive()
    ok, view = out["view"]
    assert ok and view["live"] and view["active_tab"] == "t1"
    ok, lst = out["open"]
    assert "038906016Q" in lst["open"][0]["label"]
    st.set_share(False)
    assert B.read_live() is None
    assert gui_ai.share_enabled() is False


def test_ai_settings_dialog_stores_key_and_lists_models(win, monkeypatch, tmp_path):
    """Tools > AI > Settings...: the key goes to the key store (never the
    settings file), the model list comes from the service itself, and the
    request shows in What was sent."""
    from ovtsuite import gui_ai
    from ovtsuite.ai import assistant as A
    from ovtsuite.ai import config as C
    from ovtsuite.ai import keystore as K
    sys.path.insert(0, os.path.dirname(__file__))
    from test_ai_providers import Fake
    monkeypatch.setenv("OVT_AI_KEYSTORE", "file")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    A.SENT.clear()
    fake = Fake("openai")
    try:
        dlg = gui_ai.AiSettingsDialog(gui_ai.state(win))
        assert dlg.service.currentData() == "" and not dlg.model.isEnabled()
        dlg.service.setCurrentIndex(dlg.service.findData("groq"))
        assert "free" in dlg.note.text().lower()
        dlg.key.setText("gsk-secret")
        dlg._key_edited("gsk-secret")
        dlg.base.setText(fake.url + "/v1")
        dlg._list_models()
        for _ in range(300):
            QtWidgets.QApplication.processEvents()
            if dlg.model.count() == 2:
                break
            threading.Event().wait(0.01)
        assert [dlg.model.itemText(i) for i in range(dlg.model.count())] == ["m-1", "m-2:free"]
        dlg.enabled.setChecked(True)
        dlg._ok()
    finally:
        fake.close()
    cfg = C.load()
    assert cfg["enabled"] and cfg["service"] == "groq" and cfg["models"]["groq"] == "m-1"
    assert cfg["base_urls"]["groq"] == fake.url + "/v1"
    assert K.get_key("groq") == "gsk-secret"
    assert "gsk-secret" not in open(C.path()).read()
    # reopened: the key is shown as stored, not as text
    dlg = gui_ai.AiSettingsDialog(gui_ai.state(win))
    assert dlg.service.currentData() == "groq" and dlg.key.text() == ""
    assert "stored" in dlg.key.placeholderText()
    log = gui_ai.SentLogDialog(win)
    assert log.list.count() >= 1 and "gsk-secret" not in log.body.toPlainText()
    assert "/v1/models" in log.list.item(0).text()


# -- phase 4: the assistant panel ------------------------------------------------

class Scripted:
    """An OpenAI-compatible stand-in that answers from a script: each item is
    either ("tool", name, args) or ("text", reply)."""

    def __init__(self, script):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.script, self.requests = list(script), []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.requests.append(body)
                step = me.script.pop(0) if me.script else ("text", "done")
                if step[0] == "tool":
                    msg = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "c%d" % len(me.requests), "type": "function",
                         "function": {"name": step[1], "arguments": json.dumps(step[2])}}]}
                else:
                    msg = {"role": "assistant", "content": step[1]}
                out = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}],
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


def _use(fake):
    from ovtsuite.ai import config as C
    C.update(enabled=True, service="custom", base_urls={"custom": fake.url},
             models={"custom": "m-1"})


def _wait(dock, secs=60):
    import time
    end = time.time() + secs
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if dock.job is None:
            return
        time.sleep(0.01)
    raise AssertionError("the assistant did not finish")


def test_assistant_off_until_enabled(win):
    from ovtsuite import gui_ai
    d = gui_ai.show_assistant(win)
    assert d.banner.isVisibleTo(d) and "off" in d.banner.text()
    d.ask("hello")
    assert d.job is None and d.chat == []            # nothing sent


def test_assistant_reads_the_selected_map_and_links_it(win):
    from ovtsuite import gui_ai
    assert win.open_path(BIN)
    ws = win.current
    e = _smoke(ws)
    ws.show_entity(e)
    fake = Scripted([("tool", "get_map", {"handle": "t1", "map": SMOKE}),
                     ("text", "This is the Max fuel vs. boost (Smoke Limiter, MAP) table.")])
    try:
        _use(fake)
        d = gui_ai.show_assistant(win)
        d.refresh_context()
        assert d.use_map.isEnabled()
        d.ask("What does this map do?")
        _wait(d)
    finally:
        fake.close()
    first, second = fake.requests
    # what the user is looking at travels with the question; the system
    # prompt stays the same for the whole chat (a changing one invalidated
    # the earlier answers' thinking signatures, and defeats prompt caching)
    system = first["messages"][0]["content"]
    question = first["messages"][1]["content"]
    assert "t1" in question and SMOKE in question
    assert SMOKE not in system
    assert second["messages"][-1]["role"] == "tool"
    assert '"values"' in second["messages"][-1]["content"]   # the real tool ran
    kinds = [k for k, _t in d.chat]
    assert kinds == ["you", "ai", "map"]
    assert "ovtmap:t1/" in d.view.toMarkdown()
    # the link opens the map in its tab
    other = next(x for x in ws.primary.entities if x.kind == "map" and x is not e)
    ws.show_entity(other)
    d._link(QtCore.QUrl("ovtmap:t1/" + SMOKE))
    assert ws.model.entity is e
    assert d.tokens == 30


def test_assistant_proposal_lands_in_the_review_list(win):
    from ovtsuite import gui_ai
    from ovtsuite.ai import proposals as P
    assert win.open_path(BIN)
    fake = Scripted([("tool", "propose_change", {
        "handle": "t1", "title": "smoke +1", "rationale": "test",
        "changes": [{"map": SMOKE, "add": 1}]}), ("text", "Proposed.")])
    try:
        _use(fake)
        d = gui_ai.show_assistant(win)
        d.ask("Raise the smoke limit a little")
        _wait(d)
    finally:
        fake.close()
    (p,) = P.all_pending()
    assert p["client"] == "assistant"
    assert ("proposal", (p["title"], p["id"])) in d.chat
    d._link(QtCore.QUrl("ovtprop:" + p["id"]))
    assert gui_ai.state(win).dialog.isVisible()
    assert gui_ai.state(win).button.text() == "AI: 1 proposal"


def test_context_actions_and_find_button(win, tmp_path):
    from ovtsuite import gui_ai
    assert win.open_path(BIN)
    ws = win.current
    btn = [b for b in win.search_dock.panel.findChildren(QtWidgets.QPushButton)
           if b.text() == "Ask AI"]
    assert len(btn) == 1
    fake = Scripted([("text", "It limits fuel.")])
    try:
        _use(fake)
        gui_ai.explain(ws, _smoke(ws))
        d = gui_ai.assistant_dock(win)
        _wait(d)
    finally:
        fake.close()
    q = fake.requests[0]["messages"][-1]["content"]
    assert SMOKE in q and "t1" in q
    menu = QtWidgets.QMenu()
    gui_ai.extend_menu(ws, menu, _smoke(ws))
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "Ask AI: explain this parameter" in texts
    changed = next(a for a in menu.actions() if a.text() == "Ask AI: what changed in it")
    assert not changed.isEnabled()                   # no comparison loaded


def test_summarise_a_project_log(win, tmp_path):
    import glob
    from ovtsuite import gui_ai, gui_project
    logs = sorted(glob.glob(os.path.join(os.path.dirname(BINARIES), "Logs", "*.csv")))
    if not logs:
        pytest.skip("no reference logs")
    from ovtsuite.project.importers import import_file
    proj, _s = import_file(BIN)
    with open(logs[0], "rb") as fh:
        att = proj.add_log(os.path.basename(logs[0]), fh.read())
    path = proj.save(str(tmp_path / "car.ovt"))
    assert win.open_path(path)
    doc = win.current.project_ref.doc
    fake = Scripted([("tool", "log_summary", {"handle": "t1", "log": att.id}),
                     ("text", "A gentle drive.")])
    try:
        _use(fake)
        gui_ai.summarise_log(win, doc, att.id)
        _wait(gui_ai.assistant_dock(win))
    finally:
        fake.close()
    res = json.loads(fake.requests[1]["messages"][-1]["content"])
    assert res["rows"] > 0 and res["channels"] and "values" not in res
    assert len(fake.requests[1]["messages"][-1]["content"]) < 20000   # a summary, not the log


# -- phase 5: the right-hand panel, window tools, plans ---------------------------

def test_panel_folds_opens_and_remembers(G, win):
    from ovtsuite import gui_ai
    d = gui_ai.assistant_dock(win)
    assert win.dockWidgetArea(d) == QtCore.Qt.DockWidgetArea.RightDockWidgetArea
    assert d.isVisible() and d.collapsed                      # folded at first
    assert d.stack.currentWidget() is d.rail and d.maximumWidth() == gui_ai.RAIL_WIDTH
    d.toggle()                                                # open
    assert not d.collapsed and d.stack.currentWidget() is d.content
    assert d.maximumWidth() > 1000                            # resizable again
    d.toggle()                                                # fold
    assert d.collapsed
    d.expand()
    st = gui_ai._settings()
    assert gui_ai._truthy(st.value("ai/panel_visible")) and \
        not gui_ai._truthy(st.value("ai/panel_collapsed"))
    # a new window comes back the way it was left
    w2 = G.MainWindow()
    try:
        d2 = gui_ai.assistant_dock(w2)
        assert not d2.collapsed
    finally:
        w2.close_discarding()
    d.hide()
    QtWidgets.QApplication.processEvents()
    assert not gui_ai._truthy(gui_ai._settings().value("ai/panel_visible"))


def test_assistant_pulls_up_tables_and_navigates(win):
    from ovtsuite import gui_ai
    assert win.open_path(BIN)
    ws = win.current
    fake = Scripted([("tool", "show_category", {"handle": "t1", "category": "Smoke limit"}),
                     ("tool", "show_map", {"handle": "t1", "map": SMOKE, "cells": [[0, 1]]}),
                     ("tool", "filter_tree", {"handle": "t1", "text": "smoke"}),
                     ("text", "Here it is.")])
    try:
        _use(fake)
        d = gui_ai.show_assistant(win)
        d.ask("Show me the smoke limiter")
        _wait(d)
    finally:
        fake.close()
    results = [json.loads(m["content"]) for r in fake.requests for m in r["messages"]
               if m["role"] == "tool"]
    assert results[0]["shown"].endswith("Smoke limit")
    assert ws.model.entity is _smoke(ws)
    sel = {ws.model.z_at(ix) for ix in ws.table.selectionModel().selectedIndexes()}
    assert sel == {(0, 1)}
    assert ws.filter.text() == "smoke" and results[-1]["visible_parameters"] > 0


def test_plan_from_chat_accepted_step_by_step_as_versions(win, tmp_path):
    from ovtsuite import gui_ai
    from ovtsuite.ai import proposals as P
    proj_path = _project(tmp_path)
    assert win.open_path(proj_path)
    ws = win.current
    before = _smoke(ws).values()
    fake = Scripted([("tool", "propose_plan", {
        "handle": "t1", "title": "Stage 1", "goal": "a bit more fuel",
        "steps": [{"title": "smoke +1", "rationale": "a", "changes": [{"map": SMOKE, "add": 1}]},
                  {"title": "smoke +1 again", "rationale": "b",
                   "changes": [{"map": SMOKE, "add": 1}]}]}),
        ("text", "Planned in two steps.")])
    try:
        _use(fake)
        d = gui_ai.show_assistant(win)
        d.ask("Plan a little more fuel")
        _wait(d)
    finally:
        fake.close()
    plan = [t for k, t in d.chat if k == "plan"]
    assert len(plan) == 1 and plan[0][0] == "Stage 1" and plan[0][1] == 2
    md = d.view.toMarkdown()
    assert "Accept plan" in md and "Preview result" in md
    last = plan[0][3]
    d._act("prev", last)                                    # preview: both steps
    pe = next(x for x in ws.comparison.entities if x.name == SMOKE)
    assert pe.values()[0][0] == pytest.approx(before[0][0] + 2, abs=0.05)
    d._act("prev", last)                                    # preview off
    assert not getattr(ws.comparison, "binfile", None) or \
        not ws.comparison.binfile.name.startswith("AI proposal")
    d._act("accplan", last)
    assert P.all_pending() == []
    assert _smoke(ws).values()[0][0] == pytest.approx(before[0][0] + 2, abs=0.05)
    s = ws.project_ref.set
    labels = [c.label for c in s.lineage(ws.project_ref.branch.head)[:2]]
    assert labels == ["AI: Stage 1 - 2/2: smoke +1 again", "AI: Stage 1 - 1/2: smoke +1"]
    assert not ws.is_dirty()


def test_reply_streams_into_the_panel(win):
    from ovtsuite import gui_ai
    sys.path.insert(0, os.path.dirname(__file__))
    from test_ai_providers import StreamFake
    from ovtsuite.ai import config as C
    assert win.open_path(BIN)
    fake = StreamFake("openai")
    try:
        C.update(enabled=True, service="custom", base_urls={"custom": fake.url + "/v1"},
                 models={"custom": "m-1"})
        d = gui_ai.show_assistant(win)
        seen = []
        orig = d._event
        d._event = lambda ev: (seen.append(ev.kind), orig(ev))
        d.job = None
        d.ask("hi")
        d.job.event.disconnect()
        d.job.event.connect(d._event)
        _wait(d)
    finally:
        fake.close()
    assert "delta" in seen
    assert [k for k, _t in d.chat] == ["you", "ai"]          # one answer, not two
    assert d.chat[-1][1] == "All good."


def test_settings_from_the_menu_reach_an_open_panel(win, monkeypatch):
    """Tools > AI > Settings... (not only the panel's own button) switches
    the service of a chat already under way; during an answer it waits."""
    from ovtsuite import gui_ai
    from ovtsuite.ai import config as C
    d = gui_ai.show_assistant(win)
    calls = []

    class Asst:
        _provider = None

        def use_settings(self, cfg=None):
            calls.append("switched")
    d.assistant = Asst()
    C.update(enabled=True, service="gemini")
    dlg = gui_ai.AiSettingsDialog(gui_ai.state(win))
    dlg.service.setCurrentIndex(dlg.service.findData("anthropic"))
    dlg._ok()
    assert calls == ["switched"]
    assert C.load()["service"] == "anthropic"
    assert d.status.text().startswith("Claude")
    d.job = object()                                   # an answer is being written
    gui_ai.AiSettingsDialog(gui_ai.state(win))._ok()
    assert calls == ["switched"]
    d._done(None, RuntimeError("x"))                  # ...and has ended
    assert calls == ["switched", "switched"]


def test_the_notice_comes_first_and_only_once(win, G):
    """Bear: when AI is first turned on, say that it can make mistakes and
    that its changes are always reviewed before flashing."""
    from ovtsuite import gui_ai
    from ovtsuite.ai import config as C
    C.update(enabled=False, notice_accepted=0)
    st = gui_ai.state(win)
    # declined: AI stays off
    G._notice_answer[0] = False
    dlg = gui_ai.AiSettingsDialog(st)
    dlg.enabled.setChecked(True)
    assert len(G._notices) == 1 and not dlg.enabled.isChecked()
    st.set_share(True)
    assert len(G._notices) == 2 and st.bridge is None
    # accepted: on, and never asked again
    G._notice_answer[0] = True
    dlg.enabled.setChecked(True)
    assert dlg.enabled.isChecked()
    dlg._ok()
    cfg = C.load()
    assert cfg["enabled"] and cfg["notice_accepted"] == gui_ai.NOTICE_VERSION
    n = len(G._notices)
    dlg2 = gui_ai.AiSettingsDialog(st)
    dlg2.enabled.setChecked(False)
    dlg2.enabled.setChecked(True)
    assert len(G._notices) == n


def test_ai_turned_on_before_the_notice_still_shows_it_first(win, G):
    """An install that had AI on before the notice existed: it is shown before
    the first question goes out, and declining sends nothing."""
    from ovtsuite import gui_ai
    from ovtsuite.ai import config as C
    C.update(enabled=True, notice_accepted=0)
    d = gui_ai.show_assistant(win)
    made = []
    d._make_assistant = lambda: made.append(1)
    G._notice_answer[0] = False
    d.ask("what does this map do?")
    assert len(G._notices) == 1 and not made and d.job is None
    assert "Nothing was sent" in d.chat[-1][1]

