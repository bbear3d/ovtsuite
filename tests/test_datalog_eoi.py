# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's derived end-of-injection channel (ovtsuite.datalog.derived):
worked out from each row's SOI and duration, marked ``=eoi`` in the file and
read back as such, and added to the Timing preset when both cells are in it."""
import io
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.datalog import derived as D  # noqa: E402
from ovtsuite.datalog import logfile as L  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")


def _chans():
    return [L.Channel(1, 0, "Engine Speed", "rpm", "Eng_nAvrg"),
            L.Channel(4, 1, "Main Injection Timing", "deg", "InjCrv_phiMI1Des"),
            L.Channel(1, 2, "Main Injection Duration", "deg", "InjVlv_phiMI1DurCorr")]


def test_eoi_is_duration_minus_soi_atdc():
    ch = _chans()
    assert D.can_add_eoi(ch)
    ch.append(D.eoi_channel())
    assert not D.can_add_eoi(ch)                       # once only
    texts = ["2000 rpm", "4.0 deg", "9.8 deg", "N/A"]
    values = [2000.0, 4.0, 9.8, None]
    D.fill(ch, texts, values)
    assert values[3] == pytest.approx(5.8)
    assert texts[3] == "5.8 deg ATDC"
    values[1] = None                                   # a cell the ECU did not answer
    D.fill(ch, texts, values)
    assert values[3] is None and texts[3] == "N/A"


def test_not_offered_without_both_cells():
    assert not D.can_add_eoi(_chans()[:2])


def test_marker_round_trip():
    ch = _chans() + [D.eoi_channel()]
    line = L.marker_line(ch)
    assert line.endswith(",=eoi")
    back = L.parse_marker_line(line)
    assert back[-1].is_derived and back[-1].derived == "eoi"
    assert [c.key for c in back[:3]] == [c.key for c in ch[:3]]


def test_written_and_read_back(tmp_path):
    ch = _chans() + [D.eoi_channel()]
    p = tmp_path / "t.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = L.KwhackWriter(fh, ch)
        texts = ["2000 rpm", "4.0 deg", "9.8 deg", "N/A"]
        values = [2000.0, 4.0, 9.8, None]
        D.fill(ch, texts, values)
        w.row(datetime(2026, 10, 19, 22, 0, 0), texts)
    log = L.read_kwhack(str(p))
    assert log.channels[-1].derived == "eoi"
    assert log.column(3) == [pytest.approx(5.8)]


@pytest.mark.skipif(not os.path.exists(LQ), reason="needs 03G906016LQ.bin")
def test_timing_preset_on_lq_carries_both_cells():
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.datalog.served import unserved_for
    from ovtsuite.logtune.roles import RECIPES, recipe_groups
    from ovtsuite.session import TunerSession
    namer = ChannelNamer(TunerSession(LQ))
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")
    rec = next(r for r in RECIPES if r.key == "timing")
    groups, _ = recipe_groups(namer, rec, rec.max_groups, avoid=unserved_for(namer))
    ch = [namer.channel(g, f) for g in groups for f in range(namer.slots(g))]
    assert D.can_add_eoi(ch)
