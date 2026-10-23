# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Calibration value files (DCM, CDFX): the values themselves, by name.

A definition says where a table is and how it converts; a value file says
what a table's physical values are, keyed by the table's name. With a
definition and a binary, values can be exported (read out of the image) or
applied (written into it). Without either, a value file converts to the
other value format.

One model for both formats (:class:`ValueSet` of :class:`Param`), so DCM and
CDFX read and write through the same export and apply. Values are physical:
a number, or a text where the definition has a verbal conversion (A2L
``COMPU_VTAB``).

Conventions in :class:`Param`: ``z`` is flat, Y outer and X inner -- the
value at axis point (x, y) is ``z[y * nx + x]``, for a 2-D block row ``y``
column ``x``. Axis kinds: ``std`` (stored with the table), ``com`` (a shared
axis, named by ``x_ref``), ``fix`` (given by the definition).

Nothing is matched by guessing: a name is found exactly, then ignoring
case, then by the file's display name; a table whose size differs from the
definition's is reported and left alone.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..definitions.model import Definition
from .entities import Image, entities as _entities

#: value formats, read and written
FORMATS = ("dcm", "cdfx")
KINDS = ("value", "block", "curve", "map", "axis", "text", "cuboid")


@dataclass
class Param:
    name: str
    kind: str
    #: physical values (float or str), flat: Y outer, X inner
    z: list = field(default_factory=list)
    nx: int = 1
    ny: int = 1
    x: Optional[list] = None
    y: Optional[list] = None
    x_kind: str = ""
    y_kind: str = ""
    x_ref: str = ""
    y_ref: str = ""
    unit: str = ""
    unit_x: str = ""
    unit_y: str = ""
    description: str = ""
    function: str = ""
    display: str = ""
    #: what the reader could not take in (a variant block, a count that
    #: does not match the values)
    notes: List[str] = field(default_factory=list)

    @property
    def cells(self) -> int:
        return self.nx * self.ny


@dataclass
class ValueSet:
    params: List[Param] = field(default_factory=list)
    title: str = ""
    #: (function, version, description), DCM ``FUNKTIONEN``
    functions: List[Tuple[str, str, str]] = field(default_factory=list)
    #: header comment lines, without the comment marker
    comments: List[str] = field(default_factory=list)
    #: what the file says it was made from: {"a2l": ..., "image": ...}
    origin: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class ValueFileError(ValueError):
    """A value file that cannot be read."""


# ------------------------------------------------------------------ sniffing
def detect(data: bytes) -> Optional[str]:
    """``"dcm"`` / ``"cdfx"`` from the file's own structure, else None."""
    from . import fmt_cdfx, fmt_dcm
    if fmt_cdfx.is_cdfx(data):
        return "cdfx"
    if fmt_dcm.is_dcm(data):
        return "dcm"
    return None


def load(data: bytes, title: str = "") -> ValueSet:
    from . import fmt_cdfx, fmt_dcm
    t = detect(data)
    if t == "cdfx":
        vs = fmt_cdfx.loads(data)
    elif t == "dcm":
        vs = fmt_dcm.loads(data)
    else:
        raise ValueFileError("not a DCM or CDFX value file")
    vs.title = vs.title or title
    return vs


def dumps(vs: ValueSet, fmt: str, source: str = "") -> bytes:
    from . import fmt_cdfx, fmt_dcm
    if fmt == "dcm":
        return fmt_dcm.dumps(vs, source)
    if fmt == "cdfx":
        return fmt_cdfx.dumps(vs, source)
    raise ValueFileError("no writer for %r" % fmt)


def merge(sets: List[ValueSet]) -> ValueSet:
    """Several value files as one; a later file's value for a name replaces
    an earlier one (how a delta DCM is applied over a full one)."""
    out = ValueSet(title=sets[0].title if sets else "")
    pos: Dict[str, int] = {}
    for vs in sets:
        out.comments += vs.comments
        out.warnings += vs.warnings
        out.functions += [f for f in vs.functions if f not in out.functions]
        for k, v in vs.origin.items():
            out.origin.setdefault(k, v)
        for p in vs.params:
            if p.name in pos:
                out.params[pos[p.name]] = p
            else:
                pos[p.name] = len(out.params)
                out.params.append(p)
    return out


# ------------------------------------------------------------------ helpers
class WritableImage(Image):
    """An image a value file can be applied to (what :class:`Entity` reads
    and writes)."""

    def __init__(self, data: bytes, endian: str = ">"):
        super().__init__(b"", endian)
        self.data = bytearray(data)

    def read(self, addr: int, bits: int = 16, signed: bool = False) -> int:
        n = max(1, bits // 8)
        return int.from_bytes(bytes(self.data[addr:addr + n]),
                              "little" if self.endian == "<" else "big",
                              signed=signed)

    def write(self, addr: int, value: int, bits: int = 16) -> None:
        n = max(1, bits // 8)
        value &= (1 << (8 * n)) - 1
        self.data[addr:addr + n] = value.to_bytes(
            n, "little" if self.endian == "<" else "big")


def ident(name: str) -> str:
    """``name`` as an identifier the value formats accept (a DCM name is one
    token, a CDFX SHORT-NAME an identifier): every run of other characters
    becomes ``_`` (``"Target Boost (MT)"`` -> ``"Target_Boost_MT"``).
    Indices (``[3]``) and dots are kept."""
    return re.sub(r"[^A-Za-z0-9_.\[\]]+", "_", name).strip("_") or "_"


def _first_line(text: str) -> str:
    return (text or "").split("\n", 1)[0].strip()


def _function(t) -> str:
    """The function a table belongs to, when the definition says so as an
    identifier (an A2L ``FUNCTION``, a DAMOS ``/FKT``)."""
    for c in getattr(t, "categories", []) or []:
        name = str(c).split(":", 1)[0].strip()
        if name and not re.search(r"\s", name):
            return name
    return ""


def _axis_kind(ad) -> str:
    if ad is None:
        return ""
    if ad.values is not None and ad.values.addr is not None:
        return "com" if ad.ref else "std"
    return "fix"


def _axis_raw(e, ax) -> list:
    signed = bool(ax.signed) if ax.signed is not None else False
    return e.binfile.read_many(ax.addr, ax.count, ax.bits, signed)


def _axis_points(e, ax, ad) -> list:
    """An entity axis' points, texts where its conversion has them."""
    if ax is None:
        return []
    if ax.addr is None:
        if ad is not None and ad.labels and len(ad.labels) >= ax.count:
            return list(ad.labels[:ax.count])
        return list(ax.values or [])[:ax.count]
    vals = e.axis_values(ax)
    st = ad.values if ad is not None else None
    if ax.is_float:
        raws = [struct.unpack_from(e.binfile.endian + "f", e.binfile.data,
                                   ax.addr + 4 * i)[0] for i in range(ax.count)]
        return [_shortest(ax.eq, r, True) for r in raws]
    if ax.signed is None:
        # as Entity.axis_values decided: unsigned when that reads monotonic
        un = e.binfile.read_many(ax.addr, ax.count, ax.bits, False)
        sg = e.binfile.read_many(ax.addr, ax.count, ax.bits, True)
        raws = un if [ax.eq.decode(v) for v in un] == vals else sg
    else:
        raws = _axis_raw(e, ax)
    out = [_shortest(ax.eq, r, False) for r in raws]
    if st is not None and st.texts:
        out = [st.texts.get(r, v) for r, v in zip(raws, out)]
    return out


def _shortest(eq, raw, is_float: bool):
    """The physical value of ``raw``: the shortest decimal that is the exact
    value to 1e-9 of it (``0.3``, not ``0.30000000000000004``; ``1000.3125``
    stays) and converts back to exactly ``raw`` -- so a value file written
    from an image applies back to the same bytes, however fine the
    conversion."""
    phys = eq.decode(raw)
    if phys != phys or phys in (float("inf"), float("-inf")):
        return phys
    for prec in (6, 8, 10, 12, 15, 17):
        v = float("%.*g" % (prec, phys))
        if abs(v - phys) > 1e-9 * abs(phys):
            continue
        try:
            back = eq.encode(v)
        except (ZeroDivisionError, OverflowError, ValueError):
            return phys
        if is_float:
            if struct.pack("<f", back) == struct.pack("<f", raw):
                return v
        elif back == back and round(back) == raw:
            return v
    return phys


def _cell(e, r, c):
    raw = e.raw_cell(r, c)
    st = e._tabledef.z
    if st.texts and isinstance(raw, int) and raw in st.texts:
        return st.texts[raw]
    return _shortest(e.eq, raw, e.is_float)


def _text_of(e) -> str:
    raw = bytes(e.binfile.data[e.addr:e.addr + e.n_cells])
    return raw.split(b"\0", 1)[0].decode("latin-1")


# ------------------------------------------------------------------ export
def export(defn: Definition, size: int, image: bytes,
           loss: Optional[Dict[str, List[str]]] = None,
           dynamic: str = "skip") -> ValueSet:
    """Every table of ``defn`` (addresses in image offsets) with its values
    read from ``image``, and every shared axis once."""
    loss = loss if loss is not None else {}
    endian = defn.byte_order()
    ents = _entities(defn, 0, size, image, endian, dynamic, loss)
    img = WritableImage(image, endian)
    for e in ents:
        e.binfile = img
    vs = ValueSet(title=defn.title)
    axes: Dict[str, Param] = {}
    for e in ents:
        t = e._tabledef
        p = Param(name=e.name, kind="value", unit=e.units or "",
                  description=_first_line(t.description),
                  function=_function(t))
        try:
            if e.text_kind:
                p.kind, p.z = "text", [_text_of(e)]
            elif e.kind == "constant":
                p.z = [_cell(e, 0, 0)]
            elif e.kind == "indexed" or (e.kind in ("curve", "map") and e.x is None):
                p.kind, p.nx, p.ny = "block", e.cols, e.rows
                p.z = [_cell(e, r, c) for r in range(e.rows) for c in range(e.cols)]
            elif e.kind == "curve":
                p.kind, p.nx = "curve", e.cols
                p.z = [_cell(e, 0, c) for c in range(e.cols)]
            else:
                p.kind, p.nx, p.ny = "map", e.cols, e.rows
                p.z = [_cell(e, r, c) for r in range(e.rows) for c in range(e.cols)]
        except (struct.error, IndexError, TypeError) as exc:
            loss.setdefault(e.name, []).append("values not readable (%s) -- left out" % exc)
            continue
        for tag in ("x", "y"):
            ax = getattr(e, tag)
            ad = getattr(t, tag)
            if ax is None or p.kind not in ("curve", "map") or (tag == "y" and p.kind != "map"):
                continue
            pts = _axis_points(e, ax, ad)
            setattr(p, tag, pts)
            setattr(p, tag + "_kind", _axis_kind(ad))
            setattr(p, "unit_" + tag, ax.units or "")
            if ad is not None and ad.ref and _axis_kind(ad) == "com":
                setattr(p, tag + "_ref", ad.ref)
                if ad.ref not in axes:
                    axes[ad.ref] = Param(name=ad.ref, kind="axis", z=list(pts),
                                         nx=len(pts), unit=ax.units or "")
        vs.params.append(p)
    vs.params += [axes[k] for k in sorted(axes)]
    return vs


# ------------------------------------------------------------------ apply
@dataclass
class ApplyReport:
    applied: int = 0
    cells_changed: int = 0
    unchanged: int = 0
    not_found: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    clamped: Dict[str, int] = field(default_factory=dict)
    #: axes the file gives differently from the binary that were not written
    #: (shared or fixed axes, which belong to another object)
    axis_differs: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        out = ["%d value%s applied: %d changed (%d cell%s), %d already as given."
               % (self.applied, "" if self.applied == 1 else "s", len(self.changed),
                  self.cells_changed, "" if self.cells_changed == 1 else "s",
                  self.unchanged)]
        if self.not_found:
            out.append("Not in the definition (%d): %s" % (
                len(self.not_found), ", ".join(self.not_found[:40]) +
                (" ..." if len(self.not_found) > 40 else "")))
        for k in sorted(self.skipped):
            out.append("Not applied: %s -- %s" % (k, self.skipped[k]))
        for k in sorted(self.clamped):
            out.append("Limited to what the cell can hold: %s (%d cells)"
                       % (k, self.clamped[k]))
        for k in self.axis_differs:
            out.append("Axis given differently and not written: %s" % k)
        return out


def _index(name: str) -> Tuple[str, List[int]]:
    """``"c_x[2]"`` -> ``("c_x", [2])``; CDFX names one element of a block
    like that."""
    m = re.match(r"^(.*?)((?:\[\d+\])+)$", name)
    if not m:
        return name, []
    return m.group(1), [int(i) for i in re.findall(r"\d+", m.group(2))]


def _num(v):
    """A value file's value as a number, or None for a text."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _text_raw(st, v) -> Optional[int]:
    if not isinstance(v, str) or st is None or not st.texts:
        return None
    for raw, txt in st.texts.items():
        if txt == v or txt.split(" (to ", 1)[0] == v:
            return raw
    return None


def _raw_for(e, st, v):
    """``(raw, clamped)`` for a physical value or a text; raw None when a
    text is not one of the conversion's."""
    raw = _text_raw(st, v)
    if raw is not None:
        return raw, False
    n = _num(v)
    if n is None:
        return None, False
    if e.is_float:
        return float(e.eq.encode(n)), False
    raw = int(round(e.eq.encode(n)))
    lo, hi = e.raw_range
    return max(lo, min(hi, raw)), not (lo <= raw <= hi)


def _write_cell(e, r, c, v, rep: ApplyReport) -> Optional[int]:
    """Write one cell; 1 if it changed, 0 if not, None if the value cannot
    be stored."""
    raw, clamped = _raw_for(e, e._tabledef.z, v)
    if raw is None:
        return None
    if clamped:
        rep.clamped[e.name] = rep.clamped.get(e.name, 0) + 1
    a = e.addr + e.index(r, c) * e.step
    en = e.binfile.endian
    if e.is_float:
        new = struct.pack(en + "f", raw)
    else:
        enc = raw + (1 << e.bits) if raw < 0 else raw
        new = enc.to_bytes(e.step, "little" if en == "<" else "big")
    if bytes(e.binfile.data[a:a + e.step]) == new:
        return 0
    e.binfile.data[a:a + e.step] = new
    return 1


def _write_axis(e, ax, ad, pts, rep: ApplyReport) -> int:
    """Write a stored axis' points; the cells changed."""
    st = ad.values
    signed = ax.signed if ax.signed is not None else any(
        (_num(v) or 0) < 0 for v in pts)
    n = max(1, ax.bits // 8)
    en = e.binfile.endian
    changed = 0
    for i, v in enumerate(pts[:ax.count]):
        a = ax.addr + i * n
        raw = _text_raw(st, v)
        if raw is None:
            num = _num(v)
            if num is None:
                continue
            if ax.is_float:
                new = struct.pack(en + "f", float(ax.eq.encode(num)))
            else:
                raw = int(round(ax.eq.encode(num)))
        if raw is not None:
            lo = -(1 << (ax.bits - 1)) if signed else 0
            hi = (1 << (ax.bits - 1)) - 1 if signed else (1 << ax.bits) - 1
            if not lo <= raw <= hi:
                key = e.name + " (axis)"
                rep.clamped[key] = rep.clamped.get(key, 0) + 1
                raw = max(lo, min(hi, raw))
            enc = raw + (1 << ax.bits) if raw < 0 else raw
            new = enc.to_bytes(n, "little" if en == "<" else "big")
        if bytes(e.binfile.data[a:a + n]) != new:
            e.binfile.data[a:a + n] = new
            changed += 1
    return changed


def _same_points(a: list, b: list) -> bool:
    return _close(a, b, 1e-6)


def apply(vs: ValueSet, defn: Definition, size: int, image: bytes,
          loss: Optional[Dict[str, List[str]]] = None,
          rep: Optional[ApplyReport] = None) -> Tuple[bytes, ApplyReport]:
    """``image`` with every value of ``vs`` whose table ``defn`` places
    written into it, and what happened."""
    rep = rep or ApplyReport()
    loss = loss if loss is not None else {}
    endian = defn.byte_order()
    ents = _entities(defn, 0, size, image, endian, "skip", loss)
    img = WritableImage(image, endian)
    for e in ents:
        e.binfile = img
    exact = {e.name: e for e in ents}
    lower: Dict[str, object] = {}
    for e in ents:
        lower.setdefault(e.name.lower(), e)
    for e in ents:
        # a name with spaces is written as an identifier (``ident``)
        lower.setdefault(ident(e.name).lower(), e)
    # shared axes, by the name the value file uses for them
    refs: Dict[str, Tuple[object, object, object]] = {}
    for e in ents:
        t = e._tabledef
        for tag in ("x", "y"):
            ad, ax = getattr(t, tag), getattr(e, tag)
            if ad is not None and ad.ref and ax is not None and ax.addr is not None:
                refs.setdefault(ad.ref, (e, ax, ad))
                refs.setdefault(ad.ref.lower(), (e, ax, ad))

    def find(name):
        return exact.get(name) or lower.get(name.lower()) or lower.get(ident(name).lower())

    def count(name, changed):
        rep.applied += 1
        if changed:
            rep.cells_changed += changed
            rep.changed.append(name)
        else:
            rep.unchanged += 1

    # shared axes first: the curves and maps on them are checked against
    # the axis as the file gives it
    order = [p for p in vs.params if p.kind == "axis"] + \
        [p for p in vs.params if p.kind != "axis"]
    for p in order:
        if p.kind == "axis":
            hit = refs.get(p.name) or refs.get(p.name.lower()) or \
                (refs.get(p.display) if p.display else None)
            if hit is None:
                rep.not_found.append(p.name)
                continue
            e, ax, ad = hit
            if len(p.z) != ax.count:
                rep.skipped[p.name] = ("%d axis points in the file, %d in the binary"
                                       % (len(p.z), ax.count))
                continue
            count(p.name, _write_axis(e, ax, ad, p.z, rep))
            continue
        base, idx = _index(p.name)
        e = find(p.name) or (find(p.display) if p.display else None)
        if e is None and idx:
            e = find(base)
        if e is None:
            rep.not_found.append(p.name)
            continue
        if p.kind == "cuboid":
            rep.skipped[p.name] = "three-dimensional tables are not supported"
            continue
        if e.text_kind:
            txt = str(p.z[0]) if p.z else ""
            raw = txt.encode("latin-1", "replace")[:e.n_cells]
            raw += b"\0" * (e.n_cells - len(raw))
            old = bytes(img.data[e.addr:e.addr + e.n_cells])
            if old != raw:
                img.data[e.addr:e.addr + e.n_cells] = raw
            count(e.name, int(old != raw))
            continue
        if idx and e.name.lower() != p.name.lower():
            # one element of a block, named "block[i]" / "block[i][j]"
            flat = idx[0] if len(idx) == 1 else idx[0] * e.cols + idx[1]
            if flat >= e.n_cells or not p.z:
                rep.skipped[p.name] = "element outside %s's %d values" % (e.name, e.n_cells)
                continue
            r, c = divmod(flat, e.cols)
            done = _write_cell(e, r, c, p.z[0], rep)
            if done is None:
                rep.skipped[p.name] = "value %r cannot be stored" % (p.z[0],)
                continue
            count(p.name, done)
            continue
        if len(p.z) != e.n_cells:
            rep.skipped[p.name] = ("%d values in the file, %d (%dx%d) in the definition"
                                   % (len(p.z), e.n_cells, e.cols, e.rows))
            continue
        changed = bad = 0
        for i, v in enumerate(p.z):
            r, c = divmod(i, e.cols)
            done = _write_cell(e, r, c, v, rep)
            if done is None:
                bad += 1
            else:
                changed += done
        t = e._tabledef
        for tag in ("x", "y"):
            pts = getattr(p, tag)
            ax, ad = getattr(e, tag), getattr(t, tag)
            if not pts or ax is None:
                continue
            if ax.addr is not None and not (ad is not None and ad.ref):
                if len(pts) == ax.count:
                    changed += _write_axis(e, ax, ad, pts, rep)
                else:
                    rep.skipped[p.name + " " + tag.upper()] = (
                        "%d axis points in the file, %d in the binary" % (len(pts), ax.count))
            elif not _same_points(pts, _axis_points(e, ax, ad)):
                rep.axis_differs.append("%s %s (%s)" % (
                    p.name, tag.upper(), ("shared axis " + ad.ref) if ad is not None and ad.ref
                    else "fixed by the definition"))
        if bad:
            rep.skipped[p.name] = "%d values are texts the conversion does not have" % bad
        count(p.name, changed)
    return bytes(img.data), rep


# ------------------------------------------------------------------ compare
def differences(a: ValueSet, b: ValueSet, tol: float = 1e-6) -> List[str]:
    """Where ``b`` disagrees with ``a``, by name (a self-check and a test)."""
    bm = {p.name: p for p in b.params}
    out = []
    for p in a.params:
        q = bm.get(p.name)
        if q is None:
            out.append("%s: missing" % p.name)
            continue
        if (p.nx, p.ny) != (q.nx, q.ny):
            out.append("%s: %dx%d vs %dx%d" % (p.name, p.nx, p.ny, q.nx, q.ny))
            continue
        for tag in ("z", "x", "y"):
            if not _close(getattr(p, tag) or [], getattr(q, tag) or [], tol):
                out.append("%s: %s differs" % (p.name, tag.upper()))
                break
    return out


def _close(u: list, v: list, tol: float) -> bool:
    if len(u) != len(v):
        return False
    for a, b in zip(u, v):
        na, nb = _num(a), _num(b)
        if na is not None and nb is not None:
            if abs(na - nb) > tol * max(1.0, abs(na), abs(nb)):
                return False
        elif str(a) != str(b):
            return False
    return True
