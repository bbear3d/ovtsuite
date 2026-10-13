# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Finding head units on the network, and what a probe is allowed to cost.

The sweep is the protocol itself asked of many addresses, so most of what is
worth pinning here is about restraint: a probe must not take the cable, must not
tell a non-head-unit apart from silence in a way that fills the list with noise,
and must stop when the dialog says stop.
"""
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import discover, headunits  # noqa: E402
from mock_bridge import MockBridge  # noqa: E402


@pytest.fixture
def bridge():
    server = MockBridge()
    try:
        yield server
    finally:
        server.close()


def test_a_probe_reports_what_the_greeting_said(bridge):
    found = discover.probe("127.0.0.1", bridge.port, timeout=5.0, context=False)
    assert found is not None
    assert (found.host, found.port) == ("127.0.0.1", bridge.port)
    assert found.cable == "ch34x"
    assert found.five_baud is True and found.can is False
    assert found.max_speed == 0x87
    assert found.paired is False


def test_a_probe_does_not_take_the_cable(bridge):
    """A head unit driving gauges keeps driving them. This is what lets the scan
    run from a dialog without asking the driver first."""
    discover.probe("127.0.0.1", bridge.port, timeout=5.0, context=False)
    assert bridge.claims == []
    assert bridge.holder is None


def test_nothing_listening_is_not_a_head_unit():
    spare = socket.socket()
    spare.bind(("127.0.0.1", 0))
    port = spare.getsockname()[1]
    spare.close()                                   # closed: the connect is refused

    assert discover.probe("127.0.0.1", port, timeout=0.5, context=False) is None


def test_something_else_on_the_port_is_not_a_head_unit():
    """A port that accepts and then says something that is not our framing is the
    same answer as silence -- to a driver scanning a subnet they mean the same."""
    other = socket.socket()
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    port = other.getsockname()[1]

    def babble():
        try:
            client, _ = other.accept()
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            client.close()
        except OSError:
            pass

    thread = threading.Thread(target=babble, daemon=True)
    thread.start()
    try:
        assert discover.probe("127.0.0.1", port, timeout=1.0, context=False) is None
    finally:
        other.close()


def test_candidates_lead_with_the_gateway_and_leave_us_out(monkeypatch):
    """The hot-spotting head unit IS the gateway, so it must be tried first."""
    monkeypatch.setattr(discover, "default_gateways", lambda: ["192.168.43.1"])
    monkeypatch.setattr(discover, "local_addresses", lambda: ["192.168.43.57"])

    addresses = discover.candidates()
    assert addresses[0] == "192.168.43.1"
    assert "192.168.43.57" not in addresses
    assert "192.168.43.2" in addresses and "192.168.43.254" in addresses
    assert len(addresses) == len(set(addresses))


def test_a_gateway_that_cannot_be_read_costs_nothing(monkeypatch):
    """Every reader is allowed to fail: the gateway is inside the /24 anyway, so
    a sweep that has to find it the slow way still finds it."""
    monkeypatch.setattr(discover, "default_gateways", lambda: [])
    monkeypatch.setattr(discover, "local_addresses", lambda: ["192.168.43.57"])

    addresses = discover.candidates()
    assert "192.168.43.1" in addresses


def test_a_sweep_finds_it_and_reports_as_it_goes(bridge, monkeypatch):
    monkeypatch.setattr(discover, "candidates", lambda: ["127.0.0.1"])
    live = []

    found = discover.sweep(port=bridge.port, timeout=5.0, context=False,
                           on_found=live.append)

    assert [u.host for u in found] == ["127.0.0.1"]
    assert [u.host for u in live] == ["127.0.0.1"]


def test_stopping_a_sweep_does_not_wait_for_every_timeout(monkeypatch):
    """254 addresses at 0.6 s each is two and a half minutes of Cancel doing
    nothing, so the flag is read between probes rather than after them."""
    monkeypatch.setattr(discover, "candidates",
                        lambda: ["10.255.255.%d" % n for n in range(1, 255)])
    tried = []

    def counting_probe(host, port, timeout, context):
        tried.append(host)
        return None

    monkeypatch.setattr(discover, "probe", counting_probe)
    assert discover.sweep(workers=1, should_stop=lambda: True, context=False) == []
    assert tried == []


def test_a_found_unit_becomes_a_head_unit_with_no_token(bridge):
    found = discover.probe("127.0.0.1", bridge.port, timeout=5.0, context=False)
    unit = found.as_unit("Golf")

    assert isinstance(unit, headunits.HeadUnit)
    assert unit.name == "Golf" and unit.host == "127.0.0.1"
    assert unit.cable == "ch34x" and unit.max_speed == 0x87
    assert unit.token == b""                        # discovery never pairs
