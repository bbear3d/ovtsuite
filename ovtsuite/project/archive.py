# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.ovt`` archive: ZIP layout, structural identification, integrity and
optional Ed25519 signing (spec sections 2 and 5)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MIMETYPE = b"application/x-ovt-project"
EXT = ".ovt"
MANIFEST = "manifest.json"
INTEGRITY = "integrity.json"
_RESERVED = ("mimetype", MANIFEST, INTEGRITY)

#: Critical features this reader implements (spec section 6).
FEATURES = frozenset({"sets-v1", "delta-v1"})


class ProjectError(ValueError):
    """Not an .ovt project, or one this build cannot read."""


class UnsupportedFeature(ProjectError):
    """The project needs a critical feature this build does not implement."""

    def __init__(self, missing):
        self.missing = sorted(missing)
        super().__init__(
            "this project needs a newer %s (unsupported feature%s: %s)"
            % (_app_name(), "s" if len(self.missing) > 1 else "",
               ", ".join(self.missing)))


def _app_name() -> str:
    from .. import branding
    return branding.APP_NAME


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------ identification
def is_project(path_or_bytes) -> bool:
    """True when the file opens with a stored ``mimetype`` entry holding
    :data:`MIMETYPE` -- a fixed-offset structural test (spec 2.1)."""
    try:
        if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
            head = bytes(path_or_bytes[:128])
        else:
            with open(path_or_bytes, "rb") as fh:
                head = fh.read(128)
    except OSError:
        return False
    if len(head) < 30 or head[:4] != b"PK\x03\x04":
        return False
    method, = struct.unpack_from("<H", head, 8)
    name_len, extra_len = struct.unpack_from("<HH", head, 26)
    if method != 0 or name_len != 8 or head[30:38] != b"mimetype":
        return False
    p = 38 + extra_len
    return head[p:p + len(MIMETYPE)] == MIMETYPE


# --------------------------------------------------------------- signatures
@dataclass
class Signature:
    status: str = "unsigned"        # "unsigned" | "valid" | "invalid" | "unavailable"
    key: str = ""                   # public key hex
    signer: str = ""

    @property
    def fingerprint(self) -> str:
        return sha256(bytes.fromhex(self.key))[:16] if self.key else ""

    def describe(self) -> str:
        if self.status == "unsigned":
            return "Not signed"
        if self.status == "valid":
            who = self.signer or "unknown signer"
            return "Signed by %s (key %s)" % (who, self.fingerprint)
        if self.status == "unavailable":
            return "Signed, but signatures can't be checked on this install"
        return "Signature INVALID -- the project was changed after signing"


def _sign(manifest: bytes, key_path: str, signer: str) -> dict:
    from ..modules import signing
    return {"alg": "ed25519", "key": signing.public_hex(key_path),
            "signer": signer, "sig": signing.sign(manifest, key_path).hex()}


def _verify(manifest: bytes, sig: Optional[dict]) -> Signature:
    if not sig:
        return Signature()
    key = str(sig.get("key", ""))
    out = Signature(status="invalid", key=key, signer=str(sig.get("signer", "")))
    if sig.get("alg") != "ed25519":
        return out
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        out.status = "unavailable"
        return out
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(str(sig.get("sig", ""))), manifest)
        out.status = "valid"
    except (InvalidSignature, ValueError):
        pass
    return out


# ------------------------------------------------------------------ reading
@dataclass
class LoadReport:
    """What loading found besides the content itself."""
    damaged: List[str] = field(default_factory=list)   # entries failing their hash
    missing: List[str] = field(default_factory=list)   # listed, not in the archive
    warnings: List[str] = field(default_factory=list)
    signature: Signature = field(default_factory=Signature)
    manifest_ok: bool = True

    @property
    def ok(self) -> bool:
        return (self.manifest_ok and not self.damaged and not self.missing
                and self.signature.status != "invalid")


def read(data: bytes) -> Tuple[dict, Dict[str, bytes], LoadReport]:
    """``(manifest, blobs, report)`` from the bytes of an ``.ovt`` file.

    Every entry other than the reserved three lands in ``blobs`` -- including
    entries this build does not understand, which are carried through saves.
    """
    if not is_project(data):
        raise ProjectError("not an OVT project file")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as ex:
        raise ProjectError("OVT project is corrupt: %s" % ex) from ex
    report = LoadReport()
    blobs: Dict[str, bytes] = {}
    raw_manifest = None
    integrity = {}
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                payload = zf.read(info)
            except (zipfile.BadZipFile, OSError, ValueError) as ex:
                # zipfile checks each entry's CRC: a damaged entry is
                # reported, the rest of the project still opens.
                if info.filename in (MANIFEST,):
                    raise ProjectError("OVT project manifest is damaged: %s"
                                       % ex) from ex
                report.damaged.append(info.filename)
                continue
            if info.filename == MANIFEST:
                raw_manifest = payload
            elif info.filename == INTEGRITY:
                try:
                    integrity = json.loads(payload.decode("utf-8"))
                except ValueError:
                    report.warnings.append("integrity record unreadable")
            elif info.filename != "mimetype":
                blobs[info.filename] = payload
    if raw_manifest is None:
        raise ProjectError("OVT project has no manifest")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except ValueError as ex:
        raise ProjectError("OVT project manifest is unreadable: %s" % ex) from ex
    if manifest.get("format") != "ovt-project":
        raise ProjectError("not an OVT project manifest")
    missing = set(manifest.get("requires") or []) - FEATURES
    if missing:
        raise UnsupportedFeature(missing)

    want = integrity.get("manifest_sha256") if isinstance(integrity, dict) else None
    if want and want != sha256(raw_manifest):
        report.manifest_ok = False
        report.warnings.append("manifest does not match its integrity record")
    report.signature = _verify(raw_manifest, (integrity or {}).get("signature"))

    for path, ent in (manifest.get("entries") or {}).items():
        if path in report.damaged:
            continue
        got = blobs.get(path)
        if got is None:
            report.missing.append(path)
        elif isinstance(ent, dict) and ent.get("sha256") and \
                ent["sha256"] != sha256(got):
            report.damaged.append(path)
    return manifest, blobs, report


# ------------------------------------------------------------------ writing
def write(manifest: dict, blobs: Dict[str, bytes], *,
          sign_key: Optional[str] = None, signer: str = "") -> bytes:
    """The bytes of an ``.ovt`` file. ``manifest["entries"]`` is (re)computed
    here from ``blobs``; ``sign_key`` (an Ed25519 PEM path) signs it."""
    manifest = dict(manifest)
    manifest["entries"] = {p: {"sha256": sha256(b), "size": len(b)}
                           for p, b in sorted(blobs.items())}
    raw_manifest = json.dumps(manifest, indent=1, sort_keys=False).encode("utf-8")
    integrity = {"manifest_sha256": sha256(raw_manifest),
                 "signature": _sign(raw_manifest, sign_key, signer)
                 if sign_key else None}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        mt = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
        mt.compress_type = zipfile.ZIP_STORED
        zf.writestr(mt, MIMETYPE)
        zf.writestr(MANIFEST, raw_manifest, compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=6)
        zf.writestr(INTEGRITY, json.dumps(integrity, indent=1).encode("utf-8"),
                    compress_type=zipfile.ZIP_DEFLATED)
        for path in sorted(blobs):
            if path in _RESERVED:
                continue
            zf.writestr(path, blobs[path], compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=6)
    return buf.getvalue()


def atomic_write(path: str, data: bytes) -> None:
    """Temp file beside ``path`` + ``os.replace`` (never writes in place)."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
