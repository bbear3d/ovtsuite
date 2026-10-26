# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The gearbox fault-code window (ovtsuite.gui_tcu_dtc): Apply, Cancel and OK.

Edits are written as they are made; Apply folds those since the last Apply
into one undo step, Cancel puts back exactly the bytes they changed, OK
applies and closes, Esc and the title bar's X ask when edits are not applied.
Checked on a ZF 6HP reference image (two coding variants, codes switched by
class), skipped when absent.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore  # noqa: E402

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from conftest import ZF6HP_REFERENCE as REF6
except Exception:                           # pragma: no cover
    REF6 = os.path.join(ROOT, "Reference", "ZF6HP")
STOCK = os.path.join(REF6, "5.0TFSI-Stock.bin")
#: windows (and their sessions) kept alive to the end of the run
_KEEP = []


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def s():
    if not os.path.isfile(STOCK):
        pytest.skip("reference image not present: %s" % STOCK)
    from ovtsuite.session import TunerSession
    return TunerSession(STOCK)


def _no_question(monkeypatch):
    def ask(*a, **k):
        raise AssertionError("asked: %r" % (a[2] if len(a) > 2 else a,))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(ask))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(ask))


def _window(s, compare=None):
    from ovtsuite.gui_tcu_dtc import open_for
    d = open_for(s, compare=compare)
    _KEEP.append((s, compare, d))
    d._confirm = lambda text: True          # "other codes share the class"
    d._tell = lambda text: None
    d.show()
    return d


def _keys(d):
    return [d.table.item(i, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            for i in range(d.table.rowCount())]


def _on(d, key):
    return {r.key: r for r in d.model.rows(d._vi())}[key].on


def _distinct_on(d, n):
    """``n`` switchable rows that are on, each in a different class (so
    switching one does not switch the next)."""
    out, groups = [], set()
    for r in sorted(d.model.rows(d._vi()), key=lambda r: str(r.sort)):
        if r.on and r.can_switch and r.group not in groups:
            groups.add(r.group)
            out.append(r.key)
            if len(out) == n:
                break
    assert len(out) == n
    return out


def _untick(app, d, key):
    d.table.item(_keys(d).index(key), 0).setCheckState(
        QtCore.Qt.CheckState.Unchecked)
    app.processEvents()                     # the switch is queued


def test_buttons_are_apply_cancel_ok_in_that_order(app, s):
    d = _window(s)
    texts = [b.text() for b in d.findChildren(QtWidgets.QPushButton)]
    assert "Close" not in texts
    for keep in ("Disable selected", "Enable selected",
                 "Restore all from stock..."):
        assert keep in texts
    row = next(lay for lay in d.layout().children()
               if isinstance(lay, QtWidgets.QHBoxLayout)
               and lay.indexOf(d.apply_btn) >= 0)
    idx = [row.indexOf(b) for b in (d.apply_btn, d.cancel_btn, d.ok_btn)]
    assert idx == sorted(idx) and idx[2] == row.count() - 1
    assert [d.apply_btn.text(), d.cancel_btn.text(), d.ok_btn.text()] == \
        ["Apply", "Cancel", "OK"]
    assert not d.apply_btn.isEnabled()      # nothing to apply yet
    d.cancel()


def test_cancel_after_apply_takes_back_only_the_later_edits(app, s,
                                                          monkeypatch):
    _no_question(monkeypatch)
    from ovtsuite.gui_tcu_dtc import PENDING_BG
    orig = bytes(s.binfile.data)
    n0 = len(s.undo_stack)
    d = _window(s)
    k1, k2, k3 = _distinct_on(d, 3)
    _untick(app, d, k1)
    d.table.clearSelection()
    d.table.selectRow(_keys(d).index(k2))
    d._set_selected(False)                  # the bulk button's path
    assert len(s.undo_stack) == n0 + 2      # written as they are made
    assert not _on(d, k1) and not _on(d, k2)
    assert d.has_pending() and d.apply_btn.isEnabled()
    assert "changed, not applied" in d.pending_lbl.text()
    cell = d.table.item(_keys(d).index(k1), 1)
    assert cell.background().color() == PENDING_BG and cell.font().bold()
    d.apply_btn.click()
    assert len(s.undo_stack) == n0 + 1      # one undo step per Apply
    assert s.undo_stack.undo_label() == "edit fault codes"
    assert not d.has_pending() and d.pending_lbl.text() == ""
    cell = d.table.item(_keys(d).index(k1), 1)
    assert cell.background().color() != PENDING_BG
    applied = bytes(s.binfile.data)
    _untick(app, d, k3)
    assert bytes(s.binfile.data) != applied and d.has_pending()
    d.cancel_btn.click()
    assert not d.isVisible()
    assert bytes(s.binfile.data) == applied  # exactly as at the Apply
    assert len(s.undo_stack) == n0 + 1 and not s.can_redo
    assert _on(d, k3) and not _on(d, k1)
    s.undo()                                # the Apply, as one step
    assert bytes(s.binfile.data) == orig


def test_cancel_with_no_apply_restores_the_file_as_opened(app, s,
                                                         monkeypatch):
    _no_question(monkeypatch)
    s.edit_bytes(0x10, bytes([s.binfile.data[0x10] ^ 0xFF]))  # an earlier edit
    opened = bytes(s.binfile.data)
    n0 = len(s.undo_stack)
    d = _window(s)
    for k in _distinct_on(d, 2):
        _untick(app, d, k)
    assert bytes(s.binfile.data) != opened
    d.cancel()
    assert bytes(s.binfile.data) == opened
    assert len(s.undo_stack) == n0          # the user's own history is kept


def test_ok_applies_and_closes(app, s, monkeypatch):
    _no_question(monkeypatch)
    n0 = len(s.undo_stack)
    d = _window(s)
    keys = _distinct_on(d, 2)
    for k in keys:
        _untick(app, d, k)
    d.ok_btn.click()
    assert not d.isVisible() and not d.has_pending()
    assert len(s.undo_stack) == n0 + 1
    assert not any(_on(d, k) for k in keys)


def test_switching_back_leaves_no_undo_step(app, s, monkeypatch):
    _no_question(monkeypatch)
    n0 = len(s.undo_stack)
    opened = bytes(s.binfile.data)
    d = _window(s)
    k = _distinct_on(d, 1)[0]
    _untick(app, d, k)
    d.table.item(_keys(d).index(k), 0).setCheckState(
        QtCore.Qt.CheckState.Checked)
    app.processEvents()
    assert d.pending_lbl.text() == ""       # nothing differs any more
    d.apply()
    assert len(s.undo_stack) == n0
    assert bytes(s.binfile.data) == opened
    d.close()


def test_x_and_esc_ask_when_edits_are_not_applied(app, s, monkeypatch):
    SB = QtWidgets.QMessageBox.StandardButton
    opened = bytes(s.binfile.data)
    d = _window(s)
    _untick(app, d, _distinct_on(d, 1)[0])
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


def test_x_with_answer_apply_keeps_the_edits(app, s, monkeypatch):
    SB = QtWidgets.QMessageBox.StandardButton
    n0 = len(s.undo_stack)
    d = _window(s)
    k = _distinct_on(d, 1)[0]
    _untick(app, d, k)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: SB.Apply))
    d.reject()
    assert not d.isVisible() and not _on(d, k)
    assert len(s.undo_stack) == n0 + 1


def test_x_with_nothing_pending_closes_without_asking(app, s, monkeypatch):
    _no_question(monkeypatch)
    d = _window(s)
    _untick(app, d, _distinct_on(d, 1)[0])
    d.apply()
    d.close()
    assert not d.isVisible()


def test_cancel_after_an_edit_elsewhere_keeps_that_edit(app, s, monkeypatch):
    """The window is not modal: an edit made in the grid between this
    window's edits stays, and its history too; Cancel adds one step that
    puts back what the window changed."""
    _no_question(monkeypatch)
    opened = bytearray(s.binfile.data)
    n0 = len(s.undo_stack)
    d = _window(s)
    k = _distinct_on(d, 1)[0]
    _untick(app, d, k)
    s.edit_bytes(0x10, bytes([opened[0x10] ^ 0xFF]))     # elsewhere
    d.cancel()
    want = bytearray(opened)
    want[0x10] ^= 0xFF
    assert bytes(s.binfile.data) == bytes(want)
    assert len(s.undo_stack) == n0 + 3
    assert s.undo_stack.undo_label().startswith("cancel")
    assert _on(d, k)


def test_restore_and_copy_from_comparison_are_taken_back_too(
        app, s, monkeypatch, isolated_qsettings):
    _no_question(monkeypatch)
    original = s.sibling(bytes(s.binfile.data), "6HP - as read")
    d = _window(s, compare=original)
    keys = _distinct_on(d, 2)
    for k in keys:
        _untick(app, d, k)
    d.apply()
    applied = bytes(s.binfile.data)
    n1 = len(s.undo_stack)
    d._copy_all_from_cmp()                  # back to the original's states
    assert all(_on(d, k) for k in keys) and d.has_pending()
    d._discard()                            # what Cancel does before closing
    assert bytes(s.binfile.data) == applied and len(s.undo_stack) == n1
    assert not any(_on(d, k) for k in keys)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (STOCK, "")))
    d._restore_stock()                      # every class back to stock
    assert all(_on(d, k) for k in keys) and d.has_pending()
    d.cancel()
    assert bytes(s.binfile.data) == applied
    assert len(s.undo_stack) == n1


def test_cell_colours_follow_the_table_editor(app, s, monkeypatch):
    """As the engine fault-code window and the table editor: a state that
    differs from the comparison orange (gui.DIFF_BG, dark text) in the On box
    and Cmp on; a row not applied yet PENDING_BG, bold, dark text -- over
    orange in On, while Cmp on stays orange; a code the gearbox has stored
    STORED_BG under both; alternating rows so the selection shows."""
    _no_question(monkeypatch)
    from types import SimpleNamespace
    from ovtsuite import gui
    from ovtsuite.gui_cellmarks import CellDelegate, STORED_BG
    from ovtsuite.gui_tcu_dtc import PENDING_BG
    original = s.sibling(bytes(s.binfile.data), "6HP - as read")
    d = _window(s, compare=original)
    rows = {r.key: r for r in d.model.rows(d._vi())}
    k1, k2, k3 = [k for k in _distinct_on(d, 8) if rows[k].vag_numbers][:3]
    _untick(app, d, k1)
    d.apply()                               # k1 differs, applied
    _untick(app, d, k2)                     # k2 differs, not applied
    d._ecu_codes = {rows[k].vag_numbers[0]: SimpleNamespace(
        code=rows[k].vag_numbers[0], text="stored", status=0x28)
        for k in (k1, k3)}
    d._populate()
    cmp_col = d._cols.index("Cmp on")
    dark = gui.CELL_DARK_FG

    def cell(k, c):
        return d.table.item(_keys(d).index(k), c)

    for c in (0, cmp_col):                  # the primary's state, the comparison's
        assert cell(k1, c).background().color() == gui.DIFF_BG
        assert cell(k1, c).foreground().color() == dark
    assert cell(k1, 1).background().color() == STORED_BG
    for c in range(d.table.columnCount()):
        it = cell(k2, c)
        if c == cmp_col:
            assert it.background().color() == gui.DIFF_BG
        else:
            assert it.background().color() == PENDING_BG and it.font().bold()
            assert it.foreground().color() == dark
        assert cell(k3, c).background().color() == STORED_BG
    for r in range(d.table.rowCount()):
        for c in range(d.table.columnCount()):
            assert d.table.item(r, c).background().color() != gui.EDIT_BG
    assert d.table.alternatingRowColors()
    assert isinstance(d.table.itemDelegate(), CellDelegate)
    d.cancel()


def test_check_box_on_a_selected_row_stays_readable(app, s):
    """The TCU window's "On" boxes use the shared delegate: on a selected
    row the box is drawn unselected, in the table's own palette."""
    from ovtsuite.gui_cellmarks import CellDelegate, check_on_selection
    d = _window(s)
    t = d.table
    dlg = t.itemDelegate()
    assert isinstance(dlg, CellDelegate)
    S = QtWidgets.QStyle.StateFlag
    t.selectRow(0)
    app.processEvents()
    ix = t.model().index(0, 0)
    o = QtWidgets.QStyleOptionViewItem()
    t.initViewItemOption(o)
    o.rect = t.visualRect(ix)
    o.state |= S.State_Selected
    dlg.initStyleOption(o, ix)
    got = check_on_selection(o, ix)
    assert got is not None
    chip, ind = got
    assert not ind.state & S.State_Selected
    assert ind.state & (S.State_On | S.State_Off)
    assert chip.contains(ind.rect)
    d.close()
