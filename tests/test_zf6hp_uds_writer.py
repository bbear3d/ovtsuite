# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Zf6hpUdsWriter: image classification (pure), the non-destructive write
probe (plan), and end-to-end writing -- the last over a threaded in-memory
ISO-TP bus against the bench simulator, on a SMALL monkeypatched block layout
so a full erase/download/transfer/verify cycle runs in milliseconds."""
import collections
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "work"))

from ovtsuite.flash import zf6hp_uds as U        # noqa: E402
from ovtsuite.flash import ecu_sim as S          # noqa: E402
from ovtsuite.flash import crypto as C           # noqa: E402

_Frame = collections.namedtuple("Frame", "can_id payload is_tx_echo")


# ----------------------------------------------------------- pure classify --
def _synth(size, blocks, seed=0, db3_magic=True, blank=()):
    img = bytearray(b"\xFF" * size)
    for spec in blocks:
        if spec.name in blank:
            continue
        img[spec.base:spec.base + spec.length] = bytes(
            ((i * 7 + spec.block_id + seed) & 0xFF) for i in range(spec.length))
    if db3_magic:
        db3 = next(b for b in blocks if b.name == "DB_3")
        img[db3.base:db3.base + 3] = b"BCB"
    return bytes(img)


def test_classify_full_image():
    img = _synth(U.IMAGE_SIZE, U.GS19_BLOCKS)
    plan = U.classify_image(img)
    assert not plan.cal_only
    assert {b.spec.name for b in plan.write_set} == {"DB_1", "DB_2", "DB_3", "DB_4"}
    db3 = next(b for b in plan.blocks if b.spec.name == "DB_3")
    assert db3.present and db3.valid


def test_classify_cal_only_sized_image():
    full = _synth(U.IMAGE_SIZE, U.GS19_BLOCKS)
    cal = full[U.GS19_CAL.base:U.GS19_CAL.base + 0x80000]
    plan = U.classify_image(cal)
    assert plan.cal_only
    assert [b.spec.name for b in plan.write_set] == ["DB_4"]


def test_classify_program_blank_falls_back_to_cal_only():
    img = _synth(U.IMAGE_SIZE, U.GS19_BLOCKS, blank=("DB_1",), db3_magic=True)
    plan = U.classify_image(img)
    assert plan.cal_only
    assert [b.spec.name for b in plan.write_set] == ["DB_4"]


def test_classify_db3_without_magic_is_cal_only():
    img = _synth(U.IMAGE_SIZE, U.GS19_BLOCKS, db3_magic=False)
    plan = U.classify_image(img)
    assert plan.cal_only
    db3 = next(b for b in plan.blocks if b.spec.name == "DB_3")
    assert db3.present and not db3.valid
    assert [b.spec.name for b in plan.write_set] == ["DB_4"]


def test_classify_rejects_odd_size():
    with pytest.raises(U.KLineError):
        U.classify_image(b"\x00" * 1234)


# ------------------------------------------------- small-layout bus harness --
_TINY = [
    U.BlockSpec(0x01, "DB_1", 0x1000, 0x800, "program"),
    U.BlockSpec(0x02, "DB_2", 0x1800, 0x400, "program"),
    U.BlockSpec(0x03, "DB_3", 0x0800, 0x400, "descriptor"),
    U.BlockSpec(0x04, "DB_4", 0x2000, 0x800, "calibration"),
]
_TINY_SIZE = 0x4000


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(U, "GS19_BLOCKS", _TINY)
    monkeypatch.setattr(U, "GS19_CAL", _TINY[-1])
    monkeypatch.setattr(U, "IMAGE_SIZE", _TINY_SIZE)
    return _TINY


class _Endpoint:
    def __init__(self, out_q, in_q, lock):
        self._out, self._in, self._lock = out_q, in_q, lock

    def pass_ids(self, *a, **k):
        pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        with self._lock:
            self._out.append((int(can_id), bytes(payload)))

    def read(self, max_msgs=32, timeout_ms=100):
        end = time.time() + timeout_ms / 1000.0
        out = []
        while True:
            with self._lock:
                while self._in and len(out) < max_msgs:
                    cid, data = self._in.popleft()
                    out.append(_Frame(cid, data, False))
            if out or time.time() >= end:
                return out
            time.sleep(0.0005)


def _bus():
    t2e, e2t, lock = collections.deque(), collections.deque(), threading.Lock()
    return _Endpoint(t2e, e2t, lock), _Endpoint(e2t, t2e, lock)


def _tiny_profile(image):
    return S.EcuProfile(
        name="tiny", title="tiny GS19",
        key_fns={0x04: C.zf6hp_read_key, 0x12: C.zf6hp_write_key},
        seed_values={0x03: b"\x11\x22\x33\x44", 0x11: b"\x55\x66\x77\x88"},
        verify_blocks={s.block_id: (s.base, s.length) for s in _TINY})


def _run_sim(image, erase_delay=0.0, erase_pending=True):
    tester_can, ecu_can = _bus()
    simlog = []
    prof = _tiny_profile(image)
    responder = S.EcuResponder(prof, image=image, read_mode="serve", log=simlog.append,
                               erase_delay_s=erase_delay, erase_pending=erase_pending,
                               pending_interval_s=0.1)
    io_ecu = S.IsoTpEcu(ecu_can, rx_id=0x7E1, tx_id=0x7E9)
    sim = S.Simulator(io_ecu, responder, log=simlog.append)
    stop = threading.Event()
    th = threading.Thread(target=sim.run, kwargs={"is_stop": stop.is_set, "idle_ms": 10})
    th.start()
    tester_io = U.IsoTp(tester_can, tx_id=0x7E1, rx_id=0x7E9)
    return tester_io, responder, simlog, stop, th


def _dl_blocks(simlog):
    """block ids that received a requestDownload, from the sim log."""
    ids = []
    for ln in simlog:
        if "requestDownload:" in ln and "block/addr=" in ln:
            ids.append(int(ln.split("block/addr=")[1].split()[0], 16))
    return ids


# ----------------------------------------------------------------- plan --
def test_plan_skips_all_when_image_matches_tcu(tiny):
    img = _synth(_TINY_SIZE, _TINY)
    tester_io, responder, simlog, stop, th = _run_sim(img)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).plan(img)
    finally:
        stop.set(); th.join(timeout=3)
    assert plan.write_set == []
    assert all(b.resident_match for b in plan.blocks if b.will_write is False and b.present)


def test_plan_flags_only_changed_cal(tiny):
    resident = _synth(_TINY_SIZE, _TINY, seed=0)
    edited = bytearray(resident)
    cal = _TINY[-1]
    edited[cal.base + 0x10:cal.base + 0x14] = b"\xDE\xAD\xBE\xEF"   # cal change only
    tester_io, responder, simlog, stop, th = _run_sim(resident)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).plan(bytes(edited))
    finally:
        stop.set(); th.join(timeout=3)
    assert [b.spec.name for b in plan.write_set] == ["DB_4"]


# ---------------------------------------------------------------- write --
def test_write_only_changed_blocks(tiny):
    # The TCU's 0x0202 is a POST-download checksum (it answers conditionsNotCorrect
    # before a download on the car), so there is no reliable pre-write skip of
    # unchanged blocks. write() therefore writes EVERY present/valid candidate.
    resident = _synth(_TINY_SIZE, _TINY, seed=0)
    edited = bytearray(resident)
    edited[0x1000 + 0x20:0x1000 + 0x24] = b"\x01\x02\x03\x04"      # DB_1 change
    edited[0x2000 + 0x40:0x2000 + 0x44] = b"\xDE\xAD\xBE\xEF"      # DB_4 change
    tester_io, responder, simlog, stop, th = _run_sim(resident)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).write(bytes(edited))
    finally:
        stop.set(); th.join(timeout=5)
    # every present/valid candidate block is downloaded (no pre-write skip)
    assert sorted(_dl_blocks(simlog)) == [0x01, 0x02, 0x03, 0x04]
    # the sim applied them: resident image now equals the edited image in those regions
    for base, ln in ((0x1000, 0x800), (0x2000, 0x800)):
        assert responder.image[base:base + ln] == bytes(edited[base:base + ln])
    # unchanged program blocks were left intact
    assert responder.image[0x1800:0x1800 + 0x400] == resident[0x1800:0x1800 + 0x400]


def test_write_cal_only_image_writes_only_cal(tiny):
    resident = _synth(_TINY_SIZE, _TINY, seed=0)
    new_cal = bytes(((i * 5 + 1) & 0xFF) for i in range(_TINY[-1].length))   # different cal
    tester_io, responder, simlog, stop, th = _run_sim(resident)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).write(new_cal)        # cal-sized image
    finally:
        stop.set(); th.join(timeout=5)
    assert plan.cal_only
    assert _dl_blocks(simlog) == [0x04]
    assert responder.image[0x2000:0x2000 + 0x800] == new_cal


def test_write_rides_out_slow_erase_with_responsepending(tiny):
    """A slow erase that answers with responsePending keepalives must not abort
    the write (the 6HP19 lesson): the hardened client waits it out and the
    changed block still lands."""
    resident = _synth(_TINY_SIZE, _TINY, seed=0)
    new_cal = bytes(((i * 5 + 1) & 0xFF) for i in range(_TINY[-1].length))
    tester_io, responder, simlog, stop, th = _run_sim(resident, erase_delay=0.4, erase_pending=True)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).write(new_cal)
    finally:
        stop.set(); th.join(timeout=5)
    assert _dl_blocks(simlog) == [0x04]
    assert responder.image[0x2000:0x2000 + 0x800] == new_cal
    assert any("responsePending" in l for l in simlog)


def test_write_rides_out_silent_slow_erase(tiny):
    """A slow erase that answers with NO responsePending (silent) must also not
    abort: the delay runs past the client's first ~2 s window, and the writer
    (fail_fast_on_silence=False on erase) waits it out to the op budget. Under
    the old fast-fail-on-silence behaviour this would have raised at 2 s."""
    resident = _synth(_TINY_SIZE, _TINY, seed=0)
    new_cal = bytes(((i * 5 + 1) & 0xFF) for i in range(_TINY[-1].length))
    tester_io, responder, simlog, stop, th = _run_sim(resident, erase_delay=2.3, erase_pending=False)
    try:
        plan = U.Zf6hpUdsWriter(tester_io).write(new_cal)
    finally:
        stop.set(); th.join(timeout=8)
    assert _dl_blocks(simlog) == [0x04]
    assert responder.image[0x2000:0x2000 + 0x800] == new_cal
    assert any("silent" in l for l in simlog)                # genuinely the silent path
    assert any("3E 80" in l for l in simlog)                 # testerPresent keepalive held the session


def test_request_with_keepalive_holds_and_resends_until_answer():
    """request_with_keepalive mirrors the OEM tool on a silent erase: it holds
    the session with testerPresent 3E 80 and re-sends the request until the ECU
    answers, then returns that answer."""
    import time, types
    class SilentIo:
        def __init__(self, answer_at_s):
            self.t0 = time.monotonic(); self.answer_at = answer_at_s
            self.sent = []; self.can = types.SimpleNamespace(clear_rx=lambda: None)
        def send(self, payload): self.sent.append(bytes(payload))
        def recv(self, timeout_ms):
            end = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < end:
                if time.monotonic() - self.t0 >= self.answer_at:
                    return bytes([0x71, 0x01, 0xFF, 0x00, 0x00])
                time.sleep(0.005)
            raise U.KLineTimeout("silent")
    io = SilentIo(0.4)
    erase = bytes([0x31, 0x01, 0xFF, 0x00, 0x01, 0x04])
    resp = U.Zf6hpUdsClient(io).request_with_keepalive(
        erase, keepalive_interval_s=0.05, resend_interval_s=0.15, poll_ms=30, overall_ms=5000)
    assert resp == bytes([0x71, 0x01, 0xFF, 0x00, 0x00])
    assert bytes([0x3E, 0x80]) in io.sent          # held the session with testerPresent
    assert io.sent.count(erase) >= 2               # re-sent the erase while silent
