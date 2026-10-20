# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Editing many gearbox schedules at once -- the Qt-free core behind the
shift-map and lock-up editors (design: project doc
``claude/shift_map_editor_design.md``).

A shift point the driver feels is a blend of several stored schedules, so an
edit that is meant to move it has to reach every schedule that may feed it,
and has to keep each schedule's own character while doing so. This package
does that once, for any gearbox a module describes:

* :mod:`.model`  -- a :class:`ScheduleSet` (schedules x lines x pedal rows),
  built by a module's provider (``ovtsuite.zf6hp.shiftedit6hp``);
* :mod:`.ops`    -- the operations (offset, scale, slide along pedal, band,
  set curve, copy, smooth, follow a reference, set the gap to the paired
  line, set "off"), each with a pedal range, a taper and a direction;
* :mod:`.checks` -- advisory checks, each with an optional auto-fix that can
  only take back part of the edit (never block, never invent);
* :mod:`.edit`   -- an :class:`Edit`: base values + operations + fixes ->
  before/after, findings, and one undoable ``autotune.Plan``;
* :mod:`.blend`  -- the effective (blended) line for a set of weighted corners;
* :mod:`.logfit` -- logged events against the schedules, and weights fitted
  from them.

Nothing is locked: the user may edit any line of any schedule. Checks warn.
"""
from .model import Line, Schedule, ScheduleSet, Scope, parse_range
from .ops import (BandRemap, CopyFrom, Offset, Scale, SetCurve, SlidePedal,
                  SetGap, SetOff, Smooth, TrackReference, ratio_of)
from .checks import (GapCheck, IdenticalCheck, MonotonicCheck, ReferenceGapCheck,
                     RpmLimitCheck, SharedCheck, Finding)
from .edit import Edit
from .blend import Blend, blend_pedal, effective_line
from .logfit import Event, Fit, evaluate, fit_weights, lookup_from

__all__ = [
    "Line", "Schedule", "ScheduleSet", "Scope", "parse_range",
    "BandRemap", "CopyFrom", "Offset", "Scale", "SetCurve", "SlidePedal", "Smooth",
    "SetGap", "SetOff", "TrackReference", "ratio_of",
    "GapCheck", "IdenticalCheck", "MonotonicCheck", "ReferenceGapCheck", "RpmLimitCheck", "SharedCheck",
    "Finding",
    "Edit", "Blend", "blend_pedal", "effective_line",
    "Event", "Fit", "evaluate", "fit_weights", "lookup_from",
]
