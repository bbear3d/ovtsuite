# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Gearbox shift-point tools for the AI assistant and MCP clients.

``fullload_shift_points`` is the shift map editor's "Full-load optimum
(engine torque)" step (:mod:`ovtsuite.gearbox_edit.fullload`): the full-load
upshifts worked out from the torque of the engine ECU kept in the same
project, written into the chosen schedules and blended into part load.

Nothing here writes: the result carries ``changes`` in the form
``check_changes`` / ``propose_change`` take, so the edit goes through the
user's review like any other AI change.
"""

from __future__ import annotations

import importlib

from .describe import num
from .tools import Registry, ToolError

H = {"type": "string", "description": "Handle of the GEARBOX (TCU) set of a project, from "
                                      "open_file / list_open (e.g. \"h2\")."}

#: (platform, kind) -> (module, function) giving a ScheduleSet -- the
#: providers the shift map editor uses (gui_gearbox_edit.PROVIDERS), Qt-free
PROVIDERS = {
    ("zf6hp", "shift"): ("ovtsuite.zf6hp.shiftedit6hp", "schedule_set"),
    ("zf6hp", "tcc"): ("ovtsuite.zf6hp.tccedit6hp", "schedule_set"),
    ("zf8hp", "shift"): ("ovtsuite.zf8hp.shiftedit8hp", "schedule_set"),
    ("zf8hp", "tcc"): ("ovtsuite.zf8hp.shiftedit8hp", "tcc_schedule_set"),
}



def _is_manual(s) -> bool:
    """A manual (M) program -- left alone by default (as the editor does)."""
    g = (s.group or "").strip()
    return g.startswith("M ") or g.startswith("M(") or g == "M"

def schedule_set(session, kind: str = "shift"):
    spec = PROVIDERS.get((getattr(session, "platform", ""), kind))
    if spec is None:
        return None
    try:
        sset = getattr(importlib.import_module(spec[0]), spec[1])(session)
    except ImportError:
        return None
    return sset if sset.schedules else None


def _blends(project, sid, session, sset):
    from ..gearbox_edit import Blend
    raw = (project.set(sid).extra or {}).get("blends") if project is not None else None
    if raw:
        out = []
        for d in raw:
            try:
                out.append(Blend.from_dict(d))
            except Exception:
                continue
        return out
    try:
        from ..zf6hp import logevents6hp
        return logevents6hp.default_blends(session, sset)
    except Exception:
        return []


def register(reg: Registry) -> None:

    @reg.tool(
        "fullload_shift_points",
        "Full-load (full-throttle) upshift points for a ZF 6HP / 8HP gearbox kept in a project WITH "
        "its engine ECU, as Tools > Shift map editor > 'Full-load optimum (engine torque)' works "
        "them out. The engine's full-load torque is the dyno calculation on the ECU set (default: the "
        "version recorded as flashed, through the set's original). Each upshift goes where the next "
        "gear starts giving at least as much power at the same road speed (the crossover), capped by "
        "a ceiling under the cut-off; the map value is that speed less the LEAD (rpm the engine "
        "climbs between the threshold and the ratio change: from full-load upshifts in the project's "
        "logs, else physics from mass/axle/tyre, or manual), with converter slip in gears whose "
        "lock-up maps leave the converter open at full load. The full-load rows (>= 99.9 % pedal) of "
        "the chosen schedules get the result; rows from blend_from pedal up follow it ('factory' "
        "keeps each line's shape, 'ease' moves them by a smooth fraction, 'rows' leaves them), and a "
        "rise steeper than spread x the line's steepest factory rise is spread over lower rows. "
        "Lines may be raised OR lowered (direction limits it). Returns the targets per line, what "
        "the edit changes and the advisory check findings, plus 'changes' for check_changes / "
        "propose_change. Writes NOTHING. Engine rpm figures are map speed x the gear's ratio "
        "(turbine rpm), as the shift editor shows them.",
        {"type": "object", "required": ["handle"], "properties": {
            "handle": H,
            "ecu_set": {"type": "string", "description": "ECU set name in the same project "
                                                         "(default: the most recently flashed)."},
            "ecu_version": {"type": "string", "description": "Checkpoint id (or its first "
                                                             "characters) of the ECU set; default "
                                                             "the version flashed, else the head."},
            "schedules": {"type": "string", "description": "A preset name (e.g. 'Coding 1: D "
                                                           "(economic) — adaptive grid') or a range "
                                                           "('10-24, 31'). Default: every "
                                                           "schedule except manual (M) programs."},
            "lines": {"type": "array", "items": {"type": "string"},
                      "description": "Upshift labels to set (default all, e.g. ['1>2','2>3'])."},
            "mode": {"type": "string", "enum": ["all", "kickdown"],
                     "description": "'all' (default): 100 % and kickdown rows to the optimum; "
                                    "'kickdown': kickdown row to it, 100 % keeps its offset."},
            "shape": {"type": "string", "enum": ["factory", "ease", "rows"],
                      "description": "Blend into part load (default factory)."},
            "blend_from": {"type": "number", "description": "Pedal % below which nothing is "
                                                            "blended (default 80)."},
            "spread": {"type": "number", "description": "Spread steps steeper than this x the "
                                                        "factory's steepest (default 1.5; 0 = off)."},
            "min_gain": {"type": "number", "description": "Leave a line alone where the average "
                                                          "power gain is under this % (default 1)."},
            "direction": {"type": "string", "enum": ["any", "raise", "lower"]},
            "lead": {"type": "string", "enum": ["logs", "physics", "manual"],
                     "description": "Default logs (physics where no logged full-load upshift)."},
            "manual_lead": {"type": "array", "items": {"type": "number"},
                            "description": "Turbine rpm per upshift, in gear order (lead=manual)."},
            "mass_kg": {"type": "number", "description": "Vehicle mass with driver (default 1800)."},
            "response_s": {"type": "number", "description": "Threshold to ratio change at full "
                                                            "load, s (default 0.30)."},
            "axle_ratio": {"type": "number", "description": "Final drive (default: the "
                                                            "calibration's, else 4.16)."},
            "tyre": {"type": "string", "description": "Tyre size, e.g. '245/45R18' (default "
                                                      "255/40R19)."},
            "slip_percent": {"type": "number", "description": "Converter slip in gears open at "
                                                              "full load (default 6)."},
            "ceiling_rpm": {"type": "number", "description": "Highest engine rpm to shift at "
                                                             "(default 200 under the cut-off)."},
            "kickdown_downshifts": {"type": "boolean", "description": "Move each kickdown "
                                                                      "downshift with its "
                                                                      "upshift (default true)."}}},
        title="Full-load shift points")
    def fullload_shift_points(ctx, handle, ecu_set=None, ecu_version=None, schedules=None,
                              lines=None, mode="all", shape="factory", blend_from=80.0,
                              spread=1.5, min_gain=1.0, direction="any", lead="logs",
                              manual_lead=None, mass_kg=1800.0, response_s=0.30,
                              axle_ratio=None, tyre=None, slip_percent=6.0, ceiling_rpm=None,
                              kickdown_downshifts=True):
        from .. import fullload_ecu as fe
        from .. import speedview as _sv
        from ..gearbox_edit import Edit, GapCheck, MonotonicCheck, Scope, SharedCheck
        from ..gearbox_edit import fullload as FL
        t = ctx.workspace.get(handle)
        if getattr(t, "kind", "") != "project-set" or t.project is None:
            raise ToolError("%s is not a set of a project: the torque comes from an engine ECU "
                            "kept in the same project as the gearbox" % handle)
        sess = t.session
        sset = schedule_set(sess, "shift")
        if sset is None:
            raise ToolError("%s has no shift schedules this tool knows (ZF 6HP / 8HP)" % handle)
        proj = t.project
        sets = fe.ecu_sets(proj)
        if not sets:
            raise ToolError("the project holds no engine ECU set")
        if ecu_set:
            ecu = next((s for s in sets if s.name.lower() == ecu_set.lower()), None)
            if ecu is None:
                raise ToolError("no ECU set %r; the project has: %s"
                                % (ecu_set, ", ".join(s.name for s in sets)))
        else:
            ecu = fe.default_set(proj)
        vers = fe.versions(proj, ecu.id)
        if ecu_version:
            ver = next((v for v in vers if v.cid.startswith(ecu_version)), None)
            if ver is None:
                raise ToolError("no version %r of %s" % (ecu_version, ecu.name))
        else:
            ver = vers[0] if vers else None
        if ver is None:
            raise ToolError("%s has no versions" % ecu.name)
        # vehicle: the calibration's final drive where it states one
        base = _sv.Vehicle(axle_ratio=float(axle_ratio) if axle_ratio else 4.16,
                           tyre=tyre or "255/40R19")
        veh = base if axle_ratio else _sv.vehicle_of(sess, base)
        try:
            circ = veh.circumference_m
        except ValueError as ex:
            raise ToolError("tyre: %s" % ex)
        vn = {}
        for ln in sset.lines:
            for g, r in ((ln.gear_left, ln.ratio_left), (ln.gear_entered, ln.ratio_entered)):
                if g and r:
                    vn[g] = circ * 60.0 / 1000.0 / (r * veh.axle_ratio)
        src = fe.EcuTorque(proj)
        curve, by_gear, note = src.curves(ecu.id, ver.cid, vn)
        if curve is None:
            raise ToolError(note or "the ECU's full-load curve could not be worked out")
        # which schedules
        if schedules:
            try:
                sel = sset.select(schedules)
            except ValueError as ex:
                raise ToolError(str(ex))
            if not sel:
                raise ToolError("%r selects no schedule; presets: %s"
                                % (schedules, "; ".join(list(sset.presets)[:30])))
        else:
            sel = [i for i, s in enumerate(sset.schedules)
                   if not _is_manual(s)]
        tcc = schedule_set(sess, "tcc")
        open_g = FL.open_gears(sset, tcc, sel)
        slip = {g: float(slip_percent) / 100.0 for g in open_g}
        ups = [ln for ln in sset.lines if ln.kind == "up"]
        manual = None
        measured = None
        if lead == "manual":
            if not manual_lead:
                raise ToolError("lead=manual needs manual_lead (one value per upshift)")
            manual = {ln.index: float(v) for ln, v in zip(ups, manual_lead)}
        elif lead == "logs":
            try:
                from ..zf6hp.logevents6hp import ProjectLogs
                pl = ProjectLogs(proj, t.sid, sess, sset)
                evs = [e for e in pl.shifts if e.kind == "up" and e.pedal >= 95.0]
                if evs:
                    thr = FL.blend_threshold(sset, _blends(proj, t.sid, sess, sset),
                                             pl.lookup(evs))
                    measured = FL.measured_leads(evs, thr,
                                                 lambda ln: sset.line(ln).ratio_left or 1.0)
            except Exception:
                measured = None
        plan = FL.plan_upshifts(
            sset, curve, curves=by_gear, slip=slip,
            ceiling=float(ceiling_rpm) if ceiling_rpm else None,
            vehicle=FL.VehicleModel(mass_kg=float(mass_kg), axle_ratio=veh.axle_ratio,
                                    circumference_m=circ, response_s=float(response_s)),
            measured=measured, manual=manual)
        want = [ln.index for ln in ups if not lines or ln.label in lines]
        if lines:
            bad = [x for x in lines if x not in {ln.label for ln in ups}]
            if bad:
                raise ToolError("not upshift lines: %s (have %s)"
                                % (", ".join(bad), ", ".join(ln.label for ln in ups)))
        gain = float(min_gain or 0.0) / 100.0
        op = FL.FullLoadTarget(
            Scope(sel, want, direction=direction or "any"), plan.outputs(),
            shape={"factory": FL.SHAPE_FACTORY, "ease": FL.SHAPE_EASE,
                   "rows": FL.SHAPE_ROWS}.get(shape or "factory", FL.SHAPE_FACTORY),
            mode=FL.MODE_KICKDOWN if mode == "kickdown" else FL.MODE_ALL,
            p0=float(blend_from if blend_from is not None else 80.0),
            smooth=float(spread) if spread else None,
            kickdown_downshifts=bool(kickdown_downshifts),
            worth=(lambda i, c, old: plan.gain(c, old) >= gain) if gain > 0 else None)
        res = Edit(sset, [op], [GapCheck(60.0, tolerance=1.0), MonotonicCheck(),
                                FL.StepCheck(1.5), SharedCheck()]).compute()
        changes = {}
        for ch in res.changes:
            i, r, c = ch.key
            s = sset.schedules[i]
            changes.setdefault(id(s.entity), (s.entity, []))[1].append(
                {"row": r, "col": s.col_offset + c, "value": num(ch.new)})
        out_changes = [{"map": t.index.eid(e), "cells": cells}
                       for e, cells in changes.values()]
        targets = []
        for st in sorted(plan.targets.values(), key=lambda x: x.gear_from):
            targets.append({
                "line": st.label, "shift_engine_rpm": num(round(st.shift_engine)),
                "lead_rpm": num(round(st.lead)), "lead_source": st.lead_source,
                "map_value_rpm": num(round(st.threshold)),
                "map_value_output_rpm": num(round(st.output, 1)), "why": st.reason,
                "slip_percent": num(round(st.slip * 100, 1))})
        return {
            "ecu": {"set": ecu.name, "version": ver.cid, "version_label": ver.label,
                    "curve": note},
            "vehicle": {"mass_kg": num(mass_kg), "axle_ratio": num(veh.axle_ratio),
                        "tyre": veh.tyre, "response_s": num(response_s)},
            "converter_open_in_gears": open_g, "ceiling_rpm": num(round(plan.ceiling)),
            "targets": targets, "notes": plan.notes,
            "schedules": len(sel), "summary": res.summary(sset),
            "findings": [{"severity": f.severity, "check": f.code, "message": f.message}
                         for f in res.findings][:60],
            "changes": out_changes,
            "next": ("nothing changed yet: check_changes with 'changes', then propose_change"
                     if out_changes else "no cell needs a change"),
        }

    _register_partload(reg)


# ---------------------------------------------------------------- shared
_STRAT = {"type": "string", "description": "A preset -- 'economy', 'balanced', 'sport' -- or "
                                          "'leave' to leave those programs alone."}
_ECU = {"ecu_set": {"type": "string", "description": "ECU set name in the same project "
                                                    "(default: the most recently flashed)."},
        "ecu_version": {"type": "string", "description": "Checkpoint id (or its start) of the "
                                                        "ECU set; default the version flashed."}}
#: logs read once per project set and ECU version (a project's worth takes
#: tens of seconds)
_DRIVES = {}


def _target(ctx, handle):
    t = ctx.workspace.get(handle)
    if getattr(t, "kind", "") != "project-set" or t.project is None:
        raise ToolError("%s is not a set of a project: the engine comes from an ECU kept in "
                        "the same project as the gearbox" % handle)
    sset = schedule_set(t.session, "shift")
    if sset is None:
        raise ToolError("%s has no shift schedules this tool knows (ZF 6HP / 8HP)" % handle)
    return t, t.session, sset


def _ecu(proj, ecu_set=None, ecu_version=None):
    from .. import fullload_ecu as fe
    sets = fe.ecu_sets(proj)
    if not sets:
        raise ToolError("the project holds no engine ECU set")
    if ecu_set:
        ecu = next((s for s in sets if s.name.lower() == ecu_set.lower()), None)
        if ecu is None:
            raise ToolError("no ECU set %r; the project has: %s"
                            % (ecu_set, ", ".join(s.name for s in sets)))
    else:
        ecu = fe.default_set(proj)
    vers = fe.versions(proj, ecu.id)
    ver = next((v for v in vers if v.cid.startswith(ecu_version)), None) if ecu_version \
        else (vers[0] if vers else None)
    if ver is None:
        raise ToolError("no version %r of %s" % (ecu_version, ecu.name))
    return ecu, ver


def _vn(sess, sset, axle_ratio=None, tyre=None):
    from .. import speedview as _sv
    base = _sv.Vehicle(axle_ratio=float(axle_ratio) if axle_ratio else 4.16,
                       tyre=tyre or "255/40R19")
    veh = base if axle_ratio else _sv.vehicle_of(sess, base)
    try:
        circ = veh.circumference_m
    except ValueError as ex:
        raise ToolError("tyre: %s" % ex)
    vn = {}
    for ln in sset.lines:
        for g, r in ((ln.gear_left, ln.ratio_left), (ln.gear_entered, ln.ratio_entered)):
            if g and r:
                vn[g] = circ * 60.0 / 1000.0 / (r * veh.axle_ratio)
    return veh, circ, vn


def _select(sset, schedules):
    if schedules:
        try:
            sel = sset.select(schedules)
        except ValueError as ex:
            raise ToolError(str(ex))
        if not sel:
            raise ToolError("%r selects no schedule; presets: %s"
                            % (schedules, "; ".join(list(sset.presets)[:30])))
        return sel
    return [i for i, s in enumerate(sset.schedules) if not _is_manual(s)]


def _changes_out(t, sset, res):
    changes = {}
    for ch in res.changes:
        i, r, c = ch.key
        s = sset.schedules[i]
        changes.setdefault(id(s.entity), (s.entity, []))[1].append(
            {"row": r, "col": s.col_offset + c, "value": num(ch.new)})
    return [{"map": t.index.eid(e), "cells": cells} for e, cells in changes.values()]


def _preset(name):
    from ..gearbox_edit import partload as PL
    key = (name or "").strip().lower()
    if key in ("", "leave", "none", "off"):
        return None
    for st in PL.PRESETS.values():
        if st.name.lower() == key:
            return st
    raise ToolError("unknown strategy %r: economy, balanced, sport or leave" % name)


def _counts(findings):
    out = {}
    for f in findings:
        out[f.code] = out.get(f.code, 0) + 1
    return out


def _findings(findings, limit: int = 60):
    """Warnings first, a few of each check, so one busy check can not hide
    the others."""
    per = {}
    order = sorted(findings, key=lambda f: (f.severity != "warning", f.code))
    out = []
    for f in order:
        if per.get(f.code, 0) >= max(5, limit // 6):
            continue
        per[f.code] = per.get(f.code, 0) + 1
        out.append({"severity": f.severity, "check": f.code, "message": f.message,
                    "fixed": bool(f.fixed)})
        if len(out) >= limit:
            break
    return out


def _metrics_json(m):
    return {"l_per_100km": num(round(m.l100, 3)), "short_of_power_pct": num(round(m.short_pct, 2)),
            "shifts_per_hour": num(round(m.shifts_h)), "mean_rpm": num(round(m.mean_rpm)),
            "in_logged_gear_pct": num(round(m.agree * 100, 1))}


def _register_partload(reg) -> None:

    @reg.tool(
        "partload_shift_strategy",
        "Part-load (light and medium pedal) shift strategy for a ZF 6HP / 8HP gearbox kept in a "
        "project with its engine ECU, as the shift map editor's 'Part-load strategy' step: each "
        "upshift is set by the engine speed it leaves the next gear at, rising with pedal from a "
        "floor to the full-load optimum (the fullload_shift_points targets) at full load; the "
        "downshift that undoes it sits at the strategy's hysteresis. Presets: economy (floor 1250 "
        "rpm, rise from 25 % pedal, curve 1.6), balanced (1500, 15 %, 1.3), sport (1850, 5 %, "
        "1.0). By mode: D programs 'd', S programs 's' (default economy / sport), others 'other' "
        "(default leave), M always left. full_torque_rows: from the pedal where the ECU's "
        "driver's wish already asks for full torque, rows get the full-load points. The 100 % "
        "and kickdown rows are the full-load step's and are left alone. Returns the summary, "
        "check findings (including upshifts that land with the converter open) and 'changes' "
        "for check_changes / propose_change; evaluate them with replay_shift_lines. Writes "
        "NOTHING.",
        {"type": "object", "required": ["handle"], "properties": dict({
            "handle": H, "d": _STRAT, "s": _STRAT, "other": _STRAT,
            "schedules": {"type": "string", "description": "Preset name or range; default "
                                                           "every schedule except M."},
            "fraction": {"type": "number", "description": "How far toward the strategy, 0-1 "
                                                          "(default 1)."},
            "full_torque_rows": {"type": "boolean", "description": "Default true."},
            "downshifts": {"type": "boolean", "description": "Write the paired downshifts "
                                                             "(default true)."},
            "direction": {"type": "string", "enum": ["any", "raise", "lower"]},
            "axle_ratio": {"type": "number"}, "tyre": {"type": "string"},
            "mass_kg": {"type": "number"}}, **_ECU)},
        title="Part-load shift strategy")
    def partload_shift_strategy(ctx, handle, d="economy", s="sport", other="leave",
                                schedules=None, fraction=1.0, full_torque_rows=True,
                                downshifts=True, direction="any", axle_ratio=None, tyre=None,
                                mass_kg=1800.0, ecu_set=None, ecu_version=None):
        from .. import dyno as _dy
        from .. import fullload_ecu as fe
        from ..gearbox_edit import Edit, GapCheck, MonotonicCheck, Scope, SharedCheck
        from ..gearbox_edit import fullload as FL
        from ..gearbox_edit import partload as PL
        t, sess, sset = _target(ctx, handle)
        proj = t.project
        ecu, ver = _ecu(proj, ecu_set, ecu_version)
        veh, circ, vn = _vn(sess, sset, axle_ratio, tyre)
        src = fe.EcuTorque(proj)
        curve, by_gear, note = src.curves(ecu.id, ver.cid, vn)
        if curve is None:
            raise ToolError(note or "the ECU's full-load curve could not be worked out")
        sel = _select(sset, schedules)
        tcc = schedule_set(sess, "tcc")
        slip = {g: 0.06 for g in FL.open_gears(sset, tcc, sel)}
        plan = FL.plan_upshifts(sset, curve, curves=by_gear, slip=slip,
                                vehicle=FL.VehicleModel(mass_kg=float(mass_kg),
                                                        axle_ratio=veh.axle_ratio,
                                                        circumference_m=circ))
        ft = None
        if full_torque_rows:
            ms = _dy.models(src.session(ecu.id, ver.cid))
            if ms:
                ft = fe.full_torque_pedal(ms[0], vn, sset.schedules[sel[0]].pedal)[0]
        modes = {PL.mode_of(x.group) for x in sset.schedules}
        if modes & {"D", "S"}:
            assign = PL.assign_by_mode({"D": _preset(d), "S": _preset(s), "M": None},
                                       _preset(other))
        else:
            one = _preset(d)
            assign = lambda _s: one                                         # noqa: E731
        ups = [ln.index for ln in sset.lines if ln.kind == "up"]
        op = PL.PartLoadTarget(Scope(sel, ups, direction=direction or "any"), assign,
                               anchors=plan.outputs(), fraction=float(fraction),
                               full_torque=ft, downshifts=bool(downshifts))
        res = Edit(sset, [op], [GapCheck(60.0, tolerance=1.0), MonotonicCheck(),
                                FL.StepCheck(1.5), PL.ConverterCheck(tcc),
                                SharedCheck()]).compute()
        out = _changes_out(t, sset, res)
        return {"ecu": {"set": ecu.name, "version": ver.cid, "curve": note},
                "full_torque_pedal": num(ft) if ft is not None else None,
                "strategies": {"D": d, "S": s, "other": other},
                "schedules": len(sel), "summary": res.summary(sset),
                "findings_by_check": _counts(res.findings),
                "findings": _findings(res.findings),
                "changes": out,
                "next": ("nothing changed yet: replay_shift_lines with 'changes' to see what it "
                         "does to fuel and response, check_changes, then propose_change"
                         if out else "no cell needs a change")}

    @reg.tool(
        "replay_shift_lines",
        "Replay the project's logged drives (gearbox logs paired with engine logs taken at the "
        "same time) with the gearbox's shift lines as they are and, given 'changes' (the form "
        "check_changes takes, e.g. from partload_shift_strategy / fullload_shift_points), as "
        "they would be. Each logged moment keeps its road speed, pedal and the power the engine "
        "gave; the lines pick the gear; the ECU's own torque-to-quantity map gives the fuel. "
        "Returns l/100 km, % of time the gear could not give the power used, shifts per hour, "
        "mean engine rpm and the share of time in the logged gear -- before and after. Open "
        "loop: compare before with after, not with the logs' own figure. The first call reads "
        "the logs (tens of seconds); later calls reuse them.",
        {"type": "object", "required": ["handle"], "properties": dict({
            "handle": H,
            "changes": {"type": "array", "items": {"type": "object"},
                        "description": "Cell changes to judge (map + cells)."},
            "schedules": {"type": "string", "description": "Schedules a logged program with no "
                                                           "blend and no own schedule reads "
                                                           "(default: every schedule except M)."}},
            **_ECU)},
        title="Replay shift lines")
    def replay_shift_lines(ctx, handle, changes=None, schedules=None, ecu_set=None,
                           ecu_version=None):
        from .. import dyno as _dy
        from .. import fullload_ecu as fe
        from .. import shift_replay as SR
        from ..gearbox_edit import replay as RP
        t, sess, sset = _target(ctx, handle)
        proj = t.project
        ecu, ver = _ecu(proj, ecu_set, ecu_version)
        key = (getattr(proj, "uid", id(proj)), t.sid, ecu.id, ver.cid)
        if key not in _DRIVES:
            src = fe.EcuTorque(proj)
            ms = _dy.models(src.session(ecu.id, ver.cid))
            if not ms:
                raise ToolError("%s has no torque model" % ecu.name)
            fuel = SR.EngineFuel(ms[0])
            dr = SR.drives(proj, t.sid, [x.id for x in fe.ecu_sets(proj)], fuel)
            if not dr.samples:
                raise ToolError("no gearbox log in this project has an engine log taken with "
                                "it (they pair by start time, within 10 s)")
            curve, _by, _n = src.curves(ecu.id, ver.cid, {})
            _DRIVES[key] = (dr, fuel, SR.envelope_fn(curve, dr.envelope))
        dr, fuel, tmax = _DRIVES[key]
        sel = _select(sset, schedules)
        origin = {i: x.values() for i, x in enumerate(sset.schedules)}
        grid = {i: list(v) for i, v in origin.items()}
        applied = 0
        for ch in changes or ():
            e = t.index.resolve(ch.get("map", ""))
            hits = [i for i, x in enumerate(sset.schedules) if x.entity is e]
            if not hits:
                raise ToolError("%s is not a shift schedule of this gearbox" % ch.get("map"))
            for cell in ch.get("cells", ()):
                r, c, v = int(cell["row"]), int(cell["col"]), float(cell["value"])
                for i in hits:
                    x = sset.schedules[i]
                    if x.col_offset <= c < x.col_offset + x.cols and r < x.rows:
                        grid[i][r * x.cols + c - x.col_offset] = v
                        applied += 1
        ratios = {}
        for ln in sset.lines:
            if ln.gear_left and ln.ratio_left:
                ratios[ln.gear_left] = ln.ratio_left
            if ln.gear_entered and ln.ratio_entered:
                ratios[ln.gear_entered] = ln.ratio_entered
        bank = sset.schedules[sel[0]].bank if sel else 0
        corners = RP.program_corners(sset, _blends(proj, t.sid, sess, sset), sel, bank)

        def run(g):
            up, dn = RP.line_source(sset, g, lambda i: sset.schedules[i].pedal, corners)
            return RP.replay(dr.samples, ratios, up, dn, fuel.litres_per_hour, tmax)
        before = run(origin)
        out = {"logs": dr.logs, "hours": num(round(dr.hours, 2)), "km": num(round(dr.km, 1)),
               "ecu": {"set": ecu.name, "version": ver.cid},
               "before": _metrics_json(before)}
        if changes:
            after = run(grid)
            out["after"] = _metrics_json(after)
            out["cells_applied"] = applied
            out["fuel_change_pct"] = num(round(100.0 * (after.l100 - before.l100) / before.l100, 2)) \
                if before.l100 else None
        return out
