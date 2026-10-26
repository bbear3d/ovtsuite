# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Injector setup (PD): your presets (save, edit, rename, delete,
asked before one is replaced, kept selected), every dataset identified,
"Nozzles only" following the preset kind, and double-click to a table."""
import contextlib
import io
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
GQ = os.path.join(BINARIES, "EDC15", "038906019GQ.bin")

Yes = QtWidgets.QMessageBox.StandardButton.Yes
No = QtWidgets.QMessageBox.StandardButton.No


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


_sessions = {}


def _load(path):
    if not os.path.exists(path):
        pytest.skip("reference binary missing: %s" % os.path.basename(path))
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return TunerSession(path)


def _dialog(path, host=None):
    from ovtsuite import gui_injectors
    _app()
    s = _load(path)
    host = host or QtWidgets.QMainWindow()
    ws = SimpleNamespace(primary=s, comparison=None, window=lambda: host)
    return gui_injectors.InjectorDialog(ws, host), s, host


def _fill(monkeypatch, **values):
    """The next InjectorPresetDialog: these values typed, then OK."""
    from ovtsuite import gui_injectors

    def exec_(dlg):
        for k, v in values.items():
            w = getattr(dlg, k)
            if isinstance(w, QtWidgets.QLineEdit):
                w.setText(v)
            elif isinstance(w, QtWidgets.QPlainTextEdit):
                w.setPlainText(v)
            elif isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(v)
            elif isinstance(w, QtWidgets.QComboBox):
                w.setCurrentIndex(v)
        return True
    monkeypatch.setattr(gui_injectors.InjectorPresetDialog, "exec", exec_)


def _answer(monkeypatch, reply):
    asked = []

    def q(*a, **k):
        asked.append(a[2] if len(a) > 2 else "")
        return reply
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", q)
    return asked


def test_your_presets_save_edit_rename_delete(monkeypatch):
    from ovtsuite import gui_injectors
    from ovtsuite import injectors as I
    d, _s, _h = _dialog(LQ)
    infos = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: infos.append(a[2]))
    # factory presets are read-only
    d.preset.setCurrentIndex(d.preset.findData("bhw"))
    assert not d.preset_del.isEnabled() and not d.preset_rename.isEnabled()
    assert not d.preset_edit.isEnabled()
    # save: kept selected afterwards, with its notes and parts
    _fill(monkeypatch, name="My LQ", notes="fitted 2026", nozzle="DSLA150P1043")
    d._save_preset()
    assert d.current_preset().id == "user-my-lq"
    p = d.current_preset()
    assert p.kind == I.USER and p.notes == "fitted 2026" and p.nozzle == "DSLA150P1043"
    assert d.preset_del.isEnabled() and d.preset_rename.isEnabled()
    assert [u.id for u in gui_injectors.user_presets()] == ["user-my-lq"]
    # the same name again: asked first; No keeps the old one
    asked = _answer(monkeypatch, No)
    _fill(monkeypatch, name="my lq", notes="other")
    d._save_preset()
    assert asked and "already have a preset named My LQ" in asked[0]
    assert gui_injectors.user_presets()[0].notes == "fitted 2026"
    # Yes replaces it (same id: project records name presets by id)
    _answer(monkeypatch, Yes)
    d._save_preset()
    us = gui_injectors.user_presets()
    assert len(us) == 1 and us[0].id == "user-my-lq" and us[0].notes == "other"
    assert d.current_preset().id == "user-my-lq"
    # rename keeps the id and the selection
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        lambda *a, **k: ("LQ nozzles", True))
    d._rename_preset()
    us = gui_injectors.user_presets()
    assert [(u.id, u.name) for u in us] == [("user-my-lq", "LQ nozzles")]
    assert d.current_preset().name == "LQ nozzles"
    assert d.preset.currentText().strip() == "LQ nozzles"
    # edit: notes and body, the calibration kept
    char = us[0].char.to_json()
    _fill(monkeypatch, notes="edited", body="0414720215")
    d._edit_preset()
    p = gui_injectors.user_presets()[0]
    assert p.notes == "edited" and p.body == "0414720215" and p.name == "LQ nozzles"
    assert p.char.to_json() == char and d.current_preset().id == "user-my-lq"
    # a second preset; renaming onto the first's name is refused
    _fill(monkeypatch, name="Second")
    d._save_preset()
    assert d.current_preset().id == "user-second"
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        lambda *a, **k: ("lq nozzles", True))
    d._rename_preset()
    assert infos and "Choose another name" in infos[-1]
    assert {u.name for u in gui_injectors.user_presets()} == {"LQ nozzles", "Second"}
    # delete asks; No keeps it, Yes removes it
    asked = _answer(monkeypatch, No)
    d._delete_preset()
    assert "Delete your preset Second?" in asked[0]
    assert len(gui_injectors.user_presets()) == 2
    _answer(monkeypatch, Yes)
    d._delete_preset()
    assert [u.id for u in gui_injectors.user_presets()] == ["user-my-lq"]
    assert d.preset.findData("user-second") < 0 and d.current_preset() is not None
    d.close()


def test_nozzles_only_follows_the_preset_kind():
    d, _s, _h = _dialog(LQ)
    assert d.r_body.isChecked()
    d.preset.setCurrentIndex(d.preset.findData("firad-hf50"))
    assert d.r_noz.isChecked()                   # aftermarket: nozzles only
    d.preset.setCurrentIndex(d.preset.findData("bhw"))
    assert d.r_body.isChecked()                  # back to a factory set
    # chosen by hand: it stays
    d.preset.setCurrentIndex(d.preset.findData("firad-hf50"))
    d.r_body.setChecked(True)
    d.r_noz.setChecked(True)
    d.preset.setCurrentIndex(d.preset.findData("bhw"))
    assert d.r_noz.isChecked()
    # the estimated Wuzetem says it is assumed to deliver what its base does
    d.preset.setCurrentIndex(d.preset.findData("wuzetem-1043"))
    assert "Assumed to deliver" in d.info.text() and "estimated" in d.info.text()
    d.close()


def test_every_dataset_is_identified():
    d, _s, _h = _dialog(GQ)
    assert len(d.idents) == 3 and len(d.ds_boxes) == 3
    txt = d.ident.text()
    assert "All three datasets" in txt and "BHW" in txt.upper()
    assert "not set up for the same hardware" not in txt
    d.close()


def test_double_click_opens_the_table_and_reports():
    d, s, host = _dialog(LQ)
    shown = []
    d.ws.show_entity = lambda e: shown.append(e)
    d.preset.setCurrentIndex(d.preset.findData("bhw"))
    d._replan()
    assert d.changes.rowCount() > 0
    d._goto(0, 0)
    assert shown and "Opened" in d.goto_status.text()

    def boom(e):
        raise RuntimeError("no such table")
    d.ws.show_entity = boom
    d._goto(0, 0)
    assert "Could not open" in d.goto_status.text() and "no such table" in d.goto_status.text()
    d.close()


def _gq_logs(tmp_path, s):
    """Before / after logs of idle and a road speed (the new hardware
    delivering 1/0.9 of what is asked): native (no ident) and VCDS with the
    AG / SG ident."""
    sys.path.insert(0, os.path.dirname(__file__))
    import logtune_sim as sim
    from ovtsuite.datalog import logfile as lf
    from ovtsuite.datalog.channels import ChannelNamer

    def rows(f):
        out, t = [], 0.0
        for n, q, v in ((900, 7.0, 0.0), (1900, 15.0, 90.0)):
            for _ in range(20):
                out.append((t, {"rpm": float(n), "iq": q * f, "clt": 88.0, "vss": v,
                                "pedal": 0.0 if v == 0 else 20.0}))
                t += 0.2
        return out
    got = {}
    for name, f in (("before", 1.0), ("after", 0.9)):
        p = sim.write_log(str(tmp_path / (name + ".csv")), s, ChannelNamer(s), [1, 6], rows(f))
        got[name] = p
        for box in ("AG", "SG"):
            log = lf.load(p)
            log.identity = "038906019GQ 1,9l R4 EDC  %s  1524" % box
            q = str(tmp_path / ("%s_%s.csv" % (name, box.lower())))
            lf.write_vcds(q, log)
            got["%s_%s" % (name, box.lower())] = q
    return got


def _learn_with(d, after, before):
    for lst in (d.after_list, d.before_list):
        lst.clear()
    for p in after:
        d._put_log(d.after_list, p)
    for p in before:
        d._put_log(d.before_list, p)
    d._replan()
    return [d.findings.item(i).text() for i in range(d.findings.count())]


def test_from_logs_follows_each_logs_ident(tmp_path):
    """As injector_learn_report: a log carrying the ECU's ident is used for
    its dataset(s) only, one without for every dataset ticked -- and the log
    list and the findings say which."""
    d, s, _h = _dialog(GQ)
    lg = _gq_logs(tmp_path, s)
    d.mode.setCurrentIndex(1)
    for cb in d.ds_boxes:
        cb.setChecked(True)
    # no ident: every dataset ticked, with a note
    got = _learn_with(d, [lg["after"]], [lg["before"]])
    assert {pl.model.label for pl in d.plans} == {"Automatic", "Manual 1", "Manual 2"}
    assert any("every dataset ticked" in t for t in got)
    assert "no ECU ident" in d.after_list.item(0).toolTip()
    # an AG log: the automatic dataset only
    got = _learn_with(d, [lg["after_ag"]], [lg["before_ag"]])
    assert [pl.model.label for pl in d.plans] == ["Automatic"]
    assert any(t.startswith("Manual 1: no log") for t in got)
    assert "Used for: Automatic (its ECU ident)" in d.after_list.item(0).toolTip()
    # an SG log: both manual datasets, which share the ident
    got = _learn_with(d, [lg["after_sg"]], [lg["before_sg"]])
    assert [pl.model.label for pl in d.plans] == ["Manual 1", "Manual 2"]
    assert any("Manual 1 and Manual 2 share one ident" in t for t in got)
    # only Manual 1 ticked, an AG log: used for it, with a word
    for cb, m in zip(d.ds_boxes, d.models):
        cb.setChecked(m.label == "Manual 1")
    got = _learn_with(d, [lg["after_ag"]], [lg["before"]])
    assert [pl.model.label for pl in d.plans] == ["Manual 1"]
    assert any("ident of a dataset not ticked" in t for t in got)
    d.close()


# ------------------------------------------------------------ the window itself
BEW = os.path.join(os.path.dirname(BINARIES), "EDC16-SGO-Unpacked", "038906016AB_7989.bin")
N16 = os.path.join(BINARIES, "038906016N.bin")


def test_ok_after_apply_only_closes(monkeypatch):
    """BEW -> ARL injectors: Apply writes (asked once); OK straight after --
    before the window has planned again, and after -- closes without asking
    to write the tables again. A choice changed after Apply asks again."""
    d, s, host = _dialog(BEW)
    asked = _answer(monkeypatch, Yes)
    d.preset.setCurrentIndex(d.preset.findData("pd-al-1043"))
    d._replan()
    names = [d.changes.item(r, 0).text() for r in range(d.changes.rowCount())]
    assert any("InjCrv_phiStrt" in n for n in names)          # the start maps too
    assert d._apply() and len(asked) == 1
    rev = s.undo_stack.revision
    d._ok()                                       # the replan timer still pending
    assert len(asked) == 1 and d.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert s.undo_stack.revision == rev
    d.close()

    d, s, host = _dialog(BEW)
    asked = _answer(monkeypatch, Yes)
    d.preset.setCurrentIndex(d.preset.findData("pd-al-1043"))
    d._replan()
    assert d._apply()
    d._replan()                                   # planned again: nothing left
    assert not any(pl.n_cells for pl in d.plans)
    assert not d.apply_btn.isEnabled() and d.ok_btn.isEnabled()
    d.preset.setCurrentIndex(d.preset.findData("pd100"))    # changed since: asks
    d._ok()
    assert len(asked) == 2
    d.close()


def test_window_icon_buttons_selection_and_size():
    from ovtsuite.gui_cellmarks import CellDelegate
    from ovtsuite.gui_fit import available
    d, s, host = _dialog(LQ)
    assert not d.windowIcon().isNull()
    d.show()
    QtWidgets.QApplication.processEvents()
    # Help, Apply, Cancel, OK at the right, OK last
    row = [d.help_btn, d.apply_btn, d.cancel_btn, d.ok_btn]
    xs = [b.mapTo(d, b.rect().topLeft()).x() for b in row]
    assert xs == sorted(xs)
    others = [b for b in d.findChildren(QtWidgets.QPushButton) if b.isVisible()
              and b not in row and b.mapTo(d, b.rect().topLeft()).y()
              == d.ok_btn.mapTo(d, d.ok_btn.rect().topLeft()).y()]
    assert all(b.mapTo(d, b.rect().topLeft()).x() < xs[0] for b in others)
    # the table list selects as the table editor
    assert d.changes.alternatingRowColors()
    assert isinstance(d.changes.itemDelegate(), CellDelegate)
    # modest: fits a 1080p laptop screen at 150 % (720 logical px, less the
    # taskbar), and never larger than the screen it opens on
    assert d.minimumSizeHint().height() <= 680
    g = available(d)
    assert d.height() <= g.height() and d.width() <= g.width()
    # the size it is left at is the size it opens at
    d.resize(d.width(), d.minimumSizeHint().height() + 20)
    left = d.size()
    d.reject()
    d2, _s, _h = _dialog(LQ)
    assert d2.size() == left
    d2.close()


def test_comparison_shows_its_hardware_and_tables():
    """BEW with the N (the same PD100 hardware) as the comparison: what it is
    set up for, a Comparison column (orange where the written table differs)
    and its curve; Show comparison hides them."""
    from ovtsuite import gui
    from ovtsuite import gui_injectors
    _app()
    s, c = _load(BEW), _load(N16)
    host = QtWidgets.QMainWindow()
    ws = SimpleNamespace(primary=s, comparison=c, window=lambda: host)
    d = gui_injectors.InjectorDialog(ws, host)
    assert d.cmp_check.isVisibleTo(d)
    assert "038906016N" in d.cmp_ident.text() and "PD100" in d.cmp_ident.text()
    d.preset.setCurrentIndex(d.preset.findData("pd-al-1043"))
    d._replan()
    assert not d.changes.isColumnHidden(4)
    cells = [d.changes.item(r, 4) for r in range(d.changes.rowCount())]
    assert all(it is not None and it.text() for it in cells)
    dur = next(it for r, it in enumerate(cells)
               if d.changes.item(r, 0).text() == "InjVlv_phiInjMI1_MAP2")
    assert "differ" in dur.text() and dur.background().color() == gui.DIFF_BG
    assert d.chart.cmp
    d.cmp_check.setChecked(False)
    assert d.changes.isColumnHidden(4) and not d.chart.cmp
    assert not d.cmp_ident.isVisibleTo(d)
    d.close()
    # no comparison: no row, no column
    d, s, host = _dialog(LQ)
    assert not d.cmp_check.isVisibleTo(d) and d.changes.isColumnHidden(4)
    d.close()
