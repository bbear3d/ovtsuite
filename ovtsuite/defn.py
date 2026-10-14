# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Definition snapshot: a parsed file's resolved parameter table, stored IN its
container so opening it again needs no identification and no matching.

Opening a plain binary means scoring every knowledge base, placing thousands
of parameters, reading state tables and so on -- seconds of work whose answer
depends only on the image's structure. A native container (``.edc16``,
``.edc15``, ``.6hp``) can carry that answer as ``BLOCK_DEFN``: every entity
(address, shape, axes, units, equation, provenance, tier, tree label), the
pairing keys, and every other public attribute the platform's loader sets
(match result, state tables, identification block, ...).

It is ADVISORY. The bytes stay the authority, so a snapshot is used only when
all of these still hold, and otherwise the file opens the normal way:

* the format version, platform and image length match;
* the knowledge-base data files are byte-identical to the ones it was built
  against (``data_digest``);
* the code that builds the model is the code that built it (``code_digest``);
* the image's STRUCTURE is unchanged: a digest of every byte that is not an
  editable parameter cell or axis breakpoint (``mask``). Tuning values keeps
  it valid; touching a header, a count word or the code area does not, and
  such a file simply takes the slow path (and is re-snapshotted when saved).

The encoding is plain JSON with a small tagged vocabulary -- never pickle, so
opening a shared file cannot execute anything. Objects are rebuilt only from
classes inside this package, and only by assigning their fields.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import inspect
import json
import os
import zlib
from typing import Dict, List, Optional, Tuple

from .model import Axis, Entity

#: Bump when the snapshot layout itself changes.
FORMAT = 1

def supports(platform: str) -> bool:
    """Can a session of ``platform`` carry a snapshot? Only when its module
    is installed and declares a ``load_kb`` hook (EDC16, EDC15 and ZF 6HP do;
    HVAC/VDO/8HP load in milliseconds and have none)."""
    from .modules import registry as _registry
    m = _registry.for_platform(platform)
    return m is not None and bool(m.load_kb)

#: Session attributes that are infrastructure, not part of the definition.
_SKIP = frozenset({
    "binfile", "path", "undo_stack", "change_log", "original", "last_checksum",
    "write_digest", "correct_program_area", "sgo_warning", "load_note", "entities", "by_key", "kb", "platform",
    "defn_state", "defn_skipped", "has_definition_file", "definition_notes",
})

_PKG = os.path.dirname(os.path.abspath(__file__))
_EF = [f.name for f in dataclasses.fields(Entity)
       if f.name not in ("binfile", "_eq")]
_AF = [f.name for f in dataclasses.fields(Axis) if f.name != "_eq"]


class Unsupported(Exception):
    """A value the snapshot vocabulary cannot carry; no snapshot is written."""


# ------------------------------------------------------------------ digests
_DIGEST_CACHE: Dict[str, str] = {}


def _rel(p: str) -> str:
    """``p`` relative to the package tree it is in -- the app's own, or an
    installed module's (laid out the same way) -- so a digest does not depend
    on where anything is installed."""
    from .modules import loader
    ap = os.path.abspath(p)
    for root in [_PKG] + list(loader.module_roots().values()):
        root = os.path.abspath(root)
        if ap == root or ap.startswith(root + os.sep):
            return os.path.relpath(ap, root).replace("\\", "/")
    return os.path.basename(ap)


def _hash_files(paths) -> str:
    h = hashlib.sha1()
    for p in paths:
        h.update(_rel(p).encode())
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def _module_of(platform: Optional[str]):
    from .modules import registry
    return registry.for_platform(platform) if platform else None


#: Data files that are DERIVED from the definition data rather than read
#: when a model is built: the search label index (ovtsuite.search) is
#: regenerated whenever the labels change, and hashing it would make a GUI
#: change look like a knowledge-base change.
_NOT_MODEL_DATA = {"search_index.json.gz"}


def data_digest(platform: Optional[str] = None) -> str:
    """Hash of the definition data a ``platform`` session is built from: its
    module's data files only, so installing or removing ANOTHER module never
    invalidates a stored definition. Without a platform, every visible data
    file (the pre-module behaviour)."""
    key = "data|%s" % (platform or "")
    if key not in _DIGEST_CACHE:
        from . import datafiles
        m = _module_of(platform)
        files = [p for n, p in datafiles.listing(m.id if m is not None else None)
                 if n not in _NOT_MODEL_DATA]
        _DIGEST_CACHE[key] = _hash_files(sorted(files))
    return _DIGEST_CACHE[key]


#: Bump when the way the BASE app builds or restores a model changes in a way
#: that makes a stored definition wrong: what a loader derives, an entity
#: field's meaning, the decode, the variant tables. Every stored definition
#: is then rebuilt once. A change to a file in ``MODEL_FILES`` also
#: invalidates them (in a source checkout), and tests/test_defn.py fails until
#: its recorded hash is updated -- that is the prompt to decide whether this
#: wants bumping too. A frozen build has no source to hash, so there this
#: number (with each module's own version) is ALL that tells builds apart.
MODEL_VERSION = 1

#: The base files that build, shape or restore a session's model -- traced
#: from ``TunerSession.__init__`` through a module's ``build`` / ``load_kb``
#: and :func:`restore`. Nothing else in the base app can change what a stored
#: definition holds: the GUI, the AI tools, projects, flashing, logging and
#: the app version do not. Tree labels have their own digest (``ui_digest``).
MODEL_FILES = (
    "session.py",     # the load flow, attribute defaults, variant_entities()
    "defn.py",        # this encoder / decoder
    "model.py",       # Entity / Axis
    "equation.py",    # the equations entities carry
    "binary.py",      # how the image is read (endianness, cells)
    "datafiles.py",   # which data files a knowledge base is read from
    "kb.py",          # the knowledge-base model the loaders read
    "match.py",       # anchor matching / cross-binary identification
    "structure.py",   # structural detection of maps and curves
    "layout.py",      # the ECU's calibration pointer table
    "fileinfo.py",    # ident16(), which layout.identify() keys on
    "catalogue.py",   # applying the tuning catalogue; resolve_selectors()
    "tiers.py",       # an entity's tier
    "groups.py",      # an entity's task group
    "variant.py",     # the variant (coding-selected) tables a snapshot holds
)


def _hash_source(paths) -> str:
    """Like ``_hash_files``, but line endings are normalised, so a checkout
    with CRLF line endings hashes the same as one with LF."""
    h = hashlib.sha1()
    for p in paths:
        h.update(_rel(p).encode())
        try:
            with open(p, "rb") as fh:
                h.update(fh.read().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def model_files_digest() -> str:
    """Hash of the base app's ``MODEL_FILES`` ("" without source files)."""
    if "model_files" not in _DIGEST_CACHE:
        files = [os.path.join(_PKG, n) for n in MODEL_FILES]
        _DIGEST_CACHE["model_files"] = (
            _hash_source(files) if all(os.path.isfile(p) for p in files)
            else "")
    return _DIGEST_CACHE["model_files"]


def _py_files(top: str) -> list:
    files = []
    for d, dirs, names in os.walk(top):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        files += [os.path.join(d, n) for n in sorted(names) if n.endswith(".py")]
    return files


def code_digest(platform: Optional[str] = None) -> str:
    """Hash of what builds a ``platform`` session's model: ``MODEL_VERSION``,
    the base app's ``MODEL_FILES``, and that platform's module (id, version
    and source). Other modules, the rest of the app and the app's version are
    left out, so an unrelated change or a new release keeps stored
    definitions valid.

    A build that ships without source files (a frozen app) hashes only
    ``MODEL_VERSION`` and the module's version."""
    key = "code|%s" % (platform or "")
    if key not in _DIGEST_CACHE:
        from .modules import loader
        m = _module_of(platform)
        mod = ""
        if m is not None:
            root = loader.module_roots().get(m.id, _PKG)
            files = _py_files(os.path.join(root, m.id))
            mod = "%s %s %s" % (m.id, m.version,
                                _hash_source(files) if files else "")
        _DIGEST_CACHE[key] = hashlib.sha1(
            ("%d|%s|%s|%d" % (MODEL_VERSION, model_files_digest(), mod,
                              FORMAT)).encode()
        ).hexdigest()
    return _DIGEST_CACHE[key]


def ui_digest() -> str:
    """Hash of what produces the tree labels (gui.py + the translators)."""
    if "ui" not in _DIGEST_CACHE:
        _DIGEST_CACHE["ui"] = _hash_files(
            [os.path.join(_PKG, "gui.py"),
             os.path.join(_PKG, "signal_names.py"),
             os.path.join(_PKG, "catalogue.py")])
    return _DIGEST_CACHE["ui"]


def _axis_span(ax) -> Tuple[int, int]:
    step = 4 if ax.is_float else max(1, ax.bits // 8)
    return ax.addr, ax.addr + ax.count * step


def mask_digest(data: bytes, entities) -> str:
    """Hash of ``data`` with every parameter cell and axis breakpoint left out
    -- the part of the image that determines where parameters are."""
    spans: List[Tuple[int, int]] = []
    for e in entities:
        spans.append((e.addr, e.end))
        for ax in (e.x, e.y):
            if ax is not None and ax.addr is not None:
                spans.append(_axis_span(ax))
    spans.sort()
    n = len(data)
    mv = memoryview(data)
    h = hashlib.sha1()
    cur = 0
    for a, b in spans:
        if a > cur:
            h.update(mv[cur:min(a, n)])
        if b > cur:
            cur = b
        if cur >= n:
            break
    if cur < n:
        h.update(mv[cur:])
    h.update(str(n).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- encoding
class _Enc:
    def __init__(self, ents: List[Entity], kb):
        self.ents = ents
        self.eidx = {id(e): i for i, e in enumerate(ents)}
        self.memo: Dict[int, int] = {}
        self.kbidx: Dict[int, Tuple[str, int]] = {}
        if kb is not None:
            for name, val in vars(kb).items():
                if isinstance(val, list):
                    for i, o in enumerate(val):
                        if type(o).__module__.startswith("ovtsuite") \
                                and not isinstance(o, (str, int, float)):
                            self.kbidx.setdefault(id(o), (name, i))

    def _table(self, seq):
        """A long list of one dataclass as ``{"$l": class, "k": field names,
        "r": rows}`` -- the field names once, not once per object -- which
        halves the size and the decode time of a match result."""
        if len(seq) < 8:
            return None
        t = type(seq[0])
        if (not dataclasses.is_dataclass(t) or not t.__module__.startswith(
                "ovtsuite.") or issubclass(t, Entity)):
            return None
        for x in seq:
            if type(x) is not t or id(x) in self.memo or id(x) in self.kbidx:
                return None
        names = [f.name for f in dataclasses.fields(t)
                 if not f.name.startswith("_eq")]
        rows = []
        for x in seq:
            n = len(self.memo)
            self.memo[id(x)] = n
            rows.append([n] + [self.enc(getattr(x, k)) for k in names
                               if hasattr(x, k)] if all(
                hasattr(x, k) for k in names) else None)
            if rows[-1] is None:
                raise Unsupported("partial object %s" % t.__qualname__)
        return {"$l": "%s:%s" % (t.__module__, t.__qualname__),
                "k": names, "r": rows}

    def enc(self, o):
        if o is None or isinstance(o, (bool, str)):
            return o
        if isinstance(o, enum.Enum):
            raise Unsupported("enum %s" % type(o).__name__)
        if isinstance(o, (int, float)) and type(o) in (int, float):
            return o
        t = type(o)
        if t is list:
            tab = self._table(o)
            if tab is not None:
                return tab
            return [self.enc(x) for x in o]
        if t is tuple:
            return {"$t": [self.enc(x) for x in o]}
        if t is dict:
            return {"$d": [[self.enc(k), self.enc(v)] for k, v in o.items()]}
        if t in (set, frozenset):
            return {"$s": [self.enc(x) for x in sorted(o, key=repr)]}
        if t in (bytes, bytearray):
            return {"$b": bytes(o).hex()}
        if isinstance(o, Entity):
            i = self.eidx.get(id(o))
            if i is None:
                i = len(self.ents)
                self.ents.append(o)
                self.eidx[id(o)] = i
            return {"$e": i}
        k = self.kbidx.get(id(o))
        if k is not None:
            return {"$k": list(k)}
        mod = t.__module__
        if not mod.startswith("ovtsuite."):
            raise Unsupported("%s.%s" % (mod, t.__qualname__))
        if t.__name__ == "BinaryFile":
            return {"$bin": 1}
        if id(o) in self.memo:
            return {"$r": self.memo[id(o)]}
        n = len(self.memo)
        self.memo[id(o)] = n
        if dataclasses.is_dataclass(o):
            fields = {f.name: getattr(o, f.name) for f in dataclasses.fields(o)
                      if hasattr(o, f.name)}
        elif hasattr(o, "__dict__"):
            fields = dict(vars(o))
        else:
            raise Unsupported("slots object %s" % t.__qualname__)
        return {"$o": n, "c": "%s:%s" % (mod, t.__qualname__),
                "f": {k: self.enc(v) for k, v in fields.items()
                      if not k.startswith("_eq")}}


class _Dec:
    def __init__(self, ents: List[Entity], kb, binfile):
        self.ents = ents
        self.kb = kb
        self.binfile = binfile
        self.refs: Dict[int, object] = {}

    def dec(self, v):
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        if isinstance(v, list):
            return [self.dec(x) for x in v]
        if "$t" in v:
            return tuple(self.dec(x) for x in v["$t"])
        if "$d" in v:
            return {self.dec(k): self.dec(x) for k, x in v["$d"]}
        if "$s" in v:
            return set(self.dec(x) for x in v["$s"])
        if "$b" in v:
            return bytes.fromhex(v["$b"])
        if "$e" in v:
            return self.ents[v["$e"]]
        if "$k" in v:
            name, i = v["$k"]
            return getattr(self.kb, name)[i]
        if "$bin" in v:
            return self.binfile
        if "$r" in v:
            return self.refs[v["$r"]]
        if "$l" in v:
            cls = _resolve(v["c"] if "c" in v else v["$l"])
            names = v["k"]
            out = []
            for row in v["r"]:
                obj = object.__new__(cls)
                self.refs[row[0]] = obj
                self._fill(obj, cls, {k: self.dec(x)
                                      for k, x in zip(names, row[1:])})
                out.append(obj)
            return out
        if "$o" in v:
            cls = _resolve(v["c"])
            obj = object.__new__(cls)
            self.refs[v["$o"]] = obj
            self._fill(obj, cls, {k: self.dec(x) for k, x in v["f"].items()})
            return obj
        raise ValueError("unknown snapshot node")


    @staticmethod
    def _fill(obj, cls, saved):
        """Assign ``saved`` to ``obj`` and give any field the snapshot did not
        carry its declared default."""
        defaults = _DEFAULTS.get(cls)
        if defaults is None:
            defaults = _DEFAULTS[cls] = (
                [(f.name, f.default, f.default_factory)
                 for f in dataclasses.fields(cls)]
                if dataclasses.is_dataclass(cls) else [])
        for name, d, factory in defaults:
            if name in saved:
                continue
            if d is not dataclasses.MISSING:
                saved[name] = d
            elif factory is not dataclasses.MISSING:
                saved[name] = factory()
        for k, x in saved.items():
            object.__setattr__(obj, k, x)


_DEFAULTS: Dict[type, list] = {}
#: Attributes decoded only when something asks for them (large, and read by
#: tools rather than by the editor).
LAZY = ("match15",)


_CLASSES: Dict[str, type] = {}


def _resolve(spec: str):
    hit = _CLASSES.get(spec)
    if hit is not None:
        return hit
    mod, _, qual = spec.partition(":")
    if not mod.startswith("ovtsuite."):
        raise ValueError("snapshot names a class outside the package: %s" % spec)
    import importlib
    obj = importlib.import_module(mod)
    for part in qual.split("."):
        obj = getattr(obj, part)
    if not inspect.isclass(obj):
        raise ValueError("snapshot names a non-class: %s" % spec)
    _CLASSES[spec] = obj
    return obj


# ------------------------------------------------------------ kb per platform
def load_kb(platform: str, family: str, delta: int = 0):
    """The knowledge base a session of this platform/family was built with
    (the platform module's ``load_kb`` hook)."""
    from .modules import registry as _registry
    m = _registry.for_platform(platform)
    if m is None or not m.load_kb:
        raise ValueError(platform)
    return m.call("load_kb", family, delta)


# ------------------------------------------------------------------- build
def _erow(e: Entity):
    row = []
    for n in _EF:
        v = getattr(e, n)
        if n in ("x", "y"):
            v = None if v is None else [getattr(v, k) for k in _AF]
        row.append(v)
    return row


def _labels(ents) -> Optional[dict]:
    """The tree label of each entity, from the GUI's own function, so a
    reopened file does not recompute (and re-translate) ten thousand of them.
    None when the GUI module is not importable."""
    try:
        from . import gui as _gui
        fn = _gui._english
        out = [fn(e) for e in ents]
    except Exception:
        return None
    return {"ui": ui_digest(), "labels": out}


def param_spans(entities) -> List[List[int]]:
    """Merged ``[start, end)`` spans of every parameter cell and axis
    breakpoint -- the bytes a tuning edit may change without touching the
    image's structure (the complement of what ``mask_digest`` hashes)."""
    spans = []
    for e in entities:
        spans.append((e.addr, e.end))
        for ax in (e.x, e.y):
            if ax is not None and ax.addr is not None:
                spans.append(_axis_span(ax))
    spans.sort()
    merged: List[List[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def outside_params(merged, addr: int) -> bool:
    """True when ``addr`` is in no span of ``param_spans``."""
    import bisect
    k = bisect.bisect_right([m[0] for m in merged], addr) - 1 if merged else -1
    return k < 0 or addr >= merged[k][1]


def build(session, data: bytes) -> Optional[bytes]:
    """The snapshot of ``session`` as bytes, or None when it has none to give
    (unsupported platform, or a value the vocabulary cannot carry)."""
    if (not supports(session.platform)
            or getattr(session, "support", "full") != "full"):
        return None
    for k in list(getattr(session, "_lazy", None) or ()):
        getattr(session, k)             # decode what was left for later
    try:
        # a packaged definition's tables (xdf_session.add_definition) are
        # added again on every open -- the snapshot is the module's alone
        main = [e for e in session.entities
                if getattr(e, "source", "") != "definition"]
        variants = None
        if session.platform == "edc16":
            variants = list(session.variant_entities())
        ents = main + (variants or [])
        # The table describes the bytes it was BUILT from. If a raw edit since
        # then touched structure -- a count word, a header, anything outside
        # a cell or breakpoint (TunerSession.edit_bytes sets the flag) -- the
        # shapes in memory are stale, and a snapshot of them would pass the
        # mask check against the new bytes and come back wrong on every open.
        # (2026-10-10: restoring ASDdc_dnIntegLtdPos_MAP's LQ header 4x4 ->
        # 4x6 by raw bytes saved a 4x4 definition.) Write none; the next open
        # parses from the bytes and the save after that snapshots the right
        # table. Checksum correction is not an edit and does not set it.
        if getattr(session, "_structure_edited", False):
            return None
        enc = _Enc(ents, getattr(session, "kb", None))
        attrs = {}
        # Lazily-decoded attributes go LAST: they may point at objects the
        # eager ones define, never the other way round.
        for k, v in sorted(vars(session).items(),
                           key=lambda kv: (kv[0] in LAZY, kv[0])):
            if k.startswith("_") or k in _SKIP:
                continue
            attrs[k] = enc.enc(v)
        # An entity the tree no longer lists (a placement replaced by a
        # structurally-found twin) can still be a pairing key's target;
        # encoding it registers it after the tree's own.
        by_key = [[enc.enc(k), enc.enc(e)["$e"]]
                  for k, e in session.by_key.items()
                  if getattr(e, "source", "") != "definition"]
        snap = {
            "format": FORMAT, "platform": session.platform,
            "image_len": len(data),
            "code": code_digest(session.platform),
            "data": data_digest(session.platform),
            "mask": mask_digest(data, ents),
            "kb_delta": getattr(session, "kb_delta", 0),
            "efields": _EF, "afields": _AF,
            "n_main": len(main), "n_var": None if variants is None
            else len(variants),
            "ents": [_erow(e) for e in ents],
            "by_key": by_key, "attrs": attrs,
        }
        ui = _labels(ents)
        if ui is not None:
            snap["ui"] = ui
        return json.dumps(snap, separators=(",", ":"),
                          allow_nan=True).encode("utf-8")
    except Unsupported as ex:
        session.defn_skipped = str(ex)
        return None


# ----------------------------------------------------------------- restore
def check(snap: dict, platform: str, data: bytes) -> Optional[str]:
    """Why ``snap`` cannot be used for this image, or None when it can."""
    if snap.get("format") != FORMAT:
        return "snapshot format %s" % snap.get("format")
    if snap.get("platform") != platform:
        return "snapshot is for %s" % snap.get("platform")
    if snap.get("image_len") != len(data):
        return "image length changed"
    if snap.get("code") != code_digest(platform):
        return "built by different code"
    if snap.get("data") != data_digest(platform):
        return "knowledge base changed"
    if snap.get("efields") != _EF or snap.get("afields") != _AF:
        return "entity layout changed"
    return None


def usable(blob: bytes, platform: str, data: bytes) -> Optional[str]:
    """Why the snapshot ``blob`` would not be used for ``data``, or None when
    :func:`restore` would take it -- the same checks, without decoding the
    attributes or loading a knowledge base. For choosing among several."""
    try:
        snap = json.loads(bytes(blob).decode("utf-8"))
        why = check(snap, platform, data)
        if why:
            return why
        return _structure_ok(snap, _rows_to_entities(snap, None), data)
    except Exception as ex:
        return "unreadable snapshot (%s)" % ex


def restore(session, container, data: bytes, rep=None) -> Optional[str]:
    """Populate ``session`` from the container's snapshot. Returns None on
    success, otherwise the reason it was not used (the caller then builds the
    model the normal way; nothing has been changed on ``session`` yet)."""
    blk = container.get(_block_defn()) if container is not None else None
    if blk is None:
        return "no snapshot"
    platform = session.platform
    if not supports(platform):
        return "platform has no snapshot"
    try:
        snap = json.loads(bytes(blk.data).decode("utf-8"))
        why = check(snap, platform, data)
        if why:
            return why
        if rep is not None:
            rep.step("Reading the stored definition")
        ents = _rows_to_entities(snap, session.binfile)
        why = _structure_ok(snap, ents, data)
        if why:
            return why
        attrs = snap["attrs"]
        family = attrs.get("family")
        kb = load_kb(platform, family, snap.get("kb_delta", 0))
        dec = _Dec(ents, kb, session.binfile)
        decoded = {}
        lazy = {}
        for k, v in attrs.items():
            if k in LAZY and isinstance(v, dict):
                lazy[k] = (lambda v=v: dec.dec(v))
            else:
                decoded[k] = dec.dec(v)
        by_key = {dec.dec(k): ents[i] for k, i in snap["by_key"]}
    except Exception as ex:                       # any damage -> normal path
        return "unreadable snapshot (%s)" % ex
    n_main = snap["n_main"]
    session.entities = ents[:n_main]
    if snap.get("n_var") is not None:
        session._variant_entities = ents[n_main:n_main + snap["n_var"]]
    session.by_key = by_key
    session.kb = kb
    for k, v in decoded.items():
        setattr(session, k, v)
    for k in lazy:                      # drop the loader's placeholder so the
        session.__dict__.pop(k, None)   # first read reaches __getattr__
    session._lazy = lazy
    # selector-derived names are read from the bytes, never from the snapshot
    from . import catalogue as _catalogue
    _catalogue.resolve_selectors(
        session.entities, group_key=(lambda e: e.group)
        if platform == "edc15" else None)
    ui = snap.get("ui")
    if ui and ui.get("ui") == ui_digest() and len(ui["labels"]) == len(ents):
        for e, lab in zip(ents, ui["labels"]):
            e._ui_label = lab
    return None


def _block_defn() -> int:
    from . import container as _c
    return _c.BLOCK_DEFN


def _rows_to_entities(snap: dict, binfile) -> List[Entity]:
    ef, af = snap["efields"], snap["afields"]
    ents = []
    for row in snap["ents"]:
        kw = dict(zip(ef, row))
        for ax in ("x", "y"):
            a = kw.get(ax)
            if a is not None:
                kw[ax] = Axis(**dict(zip(af, a)))
        kw["binfile"] = binfile
        ents.append(Entity(**kw))
    return ents


def _structure_ok(snap: dict, ents: List[Entity], data: bytes) -> Optional[str]:
    if mask_digest(data, ents) != snap.get("mask"):
        return "image structure changed"
    return None
