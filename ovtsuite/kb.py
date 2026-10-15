# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Per-family reference knowledge base.

Each family ships as a gzipped JSON distilled from a reference XDF by
``tools/generate_kb.py`` (the rs6_lookup pattern from hp6tuner, kept as data
files rather than generated .py because the descriptions make them large).

Families:
  p350        EDC16U31 P350 (1.9L BEW / 2.0L BHW) -- from the 038997016Q XDF
  p475_83     EDC16U31 P475 8.3 (BPW)             -- from the KN/LQ XDF
  p475_84_mf  EDC16U31 P475 8.4 (Audi A6 103KW)   -- from the MF XDF
  p475_84_hq  EDC16U31 P475 8.4 (Audi A6 136HP)   -- from the HQ XDF

EDC15 families (vm_alh, p_pd) live in ``edc15/kb15.py`` with their own
dataset-relative record format.
"""

import base64
import gzip
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _path(name: str) -> str:
    """A KB data file wherever its module is installed (ovtsuite.datafiles);
    the base-app path when nothing provides it, so a missing family still
    fails with the file it looked for."""
    from . import datafiles
    return datafiles.path(name) or os.path.join(_DATA_DIR, name)

FAMILIES = ("p350", "p475_83", "p475_84_mf", "p475_84_hq", "p447",
            "p447_906016",
            # EDC16U1 (1 MB, 03G906016 PD): a full Bosch-A2L catalogue since
            # 2026-10-13, so it competes in detection like the others. It wins
            # every 1 MB U1 image (0.79-0.99 vs p350's ~0.5), including ones
            # with no readable dataset id; 2 MB images score ~0.
            "u1_379u85b6",
            # EDC16U1 V10 TDI (P216, A2L C216U5.K.T.1): the other V10
            # datasets (070906016AA P21665KX, the Phaeton) are no exact match
            # but score 0.45-0.54 against it where every other family scores
            # under 0.1, and its placements agree with the structural map
            # finder on every tuning map but one
            "p216_5kt1",
            # EDC16U1 R5 2.5 TDI (070906016N, A2L R5-TDI / project C213)
            "p213_r5tdi")


@dataclass
class KbTable:
    name: str
    kind: str                 # 'map' | 'curve' | 'indexed'
    struct_addr: Optional[int]
    x_addr: Optional[int]
    x_count: int
    y_addr: Optional[int]
    y_count: int
    rows: int
    cols: int
    z_addr: int
    z_bits: int
    z_signed: bool
    z_equation: str
    x_equation: str
    y_equation: str
    units: str
    x_units: str
    y_units: str
    categories: List[str]
    description: str
    # baked matching data (so no reference binary is needed at runtime):
    fp: str = ""              # base64 structure/axis fingerprint bytes
    fp_addr: int = 0          # where the fingerprint starts (reference space)
    fp_contig: bool = True    # Z data directly follows the structure
    vhash: int = 0            # crc32 of the reference Z bytes (verify check)
    cat: Optional[dict] = None  # baked tuning-catalogue record (catalogue.py)

    @property
    def fp_bytes(self) -> bytes:
        return base64.b64decode(self.fp) if self.fp else b""


@dataclass
class KbConstant:
    name: str
    addr: int
    bits: int
    signed: bool
    equation: str
    units: str
    categories: List[str]
    description: str
    vhash: int = 0            # crc32 of the reference constant bytes
    cat: Optional[dict] = None  # baked tuning-catalogue record (catalogue.py)


@dataclass
class KbSignature:
    """A masked byte-signature entity: located by searching the target
    for ``sig`` (hex with '??' wildcards); the value sits at ``field``
    bytes into each match. Version-independent placement for code-region
    parameters the A2L does not describe."""
    name: str
    sig: str
    field: int
    bits: int
    signed: bool
    equation: str
    units: str
    categories: List[str]
    description: str
    multi: bool = False
    cat: Optional[dict] = None  # baked tuning-catalogue record (catalogue.py)


@dataclass
class Kb:
    family: str
    title: str
    tables: List[KbTable] = field(default_factory=list)
    constants: List[KbConstant] = field(default_factory=list)
    signatures: List[KbSignature] = field(default_factory=list)
    # header-address -> symbol name, for naming variant/secondary tables the
    # main KB doesn't cover (baked from the A2L so no A2L is needed at runtime)
    a2l_names: Dict[int, str] = field(default_factory=dict)

    @property
    def by_struct_addr(self) -> Dict[int, KbTable]:
        return {t.struct_addr: t for t in self.tables
                if t.struct_addr is not None}


def load(family: str) -> Kb:
    path = _path(family + ".json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    kb = Kb(family=family, title=raw.get("title", ""))
    for r in raw["tables"]:
        kb.tables.append(KbTable(*r))
    for r in raw["constants"]:
        kb.constants.append(KbConstant(*r))
    kb.a2l_names = {int(k): v for k, v in raw.get("a2l_names", {}).items()}
    _merge_supplement(kb, family)
    return kb

_CACHE: Dict[str, tuple] = {}


def load_cached(family: str) -> Kb:
    """``load`` once per process (and per change to the data files).

    Parsing a family's JSON is ~0.1 s, and identifying a file scores every
    family, so opening several files -- or the second file of a comparison --
    paid for the same parse over and over. The returned Kb is SHARED: treat it
    as read-only (nothing in the loader or matcher edits one)."""
    stamp = tuple(
        os.path.getmtime(p) if os.path.exists(p) else 0
        for p in (_path(family + ".json.gz"),
                  _path(family + ".supplement.json")))
    hit = _CACHE.get(family)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    kb = load(family)
    _CACHE[family] = (stamp, kb)
    return kb


def _merge_supplement(kb: "Kb", family: str) -> None:
    """Merge hand-authored, RE-discovered (non-A2L) entities from
    data/<family>.supplement.json so they survive KB regeneration.
    Same array formats as the baked KB. Missing file -> no-op."""
    path = _path(family + ".supplement.json")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        sup = json.load(fh)
    have = {c.name for c in kb.constants}
    for r in sup.get("constants", []):
        c = KbConstant(*r)
        if c.name not in have:
            kb.constants.append(c); have.add(c.name)
    for r in sup.get("tables", []):
        kb.tables.append(KbTable(*r))
    for r in sup.get("signatures", []):
        kb.signatures.append(KbSignature(**r))
    for k, v in sup.get("a2l_names", {}).items():
        kb.a2l_names.setdefault(int(k), v)


def available() -> List[str]:
    from . import datafiles
    return [f for f in FAMILIES if datafiles.exists(f + ".json.gz")]


def exists(family: str) -> bool:
    """Whether a family's KB data file is present, whether or not it is in
    FAMILIES -- used for sub-families deliberately excluded from
    detect_family's automatic candidate scan (see session.py
    _DATASET_ID_SUBFAMILY) but still loadable by exact name."""
    from . import datafiles
    return datafiles.exists(family + ".json.gz")
