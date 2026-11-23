# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A J2534 device that refuses a K-line baud rate (an OpenPort's firmware stops near
125 kbaud, ERR_INVALID_BAUDRATE) is "no clean rate" for the EDC16 High-speed discovery,
which catches KLineError and falls back to Medium -- not a J2534Error that aborts the read."""
import pytest

from ovtsuite.flash import j2534 as J
from ovtsuite.flash.kline_j2534 import J2534KLine
from ovtsuite.flash.transport import KLineError


class _Channel:
    def __init__(self, refuse_above):
        self.refuse_above = refuse_above
        self.baud = 10400

    def set_baud(self, baud):
        if baud > self.refuse_above:
            raise J.J2534Error("PassThruIoctl 0x02: ERR_INVALID_BAUDRATE (0x19) SET_CONFIG failed")
        self.baud = baud


def _link(refuse_above):
    link = J2534KLine(dev=None)
    link._ch = _Channel(refuse_above)
    return link


def test_a_refused_rate_is_a_kline_error_the_discovery_can_step_past():
    link = _link(125000)
    link.set_baud(124800)
    assert link._ch.baud == 124800
    with pytest.raises(KLineError) as ex:
        link.set_baud(244898)
    assert "244898" in str(ex.value)
    assert link._ch.baud == 124800                     # the channel is left as it was


def test_before_the_channel_exists_the_rate_is_only_remembered():
    link = J2534KLine(dev=None)
    link.set_baud(244898)
    assert link._baud0 == 244898
