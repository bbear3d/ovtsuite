# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag as a program of its own: ``ovtsuite --diag`` / ``OVTDiag.exe``.

The same host as the other tools (:func:`ovtsuite.gui._run_tool`): its own
taskbar identity and icon, the suite's diagnostics log, settings, theme,
splash, F1 help and the Help/About buttons -- and no main window, because
OVT Diag never needed one: it talks to the car, not to a binary.
"""
from typing import Sequence

from .. import branding


def run_diag(args: Sequence[str] = ()) -> int:
    """Launch OVT Diag. ``args`` is reserved (a vehicle id to open, later)."""
    from .. import gui

    def make():
        from .gui_diag import DiagWindow
        return DiagWindow()

    return gui._run_tool("diag", branding.DIAG_APP_ID, make)
