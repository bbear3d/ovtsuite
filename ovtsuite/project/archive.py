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
import zlib
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


def read(data: bytes, cache: Optional["PackCache"] = None
         ) -> Tuple[dict, Dict[str, bytes], LoadReport]:
    """``(manifest, blobs, report)`` from the bytes of an ``.ovt`` file.

    Every entry other than the reserved three lands in ``blobs`` -- including
    entries this build does not understand, which are carried through saves.
    ``cache`` (a dict) is filled with each sound entry's stored bytes, so
    the next :func:`write` copies them instead of compressing again.
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
            except (zipfile.BadZipFile, OSError, ValueError,
                    zlib.error) as ex:
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

    shas: Dict[str, str] = {}
    for path, ent in (manifest.get("entries") or {}).items():
        if path in report.damaged:
            continue
        got = blobs.get(path)
        if got is None:
            report.missing.append(path)
        elif isinstance(ent, dict) and ent.get("sha256"):
            sha = sha256(got)
            if ent["sha256"] != sha:
                report.damaged.append(path)
            else:
                shas[path] = sha
    if cache is not None:
        _seed(cache, data, zf, blobs, shas, set(report.damaged))
    return manifest, blobs, report


# ------------------------------------------------------------- pack cache
class Packed:
    """One entry as stored in the zip: the blob it was made from (to tell
    whether the blob is still the same), its sha256, CRC and stored bytes."""

    __slots__ = ("blob", "sha", "method", "crc", "data")

    def __init__(self, blob, sha, method, crc, data):
        self.blob = blob
        self.sha = sha
        self.method = method
        self.crc = crc
        self.data = data

    def is_for(self, blob: bytes) -> bool:
        return self.blob is blob or (len(self.blob) == len(blob)
                                     and self.blob == blob)


#: path -> :class:`Packed`, kept with a project between saves: an entry whose
#: blob did not change is copied, not hashed and compressed again
PackCache = Dict[str, Packed]

#: blobs bigger than this are first tried on a sample: data that does not
#: compress (an already-compressed attachment, encrypted firmware) is stored
_SAMPLE = 64 * 1024
#: deflate level; a big blob (a log) takes the fast level -- it is
#: compressed once, then copied by every later save (:data:`PackCache`)
_LEVEL, _LEVEL_BIG, _BIG = 6, 1, 1024 * 1024


def _compressible(raw: bytes) -> bool:
    """Whether deflating ``raw`` is worth it, judged on a few slices of it."""
    if len(raw) <= _SAMPLE:
        return True
    n, k = len(raw), _SAMPLE // 4
    sample = b"".join(raw[i * (n - k) // 3:i * (n - k) // 3 + k]
                      for i in range(4))
    return len(zlib.compress(sample, 1)) < len(sample) * 0.95


def _seed(cache, data, zf, blobs, shas, damaged) -> None:
    """Fill ``cache`` with the entries of the zip just read, as stored."""
    view = memoryview(data)
    for info in zf.infolist():
        path = info.filename
        blob = blobs.get(path)
        if blob is None or path in damaged or info.flag_bits & 0x1 or \
                info.compress_type not in (zipfile.ZIP_STORED,
                                           zipfile.ZIP_DEFLATED):
            continue
        try:
            off = info.header_offset
            if bytes(view[off:off + 4]) != b"PK\x03\x04":
                continue
            n, x = struct.unpack_from("<HH", data, off + 26)
            start = off + 30 + n + x
            stored = bytes(view[start:start + info.compress_size])
        except (struct.error, ValueError):
            continue
        if len(stored) != info.compress_size:
            continue
        cache[path] = Packed(blob, shas.get(path) or sha256(blob),
                             info.compress_type, info.CRC, stored)


def pack(path: str, blob: bytes, cache: Optional[PackCache] = None) -> Packed:
    """``blob`` as stored in the zip -- from ``cache`` when it holds this
    very blob, else hashed and compressed now (and remembered)."""
    got = cache.get(path) if cache is not None else None
    if got is not None and got.is_for(blob):
        return got
    raw = bytes(blob)
    method, stored = zipfile.ZIP_STORED, raw
    if len(raw) >= 64 and _compressible(raw):
        co = zlib.compressobj(_LEVEL_BIG if len(raw) > _BIG else _LEVEL,
                              zlib.DEFLATED, -15)
        comp = co.compress(raw) + co.flush()
        if len(comp) < len(raw):
            method, stored = zipfile.ZIP_DEFLATED, comp
    # (a mutable blob is remembered as the copy: it may change in place)
    out = Packed(blob if type(blob) is bytes else raw, sha256(raw), method,
                 zlib.crc32(raw) & 0xFFFFFFFF, stored)
    if cache is not None:
        cache[path] = out
    return out


_DOS_DATE = ((2026 - 1980) << 9) | (1 << 5) | 1
_DOS_TIME = 0


def _zip(entries) -> Optional[bytes]:
    """A zip of ``entries`` (``(name, Packed)`` in order) laid out as
    :mod:`zipfile` lays it out, or None when it would need ZIP64."""
    chunks: List[bytes] = []
    central: List[bytes] = []
    off = 0
    for name, e in entries:
        fn = name.encode("utf-8")
        flags = 0x800 if not name.isascii() else 0
        size = len(e.blob)
        if max(size, len(e.data), off) >= 0xFFFFFFFF:
            return None
        head = struct.pack("<4s2B4HL2L2H", b"PK\x03\x04", 20, 0, flags,
                           e.method, _DOS_TIME, _DOS_DATE, e.crc,
                           len(e.data), size, len(fn), 0)
        central.append(struct.pack(
            "<4s4B4HL2L5H2L", b"PK\x01\x02", 20, 3, 20, 0, flags, e.method,
            _DOS_TIME, _DOS_DATE, e.crc, len(e.data), size, len(fn), 0, 0,
            0, 0, 0o600 << 16, off) + fn)
        chunks += [head, fn, e.data]
        off += len(head) + len(fn) + len(e.data)
    cd = b"".join(central)
    if len(entries) >= 0xFFFF or off + len(cd) >= 0xFFFFFFFF:
        return None
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, len(entries),
                      len(entries), len(cd), off, 0)
    return b"".join(chunks) + cd + end


# ------------------------------------------------------------------ writing
def write(manifest: dict, blobs: Dict[str, bytes], *,
          sign_key: Optional[str] = None, signer: str = "",
          cache: Optional[PackCache] = None) -> bytes:
    """The bytes of an ``.ovt`` file. ``manifest["entries"]`` is (re)computed
    here from ``blobs``; ``sign_key`` (an Ed25519 PEM path) signs it.

    ``cache`` (kept by the caller between saves, see :data:`PackCache`)
    spares hashing and compressing the blobs that did not change. Blobs that
    do not compress are stored, the rest deflated; any zip reader -- older
    builds' included -- reads either."""
    packed = {p: pack(p, b, cache) for p, b in sorted(blobs.items())}
    if cache is not None:
        for p in [p for p in cache if p not in blobs]:
            del cache[p]                # a blob that is gone
    manifest = dict(manifest)
    manifest["entries"] = {p: {"sha256": e.sha, "size": len(e.blob)}
                           for p, e in packed.items()}
    raw_manifest = json.dumps(manifest, indent=1, sort_keys=False).encode("utf-8")
    integrity = {"manifest_sha256": sha256(raw_manifest),
                 "signature": _sign(raw_manifest, sign_key, signer)
                 if sign_key else None}
    raw_integrity = json.dumps(integrity, indent=1).encode("utf-8")
    entries = [("mimetype", Packed(MIMETYPE, "", zipfile.ZIP_STORED,
                                   zlib.crc32(MIMETYPE), MIMETYPE)),
               (MANIFEST, pack(MANIFEST, raw_manifest)),
               (INTEGRITY, pack(INTEGRITY, raw_integrity))]
    entries += [(p, e) for p, e in packed.items() if p not in _RESERVED]
    out = _zip(entries)
    if out is not None:
        return out
    # too big for a plain zip: zipfile writes the ZIP64 records
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=True) as zf:
        mt = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
        mt.compress_type = zipfile.ZIP_STORED
        zf.writestr(mt, MIMETYPE)
        zf.writestr(MANIFEST, raw_manifest, compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=6)
        zf.writestr(INTEGRITY, raw_integrity,
                    compress_type=zipfile.ZIP_DEFLATED)
        for path, e in packed.items():
            if path in _RESERVED:
                continue
            zf.writestr(path, e.blob, compress_type=e.method, compresslevel=6)
    return buf.getvalue()


def atomic_write(path: str, data: bytes) -> None:
    """Temp file beside ``path`` + ``os.replace`` (never writes in place)."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)              # a full disk leaves no stray temp file
        except OSError:
            pass
        raise
