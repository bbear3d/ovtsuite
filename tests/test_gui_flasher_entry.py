# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""How the ECU flasher picks its module, and its standalone (--flasher) mode.

Offscreen Qt, guarded like test_gui_staged.py: any failure to import Qt is a skip.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import branding, cli  # noqa: E402
from ovtsuite.flash import gui_flash, standalone  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, app):
    """Saved flasher settings go to a temp dir, never the developer's own."""
    QtCore.QSettings.setPath(QtCore.QSettings.Format.NativeFormat,
                             QtCore.QSettings.Scope.UserScope, str(tmp_path))
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, str(tmp_path))
    yield


def _saved(ecu, transport="kline"):
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.beginGroup("flash")
    s.setValue("ecu", ecu)
    s.setValue("transport", transport)
    s.endGroup()
    s.sync()


class _Window(QtWidgets.QWidget):
    _eeprom = None

    def __init__(self, platform=None):
        super().__init__()
        self.primary = (types.SimpleNamespace(platform=platform,
                                              is_edc15=platform == "edc15")
                        if platform else None)


@pytest.mark.parametrize("platform,ecu", [
    ("edc16", "EDC16"), ("edc15", "EDC15"), ("zf6hp", "ZF6HP"), ("hvac", "Climatronic")])
def test_the_open_tab_decides_the_module(app, platform, ecu):
    _saved("EDC16" if ecu != "EDC16" else "ZF6HP")      # history says something else
    d = gui_flash.FlashDialog(_Window(platform))
    assert d._ecu() == ecu


def test_a_k_line_only_module_beats_a_saved_can_transport(app):
    _saved("EDC16", transport="can")
    d = gui_flash.FlashDialog(_Window("edc15"))
    assert d._ecu() == "EDC15" and not d._is_can()


def test_no_open_file_uses_the_last_selection(app):
    _saved("ZF6HP", transport="can")
    d = gui_flash.FlashDialog(_Window(None))
    assert d._ecu() == "ZF6HP"


def test_unknown_platform_uses_the_last_selection(app):
    _saved("EDC15")
    d = gui_flash.FlashDialog(_Window("mystery"))
    assert d._ecu() == "EDC15"


def test_standalone_dialog_has_a_file_picker_and_no_parent(app):
    d = gui_flash.FlashDialog(standalone.StandaloneFlashHost(), "write")
    assert d.parentWidget() is None
    assert d.file_btn is not None and d.file_lbl.text() == "No file chosen"
    with pytest.raises(RuntimeError, match="Choose a file"):
        d._prepare_write_image()


def test_main_window_dialog_has_no_file_picker(app):
    d = gui_flash.FlashDialog(_Window("edc16"))
    assert not hasattr(d, "file_btn")


def test_loading_a_file_retargets_the_module(app):
    _saved("EDC16")
    host = standalone.StandaloneFlashHost()
    d = gui_flash.FlashDialog(host, "write")
    host.open_path = lambda path, parent=None: setattr(
        host, "primary", types.SimpleNamespace(
            platform="zf6hp", is_edc15=False, path=path,
            binfile=types.SimpleNamespace(name="x.bin"))) or True
    assert d.load_file("x.bin")
    assert d._ecu() == "ZF6HP" and d.file_lbl.text() == "x.bin"


def test_a_bad_file_is_reported_and_keeps_the_previous_one(app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    host = standalone.StandaloneFlashHost()
    keep = object()
    host.primary = keep
    assert host.open_path(str(tmp_path / "missing.bin")) is False
    assert host.primary is keep and shown


def test_a_read_in_standalone_mode_is_saved_to_a_file(app, tmp_path, monkeypatch):
    target = tmp_path / "out.bin"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    assert standalone.StandaloneFlashHost().load_from_image(b"\x01\x02", "ECU-read")
    assert target.read_bytes() == b"\x01\x02"


def test_settings_are_keyed_per_module(app):
    """_save_settings stores under the ECU and records it as the last-used one."""
    d = gui_flash.FlashDialog(_Window("edc16"))
    i = d.transport_box.findData("can")
    if i >= 0:
        d.transport_box.setCurrentIndex(i)
    d._save_settings()
    s = QtCore.QSettings(*branding.QSETTINGS)
    assert s.value("flash/last_ecu") == "EDC16"
    assert s.value("flash/transport/EDC16") == "can"


def test_ecu_and_tcu_do_not_clobber_each_other(app):
    """The reported bug: the flasher launched from the ECU tab vs the TCU tab
    must bring back each module's own connection, not whichever flashed last."""
    # ECU tab (EDC16): force K-line and remember it.
    d_ecu = gui_flash.FlashDialog(_Window("edc16"))
    i = d_ecu.transport_box.findData("kline")
    if i >= 0:
        d_ecu.transport_box.setCurrentIndex(i)
    d_ecu._save_settings()
    assert not d_ecu._is_can()
    # TCU tab (ZF6HP): CAN-only; pick a distinctive rate and remember it.
    d_tcu = gui_flash.FlashDialog(_Window("zf6hp"))
    assert d_tcu._is_can()
    k = d_tcu.can_rate.findData(125000)
    assert k >= 0
    d_tcu.can_rate.setCurrentIndex(k)
    d_tcu._save_settings()
    # Reopen the ECU tab: EDC16 kept K-line even though ZF6HP (CAN) saved last.
    again_ecu = gui_flash.FlashDialog(_Window("edc16"))
    assert not again_ecu._is_can()
    # Reopen the TCU tab: ZF6HP kept its own CAN rate.
    again_tcu = gui_flash.FlashDialog(_Window("zf6hp"))
    assert again_tcu.can_rate.currentData() == 125000


def test_switching_module_in_the_dialog_recalls_its_settings(app):
    """Changing the ECU box live recalls THAT module's remembered setup."""
    seed = gui_flash.FlashDialog(_Window("zf6hp"))       # TCU tab seeds ZF6HP
    k = seed.can_rate.findData(125000)
    seed.can_rate.setCurrentIndex(k)
    seed._save_settings()
    d = gui_flash.FlashDialog(_Window("edc16"))          # open on EDC16...
    j = d.ecu_box.findText("ZF6HP")
    assert j >= 0
    d.ecu_box.setCurrentIndex(j)                         # ...switch to ZF6HP by hand
    assert d._ecu() == "ZF6HP"
    assert d.can_rate.currentData() == 125000            # recalled, not EDC16's


def test_old_flat_settings_migrate_once(app):
    """A pre-upgrade flat ``flash/*`` group is carried into the per-module keys."""
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.beginGroup("flash")
    s.setValue("ecu", "ZF6HP")
    s.setValue("transport", "can")
    s.endGroup()
    s.sync()
    gui_flash.FlashDialog(_Window(None))                 # no file -> last/migrated
    s2 = QtCore.QSettings(*branding.QSETTINGS)
    assert s2.value("flash/last_ecu") == "ZF6HP"
    assert s2.value("flash/transport/ZF6HP") == "can"
    assert gui_flash._as_bool(s2.value("flash/_migrated"), False)


def test_flasher_flag_launches_only_the_flasher(monkeypatch):
    import ovtsuite.gui as gui
    seen = []
    monkeypatch.setattr(gui, "run_flasher", lambda path=None: seen.append(path) or 0)
    assert cli.main(["--flasher"]) == 0
    assert cli.main(["--flasher", "tune.bin"]) == 0
    assert seen == [None, "tune.bin"]
