# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Deterministic, hardware-free tests for the TRx bench simulator.

A pair of in-memory CAN endpoints (tester <-> sim) lets a real
ovtsuite.flash.tp20_transport.Tp20Channel (tester side) talk to the new
trx_sim.Tp20Ecu + Trx09dResponder, so the TP2.0 framing is exercised by the
project's own tester code, not a bespoke stub.
"""
import sys, os, threading, queue, time
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import trx_sim as T
from ovtsuite.flash import crypto as C
from ovtsuite.flash.tp20_transport import Tp20Channel, Tp20Params, CanKLine


class Msg:
    def __init__(self, can_id, payload):
        self.can_id = can_id
        self.payload = bytes(payload)
        self.is_tx_echo = False


class FakeBus:
    """Two endpoints sharing two queues; each writes to the other's inbox."""
    def __init__(self, inbox, outbox):
        self.inbox = inbox
        self.outbox = outbox
    def write_can(self, can_id, payload, timeout_ms=0):
        self.outbox.put(Msg(can_id, payload))
    def write(self, can_id, payload, timeout_ms=0):   # alias some code uses
        self.write_can(can_id, payload)
    def read(self, max_msgs=32, timeout_ms=0):
        out = []
        end = time.time() + timeout_ms / 1000.0
        while len(out) < max_msgs:
            try:
                out.append(self.inbox.get(timeout=max(0.0, end - time.time())))
            except queue.Empty:
                break
        return out
    def clear_rx(self):
        pass
    def disconnect(self):
        pass


def make_pair():
    a2b, b2a = queue.Queue(), queue.Queue()
    tester = FakeBus(b2a, a2b)       # tester reads b2a, writes a2b
    sim = FakeBus(a2b, b2a)          # sim reads a2b, writes b2a
    return tester, sim


def test_sa2_key_matches_firmware():
    # trx_key must equal the firmware's 3-round rotl/add/xor for arbitrary seeds
    def fw(seed):
        x = seed & 0xFFFFFFFF
        for _ in range(3):
            x = ((x << 1) | (x >> 31)) & 0xFFFFFFFF
            s = x + 0x54414A49
            x = (s & 0xFFFFFFFF) ^ 0x53414A49 if s > 0xFFFFFFFF else s & 0xFFFFFFFF
        return x
    for seed in (0, 0xFFFFFFFF, 0x11223344, 0xDEADBEEF, 0xA5A5A5A5):
        got = int.from_bytes(T.trx_key(seed.to_bytes(4, "big")), "big")
        assert got == fw(seed), (hex(seed), hex(got), hex(fw(seed)))


def test_responder_security_roundtrip():
    r = T.Trx09dResponder(T.aisin09d_profile(), image=bytes(0x1000))
    seed_resp = r.handle(bytes([0x27, 0x01]))
    assert seed_resp[0] == 0x67 and seed_resp[1] == 0x01
    seed = seed_resp[2:]
    key = T.trx_key(seed)
    ok = r.handle(bytes([0x27, 0x02]) + key)
    assert ok == bytes([0x67, 0x02]), ok.hex()
    assert r.unlocked


def test_responder_strict_key_rejects_wrong():
    r = T.Trx09dResponder(T.aisin09d_profile(), strict_key=True)
    r.handle(bytes([0x27, 0x01]))
    bad = r.handle(bytes([0x27, 0x02, 0x00, 0x00, 0x00, 0x00]))
    assert bad == bytes([0x7F, 0x27, 0x35]), bad.hex()


def test_responder_read_window_serves_image():
    img = bytes(range(256)) * (0xE0000 // 256)
    prof = T.aisin09d_profile()
    r = T.Trx09dResponder(prof, image=img, read_mode="serve")
    resp = r.handle(bytes([0x23, 0x04, 0x00, 0x01]))      # window 1 -> cal base
    assert resp[0] == 0x63
    base = prof.windows[0x0001][0]
    assert resp[1:] == img[base:base + 0x400], "window read must serve image bytes"


def test_responder_raw_read_addr_high_0x04():
    """Regression: a 5-byte raw read of a 0x04xxxx address (addr-hi == 0x04) must
    be served as an ADDRESS, not mis-parsed as the 4-byte windowed form (whose
    marker is also req[1]==0x04). The sequential dump died at 0x0400EE because of
    this collision (ecu-sim-20261007-210945.log)."""
    img = bytes((i * 5) & 0xFF for i in range(0xE0000))
    r = T.Trx09dResponder(T.aisin09d_profile(), image=img, read_mode="serve")
    resp = r.handle(bytes([0x23, 0x04, 0x00, 0xEE, 0xFE]))     # addr 0x0400EE, len 0xFE
    assert resp[0] == 0x63
    assert resp[1:] == img[0x0400EE:0x0400EE + 0xFE], "raw 0x04xxxx read served wrong bytes"
    # and the 4-byte windowed form still works unchanged
    w = r.handle(bytes([0x23, 0x04, 0x00, 0x01]))
    assert w[0] == 0x63 and w[1:] == img[0x2000:0x2000 + 0x400]


def test_responder_faithful_refuses_read():
    r = T.Trx09dResponder(T.aisin09d_profile(), image=bytes(0x1000), read_mode="faithful")
    resp = r.handle(bytes([0x23, 0x04, 0x00, 0x01]))
    assert resp == bytes([0x7F, 0x23, 0x7F]), resp.hex()


class ByteLink:
    """Minimal KLine-like byte link for the K-line ECU responder."""
    def __init__(self):
        self.inbuf = bytearray()
        self.out = bytearray()
    def feed(self, b):
        self.inbuf += bytes(b)
    def read(self, n):
        if len(self.inbuf) < n:
            raise TimeoutError("short read")
        o = bytes(self.inbuf[:n]); del self.inbuf[:n]; return o
    def write_bytes(self, b):
        self.out += bytes(b)


def _kwp_frame(service_body, dest=0x01, src=0xF1):
    n = len(service_body)
    f = bytes([0x80 | n, dest, src]) + bytes(service_body)
    return f + bytes([sum(f) & 0xFF])


def test_kline_responder_framing():
    link = ByteLink()
    ecu = T.KLineEcu(link, controller=0x01)
    link.feed(_kwp_frame([0x10, 0x85]))
    req = ecu.recv(100)
    assert req == bytes([0x10, 0x85])
    ecu.send(T.Trx09dResponder(T.aisin09d_profile()).handle(req))
    out = bytes(link.out)
    assert out[1] == 0xF1 and out[2] == 0x01            # ECU->tester addressed
    assert out[-1] == (sum(out[:-1]) & 0xFF)            # checksum
    n = out[0] & 0x3F
    assert out[3:3 + n] == bytes([0x50, 0x85])


def test_write_capture_and_optional_bcb():
    import tempfile, os
    d = tempfile.mkdtemp()
    r = T.Trx09dResponder(T.aisin09d_profile(), capture_dir=d)
    assert r.handle(bytes([0x34, 0x00, 0x20, 0x00, 0x00, 0x00, 0x04, 0x00]))[0] == 0x74
    r.handle(bytes([0x36]) + bytes(range(16)))
    r.handle(bytes([0x36]) + bytes(range(16, 32)))
    assert r.handle(bytes([0x37])) == bytes([0x77])
    caps = os.listdir(d)
    assert any(c.startswith("trx-write-01-addr002000") for c in caps), caps


def test_sgo_flat_image_places_section():
    # a minimal synthetic v2 SGO with one plain (stored==declared) section,
    # outer-XOR-0xFF'd, placed at its declared address -- the 09D case.
    import struct
    body = bytes([0xAA]) * 0x100
    blob = bytes(b ^ 0xFF for b in body)
    addr, declared = 0x2000, len(body)
    # 0x19-byte header: addr(3) crypt(1) declared(3) pad.. slen@0x15(4) then blob
    hdr = bytearray(0x19)
    hdr[0:3] = addr.to_bytes(3, "big")
    hdr[4:7] = declared.to_bytes(3, "big")
    struct.pack_into("<I", hdr, 0x15, len(blob))
    sec = bytes(hdr) + blob
    ms = 0x40                                          # metastart (past the header)
    buf = bytearray(b"SGML Object File")
    buf += b"\x00" * (ms + 4 - len(buf))               # room for header + metalen u32
    struct.pack_into("<I", buf, 0x29, ms)              # metastart
    struct.pack_into("<I", buf, ms, 0)                 # metalen 0 -> section table at ms+4
    struct.pack_into("<I", buf, 0x2D, ms + 4 + len(sec))   # sgolen (table end)
    buf += sec
    img = T._sgo_flat_image(bytes(buf))
    assert img[addr:addr + declared] == body


def _run_sim(sim_bus, responder, stop):
    io = T.Tp20Ecu(sim_bus, dest=0x01, ecu_rx_id=0x740)
    sim = T.Simulator(io, responder)
    sim.run(is_stop=stop.is_set, idle_ms=50)


def test_tp20_setup_answers_any_logical_address():
    """A real tool targets the transmission's own logical address (e.g. 0x02),
    not engine 0x01. The default (dest=None) must answer whatever address the
    tool used, reply with byte0=0x00, and advertise the real ECU A1 params."""
    for addr, listen in ((0x02, 0x300), (0x1F, 0x440)):
        tester_bus, sim_bus = make_pair()
        io = T.Tp20Ecu(sim_bus, dest=None, ecu_rx_id=0x740)
        # tester broadcasts setup on 0x200 for logical <addr>
        tester_bus.write_can(0x200, bytes([addr, 0xC0, 0x00, 0x10,
                                           listen & 0xFF, (listen >> 8) & 0xFF, 0x01]))
        # then its A0 params on the id the ECU will assign it (0x740)
        tester_bus.write_can(0x740, bytes([0xA0, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF]))
        assert io.wait_setup(1000) is True
        assert io.matched_dest == addr and io.ecu_tx_id == listen
        # the D0 reply must be on 0x200+addr with byte0 == 0x00
        d0 = None
        while not tester_bus.inbox.empty():
            m = tester_bus.inbox.get()
            if m.can_id == 0x200 + addr and len(m.payload) >= 2 and m.payload[1] == 0xD0:
                d0 = m
        assert d0 is not None, "no D0 on 0x%X" % (0x200 + addr)
        assert d0.payload[0] == 0x00
        assert (d0.payload[2] | (d0.payload[3] << 8)) == listen      # ECU sends on listen
        assert (d0.payload[4] | (d0.payload[5] << 8)) == 0x740       # tester sends to 0x740


def test_ident_9b_full_block_from_image():
    """1A 9B must return the FULL structured ID block (PN+dataset+coding+hw+sw),
    built from the real image fields -- a bare PN is rejected by the tool
    (trxreadsimfail.txt). Fields sit at fixed flash offsets."""
    img = bytearray(b"\xFF" * 0x20600)                       # flash addr == offset
    img[0x20500:0x2050C] = b"90P01T0I.MOT"; img[0x2050C] = 0  # sw @0x20500
    img[0x2050D:0x20519] = b"09D927750FF "; img[0x20519] = 0  # PN @0x2050D
    img[0x20527:0x2052B] = b"1274"; img[0x2052B] = 0          # dataset @0x20527
    r = T.Trx09dResponder(T.aisin09d_profile(), image=bytes(img))
    resp = r.handle(bytes([0x1A, 0x9B]))
    assert resp[:2] == bytes([0x5A, 0x9B]), resp.hex()
    body = resp[2:]
    assert body[:12] == b"09D927750FF ", body[:12]           # 12-wide PN field
    assert body[12:16] == b"1274", body[12:16]               # 4-wide dataset
    assert body[16:19] == b"\x00\x00\x00"                    # coding (RAM, zero-filled)
    assert body[19:25] == b"\x00" * 6                        # hw/serial (RAM)
    assert body[25:37] == b"90P01T0I.MOT", body[25:37]       # 20-wide sw field
    assert body[25:45].endswith(b"        "), "sw field space-padded to 20"
    assert len(body) == 45, len(body)


def test_tp20_reanswers_repeated_setup():
    """Regression for the Scanmatik deadlock (trxreadattempt.txt): the tool
    re-sends the 0x200 setup until it accepts a D0. The sim must answer EVERY
    setup (incl. after it already thinks the channel is open), reply on
    0x200+addr, and only stop once real data arrives -- not go deaf after the
    first reply."""
    tester_bus, sim_bus = make_pair()
    io = T.Tp20Ecu(sim_bus, dest=None, ecu_rx_id=0x740)
    setup = bytes([0x02, 0xC0, 0x00, 0x10, 0x00, 0x03, 0x01])   # exact Scanmatik bytes
    # three retries, then finally a real request on the data channel (0x740)
    for _ in range(3):
        tester_bus.write_can(0x200, setup)
    # first open
    assert io.wait_setup(1000) is True
    d0s = [m for m in list(tester_bus.inbox.queue) if m.can_id == 0x202]
    # drain + feed the remaining retries through recv; each must be re-answered
    io.recv(50); io.recv(50)
    n_d0 = 0
    while not tester_bus.inbox.empty():
        m = tester_bus.inbox.get()
        if m.can_id == 0x202 and len(m.payload) >= 2 and m.payload[1] == 0xD0:
            n_d0 += 1
            assert m.payload[0] == 0x00                      # byte0 == 0x00
            assert m.payload[:2] == bytes([0x00, 0xD0])
    assert n_d0 >= 2, "sim must re-answer repeated setups, got %d D0" % n_d0


def test_tp20_end_to_end_with_real_tester():
    """The project's own Tp20Channel (tester) opens a channel to Tp20Ecu and
    runs session + security + a window read; the sim serves from an image.
    A small window keeps the in-memory queue loopback quick and deterministic
    (on real hardware the frame rate is far higher)."""
    img = bytes((i * 7) & 0xFF for i in range(0xE0000))
    prof = T.aisin09d_profile()
    prof.windows = {0x0001: (0x2000, 0x40)}        # 0x40 bytes -> a few frames
    responder = T.Trx09dResponder(prof, image=img, read_mode="serve")
    tester_bus, sim_bus = make_pair()
    stop = threading.Event()
    th = threading.Thread(target=_run_sim, args=(sim_bus, responder, stop), daemon=True)
    th.start()
    try:
        ch = Tp20Channel(tester_bus, dest=0x01, listen_id=0x300,
                         params=Tp20Params(), poll_timeout_ms=2000)
        ch.open()
        assert ch.opened and ch.tx_id == 0x740 and ch.rx_id == 0x300
        # startDiagnosticSession 10 85
        ch.send(bytes([0x10, 0x85]))
        assert ch.recv(2000)[:2] == bytes([0x50, 0x85])
        # security requestSeed 27 01 -> sendKey 27 02 key
        ch.send(bytes([0x27, 0x01]))
        seed_resp = ch.recv(2000)
        assert seed_resp[:2] == bytes([0x67, 0x01])
        key = T.trx_key(seed_resp[2:])
        ch.send(bytes([0x27, 0x02]) + key)
        assert ch.recv(2000) == bytes([0x67, 0x02])
        # readMemoryByAddress window 1
        ch.send(bytes([0x23, 0x04, 0x00, 0x01]))
        rd = ch.recv(3000)
        assert rd[0] == 0x63
        base = 0x2000
        assert rd[1:] == img[base:base + 0x40]
        ch.close()
    finally:
        stop.set()
        th.join(timeout=3)


def test_tp20_dedups_retransmitted_request():
    """A tool re-sends its request (same TP2.0 sequence) until it accepts our
    reply. The sim must answer it ONCE and keep its TX sequence in sync.
    Re-dispatching each copy burns a fresh tx_seq and desyncs the counter the
    tool tracks -- the real failure in ecu-sim-20261007-180542.log, where 10 86
    was answered as seq 7/8/9/A and the read reply then came as seq B while the
    tool still expected seq 8, so it rejected the read and disconnected."""
    tester_bus, sim_bus = make_pair()
    io = T.Tp20Ecu(sim_bus, dest=None, ecu_rx_id=0x740)
    io.trace_frames = False
    tester_bus.write_can(0x200, bytes([0x02, 0xC0, 0x00, 0x10, 0x00, 0x03, 0x01]))
    assert io.wait_setup(1000)
    # the SAME request (op1 seq0, payload 1A 9B) sent three times
    req = bytes([0x10, 0x00, 0x02, 0x1A, 0x9B])
    for _ in range(3):
        tester_bus.write_can(0x740, req)
    dispatched = []
    p = io.recv(200)
    assert p == bytes([0x1A, 0x9B]), p
    dispatched.append(p)
    io.send(bytes([0x5A, 0x9B, 0xAA, 0xBB]))     # reply: caches frames, advances tx_seq
    tx_after_first = io.tx_seq
    # the two retransmits must be swallowed: no re-dispatch, tx_seq frozen
    for _ in range(4):
        p = io.recv(100)
        if p is not None:
            dispatched.append(p)
    assert len(dispatched) == 1, \
        "request dispatched %d times (retransmits not deduped)" % len(dispatched)
    assert io.tx_seq == tx_after_first, "tx_seq advanced on a swallowed retransmit"
    # a genuinely new request (seq1) still dispatches
    tester_bus.write_can(0x740, bytes([0x11, 0x00, 0x02, 0x23, 0x04]))
    assert io.recv(200) == bytes([0x23, 0x04])


def test_send_is_per_frame_not_batched():
    """send() must write one frame per write_can call (paced), NOT via a batched
    multi-message write -- batching overran the bench device TX buffer and
    dropped the tail of each block (ecu-sim-20261007-200302.log). The channel may
    still expose write_many for other uses, but send() must not reach for it."""
    class Bus:
        def __init__(self):
            self.frames = []
            self.many_calls = 0
        def write_can(self, cid, payload, extended=False, timeout_ms=100):
            self.frames.append(bytes(payload))
        def write_many(self, frames, timeout_ms=100):
            self.many_calls += 1
            for fr in frames:
                self.frames.append(bytes(fr[1]))
        def read(self, max_msgs=32, timeout_ms=0):
            return []
    bus = Bus()
    io = T.Tp20Ecu(bus, dest=0x01)
    io.ecu_tx_id = 0x300
    io.opened = True
    io.block_size = 0x0F
    payload = bytes((i * 3) & 0xFF for i in range(100))   # ~15 frames
    io.send(payload)
    assert bus.many_calls == 0, "send() must not batch frames via write_many"
    assert len(bus.frames) >= 15
    body = b"".join(fr[1:] for fr in bus.frames)
    assert body[2:2 + len(payload)] == payload


def test_bridge_tp20_ecu_proxy_marshals():
    """BridgeTp20Ecu (the 64-bit client handle to the in-bridge ECU responder)
    marshals recv()/send() to the tp_ecu_* ops: recv decodes {payload hex} ->
    bytes (None when empty) and tracks frames_seen; send hex-encodes the KWP
    payload; one IPC per call."""
    from ovtsuite.flash import j2534 as J

    class FakeBridge:
        def __init__(self):
            self.sent = []
            self.q = []
            self.ops = []
        def _call(self, op, **kw):
            self.ops.append(op)
            if op == "tp_ecu_open":
                return {"ecu_rx": kw["ecu_rx"]}
            if op == "tp_ecu_send":
                self.sent.append(kw["payload"]); return None
            if op == "tp_ecu_recv":
                return self.q.pop(0) if self.q else {"payload": None, "frames_seen": 7}
            if op == "tp_ecu_close":
                return None
            raise J.J2534Error("unknown op %r" % op)

    fb = FakeBridge()
    io = J.BridgeTp20Ecu(fb, baud=500000, ecu_rx=0x740, dest=0x02,
                         a1_params=bytes([0x0F, 0xCA, 0xFF, 0x4A, 0xFF]))
    io.open()
    assert io.opened and fb.ops[0] == "tp_ecu_open"
    assert io.recv(100) is None and io.frames_seen == 7         # idle -> None
    fb.q.append({"payload": "239b", "frames_seen": 9})
    assert io.recv(100) == bytes([0x23, 0x9B]) and io.frames_seen == 9
    io.send(bytes([0x63, 0x01]))
    assert fb.sent == ["6301"]
    io.close()
    assert not io.opened and "tp_ecu_close" in fb.ops


class _RecBus:
    """Records every frame written; read() is idle. For single-block responses
    (<= block_size frames) send() never waits on a mid-block ACK, so these stay
    fast and deterministic."""
    def __init__(self):
        self.frames = []
    def write_can(self, cid, payload, extended=False, timeout_ms=100):
        self.frames.append(bytes(payload))
    def read(self, max_msgs=32, timeout_ms=0):
        return []


def _mk_ecu(bus, timing):
    io = T.Tp20Ecu(bus, dest=0x01, timing=timing)
    io.ecu_tx_id = 0x300
    io.opened = True
    io.block_size = 0x0F
    return io


def test_timing_drops_frame_but_caches_whole_response():
    """With drop_cf_prob=1.0 send() drops every frame after the first, but the
    FULL response is still cached in _last_tx_frames -- so a tester that times
    out and retransmits recovers the whole reply via _resend_last. A dropped
    frame is a gap on the wire, never lost data (matches a real lossy bus)."""
    t = T.TrxTiming(drop_cf_prob=1.0)
    bus = _RecBus()
    io = _mk_ecu(bus, t)
    payload = bytes((i * 7) & 0xFF for i in range(40))     # 42-byte body -> 6 frames, 1 block
    io.send(payload)
    assert len(io._last_tx_frames) == 6, "full response must be cached regardless of drops"
    assert len(bus.frames) == 1, "only the first frame should reach the wire (rest dropped)"
    # the retransmit path re-emits the whole cached response
    bus.frames.clear()
    io._resend_last()
    assert len(bus.frames) == 6, "retransmit must recover every frame"
    body = b"".join(fr[1:] for fr in bus.frames)
    assert body[2:2 + len(payload)] == payload


def test_timing_drop_whole_response_keeps_cache():
    """drop_resp_prob=1.0 -> the ENTIRE response is withheld from the wire, but
    the cache still holds it so the tester's retransmit recovers it."""
    t = T.TrxTiming(drop_resp_prob=1.0)
    bus = _RecBus()
    io = _mk_ecu(bus, t)
    payload = bytes([0x63, 0x01, 0x02, 0x03])
    io.send(payload)
    assert bus.frames == [], "a dropped whole response must put nothing on the wire"
    assert io._last_tx_frames, "the dropped response must still be cached for retransmit"


def test_timing_none_is_ideal_bus():
    """No timing (the default) -> every frame goes out, no drops, no cache loss."""
    bus = _RecBus()
    io = _mk_ecu(bus, None)
    payload = bytes((i * 3) & 0xFF for i in range(40))     # 6 frames, 1 block
    io.send(payload)
    assert len(bus.frames) == 6 == len(io._last_tx_frames)


def test_timing_to_dict_accepts_any_duck_type():
    """j2534._timing_to_dict must flatten a TrxTiming, a SimTiming-like object
    (same fields, NO as_dict) and a plain dict alike, and collapse an all-zero
    model to None (the bridge then takes the zero-overhead path)."""
    from ovtsuite.flash import j2534 as J

    class SimLike:                       # mimics ecu_sim.SimTiming: fields, no as_dict
        cf_gap_ms = 2.0; resp_latency_ms = 0.0; jitter_ms = 1.0
        drop_cf_prob = 0.0; drop_resp_prob = 0.0; stall_every = 0; stall_ms = 0.0

    for src in (T.TrxTiming(cf_gap_ms=2.0, jitter_ms=1.0), SimLike(),
                {"cf_gap_ms": 2.0, "jitter_ms": 1.0}):
        d = J._timing_to_dict(src)
        assert d is not None and d["cf_gap_ms"] == 2.0 and d["jitter_ms"] == 1.0
        assert set(d) == set(J._TIMING_FIELDS), "must emit exactly the shared fields"
        # round-trips back into a TrxTiming on the bridge side
        rt = T.TrxTiming.from_dict(d)
        assert rt.cf_gap_ms == 2.0 and rt.jitter_ms == 1.0
    assert J._timing_to_dict(None) is None
    assert J._timing_to_dict(T.TrxTiming()) is None, "all-zero model collapses to None"
    assert J._timing_to_dict({"cf_gap_ms": 0, "stall_every": 0}) is None


def test_bridge_tp20_ecu_forwards_timing():
    """BridgeTp20Ecu must serialize its timing model into the tp_ecu_open IPC as
    a plain dict (so the bridge can rebuild TrxTiming.from_dict next to the DLL),
    and omit it (None) for an ideal bus."""
    from ovtsuite.flash import j2534 as J

    class FakeBridge:
        def __init__(self): self.open_kw = None
        def _call(self, op, **kw):
            if op == "tp_ecu_open":
                self.open_kw = kw; return {"ecu_rx": kw["ecu_rx"]}
            return None

    fb = FakeBridge()
    io = J.BridgeTp20Ecu(fb, baud=500000, ecu_rx=0x740, dest=0x02,
                         timing=T.TrxTiming(resp_latency_ms=15.0, drop_cf_prob=0.1))
    io.open()
    tmg = fb.open_kw["timing"]
    assert isinstance(tmg, dict) and tmg["resp_latency_ms"] == 15.0 and tmg["drop_cf_prob"] == 0.1
    # ideal bus -> no timing payload
    fb2 = FakeBridge()
    J.BridgeTp20Ecu(fb2, baud=500000, timing=None).open()
    assert fb2.open_kw["timing"] is None
    J.BridgeTp20Ecu(fb2, baud=500000, timing=T.TrxTiming()).open()
    assert fb2.open_kw["timing"] is None


def test_bridge_tp20_ecu_drains_transfer_batch():
    """A transferData (0x36) write stream the bridge drains in-bridge comes back
    as a {batch:[...], tail:...}. BridgeTp20Ecu must: hand every block to the
    64-bit responder (for capture), SWALLOW the 76 send for each drained block
    (the bridge already ACKed it), and send normally for the trailing non-0x36
    message and for ordinary single messages. This is what stops the streamed
    write from desyncing on the per-message IPC gap."""
    from ovtsuite.flash import j2534 as J

    class FakeBridge:
        def __init__(self, scripted):
            self.scripted = list(scripted)   # successive tp_ecu_recv results
            self.sent = []                   # payloads actually put on the wire
        def _call(self, op, **kw):
            if op == "tp_ecu_open":
                return {"ecu_rx": kw["ecu_rx"]}
            if op == "tp_ecu_send":
                self.sent.append(kw["payload"]); return None
            if op == "tp_ecu_recv":
                return self.scripted.pop(0) if self.scripted else {"payload": None}
            if op == "tp_ecu_close":
                return None
            raise J.J2534Error("op %r" % op)

    # one ordinary message (34 requestDownload), then a drained batch of three
    # 0x36 blocks + a 0x37 exit tail, then idle.
    scripted = [
        {"payload": "3400200000de00", "frames_seen": 5},
        {"batch": ["360001", "360002", "360003"], "tail": "37", "frames_seen": 40},
        {"payload": None, "frames_seen": 40},
    ]
    fb = FakeBridge(scripted)
    io = J.BridgeTp20Ecu(fb, baud=500000, ecu_rx=0x740, dest=0x02)
    io.open()
    assert fb._call  # opened

    # drive the responder loop exactly as the sim does: recv -> handle -> send.
    def pump():
        got = []
        for _ in range(6):
            p = io.recv(100)
            if p is None:
                break
            got.append(p)
            # the responder answers 0x36 with 76, 0x34 with 74..., 0x37 with 77
            if p[0] == 0x36:
                io.send(b"\x76")
            elif p[0] == 0x34:
                io.send(bytes([0x74, 0x00, 0xFF]))
            elif p[0] == 0x37:
                io.send(b"\x77")
        return got

    got = pump()
    # every message reached the responder, in order (blocks captured)
    assert [g.hex() for g in got] == ["3400200000de00", "360001", "360002", "360003", "37"]
    # the three drained 0x36 ACKs (76) were NOT re-sent; only the 0x34 and 0x37
    # responses went on the wire.
    assert fb.sent == ["7400ff", "77"], fb.sent


def test_drain_transfer_batch_acks_and_holds():
    """trx_sim.drain_transfer_batch must ACK every 0x36 block it consumes, stop
    on a non-0x36 tail (and NOT ACK it), and -- when it fills a batch -- leave the
    final block un-ACKed and report held, so the tester stays flow-controlled
    across the boundary."""
    class FakeSrv:
        def __init__(self, incoming):
            self.incoming = list(incoming)   # payloads recv() will yield (None=idle)
            self.acks = 0
        def send(self, payload):
            assert payload == b"\x76"; self.acks += 1
        def recv(self, timeout_ms):
            return self.incoming.pop(0) if self.incoming else None

    # stream of three 0x36 blocks then a 0x37 exit
    srv = FakeSrv([bytes([0x36, 2]), bytes([0x36, 3]), bytes([0x37])])
    blocks, tail, held = T.drain_transfer_batch(srv, bytes([0x36, 1]), cap=64, idle_ms=10)
    assert blocks == ["3601", "3602", "3603"]      # all three captured
    assert tail == "37" and held is False
    assert srv.acks == 3, "exactly the three 0x36 blocks are ACKed, not the tail"

    # cap hit: last block held (un-ACKed) for the next call
    srv = FakeSrv([bytes([0x36, 2]), bytes([0x36, 3]), bytes([0x36, 4])])
    blocks, tail, held = T.drain_transfer_batch(srv, bytes([0x36, 1]), cap=3, idle_ms=10)
    assert blocks == ["3601", "3602", "3603"] and tail is None and held is True
    assert srv.acks == 2, "a full batch ACKs all but the held final block"

    # stream pauses (idle) -> every taken block ACKed, nothing held
    srv = FakeSrv([bytes([0x36, 2])])
    blocks, tail, held = T.drain_transfer_batch(srv, bytes([0x36, 1]), cap=64, idle_ms=10)
    assert blocks == ["3601", "3602"] and tail is None and held is False
    assert srv.acks == 2


def test_bridge_tp20_ecu_single_path_unchanged():
    """With no batch (a plain read / setup message), BridgeTp20Ecu behaves as
    before: the response is sent normally, nothing is swallowed."""
    from ovtsuite.flash import j2534 as J

    class FakeBridge:
        def __init__(self): self.sent = []; self.q = [{"payload": "239b", "frames_seen": 3}]
        def _call(self, op, **kw):
            if op == "tp_ecu_open": return {"ecu_rx": kw["ecu_rx"]}
            if op == "tp_ecu_send": self.sent.append(kw["payload"]); return None
            if op == "tp_ecu_recv": return self.q.pop(0) if self.q else {"payload": None}
            return None
    fb = FakeBridge()
    io = J.BridgeTp20Ecu(fb, baud=500000)
    io.open()
    p = io.recv(100)
    assert p == bytes([0x23, 0x9B])
    io.send(bytes([0x63, 0x01]))
    assert fb.sent == ["6301"], fb.sent


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except Exception:
            fail += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("%d/%d passed" % (len(fns) - fail, len(fns)))
    sys.exit(1 if fail else 0)
