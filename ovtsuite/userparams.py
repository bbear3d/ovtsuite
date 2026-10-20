# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Parameters the user defines themselves (Add parameter... in the tree).

A user parameter is a plain description -- a JSON-able dict, the *spec* --
of a table the definitions do not have: where its data is, its shape, how a
cell is stored and converted, and its axes. :func:`to_entity` turns a spec
into an ordinary :class:`~ovtsuite.model.Entity` (``source == "user"``,
group :data:`~ovtsuite.groups.USER`), so the table, undo, comparison,
patches and the hex editor treat it like any other parameter.

They are kept the way a packaged definition is: OUTSIDE the stored model.
A project keeps a set's specs beside its binaries
(:meth:`~ovtsuite.project.project.Project.user_params`) and they are added
again every time the set opens, after the module's model and any attached
definition (``gui_project._apply_definition``); the definition snapshot
(:func:`ovtsuite.defn.build`) leaves ``source == "user"`` entities out. A tab
that is not a project yet holds them on its session (``_user_params``) and
its first save as a project carries them over.

**Duplicate as user parameter** starts a spec from a detected parameter
(:func:`from_entity`): the copy can be changed freely and is kept with the
project like any user parameter, so re-matching or a module update never
overwrites it. Its spec names the parameter it stands in for
(``replaces``: symbol and EDC15 dataset) and, with ``hide``, the original
is hidden: :func:`apply` marks it (``_hidden_by``, never stored in a
snapshot) and puts the copy in its place in the tree, and the tree and
searches leave hidden parameters out (:func:`hidden`). Turning ``hide``
off, or deleting the copy, brings the original back.

No Qt here.
"""

from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional

from .equation import Equation
from .model import Axis, Entity

#: the provenance (``Entity.source``) of a user parameter
SOURCE = "user"
#: the format of the stored list (:func:`dumps` / :func:`loads`)
FORMAT = 1

KINDS = ("map", "curve", "constant")
BITS = (8, 16, 32)


def blank(kind: str = "map") -> dict:
    """A new spec with every field at its default."""
    return {"name": "", "title": "", "kind": kind, "addr": 0,
            "rows": 1 if kind != "map" else 8,
            "cols": 1 if kind == "constant" else 8,
            "bits": 16, "signed": False, "float": False, "order": "row",
            "equation": "X", "units": "", "description": "", "folder": "",
            "x": None, "y": None, "replaces": None, "hide": False}


def _axis_size(ax: dict) -> int:
    if ax.get("float"):
        return 4
    return max(1, int(ax.get("bits", 16)) // 8)


def _check_eq(text: str, what: str, errs: List[str]) -> None:
    try:
        Equation(text or "X")
    except Exception:
        errs.append("%s: the equation %r is not one this app reads (use X, "
                    "numbers and + - * / ( ))." % (what, text))


def normalise(spec: dict) -> dict:
    """``spec`` with its shape made consistent with its kind (a value is
    1 x 1 with no axes, a curve has one row and no Y axis), as a copy."""
    s = copy.deepcopy(spec)
    for k, v in blank(s.get("kind") or "map").items():
        s.setdefault(k, v)
    kind = s["kind"]
    s["rows"] = int(s.get("rows") or 1)
    s["cols"] = int(s.get("cols") or 1)
    s["addr"] = int(s.get("addr") or 0)
    s["bits"] = int(s.get("bits") or 16)
    if s.get("float"):
        s["bits"] = 32
        s["signed"] = False
    if kind == "constant":
        s["rows"] = s["cols"] = 1
        s["x"] = s["y"] = None
    elif kind == "curve":
        s["rows"] = 1
        s["y"] = None
    for tag in ("x", "y"):
        ax = s.get(tag)
        if ax and ax.get("float"):
            ax["bits"] = 32
    return s


def validate(spec: dict, size: int, taken=()) -> List[str]:
    """What is wrong with ``spec`` for an image of ``size`` bytes, as
    sentences (empty when it is fine). ``taken`` is the symbols already in
    use (a user parameter's name must be unique)."""
    errs: List[str] = []
    s = spec
    name = (s.get("name") or "").strip()
    if not name:
        errs.append("Give the parameter a symbol (name).")
    elif any(ch.isspace() for ch in name):
        errs.append("The symbol cannot contain spaces.")
    elif name in set(taken):
        errs.append("There is already a parameter called %s." % name)
    if s.get("kind") not in KINDS:
        errs.append("Kind must be a map, a curve or a value.")
    bits = int(s.get("bits") or 0)
    if bits not in BITS:
        errs.append("A cell is 8, 16 or 32 bits.")
    if s.get("float") and bits != 32:
        errs.append("A floating-point cell is 32 bits.")
    rows, cols = int(s.get("rows") or 0), int(s.get("cols") or 0)
    if rows < 1 or cols < 1 or rows > 1024 or cols > 1024:
        errs.append("Rows and columns are 1 to 1024.")
    addr = int(s.get("addr") or 0)
    n = max(1, rows) * max(1, cols) * max(1, bits // 8)
    if addr < 0 or addr + n > size:
        errs.append("The data (0x%X to 0x%X) is outside the binary (0x%X "
                    "bytes)." % (addr, addr + n, size))
    _check_eq(s.get("equation") or "X", "Data", errs)
    for tag, count in (("x", cols), ("y", rows)):
        ax = s.get(tag)
        if not ax:
            continue
        what = "%s axis" % tag.upper()
        if ax.get("values") is not None:
            vals = ax.get("values") or []
            if len(vals) != count:
                errs.append("%s: %d values given, the table has %d."
                            % (what, len(vals), count))
            if any(not isinstance(v, (int, float)) or math.isnan(v)
                   for v in vals):
                errs.append("%s: the values must be numbers." % what)
            continue
        ab = int(ax.get("bits") or 0)
        if ab not in BITS:
            errs.append("%s: a point is 8, 16 or 32 bits." % what)
            continue
        if ax.get("float") and ab != 32:
            errs.append("%s: a floating-point point is 32 bits." % what)
        aa = ax.get("addr")
        if aa is None:
            errs.append("%s: give its address or its values." % what)
            continue
        aa = int(aa)
        if aa < 0 or aa + count * _axis_size(ax) > size:
            errs.append("%s (0x%X, %d points) is outside the binary."
                        % (what, aa, count))
        _check_eq(ax.get("equation") or "X", what, errs)
    return errs


def _axis(ax: Optional[dict], count: int) -> Optional[Axis]:
    if not ax:
        return None
    if ax.get("values") is not None:
        vals = [float(v) for v in ax["values"]][:count]
        return Axis(addr=None, count=len(vals), values=vals,
                    units=ax.get("units") or "")
    return Axis(addr=int(ax["addr"]), count=count,
                bits=int(ax.get("bits") or 16),
                is_float=bool(ax.get("float")),
                equation=ax.get("equation") or "X",
                units=ax.get("units") or "",
                signed=bool(ax.get("signed")) if not ax.get("float") else None)


def to_entity(spec: dict, binfile) -> Entity:
    """The parameter ``spec`` describes, reading ``binfile``."""
    from .groups import USER
    s = normalise(spec)
    kind = s["kind"]
    x = _axis(s.get("x"), s["cols"]) if kind != "constant" else None
    y = _axis(s.get("y"), s["rows"]) if kind == "map" else None
    if kind == "curve" and x is None:
        kind = "indexed"
    title = (s.get("title") or "").strip() or s["name"]
    folder = (s.get("folder") or "").strip() or {
        "map": "Maps", "curve": "Curves", "indexed": "Curves",
        "constant": "Values"}[kind]
    e = Entity(
        name=s["name"].strip(), kind=kind, tier=USER, categories=[folder],
        description=(s.get("description") or "").strip() or title,
        units=s.get("units") or "", addr=s["addr"], rows=s["rows"],
        cols=s["cols"], bits=s["bits"],
        signed=bool(s.get("signed")) and not s.get("float"),
        equation=s.get("equation") or "X", source=SOURCE, x=x, y=y,
        binfile=binfile, declared_rows=s["rows"], declared_cols=s["cols"],
        order="col" if s.get("order") == "col" else "row",
        label=title if title != s["name"] else "",
        note="Defined by you (Add parameter...); not from a definition.")
    e.is_float = bool(s.get("float"))
    e.category_path = [folder]
    e._ui_label = title             # the tree shows the title as typed
    e.user_spec = s
    return e


def is_user(e) -> bool:
    return getattr(e, "source", "") == SOURCE


def hidden(e) -> bool:
    """A detected parameter a user parameter stands in for, with "hide the
    original" on: left out of the tree and of searches."""
    return bool(getattr(e, "_hidden_by", None))


def visible(entities) -> list:
    return [e for e in entities if not getattr(e, "_hidden_by", None)]


def _axis_spec(e, ax) -> Optional[dict]:
    if ax is None:
        return None
    if ax.addr is None:
        vals = list(ax.values or [])
        return {"values": [float(v) for v in vals], "units": ax.units or ""} \
            if vals else None
    signed = ax.signed
    if signed is None:                  # decided from the values, as shown
        from .xdf_export import _axis_signed
        signed = _axis_signed(e, ax)
    return {"addr": int(ax.addr), "bits": int(ax.bits),
            "signed": bool(signed) and not ax.is_float,
            "float": bool(ax.is_float), "equation": ax.equation or "X",
            "units": ax.units or ""}


def from_entity(e, session=None) -> dict:
    """A spec that reads exactly what the detected parameter ``e`` reads,
    named after it (``<symbol>_edit``, unique in ``session``), standing in
    for it with its original hidden: what **Duplicate as user parameter**
    starts from."""
    kind = {"indexed": "curve"}.get(e.kind, e.kind)
    if kind not in KINDS:
        raise ValueError("%s cannot be duplicated (%s)" % (e.name, e.kind))
    taken = {x.name for x in getattr(session, "entities", ())} if session else set()
    base = "%s_edit" % (e.name or "param").replace(" ", "_")
    name, i = base, 2
    while name in taken:
        name, i = "%s%d" % (base, i), i + 1
    title = getattr(e, "title", "") or e.name
    path = list(getattr(e, "category_path", None) or ()) or list(e.categories[:1])
    s = blank(kind)
    s.update({
        "name": name, "title": "%s (edited)" % title,
        "folder": " / ".join(path),
        "description": e.description or "",
        "addr": int(e.addr), "rows": int(e.rows), "cols": int(e.cols),
        "bits": int(e.bits), "signed": bool(e.signed),
        "float": bool(getattr(e, "is_float", False)),
        "order": "col" if getattr(e, "order", "row") == "col" else "row",
        "equation": e.equation or "X", "units": e.units or "",
        "x": _axis_spec(e, e.x) if kind != "constant" else None,
        "y": _axis_spec(e, e.y) if kind == "map" else None,
        "replaces": {"name": e.name, "group": getattr(e, "group", "") or ""},
        "hide": True})
    if e.kind == "indexed":             # a curve without an axis
        s["x"] = None
        s["cols"] = int(e.rows) * int(e.cols)
    return normalise(s)


def _original(session, rep: dict):
    """The detected parameter ``rep`` (``{"name", "group"}``) names."""
    name, group = rep.get("name"), rep.get("group") or ""

    def hit(e):
        return (not is_user(e) and e.name == name
                and (getattr(e, "group", "") or "") == group)
    e = next((e for e in session.entities if hit(e)), None)
    if e is None:
        try:
            e = next((v for v in session.variant_entities() if hit(v)), None)
        except Exception:
            e = None
    return e


def _clear_hidden(session) -> None:
    lists = [session.entities]
    cached = getattr(session, "_variant_entities", None)
    if cached:
        lists.append(cached)
    for lst in lists:
        for e in lst:
            if getattr(e, "_hidden_by", None):
                e._hidden_by = None


def specs(session) -> List[dict]:
    """The session's user parameters, as specs."""
    return [copy.deepcopy(s) for s in (getattr(session, "_user_params", None) or [])]


def drop(session) -> None:
    """Take every user parameter out of ``session``."""
    keep = [e for e in session.entities if not is_user(e)]
    if len(keep) != len(session.entities):
        session.entities = keep
    by_key = getattr(session, "by_key", None)
    if by_key:
        session.by_key = {k: v for k, v in by_key.items() if not is_user(v)}


def apply(session, spec_list) -> int:
    """Give ``session`` exactly the user parameters ``spec_list`` (any it had
    are replaced). A spec that does not fit this image is skipped. Returns
    how many were added."""
    drop(session)
    size = len(session.binfile.data)
    taken = {e.name for e in session.entities}
    ents, kept = [], []
    for sp in spec_list or []:
        s = normalise(sp)
        if validate(s, size, taken):
            continue
        e = to_entity(s, session.binfile)
        ents.append(e)
        kept.append(s)
        taken.add(e.name)
    # a new list: the tree rebuilds when the entity list changes
    session.entities = list(session.entities) + ents
    if getattr(session, "by_key", None) is None:
        session.by_key = {}
    for e in ents:
        session.by_key[(SOURCE, e.name, e.addr)] = e
    # the detected parameters they stand in for: hidden, the copy in place
    _clear_hidden(session)
    for e, s in zip(ents, kept):
        rep = s.get("replaces")
        if not rep or not s.get("hide"):
            continue
        orig = _original(session, rep)
        if orig is None:
            continue
        orig._hidden_by = e.name
        e.tier = orig.tier
        e.categories = list(orig.categories)
        e.category_path = list(getattr(orig, "category_path", None) or ())
        e.note = ("Your copy of %s (Duplicate as user parameter), shown in "
                  "its place; the original is hidden." % orig.name)
    session._user_params = [copy.deepcopy(s) for s in spec_list or []]
    return len(ents)


def put(session, spec: dict, replace: Optional[str] = None) -> Entity:
    """Add ``spec`` to ``session`` (or replace the user parameter called
    ``replace``). Returns the new entity. The spec must validate."""
    s = normalise(spec)
    cur = [x for x in specs(session) if x.get("name") != replace]
    cur.append(s)
    apply(session, cur)
    return find(session, s["name"])


def remove(session, name: str) -> bool:
    cur = specs(session)
    left = [x for x in cur if x.get("name") != name]
    if len(left) == len(cur):
        return False
    apply(session, left)
    return True


def find(session, name: str) -> Optional[Entity]:
    return next((e for e in session.entities
                 if is_user(e) and e.name == name), None)


def preview(spec: dict, binfile, limit: int = 8) -> Dict[str, object]:
    """What ``spec`` would show: ``{"z": rows of values (at most ``limit``
    square), "x": [...], "y": [...], "range": (lo, hi)}``. Raises on a spec
    that does not validate."""
    e = to_entity(spec, binfile)
    z = [[e.cell(r, c) for c in range(min(e.cols, limit))]
         for r in range(min(e.rows, limit))]
    allv = [e.cell(r, c) for r in range(e.rows) for c in range(e.cols)]
    fin = [v for v in allv if v == v]
    return {"z": z, "x": e.x_values()[:limit], "y": e.y_values()[:limit],
            "range": (min(fin), max(fin)) if fin else None}


def dumps(spec_list) -> bytes:
    import json
    return json.dumps({"format": FORMAT, "params": [normalise(s) for s in spec_list]},
                      indent=1, sort_keys=True).encode("utf-8")


def loads(data: Optional[bytes]) -> List[dict]:
    """The specs in ``data`` (``[]`` for nothing, or for a newer format this
    version cannot read)."""
    import json
    if not data:
        return []
    try:
        d = json.loads(bytes(data).decode("utf-8"))
    except ValueError:
        return []
    if not isinstance(d, dict) or int(d.get("format", 0)) > FORMAT:
        return []
    return [p for p in d.get("params") or [] if isinstance(p, dict)]


def apply_from_project(proj, sid, session) -> str:
    """The set's stored user parameters, added to ``session``. Returns a
    note ("" when the set has none)."""
    try:
        lst = proj.user_params(sid)
    except Exception:
        return ""
    if not lst:
        return ""
    n = apply(session, lst)
    if n < len(lst):
        return ("%d of %d user parameters do not fit this binary and are not "
                "shown" % (len(lst) - n, len(lst)))
    return ""


__all__ = ["SOURCE", "KINDS", "BITS", "blank", "normalise", "validate",
           "to_entity", "is_user", "hidden", "visible", "from_entity", "specs",
           "drop", "apply", "put", "remove", "find", "preview", "dumps",
           "loads", "apply_from_project"]
