# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Force full write is off on a first launch for every flasher entry, is not
remembered between runs, and a tick does not follow the user to another
control unit. Offscreen Qt."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.flash import gui_flash  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, app):
    for fmt in (QtCore.QSettings.Format.NativeFormat, QtCore.QSettings.Format.IniFormat):
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.Scope.UserScope, str(tmp_path))
    yield


class _Window(QtWidgets.QWidget):
    _eeprom = None
    primary = None

    def load_from_image(self, data, name):
        pass


def test_every_flasher_entry_starts_with_force_full_off():
    d = gui_flash.FlashDialog(_Window())
    assert d.ecu_box.count() >= 8
    for i in range(d.ecu_box.count()):
        d.ecu_box.setCurrentIndex(i)
        assert not d.force_full.isChecked(), d.ecu_box.currentText()


def test_force_full_is_not_remembered_between_runs_or_between_units():
    d = gui_flash.FlashDialog(_Window())
    d.ecu_box.setCurrentText("EDC15")
    d.force_full.setChecked(True)
    d.ecu_box.setCurrentText("EDC16")
    assert not d.force_full.isChecked()          # not carried to another unit
    d.ecu_box.setCurrentText("EDC15")
    d.force_full.setChecked(True)
    d._save_settings()
    d.close()
    again = gui_flash.FlashDialog(_Window())     # the next run
    for i in range(again.ecu_box.count()):
        again.ecu_box.setCurrentIndex(i)
        assert not again.force_full.isChecked(), again.ecu_box.currentText()


# ----------------------------------------- a failed write suggests a lower speed
@pytest.fixture
def shown(monkeypatch):
    seen = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        staticmethod(lambda parent, title, text, *a, **k: seen.append(text)))
    return seen


def _dialog(ecu, speed, op="write"):
    d = gui_flash.FlashDialog(_Window())
    d.ecu_box.setCurrentText(ecu)
    d.speed_box.setCurrentIndex(d.speed_box.findData(speed))
    d._op = op
    return d


def test_failed_write_suggests_the_next_lower_speed(shown):
    d = _dialog("EDC16", "Medium")
    assert d.speed_box.currentData() == "Medium"
    d._error("Block 12 was refused.")
    assert "try a lower speed (Low (38400))" in shown[-1]
    assert shown[-1].startswith("Block 12 was refused.")
    assert d.stage.text() == "Failed: Block 12 was refused."
    d = _dialog("EDC15", "Medium", op="ee_write")
    d._error("No answer.")
    assert "lower speed (Low (10400))" in shown[-1]


def test_no_suggestion_at_the_lowest_speed_for_reads_or_a_cancel(shown):
    d = _dialog("EDC16", "Very Low")
    d._error("Block 12 was refused.")
    assert shown[-1] == "Block 12 was refused."
    d = _dialog("EDC15", "Low")
    d._error("No answer.")
    assert shown[-1] == "No answer."
    d = _dialog("EDC16", "Medium", op="read")
    d._error("No answer.")
    assert shown[-1] == "No answer."
    d = _dialog("EDC16", "Medium")
    d._error("Stopped.")
    assert shown[-1] == "Stopped."


def test_no_suggestion_where_no_speed_is_chosen(shown):
    d = _dialog("EDC16", "Medium")
    d.transport_box.setCurrentIndex(d.transport_box.findData("can"))
    d._error("Block 12 was refused.")
    assert "lower speed" not in shown[-1]
    d = _dialog("ZF6HP", "Medium")
    d._error("Block 12 was refused.")
    assert "lower speed" not in shown[-1]


# ------------------------------------------- the flasher and an EEPROM-only tab
def test_an_eeprom_tab_picks_the_edc15_entry_and_is_the_eeprom_to_write():
    from types import SimpleNamespace
    from ovtsuite.flash import standalone
    assert standalone.ecu_for_platform("edc15eeprom") == "EDC15"
    img = bytes(range(256)) * 2

    class Tab(_Window):
        primary = SimpleNamespace(platform="edc15eeprom", binfile=SimpleNamespace(data=img))

        def is_eeprom_image(self):
            return True
    d = gui_flash.FlashDialog(Tab())
    d.ecu_box.setCurrentText("EDC15")
    assert d._window_eeprom() == img
    assert d._held_eeprom() == img


def test_finishing_a_job_leaves_the_controls_as_the_unit_had_them():
    """_busy(False) used to switch the Speed list back on for the VDO cluster
    (it talks at 10400 only)."""
    d = gui_flash.FlashDialog(_Window())
    d.show()
    for i in range(d.ecu_box.count()):
        d.ecu_box.setCurrentIndex(i)
        before = (d.speed_box.isEnabled(), d.force_full.isVisibleTo(d),
                  d.fast_disable_chk.isVisibleTo(d), d.boot_mode.isVisibleTo(d))
        d._busy(True)
        assert not d.speed_box.isEnabled()
        d._busy(False)
        after = (d.speed_box.isEnabled(), d.force_full.isVisibleTo(d),
                 d.fast_disable_chk.isVisibleTo(d), d.boot_mode.isVisibleTo(d))
        assert before == after, d.ecu_box.currentText()
