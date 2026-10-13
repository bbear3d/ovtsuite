# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Anchor matching and cross-binary identification.

Given a reference knowledge base (kb.py) plus the binary whose layout the KB
addresses (its *reference binary*), identify every KB entity in a *target*
binary of the same firmware lineage:

1. Fingerprint each KB map/curve as its storage-structure bytes (count
   header + axis values) taken from the reference binary.
2. Find those fingerprints in the target's calibration region. Unique hits
   become anchors; ambiguous ones are resolved against the offset model.
3. Drop non-monotonic anchors (longest-increasing-subsequence filter).
4. Build a piecewise-constant OffsetModel (ported from hp6tuner.xdfmatch:
   the drift between two calibration layouts moves in steps -- each added or
   removed variable shifts everything after it -- so nearest-anchor offset
   tracks it exactly where linear interpolation smears).
5. Align the target's parameter-block pointer table (the ECU's own index of
   the calibration, see layout.py) to the reference's: every aligned block
   whose length is unchanged is an exact anchor, and anything inside it is
   placed by the ECU's structure rather than by byte comparison.
6. Project headerless entities (constants, indexed tables) through the
   model, tagging each with how far to trust the placement.

Provenance tiers (Identified.source), strongest first:
  'anchored'   fingerprint matched -- address corroborated byte-for-byte
  'struct'     a structure with this table's exact dimensions sits at the
               model-projected address (axis values differ -- calibration
               changes -- but the layout corroborates the placement)
  'verified'   projected via the model AND the entity bytes equal the
               reference (value unchanged between calibrations)
  'placed'     inside a pointer-table block of unchanged length: the ECU
               addresses this block as base + fixed displacement, so the
               address is exact even though the bytes changed
  'bracketed'  the anchors on both sides carry the same offset (no layout
               step in this gap); bytes differ from the reference
  'projected'  projected via the model only (the offset steps nearby, or the
               entity lies beyond the last anchor)
"""

import bisect
import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .kb import Kb, KbTable, KbConstant
from .progress import Reporter as _Progress
from .structure import try_curve, try_map, _mono


def _crc(b) -> int:
    return zlib.crc32(bytes(b)) & 0xFFFFFFFF


# Layout-matching reference binary for each KB family (established in M1:
# the Q XDF addresses fit T/AB directly, not the Q/K binaries).
REF_BINARIES = {
    "p350": "038906016AB.bin",
    "p475_83": "03G906016LQ.bin",
    "p475_84_mf": "03G906016MF.bin",
    "p475_84_hq": "03G906016HQ.bin",
    # EDC16U34 / P447HAXN: KB from the Bosch A2L for 03G906021LA SW 501410
    # (tools/generate_kb_definition.py). HAXN calibration addresses are the
    # same in every HAXN software, so KK (also HAXN) stays the reference.
    "p447": "03G906021KK.bin",
    "p447_906016": "03G906016AC.bin",   # EDC16U34 / P447 dataset P447HAS7 (BRM 1.9, from A2L)
    # P529 sub-families: deliberately NOT in ovtsuite.kb.FAMILIES, so they never
    # enter detect_family's automatic candidate scan (adding them there once
    # misrouted 43 unrelated files -- their score denominator is too small and
    # wins unfairly against a real family's, see project memory). They're
    # reached only via the exact dataset-id override in session.py, after the
    # broad p475_83 match already won on real evidence. Listed here anyway so
    # generate_layouts.py/generate_kb.py's self-check and the audit/tiers CLI
    # can still find their reference binary. No A2L exists for these yet --
    # built by tools/generate_p529_kb.py from p475_83's own curated table
    # catalogue (names/units/equations) with addresses offset-projected via
    # match() against p475_83 and then INDEPENDENTLY structurally re-verified
    # (try_map/try_curve) against each sub-group's own reference binary before
    # baking. map/curve tables only (680/680/602) -- no constants, no indexed.
    "p529_tawi": "038906016AH_p529tawi.bin",   # P529TAWI (038906016AH/AJ sw 8950/8951)
    "p529_tawj": "038906016AH_p529tawj.bin",   # P529TAWJ (038906016AH/AJ/S/T sw 9138/9139/9713/9710/9711)
    "p529_tawa": "03G906016GK_p529tawa.bin",   # P529TAWA (03G906016GK/GL/JN/JP/MQ sw 9970)
    # Additional mismatch-group sub-families (2026-09-15), same pattern as P529 above:
    # dataset-id reads a different project than the file structurally matches, and the
    # mismatch group is large/consistent enough to verify its own address layout. Also
    # deliberately NOT in ovtsuite.kb.FAMILIES -- reached only via _DATASET_ID_SUBFAMILY.
    "u1_379u85b6": "u1_379u85b6_ref.bin",   # 379U85B6 (parent p350, 456/44 files consistent)
    # P447HAXE: since 2026-10-13 from the Bosch A2L C447HAXE_00 (address-exact
    # on all 39 HAXE files), tools/generate_kb_definition.py
    "p447_haxe": "p447_haxe_ref.bin",
    # P447HABB / P447HAS9: address-exact definitions, generate_kb_definition.py
    "p447_habb": "03G906021AN_P447HABB_ref.bin",   # A2L C447HABB_00
    "p447_has9": "03G906021SC_P447HAS9_ref.bin",   # XDF for 03G906021SC
    "p475_aasj": "p475_aasj_ref.bin",   # P475AASJ (parent p447_906016, 602/5 files consistent)
    "p475_uas8": "p475_uas8_ref.bin",   # P475UAS8 (parent p447_906016, 672/5 files consistent)
    "p447_hbxb": "p447_hbxb_ref.bin",   # P447HBXB (parent p447, 641/5 files consistent)
    "p475_u8d4": "p475_u8d4_ref.bin",   # P475U8D4 (parent p350, 504/5 files consistent)
    "p475_u8d3": "p475_u8d3_ref.bin",   # P475U8D3 (parent p350, 512/5 files consistent)
    "p475_uabc": "p475_uabc_ref.bin",   # P475UABC (parent p447_906016, 539/3 files consistent)
    "p475_aas8": "p475_aas8_ref.bin",   # P475AAS8 (parent p447_906016, 834/3 files consistent)
    "p213_u8c3": "p213_u8c3_ref.bin",   # P213U8C3 (parent p350, 485/2 files consistent)
    # U8A7 (P350U8A7 + P475U8A7, 11 files with one pointer table): built by
    # tools/generate_derived_kb.py -- p350 translated through the ECU pointer
    # table (exact blocks + evidence-resolved changed stretches), tables
    # structurally re-verified across the group. Tables AND constants.
    "p350_u8a7": "p350_u8a7_ref.bin",   # = 038997016L sw 3791 (1037399384)
}


@dataclass
class Anchor:
    ref_addr: int
    target_addr: int
    name: str = ""

    @property
    def offset(self):
        return self.target_addr - self.ref_addr


class OffsetModel:
    """Monotonic ref<->target address mapping (piecewise-constant)."""

    def __init__(self, anchors: Sequence[Anchor]):
        anchors = sorted(anchors, key=lambda a: a.ref_addr)
        self.r = [a.ref_addr for a in anchors]
        self.off = [a.target_addr - a.ref_addr for a in anchors]

    def __len__(self):
        return len(self.r)

    def to_target(self, ref_addr: int) -> int:
        if not self.r:
            return ref_addr
        i = bisect.bisect_right(self.r, ref_addr)
        if i <= 0:
            return ref_addr + self.off[0]
        if i >= len(self.r):
            return ref_addr + self.off[-1]
        lo, hi = i - 1, i
        if ref_addr - self.r[lo] <= self.r[hi] - ref_addr:
            return ref_addr + self.off[lo]
        return ref_addr + self.off[hi]

    def bracket_agrees(self, ref_addr: int) -> bool:
        """True when the anchors on both sides carry the same offset --
        the step, if any, is not inside this gap."""
        if len(self.r) < 2:
            return False
        i = bisect.bisect_right(self.r, ref_addr)
        if i <= 0 or i >= len(self.r):
            return False
        return self.off[i - 1] == self.off[i]


def _lis_filter(anchors: List[Anchor]) -> List[Anchor]:
    """Keep the longest subsequence with strictly increasing target
    addresses (anchors are sorted by ref address)."""
    if not anchors:
        return anchors
    n = len(anchors)
    tails: List[int] = []          # indices; tails[k] = last index of LIS len k+1
    prev = [-1] * n
    for i, a in enumerate(anchors):
        t = a.target_addr
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if anchors[tails[mid]].target_addr < t:
                lo = mid + 1
            else:
                hi = mid
        if lo > 0:
            prev[i] = tails[lo - 1]
        if lo == len(tails):
            tails.append(i)
        else:
            tails[lo] = i
    out = []
    i = tails[-1]
    while i >= 0:
        out.append(anchors[i])
        i = prev[i]
    return out[::-1]


def _fingerprint(t: KbTable):
    """Structure bytes (headers + axes) of a KB table, from the *baked*
    knowledge base. Returns ``(fp_addr, fp_bytes, contiguous_z)`` or None.

    The bytes were captured from the reference binary at generation time and
    stored in the KB, so matching needs no reference binary at runtime.
    """
    if not t.fp:
        return None
    return t.fp_addr, t.fp_bytes, t.fp_contig


def _fingerprint_from_ref(refdata: bytes, t: KbTable):
    """Structure bytes (headers + axes) of a KB table read live from its
    reference binary. Used only at KB-generation time (tools/generate_kb.py)
    to bake the fingerprint into the KB; the runtime path uses ``_fingerprint``.

    Returns ``(fp_addr, fp_bytes, contiguous_z)`` or None. ``fp_addr`` is
    where the fingerprint starts in the reference address space (NOT always
    ``struct_addr`` -- group-map axis blocks start at first_axis-2).
    ``contiguous_z`` is True when the Z data directly follows the structure,
    so an anchored hit places Z exactly; group maps keep their Z planes
    elsewhere and Z must go through the offset model.
    """
    sa = t.struct_addr
    if t.kind == "curve":
        ax = t.x_addr if t.x_addr is not None else t.y_addr
        n = t.x_count if t.x_addr is not None else t.y_count
        return sa, bytes(refdata[sa:ax + 2 * n]), True
    if t.kind == "map":
        first = min(a for a in (t.x_addr, t.y_addr) if a is not None)
        end = max(t.x_addr + 2 * t.x_count, t.y_addr + 2 * t.y_count)
        if end - sa <= 4 + 2 * (t.x_count + t.y_count) + 8:
            # standard [Nx][Ny][X][Y][Z]
            return sa, bytes(refdata[sa:end]), True
        # group-map layout [N][axis]...: first counted axis block only
        n = t.x_count if first == t.x_addr else t.y_count
        return first - 2, bytes(refdata[first - 2:first + 2 * n]), False
    return None


_FIND_CAP = 32


def _find_all(hay: bytes, needle: bytes, start: int, end: int) -> List[int]:
    """All occurrences, up to a cap; a capped result is untrustworthy and
    the caller drops it rather than anchoring to the wrong copy."""
    out = []
    i = hay.find(needle, start, end)
    while i != -1 and len(out) <= _FIND_CAP:
        out.append(i)
        i = hay.find(needle, i + 1, end)
    return out


@dataclass
class Identified:
    name: str
    kind: str            # 'map' | 'curve' | 'indexed' | 'constant'
    ref_addr: int        # KB (reference-layout) address of the data
    target_addr: int     # placed address in the target
    source: str          # see module docstring
    kb_table: Optional[KbTable] = None
    kb_constant: Optional[KbConstant] = None
    note: str = ""       # placement evidence in words (GUI detail line)


@dataclass
class MatchResult:
    anchors: List[Anchor] = field(default_factory=list)
    model: Optional[OffsetModel] = None
    items: List[Identified] = field(default_factory=list)
    ambiguous: int = 0
    unmatched_tables: List[str] = field(default_factory=list)
    layout: object = None                    # layout.Alignment when available
    # fingerprint anchors that contradicted an exact pointer-table block
    # (a same-valued twin elsewhere matched instead); dropped from the model
    conflicts: List[Anchor] = field(default_factory=list)
    # zf6hp.directory.Directory when the platform has one -- falsy unless the
    # image's address array verified as a parameter directory
    directory: object = None

    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for it in self.items:
            c[it.source] = c.get(it.source, 0) + 1
        return c


def _entity_span(t: KbTable) -> int:
    return (t.rows * t.cols) * (t.z_bits // 8)


# Bracket re-anchoring (pass 2.8): search only the target span between the two
# neighbouring anchors, so a recovered structure can't be pulled in from an
# unrelated part of the image.  Fingerprint hits are byte-exact and uniqueness-
# checked, so a wider bracket is safe; a dims-only match is weaker, so restrict
# it to a tight bracket.
_BRACKET_FP_MAX = 0x4000
_BRACKET_DIMS_MAX = 0x2000

# A per-gear family member: <stem><index><suffix>, e.g. AccPed_trqEng[0]_MAP,
# Gearbx_trqMaxGear[1]_CUR.  Used by pass 2.9 to place per-gear arrays in order.
_FAMILY_RE = re.compile(r"^(.*?)(\d+)(_[A-Za-z0-9]+)$")


def match(kb: Kb, tgtdata: bytes, region: tuple, progress=None) -> MatchResult:
    res = MatchResult()
    start, end = region
    rep = _Progress.of(progress)

    # ---- pass 1: unique fingerprints -> anchors -------------------------
    rep.step("Matching: searching %d fingerprints" % len(kb.tables))
    pending = []          # (table, fp_addr, occurrences)
    contig: Dict[str, bool] = {}
    fp_addr_of: Dict[str, int] = {}
    for t in kb.tables:
        if t.kind == "indexed" or t.struct_addr is None:
            continue
        r3 = _fingerprint(t)
        if r3 is None:
            continue
        fp_addr, fp, contiguous = r3
        if len(fp) < 8:
            continue
        contig[t.name] = contiguous
        fp_addr_of[t.name] = fp_addr
        occ = _find_all(tgtdata, fp, start, end)
        if len(occ) == 1 and len(fp) >= 16:
            # Short fingerprints (2x2 maps are 12 bytes) are too weak to
            # anchor on sight -- same-shaped neighbours can carry identical
            # values in another calibration. They go to model-guided
            # resolution instead.
            res.anchors.append(Anchor(fp_addr, occ[0], t.name))
        elif len(occ) > _FIND_CAP:
            res.ambiguous += 1        # capped: too common to trust at all
        elif occ:
            pending.append((t, fp_addr, occ))
        else:
            res.unmatched_tables.append(t.name)

    res.anchors.sort(key=lambda a: a.ref_addr)
    res.anchors = _lis_filter(res.anchors)
    model = OffsetModel(res.anchors)

    # ---- pass 2: resolve ambiguous fingerprints against the model ------
    rep.step("Matching: resolving %d ambiguous fingerprints" % len(pending))
    resolved = []
    for t, fp_addr, occ in pending:
        want = model.to_target(fp_addr)
        ranked = sorted(occ, key=lambda a: abs(a - want))
        best = ranked[0]
        # accept only when the prediction clearly singles one occurrence out
        near = abs(best - want) <= 0x800
        clear = len(ranked) == 1 or abs(ranked[1] - want) >= 4 * max(
            1, abs(best - want))
        if near and clear:
            resolved.append(Anchor(fp_addr, best, t.name))
        else:
            res.ambiguous += 1
    if resolved:
        res.anchors = _lis_filter(
            sorted(res.anchors + resolved, key=lambda a: a.ref_addr))
        model = OffsetModel(res.anchors)

    # ---- pass 2.4: repair singleton-offset anchors ----------------------
    rep.step("Matching: repairing anchors (%d so far)" % len(res.anchors))
    # A decoy match (another table of the same shape holding this table's
    # reference values) shows up as one anchor whose offset differs from
    # BOTH neighbours while the neighbours agree. If a structure with the
    # right dimensions -- or the fingerprint itself -- sits at the
    # neighbour-consistent position, the anchor is moved there.
    def _dims_ok(tab: KbTable, pos: int) -> bool:
        if tab.kind == "curve" or not contig.get(tab.name, True):
            f = try_curve(tgtdata, pos)
            return f is not None and f.cols in (tab.x_count, tab.y_count)
        f = try_map(tgtdata, pos)
        return f is not None and {f.cols, f.rows} == {tab.x_count,
                                                      tab.y_count}

    kb_by_name = {t.name: t for t in kb.tables}
    fps = {}
    for t in kb.tables:
        if t.kind != "indexed" and t.struct_addr is not None:
            r3 = _fingerprint(t)
            if r3:
                fps[t.name] = r3[1]
    repaired = []
    A = res.anchors
    for i, a in enumerate(A):
        if 0 < i < len(A) - 1:
            prev_off = A[i - 1].offset
            next_off = A[i + 1].offset
            if (a.offset != prev_off and a.offset != next_off
                    and prev_off == next_off):
                want = a.ref_addr + prev_off
                tab = kb_by_name.get(a.name)
                fp = fps.get(a.name)
                if tab is not None and 0 <= want < len(tgtdata):
                    if (fp and bytes(tgtdata[want:want + len(fp)]) == fp)                             or _dims_ok(tab, want):
                        repaired.append(Anchor(a.ref_addr, want, a.name))
                        continue
        repaired.append(a)
    res.anchors = _lis_filter(sorted(repaired, key=lambda a: a.ref_addr))
    model = OffsetModel(res.anchors)

    # ---- pass 2.5: structure-verify tables whose bytes changed ----------
    rep.step("Matching: verifying re-calibrated structures")
    # A re-calibrated table's axis values differ from the reference, so its
    # fingerprint fails -- but its storage structure (counts + monotonic
    # axes) survives. Finding that exact structure at the model-projected
    # address corroborates the placement and adds an anchor.
    have = {a.name for a in res.anchors}
    struct_anchors = []
    for t in kb.tables:
        if t.kind == "indexed" or t.struct_addr is None or t.name in have:
            continue
        fp_addr = fp_addr_of.get(t.name)
        if fp_addr is None:
            continue
        ps = model.to_target(fp_addr)
        if not (start <= ps < end):
            continue
        ok = False
        if t.kind == "curve" or not contig.get(t.name, True):
            n = t.x_count if (t.x_addr is not None and (
                t.kind == "curve" or not contig.get(t.name, True))) else t.y_count
            f = try_curve(tgtdata, ps)
            ok = f is not None and f.cols == (
                t.x_count if t.x_addr is not None and t.kind == "curve"
                else (t.x_count if not contig.get(t.name, True) else t.y_count))
            if not ok and f is not None:
                ok = f.cols in (t.x_count, t.y_count)
        else:
            f = try_map(tgtdata, ps)
            ok = f is not None and {f.cols, f.rows} == {t.x_count, t.y_count}
        if ok:
            struct_anchors.append(Anchor(fp_addr, ps, t.name))
    if struct_anchors:
        res.anchors = _lis_filter(
            sorted(res.anchors + struct_anchors, key=lambda a: a.ref_addr))
        model = OffsetModel(res.anchors)
    struct_named = {a.name for a in struct_anchors}

    # ---- pass 2.7: the ECU's own pointer table --------------------------
    rep.step("Matching: aligning the ECU pointer table")
    # Each aligned block of unchanged length is an exact anchor (the code
    # addresses its contents as base + fixed displacement). A fingerprint
    # anchor that contradicts the block it lies in has matched a same-valued
    # twin elsewhere (e.g. identical curves for neighbouring gears) and is
    # dropped in favour of the ECU's placement.
    lay = None
    try:
        from . import layout as _layout
        lay = _layout.align_to_reference(kb.family, tgtdata, start)
    except Exception:
        lay = None
    if lay is not None and lay.blocks:
        # Two or more fingerprint anchors that agree with each other but not
        # with their block mean the block was rearranged at equal length --
        # then the block is the odd one out and is withdrawn instead.
        contra: Dict[int, List[Anchor]] = {}
        for a in res.anchors:
            b = lay.block_at(a.ref_addr)
            if b is not None and a.offset != b.delta:
                contra.setdefault(b.index, []).append(a)
        drop_blocks = {i for i, al in contra.items()
                       if len(al) >= 2 and len({a.offset for a in al}) == 1}
        if drop_blocks:
            lay.blocks = [b for b in lay.blocks if b.index not in drop_blocks]
            lay.finish()
        keep = []
        for a in res.anchors:
            b = lay.block_at(a.ref_addr)
            if b is not None and a.offset != b.delta:
                res.conflicts.append(a)
            else:
                keep.append(a)
        have_ref = {a.ref_addr for a in keep}
        block_refs = {b.ref_lo for b in lay.blocks}
        ptr_anchors = [Anchor(b.ref_lo, b.tgt_lo, "ptr:%d" % b.index)
                       for b in lay.blocks if b.ref_lo not in have_ref]
        # bases of blocks that pair by position but whose length changed
        # internally: anchor the base only (near-start entities place as
        # base + displacement); the length change past the base is left to
        # the surrounding fingerprint/struct anchors.
        base_anchors = [Anchor(b.ref_lo, b.tgt_lo, "base:%d" % b.index)
                        for b in getattr(lay, "base_anchors", [])
                        if b.ref_lo not in have_ref and b.ref_lo not in block_refs]
        res.anchors = _lis_filter(
            sorted(keep + ptr_anchors + base_anchors, key=lambda a: a.ref_addr))
        model = OffsetModel(res.anchors)
        struct_named -= {a.name for a in res.conflicts}
    res.layout = lay
    res.model = model

    # ---- pass 2.8: bracket re-anchor structures still unplaced -----------
    rep.step("Matching: bracket re-anchoring")
    # A table not yet anchored sits in a gap between two aligned anchors; its
    # exact structure usually appears once inside that gap.  Pass 2.5 checked
    # only the single projected point -- here we search the whole TARGET
    # bracket bounded by the neighbouring anchors, so a map whose fingerprint
    # is ambiguous globally, or whose axis values changed (fingerprint gone but
    # counts+monotonic axes survive), is recovered structurally rather than
    # left to the offset model.  Only within the bracket, and only on a unique
    # hit, so nothing is dragged in from an unrelated block.
    have = {a.name for a in res.anchors}
    ranch = sorted(res.anchors, key=lambda a: a.ref_addr)
    rrefs = [a.ref_addr for a in ranch]
    rtar = [a.target_addr for a in ranch]
    fp_new, dim_new = [], []
    for t in kb.tables:
        if t.kind == "indexed" or t.struct_addr is None or t.name in have:
            continue
        fp_addr = fp_addr_of.get(t.name)
        if fp_addr is None:
            continue
        # The pointer table is authoritative: never re-anchor a parameter that
        # already falls inside an exact block (that placement is base + fixed
        # displacement).  Only recover things sitting in the GAPS between blocks.
        if lay is not None and lay.block_at(fp_addr) is not None:
            continue
        j = bisect.bisect_right(rrefs, fp_addr)
        if j == 0 or j >= len(rrefs):
            continue                       # no bracketing anchor on one side
        b_lo = max(start, rtar[j - 1]); b_hi = min(end, rtar[j])
        if not 0 < b_hi - b_lo <= _BRACKET_FP_MAX:
            continue
        fp = fps.get(t.name)
        if fp:                             # byte-exact structure+axes -> anchored
            k = tgtdata.find(fp, b_lo, b_hi)
            if k != -1 and tgtdata.find(fp, k + 2, b_hi) == -1:
                fp_new.append(Anchor(fp_addr, k, t.name))
                continue
        if b_hi - b_lo <= _BRACKET_DIMS_MAX:   # tight bracket: exact-dims -> struct
            hit = None
            many = False
            for pos in range((b_lo + 1) & ~1, b_hi, 2):
                if _dims_ok(t, pos):
                    if hit is not None:
                        many = True
                        break
                    hit = pos
            if hit is not None and not many:
                dim_new.append(Anchor(fp_addr, hit, t.name))
    if fp_new or dim_new:
        res.anchors = _lis_filter(
            sorted(res.anchors + fp_new + dim_new, key=lambda a: a.ref_addr))
        model = OffsetModel(res.anchors)
        res.model = model
        kept = {a.name for a in res.anchors}
        struct_named |= {a.name for a in dim_new if a.name in kept}

    # ---- pass 2.9: ordered per-family placement -------------------------
    rep.step("Matching: placing per-gear families")
    # A per-gear map/curve array (e.g. AccPed_trqEng0..6) is N sibling tables of
    # one shape sitting consecutively.  Individually they defeat 2.8's uniqueness
    # test (N identical-shape structures in the bracket), but as an ORDERED SET
    # they place exactly: the i-th sibling by reference address maps to the i-th
    # same-shape structure by target address in the region.  Placed only when the
    # region holds EXACTLY as many same-shape structures as the family has
    # members, so a wrong count places nothing.
    have = {a.name for a in res.anchors}
    ranch = sorted(res.anchors, key=lambda a: a.ref_addr)
    rrefs = [a.ref_addr for a in ranch]; rtar = [a.target_addr for a in ranch]
    fams: Dict[tuple, list] = {}
    for t in kb.tables:
        if t.kind == "indexed" or t.struct_addr is None or t.name in have:
            continue
        m = _FAMILY_RE.match(t.name)
        if not m or fp_addr_of.get(t.name) is None:
            continue
        fams.setdefault((m.group(1), m.group(3), t.rows, t.cols), []).append(
            (int(m.group(2)), t))
    fam_new = []
    for (stem, suf, rows, cols), mem in fams.items():
        if len(mem) < 3:
            continue
        mem.sort()
        tabs = [t for _, t in mem]
        refs = [fp_addr_of[t.name] for t in tabs]
        if lay is not None and any(lay.block_at(r) is not None for r in refs):
            continue
        jlo = bisect.bisect_right(rrefs, min(refs))
        jhi = bisect.bisect_right(rrefs, max(refs))
        if jlo == 0 or jhi >= len(rrefs):
            continue
        t_lo = max(start, rtar[jlo - 1]); t_hi = min(end, rtar[jhi])
        if not 0 < t_hi - t_lo <= _BRACKET_FP_MAX * 4:
            continue
        step = (rows * cols) * (tabs[0].z_bits // 8)
        found = []
        pos = t_lo & ~1
        while pos < t_hi:
            f = try_curve(tgtdata, pos) if rows == 1 else try_map(tgtdata, pos)
            ok = (f is not None and (f.cols == cols if rows == 1
                                     else {f.rows, f.cols} == {rows, cols}))
            if ok:
                found.append(pos)
                pos = f.z_addr + step; pos += pos & 1
            else:
                pos += 2
        if len(found) == len(tabs):
            for t, ps in zip(tabs, found):
                fam_new.append(Anchor(fp_addr_of[t.name], ps, t.name))
    if fam_new:
        res.anchors = _lis_filter(
            sorted(res.anchors + fam_new, key=lambda a: a.ref_addr))
        model = OffsetModel(res.anchors)
        res.model = model
        kept = {a.name for a in res.anchors}
        struct_named |= {a.name for a in fam_new if a.name in kept}

    anchored = {a.name for a in res.anchors if not a.name.startswith("ptr:")}
    by_name = {a.name: a for a in res.anchors}
    res.unmatched_tables = [n for n in res.unmatched_tables
                            if n not in anchored]

    def grade(ref_addr, span, same, is_const=False):
        """(source, note) for a model-projected entity."""
        strong = (model.bracket_agrees(ref_addr) if is_const else span >= 4)
        if same and strong:
            return "verified", "bytes equal the reference at the projected address"
        b = lay.block_at(ref_addr, span) if lay is not None else None
        if b is not None:
            return "placed", ("pointer-table block %d (0x%X, %d bytes, length "
                              "unchanged): the ECU addresses it as base + fixed "
                              "displacement" % (b.index, b.tgt_lo, b.length))
        if model.bracket_agrees(ref_addr):
            return "bracketed", ("same offset on the anchors either side; "
                                 "bytes differ from the reference")
        return "projected", ("offset model only: the layout steps nearby or no "
                             "anchor follows; check before writing")

    # ---- pass 3: place every KB entity ---------------------------------
    rep.step("Matching: placing %d parameters"
             % (len(kb.tables) + len(kb.constants)))
    for t in kb.tables:
        if t.name in anchored:
            a = by_name[t.name]
            src = "struct" if t.name in struct_named else "anchored"
            if contig.get(t.name, True):
                # Z follows the anchored structure directly
                za = a.target_addr + (t.z_addr - fp_addr_of[t.name])
                res.items.append(Identified(t.name, t.kind, t.z_addr, za,
                                            src, kb_table=t))
            else:
                # group map: axes anchored, Z planes live elsewhere
                za = model.to_target(t.z_addr)
                span = _entity_span(t)
                same = (0 <= za and za + span <= len(tgtdata)
                        and _crc(tgtdata[za:za + span]) == t.vhash)
                src, note = grade(t.z_addr, span, same)
                res.items.append(Identified(t.name, t.kind, t.z_addr, za,
                                            src, kb_table=t, note=note))
            continue
        ta = model.to_target(t.z_addr)
        span = _entity_span(t)
        same = (0 <= ta and ta + span <= len(tgtdata) and
                _crc(tgtdata[ta:ta + span]) == t.vhash)
        src, note = grade(t.z_addr, span, same)
        res.items.append(Identified(t.name, t.kind, t.z_addr, ta, src,
                                    kb_table=t, note=note))

    for c in kb.constants:
        ta = model.to_target(c.addr)
        w = c.bits // 8
        same = (0 <= ta and ta + w <= len(tgtdata) and
                _crc(tgtdata[ta:ta + w]) == c.vhash)
        # a constant's own bytes are weak evidence; require the bracket to
        # agree as well before calling it verified
        src, note = grade(c.addr, w, same, is_const=True)
        res.items.append(Identified(c.name, "constant", c.addr, ta, src,
                                    kb_constant=c, note=note))
    return res
