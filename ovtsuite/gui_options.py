# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Options...: the app's global settings, the toolbar's buttons and
the keyboard shortcuts.

The plain options are declared in :mod:`ovtsuite.options` and their page is
built from those declarations; the Toolbar and Keyboard shortcuts pages have
editors of their own. Nothing changes until OK or Apply; then every open
window applies the change at once (:func:`install` listens to
``options.notifier()``).

:func:`install` also gives the window its action ids, icons, changed
shortcuts and toolbar, and :func:`show_main` / the close hook remember the
window's size and position.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import gui_actions as _ga
from . import options as _opt

#: QSettings key of the main window's size and position
GEOMETRY_KEY = "window/geometry"

SHORTCUTS_PAGE = "Keyboard shortcuts"
AI_PAGE = "AI"
ADVANCED_PAGE = "Advanced"


# ------------------------------------------------------------ the window
def install(win) -> None:
    """Called once the menus are built: Tools > Options..., the action ids,
    icons and shortcuts, the toolbar, and live updates."""
    tools = next((a.menu() for a in win.menuBar().actions()
                  if _ga.slug(a.text()) == "tools" and a.menu()), None)
    if tools is not None:
        tools.addSeparator()
        act = win._act(tools, "Options...", lambda: open_options(win), "Ctrl+,")
        act.setMenuRole(QtGui.QAction.MenuRole.PreferencesRole)
        act.setToolTip("Settings for the whole app: start-up, saving, theme, "
                       "the toolbar and the keyboard shortcuts")
    _ga.register(win)
    _ga.apply_shortcuts(win)
    _ga.apply_icons(win)
    from . import gui_toolbar
    gui_toolbar.install(win)
    note = _opt.notifier()

    def changed(key, win=win):
        try:
            _apply(win, key)
        except RuntimeError:            # the window is gone
            pass
    note.changed.connect(changed)
    win.destroyed.connect(lambda *_a: _disconnect(note, changed))
    win.installEventFilter(_CloseWatcher(win))


def _disconnect(note, slot):
    try:
        note.changed.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


def _apply(win, key: str) -> None:
    """An option changed: put it into effect in ``win`` now."""
    from . import gui_toolbar
    if key.startswith("toolbar/"):
        gui_toolbar.rebuild(win)
    elif key == _opt.SHORTCUT_PREFIX:
        _ga.apply_shortcuts(win)
        gui_toolbar.rebuild(win)            # tooltips name the shortcuts
    elif key == "ui/menu_icons":
        _ga.apply_icons(win)
    elif key == "ui/table_panels":
        from . import gui_panelayout
        gui_panelayout.apply_all(win, gui_panelayout.mode())
    elif key == "ui/compare_layout":
        from . import gui_cmpscroll
        gui_cmpscroll.apply_all(win)
    elif key == "ui/compare_size":
        from . import gui_cmpsize
        gui_cmpsize.apply_all(win)
    elif key.startswith("ui/compare_delta"):
        from . import gui_compare_delta
        gui_compare_delta.apply_all(win)
    elif key == "ui/theme":
        from . import gui_theme
        gui_theme.apply()
    elif key == "projects/autosave_s":
        t = win.__dict__.get("_ovt_autosave_timer")
        if t is not None:
            t.setInterval(_opt.get(key) * 1000)
    elif key == "projects/watch_s":
        t = win.__dict__.get("_ovt_disksync_timer")
        if t is not None:
            t.setInterval(_opt.get(key) * 1000)
    elif key == _opt.PROGRAM_AREA_KEY:
        sync = getattr(win, "_sync_program_area", None)
        if sync is not None:
            sync()                          # every open ZF 6HP tab follows
    elif key == "recent/max":
        try:
            win._rebuild_recent()
        except Exception:
            pass


class _CloseWatcher(QtCore.QObject):
    """Remembers the window's size and position as it closes."""

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Close and obj is self.parent():
            remember_geometry(obj)
        return False


def remember_geometry(win) -> None:
    s = _opt._store()
    if _opt.get("window/remember"):
        s.setValue(GEOMETRY_KEY, win.saveGeometry())
    else:
        s.remove(GEOMETRY_KEY)


def _saved_geometry():
    return _opt._store().value(GEOMETRY_KEY) if _opt.get("window/remember") \
        else None


def start_screen() -> Optional[QtGui.QScreen]:
    """The screen the app starts on -- where the main window was last time,
    else the one under the mouse -- so the start-up questions, the splash and
    the window all come up on the same screen of a multi-screen desk."""
    geo = _saved_geometry()
    if geo:
        probe = QtWidgets.QWidget()
        try:
            if probe.restoreGeometry(QtCore.QByteArray(geo)):
                s = QtGui.QGuiApplication.screenAt(probe.geometry().center())
                if s is not None:
                    return s
        finally:
            probe.deleteLater()
    return (QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
            or QtGui.QGuiApplication.primaryScreen())


def center_on(widget, screen=None) -> None:
    """Put a window (not shown yet) in the middle of ``screen``."""
    screen = screen or start_screen()
    if screen is None:
        return
    if not widget.testAttribute(QtCore.Qt.WidgetAttribute.WA_Resized):
        widget.adjustSize()             # (a size the window chose is kept)
    g = screen.availableGeometry()
    widget.move(g.center() - widget.rect().center())


def show_main(win, screen=None) -> None:
    """Show the main window as it was last time (or maximized, on
    ``screen`` -- the start-up screen)."""
    geo = _saved_geometry()
    if geo and win.restoreGeometry(QtCore.QByteArray(geo)):
        win.show()
        return
    screen = screen or start_screen()
    if screen is not None:
        win.move(screen.availableGeometry().topLeft())
    win.showMaximized()


def open_options(win, page: Optional[str] = None) -> bool:
    dlg = OptionsDialog(win, page)
    return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted


# ------------------------------------------------------------ the dialog
class OptionsDialog(QtWidgets.QDialog):
    """Tools > Options..."""

    def __init__(self, win, page: Optional[str] = None):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Options")
        _ga.register(win)
        self.widgets: Dict[str, QtWidgets.QWidget] = {}
        outer = QtWidgets.QVBoxLayout(self)
        body = QtWidgets.QHBoxLayout()
        outer.addLayout(body, 1)
        self.pages = QtWidgets.QListWidget()
        self.pages.setFixedWidth(170)
        self.stack = QtWidgets.QStackedWidget()
        body.addWidget(self.pages)
        body.addWidget(self.stack, 1)
        names = list(_opt.PAGES)
        names.insert(names.index(_opt.TOOLBAR) + 1, SHORTCUTS_PAGE)
        if AI_PAGE not in names:            # it has options of its own now
            names.insert(names.index(_opt.MODULES) + 1, AI_PAGE)
        names.append(ADVANCED_PAGE)
        for name in names:
            w = self._build_page(name)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setWidget(w)
            self.stack.addWidget(scroll)
            self.pages.addItem(name)
        self.pages.setFixedWidth(max(170, self.pages.sizeHintForColumn(0) + 28))
        self.pages.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.page_names = names
        self.pages.setCurrentRow(names.index(page) if page in names else 0)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Apply
            | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
                  ).setToolTip("Put this page's settings back as they were "
                               "when the app was installed")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.clicked.connect(self._clicked)
        self.buttons = bb
        outer.addWidget(bb)
        self.resize(860, 600)

    # -- pages -----------------------------------------------------------
    def _build_page(self, name: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        title = QtWidgets.QLabel(name)
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)
        opts = [o for o in _opt.OPTIONS if o.page == name and _shown(o)]
        if opts:
            form = QtWidgets.QFormLayout()
            form.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for o in opts:
                self._add_option(form, o)
            v.addLayout(form)
        if name == _opt.TOOLBAR:
            v.addWidget(self._toolbar_editor(), 1)
        elif name == SHORTCUTS_PAGE:
            v.addWidget(self._shortcut_editor(), 1)
        elif name == _opt.MODULES:
            b = QtWidgets.QPushButton("Module manager...")
            b.clicked.connect(self._module_manager)
            v.addWidget(b, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        elif name == AI_PAGE:
            v.addWidget(_note("The AI assistant's service, models, privacy and "
                              "limits have a dialog of their own."))
            b = QtWidgets.QPushButton("AI settings...")
            b.clicked.connect(self._ai_settings)
            v.addWidget(b, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        elif name == ADVANCED_PAGE:
            self._advanced(v)
        elif name == _opt.GENERAL:
            self._integration(v)
        if name not in (_opt.TOOLBAR, SHORTCUTS_PAGE):
            v.addStretch(1)
        return w

    def _add_option(self, form, o) -> None:
        val = _opt.get(o.key)
        if o.kind == "bool":
            box = QtWidgets.QCheckBox(o.label)
            box.setChecked(bool(val))
            widget, label = box, ""
        elif o.kind == "int":
            box = QtWidgets.QSpinBox()
            box.setRange(o.minimum, o.maximum or 1_000_000)
            box.setSuffix(o.suffix)
            box.setValue(int(val))
            widget, label = box, o.label + ":"
        else:
            box = QtWidgets.QComboBox()
            for v, text in o.choices:
                box.addItem(text, v)
            box.setCurrentIndex(max(0, box.findData(val)))
            widget, label = box, o.label + ":"
        self.widgets[o.key] = widget
        if not isinstance(widget, QtWidgets.QCheckBox):
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                                 QtWidgets.QSizePolicy.Policy.Fixed)
        form.addRow(label, widget)
        help_text = o.help + (" Takes effect the next time the app starts."
                              if o.restart else "")
        if help_text:
            widget.setToolTip(help_text.strip())
            form.addRow("", _note(help_text.strip()))

    def _value(self, key):
        w = self.widgets[key]
        if isinstance(w, QtWidgets.QCheckBox):
            return w.isChecked()
        if isinstance(w, QtWidgets.QSpinBox):
            return w.value()
        return w.currentData()

    def _set_value(self, key, val) -> None:
        w = self.widgets[key]
        if isinstance(w, QtWidgets.QCheckBox):
            w.setChecked(bool(val))
        elif isinstance(w, QtWidgets.QSpinBox):
            w.setValue(int(val))
        else:
            w.setCurrentIndex(max(0, w.findData(val)))

    # -- the toolbar editor ------------------------------------------------
    def _toolbar_editor(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Buttons")
        g = QtWidgets.QGridLayout(box)
        self.tb_avail = QtWidgets.QTreeWidget()
        self.tb_avail.setHeaderLabel("Menu items")
        self.tb_avail.itemDoubleClicked.connect(lambda *_a: self._tb_add())
        for menu, ids in _ga.by_menu(self.win):
            top = QtWidgets.QTreeWidgetItem([menu])
            top.setFlags(top.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            self.tb_avail.addTopLevelItem(top)
            for aid in ids:
                it = QtWidgets.QTreeWidgetItem(
                    [_ga.path_of(self.win, aid).split(" ▸ ", 1)[-1]])
                it.setData(0, QtCore.Qt.ItemDataRole.UserRole, aid)
                it.setIcon(0, _ga.action(self.win, aid).icon())
                it.setToolTip(0, _ga.path_of(self.win, aid))
                top.addChild(it)
        self.tb_avail.expandAll()
        self.tb_list = QtWidgets.QListWidget()
        self.tb_list.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.tb_list.itemDoubleClicked.connect(lambda *_a: self._tb_remove())
        self._tb_fill(_opt.toolbar())
        mid = QtWidgets.QVBoxLayout()
        mid.addStretch(1)
        for text, slot in (("Add ▶", self._tb_add), ("◀ Remove", self._tb_remove),
                           ("Separator", self._tb_sep)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            mid.addWidget(b)
        mid.addStretch(1)
        side = QtWidgets.QVBoxLayout()
        for text, d in (("Up", -1), ("Down", 1)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _c=False, d=d: self._tb_move(d))
            side.addWidget(b)
        side.addStretch(1)
        g.addWidget(QtWidgets.QLabel("Available:"), 0, 0)
        g.addWidget(QtWidgets.QLabel("On the toolbar (drag to reorder):"), 0, 2)
        g.addWidget(self.tb_avail, 1, 0)
        g.addLayout(mid, 1, 1)
        g.addWidget(self.tb_list, 1, 2)
        g.addLayout(side, 1, 3)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(2, 1)
        return box

    def _tb_item(self, aid: str) -> QtWidgets.QListWidgetItem:
        if aid == "|":
            it = QtWidgets.QListWidgetItem("──────── separator ────────")
        else:
            a = _ga.action(self.win, aid)
            it = QtWidgets.QListWidgetItem(
                _ga.path_of(self.win, aid) if a is not None
                else "%s (not available now)" % aid)
            if a is not None:
                it.setIcon(a.icon())
        it.setData(QtCore.Qt.ItemDataRole.UserRole, aid)
        return it

    def _tb_fill(self, ids: List[str]) -> None:
        self.tb_list.clear()
        for aid in ids:
            self.tb_list.addItem(self._tb_item(aid))

    def toolbar_ids(self) -> List[str]:
        return [self.tb_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                for i in range(self.tb_list.count())]

    def _tb_insert(self, aid: str) -> None:
        row = self.tb_list.currentRow()
        row = self.tb_list.count() if row < 0 else row + 1
        self.tb_list.insertItem(row, self._tb_item(aid))
        self.tb_list.setCurrentRow(row)

    def _tb_add(self) -> None:
        it = self.tb_avail.currentItem()
        aid = it.data(0, QtCore.Qt.ItemDataRole.UserRole) if it else None
        if aid and aid not in self.toolbar_ids():
            self._tb_insert(aid)

    def _tb_sep(self) -> None:
        self._tb_insert("|")

    def _tb_remove(self) -> None:
        row = self.tb_list.currentRow()
        if row >= 0:
            self.tb_list.takeItem(row)

    def _tb_move(self, d: int) -> None:
        row = self.tb_list.currentRow()
        to = row + d
        if row < 0 or not 0 <= to < self.tb_list.count():
            return
        it = self.tb_list.takeItem(row)
        self.tb_list.insertItem(to, it)
        self.tb_list.setCurrentRow(to)

    # -- the shortcut editor ---------------------------------------------
    def _shortcut_editor(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.sc_filter = QtWidgets.QLineEdit()
        self.sc_filter.setPlaceholderText("Filter by name or key...")
        self.sc_filter.textChanged.connect(self._sc_filter)
        v.addWidget(self.sc_filter)
        self.sc_tree = QtWidgets.QTreeWidget()
        self.sc_tree.setHeaderLabels(["Menu item", "Shortcut", "Default"])
        self.sc_tree.setRootIsDecorated(True)
        self.sc_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        changed = _opt.shortcuts()
        #: the shortcuts as edited here: {action id: sequence}
        self.sc_values: Dict[str, str] = {}
        for menu, ids in _ga.by_menu(self.win):
            top = QtWidgets.QTreeWidgetItem([menu])
            top.setFlags(top.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            self.sc_tree.addTopLevelItem(top)
            for aid in ids:
                a = _ga.action(self.win, aid)
                if a is None or a.menu() is not None:
                    continue
                default = _ga.default_shortcut(self.win, aid)
                self.sc_values[aid] = changed.get(aid, default)
                it = QtWidgets.QTreeWidgetItem(
                    [_ga.path_of(self.win, aid).split(" ▸ ", 1)[-1], "",
                     _native(default)])
                it.setData(0, QtCore.Qt.ItemDataRole.UserRole, aid)
                it.setIcon(0, a.icon())
                top.addChild(it)
        self.sc_tree.expandAll()
        self.sc_tree.currentItemChanged.connect(self._sc_pick)
        v.addWidget(self.sc_tree, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Shortcut:"))
        self.sc_edit = QtWidgets.QKeySequenceEdit()
        self.sc_edit.setMaximumSequenceLength(1)
        self.sc_edit.keySequenceChanged.connect(self._sc_typed)
        row.addWidget(self.sc_edit, 1)
        clear = QtWidgets.QPushButton("None")
        clear.setToolTip("No shortcut for this item")
        clear.clicked.connect(lambda: self._sc_set(""))
        reset = QtWidgets.QPushButton("Default")
        reset.clicked.connect(self._sc_default)
        row.addWidget(clear)
        row.addWidget(reset)
        v.addLayout(row)
        self.sc_warn = QtWidgets.QLabel("")
        self.sc_warn.setWordWrap(True)
        v.addWidget(self.sc_warn)
        self._sc_enable(False)
        self._sc_refresh()
        return w

    def _sc_current(self) -> Optional[str]:
        it = self.sc_tree.currentItem()
        return it.data(0, QtCore.Qt.ItemDataRole.UserRole) if it else None

    def _sc_enable(self, on: bool) -> None:
        self.sc_edit.setEnabled(on)

    def _sc_pick(self, *_a) -> None:
        aid = self._sc_current()
        self._sc_enable(aid is not None)
        self.sc_edit.blockSignals(True)
        self.sc_edit.setKeySequence(QtGui.QKeySequence(
            self.sc_values.get(aid, "") if aid else ""))
        self.sc_edit.blockSignals(False)

    def _sc_typed(self, seq) -> None:
        self._sc_set(seq.toString())

    def _sc_default(self) -> None:
        aid = self._sc_current()
        if aid:
            self._sc_set(_ga.default_shortcut(self.win, aid))

    def _sc_set(self, seq: str) -> None:
        aid = self._sc_current()
        if not aid:
            return
        self.sc_values[aid] = seq
        self.sc_edit.blockSignals(True)
        self.sc_edit.setKeySequence(QtGui.QKeySequence(seq))
        self.sc_edit.blockSignals(False)
        self._sc_refresh()

    def _sc_refresh(self) -> None:
        clash = _ga.conflicts(self.sc_values)
        bad = {aid for ids in clash.values() for aid in ids}
        for i in range(self.sc_tree.topLevelItemCount()):
            top = self.sc_tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                aid = it.data(0, QtCore.Qt.ItemDataRole.UserRole)
                seq = self.sc_values.get(aid, "")
                it.setText(1, _native(seq))
                f = it.font(1)
                f.setBold(seq != _ga.default_shortcut(self.win, aid))
                it.setFont(1, f)
                it.setForeground(1, QtGui.QBrush(
                    QtGui.QColor(220, 50, 50) if aid in bad else
                    self.palette().color(QtGui.QPalette.ColorRole.Text)))
        if clash:
            self.sc_warn.setText("Used twice: " + "; ".join(
                "%s by %s" % (_native(k), " and ".join(
                    _ga.path_of(self.win, a) for a in v))
                for k, v in clash.items()) + ". Change one before saving.")
        else:
            self.sc_warn.setText("")
        self.sc_warn.setVisible(bool(clash))

    def _sc_filter(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.sc_tree.topLevelItemCount()):
            top = self.sc_tree.topLevelItem(i)
            any_shown = False
            for j in range(top.childCount()):
                it = top.child(j)
                hit = not text or text in it.text(0).lower() or \
                    text in it.text(1).lower()
                it.setHidden(not hit)
                any_shown |= hit
            top.setHidden(not any_shown)

    # -- advanced ----------------------------------------------------------
    def _integration(self, v) -> None:
        """General: the converter's entry in the file manager's right-click
        menu -- the installer offers it; this adds or removes it later.
        It is a setting of this PC (the registry, or the desktop's menu
        files), not of the options file, so it is read from there."""
        self.shell_box = None
        try:
            from .convert import shellmenu as _sm
        except Exception:
            return
        if not _sm.supported():
            return
        box = QtWidgets.QGroupBox("Right-click menu")
        bl = QtWidgets.QVBoxLayout(box)
        self.shell_box = QtWidgets.QCheckBox(
            "Show \"%s\" when right-clicking definitions, value files and "
            "binaries" % _sm.LABEL)
        try:
            self._shell_was = bool(_sm.installed())
        except Exception:
            self._shell_was = False
        self.shell_box.setChecked(self._shell_was)
        bl.addWidget(self.shell_box)
        bl.addWidget(_note(
            "Opens the files in the converter; files right-clicked together "
            "open in one window. Double-clicking a file still opens what "
            "opened it before. For your user only; no administrator rights "
            "needed." + ("\nOn Windows 11 it is under \"Show more options\"."
                         if os.name == "nt" else "")))
        v.addSpacing(8)
        v.addWidget(box)

    def _apply_integration(self) -> None:
        box = getattr(self, "shell_box", None)
        if box is None or box.isChecked() == self._shell_was:
            return
        from .convert import shellmenu as _sm
        try:
            if box.isChecked():
                _sm.install()
            else:
                _sm.remove(everywhere=True)
            self._shell_was = box.isChecked()
        except _sm.ShellMenuError as exc:
            QtWidgets.QMessageBox.warning(self, "Right-click menu", str(exc))
            box.setChecked(self._shell_was)

    def _advanced(self, v) -> None:
        v.addWidget(_note("Keep your options, toolbar and shortcuts in a file, "
                          "to back them up or use them on another computer."))
        row = QtWidgets.QHBoxLayout()
        for text, slot in (("Export...", self._export), ("Import...", self._import)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addSpacing(16)
        v.addWidget(_note("Put every option, the toolbar and the keyboard "
                          "shortcuts back as they were when the app was "
                          "installed. Recent files, open tabs and saved "
                          "connections are kept."))
        b = QtWidgets.QPushButton("Reset all options...")
        b.clicked.connect(self._reset_all)
        v.addWidget(b, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

    def _export(self) -> None:
        from . import branding
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export options", "%s options.json" % branding.APP_NAME,
            "Options (*.json)")
        if path:
            try:
                self.apply()
                _opt.export_to(path)
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, "Export options", str(ex))

    def _import(self) -> None:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import options", "", "Options (*.json)")
        if not path:
            return
        try:
            _opt.import_from(path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Import options",
                                          "%s could not be read: %s" % (path, ex))
            return
        self._reload()

    def _reset_all(self) -> None:
        SB = QtWidgets.QMessageBox.StandardButton
        if QtWidgets.QMessageBox.question(
                self, "Reset all options", "Put every option, the toolbar and "
                "the keyboard shortcuts back to their defaults?",
                SB.Yes | SB.Cancel, SB.Cancel) != SB.Yes:
            return
        _opt.reset()
        self._reload()

    def _reload(self) -> None:
        """The widgets show the stored options again."""
        for key in self.widgets:
            self._set_value(key, _opt.get(key))
        self._tb_fill(_opt.toolbar())
        changed = _opt.shortcuts()
        for aid in self.sc_values:
            self.sc_values[aid] = changed.get(aid, _ga.default_shortcut(self.win, aid))
        self._sc_pick()
        self._sc_refresh()

    def _module_manager(self) -> None:
        self.win.open_modules()
        # its "Allow unsigned modules" is this page's switch too
        self._set_value("modules/allow_unsigned",
                        _opt.get("modules/allow_unsigned"))

    def _ai_settings(self) -> None:
        from . import gui_ai
        try:
            gui_ai.AiSettingsDialog(gui_ai.state(self.win)).exec()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "AI settings", str(ex))

    # -- buttons -----------------------------------------------------------
    def _clicked(self, button) -> None:
        role = self.buttons.buttonRole(button)
        R = QtWidgets.QDialogButtonBox.ButtonRole
        if role == R.ApplyRole:
            self.apply()
        elif role == R.ResetRole:
            self.restore_page_defaults()

    def restore_page_defaults(self) -> None:
        name = self.page_names[self.pages.currentRow()]
        for o in _opt.OPTIONS:
            if o.page == name and o.key in self.widgets:
                self._set_value(o.key, o.default)
        if name == _opt.TOOLBAR:
            self._tb_fill(_opt.DEFAULT_TOOLBAR)
        elif name == SHORTCUTS_PAGE:
            for aid in self.sc_values:
                self.sc_values[aid] = _ga.default_shortcut(self.win, aid)
            self._sc_pick()
            self._sc_refresh()

    def apply(self) -> bool:
        """Store and apply everything as edited. False (nothing stored)
        while two items share a shortcut."""
        if _ga.conflicts(self.sc_values):
            self.pages.setCurrentRow(self.page_names.index(SHORTCUTS_PAGE))
            QtWidgets.QMessageBox.warning(
                self, "Options", "Two menu items have the same shortcut; "
                "change one of them first.")
            return False
        for key in self.widgets:
            _opt.set(key, self._value(key))
        _opt.set_toolbar(self.toolbar_ids())
        stored = _opt.shortcuts()
        changes: Dict[str, Optional[str]] = {}
        for aid, seq in self.sc_values.items():
            default = _ga.default_shortcut(self.win, aid)
            want = None if seq == default else seq
            if stored.get(aid) != want:
                changes[aid] = want
        _opt.set_shortcuts(changes)
        self._apply_integration()
        return True

    def accept(self) -> None:
        if self.apply():
            super().accept()


def _shown(o) -> bool:
    """A module's option only while that module is installed."""
    if not o.module:
        return True
    from .modules import registry as _registry
    return _registry.is_installed(o.module)


def _note(text: str) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text)
    lab.setWordWrap(True)
    pal = lab.palette()
    pal.setColor(QtGui.QPalette.ColorRole.WindowText,
                 pal.color(QtGui.QPalette.ColorGroup.Disabled,
                           QtGui.QPalette.ColorRole.WindowText))
    lab.setPalette(pal)
    return lab


def _native(seq: str) -> str:
    return QtGui.QKeySequence(seq).toString(
        QtGui.QKeySequence.SequenceFormat.NativeText) if seq else ""
