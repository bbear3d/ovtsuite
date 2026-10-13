# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TP 2.0 CAN transport: end-to-end through diag/edc16, and replay vs the
real Scanmatik logs in Reference/Logs."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from ovtsuite.flash import kwp as KWP  # noqa: E402
from mock_tp20 import MockTp20Ecu, _Msg as _M  # noqa: E402

#: Slow: drives the TP2.0 session and its timers.
import pytest

pytestmark = pytest.mark.slow



# --------------------------------------------------------- transport unit
def test_tp20_setup_and_message_roundtrip():
    def handler(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req == b"\x1a\x9b":
            return b"\x5a\x9b" + b"03G906016GC  9970"      # 19 bytes -> 3 frames
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler)
    ch = TT.Tp20Channel(ecu, dest=0x01)
    ch.open()
    assert ch.tx_id == 0x740 and ch.rx_id == 0x300
    ch.send(b"\x10\x89")
    assert ch.recv() == b"\x50\x89"
    ch.send(b"\x1a\x9b")
    r = ch.recv()
    assert r[:2] == b"\x5a\x9b" and r[2:].decode() == "03G906016GC  9970"
    ch.close()
    assert not ecu.opened


# --------------------------------------------------------- CanKLine + diag
def test_diag_live_data_over_can():
    from ovtsuite.flash.diag import DiagSession
    # a measuring-block group: 61 01 then (formula,A,B) triples
    group1 = bytes.fromhex("6101") + bytes.fromhex("010102 040506 190a0b 250000")

    def handler(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req[:1] == b"\x1a":
            return b"\x5a" + req[1:] + b"03G906016GC  9970"[:16]
        if req == b"\x21\x01":
            return group1
        if req == b"\x3e":
            return b"\x7e"
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler)
    link = TT.CanKLine(ecu, dest=0x01)
    sess = DiagSession(link, ecu="EDC16")
    sess.connect(speed="Low", cap_high=False)            # no elevated baud over CAN
    cells = sess.read_group(1)
    # three real cells (0x25 sentinel dropped); formula/A/B decoded
    assert len(cells) == 3
    assert cells[0].a == 0x01 and cells[0].b == 0x02 and cells[0].fid == 0x01
    sess.keepalive()
    sess.close()


# --------------------------------------------------------- CanKLine + edc16 read
def test_edc16_read_over_can():
    from ovtsuite.flash import edc16 as E
    # 512 KB of cal to hand back, 254 bytes per transferData like the real ECU
    cal = bytes((i * 7 + 3) & 0xFF for i in range(E.CAL_SIZE))
    state = {"pos": 0, "end": 0, "uploads": []}

    def handler(req):
        s = req[0]
        if s == 0x27 and req[1] == 0x03:                 # security seed
            return bytes([0x67, 0x03, 0x11, 0x22, 0x33, 0x44])
        if s == 0x27 and req[1] == 0x04:                 # key accepted
            return bytes([0x67, 0x04, 0x34])
        if s == 0x10:                                    # enter programming session
            return bytes([0x50, req[1]])
        if s == 0x35:                                    # requestUpload addr,size -> region
            addr = int.from_bytes(req[1:4], "big")
            size = int.from_bytes(req[5:8], "big")
            off = addr - E.CAL_START
            state["pos"], state["end"] = off, off + size
            state["uploads"].append((addr, size))
            return b"\x75\xff"
        if s == 0x36:                                    # transferData (read pulls)
            p = state["pos"]
            chunk = cal[p:min(p + 254, state["end"])]
            state["pos"] = p + len(chunk)
            more = state["pos"] < state["end"]
            return b"\x76" + chunk if more or chunk else b"\x76"
        if s == 0x37:
            return b"\x77"
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])
    # edc16 verifies the LVL3 seed checksum, so give it a valid one via monkeypatch
    ecu = MockTp20Ecu(handler)
    link = TT.CanKLine(ecu, dest=0x01)
    fl = E.Edc16Flash(link)
    fl._fast = True
    # bypass the K-line-specific seed framing: edc16 over CAN uses the same
    # 27 03/04 exchange; the mock returns a framed 67 03 <seed>. Patch the auth
    # to the CAN form used by the read path.
    img = fl.read_flash(speed="Medium", allow_fast=False, upload_chunk=0x10000)
    assert len(img) == E.FULL_SIZE
    assert img[E.CAL_START:] == cal
    # read was chunked into 64 KB requestUploads by address, like MPPS
    assert state["uploads"] == [(E.CAL_START + i * 0x10000, 0x10000) for i in range(8)]


# ------------------------------------------- broadcast flood must not accumulate
def test_tp20_discards_broadcast_flood():
    """On a live vehicle the bus carries hundreds of broadcast frames/s on ids
    that are not our channel.  The transport must drop them, not buffer them:
    otherwise `_pending` grows without bound and the per-frame linear scan turns
    into O(n^2), ACK latency climbs, and the ECU disconnects mid-read (the
    Readattempt.log failure).  Drive many message exchanges under a heavy flood
    and assert `_pending` stays tiny throughout."""
    def handler(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req[:1] == b"\x1a":
            return b"\x5a" + req[1:] + b"03G906016GC  9970"[:16]
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler)
    # the exact powertrain ids the real capture floods (Readattempt.log)
    ecu.flood_ids = (0x280, 0x380, 0x480, 0x588, 0x288, 0x362, 0x488)
    ch = TT.Tp20Channel(ecu, dest=0x01)
    ch.open()
    assert ch.tx_id == 0x740 and ch.rx_id == 0x300
    high = 0
    for _ in range(200):                                 # many round-trips under flood
        ch.send(b"\x1a\x9b")
        r = ch.recv()
        assert r[:2] == b"\x5a\x9b"
        high = max(high, len(ch._pending))
    # bounded regardless of how long we run: one poll's worth of frames, never
    # the accumulated thousands the bug produced
    assert high <= 32, "pending buffer grew to %d -- flood is being buffered" % high
    ch.close()


# ------------------------------------------- KWP2000 responsePending absorption
def test_tp20_absorbs_response_pending():
    """The ECU answers a slow service (here 1A 9C) with 7F 1A 78 responsePending,
    then sends the real 5A 9C reply as a separate message.  CanKLine must absorb
    the pending frame and surface the real reply -- and stay synced, so the NEXT
    request gets its own answer and not the stale one (the Writeattempt.log bug:
    the delayed reply was consumed by the following request, drifting sequence
    numbers until the ECU disconnected)."""
    def handler(req):
        if req[:2] == b"\x1a\x9c":
            return b"\x5a\x9c" + bytes(range(8))
        if req == b"\x10\x89":
            return b"\x50\x89"
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler)
    ecu.pending_once = {0x1A}
    link = TT.CanKLine(ecu, dest=0x01)
    link.wake_up()
    f1 = bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C])
    link.write_bytes(f1 + bytes([sum(f1) & 0xFF]))
    assert b"\x5a\x9c" in bytes(link._rx), bytes(link._rx).hex()   # real reply, not the pending
    link.clear_rx()
    f2 = bytes([0x82, 0x10, 0xF1, 0x10, 0x89])                    # channel still synced
    link.write_bytes(f2 + bytes([sum(f2) & 0xFF]))
    assert b"\x50\x89" in bytes(link._rx), bytes(link._rx).hex()
    link.close()


# ------------------------------------------- device-side hardware id filter
def test_tp20_installs_device_id_filter():
    """open() must narrow the CAN channel's hardware filter to our TP 2.0 ids
    (setup reply 0x201 + channel 0x300), so the broadcast flood never reaches
    the device RX buffer and can't overflow it during a write (the write-time
    ERR_BUFFER_OVERFLOW).  With the filter modelled in the mock, flood frames
    stop appearing at the software layer entirely."""
    ecu = MockTp20Ecu(lambda r: b"\x50\x89" if r == b"\x10\x89" else b"\x7e")
    ecu.flood_ids = (0x280, 0x380, 0x480, 0x588, 0x288, 0x362, 0x488)
    ch = TT.Tp20Channel(ecu, dest=0x01)
    ch.open()
    assert ch._hw_filtered
    # device now admits only setup-reply id and our channel id
    assert ecu.allowed_ids == {TT.SETUP_ID + 0x01, ch.rx_id}
    # none of the flood ids are in the admitted set
    assert not (set(ecu.flood_ids) & ecu.allowed_ids)
    for _ in range(50):
        ch.send(b"\x10\x89")
        assert ch.recv() == b"\x50\x89"
    # with the device filtering, no flood ever lands in the software buffer
    assert all(m.can_id in ecu.allowed_ids for m in ch._pending)
    ch.close()


# --------------------------------------------------------- replay vs real log
def _load(path):
    from ovtsuite.sniff import capture as C
    return C.read(path)


def test_recv_reassembles_real_mpps_log():
    """Drive Tp20Channel.recv from the recorded ECU->tester frames and check it
    rebuilds the same KWP messages the sniffer decoder found."""
    from ovtsuite.sniff import capture as C, tp20 as T
    logp = os.path.join(os.path.dirname(__file__), "..", "Reference", "Logs", "EDC16MPPSRead.log")
    if not os.path.exists(logp):
        return                                            # logs not present in this checkout
    recs = C.read(logp)
    # reference: messages the standalone decoder extracts on the ECU->tester side
    dec = T.Tp20Decoder()
    ref = []
    for r in recs:
        for e in dec.feed(T.Frame(r.t, r.can_id, r.data, r.tx)):
            if e.kind == "message" and e.channel == "E->T":
                ref.append(e.payload)

    # feed only the 0x300 (ECU->tester) frames into a channel and reassemble
    class Replay:
        def __init__(self, frames):
            self.frames = frames
            self.i = 0

        def write_can(self, *a, **k):
            pass

        def read(self, max_msgs=16, timeout_ms=100):
            if self.i >= len(self.frames):
                return []
            out = self.frames[self.i:self.i + max_msgs]
            self.i += len(out)
            return out
    frames = [type("M", (), {"can_id": 0x300, "payload": r.data})() for r in recs if r.can_id == 0x300]
    ch = TT.Tp20Channel(Replay(frames))
    ch.rx_id = 0x300
    got = []
    while True:
        try:
            got.append(ch.recv(timeout_ms=50))
        except Exception:
            break
    # every reference message should appear in order among what we reassembled
    j = 0
    for payload in ref:
        while j < len(got) and got[j] != payload:
            j += 1
        assert j < len(got), "did not reassemble %s" % payload[:8].hex()
        j += 1


# --------------------------------------------------------- CanKLine + edc16 write
def test_edc16_write_over_can():
    from ovtsuite.flash import edc16 as E
    img = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_START, E.FULL_SIZE):
        img[i] = (i * 5 + 1) & 0xFF
    written = bytearray()
    st = {"phase": None}

    def handler(req):
        s = req[0]
        if s == 0x1A and req[1] == 0x9C:                  # erase signature read
            return bytes([0x5A, 0x9C]) + bytes(range(6))
        if s == 0x27 and req[1] == 0x01:
            return bytes([0x67, 0x01, 0x11, 0x22, 0x33, 0x44])
        if s == 0x27 and req[1] == 0x02:
            return bytes([0x67, 0x02, 0x34])
        if s == 0x10:
            return bytes([0x50, req[1]])
        if s == 0x83:                                    # accessTimingParameter
            return bytes([0xC3, req[1] if len(req) > 1 else 0])
        if s == 0x31 and req[1] == 0xC5:                 # checksum-compare routine start
            st["phase"] = "cmp"
            return bytes([0x71, 0xC5])
        if s == 0x33 and req[1] == 0xC5:                 # poll checksum -> always mismatch
            return bytes([0x73, 0xC5, 0x01])             # 01 = mismatch => not skipped
        if s == 0x34:                                    # requestDownload
            return bytes([0x74, 0x20, 0x00])
        if s == 0x31 and req[1] == 0xC4:                 # erase
            return bytes([0x71, 0xC4])
        if s == 0x33 and req[1] == 0xC4:                 # poll erase -> done
            return bytes([0x73, 0xC4, 0x00])
        if s == 0x36:                                    # transferData (write up)
            written.extend(req[1:])
            return b"\x76"
        if s == 0x37:
            return b"\x77"
        if s == 0x31 and req[1] == 0xC5 and st["phase"] == "verify":
            return bytes([0x71, 0xC5])
        if s == 0x11:                                    # ecuReset (NAK like real ECU)
            return bytes([0x7F, 0x11, 0x90])
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])
    # post-write verify must report "match": flip the poll after erase/transfer.
    calls = {"n": 0}
    real_handler = handler

    seen = []
    def handler2(req):
        seen.append(bytes(req[:2]))
        # first checksum poll per block = "mismatch" (forces a write); the
        # verify poll after transfer = "match"
        if req[:2] == b"\x33\xc5":
            calls["n"] += 1
            return bytes([0x73, 0xC5, 0x00 if calls["n"] % 2 == 0 else 0x01])
        return real_handler(req)
    ecu = MockTp20Ecu(handler2)
    link = TT.CanKLine(ecu, dest=0x01)
    fl = E.Edc16Flash(link)
    fl._fast = True
    fl.write_flash(bytes(img), force_full=False, allow_fast=False)
    # both cal blocks (0x180000-0x1FE000) were transferred (encrypted) up
    assert len(written) > 0
    svc = [r[0] for r in seen]
    # accessTimingParameter (0x83) is a K-line-only step: it must NOT be sent over
    # TP 2.0 -- the real ECU drops the channel on it (Writeattempt.log).
    assert 0x83 not in svc
    # Entering the programming session (10 85) resets security over CAN, so the
    # write MUST re-authenticate (27 01) AFTER the session enter, before any
    # programming command -- verified against EDC16MPPSWrite.log.
    ir = [i for i, r in enumerate(seen) if r == b"\x10\x85"]
    iq = [i for i, r in enumerate(seen) if r == b"\x27\x01"]
    assert ir and iq, (ir, iq)
    assert any(q > ir[0] for q in iq), "no 27 01 re-auth after 10 85 session enter"
    # entering the programming session resets the ECU transport -> the channel is
    # torn down and set up AGAIN (>=2 channel setups) before the re-auth/download
    assert getattr(ecu, "setup_count", 0) >= 2, "TP2.0 channel not re-established after 10 85"


def test_edc16_write_encrypts_before_opening_session(monkeypatch):
    """Encryption (~1-2 s local CPU) must run BEFORE the ECU session opens.  Doing
    it after left the TP 2.0 channel idle and the ECU dropped it (Writeattempt.log
    A8 after a 1.5 s gap).  Assert encrypt() happens before the channel wake_up."""
    from ovtsuite.flash import edc16 as E
    img = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_START, E.FULL_SIZE):
        img[i] = (i * 7) & 0xFF

    def handler(req):
        s = req[0]
        if s == 0x1A and req[1] == 0x9C:
            return bytes([0x5A, 0x9C]) + bytes(range(6))
        if s == 0x27 and req[1] == 0x01:
            return bytes([0x67, 0x01, 0x11, 0x22, 0x33, 0x44])
        if s == 0x27 and req[1] == 0x02:
            return bytes([0x67, 0x02, 0x34])
        if s == 0x10:
            return bytes([0x50, req[1]])
        if s == 0x34:
            return bytes([0x74, 0x20, 0x00])
        if s == 0x31 and req[1] == 0xC4:
            return bytes([0x71, 0xC4])
        if s == 0x33 and req[1] == 0xC4:
            return bytes([0x73, 0xC4, 0x00])
        if s == 0x36:
            return b"\x76"
        if s == 0x37:
            return b"\x77"
        if s == 0x31 and req[1] == 0xC5:
            return bytes([0x71, 0xC5])
        if s == 0x33 and req[1] == 0xC5:
            return bytes([0x73, 0xC5, 0x00])            # match -> verify passes
        if s == 0x11:
            return bytes([0x7F, 0x11, 0x90])
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])

    order = []
    real_encrypt = E.encrypt
    monkeypatch.setattr(E, "encrypt",
                        lambda *a, **k: (order.append("encrypt"), real_encrypt(*a, **k))[1])
    ecu = MockTp20Ecu(handler)
    link = TT.CanKLine(ecu, dest=0x01)
    real_wake = link.wake_up
    link.wake_up = lambda *a, **k: (order.append("session"), real_wake(*a, **k))[1]
    fl = E.Edc16Flash(link)
    fl._fast = True
    fl.write_flash(bytes(img), force_full=True, allow_fast=False)
    assert order[:1] == ["encrypt"], order          # encryption precedes the ECU session
    assert "session" in order


# --------------------------------------------------------- timing / windowing
def test_tp20_fast_params_and_windowing():
    # A0 advertises the minimum inter-frame time and a full block-size window
    p = TT.Tp20Params()
    a0 = p.a0()
    assert a0[0] == 0xA0
    # A0 advertises the CONFIGURED t3_ms (default 5 ms): advertising the old
    # 0.1 ms coincided with the HVAC ECU flow-controlling us with 0x9x, so we
    # advertise a safe value and instead speed our SENDS via the ECU's A1
    # (max(ecu, t3) clamp, or pace_ecu). So A0 carries t3_ms, not a minimal value.
    assert TT._timing_ms(a0[4]) == TT.Tp20Params().t3_ms
    assert a0[1] >= 0x0F                          # block size (frames per ACK)

    # a multi-frame send ACKs only at block boundaries + last frame, not per frame
    class CountingEcu(MockTp20Ecu):
        def __init__(self):
            super().__init__(lambda req: b"\x76")
            self.acks_requested = 0

        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            if can_id == self.ecu_rx and payload and (payload[0] >> 4) in (0x0, 0x1):
                if (payload[0] >> 4) in (0x0, 0x1):
                    self.acks_requested += 1
            super().write_can(can_id, payload, extended, timeout_ms)
    ecu = CountingEcu()
    ch = TT.Tp20Channel(ecu, dest=0x01)
    ch.open()
    ch.ecu_interframe_ms = 0.0                    # don't sleep in the test
    ch.send(b"\x36" + bytes(200))                 # ~29 frames
    # 29 frames, BS 15 -> ACKs at 15, and last: far fewer than one-per-frame
    assert ecu.acks_requested <= 4


# --------------------------------------------------------- pacing at ECU rate
def test_pace_ecu_paces_at_module_advertised_interframe(monkeypatch):
    # pace_ecu=True -> send inter-frame gap follows the module's OWN advertised
    # interframe (ecu_interframe_ms), NOT the max(ecu, t3_ms) clamp. (_POLL_SPIN_MS
    # is 0.3 ms, below both, so max(gaps) isolates the send pacing.)
    gaps = []
    monkeypatch.setattr(TT, "_precise_wait", lambda sec: gaps.append(sec))
    ch = TT.Tp20Channel(MockTp20Ecu(lambda req: b"\x76"), dest=0x01,
                        params=TT.Tp20Params(pace_ecu=True, t3_ms=5.0))
    ch.open(); ch.ecu_interframe_ms = 1.0                 # module advertised 1 ms
    ch.send(b"\x36" + bytes(200))                        # multi-frame
    assert gaps and abs(max(gaps) - 0.001) < 1e-6, gaps   # paced at 1 ms, not 5 ms

    gaps.clear()
    ch2 = TT.Tp20Channel(MockTp20Ecu(lambda req: b"\x76"), dest=0x01,
                         params=TT.Tp20Params(t3_ms=5.0))  # no pace_ecu -> clamp
    ch2.open(); ch2.ecu_interframe_ms = 1.0
    ch2.send(b"\x36" + bytes(200))
    assert gaps and abs(max(gaps) - 0.005) < 1e-6, gaps   # clamped up to 5 ms


# --------------------------------------------------------- tx ack cadence
def test_tx_ack_every_requests_acks_more_often_than_block_size():
    # tx_ack_every makes send() request an ACK every N frames (the ODIS cadence
    # of ~4), far more often than the ECU's BS=15 -- which is what kept this HVAC
    # ECU in sync instead of flow-controlling nearly every block with 9x.
    class CountingEcu(MockTp20Ecu):
        def __init__(self):
            super().__init__(lambda req: b"\x76")
            self.acks_requested = 0
        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            if can_id == self.ecu_rx and payload and (payload[0] >> 4) in (0x0, 0x1):
                self.acks_requested += 1
            super().write_can(can_id, payload, extended, timeout_ms)
    ecu = CountingEcu()
    ch = TT.Tp20Channel(ecu, dest=0x01, params=TT.Tp20Params(tx_ack_every=4))
    ch.open(); ch.ecu_interframe_ms = 0.0
    ch.send(b"\x36" + bytes(200))                 # ~29 frames
    assert 6 <= ecu.acks_requested <= 9, ecu.acks_requested   # every ~4, not every 15


def test_inter_send_ms_paces_before_each_message(monkeypatch):
    # inter_send_ms inserts a precise busy-wait BEFORE each message we send (the
    # ~4 ms inter-chunk breather that keeps this HVAC ECU from not-ready'ing).
    waits = []
    monkeypatch.setattr(TT, "_precise_wait", lambda sec: waits.append(sec))
    ch = TT.Tp20Channel(MockTp20Ecu(lambda req: b"\x76"), dest=0x01,
                        params=TT.Tp20Params(inter_send_ms=4.0))
    ch.open(); ch.ecu_interframe_ms = 0.0
    ch.send(b"\x36" + bytes(40))
    assert any(abs(w - 0.004) < 1e-6 for w in waits), waits   # a 4 ms pre-send wait fired


# --------------------------------------------------------- keep-alive handling
def test_tp20_handles_ecu_keepalive_midstream():
    ecu = MockTp20Ecu(lambda req: b"\x50\x89" if req == b"\x10\x89" else b"\x7e")
    ecu.inject_keepalive = True                      # ECU sends A3 before every reply
    ch = TT.Tp20Channel(ecu, dest=0x01)
    ch.open()
    ch.send(b"\x10\x89")
    assert ch.recv() == b"\x50\x89"                  # A3 answered, not fatal
    ch.send(b"\x3e")
    assert ch.recv() == b"\x7e"
    # the channel test must be answered with A1, never A4 (break) -- an A4
    # aborts the real ECU's upload (Readattempt.log bug)
    assert ecu.a1_replies > 0 and not ecu.got_break


# --------------------------------------------------------- reconnect over CAN
def test_diag_reconnect_over_can():
    from ovtsuite.flash.diag import DiagSession
    grp = bytes.fromhex("6101") + bytes.fromhex("010102 040506 190a0b 250000")

    def handler(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req[:1] == b"\x1a":
            return b"\x5a" + req[1:] + b"03G906016GC  9970"[:16]
        if req == b"\x21\x01":
            return grp
        if req == b"\x3e":
            return b"\x7e"
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler)
    ecu.inject_keepalive = True                      # ECU pokes A3 -> must not drop us
    link = TT.CanKLine(ecu, dest=0x01)
    sess = DiagSession(link, ecu="EDC16")
    sess.connect(speed="Low", cap_high=False)
    assert len(sess.read_group(1)) == 3
    # a fresh channel setup mid-session (reconnect) must re-establish and work
    sess.reconnect()
    assert link.tp.opened and len(sess.read_group(1)) == 3
    sess.close()


# ------------------------------------------- gatewayed flow control on multi-frame
class _DropsFirstFrameEcu(MockTp20Ecu):
    """A gatewayed VW module (HVAC 0x2C) that, on the first multi-frame request,
    drops the leading non-boundary frame while it is busy, then keeps inviting
    that sequence until the tester resends it.  This is the exact behaviour in
    climatronic-canlog-20260921-161506: the sendKey frame seq3 (no-ACK) was
    dropped and the module answered every subsequent frame with 'ready, want
    seq3' -- a transport that only retransmits the boundary frame never recovers.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._want = None
        self._dropped_once = False
        self._buf = bytearray()
        self._exp = None

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        payload = bytes(payload)
        if can_id != self.ecu_rx or not payload:
            return super().write_can(can_id, payload, extended, timeout_ms)
        b0 = payload[0]
        op = b0 >> 4
        seq = b0 & 0x0F
        if op not in (0x0, 0x1, 0x2, 0x3):
            return super().write_can(can_id, payload, extended, timeout_ms)
        if self._want is None:
            self._want = seq
        ack_expected = op in (0x0, 0x1)
        last = op in (0x1, 0x3)
        # model "busy": drop the first non-boundary frame once, NAK not-ready
        if not last and not self._dropped_once and seq == self._want:
            self._dropped_once = True
            self.out.append(_M(self.ecu_tx, bytes([(0x9 << 4) | self._want])))
            return
        if seq != self._want:                        # out of order: still want _want
            if ack_expected:
                self.out.append(_M(self.ecu_tx, bytes([(0xB << 4) | self._want])))
            return
        self._buf += payload[1:]                     # in order: accept
        if self._exp is None and len(self._buf) >= 2:
            self._exp = int.from_bytes(self._buf[:2], "big")
        self._want = (self._want + 1) & 0x0F
        if ack_expected:
            self.out.append(_M(self.ecu_tx, bytes([(0xB << 4) | self._want])))
        if last:
            req = bytes(self._buf[2:2 + (self._exp or 0)])
            self._buf = bytearray()
            self._exp = None
            self._respond(self.handler(req))


def test_tp20_multiframe_recovers_from_dropped_frame():
    # a 6-byte request (27 04 + 4-byte key) segments into 2 frames: a leading
    # no-ACK frame + a boundary frame.  The ECU drops the first, then keeps
    # asking for it; the transport must resend from the requested sequence.
    def handler(req):
        if req[:1] == b"\x27" and len(req) == 6:
            return b"\x67\x04"                       # sendKey accepted
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = _DropsFirstFrameEcu(handler, dest=0x2C)
    ch = TT.Tp20Channel(ecu, dest=0x2C, poll_timeout_ms=3000)
    ch.open()
    import time as _t
    t0 = _t.monotonic()
    ch.send(bytes.fromhex("2704B188D388"))           # the exact key from the log
    dt = _t.monotonic() - t0
    assert ch.recv() == b"\x67\x04"                  # unlock reply came back
    assert dt < 2.0, "must recover fast, not spin to the deadline (was %.2fs)" % dt
    ch.close()
