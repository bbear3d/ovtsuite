# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which measuring blocks the ECU will actually serve.

**The rule, from the firmware.** The EDC16 0x21 handler (03G906016LQ
@0x09B6EC, the same routine in every P350/P475/U1/P447 image checked) builds
group ``g`` from TWO map rows: the one whose Y-axis value is ``g`` (cells 1-4)
and the one whose value is ``g + 127`` (cells 5-8). Rows 128..254 are
therefore the second halves of groups 1..127, never groups of their own -- see
:mod:`ovtsuite.msblock`.

That explains everything the car showed. Every block of 128 or more ever
logged came back N/A (133, 194 and 213 on the P475 LQ, 3377 samples of 213
alone), while group 6 answered eight cells, the last four being exactly row
133's engine speed / air mass / boost / gear. Earlier this module treated
everything from 100 up as unserved, and before that a heuristic "trailing
outlier" gap in the map; both were fitting a symptom of the rows above 127.
Groups 100..127 are ordinary groups and the presets may use them.

The namers (:class:`~ovtsuite.datalog.channels.ChannelNamer`,
:class:`~ovtsuite.datalog.msbcatalogue.CatalogueNamer`) already fold the upper
rows into their base group, so on EDC16 :func:`unserved_for` normally has
nothing to say; it stays as the guard for a namer that has not been folded
(an old catalogue, a hand-built namer).
"""
from typing import List

#: The first local identifier the ECU does not answer as a group of its own.
FIRST_UNSERVED = 128


def unserved_for(namer) -> List[int]:
    """Blocks the presets must not use: everything from ``FIRST_UNSERVED`` up.
    ``[]`` when the namer cannot say.

    Deliberately engine-side only. The transmission's own catalogue really does
    use 125, 225, 226 and 227, so the same rule applied there would throw away
    the blocks that matter most.
    """
    if namer is None or not getattr(namer, "available", False):
        return []
    if type(namer).__name__ not in ("ChannelNamer", "CatalogueNamer"):
        return []
    try:
        return sorted({int(g) for g in namer.populated_groups()
                       if int(g) >= FIRST_UNSERVED})
    except Exception:
        return []
