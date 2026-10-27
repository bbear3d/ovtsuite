# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Third-party definitions in the base app (ovtsuite.definitions): WinOLS
projects and map packs, ASAP2, DAMOS, and the generic entity builder.

The WinOLS files here are written by a small serializer that follows the
record layout documented in ovtsuite/definitions/winols.py, once per format
version, so every layout is checked against known values. Real-file tests at
the end are skipped when the reference material is absent.
"""

import io
import os
import struct
import zipfile

import pytest

from ovtsuite import definitions, xdf_session
from ovtsuite.binary import BinaryFile
from ovtsuite.definitions import a2l, dam, winols

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")


# =================================================================== WinOLS
class _W:
    """Writes WinOLS fields the way format ``ver`` does."""

    def __init__(self, ver):
        self.ver = ver
        self.nul = ver < 400
        self.out = bytearray()

    def u8(self, v):
        self.out += bytes([v])

    def u32(self, v):
        self.out += struct.pack("<I", v & 0xFFFFFFFF)

    def f64(self, v):
        self.out += struct.pack("<d", v)

    def raw(self, b):
        self.out += b

    def str(self, t):
        if t is None:
            self.u32(-1)
            return
        b = t.encode("latin-1")
        self.u32(len(b))
        self.out += b
        if self.nul and b:
            self.out += b"\0"

    def mlstr(self, t):
        self.str(t)
        if self.ver >= 397:
            self.raw(b"\0" * 8)
            self.u32(0)


def _axis(w, label="", units="", factor=1.0, offset=0.0, source=0, addr=0,
          org=2, size=2, signed=False, data=b""):
    w.mlstr(label)
    w.str(units)
    w.f64(factor)
    w.f64(offset)
    for v in (source, addr, org, size, 10):
        w.u32(v)
    w.raw(b"\0" * (14 if w.ver >= 200 else 2))
    w.u32(2)                                # precision
    w.u8(1 if signed else 0)
    w.u32(len(data))
    w.raw(data)
    w.u32(0)
    w.u32(-1)
    if w.ver >= 397:
        w.str("")                           # axis id
        w.u32(0)                            # point texts
        if w.ver >= 479:
            w.raw(b"\0" * 4)


def _record(w, typ, name, desc, org, size, folder, cols, rows, addr, factor,
            offset=0.0, signed=False, units="", x=None, y=None):
    w.u8(0)
    if w.ver >= 200:
        w.u32(-1)
        w.mlstr("")
        w.u8(0)
    w.mlstr(desc)
    for v in (typ, 2, org, size, 10, folder):
        w.u32(v)
    w.str(name)
    precx = 117 if w.ver >= 397 else (61 if w.ver >= 200 else 57)
    pre = bytearray(precx)
    pre[-3] = 1 if signed else 0
    w.raw(bytes(pre))
    for v in (cols, rows, 0, 0, 3):
        w.u32(v)
    w.str("")                               # label
    w.str(units)
    w.f64(factor)
    w.f64(offset)
    w.u32(addr)
    w.u32(addr + cols * rows * size)
    w.raw(b"\0" * (32 if w.ver >= 200 else 24))
    _axis(w, **(x or {}))
    _axis(w, **(y or {}))
    w.raw(b"\0" * 77)
    w.raw(b"\x01\x01\x01")
    if w.ver >= 397:
        w.u32(-1)
        w.raw(b"\0" * {597: 64, 503: 60, 479: 48, 397: 36}[w.ver])


def _maps(w):
    """Three maps: a signed 16-bit value, a curve with its axis in the
    image, a map with an axis in the image and one given by the record."""
    _record(w, 2, "VAL", "a value", 2, 2, 5, 1, 1, 0x10, 0.5, signed=True,
            units="Nm")
    _record(w, 3, "CUR", "a curve", 3, 2, 5, 4, 1, 0x28, 0.1,
            x=dict(label="rpm", units="1/min", source=1, addr=0x20, org=3,
                   size=2, factor=10.0))
    fixed = b"".join(struct.pack(">f", v) for v in (0.0, 1.0, 2.0))
    _record(w, 4, "", "map without a name", 1, 1, 6, 2, 3, 0x40, 1.0,
            offset=-40.0,
            x=dict(source=1, addr=0x30, org=2, size=2, factor=1.0),
            y=dict(source=4, org=6, size=4, data=fixed, label="gear"))
    return 3


def _image():
    img = bytearray(0x100)
    struct.pack_into(">h", img, 0x10, -6)                       # VAL
    struct.pack_into("<4H", img, 0x20, 100, 200, 300, 400)      # CUR axis
    struct.pack_into("<4H", img, 0x28, 1, 2, 3, 4)              # CUR
    struct.pack_into(">2H", img, 0x30, 5, 6)                    # map X
    img[0x40:0x46] = bytes([40, 41, 42, 43, 44, 45])            # map Z
    return bytes(img)


def _header(w, size_field=True):
    w.raw(winols.FILE_MAGIC)
    w.u32(w.ver)
    if w.ver >= 200:
        w.u32(0)                            # file size, patched later
    for t in ("Audi", "A4", "1.8T", "2002", "", "", "150PS", "Manual",
              "Eprom", "Bosch", "ME7.5", "8E0906018", "", "360654", "", ""):
        w.str(t)
    w.raw(b"\0" * 8)
    w.str("test.ols")
    w.str("OLS 5.0 (Windows)")
    w.str("")
    w.str("0 (Original)" if w.ver >= 200 else "0")


def _folders(w):
    w.raw(winols.FOLDERS_MAGIC + b"\0\0\0\0" + winols.FOLDERS_MAGIC)
    w.u32(2)
    for fid, name in ((5, "Torque"), (6, "Temperature")):
        w.u32(fid)
        w.u32(0)
        if w.ver >= 479:
            w.mlstr(name)
        else:
            w.str(name)
        w.u8(1)
        w.u32(1)
        w.u8(1)
        w.raw(b"\0" * {114: 0, 117: 0, 292: 15, 397: 25, 479: 25, 503: 25, 597: 25}[w.ver])


def make_ols(ver, image=None, versions=None):
    """``versions``: [(name, image)] -- a project embedding several binaries
    (the version list before the map list, every image whole at the end)."""
    w = _W(ver)
    _header(w)
    w.raw(b"\0" * 64)                       # header fields not read
    if versions:
        w.u32(len(versions[0][1]))
        w.raw(winols.VERSIONS_MAGIC)
        w.u32(len(versions))
        for name, _img in versions:
            for t in (name, ""):
                w.u32(len(t))
                w.raw(t.encode())
            w.raw(winols.VERSION_SIG + b"\0" * 12)
        w.raw(b"\0" * 16)
    w.raw(winols.RECORDS_MAGIC)
    if ver >= 200:
        w.u8(0)
    count_at = len(w.out)
    w.u32(0)
    n = _maps(w)
    struct.pack_into("<I", w.out, count_at, n)
    _folders(w)
    w.raw(b"\0" * 32)
    if versions:
        w.raw(winols.IMAGE_SIG + b"".join(img for _n, img in versions))
    else:
        w.raw(winols.IMAGE_SIG + (image if image is not None else _image()))
    if ver >= 200:
        struct.pack_into("<I", w.out, 20, len(w.out))
    return bytes(w.out)


def make_kp(ver):
    w = _W(ver)
    _header(w)
    rec = _W(ver)
    rec.u8(0)
    rec.u32(0)
    n = _maps(rec)
    struct.pack_into("<I", rec.out, 1, n)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("intern", bytes(rec.out))
    w.raw(winols.RECORDS_MAGIC + b"\0")
    w.u32(len(z.getvalue()))
    w.raw(z.getvalue())
    _folders(w)
    return bytes(w.out)


VERSIONS = (117, 292, 397, 479, 503, 597)


@pytest.mark.parametrize("ver", VERSIONS)
def test_every_winols_layout_reads_field_by_field(ver):
    wf = winols.parse(make_ols(ver))
    assert wf.kind == "ols" and wf.declared == 3 and not wf.problems
    assert len(wf.records) == 3
    assert wf.folders == {5: "Torque", 6: "Temperature"}
    assert wf.image == _image()
    h = wf.header
    assert h.vehicle["make"] == "Audi" and h.ecu["build"] == "ME7.5"
    if ver >= 200:
        assert h.current_version == "Original"
    v, c, m = wf.records
    assert (v.type, v.name, v.signed, v.addr, v.factor, v.units) == \
        (2, "VAL", True, 0x10, 0.5, "Nm")
    assert c.x.source == 1 and c.x.addr == 0x20 and c.x.org == 3
    assert m.name == "" and m.description == "map without a name"
    assert m.y.source == 4 and len(m.y.data) == 12


@pytest.mark.parametrize("ver", VERSIONS)
def test_winols_maps_become_entities_with_the_right_values(ver):
    defn = winols.load(make_ols(ver))
    by = {t.name: t for t in defn.tables}
    assert set(by) == {"VAL", "CUR", "map without a name"}
    assert by["VAL"].kind == "constant" and by["VAL"].categories == ["Torque"]
    m = by["map without a name"]
    assert (m.kind, m.rows, m.cols, m.order) == ("map", 3, 2, "row")
    assert m.y.fixed == [0.0, 1.0, 2.0] and m.y.name == "gear"
    bf = BinaryFile(_image(), endian=defn.byte_order())
    ents = {e.name: e for e in xdf_session.entities(defn, bf)}
    assert ents["VAL"].cell(0, 0) == -3.0                  # signed, x 0.5
    # the curve is little-endian, the value big-endian: the session reads
    # one byte order, and a table in the other is left out (and counted)
    m = ents["map without a name"]
    assert m.values() == [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]]
    assert m.y_values() == [0.0, 1.0, 2.0]
    assert m.x_values() == [5.0, 6.0]


def test_a_winols_map_pack_is_a_definition_only():
    data = make_kp(597)
    wf = winols.parse(data)
    assert wf.kind == "kp" and len(wf.records) == 3 and not wf.image
    assert wf.folders[6] == "Temperature"
    assert definitions.detect(data) == "kp"


def test_an_ols_5_map_pack_of_format_503_reads_whole():
    """A WinOLS 5.0 map pack (format 503, the Lupo 3L 045906019K pack): its
    record tail is 60 bytes -- between 479's 48 and 597's 64 -- and every
    one of its 102 maps reads, in order, to the end of the list."""
    p = os.path.join(REF, "EDC15", "045906019K_354603.kp")
    if not os.path.exists(p):
        pytest.skip("045906019K_354603.kp not present")
    data = open(p, "rb").read()
    assert winols.format_version(data) == 503
    wf = winols.parse(data)
    assert wf.kind == "kp" and not wf.problems and len(wf.records) == wf.declared == 102
    first = wf.records[0]
    assert first.description == "driver wish (gas)"
    assert (first.cols, first.rows, first.addr) == (8, 12, 0x4C8F6)
    assert len(winols.to_definition(wf).tables) == 102


def test_an_ols_5_project_of_format_250_reads_whole():
    """A WinOLS 5.0 project (format 250, the RS6 5HP24 gearbox def): its
    records carry no i32 -1 prefix (like format 117) but use the 292-era
    field sizes, with ztail 24 and axpre 6. All 163 records -- the leading
    whole-image hexdump plus 162 maps/curves -- read in order to the folder
    section, and the embedded image comes back whole."""
    p = os.path.join(REF, "Binaries", "ZF5HP24", "Definitions",
                     "4B0927156FB_RS6_5HP24.ols")
    if not os.path.exists(p):
        pytest.skip("4B0927156FB_RS6_5HP24.ols not present")
    data = open(p, "rb").read()
    assert winols.format_version(data) == 250
    wf = winols.parse(data)
    assert wf.kind == "ols" and not wf.problems
    assert len(wf.records) == wf.declared == 163
    assert len(wf.image) == 0x80000
    assert wf.folders.get(1) == "Hexdump"
    maps = [r for r in wf.records if r.type in (3, 4, 5)]
    assert len(maps) == 162
    first = maps[0]
    assert (first.type, first.cols, first.rows, first.addr) == (4, 8, 8, 0x63CF4)
    assert first.x.source in (1, 3) and first.x.addr == 0x63CE4
    assert len(winols.to_definition(wf).tables) == 162


def test_a_little_endian_curve_reads_with_its_axis():
    defn = winols.load(make_ols(597))
    defn.tables = [t for t in defn.tables if t.name == "CUR"]
    bf = BinaryFile(_image(), endian=defn.byte_order())
    (e,) = xdf_session.entities(defn, bf)
    assert e.kind == "curve" and e.x_values() == [1000.0, 2000.0, 3000.0, 4000.0]
    assert [round(v, 6) for v in e.values()[0]] == [0.1, 0.2, 0.3, 0.4]


def test_a_broken_record_list_is_reported_not_guessed():
    data = bytearray(make_ols(597))
    p = data.find(b"\x01\x01\x01", data.find(winols.RECORDS_MAGIC))
    data[p] = 7                             # first record's tail marker
    wf = winols.parse(bytes(data))
    assert wf.problems and len(wf.records) < 3


# ============================================================== projects
def test_an_ols_imports_as_a_project_with_its_definition_and_details():
    from ovtsuite.project import Project, importers
    p = Project("")
    s = importers.import_bytes(p, make_ols(597), "car.ols")
    m = s.members[0]
    assert p.member_bytes(s.id, m.id) == _image()
    assert m.definition["type"] == "ols"
    assert s.checkpoints[0].label == "WinOLS: Original"
    assert p.info["make"] == "Audi" and p.info["model"] == "A4"
    assert p.info["ecu"] == "Bosch ME7.5"
    assert "360654" in p.info["notes"]


def _three_versions():
    a = _image()
    b = bytearray(a)
    b[0x40] ^= 0xFF
    c = bytearray(b)
    c[0x41] ^= 0xFF
    return [("Original", a), ("Stage 1", bytes(b)), ("Stage 2", bytes(c))]


def _ols_with_versions():
    return make_ols(597, versions=_three_versions())


def test_an_ols_with_several_binaries_lists_them():
    from ovtsuite.project import importers
    got = importers.ols_versions(_ols_with_versions())
    assert [(n, size) for n, _c, size in got] == [
        (n, len(i)) for n, i in _three_versions()]
    assert importers.ols_versions(make_ols(597)) == [("Original", "", len(_image()))]


def test_an_ols_imports_its_original_only_by_default():
    from ovtsuite.project import Project, importers
    p = Project("")
    s = importers.import_bytes(p, _ols_with_versions(), "car.ols")
    assert len(s.checkpoints) == 1
    assert p.member_bytes(s.id, s.members[0].id) == _three_versions()[0][1]


def test_an_ols_can_import_every_binary_as_versions():
    from ovtsuite.project import Project, importers
    p = Project("")
    s = importers.import_bytes(p, _ols_with_versions(), "car.ols",
                               ols_versions="all")
    want = _three_versions()
    assert [c.label for c in s.checkpoints] == [
        "WinOLS: Original", "WinOLS: Stage 1", "WinOLS: Stage 2"]
    assert s.checkpoints[0].parent is None
    assert s.checkpoints[1].parent == s.checkpoints[0].id
    assert s.checkpoints[2].parent == s.checkpoints[1].id
    mid = s.members[0].id
    for c, (_n, img) in zip(s.checkpoints, want):
        assert p.state(s.id, c.id)[mid] == img
    assert p.member_bytes(s.id, mid) == want[2][1]      # the newest is the head
    assert len(s.branches) == 1


def test_an_ols_can_import_one_chosen_binary():
    from ovtsuite.project import Project, importers
    p = Project("")
    s = importers.import_bytes(p, _ols_with_versions(), "car.ols",
                               ols_versions=[1])
    assert len(s.checkpoints) == 1
    assert s.checkpoints[0].label == "WinOLS: Stage 1"
    assert p.member_bytes(s.id, s.members[0].id) == _three_versions()[1][1]
    with pytest.raises(importers.ImportRefused, match="not one of them"):
        importers.import_bytes(Project(""), _ols_with_versions(), "car.ols",
                               ols_versions=[7])


def test_a_definition_alone_is_refused_with_what_to_do():
    from ovtsuite.project import Project, importers
    with pytest.raises(importers.ImportRefused, match="attach it"):
        importers.import_bytes(Project(""), make_kp(479), "maps.kp")
    with pytest.raises(importers.ImportRefused, match="A2L"):
        importers.import_bytes(Project(""), A2L.encode(), "car.a2l")


def test_a_binary_beside_its_a2l_imports_with_it(tmp_path):
    from ovtsuite.project import importers
    (tmp_path / "car.bin").write_bytes(_a2l_image())
    (tmp_path / "car.a2l").write_text(A2L)
    proj, s = importers.import_file(str(tmp_path / "car.bin"))
    assert s.members[0].definition["type"] == "a2l"


# ================================================================== ASAP2
A2L = r'''
ASAP2_VERSION 1 51
/* a comment with /begin inside */
/begin PROJECT P "test"
 /begin MODULE M "the module"
  /begin MOD_COMMON "" BYTE_ORDER MSB_LAST ALIGNMENT_WORD 2 /end MOD_COMMON
  /begin COMPU_METHOD CM_rpm "" LINEAR "%5.0" "1/min" COEFFS_LINEAR 10 0 /end COMPU_METHOD
  /begin COMPU_METHOD CM_q "" RAT_FUNC "%6.2" "mg" COEFFS 0 100 0 0 0 1 /end COMPU_METHOD
  /begin COMPU_METHOD CM_onoff "" TAB_VERB "%1" "" COMPU_TAB_REF VT /end COMPU_METHOD
  /begin COMPU_VTAB VT "" TAB_VERB 2 0 "off" 1 "on" /end COMPU_VTAB
  /begin RECORD_LAYOUT RL_map
    NO_AXIS_PTS_X 1 UBYTE
    NO_AXIS_PTS_Y 2 UBYTE
    AXIS_PTS_X 3 UWORD INDEX_INCR DIRECT
    AXIS_PTS_Y 4 UWORD INDEX_INCR DIRECT
    FNC_VALUES 5 SWORD COLUMN_DIR DIRECT
  /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_val FNC_VALUES 1 UBYTE ROW_DIR DIRECT /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_cur FNC_VALUES 1 UWORD ROW_DIR DIRECT /end RECORD_LAYOUT
  /begin RECORD_LAYOUT RL_ax NO_AXIS_PTS_X 1 UWORD AXIS_PTS_X 2 UWORD INDEX_INCR DIRECT /end RECORD_LAYOUT
  /begin AXIS_PTS AX_shared "a shared axis" 0x1060 NO_INPUT_QUANTITY RL_ax 0 CM_rpm 3 0 9000 /end AXIS_PTS
  /begin CHARACTERISTIC KF_q "a map" MAP 0x1000 RL_map 0 CM_q -300 300
    /begin AXIS_DESCR STD_AXIS N CM_rpm 4 0 9000 /end AXIS_DESCR
    /begin AXIS_DESCR STD_AXIS Q CM_q 3 0 300 /end AXIS_DESCR
  /end CHARACTERISTIC
  /begin CHARACTERISTIC C_on "a switch" VALUE 0x1050 RL_val 0 CM_onoff 0 1 /end CHARACTERISTIC
  /begin CHARACTERISTIC KL_c "a curve on a shared axis" CURVE 0x1070 RL_cur 0 CM_q 0 300
    /begin AXIS_DESCR COM_AXIS N CM_rpm 3 0 9000 AXIS_PTS_REF AX_shared /end AXIS_DESCR
  /end CHARACTERISTIC
  /begin FUNCTION FN_q "fuel quantity" /begin DEF_CHARACTERISTIC KF_q KL_c /end DEF_CHARACTERISTIC /end FUNCTION
 /end MODULE
/end PROJECT
'''


def _a2l_image():
    img = bytearray(0x2000)
    # KF_q at 0x1000: the image stores 3 x 2 points (the description allows
    # up to 4 x 3), packed by the actual counts
    struct.pack_into("<BB", img, 0x1000, 3, 2)
    struct.pack_into("<3H", img, 0x1002, 100, 200, 300)            # X (x10)
    struct.pack_into("<2H", img, 0x1008, 1000, 2000)               # Y (/100)
    struct.pack_into("<6h", img, 0x100C, 100, 200, 300, 400, 500, 600)
    img[0x1050] = 1                                                 # C_on
    struct.pack_into("<4H", img, 0x1060, 3, 50, 60, 70)            # AX_shared
    struct.pack_into("<3H", img, 0x1070, 150, 250, 350)            # KL_c
    return bytes(img)


def test_a2l_layouts_follow_the_record_layout_and_the_image_counts():
    defn = a2l.load(A2L.encode())
    assert not defn.warnings
    assert a2l.resolve_base(defn, 0x2000) == 0
    bf = BinaryFile(_a2l_image(), endian=defn.byte_order())
    assert defn.byte_order() == "<"
    ents = {e.name: e for e in xdf_session.entities(defn, bf)}
    m = ents["KF_q"]
    assert (m.rows, m.cols, m.order) == (2, 3, "col")
    assert m.x_values() == [1000.0, 2000.0, 3000.0]
    assert m.y_values() == [10.0, 20.0]
    # COLUMN_DIR: one column after the other (Y varies fastest)
    assert m.values() == [[1.0, 3.0, 5.0], [2.0, 4.0, 6.0]]
    assert m.categories == ["FN_q: fuel quantity"]
    on = ents["C_on"]
    assert on.cell(0, 0) == 1 and "1 = on" in on.description
    c = ents["KL_c"]
    assert c.x_values() == [500.0, 600.0, 700.0]
    assert c.values() == [[1.5, 2.5, 3.5]]


def test_a2l_tokeniser_keeps_quoted_text_and_drops_comments():
    tree = a2l.parse_tree('/begin X "a /* not a comment */ b" // gone\n y /end X')
    (b,) = tree.blocks("X")
    assert b.args() == ["a /* not a comment */ b", "y"]


def test_resolve_base_moves_ecu_addresses_onto_the_image():
    defn = definitions.Definition(type="a2l", tables=[
        definitions.TableDef(name="t%d" % i, kind="constant",
                             z=definitions.Storage(addr=0x80010000 + 16 * i))
        for i in range(50)])
    defn.segments = [(0x80000000, 0x100000)]
    assert a2l.resolve_base(defn, 0x100000) == 0x80000000


# ================================================================== DAMOS
# Laid out as a real EDC15 DAMOS is: a header up to the first ";", records
# ending in ";" (also inside {} is not an end), long records wrapped.
DAM = """/EPR, P127.VFE.VA03, TEST.DAM:, DAMPAR_03
/PRO,{EDC15P+}
/SRC,3
/UP,{S80166}
/ABL,2
/RFG,2
/PBA, $EC000h
/SND,CODE1  $80000 $BFFFF
/SND,DATA1  $F0000 $FFFFF
/SND,DATA2  $EC000 $EFFFF
/SND,DSPTAB 12;
1,/SPZ,QMAX,{max quantity},1
$0100,$0100
/SPW,1,-2,  0., 100.
/SPX,0,0,  0.,  0.,0
/SPY,0,0,  0.,  0.,0
/FKX,0,0,0
/FKY,0,0,0
/ABL,0;
2,/SPZ,TMP,{Ausschalttemperatur},1
$0102,$0102
/SPW,3,2,  0., 100.
/SPX,0,0,  0.,  0.,0
/SPY,0,0,  0.,  0.,0
/FKX,0,0,0
/FKY,0,0,0
/ABL,0;
3,/SPZ,FRQ,{PWM frequency},1
$0104,$0104
/SPW,4,2,  0., 7812.5
/SPX,0,0,  0.,  0.,0
/SPY,0,0,  0.,  0.,0
/FKX,0,0,0
/FKY,0,0,0
/ABL,0;
4,/SPZ,QKL,{quantity curve},2
$0010,$0200
/SPW,1,-2,  0., 100.
/SPX,2,-2,  0., 6000.,1
/SPY,0,0,  0.,  0.,0
/FKX,8,0,0
/FKY,0,0,0
/ABL,2;
5,/SPZ,QKF,{quantity map},3
$0012,$0300
/SPW,1,-2,  0., 100.
/SPX,2,-2,  0., 6000.,1
/SPY,1,-2,  0., 100.,2
/FKX,8,0,0
/FKY,8,0,0
/ABL,2;
6,/SPZ,AXN,{points for nmit},6
$0014,$0400
/SPW,0,0,  0.,  0.
/SPX,2,-2,  0., 6000.,1
/SPY,0,0,  0.,  0.,0
/FKX,8,0,0
/FKY,0,0,0
/ABL,2;
7,/SPZ,AXQ,{points for quantity},6
$0016,$0420
/SPW,0,0,  0.,  0.
/SPX,1,-2,  0., 100.,2
/SPY,0,0,  0.,  0.,0
/FKX,8,0,0
/FKY,0,0,0
/ABL,2;
8,/SPZ,GKF,{group map},8
$0018,$0500
/SPW,1,-2,  0., 100.
/SPX,2,-2,  0., 6000.,6
/SPY,1,-2,  0., 100.,7
/FKX,8,0,0
/FKY,8,0,0
/ABL,0;
9,/SPZ,HWNR,{hardware number},12
$0600,$0600
/SPW,0,5,  0.,  0.
/SPX,0,0,  0.,  0.,0
/SPY,0,0,  0.,  0.,0
/FKX,0,0,0
/FKY,0,0,0
/ABL,0;
1,/REG,MENGE,{Umrechnung Menge},6,0,{mg/Hub},3,7,  0., 327.67
/REP, 1.,  0.,  0., 0.01,  0.,  0.;
2,/REG,DREHZAHL,{Umrechnung Drehzahl},6,0,{1/min},5,7,-32768.
 32767.
/REP, 1.,  0.,  0., 1.,  0.,  0.;
3,/REG,T,{Umrechnung Temperatur},6,0,{Grad C},3,7,-150.04, 799.96
/REP, 1., 273.14,  0.,0.1,  0.,  0.;
4,/REG,ASIC_F,{Frequenz; PWM-Register},6,0,{Hz},4,7, 0.999936, 7812.5
/REP,  0., 7812.5, 1.,  0.,-1.,  0.;
1,/SRC,dzmNmit,{Drehzahl},$F94A,-2,2,2,2;
2,/SRC,mrmM_E,{Menge},$C000,-2,1,2,3;
/FKT,MENGE,{Menge; Begrenzung}
2,QMAX,QKL
1,mrmM_E;
"""

# the image: a 512 KB EDC15 image covers 0x80000-0xFFFFF, so a DAMOS
# address (relative to the program base 0xEC000) is at offset + 0x6C000
_D = 0xEC000 - 0x80000


def _dam_image():
    img = bytearray(0x80000)
    struct.pack_into("<hHH", img, _D + 0x100, 4000, 2932, 30)
    struct.pack_into("<HH3h3h", img, _D + 0x200, 0xF94A, 3,
                     1000, 2000, 3000, 100, 200, 300)
    # map: X (2 points), Y (3 points), values with Y varying fastest
    struct.pack_into("<HH2hHH3h6h", img, _D + 0x300, 0xF94A, 2, 1000, 2000,
                     0xC000, 3, 100, 200, 300, 1, 2, 3, 4, 5, 6)
    struct.pack_into("<HH4h", img, _D + 0x400, 0xF94A, 4, 500, 1000, 1500, 2000)
    struct.pack_into("<HH2h", img, _D + 0x420, 0xC000, 2, 1000, 2000)
    struct.pack_into("<8h", img, _D + 0x500, *range(10, 90, 10))
    img[_D + 0x600:_D + 0x605] = b"0281A"
    return BinaryFile(bytes(img), endian="<")


def test_damos_reads_every_kind_of_parameter():
    defn = dam.load(DAM.encode())
    assert defn.base_of(0x80000) == 0x80000
    assert defn.byte_order() == "<"
    ents = {e.name: e for e in xdf_session.entities(defn, _dam_image())}
    assert set(ents) == {"QMAX", "TMP", "FRQ", "QKL", "QKF", "AXN", "AXQ",
                         "GKF", "HWNR"}
    assert ents["QMAX"].cell(0, 0) == 40.0
    assert ents["QMAX"].units == "mg/stroke"                 # English units
    # a description the phrase table knows is shown in English
    assert ents["TMP"].description == \
        "switch-off temperature | DE: Ausschalttemperatur"
    assert ents["TMP"].units == "deg C"
    assert abs(ents["TMP"].cell(0, 0) - 20.06) < 1e-9       # T: raw*0.1-273.14
    # a reciprocal conversion: 7812.5 / (raw + 1), and back
    frq = ents["FRQ"]
    assert abs(frq.cell(0, 0) - 7812.5 / 31) < 1e-9
    assert round(frq.eq.encode(7812.5 / 31)) == 30
    c = ents["QKL"]
    assert c.addr == _D + 0x20A and c.cols == 3
    assert c.x_values() == [1000.0, 2000.0, 3000.0]
    assert c.values() == [[1.0, 2.0, 3.0]]
    assert c.categories == ["MENGE: Menge; Begrenzung"]
    assert "X: engine speed (dzmNmit)" in c.description
    # stored X-outer: shown as WinOLS shows it, the fast axis across
    m = ents["QKF"]
    assert (m.rows, m.cols) == (2, 3)
    assert m.x_values() == [1.0, 2.0, 3.0] and m.y_values() == [1000.0, 2000.0]
    assert m.values() == [[0.01, 0.02, 0.03], [0.04, 0.05, 0.06]]
    assert ents["AXN"].values() == [[500.0, 1000.0, 1500.0, 2000.0]]
    # a group map: values only, its axes the shared ones
    g = ents["GKF"]
    assert g.addr == _D + 0x500 and (g.rows, g.cols) == (4, 2)
    assert g.y_values() == [500.0, 1000.0, 1500.0, 2000.0]
    assert g.x_values() == [10.0, 20.0]
    assert g.values()[0] == [0.1, 0.2]
    assert ents["HWNR"].text_kind == "ascii" and ents["HWNR"].cols == 5


def test_damos_without_segments_finds_its_place_in_the_image():
    # an older DAMOS (EDC15P-3.1) states no memory segments: where its
    # addresses land is found from the image -- each map block starts with
    # its input's RAM address and its point count
    text = "\n".join(l for l in DAM.splitlines() if not l.startswith("/SND,")
                     ).replace("/PBA, $EC000h", "/PBA, $38000h")
    text = text.replace("/PBA, $38000h", "/PBA, $38000h\n/SND,DSPTAB 12;")
    defn = dam.load(text.encode())
    src = _dam_image().data
    img = bytearray(0x80000)
    img[0x38000:0x38800] = src[_D:_D + 0x800]      # image starts at address 0
    bf = BinaryFile(bytes(img), endian="<")
    assert defn.base_in(bf.data) == 0
    ents = {e.name: e for e in xdf_session.entities(defn, bf)}
    assert ents["QKL"].x_values() == [1000.0, 2000.0, 3000.0]
    assert ents["QMAX"].cell(0, 0) == 40.0
    # nothing fits an unrelated image: no place is guessed
    assert defn.locate(bytes(0x80000)) is None


def test_german_units_and_descriptions_read_in_english():
    from ovtsuite.signal_names import english_units, translate_german
    assert english_units("mg/Hub Luft") == "mg/stroke air"
    assert english_units("Grad KW") == "deg CrS"
    assert english_units("%/(Grad C*s)") == "%/(deg C*s)"
    assert english_units("1/min") == "1/min"
    # whole Bosch descriptions come from the phrase table, spacing ignored
    assert translate_german("ADF Kanal          Atmosphaerendruckfuehler") \
        .startswith("ADF channel")


def test_damos_records_split_on_semicolons_outside_braces_only():
    recs = dam._records('/UP,{C167}\n/PBA,$0;\n1,/SRC,a,{x; y},$10,2;\n')
    assert recs[1] == [["", "1"], ["SRC", "a", "{x; y}", "$10", "2"]]
    assert recs[0][0] == ["UP", "{C167}"]


# ================================================================ dispatch
def test_detect_is_by_content():
    assert definitions.detect(make_ols(597)) == "ols"
    assert definitions.detect(A2L.encode()) == "a2l"
    assert definitions.detect(DAM.encode()) == "dam"
    assert definitions.detect(b'<?xml version="1.0"?><XDFFORMAT version="1.60">') == "xdf"
    assert definitions.detect(bytes(4096)) is None


# =========================================================== reference files
def _ref(*parts):
    p = os.path.join(REF, *parts)
    if not os.path.exists(p):
        pytest.skip("reference material absent: %s" % os.path.join(*parts))
    return p


def test_reference_winols_project_parses_completely():
    import glob
    paths = glob.glob(os.path.join(REF, "ZF8HP", "A2L", "*.ols"))
    if not paths:
        pytest.skip("no WinOLS project in Reference/ZF8HP/A2L")
    wf = winols.parse(open(paths[0], "rb").read())
    assert not wf.problems and len(wf.records) == wf.declared > 1000
    assert len(wf.image) == 0x200000


def test_reference_a2l_reads_without_warnings():
    p = _ref("A2L", "038997016Q_4999_502333_P350_U8A9_EDC16U31_4.47.a2l")
    defn = a2l.load(open(p, "rb").read())
    assert len(defn.tables) > 5000 and not defn.warnings


def test_the_import_dialog_offers_the_versions_only_when_there_are_several(tmp_path):
    pytest.importorskip("PyQt6")
    from ovtsuite import gui_olsversions as ov
    one, many = tmp_path / "one.ols", tmp_path / "many.ols"
    one.write_bytes(make_ols(597))
    many.write_bytes(_ols_with_versions())
    assert ov.choose(None, str(one)) is None            # nothing to ask
    assert ov.choose(None, str(tmp_path / "x.bin")) is None
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = ov.OlsVersionsDialog(None, "many.ols", [("A", "", 1), ("B", "", 1)])
    assert dlg.value() is None                          # the original by default
    dlg.all.setChecked(True)
    assert dlg.value() == "all"
    dlg.one.setChecked(True)
    dlg.combo.setCurrentIndex(1)
    assert dlg.value() == [1]
