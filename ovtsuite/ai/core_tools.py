# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The base app's AI tools. Platform-specific tools come from modules.

Descriptions are written for the model: they say what comes back and what
the tool will NOT do, because that is what keeps an assistant from guessing.
"""

from __future__ import annotations

import re
from typing import Optional

from . import describe as D
from . import proposals as P
from .tools import Registry, ToolError

H = {"type": "string", "description": "Target handle from open_file / list_open (e.g. \"h1\")."}

#: Project-info keys never returned: identify the car or its immobiliser.
_REDACT = re.compile(r"vin|immo|skc|pin|chassis.?n(umber|o)", re.I)

_INSTRUCTIONS = """\
%(app)s: ECU/TCU calibration editor (Bosch EDC15/EDC16, ZF 6HP/8HP, Aisin, VDO clusters, Climatronic).

How to work:
1. open_file a binary, container or .ovt project -> one handle per binary set.
2. identify, then find_maps / list_categories / list_maps to locate parameters.
3. get_map for values, axes, units, allowed range and placement; diff to see what differs from the comparison
   (for a project set that is the as-read original).
4. To suggest a change call propose_change (one system) or propose_plan (several steps, e.g. a stage-1 tune).
   Neither modifies any file: they file proposals the user reviews and accepts in %(app)s, where checksums are
   corrected on save. Both are refused on safety-check errors: try changes first with check_changes.
5. In the app you can also pull up a table (show_map), navigate the tree (show_category) or filter it
   (filter_tree) to show the user what you mean.

Rules: every value comes from the module's definitions. Never infer a map from raw bytes or addresses. A parameter
whose placement is not trusted is a guess about where the map lives; say so when you use it. You cannot flash an
ECU or write files. Explain the reasoning for every proposed change, including risks (smoke, EGT, clutch/converter
load, torque limits)."""

from .. import branding as _branding
INSTRUCTIONS = _INSTRUCTIONS % {"app": _branding.APP_NAME}


def _target(ctx, handle):
    return ctx.workspace.get(handle)


def _cmp_info(t) -> Optional[str]:
    if t.comparison is None:
        return None
    return t.cmp_label or "comparison"


def register(reg: Registry) -> None:

    # ------------------------------------------------------------ files
    @reg.tool("open_file",
              "Open a binary (.bin), a delivery container (.sgo/.dim/.frf), a native container, or a project "
              "(.ovt) from disk, read-only. Returns one handle per target: a plain file gives one, a project "
              "gives one per binary set (ECU, TCU, ...) on its active branch unless set/branch are given. "
              "compare_with (files only) loads a second binary to compare against; project sets always compare "
              "against their as-read original.",
              {"type": "object", "required": ["path"], "properties": {
                  "path": {"type": "string", "description": "Absolute path of the file."},
                  "set": {"type": "string", "description": "Project only: the set name or id to open."},
                  "branch": {"type": "string", "description": "Project only: branch name (default: active)."},
                  "compare_with": {"type": "string", "description": "File only: path of a binary to compare against."}}},
              title="Open file")
    def open_file(ctx, path, set=None, branch=None, compare_with=None):
        ts = ctx.workspace.open(path, set_name=set, branch=branch,
                                compare_with=compare_with)
        if compare_with:
            for t in ts:
                t.set_comparison(compare_with)
        return {"opened": [t.describe() for t in ts]}

    @reg.tool("list_open", "List the targets currently open, with their handles.", title="List open")
    def list_open(ctx):
        live = bool(getattr(ctx.workspace, "live", False))
        d = {"open": [t.describe() for t in ctx.workspace.all()], "live": live}
        if not live:
            d["note"] = ("the app is not running (or not sharing its tabs): only "
                         "files opened here are listed")
        return d

    @reg.tool("current_view",
              "What the user is looking at in the app right now: the active tab's handle and the selected "
              "parameter's id (and the table tabs open in it). live=false when the app is not running -- then "
              "only files opened with open_file exist.",
              title="Current view")
    def current_view(ctx):
        return ctx.workspace.view()

    @reg.tool("set_comparison",
              "File targets: compare against another binary from disk (or clear it with no path).",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H, "path": {"type": "string", "description": "Binary to compare against; omit to clear."}}},
              title="Set comparison")
    def set_comparison(ctx, handle, path=None):
        t = _target(ctx, handle)
        if t.kind != "file":
            raise ToolError("project sets compare against their as-read original")
        import os
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(path):
                raise ToolError("no such file: %s" % path)
        t.set_comparison(path)
        return {"handle": handle, "comparison": _cmp_info(t)}

    # ------------------------------------------------------------ identity
    @reg.tool("identify",
              "What the target is: platform, software family, how fully the app supports it (full definitions, "
              "checksum only, or none), load notes and warnings, number of parameters, checksum state, comparison.",
              {"type": "object", "required": ["handle"], "properties": {"handle": H}},
              title="Identify")
    def identify(ctx, handle):
        t = _target(ctx, handle)
        s = t.session
        ident = getattr(s, "binary_identity", None)
        mod = getattr(s, "module", None)
        d = {"handle": handle, "label": t.label,
             "platform": getattr(s, "platform", ""),
             "family": getattr(s, "family", None),
             "identity": ident.describe() if ident is not None else "",
             "support": getattr(s, "support", ""),
             "module": ({"id": mod.id, "name": mod.name, "version": mod.version}
                        if mod is not None else None),
             "parameters": len(s.entities),
             "size": len(s.binfile.data)}
        for k in ("unsupported_reason", "load_note", "sgo_warning", "frf_warning"):
            v = getattr(s, k, None)
            if v:
                d[k] = v
        if t.notes:
            d["notes"] = list(t.notes)
        try:
            d["checksum"] = D.checksum_json(s.checksum_status())
        except Exception as ex:
            d["checksum"] = {"ok": None, "report": "checksum check failed: %s" % ex}
        d["comparison"] = _cmp_info(t)
        return d

    @reg.tool("file_information",
              "What the app's File information panel says about the target: identification read "
              "from the image's own records (part number, software, dataset, blocks), container, "
              "definition and parse facts -- for every platform, including ones without "
              "parameters (checksum-only modules). VIN / immobiliser / SKC values are withheld.",
              {"type": "object", "required": ["handle"], "properties": {"handle": H}},
              title="File information")
    def file_information(ctx, handle):
        from ..fileinfo import rows_for_session
        from .redact import KEY, text as _vin
        t = _target(ctx, handle)
        try:
            rows = rows_for_session(t.session)
        except Exception as ex:
            raise ToolError("no file information: %s" % ex)
        out = []
        for label, value in rows:
            v = str(value)
            if KEY.search(str(label)):
                v = "[withheld]"
            else:
                v = _vin(v)[0]
            out.append({"field": str(label), "value": v})
        return {"handle": handle, "rows": out}

    @reg.tool("project_info",
              "For a project set: the project's sets, members, branches and vehicle details. VIN and immobiliser "
              "data are never returned.",
              {"type": "object", "required": ["handle"], "properties": {"handle": H}},
              title="Project info")
    def project_info(ctx, handle):
        t = _target(ctx, handle)
        if t.kind != "project-set":
            raise ToolError("%s is a plain file, not a project set" % handle)
        p = t.project
        info = {k: v for k, v in (p.info or {}).items() if not _REDACT.search(k)}
        sets = []
        for s in p.sets:
            sets.append({
                "name": s.name, "platform": s.platform, "kind": s.kind,
                "members": [m.label for m in s.members],
                "active_branch": s.active.name if s.active else None,
                "branches": [{"name": b.name,
                              "head": (s.checkpoint(b.head).label if s.checkpoint(b.head) else ""),
                              "checkpoints": len(s.lineage(b.head))} for b in s.branches]})
        return {"project": p.name, "info": info, "sets": sets,
                "logs": len(p.logs), "attachments": len(p.attachments),
                "patches": len(p.patches)}

    # ------------------------------------------------------------ parameters
    @reg.tool("list_categories",
              "The parameter tree's top-level categories with how many parameters each holds. Use it to find "
              "where to look before list_maps.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H, "depth": {"type": "integer", "description": "Category levels (1-3, default 1)."}}},
              title="List categories")
    def list_categories(ctx, handle, depth=1):
        t = _target(ctx, handle)
        depth = max(1, min(3, int(depth)))
        counts = {}
        for e in t.index.ordered:
            path = list(getattr(e, "category_path", None) or e.categories or ["(uncategorised)"])
            key = " / ".join(path[:depth])
            counts[key] = counts.get(key, 0) + 1
        return {"categories": [{"category": k, "parameters": n} for k, n in counts.items()]}

    @reg.tool("list_maps",
              "List parameters (maps, curves, constants) with id, title, kind, shape, units and category, in the "
              "app's tree order. Filter by category text, kind, or changed_only (differs from the comparison). "
              "Paged: use offset/limit.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H,
                  "category": {"type": "string", "description": "Substring of the category path."},
                  "kind": {"type": "string", "enum": ["map", "curve", "constant", "indexed"]},
                  "changed_only": {"type": "boolean"},
                  "offset": {"type": "integer"}, "limit": {"type": "integer", "description": "Max 500, default 100."}}},
              title="List maps")
    def list_maps(ctx, handle, category=None, kind=None, changed_only=False,
                  offset=0, limit=100):
        t = _target(ctx, handle)
        limit = max(1, min(500, int(limit)))
        cmp = t.comparison if changed_only else None
        if changed_only and cmp is None:
            raise ToolError("changed_only needs a comparison (set_comparison, or a project set)")
        cat = (category or "").lower()
        rows = []
        for e in t.index.ordered:
            if kind and e.kind != kind:
                continue
            if cat:
                path = " / ".join(getattr(e, "category_path", None) or e.categories or [])
                if cat not in path.lower():
                    continue
            if changed_only and not D.changed(e, t.counterpart(e)):
                continue
            rows.append(e)
        page = rows[int(offset):int(offset) + limit]
        return {"total": len(rows), "offset": int(offset),
                "maps": [D.summary(t.index, e) for e in page]}

    @reg.tool("find_maps",
              "Search parameters by name, symbol, description, category or units, using the app's own search "
              "(fuzzy by default). Returns the best matches with ids for get_map.",
              {"type": "object", "required": ["handle", "query"], "properties": {
                  "handle": H, "query": {"type": "string"},
                  "match": {"type": "string", "enum": ["fuzzy", "exact", "regex"]},
                  "limit": {"type": "integer", "description": "Default 25, max 200."}}},
              title="Find maps")
    def find_maps(ctx, handle, query, match="fuzzy", limit=25):
        from .. import search as S
        from ..tiers import tier_name
        t = _target(ctx, handle)
        eng = _engine(S, tier_name)
        opts = S.SearchOptions(mode="text", match=match,
                               max_results=max(1, min(200, int(limit))))
        try:
            hits = eng.run(query, opts, [S.Source(t.label, t.session)])
        except S.QueryError as ex:
            raise ToolError(str(ex))
        out = []
        for h in hits[:opts.max_results]:
            d = D.summary(t.index, h.entity)
            if h.where:
                d["matched"] = h.where
            out.append(d)
        return {"query": query, "results": out}

    @reg.tool("get_map",
              "One parameter in full: description, values (display units, after the definition's equation), axes "
              "with units, allowed range, placement grade, and the comparison's values with the changed cells.",
              {"type": "object", "required": ["handle", "map"], "properties": {
                  "handle": H, "map": {"type": "string", "description": "Id from find_maps/list_maps."},
                  "comparison": {"type": "boolean", "description": "Include the comparison (default true)."}}},
              title="Get map")
    def get_map(ctx, handle, map, comparison=True):
        t = _target(ctx, handle)
        e = t.index.resolve(map)
        cp = t.counterpart(e) if comparison else None
        d = D.detail(t.index, e, cp, with_comparison=bool(comparison and t.comparison is not None))
        if comparison and t.comparison is not None:
            d["comparison_label"] = _cmp_info(t)
        return d

    @reg.tool("diff",
              "Every named parameter that differs from the comparison (a project set: its as-read original), "
              "paired by definition identity, not by address. Shows changed-cell counts and the first change.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H, "limit": {"type": "integer", "description": "Default 200."}}},
              title="Diff")
    def diff(ctx, handle, limit=200):
        from .. import patch as _patch
        t = _target(ctx, handle)
        cmp = t.comparison
        if cmp is None:
            raise ToolError("no comparison: use set_comparison, or open a project set")
        # the comparison is the "from" side: entries carry what changed TO
        entries = _patch.diff_entities(cmp, t.session)
        out = []
        for pe in entries[:max(1, int(limit))]:
            try:
                e = t.index.resolve(pe.name)
                eid, ttl = t.index.eid(e), D.title(e)
            except ToolError:
                eid, ttl = pe.name, pe.label or pe.name
            out.append({"id": eid, "title": ttl,
                        "changed_cells": pe.changed_cells,
                        "total_cells": pe.total_cells, "first_change": pe.first_change})
        return {"comparison": _cmp_info(t), "changed": len(entries),
                "first_change_reads": "comparison -> current", "maps": out,
                "truncated": len(entries) > len(out)}

    @reg.tool("history",
              "Version history: for a project set the checkpoints of its branch (newest first: label, time, bytes "
              "changed, log lines); for a file the saved versions from its change log.",
              {"type": "object", "required": ["handle"], "properties": {
                  "handle": H, "limit": {"type": "integer", "description": "Default 50."}}},
              title="History")
    def history(ctx, handle, limit=50):
        t = _target(ctx, handle)
        limit = max(1, int(limit))
        if t.kind == "project-set":
            s = t.project.set(t.sid)
            b = s.branch(t.branch)
            lin = s.lineage(b.head)          # newest first
            return {"branch": b.name, "checkpoints": [
                {"id": c.id, "label": c.label, "kind": c.kind, "time": c.ts,
                 "bytes_changed": c.n_bytes, "log": list(c.log or [])[:20]}
                for c in lin[:limit]]}
        from .. import history as _h
        vs = _h.list_versions(t.session)
        return {"versions": [{"id": v.get("id"), "label": _h.display_label(v),
                              "time": v.get("ts", ""), "pending": v.get("pending")}
                             for v in vs[:limit]]}

    @reg.tool("checksum_status",
              "Verify the target's checksums with its module. Read-only: corrections happen when the user saves "
              "in the app.",
              {"type": "object", "required": ["handle"], "properties": {"handle": H}},
              title="Checksum status")
    def checksum_status(ctx, handle):
        t = _target(ctx, handle)
        rep = D.checksum_json(t.session.checksum_status())
        if rep is None:
            return {"supported": False,
                    "why": "no installed module can check this binary's checksum"}
        rep["supported"] = True
        return rep

    # ------------------------------------------------------------ the window
    def _ui(ctx, handle):
        ui = getattr(ctx.workspace, "ui", None)
        if ui is None:
            raise ToolError("the app's window is not available (not running, or not sharing "
                            "its tabs): only files can be read")
        t = _target(ctx, handle)
        if not getattr(t, "ws", None):
            raise ToolError("%s is not a tab in the app" % handle)
        return ui, t

    @reg.tool("show_map",
              "Pull up a parameter's table in the app for the user: selects it in the parameter tree and "
              "opens its table (new_tab keeps the current table open beside it). Optionally selects cells "
              "to point at.",
              {"type": "object", "required": ["handle", "map"], "properties": {
                  "handle": H, "map": {"type": "string"},
                  "new_tab": {"type": "boolean"},
                  "cells": {"type": "array", "description": "[[row, col], ...] to select",
                            "items": {"type": "array", "items": {"type": "integer"}}}}},
              title="Show map")
    def show_map(ctx, handle, map, new_tab=False, cells=None):
        ui, t = _ui(ctx, handle)
        e = t.index.resolve(map)
        ui.show_map(t, e, bool(new_tab), cells or [])
        return {"shown": t.index.eid(e), "title": D.title(e), "tab": handle}

    @reg.tool("show_category",
              "Navigate the parameter tree to a category (e.g. \"Smoke limit\" or \"Power / Boost\"): "
              "expands it, scrolls to it and collapses the rest.",
              {"type": "object", "required": ["handle", "category"], "properties": {
                  "handle": H, "category": {"type": "string"}}},
              title="Show category")
    def show_category(ctx, handle, category):
        ui, t = _ui(ctx, handle)
        path = ui.show_category(t, category)
        if not path:
            raise ToolError("no category %r in the tree; list_categories shows them" % category)
        return {"shown": path, "tab": handle}

    @reg.tool("filter_tree",
              "Filter the app's parameter tree to rows matching text (empty text clears it).",
              {"type": "object", "required": ["handle", "text"], "properties": {
                  "handle": H, "text": {"type": "string"}}},
              title="Filter tree")
    def filter_tree(ctx, handle, text):
        ui, t = _ui(ctx, handle)
        n = ui.filter_tree(t, text)
        return {"filter": text, "visible_parameters": n, "tab": handle}

    # ------------------------------------------------------------ logs
    LOG = {"type": "object", "properties": {
        "handle": {"type": "string", "description": "A project target whose logs to use."},
        "log": {"type": "string", "description": "Log id or file name from list_logs."},
        "path": {"type": "string", "description": "Or: absolute path of a log file (.csv)."}}}

    def _log(ctx, handle=None, log=None, path=None):
        from . import logs as L
        if path:
            import os
            return L.load_path(os.path.abspath(os.path.expanduser(path)))
        if not (handle and log):
            raise ToolError("give handle + log (from list_logs) or path")
        t = _target(ctx, handle)
        if t.kind != "project-set":
            raise ToolError("%s is not a project: give the log's path instead" % handle)
        p = t.project
        a = next((x for x in p.logs if log in (x.id, x.filename)), None)
        if a is None:
            raise ToolError("no log %r in the project; list_logs shows them" % log)
        return L.load_bytes(p.log_bytes(a.id), a.filename)

    @reg.tool("list_logs",
              "The data logs kept in a project (all sets), newest last: id, file name, when added.",
              {"type": "object", "required": ["handle"], "properties": {"handle": H}},
              title="List logs")
    def list_logs(ctx, handle):
        t = _target(ctx, handle)
        if t.kind != "project-set":
            raise ToolError("%s is not a project; logs on disk are read by path" % handle)
        p = t.project
        sets = {s.id: s.name for s in p.sets}
        return {"logs": [{"id": a.id, "file": a.filename, "added": a.added,
                          "set": sets.get(a.set, "") if a.set else ""} for a in p.logs]}

    @reg.tool("log_summary",
              "Summarise a data log without sending it: duration, sample rate and, per channel, unit, "
              "min/max (and when), mean, 5th/50th/95th percentile. Start here for any log question.",
              LOG, title="Log summary")
    def log_summary(ctx, handle=None, log=None, path=None):
        from . import logs as L
        lg = _log(ctx, handle, log, path)
        return L.summary(lg)

    @reg.tool("log_window",
              "Channels of a data log over a time range, down-sampled to at most 200 rows: for looking "
              "at one pull, shift or event found with log_summary.",
              {"type": "object", "properties": dict(LOG["properties"], **{
                  "channels": {"type": "array", "items": {"type": "string"},
                               "description": "Channel names (or indexes as text); default all."},
                  "start_s": {"type": "number"}, "end_s": {"type": "number"},
                  "max_rows": {"type": "integer", "description": "Default 200."}})},
              title="Log window")
    def log_window(ctx, handle=None, log=None, path=None, channels=None,
                   start_s=None, end_s=None, max_rows=200):
        from . import logs as L
        lg = _log(ctx, handle, log, path)
        return L.window(lg, channels, start_s, end_s, max_rows)

    # ------------------------------------------------------------ help
    @reg.tool("search_help",
              "Search the app's help pages (and the installed modules' pages): how to use a feature, "
              "where a menu item is, what a dialog does. Returns page ids with a snippet.",
              {"type": "object", "required": ["query"], "properties": {
                  "query": {"type": "string"}, "limit": {"type": "integer"}}},
              title="Search help")
    def search_help(ctx, query, limit=8):
        idx = _help_index()
        hits = idx.search(query, limit=int(limit))
        if not hits:                       # any word, ranked by how many match
            import re as _re
            words = [w for w in _re.split(r"\W+", query.lower()) if len(w) > 2]
            scored = []
            for p in idx.pages.values():
                low = (p.title + " " + p.text).lower()
                n = sum(1 for w in words if w in low)
                if n:
                    scored.append((-n, p.title, p))
            scored.sort(key=lambda x: (x[0], x[1]))
            hits = [(p, "") for _n, _t, p in scored[:int(limit)]]
        return {"results": [{"page": p.id, "title": p.title, "snippet": snip}
                            for p, snip in hits]}

    @reg.tool("read_help", "One help page's text (Markdown).",
              {"type": "object", "required": ["page"], "properties": {"page": {"type": "string"}}},
              title="Read help")
    def read_help(ctx, page):
        p = _help_index().get(page)
        if p is None:
            raise ToolError("no help page %r; search_help finds them" % page)
        text = p.text
        return {"page": p.id, "title": p.title,
                "text": text if len(text) < 20000 else text[:20000] + "\n...(cut)"}

    # ------------------------------------------------------------ proposals
    change = {"type": "object", "required": ["map"], "properties": {
        "map": {"type": "string", "description": "Parameter id."},
        "values": {"type": "array", "description": "Full table [rows][cols] in display units (a constant: [[v]]).",
                   "items": {"type": "array", "items": {"type": "number"}}},
        "cells": {"type": "array", "description": "Cell edits in display units.",
                  "items": {"type": "object", "required": ["row", "col", "value"], "properties": {
                      "row": {"type": "integer"}, "col": {"type": "integer"},
                      "value": {"type": "number"}}}},
        "scale": {"type": "number", "description": "Multiply cells by this factor."},
        "add": {"type": "number", "description": "Add this to cells (display units)."},
        "rows": {"type": "array", "description": "Limit scale/add to these row indices.",
                 "items": {"type": "integer"}},
        "cols": {"type": "array", "description": "Limit scale/add to these column indices.",
                 "items": {"type": "integer"}}}}

    @reg.tool("check_changes",
              "Try changes WITHOUT filing anything: runs the same safety checks a proposal must pass (values "
              "that do not fit their cells, the platform's own tuning rules) and reports, per parameter, how "
              "many cells would change. Use it to iterate until there are no errors, then propose.",
              {"type": "object", "required": ["handle", "changes"], "properties": {
                  "handle": H, "changes": {"type": "array", "items": change}}},
              title="Check changes")
    def check_changes(ctx, handle, changes):
        t = _target(ctx, handle)
        pairs = P.resolve_changes(t, changes)
        problems = P.check(t, pairs)
        maps = []
        for e, want in pairs:
            cur = e.values()
            n = sum(1 for r in range(e.rows) for c in range(e.cols)
                    if abs(e.clamp(want[r][c]) - cur[r][c]) > 1e-9)
            maps.append({"map": t.index.eid(e), "title": D.title(e), "cells_changed": n})
        return {"ok": not P.errors(problems), "problems": problems, "maps": maps}

    @reg.tool("propose_plan",
              "Propose a multi-step change (a stage-1 tune, a shift-feel rework ...) as ordered steps, each a "
              "group of changes with its own title and rationale. Later steps build on earlier ones. The whole "
              "plan is safety-checked as one; nothing is written -- the user reviews the steps and accepts the "
              "plan or single steps, each becoming its own version.",
              {"type": "object", "required": ["handle", "title", "goal", "steps"], "properties": {
                  "handle": H, "title": {"type": "string"},
                  "goal": {"type": "string", "description": "What the plan achieves, and its risks."},
                  "steps": {"type": "array", "items": {
                      "type": "object", "required": ["title", "rationale", "changes"], "properties": {
                          "title": {"type": "string"}, "rationale": {"type": "string"},
                          "changes": {"type": "array", "items": change}}}}}},
              read_only=False, title="Propose plan")
    def propose_plan(ctx, handle, title, goal, steps):
        t = _target(ctx, handle)
        props = P.build_plan(t, title, goal, steps, client=ctx.client)
        for p in props:
            P.save(p)
        return {"plan": props[0]["plan"]["id"], "status": "pending review in the app",
                "steps": [{"proposal": p["id"], "title": p["title"],
                           "maps": [x["map"] for x in p["summary"] if x.get("changed_cells")]}
                          for p in props],
                "checks": props[0].get("checks", [])}

    @reg.tool("propose_change",
              "Propose changes to one or more parameters. Does NOT modify any file: files a pending proposal the "
              "user reviews (diff view) and accepts or rejects in the app. Values are rounded to what each cell "
              "can hold; a value outside a cell's range, or a broken platform rule, refuses the proposal (see "
              "check_changes). Give a clear title and a rationale that explains the intent and the risks. For "
              "changes across several systems use propose_plan.",
              {"type": "object", "required": ["handle", "title", "rationale", "changes"], "properties": {
                  "handle": H, "title": {"type": "string"}, "rationale": {"type": "string"},
                  "changes": {"type": "array", "items": change}}},
              read_only=False, title="Propose change")
    def propose_change(ctx, handle, title, rationale, changes):
        t = _target(ctx, handle)
        prop = P.build(t, title, rationale, changes, client=ctx.client)
        P.save(prop)
        return {"proposal": prop["id"], "status": "pending review in the app",
                "summary": prop["summary"], "checks": prop.get("checks", [])}

    @reg.tool("list_proposals",
              "Pending proposals, for one target (handle) or all.",
              {"type": "object", "properties": {"handle": H}},
              title="List proposals")
    def list_proposals(ctx, handle=None):
        if handle:
            ps = P.for_target(_target(ctx, handle).identity())
        else:
            ps = P.all_pending()
        return {"proposals": [P.brief(p) for p in ps]}

    @reg.tool("get_proposal", "One pending proposal in full (rationale, per-map summary).",
              {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
              title="Get proposal")
    def get_proposal(ctx, id):
        p = P.load(id)
        return {k: p[k] for k in ("id", "title", "rationale", "created", "client",
                                  "target", "summary") if k in p}

    @reg.tool("withdraw_proposal", "Delete a pending proposal you made that is no longer wanted.",
              {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
              read_only=False, title="Withdraw proposal")
    def withdraw_proposal(ctx, id):
        P.remove(id)
        return {"withdrawn": id}


_ENGINE = {}
_HELP = {}


def _help_index():
    if "i" not in _HELP:
        from ..helpdocs import HelpIndex
        _HELP["i"] = HelpIndex()
    return _HELP["i"]


def _engine(S, tier_name):
    if "e" not in _ENGINE:
        try:
            titles = S.load_shipped_titles()
        except Exception:
            titles = None
        _ENGINE["e"] = S.SearchEngine(D.title, tier_name, titles)
    return _ENGINE["e"]
