# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VSS pulse-scaling detection: primary + coding variants, value-agnostic.

Variants are resolved through the active-coding state array by *address*, so a
variant is found at any (stock or non-stock) pulse count, and a compact variant
block that switches only a couple of parameters is handled too.
"""
import os
import pytest
from ovtsuite.session import TunerSession
from ovtsuite.variant import vss_pulse_settings

BINDIR = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")


def _load(name):
    p = os.path.join(BINDIR, name)
    if not os.path.exists(p):
        pytest.skip("%s not present" % name)
    return TunerSession(p)


def _variants(rows):
    return [r for r in rows if not r["primary"]]


def test_primary_found_and_scaled():
    rows = vss_pulse_settings(_load("038997016T.Bin"))
    assert rows and rows[0]["primary"]
    assert rows[0]["imp_km"] == 4000            # 4.0 Imp/m stock, single config
    assert _variants(rows) == []


def test_ab_manual_variant_flips_source():
    # 038906016AB is DSG(active, CAN)+manual: the manual variant is a full VSSCD
    # relocation on the hard-wired sensor at 3538 pulses/km.
    rows = vss_pulse_settings(_load("038906016AB.bin"))
    assert rows[0]["src"] == "CAN" and rows[0]["imp_km"] == 4000
    v = _variants(rows)
    assert len(v) == 1
    assert v[0]["imp_km"] == 3538 and v[0]["src"] == "HW sensor"


def test_q_compact_manual_variant():
    # 038906016Q is auto(active, sensor)+manual(sensor): both use the sensor so
    # only ImpPerDist is switched (a *compact* block). The manual variant is
    # 3538 pulses/km at 0x189A70 -- a value-based/byte-shape search misses it,
    # the address-based state-array resolution finds it.
    rows = vss_pulse_settings(_load("038906016Q.Bin"))
    assert rows[0]["imp_km"] == 2669 and rows[0]["src"] == "HW sensor"
    v = _variants(rows)
    assert len(v) == 1
    assert v[0]["addr"] == 0x189A70
    assert v[0]["imp_km"] == 3538 and v[0]["src"] == "HW sensor"


def test_single_state_compact_array():
    # 038997016R (BEW 1.9L) stores its coding redirects as a single-state sorted
    # array with the manual variant inline -- a different array shape from Q's
    # multi-state rows. The bracket scan must still find the 3538 manual variant.
    rows = vss_pulse_settings(_load("038997016R.bin"))
    assert rows[0]["imp_km"] == 2669 and rows[0]["src"] == "HW sensor"
    v = _variants(rows)
    assert len(v) == 1
    assert v[0]["addr"] == 0x18AC7C
    assert v[0]["imp_km"] == 3538 and v[0]["src"] == "HW sensor"


def test_value_agnostic_finds_retuned_variant():
    # retune the manual variant to a non-stock pulse count; still found, because
    # the variant address comes from the coding array, not the value.
    s = _load("038906016Q.Bin")
    d = bytearray(s.binfile.data)
    new = round(3.6e9 / 9.0)                     # 9.0 Imp/m -> 9000 pulses/km
    d[0x189A70:0x189A74] = new.to_bytes(4, "big")
    s.binfile.data = d
    v = _variants(vss_pulse_settings(s))
    assert len(v) == 1 and v[0]["imp_km"] == 9000
