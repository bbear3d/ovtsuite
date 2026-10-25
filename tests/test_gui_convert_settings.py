# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Converter window's settings: every field set, the window closed, a new
one opened -- every value back; other targets' options kept while the target
changes; saved on change, on closing and on quitting, whatever the order the
window fills itself in. And the RomRaider ROM list's line while no RomRaider
file is added.

``python -m pytest tests/test_gui_convert_settings.py -q --slow``"""

import json
import os
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    # the settings of the test's own: never the user's window.json or QSettings
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))
    from PySide6 import QtCore
    from ovtsuite import appsettings
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appsettings, "settings",
                        lambda: QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
    from ovtsuite import gui_convert
    monkeypatch.setattr(gui_convert, "ask_overwrite", lambda parent, path: ("yes", False))
    # never a file dialog (the checksum list's "Another plugin (DLL)...")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *a, **k: pytest.fail("a file dialog was opened"))


def _state(tmp_path):
    with open(str(tmp_path / "cfg" / "window.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _set_all(d):
    """Every kept field set to a value other than its default: what was set."""
    from PySide6 import QtWidgets as W
    for name, w in d._persisted().items():
        if isinstance(w, W.QComboBox):
            if w.isEditable():
                w.setCurrentText({"tr_from": "fr", "tr_to": "pl"}[name])
                continue
            choices = [i for i in range(w.count())
                       if i != w.currentIndex() and w.itemData(i) not in ("dll", None, "")]
            if name == "tr_provider":                # (its default is "")
                choices = [i for i in range(w.count()) if w.itemData(i)]
            assert choices, name
            w.setCurrentIndex(choices[-1] if name != "xdf_checksum" else
                              w.findData("keep"))
        elif isinstance(w, (W.QCheckBox, W.QGroupBox)):
            w.setChecked(not w.isChecked())
        elif isinstance(w, W.QSpinBox):
            w.setValue(17 if w.value() != 17 else 19)
        else:
            w.setText({"fill": "0x00", "pattern": "IQ*", "categories": "Fuel; Boost"}[name])
    d.size.setText("0x80000")
    d.base.setText("0x400000")
    d.target.setCurrentIndex(d.target.findData("a2l"))
    d.tabs.setCurrentIndex(3)
    d.out_on.setChecked(False)
    # the Files tab's Binary tick, unticked by hand (as with a project open)
    d.bin_on.setEnabled(True)
    d.bin_on.setChecked(True)
    d.bin_on.setChecked(False)
    return d._fields()


def test_every_setting_comes_back(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    defaults = d._fields()
    want = _set_all(d)
    changed = [k for k in want if want[k] != defaults.get(k)]
    assert set(changed) == set(want), set(want) - set(changed)   # every one set
    d.close()                                    # (no explicit save: the close's)
    d2 = ConverterDialog(None)
    got = d2._fields()
    assert {k: got.get(k) for k in want} == want
    assert d2.target.currentData() == "a2l"
    assert d2.tabs.currentIndex() == 3
    assert not d2.out_on.isChecked()
    assert d2._emb_want == {"binary": False, "mpc": True}
    assert _state(tmp_path)["window"]["binary"] is False
    # the job built from them
    j = d2.job()
    assert j.size == 0x80000 and j.base == 0x400000
    assert j.filters["pattern"] == "IQ*" and j.filters["categories"] == ["Fuel", "Boost"]
    assert j.translate["source"] == "fr" and j.translate["target"] == "pl"
    assert j.options["existing"] == want["existing"]

    # every target in turn: the other formats' options are not touched
    for i in range(d2.target.count()):
        d2.target.setCurrentIndex(i)
    d2.target.setCurrentIndex(d2.target.findData("dam"))
    assert {k: d2._fields().get(k) for k in want} == want
    d2.close()
    d3 = ConverterDialog(None)
    assert {k: d3._fields().get(k) for k in want} == want
    assert d3.target.currentData() == "dam"
    d3.close()


def test_saved_soon_after_a_change_without_closing(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.record.setValue(77)
    d.a2l_addr.setCurrentIndex(d.a2l_addr.findData("ecu"))
    t = time.time()
    while d._dirty and time.time() - t < 5:
        app.processEvents()
        time.sleep(0.02)
    s = _state(tmp_path)["fields"]
    assert s["record"] == 77 and s["a2l_addresses"] == "ecu"
    # the app quitting with the window open (no closeEvent): flushed
    d.swap16.setChecked(True)
    d._flush_state()
    assert _state(tmp_path)["fields"]["swap16"] is True
    d.close()


def test_a_window_only_looked_at_does_not_overwrite(app, tmp_path):
    """Two windows (in the app and the standalone tool): one opened and
    closed untouched leaves the other's newer settings alone."""
    from ovtsuite.gui_convert import ConverterDialog
    a = ConverterDialog(None)
    b = ConverterDialog(None)
    a.record.setValue(99)
    a.close()
    b.close()
    assert _state(tmp_path)["fields"]["record"] == 99


def test_target_kept_while_the_files_do_not_offer_it(app, tmp_path):
    """The target is chosen once the files are added (the list is empty while
    the window opens); files that do not offer it do not replace it."""
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.target.setCurrentIndex(d.target.findData("kp"))
    d.close()
    vf = tmp_path / "v.dcm"
    vf.write_text("KONSERVIERUNG_FORMAT 2.0\n\nFESTWERT ABC\n   WERT 1.5\nEND\n")
    d = ConverterDialog(None, sources=[str(vf)])
    assert d.target.currentData() in ("dcm", "cdfx")    # values only
    d.record.setValue(40)                                # something else changed
    d.close()
    assert _state(tmp_path)["target"] == "kp"
    d = ConverterDialog(None)
    assert d.target.currentData() == "kp"
    d.close()


def test_a_field_being_typed_does_not_stop_the_rest_being_saved(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.size.setText("0xZZ")                       # not a number (yet)
    d.fill.setText("0x")
    d.record.setValue(48)
    d.close()
    d = ConverterDialog(None)
    assert d.record.value() == 48 and d.size.text() == "0xZZ" and d.fill.text() == "0x"
    d.close()


def test_an_older_versions_file_is_read(app, tmp_path):
    """The window.json older versions wrote: the job only (no "fields")."""
    from ovtsuite.gui_convert import ConverterDialog
    os.makedirs(str(tmp_path / "cfg"))
    with open(str(tmp_path / "cfg" / "window.json"), "w", encoding="utf-8") as fh:
        json.dump({"target": "xdf", "base": None, "size": None, "title": "",
                   "dynamic": "skip", "filters": {}, "checksum": "auto",
                   "translate": {"backend": "builtin", "provider": "", "source": "de",
                                 "target": "en", "mode": "bilingual"},
                   "options": {"indexed": False, "existing": "overwrite"},
                   "self_check": True,
                   "formats": {"a2l_addresses": "ecu", "record": 24},
                   "window": {"tab": 2, "convert": True, "binary": True, "mpc": False}},
                  fh)
    d = ConverterDialog(None)
    assert d.target.currentData() == "xdf" and d.cks.currentData() == "auto"
    assert not d.indexed.isChecked() and d.existing.currentData() == "overwrite"
    assert d.dynamic.currentData() == "skip"
    assert d.tr_box.isChecked() and d.tr_mode.currentData() == "bilingual"
    assert d.a2l_addr.currentData() == "ecu" and d.record.value() == 24
    assert d.tabs.currentIndex() == 2 and d._emb_want == {"binary": True, "mpc": False}
    d.close()


def test_the_dll_entry_never_opens_a_dialog_on_restore(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    os.makedirs(str(tmp_path / "cfg"))
    with open(str(tmp_path / "cfg" / "window.json"), "w", encoding="utf-8") as fh:
        json.dump({"target": "xdf", "checksum": "dll",
                   "fields": {"xdf_checksum": "dll"}}, fh)
    d = ConverterDialog(None)                    # (the fixture fails on a dialog)
    assert d.cks.currentData() != "dll"
    d.record.setValue(5)                         # (saved: something changed)
    d.close()
    assert _state(tmp_path)["fields"]["xdf_checksum"] != "dll"


def test_rom_list_says_how_it_fills(app, tmp_path):
    from test_convert_romxml import ROMRAIDER
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    assert d.rom.count() == 1 and d.rom.itemData(0) is None
    assert d.rom.currentText() == d.ROM_NONE and "Files tab" in d.ROM_NONE
    assert "Files tab" in d.rom.toolTip()
    assert "rom" not in d.job().options
    defs = tmp_path / "ecu_defs.xml"
    defs.write_text(ROMRAIDER)
    d.add_sources([str(defs)])
    assert [d.rom.itemData(i) for i in range(d.rom.count())] == [
        None, "32BITBASE", "DZ1E402B", "A2WC500R"]
    assert d.rom.currentText().startswith("Found from the binary")
    d.sources.selectAll()
    d._remove_selected()
    assert d.rom.count() == 1 and d.rom.currentText() == d.ROM_NONE
    d.close()
