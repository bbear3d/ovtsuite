# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""#27: the ZF 8HP "off" markers (10000) in the grid's converted Z modes.

Shown as "off", never scaled; typing or pasting "off" writes the marker back;
add/sub/mul/div leave it alone; a typed speed still converts.
"""
import os

import pytest

from ovtsuite import gui as G
from ovtsuite.session import TunerSession

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not G.HAVE_QT, reason="PySide6 not available")]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZF8HP = os.path.join(ROOT, "Reference", "ZF8HP", "OEM", "8K0927158K__1014.frf.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path):
    if not os.path.exists(ZF8HP):
        pytest.skip("no ZF 8HP reference binary")
    from PySide6 import QtCore
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    w = G.MainWindow(TunerSession(ZF8HP), None)
    yield w
    w.close_discarding()


def _wk(win, mode):
    """A WK table with an off cell and a real one, shown in ``mode``."""
    for e in win.primary.entities:
        if not getattr(e, "z_speed", False):
            continue
        vals = e.values()
        off = [(r, c) for r in range(e.rows) for c in range(e.cols)
               if vals[r][c] == 10000]
        real = [(r, c) for r in range(e.rows) for c in range(e.cols)
                if 0 < vals[r][c] < 10000
                and (e.col_ratios[c] if e.col_ratios else 1.0) != 1.0]
        if off and real:
            win.model.set_entity(e, None)
            win.current._zmode_pref = mode
            win._sync_zmode()
            return e, off[0], real[0]
    pytest.skip("no speed map with both an off and a real cell")


def _idx(win, r, c):
    m = win.model
    return m.index(r + m._roff, c + m._coff)


def _text(win, r, c, role=None):
    from PySide6 import QtCore
    return win.model.data(_idx(win, r, c), role or QtCore.Qt.DisplayRole)


@pytest.mark.parametrize("mode", ["engine", "kmh", "mph"])
def test_off_cells_show_as_off_in_converted_modes(win, mode):
    e, (ro, co), (rr, cr) = _wk(win, mode)
    assert win.model.zmode == mode
    assert _text(win, ro, co) == "off"
    assert _text(win, rr, cr) not in ("off", "%g" % e.values()[rr][cr])


def test_off_cells_show_stored_value_as_stored(win):
    e, (ro, co), _ = _wk(win, "output")
    assert _text(win, ro, co) == "10000"


def test_typing_off_and_speeds(win):
    from PySide6 import QtCore
    e, (ro, co), (rr, cr) = _wk(win, "engine")
    m = win.model
    # a speed typed into an off cell switches it on, in engine rpm
    assert m.setData(_idx(win, ro, co), "3000", QtCore.Qt.EditRole)
    stored = e.values()[ro][co]
    assert stored != 10000 and abs(stored * e.col_ratios[co] - 3000) < e.col_ratios[co]
    # "off" puts the marker back, unscaled
    assert m.setData(_idx(win, ro, co), "off", QtCore.Qt.EditRole)
    assert e.values()[ro][co] == 10000


def test_copy_paste_keeps_off(win):
    e, (ro, co), (rr, cr) = _wk(win, "engine")
    m = win.model
    text = m.copy_tsv([_idx(win, ro, co)])
    assert text == "off"
    before = e.values()[rr][cr]
    assert m.paste_tsv(_idx(win, rr, cr), text) == 1
    assert e.values()[rr][cr] == 10000
    win.primary.undo()
    assert e.values()[rr][cr] == before


def test_arithmetic_skips_off_cells(win):
    e, (ro, co), (rr, cr) = _wk(win, "engine")
    m = win.model
    before = e.values()[rr][cr]
    n = m.apply_op([_idx(win, ro, co), _idx(win, rr, cr)], "add", 200)
    assert n == 1
    assert e.values()[ro][co] == 10000
    assert e.values()[rr][cr] != before
