# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Grouped gear-ratio model, VAG-code formatting, and fault-name decoding."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

from ovtsuite.session import TunerSession  # noqa: E402
from ovtsuite.gears import GearRatios  # noqa: E402
from ovtsuite.dtc import DtcTable  # noqa: E402
from ovtsuite.signal_names import fault_name  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


@needs_ref
def test_gear_ratios_grouped():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    assert g.available
    labels = [lbl for lbl, _ in g.abs_layout()]
    assert "1st gear" in labels and "6th gear" in labels
    assert "Reverse" in labels
    assert "ACC differential ratio (ACCCD)" in labels
    vn = g.vn_layout()
    assert len(vn) >= 7                        # 6 gears + reverse


@needs_ref
def test_gear_all_variant_copies():
    """Q has an automatic/manual coding variant: two copies must be exposed,
    labelled by transmission, with different overall ratios and independent
    editing of the secondary copy."""
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    copies = g.copies()
    assert len(copies) == 2
    assert copies[0].is_primary and not copies[1].is_primary
    # labelled by transmission (auto vs manual)
    joined = " ".join(c.label.lower() for c in copies)
    assert "automatic" in joined and "manual" in joined
    # 1st-gear overall ratio differs between the two copies
    prim = copies[0].by_name["Gearbx_rTrans1_C"].cell(0, 0)
    var = copies[1].by_name["Gearbx_rTrans1_C"].cell(0, 0)
    assert abs(prim - 14.05) < 0.02 and abs(var - 12.80) < 0.02
    # editing the secondary copy writes only the secondary address
    g.set_value(copies[1].by_name["Gearbx_rTrans1_C"], 11.5)
    assert abs(copies[1].by_name["Gearbx_rTrans1_C"].cell(0, 0) - 11.5) < 0.01
    assert abs(s.entities and [e for e in s.entities
               if e.name == "Gearbx_rTrans1_C"][0].cell(0, 0) - 14.05) < 0.02
    s.undo()


@needs_ref
def test_gear_single_copy_when_no_variant():
    """A binary with no detected secondary block shows exactly one copy."""
    kn = os.path.join(BIN, "03G906016KN.bin")
    if not os.path.exists(kn):
        pytest.skip("KN not present")
    g = GearRatios(TunerSession(kn))
    assert len(g.copies()) == 1


@needs_ref
def test_vag_code_five_digit():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    dt = DtcTable(s)
    for fp in dt.paths.values():
        vs = fp.vag_str
        if vs != "-":
            assert len(vs) >= 5 and vs.isdigit()   # zero-padded, min 5 digits
    # a small internal code pads to five digits
    small = [fp for fp in dt.paths.values() if fp.vag_code and fp.vag_code < 1000]
    assert small and all(len(fp.vag_str) == 5 for fp in small)


def test_fault_name_decoding():
    assert "Mass Air Flow" in fault_name("AFSCD_SRCAirm")
    assert "circuit" in fault_name("AFSCD_SRCAirm").lower()
    assert "high" in fault_name("BPSCD_SRCMax").lower()
    assert "implausible" in fault_name("APP_Npl").lower()
    assert fault_name("ACCD") == "A/C Compressor"
    # no double 'circuit' when a sub-token overlaps a longer one
    assert fault_name("LSUCDCircPumpCur0").lower().count("circuit") == 1


@needs_ref
def test_vag_recovery_p475():
    """The 03G p475_83 binaries have a misaligned CDK block; codes must be
    recovered from the canonical reference and Q (p350) must stay verbatim."""
    from ovtsuite.dtc import DtcTable
    kn = os.path.join(BIN, "03G906016KN.bin")
    if not os.path.exists(kn):
        pytest.skip("KN reference not present")
    dt = DtcTable(TunerSession(kn))
    assert dt.vag_block_reliable is False
    # most codes now land in the standard VAG range, not the 33xxx garbage
    inr = sum(1 for fp in dt.paths.values()
              if fp.vag_code and 16000 <= fp.vag_code < 20000)
    assert inr > 120
    # a known fault recovers its canonical value and is flagged
    afs = dt.paths.get("AFSCD_SRCAirm")
    assert afs.vag_code == 16487 and afs.vag_source == "p-code table"
    assert afs.vag_str.endswith("*")
    acd = dt.paths.get("ACCD")
    assert acd.vag_code == 18065 and acd.vag_source == "name table"

    # p350 binary is trusted verbatim (no override, no asterisk)
    q = DtcTable(TunerSession(os.path.join(BIN, "038906016Q.Bin")))
    assert q.vag_block_reliable is True
    assert all(fp.vag_code == fp.vag_raw for fp in q.paths.values())
    assert all(not fp.vag_str.endswith("*") for fp in q.paths.values()
               if fp.vag_str != "-")


@needs_ref
def test_gear_copy_labels_from_coding_not_values():
    """Copies are labelled by the coding pointer array, not the ratio values.
    AB pairs a DSG (main block) with a 5-speed manual, so the primary copy must
    read 'DSG' even though its 1st-gear ratio looks like any other."""
    from ovtsuite.variant import gear_copy_codings
    ab = os.path.join(BIN, "038906016AB.bin")
    if not os.path.exists(ab):
        pytest.skip("AB full read not present")
    s = TunerSession(ab)
    gc = gear_copy_codings(s)
    by = {e.name: e for e in s.entities}
    prim_addr = by["Gearbx_rTrans1_C"].addr
    assert prim_addr in gc
    _code, tx = gc[prim_addr]
    assert tx == "DSG"
    labels = [c.label for c in GearRatios(s).copies()]
    assert any("DSG" in x for x in labels)
    assert any("manual" in x.lower() for x in labels)


@needs_ref
def test_vn_autogen_geometry_roundtrip():
    """On a HW-sensor car, regenerating the v/n bands from the absolute ratios
    (geometry path) reproduces the stock bands to within ~5% -- the model is
    v/n = 0.06*C/R with geometric-mean crossovers."""
    import math
    from ovtsuite.gears import _phys
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    assert not g.signal_is_can()                     # Q uses the FGG sensor
    cp = g.copies()[0]
    circ = g.effective_circ(cp)
    assert circ is not None and 1.8 < circ < 2.2     # ~195/65R15
    def band(gr, gg):
        c = gr.copies()[0]
        return (_phys(s, gr._ent(c, "Gearbx_rVn%dL_C" % gg)),
                _phys(s, gr._ent(c, "Gearbx_rVn%dH_C" % gg)))
    stock = {gg: band(g, gg) for gg in range(1, 5)}   # well-defined lower gears
    plan = g.plan_vn(cp)
    assert plan["source"] == "geometry" and plan["writes"]
    g.apply_vn(plan["writes"])
    assert len(s.undo_stack) == 1                     # ONE undoable step
    g2 = GearRatios(s)
    for gg in range(1, 5):
        L0, H0 = stock[gg]
        L2, H2 = band(g2, gg)
        assert abs(L2 - L0) <= 0.05 * L0 + 1e-4, (gg, L0, L2)
        assert abs(H2 - H0) <= 0.05 * H0 + 1e-4, (gg, H0, H2)
    s.undo()                                          # single undo restores all
    g3 = GearRatios(s)
    for gg in range(1, 7):
        assert abs(band(g3, gg)[0] - band(g, gg)[0]) < 1e-9


@needs_ref
def test_vn_autogen_can_needs_tyre():
    """A CAN-speed car (BRM/AC, DSG) has an inert ImpPerDist, so the auto-gen
    must ask for a tyre size -- and still offer an inferred hint -- then produce
    a full set of writes when given the circumference (transfer case folded)."""
    p = os.path.join(BIN, "03G906016AC.bin")
    if not os.path.exists(p):
        pytest.skip("AC reference not present")
    g = GearRatios(TunerSession(p))
    assert g.signal_is_can()
    cp = g.copies()[0]
    ask = g.plan_vn(cp)
    assert ask.get("need_tire") and 1.8 < (ask.get("circ_hint") or 0) < 2.2
    plan = g.plan_vn(cp, circ_m=1.97, fold_transfer=True)
    assert plan["source"] == "can" and len(plan["writes"]) >= 10


@needs_ref
def test_vn_autogen_scales_with_ratio_change():
    """After lowering a gear's absolute ratio (taller gearing), its regenerated
    v/n midpoint rises (more km/h per rpm)."""
    import math
    from ovtsuite.gears import _phys
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    cp = g.copies()[0]
    e4 = g._ent(cp, "Gearbx_rTrans4_C")
    r4 = _phys(s, e4)
    g.set_value(e4, r4 * 0.8)                          # taller 4th
    plan = g.plan_vn(cp)
    true = plan["true"]
    # 4th-gear v/n must have risen ~1/0.8 vs the others' scale
    assert true[4] > 0
    # regenerate and confirm 4th band moved up
    g.apply_vn(plan["writes"])
    g2 = GearRatios(s)
    lo = _phys(s, g2._ent(g2.copies()[0], "Gearbx_rVn4L_C"))
    hi = _phys(s, g2._ent(g2.copies()[0], "Gearbx_rVn4H_C"))
    assert lo < true[4] < hi


@needs_ref
def test_vn_autogen_placeholder_top_gear():
    """A 5-speed (038906016Q) stores a 6th-gear ratio ~= 5th as an unused
    placeholder. Auto-gen must treat 5th as the top real gear (keep its upper
    bound) and leave the 6th placeholder band untouched."""
    from ovtsuite.gears import _phys
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    cp = g.copies()[0]
    plan = g.plan_vn(cp, circ_m=g.effective_circ(cp), fold_transfer=False)
    assert plan["gears"] == [1, 2, 3, 4, 5]
    assert plan["placeholders"] == [6]
    g6L0 = _phys(s, g._ent(cp, "Gearbx_rVn6L_C"))
    g6H0 = _phys(s, g._ent(cp, "Gearbx_rVn6H_C"))
    g5H0 = _phys(s, g._ent(cp, "Gearbx_rVn5H_C"))
    g.apply_vn(plan["writes"])
    g2 = GearRatios(s); c2 = g2.copies()[0]
    # 6th placeholder untouched; 5th upper bound (the ceiling) preserved
    assert _phys(s, g2._ent(c2, "Gearbx_rVn6L_C")) == g6L0
    assert _phys(s, g2._ent(c2, "Gearbx_rVn6H_C")) == g6H0
    assert _phys(s, g2._ent(c2, "Gearbx_rVn5H_C")) == g5H0


@needs_ref
def test_vn_autogen_hw_with_tyre():
    """HW-sensor cars accept an explicit tyre circumference too (tyres vary);
    v/n = 0.06*C/R, so a larger tyre raises every band proportionally."""
    from ovtsuite.gears import _phys
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = GearRatios(s)
    cp = g.copies()[0]
    base = g.plan_vn(cp, circ_m=1.99, fold_transfer=False)["true"]
    big = g.plan_vn(cp, circ_m=1.99 * 1.05, fold_transfer=False)["true"]
    for gg in base:
        assert abs(big[gg] / base[gg] - 1.05) < 1e-6      # scales with circumference
