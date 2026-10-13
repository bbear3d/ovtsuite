# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison block's axis bands, and the axis column's width.

Two things Bear reported on the grid with a comparison loaded:

* the axis column was hundreds of pixels wide. The comparison banner is
  spanned across the grid but its text lives in column 0, and Qt sized that
  column to fit the whole sentence;
* the comparison had no X-axis row under its banner, so it could not be read
  once scrolled, and its left band always showed the PRIMARY's Y breakpoints
  -- wrong whenever the two maps are the same size with different axes, and
  wrong again (Y where X belongs) with the axes swapped.
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

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
RESAMPLED = "AccPed_dnDOSetP_MAP"      # 6x6 over a 5x6: values resampled
MISMATCH = "AccPed_trqEngLim_MAP"      # same 7x6, different breakpoints


@pytest.fixture(scope="module")
def win(tmp_path_factory, isolated_qsettings_module):
    for p in (LQ, KN):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    w = gui.MainWindow(TunerSession(LQ), TunerSession(KN))
    w.resize(1200, 760)
    w.show()
    app.processEvents()
    yield w
    w.close_discarding()


def _show(win, name, transposed=False):
    ws = win.current
    e = next(e for e in ws.primary.entities if e.name == name)
    ws.show_entity(e)
    if ws.model.transposed != transposed:
        ws._toggle_transpose()
    QtWidgets.QApplication.processEvents()
    return ws, e, ws.primary.counterpart(ws.comparison, e)


def _row(m, r):
    return [m.data(m.index(r, c)) for c in range(m._left, m.columnCount())]


def _col(m, c, rows):
    return [m.data(m.index(r, c)) for r in rows]


def _fmt(vals):
    return ["%.3f" % v for v in vals]


def test_the_banner_does_not_set_the_axis_column_width(win):
    ws, _e, _cp = _show(win, RESAMPLED)
    m = ws.model
    sep = m.sep_row()
    assert m.data(m.index(sep, 0), QtCore.Qt.SizeHintRole) == QtCore.QSize(0, 0)
    assert ws.table.columnWidth(0) < 150, \
        "axis column %dpx: sized to the banner again" % ws.table.columnWidth(0)


def test_the_full_banner_is_in_its_tooltip(win):
    ws, e, _cp = _show(win, RESAMPLED)
    m = ws.model
    tip = m.data(m.index(m.sep_row(), 0), QtCore.Qt.ToolTipRole)
    assert e.name in tip and "resampled" in tip


def test_a_resampled_comparison_repeats_the_primary_axes(win):
    """Its values were put onto the primary's breakpoints, so those label it."""
    ws, e, _cp = _show(win, RESAMPLED)
    m = ws.model
    assert m.overlay.mode == "resampled"
    sep = m.sep_row()
    assert _row(m, sep + 1) == _fmt(e.x_values())
    assert _col(m, 0, range(sep + 2, m.rowCount())) == _fmt(e.y_values())


def test_same_size_different_axes_shows_the_comparisons_own(win):
    """Each value sits in the cell it is stored in -- a different operating
    point -- so the band must say which."""
    ws, e, cp = _show(win, MISMATCH)
    m = ws.model
    assert m.overlay.mode == "axis-mismatch"
    sep = m.sep_row()
    assert _row(m, sep + 1) == _fmt(cp.x_values())
    assert _row(m, sep + 1) != _fmt(e.x_values())
    assert _col(m, 0, range(sep + 2, m.rowCount())) == _fmt(cp.y_values())


def test_swapped_axes_swap_the_comparison_bands_too(win):
    ws, e, cp = _show(win, MISMATCH, transposed=True)
    m = ws.model
    sep = m.sep_row()
    assert _row(m, sep + 1) == _fmt(cp.y_values())          # across: Y
    assert _col(m, 0, range(sep + 2, m.rowCount())) == _fmt(cp.x_values())


def test_the_comparison_bands_are_not_editable(win):
    ws, _e, _cp = _show(win, MISMATCH)
    m = ws.model
    sep = m.sep_row()
    for ix in (m.index(sep + 1, 1), m.index(sep + 2, 0), m.index(sep + 1, 0)):
        assert not (m.flags(ix) & QtCore.Qt.ItemIsEditable)
