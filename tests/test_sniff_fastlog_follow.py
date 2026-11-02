# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from ovtsuite.sniff.kline import Framer, s0bg_to_baud


def _frame(body, dest=0x01, src=0xF1):
    h = bytes([0x80 | len(body), dest, src]) + bytes(body)
    return h + bytes([sum(h) & 0xFF])


def test_s0bg_rates():
    assert s0bg_to_baud(0x03) == 125000
    assert s0bg_to_baud(0x2F) == 10400
    assert s0bg_to_baud(0x33) == 9600


def test_tap_follows_fast_request_and_stop():
    f = Framer("kwp")
    f.feed(1.0, _frame([0x10, 0x90, 0x03, 0x02, 0x12, 0x34, 0x56, 0x78]))
    assert f.new_baud == 125000
    f.new_baud = None
    f.feed(2.0, _frame([0x10, 0x91, 0x03, 0, 0, 0]))
    assert f.new_baud == 125000
    f.new_baud = None
    f.feed(3.0, _frame([0x10, 0x90, 0x2F, 0x01, 0x86, 0xD0]))
    assert f.new_baud == 10400


def test_iso_speed_change_still_followed():
    f = Framer("kwp")
    f.feed(1.0, _frame([0x10, 0x89, 0x87]))
    f.feed(1.1, _frame([0x50, 0x89, 0x87], dest=0xF1, src=0x01))
    assert f.new_baud == 124800
