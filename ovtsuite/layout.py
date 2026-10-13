# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ECU's own calibration index: the parameter-block pointer table.

Every EDC16 calibration block (the last 512 KB of the image, which is also
exactly what an OBD/K-line read returns) starts with a Bosch block header and,
right after it, a ``0xCAFEAFFE`` descriptor::

    CAFEAFFE  tail  tail2  00000108 00010108 <id> 00000000
    default_lo default_hi active data_lo data_lo tail 0 0 <flags>

``default_lo..default_hi`` is an array of N 32-bit addresses in the *program*
area (0x4B998 on P350): the linker's list of every calibration block the code
addresses through a base pointer, in ascending address order.  ``active`` is
the working copy of the same array inside the calibration (0x180084), where
coding can redirect individual entries into the secondary (variant) block.
N is 372 on P350 and 394-398 on P475; the blocks tile the calibration from
the first entry to the last.

Because the code reaches every parameter as *block base + fixed
displacement*, a block whose length is unchanged between two binaries has an
unchanged internal layout by construction -- so aligning a target's pointer
table to the family reference's gives an **exact** placement for everything
inside the aligned, equal-length blocks, with no byte comparison at all.
That is what turns "projected" placements (recalibrated values the byte
matcher can't confirm) into placements the ECU itself vouches for, and it is
available on a bare 512 KB calibration read because the active array lives
inside it.

The *default* array (needed to resolve redirected entries exactly, and for
variant.py's exact pointer-array diff) is only present in a full read.
``tools/generate_layouts.py`` therefore catalogues every full reference
binary at build time -- its Bosch software number, dataset id, part numbers
and complete pointer table -- into ``data/layouts.json.gz`` so a calibration
read can be tied to the full binary it came from (same software number and
identical table) and borrow that knowledge.
"""
import bisect
import difflib
import gzip
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

_MAGIC = b"\xCA\xFE\xAF\xFE"
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CATALOG = os.path.join(_DATA_DIR, "layouts.json.gz")
CAL_SIZE = 0x80000            # what a K-line read returns: the last 512 KB
MIN_RUN = 3                   # consecutive equal block lengths needed to pair
MIN_PAIRED = 0.5              # fraction of reference entries an alignment
                              # must pair before it is trusted at all


def _u32(d, o):
    return struct.unpack_from(">I", d, o)[0]


@dataclass
class Descriptor:
    addr: int
    default_lo: int
    default_hi: int
    active: int
    data_lo: int              # first address of the secondary/variant area
    count: int

    @property
    def active_end(self):
        return self.active + 4 * self.count


def cal_start(data: bytes) -> int:
    """Start of the last 512 KB (the calibration block a read contains)."""
    return max(0, len(data) - CAL_SIZE)


def address_base(data: bytes) -> int:
    """CPU address of file offset 0, from the calibration block's own header
    (``start end FADECAFE`` at +0x34): 0 on the 2 MB EDC16U31 (flash at 0),
    0x800000 on the 1 MB EDC16U1, whose pointer tables hold CPU addresses."""
    lo = cal_start(data)
    if lo + 0x40 <= len(data) and _u32(data, lo + 0x3C) == 0xFADECAFE:
        start = _u32(data, lo + 0x34)
        if start >= lo and (start - lo) % 0x10000 == 0:
            return start - lo
    return 0


def find_descriptor(data: bytes) -> Optional[Descriptor]:
    """The descriptor of the calibration block, with every address already
    translated to a file offset."""
    lo = cal_start(data)
    base = address_base(data)
    i = data.find(_MAGIC, lo)
    if i < 0 or i + 0x40 > len(data):
        return None
    dlo, dhi, act, data_lo = (_u32(data, i + 0x1C) - base, _u32(data, i + 0x20) - base,
                              _u32(data, i + 0x24) - base, _u32(data, i + 0x28) - base)
    n = (dhi - dlo) // 4
    if not (0 < n < 4096 and 0 <= dlo < dhi and lo <= act < len(data)
            and act + 4 * n <= len(data)):
        return None
    return Descriptor(i, dlo, dhi, act, data_lo, n)


@dataclass
class PointerTable:
    """The table's entries as *primary* (main-block) addresses.

    ``primaries[k]`` is None where only the active array was available and
    entry k is redirected into the secondary block (its primary is unknown
    without the default array); ``active[k]`` is always the working copy."""
    active: List[int]
    primaries: List[Optional[int]]
    from_default: bool        # primaries came from the linker's default array

    @property
    def count(self):
        return len(self.active)

    @property
    def redirected(self):
        return sum(1 for a, p in zip(self.active, self.primaries) if a != p)

    @property
    def known(self):
        return [p for p in self.primaries if p is not None]


def pointer_table(data: bytes, main_lo: Optional[int] = None) -> Optional[PointerTable]:
    """Read the pointer table; the default array is used for primaries when
    it is present (full read), else redirected active entries become None."""
    desc = find_descriptor(data)
    if desc is None:
        return None
    lo = cal_start(data)
    base = address_base(data)
    active = [_u32(data, desc.active + 4 * k) - base for k in range(desc.count)]
    active_ok = all(lo <= a < len(data) for a in active)
    have_default = (desc.default_hi <= len(data)
                    and any(b != 0xFF for b in data[desc.default_lo:desc.default_lo + 16]))
    if have_default:
        prim = [_u32(data, desc.default_lo + 4 * k) - base for k in range(desc.count)]
        if all(lo <= p < len(data) for p in prim) and all(
                prim[k] < prim[k + 1] for k in range(len(prim) - 1)):
            # A dataset with no coding overlay leaves the active (coding-selected)
            # array blank (0xFFFFFFFF) and the ECU uses the default array directly;
            # treat default as active so placement still resolves exactly.
            if not active_ok:
                active = list(prim)
            return PointerTable(active, prim, True)
    if not active_ok:
        return None
    # active only: everything below the main block is a redirect
    if main_lo is None:
        main_lo = desc.data_lo
    known = [a for a in active if a >= main_lo]
    # a primary list must be ascending; redirected entries are the exceptions
    prim: List[Optional[int]] = []
    last = -1
    for a in active:
        if a >= main_lo and a > last:
            prim.append(a)
            last = a
        else:
            prim.append(None)
    if len([p for p in prim if p is not None]) < len(known) // 2:
        return None
    return PointerTable(active, prim, False)


# ---------------------------------------------------------------------------
@dataclass
class Block:
    index: int                # reference table index
    ref_lo: int
    ref_hi: int
    tgt_lo: int

    @property
    def delta(self):
        return self.tgt_lo - self.ref_lo

    @property
    def length(self):
        return self.ref_hi - self.ref_lo


@dataclass
class Alignment:
    """Reference table <-> target table, block by block."""
    blocks: List[Block] = field(default_factory=list)     # exact, equal-length
    base_anchors: List[Block] = field(default_factory=list)  # position-paired
                                    # block bases (length changed internally):
                                    # valid base+displacement anchors, but only
                                    # near the block start -- not span-trusted
    paired: int = 0                # entries aligned (incl. changed-length ones)
    ref_count: int = 0
    tgt_count: int = 0
    tgt_redirected: int = 0
    identical: bool = False        # every entry equal: same layout outright
    _starts: List[int] = field(default_factory=list, repr=False)

    def finish(self):
        self.blocks.sort(key=lambda b: b.ref_lo)
        self._starts = [b.ref_lo for b in self.blocks]
        return self

    def block_at(self, ref_addr: int, span: int = 1) -> Optional[Block]:
        """The exact block containing [ref_addr, ref_addr+span), if any."""
        i = bisect.bisect_right(self._starts, ref_addr) - 1
        if i < 0:
            return None
        b = self.blocks[i]
        return b if ref_addr + max(span, 1) <= b.ref_hi else None

    @property
    def covered(self) -> int:
        return sum(b.length for b in self.blocks)

    @property
    def span(self) -> int:
        return (self.blocks[-1].ref_hi - self.blocks[0].ref_lo) if self.blocks else 0


def _tokens(prim: Sequence[Optional[int]]):
    """Per entry: the gap to the next known primary (block length), or 'R'
    for a redirected entry whose primary is unknown."""
    out = []
    n = len(prim)
    for k in range(n):
        if prim[k] is None:
            out.append("R")
            continue
        j = k + 1
        while j < n and prim[j] is None:
            j += 1
        out.append(prim[j] - prim[k] if j < n else "END")
    return out


def _pair(ref, tgt) -> Dict[int, int]:
    tr, tt = _tokens(ref), _tokens(tgt)
    sm = difflib.SequenceMatcher(None, tr, tt, autojunk=False)
    pairs: Dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        # a run of fewer than MIN_RUN equal block lengths can coincide by
        # chance (2- and 4-byte blocks are common); only longer runs pair
        if tag == "equal" and i2 - i1 >= MIN_RUN:
            for k in range(i2 - i1):
                pairs[i1 + k] = j1 + k
    return pairs


def _fill_redirects(ref, tgt, pairs):
    """Recover the primaries of redirected entries: a stretch between two
    paired entries that holds the same number of entries and spans the same
    number of bytes on both sides is structurally identical, so its holes
    take the reference's spacing."""
    tgt = list(tgt)
    items = sorted(pairs.items())
    if not items:
        return tgt, 0
    filled = 0
    # stretches between neighbouring pairs, plus the head (before the first
    # pair) and the tail (after the last), each with the same entry count on
    # both sides; a stretch is identical when every KNOWN target entry sits
    # at the reference's spacing from the pair, so its holes take it too
    stretches = []
    ia0, ja0 = items[0]
    if ia0 == ja0 > 0:
        stretches.append((ia0, ja0, range(0, ja0)))
    for (ia, ja), (ib, jb) in zip(items, items[1:]):
        if ib - ia == jb - ja and ib - ia >= 2:
            stretches.append((ia, ja, range(ja + 1, jb)))
    ian, jan = items[-1]
    if len(ref) - ian == len(tgt) - jan > 1:
        stretches.append((ian, jan, range(jan + 1, len(tgt))))
    for ia, ja, rng in stretches:
        if not any(tgt[j] is None for j in rng):
            continue
        if all(tgt[j] is None or tgt[j] - tgt[ja] == ref[ia + (j - ja)] - ref[ia]
               for j in rng):
            for j in rng:
                if tgt[j] is None:
                    tgt[j] = tgt[ja] + (ref[ia + (j - ja)] - ref[ia])
                    filled += 1
    return tgt, filled


def align(ref: Sequence[int], tgt: Sequence[Optional[int]]) -> Alignment:
    """Align the reference's primaries to a target's (possibly redirect-holed)
    primaries by block-length sequence; equal-length aligned neighbours give
    exact blocks."""
    al = Alignment(ref_count=len(ref), tgt_count=len(tgt),
                   tgt_redirected=sum(1 for p in tgt if p is None))
    pairs = _pair(ref, tgt)
    if al.tgt_redirected:
        tgt, n = _fill_redirects(ref, tgt, pairs)
        if n:
            pairs = _pair(ref, tgt)
    if list(ref) == list(tgt):
        al.identical = True
        for k in range(len(ref) - 1):
            al.blocks.append(Block(k, ref[k], ref[k + 1], ref[k]))
        al.paired = len(ref)
        return al.finish()
    tr, tt = _tokens(ref), _tokens(tgt)
    al.paired = len(pairs)
    exact_refs = set()
    for i, j in pairs.items():
        if i + 1 >= len(ref) or tgt[j] is None:
            continue
        # equal token == equal gap to the next known entry on both sides
        if tr[i] == tt[j] and isinstance(tr[i], int):
            al.blocks.append(Block(i, ref[i], ref[i + 1], tgt[j]))
            exact_refs.add(ref[i])

    # position-pair the blocks that sit BETWEEN aligned pairs: when the same
    # number of (length-changed) blocks lies between two paired entries on both
    # sides, they pair 1:1 by index. The code reaches every parameter as
    # block base + fixed displacement, so a changed block's base is still an
    # exact anchor -- entities near its start place correctly even though the
    # block's overall length changed (the change is internal / at its tail).
    # Stored separately from `blocks`: trusted only as a base anchor, never as
    # a span for grading, since the internal step location is unknown.
    def _base(i, j):
        if i + 1 < len(ref) and 0 <= j < len(tgt) and tgt[j] is not None \
                and ref[i] not in exact_refs:
            al.base_anchors.append(Block(i, ref[i], ref[i + 1], tgt[j]))
    items = sorted(pairs.items())
    if items:
        ia0, ja0 = items[0]
        if ia0 == ja0 and ia0 > 0:                       # head stretch
            for k in range(ia0):
                _base(k, k)
        for (ia, ja), (ib, jb) in zip(items, items[1:]):
            if ib - ia == jb - ja and ib - ia >= 2:      # equal count between
                for k in range(1, ib - ia):
                    _base(ia + k, ja + k)
        ian, jan = items[-1]
        if len(ref) - ian == len(tgt) - jan and len(ref) - ian > 1:   # tail
            for k in range(1, len(ref) - ian):
                _base(ian + k, jan + k)
    return al.finish()


# ---------------------------------------------------------------------------
@dataclass
class Layout:
    """One catalogued full binary."""
    family: str
    file: str
    sw: str                   # 10-digit Bosch software number
    dataset: str
    hw_part: str
    sw_part: str
    region: List[int]
    primaries: List[int]
    reference: bool = False   # the family's KB reference binary


_catalog_cache: Optional[List[Layout]] = None


def catalog() -> List[Layout]:
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = []
        from . import datafiles
        cat = datafiles.path(os.path.basename(_CATALOG))  # EDC16 module data
        if cat is not None:
            with gzip.open(cat, "rt", encoding="utf-8") as fh:
                for r in json.load(fh):
                    _catalog_cache.append(Layout(**r))
    return _catalog_cache


def reference_layout(family: str) -> Optional[Layout]:
    for l in catalog():
        if l.family == family and l.reference:
            return l
    return None


def identify(data: bytes, family: Optional[str] = None):
    """(layout, identical) for a target: the catalogued full binary with the
    same Bosch software number whose pointer table matches, else the closest
    one by aligned entries; identical=True when the table is equal outright."""
    from .fileinfo import ident16
    pt = pointer_table(data)
    if pt is None:
        return None, False
    info = ident16(data)
    sw = info.get("bosch_sw")
    best, best_score, identical = None, -1, False
    for l in catalog():
        if family and l.family != family:
            continue
        al = align(l.primaries, pt.primaries)
        score = al.paired + (1000 if l.sw == sw else 0)
        if al.identical:
            score += 5000
        if score > best_score:
            best, best_score, identical = l, score, al.identical
    return best, identical


def align_to_reference(family: str, data: bytes, main_lo: Optional[int] = None):
    """Alignment of ``data``'s pointer table to the family reference's, or
    None when either table is unavailable."""
    ref = reference_layout(family)
    if ref is None:
        return None
    pt = pointer_table(data, main_lo)
    if pt is None:
        return None
    al = align(ref.primaries, pt.primaries)
    if al.paired < MIN_PAIRED * max(1, al.ref_count):
        return None               # a different software: don't trust chance runs
    # The area between the calibration region start and the first pointer
    # block is the linker's small-data section (byte/word constants reached
    # by base-register displacement, not through the table). Its bounds are
    # fixed on both sides -- region start and block 0 -- so an unchanged
    # length makes it one more exact block.
    first = pt.primaries[0]
    if (main_lo is not None and first is not None and ref.region
            and first - main_lo == ref.primaries[0] - ref.region[0] > 0):
        al.blocks.append(Block(-1, ref.region[0], ref.primaries[0], main_lo))
    if al.identical and ref.region:
        # the last entry's block runs to the end of the calibration region
        al.blocks.append(Block(len(ref.primaries) - 1, ref.primaries[-1],
                               ref.region[1], ref.primaries[-1]))
    return al.finish()
