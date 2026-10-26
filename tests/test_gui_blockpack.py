# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Build logging blocks... (gui_blockpack) on the LQ, offscreen:
what it lists, Generate as one undo step recorded in the project, the fill
option, your presets, and Remove generated."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.slow

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(REPO, "Reference", "Binaries", "03G906016LQ.bin")
_KEEP = []


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def lq():
    if not os.path.exists(LQ):
        pytest.skip("needs 03G906016LQ.bin")
    from ovtsuite.session import TunerSession
    return TunerSession(LQ)


def _ws(app, session):
    from PySide6 import QtWidgets

    class _Set:
        def __init__(self):
            self.extra = {}

    class _Ref:
        def __init__(self):
            self.set = _Set()

    class _Ws(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.primary = session
            self.project_ref = _Ref()
            self.opened = {}

        def open_tool(self, kind, factory):
            d = self.opened[kind] = factory()
            return d

    w = _Ws()
    _KEEP.append(w)
    return w


def _rows(d):
    out = {}
    for r, e in enumerate(d._rows):
        if e is not None:
            out[e.key] = r
    return out


def _tick(d, key, on):
    from PySide6 import QtCore
    d.table.item(_rows(d)[key], 0).setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)


def test_it_lists_every_preset_with_today_and_optimised(app, lq):
    from ovtsuite import gui_blockpack as G
    ws = _ws(app, lq)
    assert G.availability(ws) is None
    d = G.open_for(ws)
    _KEEP.append(d)
    rows = _rows(d)
    assert set(rows) >= {"boost", "fuel", "timing", "egt", "cruise"}
    r = rows["boost"]
    assert d.table.item(r, G.C_TODAY).text().startswith("3")
    assert d.table.item(r, G.C_OPT).text().startswith("1")
    assert "fills" in d.table.item(r, G.C_CHANGES).text()
    assert "unused group" in d.room.text()
    assert d.gen_btn.isEnabled() and not d.remove_btn.isEnabled()
    # the detail shows the cells that would be added
    d.table.selectRow(r)
    top = d.detail.topLevelItem(0)
    assert top.text(0).startswith("Block ") and top.childCount() >= 6
    assert any(top.child(i).text(2) == "added" for i in range(top.childCount()))


def test_generate_writes_one_step_and_records_it(app, lq, monkeypatch):
    from ovtsuite import gui_blockpack as G
    from ovtsuite.datalog.blockpack.manifest import load_extra
    called = []
    monkeypatch.setattr(G, "refresh_loggers", lambda: called.append(1))
    ws = _ws(app, lq)
    d = G.BlockPackDialog(ws)
    _KEEP.append(d)
    for key in _rows(d):
        _tick(d, key, key == "boost")
    depth = len(lq.undo_stack._done)
    d._generate()
    try:
        assert len(lq.undo_stack._done) == depth + 1
        assert lq.undo_stack.top().label == "Build logging blocks"
        m = load_extra(ws.project_ref.set.extra)
        assert m is not None and set(m.presets) == {"boost"}
        assert called
        assert d.table.item(_rows(d)["boost"], G.C_STATE).text() == "Built"
        assert "Built logging blocks" in d.status.text()
        # Remove generated empties them again, as one more undo step
        for key in _rows(d):
            _tick(d, key, key == "boost")
        assert d.remove_btn.isEnabled()
        d._remove()
        try:
            assert load_extra(ws.project_ref.set.extra) is None
            assert d.table.item(_rows(d)["boost"], G.C_STATE).text() == ""
        finally:
            lq.undo()
    finally:
        lq.undo()


def test_without_filling_it_uses_unused_groups(app, lq):
    from ovtsuite import gui_blockpack as G
    ws = _ws(app, lq)
    d = G.BlockPackDialog(ws)
    _KEEP.append(d)
    d.fill_cb.setChecked(False)
    try:
        boost = d._preview["boost"]
        assert boost.new_groups and not boost.filled_groups
        assert boost.new_groups[0] == 124           # highest free group first
    finally:
        d.fill_cb.setChecked(True)


def test_your_presets_are_listed_and_planned(app, lq):
    from PySide6 import QtCore
    from ovtsuite import branding
    from ovtsuite import gui_blockpack as G
    from ovtsuite.datalog import userpresets as up
    prefs = QtCore.QSettings(*branding.QSETTINGS)
    up.save(prefs, "EDC16", [up.UserPreset("Boost check", [
        up.Cell(1, 0, "Eng_nAvrg", "Engine speed"),
        up.Cell(10, 2, "BPSCD_pFltVal", "Boost"),
        up.Cell(11, 0, "APSCD_pVal", "Atmospheric pressure")])])
    ws = _ws(app, lq)
    d = G.BlockPackDialog(ws)
    _KEEP.append(d)
    keys = _rows(d)
    assert "user:Boost check" in keys
    heading = [r for r, e in enumerate(d._rows) if e is None]
    assert heading and d.table.item(heading[0], G.C_NAME).text() == "Your presets"
    p = d._preview["user:Boost check"]
    assert len(p.baseline) >= 2 and len(getattr(p, "groups", p.baseline)) >= 1


def test_not_offered_for_a_file_without_measuring_blocks(app):
    from ovtsuite import gui_blockpack as G

    class _S:
        platform = "zf6hp"
        support = "full"
        entities = ()

    class _W:
        primary = _S()
    assert "no measuring blocks" in G.availability(_W())


def test_blocks_of_a_deleted_preset_are_listed_and_can_be_removed(app, lq):
    """Review fix: deleting your preset left its built cells in the file with
    no row to remove them from. They are listed under "Presets no longer
    there", tickable for Remove only, never planned or generated."""
    from PySide6 import QtCore
    from ovtsuite import branding
    from ovtsuite import gui_blockpack as G
    from ovtsuite.datalog import userpresets as up
    from ovtsuite.datalog.blockpack.manifest import load_extra
    prefs = QtCore.QSettings(*branding.QSETTINGS)
    mine = up.UserPreset("Boost check", [
        up.Cell(1, 0, "Eng_nAvrg", "Engine speed"),
        up.Cell(10, 2, "BPSCD_pFltVal", "Boost"),
        up.Cell(11, 0, "APSCD_pVal", "Atmospheric pressure")])
    up.save(prefs, "EDC16", [mine])
    ws = _ws(app, lq)
    d = G.BlockPackDialog(ws)
    _KEEP.append(d)
    if not isinstance(d._preview.get(mine.data), G.PresetPlan):
        pytest.skip("the preset gains nothing on the LQ")
    for key in _rows(d):
        _tick(d, key, key == mine.data)
    d._generate()
    try:
        cells = load_extra(ws.project_ref.set.extra).preset_cells(mine.data)
        assert cells
        up.save(prefs, "EDC16", [])                       # the preset deleted
        d.refresh_from_session()
        r = _rows(d)[mine.data]
        assert d._rows[r].is_orphan
        assert d.table.item(r, G.C_STATE).text() == "Built for a deleted preset"
        assert "Presets no longer there" in [d.table.item(i, G.C_NAME).text()
                                             for i, e in enumerate(d._rows) if e is None]
        assert mine.data not in d._preview                # never planned
        for key in _rows(d):
            _tick(d, key, key == mine.data)
        assert d.remove_btn.isEnabled() and not d.gen_btn.isEnabled()
        d._remove()
        try:
            assert all(d._b.provider.value_at(c.group, c.slot) == 255 for c in cells)
            assert load_extra(ws.project_ref.set.extra) is None
            assert mine.data not in _rows(d)
        finally:
            lq.undo()
    finally:
        lq.undo()


GC = os.path.join(REPO, "Reference", "Binaries", "03G906016GC.bin")
P216 = os.path.join(REPO, "Reference", "Binaries", "p216_5kt1_ref.bin")
NO_MAP = os.path.join(REPO, "Reference", "Binaries", "p475_aas8_ref.bin")


def _open(path):
    if not os.path.exists(path):
        pytest.skip("needs %s" % os.path.basename(path))
    from ovtsuite.session import TunerSession
    return TunerSession(path)


def test_gc_gains_and_says_which_tables_were_found(app):
    """03G906016GC: its definition leaves out the ESB and reserving tables;
    the window finds them, offers every preset, and says so."""
    from ovtsuite import gui_blockpack as G
    d = G.BlockPackDialog(_ws(app, _open(GC)))
    _KEEP.append(d)
    rows = _rows(d)
    r = rows["boost"]
    assert d.table.item(r, G.C_OPT).text().startswith("1")
    assert not d.limits.isHidden()
    assert "found in the file by structure" in d.limits.text()
    assert d.gen_btn.isEnabled()


def test_a_file_it_cannot_analyse_says_why_not_no_gain(app):
    from ovtsuite import gui_blockpack as G
    d = G.BlockPackDialog(_ws(app, _open(P216)))
    _KEEP.append(d)
    texts = [d.table.item(r, G.C_CHANGES).text() for r in _rows(d).values()]
    assert texts and all(t.startswith("Not analysed: ") for t in texts)
    assert not any(t.startswith("No gain") for t in texts)
    assert "signal name" in d.limits.text()
    assert not d.gen_btn.isEnabled()


def test_a_definition_without_the_block_table_is_named_as_the_reason(app):
    from ovtsuite import gui_blockpack as G
    why = G.availability(_ws(app, _open(NO_MAP)))
    assert why and "does not place its measuring-block table" in why
