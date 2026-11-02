# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P4: the assistant's read-only tools over a garage on disk."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.ai.tools import Context, Registry
from ovtsuite.diag import ai_tools as AT
from ovtsuite.diag import garage as G
from ovtsuite.diag.journal import Journal
from ovtsuite.diag.model import Fault, ModuleInfo, ScanResult


@pytest.fixture
def garage(tmp_path, monkeypatch):
    root = str(tmp_path / "garage")
    monkeypatch.setattr(G, "root", lambda: root)
    g = G.Garage(root)
    v = g.new_vehicle("Audi A6 (4F)", "WAUZZZ4F26N123456", "C6")
    import time
    now = time.time()
    s1 = ScanResult(vehicle_id=v.id, when=now - 3600, interface="mock", transport="can", generation="C6", basis="gateway",
                    gateway={"gateway_addr": "19", "raw": "", "entries": [
                        {"addr": "01", "dest": 1, "status": 0x11, "bus": 0, "name": "Engine"},
                        {"addr": "10", "dest": 0x1D, "status": 0, "bus": 0, "name": "Parking"}]})
    s1.modules = [ModuleInfo(addr="01", name="Engine", protocol="kwp2000", transport="can", part="03G906016LQ",
                             sw="0971", coding=76, wsc=1231, reached=True, platform="edc16",
                             faults=[Fault(code=0x4191, status=0x60, vag=16785, sae="P0401", text="EGR flow insufficient",
                                           status_text="0x60 stored, present")]),
                  ModuleInfo(addr="17", name="Instruments", protocol="kwp2000", transport="can", part="4F0920933N",
                             sw="0147", coding_bytes="82ff12", reached=True),
                  ModuleInfo(addr="46", name="Central convenience", reached=False, error="no response")]
    g.save_scan(s1)
    Journal(v.id).record(addr="01", module="Engine", function="clear_faults", before=[16785], after=[], verified=True)
    s2 = ScanResult(vehicle_id=v.id, when=now + 60, interface="mock", transport="can", generation="C6")
    s2.modules = [ModuleInfo(addr="01", name="Engine", protocol="kwp2000", transport="can", part="03G906016LQ",
                             sw="0971", coding=77, wsc=1231, reached=True, platform="edc16"),
                  ModuleInfo(addr="17", name="Instruments", protocol="kwp2000", transport="can", part="4F0920933N",
                             sw="0147", coding_bytes="82ff12", reached=True,
                             faults=[Fault(code=0x0414, status=0x60, vag=1044, sae="", text="", status_text="present")])]
    g.save_scan(s2)
    return g, v


def _reg():
    reg = Registry()
    AT.register(reg)
    return reg


def _call(reg, name, **args):
    ok, out = reg.call(name, Context(workspace=None, client="test"), args)
    assert ok, out
    return out


def test_vehicles_scan_history_and_diff(garage):
    g, v = garage
    reg = _reg()
    assert all(t.read_only for t in reg.all())
    vs = _call(reg, "diag_vehicles")
    assert vs["count"] == 1 and vs["vehicles"][0]["vin"] == "WAUZZZ4F26N123456" and vs["vehicles"][0]["scans"] == 2
    assert vs["vehicles"][0]["last_scan_faults"] == 1
    s = _call(reg, "diag_scan", vehicle=v.id)
    assert s["faults_total"] == 1 and s["modules_answered"] == 2 and s["modules"][1]["faults"][0]["code"] == "01044"
    by_name = _call(reg, "diag_scan", vehicle="Audi A6 (4F)", faults_only=True)
    assert [m["addr"] for m in by_name["modules"]] == ["17"]
    hist = _call(reg, "diag_scan_history", vehicle=v.id)
    assert len(hist["scans"]) == 2 and hist["scans"][0]["faults"] == 1 and hist["scans"][1]["basis"] == "gateway"
    first = _call(reg, "diag_scan", vehicle=v.id, scan=hist["scans"][1]["scan"])
    assert first["modules"][0]["faults"][0]["sae"] == "P0401" and first["gateway_list"][0]["tp20_dest"] == "0x01"
    assert len(first["gateway_list"]) == 1                                  # installed only
    assert first["modules"][1]["coding"] == "82 FF 12"
    d = _call(reg, "diag_scan_diff", vehicle=v.id)
    assert d["new_faults"] == 1 and d["cleared_faults"] == 1
    changed = {m["addr"]: m for m in d["modules_changed"]}
    assert changed["01"]["changes"] == ["coding 0000076 -> 0000077"] and changed["01"]["cleared_faults"][0]["sae"] == "P0401"
    assert changed["17"]["new_faults"][0]["code"] == "01044"
    assert d["journal_between"][0]["summary"].startswith("01 Engine") or "clear" in d["journal_between"][0]["summary"].lower()
    j = _call(reg, "diag_journal", vehicle=v.id, addr="01")
    assert len(j["entries"]) == 1 and j["entries"][0]["function"] == "clear_faults" and j["entries"][0]["verified"]
    ok, err = reg.call("diag_scan", Context(workspace=None), {"vehicle": "nope"})
    assert not ok and "no vehicle" in err["error"]


def test_fault_explain_and_definitions():
    reg = _reg()
    e = _call(reg, "diag_fault_explain", code="P0299")
    assert e["sae"] == "P0299" and e["vag"] == 16683 and e["text"]
    e2 = _call(reg, "diag_fault_explain", code="16683", addr="01", platform="edc16", status=0x60)
    assert e2["sae"] == "P0299" and e2["status"]["byte"] == "0x60" and "stored" in e2["status"]["text"]
    e3 = _call(reg, "diag_fault_explain", code="0x4123")
    assert e3["vag"] == 0x4123 and e3["sae"] == "P0291"             # a VAG number unless a definition says SAE
    # a raw word is read the way the module's definition says, never by its magnitude:
    # 0x0414 is VAG 01044 on the C6 cluster (its document says vag) and on an engine
    # no document describes; the EDC16 pack's per-part documents say which calibrations
    # report SAE words (03G906016LQ is P475: 0x0414 there is P0414)
    e4 = _call(reg, "diag_fault_explain", code="0x0414", addr="17", protocol="kwp2000")
    assert e4["vag"] == 1044 and e4["sae"] is None
    e5 = _call(reg, "diag_fault_explain", code="0x0414", addr="01", protocol="kw1281")
    assert e5["vag"] == 1044 and e5["sae"] is None
    e6 = _call(reg, "diag_fault_explain", code="0x0299", addr="01", protocol="kwp2000", platform="edc16")
    assert e6["vag"] == 665 and e6["sae"] is None
    from ovtsuite.diag.defs import load as L
    if any(os.path.isdir(os.path.join(d, "edc16-faults")) for d in L.module_dirs()):
        e7 = _call(reg, "diag_fault_explain", code="0x0299", addr="01", protocol="kwp2000", platform="edc16",
                   part="03G906016LQ")
        assert e7["vag"] == 16683 and e7["sae"] == "P0299" and "PCR_GovDev" in e7["text"]
        e8 = _call(reg, "diag_fault_explain", code="0x0414", addr="01", protocol="kwp2000", platform="edc16",
                   part="038997016T")                                     # P350: VAG numbers
        assert e8["vag"] == 1044 and e8["sae"] is None
    ok, err = reg.call("diag_fault_explain", Context(workspace=None), {"code": "garbage"})
    assert not ok
    d = _call(reg, "diag_definitions", addr="01", generation="C6", protocol="kwp2000", platform="edc16")
    assert d["addressing"]["tp20"]["dest"] == 1 and d["module"]["variants"]["adaptation"] == "routine_b8"
    ids = [p["id"] for p in d["procedures"]]
    assert "procedure.engine.boost-check" in ids and all(p["safety"] == "read-only" for p in d["procedures"])
    d19 = _call(reg, "diag_definitions", addr="19", generation="C6", protocol="kwp2000")
    assert d19["module"]["identification"]["record"] == "kwp_dids"


def test_interface_lookup_is_strict(monkeypatch):
    from ovtsuite.diag import link as L
    monkeypatch.setattr(L, "interfaces", lambda: [L.Interface("COM3", "COM3 - FTDI", "serial"),
                                                   L.Interface("COM4", "COM4 - FTDI", "serial")])
    reg = _reg()
    assert _call(reg, "diag_interfaces")["interfaces"][0]["label"] == "COM3 - FTDI"
    assert AT._iface("com3").label == "COM3 - FTDI"
    with pytest.raises(Exception):
        AT._iface("FTDI")                                           # ambiguous
    with pytest.raises(Exception):
        AT._iface("COM9")
