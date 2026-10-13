# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flasher's ZF 6HP entries merged into one (2026-10-11).

ZF6HP now covers the 6HP19/26 (KWP2000 over TP 2.0) and the 6HP28 (UDS):

* one ZF6HP entry; the old ZF6HP-UDS entry and its own buttons are gone, and a
  saved ZF6HP-UDS restores as ZF6HP (Automatic);
* Write picks the transport from the open file (zf6hp_uds.transport_for_session),
  says so when that was only a guess, and the Advanced override forces either;
* Read (Automatic) tries TP 2.0 and moves to UDS only when TP 2.0 never got a
  KWP answer -- not after any answer, not after a cancel, never when forced;
* a forced transport keeps Advanced open, and is remembered.

Offscreen Qt, fixtures modelled on test_fix_flasher.py.
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

from ovtsuite import branding  # noqa: E402
from ovtsuite.flash import gui_flash  # noqa: E402
from ovtsuite.flash.transport import KLineError, KLineTimeout  # noqa: E402

pytestmark = pytest.mark.slow

ROOT = os.path.join(os.path.dirname(__file__), "..")
ZF6HP19_REF = os.path.join(ROOT, "Reference", "ZF6HP", "OEM", "4F1910156A__0030.sgo.bin")

MB = 0x100000


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
        self.loaded = []

    def load_from_image(self, data, name):
        self.loaded.append((data, name))


def _dialog(primary=None, ecu="ZF6HP"):
    d = gui_flash.FlashDialog(_Window(primary))
    if ecu:
        d.ecu_box.setCurrentText(ecu)
    return d


def _with_port(d):
    d.port_box.blockSignals(True)
    d.port_box.clear()
    d.port_box.addItem("dummy", "dummy-port")
    d.port_box.setCurrentIndex(0)
    d.port_box.blockSignals(False)


def _primary(family=None, size=MB, meta=None):
    """A ZF 6HP session as far as the flasher looks at it."""
    cont = None
    if meta is not None:
        cont = types.SimpleNamespace(meta=lambda: dict(meta))
    return types.SimpleNamespace(
        platform="zf6hp", family=family, _container=cont, correct_program_area=False,
        binfile=types.SimpleNamespace(data=bytearray([0x11]) * size))


P19 = dict(family="zf6hp19_4e1910156a", size=MB)        # 6HP19: TP 2.0, high
P28 = dict(family="zf6hp28", size=0x80000)               # 6HP28 cal: UDS, high


class _Questions:
    def __init__(self, *answers):
        self.texts = []
        self._answers = list(answers)

    def __call__(self, parent, title, text, *a, **k):
        self.texts.append((title, text))
        yes = self._answers.pop(0) if self._answers else True
        return (QtWidgets.QMessageBox.StandardButton.Yes if yes
                else QtWidgets.QMessageBox.StandardButton.No)


class _FakeWorker(QtCore.QObject):
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


def _write(d, monkeypatch, *answers):
    _with_port(d)
    q = _Questions(*answers)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    d._start("write")
    return q


# ---- one entry ---------------------------------------------------------------

def test_one_zf6hp_entry(app):
    d = _dialog(ecu=None)
    items = [d.ecu_box.itemText(i) for i in range(d.ecu_box.count())]
    assert items.count("ZF6HP") == 1 and "ZF6HP-UDS" not in items
    assert "ZF6HP-UDS" not in gui_flash.CAN_ONLY_ECUS
    assert "ZF6HP-UDS" not in gui_flash._CAN_DEST
    for name in ("zf_uds_read_btn", "zf_uds_write_btn"):
        assert not hasattr(d, name)


def test_one_read_and_one_write_button(app):
    d = _dialog(_primary(**P28))
    assert d.read_btn.isVisibleTo(d) and d.write_btn.isVisibleTo(d)
    assert d.write_btn.isEnabled()
    assert d.read_btn.text() == "Read flash" and d.write_btn.text() == "Write flash"


def test_note_is_short_and_plain(app):
    note = _dialog().note.text()
    for word in ("6HP19", "6HP26", "6HP28", "Do not disconnect"):
        assert word in note
    assert "0x" not in note


# ---- options column ----------------------------------------------------------

@pytest.mark.parametrize("prim,force_full,cal_only", [
    (None, True, False),                       # no file: the everyday shape
    (P19, True, False),
    (P28, False, True)])
def test_options_follow_the_file(app, prim, force_full, cal_only):
    d = _dialog(_primary(**prim) if prim else None)
    assert d.force_full.isVisibleTo(d) is force_full
    assert d.uds_cal_only_chk.isVisibleTo(d) is cal_only


def test_options_follow_the_override(app):
    d = _dialog(_primary(**P19))
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("uds"))
    assert d.uds_cal_only_chk.isVisibleTo(d) and not d.force_full.isVisibleTo(d)
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("tp20"))
    assert d.force_full.isVisibleTo(d) and not d.uds_cal_only_chk.isVisibleTo(d)


def test_calibration_only_hidden_for_other_ecus(app):
    d = _dialog(_primary(**P28))
    d.ecu_box.setCurrentText("Aisin TRx")
    assert not d.uds_cal_only_chk.isVisibleTo(d)


# ---- write routing -------------------------------------------------------------

def test_write_routes_uds_for_a_6hp28(app, monkeypatch, no_worker):
    d = _dialog(_primary(**P28))
    d.uds_cal_only_chk.setChecked(True)
    q = _write(d, monkeypatch)
    a = no_worker.last.args
    assert a["op"] == "write" and a["ecu"] == "ZF6HP" and a["zf_transport"] == "uds"
    assert a["uds_cal_only"] is True and a["force_full"] is False
    assert q.texts[-1][0] == "6HP28 write"
    assert "guessed" not in q.texts[-1][1]                 # identified, not guessed


def test_write_routes_tp20_for_a_6hp19(app, monkeypatch, no_worker):
    d = _dialog(_primary(**P19))
    d.force_full.setChecked(True)
    d.uds_cal_only_chk.setChecked(True)                    # hidden: must not leak
    q = _write(d, monkeypatch)
    a = no_worker.last.args
    assert a["zf_transport"] == "tp20" and a["force_full"] is True
    assert a["uds_cal_only"] is False
    titles = [t for t, _ in q.texts]
    assert "Write to ECU" in titles and "6HP28 write" not in titles
    assert "TP 2.0" in dict(q.texts)["Write to ECU"]


def test_write_routes_tp20_for_the_6hp19_reference(app, monkeypatch, no_worker):
    """The real 6HP19 reference, through the real session identification."""
    if not os.path.exists(ZF6HP19_REF):
        pytest.skip("reference 6HP19 image not present")
    from ovtsuite.session import TunerSession
    sess = TunerSession(ZF6HP19_REF)
    d = _dialog(sess)
    assert d._zf_write_choice().transport == "tp20"
    assert d._zf_write_choice().confidence == "high"
    # the .sgo.bin is 128 bytes short of a full image (test_fix_flasher pads it
    # the same way); routing is what is tested here, not the size check
    data = sess.binfile.data
    sess.binfile.data = bytearray(data) + bytes(gui_flash._zf6hp.IMAGE_SIZE - len(data))
    _write(d, monkeypatch)
    assert no_worker.last.args["zf_transport"] == "tp20"


def test_write_routes_uds_for_our_own_6hp28_read(app, monkeypatch, no_worker):
    """A .6hp saved by the UDS read carries variant=6hp28 in its meta."""
    d = _dialog(_primary(size=0x80000, meta={"variant": "6hp28"}))
    _write(d, monkeypatch)
    assert no_worker.last.args["zf_transport"] == "uds"


def test_a_guess_is_said_in_the_confirmation(app, monkeypatch, no_worker):
    d = _dialog(_primary(size=2 * MB))                     # no identity, 2 MB
    choice = d._zf_write_choice()
    assert choice.is_uds and choice.confidence == "low"
    q = _write(d, monkeypatch, False)
    text = q.texts[-1][1]
    assert "guessed" in text and "UDS (6HP28)" in text and "Advanced" in text
    assert choice.reason in text
    assert no_worker.last is None                          # answered No


def test_a_tp20_guess_is_said_too(app, monkeypatch, no_worker):
    d = _dialog(_primary(size=MB))                         # no identity, 1 MB
    q = _write(d, monkeypatch, True, False)                # checksum yes, write no
    text = dict(q.texts)["Write to ECU"]
    assert "guessed" in text and "TP 2.0 (6HP19 / 6HP26)" in text


@pytest.mark.parametrize("prim,forced", [(P19, "uds"), (P28, "tp20")])
def test_override_forces_the_write_path(app, monkeypatch, no_worker, prim, forced):
    p = dict(prim, size=2 * MB)                            # big enough for either path
    d = _dialog(_primary(**p))
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData(forced))
    q = _write(d, monkeypatch)
    assert no_worker.last.args["zf_transport"] == forced
    assert all("guessed" not in t for _, t in q.texts)


@pytest.mark.parametrize("forced", ["auto", "tp20", "uds"])
def test_read_hands_the_choice_to_the_worker(app, monkeypatch, no_worker, forced):
    d = _dialog(_primary(**P28))
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData(forced))
    _with_port(d)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: pytest.fail("a read asks nothing"))
    d._start("read")
    a = no_worker.last.args
    assert a["op"] == "read" and a["zf_transport"] == forced


# ---- read: TP 2.0 first, UDS only when nothing answered ---------------------------

class _SilentTp:
    """No TCU on TP 2.0: the channel setup is never answered."""
    is_stop = None

    def open(self):
        raise KLineTimeout("TP2.0 channel setup: no response from ECU 0x02")

    def send(self, payload):
        pass

    def recv(self, timeout_ms=None):
        raise KLineTimeout("TP2.0: message receive timed out")

    def close(self):
        pass


class _RefusingTp(_SilentTp):
    """A TP 2.0 TCU that answers, but refuses the identification read."""

    def open(self):
        pass

    def recv(self, timeout_ms=None):
        return bytes([0x7F, 0x1A, 0x22])


class _Run:
    """Run a worker in this thread with its transports stubbed."""

    def __init__(self, monkeypatch, tp, transport="auto"):
        self.w = gui_flash._Worker("read", "ZF6HP", None, "Medium", transport="can",
                                   can_dev="dummy", zf_transport=transport)
        self.ok, self.err, self.stages, self.uds, self.links = [], [], [], [], []
        self.w.finished_ok.connect(self.ok.append)
        self.w.failed.connect(self.err.append)
        self.w.stage.connect(self.stages.append)

        def _open_link():
            self.links.append(tp)
            return types.SimpleNamespace(tp=tp, close=lambda: None)

        def _uds_read():
            self.uds.append(True)
            self.w.finished_ok.emit({"_uds_read": True, "cal": b"\x11" * 16,
                                     "base": 0x180000})

        monkeypatch.setattr(self.w, "_open_link", _open_link)
        monkeypatch.setattr(self.w, "_run_zf6hp_read", _uds_read)

    def __call__(self):
        self.w.run()
        return self


def test_auto_read_falls_back_to_uds_when_tp20_never_answers(app, monkeypatch):
    r = _Run(monkeypatch, _SilentTp())()
    assert r.links and r.uds and not r.err
    assert r.ok and r.ok[0]["_uds_read"]
    assert any("Trying UDS" in s for s in r.stages)


def test_auto_read_stays_on_tp20_after_an_answer(app, monkeypatch):
    r = _Run(monkeypatch, _RefusingTp())()
    assert not r.uds and not r.ok
    assert r.err and "readEcuIdentification" in r.err[0]


def test_auto_read_does_not_fall_back_after_a_cancel(app, monkeypatch):
    r = _Run(monkeypatch, _SilentTp())
    r.w.request_stop()
    r()
    assert not r.uds and r.err


def test_auto_read_does_not_fall_back_on_a_bug(app, monkeypatch):
    """Only a transport failure means 'no TP 2.0 TCU here'."""
    class _Broken(_SilentTp):
        def open(self):
            raise AttributeError("oops")
    r = _Run(monkeypatch, _Broken())()
    assert not r.uds and r.err and "oops" in r.err[0]


def test_forced_tp20_read_never_falls_back(app, monkeypatch):
    r = _Run(monkeypatch, _SilentTp(), transport="tp20")()
    assert not r.uds and r.err and "no response" in r.err[0]


def test_forced_uds_read_skips_tp20(app, monkeypatch):
    r = _Run(monkeypatch, _SilentTp(), transport="uds")()
    assert r.uds and not r.links and not r.err


def test_both_failing_says_both(app, monkeypatch):
    r = _Run(monkeypatch, _SilentTp())

    def _uds_fail():
        raise KLineError("UDS: no response from 0x7E9")
    monkeypatch.setattr(r.w, "_run_zf6hp_read", _uds_fail)
    r()
    assert r.err and "TP 2.0" in r.err[0] and "UDS: no response" in r.err[0]


# ---- read results: each path keeps its own handling --------------------------------

def test_uds_read_result_goes_into_a_project(app, monkeypatch):
    """A 6HP28 calibration read is a read like any other: handed to the window
    (a project there; the standalone flasher saves it as an .ovt) as a full
    2 MB image, 0xFF below the calibration at its flash address."""
    from ovtsuite.flash import zf6hp_uds
    from ovtsuite import container as C
    d = _dialog()
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    d._done("read", {"_uds_read": True, "cal": b"\x11" * 16, "base": 0x180000})
    assert d._window.loaded
    blob = d._window.loaded[0][0]
    c = C.Container.from_bytes(blob)
    img = c.external()
    assert c.kind == "zf6hp" and len(img) == zf6hp_uds.IMAGE_SIZE == 0x200000
    assert img[0x180000:0x180010] == b"\x11" * 16
    assert img[:0x180000] == b"\xFF" * 0x180000 and img[0x180010:] == b"\xFF" * (len(img) - 0x180010)
    assert "UDS" in d.stage.text()


def test_tp20_read_result_opens_as_a_new_file(app):
    d = _dialog()
    d._done("read", b"\x00" * 16)
    assert d._window.loaded and d._window.loaded[0][0] == b"\x00" * 16
    assert "TP 2.0" in d.stage.text()


# ---- settings and the Advanced section ----------------------------------------------

def _save_flat(**values):
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.beginGroup("flash")
    for k, v in values.items():
        s.setValue(k, v)
    s.endGroup()
    s.sync()


def test_saved_zf6hp_uds_restores_as_zf6hp_automatic(app):
    _save_flat(ecu="ZF6HP-UDS", transport="can", zf6hp_transport="uds")
    d = gui_flash.FlashDialog(_Window(None))
    assert d._ecu() == "ZF6HP" and d._is_can()
    assert d._zf_override() == "auto"


def test_the_override_is_remembered(app):
    d = _dialog()
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("uds"))
    d._save_settings()
    again = gui_flash.FlashDialog(_Window(None))
    assert again._ecu() == "ZF6HP" and again._zf_override() == "uds"
    assert again.adv_toggle.isChecked()                    # forced: shown, not hidden
    assert again.zf_transport_row.isVisibleTo(again)


def test_advanced_offers_the_override_for_zf6hp_only(app):
    d = _dialog()
    assert d.adv_toggle.isVisibleTo(d)
    d.adv_toggle.setChecked(True)
    assert d.zf_transport_row.isVisibleTo(d)
    assert d.zf_transport_box.currentData() == "auto"
    assert [d.zf_transport_box.itemText(i) for i in range(d.zf_transport_box.count())] \
        == ["Automatic", "TP 2.0 (6HP19 / 6HP26)", "UDS (6HP28)"]
    d.ecu_box.setCurrentText("EDC16")
    assert not d.zf_transport_row.isVisibleTo(d)


def test_advanced_will_not_collapse_while_forced(app):
    d = _dialog()
    d.adv_toggle.setChecked(True)
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("tp20"))
    d.adv_toggle.setChecked(False)
    assert d.adv_toggle.isChecked() and d.zf_transport_row.isVisibleTo(d)
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("auto"))
    d.adv_toggle.setChecked(False)
    assert not d.adv_toggle.isChecked()


def test_forcing_from_code_opens_advanced(app):
    d = _dialog()
    assert not d.adv_toggle.isChecked()
    d.zf_transport_box.setCurrentIndex(d.zf_transport_box.findData("uds"))
    assert d.adv_toggle.isChecked()
