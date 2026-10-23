# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16U34 (03G906021 / P447) knowledge bases built from address-exact
definitions (tools/generate_kb_definition.py)."""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")

#: family -> (reference binary, dataset id it is routed by)
U34 = {
    "p447": ("03G906021KK.bin", "P447HAXN"),
    "p447_haxe": ("p447_haxe_ref.bin", "P447HAXE"),
    "p447_habb": ("03G906021AN_P447HABB_ref.bin", "P447HABB"),
    "p447_has9": ("03G906021SC_P447HAS9_ref.bin", "P447HAS9"),
}


def _ref(name):
    p = os.path.join(BIN, name)
    if not os.path.exists(p):
        pytest.skip("%s not present" % name)
    return p


@pytest.mark.parametrize("family", sorted(U34))
def test_definition_kb_places_everything_at_its_own_address(family):
    """The definition is address-exact for every file of its dataset, so on
    its own reference every table and value lands exactly where the
    definition puts it -- nothing shifted by the offset model."""
    from ovtsuite import kb as kb_mod
    from ovtsuite.match import match
    from ovtsuite.structure import find_cal_region
    d = open(_ref(U34[family][0]), "rb").read()
    k = kb_mod.load_cached(family)
    assert len(k.tables) > 2500 and len(k.constants) > 7500
    res = match(k, d, find_cal_region(d))
    assert len(res.items) == len(k.tables) + len(k.constants)
    moved = [it.name for it in res.items if it.target_addr != it.ref_addr]
    assert not moved, moved[:10]


@pytest.mark.parametrize("family", sorted(U34))
def test_dataset_routes_to_its_definition_kb(family):
    from ovtsuite.fileinfo import ident16
    from ovtsuite.session import TunerSession
    s = TunerSession(_ref(U34[family][0]))
    assert ident16(bytes(s.binfile.data), s.region).get("dataset_id") == U34[family][1]
    assert s.family == family
    names = {e.name: e for e in s.entities}
    # the per-gear driver's wish maps carry their Bosch symbols
    for g in range(7):
        e = names.get("AccPed_trqEng%d_MAP" % g)
        assert e is not None and e.kind == "map", g
    # descriptions are rendered English after the symbol
    e = names["AccPed_trqEng0_MAP"]
    assert e.description.startswith("A2L: AccPed_trqEng0_MAP | ")


def test_hbxb_is_served_by_the_haxn_kb():
    """HBXB is not refined any more: the A2L-built p447 KB holds every table
    the old 641-table p447_hbxb sub-family had, at the same addresses."""
    from ovtsuite import kb as kb_mod
    from ovtsuite.match import match
    from ovtsuite.structure import find_cal_region
    d = open(_ref("p447_hbxb_ref.bin"), "rb").read()
    r = find_cal_region(d)
    old = {it.name: it.target_addr
           for it in match(kb_mod.load_cached("p447_hbxb"), d, r).items}
    new = {it.name: it.target_addr
           for it in match(kb_mod.load_cached("p447"), d, r).items}
    assert old and all(new.get(n) == a for n, a in old.items())
    assert len(new) > 4 * len(old)


def test_cal_region_prefers_the_denser_of_two_equal_runs():
    """03G906021 HK 2854 / TQ 2186: the variant block at 0x184000 spans as
    many 16 KB buckets as the main calibration at 0x1C0000. The run with
    more structure headers is the calibration."""
    from ovtsuite.structure import find_cal_region
    buf = bytearray(b"\xff" * 0x200000)
    curve = struct.pack(">HHHH", 3, 10, 20, 30)

    def fill(start, end, per_bucket):
        for b in range(start, end, 0x4000):
            for i in range(per_bucket):
                a = b + i * 64
                buf[a:a + len(curve)] = curve

    fill(0x184000, 0x1B8000, 25)
    fill(0x1C0000, 0x1F4000, 40)
    lo, hi = find_cal_region(bytes(buf))
    assert lo <= 0x1C0000 < hi and lo >= 0x1B8000


@pytest.mark.parametrize("family", sorted(U34))
def test_measuring_blocks_are_named(family):
    """Every U34 family has a signal catalog: group 1 reads engine speed,
    injection quantity, main-injection duration, coolant temperature, and
    nearly every signal the binary's own blocks use has a name."""
    from ovtsuite.session import TunerSession
    from ovtsuite.signals import SignalCatalog, observed_ids
    from ovtsuite.msblock import MeasuringBlocks
    s = TunerSession(_ref(U34[family][0]))
    cat = SignalCatalog(s.family, s)
    g1 = MeasuringBlocks(s).groups()[0].channels()
    names = [cat.info(i).name for i in g1]
    assert names[0] == "Eng_nAvrg" and names[3] == "CTSCD_tClnt", names
    assert names[1].startswith("InjCtl_q") and names[2].startswith("InjVlv_phiMI1Dur")
    used = [i for i, n in observed_ids(s).items() if i != 255 and n]
    named = sum(1 for i in used if cat.info(i).known)
    assert named >= 0.85 * len(used), (named, len(used))
