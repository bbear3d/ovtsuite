# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Export a session's identified entities as a TunerPro 1.80 XDF.

The exported definition is anchored to the loaded (primary) binary's own
addresses, uses the STORED dimensions (so no reserved-max garbage rows --
the audit's 'oversized' fix), and organizes the tree by tuning tier first,
then A2L functional category:

    <tier> / <category> / <entity>

TunerPro is an ANSI application: a Latin-1 character (an umlaut, a degree
sign) is written as a character reference (``&#228;``), as TunerPro writes them; the few others are transliterated.

Each value carries its display precision (``decimalpl`` and ``outputtype``:
the definition's decimal places, else as many as the factor needs), its
physical range (``min`` / ``max``: the stored width through the conversion)
and its signedness (``datatype`` 1 = signed).
"""

import math

from typing import List, Optional

from .model import Entity
from .tiers import tier_name, TIER_NAMES
from . import branding

#: characters outside Latin-1, which an ANSI application cannot show
_ASCII = str.maketrans({
    "—": "-", "–": "-", "→": "->", "…": "...", "‘": "'", "’": "'",
    "“": '"', "”": '"', "≤": "<=", "≥": ">=", "Ω": "Ohm",
})


def _esc(s):
    s = str(s).translate(_ASCII)
    s = (s.replace("&", "&amp;").replace("<", "&lt;")
          .replace(">", "&gt;").replace('"', "&quot;"))
    out = []
    for ch in s:
        o = ord(ch)
        if o < 32 and ch not in "\t\n\r":
            continue                        # not allowed in XML 1.0
        out.append(ch if o < 128 else ("&#%d;" % o if o < 256 else "?"))
    return "".join(out)


def _g(v: float) -> str:
    """A number the way the XDF fields hold it: up to 6 significant digits."""
    if not math.isfinite(v):
        return "0"
    t = "%.6g" % v
    return "0" if t in ("-0", "0") else t


def _decimals(obj) -> int:
    """Display decimals: the definition's own, else what the conversion
    factor needs (0.01 -> 2), else 0."""
    d = getattr(obj, "decimals", None)
    if d is not None:
        return max(0, min(15, int(d)))
    try:
        eq = obj.eq
        f = abs(eq.factor) if eq.linear else 0.0
    except Exception:
        f = 0.0
    if 0 < f < 1:
        return max(0, min(15, -int(math.floor(math.log10(f) + 1e-9))))
    return 0


def _range(obj, bits: int, signed: bool):
    """The physical values the stored width can hold, low to high."""
    lo, hi = ((-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed
              else (0, (1 << bits) - 1))
    try:
        a, b = obj.eq.decode(lo), obj.eq.decode(hi)
        a, b = float(a), float(b)
    except Exception:
        a, b = float(lo), float(hi)
    return min(a, b), max(a, b)


def _display_xml(obj, bits: int, signed: bool, indent: str = "      ",
                 is_float: bool = False) -> str:
    """``decimalpl``, ``min``, ``max``, ``outputtype`` and ``datatype``."""
    dec = _decimals(obj)
    out = "%s<decimalpl>%d</decimalpl>\n" % (indent, dec)
    if not is_float:
        lo, hi = _range(obj, bits, signed)
        out += "%s<min>%s</min>\n%s<max>%s</max>\n" % (indent, _g(lo), indent, _g(hi))
    out += "%s<outputtype>%d</outputtype>\n" % (indent, 1 if dec > 0 or is_float else 2)
    out += "%s<datatype>%d</datatype>\n" % (indent, 1 if signed else 0)
    return out


def _title(e) -> str:
    return getattr(e, "xdf_title", None) or e.title


def _description(e) -> str:
    d = getattr(e, "xdf_description", None)
    return e.description if d is None else d


def _typeflags(e: Entity, le: bool = False):
    # TunerPro mmedtypeflags: bit0 (0x01) = signed, bit1 (0x02) = LSB-first
    # (little-endian). Big-endian data must NOT set bit1 -- the old code
    # returned 0x02 for an unsigned big-endian value, which silently flagged
    # every unsigned EDC16 parameter as little-endian (byte-swapped in TunerPro).
    flags = 0
    if getattr(e, "is_float", False):
        # IEEE-754 cells: TunerPro's float flag (0x10000), as WinOLS and
        # TunerPro write it; the cells were read as integers before
        return "0x%X" % (0x10000 | (0x02 if le else 0))
    if e.signed:
        flags |= 0x01
    if le:
        flags |= 0x02
    return "0x%02X" % flags


def _axis_flags(is_float: bool, le: bool, signed: bool = False) -> str:
    if is_float:
        # 0x10000 is TunerPro's float flag; 0x04 (written here before) is
        # its column-major flag
        return "0x%X" % (0x10000 | (0x02 if le else 0))
    flags = 0
    if signed:
        flags |= 0x01
    if le:
        flags |= 0x02
    return "0x%02X" % flags


def _axis_xml(aid, addr, count, bits, equation, units, is_float, le=False,
              signed=False, ax=None):
    if addr is None:
        return (
            '    <XDFAXIS id="%s" uniqueid="0x0">\n'
            '      <EMBEDDEDDATA mmedelementsizebits="8" '
            'mmedmajorstridebits="-32" mmedminorstridebits="0" />\n'
            '      <indexcount>1</indexcount>\n'
            '      <datatype>0</datatype>\n'
            '      <unittype>0</unittype>\n'
            '      <DALINK index="0" />\n'
            '      <LABEL index="0" value="0.0" />\n'
            '      <MATH equation="X"><VAR id="X" /></MATH>\n'
            '    </XDFAXIS>\n' % aid)
    flag = _axis_flags(is_float, le, signed)
    return (
        '    <XDFAXIS id="%s" uniqueid="0x0">\n'
        '      <EMBEDDEDDATA mmedtypeflags="%s" mmedaddress="0x%X" '
        'mmedelementsizebits="%d" mmedcolcount="%d" '
        'mmedmajorstridebits="0" mmedminorstridebits="0" />\n'
        '      <units>%s</units>\n'
        '      <indexcount>%d</indexcount>\n'
        '      <embedinfo type="1" />\n'
        '%s'
        '      <unittype>0</unittype>\n'
        '      <DALINK index="0" />\n'
        '      <MATH equation="%s"><VAR id="X" /></MATH>\n'
        '    </XDFAXIS>\n' % (
            aid, flag, addr, bits, count, _esc(units), count,
            (_display_xml(ax, bits, signed, is_float=is_float) if ax is not None
             else '      <datatype>%d</datatype>\n' % (3 if is_float else 0)),
            _esc(equation)))


def _fixed_axis_xml(aid, ax, count) -> Optional[str]:
    """An axis a definition gives as values or texts (not stored in the
    image): one LABEL per point, as TunerPro writes such an axis."""
    labels = list(getattr(ax, "labels", None) or [])
    vals = list(getattr(ax, "values", None) or [])
    pts = labels if len(labels) == count else (
        ["%g" % float(v) for v in vals] if len(vals) == count else None)
    if not pts or count < 2:
        return None
    out = ['    <XDFAXIS id="%s" uniqueid="0x0">\n' % aid,
           '      <EMBEDDEDDATA mmedelementsizebits="8" '
           'mmedmajorstridebits="-32" mmedminorstridebits="0" />\n',
           '      <units>%s</units>\n' % _esc(getattr(ax, "units", "") or ""),
           '      <indexcount>%d</indexcount>\n' % count,
           '      <datatype>0</datatype>\n',
           '      <unittype>0</unittype>\n',
           '      <DALINK index="0" />\n']
    for i, p in enumerate(pts):
        out.append('      <LABEL index="%d" value="%s" />\n' % (i, _esc(p)))
    out.append('      <MATH equation="X"><VAR id="X" /></MATH>\n'
               '    </XDFAXIS>\n')
    return "".join(out)


def _axis_signed(e, ax) -> bool:
    """An axis is exported signed when its values only read monotonic that
    way (the same rule ``Entity.axis_values`` applies). Without an image
    (a conversion), what the definition states."""
    if getattr(getattr(e, "binfile", None), "data", None) is None:
        return bool(getattr(ax, "signed", False))
    try:
        if ax.is_float or ax.addr is None:
            return False
        bf = e.binfile
        un = bf.read_many(ax.addr, ax.count, ax.bits, False)
        if len(un) < 2:
            return False
        mono = (all(a < b for a, b in zip(un, un[1:]))
                or all(a > b for a, b in zip(un, un[1:])))
        return not mono
    except Exception:
        return False


def _is_unpopulated(e) -> bool:
    """True if this parameter lands in flash-erased (0xFF) space, i.e. it is
    not present in this calibration variant.

    Tables: skipped when their whole data span is 0xFF. Constants: skipped only
    when the value AND a small neighbourhood are all 0xFF - a lone 0xFFFF among
    real data is kept, since that is a legitimate max-limit value."""
    try:
        data = e.binfile.data
        kind = getattr(e, "kind", None)
        if kind in ("map", "curve"):
            end = e.addr + e.n_cells * e.step
            chunk = bytes(data[e.addr:end])
            return bool(chunk) and all(b == 0xFF for b in chunk)
        if kind == "constant":
            lo = max(0, e.addr - 12)
            hi = min(len(data), e.addr + e.step + 12)
            chunk = bytes(data[lo:hi])
            return bool(chunk) and all(b == 0xFF for b in chunk)
    except Exception:
        return False
    return False


def _zf_z_math(entity):
    """Per-column Z equations, empty for every platform but ZF 6HP."""
    try:
        from .speedview import z_math
        return z_math(entity)
    except Exception:
        return []


def _checksum_xml(c: dict, uid: int) -> str:
    """One XDFCHECKSUM block: as read (``raw``), else from its fields."""
    if c.get("raw"):
        return "  " + c["raw"].strip() + "\n"
    out = ['  <XDFCHECKSUM uniqueid="0x%X">\n' % uid,
           '    <title>%s</title>\n' % _esc(c.get("title", "Checksum")),
           '    <REGION>\n']
    for k in ("pluginmoduleid", "datastart", "dataend", "storesizebits",
              "storeaddress", "calculationmethod"):
        if c.get(k) not in (None, ""):
            out.append('      <%s>%s</%s>\n' % (k, _esc(c[k]), k))
    out.append('    </REGION>\n  </XDFCHECKSUM>\n')
    return "".join(out)


def export_xdf(entities: List[Entity], title: str, fileversion: str = "",
               image_size: int = 0x200000, little_endian: bool = None,
               platform: str = "edc16", tiers: bool = True,
               description: Optional[str] = None,
               checksums: Optional[List[dict]] = None,
               include_indexed: bool = False,
               patches: Optional[List[dict]] = None) -> str:
    """``little_endian`` defaults to the entities' binary byte order (EDC15
    images are little-endian); ``platform`` selects the header text and
    whether the EDC16 checksum-plugin block is emitted.

    For a conversion (:mod:`ovtsuite.convert`): ``tiers=False`` keeps the
    definition's own categories only, ``description`` replaces the header
    text, and ``checksums`` (a list, possibly empty) replaces the platform's
    checksum block with exactly those blocks, and ``include_indexed`` writes
    value blocks without an axis as tables (the session leaves them out).
    ``patches`` adds TunerPro XDFPATCH blocks (:attr:`Definition.patches`
    form, addresses as image offsets)."""
    if little_endian is None:
        little_endian = any(getattr(e.binfile, "endian", ">") == "<"
                            for e in entities[:1])
    le = little_endian
    # collect categories: tier names + every A2L category used
    cats: List[str] = []
    cat_index = {}

    def cat_id(name):
        if name not in cat_index:
            cat_index[name] = len(cats)
            cats.append(name)
        return cat_index[name]

    if tiers:
        for tier in sorted({e.tier for e in entities}):
            cat_id(tier_name(tier))
    for e in entities:
        for c in e.categories:
            cat_id(c)
    for p in patches or ():
        for c in p.get("categories") or ():
            cat_id(c)

    out = []
    out.append('<!-- %s XDF export -->\n' % branding.APP_NAME)
    out.append('<XDFFORMAT version="1.80">\n')
    out.append('  <XDFHEADER>\n')
    out.append('    <flags>0x1</flags>\n')
    out.append('    <fileversion>%s</fileversion>\n' % _esc(fileversion))
    out.append('    <deftitle>%s</deftitle>\n' % _esc(title))
    if description is not None:
        out.append('    <description>%s</description>\n' % _esc(description))
    else:
        out.append('    <description>Generated by ' + branding.APP_NAME +
                   ' from the primary '
                   'binary (%s)</description>\n' % _esc(platform.upper()))
    out.append('    <BASEOFFSET offset="0" subtract="0" />\n')
    out.append('    <DEFAULTS datasizeinbits="8" sigdigits="3" '
               'outputtype="2" signed="0" lsbfirst="%d" float="0" />\n'
               % (1 if le else 0))
    out.append('    <REGION type="0xFFFFFFFF" startaddress="0x0" ' +
               ('size="0x%X" regioncolor="0x0" regionflags="0x0" ' % image_size) +
               'name="Binary File" desc="" />\n')
    for i, name in enumerate(cats):
        out.append('    <CATEGORY index="0x%X" name="%s" />\n'
                   % (i, _esc(name)))
    out.append('  </XDFHEADER>\n')

    # Flash-checksum region -> the Bosch EDC16 TunerPro plugin, so TunerPro
    # auto-corrects both checksums whenever the tune is saved. The plugin
    # handles 2 MB images only; for 1 MB EDC16U1 the block is omitted (this
    # app still corrects on save). dataend is the last byte address.
    if checksums is not None:
      for i, c in enumerate(checksums):
        out.append(_checksum_xml(c, 0x7F0 + i))
    elif image_size == 0x200000 and platform == "edc16":
      out.append('  <XDFCHECKSUM uniqueid="0x1">\n')
      out.append('    <title>EDC16 Checksums</title>\n')
      out.append('    <REGION>\n')
      out.append('      <pluginmoduleid>b3c7a1d2-4f8e-4a3b-9c6d-1e2f3a4b5c6d'
               '</pluginmoduleid>\n')
      out.append('      <datastart>0x0</datastart>\n')
      out.append('      <dataend>0x1FFFFF</dataend>\n')
      out.append('      <storesizebits>0x20</storesizebits>\n')
      out.append('      <storeaddress>0x0</storeaddress>\n')
      out.append('      <calculationmethod>0x0</calculationmethod>\n')
      out.append('    </REGION>\n')
      out.append('  </XDFCHECKSUM>\n')

    uid = 0x1000
    for e in entities:
        if e.kind == "indexed" and not include_indexed:
            continue
        if platform == "edc16" and _is_unpopulated(e):
            # (EDC15 datasets use 0xC3 filler; an all-0xFFFF table there is a
            # legitimate "never" limit, so the filter is EDC16-only)
            continue
        uid += 1
        catmems = ['      <CATEGORYMEM index="0" category="%d" />'
                   % (cat_id(tier_name(e.tier)) + 1)] if tiers else []
        for depth, c in enumerate(e.categories, start=len(catmems)):
            catmems.append('      <CATEGORYMEM index="%d" category="%d" />'
                           % (depth, cat_id(c) + 1))
        catmem = "\n".join(catmems)
        isf = bool(getattr(e, "is_float", False))

        if e.kind == "constant":
            out.append(
                '  <XDFCONSTANT uniqueid="0x%X">\n'
                '    <title>%s</title>\n'
                '    <description>%s</description>\n'
                '%s\n'
                '    <EMBEDDEDDATA mmedtypeflags="%s" mmedaddress="0x%X" '
                'mmedelementsizebits="%d" mmedmajorstridebits="0" '
                'mmedminorstridebits="0" />\n'
                '    <units>%s</units>\n'
                '%s'
                '    <unittype>0</unittype>\n'
                '    <DALINK index="0" />\n'
                '    <MATH equation="%s"><VAR id="X" /></MATH>\n'
                '  </XDFCONSTANT>\n' % (
                    uid, _esc(_title(e)), _esc(_description(e)), catmem,
                    _typeflags(e, le), e.addr, e.bits, _esc(e.units),
                    _display_xml(e, e.bits, bool(e.signed), "    ", isf),
                    _esc(e.equation)))
            continue

        # table (map or curve)
        out.append('  <XDFTABLE uniqueid="0x%X" flags="0x30">\n' % uid)
        out.append('    <title>%s</title>\n' % _esc(_title(e)))
        out.append('    <description>%s</description>\n' % _esc(_description(e)))
        out.append(catmem + "\n")
        # X axis (columns)
        if e.x is not None and e.x.addr is not None:
            out.append(_axis_xml("x", e.x.addr, e.cols, e.x.bits,
                                 e.x.equation, e.x.units, e.x.is_float, le,
                                 _axis_signed(e, e.x), e.x))
        else:
            out.append(_fixed_axis_xml("x", e.x, e.cols)
                       or _axis_xml("x", None, e.cols, 16, "X", "", False))
        # Y axis (rows)
        if e.y is not None and e.y.addr is not None:
            out.append(_axis_xml("y", e.y.addr, e.rows, e.y.bits,
                                 e.y.equation, e.y.units, e.y.is_float, le,
                                 _axis_signed(e, e.y), e.y))
        else:
            out.append(_fixed_axis_xml("y", e.y, e.rows)
                       or _axis_xml("y", None, e.rows, 16, "X", "", False))
        # Z data -- EDC16 stores maps column-major (major stride = one
        # element), EDC15 first-axis-major = plain row-major (stride 0)
        colmajor = (e.kind == "map" and getattr(e, "order", "col") == "col")
        out.append(
            '    <XDFAXIS id="z">\n'
            '      <EMBEDDEDDATA mmedtypeflags="%s" mmedaddress="0x%X" '
            'mmedelementsizebits="%d" mmedrowcount="%d" mmedcolcount="%d" '
            'mmedmajorstridebits="%d" mmedminorstridebits="0" />\n'
            '      <units>%s</units>\n'
            '%s'
            '      <MATH equation="%s"><VAR id="X" /></MATH>\n' % (
                _typeflags(e, le), e.addr, e.bits, e.rows, e.cols,
                # column-major: minor stride down a column, major across rows
                e.bits if colmajor else 0, _esc(e.units),
                _display_xml(e, e.bits, bool(e.signed),
                             is_float=bool(getattr(e, "is_float", False))),
                _esc(e.equation)))
        # A ZF shift, TCC or tip-limit map stores output-shaft speed, and each
        # column converts to engine RPM by its own gear ratio. TunerPro takes
        # one MATH per column, which is how the source definitions express it
        # too, so an export from here reads the same way there.
        for col, eq in _zf_z_math(e):
            out.append('      <MATH col="%d" equation="%s"><VAR id="X" /></MATH>\n'
                       % (col, _esc(eq)))
        out.append('    </XDFAXIS>\n  </XDFTABLE>\n')
    for i, p in enumerate(patches or ()):
        out.append(_patch_xml(p, 0x7000 + i, [cat_id(c) + 1 for c in p.get("categories") or ()]))
    out.append('</XDFFORMAT>\n')
    return "".join(out)


def _patch_xml(p: dict, uid: int, cats: List[int]) -> str:
    """One TunerPro XDFPATCH: its entries' new bytes and, when known, the
    bytes they replace (so TunerPro can tell it is applied and remove it)."""
    out = ['  <XDFPATCH uniqueid="0x%X">\n' % uid,
           '    <title>%s</title>\n' % _esc(p.get("title") or "Patch")]
    if p.get("description"):
        out.append('    <description>%s</description>\n' % _esc(p["description"]))
    for k, c in enumerate(cats):
        out.append('    <CATEGORYMEM index="%d" category="%d" />\n' % (k, c))
    for e in p.get("entries") or ():
        data = bytes(e["patch"])
        base = e.get("base")
        out.append('    <XDFPATCHENTRY name="%s" address="0x%X" datasize="0x%X" '
                   'patchdata="%s"%s />\n' % (
                       _esc(str(e.get("name", ""))), e["address"], len(data),
                       data.hex().upper(),
                       ' basedata="%s"' % bytes(base).hex().upper() if base else ""))
    out.append('  </XDFPATCH>\n')
    return "".join(out)
