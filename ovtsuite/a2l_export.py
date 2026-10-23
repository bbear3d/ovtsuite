# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Export a session's parameters as an ASAP2 (A2L) description.

The description is of the loaded image itself: one memory segment from 0 to
its size, so every address is a file offset -- what WinOLS, TunerPro-style
importers and this app's own reader expect. (A tool that works against the
ECU's own address space, INCA or CANape, would need the segment moved to the
flash base.)

What goes in:

* a value -> ``VALUE``; a curve -> ``CURVE``; a map -> ``MAP``; a curve
  without an axis -> ``VAL_BLK``; text (part numbers) -> ``ASCII``;
* each stored axis -> one ``AXIS_PTS`` (shared by every table that reads it)
  referred to by ``COM_AXIS``, so no count headers need describing; an axis a
  definition gives as values -> ``FIX_AXIS_PAR_LIST``;
* each distinct equation and unit -> one ``COMPU_METHOD``: a linear one and a
  rational one ``(aX+b)/(X+d)`` as ``RAT_FUNC`` (physical -> raw, as ASAP2
  wants), anything else as ``FORM`` with its formula in ``X1``;
* the tree's groups -> ``FUNCTION`` blocks naming their parameters.

What does not fit is said in a ``/* ... */`` comment beside the parameter and
listed by :func:`export_a2l`'s ``warnings``: a ZF 6HP table whose columns
each convert by their own gear ratio has only one conversion in ASAP2, so it
goes out raw-to-output-speed.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import branding
from .model import Entity
from .tiers import tier_name
from .xdf_export import _axis_signed, _is_unpopulated, _zf_z_math

_ASCII = str.maketrans({
    "—": "-", "–": "-", "·": "-", "→": "->", "×": "x", "…": "...",
    "°": "deg", "µ": "u", "²": "2", "³": "3",
})


def _text(s) -> str:
    """A quoted ASAP2 string: ASCII, one line, no bare quotes."""
    s = str(s or "").translate(_ASCII)
    s = s.encode("ascii", "replace").decode("ascii")
    s = " ".join(s.split()).replace("\\", "/").replace('"', "'")
    return '"%s"' % s


_IDENT_BAD = re.compile(r"[^A-Za-z0-9_.]")


class _Names:
    """Unique ASAP2 identifiers (letters, digits, ``_`` and ``.``)."""

    def __init__(self):
        self.used = set()

    def make(self, raw: str, fallback: str = "P") -> str:
        s = _IDENT_BAD.sub("_", str(raw or "")).strip("_.") or fallback
        if not (s[0].isalpha() or s[0] == "_"):
            s = "_" + s
        base, i = s, 2
        while s in self.used:
            s = "%s_%d" % (base, i)
            i += 1
        self.used.add(s)
        return s


def _dtype(bits: int, signed: bool, flt: bool) -> str:
    if flt:
        return "FLOAT32_IEEE" if bits == 32 else "FLOAT64_IEEE"
    return {8: ("UBYTE", "SBYTE"), 16: ("UWORD", "SWORD"),
            32: ("ULONG", "SLONG")}.get(bits, ("UBYTE", "SBYTE"))[bool(signed)]


def _num(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return "%.12g" % v


class _Compus:
    """One COMPU_METHOD per (equation, units)."""

    def __init__(self, names: _Names):
        self.names = names
        self.by_key: Dict[Tuple[str, str], str] = {}
        self.blocks: List[str] = []

    def get(self, equation: str, units: str, decimals: int = 3) -> str:
        key = (equation or "X", units or "")
        if key in self.by_key:
            return self.by_key[key]
        name = self.names.make("CM_%d" % (len(self.blocks) + 1))
        self.by_key[key] = name
        self.blocks.append(self._block(name, key[0], key[1], decimals))
        return name

    @staticmethod
    def _block(name: str, eq_text: str, units: str, decimals: int) -> str:
        from .equation import Equation
        fmt = '"%%10.%d"' % max(0, min(decimals, 9))
        head = "  /begin COMPU_METHOD %s %s\n" % (name, _text(eq_text))
        eq_text = eq_text.strip()
        if eq_text in ("X", "x", ""):
            return head + "    IDENTICAL %s %s\n  /end COMPU_METHOD\n" % (fmt, _text(units))
        try:
            eq = Equation(eq_text)
        except Exception:
            eq = None
        if eq is not None and eq.linear and eq.factor != 0:
            # physical = f*X + o  <=>  raw = (1*p - o) / f
            coeffs = (0, 1, -eq.offset, 0, 0, eq.factor)
        elif eq is not None and getattr(eq, "_mobius", None) is not None:
            a, b, d = eq._mobius
            # physical = (aX + b)/(X + d)  <=>  raw = (-d*p + b) / (p - a)
            coeffs = (0, -d, b, 0, 1, -a)
        else:
            formula = re.sub(r"\b[Xx]\b", "X1", eq_text)
            return (head + "    FORM %s %s\n"
                    "    /begin FORMULA %s\n    /end FORMULA\n"
                    "  /end COMPU_METHOD\n" % (fmt, _text(units), _text(formula)))
        return (head + "    RAT_FUNC %s %s\n    COEFFS %s\n  /end COMPU_METHOD\n"
                % (fmt, _text(units), " ".join(_num(c) for c in coeffs)))


def _limits(equation: str, bits: int, signed: bool, flt: bool) -> Tuple[float, float]:
    """The physical range a cell can hold (for the limits ASAP2 asks for)."""
    from .equation import Equation
    if flt:
        return -3.4e38, 3.4e38
    lo, hi = ((-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed
              else (0, (1 << bits) - 1))
    try:
        eq = Equation(equation or "X")
        a, b = eq.decode(lo), eq.decode(hi)
        if a == a and b == b:
            return min(a, b), max(a, b)
    except Exception:
        pass
    return float(lo), float(hi)


def export_a2l(entities: List[Entity], title: str, image_size: int,
               little_endian: Optional[bool] = None, platform: str = "",
               warnings: Optional[List[str]] = None, tiers: bool = True,
               base: int = 0) -> str:
    """The A2L text for ``entities`` (in the loaded image's own addresses).
    ``little_endian`` defaults to the image's byte order; what could not be
    expressed is appended to ``warnings`` (and commented in the file)."""
    warnings = warnings if warnings is not None else []
    if little_endian is None:
        little_endian = any(getattr(e.binfile, "endian", ">") == "<"
                            for e in entities[:1])
    names = _Names()
    compus = _Compus(names)
    layouts: Dict[str, str] = {}
    axis_pts: Dict[tuple, str] = {}
    ax_blocks: List[str] = []
    chars: List[str] = []
    funcs: Dict[str, List[str]] = {}

    def layout(dt: str, kind: str, direction: str = "ROW_DIR") -> str:
        key = "RL_%s_%s" % (kind, dt) + ("" if kind == "AX" else "_" + direction[:3])
        if key not in layouts:
            names.used.add(key)
            if kind == "AX":
                body = "AXIS_PTS_X 1 %s INDEX_INCR DIRECT" % dt
            else:
                body = "FNC_VALUES 1 %s %s DIRECT" % (dt, direction)
            layouts[key] = "  /begin RECORD_LAYOUT %s\n    %s\n  /end RECORD_LAYOUT\n" % (
                key, body)
        return key

    def stored_axis(e: Entity, ax, count: int) -> str:
        signed = ax.signed if ax.signed is not None else _axis_signed(e, ax)
        key = (ax.addr, count, ax.bits, bool(signed), bool(ax.is_float),
               ax.equation or "X", ax.units or "")
        if key in axis_pts:
            return axis_pts[key]
        name = names.make("AX_%06X" % ax.addr)
        dt = _dtype(ax.bits, signed, ax.is_float)
        cm = compus.get(ax.equation, ax.units)
        lo, hi = _limits(ax.equation, ax.bits, signed, ax.is_float)
        ax_blocks.append(
            "  /begin AXIS_PTS %s %s 0x%X NO_INPUT_QUANTITY %s 0 %s %d %s %s\n"
            "  /end AXIS_PTS\n" % (name, _text("axis at 0x%X" % ax.addr), ax.addr + base,
                                   layout(dt, "AX"), cm, count, _num(lo), _num(hi)))
        axis_pts[key] = name
        return name

    def axis_descr(e: Entity, ax, count: int) -> str:
        if ax is not None and ax.addr is not None:
            ref = stored_axis(e, ax, count)
            signed = ax.signed if ax.signed is not None else _axis_signed(e, ax)
            cm = compus.get(ax.equation, ax.units)
            lo, hi = _limits(ax.equation, ax.bits, signed, ax.is_float)
            return ("    /begin AXIS_DESCR COM_AXIS NO_INPUT_QUANTITY %s %d %s %s\n"
                    "      AXIS_PTS_REF %s\n    /end AXIS_DESCR\n"
                    % (cm, count, _num(lo), _num(hi), ref))
        vals = list(getattr(ax, "values", None) or [])[:count] if ax is not None else []
        if len(vals) != count:
            vals = list(range(count))           # no axis: the cell index
        units = getattr(ax, "units", "") if ax is not None else ""
        cm = compus.get("X", units)
        return ("    /begin AXIS_DESCR FIX_AXIS NO_INPUT_QUANTITY %s %d %s %s\n"
                "      /begin FIX_AXIS_PAR_LIST %s\n      /end FIX_AXIS_PAR_LIST\n"
                "    /end AXIS_DESCR\n"
                % (cm, count, _num(min(vals)), _num(max(vals)),
                   " ".join(_num(float(v)) for v in vals)))

    for e in entities:
        if e.kind not in ("map", "curve", "constant", "indexed"):
            continue
        if platform == "edc16" and _is_unpopulated(e):
            continue                        # erased flash: not in this variant
        name = names.make(e.name)
        desc = e.title if e.title and e.title != e.name else (e.description or "")
        if e.description and e.description not in desc:
            desc = (desc + " | " if desc else "") + e.description
        flt = bool(getattr(e, "is_float", False))
        dt = _dtype(e.bits, e.signed, flt)
        note = ""
        if _zf_z_math(e):
            note = ("%s: each column converts by its own gear ratio, which ASAP2 "
                    "cannot express -- exported as stored (output speed)" % e.name)
            warnings.append(note)
        if getattr(e, "text_kind", "") == "ascii":
            chars.append("  /begin CHARACTERISTIC %s %s ASCII 0x%X %s 0 %s 0 255\n"
                         "    NUMBER %d\n  /end CHARACTERISTIC\n"
                         % (name, _text(desc), e.addr + base, layout("UBYTE", "V"),
                            compus.get("X", ""), max(1, e.n_cells)))
        else:
            cm = compus.get(e.equation, e.units)
            lo, hi = _limits(e.equation, e.bits, e.signed, flt)
            direction = "COLUMN_DIR" if (e.kind == "map" and
                                         getattr(e, "order", "col") == "col") else "ROW_DIR"
            ctype = {"constant": "VALUE", "curve": "CURVE", "map": "MAP",
                     "indexed": "VAL_BLK"}[e.kind]
            if e.kind == "curve" and e.x is None:
                ctype = "VAL_BLK"
            body = ""
            if ctype == "VAL_BLK":
                body += "    MATRIX_DIM %d %d 1\n" % (max(1, e.cols), max(1, e.rows))
            elif ctype == "CURVE":
                body += axis_descr(e, e.x, e.cols)
            elif ctype == "MAP":
                body += axis_descr(e, e.x, e.cols) + axis_descr(e, e.y, e.rows)
            if e.units:
                body += "    PHYS_UNIT %s\n" % _text(e.units)
            if getattr(e, "sel_mask", 0):
                body += "    BIT_MASK 0x%X\n" % e.sel_mask
            chars.append("  /begin CHARACTERISTIC %s %s %s 0x%X %s 0 %s %s %s\n%s"
                         "  /end CHARACTERISTIC\n"
                         % (name, _text(desc), ctype, e.addr + base,
                            layout(dt, "V", direction), cm, _num(lo), _num(hi), body))
        if note:
            chars[-1] = "  /* %s */\n" % note.replace("*/", "* /") + chars[-1]
        path = ([tier_name(e.tier)] if tiers else []) + list(
            getattr(e, "category_path", None) or e.categories[:1] or [])
        funcs.setdefault(" / ".join(p for p in path if p), []).append(name)

    out = ["/* %s A2L export of %s */\n" % (branding.APP_NAME, title.replace("*/", "")),
           'ASAP2_VERSION 1 61\n',
           "/begin PROJECT %s %s\n" % (names.make(title, "Project"), _text(title)),
           "  /begin MODULE %s %s\n" % (names.make("Module"), _text(
               "%s, generated by %s" % (platform.upper() or "image", branding.APP_NAME)))]
    out.append("  /begin MOD_COMMON %s\n    BYTE_ORDER %s\n"
               "    ALIGNMENT_BYTE 1\n    ALIGNMENT_WORD 1\n    ALIGNMENT_LONG 1\n"
               "    ALIGNMENT_FLOAT32_IEEE 1\n    ALIGNMENT_FLOAT64_IEEE 1\n"
               "  /end MOD_COMMON\n"
               % (_text("byte order of the image"),
                  "MSB_LAST" if little_endian else "MSB_FIRST"))
    out.append("  /begin MOD_PAR %s\n"
               "    /begin MEMORY_SEGMENT Image %s DATA FLASH INTERN 0x%X 0x%X "
               "-1 -1 -1 -1 -1\n    /end MEMORY_SEGMENT\n  /end MOD_PAR\n"
               % (_text("addresses are offsets in the image file" if not base
                        else "addresses are ECU addresses; the image file "
                        "starts at 0x%X" % base),
                  _text("the image file"), base, image_size))
    out.extend(layouts.values())
    out.extend(compus.blocks)
    out.extend(ax_blocks)
    out.extend(chars)
    fn = _Names()
    fn.used |= names.used
    for label, members in funcs.items():
        out.append("  /begin FUNCTION %s %s\n    /begin REF_CHARACTERISTIC\n"
                   % (fn.make(label.replace(" / ", "."), "Group"), _text(label)))
        for i in range(0, len(members), 6):
            out.append("      %s\n" % " ".join(members[i:i + 6]))
        out.append("    /end REF_CHARACTERISTIC\n  /end FUNCTION\n")
    out.append("  /end MODULE\n/end PROJECT\n")
    return "".join(out)
