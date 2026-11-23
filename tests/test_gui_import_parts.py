# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The compare-first dialog for an EEPROM / MPC in an import into a linked
flash set (ovtsuite.gui_importparts), driven programmatically."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("PySide6")

from PySide6 import QtWidgets  # noqa: E402

from test_gui_eeprom_pairing import app, G, win, _doc  # noqa: E402,F401
from test_eeprom_pairing import eeprom15, flash15  # noqa: E402
from ovtsuite import gui_importparts as gip  # noqa: E402
from ovtsuite.project import Project  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402
from ovtsuite.project import companions as co  # noqa: E402


def _held(win):
    proj = Project("p")
    fl = imp.import_bytes(proj, flash15(), "flash.bin")
    imp.pair_eeprom(proj, fl.id, eeprom15(), "car.bin")
    doc = _doc(win, proj)
    held = {}
    donor = eeprom15(vin=b"WVWZZZ1JZ3W000002")
    imp.import_read(proj, flash15(), "read", eeprom=donor, into_set=fl.id,
                    held=held)
    return doc, proj, fl, held, donor


def test_default_is_keep_and_cancel_changes_nothing(win):
    doc, proj, fl, held, _d = _held(win)
    seen = {}

    def run(dlg):
        seen["choices"] = dlg.choices()
        seen["warn"] = dlg.findChild(QtWidgets.QLabel, "immo_warning")
        return True                                 # OK with the default
    assert gip.offer(win, doc, fl.id, held, "read.bin", dialog=run)[0].how == "keep"
    assert seen["choices"] == {"eeprom": "keep"} and seen["warn"] is not None
    assert co.injected(proj, fl.id)["eeprom"] == eeprom15()
    assert gip.offer(win, doc, fl.id, held, "read.bin",
                     dialog=lambda d: False) == []           # cancelled
    assert co.injected(proj, fl.id)["eeprom"] == eeprom15()


def test_choosing_a_new_version_applies_it(win):
    doc, proj, fl, held, donor = _held(win)

    def run(dlg):
        dlg.radios["eeprom"]["version"].setChecked(True)
        return True
    done = gip.offer(win, doc, fl.id, held, "read.bin", dialog=run)
    assert done[0].how == "version"
    assert co.injected(proj, fl.id)["eeprom"] == donor


def test_identical_parts_show_no_dialog(win):
    doc, proj, fl, _h, _d = _held(win)
    assert gip.offer(win, doc, fl.id, {"eeprom": eeprom15()}, "x.bin",
                     dialog=lambda d: pytest.fail("asked")) == []
