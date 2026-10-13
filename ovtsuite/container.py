# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT native containers: one compressed block-directory format, four flavours.

``.edc16`` (EDC16 ECU), ``.edc15`` (EDC15 ECU), ``.6hp`` (ZF 6HP TCU),
``.hvac`` (Climatronic) and ``.vdo`` (VDO instrument cluster EEPROM) are the
same file layout with a different magic. A
file is a directory of independently compressed, CRC-checked regions, so a
reader can tell at a glance what it holds (main image, MPC internal flash,
EEPROM, change log, ...) and pull out just the part it needs; adding a region
is another directory entry, never a format change.

Layout (all integers little-endian, the container's own byte order -- the ECU
payload bytes inside each block keep their native content untouched):

    magic      8s   per flavour, see FORMATS
    version    H    format version (1)
    count      H    number of block directory entries
    reserved   I    0

    directory: ``count`` fixed 28-byte entries, in the order the blocks are
    stored, each:
        name       8s   short region label, NUL-padded
        type       B    numeric region code (BLOCK_* below)
        comp       B    compression: 0 = stored, 1 = zlib/DEFLATE
        flags      H    reserved (0)
        address    I    logical/VMA base of the region
        orig_size  I    uncompressed byte length
        comp_size  I    stored (compressed) byte length
        crc32      I    CRC-32 of the ORIGINAL (uncompressed) bytes

    then the block payloads, concatenated in directory order. A block's file
    offset is implicit: header + directory, plus the sum of the preceding
    blocks' comp_size.

This layout is byte-for-byte the one ``.edc16`` has always used, so existing
``.edc16`` files read unchanged and files written here read in older builds
(which keep every block they do not understand).

Block types
-----------
``BLOCK_EXTERNAL`` is the platform's MAIN image whatever the platform calls it:
the external flash of an EDC16, the flash of an EDC15, the TCU flash of a 6HP,
the Climatronic image. ``MPC`` (internal flash) and ``EEPROM`` are optional for
every flavour and present whenever the source had them.

``BLOCK_LOG`` is the change log / version history (UTF-8 text, see
``ovtsuite.changelog``).

``BLOCK_META`` is a small JSON object of platform facts that have no other
home (an HVAC image's part number, flash base, section map and baseline
digests).

``BLOCK_LOADER`` is a flash loader that travels with the image (HVAC).

``BLOCK_DEFN`` is the definition snapshot (``ovtsuite.defn``): the resolved
parameter table for the main image, so opening the file needs no
identification and no matching. It is advisory -- see that module.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Dict, Optional

VERSION = 1

_HDR = struct.Struct("<8sHHI")          # magic, version, count, reserved
_ENTRY = struct.Struct("<8sBBHIIII")    # name, type, comp, flags, addr, orig, comp, crc32

# Region type codes. 1-7 are the original .edc16 set and must not change.
BLOCK_EXTERNAL = 1      # the platform's main image (external flash / flash)
BLOCK_BOOT = 2          # boot/low region only
BLOCK_PROGRAM = 3       # program area only
BLOCK_CAL = 4           # calibration only
BLOCK_MPC = 5           # MPC internal flash (VMA 0x400000)
BLOCK_EEPROM = 6        # EEPROM
BLOCK_LOG = 7           # change log / version history (UTF-8 text)
BLOCK_DEFN = 8          # definition snapshot (ovtsuite.defn)
BLOCK_META = 9          # platform metadata, JSON
BLOCK_LOADER = 10       # flash loader that travels with the image

_DEFAULT_NAME = {
    BLOCK_EXTERNAL: "external",
    BLOCK_BOOT: "boot",
    BLOCK_PROGRAM: "program",
    BLOCK_CAL: "cal",
    BLOCK_MPC: "mpc",
    BLOCK_EEPROM: "eeprom",
    BLOCK_LOG: "history",
    BLOCK_DEFN: "defn",
    BLOCK_META: "meta",
    BLOCK_LOADER: "loader",
}
_DEFAULT_ADDR = {
    BLOCK_EXTERNAL: 0x000000,
    BLOCK_BOOT: 0x010000,
    BLOCK_PROGRAM: 0x040000,
    BLOCK_CAL: 0x180000,
    BLOCK_MPC: 0x400000,
    BLOCK_EEPROM: 0x000000,
    BLOCK_LOG: 0x000000,
    BLOCK_DEFN: 0x000000,
    BLOCK_META: 0x000000,
    BLOCK_LOADER: 0x000000,
}

COMP_STORE = 0
COMP_ZLIB = 1

#: zlib level per block type. The definition snapshot is large, mostly
#: repetitive JSON that is rewritten on every save, so it trades a little
#: size for a much faster save; images keep the smallest output.
_LEVEL = {BLOCK_DEFN: 6}


@dataclass(frozen=True)
class Format:
    kind: str       # platform key: "edc16" | "edc15" | "zf6hp" | "zf8hp" | "hvac" | "vdo"
    magic: bytes
    ext: str
    title: str


FORMATS: Dict[str, Format] = {f.kind: f for f in (
    Format("edc16", b"OVTEDC16", ".edc16", "EDC16 container"),
    Format("edc15", b"OVTEDC15", ".edc15", "EDC15 container"),
    Format("zf6hp", b"OVT6HP\x00\x00", ".6hp", "ZF 6HP container"),
    # ZF 8HP (Gen1): the 2 MB flash image (FRF-assembled or an OBD read) or a
    # 512 KB calibration extract as the main image; BLOCK_META records the
    # image's own identity and block map -- see ovtsuite.zf8hp.container_8hp.
    Format("zf8hp", b"OVT8HP\x00\x00", ".8hp", "ZF 8HP container"),
    Format("hvac", b"OVTHVAC3", ".hvac", "HVAC container"),
    # VDO instrument cluster (VW Mk4 era): the 2 KB EEPROM is the main image
    # (BLOCK_EXTERNAL); BLOCK_META holds what the cluster reported when it was
    # read (ROM version, ident, coding) -- see ovtsuite.vdo.container_vdo.
    Format("vdo", b"OVTVDO\x00\x00", ".vdo", "VDO cluster container"),
)}
_BY_MAGIC = {f.magic: f for f in FORMATS.values()}
_BY_EXT = {f.ext: f for f in FORMATS.values()}


def format_for_magic(head: bytes) -> Optional[Format]:
    """The flavour whose magic opens ``head``, or None."""
    return _BY_MAGIC.get(bytes(head[:8]))


def format_for_ext(path: str) -> Optional[Format]:
    low = (path or "").lower()
    for ext, f in _BY_EXT.items():
        if low.endswith(ext):
            return f
    return None


def format_for_kind(kind: str) -> Optional[Format]:
    return FORMATS.get(kind)


@dataclass
class Block:
    """One region inside a container."""
    type: int
    data: bytes
    address: int = 0
    name: str = ""

    @property
    def size(self) -> int:
        return len(self.data)

    def label(self) -> str:
        return self.name or _DEFAULT_NAME.get(self.type, "block%d" % self.type)


class Container:
    """A native container in memory: a flavour plus an ordered list of
    :class:`Block`."""

    #: Subclasses pin a flavour (``Edc16Container``); the base accepts any.
    FIXED: Optional[Format] = None

    def __init__(self, fmt=None, blocks=None):
        if fmt is None:
            fmt = self.FIXED
        if isinstance(fmt, str):
            fmt = FORMATS[fmt]
        if fmt is None:
            raise ValueError("a container needs a flavour")
        if self.FIXED is not None and fmt is not self.FIXED:
            raise ValueError("%s only holds %s" % (type(self).__name__,
                                                  self.FIXED.kind))
        self.fmt = fmt
        self.blocks = list(blocks or [])

    # -- identity -------------------------------------------------------
    @property
    def kind(self) -> str:
        return self.fmt.kind

    @property
    def ext(self) -> str:
        return self.fmt.ext

    # -- building -------------------------------------------------------
    def add(self, type_, data, address=None, name=None):
        """Append a region. Replaces any existing block of the same type."""
        if not data:
            return self
        addr = _DEFAULT_ADDR.get(type_, 0) if address is None else address
        nm = name or _DEFAULT_NAME.get(type_, "block%d" % type_)
        self.blocks = [b for b in self.blocks if b.type != type_]
        self.blocks.append(Block(type=type_, data=bytes(data), address=addr,
                                 name=nm))
        return self

    def remove(self, type_):
        self.blocks = [b for b in self.blocks if b.type != type_]
        return self

    def get(self, type_):
        """The block of `type_`, or None."""
        for b in self.blocks:
            if b.type == type_:
                return b
        return None

    def has(self, type_) -> bool:
        return self.get(type_) is not None

    def types(self):
        return [b.type for b in self.blocks]

    # -- convenience views ----------------------------------------------
    def external(self):
        """The main image (BLOCK_EXTERNAL), or None."""
        b = self.get(BLOCK_EXTERNAL)
        return b.data if b else None

    def mpc(self):
        """The MPC internal-flash image (BLOCK_MPC), or None."""
        b = self.get(BLOCK_MPC)
        return b.data if b else None

    def eeprom(self):
        b = self.get(BLOCK_EEPROM)
        return b.data if b else None

    def meta(self) -> dict:
        """The BLOCK_META JSON object ({} when absent or unreadable)."""
        import json
        b = self.get(BLOCK_META)
        if b is None:
            return {}
        try:
            out = json.loads(bytes(b.data).decode("utf-8"))
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}

    def set_meta(self, obj: dict):
        import json
        return self.add(BLOCK_META, json.dumps(
            obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def summary(self):
        """A one-line-per-block human description of what the file holds."""
        rows = []
        for b in self.blocks:
            rows.append("%-8s @0x%06X  %d bytes" % (b.label(), b.address, b.size))
        return "\n".join(rows)

    # -- serialisation --------------------------------------------------
    def to_bytes(self, *, compress=True) -> bytes:
        entries = []
        payloads = []
        for b in self.blocks:
            raw = bytes(b.data)
            if compress:
                comp = zlib.compress(raw, _LEVEL.get(b.type, 9))
                # never let "compression" inflate a block
                if len(comp) < len(raw):
                    stored, ctype = comp, COMP_ZLIB
                else:
                    stored, ctype = raw, COMP_STORE
            else:
                stored, ctype = raw, COMP_STORE
            name = b.label().encode("ascii", "replace")[:8]
            entries.append(_ENTRY.pack(
                name, b.type & 0xFF, ctype, 0,
                b.address & 0xFFFFFFFF, len(raw), len(stored),
                zlib.crc32(raw) & 0xFFFFFFFF))
            payloads.append(stored)
        out = bytearray()
        out += _HDR.pack(self.fmt.magic, VERSION, len(self.blocks), 0)
        for e in entries:
            out += e
        for p in payloads:
            out += p
        return bytes(out)

    def save(self, path, *, compress=True):
        with open(path, "wb") as fh:
            fh.write(self.to_bytes(compress=compress))
        return path

    # -- parsing --------------------------------------------------------
    @classmethod
    def from_bytes(cls, data):
        if len(data) < _HDR.size:
            raise ValueError("not an OVT container (too short)")
        magic, version, count, _ = _HDR.unpack_from(data, 0)
        fmt = _BY_MAGIC.get(bytes(magic))
        if fmt is None or (cls.FIXED is not None and fmt is not cls.FIXED):
            raise ValueError("not a%s container (bad magic)" % (
                " " + cls.FIXED.ext if cls.FIXED else "n OVT"))
        if version != VERSION:
            raise ValueError("unsupported %s version %d (this build reads %d)"
                             % (fmt.ext, version, VERSION))
        dir_off = _HDR.size
        payload_off = dir_off + count * _ENTRY.size
        if len(data) < payload_off:
            raise ValueError("%s directory truncated" % fmt.ext)
        blocks = []
        off = payload_off
        for i in range(count):
            (name, btype, comp, _flags, addr, orig, csize, crc) = \
                _ENTRY.unpack_from(data, dir_off + i * _ENTRY.size)
            chunk = data[off:off + csize]
            if len(chunk) != csize:
                raise ValueError("%s block %d payload truncated" % (fmt.ext, i))
            off += csize
            if comp == COMP_ZLIB:
                raw = zlib.decompress(chunk)
            elif comp == COMP_STORE:
                raw = chunk
            else:
                raise ValueError("%s block %d unknown compression %d"
                                 % (fmt.ext, i, comp))
            if len(raw) != orig:
                raise ValueError("%s block %d size mismatch" % (fmt.ext, i))
            if (zlib.crc32(raw) & 0xFFFFFFFF) != crc:
                raise ValueError("%s block %d CRC mismatch (corrupt)"
                                 % (fmt.ext, i))
            nm = name.split(b"\x00", 1)[0].decode("ascii", "replace")
            blocks.append(Block(type=btype, data=raw, address=addr, name=nm))
        obj = cls.__new__(cls)
        obj.fmt = fmt
        obj.blocks = blocks
        return obj

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            return cls.from_bytes(fh.read())


class Edc16Container(Container):
    """The ``.edc16`` flavour (kept as its own name: callers predate the
    shared core)."""
    FIXED = FORMATS["edc16"]


def is_container(path_or_bytes, kind: Optional[str] = None) -> bool:
    """True if the path/bytes open with a native container magic (any flavour,
    or just ``kind``)."""
    try:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            head = bytes(path_or_bytes[:8])
        else:
            with open(path_or_bytes, "rb") as fh:
                head = fh.read(8)
    except Exception:
        return False
    f = _BY_MAGIC.get(head)
    return f is not None and (kind is None or f.kind == kind)
