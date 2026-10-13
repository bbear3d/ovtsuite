# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tune an EDC16 PD calibration from data logs.

    from ovtsuite.logtune import Options, analyse, runs_from_paths
    from ovtsuite.datalog.channels import ChannelNamer

    runs = runs_from_paths(["pulls-3rd.csv", "cruise.csv"], ChannelNamer(session))
    result = analyse(session, runs, Options(lambda_target=1.20))
    print(to_text(result))
    if result.plan.ok:
        result.plan.apply()          # one undoable step

What gets logged, and how to drive while logging, is described by
:data:`ovtsuite.logtune.roles.RECIPES`; the Data logging dialog turns each
recipe into the measuring blocks this particular calibration carries.
"""
from .analysis import (CRITICAL, GOOD, INFO, WARN, CellNote, Finding, LogTuner,
                       Options, Result, analyse)
from .report import to_html, to_text
from .roles import RECIPES, ROLES, Recipe, plan_groups, recipe_groups, resolve_log
from .samples import Run, run_from_log, runs_from_paths

__all__ = [
    "CRITICAL", "GOOD", "INFO", "WARN", "CellNote", "Finding", "LogTuner", "Options",
    "RECIPES", "ROLES", "Recipe", "Result", "Run", "analyse", "plan_groups",
    "recipe_groups", "resolve_log", "run_from_log", "runs_from_paths", "to_html", "to_text",
]
