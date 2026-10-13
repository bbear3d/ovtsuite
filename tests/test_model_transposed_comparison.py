# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The transposed comparison grid, which crashed the app.

What happened: loading a ZF 6HP tune with a comparison and clicking a 1-D
table froze the UI, burned CPU for a minute, then died with a Windows access
violation inside ``resizeColumnsToContents()``.

The cause is entirely in the model's shape. ``_cmp`` holds the comparison in
the *entity's* orientation, exactly like ``_vals``, but a 1-D table is shown
transposed (``_default_transpose``), so its comparison block is one grid row
deep however many rows the entity has. ``rowCount`` added ``len(self._cmp)``
regardless, so a 256x1 curve claimed 258 rows by 256 columns -- and 255 of
those rows were not there. ``_kind`` mapped them to ``("cz", gc, gr-sep-1)``
and ``data()`` indexed ``self._cmp[gc][gr-sep-1]`` off the end of a
one-element row.

That is an IndexError on 65,280 of 66,048 cells. Raising it is not the
failure -- Qt calls ``data()`` from inside its own layout loop, so each one
unwinds through C++, and doing that 65,000 times during a column resize is
the hang, the CPU, and finally the access violation.

So two things are pinned here: the row count is right, and ``data()`` cannot
raise even when it is wrong.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import zf6hp_path                       # noqa: E402

from ovtsuite.session import TunerSession             # noqa: E402

STOCK = "3.0TFSI-Stock.bin"
STAGE1 = "3.0TFSI-Stage-1.bin"

QtCore = pytest.importorskip("PySide6.QtCore")


@pytest.fixture(scope="module")
def app():
    """A QApplication, or a skip where Qt cannot start.

    Importing QtWidgets needs the platform libraries even for the offscreen
    plugin (libEGL on a headless Linux box), so this is a skip and not an
    error: the machine simply cannot host a GUI test.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets
    except ImportError as ex:
        pytest.skip("Qt cannot start here: %s" % ex)
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def pair():
    return TunerSession(zf6hp_path(STAGE1)), TunerSession(zf6hp_path(STOCK))


@pytest.fixture
def model(app, pair):
    from ovtsuite import gui as G
    return G.TableModel(pair[0])


def _one_dimensional(session, minimum=8):
    """Tables stored Nx1, which the view shows transposed."""
    return [e for e in session.entities
            if e.cols == 1 and e.rows >= minimum]


def _widest(session):
    ones = _one_dimensional(session)
    assert ones, "no 1-D tables in the reference image"
    return max(ones, key=lambda e: e.rows)


def test_a_transposed_table_is_actually_transposed(pair, model):
    """Guards the premise: if 1-D tables stopped transposing, these pass
    vacuously and the bug could come back unnoticed."""
    a, b = pair
    e = _widest(a)
    model.set_entity(e, a.counterpart(b, e))
    assert model.transposed, "%s is stored %dx%d and should show transposed" \
        % (e.name, e.rows, e.cols)


def test_row_count_does_not_invent_comparison_rows(pair, model):
    """The regression. 256x1 claimed 258 rows; it has three."""
    a, b = pair
    e = _widest(a)
    cp = a.counterpart(b, e)
    assert cp is not None, "need a paired entity for the comparison block"
    model.set_entity(e, cp)
    assert model.columnCount() == e.rows + model._left
    # one data row (entity.cols) + banner + one comparison row
    assert model.rowCount() == e.cols + model._top + 1 + e.cols, \
        "grid is %dx%d for a %dx%d entity" % (
            model.rowCount(), model.columnCount(), e.rows, e.cols)
    assert model.rowCount() < e.rows, \
        "the transposed grid must not be as deep as the entity is long"


def test_every_cell_of_a_transposed_comparison_reads(pair, model):
    """No cell may raise: 65,280 of them did, and Qt does not survive that."""
    a, b = pair
    e = _widest(a)
    model.set_entity(e, a.counterpart(b, e))
    for r in range(model.rowCount()):
        for c in range(model.columnCount()):
            for role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole,
                         QtCore.Qt.BackgroundRole, QtCore.Qt.FontRole,
                         QtCore.Qt.ForegroundRole,
                         QtCore.Qt.TextAlignmentRole):
                model.data(model.index(r, c), role)


def test_the_comparison_row_still_shows_the_other_value(pair, model):
    """Bounding the grid must not have blanked the comparison itself."""
    a, b = pair
    ones = [e for e in _one_dimensional(a)
            if a.counterpart(b, e) is not None]
    shown = 0
    for e in ones[:40]:
        model.set_entity(e, a.counterpart(b, e))
        sep = model.sep_row()
        assert sep is not None
        row = sep + 1 + model._top          # past the comparison's axis band
        vals = [model.data(model.index(row, c), QtCore.Qt.DisplayRole)
                for c in range(model._left, model.columnCount())]
        if any(v not in (None, "") for v in vals):
            shown += 1
    assert shown, "no comparison row rendered a value in %d tables" % len(ones)


def test_kind_never_points_outside_the_grid(pair, model):
    """``_kind`` is the map from grid to entity; it must stay inside both."""
    a, b = pair
    for e in _one_dimensional(a)[:40]:
        cp = a.counterpart(b, e)
        if cp is None:
            continue
        model.set_entity(e, cp)
        for r in range(model.rowCount()):
            for c in range(model.columnCount()):
                k, i, j = model._kind(r, c)
                if k in ("z", "cz"):
                    assert 0 <= i < e.rows and 0 <= j < e.cols, \
                        "%s at grid (%d,%d) -> %s (%d,%d) outside %dx%d" % (
                            e.name, r, c, k, i, j, e.rows, e.cols)


def test_at_is_a_blank_not_an_exception(model):
    assert model._at([[1.0]], 0, 0) == 1.0
    assert model._at([[1.0]], 0, 5) is None
    assert model._at([[1.0]], 5, 0) is None
    assert model._at([], 0, 0) is None
    assert model._at([None], 0, 0) is None


def test_cmp_span_transposes_with_the_grid(model):
    model._cmp = [[1.0], [2.0], [3.0]]
    model.transposed = False
    assert model._cmp_span() == 3
    model.transposed = True
    assert model._cmp_span() == 1
    model._cmp = []
    assert model._cmp_span() == 0
