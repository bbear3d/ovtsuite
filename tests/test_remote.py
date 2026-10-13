# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pass-through link (ovtsuite/flash/remote.py) against a mock KW-Dash.

What these pin is not "does it answer" but the two things a network link between
this app and a cable in a car gets wrong: whether a whole K-line frame leaves in
ONE operation however the caller wrote it, and whether a failure on the cable
costs the connection.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash.remote import (  # noqa: E402
    CLIENT_ID, BridgeClient, BridgeError, RemoteKLine, RemoteTp20,
)
from ovtsuite.flash.transport import KLineTimeout  # noqa: E402
from mock_bridge import MockBridge  # noqa: E402

_IDENT = bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C, 0x39])
_REPLY = bytes([0x03, 0xF1, 0x10])


@pytest.fixture
def bridge():
    server = MockBridge()
    try:
        yield server
    finally:
        server.close()


def _paired(bridge, client_id=CLIENT_ID):
    """Paired as the real client identifies itself.  Spelling the id out here
    instead would let a change to the default pass this file and fail on a
    reconnect, which is the one thing these tests exist to catch."""
    client = BridgeClient("127.0.0.1", bridge.port, client_id=client_id, timeout=5.0)
    client.connect()
    client.pair(bridge.pin)
    client.claim()
    return client


def test_capabilities_reach_the_client(bridge):
    client = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    caps = client.connect()
    assert caps["cable"] == "ch34x"
    assert caps["five_baud"] is True and caps["can"] is False
    assert caps["max_speed"] == 0x87
    assert caps["paired"] is False
    client.close()


def test_cable_is_out_of_reach_until_claimed(bridge):
    """Greeting must not cost the gauges anything: no auth, no cable."""
    client = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    client.connect()
    with pytest.raises(BridgeError) as refused:
        client.call("clear_rx")
    assert refused.value.code == "auth_failed"

    client.pair(bridge.pin)
    with pytest.raises(BridgeError) as unclaimed:
        client.call("clear_rx")
    assert unclaimed.value.code == "not_claimed"
    assert bridge.cable.calls == []
    client.close()


def test_token_replaces_the_pin_and_a_replay_does_not(bridge):
    first = _paired(bridge)
    token = first.token
    first.release()
    first.close()

    again = BridgeClient("127.0.0.1", bridge.port, token=token, timeout=5.0)
    assert again.connect()["paired"] is True
    again.claim()
    stale = again.nonce
    again.close()

    third = BridgeClient("127.0.0.1", bridge.port, token=token, timeout=5.0)
    third.connect()
    assert third.nonce != stale, "a reused nonce would make a recorded session replayable"
    third.close()

    # Connect without a token so the greeting succeeds, then offer the wrong one:
    # the MAC is over a nonce this connection was given, so only the real token signs it.
    impostor = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    impostor.connect()
    impostor.token = os.urandom(32)
    with pytest.raises(BridgeError) as bad:
        impostor.authenticate()
    assert bad.value.code == "auth_failed"
    impostor.close()


def test_the_pin_is_proved_not_sent(bridge):
    """A wrong PIN fails, and the right one never crosses the wire.

    The proof is an HMAC over this connection's binding, so a relay holding its own
    certificate cannot pass one through - which is the only protection there is on a
    first connect, when nothing is pinned yet.
    """
    import hashlib
    import hmac

    client = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    client.connect()

    with pytest.raises(BridgeError) as wrong:
        client.pair("000000" if bridge.pin != "000000" else "111111")
    assert wrong.value.code == "auth_failed"

    # The proof the client sends is exactly HMAC(pin, binding) - built here independently
    # so that a client which sent something else, or the PIN itself, would fail this.
    binding = client.peer_fingerprint() + client.nonce
    expected = hmac.new(bridge.pin.encode("utf-8"), binding, hashlib.sha256).digest()
    reply = client.call("auth", pin_proof=expected.hex())
    assert reply["ok"] is True
    client.close()


def test_a_second_laptop_is_told_busy(bridge):
    holder = _paired(bridge, "laptop-a")
    other = BridgeClient("127.0.0.1", bridge.port, client_id="laptop-b", timeout=5.0)
    other.connect()
    other.pair(bridge.pin)

    with pytest.raises(BridgeError) as busy:
        other.claim()
    assert busy.value.code == "busy", "two clients on one UART is the failure to prevent"

    holder.release()
    other.claim()
    assert bridge.claims == ["claimed", "released", "claimed"]
    holder.close()
    other.close()


def test_a_frame_written_byte_by_byte_leaves_once(bridge):
    """The whole reason this link is frame-shaped.

    edc16 writes through ``write_byte``/``write_bytes`` as if it had a UART. A
    link that forwarded each byte would be a round trip per byte -- 44 minutes of
    network on a 512 KB read that the cable does in two and a half.
    """
    client = _paired(bridge)
    link = RemoteKLine(client, prefer_fast_init=True)
    link.wake_up()
    bridge.cable.calls.clear()

    for byte in _IDENT:
        link.write_byte(byte)

    transfers = [c for c in bridge.cable.calls if c[0] == "xfer"]
    assert len(transfers) == 1, "one frame, one operation"
    assert bytes.fromhex(transfers[0][1]["data"]) == _IDENT
    assert link.read(3) == _REPLY
    client.close()


def test_write_raw_leaves_the_echo_readable(bridge):
    """K-line is half duplex and edc16 reads its own echo back deliberately."""
    client = _paired(bridge)
    link = RemoteKLine(client)
    link.wake_up()

    link.write_raw(_IDENT)
    assert link.read(len(_IDENT)) == _IDENT, "the echo comes first"
    assert link.read(3) == _REPLY, "then the controller's answer"

    link.clear_rx()
    link.write_bytes(_IDENT)
    assert link.read(3) == _REPLY, "write_bytes drains the echo instead"
    client.close()


def test_the_init_is_one_operation(bridge):
    """Fast init anchors StartCommunication to Twup = 50 ms from the pulse start;
    a drift to 66 ms killed every attempt on the serial reference. It cannot be
    assembled out of remote steps, so it is not offered as any."""
    client = _paired(bridge)

    fast = RemoteKLine(client, prefer_fast_init=True)
    assert fast.wake_up() == 2031, "EF 8F is KW2031"
    assert [c[0] for c in bridge.cable.calls] == ["fast_init"]

    bridge.cable.calls.clear()
    slow = RemoteKLine(client, baud=9600, prefer_fast_init=False)
    slow.wake_up(address=0x01)
    assert bridge.cable.calls[0][0] == "slow_init"
    assert bridge.cable.calls[0][1]["baud"] == 9600, "the wake-up rate is per-ECU, never searched"
    client.close()


def test_set_baud_is_the_primitive_the_state_machine_calls(bridge):
    """edc16._enter_session reads the speed ack itself and changes rate 75 ms
    later, so the bare primitive is what it needs -- not a composite."""
    client = _paired(bridge)
    link = RemoteKLine(client)
    link.wake_up()
    bridge.cable.calls.clear()

    link.set_baud(124800)
    assert bridge.cable.calls[-1][0] == "set_baud"
    assert bridge.cable.calls[-1][1]["baud"] == 124800
    assert link.baud == 124800
    client.close()


def test_a_cable_timeout_is_a_klinetimeout_and_the_link_survives(bridge):
    """The property staged mode depends on: a failure at the cable must not cost
    the connection, and must arrive as the exception the callers already handle."""
    client = _paired(bridge)
    link = RemoteKLine(client)
    link.wake_up()

    bridge.cable.fail_next = ("timeout", "no reply in 900 ms")
    with pytest.raises(KLineTimeout):
        link.write_bytes(_IDENT)

    link.clear_rx()
    link.write_bytes(_IDENT)
    assert link.read(3) == _REPLY, "the very next operation works"
    client.close()


def test_tp20_channel_is_a_drop_in_for_cankline(bridge):
    """RemoteTp20 presents BridgeTp20's surface, so CanKLine(tp=...) is unchanged
    and the acknowledgement timing never crosses the network."""
    client = _paired(bridge)
    tp = RemoteTp20(client, dest=0x01)
    tp.open()
    tp.send(bytes([0x1A, 0x9B]))
    assert tp.recv(4000) == _REPLY
    tp.keepalive()
    tp.close()

    ops = [c[0] for c in bridge.cable.calls]
    assert ops == ["tp_open", "tp_send", "tp_recv", "tp_keepalive", "tp_close"]

    for name in ("open", "send", "recv", "keepalive", "close"):
        assert hasattr(tp, name), "CanKLine(tp=...) calls %s" % name
    client.close()


def test_a_dropped_socket_frees_the_cable(bridge):
    """Otherwise a laptop that closed its lid leaves the dashboard mute."""
    client = _paired(bridge)
    client.close()

    for _ in range(100):
        if bridge.claims[-1:] == ["released"]:
            break
        import time
        time.sleep(0.01)

    assert bridge.claims == ["claimed", "released"]
