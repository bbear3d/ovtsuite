# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data-log model and the two CSV layouts.

Native layout is KWHack's, so logs move between the two tools unchanged::

    # Cells: 1.1,1.2,11.1
    Timestamp,Engine speed (Eng_nAvrg),Injected quantity (InjCtl_qSetUnBal),...
    2026-09-06 14:03:11.250,812 rpm,3.4 mg/stroke,...

The marker line lists the logged cells as ``group.field`` (field 1-based), the
header carries the display labels and every row is a wall-clock stamp plus
the cells' display strings (value and unit as the ECU's formula gives them).

The VCDS layout (multi-row header, a Gxxx/Fy marker row, per-cell TIME/STAMP
columns with relative seconds, units on their own row, trailing commas) is
read and written too, so logs open in existing VCDS-CSV analysers and old
VCDS logs load here.

A :class:`Channel` is one logged cell: ``group`` + ``field`` (0-based
internally, shown 1-based) with a label, a unit and -- when the loaded binary
resolved it -- the A2L signal it carries.
"""
import csv
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from .. import __version__ as _VERSION
from .. import branding

_VALUE_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(.*?)\s*$")
_MARKER = "# Cells: "


@dataclass
class Channel:
    group: int
    field: int                          # 0-based
    label: str = ""
    unit: str = ""
    signal: str = ""                    # A2L measurement name when known
    # Set only for channels read by address rather than from a measuring block
    # (see datalog.memchan): the AST path has an ECU_ADDRESS for every signal
    # and a measuring descriptor for none, so no block can ever carry them.
    addr: Optional[int] = None
    width: int = 0
    signed: bool = False
    scale: float = 1.0
    #: Set only for a channel the logger works out from other channels of the
    #: same row instead of reading it (see datalog.derived): "eoi" = end of
    #: injection, duration - start of injection, deg ATDC.
    derived: str = ""

    @property
    def is_memory(self) -> bool:
        return self.addr is not None

    @property
    def is_derived(self) -> bool:
        return bool(self.derived)

    @property
    def key(self):
        # Memory channels have no group/field, so keying them that way would
        # collide with block cell (0, 0) and with each other.
        if self.derived:
            return ("=", self.derived)
        return ("@", self.addr) if self.addr is not None else (self.group, self.field)

    @property
    def title(self):
        if self.label:
            return self.label
        if self.addr is not None:
            return "%s (0x%06X)" % (self.signal or "memory", self.addr)
        return "Group %d / Cell %d" % (self.group, self.field + 1)


@dataclass
class DataLog:
    channels: List[Channel] = field(default_factory=list)
    stamps: List[datetime] = field(default_factory=list)
    values: List[List[Optional[float]]] = field(default_factory=list)   # rows x channels
    texts: List[List[str]] = field(default_factory=list)                # display strings
    identity: str = ""
    path: str = ""
    layout: str = "kwhack"              # or "vcds"

    @property
    def rows(self) -> int:
        return len(self.stamps)

    def column(self, i: int) -> List[Optional[float]]:
        return [r[i] for r in self.values]

    def seconds(self) -> List[float]:
        if not self.stamps:
            return []
        t0 = self.stamps[0]
        return [(t - t0).total_seconds() for t in self.stamps]

    def append(self, stamp: datetime, texts: Sequence[str]):
        vals = []
        for i, t in enumerate(texts):
            v, u = split_value_unit(t)
            vals.append(v)
            if u and i < len(self.channels) and not self.channels[i].unit:
                self.channels[i].unit = u
        self.stamps.append(stamp)
        self.texts.append(list(texts))
        self.values.append(vals)

    def find(self, group: int, field_: int) -> Optional[int]:
        for i, c in enumerate(self.channels):
            if c.group == group and c.field == field_:
                return i
        return None


#: a cell the logger could not decode is written as its raw measuring-block
#: triplet, "(fid a b)" -- see diag.decode_cell's fallback.
_RAW_CELL_RE = re.compile(r"^\s*\((\d+)\s+(\d+)\s+(\d+)\)\s*$")


def split_value_unit(text: str):
    """'812 rpm' -> (812.0, 'rpm'); non-numeric display -> (None, '').

    A raw "(fid a b)" triplet is decoded here rather than discarded. The logger
    writes one whenever :func:`~ovtsuite.flash.diag.decode_cell` had no formula
    for that cell, so a log recorded before a formula was implemented would
    otherwise stay unreadable for ever -- the numbers are all there, only the
    interpretation was missing. Re-decoding on read means implementing a formula
    retroactively recovers every log already taken. (Found with the GS19's
    group 9 engine-torque cell, formula 177, logged as "(177 128 12)" for a
    whole 460 s drive.)
    """
    m = _RAW_CELL_RE.match(text or "")
    if m:
        from ..flash.diag import decode_cell
        v, u, _txt = decode_cell(*(int(x) for x in m.groups()))
        return (float(v) if v is not None else None), u
    q = (text or "").strip()
    if len(q) >= 2 and q[0] == q[-1] == '"':
        # Formula-17 text as older versions wrote it, quotes and padding
        # included: the 6HP19's engaged gear was logged as '"3 "'.
        text = q[1:-1]
    m = _VALUE_RE.match(text or "")
    if not m:
        return None, ""
    try:
        v = float(m.group(1))
    except ValueError:
        return None, ""
    # Logs written before the formula table was corrected say "mg/h" for a
    # quantity per stroke (VAG's "mg/H", H = Hub); no measuring-block formula
    # gives milligrams per hour.
    return v, _LEGACY_UNITS.get(m.group(2), m.group(2))


_LEGACY_UNITS = {"mg/h": "mg/stroke"}


# ------------------------------------------------------------ KWHack layout
def _marker_part(c: Channel) -> str:
    if c.derived:
        # =<name>: worked out from the row's other cells, nothing read
        return "=" + c.derived
    if c.addr is not None:
        # @<addr>:<width><s|u> -- distinguishable from "g.f" by the @, so an
        # older reader does not silently take it for a block cell.
        return "@%06X:%d%s" % (c.addr, c.width, "s" if c.signed else "u")
    return "%d.%d" % (c.group, c.field + 1)


def marker_line(channels: Sequence[Channel]) -> str:
    return _MARKER + ",".join(_marker_part(c) for c in channels)


def parse_marker_line(line: str) -> Optional[List[Channel]]:
    if not line or not line.startswith(_MARKER):
        return None
    rest = line[len(_MARKER):].strip()
    out = []
    if not rest:
        return out
    for part in rest.split(","):
        part = part.strip()
        if part.startswith("="):
            out.append(Channel(0, 0, derived=part[1:]))
            continue
        if part.startswith("@"):
            spec, _, wid = part[1:].partition(":")
            try:
                addr = int(spec, 16)
                width = int(wid.rstrip("su") or 1)
            except ValueError:
                return None
            out.append(Channel(0, 0, addr=addr, width=width,
                               signed=wid.endswith("s")))
            continue
        g, _, f = part.partition(".")
        try:
            gi, fi = int(g), int(f)
        except ValueError:
            return None
        if fi < 1:
            return None
        out.append(Channel(gi, fi - 1))
    return out


class KwhackWriter:
    """Streams the native layout: marker + header once, then a row per poll."""

    def __init__(self, fh, channels: Sequence[Channel]):
        self.fh = fh
        self.channels = list(channels)
        self._w = csv.writer(fh, lineterminator="\n")
        fh.write(marker_line(self.channels) + "\n")
        self._w.writerow(["Timestamp"] + [c.title for c in self.channels])
        fh.flush()

    def row(self, stamp: datetime, texts: Sequence[str]):
        self._w.writerow([stamp.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (stamp.microsecond // 1000)]
                         + list(texts))
        self.fh.flush()


def write_kwhack(path: str, log: DataLog):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = KwhackWriter(fh, log.channels)
        for st, tx in zip(log.stamps, log.texts):
            w.row(st, tx)


def _parse_stamp(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


def read_kwhack(path: str) -> DataLog:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        first = fh.readline().rstrip("\r\n")
        channels = parse_marker_line(first)
        rdr = csv.reader(fh)
        if channels is None:
            header = [first] and next(csv.reader(io.StringIO(first)))
        else:
            header = next(rdr, [])
        if not header or header[0].strip().lower() != "timestamp":
            raise ValueError("not a KWHack data log (no Timestamp header)")
        labels = header[1:]
        if channels is None:
            channels = [Channel(0, i, lbl) for i, lbl in enumerate(labels)]
        for i, lbl in enumerate(labels):
            if i < len(channels):
                channels[i].label = lbl
        log = DataLog(channels=channels, path=path, layout="kwhack")
        for row in rdr:
            if not row or not row[0].strip():
                continue
            st = _parse_stamp(row[0])
            if st is None:
                continue
            texts = row[1:1 + len(channels)]
            texts += [""] * (len(channels) - len(texts))
            log.append(st, texts)
    return log


# -------------------------------------------------------------- VCDS layout
def write_vcds(path: str, log: DataLog, tool_version: str = _VERSION):
    started = log.stamps[0] if log.stamps else datetime.now()
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow([started.strftime("%A"), started.strftime("%d"), started.strftime("%B"),
                    started.strftime("%Y"), started.strftime("%H:%M:%S"),
                    "%s Version: %s" % (branding.APP_NAME, tool_version), "Data version: %s" % tool_version])
        ident = log.identity or ""
        pn = ident.split("  ")[0].strip() if ident else ""
        w.writerow([pn, "ADVMB", ident, ""])
        markers = ["", ""]
        for c in log.channels:
            markers += ["G%03d" % c.group, "F%d" % c.field]
        w.writerow(markers + [""])
        w.writerow([])
        h1 = ["Marker"]
        for c in log.channels:
            h1 += ["TIME", "Group %d - Field %d" % (c.group, c.field)]
        w.writerow(h1 + [""])
        h2 = [""]
        for c in log.channels:
            h2 += ["STAMP", c.title]
        w.writerow(h2 + [""])
        units = [""]
        for c in log.channels:
            units += ["", c.unit]
        w.writerow(units + [""])
        for st, vals, tx in zip(log.stamps, log.values, log.texts):
            rel = "%.2f" % (st - started).total_seconds()
            row = [""]
            for v, t in zip(vals, tx):
                row += [rel, ("%g" % v) if v is not None else split_value_unit(t)[0] or t]
            w.writerow(row + [""])


def read_vcds(path: str) -> DataLog:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    # locate the Marker header row
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Marker"), None)
    if hi is None:
        raise ValueError("not a VCDS data log (no Marker header row)")
    h1 = rows[hi]
    h2 = rows[hi + 1] if hi + 1 < len(rows) else []
    units = rows[hi + 2] if hi + 2 < len(rows) else []
    # the Gxxx/Fy marker row (if present) gives exact group/field identities
    gf = []
    for r in rows[:hi]:
        if len(r) > 2 and re.match(r"^G\d{3,}$", r[2].strip() or ""):
            gf = [(int(r[i][1:]), int(r[i + 1][1:])) for i in range(2, len(r) - 1, 2)
                  if re.match(r"^G\d{3,}$", r[i].strip() or "") and re.match(r"^F\d+$", r[i + 1].strip() or "")]
            break
    channels = []
    col = 1
    k = 0
    while col + 1 < len(h1):
        name = h1[col + 1].strip()
        if h1[col].strip() != "TIME":
            break
        m = re.match(r"Group (\d+) - Field (\d+)", name)
        if k < len(gf):
            g, f = gf[k]
        elif m:
            g, f = int(m.group(1)), int(m.group(2))
        else:
            g, f = 0, k
        label = h2[col + 1].strip() if col + 1 < len(h2) else name
        unit = units[col + 1].strip() if col + 1 < len(units) else ""
        channels.append(Channel(g, f, label or name, unit))
        col += 2
        k += 1
    started = None
    top = rows[0] if rows else []
    if len(top) >= 5:
        try:
            started = datetime.strptime(" ".join(top[1:5]), "%d %B %Y %H:%M:%S")
        except ValueError:
            started = None
    if started is None:
        started = datetime.now().replace(microsecond=0)
    ident = rows[1][2].strip() if len(rows) > 1 and len(rows[1]) > 2 else ""
    log = DataLog(channels=channels, identity=ident, path=path, layout="vcds")
    start_row = hi + 3
    for r in rows[start_row:]:
        if len(r) < 3:
            continue
        rel = None
        texts = []
        for i, c in enumerate(channels):
            ci = 1 + 2 * i
            if ci + 1 >= len(r):
                texts.append("")
                continue
            try:
                rel = float(r[ci]) if rel is None else rel
            except ValueError:
                pass
            t = r[ci + 1].strip()
            texts.append((t + (" " + c.unit if c.unit and t else "")) if t else "")
        if rel is None:
            continue
        log.append(started + timedelta(seconds=rel), texts)
    return log


def load(path: str) -> DataLog:
    """Detect the layout and read it."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = fh.read(4096)
    if head.startswith(_MARKER) or head.lower().startswith("timestamp,"):
        return read_kwhack(path)
    if "\nMarker," in head or head.startswith("Marker,"):
        return read_vcds(path)
    if "ADVMB" in head or re.search(r"\bG\d{3},F\d\b", head):
        return read_vcds(path)
    return read_kwhack(path)


def default_name(identity: str = "", when: Optional[datetime] = None, ext=".csv") -> str:
    when = when or datetime.now()
    pn = (identity.split("  ")[0].strip().replace(" ", "") if identity else "log") or "log"
    return "%s_%s%s" % (pn, when.strftime("%Y%m%d_%H%M%S"), ext)
