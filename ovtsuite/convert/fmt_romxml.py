# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EcuFlash and RomRaider XML definitions (the Subaru / Mitsubishi tools),
read and written.

Measured from the community SubaruDefs repository (666 EcuFlash ROM files,
RomRaider's ``ecu_defs.xml`` with 334 ROMs, both kept from one source) and
from RomRaider's own source for what its attributes mean:

EcuFlash -- one ``<rom>`` per file: ``<romid>`` (``xmlid``,
``internalidaddress``, ``internalidstring`` ...), named ``<scaling>``s
(``storagetype``, ``endian``, ``toexpr`` / ``frexpr`` in ``x``, ``format``,
``units``) and ``<table>``s (``name``, ``type`` 1D / 2D / 3D, ``scaling``,
``address`` in hex without ``0x``, ``elements``, ``category``,
``<description>``) with child tables for axes (``X Axis``, ``Y Axis``,
``Static Y Axis`` / ``Static X Axis`` with ``<data>`` texts). A ROM file
``<include>``s a base by its ``xmlid`` and gives only addresses (and
``elements``); a child named ``X`` / ``Y`` is that axis.

RomRaider -- one ``<roms>`` file of many ``<rom base="xmlid">``; tables carry
``storageaddress`` (``0x``), ``storagetype``, ``endian``, ``sizex`` /
``sizey`` and an inline ``<scaling expression to_byte format units>``.
``Switch`` tables hold byte patterns (``<state data>``).

Both store a 3D table Y outer, X inner (RomRaider ``Table3D.populateTable``)
unless ``swapxy``; ``flipx`` / ``flipy`` reverse an axis. A float's
``endian="little"`` in a RomRaider file means big-endian unless the ROM's
``<memmodel endian>`` states the order (``RomAttributeParser.byteToFloat``:
"corrects improperly defined float table endian in legacy definition
files"); a stated memory-model order is every table's, integers too
(``TableScaleUnmarshaller``).

On/off byte patterns (EcuFlash ``bloblist``, RomRaider ``Switch``) have no
form in OVT's model and are reported, not read.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from ..definitions.model import AxisDef, Definition, DefinitionError, Storage, TableDef
from ..equation import Equation

_TYPES = {"uint8": (8, False, False), "int8": (8, True, False),
          "uint16": (16, False, False), "int16": (16, True, False),
          "uint32": (32, False, False), "int32": (32, True, False),
          "float": (32, False, True)}


def _head(data: bytes) -> str:
    return bytes(data[:4096]).lstrip(b"\xef\xbb\xbf").decode("utf-8", "replace")


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def is_ecuflash(data: bytes) -> bool:
    h = _strip_comments(_head(data)).lstrip()
    if h.startswith("<?xml"):
        h = h[h.find("?>") + 2:].lstrip()
    return h.startswith("<rom>") or (h.startswith("<rom") and "<romid>" in h and
                                     "<roms" not in h[:10])


def is_romraider(data: bytes) -> bool:
    h = _strip_comments(_head(data)).lstrip()
    if h.startswith("<?xml"):
        h = h[h.find("?>") + 2:].lstrip()
    return h.startswith("<roms")


# ------------------------------------------------------------------ helpers
def _int(s: Optional[str], hexdefault: bool = True) -> Optional[int]:
    if s is None or not str(s).strip():
        return None
    s = str(s).strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 16) if hexdefault else int(s, 10)
    except ValueError:
        return None


def _storage(stype: str, endian: str, expr: str, units: str, addr: Optional[int]) -> Optional[Storage]:
    t = _TYPES.get((stype or "").lower())
    if t is None:
        return None
    bits, signed, flt = t
    st = Storage(addr=addr, bits=bits, signed=signed, float=flt,
                 endian=("<" if endian == "little" else ">") if bits > 8 else "",
                 units=units or "")
    _conversion(st, expr)
    return st


def _conversion(st: Storage, expr: str) -> None:
    text = (expr or "x").replace("x", "X").replace(" ", "")
    try:
        eq = Equation(text)
    except ValueError:
        st.equation = text
        return
    if eq.linear:
        st.factor, st.offset, st.equation = eq.factor, eq.offset, ""
    else:
        st.equation = text


def _decimals(fmt: Optional[str]) -> Optional[int]:
    if not fmt:
        return None
    m = re.match(r"^%\d*\.(\d+)", fmt)
    if m:
        return int(m.group(1))
    m = re.match(r"^[#0,]*\.([0#]+)$", fmt)            # Java DecimalFormat
    if m:
        return len(m.group(1))
    return 0 if re.match(r"^[#0,]+$", fmt) else None


def _static(t: ET.Element) -> List[str]:
    return [(d.text or "").strip() for d in t.findall("data")]


def _order(t: ET.Element) -> Tuple[str, bool, bool]:
    def flag(k):
        return str(t.get(k, "")).lower() == "true"
    return ("col" if flag("swapxy") else "row"), flag("flipx"), flag("flipy")


# ------------------------------------------------------------------ EcuFlash
class _Flash:
    """An EcuFlash ROM with its includes resolved: scalings and tables by
    name, a ROM's own entries over its bases'."""

    def __init__(self, root: ET.Element, resolve: Callable[[str], Optional[ET.Element]],
                 depth: int = 0):
        self.root = root
        self.scalings: Dict[str, ET.Element] = {}
        self.tables: Dict[str, ET.Element] = {}
        self.order: List[str] = []
        self.missing: List[str] = []
        for inc in root.findall("include"):
            xid = (inc.text or "").strip()
            base = resolve(xid) if depth < 8 else None
            if base is None:
                self.missing.append(xid)
                continue
            b = _Flash(base, resolve, depth + 1)
            self.missing += b.missing
            self.scalings.update(b.scalings)
            for n in b.order:
                self._put(n, b.tables[n], base=True)
        for s in root.findall("scaling"):
            self.scalings[s.get("name", "")] = s
        for t in root.findall("table"):
            self._put(t.get("name", ""), t)

    def _put(self, name: str, t: ET.Element, base: bool = False) -> None:
        if name in self.tables and not base:
            self.tables[name] = _merge_flash(self.tables[name], t)
            return
        if name not in self.tables:
            self.order.append(name)
        self.tables[name] = t

    def scaling(self, name: Optional[str]) -> Optional[ET.Element]:
        return self.scalings.get(name or "")


def _merge_flash(base: ET.Element, over: ET.Element) -> ET.Element:
    """A ROM's entry for a table over its base's: attributes replace, a
    child named ``X`` / ``Y`` (or by its own name) is that axis."""
    out = ET.Element("table", dict(base.attrib))
    out.attrib.update(over.attrib)
    kids = [ET.Element("table", dict(k.attrib)) for k in base.findall("table")]
    for k, src in zip(kids, base.findall("table")):
        for d in src:
            k.append(d)
    for oc in over.findall("table"):
        n = oc.get("name", "")
        hit = None
        for k in kids:
            if k.get("name") == n or (n in ("X", "Y") and
                                      (n + " Axis") in k.get("type", "")):
                hit = k
                break
        if hit is None:
            hit = ET.Element("table", {})
            kids.append(hit)
        hit.attrib.update({a: v for a, v in oc.attrib.items() if not (a == "name" and v in ("X", "Y"))})
    for k in kids:
        out.append(k)
    for d in base.findall("description"):
        out.append(d)
    return out


def finder(path: Optional[str]) -> Callable[[str], Optional[ET.Element]]:
    """How an EcuFlash ROM's ``<include>`` is found: a file with that
    ``xmlid`` in the ROM's folder or the folder above it, and their
    sub-folders (EcuFlash's metadata folder keeps a make's ROMs in one
    folder per model and its bases in ``Bases`` beside them)."""
    cache: Dict[str, Optional[ET.Element]] = {}
    index: Dict[str, str] = {}
    built = [False]

    def build():
        built[0] = True
        if not path:
            return
        d = os.path.dirname(os.path.abspath(path))
        roots = [d, os.path.dirname(d)]
        seen = set()
        for r in roots:
            for dp, _dn, fn in os.walk(r):
                if dp.count(os.sep) - r.count(os.sep) > 3:
                    continue
                for f in fn:
                    if not f.lower().endswith(".xml"):
                        continue
                    p = os.path.join(dp, f)
                    if p in seen:
                        continue
                    seen.add(p)
                    try:
                        with open(p, "rb") as fh:
                            head = fh.read(8192).decode("utf-8", "replace")
                    except OSError:
                        continue
                    m = re.search(r"<xmlid>\s*([^<]+?)\s*</xmlid>", head)
                    if m:
                        index.setdefault(m.group(1), p)
            if len(index) > 0 and r == roots[-1]:
                break

    def resolve(xid: str) -> Optional[ET.Element]:
        if xid in cache:
            return cache[xid]
        if not built[0]:
            build()
        p = index.get(xid)
        el = None
        if p:
            try:
                el = ET.parse(p).getroot()
            except (ET.ParseError, OSError):
                el = None
        cache[xid] = el
        return el
    return resolve


def load_ecuflash(data: bytes, title: str = "", path: Optional[str] = None,
                  resolve: Optional[Callable[[str], Optional[ET.Element]]] = None) -> Definition:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DefinitionError("EcuFlash XML not readable: %s" % exc)
    rom = _Flash(root, resolve or finder(path))
    xid = (root.findtext("romid/xmlid") or "").strip()
    defn = Definition(type="ecuflash", title=title or xid or "EcuFlash")
    _romid(defn, root.find("romid"))
    if rom.missing:
        raise DefinitionError(
            "includes %s, which is not here: put %s beside it (or in its folder "
            "tree, as in EcuFlash's metadata folder)." % (
                ", ".join(rom.missing), " and ".join("%s.xml" % m for m in rom.missing)))
    blobs = 0
    for name in rom.order:
        t = rom.tables[name]
        addr = _int(t.get("address"))
        if addr is None:
            continue                      # a base entry this ROM does not have
        sc = rom.scaling(t.get("scaling"))
        if sc is not None and sc.get("storagetype") == "bloblist":
            blobs += 1
            continue
        td = _flash_table(rom, t, name, addr, defn.warnings)
        if td is not None:
            defn.tables.append(td)
    if blobs:
        defn.warnings.append("%d on/off byte-pattern tables (bloblist) have no form here "
                             "-- not read" % blobs)
    return defn


def _romid(defn: Definition, rid: Optional[ET.Element]) -> None:
    if rid is None:
        return
    for k in ("xmlid", "internalidaddress", "internalidstring", "ecuid", "make", "model",
              "submodel", "market", "year", "transmission", "memmodel", "flashmethod",
              "checksummodule", "filesize"):
        v = (rid.findtext(k) or "").strip()
        if v:
            defn.info["rom_" + k] = v


def _flash_storage(rom: _Flash, t: ET.Element, addr: Optional[int]) -> Optional[Storage]:
    sc = rom.scaling(t.get("scaling"))
    if sc is None:
        return None
    return _storage(sc.get("storagetype", ""), sc.get("endian", "big"),
                    sc.get("toexpr", "x"), sc.get("units", ""), addr)


def _flash_table(rom: _Flash, t: ET.Element, name: str, addr: int,
                 warnings: List[str]) -> Optional[TableDef]:
    z = _flash_storage(rom, t, addr)
    if z is None:
        warnings.append("%s: scaling %r is not defined -- left out" % (name, t.get("scaling")))
        return None
    sc = rom.scaling(t.get("scaling"))
    typ = t.get("type", "1D")
    axes = {}
    for k in t.findall("table"):
        kt = k.get("type", "")
        tag = "x" if kt.startswith(("X Axis", "Static X")) else "y"
        n = _int(k.get("elements"), hexdefault=False) or 0
        if kt.startswith("Static"):
            labels = _static(k)
            n = n or len(labels)
            axes[tag] = AxisDef(n, labels=labels[:n] or None, name=k.get("name", ""))
            continue
        ka = _int(k.get("address"))
        st = _flash_storage(rom, k, ka)
        axes[tag] = AxisDef(n, values=st if ka is not None else None, name=k.get("name", ""))
    order, flipx, flipy = _order(t)
    td = TableDef(name=name, kind="constant", z=z,
                  description=(t.findtext("description") or "").strip(),
                  categories=[t.get("category")] if t.get("category") else [],
                  order=order, decimals=_decimals(sc.get("format") if sc is not None else None))
    if typ == "3D":
        x, y = axes.get("x"), axes.get("y")
        if x is None or y is None or not x.count or not y.count:
            warnings.append("%s: a 3D table without both axis sizes -- left out" % name)
            return None
        td.kind, td.cols, td.rows, td.x, td.y = "map", x.count, y.count, x, y
    elif typ == "2D":
        ax = axes.get("y") or axes.get("x")
        n = (ax.count if ax else 0) or (_int(t.get("elements"), hexdefault=False) or 0)
        if not n:
            warnings.append("%s: a 2D table without a size -- left out" % name)
            return None
        td.kind, td.cols, td.x = "curve", n, ax
        if ax is not None and not ax.count:
            ax.count = n
    else:
        n = _int(t.get("elements"), hexdefault=False) or 1
        if n > 1:
            td.kind, td.cols = "curve", n
    if flipx or flipy:
        td.loss.append("its axes are stored reversed (flip) -- read as stored")
    return td


# ------------------------------------------------------------------ RomRaider
def rom_ids(data: bytes) -> List[Tuple[str, str, Optional[int], str]]:
    """Every ROM of a RomRaider file: ``(xmlid, internalidstring,
    internalidaddress, a description)``."""
    root = ET.fromstring(data)
    out = []
    for rm in root.findall("rom"):
        rid = rm.find("romid")
        if rid is None:
            continue
        desc = " ".join(v for v in ((rid.findtext(k) or "").strip() for k in
                                    ("year", "make", "model", "submodel", "transmission",
                                     "market")) if v)
        out.append(((rid.findtext("xmlid") or "").strip(),
                    (rid.findtext("internalidstring") or "").strip(),
                    _int(rid.findtext("internalidaddress")), desc))
    return out


def identify(data: bytes, image: bytes) -> Optional[str]:
    """The ROM of a RomRaider file this image is: its ``internalidstring``
    at its ``internalidaddress`` -- how RomRaider recognises a ROM."""
    hits = []
    for xid, idstr, addr, _d in rom_ids(data):
        if not idstr or addr is None:
            continue
        b = idstr.encode("latin-1", "replace")
        if image[addr:addr + len(b)] == b:
            hits.append(xid)
    return hits[0] if len(hits) == 1 else None


def default_rom(data: bytes) -> Optional[str]:
    """The ROM used when none is named and no binary is given: the only
    ROM, or the only one that places any table; else ``None`` (the ROM has
    to be named or found from a binary)."""
    return _default_rom({(rm.findtext("romid/xmlid") or "").strip(): rm
                         for rm in ET.fromstring(data).findall("rom")})


def _default_rom(roms: Dict[str, ET.Element]) -> Optional[str]:
    if len(roms) == 1:
        return next(iter(roms))
    with_tables = [k for k, rm in roms.items()
                   if any(t.get("storageaddress") for t in rm.iter("table"))]
    return with_tables[0] if len(with_tables) == 1 else None


def load_romraider(data: bytes, title: str = "", rom: Optional[str] = None,
                   image: Optional[bytes] = None) -> Definition:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DefinitionError("RomRaider XML not readable: %s" % exc)
    roms = {(rm.findtext("romid/xmlid") or "").strip(): rm for rm in root.findall("rom")}
    if rom is None and image is not None:
        rom = identify(data, image)
        if rom is None:
            raise DefinitionError(
                "none of its %d ROMs is this binary (no ROM's id string is at its id "
                "address): name the ROM to use." % len(roms))
    if rom is None:
        rom = _default_rom(roms)
        if rom is None:
            raise DefinitionError(
                "holds %d ROMs: give the binary (the ROM is found from it) or name the "
                "ROM to use." % len(roms))
    if rom not in roms:
        raise DefinitionError("has no ROM %r" % rom)
    chain = []
    cur: Optional[str] = rom
    while cur and cur in roms and cur not in chain and len(chain) < 16:
        chain.append(cur)
        cur = roms[cur].get("base")
    if cur and cur not in roms:
        raise DefinitionError("ROM %s is based on %s, which is not in the file" % (chain[-1], cur))
    tables: Dict[str, ET.Element] = {}
    order: List[str] = []
    memendian = None
    for xid in reversed(chain):
        rm = roms[xid]
        mm = rm.find("romid/memmodel")
        if mm is not None and mm.get("endian"):
            memendian = mm.get("endian").lower()
        for t in rm.findall("table"):
            n = t.get("name", "")
            if n in tables:
                tables[n] = _merge_rr(tables[n], t)
            else:
                tables[n] = t
                order.append(n)
    defn = Definition(type="romraider", title=title or rom)
    _romid(defn, roms[rom].find("romid"))
    switches = 0
    for n in order:
        t = tables[n]
        addr = _int(t.get("storageaddress"))
        if addr is None:
            continue
        if t.get("type") == "Switch":
            switches += 1
            continue
        td = _rr_table(t, n, addr, memendian, defn.warnings)
        if td is not None:
            defn.tables.append(td)
    if switches:
        defn.warnings.append("%d on/off byte-pattern tables (Switch) have no form here -- "
                             "not read" % switches)
    return defn


def _merge_rr(base: ET.Element, over: ET.Element) -> ET.Element:
    out = ET.Element("table", dict(base.attrib))
    out.attrib.update(over.attrib)
    kids = [ET.Element("table", dict(k.attrib)) for k in base.findall("table")]
    for k, src in zip(kids, base.findall("table")):
        for d in src:
            k.append(d)
    for oc in over.findall("table"):
        hit = next((k for k in kids if k.get("type") == oc.get("type") and
                    (not oc.get("name") or k.get("name") == oc.get("name")
                     or oc.get("type") in ("X Axis", "Y Axis"))), None)
        if hit is None:
            hit = ET.Element("table", {})
            kids.append(hit)
        hit.attrib.update(oc.attrib)
        for d in oc:
            hit.append(d)
    for k in kids:
        out.append(k)
    sc = over.find("scaling") if over.find("scaling") is not None else base.find("scaling")
    if sc is not None:
        out.append(sc)
    for d in (over.findall("description") or base.findall("description")):
        out.append(d)
    return out


def _rr_storage(t: ET.Element, addr: Optional[int], memendian: Optional[str]) -> Optional[Storage]:
    sc = t.find("scaling")
    stype = (t.get("storagetype") or "").lower()
    endian = (t.get("endian") or "big").lower()
    if memendian:
        # TableScaleUnmarshaller: a stated memory-model order is every
        # table's, whatever its own attribute says
        endian = memendian
    elif stype == "float":
        # RomAttributeParser.byteToFloat: legacy files state a float's
        # order the other way round
        endian = "big" if endian == "little" else "little"
    return _storage(stype, endian, sc.get("expression", "x") if sc is not None else "x",
                    sc.get("units", "") if sc is not None else "", addr)


def _rr_table(t: ET.Element, name: str, addr: int, memendian: Optional[str],
              warnings: List[str]) -> Optional[TableDef]:
    z = _rr_storage(t, addr, memendian)
    if z is None:
        warnings.append("%s: storage type %r -- left out" % (name, t.get("storagetype")))
        return None
    sc = t.find("scaling")
    typ = t.get("type", "1D")
    axes = {}
    for k in t.findall("table"):
        kt = k.get("type", "")
        tag = "x" if kt.startswith(("X Axis", "Static X")) else "y"
        if kt.startswith("Static"):
            labels = _static(k)
            axes[tag] = AxisDef(len(labels), labels=labels or None, name=k.get("name", ""))
            continue
        ka = _int(k.get("storageaddress"))
        st = _rr_storage(k, ka, memendian) if ka is not None else None
        axes[tag] = AxisDef(0, values=st, name=k.get("name", ""))
    order, flipx, flipy = _order(t)
    td = TableDef(name=name, kind="constant", z=z,
                  description=(t.findtext("description") or "").strip(),
                  categories=[t.get("category")] if t.get("category") else [],
                  order=order, decimals=_decimals(sc.get("format") if sc is not None else None))
    sx = _int(t.get("sizex"), hexdefault=False)
    sy = _int(t.get("sizey"), hexdefault=False)
    if typ == "3D":
        x, y = axes.get("x"), axes.get("y")
        if x is None or y is None or not sx or not sy:
            warnings.append("%s: a 3D table without both axis sizes -- left out" % name)
            return None
        x.count, y.count = sx, sy
        for ad, n in ((x, sx), (y, sy)):
            if ad.labels:
                ad.labels = ad.labels[:n]
        td.kind, td.cols, td.rows, td.x, td.y = "map", sx, sy, x, y
    elif typ == "2D":
        ax = axes.get("y") or axes.get("x")
        n = sy or sx or (ax.count if ax else 0)
        if not n:
            warnings.append("%s: a 2D table without a size -- left out" % name)
            return None
        if ax is not None:
            ax.count = n
            if ax.labels:
                ax.labels = ax.labels[:n]
        td.kind, td.cols, td.x = "curve", n, ax
    else:
        n = sx or sy or 1
        if n > 1:
            td.kind, td.cols = "curve", n
    if flipx or flipy:
        td.loss.append("its axes are stored reversed (flip) -- read as stored")
    return td


# ------------------------------------------------------------------ writing
def _expr(st: Storage) -> Tuple[str, Optional[str]]:
    """``(to physical, from physical)`` in ``x``; the second None when the
    conversion cannot be inverted."""
    text = st.equation_text()
    eq = Equation(text)
    to = text.replace("X", "x")
    if eq.linear and eq.factor:
        f, o = eq.factor, eq.offset
        back = "x" if (f, o) == (1, 0) else (
            "x/%r" % f if not o else "(x-(%r))/%r" % (o, f))
        return to, back
    if getattr(eq, "_mobius", None):
        a, b, d = eq._mobius
        return to, "(%r-%r*x)/(x-%r)" % (b, d, a)
    return to, None


def _stype(st: Storage) -> Optional[str]:
    if st.float:
        return "float" if st.bits == 32 else None
    if st.bits not in (8, 16, 32):
        return None
    return ("int%d" if st.signed else "uint%d") % st.bits


def _endian(st: Storage, default: str) -> str:
    e = st.endian or default
    return "little" if e == "<" else "big"


def _fmt_c(dec: Optional[int]) -> str:
    return "%%.%df" % (2 if dec is None else dec)


def _fmt_java(dec: Optional[int]) -> str:
    d = 2 if dec is None else dec
    return "0" if d == 0 else "0." + "0" * d


def _ok(t: TableDef, loss: Dict[str, List[str]]) -> bool:
    name = t.symbol or t.name
    for tag, st in (("values", t.z), ("X axis", t.x.values if t.x else None),
                    ("Y axis", t.y.values if t.y else None)):
        if st is None:
            continue
        if _stype(st) is None:
            loss.setdefault(name, []).append("%s: %d-bit %s has no storage type in the "
                                             "format -- left out" % (
                                                 tag, st.bits, "float" if st.float else "integer"))
            return False
    if t.z.addr is None:
        loss.setdefault(name, []).append("no stored values -- left out")
        return False
    return True


def dumps_ecuflash(defn: Definition, title: str = "",
                   loss: Optional[Dict[str, List[str]]] = None) -> Tuple[str, int]:
    """A self-contained EcuFlash ROM file (no includes): ``(text, tables)``."""
    loss = loss if loss is not None else {}
    le = defn.byte_order()
    scalings: Dict[tuple, str] = {}
    lines_sc: List[str] = []

    def scaling(st: Storage, dec: Optional[int], hint: str) -> str:
        to, back = _expr(st)
        key = (to, back, st.units or "", _stype(st), _endian(st, le), dec)
        if key in scalings:
            return scalings[key]
        n = re.sub(r"\s+", "", hint or "scaling")[:40] or "scaling"
        base, k = n, 1
        while n in scalings.values():
            k += 1
            n = "%s%d" % (base, k)
        scalings[key] = n
        attrs = [("name", n), ("units", st.units or ""), ("toexpr", to)]
        if back is not None:
            attrs.append(("frexpr", back))
        attrs += [("format", _fmt_c(dec)), ("storagetype", _stype(st)),
                  ("endian", _endian(st, le))]
        lines_sc.append("  <scaling %s />" % " ".join("%s=%s" % (a, quoteattr(v)) for a, v in attrs))
        return n

    tables: List[str] = []
    n_out = 0
    for t in defn.tables:
        if not _ok(t, loss):
            continue
        name = t.symbol or t.name
        if t.z.equation and _expr(t.z)[1] is None:
            loss.setdefault(name, []).append("its conversion has no inverse: EcuFlash shows "
                                              "it but cannot write it")
        typ = {"constant": "1D", "curve": "2D", "map": "3D"}[t.kind]
        if t.kind == "curve" and t.x is None:
            typ = "1D"
        attrs = [("name", t.name)]
        if t.categories:
            attrs.append(("category", t.categories[0]))
        attrs += [("type", typ), ("scaling", scaling(t.z, t.decimals, t.z.units or name)),
                  ("address", "%x" % t.z.addr)]
        if typ == "1D" and t.cols > 1:
            attrs.append(("elements", str(t.rows * t.cols)))
        if t.kind == "map" and t.order == "col":
            attrs.append(("swapxy", "true"))
        out = ["  <table %s>" % " ".join("%s=%s" % (a, quoteattr(v)) for a, v in attrs)]
        axes = []
        if typ == "2D":
            axes = [("Y Axis", t.x, t.cols)]
        elif typ == "3D":
            axes = [("X Axis", t.x, t.cols), ("Y Axis", t.y, t.rows)]
        for kind, ad, n in axes:
            label = (ad.name if ad and ad.name else kind)
            if ad is not None and ad.values is not None and ad.values.addr is not None:
                out.append("    <table name=%s type=%s elements=\"%d\" scaling=%s address=\"%x\" />" % (
                    quoteattr(label), quoteattr(kind), n,
                    quoteattr(scaling(ad.values, None, ad.values.units or label)), ad.values.addr))
            else:
                pts = list(ad.labels) if ad is not None and ad.labels else (
                    ["%g" % v for v in ad.fixed] if ad is not None and ad.fixed else [])
                out.append("    <table name=%s type=%s elements=\"%d\">" % (
                    quoteattr(label), quoteattr("Static " + kind), n))
                out += ["      <data>%s</data>" % escape(str(p)) for p in pts[:n]]
                out.append("    </table>")
        if t.description:
            out.append("    <description>%s</description>" % escape(t.description))
        out.append("  </table>")
        tables += out
        n_out += 1
    head = ['<?xml version="1.0" encoding="utf-8"?>',
            "<!-- written by OVT Converter -->", "<rom>", "  <romid>",
            "    <xmlid>%s</xmlid>" % escape(defn.info.get("rom_xmlid") or title or defn.title or "ovt")]
    for k in ("internalidaddress", "internalidstring", "ecuid", "make", "model", "market",
              "year", "transmission", "memmodel", "flashmethod", "checksummodule"):
        v = defn.info.get("rom_" + k)
        if v:
            head.append("    <%s>%s</%s>" % (k, escape(v), k))
    head.append("  </romid>")
    return "\n".join(head + lines_sc + tables + ["</rom>", ""]), n_out


def dumps_romraider(defn: Definition, title: str = "",
                    loss: Optional[Dict[str, List[str]]] = None) -> Tuple[str, int]:
    """A RomRaider ``<roms>`` file with one ROM: ``(text, tables)``. Its byte
    order is stated in ``<memmodel endian>`` when every table shares one;
    otherwise each table states its own, a float's the legacy way round."""
    loss = loss if loss is not None else {}
    le = defn.byte_order()
    orders = {st.endian or le for t in defn.tables
              for st in (t.z, t.x.values if t.x else None, t.y.values if t.y else None)
              if st is not None and st.bits > 8}
    # one byte order for everything: state it once (RomRaider then applies it
    # to every table); else each table's own, a float's the legacy way round
    one = orders.pop() if len(orders) == 1 else (le if not orders else None)

    def rr_endian(st: Storage) -> str:
        e = _endian(st, le)
        if st.float:
            return "big" if e == "little" else "little"
        return e

    def sc_xml(st: Storage, dec: Optional[int], indent: str) -> str:
        to, back = _expr(st)
        attrs = [("units", st.units or ""), ("expression", to)]
        if back is not None:
            attrs.append(("to_byte", back))
        attrs += [("format", _fmt_java(dec)), ("fineincrement", "1"), ("coarseincrement", "1")]
        return "%s<scaling %s />" % (indent, " ".join("%s=%s" % (a, quoteattr(v)) for a, v in attrs))

    body: List[str] = []
    n_out = 0
    for t in defn.tables:
        if not _ok(t, loss):
            continue
        name = t.symbol or t.name
        if t.z.equation and _expr(t.z)[1] is None:
            loss.setdefault(name, []).append("its conversion has no inverse: RomRaider shows "
                                              "it but cannot write it")
        typ = {"constant": "1D", "curve": "2D", "map": "3D"}[t.kind]
        if t.kind == "curve" and t.x is None:
            typ = "1D"
        attrs = [("type", typ), ("name", t.name)]
        if t.categories:
            attrs.append(("category", t.categories[0]))
        attrs += [("storagetype", _stype(t.z)), ("endian", rr_endian(t.z))]
        if typ == "3D":
            attrs += [("sizex", str(t.cols)), ("sizey", str(t.rows))]
            if t.order == "col":
                attrs.append(("swapxy", "true"))
        elif typ == "2D":
            attrs.append(("sizey", str(t.cols)))
        elif t.cols * t.rows > 1:
            attrs.append(("sizex", str(t.cols * t.rows)))
        attrs.append(("storageaddress", "0x%X" % t.z.addr))
        out = ["  <table %s>" % " ".join("%s=%s" % (a, quoteattr(v)) for a, v in attrs),
               sc_xml(t.z, t.decimals, "   ")]
        axes = []
        if typ == "2D":
            axes = [("Y Axis", t.x, t.cols)]
        elif typ == "3D":
            axes = [("X Axis", t.x, t.cols), ("Y Axis", t.y, t.rows)]
        for kind, ad, n in axes:
            label = ad.name if ad and ad.name else kind
            if ad is not None and ad.values is not None and ad.values.addr is not None:
                st = ad.values
                out.append("   <table type=%s name=%s storagetype=%s endian=%s storageaddress=\"0x%X\">" % (
                    quoteattr(kind), quoteattr(label), quoteattr(_stype(st)),
                    quoteattr(rr_endian(st)), st.addr))
                out.append(sc_xml(st, None, "    "))
                out.append("   </table>")
            else:
                pts = list(ad.labels) if ad is not None and ad.labels else (
                    ["%g" % v for v in ad.fixed] if ad is not None and ad.fixed else [])
                out.append("   <table type=%s name=%s sizey=\"%d\">" % (
                    quoteattr("Static " + kind), quoteattr(label), n))
                out += ["    <data>%s</data>" % escape(str(p)) for p in pts[:n]]
                out.append("   </table>")
        if t.description:
            out.append("   <description>%s</description>" % escape(t.description))
        out.append("  </table>")
        body += out
        n_out += 1
    head = ['<?xml version="1.0" encoding="utf-8"?>',
            "<!-- written by OVT Converter -->", "<roms>", " <rom>", "  <romid>",
            "   <xmlid>%s</xmlid>" % escape(defn.info.get("rom_xmlid") or title or defn.title or "ovt")]
    for k in ("internalidaddress", "internalidstring", "ecuid", "make", "model", "submodel",
              "market", "year", "transmission", "flashmethod"):
        v = defn.info.get("rom_" + k)
        if v:
            head.append("   <%s>%s</%s>" % (k, escape(v), k))
    mm = escape(defn.info.get("rom_memmodel") or "unknown")
    if one is not None:
        head.append("   <memmodel endian=\"%s\">%s</memmodel>" % (
            "little" if one == "<" else "big", mm))
    else:
        head.append("   <memmodel>%s</memmodel>" % mm)
    head.append("  </romid>")
    return "\n".join(head + body + [" </rom>", "</roms>", ""]), n_out
