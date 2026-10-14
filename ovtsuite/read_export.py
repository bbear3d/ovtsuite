# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saving a finished read to a file rather than opening it.

Used by the standalone flasher (which has no tab to open a read in) and by
the main window's "Read complete" prompt (Export to file...). Two forms:

* an ``.ovt`` project whose original is the read (flash and, when there is
  one, the EEPROM read alongside it);
* the raw image as read (``.bin``): a native container a reader produced is
  unwrapped to its main image, and an EEPROM goes to a ``_EEPROM.bin`` file
  beside it.
"""

import os
from typing import List, Optional, Tuple

FILTERS = ["OVT project (*.ovt)", "Raw image as read (*.bin)", "All files (*)"]


def raw_parts(data, eeprom=None) -> Tuple[bytes, Optional[bytes]]:
    """The flash image as read, and its EEPROM (if any): a native container
    is unwrapped to its main image, and its EEPROM block is used when no
    separate EEPROM was given."""
    from . import container as _cont
    raw = bytes(data)
    if _cont.format_for_magic(raw[:8]) is not None:
        try:
            c = _cont.Container.from_bytes(raw)
        except Exception:
            c = None
        if c is not None:
            if eeprom is None and c.eeprom():
                eeprom = c.eeprom()
            raw = bytes(c.external() or raw)
    return raw, (bytes(eeprom) if eeprom is not None else None)


def eeprom_path(path: str) -> str:
    """Where a raw save puts the EEPROM: ``<name>_EEPROM.bin`` beside it."""
    return os.path.splitext(path)[0] + "_EEPROM.bin"


def save_read(path: str, data, name: str, eeprom=None, source=None,
              eeprom_too: bool = True) -> List[str]:
    """Save a read at ``path`` -- an .ovt project when it ends in .ovt, else
    the raw image (and the EEPROM beside it, unless ``eeprom_too`` is
    False). Returns the files written; raises on failure."""
    if path.lower().endswith(".ovt"):
        from .project import Project, import_read
        proj = Project(name)
        import_read(proj, data, name, eeprom=eeprom, source=dict(source or {}))
        proj.save(path)
        return [path]
    raw, ee = raw_parts(data, eeprom)
    with open(path, "wb") as fh:
        fh.write(raw)
    out = [path]
    if ee is not None and eeprom_too:
        ep = eeprom_path(path)
        with open(ep, "wb") as fh:
            fh.write(ee)
        out.append(ep)
    return out


def ask_save_read(parent, data, name: str, eeprom=None, source=None,
                  title: str = "Save the read") -> List[str]:
    """Ask where to save a read (an .ovt by default, or the raw image) and
    save it. Returns the files written ([] when cancelled or it failed;
    a failure is reported)."""
    from PySide6 import QtWidgets
    from . import recent as _recent
    default = os.path.join(_recent.get_last_dir(), name + ".ovt")
    path, chosen = QtWidgets.QFileDialog.getSaveFileName(
        parent, title, default, ";;".join(FILTERS))
    if not path:
        return []
    if not os.path.splitext(path)[1]:
        path += ".bin" if chosen == FILTERS[1] else ".ovt"
    eeprom_too = True
    if not path.lower().endswith(".ovt"):
        ee = raw_parts(data, eeprom)[1]
        ep = eeprom_path(path)
        if ee is not None and os.path.exists(ep):
            SB = QtWidgets.QMessageBox.StandardButton
            eeprom_too = QtWidgets.QMessageBox.question(
                parent, title, "The EEPROM read goes beside the image, in "
                "%s, which already exists. Replace it?" % os.path.basename(ep),
                SB.Yes | SB.No, SB.Yes) == SB.Yes
    try:
        written = save_read(path, data, name, eeprom=eeprom, source=source,
                            eeprom_too=eeprom_too)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(parent, title,
                                       "%s could not be saved:\n%s"
                                       % (os.path.basename(path), ex))
        return []
    _recent.set_last_dir(os.path.dirname(path))
    return written
