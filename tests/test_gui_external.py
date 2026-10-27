# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Items kept in a project, opened in another program and taken back
(ovtsuite.gui_external; Project panel > Open externally)."""
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import project as ovt  # noqa: E402
from ovtsuite import gui_external as X  # noqa: E402
from ovtsuite import gui_project as GP  # noqa: E402


class Win(QtWidgets.QMainWindow):
    """Just what the copies talk to."""

    def __init__(self):
        super().__init__()
        self.changed = []

    def workspaces(self):
        return []

    def _workspace_changed(self, t):
        self.changed.append(t)


@pytest.fixture
def env(tmp_path, monkeypatch, isolated_qsettings_module):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert app
    monkeypatch.setattr(X.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(GP, "autosave_soon", lambda *a, **k: None)
    win = Win()
    proj = ovt.Project()
    doc = GP.ProjectDoc(proj, None)
    att = proj.add_attachment("notes.txt", b"one\n")
    log = proj.add_log("pull.csv", b"a,b\n1,2\n")
    m = X.manager(win)
    monkeypatch.setattr(m, "can_ask", lambda: True)
    yield win, doc, m, att, log
    m.erase_all()
    doc.close()
    win.close()


def _edit(o, data, bump=2):
    """Save ``data`` over the copy as another program would, with a time
    that is surely different from the first write."""
    with open(o.path, "wb") as fh:
        fh.write(data)
    t = time.time() + bump
    os.utime(o.path, (t, t))


def test_the_copy_is_written_without_asking_for_a_place(env, tmp_path):
    win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    assert o.path.startswith(str(tmp_path)) and os.path.basename(o.path) == "notes.txt"
    assert open(o.path, "rb").read() == b"one\n"
    assert m.ensure(doc, "attachment", att.id) is o      # not written twice
    assert X.badge(win, doc, "attachment", att.id) == "  (open externally)"


def test_an_edit_is_found_offered_and_taken_back(env, monkeypatch):
    win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    m.poll()
    assert not o.edited
    _edit(o, b"two\n")
    m.poll()                                    # the file may still be written
    assert not o.edited
    asked = []
    monkeypatch.setattr(m, "offer", lambda c: asked.append(c))
    m.poll()
    assert o.edited and asked == [o]
    assert X.badge(win, doc, "attachment", att.id) == "  (edited externally)"
    assert m.update(o)
    assert doc.project.attachment_bytes(att.id) == b"two\n"
    assert doc.project.attachments[0].id == att.id       # same item
    assert not o.edited and doc.project.modified_since_save


def test_a_save_that_changes_nothing_is_not_an_edit(env):
    _win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    _edit(o, b"one\n")                          # touched, same bytes
    m.poll()
    m.poll()
    assert not o.edited


def test_always_update_skips_the_question(env, monkeypatch):
    _win, doc, m, _att, log = env
    o = m.ensure(doc, "log", log.id)
    o.always = True
    monkeypatch.setattr(m, "_ask_final", lambda *a: pytest.fail("asked"))
    _edit(o, b"a,b\n1,3\n")
    m.poll()
    m.poll()
    assert doc.project.log_bytes(log.id) == b"a,b\n1,3\n"


def test_not_now_is_not_asked_again_until_the_next_save(env, monkeypatch):
    _win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    asked = []
    monkeypatch.setattr(m, "offer", lambda c: asked.append(c))
    _edit(o, b"two\n")
    m.poll()
    m.poll()
    assert asked == [o]
    asked.clear()
    o.declined = o.now                          # "Not now"
    m.poll()
    assert asked == []
    _edit(o, b"three\n", bump=4)
    m.poll()
    m.poll()
    assert asked == [o]


@pytest.mark.parametrize("answer,kept,cancelled", [
    ("update", b"two\n", False), ("discard", b"one\n", False),
    ("cancel", b"one\n", True)])
def test_closing_asks_once_about_an_unanswered_edit(env, monkeypatch, answer,
                                                    kept, cancelled):
    _win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    _edit(o, b"two\n")
    monkeypatch.setattr(m, "_ask_final", lambda *a: answer)
    assert m.settle(doc) is (not cancelled)
    assert doc.project.attachment_bytes(att.id) == kept
    # asked once: a discarded edit is not asked about again
    monkeypatch.setattr(m, "_ask_final", lambda *a: pytest.fail("asked twice"))
    if not cancelled:
        assert m.settle(doc)


def test_copies_go_with_the_project_the_item_and_the_app(env):
    win, doc, m, att, log = env
    a = m.ensure(doc, "attachment", att.id)
    b = m.ensure(doc, "log", log.id)
    m.erase_item(doc, "attachment", att.id)
    assert not os.path.exists(a.folder) and os.path.exists(b.folder)
    m.erase_doc(doc)
    assert not os.path.exists(b.folder) and not m.copies
    c = m.ensure(doc, "log", log.id)
    m.erase_all()
    assert not os.path.exists(c.folder) and not os.path.exists(X.root())


def test_the_sweep_takes_what_an_earlier_run_left(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "_alive", lambda pid: pid == 4242)
    base = tmp_path / "ext"
    for name in ("111-1", "4242-1", "notapid", "%d-9" % os.getpid()):
        (base / name).mkdir(parents=True)
        (base / name / "f.txt").write_text("x")
    assert X.sweep(str(base)) == 1
    assert sorted(os.listdir(base)) == sorted(
        ["%d-9" % os.getpid(), "4242-1", "notapid"])


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
@pytest.mark.parametrize("lives,gone", [(0.5, True), (0.0, False)])
def test_a_program_the_app_started_ends_with_its_copy(env, monkeypatch, tmp_path,
                                                      lives, gone):
    """A long-lived program takes the copy with it; one that exits at once
    only handed the file on, so the copy stays."""
    _win, doc, m, att, _log = env
    script = tmp_path / "editor.sh"
    script.write_text("#!/bin/sh\nsleep %s\nexit 0\n" % lives)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(X, "HANDOFF_S", 0.2)
    assert m.open_with(doc, "attachment", att.id, str(script))
    o = m.find(doc, "attachment", att.id)
    deadline = time.time() + 5
    while o.proc is not None and time.time() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    assert o.proc is None
    assert (o not in m.copies) is gone
    assert os.path.exists(o.folder) is not gone


def test_a_closed_program_gets_one_last_look(env, monkeypatch):
    _win, doc, m, att, _log = env
    o = m.ensure(doc, "attachment", att.id)
    o.started = time.monotonic() - 60           # ran a while
    _edit(o, b"saved at the very end\n")
    monkeypatch.setattr(m, "_ask_final", lambda *a: "update")
    m._exited(o)
    assert doc.project.attachment_bytes(att.id) == b"saved at the very end\n"
    assert not os.path.exists(o.folder)


def test_launch_arguments():
    assert X.launch_args("/usr/bin/vim", "/t/a.txt") == ("/usr/bin/vim", ["/t/a.txt"])
    if sys.platform == "darwin":
        assert X.launch_args("/Applications/Numbers.app", "/t/a.csv")[0] == "open"
