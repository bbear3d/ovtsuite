# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The "Read complete" prompt (gui_project.ask_read / open_read) and saving a
read to a file (ovtsuite.read_export), with synthetic reads -- no reference
binaries needed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import read_export as rx  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

READ = bytes(range(256)) * 64                   # 16 KB, no platform
EEPROM = bytes([0xA5]) * 512

pytestmark = pytest.mark.slow          # the main window


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings to a file of the test's own (conftest)."""


# -- read_export, no GUI ------------------------------------------------------

def test_raw_save_writes_the_image_and_the_eeprom_beside_it(tmp_path):
    path = str(tmp_path / "read.bin")
    out = rx.save_read(path, READ, "read", eeprom=EEPROM)
    assert out == [path, str(tmp_path / "read_EEPROM.bin")]
    assert open(out[0], "rb").read() == READ
    assert open(out[1], "rb").read() == EEPROM
    # unless told not to
    os.remove(out[1])
    assert rx.save_read(path, READ, "read", eeprom=EEPROM,
                        eeprom_too=False) == [path]


def test_a_native_container_is_unwrapped_for_a_raw_save(tmp_path):
    from ovtsuite import container as _c
    c = _c.Container(next(iter(_c.FORMATS)))
    c.add(_c.BLOCK_EXTERNAL, READ)
    c.add(_c.BLOCK_EEPROM, EEPROM)
    blob = c.to_bytes()
    assert _c.format_for_magic(blob[:8]) is not None
    raw, ee = rx.raw_parts(blob)
    assert raw == READ and ee == EEPROM


def test_an_ovt_save_holds_the_read_and_its_eeprom(tmp_path):
    from ovtsuite import project as ovt
    path = str(tmp_path / "read.ovt")
    assert rx.save_read(path, READ, "read", eeprom=EEPROM) == [path]
    p = ovt.Project.load(path)
    s = p.sets[0]
    root = s.roots()[0]
    assert root.kind == "read"
    st = p.state(s.id, root.id)
    assert st["eeprom"] == EEPROM
    assert READ in st.values()


# -- the prompt, in the main window --------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _answer(monkeypatch, *clicks, discard=True):
    """Answer the prompt by clicking the buttons named in ``clicks`` in turn
    ("Cancel" is the Cancel button), and "Discard this read?" with
    ``discard``. Returns what was asked."""
    SB = QtWidgets.QMessageBox.StandardButton
    monkeypatch.delenv("OVT_NO_READ_PROMPT", raising=False)
    todo, asked = list(clicks), []

    def _exec(box):
        texts = [b.text() for b in box.buttons()]
        if "Export to file..." not in texts:     # another message: dismissed
            box._hit = None
            return 0
        asked.append(texts)
        want = todo.pop(0)
        box._hit = (box.button(SB.Cancel) if want == "Cancel" else
                    next(b for b in box.buttons() if b.text() == want))
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", _exec)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton",
                        lambda box: box._hit)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: asked.append("discard?")
        or (SB.Yes if discard else SB.No)))
    return asked


def test_without_a_project_the_prompt_offers_new_or_export(win, monkeypatch):
    from ovtsuite import gui_project as gp
    asked = _answer(monkeypatch, "Open as a new project")
    assert win.load_from_image(READ, "read")
    assert not any(t.startswith("Add to") for t in asked[0])
    assert "Export to file..." in asked[0]
    assert len(gp.docs(win)) == 1


def test_cancel_discards_the_read_after_asking(win, monkeypatch):
    from ovtsuite import gui_project as gp
    asked = _answer(monkeypatch, "Cancel")
    assert not win.load_from_image(READ, "read")
    assert asked[-1] == "discard?"
    assert gp.docs(win) == []
    assert win.current is None or win.current.project_ref is None


def test_cancel_then_not_discarding_asks_again(win, monkeypatch):
    from ovtsuite import gui_project as gp
    asked = _answer(monkeypatch, "Cancel", "Open as a new project",
                    discard=False)
    assert win.load_from_image(READ, "read")
    assert asked.count("discard?") == 1 and len(gp.docs(win)) == 1


def test_export_saves_the_read_and_opens_nothing(win, monkeypatch, tmp_path):
    from ovtsuite import gui_project as gp
    _answer(monkeypatch, "Export to file...")
    path = str(tmp_path / "out.bin")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, rx.FILTERS[1])))
    assert not win.load_from_image(READ, "read")
    assert open(path, "rb").read() == READ
    assert gp.docs(win) == []


def test_a_cancelled_export_asks_again(win, monkeypatch):
    from ovtsuite import gui_project as gp
    asked = _answer(monkeypatch, "Export to file...", "Open as a new project")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    assert win.load_from_image(READ, "read")
    assert len(asked) == 2 and len(gp.docs(win)) == 1


def test_with_a_project_in_front_add_is_offered(win, monkeypatch):
    from ovtsuite import gui_project as gp
    _answer(monkeypatch, "Open as a new project")
    assert win.load_from_image(READ, "read")
    asked = _answer(monkeypatch, "Add to %s" % gp.docs(win)[0].title)
    assert win.load_from_image(READ[::-1], "read 2")
    assert any(t.startswith("Add to") for t in asked[0])
    assert len(gp.docs(win)) == 1
    s = gp.docs(win)[0].project.sets
    assert sum(len(x.branches) for x in s) == 2


# -- file information after New project / Add files -----------------------------

def _count_info(G, monkeypatch):
    """Record each tab the file information is shown for (gui_files), and
    keep the limited-support explanation (a modal box) out of the way."""
    from ovtsuite import gui_files as gf
    seen = []
    monkeypatch.setattr(gf, "_info_on_open",
                        lambda win, ws=None: seen.append(ws or win.current))
    monkeypatch.setattr(G.Workspace, "_explain_limited_support",
                        lambda self, *a, **k: None)
    return seen


def test_new_project_shows_the_file_information_once(G, win, monkeypatch,
                                                     tmp_path):
    from ovtsuite import gui_project as gp
    path = tmp_path / "car.bin"
    path.write_bytes(READ)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: ([str(path)], "")))
    seen = _count_info(G, monkeypatch)
    assert gp.new_project(win)
    QtWidgets.QApplication.processEvents()
    assert seen == [win.current]
    # adding a file to it shows that file's
    other = tmp_path / "other.bin"
    other.write_bytes(READ[::-1])
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: ([str(other)], "")))
    assert gp.add_files(win, gp.docs(win)[0])
    QtWidgets.QApplication.processEvents()
    assert len(seen) == 2 and seen[1] is not seen[0]
