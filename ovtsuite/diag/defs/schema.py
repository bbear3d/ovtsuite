# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Field checks for every document kind of the definitions format v1.

Hand-written rather than JSON Schema: no new dependency in the frozen app,
and messages that say which entry and which field is wrong in the words a
contributor needs ("coding.byte1.bit3: 'bit' must be 0..7"). The JSON Schema
files published with the definitions repository are generated from these
tables (``ovtsuite diag-defs schema``) so the two cannot drift.

A check returns messages; an empty list is a valid document. Unknown top-level
and entry keys are warnings (``?`` prefix) so a newer pack still loads in an
older app; wrong types and missing required fields are errors.
"""
from typing import Any, Dict, List, Optional

from .model import KINDS, is_entry_id

# ---------------------------------------------------------------- field types
# A type is a string the checker understands. "int" accepts an int or a hex
# string ("0x7E0"); "num" an int or float; "str"; "bool"; "list"; "dict";
# "id" an entry id; "addr" two hex digits; "any".

COMMON_ENTRY = {"source": "str", "verified": "list", "confidence": "str", "contributor": "str",
                "note": "str", "explain": "str"}
SOURCES = ("binary", "odx", "capture", "bench", "community", "vendor-doc", "convention")
CONFIDENCE = ("verified", "likely", "unverified")
PRECONDITIONS = ("engine_off", "engine_running", "ignition_on", "vehicle_stationary",
                 "coolant_above_c", "no_faults_in")

SPECS: Dict[str, Dict[str, Any]] = {
    "addressing": {
        "top": {"generations": "dict", "modules": "list"},
        "required": ("modules",),
        "entry_key": "modules",
        "entry": {"addr": "addr", "name": "str", "kline": "dict", "tp20": "dict", "isotp": "dict",
                  "generations": "list", "platform": "str", "kwp_header": "int", "bus": "str",
                  "gateway": "dict"},
        "entry_required": ("addr", "name"),
    },
    "module": {
        # ``faults`` says how the module's KWP2000 fault words are to be read
        # (``faults.encoding``: see FAULT_ENCODINGS); a KW1281 module's words
        # are always VAG numbers, so the key is meaningless there.
        "top": {"identification": "dict", "variants": "dict", "security": "dict", "name": "str",
                "platform": "str", "kwp": "dict", "uds": "dict", "faults": "dict", "source": "str",
                "note": "str", "verified": "list", "confidence": "str"},
        "required": (),
    },
    "faults": {
        "top": {"codes": "list", "status_bytes": "dict", "freeze_frame": "dict"},
        "required": ("codes",),
        "entry_key": "codes",
        "entry": {"id": "id", "vag": "int", "sae": "str", "text": "str", "live_group": "int",
                  "symptoms": "dict"},
        "entry_required": ("id", "text"),
    },
    "measuring": {
        "top": {"groups": "dict"},
        "required": ("groups",),
    },
    "coding": {
        "top": {"style": "str", "digits": "list", "values": "list", "bytes": "list",
                "requires": "str", "length": "int"},
        "required": ("style",),
    },
    "adaptation": {
        "top": {"channels": "list"},
        "required": ("channels",),
        "entry_key": "channels",
        "entry": {"id": "id", "channel": "int", "name": "str", "unit": "str", "scale": "dict",
                  "range": "list", "writable": "bool", "requires": "str", "group": "str",
                  "options": "dict"},
        "entry_required": ("id", "channel", "name"),
    },
    "actuator_tests": {
        "top": {"tests": "list"},
        "required": ("tests",),
        "entry_key": "tests",
        "entry": {"id": "id", "tester_id": "int", "signal": "str", "name": "str",
                  "precondition": "any", "sequence": "int"},
        "entry_required": ("id", "name"),
    },
    "basic_settings": {
        "top": {"groups": "list"},
        "required": ("groups",),
        "entry_key": "groups",
        "entry": {"id": "id", "group": "int", "name": "str", "precondition": "any",
                  "success": "str", "requires": "str"},
        "entry_required": ("id", "group", "name"),
    },
    "dids": {
        "top": {"dids": "list", "routines": "list"},
        "required": (),
        "entry_key": "dids",
        "entry": {"id": "id", "did": "int", "name": "str", "type": "str", "length": "int",
                  "scale": "dict", "live": "bool", "coding": "bool", "adaptation": "bool",
                  "writable": "bool", "requires": "str", "unit": "str"},
        "entry_required": ("id", "did", "name"),
    },
    "routines": {
        "top": {"routines": "list"},
        "required": ("routines",),
        "entry_key": "routines",
        "entry": {"id": "id", "routine": "int", "name": "str", "params": "list", "requires": "str"},
        "entry_required": ("id", "routine", "name"),
    },
    "procedure": {
        "top": {"id": "id", "name": "str", "applies": "dict", "safety": "str", "steps": "list",
                "plugin": "str", "description": "str", "category": "str"},
        "required": ("id", "name", "steps"),
    },
    "translations": {
        "top": {"lang": "str", "entries": "dict"},
        "required": ("lang", "entries"),
    },
}

#: ``module.faults.encoding``: how a KWP2000 module encodes the 16-bit fault
#: word it reports. ``vag`` -- the VAG 5-digit number (01044 = 0x0414; a
#: P-code's number is 0x4000 + d*1024 + xyz, so 16683 = P0299): every KW1281
#: module, the C6 body modules, EDC15, the P350/P379/P213/P216 EDC16
#: calibrations. ``sae`` -- the SAE J2012 word itself (0x0299 = P0299, 0x4014
#: = C0014): the P447 / P475 / late P529 EDC16 calibrations, most UDS-era
#: engines. Absent: the app falls back to what it knows of the controller
#: family (``ovtsuite.dtc_report``), and finally to ``vag``. The word's
#: magnitude never decides -- VAG 01044 and SAE P0414 are the same word.
FAULT_ENCODINGS = ("vag", "sae")

VARIANT_KEYS = {
    "adaptation": ("routine_b8", "local_id", "kw1281", "uds_did"),
    "actuator_tests": ("routine_b8", "local_id", "kw1281", "uds_io_control"),
    "basic_settings": ("routine_b8", "local_id", "kw1281"),
    "coding": ("record_9a", "kw1281", "uds_0600"),
    "login": ("record_9a_bit23", "kw1281", "none"),
    "security_access": ("seed_plus_code", "sa2", "uds_seed_key", "none"),
    "identification": ("kwp_1a9b", "kw1281_blocks", "kwp_dids", "uds_f1xx"),
    "installation_list": ("did_04a1", "none"),
}


def _is_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        try:
            int(s, 0)
            return True
        except ValueError:
            return False
    return False


def as_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    """An int field as the format allows it: an int, or a hex/decimal string."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip(), 0)
        except ValueError:
            return default
    return default


def _type_ok(t: str, v: Any) -> bool:
    if t == "any":
        return True
    if t == "int":
        return _is_int(v)
    if t == "num":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "str":
        return isinstance(v, str)
    if t == "bool":
        return isinstance(v, bool)
    if t == "list":
        return isinstance(v, list)
    if t == "dict":
        return isinstance(v, dict)
    if t == "id":
        return is_entry_id(v)
    if t == "addr":
        return isinstance(v, (str, int)) and not isinstance(v, bool)
    return True


def check_fields(obj: Dict[str, Any], spec: Dict[str, str], required, where: str,
                 extra_allowed=COMMON_ENTRY) -> List[str]:
    out = []
    allowed = dict(spec)
    allowed.update(extra_allowed)
    for k in required:
        if k not in obj:
            out.append("%s: missing '%s'" % (where, k))
    for k, v in obj.items():
        if k in ("kind", "match", "format"):
            continue
        t = allowed.get(k)
        if t is None:
            out.append("?%s: unknown field '%s'" % (where, k))
            continue
        if not _type_ok(t, v):
            out.append("%s: '%s' must be %s" % (where, k, _describe(t)))
    src = obj.get("source")
    if src is not None and src not in SOURCES:
        out.append("%s: source %r is not one of %s" % (where, src, ", ".join(SOURCES)))
    conf = obj.get("confidence")
    if conf is not None and conf not in CONFIDENCE:
        out.append("%s: confidence %r is not one of %s" % (where, conf, ", ".join(CONFIDENCE)))
    return out


def _describe(t: str) -> str:
    return {"int": "an integer (or a hex string like 0x7E0)", "num": "a number", "str": "text",
            "bool": "true/false", "list": "a list", "dict": "a mapping",
            "id": "a dotted lower-case entry id (adaptation.ch02)",
            "addr": "a module address (two hex digits)"}.get(t, t)


def check_doc(body: Dict[str, Any], where: str = "") -> List[str]:
    """Messages for one document (``kind`` + ``match`` + the kind's fields).
    Errors have no prefix; warnings start with ``?``."""
    out: List[str] = []
    if not isinstance(body, dict):
        return ["%s: a document is a mapping" % where]
    kind = body.get("kind")
    if kind not in KINDS:
        return ["%s: kind %r is not one of %s" % (where, kind, ", ".join(KINDS))]
    spec = SPECS[kind]
    where = where or kind
    if "match" in body and not isinstance(body["match"], dict):
        out.append("%s: 'match' must be a mapping" % where)
    out += check_fields(body, spec["top"], spec["required"], where)
    ek = spec.get("entry_key")
    if ek and isinstance(body.get(ek), list):
        seen = set()
        for i, e in enumerate(body[ek]):
            ew = "%s[%d]" % (ek, i)
            if not isinstance(e, dict):
                out.append("%s: %s must be a mapping" % (where, ew))
                continue
            eid = e.get("id")
            if isinstance(eid, str):
                ew = eid
                if eid in seen:
                    out.append("%s: duplicate entry id %r" % (where, eid))
                seen.add(eid)
            out += check_fields(e, spec["entry"], spec["entry_required"], "%s: %s" % (where, ew))
    out += _KIND_CHECKS.get(kind, lambda b, w: [])(body, where)
    return out


# ------------------------------------------------------------- per-kind rules
def _check_addressing(body, where):
    out = []
    for m in body.get("modules", []):
        if not isinstance(m, dict):
            continue
        a = m.get("addr")
        for key, fields in (("kline", ("address", "init", "keybytes_kw1281")),
                            ("tp20", ("dest", "setup_id", "listen_id")),
                            ("isotp", ("request", "response", "functional"))):
            sub = m.get(key)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                out.append("%s: module %s: '%s' must be a mapping" % (where, a, key))
                continue
            for k, v in sub.items():
                if k == "init":
                    if v not in ("slow", "fast"):
                        out.append("%s: module %s: kline.init must be slow or fast" % (where, a))
                elif k == "keybytes_kw1281":
                    if not isinstance(v, list):
                        out.append("%s: module %s: kline.keybytes_kw1281 must be a list" % (where, a))
                elif k in fields:
                    if not _is_int(v):
                        out.append("%s: module %s: %s.%s must be an integer" % (where, a, key, k))
                else:
                    out.append("?%s: module %s: unknown field %s.%s" % (where, a, key, k))
        gw = m.get("gateway")
        if gw is not None:
            if not isinstance(gw, dict):
                out.append("%s: module %s: 'gateway' must be a mapping" % (where, a))
            else:
                v = gw.get("installation_list")
                if v is not None and v not in VARIANT_KEYS["installation_list"]:
                    out.append("%s: module %s: gateway.installation_list %r is not one of %s"
                               % (where, a, v, ", ".join(VARIANT_KEYS["installation_list"])))
                for k in gw:
                    if k not in ("installation_list", "installation_list_did"):
                        out.append("?%s: module %s: unknown field gateway.%s" % (where, a, k))
    gens = body.get("generations") or {}
    for g, info in gens.items():
        if not isinstance(info, dict):
            out.append("%s: generation %s must be a mapping" % (where, g))
    return out


def _check_module(body, where):
    out = []
    ident = body.get("identification") or {}
    rec = ident.get("record")
    if rec is not None and rec not in VARIANT_KEYS["identification"]:
        out.append("%s: identification.record %r is not one of %s"
                   % (where, rec, ", ".join(VARIANT_KEYS["identification"])))
    for k, v in (body.get("variants") or {}).items():
        if k == "link_speed":
            if not isinstance(v, list) or not all(_is_int(x) for x in v):
                out.append("%s: variants.link_speed must be a list of speed bytes" % where)
            continue
        allowed = VARIANT_KEYS.get(k)
        if allowed is None:
            out.append("?%s: unknown variant '%s'" % (where, k))
        elif v not in allowed:
            out.append("%s: variants.%s %r is not one of %s" % (where, k, v, ", ".join(allowed)))
    sec = body.get("security") or {}
    for k in ("functions_needing_security", "functions_needing_login"):
        if k in sec and not isinstance(sec[k], list):
            out.append("%s: security.%s must be a list" % (where, k))
    for i, c in enumerate(sec.get("known_codes") or []):
        if not isinstance(c, dict) or not _is_int(c.get("code")):
            out.append("%s: security.known_codes[%d] needs an integer 'code'" % (where, i))
    flt = body.get("faults")
    if isinstance(flt, dict):
        enc = flt.get("encoding")
        if enc is not None and enc not in FAULT_ENCODINGS:
            out.append("%s: faults.encoding %r is not one of %s" % (where, enc, ", ".join(FAULT_ENCODINGS)))
        for k in flt:
            if k != "encoding":
                out.append("?%s: unknown field faults.%s" % (where, k))
    return out


def _check_measuring(body, where):
    out = []
    for g, info in (body.get("groups") or {}).items():
        if not _is_int(g):
            out.append("%s: group key %r must be a number" % (where, g))
        if not isinstance(info, dict):
            out.append("%s: group %s must be a mapping" % (where, g))
            continue
        cells = info.get("cells")
        if not isinstance(cells, list):
            out.append("%s: group %s: 'cells' must be a list" % (where, g))
            continue
        for i, c in enumerate(cells):
            if c is None or isinstance(c, str):
                continue
            if isinstance(c, dict):
                if "name" not in c:
                    out.append("%s: group %s cell %d: needs 'name'" % (where, g, i + 1))
            else:
                out.append("%s: group %s cell %d: a name or a mapping" % (where, g, i + 1))
    return out


def _check_coding(body, where):
    out = []
    style = body.get("style")
    if style not in ("short", "long"):
        out.append("%s: style must be short or long" % where)
    if style == "long" and "bytes" not in body:
        out.append("%s: long coding needs 'bytes'" % where)
    if style == "short" and "digits" not in body and "values" not in body:
        out.append("%s: short coding needs 'digits' and/or 'values'" % where)
    for i, d in enumerate(body.get("digits") or []):
        if not isinstance(d, dict) or not _is_int(d.get("position")) or "name" not in d:
            out.append("%s: digits[%d] needs 'position' and 'name'" % (where, i))
        elif "options" in d and not isinstance(d["options"], dict):
            out.append("%s: digits[%d]: 'options' must be a mapping" % (where, i))
    seen = set()
    for i, v in enumerate(body.get("values") or []):
        if not isinstance(v, dict) or "value" not in v or "name" not in v:
            out.append("%s: values[%d] needs 'value' and 'name'" % (where, i))
            continue
        vid = v.get("id")
        if vid is not None and not is_entry_id(vid):
            out.append("%s: values[%d]: id %r is not a dotted lower-case id" % (where, i, vid))
        if vid in seen:
            out.append("%s: duplicate entry id %r" % (where, vid))
        seen.add(vid)
    for i, b in enumerate(body.get("bytes") or []):
        if not isinstance(b, dict) or not _is_int(b.get("index")):
            out.append("%s: bytes[%d] needs 'index'" % (where, i))
            continue
        for j, bit in enumerate(b.get("bits") or []):
            bw = "%s: byte %s bit[%d]" % (where, b.get("index"), j)
            if not isinstance(bit, dict):
                out.append(bw + ": must be a mapping")
                continue
            if not is_entry_id(bit.get("id")):
                out.append(bw + ": needs a dotted lower-case 'id'")
            elif bit["id"] in seen:
                out.append("%s: duplicate entry id %r" % (where, bit["id"]))
            seen.add(bit.get("id"))
            if "bit" in bit:
                n = as_int(bit["bit"])
                if n is None or not 0 <= n <= 7:
                    out.append(bw + ": 'bit' must be 0..7")
            elif "bits" in bit:
                bs = bit["bits"]
                if (not isinstance(bs, list) or not bs
                        or any(as_int(x) is None or not 0 <= as_int(x) <= 7 for x in bs)):
                    out.append(bw + ": 'bits' must list bit numbers 0..7")
            else:
                out.append(bw + ": needs 'bit' or 'bits'")
            t = bit.get("type")
            if t is not None and t not in ("bool", "enum", "int"):
                out.append(bw + ": type must be bool, enum or int")
            if t == "enum" and not isinstance(bit.get("options"), dict):
                out.append(bw + ": an enum needs 'options'")
    return out


def _check_preconditions(pre, where):
    out = []
    if pre is None:
        return out
    items = pre if isinstance(pre, list) else [pre]
    for p in items:
        if isinstance(p, str):
            if p not in PRECONDITIONS:
                out.append("%s: precondition %r is not one of %s" % (where, p, ", ".join(PRECONDITIONS)))
        elif isinstance(p, dict):
            for k in p:
                if k not in PRECONDITIONS:
                    out.append("%s: precondition %r is not one of %s" % (where, k, ", ".join(PRECONDITIONS)))
        else:
            out.append("%s: a precondition is a name or a mapping" % where)
    return out


def _check_actuator_tests(body, where):
    out = []
    for t in body.get("tests") or []:
        if isinstance(t, dict):
            out += _check_preconditions(t.get("precondition"), "%s: %s" % (where, t.get("id")))
    return out


def _check_basic_settings(body, where):
    out = []
    for g in body.get("groups") or []:
        if isinstance(g, dict):
            out += _check_preconditions(g.get("precondition"), "%s: %s" % (where, g.get("id")))
    return out


SCALE_KINDS = ("linear", "table", "bitfield", "ascii", "bcd")
DID_TYPES = ("u8", "u16", "u32", "s8", "s16", "s32", "ascii", "bytes", "bcd")


def _check_dids(body, where):
    out = []
    for d in body.get("dids") or []:
        if not isinstance(d, dict):
            continue
        dw = "%s: %s" % (where, d.get("id"))
        t = d.get("type")
        if t is not None and t not in DID_TYPES:
            out.append("%s: type %r is not one of %s" % (dw, t, ", ".join(DID_TYPES)))
        sc = d.get("scale")
        if isinstance(sc, dict):
            k = sc.get("kind", "linear")
            if k not in SCALE_KINDS:
                out.append("%s: scale.kind %r is not one of %s" % (dw, k, ", ".join(SCALE_KINDS)))
            if k == "table" and not isinstance(sc.get("table"), dict):
                out.append("%s: a table scale needs 'table'" % dw)
    return out


STEP_KINDS = ("read", "choose", "write", "live", "check", "show", "wait", "log")


def _check_procedure(body, where):
    out = []
    s = body.get("safety")
    if s is not None and s not in ("read-only", "write", "actuates"):
        out.append("%s: safety must be read-only, write or actuates" % where)
    for i, st in enumerate(body.get("steps") or []):
        if not isinstance(st, dict) or len(st) != 1:
            out.append("%s: steps[%d] is one mapping with one step kind" % (where, i))
            continue
        k = next(iter(st))
        if k not in STEP_KINDS:
            out.append("%s: steps[%d]: %r is not one of %s" % (where, i, k, ", ".join(STEP_KINDS)))
        elif k == "check":
            out += _check_preconditions(st[k], "%s: steps[%d]" % (where, i))
    return out


def _check_translations(body, where):
    out = []
    for k, v in (body.get("entries") or {}).items():
        if not isinstance(v, str):
            out.append("%s: %s must be text" % (where, k))
        if not isinstance(k, str) or "." not in k:
            out.append("%s: key %r is not an entry id plus field (adaptation.ch02.name)" % (where, k))
    return out


_KIND_CHECKS = {
    "addressing": _check_addressing,
    "module": _check_module,
    "measuring": _check_measuring,
    "coding": _check_coding,
    "actuator_tests": _check_actuator_tests,
    "basic_settings": _check_basic_settings,
    "dids": _check_dids,
    "procedure": _check_procedure,
    "translations": _check_translations,
}


def json_schema(kind: str) -> Dict[str, Any]:
    """A JSON Schema (draft 2020-12, the subset these tables express) for
    ``kind`` -- what the public definitions repository publishes."""
    spec = SPECS[kind]
    types = {"int": {"type": ["integer", "string"]}, "num": {"type": "number"},
             "str": {"type": "string"}, "bool": {"type": "boolean"}, "list": {"type": "array"},
             "dict": {"type": "object"}, "id": {"type": "string", "pattern": r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$"},
             "addr": {"type": ["string", "integer"]}, "any": {}}
    props = {"kind": {"const": kind}, "match": {"type": "object"}}
    for k, t in spec["top"].items():
        props[k] = dict(types[t])
    out = {"$schema": "https://json-schema.org/draft/2020-12/schema",
           "title": "OVT Diag definitions v%d: %s" % (1, kind), "type": "object",
           "properties": props, "required": ["kind"] + list(spec["required"])}
    ek = spec.get("entry_key")
    if ek:
        eprops = {k: dict(types[t]) for k, t in spec["entry"].items()}
        for k, t in COMMON_ENTRY.items():
            eprops[k] = dict(types[t])
        out["properties"][ek] = {"type": "array", "items": {
            "type": "object", "properties": eprops, "required": list(spec["entry_required"])}}
    return out
