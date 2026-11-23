# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Variant / secondary calibration tables (coding-selected duplicates).

Some EDC16 datasets carry a *secondary* calibration block before the main
block, holding duplicate copies of tables the ECU selects between by coding.
In the 038906016 (P350) binaries this is the **automatic vs manual gearbox**
variant: the drivetrain/gear tables that matter for cruise control exist in
two versions -- including the gear ratios (``Gearbx_rTrans1..6_C``) and the
speed/rpm detection bands (``Gearbx_rVn*_C``), whose stock values differ
between the copies.

Two ways to find them:

1. **Pointer arrays (exact).** A ``0xCAFEAFFE`` descriptor at ~0x180040
   references a *default* pointer array (all-primary: the start of each of
   ~370-400 calibration blocks, "slots") and an *active* array (@0x180084),
   which holds one run of slots per coding state. Where a state's
   ``active[i]`` is a secondary address and its default the primary, slot
   ``i`` is switched under that coding: its primary block runs to the next
   slot's start and its relocated copy is exactly as long, so every catalog
   parameter inside is paired -- including single-value constants like the gear
   ratios. The default array lives in the program area, so a full read has it.
   A calibration-only (OBD) read has the program area blank, but the active
   array is the default one with each state's switched slots redirected, so the
   default array is *rebuilt* from it: the slots a state leaves in the main
   block name their starts, and a slot every state switches is placed by the
   packing of the relocated copies (back to back, in slot order, each at its
   primary's offset modulo 4; the last block of a run ends where the next
   unswitched slot starts). Nothing is matched by byte values, so a block whose
   values were retuned is found as surely as an untouched one, and the rebuilt
   array is the real one on every full read in the corpus. Both reads then go
   through the same code.

2. **Axis fingerprint (fallback).** Only where there are no pointer arrays at
   all: maps and curves are paired to a main-block twin by identical axis
   bytes. Approximate, and it cannot see value constants.

``coding_domain`` inspects the paired names and reports the coding condition
(e.g. "gearbox coding (automatic vs manual)"). How a coding value itself reads
-- a decimal number on P350, a variant above function-code bits on P447 -- is
:func:`describe_codings`.
"""

import bisect
import dataclasses
import operator
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional

from .structure import scan

_SEARCH_LO = 0x182000
_CAFEAFFE = b"\xCA\xFE\xAF\xFE"
# Window for the last relocated block of a calibration-only read: nothing
# follows it to measure its length by.
_LAST_BLOCK_MAX = 0x400
# How far past the previous unswitched slot's start the byte-match fallback
# looks for a block's primary base.
_ANCHOR_SPAN = 0x4000

# Family -> reference A2L (header-address -> name), for naming primaries.
_A2L = {
    "p350": "038997016Q_4999_502333_P350_U8A9_EDC16U31_4.47.a2l",
    "p475_83": "BPW.a2l", "p475_84_hq": "BPW.a2l", "p475_84_mf": "BPW.a2l",
    "p447_906016": "03G906016AB-P447HAS7.a2l",
}

# Per-parameter coding domains, matched on the parameter name. A key ending in
# "_" is an *anchored prefix* (name.startswith), otherwise it is a substring.
# Order matters: the first matching entry wins.
#
# The gearbox domain is deliberately anchored to the transmission/road-speed
# subsystems' own prefixes (Gearbx_, VSSCD_, DrvTrn_, ...) rather than loose
# substrings. A bare "Gearbx" substring wrongly claimed A/C parameters that
# merely reference the gearbox CAN frame (e.g. ACCtl_tiCANGearbx_C) and any
# "*Gearbx*" slope/offset the frame-manager owns (FrmMng_*, which is really
# drivetrain coding); anchoring to Gearbx_/VSSCD_/... counts only genuine
# gear-ratio and VSS coding.
_PARAM_DOMAINS = [
    ("gearbox coding (automatic vs manual)",
     ("Gearbx_", "VSSCD_", "DrvTrn_", "Diff_", "ConvCD_", "ASDdc_", "ASDrf_")),
    ("cruise-control coding",
     ("CrCtl", "CoVeh")),
    ("drivetrain / torque-interface coding",
     ("FrmMng", "OvRMon", "TSC", "TrqIntf")),
    ("accelerator-pedal coding",
     ("AccPed", "APPCD", "AccP")),
    ("air / boost-path coding",
     ("AirCtl", "AFSCD", "Boost", "Chrg", "EGR", "AGR")),
    ("fuelling / smooth-running coding",
     ("FBC", "InjCrv", "SmoothRun", "MSMFuel")),
    ("A/C-HVAC coding (Climatronic vs manual A/C)",
     ("ACCD", "Climatronic", "ACtype", "swtAC")),
    ("auxiliary-heater coding",
     ("AOHtCD", "AOHt", "Heater", "Htr", "Webasto")),
    ("immobilizer / dataset-variant coding",
     ("USAp", "UKey", "dCodeTbl")),
]


@dataclass
class VariantTable:
    name: str
    kind: str            # 'map' | 'curve' | 'value'
    rows: int
    cols: int
    sec_z: int           # secondary data address (what you edit)
    main_z: int          # primary data address
    differs: bool
    method: str          # 'array' (exact, from the pointer arrays) | 'axis' (fallback)
    ambiguous: bool = False

    @property
    def label(self):
        t = "" if self.method in ("array", "anchor") and not self.ambiguous else "?"
        d = " [differs]" if self.differs else ""
        return "%s (variant%s)%s" % (self.name, t, d)


def _u32(d, o):
    return (d[o] << 24) | (d[o + 1] << 16) | (d[o + 2] << 8) | d[o + 3]


def _a2l_names(family):
    fn = _A2L.get(family)
    if not fn:
        return {}
    path = os.path.join(os.path.dirname(__file__), "..", "Reference", "A2L", fn)
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="latin-1").read()
    out = {}
    for m in re.finditer(
            r'/begin (?:CHARACTERISTIC|AXIS_PTS)\s+(\S+)\s+"(.*?)"\s+(\S+)\s+'
            r'(0x[0-9A-Fa-f]+)', txt, re.S):
        out[int(m.group(4), 16)] = m.group(1)
    return out


def _locate_arrays(data):
    """(default_array_addr, active_array_addr) or (None, active) -- searching the
    data area so a stray 0xCAFEAFFE in program code is skipped."""
    i = data.find(_CAFEAFFE, 0x180000)
    if i < 0:
        return None, None
    default = _u32(data, i + 0x1C)
    active = _u32(data, i + 0x24)
    if not (0x1000 <= active < len(data)):
        active = None
    if not (default < len(data)
            and any(b != 0xFF for b in data[default:default + 64])):
        default = None
    return default, active


def _found_by_header(structs):
    return {f.addr: f for f in structs}


def find_variant_tables(session, min_structs: int = 20) -> List[VariantTable]:
    data = bytes(session.binfile.data)
    main_lo, main_hi = session.region

    main_structs = scan(data, main_lo, main_hi)
    main_by_hdr = _found_by_header(main_structs)
    name_by_z = {e.addr: e.name for e in session.entities}
    # names for secondary/variant primaries: the map baked into the KB (so no
    # A2L is needed at runtime), supplemented by the A2L file when present.
    a2l = dict(getattr(session.kb, "a2l_names", {}))
    a2l.update(_a2l_names(session.family))

    def name_for_primary(hdr):
        f = main_by_hdr.get(hdr)
        z = f.z_addr if f else hdr
        return name_by_z.get(z) or a2l.get(hdr) or ("@0x%X" % hdr)

    default, active = _locate_arrays(data)

    # ---- pointer arrays: the exact (primary -> relocated) blocks ------------
    # The arrays give the *exact* set of switched blocks, on a full read from
    # its default array and on a calibration read from the default array
    # rebuilt out of the active one (_switched_blocks). Each is a verbatim
    # relocation of a contiguous main-block span, so every catalog parameter
    # inside that span -- including value constants like the gear ratios -- has
    # a secondary copy at the same offset. We therefore *project* each catalog
    # entity within a block to its secondary twin, instead of emitting one
    # coarse table per pointer (which would hide the individual constants inside
    # a switched structure). Nothing is matched by bytes, so a block whose
    # values were retuned is found as surely as an untouched one, and no
    # spurious variant tables appear on binaries that have no real coding block.
    blocks = (_switched_blocks(data, main_lo, main_hi, default, active)
              if active is not None else None)
    if blocks is not None:
        sec_structs = []
        for a, b in find_secondary_regions(data, main_lo):
            sec_structs += scan(data, a, b)
        sec_by_hdr = _found_by_header(sec_structs)
        by_z = {e.addr: e for e in session.entities}
        kb_addrs = sorted(by_z)

        out = []
        seen = set()
        for base, act, blk in blocks:
            lo_i = bisect.bisect_left(kb_addrs, base)
            hi_i = bisect.bisect_left(kb_addrs, base + blk)
            projected = False
            for a in kb_addrs[lo_i:hi_i]:
                e = by_z[a]
                sec_z = act + (a - base)
                span = e.n_cells * e.step
                if sec_z + span > len(data) or not (_SEARCH_LO <= sec_z < main_lo):
                    continue
                key = (a, sec_z)
                if key in seen:
                    continue
                seen.add(key)
                differs = (data[a:a + span] != data[sec_z:sec_z + span])
                kind = "value" if e.kind in ("constant", "indexed") else e.kind
                out.append(VariantTable(
                    name=e.name, kind=kind, rows=e.rows, cols=e.cols,
                    sec_z=sec_z, main_z=a, differs=differs, method="array"))
                projected = True
            if projected:
                continue
            # no catalog entity inside the span: keep the coarse header pairing
            pf = main_by_hdr.get(base)
            sf = sec_by_hdr.get(act)
            if pf and sf:
                main_z, sec_z, n_cells = pf.z_addr, sf.z_addr, pf.rows * pf.cols
                kind, rows, cols = pf.kind, pf.rows, pf.cols
            else:
                main_z, sec_z, n_cells = base, act, 1
                kind, rows, cols = "value", 1, 1
            key = (main_z, sec_z)
            if key in seen:
                continue
            seen.add(key)
            differs = (data[main_z:main_z + n_cells * 2]
                       != data[sec_z:sec_z + n_cells * 2])
            out.append(VariantTable(
                name=name_for_primary(base), kind=kind, rows=rows, cols=cols,
                sec_z=sec_z, main_z=main_z, differs=differs, method="array"))
        out = _drop_cross_block(session, data, main_lo, main_hi, active, out)
        out.sort(key=lambda v: v.sec_z)
        # A real coding block carries at least one parameter that actually takes
        # a different value between codings (a byte-identical set is just the
        # same data relocated, not a coding alternative). Small real blocks --
        # e.g. the 3-parameter A/C-type coding on some datasets -- are kept.
        return out if any(v.differs for v in out) else []

    # ---- no usable pointer arrays: axis fingerprint (approximate) -----------
    axis = _axis_variants(data, main_lo, main_structs, name_by_z, a2l,
                          min_structs)
    axis.sort(key=lambda v: v.sec_z)
    return axis if any(v.differs for v in axis) else []


def _drop_cross_block(session, data, main_lo, main_hi, active, found):
    """Drop a copy that runs past the end of its relocated block into the
    next one. The ECU reads the copy from inside the block, so such an entry
    is a mis-sized projection -- and editing it would write into the next
    block's table (038997016R: a 4-byte ASMod_facPresLsCmprPre_C copy at
    0x1891DC ran over ConvCD_DebConvErrDef_C's copy at 0x1891DE)."""
    if active is None or not found:
        return found
    starts = sorted({x for st in _active_states(data, active, main_lo, main_hi)
                     for x in st if not (main_lo <= x < main_hi)})
    if not starts:
        return found
    by_z = {e.addr: e for e in session.entities}
    out = []
    for v in found:
        e = by_z.get(v.main_z)
        span = e.n_cells * e.step if e is not None else 2
        j = bisect.bisect_right(starts, v.sec_z)
        if j < len(starts) and v.sec_z + span > starts[j]:
            continue
        out.append(v)
    return out


# ---- placing the relocated blocks from the arrays alone --------------------

def _anchor(data, sec, lo, hi, win=0x40, thresh=0.6):
    """Best main-block base for the secondary copy at ``sec``, found by byte
    match over the sorted-neighbour window [lo, hi]. The switched copy is a
    verbatim relocation of a contiguous main range, so most bytes (all the
    untuned ones plus the structural header) match at the true base. Returns
    (base, score) or (None, 0) if nothing clears the threshold.

    A copy sits at the same offset modulo 4 as the block it copies (the
    relocation keeps the alignment; true of every redirected block on every
    dataset checked), so only such bases are candidates. Without that, a block
    of repeated 16-bit values also matches itself two bytes off."""
    n = len(data)
    lo2 = max(0, lo - 16)
    hi2 = min(hi, lo + _ANCHOR_SPAN) + 16
    hi2 = min(hi2, n - win)
    ref = data[sec:sec + win]
    scores = []
    for p in range(lo2 + (sec - lo2) % 4, hi2, 4):
        scores.append((sum(map(operator.eq, ref, data[p:p + win])), p))
    if not scores:
        return None, 0
    best, best_p = max(scores)
    if best >= int(thresh * win):
        return best_p, best
    # A copy whose values were retuned (the gear ratios and v/n bands, say)
    # loses bytes at the true base. Still take it when it stands well clear of
    # every unrelated alignment: a shifted self-match of the same block (a few
    # bytes either side) does not count as a rival.
    rival = max((s for s, p in scores if abs(p - best_p) > 8), default=0)
    if best >= int(0.35 * win) and best >= 2 * rival:
        return best_p, best
    return None, 0


def _populated_len(data, v, limit):
    """Bytes of data at ``v`` before the blank (0xFF) fill that follows the
    last packed copy: that copy's length, which no later copy measures."""
    q = data.find(b"\xff" * 0x100, v, limit)
    q = limit if q < 0 else q
    while q > v and data[q - 1] == 0xFF:
        q -= 1
    return q - v


def _structural_blocks(states, sec_starts, in_main, in_sec, tail=0):
    """``{secondary start: (primary base, length)}`` for the relocated blocks
    whose primary base follows from the pointer arrays alone -- no byte match,
    so a block whose values were all retuned is placed as surely as an
    untouched one.

    In a state, the slots between two unswitched ones tile the main block
    exactly, so a run of switched slots ends where the next unswitched slot
    starts. The copies are packed in slot order, so a run's blocks lie back to
    back (each is as long as the step to the next) and only the run's last
    block needs measuring: by the gap to the next packed copy, which is the
    block's length or two bytes more (padding that keeps a copy at its
    primary's offset modulo 4 -- the one of the two lengths that keeps it is
    the length). The last packed copy of all has no next copy: it is as long
    as the data that follows it is populated (``tail``; a copy that ends in
    blank bytes may be up to three longer, which the same alignment decides).
    Checked against the default array on every full read in the corpus: every
    block derived this way is exact, length included."""
    def lengths(v):
        """The lengths the block at ``v`` can have, most likely first."""
        j = bisect.bisect_right(sec_starts, v)
        if j < len(sec_starts):
            g = sec_starts[j] - v
            return (g, g - 2)
        if tail > 0 and v == sec_starts[-1]:
            return tuple(tail + d for d in range(4))
        return ()

    out = {}
    for st in states:
        n = len(st)
        i = 0
        while i < n:
            if not in_sec(st[i]):
                i += 1
                continue
            j = i
            while j + 1 < n and in_sec(st[j + 1]):
                j += 1
            lo = st[i - 1] if i > 0 and in_main(st[i - 1]) else None
            hi = st[j + 1] if j + 1 < n and in_main(st[j + 1]) else None
            if hi is not None:
                end = hi
                run = []
                for k in range(j, i - 1, -1):
                    ln = next((x for x in lengths(st[k])
                               if x > 0 and (end - x - st[k]) % 4 == 0), None)
                    if ln is None:
                        run = [] if k == j else run
                        break
                    end -= ln
                    run.append((st[k], end, ln))
                else:
                    if lo is not None and end <= lo:
                        run = []            # does not fit between its neighbours
                for v, b, x in run:
                    out.setdefault(v, (b, x))
            i = j + 1
    return out


def _rebuild_slots(data, states, main_lo, main_hi):
    """The default array a calibration read lacks, rebuilt from the active one.

    The active array is the default array with each coding state's switched
    slots pointed into the relocated region, so a slot a state leaves in the
    main block names its start there. A slot every state switches is placed by
    the packing (:func:`_structural_blocks`), or failing that by byte match
    between its known neighbours. Returns ``(slots, lens)``: the slot starts
    (None where one could not be placed) and each relocated block's length."""
    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    n = len(states[0])
    slots = [None] * n
    for st in states:
        for i, v in enumerate(st[:n]):
            if in_main(v) and slots[i] is None:
                slots[i] = v
    sec_starts = sorted({x for st in states for x in st if in_sec(x)})
    tail = (_populated_len(data, sec_starts[-1], main_lo)
            if sec_starts else 0)
    placed = _structural_blocks(states, sec_starts, in_main, in_sec, tail)
    lens = {}
    for st in states:
        for i, v in enumerate(st[:n]):
            if in_sec(v) and v in placed:
                base, ln = placed[v]
                if slots[i] is None:
                    slots[i] = base
                if slots[i] == base:            # a state that keeps the slot
                    lens[v] = ln                # in main must agree
    # what the packing cannot place: byte match between the known neighbours
    for st in states:
        for i, v in enumerate(st[:n]):
            if not in_sec(v) or slots[i] is not None:
                continue
            lo = next((slots[k] for k in range(i - 1, -1, -1)
                       if slots[k] is not None), None)
            hi = next((slots[k] for k in range(i + 1, n)
                       if slots[k] is not None), None)
            if lo is None or hi is None or not lo < hi:
                continue
            base, _score = _anchor(data, v, lo, hi)
            if base is None:
                continue
            slots[i] = base
            j = bisect.bisect_right(sec_starts, v)
            lens[v] = (min(sec_starts[j] - v, hi - base)
                       if j < len(sec_starts)
                       else min(hi - base, _LAST_BLOCK_MAX))
    return slots, lens


def _switched_blocks(data, main_lo, main_hi, default, active):
    """Every block some coding state relocates, as ``(primary start, relocated
    start, length)`` -- or None when the arrays give no layout.

    One layout for every read. A full read has the default array, whose
    consecutive entries are the slot starts. A calibration read lacks it (the
    program area is blank), so it is rebuilt from the active array
    (:func:`_rebuild_slots`) and the same blocks come out of it: checked
    against the default array on every full read in the corpus, every block's
    start and length are exact."""
    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    states = _active_states(data, active, main_lo, main_hi)
    lens = {}
    if default is not None:
        slots = []                      # the default (all-primary) slot starts
        while (default + len(slots) * 4 + 4 <= len(data)
               and active + len(slots) * 4 + 4 <= len(data)):
            base = _u32(data, default + len(slots) * 4)
            if not (0x1000 <= base < 0x200000):
                break
            slots.append(base)
        n = len(slots)
        # Every coding state of the active array redirects its own slots, so
        # all of them are read (not only the first). A state's slot count must
        # match the default array's; if the parse does not agree, only the
        # first state -- read straight from the array -- is used.
        if not states or len(states[0]) != n:
            states = [[_u32(data, active + k * 4) for k in range(n)]]
    else:
        if sum(len(st) for st in states) < 50:
            return None
        slots, lens = _rebuild_slots(data, states, main_lo, main_hi)
        n = len(slots)

    pairs = []                          # (slot index, primary start, secondary)
    seen_pair = set()
    for st in states:
        for i, act in enumerate(st[:n]):
            base = slots[i]
            if base is None or act == base or (base, act) in seen_pair:
                continue
            if not (in_sec(act) and in_main(base)):
                continue
            seen_pair.add((base, act))
            pairs.append((i, base, act))

    # The primary block a slot redirects runs to the next slot's start, and
    # its relocated copy is exactly as long. The packed copies of different
    # blocks cannot overlap, so the gap to the next copy bounds it too. A slot
    # with no next start (the array's last) takes its length from the packing;
    # failing that only the table at its own start is taken, never a guessed
    # window of the neighbouring data.
    acts = sorted({a for _i, _b, a in pairs})
    out = []
    for i, base, act in pairs:
        ln = None
        if i + 1 < n and slots[i + 1] is not None and slots[i + 1] > base:
            ln = slots[i + 1] - base
        elif act in lens:
            ln = lens[act]
        j = bisect.bisect_right(acts, act)
        gap = acts[j] - act if j < len(acts) else None
        if ln is None or ln <= 0:
            ln = 1
        elif gap:
            ln = min(ln, gap)
        out.append((base, act, ln))
    return out


# ---- axis fingerprint (fallback when there are no pointer arrays) -------

def _axis_variants(data, main_lo, main_structs, name_by_z, a2l, min_structs):
    sec_structs = []
    for a, b in find_secondary_regions(data, main_lo):
        sec_structs += scan(data, a, b)
    if len(sec_structs) < min_structs:
        return []
    main_by_fp = defaultdict(list)
    for f in main_structs:
        main_by_fp[_fingerprint(data, f)].append(f)
    # A relocated block keeps its layout, so a copy sits at a fixed distance
    # from its main table. Where an axis is unique the match is certain and
    # gives that distance for its neighbourhood.
    unique = sorted((f.z_addr, f.z_addr - main_by_fp[_fingerprint(data, f)][0].z_addr)
                    for f in sec_structs
                    if len(main_by_fp.get(_fingerprint(data, f), ())) == 1)
    uz = [z for z, _d in unique]

    def resolve(f, twins):
        """The twin a copy with a SHARED axis belongs to: the one at the
        distance its nearest certain neighbour has. Taking the first twin
        instead named every such copy after one table -- on 038906016Q,
        FBC_fSurge_CUR's copy came out as a second FBC_BPFac1NumG_CUR and
        ASDrf_trqPushNeg_CUR's as a second ASDrf_trqPullPos_CUR."""
        i = bisect.bisect_left(uz, f.z_addr)
        near = sorted((abs(uz[j] - f.z_addr), unique[j][1])
                      for j in (i - 1, i) if 0 <= j < len(uz))
        for gap, delta in near:
            if gap > 0x400:
                break
            hit = [t for t in twins if t.z_addr == f.z_addr - delta]
            if hit:
                return hit[0], False
        return twins[0], True

    out = []
    for f in sec_structs:
        twins = main_by_fp.get(_fingerprint(data, f))
        if not twins:
            continue
        m, amb = (twins[0], False) if len(twins) == 1 else resolve(f, twins)
        name = name_by_z.get(m.z_addr) or a2l.get(m.addr) or ("@0x%X" % m.z_addr)
        differs = (_z_bytes(data, f) != _z_bytes(data, m))
        out.append(VariantTable(
            name=name, kind=f.kind, rows=f.rows, cols=f.cols,
            sec_z=f.z_addr, main_z=m.z_addr, differs=differs, method="axis",
            ambiguous=amb))
    return out


def _fingerprint(data, f):
    return (f.kind, f.rows, f.cols, bytes(data[f.addr:f.z_addr]))


def _z_bytes(data, f, step=2):
    return bytes(data[f.z_addr:f.z_addr + f.n_cells * step])


def find_secondary_regions(data, main_start):
    regions = []
    run = None
    o = _SEARCH_LO
    while o < main_start:
        populated = any(b != 0xFF for b in data[o:o + 0x1000])
        if populated and run is None:
            run = o
        elif not populated and run is not None:
            regions.append((run, o))
            run = None
        o += 0x1000
    if run is not None:
        regions.append((run, main_start))
    return regions


def param_domain(name: str) -> str:
    """The coding domain a single variant parameter belongs to.

    A key ending in "_" matches as an anchored prefix (name.startswith); every
    other key matches as a substring.
    """
    for label, keys in _PARAM_DOMAINS:
        for k in keys:
            if name.startswith(k) if k.endswith("_") else k in name:
                return label
    return "other / uncategorised"


def coding_domains(variant_tables):
    """Counter of coding domain -> number of *differing* variant parameters, so
    a binary with several small coding blocks reports each one."""
    from collections import Counter
    return Counter(param_domain(v.name) for v in variant_tables if v.differs)


def coding_domain(variant_tables) -> Optional[str]:
    """The dominant *recognised* coding condition (backwards-compatible single
    answer); None if only uncategorised parameters differ."""
    dom = coding_domains(variant_tables)
    dom.pop("other / uncategorised", None)
    if not dom:
        return None
    return dom.most_common(1)[0][0]


def has_secondary_block(session, min_structs: int = 20) -> bool:
    return bool(find_variant_tables(session, min_structs))


# ---- stored coding values (coding-acceptance table + inverses) -----------

# How a coding value reads depends on the dataset. The A2L names the table
# ``USAp_dCodeTbl_C`` "Identifier dieser Datensatzvariante" -- the identifier
# of the dataset variant -- and gives ``USAp_numFuncCodeBits_C``, the number of
# low bits that carry the *function code* (A/C, alternator, ESP, fan, vehicle
# speed, WIV; their positions are ``UAcc_*CodePos_C``).
#
# * P350 and the other datasets with no function bits (``numFuncCodeBits`` 0)
#   hold the coding as a plain decimal number, and VAG's digit scheme applies
#   (confirmed against VCDS): the last digit is the transmission, the
#   second-to-last the A/C system. Higher digits encode further options.
# * P447 (``numFuncCodeBits`` 18) holds ``variant << 18`` -- the function
#   bits, which a car's EEPROM supplies, are zero in the dataset -- so the
#   variant number is the value shifted down, and no decimal digit means
#   anything. Reading its digits as the P350 scheme gets the transmission
#   backwards (variant 3, 786432, is the automatic; 524288 the manual).
#
# Either way the dataset states the transmission itself: ``Gearbx_swtTypeVal_C``
# ("Getriebetyp"), switched with the coding like every other variant
# parameter. That is what a transmission is read from; the P350 digit scheme
# only adds the detail it carries (5- or 6-speed, torque converter or DSG).
_TX = {"1": "5-speed manual", "2": "6-speed manual", "4": "automatic (TC)",
       "6": "CVT", "8": "DSG"}
_AC = {"3": "manual A/C", "7": "Climatronic"}
# Gearbx_swtTypeVal_C, from the A2L
_TYPE_NAMES = {0: "manual", 1: "automatic", 2: "automatic (shift)",
               3: "CVT", 4: "automatic manual"}


def _u16(d, o):
    return (d[o] << 8) | d[o + 1]


def decode_coding(value):
    """(transmission, a/c) implied by a coding value's last two decimal digits
    -- the P350-style scheme, meaningful only where the dataset has no function
    code bits (see :func:`describe_codings`, which knows which applies) -- or
    (None, None) for the unknown parts."""
    s = "%d" % value
    d0 = s[-1]
    d1 = s[-2] if len(s) >= 2 else "0"
    return _TX.get(d0), _AC.get(d1)


def _is_automatic(label):
    return bool(label) and (label.startswith("automatic")
                            or label in ("DSG", "CVT"))


def _coding_pairs(data):
    """Every {value, inverse} coding pair stored in the calibration region.
    Codings are held as a 32-bit value immediately preceded by its bitwise
    complement (integrity twin); such adjacency is astronomically unlikely by
    chance, so the pair itself is the signature. Returns [(tbl_addr, value)],
    de-duplicated by value, lowest address kept.

    A scan, and so limited to values below 0x100000 (a wider window lets
    chance pairs in): :func:`stored_codings` is what finds every coding,
    including the larger ones function-coded datasets hold."""
    out = {}
    o = 0x180000
    n = len(data)
    while o + 8 <= n:
        inv = _u32(data, o)
        tbl = _u32(data, o + 4)
        if (inv ^ tbl) & 0xFFFFFFFF == 0xFFFFFFFF \
                and 0 < tbl < 0x100000 and tbl != 0xFFFF and inv > 0xFFF00000:
            out.setdefault(tbl, o + 4)
        o += 2
    return sorted(((a, v) for v, a in out.items()), key=lambda r: r[1])


def _read_code_at(data, ptr):
    """The coding value stored at ``ptr`` (the lower of the value / bitwise-
    inverse twin pair, matching how codings are held)."""
    v = _u32(data, ptr)
    nxt = _u32(data, ptr + 4) if ptr + 8 <= len(data) else 0
    prv = _u32(data, ptr - 4) if ptr >= 4 else 0
    if ((v ^ nxt) & 0xFFFFFFFF) == 0xFFFFFFFF:
        return min(v, nxt)
    if ((v ^ prv) & 0xFFFFFFFF) == 0xFFFFFFFF:
        return min(v, prv)
    return v


def stored_codings(session, tables=None):
    """``[(address, value)]`` of every coding the dataset stores, by value.

    The coding table ``USAp_dCodeTbl_C`` in the main block, and the copy of it
    in every relocated block (one per other coding state): each is a value
    followed by its bitwise complement. Found where the variant tables put
    them rather than by scanning for pairs, so a function-coded dataset's
    larger values (P447's ``variant << 18``) are not lost."""
    data = bytes(session.binfile.data)
    tbl_e = next((e for e in session.entities
                  if e.name == "USAp_dCodeTbl_C"), None)
    if tbl_e is None:
        return _coding_pairs(data)
    if tables is None:
        tables = find_variant_tables(session)
    out = {}
    for a in [tbl_e.addr] + [vt.sec_z for vt in tables
                             if vt.main_z == tbl_e.addr]:
        if a < 4 or a + 4 > len(data):
            continue
        v = _u32(data, a)
        if v and (v ^ _u32(data, a - 4)) == 0xFFFFFFFF:
            out.setdefault(v, a)
    return sorted(((a, v) for v, a in out.items()), key=lambda r: r[1])


def _func_code_bits(session, data):
    """How many low bits of a coding are function code (0: none)."""
    e = next((e for e in session.entities
              if e.name == "USAp_numFuncCodeBits_C"), None)
    n = data[e.addr] if e is not None and e.addr < len(data) else 0
    return n if 0 < n < 32 else 0


def _declared_types(session, data, tables):
    """``{coding: Gearbx_swtTypeVal_C}``: the gearbox type the dataset itself
    declares for each stored coding, read in the copy that coding selects."""
    ent = next((e for e in session.entities
                if e.name == "Gearbx_swtTypeVal_C"), None)
    if ent is None:
        return {}
    stored = [v for _a, v in stored_codings(session, tables)]
    copies = [vt.sec_z for vt in tables if vt.main_z == ent.addr]
    if not copies:                      # not switched: every coding reads it
        return {v: data[ent.addr] for v in stored}
    cc = copy_codings(session, tables)
    seen, clash = {}, set()
    for a in [ent.addr] + copies:
        for c in cc.get(a, ()):
            if seen.setdefault(c, data[a]) != data[a]:
                clash.add(c)
    return {c: t for c, t in seen.items() if c not in clash}


def describe_codings(session, tables=None):
    """``{coding: {transmission, ac, variant, function}}`` for every stored
    coding. ``variant`` and ``function`` (the value's variant number and its
    function-code bits) are None on datasets without function bits, where
    ``transmission`` and ``ac`` come from the decimal digit scheme -- checked
    against the dataset's own gearbox type, which wins a disagreement. On a
    function-coded dataset the transmission is the dataset's declared type and
    the A/C system is None: it lives in the function bits, which the car
    supplies, not in the file."""
    data = bytes(session.binfile.data)
    if tables is None:
        tables = find_variant_tables(session)
    bits = _func_code_bits(session, data)
    declared = _declared_types(session, data, tables)
    out = {}
    for _a, v in stored_codings(session, tables):
        decl = _TYPE_NAMES.get(declared.get(v))
        d = {"variant": None, "function": None}
        if bits:
            d["variant"] = v >> bits
            d["function"] = v & ((1 << bits) - 1)
            tx, ac = decl, None
        else:
            tx, ac = decode_coding(v)
            if decl is not None and (tx is None
                                     or _is_automatic(tx) != _is_automatic(decl)):
                tx = decl
        d["transmission"], d["ac"] = tx, ac
        out[v] = d
    return out


def coding_labels(text):
    """The transmission labels in a "coding ... (variant 3, automatic)" text,
    as :func:`coding_text` writes it."""
    import re
    m = re.search(r"\((.*)\)\s*$", text or "")
    if not m:
        return set()
    return {p.strip() for p in m.group(1).split(", ")
            if p.strip() and not p.startswith("variant ")}


def function_code_bits(session) -> int:
    """How many low bits of a coding are function code on this dataset (0:
    none -- a P350-style plain number). Cheap enough for a filter box: it is
    kept until the session's parameter list is replaced."""
    hit = getattr(session, "_func_bits", None)
    ents = getattr(session, "entities", None)
    if hit is not None and hit[0] is ents:
        return hit[1]
    try:
        bits = _func_code_bits(session, session.binfile.data)
    except Exception:                       # not an EDC16 session
        bits = 0
    try:
        session._func_bits = (ents, bits)
    except Exception:
        pass
    return bits


def coding_query(session, text: str) -> str:
    """A typed filter ``text`` with every coding number in it read the way the
    dataset reads a coding.

    A scan tool shows the car's *whole* coding. On a function-coded dataset
    (P447) that is the variant in the upper bits plus the function bits the
    car sets -- 786437, say, for variant 3 with function bits 5 -- while the
    file holds only the variant, 786432. So a typed ``coding 786437`` (or
    ``coding 0786437``, as a tool pads it) is rounded down to the value the
    file stores, and finds the copies variant 3 selects. A number that rounds
    to nothing is left alone, so a half-typed ``coding 7864`` still matches by
    its digits, and so is every number on a dataset without function bits,
    where the coding is stored exactly as shown. A bare number of five digits
    or more is taken as a coding when it names a stored one."""
    import re
    bits = function_code_bits(session)
    if not bits or not text:
        return text
    mask = (1 << bits) - 1

    def base(n):
        return n & ~mask or n               # below the lowest variant: as typed

    def fix_list(m):
        # a label's own "786432+" (the car's function bits come on top) reads
        # as the number it carries
        nums = [str(base(int(x))) if x else x
                for x in m.group(2).replace("+", "").split("/")]
        return m.group(1) + "/".join(nums)

    out = re.sub(r"(coding\s+)([\d/+]+)", fix_list, text)
    m = re.fullmatch(r"\s*(\d{5,})\s*", out)
    if m and base(int(m.group(1))) != int(m.group(1)):
        stored = {v for _a, v in stored_codings(session)}
        if base(int(m.group(1))) in stored:
            return "coding %d" % base(int(m.group(1)))
    return out


def gear_copy_codings(session):
    """Associate each physical gear-ratio copy with the coding that selects it,
    decided **structurally from the coding pointer array -- never from the ratio
    values** (auto and manual gearing is not a fixed numeric relationship).

    Returns ``{rTrans1_data_addr: (coding_value, transmission)}`` mapping each
    gear copy (identified by its ``Gearbx_rTrans1_C`` data address) to a coding:
    the main block's own for the primary copy, the lowest of those that select
    it for a variant copy (:func:`copy_codings` has them all).
    """
    data = bytes(session.binfile.data)
    by = {e.name: e for e in session.entities}
    gear_e = by.get("Gearbx_rTrans1_C")
    tbl_e = by.get("USAp_dCodeTbl_C")
    if gear_e is None:
        return {}
    tables = find_variant_tables(session)
    cc = copy_codings(session, tables)
    desc = describe_codings(session, tables)
    base = _read_code_at(data, tbl_e.addr) if tbl_e is not None else None

    def tx(code):
        return (desc.get(code) or {}).get("transmission") or "coding %d" % code

    out = {}
    for a in [gear_e.addr] + [vt.sec_z for vt in tables
                              if vt.main_z == gear_e.addr]:
        codes = sorted(cc.get(a, ()))
        if codes:
            code = base if (a == gear_e.addr and base in codes) else codes[0]
            out[a] = (code, tx(code))
    # no variant copies (a single-coding file): the main block's coding names it
    if gear_e.addr not in out and base is not None and base in desc:
        out[gear_e.addr] = (base, tx(base))
    return out


def _active_states(data, active, main_lo, main_hi):
    """The coding states in the active pointer array: a list of per-state
    slot lists (one state on single-state datasets). Same parse as the gear
    resolvers above: main-block pointers ascend within a state, so a big step
    back marks the start of the next state."""
    def ok(v):
        return main_lo <= v < main_hi or _SEARCH_LO <= v < main_lo
    entries = []
    o = active
    while o + 4 <= len(data):
        v = _u32(data, o)
        if not ok(v):
            break
        entries.append(v)
        o += 4
    resets, last = [0], 0
    for i, v in enumerate(entries):
        if main_lo <= v < main_hi:
            if v < last - 0x1000:
                resets.append(i)
            last = v
    stride = resets[1] if len(resets) > 1 else len(entries)
    if not stride:
        return []
    return [entries[k:k + stride]
            for k in range(0, len(entries) - stride + 1, stride)]


def copy_codings(session, tables=None):
    """``{copy data address: [coding values that select that copy]}`` for
    every coding-switched table and its primary. A primary copy always carries
    the base coding held in the main block's own ``USAp_dCodeTbl_C``, plus any
    state that keeps the table's slot in the main block.

    Decided from the active pointer array -- **never from the values**. Each
    state of the array is one stored coding: where a state points a slot into
    the secondary block, that coding uses the secondary copy of everything in
    the slot; where it points into the main block, the primary. A state's own
    coding value is read through the same mapping from ``USAp_dCodeTbl_C``.

    A single-state array cannot say this per coding; there the coding held in
    the main block's code table owns the primary, and the secondary copy goes
    to the other stored codings only when they all share one transmission
    (the rule ``gear_copy_codings`` already applies to the gear ratios).
    Returns {} when the dataset does not say.
    """
    data = bytes(session.binfile.data)
    main_lo, main_hi = session.region
    by = {e.name: e for e in session.entities}
    tbl_e = by.get("USAp_dCodeTbl_C")
    _default, active = _locate_arrays(data)
    if tbl_e is None or active is None:
        return {}
    if tables is None:
        tables = find_variant_tables(session)
    if not tables:
        return {}
    states = _active_states(data, active, main_lo, main_hi)
    out = {}

    def add(addr, code):
        lst = out.setdefault(addr, [])
        if code not in lst:
            lst.append(code)

    if len(states) >= 2:
        # Each state's own coding: the code-table slot is found in the first
        # state (exact pointer, or the secondary entry bracketing it between
        # main-block neighbours) and read through each state's pointer there
        # -- the same reading gear_copy_codings has always used.
        first = states[0]

        def in_main(v):
            return main_lo <= v < main_hi

        def slot_for(p):
            for i, v in enumerate(first):
                if v == p:
                    return i
                if not in_main(v):
                    lo = next((first[j] for j in range(i - 1, -1, -1)
                               if in_main(first[j])), 0)
                    hi = next((first[j] for j in range(i + 1, len(first))
                               if in_main(first[j])), 0)
                    if lo <= p < hi:
                        return i
            return None

        ic = slot_for(tbl_e.addr)
        if ic is None:
            return {}
        base = _read_code_at(data, tbl_e.addr)
        sec_sorted = sorted({v for st in states for v in st
                             if not in_main(v)})
        # where the code table sits in its block: a state that keeps the slot
        # in the main block names the block's start there
        kept = next((st[ic] for st in states
                     if in_main(st[ic]) and st[ic] <= tbl_e.addr), None)
        table_copies = [vt.sec_z for vt in tables if vt.main_z == tbl_e.addr]

        def state_code(p):
            """The coding a state holds: the code table's copy in the block
            its slot points at. Reading at the block's first word instead
            gave whatever else starts it (03G906016LQ: every state came out
            as the main block's coding)."""
            if in_main(p):
                return base
            if kept is not None:
                return _read_code_at(data, p + (tbl_e.addr - kept))
            j = bisect.bisect_right(sec_sorted, p)
            end = sec_sorted[j] if j < len(sec_sorted) else p + 0x1000
            at = next((a for a in table_copies if p <= a < end), p)
            return _read_code_at(data, at)

        codes = [state_code(st[ic]) for st in states]
        # A secondary copy belongs to the relocated block it sits in: the
        # nearest secondary pointer at or below it. The states that point at
        # that block are the codings that select the copy.
        sec_ptrs = sorted({v for st in states for v in st if not in_main(v)})
        users = {}
        for k, st in enumerate(states):
            for v in st:
                if not in_main(v):
                    users.setdefault(v, set()).add(codes[k])
        for vt in tables:
            j = bisect.bisect_right(sec_ptrs, vt.sec_z) - 1
            if j < 0:
                continue
            # the main block's own code table names the base coding: every
            # primary copy is its calibration (the base-coding rule the gear
            # resolver uses too)
            add(vt.main_z, base)
            blk = sec_ptrs[j]
            for c in sorted(users.get(blk, ())):
                add(vt.sec_z, c)
            # The primary copy belongs to the states that keep that slot in
            # the main block -- the slot is wherever a state points at the
            # relocated block.
            slots = {k for st in states for k, v in enumerate(st) if v == blk}
            for k2, st in enumerate(states):
                if any(k < len(st) and in_main(st[k]) for k in slots):
                    add(vt.main_z, codes[k2])
        return out

    # single state: the main block's code table owns the primary copies. The
    # transmission of a stored coding comes from the digit scheme, which holds
    # only where there are no function code bits; with them, the codings are
    # not told apart by transmission here and no copy is given to the others.
    if _func_code_bits(session, data):
        tx_by_code = {v: "coding %d" % v
                      for _a, v in stored_codings(session, tables)}
    else:
        tx_by_code = {v: (decode_coding(v)[0] or "coding %d" % v)
                      for _a, v in stored_codings(session, tables)}
    cur = _read_code_at(data, tbl_e.addr)
    if cur not in tx_by_code:
        return {}
    cur_tx = tx_by_code[cur]
    mine = [v for v, tx in tx_by_code.items() if tx == cur_tx]
    others = [v for v, tx in tx_by_code.items() if tx != cur_tx]
    if len({tx_by_code[v] for v in others}) != 1:
        others = []
    for vt in tables:
        for v in mine:
            add(vt.main_z, v)
        for v in others:
            add(vt.sec_z, v)
    return out


def coding_text(codes, desc=None) -> str:
    """"coding 150031/158031 (5-speed manual)" -- the coding values that
    select a copy, then what they say (``desc`` is :func:`describe_codings`):
    the variant number where the dataset has function bits, and the
    transmission; the A/C system only where no transmission is named. Without
    ``desc`` the P350 digit scheme (:func:`decode_coding`) reads them. Kept
    short: it rides on the end of a tree label."""
    if not codes:
        return ""
    codes = sorted(codes)

    def read(c):
        if desc is None:
            tx, ac = decode_coding(c)
            return {"transmission": tx, "ac": ac, "variant": None}
        return desc.get(c) or {}

    parts = []
    variants = sorted({read(c)["variant"] for c in codes
                       if read(c).get("variant") is not None})
    if variants:
        parts.append("variant " + "/".join("%d" % x for x in variants))
    for c in codes:
        d = read(c)
        x = d.get("transmission") or d.get("ac")
        if x and x not in parts:
            parts.append(x)
    # a function-coded dataset stores only the variant: the car adds its
    # function bits, so a scan tool shows a larger number -- the "+" says so
    plus = "+" if variants else ""
    return "coding %s%s" % ("/".join("%d%s" % (c, plus) for c in codes),
                            (" (%s)" % ", ".join(parts)) if parts else "")


def coding_states(session):
    """All coding values stored in the flash, decoded. One dict per value:
    ``{coding, coding_hex, inv_ok, transmission, ac, gearbox, gears, tbl_addr,
    variant, function}`` (see :func:`describe_codings` for how a value reads;
    ``variant`` / ``function`` are None without function code bits).
    ``gearbox`` is a compact label kept for back-compat (automatic / manual ...).
    """
    data = bytes(session.binfile.data)
    tables = find_variant_tables(session)
    desc = describe_codings(session, tables)
    # the gear ratios each coding selects: the copy of Gearbx_rTrans1_C its
    # state reads (the main one for the codings that keep the slot)
    by = {e.name: e for e in session.entities}
    gear_e = by.get("Gearbx_rTrans1_C")
    gears_by = {}
    if gear_e is not None:
        cc = copy_codings(session, tables)
        for a in [gear_e.addr] + [vt.sec_z for vt in tables
                                  if vt.main_z == gear_e.addr]:
            g = [round(_u16(data, a + 2 * j) / 100.0, 2) for j in range(6)]
            for c in cc.get(a, ()):
                gears_by.setdefault(c, g)
    out = []
    for addr, v in stored_codings(session, tables):
        d = desc.get(v) or {}
        tx, ac = d.get("transmission"), d.get("ac")
        if tx and (tx.startswith(("5-speed", "6-speed")) or tx == "manual"):
            gearbox = "manual"
        elif tx in ("CVT", "DSG"):
            gearbox = tx
        elif tx and tx.startswith("automatic"):
            gearbox = "automatic"
        else:
            gearbox = "unknown"
        out.append(dict(coding=v, coding_hex="0x%08X" % v, inv_ok=True,
                        transmission=tx or "unknown",
                        ac=ac or ("in the function code"
                                  if d.get("variant") is not None
                                  else "unknown"),
                        gearbox=gearbox, gears=gears_by.get(v), tbl_addr=addr,
                        variant=d.get("variant"),
                        function=d.get("function")))
    out.sort(key=lambda c: c["coding"])
    return out


# ---- VSS (vehicle-speed sensor) pulse-scaling settings -------------------
# VSSCD_ImpPerDist_C ("Streckenfaktor: FGG-Impulse pro Strecke") is the
# impulses-per-metre constant the ECU uses to turn the speed-sensor pulse
# train into road speed (v[km/h] = 3.6 * f[Hz] / ImpPerDist; physical =
# 3.6e9 / raw, uint32 big-endian). On a multi-gearbox dataset it is
# coding-selected: a manual/other gearbox variant is held in the secondary
# block and picked by the active-coding state array, so its pulse count can
# differ from the active primary.
_VSS_IMP = "VSSCD_ImpPerDist_C"
_VSS_SRC = "VSSCD_swtSigVal_C"
_VSS_STYP = "VSSCD_swtSigType_C"
# valid ImpPerDist raw range (physical 2.5 .. 20 Imp/m); used only as a
# validity gate to disambiguate the ImpPerDist redirect from a neighbouring
# parameter's redirect -- not as a value match, so any realistic (in-range)
# retune is still found.
_VSS_RAW_LO, _VSS_RAW_HI = 180_000_000, 1_440_000_000


def _coding_redirects(data, arr_base, main_lo, main_hi, target, valid):
    """Coding-selected redirect addresses for the main-block ``target``, read
    from one coding address-array.

    The active/default coding arrays are sorted lists of switched parameter
    addresses; a secondary-region entry redirects the main-block slot it sits in
    (bounded by the nearest main-block entries around it). Any secondary entry
    whose bracket ``[lo, hi)`` covers ``target`` and whose stored value passes
    ``valid`` is a coding variant of ``target``. This is resolved by *address*
    (bracket position), so it is value-agnostic and copes with a **compact**
    variant block (only a couple of parameters switched, e.g. when both gearboxes
    share the speed source) as well as a full relocated block. Returns the sec
    addresses, de-duplicated."""
    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    entries, o = [], arr_base
    while o + 4 <= len(data):
        v = _u32(data, o)
        if in_main(v) or in_sec(v):
            entries.append(v)
            o += 4
        else:
            break
    n = len(entries)
    out, seen = [], set()
    for i, v in enumerate(entries):
        if not in_sec(v):
            continue
        lo = next((entries[q] for q in range(i - 1, -1, -1)
                   if in_main(entries[q])), 0)
        hi = next((entries[q] for q in range(i + 1, n)
                   if in_main(entries[q])), 1 << 30)
        if lo <= target < hi and 0 <= v < len(data) and v not in seen \
                and valid(v):
            seen.add(v)
            out.append(v)
    return out


class _Pair:
    """A primary copy and one coding-selected copy of it, as
    :func:`copy_codings` takes them (``main_z`` / ``sec_z``)."""
    def __init__(self, main_z, sec_z):
        self.main_z, self.sec_z = main_z, sec_z


def row_transmissions(session, rows):
    """The transmission each of ``rows`` (``vss_pulse_settings``' dicts) is
    read for, in order -- ``["automatic (TC)", "5-speed manual"]`` -- or None
    when the coding arrays do not give exactly one per row, or two rows
    come out the same. Resolved as the gear ratios are (:func:`copy_codings`,
    from the active pointer array, never from the values): a variant copy
    belongs to the codings that point its block's slot there, the primary to
    the codings every variant slot leaves in the main block."""
    prim = next((r["addr"] for r in rows if r["primary"]), None)
    variants = [r["addr"] for r in rows if not r["primary"]]
    if prim is None or not variants:
        return None
    codes = {}
    prim_codes = None
    for a in variants:
        got = copy_codings(session, [_Pair(prim, a)])
        if a not in got or not got.get(prim):
            return None
        codes[a] = got[a]
        mine = set(got[prim])
        prim_codes = mine if prim_codes is None else prim_codes & mine
    codes[prim] = sorted(prim_codes or ())

    desc = describe_codings(session)

    def tx(cs):
        names = {(desc.get(c) or {}).get("transmission") or "coding %d" % c
                 for c in cs}
        return next(iter(names)) if len(names) == 1 else None
    out = [tx(codes.get(r["addr"], ())) for r in rows]
    if None in out or len(set(out)) != len(out):
        return None
    return out


def vss_pulse_settings(session) -> List[dict]:
    """The VSS pulse-scaling constant and every coding-selected variant copy.

    The primary ``VSSCD_ImpPerDist_C`` comes from the knowledge base; the coding
    variants are resolved through the coding address-arrays by *bracket position*
    (see :func:`_coding_redirects`). That is value-agnostic -- it finds a variant
    at any in-range pulse count and is not fooled by a coincidental value match
    -- and it handles a **compact** variant block (only ImpPerDist switched, when
    both gearboxes share the speed source, e.g. the P350 auto+manual files) as
    well as a full relocated block (e.g. the DSG+manual file that flips
    sensor<->CAN). The speed source is resolved the same way: ``VSSCD_swtSigVal_C``
    is itself coding-switched only when the gearboxes differ in source.

    Each dict: ``addr``, ``raw``, ``imp_m``, ``imp_km``, ``src`` ("HW sensor" |
    "CAN" | "?"), ``primary``. Empty if the KB has no VSSCD entry.
    """
    ents = {e.name: e for e in
            list(session.entities) + list(session.variant_entities())}
    imp = ents.get(_VSS_IMP)
    if imp is None:
        return []
    data = bytes(session.binfile.data)
    main_lo, main_hi = session.region
    src = ents.get(_VSS_SRC)
    default, active = _locate_arrays(data)

    def gather(target, valid):
        found, seen = [], set()
        for base in (active, default):
            if base is None:
                continue
            for a in _coding_redirects(data, base, main_lo, main_hi,
                                       target, valid):
                if a not in seen:
                    seen.add(a)
                    found.append(a)
        return found

    imp_addrs = [imp.addr] + gather(
        imp.addr, lambda a: _VSS_RAW_LO <= _u32(data, a) <= _VSS_RAW_HI)

    off_sv = (src.addr - imp.addr) if src is not None else None
    prim_src = data[src.addr] if (src is not None and 0 <= src.addr < len(data)) \
        else None
    win = 0x4C
    ref = data[imp.addr - win + 4:imp.addr + 4]

    def source(a, primary):
        """(text, src_addr, src_val). src_addr is the swtSigVal byte that
        governs this config's source, or None when the config shares the
        primary's source (a compact variant) and so has no own switch byte."""
        if src is None:
            return "?", None, None
        if primary:
            return ({0: "HW sensor", 1: "CAN"}.get(prim_src, "?"),
                    src.addr, prim_src)
        # a full relocated block carries its own swtSigVal at the same
        # in-block offset; a compact variant (only ImpPerDist switched)
        # shares the primary source. Tell them apart by block similarity.
        seg = data[a - win + 4:a + 4]
        full = (len(seg) == len(ref)
                and sum(x == y for x, y in zip(seg, ref)) >= 0.5 * len(ref))
        b = a + off_sv if off_sv is not None else None
        if full and b is not None and 0 <= b < len(data):
            return {0: "HW sensor", 1: "CAN"}.get(data[b], "?"), b, data[b]
        return {0: "HW sensor", 1: "CAN"}.get(prim_src, "?"), None, prim_src

    styp = ents.get(_VSS_STYP)
    styp_addr = styp.addr if styp is not None else None
    out = []
    for i, a in enumerate(imp_addrs):
        raw = _u32(data, a)
        impm = 3.6e9 / raw if raw else 0.0
        stext, saddr, sval = source(a, i == 0)
        out.append(dict(addr=a, raw=raw, imp_m=round(impm, 4),
                        imp_km=round(impm * 1000), src=stext,
                        src_addr=saddr, src_val=sval, styp_addr=styp_addr,
                        primary=(i == 0)))
    return out
