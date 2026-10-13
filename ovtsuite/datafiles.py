# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where the app's data files are, now that modules bring their own.

Every data file belongs to the base app or to exactly one module (a module
declares globs in its ``ModuleSpec.data``). The base app's files live in the
package's ``data`` directory; an installed module's live in its own copy of
that directory under the module's install folder, so a module's own code
finds them next to itself exactly as in the source tree.

Base code that reads a module's data -- the KB engine reading EDC16 family
files, the search label cache, the stored-definition digests -- goes through
here, so it finds the file wherever the owning module is installed and never
sees the data of a module that is hidden or absent.
"""

import fnmatch
import os
from typing import Dict, List, Optional, Tuple

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _specs() -> Dict[str, tuple]:
    """module id -> its data globs, for every module whose declaration is
    readable (installed, or hidden by the loader)."""
    from .modules import loader, registry
    out = {mid: s.data for mid, s in registry.installed().items()}
    out.update(loader.hidden_data())
    return out


def owner(name: str) -> str:
    """The module that owns data file ``name`` ("" = the base app)."""
    for mid, globs in _specs().items():
        if any(fnmatch.fnmatchcase(name, g) for g in globs):
            return mid
    return ""


def _visible(name: str) -> bool:
    from .modules import loader
    return owner(name) not in loader.disabled()


def dirs() -> List[str]:
    """Every data directory, base first, then each installed module's."""
    from .modules import loader
    out = [BASE_DIR]
    for root in loader.module_roots().values():
        d = os.path.join(root, "data")
        if os.path.isdir(d):
            out.append(d)
    return out


def path(name: str) -> Optional[str]:
    """Absolute path of data file ``name``, or None when no installed part of
    the app provides it."""
    if not _visible(name):
        return None
    for d in dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def exists(name: str) -> bool:
    return path(name) is not None


def listing(module_id: Optional[str] = None) -> List[Tuple[str, str]]:
    """``[(name, abspath)]`` of every visible data file -- only those owned
    by ``module_id`` ("" = the base app's) when given."""
    seen = {}
    for d in dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if n in seen or not os.path.isfile(p) or not _visible(n):
                continue
            if module_id is not None and owner(n) != module_id:
                continue
            seen[n] = p
    return sorted(seen.items())
