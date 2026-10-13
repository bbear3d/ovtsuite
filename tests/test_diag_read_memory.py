# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""``readMemoryByAddress (0x23)`` on the EDC16 diag session.

Why it matters: nineteen AST (gearbox speed-request) signals in BPW.a2l have
an ECU_ADDRESS and no measuring descriptor, so no group can ever show them.
0x23 is the only route, which makes its request framing and its refusal
behaviour load-bearing -- a silent wrong answer here would be read as the
gearbox asking for nothing, which is exactly the conclusion under test.
"""
import pytest

from ovtsuite.flash import kwp as _kwp
from ovtsuite.flash.diag import DiagSession


class _Msg:
    def __init__(self, body):
        self.body = body


class _FakeKwp:
    """Records requests; replies from a table, or refuses."""

    def __init__(self, table=None, refuse=False, echo=0):
        self.table = table or {}
        self.refuse = refuse
        self.echo = echo
        self.sent = []

    def send_receive(self, service, body=b"", exclude_addresses=False):
        self.sent.append((service, body))
        if self.refuse:
            raise _kwp.NegativeResponse(service, 0x31)
        data = self.table.get(bytes(body), b"\xAB\xCD")
        return _Msg(b"\x00" * self.echo + data)


def _sess(fake):
    s = DiagSession.__new__(DiagSession)
    s.kwp = fake
    s.kw = None
    return s


def test_it_frames_a_three_byte_address_and_a_length():
    f = _FakeKwp()
    _sess(f).read_memory(0x802EC3, 2)
    assert f.sent == [(0x23, b"\x80\x2E\xC3\x02")]


@pytest.mark.parametrize("width, expect", [
    (3, b"\x80\x2E\xC3\x02"),
    (4, b"\x00\x80\x2E\xC3\x02"),
])
def test_the_address_width_is_selectable(width, expect):
    """A calibration that refuses 3-byte addressing may accept 4, so the
    probe tries each; the framing must follow the width it was asked for."""
    f = _FakeKwp()
    _sess(f).read_memory(0x802EC3, 2, addr_bytes=width)
    assert f.sent[0][1] == expect


def test_an_address_too_big_for_the_width_is_a_clear_error():
    """The probe walks 3, 4 then 2 bytes, and every AST address is above
    0x7F0000, so width 2 is always tried. to_bytes would raise
    OverflowError there, which reads as a tool bug rather than an
    unusable width."""
    with pytest.raises(ValueError, match="does not fit"):
        _sess(_FakeKwp()).read_memory(0x802EC3, 2, addr_bytes=2)


def test_a_width_that_does_fit_is_framed():
    f = _FakeKwp()
    _sess(f).read_memory(0x2EC3, 2, addr_bytes=2)
    assert f.sent[0][1] == b"\x2E\xC3\x02"


def test_a_refusal_is_none_not_an_exception():
    """Plenty of EDC16 calibrations serve 0x23 only for flash, or not at
    all. The probe has to tell that apart from a read that worked."""
    assert _sess(_FakeKwp(refuse=True)).read_memory(0x7F9D00, 2) is None


def test_it_returns_exactly_the_requested_width():
    f = _FakeKwp({b"\x7F\x9D\x00\x02": b"\x01\x90"})
    assert _sess(f).read_memory(0x7F9D00, 2) == b"\x01\x90"


def test_an_echoed_address_is_stripped_from_the_front():
    """Some stacks repeat the address ahead of the data. Taking the first
    bytes would decode the address as the value -- a plausible-looking
    number, which is the worst kind of wrong."""
    f = _FakeKwp({b"\x7F\x9D\x00\x02": b"\x7F\x9D\x00\x01\x90"})
    assert _sess(f).read_memory(0x7F9D00, 2) == b"\x01\x90"


def test_a_short_reply_is_none_rather_than_padded():
    f = _FakeKwp({b"\x7F\x9D\x00\x04": b"\x01"})
    assert _sess(f).read_memory(0x7F9D00, 4) is None


def test_kw1281_has_no_such_service():
    s = DiagSession.__new__(DiagSession)
    s.kwp = None
    s.kw = object()
    assert s.read_memory(0x7F9D00, 2) is None


@pytest.mark.parametrize("addr_bytes", [1, 5, 0])
def test_a_nonsense_address_width_is_rejected(addr_bytes):
    with pytest.raises(ValueError):
        _sess(_FakeKwp()).read_memory(0x7F9D00, 2, addr_bytes=addr_bytes)


@pytest.mark.parametrize("length", [0, 256])
def test_a_nonsense_length_is_rejected(length):
    with pytest.raises(ValueError):
        _sess(_FakeKwp()).read_memory(0x7F9D00, length)
