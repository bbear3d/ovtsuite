# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Auto-tune: resolve targets by name, plan the writes, validate, then apply.

Nothing in here writes to a binary except ``Plan.apply``, and that refuses
until ``Plan.validate`` has run clean. The sequence is the design::

    from ovtsuite.autotune import Plan, Target, UPSHIFTS
    from ovtsuite.zf6hp.tuning import shift_anchors

    plan = Plan(session, "stage 1: raise upshift points")
    for m in Target(family="shift", count="all").resolve(session):
        plan.scale(m, UPSHIFTS.cells(m), 1.08, "later upshifts")

    plan.validate()
    if plan.ok:
        plan.apply()                      # one undoable transaction

Ported from hp6tuner. The spine here is platform-neutral -- a plan, a target
resolver and a check registry -- and the gearbox knowledge lives in
``ovtsuite/zf6hp/tuning/``: the anchors read out of the image, the checks that
know what a shift map is, and the profiles themselves.
"""

from .plan import ERROR, NOTE, WARNING, Plan, PlannedWrite, Problem
from .validate import COMMON_CHECKS, register, run_checks

#: Shift-map targets are ZF 6HP knowledge (ovtsuite.zf6hp.tuning.targets6hp);
#: they are re-exported here for existing callers, resolved on first use so
#: the shared plan/validate framework imports without the ZF 6HP module.
_TARGET_NAMES = ("ALL_CELLS", "AmbiguousTarget", "CellRange", "DOWNSHIFT_COLUMNS",
                 "DOWNSHIFTS", "Target", "TargetError", "TargetNotFound",
                 "UPSHIFT_COLUMNS", "UPSHIFTS", "paired_downshift", "resolve",
                 "shift_column")


def __getattr__(name):
    if name in _TARGET_NAMES:
        from ..zf6hp.tuning import targets6hp
        return getattr(targets6hp, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


__all__ = [
    "ALL_CELLS", "AmbiguousTarget", "CellRange", "COMMON_CHECKS",
    "DOWNSHIFTS", "DOWNSHIFT_COLUMNS", "ERROR", "NOTE", "Plan",
    "PlannedWrite", "Problem", "Target", "TargetError", "TargetNotFound",
    "UPSHIFTS", "UPSHIFT_COLUMNS", "WARNING", "paired_downshift", "register",
    "resolve", "run_checks", "shift_column",
]
