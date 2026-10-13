# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Signing ``.ovtmod`` packages (Ed25519).

A package's ``manifest.json`` lists every file with its SHA-256; the
``signature`` beside it is an Ed25519 signature over the manifest's exact
bytes. So one signature covers the whole module, and checking it is cheap.

The base app trusts the public keys in its own data file
``module_keys.json`` (base data, shipped with the app)::

    {"keys": [{"id": "release-2026", "public": "<64 hex chars>"}]}

The private key never enters the repository. The build reads it from the
path in ``OVT_SIGNING_KEY`` (PEM), or creates one at the default location
(:func:`default_key_path`) on first use and adds its public half to
``module_keys.json`` -- keep a backup of that file: modules signed with a lost
key can still be loaded, but no new module can be signed for the same users.

An unsigned or unverifiable module is refused unless the user allowed
unsigned modules (developer setting, or ``OVT_ALLOW_UNSIGNED=1``).
"""

import json
import os
from typing import Dict, Optional, Tuple

ENV_KEY = "OVT_SIGNING_KEY"
ENV_ALLOW_UNSIGNED = "OVT_ALLOW_UNSIGNED"
KEYS_FILE = "module_keys.json"


class SigningUnavailable(RuntimeError):
    """The ``cryptography`` package is not installed."""


def _ed():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
    except ImportError as ex:                       # pragma: no cover
        raise SigningUnavailable(
            "module signatures need the 'cryptography' package") from ex
    return ed25519, serialization


# ------------------------------------------------------------------ keys
def default_key_path() -> str:
    """Where the build keeps the private signing key when ``OVT_SIGNING_KEY``
    is not set: outside every repository, in the user's home."""
    return os.path.join(os.path.expanduser("~"), ".ovt-signing", "module-key.pem")


def generate_key(path: str) -> str:
    """Create a private key at ``path`` (refuses to overwrite); returns the
    public key as hex."""
    ed25519, ser = _ed()
    if os.path.exists(path):
        raise FileExistsError(path)
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                            ser.NoEncryption())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return public_hex(path)


def _load_private(path: str):
    ed25519, ser = _ed()
    with open(path, "rb") as fh:
        return ser.load_pem_private_key(fh.read(), password=None)


def public_hex(path: str) -> str:
    _ed25519, ser = _ed()
    pub = _load_private(path).public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)
    return pub.hex()


def sign(manifest_bytes: bytes, key_path: str) -> bytes:
    return _load_private(key_path).sign(manifest_bytes)


# ------------------------------------------------------------------ trust
def trusted_keys() -> Dict[str, str]:
    """key id -> public key hex, from the base app's ``module_keys.json``."""
    from .. import datafiles
    p = datafiles.path(KEYS_FILE)
    if p is None:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k["id"]: k["public"] for k in raw.get("keys", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def allow_unsigned() -> bool:
    if os.environ.get(ENV_ALLOW_UNSIGNED, "") not in ("", "0"):
        return True
    try:
        from ..appsettings import settings
        return bool(settings().value("modules/allow_unsigned", False, type=bool))
    except Exception:
        return False


def verify(manifest_bytes: bytes, signature: Optional[bytes],
           keys: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """``(ok, who)``: whether ``signature`` is a valid signature of
    ``manifest_bytes`` by a trusted key, and which key (or why not)."""
    if not signature:
        return False, "unsigned"
    try:
        ed25519, _ser = _ed()
        from cryptography.exceptions import InvalidSignature
    except SigningUnavailable as ex:
        return False, str(ex)
    for kid, pub in (keys if keys is not None else trusted_keys()).items():
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(
                signature, manifest_bytes)
            return True, kid
        except (InvalidSignature, ValueError):
            continue
    return False, "not signed by a trusted key"
