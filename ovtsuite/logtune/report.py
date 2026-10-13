# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Render a :class:`~ovtsuite.logtune.analysis.Result` as text or HTML."""
from __future__ import annotations

import html
from typing import List

from .analysis import CRITICAL, GOOD, INFO, WARN, Result

_ICON = {CRITICAL: "!!", WARN: "! ", INFO: "- ", GOOD: "ok"}
_COLOR = {CRITICAL: "#c62828", WARN: "#b26a00", INFO: "#546e7a", GOOD: "#2e7d32"}
_WORD = {CRITICAL: "Critical", WARN: "Warning", INFO: "Info", GOOD: "OK"}


def _table_text(head: List[str], rows: List[List[str]]) -> List[str]:
    cols = list(zip(*([head] + rows))) if rows else [[h] for h in head]
    widths = [max(len(str(x)) for x in c) for c in cols]
    line = lambda r: "  " + "  ".join(str(x).rjust(w) for x, w in zip(r, widths))
    return [line(head), "  " + "  ".join("-" * w for w in widths)] + [line(r) for r in rows]


def to_text(res: Result, changes_limit: int = 60) -> str:
    out = []
    for f in res.sorted_findings():
        out.append("[%s] %s: %s" % (_ICON.get(f.severity, "  "), f.topic.upper(), f.title))
        if f.detail:
            out.append("     " + f.detail)
        if f.table:
            out.extend(_table_text(*f.table))
        out.append("")
    plan = res.plan
    if plan is not None and plan.effective_writes:
        out.append("PROPOSED CHANGES (%d cells, %d map(s)) -- %s"
                   % (len(plan.effective_writes), len(plan.targets()),
                      "ready to apply" if plan.ok else "BLOCKED"))
        head = ["Map", "X", "Y", "Old", "New", "Weight", "Why"]
        rows = [[c.map, "%g" % c.x, "%g" % c.y, "%.2f" % c.old, "%.2f" % c.new,
                 "%.1f" % c.weight, c.reason] for c in res.cells[:changes_limit]]
        out.extend(_table_text(head, rows))
        if len(res.cells) > changes_limit:
            out.append("  ... %d more" % (len(res.cells) - changes_limit))
    else:
        out.append("No calibration changes proposed.")
    return "\n".join(out)


def to_html(res: Result) -> str:
    e = html.escape
    parts = ["<html><body style='font-family:sans-serif'>"]
    for f in res.sorted_findings():
        col = _COLOR.get(f.severity, "#333")
        parts.append("<p style='margin:10px 0 2px 0'><span style='color:%s;font-weight:bold'>%s</span>"
                     " &nbsp;<b>%s</b> <span style='color:#888'>(%s)</span></p>"
                     % (col, _WORD.get(f.severity, ""), e(f.title), e(f.topic)))
        if f.detail:
            parts.append("<p style='margin:0 0 4px 12px'>%s</p>" % e(f.detail))
        if f.table:
            head, rows = f.table
            parts.append("<table cellspacing=0 cellpadding=3 border=1 style='margin-left:12px;"
                         "border-collapse:collapse;border-color:#ccc'>")
            parts.append("<tr>" + "".join("<th style='background:#eee'>%s</th>" % e(h) for h in head) + "</tr>")
            for r in rows:
                parts.append("<tr>" + "".join("<td align=right>%s</td>" % e(str(x)) for x in r) + "</tr>")
            parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)
