# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Optimised logging blocks: lay out measuring blocks so a logging preset
reads as few groups per row as possible (docs/LOGGING_BLOCKS_PLAN.md).

The controller answers one group per round trip -- about 32 ms on the P475
LQ over TP 2.0, about 340 ms on an EDC15 over KW1281 -- so the number of
groups a preset reads IS its sample rate. This package plans new groups in
unused group numbers, or fills empty cells of stock groups, so each preset
(shipped or your own) reads fewer of them, and only ever when that lowers the
count.

Nothing here knows an ECU. A platform module supplies a
:class:`~.provider.LayoutProvider` (group range, slots, what is free, cell
sources with their fingerprints, writing); everything else -- requirements,
planner, the layout manifest, the probe and the namer overlays -- is shared.

Modules:

* :mod:`.model`        -- the data types;
* :mod:`.requirements` -- what a preset needs, from a recipe or your preset;
* :mod:`.planner`      -- :func:`optimise` one preset, :func:`allocate` many;
* :mod:`.provider`     -- the platform protocol, lookup, and the planner's
  layout snapshot;
* :mod:`.manifest`     -- the record of what was written, and its stores;
* :mod:`.probe`        -- does the ECU serve the groups as built?
* :mod:`.namer`        -- the manifest laid over a namer, and the stock view.
"""
from .model import (EMPTY, FILL, NEW, NO_SLOT, Allocation, CellSource, CellWrite,
                    Layout, NoGain, PlanItem, PresetPlan, Requirement)
from .planner import allocate, optimise, source_picker

__all__ = ["EMPTY", "NO_SLOT", "NEW", "FILL", "Requirement", "CellSource",
           "CellWrite", "Layout", "PlanItem", "PresetPlan", "NoGain",
           "Allocation", "optimise", "allocate", "source_picker"]
