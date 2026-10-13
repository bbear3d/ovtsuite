# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only; validates the ISO-TP layer against the real VCDS airbag capture
and the UDS probe against a synthetic mock 6HP28."""
import os
import re
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, os.pardir))

from ovtsuite.flash import zf6hp_uds as U   # noqa: E402

AIRBAG_LOG = os.path.join(HERE, os.pardir, "Reference", "Logs", "VCDS-Autoscan.log")


class _Msg:
    def __init__(self, can_id, payload):
        self.can_id = can_id
        self.payload = bytes(payload)


class ReplayCan:
    """Feeds a pre-recorded list of RESPONSE frames (id, bytes) back on read();
    records everything written. Used to replay a captured ECU side."""

    def __init__(self, resp_frames):
        self._resp = list(resp_frames)
        self.written = []

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        self.written.append((can_id, bytes(payload)))

    def read(self, max_msgs=16, timeout_ms=0):
        out, self._resp = self._resp[:max_msgs], self._resp[max_msgs:]
        return [_Msg(c, p) for c, p in out]

    def pass_ids(self, ids):
        pass


def _load_frames(req_id, resp_id):
    """Return (request_frames, response_frames) from the airbag capture."""
    reqs, resps = [], []
    for line in open(AIRBAG_LOG):
        m = re.match(r"\([\d.]+\) can ([0-9A-F]+)#([0-9A-F]*)", line)
        if not m:
            continue
        cid = int(m.group(1), 16)
        data = bytes.fromhex(m.group(2))
        if cid == req_id:
            reqs.append((cid, data))
        elif cid == resp_id:
            resps.append((cid, data))
    return reqs, resps


@pytest.mark.skipif(not os.path.exists(AIRBAG_LOG), reason="airbag capture not present")
def test_isotp_reassembles_real_multiframe_response():
    # The airbag (0x715 req / 0x77F resp) answers 22 F19E with a first-frame +
    # consecutive-frame message. Feed its real response frames through IsoTp
    # and confirm the reassembled UDS payload is the full 0x62 F19E ... string.
    _, resps = _load_frames(0x715, 0x77F)
    # take the first FF (1x..) and the CFs that follow it
    start = next(i for i, (_, d) in enumerate(resps) if d and (d[0] >> 4) == 1)
    length = ((resps[start][1][0] & 0x0F) << 8) | resps[start][1][1]
    frames = [resps[start]]
    need = length - 6
    j = start + 1
    while need > 0 and j < len(resps):
        d = resps[j][1]
        if d and (d[0] >> 4) == 2:
            frames.append(resps[j])
            need -= 7
        j += 1
    can = ReplayCan(frames)
    io = U.IsoTp(can, tx_id=0x715, rx_id=0x77F)
    msg = io.recv(timeout_ms=50)
    assert msg[0] == 0x62 and msg[1:3] == b"\xf1\x9e"
    assert len(msg) == length
    # the tester must have sent exactly one flow-control frame (30 00 01-ish)
    fcs = [p for cid, p in can.written if cid == 0x715 and (p[0] >> 4) == 3]
    assert len(fcs) == 1 and fcs[0][0] == 0x30


@pytest.mark.skipif(not os.path.exists(AIRBAG_LOG), reason="airbag capture not present")
def test_isotp_single_frame_roundtrip():
    # A single-frame response (06 50 03 00 4B 00 FA) decodes to the 6 UDS bytes.
    can = ReplayCan([(0x77F, bytes.fromhex("065003004B00FAAA"))])
    io = U.IsoTp(can, tx_id=0x715, rx_id=0x77F)
    msg = io.recv(timeout_ms=50)
    assert msg == bytes.fromhex("5003004B00FA")


def test_isotp_send_single_frame_is_padded_to_8():
    can = ReplayCan([])
    io = U.IsoTp(can, tx_id=0x7E1, rx_id=0x7E9, pad=0x55)
    io.send(bytes([0x22, 0xF1, 0x87]))
    cid, frame = can.written[0]
    assert cid == 0x7E1
    assert frame == bytes([0x03, 0x22, 0xF1, 0x87, 0x55, 0x55, 0x55, 0x55])


class MockUds6hp28:
    """A synthetic 6HP28-ish ECU on ISO-TP 0x7E1/0x7E9. Answers a fixed set of
    read-only services; segments long replies into FF+CF and honours the
    tester's flow control. Everything it exposes is read-only."""

    def __init__(self, req_id=0x7E1, resp_id=0x7E9):
        self.req_id = req_id
        self.resp_id = resp_id
        self.out = []
        self._rx = bytearray()
        self._need = None
        self.session = 0x01
        self.seed_sent = {}
        self.unlocked = set()

    def pass_ids(self, ids):
        pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        if can_id not in (self.req_id, 0x7DF):
            return
        d = bytes(payload)
        pci = d[0] >> 4
        if pci == 3:            # flow control from tester -- nothing to do
            return
        if pci == 0:            # single frame request
            self._handle(d[1:1 + (d[0] & 0x0F)])
        elif pci == 1:          # first frame (not used by this probe's requests)
            self._need = ((d[0] & 0x0F) << 8) | d[1]
            self._rx = bytearray(d[2:])
            self.out.append((self.resp_id, bytes([0x30, 0x00, 0x00])))
        elif pci == 2:
            self._rx += d[1:]
            if self._need and len(self._rx) >= self._need:
                self._handle(bytes(self._rx[:self._need]))
                self._need = None

    def _emit(self, resp):
        resp = bytes(resp)
        if len(resp) <= 7:
            self.out.append((self.resp_id, bytes([len(resp)]) + resp))
            return
        self.out.append((self.resp_id, bytes([0x10 | (len(resp) >> 8), len(resp) & 0xFF]) + resp[:6]))
        idx, sn = 6, 1
        while idx < len(resp):
            self.out.append((self.resp_id, bytes([0x20 | (sn & 0x0F)]) + resp[idx:idx + 7]))
            idx += 7
            sn = (sn + 1) & 0x0F

    def _handle(self, req):
        s = req[0]
        if s == 0x3E:
            return self._emit(bytes([0x7E, 0x00]))
        if s == 0x10:
            self.session = req[1]
            return self._emit(bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4]))
        if s == 0x22:
            did = (req[1] << 8) | req[2]
            table = {0xF187: b"0B6910258  ", 0xF189: b"0009", 0xF197: b"GS19 6HP28"}
            if did in table:
                return self._emit(bytes([0x62, req[1], req[2]]) + table[did])
            return self._emit(bytes([0x7F, 0x22, 0x31]))
        if s == 0x27:
            sub = req[1]
            if sub in (0x01, 0x03, 0x11, 0x05):          # requestSeed
                seed = bytes([0x11, 0x22, 0x33, 0x44])
                self.seed_sent[sub] = seed
                return self._emit(bytes([0x67, sub]) + seed)
            if sub in (0x02, 0x04, 0x12, 0x06):          # sendKey
                return self._emit(bytes([0x7F, 0x27, 0x35]))  # invalidKey -- we don't model the real key
        if s == 0x23:
            return self._emit(bytes([0x7F, 0x23, 0x31]))
        if s == 0x35:
            return self._emit(bytes([0x7F, 0x35, 0x31]))
        return self._emit(bytes([0x7F, s, 0x11]))

    def read(self, max_msgs=16, timeout_ms=0):
        out, self.out = self.out[:max_msgs], self.out[max_msgs:]
        return [_Msg(c, p) for c, p in out]


def test_probe_against_mock_reports_connected_and_identity():
    ecu = MockUds6hp28()
    io = U.IsoTp(ecu, tx_id=0x7E1, rx_id=0x7E9)
    probe = U.Zf6hpUdsProbe(io, seed_only=False)
    res = probe.run()
    assert res.connected is True
    assert "F187 VW spare part no" in res.identity
    assert "0B6910258" in res.identity["F187 VW spare part no"]
    # security requestSeed was positive on the modelled levels
    seed_steps = [s for s in res.steps if "requestSeed" in s.name and s.ok]
    assert seed_steps
    # a readable report renders
    assert "connected: yes" in res.report()


def test_probe_no_answer_reports_not_connected():
    class Dead:
        def pass_ids(self, ids):
            pass

        def write_can(self, *a, **k):
            pass

        def read(self, max_msgs=16, timeout_ms=0):
            return []

    io = U.IsoTp(Dead(), tx_id=0x7E1, rx_id=0x7E9)
    # short timeouts so the test is fast
    io._poll_spin_ms = 0.0
    probe = U.Zf6hpUdsProbe(io)
    probe.client.request = lambda *a, **k: (_ for _ in ()).throw(U.KLineTimeout("no response"))
    res = probe.run()
    assert res.connected is False


class MockUds6hp28Write(MockUds6hp28):
    """Extends the read mock with a programming session that unlocks write
    security (SA2) and answers the checksum routine + requestDownload/exit.
    Records whether transferData (0x36) or an erase is ever sent -- the
    write-path probe must send neither."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saw_transfer_data = False
        self.saw_erase = False
        self.saw_download = False
        self._wkey = None

    def _handle(self, req):
        from ovtsuite.flash.crypto import zf6hp_write_key
        s = req[0]
        if s == 0x27:
            sub = req[1]
            if sub == 0x11:                       # programming requestSeed
                seed = bytes([0x12, 0x34, 0x56, 0x78])
                self._wkey = zf6hp_write_key(seed)
                return self._emit(bytes([0x67, 0x11]) + seed)
            if sub == 0x12:                       # programming sendKey
                if self._wkey is not None and bytes(req[2:6]) == self._wkey:
                    self.unlocked.add(0x12)
                    return self._emit(bytes([0x67, 0x12]))
                return self._emit(bytes([0x7F, 0x27, 0x35]))
            # fall through for other subfunctions
            return super()._handle(req)
        if s == 0x85:                             # controlDTCSetting
            return self._emit(bytes([0xC5, req[1]]))
        if s == 0x28:                             # communicationControl
            return self._emit(bytes([0x68, req[1]]))
        if s == 0x31:                             # routineControl
            sub = req[1]; rid = (req[2] << 8) | req[3]
            if rid == 0xFF00:                     # eraseMemory -- MUST NOT happen
                self.saw_erase = True
                return self._emit(bytes([0x7F, 0x31, 0x31]))
            if 0x12 not in self.unlocked:
                return self._emit(bytes([0x7F, 0x31, 0x33]))   # securityAccessDenied
            if rid in (0x0203, 0xFF01):           # read-only programming gates
                if sub == 0x01:
                    return self._emit(bytes([0x71, 0x01, req[2], req[3]]))
                if sub == 0x03:
                    return self._emit(bytes([0x71, 0x03, req[2], req[3], 0x00]))
            if rid == 0x0202:                     # checksum/verify (reads flash)
                # expect VAG data form: 01 <block_id> 00 04 <expected:4>
                if sub == 0x01:
                    assert req[4] == 0x01 and req[6] == 0x00 and req[7] == 0x04
                    return self._emit(bytes([0x71, 0x01, 0x02, 0x02]))
                if sub == 0x03:                   # result: a status byte
                    return self._emit(bytes([0x71, 0x03, 0x02, 0x02, 0x00]))
            return self._emit(bytes([0x7F, 0x31, 0x31]))
        if s == 0x34:                             # requestDownload -- MUST NOT happen
            self.saw_download = True
            return self._emit(bytes([0x7F, 0x34, 0x33]))
        if s == 0x36:                             # transferData -- MUST NOT happen
            self.saw_transfer_data = True
            return self._emit(bytes([0x7F, 0x36, 0x24]))
        return super()._handle(req)


def test_write_path_probe_is_nondestructive_and_reaches_the_write_machinery():
    ecu = MockUds6hp28Write()
    io = U.IsoTp(ecu, tx_id=0x7E1, rx_id=0x7E9)
    io._poll_spin_ms = 0.0
    probe = U.Zf6hpUdsProbe(io, probe_write=True)
    res = probe.run()
    names = [s.name for s in res.steps]
    oks = {s.name: s.ok for s in res.steps}
    # write security unlocked in programming
    assert any("sendKey via SA2 tape [prog]" in n for n in names)
    assert probe._write_unlocked is True
    # the two read-only programming gates ran
    assert any(n.startswith("routineStart 0x0203") for n in names)
    assert any(n.startswith("routineStart 0xFF01") for n in names)
    # the per-block checksum/verify routine ran for every block (0x01..0x04)
    assert sum(n.startswith("routineStart 0x0202") for n in names) == 4
    assert all(oks[n] for n in names if n.startswith("routineStart 0x0202"))
    # the bus was restored at the end
    assert any("enableRxTx" in n for n in names)
    assert any("controlDTCSetting on" in n for n in names)
    # the destructive / download services were NEVER sent
    assert ecu.saw_transfer_data is False
    assert ecu.saw_erase is False
    assert ecu.saw_download is False
    # no requestDownload (0x34) step is even attempted any more
    assert not any("requestDownload" in n for n in names)
    # read services were retried AFTER the programming preconditions, and in
    # that order (the FF01 gate precedes the retried read)
    assert any(n.startswith("readMemByAddr") and "after preconditions" in n
               for n in names)
    i_dep = next(i for i, n in enumerate(names)
                 if n.startswith("routineStart 0xFF01"))
    i_read = next(i for i, n in enumerate(names)
                  if n.startswith("readMemByAddr") and "after preconditions" in n)
    assert i_dep < i_read


def test_write_path_probe_skipped_when_not_opted_in():
    ecu = MockUds6hp28Write()
    io = U.IsoTp(ecu, tx_id=0x7E1, rx_id=0x7E9)
    io._poll_spin_ms = 0.0
    probe = U.Zf6hpUdsProbe(io)              # probe_write defaults False
    res = probe.run()
    names = [s.name for s in res.steps]
    assert not any(n.startswith("routineStart 0x0202") for n in names)
    assert not any("requestDownload" in n for n in names)
    assert ecu.saw_transfer_data is False
    assert ecu.saw_download is False


# ----------------------------------------------------- ISO15765 (device ISO-TP)
from ovtsuite.flash import j2534 as _J          # noqa: E402


class _FakeIso15765Chan:
    """Minimal ISO15765 channel: records message writes and returns queued
    reassembled responses, optionally preceded by a START_OF_MESSAGE indication
    (which the reader must skip, as a real DLL emits one per message)."""
    def __init__(self):
        self.writes = []
        self._queue = []
        self.flushes = 0

    def write(self, data, flags=0, timeout_ms=100):
        self.writes.append(bytes(data))

    def read(self, max_msgs=8, timeout_ms=100):
        out = self._queue[:max_msgs]
        del self._queue[:max_msgs]
        return out

    def clear_rx(self):
        self.flushes += 1

    def clear_tx(self):
        pass

    def queue(self, rx_id, payload, with_start=True):
        p = bytes(payload)
        if with_start:
            self._queue.append(_J.Msg(_J.ISO15765, _J.START_OF_MESSAGE, 0, 0,
                                      rx_id.to_bytes(4, "big") + p[:1]))
        self._queue.append(_J.Msg(_J.ISO15765, 0, 0, 0, rx_id.to_bytes(4, "big") + p))


def test_iso15765_io_send_flushes_and_frames_the_id():
    ch = _FakeIso15765Chan()
    io = U.Iso15765Io(ch, tx_id=0x7E1, rx_id=0x7E9)
    io.send(bytes([0x10, 0x01]))
    assert ch.writes[-1] == (0x7E1).to_bytes(4, "big") + bytes([0x10, 0x01])
    assert ch.flushes >= 1                     # CLEAR_RX before the request (OEM recovery)


def test_iso15765_io_recv_skips_start_indication():
    ch = _FakeIso15765Chan()
    io = U.Iso15765Io(ch, tx_id=0x7E1, rx_id=0x7E9)
    full = bytes([0x50, 0x01, 0x00, 0x32, 0x01, 0xF4])
    ch.queue(0x7E9, full, with_start=True)
    assert io.recv(500) == full                # the START_OF_MESSAGE frame is ignored


def test_iso15765_client_rides_response_pending():
    ch = _FakeIso15765Chan()
    io = U.Iso15765Io(ch, tx_id=0x7E1, rx_id=0x7E9)
    client = U.Zf6hpUdsClient(io)
    ch.queue(0x7E9, bytes([0x7F, 0x23, 0x78]), with_start=False)   # responsePending
    ch.queue(0x7E9, bytes([0x63]) + bytes(240), with_start=True)   # final 0x23 reply
    resp = client.request(bytes([0x23, 0x14, 0x00, 0x18, 0x00, 0x00, 0xF0]),
                          timeout_ms=500, overall_ms=3000, fail_fast_on_silence=False)
    assert resp[0] == 0x63 and len(resp) == 241


def test_connect_iso15765_raises_on_unsupported_device():
    class _NoIso:
        def connect(self, proto, rate, flags=0):
            raise _J.J2534Error("ERR_INVALID_PROTOCOL_ID")
    with pytest.raises(Exception):
        U.connect_iso15765(_NoIso(), 500000)


def test_connect_iso15765_tears_down_channel_on_filter_failure():
    closed = {"n": 0}

    class _Chan:
        def add_filter(self, *a, **k):
            raise _J.J2534Error("ERR_INVALID_FILTER_ID")
        def disconnect(self):
            closed["n"] += 1

    class _Dev:
        def connect(self, proto, rate, flags=0):
            return _Chan()

    with pytest.raises(Exception):
        U.connect_iso15765(_Dev(), 500000)
    assert closed["n"] == 1                    # channel was disconnected before fallback


# ------------------------------------------------ gateway wake + transport pick
class _FakeWakeIo:
    """Stands in for an IsoTp whose functional_scan returns a canned reply map,
    so wake_gateway can be tested without wall-clock listen windows."""
    def __init__(self, replies):
        self.replies = replies
        self.requests = []

    def functional_scan(self, request, listen_ms=500, func_id=None):
        self.requests.append(bytes(request))
        return dict(self.replies)


def test_wake_gateway_reports_routing_open_on_reply():
    io = _FakeWakeIo({0x7E9: bytes([0x50, 0x01, 0x00, 0x32, 0x01, 0xF4])})
    assert U.wake_gateway(io) is True
    # It sends BOTH a functional testerPresent and a functional default session.
    assert io.requests == [bytes([0x3E, 0x00]), bytes([0x10, 0x01])]


def test_wake_gateway_reports_asleep_on_silence():
    assert U.wake_gateway(_FakeWakeIo({})) is False


class _FakeRawChan:
    def __init__(self):
        self.disconnected = 0
        self.passed_all = 0
    def pass_all(self):
        self.passed_all += 1
    def clear_rx(self):
        pass
    def disconnect(self):
        self.disconnected += 1


class _FakeIsoChan:
    def __init__(self):
        self.filters = []
        self.disconnected = 0
    def add_filter(self, *a, **k):
        self.filters.append((a, k))
    def clear_rx(self):
        pass
    def disconnect(self):
        self.disconnected += 1


class _FakeDev:
    """connect(proto, rate) hands back a raw CAN or ISO15765 channel; ISO15765
    can be made to raise to exercise the raw-CAN fallback."""
    def __init__(self, raw, iso=None, iso_raises=False):
        self.raw = raw
        self.iso = iso
        self.iso_raises = iso_raises
        self.connects = []
    def connect(self, proto, rate, flags=0):
        self.connects.append(proto)
        if proto == _J.ISO15765:
            if self.iso_raises:
                raise _J.J2534Error("ERR_INVALID_PROTOCOL_ID")
            return self.iso
        return self.raw


def test_open_6hp_transport_raw_only_does_not_wake():
    raw = _FakeRawChan()
    dev = _FakeDev(raw)
    ch, kind, awake = U.open_6hp_transport(dev, 500000, prefer_iso15765=False)
    assert (ch, kind, awake) == (raw, "raw", False)
    assert dev.connects == [_J.CAN]            # no ISO15765 attempt, no wake connect


def test_open_6hp_transport_wakes_over_raw_then_upgrades_to_iso15765(monkeypatch):
    woke = {"n": 0}
    monkeypatch.setattr(U, "wake_gateway", lambda io, log=None: (woke.__setitem__("n", woke["n"] + 1) or True))
    raw, iso = _FakeRawChan(), _FakeIsoChan()
    dev = _FakeDev(raw, iso=iso)
    ch, kind, awake = U.open_6hp_transport(dev, 500000, prefer_iso15765=True)
    assert (ch, kind, awake) == (iso, "iso15765", True)
    assert woke["n"] == 1                       # gateway woken over raw CAN first
    assert raw.disconnected == 1                # raw wake channel released before ISO15765
    assert iso.filters                          # flow-control filter set on the ISO15765 channel
    assert dev.connects == [_J.CAN, _J.ISO15765]


def test_open_6hp_transport_falls_back_to_raw_when_iso_unavailable(monkeypatch):
    monkeypatch.setattr(U, "wake_gateway", lambda io, log=None: True)
    raw = _FakeRawChan()
    dev = _FakeDev(raw, iso_raises=True)
    ch, kind, awake = U.open_6hp_transport(dev, 500000, prefer_iso15765=True)
    assert kind == "raw" and awake is False and ch is raw


def _physical_session_frames():
    # testerPresent ack, then default-session positive response (both single-frame)
    return [(0x7E9, bytes([0x02, 0x7E, 0x00])),
            (0x7E9, bytes([0x06, 0x50, 0x01, 0x00, 0x32, 0x01, 0xF4]))]


def test_reader_assume_awake_skips_functional_wake():
    io = U.IsoTp(ReplayCan(_physical_session_frames()))
    reader = U.Zf6hpUdsReader(io, assume_awake=True)
    calls = []
    reader._functional_wake = lambda: calls.append(1)
    reader._connect()
    assert calls == []                          # wake skipped -- gateway already open


def test_reader_without_assume_awake_runs_functional_wake():
    io = U.IsoTp(ReplayCan(_physical_session_frames()))
    reader = U.Zf6hpUdsReader(io, assume_awake=False)
    calls = []
    reader._functional_wake = lambda: calls.append(1)
    reader._connect()
    assert calls == [1]                         # reader opens routing itself
