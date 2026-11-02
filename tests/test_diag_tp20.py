# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P3: TP 2.0 proper -- several channels on one CAN connection
(canmux / link.Bus), the gateway's installation list, identification through
data identifiers, and the parallel vehicle scan. The wire forms are the ones
in Bear's own autoscan capture (Reference/Logs/VCDS-Autoscan.log)."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_tp20 import MockTp20Bus  # noqa: E402

from ovtsuite.diag import defs as D
from ovtsuite.diag import link as L
from ovtsuite.diag import scan as SC
from ovtsuite.diag import gateway as GW
from ovtsuite.diag.canmux import CanMux
from ovtsuite.diag.model import Vehicle
from ovtsuite.diag.session import ModuleSession, NotSupported

HERE = os.path.dirname(os.path.abspath(__file__))
#: the C6 gateway's installation list as captured (62 04 A1 payload)
LIST_C6 = bytes.fromhex(open(os.path.join(HERE, "_gateway_list_c6.hex")).read().strip())


def _resolver():
    return D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))


class Kwp9bModule:
    """An engine-like module: 1A 9B identification, 21 groups, no faults."""

    def __init__(self, part, sw=b"0971", faults=()):
        self.part, self.sw, self.faults = part.encode().ljust(12), sw, list(faults)
        self.requests = []

    def __call__(self, req):
        self.requests.append(bytes(req))
        svc = req[0]
        if svc == 0x10:
            return b"\x50" + req[1:]
        if svc == 0x3E:
            return b"\x7E"
        if req == b"\x1A\x9B":
            return (b"\x5A\x9B" + self.part + self.sw + b"\x03\x00\x00\x4C" + bytes.fromhex("01 38 D4 68 04 CF")
                    + b"R4 2,0L EDC G000AG")
        if svc == 0x18:
            out = bytearray([len(self.faults)])
            for c, st in self.faults:
                out += bytes([c >> 8, c & 0xFF, st])
            return b"\x58" + bytes(out)
        if svc == 0x12:
            return b"\x7F\x12\x31"
        if svc == 0x21:
            g = req[1]
            return b"\x61" + bytes([g]) + bytes([1, 200, g]) * 4
        return b"\x7F" + bytes([svc, 0x11])


class DidModule:
    """A C6 body module / gateway as captured: 1A 9B -> 7F 1A 11, data
    identifiers for the identification, 22 06 00 long coding, 18 02 FF 00
    faults, optionally the installation list on 22 04 A1."""

    def __init__(self, part, sw, hw_part, hw_version, name, coding, wsc6=bytes.fromhex("01 38 D4 68 04 CF"),
                 faults=(), installation_list=None):
        self.dids = {0xF187: part.encode().ljust(11) + b" ", 0xF189: sw.encode(), 0xF191: hw_part.encode().ljust(11) + b" ",
                     0xF1A3: hw_version.encode(), 0xF197: name.encode().ljust(13), 0xF1A5: wsc6,
                     0x0600: bytes(coding), 0x0601: bytes([len(coding)]), 0xF1DF: b"\x40"}
        if installation_list is not None:
            self.dids[0x04A1] = installation_list
        self.faults = list(faults)
        self.requests = []

    def __call__(self, req):
        self.requests.append(bytes(req))
        svc = req[0]
        if svc == 0x10:
            return b"\x50" + req[1:]
        if svc == 0x3E:
            return b"\x7E"
        if svc == 0x1A:
            return b"\x7F\x1A\x11"
        if svc == 0x22:
            did = (req[1] << 8) | req[2]
            if did not in self.dids:
                return b"\x7F\x22\x31"
            return b"\x62" + req[1:3] + self.dids[did]
        if svc == 0x18:
            if req[1] != 0x02:
                return b"\x7F\x18\x12"
            out = bytearray([len(self.faults)])
            for c, st in self.faults:
                out += bytes([c >> 8, c & 0xFF, st])
            return b"\x58" + bytes(out)
        if svc == 0x12:
            return b"\x7F\x12\x31"
        if svc == 0x21:
            return b"\x61" + req[1:2] + bytes.fromhex("24 46 49 13 04 C8 06 40 BC 7F C5 09")
        return b"\x7F" + bytes([svc, 0x11])


def _c6_bus(**extra):
    gw = DidModule("4F0907468N", "0035", "4F0907468G", "H06", "GW-BEM 5CAN-M", b"\x00\x03\x03",
                   wsc6=bytes.fromhex("06 E7 EE 6E 09 57"), faults=[(0x0414, 0x60), (0x0528, 0x24)],
                   installation_list=LIST_C6)
    cluster = DidModule("4F0920933N", "0147", "4F0920933N", "H08", "KOMBIINSTR.",
                        bytes.fromhex("82 FF 12 05 08 00 10 08 02 03 00"), faults=[(0x0414, 0x60), (0x0522, 0x24)])
    ecus = {0x1F: gw, 0x07: cluster, 0x01: Kwp9bModule("03G906016LQ"), 0x02: Kwp9bModule("4F9910156A", b"0020"),
            0x03: Kwp9bModule("4F0910517AL", b"0060", faults=[(0x0823, 0x64)])}
    ecus.update(extra)
    return MockTp20Bus(ecus), ecus


# ------------------------------------------------------------- gateway list
def test_installation_list_from_the_capture():
    names = {m["addr"]: m["name"] for m in _resolver().addressing("C6")}
    lst = GW.parse_installation_list(LIST_C6, names)
    assert len(lst.entries) == 59
    gw = lst.entry("19")
    assert gw.dest == 0x1F and gw.installed and gw.faults_flagged and gw.name == "CAN gateway"
    assert lst.entry("17").dest == 0x07 and lst.entry("08").dest == 0x2C and lst.entry("03").dest == 0x03
    assert lst.entry("01").status == 0x11 and lst.entry("01").installed and not lst.entry("01").faults_flagged
    assert lst.entry("10").status == 0 and not lst.entry("10").installed and lst.entry("10").status_text == "not installed"
    assert lst.entry("46").bus_name == "comfort CAN" and lst.entry("56").bus_name == "infotainment"
    assert lst.entry("02").bus_name == "drive CAN" and lst.entry("17").bus_name == "bus 0x03"
    assert lst.by_dest(0x2C).addr == "08"
    assert len(lst.installed) == 26
    # every installed dest is what the C6 table says
    rows = {m["addr"]: m for m in _resolver().addressing("C6")}
    for e in lst.installed:
        assert D.as_int(rows[e.addr]["tp20"]["dest"]) == e.dest, e
    back = GW.InstallationList.from_dict(lst.to_dict())
    assert back.raw == LIST_C6 and [e.to_dict() for e in back.entries] == [e.to_dict() for e in lst.entries]


def test_rows_from_gateway_adds_unknown_modules_and_prefers_the_cars_dests():
    table = _resolver().addressing("C6")
    lst = GW.parse_installation_list(LIST_C6 + bytes([0x77, 0x5A, 0x01, 0x04]) + bytes([0x7E, 0x66, 0x01, 0x01]))
    rows = SC.rows_from_gateway(lst, table)
    addrs = [r["addr"] for r in rows]
    assert "7E" in addrs and "10" not in addrs                     # unknown installed module in, absent module out
    unknown = next(r for r in rows if r["addr"] == "7E")
    assert D.as_int(unknown["tp20"]["dest"]) == 0x66 and "gateway" in unknown["name"].lower()
    assert all(r.get("_gateway") for r in rows)


# ------------------------------------------------------------------- the mux
def test_mux_dispatches_by_id_and_parallel_channels_do_not_cross():
    ch, ecus = _c6_bus()
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    out = {}

    def run(addr, n=25):
        row = res.module_address(addr, "C6")
        sess = ModuleSession(bus.link_for(row), row, res, generation="C6")
        sess.connect()
        vals = [sess.read_group(g)[0].b for g in range(1, n + 1)]
        out[addr] = (sess.info.part, sess.link.description, vals)
        sess.close()

    threads = [threading.Thread(target=run, args=(a,)) for a in ("01", "02", "03")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(out) == ["01", "02", "03"]
    assert out["01"][0] == "03G906016LQ" and out["02"][0] == "4F9910156A" and out["03"][0] == "4F0910517AL"
    for addr, (_p, desc, vals) in out.items():
        assert vals == list(range(1, 26)), addr                     # every reply reached its own channel
    listens = {desc.split("listen ")[1][:5] for _p, desc, _v in out.values()}
    assert len(listens) == 3                                      # three distinct listen ids
    assert bus.mux.dropped == 0
    assert bus.listen_ids.free == list(range(0x300, 0x310))       # all given back on close
    bus.close()


def test_mux_refuses_a_module_that_answers_on_another_channels_id():
    """Two modules that both answer on 0x300 whatever listen id they were
    given: the first channel keeps its frames, the second one's open fails
    with a readable error instead of both dialogs reading each other's
    frames, and a scan records the second module as not reached."""
    from ovtsuite.flash.transport import KLineError
    ch, ecus = _c6_bus()
    ch.honour_listen = False
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    row1, row2 = res.module_address("01", "C6"), res.module_address("02", "C6")
    s1 = ModuleSession(bus.link_for(row1), row1, res, generation="C6")
    s1.connect()
    assert s1.info.part == "03G906016LQ"
    s2 = ModuleSession(bus.link_for(row2, setup_timeout_ms=200), row2, res, generation="C6")
    with pytest.raises(KLineError, match="cannot be told apart"):
        s2.connect()
    s2.close()
    # the first channel is still whole
    assert [c.b for c in s1.read_group(3)][0] == 3
    s1.close()
    assert bus.listen_ids.free == list(range(0x300, 0x310))
    bus.close()


def test_mux_port_read_waits_briefly_and_reports_a_dead_channel():
    class Dead:
        def pass_all(self):
            pass

        def read(self, n, t):
            raise OSError("device unplugged")

        def write_can(self, *a, **k):
            pass

    mux = CanMux(Dead())
    port = mux.subscribe("x")
    from ovtsuite.diag.canmux import MuxClosed
    with pytest.raises(MuxClosed):
        for _ in range(50):
            port.read(16, 0)
    mux.close()


# ------------------------------------------------------ DID identification
def test_did_identification_when_9b_is_refused():
    ch, ecus = _c6_bus()
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    row = res.module_address("17", "C6")
    sess = ModuleSession(bus.link_for(row), row, res, generation="C6")
    sess.connect()
    info = sess.info
    assert sess.moddef["identification"]["record"] == "kwp_dids"
    assert info.part == "4F0920933N" and info.sw == "0147"
    assert info.extra["hw_version"] == "H08" and info.extra["system_name"] == "KOMBIINSTR."
    assert info.coding_bytes == "82ff12050800100802 0300".replace(" ", "") and info.coding is None
    assert info.coding_text == "82 FF 12 05 08 00 10 08 02 03 00"
    assert info.wsc == 45678 or info.wsc is not None             # decoded from the F1 A5 pack
    from ovtsuite.diag.kwp2000 import Wsc
    assert Wsc.decode(bytes.fromhex("01 38 D4 68 04 CF")).wsc == info.wsc
    assert "KOMBIINSTR." in info.identity_text and "4F0920933N" in info.identity_text
    # the module was never asked for a combined identifier list
    assert all(len(r) == 3 for r in ecus[0x07].requests if r[0] == 0x22)
    # the gateway doc says the cluster has no installation list; long coding writes are a later phase
    with pytest.raises(NotSupported):
        sess.installation_list()
    with pytest.raises(NotSupported):
        sess.set_coding(1)
    faults = sess.read_faults()
    assert [f.code for f in faults] == [0x0414, 0x0522]
    assert ecus[0x07].requests.count(b"\x18\x02\xFF\x00") == 1   # the form this module takes
    sess.close()
    bus.close()


def test_did_identification_falls_back_without_a_module_document():
    """A module the definitions know nothing about that refuses 1A 9B is
    still identified from the captured identifier set."""
    ch, ecus = _c6_bus()
    ecus[0x21] = DidModule("4F0910289E", "0220", "4F0907289E", "H12", "Komfortgeraet", b"\x01\x02\x03")
    ch.ecus[0x21] = type(ch.ecus[0x1F])(ecus[0x21], dest=0x21, ecu_rx=0x740 + 0x21)
    ch.ecus[0x21].out = ch.out
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    row = res.module_address("46", "C6")
    sess = ModuleSession(bus.link_for(row), row, res, generation="C6")
    sess.connect()
    assert sess.info.part == "4F0910289E" and sess.info.coding == 0x010203 and sess.info.coding_bytes == "010203"
    assert sess.info.extra["identification"] == "dids"
    sess.close()
    bus.close()


# ------------------------------------------------------------------ the scan
def test_parallel_scan_uses_the_gateway_list():
    ch, ecus = _c6_bus()
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    log, progress = [], []
    result = SC.scan_vehicle(L.Interface("mock", "mock bus", "openport"), "can", Vehicle(id="v", generation="C6"),
                             res, log=log.append, progress=lambda m, d, t: progress.append((m, d, t)),
                             bus=bus, workers=4)
    bus.close()
    assert result.basis == "gateway" and result.gateway and len(result.gateway["entries"]) == 59
    addrs = [m.addr for m in result.modules]
    assert addrs == sorted(addrs)
    installed = {e["addr"] for e in result.gateway["entries"] if e["status"]}
    assert set(addrs) == installed                                 # exactly the installed modules were visited
    assert "10" not in addrs and "19" in addrs and "17" in addrs
    gw = result.module("19")
    assert gw.reached and gw.part == "4F0907468N" and gw.coding == 0x000303 and gw.extra["installation_list"]
    assert [f.code for f in gw.faults] == [0x0414, 0x0528]
    assert result.module("17").reached and result.module("17").coding_text.startswith("82 FF")
    assert result.module("01").reached and result.module("01").part == "03G906016LQ"
    assert result.module("03").fault_count == 1
    missing = result.module("46")                                  # listed as installed, nobody answers
    assert missing.reached is False and "no response" in missing.error.lower()
    assert missing.extra["gateway_entry"]["dest"] == 0x21
    assert progress[-1][0] == "done" and progress[-1][1] == progress[-1][2] == len(result.modules)
    assert any("Gateway lists 26 modules" in m for m in log)
    # the listen ids were all returned
    assert len(bus.listen_ids._free) == 16


def test_scan_without_gateway_walks_the_table_rows():
    ch, ecus = _c6_bus()
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    rows = [res.module_address(a, "C6") for a in ("01", "02", "03")]
    result = SC.scan_vehicle(L.Interface("mock", "mock bus", "openport"), "can", Vehicle(id="v", generation="C6"),
                             res, modules=rows, bus=bus, use_gateway=False)
    bus.close()
    assert result.basis == "table" and result.gateway is None
    assert [m.addr for m in result.modules] == ["01", "02", "03"] and all(m.reached for m in result.modules)


def test_scan_stops_between_modules():
    ch, ecus = _c6_bus()
    res = _resolver()
    bus = L.Bus.from_channel(ch)
    seen = []

    def stop():
        return len(seen) >= 1

    result = SC.scan_vehicle(L.Interface("mock", "mock bus", "openport"), "can", Vehicle(id="v", generation="C6"),
                             res, bus=bus, workers=1, is_stop=stop,
                             progress=lambda m, d, t: seen.append(m) if ("ok" in m or "not reached" in m) else None)
    bus.close()
    assert len(result.visited) <= 3                                 # the gateway, the one visited, at most one in flight
    # a stopped scan says so and keeps the rows it never got to as "not tried",
    # so the partial result is not mistaken for a complete one
    assert result.stopped
    not_tried = [m for m in result.modules if m.reached is None]
    assert not_tried and all(m.error == "not tried (scan stopped)" for m in not_tried)
    assert len(result.modules) == len(result.visited) + len(not_tried) >= 10
    assert result.to_dict()["stopped"] is True
    from ovtsuite.diag.model import ScanResult
    assert ScanResult.from_dict(result.to_dict()).stopped is True


def test_learned_addresses_land_in_the_local_pack(tmp_path, monkeypatch):
    from ovtsuite.diag import learned as LN
    monkeypatch.setattr(LN, "user_dir", lambda: str(tmp_path))
    path, n = LN.record_addresses([{"addr": "7E", "name": "Module 7E (gateway list)", "dest": 0x66, "bus": "comfort"}], "C6")
    assert n == 1 and os.path.isfile(path)
    path2, n2 = LN.record_addresses([{"addr": "7E", "dest": 0x67}], "C6")        # same row, new dest
    assert path2 == path and n2 == 1
    packs = D.load_packs([str(tmp_path)], "user")
    assert [p.id for p in packs] == ["my-local"]
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled") + packs)
    row = res.module_address("7E", "C6")
    assert D.as_int(row["tp20"]["dest"]) == 0x67 and row["bus"] == "comfort" and row["_pack"] == "my-local"
    assert D.validate_pack(packs[0]).ok
    # a K-line baud rate a module answered at is remembered on its row, and
    # the session then tries it first
    path3, changed = LN.record_kline_baud("17", 4800, "C6", "Instruments")
    assert path3 == path and changed
    assert LN.record_kline_baud("17", 4800, "C6")[1] is False           # already known
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled") + D.load_packs([str(tmp_path)], "user"))
    row17 = res.module_address("17", "C6")
    assert D.as_int(row17["kline"]["baud"]) == 4800
    import types
    from ovtsuite.diag.session import ModuleSession as _MS
    assert _MS.kline_bauds(types.SimpleNamespace(module=row17)) == [4800, 10400, 9600]
