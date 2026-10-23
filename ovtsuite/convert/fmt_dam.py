# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Writing Bosch DAMOS (``.dam``) descriptions.

Two ways, by where the definition came from:

* **from a DAMOS** (``Definition.passthrough``): the source file is
  re-emitted record by record. A record nothing changed goes out byte for
  byte; a translated description is replaced inside its braces; a table left
  out by a filter drops its record, and the parameter numbers, the group
  references to shared axes, the function member lists and the header count
  follow. Unchanged, the output is the source file exactly.
* **from anything else**: the file is written the way the Bosch-generated
  DAMOS files in ``Reference/`` are (S80166 processor, LF line ends, ASCII):
  one ``/REG`` per conversion, one ``/SRC`` per axis input, ``/SPZ``
  parameter records and ``/FKT`` functions from the categories.

Rules measured from the reference corpus (docs/CONVERTER.md): ``/ABL`` is 2
for kinds 2, 3 and 6 and 0 otherwise; ``$pointer`` equals ``$address`` for
kinds 1 and 12; the header counts equal the record counts.

A curve or map can only be a DAMOS parameter when the image stores it as a
DAMOS record: per axis its input's RAM address, its point count and its
points, then the values, the last axis varying fastest. A table stored any
other way is left out and said so.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..definitions.model import Definition, TableDef
from ..equation import Equation

_ASCII = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe",
                        "Ü": "Ue", "ß": "ss", "°": "Grad ", "µ": "u", "—": "-",
                        "–": "-", "·": "*", "×": "x", "…": "..."})


def _ascii(s: str) -> str:
    s = str(s or "").translate(_ASCII).encode("ascii", "replace").decode("ascii")
    return " ".join(s.replace("{", "(").replace("}", ")").replace(";", ",").split())


def _num(v: float) -> str:
    """A real number as the reference files write one: ``0.``, ``10000.``,
    ``0.31999``, ``1.310720E9``."""
    v = float(v)
    if v == int(v) and abs(v) < 1e7:
        return "%d." % int(v)
    s = "%.15G" % v                 # exact: a scaling must read back the same
    if "E" in s:
        m, e = s.split("E")
        if "." not in m:
            m += "."
        return "%sE%d" % (m, int(e))
    return s


# ------------------------------------------------------------- the source
def split_records(text: str) -> List[str]:
    """The file as its records, each with the text up to and including the
    ``;`` that ends it and the line break after (braces may hold anything).
    Joined, they are the file."""
    out: List[str] = []
    start = 0
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        elif ch == ";" and depth == 0:
            j = i + 1
            while j < n and text[j] in "\r\n":
                j += 1
                if text[j - 1] == "\n":
                    break
            out.append(text[start:j])
            start = j
            i = j
            continue
        i += 1
    if start < n:
        out.append(text[start:])
    return out


_SPZ = re.compile(r"^(\s*)(\d+)(\s*,\s*/SPZ\s*,\s*)([^,\s]+)(\s*,\s*)(\{[^}]*\})(\s*,\s*)(-?\d+)")
_SPXY = re.compile(r"(/SP[XY]\s*,[^;/]*?,)(\s*)(\d+)(\s*(?=[\r\n/;]))")


def _inner(brace: str) -> str:
    return brace[1:-1].strip()


def passthrough_dumps(defn: Definition, raw: bytes, loss: Dict[str, List[str]],
                      warnings: List[str]) -> Tuple[str, int]:
    text = bytes(raw).decode("latin-1")
    recs = split_records(text)
    tabs = {t.symbol or t.name: t for t in defn.tables}
    params = []                      # (index in recs, number, name, kind)
    for i, r in enumerate(recs):
        m = _SPZ.match(r)
        if m:
            params.append((i, int(m.group(2)), m.group(4), int(m.group(8))))
    names = {p[2] for p in params}
    for n in tabs:
        if n not in names:
            loss.setdefault(n, []).append(
                "not in the source DAMOS: writing back into a DAMOS keeps its own "
                "parameters only -- left out")
    keep = {p[2] for p in params if p[2] in tabs}
    # a group curve / map needs its shared axes (kind 6), wanted or not
    by_no = {p[1]: p for p in params}
    for i, no, name, kind in params:
        if name in keep and kind in (7, 8):
            for m in _SPXY.finditer(recs[i]):
                ref = by_no.get(int(m.group(3)))
                if ref is not None and ref[3] == 6:
                    keep.add(ref[2])
    renumber = len(keep) != len(params)
    newno: Dict[int, int] = {}
    k = 0
    for i, no, name, kind in params:
        if name in keep:
            k += 1
            newno[no] = k if renumber else no
    out = list(recs)
    dropped = set()
    for i, no, name, kind in params:
        r = recs[i]
        if name not in keep:
            out[i] = ""
            dropped.add(name)
            continue
        m = _SPZ.match(r)
        t = tabs.get(name)
        if t is not None and _ascii(t.description) != _ascii(_inner(m.group(6))):
            r = r[:m.start(6)] + "{%s}" % _ascii(t.description) + r[m.end(6):]
            m = _SPZ.match(r)
        if renumber:
            r = r[:m.start(2)] + str(newno[no]) + r[m.end(2):]
            if kind in (7, 8):
                r = _SPXY.sub(lambda mm: mm.group(1) + mm.group(2) +
                              str(newno.get(int(mm.group(3)), int(mm.group(3)))) +
                              mm.group(4), r)
        out[i] = r
    if renumber:
        # header parameter count, function member lists
        out[0] = re.sub(r"(/SPC\s*,\s*)(\d+)", lambda mm: mm.group(1) + str(len(keep)),
                        out[0], count=1)
        for i, r in enumerate(out):
            if "/FKT" in r and dropped:
                out[i] = _fkt_without(r, dropped)
    return "".join(out), len(keep)


def _fkt_without(rec: str, dropped) -> str:
    """A ``/FKT`` record with ``dropped`` names taken out of its lists (the
    count in front of each list follows)."""
    head, sep, body = rec.partition("}")
    if not sep:
        return rec
    tail = ";" + rec.split(";", 1)[1] if ";" in body else ""
    body = body.split(";", 1)[0]
    items = re.split(r"(,|\n)", body)
    fields = [x for x in items[::2]]
    seps = items[1::2]
    i = 0
    out_fields: List[str] = []
    while i < len(fields):
        f = fields[i].strip()
        if f.lstrip("-").isdigit() and out_fields is not None:
            n = int(f)
            members = fields[i + 1:i + 1 + n]
            kept = [x for x in members if x.strip() not in dropped]
            out_fields.append(str(len(kept)))
            out_fields.extend(kept)
            i += 1 + n
        else:
            out_fields.append(fields[i])
            i += 1
    # rebuild with the original separators where they still line up
    rebuilt = ""
    for j, f in enumerate(out_fields):
        rebuilt += f
        if j < len(out_fields) - 1:
            rebuilt += seps[j] if j < len(seps) else ","
    return head + sep + rebuilt + tail


# ------------------------------------------------------------- synthesis
class _Out:
    def __init__(self):
        self.regs: Dict[tuple, int] = {}
        self.reg_lines: List[str] = []
        self.srcs: Dict[tuple, int] = {}
        self.src_lines: List[str] = []

    def reg(self, factor: float, offset: float, units: str, decimals: Optional[int],
            lo: float, hi: float) -> int:
        key = (factor, offset, units, decimals)
        if key in self.regs:
            return self.regs[key]
        n = len(self.reg_lines) + 1
        self.regs[key] = n
        dec = 3 if decimals is None else max(0, int(decimals))
        self.reg_lines.append(
            "%d,/REG,CONV_%d,{%s},6,0,{%s},1,%d,%s,%s\n/REP,%s,%s,%s,%s,%s,%s;\n" % (
                n, n, _ascii("raw * %g %+g" % (factor, offset)), _ascii(units),
                1 + dec, _num(lo), _num(hi), _num(1.0), _num(-offset), _num(0.0),
                _num(factor), _num(0.0), _num(0.0)))
        return n

    def src(self, name: str, addr: Optional[int], typ: int) -> int:
        key = (name, addr)
        if key in self.srcs:
            return self.srcs[key]
        n = len(self.src_lines) + 1
        self.srcs[key] = n
        self.src_lines.append("%d,/SRC,%s,{%s},$%X,%d,0,2,3;\n" % (
            n, re.sub(r"\W", "_", name)[:32] or "Source%d" % n, _ascii(name),
            addr or 0, typ))
        return n


def _typ(st) -> Optional[int]:
    if st.float or st.bits not in (8, 16, 32):
        return None
    t = st.bits // 8
    return -t if st.signed else t


def _limits(st) -> Tuple[float, float]:
    bits = st.bits
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if st.signed else (0, (1 << bits) - 1)
    f, o = _lin(st)
    a, b = lo * f + o, hi * f + o
    return (min(a, b), max(a, b))


def _lin(st) -> Tuple[float, float]:
    if st.equation:
        eq = Equation(st.equation)
        return (eq.factor, eq.offset)
    return (st.factor, st.offset)


def _linear(st) -> bool:
    if not st.equation:
        return True
    try:
        return Equation(st.equation).linear
    except Exception:
        return False


def _record_layout(t: TableDef) -> Optional[Tuple[int, List[tuple], bool]]:
    """``(record start, [axes in storage order], swapped)`` when the table is
    stored as a DAMOS record, else None. ``swapped``: the first stored axis
    is the table's X (a column-major map)."""
    axes = [a for a in (t.x, t.y) if a is not None]
    if any(a.values is None or a.values.addr is None for a in axes):
        return None
    if t.kind == "curve":
        if len(axes) != 1:
            return None
        a = axes[0]
        if a.values.addr + a.count * a.values.size != t.z.addr:
            return None
        return a.values.addr - 4, [a], False
    if t.kind == "map":
        if len(axes) != 2:
            return None
        a1, a2 = sorted(axes, key=lambda a: a.values.addr)
        if a1.values.addr + a1.count * a1.values.size + 4 != a2.values.addr:
            return None
        if a2.values.addr + a2.count * a2.values.size != t.z.addr:
            return None
        fastest = t.x if t.order == "row" else t.y
        if a2 is not fastest:
            return None
        return a1.values.addr - 4, [a1, a2], a1 is t.x
    return None


def synth_dumps(defn: Definition, size: int, image: Optional[bytes],
                loss: Dict[str, List[str]], warnings: List[str]) -> Tuple[str, int]:
    endian = defn.byte_order()
    out = _Out()
    spz: List[str] = []
    members: Dict[str, List[str]] = {}
    names_used = set()

    def lose(t, why):
        loss.setdefault(t.name, []).append(why + " -- left out")

    def ram(addr: int) -> Optional[int]:
        if image is None or addr < 0 or addr + 2 > len(image):
            return None
        return int.from_bytes(image[addr:addr + 2], "little" if endian == "<" else "big")

    def axis_spec(a, n_rec) -> Optional[str]:
        st = a.values
        typ = _typ(st)
        if typ is None:
            return None
        lo, hi = _limits(st)
        f, o = _lin(st)
        unit = out.reg(f, o, st.units or "", None, lo, hi)
        return unit, typ, lo, hi

    for t in defn.tables:
        z = t.z
        if z.addr is None:
            lose(t, "no stored values")
            continue
        name = re.sub(r"[^\w.]", "_", t.symbol or t.name)[:48] or "P"
        base_name, k = name, 1
        while name in names_used:
            k += 1
            name = "%s_%d" % (base_name, k)
        names_used.add(name)
        desc = _ascii(t.description or (t.name if t.name != name else ""))
        n_rec = len(spz) + 1
        if t.text_kind == "ascii":
            spz.append("%d,/SPZ,%s,{%s},12\n$%X,$%X\n/SPW,0,%d,  0.,  0.\n"
                       "/SPX,0,0,  0.,  0.,0\n/SPY,0,0,  0.,  0.,0\n/FKX,0,0,0\n"
                       "/FKY,0,0,0\n/ABL,0;\n" % (n_rec, name, desc, z.addr, z.addr,
                                                  t.cols * t.rows))
            members.setdefault(t.categories[0] if t.categories else "", []).append(name)
            continue
        typ = _typ(z)
        if typ is None:
            lose(t, "%s values have no DAMOS type" % ("float" if z.float else "%d-bit" % z.bits))
            continue
        if not _linear(z):
            lose(t, "conversion %s is not linear" % z.equation)
            continue
        lo, hi = _limits(z)
        f, o = _lin(z)
        wu = out.reg(f, o, z.units or "", t.decimals, lo, hi)
        if t.kind == "constant":
            spz.append("%d,/SPZ,%s,{%s},1\n$%X,$%X\n/SPW,%d,%d,%s,%s\n"
                       "/SPX,0,0,  0.,  0.,0\n/SPY,0,0,  0.,  0.,0\n/FKX,0,0,0\n"
                       "/FKY,0,0,0\n/ABL,0;\n" % (n_rec, name, desc, z.addr, z.addr,
                                                  wu, typ, _num(lo), _num(hi)))
        elif t.kind in ("curve", "map"):
            lay = _record_layout(t)
            if lay is None:
                lose(t, "not stored as a DAMOS record (input, count, points, values)")
                continue
            rec, axes, swapped = lay
            specs = []
            bad = False
            for a in axes:
                s = axis_spec(a, n_rec)
                if s is None or not _linear(a.values):
                    bad = True
                    break
                head = a.values.addr - 4
                src_addr = ram(head)
                if src_addr is None:
                    loss.setdefault(t.name, []).append(
                        "axis input RAM address not known without a binary -- "
                        "written as $0")
                specs.append(s + (out.src(a.name or "Input of %s" % name, src_addr,
                                          s[1]), a.count))
            if bad:
                lose(t, "an axis has no DAMOS type or a non-linear conversion")
                continue
            loss.setdefault(t.name, []).append(
                "pointer-table address not known: $pointer written as the "
                "record address")
            kind = 2 if t.kind == "curve" else 3
            (xu, xt, xlo, xhi, xin, xn) = specs[0]
            line = ("%d,/SPZ,%s,{%s},%d\n$%X,$%X\n/SPW,%d,%d,%s,%s\n"
                    "/SPX,%d,%d,%s,%s,%d\n" % (n_rec, name, desc, kind, rec, rec, wu,
                                               typ, _num(lo), _num(hi), xu, xt,
                                               _num(xlo), _num(xhi), xin))
            if kind == 3:
                (yu, yt, ylo, yhi, yin, yn) = specs[1]
                line += "/SPY,%d,%d,%s,%s,%d\n/FKX,%d,0,0\n/FKY,%d,0,0\n/ABL,2;\n" % (
                    yu, yt, _num(ylo), _num(yhi), yin, xn, yn)
            else:
                line += "/SPY,0,0,  0.,  0.,0\n/FKX,%d,0,0\n/FKY,0,0,0\n/ABL,2;\n" % xn
            spz.append(line)
        else:
            lose(t, "a value block without an axis has no DAMOS kind")
            continue
        members.setdefault(t.categories[0] if t.categories else "", []).append(name)

    le = endian == "<"
    title = _ascii(defn.title or "OVT")
    from .. import branding
    head = ("/EPR, %s, %s.DAM:, DAMPAR_03\n/EAD,$FFFF\n/EPK,{}\n"
            "Generated by %s %s\n/PNR,{}\n/KNR,{}\n/PRO,{%s}\n/YNR,{}\n/BEA,{%s}\n"
            "/DST,{},{},{}\n/TEL,{}\n/STA\n/VAD,$1032\n/SPC,%d\n/UMR,%d\n/UTB,0\n"
            "/SRC,%d\n/UMV,0\n/UP,{%s}\n/SGB,{}\n/ABL,2\n/RFG,2\n/DAB,4 \n"
            "/PBA, $0h \n/SND,CODE1 $0 $%X;\n" % (
                title[:20], title[:8], _ascii(branding.APP_NAME), branding.__version__,
                title[:40], _ascii(branding.APP_NAME), len(spz), len(out.reg_lines),
                len(out.src_lines), "S80166" if le else "MPC555", max(0, size - 1)))
    if not le:
        warnings.append("DAMOS for a big-endian image: no Bosch reference file of "
                        "this kind was available to check the writer against.")
    fkt = []
    for label, mem in members.items():
        if not label or not mem:
            continue
        code = re.sub(r"\W", "_", label.split(":")[0])[:16] or "FKT"
        lines = []
        for i in range(0, len(mem), 10):
            lines.append(",".join(mem[i:i + 10]))
        fkt.append("/FKT,%s,{%s}\n0\n%d,%s;\n" % (code, _ascii(label), len(mem),
                                                  "\n".join(lines)))
    return head + "".join(spz) + "".join(out.reg_lines) + "".join(out.src_lines) \
        + "".join(fkt), len(spz)


def dumps(defn: Definition, size: int, image: Optional[bytes], warnings: List[str],
          loss: Dict[str, List[str]]) -> Tuple[str, int]:
    pt = defn.passthrough
    if isinstance(pt, tuple) and pt and pt[0] == "dam":
        return passthrough_dumps(defn, pt[1], loss, warnings)
    return synth_dumps(defn, size, image, loss, warnings)
