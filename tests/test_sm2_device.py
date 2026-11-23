# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The SM2 as a picker device (ovtsuite/flash/sm2_device.py) and its entries in
the flasher and diagnostics pickers."""
import pytest

from ovtsuite.diag import link
from ovtsuite.flash import gui_flash as G
from ovtsuite.flash import j2534 as J
from ovtsuite.flash import sm2_device as D
from ovtsuite.flash import sm2usb as S
from ovtsuite.flash.sm2can import Sm2CanChannel
from ovtsuite.flash.sm2kline import Sm2KLineChannel
from tests.test_sm2can import FakeCanSm2
from tests.test_sm2kline import FakeKLineSm2


class Both(FakeKLineSm2):
    """Answers the CAN set-up replies as well (block 3 -> status, 0x53 -> handle)."""

    def answer(self, cmd, body):
        if cmd == 0x52:
            return 0, bytes(20)
        return super().answer(cmd, body)


def test_connect_picks_the_channel_for_the_protocol():
    dev = D.Sm2Device(S.Sm2(FakeCanSm2()))
    assert isinstance(dev.connect(J.CAN, 500000), Sm2CanChannel)
    t = Both()
    dev = D.Sm2Device(S.Sm2(t))
    assert isinstance(dev.connect(J.ISO9141, 10400), Sm2KLineChannel)
    assert isinstance(dev.connect(J.ISO14230, 10400), Sm2KLineChannel)
    with pytest.raises(S.Sm2Error):
        dev.connect(J.ISO15765, 500000)
    assert dev.firmware == "A111788"


def test_a_new_channel_closes_the_previous_one():
    t = Both()
    dev = D.Sm2Device(S.Sm2(t))
    first = dev.connect(J.ISO9141, 10400)
    dev.connect(J.ISO14230, 10400)
    assert first.id is None                      # disconnected
    dev.close()


def test_flasher_picker_treats_the_sm2_like_the_openport(monkeypatch):
    monkeypatch.setattr(D, "find_devices", lambda: ["SM2 bus 1 addr 4"])
    assert (G.SM2_USB, "SM2 bus 1 addr 4 (Direct-USB)") in G.openport_devices()
    assert G.is_j2534(G.SM2_USB)
    assert G._port_key(G.SM2_USB) == ("sm2",)
    assert G._port_key(G.SM2_USB) != G._port_key(G.OPENPORT_USB)
    monkeypatch.setattr(D, "Sm2Device", lambda: "opened")
    assert G.open_j2534(G.SM2_USB) == "opened"


def test_diagnostics_picker_offers_and_opens_the_sm2(monkeypatch):
    monkeypatch.setattr(D, "find_devices", lambda: ["SM2 bus 1 addr 4"])
    assert any(i.data == link.SM2_USB and i.kind == "openport" for i in link.interfaces())
    iface = link.interface_from_data(link.SM2_USB)
    assert iface.kind == "openport" and iface.is_j2534_like if hasattr(iface, "is_j2534_like") else True
    monkeypatch.setattr(D, "Sm2Device", lambda: "opened")
    assert link.open_device(iface) == "opened"


def test_hint_only_when_an_sm2_is_there_but_unusable(monkeypatch):
    monkeypatch.setattr(S, "explain_absence", lambda: "no SM2 on USB (20A2:0001)")
    assert D.explain_absence() == ""
    monkeypatch.setattr(S, "explain_absence", lambda: "an SM2 is on USB but no driver")
    assert D.explain_absence().startswith("an SM2 is on USB")
