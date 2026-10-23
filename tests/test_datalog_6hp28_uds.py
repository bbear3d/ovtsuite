# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Logging the ZF 6HP28 (GS19) over UDS DIDs, ISO-TP 0x7E1/0x7E9 via the gateway.

The DID set and element widths come from the 6HP28's own DID table and the
named DIDs from the car's own session (Reference/Logs/VCDS-UDS-Trans.log);
these pin the decode (F4xx as SAE J1979 PIDs, named DIDs, RAM DIDs at their
table width, code-built DIDs raw), the multi-DID read and its fallback, and the
session end-to-end against a mock UDS link, with no Qt dependency.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash.diag import KLineTimeout                       # noqa: E402
from ovtsuite.datalog import zf6hp_uds_live as L                   # noqa: E402
from ovtsuite.datalog import logfile as LF                         # noqa: E402

DATA = {
    0xF40C: bytes([0x0B, 0xB8]),            # 3000 rpm / 4 = 750 rpm
    0xF405: bytes([130]),                    # 90 degC
    0xF442: bytes([0x36, 0xB0]),             # 14.0 V
    0x0407: bytes([0, 1, 0, 2, 0, 3, 1, 0]),
    0x1024: bytes([0x12, 0x34]),
    0x1008: bytes([7]),
    0x1007: bytes([0xAA, 0xBB, 0xCC]),
    0x0285: bytes([0x77]),                   # 11.9 V
    0x2104: bytes([0x4E]),
    0xF187: b"8K0927158J ",
    0xF191: b"8K0927158H ",
}


# ----------------------------------------------------------------- decode --
def test_obd_dids_scale_as_j1979_pids():
    assert L.decode_did(0xF40C, DATA[0xF40C])[0].value == 750.0
    c = L.decode_did(0xF405, DATA[0xF405])[0]
    assert c.value == 90.0 and c.unit == "°C"
    assert L.decode_did(0xF442, DATA[0xF442])[0].value == 14.0


def test_ram_dids_decode_at_table_width():
    cells = L.decode_did(0x0407, DATA[0x0407])          # 4 x u16, big-endian
    assert [c.value for c in cells] == [1.0, 2.0, 3.0, 256.0]
    assert L.decode_did(0x1024, DATA[0x1024])[0].value == 0x1234
    assert L.decode_did(0x1008, DATA[0x1008])[0].value == 7.0
    # a short answer gives N/A for the missing element, not a shifted value
    cells = L.decode_did(0x0407, DATA[0x0407][:6])
    assert cells[3].value is None and cells[2].value == 3.0


def test_code_built_did_is_one_raw_cell():
    c = L.decode_did(0x1007, DATA[0x1007])
    assert len(c) == 1 and c[0].value == float(0xAABBCC) and c[0].text == "AA BB CC"
    assert L.decode_did(0x1007, bytes(6))[0].value is None   # too long for a number


def test_table_is_consistent():
    assert len(set(L.DIDS)) == len(L.DIDS)
    assert not any(d >> 8 == 0xF1 for d in L.DIDS)           # no identification DIDs
    assert all(t in (1, 2) for _n, t in L.RAM_DIDS.values())
    assert 0x04A1 not in L.DIDS                               # the gateway's block


# ------------------------------------------------------------------ namer --
def test_namer_one_group_per_did():
    nm = L.Blocks6hpUds()
    assert nm.populated_groups() == L.DIDS
    assert nm.slots(0x0407) == 4 and nm.slots(0x1007) == 1 and nm.slots(0xF40C) == 1
    label, signal, unit = nm.resolve(0xF40C, 0)
    assert "Engine speed" in label and unit == "rpm" and signal == "zf6hp28_f40c"
    label, signal, _u = nm.resolve(0x0407, 2)
    assert label == "6HP28 DID 0407[3] (u16)" and signal == "zf6hp28_0407_3"
    assert nm.resolve(0x1007, 0)[0] == "6HP28 DID 1007 (raw)"
    assert nm.group_label(0x1024).startswith("1024 - RAM u16, rolling history")


def test_did_groups_round_trip_through_both_log_layouts(tmp_path):
    from datetime import datetime
    nm = L.Blocks6hpUds()
    log = LF.DataLog(channels=[nm.channel(0xF40C, 0), nm.channel(0x0407, 3)])
    log.append(datetime(2026, 10, 22, 12, 0, 0), ["750 rpm", "256"])
    for write, read, name in ((LF.write_kwhack, LF.read_kwhack, "a.csv"),
                              (LF.write_vcds, LF.read_vcds, "b.csv")):
        p = str(tmp_path / name)
        write(p, log)
        back = read(p).channels
        # a four-digit DID group must survive (the VCDS marker was 3 digits)
        assert [(c.group, c.field) for c in back] == [(0xF40C, 0), (0x0407, 3)], name


# ------------------------------------------------------------- named DIDs
def test_named_dids_from_the_car_session():
    c = L.decode_did(0x0285, DATA[0x0285])[0]
    assert c.value == 11.9 and c.unit == "V"
    c = L.decode_did(0x2104, DATA[0x2104])[0]          # scale not established: raw
    assert c.value == 78.0 and c.unit == ""
    nm = L.Blocks6hpUds()
    assert nm.resolve(0x2104, 0)[0] == "ATF temperature (raw, DID 2104)"
    assert nm.resolve(0x0285, 0)[2] == "V"
    assert nm.slots(0x0288) == 1                       # named, though also a RAM DID
    assert L.DIDS[:4] == [0xF40C, 0x2102, 0x2103, 0x2106]
    assert L.did_length(0x2102) == 2 and L.did_length(0x0805) == 1


# ------------------------------------------------------------- live session
class MockLink:
    """A UdsLink stand-in: answers UDS requests from DATA."""

    def __init__(self, kind="ISO15765", multi=True, silent=False, seen=None):
        self.kind, self.multi, self.silent = kind, multi, silent
        self.seen = seen if seen is not None else []
        self.closed = False

    def request(self, req, timeout_ms=1000):
        req = bytes(req)
        self.seen.append(req)
        if self.silent:
            raise KLineTimeout("no response")
        if req[:1] == b"\x10":
            return b"\x50" + req[1:2] + b"\x00\x32\x01\xf4"
        if req[:1] == b"\x3e":
            return b"\x7e\x00" if req[1:2] == b"\x00" else b"\x7f\x3e\x13"
        if req[:1] == b"\x22":
            dids = [int.from_bytes(req[i:i + 2], "big") for i in range(1, len(req), 2)]
            if len(dids) > 1 and not self.multi:
                return b"\x7f\x22\x13"
            out = b"".join(d.to_bytes(2, "big") + DATA[d] for d in dids if d in DATA)
            return (b"\x62" + out) if out else b"\x7f\x22\x31"
        return b"\x7f" + req[:1] + b"\x11"

    def close(self):
        self.closed = True


def _session(**kw):
    seen = []
    links = []

    def opener(prefer_iso):
        link = MockLink(seen=seen, **kw)
        links.append((prefer_iso, link))
        return link
    return L.Zf6hpUdsLiveSession(opener), seen, links


def test_session_enters_extended_and_reads_dids():
    s, seen, _links = _session()
    s.connect()
    assert seen[0] == b"\x10\x03" and s.session == 0x03
    assert not any(r == b"\x22\x04\xa1" for r in seen)
    assert "8K0927158J" in s.identity
    assert s.read_group(0xF40C)[0].value == 750.0
    assert [c.value for c in s.read_group(0x0407)] == [1.0, 2.0, 3.0, 256.0]


def test_iso15765_without_a_session_retries_on_raw_can():
    calls = []

    def opener(prefer_iso):
        calls.append(prefer_iso)
        return MockLink(kind="ISO15765", silent=True) if prefer_iso else MockLink(kind="raw CAN")
    s = L.Zf6hpUdsLiveSession(opener)
    s.connect()
    assert calls == [True, False] and s.link.kind == "raw CAN"


def test_multi_did_read_and_unsupported_did_is_dropped():
    s, seen, _links = _session()
    s.connect()
    del seen[:]
    res = s.read_groups([0xF40C, 0xF405, 0x0407, 0x1007, 0x2131])
    assert res[0xF40C][0].value == 750.0 and res[0x0407][3].value == 256.0
    assert res[0x1007][0].text == "AA BB CC"
    assert res[0x2131] is None and 0x2131 in s.unsupported
    # the three fixed-length DIDs went in one request
    assert seen[0] == b"\x22\xf4\x0c\xf4\x05\x04\x07" and s._batch_ok is True
    del seen[:]
    s.read_groups([0x2131])
    assert seen == []                       # not asked again this connection


def test_falls_back_to_single_dids_when_multi_did_is_refused():
    s, _seen, _links = _session(multi=False)
    s.connect()
    res = s.read_groups([0xF40C, 0xF405, 0x1024, 0x0285])
    assert s._batch_ok is False
    assert res[0xF405][0].value == 90.0 and res[0x1024][0].value == 0x1234
    assert res[0x0285][0].value == 11.9


def test_keepalive_reconnect_and_close():
    s, seen, links = _session()
    s.connect()
    s.keepalive()
    assert seen[-1] == b"\x3e\x00"
    s.reconnect()
    assert links[0][1].closed and len(links) == 2      # the old route is closed first
    s.close()
    assert links[1][1].closed and s.link is None


def test_session_declines_memory_reads():
    s, _seen, _links = _session()
    assert s.supports_memory_read is False and s.is_kwp2000 is False
    assert s.read_memory(0x180000, 16) is None


# ---------------------------------------------------------------- transport
class _FakeIo:
    def __init__(self):
        self.functional = []

    def functional_scan(self, request, listen_ms=500, func_id=None):
        self.functional.append(bytes(request))
        return {0x7E9: b"\x50\x01"} if request[:1] == b"\x10" else {}


def test_open_link_uses_in_bridge_isotp_and_wakes_the_gateway():
    from ovtsuite.flash import j2534 as J
    io = _FakeIo()
    bridge = object.__new__(J.BridgePassThru)
    bridge.open_iso_tp = lambda rate, **kw: io
    link = L.open_link(bridge, 500000)
    assert link.kind == "in-bridge ISO-TP" and link.io is io
    assert io.functional == [b"\x3e\x00", b"\x10\x01"]       # functional wake first


def test_open_link_on_a_direct_device_takes_the_flasher_transport(monkeypatch):
    from ovtsuite.flash import zf6hp_uds as U
    chan = object()
    monkeypatch.setattr(U, "open_6hp_transport",
                        lambda can, rate, **kw: (chan, "iso15765", True))
    link = L.open_link(object(), 500000)
    assert link.kind == "ISO15765" and isinstance(link.io, U.Iso15765Io)
    assert link.io.can is chan and link.io.tx_id == 0x7E1 and link.io.rx_id == 0x7E9
