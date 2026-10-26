# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS map packs (``.kp``) written by the converter, laid out as the
format-292 packs WinOLS 5.0 wrote and the format-503 packs WinOLS 4.x wrote.

Each layout checker below is run on the genuine packs first (so every
invariant it asserts is one those files hold), then on a genuine pack
converted back to a pack (record by record against the original), and on
the 038906019GQ EDC15 project converted to one.

Run alone: ``python -m pytest tests/test_convert_kp.py -q``.
"""

import io
import os
import struct
import zipfile
import zlib
from collections import Counter

import pytest

from ovtsuite import convert as C
from ovtsuite.definitions import winols
from ovtsuite.definitions import winols_write as W

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
try:                                    # reference files outside the repository
    from _private_refs import WINOLS_SAMPLES
except ImportError:                     # (not published: that case skips)
    WINOLS_SAMPLES = ""
GENUINE = [
    os.path.join(REF, "A2L", "golf5_bxe.kp"),
    os.path.join(REF, "Binaries", "ZF5HP24", "Definitions",
                 "4B0927156FB_RS6_5HP24.kp"),
    # outside the repository: used when present
    os.path.join(WINOLS_SAMPLES, "Audi_A4_MAPPAK_360654.kp"),
]
GQ = os.path.join(REF, "EDC15", "038906019GQ.ols")

#: Bytes of the header's settings block that differ between the genuine
#: packs (window and font settings / leftover memory).
_HEAD_FREE = range(0x30, 0xB0)


def need(path):
    if not os.path.isfile(path):
        pytest.skip("reference file missing: %s" % path)


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


# ---------------------------------------------------------------- the walker
class _Rd:
    def __init__(self, d, p=0):
        self.d, self.p = d, p

    def u8(self):
        self.p += 1
        return self.d[self.p - 1]

    def u32(self):
        self.p += 4
        return struct.unpack_from("<I", self.d, self.p - 4)[0]

    def i32(self):
        self.p += 4
        return struct.unpack_from("<i", self.d, self.p - 4)[0]

    def f64(self):
        self.p += 8
        return struct.unpack_from("<d", self.d, self.p - 8)[0]

    def raw(self, n):
        assert self.p + n <= len(self.d), "past the end at 0x%X" % self.p
        self.p += n
        return self.d[self.p - n:self.p]

    def str(self):
        n = self.i32()
        t = self.raw(n).decode("latin-1")
        if n:                                   # format 292: NUL after text
            assert self.u8() == 0, "unterminated string at 0x%X" % self.p
        return t


def _axis(r):
    a = dict(label=r.str(), units=r.str(), factor=r.f64(), offset=r.f64())
    for k in ("source", "addr", "org", "size", "base"):
        a[k] = r.u32()
    a["pre"] = r.raw(14)
    a["prec"], a["signed"] = r.i32(), r.u8()
    a["data"] = r.raw(r.u32())
    a["end"] = (r.i32(), r.i32())
    return a


def _record(r):
    x = {"b0": r.u8(), "marker": r.i32(), "bittexts": r.str(), "b1": r.u8(),
         "desc": r.str()}
    for k in ("typ", "h1", "org", "size", "base", "folder"):
        x[k] = r.u32()
    x["name"] = r.str()
    x["pre"] = r.raw(61)
    for k in ("cols", "rows", "c2", "c3", "prec"):
        x[k] = r.u32()
    x["label"], x["units"] = r.str(), r.str()
    x["factor"], x["offset"] = r.f64(), r.f64()
    x["addr"], x["end"] = r.u32(), r.u32()
    x["ztail"] = struct.unpack("<8I", r.raw(32))
    x["X"], x["Y"] = _axis(r), _axis(r)
    x["tail"] = r.raw(77)
    x["flags"] = r.raw(3)
    return x


def layout(d: bytes) -> dict:
    """Every section of a format-292 map pack, checked as it is read: the
    invariants all genuine samples hold."""
    out = {}
    r = _Rd(d)
    assert r.raw(16) == winols.FILE_MAGIC
    assert r.u32() == 292
    assert r.u32() == len(d), "file size field"
    assert [r.str() for _ in range(16)] == [""] * 16, "vehicle / ECU"
    assert r.raw(8) == b"\0" * 8, "times: 0 in a map pack"
    out["filename"] = r.str()
    assert out["filename"].endswith(".kp")
    assert r.str() == W.WRITER
    assert r.str() == ""
    assert r.str() == "1"
    head = r.raw(555)
    out["head"] = head
    assert all(head[i] == W._HEAD[i] for i in range(555)
               if i not in _HEAD_FREE and not 0xB0 <= i < 0xB4), \
        "settings block"
    out["head_count"] = struct.unpack_from("<I", head, 0xB0)[0]
    assert r.u32() == len(d) - 4, "offset of the image marker"
    assert r.u32() == 0, "one version's image size: none"
    vstart = r.p
    assert r.raw(4) == winols.VERSIONS_MAGIC
    assert r.u32() == 2
    for name in ("Original", "Version 1"):
        assert r.str() == name and r.str() == ""
        assert r.raw(16) == winols.VERSION_SIG + b"\0" * 12
    out["versions"] = d[vstart:r.p]
    assert r.raw(5) == winols.RECORDS_MAGIC + b"\x01"
    zlen = r.u32()
    z = r.raw(zlen)
    # the archive as WinOLS writes it: one entry, deflated (flag 2: best
    # compression), no time stamp, the archive attribute
    assert z[:4] == b"PK\x03\x04"
    ver, flag, meth, tm, dt = struct.unpack_from("<HHHHH", z, 4)
    assert (ver, flag, meth, tm, dt) == (20, 2, 8, 0, 0)
    assert z[26:28] == b"\x06\0" and z[28:30] == b"\0\0" and z[30:36] == b"intern"
    cd = z.rfind(b"PK\x01\x02")
    made, need_, flag2, meth2 = struct.unpack_from("<HHHH", z, cd + 4)
    assert (made, need_, flag2, meth2) == (20, 20, 2, 8)
    assert struct.unpack_from("<I", z, cd + 38)[0] == 0x20
    assert z[-22:-18] == b"PK\x05\x06" and z[-2:] == b"\0\0"
    zf = zipfile.ZipFile(io.BytesIO(z))
    assert zf.namelist() == ["intern"]
    intern = zf.read("intern")
    ri = _Rd(intern)
    assert ri.u8() == 0
    n = ri.u32()
    recs = [_record(ri) for _ in range(n)]
    assert ri.p == len(intern), "the records fill the archive exactly"
    assert out["head_count"] == n, "the header names the map count"
    out["records"] = recs
    for x in recs:
        assert x["marker"] == -1 and x["flags"][0] == 1
        assert 1 <= x["typ"] <= 5
    # folders
    assert r.raw(12) == winols.FOLDERS_MAGIC + b"\0" * 4 + winols.FOLDERS_MAGIC
    folders = []
    for _ in range(r.u32()):
        fid, res, name = r.u32(), r.u32(), r.str()
        assert (r.u8(), r.u32(), r.u8()) == (1, 1, 1)
        assert r.raw(15) == b"\0" * 15
        folders.append((fid, res, name))
    assert folders[:2] == [(1, 9001, "Hexdump"), (0, 9000, "My maps")]
    names = [f[2] for f in folders[2:]]
    assert names == sorted(names, key=str.lower), "listed by name"
    assert all(f[1] == 0 and f[0] >= 2 for f in folders[2:])
    ids = {f[0] for f in folders}
    assert {x["folder"] for x in recs} <= ids
    out["folders"] = folders
    tail = d[r.p:]
    assert len(tail) == 8298 and tail.endswith(W.IMAGES_MAGIC)
    assert all(tail[i] == b for i, b in enumerate(W._TAIL_BLOCK) if b), \
        "the closing block's markers and settings"
    out["tail"] = tail
    return out


def _ours(x):
    """The per-record invariants of a record this app wrote."""
    pre = bytearray(W._PRE)
    pre[-3] = x["pre"][-3]
    assert x["pre"] == bytes(pre)
    assert x["h1"] == 2 and x["base"] == 10 and x["desc"]
    n = x["cols"] * x["rows"]
    assert x["end"] == x["addr"] + n * x["size"]
    assert x["ztail"][3] == x["addr"] and x["ztail"][0] == x["ztail"][6]
    assert struct.unpack_from("<II", x["tail"], 6) == (x["cols"], x["rows"])
    assert struct.unpack_from("<dd", x["tail"], 41) == (
        (0.0, 0.01) if x["typ"] == 3 else (300.0, 1.0))
    for k in "XY":
        a = x[k]
        if a["source"] == 0:
            assert (a["label"], a["units"], a["org"], a["size"], a["prec"],
                    a["addr"], a["data"]) == ("-", "-", 1, 1, 0, 0, b"")
        assert a["pre"] == b"\0" * 14 and a["end"] == (0, -1)


# --------------------------------------------------------------------- tests
@pytest.mark.parametrize("path", GENUINE)
def test_genuine_packs_hold_the_layout(path):
    """The checker's invariants come from the genuine packs."""
    need(path)
    layout(open(path, "rb").read())


def test_the_block_before_the_counts_is_the_samples_commonest():
    """Written as most genuine records hold it: masks FF / FFFF / FFFFFFFF,
    no range (byte 18 clear). golf5 and RS6 give a range in nearly every
    record; the A4 pack (2350 records) does not in most."""
    for p in GENUINE:
        need(p)
    recs = [x for p in GENUINE for x in layout(open(p, "rb").read())["records"]]
    plain = sum(1 for x in recs if x["pre"][:58] == bytes(W._PRE[:58])
                and x["pre"][59:] == b"\0\0")
    assert plain > len(recs) // 2


@pytest.mark.parametrize("path", GENUINE)
def test_a_genuine_pack_converts_back_to_its_layout(tmp_path, path):
    need(path)
    src = open(path, "rb").read()
    out = tmp_path / "back.kp"
    res = C.run(C.Job(sources=[path], target="kp", out=str(out)))
    assert res.check == []
    a, b = layout(src), layout(out.read_bytes())
    assert b["versions"] == a["versions"]
    assert all(b["head"][i] == a["head"][i] for i in range(555)
               if i not in _HEAD_FREE)
    assert b["tail"] == W._TAIL_BLOCK
    # the folders that hold maps, in the same order
    # a map pack passed through: the same folders in the same order, and
    # every record as it was, byte for byte
    assert b["folders"] == a["folders"]
    assert b["records"] == a["records"]


def _intern(d: bytes) -> bytes:
    """The records of a format-292 file as stored: a map pack's archive,
    or the list of a project (hexdump view and all)."""
    h = winols.read_header(d)
    p = d.find(winols.RECORDS_MAGIC, h.end) + 5
    n = struct.unpack_from("<I", d, p)[0]
    if d[p + 4:p + 8] == b"PK\x03\x04":
        return zipfile.ZipFile(io.BytesIO(d[p + 4:p + 4 + n])).read("intern")[5:]
    return d[p + 4:]


@pytest.fixture(scope="module")
def gq(tmp_path_factory):
    need(GQ)
    out = tmp_path_factory.mktemp("kp") / "gq.kp"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OVT_CONVERTER_DIR", str(out.parent / "cfg"))
        res = C.run(C.Job(sources=[GQ], target="kp", out=str(out)))
    return res, out.read_bytes()


def test_the_edc15_project_passes_through(gq):
    """The 038906019GQ project (an EDC15 with three code blocks, WinOLS
    folders ARF_01 ... ARF_17): every map record, in its own folder, the
    folder list as the project has it -- not regrouped by code block."""
    res, data = gq
    src = winols.parse(open(GQ, "rb").read())
    assert res.check == [] and res.tables_out == 17292 == len(src.records) - 1
    lay = layout(data)
    assert lay["head_count"] == 17292
    # the project's folders (Potential maps holds no record: not listed,
    # as in every format-292 pack WinOLS wrote)
    assert lay["folders"] == [f for f in src.folder_list if f[1] != 9002]
    assert not any(f[2].startswith("Codeblock") for f in lay["folders"])
    assert [x["folder"] for x in lay["records"]] == \
        [r.folder for r in src.records if r.type != 1]
    assert Counter(x["folder"] for x in lay["records"])[0] == 16917
    # the records as the project stores them (it is of format 292 too),
    # but the hexdump view, which a map pack holds none of
    wf = winols.parse(data)
    assert not wf.problems and len(wf.records) == 17292
    hexdump = src.records[0]
    assert hexdump.type == 1
    rest = _intern(open(GQ, "rb").read())
    w = W._W()
    W._passthrough_record(w, hexdump, 0x80000)
    assert _intern(data) == rest[len(w.out):len(w.out) + len(_intern(data))]


def _fields(r):
    return (r.type, r.folder, r.name, r.org, r.size, r.cols, r.rows, r.addr,
            r.factor, r.offset, r.signed, r.precision,
            r.x.source, r.x.addr, r.x.org, r.x.factor, r.x.data,
            r.y.source, r.y.addr, r.y.org, r.y.factor, r.y.data)


def _texts(r):
    return (r.description, r.label, r.units, r.x.label, r.x.units,
            r.y.label, r.y.units)


def test_translation_changes_only_texts(gq, tmp_path):
    """Translated, the same records, folders and order: descriptions,
    labels, units and folder names change; names (identifiers) do not."""
    out = tmp_path / "en.kp"
    res = C.run(C.Job(sources=[GQ], target="kp", out=str(out),
                      translate={"backend": "builtin", "source": "de",
                                 "target": "en", "mode": "replace"}))
    assert res.check == []
    a = winols.parse(gq[1])
    b = winols.parse(out.read_bytes())
    assert [_fields(r) for r in a.records] == [_fields(r) for r in b.records]
    changed = sum(_texts(x) != _texts(y) for x, y in zip(a.records, b.records))
    assert changed > len(a.records) // 2
    assert [f[:2] for f in a.folder_list] == [f[:2] for f in b.folder_list]
    names = dict((f, n) for f, _r, n in b.folder_list)
    assert names[0] == "My maps" and names[1] == "Hexdump"
    assert names[11] != "ARF_01: Struktur der Abgasrueckfuehrung"
    assert "ARF_01" in names[11]


def test_reviewed_translations_are_used(tmp_path):
    """Rows reviewed in the window (planned on the converted definition)
    are what goes in; a description joined with its value label in the
    definition translates both parts."""
    from ovtsuite.convert import kp_passthrough as P
    got = P._reviewed([
        {"field": "description", "kind": "text", "source": "A | Z: B",
         "result": "a | Z: b"},
        {"field": "category", "kind": "category", "source": "Ordner",
         "result": "Folder"},
        {"field": "units", "kind": "units", "source": "Grad", "result": "Grad"}])
    assert got == {("text", "A | Z: B"): "a | Z: b", ("text", "A"): "a",
                   ("text", "B"): "b", ("category", "Ordner"): "Folder"}
    need(GENUINE[0])
    wf = winols.parse(open(GENUINE[0], "rb").read())
    desc = wf.records[0].description
    out = tmp_path / "t.kp"
    C.run(C.Job(sources=[GENUINE[0]], target="kp", out=str(out),
                translate={"mode": "replace", "rows": [
                    {"table": desc, "field": "name", "kind": "text",
                     "source": desc, "result": "Renamed map"},
                    {"table": "", "field": "category", "kind": "category",
                     "source": "turbo boost", "result": "Boost"}]}))
    back = winols.parse(out.read_bytes())
    assert sum(r.description == "Renamed map" for r in back.records) == \
        sum(r.description == desc for r in wf.records)
    assert (3, 0, "Boost") in back.folder_list
    assert [_fields(r) for r in back.records] == [_fields(r) for r in wf.records]


def test_a_503_pack_passes_through_to_292(tmp_path):
    """The 038906012FD pack (format 503, its maps in 23 folders): the same
    records with the same fields and texts, and its folder list, in a
    format-292 pack."""
    path = os.path.join(REF, "EDC15", "EDC15V_362774_038906012FD_0281010653.kp")
    need(path)
    out = tmp_path / "fd.kp"
    res = C.run(C.Job(sources=[path], target="kp", out=str(out)))
    assert res.check == []
    a = winols.parse(open(path, "rb").read())
    data = out.read_bytes()
    layout(data)
    b = winols.parse(data)
    assert len(b.records) == len(a.records) == 17373
    assert [_fields(r) + _texts(r) for r in a.records] == \
        [_fields(r) + _texts(r) for r in b.records]
    assert b.folder_list == [f for f in a.folder_list if f[1] != 9002]


def test_other_sources_keep_the_built_folders(tmp_path):
    """Only a WinOLS source passes through: an XDF still goes in by its
    categories."""
    xdf = tmp_path / "s.xdf"
    xdf.write_text(
        '<XDFFORMAT version="1.80"><XDFHEADER><deftitle>t</deftitle>'
        '<REGION type="0xFFFFFFFF" startaddress="0x0" size="0x1000" />'
        '<CATEGORY index="0x0" name="Boost" /></XDFHEADER>'
        '<XDFCONSTANT uniqueid="0x1"><title>K1</title>'
        '<CATEGORYMEM index="0" category="1" />'
        '<EMBEDDEDDATA mmedaddress="0x10" mmedelementsizebits="16" />'
        '<MATH equation="X"><VAR id="X" /></MATH></XDFCONSTANT></XDFFORMAT>')
    out = tmp_path / "s.kp"
    C.run(C.Job(sources=[str(xdf)], target="kp", out=str(out), size=0x1000))
    wf = winols.parse(out.read_bytes())
    assert [n for _f, _r, n in wf.folder_list] == ["Hexdump", "My maps", "Boost"]


def test_a2l_groups_of_a_project_name_its_folders(tmp_path):
    """An A2L of a project with code blocks groups each map by block and
    folder, as the XDF does -- not by the block alone."""
    need(GQ)
    out = tmp_path / "gq.a2l"
    C.run(C.Job(sources=[GQ], target="a2l", out=str(out), self_check=False))
    text = out.read_text(encoding="latin-1")
    assert '"Codeblock 0 / ARF_01: Struktur der Abgasrueckfuehrung"' in text
    assert '"Codeblock 2 / My maps"' in text


def test_curves_carry_the_curve_view_setting():
    """The tail's two f64 at 41 and 49 are 0 / 0.01 in a curve's record and
    300 / 1 in a map's or value's (the first is the 3D view's angle, which
    the user may have turned): 145 of the 146 curves of golf5 and the RS6
    pack, and every record of the 038906012FD pack (format 503)."""
    for p in GENUINE[:2]:
        need(p)
    recs = [x for p in GENUINE[:2] for x in layout(open(p, "rb").read())["records"]]
    curves = [x for x in recs if x["typ"] == 3]
    assert len(curves) == 146
    assert sum(1 for x in curves if struct.unpack_from("<dd", x["tail"], 41)
               == (0.0, 0.01)) == 145


# ------------------------------------------------------------- format 503
#: Genuine format-503 packs WinOLS 4.x wrote: 045906019K (102 maps, German),
#: a 038906012FD pack (17373 maps, English; used when present) with the
#: binary it was made on.
G503 = [
    os.path.join(REF, "EDC15", "045906019K_354603.kp"),
    os.path.join(REF, "EDC15", "EDC15V_362774_038906012FD_0281010653.kp"),
]
FD_BIN = os.path.join(REF, "Binaries", "EDC15", "038906012FD.bin")


class _Rd5(_Rd):
    def str(self):
        n = self.i32()
        return None if n < 0 else self.raw(n).decode("latin-1")

    def ml(self):
        t = self.str()
        assert self.raw(8) == b"\0" * 8
        return t, [(self.str(), self.u32()) for _ in range(self.u32())]


def _axis5(r):
    a = dict(label=r.ml(), units=r.str(), factor=r.f64(), offset=r.f64())
    for k in ("source", "addr", "org", "size", "base"):
        a[k] = r.u32()
    a["pre"] = r.raw(14)
    a["prec"], a["signed"] = r.i32(), r.u8()
    a["data"] = r.raw(r.u32())
    a["end"] = (r.i32(), r.i32())
    a["id"] = r.str()
    a["texts"] = [r.ml() for _ in range(r.u32())]
    a["extra"] = r.raw(4)
    return a


def _record5(r):
    x = {"b0": r.u8(), "marker": r.i32(), "bittexts": r.ml(), "b1": r.u8(),
         "desc": r.ml()}
    for k in ("typ", "h1", "org", "size", "base", "folder"):
        x[k] = r.u32()
    x["name"] = r.str()
    x["pre"] = r.raw(117)
    for k in ("cols", "rows", "c2", "c3", "prec"):
        x[k] = r.u32()
    x["label"], x["units"] = r.str(), r.str()
    x["factor"], x["offset"] = r.f64(), r.f64()
    x["addr"], x["end"] = r.u32(), r.u32()
    x["ztail"] = struct.unpack("<8I", r.raw(32))
    x["X"], x["Y"] = _axis5(r), _axis5(r)
    x["tail"] = r.raw(77)
    x["flags"] = r.raw(3)
    x["tail2"] = r.raw(64)
    return x


def _image_parts(x):
    """Where the record's bytes in the data after the image marker come
    from: its values, then the axes stored in the image."""
    out = [(x["addr"], x["cols"] * x["rows"] * x["size"])]
    for k, n in (("X", x["cols"]), ("Y", x["rows"])):
        if x[k]["source"] in (1, 3):
            out.append((x[k]["addr"], n * x[k]["size"]))
    return out


def layout503(d: bytes) -> dict:
    """Every section of a format-503 map pack, checked as it is read: the
    invariants both genuine samples hold."""
    out = {}
    r = _Rd5(d)
    assert r.raw(16) == winols.FILE_MAGIC
    assert r.u32() == 503
    assert r.u32() == len(d), "file size field"
    assert [r.str() for _ in range(16)] == [""] * 16, "vehicle / ECU"
    assert r.raw(8) == b"\0" * 8, "times: 0 in a map pack"
    out["filename"] = r.str()
    assert out["filename"].endswith(".kp")
    assert (r.str(), r.str(), r.str()) == (W.WRITER, "", "1")
    vat = d.find(winols.VERSIONS_MAGIC, r.p)
    head = d[r.p:vat - 8]
    out["head"] = head
    marker, nimage = struct.unpack_from("<II", d, vat - 8)
    assert d[marker:marker + 4] == W.IMAGES_MAGIC
    assert marker + 4 + nimage == len(d), "the map data fills the file"
    image = d[marker + 4:]
    out["image"] = image
    # the header's checks of the map data: CRC-32 (inverted) and byte sum
    crc, = struct.unpack_from("<I", head, W._HEAD503_CRC_AT)
    assert crc == zlib.crc32(image) ^ 0xFFFFFFFF
    assert struct.unpack_from("<I", head, W._HEAD503_SUM_AT)[0] == \
        sum(image) & 0xFFFF
    out["head_count"] = struct.unpack_from("<I", head, W._HEAD503_COUNT_AT)[0]
    r.p = vat
    assert r.raw(4) == winols.VERSIONS_MAGIC and r.u32() == 2
    for name in ("Original", "Version 1"):
        assert r.str() == name and r.str() == ""
        assert r.raw(4) == winols.VERSION_SIG
        r.raw(W._VERSION503_REST)               # times (may be set)
    out["versions"] = d[vat:r.p]
    assert r.raw(5) == winols.RECORDS_MAGIC + b"\x01"
    z = r.raw(r.u32())
    intern = zipfile.ZipFile(io.BytesIO(z)).read("intern")
    ri = _Rd5(intern)
    assert ri.u8() == 0
    n = ri.u32()
    recs = [_record5(ri) for _ in range(n)]
    assert ri.p == len(intern), "the records fill the archive exactly"
    assert out["head_count"] == n, "the header names the map count"
    for x in recs:
        assert x["marker"] == -1 and x["flags"] == b"\x01\x01\x01"
        assert x["tail2"][:4] == b"\xff\xff\xff\xff"
        assert x["end"] == x["addr"] + x["cols"] * x["rows"] * x["size"]
    out["records"] = recs
    if image:                                   # (none without a binary)
        assert sum(n for x in recs for _a, n in _image_parts(x)) == len(image)
    assert r.raw(12) == winols.FOLDERS_MAGIC + b"\0" * 4 + winols.FOLDERS_MAGIC
    folders = []
    for _ in range(r.u32()):
        fid, res, name = r.u32(), r.u32(), r.ml()
        assert name[1] == []
        b, nv = r.u8(), r.u32()
        assert nv == 1
        vb = r.u8()
        assert r.raw(25) == W._FOLDER_TAIL503
        folders.append((fid, res, name[0], b, vb))
    assert [f[:2] for f in folders[:2]] == [(1, 9001), (0, 9000)]
    assert (2, 9002) in [f[:2] for f in folders], "Potential maps"
    names = [f[2] for f in folders[2:]]
    assert names == sorted(names, key=str.lower), "listed by name"
    assert {x["folder"] for x in recs} <= {f[0] for f in folders}
    out["folders"] = folders
    out["closing"] = d[r.p:marker]
    return out


def _need503(path):
    need(path)
    return open(path, "rb").read()


@pytest.mark.parametrize("path", G503)
def test_genuine_503_packs_hold_the_layout(path):
    d = _need503(path)
    lay = layout503(d)
    assert W.template_closing(d) == lay["closing"]
    # the settings block is WinOLS' default but for the checks and the
    # count (the 019K pack's in German)
    head = bytearray(lay["head"])
    for at in (W._HEAD503_CRC_AT, W._HEAD503_SUM_AT, W._HEAD503_COUNT_AT):
        head[at:at + 4] = b"\0" * 4
    head = bytes(head)
    for de, en in (("PKW", "Passenger car"), ("Motor", "Engine"),
                   ("Komplette Bin\xe4rdatei", "Complete binary file"),
                   ("German", "English")):
        a, b = (struct.pack("<I", len(t)) + t.encode("latin-1")
                for t in (de, en))
        head = head.replace(a, b)
    assert head == bytes(W._HEAD503)


def _image_from_pack(lay, size=0x80000) -> bytes:
    """The binary a pack's map data came from, as far as the pack holds it."""
    img = bytearray(size)
    p = 0
    for x in lay["records"]:
        for addr, n in _image_parts(x):
            img[addr:addr + n] = lay["image"][p:p + n]
            p += n
    return bytes(img)


def _convert503(d: bytes, image) -> bytes:
    from ovtsuite.convert.entities import entities
    defn = winols.load(d, "back")
    loss = {}
    ents = entities(defn, 0, 0x80000, image, None, "max", loss)
    assert not loss
    warn = []
    out = W.write_kp(ents, 0x80000, "back", defn.byte_order() == "<",
                     warnings=warn, tiers=False, version=503, template=d)
    assert warn == []
    return out


def _same_structure(a, b, free=()):
    """``b`` (written here) holds ``a``'s (genuine) maps record by record:
    every count, size, address, block and setting; the texts may differ,
    and the axis fields named in ``free``."""
    assert len(a["records"]) == len(b["records"])
    for x, y in zip(a["records"], b["records"]):
        assert y["h1"] == 2 and y["base"] == 10
        for k in ("typ", "org", "size", "cols", "rows", "addr", "end",
                  "flags", "b0", "b1", "marker"):
            assert x[k] == y[k], (x["desc"], k)
        # the tail but the 3D view's angle and scale (free: the user turns
        # the view; WinOLS' default is written)
        assert (x["tail"][:41], x["tail"][57:]) ==             (y["tail"][:41], y["tail"][57:]), (x["desc"], "tail")
        assert y["tail2"] == W._TAIL503
        assert y["factor"] == pytest.approx(x["factor"], rel=1e-9, abs=1e-12)
        assert y["offset"] == pytest.approx(x["offset"], rel=1e-9, abs=1e-9)
        assert y["pre"][-3] == x["pre"][-3], "signed"
        assert y["ztail"][:3] == x["ztail"][:3]
        for k in "XY":
            u, v = x[k], y[k]
            for f in ("source", "addr", "size", "data", "end", "id", "texts",
                      "extra"):
                # an absent axis' size is a leftover (16 bit in some of 019K)
                if f not in free and (u["source"] or f != "size"):
                    assert u[f] == v[f], (x["desc"], k, f)
            if u["source"] and u["org"]:
                assert (u["org"], u["base"], u["pre"]) == \
                    (v["org"], v["base"], v["pre"]), (x["desc"], k)


def test_a_genuine_503_pack_converts_back_to_its_layout():
    """045906019K back to format 503 with itself as the template: every
    section but the records' texts comes out as WinOLS wrote it."""
    d = _need503(G503[0])
    a = layout503(d)
    out = _convert503(d, _image_from_pack(a))
    b = layout503(out)
    assert b["closing"] == a["closing"]
    assert b["versions"] == a["versions"]       # its times are 0
    assert b["image"] == a["image"], "the map data, in the same order"
    # an axis' two i32 after its values: 0 / -1 as in the 038906012FD pack
    # (here often 4 and an address: what WinOLS last showed, it seems)
    _same_structure(a, b, free=("end",))
    # the header: the genuine one (in English), with this data's checks
    head = bytearray(W._HEAD503)
    struct.pack_into("<I", head, W._HEAD503_CRC_AT,
                     zlib.crc32(b["image"]) ^ 0xFFFFFFFF)
    struct.pack_into("<I", head, W._HEAD503_SUM_AT, sum(b["image"]) & 0xFFFF)
    struct.pack_into("<I", head, W._HEAD503_COUNT_AT, len(b["records"]))
    assert b["head"] == bytes(head)
    assert b["head"][W._HEAD503_CRC_AT:W._HEAD503_CRC_AT + 4] == \
        a["head"][W._HEAD503_CRC_AT:W._HEAD503_CRC_AT + 4]
    # and the app reads it back
    wf = winols.parse(out)
    assert not wf.problems and len(wf.records) == len(a["records"])


def test_the_038906012fd_pack_converts_back_to_its_layout():
    """The user's WinOLS pack of 038906012FD (17373 maps), converted back
    with its binary: header, map data and closing block byte for byte."""
    d = _need503(G503[1])
    need(FD_BIN)
    a = layout503(d)
    b = layout503(_convert503(d, open(FD_BIN, "rb").read()))
    assert b["head"] == a["head"], "settings, the data's checks, the count"
    assert b["image"] == a["image"]
    assert b["closing"] == a["closing"]
    _same_structure(a, b)
    assert [x["tail"] for x in a["records"]] == [x["tail"] for x in b["records"]]


def test_format_503_needs_a_genuine_503_template():
    need(GENUINE[0])
    with pytest.raises(ValueError):
        W.write_kp([], 0x1000, version=503)
    with pytest.raises(ValueError):
        W.template_closing(open(GENUINE[0], "rb").read())   # format 292
    with pytest.raises(ValueError):
        W.write_kp([], 0x1000, version=400)


def test_the_reader_reads_a_written_503_pack():
    d = _need503(G503[0])
    from ovtsuite.convert.entities import entities
    ents = entities(winols.load(d, "t"), 0, 0x80000, None, None, "max", {})
    out = W.write_kp(ents, 0x80000, "t", True, tiers=False, version=503,
                     template=d)
    lay = layout503(out)
    assert lay["image"] == b""                  # no binary: no map data
    wf = winols.parse(out)
    assert not wf.problems and len(wf.records) == 102
