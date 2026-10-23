# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The binaries a WinOLS project embeds: read (every version, not one run of
bytes), used as the binary when none is given, listed and extracted (the
converter, the CLI, the AI tool); and the categories an OLS converts to in
an XDF (code blocks, folders, their numbering).

``python -m pytest tests/test_convert_embedded.py -q``"""

import os
import re
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
REF = os.path.join(ROOT, "Reference")
MSA15 = os.path.join(REF, "_incoming", "2026-10-14", "028906021CK_MSA15.ols")
VP37 = os.path.join(REF, "EDC15", "0281001410_MSA15_90hp_vp37.ols")        # 2 versions
M235 = os.path.join(REF, "WinOLS-formats", "f440_M235i_translated_axis_texts.ols")
BKC = os.path.join(REF, "A2L", "2007_VW_DAMOS_Golf_Original_03G906016B__5645_"
                   "0281011478_369564_BKC_1.9_104.7PS.bin.ols")               # 2 areas
GQ = os.path.join(REF, "EDC15", "038906019GQ.ols")
PAIR = os.path.join("C:\\Claude", "WinOLS samples", "Tests", "OLS-tests",
                    "( 038906019GQ  0281010941 (Original) - 363213)")


def _data(path):
    if not os.path.isfile(path):
        pytest.skip("%s not present" % os.path.basename(path))
    with open(path, "rb") as fh:
        return fh.read()


# ------------------------------------------------------------------ reading
def test_every_version_of_a_project_is_its_own_image():
    """f440: three versions (Original, Stage 1, Stage 1_AEE) of a 4 MB
    binary, stored one after the other -- not one 12 MB image."""
    from ovtsuite.definitions import winols as W
    d = _data(M235)
    wf = W.parse(d)
    assert [v.name for v in wf.versions] == ["Original", "Stage 1", "Stage 1_AEE"]
    assert all(len(v.data) == 0x400000 for v in wf.versions)
    assert wf.image == wf.versions[0].data
    assert wf.versions[1].data != wf.image                 # a tuned version
    assert len(d) == d.index(W.IMAGE_SIG, len(d) - 0xC00004) + 4 + 0xC00000
    fast = W.scan_images(d)
    assert [v.data for v in fast.versions] == [v.data for v in wf.versions]


def test_a_small_project_with_a_version_is_split_too():
    from ovtsuite.definitions import winols as W
    wf = W.parse(_data(VP37))
    assert [(v.name, len(v.data)) for v in wf.versions] == [("Original", 0x8000),
                                                            ("org", 0x8000)]


def test_memory_areas_of_an_image_are_found():
    from ovtsuite.definitions import winols as W
    wf = W.parse(_data(BKC))
    assert len(wf.image) == 0x170000
    assert wf.areas == [("#1", 0, 0x6FFFF), ("#2", 0x70000, 0x16FFFF)]


def test_the_source_list_says_what_a_project_holds():
    from ovtsuite.convert import embedded
    _data(M235)
    s = embedded.summary(M235)
    assert "9477 maps" in s and "4 MB binary" in s and "2 versions (Stage 1, Stage 1_AEE)" in s


# ------------------------------------------------- the embedded binary used
def test_the_embedded_binary_is_the_binary_when_none_is_given(tmp_path):
    from ovtsuite import convert as C
    from ovtsuite.convert import calvalues
    _data(MSA15)
    res = C.run(C.Job(sources=[MSA15], target="dcm", out=str(tmp_path / "v.dcm")))
    assert res.size == 0x40000 and len(calvalues.load(open(res.out[0], "rb").read()).params) > 7000
    assert any("embedded in the WinOLS project" in w for w in res.warnings)
    # values written into a copy of it, with no binary given (a project
    # whose maps are listed once: its own values change nothing)
    _data(VP37)
    res = C.run(C.Job(sources=[VP37], target="dcm", out=str(tmp_path / "w.dcm")))
    res = C.run(C.Job(sources=[VP37, str(tmp_path / "w.dcm")], target="bin",
                      out=str(tmp_path / "o.bin")))
    assert not res.check and res.tables_out > 3000
    from ovtsuite.definitions import winols as W
    assert open(str(tmp_path / "o.bin"), "rb").read() == W.parse(_data(VP37)).image


def test_an_image_target_writes_the_original_not_every_version(tmp_path):
    from ovtsuite import convert as C
    _data(VP37)
    res = C.run(C.Job(sources=[VP37], target="bin", out=str(tmp_path / "o.bin")))
    assert os.path.getsize(res.out[0]) == 0x8000


# ---------------------------------------------------------------- extracting
def test_extract_the_default_images_exactly_as_stored(tmp_path):
    from ovtsuite import convert as C
    from ovtsuite.definitions import winols as W
    wf = W.parse(_data(M235))
    res = C.run(C.Job(sources=[M235], target=C.EXTRACT, out=str(tmp_path)))
    names = sorted(os.path.basename(p) for p in res.out)
    stem = "f440_M235i_translated_axis_texts"
    # the original keeps the extension of the file WinOLS imported (.ORI)
    assert names == sorted([stem + "_original.ori", stem + "_stage_1.bin",
                            stem + "_stage_1_aee.bin"])
    by = {os.path.basename(p): open(p, "rb").read() for p in res.out}
    assert by[stem + "_original.ori"] == wf.versions[0].data
    assert by[stem + "_stage_1_aee.bin"] == wf.versions[2].data
    assert not res.check


def test_extract_some_or_all_and_other_formats(tmp_path):
    from ovtsuite import convert as C
    from ovtsuite.convert import image
    from ovtsuite.definitions import winols as W
    wf = W.parse(_data(BKC))
    # areas are not ticked by default; named, they are written alone
    res = C.run(C.Job(sources=[BKC], target=C.EXTRACT, out=str(tmp_path),
                      options={"extract": ["original-area2"], "extract_format": "hex"}))
    assert len(res.out) == 1 and res.out[0].endswith("_original_area2.hex")
    img = image.read(open(res.out[0], "rb").read())
    assert img.start == 0x70000 and img.flat() == wf.image[0x70000:]
    res = C.run(C.Job(sources=[BKC], target=C.EXTRACT, out=str(tmp_path / "all"),
                      options={"extract": "all"}))
    assert len(res.out) == 3
    # the window's picks: a name of the user's own
    res = C.run(C.Job(sources=[BKC], target=C.EXTRACT, out=str(tmp_path / "w"),
                      options={"extract": [{"source": BKC, "key": "original-area1",
                                            "name": "mpc.bin"}]}))
    assert [os.path.basename(p) for p in res.out] == ["mpc.bin"]
    assert open(res.out[0], "rb").read() == wf.image[:0x70000]


def test_extract_never_overwrites_the_project(tmp_path):
    from ovtsuite import convert as C
    _data(VP37)
    with pytest.raises(C.ConvertError):
        C.run(C.Job(sources=[VP37], target=C.EXTRACT, out=os.path.dirname(VP37),
                    options={"extract": [{"source": VP37, "key": "original",
                                          "name": os.path.basename(VP37)}]}))


def test_extract_from_the_command_line(tmp_path, capsys):
    from ovtsuite.convert import cli
    import argparse
    _data(VP37)
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    cli.add_parser(sub)
    a = p.parse_args(["convert", VP37, "--list-images"])
    assert a.fn(a) == 0
    out = capsys.readouterr().out
    assert "version1" in out and "_org.bin" in out
    a = p.parse_args(["convert", VP37, "--extract-images", "-o", str(tmp_path),
                      "--only", "org"])
    assert a.fn(a) == 0
    assert os.listdir(str(tmp_path)) == ["0281001410_MSA15_90hp_vp37_org.bin"]


def test_extract_through_the_ai_tool(tmp_path):
    from ovtsuite.ai.tools import Context, Registry
    from ovtsuite.convert import ai_tools
    _data(VP37)
    reg = Registry()
    ai_tools.register(reg)
    ok, r = reg.call("inspect_file_format", Context(None), {"path": VP37})
    assert ok and [e["key"] for e in r["embedded_images"]] == ["original", "version1"]
    ok, r = reg.call("extract_embedded_images", Context(None),
                     {"sources": [VP37], "out_dir": str(tmp_path), "only": ["original"]})
    assert ok and len(r["written"]) == 1 and os.path.getsize(r["written"][0]) == 0x8000


# ---------------------------------------------------------------- categories
def test_folders_of_every_layout_are_read():
    """The folder list's layout differs between formats (string or
    multilingual name, tails of 0 to 29 bytes); each is read whole, and a
    folder inside another names it."""
    from ovtsuite.definitions import winols as W
    wf = W.parse(_data(M235))
    assert len(wf.folders) == 525 and wf.folders[0] == "My maps"
    used = {r.folder for r in wf.records if r.type != 1}
    assert used <= set(wf.folders)
    wf = W.parse(_data(BKC))
    assert len(wf.folders) == 364
    assert {r.folder for r in wf.records if r.type != 1} <= set(wf.folders)
    assert any(wf.folders[p] == "Potential maps" for p in wf.parents.values())


def _xdf_cats(text):
    head = text[:text.find("</XDFHEADER>")]
    names = {int(i, 16): n for i, n in
             re.findall(r'<CATEGORY index="0x([0-9A-Fa-f]+)" name="([^"]*)"', head)}
    tables = []
    for kind, b in re.findall(r"<(XDFTABLE|XDFCONSTANT)[^>]*>(.*?)</\1>", text, re.S):
        z = b.split('<XDFAXIS id="z"')[-1] if kind == "XDFTABLE" else b
        addr = int(re.search(r'mmedaddress="0x([0-9A-Fa-f]+)"', z).group(1), 16)
        mems = tuple((int(i), names[int(c) - 1]) for i, c in
                     re.findall(r'<CATEGORYMEM index="(\d+)" category="(\d+)"', b))
        tables.append((addr, mems))
    return [names[k] for k in sorted(names)], tables


def test_ols_categories_match_the_expected_xdf(tmp_path):
    """The sample pair: an EDC15 project and the XDF expected of it. Three code blocks (Codeblock 0-2) numbered first, each map in
    its block (CATEGORYMEM 0) and its WinOLS folder (CATEGORYMEM 1), the
    folders numbered by name. The one name that differs is the folder
    WinOLS calls "My maps", which the expected XDF names from its maps' commonest
    word ("LLR")."""
    from ovtsuite import convert as C
    src = PAIR + ".ols" if os.path.isfile(PAIR + ".ols") else GQ
    if not os.path.isfile(PAIR + ".xdf"):
        pytest.skip("the sample pair is not present")
    _data(src)
    with open(PAIR + ".xdf", encoding="latin-1") as fh:
        ref_names, ref_tables = _xdf_cats(fh.read())
    out = str(tmp_path / "o.xdf")
    C.run(C.Job(sources=[src], target="xdf", out=out, self_check=False))
    with open(out, encoding="latin-1") as fh:
        names, tables = _xdf_cats(fh.read())
    rename = {"LLR": "My maps"}
    assert names == [rename.get(n, n) for n in ref_names]
    assert names[:3] == ["Codeblock 0", "Codeblock 1", "Codeblock 2"]
    want = Counter((a, tuple((i, rename.get(n, n)) for i, n in m)) for a, m in ref_tables)
    assert Counter(tables) == want


def test_categories_of_a_project_with_one_code_block(tmp_path):
    """No code block category when the maps are listed once; folders by
    name; German letters kept as character references (as TunerPro writes them), not lost as '?'."""
    from ovtsuite import convert as C
    from ovtsuite.xdf_export import _esc
    _data(M235)
    out = str(tmp_path / "o.xdf")
    C.run(C.Job(sources=[M235], target="xdf", out=out, self_check=False))
    with open(out, encoding="latin-1") as fh:
        names, tables = _xdf_cats(fh.read())
    assert not any(n.startswith("Codeblock") for n in names)
    assert names == sorted(names, key=str.lower)
    assert all(len(m) == 1 and m[0][0] == 0 for _a, m in tables)
    assert _esc("Bremskraftverst\xe4rker \xdcberwachung Gro\xdf 5\xb0C") == \
        "Bremskraftverst&#228;rker &#220;berwachung Gro&#223; 5&#176;C"


def test_winols_xdf_fields_match_the_expected_xdf(tmp_path):
    """Titles from the description, 'OLS: name | ...' descriptions with the
    axis names, WinOLS's decimals, the range, signedness, umlauts kept."""
    import re
    from ovtsuite import convert as C
    gq = os.path.join(r"C:\Claude\WinOLS samples\Tests\OLS-tests",
                      "( 038906019GQ  0281010941 (Original) - 363213).ols")
    if not os.path.exists(gq):
        pytest.skip("needs the sample WinOLS project")
    out = str(tmp_path / "o.xdf")
    C.run(C.Job(sources=[gq], target="xdf", out=out, self_check=False))
    text = open(out, encoding="utf-8").read()
    i = text.index("OLS: mrwBMVE_KL")
    block = text[text.rindex("<XDFTABLE", 0, i):text.index("</XDFTABLE>", i)]
    assert "<title>BEG Schwellwert VE Begrenzungsmenge M_E = f(N)</title>" in block
    assert "| Z: Umrechnung Menge | X: Umrechnung Drehzahl</description>" in block
    assert "<decimalpl>4</decimalpl>" in block
    assert "<min>-327.68</min>" in block and "<max>327.67</max>" in block
    assert "<outputtype>1</outputtype>" in block
    i = text.index("OLS: mrwV_ANFAH")
    const = text[text.rindex("<XDFCONSTANT", 0, i):text.index("</XDFCONSTANT>", i)]
    assert "<datatype>0</datatype>" in const and "<max>655.35</max>" in const
    assert "?" not in re.sub(r"<\?xml[^>]*\?>", "", text[:2000])
