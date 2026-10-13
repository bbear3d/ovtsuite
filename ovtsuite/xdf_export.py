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

TunerPro is an ANSI application, so non-Latin-1 characters are transliterated.
"""

from typing import List

from .model import Entity
from .tiers import tier_name, TIER_NAMES
from . import branding

_ASCII = str.maketrans({
    "—": "-", "–": "-", "·": "-", "→": "->",
    "×": "x", "…": "...", "°": "deg", "µ": "u",
})


def _esc(s):
    s = str(s).translate(_ASCII).encode("ascii", "replace").decode("ascii")
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _typeflags(e: Entity, le: bool = False):
    # TunerPro mmedtypeflags: bit0 (0x01) = signed, bit1 (0x02) = LSB-first
    # (little-endian). Big-endian data must NOT set bit1 -- the old code
    # returned 0x02 for an unsigned big-endian value, which silently flagged
    # every unsigned EDC16 parameter as little-endian (byte-swapped in TunerPro).
    flags = 0
    if e.signed:
        flags |= 0x01
    if le:
        flags |= 0x02
    return "0x%02X" % flags


def _axis_flags(is_float: bool, le: bool, signed: bool = False) -> str:
    if is_float:
        return "0x04"
    flags = 0
    if signed:
        flags |= 0x01
    if le:
        flags |= 0x02
    return "0x%02X" % flags


def _axis_xml(aid, addr, count, bits, equation, units, is_float, le=False,
              signed=False):
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
        '      <datatype>%d</datatype>\n'
        '      <unittype>0</unittype>\n'
        '      <DALINK index="0" />\n'
        '      <MATH equation="%s"><VAR id="X" /></MATH>\n'
        '    </XDFAXIS>\n' % (
            aid, flag, addr, bits, count, _esc(units), count,
            3 if is_float else 0, _esc(equation)))


def _axis_signed(e, ax) -> bool:
    """An axis is exported signed when its values only read monotonic that
    way (the same rule ``Entity.axis_values`` applies)."""
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


def export_xdf(entities: List[Entity], title: str, fileversion: str = "",
               image_size: int = 0x200000, little_endian: bool = None,
               platform: str = "edc16") -> str:
    """``little_endian`` defaults to the entities' binary byte order (EDC15
    images are little-endian); ``platform`` selects the header text and
    whether the EDC16 checksum-plugin block is emitted."""
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

    for tier in sorted({e.tier for e in entities}):
        cat_id(tier_name(tier))
    for e in entities:
        for c in e.categories:
            cat_id(c)

    out = []
    out.append('<!-- %s XDF export -->\n' % branding.APP_NAME)
    out.append('<XDFFORMAT version="1.80">\n')
    out.append('  <XDFHEADER>\n')
    out.append('    <flags>0x1</flags>\n')
    out.append('    <fileversion>%s</fileversion>\n' % _esc(fileversion))
    out.append('    <deftitle>%s</deftitle>\n' % _esc(title))
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
    if image_size == 0x200000 and platform == "edc16":
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
        if e.kind == "indexed":
            continue
        if platform == "edc16" and _is_unpopulated(e):
            # (EDC15 datasets use 0xC3 filler; an all-0xFFFF table there is a
            # legitimate "never" limit, so the filter is EDC16-only)
            continue
        uid += 1
        catmems = ['      <CATEGORYMEM index="0" category="%d" />'
                   % (cat_id(tier_name(e.tier)) + 1)]
        for depth, c in enumerate(e.categories, start=1):
            catmems.append('      <CATEGORYMEM index="%d" category="%d" />'
                           % (depth, cat_id(c) + 1))
        catmem = "\n".join(catmems)
        dt = 1 if e.bits == 16 else (2 if e.bits == 32 else 0)

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
                '    <datatype>%d</datatype>\n'
                '    <unittype>0</unittype>\n'
                '    <DALINK index="0" />\n'
                '    <MATH equation="%s"><VAR id="X" /></MATH>\n'
                '  </XDFCONSTANT>\n' % (
                    uid, _esc(e.title), _esc(e.description), catmem,
                    _typeflags(e, le), e.addr, e.bits, _esc(e.units), dt,
                    _esc(e.equation)))
            continue

        # table (map or curve)
        out.append('  <XDFTABLE uniqueid="0x%X" flags="0x30">\n' % uid)
        out.append('    <title>%s</title>\n' % _esc(e.title))
        out.append('    <description>%s</description>\n' % _esc(e.description))
        out.append(catmem + "\n")
        # X axis (columns)
        if e.x is not None and e.x.addr is not None:
            out.append(_axis_xml("x", e.x.addr, e.cols, e.x.bits,
                                 e.x.equation, e.x.units, e.x.is_float, le,
                                 _axis_signed(e, e.x)))
        else:
            out.append(_axis_xml("x", None, e.cols, 16, "X", "", False))
        # Y axis (rows)
        if e.y is not None and e.y.addr is not None:
            out.append(_axis_xml("y", e.y.addr, e.rows, e.y.bits,
                                 e.y.equation, e.y.units, e.y.is_float, le,
                                 _axis_signed(e, e.y)))
        else:
            out.append(_axis_xml("y", None, e.rows, 16, "X", "", False))
        # Z data -- EDC16 stores maps column-major (major stride = one
        # element), EDC15 first-axis-major = plain row-major (stride 0)
        colmajor = (e.kind == "map" and getattr(e, "order", "col") == "col")
        out.append(
            '    <XDFAXIS id="z">\n'
            '      <EMBEDDEDDATA mmedtypeflags="%s" mmedaddress="0x%X" '
            'mmedelementsizebits="%d" mmedrowcount="%d" mmedcolcount="%d" '
            'mmedmajorstridebits="%d" mmedminorstridebits="0" />\n'
            '      <units>%s</units>\n'
            '      <outputtype>1</outputtype>\n'
            '      <MATH equation="%s"><VAR id="X" /></MATH>\n' % (
                _typeflags(e, le), e.addr, e.bits, e.rows, e.cols,
                # column-major: minor stride down a column, major across rows
                e.bits if colmajor else 0, _esc(e.units), _esc(e.equation)))
        # A ZF shift, TCC or tip-limit map stores output-shaft speed, and each
        # column converts to engine RPM by its own gear ratio. TunerPro takes
        # one MATH per column, which is how the source definitions express it
        # too, so an export from here reads the same way there.
        for col, eq in _zf_z_math(e):
            out.append('      <MATH col="%d" equation="%s"><VAR id="X" /></MATH>\n'
                       % (col, _esc(eq)))
        out.append('    </XDFAXIS>\n  </XDFTABLE>\n')
    out.append('</XDFFORMAT>\n')
    return "".join(out)
