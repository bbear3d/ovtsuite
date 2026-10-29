# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Copy and paste in the dialogs' small tables (ovtsuite.gui_cellmarks
``install_clipboard``): Ctrl+C / Ctrl+V and the right-click menu, and the
gear-ratio and VSS dialogs built on it."""
import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui  # noqa: E402

from ovtsuite import gui_cellmarks as cm  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _table(rows=3, cols=3, editable=(1, 2)):
    t = QtWidgets.QTableWidget(rows, cols)
    for r in range(rows):
        for c in range(cols):
            it = QtWidgets.QTableWidgetItem("%d.%d" % (r, c))
            if c not in editable:
                it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            t.setItem(r, c, it)
    return t


def test_parse_tsv():
    assert cm.parse_tsv("1\t2\r\n3\t4\n\n5") == [["1", "2"], ["3", "4"], ["5"]]
    assert cm.parse_tsv("") == []


def test_paste_anchor_is_top_left_of_the_selection(app):
    t = _table()
    assert cm.paste_anchor(t) is None
    t.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(1, 1, 2, 2), True)
    assert cm.paste_anchor(t) == (1, 1)


def test_copy_takes_the_whole_selection(app):
    t = _table()
    cm.install_clipboard(t)
    t.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(0, 1, 1, 2), True)
    assert cm.copy_to_clipboard(t) == "0.1\t0.2\n1.1\t1.2"
    assert QtWidgets.QApplication.clipboard().text() == "0.1\t0.2\n1.1\t1.2"


def test_shortcuts_are_installed_on_the_table(app):
    t = _table()
    cm.install_clipboard(t, paste=lambda r, c, g: 0)
    keys = {a.shortcut().toString() for a in t.actions()}
    assert {"Ctrl+C", "Ctrl+V"} <= keys
    ro = _table()
    cm.install_clipboard(ro)                   # read-only: copy only
    assert {a.shortcut().toString() for a in ro.actions()} == {"Ctrl+C"}


def test_paste_callback_gets_the_anchor_and_the_clipboard_rows(app):
    t = _table()
    got = []
    cm.install_clipboard(t, paste=lambda r, c, g: got.append((r, c, g)) or 2)
    t.setCurrentCell(1, 1)
    QtWidgets.QApplication.clipboard().setText("a\tb\nc\td")
    (v,) = [a for a in t.actions() if a.shortcut().toString() == "Ctrl+V"]
    v.trigger()
    assert got == [(1, 1, [["a", "b"], ["c", "d"]])]


def test_has_editable(app):
    assert cm.has_editable(_table())
    assert not cm.has_editable(_table(editable=()))


# ---------------------------------------------------------------- gear ratios
class _Ent:
    """One single-cell constant."""
    def __init__(self, name, addr, v):
        self.name, self.addr, self.v = name, addr, v

    def cell(self, r, c):
        return self.v


class _Sess:
    def __init__(self):
        self.calls = []

    def edit_values(self, pairs, label="edit"):
        self.calls.append((label, [(e.name, v) for e, v in pairs]))
        for e, v in pairs:
            e.v = v


def _gear_dialog(app, mode="ratio"):
    """A GearRatioDialog shell with just what its paste path uses."""
    from ovtsuite import gui
    d = gui.GearRatioDialog.__new__(gui.GearRatioDialog)
    QtWidgets.QDialog.__init__(d)
    d.session = _Sess()
    d.vn_tbl = QtWidgets.QTableWidget(2, 3)
    d.abs_tbl = QtWidgets.QTableWidget(2, 2)
    d.copy_all = QtWidgets.QCheckBox()
    d.copies = [object()]
    d.pbn = {}
    d.gears = type("G", (), {"copies": lambda self: [object()]})()
    d._loading = False
    d._populate = lambda: None
    d._vn_kind = lambda: mode
    d.vn_ref = type("R", (), {"value": lambda self: 1000.0})()
    return d


def _fill(tbl, d, col_of_ents, texts):
    for r, row in enumerate(col_of_ents):
        for c, ent in row.items():
            it = QtWidgets.QTableWidgetItem(texts[r][c])
            it.setData(QtCore.Qt.UserRole, ent)
            it.setData(QtCore.Qt.UserRole + 1, ent.name)
            it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled
                        | QtCore.Qt.ItemIsEditable)
            tbl.setItem(r, c, it)


def test_gear_paste_is_one_undo_step_and_skips_labels(app):
    d = _gear_dialog(app)
    a, b, c = _Ent("A", 1, 1.0), _Ent("B", 2, 2.0), _Ent("C", 3, 3.0)
    lbl = QtWidgets.QTableWidgetItem("label")
    d.abs_tbl.setItem(0, 0, lbl)
    _fill(d.abs_tbl, d, [{1: a}, {1: b}], [["", "1.0"], ["", "2.0"]])
    n = d._paste(d.abs_tbl, 0, 0, [["x", "5.5"], ["y", "6.5"], ["z", "7"]])
    assert n == 2
    assert d.session.calls == [("paste gear ratios",
                                [("A", 5.5), ("B", 6.5)])]
    assert (a.v, b.v) == (5.5, 6.5)


def test_gear_paste_ignores_unreadable_text_and_unchanged_cells(app):
    d = _gear_dialog(app)
    a, b = _Ent("A", 1, 1.0), _Ent("B", 2, 2.0)
    _fill(d.abs_tbl, d, [{1: a}, {1: b}], [["", "1.0"], ["", "2.0"]])
    assert d._paste(d.abs_tbl, 0, 1, [["abc"], ["2.0"]]) == 0
    assert d.session.calls == []               # nothing written at all


def test_gear_paste_vn_cells_use_the_display_transform(app):
    d = _gear_dialog(app, mode="speed")        # shown as km/h at 1000 rpm
    lo = _Ent("LO", 1, 0.012)
    _fill(d.vn_tbl, d, [{1: lo}], [["", "12.0"]])
    assert d._paste(d.vn_tbl, 0, 1, [["24"]]) == 1
    assert d.session.calls[0][1] == [("LO", pytest.approx(0.024))]


def test_gear_paste_with_copy_to_all_writes_every_copy(app):
    d = _gear_dialog(app)
    a, a2 = _Ent("A", 1, 1.0), _Ent("A", 9, 1.0)
    cp1 = type("Cp", (), {"entity": lambda self, n, p: (a, False)})()
    cp2 = type("Cp", (), {"entity": lambda self, n, p: (a2, False)})()
    d.copies = [cp1, cp2]
    d.copy_all.setChecked(True)
    _fill(d.abs_tbl, d, [{1: a}], [["", "1.0"]])
    assert d._paste(d.abs_tbl, 0, 1, [["3"]]) == 2
    assert (a.v, a2.v) == (3.0, 3.0)


# ---------------------------------------------------------------------- VSS
class _VSess:
    def __init__(self):
        self.writes = []
        self.txn = []
        self.binfile = type("B", (), {"data": bytearray(64)})()

    @contextmanager
    def transaction(self, label):
        self.txn.append(label)
        yield

    def edit_bytes(self, addr, data, coalesce=False):
        if bytes(self.binfile.data[addr:addr + len(data)]) == bytes(data):
            return None                 # as the session: unchanged, no command
        self.writes.append((addr, bytes(data)))
        self.binfile.data[addr:addr + len(data)] = data
        return object()


def _vss_dialog(app, rows):
    from ovtsuite import gui
    d = gui.VssDialog.__new__(gui.VssDialog)
    QtWidgets.QDialog.__init__(d)
    d.session = _VSess()
    d._rows16 = rows
    d._reload = lambda: None
    d._after_write = lambda: None
    return d


def test_vss_pulses_raw_is_the_stored_form(app):
    from ovtsuite import gui
    assert gui.VssDialog._pulses_raw16(4000) == (900000000).to_bytes(4, "big")
    assert gui.VssDialog._pulses_raw16(0) == (0).to_bytes(4, "big")
    assert gui.VssDialog._pulses_raw16(0.001) is None     # does not fit 32 bits


def test_vss_paste_sets_pulses_and_source_in_one_step(app):
    rows = [{"addr": 0x10, "src_addr": 0x20, "styp_addr": 0x21, "primary": True},
            {"addr": 0x14, "src_addr": None, "primary": False}]
    d = _vss_dialog(app, rows)
    n = d._paste16(0, 2, [["2.000", "2000", "CAN"],
                          ["2.5", "bad", "sensor"]])
    s = d.session
    assert s.txn == ["VSS: paste"]
    # row 0: Imp/m 2.000 -> 2 pulses/m, stored as 3.6e9 / 2
    assert (0x10, (1800000000).to_bytes(4, "big")) in s.writes
    assert s.binfile.data[0x20] == 1 and s.binfile.data[0x21] == 2
    # row 1: Imp/m 2.5; no source switch of its own -> not touched
    assert (0x14, (1440000000).to_bytes(4, "big")) in s.writes
    assert n == 3          # row 0 pulses (its col 3 repeats it), source, row 1


def test_vss_paste_skips_role_address_and_comparison_columns(app):
    d = _vss_dialog(app, [{"addr": 0x10, "src_addr": None, "primary": True}])
    assert d._paste16(0, 0, [["primary", "0x10"]]) == 0
    assert d._paste16(0, 5, [["4000", "CAN"]]) == 0
    assert d.session.writes == []


# ------------------------------------------------------ VSS on EDC15 (typed)
def _vss15_dialog(app, blocks):
    from ovtsuite import gui
    d = gui.VssDialog.__new__(gui.VssDialog)
    QtWidgets.QDialog.__init__(d)
    d.session = _VSess()
    d._blocks15 = blocks
    d._reload = lambda: None
    d._after_write = lambda: None
    return d


def _block(name, addr, valid=True):
    return type("B", (), {"name": name, "addr": addr, "valid": valid})()


def test_vss15_cells_become_bytes_of_the_speed_block(app):
    import struct
    d = _vss15_dialog(app, [_block("fgwDA1", 0x10)])
    b = d._blocks15[0]
    assert d._write15(b, 4, "11000") == (0x12, struct.pack("<H", 11000))      # SF
    assert d._write15(b, 5, "13451") == (0x18, struct.pack("<H", 13451))      # TMX
    assert d._write15(b, 6, "4.5") == (0x22, struct.pack("<h", 270))          # IMP = x60
    assert d._write15(b, 4, "70000") is None and d._write15(b, 4, "abc") is None
    assert d._write15(b, 6, "600") is None                                    # s16


def test_vss15_only_valid_speed_blocks_are_editable(app):
    d = _vss15_dialog(app, [_block("fgwDA1", 0x10), _block("fgwKTG", 0x40),
                            _block("fgwDA2", 0x60, valid=False)])
    assert d._editable15(0, 4) and d._editable15(0, 6)
    assert not d._editable15(0, 3) and not d._editable15(1, 4) and not d._editable15(2, 5)


def test_vss15_paste_is_one_undo_step_and_skips_what_is_not_editable(app):
    d = _vss15_dialog(app, [_block("fgwDA1", 0x10), _block("fgwKTG", 0x40)])
    n = d._paste15(0, 3, [["x", "100", "200", "2"], ["y", "1", "2", "3"]])
    assert n == 3 and d.session.txn == ["VSS: paste"]
    assert {a for a, _ in d.session.writes} == {0x12, 0x18, 0x22}
