# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An XDF's categories renumbered into a given order.

The XDF writer numbers categories as the tables first use them. A WinOLS
project's categories read best in WinOLS's own order (and TunerPro lists
them by number): its code blocks first, then its folders
by name -- :func:`ovtsuite.definitions.winols.category_order`, which the
definition carries as ``info["category_order"]``. This rewrites the
``CATEGORY`` list and every ``CATEGORYMEM`` reference to match; a table's
memberships (and their index, i.e. its nesting) do not change.
"""

from __future__ import annotations

import re
from typing import List

_CAT = re.compile(r'(<CATEGORY index=")0x([0-9A-Fa-f]+)(" name=")([^"]*)(" />)')
_MEM = re.compile(r'(<CATEGORYMEM index="\d+" category=")(\d+)(" />)')


def _esc(s: str) -> str:
    """A name as the XDF writer writes it (character references and all)."""
    from ..xdf_export import _esc as _xdf_esc
    return _xdf_esc(s)


def reorder(text: str, order: List[str]) -> str:
    """``text`` (an XDF) with its categories numbered in ``order`` (names;
    categories not named keep their relative order after them)."""
    cats = [(int(m.group(2), 16), m.group(4)) for m in _CAT.finditer(text)]
    if len(cats) < 2 or not order:
        return text
    rank = {}
    for i, name in enumerate(order):
        rank.setdefault(_esc(name), i)
        rank.setdefault(name, i)
    by_name = sorted(cats, key=lambda c: (rank.get(c[1], len(order)), c[0]))
    new = {old: i for i, (old, _n) in enumerate(by_name)}
    if all(k == v for k, v in new.items()):
        return text
    names = {old: n for old, n in cats}
    lines = iter(sorted(new.items(), key=lambda kv: kv[1]))

    def cat(m):
        old, _i = next(lines)
        return "%s0x%X%s%s%s" % (m.group(1), new[old], m.group(3), names[old], m.group(5))

    def mem(m):
        old = int(m.group(2)) - 1
        return "%s%d%s" % (m.group(1), new.get(old, old) + 1, m.group(3))
    head_end = text.find("</XDFHEADER>")
    head, body = (text[:head_end], text[head_end:]) if head_end >= 0 else (text, "")
    head = _CAT.sub(cat, head)
    return head + _MEM.sub(mem, body)
