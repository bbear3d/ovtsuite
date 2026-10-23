# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Feature switches for the AI assistant and MCP clients.

The same features as Tools > Feature switches (:mod:`ovtsuite.featureswitch`,
the bundles coming from the platform's module): what each does, what the
file holds now, and -- for a setting or values asked for -- the parameter
changes in the form ``check_changes`` / ``propose_change`` take, so a change
goes through the user's review like any other AI change. A feature that
also edits tables or fault paths lists them; those parts are applied in the
Feature switches window.
"""

from __future__ import annotations

from typing import Optional

from ..featureswitch import engine as E, model as M
from .tools import Registry, ToolError

H = {"type": "string", "description": "Target handle from open_file / list_open (e.g. \"h1\")."}


def _bundles(t):
    from ..gui_featureswitch import _present_bundles
    return _present_bundles(t.session)


def _find(t, feature: str):
    bs = _bundles(t)
    if not bs:
        raise ToolError("%s has no feature switches (EDC15 and EDC16 engine files "
                        "with their module installed have them)" % t.handle
                        if hasattr(t, "handle") else "no feature switches here")
    f = (feature or "").strip().lower()
    hit = [b for b in bs if b.id.lower() == f or b.title.lower() == f]
    if not hit:
        raise ToolError("no feature %r; this file has: %s"
                        % (feature, ", ".join("%s (%s)" % (b.id, b.title) for b in bs)))
    return hit[0]


def _copy(s, b, g):
    vals = E.read_values(s, b, g)
    st = E.current_state(s, b, g)
    best, ok, n = E.match_info(s, b, g)
    hook = M.hook(s, b.table_hook)
    words = getattr(hook, "state_text", None)
    return {
        "copy": g or "(the only copy)",
        "state": st, "state_label": b.label(st) if st else None,
        "closest_preset": None if st or not ok else
        {"state": best, "values_matching": ok, "values_in_preset": n},
        "state_in_words": words(s, g) if words is not None and st is None else None,
        "parameters": [{"name": p.name, "meaning": p.meaning, "value": vals.get(p.name),
                        "value_text": p.value_text(vals.get(p.name)),
                        "choices": {str(k): v for k, v in p.choices.items()} or None,
                        "read_only": p.readonly}
                       for p in E.present_params(s, b)],
    }


def register(reg: Registry) -> None:

    @reg.tool("feature_switches",
              "The functions this engine ECU file can switch on and off as bundles of "
              "calibration values (Tools > Feature switches): cruise control, A/C type, "
              "EGR (on / cut when warm / off), DPF fault paths, smoke limiter by MAF or "
              "MAP, gearbox type and more -- each with its settings, what the file holds "
              "now per copy (dataset / coding variant), and its parameters' values and "
              "meanings. With 'feature', also every table and fault path each setting "
              "changes. Reads only.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H,
                  "feature": {"type": "string", "description": "A feature id or title "
                                                              "for its full detail."}}},
              title="Feature switches")
    def feature_switches(ctx, handle, feature=None):
        t = ctx.workspace.get(handle)
        s = t.session
        if feature:
            bs = [_find(t, feature)]
        else:
            bs = _bundles(t)
            if not bs:
                raise ToolError("%s has no feature switches (EDC15 / EDC16 engine files "
                                "with their module installed)" % handle)
        out = []
        for b in bs:
            gs = E.groups(s, b) or [""]
            d = {"id": b.id, "title": b.title, "summary": b.summary,
                 "settings": [{"state": st, "label": b.label(st)} for st in b.states],
                 "read_only": b.readonly, "notes": list(b.notes),
                 "copies": [_copy(s, b, g) for g in gs]}
            hook = M.hook(s, b.table_hook)
            if feature and hook is not None and hasattr(hook, "fs_preview"):
                d["changes_by_setting"] = {}
                for st in b.states:
                    try:
                        d["changes_by_setting"][st] = hook.fs_preview(
                            s, gs[0], st, first=True,
                            ref=getattr(t, "comparison", None)) or []
                    except Exception as ex:               # noqa: BLE001
                        d["changes_by_setting"][st] = [{"error": str(ex)}]
            out.append(d)
        return {"features": out}

    @reg.tool("feature_switch_changes",
              "Work out the parameter changes that set a feature switch: to one of its "
              "settings ('state'), and/or to parameter values given by name ('values', "
              "raw stored values -- use the 'choices' feature_switches lists), on the "
              "copies named (default: all). Writes NOTHING: pass 'changes' to "
              "check_changes, then propose_change with your rationale. Tables and fault "
              "paths the setting also changes are listed under 'also_in_window': those "
              "are applied in Tools > Feature switches, not by a proposal -- tell the "
              "user so.",
              {"type": "object", "required": ["handle", "feature"], "properties": {
                  "handle": H,
                  "feature": {"type": "string"},
                  "state": {"type": "string", "description": "A setting from "
                                                            "feature_switches."},
                  "values": {"type": "object", "description": "{parameter name: raw value}",
                             "additionalProperties": {"type": "integer"}},
                  "copies": {"type": "array", "items": {"type": "string"}}}},
              title="Feature switch changes")
    def feature_switch_changes(ctx, handle, feature, state=None, values=None, copies=None):
        t = ctx.workspace.get(handle)
        s = t.session
        b = _find(t, feature)
        if b.readonly:
            raise ToolError("%s is shown for information only" % b.title)
        if state is not None and state not in b.states:
            raise ToolError("no setting %r; %s has: %s"
                            % (state, b.title, ", ".join(b.states)))
        if state is None and not values:
            raise ToolError("give a 'state' or 'values'")
        gs = E.groups(s, b) or [""]
        if copies:
            want = {c.lower() for c in copies}
            gs = [g for g in gs if (g or "(the only copy)").lower() in want]
            if not gs:
                raise ToolError("no such copy; this feature's copies are: %s"
                                % ", ".join(g or "(the only copy)" for g in
                                            (E.groups(s, b) or [""])))
        vals = dict(E.preset_values(s, b, state)) if state else {}
        vals.update({k: int(v) for k, v in (values or {}).items()})
        unknown = [k for k in (values or {}) if b.param(k) is None]
        if unknown:
            raise ToolError("not a parameter of %s: %s" % (b.title, ", ".join(unknown)))
        changes, warns = E.plan(s, b, state, gs, vals)
        from . import describe as D
        out, listed = [], []
        from ..featureswitch import ents as _ents
        for c in changes:
            if not c.changed:
                continue
            e = _ents.one(s, c.param, c.group)
            if e is None:
                continue
            try:
                shown = float(e.eq.decode(c.new))
            except Exception:
                shown = float(c.new)
            out.append({"map": t.index.eid(e),
                        "cells": [{"row": 0, "col": 0, "value": D.num(shown)}]})
            listed.append({"parameter": c.param, "copy": c.group or "(the only copy)",
                           "from": c.old, "to": c.new})
        also = []
        hook = M.hook(s, b.table_hook)
        if state and hook is not None and hasattr(hook, "fs_preview"):
            for i, g in enumerate(gs):
                also += hook.fs_preview(s, g, state, first=i == 0,
                                        ref=getattr(t, "comparison", None)) or []
        return {"feature": b.id, "state": state, "changes": out, "parameters": listed,
                "warnings": warns, "also_in_window": also,
                "next": ("nothing changed yet: check_changes with 'changes', then "
                         "propose_change" if out else "no parameter needs a change")
                + ("; the tables / fault paths in 'also_in_window' are applied in "
                   "Tools > Feature switches" if also else "")}
