# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools menu: only the tools the active tab can use.

A tool that works on the tab (``needs_file`` / ``enabled_when`` in
``MainWindow._act``) and is disabled for it -- another platform's editor, a
module's own tool (:mod:`ovtsuite.gui_modtools`), a tool that needs the
module's maps, anything with no file open -- is hidden while the menu is
open, and a sub-menu goes when none of its items is left. Window-wide tools
(the flasher, the logger, Modules...) always show, and so does a disabled
sub-menu.

Only the menu hides them: the actions stay visible elsewhere (a toolbar
button greys out rather than disappearing as tabs change), and are made
visible again as the menu closes.
"""


def _tab_actions(win) -> set:
    return set(getattr(win, "_needs_file", [])) | {
        a for a, _c in getattr(win, "_conditional", [])}


def _hide(menu, tab, hidden) -> bool:
    """Hide ``menu``'s disabled tab tools (into ``hidden``); True when
    something in it is still shown."""
    shown = False
    for a in menu.actions():
        if a.isSeparator() or not a.isVisible():
            continue
        sub = a.menu()
        if sub is not None:
            if not a.isEnabled():
                shown = True                # disabled: says why
            elif _hide(sub, tab, hidden):
                shown = True
            else:
                a.setVisible(False)
                hidden.append(a)
        elif a in tab and not a.isEnabled():
            a.setVisible(False)
            hidden.append(a)
        else:
            shown = True
    return shown


def _show(win):
    _restore(win)
    win._refresh_enabled()                  # the tab's state as it is now
    _hide(win.tools_menu, _tab_actions(win), win.__dict__["_tools_hidden"])


def _restore(win):
    for a in win.__dict__.pop("_tools_hidden", []):
        try:
            a.setVisible(True)
        except RuntimeError:                # gone with its window
            pass
    win.__dict__["_tools_hidden"] = []


def install(win) -> None:
    """Follow the active tab whenever the Tools menu opens."""
    m = win.tools_menu
    m.aboutToShow.connect(lambda: _show(win))
    m.aboutToHide.connect(lambda: _restore(win))


__all__ = ["install"]
