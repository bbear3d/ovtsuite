# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Controls for showing the comparison, and the primary's own table, as a
difference (:mod:`ovtsuite.compare_delta`; model side:
``TableModel.set_delta``).

One menu holds it all (:func:`fill_menu`). It names the two files at the top
-- **Primary** is the file being edited, **Comparison** the one it is
compared with -- then asks two things:

* **Show as**: the values themselves, or primary - comparison, comparison -
  primary, a percentage or a ratio;
* **Show it in**: the comparison block, the primary table (read only while it
  shows differences), or both;

and has the two options that apply to both: blank the cells that do not
differ, and tint a difference by direction (red where the primary is the
higher, blue where it is the lower, whatever the table shows). A cell that
differs always has the orange outline (:mod:`ovtsuite.gui_cellmarks`), which
the tint and a heat map do not hide.

It is offered

* on the table's edit bar, as the **Difference view** button beside
  **3D surface** and **Dyno** (:func:`make_button`);
* in **View > Difference view** (:func:`add_menu`), which makes it a toolbar
  button too (Tools > Options > Toolbar: *View ▸ Difference view*);
* in Tools > Options > Editor, as the four options below.

The choices are options, so every tab and window follows them and they are
remembered: ``ui/compare_delta`` (what to show), ``ui/compare_delta_in``
(where), ``ui/compare_delta_blank``, ``ui/compare_delta_tint``.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import compare_delta as _cd
from . import options as _opt

MODE_KEY = "ui/compare_delta"
IN_KEY = "ui/compare_delta_in"
BLANK_KEY = "ui/compare_delta_blank"
TINT_KEY = "ui/compare_delta_tint"
KEYS = (MODE_KEY, IN_KEY, BLANK_KEY, TINT_KEY)

#: where the difference is shown: ``(value, label)``
WHERE = (("cmp", "The comparison block"),
         ("primary", "The primary table (read only)"),
         ("both", "Both"))

TITLE = "Difference view"
TIP = ("Show the comparison, the primary's own table, or both as the "
       "difference between the two instead of values. Copying always copies "
       "the real values; the primary's table is read only while it shows "
       "differences.")


def shown_as() -> str:
    """What the difference view shows (``"abs"``: values, the view is off)."""
    return _cd.valid(_opt.get(MODE_KEY))


def where() -> str:
    return _opt.get(IN_KEY)


def mode() -> str:
    """The comparison block's mode."""
    return shown_as() if where() in ("cmp", "both") else "abs"


def primary_mode() -> str:
    """The primary table's mode."""
    return shown_as() if where() in ("primary", "both") else "abs"


def active() -> bool:
    return shown_as() != "abs"


def apply(model) -> None:
    """Put the options into effect in one table model."""
    model.set_delta(mode(), _opt.get(BLANK_KEY), _opt.get(TINT_KEY),
                    primary_mode())


def file_names(ws) -> tuple:
    """``(primary, comparison)`` file names of tab ``ws`` ("" when none)."""
    def name(s):
        bf = getattr(s, "binfile", None)
        return getattr(bf, "name", "") or ""
    if ws is None:
        return "", ""
    return name(getattr(ws, "primary", None)), name(getattr(ws, "comparison", None))


def _swatch(*colours, outline: bool = False) -> QtGui.QIcon:
    """A small square for a menu line: split in the given colours, or framed
    in the difference outline."""
    from .gui_cellmarks import DIFF_OUTLINE
    pm = QtGui.QPixmap(14, 14)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    try:
        if outline:
            p.setPen(QtGui.QPen(DIFF_OUTLINE, 2))
            p.drawRect(1, 1, 11, 11)
        else:
            w = 14 // len(colours)
            for i, c in enumerate(colours):
                p.fillRect(i * w, 0, w, 14, c)
    finally:
        p.end()
    return QtGui.QIcon(pm)


# ----------------------------------------------------------------- the menu
def fill_menu(menu: QtWidgets.QMenu,
              tab: Optional[Callable[[], object]] = None) -> QtWidgets.QMenu:
    """The choices, as radio items and tick boxes that read the options each
    time the menu opens and write them when picked. ``tab()`` gives the tab
    whose two file names head the menu (None: the lines say Primary and
    Comparison only)."""
    from . import gui as _g
    menu.setToolTipsVisible(True)
    prim = menu.addAction("Primary")
    prim.setEnabled(False)
    cmpa = menu.addAction("Comparison")
    cmpa.setEnabled(False)
    menu.addSection("Show as")
    group = QtGui.QActionGroup(menu)
    group.setExclusive(True)
    modes = {}
    for m, text in _cd.MODES:
        a = menu.addAction(text)
        a.setCheckable(True)
        group.addAction(a)
        a.triggered.connect(lambda _c=False, m=m: _opt.set(MODE_KEY, m))
        modes[m] = a
    menu.addSection("Show it in")
    wgroup = QtGui.QActionGroup(menu)
    wgroup.setExclusive(True)
    wheres = {}
    for w, text in WHERE:
        a = menu.addAction(text)
        a.setCheckable(True)
        wgroup.addAction(a)
        a.triggered.connect(lambda _c=False, w=w: _opt.set(IN_KEY, w))
        wheres[w] = a
    menu.addSeparator()
    ticks = []
    for key, text, tip, icon in (
            (BLANK_KEY, "Blank the cells that do not differ",
             "Leave only the changes on screen", None),
            (TINT_KEY, "Tint by direction",
             "Red where the primary is higher than the comparison, blue "
             "where it is lower. Not shown while the table's heat map is on.",
             _swatch(_g.DELTA_DOWN_BG, _g.DELTA_UP_BG))):
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setToolTip(tip)
        if icon is not None:
            a.setIcon(icon)
        a.triggered.connect(lambda on, key=key: _opt.set(key, bool(on)))
        ticks.append((key, a))
    note = menu.addAction("A cell that differs has an orange outline")
    note.setEnabled(False)
    note.setIcon(_swatch(outline=True))

    def sync():
        p, c = file_names(tab() if tab is not None else None)
        prim.setText("Primary: %s" % p if p else "Primary: the file being edited")
        cmpa.setText("Comparison: %s" % c if c else "Comparison: none loaded")
        modes[shown_as()].setChecked(True)
        wheres[where()].setChecked(True)
        for w, a in wheres.items():
            a.setEnabled(active())
        for key, a in ticks:
            a.setChecked(bool(_opt.get(key)))
    menu.aboutToShow.connect(sync)
    sync()
    return menu


def add_menu(win, view_menu: QtWidgets.QMenu) -> QtWidgets.QMenu:
    """View > Difference view (also what the toolbar can offer)."""
    sub = view_menu.addMenu(TITLE)
    fill_menu(sub, lambda: getattr(win, "current", None))
    return sub


# ----------------------------------------------------------- the edit bar
def make_button(ws) -> QtWidgets.QPushButton:
    """The **Difference view** button for a tab's edit bar."""
    btn = QtWidgets.QPushButton(TITLE)
    btn.setToolTip(TIP)
    btn.setObjectName("differenceViewButton")
    btn.setMenu(fill_menu(QtWidgets.QMenu(btn), lambda: ws))
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
        btn.setToolTip(TIP if not (has and active()) else
                       "%s\nShowing: %s, in %s." % (
                           TIP, dict(_cd.MODES)[shown_as()],
                           dict(WHERE)[where()].lower()))
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
            "The primary table shows differences, so it is read only. "
            "Set the Difference view to Values (or to show it in the "
            "comparison block) to edit.", 8000)
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
