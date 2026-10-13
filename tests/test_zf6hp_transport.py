# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""6HP flash-transport auto-detection: which path (UDS for 6HP28 vs KWP/TP2.0
for 6HP19/26) a loaded image needs. Pure detector + a corpus-gated session check."""
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "work"))

from ovtsuite.flash import zf6hp_uds as U  # noqa: E402

_OEM_6HP28 = os.path.join(HERE, "..", "Reference", "ZF6HP", "OEM", "6HP28",
                          "8K0927158C__0052.frf.bin")


def test_family_is_the_primary_signal():
    assert U.detect_6hp_transport(family="zf6hp28").transport == U.UDS
    assert U.detect_6hp_transport(family="zf6hp26").transport == U.TP20
    assert U.detect_6hp_transport(family="zf6hp19_4e1910156a").transport == U.TP20
    for c in (U.detect_6hp_transport(family="zf6hp28"),
              U.detect_6hp_transport(family="zf6hp26")):
        assert c.confidence == "high"


def test_controller_mcu_signal():
    assert U.detect_6hp_transport(controller_mcu="MPC562").transport == U.UDS
    assert U.detect_6hp_transport(controller_mcu="MPC555").transport == U.TP20


def test_container_meta_wins_and_is_high_confidence():
    assert U.detect_6hp_transport(meta={"variant": "6hp28"}).transport == U.UDS
    assert U.detect_6hp_transport(meta={"variant": "6hp19"}).transport == U.TP20
    # meta beats a misleading size
    c = U.detect_6hp_transport(meta={"variant": "6hp28"}, size=0x80000)
    assert c.transport == U.UDS and c.confidence == "high"


def test_size_is_last_resort_and_low_confidence():
    big = U.detect_6hp_transport(size=0x200000)
    assert big.transport == U.UDS and big.confidence == "low"
    small = U.detect_6hp_transport(size=0x100000)
    assert small.transport == U.TP20 and small.confidence == "low"


def test_no_signal_defaults_to_tp20_low():
    c = U.detect_6hp_transport()
    assert c.transport == U.TP20 and c.confidence == "low"


def test_family_beats_size_heuristic():
    # a 6HP28 cal is only 512 KB but the matched family still routes it to UDS
    c = U.detect_6hp_transport(family="zf6hp28", size=0x80000)
    assert c.transport == U.UDS and c.confidence == "high"


@pytest.mark.skipif(not os.path.exists(_OEM_6HP28),
                    reason="6HP28 OEM binary not present")
def test_transport_for_session_real_images(tmp_path):
    from ovtsuite.session import TunerSession
    full = open(_OEM_6HP28, "rb").read()
    fp = tmp_path / "full.bin"; fp.write_bytes(full)
    cp = tmp_path / "cal.bin"; cp.write_bytes(full[0x180000:0x200000])
    for p in (fp, cp):
        s = TunerSession(str(p))
        assert U.transport_for_session(s).transport == U.UDS   # both are 6HP28
    # a 6HP19 reference, if present, must route to TP2.0
    import glob
    for g in glob.glob(os.path.join(HERE, "..", "Reference", "ZF6HP", "**",
                                    "3.0TDI-Stock.bin"), recursive=True):
        s = TunerSession(g)
        assert U.transport_for_session(s).transport == U.TP20
        break
