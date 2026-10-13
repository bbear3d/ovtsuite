# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read and write Dimsport ".dim" ECU dump containers.

Derived from DimExp.dll (Delphi, exports Init / Insert / Extract /
GetDimBuffer) and verified byte-for-byte against known plain files -- see
``docs/CONTAINERS.md`` for the full format writeup and derivation.

CONTAINER
---------
A flat chain of chunks. No global header, no index. Each chunk::

    off   size  field
    0x00    4   magic  "DS  " or "CMD "        (DimExp accepts both)
    0x04    2   index  u16 LE   1,2,3,4...
    0x06    2   type   u16 LE   0 = raw, 1 = obfuscated (see below)
    0x08    4   A      u32 LE   payload length
    0x0C    4   B      u32 LE   = A + 8        payload + t1/t2
    0x10    4   C      u32 LE   32-bit sum of the DE-OBFUSCATED payload
    0x14    4   t1     u32 LE   key for the substitution table
    0x18    4   t2     u32 LE   key for the byte-order scramble
    0x1C    A   payload

Next chunk at off + 0x14 + B == off + 0x1C + A. DimExp walks exactly this.

On a stock EDC16U31 dump (verified on ``stock.dim``, 03G906016LQ, and on
4 further real dumps supplied later -- ``stock-1.dim``, ``stock2.dim``,
``test.dim``): chunk 1 -> 0x81000 internal flash, already the plain image
once this container's own layer is decoded -- no further unwrap needed.
(An earlier version of this module assumed chunk 1 was additionally
wrapped in :mod:`ovtsuite.mpc`'s own XOR/zlib framing, matching the
standalone .MPC export format; every real dump checked so far decodes
chunk 1 straight to plain data, with :func:`ovtsuite.mpc.is_encoded`
false on the result, so that assumption was wrong. :func:`internal_flash`
still calls :func:`ovtsuite.mpc.decode_auto`, which is a no-op on already
-plain data, so it keeps working either way -- see
:func:`internal_flash_mpc` for pulling chunk 1 out in compressed .mpc form
instead.) chunk 2 -> 2 MiB external flash (already plain once this
container's own obfuscation is removed); chunk 3 -> 4 KiB EEPROM; chunk 4
-> a plain-text metadata line (DimExp skips decompression for this one).
One real dump (``BRM Malone.dim``) has no chunk 1 at all -- its chunks
start at index 2 -- an unexplained variant, not yet handled specially.

PAYLOAD (type 1)
-----------------
Two layers of obfuscation over a plain zlib stream, both keyed by the
header's own t1/t2 and driven by Delphi's LCG::

    RandSeed := RandSeed * $08088405 + 1        (mod 2^32)
    Random(N) = (N * RandSeed) shr 32           (unsigned)

1. Substitution table -- RandSeed := t1; start from the identity table
   P[0..255]; do 1000 times: swap P[Random(255)] and P[Random(255)].
   (Note Random(255) yields 0..254, so index 255 is never picked -- that
   is the tool's own quirk, reproduced here.) The decode table is P^-1.
2. Byte-order scramble -- RandSeed := t2; n := 2 * len; generate the pairs
   a[i] := Random(len); b[i] := Random(len) for i = 1..n, in that
   interleaved order. The encoder applies swap(buf[a[i]], buf[b[i]]) for
   i = 1..n; the decoder replays them for i = n..1.

Decode: unswap (i = n..1), then buf[j] := Pinv[buf[j]].
Encode: buf[j] := P[buf[j]], then swap (i = 1..n).

``C`` is the byte sum of the buffer at that midpoint -- after the two
layers are removed, before inflate. The result is a zlib stream. Inflated,
it is a 4-byte little-endian length followed by that many bytes of payload.
"""
import struct
import zlib

MAGICS = (b"DS  ", b"CMD ")
HDR = 0x1C
M32 = 0xFFFFFFFF

# Chunk index -> the extension DimExp's own export uses. Chunk 1 (internal
# flash) keeps the .mpc extension because it's the same content a
# standalone .mpc export/import holds -- see the module docstring for what
# form it's actually stored in inside a real .dim (plain, not
# ovtsuite.mpc-encoded, on every real dump checked so far).
EXT = {1: "mpc", 2: "bin", 3: "e2p"}


# ---------------------------------------------------------------- Delphi RNG

class DelphiRandom:
    """System.RandSeed / System.Random(N) from Delphi's RTL."""
    __slots__ = ("seed",)

    def __init__(self, seed):
        self.seed = seed & M32

    def next(self, n):
        self.seed = (self.seed * 0x08088405 + 1) & M32
        return (n * self.seed) >> 32


def _build_tables(t1):
    """Return (P, Pinv) -- the substitution table and its inverse."""
    P = list(range(256))
    r = DelphiRandom(t1)
    for _ in range(1000):
        i = r.next(0xFF)
        j = r.next(0xFF)
        P[i], P[j] = P[j], P[i]
    Pinv = [0] * 256
    for i, v in enumerate(P):
        Pinv[v] = i
    return P, Pinv


def _swap_pairs(t2, length):
    r = DelphiRandom(t2)
    return [(r.next(length), r.next(length)) for _ in range(2 * length)]


def _deobfuscate(payload, t1, t2):
    _, Pinv = _build_tables(t1)
    buf = bytearray(payload)
    for x, y in reversed(_swap_pairs(t2, len(buf))):
        buf[x], buf[y] = buf[y], buf[x]
    return bytes(bytearray(Pinv[c] for c in buf))


def _obfuscate(plain, t1, t2):
    P, _ = _build_tables(t1)
    buf = bytearray(P[c] for c in plain)
    for x, y in _swap_pairs(t2, len(buf)):
        buf[x], buf[y] = buf[y], buf[x]
    return bytes(buf)


# ------------------------------------------------------------------ container

class Chunk:
    """One raw chunk as parsed from a .dim -- ``payload`` is still in this
    container's own obfuscated/compressed framing (see :func:`decode_chunk`
    to get the plain content). Kept this way, rather than eagerly decoded,
    so a chunk that is not being changed can be written back byte-for-byte
    via :func:`build` without a decode/re-encode round trip."""
    __slots__ = ("off", "magic", "index", "type", "A", "B", "C", "t1", "t2", "payload")


def parse(data: bytes):
    """-> list[Chunk], in file order."""
    chunks, o = [], 0
    while o < len(data):
        magic = data[o:o + 4]
        if magic not in MAGICS:
            raise ValueError("bad magic at 0x%X: %r" % (o, magic))
        c = Chunk()
        c.off, c.magic = o, magic
        c.index, c.type = struct.unpack("<HH", data[o + 4:o + 8])
        c.A, c.B, c.C, c.t1, c.t2 = struct.unpack("<IIIII", data[o + 8:o + HDR])
        if c.B != c.A + 8:
            raise ValueError("chunk %d: B=%d != A+8=%d" % (c.index, c.B, c.A + 8))
        c.payload = data[o + HDR:o + HDR + c.A]
        if len(c.payload) != c.A:
            raise ValueError("chunk %d truncated" % c.index)
        chunks.append(c)
        o += HDR + c.A
    return chunks


def load(path_or_data) -> list:
    """Parse a .dim from a path or raw bytes. -> list[Chunk]."""
    data = path_or_data
    if isinstance(path_or_data, str):
        with open(path_or_data, "rb") as fh:
            data = fh.read()
    return parse(data)


def header_bytes(c: Chunk) -> bytes:
    return c.magic + struct.pack("<HHIIIII", c.index, c.type, c.A, c.A + 8,
                                 c.C, c.t1, c.t2)


def decode_chunk(c: Chunk):
    """-> (kind, bytes). kind is 'bin' (length-prefixed, stripped) or 'raw'."""
    if c.type != 1:
        return "raw", c.payload
    plain = _deobfuscate(c.payload, c.t1, c.t2)
    if (sum(plain) & M32) != c.C:
        raise ValueError("chunk %d: checksum %08X != header %08X"
                         % (c.index, sum(plain) & M32, c.C))
    try:
        out = zlib.decompress(plain)
    except zlib.error:
        return "raw", plain
    declared = struct.unpack("<I", out[:4])[0]
    body = out[4:]
    if declared != len(body):
        raise ValueError("chunk %d: declares 0x%X but carries 0x%X"
                         % (c.index, declared, len(body)))
    return "bin", body


def encode_chunk(index, type_, t1, t2, kind, data, magic=b"DS  ", level=9) -> Chunk:
    """Build a new Chunk with fresh content. Reuse the original chunk's
    ``index``/``type_``/``t1``/``t2``/``magic`` (from :func:`parse`) to
    replace its payload while keeping it decodable by the tool that made the
    original file."""
    c = Chunk()
    c.off, c.magic, c.index, c.type, c.t1, c.t2 = 0, magic, index, type_, t1, t2
    if kind == "bin":
        plain = zlib.compress(struct.pack("<I", len(data)) + data, level)
    else:
        plain = data
    c.C = sum(plain) & M32
    c.payload = _obfuscate(plain, t1, t2) if type_ == 1 else plain
    c.A = len(c.payload)
    c.B = c.A + 8
    return c


def build(chunks) -> bytes:
    """Reassemble a .dim byte stream from a list[Chunk], in order. Each
    chunk's ``payload`` is used exactly as stored -- a chunk untouched since
    :func:`parse` reproduces its original bytes exactly; one rebuilt via
    :func:`encode_chunk` carries its new content."""
    return b"".join(header_bytes(c) + c.payload for c in chunks)


def replace_chunk(chunks, index: int, new_plain: bytes, kind: str = None) -> list:
    """Return a NEW list[Chunk] with chunk ``index``'s decoded content
    replaced by ``new_plain`` (re-encoded with that chunk's own type/t1/t2/
    magic, so it stays decodable by DimExp) and every other chunk carried
    through byte-for-byte unchanged. ``kind`` defaults to the original
    chunk's own kind ('bin' length-prefixed vs 'raw'), from :func:`decode_chunk`.

    Raises ``KeyError`` if no chunk with that index exists.
    """
    out = []
    found = False
    for c in chunks:
        if c.index != index:
            out.append(c)
            continue
        found = True
        if kind is None:
            kind, _ = decode_chunk(c)
        out.append(encode_chunk(c.index, c.type, c.t1, c.t2, kind, new_plain, c.magic))
    if not found:
        raise KeyError("no chunk with index %d in this .dim" % index)
    return out


def chunk_by_index(chunks, index: int) -> Chunk:
    for c in chunks:
        if c.index == index:
            return c
    raise KeyError("no chunk with index %d in this .dim" % index)


# ---------------------------------------------------------- high-level parts

def internal_flash(chunks) -> bytes:
    """Chunk 1, fully decoded to the plain 0x81000 internal-flash image,
    ready for ``ovtsuite.edc16.checksum16``. This container's own framing is
    always removed; :mod:`ovtsuite.mpc`'s XOR/zlib framing is removed too,
    if present -- on every real dump checked so far it isn't (chunk 1
    decodes straight to plain data), but :func:`ovtsuite.mpc.decode_auto`
    handles either case transparently."""
    from . import mpc
    _kind, raw = decode_chunk(chunk_by_index(chunks, 1))
    return mpc.decode_auto(raw)


def internal_flash_mpc(chunks, trailer: bytes = b"") -> bytes:
    """Chunk 1 in compressed **.mpc** form (XOR-0xD9/zlib) -- the same
    byte layout a standalone .mpc export/import uses. If this container's
    own chunk 1 payload is already in that form, those exact bytes
    (trailer included) are returned unchanged. Otherwise -- the normal
    case on every real dump checked so far, where chunk 1 decodes straight
    to the plain image -- the plain image is freshly compressed with
    :func:`ovtsuite.mpc.encode`. ``trailer`` only matters in that
    fresh-compress case: the 4 trailing .mpc bytes' meaning hasn't been
    identified (see ``ovtsuite/mpc.py``'s module docstring), so the
    default of no trailer round-trips through this library fine but isn't
    known to satisfy a real BDM/Dimsport tool -- pass a real trailer if
    you have one to preserve."""
    from . import mpc
    _kind, raw = decode_chunk(chunk_by_index(chunks, 1))
    if mpc.is_encoded(raw):
        return raw
    return mpc.encode(raw, trailer=trailer)


def external_flash(chunks) -> bytes:
    """Chunk 2, fully decoded: the plain 1 MB/2 MB EDC16 calibration image."""
    _kind, data = decode_chunk(chunk_by_index(chunks, 2))
    return data


def eeprom(chunks) -> bytes:
    """Chunk 3, fully decoded: the plain 4 KiB EEPROM image."""
    _kind, data = decode_chunk(chunk_by_index(chunks, 3))
    return data


def metadata_text(chunks):
    """Chunk 4's raw text, or None if this .dim doesn't carry one."""
    try:
        _kind, data = decode_chunk(chunk_by_index(chunks, 4))
    except KeyError:
        return None
    return data.decode("latin-1")


def replace_external_flash(chunks, new_bin: bytes) -> list:
    """-> new list[Chunk] with chunk 2 (external flash) replaced by
    ``new_bin`` (a plain 1 MB/2 MB image, e.g. a tuned ``TunerSession``
    binary) and every other chunk unchanged. This is what a "tune, then
    save back to .dim" round trip needs -- internal flash and EEPROM are
    never touched."""
    return replace_chunk(chunks, 2, new_bin, kind="bin")


def replace_internal_flash(chunks, new_image: bytes) -> list:
    """-> new list[Chunk] with chunk 1 (internal flash) replaced by
    ``new_image`` (a plain 0x81000 image, e.g. after
    ``ovtsuite.edc16.checksum16.correct``), written as a plain "bin" chunk -- the
    same framing chunk 2 (external flash) uses. Every real dump checked so
    far stores chunk 1 this way (see the module docstring), so this is
    what keeps a rebuilt .dim's chunk 1 shaped like a genuine one. If your
    source .dim turns out to be the nested-.mpc variant instead (reading
    handles it fine either way, via :func:`internal_flash`), re-wrap
    ``new_image`` with :func:`ovtsuite.mpc.encode` yourself before calling
    this, so the chunk you write back matches the chunk you read."""
    return replace_chunk(chunks, 1, new_image, kind="bin")
