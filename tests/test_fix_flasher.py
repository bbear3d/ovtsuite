# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Regression tests for the ECU flasher fixes (gui_flash.FlashDialog):

* ZF6HP: an image whose calibration checksum layout cannot be read is never
  written silently -- the user is asked (default No).
* Climatronic: a write sends the image as open in the app (unsaved edits
  included), and its confirmation does not promise a resume the GUI lacks.
* ZF6HP with a 6HP28 file (UDS): a note that covers the 6HP28, and a
  tooltip / confirmation that do not refer to the hidden, ineffective Force
  full write. (The separate ZF6HP-UDS entry was merged into ZF6HP; see
  test_fix_zf6hp_unified.py.)
* ABS: its own note, and Log CAN traffic / Probe the bus actually reach the run.
* Every CAN-only ECU (and every K-line-only one) keeps the ECU / transport pair
  valid in both directions.
* Aisin TRx: the note says writing is not available; the GUI still refuses
  a write.
* EDC16: the diagnostic probes live in the Advanced section, so the dialog is
  no longer ~2100 px wide.

Offscreen Qt, guarded like test_gui_flasher_entry.py.
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

from ovtsuite.flash import gui_flash  # noqa: E402
from ovtsuite.flash.transport import KLineError  # noqa: E402

pytestmark = pytest.mark.slow

ROOT = os.path.join(os.path.dirname(__file__), "..")
HVAC_REF = os.path.join(ROOT, "Reference", "Binaries",
                        "4F0910043B___0080-hotseats-code12fix-v2.hvac")
ZF6HP_REF = os.path.join(ROOT, "Reference", "ZF6HP", "OEM", "4F1910156A__0030.sgo.bin")


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


class _Window(QtWidgets.QWidget):
    _eeprom = None

    def __init__(self, primary=None):
        super().__init__()
        self.primary = primary


def _dialog(primary=None, ecu=None):
    d = gui_flash.FlashDialog(_Window(primary))
    if ecu:
        d.ecu_box.setCurrentText(ecu)
    return d


class _Questions:
    """Stands in for QMessageBox.question: records every text, answers in turn."""

    def __init__(self, *answers):
        self.texts = []
        self._answers = list(answers)

    def __call__(self, parent, title, text, *a, **k):
        self.texts.append((title, text))
        yes = self._answers.pop(0) if self._answers else True
        return (QtWidgets.QMessageBox.StandardButton.Yes if yes
                else QtWidgets.QMessageBox.StandardButton.No)


class _FakeWorker(QtCore.QObject):
    """Captures what _start would hand the worker thread; runs nothing."""
    stage = QtCore.Signal(str)
    percent = QtCore.Signal(int)
    finished_ok = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    last = None

    def __init__(self, op, ecu, port, speed, **kw):
        super().__init__()
        self.args = dict(op=op, ecu=ecu, port=port, **kw)
        _FakeWorker.last = self

    def start(self):
        pass


@pytest.fixture
def no_worker(monkeypatch):
    _FakeWorker.last = None
    monkeypatch.setattr(gui_flash, "_Worker", _FakeWorker)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: pytest.fail("unexpected error box: %r" % (a[2:],)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: pytest.fail("unexpected warning box: %r" % (a[2:],)))
    return _FakeWorker


def _with_port(d):
    """One dummy device, so _start gets past 'Select a device'."""
    d.port_box.blockSignals(True)
    d.port_box.clear()
    d.port_box.addItem("dummy", "dummy-port")
    d.port_box.setCurrentIndex(0)
    d.port_box.blockSignals(False)


# ---- ECU / transport always consistent (bug 17) -----------------------------

@pytest.mark.parametrize("ecu", gui_flash.CAN_ONLY_ECUS)
def test_can_only_ecu_leaves_when_transport_goes_back_to_kline(app, ecu):
    d = _dialog(ecu=ecu)
    assert d._ecu() == ecu and d._is_can()
    d.transport_box.setCurrentIndex(d.transport_box.findData("kline"))
    assert not d._is_can()
    assert d._ecu() == "EDC16"                 # the one entry valid on both


@pytest.mark.parametrize("ecu", gui_flash.KLINE_ONLY_ECUS)
def test_kline_only_ecu_switches_transport_to_kline(app, ecu):
    d = _dialog(ecu="EDC16")
    d.transport_box.setCurrentIndex(d.transport_box.findData("can"))
    assert d._is_can()
    d.ecu_box.setCurrentText(ecu)
    assert d._ecu() == ecu and not d._is_can()


def test_every_ecu_entry_has_a_transport_rule(app):
    d = _dialog()
    entries = [d.ecu_box.itemText(i) for i in range(d.ecu_box.count())]
    both = set(entries) - set(gui_flash.CAN_ONLY_ECUS) - set(gui_flash.KLINE_ONLY_ECUS)
    assert both == {"EDC16"}


# ---- ZF6HP: unreadable checksum layout is asked about (bug 4) ----------------

def _zf_primary(data):
    return types.SimpleNamespace(platform="zf6hp", binfile=types.SimpleNamespace(
        data=bytearray(data)), correct_program_area=False)


def test_zf6hp_unreadable_layout_asks_and_no_cancels(app, monkeypatch):
    raw = bytes(gui_flash._zf6hp.IMAGE_SIZE)             # no DAC/DAR descriptor
    d = _dialog(_zf_primary(raw), ecu="ZF6HP")
    if gui_flash._registry.checksum("zf6hp") is None:
        pytest.skip("ZF 6HP module not installed")
    q = _Questions(False)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    with pytest.raises(gui_flash._WriteCancelled):
        d._prepare_write_image()
    assert len(q.texts) == 1 and "cannot be read" in q.texts[0][1]


def test_zf6hp_unreadable_layout_yes_writes_as_is(app, monkeypatch):
    raw = bytes(gui_flash._zf6hp.IMAGE_SIZE)
    d = _dialog(_zf_primary(raw), ecu="ZF6HP")
    if gui_flash._registry.checksum("zf6hp") is None:
        pytest.skip("ZF 6HP module not installed")
    q = _Questions(True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    assert d._prepare_write_image() == raw
    assert len(q.texts) == 1


def test_zf6hp_readable_layout_is_corrected_without_asking(app, monkeypatch):
    if not os.path.exists(ZF6HP_REF):
        pytest.skip("reference 6HP19 image not present")
    with open(ZF6HP_REF, "rb") as fh:
        raw = fh.read()
    raw = raw + bytes(gui_flash._zf6hp.IMAGE_SIZE - len(raw))
    d = _dialog(_zf_primary(raw), ecu="ZF6HP")
    if gui_flash._registry.checksum("zf6hp") is None:
        pytest.skip("ZF 6HP module not installed")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: pytest.fail("asked for a readable layout"))
    assert len(d._prepare_write_image()) == len(raw)


# ---- Climatronic: the in-memory image, and no promised resume (bug 7) -------

def _hvac_primary():
    if not os.path.exists(HVAC_REF):
        pytest.skip("reference .hvac not present")
    from ovtsuite.flash.hvac_image import HvacImage
    him = HvacImage.load(HVAC_REF, name=os.path.basename(HVAC_REF))
    data = bytearray(him.image)
    data[0x100] ^= 0xFF                                  # an UNSAVED edit
    return types.SimpleNamespace(platform="hvac", _hvac=him, path=HVAC_REF,
                                 binfile=types.SimpleNamespace(data=data,
                                                               name=him.name))


def test_climatronic_write_sends_the_unsaved_image(app, monkeypatch, no_worker):
    prim = _hvac_primary()
    d = _dialog(prim, ecu="Climatronic")
    _with_port(d)
    q = _Questions()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *a, **k: pytest.fail("prompted for a file"))
    d._start("write")
    img = no_worker.last.args["image"]
    from ovtsuite.flash.hvac_image import HvacImage
    assert isinstance(img, HvacImage)
    assert img.image[0x100] == prim.binfile.data[0x100]          # the edit is in
    assert prim._hvac.image[0x100] != prim.binfile.data[0x100]   # session untouched
    assert img.loader == prim._hvac.loader
    confirm = [t for title, t in q.texts if title == "Write Climatronic"]
    assert confirm and "resume" not in confirm[0]
    assert "unsaved" in confirm[0]
    # the checksum was brought up to date, as Save would
    from ovtsuite.modules import registry
    cs = registry.checksum("hvac", image=img)
    if cs is not None and cs.is_supported(bytes(img.image)):
        assert cs.verify(bytes(img.image)).ok


def test_climatronic_worker_accepts_an_hvac_image(app):
    """The worker no longer insists on a path."""
    prim = _hvac_primary()
    d = _dialog(prim, ecu="Climatronic")
    img = d._loaded_hvac_image()
    w = gui_flash._Worker("write", "Climatronic", None, "Medium", image=img)
    seen = []

    class _Hvac:
        def __init__(self, tp=None, log=None):
            pass

        def open(self):
            pass

        def flash_write(self, blocks, **_kw):
            seen.append(blocks)

    orig = gui_flash._clim.Climatronic
    gui_flash._clim.Climatronic = _Hvac
    try:
        w._run_climatronic(types.SimpleNamespace(tp=types.SimpleNamespace()))
    finally:
        gui_flash._clim.Climatronic = orig
    assert seen and seen[0] == img.to_blocks(fix_checksum=False)


# ---- ZF6HP over UDS (6HP28): note, tooltip, confirmation (bug 15) ----------

def _zf28_primary(size=0x80000):
    """A 6HP28 calibration as the session identifies it (KB family zf6hp28)."""
    return types.SimpleNamespace(platform="zf6hp", family="zf6hp28",
                                 binfile=types.SimpleNamespace(
                                     data=bytearray([0x11]) * size))


def test_zf6hp_uds_note_and_tooltip(app):
    d = _dialog(_zf28_primary(), ecu="ZF6HP")
    note = d.note.text()
    assert "6HP28" in note and "0x04" not in note and "TP 2.0, dest" not in note
    assert not d.force_full.isVisibleTo(d)
    assert "Force full" not in d.write_btn.toolTip()


def test_zf6hp_uds_confirmation_matches_what_is_offered(app, monkeypatch, no_worker):
    d = _dialog(_zf28_primary(), ecu="EDC16")
    d.force_full.setChecked(True)                        # left ticked from EDC16
    d.ecu_box.setCurrentText("ZF6HP")
    _with_port(d)
    q = _Questions(False)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    d._start("write")
    text = q.texts[-1][1]
    assert q.texts[-1][0] == "6HP28 write"
    assert "Force full" not in text and "SKIPPED" not in text
    # the module cannot read this image's checksum layout: said so
    assert "neither checked nor corrected" in text


# ---- ABS: its note, and the debug options work (bug 16) ----------------------

def test_abs_note_is_its_own(app):
    d = _dialog(ecu="ABS")
    note = d.note.text()
    assert "ESP8" in note and "areas 0x04" not in note


def test_abs_probe_reaches_the_worker(app, monkeypatch, no_worker):
    d = _dialog(ecu="ABS")
    d.show()                                  # _start checks isVisible()
    _with_port(d)
    d.adv_toggle.setChecked(True)             # both live in Advanced
    assert d.probe_chk.isVisibleTo(d) and d.can_log_chk.isVisibleTo(d)
    d.probe_chk.setChecked(True)
    d._start("read")
    assert no_worker.last.args["probe"] is True


def test_abs_can_log_reaches_the_worker(app, monkeypatch, no_worker, tmp_path):
    d = _dialog(ecu="ABS")
    d.show()
    _with_port(d)
    d.adv_toggle.setChecked(True)
    d.can_log_chk.setChecked(True)
    log = str(tmp_path / "abs.log")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (log, ""))
    d._start("read")
    assert no_worker.last.args["can_log"] == log


# ---- Aisin TRx: the note's checksum claim, and the write refusal (bug 18) ----

def test_aisin_note_says_09d_checksum_is_solved(app):
    d = _dialog(ecu="Aisin TRx")
    note = d.note.text()
    assert "not confirmed" not in note and "simulator" not in note
    assert "not available" in note and "TR-80SD" in note
    assert not d.write_btn.isEnabled()


def test_aisin_worker_refuses_write(app):
    w = gui_flash._Worker("write", "Aisin TRx", None, "Medium", image=b"\0" * 16)
    with pytest.raises(KLineError):
        w._run_aisin09d(types.SimpleNamespace(tp=types.SimpleNamespace()))


# ---- EDC16: diagnostics in Advanced, dialog no longer ~2100 px (bug 44) -----

_DIAG = ("prog_cksumprobe_btn", "prog_seedprobe_btn", "prog_ramread_btn",
         "prog_immoread_btn", "prog_findeep_btn", "prog_gptverify_btn",
         "prog_gptverify_find_btn", "gpt_crank_only_chk", "gpt_gap_chk",
         "gpt_force_ee_chk")


def test_edc16_diagnostics_are_in_advanced(app):
    d = _dialog(ecu="EDC16")
    for name in _DIAG:
        w = getattr(d, name)
        assert d.adv_panel.isAncestorOf(w), name
        assert not w.isVisibleTo(d), name                # collapsed
    for name in ("read_btn", "write_btn", "prog_arm_btn", "prog_readfull_btn"):
        assert getattr(d, name).isVisibleTo(d), name
    assert d.sizeHint().width() < 1200
    d.adv_toggle.setChecked(True)
    for name in _DIAG:
        assert getattr(d, name).isVisibleTo(d), name
    assert d.sizeHint().width() < 1200


def test_edc16_diagnostics_stay_hidden_for_other_ecus(app):
    d = _dialog(ecu="EDC15")
    d.adv_toggle.setChecked(True)
    for name in _DIAG:
        assert not getattr(d, name).isVisibleTo(d), name
    d.ecu_box.setCurrentText("EDC16")
    for name in _DIAG:
        assert getattr(d, name).isVisibleTo(d), name
    d.ecu_box.setCurrentText("ZF6HP")
    for name in _DIAG:
        assert not getattr(d, name).isVisibleTo(d), name


# ---- the cleaned-up dialog (owner-approved text cleanup, 2026-10-11) --------

def test_climatronic_offers_write_only(app, monkeypatch, no_worker):
    d = _dialog(ecu="Climatronic")
    assert not d.read_btn.isVisibleTo(d) and d.write_btn.isVisibleTo(d)
    d.adv_toggle.setChecked(True)          # nothing of the Climatronic's in Advanced
    assert not d.probe_chk.isVisibleTo(d) and not d.can_log_chk.isVisibleTo(d)
    _with_port(d)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[2]))
    d._start("read")
    assert warned and no_worker.last is None


def test_removed_probes_are_gone(app):
    d = _dialog(ecu="ZF6HP")
    for name in ("zf_areaprobe_btn", "clim_test_btn", "clim_readprobe_btn"):
        assert not hasattr(d, name)


def test_probe_keeps_advanced_open(app):
    """Probe the bus changes what Read does, so it is never armed out of sight."""
    d = _dialog(ecu="ABS")
    d.adv_toggle.setChecked(True)
    d.probe_chk.setChecked(True)
    d.adv_toggle.setChecked(False)
    assert d.adv_toggle.isChecked() and d.probe_chk.isVisibleTo(d)


@pytest.mark.parametrize("ecu", ["EDC16", "EDC15", "ZF6HP"])
def test_short_labels_details_in_tooltips(app, ecu):
    d = _dialog(ecu=ecu)
    assert d.force_full.text() == "Force full write" and d.force_full.toolTip()
    assert "(" not in d.staged_chk.text()


@pytest.mark.parametrize("ecu,offered", [
    ("EDC16", True), ("EDC15", True), ("Cluster", True), ("ABS", True),
    ("Climatronic", False), ("ZF6HP", True),
    ("Aisin TRx", False), (gui_flash.VDO_ECU, False)])
def test_advanced_only_when_it_has_something(app, ecu, offered):
    d = _dialog(ecu=ecu)
    assert d.adv_toggle.isVisibleTo(d) is offered


def test_calibration_only_sits_with_the_other_options(app):
    d = _dialog(_zf28_primary(), ecu="ZF6HP")
    lay = d.layout()
    assert lay.indexOf(d.uds_cal_only_chk) == lay.indexOf(d.force_full) + 1
    assert d.uds_cal_only_chk.isVisibleTo(d) and not d.force_full.isVisibleTo(d)


# ---- EDC16 full read reaches the app (or the standalone save prompt) --------

class _ReadWindow(_Window):
    """Records what a finished read hands the window."""

    def __init__(self):
        super().__init__()
        self.loaded = []

    def load_from_image(self, data, name, *a, **k):
        self.loaded.append((bytes(data), name))
        return True


def test_an_edc16_full_read_is_handed_to_the_window(app, monkeypatch):
    """A full read (external flash + MPC) goes to load_from_image -- the app
    opens it in a project, the standalone flasher asks where to save it.
    Building the container named a constant edc16_container no longer
    exports, and the AttributeError in this slot meant neither happened."""
    from ovtsuite import container as _c
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
    win = _ReadWindow()
    d = gui_flash.FlashDialog(win)
    ext, mpc = b"\x11" * 0x200000, b"\x22" * 0x80000
    d._done("read_program_full", {"external": ext, "internal": mpc})
    assert len(win.loaded) == 1, "the read never reached the window"
    data, name = win.loaded[0]
    assert name.startswith("edc16-full-")
    got = _c.Container.from_bytes(data)
    assert got.external() == ext
    assert [b.data for b in got.blocks if b.type == _c.BLOCK_MPC] == [mpc]



# ---- every write: checksums corrected as Save does, and checked ------------
class _FakeCs:
    """A checksum provider that records the options it was given."""

    def __init__(self, ok=True, fail=None):
        self.ok, self.fail, self.opts = ok, fail, []

    def is_supported(self, data):
        return True

    def correct(self, data, **opts):
        self.opts.append(dict(opts))
        if self.fail:
            raise ValueError(self.fail)
        return bytes(data[:-1]) + b"\x5A"

    def verify(self, data, **opts):
        ok = self.ok
        return types.SimpleNamespace(ok=ok, describe=lambda: "block 7: bad")


LQ_REF = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")


def _edc16_primary(digest=True):
    if not os.path.exists(LQ_REF):
        pytest.skip("reference 03G906016LQ.bin not present")
    with open(LQ_REF, "rb") as fh:
        data = fh.read()
    p = types.SimpleNamespace(platform="edc16", binfile=types.SimpleNamespace(
        data=bytearray(data)), correct_program_area=False)
    p._checksum_opts = lambda: {"digest": digest, "program_area": False}
    return p


def test_edc16_write_corrects_with_the_files_own_options(app, monkeypatch):
    """The EDC16 write corrects the checksums with the file's options -- the
    content digest included -- as Save does, and checks the result: without
    the digest the ECU refused every block (EDC16Checksumfailwritefail.txt)."""
    cs = _FakeCs()
    monkeypatch.setattr(gui_flash._registry, "checksum", lambda pid, *a, **k: cs)
    d = _dialog(_edc16_primary(), ecu="EDC16")
    out = d._prepare_write_image()
    assert cs.opts and cs.opts[0].get("digest") is True
    assert out[-1:] == b"\x5A"                       # the corrected copy is written


def test_a_write_whose_checksums_cannot_be_made_right_does_not_start(app, monkeypatch):
    d = _dialog(_edc16_primary(), ecu="EDC16")
    monkeypatch.setattr(gui_flash._registry, "checksum",
                        lambda pid, *a, **k: _FakeCs(fail="no checksum layout"))
    with pytest.raises(RuntimeError) as ei:
        d._prepare_write_image()
    assert "could not be corrected" in str(ei.value) and "no checksum layout" in str(ei.value)
    class _Unsettled(_FakeCs):
        n = 0

        def correct(self, data, **opts):
            _Unsettled.n += 1
            return bytes(data[:-1]) + bytes([_Unsettled.n & 0xFF])
    monkeypatch.setattr(gui_flash._registry, "checksum",
                        lambda pid, *a, **k: _Unsettled())
    with pytest.raises(RuntimeError) as ei:
        d._prepare_write_image()
    assert "corrected twice" in str(ei.value)
    # a report that never reads "all valid" (an edited EDC16's Bosch RSA
    # signature) does not stop the write: the correction settled
    monkeypatch.setattr(gui_flash._registry, "checksum",
                        lambda pid, *a, **k: _FakeCs(ok=False))
    assert d._prepare_write_image()[-1:] == bytes([0x5A])


def test_without_the_module_the_write_asks_before_anything_connects(app, monkeypatch):
    monkeypatch.setattr(gui_flash._registry, "checksum", lambda pid, *a, **k: None)
    d = _dialog(_edc16_primary(), ecu="EDC16")
    q = _Questions(False)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    with pytest.raises(gui_flash._WriteCancelled):
        d._prepare_write_image()
    assert len(q.texts) == 1 and "NOT be corrected" in q.texts[0][1]
