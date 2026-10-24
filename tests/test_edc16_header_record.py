# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An EDC16 map whose stored header holds the A2L's dimensions is read at
those dimensions even when one of its axes does not rise: the 03G906016LQ
BHW calibration stores ASDdc_dnIntegLtdPos_MAP (6 x 4) with the Y axis
2000, 3000, 4000, 50, and reading it axis by axis made it 4 x 4.

Run on its own (never the whole suite):  pytest tests/test_edc16_header_record.py
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import BINARIES  # noqa: E402
from ovtsuite.session import TunerSession  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
NAME = "ASDdc_dnIntegLtdPos_MAP"
Y_AXIS = 0x1CABA2                      # LQ: its 4 Y breakpoints


def test_a_map_with_a_falling_axis_keeps_its_header_size(tmp_path):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    data = bytearray(open(LQ, "rb").read())
    assert struct.unpack_from(">4H", data, Y_AXIS) == (1000, 2000, 3000, 4000)
    struct.pack_into(">4H", data, Y_AXIS, 2000, 3000, 4000, 50)   # as BHW stores it
    path = tmp_path / "bhw_like.bin"
    path.write_bytes(bytes(data))
    e = next(x for x in TunerSession(str(path)).entities if x.name == NAME)
    assert (e.rows, e.cols) == (4, 6)
    assert e.addr == 0x1CABAA and e.x.addr == 0x1CAB96 and e.y.addr == Y_AXIS
    assert e.x_values() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert [round(v) for v in e.y_values()] == [200, 300, 400, 5]
