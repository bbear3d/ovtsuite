# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scrub what identifies a car or opens it before anything leaves the PC.

* a VIN anywhere in text (17 characters, no I/O/Q, letters AND digits);
* any field whose name says VIN, immobiliser, SKC/PIN or login code;

Applied to the user's message and every tool result sent to a cloud
service (the setting "redact" is on by default). The tools already avoid
these fields; this is the second line, for text the user types or a module
tool returns.
"""

from __future__ import annotations

import re
from typing import Any, Tuple

VIN = re.compile(r"(?<![A-Z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9]))"
                 r"(?=[A-HJ-NPR-Z0-9]*[0-9])(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])"
                 r"[A-HJ-NPR-Z0-9]{17}", re.I)
KEY = re.compile(r"(^|[^a-z])(vin|immo|immobili[sz]er|skc|pin|login.?code|"
                 r"secret.?key|chassis.?n(umber|o))([^a-z]|$)", re.I)
MASK = "[redacted]"


def text(s: str) -> Tuple[str, int]:
    n = 0

    def sub(m):
        nonlocal n
        n += 1
        return "[VIN]"
    return VIN.sub(sub, s), n


def scrub(obj: Any) -> Tuple[Any, int]:
    """``obj`` with sensitive values replaced; and how many."""
    if isinstance(obj, str):
        return text(obj)
    if isinstance(obj, dict):
        out, n = {}, 0
        for k, v in obj.items():
            if isinstance(k, str) and KEY.search(k) and v not in (None, "", [], {}):
                out[k] = MASK
                n += 1
            else:
                out[k], m = scrub(v)
                n += m
        return out, n
    if isinstance(obj, list):
        out, n = [], 0
        for v in obj:
            v2, m = scrub(v)
            out.append(v2)
            n += m
        return out, n
    return obj, 0
