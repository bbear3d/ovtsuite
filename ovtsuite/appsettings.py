# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""QSettings under the current brand, with a one-time carry-over from the old.

The GUI keeps its connection and flash preferences in ``QSettings``, whose
storage key is the (organisation, application) pair. Renaming the suite changes
that pair, so without help every saved transport, port, ECU and speed would
silently vanish on upgrade -- the settings are still on disk, just under a name
nothing reads any more.

:func:`migrate` copies them across once, on first run under the new name, and
never writes to the old scope. It is called from the GUI entry point; the
Qt-free core never touches it.
"""

from . import branding

#: Marker key: set once the carry-over has run, so it never runs twice (and so
#: a user who deliberately cleared their settings does not get them back).
_DONE_KEY = "_migrated_from"


def settings():
    """A ``QSettings`` in the current brand's scope."""
    from PySide6 import QtCore
    return QtCore.QSettings(*branding.QSETTINGS)


def _legacy_scopes():
    return [tuple(s) for s in branding.LEGACY_QSETTINGS]


def migrate() -> str:
    """Copy every key from the newest previous scope that has any, unless this
    scope already has settings or the carry-over has already run.

    Returns the scope it copied from (``"org/app"``), or ``""`` if it did
    nothing. Safe to call on every start.
    """
    from PySide6 import QtCore

    cur = QtCore.QSettings(*branding.QSETTINGS)
    if cur.value(_DONE_KEY) or cur.allKeys():
        return ""
    for org, app in _legacy_scopes():
        old = QtCore.QSettings(org, app)
        keys = old.allKeys()
        if not keys:
            continue
        for k in keys:
            cur.setValue(k, old.value(k))
        cur.setValue(_DONE_KEY, "%s/%s" % (org, app))
        cur.sync()
        return "%s/%s" % (org, app)
    cur.setValue(_DONE_KEY, "")           # nothing to carry; don't look again
    cur.sync()
    return ""
