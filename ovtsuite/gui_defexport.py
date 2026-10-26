# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""File > Export > Definition as A2L... and Map pack for WinOLS
(.kp)...: the tab's parameters (as File > Export > Definition as
XDF... takes them) written by :mod:`ovtsuite.a2l_export` and
:mod:`ovtsuite.definitions.winols_write`."""

from __future__ import annotations

import os

from PySide6 import QtWidgets

from . import branding


def _ask(ws, title: str, filt: str, ext: str):
    from .recent import get_last_dir, set_last_dir
    base = os.path.splitext(ws.primary.binfile.name)[0] + ext
    path, _f = QtWidgets.QFileDialog.getSaveFileName(
        ws, title, os.path.join(get_last_dir("export"), base), filt)
    if path:
        set_last_dir(path, "export")
    return path


def _entities(ws):
    p = ws.primary
    return p.entities + p.variant_entities()


def _report(ws, title: str, path: str, warnings):
    msg = "Written: %s" % os.path.basename(path)
    if warnings:
        shown = "\n".join(warnings[:15])
        more = "\n... and %d more" % (len(warnings) - 15) if len(warnings) > 15 else ""
        QtWidgets.QMessageBox.information(
            ws, title, "%s\n\nNot everything could be expressed:\n\n%s%s"
            % (msg, shown, more))
    else:
        ws.statusBar().showMessage(msg, 8000)


def export_a2l(ws) -> str:
    """Write the tab's parameters as an A2L. Returns the path, or ""."""
    from .a2l_export import export_a2l as _export
    if ws is None or ws.primary is None:
        return ""
    path = _ask(ws, "Export A2L", "ASAP2 descriptions (*.a2l)", ".a2l")
    if not path:
        return ""
    warnings = []
    text = _export(_entities(ws), branding.tagged(ws.primary.binfile.name),
                   image_size=len(ws.primary.binfile.data),
                   platform=ws.primary.platform, warnings=warnings)
    try:
        with open(path, "w", encoding="ascii", errors="replace", newline="\n") as fh:
            fh.write(text)
    except OSError as ex:
        QtWidgets.QMessageBox.warning(ws, "Export A2L", "Not written: %s" % ex)
        return ""
    _report(ws, "Export A2L", path, warnings)
    return path


def export_kp(ws) -> str:
    """Write the tab's parameters as a WinOLS map pack.
    Returns the path, or ""."""
    from .definitions.winols_write import write_kp
    if ws is None or ws.primary is None:
        return ""
    path = _ask(ws, "Export map pack", "WinOLS map packs (*.kp)", ".kp")
    if not path:
        return ""
    warnings = []
    data = write_kp(_entities(ws), len(ws.primary.binfile.data),
                    title=os.path.splitext(os.path.basename(path))[0],
                    warnings=warnings)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as ex:
        QtWidgets.QMessageBox.warning(ws, "Export map pack", "Not written: %s" % ex)
        return ""
    _report(ws, "Export map pack", path, warnings)
    return path
