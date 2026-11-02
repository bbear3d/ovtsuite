# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The command palette's search (``ovtsuite.diag.palette_index``, no Qt):
token AND matching, word-start scoring, match spans, the ``NN:`` and ``>``
prefixes, fault codes in each spelling, the empty query, the chip counts and
speed. ``python -m pytest tests/test_diag_palette_index.py -q``"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ovtsuite.diag import palette_index as PI  # noqa: E402
from ovtsuite.diag.palette_index import PaletteItem as P, search  # noqa: E402


def _items():
    return [
        P("live", "Boost pressure, actual", "01", "Engine group 011 cell 3", "Ldm_pIst", value="1012 mbar"),
        P("live", "Boost pressure, specified", "01", "Engine group 011 cell 2", "Ldm_pSoll"),
        P("live", "N75 boost control valve duty", "01", "Engine group 011 cell 4"),
        P("live", "Coolant temperature", "01", "Engine group 001 cell 4"),
        P("live", "Vehicle speed", "03", "ABS group 001 cell 1"),
        P("fault", "Boost pressure regulation: control range not reached", "01", "Stored", "P0299 008857",
          chip="active"),
        P("fault", "Boost pressure sensor: implausible signal", "01", "Last seen", "P0235 000565"),
        P("fault", "Lost communication with ABS control module", "01", "", "U0121 0x4121"),
        P("actuator", "Actuator test: boost pressure control valve N75", "01"),
        P("adaptation", "Boost adaptation reset", "01"),
        P("procedure", "Boost leak check", ""),
        P("module", "Engine", "01", "Engine control unit"),
        P("module", "ABS", "03", "Brakes"),
        P("action", "Scan all modules"),
        P("action", "Clear all fault memories"),
    ]


def _titles(res):
    return [h.item.title for h in res.hits]


def test_all_tokens_must_match_case_insensitively():
    res = search(_items(), "BOOST Act")
    assert _titles(res) == ["Boost pressure, actual", "Actuator test: boost pressure control valve N75"] \
        or set(_titles(res)) == {"Boost pressure, actual", "Actuator test: boost pressure control valve N75"}
    assert not search(_items(), "boost zzzz").hits


def test_matches_subtitle_and_keywords():
    assert _titles(search(_items(), "Ldm_pIst")) == ["Boost pressure, actual"]
    assert _titles(search(_items(), "group 011 cell 2")) == ["Boost pressure, specified"]
    assert "Boost pressure, actual" in _titles(search(_items(), "boost ldm"))


def test_word_start_beats_mid_word():
    items = [P("live", "Subboost level"), P("live", "Level of boost"), P("live", "Boost level")]
    assert _titles(search(items, "boost")) == ["Boost level", "Level of boost", "Subboost level"]


def test_title_beats_subtitle_beats_nothing():
    items = [P("live", "Something else", subtitle="boost related"), P("live", "Boost thing")]
    assert _titles(search(items, "boost")) == ["Boost thing", "Something else"]


def test_shorter_title_first_for_equal_matches():
    items = [P("live", "Boost pressure actual value long"), P("live", "Boost pressure")]
    assert _titles(search(items, "boost")) == ["Boost pressure", "Boost pressure actual value long"]


def test_ties_keep_input_order():
    items = [P("live", "Boost A"), P("live", "Boost B"), P("live", "Boost C")]
    assert _titles(search(items, "boost")) == ["Boost A", "Boost B", "Boost C"]


def test_spans_mark_the_title_matches():
    res = search(_items(), "boost act")
    hit = next(h for h in res.hits if h.item.title == "Boost pressure, actual")
    assert hit.spans == [(0, 5), (16, 19)]
    # tokens in the subtitle only leave no title span
    assert search(_items(), "Ldm_pIst").hits[0].spans == []


def test_adjacent_and_overlapping_spans_merge():
    hit = search([P("live", "Boost pressure")], "boost pre pres").hits[0]
    assert hit.spans == [(0, 5), (6, 10)]
    hit = search([P("live", "abcdef")], "abc cde").hits[0]
    assert hit.spans == [(0, 5)]
    hit = search([P("live", "abcdef")], "abc def").hits[0]
    assert hit.spans == [(0, 6)]


def test_module_prefix_restricts_to_that_module():
    res = search(_items(), "03: ")
    assert {h.item.addr for h in res.hits} == {"03"}
    assert {"ABS", "Vehicle speed"} <= set(_titles(res))
    res = search(_items(), "01: boost")
    assert res.hits and all(h.item.addr == "01" for h in res.hits)
    assert "Engine" not in _titles(res)                         # does not mention boost
    assert "Engine" in _titles(search(_items(), "01:"))         # the module itself
    assert search(_items(), "01:", None).counts["module"] == 1


def test_module_prefix_is_hex_and_case_insensitive():
    items = [P("module", "Cluster", "17"), P("module", "Gateway", "1F"), P("live", "x", "1f")]
    assert set(_titles(search(items, "1f:"))) == {"Gateway", "x"}
    assert set(_titles(search(items, "1F: gate"))) == {"Gateway"}


def test_action_prefix_shows_only_actions():
    res = search(_items(), ">")
    assert set(_titles(res)) == {"Scan all modules", "Clear all fault memories"}
    assert _titles(search(_items(), "> scan")) == ["Scan all modules"]
    assert not search(_items(), "> boost").hits
    assert search(_items(), ">scan").counts == {"all": 1, "action": 1}


def test_fault_code_forms():
    items = _items()
    for q in ("P0299", "p0299", "008857", "8857", "0x2299", "0x0299"):
        res = search(items, q)
        assert [h.item.title for h in res.hits if h.item.kind == "fault"] == \
            ["Boost pressure regulation: control range not reached"], q
    assert [h.item.title for h in search(items, "U0121").hits][:1] == ["Lost communication with ABS control module"]
    assert [h.item.title for h in search(items, "0x4121").hits][:1] == ["Lost communication with ABS control module"]
    assert search(items, "000565").hits[0].item.title == "Boost pressure sensor: implausible signal"


def test_fault_code_in_the_title_text_also_matches():
    items = [P("fault", "P0299 · 008857 Boost pressure regulation", "01")]
    assert search(items, "p0299").hits
    assert search(items, "8857").hits
    assert search(items, "0x2299").hits


def test_code_does_not_match_unrelated_faults():
    res = search(_items(), "P0300")
    assert not [h for h in res.hits if h.item.kind == "fault"]


def test_empty_query_is_modules_actions_procedures():
    res = search(_items(), "")
    assert {h.item.kind for h in res.hits} == {"module", "action", "procedure"}
    assert len(res.hits) == 5
    assert search(_items(), "   ").counts == res.counts
    assert res.counts["all"] == 5


def test_counts_cover_all_matches_regardless_of_kind_filter():
    items = _items()
    every = search(items, "boost")
    only = search(items, "boost", "fault")
    assert every.counts == only.counts
    assert every.counts["all"] == 8 and every.counts["live"] == 3 and every.counts["fault"] == 2
    assert {h.item.kind for h in only.hits} == {"fault"}
    assert len(only.hits) == 2
    assert search(items, "boost", "all").counts == every.counts
    assert len(search(items, "boost", "all").hits) == 8


def test_kind_filter_without_matches_is_empty_but_counted():
    res = search(_items(), "boost", "module")
    assert res.hits == [] and res.counts["all"] == 8


def test_unicode_and_case():
    items = [P("live", "Kühlmitteltemperatur", "01", "Motor", "Température"), P("live", "İstanbul"), P("live", "Straße")]
    assert _titles(search(items, "KÜHL")) == ["Kühlmitteltemperatur"]
    assert _titles(search(items, "temperature")) == []
    assert _titles(search(items, "TEMPÉRATURE")) == ["Kühlmitteltemperatur"]
    res = search(items, "stanbul")                  # lower() of the capital I-dot grows: spans must still index the title
    assert _titles(res) == ["İstanbul"] and res.hits[0].spans == [(1, 8)]
    assert _titles(search(items, "straße")) == ["Straße"]


def test_default_targets_and_groups():
    assert P("actuator", "x").target_text() == "Actuator tests >"
    assert P("live", "x", target="Custom >").target_text() == "Custom >"
    assert P("action", "x").target_text() == ""
    kinds = [k for _g, ks in PI.GROUPS for k in ks]
    assert sorted(kinds) == sorted(PI.KINDS)


def test_performance_5000_items():
    items = [P("live", "Measuring value %d boost pressure" % i, "%02X" % (i % 64), "Engine group %03d cell %d" % (i % 255, i % 8),
               "sig_%d" % i) for i in range(5000)]
    search(items, "boost")                          # warm the per-item cache
    best = 1.0
    for q in ("boost pre", "sig_4999", "zzz", "0x2299", "01: pressure"):
        t = time.perf_counter()
        search(items, q)
        best = min(best, time.perf_counter() - t)
    t = time.perf_counter()
    search(items, "pressure val")
    warm = time.perf_counter() - t
    assert warm < 0.05, warm
    t = time.perf_counter()
    search([P(i.kind, i.title, i.addr, i.subtitle, i.keywords) for i in items], "boost pre")     # cold: new objects
    assert time.perf_counter() - t < 0.2


def test_items_are_not_mutated_and_cache_follows_object_identity():
    a = P("live", "Boost")
    assert search([a], "boost").hits
    b = P("live", "Coolant")
    res = search([b], "boost")
    assert not res.hits
    assert a.title == "Boost"
