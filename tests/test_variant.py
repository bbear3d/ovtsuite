# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Secondary / variant calibration-table detection."""

import os
import sys
from bisect import bisect_right

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
@pytest.mark.parametrize("binf", ["03G906016AC.bin", "p447_haxe_ref.bin",
                                  "038906016AH_p529tawi.bin"])
def test_no_secondary_block(binf):
    # full reads whose default array proves there is no variant switch in
    # any coding state
    s = TunerSession(os.path.join(BIN, binf))
    assert find_variant_tables(s) == []
    assert not has_secondary_block(s)


@needs_ref
def test_every_coding_state_is_read():
    """The array path reads all of the active array's coding states, not only
    the first. 03G906016LQ's first state redirects two slots (nothing
    differs there), but its other two redirect the aux-heater count, the
    output-test table and the coding table; 03G906016GC's seven states
    redirect 19 blocks, of which the first state has three."""
    s = TunerSession(os.path.join(BIN, "03G906016LQ.bin"))
    vt = find_variant_tables(s)
    assert vt and all(v.method == "array" for v in vt)
    assert any(v.name == "AOHt_NumHeat_C" and v.differs for v in vt)
    s = TunerSession(os.path.join(BIN, "03G906016GC.bin"))
    vt = find_variant_tables(s)
    assert len(vt) >= 200 and sum(v.differs for v in vt) >= 50


@needs_ref
def test_a_block_is_as_long_as_the_array_says():
    """A switched block runs to the next slot's start, and no further: the
    last slot used to get a guessed 0x40 window, which pulled the next
    parameters' unrelated bytes in as 'variants' (038997016K listed 24
    differing, of which 15 were VehDa_*/VSACD_* neighbours). A block longer
    than 0x400 is projected whole, too (038906016N's FlMng block is 0xF68
    long and was cut to 0x40)."""
    s = TunerSession(os.path.join(BIN, "038997016K.bin"))
    vt = find_variant_tables(s)
    assert len(vt) == 103 and sum(v.differs for v in vt) == 9
    assert not any(v.name.startswith(("VehDa_", "VSACD_")) for v in vt)
    s = TunerSession(os.path.join(BIN, "038906016N.bin"))
    vt = find_variant_tables(s)
    assert len(vt) == 922
    assert sum(1 for v in vt if 0x1893BC <= v.sec_z < 0x1893BC + 0xF68) > 10
    assert not any(v.name == "Chiste_stEnaTst_C" for v in vt)


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


def _calibration_only(src, tmp_path):
    """``src`` as an OBD/calibration read: the program area (and so the
    default pointer array) blank."""
    buf = bytearray(open(src, "rb").read())
    buf[:0x180000] = b"\xff" * 0x180000
    out = tmp_path / "cal.bin"
    out.write_bytes(bytes(buf))
    return str(out)


@needs_ref
@pytest.mark.parametrize("fname", ["038906016N.bin", "03G906016GC.bin",
                                   "03G906021KK.bin"])
def test_every_relocated_block_is_placed_exactly_from_the_arrays(fname):
    """With the program area blank there is no default array, yet the active
    array alone places every relocated block: the slots between two
    unswitched ones tile the main block, so a run of switched slots ends where
    the next unswitched slot starts, and the packed copies give the lengths.
    Held to the default array of the full read, every block -- base and
    length, the last packed copy included -- comes out exact."""
    from ovtsuite.variant import (_active_states, _locate_arrays,
                                  _populated_len, _structural_blocks,
                                  _SEARCH_LO, _u32)
    s = TunerSession(os.path.join(BIN, fname))
    data = bytes(s.binfile.data)
    lo, hi = s.region
    d, a = _locate_arrays(data)
    n = 0
    while 0x1000 <= _u32(data, d + n * 4) < 0x200000:
        n += 1
    default = [_u32(data, d + 4 * i) for i in range(n)]
    states = _active_states(data, a, lo, hi)
    truth = {}
    for st in states:
        for i, x in enumerate(st[:n]):
            if x != default[i] and _SEARCH_LO <= x < lo:
                truth[x] = (default[i], default[i + 1] - default[i])
    starts = sorted(truth)
    got = _structural_blocks(
        states, starts, lambda v: lo <= v < hi,
        lambda v: _SEARCH_LO <= v < lo, _populated_len(data, starts[-1], lo))
    assert truth and got == truth


@needs_ref
def test_a_calibration_read_finds_what_the_full_read_does(tmp_path):
    """038997016K (a small coding block, its last packed copy 12 bytes) read
    without its program area gives the same copies as the full read, and so
    do the copies of 038906016N, a few more named from the A2L aside. Every
    copy sits at its primary's offset modulo 4, which is how a copy is told
    from a shifted match of itself."""
    full = TunerSession(os.path.join(BIN, "038997016K.bin"))
    cal = TunerSession(_calibration_only(os.path.join(BIN, "038997016K.bin"),
                                         tmp_path))
    a = {(v.main_z, v.sec_z, v.differs) for v in find_variant_tables(full)}
    b = {(v.main_z, v.sec_z, v.differs) for v in find_variant_tables(cal)}
    assert a == b and len(a) == 103
    full = TunerSession(os.path.join(BIN, "038906016N.bin"))
    cal = TunerSession(_calibration_only(os.path.join(BIN, "038906016N.bin"),
                                         tmp_path))
    a = {(v.main_z, v.sec_z, v.differs) for v in find_variant_tables(full)}
    vc = find_variant_tables(cal)
    b = {(v.main_z, v.sec_z, v.differs) for v in vc}
    assert a <= b
    assert all((v.sec_z - v.main_z) % 4 == 0 for v in vc)


@needs_ref
def test_a_wholly_retuned_gear_block_is_still_found(tmp_path):
    """Placement does not read the values, so a secondary gear block with
    most of its bytes changed pairs with its primary just as an untouched
    one does -- the gear ratios stay editable however they were set."""
    import random
    src = os.path.join(BIN, "038906016N.bin")
    s = TunerSession(src)
    gz = next(v.sec_z for v in find_variant_tables(s)
              if v.name == "Gearbx_rTrans1_C")
    buf = bytearray(open(src, "rb").read())
    buf[:0x180000] = b"\xff" * 0x180000
    rnd = random.Random(7)
    for k in range(0x30):                       # the block holding the ratios
        if rnd.random() < 0.7:
            buf[gz + k] = (buf[gz + k] + rnd.randrange(1, 40)) & 0xFF
    out = tmp_path / "retuned.bin"
    out.write_bytes(bytes(buf))
    vt = find_variant_tables(TunerSession(str(out)))
    g = [v for v in vt if v.name == "Gearbx_rTrans1_C"]
    assert [v.sec_z for v in g] == [gz] and g[0].differs


@needs_ref
@pytest.mark.parametrize("fname", ["038906016N.bin", "03G906016GC.bin",
                                   "03G906021KK.bin", "03G906016LQ.bin"])
def test_a_calibration_read_rebuilds_the_default_array(fname, tmp_path):
    """One layout for every read: the default array a calibration read lacks is
    rebuilt from the active array, and comes out the very array the full read
    holds -- every slot start, and so every block the arrays switch."""
    from ovtsuite.variant import (_active_states, _locate_arrays,
                                  _rebuild_slots, _u32)
    src = os.path.join(BIN, fname)
    s = TunerSession(src)
    data = bytes(s.binfile.data)
    lo, hi = s.region
    d, a = _locate_arrays(data)
    n = 0
    while 0x1000 <= _u32(data, d + n * 4) < 0x200000:
        n += 1
    default = [_u32(data, d + 4 * i) for i in range(n)]
    slots, lens = _rebuild_slots(data, _active_states(data, a, lo, hi), lo, hi)
    assert slots == default
    # and the same variant tables come of it, whichever array supplied it
    full = {(v.main_z, v.sec_z, v.differs) for v in find_variant_tables(s)}
    cal = TunerSession(_calibration_only(src, tmp_path))
    assert full == {(v.main_z, v.sec_z, v.differs)
                    for v in find_variant_tables(cal)}


@needs_ref
@pytest.mark.parametrize("fname", ["038906016Q.Bin", "03G906021MP_1322_touran_ori.bin",
                                   "md_03G906021PD_2387.bin"])
def test_every_pointer_array_file_is_read_by_the_arrays(fname):
    """Nothing is left to the axis fingerprint where there are pointer arrays:
    every copy of a calibration-only read comes from the arrays too."""
    s = TunerSession(os.path.join(BIN, fname))
    vt = find_variant_tables(s)
    assert vt and all(v.method == "array" and not v.ambiguous for v in vt)


@needs_ref
@pytest.mark.parametrize("fname", ["038906016Q.Bin", "038906016AB.bin",
                                   "038997016K.bin", "038906016N.bin"])
def test_p350_digits_agree_with_the_datasets_own_gearbox_type(fname):
    """On a dataset without function code bits the coding is a decimal number
    and the digit scheme reads it (confirmed against VCDS). The dataset states
    the transmission as well -- Gearbx_swtTypeVal_C, switched with the coding --
    and the digits agree with it on every P350 file: that is why the digit
    scheme may stay there, and the declared type is checked against it."""
    from ovtsuite.variant import (_declared_types, _func_code_bits,
                                  decode_coding, find_variant_tables,
                                  stored_codings)
    s = TunerSession(os.path.join(BIN, fname))
    data = bytes(s.binfile.data)
    assert _func_code_bits(s, data) == 0
    vt = find_variant_tables(s)
    declared = _declared_types(s, data, vt)
    assert declared
    for _a, code in stored_codings(s, vt):
        if code in declared:
            digits = decode_coding(code)[0]
            assert (declared[code] == 0) == digits.endswith("manual"), code


@needs_ref
def test_p447_codings_are_variants_not_decimal_digits():
    """03G906021 is function-coded (USAp_numFuncCodeBits_C = 18): its coding
    is ``variant << 18``, and its digits mean nothing. Read as the P350 scheme
    the transmission came out backwards (786432, variant 3, an automatic, as
    '6-speed manual'; 524288 as 'DSG'), and every coding of the main block and
    of the extra option variants (variants 10, 18, 26 and 19 here) was dropped
    by a scan that only knew small values."""
    from ovtsuite.variant import coding_states, describe_codings
    s = TunerSession(os.path.join(BIN, "03G906021KK.bin"))
    states = {c["coding"]: c for c in coding_states(s)}
    assert {c: x["variant"] for c, x in states.items()} == {
        524288: 2, 786432: 3, 2621440: 10, 4718592: 18, 4980736: 19,
        6815744: 26}
    assert all(x["function"] == 0 for x in states.values())
    # variant 3 (and 19, its option bit set) is the automatic: the dataset
    # says so (Gearbx_swtTypeVal_C) and so does the speed source it reads (CAN)
    auto = {c for c, x in states.items() if x["gearbox"] == "automatic"}
    assert auto == {786432, 4980736}
    assert all(x["gearbox"] == "manual"
               for c, x in states.items() if c not in auto)
    assert all(x["ac"] == "in the function code" for x in states.values())
    # the gear ratios each coding selects are the copy its state reads
    assert states[786432]["gears"] == states[4980736]["gears"]
    assert states[524288]["gears"] == states[2621440]["gears"]
    assert states[524288]["gears"] != states[786432]["gears"]
    d = describe_codings(s)
    assert d[786432]["transmission"] == "automatic"
    assert d[524288]["transmission"] == "manual"


@needs_ref
def test_p447_copies_are_labelled_by_the_variant_and_transmission():
    """The tree and the gear window name a P447 copy by what it is: the
    variant numbers that select it and the transmission the dataset declares."""
    from ovtsuite.gears import GearRatios
    s = TunerSession(os.path.join(BIN, "03G906021MP_1322_touran_ori.bin"))
    main = next(e for e in s.entities if e.name == "Gearbx_rTrans1_C")
    copy = next(v for v in s.variant_entities()
                if v.name.startswith("Gearbx_rTrans1_C "))
    assert main.coding == "coding 786432+/4980736+ (variant 3/19, automatic)"
    assert copy.coding == "coding 524288+/4718592+ (variant 2/18, manual)"
    labels = [c.label for c in GearRatios(s).copies()]
    assert labels[0].startswith("Automatic") and labels[1] == "Manual"


@needs_ref
def test_coding_labels_read_back_what_coding_text_wrote():
    from ovtsuite.variant import coding_labels, coding_text
    desc = {786432: {"variant": 3, "transmission": "automatic", "ac": None},
            524288: {"variant": 2, "transmission": "manual", "ac": None}}
    txt = coding_text([786432, 524288], desc)
    assert txt == "coding 524288+/786432+ (variant 2/3, manual, automatic)"
    assert coding_labels(txt) == {"manual", "automatic"}
    # P350 style: no variant, the digit scheme labels it
    assert coding_text([150031, 158031]) == "coding 150031/158031 (5-speed manual)"
    assert coding_labels("coding 150034 (automatic (TC))") == {"automatic (TC)"}
    assert coding_labels("") == set()


@needs_ref
def test_a_scan_tools_p447_coding_finds_its_variant():
    """A scan tool shows a P447 car's whole coding -- the variant in the upper
    bits, the car's function bits below -- but the file holds only the variant.
    The filter reads a typed coding the way the dataset does, so 786437 (variant
    3, function bits 5) finds what 786432 does."""
    from ovtsuite.variant import coding_query, function_code_bits
    s = TunerSession(os.path.join(BIN, "03G906021MP_1322_touran_ori.bin"))
    assert function_code_bits(s) == 18
    assert coding_query(s, "coding 786437") == "coding 786432"
    assert coding_query(s, "coding 0786437") == "coding 786432"  # a tool's padding
    assert coding_query(s, "coding 1048575") == "coding 786432"  # every low bit set
    assert coding_query(s, "coding 524288/786437") == "coding 524288/786432"
    # a half-typed number, or one below the lowest variant, is left to match by digits
    assert coding_query(s, "coding 7864") == "coding 7864"
    assert coding_query(s, "coding 5") == "coding 5"
    # a bare number is a coding when it names a stored one; any other stays text
    assert coding_query(s, "786437") == "coding 786432"
    assert coding_query(s, "1234567") == "1234567"
    assert coding_query(s, "accped*map") == "accped*map"
    # and it finds the copy: the filter's row text carries "coding 786432"
    import ovtsuite.gui as g
    vs = s.variant_entities()            # labels the main copies as well
    main = next(e for e in s.entities if e.name == "Gearbx_rTrans1_C")
    ft = g._filter_text(main, g._english(main))
    assert coding_query(s, "coding 786437") in ft
    assert coding_query(s, "coding 4980737") in ft          # variant 19, as shown
    assert "coding 3" not in ft and "coding 19" not in ft   # variant numbers aren't codings
    copy = next(v for v in vs if v.name.startswith("Gearbx_rTrans1_C "))
    cft = g._filter_text(copy, g._english(copy))
    assert coding_query(s, "coding 524290") in cft          # variant 2, function bits 2
    assert coding_query(s, "coding 786437") not in cft


@needs_ref
def test_the_coding_query_leaves_a_plain_decimal_dataset_alone():
    """On P350 the stored value is exactly what the scan tool shows: nothing is
    rounded, so 150031 finds 150031 and 150032 finds nothing."""
    from ovtsuite.variant import coding_query, function_code_bits
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    assert function_code_bits(s) == 0
    for q in ("coding 150031", "coding 150032", "158034", "coding 0150031"):
        assert coding_query(s, q) == q


@needs_ref
def test_the_label_and_the_search_window_speak_the_scan_tools_coding():
    """P447 labels a copy with the stored variant value and a "+" (the car adds
    its function bits); the search window reads a scan tool's number the same
    way the filter does, and also takes the label's own "786432+"."""
    from ovtsuite import search as S
    from ovtsuite.variant import coding_query
    import ovtsuite.gui as g
    s = TunerSession(os.path.join(BIN, "03G906021MP_1322_touran_ori.bin"))
    eng = S.SearchEngine(g._english)
    src = S.Source("MP", s)
    assert coding_query(s, "coding 786432+") == "coding 786432"
    assert coding_query(s, "coding 524288+/786437") == "coding 524288/786432"

    def gears(q):
        return {h.entity.name.split(" ")[0] + ("*" if h.entity.source == "variant" else "")
                for h in eng.run(q, S.SearchOptions(), [src])
                if h.entity.name.startswith("Gearbx_rTrans1_C")}
    for q in ("coding 786437", "786437", "coding 786432+", "0786437"):
        assert gears(q) == {"Gearbx_rTrans1_C"}, q          # the main copy only
    for q in ("coding 524290", "524290", "coding 4718592+"):
        assert gears(q) == {"Gearbx_rTrans1_C*"}, q         # the second copy only
    # a dataset without function bits is searched as typed
    q350 = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    eng2 = S.SearchEngine(g._english)
    src2 = S.Source("Q", q350)
    names = {h.entity.name for h in eng2.run("coding 158032", S.SearchOptions(), [src2])
             if h.entity.name.startswith("Gearbx_rTrans1_C")}
    assert not names
