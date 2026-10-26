# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring-block (signal-block) model for EDC16 diagnostics.

A scan tool reads engine data in numbered *measuring blocks* (KWP2000
``readDataByLocalIdentifier``, 0x21). In the ECU those blocks are defined by
the signal-block tables:

  UACC_RSigBlkGrp_MAP   the measuring-block map: 255 rows x 4 slots. Each
                        cell is an **ESB index** -- an entry of the parallel
                        UACC_ESBSigNr_C / UACC_ESBFormular_C /
                        UACC_ESBNormVal_C tables (internal signal number,
                        display formula, normalisation byte). 255 is the empty
                        slot; ESB index 0 is engine speed, a real signal.
  Uacc_AdjValK2SigBlkVal_MAP / Uacc_EOPDK2SigBlkVal_MAP
                        value-selection tables for adjustment / EVP output.
  TstTog_K2SigBlkVal1/2_MAP    signal blocks used by the test-toggle output.
  BasAdj_K2SigBlkVal0/1/2_MAP  tester display during basic-setting routines.
  UACC_ShtrpApplSigBlk_MAP     short-trip diagnostic block.

**How a group read is built (firmware, verified).** The 0x21 handler
(03G906016LQ @0x09B6EC; the same routine is in every P350, P475, U1 and
P447/U34 image checked) fills an EIGHT-slot record for group ``g``:

  * slots 1-4 from the map row whose **Y-axis value** equals ``g``;
  * slots 5-8 from the row whose Y-axis value equals ``g + 127``;
  * an unused slot is sent as the pad cell ``25 00 00``.

The group number is the Y-axis value the firmware reads out of the map's own
header (``Nx, Ny, X[Nx], Y[Ny]`` directly in front of Z), not the row index.
On every image so far the axis is 1..255, so row N is group N -- but the
header is what the ECU uses, so it is what this module uses too.

Consequences, all seen on the car (P475 LQ):

  * group 6 answers eight cells -- its own four plus row 133's (engine speed,
    air mass, boost, gear);
  * a direct request for 133 or 213 answered N/A: rows 128..254 are the
    second halves of groups 1..127, not groups of their own, which is why the
    "groups of 100 and up" never identified reliably -- 100..127 are ordinary
    groups, the rows above them are not;
  * a group whose own row is empty is still served when its +127 row is
    populated (cells 1-4 pad, cells 5-8 real).

So this model presents groups 1..127 with up to eight slots. Every edit goes
through the session, so it is undoable and saved with the binary.
"""

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# The group map, and the related definition tables (in display order).
GROUP_TABLE = "UACC_RSigBlkGrp_MAP"
ESB_SIGNR = "UACC_ESBSigNr_C"
ESB_FORMULA = "UACC_ESBFormular_C"
ESB_NORM = "UACC_ESBNormVal_C"
EMPTY_ID = 255                   # empty slot (id 0 = engine speed is real)
#: The handler adds this to a group number to find its second-half row.
EXTENSION_OFFSET = 127
#: Highest group a tester can ask for: rows above it are second halves.
LAST_GROUP = 127
SLOTS_PER_ROW = 4
RELATED_TABLES = [
    "Uacc_AdjValK2SigBlkVal_MAP",
    "Uacc_EOPDK2SigBlkVal_MAP",
    "TstTog_K2SigBlkVal1_MAP",
    "TstTog_K2SigBlkVal2_MAP",
    "BasAdj_K2SigBlkVal0_MAP",
    "BasAdj_K2SigBlkVal1_MAP",
    "BasAdj_K2SigBlkVal2_MAP",
    "UACC_ShtrpApplSigBlk_MAP",
]


def _cat_name(cat, idx: int) -> str:
    """The A2L symbol a signal catalog gives cell id ``idx`` ("" unnamed)."""
    if cat is None:
        return ""
    try:
        return cat.info(idx).name or ""
    except Exception:
        return ""


def has_measuring_blocks(session) -> bool:
    """Cheap check (no table walk) for menus: does this session carry
    measuring-block definitions OVT-Suite can show?"""
    if session is None:
        return False
    ents = getattr(session, "entities", ()) or ()
    probe = getattr(session, "_mb_probe", None)
    # kept against the entity list: a definition applied later is seen
    if probe is None or probe[0] is not ents:
        if getattr(session, "is_edc15", False):
            names = any(getattr(e, "name", "").startswith("xcwK") for e in ents)
        else:
            names = any(getattr(e, "name", "") == GROUP_TABLE for e in ents)
        probe = (ents, names)
        try:
            session._mb_probe = probe
        except Exception:
            pass
    return bool(probe[1])


def map_group_axis(entity) -> List[int]:
    """The group number of each map row, as the firmware reads it: the
    Y-axis values in the map's own header directly in front of Z.

    The header is validated (Nx == cols, Ny == rows) before it is trusted;
    otherwise the rows are numbered 1..N, which is what every image checked
    carries. The definition's own Y-axis pointer is NOT used: on at least one
    P447 image (03G906021TR) it points 0xE4 bytes away from the real header.
    """
    rows, cols = entity.rows, entity.cols
    fallback = list(range(1, rows + 1))
    binfile = getattr(entity, "binfile", None)
    if binfile is None or entity.addr is None or entity.bits != 16:
        return fallback
    head = entity.addr - (4 + 2 * cols + 2 * rows)
    data = binfile.data
    if head < 0:
        return fallback
    try:
        nx, ny = struct.unpack_from(">HH", data, head)
        if (nx, ny) != (cols, rows):
            return fallback
        ys = list(struct.unpack_from(">%dH" % rows, data, head + 4 + 2 * cols))
    except struct.error:
        return fallback
    return [y & 0xFF for y in ys]


@dataclass
class MeasuringGroup:
    """One measuring block as the tester sees it: up to eight slots, the
    first four from the group's own map row, the next four from its +127
    row. ``cells[i]`` is the (row, col) of slot i, or None when the map has
    no row for that half."""
    number: int                      # 1-based group number (KWP local id)
    entity: object = None            # the UACC_RSigBlkGrp_MAP Entity
    base_row: Optional[int] = None
    ext_row: Optional[int] = None
    cells: List[Optional[Tuple[int, int]]] = field(default_factory=list)

    @property
    def index(self) -> int:
        """0-based position (kept for callers written for the 255-row view)."""
        return self.number - 1

    @property
    def slots(self) -> int:
        return len(self.cells)

    def channels(self) -> List[int]:
        out = []
        for rc in self.cells:
            out.append(EMPTY_ID if rc is None
                       else int(round(self.entity.cell(rc[0], rc[1]))))
        return out

    def editable(self, slot: int) -> bool:
        return 0 <= slot < len(self.cells) and self.cells[slot] is not None

    @property
    def populated(self) -> bool:
        # 255 (0xFF) is the empty-slot sentinel; id 0 is a real signal
        # (engine speed), so "any non-zero" would be wrong on both counts
        return any(c != EMPTY_ID for c in self.channels())

    @property
    def last_used_slot(self) -> int:
        """Index of the last populated slot (-1 when none) -- the ECU drops
        trailing pad cells, so this is how many cells it really sends."""
        ch = self.channels()
        for i in range(len(ch) - 1, -1, -1):
            if ch[i] != EMPTY_ID:
                return i
        return -1


@dataclass
class EsbEntry:
    """One ESB definition: what a group-map cell value selects."""
    index: int
    signal: int          # internal signal number (0 = undefined entry)
    formula: int         # KWP display formula id
    norm: int            # normalisation byte sent as the cell's A byte


class MeasuringBlocks:
    """All signal-block tables in a session, with the group map primary.

    On an EDC15 session this constructor hands back an
    ``edc15.msblock15.MeasuringBlocks15`` (xcwK/xcwGrp blocks)."""

    def __new__(cls, session):
        if getattr(session, "is_edc15", False):
            from .edc15.msblock15 import MeasuringBlocks15
            return MeasuringBlocks15(session)
        return super().__new__(cls)

    def __init__(self, session):
        self.session = session
        self._by_name = {e.name: e for e in session.entities}
        self.group_map = self._by_name.get(GROUP_TABLE)
        self.related = [self._by_name[n] for n in RELATED_TABLES
                        if n in self._by_name]
        self._groups: List[MeasuringGroup] = []
        self._esb: Optional[List[EsbEntry]] = None
        if self.group_map is not None:
            self._build()

    def _build(self):
        gm = self.group_map
        axis = map_group_axis(gm)
        row_of: Dict[int, int] = {}
        for r, y in enumerate(axis):
            row_of.setdefault(y, r)          # first row wins, as the handler's loop
        cols = min(gm.cols, SLOTS_PER_ROW)
        for g in range(1, LAST_GROUP + 1):
            base = row_of.get(g)
            ext = row_of.get(g + EXTENSION_OFFSET)
            cells: List[Optional[Tuple[int, int]]] = []
            for row in (base, ext):
                for c in range(cols):
                    cells.append(None if row is None else (row, c))
            self._groups.append(MeasuringGroup(g, gm, base, ext, cells))

    # ---- shape ----
    @property
    def available(self) -> bool:
        return self.group_map is not None

    @property
    def max_slots(self) -> int:
        return max((g.slots for g in self._groups), default=SLOTS_PER_ROW)

    def groups(self) -> List[MeasuringGroup]:
        return list(self._groups)

    def group(self, number) -> Optional[MeasuringGroup]:
        try:
            n = int(number)
        except (TypeError, ValueError):
            return None
        return self._groups[n - 1] if 1 <= n <= len(self._groups) else None

    def populated_groups(self) -> List[MeasuringGroup]:
        return [g for g in self._groups if g.populated]

    # ---- ESB definitions ----
    def esb(self) -> List[EsbEntry]:
        """Every ESB definition (index -> signal number, formula, norm)."""
        if self._esb is None:
            def vals(name):
                e = self._by_name.get(name)
                if e is None:
                    return []
                try:
                    return [int(round(e.cell(r, c)))
                            for r in range(e.rows) for c in range(e.cols)]
                except Exception:
                    return []
            sig, frm, nrm = vals(ESB_SIGNR), vals(ESB_FORMULA), vals(ESB_NORM)
            n = len(sig)
            self._esb = [EsbEntry(i, sig[i],
                                  frm[i] if i < len(frm) else 0,
                                  nrm[i] if i < len(nrm) else 0)
                         for i in range(n)]
        return self._esb

    def use_tables(self, entities) -> None:
        """Take tables the definition leaves out from ``entities`` (name ->
        Entity), e.g. ESB tables a platform module found by structure. A
        table the definition places is never replaced."""
        added = False
        for name, ent in dict(entities or {}).items():
            if name not in self._by_name and ent is not None:
                self._by_name[name] = ent
                added = True
        if added:
            self.related = [self._by_name[n] for n in RELATED_TABLES if n in self._by_name]
            self._esb = None

    def forget_cache(self):
        """Drop the cached ESB tables (after an edit made elsewhere)."""
        self._esb = None

    def esb_entry(self, idx: int) -> Optional[EsbEntry]:
        e = self.esb()
        return e[idx] if 0 <= idx < len(e) else None

    @property
    def max_index(self) -> Optional[int]:
        """Highest ESB index a cell may hold, or None when the ESB tables are
        not in the definition (then nothing is range-checked)."""
        n = len(self.esb())
        return n - 1 if n else None

    def valid_id(self, value: int) -> bool:
        """Can ``value`` go in a group-map cell? The empty sentinel always
        can; otherwise it must index a defined ESB entry (beyond the table
        the ECU would read past its end)."""
        if value == EMPTY_ID:
            return True
        if value < 0:
            return False
        mx = self.max_index
        if mx is None:
            return value < 0xFFFF
        if value > mx:
            return False
        ent = self.esb_entry(value)
        return ent is not None and ent.signal != 0

    def defined_ids(self) -> List[int]:
        """Every ESB index with a signal behind it (the dropdown universe)."""
        return [e.index for e in self.esb()
                if e.signal != 0 and e.index != EMPTY_ID]

    def shares_esb(self, other: "MeasuringBlocks") -> bool:
        """Do both binaries carry the same ESB tables (every entry's signal,
        formula and norm)? Then a cell id means the same in both, named or
        not, and two cells compare by their raw ids."""
        a, b = self.esb(), other.esb()
        return bool(a) and len(a) == len(b) and all(
            (x.signal, x.formula, x.norm) == (y.signal, y.formula, y.norm)
            for x, y in zip(a, b))

    def same_entry(self, other: "MeasuringBlocks", idx: int) -> bool:
        """Is ESB entry ``idx`` the same (signal, formula, norm) in both --
        including an entry with no signal number, which no name can match."""
        if idx == EMPTY_ID:
            return True
        if not self.esb() or not other.esb():
            return True                # no ESB tables: nothing tells them apart
        mine, theirs = self.esb_entry(idx), other.esb_entry(idx)
        return mine is not None and theirs is not None and (
            mine.signal, mine.formula, mine.norm) == (
            theirs.signal, theirs.formula, theirs.norm)

    def _ids_named(self, cat) -> Dict[str, List[int]]:
        """A2L symbol -> the ESB indices ``cat`` names with it (cached per
        catalog and ESB read)."""
        key = (id(cat), id(self.esb()))
        if getattr(self, "_named_key", None) != key:
            out: Dict[str, List[int]] = {}
            for e in self.esb():
                n = _cat_name(cat, e.index) if e.index != EMPTY_ID else ""
                if n:
                    out.setdefault(n, []).append(e.index)
            self._named, self._named_key = out, key
        return self._named

    def translate_from(self, other: "MeasuringBlocks", idx: int,
                       mine_cat=None, theirs_cat=None) -> Optional[int]:
        """The index in THIS binary that shows what ``idx`` shows in
        ``other``. ``idx`` itself when both ESB tables agree on it (an entry
        with no signal number included). With both binaries' signal catalogs
        (``signals.SignalCatalog``), a named id is matched by its A2L symbol:
        the internal signal numbers are the program's own, so two families
        can number the same signal differently (P475 223 is P350 222).
        Otherwise the same internal signal number, preferring the same
        formula and norm. None when this binary has no definition for it."""
        if idx == EMPTY_ID:
            return EMPTY_ID
        if not self.esb() or not other.esb():
            return idx                 # a definition without ESB tables: no meaning to match
        theirs = other.esb_entry(idx)
        mine = self.esb_entry(idx)
        name = _cat_name(theirs_cat, idx)
        if name and mine_cat is not None and getattr(mine_cat, "named_count", 0):
            same = self._ids_named(mine_cat).get(name, [])
            exact = [] if theirs is None else [
                i for i in same
                if (self.esb_entry(i).formula, self.esb_entry(i).norm)
                == (theirs.formula, theirs.norm)]
            for pool in (exact, same):
                if idx in pool:
                    return idx
                if pool:
                    return pool[0]
            return None
        if theirs is None:
            return None
        if mine is not None and (mine.signal, mine.formula, mine.norm) == (
                theirs.signal, theirs.formula, theirs.norm):
            return idx
        if not theirs.signal:
            return None
        same = [e for e in self.esb() if e.signal == theirs.signal and e.signal]
        exact = [e for e in same if (e.formula, e.norm) == (theirs.formula, theirs.norm)]
        pick = exact or same
        return pick[0].index if pick else None

    # ---- edits ----
    def set_channel(self, group: MeasuringGroup, slot: int, value: int):
        """Assign an ESB index to one slot of a group (undoable)."""
        rc = group.cells[slot] if 0 <= slot < len(group.cells) else None
        if rc is None:
            raise ValueError("block %d has no map row for slot %d"
                             % (group.number, slot + 1))
        self.session.edit_cells(group.entity, [(rc[0], rc[1], float(int(value)))])

    def set_channels(self, edits, label: str = "edit measuring blocks"):
        """[(group, slot, value)] as ONE undoable step; slots already holding
        the value are skipped. Returns the number of cells written."""
        cells = []
        for group, slot, value in edits:
            rc = group.cells[slot] if 0 <= slot < len(group.cells) else None
            if rc is None:
                continue
            cur = int(round(group.entity.cell(rc[0], rc[1])))
            if cur != int(value):
                cells.append((rc[0], rc[1], float(int(value))))
        if cells:
            self.session.edit_cells(self.group_map, cells)
        return len(cells)

    def clear_group(self, group: MeasuringGroup):
        """Empty every slot of a group (one undoable step). The empty slot is
        255 -- writing 0 would show engine speed, a real signal."""
        self.set_channels([(group, s, EMPTY_ID) for s in range(group.slots)])

    def summary(self) -> dict:
        gs = self.groups()
        pop = sum(1 for g in gs if g.populated)
        return {"groups": len(gs), "populated": pop,
                "empty": len(gs) - pop,
                "related_tables": len(self.related)}
