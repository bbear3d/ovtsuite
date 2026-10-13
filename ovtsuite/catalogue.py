# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Applying the baked tuning catalogue to placed entities.

The catalogue (``tools/tuning_catalogue.py``) is written into each knowledge
base record as a small ``cat`` dict by ``tools/bake_catalogue.py``:

    {"g": group id, "s": sub-heading, "n": plain name, "k": shorthand,
     "i": set index, "ix": index kind, "sel": selector symbol,
     "sk": selector kind}

``apply`` copies it onto an Entity when matching places the record, so the
names come with the definition rather than from a lookup at display time.

``resolve_selectors`` then reads each set's selector curve FROM THE FILE and
says which operating range each member of the set serves -- "Injection timing
(SOI) -- 20-60 °C" rather than "#4". It is structural: the selector's
breakpoints and values decide, and a member the curve never selects is
marked as such. Where there is no selector, or it is not in this file, the
label keeps "#n".
"""

from typing import Dict, Iterable, List, Optional, Tuple

_ORD = {"1": "1st", "2": "2nd", "3": "3rd"}


def apply(entity, cat: Optional[Dict]) -> None:
    """Copy a baked catalogue record onto an entity (no-op for None)."""
    if not cat:
        return
    entity.tier = int(cat.get("g", entity.tier))
    sub = cat.get("s")
    if sub:
        # "Fuel & injection / Pilot injection" nests
        entity.category_path = [p.strip() for p in sub.split(" / ")]
    entity.plain = cat.get("n", "") or ""
    entity.short = cat.get("k", "") or ""
    entity.set_index = str(cat.get("i", "") or "")
    entity.idx_kind = cat.get("ix", "") or ""
    entity.selector = cat.get("sel", "") or ""
    entity.sel_kind = cat.get("sk", "") or ""
    entity.cat_order = int(cat.get("o", 0) or 0)


#: Order of a fault path's settings in the tree: the ones people change
#: first (see tiers.DTC_KINDS).
_DTC_ORDER = ("ClaDfp", "CDKDfp", "CDCDfp", "FMKDfp", "InhDfp", "EnvDfp",
              "TypDfp")


def _dtc_rank(name: str) -> int:
    if name.startswith("DSM_"):
        kind = name[4:].split("_", 1)[0]
        if kind in _DTC_ORDER:
            return _DTC_ORDER.index(kind)
    return 0


def sort_key(e):
    """Group, then the catalogue's maps in catalogue order, then the rest --
    headings alphabetical, a fault path's settings in a fixed order."""
    return (e.tier, 0 if getattr(e, "plain", "") else 1,
            getattr(e, "cat_order", 0),
            tuple(getattr(e, "category_path", None) or ()),
            _dtc_rank(e.name), e.name)


def gear_label(idx: str) -> str:
    if idx in ("R", "r"):
        return "reverse"
    return "%s gear" % _ORD.get(idx, idx + "th")


def display_name(e) -> str:
    """The tree/heading label of a catalogue entity, or "" if it has none.

    Plain English first, shorthand in parentheses, then which member of a set
    this is: a gear, the range its selector gives it, or "#n".
    """
    plain = getattr(e, "plain", "")
    if not plain:
        return ""
    idx = getattr(e, "set_index", "")
    kind = getattr(e, "idx_kind", "")
    if idx and kind == "gear":
        plain = "%s, %s" % (plain, gear_label(idx))
    out = plain
    short = getattr(e, "short", "")
    if short:
        out = "%s (%s)" % (out, short)
    if idx and kind != "gear":
        note = getattr(e, "set_note", "")
        out = "%s %s" % (out, ("— " + note) if note else "#" + idx)
    coding = getattr(e, "coding", "")
    if coding:
        out = "%s — %s" % (out, coding)
    return out


# ------------------------------------------------------------ selectors ---
def _piecewise(xs: List[float], zs: List[float], k: float
               ) -> Tuple[List[Tuple[float, float]], bool]:
    """Where map ``k`` is the dominant member (nearest integer to z).

    Returns the x intervals (merged) and whether ``k`` contributes anywhere
    at all. The curve is linear between breakpoints and held flat beyond the
    ends, as the ECU's curve lookup is.
    """
    INF = float("inf")
    spans: List[Tuple[float, float]] = []
    contributes = False
    n = len(xs)
    if n == 0:
        return spans, False

    def add(a, b):
        if spans and abs(spans[-1][1] - a) < 1e-9:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))

    # left of the first breakpoint
    if abs(zs[0] - k) < 1:
        contributes = True
    if abs(zs[0] - k) <= 0.5:
        add(-INF, xs[0])
    for i in range(n - 1):
        x0, x1, z0, z1 = xs[i], xs[i + 1], zs[i], zs[i + 1]
        if x1 <= x0:
            continue
        lo_z, hi_z = min(z0, z1), max(z0, z1)
        if hi_z > k - 1 and lo_z < k + 1:
            contributes = True
        a, b = k - 0.5, k + 0.5
        if z1 == z0:
            if a <= z0 <= b:
                add(x0, x1)
            continue
        # x where z crosses a and b
        ta = (a - z0) / (z1 - z0)
        tb = (b - z0) / (z1 - z0)
        t0, t1 = max(0.0, min(ta, tb)), min(1.0, max(ta, tb))
        if t1 > t0:
            add(x0 + t0 * (x1 - x0), x0 + t1 * (x1 - x0))
    if abs(zs[-1] - k) < 1:
        contributes = True
    if abs(zs[-1] - k) <= 0.5:
        add(xs[-1], INF)
    return spans, contributes


def _full_spans(xs: List[float], zs: List[float], k: float
                ) -> List[Tuple[float, float]]:
    """Where map ``k`` is used alone (z == k exactly), points included."""
    INF = float("inf")
    out: List[Tuple[float, float]] = []

    def add(a, b):
        if out and out[-1][1] >= a - 1e-12:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    eq = [abs(z - k) < 1e-6 for z in zs]
    if eq and eq[0]:
        add(-INF, xs[0])
    for i, x in enumerate(xs):
        if eq[i]:
            add(x, x)
            if i + 1 < len(xs) and eq[i + 1]:
                add(x, xs[i + 1])
    if eq and eq[-1]:
        add(xs[-1], INF)
    return out


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return "%d" % round(v)
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _range_text(spans, unit: str) -> str:
    INF = float("inf")
    parts = []
    for a, b in spans:
        if a == -INF and b == INF:
            parts.append("all conditions" if not unit.strip()
                         else "all temperatures" if "°C" in unit
                         else "always")
        elif a == -INF:
            parts.append("below %s%s" % (_fmt(b), unit))
        elif b == INF:
            parts.append("above %s%s" % (_fmt(a), unit))
        else:
            sep = " to " if a < 0 or b < 0 else "–"   # "-21 to -14 °C"
            parts.append("%s%s%s%s" % (_fmt(a), sep, _fmt(b), unit))
    return ", ".join(parts)


def _gears_for(spans, bands: Dict[str, Tuple[float, float]]) -> str:
    """The gears whose v/n detection band overlaps the selector's range."""
    INF = float("inf")
    hit = []
    for g, (lo, hi) in bands.items():
        for a, b in spans:
            if (min(b, hi) > max(a, lo) if b > a
                    else lo <= a <= hi):
                hit.append(g)
                break
    if not hit:
        return ""
    nums = sorted(int(g) for g in hit if g.isdigit())
    if not nums:
        return ""
    if len(nums) == 1:
        return gear_label(str(nums[0]))
    if nums == list(range(nums[0], nums[-1] + 1)):
        return "gears %d–%d" % (nums[0], nums[-1])
    return "gears " + ", ".join(str(n) for n in nums)


def _gear_bands(by_name) -> Dict[str, Tuple[float, float]]:
    """The ECU's own gear-detection v/n bands (Gearbx_rVn<g>L/H_C)."""
    out = {}
    for g in ("1", "2", "3", "4", "5", "6", "7"):
        lo = by_name.get("Gearbx_rVn%sL_C" % g)
        hi = by_name.get("Gearbx_rVn%sH_C" % g)
        if lo is None or hi is None:
            continue
        try:
            a, b = lo.cell(0, 0), hi.cell(0, 0)
        except Exception:
            continue
        if b > a > 0:
            out[g] = (a, b)
    return out


_UNITS = {"temp": " °C", "angle": "°", "pres": " hPa"}


def note_for(e, sel, by_name) -> str:
    """The operating range a set member serves, read from its selector."""
    try:
        k = float(int(e.set_index))
    except (TypeError, ValueError):
        return ""
    try:
        xs = list(sel.x_values())
        zs = [sel.cell(0, c) for c in range(sel.cols)]
    except Exception:
        return ""
    n = min(len(xs), len(zs))
    if n == 0:
        return ""
    xs, zs = xs[:n], zs[:n]
    spans, contributes = _piecewise(xs, zs, k)
    if not contributes:
        return "not selected"
    if not spans:
        return "blend only"
    kind = getattr(e, "sel_kind", "")
    if kind == "vn":
        # A gear-interpolated set is used ALONE at its own breakpoint(s);
        # the ECU's gear-detection bands say which gear that is.
        full = _full_spans(xs, zs, k) or spans
        gears = _gears_for(full, _gear_bands(by_name))
        if gears:
            return gears
        # no gear bands in this file: the v/n range, in km/h per 1000 rpm
        scaled = [(a * 1000, b * 1000) for a, b in spans]
        return "v/n " + _range_text(scaled, " km/h per 1000 rpm")
    if kind == "angle":
        return "SOI " + _range_text(spans, _UNITS["angle"])
    return _range_text(spans, _UNITS.get(kind, ""))


def resolve_selectors(entities: Iterable, group_key=None) -> int:
    """Fill ``set_note`` on every set member whose selector is in the file.

    ``group_key(entity)`` partitions entities that must not see each other's
    selectors (EDC15 datasets); default is one pool. Returns how many members
    got a note.
    """
    pools: Dict[object, Dict[str, object]] = {}
    ents = list(entities)
    for e in ents:
        key = group_key(e) if group_key else None
        pools.setdefault(key, {}).setdefault(e.name, e)
    done = 0
    for e in ents:
        sel_name = getattr(e, "selector", "")
        if not sel_name:
            continue
        by_name = pools.get(group_key(e) if group_key else None, {})
        sel = by_name.get(sel_name)
        if sel is None:
            continue
        # kept so the note can be re-read when the selector is edited
        e._sel_ent, e._sel_pool = sel, by_name
        note = note_for(e, sel, by_name)
        if note:
            e.set_note = note
            done += 1
    return done


def refresh_notes(entities: Iterable) -> List:
    """Re-read every set member's selector from the CURRENT bytes.

    The note is part of the name ("Injection timing (SOI) — 26–36 °C"), so it
    must follow an edit to the selector curve (or to the gear bands it is
    read through), an undo, or a patch. Cheap: a handful of short curves.
    Returns the entities whose note changed."""
    changed = []
    for e in entities:
        sel = getattr(e, "_sel_ent", None)
        if sel is None:
            continue
        note = note_for(e, sel, getattr(e, "_sel_pool", {}) or {})
        if note != getattr(e, "set_note", ""):
            e.set_note = note
            changed.append(e)
    return changed


# ------------------------------------------------------------- headings ---
import re as _re

_QUOTED = _re.compile(r'^\s*([^:"“”]+?)\s*:\s*["“”]\s*(.*?)\s*["“”]')
_BARE_ID = _re.compile(r"^[a-z][a-z0-9_]*$")
_SHIFT = _re.compile(r"_(HS|RS)?_?(\d)(\d)([SZ])?$")

#: ZF DAMOS function families whose code says exactly what they hold. Keyed
#: by code prefix; ``{s}`` is the shift ("3→4") or gear the code ends in.
_ZF_FUNCS = (
    ("zf_sas_kzu_ge", "On-coming clutch, gear {g} engage"),
    ("zf_sas_kab_ge", "Off-going clutch, gear {g} engage"),
    ("zf_sas_glo_ge", "Shift timing, gear {g} engage"),
    ("zf_sas_gls_ge", "Controlled power shift, gear {g} engage"),
    ("zf_mes_ge", "Shift torque cut, gear {g} engage"),
    ("zf_sas_kzu_", "On-coming clutch, {s}"),
    ("zf_sas_kab_", "Off-going clutch, {s}"),
    ("zf_sas_glo_", "Shift timing, {s}"),
    ("zf_sas_gls_", "Controlled power shift, {s}"),
    ("zf_sas_pmod_", "System pressure during shift, {s}"),
    ("zf_mes_", "Shift torque cut, {s}"),
    ("zf_kup_dat_", "Clutch {c} data"),
    ("zf_sas_glo", "Shift timing, general"),
    ("zf_sas_gls", "Controlled power shift, general"),
    ("zf_sas_kab", "Off-going clutch, general"),
    ("zf_sas_kzu", "On-coming clutch, general"),
    ("zf_mes", "Shift torque cut, general"),
    ("zf_kup_dat", "Clutch data"),
    ("bih_mobef", "Engine torque intervention"),
    ("bih_momen", "Torque signals"),
    ("bih_drehz", "Speed signals"),
    ("bih_signa", "Project signals"),
    ("bih_signs", "Project signals"),
    ("zf_uig", "Ratio monitoring"),
    ("h1460", "Shift pressure adaptation"),
    ("h1480", "Flare adaptation"),
    ("h1500", "Tie-up (GLUE) adaptation"),
    ("zf_age", "Gear engagement adaptation"),
    ("zf_ada_pfn_hs", "Fill pressure adaptation, upshifts"),
    ("zf_ada_pfn_rs", "Fill pressure adaptation, downshifts"),
    ("zf_ada_pfn", "Fill pressure adaptation"),
    ("zf_ada_sad", "Fill pressure adaptation, general"),
    ("zf_ada_glo", "Adaptation, global"),
    ("zf_pulsada", "Pulse fill adaptation"),
    ("zf_SSY_dat", "System (line) pressure"),
    ("zf_NHY_dat", "Hydraulic logic"),
)

_ZF_WORDS = (("Switching sequence", "Shift sequence"),
             ("switching sequence", "shift sequence"),
             ("switching off", "off-going"),
             ("motor intervention", "engine intervention"),
             ("Translation monitoring", "Ratio monitoring"),
             ("Gait determination", "Gear determination"),
             (" -> ", "→"), ("->", "→"))


def _shift_of(code: str) -> str:
    m = _SHIFT.search(code)
    return "%s→%s" % (m.group(2), m.group(3)) if m else ""


def heading_text(category: str, symbol: str = "") -> str:
    """A tree heading for a definition category, in plain English.

    Definitions file parameters under their supplier's function code, often
    with the title in quotes and a second language after "| EN:" or "| DE:".
    The heading keeps the title; a bare Bosch function id ("engprt_var") is
    shown as its module's name; a ZF function code that says what it is
    ("zf_sas_kzu_34") reads as that ("On-coming clutch, 3→4").
    """
    t = (category or "").strip()
    code = ""
    m = _QUOTED.match(t)
    if m:
        code, title = m.group(1).strip(), m.group(2).strip()
    else:
        title = t.split(" | ")[0].strip()
        code = title if _re.match(r"^[A-Za-z][A-Za-z0-9_]*$", title) else ""
    if code:
        for pre, fmt in _ZF_FUNCS:
            if code.startswith(pre):
                tail = code[len(pre):]
                return fmt.format(s=_shift_of(code) or tail, g=tail.upper()
                                  if tail.lower() == "r" else tail, c=tail)
    if code.startswith(("cLcc", "cCsc")):
        # ZF lockup classes: cLccSetpointStandard -> "Lockup control:
        # setpoint standard"; cCsc (converter slip control) likewise
        words = _re.sub(r"(?<=[a-z])(?=[A-Z])", " ", code[4:]).lower()
        return "%s: %s" % ("Lockup control" if code.startswith("cLcc")
                           else "Converter slip control", words or "general")
    if not title or _re.match(r"(?i)^(data function|data of (the )?class)",
                              title):
        title = code or title
        if title.startswith("zf_ASS_"):
            # ZF module with no title in the definition: its bare code
            title = "%s module" % title[len("zf_ASS_"):]
    if _BARE_ID.match(title):
        try:
            from .signal_names import _MODULE
            mod = _MODULE.get((symbol or "").split("_", 1)[0], "")
        except Exception:
            mod = ""
        if mod:
            return mod
    for a, b in _ZF_WORDS:
        title = title.replace(a, b)
    return title or "(uncategorized)"
