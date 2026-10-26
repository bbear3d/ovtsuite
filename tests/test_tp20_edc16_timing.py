# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 TP 2.0 send pacing (edc16.EDC16_TP20_FAST_TIMING).

The slow CAN write (Reference/Logs/J2534EDC16CANWriteSLOW.txt) sent each
253-byte TransferData as 37 CAN frames 5 ms apart although the ECU's
programming-channel A1 (``A1 0F 8A FF 0A FF``) declares 1 ms. These tests drive
one such block through a fake ECU answering with that A1 and record the call
pattern: frames, ACK requests, write timeouts and the waits between frames."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import edc16 as E  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash import j2534_bridge as B  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from mock_tp20 import MockTp20Ecu, _Msg  # noqa: E402

#: the ECU's A1 T3 bytes from the trace: programming channel / default session
T3_PROGRAMMING = 0x0A          # 1.0 ms
T3_DEFAULT = 0x4A              # 10 ms

#: one TransferData as the ECU allows it (74 FD): 36 + 252 data bytes
BLOCK = b"\x36" + bytes(range(252))


class RecordingEcu(MockTp20Ecu):
    """Mock EDC16 that logs every frame the tester writes, with its timeout."""

    def __init__(self, interframe_byte):
        super().__init__(lambda req: b"\x76" if req[:1] == b"\x36" else b"\x7f\x00\x11",
                         dest=0x01, interframe_byte=interframe_byte)
        self.events = None                       # shared with the wait recorder

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        if self.events is not None and can_id == self.ecu_rx:
            self.events.append(("tx", payload[0] >> 4, timeout_ms))
        super().write_can(can_id, payload, extended, timeout_ms)


def _record_waits(monkeypatch, events):
    def wait(sec):
        if abs(sec * 1000 - TT._POLL_SPIN_MS) > 1e-9:    # not an empty-read poll spin
            events.append(("wait", round(sec * 1000, 3)))
    # the bridge imports its own copy (``import tp20_transport``, as when frozen)
    for mod in {TT, B.TP}:
        monkeypatch.setattr(mod, "_precise_wait", wait)


def _send_block(monkeypatch, params, interframe_byte=T3_PROGRAMMING):
    ecu = RecordingEcu(interframe_byte)
    ch = TT.Tp20Channel(ecu, dest=0x01, params=TT.Tp20Params(**params))
    ch.open()
    events = []
    ecu.events = events
    _record_waits(monkeypatch, events)
    ch.send(BLOCK)
    return ch, events


def _gui_params(fast):
    """The params _open_link hands an EDC16 TP 2.0 link."""
    return {"block_size": 0x0F, **(E.edc16_tp20_params() if fast else {})}


# ---------------------------------------------------------------- the switch
@pytest.fixture(autouse=True)
def _default_level(monkeypatch):
    """Each test starts from the "ecu" level (the shipped default, "aggressive",
    is checked on its own) with the switch and the read reopen on."""
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "ecu")
    monkeypatch.setattr(E, "EDC16_CAN_READ_REOPEN", True)


def test_shipped_default_is_the_aggressive_level():
    """The fastest level ships, pending a car test; "fast"/"ecu" are the
    code-level revert steps."""
    import importlib
    src = open(E.__file__, encoding="utf-8").read()
    assert 'EDC16_TP20_LEVEL = "aggressive"' in src and "EDC16_TP20_FAST_TIMING = True" in src
    assert "EDC16_TP20_READ_T3_MS = 1.0" in src and "EDC16_CAN_READ_REOPEN = True" in src
    assert importlib.import_module(E.__name__) is E
    assert set(E.EDC16_TP20_LEVELS) == {"ecu", "fast", "aggressive"}
    assert E.EDC16_TP20_LEVELS["aggressive"] == {
        "pace_ecu": True, "send_ms": 0.8, "pace_from_start": True, "backoff_on_flow": True}


def test_switch_on_paces_at_the_ecu(monkeypatch):
    assert E.edc16_tp20_params() == {"pace_ecu": True}
    assert E.edc16_tp20_params("write") == {"pace_ecu": True}
    assert E.edc16_tp20_params("write_program") == {"pace_ecu": True}
    # a copy: a caller changing it does not change the module's table
    E.edc16_tp20_params()["pace_ecu"] = False
    assert E.EDC16_TP20_LEVELS["ecu"]["pace_ecu"] is True


def test_a_read_advertises_1ms(monkeypatch):
    assert E.edc16_tp20_params("read") == {"pace_ecu": True, "t3_ms": 1.0}
    a0 = TT.Tp20Params(**E.edc16_tp20_params("read")).a0()
    assert a0 == bytes([0xA0, 0x0F, 0x8A, 0xFF, 0x0A, 0xFF])      # ODIS's own A0
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", False)
    assert E.edc16_tp20_params("read") == {}
    assert not E.can_read_reopen()


def test_switch_off_is_the_shared_default(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", False)
    assert E.edc16_tp20_params() == {}


# ------------------------------------------------------- the call pattern
def test_block_frames_and_acks_are_unchanged(monkeypatch):
    """37 frames, an ACK asked at 15, 30 and the last; only those three
    writes wait for the device's transmit confirm."""
    for fast in (False, True):
        monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", fast)
        _ch, ev = _send_block(monkeypatch, _gui_params(True))
        tx = [e for e in ev if e[0] == "tx"]
        assert len(tx) == 37
        assert [i for i, e in enumerate(tx) if e[1] in (0, 1)] == [14, 29, 36]
        assert tx[-1][1] == 1                                 # last frame, ACK wanted
        assert all(e[2] == 0 for e in tx if e[1] == 2)        # queued, no confirm wait
        assert all(e[2] == 100 for e in tx if e[1] in (0, 1))


def test_fast_timing_waits_the_ecus_1ms(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    ch, ev = _send_block(monkeypatch, _gui_params(True))
    assert ch.ecu_interframe_ms == 1.0
    waits = [e[1] for e in ev if e[0] == "wait"]
    assert waits == [1.0] * 34                    # one after each unacked frame
    # a wait follows each queued frame, never an ACK-requesting one
    pairs = [(ev[i - 1], e) for i, e in enumerate(ev) if e[0] == "wait"]
    assert all(p[0] == ("tx", 2, 0) for p in pairs)


def test_switch_off_restores_the_5ms_clamp(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", False)
    _ch, ev = _send_block(monkeypatch, _gui_params(False))
    waits = [e[1] for e in ev if e[0] == "wait"]
    assert waits == [5.0] * 34                    # max(ECU 1 ms, our A0 5 ms)
    # the trace's ~230 ms a block: 170 ms of it is these waits; fast is 34 ms
    assert sum(waits) == 170.0


def test_never_faster_than_the_ecu_declares(monkeypatch):
    """The default-session channel declares 10 ms: paced at 10, not at our 5."""
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    ch, ev = _send_block(monkeypatch, _gui_params(True), interframe_byte=T3_DEFAULT)
    assert ch.ecu_interframe_ms == 10.0
    assert {e[1] for e in ev if e[0] == "wait"} == {10.0}


def test_a0_we_advertise_is_unchanged(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    fast = TT.Tp20Params(**_gui_params(True)).a0()
    slow = TT.Tp20Params(**_gui_params(False)).a0()
    assert fast == slow == bytes([0xA0, 0x0F, 0x8A, 0xFF, 0x32, 0xFF])   # as in the trace


# ------------------------------------------------- through the 32-bit bridge
class _FakeDev:
    ecu = None

    def __init__(self, dll):
        self.device_id = J.c_uint32(1)

    def connect(self, protocol, baud, flags=0):
        return _FakeDev.ecu

    def close(self):
        pass


class _PipeProc:
    def __init__(self, dll):
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.stdin = os.fdopen(w1, "wb")
        self.stdout = os.fdopen(r2, "rb")
        self.stderr = open(os.devnull, "rb")
        srv_in, srv_out = os.fdopen(r1, "rb"), os.fdopen(w2, "wb")

        def run():
            try:
                B.serve(dll, srv_in, srv_out)
            finally:
                srv_out.close()
                srv_in.close()
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()

    def wait(self, timeout=None):
        self.t.join(timeout)

    def kill(self):
        pass


def test_bridge_tp_loop_takes_the_edc16_pacing(monkeypatch):
    """The params cross the bridge IPC (its tp_open whitelist) and the in-bridge
    loop paces at the ECU's 1 ms: the block is one tp_send, 37 device writes."""
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", True)
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    _FakeDev.ecu = RecordingEcu(T3_PROGRAMMING)
    events = []
    _record_waits(monkeypatch, events)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        tp.open()                                  # CanKLine.wake_up does this
        assert tp.ecu_interframe_ms == 1.0
        events.clear()
        _FakeDev.ecu.events = events
        tp.send(BLOCK)
        assert tp.recv(2000) == b"\x76"
        tx = [e for e in events if e[0] == "tx" and e[1] in (0, 1, 2)]
        assert len(tx) == 37
        assert [e[1] for e in events if e[0] == "wait"] == [1.0] * 34
        tp.close()
    finally:
        dev.close()


# ------------------------------------------- faster levels: fake write clock
class ClockedEcu(RecordingEcu):
    """Each device write call takes ``call_ms`` on a fake clock; ``notready``
    answers the first N ACK requests with 9x (not ready)."""

    def __init__(self, interframe_byte=T3_PROGRAMMING, call_ms=0.6, notready=0):
        super().__init__(interframe_byte)
        self.now = 0.0
        self.call_ms = call_ms
        self.notready = notready

    def clock(self):
        return self.now

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        self.now += self.call_ms / 1000.0
        payload = bytes(payload)
        if (self.notready and can_id == self.ecu_rx and payload
                and (payload[0] >> 4) in (0, 1)):
            self.notready -= 1
            if self.events is not None:
                self.events.append(("tx", payload[0] >> 4, timeout_ms))
            self.out.append(_Msg(self.ecu_tx, bytes([0x90 | ((payload[0] + 1) & 0x0F)])))
            return
        super().write_can(can_id, payload, extended, timeout_ms)


def _send_clocked(monkeypatch, level, logs=None, **ecu_kw):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", level)
    ecu = ClockedEcu(**ecu_kw)
    monkeypatch.setattr(TT, "_clock", ecu.clock)
    ch = TT.Tp20Channel(ecu, dest=0x01, params=TT.Tp20Params(**_gui_params(True)),
                        log=(logs.append if logs is not None else None))
    ch.open()
    events = []
    ecu.events = events
    _record_waits(monkeypatch, events)
    ch.send(BLOCK)
    return ch, [e[1] for e in events if e[0] == "wait"]


def test_level_ecu_waits_after_the_write(monkeypatch):
    _ch, waits = _send_clocked(monkeypatch, "ecu")
    assert waits == [1.0] * 34              # + the 0.6 ms call: ~1.6 ms a frame


def test_level_fast_counts_the_write_call_in_the_gap(monkeypatch):
    _ch, waits = _send_clocked(monkeypatch, "fast")
    assert waits == [0.4] * 34              # 1.0 ms start to start


def test_level_aggressive_goes_below_the_floor(monkeypatch):
    _ch, waits = _send_clocked(monkeypatch, "aggressive")
    assert waits == [0.2] * 34              # 0.8 ms start to start
    # a write call longer than the gap: nothing left to wait
    _ch, waits = _send_clocked(monkeypatch, "aggressive", call_ms=1.1)
    assert all(w <= 0 for w in waits)


def test_fast_level_backs_off_on_not_ready(monkeypatch):
    """The first 9x ends the faster pacing for the rest of the session."""
    logs = []
    ch, waits = _send_clocked(monkeypatch, "fast", logs=logs, notready=1)
    assert ch.backed_off and ch.flow_events >= 1
    assert waits[:14] == [0.4] * 14                     # frames 1-14: fast
    assert waits[14:] and set(waits[14:]) == {1.0}       # after the 9x at 15: ECU pace
    assert any("backed off" in m and "1.0 ms" in m for m in logs)
    assert ch.take_notes() and not ch.take_notes()      # relayed once
    # a re-setup (reopen) does not bring the fast pacing back
    ch.open()
    assert ch.backed_off
    assert ch._send_gap() == (0.001, False)


def test_ecu_level_does_not_change_on_not_ready(monkeypatch):
    ch, waits = _send_clocked(monkeypatch, "ecu", notready=1)
    assert ch.flow_events >= 1 and not ch.backed_off
    assert set(waits) == {1.0}


def test_bridge_relays_the_back_off_to_the_link_log(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "fast")
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    _FakeDev.ecu = ClockedEcu(notready=1)
    events = []
    _record_waits(monkeypatch, events)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    logs = []
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        link = TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
        assert tp.log is not None                       # CanKLine handed its log over
        tp.open()
        tp.send(BLOCK)
        assert tp.recv(2000) == b"\x76"
        assert tp.backed_off
        assert any("backed off" in m for m in logs)
        link.close()
    finally:
        dev.close()


# ------------------------------------------------------------ read reopen
def _read_ecu(fail_auth_on_setup=None):
    cal = bytes((i * 7 + 3) & 0xFF for i in range(E.CAL_SIZE))
    state = {"pos": 0, "end": 0, "uploads": [], "sessions": []}

    class Ecu(MockTp20Ecu):
        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            if can_id == 0x200:
                # the first channel is the default session's (10 ms); one set
                # up once the read session is open declares 1 ms
                n = getattr(self, "setup_count", 0) + 1
                self.interframe_byte = T3_DEFAULT if n == 1 else T3_PROGRAMMING
            super().write_can(can_id, payload, extended, timeout_ms)

    def handler(req):
        s = req[0]
        if s == 0x27 and req[1] == 0x03:
            if ecu.setup_count == fail_auth_on_setup:
                return bytes([0x7F, 0x27, 0x22])
            return bytes([0x67, 0x03, 0x11, 0x22, 0x33, 0x44])
        if s == 0x27 and req[1] == 0x04:
            return bytes([0x67, 0x04, 0x34])
        if s == 0x10:
            state["sessions"].append((ecu.setup_count, req[1]))
            return bytes([0x50, req[1]])
        if s == 0x35:
            addr = int.from_bytes(req[1:4], "big")
            size = int.from_bytes(req[5:8], "big")
            off = addr - E.CAL_START
            state["pos"], state["end"] = off, off + size
            state["uploads"].append((ecu.setup_count, addr))
            return b"\x75\xff"
        if s == 0x36:
            p = state["pos"]
            chunk = cal[p:min(p + 254, state["end"])]
            state["pos"] = p + len(chunk)
            return b"\x76" + chunk
        if s == 0x37:
            return b"\x77"
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])

    ecu = Ecu(handler)
    return ecu, cal, state


def _read(ecu, logs):
    link = TT.CanKLine(ecu, dest=0x01)
    fl = E.Edc16Flash(link, log=logs.append)
    fl._fast = True
    return fl.read_flash(speed="Medium", allow_fast=False, upload_chunk=0x10000)


@pytest.mark.slow
def test_read_streams_on_a_channel_opened_in_the_read_session():
    ecu, cal, state = _read_ecu()
    logs = []
    img = _read(ecu, logs)
    assert img[E.CAL_START:] == cal
    assert ecu.setup_count == 2
    # 10 86 on the first channel, then again on the re-opened one
    assert state["sessions"] == [(1, 0x86), (2, 0x86)]
    assert {c for c, _a in state["uploads"]} == {2}        # every upload on channel 2
    assert any("1.0 ms (first channel 10.0 ms)" in m for m in logs)


@pytest.mark.slow
def test_read_falls_back_when_the_reopened_channel_refuses():
    ecu, cal, state = _read_ecu(fail_auth_on_setup=2)
    logs = []
    img = _read(ecu, logs)
    assert img[E.CAL_START:] == cal
    assert ecu.setup_count == 3                              # reconnected the old way
    assert {c for c, _a in state["uploads"]} == {3}
    assert any("reconnecting as before" in m for m in logs)


@pytest.mark.slow
def test_read_without_reopen_when_switched_off(monkeypatch):
    monkeypatch.setattr(E, "EDC16_CAN_READ_REOPEN", False)
    ecu, cal, state = _read_ecu()
    img = _read(ecu, [])
    assert img[E.CAL_START:] == cal and ecu.setup_count == 1


class _OneChannelDev(_FakeDev):
    """A device that, like a real one, has one CAN channel: a second
    PassThruConnect while the first is open is refused."""
    connects = 0

    def connect(self, protocol, baud, flags=0):
        type(self).connects += 1
        if type(self).connects > 1:
            raise J.J2534Error("PassThruConnect(CAN): ERR_CHANNEL_IN_USE")
        return _FakeDev.ecu


@pytest.mark.slow
def test_bridge_read_falls_back_on_the_same_can_channel(monkeypatch):
    """Through the bridge, the fallback after a refused re-open must re-set up
    the TP channel on the bridge's open CAN channel (tp_reopen), not open a
    second CAN channel (tp_open -> PassThruConnect) the device refuses."""
    ecu, cal, state = _read_ecu(fail_auth_on_setup=2)
    _OneChannelDev.connects = 0
    _FakeDev.ecu = ecu
    monkeypatch.setattr(B.J, "PassThru", _OneChannelDev)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    logs = []
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        link = TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
        fl = E.Edc16Flash(link, log=logs.append)
        fl._fast = True
        img = fl.read_flash(speed="Medium", allow_fast=False, upload_chunk=0x10000)
        assert img[E.CAL_START:] == cal
        assert _OneChannelDev.connects == 1
        assert ecu.setup_count == 3
        assert any("reconnecting as before" in m for m in logs)
        link.close()
    finally:
        dev.close()


def test_stop_during_the_read_reopen_is_a_stop(monkeypatch):
    """Cancel pressed while the channel is re-opened: the read stops, it does
    not fall back and reconnect."""
    class Link:
        is_tp20 = True
        tp = None

        def reset_channel(self):
            raise E.KLineError("TP2.0: aborted (stop requested)")

    fl = E.Edc16Flash(Link(), log=lambda s: None)
    called = []
    monkeypatch.setattr(fl, "_establish_session", lambda *a, **k: called.append(a))
    with pytest.raises(E.StopRequested):
        fl.reopen_for_read(lambda: None, "Medium", False, is_stop=lambda: True)
    assert not called


def test_bridge_back_off_note_survives_a_failed_send(monkeypatch):
    """9x (back-off) then the ECU drops the channel: the send fails, and the
    back-off note is in the error rather than lost in the bridge."""
    class DroppingEcu(ClockedEcu):
        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            payload = bytes(payload)
            if (self.notready and can_id == self.ecu_rx and payload
                    and (payload[0] >> 4) in (0, 1)):
                self.notready -= 1
                self.now += self.call_ms / 1000.0
                self.out.append(_Msg(self.ecu_tx, bytes([0x90 | ((payload[0] + 1) & 0x0F)])))
                self.out.append(_Msg(self.ecu_tx, b"\xa8"))
                return
            super().write_can(can_id, payload, extended, timeout_ms)

    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "fast")
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    _FakeDev.ecu = DroppingEcu(notready=1)
    _record_waits(monkeypatch, [])
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        tp.open()
        with pytest.raises(E.KLineError) as ei:
            tp.send(BLOCK)
        assert "disconnected" in str(ei.value) and "backed off" in str(ei.value)
        tp.close()
    finally:
        dev.close()



# ------------------------------------- the shipped "aggressive" level, end to end
def test_aggressive_never_far_below_a_slow_channel(monkeypatch):
    """0.8 ms is for the 1 ms programming channel: a channel declaring 10 ms
    (default session: 1A 9C, the 27 02 key, 10 85) is paced at 8 ms."""
    _ch, waits = _send_clocked(monkeypatch, "aggressive", interframe_byte=T3_DEFAULT)
    assert waits == [pytest.approx(7.4)] * 34       # 8 ms start to start, 0.6 ms call


def test_aggressive_backs_off_to_the_ecu_pace(monkeypatch):
    logs = []
    ch, waits = _send_clocked(monkeypatch, "aggressive", logs=logs, notready=1)
    assert ch.backed_off
    assert waits[:14] == [pytest.approx(0.2)] * 14
    assert set(waits[14:]) == {1.0}                 # after the 9x: 1 ms after each write
    assert any("backed off" in m for m in logs)


def _bridge_send_clocked(monkeypatch, notready=0):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    ecu = ClockedEcu(notready=notready)
    _FakeDev.ecu = ecu
    for mod in {TT, B.TP}:
        monkeypatch.setattr(mod, "_clock", ecu.clock)
    events = []
    _record_waits(monkeypatch, events)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    logs = []
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        link = TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
        tp.open()
        events.clear()
        ecu.events = events
        tp.send(BLOCK)
        assert tp.recv(2000) == b"v"
        link.close()
    finally:
        dev.close()
    return tp, [e[1] for e in events if e[0] == "wait"], logs


def test_bridge_runs_the_aggressive_level(monkeypatch):
    """Through serve(): the level's fields pass the whitelist, the in-bridge loop
    paces 0.8 ms start to start, and no old-bridge note is logged."""
    tp, waits, logs = _bridge_send_clocked(monkeypatch)
    assert waits == [pytest.approx(0.2)] * 34
    assert not tp.backed_off
    assert not any("bridge build" in m for m in logs)


def test_bridge_aggressive_backs_off(monkeypatch):
    tp, waits, logs = _bridge_send_clocked(monkeypatch, notready=1)
    assert tp.backed_off
    assert waits[:14] == [pytest.approx(0.2)] * 14 and set(waits[14:]) == {1.0}
    assert any("backed off" in m for m in logs)


class _OldBridge(J.BridgePassThru):
    """A frozen bridge built before the levels: its tp_open applies only the
    fields it knows and answers without ``params_used``."""

    def __init__(self):                    # no bridge process
        self.calls = []

    def _call(self, op, **kw):
        self.calls.append(op)
        if op in ("tp_open", "tp_reopen"):
            return {"tx_id": 0x740, "rx_id": 0x300, "ecu_block_size": 15,
                    "ecu_interframe_ms": 1.0}
        return None                        # tp_send: no notes


def test_old_bridge_is_logged_once_and_paces_like_ecu(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    dev = _OldBridge()
    tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
    logs = []
    link = TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
    tp.open()
    tp.open()                              # a reconnect: not logged again
    link.reset_channel()
    tp.send(b">")                       # a None reply is fine
    notes = [m for m in logs if "bridge build does not support" in m]
    assert len(notes) == 1
    assert "send_ms" in notes[0] and "rebuild it" in notes[0]
    # what that bridge then runs: only the fields it knows -> the "ecu" level
    old = {k: v for k, v in _gui_params(True).items() if k not in J.BridgeTp20.NEWER_PARAMS}
    assert old == {"block_size": 0x0F, "pace_ecu": True}


def test_current_bridge_reports_its_params_and_logs_nothing(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "ecu")
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    _FakeDev.ecu = RecordingEcu(T3_PROGRAMMING)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    logs = []
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000, params=_gui_params(True))
        TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
        tp.open()
        assert not logs
        tp.close()
    finally:
        dev.close()


def test_write_logs_the_level(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    assert E.tp20_level_text() == 'TP 2.0 send pacing: level "aggressive".'
    monkeypatch.setattr(E, "EDC16_TP20_FAST_TIMING", False)
    assert "5 ms" in E.tp20_level_text()


def _write_ecu():
    """Mock EDC16 for a whole CAN write: 10 ms on the first channel, 1 ms on
    the one set up in the programming session; every device write call takes
    0.6 ms on a fake clock."""
    written = bytearray()
    polls = {"n": 0}

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
        if s == 0x31:
            return bytes([0x71, req[1]])
        if s == 0x33 and req[1] == 0xC4:
            return bytes([0x73, 0xC4, 0x00])
        if s == 0x33 and req[1] == 0xC5:                 # mismatch, then match
            polls["n"] += 1
            return bytes([0x73, 0xC5, 0x00 if polls["n"] % 2 == 0 else 0x01])
        if s == 0x36:
            written.extend(req[1:])
            return b"\x76"
        if s == 0x37:
            return b"\x77"
        if s == 0x11:
            return bytes([0x7F, 0x11, 0x90])
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])

    class Ecu(ClockedEcu):
        def write_can(self, can_id, payload, extended=False, timeout_ms=100):
            if can_id == 0x200:
                n = getattr(self, "setup_count", 0) + 1
                self.interframe_byte = T3_DEFAULT if n == 1 else T3_PROGRAMMING
                self.channel = n
            if self.events is not None and can_id == self.ecu_rx and (payload[0] >> 4) == 2:
                self.events.append(("chan", self.channel))
            super().write_can(can_id, payload, extended, timeout_ms)

    ecu = Ecu()
    ecu.handler = handler
    ecu.events = []
    return ecu, written


def _image():
    img = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_START, E.FULL_SIZE):
        img[i] = (i * 5 + 1) & 0xFF
    return bytes(img)


def _check_write_waits(events, logs):
    # each wait after a queued frame, by the channel it was sent on
    per = {1: set(), 2: set()}
    chan = None
    for e in events:
        if e[0] == "chan":
            chan = e[1]
        elif e[0] == "wait" and chan is not None:
            per[chan].add(e[1])
            chan = None
    assert per[2] == {0.2}                         # 0.8 ms start to start
    assert per[1]                                  # multi-frame sends on channel 1 too
    assert all(w >= 7.0 for w in per[1])           # 10 ms channel: >= 8 ms a frame
    assert any('level "aggressive"' in m for m in logs)
    assert not any("backed off" in m or "bridge build" in m for m in logs)


@pytest.mark.slow
def test_aggressive_write_in_process(monkeypatch):
    """The shipped level, a whole CAN write in-process with the flasher's params."""
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    ecu, written = _write_ecu()
    monkeypatch.setattr(TT, "_clock", ecu.clock)
    _record_waits(monkeypatch, ecu.events)
    logs = []
    params = TT.Tp20Params(**_gui_params(True))
    link = TT.CanKLine(ecu, dest=0x01, params=params, log=logs.append)
    fl = E.Edc16Flash(link, log=logs.append)
    fl._fast = True
    fl.write_flash(_image(), force_full=True, allow_fast=False)
    assert len(written) > 0x3E000 and ecu.setup_count == 2
    _check_write_waits(ecu.events, logs)


@pytest.mark.slow
def test_aggressive_write_through_the_bridge(monkeypatch):
    """The same write through the bridge's serve() loop (the car's path)."""
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    ecu, written = _write_ecu()
    monkeypatch.setattr(B.J, "PassThru", _FakeDev)
    _FakeDev.ecu = ecu
    for mod in {TT, B.TP}:
        monkeypatch.setattr(mod, "_clock", ecu.clock)
    _record_waits(monkeypatch, ecu.events)
    dev = J.BridgePassThru("fake.dll", proc=_PipeProc("fake.dll"))
    logs = []
    try:
        tp = dev.open_tp20(dest=0x01, baud=500000,
                           params={"block_size": 0x0F, **E.edc16_tp20_params("write")})
        link = TT.CanKLine(tp=tp, dest=0x01, log=logs.append)
        fl = E.Edc16Flash(link, log=logs.append)
        fl._fast = True
        fl.write_flash(_image(), force_full=True, allow_fast=False)
        link.close()
    finally:
        dev.close()
    assert len(written) > 0x3E000 and ecu.setup_count == 2
    _check_write_waits(ecu.events, logs)


@pytest.mark.slow
def test_aggressive_read_in_process(monkeypatch):
    monkeypatch.setattr(E, "EDC16_TP20_LEVEL", "aggressive")
    ecu, cal, state = _read_ecu()
    link = TT.CanKLine(ecu, dest=0x01,
                       params=TT.Tp20Params(**E.edc16_tp20_params("read")))
    fl = E.Edc16Flash(link, log=lambda s: None)
    fl._fast = True
    img = fl.read_flash(speed="Medium", allow_fast=False, upload_chunk=0x10000)
    assert img[E.CAL_START:] == cal and ecu.setup_count == 2
