# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A definition as a CSV map list: one row per table, for spreadsheets.

Columns are fixed (the header row names them, and reading checks it).
Addresses are written in hex (``0x1C8258``). A list read back gives the
same tables; text-table labels and per-column conversions are not in it.
"""

from __future__ import annotations

import csv
import io
from typing import List, Optional

from ..definitions.model import AxisDef, Definition, DefinitionError, Storage, TableDef

COLUMNS = ["name", "symbol", "kind", "address", "rows", "cols", "bits",
           "signed", "float", "endian", "order", "equation", "units",
           "x_address", "x_count", "x_bits", "x_signed", "x_float",
           "x_equation", "x_units", "x_points",
           "y_address", "y_count", "y_bits", "y_signed", "y_float",
           "y_equation", "y_units", "y_points",
           "categories", "description"]


def is_csv(data: bytes) -> bool:
    head = bytes(data[:256]).lstrip(b"\xef\xbb\xbf").decode("utf-8", "replace")
    return head.startswith(",".join(COLUMNS[:4]))


def _hex(v) -> str:
    return "" if v is None else "0x%X" % v


def _b(v) -> str:
    return "" if v is None else ("1" if v else "0")


def _axis_cells(ad: Optional[AxisDef]) -> List[str]:
    if ad is None:
        return [""] * 8
    st = ad.values or Storage(addr=None)
    pts = ""
    if ad.fixed:
        pts = " ".join("%g" % v for v in ad.fixed)
    elif ad.labels and st.addr is None:
        pts = " | ".join(ad.labels)
    return [_hex(st.addr), str(ad.count), str(st.bits) if st.addr is not None else "",
            _b(st.signed) if st.addr is not None else "",
            _b(st.float) if st.addr is not None else "",
            st.equation_text() if st.addr is not None else "", st.units or "", pts]


def dumps(defn: Definition) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for t in defn.tables:
        z = t.z
        w.writerow([t.name, t.symbol, t.kind, _hex(z.addr), t.rows, t.cols,
                    z.bits, _b(z.signed), _b(z.float), z.endian, t.order,
                    z.equation_text(), z.units]
                   + _axis_cells(t.x) + _axis_cells(t.y)
                   + [" / ".join(t.categories), t.description])
    return buf.getvalue()


def _int(s, default=None):
    s = (s or "").strip()
    if not s:
        return default
    return int(s, 0)


def _bool(s):
    s = (s or "").strip()
    return None if s == "" else s == "1"


def _axis(row, p) -> Optional[AxisDef]:
    count = _int(row[p + "count"])
    if not count:
        return None
    addr = _int(row[p + "address"])
    pts = (row[p + "points"] or "").strip()
    if addr is not None:
        st = Storage(addr=addr, bits=_int(row[p + "bits"], 16),
                     signed=_bool(row[p + "signed"]),
                     float=bool(_bool(row[p + "float"])),
                     equation=row[p + "equation"] or "X", units=row[p + "units"])
        return AxisDef(count, values=st)
    st = Storage(addr=None, units=row[p + "units"])
    if " | " in pts or (pts and not _numeric(pts)):
        return AxisDef(count, values=st, labels=[s.strip() for s in pts.split(" | ")])
    if pts:
        return AxisDef(count, values=st, fixed=[float(v) for v in pts.split()])
    return None


def _numeric(pts: str) -> bool:
    try:
        [float(v) for v in pts.split()]
        return True
    except ValueError:
        return False


def loads(data: bytes) -> Definition:
    text = bytes(data).decode("utf-8-sig", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not is_csv(data):
        raise DefinitionError("not an OVT map list (the header row differs)")
    defn = Definition(type="csv", title="")
    for n, r in enumerate(rows, start=2):
        try:
            z = Storage(addr=_int(r["address"]), bits=_int(r["bits"], 8),
                        signed=bool(_bool(r["signed"])),
                        float=bool(_bool(r["float"])), endian=r["endian"] or "",
                        equation=r["equation"] or "X", units=r["units"] or "")
            defn.tables.append(TableDef(
                name=r["name"], symbol=r["symbol"] or "", kind=r["kind"] or "constant",
                z=z, rows=_int(r["rows"], 1), cols=_int(r["cols"], 1),
                order=r["order"] or "row", x=_axis(r, "x_"), y=_axis(r, "y_"),
                categories=[c for c in (r["categories"] or "").split(" / ") if c],
                description=r["description"] or ""))
        except (KeyError, ValueError) as exc:
            raise DefinitionError("row %d: %s" % (n, exc))
    return defn
