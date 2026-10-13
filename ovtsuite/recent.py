# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Most-recently-opened binary tracking, persisted across runs.

Tiny JSON lists under the user's config directory (``%APPDATA%\\OVT-Suite``
on Windows, ``~/.config/ovtsuite`` elsewhere). Two independent lists are
kept: primaries (``recent.json``) and comparison binaries
(``recent_comparisons.json``). Each is most-recent first, de-duped by absolute
path, capped, and pruned of files that no longer exist on load.
"""

import json
import os

from . import branding

MAX_RECENT = 12


def _base_dir() -> str:
    if os.name == "nt":
        return os.environ.get("APPDATA") or os.path.expanduser("~")
    return (os.environ.get("XDG_CONFIG_HOME")
            or os.path.join(os.path.expanduser("~"), ".config"))


def _config_dir() -> str:
    name = branding.CONFIG_DIR if os.name == "nt" else branding.PKG
    return os.path.join(_base_dir(), name)


def _legacy_dirs():
    """Where a previous name kept its lists. Read-only: never written to."""
    return [os.path.join(_base_dir(), n) for n in branding.LEGACY_CONFIG_DIRS]


def _existing(filename: str) -> str:
    """The current path if it holds a file, else the first legacy path that
    does. Renaming the suite should not silently empty the recent-files menu."""
    cur = os.path.join(_config_dir(), filename)
    if os.path.exists(cur):
        return cur
    for d in _legacy_dirs():
        old = os.path.join(d, filename)
        if os.path.exists(old):
            return old
    return cur


def _recent_path() -> str:
    """Where the primary list is WRITTEN: always the current config dir."""
    return os.path.join(_config_dir(), "recent.json")


def _comparison_path() -> str:
    return os.path.join(_config_dir(), "recent_comparisons.json")


def _load(path: str, prune: bool = True):
    """Read a list, falling back to a previous name's copy when this one has
    none yet. The fallback is read-only; the next write lands in the current
    directory, so the list migrates the first time it changes."""
    if not os.path.exists(path):
        path = _existing(os.path.basename(path))
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh)
    except Exception:
        return []
    out = []
    seen = set()
    for p in items:
        if not isinstance(p, str):
            continue
        # a version inside a project: "<x.ovt>#ovt:<set>/<branch>@<version>"
        real, sep, rest = p.partition("#ovt:")
        ap = os.path.abspath(real) + sep + rest
        if ap in seen:
            continue
        if prune and not os.path.exists(os.path.abspath(real)):
            continue
        seen.add(ap)
        out.append(ap)
    return out[:_max()]


def _max() -> int:
    """How many recent files are kept (Options > General)."""
    try:
        from . import options
        return options.get("recent/max")
    except Exception:                   # no Qt (command line): the default
        return MAX_RECENT


def _save_list(path: str, items):
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=0)
    except Exception:
        pass                            # recent list is best-effort


def _norm(path: str) -> str:
    real, sep, rest = str(path).partition("#ovt:")
    return os.path.abspath(real) + sep + rest


def _add(path_fn, path: str):
    if not path:
        return
    ap = _norm(path)
    items = [ap] + [p for p in _load(path_fn(), prune=False) if p != ap]
    _save_list(path_fn(), items[:_max()])


def _remove(path_fn, path: str):
    ap = _norm(path)
    _save_list(path_fn(), [p for p in _load(path_fn(), prune=False) if p != ap])


# -- primary binaries -------------------------------------------------------
def load_recent(prune: bool = True):
    """The recent-primaries list (most-recent first). With ``prune``, drops
    paths that no longer exist."""
    return _load(_recent_path(), prune)


def load_recent_projects(prune: bool = True):
    """File > Open Recent: the recent list's projects (``.ovt``) only."""
    return [p for p in load_recent(prune) if p.lower().endswith(".ovt")]


def add_recent(path: str):
    """Record ``path`` as the most-recently-opened primary binary."""
    _add(_recent_path, path)


def remove_recent(path: str):
    _remove(_recent_path, path)


def clear_recent():
    _save_list(_recent_path(), [])


# -- comparison binaries ----------------------------------------------------
def load_recent_comparisons(prune: bool = True):
    """The recent-comparisons list (most-recent first)."""
    return _load(_comparison_path(), prune)


def add_recent_comparison(path: str):
    """Record ``path`` as the most-recently-opened comparison binary."""
    _add(_comparison_path, path)


def remove_recent_comparison(path: str):
    _remove(_comparison_path, path)


def clear_recent_comparisons():
    _save_list(_comparison_path(), [])


# -- settings (last-used directory) -----------------------------------------
def _settings_path() -> str:
    """Where settings are WRITTEN: always the current config dir."""
    return os.path.join(_config_dir(), "settings.json")


def get_setting(key, default=None):
    try:
        with open(_existing("settings.json"), encoding="utf-8") as fh:
            return json.load(fh).get(key, default)
    except Exception:
        return default


def set_setting(key, value):
    try:
        try:
            with open(_existing("settings.json"), encoding="utf-8") as fh:
                d = json.load(fh)
                if not isinstance(d, dict):
                    d = {}
        except Exception:
            d = {}
        d[key] = value
        os.makedirs(_config_dir(), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=0)
    except Exception:
        pass                            # settings are best-effort


#: The kinds of file a picker remembers a folder for. One shared folder sent
#: File > Open to wherever the last log, capture or export happened to be
#: written -- starting a data log moved it every time -- so each kind now keeps
#: its own, and opening a binary only ever follows binaries.
DIR_KINDS = ("binary", "log", "export", "eeprom", "patch")


def _dir_key(kind: str) -> str:
    return "last_dir_" + (kind if kind in DIR_KINDS else "binary")


def _usable(d) -> str:
    return d if isinstance(d, str) and d and os.path.isdir(d) else ""


def get_last_dir(kind: str = "binary") -> str:
    """The folder a ``kind`` file picker should open in.

    ``binary`` (primary, comparison, Save As, backups, module reads): the
    folder of the last binary opened or saved; before any, the folder of the
    newest file in the recent list. Other kinds start there too until one of
    their own has been picked. With nothing remembered at all (a fresh
    install), the user's home folder -- never ``""``: callers join a default
    file name onto this, and a bare name lands in the process's working
    directory, which for an installed app can be its own read-only install
    folder (``/opt/...``, or ``Program Files`` from a shortcut). The data-log
    and capture panes write straight to that pre-filled path."""
    d = _usable(get_setting(_dir_key(kind), ""))
    if d:
        return d
    if kind != "binary":
        return get_last_dir("binary")
    for p in load_recent():
        d = _usable(os.path.dirname(p))
        if d:
            return d
    return (_usable(get_setting("last_dir", ""))    # the old single folder
            or _usable(os.path.expanduser("~")))


def set_last_dir(path: str, kind: str = "binary"):
    """Remember the folder of ``path`` (a file or a folder) for ``kind``."""
    if path:
        d = path if os.path.isdir(path) else os.path.dirname(path)
        if d:
            set_setting(_dir_key(kind), d)
