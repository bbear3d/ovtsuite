# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Secondary / variant calibration-table detection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

from ovtsuite.session import TunerSession  # noqa: E402
from ovtsuite.variant import find_variant_tables, has_secondary_block  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


@needs_ref
def test_038906016_has_secondary_block():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    vt = find_variant_tables(s)
    assert len(vt) > 50                      # a real secondary block
    assert has_secondary_block(s)
    # at least some copies genuinely differ from the primary (variant-specific)
    assert any(v.differs for v in vt)
    # pairing carries real parameter names from the main-block twins
    named = [v for v in vt if not v.name.startswith("@")]
    assert len(named) > len(vt) * 0.8
    # the air-system base demand map is a known variant here
    assert any("AirCtl_mDesBase" in v.name for v in vt)


@needs_ref
@pytest.mark.parametrize("binf", ["03G906016LQ.bin"])
def test_no_secondary_block(binf):
    # full reads whose default array proves there is no variant switch
    s = TunerSession(os.path.join(BIN, binf))
    assert find_variant_tables(s) == []
    assert not has_secondary_block(s)


@needs_ref
def test_small_ac_coding_block_is_kept():
    # 038997016T carries a small but real A/C-type (Climatronic vs manual)
    # coding block -- three differing ACCD_* parameters -- which must not be
    # suppressed as noise.
    from ovtsuite.variant import coding_domains, param_domain
    s = TunerSession(os.path.join(BIN, "038997016T.Bin"))
    vt = find_variant_tables(s)
    differ = [v for v in vt if v.differs]
    assert differ, "the A/C coding stub must be detected"
    assert all(v.name.startswith("ACCD_") for v in differ)
    assert all(param_domain(v.name).startswith("A/C-HVAC") for v in differ)
    assert "A/C-HVAC coding (Climatronic vs manual A/C)" in coding_domains(vt)


@needs_ref
def test_variant_entities_are_editable_clones():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    ve = s.variant_entities()
    assert ve and s.variant_entities() is ve      # cached
    e = ve[0]
    assert "(variant @0x" in e.name
    assert "Coding variants" in e.categories
    # every second copy sits in the Coding variants group, under the group
    # and heading its main copy has
    from ovtsuite import groups as G
    by = {x.name: x for x in s.entities}
    for v in ve:
        assert v.tier == G.CODING
        twin = by[v.name.split(" (variant @")[0]]
        assert v.category_path[0] == G.group_name(twin.tier)
        if twin.category_path:
            assert v.category_path[1:] == list(twin.category_path)
    # points into the secondary block and reads a scaled value without error
    assert 0x182000 <= e.addr < s.region[0]
    _ = e.cell(0, 0)


@needs_ref
@pytest.mark.skipif(not os.path.exists(os.path.join(BIN, "038906016AB.bin")),
                    reason="full read not present")
def test_exact_array_pairing_and_coding_domain():
    from ovtsuite.variant import find_variant_tables, coding_domain
    s = TunerSession(os.path.join(BIN, "038906016AB.bin"))
    vt = find_variant_tables(s)
    # a full read uses the exact pointer-array diff, projected to individual
    # catalog constants within each switched span
    assert vt and all(v.method == "array" for v in vt)
    # a real gearbox coding block switches many parameters
    assert sum(1 for v in vt if v.differs) > 20
    # coding condition is recovered from the paired names
    assert coding_domain(vt) == "gearbox coding (automatic vs manual)"
    # the gear/final-drive ratio is one of the variant parameters
    assert any("VSSCD_ImpPerDist" in v.name for v in vt)
    # the individual gear ratios are surfaced (not hidden inside a block)
    assert any(v.name == "Gearbx_rTrans1_C" and v.differs for v in vt)


@needs_ref
def test_no_false_variant_block_on_p475():
    from ovtsuite.variant import find_variant_tables
    s = TunerSession(os.path.join(BIN, "03G906016LQ.bin"))
    assert find_variant_tables(s) == []


@needs_ref
def test_coding_states_recovered():
    # the ECU coding values are stored in flash (USAp_dCodeTbl + inverse); the
    # tool recovers them and ties each to the gearbox its state selects.
    from ovtsuite.variant import coding_states
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    st = coding_states(s)
    codes = {c["coding"] for c in st}
    assert {150034, 150031, 158034, 158031} <= codes
    assert all(c["inv_ok"] for c in st)          # every value has a valid inverse
    autos = {c["coding"] for c in st if c["gearbox"] == "automatic"}
    mans = {c["coding"] for c in st if c["gearbox"].startswith("manual")}
    assert {150034, 158034} <= autos             # ...034 = automatic
    assert {150031, 158031} <= mans              # ...031 = manual
    # digit-scheme decode: last digit = transmission, 2nd-last = A/C
    d = {c["coding"]: c for c in st}
    assert d[150034]["transmission"] == "automatic (TC)"
    assert d[150031]["transmission"] == "5-speed manual"
    assert all(c["ac"] == "manual A/C" for c in st)


@needs_ref
def test_climatronic_coding_is_stored():
    # the Climatronic (2nd-last digit 7) coding is stored in the flash too,
    # in the coding-acceptance table -- not only the gearbox array copies.
    from ovtsuite.variant import coding_states
    s = TunerSession(os.path.join(BIN, "038997016T.Bin"))
    st = {c["coding"]: c for c in coding_states(s)}
    assert 50074 in st and st[50074]["ac"] == "Climatronic"
    assert 150034 in st and st[150034]["ac"] == "manual A/C"


@needs_ref
def test_dsg_coding_decoded():
    # 038906016AB pairs a 5-speed manual with a DSG (last digit 8), not a TC auto
    from ovtsuite.variant import coding_states
    s = TunerSession(os.path.join(BIN, "038906016AB.bin"))
    txs = {c["coding"]: c["transmission"] for c in coding_states(s)}
    assert txs.get(150038) == "DSG"
    assert txs.get(150031) == "5-speed manual"


@needs_ref
def test_a_shared_axis_does_not_name_two_copies_after_one_table():
    """Copies whose axis several main tables share are named by their
    distance from a certain neighbour, not after the first such table: on
    038906016Q FBC_fSurge_CUR's copy used to come out as a second
    FBC_BPFac1NumG_CUR."""
    from collections import Counter
    from ovtsuite.variant import find_variant_tables
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    vt = [v for v in find_variant_tables(s) if v.differs]
    by = {(v.name, v.sec_z) for v in vt}
    assert ("FBC_fSurge_CUR", 0x189280) in by
    assert ("FBC_BPFac1NumG_CUR", 0x1891DE) in by
    # one copy per table per relocated block: only AirCtl_mDesBase_MAP has
    # two, one for each of two coding states
    dup = [n for n, k in Counter(v.name for v in vt).items() if k > 1]
    assert dup == ["AirCtl_mDesBase_MAP"]
    assert not any(v.ambiguous for v in vt)


@needs_ref
@pytest.mark.parametrize("fname", ["038906016Q.Bin", "038997016R.bin"])
def test_every_copy_is_shown_and_none_overlaps(fname):
    """Identical copies are shown too -- each is the table its codings read,
    and could not be edited for them otherwise. Every copy is held to the
    pointer array: none is ambiguous, none overlaps another, none runs past
    the end of its relocated block."""
    from ovtsuite import groups as G
    from ovtsuite.variant import find_variant_tables
    s = TunerSession(os.path.join(BIN, fname))
    vt = find_variant_tables(s)
    by = {e.addr: e for e in s.entities}
    ve = s.variant_entities()
    assert len(ve) == sum(1 for v in vt if v.main_z in by)
    assert any(not v.differs for v in vt) and len(ve) > 600
    assert not any(v.ambiguous for v in vt)
    spans = sorted((v.sec_z, v.sec_z + by[v.main_z].n_cells * by[v.main_z].step)
                   for v in vt if v.main_z in by)
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:]))
    # Advanced decides for a copy by its main copy's group
    twin = {e.name: e for e in s.entities}
    for v in ve:
        t = twin[v.name.split(" (variant @")[0]]
        assert v.base_tier == t.tier
        assert G.is_advanced(v) == G.is_advanced(t)
    assert any(G.is_advanced(v) for v in ve)
    assert any(not G.is_advanced(v) for v in ve)


def test_anchor_survives_retuned_values():
    """A relocated block whose values were retuned still anchors to its main
    base while one alignment stands clear of the rest (the gear-ratio copy
    vanished from files with corrected ratios)."""
    import random
    from ovtsuite.variant import _anchor
    rnd = random.Random(7)
    data = bytearray(rnd.randrange(256) for _ in range(0x1000))
    main, sec = 0x400, 0xC00
    data[sec:sec + 0x40] = data[main:main + 0x40]
    for k in range(0x08, 0x2C):                  # retune ~55% of the window
        data[sec + k] ^= 0x55
    base, score = _anchor(bytes(data), sec, main - 8, main + 0x80)
    assert base == main
    # pure noise anchors nowhere
    noise = bytes(rnd.randrange(256) for _ in range(0x1000))
    assert _anchor(noise, sec, main - 8, main + 0x80)[0] is None
