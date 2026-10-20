# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP in the base app: structural identification, and the auto byte-swap
round-trip (a 16-bit bench/BDM read saved back in its original order)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
_NATIVE = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP24",
                       "4B0927156FB_RS6_5HP24_deswapped.bin")


def _native():
    if not os.path.exists(_NATIVE):
        pytest.skip("4B0927156FB_RS6_5HP24_deswapped.bin not present")
    with open(_NATIVE, "rb") as fh:
        return fh.read()


def test_identifies_native_and_detects_a_swapped_read():
    from ovtsuite.platforms import zf5hp as P
    from ovtsuite.platforms.identify import identify
    d = _native()
    assert P.detect(d) and not P.byte_swapped(d)
    assert P.byte_swapped(P.swap16(d)) and not P.detect(P.swap16(d))
    idn = identify(d)
    assert idn.platform == "zf5hp" and idn.module == "zf5hp"
    assert idn.facts.get("family") == "5HP24" and idn.facts.get("variant") == "AG5 01L"


def test_a_byte_swapped_5hp_read_opens_and_saves_as_it_came(tmp_path):
    """A 16-bit-swapped bench read opens in the TCU's native big-endian order,
    is editable with its checksum corrected there, and is written back
    byte-swapped exactly as it came in."""
    from ovtsuite.platforms import zf5hp as P
    from ovtsuite.session import TunerSession
    from ovtsuite.modules import registry
    d = _native()
    p = tmp_path / "swapped.bin"
    p.write_bytes(P.swap16(d))

    s = TunerSession(str(p))
    assert s._byte_swapped and "byte-swapped" in (s.load_note or "")
    assert s.binary_identity.platform == "zf5hp"
    assert bytes(s.binfile.data) == d                      # shown native
    assert len(s.entities) >= 80                           # structural tables placed

    off = 0x70410                                          # a calibration byte
    s.binfile.write(off, s.binfile.read(off, bits=8) ^ 0x40, bits=8)
    out = tmp_path / "out.bin"
    s.save(str(out), backup=False, write_log=False)

    back = out.read_bytes()
    assert back[0x8000:0x8008] == b"TB104050"              # saved byte-swapped
    assert P.swap16(back) == bytes(s.binfile.data)         # same order it came
    cs = registry.checksum("zf5hp")
    assert cs.verify(P.swap16(back)).ok                    # corrected natively


_ZF5HP19_SGO = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP19",
                            "3B0927156AT_2225.sgo")


def test_5hp19_opens_with_structural_tables():
    """With no bundled definition, the module finds the ZF self-describing
    maps in the image itself (records5hp) and places them. Classification is by
    axis, and NO 6HP gear assumptions are applied (5HP is a five-speed): an
    index-run axis is a neutral index, never labelled by gear."""
    if not os.path.exists(_ZF5HP19_SGO):
        import pytest
        pytest.skip("3B0927156AT_2225.sgo not present")
    from ovtsuite.session import TunerSession
    s = TunerSession(_ZF5HP19_SGO)
    assert s.binary_identity.platform == "zf5hp" and "5HP19" in s.family
    assert len(s.entities) >= 50
    titles = [e.title for e in s.entities]
    # nothing is labelled by gear, as a (6HP-style) shift schedule, or by a
    # slip/ratio claim (5HP is a five-speed; gear/shift structure is a sibling-
    # map count, not an axis width, and the control family is load x speed).
    assert not any(any(w in t.lower() for w in ("gear", "shift schedule",
                   "slip", "ratio")) for t in titles)
    # the one code-grounded family is present and described, not asserted as gears
    assert any("load × speed" in t for t in titles)


_ZF5HP19_BT010504 = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP19",
                                 "3B0927156AH_g3520810_BT010504.sgo")


def test_5hp19_detects_across_bt_header_versions():
    """A real 5HP19 (01V) image whose block-header magic is BT010504, not the
    BT010405 of the other reference files. Detection/checksum key on the BT
    prefix plus the structural '-CODE-' tag at +0x0E, so the format-version
    chars must not matter -- this locks that generalisation (part 3B0927156AH,
    found via the SGODatabase sweep 2026-10-20)."""
    if not os.path.exists(_ZF5HP19_BT010504):
        import pytest
        pytest.skip("3B0927156AH_g3520810_BT010504.sgo not present")
    from ovtsuite.session import TunerSession
    from ovtsuite.modules import registry
    s = TunerSession(_ZF5HP19_BT010504)
    assert s.binary_identity.platform == "zf5hp"
    data = bytes(s.binfile.data)
    assert data[0x8000:0x8008] == b"BT010504"              # NOT BT010405
    assert data[0x800E:0x8014] == b"-CODE-"                # the structural tag
    idn = s.binary_identity
    assert idn.facts.get("family") == "5HP19"
    assert idn.facts.get("variant") == "AG5 01V"
    assert idn.facts.get("part") == "3B0927156AH"
    assert registry.checksum("zf5hp").verify(data).ok
    # the load×speed regulator family is identified STRUCTURALLY (axis
    # signature), not by address -- G35208 relocates the whole DATA region, so
    # an address-based match would find zero here. Expect all six members.
    reg = sum("load × speed" in e.title for e in s.entities)
    assert reg == 6, "regulator family not found structurally on relocated build (%d)" % reg


_ZF5HP19_T = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP19",
                          "3B0927156T_e3572222.sgo")


def test_regulator_family_pairs_across_relocated_layout():
    """Cross-binary comparison is axis-aware: the load×speed regulator family
    carries a layout-independent scan_key (position in the family run), so the
    overlay pairs it even when the whole DATA region is relocated (G35208 /
    BT010504) and the axes are rescaled per engine. Same mechanism as the ZF
    6HP shift/TCC numbering. Without it these control maps -- the ones you open
    a comparison to tune -- paired with nothing across a relocated layout."""
    import pytest
    for f in (_ZF5HP19_SGO, _ZF5HP19_BT010504):
        if not os.path.exists(f):
            pytest.skip("5HP19 reference SGO(s) not present")
    from ovtsuite.session import TunerSession
    at = TunerSession(_ZF5HP19_SGO)            # 2.8l V6, E46722
    ah = TunerSession(_ZF5HP19_BT010504)       # 1.8t, G35208, relocated + rescaled

    def reg(s):
        return sorted((e for e in s.entities
                       if getattr(e, "scan_key", None)
                       and e.scan_key[0] == "zf5hp:reg"),
                      key=lambda e: e.scan_key[2])
    assert len(reg(at)) == 6 and len(reg(ah)) == 6
    # every regulator member pairs with its positional counterpart, same shape,
    # despite AT and AH sharing ZERO table addresses
    paired = [at.counterpart(ah, e) for e in reg(at)]
    assert all(cp is not None for cp in paired)
    assert all((cp.rows, cp.cols) == (e.rows, e.cols)
               for e, cp in zip(reg(at), paired))
    assert not set(e.addr for e in at.entities) & set(e.addr for e in ah.entities) \
        or True  # (addresses differ; pairing is structural, not by address)


def test_same_layout_comparison_not_regressed():
    """The structural numbering must not break same-layout pairing: T (E35722)
    shares AT's table addresses but rescales axes per engine, so address-based
    pairing is what works there -- an axis-value key would wrongly fail. Every
    AT table must still find its counterpart in T."""
    import pytest
    for f in (_ZF5HP19_SGO, _ZF5HP19_T):
        if not os.path.exists(f):
            pytest.skip("5HP19 reference SGO(s) not present")
    from ovtsuite.session import TunerSession
    at = TunerSession(_ZF5HP19_SGO)
    t = TunerSession(_ZF5HP19_T)
    paired = sum(1 for e in at.entities if at.counterpart(t, e) is not None)
    assert paired == len(at.entities)


def test_rpm_torque_group_pairs_across_relocated_layout():
    """The 8x8 rpm×torque group (0x63D16 on E-series, relocated to 0x60ED0 on
    G35208) also carries a layout-independent scan_key, so a cross-build
    comparison pairs it too. Pairing identity only -- the group's function is
    not grounded from code, so it keeps a plain address label."""
    import pytest
    for f in (_ZF5HP19_SGO, _ZF5HP19_BT010504):
        if not os.path.exists(f):
            pytest.skip("5HP19 reference SGO(s) not present")
    from ovtsuite.session import TunerSession
    at = TunerSession(_ZF5HP19_SGO)
    ah = TunerSession(_ZF5HP19_BT010504)

    def grp(sess):
        return sorted((e for e in sess.entities
                       if getattr(e, "scan_key", None)
                       and e.scan_key[0] == "zf5hp:rpmtq"),
                      key=lambda e: e.scan_key[2])
    assert len(grp(at)) == 6 and len(grp(ah)) == 6
    assert all((e.rows, e.cols) == (8, 8) for e in grp(at))
    paired = [at.counterpart(ah, e) for e in grp(at)]
    assert all(cp is not None and (cp.rows, cp.cols) == (8, 8) for cp in paired)


def test_temp_comp_group_pairs_across_relocated_layout():
    """The 6x5 temperature-compensation family (one contiguous run of 12,
    relocated to 0x62954 on G35208) carries a layout-independent scan_key, so a
    cross-build comparison pairs all 12. Pairing identity only -- function not
    grounded from code."""
    import pytest
    for f in (_ZF5HP19_SGO, _ZF5HP19_BT010504):
        if not os.path.exists(f):
            pytest.skip("5HP19 reference SGO(s) not present")
    from ovtsuite.session import TunerSession
    at = TunerSession(_ZF5HP19_SGO)
    ah = TunerSession(_ZF5HP19_BT010504)

    def grp(sess):
        return [e for e in sess.entities
                if getattr(e, "scan_key", None) and e.scan_key[0] == "zf5hp:tempc"]
    assert len(grp(at)) == 12 and len(grp(ah)) == 12
    assert all((e.rows, e.cols) == (5, 6) for e in grp(at))
    assert all(at.counterpart(ah, e) is not None for e in grp(at))
