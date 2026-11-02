# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag garage, journal, scan diff and reports."""
import os

from ovtsuite.diag.garage import Garage, scan_label
from ovtsuite.diag.journal import Journal
from ovtsuite.diag.model import Fault, ModuleInfo, ScanResult, Vehicle, diff_scans
from ovtsuite.diag import report as R


def _scan(vid, faults01, coding01=71, when=1000.0, reach03=True):
    s = ScanResult(vehicle_id=vid, when=when, interface="OpenPort", transport="can", generation="C6")
    m1 = ModuleInfo(addr="01", name="Engine", protocol="kwp2000", transport="can",
                    part="03G906016LQ", sw="8449", coding=coding01, wsc=12345, reached=True)
    m1.faults = [Fault(code=c, status=0x60, vag=c, sae=sae, text=t) for c, sae, t in faults01]
    m3 = ModuleInfo(addr="03", name="ABS / ESP", reached=reach03, error="" if reach03 else "no answer")
    s.modules = [m1, m3]
    return s


def test_garage_vehicles_and_scans(tmp_path):
    g = Garage(str(tmp_path / "garage"))
    assert g.vehicles() == []
    v = g.new_vehicle("Audi A6 (4F) 2006", vin="WAUZZZ4F76N123456", generation="C6")
    assert v.id == "wauzzz4f76n123456"
    v2 = g.new_vehicle("Audi A6 (4F) 2006", vin="WAUZZZ4F76N123456")
    assert v2.id == "wauzzz4f76n123456-2"
    assert [x.id for x in g.vehicles()] and g.vehicle(v.id).generation == "C6"
    p = g.save_scan(_scan(v.id, [(16785, "P0401", "EGR flow")]))
    assert os.path.isfile(p) and g.scans(v.id) == [os.path.basename(p)]
    again = g.latest_scan(v.id)
    assert again.module("01").faults[0].sae == "P0401"
    assert scan_label("20261025-211400.json") == "2026-10-25 21:14"
    v.notes = "swap car"
    g.save_vehicle(v)
    assert g.vehicle(v.id).notes == "swap car"
    g.delete_vehicle(v.id)
    assert g.vehicle(v.id) is None


def test_diff_scans():
    before = _scan("a6", [(16785, "P0401", "EGR flow"), (16683, "P0299", "Boost low")], coding01=71,
                   reach03=False)
    after = _scan("a6", [(16785, "P0401", "EGR flow"), (17000, "P0616", "Starter")], coding01=72,
                  when=2000.0)
    d = diff_scans(before, after)
    m01 = next(m for m in d.modules if m.addr == "01")
    assert [f.vag for f in m01.new_faults] == [17000]
    assert [f.vag for f in m01.cleared_faults] == [16683]
    assert m01.changes == ["coding 0000071 -> 0000072"]
    m03 = next(m for m in d.modules if m.addr == "03")
    assert m03.newly_reached and not m03.lost
    assert d.new_faults == 1 and d.cleared_faults == 1 and len(d.changed) == 2
    # reversed: the ABS module is lost
    back = diff_scans(after, before)
    assert next(m for m in back.modules if m.addr == "03").lost


def test_journal_roundtrip(tmp_path):
    j = Journal("a6", str(tmp_path / "journal.jsonl"))
    assert j.entries() == []
    e = j.record("01", "Engine", "clear_faults", before=[16785, 16683], after=[])
    j.record("17", "Instruments", "adaptation", before=15000, after=30000, entry_id="adaptation.ch02",
             verified=True)
    entries = j.entries()
    assert len(entries) == 2 and entries[0].seq == 0
    assert entries[0].summary == "01 Engine: cleared 2 faults"
    assert entries[1].summary == "17 Instruments: adaptation.ch02 15000 -> 30000"
    assert entries[1].verified is True
    j.mark_reverted(0)
    assert j.entries()[0].reverted and not j.entries()[1].reverted
    assert [x.entry_id for x in j.for_module("17")] == ["adaptation.ch02"]
    assert len(j.since(e.when)) == 2


def test_reports_text_and_html():
    v = Vehicle(id="a6", name="Audi A6 (4F)", vin="WAUZZZ4F76N123456")
    before = _scan("a6", [(16683, "P0299", "Boost low")], when=1000.0)
    after = _scan("a6", [(16785, "P0401", "EGR flow")], coding01=72, when=2000.0)
    txt = R.text_report(v, after, before)
    assert "03G906016LQ" in txt and "16785 · P0401  EGR flow" in txt
    assert "+ new: 16785" in txt and "- cleared: 16683" in txt and "coding 0000071 -> 0000072" in txt
    assert "1 of 2 modules" in txt or "1 of 2 answered" in txt or "2 of 2" in txt
    page = R.html_report(v, after, before)
    assert page.startswith("<!doctype html>") and "EGR flow" in page and "P0401" in page
    assert "coding 0000071 -&gt; 0000072" in page
    assert "<script" not in page
