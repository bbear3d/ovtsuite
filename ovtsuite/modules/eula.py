# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Has the user accepted each installed closed-source module's licence?

A closed module carries its agreement as ``<id>/EULA.txt`` (put there by the
build, see :mod:`.package`). It counts as accepted when either

* its folder holds ``<id>/EULA.accepted`` naming the agreement's SHA-256 --
  written by a Windows installer whose License page showed the agreement
  (a module's own installer, or the full / OVT Tools installer with its
  modules: packaging/modules_eula.iss); or
* this user's settings hold that SHA-256 under ``modules/eula/<id>`` --
  written when the user accepts it in the app (installing a ``.ovtmod`` from
  the Module Manager, or the first-start prompt).

Anything else -- a Linux package, a module copied into place -- is
*pending*, and the GUI asks
before the app or a tool starts (``gui_modules.confirm_licences``). A module
whose agreement is declined is hidden for that run, exactly as if it were
not installed, and asked about again on the next start. Keyed by the
agreement's hash, so a changed agreement is asked again.

Only installed (mounted) modules are checked: a source checkout's own
modules are the developer's.
"""

import hashlib
import os
from typing import Dict, List, NamedTuple, Optional

from . import api as _api
from . import loader as _loader

#: written beside EULA.txt by an installer that showed the agreement
MARKER = "EULA.accepted"
_KEY = "modules/eula/%s"

#: module id -> why it is hidden this run (the agreement was declined)
_declined: Dict[str, str] = {}


class Pending(NamedTuple):
    id: str
    name: str
    text: str
    digest: str


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _settings():
    from ..appsettings import settings
    return settings()


def _eula_bytes(root: str, mid: str) -> Optional[bytes]:
    try:
        with open(os.path.join(root, *_api.eula_path(mid).split("/")), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def is_accepted(mid: str, root: str, sha: str) -> bool:
    try:
        with open(os.path.join(root, mid, MARKER), encoding="utf-8") as fh:
            if fh.read().strip().lower() == sha:
                return True
    except OSError:
        pass
    try:
        return str(_settings().value(_KEY % mid, "") or "").lower() == sha
    except Exception:                # noqa: BLE001 - no Qt: nothing recorded
        return False


def accept(mid: str, sha: str) -> None:
    """Record that this user accepted ``mid``'s agreement (``sha``)."""
    s = _settings()
    s.setValue(_KEY % mid, sha)
    s.sync()
    _declined.pop(mid, None)


def pending() -> List[Pending]:
    """Every mounted installed module whose agreement is not accepted yet."""
    out = []
    roots = _loader.module_roots()
    for mid, rec in sorted(_loader.installed_records().items()):
        root = roots.get(mid)
        if root is None or mid in _loader.disabled():
            continue
        data = _eula_bytes(root, mid)
        if data is None:
            continue                 # open source: nothing to accept
        sha = digest(data)
        if not is_accepted(mid, root, sha):
            out.append(Pending(mid, rec.manifest.name,
                               data.decode("utf-8", "replace"), sha))
    return out


def decline(mid: str) -> None:
    """Hide ``mid`` for the rest of this run."""
    _declined[mid] = "licence agreement not accepted"
    _loader.set_disabled(set(_loader.disabled()) | {mid})


def declined() -> Dict[str, str]:
    return dict(_declined)
