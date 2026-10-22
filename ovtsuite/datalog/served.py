# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which measuring blocks the ECU will actually serve.

The calibration's group map (``UACC_RSigBlkGrp_MAP``, 255 rows x 4) defines a
row per block number, and a row being populated does NOT mean the ECU answers
``readDataByLocalIdentifier`` for it. Measured on the car: group 213 is
populated with four perfectly ordinary signals -- ``Eng_nAvrg``,
``APPCD_rAPP1UnFlt``, ``VSSCD_v``, ``Gearbx_stGear`` -- and returned N/A on all
3377 samples of a real log, while groups 6, 9 and 21 from the same request
served normally.

Nothing in the map distinguishes them. There is no companion max-group
constant (the A2L has exactly one group table and no count beside it), and the
dispatcher's own bound check has not been located in the disassembly. The
authoritative answer is to ask the ECU -- ``DiagSession.probe_groups`` does
that -- and this module is what to use when nobody has.

What the file *does* say is where the table stops being a table. On the P475
LQ, stock and tuned alike, the 92 populated rows run 1..140 with no internal
gap wider than 7, then a **54-row hole**, then seven stragglers: 194, 213,
225, 226, 227, 228, 252. A hole 7.7x the largest gap in the dense region is a
structural break, not a coincidence, and three of the stragglers (225, 226,
227) are precisely the block numbers the *transmission* controller documents
for itself via ``K_MWBEMV`` -- so those rows look like another controller's
definitions carried along in the image rather than this ECU's own blocks.
Group 213, the one measured dead, sits in that trailing set.

The presets used to treat that set as a last resort. Since 2026-10-10 they
leave out every block from 100 up as well (see ``FIRST_UNSERVED``), and they
never fall back onto either: a role that lives only up there is reported as
not available instead.
"""
from typing import List, Sequence

#: A gap only counts as the end of the table if it is this many times the
#: largest gap inside the dense region. 7.7x on the LQ; 3x leaves room for a
#: calibration whose main table is less tidy without inventing a boundary
#: where there is none.
GAP_RATIO = 3.0

#: ...and the stragglers must be a small minority. 7 of 92 (7.6%) on the LQ.
MAX_TRAILING_FRACTION = 0.25


def trailing_outliers(groups: Sequence[int]) -> List[int]:
    """The populated block numbers that sit beyond the group map's real end.

    Returns ``[]`` unless the sequence has one clear break: the widest gap must
    be ``GAP_RATIO`` times the widest gap below it, and what follows must be at
    most ``MAX_TRAILING_FRACTION`` of all populated groups. Both guards exist so
    an evenly-spread map is left entirely alone -- a wrong answer here would
    push a preset off a block the ECU serves perfectly well.
    """
    g = sorted(set(int(x) for x in groups))
    if len(g) < 8:
        return []
    gaps = [(b - a, b) for a, b in zip(g, g[1:])]
    widest, start = max(gaps)
    below = [d for d, b in gaps if b < start]
    if not below or widest < GAP_RATIO * max(below):
        return []
    trailing = [x for x in g if x >= start]
    if len(trailing) > MAX_TRAILING_FRACTION * len(g):
        return []
    return trailing


#: Engine-side block numbers from here up are not served. Measured on the P475
#: LQ over CAN and K-line: every block below 100 that has been logged answered
#: (1, 3, 6, 8, 9, 10, 11, 15, 20, 21, 67) and every block of 100 or more came
#: back N/A on every sample (133 on 2026-10-10, 194 on 2026-10-05, 213 on
#: 2026-10-04/05). Bear's rule, 2026-10-10: no preset uses a group over 100.
FIRST_UNSERVED = 100


def unserved_for(namer) -> List[int]:
    """Blocks the presets must not use: everything from ``FIRST_UNSERVED`` up,
    plus ``trailing_outliers``. ``[]`` when the namer cannot say.

    Deliberately engine-side only. The transmission's own catalogue really does
    use 125, 225, 226 and 227, so the same rule applied there would throw away
    the blocks that matter most.
    """
    if namer is None or not getattr(namer, "available", False):
        return []
    if type(namer).__name__ not in ("ChannelNamer", "CatalogueNamer"):
        return []
    try:
        groups = namer.populated_groups()
        return sorted(set(trailing_outliers(groups)) | {int(g) for g in groups if int(g) >= FIRST_UNSERVED})
    except Exception:
        return []
