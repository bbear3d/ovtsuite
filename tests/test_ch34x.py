# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CH340/CH341 direct (libusb) port: baud divisors, names, and the pyserial-shaped surface."""
import pytest

from ovtsuite.flash import ch34x, ftdi


def _rate(div, factor):
    clk = {3: 6000000, 2: 750000, 1: 93750, 0: 11719}[div]
    return clk / (256 - factor)


@pytest.mark.parametrize("want", [10400, 38400, 124800, 244898])
def test_divisor_close_to_requested(want):
    div, factor = ch34x.divisor_for(want)
    assert abs(_rate(div, factor) - want) / want < 0.025


def test_divisor_rejects_out_of_range():
    for bad in (0, -5, 3000000):
        with pytest.raises(ch34x.Ch34xError):
            ch34x.divisor_for(bad)


def test_names_route_through_the_ftdi_facade():
    assert ch34x.is_ch34x_name("ch34x://1a86:7523:14:9")
    assert ftdi.is_ftdi_name("ch34x://1a86:7523:ABC")
    assert not ch34x.is_ch34x_name("/dev/cu.usbserial-1410")


def test_no_gpl_marker_in_module():
    import inspect
    assert "SPDX-License-Identifier: GPL" not in inspect.getsource(ch34x)
