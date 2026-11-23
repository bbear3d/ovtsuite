# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Exporting an EDC16 internal flash (MPC) in the form the other tool wants.

An MPC image is stored plain (0x81000, 0x80100 or 0x80000 bytes) or compressed
(the XOR / zlib ``.MPC`` of the BDM tools); every one of them opens here, and
every one can be written (:data:`ovtsuite.platforms.edc16.MPC_FORMATS`). The
save dialog lists the forms as file types, starting at the one the image came
in (``stored``: :func:`ovtsuite.platforms.edc16.mpc_format_of`).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6 import QtWidgets

from .platforms import edc16 as _e16

#: the forms' save-dialog filters
_FILTERS = {key: "%s (*.mpc *.bin)" % label for key, _n, label in _e16.MPC_FORMATS}


def filters() -> str:
    return ";;".join(_FILTERS[k] for k, _n, _l in _e16.MPC_FORMATS)


def format_of_filter(selected: str, default: str = "plain81000") -> str:
    """The format key of the save-dialog filter ``selected``."""
    for key, text in _FILTERS.items():
        if text == selected:
            return key
    return default


def stored_format(stored: Optional[dict]) -> Tuple[str, str]:
    """``(format key, trailer hex)`` of a ``mpc_format_of`` answer (plain
    0x81000 when there is none)."""
    stored = stored or {}
    return stored.get("format") or "plain81000", stored.get("trailer", "")


def save_mpc(parent, plain: bytes, name: str, stored: Optional[dict] = None,
             title: str = "Export MPC") -> Optional[str]:
    """Ask where and in which form to write the internal flash ``plain``
    (``name`` without extension; ``stored``: the form it came in), and write
    it. Returns the path, or None (cancelled, or it failed -- said)."""
    from .recent import get_last_dir, set_last_dir
    key, trailer = stored_format(stored)
    path, chosen = QtWidgets.QFileDialog.getSaveFileName(
        parent, title, os.path.join(get_last_dir("export"), name + ".mpc"),
        filters(), _FILTERS[key])
    if not path:
        return None
    key = format_of_filter(chosen, key)
    if not os.path.splitext(path)[1]:
        path += ".mpc"
    try:
        with open(path, "wb") as fh:
            fh.write(_e16.mpc_as(plain, key, trailer))
    except OSError as ex:
        QtWidgets.QMessageBox.critical(parent, title, str(ex))
        return None
    set_last_dir(path, "export")
    return path
