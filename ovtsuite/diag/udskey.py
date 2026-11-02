# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Named security-access key algorithms.

A UDS module's ``security_access`` variant names a key algorithm the suite
already implements for that platform (its flash path solved them); this
module is only the lookup from that name to the function, plus the
seed -> key call. No key algorithm lives here -- each entry points at an
existing :mod:`ovtsuite.flash.crypto` function, so the diag path and the
flasher compute identical keys and there is one place a key is defined.

A definition selects one by name::

    variants: { security_access: uds_seed_key }
    security: { key_algorithm: zf6hp_read, key_levels: { 3: zf6hp_read } }

The level (the ``27 <2n-1>`` request sub-function) picks the algorithm when
``key_levels`` maps it; otherwise ``key_algorithm`` applies to every level.
An all-zero seed means "already unlocked" and never calls an algorithm.
"""
from typing import Callable, Dict, Optional

#: name -> (seed bytes -> key bytes). Every value is an existing suite key
#: function; this table never computes a key itself.
_ALGS: Dict[str, Callable[[bytes], bytes]] = {}


def _register_builtin():
    if _ALGS:
        return
    try:
        from ..flash import crypto as _c
    except Exception:
        return
    # the suite's solved VAG key functions, by the name a definition uses
    for name, fn in (("zf6hp_read", getattr(_c, "zf6hp_read_key", None)),
                     ("zf6hp_write", getattr(_c, "zf6hp_write_key", None)),
                     ("zf8hp_gen2_write", getattr(_c, "zf8hp_gen2_write_key", None))):
        if callable(fn):
            _ALGS[name] = fn


def register(name: str, fn: Callable[[bytes], bytes]):
    """Add a key algorithm (a module pack's own, via its AI/diag hook)."""
    _ALGS[str(name)] = fn


def algorithms() -> Dict[str, Callable[[bytes], bytes]]:
    _register_builtin()
    return dict(_ALGS)


def key_for(name: str, seed: bytes) -> Optional[bytes]:
    """The key for ``seed`` under algorithm ``name``, or None when the name
    is unknown (the caller reports that; it never guesses a key)."""
    _register_builtin()
    fn = _ALGS.get(str(name))
    if fn is None:
        return None
    return bytes(fn(bytes(seed)))


__all__ = ["key_for", "register", "algorithms"]
