# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the command palette (Ctrl+K, docs/OVT Diag Interface Options.pdf page
2) searches, and how: one box, any noun -- a module, a measuring value, a
coding bit, a fault code, an adaptation channel, an actuator test, a
procedure, an action.

Qt-free. The window builds the :class:`PaletteItem` list from what it knows
(:mod:`ovtsuite.diag.palette_items`); the palette widget (:mod:`gui_palette`)
shows the :func:`search` result and reports the choice.

THE CONTRACT (keep it stable; the window and the widget are written
against it):

``PaletteItem``  one searchable thing
    ``kind``      ``"module" | "live" | "fault" | "coding" | "adaptation" |
                  "actuator" | "procedure" | "action"``
    ``addr``      module address ("" for the vehicle and for actions)
    ``title``     what is shown big (``"Boost pressure, actual"``)
    ``subtitle``  the muted line (``"Engine . group 011 cell 3 . Ldm_pIst from
                  loaded calibration"``)
    ``keywords``  extra searchable text that is not shown (a signal name, the
                  VAG number ``"008857"``, a synonym)
    ``value``     right-aligned text (a live value ``"1012 mbar"``)
    ``chip`` / ``chip_kind``  a right-aligned status chip (``"active"`` bad,
                  ``"history"`` ok, ``"warn"``)
    ``target``    right-aligned destination (``"Actuator tests >"``); default
                  by kind: see :data:`DEFAULT_TARGET`
    ``payload``   opaque dict the window uses to act on the choice
                  (``{"group": 11, "cell": 3}``)

``search(items, query, kind=None) -> Result``
    ``Result.hits``    ``[Hit]`` best first, grouped later by the widget;
                       ``Hit.item``, ``Hit.score``, ``Hit.spans`` (title match
                       ranges ``[(start, end)]`` for highlighting)
    ``Result.counts``  ``{kind: n, "all": n}`` over ALL matches (before the
                       ``kind`` filter), for the filter chips
    Query rules: whitespace-separated tokens must ALL match (case-insensitive,
    in title, subtitle or keywords, word-start matches score higher); a
    leading ``NN:`` (two hex digits) restricts to that module's things
    (``01: boost``); a leading ``>`` shows only ``action`` items; a token that
    looks like a fault code (``P0299``, ``008857``, ``0x0414``) matches faults
    by code in either encoding; an empty query returns the modules, the
    actions and the procedures only (not thousands of cells).

``GROUPS``  how the widget groups kinds under headings, in order:
    ``[("LIVE VALUES", ("live",)), ("FAULTS", ("fault",)), ("MODULES",
    ("module",)), ("CODING", ("coding",)), ("ACTUATOR TESTS . ADAPTATION .
    PROCEDURES", ("actuator", "adaptation", "procedure")), ("ACTIONS",
    ("action",))]``
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

KINDS = ("module", "live", "fault", "coding", "adaptation", "actuator", "procedure", "action")

DEFAULT_TARGET = {"module": "Open >", "live": "Live data >", "fault": "Faults >", "coding": "Coding >",
                  "adaptation": "Adaptation >", "actuator": "Actuator tests >", "procedure": "Procedures >",
                  "action": ""}

GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    ("LIVE VALUES", ("live",)),
    ("FAULTS", ("fault",)),
    ("MODULES", ("module",)),
    ("CODING", ("coding",)),
    ("ACTUATOR TESTS · ADAPTATION · PROCEDURES", ("actuator", "adaptation", "procedure")),
    ("ACTIONS", ("action",)),
]


@dataclass
class PaletteItem:
    kind: str
    title: str
    addr: str = ""
    subtitle: str = ""
    keywords: str = ""
    value: str = ""
    chip: str = ""
    chip_kind: str = ""
    target: str = ""
    payload: dict = field(default_factory=dict)

    def target_text(self) -> str:
        return self.target or DEFAULT_TARGET.get(self.kind, "")


@dataclass
class Hit:
    item: PaletteItem
    score: float = 0.0
    spans: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class Result:
    hits: List[Hit] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------- the index
_MODULE_PREFIX = re.compile(r"^\s*([0-9A-Fa-f]{2}):\s*(.*)$", re.S)
_ACTION_PREFIX = re.compile(r"^\s*>\s*(.*)$", re.S)
_OBD_CODE = re.compile(r"(?<![0-9A-Za-z])([PCBUpcbu])([0-9A-Fa-f]{4})(?![0-9A-Za-z])")
_VAG_CODE = re.compile(r"(?<![0-9A-Za-z])(\d{5,6})(?![0-9A-Za-z])")
_OBD_BASE = {"P": 0x0000, "C": 0x4000, "B": 0x8000, "U": 0xC000}
_EMPTY_QUERY_KINDS = ("module", "action", "procedure")

# Score of one token in one place: a word start in the title beats the same in
# the subtitle / keywords, which beat a match in the middle of a word.
_S_TITLE_PREFIX, _S_TITLE_WORD, _S_TITLE_ANY = 100.0, 80.0, 40.0
_S_KEY_WORD, _S_KEY_ANY = 30.0, 15.0
_S_SUB_WORD, _S_SUB_ANY = 25.0, 12.0
_S_CODE = 90.0


def _fold(s: str) -> str:
    """Lower-case that never changes the length (so match ranges index the
    original text; ``"İ".lower()`` and friends would shift them)."""
    low = s.lower()
    if len(low) == len(s):
        return low
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in s)


def _norm_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    try:
        return "%02x" % int(a, 16)
    except ValueError:
        return a


def _fault_codes(item: PaletteItem) -> Set[int]:
    """The raw 16-bit codes a fault item answers to: an OBD ``P0299`` and a
    VAG number ``008857`` are both one number (``0x0299`` / ``8857``), so a
    query in either encoding, or as ``0x2299``, finds it."""
    out: Set[int] = set()
    for text in (item.title, item.keywords, item.subtitle):
        if not text:
            continue
        for m in _OBD_CODE.finditer(text):
            out.add(_OBD_BASE[m.group(1).upper()] + int(m.group(2), 16))
        for m in _VAG_CODE.finditer(text):
            out.add(int(m.group(1)))
    return out


class _Entry:
    """What is precomputed for one item: lower-cased text and fault codes."""
    __slots__ = ("item", "title", "sub", "kw", "hay", "codes", "addr", "tlen")

    def __init__(self, item: PaletteItem):
        self.item = item
        self.title = _fold(item.title or "")
        self.sub = _fold(item.subtitle or "")
        self.kw = _fold(item.keywords or "")
        self.hay = self.title + "\n" + self.sub + "\n" + self.kw
        self.tlen = len(item.title or "")
        self.addr = _norm_addr(item.addr)
        self.codes: Set[int] = _fault_codes(item) if item.kind == "fault" else set()


_CACHE: Dict[int, _Entry] = {}


def _entry(item: PaletteItem) -> _Entry:
    e = _CACHE.get(id(item))
    if e is None or e.item is not item:
        e = _CACHE[id(item)] = _Entry(item)
    return e


def _token_code(tok: str) -> Optional[int]:
    """The raw code a query token spells, or None if it is not code-like."""
    if re.fullmatch(r"0x[0-9a-f]{1,4}", tok):
        return int(tok[2:], 16)
    if re.fullmatch(r"[pcbu][0-9a-f]{4}", tok):
        return _OBD_BASE[tok[0].upper()] + int(tok[1:], 16)
    if re.fullmatch(r"\d{3,6}", tok):
        return int(tok)
    return None


def _find(text: str, tok: str) -> Tuple[int, int]:
    """``(rank, start)`` of the best occurrence of ``tok`` in ``text``: rank 2
    = at the very start, 1 = at a word start, 0 = inside a word, -1 = absent."""
    i = text.find(tok)
    if i < 0:
        return -1, -1
    best = (-1, -1)
    while i >= 0:
        if i == 0:
            return 2, 0
        rank = 1 if not text[i - 1].isalnum() else 0
        if rank > best[0]:
            best = (rank, i)
            if rank == 1:
                return best
        i = text.find(tok, i + 1)
    return best


def _merge(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _score_entry(e: _Entry, toks: Sequence[str], codes: Sequence[Optional[int]]) -> Optional[Hit]:
    total = 0.0
    spans: List[Tuple[int, int]] = []
    hay = e.hay
    for tok, code in zip(toks, codes):
        best = -1.0
        if tok in hay:
            r, at = _find(e.title, tok)
            if r >= 0:
                best = _S_TITLE_PREFIX if r == 2 else (_S_TITLE_WORD if r == 1 else _S_TITLE_ANY)
                spans.append((at, at + len(tok)))
            else:                       # a title match outscores any other place
                r, _ = _find(e.kw, tok)
                if r >= 0:
                    best = _S_KEY_WORD if r >= 1 else _S_KEY_ANY
                r, _ = _find(e.sub, tok)
                if r >= 0:
                    best = max(best, _S_SUB_WORD if r >= 1 else _S_SUB_ANY)
        if code is not None and code in e.codes:
            best = max(best, _S_CODE)
        if best < 0:
            return None
        total += best
    total -= min(e.tlen, 100) * 0.05            # shorter titles first, slightly
    return Hit(e.item, total, _merge(spans))


def search(items: Sequence[PaletteItem], query: str, kind: Optional[str] = None) -> Result:
    """Find ``query`` in ``items`` (rules in the module docstring)."""
    if len(_CACHE) > 4 * len(items) + 2000:
        _CACHE.clear()
    q = query or ""
    only_addr: Optional[str] = None
    only_actions = False
    m = _MODULE_PREFIX.match(q)
    if m:
        only_addr, q = m.group(1).lower(), m.group(2)
    else:
        m = _ACTION_PREFIX.match(q)
        if m:
            only_actions, q = True, m.group(1)
    toks = [_fold(t) for t in q.split()]
    codes = [_token_code(t) for t in toks]

    matches: List[Tuple[float, int, Hit]] = []
    for idx, item in enumerate(items):
        e = _entry(item)
        if only_addr is not None and e.addr != only_addr:
            continue
        if only_actions and item.kind != "action":
            continue
        if not toks:
            if only_addr is None and not only_actions and item.kind not in _EMPTY_QUERY_KINDS:
                continue
            matches.append((0.0, idx, Hit(item, 0.0, [])))
            continue
        hit = _score_entry(e, toks, codes)
        if hit is not None:
            matches.append((hit.score, idx, hit))

    counts: Dict[str, int] = {"all": len(matches)}
    for _, _, h in matches:
        counts[h.item.kind] = counts.get(h.item.kind, 0) + 1
    if kind and kind != "all":
        matches = [x for x in matches if x[2].item.kind == kind]
    matches.sort(key=lambda x: (-x[0], x[1]))
    return Result([x[2] for x in matches], counts)
