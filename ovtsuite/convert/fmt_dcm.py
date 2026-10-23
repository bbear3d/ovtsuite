# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""DCM -- the DAMOS exchange file of calibration values
(``KONSERVIERUNG_FORMAT 2.0``), read and written.

Keywords, from the 220 DCM files of the reference corpus (none outside this
set): ``FESTWERT`` (a value), ``FESTWERTEBLOCK`` (a block, ``n`` or
``nx @ ny``), ``KENNLINIE`` / ``FESTKENNLINIE`` / ``GRUPPENKENNLINIE``
(curves with a stored, fixed or shared axis), ``KENNFELD`` /
``FESTKENNFELD`` / ``GRUPPENKENNFELD`` (maps), ``STUETZSTELLENVERTEILUNG``
(a shared axis) and ``TEXTSTRING``. In a block: ``LANGNAME``,
``DISPLAYNAME``, ``FUNKTION``, ``EINHEIT_X/Y/W``, ``ST/X``, ``ST_TX/X``,
``ST/Y``, ``ST_TX/Y``, ``WERT``, ``TEXT``, ``END``; a group curve/map names
its shared axes in ``*SSTX``/``*SSTY`` comments.

A map is written Y row by Y row: ``ST/Y y`` then that row's ``WERT``s
across X. Text values (verbal conversions) are ``TEXT``, text axis points
``ST_TX``.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .calvalues import Param, ValueFileError, ValueSet, ident

KINDS = {
    "FESTWERT": "value", "FESTWERTEBLOCK": "block",
    "KENNLINIE": "curve", "FESTKENNLINIE": "curve", "GRUPPENKENNLINIE": "curve",
    "KENNFELD": "map", "FESTKENNFELD": "map", "GRUPPENKENNFELD": "map",
    "STUETZSTELLENVERTEILUNG": "axis", "TEXTSTRING": "text",
}
_AXIS_KIND = {"KENNLINIE": "std", "KENNFELD": "std", "FESTKENNLINIE": "fix",
              "FESTKENNFELD": "fix", "GRUPPENKENNLINIE": "com", "GRUPPENKENNFELD": "com"}
_START = set(KINDS) | {"KONSERVIERUNG_FORMAT", "FUNKTIONEN"}
_TOKEN = re.compile(r'"((?:[^"]|"")*)"|(\S+)')
#: values per line (as INCA and CRETA write them)
PER_LINE = 6


def _text(data: bytes) -> str:
    head = bytes(data[:400]).decode("latin-1")
    m = re.search(r'encoding\s*=\s*"([^"]+)"', head)
    enc = m.group(1) if m else ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    for e in ([enc] if enc else []) + ["utf-8", "latin-1"]:
        try:
            return data.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def is_dcm(data: bytes) -> bool:
    """The first line that is not a comment starts with a DCM keyword (a
    file of only comments is an empty DCM when it says so)."""
    head = bytes(data[:8192])
    if b"\0" in head:
        return False
    text = head.decode("latin-1")
    saw_comment = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("*", "!", ".")):
            saw_comment = True
            if "DAMOS" in s.upper() or "DCM" in s.upper():
                return True
            continue
        word = s.split(None, 1)[0]
        return word in _START
    return False


def _tokens(line: str) -> List[str]:
    return [a.replace('""', '"') if a or b == "" else b
            for a, b in ((m.group(1), m.group(2)) for m in _TOKEN.finditer(line))
            ] if '"' in line else line.split()


def _quoted(line: str) -> List[str]:
    """The quoted strings of a line, in order."""
    return [m.group(1).replace('""', '"') for m in _TOKEN.finditer(line) if m.group(1) is not None]


def _value(tok: str):
    try:
        return float(tok)
    except ValueError:
        return tok


def loads(data: bytes) -> ValueSet:
    text = _text(data)
    vs = ValueSet()
    cur: Optional[Param] = None
    kw = ""
    in_fkt = False
    lines = text.splitlines()
    for no, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s:
            continue
        if s[0] in "*!.":
            body = s[1:].strip()
            if cur is not None:
                m = re.match(r"^SST([XY])\s+(\S+)", body)
                if m:
                    setattr(cur, m.group(1).lower() + "_ref", m.group(2))
            elif not vs.params:
                vs.comments.append(body)
            continue
        word, _, rest = s.partition(" ")
        rest = rest.strip()
        if in_fkt:
            if word == "END":
                in_fkt = False
            elif word == "FKT":
                q = _quoted(rest)
                name = rest.split(None, 1)[0] if rest else ""
                vs.functions.append((name, q[0] if q else "", q[1] if len(q) > 1 else ""))
            continue
        if cur is None:
            if word == "KONSERVIERUNG_FORMAT":
                continue
            if word == "FUNKTIONEN":
                in_fkt = True
                continue
            if word in KINDS:
                kw = word
                a = rest.split()
                cur = Param(name=a[0] if a else "", kind=KINDS[word],
                            x_kind=_AXIS_KIND.get(word, ""),
                            y_kind=_AXIS_KIND.get(word, "") if KINDS[word] == "map" else "")
                dims = [int(x) for x in a[1:] if x.isdigit()]
                if cur.kind == "block":
                    cur.nx = dims[0] if dims else 1
                    cur.ny = dims[1] if len(dims) > 1 and "@" in a else 1
                elif cur.kind in ("curve", "axis"):
                    cur.nx = dims[0] if dims else 1
                elif cur.kind == "map":
                    cur.nx = dims[0] if dims else 1
                    cur.ny = dims[1] if len(dims) > 1 else 1
                if cur.kind in ("curve", "map"):
                    cur.x = []
                if cur.kind == "map":
                    cur.y = []
                continue
            vs.warnings.append("line %d: %s outside a parameter -- ignored" % (no, word))
            continue
        if word == "END":
            _finish(cur, kw)
            vs.params.append(cur)
            cur = None
            continue
        if word == "LANGNAME":
            q = _quoted(rest)
            cur.description = q[0] if q else rest
        elif word == "DISPLAYNAME":
            cur.display = rest.split()[0] if rest else ""
        elif word == "FUNKTION":
            cur.function = rest.split()[0] if rest else ""
        elif word == "EINHEIT_W":
            q = _quoted(rest)
            cur.unit = q[0] if q else rest
        elif word == "EINHEIT_X":
            q = _quoted(rest)
            cur.unit_x = q[0] if q else rest
        elif word == "EINHEIT_Y":
            q = _quoted(rest)
            cur.unit_y = q[0] if q else rest
        elif word == "ST/X":
            vals = [_value(t) for t in rest.split()]
            (cur.z if cur.kind == "axis" else cur.x).extend(vals)
        elif word == "ST_TX/X":
            (cur.z if cur.kind == "axis" else cur.x).extend(_quoted(rest))
        elif word == "ST/Y":
            cur.y.extend(_value(t) for t in rest.split())
        elif word == "ST_TX/Y":
            cur.y.extend(_quoted(rest))
        elif word == "WERT":
            cur.z.extend(_value(t) for t in rest.split())
        elif word == "TEXT":
            cur.z.extend(_quoted(rest))
        elif word == "VAR":
            cur.notes.append("variant values (VAR) are not read")
        else:
            cur.notes.append("line %d: %s not read" % (no, word))
    if cur is not None:
        _finish(cur, kw)
        vs.params.append(cur)
        vs.warnings.append("%s: no END -- file ends inside it" % cur.name)
    if not vs.params and not vs.comments and not vs.functions:
        raise ValueFileError("no DCM parameters in the file")
    return vs


def _finish(p: Param, kw: str) -> None:
    if p.kind == "axis":
        p.nx = len(p.z)
        return
    if p.kind == "text":
        p.z = ["".join(str(v) for v in p.z)] if p.z else [""]
        return
    if p.kind == "map" and p.y is not None and len(p.y) != p.ny:
        p.notes.append("%d Y points, %d declared" % (len(p.y), p.ny))
    if p.x is not None and p.kind in ("curve", "map") and len(p.x) != p.nx:
        p.notes.append("%d X points, %d declared" % (len(p.x), p.nx))
    if len(p.z) != p.nx * p.ny:
        p.notes.append("%d values, %dx%d declared" % (len(p.z), p.nx, p.ny))


# ------------------------------------------------------------------ writing
def _fmt(v) -> str:
    if isinstance(v, str):
        return '"%s"' % v.replace('"', '""')
    f = float(v)
    if f != f:
        return "0"
    if f.is_integer() and abs(f) < 1e15:
        return "%d" % f
    s = repr(f)                 # the shortest form that reads back the same
    if "e" in s and 1e-12 <= abs(f) < 1e15:
        from decimal import Decimal
        s = format(Decimal(s), "f")
    return s


def _rows(key: str, vals: list, out: List[str]) -> None:
    """``vals`` as lines of ``key``; numbers and texts each on their own
    keyword (``WERT``/``TEXT``, ``ST/X``/``ST_TX/X``)."""
    if not vals:
        return
    texts = any(isinstance(v, str) for v in vals)
    if texts:
        key = {"WERT": "TEXT", "ST/X": "ST_TX/X", "ST/Y": "ST_TX/Y"}.get(key, key)
        vals = [v if isinstance(v, str) else _fmt(v) for v in vals]
    for i in range(0, len(vals), PER_LINE):
        out.append("   %-8s %s" % (key, "   ".join(_fmt(v) for v in vals[i:i + PER_LINE])))


def _keyword(p: Param) -> str:
    if p.kind == "value":
        return "FESTWERT"
    if p.kind == "block":
        return "FESTWERTEBLOCK"
    if p.kind == "axis":
        return "STUETZSTELLENVERTEILUNG"
    if p.kind == "text":
        return "TEXTSTRING"
    kinds = {p.x_kind} | ({p.y_kind} if p.kind == "map" else set())
    stem = "KENNLINIE" if p.kind == "curve" else "KENNFELD"
    if "com" in kinds:
        return "GRUPPEN" + stem
    if kinds == {"fix"}:
        return "FEST" + stem
    return stem


def dumps(vs: ValueSet, source: str = "") -> bytes:
    from .. import branding
    out = ['* encoding="ISO-8859-1"',
           "* DAMOS-Austauschdatei",
           "* Erstellt mit %s %s" % (branding.APP_NAME, branding.__version__)]
    if source:
        out.append("* Quelle: %s" % source)
    if vs.title:
        out.append("* Datensatz: %s" % vs.title)
    out += ["", "KONSERVIERUNG_FORMAT 2.0", ""]
    fkts = list(vs.functions)
    have = {f[0] for f in fkts}
    for p in vs.params:
        if p.function and p.function not in have:
            have.add(p.function)
            fkts.append((p.function, "", ""))
    if fkts:
        out.append("FUNKTIONEN")
        for name, ver, desc in fkts:
            out.append('   FKT %s "%s" "%s"' % (name, ver.replace('"', "'"), desc.replace('"', "'")))
        out += ["END", ""]
    skipped = []
    for p in vs.params:
        if p.kind == "cuboid":
            skipped.append(p.name)
            continue
        kw = _keyword(p)
        name = p.name if re.match(r"^[^\s\"]+$", p.name) else ident(p.name)
        if p.kind == "block":
            head = "%s %s %d" % (kw, name, p.nx) + (" @ %d" % p.ny if p.ny > 1 else "")
        elif p.kind in ("curve", "axis"):
            head = "%s %s %d" % (kw, name, p.nx)
        elif p.kind == "map":
            head = "%s %s %d %d" % (kw, name, p.nx, p.ny)
        else:
            head = "%s %s" % (kw, name)
        out.append(head)
        if p.description:
            out.append('   LANGNAME "%s"' % p.description.replace('"', "'"))
        if p.display:
            out.append("   DISPLAYNAME %s" % p.display)
        if p.function:
            out.append("   FUNKTION %s" % p.function)
        if p.kind == "axis":
            if p.unit:
                out.append('   EINHEIT_X "%s"' % p.unit)
            _rows("ST/X", p.z, out)
            out += ["END", ""]
            continue
        if p.kind in ("curve", "map"):
            out.append('   EINHEIT_X "%s"' % p.unit_x)
        if p.kind == "map":
            out.append('   EINHEIT_Y "%s"' % p.unit_y)
        if p.kind != "text":
            out.append('   EINHEIT_W "%s"' % p.unit)
        if p.kind == "text":
            out.append('   TEXT "%s"' % str(p.z[0] if p.z else "").replace('"', "'"))
        elif p.kind in ("value",):
            _rows("WERT", p.z[:1], out)
        elif p.kind == "block":
            for r in range(p.ny):
                _rows("WERT", p.z[r * p.nx:(r + 1) * p.nx], out)
        elif p.kind == "curve":
            if p.x_ref:
                out.append("*SSTX\t%s" % ident(p.x_ref))
            _rows("ST/X", p.x or [], out)
            _rows("WERT", p.z, out)
        else:
            if p.x_ref:
                out.append("*SSTX\t%s" % ident(p.x_ref))
            if p.y_ref:
                out.append("*SSTY\t%s" % ident(p.y_ref))
            _rows("ST/X", p.x or [], out)
            ys = p.y or []
            for r in range(p.ny):
                if r < len(ys):
                    _rows("ST/Y", [ys[r]], out)
                _rows("WERT", p.z[r * p.nx:(r + 1) * p.nx], out)
        out += ["END", ""]
    if skipped:
        vs.warnings.append("Three-dimensional tables have no form in a DCM -- left "
                           "out: %s" % ", ".join(skipped))
    return ("\r\n".join(out) + "\r\n").encode("latin-1", "replace")
