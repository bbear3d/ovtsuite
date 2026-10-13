# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flash dialog's staged-mode wiring, driven for real rather than eyeballed.

Offscreen Qt, so it runs anywhere PySide6 is installed. What it pins is the small
set of rules that decide whether a tuner is offered staged at all -- the sort that
look obvious in the code and are wrong in the dialog.

The import below is guarded by hand rather than with ``pytest.importorskip``:
a headless box can have PySide6 installed and still fail the import on a missing
``libEGL``/``libGL``, which is a plain ``ImportError`` rather than a
``ModuleNotFoundError``. ``importorskip`` re-raises that -- correctly, since a
broken install is usually worth hearing about -- and it takes the whole
collection down with it. Here Qt is the environment, not the subject, so any
failure to import it is a skip.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.flash import gui_flash, headunits  # noqa: E402
from ovtsuite.flash.gui_pair import PairHeadUnitDialog  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Window(QtWidgets.QWidget):
    primary = None
    _eeprom = None


@pytest.fixture
def dialog(app):
    window = _Window()
    d = gui_flash.FlashDialog(window, mode="write")

    # The picker holds whatever this machine has; add two head units so the rules
    # can be exercised without one being paired.
    d.port_box.addItem("Golf", headunits.HeadUnit(
        "Golf", "192.168.1.42", 35510, b"\x01" * 32, cable="ch34x",
        can=False, max_speed=0x87))
    d.port_box.addItem("Bench", headunits.HeadUnit(
        "Bench", "10.0.0.5", 35510, b"\x02" * 32, cable="ftdi",
        can=True, max_speed=0xA7))
    return d


def _select(dialog, predicate):
    for i in range(dialog.port_box.count()):
        if predicate(dialog.port_box.itemData(i)):
            dialog.port_box.setCurrentIndex(i)
            dialog._port_changed()
            return dialog.port_box.itemData(i)
    return None


def _remote(host):
    return lambda data: gui_flash.is_remote(data) and getattr(data, "host", "") == host


def test_staged_is_offered_only_by_a_head_unit(dialog):
    if _select(dialog, lambda d: isinstance(d, str)) is None:
        pytest.skip("no local serial ports on this machine")

    assert not dialog.staged_chk.isEnabled(), "a local cable cannot run a job on its own"
    assert dialog.fast_disable_chk.isEnabled(), "and the init choice stays the user's"


def test_a_write_to_a_head_unit_defaults_to_staged(dialog):
    _select(dialog, _remote("192.168.1.42"))

    assert dialog.staged_chk.isEnabled()
    assert dialog.staged_chk.isChecked(), \
        "surviving a closed lid mid-flash is the whole reason the mode exists"

    # The init strategy is read from the image by the descriptor builder, so the
    # fast-init checkbox has nothing to say about a staged job.
    assert not dialog.fast_disable_chk.isEnabled()

    dialog.staged_chk.setChecked(False)
    assert dialog.fast_disable_chk.isEnabled(), "unticking it hands the choice back"


def test_both_operations_stage_on_both_controllers(dialog):
    """Reads too, and the first version of this was wrong to treat them as the safe case.
    Staged mode is not only about surviving an interrupted erase - it is about keeping the
    network out of a conversation that has the ECU's own timing in it. A 512 KB read is
    ~2100 solicited packets, and for an EDC15 the thin path cannot carry the loader's
    framing at all."""
    _select(dialog, _remote("192.168.1.42"))

    for index in (0, 1):                            # EDC16, EDC15
        dialog.ecu_box.setCurrentIndex(index)

        for op in ("read", "write"):
            dialog._op = op
            dialog._apply_staged_default()
            assert dialog.staged_chk.isEnabled(), (
                "%s on %s can be staged" % (op, dialog.ecu_box.currentText()))
            assert dialog.staged_chk.isChecked(), "and defaults to it"


def test_the_speed_cap_is_the_far_cable_s(dialog):
    """No local entry can answer this: the cable is in the car, and it told us."""
    ftdi = _select(dialog, _remote("10.0.0.5"))
    ch34x = dialog.port_box.itemData(
        [i for i in range(dialog.port_box.count())
         if gui_flash.is_remote(dialog.port_box.itemData(i))
         and dialog.port_box.itemData(i).host == "192.168.1.42"][0])

    assert gui_flash.cable_allows_high(ftdi), "an FTDI lead in the car can hold 244898"
    assert not gui_flash.cable_allows_high(ch34x), "a CH34x cannot, wherever it is"


def test_the_pairing_dialog_asks_for_what_the_dashboard_shows(app):
    d = PairHeadUnitDialog()

    assert d.port.value() == headunits.DEFAULT_PORT
    assert d.pin.maxLength() == 6, "the PIN on the head unit is six digits"
    assert d.unit is None, "nothing is paired until it succeeds"


def test_an_edc15_staged_read_carries_a_loader_and_a_plan():
    """The read's end is not on the address ladder: the loop stops at 0x7FEE2 and the last
    0x11E bytes come out of one fixed request, which travels as a value because the
    arithmetic that produced it is this end's."""
    from ovtsuite.flash import staged as _staged

    job, blob = _staged.build_edc15_read_job()

    assert job["kind"] == "read" and job["ecu"] == "edc15"
    assert job["regions"] == [{"addr": 0, "size": 0x80000}]
    assert job["loader"]["length"] == len(blob) == 1016
    assert job["loader"]["declared"] == 0x420, "declared and sent are different numbers"
    assert job["loader"]["magic"] == "c3c389c545da630b"
    assert job["read"]["packet"] == 0xFE and job["read"]["base"] == 0x08
    assert job["read"]["end"] == 0x7FEE2
    assert job["read"]["tail"].startswith("05230fffe020")

    # The job carries the one variant it is for: V, one exchange -- the only one this
    # tuner has validated; P and VM are refused rather than sent unproven.
    v = job["variant"]
    assert v["name"] == "V" and v["ardu"] and v["ecu"] and len(v["handshake"]) == 1
    for name in ("P", "VM"):
        with pytest.raises(ValueError):
            _staged.build_edc15_read_job(variant=name)


def test_an_edc15_staged_job_carries_sectors_and_a_loader():
    from ovtsuite.flash import staged as _staged

    image = bytes((i * 7 + 3) & 0xFF for i in range(0x80000))
    job, blob = _staged.build_edc15_write_job(image)

    assert job["ecu"] == "edc15" and job["kind"] == "write"
    assert job["init"] == "kw1281-then-kwp"
    assert "blocks" not in job, "sectors and blocks are different conversations"
    assert len(job["sectors"]) == 11
    assert len(blob) == job["loader"]["length"] + 0x80000

    # The three address windows, which is the thing most worth pinning: none of them
    # follows from the others, and getting one wrong erases somewhere else entirely.
    first = job["sectors"][0]
    assert first["addr"] == 0x00000
    assert first["write_addr"] == 0x200000
    assert first["cks_from"] == 0x080000 and first["cks_to"] == 0x090000
