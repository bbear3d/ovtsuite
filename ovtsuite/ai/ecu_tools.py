# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""AI tools for engine ECUs (Bosch EDC15 / EDC16): fault codes, measuring
blocks, output tests. The engine modules hand these out (their ``ai_tools``),
so they exist only when such a module is installed; the code they call is the
same the app's own tool windows use (``ovtsuite.dtc``, ``msblock``,
``outtest``). Nothing here guesses: a binary these do not cover says so.
"""

from __future__ import annotations

from .describe import num, title
from .tools import Tool, ToolError

H = {"type": "string", "description": "Target handle (open_file / list_open)."}
ECU = ("edc16", "edc15")


def _target(ctx, handle, platforms=ECU):
    t = ctx.workspace.get(handle)
    pf = getattr(t.session, "platform", "")
    if pf not in platforms:
        raise ToolError("%s is a %s binary; this tool is for %s"
                        % (handle, pf or "unrecognised", " / ".join(p.upper() for p in platforms)))
    return t


# ------------------------------------------------------------ fault codes
def _dtc_table(t):
    from ..dtc import DtcTable
    tab = DtcTable(t.session)
    if not tab.paths:
        raise ToolError("no fault-path table was found in this binary")
    return tab


def fault_codes(ctx, handle, filter=None, enabled_only=False, limit=200):
    t = _target(ctx, handle)
    tab = _dtc_table(t)
    flt = (filter or "").lower()
    rows = []
    for fp in tab.sorted_paths():
        desc = fp.code_desc()
        if flt and flt not in (fp.dfp + " " + desc + " " + (fp.pcode or "") + " "
                               + fp.vag_str).lower():
            continue
        on = tab.is_enabled(fp)
        if enabled_only and not on:
            continue
        rows.append({"path": fp.name, "vag": fp.vag_str, "pcode": fp.pcode or "",
                     "mil": tab.lights_mil(fp), "enabled": on,
                     "class": fp.current_class, "description": desc[:120]})
    sm = tab.summary()
    return {"total": sm["total"], "enabled": sm["enabled"], "disabled": sm["disabled"],
            "matching": len(rows), "paths": rows[:max(1, int(limit))]}


def propose_fault_codes(ctx, handle, title_text="", rationale="", disable=None, enable=None):
    """Disabling / re-enabling a code is rewriting its class selector -- an
    ordinary parameter -- so it goes through the normal proposal path."""
    from . import proposals as P
    from ..dtc import DISABLED_CLASS
    t = _target(ctx, handle)
    tab = _dtc_table(t)
    stock = None
    cmp = t.comparison
    if cmp is not None:
        try:
            from ..dtc import DtcTable
            stock = DtcTable(cmp)
        except Exception:
            stock = None
    changes, done = [], []

    def find(name):
        low = name.lower()
        hits = [fp for k, fp in tab.paths.items()
                if low in (k.lower(), fp.name.lower(), fp.dfp.lower(), (fp.pcode or "").lower(),
                           fp.vag_str.strip("*").lower())]
        if not hits:
            raise ToolError("no fault path %r; fault_codes lists them" % name)
        return hits
    for name in disable or ():
        for fp in find(name):
            if fp.cla is None:
                continue
            changes.append({"map": t.index.eid(fp.cla), "values": [[float(DISABLED_CLASS)]]})
            done.append("disable %s" % fp.dfp)
    for name in enable or ():
        for fp in find(name):
            if fp.cla is None:
                continue
            want = None
            if stock is not None and fp.name in stock.paths:
                want = stock.paths[fp.name].current_class
            if want is None or want == DISABLED_CLASS:
                want = tab._default_active_class()
            changes.append({"map": t.index.eid(fp.cla), "values": [[float(want)]]})
            done.append("enable %s (class %s)" % (fp.dfp, want))
    if not changes:
        raise ToolError("nothing to change: name codes in disable or enable")
    prop = P.build(t, title_text or "Fault codes: %s" % ", ".join(done)[:80],
                   rationale, changes, client=ctx.client)
    P.save(prop)
    return {"proposal": prop["id"], "status": "pending review in the app", "changes": done}


# ------------------------------------------------------------ scan-tool views
def measuring_blocks(ctx, handle, group=None, all_groups=False):
    t = _target(ctx, handle)
    s = t.session
    if getattr(s, "is_edc15", False):
        raise ToolError("EDC15 measuring blocks are in Tools > Measuring blocks; "
                        "not available to the AI yet")
    from ..msblock import MeasuringBlocks
    from ..signals import SignalCatalog
    mb = MeasuringBlocks(s)
    if not mb.available:
        raise ToolError("no measuring-block table in this binary")
    cat = SignalCatalog(s.family, s)
    groups = mb.groups() if all_groups or group else mb.populated_groups()
    out = []
    for g in groups:
        if group is not None and g.number != int(group):
            continue
        out.append({"group": g.number, "channels": [cat.label(c) for c in g.channels()]})
    sm = mb.summary()
    return {"groups": sm["groups"], "populated": sm["populated"], "blocks": out,
            "table": t.index.eid(groups[0].entity) if groups and groups[0].entity else ""}


def output_tests(ctx, handle):
    t = _target(ctx, handle)
    from ..outtest import output_tests as _ot
    rows = _ot(t.session)
    if not rows:
        raise ToolError("no output-test table in this binary")
    keep = ("code_hex", "vag", "sig", "meaning", "group", "tv", "cycle_ms", "confidence")
    return {"tests": [{k: (num(r[k]) if isinstance(r.get(k), float) else r[k])
                       for k in keep if k in r} for r in rows]}


def tools():
    return [
        Tool("fault_codes",
             "Engine fault codes (DTC fault paths) of an EDC15/EDC16 binary: path, VAG code, P-code, "
             "whether it is enabled and lights the MIL, and a description. Filter by text.",
             fault_codes,
             {"type": "object", "required": ["handle"], "properties": {
                 "handle": H, "filter": {"type": "string"},
                 "enabled_only": {"type": "boolean"},
                 "limit": {"type": "integer", "description": "Default 200."}}},
             title="Fault codes"),
        Tool("propose_fault_codes",
             "Propose disabling or re-enabling engine fault codes (by path name, Dfp_ name, P-code or VAG "
             "code). Re-enabling restores the comparison's (stock) class when there is one. Filed as a "
             "proposal for the user to accept, like any change.",
             propose_fault_codes,
             {"type": "object", "required": ["handle", "rationale"], "properties": {
                 "handle": H, "title_text": {"type": "string", "description": "Proposal title."},
                 "rationale": {"type": "string"},
                 "disable": {"type": "array", "items": {"type": "string"}},
                 "enable": {"type": "array", "items": {"type": "string"}}}},
             read_only=False, title="Propose fault-code changes"),
        Tool("measuring_blocks",
             "EDC16 measuring blocks as a scan tool numbers them, with the signal in each slot.",
             measuring_blocks,
             {"type": "object", "required": ["handle"], "properties": {
                 "handle": H, "group": {"type": "integer"},
                 "all_groups": {"type": "boolean", "description": "Include empty groups."}}},
             title="Measuring blocks"),
        Tool("output_tests",
             "Actuator / output tests the binary supports (the codes a scan tool sends).",
             output_tests,
             {"type": "object", "required": ["handle"], "properties": {"handle": H}},
             title="Output tests"),
    ]
