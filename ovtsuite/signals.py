# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Signal-ID catalog for the measuring-block editor.

The measuring-block map (UACC_RSigBlkGrp_MAP) stores each display slot as an
ESB index -- an entry of UACC_ESBSigNr_C, whose value is the firmware's
internal signal number (see :mod:`ovtsuite.msblock`). The ECU turns that
number into a live value through a signal-descriptor table in firmware; the
number-to-meaning mapping is NOT in the calibration data. This module keeps a *pluggable* catalog of what those ids
mean, from whichever source we can get:

  * an original A2L / DAMOS MEASUREMENT section (names + addresses), or
  * a disassembly pass over the firmware's signal-descriptor table
    (id -> RAM source + scaling), or
  * a VCDS/VAG-COM label file (group/field -> description), or
  * hand curation.

Catalogs live in ``data/signals_<family>.json`` -- data of the EDC16 module,
found wherever it is installed (:mod:`ovtsuite.datafiles`) -- as
``{"<id>": {"name": "...", "unit": "...", "note": "..."}}``; a missing file
just means no names yet. Whatever the source, the editor gains labels the
moment such a file is dropped in -- no code change.

Even with no catalog, ``observed_ids`` offers every ESB index the binary
defines (with how often the group map uses it), so the dropdown always lists
the valid set rather than a blind number field.
"""

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import datafiles

# Ids that mean "no signal in this slot" (seen as sentinels in the tables).
EMPTY_IDS = {255}  # 255 (0xFF) = empty slot; id 0 is a real signal (engine speed)

# Tables whose cells are signal ids, used to gather the observed universe.
SIGNAL_TABLES = [
    "UACC_RSigBlkGrp_MAP",
    "Uacc_AdjValK2SigBlkVal_MAP",
    "Uacc_EOPDK2SigBlkVal_MAP",
    "TstTog_K2SigBlkVal1_MAP",
    "TstTog_K2SigBlkVal2_MAP",
    "BasAdj_K2SigBlkVal0_MAP",
    "BasAdj_K2SigBlkVal1_MAP",
    "BasAdj_K2SigBlkVal2_MAP",
    "UACC_ShtrpApplSigBlk_MAP",
]


@dataclass
class SignalInfo:
    id: int
    name: str = ""      # Bosch A2L symbol, e.g. "Eng_nAvrg"
    unit: str = ""
    note: str = ""
    human: str = ""     # plain-English name, e.g. "Engine Speed"

    @property
    def known(self) -> bool:
        return bool(self.name)

    def label(self) -> str:
        if not self.name:
            # No resolved symbol. The firmware signal-descriptor pass tags many
            # of these with their raw internal number; the 8000-series are the
            # CAN-received signals (module-present flags, bus torque requests,
            # gateway status, etc.), so surface that rather than a bare number.
            intnum = self._intnum()
            if intnum is not None and intnum >= 8000:
                return "%d - CAN bus signal (internal %d)" % (self.id, intnum)
            if intnum is not None:
                return "%d - unnamed (internal %d)" % (self.id, intnum)
            return "%d" % self.id
        u = " [%s]" % self.unit if self.unit else ""
        if self.human:
            # "12 - Engine Speed (Eng_nAvrg) [rpm]"
            return "%d - %s (%s)%s" % (self.id, self.human, self.name, u)
        return "%d - %s%s" % (self.id, self.name, u)

    def _intnum(self):
        """The raw internal signal number the disassembly pass recorded in the
        note ('intnum=8002 unresolved'), or None."""
        import re
        m = re.search(r"intnum=(\d+)", self.note or "")
        return int(m.group(1)) if m else None


class SignalCatalog:
    """Id -> meaning for one family, plus the ids a binary actually uses."""

    def __init__(self, family: Optional[str] = None, session=None):
        self.family = family
        self._map: Dict[int, SignalInfo] = {}
        self._intnums: Dict[int, int] = {}
        #: the family whose catalog supplied the names, when it is not the
        #: session's own (a sub-family sharing its parent's signal numbering)
        self.source_family: Optional[str] = None
        if session is not None and getattr(session, "is_edc15", False):
            # EDC15: the ids are xcwGrp descriptor indices, named from the
            # dataset itself (message number -> English label)
            from .edc15.msblock15 import MeasuringBlocks15
            for sid, v in MeasuringBlocks15(session).catalog_entries().items():
                self._map[sid] = SignalInfo(sid, v.get("name", ""),
                                            v.get("unit", ""), v.get("note", ""),
                                            v.get("human", ""))
            return
        if family:
            if not self._load(family, session) and session is not None:
                parent = _parent_catalog_family(family, session)
                if parent is not None and self._load(parent):
                    self.source_family = parent
        if session is not None:
            self._read_intnums(session)

    def _load(self, family: str, session=None) -> bool:
        path = datafiles.path("signals_%s.json" % family)
        if path is None:
            return False
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return False
        for k, v in raw.items():
            try:
                sid = int(k)
            except ValueError:
                continue
            self._map[sid] = SignalInfo(sid, v.get("name", ""),
                                        v.get("unit", ""), v.get("note", ""),
                                        v.get("human", ""))
        if session is not None and raw.get("_by_intnum"):
            self._rename_by_intnum(raw["_by_intnum"],
                                   raw.get("_esb_table", ESB_SIGNR_TABLE),
                                   session)
        return True

    def _rename_by_intnum(self, by_int: dict, table: str, session) -> None:
        """Name each cell id through THIS binary's own cell -> internal
        number table, when the catalog carries the firmware's internal
        number -> signal map (EDC16U1). The cell numbering is calibration
        data and varies between datasets of one family (one cell differs
        between the 03G906016FK and HK U85B6 tables, 33 on GJ 7000); the
        internal numbering is the program's, identical on every U1 5.41
        program checked. Without the table in the session the catalog's own
        cell names stand."""
        ent = next((e for e in getattr(session, "entities", ())
                    if e.name == table), None)
        if ent is None:
            return
        try:
            nums = [int(round(ent.cell(r, c)))
                    for r in range(ent.rows) for c in range(ent.cols)]
        except Exception:
            return
        remapped: Dict[int, SignalInfo] = {}
        for sid, intn in enumerate(nums):
            if intn == 0:
                continue                    # empty ESB entry: no signal
            v = by_int.get(str(intn))
            if v:
                remapped[sid] = SignalInfo(sid, v.get("name", ""),
                                           v.get("unit", ""), v.get("note", ""),
                                           v.get("human", ""))
            else:
                remapped[sid] = SignalInfo(sid, note="intnum=%d unresolved" % intn)
        if remapped:
            self._map = remapped

    def info(self, sid: int) -> SignalInfo:
        got = self._map.get(sid)
        if got is not None:
            return got
        intnum = self._intnums.get(sid)
        if intnum:
            return SignalInfo(sid, note="intnum=%d unresolved" % intnum)
        return SignalInfo(sid)

    def _read_intnums(self, session) -> None:
        """ESB index -> internal signal number from the session's own
        UACC_ESBSigNr_C, so an unnamed id still says which signal it is."""
        ent = next((e for e in getattr(session, "entities", ())
                    if e.name == ESB_SIGNR_TABLE), None)
        if ent is None:
            return
        try:
            nums = [int(round(ent.cell(r, c)))
                    for r in range(ent.rows) for c in range(ent.cols)]
        except Exception:
            return
        self._intnums = {i: n for i, n in enumerate(nums) if n}

    def label(self, sid: int) -> str:
        if sid in EMPTY_IDS:
            return "%d - (empty)" % sid
        return self.info(sid).label()

    @property
    def named_count(self) -> int:
        return sum(1 for s in self._map.values() if s.known)

    def choices(self, observed: List[int]) -> List[tuple]:
        """(id, label) pairs to offer in a dropdown: catalog ids first
        (named), then any observed ids not in the catalog, then the empty
        sentinels. Sorted by id within each group."""
        cat = sorted(self._map)
        obs_only = sorted(set(observed) - set(cat) - EMPTY_IDS)
        out = []
        for sid in cat:
            if sid in EMPTY_IDS:
                continue
            out.append((sid, self.label(sid)))
        for sid in obs_only:
            out.append((sid, self.label(sid)))
        for sid in sorted(EMPTY_IDS):
            out.append((sid, self.label(sid)))
        return out


# The calibration table that turns a measuring-block cell id into the ECU's
# internal signal number (cell id N -> UACC_ESBSigNr_C[N]). A catalog names
# cell ids, so it carries over to another family only where this table is
# byte-identical to the one the catalog was built against.
ESB_SIGNR_TABLE = "UACC_ESBSigNr_C"


def _parent_catalog_family(family: str, session) -> Optional[str]:
    """The parent family whose signal catalog also names ``session``'s ids.

    Only for an EDC16 dataset-id sub-family (``p350_u8a7`` -> ``p350``), and
    only when the session's own UACC_ESBSigNr_C bytes equal the parent's
    reference bytes (the parent KB's crc32) -- then every cell id maps to the
    same internal signal number the parent catalog was built from. Anything
    else (no parent, table missing or different) gives None: no names rather
    than possibly wrong ones."""
    try:
        from .edc16.session16 import parent_family
        from . import kb as kb_mod
    except ImportError:
        return None
    parent = parent_family(family)
    if parent is None or not datafiles.exists("signals_%s.json" % parent):
        return None
    ent = next((e for e in getattr(session, "entities", ())
                if e.name == ESB_SIGNR_TABLE), None)
    binfile = getattr(session, "binfile", None)
    if ent is None or binfile is None or ent.addr is None:
        return None
    try:
        pkb = kb_mod.load_cached(parent)
    except Exception:
        return None
    ref = next((t for t in pkb.tables if t.name == ESB_SIGNR_TABLE), None)
    if ref is not None:
        span = ref.rows * ref.cols * (ref.z_bits // 8)
    else:
        ref = next((c for c in pkb.constants if c.name == ESB_SIGNR_TABLE),
                   None)
        if ref is None:
            return None
        span = ref.bits // 8
    if not ref.vhash or span <= 0:
        return None
    import zlib
    data = bytes(binfile.data[ent.addr:ent.addr + span])
    if len(data) != span or (zlib.crc32(data) & 0xFFFFFFFF) != ref.vhash:
        return None
    return parent


def observed_ids(session) -> Dict[int, int]:
    """Every id a measuring-block cell can usefully hold -> how many group
    slots use it now (0 = defined but unused).

    EDC16: every ESB index with a signal behind it, plus whatever the group
    map holds. The related tables (test toggle, basic settings, short trip)
    are no longer mixed in: UACC_ShtrpApplSigBlk_MAP, for one, is not in the
    ESB namespace at all (it holds values like 32767), and they offered ids
    the group map cannot use."""
    if getattr(session, "is_edc15", False):
        from .edc15.msblock15 import observed_ids as _o15
        return _o15(session)
    from .msblock import MeasuringBlocks
    mb = MeasuringBlocks(session)
    counts: Dict[int, int] = {i: 0 for i in mb.defined_ids()}
    for g in mb.groups():
        for sid in g.channels():
            if sid in EMPTY_IDS:
                continue
            counts[sid] = counts.get(sid, 0) + 1
    return counts
