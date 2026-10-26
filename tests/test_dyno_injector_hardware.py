# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The dyno chart and recorded injectors (gui_dyno with
ovtsuite.project.hardware): a file set up for other unit injectors than the
reference is not read through the reference's duration maps."""
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

from test_gui_dyno import FakeTab, LQ, _pump  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


def _load(path):
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return TunerSession(path)


def _rec(rid, preset):
    return {"id": rid, "kind": "pd_injectors", "summary": preset,
            "parts": {"preset": preset, "complete": True}}


def test_other_injectors_take_the_quantity_as_asked(monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno as _dy
    from ovtsuite import dyno_hardware, eoi, gui_dyno
    from ovtsuite import injectors as I
    from ovtsuite.project import hardware as H
    this, ref = _load(LQ), _load(LQ)
    # the reference set up for BHW injectors: other duration maps
    cat = I.catalogue()
    p = cat.get("bhw")
    m = eoi.models(ref)[0]
    plan = I.plan_fit(ref, m, cat.char_of(p), I.FitOptions(), cat.electrical_of(p),
                      cat.extras_of(p), I.platform_for(ref))
    I.apply_fit(ref, [plan], "bhw", True)
    ws = FakeTab(this, comparison=ref)
    ws.is_dirty = lambda: False
    st = gui_dyno.state_for(ws)
    # nothing known of either file's hardware: carried across the maps
    monkeypatch.setattr(dyno_hardware, "infer", lambda *_a, **_k: None)
    st.set_reference(_dy.REF_COMPARISON)
    st._calculate()
    through = [(pt.q_ref, round(pt.torque, 6)) for pt in st.curve.points]
    st.cond.fuel_basis = "quantity"
    st._calculate()
    asked = [(pt.q_ref, round(pt.torque, 6)) for pt in st.curve.points]
    assert any(q is not None for q, _t in through) and all(q is None for q, _t in asked)
    assert through != asked              # the duration maps differ: it matters
    st.cond.fuel_basis = "duration"

    # the project says the two are set up for different injectors
    extra = {H.KEY: [_rec("a", "bpw"), _rec("b", "bhw")]}
    proj = SimpleNamespace(set=lambda sid: SimpleNamespace(extra=extra))
    ws.project_ref = SimpleNamespace(doc=SimpleNamespace(project=proj, path=""), sid="s")
    recs = {r["id"]: H.Fitted.from_dict(r) for r in extra[H.KEY]}
    monkeypatch.setattr(gui_dyno.DynoState, "_run_hardware",
                        lambda self, r, _ref, _hw: [recs["b" if r.key == "ref" else "a"]])
    st._calculate()
    assert [(pt.q_ref, round(pt.torque, 6)) for pt in st.curve.points] == asked
    assert st._fuel_own == ["This file"]
    # the same injectors: read through the reference's maps again
    monkeypatch.setattr(gui_dyno.DynoState, "_run_hardware",
                        lambda self, r, _ref, _hw: [recs["b"]])
    st._calculate()
    assert [(pt.q_ref, round(pt.torque, 6)) for pt in st.curve.points] == through
    ws.project_ref = None
    ws.close()
    _pump()


# ------------------------------------------------ each run's own hardware
class _OtherTab:
    """Another open tab of the same project (what gui_dyno reads of it)."""

    def __init__(self, project_ref, dirty=False):
        self.project_ref, self._dirty = project_ref, dirty

    def is_dirty(self):
        return self._dirty


def _project(tmp_path):
    """A project of LQ with three versions -- the original (as it came),
    "stage 1" set up for PD150 injectors, and "stage 2" with PD100 set up in
    edits not saved then (pending on it) -- saved to a file."""
    from ovtsuite import project as ovt
    from ovtsuite.project import hardware as H
    from ovtsuite.project import sessionbridge as sb
    proj, s = ovt.import_file(LQ)
    root = s.active.head
    main = sb._main_member(proj, s.id)
    data = bytearray(proj.state(s.id, root)[main.id])
    data[100] ^= 0xFF
    c1 = proj.commit(s.id, None, {main.id: bytes(data)}, label="stage 1")
    data[101] ^= 0xFF
    c2 = proj.commit(s.id, None, {main.id: bytes(data)}, label="stage 2")
    H.add(proj, s.id, H.PD_INJECTORS, "PD150 injectors", {"preset": "pd150"},
          since="2026-09-01", checkpoint=c1.id)
    H.add(proj, s.id, H.PD_INJECTORS, "PD100 injectors", {"preset": "pd100"},
          since="2026-10-01", checkpoint=c2.id, pending=True)
    path = str(tmp_path / "car.ovt")
    proj.save(path)
    doc = SimpleNamespace(project=proj, path=path)
    ref = SimpleNamespace(doc=doc, sid=s.id, bid=s.active.id, branch=s.active, set=s)
    return proj, s, (root, c1.id, c2.id), ref


def _hw_of(st, src=None, key="run"):
    from ovtsuite import gui_dyno
    got = st._hardware_of(gui_dyno.Run(key, "", "", 2, None, source=src))
    return None if got is None else [x.summary for x in got]


def test_a_version_or_another_tab_has_its_own_hardware(tmp_path):
    """A version of this file is what that checkpoint is set up for; another
    tab of the project is its branch head, with its unsaved edits when it has
    them; a reference chosen from the history is that version's."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    import weakref
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno as _dy, dyno_sources as S, gui_dyno
    ws = FakeTab(_load(LQ))
    try:
        proj, s, (root, c1, c2), ref = _project(tmp_path)
        ws.project_ref, ws.is_dirty = ref, lambda: False
        st = gui_dyno.state_for(ws)
        assert _hw_of(st, key="this") == ["PD150 injectors"]   # PD100 not saved
        ws.is_dirty = lambda: True
        assert _hw_of(st, key="this") == ["PD100 injectors"]
        assert _hw_of(st, S.Source(S.VERSION, "v", vid=root)) == []
        assert _hw_of(st, S.Source(S.VERSION, "v", vid=c1)) == ["PD150 injectors"]
        assert _hw_of(st, S.Source(S.VERSION, "v", vid=c2)) == ["PD150 injectors"]
        assert _hw_of(st, S.Source(S.VERSION, "v", vid="nope")) is None
        clean, dirty = _OtherTab(ref), _OtherTab(ref, dirty=True)
        assert _hw_of(st, S.Source(S.TAB, "t", tab=weakref.ref(clean))) == ["PD150 injectors"]
        assert _hw_of(st, S.Source(S.TAB, "t", tab=weakref.ref(dirty))) == ["PD100 injectors"]
        # a tab of another project: that project's own records
        stranger = _OtherTab(SimpleNamespace(doc=SimpleNamespace(project=proj, path=""),
                                             sid=s.id, branch=s.active))
        assert _hw_of(st, S.Source(S.TAB, "t", tab=weakref.ref(stranger))) == [
            "PD150 injectors"]
        plain = _OtherTab(None)                 # a tab not in a project
        assert _hw_of(st, S.Source(S.TAB, "t", tab=weakref.ref(plain))) is None
        st.ref_kind, st.ref_version = _dy.REF_VERSION, c1
        assert _hw_of(st, key="ref") == ["PD150 injectors"]
        st.ref_kind, st.ref_version = _dy.REF_ORIGINAL, None
        assert _hw_of(st, key="ref") == []
    finally:
        ws.project_ref = None
        ws.close()
        _pump()


def test_a_log_has_the_hardware_of_when_it_was_recorded(tmp_path):
    """A log kept in the project is matched to the hardware the car had when
    it was recorded (its own first time stamp), not when it was added."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import logtune_sim as sim
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.datalog.channels import ChannelNamer
    s0 = _load(LQ)
    ws = FakeTab(s0)
    try:
        proj, s, _cids, ref = _project(tmp_path)
        lp = sim.write_log(str(tmp_path / "pull.csv"), s0, ChannelNamer(s0), [1, 8, 9, 10],
                           sim.wot_pull(sim.Car(s0)))           # recorded 2026-09-30
        with open(lp, "rb") as fh:
            a = proj.add_log("pull.csv", fh.read(), set_id=s.id)
        a.added = "2026-10-20T09:00:00"       # added after the PD100 were fitted
        proj.save(ref.doc.path)
        ws.project_ref, ws.is_dirty = ref, lambda: False
        st = gui_dyno.state_for(ws)
        src = S.Source(S.LOG, "pull", path=ref.doc.path, log_id=a.id)
        assert _hw_of(st, src) == ["PD100 injectors"]        # not read yet: when added
        runs, why = st.resolver.runs(src, s0)
        assert runs, why
        assert st.resolver.recorded(src) == "2026-09-30T12:00:00"
        assert _hw_of(st, src) == ["PD150 injectors"]        # when it was recorded
    finally:
        ws.project_ref = None
        ws.close()
        _pump()


def test_recorded_at_ignores_a_date_after_the_log_was_kept():
    from datetime import datetime
    from ovtsuite.dyno_sources import _recorded_at
    log = SimpleNamespace(stamps=[datetime(2026, 9, 30, 12, 0, 0)])
    assert _recorded_at(log) == "2026-09-30T12:00:00"
    assert _recorded_at(log, "2026-10-01T00:00:00") == "2026-09-30T12:00:00"
    assert _recorded_at(log, "2026-09-01T00:00:00") == ""   # stamped when read
    assert _recorded_at(SimpleNamespace(stamps=[])) == ""


def test_each_runs_hardware_in_its_tooltips_and_the_note(tmp_path):
    """The run list and the chart's legend say what each run was made on;
    the note names the part that differs and what each run had of it."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.project import hardware as H
    ws = FakeTab(_load(LQ))
    ws.resize(1200, 800)
    ws.show()
    try:
        proj, s, (root, c1, c2), ref = _project(tmp_path)
        ws.project_ref, ws.is_dirty = ref, lambda: False
        st = gui_dyno.state_for(ws)
        st.poll()                       # the set's own dyno settings first
        src = st.add_source(S.Source(S.VERSION, "Stage 1", vid=c1))
        assert st.run_hardware("this").startswith("Hardware: Unit injectors: PD150 injectors")
        assert st.run_hardware("ref") == ("Hardware: as it came (no hardware change "
                                          "recorded for it)")
        assert "PD150" in st.run_hardware(src.key)
        note = st.hardware_note()
        assert note.startswith("Not the same hardware (unit injectors): "), note
        assert "This file: PD150 injectors" in note and "as it came" in note
        d = gui_dyno.open_for(ws)
        _pump()
        d._runs_sig = None
        d._fill_runs()
        assert "Hardware: Unit injectors: PD150" in d.runs_list.item(0).toolTip()
        d.chart.grab()
        rect, _run = next(h for h in d.chart._legend_hits if h[1].key == "this")
        assert "Hardware: Unit injectors: PD150" in d.chart.legend_tip(rect.center())
        # a record made since (Tools > Injector setup) is seen on the next poll
        H.add(proj, s.id, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"},
              since="2026-10-10", checkpoint=c2)
        assert st.poll()
        assert "PD130" in st.run_hardware("this")
        d.close()
    finally:
        ws.project_ref = None
        ws.close()
        _pump()


# ------------------------------------- other projects, files, logs, pins
def _open_in(ws, *docs):
    """``ws``'s window with ``docs`` open (what gui_project.docs reads)."""
    win = SimpleNamespace()
    win.__dict__["_ovt_docs"] = list(docs)
    ws.win = win
    return win


def test_another_projects_runs_have_that_projects_hardware(tmp_path):
    """A branch or version of another project, open or only on disk, is what
    that project records for it -- an open one as it is in memory."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.project import Project
    from ovtsuite.project import hardware as H
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ws = FakeTab(_load(LQ))
    try:
        _pa, _sa, _ca, ref = _project(tmp_path / "a")
        pb, sb, (rootb, c1b, c2b), refb = _project(tmp_path / "b")
        ws.project_ref, ws.is_dirty = ref, lambda: False
        st = gui_dyno.state_for(ws)
        on_disk = Project.load(refb.doc.path)        # B not open: read from its file
        bid = on_disk.set(sb.id).active.id
        head = S.Source(S.PROJECT, "B", path=refb.doc.path, sid=sb.id, bid=bid)
        v1 = S.Source(S.PROJECT, "B v1", path=refb.doc.path, sid=sb.id, bid=bid, cid=c1b)
        orig = S.Source(S.PROJECT, "B orig", path=refb.doc.path, sid=sb.id, bid=bid,
                        cid=rootb)
        assert _hw_of(st, head) == ["PD150 injectors"]    # PD100 pending, not saved in
        assert _hw_of(st, v1) == ["PD150 injectors"]
        assert _hw_of(st, orig) == []                      # as it came
        # B open, with a record not saved yet: the open project's state
        H.add(pb, sb.id, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"},
              since="2026-10-10", checkpoint=c2b)
        _open_in(ws, refb.doc)
        assert _hw_of(st, head) == ["PD130 injectors"]
        assert _hw_of(st, v1) == ["PD150 injectors"]
    finally:
        ws.project_ref = None
        ws.close()
        _pump()


def test_an_ovt_file_on_disk_is_read_for_its_records(tmp_path):
    """A file added from disk that is an .ovt project is run as its set's
    branch head and has that project's hardware; the file is not held open."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno_sources as S, gui_dyno
    s0 = _load(LQ)
    ws = FakeTab(s0)
    try:
        _proj, _s, _cids, refb = _project(tmp_path)
        st = gui_dyno.state_for(ws)                  # a plain file's tab
        src = S.Source(S.FILE, "car.ovt", path=refb.doc.path)
        assert S.is_project_file(src.path) and not S.is_project_file(LQ)
        sess, why = st.resolver.session(src, s0)
        assert sess is not None and sess.platform == "edc16", why
        assert _hw_of(st, src) == ["PD150 injectors"]
        os.replace(refb.doc.path, str(tmp_path / "moved.ovt"))   # not locked
        assert _hw_of(st, S.Source(S.FILE, "bin", path=LQ)) is None   # not a project
    finally:
        ws.close()
        _pump()


def test_a_log_file_on_disk_has_the_hardware_of_the_open_car(tmp_path):
    """A log file not kept in a project is matched to the open project set
    whose part number its ECU ident names, at the log's own date; a log with
    no ident is not known."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import logtune_sim as sim
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.datalog import logfile as LF
    from ovtsuite.datalog.channels import ChannelNamer
    s0 = _load(LQ)
    ws = FakeTab(s0)
    try:
        _proj, s, _cids, ref = _project(tmp_path)
        assert s.identity.get("part") == "03G906016LQ"
        plain = sim.write_log(str(tmp_path / "pull.csv"), s0, ChannelNamer(s0),
                              [1, 8, 9, 10], sim.wot_pull(sim.Car(s0)))  # 2026-09-30
        log = LF.load(plain)
        log.identity = "03G906016LQ  R4 2,0L EDC  G000AG  6535"
        vcds = str(tmp_path / "pull_vcds.csv")
        LF.write_vcds(vcds, log)
        st = gui_dyno.state_for(ws)                  # a plain tab; the car's project open
        _open_in(ws, ref.doc)
        named, nameless = S.Source(S.LOG, "v", path=vcds), S.Source(S.LOG, "p", path=plain)
        for src in (named, nameless):
            runs, why = st.resolver.runs(src, s0)
            assert runs, why
        assert st.resolver.identity(named).startswith("03G906016LQ")
        assert st.resolver.recorded(named) == "2026-09-30T12:00:00"
        assert _hw_of(st, named) == ["PD150 injectors"]    # PD100 fitted 2026-10-01
        assert _hw_of(st, nameless) is None                # no ident: skipped
        ws.win.__dict__["_ovt_docs"] = []                  # the car's project closed
        assert _hw_of(st, named) is None
    finally:
        ws.close()
        _pump()


def test_a_pin_keeps_the_hardware_it_had(tmp_path):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui_dyno
    from ovtsuite.project import hardware as H
    ws = FakeTab(_load(LQ))
    try:
        proj, s, (_root, _c1, c2), ref = _project(tmp_path)
        ws.project_ref, ws.is_dirty = ref, lambda: False
        st = gui_dyno.state_for(ws)
        st.poll()
        pin = st.pin()
        assert [x.summary for x in pin.hardware] == ["PD150 injectors"]
        H.add(proj, s.id, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"},
              since="2026-10-10", checkpoint=c2)
        assert st.poll()
        assert "PD130" in st.run_hardware("this")
        assert "PD150" in st.run_hardware(pin.key) and "PD130" not in st.run_hardware(pin.key)
        assert _hw_of(st, pin) == ["PD150 injectors"]
        assert "Not the same hardware (unit injectors)" in st.hardware_note()
    finally:
        ws.project_ref = None
        ws.close()
        _pump()


def test_a_plain_file_is_identified_from_its_maps(monkeypatch):
    """With no project to say, a PD file's hardware is what its duration
    maps match -- said to be from the maps, kept per file content, and (an
    exact match) taken as a record is for the duration basis; a close match
    is only a note."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno as _dy
    from ovtsuite import dyno_hardware as DH
    from ovtsuite import eoi, gui_dyno
    from ovtsuite import injectors as I
    DH.forget()
    this, ref = _load(LQ), _load(LQ)
    cat = I.catalogue()
    p = cat.get("bhw")
    plan = I.plan_fit(ref, eoi.models(ref)[0], cat.char_of(p), I.FitOptions(),
                      cat.electrical_of(p), cat.extras_of(p), I.platform_for(ref))
    I.apply_fit(ref, [plan], "bhw", True)
    calls = []
    real = I.identify
    monkeypatch.setattr(I, "identify", lambda *a, **k: calls.append(1) or real(*a, **k))
    ws = FakeTab(this, comparison=ref)
    try:
        st = gui_dyno.state_for(ws)
        st.set_reference(_dy.REF_COMPARISON)
        assert st.cond.fuel_basis == "duration"
        this_hw = st.hardware["this"]
        assert DH.inferred(this_hw) and this_hw[0].parts == {"preset": "bpw"}
        assert DH.is_confident(this_hw[0])
        assert st.hardware["ref"][0].parts == {"preset": "bhw"}
        assert "(from the maps)" in st.run_hardware("this")
        assert "looks set up for 2.0 8v BPW" in st.run_hardware("this")
        note = st.hardware_note()
        assert note.startswith("Not the same hardware (unit injectors): "), note
        assert "looks set up for" in note and "(from the maps)" in note
        assert st._fuel_own == ["This file"]          # exact both: not carried across
        n = len(calls)
        assert n >= 1
        st.invalidate()                                # polled again: not re-identified
        DH.infer(_load(LQ))                            # the same bytes, another session
        assert len(calls) == n
        # a close match only: noted, but the quantity is carried across
        DH.forget()
        monkeypatch.setattr(I, "identify", lambda char, c, limit=5: [
            I.Match(m.preset, 0.005, 0.5) for m in real(char, c, limit)])
        st.invalidate()
        assert not DH.is_confident(st.hardware["this"][0])
        assert "not an exact match" in st.run_hardware("this")
        assert "Not the same hardware" in st.hardware_note()
        assert st._fuel_own == []
    finally:
        DH.forget()
        ws.close()
        _pump()


def test_a_ve_file_is_identified_from_its_pump_map():
    from conftest import BINARIES
    path = os.path.join(BINARIES, "EDC15", "038906012A.bin")
    if not os.path.exists(path):
        pytest.skip("reference binary missing")
    from ovtsuite import dyno_hardware as DH
    from ovtsuite.project import hardware as H
    DH.forget()
    got = DH.infer(_load(path), "Manual")
    assert got is not None and got[0].kind == H.VE_PUMP
    assert got[0].parts == {"plunger_mm": 10.0, "nozzle": "p520"}
    assert DH.is_confident(got[0])
    assert DH.short(got[0]).startswith("looks set up for 10 mm pump head")
    assert DH.short(got[0]).endswith("(from the maps)")
    # against a record of the same build: not different; another head: different
    rec = H.Fitted("r", H.VE_PUMP, "AFN", {"plunger_mm": 10.0, "nozzle": "p520"})
    assert DH.differ(got, [rec]) == []
    rec.parts = {"plunger_mm": 11.0, "nozzle_kept": True}
    assert DH.differ(got, [rec]) == [H.VE_PUMP]
    assert DH.differ(got, []) == []                    # "as it came": not told apart
    DH.forget()
