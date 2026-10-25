# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The feature-switch engine: read the current state, plan a change, apply it.

Platform-agnostic. A bundle (:mod:`.model`) names its parameters; the engine
resolves each name to the entity -- or the per-dataset (EDC15) / per-coding
(EDC16) *copies* -- in the loaded file, reads the raw stored value, works out
which state the file is in, and, for the copies the user ticks, writes the
target state as ONE undo step (``session.transaction`` -> one ``Command``).
Raw stored values are compared and written, so the A2L conversion never
distorts a 0/1/2 switch. Checksums are corrected by the app on save.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ents as _E
from . import model as M


@dataclass
class Change:
    param: str
    group: str                 # dataset / coding-copy label ("" = the only copy)
    addr: int
    bits: int
    old: int
    new: int
    prov: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


# -- resolving parameters to the file's entities --------------------------

def _entities(session, name: str):
    """Every entity of ``name`` in the file, one per copy/dataset."""
    return _E.named(session, name)


def _hook_module(session, name: Optional[str]):
    """The table hook a bundle's ``table_hook`` names, from the platform's
    module (:func:`model.hook`), or None.

    A feature can be a pure table edit (no single-value switch parameters); its
    presence, copies and state then come from the hook, which exposes
    ``present``/``groups``/``state`` with the same meaning as here."""
    return M.hook(session, name)


def absent_why(session, bundle: M.Bundle) -> str:
    """Why the bundle's table hook will not offer it on this file ("" when it
    does, or when the hook gives no reason): the hook's optional
    ``fs_absent_why``."""
    h = _hook_module(session, bundle.table_hook)
    fn = getattr(h, "fs_absent_why", None) if h is not None else None
    if fn is None:
        return ""
    try:
        return fn(session) or ""
    except Exception:
        return ""


def states(session, bundle: M.Bundle) -> List[str]:
    """The bundle's states this file can be set to: the hook's optional
    ``fs_states`` (a file lacking what a state needs offers fewer), else
    all of them."""
    h = _hook_module(session, bundle.table_hook)
    fn = getattr(h, "fs_states", None) if h is not None else None
    if fn is not None:
        try:
            got = [s for s in (fn(session) or ()) if s in bundle.states]
            if got:
                return got
        except Exception:
            pass
    return list(bundle.states)


def is_present(session, bundle: M.Bundle) -> bool:
    """Whether the file carries this feature at all: any of its parameters, or
    -- for a table-only feature -- the hook's table. A hook that says why it
    cannot be offered (:func:`absent_why`) keeps it out either way."""
    if absent_why(session, bundle):
        return False
    if present_params(session, bundle):
        return True
    h = _hook_module(session, bundle.table_hook)
    if h is not None and hasattr(h, "present"):
        try:
            return bool(h.present(session))
        except Exception:
            return False
    return False


def present_params(session, bundle: M.Bundle) -> List[M.Param]:
    """The bundle's parameters that this file actually carries (an optional
    one that the definition does not place is dropped)."""
    out = []
    for p in bundle.params:
        if _entities(session, p.name):
            out.append(p)
    return out


def groups(session, bundle: M.Bundle) -> List[str]:
    """The copy/dataset labels the bundle's parameters live in, in file order
    -- with, on a file carrying coding-variant copies (EDC16), the variant
    copy key (:func:`ents.variant_key`) after each dataset where any of the
    bundle's parameters or hook tables has one."""
    seen: List[str] = []
    for p in bundle.params:
        for e in _entities(session, p.name):
            g = getattr(e, "group", "") or ""
            if g not in seen:
                seen.append(g)
    h = _hook_module(session, bundle.table_hook)
    if not seen:
        if h is not None and hasattr(h, "groups"):
            try:
                seen = list(h.groups(session))
            except Exception:
                seen = []
    if seen and _E.has_variants(session):
        out: List[str] = []
        for g in seen:
            out.append(g)
            if _has_variant_copy(session, bundle, h, g):
                out.append(_E.variant_key(g))
        seen = out
    return seen


def _has_variant_copy(session, bundle, h, g) -> bool:
    if any(_E.variant_of(session, p.name, g) is not None for p in bundle.params):
        return True
    if h is None or not hasattr(h, "fs_involved"):
        return False
    ents = getattr(session, "entities", None)
    cache = getattr(session, "_fsw_hookvar", None)
    if cache is None or cache[0] is not ents:
        cache = (ents, {})
        try:
            session._fsw_hookvar = cache
        except Exception:
            pass
    key = (bundle.id, g)
    if key not in cache[1]:
        try:
            rows = h.fs_involved(session, g) or []
        except Exception:
            rows = []
        cache[1][key] = any(
            r.get("entity") is not None and _E.variant_of(
                session, r["entity"].name.split(" (variant @", 1)[0], g) is not None
            for r in rows)
    return cache[1][key]


def copies(session, bundles) -> List[str]:
    """Every copy key the ``bundles`` live in, in order: the window's one
    copy selector (datasets on EDC15, the main and coding-variant copies on
    EDC16)."""
    out: List[str] = []
    for b in bundles:
        for g in groups(session, b) or [""]:
            if g not in out:
                out.append(g)
    return out or [""]


def copy_for(session, bundle: M.Bundle, key: str) -> str:
    """The bundle's own copy that copy ``key`` stands for: ``key`` itself, or
    -- a coding-variant key for a bundle without variant copies -- the main
    copy that every coding reads."""
    gs = groups(session, bundle) or [""]
    if key in gs:
        return key
    b = _E.base_group(key)
    return b if b in gs else gs[0]


def _coding_texts(session):
    """``(main, variant)``: the coding text the main copies and the variant
    copies are read by (the commonest, from the definition's pointer
    array); ("", "") without variant copies."""
    hit = getattr(session, "_fsw_codings", None)
    vidx = _E._variant_index(session)
    if hit is not None and hit[0] is vidx:
        return hit[1]
    from collections import Counter
    vc, mc = Counter(), Counter()
    for name, vs in vidx.items():
        for v in vs:
            if getattr(v, "coding", ""):
                vc[v.coding] += 1
            m = _E.one(session, name, _E.group_of(v))
            if m is not None and getattr(m, "coding", ""):
                mc[m.coding] += 1
    res = (mc.most_common(1)[0][0] if mc else "", vc.most_common(1)[0][0] if vc else "")
    try:
        session._fsw_codings = (vidx, res)
    except Exception:
        pass
    return res


def copy_label(session, key: str, short: bool = False) -> str:
    """A copy key as the window names it: the dataset ("Manual"), or on
    EDC16 the main / coding-variant copy with the codings that read it."""
    if not _E.has_variants(session):
        return key or "The only copy"
    main, var = _coding_texts(session)
    base = _E.base_group(key)
    if _E.is_variant(key):
        what, coding = "Coding-variant copy", var
    else:
        what, coding = "Main copy", main
    if short:
        import re
        m = re.search(r"\(([^)]*)\)\s*$", coding or "")
        what = m.group(1) if m else ("variant" if _E.is_variant(key) else "main")
        return "%s %s" % (base, what) if base else what
    return "%s%s%s" % (base + ": " if base else "", what,
                       " (%s)" % coding if coding else "")


def _raw(session, e, bits: int) -> Optional[int]:
    try:
        return int(session.binfile.read(e.addr, bits, False))
    except Exception:
        return None


def _type_companion(session, val_name: str, group: str):
    """The ``X_swtYType_C`` entity paired with a switch's ``X_swtYVal_C`` in the
    same copy, or None. The Val/Type split is the format's own switch-pair
    structure (not a guess): the switch service reads {Val, Type}, and Type 1
    makes it read the EEPROM table instead of the flash Val, so a flash write
    is only authoritative with Type forced to flash (0/2 behave the same)."""
    if "_swt" not in val_name or not val_name.endswith("Val_C"):
        return None
    tname = val_name[:-len("Val_C")] + "Type_C"
    return _E.one(session, tname, group)


def read_values(session, bundle: M.Bundle, group: str) -> Dict[str, Optional[int]]:
    """Current raw value of each present parameter in one copy."""
    out: Dict[str, Optional[int]] = {}
    for p in present_params(session, bundle):
        e = _E.one(session, p.name, group)
        out[p.name] = _raw(session, e, p.bits) if e is not None else None
    return out


def current_state(session, bundle: M.Bundle, group: str) -> Optional[str]:
    """Which state the copy is in, or None when it matches none (a custom /
    tuned mix). A bundle's table hook, when it reads a state, knows the whole
    picture (tables, fault paths) and decides; otherwise every parameter that
    a state sets must hold that state's value."""
    h = _hook_module(session, bundle.table_hook)
    if h is not None and hasattr(h, "state"):
        try:
            return h.state(session, group)
        except Exception:
            return None
    info = match_info(session, bundle, group)
    return info[0] if info[1] == info[2] and info[2] else None


def match_info(session, bundle: M.Bundle, group: str) -> Tuple[Optional[str], int, int]:
    """``(state, matched, total)``: the state whose values this copy holds
    most of, how many of that state's values it holds, and how many the
    state sets. A file holding all of them is in that state; a file holding
    some (a tune that changed one switch of a bundle) only partly."""
    vals = read_values(session, bundle, group)
    best: Tuple[Optional[str], int, int] = (None, 0, 0)
    for state in bundle.states:
        n = ok = 0
        for p in present_params(session, bundle):
            tgt = p.target(state)
            if tgt is None or p.readonly:
                continue
            n += 1
            ok += vals.get(p.name) == int(tgt)
        if n and (ok * best[2] > best[1] * n or best[0] is None
                  or (ok == n and best[1] != best[2])):
            best = (state, ok, n)
    return best


def full_matches(session, bundle: M.Bundle, group: str) -> List[str]:
    """Every state whose values this copy holds in full: more than one when
    two settings write the same values (a torque-converter automatic and a
    DSG are both gearbox type 1)."""
    vals = read_values(session, bundle, group)
    out = []
    for state in bundle.states:
        want = preset_values(session, bundle, state)
        if want and all(vals.get(k) == v for k, v in want.items()):
            out.append(state)
    return out


def preset_values(session, bundle: M.Bundle, state: str) -> Dict[str, int]:
    """The values ``state`` gives the present, writable parameters (a
    parameter the state leaves alone is not listed)."""
    out = {}
    for p in present_params(session, bundle):
        if p.readonly:
            continue
        t = p.target(state)
        if t is not None:
            out[p.name] = int(t)
    return out


def value_range(p: M.Param) -> Tuple[int, int]:
    """The values a parameter's stored width can hold."""
    return 0, (1 << p.bits) - 1


# -- planning a change -----------------------------------------------------

def plan(session, bundle: M.Bundle, target_state: Optional[str],
         sel_groups: List[str],
         values: Optional[Dict[str, int]] = None) -> Tuple[List[Change], List[str]]:
    """Byte writes to take ``sel_groups`` to ``target_state`` -- or to the
    parameter ``values`` given (a preset the user edited, or values typed
    in), which take precedence -- plus warnings.

    A parameter with no value given and none in the state is left untouched;
    a USER / DERIVED parameter with no concrete value becomes a warning,
    never a blind write.
    """
    changes: List[Change] = []
    warnings: List[str] = []
    if bundle.readonly:
        return changes, ["This feature is shown for information and is not written."]
    params = present_params(session, bundle)
    done = set()            # a value shared by two copies is written once
    for g in sel_groups:
        for p in params:
            if p.readonly:
                continue
            if values is not None and p.name in values:
                tgt = values[p.name]
            else:
                tgt = p.target(target_state) if target_state else None
                if tgt is not None and p.prov in (M.USER, M.DERIVED):
                    warnings.append("%s needs a value for '%s': enter it before "
                                    "applying" % (p.name, bundle.label(target_state)))
                    continue
            if tgt is None:
                continue
            lo, hi = value_range(p)
            if not lo <= int(tgt) <= hi:
                warnings.append("%s: %d is outside %d to %d; not written"
                                % (p.name, int(tgt), lo, hi))
                continue
            e = _E.one(session, p.name, g)
            if e is None or e.addr in done:
                continue
            done.add(e.addr)
            old = _raw(session, e, p.bits)
            if old is None:
                warnings.append("Could not read %s (%s)" % (p.name, g or "the only copy"))
                continue
            changes.append(Change(p.name, g, e.addr, p.bits, old, int(tgt), p.prov))
            # force the switch to its flash value: Type 1 reads the EEPROM
            # table, so a flash Val is only authoritative with Type = 2 (0
            # reads the flash too: left as it is)
            tc = _type_companion(session, p.name, g)
            if tc is not None and tc.addr not in done:
                done.add(tc.addr)
                told = _raw(session, tc, 8)
                if told == 1:
                    changes.append(Change(tc.name, g, tc.addr, 8, told, 2,
                                          M.CODE))
    return changes, warnings


def apply(session, changes: List[Change], label: str):
    """Write the changed bytes (one command; inside the caller's transaction
    it joins that undo step). Returns the number written."""
    live = [c for c in changes if c.changed]
    if not live:
        return 0
    from ..session import Command
    writes = [(c.addr, c.bits, c.old, c.new) for c in live]
    cmd = Command(label, writes)
    session._record(cmd, "%s (%d value%s)"
                    % (label, len(writes), "" if len(writes) == 1 else "s"))
    return len(writes)


# -- what a table hook changes, in words --------------------------------------

def _spans(session):
    """``[(start, end, entity, axis_tag)]`` of every table and axis, for
    naming a raw write; kept per entity list."""
    ents = getattr(session, "entities", None) or []
    vidx = _E._variant_index(session)          # the coding-variant copies too
    hit = getattr(session, "_fsw_spans", None)
    if hit is not None and hit[0] is ents and hit[2] is vidx:
        return hit[1]
    out = []
    for e in list(ents) + [v for vs in vidx.values() for v in vs]:
        try:
            out.append((e.addr, e.addr + e.rows * e.cols * max(1, e.bits // 8), e, ""))
        except Exception:
            continue
        for tag in ("x", "y"):
            ax = getattr(e, tag, None)
            a = getattr(ax, "addr", None)
            if ax is not None and a is not None:
                try:
                    n = len(list(e.x_values() if tag == "x" else e.y_values()))
                    out.append((a, a + n * max(1, ax.bits // 8), e, tag))
                except Exception:
                    continue
    out.sort(key=lambda s: s[0])
    try:
        session._fsw_spans = (ents, out, vidx)
    except Exception:
        pass
    return out


def _decode(obj, bits, raw):
    signed = bool(getattr(obj, "signed", False))
    if signed and raw >= 1 << (bits - 1):
        raw -= 1 << bits
    try:
        return float(obj.eq.decode(raw))
    except Exception:
        return float(raw)


def _span_text(vals) -> str:
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    f = (lambda v: ("%g" % round(v, 3)))
    return f(lo) if abs(hi - lo) < 1e-9 else "%s to %s" % (f(lo), f(hi))


def write_rows(session, writes, meaning: str = "") -> List[Dict[str, str]]:
    """Preview rows for raw writes ``[(addr, bits, old, new)]``: one row per
    table (or axis) they fall in, with the old and new values' range in the
    table's own units. ``meaning`` overrides the table's description."""
    spans = _spans(session)
    groups: Dict[tuple, list] = {}
    order = []
    for w in writes:
        addr = w[0]
        hit = None
        for s, e_, ent, tag in spans:
            if s > addr:
                break
            if addr < e_:
                hit = (ent, tag)
                if not tag:
                    break
        key = (id(hit[0]), hit[1]) if hit else ("raw", addr)
        if key not in groups:
            groups[key] = [hit, []]
            order.append(key)
        groups[key][1].append(w)
    rows = []
    for key in order:
        hit, ws = groups[key]
        if hit is None:
            a = ws[0][0]
            rows.append({"name": "0x%X" % a, "meaning": meaning or "bytes outside any table",
                         "current": " ".join("%X" % w[2] for w in ws),
                         "new": " ".join("%X" % w[3] for w in ws), "kind": "raw"})
            continue
        ent, tag = hit
        obj = getattr(ent, tag) if tag else ent
        old = [_decode(obj, w[1], w[2]) for w in ws]
        new = [_decode(obj, w[1], w[3]) for w in ws]
        units = (getattr(obj, "units", "") or "").strip()
        single = not tag and ent.rows * ent.cols == 1
        what = ("%s axis" % tag.upper()) if tag else "" if single else (
            "%d cell%s" % (len(ws), "" if len(ws) == 1 else "s"))
        desc = (getattr(ent, "description", "") or "").split("|")
        desc = desc[1].strip() if len(desc) > 1 else ""
        rows.append({"name": ent.name + (" (%s axis)" % tag.upper() if tag else ""),
                     "meaning": (meaning or desc or "")[:80] + (" — %s" % what if what else ""),
                     "current": _span_text(old) + (" " + units if units else ""),
                     "new": _span_text(new) + (" " + units if units else ""),
                     "kind": "axis" if tag else "value" if single else "table",
                     "entity": ent, "axis": tag,
                     "cells": _cell_changes(ent, tag, ws, old, new)})
    return rows


def _cell_changes(ent, tag, writes, old, new) -> Dict[tuple, tuple]:
    """``{(row, col): (old, new)}`` (an axis: ``{(0, i): ...}``) of writes
    falling in ``ent`` -- what a preview grid marks."""
    out = {}
    try:
        if tag:
            ax = getattr(ent, tag)
            w = max(1, ax.bits // 8)
            for wr, o, n in zip(writes, old, new):
                out[(0, (wr[0] - ax.addr) // w)] = (o, n)
            return out
        where = {ent.addr + ent.index(r, c) * ent.step: (r, c)
                 for r in range(ent.rows) for c in range(ent.cols)}
        for wr, o, n in zip(writes, old, new):
            rc = where.get(wr[0])
            if rc is not None:
                out[rc] = (o, n)
    except Exception:
        return {}
    return out


def table_now_rows(session, entities, meaning: str = "") -> List[Dict[str, str]]:
    """Rows naming ``entities`` (tables a feature works through) with what
    they hold now, for the window before a setting is picked."""
    rows = []
    for ent in entities:
        if ent is None:
            continue
        try:
            vals = [float(ent.cell(r, c)) for r in range(ent.rows)
                    for c in range(ent.cols)]
        except Exception:
            vals = []
        units = (getattr(ent, "units", "") or "").strip()
        desc = (getattr(ent, "description", "") or "").split("|")
        desc = desc[1].strip() if len(desc) > 1 else ""
        n = ent.rows * ent.cols
        rows.append({"name": ent.name,
                     "meaning": (meaning or desc or "")[:80]
                     + (" — %d cells" % n if n > 1 else ""),
                     "current": _span_text(vals) + (" " + units if units else ""),
                     "new": "", "kind": "table" if n > 1 else "value",
                     "entity": ent, "axis": "", "cells": {}})
    return rows


def fault_now_rows(table, fps) -> List[Dict[str, str]]:
    """Rows naming fault paths ``fps`` with their state now (``target``: the
    class it has now, None when off)."""
    return [dict(r, new="") for r in fault_rows(
        table, [(fp, getattr(fp, "current_class", None) if table.is_enabled(fp)
                 else None) for fp in fps])]


# -- fault paths and "as the file was opened" ---------------------------------

def _revision(session):
    """What a cached read of ``session`` is good for: its entity list and
    undo-stack revision (every recorded edit, undo and redo bumps it); None
    while a transaction is writing (its edits are not on the stack yet)."""
    if getattr(session, "_txn", None) is not None:
        return None
    stack = getattr(session, "undo_stack", None)
    return (id(getattr(session, "entities", None)), getattr(stack, "revision", None))


def dtc_table(session):
    """The file's fault paths (``dtc.DtcTable``), built once per revision of
    the file: building one reads every fault path (a fifth of a second on an
    EDC16 image), and the window asks for it on every edit. Inside a
    transaction a fresh table is built (and not kept)."""
    from .. import dtc
    rev = _revision(session)
    hit = getattr(session, "_fsw_dtc", None)
    if rev is not None and hit is not None and hit[0] == rev:
        return hit[1]
    t = dtc.DtcTable(session)
    if rev is not None:
        try:
            session._fsw_dtc = (rev, t)
        except Exception:
            pass
    return t


def _ref_classes(table, ref):
    """``{(name, group): class}`` of the paths switched on in ``ref`` (the
    comparison), kept per revision of ``ref``."""
    rev = _revision(ref)
    hit = getattr(ref, "_fsw_refcls", None)
    if rev is not None and hit is not None and hit[0] == rev and hit[1] is type(table):
        return hit[2]
    out = {}
    try:
        rt = type(table)(ref)
        for fp in rt.paths.values():
            if rt.is_enabled(fp):
                out.setdefault((fp.name, getattr(fp, "group", "")), fp.current_class)
                out.setdefault((fp.name, None), fp.current_class)
    except Exception:
        out = {}
    if rev is not None:
        try:
            ref._fsw_refcls = (rev, type(table), out)
        except Exception:
            pass
    return out


def opened_raw(session, addr: int, bits: int) -> Optional[int]:
    """The raw value at ``addr`` in the bytes the session was opened with."""
    orig = getattr(session, "original", None)
    n = max(1, bits // 8)
    if orig is None or addr < 0 or addr + n > len(orig):
        return None
    endian = getattr(getattr(session, "binfile", None), "endian", ">")
    return int.from_bytes(bytes(orig[addr:addr + n]), "big" if endian == ">" else "little")


def _disabled_marker(table):
    import sys
    mod = sys.modules.get(type(table).__module__)
    for name in ("DISABLED_CLASS", "DISABLED_TIME"):
        if hasattr(mod, name):
            return getattr(mod, name)
    return 0


def _class_of(table, fp):
    """``(raw class, on)`` of a path now."""
    return getattr(fp, "current_class", None), table.is_enabled(fp)


def fault_targets(session, table, fps, enable: bool, ref=None, fallback=None):
    """``[(fault path, class)]`` to switch ``fps`` on (``enable``) or off --
    only those that change; class None means off. Switched on, a path gets
    the class it had when the file was opened; one that was off then too
    gets the class it has in ``ref`` (the comparison: the project's stock
    file), else ``fallback`` (a class to use for every path with none to go
    back to), and otherwise stays off."""
    dis = _disabled_marker(table)
    ref_cls = _ref_classes(table, ref) if enable and ref is not None else {}
    out = []
    for fp in fps:
        on = table.is_enabled(fp)
        if not enable:
            if on:
                out.append((fp, None))
            continue
        if on:
            continue
        e = getattr(fp, "cla", None)
        c = opened_raw(session, e.addr, e.bits) if e is not None else None
        if c is None or c == dis:
            c = ref_cls.get((fp.name, getattr(fp, "group", "")),
                            ref_cls.get((fp.name, None)))
        if (c is None or c == dis) and fallback is not None:
            c = fallback
        if c is not None and c != dis:
            out.append((fp, int(c)))
    return out


def fault_rows(table, targets) -> List[Dict[str, str]]:
    """Preview rows for :func:`fault_targets`."""
    rows = []
    for fp, cls in targets:
        cur = getattr(fp, "current_class", None)
        on = table.is_enabled(fp)
        codes = ", ".join(x for x in (getattr(fp, "vag_str", ""),
                                      getattr(fp, "pcode_str", "")) if x and x != "-")
        where = getattr(fp, "dfp", "") or fp.name
        rows.append({"name": where,
                     "meaning": "Fault path: %s%s" % (fp.code_desc(),
                                                       " (%s)" % codes if codes else ""),
                     "current": ("On (class %s)" % cur) if on and cur is not None
                     else ("On" if on else "Off"),
                     "new": "Off" if cls is None else "On (class %d)" % cls,
                     "kind": "fault",
                     # for the window's per-path choice (fault_edit_*)
                     "key": fp.name, "target": cls})
    return rows


def fault_text(cls) -> str:
    """A fault path's target as the window shows it."""
    return "Off" if cls is None else ("On" if cls < 0 else "On (class %d)" % cls)


def fault_on_class(session, table, fp, ref=None) -> int:
    """The class switching ``fp`` on gives it: the one it has, else as the
    file was opened, as the comparison ``ref`` has it, or the table's usual
    active class; -1 when none is known (the table picks its default)."""
    cur = getattr(fp, "current_class", None)
    if table.is_enabled(fp) and cur is not None:
        return int(cur)
    fb = None
    try:
        fb = table._default_active_class()
    except Exception:
        fb = None
    tg = fault_targets(session, table, [fp], True, ref, fb)
    return int(tg[0][1]) if tg and tg[0][1] is not None else -1


def apply_fault_edits(session, edits: Dict[str, Optional[int]], label: str) -> int:
    """Write per-path choices ``{fault path name: class}`` (None: off; -1:
    on with the table's own choice of class) inside the caller's
    transaction, on top of whatever a setting wrote. Returns the count."""
    if not edits:
        return 0
    from .. import dtc
    t = dtc.DtcTable(session)
    by = {fp.name: fp for fp in t.paths.values()}
    n = 0
    with session.transaction(label):
        for name, cls in edits.items():
            fp = by.get(name)
            if fp is None:
                continue
            on = t.is_enabled(fp)
            if cls is None:
                if on:
                    t.set_enabled(fp, False)
                    n += 1
            elif not on or (cls >= 0 and fp.current_class != cls):
                t.set_enabled(fp, True, enable_class=cls if cls >= 0 else None)
                n += 1
    return n


def apply_faults(session, table, targets, label: str) -> int:
    """Write :func:`fault_targets` (inside the caller's transaction)."""
    with session.transaction(label):
        for fp, cls in targets:
            if cls is None:
                table.set_enabled(fp, False)
            else:
                table.set_enabled(fp, True, enable_class=cls)
    return len(targets)


def restore_writes(session, entities, fallback=None) -> List[tuple]:
    """``[(addr, bits, old, new)]`` putting ``entities``' cells back as the
    file was opened; where ``fallback`` (``{addr: raw}``) names a value, that
    is used instead (the file was opened with the feature off)."""
    out = []
    for e in entities:
        if e is None or getattr(e, "is_float", False):
            continue
        for r in range(e.rows):
            for c in range(e.cols):
                addr = e.addr + e.index(r, c) * e.step
                old = int(session.binfile.read(addr, e.bits, False))
                new = (fallback or {}).get(addr)
                if new is None:
                    new = opened_raw(session, addr, e.bits)
                if new is not None and new != old:
                    out.append((addr, e.bits, old, int(new)))
    return out


__all__ = ["Change", "present_params", "is_present", "groups", "copies", "copy_for",
           "copy_label", "dtc_table", "fault_text", "fault_on_class", "apply_fault_edits",
           "read_values",
           "current_state", "match_info", "full_matches", "preset_values", "value_range", "plan",
           "apply", "write_rows", "table_now_rows", "fault_now_rows", "fault_rows", "fault_targets", "apply_faults",
           "opened_raw", "restore_writes"]
