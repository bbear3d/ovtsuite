# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring-block (signal-block) model for EDC16 diagnostics.

A scan tool (VCDS / VAG-COM, or a factory tester) reads engine data in
numbered *measuring blocks*. In the ECU those blocks are defined by the
signal-block tables:

  UACC_RSigBlkGrp_MAP   255 groups x 4 slots -- THE measuring-block map.
                        Row N = measuring-block group N; the four columns
                        are the channels shown in that group's slots. Each
                        cell is a signal id (255 = empty slot; id 0 is
                        engine speed, a real signal).
  Uacc_AdjValK2SigBlkVal_MAP / Uacc_EOPDK2SigBlkVal_MAP
                        value-selection tables for adjustment / EVP output.
  TstTog_K2SigBlkVal1/2_MAP    signal blocks used by the test-toggle output.
  BasAdj_K2SigBlkVal0/1/2_MAP  tester display during basic-setting routines.
  UACC_ShtrpApplSigBlk_MAP     short-trip diagnostic block.

The primary editable object is the group map: changing a group's channel
ids changes what that measuring block displays on the tester. Every edit
goes through the session, so it is undoable and saved with the binary.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# The group map, and the related definition tables (in display order).
GROUP_TABLE = "UACC_RSigBlkGrp_MAP"
EMPTY_ID = 255                   # empty slot (id 0 = engine speed is real)
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


@dataclass
class MeasuringGroup:
    """One measuring-block group: its number and channel-slot ids."""
    index: int                       # 0-based row in the map
    entity: object = None            # the UACC_RSigBlkGrp_MAP Entity
    slots: int = 4

    @property
    def number(self) -> int:
        """1-based group number, as a scan tool shows it."""
        return self.index + 1

    def channels(self) -> List[int]:
        return [int(round(self.entity.cell(self.index, c)))
                for c in range(self.slots)]

    @property
    def populated(self) -> bool:
        # 255 (0xFF) is the empty-slot sentinel; id 0 is a real signal
        # (engine speed), so "any non-zero" would be wrong on both counts
        return any(c != EMPTY_ID for c in self.channels())


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

    @property
    def available(self) -> bool:
        return self.group_map is not None

    def groups(self) -> List[MeasuringGroup]:
        if self.group_map is None:
            return []
        return [MeasuringGroup(i, self.group_map, self.group_map.cols)
                for i in range(self.group_map.rows)]

    def populated_groups(self) -> List[MeasuringGroup]:
        return [g for g in self.groups() if g.populated]

    def set_channel(self, group: MeasuringGroup, slot: int, value: int):
        """Assign an internal signal id to one slot of a group (undoable)."""
        self.session.edit_cells(group.entity,
                                [(group.index, slot, float(int(value)))])

    def clear_group(self, group: MeasuringGroup):
        """Empty every slot of a group (one undoable step). The empty slot is
        255 -- writing 0 would show engine speed, a real signal."""
        cells = [(group.index, c, float(EMPTY_ID)) for c in range(group.slots)]
        self.session.edit_cells(group.entity, cells)

    def summary(self) -> dict:
        gs = self.groups()
        pop = sum(1 for g in gs if g.populated)
        return {"groups": len(gs), "populated": pop,
                "empty": len(gs) - pop,
                "related_tables": len(self.related)}
