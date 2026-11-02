# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A scan as text (for a forum post) and as a self-contained HTML page,
with the diff against an earlier scan when one is given.
"""
import html
import time
from typing import List, Optional

from .. import branding
from .journal import Entry
from .model import ModuleInfo, ScanDiff, ScanResult, Vehicle, diff_scans


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _ident_line(m: ModuleInfo) -> str:
    bits = []
    if m.part:
        bits.append(m.part)
    if m.sw:
        bits.append("SW " + m.sw)
    if m.coding_text:
        bits.append("coding " + m.coding_text)
    if m.wsc is not None:
        bits.append("WSC %05d" % m.wsc)
    if not bits and m.identity_text:
        bits.append(m.identity_text)
    return " · ".join(bits)


def text_report(vehicle: Vehicle, scan: ScanResult, before: Optional[ScanResult] = None,
                journal: Optional[List[Entry]] = None) -> str:
    lines = ["%s scan - %s" % (branding.TOOL_NAMES.get("diag", "OVT Diag"), vehicle.label)]
    if vehicle.vin:
        lines.append("VIN: %s" % vehicle.vin)
    lines.append("Scanned: %s  (%s over %s, %.0f s)" % (_when(scan.when), scan.interface,
                                                        scan.transport.upper(), scan.duration_s))
    reached = scan.reached
    lines.append("Modules: %d of %d answered, %d fault%s%s"
                 % (len(reached), len(scan.visited), scan.fault_total,
                    "" if scan.fault_total == 1 else "s",
                    "  (the modules the gateway lists as installed)" if scan.basis == "gateway" else ""))
    if scan.stopped:
        lines.append("SCAN STOPPED before it finished: %d module%s not tried"
                     % (len(scan.modules) - len(scan.visited), "" if len(scan.modules) - len(scan.visited) == 1 else "s"))
    diff = diff_scans(before, scan) if before is not None else None
    if diff is not None:
        lines.append("Compared with %s: +%d new, -%d cleared, %d module%s changed"
                     % (_when(before.when), diff.new_faults, diff.cleared_faults,
                        len(diff.changed), "" if len(diff.changed) == 1 else "s"))
    lines.append("-" * 72)
    for m in scan.modules:
        head = "%s  %s" % (m.addr, m.name)
        if m.reached is None:
            lines.append("%-28s not tried%s" % (head, (" (%s)" % m.error) if m.error and "not tried" not in m.error else ""))
            continue
        if not m.reached:
            lines.append("%-28s not reached%s" % (head, (" (%s)" % m.error) if m.error else ""))
            if diff is not None:
                md = next((d for d in diff.modules if d.addr == m.addr), None)
                if md is not None and md.lost:
                    lines.append("    ~ no longer answers (answered on %s)" % _when(before.when))
            continue
        lines.append(head)
        il = _ident_line(m)
        if il:
            lines.append("    %s" % il)
        if m.protocol:
            lines.append("    %s over %s" % (m.protocol.upper(), m.transport.upper() or "?"))
        if m.faults:
            lines.append("    %d fault%s:" % (len(m.faults), "" if len(m.faults) == 1 else "s"))
            for f in m.faults:
                lines.append("      %s  %s%s" % (f.display_code, f.text or "(no text)",
                                                 ("  [%s]" % f.status_text) if f.status_text else ""))
        else:
            lines.append("    no faults")
        if diff is not None:
            md = next((d for d in diff.modules if d.addr == m.addr), None)
            if md is not None and not md.empty:
                for f in md.new_faults:
                    lines.append("    + new: %s %s" % (f.display_code, f.text))
                for f in md.cleared_faults:
                    lines.append("    - cleared: %s %s" % (f.display_code, f.text))
                for c in md.changes:
                    lines.append("    ~ %s" % c)
                if md.newly_reached:
                    lines.append("    ~ newly reached")
    gw = gateway_lines(scan)
    if gw:
        lines.append("-" * 72)
        lines += gw
    if journal:
        lines.append("-" * 72)
        lines.append("Journal since the previous scan:")
        for e in journal:
            lines.append("  %s  %s" % (_when(e.when), e.summary))
    lines.append("-" * 72)
    lines.append("%s %s" % (branding.APP_NAME, branding.__version__))
    return "\n".join(lines)


def gateway_lines(scan: ScanResult) -> List[str]:
    """The gateway's installation list as text rows (installed modules
    first), empty when the scan read none."""
    if not scan.gateway:
        return []
    from .gateway import InstallationList
    lst = InstallationList.from_dict(scan.gateway)
    out = ["Gateway installation list (%s): %d modules installed of %d listed"
           % (lst.gateway_addr, len(lst.installed), len(lst.entries))]
    for e in sorted(lst.entries, key=lambda x: (not x.installed, x.addr)):
        m = scan.module(e.addr)
        seen = ""
        if e.installed:
            seen = "  answered" if (m is not None and m.reached) else "  no answer" if m is not None else ""
        out.append("  %s  %-30s dest 0x%02X  %-9s %s%s" % (e.addr, e.name or "", e.dest, e.bus_name, e.status_text, seen))
    return out


def html_report(vehicle: Vehicle, scan: ScanResult, before: Optional[ScanResult] = None,
                journal: Optional[List[Entry]] = None) -> str:
    e = html.escape
    diff: Optional[ScanDiff] = diff_scans(before, scan) if before is not None else None
    rows = []
    for m in scan.modules:
        if m.reached is None:
            rows.append('<tr class="nr"><td>%s</td><td>%s</td><td colspan="3">not tried (scan stopped)</td></tr>'
                        % (e(m.addr), e(m.name)))
            continue
        if not m.reached:
            lost = ""
            if diff is not None:
                md = next((d for d in diff.modules if d.addr == m.addr), None)
                if md is not None and md.lost:
                    lost = " · no longer answers (answered on %s)" % _when(before.when)
            rows.append('<tr class="nr"><td>%s</td><td>%s</td><td colspan="3">not reached %s%s</td></tr>'
                        % (e(m.addr), e(m.name), e("(%s)" % m.error if m.error else ""), e(lost)))
            continue
        status = ("%d fault%s" % (len(m.faults), "" if len(m.faults) == 1 else "s")
                  if m.faults else "no faults")
        change = ""
        if diff is not None:
            md = next((d for d in diff.modules if d.addr == m.addr), None)
            if md is not None and not md.empty:
                parts = (["+ %s %s" % (f.display_code, e(f.text)) for f in md.new_faults]
                         + ["- %s %s" % (f.display_code, e(f.text)) for f in md.cleared_faults]
                         + [e(c) for c in md.changes] + (["newly reached"] if md.newly_reached else []))
                change = "<br>".join(parts)
        rows.append('<tr><td>%s</td><td>%s</td><td class="mono">%s</td><td class="%s">%s</td><td>%s</td></tr>'
                    % (e(m.addr), e(m.name), e(_ident_line(m)), "bad" if m.faults else "ok", status, change))
        for f in m.faults:
            rows.append('<tr class="fault"><td></td><td colspan="4"><span class="mono">%s</span> %s'
                        '<span class="muted"> %s</span></td></tr>'
                        % (e(f.display_code), e(f.text or "(no text)"), e(f.status_text)))
    grows = ""
    if scan.gateway:
        from .gateway import InstallationList
        lst = InstallationList.from_dict(scan.gateway)
        body = []
        for g in sorted(lst.entries, key=lambda x: (not x.installed, x.addr)):
            m = scan.module(g.addr)
            seen = ("answered" if (m is not None and m.reached) else "no answer" if m is not None else "") if g.installed else ""
            body.append('<tr class="%s"><td>%s</td><td>%s</td><td class="mono">0x%02X</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                        % ("" if g.installed else "nr", e(g.addr), e(g.name), g.dest, e(g.bus_name), e(g.status_text), e(seen)))
        grows = ('<h2>Gateway installation list (%s): %d installed of %d listed</h2><table>'
                 '<tr><td class="muted">Addr</td><td class="muted">Module</td><td class="muted">TP 2.0</td>'
                 '<td class="muted">Bus</td><td class="muted">Status</td><td class="muted">Scan</td></tr>%s</table>'
                 % (e(lst.gateway_addr), len(lst.installed), len(lst.entries), "\n".join(body)))
    jrows = ""
    if journal:
        jrows = "<h2>Journal</h2><ul>" + "".join("<li><span class=\"muted\">%s</span> %s</li>"
                                              % (e(_when(x.when)), e(x.summary)) for x in journal) + "</ul>"
    summary = "%d of %d modules answered, %d faults" % (len(scan.reached), len(scan.visited), scan.fault_total)
    if scan.stopped:
        summary += " · SCAN STOPPED: %d not tried" % (len(scan.modules) - len(scan.visited))
    if diff is not None:
        summary += " · vs %s: +%d new, -%d cleared, %d changed" % (
            _when(before.when), diff.new_faults, diff.cleared_faults, len(diff.changed))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1a1e24;background:#fff}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:20px 0 6px}
.muted{color:#5b6472}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
table{border-collapse:collapse;width:100%%;margin-top:14px}td{padding:6px 8px;border-top:1px solid #e3e7ec;vertical-align:top}
tr.fault td{border-top:0;padding-top:0;padding-bottom:8px}tr.nr td{color:#5b6472}
.ok{color:#1f7a4d}.bad{color:#b23b2a;font-weight:600}
@media(prefers-color-scheme:dark){body{background:#14181e;color:#e6ebf1}td{border-color:#2b333e}.muted,tr.nr td{color:#93a0b0}.ok{color:#4fc08d}.bad{color:#e5705d}}
</style></head><body>
<h1>%(vehicle)s</h1>
<div class="muted">%(vin)s%(when)s · %(iface)s over %(transport)s · %(summary)s</div>
<table><tr><td class="muted">Addr</td><td class="muted">Module</td><td class="muted">Identification</td><td class="muted">Status</td><td class="muted">Change</td></tr>
%(rows)s
</table>
%(gateway)s
%(journal)s
<p class="muted">%(app)s %(ver)s</p>
</body></html>
""" % {"title": e("%s scan %s" % (vehicle.label, _when(scan.when))), "vehicle": e(vehicle.label),
       "vin": e("VIN %s · " % vehicle.vin) if vehicle.vin else "", "when": e(_when(scan.when)),
       "iface": e(scan.interface), "transport": e(scan.transport.upper()), "summary": e(summary),
       "rows": "\n".join(rows), "journal": jrows, "gateway": grows, "app": e(branding.APP_NAME),
       "ver": e(branding.__version__)}
