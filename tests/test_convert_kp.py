# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS map packs (``.kp``) written by the converter, laid out as the
format-292 packs WinOLS 5.0 wrote.

The layout checker below is run on the genuine packs first (so every
invariant it asserts is one those files hold), then on a genuine pack
converted back to a pack, and on the 038906019GQ EDC15 project converted
to one.

Run alone: ``python -m pytest tests/test_convert_kp.py -q``.
"""

import io
import os
import struct
import zipfile

import pytest

from ovtsuite import convert as C
from ovtsuite.definitions import winols
from ovtsuite.definitions import winols_write as W

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
GENUINE = [
    os.path.join(REF, "A2L", "golf5_bxe.kp"),
    os.path.join(REF, "Binaries", "ZF5HP24", "Definitions",
                 "4B0927156FB_RS6_5HP24.kp"),
    # outside the repository: used when present
    r"C:\Claude\WinOLS samples\Tests\OLS-tests\Audi_A4_MAPPAK_360654.kp",
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
    used = {x["folder"] for x in a["records"]}
    want = [f[2].strip() for f in a["folders"][2:] if f[0] in used]
    assert [f[2] for f in b["folders"][2:]] == want
    assert len(b["records"]) == len(a["records"])
    for x, y in zip(a["records"], b["records"]):
        _ours(y)
        # the type follows the shape as the app reads it: a map of type 5
        # goes out as type 4 (both store X fastest), one of a single row or
        # a "value" of several cells as a curve (type 3)
        assert y["typ"] in (2, 3, 4), x["desc"]
        for k in ("org", "size", "cols", "rows", "addr"):
            assert x[k] == y[k], (x["desc"], k)
        assert y["factor"] == pytest.approx(x["factor"], rel=1e-9, abs=1e-12)
        assert y["offset"] == pytest.approx(x["offset"], rel=1e-9, abs=1e-9)
        for k in "XY":
            # the axes the written type has (a value has none)
            has = y["typ"] == 4 or (y["typ"] == 3 and k == "X")
            if has and x[k]["source"] in (1, 3):
                assert y[k]["source"] == 1
                for f in ("addr", "org", "size"):
                    assert x[k][f] == y[k][f], (x["desc"], k, f)


@pytest.fixture(scope="module")
def gq(tmp_path_factory):
    need(GQ)
    out = tmp_path_factory.mktemp("kp") / "gq.kp"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OVT_CONVERTER_DIR", str(out.parent / "cfg"))
        res = C.run(C.Job(sources=[GQ], target="kp", out=str(out)))
    return res, out.read_bytes()


def test_the_edc15_project_converts_to_the_layout(gq):
    res, data = gq
    assert res.check == [] and res.tables_out == 17292
    lay = layout(data)
    assert lay["head_count"] == 17292
    assert [f[2] for f in lay["folders"][2:]] == [
        "Codeblock 0", "Codeblock 1", "Codeblock 2"]
    for x in lay["records"]:
        _ours(x)
        assert x["org"] in (1, 3)               # EDC15: 8 bit, 16 bit LE
        assert x["ztail"][0] == 0x80000         # the 512 KB image
    # and the app reads it back
    wf = winols.parse(data)
    assert not wf.problems and len(wf.records) == 17292
