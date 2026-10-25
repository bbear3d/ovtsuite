# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: definitions, calibration values and binaries from one
format to another, with no project or binary open.

Every conversion is the same pipeline -- read into the definition model,
place it in an image (a binary is optional: it only settles what a
definition leaves to the image), apply the transforms asked for, write,
read the result back and compare. :func:`run` takes a :class:`Job`; the
window, the standalone tool, the CLI (``ovtsuite convert``) and MCP all
build one.

Value files (DCM, CDFX: :mod:`.calvalues`) go through the same job: with a
definition and a binary they are exported from the image or applied to it,
alone they convert to each other.

Base app, no Qt: nothing here needs a platform module, except checksum
plugin descriptors, which installed modules supply.
"""

from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from ..definitions import DefinitionError, detect as _detect_defn, load as _load_defn
from ..definitions.model import Definition
from . import calvalues as _values, fmt_csv, fmt_json, fmt_romxml, image as _image
from .entities import entities as _entities, resolve as _resolve

#: definition formats read
DEF_IN = ("xdf", "a2l", "dam", "ols", "kp", "json", "csv", "ecuflash", "romraider")
#: definition formats written
DEF_OUT = ("xdf", "a2l", "kp", "dam", "json", "csv", "ecuflash", "romraider")
#: image formats, both ways
IMG = _image.FORMATS
#: calibration value formats, both ways
VAL = _values.FORMATS
#: two definitions compared (a CSV of the differences)
DIFF = "diff"
#: the binaries a WinOLS project embeds, extracted (:mod:`.embedded`); the
#: output is a folder
EXTRACT = "extract"
EXT = {"xdf": ".xdf", "a2l": ".a2l", "kp": ".kp", "dam": ".dam",
       "json": ".ovtdef.json", "csv": ".csv", "bin": ".bin", "hex": ".hex",
       "srec": ".s19", "dcm": ".dcm", "cdfx": ".cdfx", "diff": ".diff.csv",
       "ecuflash": ".xml", "romraider": ".romraider.xml", "extract": ""}
NAMES = {"xdf": "TunerPro XDF", "a2l": "ASAP2 (A2L)", "kp": "WinOLS map pack (KP)",
         "dam": "Bosch DAMOS", "json": "OVT definition (JSON)",
         "csv": "Map list (CSV)", "ols": "WinOLS project (OLS)",
         "bin": "Binary (BIN)", "hex": "Intel HEX", "srec": "Motorola S-record",
         "dcm": "DCM values (DAMOS exchange)", "cdfx": "CDFX values (ASAM CDF)",
         "diff": "Differences between the two (CSV)",
         "ecuflash": "EcuFlash XML", "romraider": "RomRaider XML",
         "extract": "Embedded binaries (extract from the OLS)"}
#: writers still marked experimental
EXPERIMENTAL = ("kp",)


class ConvertError(ValueError):
    """A job that cannot run (nothing readable, a target that needs what was
    not given). Its text is shown to the user as it stands."""


@dataclass
class Job:
    sources: List[str]
    target: str
    out: str = ""
    #: optional binary: settles the base, stored sizes, signedness, preview
    binary: Optional[str] = None
    #: definition address of image offset 0 (None: from the definition)
    base: Optional[int] = None
    #: image size in bytes (None: from the binary / the definition)
    size: Optional[int] = None
    title: str = ""
    #: a table whose sizes the image stores, with no binary: "max" / "skip"
    dynamic: str = "max"
    #: transforms (see :mod:`.transforms`): {"categories": [...],
    #: "pattern": "...", "start": int, "end": int, "kinds": [...]}
    filters: Dict = field(default_factory=dict)
    #: {"backend": "builtin" | "online", "provider": "...", "source": "de",
    #:  "target": "en", "mode": "replace" | "bilingual" | "keep"}
    translate: Optional[Dict] = None
    #: XDF only: "none", "keep", a descriptor id, or "dll:<path>"
    checksum: str = "none"
    #: XDF: write value blocks without an axis; A2L: "offset" / "ecu";
    #: HEX/S-record: record length; byte swap etc. Porting: "port" (the
    #: binary is another software version: place every table by evidence),
    #: "reference" (the image the definition was made for), "unverified"
    #: ("category" / "skip": tables nothing confirms)
    options: Dict = field(default_factory=dict)
    self_check: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Result:
    out: List[str] = field(default_factory=list)
    tables_in: int = 0
    tables_out: int = 0
    #: {table: [what was lost]}
    loss: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    #: differences the write-then-reread check found
    check: List[str] = field(default_factory=list)
    report: str = ""
    base: int = 0
    size: int = 0
    #: value files: what was exported / applied, in words
    values: List[str] = field(default_factory=list)
    #: a port: tables per placement grade ({"exact": n, "moved": n, ...})
    placement: Dict[str, int] = field(default_factory=dict)
    #: a definition diff: its rows ({"name", "change", "old", "new"})
    diff: List[dict] = field(default_factory=list)
    #: output files that were already there and were left alone
    skipped: List[str] = field(default_factory=list)
    #: a map pack written from a WinOLS file's own records and folders
    passthrough: bool = False

    @property
    def lossy(self) -> bool:
        return bool(self.loss or self.check)


# ------------------------------------------------------------------ reading
def sniff(data: bytes) -> Tuple[str, str]:
    """``("definition", type)``, ``("values", "dcm" | "cdfx")`` or
    ``("image", format)`` from the file's own structure (never its name)."""
    t = _detect_defn(data)
    if t:
        return "definition", t
    if fmt_json.is_json(data):
        return "definition", "json"
    if fmt_romxml.is_ecuflash(data):
        return "definition", "ecuflash"
    if fmt_romxml.is_romraider(data):
        return "definition", "romraider"
    v = _values.detect(data)
    if v:
        return "values", v
    if fmt_csv.is_csv(data):
        return "definition", "csv"
    return "image", _image.detect(data)


def sniff_file(path: str, head: int = 1 << 20) -> Tuple[str, str]:
    """:func:`sniff` on a file, reading its first ``head`` bytes -- and
    all of it when those read as an image: a map pack or project (KP,
    OLS) can need more than its start to be recognised, and taking one
    for a binary would use it as one."""
    with open(path, "rb") as fh:
        data = fh.read(head)
        more = fh.read(1)
    kind, t = sniff(data)
    if kind == "image" and more:
        kind, t = sniff(_read(path))
    return kind, t


def classify(paths: List[str]) -> Dict[str, List[str]]:
    """``{"definition": [...], "values": [...], "image": [...]}``: what
    each file is, by its content."""
    out: Dict[str, List[str]] = {"definition": [], "values": [], "image": []}
    for p in paths:
        kind, _t = sniff(_read(p))
        out[kind].append(p)
    return out


def load_definition(data: bytes, title: str = "", path: Optional[str] = None,
                    rom: Optional[str] = None, image: Optional[bytes] = None) -> Definition:
    """A definition from a file's bytes. ``path``: where it is (an EcuFlash
    ROM file finds the base it includes beside it); ``rom`` / ``image``: which
    ROM of a RomRaider file (named, or the one the image is)."""
    kind, t = sniff(data)
    if kind != "definition":
        raise ConvertError("not a definition (it reads as a %s image)" % t)
    if t in ("ecuflash", "romraider"):
        from ..definitions.model import DefinitionError as _DE
        try:
            if t == "ecuflash":
                defn = fmt_romxml.load_ecuflash(data, title, path)
            else:
                defn = fmt_romxml.load_romraider(data, title, rom or None, image)
        except _DE as exc:
            raise ConvertError("%s %s" % (os.path.basename(path) if path else "The file", exc))
        defn.info.setdefault("source_type", t)
        return defn
    if t == "json":
        defn = fmt_json.loads(data)
    elif t == "csv":
        defn = fmt_csv.loads(data)
        defn.title = title
    else:
        # a private copy: the reader's cache keeps its own, and the
        # transforms change tables in place
        defn = copy.copy(_load_defn(data, t, title))
        defn.tables = [copy.copy(x) for x in defn.tables]   # copied deep when placed
        defn.checksums = [dict(c) for c in defn.checksums]
        defn.info = dict(defn.info)
        defn.warnings = list(defn.warnings)
    defn.info.setdefault("source_type", t if t not in ("winols",) else defn.type)
    if t == "dam":
        defn.passthrough = ("dam", bytes(data))
    if t in ("a2l", "dam"):
        # their table names are identifiers: translation must not touch them
        for x in defn.tables:
            if not x.symbol:
                x.symbol = x.name
    if defn.reference and not defn.images:
        defn.images = [("embedded", defn.reference[0], defn.reference[1])]
    return defn


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _segments_extent(defn: Definition) -> Optional[Tuple[int, int]]:
    if not defn.segments:
        return None
    lo = min(s[0] for s in defn.segments)
    hi = max(s[0] + s[1] for s in defn.segments)
    return lo, hi - lo


def place(defn: Definition, data: Optional[bytes], base: Optional[int],
          size: Optional[int], warnings: List[str]) -> Tuple[int, int, Optional[bytes]]:
    """``(base, size, image bytes or None)``: where offset 0 of the image is
    in definition addresses, and how large the image is. Settled, in order,
    by what was given, the binary, the image the definition carries, and the
    definition's own segments / stated size; only when none of those says,
    from where its tables lie (and then said so)."""
    if data is None and defn.reference:
        data, ref_base = defn.reference
        if base is None:
            base = ref_base
        if defn.type == "ols":
            names = [n for n, _d, _b in defn.images] or ["Original"]
            warnings.append("No binary given: the binary embedded in the WinOLS project "
                            "is used (%s, 0x%X bytes%s)." % (
                                names[0], len(data),
                                "; its other versions: %s" % ", ".join(names[1:])
                                if len(names) > 1 else ""))
    if data is not None:
        size = size or len(data)
        if base is None:
            base = defn.base_in(data)
        return base, size, data
    if not size:
        try:
            size = int(defn.info.get("image_size") or 0)
        except ValueError:
            size = 0
    if not size:
        ext = _segments_extent(defn)
        if ext:
            if base is None and defn.base_for is None:
                base = ext[0]
            size = ext[1]
            warnings.append("No binary given: image size 0x%X taken as the span of "
                            "the definition's memory segments. Give the size (and "
                            "base) of the image file to override." % size)
    if not size and defn.info.get("top") and defn.base_for is not None:
        # a DAMOS: the image ends where its highest code / data segment
        # does; its size is the power of two that reaches back to the lowest
        # table
        top = int(defn.info["top"], 16)
        low = min((t.z.addr for t in defn.tables if t.z.addr is not None), default=top)
        size = 1
        while top - size > low:
            size <<= 1
        warnings.append("No binary given: image size 0x%X, from the DAMOS's top "
                        "segment address 0x%X back to its lowest table. Give the "
                        "size to override." % (size, top))
    if base is None:
        base = defn.base_of(size) if size else defn.base
    if not size:
        end = 0
        for t in defn.tables:
            if t.z.addr is not None:
                end = max(end, t.z.addr - base + max(1, t.rows * t.cols) * max(1, t.z.bits // 8))
        size = 1
        while size < end:
            size <<= 1
        warnings.append("Image size not stated by the definition and no binary "
                        "given: 0x%X, the smallest power of two holding every "
                        "table. Give the size to override." % size)
    return base, size, None


#: the category tables a port could not confirm are put in (the name the app
#: gives them when a definition is attached to a binary)
UNVERIFIED = "Unverified - check before writing"


def reference_image(path: str, defn: Definition) -> Tuple[bytes, int]:
    """``(image, base)`` of the image a definition was made for: an Intel
    HEX / S-record at its own addresses, a plain binary where the
    definition places itself in it."""
    from ..definitions import place as P
    raw = _read(path)
    kind, t = sniff(raw)
    if kind != "image":
        raise ConvertError("%s is not an image (it reads as %s)."
                           % (os.path.basename(path), NAMES.get(t, t)))
    img = _image.read(raw)
    if img.source in ("hex", "srec"):
        return img.flat(), img.start
    return P.reference_from_file(raw, defn)


def port(defn: Definition, data: bytes, job: Job, loss: Dict[str, List[str]],
         warnings: List[str], stated: Optional[int] = None, progress=None):
    """``defn`` (made for one software version) placed table by table in
    ``data`` (another), by the evidence :mod:`ovtsuite.definitions.place`
    finds: ``(the definition for this binary, {grade: tables})``. Tables
    nothing confirms go into :data:`UNVERIFIED`, or are left out."""
    from ..definitions import place as P
    ref = None
    if job.options.get("reference"):
        ref = reference_image(job.options["reference"], defn)
    if stated is not None:
        # the binary states its address: offer it as the definition's own
        # offset; the evidence still decides, and finds what moved
        defn = copy.copy(defn)
        defn.locate = lambda _d, _b=stated: _b
    pl = P.place(defn, data, reference=ref, progress=progress)
    keep = job.options.get("unverified", "category") != "skip"
    out = P.relocated(defn, pl, show_unverified=keep)
    by = pl.by_index()
    for i, t in enumerate(defn.tables):
        p = by.get(i)
        if p is not None and not p.shown and not keep:
            loss.setdefault(t.symbol or t.name, []).append(
                "nothing in this binary confirms where it is -- left out")
    for t in out.tables:
        if getattr(t, "placement", "") == "unverified":
            t.categories = [UNVERIFIED] + [c for c in t.categories if c != UNVERIFIED]
            loss.setdefault(t.symbol or t.name, []).append(
                "unverified: nothing in this binary confirms where it is; placed by the "
                "offset of the nearest confirmed table -- check before writing")
    counts = pl.counts()
    warnings.append("Ported onto %s (offset 0x%X%s): %s." % (
        os.path.basename(job.binary or "the binary"), pl.base,
        ", with the original image" if ref is not None or pl.reference else "",
        pl.summary()))
    if not pl.established or (pl.checkable and pl.fit < 0.1):
        warnings.append("Few tables confirm any offset in this binary: it may not be "
                        "of the same project as the definition.")
    return out, counts


def copy_table(t):
    """A copy of a table whose storages, axes and lists can be changed
    without touching the original (``dynamic`` is shared, never copied:
    it holds the whole parsed file)."""
    t = copy.copy(t)
    t.z = copy.copy(t.z)
    for a in ("x", "y"):
        ad = getattr(t, a)
        if ad is not None:
            ad = copy.copy(ad)
            ad.values = copy.copy(ad.values)
            ad.fixed = list(ad.fixed) if ad.fixed else ad.fixed
            ad.labels = list(ad.labels) if ad.labels else ad.labels
            setattr(t, a, ad)
    t.categories = list(t.categories)
    t.originals = dict(t.originals)
    t.loss = list(t.loss)
    return t


def rebased(tables, base: int):
    """Copies of ``tables`` with every address an image offset."""
    out = []
    for t in tables:
        t = copy_table(t)
        for st in (t.z, t.x.values if t.x else None, t.y.values if t.y else None):
            if st is not None and st.addr is not None:
                st.addr -= base
        if t.record is not None:
            t.record -= base
        t.dynamic = None
        out.append(t)
    return out


# ------------------------------------------------------------------ progress
class Steps:
    """How far a run is, for whoever watches it. Called with a step's name,
    as the plain ``progress`` callback always was (``say("Writing")``);
    ``say("Writing", 0.7)`` also says that 70% of this part of the work is
    done when the step starts. ``progress`` gets the name only (a one-
    argument callback, as before); ``fraction`` gets the share of the whole
    job done, 0.0 to 1.0, never going back. :meth:`part` is the same
    reporter for a slice of the work (``say.part(0, 0.6)`` for the reading),
    so a helper reports 0..1 of its own part without knowing the rest.

    ``expect`` (optional) gets the share of the whole job at which the step
    now under way should end -- ``say("Writing", 0.45, 0.75)`` or
    :meth:`span` -- so a bar can move on smoothly while a long step gives no
    word (see :mod:`ovtsuite.gui_smoothbar`)."""

    def __init__(self, progress=None, fraction=None, lo: float = 0.0, hi: float = 1.0,
                 _top=None, expect=None):
        self.progress = progress
        self.fraction = fraction
        self.expect = expect
        self.lo, self.hi = lo, hi
        self._top = _top if _top is not None else self
        self._done = 0.0                      # (the top reporter's) furthest share
        self._until = 0.0                     # (the top reporter's) furthest expected end

    @classmethod
    def of(cls, progress=None, fraction=None) -> "Steps":
        """``progress`` itself when it is a :class:`Steps` already."""
        if isinstance(progress, Steps):
            return progress
        return cls(progress, fraction)

    def __call__(self, label: str = "", at: Optional[float] = None,
                 to: Optional[float] = None) -> None:
        if label and self.progress is not None:
            self.progress(label)
        if at is not None:
            self.at(at)
        if to is not None:
            self.until(to)

    def _share(self, x: float) -> float:
        return self.lo + (self.hi - self.lo) * min(1.0, max(0.0, x))

    def until(self, share: float) -> None:
        """The step under way should end at ``share`` (0..1) of this part."""
        if self.expect is None:
            return
        f = self._share(share)
        top = self._top
        if f > top._until and f > top._done:
            top._until = f
            self.expect(f)

    def span(self, a: float, b: float) -> None:
        """A step from ``a`` to ``b`` of this part starts."""
        self.at(a)
        self.until(b)

    def at(self, share: float) -> None:
        """``share`` (0..1) of this part done."""
        if self.fraction is None:
            return
        f = self._share(share)
        top = self._top
        if f > top._done:
            top._done = f
            self.fraction(f)

    def part(self, a: float, b: float) -> "Steps":
        """The reporter for ``a``..``b`` of this part."""
        w = self.hi - self.lo
        return Steps(self.progress, self.fraction, self.lo + w * a, self.lo + w * b,
                     self._top, self.expect)


# ------------------------------------------------------------------ the run
def prepare(job: Job, warnings: Optional[List[str]] = None, progress=None,
            placement: Optional[Dict[str, int]] = None, fraction=None):
    """Read and place every source: ``(definition in image offsets, base,
    size, image bytes or None, {table: [loss]}, tables read)``. Several
    sources are merged (first source wins a conflict). With the ``port``
    option every table is placed in the binary by its own evidence
    (:func:`port`); ``placement`` gets the count per grade. ``progress``
    gets each step's name, ``fraction`` the share done (see :class:`Steps`;
    ``progress`` may be one)."""
    from . import transforms
    say = Steps.of(progress, fraction)
    warnings = warnings if warnings is not None else []
    if not job.sources:
        raise ConvertError("No source file.")
    data = None
    stated = job.base
    if job.binary:
        img = _image.read(_read(job.binary))
        data = img.flat()
        # an Intel HEX / S-record image states where it lies; a definition
        # in ECU addresses (A2L, DAMOS) is placed by that, never guessed
        if img.source in ("hex", "srec") and stated is None:
            stated = img.start
    placed: List[Definition] = []
    loss: Dict[str, List[str]] = {}
    base = size = 0
    image = data
    n_in = 0
    say.at(0.05)
    porting = bool(job.options.get("port"))
    for i, src in enumerate(job.sources):
        # each source an equal share: reading it, then (porting) finding
        # its tables in the binary -- the long part -- then placing them
        one = say.part(0.05 + 0.95 * i / len(job.sources),
                       0.05 + 0.95 * (i + 1) / len(job.sources))
        one.span(0.0, 0.2 if porting else 0.85)
        raw = _read(src)
        defn = load_definition(raw, job.title or os.path.splitext(os.path.basename(src))[0],
                               path=src, rom=job.options.get("rom"), image=data)
        one.span(0.2 if porting else 0.85, 0.8 if porting else 1.0)
        n_in += len(defn.tables)
        warnings.extend("%s: %s" % (os.path.basename(src), w) for w in defn.warnings)
        given = job.base
        if given is None and stated is not None and (
                defn.base_for is not None or defn.locate is not None):
            given = stated
        if job.options.get("port"):
            if data is None:
                raise ConvertError("Porting needs the binary of the other software "
                                   "version: give it as the binary.")
            if defn.patches:
                warnings.append("%s: its %d XDF patches are bytes for the software it was "
                                "made for -- not carried to another version."
                                % (os.path.basename(src), len(defn.patches)))
                defn = copy.copy(defn)
                defn.patches = []
            defn, counts = port(defn, data, job, loss, warnings, given, say)
            one.at(0.8)
            if placement is not None:
                for k, v in counts.items():
                    placement[k] = placement.get(k, 0) + v
            given = None
        base, size, image = place(defn, data, given, job.size, warnings)
        tables, why = _resolve(defn, base, size, image, job.dynamic)
        for k, v in why.items():
            loss.setdefault(k, []).extend(v)
        out = copy.copy(defn)
        out.tables = rebased(tables, base)
        from . import xdfpatch
        out.patches = xdfpatch.rebased(defn.patches, base)
        out.base, out.base_for, out.locate = 0, None, None
        out.segments = [(0, size)]
        out.info = dict(defn.info, image_size=str(size))
        placed.append(out)
        one.at(1.0)
    defn = transforms.merge(placed, warnings) if len(placed) > 1 else placed[0]
    if job.title:
        defn.title = job.title
    return defn, base, size, image, loss, n_in


def _winols_titles(ents, tag: str) -> None:
    """A WinOLS map in an XDF: titled
    by its description (its name when it has none), described as
    ``OLS: <name> | <description> | Z: <value label> | X: .. | Y: ..`` --
    so TunerPro's tree reads in words and the identifier is still there."""
    for e in ents:
        t = getattr(e, "_tabledef", None)
        name = (t.name if t is not None else e.name) or e.name
        desc = (t.description if t is not None else e.description) or ""
        zlab = ""
        if " | Z: " in desc:
            desc, zlab = desc.split(" | Z: ", 1)
        elif desc.startswith("Z: "):
            desc, zlab = "", desc[3:]
        parts = ["%s: %s" % (tag, name)]
        if desc and desc != name:
            parts.append(desc)
        if zlab and zlab not in (name, desc):
            parts.append("Z: " + zlab)
        for k, ax in (("X", getattr(e, "x", None)), ("Y", getattr(e, "y", None))):
            lab = getattr(ax, "label_text", "") if ax is not None else ""
            if lab and lab not in ("-", "?"):
                parts.append("%s: %s" % (k, lab))
        e.xdf_title = desc or name
        e.xdf_description = " | ".join(parts)


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def job_inputs(job: Job) -> List[str]:
    """Every file ``job`` reads: its sources, the binary, the original image
    a port compares with and the binary a patch is made from."""
    o = job.options or {}
    return [p for p in list(job.sources) + [job.binary, o.get("reference"),
                                            o.get("patch_with")] if p]


def replaces_input(job: Job, out: str) -> Optional[str]:
    """The input file ``out`` would overwrite (case-blind, as Windows
    names are), or None."""
    k = _key(out)
    return next((p for p in job_inputs(job) if _key(p) == k), None)


def free_output(path: str, inputs) -> str:
    """``path``, or ``<stem>-converted<ext>`` (then ``-converted-2`` ...)
    when ``path`` is one of ``inputs``: a default name never overwrites a
    file being read."""
    keys = {_key(p) for p in inputs if p}
    if _key(path) not in keys:
        return path
    stem, ext = os.path.splitext(path)
    cand, n = stem + "-converted" + ext, 2
    while _key(cand) in keys:
        cand, n = "%s-converted-%d%s" % (stem, n, ext), n + 1
    return cand


#: what a run does with an output file that is already there
#: (``job.options["existing"]``): ask ``run``'s ``ask`` (none given: skip
#: it), replace it, or leave it alone
EXISTING = ("ask", "overwrite", "skip")


def existing_policy(job: Job) -> str:
    """``job``'s choice for an output that is already there (see
    :data:`EXISTING`; anything else, or nothing, is ``"ask"``)."""
    v = (job.options or {}).get("existing")
    return v if v in EXISTING else "ask"


def may_write(job: Job, path: str, ask=None) -> bool:
    """True when ``path`` may be written: it is not there yet, or the job
    says to overwrite, or ``ask(path)`` (asked only with ``"ask"``) says
    yes. With ``"ask"`` and no ``ask`` -- nothing to ask, e.g. the command
    line, a watch folder -- an existing file is left alone."""
    if not os.path.exists(path):
        return True
    p = existing_policy(job)
    if p == "overwrite":
        return True
    if p == "skip" or ask is None:
        return False
    return bool(ask(path))


def run(job: Job, progress=None, fraction=None, ask=None) -> Result:
    """Run ``job``: write ``job.out`` and return what happened. A default
    output name never overwrites an input; a given one that would is
    refused. ``progress`` is called with each step's name (``"Reading"``,
    ``"Writing"`` ...), ``fraction`` with the share of the job done (0.0
    to 1.0, rising; 1.0 once it is done) -- see :class:`Steps`. An output
    file already there is replaced or left alone as :func:`may_write`
    says (``ask(path) -> bool`` is asked before any work is done); one
    left alone is in ``Result.skipped``, not ``Result.out``."""
    say = Steps.of(progress, fraction)
    say.at(0.0)
    res = _run(job, say, ask)
    say.at(1.0)
    return res


def _run(job: Job, say: Steps, ask=None) -> Result:
    from . import transforms
    res = Result()
    tgt = (job.target or "").lower()
    if tgt == EXTRACT:
        _run_extract(job, res, say, ask)
        res.report = report(job, res)
        return res
    if tgt not in DEF_OUT + IMG + VAL + (DIFF,):
        raise ConvertError("Unknown target format %r." % job.target)
    if not job.out:
        stem = os.path.splitext(job.sources[0])[0] if job.sources else "out"
        job.out = free_output(stem + EXT[tgt], job_inputs(job))
    hit = replaces_input(job, job.out)
    if hit:
        raise ConvertError("The output %s would overwrite %s, which the conversion "
                           "reads. Choose another output name."
                           % (os.path.basename(job.out), os.path.basename(hit)))
    if not may_write(job, job.out, ask):
        # asked before reading anything: a file left alone costs no work
        res.skipped.append(job.out)
        res.report = report(job, res)
        return res
    kinds = classify(job.sources)

    if tgt == DIFF:
        _run_diff(job, kinds, res, say)
        return _finish(job, res)
    if tgt in VAL:
        _run_values(job, tgt, kinds, res, say)
        return _finish(job, res)
    if tgt in IMG and kinds["values"]:
        _run_apply(job, tgt, kinds, res, say)
        return _finish(job, res)
    if tgt in IMG:
        say("Converting the image", 0.1, 1.0)
        _run_image(job, res)
        res.report = report(job, res)
        return res
    if kinds["values"] and not kinds["definition"]:
        raise ConvertError(
            "%s holds calibration values, not table definitions. To convert the "
            "values choose DCM or CDFX; to write them into a binary choose an "
            "image format and add the definition (A2L, DAMOS, XDF ...) and the "
            "binary." % os.path.basename(kinds["values"][0]))
    if kinds["image"] and kinds["definition"]:
        # a binary added with the definitions is the binary, not a source
        job = _binary_for(job, kinds, "place the definition on")
    elif kinds["values"]:
        job = Job.from_dict(dict(job.to_dict(), sources=kinds["definition"]))

    # the shares, by how long each step takes: reading (and porting, the
    # long part when asked), then writing and the read-back check, each
    # about as long as the reading; translating online can take a while too
    porting = bool(job.options.get("port"))
    r_end = 0.6 if porting else 0.4
    w_at, c_at = (0.68, 0.84) if porting else (0.45, 0.78)
    say("Reading", 0.0)
    defn, base, size, image, loss, n_in = prepare(job, res.warnings, say.part(0.0, r_end),
                                                  res.placement)
    res.tables_in, res.base, res.size = n_in, base, size
    res.loss.update(loss)
    say("Applying transforms", r_end, w_at - 0.03)
    if job.filters:
        defn.tables = transforms.filter_tables(defn.tables, job.filters)
    if job.translate:
        from . import translate
        translate.apply(defn, job.translate, res.warnings)
    say.at(w_at - 0.03)
    if tgt == "xdf":
        from . import checksums
        defn.checksums = checksums.for_export(job, defn, image, res.warnings)
        if job.options.get("patch_with"):
            say("Making the patch", w_at - 0.02, w_at)
            defn.patches = list(defn.patches) + _patches_from(job, defn, image, size, res)
    elif job.options.get("patch_with"):
        raise ConvertError("A patch is written into an XDF only: choose TunerPro XDF.")
    elif defn.patches:
        res.warnings.append("%d XDF patches have no form in %s -- not carried."
                            % (len(defn.patches), NAMES.get(tgt, tgt)))
    say("Writing", w_at, c_at if job.self_check else 1.0)
    payload, written = write(defn, tgt, size, image, job, res)
    with open(job.out, "wb") as fh:
        fh.write(payload)
    res.out.append(job.out)
    res.tables_out = written
    if job.self_check:
        say("Checking the result", c_at, 1.0)
        # a DAMOS written back into its own file reads at the source's base
        hint = res.base if (tgt == "dam" and isinstance(defn.passthrough, tuple)) else None
        res.check = res.check + self_check(defn, tgt, payload, size, image, job,
                                           res.loss, hint)
    res.report = report(job, res)
    if res.lossy:
        with open(job.out + ".convert.txt", "w", encoding="utf-8") as fh:
            fh.write(res.report)
        res.out.append(job.out + ".convert.txt")
    elif os.path.exists(job.out + ".convert.txt"):
        try:                                  # an earlier run's report is stale
            os.remove(job.out + ".convert.txt")
        except OSError:
            pass
    return res


def _patches_from(job: Job, defn: Definition, image: Optional[bytes], size: int,
                  res: Result) -> List[dict]:
    """XDFPATCH blocks turning the binary into the changed one
    (``options["patch_with"]``)."""
    from . import xdfpatch
    if image is None:
        raise ConvertError("A patch is the difference between the binary and a changed "
                           "one: give the original binary as the binary.")
    path = job.options["patch_with"]
    raw = _read(path)
    kind, t = sniff(raw)
    if kind != "image":
        raise ConvertError("%s is not a binary (it reads as %s)."
                           % (os.path.basename(path), NAMES.get(t, t)))
    img = _image.read(raw)
    new = img.flat()
    if img.source in ("hex", "srec") and job.binary:
        src = _image.read(_read(job.binary))
        if src.source in ("hex", "srec"):
            new = img.flat(src.start, src.end)
    title = job.options.get("patch_title") or \
        "Changes in %s" % os.path.splitext(os.path.basename(path))[0]
    try:
        patches = xdfpatch.build(defn, image[:size], new, title,
                                 job.options.get("patch_split", "one"),
                                 "Made by OVT Converter from %s and %s." % (
                                     os.path.basename(job.binary or "the binary"),
                                     os.path.basename(path)), res.warnings,
                                 outside=job.options.get("patch_outside", "keep") != "skip")
    except xdfpatch.PatchError as exc:
        raise ConvertError(str(exc))
    n = sum(len(p["entries"]) for p in patches)
    nb = sum(len(e["patch"]) for p in patches for e in p["entries"])
    res.values.append("%d patch%s, %d entries, %d bytes." % (
        len(patches), "" if len(patches) == 1 else "es", n, nb))
    # the check: the patches turn the binary into the changed one exactly
    # (in every byte they cover, when bytes outside the tables are left out)
    if job.options.get("patch_outside", "keep") != "skip" and \
            xdfpatch.apply(patches, image[:size]) != bytes(new[:size]):
        res.check.append("The patch does not turn the binary into the changed one.")
    return patches


def _run_extract(job: Job, res: Result, say, ask=None) -> None:
    """The binaries WinOLS projects among the sources embed, written into
    the folder ``job.out`` (default: beside the first project). Options:
    ``extract`` (None: the default ones; ``"all"``; names / keys; or the
    window's ``[{"source", "key", "name"}]``), ``extract_format`` (``raw``,
    ``bin``, ``hex``, ``srec``), ``extract_names`` ({key: file name})."""
    from . import embedded
    o = job.options or {}
    projects = [p for p in job.sources if embedded.images(_read(p), "x")]
    if not projects:
        raise ConvertError("None of the files is a WinOLS project with a binary in it "
                           "(.ols): nothing to extract.")
    out = job.out or os.path.dirname(os.path.abspath(projects[0]))
    if os.path.isfile(out):
        raise ConvertError("Extracting writes into a folder: %s is a file." % out)
    job.out = out
    say("Extracting", 0.1, 0.7)
    fmt = o.get("extract_format") or "raw"
    if fmt not in ("raw", "bin", "hex", "srec"):
        raise ConvertError("Unknown format for the extracted binaries: %r." % fmt)
    done = embedded.extract(projects, out, o.get("extract"), fmt,
                            o.get("extract_names"), int(o.get("record") or 32),
                            inputs=job_inputs(job),
                            may=lambda p: may_write(job, p, ask), skipped=res.skipped)
    if not done and not res.skipped:
        raise ConvertError("Nothing chosen to extract.")
    say.at(0.7)
    for k, (path, e) in enumerate(done):
        say.at(0.7 + 0.3 * k / len(done))
        res.out.append(path)
        res.values.append("%s (0x%X bytes) -> %s" % (e.label, e.size, os.path.basename(path)))
        res.size = max(res.size, e.size)
        if job.self_check:
            back = _read(path)
            if fmt in ("hex", "srec"):
                img = _image.read(back)
                back = img.flat() if img.start == e.start else b""
            if back != e.data:
                res.check.append("%s does not read back as the stored bytes"
                                 % os.path.basename(path))
    res.values.insert(0, "%d binar%s extracted to %s%s." % (
        len(done), "y" if len(done) == 1 else "ies", out,
        "; %d already there, left alone" % len(res.skipped) if res.skipped else ""))
    res.tables_out = 0


def _finish(job: Job, res: Result) -> Result:
    res.report = report(job, res)
    if res.lossy:
        with open(job.out + ".convert.txt", "w", encoding="utf-8") as fh:
            fh.write(res.report)
        res.out.append(job.out + ".convert.txt")
    elif os.path.exists(job.out + ".convert.txt"):
        try:                                  # an earlier run's report is stale
            os.remove(job.out + ".convert.txt")
        except OSError:
            pass
    return res


def _value_sets(paths: List[str], res: Result):
    sets = []
    for p in paths:
        vs = _values.load(_read(p), os.path.splitext(os.path.basename(p))[0])
        res.warnings.extend("%s: %s" % (os.path.basename(p), w) for w in vs.warnings)
        for q in vs.params:
            for n in q.notes:
                res.loss.setdefault(q.name, []).append(n)
        sets.append(vs)
    return _values.merge(sets) if len(sets) > 1 else sets[0]


def _confirmed_only(defn: Definition, loss: Dict[str, List[str]], what: str) -> set:
    """Drop the tables a port could not confirm: values are never read from
    or written to a place nothing confirms. The names dropped, lower case."""
    keep, aside = [], set()
    for t in defn.tables:
        if getattr(t, "placement", "") == "unverified":
            loss.setdefault(t.symbol or t.name, []).append(
                "its place in this binary is unverified -- not %s" % what)
            aside.update({(t.symbol or t.name).lower(), t.name.lower()})
        else:
            keep.append(t)
    defn.tables = keep
    return aside


def _filter_values(vs, filters: Dict) -> None:
    import re
    pat = filters.get("pattern") if filters else None
    if not pat:
        return
    rx = re.compile(pat, re.I)
    vs.params = [p for p in vs.params
                 if rx.search(p.name) or rx.search(p.description or "")
                 or (p.display and rx.search(p.display))]


def _binary_for(job: Job, kinds: Dict[str, List[str]], what: str) -> Job:
    """``job`` with only its definitions as sources and its binary settled:
    the one given, else an image among the sources."""
    binary = job.binary
    if not binary and kinds["image"]:
        if len(kinds["image"]) > 1:
            raise ConvertError("More than one image among the sources -- give the "
                               "one to %s as the binary." % what)
        binary = kinds["image"][0]
    if not kinds["definition"]:
        raise ConvertError("To %s, add the definition that says where each value "
                           "is (A2L, DAMOS, XDF, OLS, KP ...)." % what)
    return Job.from_dict(dict(job.to_dict(), sources=kinds["definition"], binary=binary))


def _run_diff(job: Job, kinds, res: Result, say) -> None:
    """Two definitions compared, each placed in image offsets (on the
    binary when one is given, ported onto it with the port option)."""
    import csv
    import io
    from . import transforms
    defs = kinds["definition"]
    if len(defs) != 2 or kinds["values"]:
        raise ConvertError("A comparison takes two definitions (and optionally the "
                           "binary to place them on); %d given." % len(defs))
    sides = []
    for i, src in enumerate(defs):
        say("Reading %s" % os.path.basename(src), 0.45 * i)
        j = Job.from_dict(dict(job.to_dict(), sources=[src]))
        d, base, size, _img, loss, n = prepare(j, res.warnings, say.part(0.45 * i, 0.45 * (i + 1)),
                                               res.placement)
        if job.filters:
            d.tables = transforms.filter_tables(d.tables, job.filters)
        res.tables_in += len(d.tables)
        res.base, res.size = base, size
        sides.append(d)
    say("Comparing", 0.9, 1.0)
    rows = transforms.diff(sides[0], sides[1])
    res.diff = rows
    names = [os.path.basename(p) for p in defs]
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["change"]] = counts.get(r["change"], 0) + 1
    res.values.append("%d tables in %s, %d in %s: %s." % (
        len(sides[0].tables), names[0], len(sides[1].tables), names[1],
        ", ".join("%d %s" % (counts[k], k) for k in transforms.DIFF_KINDS if k in counts)
        or "no differences"))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["name", "change", names[0], names[1]])
    for r in rows:
        w.writerow([r["name"], r["change"], r["old"], r["new"]])
    payload = buf.getvalue().encode("utf-8")
    with open(job.out, "wb") as fh:
        fh.write(payload)
    res.out.append(job.out)
    res.tables_out = len(rows)
    if job.self_check:
        back = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
        if len(back) != len(rows) + 1:
            res.check.append("The written list does not read back: %d rows of %d"
                             % (len(back) - 1, len(rows)))


def _run_values(job: Job, tgt: str, kinds, res: Result, say) -> None:
    """A value file: exported from a definition and a binary, or converted
    from other value files."""
    from . import transforms
    if kinds["definition"]:
        if kinds["values"]:
            raise ConvertError("Give either a definition and a binary (to export "
                               "values) or value files (to convert them), not both.")
        dj = _binary_for(job, kinds, "export values from")
        say("Reading", 0.0)
        defn, base, size, image, loss, n_in = prepare(dj, res.warnings, say.part(0.0, 0.5),
                                                      res.placement)
        if image is None:
            raise ConvertError("Exporting values needs the binary they are read "
                               "from: give it as the binary.")
        res.tables_in, res.base, res.size = n_in, base, size
        res.loss.update(loss)
        _confirmed_only(defn, res.loss, "read")
        if job.filters:
            defn.tables = transforms.filter_tables(defn.tables, job.filters)
        if job.translate:
            from . import translate
            translate.apply(defn, job.translate, res.warnings)
        say("Reading the values", 0.5, 0.7)
        vs = _values.export(defn, size, image, res.loss)
        vs.origin["a2l"] = os.path.basename(dj.sources[0])
        if dj.binary:
            vs.origin["image"] = os.path.basename(dj.binary)
        res.values.append("%d values read from the binary." % len(vs.params))
    elif kinds["values"]:
        say("Reading", 0.0)
        vs = _value_sets(kinds["values"], res)
        res.tables_in = len(vs.params)
        _filter_values(vs, job.filters)
    else:
        raise ConvertError("Nothing to take values from: add value files (DCM, "
                           "CDFX), or a definition and its binary.")
    if job.title:
        vs.title = job.title
    say("Writing", 0.7, 0.85 if job.self_check else 1.0)
    payload = _values.dumps(vs, tgt, source=", ".join(os.path.basename(p) for p in job.sources))
    with open(job.out, "wb") as fh:
        fh.write(payload)
    res.out.append(job.out)
    res.tables_out = len([p for p in vs.params if not (tgt == "dcm" and p.kind == "cuboid")])
    for w in vs.warnings:
        if w not in res.warnings:
            res.warnings.append(w)
    if job.self_check:
        say("Checking the result", 0.85, 1.0)
        try:
            back = _values.load(payload)
            want = _values.ValueSet(params=[p for p in vs.params
                                            if not (tgt == "dcm" and p.kind == "cuboid")])
            res.check = _values.differences(want, back, 1e-9)[:200]
        except Exception as exc:                # noqa: BLE001 - reported
            res.check = ["The written file does not read back: %s" % exc]


def _run_apply(job: Job, tgt: str, kinds, res: Result, say) -> None:
    """Values from DCM / CDFX files written into a binary, through the
    definition that says where each one is."""
    dj = _binary_for(job, kinds, "apply values to")
    say("Reading", 0.0)
    defn, base, size, image, loss, n_in = prepare(dj, res.warnings, say.part(0.0, 0.5),
                                                  res.placement)
    if dj.binary:
        src_img = _image.read(_read(dj.binary))
    elif image is not None and defn.type == "ols":
        # a WinOLS project's own binary: the values go into a copy of it
        src_img = _image.Image([(0, bytes(image[:size]))], source="bin")
    else:
        raise ConvertError("Applying values needs the binary to write them into: "
                           "give it as the binary.")
    res.base, res.size, res.tables_in = base, size, n_in
    for k, v in loss.items():
        res.loss.setdefault(k, []).extend(v)
    aside = _confirmed_only(defn, res.loss, "written")
    vs = _value_sets(kinds["values"], res)
    if aside:
        vs.params = [p for p in vs.params
                     if _values._index(p.name)[0].lower() not in aside]
    _filter_values(vs, job.filters)
    say("Applying the values", 0.5, 0.7)
    data, rep = _values.apply(vs, defn, size, image)
    res.values.extend(rep.lines())
    for name in rep.not_found:
        res.loss.setdefault(name, []).append("not in the definition -- not applied")
    for name, why in rep.skipped.items():
        res.loss.setdefault(name, []).append(why + " -- not applied")
    res.tables_out = rep.applied
    start = src_img.start if src_img.source in ("hex", "srec") else (
        job.base if job.base is not None else 0)
    img = _image.Image([(start, data)], src_img.fill, src_img.source)
    if len(src_img.segments) > 1:
        # a sparse HEX / S-record keeps its own segments: its gaps are not
        # turned into fill-byte records for a flasher to program -- unless
        # a value landed in one
        segs = [(a, data[a - start:a - start + len(b)])
                for a, b in src_img.segments if a - start < len(data)]
        kept = _image.Image(segs, src_img.fill, src_img.source)
        if kept.flat(start, start + len(data)) == bytes(data):
            img = kept
        else:
            res.warnings.append("A value was written between the binary's "
                                "segments; the gaps are written out filled.")
    if job.options.get("checksum"):
        img = _correct_checksums(img, res.warnings)
    say("Writing", 0.7, 0.85 if job.self_check else 1.0)
    payload = _image.write(img, tgt, job.options.get("record"))
    with open(job.out, "wb") as fh:
        fh.write(payload)
    res.out.append(job.out)
    if job.self_check:
        say("Checking the result", 0.85, 1.0)
        back = _image.read(payload, img.start).flat(img.start, img.end)
        again, rep2 = _values.apply(vs, defn, size, back)
        if rep2.cells_changed:
            res.check.append("%d cells do not read back as written (%s)" % (
                rep2.cells_changed, ", ".join(rep2.changed[:10])))


def write(defn: Definition, tgt: str, size: int, image: Optional[bytes],
          job: Job, res: Result) -> Tuple[bytes, int]:
    """The target file's bytes and how many tables went in. ``defn`` is in
    image offsets."""
    from .. import branding
    title = defn.title or "Converted definition"
    if tgt == "json":
        return fmt_json.dumps(defn, base=0, image_size=size).encode("utf-8"), len(defn.tables)
    if tgt == "csv":
        return fmt_csv.dumps(defn).encode("utf-8"), len(defn.tables)
    if tgt in ("ecuflash", "romraider"):
        fn = fmt_romxml.dumps_ecuflash if tgt == "ecuflash" else fmt_romxml.dumps_romraider
        text, n = fn(defn, title, res.loss)
        if not defn.info.get("rom_internalidaddress"):
            res.warnings.append("%s identifies a ROM by an id string at an id address; "
                                "neither is known here, so choose the definition by hand "
                                "when opening the binary." % NAMES[tgt].split()[0])
        return text.encode("utf-8"), n
    if tgt == "dam":
        from . import fmt_dam
        warn: List[str] = []
        text, n = fmt_dam.dumps(defn, size, image, warn, res.loss)
        res.warnings.extend(warn)
        return text.encode("latin-1", "replace"), n
    if tgt == "kp":
        from . import kp_passthrough
        if kp_passthrough.applies(defn, job):
            # a WinOLS file's own records and folders, as they are
            warn: List[str] = []
            data, n = kp_passthrough.write(defn, job, size, warn,
                                           filtered=bool(job.filters))
            _split_warnings(warn, res)
            res.passthrough = True
            return data, n
    ents = _entities(defn, 0, size, image, None, job.dynamic, res.loss)
    le = defn.byte_order() == "<"
    warn = []
    if tgt == "xdf":
        from ..xdf_export import export_xdf
        src = defn.info.get("source_type", "")
        if src in ("ols", "kp"):
            _winols_titles(ents, src.upper())
        text = export_xdf(
            ents, title, fileversion=defn.info.get("fileversion", ""),
            image_size=size, little_endian=le, platform="",
            tiers=False, include_indexed=bool(job.options.get("indexed", True)),
            description=("Converted by %s %s from %s. %s" % (
                branding.APP_NAME, branding.__version__,
                NAMES.get(src, src or "a definition"),
                defn.info.get("checksum_note", ""))).strip(),
            checksums=defn.checksums, patches=defn.patches)
        if defn.info.get("category_order"):
            # a WinOLS project's categories: code blocks, then folders by name
            from .xdfcats import reorder
            text = reorder(text, defn.info["category_order"].split("\n"))
        _xdf_losses(ents, res.loss)
        return text.encode("utf-8"), len(ents)
    if tgt == "a2l":
        from ..a2l_export import export_a2l
        ecu = job.options.get("a2l_addresses") == "ecu"
        a2l_base = res.base if ecu else 0
        if defn.info.get("source_type", "") in ("ols", "kp"):
            # a WinOLS map's group is its folder (after its code block, as
            # in an XDF), not the first category alone -- which in a project
            # with several code blocks is the block, losing the folder
            for e in ents:
                t = getattr(e, "_tabledef", None)
                if t is not None and t.categories:
                    e.category_path = list(t.categories)
        text = export_a2l(ents, title, size, le, platform="", warnings=warn,
                          tiers=False, base=a2l_base)
        _split_warnings(warn, res)
        return text.encode("utf-8"), len(ents)
    if tgt == "kp":
        from ..definitions.winols_write import write_kp
        data = write_kp(ents, size, title, le, warnings=warn, tiers=False)
        _split_warnings(warn, res)
        res.warnings.append("WinOLS map pack export is experimental: it reads back "
                            "in %s; whether WinOLS opens it is not yet tested."
                            % branding.APP_NAME)
        return data, len(ents)
    raise ConvertError("No writer for %r." % tgt)


def _split_warnings(warn: List[str], res: Result) -> None:
    """``"name: what"`` warnings -> per-table loss; the rest stay warnings."""
    for w in warn:
        name, sep, what = w.partition(": ")
        if sep:
            res.loss.setdefault(name, []).append(what)
        else:
            res.warnings.append(w)


def _xdf_losses(ents, loss) -> None:
    from ..equation import Equation
    for e in ents:
        t = getattr(e, "_tabledef", None)
        for tag, ad in (("X", t.x if t else None), ("Y", t.y if t else None)):
            if ad is not None and ad.labels and ad.values and ad.values.addr is not None:
                loss.setdefault(e.name, []).append(
                    "%s axis point texts are not in an XDF stored axis" % tag)


# ------------------------------------------------------------------ images
def _run_image(job: Job, res: Result) -> None:
    imgs = []
    for src in job.sources:
        raw = _read(src)
        kind, t = sniff(raw)
        if kind == "definition":
            defn = load_definition(raw)
            if not defn.images:
                raise ConvertError("%s is a definition with no image in it."
                                   % os.path.basename(src))
            label, data, base = defn.images[0]
            imgs.append(_image.Image([(base, bytes(data))], source="ols"))
        else:
            base = job.base if (t == "bin" and job.base is not None) else 0
            imgs.append(_image.read(raw, base))
    img = imgs[0] if len(imgs) == 1 else _image.merge(imgs)
    o = job.options
    if o.get("crop"):
        img = img.crop(int(o["crop"][0]), int(o["crop"][1]))
    if o.get("rebase") is not None:
        img = img.rebased(int(o["rebase"]))
    if "fill" in o:
        img.fill = int(o["fill"]) & 0xFF
    if job.size:
        img = img.pad(job.size)
    if o.get("swap16"):
        img = img.swapped16()
    if o.get("checksum"):
        img = _correct_checksums(img, res.warnings)
    payload = _image.write(img, job.target, o.get("record"))
    with open(job.out, "wb") as fh:
        fh.write(payload)
    res.out.append(job.out)
    res.base, res.size = img.start, img.end - img.start
    if job.self_check:
        back = _image.read(payload, img.start)
        if back.flat(img.start, img.end) != img.flat():
            res.check.append("The written image does not read back the same.")


def _correct_checksums(img, warnings: List[str]):
    """The image with its checksums corrected by the installed module for
    its platform (the base app holds no checksum maths). Nothing changes,
    and a note says why, when no module can."""
    from .checksums import platform_of
    flat = img.flat()
    pid = platform_of(flat)
    if pid is None:
        warnings.append("Checksums not corrected: the image's platform is not "
                        "recognised.")
        return img
    try:
        from ..modules import registry
        prov = registry.checksum(pid)
    except Exception as exc:                # noqa: BLE001 - a module's fault
        warnings.append("Checksums not corrected: %s" % exc)
        return img
    if prov is None:
        warnings.append("Checksums not corrected: no installed module corrects "
                        "%s images." % pid)
        return img
    if not prov.is_supported(flat):
        warnings.append("Checksums not corrected: %s" % prov.unsupported_text(flat))
        return img
    fixed = bytes(prov.correct(flat))
    warnings.append("Checksums corrected (%s)." % pid if fixed != flat else
                    "Checksums already correct (%s)." % pid)
    return _image.Image([(img.start, fixed)], img.fill, img.source)


# ------------------------------------------------------------------ checking
def _keys(ents) -> Dict[tuple, str]:
    """Each entity by where and how its cells lie. A column-major map is
    the same bytes as the row-major map with its axes swapped (how WinOLS
    stores one), so both compare equal."""
    out = {}
    for e in ents:
        rows, cols = (1, 1) if e.kind == "constant" else (e.rows, e.cols)
        x = e.x.addr if e.x is not None else None
        y = e.y.addr if e.y is not None else None
        if e.kind == "map" and getattr(e, "order", "row") == "col":
            rows, cols, x, y = cols, rows, y, x
        if e.kind in ("indexed", "curve") and min(rows, cols) == 1:
            rows, cols = rows * cols, 1         # a 1-D block either way round
        k = (e.addr, e.bits, bool(getattr(e, "is_float", False)), rows, cols, x, y)
        out[k] = e.name
    return out


def self_check(defn: Definition, tgt: str, payload: bytes, size: int,
               image: Optional[bytes], job: Job,
               loss: Optional[Dict[str, List[str]]] = None,
               base: Optional[int] = None) -> List[str]:
    """Read the written file back with OVT's own reader and compare every
    table's place, width and dimensions with what was written (a table the
    writer reported leaving out is not expected back)."""
    left = {k for k, v in (loss or {}).items() if any("left out" in m for m in v)}
    try:
        back = load_definition(payload)
    except Exception as exc:
        return ["The written file does not read back: %s" % exc]
    want = [e for e in _entities(defn, 0, size, image, None, job.dynamic)
            if e.name not in left]
    if tgt == "xdf" and not job.options.get("indexed", True):
        want = [e for e in want if e.kind != "indexed"]
    w: List[str] = []
    bbase, bsize, _ = place(back, image, base, size, w)
    got = _entities(back, bbase, bsize, image, None, job.dynamic)
    a, b = _keys(want), _keys(got)
    out = []
    if tgt == "xdf":
        def flat(ps):
            return [(e["address"], bytes(e["patch"]), bytes(e["base"] or b""))
                    for p in ps for e in p["entries"]]
        if flat(defn.patches) != flat(back.patches):
            out.append("The XDF patches do not read back the same")
    for k in a.keys() - b.keys():
        out.append("%s (at 0x%X) does not read back the same" % (a[k], k[0]))
    for k in b.keys() - a.keys():
        out.append("%s (at 0x%X) reads back but was not written so" % (b[k], k[0]))
    out.sort()
    if len(out) > 200:
        out = out[:200] + ["... and %d more" % (len(out) - 200)]
    return out


def report(job: Job, res: Result) -> str:
    from .. import branding
    lines = ["%s converter report" % branding.APP_NAME, "",
             "Sources: %s" % ", ".join(job.sources),
             "Target:  %s (%s)" % (job.out, NAMES.get(job.target, job.target))]
    if res.skipped and job.target != EXTRACT:
        lines += ["", "Skipped: %s was already there and was left alone (not "
                  "overwritten)." % job.out]
        return "\n".join(lines) + "\n"
    if job.binary:
        lines.append("Binary:  %s" % job.binary)
    if job.target != EXTRACT:
        lines.append("Image:   0x%X bytes, offset 0 = definition address 0x%X"
                     % (res.size, res.base))
    if res.tables_in or res.tables_out:
        lines.append("Tables:  %d read, %d written" % (res.tables_in, res.tables_out))
    if res.placement:
        lines.append("Placed:  " + ", ".join("%d %s" % (n, g) for g, n in res.placement.items() if n))
    if res.values:
        lines += ["", "Values:" if not res.diff else "Differences:"] + \
            ["  - " + v for v in res.values]
    if res.warnings:
        lines += ["", "Notes:"] + ["  - " + w for w in res.warnings]
    if res.loss:
        lines += ["", "Not expressed fully (%d tables):" % len(res.loss)]
        for name in sorted(res.loss):
            for what in res.loss[name]:
                lines.append("  - %s: %s" % (name, what))
    if res.skipped:
        lines += ["", "Already there, left alone (not overwritten):"] + \
            ["  - " + p for p in res.skipped]
    if job.self_check:
        lines += ["", "Read-back check: " + ("passed" if not res.check else
                                            "%d differences" % len(res.check))]
        lines += ["  - " + c for c in res.check]
    return "\n".join(lines) + "\n"
