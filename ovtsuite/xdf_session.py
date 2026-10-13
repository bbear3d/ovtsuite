# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A packaged definition gives an unrecognised binary a parameter tree.

An .ovt project can carry a third-party definition with a raw binary
(docs/OVT_CONTAINER.md section 3). With one, the binary opens in the main
view -- tree, tables, comparison, patches -- instead of only the hex editor:
:func:`apply_definition` turns the definition's tables and constants into the
session's entities, read straight from the definition's own fields (address,
size, sign, byte order, storage order, conversion), never guessed.

Every format :mod:`ovtsuite.definitions` reads works here: TunerPro XDF,
ASAP2 (A2L), WinOLS map packs (KP) and projects (OLS), Bosch DAMOS (DAM).
"""

from __future__ import annotations

from typing import List, Optional

from .definitions import DefinitionError, load as _load_definition
from .definitions.model import AxisDef, Definition, Storage
from .groups import DEFINITION
from .model import Axis, Entity
from .signal_names import english_units, phrase_english

#: definition types :func:`apply_definition` understands
SUPPORTED = ("xdf", "a2l", "kp", "ols", "dam")


class Unsupported(ValueError):
    """The definition type does not build a parameter tree."""


def _axis(ad: Optional[AxisDef], base: int, count: int) -> Optional[Axis]:
    if ad is None:
        return None
    st = ad.values
    if st is not None and st.addr is not None:
        if st.bits > 32 or (st.float and st.bits != 32):
            return None
        return Axis(addr=st.addr - base, count=count, bits=st.bits,
                    is_float=st.float, equation=st.equation_text(),
                    units=english_units(st.units),
                    signed=None if st.float or st.signed is None else st.signed,
                    labels=ad.labels)
    if ad.fixed:
        vals = list(ad.fixed[:count])
        return Axis(addr=None, count=len(vals), values=vals,
                    units=english_units(st.units) if st else "",
                    labels=ad.labels)
    return None


def _english(text: str) -> str:
    """``main | Z: value`` with each part the phrase table knows in its
    curated English, the German main text kept after ``DE:``."""
    parts = [p.strip() for p in (text or "").split(" | ")]
    out = []
    for i, p in enumerate(parts):
        tag = p[:3] if p[:3] in ("X: ", "Y: ", "Z: ") else ""
        out.append(tag + _axis_name(p[len(tag):]))
    if out and out[0] != parts[0]:
        out.append("DE: " + parts[0])
    return " | ".join(out)


def _axis_name(name: str) -> str:
    """A known German text in English: "Umrechnung Drehzahl" -> "Conversion:
    engine speed", "dzmNmit (Drehzahl)" -> "engine speed (dzmNmit)"."""
    import re
    en = phrase_english(name)
    if en:
        return en
    m = re.match(r"^(\S+) \((.*)\)$", name)
    if m:
        en = phrase_english(m.group(2))
        if en:
            return "%s (%s)" % (en, m.group(1))
    return name


def category_text(cat: str) -> str:
    """A definition's category in English. WinOLS and DAMOS categories are
    a function's code and its German title -- ``ARMD: 10.40  Momentenbasierte
    Antiruckelfunktion``, ``AirCtl_Governor: "Adaptiver Abgasrückführregler"``
    -- shown as ``Torque-based anti-jerk function (ARMD 10.40)``; anything
    else is translated as it stands."""
    import re
    from .signal_names import translate_german
    cat = " ".join((cat or "").split())
    m = re.match(r'^([A-Za-z0-9_]+):\s*(?:(\d+(?:\.\d+)*)\s+)?"?(.*?)"?$', cat)
    if m and m.group(3):
        code, ver, title = m.group(1), m.group(2), m.group(3).strip()
        en = phrase_english(title) or translate_german(title)
        return "%s (%s)" % (en[:1].upper() + en[1:], code + (" " + ver if ver else ""))
    en = phrase_english(cat) or translate_german(cat)
    return en[:1].upper() + en[1:] if en else cat


def entities(defn, binfile) -> List[Entity]:
    """Entities for every table and constant of ``defn`` (a
    :class:`~ovtsuite.definitions.model.Definition`, or a parsed XDF),
    reading ``binfile``. A table outside the binary, or stored in the other
    byte order than the session reads, is left out (and counted in
    ``defn.warnings``)."""
    if not isinstance(defn, Definition):
        from .definitions import from_xdf
        defn = from_xdf(defn)
    data = binfile.data
    size = len(data)
    base = defn.base_in(data)
    order = getattr(binfile, "endian", ">")

    def read(addr, n, signed, endian):
        a = addr - base
        if a < 0 or a + n > size:
            return None
        return int.from_bytes(bytes(data[a:a + n]),
                              "little" if endian == "<" else "big", signed=signed)

    out: List[Entity] = []
    outside = other_order = 0
    for t in defn.tables:
        if t.dynamic is not None:
            try:
                t = t.dynamic(read)
            except Exception:
                t = None
            if t is None:
                outside += 1
                continue
        z: Storage = t.z
        if z.addr is None or z.bits > 32 or (z.float and z.bits != 32):
            continue
        if z.bits > 8 and z.endian and z.endian != order:
            other_order += 1
            continue
        x = _axis(t.x, base, t.cols) if t.kind != "constant" else None
        y = _axis(t.y, base, t.rows) if t.kind == "map" else None
        kind = t.kind
        if kind == "curve" and x is None and not t.text_kind:
            kind = "indexed"
        rows = t.rows if kind in ("map", "indexed") else 1
        # A description the phrase table knows (every EDC15 DAMOS text) is
        # shown in its curated English, the German kept after "DE:"; any
        # other is translated word by word where it is shown.
        desc = _english(t.description)
        # what each axis is, in the "main | X: ... | Y: ..." form the
        # parameter panel lays out
        for tag, ax, a in (("X", x, t.x), ("Y", y, t.y)):
            if ax is not None and a is not None and a.name:
                desc += " | %s: %s" % (tag, _axis_name(a.name))
        e = Entity(
            name=t.name, kind=kind, tier=DEFINITION,
            categories=list(t.categories), description=desc,
            units=english_units(z.units), addr=z.addr - base,
            rows=max(1, rows),
            cols=max(1, t.cols), bits=z.bits, signed=z.signed and not z.float,
            equation=z.equation_text(), source="definition", x=x, y=y,
            binfile=binfile, declared_rows=max(1, rows),
            declared_cols=max(1, t.cols),
            order="col" if t.order == "col" else "row",
            text_kind=t.text_kind)
        e.is_float = z.float
        if t.kind == "constant":
            e.rows = e.cols = 1
        e.category_path = [category_text(t.categories[0])] if t.categories \
            else ["Values" if t.kind == "constant" else "Tables"]
        if e.addr < 0 or e.end > size:
            outside += 1
            continue                    # outside this binary: not this file's
        out.append(e)
    defn._skipped = (outside, other_order)
    return out


def apply_definition(session, data: bytes, type_: str) -> int:
    """Give ``session`` (an unrecognised binary) the parameters of the
    definition ``data`` of type ``type_``. Returns how many there are."""
    type_ = (type_ or "").lower().lstrip(".")
    if type_ and type_ not in SUPPORTED:
        raise Unsupported("%s definitions do not build a parameter tree"
                          % type_.upper())
    try:
        defn = _load_definition(data, type_ or None)
    except DefinitionError as ex:
        raise Unsupported(str(ex))
    session.binfile.endian = defn.byte_order()
    ents = entities(defn, session.binfile)
    session.entities = ents
    session.by_key = {("definition", e.name, e.addr): e for e in ents}
    session._variant_entities = []      # no coding-variant tables to find
    session.support = "definition"
    session.family = defn.title or "definition"
    session.unsupported_reason = None
    session.definition_notes = describe(defn, len(ents))
    session.has_definition_file = bool(ents)
    return len(ents)


def add_definition(session, data: bytes, type_: str, label: str = "") -> int:
    """Add the tables and values of the definition ``data`` to a session a
    module already defines, beside the module's own -- under their own group
    (``From <label>``), never replacing or renaming the module's. A table at
    the same address and size as one of the module's is still listed: the
    two describe the same bytes, so an edit in either shows in both.
    Returns how many were added. The module's definition snapshot leaves
    them out (``source == "definition"``); they are added again on open."""
    type_ = (type_ or "").lower().lstrip(".")
    if type_ and type_ not in SUPPORTED:
        raise Unsupported("%s definitions do not build a parameter tree"
                          % type_.upper())
    try:
        defn = _load_definition(data, type_ or None)
    except DefinitionError as ex:
        raise Unsupported(str(ex))
    ents = entities(defn, session.binfile)
    head = "From %s" % (label or defn.title or type_.upper() or "definition")
    for e in ents:
        e.category_path = [head] + list(e.category_path or [])
    drop_added_definition(session)
    session.entities = list(session.entities) + ents
    for e in ents:
        session.by_key[("definition", e.name, e.addr)] = e
    session.definition_notes = describe(defn, len(ents))
    session.has_definition_file = bool(ents)
    return len(ents)


def drop_added_definition(session) -> None:
    """Take out what :func:`add_definition` added (before adding again)."""
    keep = [e for e in session.entities if getattr(e, "source", "") != "definition"]
    if len(keep) != len(session.entities):
        session.entities = keep
        session.by_key = {k: v for k, v in session.by_key.items()
                          if getattr(v, "source", "") != "definition"}


def describe(defn: Definition, n: int) -> str:
    """One line on what a definition gave: how many parameters, and what it
    left out and why."""
    outside, other = getattr(defn, "_skipped", (0, 0))
    bits = ["%d parameters from the %s definition" % (n, defn.type.upper())]
    if outside:
        bits.append("%d outside this binary" % outside)
    if other:
        bits.append("%d stored in the other byte order" % other)
    return "; ".join(bits)


__all__ = ["SUPPORTED", "Unsupported", "entities", "apply_definition",
           "add_definition", "drop_added_definition", "describe"]
