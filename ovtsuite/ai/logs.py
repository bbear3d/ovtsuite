# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data logs for the AI: summaries computed here, never whole logs.

A log of a few minutes is tens of thousands of cells; sending it would cost
a fortune and drown the model. Instead :func:`summary` gives per-channel
statistics (range, mean, percentiles, where the extremes are) and
:func:`window` a down-sampled slice of chosen channels over a time range --
which is what a question about one pull or one shift needs.
"""

from __future__ import annotations

import os
import tempfile
from typing import List, Optional, Sequence

from .describe import num
from .tools import ToolError

MAX_WINDOW_ROWS = 200


def load_path(path: str):
    from ..datalog import logfile as _lf
    if not os.path.isfile(path):
        raise ToolError("no such log file: %s" % path)
    try:
        return _lf.load(path)
    except Exception as ex:
        raise ToolError("could not read %s as a data log: %s" % (os.path.basename(path), ex))


def load_bytes(data: bytes, filename: str):
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(filename)[1] or ".csv")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        log = load_path(tmp)
        log.path = filename
        return log
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def channel_name(c) -> str:
    return c.title


def summary(log, name: str = "") -> dict:
    t = log.seconds()
    dur = t[-1] if t else 0.0
    chans = []
    for i, c in enumerate(log.channels):
        col = log.column(i)
        pts = [(t[k], v) for k, v in enumerate(col) if v is not None and k < len(t)]
        d = {"index": i, "name": channel_name(c)}
        if c.unit:
            d["unit"] = c.unit
        if c.signal:
            d["signal"] = c.signal
        if not c.is_memory:
            d["group"], d["field"] = c.group, c.field + 1
        if not pts:
            d["samples"] = 0
            chans.append(d)
            continue
        vals = sorted(v for _t, v in pts)
        mn = min(pts, key=lambda p: p[1])
        mx = max(pts, key=lambda p: p[1])
        d.update(samples=len(pts), min=num(mn[1]), min_at_s=num(round(mn[0], 2)),
                 max=num(mx[1]), max_at_s=num(round(mx[0], 2)),
                 mean=num(sum(vals) / len(vals)), p5=num(_pct(vals, .05)),
                 median=num(_pct(vals, .5)), p95=num(_pct(vals, .95)))
        chans.append(d)
    rate = (len(t) - 1) / dur if dur > 0 and len(t) > 1 else None
    out = {"log": name or os.path.basename(getattr(log, "path", "") or "") or "log",
           "rows": log.rows, "duration_s": num(round(dur, 2)),
           "rows_per_s": num(round(rate, 2)) if rate else None,
           "channels": chans}
    if getattr(log, "identity", ""):
        out["ecu"] = log.identity
    if log.stamps:
        out["started"] = log.stamps[0].isoformat(sep=" ", timespec="seconds")
    return out


def _pick(log, channels: Optional[Sequence]) -> List[int]:
    if not channels:
        return list(range(len(log.channels)))
    out = []
    for ch in channels:
        if isinstance(ch, int) or (isinstance(ch, str) and ch.isdigit()):
            i = int(ch)
            if not 0 <= i < len(log.channels):
                raise ToolError("no channel %d (0..%d)" % (i, len(log.channels) - 1))
            out.append(i)
            continue
        low = str(ch).lower()
        hits = [i for i, c in enumerate(log.channels)
                if low in (channel_name(c).lower(), (c.signal or "").lower())]
        if not hits:
            hits = [i for i, c in enumerate(log.channels)
                    if low in channel_name(c).lower() or low in (c.signal or "").lower()]
        if not hits:
            raise ToolError("no channel matches %r; channels: %s" % (
                ch, ", ".join(channel_name(c) for c in log.channels)))
        out.extend(h for h in hits if h not in out)
    return out


def window(log, channels=None, start_s=None, end_s=None,
           max_rows: int = MAX_WINDOW_ROWS) -> dict:
    t = log.seconds()
    idx = _pick(log, channels)
    lo = 0 if start_s is None else next((k for k, x in enumerate(t) if x >= start_s), len(t))
    hi = len(t) if end_s is None else next((k for k, x in enumerate(t) if x > end_s), len(t))
    rows = list(range(lo, hi))
    max_rows = max(2, min(int(max_rows), MAX_WINDOW_ROWS))
    step = max(1, -(-len(rows) // max_rows))
    picked = rows[::step]
    return {"columns": ["t_s"] + ["%s%s" % (channel_name(log.channels[i]),
                                            " [%s]" % log.channels[i].unit
                                            if log.channels[i].unit else "") for i in idx],
            "rows": [[num(round(t[k], 2))] + [num(log.values[k][i]) for i in idx]
                     for k in picked],
            "every_nth_row": step, "rows_in_range": len(rows)}


def paths_for(ctx, handle=None, logs=None, paths=None) -> List[str]:
    """Files on disk for the logs a tool was given: project logs (by id or
    file name, from the project of ``handle``) are written to a private temp
    folder; paths are used as they are. For analyses that read files."""
    out = []
    for p in paths or ():
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(p):
            raise ToolError("no such log file: %s" % p)
        out.append(p)
    if logs:
        if not handle:
            raise ToolError("project logs need the project's handle")
        t = ctx.workspace.get(handle)
        if t.kind != "project-set":
            raise ToolError("%s is not a project: give log paths instead" % handle)
        d = tempfile.mkdtemp(prefix="ovt-ai-logs-")
        for want in logs:
            a = next((x for x in t.project.logs if want in (x.id, x.filename)), None)
            if a is None:
                raise ToolError("no log %r in the project; list_logs shows them" % want)
            path = os.path.join(d, "%s_%s" % (a.id, os.path.basename(a.filename) or "log.csv"))
            with open(path, "wb") as fh:
                fh.write(t.project.log_bytes(a.id))
            out.append(path)
    if not out:
        raise ToolError("give logs (project log ids) or paths")
    return out
