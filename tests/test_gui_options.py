# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Options, the toolbar, keyboard shortcuts and the theme
(ovtsuite.gui_options, gui_toolbar, gui_actions, gui_theme, icons).

Offscreen, no reference data: every menu item has an id, the toolbar holds the
very menu actions, Options changes apply to the open window at once, two
items can't share a shortcut, and the dark theme comes and goes.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import options as opt  # noqa: E402

pytestmark = pytest.mark.slow          # the main window


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    path = str(tmp_path / "options.ini")
    monkeypatch.setattr(opt, "_store", lambda: QtCore.QSettings(
        path, QtCore.QSettings.Format.IniFormat))
    from ovtsuite import gui_autohide          # the panels' state, too
    monkeypatch.setattr(gui_autohide, "_settings", lambda: QtCore.QSettings(
        str(tmp_path / "panels.ini"), QtCore.QSettings.Format.IniFormat))
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    from ovtsuite import gui
    w = gui.MainWindow()
    w.show()
    yield w
    w.close_discarding()
    from ovtsuite import gui_theme
    gui_theme.apply(app, "system")


def _ids(w):
    return w.__dict__["actions_by_id"]


def _toolbar_ids(w):
    from ovtsuite import gui_toolbar
    return [a.objectName() or "|" for a in gui_toolbar.toolbar(w).actions()]


def test_menu_items_have_ids_and_the_toolbar_reuses_them(win):
    ids = _ids(win)
    assert ids["file/save"].shortcut().toString() == "Ctrl+S"
    assert "tools/options" in ids and "tools/ai/assistant" in ids
    assert _toolbar_ids(win) == opt.DEFAULT_TOOLBAR
    from ovtsuite import gui_toolbar
    tb = gui_toolbar.toolbar(win)
    assert ids["file/save"] in tb.actions()            # the menu's own action
    assert not ids["file/save"].icon().isNull()
    pm = ids["file/save"].icon().pixmap(16, 16)
    assert not pm.isNull()


def test_toolbar_options_apply_at_once(win):
    from ovtsuite import gui_toolbar
    tb = gui_toolbar.toolbar(win)
    opt.set_toolbar(["edit/undo", "|", "tools/hex-editor"])
    assert _toolbar_ids(win) == ["edit/undo", "|", "tools/hex-editor"]
    opt.set("toolbar/icon_size", "large")
    assert tb.iconSize().width() == gui_toolbar.SIZES["large"]
    opt.set("toolbar/style", "under")
    assert tb.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    opt.set("toolbar/visible", False)
    assert not tb.isVisible()


def test_changed_shortcuts_apply_and_go_back(win):
    save = _ids(win)["file/save"]
    opt.set_shortcuts({"file/save": "F2"})
    assert save.shortcut().toString() == "F2"
    opt.set_shortcuts({"file/save": None})
    assert save.shortcut().toString() == "Ctrl+S"


def test_the_dialog_stores_and_applies(win):
    from ovtsuite.gui_options import OptionsDialog, SHORTCUTS_PAGE
    dlg = OptionsDialog(win, opt.TOOLBAR)
    assert dlg.page_names[dlg.pages.currentRow()] == opt.TOOLBAR
    dlg.widgets["projects/autosave_s"].setValue(120)
    # the toolbar editor: add Hex editor after the first button
    dlg.tb_list.setCurrentRow(0)
    item = None
    for i in range(dlg.tb_avail.topLevelItemCount()):
        top = dlg.tb_avail.topLevelItem(i)
        for j in range(top.childCount()):
            if top.child(j).data(0, QtCore.Qt.ItemDataRole.UserRole) == \
                    "tools/hex-editor":
                item = top.child(j)
    dlg.tb_avail.setCurrentItem(item)
    dlg._tb_add()
    assert dlg.toolbar_ids()[:2] == ["file/new-project", "tools/hex-editor"]
    assert dlg.apply()
    assert opt.get("projects/autosave_s") == 120
    assert win.__dict__["_ovt_autosave_timer"].interval() == 120_000
    assert _toolbar_ids(win)[:2] == ["file/new-project", "tools/hex-editor"]
    # a shortcut two items share is refused until one changes
    dlg.pages.setCurrentRow(dlg.page_names.index(SHORTCUTS_PAGE))
    dlg.sc_values["file/save"] = "Ctrl+O"
    assert not dlg.apply()
    assert _ids(win)["file/save"].shortcut().toString() == "Ctrl+S"
    dlg.sc_values["file/save"] = "F2"
    assert dlg.apply()
    assert _ids(win)["file/save"].shortcut().toString() == "F2"
    # Restore defaults on the toolbar page
    dlg.pages.setCurrentRow(dlg.page_names.index(opt.TOOLBAR))
    dlg.restore_page_defaults()
    assert dlg.toolbar_ids() == opt.DEFAULT_TOOLBAR
    dlg.reject()


def test_dark_theme_comes_and_goes(win, app):
    base = app.style().name()
    opt.set("ui/theme", "dark")
    assert app.style().name().lower() == "fusion"
    assert app.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 80
    opt.set("ui/theme", "system")
    from ovtsuite import gui_theme
    assert gui_theme._base_style_name(app).lower() in (
        base.lower(), app.style().name().lower())
    assert app.palette().color(QtGui.QPalette.ColorRole.Window).lightness() > 150


def test_the_window_remembers_its_size(win):
    """(Offscreen, the screen is small: a restored size is fitted to it, so
    this checks the window comes back as it was -- not maximized -- and
    opens maximized when the option is off.)"""
    from ovtsuite import gui, gui_options
    win.showNormal()
    QtWidgets.QApplication.processEvents()
    win.close_discarding()                          # remembers as it closes
    assert opt._store().contains(gui_options.GEOMETRY_KEY)
    for remember, maximized in ((True, False), (False, True)):
        opt.set("window/remember", remember)
        w2 = gui.MainWindow()
        try:
            gui_options.show_main(w2)
            assert w2.isMaximized() == maximized
        finally:
            w2.close_discarding()


def test_the_project_panel_button_expands_and_collapses(win):
    """View > Project panel: hidden or collapsed -> expanded, to stay;
    expanded -> collapsed to its strip."""
    from ovtsuite import gui_project as gp
    act = _ids(win)["view/project-panel"]
    dock = gp.panel(win)
    act.trigger()
    assert dock.isVisible() and not dock.is_collapsed()
    act.trigger()
    assert dock.isVisible() and dock.is_collapsed()
    dock.fly_out()                                  # hovering the strip
    act.trigger()
    assert not dock.is_collapsed() and not dock.is_flown_out()
    act.trigger()
    assert dock.is_collapsed()