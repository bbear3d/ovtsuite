# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which header form a K-line module answers (Kwp2000.detect_header), and OVT
Diag's session using it: a module woken by 5-baud init is not always asked at
its own address -- the EDC16 is asked at 0x10, the 09D TCU answers a header with
no address bytes. Driven against the K-line ECU simulator over an in-memory
wire (no module definitions needed)."""
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import kwp as K
from ovtsuite.flash import trx_kline_sim as KS
from ovtsuite.diag import link as L
from ovtsuite.diag.session import ModuleSession

from test_aisin09d_kline import _End, _Rec, _PickyEcu

K.Kwp2000.P3 = 0.0

_IDENT = bytes.fromhex(
    "5A 9B 30 33 48 39 30 36 30 33 32 45 44 20 37 38 37 34 03 00 04 97 00 00 0E 2C "
    "7A B6 54 2D 47 50 20 4C 45 56 32 20 20 20 20 20 20 20 47 20 20 20")


class _Module:
    """A KWP2000 module: sessions and the ident record, nothing else."""

    def handle(self, p):
        if p[0] == 0x10:
            return bytes([0x50, p[1]])
        if p[0] == 0x1A:
            return _IDENT
        if p[0] == 0x82:
            return b"\xC2"
        return bytes([0x7F, p[0], 0x11])


def _wire(accept):
    a2b, b2a = queue.Queue(), queue.Queue()
    tester, ecu_end = _Rec(b2a, a2b, init="slow"), _End(a2b, b2a)
    ecu = _PickyEcu(ecu_end, _Module(), controller=0x10, echo=False)
    ecu.accept = accept
    ecu.do_init = lambda *a, **k: True
    ecu_end._timeout = 20.0              # outlasts every wait the tester makes
    t = threading.Thread(target=ecu.run, daemon=True)
    t.start()
    return tester, t


def _session(accept, row=None):
    tester, th = _wire(accept)
    row = row or {"addr": "02", "name": "Transmission", "kline": {"address": 2}}
    sess = ModuleSession(L.Link(tester, [], "kline"), row)
    sess.connect()
    return sess, tester, th


def test_header_candidates_order():
    # no address first, then address*16 and the address, then the engine ECU's 0x10
    assert K.header_candidates(2, 2) == [None, 0x20, 0x02, 0x10]
    assert K.header_candidates(1, 1) == [None, 0x10, 0x01]
    assert K.header_candidates(None, 0x03) == [None, 0x30, 0x03, 0x10]


def test_a_module_that_answers_no_address_headers():
    sess, link, th = _session(accept=None)
    assert sess.kwp.addressless is True
    assert sess.info.reached and "03H906032ED" in sess.identity


def test_a_module_that_answers_its_address_times_16():
    sess, link, th = _session(accept=0x20)
    assert sess.kwp.addressless is False and sess.kwp.controller == 0x20
    assert sess.info.reached and "03H906032ED" in sess.identity


def test_the_engine_ecu_form_is_still_found():
    # woken at 0x01, asked at 0x10: the EDC16's way
    sess, link, th = _session(accept=0x10,
                              row={"addr": "01", "name": "Engine", "kline": {"address": 1}})
    assert sess.kwp.controller == 0x10 and not sess.kwp.addressless
    assert sess.info.reached


def test_a_given_header_is_used_without_searching():
    row = {"addr": "02", "name": "Transmission", "kline": {"address": 2}, "kwp_header": "0x1A"}
    sess, link, th = _session(accept=0x1A, row=row)
    assert sess.kwp.controller == 0x1A and not sess.kwp.addressless


def test_a_wrong_given_header_falls_back_to_a_search():
    row = {"addr": "02", "name": "Transmission", "kline": {"address": 2}, "kwp_header": "0x1A"}
    sess, link, th = _session(accept=None, row=row)
    assert sess.kwp.addressless is True
    assert sess.info.reached


# ---- the logger's connect (flash.diag.DiagSession) --------------------------

def _logger(ecu, accept, timeout=0.5):
    from ovtsuite.flash.diag import DiagSession
    tester, th = _wire(accept)
    tester._timeout = timeout
    sess = DiagSession(tester, ecu)
    sess.connect("Low")
    return sess, tester


def test_logger_edc16_connect_is_unchanged_when_the_ecu_answers_0x10():
    sess, link = _logger("EDC16", accept=0x10)
    assert sess.kwp.controller == 0x10 and not sess.kwp.addressless
    assert "03H906032ED" in sess.identity


def test_logger_falls_back_when_nothing_answers_at_0x10():
    # a KWP2000 module that is not an EDC16: asked at 0x10 it is silent
    sess, link = _logger("EDC16", accept=None)
    assert sess.kwp.addressless is True
    assert "03H906032ED" in sess.identity


def test_logger_connects_the_09d_over_k_line_without_addresses():
    sess, link = _logger("Aisin TRx", accept=None)
    assert link.woken == 0x02
    assert sess.kwp.addressless is True


def test_logger_connects_the_09d_over_k_line_by_its_own_address():
    sess, link = _logger("Aisin TRx", accept=0x1A)
    assert not sess.kwp.addressless and sess.kwp.controller == 0x1A
    assert sess.identity


def test_logger_09d_k_line_says_so_when_it_answers_no_header_form():
    import pytest
    from ovtsuite.flash.transport import KLineError
    with pytest.raises(KLineError, match="no header form"):
        _logger("Aisin TRx", accept=0x77)


def test_detect_header_leaves_the_dialog_alone_when_nothing_answers():
    tester, th = _wire(accept=0x77)
    tester._timeout = 0.2
    d = K.Kwp2000(tester, controller=0x42)
    d.fast = True
    assert d.detect_header([None, 0x20], timeout=0.2) is None
    assert d.controller == 0x42 and d.addressless is False
