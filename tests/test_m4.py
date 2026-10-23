# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""M4 tests: DSM tiering, DTC editor, axis-aware comparison, family detect."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


def test_dsm_not_expert():
    from ovtsuite.tiers import tier_of
    from ovtsuite import groups as G
    # every DSM parameter -- fault paths, classes, enables -- has its own
    # "Fault codes (DTCs)" group
    for n in ("DSM_CDCDfp_ACCD_C", "DSM_Class10Mil_C", "DSM_ClaDfp_ACCD_C",
              "DSM_FMKDfp_ACCD_C"):
        assert tier_of(n) == G.DTC
    # OBD/frame compliance is plumbing
    assert tier_of("FrmMng_anything") == G.INTERNALS


@needs_ref
def test_dtc_table_and_toggle():
    from ovtsuite.session import TunerSession
    from ovtsuite.dtc import DtcTable, DISABLED_CLASS
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    dt = DtcTable(s)
    assert len(dt.paths) > 300
    assert len(dt.classes) >= 10
    # a code that lights the MIL, toggled off and back on, restores exactly
    fp = next(f for f in dt.sorted_paths()
              if dt.is_enabled(f) and dt.lights_mil(f))
    orig = fp.current_class
    dt.set_enabled(fp, False)
    assert fp.current_class == DISABLED_CLASS
    assert not dt.is_enabled(fp)
    dt.set_enabled(fp, True)
    assert fp.current_class == orig
    assert dt.is_enabled(fp)


@needs_ref
def test_dtc_restore_from_stock():
    from ovtsuite.session import TunerSession
    from ovtsuite.dtc import DtcTable
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    dt = DtcTable(s)
    fp = next(f for f in dt.sorted_paths()
              if dt.is_enabled(f) and dt.lights_mil(f))
    orig = fp.current_class
    dt.set_enabled(fp, False)
    # rebuild a "stock" table from a fresh (unedited) session and restore
    stock = DtcTable(TunerSession(os.path.join(BIN, "038906016Q.Bin")))
    dt.restore_from(stock)
    assert fp.current_class == orig


@needs_ref
def test_overlay_direct_same_layout():
    from ovtsuite.session import TunerSession
    from ovtsuite.transfer import build_overlay
    a = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    b = TunerSession(os.path.join(BIN, "038906016AB.bin"))
    e = next(x for x in a.entities if x.name == "EngPrt_trqLimP_MAP")
    ov = build_overlay(e, a.counterpart(b, e))
    assert ov is not None and ov.mode == "direct"
    assert len(ov.values) == e.rows and len(ov.values[0]) == e.cols


@needs_ref
def test_overlay_resampled_cross_family():
    from ovtsuite.session import TunerSession
    from ovtsuite.transfer import build_overlay
    a = TunerSession(os.path.join(BIN, "038906016Q.Bin"))         # p350
    b = TunerSession(os.path.join(BIN, "03G906016LQ.bin"))        # p475
    e = next(x for x in a.entities if x.name == "EngPrt_trqLimP_MAP")
    ce = a.counterpart(b, e)
    if ce is None or (ce.rows, ce.cols) == (e.rows, e.cols):
        pytest.skip("shapes coincide; no resample to test")
    ov = build_overlay(e, ce)
    assert ov.mode == "resampled"
    # overlay is aligned to the PRIMARY's grid
    assert len(ov.values) == e.rows and len(ov.values[0]) == e.cols


@needs_ref
def test_family_detection_all():
    from ovtsuite.session import detect_family
    from ovtsuite.structure import find_cal_region
    expect = {
        "038906016Q.Bin": "p350",
        "038997016T.Bin": "p350",
        "038997016N.bin": "p350",         # header project code "P475" but
                                          # structurally p350 (override wins)
        "03G906016LQ.bin": "p475_83",
        "03G906016KN.bin": "p475_83",     # 8.3 -- must not fall to 8.4
        "03G906016MF.bin": "p475_84_mf",
        "03G906016HQ.bin": "p475_84_hq",
        "03G906021KK.bin": "p447",        # EDC16U34/P447 (project-gated, not p350)
        "03G997022E.bin": "p447",         # different P447 dataset, same family
        "03G906016AC.bin": "p447_906016",  # P447 dataset P447HAS7 (BRM), A2L KB
    }
    for name, fam in expect.items():
        p = os.path.join(BIN, name)
        if not os.path.exists(p):
            continue
        d = open(p, "rb").read()
        assert detect_family(d, find_cal_region(d)) == fam, name


@needs_ref
def test_p447_906016_named_from_a2l():
    """The 906016 (P447HAS7) A2L KB gives every entity a real Bosch symbol name
    and anchors the per-gear driver-wish maps -- verified on the AC reference,
    the BRM's own dataset."""
    from ovtsuite.session import TunerSession
    p = os.path.join(BIN, "03G906016AC.bin")
    if not os.path.exists(p):
        pytest.skip("AC reference not present")
    s = TunerSession(p)
    assert s.family == "p447_906016"
    names = {e.name: e for e in s.entities}
    # no @0x placeholders: everything is A2L-named
    assert not any(n.startswith("@0x") for n in names)
    # the per-gear driver-wish maps place structurally (anchored/verified/struct)
    ok = {"anchored", "verified", "struct", "bracketed"}
    for g in range(7):
        e = names.get("AccPed_trqEng%d_MAP" % g)
        assert e is not None, g
        assert getattr(e, "source", None) in ok, (g, getattr(e, "source", None))
    # the functional groups the tuner cares about are all present by name
    for pre in ("InjCrv_", "PCR_", "AirCtl_", "LIGov_", "EGRVlv_", "Gearbx_"):
        assert any(n.startswith(pre) for n in names), pre


@needs_ref
def test_ident16_part_number_all_families():
    # The File-Loaded ident must surface the ECU part number for EVERY family --
    # p350 regressed because the record was anchored on its FIRST field (a Bosch
    # number on 906 parts), not the second (the definitive VAG part number).
    from ovtsuite.fileinfo import ident16
    from ovtsuite.structure import find_cal_region
    expect = {
        "038906016Q.Bin": "038906016Q",     # p350, first field = Bosch number
        "038906016AB.bin": "038906016AB",    # p350, first field = Bosch number
        "038997016R.bin": "038997016R",      # p350, first field = 038906016K (hardware)
        "038997016T.Bin": "038997016T",      # p350, gap field carries non-null bytes
        "03G906016LQ.bin": "03G906016LQ",    # p475
        "03G906016MF.bin": "03G906016MF",    # p475
    }
    for name, part in expect.items():
        d = open(os.path.join(BIN, name), "rb").read()
        info = ident16(d, find_cal_region(d))
        assert info.get("part") == part, (name, info.get("part"))
        assert info.get("ident_addr") is not None, name   # structured match, not fallback


@needs_ref
def test_p447_906016_measuring_blocks_named():
    """The 906016 (P447HAS7) measuring-block catalog resolves group 1 to the
    VCDS ground truth (Engine speed / Injection qty / Duration / Coolant),
    proving the signal catalog and resolver chain populate for the BRM family."""
    import os as _os
    if not _os.path.exists(_os.path.join(
            _os.path.dirname(__file__), "..", "ovtsuite", "data",
            "signals_p447_906016.json")):
        pytest.skip("signals_p447_906016.json not built")
    from ovtsuite.session import TunerSession
    from ovtsuite.msblock import MeasuringBlocks
    from ovtsuite.signals import SignalCatalog
    p = os.path.join(BIN, "03G906016AC.bin")
    if not os.path.exists(p):
        pytest.skip("AC reference not present")
    s = TunerSession(p)
    assert s.family == "p447_906016"
    mb = MeasuringBlocks(s)
    assert mb.available and len(mb.populated_groups()) > 50
    cat = SignalCatalog(s.family, s)
    g1 = mb.groups()[0]
    labels = [cat.label(c).lower() for c in g1.channels()]
    assert any("engine speed" in x for x in labels)
    assert any("injection" in x for x in labels)
    assert any("coolant" in x for x in labels)


@needs_ref
def test_write_init_strategy_from_family():
    # The K-line write init strategy is derived from the OS *family* (identified
    # structurally by the app), not the part number: P350 accepts a cold slow
    # init (False = slow only); every later P475 CAN-init family must be primed
    # (True = fast-init prime first).
    from ovtsuite.flash.edc16 import fast_prime_from_image
    expect = {
        "038906016Q.Bin": False,     # p350
        "038997016T.Bin": False,     # p350
        "038906016AB.bin": False,    # p350 (part number would misread the 038-series)
        "03G906016LQ.bin": True,     # p475_83
        "03G906016KN.bin": True,     # p475_83
        "03G906016MF.bin": True,     # p475_84_mf
        "03G906016HQ.bin": True,     # p475_84_hq
        "03G906016AC.bin": False,    # P447/BRM -- confirmed cold slow init on bench
    }
    for name, want in expect.items():
        d = open(os.path.join(BIN, name), "rb").read()
        assert fast_prime_from_image(d) is want, name
    # An unrecognisable image falls through to the AUTO slow-first fallback.
    assert fast_prime_from_image(b"\xFF" * 0x200000) is None


@needs_ref
def test_interp_edge_clamp():
    from ovtsuite.transfer import _interp1
    xs = [0.0, 10.0, 20.0]
    ys = [100.0, 200.0, 400.0]
    assert _interp1(xs, ys, -5) == 100.0      # below -> clamp low
    assert _interp1(xs, ys, 25) == 400.0      # above -> clamp high
    assert _interp1(xs, ys, 5) == 150.0       # midpoint
    # descending axis
    assert _interp1([20.0, 10.0, 0.0], [400.0, 200.0, 100.0], 5) == 150.0


@needs_ref
def test_measuring_block_map_loads_full():
    """The 255-group measuring-block table loads at full size, not capped."""
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    g = next(x for x in s.entities if x.name == "UACC_RSigBlkGrp_MAP")
    assert (g.rows, g.cols) == (255, 4)


@needs_ref
def test_measuring_blocks_model_and_edit():
    from ovtsuite.session import TunerSession
    from ovtsuite.msblock import MeasuringBlocks
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    mb = MeasuringBlocks(s)
    assert mb.available
    groups = mb.groups()
    assert len(groups) == 127             # rows 128+ are cells 5-8 of 1..127
    assert groups[0].number == 1          # 1-based like a scan tool
    assert groups[0].slots == 8
    assert mb.related                      # definition tables located
    g = next(x for x in groups if x.populated)
    before = g.channels()
    mb.set_channel(g, 1, 123)
    assert g.channels()[1] == 123
    s.undo()
    assert g.channels() == before
    # clear-group empties all slots (255; id 0 is engine speed), undoably
    g2 = next(x for x in groups if x.populated)
    before2 = g2.channels()
    mb.clear_group(g2)
    assert g2.channels() == [255] * g2.slots
    s.undo()
    assert g2.channels() == before2


@needs_ref
def test_xdf_export_edc16_never_lsb_first():
    """EDC16 images are big-endian: the exported XDF must NOT set the
    LSB-first typeflag bit (0x02) on any entity, and the header default must
    be lsbfirst=0. Regression for unsigned params being byte-swapped in
    TunerPro (0x02 == little-endian)."""
    import re
    from ovtsuite.session import TunerSession
    from ovtsuite.xdf_export import export_xdf
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    xml = export_xdf(s.entities, "t", s.binfile.name,
                     image_size=len(s.binfile.data), platform="edc16")
    assert 'lsbfirst="0"' in xml
    flags = set(re.findall(r'mmedtypeflags="(0x[0-9A-Fa-f]+)"', xml))
    # only unsigned (0x00), signed (0x01), and float axis (0x04) may appear;
    # 0x02 / 0x03 would mean the LSB-first bit was set on big-endian data.
    assert not (flags & {"0x02", "0x03", "0x06", "0x07"}), sorted(flags)



@needs_ref
def test_project_code_override_when_structure_disagrees():
    """038997016N carries a "P475" project code in its header but its map
    lineage is p350. The project code is only a prior: a strong structural
    match must override it, otherwise the binary is force-fit to a p475 KB and
    ~95% of parameters land in flash-erased space (VSS reads 0xFFFFFFFF, gears
    read garbage). Regression for that misdetection."""
    import os
    from ovtsuite.session import TunerSession
    from ovtsuite.variant import vss_pulse_settings
    p = os.path.join(BIN, "038997016N.bin")
    if not os.path.exists(p):
        pytest.skip("038997016N.bin not present")
    s = TunerSession(p)
    # p350 lineage -- as a U8A7 build, its exact p350_u8a7 sub-family
    assert s.family == "p350_u8a7"
    # almost everything resolves structurally, not by the offset model
    projected = sum(1 for e in s.entities if getattr(e, "source", "") == "projected")
    assert projected < len(s.entities) * 0.10, projected
    # VSS resolves to a real pulses/km, not the 0xFFFFFFFF erased-flash read
    rows = vss_pulse_settings(s)
    assert rows and rows[0]["primary"]
    assert 2000 <= rows[0]["imp_km"] <= 8000, rows[0]["imp_km"]


@needs_ref
def test_length_changed_block_base_placement():
    """A pointer-table block whose length changed internally still pairs by
    position between two aligned equal-length blocks; its base is an exact
    base+displacement anchor. Regression for the FrmMng CAN-frame constants
    (dMulinfo2_*, the Charger-2 debounce group) being projected into an
    overlapping/misplaced address on the 2.0L 038997016K dataset."""
    import os
    from ovtsuite.session import TunerSession
    p = os.path.join(BIN, "038997016K.bin")
    if not os.path.exists(p):
        pytest.skip("038997016K.bin not present")
    s = TunerSession(p)
    assert s.family == "p350"
    n = {e.name: e for e in list(s.entities) + list(s.variant_entities())}
    # the whole FrmMng constant run must be monotonic (no overlap/inversion)
    run = sorted((e.addr, e.name) for e in n.values()
                 if e.name.startswith("FrmMng_") and e.kind == "constant")
    inversions = sum(1 for i in range(1, len(run)) if run[i][0] < run[i - 1][0])
    assert inversions == 0, inversions
    # the Charger-2 timeout group is contiguous and precedes the multiplex
    # constants, which are three consecutive bytes
    d0 = n["FrmMng_dMulinfo2_0_C"]
    d1 = n["FrmMng_dMulinfo2_1_C"]
    d2 = n["FrmMng_dMulinfo2_2_C"]
    assert d1.addr == d0.addr + 1 and d2.addr == d0.addr + 2
    assert n["FrmMng_DebCHRGR2TOOk_C"].addr < d0.addr
    # and the block-base placement lands them at the -100 (block base) delta,
    # not the -132 map-anchor delta that caused the overlap
    assert d0.addr == n["FrmMng_DebCHRGR2TOOk_C"].addr + 0x1D  # 0x211C -> 0x2139


def test_degenerate_axis_is_not_read_as_a_map():
    """A 1-D curve can come out of an XDF as a 1xN table with a
    placeholder second axis: one element, stored at the SAME address as the Z
    data. Counting it as real makes the table a "map", which then takes the
    map header offset (first axis - 4) instead of the curve one (axis - 2), so
    the fingerprint is cut from the wrong bytes and the entity never anchors.

    888 tables across the two p447 references were being read that way. On
    03G906021KK the share of placed headers with a real structure under them
    went from 42% to 99% once this was fixed.
    """
    import glob
    import os as _os
    from ovtsuite.xdf import parse_xdf

    xdfs = sorted(glob.glob(_os.path.join(REF, "XDFs", "*.xdf")))
    if not xdfs:
        import pytest
        pytest.skip("reference XDFs not present")
    for path in xdfs:
        for t in parse_xdf(path).tables:
            for ax in (t.x, t.y):
                if ax is None or ax.addr is None:
                    continue
                if ax.addr == t.z_addr and ax.count <= 1:
                    assert t.kind != "map", (
                        "%s in %s has a placeholder axis and must not be a map"
                        % (t.name, _os.path.basename(path)))


def test_curve_struct_addr_is_two_before_its_axis():
    """The header offset follows from the kind, so a mis-typed curve puts the
    fingerprint two bytes early."""
    import glob
    import os as _os
    from ovtsuite.xdf import parse_xdf

    xdfs = sorted(glob.glob(_os.path.join(REF, "XDFs", "*.xdf")))
    if not xdfs:
        import pytest
        pytest.skip("reference XDFs not present")
    seen = 0
    for path in xdfs:
        for t in parse_xdf(path).tables:
            if t.kind == "curve":
                assert t.struct_addr == t.input_axis.addr - 2
                seen += 1
            elif t.kind == "map":
                first = min(t.x.addr, t.y.addr)
                assert t.struct_addr == first - 4
    assert seen > 1000, "expected plenty of curves across the references"


def test_a_one_dimensional_table_written_as_a_column_is_normalised():
    """TunerPro renders a curve either way -- 1xN with the breakpoints on x,
    or Nx1 with them on y -- and which one a converter emits is a display
    choice over identical bytes. rows/cols used to disagree with `kind` in the
    column case, so a curve's declared span and every dimension check used 1
    where it needed N. 398, 418, 431 and 548 tables in four EDC16 references
    are in this form, and the ZF definitions regenerated in September 2026 put
    every curve in it, where it took structure verification from 100% to 40%.
    """
    from ovtsuite.xdf import XdfAxis, XdfTable, _as_row

    col = XdfTable(name="curve as a column", z_addr=0x2000, rows=5, cols=1,
                   x=XdfAxis(addr=None, count=1),
                   y=XdfAxis(addr=0x1FF0, count=5))
    _as_row(col)
    assert (col.rows, col.cols) == (1, 5)
    assert col.kind == "curve"
    assert col.input_axis.addr == 0x1FF0
    assert col.struct_addr == 0x1FEE

    row = XdfTable(name="curve as a row", z_addr=0x2000, rows=1, cols=5,
                   x=XdfAxis(addr=0x1FF0, count=5),
                   y=XdfAxis(addr=0x2000, count=1))
    before = (row.rows, row.cols, row.x.addr)
    _as_row(row)
    assert (row.rows, row.cols, row.x.addr) == before

    a_map = XdfTable(name="map", z_addr=0x2000, rows=8, cols=1,
                     x=XdfAxis(addr=0x1F00, count=1),
                     y=XdfAxis(addr=0x1F10, count=8))
    _as_row(a_map)
    assert a_map.kind == "map"
    assert (a_map.rows, a_map.cols) == (8, 1)

    indexed = XdfTable(name="indexed", z_addr=0x2000, rows=6, cols=1,
                       x=XdfAxis(addr=None, count=1),
                       y=XdfAxis(addr=0x2000, count=1))
    _as_row(indexed)
    assert indexed.kind == "indexed"
    assert (indexed.rows, indexed.cols) == (6, 1)
