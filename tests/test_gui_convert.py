# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Converter window (offscreen): sources, targets, preview, convert,
review, presets. ``python -m pytest tests/test_gui_convert.py -q --slow``"""

import os
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLS = os.path.join(ROOT, "Reference", "_incoming", "2026-10-14", "028906021CK_MSA15.ols")
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")
DAM = os.path.join(ROOT, "Reference", "EDC15", "038906019GQ.dam")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))
    # the window's size and splitter go to a file of the test's own, not
    # the user's settings
    from PySide6 import QtCore
    from ovtsuite import appsettings
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appsettings, "settings",
                        lambda: QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))


def _wait(app, dlg, limit=120):
    t = time.time()
    while dlg._worker is not None and time.time() - t < limit:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def test_definition_preview_and_convert(app, tmp_path):
    if not os.path.isfile(OLS):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([OLS])
    fmts = [d.target.itemData(i) for i in range(d.target.count())]
    assert "xdf" in fmts and "hex" in fmts          # an OLS carries its image
    d.target.setCurrentIndex(d.target.findData("xdf"))
    assert d.out.text().endswith(".xdf")
    d.out.setText(str(tmp_path / "o.xdf"))
    seen = []
    d.progress.valueChanged.connect(seen.append)
    d.preview()
    _wait(app, d)
    assert d.table.rowCount() > 7000
    assert d.progress.value() == d.progress.maximum() > 1      # a bar, not a spinner
    del seen[:]
    d.convert()
    _wait(app, d)
    assert os.path.isfile(str(tmp_path / "o.xdf"))
    assert "read-back check passed" in d.summary.text()
    # the bar filled from empty to full through the steps, never going back
    assert d.progress.minimum() == 0 and d.progress.maximum() > 1
    assert d.progress.value() == d.progress.maximum()
    assert seen[0] == 0 and seen[-1] == d.progress.maximum()
    middle = [v for v in seen if 0 < v < d.progress.maximum()]
    assert len(middle) >= 3 and middle == sorted(middle)


def test_progress_reports_the_share_done(tmp_path):
    """The run reports how far it is beside each step's name: rising from
    0 to 1, one-argument step callbacks working as before."""
    if not os.path.isfile(OLS):
        pytest.skip("reference file missing")
    from ovtsuite import convert as C
    steps, shares = [], []
    C.run(C.Job(sources=[OLS], target="xdf", out=str(tmp_path / "a.xdf")),
          progress=steps.append, fraction=shares.append)
    assert "Reading" in steps and "Writing" in steps
    assert shares == sorted(shares) and shares[-1] == 1.0 and len(shares) >= 4
    assert all(0.0 < f <= 1.0 for f in shares)
    steps2 = []
    C.run(C.Job(sources=[OLS], target="xdf", out=str(tmp_path / "b.xdf")),
          progress=steps2.append)
    assert steps2 == steps


def test_an_error_empties_the_bar(app, monkeypatch):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    d._busy(True, "Converting...")
    d._set_fraction(0.4)
    assert 0 < d.progress.value() < d.progress.maximum()
    d._convert_done(None, RuntimeError("no"))
    assert d.progress.value() == 0


def test_image_targets_hide_definition_transforms(app, tmp_path):
    if not os.path.isfile(LQ):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([LQ])
    assert [d.target.itemData(i) for i in range(d.target.count())] == ["bin", "hex", "srec"]
    assert not d.tr_box.isEnabled() and not d.filter_box.isEnabled()
    d.target.setCurrentIndex(d.target.findData("hex"))
    d.out.setText(str(tmp_path / "o.hex"))
    d.convert()
    _wait(app, d)
    assert os.path.getsize(str(tmp_path / "o.hex")) > 0x200000


def test_review_edits_reach_the_job(app):
    if not os.path.isfile(DAM):
        pytest.skip("reference file missing")
    from ovtsuite import convert as C
    from ovtsuite.convert import translate as T
    from ovtsuite.gui_convert import ReviewDialog
    defn = C.prepare(C.Job(sources=[DAM], target="xdf"))[0]
    rv = ReviewDialog(T.plan(defn, {"backend": "builtin"}))
    rv.table.item(0, 3).setText("Mine")
    rv._accept()
    edited = [r for r in rv.rows if r["by"] == "you"]
    assert len(edited) == 1 and edited[0]["result"] == "Mine"


def test_presets_round_trip(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.apply_settings({"target": "a2l", "size": 0x200000,
                      "options": {"a2l_addresses": "ecu"},
                      "translate": {"backend": "builtin", "mode": "bilingual"}})
    assert d.size.text() == "0x200000" and d.tr_box.isChecked()
    assert d.a2l_addr.currentData() == "ecu"
    assert d.job().size == 0x200000


# ------------------------------------------------------------------ values
VF = os.path.join(ROOT, "Reference", "ValueFiles", "scm900_42AEb_changed_labels.DCM")
BPW = os.path.join(ROOT, "Reference", "A2L", "BPW.a2l")


def test_value_files_offer_value_targets(app, tmp_path):
    if not os.path.isfile(VF):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([VF])
    assert [d.target.itemData(i) for i in range(d.target.count())] == ["dcm", "cdfx"]
    d.target.setCurrentIndex(d.target.findData("cdfx"))
    assert d.filter_box.isEnabled() and not d.tr_box.isEnabled()
    d.out.setText(str(tmp_path / "o.cdfx"))
    d.convert()
    _wait(app, d)
    assert os.path.isfile(str(tmp_path / "o.cdfx"))
    assert d.table.rowCount() > 400
    assert "read-back check passed" in d.summary.text()


def test_definition_exports_values_with_its_binary(app, tmp_path, monkeypatch):
    if not (os.path.isfile(BPW) and os.path.isfile(LQ)):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: asked.append(a[2]))
    d = ConverterDialog(None)
    d.add_sources([BPW])
    fmts = [d.target.itemData(i) for i in range(d.target.count())]
    assert "xdf" in fmts and "dcm" in fmts and "cdfx" in fmts
    d.target.setCurrentIndex(d.target.findData("dcm"))
    assert "values read from the binary" in d.target.currentText()
    assert "needed" in d.binary.placeholderText()
    d.binary.setText("")
    d.out.setText(str(tmp_path / "o.dcm"))
    d.convert()                                   # no binary: asked for it
    assert asked and "binary" in asked[0] and d._worker is None
    d.binary.setText(LQ)
    d.convert()
    _wait(app, d)
    assert os.path.getsize(str(tmp_path / "o.dcm")) > 100000
    assert "read-back check passed" in d.summary.text()


def test_definition_and_values_write_into_a_binary(app, tmp_path):
    from test_convert_values import _mini
    from ovtsuite.gui_convert import ConverterDialog
    a2l, s19, _img = _mini(tmp_path)
    dcm = tmp_path / "delta.dcm"
    dcm.write_text("KONSERVIERUNG_FORMAT 2.0\nFESTWERT K_SPEED\n   WERT 1250.5\nEND\n"
                   "FESTWERT NOT_THERE\n   WERT 1\nEND\n")
    d = ConverterDialog(None)
    d.add_sources([a2l, str(dcm)])
    assert d._mode() == "apply"
    assert [d.target.itemData(i) for i in range(d.target.count())] == ["bin", "hex", "srec"]
    d.target.setCurrentIndex(d.target.findData("srec"))
    assert d.opts.currentIndex() == d._opt_pages["apply"]
    d.apply_cks.setChecked(False)
    d.binary.setText(s19)
    d.out.setText(str(tmp_path / "out.s19"))
    d.convert()
    _wait(app, d)
    assert os.path.isfile(str(tmp_path / "out.s19"))
    names = {d.table.item(r, 1).text(): d.table.item(r, 0).data(0x0100)
             for r in range(d.table.rowCount())}
    assert names == {"K_SPEED": None, "NOT_THERE": 1}     # marked: not applied
    assert "1 changed" in d.summary.text()


# ------------------------------------------------------------------ port, diff
FK = os.path.join(ROOT, "Reference", "A2L", "03G906016FK_9971_399899_P379_U85B6_EDC16U1_5.41")
AA = os.path.join(ROOT, "Reference", "A2L", "03G906016AA_5928_370480_P379_U85B2_EDC16U1_5.41")
AA_BIN = os.path.join(ROOT, "Reference", "Binaries", "03G906016AA_U85B2_from_A2L_HEX.bin")


def test_port_onto_another_software_version(app, tmp_path, monkeypatch):
    if not all(os.path.isfile(p) for p in (FK + ".a2l", FK + ".HEX", AA_BIN)):
        pytest.skip("reference file missing")
    from ovtsuite import convert as C
    from ovtsuite.gui_convert import ConverterDialog
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: asked.append(a[2]))
    d = ConverterDialog(None)
    d.add_sources([FK + ".a2l"])
    # greyed out until wanted, never hidden (the window keeps its shape)
    assert d.port.isEnabled() and not d.port_box.isEnabled()
    d.port.setChecked(True)
    assert d.port_box.isEnabled()
    assert "other software version" in d.binary.placeholderText()
    d.target.setCurrentIndex(d.target.findData("json"))
    d.binary.setText("")
    d.out.setText(str(tmp_path / "ported.ovtdef.json"))
    d.convert()                                   # no binary: asked for it
    assert asked and "port" in asked[0] and d._worker is None
    d.binary.setText(AA_BIN)
    d.reference.setText(FK + ".HEX")
    j = d.job()
    assert j.options["port"] and j.options["reference"] == FK + ".HEX"
    d.convert()
    _wait(app, d)
    assert "ported:" in d.summary.text() and "found moved" in d.summary.text()
    with open(str(tmp_path / "ported.ovtdef.json"), "rb") as fh:
        defn = C.load_definition(fh.read())
    assert any(C.UNVERIFIED in t.categories for t in defn.tables)


def test_two_definitions_offer_a_comparison(app, tmp_path):
    if not all(os.path.isfile(p) for p in (FK + ".a2l", AA + ".a2l")):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([FK + ".a2l"])
    assert "diff" not in [d.target.itemData(i) for i in range(d.target.count())]
    d.add_sources([AA + ".a2l"])
    d.target.setCurrentIndex(d.target.findData("diff"))
    d.out.setText(str(tmp_path / "fk_aa.diff.csv"))
    d.convert()
    _wait(app, d)
    assert d.table.horizontalHeaderItem(2).text() == "Change"
    assert d.table.rowCount() > 8000
    assert os.path.isfile(str(tmp_path / "fk_aa.diff.csv"))


def test_xdf_with_a_patch_from_a_changed_binary(app, tmp_path, monkeypatch):
    xdf = os.path.join(ROOT, "Reference", "XDFs", "038997016Q-BEW-BHW.xdf")
    tuned = os.path.join(ROOT, "Reference", "Tunes", "016Q_uprava_klapka_T5.bin")
    if not (os.path.isfile(xdf) and os.path.isfile(tuned + ".bak")):
        pytest.skip("reference file missing")
    from ovtsuite import convert as C
    from ovtsuite.gui_convert import ConverterDialog
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: asked.append(a[2]))
    d = ConverterDialog(None)
    d.add_sources([xdf])
    d.target.setCurrentIndex(d.target.findData("xdf"))
    assert not d.patch_opts.isEnabled()
    d.patch_with.setText(tuned)
    assert d.patch_opts.isEnabled()
    d.binary.setText("")
    d.out.setText(str(tmp_path / "with_patch.xdf"))
    d.convert()                                   # no original: asked for it
    assert asked and "original binary" in asked[0]
    d.binary.setText(tuned + ".bak")
    d.patch_split.setCurrentIndex(d.patch_split.findData("table"))
    d.convert()
    _wait(app, d)
    with open(str(tmp_path / "with_patch.xdf"), "rb") as fh:
        assert len(C.load_definition(fh.read()).patches) == 6


def test_romraider_rom_picker(app, tmp_path, monkeypatch):
    from test_convert_romxml import ROMRAIDER
    from ovtsuite import convert as C
    from ovtsuite.gui_convert import ConverterDialog
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: asked.append(a[2]))
    defs = tmp_path / "ecu_defs.xml"
    defs.write_text(ROMRAIDER)
    img = bytearray(0x10000)
    img[0x2004:0x200C] = b"A2WC500R"
    car = tmp_path / "car.bin"
    car.write_bytes(bytes(img))
    d = ConverterDialog(None)
    assert d.rom_box.isHidden()
    d.add_sources([str(defs)])
    assert not d.rom_box.isHidden()
    assert [d.rom.itemData(i) for i in range(d.rom.count())] == [
        None, "32BITBASE", "DZ1E402B", "A2WC500R"]
    d.target.setCurrentIndex(d.target.findData("json"))
    d.out.setText(str(tmp_path / "o.json"))
    d.binary.setText("")
    d.convert()                                   # three ROMs, no binary: asked
    assert asked and "pick the one" in asked[-1]
    d.binary.setText(str(car))                    # found from the binary
    assert "A2WC500R" in d.rom_found.text()
    d.convert()
    _wait(app, d)
    with open(str(tmp_path / "o.json"), "rb") as fh:
        assert [t.z.addr for t in C.load_definition(fh.read()).tables] == [0xC5000]
    d.rom.setCurrentIndex(d.rom.findData("DZ1E402B"))   # or named
    assert d.job().options["rom"] == "DZ1E402B"
    d.sources.selectAll()
    d._remove_selected()
    assert d.rom_box.isHidden() and "rom" not in d.job().options


def test_watch_a_folder(app, tmp_path):
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.target.setCurrentIndex(d.target.findData("hex"))
    w = d.open_watch()
    assert "A binary that arrives: converted to Intel HEX" in w.what.text()
    inbox = tmp_path / "in"
    inbox.mkdir()
    w.folder.setText(str(inbox))
    w.toggle()
    assert w.running() and not w.folder.isEnabled()
    (inbox / "car.bin").write_bytes(bytes(0x400))
    t = time.time()
    while w.log.count() == 0 and time.time() - t < 20:
        app.processEvents()
        time.sleep(0.05)
    assert w.log.count() == 1 and "car.bin" in w.log.item(0).text()
    assert (inbox / "Converted" / "car.hex").is_file()
    d.close()                                   # closing the converter stops the watch
    assert not w.running()


def test_more_menu_offers_the_shell_menu_on_this_system(app):
    from ovtsuite.convert import shellmenu
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    assert d.act_watch.isVisible()
    assert d.act_shell.isVisible() == shellmenu.supported()


def test_files_from_a_second_start_join_the_open_window(app, tmp_path):
    """A right-click on several files starts the converter once per file;
    each start hands its file to the window already open."""
    import subprocess
    import sys
    from ovtsuite import gui_instance as gi
    from ovtsuite.gui_convert import ConverterDialog
    name = "ovt-conv-test-%d" % os.getpid()
    assert gi.server_name("converter") != gi.server_name()
    d = ConverterDialog(None)
    gi.listen(d, lambda p: d.add_sources([p]), name=name)
    files = []
    for n in ("a.bin", "b.bin"):
        p = tmp_path / n
        p.write_bytes(bytes(0x100))
        files.append(str(p))
    code = ("import sys; from PySide6 import QtCore; app = QtCore.QCoreApplication([]); "
            "from ovtsuite import gui_instance as gi; "
            "sys.exit(0 if gi.hand_over(%r, name=%r) else 3)" % (files, name))
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT)
    t = time.time()
    while proc.poll() is None and time.time() - t < 20:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    assert proc.returncode == 0
    assert sorted(d.source_paths()) == sorted(files)


# ------------------------------------------- embedded binaries, window shape
M235 = os.path.join(ROOT, "Reference", "WinOLS-formats", "f440_M235i_translated_axis_texts.ols")


def test_ols_binary_written_beside_the_conversion(app, tmp_path):
    """A WinOLS project's binary is a ticked output on the Files tab, its
    name filled in beside the file saved; its versions are on the Project
    binaries tab, unticked. The binary alone can be written (the
    conversion unticked)."""
    if not os.path.isfile(M235):
        pytest.skip("reference file missing")
    from ovtsuite import convert as C
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([M235])
    assert "embeds a 4 MB binary + 2 versions" in d.sources.item(0).text()
    assert "embedded in" in d.binary.placeholderText()
    assert C.EXTRACT not in [d.target.itemData(i) for i in range(d.target.count())]
    assert d.bin_on.isEnabled() and d.bin_on.isChecked()
    assert d.bin_out.text().endswith("f440_M235i_translated_axis_texts.ori")
    assert not d.mpc_on.isEnabled()             # no MPC in this project
    assert d.ex_list.topLevelItemCount() == 3 and d.extract_picks() == []
    d.ex_list.topLevelItem(2).setCheckState(0, d.ex_list.topLevelItem(0).checkState(0).Checked)
    d.ex_list.topLevelItem(2).setText(2, "v2.bin")
    d.out_on.setChecked(False)
    d.out.setText(str(tmp_path / "x.xdf"))
    d.bin_out.setText(str(tmp_path / "aee.ori"))
    assert [o["key"] for o in d.embedded_outputs()] == ["original", "version2"]
    d.preview()
    assert d.table.rowCount() == 2
    d.convert()
    _wait(app, d)
    assert sorted(n for n in os.listdir(str(tmp_path)) if n != "cfg") ==         ["aee.ori", "v2.bin"]
    assert os.path.getsize(str(tmp_path / "aee.ori")) == 0x400000
    assert 0 < d.progress.value() == d.progress.maximum()      # done


EDC16_OLS = os.path.join(ROOT, "Reference", "A2L", "2007_VW_DAMOS_Golf_Original_"
                         "03G906016B__5645_0281011478_369564_BKC_1.9_104.7PS.bin.ols")


def test_edc16_ols_gives_its_binary_and_mpc(app, tmp_path):
    """An EDC16 project stores the original as two areas, the MPC's 448 KB
    and the 1 MB flash: they are the Binary and MPC outputs, named
    <project>.bin / <project>.mpc."""
    if not os.path.isfile(EDC16_OLS):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([EDC16_OLS])
    stem = os.path.splitext(os.path.basename(EDC16_OLS))[0]
    assert d.bin_on.isChecked() and d.mpc_on.isChecked()
    assert os.path.basename(d.bin_out.text()) == stem + ".bin"
    assert os.path.basename(d.mpc_out.text()) == stem + ".mpc"
    d.out_on.setChecked(False)
    d.bin_out.setText(str(tmp_path / "flash.bin"))
    d.mpc_out.setText(str(tmp_path / "mpc.bin"))
    d.mpc_on.setChecked(False)                  # each on its own
    assert [o["key"] for o in d.embedded_outputs()] == ["original-area2"]
    d.mpc_on.setChecked(True)
    d.convert()
    _wait(app, d)
    assert os.path.getsize(str(tmp_path / "flash.bin")) == 0x100000
    assert os.path.getsize(str(tmp_path / "mpc.bin")) == 0x70000


def test_the_preview_starts_under_the_output_group(app):
    """By default the tabs are only as tall as the Files tab's Input and
    Output groups: the preview starts right under the Output group, and
    has the rest. A splitter saved under the old key (the tabs taller) is
    not restored; one saved since is."""
    from ovtsuite import appsettings
    from ovtsuite.gui_convert import ConverterDialog
    from PySide6 import QtCore, QtWidgets as W

    def gap(d):
        out = [g for g in d.tabs.widget(0).findChildren(W.QGroupBox)
               if g.title() == "Output"][0]
        below = out.mapTo(d, QtCore.QPoint(0, out.height())).y()
        return d.split.widget(1).mapTo(d, QtCore.QPoint(0, 0)).y() - below

    d = ConverterDialog(None)
    d.show()
    app.processEvents()
    assert d.split.sizes()[0] == d._files_height()
    assert 0 <= gap(d) <= 30                     # the tab frame and the handle
    # an old layout: the tabs 200 px taller than they need
    top, prev = d.split.sizes()
    d.split.setSizes([top + 200, max(80, prev - 200)])
    st = appsettings.settings()
    st.setValue("convert/split", d.split.saveState())
    st.setValue("convert/geometry", d.saveGeometry())
    st.sync()
    d.hide()                                     # (closing would save the new key)
    d2 = ConverterDialog(None)
    d2.show()
    app.processEvents()
    assert 0 <= gap(d2) <= 30
    d2.split.setSizes([d2.split.sizes()[0] + 40, d2.split.sizes()[1] - 40])
    want = d2.split.sizes()
    d2.close()                                   # kept under the new key
    d3 = ConverterDialog(None)
    d3.show()
    app.processEvents()
    assert d3.split.sizes() == want
    d3.close()


def test_the_window_keeps_its_shape(app):
    """Every target format, the optional groups ticked and unticked, port,
    a long status and summary: the window, its splitter and each section
    keep their size."""
    from ovtsuite.gui_convert import ConverterDialog
    from PySide6 import QtWidgets as W
    d = ConverterDialog(None)
    if os.path.isfile(M235):
        d.add_sources([M235])
    d.resize(900, 700)
    d.show()
    app.processEvents()
    groups = d.tabs.widget(0).findChildren(W.QGroupBox)

    def shape():
        app.processEvents()
        return ((d.width(), d.height()), tuple(d.split.sizes()), d.minimumSizeHint().width(),
                tuple(g.height() for g in groups), d.tabs.height(), d.opts.height())
    first = shape()
    for i in range(d.target.count()):
        d.target.setCurrentIndex(i)
        assert shape() == first, d.target.itemData(i)
    for box in (d.filter_box, d.tr_box, d.port):
        box.setChecked(True)
        assert shape() == first
        box.setChecked(False)
        assert shape() == first
    d.status.setText("Preset saved. On the command line: ovtsuite convert <files> "
                     "--preset \"C:/Users/someone/AppData/Roaming/OVT/presets/x.json\"" * 2)
    d._set_summary("; ".join(["7290 tables will be written"] * 12))
    assert shape() == first
    d.close()


def test_tables_tab_lists_matches_and_fills_size_and_offset(app):
    """The files are read in the background: the image size and offset fill
    in (and are not passed on as given), and the Tables tab lists the tables
    a filter matches -- with wildcards, or text found anywhere."""
    if not os.path.isfile(OLS):
        pytest.skip("reference file missing")
    from ovtsuite.gui_convert import ConverterDialog
    d = ConverterDialog(None)
    d.add_sources([OLS])
    d.target.setCurrentIndex(d.target.findData("xdf"))
    d._read_info()
    t = time.time()
    while d._info is None and time.time() - t < 120:
        app.processEvents()
        time.sleep(0.02)
    assert d._info is not None
    assert d.size.text().startswith("0x") and d.base.text().startswith("0x")
    j = d.job()
    assert j.size is None and j.base is None          # what it works out anyway
    total = d.match_list.count()
    assert total > 1000
    d.filter_box.setChecked(True)
    d.pattern.setText("anw*_MAX")
    d._fill_matches()
    n = d.match_list.count()
    assert 0 < n < total and all(d.match_list.item(i).text().lower().startswith("anw")
                                 and "_max" in d.match_list.item(i).text().lower()
                                 for i in range(n))
    d.pattern.setText("_MAX")                         # no wildcard: anywhere
    d._fill_matches()
    assert d.match_list.count() >= n
    assert d.job().filters.get("wildcard") is True
    d.pattern_kind.setCurrentIndex(d.pattern_kind.findData("regex"))
    d.pattern.setText("(")
    d._fill_matches()
    assert "Not a valid pattern" in d.match_count.text()
    d.close()
