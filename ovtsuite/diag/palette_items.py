# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the command palette (Ctrl+K) can find: the :class:`PaletteItem` list
for the window's current state.

Qt-free and car-free. The window fills a :class:`Context` from what it already
knows (the last scan, the live sessions, the definition packs) and calls
:func:`build_items` every time the palette opens; nothing here reads the car.
Every source is independent: one that fails contributes nothing and is logged,
the others still build.

The kinds, what each is built from, and the payload the window routes on
(``gui_diag.DiagWindow._palette_activated``):

``module``       a reached module. ``addr``; title the name (``Engine``);
                 subtitle ``03G906016LQ . EDC16U31 . KWP2000 over TP 2.0``;
                 chip = the fault count when there are faults.
                 payload ``{}``.
``live``         a named cell of a measuring group of a reached module (names
                 from the definitions or the factory catalogue). Subtitle
                 ``Engine . group 011 cell 3`` and ``. Ldm_pIst from the
                 factory catalogue`` when the catalogue gives the signal;
                 ``value`` is the reading while one is flowing. payload
                 ``{"group": 11, "cell": 3}`` (``cell`` counts from 1, as
                 the subtitle does).
``fault``        a stored fault of a reached module in the last scan.
                 Keywords carry every form of the code: the VAG number
                 (``01044`` and ``001044``), the SAE code (``P0299``), the
                 word in hex (``0414``, ``0x0414``). payload ``{"code": word,
                 "addr": addr, "index": position in the module's list}``.
``coding``       a named bit of the module's long-coding definition. payload
                 ``{"byte": 2, "bit": 3}``.
``adaptation``   an adaptation entry of the definitions. payload
                 ``{"channel": 2, "id": entry id}``.
``actuator``     an actuator test the definitions name. payload ``{"id": entry id}``.
``procedure``    a guided procedure that applies to a reached module; one item
                 per procedure id, the modules it applies to in the subtitle.
                 payload ``{"id": procedure id, "addr": first module}``.
``action``       a window action. payload ``{"action": "<name>"}``, the names
                 in :data:`ACTIONS`.

Every payload also carries ``"key"``, a string that identifies the item among
the module's items of its kind; with ``addr`` and ``kind`` it addresses the
item for :meth:`CommandPalette.set_values` (see :func:`value_key`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from .defs.schema import as_int
from .model import Fault, ModuleInfo
from .palette_index import PaletteItem

log = logging.getLogger(__name__)

#: the action names an ``action`` item's payload can carry
ACTIONS = ("scan_all", "stop_scan", "connect", "disconnect", "disconnect_all", "open_journal", "export_html",
           "export_pdf", "export_text", "open_tools", "open_obd", "open_garage", "open_gateway", "trace_toggle",
           "install_pack", "reload_definitions", "open_definitions_folder")

_DOT = " · "
_PROTO = {"kw1281": "KW1281", "kwp2000": "KWP2000", "uds": "UDS"}


@dataclass
class Context:
    """What the window knows, handed to :func:`build_items`."""
    #: the definitions :class:`~ovtsuite.diag.defs.Resolver` (coding, adaptation, actuator tests, procedures)
    resolver: Any = None
    generation: str = ""
    #: :class:`ModuleInfo` from the last scan and the live sessions; only the reached ones count
    modules: Sequence[ModuleInfo] = ()
    #: the address-table rows by module address (a module name the scan did not give)
    rows_by_addr: Mapping[str, dict] = field(default_factory=dict)
    #: addresses with an open (or opening) live session
    lives: Collection[str] = ()
    #: ``{(addr, group, cell): (text, unit)}`` of the live values flowing now (cell from 1)
    readings: Mapping[Tuple[str, int, int], Tuple[str, str]] = field(default_factory=dict)
    #: ``addr -> {group: [cell name or None, ...]}``; None = no live names at all
    names_for: Optional[Callable[[str], Dict[int, List[Optional[str]]]]] = None
    #: ``addr -> {(group, cell): signal name}`` (cell from 1) from the factory catalogue
    signals_for: Optional[Callable[[str], Dict[Tuple[int, int], str]]] = None
    #: ``addr -> [procedure documents]`` (``procedures.available``); None = ask the resolver
    procedures_for: Optional[Callable[[str], List[dict]]] = None
    selected_addr: str = ""
    recording: bool = False             # trace recording is on
    scanning: bool = False              # a scan is running


def value_key(item: PaletteItem) -> tuple:
    """``(addr, kind, key)`` for :meth:`CommandPalette.set_values`."""
    return (item.addr, item.kind, (item.payload or {}).get("key", ""))


def protocol_label(m: ModuleInfo) -> str:
    """``KWP2000 over TP 2.0``, ``KW1281 @ 9600``, ``UDS``: what the module answered with."""
    if not m.protocol:
        return ""
    base = _PROTO.get(m.protocol, m.protocol.upper())
    if m.protocol == "uds":
        return base
    if m.transport == "can":
        return base + " over TP 2.0"
    baud = as_int((m.extra or {}).get("baud"), None)
    return "%s @ %d" % (base, baud) if baud else base


def module_name(m: ModuleInfo, ctx: Context) -> str:
    return m.name or (ctx.rows_by_addr.get(m.addr) or {}).get("name", "") or m.addr


def _ident(m: ModuleInfo, ctx: Context):
    return m.identity.with_(generation=ctx.generation or None)


def _text(v) -> str:
    return "" if v is None else str(v)


def fault_title(f: Fault) -> str:
    return f.text or "(no text)"


def fault_forms(f: Fault) -> str:
    """Every form the user might type for ``f``: VAG number, SAE code, the word in hex."""
    forms: List[str] = []
    if f.vag is not None:
        forms += ["%05d" % f.vag, "%06d" % f.vag]
    if f.sae:
        forms.append(f.sae)
    word = int(f.code or 0)
    forms += ["%04X" % word, "0x%04X" % word]
    return " ".join(forms)


def fault_chip(f: Fault) -> Tuple[str, str]:
    """``(chip, chip_kind)``: ``active`` (bad) while present, ``history`` (ok) when intermittent only."""
    return ("history", "ok") if f.intermittent else ("active", "bad")


# ------------------------------------------------------------------ sources
def _modules(ctx: Context) -> List[ModuleInfo]:
    seen = set()
    out = []
    for m in ctx.modules or ():
        if m.reached and m.addr not in seen:
            seen.add(m.addr)
            out.append(m)
    return sorted(out, key=lambda m: m.addr)


def _module_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    out = []
    for m in mods:
        bits = [m.part or m.identity_text, (m.extra or {}).get("system_name") or "", protocol_label(m)]
        if bits[0] and bits[1] == bits[0]:
            bits[1] = ""
        n = m.fault_count
        chip = str(n) if n else ""
        kind = ("warn" if all(f.intermittent for f in m.faults) else "bad") if n else ""
        out.append(PaletteItem(
            "module", module_name(m, ctx), addr=m.addr, subtitle=_DOT.join(b for b in bits if b),
            keywords=" ".join(x for x in (m.addr, m.part, m.identity_text, m.platform) if x),
            chip=chip, chip_kind=kind, payload={"key": m.addr}))
    return out


def _live_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    if ctx.names_for is None:
        return []
    out = []
    for m in mods:
        try:
            names = ctx.names_for(m.addr) or {}
            signals = (ctx.signals_for(m.addr) if ctx.signals_for is not None else None) or {}
        except Exception as ex:
            log.warning("palette: live names of %s: %s", m.addr, ex)
            continue
        mname = module_name(m, ctx)
        uds = m.protocol == "uds"
        for g in sorted(names):
            for i, name in enumerate(names[g] or [], 1):
                if not name:
                    continue
                if uds:
                    sub = "%s%slive value" % (mname, _DOT)
                else:
                    sub = "%s%sgroup %03d cell %d" % (mname, _DOT, g, i)
                sig = signals.get((g, i), "")
                if sig:
                    sub += "%s%s from the factory catalogue" % (_DOT, sig)
                val = ctx.readings.get((m.addr, g, i))
                value = ""
                if val:
                    value = (val[0] + (" " + val[1] if val[1] else "")).strip()
                out.append(PaletteItem("live", str(name), addr=m.addr, subtitle=sub, keywords=sig, value=value,
                                       payload={"group": g, "cell": i, "key": "%d.%d" % (g, i)}))
    return out


def _fault_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    out = []
    for m in mods:
        mname = module_name(m, ctx)
        for idx, f in enumerate(m.faults):
            chip, kind = fault_chip(f)
            sub = mname + _DOT + (f.status_text or "stored")
            out.append(PaletteItem("fault", fault_title(f), addr=m.addr, subtitle=sub, keywords=fault_forms(f),
                                   chip=chip, chip_kind=kind,
                                   payload={"code": f.code, "addr": m.addr, "index": idx,
                                            "key": "%d.%d" % (f.code, idx)}))
    return out


def _coding_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    if ctx.resolver is None:
        return []
    out = []
    for m in mods:
        try:
            cdoc = ctx.resolver.coding_doc(_ident(m, ctx))
        except Exception as ex:                 # a bad module's definitions leave the others alone
            log.warning("palette: coding of %s: %s", m.addr, ex)
            continue
        mname = module_name(m, ctx)
        for b in cdoc.get("bytes") or []:
            idx = as_int(b.get("index"), 0)
            for bit in b.get("bits") or []:
                name = bit.get("name")
                if not name:
                    continue
                bits = bit.get("bits") or ([bit.get("bit")] if bit.get("bit") is not None else [])
                bits = [as_int(x, 0) for x in bits]
                first = min(bits) if bits else 0
                where = "byte %d bit %s" % (idx, ",".join(str(x) for x in sorted(bits)) if bits else "0")
                opts = " ".join(_text(v) for v in (bit.get("options") or {}).values())
                out.append(PaletteItem("coding", str(name), addr=m.addr, subtitle=mname + _DOT + where,
                                       keywords=opts, payload={"byte": idx, "bit": first,
                                                               "key": "%d.%d" % (idx, first)}))
    return out


def _adaptation_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    if ctx.resolver is None:
        return []
    out = []
    for m in mods:
        try:
            entries = ctx.resolver.view("adaptation", _ident(m, ctx)).entries
        except Exception as ex:
            log.warning("palette: adaptation of %s: %s", m.addr, ex)
            continue
        mname = module_name(m, ctx)
        for eid, r in sorted(entries.items(), key=lambda kv: as_int(kv[1].entry.get("channel"), 0)):
            e = r.entry
            name = e.get("name")
            if not name:
                continue
            ch = as_int(e.get("channel"), 0)
            out.append(PaletteItem("adaptation", str(name), addr=m.addr,
                                   subtitle="%s%schannel %02d" % (mname, _DOT, ch),
                                   keywords=" ".join(x for x in (_text(e.get("unit")), eid) if x),
                                   payload={"channel": ch, "id": eid, "key": eid}))
    return out


def _actuator_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    if ctx.resolver is None:
        return []
    out = []
    for m in mods:
        try:
            entries = ctx.resolver.view("actuator_tests", _ident(m, ctx)).entries
        except Exception as ex:
            log.warning("palette: actuator tests of %s: %s", m.addr, ex)
            continue
        mname = module_name(m, ctx)
        for eid, r in entries.items():
            e = r.entry
            name = e.get("name")
            if not name:
                continue
            tid = as_int(e.get("tester_id"), None)
            sub = mname + (_DOT + "test 0x%04X" % tid if tid is not None else "")
            out.append(PaletteItem("actuator", "Actuator test: %s" % name, addr=m.addr, subtitle=sub,
                                   keywords="actuator output test " + str(name),
                                   payload={"id": eid, "key": eid}))
    return out


def _procedures_for(ctx: Context, m: ModuleInfo) -> List[dict]:
    if ctx.procedures_for is not None:
        return list(ctx.procedures_for(m.addr) or [])
    if ctx.resolver is None:
        return []
    from . import procedures
    return procedures.available(ctx.resolver, _ident(m, ctx))


def _procedure_items(ctx: Context, mods: List[ModuleInfo]) -> List[PaletteItem]:
    docs: Dict[str, dict] = {}
    where: Dict[str, List[ModuleInfo]] = {}
    for m in mods:
        try:
            found = _procedures_for(ctx, m)
        except Exception as ex:
            log.warning("palette: procedures of %s: %s", m.addr, ex)
            continue
        for d in found:
            pid = str(d.get("id") or d.get("name") or "")
            if not pid:
                continue
            docs.setdefault(pid, d)
            where.setdefault(pid, []).append(m)
    out = []
    for pid, d in docs.items():
        mods_ = where[pid]
        n = len(d.get("steps") or [])
        sub = "Guided%s%d step%s%s%s" % (_DOT, n, "" if n == 1 else "s", _DOT, d.get("safety") or "read-only")
        if len(mods_) <= 3:
            sub += _DOT + ", ".join(module_name(m, ctx) for m in mods_)
        else:
            sub += _DOT + "%d modules" % len(mods_)
        sel = next((m for m in mods_ if m.addr == ctx.selected_addr), mods_[0])
        out.append(PaletteItem("procedure", "Procedure: %s" % (d.get("name") or pid), addr=sel.addr, subtitle=sub,
                               keywords=" ".join(x for x in (_text(d.get("description")), pid) if x),
                               payload={"id": pid, "addr": sel.addr, "key": pid}))
    return out


def _action_items(ctx: Context) -> List[PaletteItem]:
    def act(name, title, sub="", kw="", value=""):
        return PaletteItem("action", title, subtitle=sub, keywords=kw, value=value,
                           payload={"action": name, "key": name})

    out = [act("scan_all", "Scan all modules", "Visit every module of the table and read its faults",
               "find modules scan vehicle", "F5")]
    if ctx.scanning:
        out.append(act("stop_scan", "Stop scan", "End the scan after the current module", "cancel abort"))
    sel = ctx.selected_addr
    if sel:
        mod = next((m for m in ctx.modules or () if m.addr == sel), None)
        name = (module_name(mod, ctx) if mod is not None else (ctx.rows_by_addr.get(sel) or {}).get("name", ""))
        label = ("%s %s" % (sel, name)).strip()
        if sel in ctx.lives:
            out.append(act("disconnect", "Disconnect %s" % label, "Close the session on the selected module",
                           "session close", "Ctrl+L"))
        else:
            out.append(act("connect", "Connect %s" % label, "Open a session on the selected module",
                           "session open", "Ctrl+L"))
    if ctx.lives:
        out.append(act("disconnect_all", "Disconnect all", "Close every open session", "sessions close"))
    out += [
        act("open_journal", "Open journal", "Every write made to this vehicle", "writes log history"),
        act("export_html", "Export report as HTML", "The scan report as a web page", "save scan report"),
        act("export_pdf", "Export report as PDF", "The scan report as a PDF file", "save scan report"),
        act("export_text", "Export report as text", "Copy the scan report as plain text", "copy scan report"),
        act("open_tools", "Open Vehicle tools", "Gateway list, vehicle live data, OBD-II, journal, garage"),
        act("open_obd", "Open OBD-II", "Generic emissions diagnostics", "obd emissions readiness"),
        act("open_garage", "Open Garage", "Vehicles and their saved scans", "vehicles scans"),
        act("open_gateway", "Open gateway list", "The modules the gateway lists as installed", "installation list"),
        act("trace_toggle", "Stop trace recording" if ctx.recording else "Start trace recording",
            "Write every byte on the link to a file", "trace log record bus"),
        act("install_pack", "Install pack...", "Add a definitions pack", "definitions import"),
        act("reload_definitions", "Reload definitions", "Read every definitions pack again"),
        act("open_definitions_folder", "Open definitions folder", "Your local definitions packs", "folder packs"),
    ]
    return out


# --------------------------------------------------------------------- build
def build_items(ctx: Context) -> List[PaletteItem]:
    """Every searchable thing in ``ctx``. Never raises: a source that fails
    contributes nothing (it is logged)."""
    try:
        mods = _modules(ctx)
    except Exception as ex:
        log.warning("palette: modules: %s", ex)
        mods = []
    out: List[PaletteItem] = []
    for source in (_module_items, _live_items, _fault_items, _coding_items, _adaptation_items, _actuator_items,
                   _procedure_items):
        try:
            out += source(ctx, mods)
        except Exception as ex:
            log.warning("palette: %s: %s", source.__name__.strip("_"), ex)
    try:
        out += _action_items(ctx)
    except Exception as ex:
        log.warning("palette: actions: %s", ex)
    return out
