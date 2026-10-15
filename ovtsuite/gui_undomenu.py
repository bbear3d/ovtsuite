# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Undo / Redo with a history: the toolbar buttons drop down a list of the
steps they would take, and picking one undoes (or redoes) every step down to
it at once. Their tooltips name the next step, and they are greyed out when
there is nothing to undo or redo.

The actions themselves stay the Edit menu's plain "Undo" / "Redo" (the
view-only restriction and the toolbar ids match those); only the toolbar
BUTTONS get the menu (:func:`decorate`, from ``gui_toolbar.rebuild``)."""

from __future__ import annotations

import re
from typing import List, Optional

from PySide6 import QtCore, QtWidgets

UNDO, REDO = "undo", "redo"

#: How many steps a drop-down lists (the rest are summarised).
MAX_ITEMS = 25

_EDIT_BYTES = re.compile(r"^edit bytes @0x([0-9A-Fa-f]+)$")
_EDIT_REGION = re.compile(r"^edit (\w+) @0x([0-9A-Fa-f]+)$")


def _session(win):
    ws = getattr(win, "current", None)
    return getattr(ws, "primary", None) if ws is not None else None


def _entities(s):
    out = list(getattr(s, "entities", None) or [])
    if getattr(s, "platform", "") == "edc16":
        try:
            out += list(s.variant_entities())
        except Exception:
            pass
    return out


def _title(e) -> str:
    try:
        from .gui import _english
        t = _english(e)
    except Exception:
        t = ""
    return t or getattr(e, "title", "") or e.name


def describe(s, label: str) -> str:
    """A command's label as a menu item: the parameter's tree name instead
    of its symbol, the parameter an address is in, sentence case."""
    label = (label or "").strip() or "edit"
    m = _EDIT_BYTES.match(label)
    if m:
        addr = int(m.group(1), 16)
        for e in _entities(s):
            if e.addr <= addr < e.end:
                return "Edit bytes of %s (0x%X)" % (_title(e), addr)
            for ax, nm in ((e.x, "X axis"), (e.y, "Y axis")):
                if ax is not None and ax.addr is not None:
                    step = 4 if ax.is_float else max(1, ax.bits // 8)
                    if ax.addr <= addr < ax.addr + ax.count * step:
                        return "Edit %s of %s" % (nm, _title(e))
        return "Edit bytes at 0x%X" % addr
    m = _EDIT_REGION.match(label)
    if m and m.group(1) != "bytes":
        return "Edit %s at 0x%X" % (m.group(1).upper(), int(m.group(2), 16))
    if label.startswith("edit ") and " " not in label[5:]:
        name = label[5:]
        for e in _entities(s):
            if e.name == name:
                return "Edit %s" % _title(e)
    return label[:1].upper() + label[1:]


def labels(win, kind: str) -> List[str]:
    """The steps the ``kind`` button would take, next first."""
    s = _session(win)
    if s is None:
        return []
    st = s.undo_stack
    return st.undo_labels() if kind == UNDO else st.redo_labels()


def step(win, kind: str, n: int) -> None:
    """Undo (or redo) ``n`` steps on the active tab, as one refresh."""
    ws = getattr(win, "current", None)
    s = _session(win)
    if s is None or _view_only(ws):
        return
    (s.undo_steps if kind == UNDO else s.redo_steps)(n)
    ws.model.refresh()
    ws._status()
    sync(win)


def _view_only(ws) -> bool:
    try:
        from . import gui_viewonly as _vo
        return bool(_vo.is_view_only(ws))
    except Exception:
        return False


def tooltip(win, kind: str) -> str:
    s = _session(win)
    word = "Undo" if kind == UNDO else "Redo"
    keys = "Ctrl+Z" if kind == UNDO else "Ctrl+Y"
    labs = labels(win, kind)
    if s is None or not labs:
        return "%s  (%s)\nNothing to %s" % (word, keys, word.lower())
    return "%s: %s  (%s)\nThe arrow lists every step." % (
        word, describe(s, labs[0]), keys)


def sync(win) -> None:
    """Grey out Undo / Redo when there is nothing to undo or redo."""
    ws = getattr(win, "current", None)
    s = _session(win)
    for kind in (UNDO, REDO):
        a = win.__dict__.get("_ovt_%s_action" % kind)
        if a is None:
            continue
        ok = (s is not None and not _view_only(ws)
              and bool(s.can_undo if kind == UNDO else s.can_redo))
        if a.isEnabled() != ok:
            a.setEnabled(ok)


def register(win, undo_action, redo_action) -> None:
    """Remember the window's Undo / Redo actions."""
    win.__dict__["_ovt_undo_action"] = undo_action
    win.__dict__["_ovt_redo_action"] = redo_action


def kind_of(win, action) -> Optional[str]:
    for kind in (UNDO, REDO):
        if win.__dict__.get("_ovt_%s_action" % kind) is action:
            return kind
    return None


class _Tips(QtCore.QObject):
    """The button's tooltip, worked out when it is shown."""

    def __init__(self, win, kind, parent):
        super().__init__(parent)
        self.win, self.kind = win, kind

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.ToolTip:
            QtWidgets.QToolTip.showText(ev.globalPos(),
                                        tooltip(self.win, self.kind), obj)
            return True
        return False


def fill(win, kind: str, menu: QtWidgets.QMenu) -> None:
    menu.clear()
    s = _session(win)
    labs = labels(win, kind)
    if s is None or not labs:
        a = menu.addAction("Nothing to %s" % ("undo" if kind == UNDO else "redo"))
        a.setEnabled(False)
        return
    word = "Undo" if kind == UNDO else "Redo"
    for i, lab in enumerate(labs[:MAX_ITEMS]):
        text = describe(s, lab)
        if i:
            text = "%s  (%d steps)" % (text, i + 1)
        a = menu.addAction(text)
        a.setToolTip("%s %d step(s), down to this one" % (word, i + 1))
        a.triggered.connect(lambda _=False, n=i + 1: step(win, kind, n))
    if len(labs) > MAX_ITEMS:
        menu.addSeparator()
        a = menu.addAction("... %d more" % (len(labs) - MAX_ITEMS))
        a.setEnabled(False)


def decorate(win, tb: QtWidgets.QToolBar, action) -> None:
    """Give the toolbar button of ``action`` (Undo or Redo) its drop-down
    list. Run after every toolbar rebuild: the buttons are made anew."""
    kind = kind_of(win, action)
    btn = tb.widgetForAction(action)
    if kind is None or not isinstance(btn, QtWidgets.QToolButton):
        return
    menu = QtWidgets.QMenu(btn)
    menu.setObjectName("%s_history" % kind)
    menu.setToolTipsVisible(True)
    menu.aboutToShow.connect(lambda: fill(win, kind, menu))
    btn.setMenu(menu)
    btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    btn.installEventFilter(_Tips(win, kind, btn))
