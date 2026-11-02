# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Import an ODX into OVT Diag definition documents.

Two ODX flavours appear around VAG UDS modules:

* **ODX-D** (a diagnostic layer: ``DIAG-LAYER-CONTAINER`` with
  ``ECU-VARIANT`` / ``BASE-VARIANT``) carries the real diagnostic
  description -- ``DIAG-SERVICE`` entries, their ``DATA-OBJECT-PROP`` with a
  ``DIAG-CODED-TYPE`` (base type + bit length) and a ``COMPU-METHOD``
  (identity / linear / text table), and ``DTC-DOP`` fault tables. This is
  what fills ``dids`` (live values, long coding), ``routines`` and the fault
  texts for a module.
* **ODX-F** (a ``FLASH`` container -- the ``.frf``/``.sgo`` the flasher
  reads) carries the flash job, the expected identification values and the
  block security methods. There is little diagnostic content, but the
  identification idents and the routine ids are worth importing.

The importer reads whichever it is given and returns a list of format-v1
documents (``dids`` / ``routines`` / ``faults`` / ``module``) to write into
a pack. It degrades: a COMPU-METHOD shape it does not model is kept raw
(type ``bytes`` / no scale), never dropped, and a DTC with no P-code text is
kept by its ISO code. Nothing here talks to a car.

Qt-free; the CLI (``ovtsuite diag-defs import-odx``) calls :func:`import_odx`.
"""
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# ISO base types -> our DID type
_BASE_TYPE = {
    ("A_UINT32", 8): "u8", ("A_UINT32", 16): "u16", ("A_UINT32", 32): "u32",
    ("A_INT32", 8): "s8", ("A_INT32", 16): "s16", ("A_INT32", 32): "s32",
    ("A_ASCIISTRING", None): "ascii", ("A_UNICODE2STRING", None): "ascii",
    ("A_BYTEFIELD", None): "bytes",
}


def _text(el, path, default=""):
    if el is None:
        return default
    v = el.findtext(path)
    return v.strip() if v else default


def _slug(s: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "x"


def _did_type(dct: Optional[ET.Element]) -> Tuple[str, Optional[int]]:
    """(our type, byte length) from a DIAG-CODED-TYPE."""
    if dct is None:
        return "bytes", None
    base = dct.get("BASE-DATA-TYPE") or _text(dct, "./BASE-DATA-TYPE")
    bitlen = dct.findtext("./BIT-LENGTH")
    nbits = int(bitlen) if bitlen and bitlen.isdigit() else None
    if base in ("A_ASCIISTRING", "A_UNICODE2STRING"):
        n = dct.findtext("./MAX-LENGTH") or dct.findtext("./MIN-LENGTH")
        return "ascii", (int(n) if n and n.isdigit() else None)
    if base == "A_BYTEFIELD":
        n = dct.findtext("./MAX-LENGTH")
        return "bytes", (int(n) if n and n.isdigit() else None)
    t = _BASE_TYPE.get((base, nbits))
    if t:
        return t, (nbits // 8 if nbits else None)
    if nbits:
        signed = base == "A_INT32"
        width = max(1, (nbits + 7) // 8)
        return ("s%d" % (width * 8) if signed else "u%d" % (width * 8)), width
    return "bytes", None


def _compu_scale(cm: Optional[ET.Element], unit: str) -> Optional[Dict[str, Any]]:
    """A `scale` dict from a COMPU-METHOD, or None (identity)."""
    if cm is None:
        return None
    cat = cm.findtext("./CATEGORY") or ""
    if cat in ("IDENTICAL", ""):
        return {"unit": unit} if unit else None
    if cat in ("LINEAR", "SCALE-LINEAR"):
        coeffs = cm.find(".//COMPU-RATIONAL-COEFFS")
        num = [float(n.text) for n in coeffs.findall("./COMPU-NUMERATOR/V")] if coeffs is not None else []
        den = [float(n.text) for n in coeffs.findall("./COMPU-DENOMINATOR/V")] if coeffs is not None else [1.0]
        offset = num[0] if num else 0.0
        factor = (num[1] if len(num) > 1 else 1.0) / (den[0] if den and den[0] else 1.0)
        return {"kind": "linear", "factor": round(factor, 10), "offset": round(offset, 10), "unit": unit}
    if cat == "TEXTTABLE":
        table: Dict[str, str] = {}
        for sc in cm.findall(".//COMPU-SCALE"):
            lo = sc.findtext("./LOWER-LIMIT")
            txt = sc.findtext("./COMPU-CONST/VT") or sc.findtext("./COMPU-CONST/V") or ""
            if lo is not None:
                table[str(int(lo, 0) if lo.strip().lower().startswith("0x") else int(float(lo)))] = txt
        if table:
            return {"kind": "table", "table": table}
    return None


class OdxImport:
    """What was imported: the docs plus a human summary."""

    def __init__(self):
        self.docs: List[Dict[str, Any]] = []
        self.kind = ""                 # odx-d | odx-f
        self.ecu_name = ""
        self.counts: Dict[str, int] = {}
        self.notes: List[str] = []

    def summary(self) -> str:
        lines = ["ODX import (%s)%s:" % (self.kind, (" " + self.ecu_name) if self.ecu_name else "")]
        for k, n in self.counts.items():
            lines.append("  %d %s" % (n, k))
        lines += ["  " + n for n in self.notes]
        return "\n".join(lines)


def import_odx(data: bytes, match: Optional[Dict[str, Any]] = None, name: str = "") -> OdxImport:
    """Parse one ODX document into definition documents. ``match`` is the
    ``match`` block stamped onto every doc (e.g. ``{addr: "02", protocol:
    uds, part: "8K0927158*"}``); when omitted it is left empty for the user
    to fill. Raises ``ValueError`` on XML that is not ODX."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as ex:
        raise ValueError("not valid XML: %s" % ex)
    tag = root.tag.split("}")[-1]
    if tag != "ODX" and root.find(".//DIAG-LAYER-CONTAINER") is None and root.find("./FLASH") is None:
        raise ValueError("not an ODX document (root <%s>)" % tag)
    out = OdxImport()
    m = dict(match or {})
    if root.find(".//DIAG-LAYER-CONTAINER") is not None or root.find(".//DIAG-SERVICE") is not None:
        _import_odx_d(root, m, out)
    if root.find("./FLASH") is not None:
        _import_odx_f(root, m, out)
    if not out.kind:
        raise ValueError("no DIAG-LAYER or FLASH content found")
    return out


# ------------------------------------------------------------------- ODX-D
def _import_odx_d(root: ET.Element, match: Dict[str, Any], out: OdxImport):
    out.kind = "odx-d"
    layer = root.find(".//ECU-VARIANT")
    if layer is None:
        layer = root.find(".//BASE-VARIANT")
    if layer is None:
        layer = root.find(".//DIAG-LAYER")
    out.ecu_name = _text(layer, "./SHORT-NAME") if layer is not None else ""
    dops = _index(root, ".//DATA-OBJECT-PROP")
    dtc_dops = root.findall(".//DTC-DOP")
    dids: List[Dict[str, Any]] = []
    routines: List[Dict[str, Any]] = []
    for svc in root.findall(".//DIAG-SERVICE"):
        sid, params = _service_request(svc, root)
        if sid == 0x22:
            did = _const_param(params, after=0x22)
            if did is None:
                continue
            ref = _first_pos_response_dop(svc, root)
            entry = _did_entry(did, svc, dops.get(ref) if ref else None)
            dids.append(entry)
        elif sid == 0x31:
            rid = _const_param(params, after=0x31, skip=1)     # 31 <sub> <rid:2>
            if rid is not None:
                routines.append({"id": "routine.%04x" % rid, "routine": rid,
                                 "name": _text(svc, "./LONG-NAME") or _text(svc, "./SHORT-NAME"),
                                 "params": [], "requires": ""})
    faults = _import_dtcs(dtc_dops)
    if dids:
        out.docs.append({"kind": "dids", "match": dict(match), "source": "odx", "dids": dids,
                         **({"routines": routines} if routines else {})})
        out.counts["DIDs"] = len(dids)
    elif routines:
        out.docs.append({"kind": "routines", "match": dict(match), "source": "odx", "routines": routines})
    if routines:
        out.counts["routines"] = len(routines)
    if faults:
        out.docs.append({"kind": "faults", "match": dict(match), "source": "odx", "codes": faults})
        out.counts["fault texts"] = len(faults)
    if not (dids or routines or faults):
        out.notes.append("no DIDs, routines or DTCs recognised in the diagnostic layer")


def _index(root, path) -> Dict[str, ET.Element]:
    return {el.get("ID"): el for el in root.findall(path) if el.get("ID")}


def _service_request(svc, root) -> Tuple[Optional[int], List[ET.Element]]:
    ref = svc.find("./REQUEST-REF")
    req = None
    if ref is not None:
        req = root.find(".//REQUEST[@ID='%s']" % ref.get("ID-REF"))
    if req is None:
        req = svc.find(".//REQUEST")
    if req is None:
        return None, []
    params = req.findall(".//PARAM")
    sid = None
    for p in params:
        if p.get("SEMANTIC") == "SERVICE-ID" or _text(p, "./SHORT-NAME").upper().startswith("SID"):
            v = p.findtext("./CODED-VALUE")
            if v:
                sid = int(v, 0) if v.strip().lower().startswith("0x") else int(v)
                break
    if sid is None and params:
        v = params[0].findtext("./CODED-VALUE")
        if v:
            sid = int(v) if v.isdigit() else (int(v, 0) if v.lower().startswith("0x") else None)
    return sid, params


def _const_param(params: List[ET.Element], after: int, skip: int = 0) -> Optional[int]:
    """The constant value of the parameter after the SID (optionally skipping
    ``skip`` more constant params) -- the DID or routine id."""
    consts = []
    for p in params:
        v = p.findtext("./CODED-VALUE")
        if v is not None:
            consts.append(int(v, 0) if v.strip().lower().startswith("0x") else int(v))
    # consts[0] is the SID; consts[1+skip] is the identifier
    idx = 1 + skip
    if len(consts) > idx and consts[0] == after:
        return consts[idx]
    # some ODX fold the DID into one 2-byte const after the SID
    if len(consts) > 1 and consts[0] == after and consts[1] > 0xFF:
        return consts[1]
    return None


def _first_pos_response_dop(svc, root) -> Optional[str]:
    ref = svc.find("./POS-RESPONSE-REFS/POS-RESPONSE-REF")
    if ref is None:
        ref = svc.find(".//POS-RESPONSE-REF")
    resp = root.find(".//POS-RESPONSE[@ID='%s']" % ref.get("ID-REF")) if ref is not None else None
    if resp is None:
        resp = svc.find(".//POS-RESPONSE")
    if resp is None:
        return None
    for p in resp.findall(".//PARAM"):
        dref = p.find("./DOP-REF")
        if dref is not None and p.get("SEMANTIC") in (None, "DATA", "VALUE"):
            return dref.get("ID-REF")
    return None


def _did_entry(did: int, svc, dop: Optional[ET.Element]) -> Dict[str, Any]:
    name = _text(svc, "./LONG-NAME") or _text(svc, "./SHORT-NAME") or ("DID 0x%04X" % did)
    entry: Dict[str, Any] = {"id": "did.%04x" % did, "did": did, "name": name}
    if dop is not None:
        t, length = _did_type(dop.find("./DIAG-CODED-TYPE"))
        entry["type"] = t
        if length:
            entry["length"] = length
        unit = _text(dop, ".//UNIT/DISPLAY-NAME") or _text(dop, ".//UNIT-REF")
        scale = _compu_scale(dop.find("./COMPU-METHOD"), unit)
        if scale:
            if set(scale) == {"unit"}:
                entry["unit"] = scale["unit"]
            else:
                entry["scale"] = scale
        if t not in ("ascii", "bytes"):
            entry["live"] = True
    else:
        entry["type"] = "bytes"
    if did == 0x0600:
        entry["coding"] = True
        entry["live"] = False
    return entry


def _import_dtcs(dtc_dops) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for dop in dtc_dops:
        for dtc in dop.findall(".//DTC"):
            code = dtc.findtext("./TROUBLE-CODE")
            disp = dtc.findtext("./DISPLAY-TROUBLE-CODE") or ""
            text = dtc.findtext("./TEXT") or dtc.findtext("./TEXT/*") or _text(dtc, "./LONG-NAME")
            if code is None:
                continue
            num = int(code, 0) if code.strip().lower().startswith("0x") else int(code)
            if num in seen:
                continue
            seen.add(num)
            e: Dict[str, Any] = {"id": "dtc.%06x" % (num & 0xFFFFFF), "text": text or disp or "DTC %06X" % num}
            sae = _sae_from_display(disp) or _sae_from_uds(num)
            if sae:
                e["sae"] = sae
            out.append(e)
    return out


def _sae_from_display(disp: str) -> str:
    disp = (disp or "").strip().upper()
    if len(disp) == 5 and disp[0] in "PCBU" and all(c in "0123456789ABCDEF" for c in disp[1:]):
        return disp
    return ""


def _sae_from_uds(num: int) -> str:
    from .obd import decode_dtc
    hi = (num >> 8) & 0xFFFF
    try:
        return decode_dtc(bytes([(hi >> 8) & 0xFF, hi & 0xFF]))
    except Exception:
        return ""


# ------------------------------------------------------------------- ODX-F
def _import_odx_f(root: ET.Element, match: Dict[str, Any], out: OdxImport):
    out.kind = out.kind or "odx-f"
    flash = root.find("./FLASH")
    out.ecu_name = out.ecu_name or (flash.get("ID") if flash is not None else "")
    idents: Dict[str, List[str]] = {}
    for ei in root.findall(".//EXPECTED-IDENT"):
        sn = ei.findtext("./SHORT-NAME") or ""
        vals = [v.text for v in ei.findall(".//IDENT-VALUE") if v.text]
        if sn and vals:
            idents[sn] = vals
    security = []
    for sec in root.findall(".//SECURITY"):
        meth = sec.findtext("./SECURITY-METHOD")
        if meth:
            security.append(meth)
    blocks = []
    for db in root.findall(".//DATABLOCK"):
        if db.get("TYPE") == "ERASE":
            continue
        seg = db.find("./SEGMENTS/SEGMENT")
        if seg is not None:
            src = seg.findtext("./SOURCE-START-ADDRESS")
            blocks.append((db.findtext("./SHORT-NAME") or "", src))
    note = "flash container: %d block(s)%s%s" % (
        len(blocks), (", security %s" % "/".join(sorted(set(security)))) if security else "",
        (", idents %s" % ", ".join(sorted(idents))) if idents else "")
    out.notes.append(note)
    out.counts.setdefault("flash blocks", len(blocks))
    if idents or security or blocks:
        doc: Dict[str, Any] = {"kind": "module", "match": dict(match), "source": "odx",
                               "identification": {"record": "uds_f1xx"},
                               "note": "Imported from the flash ODX (%s). %s" % (out.ecu_name, note)}
        out.docs.append(doc)


__all__ = ["import_odx", "OdxImport"]
