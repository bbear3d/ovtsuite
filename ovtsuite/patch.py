# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch generation and application, across binaries of the same ECU type.

Two identified binaries (a primary/baseline and a comparison) are diffed BY
KB IDENTITY -- the same ``TunerSession.counterpart`` pairing the GUI's live
comparison already uses -- never by raw byte address, so the diff works
across two different sub-families with different internal layouts. The
resulting candidates are named parameters (tables/curves/constants) plus, for
data no KB entry covers, literal address ranges ("undefined blocks").

The user selects which candidates to keep; the saved :class:`Patch` carries
each selected parameter by its KB name and its captured values in DISPLAY
units, not by address. Applying a patch to a TARGET binary resolves every
entry by name against the target's OWN identified entities -- the target's
own addresses, its own equation, its own bit width and its own table shape,
never the source's. A table whose shape or bit width differs between source
and target is reconciled with the exact machinery the GUI's live comparison
overlay already uses (:func:`ovtsuite.transfer.build_overlay`, including the
curated ZF TCC column mapping in ``zf6hp.view6hp.compare_columns`` -- generic
interpolation across a label-indexed axis silently scrambles values, as the
project has already found the hard way).

A target entity placed only by a plausible guess (``source`` not in
``model.TRUSTED_PLACEMENT`` -- bracketed/projected) is not written unless the
caller says so via ``dispositions``: per entry this is a discard / apply /
"selective" choice, where "selective" is simply setting each entry's own key
in ``dispositions`` individually rather than relying on the patch-wide
default.

Undefined-data blocks carry no KB identity -- there is nothing else to key
them on -- so they are always address-literal. Applying one to a target of a
DIFFERENT detected family is allowed (the caller may know two families share
layout even without a formal KB relationship) but is flagged, both in the
returned :class:`ApplyResult` and by requiring the same disposition
confirmation as an untrusted parameter.

Application goes entirely through ``TunerSession.transaction()`` /
``edit_cells`` / ``edit_bytes``, so it is one undoable ``CompoundCommand``,
participates in the change log, and saves atomically -- there is no separate
write path here.
"""

import json
import re
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from .model import Entity, placement_trusted, values_equal
from .session import Command
from .transfer import build_overlay

#: Prefix marking a machine-readable patch record inside a change-log line
#: (see ``_log_patch_record``/``list_applied_patches``/``revert_applied_patch``
#: below). Chosen so it also reads sensibly in the plain-text ``.log`` file a
#: human opens beside the saved binary.
PATCH_LOG_MARKER = "PATCH "

DISCARD = "discard"
APPLY = "apply"

FORMAT_VERSION = 1


class Incompatible(Exception):
    """A patch entry could not be reconciled onto a target entity's shape."""


def _fmt(v) -> str:
    if isinstance(v, float):
        return "%.4g" % v
    return str(v)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class PatchEntry:
    """One parameter (table/curve/constant), captured from the COMPARISON
    binary, keyed by its stable KB name. ``values``/``x_axis``/``y_axis`` are
    the comparison entity's own shape and breakpoints -- the shape a patch
    was CAPTURED at, not necessarily the shape it will be applied to."""

    name: str
    kind: str                       # 'map' | 'curve' | 'indexed' | 'constant'
    rows: int
    cols: int
    values: List[List[float]]
    x_axis: List[float] = field(default_factory=list)
    y_axis: List[float] = field(default_factory=list)
    units: str = ""
    label: str = ""
    description: str = ""
    categories: List[str] = field(default_factory=list)
    tier: int = 0
    source_grade: str = ""          # comparison entity's own provenance
    changed_cells: Optional[int] = None   # None when the two shapes differ
    #: axis breakpoints that differ (same shape only). Nonzero means the
    #: captured ``x_axis`` / ``y_axis`` are part of the change and are written
    #: on apply; a patch from before this field (0) writes cells only, as it
    #: always did.
    changed_axes: int = 0
    total_cells: int = 0
    first_change: str = ""
    selected: bool = True

    def title(self) -> str:
        return self.label or self.name

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PatchEntry":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class BlockEntry:
    """A literal, address-keyed byte range -- undefined data with no KB
    identity, captured verbatim from the COMPARISON binary (the side a patch
    goes TO, like every :class:`PatchEntry`), at the PRIMARY's address
    layout (``source_family``). Applying one is
    always address-literal: there is no reconciliation possible without an
    identity to reconcile by."""

    addr: int
    length: int
    data: bytes
    note: str = ""
    source_family: str = ""
    selected: bool = True

    def to_dict(self) -> dict:
        return {"addr": self.addr, "length": self.length,
               "data": self.data.hex(), "note": self.note,
               "source_family": self.source_family, "selected": self.selected}

    @classmethod
    def from_dict(cls, d: dict) -> "BlockEntry":
        return cls(addr=d["addr"], length=d["length"],
                   data=bytes.fromhex(d["data"]), note=d.get("note", ""),
                   source_family=d.get("source_family", ""),
                   selected=d.get("selected", True))


@dataclass
class Patch:
    version: int = FORMAT_VERSION
    created: str = ""
    source_family: str = ""
    source_name: str = ""
    comparison_family: str = ""
    comparison_name: str = ""
    entries: List[PatchEntry] = field(default_factory=list)
    blocks: List[BlockEntry] = field(default_factory=list)

    # -- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return {"version": self.version, "created": self.created,
               "source_family": self.source_family,
               "source_name": self.source_name,
               "comparison_family": self.comparison_family,
               "comparison_name": self.comparison_name,
               "entries": [e.to_dict() for e in self.entries],
               "blocks": [b.to_dict() for b in self.blocks]}

    @classmethod
    def from_dict(cls, d: dict) -> "Patch":
        return cls(version=d.get("version", 1), created=d.get("created", ""),
                   source_family=d.get("source_family", ""),
                   source_name=d.get("source_name", ""),
                   comparison_family=d.get("comparison_family", ""),
                   comparison_name=d.get("comparison_name", ""),
                   entries=[PatchEntry.from_dict(e) for e in d.get("entries", [])],
                   blocks=[BlockEntry.from_dict(b) for b in d.get("blocks", [])])

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1)

    @classmethod
    def load(cls, path: str) -> "Patch":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # -- reporting ---------------------------------------------------------
    def selected_entries(self) -> List[PatchEntry]:
        return [e for e in self.entries if e.selected]

    def selected_blocks(self) -> List[BlockEntry]:
        return [b for b in self.blocks if b.selected]

    def trust_summary(self) -> Dict[str, int]:
        """Count of selected entries by the CAPTURED (comparison) side's own
        provenance grade -- an at-a-glance read on how much of this patch is
        corroborated evidence vs. a projected guess, before anyone applies
        it. The target's own grade is judged separately, at apply time, since
        it can differ from the family this patch was captured against."""
        out: Dict[str, int] = {}
        for e in self.selected_entries():
            out[e.source_grade] = out.get(e.source_grade, 0) + 1
        return out


# ---------------------------------------------------------------------------
# diff (generation)
# ---------------------------------------------------------------------------

def diff_entities(primary, comparison, entities=None) -> List[PatchEntry]:
    """Candidate entries for every ``primary`` entity that differs from its
    counterpart in ``comparison``, paired by KB identity
    (``TunerSession.counterpart`` -- never by address).

    ``entities`` defaults to ``primary.entities`` only. Variant/secondary
    entities are deliberately excluded by default: their names embed the
    address they were found at (``"<name> (variant @0x...)"``), which breaks
    the whole cross-binary identity model a patch depends on -- a caller that
    passes them explicitly is responsible for that.
    """
    if entities is None:
        entities = list(primary.entities)
    out = []
    for e in entities:
        cp = primary.counterpart(comparison, e)
        if cp is None:
            continue
        entry = _candidate(e, cp)
        if entry is not None:
            out.append(entry)
    return out


def _candidate(e: Entity, cp: Entity) -> Optional[PatchEntry]:
    if e.kind == "constant":
        a, b = e.cell(0, 0), cp.cell(0, 0)
        if values_equal(a, b, 1e-9):
            return None
        return PatchEntry(name=e.name, kind="constant", rows=1, cols=1,
                          values=[[b]], units=e.units, label=e.label,
                          description=e.description,
                          categories=list(e.categories), tier=e.tier,
                          source_grade=cp.source, changed_cells=1,
                          total_cells=1,
                          first_change="%s -> %s" % (_fmt(a), _fmt(b)))

    same_shape = (e.rows, e.cols) == (cp.rows, cp.cols)
    cvals = cp.values()
    axes = []
    if same_shape:
        pvals = e.values()
        n = 0
        first = ""
        for r in range(e.rows):
            for c in range(e.cols):
                if not values_equal(pvals[r][c], cvals[r][c], 1e-9):
                    n += 1
                    if not first:
                        first = "[%d,%d] %s -> %s" % (
                            r, c, _fmt(pvals[r][c]), _fmt(cvals[r][c]))
        # The axes sit outside the Z bytes: a map whose only edit is its
        # breakpoints is a change too (it used to show up as unnamed bytes).
        from .diff import axis_changes
        axes = axis_changes(e, cp)
        if n == 0 and not axes:
            return None
        if axes:
            ax, i, a, b = axes[0]
            note = "%s axis[%d] %s -> %s" % (ax.upper(), i, _fmt(a), _fmt(b))
            if len(axes) > 1:
                note += " (+%d more)" % (len(axes) - 1)
            first = "%s; %s" % (first, note) if first else note
        changed_cells = n
    else:
        changed_cells = None
        first = "%dx%d -> %dx%d (structure differs)" % (
            e.rows, e.cols, cp.rows, cp.cols)

    return PatchEntry(name=e.name, kind=e.kind, rows=cp.rows, cols=cp.cols,
                      values=cvals, x_axis=cp.x_values(), y_axis=cp.y_values(),
                      units=e.units, label=e.label, description=e.description,
                      categories=list(e.categories), tier=e.tier,
                      source_grade=cp.source, changed_cells=changed_cells,
                      changed_axes=len(axes),
                      total_cells=cp.rows * cp.cols, first_change=first)


def _subtract_ranges(ranges: List[Tuple[int, int]],
                     exempt: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """``ranges`` with every ``exempt`` span carved out -- splitting a range
    that only partially overlaps one, dropping it entirely if fully
    covered. Used to keep checksum/digest/signature bytes out of a raw
    byte-range diff without disturbing genuine differences that merely sit
    next to one."""
    if not exempt:
        return list(ranges)
    exempt = sorted(exempt)
    out: List[Tuple[int, int]] = []
    for s, e in ranges:
        cur = [(s, e)]
        for ex_s, ex_e in exempt:
            nxt = []
            for cs, ce in cur:
                if ex_e <= cs or ex_s >= ce:
                    nxt.append((cs, ce))
                    continue
                if ex_s > cs:
                    nxt.append((cs, ex_s))
                if ex_e < ce:
                    nxt.append((ex_e, ce))
            cur = nxt
        out.extend(cur)
    return out


def _checksum_exempt(session) -> List[Tuple[int, int]]:
    """``session.checksum_exempt_ranges()``, tolerating a session type (or a
    test double) that doesn't have it."""
    try:
        return session.checksum_exempt_ranges()
    except Exception:
        return []


def _region_exempt_ranges(name: str, data: bytes) -> List[Tuple[int, int]]:
    """Checksum-exempt ranges for one flash region's OWN raw bytes, by the
    region name ``TunerSession.region_buffers()`` uses -- so MPC/EEPROM
    checksums are excluded regardless of which container format (.dim /
    .sgo / .edc16) supplied the bytes."""
    try:
        if name == "MPC (internal flash)":
            # The EDC16 module's checksum layout; without it nothing is exempt
            from .modules import registry as _registry
            cs = _registry.checksum("edc16")
            return cs.exempt_ranges(data) if cs is not None else []
        if name == "EEPROM":
            from .modules import registry as _registry
            cs = _registry.checksum("edc16")
            return cs.exempt_ranges(data, region="eeprom") if cs is not None else []
    except Exception:
        pass
    return []


def _axis_spans(entities) -> List[Tuple[int, int]]:
    """``[(start, end)]`` of the axis breakpoints the entities store in the
    image."""
    out = []
    for e in entities:
        for ax in (getattr(e, "x", None), getattr(e, "y", None)):
            a = getattr(ax, "addr", None)
            n = int(getattr(ax, "count", 0) or 0)
            if a is None or n <= 0:
                continue
            w = 4 if getattr(ax, "is_float", False) else max(
                1, int(getattr(ax, "bits", 16) or 16) // 8)
            out.append((a, a + n * w))
    return out


def diff_gaps(primary, comparison, *,
              exclude_checksums: bool = True) -> List[Tuple[int, int]]:
    """Byte ranges inside ``primary``'s own calibration region where the two
    binaries differ but no identified entity's address span covers the
    range -- the undefined-data candidates (the hp6tuner
    "undocumented differences" idea, also used by ``ovtsuite.diff``).

    Address-literal by nature, so only meaningful between two binaries that
    share PRIMARY's own address layout (same family or sub-family); returns
    ``[]`` for a length mismatch rather than guessing.

    ``exclude_checksums`` (default True) drops the checksum/digest/
    signature bytes ``primary.checksum_exempt_ranges()`` reports -- a
    checksum trailer recomputes on its own whenever ANYTHING in its block
    changes, so it is never a meaningful "undefined data" candidate to
    carry into a patch.
    """
    a = bytes(primary.binfile.data)
    b = bytes(comparison.binfile.data)
    if len(a) != len(b):
        return []
    region = getattr(primary, "region", (0, len(a)))
    from .diff import _byte_ranges
    spans = sorted((e.addr, e.end) for e in primary.entities
                   if getattr(e, "addr", None) is not None)
    # An entity's axes are stored outside [addr, end) -- before the map on
    # EDC16 and ZF -- and belong to it just the same: a changed breakpoint
    # is that parameter's change (diff_entities), not undefined data.
    spans = sorted(spans + _axis_spans(primary.entities))
    merged: List[Tuple[int, int]] = []
    for s0, s1 in spans:                # overlapping spans as one, for bisect
        if merged and s0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], s1))
        else:
            merged.append((s0, s1))
    spans = merged
    starts = [s for s, _ in spans]
    import bisect

    def _covering(pos):
        i = bisect.bisect_right(starts, pos) - 1
        if 0 <= i < len(spans) and spans[i][0] <= pos < spans[i][1]:
            return spans[i][1]
        return None

    gaps: List[Tuple[int, int]] = []
    for ds, de in _byte_ranges(a, b, region[0], region[1]):
        pos = ds
        while pos < de:
            end = _covering(pos)
            if end is not None:
                pos = min(de, end)
                continue
            nxt = de
            j = bisect.bisect_right(starts, pos)
            if j < len(starts):
                nxt = min(nxt, starts[j])
            if gaps and gaps[-1][1] == pos:
                gaps[-1] = (gaps[-1][0], nxt)
            else:
                gaps.append((pos, nxt))
            pos = nxt
    if exclude_checksums:
        gaps = _subtract_ranges(gaps, _checksum_exempt(primary))
    return gaps


#: Every block :func:`diff_outside_calibration` can report on -- pass a
#: subset via its ``blocks`` argument to look at just one or two.
PROGRAM_BLOCK = "Program/boot area"
MPC_BLOCK = "MPC (internal flash)"
EEPROM_BLOCK = "EEPROM"
ALL_BLOCKS = (PROGRAM_BLOCK, MPC_BLOCK, EEPROM_BLOCK)


def diff_outside_calibration(primary, comparison, *, blocks=None,
                             exclude_checksums: bool = True
                             ) -> List[Tuple[str, int, int]]:
    """Raw contiguous byte ranges that differ OUTSIDE ``primary``'s own
    calibration region -- the program/boot code sharing the same external-
    flash image -- plus, for whichever flash-container format either side
    was loaded from (.dim / .sgo / .edc16), the separate MPC (internal
    flash) and EEPROM regions those containers can carry. Those regions
    live in their own blobs entirely apart from ``binfile.data`` and are
    never covered by any identified entity either way; they are read
    through :meth:`TunerSession.region_buffers`, the SAME uniform
    container-format abstraction the hex editor's region switcher already
    uses, so a new container format only has to teach itself to
    ``region_buffers`` once to show up here too.

    ``blocks`` restricts which of :data:`ALL_BLOCKS` to look at (default:
    all of them) -- e.g. ``blocks=[MPC_BLOCK]`` to check only the internal
    flash. ``exclude_checksums`` (default True) drops each region's own
    checksum/digest/signature bytes, the same way :func:`diff_gaps` does
    for the calibration region -- those bytes recompute automatically
    whenever anything else in their block changes and are never a
    meaningful content difference on their own.

    Returns a list of ``(label, addr, length)`` tuples, address-literal like
    :func:`diff_gaps` and meaningful only between two binaries that already
    share this layout -- for a length mismatch in any region, that region is
    simply skipped rather than guessed at (including when one side doesn't
    carry it at all -- e.g. a plain .bin has neither MPC nor EEPROM, and a
    .sgo has MPC but no EEPROM).
    """
    from .diff import _byte_ranges
    active = set(ALL_BLOCKS) if blocks is None else (set(blocks) & set(ALL_BLOCKS))
    rows: List[Tuple[str, int, int]] = []

    if PROGRAM_BLOCK in active:
        a = bytes(primary.binfile.data)
        b = bytes(comparison.binfile.data)
        if len(a) == len(b):
            region = getattr(primary, "region", (0, len(a)))
            prog_ranges: List[Tuple[int, int]] = []
            for lo, hi in ((0, region[0]), (region[1], len(a))):
                if hi <= lo:
                    continue
                prog_ranges.extend(_byte_ranges(a, b, lo, hi))
            if exclude_checksums:
                prog_ranges = _subtract_ranges(prog_ranges, _checksum_exempt(primary))
            for ds, de in prog_ranges:
                rows.append((PROGRAM_BLOCK, ds, de - ds))

    if ((MPC_BLOCK in active or EEPROM_BLOCK in active)
            and hasattr(primary, "region_buffers")
            and hasattr(comparison, "region_buffers")):
        p_regions = dict(primary.region_buffers())
        c_regions = dict(comparison.region_buffers())
        for name in (getattr(primary, "MPC_REGION", MPC_BLOCK),
                    getattr(primary, "EEPROM_REGION", EEPROM_BLOCK)):
            if name not in active:
                continue
            pa, cb = p_regions.get(name), c_regions.get(name)
            if pa is None or cb is None or len(pa) != len(cb):
                continue
            region_ranges = _byte_ranges(bytes(pa), bytes(cb), 0, len(pa))
            if exclude_checksums:
                region_ranges = _subtract_ranges(
                    region_ranges, _region_exempt_ranges(name, bytes(pa)))
            for ds, de in region_ranges:
                rows.append((name, ds, de - ds))
    return rows


def build_block_entry(primary, comparison, addr: int, end: int,
                      note: str = "") -> BlockEntry:
    """Capture ``[addr, end)`` -- a whole gap from :func:`diff_gaps`, or a
    user-narrowed sub-range of one; both are just an address and a length.

    A patch goes FROM ``primary`` TO ``comparison`` (every
    :class:`PatchEntry` carries the comparison's values), so the block's
    bytes are the COMPARISON's too: applying the patch to a copy of the
    primary must turn that range into what the comparison holds, exactly
    like its parameters. The address layout -- and so ``source_family``,
    which the cross-family check in :func:`apply_patch` compares against --
    is the primary's, whose calibration region and entity spans
    :func:`diff_gaps` used to find the range (it only reports ranges when
    both binaries are the same length)."""
    data = bytes(comparison.binfile.data[addr:end])
    return BlockEntry(addr=addr, length=end - addr, data=data, note=note,
                      source_family=getattr(primary, "family", ""))


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

class _Snapshot:
    """Just enough of the ``Entity``/``transfer.build_overlay`` interface to
    stand in for a captured :class:`PatchEntry` -- no live binary needed."""

    def __init__(self, entry: PatchEntry):
        self.kind = entry.kind
        self.rows, self.cols = entry.rows, entry.cols
        self._values = entry.values
        self._x, self._y = entry.x_axis, entry.y_axis

    def values(self) -> List[List[float]]:
        return self._values

    def x_values(self) -> List[float]:
        return self._x

    def y_values(self) -> List[float]:
        return self._y

    def cell(self, r: int, c: int) -> float:
        return self._values[r][c]


def find_entity(session, name: str) -> Optional[Entity]:
    """The entity named ``name`` on ``session``, or ``None``. Public so a
    caller (the GUI's apply dialog, chiefly) can show target-resolution
    status before actually applying."""
    for e in session.entities:
        if e.name == name:
            return e
    # A signature-located constant found more than once is "<name>#<n>"
    # (signatures.resolve). Hand-written patches from before that spelling
    # settled call the same instance "<name>_i<n>" -- the A/C start-condition
    # patch for the P475 did, and so matched nothing on the very LQ/KN it
    # was written for. Same instance, older spelling: accept it.
    m = re.match(r"^(.*)_i(\d+)$", name)
    if m:
        alt = "%s#%s" % (m.group(1), m.group(2))
        for e in session.entities:
            if e.name == alt:
                return e
    return None


def _reconcile(target: Entity, entry: PatchEntry, session) -> List[Tuple[int, int, float]]:
    """Cell writes (target row/col index space, display units) for applying
    ``entry`` onto ``target``. Raises :class:`Incompatible` when the two
    cannot be reconciled at all."""
    if entry.kind == "constant" or target.kind == "constant":
        if not entry.values or not entry.values[0]:
            return []
        return [(0, 0, entry.values[0][0])]

    if (target.rows, target.cols) == (entry.rows, entry.cols):
        return [(r, c, entry.values[r][c])
               for r in range(target.rows) for c in range(target.cols)]

    snap = _Snapshot(entry)
    columns = None
    if getattr(session, "is_zf6hp", False):
        from . import speedview as _view6
        columns = _view6.compare_columns(target, snap)
    overlay = build_overlay(target, snap, columns)
    if overlay is None:
        raise Incompatible(
            "%s: %dx%d captured, %dx%d on this target -- no reconciliation "
            "available" % (entry.name, entry.rows, entry.cols,
                           target.rows, target.cols))
    out = []
    for r in range(target.rows):
        for c in range(target.cols):
            v = overlay.values[r][c]
            if v is not None:
                out.append((r, c, v))
    return out


def _apply_axes(target: Entity, entry: PatchEntry, session) -> str:
    """Write ``entry``'s captured breakpoints onto ``target``'s axes, when
    the shapes match. Returns a warning, or "" when all went in."""
    if (target.rows, target.cols) != (entry.rows, entry.cols):
        return ("%s: its axes changed in the patch, but this target's map is "
                "%dx%d (captured %dx%d), so only its values were written"
                % (entry.name, target.rows, target.cols, entry.rows,
                   entry.cols))
    skipped = 0
    for ax, want in ((getattr(target, "x", None), entry.x_axis),
                     (getattr(target, "y", None), entry.y_axis)):
        if not want:
            continue
        if ax is None or getattr(ax, "addr", None) is None:
            skipped += 1                # an axis the definition supplies
            continue
        have = (target.x_values() if ax is target.x else target.y_values())
        for i, v in enumerate(want[:len(have)]):
            if not values_equal(have[i], v, 1e-9):
                session.edit_axis(ax, i, v)
    if skipped:
        return ("%s: this target has an axis that is not stored in the image; "
                "it was left as it is" % entry.name)
    return ""


@dataclass
class ApplyResult:
    applied: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: id of the change-log record this apply was persisted under (see
    #: ``_log_patch_record``), or None when nothing was actually written --
    #: pass this to a future ``list_applied_patches``/``revert_applied_patch``
    #: call, once the session has been SAVED (the record lives in
    #: ``target.change_log`` until then, flushed to the ``.log`` file at
    #: ``TunerSession.save()`` like every other change-log line).
    record_id: Optional[str] = None
    conflicts: int = 0


def apply_patch(patch: Patch, target, *,
                dispositions: Optional[Dict[str, str]] = None,
                default_disposition: str = DISCARD,
                label: str = "apply patch",
                source_label: Optional[str] = None,
                fix_checksum: bool = True) -> ApplyResult:
    """Write every selected entry/block in ``patch`` into ``target``.

    Addresses and shapes come from resolving each entry BY NAME against
    ``target.entities`` -- never from the patch's own captured addresses, so
    the same patch applies correctly to a binary with a different internal
    layout. One :class:`~ovtsuite.session.CompoundCommand` (via
    ``target.transaction``), so the whole apply is a single undo step and
    either all of it lands or, on an unexpected error, none of it does.

    ``dispositions`` maps an entry name (or, for a block, ``"block@0x%X" %
    addr``) to :data:`DISCARD` or :data:`APPLY`, and governs two things a
    caller must confirm explicitly: a target parameter whose own placement is
    not ``model.TRUSTED_PLACEMENT`` (bracketed/projected -- a plausible
    guess, not an established address), and an undefined-data block being
    applied to a target of a different detected family than it was captured
    from. Anything not present in ``dispositions`` falls back to
    ``default_disposition`` -- pass an explicit per-name entry for a
    "selective" apply, or set ``default_disposition=APPLY`` for a blanket
    "apply anyway".

    Every entry actually touched that already differed from ``target``'s
    pristine, as-loaded bytes (``target.original``) is counted in
    ``ApplyResult.conflicts`` -- this patch is not the first thing to have
    changed it this session, so applying it stomps whatever came before.
    Reported, not blocked: re-running a patch-apply is itself a normal
    workflow.

    ``fix_checksum`` (default True) recomputes the flash checksum right
    after a successful apply, the same correction ``TunerSession.save()``
    already does -- so a patch that would otherwise leave the checksum stale
    doesn't have to wait for a save to become flash-correct in memory too.

    Every write this call actually makes is persisted as one compact, machine
    -readable record in ``target``'s change log (see ``_log_patch_record``),
    which is what lets a LATER session revert this exact apply from the
    binary's own ``.log`` file (once saved) -- see ``list_applied_patches``/
    ``revert_applied_patch`` -- with no need to keep the original ``.patch``
    file around.
    """
    res = ApplyResult()
    dispositions = dispositions or {}
    before = target.undo_stack.top()
    with target.transaction(label):
        for pe in patch.entries:
            if not pe.selected:
                continue
            te = find_entity(target, pe.name)
            if te is None:
                res.skipped.append((pe.name, "not identified in this target"))
                continue
            disp = dispositions.get(pe.name, default_disposition)
            if not placement_trusted(te) and disp != APPLY:
                res.skipped.append(
                    (pe.name, "target address is %r (not established) and "
                              "was not confirmed" % te.source))
                continue
            try:
                cells = _reconcile(te, pe, target)
            except Incompatible as exc:
                res.skipped.append((pe.name, str(exc)))
                continue
            if not cells:
                res.skipped.append((pe.name, "nothing to write for this "
                                              "target's shape"))
                continue
            if bytes(target.binfile.data[te.addr:te.end]) != \
                    bytes(target.original[te.addr:te.end]):
                res.conflicts += 1
            target.edit_cells(te, cells)
            if pe.changed_axes:
                note = _apply_axes(te, pe, target)
                if note:
                    res.warnings.append(note)
            if not placement_trusted(te):
                res.warnings.append(
                    "%s applied at a %r (unestablished) target address, "
                    "confirmed by the caller" % (pe.name, te.source))
            res.applied.append(pe.name)

        for be in patch.blocks:
            if not be.selected:
                continue
            key = "block@0x%X" % be.addr
            cross_family = (getattr(target, "family", "") != be.source_family)
            disp = dispositions.get(key, default_disposition)
            if cross_family and disp != APPLY:
                res.skipped.append(
                    (key, "captured from family %r, this target is %r; "
                          "undefined-data blocks are address-literal and "
                          "not verified across families" %
                          (be.source_family, getattr(target, "family", ""))))
                continue
            if cross_family:
                res.warnings.append(
                    "%s applied across families (%r -> %r) at the caller's "
                    "confirmation; the address is literal, not re-derived"
                    % (key, be.source_family, getattr(target, "family", "")))
            end = be.addr + be.length
            if bytes(target.binfile.data[be.addr:end]) != \
                    bytes(target.original[be.addr:end]):
                res.conflicts += 1
            target.edit_bytes(be.addr, be.data)
            res.applied.append(key)

    if res.conflicts:
        res.warnings.append(
            "%d entrie(s)/block(s) had already changed from the file as "
            "loaded before this patch touched them; the patch overwrote "
            "those" % res.conflicts)

    cmd = target.undo_stack.top()
    if cmd is not None and cmd is not before and cmd.writes:
        rec = _log_patch_record(
            target, source=source_label or patch.comparison_name or "patch",
            writes=cmd.writes, n_entries=len(res.applied) - sum(
                1 for a in res.applied if a.startswith("block@")),
            n_blocks=sum(1 for a in res.applied if a.startswith("block@")))
        res.record_id = rec["id"]

    if fix_checksum and res.applied:
        try:
            _before_cs, changed = target.correct_checksums()
            if changed:
                res.warnings.append(
                    "checksum recomputed after applying this patch")
        except Exception:
            pass          # never fail an apply over an unsupported checksum
    return res


def new_patch(primary, comparison, entries: List[PatchEntry],
             blocks: Optional[List[BlockEntry]] = None) -> Patch:
    """Convenience constructor: stamps the provenance metadata from the two
    sessions a diff was run against."""
    import os as _os
    return Patch(created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                source_family=getattr(primary, "family", ""),
                source_name=_os.path.basename(primary.binfile.name),
                comparison_family=getattr(comparison, "family", ""),
                comparison_name=_os.path.basename(comparison.binfile.name),
                entries=entries, blocks=blocks or [])


# ---------------------------------------------------------------------------
# reverting a previously applied patch, from the change log alone
# ---------------------------------------------------------------------------

def _log_path_for(session) -> str:
    return os.path.splitext(session.path)[0] + ".log"


def _iter_patch_records(lines):
    for line in lines:
        line = line.strip()
        if line.startswith(PATCH_LOG_MARKER):
            try:
                yield json.loads(line[len(PATCH_LOG_MARKER):])
            except ValueError:
                continue


def _log_patch_record(session, *, source: str, writes, n_entries: int = 0,
                      n_blocks: int = 0) -> dict:
    """Append one machine-readable record of ``writes`` (the same
    ``(addr, bits, old, new)`` tuples a ``Command`` carries) to ``session``'s
    change log -- the SAME log ``TunerSession.write_change_log`` already
    flushes beside the saved binary as a plain ``.log`` file. Reusing that
    file (rather than a separate patch-tracking file) is what lets a patch
    apply be found and reverted later from the binary alone.
    """
    rec = {"id": uuid.uuid4().hex[:10],
          "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "source": source, "n_entries": n_entries, "n_blocks": n_blocks,
          "writes": [list(w) for w in writes]}
    session.log(PATCH_LOG_MARKER + json.dumps(rec, separators=(",", ":")))
    return rec


def list_applied_patches(session) -> List[dict]:
    """Every patch-apply (and every revert of one -- a revert logs its own
    record, so it shows up here too and can itself be reverted) found for
    ``session``, most recent first.

    Reads both ``session``'s in-memory ``change_log`` (edits made this
    session but not yet saved -- these disappear if the session is closed
    without saving) and the ``.log`` file beside ``session.path`` on disk (
    edits from a previous, saved session). Each dict gets an extra
    ``"pending"`` key: True for one only found in memory so far.
    """
    from . import changelog as _changelog
    disk: List[dict] = list(_iter_patch_records(
        _changelog.read_lines(getattr(session, "path", None))))
    for r in disk:
        r["pending"] = False
    pending = list(_iter_patch_records(session.change_log))
    for r in pending:
        r["pending"] = True
    combined = disk + pending
    combined.reverse()
    return combined


@dataclass
class RevertResult:
    record_id: str
    n_writes: int
    warnings: List[str] = field(default_factory=list)


def _resized_records(target):
    """``[(start, end, name)]``: the records of the tables resized or moved
    in ``target`` (ovtsuite.resize), as they are now."""
    try:
        from . import resize as _resize
    except Exception:
        return []
    codec = _resize.codec_for(target)
    if codec is None:
        return []
    out = []
    data = bytes(target.binfile.data)
    try:
        ents = list(target.entities) + list(target.variant_entities())
    except Exception:
        ents = list(getattr(target, "entities", []) or [])
    for e in ents:
        if not _resize.is_resized(e):
            continue
        try:
            shp = codec.record(data, e)
        except Exception:
            shp = None
        if shp is not None:
            out.append((shp.start, shp.end, e.name))
    return out


def revert_applied_patch(target, record: dict) -> RevertResult:
    """Undo a previously applied patch using the before/after byte values
    recorded in the change log at apply time (``list_applied_patches``
    finds ``record``) -- no original ``.patch`` file needed.

    Writes the ORIGINAL ("old") value back at every address the patch
    touched. If any of those addresses have been written again since (by a
    later edit, or a later patch), reverting still restores the pre-patch
    value there -- flagged in ``RevertResult.warnings``, not blocked, since
    silently refusing would leave the user with no way to back out a bad
    patch at all.

    The revert itself is logged as a new record (so it can be found by
    ``list_applied_patches`` and, if it turns out to be unwanted, reverted
    right back -- reverting a revert re-applies the original patch).
    """
    writes = record.get("writes") or []
    if not writes:
        raise ValueError("this patch record has no writes to revert")
    warnings: List[str] = []
    reverse_writes = []
    drifted = 0
    resized = _resized_records(target)
    held: Dict[str, int] = {}
    for addr, bits, old, new in writes:
        cur = target.binfile.read(addr, bits, False)
        if cur != new:
            hit = next((name for lo, hi, name in resized if lo <= addr < hi), None)
            if hit is not None:
                # a table resized or moved since the patch: its record was
                # rewritten, and the pre-patch bytes would land in the new
                # layout -- another cell, an axis point, a point count
                held[hit] = held.get(hit, 0) + 1
                continue
            drifted += 1
        reverse_writes.append((addr, bits, new, old))
    for name, n in sorted(held.items()):
        warnings.append(
            "%s was resized or moved after this patch: %d of its byte range(s) "
            "were left as they are (writing the old bytes into the new layout "
            "would corrupt it). Set it by hand, or undo the resize first." % (name, n))
    if not reverse_writes:
        raise ValueError("nothing of this patch can be reverted: %s"
                         % "; ".join(warnings))
    if drifted:
        warnings.append(
            "%d of %d byte range(s) were changed again after this patch was "
            "applied; reverting still restores the pre-patch value there, "
            "which may discard that later change" % (drifted, len(writes)))
    source = record.get("source") or record.get("id") or "?"
    label = "revert patch %s" % source
    cmd = Command(label, reverse_writes)
    cmd.apply(target.binfile)
    target.push(cmd, summary=label)
    rec = _log_patch_record(
        target, source="revert of %s" % source, writes=reverse_writes,
        n_entries=record.get("n_entries", 0), n_blocks=record.get("n_blocks", 0))
    return RevertResult(record_id=rec["id"], n_writes=len(reverse_writes),
                        warnings=warnings)


# ---------------------------------------------------------------------------
# diffing against a family's own reference binary ("what does this change vs
# stock"), and applying one patch across a folder of targets
# ---------------------------------------------------------------------------

def diff_against_reference(primary, family: Optional[str] = None):
    """Load and return ``primary``'s own family reference binary as a
    :class:`~ovtsuite.session.TunerSession`, suitable as the ``comparison``
    argument to ``diff_entities``/``diff_gaps`` -- "what does this tune
    change vs. stock" without hunting down a specific stock file by hand.

    Direction matters for a meaningful diff: call
    ``diff_entities(reference, primary)`` (reference first) so changes read
    as stock -> tune, not the other way round.
    """
    from . import match as _match
    from . import session as _session_mod
    fam = family or getattr(primary, "family", None)
    if not fam or fam not in _match.REF_BINARIES:
        raise ValueError("no reference binary known for family %r" % fam)
    ref_path = os.path.join(_session_mod._ref_dir(), _match.REF_BINARIES[fam])
    return _session_mod.TunerSession(ref_path, family=fam)


def batch_apply(patch: Patch, paths: List[str], *,
                family: Optional[str] = None, write: bool = False,
                out_dir: Optional[str] = None,
                default_disposition: str = DISCARD, fix_checksum: bool = True,
                source_label: Optional[str] = None) -> Dict[str, ApplyResult]:
    """Apply ``patch`` to every binary in ``paths`` independently -- one bad
    or unmatched file never aborts the rest, the way the project's own SGO
    cataloguing tools already treat a big batch.

    A file's own family/sub-family is auto-detected (the normal
    ``TunerSession`` path) unless ``family`` pins one for all of them. A
    file that fails to even load gets a single ``skipped`` entry carrying
    the exception message, rather than raising and losing every result
    after it.

    ``write`` actually saves each result -- into ``out_dir`` (by basename,
    originals untouched) when given, otherwise back in place. Dry run
    (``write=False``, the default) returns every ``ApplyResult`` without
    writing anything, so the caller can review before committing to a big
    batch.
    """
    from .session import TunerSession
    out: Dict[str, ApplyResult] = {}
    for path in paths:
        try:
            target = TunerSession(path, family=family)
        except Exception as exc:
            out[path] = ApplyResult(skipped=[(path, "could not load: %s" % exc)])
            continue
        res = apply_patch(patch, target, default_disposition=default_disposition,
                          fix_checksum=fix_checksum, source_label=source_label,
                          label="batch apply patch")
        out[path] = res
        if write and res.applied:
            dest = (os.path.join(out_dir, os.path.basename(path))
                   if out_dir else path)
            target.save(dest)
    return out


# ---------------------------------------------------------------------------
# generating a patch from a single binary's own version history
# ---------------------------------------------------------------------------

def _session_from_bytes(data: bytes, like_session, progress=None):
    """Load ``data`` as a standalone :class:`~ovtsuite.session.TunerSession`,
    pinned to ``like_session``'s own family so it identifies entities the
    same way regardless of platform (EDC16/EDC15/ZF 6HP all set
    ``session.family`` themselves during ``__init__``, so this works
    uniformly across every platform this project targets).

    ``TunerSession`` only reads a path at construction time, so the temp
    file backing this reconstructed session is removed immediately
    afterward -- the returned session already holds everything it needs in
    memory.
    """
    import tempfile
    from .session import TunerSession
    # The same file at another point in its history has the same layout, so
    # it reuses the loaded session's definition rather than identifying and
    # matching it again.
    if len(data) == len(like_session.binfile.data):
        try:
            return like_session.sibling(data)
        except Exception:
            pass                      # fall back to a full load below
    fam = getattr(like_session, "family", None)
    fd, tmp = tempfile.mkstemp(suffix=os.path.basename(like_session.path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return TunerSession(tmp, family=fam, progress=progress)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def sessions_from_history(session, older_id: str, newer_id: Optional[str] = None):
    """Reconstruct two points in ``session``'s own automatic version history
    (see :mod:`ovtsuite.history`) as standalone sessions, suitable as the
    ``primary``/``comparison`` pair to ``diff_entities``/``diff_gaps``.

    ``newer_id=None`` (the default) means "current" -- ``session``'s live
    bytes exactly as they are now, including any unsaved edits -- so a
    patch can be built between any earlier checkpoint and what's on screen
    right now without saving first. Returns ``(older_session,
    newer_session)``; the caller is responsible for these being separate,
    throwaway ``TunerSession`` objects (nothing here mutates ``session``
    itself).
    """
    from . import history as _history
    older_bytes = _history.bytes_at_version(session, older_id)
    older = _session_from_bytes(older_bytes, session)
    if newer_id is None:
        # Reuse the live session as-is for "current" -- diffing against it
        # directly picks up unsaved edits, and avoids reconstructing bytes
        # we already have in memory.
        newer = session
    else:
        newer_bytes = _history.bytes_at_version(session, newer_id)
        newer = _session_from_bytes(newer_bytes, session)
    return older, newer


def session_at_version(session, version_id: str, progress=None):
    """One checkpoint of ``session``'s own version history as a standalone,
    read-only-by-convention :class:`~ovtsuite.session.TunerSession` -- what
    the GUI's "Compare with saved version" shows as the comparison.

    Named ``<file> @ <checkpoint name>`` so every place that shows the
    comparison's name says which version it is, and tagged with
    ``version_of = {"path", "id", "label", "ts"}`` so the GUI can remember
    and restore it (the temp file it was read from no longer exists).
    Raises ``ValueError`` for an id not in the history.
    """
    from . import history as _history
    ver = next((v for v in _history.list_versions(session)
                if v["id"] == version_id), None)
    if ver is None:
        raise ValueError("no version %r found in this binary's history" % version_id)
    data = _history.bytes_at_version(session, version_id)
    s = _session_from_bytes(data, session, progress=progress)
    label = _history.display_label(ver)
    short = label if len(label) <= 48 else label[:47].rstrip() + "\u2026"
    s.binfile.name = "%s @ %s" % (session.binfile.name, short)
    s.version_of = {"path": getattr(session, "path", None), "id": version_id,
                    "label": label, "ts": ver.get("ts", "")}
    return s


def diff_from_history(session, older_id: str, newer_id: Optional[str] = None, *,
                      include_gaps: bool = False) -> Patch:
    """Build a :class:`Patch` from two points in ``session``'s own version
    history, instead of a separately loaded comparison file -- the same
    identity-based diff (:func:`diff_entities`, and optionally
    :func:`diff_gaps`) run between two reconstructed states of the SAME
    binary rather than two different binaries.

    ``newer_id=None`` (the default) diffs the current live state (including
    any unsaved edits) against ``older_id``. Pass an explicit ``newer_id``
    to diff two past checkpoints against each other instead.
    """
    older, newer = sessions_from_history(session, older_id, newer_id)
    entries = diff_entities(older, newer)
    blocks: List[BlockEntry] = []
    if include_gaps:
        for start, end in diff_gaps(older, newer):
            blocks.append(build_block_entry(older, newer, start, end))
    pt = new_patch(older, newer, entries, blocks)
    base = os.path.basename(session.binfile.name)
    pt.source_name = "%s @ %s" % (base, older_id)
    pt.comparison_name = "%s @ %s" % (base, "current" if newer_id is None else newer_id)
    return pt
