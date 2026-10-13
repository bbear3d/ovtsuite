# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the BASE app knows about every control module it supports.

OVT-Suite ships as a base app plus optional modules (see docs/MODULES_PLAN.md).
The base must name any binary it is handed -- "ZF 8HP Gen2, software
ZX8P4000" -- and say which module gives full support, whether or not that
module is installed. So everything needed to *recognise* a platform lives
here, in the base:

* this catalogue (``PLATFORMS``: display names, which module serves each
  platform, load-step counts; ``MODULES``: the installable modules);
* one detector per platform (``platforms.edc16``, ``.edc15``, ``.zf6hp``,
  ``.zf8hp``, ``.vdo``, ``.aisin``, ``.hvac``), each a format-defined
  structural test -- a descriptor, a signature, a layout anchor -- never a
  size or content guess;
* :func:`ovtsuite.platforms.identify.identify`, which runs them in a fixed
  order and returns an :class:`~ovtsuite.platforms.identify.Identity`.

Definitions, editors and checksums are NOT here: those belong to a platform's
module and are reached through :mod:`ovtsuite.modules.registry`.

Nothing in this package may import a module package (``ovtsuite.edc15``,
``ovtsuite.zf6hp`` ...); ``tests/test_modules.py`` enforces it.
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass(frozen=True)
class ModuleInfo:
    """An installable module, as the base app knows it (installed or not)."""
    id: str
    name: str
    description: str
    platforms: Tuple[str, ...]


@dataclass(frozen=True)
class Platform:
    id: str
    name: str             # long display name
    short: str            # badge text
    module: str           # id of the module that serves it ("" = none)
    steps: int = 4        # load progress steps (approximate, see progress.py)
    kinds: Tuple[str, ...] = field(default=())   # native container kinds


#: Every module the base app can recommend. Order = display order.
MODULES: Dict[str, ModuleInfo] = {m.id: m for m in (
    ModuleInfo("edc16", "Bosch EDC16",
               "EDC16 engine ECUs: definitions, maps, checksum, EEPROM/immo editor.",
               ("edc16",)),
    ModuleInfo("edc15", "Bosch EDC15",
               "EDC15 engine ECUs: definitions, maps, checksum, EEPROM editor.",
               ("edc15",)),
    ModuleInfo("zf6hp", "ZF 6HP",
               "ZF 6HP19/26/28 TCUs: definitions, shift/TCC tools, checksum.",
               ("zf6hp",)),
    ModuleInfo("zf8hp", "ZF 8HP",
               "ZF 8HP Gen1/Gen2 TCUs: definitions and checksum.",
               ("zf8hp",)),
    ModuleInfo("aisin", "Aisin TR-xx",
               "Aisin TR-60SN (09D) and TR-80SD TCUs: identification, and the 09D checksum.",
               ("aisin",)),
    ModuleInfo("vdo", "VDO Cluster",
               "VDO Mk4 instrument clusters: EEPROM definitions, checksum, needle sweep.",
               ("vdo",)),
    ModuleInfo("hvac", "4F/4L HVAC",
               "Audi Climatronic 4F/4L HVAC: firmware variables and checksum.",
               ("hvac",)),
)}

#: Every platform the base app can identify.
PLATFORMS: Dict[str, Platform] = {p.id: p for p in (
    Platform("edc16", "Bosch EDC16", "EDC16", "edc16", 13, ("edc16",)),
    Platform("edc15", "Bosch EDC15", "EDC15", "edc15", 7, ("edc15",)),
    Platform("zf6hp", "ZF 6HP", "6HP", "zf6hp", 11, ("zf6hp",)),
    Platform("zf8hp", "ZF 8HP", "8HP", "zf8hp", 3, ("zf8hp",)),
    Platform("aisin", "Aisin", "AISIN", "aisin", 2, ()),
    Platform("vdo", "VDO instrument cluster", "VDO", "vdo", 4, ("vdo",)),
    Platform("hvac", "Climatronic 4F/4L HVAC", "HVAC", "hvac", 4, ("hvac",)),
    Platform("unknown", "Unrecognised binary", "?", "", 1, ()),
)}

UNKNOWN = "unknown"


def platform(pid: str) -> Platform:
    """The catalogue entry for ``pid`` (the "unknown" entry for anything else)."""
    return PLATFORMS.get(pid or UNKNOWN, PLATFORMS[UNKNOWN])


def module_for(pid: str) -> str:
    """Id of the module that serves platform ``pid``; "" when none does."""
    return platform(pid).module
