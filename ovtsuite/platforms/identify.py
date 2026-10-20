# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Name any binary: which platform it is, and which module serves it.

The order is the one ``TunerSession`` has always used, and it matters only
where two tests could both pass, which the reference corpus says they never
do (2,000+ images, see docs/MODULES_PLAN.md):

1. a native container's declared kind wins over sniffing the bytes (an
   ``.edc15`` says what it holds) -- except ``.edc16``, which may wrap any
   ECU image; an HVAC container is recognised by the caller, which passes
   ``hvac=True``;
2. VDO -- exactly 2 KB with a cluster layout's anchors;
3. EDC15 -- a V4.1 dataset signature;
4. ZF 8HP -- a ZF block descriptor placing itself at a calibration base;
5. ZF 6HP -- ZF ident block or shift-schedule family, EDC16 descriptor absent;
6. Aisin -- the 09D checksum descriptor or the TR-80 block trailers;
7. EDC16 -- its CAFEAFFE calibration descriptor;
8. otherwise **unknown**. Nothing is forced into a platform by elimination.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import PLATFORMS, MODULES, UNKNOWN, platform as _platform
from . import aisin as _aisin
from . import edc15 as _edc15
from . import edc16 as _edc16
from . import vdo as _vdo
from . import zf5hp as _zf5hp
from . import zf6hp as _zf6hp
from . import zf8hp as _zf8hp


@dataclass
class Identity:
    """What the base app can say about a binary without any module."""
    platform: str                        # catalogue id, "unknown" when none
    source: str = "structure"            # "container" | "structure" | "none"
    facts: Dict[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.platform != UNKNOWN

    @property
    def module(self) -> str:
        """Id of the module that serves this platform ("" for unknown)."""
        return _platform(self.platform).module

    @property
    def module_name(self) -> str:
        m = MODULES.get(self.module)
        return m.name if m else ""

    @property
    def name(self) -> str:
        return _platform(self.platform).name

    def describe(self) -> str:
        """One line: "ZF 8HP Gen2, software ZX8P4000 (8K0927158AD)"."""
        if not self.known:
            return "Unrecognised binary"
        f = self.facts
        head = self.name
        if f.get("family"):
            head = "%s %s" % (head, f["family"])
        bits = []
        if f.get("software"):
            bits.append("software %s" % f["software"])
        if f.get("ident"):
            bits.append("ident %s" % f["ident"])
        if f.get("part"):
            bits.append(f["part"])
        if f.get("variant"):
            bits.append(f["variant"])
        return head + (", " + ", ".join(bits) if bits else "")


def _facts(pid: str, data: bytes) -> Dict[str, str]:
    """Best-effort identity strings; never raises, never changes the verdict."""
    try:
        if pid == "zf8hp":
            ident = _zf8hp.identify(data)
            out = {k: ident[k] for k in ("software", "part") if ident.get(k)}
            out["family"] = "Gen%d" % _zf8hp.generation(data)
            return out
        if pid == "zf6hp":
            m = _zf6hp._IDENT.search(data)
            return {"ident": m.group(0)[3:-2].decode("latin-1")} if m else {}
        if pid == "edc16":
            from ..fileinfo import ident16
            i = ident16(data)
            out = {}
            if i.get("ecu_system"):
                out["family"] = i["ecu_system"].replace("EDC16", "")
            if i.get("part"):
                out["part"] = i["part"]
            if i.get("dataset_id"):
                out["software"] = i["dataset_id"]
            return out
        if pid == "edc15":
            return {"variant": "%d dataset(s)" % len(_edc15.datasets(data))}
        if pid == "vdo":
            lay = _vdo.layout_of(bytes(data))
            return {"family": lay + " layout"} if lay else {}
        if pid == "aisin":
            return _aisin.describe(data)
        if pid == "zf5hp":
            return _zf5hp.describe(data)
    except Exception:            # identity strings are decoration only
        pass
    return {}


def identify(data: bytes, container_kind: Optional[str] = None,
             hvac: bool = False) -> Identity:
    """The platform of ``data`` (the flat image a loader produced).

    ``container_kind`` is a native container's declared platform, which wins;
    ``hvac`` says the caller recognised a Climatronic container."""
    data = bytes(data)
    # An .edc16 may legitimately wrap any ECU image (it predates the other
    # containers), so its kind is sniffed rather than trusted.
    if container_kind in PLATFORMS and container_kind not in (UNKNOWN, "edc16"):
        return Identity(container_kind, "container",
                        _facts(container_kind, data))
    if hvac:
        return Identity("hvac", "container")
    for pid, test in (("vdo", _vdo.detect), ("edc15", _edc15.detect),
                      ("zf8hp", _zf8hp.detect), ("zf6hp", _zf6hp.detect),
                      ("zf5hp", _zf5hp.detect),
                      ("aisin", _aisin.detect), ("edc16", _edc16.detect)):
        if test(data):
            return Identity(pid, "structure", _facts(pid, data))
    return Identity(UNKNOWN, "none")


def identify_file(path: str) -> Identity:
    """Identify a file on disk, unwrapping the containers the base app reads
    (native containers, .sgo, .frf, .dim). Used by the flasher and the CLI."""
    from .. import container as _cont
    with open(path, "rb") as fh:
        raw = fh.read()
    head = raw[:0x30]
    if raw[:8] == b"OVTHVAC\x01":
        return Identity("hvac", "container")
    fmt = _cont.format_for_magic(head)
    if fmt is not None:
        c = _cont.Container.from_bytes(raw)
        ext = c.external() or b""
        return identify(ext, container_kind=c.kind)
    from .. import sgo as _sgo
    if _sgo.detect(head):
        c = _sgo.parse(raw, path)
        from ..flash import hvac_image as _hi
        if _hi.is_hvac_container(c):
            return Identity("hvac", "container")
        got = identify(_sgo.image(c)[1])
        if not got.known and c.needs_module:
            return Identity(c.needs_module, "container")   # EDC16 .sgo, no module
        return got
    from .. import frf as _frf
    if _frf.detect(head):
        c = _frf.parse(raw, path)
        try:
            return identify(_frf.image(c).data)
        except Exception:
            cal = [b for b in c.blocks if getattr(b, "is_calibration", False)]
            return identify(cal[0].plaintext) if cal else Identity(UNKNOWN, "none")
    from .. import dim as _dim
    if head[:4] in _dim.MAGICS:
        return identify(_dim.external_flash(_dim.load(path)))
    return identify(raw)
