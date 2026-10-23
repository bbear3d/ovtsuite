# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Binary images as segments of bytes at addresses: BIN, Intel HEX and
Motorola S-record, read and written.

Each format's own records decide everything: an Intel HEX line's type and
address, an S-record's type and address width. A file is a HEX or an
S-record by its records (every non-blank line one, each checksum right), not
by its name; anything else is a plain binary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

FORMATS = ("bin", "hex", "srec")


class ImageError(ValueError):
    """A file that claims to be a HEX or S-record and does not parse."""


@dataclass
class Image:
    #: ``[(address, bytes)]``, sorted, not overlapping
    segments: List[Tuple[int, bytes]] = field(default_factory=list)
    #: what a gap between segments reads as when the image is made flat
    fill: int = 0xFF
    source: str = "bin"

    @property
    def start(self) -> int:
        return self.segments[0][0] if self.segments else 0

    @property
    def end(self) -> int:
        """One past the last byte."""
        return max((a + len(b) for a, b in self.segments), default=0)

    def flat(self, start: Optional[int] = None, end: Optional[int] = None) -> bytes:
        """The bytes from ``start`` to ``end`` (default: the whole span),
        gaps filled."""
        s = self.start if start is None else start
        e = self.end if end is None else end
        buf = bytearray([self.fill]) * max(0, e - s)
        for a, b in self.segments:
            lo, hi = max(a, s), min(a + len(b), e)
            if lo < hi:
                buf[lo - s:hi - s] = b[lo - a:hi - a]
        return bytes(buf)

    # ---------------------------------------------------------------- ops
    def crop(self, start: int, end: int) -> "Image":
        return Image([(start, self.flat(start, end))], self.fill, self.source)

    def pad(self, size: int) -> "Image":
        """The image from its start, ``size`` bytes long (filled or cut)."""
        return self.crop(self.start, self.start + size)

    def rebased(self, new_start: int) -> "Image":
        d = new_start - self.start
        return Image([(a + d, b) for a, b in self.segments], self.fill, self.source)

    def swapped16(self) -> "Image":
        """Every 16-bit word byte-swapped (an image read by a tool that
        stores words the other way round)."""
        out = []
        for a, b in self.segments:
            b = bytearray(b)
            for i in range(0, len(b) - 1, 2):
                b[i], b[i + 1] = b[i + 1], b[i]
            out.append((a, bytes(b)))
        return Image(out, self.fill, self.source)


def merge(images: List[Image], fill: int = 0xFF) -> Image:
    """One address space from several images; an overlap is an error, not
    a choice made silently."""
    segs = sorted((s for im in images for s in im.segments), key=lambda s: s[0])
    for (a1, b1), (a2, _b2) in zip(segs, segs[1:]):
        if a1 + len(b1) > a2:
            raise ImageError("images overlap at 0x%X" % a2)
    return Image(_coalesce(segs), fill)


def _coalesce(segs: List[Tuple[int, bytes]]) -> List[Tuple[int, bytes]]:
    out: List[Tuple[int, bytearray]] = []
    for a, b in sorted(segs, key=lambda s: s[0]):
        if out and out[-1][0] + len(out[-1][1]) == a:
            out[-1][1].extend(b)
        else:
            out.append((a, bytearray(b)))
    return [(a, bytes(b)) for a, b in out]


# ------------------------------------------------------------- detecting
def _lines(data: bytes) -> List[str]:
    try:
        text = bytes(data).decode("ascii")
    except UnicodeDecodeError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def detect(data: bytes) -> str:
    """``"hex"``, ``"srec"`` or ``"bin"`` from the file's records."""
    head = bytes(data[:64]).lstrip()
    if head[:1] == b":" and _is_hex(data):
        return "hex"
    if head[:1] == b"S" and head[1:2].isdigit() and _is_srec(data):
        return "srec"
    return "bin"


def _is_hex(data) -> bool:
    lines = _lines(data)
    if not lines:
        return False
    for ln in lines[:64]:
        try:
            _hex_record(ln)
        except ImageError:
            return False
    return True


def _is_srec(data) -> bool:
    lines = _lines(data)
    if not lines:
        return False
    for ln in lines[:64]:
        try:
            _srec_record(ln)
        except ImageError:
            return False
    return True


# ------------------------------------------------------------- Intel HEX
def _hex_record(line: str):
    if not line.startswith(":"):
        raise ImageError("not an Intel HEX record: %r" % line[:20])
    try:
        b = bytes.fromhex(line[1:])
    except ValueError:
        raise ImageError("not hex digits: %r" % line[:20])
    if len(b) < 5 or len(b) != b[0] + 5:
        raise ImageError("record length wrong: %r" % line[:20])
    if sum(b) & 0xFF:
        raise ImageError("record checksum wrong: %r" % line[:20])
    return b[3], (b[1] << 8) | b[2], b[4:-1]


def read_hex(data: bytes) -> Image:
    segs = []
    ext = 0
    for ln in _lines(data):
        t, a, body = _hex_record(ln)
        if t == 0:
            segs.append((ext + a, bytes(body)))
        elif t == 1:
            break
        elif t == 2:
            ext = int.from_bytes(body, "big") << 4
        elif t == 4:
            ext = int.from_bytes(body, "big") << 16
        # 3 / 5: start addresses, not image content
    return Image(_coalesce(segs), source="hex")


def _hex_line(t: int, a: int, body: bytes) -> str:
    rec = bytes([len(body), (a >> 8) & 0xFF, a & 0xFF, t]) + bytes(body)
    return ":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper()


def write_hex(img: Image, record: int = 16) -> str:
    """Extended linear address records; gaps are left unwritten."""
    out = []
    ext = None
    for a0, b in img.segments:
        i = 0
        while i < len(b):
            a = a0 + i
            if (a >> 16) != ext:
                ext = a >> 16
                out.append(_hex_line(4, 0, ext.to_bytes(2, "big")))
            # a record never crosses a 64 KiB boundary
            n = min(record, len(b) - i, 0x10000 - (a & 0xFFFF))
            out.append(_hex_line(0, a & 0xFFFF, b[i:i + n]))
            i += n
    out.append(":00000001FF")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------- S-record
_SREC_ADDR = {"0": 2, "1": 2, "2": 3, "3": 4, "5": 2, "6": 3, "7": 4, "8": 3, "9": 2}


def _srec_record(line: str):
    if len(line) < 4 or line[0] != "S" or line[1] not in _SREC_ADDR:
        raise ImageError("not an S-record: %r" % line[:20])
    try:
        b = bytes.fromhex(line[2:])
    except ValueError:
        raise ImageError("not hex digits: %r" % line[:20])
    if len(b) != b[0] + 1:
        raise ImageError("record length wrong: %r" % line[:20])
    if (sum(b) & 0xFF) != 0xFF:
        raise ImageError("record checksum wrong: %r" % line[:20])
    n = _SREC_ADDR[line[1]]
    return line[1], int.from_bytes(b[1:1 + n], "big"), b[1 + n:-1]


def read_srec(data: bytes) -> Image:
    segs = []
    for ln in _lines(data):
        t, a, body = _srec_record(ln)
        if t in "123":
            segs.append((a, bytes(body)))
    return Image(_coalesce(segs), source="srec")


def _srec_line(t: str, a: int, body: bytes) -> str:
    n = _SREC_ADDR[t]
    rec = bytes([n + len(body) + 1]) + a.to_bytes(n, "big") + bytes(body)
    return "S" + t + (rec + bytes([(~sum(rec)) & 0xFF])).hex().upper()


def write_srec(img: Image, record: int = 32, header: str = "") -> str:
    """S1/S2/S3 by the highest address (the narrowest that holds it)."""
    top = max(img.end - 1, 0)
    t = "1" if top <= 0xFFFF else ("2" if top <= 0xFFFFFF else "3")
    end_t = {"1": "9", "2": "8", "3": "7"}[t]
    out = [_srec_line("0", 0, header.encode("ascii", "replace")[:64])]
    count = 0
    for a0, b in img.segments:
        for i in range(0, len(b), record):
            out.append(_srec_line(t, a0 + i, b[i:i + record]))
            count += 1
    if count <= 0xFFFF:
        out.append(_srec_line("5", count, b""))
    out.append(_srec_line(end_t, 0, b""))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------- any
def read(data: bytes, base: int = 0) -> Image:
    """An image from a file's bytes; a plain binary starts at ``base``."""
    kind = detect(data)
    if kind == "hex":
        return read_hex(data)
    if kind == "srec":
        return read_srec(data)
    return Image([(base, bytes(data))], source="bin")


def write(img: Image, fmt: str, record: Optional[int] = None) -> bytes:
    if fmt == "hex":
        return write_hex(img, record or 16).encode("ascii")
    if fmt == "srec":
        return write_srec(img, record or 32).encode("ascii")
    if fmt == "bin":
        return img.flat()
    raise ValueError("unknown image format %r" % fmt)
