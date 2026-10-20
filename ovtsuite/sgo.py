# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r'''v2 "SGML Object File" (.sgo) container support -- transparent read/write.

Independent, clean-room implementation. The container LAYOUT (which bytes
mean what) was learned by reading SGOUnpack's own scripts structurally (see
each fact's docstring below for exactly what was read and why it's safe) --
no SGOUnpack code is imported, ported, or copied anywhere in this module. The
per-section CIPHERS are either already-shipped OVT-Suite code
(:mod:`ovtsuite.flash.crypto`, independently cryptanalysed for the flash
read/write transport, reused here because real files empirically decode with
it -- see ``_decode_section``) or a trivial, non-creative XOR (the outer
whole-file 0xFF layer every section carries, and "plain" sections that carry
nothing else).

FORMAT FACTS (all independently verified against real files on disk, not
assumed):

  offset 0x00, 16 bytes: magic ``b"SGML Object File"``.
  offset 0x15, u32 LE: file integrity dword = (sum of every OTHER byte in
      the file) - 1, mod 2**32. Verified exactly against 2 real files from
      two different platforms (ZF 6HP and EDC16).
  offset 0x29, u32 LE: ``metastart``.
  offset ``metastart``, u32 LE: ``metalen`` -- the metadata/tape block
      immediately follows, ``metalen`` bytes, starting at ``metastart + 4``.
  offset 0x2D, u32 LE: ``sgolen`` -- offset where the section table ends
      and a trailer (section index, unexamined) begins.
  section table starts at ``metastart + metalen + 4``; each entry is 0x19
      (25) bytes:
        +0x00  u24 BE   addr           target flash address
        +0x03  u8       crypt          NOT a reliable scheme selector on its
                                        own (see ``_decode_section``) -- kept
                                        and round-tripped verbatim, never
                                        interpreted as ground truth
        +0x04  u24 BE   declared_len   decoded/target length
        +0x13  u16 BE   checksum16     16-bit additive sum of the DECODED
                                        section bytes. Verified against 5
                                        real EDC16 sections (4 independent
                                        addresses in one file, matching a
                                        real reference binary exactly) --
                                        this is what makes decode
                                        self-verifying instead of a guess.
        +0x15  u32 LE   slen           stored (possibly compressed) length
                                        of the blob that follows
        +0x19  blob[slen]              the section's stored bytes, always
                                        XORed with 0xFF at rest (the "outer"
                                        layer -- present whether or not the
                                        section carries a further cipher)
      next section starts at ``prev_off + 0x19 + slen``.

A section's true payload sits behind the outer XOR-0xFF, and beyond that:

  - "plain": nothing further. xorff(blob) IS the plaintext.
  - "bcb": a plaintext ASCII provenance header (observed: a "[SOURCE]/
    [CHECKSUM]/[FORMAT]" block) can precede the real stream, so the BCB
    marker (``ovtsuite.flash.crypto.ZF6HP_BCB_MARKER``) is located by
    search, not assumed to sit at offset 0; the bytes from there are handed
    to :func:`ovtsuite.flash.crypto.zf6hp_bcb_decode` -- OVT-Suite's own,
    already-shipped, already write-capture-verified BCB/MILKYWAY decoder.
    Re-encoding uses :func:`ovtsuite.flash.crypto.zf6hp_bcb_encode`; the
    ASCII prefix (if any) is preserved verbatim, but the compressed bytes it
    produces are not guaranteed byte-identical to what a real tool would
    have written for the same content (the encoder's own docstring already
    flags this) -- only DECODE(ENCODE(x)) == x is guaranteed, which is all
    a save-with-edits needs.
  - "edc16": xorff(blob) XORed with :func:`ovtsuite.flash.crypto.keystream`
    -- the SAME cipher already shipped for EDC16 transferData (0x36) writes,
    independently cryptanalysed there, never re-derived here. Confirmed
    byte-for-byte against real (.sgo, .bin) pairs across multiple different
    2 MB EDC16U31-family files (100% match). NOT actually address-keyed --
    :func:`ovtsuite.flash.crypto.keystream`'s internal state always starts
    from the same hardcoded constants and only ever depends on the NUMBER
    of bytes requested, never on the ``start`` value passed in (checked
    directly against the ported algorithm: nothing in ``encrypt`` reads
    ``start``/``pos``/``src`` except as loop bookkeeping and an index into
    the plaintext) -- calling it once per section with that section's own
    length is what makes decoding position-independent, not the address
    argument itself.

    2026-09-21: solved for the 1 MB EDC16U1 family too, and it needed NO new
    cipher at all. 52/52 real (.sgo, unpacked reference .bin) pairs in the
    project's EDC16U1 catalogue carry a section addressed at
    ``_U1_MIRROR_BASE`` (0x800000) whose ``edc16``-decoded content is a
    byte-for-byte duplicate of the ENTIRE real image starting at flash
    offset 0 (not a separate address space like ``MAIN_IMAGE_MAX``'s own
    2 MB "internal flash" convention below) -- exact match, zero
    exceptions, using this SAME already-shipped cipher, once the section's
    declared address is remapped back to its real flash offset (see
    ``_real_span``). The two lower, smaller-addressed sections some of
    these containers also carry (0x8000 and 0x28000, both crypt=2/3) do NOT
    decode correctly under any scheme this module knows -- an "edc16"
    attempt there reproduces only ~8% of real bytes despite passing its own
    checksum16 self-check (the same false-accept risk
    ``_cross_check_edc16`` exists to catch) -- and are structurally
    redundant with the mirror section for every file checked, so they are
    always left/demoted to unrecognized rather than trusted (see
    ``_resolve_overlaps``).

Because the section header's own ``crypt`` byte is demonstrably NOT a
reliable scheme selector (real per-family unpacker configs override it; see
the module-level evidence above), ``_decode_section`` never branches on it.
Instead every section is decoded by trying each known scheme and keeping
ONLY the one whose result reproduces the section's own declared length AND
its own declared checksum16 -- both are format-defined fields already in the
file, not values this module invents, so this is verification, not a guess.
A section matching no known scheme is left with ``plaintext=None`` and
``scheme=None``: its bytes are still carried through unchanged on save, but
its region cannot be viewed/edited as calibration data.
'''

import struct
import zlib
from dataclasses import dataclass, field
from typing import List, Optional

from . import branding as _branding
from .platforms import zf6hp as _zf6hp
from . import bcb1 as _bcb1
from .flash import crypto as _crypto

MAGIC = b"SGML Object File"

_HDR_LEN = 0x19          # section header size
_CKSUM16_OFF = 0x13      # section header: u16 BE, sum of decoded bytes
_RANGE_LO_OFF = 0x0D     # section header: u24 BE, real range low address
_RANGE_HI_OFF = 0x10     # section header: u24 BE, real range high address
_SLEN_OFF = 0x15         # section header: u32 LE, stored blob length
_FILE_CKSUM_OFF = 0x15   # file header: u32 LE integrity dword


def detect(data: bytes) -> bool:
    """True when ``data`` is a v2 SGML Object File."""
    return len(data) >= 0x30 and data[:len(MAGIC)] == MAGIC


def _u32le(b, p):
    return struct.unpack_from("<I", b, p)[0]


def _u24be(b, p):
    return struct.unpack_from(">I", b"\x00" + bytes(b[p:p + 3]), 0)[0]


def _pack_u24be(v):
    return struct.pack(">I", v & 0xFFFFFF)[1:]


def _xorff(d: bytes) -> bytes:
    return bytes(x ^ 0xFF for x in d)


@dataclass
class SgoSection:
    index: int
    off: int
    addr: int
    crypt: int
    declared_len: int
    header: bytes
    blob: bytes
    scheme: Optional[str] = None
    plaintext: Optional[bytes] = None
    bcb_prefix: bytes = b""
    #: Real address range from the section header (_RANGE_LO_OFF/_RANGE_HI_OFF).
    #: ``hi - lo + 1 == declared_len`` on every real file checked. This, not
    #: ``addr``, is where an EDC15 area actually belongs: EDC15 containers
    #: declare ``addr`` as a constant tag (0x680000 / 0x40E000) for every
    #: section, so ``addr`` cannot place them.
    lo: int = 0
    hi: int = 0
    #: The BCB Type1 key, for a "bcb1" section (see :mod:`ovtsuite.bcb1`): the
    #: solved 11-byte EDC15 key, or the fixed 6-byte ZF 5HP ``GEHEIM`` key.
    bcb1_key: bytes = b""
    #: How this "bcb1" section's 16-bit field is computed on re-encode:
    #: "sum" (EDC15 additive) or "crc32" (ZF 5HP, zlib.crc32 low-16).
    bcb1_ck16: str = "sum"

    @property
    def end(self) -> int:
        return self.addr + self.declared_len


@dataclass
class SgoContainer:
    prefix: bytes
    sections: List[SgoSection]
    trailer: bytes
    name: str = ""
    #: The module whose checksum must vouch for a decode before it is kept,
    #: when that module is not installed (its sections are left undecoded):
    #: "edc16" for an EDC16 container opened without the EDC16 module.
    needs_module: str = ""

    @property
    def unrecognized(self) -> List[SgoSection]:
        return [s for s in self.sections if s.scheme is None]


def _decode_section(sec: SgoSection) -> None:
    """Fill ``sec.plaintext``/``sec.scheme`` in place. Never guesses: a
    candidate is accepted only if it reproduces both the section's own
    declared length and its own declared checksum16 field."""
    target_cksum = struct.unpack_from(">H", sec.header, _CKSUM16_OFF)[0]
    raw = _xorff(sec.blob)


    def _accept(plaintext, scheme, prefix=b"", require_cksum=True):
        if len(plaintext) != sec.declared_len:
            return False
        if require_cksum and (sum(plaintext) & 0xFFFF) != target_cksum:
            return False
        sec.plaintext = plaintext
        sec.scheme = scheme
        sec.bcb_prefix = prefix
        return True

    # 1) plain: nothing beyond the outer XOR-0xFF.
    if _accept(raw, "plain"):
        return
    # 1b) Aisin 09D: a plain section whose header checksum16 is not a usable
    #     self-check (project doc aisin_09d_container_and_flash.md). Accepted
    #     only on the image's OWN structure instead: placed at its declared
    #     address it carries the 09D checksum descriptor declaring exactly the
    #     region it spans.
    from .platforms import aisin as _aisin
    if (sec.addr == _aisin.D09_START
            and len(raw) == _aisin.D09_END + 1 - _aisin.D09_START
            and _aisin.base_09d(raw) == _aisin.D09_START
            and _accept(raw, "plain", require_cksum=False)):
        return

    # 2) ZF 6HP BCB/MILKYWAY -- marker located by search (see module
    #    docstring: a plaintext provenance header can precede it). The
    #    header's checksum16 field is NOT used to gate this scheme: it does
    #    not match real BCB-decoded content (checked against real files --
    #    same conclusion :func:`ovtsuite.flash.crypto.zf6hp_bcb_decode`'s own
    #    docstring already reached for the format's internal terminal
    #    checksum record, which also doesn't follow the naive additive-sum
    #    formula the .sgo unpacker tool's docs claim). Exact declared-length
    #    agreement (BCB decoding is self-terminating and length-sensitive --
    #    a wrong key/offset reliably produces a malformed-record error long
    #    before landing on the exact right total by chance) plus the
    #    container-level structural cross-check in ``_cross_check_bcb``
    #    (real ZF ident block / shift-schedule structures actually present)
    #    stand in for it instead.
    #    What DOES validate a BCB area is the 24-bit sum its own provenance
    #    header states -- the ZF dialect's ``[CHECKSUM]`` block, the EDC15
    #    dialect's trailing hex (_bcb1.header_sum reads both). Verified on
    #    116/116 real ZF 6HP BCB sections. This matters for more than rigour:
    #    both dialects declare "BCB Type1", and the MILKYWAY key decodes an
    #    EDC15 area to records whose lengths come out EXACTLY right with
    #    garbage content, so length agreement alone would silently accept the
    #    wrong decode. An area with no stated sum falls back to the old
    #    length-plus-structural-cross-check behaviour rather than being
    #    rejected.
    idx = raw.find(_crypto.ZF6HP_BCB_MARKER)
    if idx != -1:
        stated = _bcb1.header_sum(raw[:idx])
        try:
            decoded = _crypto.zf6hp_bcb_decode(raw[idx:])
        except ValueError:
            decoded = None
        if decoded is not None and (stated is None
                                    or (sum(decoded) & 0xFFFFFF) == stated):
            if _accept(decoded, "bcb", raw[:idx], require_cksum=False):
                return

    # 3) EDC16 transferData cipher (see module docstring) -- keyed by
    #    LENGTH only, not by the section's declared address, but a
    #    section's declared address can itself be a stand-in for a
    #    different real flash offset (the EDC16U1 whole-image mirror
    #    convention -- see _real_span), so always decode at the REAL
    #    offset.
    if len(raw) == sec.declared_len:
        real_addr, real_end = _real_span(sec)
        ks = _crypto.keystream(real_addr, real_end)
        decoded = bytes(a ^ b for a, b in zip(raw, ks))
        if _accept(decoded, "edc16"):
            return

    # Nothing matched -- leave unrecognized rather than guess.
    sec.plaintext = None
    sec.scheme = None


def _accept_bcb1(s: "SgoSection", key: bytes, ck16_kind: str) -> bool:
    """Try one full BCB Type1 key on section ``s``; set it decoded and return
    True only if every check the format itself defines passes.

    ``ck16_kind`` selects how the section-table 16-bit field (+0x13) is
    validated, because the two dialects populate it differently:
      - "sum":   low 16 bits of the additive byte sum (EDC15 dialect)
      - "crc32": low 16 bits of ``zlib.crc32`` of the plaintext (ZF 5HP
                 ``GEHEIM`` dialect -- verified on real 5HP19 files)
    The terminal 24-bit sum (inside :func:`ovtsuite.bcb1.decode`) and the
    provenance header's restated sum are checked for both."""
    prefix, body = _bcb1.split(_xorff(s.blob))
    plain = _bcb1.decode(body, s.declared_len, key)
    if plain is None:
        return False
    stated = _bcb1.header_sum(prefix)
    if stated is not None and (sum(plain) & 0xFFFFFF) != stated:
        return False
    ck16 = struct.unpack_from(">H", s.header, _CKSUM16_OFF)[0]
    if ck16:
        got = (zlib.crc32(plain) & 0xFFFF) if ck16_kind == "crc32" \
            else (sum(plain) & 0xFFFF)
        if got != ck16:
            return False
    s.plaintext = plain
    s.scheme = "bcb1"
    s.bcb_prefix = prefix
    s.bcb1_key = key
    s.bcb1_ck16 = ck16_kind
    return True


def _decode_bcb1(sections: List["SgoSection"]) -> None:
    """Decode a container's Bosch BCB Type1 areas (EDC15 and ZF 5HP dialects).

    Two dialects share the record format and differ only in the key and in how
    the section's 16-bit field is populated:

      - **ZF 5HP** (VW "01V"/"01L"): the fixed 6-byte key ``GEHEIM``
        (:data:`ovtsuite.bcb1.KEY_5HP`), nothing to solve; its 16-bit field is
        a CRC-32 low-16. Tried first because it needs no solving.
      - **EDC15**: an 11-byte key whose four per-dataset bytes are stored
        nowhere, so they are solved from the areas themselves
        (:func:`ovtsuite.bcb1.solve_key`); its 16-bit field is an additive sum.

    Both are confirmed by values the format itself defines -- the area's
    terminal 24-bit sum, the same sum restated in the provenance header, and
    the section's 16-bit field where populated. An area failing any check is
    left unrecognized rather than decoded with a guess."""
    pend = [s for s in sections
            if s.scheme is None and _bcb1.looks_like(_xorff(s.blob))]
    if not pend:
        return
    # ZF 5HP: fixed GEHEIM key, CRC-32 low-16 field.
    pend = [s for s in pend if not _accept_bcb1(s, _bcb1.KEY_5HP, "crc32")]
    if not pend:
        return
    # EDC15: solve the 11-byte key from the remaining areas.
    bodies = [(_bcb1.split(_xorff(s.blob))[1], s.declared_len) for s in pend]
    key = _bcb1.solve_key(bodies)
    if key is None:
        return
    for s in pend:
        if not _accept_bcb1(s, key, "sum"):
            # The KEY.BC4 area carries its own fixed key.
            _accept_bcb1(s, _bcb1.KEY_BC4, "sum")


def parse(data: bytes, name: str = "") -> SgoContainer:
    """Structural parse of a v2 SGO container, decoding every section it can
    (self-verified, see ``_decode_section``) and carrying the rest through
    losslessly for an exact round-trip."""
    if not detect(data):
        raise ValueError("not a v2 SGML Object File (bad magic)")
    ms = _u32le(data, 0x29)
    ml = _u32le(data, ms)
    sgolen = _u32le(data, 0x2D)
    first = ms + ml + 4
    prefix = bytes(data[:first])
    sections = []
    bs = first
    i = 0
    while bs < sgolen:
        addr = _u24be(data, bs)
        crypt = data[bs + 3]
        declared_len = _u24be(data, bs + 4)
        slen = _u32le(data, bs + _SLEN_OFF)
        header = bytes(data[bs:bs + _HDR_LEN])
        blob = bytes(data[bs + _HDR_LEN: bs + _HDR_LEN + slen])
        sec = SgoSection(index=i, off=bs, addr=addr, crypt=crypt,
                          declared_len=declared_len, header=header, blob=blob,
                          lo=_u24be(data, bs + _RANGE_LO_OFF),
                          hi=_u24be(data, bs + _RANGE_HI_OFF))
        _decode_section(sec)
        sections.append(sec)
        bs += _HDR_LEN + slen
        i += 1
    trailer = bytes(data[sgolen:])
    _decode_bcb1(sections)
    needs = "" if _cross_check_edc16(sections) else "edc16"
    _cross_check_bcb(sections)
    _resolve_overlaps(sections)
    return SgoContainer(prefix=prefix, sections=sections, trailer=trailer, name=name,
                        needs_module=needs)


def _cross_check_bcb(sections: List["SgoSection"]) -> None:
    """"bcb"-scheme sections have no reliable per-section checksum field to
    self-verify against (see ``_decode_section``), so cross-check the
    assembled image structurally instead, the same way the anomalous 3-file
    group was told apart from real ZF 6HP calibration data earlier in this
    project: a real ZF ident block or a run of shift-schedule structures
    (:func:`ovtsuite.zf6hp.detect`) actually present. Demote every "bcb"
    section in this container if the assembled content doesn't look like
    real ZF 6HP calibration data."""
    bcb_secs = [s for s in sections if s.scheme == "bcb"]
    if not bcb_secs:
        return
    lo = min(s.addr for s in bcb_secs)
    hi = max(s.end for s in bcb_secs)
    full = bytearray(b"\xFF" * (hi - lo))
    for s in bcb_secs:
        full[s.addr - lo:s.end - lo] = s.plaintext
    try:
        ok = _zf6hp.detect(bytes(full))
    except Exception:
        ok = False
    if not ok:
        for s in bcb_secs:
            s.plaintext = None
            s.scheme = None
            s.bcb_prefix = b""


def _demote(secs) -> None:
    for s in secs:
        s.plaintext = None
        s.scheme = None
        s.bcb_prefix = b""


def _cross_check_edc16(sections: List["SgoSection"]) -> bool:
    """The per-section checksum16 self-check (see ``_decode_section``) is
    only 16 bits -- empirically NOT always enough on its own: the "edc16"
    (address-keyed transferData cipher) scheme has been shown, on a real
    file, to pass that check while decoding to content that disagrees with
    an independently-verified reference. Wherever at least one section in
    this container used the "edc16" scheme, cross-check the assembled image
    against the EDC16 module's checksum -- OVT-Suite's own, separately
    implemented EDC16 flash-checksum verifier (not something this module
    invents) -- and demote EVERY "edc16" section back to unrecognized if it
    doesn't pass.

    Without the EDC16 module there is nothing to vouch for the decode, so the
    "edc16" sections are demoted too and False is returned (the container
    records that it needs the module); True otherwise. This is what currently separates the 2 MB EDC16U31 family
    (passes; independently confirmed against real reference binaries in
    testing) from the 1 MB EDC16U1 family (the self-check alone accepts a
    wrong decode there; this cross-check catches and rejects it)."""
    edc16_secs = [s for s in sections if s.scheme == "edc16"]
    if not edc16_secs:
        return True
    from .modules import registry as _registry
    cs = _registry.checksum("edc16")
    if cs is None:
        _demote(edc16_secs)
        return False
    # Only the standard main-flash address window is checkable this way --
    # a section OUTSIDE it (e.g. the internal-flash range some EDC16U31
    # containers carry near 0x404000, a different address space entirely,
    # see project memory on .mpc/internal-flash reads) has no independent
    # ground truth to cross-check against here and is left on its own
    # self-check result, unchanged. Real (remapped) spans throughout -- see
    # _real_span -- so the EDC16U1 whole-image mirror section lands here
    # too, not just sections whose declared address is already real.
    spans = [(s,) + _real_span(s) for s in edc16_secs]
    main = [(s, ra, re_) for s, ra, re_ in spans if re_ <= max(_EDC16_SIZES)]
    if not main:
        return True
    size = min(sz for sz in _EDC16_SIZES if sz >= max(re_ for _, _, re_ in main))
    full = bytearray(b"\xFF" * size)
    for s, ra, re_ in main:
        full[ra:re_] = s.plaintext
    ok = False
    try:
        if cs.is_supported(bytes(full)):
            ok = cs.verify(bytes(full)).ok
    except Exception:
        ok = False
    if not ok:
        # This container's checksum16 self-check has now been shown
        # unreliable (it accepted a wrong "main" decode) -- don't leave any
        # OTHER "edc16" section in the same file trusted on that same weak
        # basis, checkable or not.
        _demote(edc16_secs)
    return True


#: Sections beyond this address are a different address space in the same
#: physical part (observed: an EDC16U31 "internal flash" region mapped near
#: 0x404000, already handled as its own separate artifact -- a .mpc file --
#: everywhere else in this project) rather than more of the main flash
#: image. Reuses ``ovtsuite.zf6hp``'s own already-established ceiling for
#: what a single ZF 6HP image can span, rather than inventing a new number.
MAIN_IMAGE_MAX = 0x400000

#: EDC16's own fixed image sizes (matches the same convention already used
#: by ``ovtsuite.cli``'s "EDC16U31 (2MB)"/"EDC16U1 (1MB)" variant check, and
#: by ``ovtsuite.edc16.checksum16.is_supported``, which requires an EXACT match --
#: a real full flash dump is always exactly one of these, so an "edc16"
#: image built from sections that fall short of one (a trailing few KB some
#: .sgo containers simply don't carry a section for) is padded up to it in
#: ``image()`` rather than left short. ZF 6HP has no equivalent fixed-size
#: convention (its own reference extracts are various sizes) and is never
#: padded this way.
_EDC16_SIZES = (0x100000, 0x200000)

#: Some EDC16U1 (1 MB) .sgo containers carry a section declared at this
#: address whose edc16-decoded content is a byte-for-byte duplicate of the
#: ENTIRE real image starting at flash offset 0 -- NOT a separate address
#: space like MAIN_IMAGE_MAX's own 2 MB internal-flash convention above.
#: Verified against every (.sgo, reference .bin) pair in the project's
#: whole EDC16U1 catalogue that carries a section here: 52/52 files,
#: byte-exact, zero exceptions, using the SAME already-shipped edc16 cipher
#: -- see the module docstring's "edc16" bullet for the full story.
_U1_MIRROR_BASE = 0x800000


#: EDC15 (C167) maps its 512 KB external flash at this address, so an EDC15
#: area's file offset is ``lo - _EDC15_FLASH_BASE``. Taken from the section
#: header's own real-range fields: the main area is always declared
#: 0x080000-0x0FBFFF, i.e. offset 0 for 0x7C000 bytes, and the trailing
#: 0x4000 (the boot sector) is never carried by these containers.
_EDC15_FLASH_BASE = 0x80000
_EDC15_SIZE = 0x80000


def _image_span(sec: "SgoSection"):
    """``(start, end)`` of a section inside the flat main image, or None if
    it does not belong in that image at all.

    EDC15 containers carry, besides the flash area, three small
    0x00E000-based auxiliary/dataset-descriptor areas that overlap each other
    and are NOT part of the flash image -- the same relationship the EDC16U31
    internal-flash region already has. They must be excluded explicitly
    rather than placed at their own low addresses, which would corrupt the
    image."""
    if sec.scheme == "bcb1":
        if sec.bcb1_key == _bcb1.KEY_5HP:
            # ZF 5HP: the single area sits at its literal flash address in a
            # 512 KiB image (file offset == address, e.g. 0x8000..0x7FFFD);
            # the boot sector below it is not carried and stays fill.
            return sec.lo, sec.lo + sec.declared_len
        if sec.lo >= _EDC15_FLASH_BASE and sec.hi < _EDC15_FLASH_BASE + _EDC15_SIZE:
            base = sec.lo - _EDC15_FLASH_BASE
            return base, base + sec.declared_len
        return None
    return _real_span(sec)


def _real_span(sec: "SgoSection"):
    """(real_addr, real_end): a section's actual flash address range, after
    undoing the EDC16U1 whole-image-mirror convention (_U1_MIRROR_BASE) if
    this section matches it. Every other section's real span is just
    (sec.addr, sec.end) -- its declared address already IS its real flash
    address."""
    if sec.addr >= _U1_MIRROR_BASE:
        base = sec.addr - _U1_MIRROR_BASE
        if base + sec.declared_len <= max(_EDC16_SIZES):
            return base, base + sec.declared_len
    return sec.addr, sec.end


def _resolve_overlaps(sections: List["SgoSection"]) -> None:
    """A successfully-decoded whole-image mirror section (_U1_MIRROR_BASE)
    is independently cross-checked ground truth (_cross_check_edc16) for
    its ENTIRE covered range. Demote any OTHER section whose real span
    falls inside it, regardless of that section's own self-check result:
    empirically (52/52 real EDC16U1 files), such sections do not actually
    decode correctly under any scheme this module knows -- a wrong "edc16"
    attempt there matches only ~8% of real bytes despite passing its own
    16-bit checksum, and the whole-container cross-check does not by
    itself catch that here because the mirror section is later in file
    order and silently overwrites the wrong bytes when the two are merged
    into one image. Left untouched, the smaller section's bad plaintext
    would still surface in the GUI/CLI and in apply_image's re-encode
    path."""
    mirrors = [s for s in sections if s.scheme is not None and s.addr >= _U1_MIRROR_BASE]
    if not mirrors:
        return
    for m in mirrors:
        m_addr, m_end = _real_span(m)
        for s in sections:
            if s is m or s.scheme is None or s.addr >= _U1_MIRROR_BASE:
                continue
            if m_addr <= s.addr and s.end <= m_end:
                s.plaintext = None
                s.scheme = None
                s.bcb_prefix = b""


def image(container: SgoContainer, fill: int = 0xFF, max_size: int = MAIN_IMAGE_MAX):
    """Flatten the main-flash address range into one flat, 0-based byte
    image -- matching how every OTHER binary this project loads is
    addressed (file offset == target flash address, starting at 0), so the
    result plugs directly into :class:`ovtsuite.binary.BinaryFile` and
    everything built on it (family detection, checksum verification, map
    scanning) unchanged.

    Returns ``(0, data)`` for symmetry with :func:`apply_image` (which takes
    the same base back). A gap -- before the first section, between
    sections, or a section with no recognized scheme -- is left as ``fill``
    bytes; ``container.unrecognized`` lists the sections responsible for the
    latter. Any section beyond ``max_size`` (a different address space, not
    a gap in this one -- see ``MAIN_IMAGE_MAX``) is left out of the image
    entirely, not merged in at a huge offset."""
    entries = []
    for s in container.sections:
        span = _image_span(s)
        if span is None:
            continue
        ra, re_ = span
        if re_ <= max_size:
            entries.append((s, ra, re_))
    if not entries:
        return 0, b""
    hi = max(re_ for _, _, re_ in entries)
    if any(s.scheme == "bcb1" for s, _, _ in entries):
        hi = max(hi, _EDC15_SIZE)
    if any(s.scheme == "edc16" for s, _, _ in entries):
        covering = [sz for sz in _EDC16_SIZES if sz >= hi]
        if covering:
            hi = min(covering)
    buf = bytearray(bytes([fill & 0xFF]) * hi)
    for s, ra, re_ in entries:
        if s.plaintext is not None:
            buf[ra:re_] = s.plaintext
    return 0, bytes(buf)


def _encode_section(sec: SgoSection, plaintext: bytes) -> bytes:
    """Inverse of ``_decode_section`` for one section's chosen scheme.
    Raises ``ValueError`` for an unrecognized-scheme section -- edits to a
    region OVT-Suite couldn't decode can't be re-encoded either."""
    if sec.scheme == "plain":
        return _xorff(plaintext)
    if sec.scheme == "bcb":
        body = _crypto.zf6hp_bcb_encode(plaintext)
        # The area's provenance header restates its 24-bit sum, which
        # _decode_section now validates, so it has to be refreshed too.
        prefix = _bcb1.set_header_sum(sec.bcb_prefix, sum(plaintext))
        return _xorff(prefix + body)
    if sec.scheme == "edc16":
        real_addr, _ = _real_span(sec)
        ks = _crypto.keystream(real_addr, real_addr + len(plaintext))
        return _xorff(bytes(a ^ b for a, b in zip(plaintext, ks)))
    if sec.scheme == "bcb1":
        body = _bcb1.encode(plaintext, sec.bcb1_key)
        # The area's provenance header restates its 24-bit sum; a genuine file
        # always has the two agree, so refresh it or the area fails its own
        # validation on the next parse.
        prefix = _bcb1.set_header_sum(sec.bcb_prefix, sum(plaintext))
        return _xorff(prefix + _bcb1.MARKER + body)
    raise ValueError(
        "section %d at 0x%06X uses an unrecognized container scheme "
        "(crypt=0x%02X) -- %s cannot re-encode edits to it" %
        (sec.index, sec.addr, sec.crypt, _branding.APP_NAME))


def apply_image(container: SgoContainer, base_addr: int, data: bytes) -> SgoContainer:
    """Return a NEW :class:`SgoContainer` with each section's plaintext
    refreshed from ``data`` (a flat image as returned by :func:`image`,
    based at ``base_addr``). A section whose bytes are unchanged from what
    was decoded keeps its ORIGINAL stored blob verbatim (byte-identical);
    only a section whose plaintext actually differs is re-encoded, so a
    save with no edits reproduces the source file exactly and a save with
    edits touches only the sections that changed."""
    new_sections = []
    for s in container.sections:
        span = _image_span(s)
        if span is None:
            new_sections.append(s)
            continue
        real_addr, real_end = span
        lo, hi = real_addr - base_addr, real_end - base_addr
        current = bytes(data[lo:hi]) if 0 <= lo and hi <= len(data) else None
        if current is None or s.plaintext is None or current == s.plaintext:
            new_sections.append(s)
            continue
        blob = _encode_section(s, current)
        header = bytearray(s.header)
        ck16 = (zlib.crc32(current) & 0xFFFF) if s.bcb1_ck16 == "crc32" \
            else (sum(current) & 0xFFFF)
        struct.pack_into(">H", header, _CKSUM16_OFF, ck16)
        struct.pack_into("<I", header, _SLEN_OFF, len(blob))
        new_sections.append(SgoSection(
            index=s.index, off=s.off, addr=s.addr, crypt=s.crypt,
            declared_len=s.declared_len, header=bytes(header), blob=blob,
            scheme=s.scheme, plaintext=current, bcb_prefix=s.bcb_prefix,
            lo=s.lo, hi=s.hi, bcb1_key=s.bcb1_key, bcb1_ck16=s.bcb1_ck16))
    return SgoContainer(prefix=container.prefix, sections=new_sections,
                         trailer=container.trailer, name=container.name)


def replace_section(container: SgoContainer, addr: int, plaintext: bytes) -> SgoContainer:
    """Return a NEW :class:`SgoContainer` with the single section at flash
    ``addr`` re-encoded from ``plaintext`` and every other section carried
    through byte-for-byte. This is :func:`apply_image`'s per-section update
    applied to one addressed section, for a region (the MPC internal flash
    at 0x404000) that lies outside the 0-based flat image apply_image works
    from. ``plaintext`` must match the section's current decoded length --
    a raw byte edit never changes it -- and the section header's decoded
    checksum16 and stored length are refreshed so the result decodes and
    verifies exactly as a genuine file would."""
    new_sections = []
    for s in container.sections:
        if (s.addr == addr and s.plaintext is not None
                and len(plaintext) == len(s.plaintext)):
            blob = _encode_section(s, plaintext)
            header = bytearray(s.header)
            ck16 = (zlib.crc32(bytes(plaintext)) & 0xFFFF) if s.bcb1_ck16 == "crc32" \
                else (sum(plaintext) & 0xFFFF)
            struct.pack_into(">H", header, _CKSUM16_OFF, ck16)
            struct.pack_into("<I", header, _SLEN_OFF, len(blob))
            new_sections.append(SgoSection(
                index=s.index, off=s.off, addr=s.addr, crypt=s.crypt,
                declared_len=s.declared_len, header=bytes(header), blob=blob,
                scheme=s.scheme, plaintext=bytes(plaintext), bcb_prefix=s.bcb_prefix,
                lo=s.lo, hi=s.hi, bcb1_key=s.bcb1_key, bcb1_ck16=s.bcb1_ck16))
        else:
            new_sections.append(s)
    return SgoContainer(prefix=container.prefix, sections=new_sections,
                        trailer=container.trailer, name=container.name)


def to_bytes(container: SgoContainer) -> bytes:
    """Rebuild the full .sgo file.

    Section offsets shift whenever a re-encoded blob changes length (BCB
    compression is not length-stable), so three size-dependent fields are
    rebuilt rather than carried through:

    * the trailer, which is the container's own section index -- ``u32 LE``
      section count followed by one ``u32 LE`` absolute file offset per
      section. Verified against 14 real containers spanning all three
      platforms (EDC15, EDC16U31/U1, ZF 6HP): the stored offsets equal the
      real section offsets exactly in every one.
    * ``sgolen`` at 0x2D, the end of the section table, which is also the
      only size-dependent field anywhere in the prefix (checked by scanning
      the whole prefix for u32s matching any file length).
    * the file integrity dword at 0x15.

    With those three rebuilt, a length-changing save is structurally sound,
    so editing a compressed ("bcb" or "bcb1") section is no longer limited
    to length-preserving changes.
    """
    out = bytearray(container.prefix)
    offsets = []
    for s in container.sections:
        offsets.append(len(out))
        out += s.header
        out += s.blob
    sgolen = len(out)
    trailer = bytearray(container.trailer)
    if len(trailer) >= 4 + 4 * len(offsets):
        struct.pack_into("<I", trailer, 0, len(offsets))
        for i, off in enumerate(offsets):
            struct.pack_into("<I", trailer, 4 + 4 * i, off)
    out += trailer
    if len(out) > 0x31:
        struct.pack_into("<I", out, 0x2D, sgolen)
    total = sum(out) - sum(out[_FILE_CKSUM_OFF:_FILE_CKSUM_OFF + 4])
    struct.pack_into("<I", out, _FILE_CKSUM_OFF, (total - 1) & 0xFFFFFFFF)
    return bytes(out)
