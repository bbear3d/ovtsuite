# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pending AI proposals: changes an AI suggests and the user accepts or not.

A proposal never touches a binary. It is a JSON file in the per-user inbox
(``<config dir>/ai/proposals``, ``OVT_AI_DIR`` overrides) holding:

* what it is for -- a file (path + SHA-256 of the bytes it was made against)
  or a project set/branch (project uid, set, branch, base checkpoint);
* a title and the AI's rationale;
* the change itself as an ``ovtsuite.patch`` Patch (entries by KB name, in
  display units), plus each entry's durable entity reference so a parameter
  without a symbol (a structurally found ZF schedule) is found again.

The inbox lives outside the ``.ovt`` so an MCP server running beside the app
never writes a project the app has open. Accepting one goes through
:func:`apply_to_session`: one undo step, a change-log record (revertable like
any patch apply), placement rules unchanged.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Dict, List, Optional

from .. import patch as _patch
from ..model import placement_trusted, values_equal
from .describe import num, title
from .tools import ToolError

ENV_DIR = "OVT_AI_DIR"
VERSION = 1
PENDING = "pending"


def inbox_dir() -> str:
    override = os.environ.get(ENV_DIR)
    if override:
        root = override
    else:
        from ..recent import _config_dir
        root = os.path.join(_config_dir(), "ai")
    d = os.path.join(root, "proposals")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ building
def _cells_from_change(e, ch: dict, current: List[List[float]]) -> List[List[float]]:
    """The full proposed table for one change request."""
    new = [list(r) for r in current]
    given = [k for k in ("values", "cells", "scale", "add") if ch.get(k) is not None]
    if not given:
        raise ToolError("change for %s: give values, cells, scale or add" % ch.get("map"))
    if "values" in given:
        vals = ch["values"]
        if e.kind == "constant" and not isinstance(vals, list):
            vals = [[vals]]
        if (not isinstance(vals, list) or len(vals) != e.rows
                or any(not isinstance(r, list) or len(r) != e.cols for r in vals)):
            raise ToolError("change for %s: values must be %d rows x %d columns"
                            % (ch.get("map"), e.rows, e.cols))
        new = [[float(v) for v in r] for r in vals]
    for cell in ch.get("cells") or ():
        if isinstance(cell, dict):
            r, c, v = cell.get("row"), cell.get("col"), cell.get("value")
        elif isinstance(cell, (list, tuple)) and len(cell) == 3:
            r, c, v = cell
        else:
            raise ToolError("change for %s: each cell is {row, col, value}" % ch.get("map"))
        if not (isinstance(r, int) and isinstance(c, int)
                and 0 <= r < e.rows and 0 <= c < e.cols):
            raise ToolError("change for %s: cell [%s,%s] is outside %dx%d"
                            % (ch.get("map"), r, c, e.rows, e.cols))
        new[r][c] = float(v)
    rows = ch.get("rows")       # optional restriction of scale/add
    cols = ch.get("cols")
    rr = range(e.rows) if rows is None else [int(i) for i in rows]
    cc = range(e.cols) if cols is None else [int(i) for i in cols]
    if ch.get("scale") is not None:
        for r in rr:
            for c in cc:
                new[r][c] *= float(ch["scale"])
    if ch.get("add") is not None:
        for r in rr:
            for c in cc:
                new[r][c] += float(ch["add"])
    return new


def resolve_changes(target, changes: List[dict], start=None):
    """``[(entity, wanted table)]`` for ``changes``; several changes to one
    parameter compose in the order given. ``start`` (id(entity) -> table)
    continues from earlier steps of a plan."""
    if not changes:
        raise ToolError("no changes given")
    idx = target.index
    work, order = {}, []
    for ch in changes:
        if not isinstance(ch, dict) or not ch.get("map"):
            raise ToolError("each change needs a 'map' id")
        e = idx.resolve(ch["map"])
        from .describe import protected
        if protected(e):
            raise ToolError("%s is identity / immobiliser data; AI proposals do not change it"
                            % ch["map"])
        key = id(e)
        if key not in work:
            base = (start or {}).get(key)
            work[key] = [e, [list(r) for r in base] if base else e.values()]
            order.append(key)
        work[key][1] = _cells_from_change(e, ch, work[key][1])
    return [tuple(work[k]) for k in order]


def _load_platform_checks(session) -> None:
    """A module registers its tuning checks when its ``tuning`` package is
    imported (ZF 6HP: shift-point hysteresis, ceilings ...)."""
    mod = getattr(session, "module", None)
    if mod is None:
        return
    import importlib
    from .. import branding
    try:
        importlib.import_module("%s.%s.tuning" % (branding.PKG, mod.id))
    except ImportError:
        pass


def check(target, pairs) -> List[dict]:
    """The auto-tune safety checks over ``pairs`` -- run on the simulated
    result, nothing written. ``error`` problems block a proposal."""
    from ..autotune.plan import Plan
    session = target.session
    _load_platform_checks(session)
    plan = Plan(session, label="AI check")
    for e, want in pairs:
        cur = e.values()
        for r in range(e.rows):
            for c in range(e.cols):
                if not values_equal(want[r][c], cur[r][c], 1e-9):
                    plan.set(e, r, c, want[r][c], reason="AI")
    probs = plan.validate()
    return [{"severity": p.severity, "code": p.code, "message": p.message,
             "where": p.where} for p in probs]


def errors(problems) -> List[dict]:
    return [p for p in problems if p.get("severity") == "error"]


def build(target, title_text: str, rationale: str, changes: List[dict], *,
          client: str = "", pairs=None, problems=None, plan: Optional[dict] = None) -> dict:
    """A proposal dict for ``changes`` against ``target`` (a workspace
    Target). Values are clamped and quantised exactly as the cell would hold
    them, and every adjustment is reported back. Refused when the safety
    checks find an error (a value that does not fit its cell, a platform rule
    broken)."""
    s = target.session
    idx = target.index
    entries, refs, report = [], {}, []
    from ..session import entity_ref
    if pairs is None:
        pairs = resolve_changes(target, changes)
    if problems is None:
        problems = check(target, pairs)
    if errors(problems):
        raise ToolError("not filed -- the safety checks found errors; fix the "
                        "changes and try again (check_changes tries without "
                        "filing): %s" % json.dumps(errors(problems)))
    for e, want in pairs:
        cur = e.values()
        held, adjusted, n = [], [], 0
        first = ""
        low, high = e.value_range
        for r in range(e.rows):
            row = []
            for c in range(e.cols):
                v = e.clamp(want[r][c])
                # rounding to the cell's resolution is normal (the stored
                # value is what is shown); only a value the cell cannot reach
                # at all is worth telling the user about
                if want[r][c] < low - 1e-9 or want[r][c] > high + 1e-9:
                    adjusted.append([r, c, num(want[r][c]), num(v)])
                if not values_equal(v, cur[r][c], 1e-9):
                    n += 1
                    if not first:
                        first = "[%d,%d] %s -> %s" % (r, c, num(cur[r][c]), num(v))
                row.append(v)
            held.append(row)
        eid = idx.eid(e)
        item = {"map": eid, "title": title(e), "changed_cells": n,
                "trusted_placement": placement_trusted(e)}
        if adjusted:
            item["adjusted"] = {"columns": ["row", "col", "asked", "stored"],
                                "rows": adjusted[:100],
                                "why": "outside the allowed range %s..%s"
                                       % tuple(num(x) for x in e.value_range)}
        report.append(item)
        if n == 0:
            item["note"] = "no change: already holds these values"
            continue
        name = e.name or eid
        entries.append(_patch.PatchEntry(
            name=name, kind=e.kind, rows=e.rows, cols=e.cols, values=held,
            x_axis=e.x_values(), y_axis=e.y_values(), units=e.units,
            label=getattr(e, "label", ""), description=e.description,
            categories=list(e.categories), tier=e.tier,
            source_grade=getattr(e, "source", ""), changed_cells=n,
            total_cells=e.n_cells, first_change=first))
        ref = entity_ref(e)
        if ref:
            refs[name] = ref
    if not entries:
        raise ToolError("nothing would change: %s" % json.dumps(report))
    p = _patch.Patch(created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                     source_family=getattr(s, "family", "") or "",
                     source_name=target.label,
                     comparison_family=getattr(s, "family", "") or "",
                     comparison_name="AI: %s" % title_text, entries=entries)
    out = {"version": VERSION, "id": uuid.uuid4().hex[:12],
           "created": p.created, "status": PENDING, "client": client,
           "title": title_text.strip() or "AI proposal",
           "rationale": rationale.strip(), "target": target.identity(),
           "platform": getattr(s, "platform", ""),
           "family": getattr(s, "family", "") or "",
           "summary": report, "refs": refs, "patch": p.to_dict(),
           "checks": problems}
    if plan:
        out["plan"] = plan
    return out


def build_plan(target, title_text: str, goal: str, steps: List[dict], *,
               client: str = "") -> List[dict]:
    """A plan: one proposal per step, in order, linked by a plan id. The
    checks run on the whole plan at once (steps interact -- boost against the
    smoke limiter, an upshift against its downshift), and each step's
    changes build on the steps before it."""
    if not steps:
        raise ToolError("a plan needs at least one step")
    state, staged = {}, []
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not st.get("changes"):
            raise ToolError("step %d has no changes" % (i + 1))
        pairs = resolve_changes(target, st["changes"], start=state)
        for e, want in pairs:
            state[id(e)] = want
        staged.append(pairs)
    # the whole plan, as it would end up
    final = {}
    for pairs in staged:
        for e, want in pairs:
            final[id(e)] = (e, want)
    problems = check(target, list(final.values()))
    if errors(problems):
        raise ToolError("plan not filed -- the safety checks found errors in the "
                        "plan as a whole: %s" % json.dumps(errors(problems)))
    pid = uuid.uuid4().hex[:10]
    out = []
    prev = {}
    for i, (st, pairs) in enumerate(zip(steps, staged)):
        # a step is judged against the state the earlier steps leave
        base_pairs = []
        for e, want in pairs:
            base_pairs.append((e, want))
        meta = {"id": pid, "title": title_text.strip() or "AI plan",
                "goal": goal.strip(), "step": i + 1, "steps": len(steps)}
        prop = build(target, st.get("title") or "Step %d" % (i + 1),
                     st.get("rationale", ""), st["changes"], client=client,
                     pairs=base_pairs, problems=problems, plan=meta)
        prop["title"] = "%s - %d/%d: %s" % (meta["title"], i + 1, len(steps),
                                            st.get("title") or "step %d" % (i + 1))
        out.append(prop)
        for e, want in pairs:
            prev[id(e)] = want
    return out


# ------------------------------------------------------------------ inbox
def save(prop: dict) -> str:
    path = os.path.join(inbox_dir(), "%s.json" % prop["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(prop, fh, indent=1)
    os.replace(tmp, path)
    return path


def load(pid: str) -> dict:
    if not pid or any(ch in pid for ch in "/\\.:"):
        raise ToolError("bad proposal id %r" % pid)
    path = os.path.join(inbox_dir(), "%s.json" % pid)
    if not os.path.isfile(path):
        raise ToolError("no pending proposal %r" % pid)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def all_pending() -> List[dict]:
    out = []
    d = inbox_dir()
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                p = json.load(fh)
        except (OSError, ValueError):
            continue
        if p.get("status") == PENDING:
            out.append(p)
    out.sort(key=lambda p: p.get("created", ""))
    return out


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def match(prop: dict, identity: dict):
    """Whether ``prop`` was made for the target ``identity`` describes.

    Returns ``(matches, note)``: a project set matches on project + set (the
    branch or the version it was made against may differ -- ``note`` says
    so); a file on its path, or on the exact bytes it was made against."""
    t = prop.get("target", {})
    if identity.get("kind") == "project-set":
        if not (t.get("project_uid") == identity.get("project_uid")
                and t.get("set_id") == identity.get("set_id")):
            return False, ""
        if t.get("branch_id") != identity.get("branch_id"):
            return True, ("made for branch \"%s\"; this is \"%s\""
                          % (t.get("branch_name", "?"), identity.get("branch_name", "?")))
        if t.get("base_checkpoint") != identity.get("base_checkpoint"):
            return True, "made against an earlier version of this branch"
        return True, ""
    if t.get("kind") != "file":
        return False, ""
    if _same_path(t.get("path", ""), identity.get("path", "")):
        sha = identity.get("sha256")
        if sha and t.get("sha256") and sha != t["sha256"]:
            return True, "the file has changed since this was proposed"
        return True, ""
    if identity.get("sha256") and t.get("sha256") == identity.get("sha256"):
        return True, ""
    return False, ""


def for_target(identity: dict) -> List[dict]:
    """Pending proposals made for this file / project set (any branch)."""
    return [p for p in all_pending() if match(p, identity)[0]]


def resolve(pid: str, status: str, note: str = "") -> None:
    """Take a proposal out of the pending list: ``status`` is "accepted" or
    "rejected". Kept in a sub-folder of that name as a record."""
    p = load(pid)
    p["status"] = status
    p["resolved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if note:
        p["resolution"] = note
    d = os.path.join(inbox_dir(), status)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "%s.json" % pid), "w", encoding="utf-8") as fh:
        json.dump(p, fh, indent=1)
    os.remove(os.path.join(inbox_dir(), "%s.json" % pid))


def stamp() -> float:
    """Changes whenever the pending list may have: for cheap polling."""
    try:
        d = inbox_dir()
        return max([os.path.getmtime(d)] + [
            os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)
            if f.endswith(".json")])
    except OSError:
        return 0.0


def with_selection(prop: dict, names) -> dict:
    """A copy of ``prop`` whose patch applies only the entries in ``names``."""
    import copy
    q = copy.deepcopy(prop)
    keep = set(names)
    for e in q["patch"]["entries"]:
        e["selected"] = e.get("name") in keep
    for b in q.get("bytes", []):
        b["selected"] = byte_key(b) in keep
    return q


def byte_key(b: dict) -> str:
    return "bytes@0x%X" % int(b["addr"])


def build_bytes(target, title_text: str, rationale: str, edits, *, client: str = "",
                kind: str = "") -> dict:
    """A proposal of raw byte edits -- for changes a module makes to a table it
    reads structurally rather than through a named parameter (a ZF fault-code
    class byte, a VDO patch block). ``edits`` is ``[(addr, new bytes, note)]``;
    the bytes there now are recorded, and accepting refuses an edit whose bytes
    have changed since, so a stale proposal never overwrites newer work."""
    s = target.session
    data = bytes(s.binfile.data)
    out, report = [], []
    for addr, new, note in edits:
        addr, new = int(addr), bytes(new)
        if addr < 0 or addr + len(new) > len(data):
            raise ToolError("edit at 0x%X runs outside the image" % addr)
        old = data[addr:addr + len(new)]
        n = sum(1 for a, b in zip(old, new) if a != b)
        if n == 0:
            report.append({"map": "bytes@0x%X" % addr, "title": note,
                           "changed_cells": 0, "note": "no change: already holds these bytes"})
            continue
        out.append({"addr": addr, "old": old.hex(), "new": new.hex(), "note": note,
                    "selected": True})
        report.append({"map": "bytes@0x%X" % addr, "title": note, "changed_cells": n,
                       "trusted_placement": True})
    if not out:
        raise ToolError("nothing would change: %s" % json.dumps(report))
    p = _patch.Patch(created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                     source_family=getattr(s, "family", "") or "",
                     source_name=target.label, comparison_name="AI: %s" % title_text,
                     entries=[])
    return {"version": VERSION, "id": uuid.uuid4().hex[:12], "created": p.created,
            "status": PENDING, "client": client,
            "title": title_text.strip() or "AI proposal", "rationale": rationale.strip(),
            "target": target.identity(), "platform": getattr(s, "platform", ""),
            "family": getattr(s, "family", "") or "", "summary": report, "refs": {},
            "patch": p.to_dict(), "bytes": out, "kind": kind,
            "checks": [{"severity": "note", "code": "bytes",
                        "message": "a structural byte edit made by the module's own code; "
                                   "the map safety checks do not apply to it", "where": ""}]}


def entry_rows(prop: dict) -> List[dict]:
    """One row per changed parameter, for a review list: patch entry name,
    the AI-facing id and title, cells changed, trusted, adjustments."""
    by_map = {s.get("map"): s for s in prop.get("summary", [])}
    rows = []
    for b in prop.get("bytes", []):
        n = sum(1 for x, y in zip(bytes.fromhex(b["old"]), bytes.fromhex(b["new"])) if x != y)
        rows.append({"name": byte_key(b), "id": byte_key(b), "title": b.get("note") or byte_key(b),
                     "changed_cells": n, "total_cells": len(b["new"]) // 2, "trusted": True,
                     "first_change": "%d byte%s at 0x%X" % (n, "" if n == 1 else "s", b["addr"]),
                     "adjusted": 0})
    for e in prop.get("patch", {}).get("entries", []):
        name = e.get("name", "")
        s = by_map.get(name) or next(
            (x for x in prop.get("summary", []) if x.get("map", "").split("@")[0] == name), {})
        rows.append({"name": name, "id": s.get("map", name),
                     "title": s.get("title") or e.get("label") or name,
                     "changed_cells": e.get("changed_cells"),
                     "total_cells": e.get("total_cells"),
                     "trusted": s.get("trusted_placement", True),
                     "first_change": e.get("first_change", ""),
                     "adjusted": len((s.get("adjusted") or {}).get("rows", []))})
    return rows


def remove(pid: str) -> None:
    load(pid)                                  # validates the id
    os.remove(os.path.join(inbox_dir(), "%s.json" % pid))


def brief(p: dict) -> dict:
    t = p.get("target", {})
    return {"id": p["id"], "title": p.get("title", ""), "created": p.get("created", ""),
            "client": p.get("client", ""),
            "target": t.get("label") or t.get("path", ""),
            "maps": [s["map"] for s in p.get("summary", []) if s.get("changed_cells")]}


# ------------------------------------------------------------------ accepting
def apply_to_session(prop: dict, session, *, confirm_untrusted: bool = False,
                     fix_checksum: bool = False) -> _patch.ApplyResult:
    """Write ``prop`` into ``session`` as ONE undo step, logged like a patch
    apply (so it can be reverted from the change log later).

    Entries resolve by their durable reference first, then by name -- the
    target's own entity, never an address from the proposal. A shape that no
    longer matches is skipped, and an untrusted placement is skipped unless
    ``confirm_untrusted``. Checksums are left to the save, as for any edit."""
    from ..session import find_by_ref
    res = _patch.ApplyResult()
    patch = _patch.Patch.from_dict(prop["patch"])
    refs = prop.get("refs", {})
    label = "AI: %s" % prop.get("title", "proposal")
    before = session.undo_stack.top()
    with session.transaction(label):
        for pe in patch.selected_entries():
            te = find_by_ref(session, refs.get(pe.name)) if pe.name in refs else None
            if te is None:
                te = _patch.find_entity(session, pe.name)
            if te is None:
                res.skipped.append((pe.name, "not identified in this binary"))
                continue
            if (te.rows, te.cols) != (pe.rows, pe.cols):
                res.skipped.append((pe.name, "shape is now %dx%d, proposal is %dx%d"
                                    % (te.rows, te.cols, pe.rows, pe.cols)))
                continue
            if not placement_trusted(te) and not confirm_untrusted:
                res.skipped.append((pe.name, "address is %r (not established) "
                                    "and was not confirmed" % te.source))
                continue
            cells = [(r, c, pe.values[r][c]) for r in range(pe.rows)
                     for c in range(pe.cols)
                     if not values_equal(te.cell(r, c), pe.values[r][c], 1e-9)]
            if not cells:
                res.skipped.append((pe.name, "already holds these values"))
                continue
            session.edit_cells(te, cells)
            res.applied.append(pe.name)
        for b in prop.get("bytes", []):
            if not b.get("selected", True):
                continue
            key = byte_key(b)
            addr, old, new = int(b["addr"]), bytes.fromhex(b["old"]), bytes.fromhex(b["new"])
            now = bytes(session.binfile.data[addr:addr + len(new)])
            if now == new:
                res.skipped.append((key, "already holds these bytes"))
                continue
            if now != old:
                res.skipped.append((key, "these bytes changed since it was proposed"))
                continue
            session.edit_bytes(addr, new)
            if b.get("note"):
                session.log("%s: %s" % (label, b["note"]))
            res.applied.append(key)
    cmd = session.undo_stack.top()
    if cmd is not None and cmd is not before and getattr(cmd, "writes", None):
        rec = _patch._log_patch_record(session, source=label, writes=cmd.writes,
                                       n_entries=len(res.applied))
        res.record_id = rec["id"]
    if fix_checksum and res.applied:
        try:
            session.correct_checksums()
        except Exception:
            pass
    return res
