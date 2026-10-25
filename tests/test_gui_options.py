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

def test_the_find_button_toggles_and_ctrl_f_opens(win, app):
    """The toolbar's Find button shows and hides the Find panel, pressed
    while it is shown; Edit > Find (Ctrl+F) only ever opens it."""
    ids = _ids(win)
    btn, find = ids["edit/find-panel"], ids["edit/find"]
    assert btn is not find and btn.isCheckable() and not find.isCheckable()
    assert btn in _toolbar_ids_actions(win)
    assert not btn.icon().isNull()
    dock = win.search_dock
    assert not dock.isVisible() and not btn.isChecked()
    btn.trigger()
    app.processEvents()
    assert dock.isVisible() and btn.isChecked()
    find.trigger()                                  # stays open
    app.processEvents()
    assert dock.isVisible() and btn.isChecked()
    btn.trigger()
    app.processEvents()
    assert not dock.isVisible() and not btn.isChecked()
    find.trigger()
    app.processEvents()
    assert dock.isVisible() and btn.isChecked()
    dock.hide()                                     # Escape, the close button
    app.processEvents()
    assert not btn.isChecked()
    # tabbed with another dock and behind it: the button raises it
    other = QtWidgets.QDockWidget("Other", win)
    win.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, other)
    btn.trigger()
    win.tabifyDockWidget(dock, other)
    other.show()
    other.raise_()
    app.processEvents()
    assert not dock.isHidden() and not btn.isChecked()
    btn.trigger()
    app.processEvents()
    assert btn.isChecked() and other.visibleRegion().isEmpty()
    btn.trigger()
    app.processEvents()
    assert dock.isHidden() and not btn.isChecked()
    # listed for the toolbar editor, with its icon
    from ovtsuite import gui_actions
    assert "edit/find-panel" in dict(gui_actions.by_menu(win))["Edit"]


def _toolbar_ids_actions(w):
    from ovtsuite import gui_toolbar
    return gui_toolbar.toolbar(w).actions()


def test_zf6hp_program_area_switch_is_an_option(win, monkeypatch):
    """Options > Projects and saving holds the ZF 6HP program-area switch
    (once Tools > ZF 6HP > Correct program-area checksums): the value stored
    under its old key shows, and a change reaches the open tabs and what a
    file reads at load."""
    from ovtsuite import gui
    from ovtsuite.gui_options import OptionsDialog
    from ovtsuite.modules import registry
    key = "zf6hp/correct_program_area"
    assert opt.PROGRAM_AREA_KEY == key and gui.PROGRAM_AREA_KEY == key
    assert opt.BY_KEY[key].page == opt.PROJECTS
    texts = [a.text() for a in win.tools_menu.actions()]
    assert not any("program-area" in t for t in texts)
    if not registry.is_installed("zf6hp"):
        dlg = OptionsDialog(win, opt.PROJECTS)
        assert key not in dlg.widgets               # not without the module
        dlg.reject()
        return
    s = opt._store()
    s.setValue(key, True)                           # as the menu switch left it
    s.sync()
    assert gui.program_area_pref() is True
    synced = []
    monkeypatch.setattr(win, "_sync_program_area", lambda: synced.append(1))
    dlg = OptionsDialog(win, opt.PROJECTS)
    box = dlg.widgets[key]
    assert isinstance(box, QtWidgets.QCheckBox) and box.isChecked()
    box.setChecked(False)
    assert dlg.apply()
    assert gui.program_area_pref() is False and synced
    dlg.restore_page_defaults()
    assert not box.isChecked()
    dlg.reject()


def test_the_program_area_switch_reaches_open_tabs(win):
    class _P:
        correct_program_area = False

    class _Ws:
        primary = _P()
    ws = _Ws()
    win.workspaces = lambda: [ws]
    try:
        opt.set(opt.PROGRAM_AREA_KEY, True)         # e.g. the Options dialog
        assert ws.primary.correct_program_area is True
        opt.set(opt.PROGRAM_AREA_KEY, False)
        assert ws.primary.correct_program_area is False
    finally:
        del win.workspaces
