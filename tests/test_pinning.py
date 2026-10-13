# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What a self-signed certificate is worth, and how it is made worth something.

The head unit signs its own certificate, so TLS alone proves nothing: anything on
the same Wi-Fi can offer one. What makes the link mean something is that pairing
proved ONE certificate with the PIN, and everything afterwards insists on seeing
that same one. These tests pin the three places that has to hold.
"""
import os
import ssl
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import headunits  # noqa: E402
from ovtsuite.flash.remote import BridgeClient  # noqa: E402
from mock_bridge import MockBridge  # noqa: E402


@pytest.fixture
def bridge():
    server = MockBridge()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def store(tmp_path):
    return str(tmp_path)


def _pretend_certificate(monkeypatch, fingerprint, bridge=None):
    """Make every client see this certificate, and optionally the head unit offer
    it. The fingerprint comes from the TLS handshake, which the plaintext mock has
    none of; what is under test is what both ends DO with one, not how it is
    computed."""
    monkeypatch.setattr(BridgeClient, "peer_fingerprint", lambda self: fingerprint)
    if bridge is not None:
        bridge.fingerprint = fingerprint


def test_pairing_keeps_the_certificate_it_proved(bridge, store, monkeypatch):
    _pretend_certificate(monkeypatch, b"\x01" * 32, bridge)

    unit, client = headunits.pair("127.0.0.1", bridge.port, bridge.pin,
                                  "Golf", context=False, directory=store)
    client.close()

    assert unit.fingerprint == b"\x01" * 32
    assert headunits.load(store)[0].fingerprint == b"\x01" * 32


def test_a_different_certificate_is_refused_before_the_token_is_used(
        bridge, store, monkeypatch):
    """The order matters as much as the refusal: if the token were proved first,
    a machine in the middle would already have a MAC to relay."""
    _pretend_certificate(monkeypatch, b"\x01" * 32, bridge)
    unit, client = headunits.pair("127.0.0.1", bridge.port, bridge.pin,
                                  "Golf", context=False, directory=store)
    client.close()

    sent = []
    original = BridgeClient.call

    def watching(self, op, blob=b"", **fields):
        sent.append(op)
        return original(self, op, blob, **fields)

    monkeypatch.setattr(BridgeClient, "call", watching)
    monkeypatch.setattr(BridgeClient, "peer_fingerprint",
                        lambda self: b"\x02" * 32)   # somebody else's

    with pytest.raises(headunits.FingerprintMismatch):
        headunits.connect(unit, timeout=5.0, context=False, directory=store)

    assert "auth" not in sent, "no MAC may leave this machine"


def test_the_same_certificate_connects(bridge, store, monkeypatch):
    _pretend_certificate(monkeypatch, b"\x01" * 32, bridge)
    unit, client = headunits.pair("127.0.0.1", bridge.port, bridge.pin,
                                  "Golf", context=False, directory=store)
    client.close()

    again = headunits.connect(unit, timeout=5.0, context=False, directory=store)
    again.claim()
    again.release()
    again.close()


def test_a_pairing_made_before_pinning_learns_its_certificate_once(
        bridge, store, monkeypatch):
    """Trust on first use, for a file written by an older build. Refusing those
    would make an upgrade look like an attack."""
    _pretend_certificate(monkeypatch, b"", bridge)
    unit, client = headunits.pair("127.0.0.1", bridge.port, bridge.pin,
                                  "Golf", context=False, directory=store)
    client.close()
    assert unit.fingerprint == b""

    _pretend_certificate(monkeypatch, b"\x03" * 32, bridge)
    headunits.connect(unit, timeout=5.0, context=False, directory=store).close()

    assert unit.fingerprint == b"\x03" * 32
    assert headunits.load(store)[0].fingerprint == b"\x03" * 32


def test_the_token_proof_carries_the_certificate(bridge, monkeypatch):
    """The hole this closes: a MAC over the nonce alone says nothing about which
    certificate the client is looking at, so a relay can take one and replay it.
    The mock expects the nonce alone -- a client that saw a certificate must
    therefore FAIL against it, which is the proof that the binding really changed.
    """
    from ovtsuite.flash.remote import BridgeError

    _pretend_certificate(monkeypatch, b"")
    client = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    client.connect()
    token = client.pair(bridge.pin)
    client.close()

    _pretend_certificate(monkeypatch, b"\x04" * 32)
    relayed = BridgeClient("127.0.0.1", bridge.port, token=token, timeout=5.0)
    with pytest.raises(BridgeError):
        relayed.connect()
    relayed.close()


def test_the_context_does_not_pretend_to_verify():
    """Off on purpose, and only because the fingerprint check above exists. If
    this ever reads CERT_REQUIRED, a self-signed certificate doing its job would
    be rejected -- and someone would 'fix' it by removing the pinning instead."""
    context = headunits.ssl_context()
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_bluetooth_and_a_harness_ask_for_no_certificate():
    assert headunits._context(False) is None
    assert isinstance(headunits._context(None), ssl.SSLContext)
