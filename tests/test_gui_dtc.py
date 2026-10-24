# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The fault-code window on EDC15: one dataset at a time, edits that redraw
only their rows, the comparison table under the primary's -- and the window
opening at all with a comparison binary (it raised AttributeError: the EDC15
table had no ``counterpart``)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore  # noqa: E402

from ovtsuite.session import TunerSession  # noqa: E402

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
FD = os.path.join(BIN, "EDC15", "038906012FD.bin")
BD = os.path.join(BIN, "EDC15", "038906012BD.bin")
LQ = os.path.join(BIN, "03G906016LQ.bin")


def _need(p):
    if not os.path.exists(p):
        pytest.skip("%s not present" % os.path.basename(p))


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def fd():
    _need(FD)
    return TunerSession(FD)


def _keys(table):
    return [table.item(r, 0).data(0x100) for r in range(table.rowCount())]


def test_comparison_after_the_first_save_opens(app, fd):
    """The comparison a project tab gets once it is saved is the original
    as read -- a sibling session of the same file. The window used to die
    building its first row (DtcTable15 had no counterpart())."""
    from ovtsuite.gui_dtc import DtcDialog
    fp = next(f for f in fd.dtc_paths() if f.vag_code) \
        if hasattr(fd, "dtc_paths") else None
    original = fd.sibling(bytes(fd.binfile.data), "FD - as read")
    from ovtsuite.dtc import DtcTable
    t = DtcTable(fd)
    fp = next(f for k, f in t.paths.items()
              if f.group == "Manual" and f.vag_code and t.is_enabled(f))
    t.set_enabled(fp, False)                 # the tab now differs from it
    try:
        d = DtcDialog(fd, compare=original)
        app.processEvents()
        assert d._has_cmp and d.cmp_table is not None
        assert d.cmp_table.rowCount() == d.table.rowCount() > 0
        # row for row: the comparison table follows the primary's order
        assert _keys(d.cmp_table) == _keys(d.table)
        key = "Manual:%s" % fp.name
        r = _keys(d.table).index(key)
        on = d.table.item(r, 0)
        assert on.checkState() == QtCore.Qt.CheckState.Unchecked
        assert d.cmp_table.item(r, 0).checkState() == QtCore.Qt.CheckState.Checked
        assert "Comparison: on" in on.toolTip()
        from ovtsuite.gui import EDIT_BG
        assert on.background().color() == EDIT_BG
        assert not (d.cmp_table.item(r, 0).flags()
                    & QtCore.Qt.ItemFlag.ItemIsUserCheckable)   # read only
        d.show_box.setCurrentText("Differs from comparison")
        app.processEvents()
        assert key in _keys(d.table)
        d.close()
    finally:
        fd.undo()


def test_no_provenance_boilerplate_in_descriptions(fd):
    from ovtsuite.dtc import DtcTable
    t = DtcTable(fd)
    assert not any("code-located" in fp.code_desc() or
                   "fault record" in fp.description for fp in t.paths.values())
    # what is left is the code's standard text, where it has one
    rom = [fp for fp in t.paths.values() if fp.pcode == "P0605"]
    assert rom and rom[0].code_desc() == "Internal control module ROM error"


def test_description_column_describes_code_located_records(app, fd):
    """FD's records are located by their code only: the Description column
    shows the code's text (SAE, VAG, or the named definitions' name for
    the sub-fault that sets it), not the record id or nothing."""
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(fd)
    col = d._col["desc"]
    rows = {d.dtc.paths[d.table.item(r, 0).data(0x100)].name:
            d.table.item(r, col).text() for r in range(d.table.rowCount())}
    assert rows["F0D_X"] == "EGR valve: short circuit"
    assert rows["F13_X"].startswith("Data bus drive train: missing message")
    assert rows["F00_X"] == "Internal control module ROM error"
    n = sum(1 for t in rows.values() if t)
    assert n >= len(rows) // 2


def test_one_dataset_at_a_time_and_no_lamp_columns(app, fd):
    """Each code once: the first dataset (Manual, coding 2) by default, every
    dataset on request. FD's definition places no path bytes, so there is
    no MIL or Glow column."""
    from ovtsuite.gui_dtc import DtcDialog, ALL_DATASETS
    d = DtcDialog(fd)
    assert d.ds_box is not None
    assert d.ds_box.currentText() == "Manual (coding 2)"
    groups = {d.dtc.paths[k].group for k in _keys(d.table)}
    assert groups == {"Manual"}
    n = d.table.rowCount()
    assert n == sum(1 for fp in d.dtc.paths.values() if fp.group == "Manual")
    assert len({d.table.item(r, 1).text() for r in range(n)}) == n
    assert "MIL" not in d._cols and "Glow" not in d._cols
    d.ds_box.setCurrentText(ALL_DATASETS)
    app.processEvents()
    assert d.table.rowCount() == len(d.dtc.paths)
    d.close()


def test_lamp_columns_stay_where_the_path_bytes_are_known(app):
    _need(BD)
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(TunerSession(BD))
    assert "MIL" in d._cols and "Glow" in d._cols
    d.close()


def test_an_edit_redraws_its_rows_and_reaches_every_dataset(app, fd, monkeypatch):
    """Unticking a row switches that fault off in every dataset (one undo
    step) and redraws those rows only -- no rebuild of the table."""
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(fd)
    assert d.all_ds.isChecked()
    calls = []
    monkeypatch.setattr(d, "_populate", lambda *a: calls.append(1))
    keys = _keys(d.table)
    r = next(i for i, k in enumerate(keys)
             if d.dtc.paths[k].vag_code and d.dtc.is_enabled(d.dtc.paths[k]))
    fp = d.dtc.paths[keys[r]]
    sibs = d.dtc.siblings(fp)
    assert len(sibs) == 3
    d.table.item(r, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
    app.processEvents()
    assert not any(d.dtc.is_enabled(o) for o in sibs)
    assert calls == []                       # the row, not the table
    r2 = _keys(d.table).index(keys[r])
    assert d.table.item(r2, 0).checkState() == QtCore.Qt.CheckState.Unchecked
    d.refresh_from_session()                 # the tab's poll: already shown
    assert calls == []
    fd.undo()
    assert all(d.dtc.is_enabled(o) for o in sibs)
    d.refresh_from_session()                 # an edit from elsewhere: rebuilt
    assert calls == [1]
    d.close()


def test_edc16_dialog_has_no_dataset_box(app):
    """EDC16 keeps one set of fault-path arrays (no coding redirects them),
    so there is nothing to choose."""
    _need(LQ)
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(TunerSession(LQ))
    assert d.ds_box is None and d.all_ds is None
    assert "MIL" in d._cols
    d.close()
