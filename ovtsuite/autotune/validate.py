# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The check registry, and the checks that are not about any one platform.

Every check reads the plan's *simulated* result -- ``Plan.value_at`` -- so
nothing has been written when these run. Three severities:

``error``    blocks the apply. Reserved for things that produce a calibration
             wrong in a way the driver will feel.
``warning``  surfaced and stepped over. A reference tune does some of these
             deliberately.
``note``     informational.

A guiding rule throughout: **a check only holds the plan responsible for cells
the plan touched.** Stock calibrations contain oddities, and a validator that
blocks on pre-existing quirks blocks everything and gets switched off. What
matters is whether this plan made something worse.

The platform-specific checks live with their platform (see
``ovtsuite/zf6hp/tuning/checks6hp.py``); this module holds the two that are
about the model rather than the gearbox, and the registry that puts a
platform's set together.
"""

from typing import Callable, Dict, List, Sequence

from .plan import ERROR, NOTE, Problem


def check_clamped(plan) -> List[Problem]:
    """A value the entity cannot hold silently became a different value.

    An error rather than a shrug: the profile computed a number, the map
    stored something else, and every downstream check then validates a value
    the recipe never asked for. Note that ``was_clamped`` means the RANGE, not
    the quantisation -- rounding 1649.55 to 1650 in a 16-bit cell happens on
    nearly every scale operation, and reporting it thousands of times would
    bury the one write that ran off the end.
    """
    out = []
    for w in plan.writes:
        if w.was_clamped:
            low, high = w.target.value_range
            kind = ("float" if getattr(w.target, "is_float", False)
                    else ("signed" if getattr(w.target, "signed", False)
                          else "unsigned"))
            out.append(Problem(
                ERROR, "clamped",
                "%g does not fit %d-bit %s [%g, %g] and would become %g"
                % (w.requested, getattr(w.target, "bits", 0), kind,
                   low, high, w.new),
                "%s[%d,%d]" % (getattr(w.target, "name", "?"), w.row, w.col)))
    return out


def check_not_empty(plan) -> List[Problem]:
    """A plan that changes nothing is usually a resolver that missed."""
    if plan.writes and not plan.effective_writes:
        return [Problem(NOTE, "no-op",
                        "all %d planned write(s) already hold the intended "
                        "value" % len(plan.writes))]
    return []


#: Checks every platform runs.
COMMON_CHECKS = (check_clamped, check_not_empty)

#: platform -> the checks to run. Registered by the platform's own module, so
#: importing the core does not drag in every gearbox.
REGISTRY: Dict[str, Sequence[Callable]] = {}


def register(platform: str, checks: Sequence[Callable]) -> None:
    REGISTRY[platform] = tuple(checks)


def checks_for(plan) -> Sequence[Callable]:
    platform = getattr(getattr(plan, "session", None), "platform", "")
    return REGISTRY.get(platform, COMMON_CHECKS)


def run_checks(plan, checks=None) -> List[Problem]:
    """Run every check, collecting problems. A check must not mutate."""
    out: List[Problem] = []
    for check in (checks_for(plan) if checks is None else checks):
        out.extend(check(plan))
    return out
