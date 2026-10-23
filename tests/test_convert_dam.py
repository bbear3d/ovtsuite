# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""DAMOS export against the Bosch-generated DAMOS files in Reference/.

The only verifier besides OVT-Suite's own reader is the Bosch files
themselves: written back unchanged they must come out byte for byte, and a
DAMOS written from a WinOLS project of the same software must describe the
same tables as Bosch's own.
"""

import collections
import glob
import os
import re

import pytest

from ovtsuite import convert as C
from ovtsuite.convert.entities import entities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
INC = os.path.join(REF, "_incoming", "2026-10-14")
CORPUS = sorted(set(glob.glob(os.path.join(REF, "EDC15", "*.[dD][aA][mM]")))
                | set(glob.glob(os.path.join(INC, "F4*_EDC15P.DAM"))))


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


@pytest.mark.skipif(not CORPUS, reason="no Bosch DAMOS reference files")
@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_bosch_damos_re_emits_byte_for_byte(tmp_path, path):
    out = tmp_path / "re.dam"
    res = C.run(C.Job(sources=[path], target="dam", out=str(out)))
    assert out.read_bytes() == open(path, "rb").read()
    assert res.check == []


@pytest.mark.skipif(not CORPUS, reason="no Bosch DAMOS reference files")
@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_corpus_rules(path):
    """The rules the writer follows, as measured: /ABL 2 exactly for kinds
    2, 3 and 6; $pointer == $address exactly for kinds 1 and 12; header
    counts equal record counts. (Line ends vary: some files are CRLF.)"""
    t = open(path, "rb").read().decode("latin-1")
    recs = re.findall(r"\n\d+,/SPZ,[^\r\n]*\},(-?\d+)\r?\n\$([0-9A-F]+),\$([0-9A-F]+)\r?\n(.*?);",
                      t, re.S)
    assert recs
    for kind, ptr, addr, body in recs:
        abl = re.search(r"/ABL,(\d+)", body).group(1)
        assert (abl == "2") == (kind in ("2", "3", "6"))
        assert (ptr == addr) == (kind in ("1", "12"))
    spc = int(re.search(r"/SPC,(\d+)", t).group(1))
    assert spc == len(re.findall(r"\n\d+,/SPZ,", t))
    umr = int(re.search(r"/UMR,(\d+)", t).group(1))
    assert umr == len(re.findall(r"\n\d+,/REG,", t))


def test_filtered_damos_renumbers_and_reads_back(tmp_path):
    path = os.path.join(REF, "EDC15", "038906019GQ.dam")
    if not os.path.isfile(path):
        pytest.skip("reference file missing")
    out = tmp_path / "f.dam"
    res = C.run(C.Job(sources=[path], target="dam", out=str(out),
                      filters={"pattern": "^(fnw|anw)"}))
    assert res.check == []
    back = C.load_definition(out.read_bytes())
    assert len(back.tables) == res.tables_out
    text = out.read_text("latin-1")
    nos = [int(n) for n in re.findall(r"\n(\d+),/SPZ,", text)]
    assert nos == list(range(1, len(nos) + 1))
    assert int(re.search(r"/SPC,(\d+)", text).group(1)) == len(nos)


def test_translated_damos_keeps_identifiers(tmp_path):
    path = os.path.join(REF, "EDC15", "038906019GQ.dam")
    if not os.path.isfile(path):
        pytest.skip("reference file missing")
    out = tmp_path / "en.dam"
    res = C.run(C.Job(sources=[path], target="dam", out=str(out),
                      translate={"backend": "builtin"}))
    assert res.check == []
    text = out.read_text("latin-1")
    assert "1,/SPZ,anwADF_DPL,{ADF ramp slope atmospheric pressure sensor},1" in text


def test_ols_to_damos_matches_bosch(tmp_path):
    """A DAMOS written from the WinOLS project of 028906021CK describes every
    table it shares with Bosch's own DAMOS at the same address, size, width
    and axes -- the scaling too, except where the project itself has none."""
    ols = os.path.join(INC, "028906021CK_MSA15.ols")
    bosch = os.path.join(INC, "028906021CK_A030V4G_MSA15.DAM")
    if not (os.path.isfile(ols) and os.path.isfile(bosch)):
        pytest.skip("reference files missing")
    out = tmp_path / "syn.dam"
    res = C.run(C.Job(sources=[ols], target="dam", out=str(out)))
    assert res.check == []
    img = C.load_definition(open(ols, "rb").read()).images[0][1]

    def ents(path, base):
        d = C.load_definition(open(path, "rb").read())
        b, s, _ = C.place(d, img, base, None, [])
        found = {}
        for e in entities(d, b, s, img):
            found.setdefault(e.name, e)
        return found

    a, b = ents(bosch, 0x38000), ents(str(out), None)
    common = a.keys() & b.keys()
    assert len(common) > 3000
    diff = collections.Counter()
    for n in common:
        x, y = a[n], b[n]
        for k in ("addr", "rows", "cols", "bits", "signed"):
            diff[k] += getattr(x, k) != getattr(y, k)
        for ax in ("x", "y"):
            ax1, ax2 = getattr(x, ax), getattr(y, ax)
            diff[ax] += (ax1.addr if ax1 else None) != (ax2.addr if ax2 else None)
        if y.eq.linear and x.eq.linear:
            diff["scale"] += abs(x.eq.factor - y.eq.factor) > 1e-9 * max(1, abs(x.eq.factor))
    assert sum(diff.values()) == 0, diff
