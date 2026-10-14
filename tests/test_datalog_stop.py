# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's Disconnect has to land quickly, whatever the link is doing.

Disconnect only set a flag that the worker read between rows, while every
layer underneath could wait far longer: a responsePending loop with no bound,
the K-line wake-up retries, a dozen measuring blocks per row, an 8 s read
timeout, and a TP 2.0 stopCommunication that waited for an answer from the
ECU the session had just lost. These pin the stop checks and the bounds.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite.flash import diag as D  # noqa: E402
from ovtsuite.flash import kwp as K  # noqa: E402
from ovtsuite.flash.transport import KLine, KLineError, KLineTimeout, LinkStopped  # noqa: E402


class _ByteLink:
    """Serves a canned reply stream to Kwp2000.receive."""

    def __init__(self, replies):
        self.rx = bytearray(b"".join(replies))
        self.sent = []
        self.read_timeout = 8.0

    def write_bytes(self, data):
        self.sent.append(bytes(data))

    def write_byte(self, b):
        self.sent.append(bytes([b]))

    def read_byte(self):
        if not self.rx:
            raise KLineTimeout("empty")
        return self.rx.pop(0)

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        if len(out) < n:
            raise KLineTimeout("short")
        return out


def _frame(service, body):
    raw = bytes([0x80 | (1 + len(body)), 0xF1, 0x10, service]) + bytes(body)
    return raw + bytes([sum(raw) & 0xFF])


def _pending(service):
    return _frame(0x7F, [service, 0x78])


def test_response_pending_is_bounded():
    link = _ByteLink([_pending(0x21)] * (K.MAX_PENDING + 5))
    d = K.Kwp2000(link, 0x10)
    d.P3 = 0
    d.fast = True
    with pytest.raises(KLineTimeout):
        d.send_receive(0x21, b"\x01")


def test_response_pending_still_waits_for_the_real_answer():
    link = _ByteLink([_pending(0x21)] * 3 + [_frame(0x61, [0x01, 0x02])])
    d = K.Kwp2000(link, 0x10)
    d.P3 = 0
    d.fast = True
    assert d.send_receive(0x21, b"\x01").body == b"\x01\x02"


def test_response_pending_gives_up_on_stop():
    link = _ByteLink([_pending(0x21)] * 10)
    d = K.Kwp2000(link, 0x10)
    d.P3 = 0
    d.fast = True
    d.is_stop = lambda: True
    with pytest.raises(LinkStopped):
        d.send_receive(0x21, b"\x01")
    assert len(link.rx) > 0, "it must stop at the first pending frame"


class _GroupSession(D.DiagSession):
    def __init__(self, stop_after):
        super().__init__(link=None)
        self.read = []
        self._stop_after = stop_after

    def read_group(self, group):
        self.read.append(group)
        return []


def test_read_groups_checks_stop_between_groups():
    s = _GroupSession(2)
    s._is_stop = lambda: len(s.read) >= 2
    with pytest.raises(D.StopRequested):
        s.read_groups([1, 2, 3, 4, 5])
    assert s.read == [1, 2]
    # an explicit hook wins over the connect-time one
    s.read.clear()
    assert set(s.read_groups([7, 8], is_stop=lambda: False)) == {7, 8}


def test_kline_wake_up_retries_give_up_on_stop(monkeypatch):
    k = KLine.__new__(KLine)
    k.prefer_fast_init = True
    calls = []
    stop = {"now": False}

    def once(*_a):
        calls.append(1)
        stop["now"] = True
        raise KLineError("no answer")
    monkeypatch.setattr(k, "_fast_init", once, raising=False)
    monkeypatch.setattr(k, "_wake_up_once", once, raising=False)
    k.is_stop = lambda: stop["now"]
    with pytest.raises(LinkStopped):
        k.wake_up()
    assert calls == [1]


class _TpLink:
    is_tp20 = True

    def __init__(self):
        self.read_timeout = 8.0
        self.sent = []
        self.tp = type("Tp", (), {"is_stop": None, "opened": True})()

    def write_bytes(self, data):
        self.sent.append(bytes(data))


def test_close_over_tp20_skips_stop_communication():
    link = _TpLink()
    s = D.DiagSession(link, "EDC16")
    s.kwp = K.Kwp2000(link, 0x10)
    s.kwp.P3 = 0
    s.kwp.fast = True
    s.close()
    assert link.sent == [], "0x82 over TP 2.0 waits on the ECU; the A8 close ends it"
    assert link.read_timeout == D.CLOSE_READ_TIMEOUT


def test_close_over_kline_still_says_goodbye_but_briefly():
    link = _TpLink()
    link.is_tp20 = False
    s = D.DiagSession(link, "EDC16")
    s.kwp = K.Kwp2000(link, 0x10)
    s.kwp.P3 = 0
    s.kwp.fast = True
    s.close()
    assert link.sent and link.sent[0][3] == 0x82
    assert link.read_timeout <= D.CLOSE_READ_TIMEOUT


def test_connect_arms_the_link_and_a_local_tp20_channel():
    link = _TpLink()
    link.is_stop = None
    s = D.DiagSession(link, "EDC16")
    hook = lambda: False  # noqa: E731
    s._arm_stop(hook)
    assert link.is_stop is hook and link.tp.is_stop is hook
    # a hook the link's owner installed is kept
    mine = lambda: True  # noqa: E731
    link2 = _TpLink()
    link2.is_stop = None
    link2.tp.is_stop = mine
    D.DiagSession(link2, "EDC16")._arm_stop(hook)
    assert link2.tp.is_stop is mine


def test_can_kline_pending_wait_gives_up_on_stop():
    from ovtsuite.flash.tp20_transport import CanKLine

    class _Tp:
        opened = True

        def recv(self, timeout_ms):
            return bytes([0x7F, 0x21, 0x78])
    link = CanKLine(tp=_Tp())
    n = {"c": 0}

    def stop():
        n["c"] += 1
        return n["c"] > 2
    link.is_stop = stop
    with pytest.raises(KLineError):
        link._recv_final()
    assert n["c"] == 3
