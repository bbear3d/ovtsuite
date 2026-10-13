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

Three ways to find them, tried in order:

1. **Pointer-array diff (exact, full reads).** A ``0xCAFEAFFE`` descriptor at
   ~0x180040 references a *default* pointer array (all-primary) and an *active*
   array (@0x180084). Where ``active[i]`` is a secondary address and its
   default the primary, that is an exact (primary, secondary) pair -- including
   single-value constants. Needs the program area, so it only fires on full
   reads that contain the default array.

2. **Active-array anchor alignment (exact, calibration-only reads).** On an
   OBD/calibration read the program area (and thus the default array) is blank,
   but the *active* array @0x180084 is present: a table of primary addresses in
   sorted order, with the switched entries redirected into the secondary block.
   Each redirected entry's secondary target is a verbatim relocated copy of a
   contiguous main-block range; aligning that copy back onto the main block by
   byte match recovers the exact primary base, and every catalog parameter in
   the aligned span -- **including value constants like the gear ratios** -- is
   paired to its secondary copy.

3. **Axis fingerprint (fallback).** Maps and curves are additionally paired to a
   main-block twin by identical axis bytes; this catches structured tables even
   where the active array is unavailable, but cannot see value constants.

``coding_domain`` inspects the paired names and reports the coding condition
(e.g. "gearbox coding (automatic vs manual)").
"""

import bisect
import dataclasses
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional

from .structure import scan

_SEARCH_LO = 0x182000
_CAFEAFFE = b"\xCA\xFE\xAF\xFE"
# Minimum number of parameters that must differ for an array-paired region to
# count as a real coding block (fewer is incidental relocation noise).
_MIN_CODING_DIFFERS = 3

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
    method: str          # 'array' (exact) | 'anchor' (exact) | 'axis' (fallback)
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

    # ---- method 1: exact pointer-array diff (needs the default array) --------
    # The default/active pointer arrays give the *exact* set of switched
    # (primary -> secondary) blocks. Each switched block is a verbatim
    # relocation of a contiguous main-block span, so every catalog parameter
    # inside that span -- including value constants like the gear ratios -- has
    # a secondary copy at the same offset. We therefore *project* each catalog
    # entity within a paired span to its secondary twin, instead of emitting one
    # coarse table per pointer (which would hide the individual constants inside
    # a switched structure). Only the exact array pairs drive this, so no
    # spurious variant tables appear on binaries that have no real coding block.
    if default is not None and active is not None:
        pairs = []
        n = 0
        while (default + n * 4 + 4 <= len(data)
               and active + n * 4 + 4 <= len(data)):
            base = _u32(data, default + n * 4)
            if not (0x1000 <= base < 0x200000):
                break
            act = _u32(data, active + n * 4)
            n += 1
            if act == base:
                continue
            if not (_SEARCH_LO <= act < main_lo and main_lo <= base < main_hi):
                continue
            pairs.append((base, act))

        # block length of a switched span = gap to the next secondary target
        acts = sorted(a for _, a in pairs)

        def _blk_len(act):
            i = bisect.bisect_right(acts, act)
            nxt = acts[i] if i < len(acts) else None
            if nxt is not None and 0 < nxt - act <= 0x400:
                return nxt - act
            return 0x40

        sec_structs = []
        for a, b in find_secondary_regions(data, main_lo):
            sec_structs += scan(data, a, b)
        sec_by_hdr = _found_by_header(sec_structs)
        by_z = {e.addr: e for e in session.entities}
        kb_addrs = sorted(by_z)

        out = []
        seen = set()
        for base, act in pairs:
            blk = _blk_len(act)
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
        # A real coding block switches many parameters at once; a lone differing
        # table (seen on binaries with no coding variant, where the descriptor
        # points at incidental relocated data) is noise, so require several.
        if sum(1 for v in out if v.differs) < _MIN_CODING_DIFFERS:
            return []
        return out

    # ---- calibration-only read: anchor alignment (exact) + axis (fallback) ---
    anchor = _anchor_value_variants(session, data, main_lo, main_hi, active)
    axis = _axis_variants(data, main_lo, main_structs, name_by_z, a2l,
                          min_structs)
    # One entry per COPY: a table relocated by three coding states has three
    # copies, so de-duplicating on the main table dropped the other states'.
    seen = {v.sec_z for v in anchor}
    merged = list(anchor) + [v for v in axis if v.sec_z not in seen]
    merged = _check_against_blocks(session, data, main_lo, main_hi, active,
                                   merged, main_structs, name_by_z, a2l)
    merged = _drop_cross_block(session, data, main_lo, main_hi, active, merged)
    merged.sort(key=lambda v: v.sec_z)
    # A real coding block carries at least one parameter that actually takes a
    # different value between codings (a byte-identical set is just the same
    # data relocated, not a coding alternative). Small real blocks -- e.g. the
    # 3-parameter A/C-type coding on some datasets -- are kept.
    return merged if any(v.differs for v in merged) else []


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


def _check_against_blocks(session, data, main_lo, main_hi, active, found,
                          main_structs, name_by_z, a2l):
    """Hold every found copy to the coding pointer array.

    Each secondary pointer in the active array starts a relocated block, and
    the block is a verbatim copy of the main span its slot points at in a
    state that keeps the main block. So a copy at ``sec`` in the block at
    ``P`` IS the main table at ``M + (sec - P)``. On 038906016Q that held for
    all 635 copies where a state keeps the slot in main.

    Where every state relocates the slot there is no ``M``; the block's
    offset is then the one most of its copies agree on (a verbatim copy has
    one offset for all of them).

    A copy found at the wrong main table is renamed to the right one when a
    main table starts there, and dropped when none does: an anchor overrun
    on 038906016Q put an "AirCtl_facMaxDevCor_MAP" copy inside the next
    state's AirCtl block, overlapping its ATCor maps.
    """
    if active is None or not found:
        return found
    states = _active_states(data, active, main_lo, main_hi)

    def in_main(v):
        return main_lo <= v < main_hi

    base = {}
    for st in states:
        for k, x in enumerate(st):
            if not in_main(x) and x not in base:
                kept = [t[k] for t in states if k < len(t) and in_main(t[k])]
                base[x] = kept[0] if kept else None
    starts = sorted(base)
    if not starts:
        return found

    def block_of(v):
        j = bisect.bisect_right(starts, v.sec_z) - 1
        return starts[j] if j >= 0 else None

    # the agreed offset of blocks no state keeps in main
    from collections import Counter
    offs = {}
    for p in starts:
        if base[p] is None:
            c = Counter(v.sec_z - v.main_z for v in found
                        if block_of(v) == p and not v.ambiguous)
            if c:
                (d, n), = c.most_common(1)
                if n >= 2 or len(c) == 1:
                    offs[p] = d

    by_z = {e.addr: e for e in session.entities}
    struct_z = {f.z_addr: f for f in main_structs}
    # A "table" that starts inside a known table's data is a scan artefact
    # (038906016Q: an axis match at 0x1D25FA, inside
    # Gearbx_dtrqRmpSlpDwnINC_C), not a copy of anything.
    spans = sorted((e.addr, e.addr + e.n_cells * e.step) for e in by_z.values())
    starts_e = [a for a, _b in spans]

    def inside_other(z):
        if z in by_z:
            return False
        j = bisect.bisect_right(starts_e, z) - 1
        return j >= 0 and z < spans[j][1]

    out = []
    for v in found:
        if inside_other(v.main_z):
            continue
        p = block_of(v)
        if p is None:
            out.append(v)
            continue
        if base[p] is not None:
            want = base[p] + (v.sec_z - p)
        elif p in offs:
            want = v.sec_z - offs[p]
        else:
            out.append(v)
            continue
        if want == v.main_z:
            if v.ambiguous:
                v = dataclasses.replace(v, ambiguous=False)
            out.append(v)
            continue
        e = by_z.get(want)
        f = struct_z.get(want)
        if e is None and f is None:
            continue                    # nothing starts there: not a copy
        if e is not None:
            name, kind, rows, cols = e.name, e.kind, e.rows, e.cols
            span = e.n_cells * e.step
            kind = "value" if kind in ("constant", "indexed") else kind
        else:
            name = name_by_z.get(want) or a2l.get(f.addr) or "@0x%X" % want
            kind, rows, cols = f.kind, f.rows, f.cols
            span = f.n_cells * 2
        differs = data[want:want + span] != data[v.sec_z:v.sec_z + span]
        out.append(VariantTable(
            name=name, kind=kind, rows=rows, cols=cols, sec_z=v.sec_z,
            main_z=want, differs=differs, method=v.method))
    return out


# ---- method 2: active-array anchor alignment -----------------------------

def _anchor(data, sec, lo, hi, win=0x40, thresh=0.6):
    """Best main-block base for the secondary copy at ``sec``, found by byte
    match over the sorted-neighbour window [lo, hi]. The switched copy is a
    verbatim relocation of a contiguous main range, so most bytes (all the
    untuned ones plus the structural header) match at the true base. Returns
    (base, score) or (None, 0) if nothing clears the threshold."""
    n = len(data)
    lo2 = max(0, lo - 16)
    hi2 = min(hi, lo + 0x400) + 16
    hi2 = min(hi2, n - win)
    best_p, best = None, -1
    for p in range(lo2, hi2):
        s = 0
        for k in range(win):
            if data[sec + k] == data[p + k]:
                s += 1
        if s > best:
            best, best_p = s, p
    if best < int(thresh * win):
        return None, 0
    return best_p, best


def _anchor_value_variants(session, data, main_lo, main_hi, active):
    """Pair switched parameters -- including value constants -- via the active
    redirect array, on calibration-only reads where the default array is blank.
    Empty unless a real ``0xCAFEAFFE`` active array with secondary redirects is
    present."""
    if active is None:
        return []
    sec_lo, sec_hi = _SEARCH_LO, main_lo

    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return sec_lo <= v < sec_hi

    # read the (possibly concatenated) sorted address arrays that follow
    entries = []
    o = active
    while o + 4 <= len(data):
        v = _u32(data, o)
        if in_main(v) or in_sec(v):
            entries.append(v)
            o += 4
        else:
            break
    n = len(entries)
    if n < 50:
        return []

    by_z = {e.addr: e for e in session.entities}
    kb_addrs = sorted(by_z)

    out = []
    seen = set()
    for idx in range(n):
        v = entries[idx]
        if not in_sec(v):
            continue
        # sorted-neighbour primaries bracket the switched slot's primary
        lo = next((entries[j] for j in range(idx - 1, -1, -1)
                   if in_main(entries[j])), None)
        hi = next((entries[j] for j in range(idx + 1, n)
                   if in_main(entries[j])), None)
        if lo is None or hi is None or not (lo < hi):
            continue
        base, score = _anchor(data, v, lo, hi)
        if base is None:
            continue
        # block length: contiguous copies keep layout, so the gap to the next
        # redirected target is the block's byte length
        nxt = next((entries[j] for j in range(idx + 1, n)
                    if in_sec(entries[j])), None)
        if nxt is not None and 0 < nxt - v <= 0x400:
            blk_len = nxt - v
        else:
            blk_len = min(hi - base, 0x400)
        blk_len = max(blk_len, 2)
        # project every catalog parameter that lands in the aligned span
        l = bisect.bisect_left(kb_addrs, base)
        r = bisect.bisect_left(kb_addrs, base + blk_len)
        for a in kb_addrs[l:r]:
            e = by_z[a]
            sec_z = v + (a - base)
            span = e.n_cells * e.step
            if sec_z + span > len(data) or not in_sec(sec_z):
                continue
            key = (a, sec_z)
            if key in seen:
                continue
            seen.add(key)
            differs = (data[a:a + span] != data[sec_z:sec_z + span])
            kind = "value" if e.kind in ("constant", "indexed") else e.kind
            out.append(VariantTable(
                name=e.name, kind=kind, rows=e.rows, cols=e.cols,
                sec_z=sec_z, main_z=a, differs=differs, method="anchor"))
    return out


# ---- method 3: axis fingerprint (fallback) -------------------------------

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

# Coding digit scheme (VAG EDC16, confirmed against VCDS on these datasets):
# the last decimal digit is the transmission, the second-to-last is the A/C
# system. Higher digits encode further options (market/engine/etc.).
_TX = {"1": "5-speed manual", "2": "6-speed manual", "4": "automatic (TC)",
       "6": "CVT", "8": "DSG"}
_AC = {"3": "manual A/C", "7": "Climatronic"}


def _u16(d, o):
    return (d[o] << 8) | d[o + 1]


def decode_coding(value):
    """(transmission, a/c) implied by a coding value's last two decimal digits,
    or (None, None) for the unknown parts."""
    s = "%d" % value
    d0 = s[-1]
    d1 = s[-2] if len(s) >= 2 else "0"
    return _TX.get(d0), _AC.get(d1)


def _coding_pairs(data):
    """Every {value, inverse} coding pair stored in the calibration region.
    Codings are held as a 32-bit value immediately preceded by its bitwise
    complement (integrity twin); such adjacency is astronomically unlikely by
    chance, so the pair itself is the signature. Returns [(tbl_addr, value)],
    de-duplicated by value, lowest address kept."""
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


def _gearset_by_coding(session):
    """{coding_value: (rTrans1..6, primary_gears)} for codings that a gearbox
    pointer-array state selects -- lets a coding be tied to the gears it picks."""
    data = bytes(session.binfile.data)
    main_lo, main_hi = session.region
    by = {e.name: e for e in session.entities}
    tbl_e = by.get("USAp_dCodeTbl_C")
    gear_e = by.get("Gearbx_rTrans1_C")
    _, active = _locate_arrays(data)
    if tbl_e is None or gear_e is None or active is None:
        return {}
    tbl_p, gear_p = tbl_e.addr, gear_e.addr

    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    entries = []
    o = active
    while o + 4 <= len(data):
        v = _u32(data, o)
        if in_main(v) or in_sec(v):
            entries.append(v)
            o += 4
        else:
            break
    resets = [0]
    last = 0
    for i, v in enumerate(entries):
        if in_main(v):
            if v < last - 0x1000:
                resets.append(i)
            last = v
    if len(resets) < 2:
        return {}
    stride = resets[1]
    nstates = len(entries) // stride

    def slot_for(primary):
        for i in range(stride):
            v = entries[i]
            if v == primary:
                return i
            if in_sec(v):
                lo = next((entries[j] for j in range(i - 1, -1, -1)
                           if in_main(entries[j])), 0)
                hi = next((entries[j] for j in range(i + 1, stride)
                           if in_main(entries[j])), 0)
                if lo <= primary < hi:
                    return i
        return None

    ic = slot_for(tbl_p)
    ig = slot_for(gear_p)
    if ic is None or ig is None:
        return {}
    gbase = next((_u32(data, active + kk * stride * 4 + 4 * ig)
                  for kk in range(nstates)
                  if in_main(_u32(data, active + kk * stride * 4 + 4 * ig))), None)

    def read_code(ptr):
        v = _u32(data, ptr)
        nxt = _u32(data, ptr + 4)
        prv = _u32(data, ptr - 4)
        if ((v ^ nxt) & 0xFFFFFFFF) == 0xFFFFFFFF:
            return min((v, nxt))
        if ((v ^ prv) & 0xFFFFFFFF) == 0xFFFFFFFF:
            return min((v, prv))
        return v

    out = {}
    # base coding (main-block value) selects the primary gearset
    base_code = read_code(tbl_p)
    out[base_code] = ([round(_u16(data, gear_p + 2 * j) / 100.0, 2)
                       for j in range(6)], True)
    for k in range(nstates):
        ab = active + k * stride * 4
        code = read_code(_u32(data, ab + 4 * ic))
        gp = _u32(data, ab + 4 * ig)
        if in_main(gp):
            gz, prim = gear_p, True
        elif in_sec(gp) and gbase is not None:
            gz, prim = gp + (gear_p - gbase), False
        else:
            gz, prim = None, True
        gears = [round(_u16(data, gz + 2 * j) / 100.0, 2)
                 for j in range(6)] if gz else None
        out.setdefault(code, (gears, prim))
    return out


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


def _gear_addr_by_coding_multistate(session, data, gear_e, tbl_e, active,
                                    main_lo, main_hi):
    """{coding_value: rTrans1_data_addr} from a *multi-state* active array
    (several concatenated coding states), or {} when only one state is present.
    Purely structural: it follows each state's pointer to the gear block."""
    if tbl_e is None:
        return {}
    tbl_p, gear_p = tbl_e.addr, gear_e.addr

    def in_main(v):
        return main_lo <= v < main_hi

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    entries = []
    o = active
    while o + 4 <= len(data):
        v = _u32(data, o)
        if in_main(v) or in_sec(v):
            entries.append(v)
            o += 4
        else:
            break
    resets = [0]
    last = 0
    for i, v in enumerate(entries):
        if in_main(v):
            if v < last - 0x1000:
                resets.append(i)
            last = v
    if len(resets) < 2:
        return {}                       # single state -> caller uses fallback
    stride = resets[1]
    nstates = len(entries) // stride

    def slot_for(primary):
        for i in range(stride):
            v = entries[i]
            if v == primary:
                return i
            if in_sec(v):
                lo = next((entries[j] for j in range(i - 1, -1, -1)
                           if in_main(entries[j])), 0)
                hi = next((entries[j] for j in range(i + 1, stride)
                           if in_main(entries[j])), 0)
                if lo <= primary < hi:
                    return i
        return None

    ic = slot_for(tbl_p)
    ig = slot_for(gear_p)
    if ic is None or ig is None:
        return {}
    gbase = next((_u32(data, active + kk * stride * 4 + 4 * ig)
                  for kk in range(nstates)
                  if in_main(_u32(data, active + kk * stride * 4 + 4 * ig))),
                 None)
    out = {}
    out[_read_code_at(data, tbl_p)] = gear_p        # base coding -> primary
    for k in range(nstates):
        ab = active + k * stride * 4
        code = _read_code_at(data, _u32(data, ab + 4 * ic))
        gp = _u32(data, ab + 4 * ig)
        if in_main(gp):
            gz = gear_p
        elif in_sec(gp) and gbase is not None:
            gz = gp + (gear_p - gbase)
        else:
            continue
        out.setdefault(code, gz)
    return out


def gear_copy_codings(session):
    """Associate each physical gear-ratio copy with the coding that selects it,
    decided **structurally from the coding pointer array -- never from the ratio
    values** (auto and manual gearing is not a fixed numeric relationship).

    Returns ``{rTrans1_data_addr: (coding_value, transmission)}`` mapping each
    gear copy (identified by its ``Gearbx_rTrans1_C`` data address) to a coding.
    """
    data = bytes(session.binfile.data)
    main_lo, main_hi = session.region
    by = {e.name: e for e in session.entities}
    gear_e = by.get("Gearbx_rTrans1_C")
    tbl_e = by.get("USAp_dCodeTbl_C")
    default, active = _locate_arrays(data)
    if gear_e is None or active is None:
        return {}
    gear_p = gear_e.addr

    def in_sec(v):
        return _SEARCH_LO <= v < main_lo

    tx_by_code = {}
    for _addr, v in _coding_pairs(data):
        tx, _ac = decode_coding(v)
        tx_by_code[v] = tx or ("coding %d" % v)

    # (1) multi-state active array (e.g. P350 038xxx): resolve every state
    ms = _gear_addr_by_coding_multistate(session, data, gear_e, tbl_e, active,
                                         main_lo, main_hi)
    if ms:
        return {addr: (code, tx_by_code.get(code, "coding %d" % code))
                for code, addr in ms.items()}

    # (2) single active state (e.g. M58): the active array holds only one
    # coding state, so the per-state resolution above can't run. Fall back to
    # the invariant the multi-state resolver also uses -- the coding stored in
    # USAp_dCodeTbl is the one the *main block* (primary) holds -- and give the
    # secondary copy the remaining transmission. This is decided from the
    # coding table, never from the ratio values.
    if tbl_e is None:
        return {}
    sec_addr = None
    for vt in find_variant_tables(session):
        if vt.name == "Gearbx_rTrans1_C":
            sec_addr = vt.sec_z
            break
    cur_code = _read_code_at(data, tbl_e.addr)
    cur_tx = tx_by_code.get(cur_code)
    if cur_code not in tx_by_code:
        return {}
    out = {gear_p: (cur_code, cur_tx)}          # primary = USAp_dCodeTbl coding
    if sec_addr is not None:
        # the secondary copy takes the other transmission present in the codings
        others = [(v, tx) for v, tx in tx_by_code.items() if tx != cur_tx]
        if len({tx for _v, tx in others}) == 1:
            out[sec_addr] = others[0]
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
        codes = [_read_code_at(data, st[ic]) for st in states]
        base = _read_code_at(data, tbl_e.addr)
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

    # single state: the main block's code table owns the primary copies
    tx_by_code = {v: (decode_coding(v)[0] or "coding %d" % v)
                  for _a, v in _coding_pairs(data)}
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


def coding_text(codes) -> str:
    """"coding 150031/158031 (5-speed manual)" -- the coding values that
    select a copy, then the transmission their last digit names (VAG scheme,
    see ``decode_coding``); the A/C digit only where no transmission is
    named. Kept short: it rides on the end of a tree label."""
    if not codes:
        return ""
    codes = sorted(codes)
    desc = []
    for c in codes:
        tx, ac = decode_coding(c)
        d = tx or ac
        if d and d not in desc:
            desc.append(d)
    return "coding %s%s" % ("/".join("%d" % c for c in codes),
                            (" (%s)" % ", ".join(desc)) if desc else "")


def coding_states(session):
    """All coding values stored in the flash, decoded. One dict per value:
    ``{coding, coding_hex, inv_ok, transmission, ac, gearbox, gears, tbl_addr}``.
    ``gearbox`` is a compact label kept for back-compat (automatic / manual ...).
    """
    data = bytes(session.binfile.data)
    pairs = _coding_pairs(data)
    gset = _gearset_by_coding(session)
    out = []
    for addr, v in pairs:
        tx, ac = decode_coding(v)
        gears = gset.get(v, (None, None))[0]
        if tx and tx.startswith(("5-speed", "6-speed")):
            gearbox = "manual"
        elif tx == "automatic (TC)":
            gearbox = "automatic"
        elif tx in ("CVT", "DSG"):
            gearbox = tx
        else:
            gearbox = "unknown"
        out.append(dict(coding=v, coding_hex="0x%08X" % v, inv_ok=True,
                        transmission=tx or "unknown", ac=ac or "unknown",
                        gearbox=gearbox, gears=gears, tbl_addr=addr))
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
