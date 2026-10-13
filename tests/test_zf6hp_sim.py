# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only unit tests for the bench 6HP28 simulator: the GS19 response model
(direct) and the ISO-TP framing (a threaded in-memory bus driving the real
tester-side IsoTp against the ECU-side IsoTpEcu)."""
import collections
import os
import sys
import threading
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "work"))

from ovtsuite.flash import zf6hp_sim as S          # noqa: E402
from ovtsuite.flash import zf6hp_uds as U          # noqa: E402
from ovtsuite.flash import crypto as C             # noqa: E402

_Frame = collections.namedtuple("Frame", "can_id payload is_tx_echo")


# ---------------------------------------------------------------- responder --
def _mk(**kw):
    log = []
    r = S.Gs19Responder(log=log.append, **kw)
    return r, log


def test_session_and_identity():
    r, _ = _mk()
    assert r.handle(bytes([0x10, 0x01]))[:2] == bytes([0x50, 0x01])
    resp = r.handle(bytes([0x22, 0xF1, 0x87]))
    assert resp[:3] == bytes([0x62, 0xF1, 0x87])
    assert resp[3:].startswith(b"8K0927158C")
    # tester-present and an unknown DID
    assert r.handle(bytes([0x3E, 0x00])) == bytes([0x7E, 0x00])
    assert r.handle(bytes([0x22, 0x99, 0x99]))[:1] == bytes([0x7F])


def test_read_security_seed_key_roundtrip():
    r, log = _mk()
    seed_resp = r.handle(bytes([0x27, 0x03]))
    assert seed_resp[:2] == bytes([0x67, 0x03])
    seed = seed_resp[2:]
    key = bytes(C.zf6hp_read_key(seed))
    ok = r.handle(bytes([0x27, 0x04]) + key)
    assert ok == bytes([0x67, 0x04])
    assert 0x04 in r.unlocked
    assert any("MATCH" in l for l in log)


def test_write_security_sa2_roundtrip():
    r, log = _mk()
    seed = r.handle(bytes([0x27, 0x11]))[2:]
    key = bytes(C.zf6hp_write_key(seed))
    assert r.handle(bytes([0x27, 0x12]) + key) == bytes([0x67, 0x12])
    assert any("MATCH" in l for l in log)


def test_strict_key_rejects_wrong_key():
    r, _ = _mk(strict_key=True)
    r.handle(bytes([0x27, 0x03]))
    bad = r.handle(bytes([0x27, 0x04, 0x00, 0x00, 0x00, 0x00]))
    assert bad == bytes([0x7F, 0x27, 0x35])          # invalidKey
    # serve mode (non-strict) accepts anyway, but still logs the mismatch
    r2, log2 = _mk()
    r2.handle(bytes([0x27, 0x03]))
    assert r2.handle(bytes([0x27, 0x04, 0, 0, 0, 0])) == bytes([0x67, 0x04])
    assert any("MISMATCH" in l for l in log2)


def test_serve_mode_reads_from_image():
    img = bytes((i & 0xFF) for i in range(0x200))
    r, _ = _mk(image=img, read_mode="serve")
    # 23 ALFID=0x14 (addr 4 bytes, size 1 byte) @0x10 len 0x20
    req = bytes([0x23, 0x14]) + (0x10).to_bytes(4, "big") + bytes([0x20])
    resp = r.handle(req)
    assert resp[0] == 0x63
    assert resp[1:] == img[0x10:0x30]


def test_faithful_mode_refuses_read_like_the_car():
    r, _ = _mk(image=b"\x00" * 0x100, read_mode="faithful")
    req = bytes([0x23, 0x14]) + (0).to_bytes(4, "big") + bytes([0x10])
    assert r.handle(req) == bytes([0x7F, 0x23, 0x7F])   # serviceNotSupportedInActiveSession


def test_request_upload_then_transfer_serves_image():
    img = bytes((i * 7 & 0xFF) for i in range(0x300))
    r, _ = _mk(image=img, read_mode="serve")
    up = r.handle(bytes([0x35, 0x00, 0x14]) + (0).to_bytes(4, "big") + bytes([0x80]))
    assert up[0] == 0x75
    t = r.handle(bytes([0x36, 0x01]))
    assert t[0] == 0x76 and t[1] == 0x01
    assert t[2:] == img[0:0x80]                     # first chunk, capped by upload size
    assert r.handle(bytes([0x37])) == bytes([0x77])


# -------------------------------------------------------------- ISO-TP bus --
class _Endpoint:
    def __init__(self, out_q, in_q, lock):
        self._out, self._in, self._lock = out_q, in_q, lock

    def pass_ids(self, *a, **k):
        pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        with self._lock:
            self._out.append((int(can_id), bytes(payload)))

    def read(self, max_msgs=32, timeout_ms=100):
        # Mirror a real J2534 read: with timeout_ms=0 (how IsoTp._next_frame
        # polls) return whatever is buffered immediately, never block past the
        # deadline, but always check the queue at least once.
        end = time.time() + timeout_ms / 1000.0
        out = []
        while True:
            with self._lock:
                while self._in and len(out) < max_msgs:
                    cid, data = self._in.popleft()
                    out.append(_Frame(cid, data, False))
            if out or time.time() >= end:
                return out
            time.sleep(0.001)


def _bus():
    t2e, e2t, lock = collections.deque(), collections.deque(), threading.Lock()
    tester = _Endpoint(t2e, e2t, lock)      # writes t2e, reads e2t
    ecu = _Endpoint(e2t, t2e, lock)         # writes e2t, reads t2e
    return tester, ecu


def test_isotp_single_frame_request_multiframe_response_over_bus():
    img = bytes((i & 0xFF) for i in range(0x100))
    tester_can, ecu_can = _bus()
    io_ecu = S.IsoTpEcu(ecu_can, rx_id=0x7E1, tx_id=0x7E9)
    sim = S.Simulator(io_ecu, S.Gs19Responder(image=img, read_mode="serve"))
    stop = threading.Event()
    th = threading.Thread(target=sim.run, kwargs={"is_stop": stop.is_set, "idle_ms": 10})
    th.start()
    try:
        io = U.IsoTp(tester_can, tx_id=0x7E1, rx_id=0x7E9)
        # single-frame request
        io.send(bytes([0x10, 0x01]))
        assert io.recv(timeout_ms=2000)[:2] == bytes([0x50, 0x01])
        # read 0x40 bytes -> a multi-frame response, exercising FF/FC/CF
        io.send(bytes([0x23, 0x14]) + (0).to_bytes(4, "big") + bytes([0x40]))
        resp = io.recv(timeout_ms=2000)
        assert resp[0] == 0x63 and resp[1:] == img[0:0x40]
    finally:
        stop.set()
        th.join(timeout=2)
    assert sim.count >= 2


# ------------------------------------------------------------ profiles (ecu_sim)
from ovtsuite.flash import ecu_sim as E          # noqa: E402


def test_profile_registry_lists_modules():
    names = E.profile_names()
    assert "6hp28" in names and "generic" in names
    assert E.profile("6hp28").title.startswith("ZF 6HP28")
    # unknown name falls back, never raises
    assert E.profile("nope").name in ("6hp28",)


def test_generic_profile_accepts_any_key_and_serves():
    img = bytes((i & 0xFF) for i in range(0x80))
    r = E.make_responder("generic", image=img, read_mode="serve")
    assert r.handle(bytes([0x22, 0xF1, 0x87]))[:3] == bytes([0x62, 0xF1, 0x87])
    # no key algorithm in the generic profile -> any key is accepted (logged)
    r.handle(bytes([0x27, 0x03]))
    assert r.handle(bytes([0x27, 0x04, 0xDE, 0xAD])) == bytes([0x67, 0x04])
    req = bytes([0x23, 0x14]) + (0).to_bytes(4, "big") + bytes([0x10])
    assert r.handle(req)[1:] == img[0:0x10]


def test_profile_ids_override_and_6hp28_keys():
    p = E.profile("6hp28")
    assert (p.rx_id, p.tx_id) == (0x7E1, 0x7E9)
    r = E.EcuResponder(p)
    seed = r.handle(bytes([0x27, 0x11]))[2:]
    import ovtsuite.flash.crypto as C
    assert r.handle(bytes([0x27, 0x12]) + bytes(C.zf6hp_write_key(seed))) == bytes([0x67, 0x12])


def test_set_rx_filters_prefers_pass_ids():
    calls = {}
    class HasPassIds:
        def pass_ids(self, ids):
            calls["ids"] = list(ids)
    assert E.set_rx_filters(HasPassIds(), [0x7E1, 0x7DF]) == "pass_ids"
    assert calls["ids"] == [0x7E1, 0x7DF]


def test_set_rx_filters_uses_add_filter_without_pass_ids():
    # the 32-bit bridge / OpenPort channel case: no pass_ids -> per-id filters
    seen = []
    class BridgeLike:
        def clear_filters(self):
            seen.append("clear")
        def add_filter(self, ftype, mask, pattern, flags=0, flow=None):
            seen.append(int.from_bytes(pattern, "big"))
    assert E.set_rx_filters(BridgeLike(), [0x7E1, 0x7DF]) == "add_filter"
    assert 0x7E1 in seen and 0x7DF in seen


def test_set_rx_filters_last_resort_pass_all():
    hits = []
    class Minimal:
        def pass_all(self, extended=False):
            hits.append("all")
    assert E.set_rx_filters(Minimal(), [0x7E1]) == "pass_all"
    assert hits == ["all"]


def test_uds_reader_reads_calibration_over_bus():
    """End-to-end: Zf6hpUdsReader (tester) drives the captured read sequence
    (10 01 -> 10 4F -> 27 03/04 -> 23 14 ...) against the EcuResponder serving a
    2 MB image, over the threaded bus. Confirms the bytes come back correct."""
    img = bytes((i * 7 & 0xFF) for i in range(0x200000))
    tester_can, ecu_can = _bus()
    io_ecu = S.IsoTpEcu(ecu_can, rx_id=0x7E1, tx_id=0x7E9)
    sim = S.Simulator(io_ecu, S.Gs19Responder(image=img, read_mode="serve"))
    stop = threading.Event()
    th = threading.Thread(target=sim.run, kwargs={"is_stop": stop.is_set, "idle_ms": 5})
    th.start()
    try:
        io = U.IsoTp(tester_can, tx_id=0x7E1, rx_id=0x7E9)
        reader = U.Zf6hpUdsReader(io)
        base = U.CAL_READ_BASE
        cal = reader.read_calibration(base, base + 0x240)   # two 0xF0 chunks
        assert cal == img[base:base + 0x240]
    finally:
        stop.set()
        th.join(timeout=3)


def test_write_capture_accepts_download_transfer_and_logs_checksum():
    """The sim accepts a full write choreography (so the tool completes) and
    logs the 0x0202 expected-checksum -- the capture we want."""
    import ovtsuite.flash.crypto as C
    r, log = _mk()                                  # 6hp28 profile, serve mode
    r.handle(bytes([0x10, 0x02]))                   # programming session
    seed = r.handle(bytes([0x27, 0x11]))[2:]        # write seed
    assert r.handle(bytes([0x27, 0x12]) + bytes(C.zf6hp_write_key(seed))) == bytes([0x67, 0x12])
    assert r.handle(bytes([0x31, 0x01, 0xFF, 0x00, 0x01, 0x04]))[0] == 0x71   # erase
    rd = r.handle(bytes([0x34, 0x11, 0x41, 0x04]) + (0x1000).to_bytes(4, "big"))
    assert rd[0] == 0x74 and r._download is not None
    assert r.handle(bytes([0x36, 0x01]) + b"\xAA" * 0x40) == bytes([0x76, 0x01])
    assert r.handle(bytes([0x36, 0x02]) + b"\xBB" * 0x40) == bytes([0x76, 0x02])
    assert r._download["received"] == 0x80 and r._download["frames"] == 2
    assert r.handle(bytes([0x37])) == bytes([0x77]) and r._download is None
    chk = bytes([0x01, 0x04, 0x00, 0x04]) + b"\xDE\xAD\xBE\xEF"
    assert r.handle(bytes([0x31, 0x01, 0x02, 0x02]) + chk)[0] == 0x71
    assert any("EXPECTED CHECKSUM" in l and "deadbeef" in l.lower() for l in log)


def test_write_data_by_identifier_accepted():
    """ODIS writes a fingerprint (2E F15A ...) right after unlock; the sim must
    accept it (6E) or the tool aborts before the flash (the 1st write attempt)."""
    r, log = _mk()
    resp = r.handle(bytes([0x2E, 0xF1, 0x5A, 0, 0, 0, 0, 0, 0, 0xAA, 0xAA, 0xAA]))
    assert resp == bytes([0x6E, 0xF1, 0x5A])
    assert any("writeDataByIdentifier 0xF15A" in l for l in log)


# --------------------------------------------------------- write capture ----
def test_write_capture_decodes_and_saves_transferred_block(tmp_path):
    """A full requestDownload -> transferData -> transferExit for one block is
    captured: the raw wire stream and the BCB-decoded plaintext are saved, and
    the decoded plaintext (hence its CRC-32, the value the real 0x0202 routine
    checks) round-trips back to exactly what was sent."""
    import zlib
    plaintext = bytes(range(256)) * 6 + b"\xFF" * 2000 + bytes((i * 3) & 0xFF for i in range(500))
    stream = C.zf6hp_bcb_encode(plaintext)
    log = []
    r = S.Gs19Responder(capture_dir=str(tmp_path), log=log.append)
    blk = 0x04
    req = bytes([0x34, 0x11, 0x41, blk]) + len(plaintext).to_bytes(4, "big")
    assert r.handle(req)[0] == 0x74
    # one transferData frame carrying the whole stream (chunking is irrelevant here)
    assert r.handle(bytes([0x36, 0x01]) + stream)[:2] == bytes([0x76, 0x01])
    assert r.handle(bytes([0x37])) == bytes([0x77])
    raw = (tmp_path / ("xfer-blk0x%02X-raw.bin" % blk)).read_bytes()
    plain = (tmp_path / ("xfer-blk0x%02X-plain.bin" % blk)).read_bytes()
    assert raw == stream
    assert plain == plaintext
    assert any(("0x%08X" % (zlib.crc32(plaintext) & 0xFFFFFFFF)) in l for l in log)


# ------------------------------------------------------- real-car timing (bench)
class _TimingCan:
    """Minimal CAN stub: records TX frames with timestamps and hands back a
    single flow-control frame so IsoTpEcu.send() proceeds past _await_fc."""
    def __init__(self, fc=b"\x30\x00\x00"):
        self.writes = []
        self._fc = fc
        self._gave_fc = False

    def write_can(self, can_id, payload, *a, **k):
        self.writes.append((time.monotonic(), can_id, bytes(payload)))

    def read(self, max_msgs=32, timeout_ms=100):
        if not self._gave_fc:
            self._gave_fc = True
            return [_Frame(0x7E1, self._fc + b"\x55\x55\x55\x55\x55", False)]
        return []


def test_sim_timing_dataclass_active_and_jitter():
    assert not E.SimTiming().active()
    assert E.SimTiming(cf_gap_ms=10).active()
    assert E.SimTiming(drop_resp_prob=0.5).active()
    t = E.SimTiming(jitter_ms=5)
    for _ in range(200):
        assert 5.0 <= t.jitter(10.0) <= 15.0     # 10 +/- 5
    assert t.jitter(0.0) == 0.0                   # no jitter on a zero base


def test_sim_cf_gap_paces_consecutive_frames():
    can = _TimingCan()
    io = S.IsoTpEcu(can, rx_id=0x7E1, tx_id=0x7E9,
                    timing=E.SimTiming(cf_gap_ms=30))
    t0 = time.monotonic()
    io.send(bytes(range(20)))          # FF + 2 consecutive frames => 1 inter-frame gap
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.025            # at least ~one 30 ms gap
    assert len([w for w in can.writes if w[1] == 0x7E9]) == 3   # FF + 2 CF sent


def test_sim_drop_whole_response_sends_nothing():
    can = _TimingCan()
    io = S.IsoTpEcu(can, rx_id=0x7E1, tx_id=0x7E9,
                    timing=E.SimTiming(drop_resp_prob=1.0))
    sent = []
    io.send = lambda resp: sent.append(resp)       # observe send attempts
    log = []
    sim = S.Simulator(io, S.Gs19Responder(read_mode="serve"), log=log.append)
    sim.step(bytes([0x10, 0x01]))                  # would normally reply 50 01 ...
    assert sent == []                              # response was dropped
    assert any("DROPPED whole response" in l for l in log)


def test_sim_no_timing_still_replies():
    can = _TimingCan()
    io = S.IsoTpEcu(can, rx_id=0x7E1, tx_id=0x7E9)   # default: no timing
    sent = []
    io.send = lambda resp: sent.append(resp)
    sim = S.Simulator(io, S.Gs19Responder(read_mode="serve"))
    sim.step(bytes([0x10, 0x01]))
    assert sent and sent[0][:2] == bytes([0x50, 0x01])


# ------------------- in-bridge ISO-TP drain (j2534_bridge iso_* ops) ---------
# The 6HP28 read over the 32-bit bridge now runs the ISO-TP engine IN-BRIDGE:
# one IPC per chunk (send + reassemble + ride responsePending next to the DLL)
# instead of ~35 per-frame read round-trips. These tests drive BridgeIsoTp
# (client) and _iso_request (bridge) end-to-end against the simulator, without a
# 32-bit subprocess or a real DLL.
import pytest                                          # noqa: E402
from ovtsuite.flash import j2534 as J2534              # noqa: E402
from ovtsuite.flash import j2534_bridge as JB          # noqa: E402
from ovtsuite.flash import isotp_transport as IT       # noqa: E402


class _FakeBridge:
    """Routes the iso_* ops to the REAL in-bridge helpers over a given in-bridge
    IsoTp, so the 64-bit BridgeIsoTp can be exercised without a subprocess."""
    def __init__(self, io):
        self.io = io
        self.ops = []

    def _call(self, op, **kw):
        self.ops.append(op)
        if op == "iso_request":
            resp = JB._iso_request(
                self.io, bytes.fromhex(kw["payload"]), can_id=kw.get("can_id"),
                timeout_ms=kw.get("timeout_ms", 2000), p2_star_ms=kw.get("p2_star_ms", 5000),
                overall_ms=kw.get("overall_ms", 20000), fail_fast=kw.get("fail_fast", True))
            return {"resp": resp.hex() if resp is not None else None}
        if op == "iso_request_keepalive":
            resp = JB._iso_request_keepalive(
                self.io, bytes.fromhex(kw["payload"]),
                keepalive_interval_s=kw.get("keepalive_interval_s", 1.0),
                resend_interval_s=kw.get("resend_interval_s", 3.5),
                poll_ms=kw.get("poll_ms", 500), overall_ms=kw.get("overall_ms", 60000))
            return {"resp": resp.hex() if resp is not None else None}
        if op == "iso_functional":
            res = self.io.functional_scan(bytes.fromhex(kw["request"]),
                                          listen_ms=kw.get("listen_ms", 300),
                                          func_id=kw.get("func_id", 0x7DF))
            return {("%X" % cid): v.hex() for cid, v in res.items()}
        if op in ("iso_flush", "iso_close"):
            return None
        raise AssertionError("unexpected bridge op %r" % op)


def test_inbridge_isotp_reader_reads_calibration_over_bus():
    """End-to-end: Zf6hpUdsReader -> Zf6hpUdsClient native hook -> BridgeIsoTp ->
    the real _iso_request -> the real in-bridge IsoTp <-> sim ECU. The in-bridge
    drain must reassemble the same bytes the per-frame path does."""
    img = bytes((i * 7 & 0xFF) for i in range(0x200000))
    tester_can, ecu_can = _bus()
    io_ecu = S.IsoTpEcu(ecu_can, rx_id=0x7E1, tx_id=0x7E9)
    sim = S.Simulator(io_ecu, S.Gs19Responder(image=img, read_mode="serve"))
    stop = threading.Event()
    th = threading.Thread(target=sim.run, kwargs={"is_stop": stop.is_set, "idle_ms": 5})
    th.start()
    try:
        inbridge = IT.IsoTp(tester_can, tx_id=0x7E1, rx_id=0x7E9)   # the engine the bridge runs
        fake = _FakeBridge(inbridge)
        bridge_io = J2534.BridgeIsoTp(fake, tx_id=0x7E1, rx_id=0x7E9)
        reader = U.Zf6hpUdsReader(bridge_io, assume_awake=True)     # sim doesn't route 0x7DF
        base = U.CAL_READ_BASE
        cal = reader.read_calibration(base, base + 0x240)           # two 0xF0 chunks, in-bridge drain
        assert cal == img[base:base + 0x240]
        assert "iso_request" in fake.ops                            # went through the in-bridge path
    finally:
        stop.set()
        th.join(timeout=3)


def test_bridge_isotp_request_timeout_raises_kline_timeout():
    from ovtsuite.flash.transport import KLineTimeout

    class _B:
        def _call(self, op, **kw):
            return {"resp": None}                                   # bridge saw no answer

    io = J2534.BridgeIsoTp(_B())
    with pytest.raises(KLineTimeout):
        io.request(b"\x3e\x00", overall_ms=100)


def test_client_request_delegates_to_native_inbridge_transport():
    """Zf6hpUdsClient.request must delegate to a transport that exposes its own
    request() (BridgeIsoTp), so the whole exchange is one in-bridge IPC."""
    calls = []

    class _NativeIo:
        def request(self, payload, **kw):
            calls.append((bytes(payload), kw))
            return b"\x50\x01"

    client = U.Zf6hpUdsClient(_NativeIo())
    assert client.request(b"\x10\x01", overall_ms=1234) == b"\x50\x01"
    assert calls[0][0] == b"\x10\x01" and calls[0][1]["overall_ms"] == 1234


def test_client_request_with_keepalive_delegates_to_native():
    """A silent-erase keepalive must also delegate to the in-bridge transport so
    the whole loop runs next to the DLL."""
    calls = []

    class _NativeIo:
        def request_with_keepalive(self, payload, **kw):
            calls.append((bytes(payload), kw))
            return b"\x71\x01"

    client = U.Zf6hpUdsClient(_NativeIo())
    assert client.request_with_keepalive(b"\x31\x01\xff\x00", overall_ms=4321) == b"\x71\x01"
    assert calls[0][0] == b"\x31\x01\xff\x00" and calls[0][1]["overall_ms"] == 4321


def test_inbridge_isotp_write_streams_multiframe_transferdata_over_bus():
    """The write path over the in-bridge transport: programming session + write
    unlock, erase via the in-bridge keepalive op, requestDownload, and a
    MULTI-FRAME transferData streamed in-bridge (FF + FC + CF) that the sim
    reassembles and ACKs. Exercises _iso_request, _iso_request_keepalive and the
    BridgeIsoTp request/keepalive hooks end-to-end."""
    from ovtsuite.flash import crypto as C2
    tester_can, ecu_can = _bus()
    io_ecu = S.IsoTpEcu(ecu_can, rx_id=0x7E1, tx_id=0x7E9)
    sim = S.Simulator(io_ecu, S.Gs19Responder(image=bytes(0x2000), read_mode="serve"))
    stop = threading.Event()
    th = threading.Thread(target=sim.run, kwargs={"is_stop": stop.is_set, "idle_ms": 5})
    th.start()
    try:
        inbridge = IT.IsoTp(tester_can, tx_id=0x7E1, rx_id=0x7E9)
        bio = J2534.BridgeIsoTp(_FakeBridge(inbridge), tx_id=0x7E1, rx_id=0x7E9)
        client = U.Zf6hpUdsClient(bio)
        assert client.request(bytes([0x10, 0x02]))[0] == 0x50          # programming session
        seed = client.request(bytes([0x27, 0x11]))[2:]                 # write seed
        assert client.request(bytes([0x27, 0x12]) + bytes(C2.zf6hp_write_key(seed)))[0] == 0x67
        er = client.request_with_keepalive(bytes([0x31, 0x01, 0xFF, 0x00, 0x01, 0x04]),
                                           overall_ms=5000)             # erase (in-bridge keepalive)
        assert er[0] == 0x71
        rd = client.request(bytes([0x34, 0x11, 0x41, 0x04]) + (0x200).to_bytes(4, "big"))
        assert rd[0] == 0x74                                            # requestDownload
        block = bytes((i & 0xFF) for i in range(0x100))                # 256B -> FF + CF (multi-frame)
        assert client.request(bytes([0x36, 0x01]) + block)[:2] == bytes([0x76, 0x01])
        assert client.request(bytes([0x37]))[0] == 0x77                # transferExit
    finally:
        stop.set()
        th.join(timeout=3)


def test_isotp_send_batches_cf_when_stmin_zero():
    """When the ECU's flow control allows a continuous stream (BS=0, STmin=0) and
    the channel offers write_can_many, IsoTp.send streams the consecutive frames
    in batched PassThruWriteMsgs calls (the write-speed fix) -- in order, same
    bytes. A channel without write_can_many keeps the per-frame path."""
    class _BatchCan:
        def __init__(self):
            self.singles = []
            self.batches = []
            self._rx = []
        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            self.singles.append((can_id, bytes(payload)))
            if (payload[0] >> 4) == 1:                       # FF -> hand back FC 30 00 00
                self._rx.append(_Frame(0x7E9, bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0]), False))
        def write_can_many(self, can_id, payloads, extended=False, timeout_ms=100):
            self.batches.append((can_id, [bytes(p) for p in payloads]))
        def read(self, max_msgs=16, timeout_ms=0):
            out = self._rx[:max_msgs]; del self._rx[:max_msgs]; return out

    ch = _BatchCan()
    io = U.IsoTp(ch, tx_id=0x7E1, rx_id=0x7E9)
    payload = bytes([0x36, 0x01]) + bytes(i & 0xFF for i in range(100))   # 102B -> FF + 14 CF
    io.send(payload)
    assert ch.batches, "consecutive frames were not batched"
    assert len(ch.singles) == 1 and (ch.singles[0][1][0] >> 4) == 1       # only the FF went singly
    cf = [p for _, frames in ch.batches for p in frames]
    assert all((f[0] >> 4) == 2 for f in cf)                             # all CF
    data = bytearray()
    for f in cf:
        data += f[1:]
    assert bytes(data)[:len(payload) - 6] == payload[6:]                 # reassembles correctly


def test_classify_image_force_cal_only_restricts_full_image_to_db4():
    """The GUI 'Calibration only' option: a full 2 MB image that would normally
    write all four blocks must, with force_cal_only=True, write DB_4 only and
    leave the program set in place."""
    img = bytearray(b"\x00" * U.IMAGE_SIZE)
    for spec in U.GS19_BLOCKS:
        region = bytearray((i & 0xFF) or 0xA5 for i in range(spec.length))   # non-blank
        if spec.name == "DB_3":
            region[:len(U._BCB_MAGIC)] = U._BCB_MAGIC                         # valid descriptor magic
        img[spec.base:spec.base + spec.length] = region
    full = U.classify_image(bytes(img))
    assert not full.cal_only
    assert [b.spec.name for b in full.blocks if b.will_write] == ["DB_1", "DB_2", "DB_3", "DB_4"]
    calonly = U.classify_image(bytes(img), force_cal_only=True)
    assert calonly.cal_only
    assert [b.spec.name for b in calonly.blocks if b.will_write] == ["DB_4"]
    # the excluded program blocks say why
    excl = [b for b in calonly.blocks if b.spec.name == "DB_1"][0]
    assert "calibration-only requested" in excl.reason
