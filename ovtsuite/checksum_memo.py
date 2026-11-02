# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Images whose checksums are already known to be right.

Verifying or correcting a 2 MB EDC16 image sums every block and regenerates
its digests; the editor does that in the background after each edit and on
every save, and the flasher did it again, twice, on the window's thread when
Write was pressed -- the window froze for the same image it had just been told
was fine. An image is *settled* for a provider and options when correcting it
gives it back unchanged (it is a fixed point of the correction) or when the
provider's own verify says it is valid. Whoever finds that out says so here
(:func:`mark`); whoever is about to do the work asks first (:func:`settled`).

The key is the provider, its options and a hash of the bytes, so any edit makes
the image a different one, and a different option (the EDC16 digest on or off)
a different question. Options that are not plain values (an MPC's key image)
are never remembered. A few images are kept, newest last.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

_MAX = 16
_known: "OrderedDict[tuple, bool]" = OrderedDict()
_lock = threading.Lock()


def _key(cs, data, opts):
    """The memo key, or None when this question is not one to remember."""
    items = []
    for k, v in sorted((opts or {}).items()):
        if not isinstance(v, (bool, int, str, type(None))):
            return None
        items.append((k, v))
    t = type(cs)
    return (t.__module__, t.__qualname__, tuple(items), len(data),
            hashlib.blake2b(data, digest_size=16).digest())


def settled(cs, data, opts=None) -> bool:
    """Is ``data`` known to need no correction by ``cs`` with ``opts``?"""
    try:
        k = _key(cs, data, opts)
    except Exception:                       # noqa: BLE001 - only an optimisation
        return False
    if k is None:
        return False
    with _lock:
        return k in _known


def mark(cs, data, opts=None) -> None:
    """Remember that ``data`` needs no correction by ``cs`` with ``opts``."""
    try:
        k = _key(cs, data, opts)
    except Exception:                       # noqa: BLE001
        return
    if k is None:
        return
    with _lock:
        _known[k] = True
        _known.move_to_end(k)
        while len(_known) > _MAX:
            _known.popitem(last=False)


def clear() -> None:
    with _lock:
        _known.clear()
