# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""End of injection for the AI assistant and MCP clients.

The same calculation as Tools > End of injection (:mod:`ovtsuite.eoi`): the
start of injection the ECU's own table set gives, the duration its own
duration-table selection gives at that start, and EOI = duration - start.
Which tables and selectors take part comes from the platform module's
``injection`` hook, so a binary whose module has none gets a plain error.

Nothing here writes: ``end_of_injection_changes`` returns cell changes in the
form ``check_changes`` / ``propose_change`` take, so moving the timing goes
through the user's review like any other AI change.
"""

from __future__ import annotations

from typing import Optional

from .. import eoi as E
from .describe import num
from .tools import Registry, ToolError

H = {"type": "string", "description": "Target handle from open_file / list_open (e.g. \"h1\")."}

_CONVENTION = ("Angles in crank degrees. start_btdc: degrees before TDC (negative = after TDC). "
               "eoi: degrees ATDC (negative = before TDC), the same convention as Tune from logs. "
               "duration: crank degrees. With units='microseconds' these angles are times at the point's "
               "own rpm instead (start_btdc: µs before TDC, eoi: µs after TDC, duration: µs).")


_UNITS = {"type": "string", "enum": ["degrees", "microseconds"],
          "description": "Unit of the angles: 'degrees' (default) or 'microseconds', the time at each "
                         "point's own rpm (no time at 0 rpm: null). Applies to start, duration and "
                         "EOI values, and to target_eoi."}


def _units(units):
    u = (units or "degrees").lower()
    if u in ("degrees", "degree", "deg"):
        return False
    if u in ("microseconds", "microsecond", "us", "µs"):
        return True
    raise ToolError("units must be 'degrees' or 'microseconds'")


def _conv(us, v, rpm, nd=2):
    """``v`` degrees as the requested unit at ``rpm`` (None: no time there)."""
    if not us:
        return _round(v, nd)
    t = E.deg_to_us(v, float(rpm))
    return None if t is None else _round(t, 0 if nd >= 1 else nd)


_SET = {"type": "string", "description": "SOI table set: 'base' (default) or a key from "
                                        "'soi_sets' -- e.g. 'dyn', the EDC16 dynamic set the ECU "
                                        "uses instead while smoke-limited."}
_COND = {"intake_air": {"type": "number", "description": "Intake air temperature °C for the "
                                                       "air-temperature correction (default 20)."},
         "air_pressure": {"type": "number", "description": "Atmospheric pressure hPa for the "
                                                         "altitude correction and the start set's "
                                                         "selector (default 1013)."},
         "start_quantity": {"type": "number", "description": "Start set only: the quantity "
                                                           "mg/stroke while starting (default 40)."},
         "fuel_temperature": {"type": "number", "description": "Fuel temperature °C for the "
                                                             "correction of the quantity the "
                                                             "duration maps read (default: the "
                                                             "ECU's reference temperature, no "
                                                             "correction)."}}


def _calc(m, soi_set, intake_air, air_pressure, start_quantity=None, fuel_temperature=None):
    cond = {}
    if fuel_temperature is not None:
        cond["fuel"] = float(fuel_temperature)
    if start_quantity is not None:
        cond["q_start"] = float(start_quantity)
    if intake_air is not None:
        cond["air"] = float(intake_air)
    if air_pressure is not None:
        cond["baro"] = float(air_pressure)
    try:
        return E.Calculator(m, cond, soi_set)
    except KeyError:
        raise ToolError("no SOI set %r here; this dataset has: %s"
                        % (soi_set, ", ".join(["base"] + [s.key for s in m.soi_sets])))


_COND_NAME = {"air": "intake_air", "baro": "air_pressure", "q_start": "start_quantity",
              "fuel": "fuel_temperature"}


def _sets(m):
    return [{"key": "base", "label": "Base"}] + [
        {"key": s.key, "label": s.label, "when": s.when} for s in m.soi_sets]


def _model(ctx, handle, dataset: Optional[str]):
    t = ctx.workspace.get(handle)
    ms = E.models(t.session)
    if not ms:
        raise ToolError("%s has no end-of-injection model: supported are EDC15P unit-injector (PD) "
                        "and EDC16 PD binaries (no VP37 distributor pumps)" % handle)
    if dataset:
        hit = [m for m in ms if m.label.lower() == dataset.lower()]
        if not hit:
            raise ToolError("no dataset %r; this binary has: %s"
                            % (dataset, ", ".join(m.label for m in ms)))
        m = hit[0]
    else:
        m = ms[0]
    if m.missing:
        raise ToolError("the definition does not place what the calculation needs for %s: %s"
                        % (m.label, ", ".join(m.missing)))
    return t, m, [x.label for x in ms]


def _members(t, tables, mem):
    out = []
    for i, w in mem:
        e = tables[i]
        out.append({"table": i, "map": t.index.eid(e) if e is not None else None,
                    "weight": num(round(w, 4))})
    return out


def _round(v, nd=2):
    return num(round(float(v), nd))


def register(reg: Registry) -> None:

    @reg.tool("end_of_injection",
              "End of injection (EOI) as the ECU's own tables give it, for EDC15P PD and EDC16 PD: the start "
              "of injection from the coolant-selected start table set (after the ECU's start limiter where the "
              "platform has one), the duration from the duration tables the ECU's selector picks at that "
              "start, and EOI = duration - start. With rpm and quantity: one operating point with every "
              "table and weight that takes part. Without: the grid over the start table's own breakpoints. "
              "Tables of a set are blended by the selector's fraction (table n plus the fraction of n+1), "
              "as the ECU's code does. The altitude and air-temperature corrections are included at "
              "intake_air / air_pressure. 'soi_sets' lists sets the ECU uses INSTEAD of the base set in "
              "some state (EDC16 'dyn': dynamic SOI while the smoke limit is the binding torque limit, "
              "i.e. at full load -- use it for full-load EOI); pick one with soi_set. What the result "
              "leaves out is listed in 'assumes'. " + _CONVENTION,
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H,
                  "dataset": {"type": "string", "description": "Dataset label (EDC15 'Manual 1' ...); "
                                                               "default the first."},
                  "coolant": {"type": "number", "description": "Coolant temperature °C (default warm, 90)."},
                  "rpm": {"type": "number", "description": "Engine speed for one point."},
                  "quantity": {"type": "number", "description": "Injected quantity mg/stroke for one point."},
                  "values": {"type": "string", "enum": ["eoi", "start", "duration", "all"],
                             "description": "Grid only: which values to return (default eoi)."},
                  "soi_set": _SET, "units": _UNITS, **_COND}},
              title="End of injection")
    def end_of_injection(ctx, handle, dataset=None, coolant=None, rpm=None, quantity=None,
                         values="eoi", soi_set=None, intake_air=None, air_pressure=None,
                         start_quantity=None, fuel_temperature=None, units=None):
        us = _units(units)
        t, m, labels = _model(ctx, handle, dataset)
        calc = _calc(m, soi_set, intake_air, air_pressure, start_quantity, fuel_temperature)
        clt = float(coolant if coolant is not None else m.coolant_warm)
        out = {"dataset": m.label, "datasets": labels, "platform": m.platform,
               "units": "microseconds" if us else "degrees",
               "coolant": num(clt), "convention": _CONVENTION, "assumes": list(m.notes),
               "soi_set": calc.soi_key, "soi_sets": _sets(m),
               "conditions": {_COND_NAME[k]: (num(calc.conditions[k])
                                              if calc.conditions[k] is not None else "reference")
                              for k in calc.conditions_used}}
        if calc.by_coolant:
            out["note"] = ("start set: the grid's second axis and the point's 'quantity' argument "
                           "are COOLANT °C; the duration is read at start_quantity")
        probs = E.selector_problems(m)
        if probs:
            out["warnings"] = probs
        try:
            if (rpm is None) != (quantity is None):
                raise ToolError("give both rpm and quantity for one point, or neither for the grid")
            if rpm is not None:
                c = calc.cell(clt, float(rpm), float(quantity))
                out["point"] = {
                    "rpm": num(rpm), "quantity": num(quantity),
                    "start_btdc": _conv(us, c.soi_btdc, rpm), "start_limited": c.soi_limited,
                    "start_tables": _members(t, calc.soi_ts.tables, c.soi_members),
                    "start_selector": (t.index.eid(calc.soi_ts.selector)
                                       if calc.soi_ts.selector is not None else None),
                    "start_corrections_btdc": _conv(us, calc.btdc(c.soi_cor), rpm),
                    "duration_selector": t.index.eid(m.duration.selector),
                    "duration_table_number": _round(c.index, 3),
                    "duration_tables": _members(t, m.duration.tables, c.dur_members),
                    "duration_quantity": _round(c.q_dur) if c.q_dur is not None else None,
                    "duration": _conv(us, c.duration, rpm), "eoi": _conv(us, c.eoi, rpm),
                    "eoi_text": E.fmt_angle(c.eoi)}
                if us:
                    out["point"]["degrees"] = {"start_btdc": _round(c.soi_btdc),
                                               "duration": _round(c.duration), "eoi": _round(c.eoi)}
                if m.limiter is not None:
                    out["point"]["limiter"] = t.index.eid(m.limiter)
                return out
            g = calc.grid(clt)
        except ValueError as ex:
            raise ToolError(str(ex))
        want = ("eoi", "start", "duration") if values == "all" else (values or "eoi",)
        grid = {"rpm": [num(v) for v in g.rpm],
                ("coolant" if calc.by_coolant else "quantity"): [num(v) for v in g.q],
                "layout": "[rpm index][%s index]" % ("coolant" if calc.by_coolant else "quantity"), "axes_from": t.index.eid(g.ref.e)}
        pick = {"eoi": lambda c: c.eoi, "start": lambda c: c.soi_btdc,
                "duration": lambda c: c.duration}
        if us:
            grid["note"] = "values are microseconds at each row's rpm (null at 0 rpm)"
        for k in want:
            if k not in pick:
                raise ToolError("values must be eoi, start, duration or all")
            grid[k if k != "start" else "start_btdc"] = [
                [_conv(us, pick[k](c), c.rpm, 1) for c in row] for row in g.cells]
        lim = [[c.rpm, c.q] for row in g.cells for c in row if c.soi_limited]
        if lim:
            grid["start_limited_at"] = [[num(a), num(b)] for a, b in lim]
        flat = [_conv(us, c.eoi, c.rpm, 1) for row in g.cells for c in row]
        flat = [v for v in flat if v is not None]
        grid["eoi_range"] = [min(flat), max(flat)] if flat else None
        out["grid"] = grid
        return out

    @reg.tool("end_of_injection_changes",
              "Work out the start-of-injection changes that give a target end of injection, the way "
              "Tools > End of injection > Set target EOI does: every start-grid point (inside the optional "
              "rpm / quantity window) gets the start that, with the duration the ECU then selects, ends "
              "injection at target_eoi. The change is added to the same cell of each start table the coolant "
              "selects (and of their coding-variant copies on EDC16), so fuelling stays calibrated. Points the "
              "start limiter caps are set to the limit and listed. Writes NOTHING and files nothing: pass "
              "'changes' to check_changes, then propose_change with your rationale. " + _CONVENTION,
              {"type": "object", "required": ["handle", "target_eoi"], "properties": {
                  "handle": H,
                  "target_eoi": {"type": "number", "description": "Wanted end of injection, deg ATDC "
                                                                  "(negative = before TDC); with "
                                                                  "units='microseconds', µs ATDC."},
                  "dataset": {"type": "string"},
                  "coolant": {"type": "number", "description": "°C the change is made for (default 90)."},
                  "rpm_min": {"type": "number"}, "rpm_max": {"type": "number"},
                  "quantity_min": {"type": "number"}, "quantity_max": {"type": "number"},
                  "only_later": {"type": "boolean", "description": "Only change points whose EOI is now "
                                                                   "later than the target (default false)."},
                  "soi_set": _SET, "units": _UNITS, **_COND}},
              title="End of injection changes")
    def end_of_injection_changes(ctx, handle, target_eoi, dataset=None, coolant=None,
                                 rpm_min=None, rpm_max=None, quantity_min=None, quantity_max=None,
                                 only_later=False, soi_set=None, intake_air=None, air_pressure=None,
                                 start_quantity=None, fuel_temperature=None, units=None):
        us = _units(units)
        t, m, _labels = _model(ctx, handle, dataset)
        calc = _calc(m, soi_set, intake_air, air_pressure, start_quantity, fuel_temperature)
        clt = float(coolant if coolant is not None else m.coolant_warm)
        goal = float(target_eoi)

        def inside(rpm, q):
            return not ((rpm_min is not None and rpm < rpm_min) or
                        (rpm_max is not None and rpm > rpm_max) or
                        (quantity_min is not None and q < quantity_min) or
                        (quantity_max is not None and q > quantity_max))

        def target(rpm, q):
            if not inside(rpm, q):
                return None
            # a time target is a different angle at every speed
            deg = E.us_to_deg(goal, rpm) if us else goal
            if deg is None:                              # no time at 0 rpm
                return None
            if only_later and calc.cell(clt, rpm, q).eoi <= deg + 1e-6:
                return None
            return deg

        try:
            plan = E.plan_target_eoi(calc, clt, target)
        except ValueError as ex:
            raise ToolError(str(ex))
        changes, maps = [], []
        for e, cells in plan.items():
            rows = [{"row": r, "col": c, "value": num(round(v, 4))}
                    for (r, c), v in sorted(cells.items())]
            pre = (e.name or "") + " (variant @"
            copies = [v for v in t.index.ordered if (v.name or "").startswith(pre)
                      and v.rows == e.rows and v.cols == e.cols]
            for x in [e] + copies:
                changes.append({"map": t.index.eid(x), "cells": rows})
                maps.append({"map": t.index.eid(x), "cells": len(rows),
                             "copy_of": t.index.eid(e) if x is not e else None})
        return {"dataset": m.label, "coolant": num(clt), "target_eoi": num(goal),
                "units": "microseconds" if us else "degrees",
                "soi_set": calc.soi_key,
                "changes": changes, "maps": maps,
                "limited_points": [[num(a), num(b)] for a, b in plan.limited],
                "unreachable_points": [[num(a), num(b)] for a, b in plan.unreached],
                "next": ("nothing changed yet: check_changes with 'changes', then propose_change"
                         if changes else "no point needs a change"),
                "convention": _CONVENTION}
