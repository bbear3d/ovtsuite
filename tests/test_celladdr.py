# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a table's cells are stored (ovtsuite.celladdr), for the status
bar's address: Z cells in either storage order, axis points, comparison
cells, and the text for one cell or a range."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import celladdr as CA  # noqa: E402
from ovtsuite.model import Axis, Entity  # noqa: E402


def _ent(kind="map", addr=0x1000, rows=3, cols=4, bits=16, order="col"):
    return Entity(name="T", kind=kind, tier=1, categories=["Misc"],
                  description="", units="", addr=addr, rows=rows, cols=cols,
                  bits=bits, signed=False, equation="X", source="verified",
                  order=order)


def test_z_cells_follow_the_storage_order():
    col = _ent()                          # EDC16: Y (rows) varies fastest
    assert CA.cell_addr(col, "z", 0, 0) == 0x1000
    assert CA.cell_addr(col, "z", 1, 0) == 0x1002
    assert CA.cell_addr(col, "z", 0, 1) == 0x1000 + 3 * 2
    row = _ent(order="row")
    assert CA.cell_addr(row, "z", 0, 1) == 0x1002
    assert CA.cell_addr(row, "z", 1, 0) == 0x1000 + 4 * 2
    assert CA.cell_addr(row, "z", 3, 0) is None          # out of the table


def test_axis_points():
    e = _ent()
    e.x = Axis(addr=0x800, count=4, bits=16)
    e.y = Axis(addr=0x900, count=3, bits=8)
    assert CA.cell_addr(e, "x", 2, 0) == 0x804
    assert CA.cell_addr(e, "y", 2, 0) == 0x902
    e.x = Axis(addr=0x800, count=4, bits=32, is_float=True)
    assert CA.cell_addr(e, "x", 1, 0) == 0x804
    e.y = Axis(addr=None, count=3, values=[1, 2, 3])     # given, not stored
    assert CA.cell_addr(e, "y", 0, 0) is None
    assert CA.cell_addr(e, "corner", 0, 0) is None
    assert CA.cell_addr(e, "sep", 0, 2) is None


def test_comparison_cells():
    e, cp = _ent(), _ent(addr=0x5000)
    cp.x = Axis(addr=0x4000, count=4, bits=16)
    assert CA.cell_addr(e, "cz", 1, 1, cp, "direct") == 0x5000 + 2 * (3 + 1)
    assert CA.cell_addr(e, "cx", 3, 0, cp, "axis-mismatch") == 0x4006
    assert CA.cell_addr(e, "cz", 1, 1, cp, "resampled") is None
    assert CA.cell_addr(e, "cz", 1, 1, None, None) is None
    # a 1x4 curve over a 4x1 one: shown turned
    c14, c41 = _ent("curve", rows=1, cols=4), _ent("curve", 0x6000, 4, 1)
    assert CA.cell_addr(c14, "cz", 0, 2, c41, "direct") == 0x6004


def test_describe():
    assert CA.describe([]) == ""
    assert CA.describe([None]) == ""
    assert CA.describe([0x1A2B3C]) == "0x1A2B3C"
    assert CA.describe([0x10, 0x10, None]) == "0x000010"
    assert CA.describe([0x1006, 0x1000, 0x1002]) == \
        "0x001000–0x001006 (3 cells)"
