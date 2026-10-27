# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table as a difference instead of absolute values.

Pure arithmetic and text, no Qt: :class:`gui.TableModel` asks it what a
comparison cell shows (:mod:`ovtsuite.gui_compare_delta` owns the controls).
Values arrive in display units -- the same factor the table's own cells use --
so a delta reads in the units on screen.

Modes (the first is today's view):

``abs``    the comparison's own values
``d1``     this file - comparison   (positive: this file is higher; the
           default delta, because it reads as "what I changed")
``d2``     comparison - this file
``pct``    (this file - comparison) / |comparison|, in percent
``ratio``  this file / comparison

Hex maps show a difference as a signed hex byte (``+0A``); a percentage or
ratio stays decimal. Text (ASCII) maps stay as they are. A comparison value of
0 has no percentage or ratio: those cells show
:data:`NONE_TEXT`. A cell that does not differ shows "0" (percent "0%",
ratio "1"), or nothing when "blank unchanged cells" is on.
"""
from __future__ import annotations

from typing import Optional, Tuple

MODES: Tuple[Tuple[str, str], ...] = (
    ("abs", "Absolute values"),
    ("d1", "Difference: this file - comparison"),
    ("d2", "Difference: comparison - this file"),
    ("pct", "Percent change from comparison"),
    ("ratio", "Ratio: this file / comparison"),
)
#: the banner's wording for each mode
BANNER = {
    "d1": "difference (this file - comparison)",
    "d2": "difference (comparison - this file)",
    "pct": "percent change from comparison",
    "ratio": "ratio (this file / comparison)",
}
#: where the mode control is offered
CONTROLS: Tuple[Tuple[str, str], ...] = (
    ("banner", "On the comparison banner"),
    ("toolbar", "On a toolbar"),
    ("both", "On the banner and a toolbar"),
    ("none", "Nowhere (Options only)"),
)
NONE_TEXT = "-"


def valid(mode: str) -> str:
    return mode if mode in dict(MODES) else "abs"


def delta(mode: str, this: float, other: float) -> Optional[float]:
    """The mode's number for a cell, or None where it is undefined."""
    if mode == "d1":
        return this - other
    if mode == "d2":
        return other - this
    if mode == "pct":
        return None if other == 0 else (this - other) / abs(other) * 100.0
    if mode == "ratio":
        return None if other == 0 else this / other
    return None


def zero_text(mode: str) -> str:
    return {"pct": "0%", "ratio": "1"}.get(mode, "0")


def text(mode: str, d: Optional[float], differs: bool, dec: int = 3,
         blank: bool = False) -> str:
    """What the cell shows."""
    if not differs:
        return "" if blank else zero_text(mode)
    if d is None:
        return NONE_TEXT
    if mode == "pct":
        return "%+.1f%%" % d
    if mode == "ratio":
        return "%.3f" % d
    return "%+.*f" % (dec, d)


def hex_text(mode: str, d: Optional[float], differs: bool,
             blank: bool = False) -> str:
    """:func:`text` for a map of hex bytes: a difference is a signed hex
    byte (``+0A``, ``-04``; ``00`` where nothing differs); a percentage or
    a ratio is a decimal number, as for any other map."""
    if mode in ("pct", "ratio") or d is None or not differs:
        if mode in ("d1", "d2") and not differs and not blank:
            return "00"
        return text(mode, d, differs, 0, blank)
    n = int(round(abs(d))) & 0xFF if abs(d) < 256 else int(round(abs(d)))
    return ("+" if d > 0 else "-") + "%02X" % n


def sign(mode: str, d: Optional[float]) -> int:
    """+1 above no change, -1 below, 0 when undefined (the tint's direction).
    A ratio is "above" when over 1."""
    if d is None:
        return 0
    if mode == "ratio":
        return 1 if d > 1 else -1 if d < 1 else 0
    return 1 if d > 0 else -1 if d < 0 else 0
