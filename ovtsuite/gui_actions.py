# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Every menu item of the main window, by a stable id.

The menus are built in ``MainWindow._make_actions``; :func:`register` walks
the finished menu bar and gives each item an id made from where it is --
``file/save``, ``tools/ecu-read-write``, ``tools/ai/assistant`` -- stored as
the action's ``objectName`` and in ``win.actions_by_id``. The toolbar
(:mod:`ovtsuite.gui_toolbar`) and the Options dialog's toolbar and keyboard
shortcut pages (:mod:`ovtsuite.gui_options`) work with those ids, and use
the very same ``QAction`` objects as the menus: enabled state, check marks
and shortcuts stay in step everywhere.

A sub-menu has an id too (``file/open-recent``): on the toolbar it is a
button that drops the menu down. Items of menus filled each time they open
(recent files, open tabs, saved versions) have none.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import shiboken6
from PySide6 import QtGui, QtWidgets

from . import icons as _icons
from . import options as _opt

#: menus whose items are rebuilt every time they open: no ids inside
_DYNAMIC = ("recent_menu", "project_open_menu", "history_menu",
            "recent_cmp_menu", "compare_tab_menu", "compare_version_menu",
            "copy_tab_menu")


def slug(text: str) -> str:
    """``"&Save As..."`` -> ``"save-as"``; ``"Help contents\\tF1"`` ->
    ``"help-contents"``; ``"About OVT-Suite..."`` -> ``"about"``."""
    from . import branding
    text = text.split("\t", 1)[0].replace("&&", "and").replace("&", "")
    text = text.replace(branding.APP_NAME, "")
    text = re.sub(r"[^0-9a-zA-Z]+", "-", text.lower()).strip("-")
    return text or "item"


def label(action: QtGui.QAction) -> str:
    """An action's text as a person reads it."""
    return action.text().split("\t", 1)[0].replace("&&", "\0").replace(
        "&", "").replace("\0", "&").rstrip(".").rstrip()


def _ids(win) -> Dict[str, QtGui.QAction]:
    if "actions_by_id" not in win.__dict__:
        win.__dict__["actions_by_id"] = {}
    return win.__dict__["actions_by_id"]


def register(win) -> Dict[str, QtGui.QAction]:
    """Give every menu item of ``win`` its id (again: an item added since
    gets one too). Returns ``win.actions_by_id``."""
    ids = _ids(win)
    for aid in [k for k, a in ids.items() if not shiboken6.isValid(a)]:
        del ids[aid]                    # gone (its menu was rebuilt)
    paths = win.__dict__.setdefault("action_paths", {})
    dynamic = {id(win.__dict__.get(n)) for n in _DYNAMIC
               if win.__dict__.get(n) is not None}
    taken = {id(a) for a in ids.values()}

    def walk(menu: QtWidgets.QMenu, prefix: str, names: List[str]):
        for a in menu.actions():
            if a.isSeparator() or not a.text():
                continue
            sub = a.menu()
            aid = prefix + slug(a.text())
            if id(a) not in taken:
                base, i = aid, 2
                while aid in ids:
                    aid = "%s-%d" % (base, i)
                    i += 1
                ids[aid] = a
                taken.add(id(a))
                if sub is None and a not in win.actions():
                    win.addAction(a)        # its shortcut works menu closed
                a.setObjectName(aid)
                paths[aid] = " ▸ ".join(names + [label(a)])
                _defaults(win).setdefault(aid, a.shortcut().toString())
            else:
                aid = a.objectName() or aid
            if sub is not None and id(sub) not in dynamic:
                walk(sub, aid + "/", names + [label(a)])

    for top in win.menuBar().actions():
        menu = top.menu()
        if menu is not None:
            walk(menu, slug(top.text()) + "/", [label(top)])
    for aid, a, path in win.__dict__.get("extra_actions", ()):
        if id(a) in taken or not shiboken6.isValid(a):
            continue
        ids[aid] = a
        taken.add(id(a))
        if a not in win.actions():
            win.addAction(a)
        a.setObjectName(aid)
        paths[aid] = path
        _defaults(win).setdefault(aid, a.shortcut().toString())
    return ids


def add_extra(win, aid: str, action: QtGui.QAction, path: str) -> None:
    """An action in no menu that the toolbar (and the Options pages) can
    still offer -- the Find panel's on/off button. ``path`` is where it is
    listed: ``"Edit ▸ Find panel"``."""
    win.__dict__.setdefault("extra_actions", []).append((aid, action, path))


def _defaults(win) -> Dict[str, str]:
    """Each action's shortcut as the app defines it."""
    if "_default_shortcuts" not in win.__dict__:
        win.__dict__["_default_shortcuts"] = {}
    return win.__dict__["_default_shortcuts"]


def default_shortcut(win, aid: str) -> str:
    return _defaults(win).get(aid, "")


def by_menu(win) -> List[Tuple[str, List[str]]]:
    """``[(menu name, [action id, ...])]`` in menu order, for the Options
    pages."""
    out: Dict[str, List[str]] = {}
    for aid in _ids(win):
        top = (win.__dict__.get("action_paths", {}).get(aid, aid)
               .split(" ▸ ", 1)[0])
        out.setdefault(top, []).append(aid)
    return list(out.items())


def path_of(win, aid: str) -> str:
    return win.__dict__.get("action_paths", {}).get(aid, aid)


def action(win, aid: str) -> Optional[QtGui.QAction]:
    return _ids(win).get(aid)


# ------------------------------------------------------------- applying
def apply_icons(win) -> None:
    """Icons on the actions that have one (menus show them unless switched
    off in Options > Appearance)."""
    for aid, a in _ids(win).items():
        ic = _icons.for_action(aid)
        if not ic.isNull():
            a.setIcon(ic)
    show = bool(_opt.get("ui/menu_icons"))
    for a in _ids(win).values():
        a.setIconVisibleInMenu(show)


def apply_shortcuts(win) -> None:
    """The shortcuts changed in Options over the app's own."""
    changed = _opt.shortcuts()
    for aid, a in register(win).items():
        seq = changed.get(aid, default_shortcut(win, aid))
        a.setShortcut(QtGui.QKeySequence(seq))


def conflicts(assign: Dict[str, str]) -> Dict[str, List[str]]:
    """``{sequence: [action ids]}`` for every key sequence given to more
    than one action."""
    seen: Dict[str, List[str]] = {}
    for aid, seq in assign.items():
        norm = QtGui.QKeySequence(seq).toString() if seq else ""
        if norm:
            seen.setdefault(norm, []).append(aid)
    return {k: v for k, v in seen.items() if len(v) > 1}
