# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The installed modules' own items in the Tools menu.

A module declares them with its ``menu_tools`` hook
(:class:`ovtsuite.modules.api.MenuTool`): the text, the section of the menu
it belongs in, an icon and the tabs it applies to. :func:`add` puts a
section's items where ``MainWindow._make_actions`` calls it, as ordinary
tab tools -- disabled, and hidden while the menu is open
(:mod:`ovtsuite.gui_toolsmenu`), on a tab they do not apply to. There are no
per-module sub-menus.
"""

from __future__ import annotations

from typing import List, Tuple

from .modules import api as _api
from .modules import registry as _registry

#: the sections the menu has, in order; an item naming another goes last
SECTIONS = (_api.TOOLS_EDITORS, _api.TOOLS_SETTINGS, _api.TOOLS_FILES)


def tools() -> List[Tuple[object, object]]:
    """``[(spec, MenuTool)]`` of every installed module, in module order."""
    out = []
    for spec in _registry.installed().values():
        if not getattr(spec, "menu_tools", None):
            continue
        try:
            items = spec.call("menu_tools") or ()
        except Exception:                   # a broken module adds nothing
            continue
        for t in items:
            if getattr(t, "text", "") and callable(getattr(t, "run", None)):
                out.append((spec, t))
    return out


def logtune_for(ws):
    """The first module log analysis (``logtune`` hook) that applies to tab
    ``ws``, or None."""
    if ws is None or getattr(ws, "primary", None) is None:
        return None
    for spec in _registry.installed().values():
        if not getattr(spec, "logtune", None):
            continue
        try:
            items = spec.call("logtune") or ()
        except Exception:                   # a broken module adds nothing
            continue
        for t in items:
            try:
                if not callable(getattr(t, "run", None)):
                    continue
                if t.applies is not None:
                    ok = bool(t.applies(ws))
                else:                       # as applies(): the module's own platforms
                    p = ws.primary
                    ok = getattr(p, "platform", "") in spec.platforms and \
                        getattr(p, "support", "full") == "full"
                if ok:
                    return t
            except Exception:               # noqa: BLE001
                continue
    return None


def applies(win, spec, tool) -> bool:
    """Is ``tool`` for the tab in front of ``win`` (which has a file)?"""
    ws = win.current
    if ws is None or getattr(ws, "primary", None) is None:
        return False
    if tool.applies is not None:
        return bool(tool.applies(ws))
    p = ws.primary
    return getattr(p, "platform", "") in spec.platforms and \
        getattr(p, "support", "full") == "full"


def add(win, menu, section: str) -> list:
    """Add the modules' items of ``section`` to ``menu``; the actions."""
    from . import icons as _icons
    acts = []
    for spec, t in tools():
        sec = t.section if t.section in SECTIONS else SECTIONS[-1]
        if sec != section:
            continue
        a = win._act(menu, t.text,
                     lambda _c=False, t=t: t.run(win), needs_file=True,
                     enabled_when=lambda spec=spec, t=t: applies(win, spec, t))
        if t.tooltip:
            a.setToolTip(t.tooltip)
        if t.icon:
            a.setIcon(_icons.icon(t.icon))
        a.setData(spec.id)                  # whose item it is
        acts.append(a)
    win.__dict__.setdefault("module_tool_actions", []).extend(acts)
    return acts


__all__ = ["SECTIONS", "add", "applies", "logtune_for", "tools"]
