# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The parameter search engine (ovtsuite/search.py): matching, ranking, and the
address / value / byte scans, on small synthetic images plus -- when the
reference binaries are present -- a real one.

Nothing here needs Qt.
"""
import gzip
import json
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite import search as S                                  # noqa: E402
from ovtsuite.binary import BinaryFile                            # noqa: E402
from ovtsuite.model import Axis, Entity                           # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------- fixtures -----
class _Session:
    """What the engine reads of a TunerSession: entities and variants."""

    def __init__(self, entities, binfile):
        self.entities = entities
        self.binfile = binfile

    def variant_entities(self):
        return []


def _ent(bf, name, kind="constant", addr=0, rows=1, cols=1, bits=16, **kw):
    e = Entity(name=name, kind=kind, tier=kw.pop("tier", 1),
               categories=kw.pop("categories", ["Misc"]),
               description=kw.pop("description", ""), units=kw.pop("units", ""),
               addr=addr, rows=rows, cols=cols, bits=bits, signed=False,
               equation=kw.pop("equation", "X"), source="verified", **kw)
    e.binfile = bf
    return e


@pytest.fixture()
def world():
    data = bytearray(0x400)
    bf = BinaryFile(bytes(data), name="synthetic.bin", endian=">")
    # a 3 x 4 map, column-major (EDC16): cell (r, c) holds 100*r + c
    m = _ent(bf, "FlMng_qMaxSmk_MAP", kind="map", addr=0x100, rows=3, cols=4,
             description="OLS: FlMng_qMaxSmk_MAP | Smoke limitation fuel "
                         "quantity", units="[mg/hub]", categories=["Fuel"],
             tier=1)
    m.x = Axis(addr=0x80, count=4, bits=16)
    m.y = Axis(addr=0x90, count=3, bits=16)
    for r in range(3):
        for c in range(4):
            bf.write(0x100 + (c * 3 + r) * 2, 100 * r + c, 16)
    for i in range(4):
        bf.write(0x80 + 2 * i, 1000 + i, 16)
    for i in range(3):
        bf.write(0x90 + 2 * i, 2000 + i, 16)
    # a row-major map (EDC15): cell (r, c) at r*cols + c
    m2 = _ent(bf, "EDC15_rowmajor", kind="map", addr=0x200, rows=2, cols=3,
              order="row", description="Row-major map", tier=2)
    for r in range(2):
        for c in range(3):
            bf.write(0x200 + (r * 3 + c) * 2, 7 * (r * 3 + c), 16)
    # constants
    k1 = _ent(bf, "InjCrv_tPreTrbn_C", addr=0x300,
              description="Switch-on threshold of the pre-turbine "
                          "temperature", units="[deg C]", tier=3)
    bf.write(0x300, 1234, 16)
    k2 = _ent(bf, "AirCtl_rBoostLimit_C", addr=0x302,
              description="Boost pressure limit ratio", units="[-]", tier=1)
    bf.write(0x302, 4321, 16)
    k3 = _ent(bf, "TurboCharger_bar_C", addr=0x304,
              description="Charger", categories=["Air path"], tier=1)
    bf.write(0x304, 55, 16)
    bf.write(0x310, 0xDEADBEEF, 32)
    bf.write(0x320, struct.unpack(">I", struct.pack(">f", 50.0))[0], 32)
    for i, ch in enumerate(b"09L927156C"):
        bf.write(0x340 + i, ch, 8)
    ents = [m, m2, k1, k2, k3]
    sess = _Session(ents, bf)
    src = S.Source("synthetic", sess)
    eng = S.SearchEngine(title_fn=lambda e: e.description.split("|")[-1].strip()
                         or e.name, tier_name=lambda t: "Tier %d" % t)
    return eng, src, ents, bf


def names(hits):
    return [h.entity.name for h in hits]


# ------------------------------------------------------------- text -------
def test_fuzzy_ranks_prefix_and_hump_above_scatter(world):
    eng, src, ents, _ = world
    hits = eng.run("boost", S.SearchOptions(), [src])
    assert names(hits)[0] == "AirCtl_rBoostLimit_C"
    # "TurboCharger_bar_C" does not contain b-o-o-s-t in order in its symbol
    assert "TurboCharger_bar_C" not in names(hits)


def test_long_fields_need_the_word_not_scattered_letters(world):
    """b-o-o-s-t can be picked out of almost any sentence; that is not a
    match. Scatter is for short names (abbreviations), not descriptions."""
    eng, src, ents, bf = world
    sc = _ent(bf, "Zzz_x_C", addr=0x306,
              description="big old orange oats stack", tier=2)
    src2 = S.Source("scatter", _Session(ents + [sc], bf))
    assert "Zzz_x_C" not in names(eng.run("boost", S.SearchOptions(), [src2]))
    o = S.SearchOptions(match="regex", fields=("description",))
    assert "Zzz_x_C" in names(eng.run("b.*o.*o.*s.*t", o, [src2]))
    # ... but a real word in a description is found
    assert "InjCrv_tPreTrbn_C" in names(
        eng.run("turbine", S.SearchOptions(), [src2]))


def test_scatter_is_bounded_in_short_names(world):
    eng, src, *_ = world
    o = S.SearchOptions(fields=("symbol",))
    assert names(eng.run("flmsmk", o, [src])) == ["FlMng_qMaxSmk_MAP"]
    assert eng.run("fzzz", o, [src]) == []
    # a letter pair spread across a long name is not a match
    sc = S.fuzzy_score("ax", "a" + "q" * 30 + "x", "a" + "q" * 30 + "x",
                       S.char_mask("ax"), S.char_mask("a" + "q" * 30 + "x"))
    assert sc is None


def test_fuzzy_words_in_any_order_and_all_required(world):
    eng, src, *_ = world
    o = S.SearchOptions()
    assert names(eng.run("smoke fuel", o, [src])) == ["FlMng_qMaxSmk_MAP"]
    assert names(eng.run("fuel smoke", o, [src])) == ["FlMng_qMaxSmk_MAP"]
    assert eng.run("smoke boost", o, [src]) == []


def test_fuzzy_subsequence_across_symbol(world):
    eng, src, *_ = world
    assert names(eng.run("flmsmk", S.SearchOptions(fields=("symbol",)),
                         [src])) == ["FlMng_qMaxSmk_MAP"]


def test_case_sensitive_fuzzy_does_not_reject_uppercase(world):
    """The bit-mask prefilter works on lowered text; a case-sensitive query
    for an upper-case letter must still get past it."""
    eng, src, *_ = world
    o = S.SearchOptions(case_sensitive=True, fields=("symbol",))
    assert "AirCtl_rBoostLimit_C" in names(eng.run("AirCtl", o, [src]))
    assert eng.run("airctl", o, [src]) == []


def test_exact_whole_word_treats_underscore_as_separator(world):
    eng, src, *_ = world
    o = S.SearchOptions(match="exact", whole_word=True, fields=("symbol",))
    assert names(eng.run("InjCrv", o, [src])) == ["InjCrv_tPreTrbn_C"]
    assert eng.run("Injc", o, [src]) == []
    o2 = S.SearchOptions(match="exact", fields=("symbol",))
    assert "InjCrv_tPreTrbn_C" in names(eng.run("injc", o2, [src]))


def test_regex_and_bad_regex(world):
    eng, src, *_ = world
    o = S.SearchOptions(match="regex", fields=("symbol",))
    assert set(names(eng.run(r"_[rt][A-Z]\w+_C$", o, [src]))) == {
        "InjCrv_tPreTrbn_C", "AirCtl_rBoostLimit_C"}
    with pytest.raises(S.QueryError):
        eng.run("([", o, [src])


def test_empty_query_is_an_error_not_everything(world):
    eng, src, *_ = world
    with pytest.raises(S.QueryError):
        eng.run("   ", S.SearchOptions(), [src])


def test_tier_filter(world):
    eng, src, *_ = world
    o = S.SearchOptions(match="regex", fields=("symbol",), tier=3)
    assert names(eng.run(".", o, [src])) == ["InjCrv_tPreTrbn_C"]


def test_category_field_includes_tier_name(world):
    eng, src, *_ = world
    o = S.SearchOptions(match="exact", fields=("category",))
    assert names(eng.run("tier 3", o, [src])) == ["InjCrv_tPreTrbn_C"]
    assert names(eng.run("air path", o, [src])) == ["TurboCharger_bar_C"]


def test_shipped_title_cache_is_used_by_key(world):
    eng, src, ents, _ = world
    e = ents[3]
    titles = {S.title_key(e): "CACHED TITLE"}
    eng2 = S.SearchEngine(lambda e: "COMPUTED", titles=titles)
    hits = eng2.run("cached", S.SearchOptions(fields=("title",)), [src])
    assert names(hits) == [e.name]
    # an entity whose fields changed no longer matches its cache key
    e2 = ents[2]
    assert S.title_key(e2) not in titles


# ------------------------------------------------------------ address -----
def test_parse_addresses():
    assert S.parse_addresses("0x100") == [(0x100, 0x101)]
    assert S.parse_addresses("$100, 200") == [(0x100, 0x101), (0x200, 0x201)]
    assert S.parse_addresses("100-10F") == [(0x100, 0x110)]
    assert S.parse_addresses("0x100+4") == [(0x100, 0x104)]
    assert S.parse_addresses("256", base=10) == [(256, 257)]
    assert S.parse_addresses("10F..100") == [(0x100, 0x110)]
    with pytest.raises(S.QueryError):
        S.parse_addresses("zz")
    with pytest.raises(S.QueryError):
        S.parse_addresses("1G")


def test_address_inside_a_map_resolves_to_the_cell_column_major(world):
    eng, src, ents, _ = world
    # cell (r=2, c=3) is stored at index c*rows + r = 11 -> 0x100 + 22
    h = eng.run("0x%X" % (0x100 + 22), S.SearchOptions(mode="address"), [src])
    assert [x.entity.name for x in h] == ["FlMng_qMaxSmk_MAP"]
    assert h[0].cells == (("z", 2, 3),)
    # the second byte of that 16-bit cell is the same cell
    h = eng.run("0x%X" % (0x100 + 23), S.SearchOptions(mode="address"), [src])
    assert h[0].cells == (("z", 2, 3),)


def test_address_row_major_cell(world):
    eng, src, *_ = world
    # (r=1, c=2) -> index 1*3 + 2 = 5 -> 0x200 + 10
    h = eng.run("0x20A", S.SearchOptions(mode="address"), [src])
    assert h[0].entity.name == "EDC15_rowmajor"
    assert h[0].cells == (("z", 1, 2),)


def test_address_in_axes(world):
    eng, src, *_ = world
    h = eng.run("0x84", S.SearchOptions(mode="address"), [src])
    assert h[0].cells == (("x", 2, 0),) and "X axis" in h[0].where
    h = eng.run("0x92", S.SearchOptions(mode="address"), [src])
    assert h[0].cells == (("y", 1, 0),)


def test_address_range_lists_every_parameter_touched(world):
    eng, src, *_ = world
    h = eng.run("0x300-0x305", S.SearchOptions(mode="address"), [src])
    assert [x.entity.name for x in h] == [
        "InjCrv_tPreTrbn_C", "AirCtl_rBoostLimit_C", "TurboCharger_bar_C"]


def test_address_not_covered_gives_nearest_hint(world):
    eng, src, *_ = world
    o = S.SearchOptions(mode="address")
    assert eng.run("0x3F0", o, [src]) == []
    hint = eng.nearest_hint("0x3F0", o, [src])
    assert "Nothing covers 0x3F0" in hint and "TurboCharger_bar_C" in hint


def test_address_decimal_base(world):
    eng, src, *_ = world
    o = S.SearchOptions(mode="address", base=10)
    h = eng.run(str(0x300), o, [src])
    assert h[0].entity.name == "InjCrv_tPreTrbn_C"


def test_every_cell_of_the_map_round_trips_through_its_address(world):
    """Address -> cell must be the inverse of Entity.index for both orders."""
    eng, src, ents, _ = world
    for e in (ents[0], ents[1]):
        for r in range(e.rows):
            for c in range(e.cols):
                a = e.addr + e.index(r, c) * e.step
                h = eng.run("0x%X" % a, S.SearchOptions(mode="address"),
                            [src])
                mine = [x for x in h if x.entity is e]
                assert mine and mine[0].cells == (("z", r, c),), (e.name, r, c)


# -------------------------------------------------------------- value -----
def test_value_exact_range_and_comparison(world):
    eng, src, *_ = world
    o = S.SearchOptions(mode="value", include_axes=False)
    assert names(eng.run("1234", o, [src])) == ["InjCrv_tPreTrbn_C"]
    assert names(eng.run("4000..5000", o, [src])) == ["AirCtl_rBoostLimit_C"]
    h = eng.run("200..203", o, [src])
    assert h[0].entity.name == "FlMng_qMaxSmk_MAP"
    assert set(h[0].cells) == {("z", 2, 0), ("z", 2, 1), ("z", 2, 2),
                               ("z", 2, 3)}
    assert h[0].n_cells == 4


def test_value_in_axes_only_when_asked(world):
    eng, src, *_ = world
    assert eng.run("1002", S.SearchOptions(mode="value",
                                           include_axes=False), [src]) == []
    h = eng.run("1002", S.SearchOptions(mode="value"), [src])
    assert h[0].cells == (("x", 2, 0),)


def test_value_uses_equation_unless_raw(world):
    eng, src, ents, bf = world
    ents[2].equation = "(X)/(10)"
    ents[2]._eq = None
    o = S.SearchOptions(mode="value", include_axes=False)
    assert names(eng.run("123.4", o, [src])) == ["InjCrv_tPreTrbn_C"]
    assert eng.run("1234", o, [src]) == []
    o.raw_values = True
    assert names(eng.run("1234", o, [src])) == ["InjCrv_tPreTrbn_C"]
    assert names(eng.run("0x4D2", o, [src])) == ["InjCrv_tPreTrbn_C"]


def test_value_bad_input(world):
    eng, src, *_ = world
    with pytest.raises(S.QueryError):
        eng.run("twelve", S.SearchOptions(mode="value"), [src])
    with pytest.raises(S.QueryError):
        eng.run("", S.SearchOptions(mode="value"), [src])


# -------------------------------------------------------------- bytes -----
def test_bytes_requires_a_target(world):
    eng, src, *_ = world
    with pytest.raises(S.QueryError):
        eng.run("DE AD", S.SearchOptions(mode="bytes"), [src])


def test_bytes_hex_with_wildcards_maps_to_cells(world):
    eng, src, ents, bf = world
    m = ents[0]
    o = S.SearchOptions(mode="bytes", include_axes=False)
    # cell (1, 2) = 102 = 0x0066 stored at index 2*3 + 1 = 7
    h = eng.run("00 66", o, [src], target=m)
    assert [x.addr for x in h] == [0x100 + 14]
    assert h[0].cells == (("z", 1, 2),)
    h = eng.run("00 ??", S.SearchOptions(mode="bytes", include_axes=False),
                [src], target=m)
    assert len(h) >= 12                         # every 00xx cell and overlaps


def test_bytes_stays_inside_the_parameter(world):
    eng, src, ents, bf = world
    k1 = ents[2]                                # holds 1234 = 04 D2
    o = S.SearchOptions(mode="bytes")
    assert [x.addr for x in eng.run("04 D2", o, [src], target=k1)] == [0x300]
    # 43 21 is in AirCtl_rBoostLimit_C, a different parameter
    assert eng.run("10 E1", o, [src], target=k1) == []


def test_bytes_int_endianness_and_width(world):
    eng, src, ents, bf = world
    k = ents[2]
    o = S.SearchOptions(mode="bytes", bytes_kind="int", width=16)
    assert len(eng.run("1234", o, [src], target=k)) == 1       # auto = big
    o.endian = "little"
    assert eng.run("1234", o, [src], target=k) == []
    o.endian = "big"
    o.width = 8
    with pytest.raises(S.QueryError):
        eng.run("300", o, [src], target=k)                      # > 255


def test_bytes_ascii_case_option(world):
    eng, src, ents, bf = world
    pn = _ent(bf, "PartNo", addr=0x340, rows=1, cols=10, bits=8)
    sess = _Session([pn], bf)
    src2 = S.Source("pn", sess)
    o = S.SearchOptions(mode="bytes", bytes_kind="ascii")
    assert len(eng.run("927156", o, [src2], target=pn)) == 1
    assert len(eng.run("09l9", o, [src2], target=pn)) == 1      # insensitive
    o.case_sensitive = True
    assert eng.run("09l9", o, [src2], target=pn) == []


def test_bytes_float32(world):
    eng, src, ents, bf = world
    f = _ent(bf, "F", addr=0x320, bits=32, is_float=True)
    src2 = S.Source("f", _Session([f], bf))
    o = S.SearchOptions(mode="bytes", bytes_kind="float", width=32)
    assert [x.addr for x in eng.run("50", o, [src2], target=f)] == [0x320]


def test_bad_hex_is_explained(world):
    eng, src, ents, _ = world
    with pytest.raises(S.QueryError, match="two digits"):
        eng.run("DEA", S.SearchOptions(mode="bytes"), [src], target=ents[2])


# ------------------------------------------------------- cancel / sources --
def test_cancel_stops_a_search(world):
    eng, src, *_ = world
    c = S.Cancel()
    c.set()
    with pytest.raises(S.Cancelled):
        eng.run("1", S.SearchOptions(mode="value"), [src], cancel=c)


def test_two_sources_are_searched_and_labelled(world):
    eng, src, ents, bf = world
    other = S.Source("comparison", _Session(list(ents), bf), role="comparison")
    h = eng.run("InjCrv", S.SearchOptions(), [src, other])
    assert {x.source.label for x in h} == {"synthetic", "comparison"}


def test_bytes_search_runs_against_the_comparison_twin(world):
    eng, src, ents, bf = world
    data2 = bytearray(bf.data)
    bf2 = BinaryFile(bytes(data2), name="cmp.bin", endian=">")
    twin = _ent(bf2, ents[2].name, addr=0x300)
    bf2.write(0x300, 0x1234, 16)
    other = S.Source("cmp", _Session([twin], bf2), role="comparison")
    o = S.SearchOptions(mode="bytes")
    h = eng.run("12 34", o, [src, other], target=ents[2])
    assert [x.source.label for x in h] == ["cmp"]


# ------------------------------------------------------------ history -----
def test_history_is_mru_deduped_and_round_trips():
    h = S.History(limit=3)
    for q in ("a", "b", "a", "c", "d"):
        h.push("text", q)
    h.push("value", "5")
    h.push("text", "   ")
    assert h.for_mode("text") == ["d", "c"]
    assert len(h.items) == 3
    h2 = S.History.loads(h.dumps(), limit=3)
    assert h2.items == h.items
    assert S.History.loads("not json").items == []


def test_options_round_trip_and_reject_junk():
    o = S.SearchOptions(mode="bytes", match="regex", fields=("symbol",))
    o2 = S.SearchOptions.from_dict(json.loads(json.dumps(o.to_dict())))
    assert o2 == o
    j = S.SearchOptions.from_dict({"mode": "nope", "match": "x",
                                   "fields": ["bogus"], "zzz": 1})
    assert j.mode == "text" and j.match == "fuzzy"
    assert j.fields == S.DEFAULT_FIELDS


# ------------------------------------------------------- shipped index ----
def test_shipped_index_ignored_when_stale_or_missing(tmp_path):
    p = tmp_path / "idx.json.gz"
    assert S.load_shipped_titles(str(p), lambda: ("d", "u")) is None
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump({"format": S.FORMAT, "data": "d", "ui": "u",
                   "titles": {"k": "v"}}, fh)
    assert S.load_shipped_titles(str(p), lambda: ("d", "u")) == {"k": "v"}
    assert S.load_shipped_titles(str(p), lambda: ("d2", "u")) is None
    assert S.load_shipped_titles(str(p), lambda: ("d", "u2")) is None
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump({"format": S.FORMAT + 1, "data": "d", "ui": "u",
                   "titles": {}}, fh)
    assert S.load_shipped_titles(str(p), lambda: ("d", "u")) is None


def test_fuzzy_score_prefers_tight_and_word_start():
    def sc(tok, text):
        return S.fuzzy_score(tok, text.lower(), text, S.char_mask(tok),
                             S.char_mask(text.lower()))
    assert sc("abc", "abc") > sc("abc", "a_b_c") > sc("abc", "axxbxxc")
    assert sc("xyz", "abc") is None
    assert sc("lim", "AirCtl_rBoostLimit_C") > sc("lim", "slimy")


# ------------------------------------------------- real binary (if here) --
_REAL = os.path.join(ROOT, "Reference", "Binaries", "038906016Q.Bin")


@pytest.mark.skipif(not os.path.exists(_REAL), reason="no reference binary")
def test_real_image_every_parameter_resolves_from_its_own_address():
    from ovtsuite.session import TunerSession
    sess = TunerSession(_REAL)
    src = S.Source("real", sess)
    eng = S.SearchEngine(lambda e: e.name)
    idx = eng.addresses(src)
    ents = [e for e in sess.entities if e.rows * e.cols > 0
            and e.addr is not None]
    assert len(ents) > 1000
    bad = []
    for e in ents[::7]:
        spans = [s for s in idx.overlapping(e.addr, e.addr + 1)
                 if s.ent is e and s.kind == "z"]
        if not spans or spans[0].cell_at(e.addr) != ("z", 0, 0):
            bad.append(e.name)
    assert not bad, bad[:5]
    # a text search over ~10k parameters is interactive
    import time
    t = time.time()
    hits = eng.run("boost pressure", S.SearchOptions(), [src])
    assert hits and time.time() - t < 2.0


@pytest.mark.skipif(not os.path.exists(_REAL), reason="no reference binary")
def test_real_image_value_search_finds_a_known_constant():
    from ovtsuite.session import TunerSession
    sess = TunerSession(_REAL)
    src = S.Source("real", sess)
    eng = S.SearchEngine(lambda e: e.name)
    k = next(e for e in sess.entities
             if e.kind == "constant" and not e.text_kind and e.bits == 16)
    v = k.cell(0, 0)
    h = eng.run("%.9g" % v, S.SearchOptions(mode="value", include_axes=False,
                                           tolerance=1e-6), [src])
    assert any(x.entity is k for x in h)


@pytest.mark.skipif(not os.path.exists(_REAL), reason="no reference binary")
def test_shipped_index_agrees_with_the_live_labels_where_it_applies():
    """Where the shipped cache has an entry for a parameter, it must be the
    very label the tree shows -- and it should cover almost all of an EDC16
    image. If gui.py has changed since the index was built the index is stale
    and correctly ignored; rebuild it with tools/build_search_index.py."""
    titles = S.load_shipped_titles()
    if titles is None:
        pytest.skip("search index is stale or missing -- run "
                    "tools/build_search_index.py")
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    sess = TunerSession(_REAL)
    # catalogue-named entities (named maps, fault-path settings) are made
    # live by design -- a selector note can change with an edit -- and so are
    # coding-switched copies, whose coding is read from this image
    ents = [e for e in S.SearchEngine.entities_of(sess)
            if not getattr(e, "plain", "") and not getattr(e, "coding", "")]
    hit = [e for e in ents if S.title_key(e) in titles]
    assert len(hit) >= 0.95 * len(ents), (len(hit), len(ents))
    wrong = [e.name for e in hit if titles[S.title_key(e)] != gui._english(e)]
    assert not wrong, wrong[:5]


# -- wildcards (* and ?) ---------------------------------------------------------

def test_wildcard_body_and_regex():
    from ovtsuite import search as S
    assert S.wildcard_regex("plain") is None
    p = S.wildcard_regex("acc*map")
    assert p.search("xx_AccPed_trqEngLim_MAP") and not p.search("AccPed_C")
    assert S.wildcard_regex("n?_max").search("n2_Max")
    assert not S.wildcard_regex("n?_max").search("n12_max_")
    # literal everywhere else: regex characters mean themselves
    assert S.wildcard_regex("a.b*").search("a.bc")
    assert not S.wildcard_regex("a.b*").search("axbc")
    # with a separator a wildcard stays inside one field
    sep = S.wildcard_regex("boost*limit", sep="\0")
    assert sep.search("boost pressure limit\0x")
    assert not sep.search("boost\0limit")


def test_wildcards_in_parameter_searches(world):
    eng, src, *_ = world
    sym = ("symbol",)
    fuzzy = S.SearchOptions(fields=sym)
    assert names(eng.run("Inj*Trbn", fuzzy, [src])) == ["InjCrv_tPreTrbn_C"]
    # a wildcard word and a fuzzy word together: both must match
    assert names(eng.run("*Boost* airctl", fuzzy, [src])) == [
        "AirCtl_rBoostLimit_C"]
    exact = S.SearchOptions(match="exact", fields=sym)
    assert names(eng.run("injcrv_?pre", exact, [src])) == ["InjCrv_tPreTrbn_C"]
    whole = S.SearchOptions(match="exact", whole_word=True, fields=sym)
    # whole words: a wildcard stays inside one word (_ separates words)
    assert names(eng.run("InjC*", whole, [src])) == ["InjCrv_tPreTrbn_C"]
    assert names(eng.run("InjC??", whole, [src])) == ["InjCrv_tPreTrbn_C"]
    assert names(eng.run("InjC*Trbn", whole, [src])) == []
    assert names(eng.run("InjC*Trbn", exact, [src])) == ["InjCrv_tPreTrbn_C"]
