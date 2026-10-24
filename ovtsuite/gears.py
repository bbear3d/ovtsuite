# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Grouped drivetrain-ratio model for the gear-ratio editor.

The ECU stores the drivetrain ratios as many single-value constants:
``Gearbx_rTrans1_C`` .. ``Gearbx_rTrans6_C`` (per-gear OVERALL ratios --
gearbox x final drive, e.g. 11.34 / 6.18 / 4.36 / 3.09 / 2.29 / 1.91 on a
038997016T, so there is no separate axle constant in the gear-detection
model), ``Gearbx_rTransR_C`` (reverse), ``Gearbx_rAWDLo_C`` (transfer-case
reduction, "Untersetzungsverhaeltnis des Verteilergetriebes") and
``ACCCD_rAxleRed_C`` ("Gear ratio of the differential" used by the ACC
module only -- a placeholder such as 2.0 on ECUs without ACC); plus the
speed/rpm (v/n) tolerance bands ``Gearbx_rVn<gear>L_C`` / ``..H_C`` that the
ECU uses to detect the engaged gear. Editing these one variable at a time is
error-prone, so this model presents them as two tables:

  * **Absolute ratios** -- one value per drivetrain element (gears, reverse,
    transfer case, final drive).
  * **V/N ratios** -- a min/max pair per gear (lower and upper detection band).

On datasets with an automatic/manual (or other) coding variant, each ratio
exists in more than one physical copy: the main-block copy plus a secondary
copy per coding block. This model returns **every copy** as a separate
*column* (a :class:`GearCopy`), labelled by the transmission the coding selects
where that can be resolved. Every value is read and written in physical units
through the session, so editing any copy -- primary or variant -- is undoable
and saved with the binary.
"""

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional

_VSS_IMPPERDIST = "VSSCD_ImpPerDist_C"
_VSS_SIGVAL = "VSSCD_swtSigVal_C"     # 0 = HW FGG sensor, 1 = CAN
_AWDLO = "Gearbx_rAWDLo_C"            # transfer-case (AWD low) reduction
_TRANSR = "Gearbx_rTransR_C"


def _phys(session, entity):
    """Physical value of a single-cell constant entity, or None."""
    if entity is None:
        return None
    try:
        raw = session.binfile.read(entity.addr, entity.bits, entity.signed)
        return eval(entity.equation or "X", {"__builtins__": {}},
                    {"X": raw, "x": raw})
    except Exception:
        return None

# (display label, entity symbol) for the absolute-ratio table, in row order.
_ABS_ROWS = [
    ("1st gear", "Gearbx_rTrans1_C"),
    ("2nd gear", "Gearbx_rTrans2_C"),
    ("3rd gear", "Gearbx_rTrans3_C"),
    ("4th gear", "Gearbx_rTrans4_C"),
    ("5th gear", "Gearbx_rTrans5_C"),
    ("6th gear", "Gearbx_rTrans6_C"),
    ("7th gear", "Gearbx_rTrans7_C"),
    ("Reverse", "Gearbx_rTransR_C"),
    ("Transfer case (AWD low)", "Gearbx_rAWDLo_C"),
    ("ACC differential ratio (ACCCD)", "ACCCD_rAxleRed_C"),
]

# (display label, low/min symbol, high/max symbol) for the v/n table.
_VN_ROWS = [
    ("1st gear", "Gearbx_rVn1L_C", "Gearbx_rVn1H_C"),
    ("2nd gear", "Gearbx_rVn2L_C", "Gearbx_rVn2H_C"),
    ("3rd gear", "Gearbx_rVn3L_C", "Gearbx_rVn3H_C"),
    ("4th gear", "Gearbx_rVn4L_C", "Gearbx_rVn4H_C"),
    ("5th gear", "Gearbx_rVn5L_C", "Gearbx_rVn5H_C"),
    ("6th gear", "Gearbx_rVn6L_C", "Gearbx_rVn6H_C"),
    ("7th gear", "Gearbx_rVn7L_C", "Gearbx_rVn7H_C"),
    ("Reverse", "Gearbx_rVnRL_C", "Gearbx_rVnRH_C"),
]

# every symbol the drivetrain model touches (for filtering variant tables)
_GEAR_NAMES = ({n for _, n in _ABS_ROWS}
               | {lo for _, lo, _ in _VN_ROWS}
               | {hi for _, _, hi in _VN_ROWS})

# the six overall-ratio symbols, used to fingerprint a copy against a coding
_RTRANS = ["Gearbx_rTrans%d_C" % i for i in range(1, 7)]


@dataclass
class GearCopy:
    """One physical copy of the drivetrain-ratio set (a table column).

    ``by_name`` maps each ratio symbol present in this copy to its editable
    Entity. ``shared`` is True for a value the ECU reads from the primary
    main-block address for this copy too (no separate variant storage)."""
    label: str                       # e.g. "Automatic" / "5-speed manual"
    is_primary: bool
    by_name: Dict[str, object] = field(default_factory=dict)
    codings: List[int] = field(default_factory=list)

    def entity(self, name, primary_by_name):
        """The Entity to show/edit for ``name`` in this copy, and whether it is
        a value shared with the primary (no dedicated variant copy)."""
        e = self.by_name.get(name)
        if e is not None:
            return e, False
        return primary_by_name.get(name), True     # shared main-block value


class GearRatios:
    """Drivetrain ratios of one session, grouped into the two editor tables,
    across every coding-selected copy.

    On an EDC15 session this constructor hands back an
    ``edc15.gears15.GearRatios15`` (v/n gear-recognition thresholds, one
    column per dataset)."""

    def __new__(cls, session):
        if getattr(session, "is_edc15", False):
            from .edc15.gears15 import GearRatios15
            return GearRatios15(session)
        return super().__new__(cls)

    def __init__(self, session):
        self.session = session
        self._by_name = {e.name: e for e in session.entities}
        self._copies = None

    @property
    def available(self) -> bool:
        return any(n in self._by_name for n in _GEAR_NAMES)

    # -- row layout (which rows to draw) --------------------------------
    def abs_layout(self):
        """(label, symbol) for each absolute-ratio row present in the binary."""
        return [(lbl, n) for lbl, n in _ABS_ROWS if n in self._by_name]

    def vn_layout(self):
        """(label, lo_symbol, hi_symbol) for each v/n row present."""
        return [(lbl, lo, hi) for lbl, lo, hi in _VN_ROWS
                if lo in self._by_name or hi in self._by_name]

    # -- copies (columns) ----------------------------------------------
    def copies(self) -> List[GearCopy]:
        if self._copies is not None:
            return self._copies
        primary = GearCopy(label="Primary (main block)", is_primary=True,
                           by_name={n: e for n, e in self._by_name.items()
                                    if n in _GEAR_NAMES})
        copies = [primary]
        copies += self._variant_copies()
        self._label_copies(copies)
        self._copies = copies
        return copies

    def _variant_copies(self) -> List[GearCopy]:
        """Editable secondary-copy columns, one per distinct secondary block,
        built from the variant/coding table pairs."""
        import dataclasses
        try:
            from . import variant as _variant
            vts = _variant.find_variant_tables(self.session)
        except Exception:
            return []
        prim_by_z = {e.addr: e for e in self.session.entities}
        # per symbol, the list of secondary Entity clones (address-ordered)
        per_name: Dict[str, list] = {}
        for vt in vts:
            if vt.name not in _GEAR_NAMES:
                continue
            twin = prim_by_z.get(vt.main_z)
            if twin is None:
                continue
            clone = dataclasses.replace(twin, addr=vt.sec_z, source="variant")
            per_name.setdefault(vt.name, []).append((vt.sec_z, clone))
        if not per_name:
            return []
        for lst in per_name.values():
            lst.sort(key=lambda t: t[0])
        ncols = max(len(lst) for lst in per_name.values())
        out = []
        for k in range(ncols):
            by_name = {name: lst[k][1] for name, lst in per_name.items()
                       if k < len(lst)}
            out.append(GearCopy(label="Variant %d" % (k + 1),
                                is_primary=False, by_name=by_name))
        return out

    def _label_copies(self, copies):
        """Name each copy by the transmission its coding selects, decided from
        the coding pointer array (structurally, by the gear copy's data
        address) -- never from the ratio values, since auto vs manual gearing
        is not a fixed numeric relationship."""
        try:
            from . import variant as _variant
            by_addr = _variant.gear_copy_codings(self.session)
        except Exception:
            by_addr = {}
        if not by_addr:
            return
        for cp in copies:
            # the copy's identity address is its own Gearbx_rTrans1_C entity
            e = cp.by_name.get("Gearbx_rTrans1_C")
            if e is None and cp.is_primary:
                e = self._by_name.get("Gearbx_rTrans1_C")
            if e is None:
                continue
            hit = by_addr.get(e.addr)
            if not hit:
                continue
            code, tx = hit
            cp.codings = [code]
            if tx:
                # a drop-down entry: "Automatic (TC)", not "automatic (TC)"
                cp.label = tx[:1].upper() + tx[1:]
                if cp.is_primary:
                    cp.label += " · main block"

    # -- edit -----------------------------------------------------------
    def set_value(self, entity, value: float):
        """Write one physical ratio value (undoable through the session)."""
        if entity is not None:
            self.session.edit_cells(entity, [(0, 0, float(value))])

    # -- auto-generate v/n bands from the absolute ratios ---------------
    def _ent(self, copy, name):
        """Entity for ``name`` in this copy (variant clone or shared primary)."""
        if copy is None:
            return self._by_name.get(name)
        e, _shared = copy.entity(name, self._by_name)
        return e

    def signal_is_can(self) -> bool:
        """True when the speed source is CAN (VSSCD_swtSigVal_C == 1); the FGG
        pulse path (and ImpPerDist) is then inert, so a real tyre size is
        needed and the transfer-case ratio must be folded in."""
        v = _phys(self.session, self._by_name.get(_VSS_SIGVAL))
        return v is not None and round(v) == 1

    def imp_per_dist_m(self) -> Optional[float]:
        """VSS pulses per metre (Imp/m). VSSCD_ImpPerDist_C stores the raw
        3.6e9/ImpPerDist form, so convert."""
        raw = _phys(self.session, self._by_name.get(_VSS_IMPPERDIST))
        if not isinstance(raw, (int, float)) or raw <= 0:
            return None
        return 3.6e9 / raw if raw > 1e6 else raw

    def transfer_case(self, copy=None) -> float:
        v = _phys(self.session, self._ent(copy, _AWDLO))
        return v if isinstance(v, (int, float)) and v > 0.01 else 1.0

    def _ratios(self, copy):
        """{gear-number: overall ratio} for the forward gears present."""
        R = {}
        for i in range(1, 8):
            r = _phys(self.session, self._ent(copy, "Gearbx_rTrans%d_C" % i))
            if isinstance(r, (int, float)) and r > 0.1:
                R[i] = r
        return R

    def effective_circ(self, copy) -> Optional[float]:
        """Rolling circumference (m) inferred from THIS copy's current v/n bands
        vs its ratios: each interior crossover VnH[g] == 0.06*C/sqrt(R[g]*R[g+1]),
        so C = median(VnH[g]*sqrt(R[g]*R[g+1]))/0.06. Value-agnostic to the tune;
        returns None when too few clean gears (e.g. an all-open band)."""
        R = self._ratios(copy)
        gears = sorted(R)
        ks = []
        for a, b in zip(gears, gears[1:]):
            h = _phys(self.session, self._ent(copy, "Gearbx_rVn%dH_C" % a))
            if isinstance(h, (int, float)) and 0.0 < h < 0.25:   # skip sentinels
                ks.append(h * math.sqrt(R[a] * R[b]))
        if not ks:
            return None
        ks.sort()
        return (ks[len(ks) // 2] / 0.06)

    def plan_vn(self, copy, circ_m=None, fold_transfer=None):
        """Regenerated v/n min/max for every present gear of ``copy`` from its
        absolute ratios: v/n[g] = 0.06*C/(R[g]*tc), with the L/H band edges at
        the geometric mean of adjacent gears' v/n (the ECU's crossover layout);
        the 1st-gear floor and top-gear ceiling are left as-is.

        ``circ_m`` is the effective rolling circumference (m). Leave it None to
        auto-source: HW (FGG sensor) infers it from the current band/ratio
        geometry; CAN returns ``{"need_tire": True, "circ_hint": C|None}`` since
        ImpPerDist is inert there and a real tyre size is required. ``tc`` (the
        transfer-case reduction) is folded into the ratios when ``fold_transfer``
        is True (default: only on the CAN/tyre path)."""
        can = self.signal_is_can()
        if fold_transfer is None:
            fold_transfer = bool(can) or (circ_m is not None and can)
        if circ_m is None:
            if can:
                return {"need_tire": True, "circ_hint": self.effective_circ(copy)}
            C = self.effective_circ(copy)          # HW: infer from geometry
            source = "geometry"
            if C is None:
                return {"need_tire": True, "circ_hint": None}
            fold_transfer = False                  # geometry already reflects it
        else:
            C = float(circ_m)
            source = "can" if can else "tyre"
        tc = self.transfer_case(copy) if fold_transfer else 1.0
        R = self._ratios(copy)
        allg = sorted(R)
        # keep only the REAL gears: overall ratios strictly decrease with gear,
        # so a gear whose ratio is not meaningfully lower than the previous one
        # is an unused placeholder (e.g. 6th on a 5-speed, where R6 ~= R5). Those
        # are left untouched -- generating a crossover against a duplicate ratio
        # would otherwise collapse the top real gear's band.
        gears = []
        for g in allg:
            if gears and R[g] >= R[gears[-1]] * 0.99:
                break
            gears.append(g)
        if not gears:
            return {"writes": [], "circ": C, "source": source,
                    "transfer_case": tc, "true": {},
                    "gears": [], "placeholders": allg}
        true = {g: 0.06 * C / (R[g] * tc) for g in gears}
        steps = [true[a] / true[b] for a, b in zip(gears, gears[1:])
                 if true[b] > 0 and true[a] > true[b]]
        step = sorted(steps)[len(steps) // 2] if steps else 1.6
        writes = []
        for idx, g in enumerate(gears):
            if idx > 0:
                e = self._ent(copy, "Gearbx_rVn%dL_C" % g)
                if e is not None:
                    writes.append((e, math.sqrt(true[gears[idx - 1]] * true[g])))
            if idx < len(gears) - 1:
                e = self._ent(copy, "Gearbx_rVn%dH_C" % g)
                if e is not None:
                    writes.append((e, math.sqrt(true[g] * true[gears[idx + 1]])))
        rr = _phys(self.session, self._ent(copy, _TRANSR))
        if isinstance(rr, (int, float)) and abs(rr) > 0.1:
            tR = 0.06 * C / (abs(rr) * tc)
            half = math.sqrt(step)
            for name, val in (("Gearbx_rVnRL_C", tR / half),
                              ("Gearbx_rVnRH_C", tR * half)):
                e = self._ent(copy, name)
                if e is not None:
                    writes.append((e, val))
        return {"writes": writes, "circ": C, "source": source,
                "transfer_case": tc, "true": true, "gears": gears,
                "placeholders": [g for g in allg if g not in gears]}

    def apply_vn(self, writes):
        """Apply plan_vn()['writes'] as ONE undoable step; returns the count."""
        self.session.edit_values(writes, label="auto V/N gear-detection ratios")
        self.invalidate()
        return len(writes)

    def invalidate(self):
        self._copies = None
