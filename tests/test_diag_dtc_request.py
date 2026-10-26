# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""``readDiagnosticTroubleCodesByStatus (0x18)`` on the EDC16 diag session.

P475 software (03G906016GC) takes statusOfDTC 00 only: its 0x18 descriptor
lists status 00 and groups FF00/0000, so the ``18 02 FF 00`` other EDC16s
answer draws ``7F 18 12`` there (Reference/Logs/CANReadCodesFailEDC16.txt).
"""
import pytest

from ovtsuite.flash import kwp as _kwp
from ovtsuite.flash.diag import DiagSession


class _Msg:
    def __init__(self, body):
        self.body = body


class _P475Kwp:
    """Answers 0x18 only with status 00, as the GC firmware does."""

    def __init__(self, accept=b"\x00\xFF\x00"):
        self.accept = accept
        self.sent = []

    def send_receive(self, service, body=b"", exclude_addresses=False):
        self.sent.append((service, bytes(body)))
        if bytes(body) != self.accept:
            raise _kwp.NegativeResponse(service, 0x12)
        return _Msg(bytes([0x01, 0x46, 0x8B, 0x24]))     # one code: 18059


def _sess(fake):
    s = DiagSession.__new__(DiagSession)
    s.kwp = fake
    s.kw = None
    s._dtc_form = None
    return s


def test_status_00_is_tried_when_02_is_refused():
    f = _P475Kwp()
    s = _sess(f)
    dtcs = s.read_dtcs()
    assert [d.code for d in dtcs] == [0x468B]
    assert f.sent == [(0x18, b"\x02\xFF\x00"), (0x18, b"\x00\xFF\x00")]
    # the accepted form is remembered for the session
    f.sent.clear()
    s.read_dtcs()
    assert f.sent == [(0x18, b"\x00\xFF\x00")]


def test_an_ecu_taking_02_is_asked_once():
    f = _P475Kwp(accept=b"\x02\xFF\x00")
    _sess(f).read_dtcs()
    assert f.sent == [(0x18, b"\x02\xFF\x00")]


def test_other_refusals_are_not_retried():
    class _Locked(_P475Kwp):
        def send_receive(self, service, body=b"", exclude_addresses=False):
            self.sent.append((service, bytes(body)))
            raise _kwp.NegativeResponse(service, 0x33)
    f = _Locked()
    with pytest.raises(_kwp.NegativeResponse):
        _sess(f).read_dtcs()
    assert len(f.sent) == 1
