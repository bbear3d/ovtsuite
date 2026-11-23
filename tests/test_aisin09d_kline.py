# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Aisin TRx (09D) over K-line: Aisin09dFlash(kline=...) against the ECU-side
K-line simulator (trx_kline_sim.KLineEcu) over an in-memory duplex wire.

The wire is two byte queues, so the real ISO 14230 framing on both sides is
exercised (the tester's Kwp2000, the simulator's frame reader); only the serial
port and the 5-baud init timing are left out."""
import os
import queue
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import aisin09d as A
from ovtsuite.flash import trx_kline_sim as KS
from ovtsuite.flash import trx_sim as T
from ovtsuite.flash import kwp as _kwp
from ovtsuite.flash.transport import KLineTimeout

_kwp.Kwp2000.P3 = 0.0          # the 55 ms inter-message gap would make a 1 MiB read take minutes


class _End:
    """One end of the wire: reads from ``rx``, writes to ``tx``."""

    def __init__(self, rx, tx):
        self.rx, self.tx = rx, tx
        self._timeout = 3.0
        self.woken = None

    # tester-side surface the Kwp2000 / channel uses
    @property
    def read_timeout(self):
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, s):
        self._timeout = s

    def read_byte(self):
        try:
            return self.rx.get(timeout=self._timeout)
        except queue.Empty:
            raise KLineTimeout("test wire: read timed out")

    def read(self, n):
        return bytes(self.read_byte() for _ in range(n))

    def write_bytes(self, data):
        for b in bytes(data):
            self.tx.put(b)

    def write_byte(self, b):
        self.tx.put(b)

    def clear_rx(self):
        pass

    def set_baud(self, baud):
        pass

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        self.woken = address                     # the real init is the link's job
        return 0x2000


def _serve(img):
    a2b, b2a = queue.Queue(), queue.Queue()
    tester, ecu_end = _End(b2a, a2b), _End(a2b, b2a)
    resp = T.Trx09dResponder(T.aisin09d_profile(), image=img, read_mode="serve")
    ecu = KS.KLineEcu(ecu_end, resp, controller=A.K_ADDRESS, echo=False)
    ecu.do_init = lambda *a, **k: True           # the wake-up is faked on the tester side
    ecu_end._timeout = 3.0
    t = threading.Thread(target=ecu.run, daemon=True)
    t.start()
    return tester, t


def test_read_over_kline_matches_served_image():
    img = bytes((i * 31 + 7) & 0xFF for i in range(0x100000))
    link, th = _serve(img)
    flash = A.Aisin09dFlash(kline=link)
    pcts = []
    out = flash.read_image(on_percent=pcts.append, image_size=0x1500)
    th.join(5)
    assert link.woken == A.K_ADDRESS, "the 5-baud wake-up must target the TCU address"
    assert out == img[:0x1500]
    assert pcts and pcts[-1] == 100
    assert not th.is_alive(), "stopCommunication should end the simulator's session"


def test_long_response_uses_length_byte():
    # a 0xFE-byte block makes a 255-byte response (> 63): the ISO 14230 length
    # byte path, on both the simulator's framing and the tester's
    img = bytes((i * 5) & 0xFF for i in range(0x100000))
    link, th = _serve(img)
    flash = A.Aisin09dFlash(kline=link)
    out = flash.read_image(image_size=0x200)      # 0xFE + 0xFE + 4
    th.join(5)
    assert out == img[:0x200]


class _Rec(_End):
    """A tester end that records baud changes and how the wake-up got in."""

    def __init__(self, *a, init="fast"):
        super().__init__(*a)
        self.bauds = []
        self.last_init = init
        self.fast_target = 0x10          # as the real links: EDC16's until told otherwise

    def set_baud(self, baud):
        self.bauds.append(baud)


class _PickyEcu(KS.KLineEcu):
    """An ECU that only answers frames in one header form: ``accept`` is None
    (a header with no addresses) or the one address it is asked at."""
    accept = "any"

    def _read_frame(self):
        got = super()._read_frame()
        if self.accept == "any" or got is None:
            return got
        dest, src, payload = got
        if dest != self.accept:                  # includes dest None vs an address
            return (None, None, b"")              # ignored: the serve loop skips an empty one
        return got


def _serve_rec(img, refuse_raise=False, init="fast", accept="any"):
    a2b, b2a = queue.Queue(), queue.Queue()
    tester, ecu_end = _Rec(b2a, a2b, init=init), _End(a2b, b2a)
    inner = T.Trx09dResponder(T.aisin09d_profile(), image=img, read_mode="serve")

    class R:
        def handle(self, payload):
            if refuse_raise and payload[:1] == b"\x10" and len(payload) == 3:
                return bytes([0x7F, 0x10, 0x12])            # subFunctionNotSupported
            return inner.handle(payload)

    ecu = _PickyEcu(ecu_end, R(), controller=0x10, echo=False)
    ecu.accept = accept
    ecu.do_init = lambda *a, **k: True
    t = threading.Thread(target=ecu.run, daemon=True)
    t.start()
    return tester, t


def test_fast_init_targets_the_request_address():
    img = bytes(0x400)
    link, th = _serve_rec(img, init="fast")
    flash = A.Aisin09dFlash(kline=link)
    flash.read_image(image_size=0x100)
    th.join(5)
    assert link.fast_target == A.FAST_INIT_TARGET   # StartCommunication goes there


def test_slow_init_wakes_at_the_5_baud_address_not_the_request_address():
    link, th = _serve_rec(bytes(0x400), init="slow")
    flash = A.Aisin09dFlash(kline=link)
    flash.read_image(image_size=0x100)
    th.join(5)
    assert link.woken == A.K_ADDRESS != A.K_REQUEST_ADDRESS


def test_header_detection_prefers_no_address_header():
    # the 09D answered "02 1A 9B ck" on the bench, so that is tried first
    img = bytes((i * 3) & 0xFF for i in range(0x400))
    link, th = _serve_rec(img, accept=None)
    flash = A.Aisin09dFlash(kline=link)
    out = flash.read_image(image_size=0x200)
    th.join(5)
    assert flash.tp.addressless is True
    assert out == img[:0x200]


def test_header_detection_falls_back_to_the_unit_address():
    img = bytes((i * 3) & 0xFF for i in range(0x400))
    link, th = _serve_rec(img, accept=A.K_REQUEST_ADDRESS)
    flash = A.Aisin09dFlash(kline=link)
    out = flash.read_image(image_size=0x200)
    th.join(5)
    assert flash.tp.addressless is False
    assert flash.tp.kwp.controller == A.K_REQUEST_ADDRESS
    assert out == img[:0x200]


def test_header_detection_finds_0x10_last():
    img = bytes((i * 3) & 0xFF for i in range(0x400))
    link, th = _serve_rec(img, accept=0x10)
    flash = A.Aisin09dFlash(kline=link)
    out = flash.read_image(image_size=0x200)
    th.join(5)
    assert flash.tp.kwp.controller == 0x10 and not flash.tp.addressless
    assert out == img[:0x200]


def test_baud_escalation_then_read():
    img = bytes((i * 3) & 0xFF for i in range(0x1000))
    link, th = _serve_rec(img)
    flash = A.Aisin09dFlash(kline=link, speed="Medium")
    out = flash.read_image(image_size=0x300)
    th.join(5)
    assert link.bauds == [124800]
    assert out == img[:0x300]


def test_refused_escalation_stays_at_10400():
    img = bytes((i * 3) & 0xFF for i in range(0x1000))
    link, th = _serve_rec(img, refuse_raise=True)
    flash = A.Aisin09dFlash(kline=link, speed="High")
    out = flash.read_image(image_size=0x300)
    th.join(5)
    assert link.bauds == []
    assert out == img[:0x300]


def _serve_high_baud_dead(img, hears_return=True):
    """A TCU that accepts the speed byte and then answers nothing -- until it
    is told (``10 86 00``) to leave the high baud, if ``hears_return``."""
    a2b, b2a = queue.Queue(), queue.Queue()
    tester, ecu_end = _Rec(b2a, a2b), _End(a2b, b2a)
    inner = T.Trx09dResponder(T.aisin09d_profile(), image=img, read_mode="serve")
    state = {"high": False, "sessions": []}

    class R:
        def handle(self, p):
            if p[:1] == b"\x10":
                state["sessions"].append(bytes(p))
            if state["high"]:
                if p == bytes([0x10, 0x86, 0x00]) and hears_return:
                    state["high"] = False
                    return inner.handle(bytes([0x10, 0x86]))
                return None
            if p[:1] == b"\x10" and len(p) == 3 and p[2] != 0:
                state["high"] = True
                return inner.handle(bytes(p[:2]))
            return inner.handle(p)

    ecu = _PickyEcu(ecu_end, R(), controller=0x10, echo=False)
    ecu.do_init = lambda *a, **k: True
    tester._timeout = 0.3
    ecu_end._timeout = 3.0
    t = threading.Thread(target=ecu.run, daemon=True)
    t.start()
    return tester, t, state


def test_session_start_is_sent_once_when_the_baud_is_raised():
    img = bytes((i * 3) & 0xFF for i in range(0x1000))
    link, th = _serve_rec(img)
    seen = []
    flash = A.Aisin09dFlash(kline=link, speed="Medium")
    real = flash._request

    def spy(payload, *a, **k):
        if payload[:1] == b"\x10":
            seen.append(bytes(payload))
        return real(payload, *a, **k)

    flash._request = spy
    assert flash.read_image(image_size=0x100) == img[:0x100]
    th.join(5)
    assert seen == [bytes([0x10, 0x86, 0x87])]        # one start, speed byte inside it


def test_a_failed_high_baud_check_tells_the_tcu_to_leave_it():
    img = bytes((i * 3) & 0xFF for i in range(0x1000))
    link, th, state = _serve_high_baud_dead(img)
    flash = A.Aisin09dFlash(kline=link, speed="Medium")
    out = flash.read_image(image_size=0x100)
    th.join(5)
    assert link.bauds == [124800, 10400]              # up, then back with the TCU's consent
    assert bytes([0x10, 0x86, 0x00]) in state["sessions"]
    assert out == img[:0x100]


def test_a_tcu_that_stays_at_the_high_baud_fails_the_read_cleanly():
    img = bytes((i * 3) & 0xFF for i in range(0x1000))
    link, th, state = _serve_high_baud_dead(img, hears_return=False)
    flash = A.Aisin09dFlash(kline=link, speed="Medium")
    with pytest.raises(A.KLineError, match="not answering at 10400 either"):
        flash.read_image(image_size=0x100)
    assert link.bauds[-1] == 10400


def test_low_speed_never_asks_for_a_raise():
    link, th = _serve_rec(bytes(0x400))
    A.Aisin09dFlash(kline=link, speed="Low").read_image(image_size=0x100)
    th.join(5)
    assert link.bauds == []


def test_kline_size_is_automatic():
    assert A.KLINE_IMAGE_SIZE is None


def _small_part(img512, mode):
    """An ECU whose flash is 512 KiB: it NAKs, goes quiet, or wraps past the end."""
    a2b, b2a = queue.Queue(), queue.Queue()
    tester, ecu_end = _End(b2a, a2b), _End(a2b, b2a)
    tester._timeout = 0.3
    inner = T.Trx09dResponder(T.aisin09d_profile(), image=img512 + b"\xFF" * 0x80000,
                              read_mode="serve")

    class R:
        def handle(self, p):
            if p[0] == 0x23:
                addr = p[1] << 16 | p[2] << 8 | p[3]
                if addr >= 0x80000:
                    if mode == "nak":
                        return bytes([0x7F, 0x23, 0x31])        # requestOutOfRange
                    if mode == "quiet":
                        return None
                    if mode == "wrap":
                        q = bytes([p[0], (addr - 0x80000) >> 16 & 0xFF,
                                   (addr - 0x80000) >> 8 & 0xFF, addr & 0xFF, p[4]])
                        return inner.handle(q)
            return inner.handle(p)

    ecu = KS.KLineEcu(ecu_end, R(), echo=False)
    ecu.do_init = lambda *a, **k: True
    ecu_end._timeout = 3.0
    t = threading.Thread(target=ecu.run, daemon=True)
    t.start()
    return tester, t


def test_auto_size_reads_512k_part_that_naks_past_the_end():
    img = bytes((i * 7 + 1) & 0xFF for i in range(0x80000))
    link, th = _small_part(img, "nak")
    out = A.Aisin09dFlash(kline=link).read_image(image_size=None)
    assert out == img


def test_auto_size_does_not_cut_at_512k_when_the_part_just_goes_quiet():
    """A timeout at 0x80000 is not a definite end: it is asked again, and a
    link that stays silent fails the read instead of passing 512 KiB off as
    the whole flash (only the out-of-range refusal ends the image there)."""
    img = bytes((i * 7 + 1) & 0xFF for i in range(0x80000))
    link, th = _small_part(img, "quiet")
    log = []
    with pytest.raises(A.KLineError, match="no answer at 0x080000 after 3 tries"):
        A.Aisin09dFlash(kline=link, log=log.append).read_image(image_size=None)
    assert sum("asking again" in m for m in log) == 2


def test_auto_size_retries_a_lost_answer_at_512k_and_then_reads_on():
    img = bytes((i * 7 + 1) & 0xFF for i in range(0x80000))
    link, th = _small_part(img, "nak")
    flash = A.Aisin09dFlash(kline=link)
    real = flash._request
    seen = {"n": 0}

    def flaky(payload, *a, **k):
        if payload[:1] == b"\x23" and payload[1:4] == b"\x08\x00\x00":
            seen["n"] += 1
            if seen["n"] == 1:
                raise A.KLineTimeout("a lost frame")
        return real(payload, *a, **k)

    flash._request = flaky
    assert flash.read_image(image_size=None) == img
    assert seen["n"] == 2                     # asked again, then the refusal cut it


def test_auto_size_only_cuts_on_out_of_range_not_any_refusal():
    img = bytes((i * 7 + 1) & 0xFF for i in range(0x80000))
    link, th = _small_part(img, "nak")
    flash = A.Aisin09dFlash(kline=link)
    real = flash._request

    def other_nak(payload, *a, **k):
        if payload[:1] == b"\x23" and payload[1:4] == b"\x08\x00\x00":
            return bytes([0x7F, 0x23, 0x22])           # conditionsNotCorrect
        return real(payload, *a, **k)

    flash._request = other_nak
    with pytest.raises(A.KLineError, match="not the out-of-range"):
        flash.read_image(image_size=None)


def test_auto_size_reads_512k_part_that_wraps():
    img = bytes((i * 7 + 1) & 0xFF for i in range(0x80000))
    link, th = _small_part(img, "wrap")
    out = A.Aisin09dFlash(kline=link).read_image(image_size=None)
    assert out == img


def test_auto_size_keeps_a_real_1mib_image():
    import random
    img = random.Random(1).randbytes(0x100000)       # halves differ, as a real flash's do
    link, th = _serve(img)
    out = A.Aisin09dFlash(kline=link).read_image(image_size=None)
    assert len(out) == 0x100000 and out == img


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
