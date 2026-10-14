# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The app's global options: one declaration each, read anywhere.

Every option the Options dialog (:mod:`ovtsuite.gui_options`) edits is an
:class:`Option` here -- its settings key, type, default, label, page and help
text -- so the default lives in one place and code reads ``options.get(key)``
instead of a constant. Keys that existed before the dialog keep their names,
so nobody's saved values are lost.

A change made through :func:`set` is announced by :func:`notifier`'s
``changed(key)`` signal; open windows, timers and the toolbar listen to it
and apply it at once (an option marked ``restart`` takes effect at the next
start).

Besides the plain options, two families of keys are kept here too:
``toolbar/actions`` (the toolbar's buttons, a list of action ids) and
``shortcuts/<action id>`` (keyboard shortcuts changed from their defaults).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: the toolbar's buttons: action ids, "|" for a separator (gui_toolbar)
TOOLBAR_KEY = "toolbar/actions"
#: prefix of a changed keyboard shortcut: ``shortcuts/<action id>``
SHORTCUT_PREFIX = "shortcuts/"


@dataclass(frozen=True)
class Option:
    key: str
    default: Any
    label: str
    page: str
    #: "bool", "int" or "choice"
    kind: str = "bool"
    help: str = ""
    minimum: int = 0
    maximum: int = 0
    suffix: str = ""
    #: ((value, label), ...) for a "choice"
    choices: Tuple[Tuple[str, str], ...] = ()
    #: takes effect at the next start only
    restart: bool = False


GENERAL = "General"
PROJECTS = "Projects and saving"
EDITOR = "Editor"
APPEARANCE = "Appearance"
TOOLBAR = "Toolbar"
MODULES = "Modules"

OPTIONS: Tuple[Option, ...] = (
    # -- General -----------------------------------------------------------
    Option("startup/restore", "ask", "At start-up, last session's projects",
           GENERAL, "choice",
           "What happens to the projects that were open when the app last "
           "closed.",
           choices=(("ask", "Ask which to reopen"), ("always", "Reopen them all"),
                    ("never", "Start empty"))),
    Option("window/remember", True, "Remember the window's size and position",
           GENERAL, help="Off: the window always opens maximized."),
    Option("ui/file_info_on_open", True, "Show file information when a file "
           "opens", GENERAL),
    Option("projects/ask_info", True, "Ask for the vehicle details of a new "
           "project", GENERAL),
    Option("recent/max", 12, "Recent files listed", GENERAL, "int",
           minimum=1, maximum=50),
    Option("tabs/closed_max", 15, "Closed projects kept in File > Restore "
           "project from history", GENERAL, "int", minimum=1, maximum=100),
    Option("updates/check_at_startup", True, "Check for updates at start-up",
           GENERAL, help="At most once a day, a few seconds after the app "
           "starts. Help > Check for Updates checks at any time."),
    # -- Projects and saving -------------------------------------------------
    Option("projects/autosave_s", 60, "Keep unsaved work for crash recovery "
           "every", PROJECTS, "int",
           "How often unsaved changes are put aside, so they can be recovered "
           "if the app does not close properly.",
           minimum=10, maximum=3600, suffix=" s"),
    Option("projects/watch_disk", True, "Notice when another program changes "
           "an open project's file", PROJECTS,
           help="Off: changes are still found before saving (nothing is ever "
           "written over them without asking), just not while you work."),
    Option("projects/auto_reload", True, "Reload a project with nothing "
           "unsaved when its file changes", PROJECTS,
           help="Off: ask first."),
    Option("projects/watch_s", 2, "Look at the open projects' files every",
           PROJECTS, "int", minimum=1, maximum=60, suffix=" s"),
    # -- Editor --------------------------------------------------------------
    Option("history/version_menu_max", 25, "Versions listed in Compare with "
           "saved version", EDITOR, "int", minimum=5, maximum=200),
    # -- Appearance ----------------------------------------------------------
    Option("ui/theme", "system", "Theme", APPEARANCE, "choice",
           "System follows Windows' light or dark app mode.",
           choices=(("system", "System"), ("light", "Light"), ("dark", "Dark"))),
    Option("ui/menu_icons", True, "Show icons in menus", APPEARANCE),
    Option("ui/scrollbars", "auto", "Scroll bars", APPEARANCE, "choice",
           "Windows 11 hides scroll bars until the mouse is over them unless "
           "its \"Always show scrollbars\" setting is on.",
           choices=(("auto", "As Windows is set"), ("always", "Always show")),
           restart=True),
    # -- Toolbar (the buttons themselves: the page's own editor) ---------------
    Option("toolbar/visible", True, "Show the toolbar", TOOLBAR),
    Option("toolbar/icon_size", "medium", "Icon size", TOOLBAR, "choice",
           choices=(("small", "Small"), ("medium", "Medium"), ("large", "Large"))),
    Option("toolbar/style", "icons", "Buttons show", TOOLBAR, "choice",
           choices=(("icons", "Icons only"), ("beside", "Text beside the icon"),
                    ("under", "Text under the icon"))),
    # -- Modules -------------------------------------------------------------
    Option("modules/allow_unsigned", False, "Allow unsigned modules "
           "(development)", MODULES,
           help="Also in Tools > Modules. Only for modules you build "
           "yourself: an unsigned module is not checked."),
)

BY_KEY: Dict[str, Option] = {o.key: o for o in OPTIONS}
PAGES: List[str] = list(dict.fromkeys(o.page for o in OPTIONS))

#: the toolbar's buttons until the user changes them
DEFAULT_TOOLBAR = [
    "file/new-project", "file/open-project", "file/open-recent", "file/save",
    "|", "edit/undo", "edit/redo", "|", "edit/find-panel",
    "edit/show-differences",
    "edit/version-history", "|", "tools/ecu-read-write", "tools/data-logging",
    "|", "view/project-panel", "tools/ai/assistant", "tools/options",
]


# ------------------------------------------------------------- storage
def _store():
    """Where options are kept (the app's QSettings; tests swap it)."""
    from .appsettings import settings
    return settings()


def _convert(opt: Option, raw):
    if raw is None:
        return opt.default
    try:
        if opt.kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if opt.kind == "int":
            v = int(float(raw))
            if opt.maximum:
                v = max(opt.minimum, min(opt.maximum, v))
            return v
        if opt.kind == "choice":
            v = str(raw)
            return v if v in {c for c, _l in opt.choices} else opt.default
    except (TypeError, ValueError):
        return opt.default
    return raw


def get(key: str):
    """The option's value (its default when unset or unreadable)."""
    opt = BY_KEY[key]
    try:
        return _convert(opt, _store().value(key))
    except Exception:
        return opt.default


def set(key: str, value) -> None:            # noqa: A001 (module API)
    """Change an option and announce it (nothing when it is unchanged)."""
    opt = BY_KEY[key]
    value = _convert(opt, value)
    if value == get(key):
        return
    s = _store()
    if value == opt.default:
        s.remove(key)
    else:
        s.setValue(key, value)
    s.sync()
    _announce(key)


def reset(keys=None) -> None:
    """Put options (all of them, the toolbar and the shortcuts too, by
    default) back to their defaults."""
    s = _store()
    if keys is None:
        keys = [o.key for o in OPTIONS] + [TOOLBAR_KEY] + [
            k for k in s.allKeys() if k.startswith(SHORTCUT_PREFIX)]
    changed = [k for k in keys if s.contains(k)]
    for k in changed:
        s.remove(k)
    s.sync()
    for k in changed:
        _announce(k)


# --------------------------------------------------------- the toolbar
def toolbar() -> List[str]:
    """The toolbar's action ids ("|" = separator)."""
    try:
        raw = _store().value(TOOLBAR_KEY)
        items = json.loads(raw) if raw else None
    except Exception:
        items = None
    if not isinstance(items, list):
        return list(DEFAULT_TOOLBAR)
    return _migrate_toolbar([str(x) for x in items])


#: the key naming the toolbar changes already made to a saved toolbar
TOOLBAR_MIGRATED_KEY = "toolbar/migrated"
#: ``(name, old id, new id)``: a saved toolbar's button that became another
#: one -- done once, so the old button can be put back afterwards
_TOOLBAR_RENAMES = (
    # the Find button shows / hides the panel (Edit > Find still opens it)
    ("find-panel", "edit/find", "edit/find-panel"),
)


def _migrate_toolbar(items: List[str]) -> List[str]:
    s = _store()
    try:
        raw = str(s.value(TOOLBAR_MIGRATED_KEY) or "")
    except Exception:
        return items
    done = {x for x in raw.split(",") if x}     # (``set`` is ours, here)
    todo = [r for r in _TOOLBAR_RENAMES if r[0] not in done]
    if not todo:
        return items
    for name, old, new in todo:
        items = [new if x == old and new not in items else x for x in items]
        done.add(name)
    s.setValue(TOOLBAR_MIGRATED_KEY, ",".join(sorted(done)))
    s.setValue(TOOLBAR_KEY, json.dumps(items))
    s.sync()
    return items


def set_toolbar(ids: List[str]) -> None:
    ids = [str(x) for x in ids]
    if ids == toolbar():
        return
    s = _store()
    # (chosen now: no older toolbar's buttons to change)
    s.setValue(TOOLBAR_MIGRATED_KEY,
               ",".join(sorted(r[0] for r in _TOOLBAR_RENAMES)))
    if ids == DEFAULT_TOOLBAR:
        s.remove(TOOLBAR_KEY)
    else:
        s.setValue(TOOLBAR_KEY, json.dumps(ids))
    s.sync()
    _announce(TOOLBAR_KEY)


# ---------------------------------------------------------- shortcuts
def shortcuts() -> Dict[str, str]:
    """Shortcuts changed from their defaults: ``{action id: key sequence}``
    ("" = no shortcut)."""
    s = _store()
    out = {}
    for k in s.allKeys():
        if k.startswith(SHORTCUT_PREFIX):
            out[k[len(SHORTCUT_PREFIX):]] = str(s.value(k) or "")
    return out


def set_shortcuts(changed: Dict[str, Optional[str]]) -> None:
    """``{action id: sequence}``; None puts one back to its default."""
    s = _store()
    for aid, seq in changed.items():
        if seq is None:
            s.remove(SHORTCUT_PREFIX + aid)
        else:
            s.setValue(SHORTCUT_PREFIX + aid, str(seq))
    s.sync()
    if changed:
        _announce(SHORTCUT_PREFIX)


# --------------------------------------------------------- export, import
def export_to(path: str) -> None:
    """Every option, the toolbar and the shortcuts as a JSON file."""
    data = {"options": {o.key: get(o.key) for o in OPTIONS},
            "toolbar": toolbar(), "shortcuts": shortcuts()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


def import_from(path: str) -> None:
    """Options from a file written by :func:`export_to` (unknown keys are
    ignored)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("not an options file")
    for k, v in (data.get("options") or {}).items():
        if k in BY_KEY:
            set(k, v)
    if isinstance(data.get("toolbar"), list):
        set_toolbar(data["toolbar"])
    if isinstance(data.get("shortcuts"), dict):
        cur = shortcuts()
        want = {str(k): str(v) for k, v in data["shortcuts"].items()}
        changes: Dict[str, Optional[str]] = {k: None for k in cur if k not in want}
        changes.update(want)
        set_shortcuts(changes)


# --------------------------------------------------------- announcing
_NOTIFIER = None


def notifier():
    """A QObject whose ``changed(str)`` signal carries every changed key."""
    global _NOTIFIER
    if _NOTIFIER is None:
        from PySide6 import QtCore

        class _Notifier(QtCore.QObject):
            changed = QtCore.Signal(str)
        _NOTIFIER = _Notifier()
    return _NOTIFIER


def _announce(key: str) -> None:
    try:
        notifier().changed.emit(key)
    except Exception:
        pass
