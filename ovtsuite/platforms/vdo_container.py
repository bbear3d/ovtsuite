# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.vdo`` container: a VDO cluster EEPROM in OVT's native format.

Same block-directory file as ``.edc16`` / ``.edc15`` / ``.6hp`` / ``.hvac``
(:mod:`ovtsuite.container`), magic ``OVTVDO\\0\\0``:

* ``BLOCK_EXTERNAL`` -- the 2048-byte EEPROM, the platform's main image.
* ``BLOCK_META`` -- JSON: what the cluster said about itself when it was read
  (``rom_version`` "VWK501MH 01.10" with ``rom_source`` "cluster" -- or
  "patch header" when it was only derived from the EEPROM, ``ident`` "1J0920806M  KOMBI+WEGFAHRSP
  VDO V07", ``coding``, ``workshop``), the identified ``layout`` / ``family``,
  ``read_at`` and ``source``. ``rom_version`` from a cluster read is the most
  authoritative identity there is (see :mod:`ovtsuite.vdo.identify`), which
  is the main reason this container exists rather than a bare ``.bin``.
* ``BLOCK_LOG`` -- the change log / version history, like every container.

Opening a ``.vdo`` needs nothing else: identification reads the metadata and
the EEPROM's own structure, and the definition is a small JSON file.
"""

import datetime
from typing import Optional

from .. import branding as _branding
from .. import container as _cont

KIND = "vdo"
EXT = ".vdo"

#: Metadata keys this module owns (anything else in BLOCK_META is kept).
META_KEYS = ("rom_version", "rom_source", "ident", "coding", "workshop", "layout", "family",
             "read_at", "source", "sw_blocks")


def new_container(eeprom: bytes, meta: Optional[dict] = None):
    """A fresh ``.vdo`` container for ``eeprom`` (e.g. straight off a cluster
    read). ``meta`` is merged into the stored metadata."""
    c = _cont.Container(_cont.format_for_kind(KIND))
    c.add(_cont.BLOCK_EXTERNAL, bytes(eeprom), name="eeprom")
    m = {"source": _branding.APP_NAME}
    try:
        from ..vdo.identify import identify
    except ImportError:
        # VDO module not installed: the flasher still saves its reads, with
        # the layout the base app can read structurally.
        from .vdo import layout_of
        lay = layout_of(bytes(eeprom))
        if lay is not None:
            m["layout"] = lay
    else:
        try:
            ident = identify(bytes(eeprom), meta or {})
            if ident is not None:
                m["layout"] = ident.layout
                m["family"] = ident.family
                if ident.rom_version and not (meta or {}).get("rom_version"):
                    m["rom_version"] = ident.rom_version
                    m["rom_source"] = ident.rom_source
        except Exception:
            pass
    m.update({k: v for k, v in (meta or {}).items() if v not in (None, "")})
    c.set_meta(m)
    return c


def container_bytes(eeprom: bytes, meta: Optional[dict] = None) -> bytes:
    return new_container(eeprom, meta).to_bytes()


def fill_meta(c, session) -> None:
    """Refresh ``c``'s metadata from ``session`` before a save: keep what a
    cluster read recorded, add the identity this session settled on."""
    m = c.meta()
    m.update({k: v for k, v in (getattr(session, "vdo_meta", None) or {}).items()
              if v not in (None, "")})
    ident = getattr(session, "vdo_identity", None)
    if ident is not None:
        m["layout"] = ident.layout
        m["family"] = getattr(session, "family", ident.family)
        if ident.rom_version:
            m["rom_version"] = ident.rom_version
            m["rom_source"] = ident.rom_source or "cluster"
    m.setdefault("source", _branding.APP_NAME)
    m.setdefault("saved_first", datetime.datetime.now().isoformat(
        timespec="seconds"))
    c.set_meta(m)
