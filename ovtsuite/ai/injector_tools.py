# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit-injector (PD) hardware for the AI assistant and MCP clients -- the
same calculation as Tools > Injector setup (:mod:`ovtsuite.injectors`).

``injector_hardware`` says which preset hardware set a binary is calibrated
for (and any sign in its structure that it is not stock);
``injector_presets`` lists what can be fitted; ``injector_setup_changes``
works out the cell changes that fit another set, in the form
``check_changes`` / ``propose_change`` take, and ``propose_injector_setup``
files them as a proposal that, accepted in a project, also records the
hardware as fitted (:mod:`ovtsuite.project.hardware`).
``injector_learn_report`` reads logs (see :mod:`ovtsuite.injlearn`) and files
nothing.
"""

from __future__ import annotations

from typing import Optional

from .. import eoi as E
from .. import injectors as I
from . import proposals as P
from .describe import num
from .tools import Registry, ToolError

H = {"type": "string", "description": "Target handle from open_file / list_open (e.g. \"h1\")."}


def _models(ctx, handle, dataset: Optional[str]):
    """``(target, the datasets asked for, every dataset's label)``: the one
    named, or every dataset of the file."""
    t = ctx.workspace.get(handle)
    why = I.availability(t.session)
    if why:
        raise ToolError("%s: %s" % (handle, why))
    ms = [m for m in E.models(t.session) if not I._missing_duration(m)]
    if not ms:
        raise ToolError("%s: no duration set in this file's definition" % handle)
    labels = [m.label for m in ms]
    if dataset:
        hit = [m for m in ms if m.label.lower() == dataset.lower()]
        if not hit:
            raise ToolError("no dataset %r; this binary has: %s"
                            % (dataset, ", ".join(labels)))
        return t, hit[:1], labels
    return t, ms, labels


_TIMING = {"centre": I.KEEP_CENTRE, "eoi": I.KEEP_EOI, "soi": I.KEEP_SOI}


def _timing(timing) -> str:
    pol = _TIMING.get(timing or "centre")
    if pol is None:
        raise ToolError("timing must be centre, eoi or soi")
    return pol


def _matches(hits):
    return [{"preset": h.preset.id, "name": h.preset.name,
             "mean_difference": num(round(h.mean, 4)),
             "worst_degrees": num(round(h.worst, 3)),
             "exact": h.exact, "same_hardware": h.same} for h in hits]


def _catalogue():
    try:
        from ..gui_injectors import user_presets       # needs Qt settings
        user = user_presets()
    except Exception:
        user = []
    return I.catalogue(user)


def _preset_row(cat, p):
    c = cat.char_of(p)
    return {"id": p.id, "name": p.name, "kind": p.kind, "confidence": p.confidence,
            "listed_for": p.listed_for, "vag_part_numbers": p.vag or None,
            "bosch_part_numbers": p.bosch or None, "cam": p.cam, "body": p.body, "nozzle": p.nozzle,
            "base": p.base or None, "flow_factor": num(round(p.flow, 3)) if p.base else None,
            "flow_basis": p.basis or None,
            "mg_per_degree_10btdc_3000rpm_45mg": num(round(c.mg_per_degree(10, 3000, 45), 3))
            if c is not None else None,
            "source": p.source or None, "notes": p.notes or None}


def _changes(t, plan):
    """``plan``'s cells as check_changes / propose_change changes, with the
    EDC16 coding-variant copies of every table."""
    changes, maps = [], []
    for e, cells in plan.cells.values():
        rows = [{"row": r, "col": c, "value": num(round(v, 4))}
                for (r, c), v in sorted(cells.items())]
        pre = (e.name or "") + " (variant @"
        copies = [v for v in t.index.ordered if (v.name or "").startswith(pre)
                  and v.rows == e.rows and v.cols == e.cols]
        for x in [e] + copies:
            changes.append({"map": t.index.eid(x), "cells": rows})
            maps.append({"map": t.index.eid(x), "cells": len(rows),
                         "copy_of": t.index.eid(e) if x is not e else None})
    return changes, maps


def _setup(ctx, handle, preset, complete_injectors=None, timing="centre", dataset=None):
    """The plans for the dataset named, or for every dataset of the file
    (as the window ticks them all), and their changes together."""
    t, ms, _labels = _models(ctx, handle, dataset)
    cat = _catalogue()
    p = cat.get(preset)
    if p is None:
        raise ToolError("no preset %r (see injector_presets)" % preset)
    char = cat.char_of(p)
    if char is None:
        raise ToolError("preset %r's base %r is not installed" % (preset, p.base))
    body = (p.kind != I.AFTERMARKET) if complete_injectors is None else bool(complete_injectors)
    pol = _timing(timing)
    plat = I.platform_for(t.session)
    several = len(ms) > 1
    notes, changes, maps, findings, per = [], [], [], [], []
    for m in ms:
        pre = (m.label + ": ") if several else ""
        opts = I.FitOptions(body=body, timing=pol, selector=True)
        plan = I.plan_fit(t.session, m, char, opts, cat.electrical_of(p), cat.extras_of(p),
                          plat)
        if plan.axes:
            # a selector-axis change cannot go through propose_change: keep the
            # file's own selector (each table read where it picks it)
            opts.selector = False
            plan = I.plan_fit(t.session, m, char, opts, cat.electrical_of(p),
                              cat.extras_of(p), plat)
            notes.append(pre + "The preset's selector curve has other breakpoints; this "
                         "file's own selector is kept (Tools > Injector setup can write "
                         "the preset's curve).")
        ch, mp = _changes(t, plan)
        changes += ch
        maps += mp
        findings += [{"level": f.level, "text": pre + f.text} for f in plan.findings]
        per.append({"dataset": m.label, "maps": len(mp),
                    "cells": sum(x["cells"] for x in mp)})
    out = {"dataset": ms[0].label if not several else None,
           "datasets": [m.label for m in ms],
           "preset": p.id, "complete_injectors": body,
           "timing": timing or "centre", "confidence": p.confidence,
           "changes": changes, "maps": maps, "findings": findings, "notes": notes,
           "next": ("nothing changed yet: propose_injector_setup with the same arguments "
                    "(it records the hardware as fitted), or check_changes then "
                    "propose_change" if changes else "nothing to change")}
    if several:
        out["per_dataset"] = per
    return out, t, p, body


def _fitted(t) -> dict:
    """What the project records as fitted (a plain file: nothing)."""
    if getattr(t, "kind", "") != "project-set" or t.project is None:
        return {"recorded": None, "note": "a plain file: fitted hardware is recorded in projects"}
    from ..project import hardware as HW
    try:
        dirty = bool(t.ws.is_dirty())
    except Exception:                                   # a file workspace: no tab
        dirty = False
    return HW.report(t.project, t.sid, t.branch, I.HW_KIND, unsaved=dirty)


def register(reg: Registry) -> None:

    @reg.tool("injector_hardware",
              "Which unit-injector (PD) hardware set an EDC15P / EDC16 binary is calibrated for: its "
              "duration maps and selector compared with every preset (cam + injector body + nozzle). "
              "'exact' = the same calibration (every point within 0.05 crank degrees); 'same_hardware' = "
              "within 1 % on average; otherwise only the closest. EDC15 files can carry several "
              "datasets, each identified on its own ('per_dataset'; 'disagree' says when they are "
              "not set up for the same hardware); the top-level 'matches' are the first dataset's. "
              "Also 'not_stock': signs in the file's own structure that it was modified (e.g. lambda "
              "probe off with every lambda fault path silenced).",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H,
                  "dataset": {"type": "string",
                              "description": "EDC15 dataset label; default every dataset."},
                  "count": {"type": "integer", "description": "How many matches (default 3)."}}},
              title="Injector hardware")
    def injector_hardware(ctx, handle, dataset=None, count=3):
        t, ms, labels = _models(ctx, handle, dataset)
        cat = _catalogue()
        res = I.identify_datasets(ms, cat, int(count or 3))
        per = []
        for r in res:
            if r.error:
                per.append({"dataset": r.label, "error": r.error})
                continue
            per.append({"dataset": r.label,
                        "mg_per_degree_10btdc_3000rpm_45mg":
                            num(round(r.char.mg_per_degree(10, 3000, 45), 3)),
                        "matches": _matches(r.matches)})
        first = per[0]
        if "error" in first:
            raise ToolError("%s: %s" % (first["dataset"], first["error"]))
        out = {"dataset": first["dataset"], "datasets": labels,
               "mg_per_degree_10btdc_3000rpm_45mg": first["mg_per_degree_10btdc_3000rpm_45mg"],
               "matches": first["matches"],
               "not_stock": I.tuned_signs(t.session),
               "fitted": _fitted(t)}
        if len(per) > 1:
            out["per_dataset"] = per
            out["disagree"] = I.datasets_disagree(res)
        return out

    @reg.tool("injector_presets",
              "The unit-injector hardware presets Tools > Injector setup can fit: factory sets (exact, "
              "from stock files), aftermarket nozzles (derived from their base nozzle with the "
              "vendor's flow factor -- an upper bound, so a lean starting point to refine from logs) "
              "and the user's own.",
              {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": ["oem", "aftermarket", "user", "all"],
                           "description": "Filter (default all)."}}},
              title="Injector presets")
    def injector_presets(ctx, kind="all"):
        cat = _catalogue()
        rows = [_preset_row(cat, p) for p in cat if kind in (None, "all", p.kind)]
        return {"presets": rows, "count": len(rows)}

    @reg.tool("injector_setup_changes",
              "Work out the cell changes that fit another unit-injector hardware set to a PD binary, as "
              "Tools > Injector setup does: the duration maps (and their selector when the layouts "
              "match), the start-of-injection tables moved by the timing policy ('centre' = keep the "
              "centre of injection, default and closest to Bosch's own recalibrations; 'eoi'; 'soi'), and "
              "-- with complete_injectors -- the injector body's solenoid (BIP) calibration. Injector "
              "setup does not add power: the engine is asked for the same fuel and the new hardware "
              "delivers it. Nothing is written: pass 'changes' to check_changes, then propose_change.",
              {"type": "object", "required": ["handle", "preset"], "properties": {
                  "handle": H,
                  "preset": {"type": "string", "description": "Preset id from injector_presets."},
                  "complete_injectors": {"type": "boolean",
                                         "description": "Bodies change too (default: true for factory "
                                                        "sets, false for aftermarket nozzles)."},
                  "timing": {"type": "string", "enum": ["centre", "eoi", "soi"],
                             "description": "Start-of-injection policy (default centre)."},
                  "dataset": {"type": "string",
                              "description": "EDC15 dataset label; default every dataset (as "
                                             "the window ticks them all)."}}},
              title="Injector setup changes")
    def injector_setup_changes(ctx, handle, preset, complete_injectors=None, timing="centre",
                               dataset=None):
        out, _t, _p, _body = _setup(ctx, handle, preset, complete_injectors, timing, dataset)
        return out

    @reg.tool("propose_injector_setup",
              "File the injector setup injector_setup_changes works out as a proposal for the user to "
              "review in the app (the same arguments). In a project, accepting it also records the "
              "hardware as fitted, so the dyno chart and the logs know which versions are set up for "
              "which injectors -- prefer this to propose_change for an injector change.",
              {"type": "object", "required": ["handle", "preset"], "properties": {
                  "handle": H,
                  "preset": {"type": "string", "description": "Preset id from injector_presets."},
                  "complete_injectors": {"type": "boolean"},
                  "timing": {"type": "string", "enum": ["centre", "eoi", "soi"]},
                  "dataset": {"type": "string",
                              "description": "EDC15 dataset label; default every dataset."},
                  "rationale": {"type": "string", "description": "Why, for the user."}}},
              read_only=False, title="Propose injector setup")
    def propose_injector_setup(ctx, handle, preset, complete_injectors=None, timing="centre",
                               dataset=None, rationale=""):
        out, t, p, body = _setup(ctx, handle, preset, complete_injectors, timing, dataset)
        if not out["changes"]:
            raise ToolError("nothing to change: the file is already set up for %s" % p.name)
        errs = [f["text"] for f in out["findings"] if f["level"] == "error"]
        if errs:                    # what blocks Apply in Tools > Injector setup
            raise ToolError("this setup cannot be applied: " + "; ".join(errs))
        summary, parts = I.hardware_record(p, body)
        why = (rationale.strip() + "\n\n" if rationale else "") + \
            "Fits %s; start of injection: %s." % (summary, I.TIMING_LABELS.get(
                {"centre": I.KEEP_CENTRE, "eoi": I.KEEP_EOI, "soi": I.KEEP_SOI}[out["timing"]],
                out["timing"]))
        warns = [f["text"] for f in out["findings"] if f["level"] in ("warning", "error")]
        if warns:
            why += "\n\nWarnings:\n" + "\n".join("- " + w for w in warns)
        prop = P.build(t, "Injector setup: %s" % summary, why, out["changes"], client=ctx.client)
        prop["hardware"] = {"kind": I.HW_KIND, "summary": summary, "parts": parts}
        P.save(prop)
        return {"proposal": prop["id"], "status": "pending review in the app",
                "summary": prop["summary"], "checks": prop.get("checks", []),
                "after_accepting": ("the injectors are recorded as fitted in the project"
                                    if getattr(t, "kind", "") == "project-set" else
                                    "save, flash, then log and refine with "
                                    "injector_learn_report")}

    @reg.tool("injector_learn_report",
              "Read data logs taken with new injectors / nozzles on an EDC15P / EDC16 PD binary and say "
              "how much fuel the hardware really delivers against what the duration maps assume: from "
              "before/after logs at the same operating points (idle, steady cruise) and, on EDC16, from "
              "the ECU's own lambda probe (measuring block 40) with the air mass. Returns per-cell "
              "delivery factors and the duration-map corrections Tools > Injector setup > From logs would "
              "write (each held to 15 % per pass: apply, log again, repeat). An EDC15 file with "
              "several datasets learns for each dataset the logs apply to ('per_dataset'; "
              "'log_datasets' says which): a log is recorded while one dataset runs, and the ECU "
              "ident a VCDS-layout log records names it; a log with no ident is taken for every "
              "dataset, as the window does. The top-level 'samples' and 'factors' are then the "
              "first dataset's, the breakpoint counts the totals. Files nothing: pass "
              "'changes' to check_changes, then propose_change.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H,
                  "after": {"type": "array", "items": {"type": "string"},
                            "description": "Log files (CSV) taken with the new hardware."},
                  "before": {"type": "array", "items": {"type": "string"},
                             "description": "Log files taken with the old hardware at the same points."},
                  "logs": {"type": "array", "items": {"type": "string"},
                           "description": "Project log ids / file names (list_logs) taken with the "
                                          "new hardware."},
                  "before_logs": {"type": "array", "items": {"type": "string"},
                                  "description": "Project logs taken with the old hardware."},
                  "since_change": {"type": "boolean",
                                   "description": "In a project: the logs added since the recorded "
                                                  "injector change, and those before it as the "
                                                  "old-hardware logs."},
                  "timing": {"type": "string", "enum": ["centre", "eoi", "soi"],
                             "description": "How the start of injection follows the corrected "
                                            "duration, as for a preset (default centre)."},
                  "dataset": {"type": "string",
                              "description": "EDC15 dataset label (every log is taken for it); "
                                             "default every dataset the logs apply to."}}},
              title="Injector learn report")
    def injector_learn_report(ctx, handle, after=None, before=None, dataset=None, logs=None,
                              before_logs=None, since_change=False, timing="centre"):
        from .. import injlearn as L
        from .logs import paths_for
        t, ms, labels = _models(ctx, handle, dataset)
        pol = _timing(timing)
        used = {}
        if since_change and not (after or logs):
            if getattr(t, "kind", "") != "project-set":
                raise ToolError("since_change needs a project (its logs and fitted-hardware "
                                "record)")
            from ..project import hardware as HW
            rec, aft, bef = HW.split_logs(t.project, t.sid, I.HW_KIND)
            if rec is None:
                raise ToolError("the project records no injector change: give logs")
            if not aft:
                raise ToolError("no log was added to the project since the change (%s)"
                                % rec.since[:10])
            logs = [a.id for a in aft]
            if not before and not before_logs:
                before_logs = [a.id for a in bef]
            used = {"change": rec.summary, "fitted": rec.since, "after_logs": logs,
                    "before_logs": before_logs or []}
        after_p = paths_for(ctx, handle, logs, after)
        before_p = paths_for(ctx, handle, before_logs, before) if (before or before_logs) else []
        if used:            # logs picked here: one that is not a data log is left out
            from .logs import readable
            after_p, bad = readable(after_p)
            before_p, bad2 = readable(before_p)
            if bad or bad2:
                used["left_out"] = [{"log": n, "why": w} for n, w in bad + bad2]
            if not after_p:
                raise ToolError("none of the logs added since the change reads as a data log")
        try:
            la = L.read_logs(t.session, after_p)
            lb = L.read_logs(t.session, before_p)
        except (OSError, ValueError) as ex:
            raise ToolError(str(ex))
        if len(ms) == 1:
            out = _learn_one(L, t, ms[0], la, lb, pol, labels)
        else:
            out = _learn_each(L, t, ms, la, lb, pol)
        if used:
            out["logs_used"] = used
        out["timing"] = timing or "centre"
        out["next"] = ("nothing changed yet: check_changes with 'changes', then propose_change"
                       if out["changes"] else "nothing to change")
        return out


def _name(path) -> str:
    import os
    return os.path.basename(path)


def _log_rows(logs, which):
    return [{"log": _name(p), "datasets": ds, "how": how}
            for (p, _i, _r), (ds, how) in zip(logs, which)]


def _learn_one(L, t, m, la, lb, pol, labels):
    """One dataset (the file's only one, or the one named): every log."""
    try:
        rep = L.learn(t.session, m, [], [], timing=pol,
                      runs_after=[r for _p, _i, r in la], runs_before=[r for _p, _i, r in lb])
    except ValueError as ex:
        raise ToolError(str(ex))
    out = rep.to_json()
    out["dataset"] = m.label
    out["changes"] = _changes(t, rep.plan)[0] if rep.plan is not None else []
    if len(labels) > 1:             # say so when a log's ident names another dataset
        logs = la + lb
        which = L.log_datasets(t.session, labels, [i for _p, i, _r in logs])
        other = [_name(p) for (p, _i, _r), (ds, how) in zip(logs, which)
                 if how == "ident" and m.label not in ds]
        if other:
            out["log_datasets"] = _log_rows(logs, which)
            out["notes"].append("%s record%s the ident of another dataset than %s: the car "
                                "was not running %s when %s logged." % (
                                    ", ".join(other), "s" if len(other) == 1 else "",
                                    m.label, m.label,
                                    "it was" if len(other) == 1 else "they were"))
    return out


def _learn_each(L, t, ms, la, lb, pol):
    """Every dataset of the file, each from the logs that can apply to it.
    A log is recorded while one dataset runs; the ident a VCDS-layout log
    records names it (or the datasets that share it), and a log with no ident
    is taken for every dataset, as Tools > Injector setup does."""
    labels = [m.label for m in ms]
    wa = L.log_datasets(t.session, labels, [i for _p, i, _r in la])
    wb = L.log_datasets(t.session, labels, [i for _p, i, _r in lb])
    per, changes, maps, findings, notes, reps = [], [], [], [], [], []
    for m in ms:
        pre = m.label + ": "
        ra = [r for (_p, _i, r), (ds, _h) in zip(la, wa) if m.label in ds]
        rb = [r for (_p, _i, r), (ds, _h) in zip(lb, wb) if m.label in ds]
        row = {"dataset": m.label,
               "after_logs": [_name(p) for (p, _i, _r), (ds, _h) in zip(la, wa) if m.label in ds],
               "before_logs": [_name(p) for (p, _i, _r), (ds, _h) in zip(lb, wb)
                               if m.label in ds]}
        if not ra:
            row["skipped"] = ("no log taken with the new hardware was recorded on this "
                              "dataset (their idents name another)")
            per.append(row)
            continue
        try:
            rep = L.learn(t.session, m, [], [], timing=pol, runs_after=ra, runs_before=rb)
        except ValueError as ex:
            row["error"] = str(ex)
            per.append(row)
            continue
        js = rep.to_json()
        ch, mp = _changes(t, rep.plan) if rep.plan is not None else ([], [])
        changes += ch
        maps += [dict(x, dataset=m.label) for x in js["maps"]]
        findings += [{"level": f["level"], "text": pre + f["text"]} for f in js["findings"]]
        notes += [pre + n for n in js["notes"]]
        row.update({k: js[k] for k in ("samples", "same_work_points", "breakpoints_corrected",
                                       "breakpoints_unmeasured", "factors", "maps")})
        row["cells"] = sum(x["cells"] for x in mp)
        per.append(row)
        reps.append(js)
    if not reps:
        bad = next(r for r in per if "error" in r or "skipped" in r)
        raise ToolError("%s: %s" % (bad["dataset"], bad.get("error") or bad["skipped"]))
    unknown = sorted({_name(p) for (p, _i, _r), (_ds, how) in zip(la + lb, wa + wb)
                      if how == "unknown"})
    if unknown:
        notes.insert(0, "%s record%s no ECU ident naming one of this file's datasets, so "
                     "%s taken for every dataset, as Tools > Injector setup does. A log is "
                     "recorded while one dataset runs (the coding picks it): name that "
                     "dataset to correct it alone." % (
                         ", ".join(unknown), "s" if len(unknown) == 1 else "",
                         "it was" if len(unknown) == 1 else "they were"))
    shared = sorted({tuple(ds) for (_p, _i, _r), (ds, how) in zip(la + lb, wa + wb)
                     if how == "ident" and len(ds) > 1})
    for ds in shared:
        notes.append("%s share one ident (they differ by coding only), so a log recorded "
                     "on either was taken for both: name the one the car is coded to to "
                     "correct it alone." % " and ".join(ds))
    first = reps[0]
    return {"dataset": None, "datasets": labels,
            "samples": first["samples"], "same_work_points": first["same_work_points"],
            "factors": first["factors"],
            "breakpoints_corrected": sum(r["breakpoints_corrected"] for r in reps),
            "breakpoints_unmeasured": sum(r["breakpoints_unmeasured"] for r in reps),
            "maps": maps, "findings": findings, "notes": notes, "changes": changes,
            "per_dataset": per, "log_datasets": _log_rows(la + lb, wa + wb)}


__all__ = ["register"]
