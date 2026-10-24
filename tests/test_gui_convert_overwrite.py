# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Converter window and an output file already there (offscreen): the
question (Yes / No / to all, Make this the default), asked on the GUI
thread while the worker waits, and the Options tab's choice kept.
``python -m pytest tests/test_gui_convert_overwrite.py -q --slow``"""

import os
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))
    from PySide6 import QtCore
    from ovtsuite import appsettings
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appsettings, "settings",
                        lambda: QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))


def _wait(app, dlg, limit=60):
    t = time.time()
    while dlg._worker is not None and time.time() - t < limit:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def _window(tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    src = tmp_path / "a.bin"
    src.write_bytes(bytes([0x5A]) * 0x400)
    out = tmp_path / "a.hex"
    out.write_text("OLD")
    d = ConverterDialog(None)
    d.add_sources([str(src)])
    d.target.setCurrentIndex(d.target.findData("hex"))
    d.out.setText(str(out))
    return d, out


def _answers(monkeypatch, *answers):
    from ovtsuite import gui_convert
    asked, left = [], list(answers)

    def fake(parent, path):
        asked.append(path)
        return left.pop(0)
    monkeypatch.setattr(gui_convert, "ask_overwrite", fake)
    return asked


def test_the_default_is_ask_and_the_job_carries_it(app, tmp_path):
    d, _out = _window(tmp_path)
    assert d.existing.currentData() == "ask"
    assert d.job().options["existing"] == "ask"
    d.close()


def test_no_leaves_the_file_and_says_so(app, tmp_path, monkeypatch):
    d, out = _window(tmp_path)
    asked = _answers(monkeypatch, ("no", False))
    d.convert()
    _wait(app, d)
    assert asked == [str(out)]                   # asked from the worker's run
    assert out.read_text() == "OLD"
    assert "Skipped" in d.status.text() and d._last.skipped == [str(out)]
    assert d.existing.currentData() == "ask"     # no default made
    d.close()


def test_yes_replaces_it_and_asks_again_next_run(app, tmp_path, monkeypatch):
    d, out = _window(tmp_path)
    asked = _answers(monkeypatch, ("yes", False), ("yes", False))
    d.convert()
    _wait(app, d)
    assert out.read_text() != "OLD" and "Saved" in d.status.text()
    d.convert()
    _wait(app, d)
    assert len(asked) == 2                       # "yes" was for that run only
    d.close()


def test_yes_and_make_default_sets_overwrite_and_keeps_it(app, tmp_path, monkeypatch):
    from ovtsuite.gui_convert import ConverterDialog
    d, out = _window(tmp_path)
    asked = _answers(monkeypatch, ("yes", True))
    d.convert()
    _wait(app, d)
    assert out.read_text() != "OLD"
    assert d.existing.currentData() == "overwrite"
    out.write_text("OLD")
    d.convert()                                  # not asked: overwritten
    _wait(app, d)
    assert len(asked) == 1 and out.read_text() != "OLD"
    d.close()
    d2 = ConverterDialog(None)                   # the choice is remembered
    assert d2.existing.currentData() == "overwrite"
    d2.close()


def test_no_and_make_default_sets_skip(app, tmp_path, monkeypatch):
    from ovtsuite.gui_convert import ConverterDialog
    d, out = _window(tmp_path)
    asked = _answers(monkeypatch, ("no_all", True))
    d.convert()
    _wait(app, d)
    assert out.read_text() == "OLD" and d.existing.currentData() == "skip"
    d.convert()
    _wait(app, d)
    assert len(asked) == 1 and out.read_text() == "OLD"
    d.close()
    d2 = ConverterDialog(None)
    assert d2.existing.currentData() == "skip"
    d2.close()


def test_to_all_answers_the_rest_of_the_run_only(app, tmp_path, monkeypatch):
    """Several files in one run (the conversion and the binaries written out
    with it): "to all" answers every later one of that run."""
    d, _out = _window(tmp_path)
    asked = _answers(monkeypatch, ("yes_all", False), ("no_all", False))
    d._ask_all = None
    assert [d._overwrite_answer("f%d" % i) for i in range(3)] == [True, True, True]
    d._ask_all = None                            # a new run
    assert [d._overwrite_answer("g%d" % i) for i in range(2)] == [False, False]
    assert asked == ["f0", "g0"]
    assert d.existing.currentData() == "ask"
    d.close()


def test_make_default_answers_the_rest_of_the_run_too(app, tmp_path, monkeypatch):
    d, _out = _window(tmp_path)
    asked = _answers(monkeypatch, ("yes", True))
    d._ask_all = None
    assert [d._overwrite_answer(p) for p in ("a", "b")] == [True, True]
    assert asked == ["a"] and d.existing.currentData() == "overwrite"
    d.close()


def test_the_option_set_by_hand_is_kept_but_not_in_a_preset(app, tmp_path, monkeypatch):
    import json
    from ovtsuite.gui_convert import ConverterDialog
    d, out = _window(tmp_path)
    d.existing.setCurrentIndex(d.existing.findData("skip"))
    _answers(monkeypatch)                        # never asked
    d.convert()
    _wait(app, d)
    assert out.read_text() == "OLD"
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("mine", True))
    d._save_preset()
    with open(os.path.join(d._preset_dir(), "mine.json"), encoding="utf-8") as fh:
        assert "existing" not in json.load(fh).get("options", {})
    d.close()
    d2 = ConverterDialog(None)
    assert d2.existing.currentData() == "skip"
    # loading a preset (with no choice in it) leaves the window's alone
    d2.presets.setCurrentIndex(d2.presets.findText("mine"))
    d2._load_preset()
    assert d2.existing.currentData() == "skip"
    d2.close()


def test_a_worker_with_no_one_to_ask_leaves_it_alone(app, tmp_path):
    from ovtsuite import convert as C
    from ovtsuite.gui_convert import _Worker
    d, out = _window(tmp_path)
    w = _Worker([d.job()])
    got = []
    w.done.connect(lambda r, e: got.append((r, e)))
    w.run()                                       # here, unconnected: no question
    assert got[0][1] is None and got[0][0].skipped == [str(out)]
    assert out.read_text() == "OLD"
    d.close()


@pytest.mark.parametrize("button,tick,want", [
    ("Yes", False, ("yes", False)), ("No", False, ("no", False)),
    ("YesToAll", True, ("yes_all", True)), ("NoToAll", True, ("no_all", True)),
    (None, False, ("no", False))])
def test_the_question_box(app, button, tick, want):
    from PySide6 import QtCore
    from ovtsuite.gui_convert import ask_overwrite
    SB = QtWidgets.QMessageBox.StandardButton
    seen = {}

    def answer():
        box = QtWidgets.QApplication.activeModalWidget()
        if not isinstance(box, QtWidgets.QMessageBox):
            QtCore.QTimer.singleShot(20, answer)
            return
        seen["text"] = box.text()
        seen["buttons"] = box.standardButtons()
        seen["box"] = box.checkBox().text()
        if tick:
            box.checkBox().setChecked(True)
        if button is None:
            box.reject()                         # Esc / closed
        else:
            box.button(getattr(SB, button)).click()
    QtCore.QTimer.singleShot(20, answer)
    assert ask_overwrite(None, os.path.join("x", "a.hex")) == want
    assert "a.hex is already there" in seen["text"]
    assert seen["box"] == "Make this the default"
    for b in (SB.Yes, SB.No, SB.YesToAll, SB.NoToAll):
        assert seen["buttons"] & b
