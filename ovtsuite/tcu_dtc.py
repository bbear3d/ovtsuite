# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Gearbox (TCU) fault codes: what a module hands the base app's window.

A gearbox module that can switch its faults implements :class:`FaultModel`
and names it in its ``faults`` hook (``ModuleSpec.faults``); the base app's
window (:mod:`ovtsuite.gui_tcu_dtc`) shows any such model the same way --
search by any code form, switch rows singly or in bulk, restore from a stock
file, compare with a second file. What a row is and which bytes switch it
is entirely the module's business: on a ZF 6HP it is a code whose on/off
byte belongs to its whole fault class, on a ZF 8HP a fault with its own
on/off bit.

Every write is returned as ``(address, bytes)`` and made by the window
through the session's undo stack, never by the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Edit = Tuple[int, bytes]


@dataclass
class FaultRow:
    """One row of the window."""
    key: Any                     # stable across reads and between two files
    ident: str                   # the fault's own number: "14.1", "ZF 23"
    sort: Any                    # how the ident column sorts
    on: bool
    can_switch: bool = True
    why_not: str = ""            # why it can't be switched, when it can't
    vag: List[str] = field(default_factory=list)      # as a scan tool shows them
    pcodes: List[str] = field(default_factory=list)   # P/C/B/U codes
    group: str = ""              # what it is switched with ("class 14"), if anything
    desc: str = ""
    detail: List[str] = field(default_factory=list)   # extra lines for the panel
    vag_numbers: List[int] = field(default_factory=list)  # for matching an ECU read

    def search_text(self) -> str:
        return " ".join([self.ident, self.group, self.desc] + self.vag
                        + self.pcodes).lower()


@dataclass
class Plan:
    """What switching some rows would write."""
    edits: List[Edit] = field(default_factory=list)
    label: str = ""
    #: keys of every row the edits change -- more than were asked for when
    #: rows share an on/off byte
    affected: List[Any] = field(default_factory=list)
    #: rows that could not be switched, with the reason
    refused: List[str] = field(default_factory=list)
    #: a sentence the window shows before writing, when there is something
    #: the user should agree to (other rows switching with these)
    confirm: str = ""


class FaultModel:
    """The protocol (subclass it or duck-type it)."""

    #: window title
    title = "Gearbox fault codes"
    #: the views the window offers (coding variants); [] = one view
    variants: List[str] = []
    #: when set, a check box with this text makes a switch apply to every
    #: variant (ticked by default) -- the car uses one, which is not known
    all_variants_text = ""
    #: shown under the title (the format's catch, if it has one)
    note = ""
    #: column heading for :attr:`FaultRow.group`; "" hides the column
    group_heading = ""

    def source_text(self) -> str:
        return ""

    def refresh(self, data: bytes) -> None:
        """Re-read the state from ``data`` (after an edit, an undo...)."""

    def rows(self, variant: int = 0) -> List[FaultRow]:
        raise NotImplementedError

    def plan(self, keys: Sequence[Any], on: bool, variant: int = 0,
             all_variants: bool = True) -> Plan:
        raise NotImplementedError

    def restore_plan(self, stock: "FaultModel") -> Plan:
        """Every row set back to ``stock``'s state (a file of the same
        software)."""
        raise NotImplementedError

    def same_layout(self, other: "FaultModel") -> bool:
        """Can rows of ``other`` be paired with this model's by key?"""
        return True


def pcode_text(code: str) -> str:
    """The standard description of a P/C/B/U code ('' if not known)."""
    try:
        from .dtc import _load_pdesc
        return _load_pdesc().get(code, "") or ""
    except Exception:
        return ""


def model_for(session) -> Tuple[Optional[FaultModel], str]:
    """``(model, "")`` for a session whose module gives one, else
    ``(None, why)``."""
    if session is None:
        return None, "nothing is open"
    from .modules import registry as _registry
    pid = getattr(session, "platform", "") or ""
    m = _registry.for_platform(pid)
    if m is None or not getattr(m, "faults", None) or \
            getattr(m, "variant", "full") != "full":
        return None, "the %s module is not installed or has no fault-code support" % (
            pid or "platform")
    try:
        model = m.call("faults", session)
    except Exception as ex:
        return None, str(ex) or type(ex).__name__
    if model is None:
        return None, "this file has no fault-code table the module can read"
    return model, ""
