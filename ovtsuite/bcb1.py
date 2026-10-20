# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch "BCB Type1" area codec -- the coding used by EDC15 .sgo containers.

Each coded area names this codec in its own cleartext provenance header
(``BCB Type1 (C) R.Bosch GmbH'97 K3/EAW2-Wr``), which also carries the area's
24-bit sum as ASCII hex. After the header comes the two-byte marker ``1A 01``
and then records XORed with an 11-byte key.

Record header, u16 big-endian::

    flag = h >> 14      len = h & 0x3FFF

    flag 0   literal -- ``len`` raw bytes follow
    flag 1   RLE     -- one byte follows, repeated ``len`` times
    flag 3   terminal -- the 24-bit big-endian sum of this area's decoded
                         bytes follows; the area ends

There is NO flag 2. A flag-2 record only ever appears when the key is wrong,
so treat one as "wrong key", not as an unknown record type.

This is NOT the ZF 6HP ``MILKYWAY`` variant in :mod:`ovtsuite.flash.crypto`.
The important trap: decoding an EDC15 area under the ZF key produces records
whose lengths come out EXACTLY right while the content is garbage, so
agreement on the declared length is not validation here. Only the format's own
checksums are: the terminal 24-bit sum (checked by :func:`decode`), the same
value in the provenance header, and the section table's 16-bit field.

The key
-------
Eleven bytes, seven of which are constant for the format::

    24 AA ?? ?? ?? ?? 41 E1 55 B2 3F

Bytes 2..5 vary per dataset and are stored NOWHERE -- not in the container
headers, not in the metadata block, not in the decoded image (all searched).
:func:`solve_key` recovers them from the file itself, with no reference file
and no content heuristics:

1. In every area checked, the record framing lands only on key indices that
   are constant, so the framing is fully determined by the known seven bytes
   (:func:`_frame`). If an area ever needs an unknown byte to frame, it is
   skipped rather than guessed.
2. That leaves the four unknowns appearing only in the literal-byte *sum*, so
   the terminal checksum is one 24-bit equation in four unknown bytes. Group
   the literal bytes by key index, and the equation separates into
   ``f2(a) + f3(b) + f4(c) + f5(d) == need``, solved meet-in-the-middle over
   two halves of 65536 each.
3. Intersecting the candidate sets of a file's areas, then requiring the main
   area's own terminal sum to check out, leaves exactly one key.

The ``KEY.BC4`` area uses its own fixed value for bytes 2..5
(:data:`KEY_BC4`), validated the same way by its own terminal sum.
"""

import re
import struct
from typing import Dict, List, Optional, Set, Tuple

MARKER = b"\x1a\x01"
KEYLEN = 11
MASK24 = 0xFFFFFF

#: The seven key bytes that are constant for the format.
KEY_CONST: Dict[int, int] = {0: 0x24, 1: 0xAA, 6: 0x41, 7: 0xE1,
                             8: 0x55, 9: 0xB2, 10: 0x3F}
#: The four key indices that vary per dataset and have to be solved.
KEY_UNKNOWN: Tuple[int, ...] = (2, 3, 4, 5)
#: The fixed key of the ``KEY.BC4`` area (byte-identical in every file seen).
KEY_BC4 = bytes.fromhex("24AA5A23842141E155B23F")

#: The ZF 5HP (VW "01V"/"01L") dialect's key: the fixed 6-byte ASCII string
#: ``GEHEIM`` ("secret"), nothing per-dataset to solve. The record framing and
#: terminal 24-bit sum are identical to the EDC15 dialect; only the key length
#: (6 vs 11) differs, which :func:`decode`/:func:`encode` take from the key.
KEY_5HP = b"GEHEIM"

#: Marker of the codec in an area's cleartext provenance header. BOTH Bosch
#: BCB Type1 dialects carry it, so it identifies the CODEC FAMILY and must not
#: be used to tell them apart:
#:
#:   EDC15      one line, ``... .CC3 24.09.2003 12:15   F82C80``, then
#:              ``BCB Type1 (C) R.Bosch GmbH'97 K3/EAW2-Wr`` -- 11-byte key
#:              with four per-dataset bytes (:func:`solve_key`).
#:   ZF 6HP     ``[SOURCE]/[CHECKSUM]/[FORMAT]/[START_ADDRESS]/[END_ADDRESS]``
#:              blocks, ``BCB Type1 (C) R. Bosch GmbH 2000 K3/EMW3-Ws`` --
#:              the 8-byte MILKYWAY key in :mod:`ovtsuite.flash.crypto`.
#:
#: What tells them apart is which key actually decodes to content matching the
#: area's own stated 24-bit sum (:func:`header_sum`), which both dialects
#: carry; ovtsuite.sgo tries each and keeps only what validates.
BANNER = b"BCB Type1"

#: The ZF dialect's checksum block.
_ZF_CKSUM_RE = re.compile(rb"(\[CHECKSUM\]\s*\n\s*)([0-9A-Fa-f]{1,8})")


def looks_like(raw: bytes) -> bool:
    """True if ``raw`` (a section blob after the outer XOR 0xFF) is a BCB
    Type1 area: it names the codec in the clear and carries the marker."""
    i = raw.find(MARKER)
    return i != -1 and BANNER in raw[:i]


def split(raw: bytes) -> Tuple[bytes, bytes]:
    """``(cleartext provenance prefix, record body)``."""
    i = raw.find(MARKER)
    if i < 0:
        raise ValueError("bcb1: no 1A 01 marker")
    return raw[:i], raw[i + 2:]


def header_sum(prefix: bytes) -> Optional[int]:
    """The 24-bit sum the provenance header states, as an int, or None.

    The header's FIRST line ends with the value as six ASCII hex digits::

        \\WI0PC194\\C:\\PROGRAMM\\08\\03890601.CC3 24.09.2003 12:15   F82C80

    Only that line is considered; the banner line after it is not searched.
    """
    m = _ZF_CKSUM_RE.search(prefix)
    if m:
        return int(m.group(2), 16)
    toks = prefix.split(b"\n", 1)[0].split()
    if toks and len(toks[-1]) == 6:
        try:
            return int(toks[-1], 16)
        except ValueError:
            pass
    return None


def set_header_sum(prefix: bytes, value: int) -> bytes:
    """``prefix`` with the stated 24-bit sum replaced by ``value``.

    Needed whenever an area is re-encoded: the header restates the area's sum
    and a genuine file always has the two agree, so leaving it stale would
    make the area fail its own validation on the next parse.
    """
    m = _ZF_CKSUM_RE.search(prefix)
    if m:
        digits = (b"%0*X" % (len(m.group(2)), value & MASK24))[-len(m.group(2)):]
        return prefix[:m.start(2)] + digits + prefix[m.end(2):]
    line, sep, rest = prefix.partition(b"\n")
    toks = line.split()
    if not toks or len(toks[-1]) != 6:
        return prefix
    cut = len(line) - len(line.rstrip())
    body = line[:len(line) - cut] if cut else line
    body = body[:len(body) - 6] + (b"%06X" % (value & MASK24))
    return body + line[len(line) - cut:] + sep + rest


def _frame(body: bytes, dlen: int, k: Dict[int, int], keylen: int = KEYLEN):
    """Walk the records under the partial key ``k``.

    Returns ``(literals, rles, terminal_offset)`` where ``literals`` and
    ``rles`` are lists of ``(offset, length)``. Raises :class:`KeyError` if a
    key byte that is not in ``k`` would be needed, and returns None if the
    framing is impossible (which is how a wrong key is rejected).

    ``keylen`` is the key's repeat period (11 for the EDC15 dialect; the ZF
    5HP ``GEHEIM`` dialect passes 6). The EDC15 solver keeps the default.
    """
    n, p, tot = len(body), 0, 0
    lits: List[Tuple[int, int]] = []
    rles: List[Tuple[int, int]] = []

    def g(j: int) -> int:
        idx = j % keylen
        if idx not in k:
            raise KeyError(idx)
        return body[j] ^ k[idx]

    while p + 2 <= n:
        h = (g(p) << 8) | g(p + 1)
        flag, ln, q = h >> 14, h & 0x3FFF, p + 2
        if flag == 0:
            if q + ln > n or tot + ln > dlen:
                return None
            lits.append((q, ln))
            tot += ln
            p = q + ln
        elif flag == 1:
            if q >= n or tot + ln > dlen:
                return None
            rles.append((q, ln))
            tot += ln
            p = q + 1
        elif flag == 3:
            if q + 3 != n or tot != dlen:
                return None
            return lits, rles, q
        else:
            return None          # flag 2 -- wrong key, never a real record
    return None


def decode(body: bytes, dlen: int, key: bytes) -> Optional[bytes]:
    """Decode one area's record body with a full key.

    The key's length is its repeat period: 11 bytes for the EDC15 dialect,
    6 bytes (``b"GEHEIM"``) for the ZF 5HP dialect. Returns the plaintext, or
    None if the framing or the format's own terminal 24-bit sum does not check
    out. Never returns unvalidated bytes.
    """
    kl = len(key)
    k = {i: key[i] for i in range(kl)}
    framed = _frame(body, dlen, k, kl)
    if framed is None:
        return None
    lits, rles, q = framed
    out = bytearray()
    for off, kind, ln in sorted([(o, "L", n) for o, n in lits] +
                                [(o, "R", n) for o, n in rles]):
        if kind == "L":
            out += bytes(body[off + j] ^ key[(off + j) % kl]
                         for j in range(ln))
        else:
            out += bytes([body[off] ^ key[off % kl]]) * ln
    want = 0
    for d in range(3):
        want = (want << 8) | (body[q + d] ^ key[(q + d) % kl])
    if len(out) != dlen or (sum(out) & MASK24) != want:
        return None
    return bytes(out)


#: Cap on how many quads one area may contribute. A short area constrains the
#: four unknowns only weakly -- its literal-byte sums span far less than the
#: 24-bit checksum space, so a huge number of quads satisfy its equation. Such
#: an area is worthless as a constraint and enumerating it would exhaust
#: memory, so it is dropped (treated like an unframable one) once it exceeds
#: this. Real containers' areas come in far below it (the observed worst case
#: is ~36k quads from a 1536-byte area).
_MAX_CANDIDATES = 1 << 18


def _candidates(body: bytes, dlen: int,
                limit: int = _MAX_CANDIDATES) -> Optional[Set[Tuple[int, ...]]]:
    """Candidate ``(k2, k3, k4, k5)`` quads from one area's terminal sum.

    None when this area cannot be framed from the constant key bytes alone,
    or when it is too weak a constraint to enumerate (see
    :data:`_MAX_CANDIDATES`). Never returns a partial set.
    """
    try:
        framed = _frame(body, dlen, dict(KEY_CONST))
    except KeyError:
        return None
    if framed is None:
        return None
    lits, rles, q = framed
    groups: Dict[int, List[int]] = {j: [] for j in range(KEYLEN)}
    for off, ln in lits:
        for j in range(off, off + ln):
            groups[j % KEYLEN].append(body[j])
    base = 0
    for j, bs in groups.items():
        if j in KEY_CONST:
            base += sum(b ^ KEY_CONST[j] for b in bs)
    for off, ln in rles:
        if off % KEYLEN not in KEY_CONST:
            return None
        base += (body[off] ^ KEY_CONST[off % KEYLEN]) * ln
    want = 0
    for d in range(3):
        if (q + d) % KEYLEN not in KEY_CONST:
            return None
        want = (want << 8) | (body[q + d] ^ KEY_CONST[(q + d) % KEYLEN])
    need = (want - base) & MASK24
    f = {j: [sum(b ^ v for b in groups[j]) & MASK24 for v in range(256)]
         for j in KEY_UNKNOWN}
    half: Dict[int, List[Tuple[int, int]]] = {}
    for a in range(256):
        fa = f[2][a]
        for b in range(256):
            half.setdefault((fa + f[3][b]) & MASK24, []).append((a, b))
    out: Set[Tuple[int, ...]] = set()
    for c in range(256):
        fc = f[4][c]
        for d in range(256):
            for a, b in half.get((need - fc - f[5][d]) & MASK24, ()):
                out.add((a, b, c, d))
        if len(out) > limit:
            return None
    return out


def key_from(quad) -> bytes:
    k = dict(KEY_CONST)
    for j, v in zip(KEY_UNKNOWN, quad):
        k[j] = v
    return bytes(k[i] for i in range(KEYLEN))


def solve_key(areas) -> Optional[bytes]:
    """Solve the dataset key for a container's BCB Type1 areas.

    ``areas`` is an iterable of ``(record_body, declared_len)``. The largest
    area is used for final confirmation; every smaller one contributes its
    terminal-sum constraint. Returns None if the key cannot be pinned to
    exactly one value -- callers leave such sections unrecognized rather than
    decode them with a guess.
    """
    areas = list(areas)
    if not areas:
        return None
    main = max(areas, key=lambda a: a[1])
    # Largest first: a longer area spreads its literal sums over more of the
    # 24-bit space and so yields the tightest (smallest) candidate set, which
    # keeps every later intersection cheap.
    others = sorted((a for a in areas if a is not main and a[1] != main[1]),
                    key=lambda a: -a[1])
    cand: Optional[Set[Tuple[int, ...]]] = None
    for body, dlen in others:
        got = _candidates(body, dlen)
        if got is None:
            continue
        cand = got if cand is None else (cand & got)
        if not cand:
            return None
    if not cand:
        return None
    survivors = []
    for quad in cand:
        key = key_from(quad)
        if decode(main[0], main[1], key) is not None:
            survivors.append(key)
            if len(survivors) > 1:
                return None          # ambiguous -- refuse rather than pick
    return survivors[0] if len(survivors) == 1 else None


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------

_MAX_RECORD = 0x3FFF        # 14-bit length field
_MIN_RLE_RUN = 4            # shorter runs cost more as RLE than as literals


def encode(plaintext: bytes, key: bytes) -> bytes:
    """Encode one area's plaintext back to a record body under ``key``.

    ``decode(encode(x, k), len(x), k) == x`` for any ``x``. The literal/RLE
    split is this encoder's own choice and need not match what Bosch's packer
    produced, so re-encoding an area does not generally reproduce its original
    bytes; :func:`ovtsuite.sgo.apply_image` keeps the original blob for any
    area whose plaintext did not change, so that only matters for areas the
    caller actually edited.
    """
    recs = bytearray()
    i, n = 0, len(plaintext)
    while i < n:
        b = plaintext[i]
        j = i
        while j < n and (j - i) < _MAX_RECORD and plaintext[j] == b:
            j += 1
        run = j - i
        if run >= _MIN_RLE_RUN:
            recs += struct.pack(">H", (1 << 14) | run)
            recs.append(b)
            i = j
            continue
        k = i
        while k < n and (k - i) < _MAX_RECORD:
            if (k + _MIN_RLE_RUN <= n
                    and len(set(plaintext[k:k + _MIN_RLE_RUN])) == 1):
                break
            k += 1
        ln = k - i
        recs += struct.pack(">H", ln)
        recs += plaintext[i:k]
        i = k
    total = sum(plaintext) & MASK24
    recs += struct.pack(">H", 3 << 14)
    recs += bytes([(total >> 16) & 0xFF, (total >> 8) & 0xFF, total & 0xFF])
    kl = len(key)
    return bytes(b ^ key[p % kl] for p, b in enumerate(recs))
