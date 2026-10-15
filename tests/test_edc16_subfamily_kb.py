# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Dataset sub-family KBs built by matcher-verified seed rows and a walk of
the calibration layouts in step with their parent
(tools/extend_subfamily_kb.py): every one has the per-gear driver's wish
and the dynamic start of injection, at sizes the dataset itself stores."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")

#: sub-family -> reference binary (match.REF_BINARIES)
SUBS = ("p529_tawj", "p529_tawi", "p529_tawa", "p475_aas8", "p475_aasj",
        "p475_uabc", "p475_uas8", "p475_u8d3", "p475_u8d4", "p213_u8c3",
        "p529_u8c4")


def _path(name):
    p = os.path.join(BIN, name)
    if not os.path.exists(p):
        pytest.skip("%s not present" % name)
    return p


def _session(family):
    from ovtsuite.match import REF_BINARIES
    from ovtsuite.session import TunerSession
    return TunerSession(_path(REF_BINARIES[family]))


def _driver_wish(names, gear):
    for suffix in ("_MAP", "_GMAP"):
        e = names.get("AccPed_trqEng%d%s" % (gear, suffix))
        if e is not None:
            return e
    return None


@pytest.mark.parametrize("family", SUBS)
def test_driver_wish_and_dynamic_soi_are_there(family):
    s = _session(family)
    assert s.family == family
    names = {e.name: e for e in s.entities}
    for g in range(7):
        e = _driver_wish(names, g)
        assert e is not None and e.kind == "map", (family, g)
        top = max(e.cell(r, c) for r in range(e.rows) for c in range(e.cols))
        assert 100 < top < 1000, (family, g, top)      # Nm at full pedal
    for i in range(5):
        e = names.get("InjCrv_phiDyn%d_GMAP" % i)
        assert e is not None and (e.rows, e.cols) == (14, 16), (family, i)
        v = [e.cell(r, c) for r in range(e.rows) for c in range(e.cols)]
        assert -30 <= min(v) and max(v) <= 60, (family, i)   # deg crank
    assert len(s.entities) > 2500


def test_tawj_stores_its_own_driver_wish_sizes():
    """038906016AH/AJ (P529TAWJ) calibrates the gear maps 12x8 and the cold /
    first-gear ones 13x8 -- not LQ's 16x9 -- so the old exact-size projection
    lost every one of them. The user's read of 038906016AJ 9713 is a
    different image from the reference and gets the same tables."""
    from ovtsuite.session import TunerSession
    for p in (_path("038906016AH_p529tawj.bin"),
              _path("038906016AJ_9713_P529TAWJ_read_idx.bin")):
        s = TunerSession(p)
        assert s.family == "p529_tawj"
        n = {e.name: e for e in s.entities}
        assert (n["AccPed_trqEng0_MAP"].rows, n["AccPed_trqEng0_MAP"].cols) == (8, 13)
        for g in range(1, 7):
            e = n["AccPed_trqEng%d_MAP" % g]
            assert (e.rows, e.cols) == (8, 12), g
        assert "AccPed_trqEngLow_MAP" in n and "AccPed_stGearSel_CUR" in n


def test_a_group_stored_as_separate_maps():
    """EDC16U1 keeps the driver's wish as one AccPed_trqEng#_GMAP group;
    P213U8C3 stores seven evenly spaced 12x8 maps with the same axes. The old
    KB had the 16x8 map after them (all zero) as AccPed_trqEng6_GMAP."""
    s = _session("p213_u8c3")
    n = {e.name: e for e in s.entities}
    addrs = [n["AccPed_trqEng%d_GMAP" % g].addr for g in range(7)]
    assert addrs == sorted(addrs)
    assert len({b - a for a, b in zip(addrs, addrs[1:])}) == 1
    assert all((n["AccPed_trqEng%d_GMAP" % g].rows,
                n["AccPed_trqEng%d_GMAP" % g].cols) == (8, 12) for g in range(7))


def test_038906016AR_has_its_own_kb():
    """P529U8C4 (038906016AR, the only file of its dataset): detection lands
    on the U85B6 A2L KB, whose driver's wish is one group map; this file
    stores a 13x8 first-gear map and six 12x8 ones, evenly spaced."""
    s = _session("p529_u8c4")
    n = {e.name: e for e in s.entities}
    assert not any(k.startswith("@0x") for k in n)
    dims = [(n["AccPed_trqEng%d_GMAP" % g].rows, n["AccPed_trqEng%d_GMAP" % g].cols)
            for g in range(7)]
    assert dims == [(8, 13)] + [(8, 12)] * 6


def test_p447has9_soi_correction_groups_have_their_axes():
    """The SC XDF stores the dynamic / start / air-temperature / altitude
    start-of-injection groups without axes and the axes as separate bogus
    4-row tables. generate_kb_definition gives the 21 maps the shared axis
    block the HAS7 KB places them with and drops the bogus axis tables."""
    from ovtsuite import kb as kb_mod
    k = kb_mod.load_cached("p447_has9")
    t = {x.name: x for x in k.tables}
    for pre, n in (("InjCrv_phiDyn", 5), ("InjCrv_phiStrt", 4),
                   ("InjCrv_phiAirCor", 6), ("InjCrv_phiHghtCor", 6)):
        for i in range(n):
            x = t["%s%d_GMAP" % (pre, i)]
            assert x.kind == "map" and x.x_addr is not None and x.y_addr is not None
    assert not any(x.name in ("Motordrehzahl", "Einspritzmenge") for x in k.tables)
    s = _session("p447_has9")
    e = {x.name: x for x in s.entities}["InjCrv_phiDyn0_GMAP"]
    xs = e.axis_values(e.x)
    assert (e.rows, e.cols) == (14, 16) and xs[0] < xs[-1] <= 6000


def test_new_subfamily_pipeline_against_an_a2l():
    """seed_rows + walk, the way p529_u8c4 was made, from p475_83 (LQ) onto
    a 03G906021 HAXE image, checked against the HAXE A2L: what it places is
    right (1 of ~1,600 tables, 0 of ~5,800 values off when measured)."""
    from ovtsuite import kb as kb_mod
    from ovtsuite.match import REF_BINARIES
    from ovtsuite.structure import find_cal_region
    from tools import extend_subfamily_kb as X
    a2l = os.path.join(os.path.dirname(__file__), "..", "Reference", "A2L", "BPW.a2l")
    d = open(_path(REF_BINARIES["p447_haxe"]), "rb").read()
    refd = open(_path(REF_BINARIES["p475_83"]), "rb").read()
    reg = find_cal_region(d)
    res = X.reserved_dims(a2l)
    pk = kb_mod.load("p475_83")
    st, sc = X.seed_rows(pk, d, reg, res, refd)
    truth = kb_mod.load("p447_haxe")
    T = {t.name: t for t in truth.tables}
    C = {c.name: c.addr for c in truth.constants}
    bad_t = [r[0] for r in st if r[0] in T and r[9] != T[r[0]].z_addr]
    bad_c = [r[0] for r in sc if r[0] in C and r[1] != C[r[0]]]
    assert len(st) > 1400 and len(sc) > 5000
    assert len(bad_t) <= 3 and not bad_c, (bad_t, bad_c[:5])


def test_a_stock_file_compared_with_itself_shows_no_change():
    """Touran 03G906021MP 1322 (P447HAXN): its coding-switched tables have
    second copies whose codings are a subset of the main copy's. Each copy
    tied with the main table on coding overlap and paired with it, so the
    file compared with itself showed 116 tables changed."""
    from ovtsuite.patch import diff_entities
    from ovtsuite.session import TunerSession
    s = TunerSession(_path("03G906021MP_1322_touran_ori.bin"))
    allents = list(s.entities) + list(s.variant_entities())
    same = s.sibling(bytes(s.binfile.data))
    assert diff_entities(s, same, allents) == []
    tuned = s.sibling(open(_path("03G906021MP_1322_touran_tuned.bin"), "rb").read())
    changed = diff_entities(s, tuned, allents)
    assert changed and len(changed) < 40


def test_tuning_maps_agree_with_the_structural_finder():
    """reconcile_with_finder: the seed rows and the walk had shifted P475AASJ's
    boost maps by one (the base map named PCR_pBDesMaxAP_MAP) and put
    PCR_rBPGovMin_MAP on P529TAWJ's 16x10 maximum-duty map."""
    from ovtsuite import kb as kb_mod
    t = {x.name: x for x in kb_mod.load("p475_aasj").tables}
    assert t["PCR_pBDesMaxAP_MAP"].y_units == "[hPa]"
    assert (t["PCR_pBDesBas2_MAP"].z_addr < t["PCR_pBDesBas_MAP"].z_addr
            < t["PCR_pBDesMaxAP_MAP"].z_addr)
    assert "AccPed_trqEng0Cold_MAP" in t
    s = _session("p529_tawj")
    n = {e.name: e for e in s.entities}
    hi = n["PCR_rBPGovMax_MAP"]
    lo = n["PCR_rBPGovMin_MAP"]
    assert (hi.rows, hi.cols) == (10, 16) and hi.addr < lo.addr
    top = lambda e: max(e.cell(r, c) for r in range(e.rows) for c in range(e.cols))
    assert top(hi) > top(lo)
