# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring-block names by ECU identity (the logger with no binary open)."""
import os

import pytest

from ovtsuite.datalog import msbcatalogue as M
from ovtsuite.datalog.served import unserved_for
from ovtsuite.flash.diag import format_identity

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SGO = os.path.join(REPO, "Reference", "EDC16-SGO-Unpacked")
EDC15 = os.path.join(REPO, "Reference", "Binaries", "EDC15")

RPM = ("Engine Speed (Eng_nAvrg)", "Eng_nAvrg", "rpm")
CLT = ("Coolant Temperature (CTSCD_tClnt)", "CTSCD_tClnt", "deg C")


def _record(part, sw, text="R4 2,0L EDC G000SG"):
    """A 1A 9B answer as the ECU sends it: part(12) SW(4) 03 coding WSC text."""
    return (b"\x9B" + part.ljust(12).encode() + sw.encode() + b"\x03"
            + b"\x00" * 9 + text.ljust(20).encode())


def _catalogue(platform="edc16"):
    b = M.Builder(platform)
    if platform == "edc16":
        b.add("03G906016LQ 0971", "p475_83", {1: [RPM, None, None, CLT]}, "a.bin")
        b.add("03G906016LQ 0971", "p475_83", {1: [RPM, None, None, CLT]}, "b.bin")
        b.add("03G906016LQ 1318", "p475_83", {1: [RPM, None, None, CLT]}, "c.bin")
    else:
        b.add(M.edc15_key("038906012GN 1,9l R4 EDC  SG  4308"), "vm_fd",
              {1: [("engine speed (G1/1)", "xcwGrp00", "")] * 4}, "gn.bin")
        b.add(M.edc15_key("038906012GN 1,9l R4 EDC  AG  4308"), "vm_fd",
              {2: [("fuel quantity (G2/1)", "xcwGrp02", "")] * 4}, "gn.bin")
    return M.Catalogue(b.document())


# ------------------------------------------------------------- the format
def test_identical_maps_are_stored_once_and_sources_merge():
    doc = M.Builder("edc16")
    for k, src in (("A 0001", "x"), ("A 0001", "y"), ("B 0002", "z")):
        doc.add(k, "p", {1: [RPM, None]}, src)
    d = doc.document()
    assert len(d["maps"]) == 1 and len(d["labels"]) == 1
    assert d["ecus"]["A 0001"]["sources"] == ["x", "y"]
    assert d["maps"][0]["1"] == [0, -1]


def test_disagreeing_sources_leave_the_identity_out():
    b = M.Builder("edc16")
    b.add("A 0001", "p", {1: [RPM]}, "x")
    b.add("A 0001", "p", {1: [CLT]}, "y")
    b.add("A 0001", "p", {1: [RPM]}, "z")          # stays out once ambiguous
    assert "A 0001" not in b.document()["ecus"]
    assert b.conflicts["A 0001"] == ["x", "y", "z"]


def test_written_catalogue_reads_back(tmp_path):
    b = M.Builder("edc16")
    b.add("03G906016LQ 0971", "p475_83", {1: [RPM, None, None, CLT]}, "a.bin")
    p = str(tmp_path / M.file_name("edc16"))
    b.write(p)
    import gzip
    import json
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        cat = M.Catalogue(json.load(fh))
    n = cat.namer("03G906016LQ 0971")
    assert n.resolve(1, 0) == RPM and n.resolve(1, 3) == CLT


def test_an_unknown_format_is_refused():
    with pytest.raises(ValueError):
        M.Catalogue({"format": 99})


# ------------------------------------------------------------ EDC16 lookup
def test_edc16_matches_the_part_and_software_the_ecu_reports():
    cat = _catalogue()
    n = cat.namer(*cat.find(format_identity(_record("03G906016LQ", "0971"))))
    assert (n.key, n.match) == ("03G906016LQ 0971", "exact")


def test_edc16_falls_back_to_the_part_number():
    cat = _catalogue()
    key, how = cat.find(format_identity(_record("03G906016LQ", "9999")))
    assert how == "part" and key.startswith("03G906016LQ ")


def test_edc16_another_part_number_gets_the_generic_layout():
    cat = _catalogue()
    assert cat.find(format_identity(_record("03G906016KN", "0971"))) == (None, "generic")
    assert cat.find("not a 9B record") == (None, "generic")
    n = cat.namer(None, "generic", "x")
    assert n.match == "generic" and n.resolve(1, 0) == RPM


def test_of_a_parts_software_versions_the_shared_map_wins():
    b = M.Builder("edc16")
    b.add("P 0001", "f", {1: [CLT]}, "a")
    b.add("P 0002", "f", {1: [RPM]}, "b")
    b.add("P 0003", "f", {1: [RPM]}, "c")
    cat = M.Catalogue(b.document())
    key, how = cat.find(format_identity(_record("P", "0009")))
    assert how == "part" and cat.namer(key).resolve(1, 0) == RPM


# ------------------------------------------------------------ EDC15 lookup
def test_edc15_picks_the_dataset_the_ident_names():
    cat = _catalogue("edc15")
    assert cat.find("038906012GN 1,9l R4 EDC SG 4308") == ("038906012GN 1,9l R4 EDC SG 4308", "exact")
    assert cat.find("038906012GN 1,9l R4 EDC AG 4308") == ("038906012GN 1,9l R4 EDC AG 4308", "exact")


def test_edc15_ident_split_across_blocks_still_matches():
    """KW1281 sends the text in fixed blocks; the logger joins them stripped,
    so a word cut at a block edge comes back with a space in it."""
    cat = _catalogue("edc15")
    joined = "038906012GN 1,9l R4 EDC SG 43 08 00002 Coding"
    assert cat.find(joined) == ("038906012GN 1,9l R4 EDC SG 4308", "exact")


def test_edc15_other_software_keeps_the_gearbox():
    cat = _catalogue("edc15")
    assert cat.find("038906012GN 1,9l R4 EDC AG 4309") == ("038906012GN 1,9l R4 EDC AG 4308", "part")
    assert cat.find("038906012GN 1,9l R4 EDC SG 4309") == ("038906012GN 1,9l R4 EDC SG 4308", "part")


def test_edc15_a_longer_part_number_is_not_this_one():
    cat = _catalogue("edc15")
    assert cat.find("038906012GNA 1,9l R4 EDC SG 4308") == (None, "generic")
    assert cat.find("038906019XX 1,9l R4 EDC SG 9999") == (None, "generic")


# --------------------------------------------------------- generic layout
def test_generic_layout_names_only_what_most_part_numbers_agree_on():
    b = M.Builder("edc16")
    b.add("A 0001", "f", {1: [RPM, CLT], 9: [RPM]}, "a")
    b.add("B 0001", "f", {1: [RPM, RPM]}, "b")
    b.add("C 0001", "f", {1: [RPM, None]}, "c")
    b.add("C 0002", "f", {1: [RPM, None]}, "d")          # one part, one vote
    b.add("D 0001", "f", {1: [RPM, None]}, "e")
    g = b._generic()
    assert g[1][0] == RPM                 # 4 of 4
    assert g[1][1] is None                # CLT 1, RPM 1, unnamed 2: no majority
    assert 9 not in g                     # 1 part of 4 populates it


# ----------------------------------------------------------- the namer
def test_catalogue_namer_behaves_like_channel_namer():
    n = _catalogue().namer("03G906016LQ 0971")
    assert n.available and n.populated_groups() == [1]
    assert n.slots(1) == 4 and n.slots(77) == 4
    assert n.resolve(1, 1) == ("Group 1 / Cell 2", "", "")
    assert n.resolve(5, 0) == ("Group 5 / Cell 1", "", "")
    assert n.find_signal("CTSCD_tClnt") == (1, 3)
    assert n.group_summary(1) == "Engine Speed, Coolant Temperature"
    ch = n.channel(1, 0)
    assert (ch.label, ch.signal, ch.unit) == RPM


def test_presets_keep_off_unserved_blocks_with_a_catalogue_namer():
    b = M.Builder("edc16")
    b.add("K 0001", "p", {g: [RPM] for g in (1, 3, 99, 100, 133)}, "x")
    n = M.Catalogue(b.document()).namer("K 0001")
    assert unserved_for(n) == [100, 133]
    assert unserved_for(M.Catalogue(b.document()).namer(None, "generic")) == [100, 133]


def test_controllers_without_a_catalogue_get_nothing():
    assert M.namer_for("ZF6HP", "anything") is None


def test_with_no_identity_yet_the_generic_layout_names_the_cells():
    if M.load("edc16") is None:
        pytest.skip("edc16 catalogue not built")
    n = M.namer_for("EDC16", "")
    assert n.match == "generic" and n.resolve(1, 0)[1] == "Eng_nAvrg"


# --------------------------------------------- against the shipped data
LQ = os.path.join(SGO, "03G906016LQ_0971.bin")


@pytest.mark.skipif(not os.path.exists(LQ), reason="needs the SGO corpus")
def test_shipped_entry_names_exactly_what_the_binary_does():
    from ovtsuite import fileinfo
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession
    cat = M.load("edc16")
    if cat is None:
        pytest.skip("edc16 catalogue not built")
    s = TunerSession(LQ)
    ident = fileinfo.ident16(bytes(s.binfile.data))
    n = M.namer_for("EDC16", format_identity(_record(ident["part"], ident["sw_version"])))
    assert n is not None and (n.key, n.match) == ("03G906016LQ 0971", "exact")
    ref = ChannelNamer(s)
    assert n.populated_groups() == ref.populated_groups()
    for g in ref.populated_groups():
        for f in range(ref.slots(g)):
            assert n.resolve(g, f) == ref.resolve(g, f), (g, f)


GN = os.path.join(EDC15, "038906012GN.bin")


@pytest.mark.skipif(not os.path.exists(GN), reason="needs 038906012GN.bin")
def test_shipped_edc15_entry_follows_the_coded_dataset():
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession
    if M.load("edc15") is None:
        pytest.skip("edc15 catalogue not built")
    s = TunerSession(GN)
    for ds in s.datasets:
        n = M.namer_for("EDC15", ds.ident + " 00002")
        assert n is not None, ds.ident
        ref = ChannelNamer(s, dataset=ds.label)
        assert n.populated_groups() == ref.populated_groups()
        for g in ref.populated_groups():
            for f in range(ref.slots(g)):
                assert n.resolve(g, f) == ref.resolve(g, f), (ds.label, g, f)
