# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The data logger with optimised logging blocks (docs/LOGGING_BLOCKS_PLAN.md):
a preset built in the open LQ file reads its optimised blocks, after they
are read once from the ECU; an ECU without them falls back to the usual
blocks; the option turns it off. The worker is faked -- the probe's answer is
handed to the pane as the real worker would."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.slow

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(REPO, "Reference", "Binaries", "03G906016LQ.bin")
_KEEP = []


@pytest.fixture(scope="module")
def app():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def built(app, tmp_path, monkeypatch):
    """The LQ with the Boost control preset built, open in a fake window, and
    a logger pane for it."""
    if not os.path.exists(LQ):
        pytest.skip("needs 03G906016LQ.bin")
    from PySide6 import QtCore, QtWidgets
    from ovtsuite.datalog import gui_log as GL
    from ovtsuite.datalog.blockpack.service import Builder
    from ovtsuite.session import TunerSession
    prefs = lambda: QtCore.QSettings(str(tmp_path / "prefs.ini"),   # noqa: E731
                                     QtCore.QSettings.Format.IniFormat)
    monkeypatch.setattr(GL, "_prefs", prefs)
    s = TunerSession(LQ)

    class _Set:
        extra = {}
        name = "Stock"

    class _Doc:
        title = "LQ"
        tmpdir = str(tmp_path)

    class _Ref:
        set = _Set()
        doc = _Doc()

    class _Ws:
        primary = s
        project_ref = _Ref()

    b = Builder(s, _Ws.project_ref.set)
    b.generate(b.plan(["boost"]))

    class _Win(QtWidgets.QMainWindow):
        def workspaces(self):
            return [_Ws]

        @property
        def current(self):
            return _Ws

        @property
        def primary(self):
            return s

    win = _Win()
    pane = GL.ConnPane(win, default_ecu="EDC16")
    _KEEP.extend([win, pane])
    return pane, b, s


class _Worker:
    def __init__(self):
        self.asked = []
        self._stop = False

    def request_probe(self, token, groups):
        self.asked.append((token, list(groups)))


def _choose(pane, key):
    i = pane.preset_box.findData(key)
    pane.preset_box.setCurrentIndex(i)
    pane._apply_preset()


def _answer(pane, manifest, groups, empty=False):
    from ovtsuite.flash.diag import Cell
    read = {}
    for g in groups:
        cells = []
        for s in range(8):
            c = manifest.cell(g, s)
            if c is not None and not empty:
                cells.append(Cell(c.fid, c.a if c.a is not None else 0, 0))
            elif c is not None:
                cells.append(Cell(0x25, 0, 0))
            else:
                cells.append(Cell(0x01, 0xC8, 0x20))
        read[g] = cells
    token = pane._probing[0]
    pane._on_probed(token, read)


def test_the_preset_is_listed_as_optimised(built):
    pane, b, _s = built
    i = pane.preset_box.findData("boost")
    assert "(optimised: 1 block)" in pane.preset_box.itemText(i)
    assert pane.opt_cb.isChecked() and pane.opt_btn.isEnabled()


def test_not_connected_it_reads_the_optimised_blocks_and_says_when_they_are_checked(built):
    pane, b, _s = built
    _choose(pane, "boost")
    groups = b.manifest.presets["boost"].groups
    assert {c.group for c in pane._channels if not c.is_derived} == set(groups)
    assert "checked with the ECU when you connect" in pane.preset_hint.text()


def test_connected_it_reads_them_once_and_start_waits(built):
    pane, b, _s = built
    pane._worker = _Worker()
    try:
        _choose(pane, "boost")
        groups = b.manifest.presets["boost"].groups
        assert pane._worker.asked and pane._worker.asked[-1][1] == groups
        assert not pane.start_btn.isEnabled()          # waits for the answer
        _answer(pane, b.manifest, groups)
        assert pane._probing is None
        assert "the ECU has them" in pane.preset_hint.text()
        assert {c.group for c in pane._channels if not c.is_derived} == set(groups)
        # chosen again: no second read on this connection
        n = len(pane._worker.asked)
        _choose(pane, "boost")
        assert len(pane._worker.asked) == n
    finally:
        pane._worker = None
        pane._probe_state.clear()


def test_an_ecu_without_them_falls_back_to_the_usual_blocks(built):
    pane, b, _s = built
    pane._worker = _Worker()
    try:
        _choose(pane, "boost")
        groups = b.manifest.presets["boost"].groups
        _answer(pane, b.manifest, groups, empty=True)
        hint = pane.preset_hint.text()
        assert "does not have this preset's optimised blocks" in hint
        assert "Blocks 1, 10, 11" in hint
        got = {c.group for c in pane._channels if not c.is_derived}
        assert got == {1, 10, 11}
        # and the added cells of the filled block are not logged from the stock view
        assert not [c for c in pane._channels if b.manifest.cell(c.group, c.field)]
        i = pane.preset_box.findData("boost")
        assert "optimised" not in pane.preset_box.itemText(i)
    finally:
        pane._worker = None
        pane._probe_state.clear()
        pane._probe_unserved.clear()


def test_the_option_turns_it_off(built):
    pane, b, _s = built
    pane.opt_cb.setChecked(False)
    try:
        _choose(pane, "boost")
        assert {c.group for c in pane._channels if not c.is_derived} == {1, 10, 11}
        assert "optimised" not in pane.preset_box.itemText(pane.preset_box.findData("boost"))
    finally:
        pane.opt_cb.setChecked(True)


def test_the_worker_queues_a_probe():
    from ovtsuite.datalog.gui_log import LogWorker
    w = LogWorker("COM1", "EDC16", "Low", False)
    w.request_probe(7, [58, 4])
    assert w._probe_req == (7, [58, 4])


def test_a_failed_filled_group_still_serves_the_other_presets_usual_blocks(built):
    """Review fix: Full-load fuelling is built by filling stock groups (8, and
    10 or Boost control's 58). On an ECU without the build those answer their
    stock cells, so they must not join the connection's unserved set: each
    preset falls back to exactly its usual blocks -- Boost control's 1, 10, 11
    among them."""
    pane, b, _s = built
    b.generate(b.plan(["fuel"]))
    pane._fill_presets()
    pane._worker = _Worker()
    try:
        _choose(pane, "fuel")
        fuel = b.manifest.presets["fuel"].groups
        assert {g for g in fuel if b.manifest.cells_in([g])} and fuel != [6, 8, 9, 67]
        _answer(pane, b.manifest, fuel, empty=True)
        assert not pane._probe_unserved                # every group answered its own cells
        assert {6, 8, 9, 67} == {c.group for c in pane._channels if not c.is_derived}
        _choose(pane, "boost")
        _answer(pane, b.manifest, b.manifest.presets["boost"].groups, empty=True)
        assert {c.group for c in pane._channels if not c.is_derived} == {1, 10, 11}
    finally:
        pane._worker = None
        pane._probe_state.clear()
        pane._probe_unserved.clear()


def test_renaming_your_preset_keeps_its_optimised_blocks(built, monkeypatch):
    """Review fix: your preset's key is its name; Rename carries the layout."""
    from PySide6 import QtWidgets
    from ovtsuite.datalog import userpresets as _up
    from ovtsuite.datalog.blockpack.manifest import load_extra
    pane, b, s = built
    pane._channels = [pane._namer.channel(1, 0), pane._namer.channel(10, 1),
                      pane._namer.channel(11, 0)]
    mine = _up.from_channels("Mine", pane._channels)
    _up.save(pane_prefs(pane), "EDC16", [mine])
    pane._window.workspaces()[0].project_ref.set.extra.clear()
    b2 = type(b)(s, pane._window.workspaces()[0].project_ref.set, pane_prefs(pane))
    b2.generate(b2.plan(["boost", mine.data]))
    if mine.data not in b2.manifest.presets:
        pytest.skip("this preset gains nothing on the LQ")
    pane._fill_presets(mine.data)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Renamed", True)))
    pane.preset_box.setCurrentIndex(pane.preset_box.findData(mine.data))
    pane._rename_user_preset()
    m = load_extra(pane._window.workspaces()[0].project_ref.set.extra)
    assert "user:Renamed" in m.presets and mine.data not in m.presets
    i = pane.preset_box.findData("user:Renamed")
    assert i >= 0 and "(optimised" in pane.preset_box.itemText(i)


def pane_prefs(pane):
    from ovtsuite.datalog import gui_log as GL
    return GL._prefs()


def test_a_probe_the_link_dropped_in_is_asked_again_after_the_reconnect(monkeypatch):
    """Review fix: a probe cut by a lost link was dropped, so the pane waited
    for its answer -- and held Start -- until Disconnect."""
    from ovtsuite.datalog import gui_log as GL
    from ovtsuite.flash.transport import KLineError

    class _Link:
        read_timeout = 8.0

        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    class _Sess:
        identity, baud = "ECU", 10400
        reads = []
        browsed = 0

        def __init__(self, link, ecu, log):
            self.dropped = False

        def connect(self, *a, **k):
            _Sess.worker.request_probe(5, [58])

        def read_groups(self, groups, is_stop=None):
            _Sess.reads.append(list(groups))
            if not self.dropped:
                self.dropped = True
                raise KLineError("line dropped")
            return {g: [] for g in groups}

        def read_group(self, g):
            _Sess.browsed += 1
            if _Sess.browsed > 20:          # never asked again: end the test
                _Sess.worker.stop()
            return []

        def reconnect(self, is_stop=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(GL, "KLine", _Link)
    monkeypatch.setattr(GL, "is_j2534", lambda port: False)
    monkeypatch.setattr(GL._diag, "DiagSession", _Sess)
    monkeypatch.setattr(GL.time, "sleep", lambda s: None)
    w = GL.LogWorker("COM9", "EDC16", "Low", False)
    _Sess.worker = w
    got = []

    def answered(token, read):
        got.append((token, read))
        w.stop()
    w.probed.connect(answered)
    w._run_session()
    assert _Sess.reads == [[58], [58]] and got == [(5, {58: []})]
