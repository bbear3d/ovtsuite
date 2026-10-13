# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The start-up splash (ovtsuite.gui_splash): the app's icon, name, version
and what start-up is doing, shown without a break until start-up is done
(the questions come before it; a message during it goes on top of it) --
and every call harmless with no splash."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the GUI


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_splash_shows_steps_and_ends(app):
    from ovtsuite import branding, gui_splash as S
    s = S.begin()
    try:
        assert s.isVisible() and s.name.text() == branding.APP_NAME
        assert branding.__version__ in s.sub.text()
        assert not s.icon.pixmap().isNull()             # the app's own icon
        S.step("Loading modules...", 0.25)
        assert s.status.text() == "Loading modules..." and s.bar.value() == 250
        S.step("Reopening A6 (1 of 2)...", 0.6)
        assert s.isVisible() and s.bar.value() == 600
    finally:
        S.end()
    assert S.current() is None and not s.isVisible()
    S.step("nothing to show")                            # no splash: harmless
    S.end()


def test_a_standalone_tool_has_its_own_name(app):
    from ovtsuite import branding, gui_splash as S
    s = S.begin("flasher")
    try:
        assert s.name.text() == branding.TOOL_NAMES["flasher"]
    finally:
        S.end()


def test_reopening_last_sessions_projects_says_which(app, monkeypatch):
    from ovtsuite import gui, gui_splash as S
    said = []
    monkeypatch.setattr(S, "step", lambda text, fraction=None: said.append(text))
    w = gui.MainWindow()
    try:
        gone = os.path.join("nowhere", "A6.ovt")
        w.restore_tabs([{"primary": gone + "#ovt:s/b", "project": gone,
                         "project_title": "A6 BHW swap"},
                        {"primary": gone + "#ovt:s/c", "project": gone,
                         "project_title": "A6 BHW swap"}], [0, 1])
    finally:
        w.close_discarding()
    assert said == ["Reopening A6 BHW swap (1 of 2)...",
                    "Reopening A6 BHW swap (2 of 2)..."]


def test_a_message_during_start_up_goes_on_top_of_the_splash(app):
    """A warning while a project opens must not wait behind the splash (it
    stays on top), and the splash itself is never hidden."""
    from PySide6 import QtCore
    from ovtsuite import gui_splash as S
    s = S.begin()
    try:
        d = QtWidgets.QDialog()
        d.show()
        app.processEvents()
        assert s.isVisible() and d.isVisible()
        assert d.windowHandle().flags() & QtCore.Qt.WindowType.WindowStaysOnTopHint
        d.close()
    finally:
        S.end()


def test_start_up_questions_come_before_the_window(app, monkeypatch):
    """Which projects to reopen is asked before the main window (and the
    splash) exist; the answer is applied after. The option decides whether
    it is asked at all."""
    import json
    from ovtsuite import gui, options
    from ovtsuite.appsettings import settings
    entries = [{"primary": "x.ovt#ovt:s/b", "project": "x.ovt",
                "project_title": "A6"}]
    settings().setValue(gui.TABS_KEY, json.dumps(entries))
    placed = []
    options.set("startup/restore", "never")
    assert gui.MainWindow.ask_restore(None, placed.append) is None
    options.set("startup/restore", "always")
    assert gui.MainWindow.ask_restore(None, placed.append) == (entries, [0])
    assert placed == []                                  # nothing was asked
    options.set("startup/restore", "ask")
    monkeypatch.setattr(gui.RestoreProjectsDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    got = gui.MainWindow.ask_restore(None, placed.append)
    assert len(placed) == 1 and isinstance(placed[0], gui.RestoreProjectsDialog)
    assert got is None or got[0] == entries


def test_the_window_and_the_splash_start_on_one_screen(app):
    from ovtsuite import gui_options, gui_splash as S
    screen = gui_options.start_screen()
    assert screen is not None
    d = QtWidgets.QDialog()
    gui_options.center_on(d, screen)
    assert screen.availableGeometry().contains(d.geometry().center())
    s = S.begin(screen=screen)
    try:
        assert screen.availableGeometry().contains(s.geometry().center())
    finally:
        S.end()

def test_a_file_loading_progress_window_sits_below_the_splash(app):
    """'Opening ...' progress windows must not cover the splash."""
    from ovtsuite import gui_splash as S
    s = S.begin()
    try:
        p = QtWidgets.QProgressDialog("Opening A6...", "", 0, 10)
        p.show()
        app.processEvents()
        assert not p.frameGeometry().intersects(s.frameGeometry())
        assert s.isVisible()
        p.close()
    finally:
        S.end()


def test_tests_keep_out_of_the_users_recent_projects(isolated_qsettings):
    """The fixture moves the app's config folder too: a test that opens a
    project must not land in the user's File > Open Recent."""
    from ovtsuite import recent
    assert os.path.normcase(recent._config_dir()) == os.path.normcase(
        os.path.join(os.path.dirname(isolated_qsettings), "config"))
    recent.add_recent(os.path.abspath(__file__))
    assert os.path.exists(os.path.join(recent._config_dir(), "recent.json"))