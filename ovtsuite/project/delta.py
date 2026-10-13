# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkpoint deltas (``.ovd``, spec section 4.1).

A delta records, per member, what changed between a checkpoint's parent and
the checkpoint -- the old AND the new bytes, so it applies in either
direction, and every application is checked against the side it replaces.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

MAGIC = b"OVD1"
ABSENT = 0xFFFFFFFF
OP_PATCH = 1
OP_REPLACE = 2

#: changed ranges closer than this are merged into one patch record
MERGE_GAP = 16

Images = Dict[str, bytes]          # member id -> bytes (missing = absent)


class DeltaError(ValueError):
    """A delta is malformed, or does not fit the image it is applied to."""


@dataclass
class Record:
    member: str
    op: int
    offset: int = 0
    old: Optional[bytes] = b""     # None = absent (OP_REPLACE only)
    new: Optional[bytes] = b""

    @property
    def span(self) -> int:
        if self.op == OP_PATCH:
            return len(self.new or b"")
        return max(len(self.old or b""), len(self.new or b""))


def _ranges(a: bytes, b: bytes, gap: int = MERGE_GAP):
    """[(start, end)] of differing bytes between equal-length ``a`` / ``b``,
    with ranges closer than ``gap`` merged."""
    n = len(a)
    out = []
    i = 0
    # Fast skip over equal 4 KB blocks before the byte scan.
    blk = 4096
    while i < n:
        if a[i:i + blk] == b[i:i + blk]:
            i += blk
            continue
        end = min(n, i + blk)
        j = i
        while j < end:
            if a[j] != b[j]:
                s = j
                while j < end and a[j] != b[j]:
                    j += 1
                if out and s - out[-1][1] < gap:
                    out[-1] = (out[-1][0], j)
                else:
                    out.append((s, j))
            else:
                j += 1
        # A run may continue into the next block.
        i = end
    # Merge runs split at block boundaries.
    merged = []
    for s, e in out:
        if merged and s - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def diff(old: Images, new: Images) -> List[Record]:
    """The records turning ``old`` into ``new`` (empty when equal)."""
    recs: List[Record] = []
    for mid in sorted(set(old) | set(new)):
        a = old.get(mid)
        b = new.get(mid)
        if a is not None and b is not None and len(a) == len(b):
            a, b = bytes(a), bytes(b)
            for s, e in _ranges(a, b):
                recs.append(Record(mid, OP_PATCH, s, a[s:e], b[s:e]))
        elif a != b:
            recs.append(Record(mid, OP_REPLACE, 0,
                               None if a is None else bytes(a),
                               None if b is None else bytes(b)))
    return recs


def changed_bytes(recs: List[Record]) -> int:
    return sum(r.span for r in recs)


def _apply(images: Images, recs: List[Record], forward: bool) -> Images:
    out = {k: bytearray(v) for k, v in images.items()}
    seq = recs if forward else list(reversed(recs))
    for r in seq:
        frm, to = (r.old, r.new) if forward else (r.new, r.old)
        cur = out.get(r.member)
        if r.op == OP_PATCH:
            if cur is None:
                raise DeltaError("delta patches member %r, which is absent"
                                 % r.member)
            end = r.offset + len(frm)
            if end > len(cur) or bytes(cur[r.offset:end]) != frm:
                raise DeltaError("delta does not fit member %r at 0x%X"
                                 % (r.member, r.offset))
            cur[r.offset:end] = to
        elif r.op == OP_REPLACE:
            have = None if cur is None else bytes(cur)
            if have != frm:
                raise DeltaError("delta does not fit member %r (replace)"
                                 % r.member)
            if to is None:
                out.pop(r.member, None)
            else:
                out[r.member] = bytearray(to)
        else:
            raise DeltaError("unknown delta op %d" % r.op)
    return {k: bytes(v) for k, v in out.items()}


def apply_forward(parent: Images, recs: List[Record]) -> Images:
    """The child's images from its parent's."""
    return _apply(parent, recs, True)


def apply_backward(child: Images, recs: List[Record]) -> Images:
    """The parent's images from its child's."""
    return _apply(child, recs, False)


# ---------------------------------------------------------------- encoding
def _blob(b: Optional[bytes]) -> bytes:
    if b is None:
        return struct.pack("<I", ABSENT)
    return struct.pack("<I", len(b)) + b


def encode(recs: List[Record]) -> bytes:
    if len(recs) > 0xFFFF:
        raise DeltaError("too many delta records (%d)" % len(recs))
    out = bytearray(MAGIC)
    out += struct.pack("<H", len(recs))
    for r in recs:
        mid = r.member.encode("ascii")
        if not 0 < len(mid) < 256:
            raise DeltaError("bad member id %r" % r.member)
        out += struct.pack("<B", len(mid)) + mid + struct.pack("<B", r.op)
        if r.op == OP_PATCH:
            if len(r.old) != len(r.new):
                raise DeltaError("patch record sides differ in length")
            out += struct.pack("<II", r.offset, len(r.new)) + r.old + r.new
        else:
            out += _blob(r.old) + _blob(r.new)
    return bytes(out)


def decode(data: bytes) -> List[Record]:
    data = bytes(data)
    if data[:4] != MAGIC:
        raise DeltaError("not an OVT delta (bad magic)")
    try:
        (count,) = struct.unpack_from("<H", data, 4)
        p = 6
        recs = []

        def blob():
            nonlocal p
            (n,) = struct.unpack_from("<I", data, p)
            p += 4
            if n == ABSENT:
                return None
            b = data[p:p + n]
            if len(b) != n:
                raise DeltaError("delta truncated")
            p += n
            return b

        for _ in range(count):
            ml = data[p]
            mid = data[p + 1:p + 1 + ml].decode("ascii")
            p += 1 + ml
            op = data[p]
            p += 1
            if op == OP_PATCH:
                off, n = struct.unpack_from("<II", data, p)
                p += 8
                old = data[p:p + n]
                new = data[p + n:p + 2 * n]
                if len(new) != n:
                    raise DeltaError("delta truncated")
                p += 2 * n
                recs.append(Record(mid, op, off, old, new))
            elif op == OP_REPLACE:
                old = blob()
                new = blob()
                recs.append(Record(mid, op, 0, old, new))
            else:
                raise DeltaError("unknown delta op %d" % op)
    except (struct.error, IndexError, UnicodeDecodeError) as ex:
        raise DeltaError("delta truncated or malformed: %s" % ex) from ex
    if p != len(data):
        raise DeltaError("trailing bytes after delta")
    return recs
