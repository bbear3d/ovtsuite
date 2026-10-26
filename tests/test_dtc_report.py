# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fault-code reads as records (ovtsuite.dtc_report): status decoding, text
and CSV, history and new / gone, web search and AI questions, and reads kept
in a project (save / load, disksync merge)."""

import csv
import io
import urllib.parse

from ovtsuite import dtc_report as R
from ovtsuite import project as ovt
from ovtsuite.flash.diag import Dtc
from ovtsuite.project import disksync as _ds


def _paths(code):
    return {16485: [R.PathRef("Dfp_AirFlwSens", True)],
            17964: [R.PathRef("Dfp_BPACD", False), R.PathRef("Dfp_BPACD2", True)]}.get(code, [])


def _read(codes, **kw):
    return R.build_read(codes, paths_for=_paths, controller="EDC16",
                        ecu_ident="038906016AB 1.9l R4 EDC G000SG 1234",
                        cable="OpenPort 2.0", transport="K-line", file="car.bin", **kw)


# ------------------------------------------------------------- status
def test_status_byte_decoding():
    """KW1281 (the default for a bare status): fault type in bits 0-6,
    sporadic flag in bit 7."""
    assert R.fault_type(0x07) == "07 - signal too low"
    assert R.fault_type(0x9B) == "27 - implausible signal"
    assert R.fault_type(0x1F) == "31 - open or short to ground"
    assert R.fault_type(0x7E) == "126 - fault type 126"      # not in the table
    assert R.intermittent(0x87) and not R.intermittent(0x07)
    c = R.CodeEntry(16485, 0x87)
    assert c.status_code == "07-10" and c.status_code == Dtc(16485, 0x87).text[-5:]
    assert R.status_text(0x87) == "07-10 signal too low, intermittent"


def test_kw1281_fault_types_follow_vag_list():
    """The words come from VAG-COM's "Fehlerarten" list (German original)."""
    t = R.KW1281_FAULT_TYPES
    assert (t[8], t[18], t[19], t[49], t[57], t[67]) == (
        "control limit exceeded", "upper stop value", "lower stop value",
        "no communication", "electrical fault in circuit", "target value not reached")
    assert 0 not in t and 64 not in t and max(t) == 82       # 192-194 need 8 bits
    assert R.FAULT_TYPES is t
    assert R.fault_type(35) == "35 - -"


def test_protocol_comes_from_the_code_then_the_controller():
    assert R.protocol_for("EDC15") == R.PROTO_KW1281
    assert R.protocol_for("EDC16") == R.PROTO_KWP2000
    assert R.protocol_for("ZF6HP") == R.PROTO_KWP2000
    assert R.protocol_for("") == R.PROTO_KW1281             # a bare status / old record
    assert R.protocol_for("EDC16", R.PROTO_KW1281) == R.PROTO_KW1281


def test_edc15_kw1281_status():
    """EDC15: type = the record's fbwE..T high byte, bit 7 = re-occurred."""
    si = R.decode_status(0xA3, "EDC15")
    assert (si.code, si.number, si.intermittent) == ("35-10", 35, True)
    assert "five-digit code" in si.note and "healed" in si.note
    assert R.decode_status(0x07, "EDC15").note == ""         # a type it sends
    assert "not found" in R.decode_status(0x05, "EDC15").note


def test_edc16_kwp2000_status_is_a_bit_field():
    """EDC16 18 answer: symptom in bits 0-3 (DSM_TypDfp conversion), then
    test not complete / stored / present / lamp. 0x40 (the 038997016Q log)
    is "present", not fault type 64."""
    si = R.decode_status(0x40, "EDC16")
    assert (si.code, si.fault_type, si.intermittent, si.flags) == (
        "0x40", "00 - none given", False, ("present", "not yet confirmed"))
    si = R.decode_status(0x34, "EDC16")
    assert si.intermittent and si.flags == ("not present now", "stored",
                                            "not tested since switch-on")
    assert si.fault_type == "04 - no signal (Sig)"
    assert R.decode_status(0xE8, "EDC16").fault_type == "08 - implausible signal (Npl)"
    assert "system lamp on" in R.decode_status(0xE8, "EDC16").flags
    assert R.status_text(0x62, "EDC16") == (
        "0x62 lower limit exceeded (Min), present, stored")
    # VCDS ground truth (published logs of VAG KWP2000 modules): 0x24 is
    # "004 - No Signal/Communication - Intermittent", 0x64 the same without
    assert R.intermittent(0x24, "EDC16") and not R.intermittent(0x64, "EDC16")
    # a gearbox: the same bits, the symptom only as its number (unverified)
    assert R.fault_type(0x24, "ZF6HP") == "04 - symptom 4"
    assert R.intermittent(0x24, "ZF6HP")


def test_edc16_read_of_the_u31_log():
    """Reference/Logs/flash_filtered.txt: a 038997016Q answers 18 02 FF 00
    with eight codes, all status 0x40."""
    from ovtsuite.flash.diag import _parse_dtcs, DTC_KWP2000
    body = bytes.fromhex("0840E240419740""4A344040B7404076404071404D5240461D40")
    dtcs = _parse_dtcs(body, counted=True)
    assert len(dtcs) == 8 and {d.proto for d in dtcs} == {DTC_KWP2000}
    assert dtcs[0].code == 16610 and dtcs[0].text == "16610 - status 0x40"
    r = R.build_read(dtcs, controller="EDC16")
    assert {c.status_code for c in r.codes} == {"0x40"}
    assert not any(c.intermittent for c in r.codes)
    assert r.codes[0].status_words == "present, not yet confirmed"
    # KW1281 records keep the scan-tool form
    kw = _parse_dtcs(bytes.fromhex("406587FFFF88"), counted=False)
    assert [(d.code, d.text) for d in kw] == [(16485, "16485 - 07-10")]


def test_proto_is_kept_and_old_records_fall_back_to_the_controller():
    from ovtsuite.flash.diag import DTC_KWP2000
    r = R.build_read([Dtc(16610, 0x40, DTC_KWP2000)], controller="EDC16",
                     ts="2026-10-25T10:00:00")
    d = r.to_dict()
    assert d["codes"][0]["proto"] == DTC_KWP2000
    back = R.Read.from_dict(d)
    assert back.codes[0].status_code == "0x40" and back.to_dict() == d
    del d["codes"][0]["proto"]                       # a read kept before
    assert R.Read.from_dict(d).codes[0].status_code == "0x40"
    d["controller"] = "EDC15"
    assert R.Read.from_dict(d).codes[0].status_code == "64-00"


# ------------------------------------------------------------- record
def test_build_read_uses_the_callers_lookups_then_the_generic_ones():
    r = R.build_read([Dtc(16485, 7), Dtc(19456, 0)], paths_for=_paths,
                     describe=lambda code, paths: ("P0101", "Air mass") if paths else ("", ""))
    a, b = r.codes
    assert (a.pcode, a.desc, [p.name for p in a.paths]) == ("P0101", "Air mass",
                                                            ["Dfp_AirFlwSens"])
    # no path: the P-code the VAG number stands for, and its SAE text
    assert b.pcode == "P3000" and b.paths == [] and b.paths_text() == "none in this file"
    # without a table to match against, nothing is said about paths
    r = R.build_read([Dtc(16485, 7)])
    assert r.codes[0].paths_text() == "" and r.codes[0].pcode == "P0101"
    assert r.codes[0].desc                                       # SAE text


def test_record_round_trips_through_json():
    r = _read([Dtc(16485, 0x87), Dtc(17964, 8)], before=[Dtc(16485, 7), Dtc(18010, 2)],
              action="clear", ts="2026-10-25T10:00:00")
    back = R.Read.from_json(r.to_json())
    assert back.to_dict() == r.to_dict()
    assert [c.code for c in back.still_present] == [16485]
    assert [c.code for c in back.cleared] == [18010]
    assert back.codes[1].paths[0].enabled is False


def test_text_and_csv():
    prev = _read([Dtc(16485, 7), Dtc(18010, 2)], ts="2026-10-24T09:00:00")
    cur = _read([Dtc(16485, 0x87), Dtc(17964, 8)], ts="2026-10-25T10:00:00")
    t = R.to_text(cur, prev)
    assert "Fault code report -- 2026-10-25 10:00:00" in t
    assert "Controller: EDC16 -- 038906016AB" in t and "Cable: OpenPort 2.0 (K-line)" in t
    assert "17964" in t and "[new]" in t and "intermittent" in t
    assert "Dfp_BPACD (off), Dfp_BPACD2 (on)" in t
    assert "Gone since the read before (2026-10-24 09:00:00): 18010" in t
    rows = list(csv.reader(io.StringIO(R.to_csv(cur, prev))))
    assert tuple(rows[0]) == R.CSV_HEAD
    by = {r[3]: r for r in rows[1:]}
    assert by["17964"][-1] == "new" and by["18010"][-1] == "gone"
    assert by["16485"][8] == "yes" and by["16485"][6] == "07-10"
    assert by["17964"][9] == "Dfp_BPACD=off; Dfp_BPACD2=on"
    assert "No fault codes stored." in R.to_text(_read([]))


def test_clear_header_says_what_came_back():
    r = _read([Dtc(16485, 7)], before=[Dtc(16485, 7), Dtc(17964, 8)], action="clear")
    assert "Cleared 2 code(s); still present (came straight back): 16485" in R.to_text(r)
    r = _read([], before=[Dtc(17964, 8)], action="clear")
    assert "none came back" in R.to_text(r)


def test_diff_new_and_gone():
    a = _read([Dtc(1, 0), Dtc(2, 0)])
    b = _read([Dtc(2, 0), Dtc(3, 0)])
    assert R.diff(a, b) == ({3}, {1})
    assert R.diff(None, b) == (set(), set())


# ------------------------------------------------------------- actions
def test_search_urls_per_site():
    r = _read([Dtc(16485, 7)], describe=lambda c, p: ("P0101", "Air mass"))
    es = r.codes
    part = R.part_number(r.ecu_ident)
    assert part == "038906016AB"
    q = R.search_query(es, part)
    assert q == '"16485" P0101 "Air mass" 038906016AB'
    g = R.site_url(R.search_site("google"), es, part)
    assert g.startswith("https://www.google.com/search?q=")
    assert urllib.parse.parse_qs(urllib.parse.urlparse(g).query)["q"] == [q]
    t = R.site_url(R.search_site("tdiclub"), es, part)
    assert urllib.parse.parse_qs(urllib.parse.urlparse(t).query)["q"] == [
        "site:forums.tdiclub.com " + q]
    w = R.site_url(R.search_site("rosstech"), es, part)
    assert w == "https://wiki.ross-tech.com/wiki/index.php?search=16485"
    assert R.search_site("nonsense").id == "google"
    assert {s.id for s in R.SEARCH_SITES} == {"google", "bing", "ddg", "tdiclub",
                                              "myturbodiesel", "rosstech"}
    two = R.search_query(_read([Dtc(1, 0), Dtc(2, 0)]).codes)
    assert " OR " in two


def test_ai_questions():
    r = _read([Dtc(17964, 0x88)], describe=lambda c, p: ("P0299", "Boost low"))
    q = R.ai_question(r.codes, "T1", r)
    assert "tab T1" in q and "17964" in q and "P0299" in q and "Boost low" in q
    assert "08-10" in q and "intermittent" in q
    assert "Dfp_BPACD (disabled), Dfp_BPACD2 (enabled)" in q
    assert "fault_codes" in q and "propose_fault_codes" in q and "disabling" in q
    assert "fault_codes" not in R.ai_question(r.codes, "T1", r, engine=False)
    old = _read([Dtc(17964, 8), Dtc(1, 0)], ts="2026-10-20T08:00:00")
    clr = _read([Dtc(17964, 8)], before=[Dtc(17964, 8), Dtc(1, 0)], action="clear",
                ts="2026-10-21T08:00:00")
    q = R.ai_report_question(r, [r, clr, old], "T1")
    assert "oldest first" in q and q.index("2026-10-20") < q.index("2026-10-21")
    assert "came straight back after the clear: 17964" in q
    assert "17964 (3 reads)" in q and "pattern" in q
    assert "no earlier reads" in R.ai_report_question(r, [], "T1")


# ------------------------------------------------------------- project
def _project():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    return p, s


def test_reads_kept_in_a_project_survive_save_and_load(tmp_path):
    p, s = _project()
    r1 = _read([Dtc(16485, 7)], ts="2026-10-24T09:00:00")
    r2 = _read([], ts="2026-10-25T10:00:00")
    a1 = R.keep(p, s.id, r1)
    R.keep(p, s.id, r2)
    p.add_attachment("notes.txt", b"hello", set_id=s.id)
    assert R.is_read(a1) and a1.filename == "fault-codes-20261024-090000.json"
    assert R.label(a1) == "Fault codes %s (1 code)" % a1.added.replace("T", " ")[:16]
    path = str(tmp_path / "car.ovt")
    p.save(path)
    q = ovt.Project.load(path)
    h = R.history(q, s.id)
    assert [r.ts for _a, r in h] == ["2026-10-25T10:00:00", "2026-10-24T09:00:00"]
    assert h[1][1].codes[0].code == 16485 and h[0][1].codes == []
    assert R.history(q, "other") == []


def test_reads_kept_on_both_sides_are_merged(tmp_path):
    p, s = _project()
    path = str(tmp_path / "car.ovt")
    p.save(path)
    mine = ovt.Project.load(path)
    R.keep(mine, s.id, _read([Dtc(1, 0)], ts="2026-10-25T10:00:00"))
    theirs = ovt.Project.load(path)
    R.keep(theirs, s.id, _read([Dtc(2, 0)], ts="2026-10-25T11:00:00"))
    theirs.save(path)
    res = _ds.merge(mine, ovt.Project.load(path))
    assert res.conflicts == []
    got = [r.code_numbers for _a, r in R.history(res.project, s.id)]
    assert got == [[2], [1]]


# ------------------------------------------------------------- code words
def test_sae_word_builds_write_the_code_as_vcds_does():
    """P447 / P475 EDC16s report the SAE word (0x0103 for P0103): six digits
    (VCDS's form, "000772 - Cylinder 4 / P0304"), its P-code and SAE text
    -- not P-code-less "00259"."""
    from ovtsuite.flash.diag import DTC_KWP2000
    assert R.vag_text(0x0103) == "00259"
    assert R.vag_text(0x0103, R.ENC_SAE) == "000259"
    assert R.pcode_of(0x0103, R.ENC_SAE) == "P0103" and R.pcode_of(0x0103) == ""
    assert R.pcode_of(0x4067) == "P0103"                     # the 5-digit form
    assert R.pcode_of(33476, R.ENC_SAE) == ""                # a DSM placeholder
    assert R.pcode_of(0xC100, R.ENC_SAE) == "U0100"
    assert R.vag5_of(0x0103, R.ENC_SAE) == 16487 and R.vag5_of(16487) == 16487
    seen = []

    def paths(code):
        seen.append(code)                                    # by the raw word
        return []
    r = R.build_read([Dtc(0x0103, 0x60, DTC_KWP2000)], paths_for=paths,
                     controller="EDC16", encoding=R.ENC_SAE, ts="2026-10-25T10:00:00")
    c = r.codes[0]
    assert seen == [0x0103] and c.code == 0x0103
    assert (c.vag, c.pcode, c.vag5) == ("000259", "P0103", 16487)
    assert c.desc                                            # the SAE text
    assert "000259  P0103" in R.to_text(r) and "000259" in r.summary()
    assert R.dtc_text(Dtc(0x0103, 0x60, DTC_KWP2000), R.ENC_SAE) ==         "000259 P0103 - status 0x60"
    assert Dtc(0x0103, 0x60, DTC_KWP2000, R.ENC_SAE).text == "000259 P0103 - status 0x60"
    # a kept read opened without the file still knows
    d = r.to_dict()
    assert d["encoding"] == R.ENC_SAE
    back = R.Read.from_dict(d)
    assert back.codes[0].vag == "000259" and back.to_dict() == d
    # one kept before the encoding was recorded: the 5-digit form, as before
    del d["encoding"]
    assert R.Read.from_dict(d).codes[0].vag == "00259"
    assert R.Read.from_dict(d).codes[0].pcode == "P0103"     # stored per code
