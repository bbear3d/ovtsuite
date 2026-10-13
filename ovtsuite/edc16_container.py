# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT native EDC16 flash container (.edc16).

The format now lives in :mod:`ovtsuite.container`, shared with ``.edc15``,
``.6hp`` and ``.hvac``; this module keeps the names callers and tests have
always imported. The ``.edc16`` layout itself is unchanged -- see
``ovtsuite/container.py`` for the specification.
"""

from .container import (                                    # noqa: F401
    BLOCK_BOOT, BLOCK_CAL, BLOCK_DEFN, BLOCK_EEPROM, BLOCK_EXTERNAL,
    BLOCK_LOADER, BLOCK_LOG, BLOCK_META, BLOCK_MPC, BLOCK_PROGRAM,
    COMP_STORE, COMP_ZLIB, FORMATS, VERSION, Block, Edc16Container,
)
from .container import is_container as _is_container

MAGIC = FORMATS["edc16"].magic
EXT = FORMATS["edc16"].ext


def is_container(path_or_bytes) -> bool:
    """True if the path/bytes look like an .edc16 container (by magic)."""
    return _is_container(path_or_bytes, "edc16")
