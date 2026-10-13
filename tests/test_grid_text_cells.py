# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Parameters shown as characters (ASCII) or bytes (hex) in the grid.

Bear's reports: typing "2" into an ASCII cell saved "." -- the digit went
down the number path and stored byte 0x02 -- and the comparison half showed
those cells as decimal. Digging in turned up the rest pinned here: pasting
had the same digit bug (and dropped letters, and read hex bytes as decimal),
and a 1-D table stored 12x1 in one file and 1x12 in the other compared as its
first character repeated.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow

PRIMARY = os.path.join(ZF6HP_REFERENCE, "3.0TFSI-Stock.bin")
OTHER = os.path.join(ZF6HP_REFERENCE, "3.0TFSI-Stage-1.bin")
SWNR = "kid_VAG_SWNr"          # 12 characters, 1x12
DATKEN = "kid_VAG_DatKen"      # 2 characters; differs between the two


@pytest.fixture(scope="module")
def win(tmp_path_factory, isolated_qsettings_module):
    for p in (PRIMARY, OTHER):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    w = gui.MainWindow(TunerSession(PRIMARY), TunerSession(OTHER))
    w.show()
    app.processEvents()
    yield w
    w.close_discarding()


@pytest.fixture
def swnr(win):
    """The software-number field on screen, put back as found afterwards."""
    ws = win.current
    e = next(e for e in ws.primary.entities if e.name == SWNR)
    ws.show_entity(e)
    before = e.values()
    yield ws, e
    while ws.primary.modified:
        ws.primary.undo()
    ws.model.refresh()
    assert e.values() == before


def _cells(e):
    return [e.cell(r, c) for r in range(e.rows) for c in range(e.cols)]


def _text(e):
    return "".join(chr(int(round(v))) for v in _cells(e))


def _index(m, r, c):
    """Grid index of entity cell (r, c) in the primary half."""
    for gr in range(m.rowCount()):
        for gc in range(m.columnCount()):
            ix = m.index(gr, gc)
            if m.z_at(ix) == (r, c):
                return ix
    raise AssertionError("no grid cell for (%d, %d)" % (r, c))


def test_a_typed_digit_is_stored_as_its_character(swnr):
    ws, e = swnr
    m = ws.model
    ix = _index(m, 0, 0)
    assert m.setData(ix, "2", QtCore.Qt.EditRole)
    assert e.cell(0, 0) == 0x32, "a typed 2 must be the character '2'"
    assert m.data(ix, QtCore.Qt.DisplayRole) == "2"


def test_a_typed_letter_is_stored_as_its_character(swnr):
    ws, e = swnr
    m = ws.model
    assert m.setData(_index(m, 0, 3), "Z", QtCore.Qt.EditRole)
    assert e.cell(0, 3) == ord("Z")


def test_typing_a_string_fills_the_following_cells_in_one_undo(swnr):
    ws, e = swnr
    m = ws.model
    before = _text(e)
    assert m.setData(_index(m, 0, 0), "4E0910156C", QtCore.Qt.EditRole)
    assert _text(e) == "4E0910156C" + before[10:]
    ws.primary.undo()
    assert _text(e) == before, "the whole string is one undo step"


def test_a_string_longer_than_the_field_is_cut_at_its_end(swnr):
    ws, e = swnr
    m = ws.model
    n = e.rows * e.cols
    assert m.setData(_index(m, 0, n - 2), "XYZ", QtCore.Qt.EditRole)
    assert _text(e)[-2:] == "XY"


def test_a_raw_byte_can_still_be_written(swnr):
    ws, e = swnr
    m = ws.model
    ix = _index(m, 0, 11)
    assert m.setData(ix, "0x00", QtCore.Qt.EditRole)
    assert e.cell(0, 11) == 0
    assert m.data(ix, QtCore.Qt.DisplayRole) == "."


def test_copy_and_paste_round_trips_characters_and_raw_bytes(swnr):
    ws, e = swnr
    m = ws.model
    m.setData(_index(m, 0, 11), "0x00", QtCore.Qt.EditRole)
    row = [_index(m, 0, c) for c in range(e.cols)]
    text = m.copy_tsv(row)
    assert "0x00" in text, "an unprintable byte copies as itself"
    good = _cells(e)
    m.setData(_index(m, 0, 0), "ZZZZZZZZZZZZ", QtCore.Qt.EditRole)
    m.paste_tsv(row[0], text)
    assert _cells(e) == good


def test_the_comparison_half_shows_characters(win):
    ws = win.current
    e = next(e for e in ws.primary.entities if e.name == DATKEN)
    ws.show_entity(e)
    m = ws.model
    cp = ws.primary.counterpart(ws.comparison, e)
    sep = m.sep_row()
    assert sep is not None
    shown = "".join(m.data(m.index(r, c), QtCore.Qt.DisplayRole) or ""
                    for r in range(sep + 1, m.rowCount())
                    for c in range(m._left, m.columnCount())
                    if m._kind(r, c)[0] == "cz")
    want = "".join(chr(int(round(v))) if 0x20 <= int(round(v)) <= 0x7E
                   else "." for v in _cells(cp))
    assert shown == want, "comparison shown as %r, not %r" % (shown, want)
    tip = m.data(m.index(sep, 0), QtCore.Qt.ToolTipRole)
    assert want.strip() in tip


class _Run:
    """A 1-D table stood on its side or laid flat -- the two ways the
    reference definitions store one (the ZF 5.0 and 3.0 part numbers are
    12x1 and 1x12)."""
    kind = "curve"

    def __init__(self, vals, flat):
        self._v = list(vals)
        self.rows, self.cols = (1, len(vals)) if flat else (len(vals), 1)

    def values(self):
        return [self._v] if self.rows == 1 else [[v] for v in self._v]

    def cell(self, r, c):
        return self._v[c if self.rows == 1 else r]

    def x_values(self):
        return []

    def y_values(self):
        return []


def test_a_column_and_a_row_of_the_same_field_compare_cell_for_cell():
    """12x1 against 1x12 is one run of values either way -- not a resample
    that smears the first character across every cell."""
    from ovtsuite.transfer import build_overlay
    mine = _Run(b"4F9910156A  ", flat=True)
    theirs = _Run(b"4E0910156C  ", flat=False)
    ov = build_overlay(mine, theirs)
    assert ov.mode == "direct"
    assert bytes(int(v) for v in ov.values[0]) == b"4E0910156C  "
    # and the other way round
    ov = build_overlay(theirs, mine)
    assert bytes(int(row[0]) for row in ov.values) == b"4F9910156A  "


def test_a_hex_cell_takes_and_pastes_a_hex_byte(win, swnr, monkeypatch):
    ws, e = swnr
    monkeypatch.setattr(e, "text_kind", "hex", raising=False)
    m = ws.model
    ix = _index(m, 0, 0)
    assert m.setData(ix, "6C", QtCore.Qt.EditRole)
    assert e.cell(0, 0) == 0x6C
    m.paste_tsv(ix, "60")
    assert e.cell(0, 0) == 0x60, "a pasted hex byte was read as decimal"
    assert not m.setData(ix, "1FF", QtCore.Qt.EditRole), "not one byte"


# -- a 1-D table is one run of values, however its definition stores it ------

def _gui():
    from ovtsuite import gui
    return gui


def test_a_run_has_one_kind_and_shape_whichever_way_it_is_stored():
    G = _gui()
    col, row = _Run(b"4F9910156A  ", flat=False), _Run(b"4F9910156A  ", flat=True)
    for e in (col, row):
        assert G._is_run(e)
        assert G._kind_text(e) == "Curve"
        assert G._shape_text(e) == "1 x 12"             # along X by default
    # the grid's flag is relative to storage: shown along X either way here
    assert G._shape_text(col, transposed=True) == "1 x 12"
    assert G._shape_text(row, transposed=False) == "1 x 12"
    assert G._shape_text(col, transposed=False) == "12 x 1"   # swapped


def test_the_text_of_a_column_is_all_of_it():
    """Row 0 of a 12x1 field is its first character; the title showed "4"."""
    G = _gui()
    col = _Run(b"4F9910156A  ", flat=False)
    col.text_kind = "ascii"
    assert G._ascii_of(col) == "4F9910156A"


def test_turned_runs_pair_cell_for_cell_and_count_their_differences():
    G = _gui()
    mine = _Run(b"4F9910156A", flat=True)
    theirs = _Run(b"4E0910156C", flat=False)
    pairs = G._cell_pairs(mine, theirs)
    assert pairs[3] == ((0, 3), (3, 0))
    n, first = G.DiffListDialog._summarize(None, mine, theirs)
    assert n == 3                                   # F/E, 9/0, A/C
    assert first.startswith("[1] ")


def test_runs_of_different_length_do_not_pair():
    G = _gui()
    assert G._cell_pairs(_Run(b"12345", flat=True),
                         _Run(b"1234", flat=False)) is None


@pytest.fixture(scope="module")
def stored_as_column(win):
    """The 5.0's part number is defined 12x1 (the 3.0's is 1x12)."""
    path = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")
    if not os.path.exists(path):
        pytest.skip("reference binary missing: %s" % path)
    ws = win.new_tab()
    ws._quiet = True                    # no modal "file loaded" summary
    try:
        assert ws._load_primary(path)
    finally:
        ws._quiet = False
    e = next(e for e in ws.primary.entities if e.name == SWNR)
    assert (e.rows, e.cols) == (12, 1)
    return ws, e


def test_the_title_describes_what_is_shown(stored_as_column):
    ws, e = stored_as_column
    ws.show_entity(e)
    title = ws.title.text()
    assert "[Curve  1 x 12]" in title, title
    assert '"%s"' % _gui()._ascii_of(e) in title and len(_gui()._ascii_of(e)) > 1
    assert (ws.model.rowCount(), ws.model.columnCount()) == (1, 12)
    ws._toggle_transpose()
    assert "[Curve  12 x 1]" in ws.title.text()
    assert (ws.model.rowCount(), ws.model.columnCount()) == (12, 1)
    ws._toggle_transpose()
