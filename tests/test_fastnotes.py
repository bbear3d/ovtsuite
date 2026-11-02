# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The EDC15 fast-logging session notes: collected from the worker's messages and kept
in the project as an attachment (or beside a log recorded to a file)."""
import os
import time

import pytest

from ovtsuite.datalog import fastnotes as FN

RATE = "EDC15-fast: logging 16 ch @ 3.1 Hz (list+?/P3 25 ms, 60 req, 2 failed, 98 ms/req)"


def test_only_fast_messages_are_kept_and_the_rate_line_is_thinned():
    n = FN.SessionNotes()
    n.add("Paused (session kept alive).")
    n.add("EDC15-fast: connecting at 38400 baud...")
    for i in range(20):                       # twice a second for 10 s
        n.add(RATE, now=100.0 + i * 0.5)
    n.add("EDC15-fast: slow rows: 3 in a row")
    msgs = [m for _t, m in n.lines]
    assert msgs[0].endswith("connecting at 38400 baud...") and msgs[-1].endswith("3 in a row")
    assert sum(1 for m in msgs if FN.is_rate_line(m)) == 2        # at 100.0 and 105.0
    assert "Paused" not in "".join(msgs)


def test_the_text_has_the_header_and_a_clock_per_line():
    n = FN.SessionNotes()
    n.reset(["Binary: car.bin", "Speed: Medium (38400)"])
    n.add("EDC15-fast: 10 89 refused with NRC 0x22")
    text = n.text()
    lines = text.splitlines()
    assert lines[0].startswith("EDC15 fast-logging session, started ")
    assert lines[1:3] == ["Binary: car.bin", "Speed: Medium (38400)"]
    assert lines[-1][2] == ":" and lines[-1].endswith("  10 89 refused with NRC 0x22")


def test_take_hands_the_notes_over_once_and_keeps_the_header():
    n = FN.SessionNotes()
    n.reset(["Binary: car.bin"])
    assert n.take() is None
    n.add("EDC15-fast: one")
    name, data = n.take()
    assert name.startswith("fastlog_session_") and name.endswith(".txt")
    assert b"Binary: car.bin" in data and b"one" in data
    assert n.take() is None and n.header == ["Binary: car.bin"]


def test_a_runaway_session_stops_collecting():
    n = FN.SessionNotes()
    for i in range(FN.MAX_LINES + 50):
        n.add("EDC15-fast: m%d" % i)
    assert len(n.lines) == FN.MAX_LINES + 1 and "not kept" in n.lines[-1][1]


def test_keep_puts_the_text_in_the_project_as_an_attachment(monkeypatch):
    from ovtsuite import gui_project as GP
    calls = []
    monkeypatch.setattr(GP, "refresh_panel", lambda win: calls.append("panel"))
    monkeypatch.setattr(GP, "autosave_soon", lambda win, doc: calls.append("autosave"))
    added = []

    class Proj:
        def add_attachment(self, filename, data, set_id=None, note=""):
            added.append((filename, data, set_id))

    class Doc:
        project = Proj()
        def tabs(self, win): return ["tab"]
    win = type("Win", (), {"_workspace_changed": lambda self, t: calls.append(t)})()
    ws = type("W", (), {"win": win, "project_ref": type("R", (), {"doc": Doc(), "sid": "s1"})()})()
    assert FN.keep(object(), ws, "n.txt", b"text") is True
    assert added == [("n.txt", b"text", "s1")] and calls == ["tab", "panel", "autosave"]
    assert FN.keep(object(), type("W", (), {"project_ref": None})(), "n.txt", b"x") is False
    assert FN.keep(object(), ws, "n.txt", b"") is False


def test_keep_uses_the_standalone_hosts_own_route():
    seen = []

    class Host:
        def keep_attachment_async(self, ws, filename, data, parent, done):
            seen.append((filename, data, parent, done))
            return None
    ws = type("W", (), {"project_ref": object()})()
    assert FN.keep(Host(), ws, "n.txt", b"text", parent="p", done="d") is None
    assert seen == [("n.txt", b"text", "p", "d")]


def test_write_beside_a_log_file(tmp_path):
    log = tmp_path / "car.csv"
    log.write_text("x")
    path = FN.write_beside(str(log), "notes.txt", b"hello")
    assert os.path.dirname(path) == str(tmp_path) and open(path, "rb").read() == b"hello"
    assert FN.write_beside(str(tmp_path / "nope" / "car.csv"), "notes.txt", b"x") is None


# ------------------------------------------------ OVT Logger: kept off the GUI thread
@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(until, timeout=10.0):
    from PySide6 import QtWidgets
    end = time.time() + timeout
    while time.time() < end and not until():
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    return until()


def test_the_standalone_logger_writes_the_attachment_into_the_ovt(app, tmp_path):
    from ovtsuite.datalog import standalone as SA
    from ovtsuite.project import Project
    path = str(tmp_path / "car.ovt")
    Project("car").save(path)
    host = SA.StandaloneLogHost()
    ref = type("R", (), {"doc": type("D", (), {"path": path})(), "sid": None})()
    ws = type("W", (), {"project_ref": ref})()
    done = []
    assert host.keep_attachment_async(ws, "fastlog_session_x.txt", b"the notes", None,
                                      done.append) is None
    assert _pump(lambda: done) and done == [True]
    p = Project.load(path)
    assert [a.filename for a in p.attachments] == ["fastlog_session_x.txt"]
    assert p.attachment_bytes(p.attachments[0].id) == b"the notes"


def test_the_standalone_logger_saves_beside_the_project_when_it_cannot_write(app, tmp_path,
                                                                             monkeypatch):
    from PySide6 import QtWidgets
    from ovtsuite import gui_ovt_file as OF
    from ovtsuite.datalog import standalone as SA
    monkeypatch.setattr(OF, "update_project_quiet", lambda path, change: (None, ""))
    boxes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(a[2])))
    monkeypatch.setattr(OF, "say_update_result", lambda *a, **k: boxes.append("open elsewhere"))
    host = SA.StandaloneLogHost()
    ref = type("R", (), {"doc": type("D", (), {"path": str(tmp_path / "car.ovt")})(),
                         "sid": None})()
    ws = type("W", (), {"project_ref": ref})()
    done = []
    host.keep_attachment_async(ws, "notes.txt", b"hello", None, done.append)
    assert _pump(lambda: done) and done == [False]
    assert (tmp_path / "notes.txt").read_bytes() == b"hello"
    assert any("fast-logging session notes was saved as" in str(b) for b in boxes)


# ------------------------------------------------ the logger pane's wiring
class _Pane:
    """Just what ConnPane._keep_fast_notes / _fast_note use."""
    def __init__(self, project=True, last_log=""):
        self._fast_notes = FN.SessionNotes()
        self._fast_active = True
        self._window = object()
        self._last_log_file = last_log
        self.said = []
        self._ws = type("W", (), {"project_ref": object() if project else None})()

    def _project_ref(self):
        return getattr(self._ws, "project_ref", None), self._ws

    def _say(self, m):
        self.said.append(m)


def _pane_methods():
    from ovtsuite.datalog import gui_log as GL
    return GL.ConnPane._keep_fast_notes, GL.ConnPane._fast_note


def test_pane_keeps_the_notes_in_the_project_at_the_end(monkeypatch):
    keep, note = _pane_methods()
    p = _Pane()
    note(p, "EDC15-fast: connecting at 38400 baud...")
    got = []
    monkeypatch.setattr(FN, "keep", lambda win, ws, name, data, parent=None, done=None:
                        got.append((name, data)) or True)
    keep(p)
    assert got and got[0][0].startswith("fastlog_session_") and b"connecting" in got[0][1]
    assert any("Attachments: fastlog_session_" in m for m in p.said)
    got.clear()
    keep(p)                                       # once only
    assert not got


def test_pane_waits_for_the_standalone_threads_answer(monkeypatch):
    keep, note = _pane_methods()
    p = _Pane()
    note(p, "EDC15-fast: one")
    later = []
    monkeypatch.setattr(FN, "keep", lambda win, ws, name, data, parent=None, done=None:
                        later.append(done))        # None: the answer comes later
    keep(p)
    assert not p.said
    later[0](True)
    assert any("kept in the project" in m for m in p.said)


def test_pane_writes_beside_a_file_recording_when_there_is_no_project(tmp_path):
    keep, note = _pane_methods()
    log = tmp_path / "car.csv"
    log.write_text("x")
    p = _Pane(project=False, last_log=str(log))
    note(p, "EDC15-fast: one")
    keep(p)
    files = [f for f in os.listdir(tmp_path) if f.startswith("fastlog_session_")]
    assert len(files) == 1 and any("written to" in m for m in p.said)


def test_pane_ignores_the_messages_of_other_ecus():
    keep, note = _pane_methods()
    p = _Pane()
    p._fast_active = False
    note(p, "EDC15-fast: not this session")
    keep(p)
    assert not p.said and not p._fast_notes
