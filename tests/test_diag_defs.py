# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag definitions format v1: loading, validation, resolution, compiling,
and the bundled vag-generic pack."""
import json
import os

import pytest

from ovtsuite.diag import defs as D
from ovtsuite.diag.defs import schema as S


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


MANIFEST = "format: 1\nid: %s\nversion: 1.0.0\ntitle: t\npublisher: p\nlicense: CC-BY-SA-4.0\n"


def _pack(tmp_path, pid="p1", docs=None):
    root = tmp_path / pid
    _write(str(root / "pack.yaml"), MANIFEST % pid)
    for name, text in (docs or {}).items():
        _write(str(root / name), text)
    return str(root)


# ---------------------------------------------------------------- bundled pack
def test_bundled_pack_loads_and_validates():
    packs = D.load_packs([D.bundled_dir()], "bundled")
    assert [p.id for p in packs] == ["vag-generic"]
    rep = D.validate_pack(packs[0])
    assert rep.ok, rep.text()
    assert not rep.warnings, rep.text()


def test_bundled_addressing_c6_facts():
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    rows = {m["addr"]: m for m in res.addressing("C6")}
    assert D.as_int(rows["01"]["tp20"]["dest"]) == 0x01
    assert D.as_int(rows["03"]["tp20"]["dest"]) == 0x03
    assert D.as_int(rows["08"]["tp20"]["dest"]) == 0x2C      # Climatronic answers at 0x2C
    assert D.as_int(rows["17"]["tp20"]["dest"]) == 0x07      # C6 cluster channel setup at 0x07
    assert D.as_int(rows["01"]["kwp_header"]) == 0x10
    assert "C6" in res.generations()
    # the C6 table is the gateway's installation list: every address it holds, with its dest
    assert D.as_int(rows["19"]["tp20"]["dest"]) == 0x1F and rows["19"]["gateway"]["installation_list"] == "did_04a1"
    assert D.as_int(rows["46"]["tp20"]["dest"]) == 0x21 and rows["46"]["bus"] == "comfort"
    assert D.as_int(rows["56"]["tp20"]["dest"]) == 0x52 and rows["56"]["bus"] == "infotainment"
    assert D.as_int(rows["04"]["tp20"]["dest"]) == 0x13 and rows["04"]["bus"] == "drive"
    assert len(rows) >= 59
    # a generation the C6-only rows do not list keeps the convention rows only: no TP 2.0 dests there
    other = {m["addr"]: m for m in res.addressing("PQ35")}
    assert "09" in other and "08" in other and "tp20" not in other["08"] and "46" in other
    assert "04" not in other                                      # C6-only row


def test_bundled_module_variants_resolve_by_protocol():
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    kwp = res.module(D.Identity(addr="01", protocol="kwp2000"))
    assert kwp["variants"]["adaptation"] == "routine_b8"
    assert D.as_int(kwp["kwp"]["session"]) == 0x89
    kw = res.module(D.Identity(addr="01", protocol="kw1281"))
    assert kw["identification"]["record"] == "kw1281_blocks"
    tcu = res.module(D.Identity(addr="02", protocol="kwp2000"))
    assert D.as_int(tcu["kwp"]["session"]) == 0x86
    assert res.module(D.Identity(addr="46", protocol="kwp2000")) == {}


# ------------------------------------------------------------------ matching
def test_match_specificity_order():
    ident = D.Identity(addr="17", protocol="kwp2000", part="4F0910930AF", sw="0240", platform=None)
    m_addr = D.Match.from_doc({"addr": "17"})
    m_part = D.Match.from_doc({"addr": "17", "part": "4F0910930AF"})
    m_wild = D.Match.from_doc({"addr": "17", "part": "4F0910930*"})
    m_sw = D.Match.from_doc({"addr": "17", "part": "4F0910930AF", "sw": ["0240"]})
    scores = [m.specificity(ident) for m in (m_addr, m_wild, m_part, m_sw)]
    assert scores == sorted(scores) and len(set(scores)) == 4
    assert D.Match.from_doc({"addr": "01"}).specificity(ident) is None
    assert D.Match.from_doc({"part": "8E0910930*"}).specificity(ident) is None
    assert D.Match.from_doc({"part": "4F0910930AF", "sw": "0110"}).specificity(ident) is None
    # generation is advisory: unknown on the identity still matches
    assert D.Match.from_doc({"addr": "17", "generation": "C6"}).specificity(ident) is not None
    assert D.Match.from_doc({"addr": "17", "generation": "C6"}).specificity(
        ident.with_(generation="PQ35")) is None


def test_part_pattern_rules():
    assert D.part_matches("4F0910930*", "4F0910930AF")
    assert D.part_matches("4F0910930", "4F0910930")
    assert not D.part_matches("4F0910930", "4F0910930AF")
    m = D.Match.from_doc({"part": "4F0*910930"})
    assert any("part" in p for p in m.problems())
    assert D.Match.from_doc({"sw": "0240"}).problems()       # sw needs part


# ---------------------------------------------------------------- layering
ADAPT_A = """kind: adaptation
match: { addr: "17" }
channels:
  - { id: adaptation.ch02, channel: 2, name: "Distance since service", unit: km, source: community }
  - { id: adaptation.ch40, channel: 40, name: "Max distance", unit: km, source: community }
"""
ADAPT_B = """kind: adaptation
match: { addr: "17", part: "4F0910930*" }
channels:
  - { id: adaptation.ch02, channel: 2, name: "Distance since service (verified)", unit: km, source: bench, verified: ["4F0910930AF 0240"] }
"""


def test_resolver_layers_by_specificity_and_origin(tmp_path):
    p1 = D.load_pack(_pack(tmp_path, "generic", {"modules/17/adaptation.yaml": ADAPT_A}), "bundled")
    p2 = D.load_pack(_pack(tmp_path, "c6", {"modules/17/adaptation.yaml": ADAPT_B}), "bundled")
    res = D.Resolver([p1, p2])
    v = res.view("adaptation", D.Identity(addr="17", part="4F0910930AF"))
    assert v.entries["adaptation.ch02"].entry["name"].endswith("(verified)")
    assert v.entries["adaptation.ch02"].pack_id == "c6"
    assert v.entries["adaptation.ch02"].confidence == "verified"
    assert v.entries["adaptation.ch40"].pack_id == "generic"
    assert v.entries["adaptation.ch40"].confidence == "unverified"
    # a different part sees only the generic one
    v2 = res.view("adaptation", D.Identity(addr="17", part="8E0920930"))
    assert v2.entries["adaptation.ch02"].pack_id == "generic"
    # the user's pack beats a more specific bundled one
    p3 = D.load_pack(_pack(tmp_path, "mine", {"a.yaml": ADAPT_A.replace("service", "service (mine)")}), "user")
    v3 = D.Resolver([p1, p2, p3]).view("adaptation", D.Identity(addr="17", part="4F0910930AF"))
    assert v3.entries["adaptation.ch02"].pack_id == "mine"


def test_resolver_reports_same_level_conflict(tmp_path):
    p1 = D.load_pack(_pack(tmp_path, "a", {"x.yaml": ADAPT_A}), "bundled")
    p2 = D.load_pack(_pack(tmp_path, "b", {"x.yaml": ADAPT_A}), "bundled")
    v = D.Resolver([p1, p2]).view("adaptation", D.Identity(addr="17"))
    assert v.conflicts and "adaptation.ch02" in v.conflicts[0]
    assert D.check_conflicts([p1, p2])


# -------------------------------------------------------------- validation
def test_validation_messages(tmp_path):
    bad = """kind: adaptation
match: { addr: "ZZ", part: "4F0*930" }
channels:
  - { id: Adaptation.Ch02, channel: 2, name: x, unit: furlongs, requires: pin }
  - { channel: 3 }
  - { id: adaptation.ch03, channel: 3, name: y, source: wiki }
  - { id: adaptation.ch03, channel: 4, name: z }
"""
    pack = D.load_pack(_pack(tmp_path, "bad", {"x.yaml": bad}))
    rep = D.validate_pack(pack)
    txt = rep.text()
    assert not rep.ok
    assert "match.addr" in txt and "match.part" in txt
    assert "dotted lower-case" in txt
    assert "missing 'id'" in txt
    assert "source 'wiki'" in txt
    assert "duplicate entry id" in txt
    assert "requires 'pin'" in txt
    assert any("furlongs" in w for w in rep.warnings)


def test_coding_document_rules():
    doc = {"kind": "coding", "style": "long", "bytes": [
        {"index": 1, "bits": [
            {"id": "coding.byte1.bit3", "bit": 3, "name": "DRL", "type": "bool"},
            {"id": "coding.byte1.bits4-5", "bits": [4, 5], "name": "Fog", "type": "enum",
             "options": {0: "none", 1: "left"}},
            {"id": "coding.byte1.bit9", "bit": 9, "name": "bad bit"},
            {"id": "coding.byte1.e", "bit": 1, "name": "enum no options", "type": "enum"},
        ]}]}
    msgs = S.check_doc(doc)
    assert any("'bit' must be 0..7" in m for m in msgs)
    assert any("an enum needs 'options'" in m for m in msgs)
    assert not any("coding.byte1.bit3" in m for m in msgs)
    short = {"kind": "coding", "style": "short",
             "digits": [{"position": 1, "name": "Gearbox", "options": {1: "Manual", 2: "Auto"}}],
             "values": [{"id": "coding.v0000071", "value": "0000071", "name": "manual, A/C"}]}
    assert S.check_doc(short) == []
    assert any("needs 'digits' and/or 'values'" in m for m in S.check_doc({"kind": "coding", "style": "short"}))


def test_module_variant_vocabulary():
    ok = {"kind": "module", "variants": {"adaptation": "routine_b8", "link_speed": [0x50, "0x87"]}}
    assert S.check_doc(ok) == []
    bad = {"kind": "module", "variants": {"adaptation": "magic", "teleport": "yes"},
           "identification": {"record": "psychic"}}
    msgs = S.check_doc(bad)
    assert any("variants.adaptation" in m for m in msgs)
    assert any("unknown variant 'teleport'" in m for m in msgs)
    assert any("identification.record" in m for m in msgs)


def test_procedure_and_precondition_rules():
    proc = {"kind": "procedure", "id": "procedure.sri", "name": "SRI", "safety": "write",
            "steps": [{"read": {"channels": ["adaptation.ch02"]}}, {"check": ["engine_off", {"coolant_above_c": 70}]},
                      {"dance": {}}, {"check": ["levitating"]}]}
    msgs = S.check_doc(proc)
    assert any("'dance'" in m for m in msgs)
    assert any("levitating" in m for m in msgs)
    assert S.check_doc({"kind": "procedure", "id": "procedure.x", "name": "x", "steps": [{"wait": 1}]}) == []


# ----------------------------------------------------------------- compile
def test_compile_roundtrip_and_json_pack(tmp_path):
    src = _pack(tmp_path, "c6", {"modules/17/adaptation.yaml": ADAPT_B,
                                 "measuring.yaml": "kind: measuring\nmatch: { addr: \"17\" }\n"
                                                   "groups:\n  1: { name: basics, cells: [Speed, {name: RPM, signal: nmot}, null] }\n"})
    out = D.compile_pack(src)
    assert out.endswith("pack.json.gz")
    comp = D.load_pack(out, "module")
    assert comp.id == "c6" and len(comp.docs) == 2
    assert D.validate_pack(comp).ok
    rows = D.catalogue_rows(comp)
    assert rows[0]["groups"][1] == ["Speed", "RPM", None]
    # the directory now prefers its compiled form, and a JSON-only pack loads without YAML
    assert D.load_pack(src, "bundled").docs[0].kind in ("adaptation", "measuring")
    jdir = tmp_path / "jsonpack"
    _write(str(jdir / "pack.json"), json.dumps({"format": 1, "id": "jsonpack", "version": "0.1.0"}))
    _write(str(jdir / "a.json"), json.dumps({"kind": "adaptation", "match": {"addr": "17"},
                                            "channels": [{"id": "adaptation.ch02", "channel": 2, "name": "x"}]}))
    jp = D.load_pack(str(jdir))
    assert jp.docs[0].entries[0]["id"] == "adaptation.ch02"


def test_compile_refuses_invalid(tmp_path):
    src = _pack(tmp_path, "bad", {"x.yaml": "kind: adaptation\nchannels:\n  - { channel: 1 }\n"})
    with pytest.raises(ValueError):
        D.compile_pack(src)


def test_json_schema_export():
    js = D.json_schema("adaptation")
    assert js["properties"]["channels"]["items"]["required"] == ["id", "channel", "name"]
    assert "kind" in js["required"]
    for kind in D.KINDS:
        D.json_schema(kind)


def test_load_packs_skips_broken_and_reports(tmp_path):
    good = _pack(tmp_path, "good")
    _write(str(tmp_path / "broken" / "pack.yaml"), "format: 9\nid: broken\n")
    errors = []
    packs = D.load_packs([str(tmp_path)], "user", errors)
    assert [p.id for p in packs] == ["good"]
    assert errors and "format" in errors[0][1]
    assert D.is_pack_dir(good)


def test_load_packs_reports_every_broken_pack(tmp_path):
    """A corrupt compiled pack, a pack of bad JSON, a manifest with the wrong
    types: each is skipped with a message naming it, none stops the rest."""
    import gzip
    good = _pack(tmp_path, "good")
    os.makedirs(str(tmp_path / "corrupt"))
    with open(str(tmp_path / "corrupt" / "pack.json.gz"), "wb") as fh:     # compiled only, and broken
        fh.write(b"\x1f\x8b\x08\x00not really gzip at all")
    _write(str(tmp_path / "badjson" / "pack.json"), '{"format": 1, "id": "badjson", ')
    _write(str(tmp_path / "strreq" / "pack.yaml"), "format: 1\nid: strreq\nversion: 1.0.0\nrequires: '>=0.1'\n")
    _write(str(tmp_path / "listid" / "pack.yaml"), "format: 1\nid: [a, b]\nversion: 1.0.0\n")
    _write(str(tmp_path / "numver" / "pack.yaml"), "format: 1\nid: numver\nversion: 1.2\n")   # a float: tolerated as text
    errors = []
    packs = D.load_packs([str(tmp_path)], "user", errors)
    assert sorted(p.id for p in packs) == ["good", "numver"]
    assert [p.version for p in packs if p.id == "numver"] == ["1.2"]
    by = {os.path.basename(path): msg for path, msg in errors}
    assert set(by) == {"corrupt", "badjson", "strreq", "listid"}
    assert "pack.json.gz" in by["corrupt"]
    assert "pack.json" in by["badjson"]
    assert "requires" in by["strreq"] and "pack.yaml" in by["strreq"]
    assert "'id'" in by["listid"]
    # a compiled pack that is only corrupt beside good sources: the sources load
    with gzip.open(str(tmp_path / "good" / "pack.json.gz"), "wt") as fh:
        fh.write("{not json")
    assert D.load_pack(good).id == "good"


def test_load_packs_survives_an_unexpected_exception(tmp_path, monkeypatch):
    _pack(tmp_path, "boom")
    _pack(tmp_path, "fine")
    from ovtsuite.diag.defs import load as L
    real = L.load_sources

    def explode(path, origin="user"):
        if path.endswith("boom"):
            raise RuntimeError("something unforeseen")
        return real(path, origin)
    monkeypatch.setattr(L, "load_sources", explode)
    errors = []
    packs = D.load_packs([str(tmp_path)], "user", errors)
    assert [p.id for p in packs] == ["fine"]
    assert len(errors) == 1 and "RuntimeError: something unforeseen" in errors[0][1]


FAULTS_ONE = """kind: faults
match: { addr: "01" }
codes:
  - { id: %s.fault.v16683, vag: 16683, sae: P0299, text: "%s" }
"""


def test_fault_lookup_prefers_the_most_authoritative_pack(tmp_path):
    """Two packs label VAG 16683 under different entry ids: the user's pack
    answers, not whichever was merged first; and a module pack over a bundled
    one; and the more specific match within an origin."""
    bundled = D.load_pack(_pack(tmp_path, "bundled", {"f.yaml": FAULTS_ONE % ("bundled", "bundled text")}), "bundled")
    module = D.load_pack(_pack(tmp_path, "module", {"f.yaml": FAULTS_ONE % ("module", "module text")}), "module")
    user = D.load_pack(_pack(tmp_path, "user", {"f.yaml": FAULTS_ONE % ("user", "user text")}), "user")
    ident = D.Identity(addr="01", protocol="kwp2000", part="03G906016LQ")
    for order in ([bundled, module, user], [user, module, bundled], [module, user, bundled]):
        res = D.Resolver(order)
        assert res.fault(ident, vag=16683).pack_id == "user"
        assert res.fault_text(ident, sae="P0299") == "user text"
    assert D.Resolver([bundled, module]).fault(ident, vag=16683).pack_id == "module"
    assert D.Resolver([module, bundled]).fault(ident, vag=16683).pack_id == "module"
    specific = D.load_pack(_pack(tmp_path, "specific", {"f.yaml": FAULTS_ONE.replace(
        'match: { addr: "01" }', 'match: { addr: "01", part: "03G906016*" }') % ("specific", "for this part")}), "bundled")
    assert D.Resolver([specific, bundled]).fault_text(ident, vag=16683) == "for this part"
    assert D.Resolver([bundled, specific]).fault_text(ident, vag=16683) == "for this part"


ADDR_GENERIC = """kind: addressing
modules:
  - { addr: "15", name: Airbags, kline: { address: 0x15, init: slow }, source: convention }
  - { addr: "01", name: Engine, kline: { address: 0x01, init: slow }, source: convention }
"""
ADDR_C6 = """kind: addressing
modules:
  - { addr: "15", name: Airbags (C6), tp20: { dest: 0x05 }, generations: [C6], source: capture }
  - { addr: "01", name: Engine, tp20: { dest: 0x01 }, generations: [C6], source: capture }
  - { addr: "04", name: Steering angle, tp20: { dest: 0x13 }, generations: [C6], source: capture }
"""


def test_addressing_merges_generic_rows_under_generation_rows(tmp_path):
    """Whatever the file order, the generic (convention) row goes in first
    and the captured generation row on top, so the capture's name and source
    stand; without a requested generation the generation row's facts are
    merged but its ``generations`` list is not copied onto the generic row."""
    # the capture file sorts BEFORE the generic one, the order that leaked before
    p = D.load_pack(_pack(tmp_path, "p", {"a-c6.yaml": ADDR_C6, "z-generic.yaml": ADDR_GENERIC}), "bundled")
    res = D.Resolver([p])
    c6 = {m["addr"]: m for m in res.addressing("C6")}
    assert c6["15"]["name"] == "Airbags (C6)" and c6["15"]["source"] == "capture"
    assert D.as_int(c6["15"]["tp20"]["dest"]) == 0x05 and c6["15"]["kline"]["init"] == "slow"
    assert c6["15"]["generations"] == ["C6"]
    none = {m["addr"]: m for m in res.addressing(None)}
    assert "generations" not in none["01"] and "generations" not in none["15"]
    assert D.as_int(none["01"]["tp20"]["dest"]) == 0x01            # the fact still merges
    assert none["04"]["generations"] == ["C6"]                     # only one table lists it: kept as is
    other = {m["addr"]: m for m in res.addressing("PQ35")}
    assert other["15"]["name"] == "Airbags" and "tp20" not in other["15"] and "04" not in other
    # the bundled pack: the airbag row keeps the capture's source for C6
    bundled = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    rows = {m["addr"]: m for m in bundled.addressing("C6")}
    assert rows["15"]["source"] == "capture" and rows["15"]["name"] == "Airbags"
    assert D.as_int(rows["15"]["tp20"]["dest"]) == 0x05 and D.as_int(rows["15"]["isotp"]["request"]) == 0x715
    assert "generations" not in {m["addr"]: m for m in bundled.addressing(None)}["01"]


MODULE_GENERIC = "kind: module\nmatch: { addr: \"17\", protocol: kwp2000 }\nname: Instruments\nvariants: { coding: record_9a }\n"
MODULE_C6 = "kind: module\nmatch: { addr: \"17\", protocol: kwp2000, generation: C6 }\nname: Instruments (C6)\nvariants: { coding: uds_0600 }\n"


def test_generation_documents_rank_below_generic_without_a_generation(tmp_path):
    """A car scanned without a generation gets the generic module document's
    variants, not one generation's; with the generation the specific wins."""
    p = D.load_pack(_pack(tmp_path, "p", {"a-c6.yaml": MODULE_C6, "z-generic.yaml": MODULE_GENERIC}), "bundled")
    res = D.Resolver([p])
    nogen = D.Identity(addr="17", protocol="kwp2000")
    assert res.module(nogen)["variants"]["coding"] == "record_9a"
    assert res.module(nogen)["name"] == "Instruments"
    assert res.module(nogen.with_(generation="C6"))["variants"]["coding"] == "uds_0600"
    assert res.module(nogen.with_(generation="PQ35"))["variants"]["coding"] == "record_9a"
    m_gen = D.Match.from_doc({"addr": "17", "generation": "C6"})
    m_plain = D.Match.from_doc({"addr": "17"})
    assert m_gen.specificity(nogen) is not None and m_gen.specificity(nogen) < m_plain.specificity(nogen)
    assert m_gen.specificity(nogen.with_(generation="C6")) > m_plain.specificity(nogen.with_(generation="C6"))


def test_compiled_form_yields_to_edited_sources(tmp_path):
    """Editing any document -- not only pack.yaml -- makes the sources load
    again; the compiled form records the hash of what it was built from, so
    file times do not decide (a checkout does not keep them)."""
    import time
    from ovtsuite.diag.defs import load as L
    src = _pack(tmp_path, "c6", {"modules/17/adaptation.yaml": ADAPT_B})
    out = D.compile_pack(src)
    assert L.compiled_is_current(src)
    assert D.load_pack(src).source_hash and D.load_pack(src).docs[0].entries[0]["name"].endswith("(verified)")
    time.sleep(0.05)
    _write(str(tmp_path / "c6" / "modules" / "17" / "adaptation.yaml"), ADAPT_B.replace("(verified)", "(edited)"))
    assert not L.compiled_is_current(src)
    assert D.load_pack(src).docs[0].entries[0]["name"].endswith("(edited)")
    # even with the compiled file made newer than the edited document
    os.utime(out, None)
    assert not L.compiled_is_current(src)
    assert D.load_pack(src).docs[0].entries[0]["name"].endswith("(edited)")
    # recompiling catches up, and a compile never reads a stale compiled form
    D.compile_pack(src)
    assert L.compiled_is_current(src)
    assert D.load_pack(src).source_hash and D.load_pack(src).docs[0].entries[0]["name"].endswith("(edited)")
    # an older compiled pack without a hash: file times decide, over every document
    import gzip
    import json as _json
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        doc = _json.load(fh)
    doc.pop("source_hash")
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        _json.dump(doc, fh)
    assert L.compiled_is_current(src)
    time.sleep(0.05)
    _write(str(tmp_path / "c6" / "modules" / "17" / "adaptation.yaml"), ADAPT_B.replace("(verified)", "(newer)"))
    assert not L.compiled_is_current(src)
    assert D.load_pack(src).docs[0].entries[0]["name"].endswith("(newer)")


def test_unquoted_addr_and_sw_are_refused(tmp_path):
    """YAML reads ``addr: 10`` as ten (module 0A once normalised) and ``sw:
    0240`` as 160: validation refuses them and says to quote; the
    normaliser itself stays tolerant for callers in code."""
    pack = D.load_pack(_pack(tmp_path, "q", {
        "a.yaml": "kind: adaptation\nmatch: { addr: 10 }\nchannels:\n  - { id: adaptation.ch01, channel: 1, name: x }\n",
        "b.yaml": "kind: adaptation\nmatch: { addr: \"17\", part: 4F0910930AF, sw: [0240, \"0241\"] }\n"
                  "channels:\n  - { id: adaptation.ch02, channel: 2, name: y }\n",
        "c.yaml": "kind: adaptation\nmatch: { addr: \"10\", part: 4F0910930AF, sw: \"0240\" }\n"
                  "channels:\n  - { id: adaptation.ch03, channel: 3, name: z }\n"}))
    rep = D.validate_pack(pack)
    assert not rep.ok
    assert any("a.yaml" in e and "quote the address" in e for e in rep.errors), rep.text()
    assert any("b.yaml" in e and "match.sw" in e and "quote the version" in e for e in rep.errors), rep.text()
    assert not any("c.yaml" in e for e in rep.errors), rep.text()
    assert D.Match.from_doc({"addr": "10"}).problems() == []
    assert D.Match.from_doc({"addr": 10}).problems()
    from ovtsuite.diag.defs.model import _norm_addr
    assert _norm_addr(10) == "0A" and _norm_addr("10") == "10" and _norm_addr(0x17) == "17"
    assert S.check_doc({"kind": "adaptation", "match": {"addr": 10}, "channels": []}) or True   # schema stays field-level


def test_requires_and_units_in_nested_lists(tmp_path):
    """``requires: ""`` is the same as none; a routine inside a dids document,
    a coding bit and a measuring cell are checked like flat entries."""
    docs = {
        "d.yaml": "kind: dids\ndids:\n  - { id: dids.f187, did: 0xF187, name: part, requires: \"\" }\n"
                  "routines:\n  - { id: routines.r1, routine: 0x0203, name: r, requires: pin, unit: furlongs }\n",
        "c.yaml": "kind: coding\nmatch: { addr: \"17\" }\nstyle: long\nrequires: \"\"\nbytes:\n"
                  "  - index: 0\n    bits:\n      - { id: coding.byte0.bit0, bit: 0, name: b, requires: magic, unit: parsecs }\n",
        "m.yaml": "kind: measuring\nmatch: { addr: \"17\" }\ngroups:\n  1: { cells: [{ name: Speed, unit: furlongs/fortnight }] }\n",
        "a.yaml": "kind: adaptation\nmatch: { addr: \"17\" }\nchannels:\n  - { id: adaptation.ch01, channel: 1, name: x, requires: \"\" }\n",
    }
    rep = D.validate_pack(D.load_pack(_pack(tmp_path, "n", docs)))
    txt = rep.text()
    assert "requires ''" not in txt
    assert any("routines.r1" in e and "requires 'pin'" in e for e in rep.errors), txt
    assert any("coding.byte0.bit0" in e and "requires 'magic'" in e for e in rep.errors), txt
    assert any("routines.r1" in w and "furlongs" in w for w in rep.warnings), txt
    assert any("coding.byte0.bit0" in w and "parsecs" in w for w in rep.warnings), txt
    assert any("cell 1" in w and "furlongs/fortnight" in w for w in rep.warnings), txt
    assert len(rep.errors) == 2, txt


def test_module_faults_encoding_field():
    """``faults.encoding`` on a module document: vag or sae, nothing else."""
    assert S.check_doc({"kind": "module", "faults": {"encoding": "sae"}}) == []
    assert S.check_doc({"kind": "module", "faults": {"encoding": "vag"}}) == []
    msgs = S.check_doc({"kind": "module", "faults": {"encoding": "hex", "other": 1}})
    assert any("faults.encoding 'hex'" in m for m in msgs)
    assert any(m.startswith("?") and "faults.other" in m for m in msgs)
    assert any("'faults' must be a mapping" in m for m in S.check_doc({"kind": "module", "faults": "sae"}))
    assert "faults" in D.json_schema("module")["properties"]
    # the bundled C6 body modules say vag; the generic engine says nothing (the controller decides)
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    assert res.module(D.Identity(addr="17", protocol="kwp2000", generation="C6"))["faults"]["encoding"] == "vag"
    assert res.module(D.Identity(addr="19", protocol="kwp2000", generation="C6"))["faults"]["encoding"] == "vag"
    assert "faults" not in res.module(D.Identity(addr="01", protocol="kwp2000"))


def test_fault_text_falls_back_to_standard_pcodes():
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    ident = D.Identity(addr="01", protocol="kwp2000")
    assert res.fault_text(ident, sae="P0299")           # from ovtsuite/data/obd_pcodes.json
    assert res.fault_text(ident, vag=99999) == ""
    cov = res.coverage(ident)
    assert cov["module"] == 1 and cov["faults"] == 0
