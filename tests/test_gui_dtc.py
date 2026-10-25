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


# -- Apply / Cancel / OK ----------------------------------------------------
#
# Edits go to the session as they are made; the window remembers the undo
# steps it made since it opened or the last Apply. Apply folds them into one
# step, Cancel takes them back (bytes exactly as they were), OK applies and
# closes; the title bar's X and Esc ask when there are edits not applied.

@pytest.fixture
def own_fd():
    _need(FD)
    return TunerSession(FD)


def _no_question(monkeypatch):
    def ask(*a, **k):
        raise AssertionError("asked: %r" % (a[2] if len(a) > 2 else a,))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(ask))


def _row_of(d, key):
    return _keys(d.table).index(key)


def _enabled_keys(d, n):
    return [k for k in _keys(d.table)
            if d.dtc.is_enabled(d.dtc.paths[k])][:n]


def _untick(app, d, key):
    d.table.item(_row_of(d, key), 0).setCheckState(
        QtCore.Qt.CheckState.Unchecked)
    app.processEvents()                     # the row redraw is queued


def test_buttons_are_apply_cancel_ok_in_that_order(app, own_fd):
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(own_fd)
    texts = [b.text() for b in d.findChildren(QtWidgets.QPushButton)]
    assert "Close" not in texts
    for keep in ("Disable selected", "Enable selected",
                 "Read codes from ECU...", "Restore all from stock..."):
        assert keep in texts
    row = next(lay for lay in d.layout().children()
               if isinstance(lay, QtWidgets.QHBoxLayout)
               and lay.indexOf(d.apply_btn) >= 0)
    idx = [row.indexOf(b) for b in (d.apply_btn, d.cancel_btn, d.ok_btn)]
    assert idx == sorted(idx) and idx[2] == row.count() - 1
    assert [d.apply_btn.text(), d.cancel_btn.text(), d.ok_btn.text()] == \
        ["Apply", "Cancel", "OK"]
    assert not d.apply_btn.isEnabled()      # nothing to apply yet


def test_cancel_after_apply_takes_back_only_the_later_edits(app, own_fd,
                                                          monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog, PENDING_BG
    s = own_fd
    orig = bytes(s.binfile.data)
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    k1, k2, k3 = _enabled_keys(d, 3)
    _untick(app, d, k1)
    _untick(app, d, k2)
    assert len(s.undo_stack) == n0 + 2      # written as they are made
    assert d.has_pending() and d.apply_btn.isEnabled()
    assert d.pending_lbl.text().startswith("6 rows changed")   # 3 datasets
    cell = d.table.item(_row_of(d, k1), 1)
    assert cell.background().color() == PENDING_BG and cell.font().bold()
    d.apply_btn.click()
    assert len(s.undo_stack) == n0 + 1      # one undo step per Apply
    assert s.undo_stack.undo_label() == "edit fault codes"
    assert not d.has_pending() and d.pending_lbl.text() == ""
    assert d.table.item(_row_of(d, k1), 1).background().color() != PENDING_BG
    applied = bytes(s.binfile.data)
    _untick(app, d, k3)
    assert bytes(s.binfile.data) != applied
    d.cancel_btn.click()
    assert not d.isVisible()
    assert bytes(s.binfile.data) == applied  # exactly as at the Apply
    assert len(s.undo_stack) == n0 + 1 and not s.can_redo
    assert d.dtc.is_enabled(d.dtc.paths[k3])
    assert not d.dtc.is_enabled(d.dtc.paths[k1])
    s.undo()                                # the Apply, as one step
    assert bytes(s.binfile.data) == orig


def test_cancel_with_no_apply_restores_the_file_as_opened(app, own_fd,
                                                         monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = own_fd
    s.edit_bytes(0x10, bytes([s.binfile.data[0x10] ^ 0xFF]))  # an earlier edit
    opened = bytes(s.binfile.data)
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    for k in _enabled_keys(d, 2):
        _untick(app, d, k)
    d.cancel()
    assert bytes(s.binfile.data) == opened
    assert len(s.undo_stack) == n0          # the user's own history is kept
    assert s.undo_stack.undo_label().startswith("edit bytes")


def test_ok_applies_and_closes(app, own_fd, monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = own_fd
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    keys = _enabled_keys(d, 2)
    for k in keys:
        _untick(app, d, k)
    d.ok_btn.click()
    assert not d.isVisible() and not d.has_pending()
    assert len(s.undo_stack) == n0 + 1
    assert not any(d.dtc.is_enabled(d.dtc.paths[k]) for k in keys)


def test_switching_back_leaves_no_undo_step(app, own_fd, monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = own_fd
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    k = _enabled_keys(d, 1)[0]
    _untick(app, d, k)
    d.table.item(_row_of(d, k), 0).setCheckState(QtCore.Qt.CheckState.Checked)
    app.processEvents()
    assert d.pending_lbl.text() == ""       # nothing differs any more
    d.apply()
    assert len(s.undo_stack) == n0


def test_x_and_esc_ask_when_edits_are_not_applied(app, own_fd, monkeypatch):
    from ovtsuite.gui_dtc import DtcDialog
    SB = QtWidgets.QMessageBox.StandardButton
    s = own_fd
    opened = bytes(s.binfile.data)
    d = DtcDialog(s)
    d.show()
    _untick(app, d, _enabled_keys(d, 1)[0])
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: asked.append(1) or SB.Cancel))
    d.reject()                              # Esc
    d.close()                               # the title bar's X
    assert asked == [1, 1] and d.isVisible() and d.has_pending()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: SB.Discard))
    d.close()
    assert not d.isVisible()
    assert bytes(s.binfile.data) == opened and not s.modified


def test_x_with_nothing_pending_closes_without_asking(app, own_fd, monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(own_fd)
    d.show()
    _untick(app, d, _enabled_keys(d, 1)[0])
    d.apply()
    d.close()
    assert not d.isVisible()


def test_cancel_after_an_edit_elsewhere_keeps_that_edit(app, own_fd,
                                                       monkeypatch):
    """The window is not modal: an edit made in the grid between this
    window's edits stays, and its history too; Cancel adds one step that
    puts back what the window changed."""
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = own_fd
    opened = bytearray(s.binfile.data)
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    k = _enabled_keys(d, 1)[0]
    _untick(app, d, k)
    s.edit_bytes(0x10, bytes([opened[0x10] ^ 0xFF]))     # elsewhere
    d.cancel()
    want = bytearray(opened)
    want[0x10] ^= 0xFF
    assert bytes(s.binfile.data) == bytes(want)
    assert len(s.undo_stack) == n0 + 3
    assert s.undo_stack.undo_label().startswith("cancel")
    assert d.dtc.is_enabled(d.dtc.paths[k])


def test_restore_and_copy_from_comparison_are_taken_back_too(
        app, own_fd, monkeypatch, isolated_qsettings):
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = own_fd
    original = s.sibling(bytes(s.binfile.data), "FD - as read")
    d = DtcDialog(s, compare=original)
    d.show()
    keys = _enabled_keys(d, 2)
    for k in keys:
        _untick(app, d, k)
    d.apply()
    applied = bytes(s.binfile.data)
    n1 = len(s.undo_stack)
    d._copy_all_from_cmp()                  # Manual back to the original's
    assert d.dtc.is_enabled(d.dtc.paths[keys[0]]) and d.has_pending()
    d._discard()                            # what Cancel does before closing
    assert bytes(s.binfile.data) == applied and len(s.undo_stack) == n1
    assert not d.dtc.is_enabled(d.dtc.paths[keys[0]])
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (FD, "")))
    d._restore_stock()                      # every dataset back to stock
    assert all(d.dtc.is_enabled(o)
               for o in d.dtc.siblings(d.dtc.paths[keys[0]]))
    assert d.has_pending()
    d.cancel()
    assert bytes(s.binfile.data) == applied
    assert len(s.undo_stack) == n1


def test_edc16_lamp_and_class_edits_cancel_exactly(app, monkeypatch):
    _need(LQ)
    _no_question(monkeypatch)
    from ovtsuite.gui_dtc import DtcDialog
    s = TunerSession(LQ)
    opened = bytes(s.binfile.data)
    n0 = len(s.undo_stack)
    d = DtcDialog(s)
    d.show()
    col = d._col["mil"]
    r = next(i for i in range(d.table.rowCount())
             if d.table.item(i, col).flags()
             & QtCore.Qt.ItemFlag.ItemIsUserCheckable
             and d.table.item(i, col).checkState() == QtCore.Qt.CheckState.Checked)
    d.table.item(r, col).setCheckState(QtCore.Qt.CheckState.Unchecked)
    app.processEvents()
    fp = next(d.dtc.paths[k] for i, k in enumerate(_keys(d.table))
              if i != r and d.dtc.paths[k].current_class)
    d._set_class([fp], 0)                   # the context menu's "0: off"
    assert len(s.undo_stack) == n0 + 2 and d.has_pending()
    d.cancel()
    assert bytes(s.binfile.data) == opened and len(s.undo_stack) == n0
