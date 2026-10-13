# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Audit a knowledge base (i.e. its source XDF) against a binary whose
layout it addresses.

Checks, per curve/map, that the storage structure exists at the declared
address with the declared dimensions. Finding classes:

  'oversized'  structure present, but stores FEWER points than declared.
               The A2L declares reserved (maximum) dimensions; the ECU
               reads the actual size from the count words. Not an XDF
               conversion error, but the XDF shows garbage rows/columns --
               the correction is to use the stored counts.
  'shifted'    structure found a few bytes from the declared address
               (off-by-N conversion defects).
  'dims'       structure present with LARGER or different dims -- suspect.
  'missing'    no structure found nearby.

Tables with non-16-bit axes are skipped (counted as ``unscannable``).
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .kb import Kb, KbTable
from .structure import try_curve, try_curve_f32, try_map

_NEAR = 8   # bytes to search either side for a shifted structure


@dataclass
class Finding:
    name: str
    kind: str          # 'missing' | 'shifted' | 'dims'
    declared: int
    suggestion: Optional[int] = None
    note: str = ""


@dataclass
class AuditResult:
    checked: int = 0
    ok: int = 0
    unscannable: int = 0
    findings: List[Finding] = field(default_factory=list)


def _grade(found_dims, declared_dims) -> Optional[str]:
    if found_dims == declared_dims or set(found_dims) == set(declared_dims):
        return "ok"
    if (found_dims[0] <= declared_dims[0] and found_dims[1] <= declared_dims[1]) \
            or (found_dims[0] <= declared_dims[1]
                and found_dims[1] <= declared_dims[0]):
        return "oversized"
    return "dims"


def _curve_at(data: bytes, pos: int, declared: int) -> Optional[str]:
    for probe in (try_curve, try_curve_f32):
        f = probe(data, pos)
        if f is not None:
            return _grade((1, f.cols), (1, declared))
    return None


def _struct_at(data: bytes, t: KbTable, pos: int) -> Optional[str]:
    """Classify what begins at ``pos`` relative to this table's declaration.

    Curves: ``pos`` is the count word. Maps are probed under BOTH verified
    layouts -- standard ``[Nx][Ny][X][Y][Z]`` at ``pos`` = struct header,
    and group-map counted axis blocks ``[Nx][X...][Ny][Y...]`` where
    ``pos`` = first count word. Returns 'ok' | 'oversized' | 'dims' | None.
    """
    if t.kind == "curve":
        declared = t.x_count if t.x_addr is not None else t.y_count
        return _curve_at(data, pos, declared)
    best = None
    f = try_map(data, pos)
    if f is not None:
        best = _grade((f.rows, f.cols), (t.y_count, t.x_count))
        if best == "ok":
            return best
    # group-map interpretation: two counted axis blocks back to back
    g1 = _curve_at(data, pos, t.x_count)
    if g1 in ("ok", "oversized"):
        c1 = try_curve(data, pos) or try_curve_f32(data, pos)
        g2 = _curve_at(data, c1.z_addr, t.y_count)
        if g2 is not None:
            both = "ok" if (g1 == g2 == "ok") else "oversized"
            best = both if best is None or both == "ok" else best
    return best


def audit(kb_obj: Kb, data: bytes) -> AuditResult:
    res = AuditResult()
    for t in kb_obj.tables:
        if t.kind == "indexed" or t.struct_addr is None:
            continue
        axes = [a for a in (t.x_addr, t.y_addr) if a is not None]
        if any(not (0 < a < len(data)) for a in axes):
            continue
        res.checked += 1
        # Structures may sit at ANY byte alignment; probes read big-endian
        # words at any offset and the +-N search covers odd shifts. An
        # unrecoverable oddity (e.g. CrCtl_facDevVDspl_CUR's byte-packed
        # storage) surfaces as 'missing' for manual review.
        sa = t.struct_addr
        got = _struct_at(data, t, sa)
        if got != "ok" and t.kind == "map":
            # group maps: the first counted axis block starts at first-2
            alt = min(a for a in axes) - 2
            got2 = _struct_at(data, t, alt)
            if got2 == "ok" or (got2 is not None and got is None):
                sa, got = alt, got2
        if got == "ok":
            res.ok += 1
            continue
        sug = None
        sug_kind = None
        for delta in sorted(range(-_NEAR, _NEAR + 1), key=abs):
            if not delta:
                continue
            g = _struct_at(data, t, sa + delta)
            if g in ("ok", "oversized"):
                sug, sug_kind = sa + delta, g
                break
        if sug is not None:
            res.findings.append(Finding(t.name, "shifted", sa, sug,
                                        "structure (%s) found %+d bytes away"
                                        % (sug_kind, sug - sa)))
        elif got in ("oversized", "dims"):
            res.findings.append(Finding(t.name, got, sa,
                                        note="declared %dx%d" % (
                                            t.y_count, t.x_count)))
        else:
            res.findings.append(Finding(t.name, "missing", sa))
    return res
