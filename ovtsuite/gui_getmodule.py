# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Installing a module straight from the app's GitHub releases.

The releases carry every module as a ``.ovtmod`` package (an open-source
module's runs anywhere; a closed one's is compiled, one per interpreter and
platform, and only the one built for this app is offered), so a module can
be fetched and installed without its setup program:

* **Tools > Modules...** shows, for each module, the newest version published
  on GitHub, and **Install from GitHub** downloads and installs it;
* opening a file whose module is not installed offers to do the same, when
  the module is published (the *Limited support* box).

A download is checked against the release's signed checksum list (asking
first if a release has none), then installed exactly as **Install from
file...** installs a package: its signature and every file are checked, it is
confirmed, a closed module's licence agreement is shown, and it goes to the
per-user modules folder (no administrator rights). When the newest package is
refused -- built for a later version of the app -- the next older one is
tried.

What GitHub publishes is looked up once and kept for the session
(:data:`CACHE_SECONDS`), so opening several unsupported files asks GitHub
once. It is looked up by itself only when the app may check for updates by
itself (Tools > Options, and not ``OVTSUITE_NO_UPDATE_CHECK``); otherwise
only when asked (Check GitHub, or the offer's button).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from . import updates as _up

#: How long a lookup of the published modules is reused.
CACHE_SECONDS = 3600
#: The lookup when a file opens waits at most this long (seconds).
OPEN_TIMEOUT = 6.0
#: Older packages tried after the newest is refused.
FALLBACKS = 2

_cache: Dict[str, object] = {}


def may_look_up() -> bool:
    """Whether GitHub may be asked without the user asking: the same
    switches as the update check at startup."""
    import os
    from . import gui_update as _gu
    from . import options
    if os.environ.get(_gu.ENV_OFF, "") not in ("", "0"):
        return False
    try:
        return bool(options.get(_up.OPTION_KEY))
    except Exception:                          # noqa: BLE001
        return False


def _fetch(timeout: float = 15.0) -> Dict[Tuple[str, str], List[_up.AvailableModule]]:
    """The modules published on GitHub (the most recent 100 releases),
    against what is installed. Raises :class:`updates.UpdateError`."""
    rels = _up.fetch_releases(timeout=timeout, max_pages=1)
    return _up.available_modules(rels, _up.installed_modules())


def published(timeout: float = 15.0, fresh: bool = False):
    """``(available, error)``: the cached lookup, made now when there is
    none (or it is old, or ``fresh``). ``error`` is "" when it worked."""
    now = time.time()
    if not fresh and _cache.get("at") and now - float(_cache["at"]) < CACHE_SECONDS:
        return _cache.get("avail") or {}, str(_cache.get("error") or "")
    try:
        avail, err = _fetch(timeout), ""
    except _up.UpdateError as ex:
        avail, err = {}, str(ex)
    except Exception as ex:                    # noqa: BLE001 - offline, proxy ...
        avail, err = {}, "GitHub could not be reached (%s)" % ex
    if err and _cache.get("avail"):
        return _cache["avail"], err             # keep the last good answer
    _cache.update(at=now, avail=avail, error=err)
    return avail, err


def forget() -> None:
    """Drop the cached lookup (after an install, the installed versions
    changed)."""
    _cache.clear()


def published_async(parent, done: Callable[[dict, str], None],
                    fresh: bool = False) -> None:
    """:func:`published` on a worker thread; ``done(available, error)`` on
    the GUI thread."""
    from .gui_update import _Relay, _keep
    relay = _Relay()
    _keep.append(relay)

    def finished(result, error):
        try:
            _keep.remove(relay)
        except ValueError:
            pass
        done(result or {}, error or "")

    relay.finished.connect(finished, QtCore.Qt.ConnectionType.QueuedConnection)

    def work():
        avail, err = published(fresh=fresh)
        relay.finished.emit(avail, err)
    threading.Thread(target=work, name="module-lookup", daemon=True).start()


def _installed_variant(mid: str) -> str:
    for m in _up.installed_modules():
        if m.id == mid:
            return m.variant
    return "full"


def best(avail, mid: str) -> Optional[_up.AvailableModule]:
    """The package Install from GitHub would fetch first for ``mid``."""
    got = _up.module_choices(avail, mid, _installed_variant(mid))
    return got[0] if got else None


class _Rel:
    """What gui_update._download needs of an offer: its release."""

    def __init__(self, release):
        self.release = release


def install(parent, mid: str, avail=None) -> Optional[str]:
    """Download module ``mid`` from GitHub and install it (with the usual
    confirmation and licence agreement). Returns the module id, or None
    (not published, cancelled, refused -- said so)."""
    from . import gui_update as _gu
    from .gui_modules import install_from_file
    from .modules import installer as _installer
    from .modules import package as _package
    if avail is None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
        try:
            avail, err = published()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
    else:
        err = ""
    choices = _up.module_choices(avail, mid, _installed_variant(mid))
    from . import platforms as _platforms
    name = getattr(_platforms.MODULES.get(mid), "name", mid)
    if not choices:
        QtWidgets.QMessageBox.information(
            parent, "Install from GitHub",
            "The %s module is not published on GitHub%s."
            % (name, " (%s)" % err.rstrip(".") if err else ""))
        return None
    why = ""
    for cand in choices[:1 + FALLBACKS]:
        got = _gu._download(parent, _Rel(cand.release), [cand.package],
                             quiet=True, what="module")
        if got is None:
            if _gu.last_error and _gu.last_error != "cancelled":
                QtWidgets.QMessageBox.warning(
                    parent, "Install from GitHub",
                    "The %s module was not downloaded: %s."
                    % (name, _gu.last_error.rstrip(".")))
            return None
        path = got[0][1]
        try:
            _installer.check(path)
        except (_installer.InstallError, _package.PackageError) as ex:
            why = str(ex)
            continue                        # built for another app: an older one
        done = install_from_file(parent, path)
        if done is not None:
            forget()
        return done
    QtWidgets.QMessageBox.warning(
        parent, "Install from GitHub",
        "No published version of the %s module can be installed in this "
        "version of the app: %s" % (name, why))
    return None


def offer_button(box: QtWidgets.QMessageBox, mid: str):
    """For the *Limited support* box of a file whose module ``mid`` is not
    installed: a "Download and install" button when GitHub publishes the
    module (looked up now, at most :data:`OPEN_TIMEOUT` seconds, then
    cached). Returns ``(button, available)``, or ``(None, None)``. When
    GitHub may not be asked unprompted, the button looks it up when clicked
    (``available`` is then None)."""
    from . import platforms as _platforms
    if not may_look_up() and not _cache.get("at"):
        name = getattr(_platforms.MODULES.get(mid), "name", mid)
        btn = box.addButton("Get the module from GitHub...",
                            QtWidgets.QMessageBox.ButtonRole.ActionRole)
        btn.setToolTip("Look for the %s module on GitHub and install it" % name)
        return btn, None
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
    try:
        avail, _err = published(timeout=OPEN_TIMEOUT)
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()
    top = best(avail, mid)
    if top is None:
        return None, None
    mb = top.package.size / 1e6
    btn = box.addButton("Download and install (%s, %.1f MB)" % (top.version, mb),
                        QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(btn)
    box.setInformativeText(
        box.informativeText() + "\n\nThe %s module %s is published on GitHub: "
        "download and install it to edit this file." % (top.name, top.version))
    return btn, avail
