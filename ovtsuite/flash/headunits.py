# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Head units this laptop has paired with, and how to reach them.

A pass-through head unit is one more entry in the same picker as a serial cable
or a J2534 device -- the user picks it the same way and the rest of the app does
not care.  What it needs that the others do not is somewhere to keep the PAIRING:
a PIN is typed once, on the dashboard, and the token it buys is what every later
connect proves knowledge of.

STORED AS A FILE, not in QSettings, on purpose.  The rest of the flash dialog's
state is remembered through QSettings and this deliberately is not: a token is
bytes, and QSettings' per-platform type coercion is exactly the thing that has
bitten this app before (see ``gui_flash._as_bool``).  A token that came back as
the string "b'\\x12...'" would fail authentication with a message about the PIN.
"""

import json
import os
import ssl

from .. import branding


#: Where a head unit listens unless it says otherwise.
DEFAULT_PORT = 35510


class HeadUnit(object):
    """One paired head unit, and what it last told us about its cable."""

    def __init__(self, name, host, port=DEFAULT_PORT, token=b"", cable="",
                 can=False, five_baud=True, max_speed=0x50, fingerprint=b""):
        self.name = name or host
        self.host = host
        self.port = int(port)
        self.token = bytes(token or b"")
        self.cable = cable
        self.can = bool(can)
        self.five_baud = bool(five_baud)
        self.max_speed = int(max_speed)
        #: SHA-256 of the head unit's certificate, learned at pairing.  What makes
        #: a self-signed certificate mean anything: the PIN proved this exact one,
        #: so every later connect can insist on seeing it again.
        self.fingerprint = bytes(fingerprint or b"")

    @property
    def label(self):
        where = "%s:%d" % (self.host, self.port)
        if self.cable:
            return "%s — %s (%s, pass-through)" % (self.name, where, self.cable)
        return "%s — %s (pass-through)" % (self.name, where)

    def to_json(self):
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "token": self.token.hex(),
            "cable": self.cable,
            "can": self.can,
            "five_baud": self.five_baud,
            "max_speed": self.max_speed,
            "fingerprint": self.fingerprint.hex(),
        }

    @staticmethod
    def from_json(entry):
        return HeadUnit(
            entry.get("name", ""),
            entry.get("host", ""),
            entry.get("port", DEFAULT_PORT),
            bytes.fromhex(entry.get("token", "") or ""),
            entry.get("cable", ""),
            entry.get("can", False),
            entry.get("five_baud", True),
            entry.get("max_speed", 0x50),
            # Absent in a file written before certificates were pinned.  Empty
            # means "not known yet", which `connect` learns and stores rather
            # than refusing -- a pairing that predates the pin is still a pairing.
            bytes.fromhex(entry.get("fingerprint", "") or ""),
        )

    def __repr__(self):
        return "<HeadUnit %s>" % self.label


def config_dir():
    """Where this app keeps per-user state, per platform.

    Deliberately the same shape as ``recent._config_dir`` -- product name on
    Windows, package name elsewhere, both read from ``branding`` so a rename
    moves them together.  Pairings live beside the recent-files lists rather
    than in a second location of their own.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, branding.CONFIG_DIR)
    base = (os.environ.get("XDG_CONFIG_HOME")
            or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, branding.PKG)


def _path(directory=None):
    return os.path.join(directory or config_dir(), "headunits.json")


def load(directory=None):
    """Every paired head unit.  A damaged entry costs that one, never the file."""
    try:
        with open(_path(directory), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []

    out = []
    for entry in raw.get("head_units", []):
        try:
            unit = HeadUnit.from_json(entry)
        except (ValueError, AttributeError):
            continue                                    # one bad entry, not the list
        if unit.host:
            out.append(unit)
    return out


def save(units, directory=None):
    """Write the list.  Whole and then moved into place, so a crash costs the newest."""
    directory = directory or config_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        target = _path(directory)
        temporary = target + ".new"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"head_units": [u.to_json() for u in units]}, handle, indent=2)
        os.replace(temporary, target)
    except OSError:
        pass                                            # unsaved is worse than nothing, not fatal


def remember(unit, directory=None):
    """Add or replace one, matched on host and port."""
    units = [u for u in load(directory)
             if not (u.host == unit.host and u.port == unit.port)]
    units.append(unit)
    save(units, directory)
    return units


def forget(host, port=DEFAULT_PORT, directory=None):
    """Drop one.  The head unit keeps its own copy until it is forgotten there too."""
    units = [u for u in load(directory) if not (u.host == host and u.port == int(port))]
    save(units, directory)
    return units


class FingerprintMismatch(Exception):
    """This head unit answered with a certificate that is not the paired one."""


def ssl_context():
    """The context every connection to a head unit uses.

    VERIFICATION IS OFF HERE AND PINNED ONE LAYER UP, which is not the same as
    absent.  The certificate is generated on the head unit and signed by nobody,
    so there is no chain to walk and no hostname in it to match -- an address on a
    car's Wi-Fi is not a name anyone can certify.  What makes the link mean
    something is the FINGERPRINT: the PIN proved that exact certificate at
    pairing, ``connect`` refuses any other one afterwards, and the token proof is
    bound to it on every connection.  Turning ``CERT_REQUIRED`` on here would only
    fail a self-signed certificate that is doing its job.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _context(context):
    """What to wrap the socket in: a caller's context, the default one, or nothing.

    ``None`` -- the ordinary case -- means TLS with :func:`ssl_context`.  ``False``
    means a link that carries no certificate: Bluetooth, where the transport is
    already encrypted and authenticated by BT pairing before a byte of this
    protocol reaches it, and where offering a certificate would be answering a
    question already answered.  A test harness is the same shape as Bluetooth and
    uses the same value, deliberately: nothing here exists only for the tests.
    """
    if context is False:
        return None
    return context or ssl_context()


def connect(unit, timeout=30.0, context=None, directory=None):
    """A connected, authenticated client for this head unit.

    Also refreshes what we know about its cable: the greeting reports the chip,
    the speed cap and whether it can do a 5-baud init, so the picker can grey out
    High -- and a K-line WRITE -- for the cable that is actually at the far end
    rather than for a guess made here.

    Raises :class:`FingerprintMismatch` if the certificate is not the one this
    pairing was made against.  That check happens BEFORE the token is used, so a
    machine in the middle never sees a MAC it could relay.
    """
    from .remote import BridgeClient

    # No token yet: greet first, look at the certificate, and only then
    # authenticate.  BridgeClient.connect() would do both in one step.
    client = BridgeClient(unit.host, unit.port, timeout=timeout,
                          ssl_context=_context(context))
    caps = client.connect()

    seen = client.peer_fingerprint()
    if unit.fingerprint and seen and seen != unit.fingerprint:
        client.close()
        raise FingerprintMismatch(
            "This is not the head unit you paired with: it offered a different "
            "certificate. Either it was reinstalled -- pair again -- or something "
            "on the network is pretending to be it.")

    if unit.token:
        client.token = unit.token
        client.authenticate()

    # A pairing made before certificates were pinned learns its fingerprint on the
    # first connect that sees one.  Trust-on-first-use, and only ever once.
    if seen and not unit.fingerprint:
        unit.fingerprint = seen
        remember(unit, directory)

    unit.cable = caps.get("cable", unit.cable)
    unit.can = bool(caps.get("can", unit.can))
    unit.five_baud = bool(caps.get("five_baud", unit.five_baud))
    unit.max_speed = int(caps.get("max_speed", unit.max_speed))
    return client


def pair(host, port=DEFAULT_PORT, pin="", name="", timeout=30.0, context=None,
         directory=None):
    """First connect: exchange the PIN on the dashboard for a token, and keep it.

    The certificate seen here is kept with the token.  This is the one moment it
    can be learned safely, because the PIN signed a challenge over it: a relay
    holding its own certificate produces a proof the head unit rejects, so a
    pairing that succeeds is a pairing with the machine on the dashboard.
    """
    from .remote import BridgeClient

    client = BridgeClient(host, port, timeout=timeout,
                          ssl_context=_context(context))
    caps = client.connect()
    token = client.pair(pin)

    unit = HeadUnit(name or host, host, port, token,
                    caps.get("cable", ""), caps.get("can", False),
                    caps.get("five_baud", True), caps.get("max_speed", 0x50),
                    client.peer_fingerprint())
    remember(unit, directory)
    return unit, client


__all__ = ["HeadUnit", "DEFAULT_PORT", "FingerprintMismatch", "load", "save",
           "remember", "forget", "connect", "pair", "config_dir", "ssl_context"]
