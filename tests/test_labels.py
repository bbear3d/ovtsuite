# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Concise, symbol-free tree labels."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402
import re  # noqa: E402

import ovtsuite.gui as g  # noqa: E402
from ovtsuite.session import TunerSession  # noqa: E402

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


def test_concise_helpers():
    assert g._concise("Segment value for Eng_nAvrg > FBC_nThresFbc_C") \
        == "Segment value"
    assert "_MAP" not in g._concise(
        "Selection of the _MAPs AccPed_trqEng#_MAP, #={0,...,5}")
    long = g._concise("x" * 200)
    assert len(long) <= 67
    assert g._can_partner("FrmMng_AIRBG1TOTmp_C") == "Airbag"


@needs_ref
def test_labels_clean_and_bounded():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    sym = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9#]+\b")
    for e in s.entities:
        lbl = g._english(e)
        assert lbl and lbl != e.name
        assert not sym.search(lbl)             # no raw Bosch symbols leak
        assert len(lbl) <= 68                  # scannable length
