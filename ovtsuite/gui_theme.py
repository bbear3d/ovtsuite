# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Light, dark or the system's theme (Options > Appearance > Theme).

* **System** -- the platform style with the system's colours, as always
  (Qt's ``windows11`` style follows Windows' dark app mode itself).
* **Light** -- the platform style, light whatever Windows is set to.
* **Dark** -- the platform style in its dark colours where it has them
  (Windows 11); elsewhere (Windows 10's style has no dark mode) Qt's Fusion
  style with a dark palette.

The Windows 11 style is never swapped out for another: a fresh instance of
it, made after start-up, loses the accent colour of ticked boxes and the
like -- so light, dark and system only change its colour scheme.

The map grid's fills (differences, edits, Find hits) keep their colours and
their dark text in every theme; everything else follows the palette, and the
icons (:mod:`ovtsuite.icons`) are drawn in its text colour.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import options as _opt

_BASE_PROP = "ovt_base_style"
#: platform styles that draw a dark colour scheme themselves
_DARK_CAPABLE = ("windows11", "macos")


def dark_palette() -> QtGui.QPalette:
    P = QtGui.QPalette
    R, G = P.ColorRole, P.ColorGroup
    c = QtGui.QColor
    pal = P()
    for role, col in ((R.Window, c(45, 45, 48)), (R.WindowText, c(225, 225, 225)),
                      (R.Base, c(30, 30, 32)), (R.AlternateBase, c(40, 40, 43)),
                      (R.ToolTipBase, c(50, 50, 54)), (R.ToolTipText, c(230, 230, 230)),
                      (R.PlaceholderText, c(140, 140, 140)),
                      (R.Text, c(225, 225, 225)), (R.Button, c(55, 55, 59)),
                      (R.ButtonText, c(225, 225, 225)), (R.BrightText, c(255, 90, 90)),
                      (R.Light, c(75, 75, 80)), (R.Midlight, c(62, 62, 66)),
                      (R.Mid, c(40, 40, 43)), (R.Dark, c(25, 25, 27)),
                      (R.Shadow, c(10, 10, 10)),
                      (R.Highlight, c(42, 110, 190)), (R.HighlightedText, c(255, 255, 255)),
                      (R.Link, c(110, 170, 245)), (R.LinkVisited, c(170, 140, 230)),
                      (R.Accent, c(42, 110, 190))):
        pal.setColor(role, col)
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(G.Disabled, role, c(120, 120, 120))
    pal.setColor(G.Disabled, R.Highlight, c(70, 70, 75))
    pal.setColor(G.Disabled, R.HighlightedText, c(150, 150, 150))
    return pal


def _base_style_name(app) -> str:
    """The style the app started with (before any theme switched it)."""
    name = app.property(_BASE_PROP)
    if not name:
        st = app.style()
        base = st.baseStyle() if isinstance(st, QtWidgets.QProxyStyle) and \
            st.baseStyle() is not None else st
        name = base.name() if base is not None else "fusion"
        app.setProperty(_BASE_PROP, name)
    return str(name)


def is_dark(app=None) -> bool:
    app = app or QtWidgets.QApplication.instance()
    return bool(app is not None and app.property("ovt_theme") == "dark")


def apply(app=None, theme: str = None) -> str:
    """Put ``theme`` ("system", "light", "dark"; default: the option) into
    effect now. Returns the theme applied."""
    app = app or QtWidgets.QApplication.instance()
    if app is None:
        return ""
    theme = theme or _opt.get("ui/theme")
    base = _base_style_name(app)
    hints = app.styleHints()
    scheme = QtCore.Qt.ColorScheme
    if theme == "dark" and base.lower() in _DARK_CAPABLE:
        hints.setColorScheme(scheme.Dark)       # the style draws it dark
        _clear_palette(app)
    elif theme == "dark":
        hints.setColorScheme(scheme.Dark)
        if (app.style().name() or "").lower() != "fusion":
            app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
        app.setPalette(dark_palette())
        app.setProperty(_OWN_PALETTE, True)
    else:
        hints.setColorScheme(scheme.Light if theme == "light" else scheme.Unknown)
        cur = app.style()
        cur_name = ((cur.baseStyle() if isinstance(cur, QtWidgets.QProxyStyle)
                     and cur.baseStyle() is not None else cur).name() or "")
        if cur_name.lower() != base.lower():
            st = QtWidgets.QStyleFactory.create(base)
            if st is not None:
                app.setStyle(st)
                from . import gui_style
                gui_style.install(app)  # Windows 11 scroll bars, as at start
        _clear_palette(app)
    app.setProperty("ovt_theme", theme)
    _follow_windows(app)
    # Windows answers a colour-scheme request with a theme-change event a
    # moment later; the check boxes are put right after it (see _settle)
    QtCore.QTimer.singleShot(0, lambda: _settle(app))
    for w in app.topLevelWidgets():
        w.update()
    return theme


#: the app palette was set by us (the Fusion dark one), not the platform's
_OWN_PALETTE = "ovt_own_palette"
#: the widget classes put right by :func:`_settle`
_ACCENTED = ("QCheckBox", "QRadioButton")


def _clear_palette(app) -> None:
    """Back to the platform's own palette (Windows' light or dark, as the
    colour scheme says) where the Fusion dark one had been set -- never the
    style's "standard" palette, which is always light and on a dark Windows
    11 mixed with the style's own dark drawing."""
    if app.property(_OWN_PALETTE):
        app.setPalette(QtGui.QPalette())
        app.setProperty(_OWN_PALETTE, False)


def _settle(app) -> None:
    """After a colour-scheme switch while the app runs, Qt (6.11, Windows)
    leaves check boxes and radio buttons with the window colour as their
    accent: a ticked box loses its fill. They get the app's palette as their
    own (each time the scheme changes, so it never goes stale)."""
    pal = QtGui.QPalette(app.palette())
    for cls in _ACCENTED:
        QtWidgets.QApplication.setPalette(pal, cls)


def _follow_windows(app) -> None:
    """Windows switching light / dark while the app runs (System theme):
    put the check boxes right again afterwards."""
    if app.property("ovt_follows_scheme"):
        return
    app.setProperty("ovt_follows_scheme", True)
    app.styleHints().colorSchemeChanged.connect(
        lambda _s, app=app: QtCore.QTimer.singleShot(0, lambda: _settle(app)))

def apply_saved(app) -> None:
    """At start-up, before the first window: the theme in Options (System,
    the default, changes nothing)."""
    _base_style_name(app)
    if _opt.get("ui/theme") != "system":
        apply(app)
