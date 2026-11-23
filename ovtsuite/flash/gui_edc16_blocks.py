# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The EDC16 flasher's block picker: which blocks **Read full...** reads and
**Write full...** writes, asked in a small dialog when one of them is chosen.

* Read full offers the blocks the ECU gives up over OBD (program,
  calibration, MPC) and Boot when the boot database can fill it; the first
  time, all the readable ones are ticked.
* Write full offers only the blocks the loaded project or file holds (the
  rest are greyed out); the first time, all of them are ticked -- Boot too,
  with a warning beside it (the riskiest block to write).
* After that, each picker brings back what was ticked the last time
  (``flash/edc16_blocks_read`` / ``flash/edc16_blocks_write``), less any
  block it cannot offer now.
"""
from typing import Dict, List, Optional

from PySide6 import QtCore, QtWidgets

from .. import branding
from . import edc16_blocks as B

READ, WRITE = "read", "write"

_KEY = {READ: "flash/edc16_blocks_read", WRITE: "flash/edc16_blocks_write"}

_TIPS = {
    "boot": "The boot block. It can't be read over OBD: Read full fills it from "
            "the boot database. The riskiest block to write.",
    "program": "The program (software).",
    "cal": "The calibration (maps).",
    "mpc": "The processor's internal flash.",
}


def _bootdb_ok() -> bool:
    try:
        from .. import bootdb
        return bool(bootdb.available())
    except Exception:
        return False


def offered(mode: str, avail: Optional[Dict[str, bool]] = None) -> Dict[str, bool]:
    """{key: whether the picker offers it}. ``avail``: which blocks the
    source holds (edc16_blocks.available), for a write."""
    if mode == READ:
        return {k: B.get(k).readable or (k == "boot" and _bootdb_ok())
                for k in B.UI_ORDER}
    avail = avail or {}
    return {k: bool(avail.get(k)) for k in B.UI_ORDER}


def defaults(mode: str, on: Dict[str, bool]) -> List[str]:
    """The first-time ticks: every readable block for a read; every block
    the source holds for a write (Boot too: the picker warns when it is
    ticked, and the confirmation says so in capitals)."""
    if mode == READ:
        return [k for k in B.UI_ORDER if on.get(k) and B.get(k).readable]
    return [k for k in B.UI_ORDER if on.get(k)]


def remembered(mode: str) -> Optional[List[str]]:
    """The blocks ticked the last time (None: never picked)."""
    v = QtCore.QSettings(*branding.QSETTINGS).value(_KEY[mode])
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        items = [x for x in str(v).split(",") if x]
    return [k for k in items if k in B.UI_ORDER]


def remember(mode: str, keys: List[str]) -> None:
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.setValue(_KEY[mode], ",".join(keys))
    s.sync()


def initial(mode: str, on: Dict[str, bool]) -> List[str]:
    """What the picker opens with: the last pick (of what is offered now),
    else the defaults."""
    last = remembered(mode)
    if last is not None:
        got = [k for k in last if on.get(k)]
        if got:
            return got
    return defaults(mode, on)


class BlockPickDialog(QtWidgets.QDialog):
    """Tick the blocks for one Read full / Write full."""

    def __init__(self, parent, mode: str, on: Dict[str, bool], ticked: List[str]):
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle("Read full" if mode == READ else "Write full")
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            "Blocks to read from the ECU:" if mode == READ else
            "Blocks to erase and rewrite from the loaded file:"))
        self.chks = {}
        for k in B.UI_ORDER:
            c = QtWidgets.QCheckBox(B.get(k).label)
            c.setEnabled(bool(on.get(k)))
            c.setChecked(bool(on.get(k)) and k in ticked)
            tip = _TIPS.get(k, "")
            if not on.get(k):
                tip += ("  Not offered: the boot database is not installed."
                        if mode == READ else
                        "  Not offered: the loaded file holds no data for it.")
            c.setToolTip(tip.strip())
            c.toggled.connect(self._check)
            self.chks[k] = c
            v.addWidget(c)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:gray")
        v.addWidget(self.hint)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Read" if mode == READ else "Write...")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._check()

    def picked(self) -> List[str]:
        return [k for k in B.UI_ORDER
                if self.chks[k].isEnabled() and self.chks[k].isChecked()]

    def _check(self, *_a):
        sel = self.picked()
        why = ""
        if not sel:
            why = "Tick at least one block."
        elif self.mode == READ and sel == ["boot"]:
            why = ("Boot is filled from the boot database, which identifies the "
                   "ECU from the program: tick Program as well.")
        elif self.mode == WRITE and "boot" in sel:
            why = "Boot is ticked: a failed boot write needs a bench (BDM) recovery."
        self.hint.setText(why)
        self.hint.setVisible(bool(why))
        self.ok.setEnabled(bool(sel) and not (self.mode == READ and sel == ["boot"]))


def _ask(dlg: BlockPickDialog) -> bool:
    """Run the picker (a seam for tests)."""
    return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted


def pick(parent, mode: str, avail: Optional[Dict[str, bool]] = None
         ) -> Optional[List[str]]:
    """Ask which blocks; None when cancelled. The pick is remembered."""
    on = offered(mode, avail)
    if not any(on.values()):
        return []
    d = BlockPickDialog(parent, mode, on, initial(mode, on))
    if not _ask(d):
        return None
    sel = d.picked()
    remember(mode, sel)
    return sel
