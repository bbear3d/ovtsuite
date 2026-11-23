# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The formats a data log can be written in (the logger's **Export As...**).

One list, one entry point: :func:`export` writes a :class:`~.logfile.DataLog` in the
format ``key`` names, with the :class:`Options` that format has. The window that asks
for the choice is :mod:`.gui_export`; this module has no Qt, so the formats are testable
and usable from a script.
"""
import os
from dataclasses import dataclass
from typing import List, Optional

from . import logfile as _lf


@dataclass(frozen=True)
class Format:
    key: str
    title: str
    blurb: str
    suffix: str                 # added to the log's name for the suggested file name


FORMATS = (
    Format("ovt", "OVT-Suite log (CSV)",
           "The log's own layout: a marker line, the labels and one row per sample with the "
           "values as the logger shows them. KWHack writes and reads the same layout.",
           "_ovt"),
    Format("vcds_advmb", "VCDS CSV - advanced measuring blocks (ADVMB)",
           "The layout recent VCDS writes for advanced measuring blocks: a marker row of "
           "group and field numbers, a time column per field, the units on their own row. "
           "Any number of cells.",
           "_vcds"),
    Format("vcds_classic", "VCDS CSV - classic (groups A, B and C)",
           "The layout VCDS wrote for KW1281 measuring blocks, which simple log viewers read: "
           "at most three groups of four cells to a log, plain numbers, ANSI text. A log of "
           "more than three groups becomes several files or several logs in one file.",
           "_vcds_classic"),
)
DEFAULT_KEY = "ovt"


def by_key(key: str) -> Format:
    for f in FORMATS:
        if f.key == key:
            return f
    raise KeyError(key)


@dataclass
class Options:
    #: classic layout, a log of more than three groups: "logs" (one file, one log after
    #: another -- a reader takes one at a time) or "files" (one file per three groups)
    groups: str = "logs"


def part_path(path: str, n: int, groups) -> str:
    """The file of log ``n`` (1-based) of a log split over several: ``name_part2_groups_11.csv``."""
    stem, ext = os.path.splitext(path)
    return "%s_part%d_groups_%s%s" % (stem, n, "-".join(str(g) for g in groups), ext or ".csv")


def classic_plan(log: "_lf.DataLog") -> List[List[int]]:
    """The group numbers of each log a classic export of ``log`` holds (three blocks to a
    log): one entry when it fits, more when it does not."""
    return [[g for g, _idx in chunk] for chunk in _lf.classic_chunks(log)]


def export(path: str, log: "_lf.DataLog", key: str,
           options: Optional[Options] = None) -> List[str]:
    """Write ``log`` to ``path`` as format ``key``; the paths written (several for a
    classic export that splits the log into files)."""
    opt = options or Options()
    if key == "ovt":
        _lf.write_kwhack(path, log)
        return [path]
    if key == "vcds_advmb":
        _lf.write_vcds(path, log)
        return [path]
    if key == "vcds_classic":
        chunks = _lf.classic_chunks(log)
        if len(chunks) == 1 or opt.groups == "logs":
            _lf.write_vcds_classic(path, log, logs=chunks)
            return [path]
        out = []
        for n, chunk in enumerate(chunks, 1):
            p = part_path(path, n, [g for g, _idx in chunk])
            _lf.write_vcds_classic(p, log, logs=[chunk])
            out.append(p)
        return out
    raise KeyError(key)
