# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Calibration value files (DCM, CDFX): read, written, exported from a
binary through a definition and applied back into one.

Run alone: ``python -m pytest tests/test_convert_values.py -q`` (add
``--slow`` for the checks against the DAMOS corpus, which skip when it is
not there; ``OVT_DAMOS_CORPUS`` points at it).
"""

import os

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import calvalues as V, fmt_cdfx, fmt_dcm, image as I

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
VF = os.path.join(REF, "ValueFiles")
SAMPLES = {
    "simos185_73AEb_textstring.DCM": {"text": 1, "axis": 1},
    "simos_RadBli_2d_block.dcm": {"block": 2, "axis": 3},
    "scm900_42AEb_changed_labels.DCM": {"text": 3, "axis": 13},
    "sc4800_49AEfF_changed_labels.CDFX": {"cuboid": 2, "axis": 31, "map": 20},
}
A2L = os.path.join(REF, "A2L", "BPW.a2l")
LQ = os.path.join(REF, "Binaries", "03G906016LQ.bin")


def need(*paths):
    for p in paths:
        if not os.path.isfile(p):
            pytest.skip("reference file missing: %s" % p)


def sample(name):
    p = os.path.join(VF, name)
    need(p)
    with open(p, "rb") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


# ------------------------------------------------------------------ reading
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_samples_read_whole(name):
    """Every parameter reads with its declared size; nothing is left over."""
    data = sample(name)
    assert C.sniff(data)[0] == "values"
    vs = V.load(data)
    kinds = {}
    for p in vs.params:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
        assert not p.notes, (p.name, p.notes)
    for k, n in SAMPLES[name].items():
        assert kinds.get(k) == n, (k, kinds)


def test_dcm_keywords():
    vs = V.load(sample("simos185_73AEb_textstring.DCM"))
    ts = {p.name: p for p in vs.params}["ecu_id_sys_desc"]
    assert ts.kind == "text" and ts.z == ["73AEb-FS00MS1"]

    vs = V.load(sample("simos_RadBli_2d_block.dcm"))
    blk = {p.name: p for p in vs.params}["RadBli_idxBmAsgChg_C_VW"]
    assert (blk.kind, blk.nx, blk.ny, len(blk.z)) == ("block", 2, 2, 4)
    # group maps name their shared axes in *SSTX / *SSTY comments
    grp = [p for p in vs.params if p.x_kind == "com"]
    assert grp and all(p.x_ref for p in grp)
    axes = {p.name for p in vs.params if p.kind == "axis"}
    assert {p.x_ref for p in grp} <= axes

    vs = V.load(sample("scm900_42AEb_changed_labels.DCM"))
    by = {p.name: p for p in vs.params}
    # a group curve on a text axis, its values texts too
    tx = [p for p in vs.params if p.x and all(isinstance(v, str) for v in p.x)]
    assert tx
    assert any(isinstance(v, str) for p in vs.params for v in p.z if p.kind != "text")
    fix = [p for p in vs.params if p.kind == "curve" and p.x_kind == "fix"]
    assert len(fix) == 1
    m = next(p for p in vs.params if p.kind == "map")
    assert len(m.z) == m.nx * m.ny and len(m.y) == m.ny and len(m.x) == m.nx
    assert by  # names unique enough to index


def test_cdfx_shared_axes_and_maps():
    vs = V.load(sample("sc4800_49AEfF_changed_labels.CDFX"))
    by = {p.name: p for p in vs.params}
    assert vs.origin == {"a2l": "SC4800_A_OEM.a2l", "image": "FS_VWSC4_0D1_800.S19"}
    # a curve on a shared axis gets that axis' points
    c = by["id_tco_cor_inc"]
    assert c.x_kind == "com" and c.x_ref == "ldp_n_32_id_tco_cor_inc"
    assert c.x == by["ldp_n_32_id_tco_cor_inc"].z
    # maps: one VG per Y point, its LABEL that point
    m = by["id_maf_n_inh_mpi_inj_purge"]
    assert m.y[:3] == [500.0, 1000.0, 1500.0] and m.nx == 8
    assert len(m.z) == m.nx * m.ny


@pytest.mark.parametrize("target", ["dcm", "cdfx"])
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_value_formats_round_trip(name, target):
    """Read, write in either format, read back: the same values (a DCM has
    no form for a 3-D table, so those are left out and said so)."""
    vs = V.load(sample(name))
    back = V.load(V.dumps(vs, target))
    want = V.ValueSet(params=[p for p in vs.params
                              if not (target == "dcm" and p.kind == "cuboid")])
    assert V.differences(want, back, 1e-12) == []
    if target == "dcm" and any(p.kind == "cuboid" for p in vs.params):
        assert any("Three-dimensional" in w for w in vs.warnings)


def test_value_file_is_not_a_definition(tmp_path):
    p = tmp_path / "x.dcm"
    p.write_bytes(sample("simos_RadBli_2d_block.dcm"))
    with pytest.raises(C.ConvertError, match="holds calibration values"):
        C.run(C.Job(sources=[str(p)], target="xdf", out=str(tmp_path / "x.xdf")))


def test_convert_dcm_to_cdfx_job(tmp_path):
    src = tmp_path / "in.DCM"
    src.write_bytes(sample("scm900_42AEb_changed_labels.DCM"))
    res = C.run(C.Job(sources=[str(src)], target="cdfx", out=str(tmp_path / "o.cdfx")))
    assert res.check == [] and not res.lossy
    assert res.tables_in == res.tables_out > 400


# ------------------------------------------------------------------ a small A2L
MINI = r'''ASAP2_VERSION 1 60
/begin PROJECT P ""
 /begin MODULE M ""
  /begin MOD_COMMON "" BYTE_ORDER MSB_FIRST /end MOD_COMMON
  /begin COMPU_METHOD CM_LIN "" LINEAR "%6.2" "rpm" COEFFS_LINEAR 0.5 0 /end COMPU_METHOD
  /begin COMPU_METHOD CM_ID "" IDENTICAL "%d" "" /end COMPU_METHOD
  /begin COMPU_METHOD CM_ENUM "" TAB_VERB "%d" "" COMPU_TAB_REF VT_MODE /end COMPU_METHOD
  /begin COMPU_VTAB VT_MODE "" TAB_VERB 3 0 "OFF" 1 "LOW" 2 "HIGH" /end COMPU_VTAB
  /begin RECORD_LAYOUT RL_U16 FNC_VALUES 1 UWORD COLUMN_DIR DIRECT /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_U8 FNC_VALUES 1 UBYTE COLUMN_DIR DIRECT /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_CUR AXIS_PTS_X 1 UWORD INDEX_INCR DIRECT FNC_VALUES 2 UWORD COLUMN_DIR DIRECT /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_AX AXIS_PTS_X 1 UWORD INDEX_INCR DIRECT /end RECORD_LAYOUT
  /begin AXIS_PTS A_N "shared rpm axis" 0x80000300 NO_INPUT_QUANTITY RL_AX 0 CM_LIN 4 0 32767 /end AXIS_PTS
  /begin CHARACTERISTIC K_SPEED "speed limit" VALUE 0x80000200 RL_U16 0 CM_LIN 0 32767 /end CHARACTERISTIC
  /begin CHARACTERISTIC K_MODE "mode" VALUE 0x80000202 RL_U16 0 CM_ENUM 0 2 /end CHARACTERISTIC
  /begin CHARACTERISTIC K_BLK "block" VAL_BLK 0x80000204 RL_U16 0 CM_LIN 0 32767 NUMBER 3 /end CHARACTERISTIC
  /begin CHARACTERISTIC C_STD "own axis" CURVE 0x80000210 RL_CUR 0 CM_LIN 0 32767
   /begin AXIS_DESCR STD_AXIS N CM_LIN 4 0 32767 /end AXIS_DESCR
  /end CHARACTERISTIC
  /begin CHARACTERISTIC C_COM "shared axis" CURVE 0x80000240 RL_U16 0 CM_LIN 0 32767
   /begin AXIS_DESCR COM_AXIS N CM_LIN 4 0 32767 AXIS_PTS_REF A_N /end AXIS_DESCR
  /end CHARACTERISTIC
  /begin CHARACTERISTIC M_MAP "map" MAP 0x80000260 RL_U16 0 CM_LIN 0 32767
   /begin AXIS_DESCR COM_AXIS N CM_LIN 4 0 32767 AXIS_PTS_REF A_N /end AXIS_DESCR
   /begin AXIS_DESCR FIX_AXIS M CM_ENUM 3 0 2 FIX_AXIS_PAR_DIST 0 1 3 /end AXIS_DESCR
  /end CHARACTERISTIC
  /begin CHARACTERISTIC K_TXT "part number" ASCII 0x80000280 RL_U8 0 CM_ID 0 255 NUMBER 8 /end CHARACTERISTIC
 /end MODULE
/end PROJECT
'''
START = 0x80000100        # not a round address: only the S19 can say it


def _mini(tmp_path):
    """The A2L above and an S19 of 0x1F00 bytes at START holding values."""
    a2l = tmp_path / "mini.a2l"
    a2l.write_text(MINI)
    img = bytearray(0x1F00)

    def put(addr, *words):
        o = addr - START
        for w in words:
            img[o:o + 2] = w.to_bytes(2, "big")
            o += 2
    put(0x80000200, 2000)                      # K_SPEED 1000 rpm
    put(0x80000202, 1)                         # K_MODE LOW
    put(0x80000204, 2, 4, 6)                   # K_BLK 1 2 3
    put(0x80000210, 200, 400, 600, 800)        # C_STD: axis, then values
    put(0x80000218, 10, 20, 30, 40)
    put(0x80000240, 1, 2, 3, 4)                # C_COM
    put(0x80000260, *range(100, 112))          # M_MAP 4 x 3
    put(0x80000300, 1000, 2000, 3000, 4000)    # A_N 500..2000 rpm
    o = 0x80000280 - START
    img[o:o + 8] = b"03G906\0\0"
    s19 = tmp_path / "mini.s19"
    s19.write_bytes(I.write(I.Image([(START, bytes(img))], source="srec"), "srec"))
    return str(a2l), str(s19), bytes(img)


def test_s19_address_places_an_a2l(tmp_path):
    """An A2L in ECU addresses sits where the S19 says, not where a guess
    from its size would put it."""
    a2l, s19, _img = _mini(tmp_path)
    res = C.run(C.Job(sources=[a2l], binary=s19, target="dcm",
                      out=str(tmp_path / "v.dcm")))
    assert res.base == START
    vs = V.load(open(res.out[0], "rb").read())
    by = {p.name: p for p in vs.params}
    assert by["K_SPEED"].z == [1000.0]
    assert by["K_MODE"].z == ["LOW"]                       # verbal conversion
    assert by["K_BLK"].z == [1.0, 2.0, 3.0]
    assert by["C_STD"].x == [100.0, 200.0, 300.0, 400.0]
    assert by["C_COM"].x_ref == "A_N" and by["A_N"].z == [500.0, 1000.0, 1500.0, 2000.0]
    assert by["M_MAP"].y == ["OFF", "LOW", "HIGH"]         # enumeration axis
    assert by["K_TXT"].kind == "text" and by["K_TXT"].z == ["03G906"]
    # Y outer, X inner: the map is stored column-major (Y fastest)
    m = by["M_MAP"]
    assert m.z[:4] == [50.0, 51.5, 53.0, 54.5]


def test_apply_writes_values_into_the_s19(tmp_path):
    a2l, s19, img = _mini(tmp_path)
    dcm = tmp_path / "delta.dcm"
    dcm.write_text("\n".join([
        "KONSERVIERUNG_FORMAT 2.0",
        "FESTWERT K_SPEED", "   WERT 1250.5", "END",
        "FESTWERT K_MODE", '   TEXT "HIGH"', "END",
        "FESTWERT k_blk[1]", "   WERT 7", "END",              # an element, any case
        "STUETZSTELLENVERTEILUNG A_N 4", "   ST/X 600 1100 1600 2100", "END",
        "GRUPPENKENNLINIE C_COM 4", "*SSTX\tA_N", "   ST/X 600 1100 1600 2100",
        "   WERT 9 9 9 99999", "END",                          # last one too big
        "KENNLINIE C_STD 3", "   ST/X 1 2 3", "   WERT 1 2 3", "END",   # wrong size
        "TEXTSTRING K_TXT", '   TEXT "03G906016"', "END",
        "FESTWERT NOT_THERE", "   WERT 1", "END", ""]))
    out = tmp_path / "out.s19"
    res = C.run(C.Job(sources=[a2l, str(dcm)], binary=s19, target="srec", out=str(out)))
    assert res.check == []
    new = I.read(out.read_bytes())
    assert new.start == START
    data = new.flat()

    def word(addr):
        return int.from_bytes(data[addr - START:addr - START + 2], "big")
    assert word(0x80000200) == 2501                             # 1250.5 / 0.5
    assert word(0x80000202) == 2
    assert word(0x80000206) == 14
    assert [word(0x80000300 + 2 * i) for i in range(4)] == [1200, 2200, 3200, 4200]
    assert [word(0x80000240 + 2 * i) for i in range(4)] == [18, 18, 18, 65535]
    assert data[0x80000280 - START:0x80000288 - START] == b"03G90601"
    assert "NOT_THERE" in res.loss and "C_STD" in res.loss
    assert any("Limited" in v and "C_COM" in v for v in res.values)
    # nothing outside the tables changed
    changed = {i for i in range(len(img)) if img[i] != data[i]}
    assert changed and all(0x100 <= i < 0x210 or 0x13F <= i < 0x290 or 0x200 <= i < 0x208
                           for i in changed)


@pytest.mark.parametrize("fmt", ["dcm", "cdfx"])
def test_export_then_apply_restores_the_binary(tmp_path, fmt):
    """Values exported from 03G906016LQ through BPW.a2l, applied to a copy
    whose tables were all disturbed, give back the original bytes."""
    need(A2L, LQ)
    res = C.run(C.Job(sources=[A2L], binary=LQ, target=fmt,
                      out=str(tmp_path / ("lq." + fmt))))
    assert res.check == []
    defn, base, size, image, _loss, _n = C.prepare(C.Job(sources=[A2L], target="dcm",
                                                         binary=LQ), [])
    bad = bytearray(image)
    from ovtsuite.convert.entities import entities
    ents = entities(defn, 0, size, image, defn.byte_order(), "skip", {})
    for e in ents:
        if not e.text_kind:
            for a in range(e.addr, e.end):
                bad[a] ^= 0x5A
    broken = tmp_path / "broken.bin"
    broken.write_bytes(bytes(bad))
    out = tmp_path / "fixed.bin"
    res = C.run(C.Job(sources=[A2L, res.out[0]], binary=str(broken), target="bin",
                      out=str(out)))
    assert res.check == []
    assert out.read_bytes() == open(LQ, "rb").read()


# ------------------------------------------------------------------ the corpus
CORPUS = os.environ.get("OVT_DAMOS_CORPUS") or next(
    (p for p in (r"E:\HP Laptop Backup\VAG\Damos",
                 os.path.expanduser("~/mnt/Damos")) if os.path.isdir(p)), "")
SC4 = os.path.join(CORPUS, "SIMOS 22 EA890ROB (SC4) SC4800", "Daten_20l_49AEx")


def _sc4(ds):
    return os.path.join(SC4, "s19", "%s4800_A32_X547_Ac1__based_on_710_X546_Ma1.s19" % ds)


@pytest.mark.slow
@pytest.mark.parametrize("which", ["new", "changed"])
def test_sc4800_cdfx_matches_its_s19(which):
    """The supplier's CDFX for dataset 49AEfF against the S19 of 49AEfF,
    through SC4800_A_OEM.a2l: every parameter the A2L places agrees (the
    3-D tables and their axes are not read from A2L)."""
    a2l = os.path.join(SC4, "s19", "SC4800_A_OEM.a2l")
    cdfx = os.path.join(SC4, "dcm", "SC4800_%s_labels_49AEfF.CDFX" % which)
    if not all(os.path.isfile(p) for p in (a2l, cdfx, _sc4("49AEfF"))):
        pytest.skip("DAMOS corpus not available")
    defn, base, size, image, _l, _n = C.prepare(
        C.Job(sources=[a2l], target="cdfx", binary=_sc4("49AEfF")), [])
    assert base == 0x80500000
    exp = {p.name: p for p in V.export(defn, size, image).params}
    vs = V.load(open(cdfx, "rb").read())
    missing, wrong = [], []
    for p in vs.params:
        q = exp.get(p.name)
        if q is None:
            missing.append(p.name)
            continue
        if not (V._close(p.z, q.z, 1e-6) and (not p.x or V._close(p.x, q.x or [], 1e-6))
                and (not p.y or V._close(p.y, q.y or [], 1e-6))):
            wrong.append(p.name)
    assert wrong == []
    assert set(missing) <= {"ip_iga_st_h_prs", "ip_iga_st_l_prs", "ldpm_maf_ip_iga_st_st",
                            "ldpm_n_ip_iga_st_st", "ldpm_tco_ip_iga_st_st"}


@pytest.mark.slow
def test_sc4800_values_carry_one_dataset_onto_another(tmp_path):
    """Every value of 49AEbP, exported and applied onto 49AEfF, makes every
    table byte equal to 49AEbP's and touches nothing else."""
    a2l = os.path.join(SC4, "s19", "SC4800_A_OEM.a2l")
    if not all(os.path.isfile(p) for p in (a2l, _sc4("49AEfF"), _sc4("49AEbP"))):
        pytest.skip("DAMOS corpus not available")
    res = C.run(C.Job(sources=[a2l], binary=_sc4("49AEbP"), target="cdfx",
                      out=str(tmp_path / "bp.cdfx")))
    out = tmp_path / "fF_as_bP.s19"
    res = C.run(C.Job(sources=[a2l, res.out[0]], binary=_sc4("49AEfF"), target="srec",
                      out=str(out)))
    assert res.check == [] and not res.loss
    src = I.read(open(_sc4("49AEbP"), "rb").read()).flat()
    dst = I.read(open(_sc4("49AEfF"), "rb").read()).flat()
    got = I.read(out.read_bytes()).flat()
    from ovtsuite.convert.entities import entities
    defn, _b, size, _i, _l, _n = C.prepare(C.Job(sources=[a2l], target="dcm",
                                                 binary=_sc4("49AEbP")), [])
    cover = bytearray(size)
    for e in entities(defn, 0, size, src, defn.byte_order(), "skip", {}):
        cover[e.addr:e.end] = b"\1" * (e.end - e.addr)
        for ax in (e.x, e.y):
            if ax is not None and ax.addr is not None:
                n = ax.count * max(1, ax.bits // 8)
                cover[ax.addr:ax.addr + n] = b"\1" * n
    assert all(got[i] == (src[i] if cover[i] else dst[i]) for i in range(size))
