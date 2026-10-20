# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Channels the logger works out from other cells of the same row.

Nothing is read from the ECU for them: the value is computed when the row is
read, written to the file like any other column, and marked ``=<name>`` in the
``# Cells:`` line so a log read back knows the column was derived.

``eoi`` -- end of injection, degrees ATDC (negative = before TDC):
``duration - start of injection``, from the row's SOI (deg BTDC) and
injection-duration (crank degrees) cells. The same convention as Tune from
logs and Tools > End of injection. Added to the Timing preset when both cells
are logged (EDC16), and offered when both are ticked by hand.

EDC16 cells are recognised by their A2L signal; EDC15P (PD) cells by the
setpoints the measuring blocks carry, start of delivery ``zmmFBsoll`` and
delivery duration ``zmmFDsoll`` (named from their KW1281 message numbers, see
``edc15.msblock15.MESSAGE_SIGNALS``). A crank angle sent with KW1281 formula 4
or 27 arrives as a size plus "BTDC"/"ATDC" in its text; the text decides the
sign, so an injection starting after TDC counts as a negative advance.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .logfile import Channel

EOI = "eoi"
EOI_LABEL = "End of injection (calculated)"
EOI_UNIT = "deg ATDC"


def eoi_channel() -> Channel:
    return Channel(0, 0, label=EOI_LABEL, unit=EOI_UNIT, signal="EOI",
                   derived=EOI)


#: EDC15P measuring-block setpoints, which the engine roles (EDC16 A2L
#: signals) do not list.
_EDC15_ROLES = {"zmmFBsoll": "soi", "zmmFDsoll": "dur"}


def _role(c: Channel) -> Optional[str]:
    from ..logtune.roles import role_of_signal, _label_symbol
    for sig in (getattr(c, "signal", "") or "",
                _label_symbol(getattr(c, "label", "") or "")):
        r = (role_of_signal(sig) or _EDC15_ROLES.get(sig)) if sig else None
        if r:
            return r
    return None


def advance(value, text: str = "") -> Optional[float]:
    """Degrees before TDC from a logged angle cell: the text's "ATDC" makes it
    negative, "BTDC" positive; a plain number is taken as it stands."""
    if value is None:
        return None
    v = float(value)
    t = (text or "").upper()
    if "ATDC" in t:
        return -abs(v)
    if "BTDC" in t:
        return abs(v)
    return v


def sources(channels: Sequence[Channel]) -> Tuple[Optional[int], Optional[int]]:
    """Indices of the SOI and duration channels among ``channels``."""
    soi = dur = None
    for i, c in enumerate(channels):
        if c.is_derived:
            continue
        r = _role(c)
        if r == "soi" and soi is None:
            soi = i
        elif r == "dur" and dur is None:
            dur = i
    return soi, dur


def can_add_eoi(channels: Sequence[Channel]) -> bool:
    soi, dur = sources(channels)
    return soi is not None and dur is not None and \
        not any(c.derived == EOI for c in channels)


def fill(channels: Sequence[Channel], texts: List[str],
         values: List[Optional[float]], plan=None) -> None:
    """Fill the derived channels' slots of one row in place. ``plan`` is
    ``sources(channels)``, computed once per recording."""
    if not any(c.is_derived for c in channels):
        return
    soi, dur = plan if plan is not None else sources(channels)
    for i, c in enumerate(channels):
        if c.derived != EOI:
            continue
        v = None
        if soi is not None and dur is not None:
            a, b = advance(values[soi], texts[soi]), values[dur]
            if a is not None and b is not None:
                v = float(b) - float(a)
        values[i] = v
        texts[i] = ("%.1f %s" % (v, EOI_UNIT)) if v is not None else "N/A"
