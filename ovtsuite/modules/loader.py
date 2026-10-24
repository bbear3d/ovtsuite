# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Finding modules, and hiding the ones that must not load.

Two kinds of module:

* **in-tree** -- the package is part of the app (a source checkout, or a
  build that includes it);
* **installed** -- extracted from a ``.ovtmod`` into a modules folder (see
  :mod:`.installer`). Each checked one is *mounted*: its directory, laid out
  like the app's package directory, is appended to the app package's
  ``__path__``, so ``import <app>.<id>`` finds it with no custom importer
  and its own code finds its data beside it. A module that fails a check
  (signature, API level, damaged file, licence) is not mounted; why is kept
  for the Module Manager.

``OVT_DISABLE_MODULES`` (comma-separated module ids, or ``all``) hides
modules exactly as if they were not installed: importing ``<app>.<id>`` or
anything under it raises ``ModuleNotFoundError``. That is how the base app is
tested without its modules, and how a developer reproduces a user's
base-only install.
"""

import importlib.abc
import os
import sys
from typing import Dict, FrozenSet

ENV = "OVT_DISABLE_MODULES"

_disabled: FrozenSet[str] = frozenset()
#: Data globs of hidden modules (read before they were hidden), so the data
#: resolver can hide their files too.
_hidden_data: Dict[str, tuple] = {}
#: module id -> install root (a directory laid out like the app's package
#: directory) for modules installed outside the app's own tree. Empty while
#: every module is in-tree; the .ovtmod loader fills it.
_roots: Dict[str, str] = {}


def _pkg() -> str:
    from ..branding import PKG
    return PKG


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if len(parts) >= 2 and parts[0] == _pkg() and parts[1] in _disabled:
            raise ModuleNotFoundError(
                "module %r is not installed" % parts[1], name=fullname)
        return None


_BLOCKER = _Blocker()


def _parse(value: str) -> FrozenSet[str]:
    from ..platforms import MODULES
    ids = {v.strip().lower() for v in (value or "").split(",") if v.strip()}
    if "all" in ids:
        return frozenset(MODULES)
    return frozenset(ids & set(MODULES))


def disabled() -> FrozenSet[str]:
    return _disabled


def hidden_data() -> Dict[str, tuple]:
    return dict(_hidden_data)


def module_roots() -> Dict[str, str]:
    return dict(_roots)


#: module id -> its installer.Installed record (mounted or not)
_installed: Dict[str, object] = {}
#: module id -> why its installed copy is not used
_problems: Dict[str, str] = {}


def installed_records() -> Dict[str, object]:
    return dict(_installed)


def problems() -> Dict[str, str]:
    return dict(_problems)


def manifest(mid: str):
    """The manifest of the mounted installed copy of ``mid`` (None for an
    in-tree module or one not installed)."""
    rec = _installed.get(mid)
    return rec.manifest if rec is not None and mid in _roots else None


def in_tree(mid: str) -> bool:
    import os
    pkg = sys.modules[_pkg()]
    for base in list(getattr(pkg, "__path__", []))[:1]:
        if os.path.isfile(os.path.join(base, mid, "__init__.py")):
            return True
    return False


def mount(rec) -> bool:
    """Mount one checked installed module; False if it cannot be."""
    mid = rec.manifest.id
    if rec.problem:
        _problems[mid] = rec.problem
        return False
    if in_tree(mid):
        _problems.pop(mid, None)
        _installed[mid] = rec
        return False                 # the app's own copy wins
    pkg = sys.modules[_pkg()]
    if rec.root not in pkg.__path__:
        pkg.__path__.append(rec.root)
    _roots[mid] = rec.root
    _installed[mid] = rec
    _problems.pop(mid, None)
    return True


def mount_installed(verify_files: bool = True) -> None:
    """Find every installed module and mount the best copy of each."""
    from . import installer
    found = installer.discover(verify_files)
    for mid, cands in found.items():
        if mid in _roots:
            continue                 # already mounted (a restart replaces it)
        good = [c for c in cands if not c.problem]
        if good:
            if mount(good[0]) and good[0].manifest.platform:
                # the compiled copy is in use: a source copy an update left
                # behind (another modules folder, an older install) goes
                try:
                    installer.scrub_source(mid, found)
                except Exception:    # noqa: BLE001 - never stops the start
                    pass
        else:
            _installed[mid] = cands[0]
            _problems[mid] = cands[0].problem
    from . import registry
    registry.reset()


def set_disabled(ids) -> None:
    """Hide exactly ``ids`` (module ids). Already-imported module code is
    evicted from ``sys.modules`` so the next import really fails."""
    global _disabled
    import importlib
    for mid in ids:
        if mid not in _hidden_data:
            try:                     # remember what it owns before hiding it
                _hidden_data[mid] = importlib.import_module(
                    "%s.%s.module" % (_pkg(), mid)).MODULE.data
            except Exception:
                _hidden_data[mid] = ()
    _disabled = frozenset(ids)
    if _disabled and _BLOCKER not in sys.meta_path:
        sys.meta_path.insert(0, _BLOCKER)
    for name in list(sys.modules):
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == _pkg() and parts[1] in _disabled:
            del sys.modules[name]
            parent = sys.modules.get(_pkg())
            if len(parts) == 2 and parent is not None and hasattr(parent, parts[1]):
                delattr(parent, parts[1])
    from . import registry
    registry.reset()


def install() -> None:
    """Called once when the app package imports: mount installed modules,
    then apply ``OVT_DISABLE_MODULES``. Never raises -- a broken modules
    folder must not stop the app (or the flasher) from starting."""
    try:
        mount_installed()
    except Exception as ex:          # noqa: BLE001
        _problems["*"] = "module discovery failed: %s" % ex
    ids = _parse(os.environ.get(ENV, ""))
    if ids:
        set_disabled(ids)
