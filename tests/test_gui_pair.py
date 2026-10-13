# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The pairing dialog's scan, driven rather than eyeballed.

Offscreen Qt. What is worth pinning is the small set of things a driver in a car
depends on: that a found unit fills the address in, that an already-paired one
says so instead of looking like a failure, and that finding nothing offers the
way that always works.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.flash import discover, headunits  # noqa: E402
from ovtsuite.flash.gui_pair import PairHeadUnitDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _found(host="192.168.43.1", paired=False):
    return discover.Found(host, headunits.DEFAULT_PORT, {
        "cable": "ch34x", "can": False, "five_baud": True,
        "max_speed": 0x87, "paired": paired})


def test_choosing_a_found_unit_fills_the_address_in(app):
    dialog = PairHeadUnitDialog()
    dialog._add(_found())

    assert dialog.host.text() == "192.168.43.1"
    assert dialog.port.value() == headunits.DEFAULT_PORT


def test_one_result_is_selected_without_being_clicked(app):
    """The usual case is exactly one head unit, and making the driver click it
    is a step that can only be got wrong."""
    dialog = PairHeadUnitDialog()
    dialog._add(_found())

    assert dialog.found.currentRow() == 0


def test_an_already_paired_unit_says_so(app):
    dialog = PairHeadUnitDialog()
    dialog._add(_found(paired=True))

    assert "Already paired" in dialog.status.text()


def test_the_label_says_which_ones_need_pairing(app):
    assert "needs pairing" in _found().label
    assert "paired" in _found(paired=True).label
    assert "ch34x" in _found().label


def test_finding_nothing_points_at_the_thing_that_always_works(app):
    dialog = PairHeadUnitDialog()
    dialog._scanned()

    text = dialog.status.text()
    assert "Nothing answered" in text
    assert "address" in text, "the manual address is the fallback that always works"


def test_the_scan_button_becomes_the_stop_button(app, monkeypatch):
    """A sweep is 254 addresses; a driver who starts one by accident has to be
    able to stop it without waiting for all of them."""
    started = []

    class _Fake:
        def __init__(self, *a, **k):
            self.stopped = False
            self.found = _Signal()
            self.done = _Signal()

        def start(self):
            started.append(self)

        def stop(self):
            self.stopped = True

    class _Signal:
        def connect(self, *_):
            pass

    monkeypatch.setattr("ovtsuite.flash.gui_pair._Sweep", _Fake)

    dialog = PairHeadUnitDialog()
    dialog._scan()
    assert dialog.scan.text() == "Stop"
    assert len(started) == 1

    dialog._scan()                                  # second press stops it
    assert started[0].stopped is True
    assert len(started) == 1, "and does not start a second sweep"
