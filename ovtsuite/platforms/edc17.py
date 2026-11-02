# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC17 images: format-defined identification (base-app knowledge).

Every EDC17 image carries the EPK Bosch stamps into its calibration -- the
string the DAMOS A2L repeats in its ``EPK`` statement and the flash tools
check -- for example::

    35/1/EDC17_CP14/5/P680//C680I3WF///
    36/1/EDC17C74/5/P1274//C1274DT8K1///

read as: format, ?, ECU variant, ?, DAMOS project (``P680``), software id
(``C680I3WF``). It names the software exactly, which is what selects a
definition, so it is also the positive test: an image is EDC17 when it carries
one. A MED17 image has the banner ``ME(D)/EDC17`` but an EPK of another form
(``38/1/MED17/3///D17121V05``) and is not taken for one.

The EPK of an EDC17CP52 is written another way
(``51/1/EDC17CP52_C1/242/P_591//P_591_EDC17CP52_473//``); it is recognised too.
"""
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

#: ``(format)/(?)/(ECU)/(?)/(project)//(software)///``
_EPK = re.compile(rb"[0-9]{2}/[0-9]{1,2}/(EDC17[A-Z0-9_]*)/[0-9]+/(P[0-9]+)//(C[0-9A-Za-z_]+)///")
_EPK_CP52 = re.compile(rb"[0-9]{2}/1/(EDC17CP52)_C1/[0-9]+/(P_[0-9]+)//(P_[0-9A-Za-z_]+)//")
#: a VAG spare part number as an ASCII ident (03L906022GP), and Bosch's software number
_PART = re.compile(rb"\b[0-9][0-9A-Z]{2}9[0-9]{5}[A-Z]{0,3}\b")
_BOSCH_SW = re.compile(rb"\b10(?:37|39)[0-9]{6}\b")

Epk = Tuple[str, str, str]          # (ecu, project, software), e.g. ("EDC17CP14", "P680", "C680I3WF")


def epks(data: bytes) -> List[Epk]:
    """Every EPK in ``data``, commonest first (an image can mention another
    software's EPK in a table). The ECU name has its underscore removed."""
    seen: Counter = Counter()
    for rx in (_EPK, _EPK_CP52):
        for m in rx.finditer(data):
            seen[(m.group(1).decode().replace("_", ""), m.group(2).decode(),
                  m.group(3).decode())] += 1
    return [k for k, _n in seen.most_common()]


def detect(data: bytes) -> bool:
    """True when ``data`` carries an EDC17 EPK."""
    return bool(epks(data))


def ident(data: bytes) -> Optional[Dict[str, str]]:
    """What the image says it is: ``ecu`` (``EDC17CP14``), ``variant``
    (``CP14``), ``project`` (``P680``), ``software`` (``C680I3WF``), and, when
    printed in it, ``part`` and ``bosch_sw``. None when it is not an EDC17."""
    found = epks(data)
    if not found:
        return None
    ecu, project, software = found[0]
    out = {"ecu": ecu, "variant": ecu[5:], "project": project, "software": software}
    # the engine software's part number (group 906) over the ECU's own (907)
    parts = []
    for m in _PART.finditer(data):
        parts.append(m.group(0).decode())
        if len(parts) >= 40:
            break
    if parts:
        out["part"] = next((p for p in parts if p[3:6] == "906"), parts[0])
    m = _BOSCH_SW.search(data)
    if m:
        out["bosch_sw"] = m.group(0).decode()
    return out
