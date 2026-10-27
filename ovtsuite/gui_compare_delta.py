# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Controls for showing the comparison, and this file's own table, as a
difference (:mod:`ovtsuite.compare_delta`; model side:
``TableModel.set_delta``).

One menu holds it all (:func:`fill_menu`): the comparison's view, this
file's view (read only while it shows differences), and the two options
that apply to both (blank the cells that do not differ, tint by
direction). It is offered

* on the table's edit bar, as the **Difference view** button beside
  **3D surface** and **Dyno** (:func:`make_button`);
* in **View > Difference view** (:func:`add_menu`), which makes it a toolbar
  button too (Tools > Options > Toolbar: *View ▸ Difference view*);
* in Tools > Options > Editor, as the four options below.

The choices are options, so every tab and window follows them and they are
remembered: ``ui/compare_delta`` (the comparison), ``ui/primary_delta``
(this file), ``ui/compare_delta_blank``, ``ui/compare_delta_tint``. The tint
is blue below, red above, opaque with dark text so the numbers read in both
themes.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import compare_delta as _cd
from . import options as _opt

MODE_KEY = "ui/compare_delta"
PRIMARY_KEY = "ui/primary_delta"
BLANK_KEY = "ui/compare_delta_blank"
TINT_KEY = "ui/compare_delta_tint"
KEYS = (MODE_KEY, PRIMARY_KEY, BLANK_KEY, TINT_KEY)

TITLE = "Difference view"
TIP = ("Show the comparison, and this file's own table, as the difference "
       "from each other instead of values. Copying always copies the real "
       "values; this file's table is read only while it shows differences.")


def mode() -> str:
    return _cd.valid(_opt.get(MODE_KEY))


def primary_mode() -> str:
    return _cd.valid(_opt.get(PRIMARY_KEY))


def active() -> bool:
    return mode() != "abs" or primary_mode() != "abs"


def apply(model) -> None:
    """Put the options into effect in one table model."""
    model.set_delta(mode(), _opt.get(BLANK_KEY), _opt.get(TINT_KEY),
                    primary_mode())


# ----------------------------------------------------------------- the menu
def fill_menu(menu: QtWidgets.QMenu) -> QtWidgets.QMenu:
    """The choices, as radio items and tick boxes that read the options each
    time the menu opens and write them when picked."""
    menu.setToolTipsVisible(True)
    groups = []
    for title, key, labels in (
            ("Comparison shows", MODE_KEY, dict(_cd.MODES)),
            ("This file's table shows (read only)", PRIMARY_KEY,
             dict(_cd.MODES, abs="Values"))):
        menu.addSection(title)
        group = QtGui.QActionGroup(menu)
        group.setExclusive(True)
        acts = {}
        for m, text in labels.items():
            a = menu.addAction("Values" if m == "abs" else text)
            a.setCheckable(True)
            group.addAction(a)
            a.triggered.connect(lambda _c=False, key=key, m=m: _opt.set(key, m))
            acts[m] = a
        groups.append((key, acts))
    menu.addSeparator()
    ticks = []
    for key, text, tip in (
            (BLANK_KEY, "Blank the cells that do not differ",
             "Leave only the changes on screen"),
            (TINT_KEY, "Tint by direction",
             "Blue where this file is lower than the comparison, red where "
             "it is higher")):
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setToolTip(tip)
        a.triggered.connect(lambda on, key=key: _opt.set(key, bool(on)))
        ticks.append((key, a))

    def sync():
        for key, acts in groups:
            acts[_cd.valid(_opt.get(key))].setChecked(True)
        for key, a in ticks:
            a.setChecked(bool(_opt.get(key)))
    menu.aboutToShow.connect(sync)
    sync()
    return menu


def add_menu(win, view_menu: QtWidgets.QMenu) -> QtWidgets.QMenu:
    """View > Difference view (also what the toolbar can offer)."""
    sub = view_menu.addMenu(TITLE)
    fill_menu(sub)
    return sub


# ----------------------------------------------------------- the edit bar
def make_button(ws) -> QtWidgets.QPushButton:
    """The **Difference view** button for a tab's edit bar."""
    btn = QtWidgets.QPushButton(TITLE)
    btn.setToolTip(TIP)
    btn.setObjectName("differenceViewButton")
    btn.setMenu(fill_menu(QtWidgets.QMenu(btn)))
    ws.delta_btn = btn
    ws.model.modelReset.connect(lambda: refresh_button(ws))
    refresh_button(ws)
    return btn


def refresh_button(ws) -> None:
    """On while a comparison is loaded; says so when a difference view is
    showing."""
    btn = getattr(ws, "delta_btn", None)
    if btn is None:
        return
    try:
        has = ws.model.sep_row() is not None
        btn.setEnabled(has)
        btn.setText(TITLE + (" (on)" if has and active() else ""))
    except RuntimeError:                # the tab is going away
        pass


def attach(grid) -> None:
    """A tab's comparison grid: put the options into its model, and say why
    an edit of a table showing differences does nothing."""
    apply(grid.ws.model)
    grid.ws.model.edit_blocked.connect(lambda: _blocked(grid.ws))


def _blocked(ws) -> None:
    try:
        ws.win.statusBar().showMessage(
            "This file's table shows differences, so it is read only. "
            "Set \"This file's table shows\" to Values to edit.", 8000)
    except Exception:
        pass


def apply_all(win) -> None:
    """A difference option changed: every tab of ``win`` follows now."""
    try:
        tabs = list(win.workspaces())
    except Exception:
        tabs = []
    for ws in tabs:
        try:
            apply(ws.model)
            refresh_button(ws)
            g = getattr(ws, "cmp_grid", None)
            if g is not None and g.active():
                QtCore.QTimer.singleShot(0, g._fit)
        except RuntimeError:            # a tab being closed
            pass
