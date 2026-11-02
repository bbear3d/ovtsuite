# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag for the AI assistant and MCP clients: read-only tools.

What the assistant may do with a car is what a reader can do with the
diag window: the garage (vehicles, their saved scans, the write journal),
what a scan found (modules, identification, faults with their texts,
status words and freeze frames, the gateway's list), what two scans differ
in, what the definitions know about a module (adaptation channels,
codings, actuator tests, basic settings, procedures), what a fault code
means, and -- when a cable is plugged in and no diag window holds it -- a
read of a module's faults or measuring groups, or a generic OBD-II read.
Nothing here writes to a module: coding, adaptation, clearing and
procedures stay in the window, behind its confirmations (scope decision 8).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..ai.tools import Registry, ToolError

VID = {"type": "string", "description": "Vehicle id from diag_vehicles (or its name)."}
ADDR = {"type": "string", "description": "Module address, two hex digits as VAG lists them (\"01\" engine, \"17\" instruments)."}


def _garage():
    from .garage import Garage
    return Garage()


def _resolver():
    from . import defs as D
    return D.Resolver(D.load_all())


def _vehicle(g, vid: str):
    v = g.vehicle(vid)
    if v is None:
        for cand in g.vehicles():
            if cand.name.lower() == (vid or "").lower() or cand.vin.upper() == (vid or "").upper():
                v = cand
                break
    if v is None:
        raise ToolError("no vehicle %r in the garage; diag_vehicles lists them" % vid)
    return v


def _scan(g, v, name: Optional[str] = None):
    if name:
        s = g.load_scan(v.id, name)
        if s is None:
            raise ToolError("vehicle %s has no scan %r" % (v.label, name))
        return s
    s = g.latest_scan(v.id)
    if s is None:
        raise ToolError("vehicle %s has no saved scan yet (scan it in OVT Diag first)" % v.label)
    return s


def _fault_dict(f) -> Dict[str, Any]:
    d = {"code": f.display_code, "text": f.text, "status": f.status_text, "intermittent": f.intermittent}
    if f.vag is not None:
        d["vag"] = f.vag
    if f.sae:
        d["sae"] = f.sae
    if f.freeze:
        d["freeze_frame"] = f.freeze
    return d


def _module_dict(m, faults: bool = True) -> Dict[str, Any]:
    d = {"addr": m.addr, "name": m.name, "reached": m.reached, "protocol": m.protocol, "transport": m.transport,
         "part": m.part, "software": m.sw, "coding": m.coding_text, "workshop_code": m.wsc,
         "identification": m.identity_text, "platform": m.platform}
    for k in ("hw_part", "hw_version", "system_name"):
        if m.extra.get(k):
            d[k] = m.extra[k]
    if m.error:
        d["error"] = m.error
    if faults:
        d["faults"] = [_fault_dict(f) for f in m.faults]
    else:
        d["fault_count"] = m.fault_count
    return d


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def register(reg: Registry) -> None:

    @reg.tool("diag_vehicles",
              "The vehicles OVT Diag knows (its garage): id, name, VIN, generation, when each was last scanned "
              "and with how many faults. Start here for any question about a car's diagnostics.",
              title="Diag: vehicles")
    def diag_vehicles(ctx):
        g = _garage()
        out = []
        for v in g.vehicles():
            s = g.latest_scan(v.id)
            out.append({"id": v.id, "name": v.name, "vin": v.vin, "generation": v.generation,
                        "last_scan": _when(s.when) if s else None,
                        "last_scan_faults": s.fault_total if s else None,
                        "last_scan_modules": "%d of %d" % (len(s.reached), len(s.modules)) if s else None,
                        "scans": len(g.scans(v.id))})
        return {"vehicles": out, "count": len(out)}

    @reg.tool("diag_scan",
              "A saved scan of a vehicle (the latest unless a scan name is given): every module with its "
              "identification (part number, software, coding, workshop code) and its faults -- code in VAG and "
              "SAE form, text, status words (present / intermittent / stored, as the module's family reports "
              "them) and freeze frame -- plus the gateway's installation list when the scan read one.",
              {"type": "object", "required": ["vehicle"], "properties": {
                  "vehicle": VID,
                  "scan": {"type": "string", "description": "Scan name from diag_scan_history; default: the latest."},
                  "addr": {**ADDR, "description": "Only this module (two hex digits); default: all."},
                  "faults_only": {"type": "boolean", "description": "Only modules with faults (default false)."}}},
              title="Diag: scan")
    def diag_scan(ctx, vehicle, scan=None, addr=None, faults_only=False):
        g = _garage()
        v = _vehicle(g, vehicle)
        s = _scan(g, v, scan)
        mods = s.modules
        if addr:
            m = s.module(addr)
            if m is None:
                raise ToolError("module %s is not in that scan" % addr)
            mods = [m]
        if faults_only:
            mods = [m for m in mods if m.fault_count]
        out = {"vehicle": v.label, "vin": v.vin, "scanned": _when(s.when), "interface": s.interface,
               "transport": s.transport, "basis": s.basis, "modules_answered": len(s.reached),
               "modules_total": len(s.modules), "faults_total": s.fault_total,
               "modules": [_module_dict(m) for m in mods]}
        if s.gateway:
            from .gateway import InstallationList
            lst = InstallationList.from_dict(s.gateway)
            out["gateway_list"] = [{"addr": e.addr, "name": e.name, "tp20_dest": "0x%02X" % e.dest,
                                    "bus": e.bus_name, "status": e.status_text} for e in lst.installed]
        return out

    @reg.tool("diag_scan_history",
              "The saved scans of a vehicle, newest first, each with its date, modules answered and fault total.",
              {"type": "object", "required": ["vehicle"], "properties": {"vehicle": VID}},
              title="Diag: scan history")
    def diag_scan_history(ctx, vehicle):
        g = _garage()
        v = _vehicle(g, vehicle)
        out = []
        for name in g.scans(v.id):                      # newest first
            s = g.load_scan(v.id, name)
            if s is None:
                continue
            out.append({"scan": name, "when": _when(s.when), "modules_answered": len(s.reached),
                        "modules_total": len(s.modules), "faults": s.fault_total, "basis": s.basis})
        return {"vehicle": v.label, "scans": out}

    @reg.tool("diag_scan_diff",
              "What changed between two scans of a vehicle (default: the latest against the one before): faults "
              "that appeared or went away per module, identification fields that changed, modules newly reached "
              "or lost -- and the journal entries made between them (what was written, and whether it verified).",
              {"type": "object", "required": ["vehicle"], "properties": {
                  "vehicle": VID,
                  "before": {"type": "string", "description": "Older scan name (default: the one before the newest)."},
                  "after": {"type": "string", "description": "Newer scan name (default: the newest)."}}},
              title="Diag: scan diff")
    def diag_scan_diff(ctx, vehicle, before=None, after=None):
        from .journal import Journal
        from .model import diff_scans
        g = _garage()
        v = _vehicle(g, vehicle)
        names = g.scans(v.id)                           # newest first
        if len(names) < 2 and not (before and after):
            raise ToolError("vehicle %s has %d scan(s); a diff needs two" % (v.label, len(names)))
        a = _scan(g, v, after or names[0])
        b = _scan(g, v, before or names[1])
        d = diff_scans(b, a)
        changed = []
        for md in d.changed:
            changed.append({"addr": md.addr, "name": md.name,
                            "new_faults": [_fault_dict(f) for f in md.new_faults],
                            "cleared_faults": [_fault_dict(f) for f in md.cleared_faults],
                            "changes": md.changes, "newly_reached": md.newly_reached, "lost": md.lost})
        j = Journal(v.id)
        entries = [e for e in j.entries() if b.when <= e.when <= a.when + 1]
        return {"vehicle": v.label, "before": _when(b.when), "after": _when(a.when),
                "new_faults": d.new_faults, "cleared_faults": d.cleared_faults, "modules_changed": changed,
                "journal_between": [{"when": _when(e.when), "summary": e.summary, "verified": e.verified}
                                    for e in entries]}

    @reg.tool("diag_journal",
              "The vehicle's write journal: every coding, adaptation, fault clear and procedure OVT Diag did on "
              "it, with the value before and after and whether the module confirmed it.",
              {"type": "object", "required": ["vehicle"], "properties": {
                  "vehicle": VID, "addr": {**ADDR, "description": "Only this module; default: all."},
                  "limit": {"type": "integer", "description": "Newest N entries (default 50)."}}},
              title="Diag: journal")
    def diag_journal(ctx, vehicle, addr=None, limit=50):
        from .journal import Journal
        g = _garage()
        v = _vehicle(g, vehicle)
        j = Journal(v.id)
        entries = j.for_module(addr) if addr else j.entries()
        entries = list(reversed(entries))[:max(1, int(limit or 50))]
        return {"vehicle": v.label, "entries": [
            {"when": _when(e.when), "addr": e.addr, "module": e.module, "function": e.function,
             "entry": e.entry_id, "before": e.before, "after": e.after, "verified": e.verified,
             "note": e.note, "reverted": e.reverted} for e in entries]}

    @reg.tool("diag_fault_explain",
              "What a fault code means: its text from the definitions or the standard P-code list, both of its "
              "forms (VAG 5-digit and SAE Pxxxx), and -- given the module it was read from -- how its status "
              "byte decodes for that module's family (present, intermittent, stored, lamp). Give the code as "
              "read (\"16683\", \"P0299\", \"0x4123\") and the module address when known.",
              {"type": "object", "required": ["code"], "properties": {
                  "code": {"type": "string", "description": "The code: VAG 5-digit, SAE Pxxxx, or the raw word in hex."},
                  "addr": ADDR,
                  "status": {"type": "integer", "description": "The status byte as read (0..255), if you have it."},
                  "platform": {"type": "string", "description": "edc15 / edc16 when known (selects the family's status words)."},
                  "protocol": {"type": "string", "description": "kw1281 or kwp2000 (default kwp2000)."},
                  "part": {"type": "string", "description": "The module's part number when known: a raw 0x word is read the way that module's definition says (VAG number or SAE word)."}}},
              title="Diag: explain fault")
    def diag_fault_explain(ctx, code, addr=None, status=None, platform=None, protocol="kwp2000", part=None):
        from .. import dtc as _dtc
        from .. import dtc_report as _rep
        from .defs.model import Identity
        res = _resolver()
        text = str(code).strip().upper()
        vag = sae = None
        ident = Identity(addr=addr, platform=platform, protocol=protocol, part=part or None)
        if text.startswith("0X"):
            # a raw word: read it the way the module's definition says (the
            # module document's faults.encoding, else the controller family),
            # as the session does -- never from its magnitude
            from .session import decode_fault_word, fault_word_encoding
            word = int(text, 16)
            enc = fault_word_encoding(res.module(ident), platform or "", protocol or "kwp2000")
            vag, sae = decode_fault_word(word, protocol or "kwp2000", enc)
            sae = sae or None
        elif text[:1] in "PCBU" and len(text) == 5:
            sae = text
            try:
                from .obd import encode_dtc
                vag = _rep.vag5_of(int.from_bytes(encode_dtc(sae), "big"), _rep.ENC_SAE)
            except Exception:
                vag = None
        elif text.isdigit():
            vag = int(text)
            sae = _dtc.pcode_of_vag5(vag) or None
        else:
            raise ToolError("code %r: give a VAG 5-digit number, an SAE code like P0299, or 0x<word>" % code)
        txt = res.fault_text(ident, vag=vag, sae=sae) if (vag or sae) else ""
        if not txt and sae:
            txt = _dtc._load_pdesc().get(sae, "")
        out = {"vag": vag, "sae": sae, "text": txt or "(no text known)"}
        r = res.fault(ident, vag=vag, sae=sae) if (vag or sae) else None
        if r is not None:
            e = r.entry
            out["definition"] = {"pack": r.pack_id, "confidence": r.confidence}
            for k in ("live_group", "symptoms", "explain"):
                if e.get(k) is not None:
                    out[k] = e[k]
        if status is not None:
            si = _rep.decode_status(int(status), (platform or "").upper(), protocol or "kwp2000")
            out["status"] = {"byte": "0x%02X" % int(status), "text": si.text, "intermittent": bool(si.intermittent)}
        return out

    @reg.tool("diag_definitions",
              "What the definition packs know about a module: its address-table row (K-line address, TP 2.0 dest), "
              "the protocol variants, and the named adaptation channels, codings, actuator tests, basic settings "
              "and procedures that apply to its identity -- with each entry's source pack and confidence.",
              {"type": "object", "required": ["addr"], "properties": {
                  "addr": ADDR,
                  "generation": {"type": "string", "description": "Vehicle generation (C6, PQ35 ...), if known."},
                  "part": {"type": "string", "description": "The module's part number, if known (selects part-specific entries)."},
                  "protocol": {"type": "string", "description": "kw1281 / kwp2000 / uds, if known."},
                  "platform": {"type": "string", "description": "edc15 / edc16 / zf6hp ..., if known."}}},
              title="Diag: definitions")
    def diag_definitions(ctx, addr, generation=None, part=None, protocol=None, platform=None):
        from . import procedures as PR
        from .defs.model import Identity
        res = _resolver()
        ident = Identity(addr=addr, generation=generation, part=part, protocol=protocol, platform=platform)
        row = res.module_address(addr, generation)
        out: Dict[str, Any] = {"addressing": row, "module": res.module(ident), "coverage": res.coverage(ident)}
        for kind in ("adaptation", "actuator_tests", "basic_settings", "faults"):
            v = res.view(kind, ident)
            out[kind] = [dict(r.entry, _pack=r.pack_id, _confidence=r.confidence) for r in v.entries.values()][:200]
        cv = res.view("coding", ident)
        out["coding"] = {"top": {k: val for k, val in cv.top.items() if k != "match"},
                         "entries": [dict(r.entry, _pack=r.pack_id) for r in cv.entries.values()]}
        out["procedures"] = [{"id": d.get("id"), "name": d.get("name"), "safety": d.get("safety"),
                              "description": d.get("description"), "pack": d.get("_pack")}
                             for d in PR.available(res, ident)]
        return out

    @reg.tool("diag_interfaces",
              "The diagnostic interfaces plugged in right now (serial K-line cables, J2534 devices, the USB "
              "OpenPort, paired head units) -- what diag_read_live and diag_obd can use.",
              title="Diag: interfaces")
    def diag_interfaces(ctx):
        from . import link as L
        return {"interfaces": [{"label": i.label, "kind": i.kind, "can": i.can_capable} for i in L.interfaces()]}

    @reg.tool("diag_read_live",
              "Connect to one module on the car now (read-only), identify it, read its faults and, if asked, "
              "measuring groups; then disconnect. Needs an interface from diag_interfaces that the OVT Diag "
              "window is not holding, and the ignition on. Over CAN give the generation so the module's TP 2.0 "
              "address is known (C6 ...).",
              {"type": "object", "required": ["addr", "interface"], "properties": {
                  "addr": ADDR,
                  "interface": {"type": "string", "description": "Interface label (or a unique part of it) from diag_interfaces."},
                  "transport": {"type": "string", "description": "kline (default) or can."},
                  "generation": {"type": "string", "description": "Vehicle generation for the address table (C6 ...)."},
                  "groups": {"type": "array", "items": {"type": "integer"}, "description": "Measuring groups to read once (optional)."},
                  "can_rate": {"type": "integer", "description": "CAN bit rate (default 500000)."}}},
              read_only=True, title="Diag: read live")
    def diag_read_live(ctx, addr, interface, transport="kline", generation=None, groups=None, can_rate=500000):
        from . import link as L
        from .session import ModuleSession
        it = _iface(interface)
        res = _resolver()
        row = res.module_address(addr, generation)
        if row is None:
            raise ToolError("no module %s in the address table%s" % (addr, (" for " + generation) if generation else ""))
        log: List[str] = []
        link = L.open_link(it, transport or "kline", row, log.append, can_rate=int(can_rate or 500000))
        sess = ModuleSession(link, row, res, log.append, generation=generation or "")
        try:
            sess.connect()
            sess.read_faults()
            out = _module_dict(sess.info)
            if groups:
                out["groups"] = {}
                for g in groups:
                    r = sess.read_group_full(int(g))
                    if r is None or not r.available:
                        out["groups"]["%03d" % int(g)] = None
                        continue
                    names = sess.group_names(int(g))
                    out["groups"]["%03d" % int(g)] = [
                        {"cell": "%03d.%d" % (int(g), i + 1), "name": names[i] if i < len(names) else None,
                         "value": c.decoded[2], "raw": "%02X %02X %02X" % (c.fid, c.a, c.b)}
                        for i, c in enumerate(r.cells or [])]
            out["log"] = log[-20:]
            return out
        except Exception as ex:
            raise ToolError("could not read %s on %s: %s" % (addr, it.label, ex))
        finally:
            try:
                sess.close()
            except Exception:
                pass

    @reg.tool("diag_obd",
              "Generic OBD-II (SAE J1979) read on the car now: readiness monitors and MIL per emissions module, "
              "the emissions fault codes (stored / pending / permanent) in Pxxxx form, the standard live values "
              "the modules support, the VIN and calibration ids. Over CAN (0x7DF) or K-line (0x33). Read-only.",
              {"type": "object", "required": ["interface"], "properties": {
                  "interface": {"type": "string", "description": "Interface label (or a unique part of it) from diag_interfaces."},
                  "transport": {"type": "string", "description": "can (default) or kline."},
                  "can_rate": {"type": "integer", "description": "CAN bit rate (default 500000)."}}},
              read_only=True, title="Diag: OBD-II")
    def diag_obd(ctx, interface, transport="can", can_rate=500000):
        from . import link as L
        from . import obd as O
        it = _iface(interface)
        log: List[str] = []
        t, owners = L.open_obd(it, transport or "can", log.append, can_rate=int(can_rate or 500000))
        sess = O.ObdSession(t, log.append)
        try:
            sess.connect()
            snap = sess.snapshot()
        except Exception as ex:
            raise ToolError("OBD-II read on %s failed: %s" % (it.label, ex))
        finally:
            try:
                sess.close()
            finally:
                for o in owners:
                    try:
                        o.close()
                    except Exception:
                        pass
        d = snap.to_dict()
        d["pids_named"] = {m: {("%02X %s" % (p, O.PIDS[p].name) if p in O.PIDS else "%02X" % p): O.format_value(p, v)
                               for p, v in vals.items()} for m, vals in snap.pids.items()}
        d["log"] = log[-10:]
        return d


def _iface(label: str):
    from . import link as L
    want = (label or "").strip().lower()
    found = [i for i in L.interfaces() if want and want in i.label.lower()]
    if not found:
        raise ToolError("no interface matching %r is plugged in; diag_interfaces lists them" % label)
    if len(found) > 1 and not any(i.label.lower() == want for i in found):
        raise ToolError("%r matches several interfaces: %s" % (label, ", ".join(i.label for i in found)))
    return next((i for i in found if i.label.lower() == want), found[0])


__all__ = ["register"]
