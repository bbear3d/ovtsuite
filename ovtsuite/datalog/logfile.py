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
    if _BITS_RE.match((text or "").strip()):
        # "00101011" or "01001001 00001000": a switch cell's bytes in binary, not the
        # number 101011, nor 1001001 with a unit "00001000" (which also became that
        # channel's unit, and a number in the VCDS export)
        return None, ""
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
#: a switch cell: one or more bytes written in binary, eight digits each ("00101011",
#: "01001001 00001000" -- how VCDS and this logger show them)
_BITS_RE = re.compile(r"^[01]{8}(?: [01]{8})*$")


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
_GROUP_TAG_RE = re.compile(r"\s*\(G\d+/\d+\)\s*$")


def _vcds_title(c: Channel) -> str:
    """The field's name as VCDS writes it: plain. The logger tags a name that more
    than one cell carries with ``(G1/1)``; in the VCDS layout the marker row above
    already says which group and field each column is."""
    return _GROUP_TAG_RE.sub("", c.title) or c.title


def _vcds_cell(v, t: str) -> str:
    """One data cell: the number as the logger printed it (``0.0`` stays ``0.0``),
    without its unit, which has a row of its own. A cell that is not a number -- a
    switch's bit groups, ``N/A``, text -- is written as it is, never as a number
    made of it."""
    num, _unit = split_value_unit(t)
    if num is None:
        return t if t else (("%g" % v) if v is not None else "")
    m = _VALUE_RE.match(t or "")
    out = m.group(1) if m and not _RAW_CELL_RE.match(t) else "%g" % num
    if "ATDC" in (t or "").upper() and num != 0 and not out.startswith("-"):
        # An angle cell says "x °BTDC" or "x °ATDC" with the size only; one column
        # has to say which side, so after TDC is negative (the unit row says °BTDC).
        out = "-" + out
    return out


def _vcds_unit(c: Channel) -> str:
    return "°BTDC" if c.unit == "°ATDC" else c.unit


def write_vcds(path: str, log: DataLog, tool_version: str = _VERSION):
    started = log.stamps[0] if log.stamps else datetime.now()
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\r\n")       # as VCDS writes it
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
            h2 += ["STAMP", _vcds_title(c)]
        w.writerow(h2 + [""])
        units = [""]
        for c in log.channels:
            units += ["", _vcds_unit(c)]
        w.writerow(units + [""])
        for st, vals, tx in zip(log.stamps, log.values, log.texts):
            rel = "%.2f" % (st - started).total_seconds()
            row = [""]
            for v, t in zip(vals, tx):
                row += [rel, _vcds_cell(v, t)]
            w.writerow(row + [""])


# --------------------------------------------------- VCDS classic layout
#: The classic VCDS measuring-block layout (what VCDS wrote for KW1281 groups, and what
#: simple log viewers read): at most three blocks -- "Group A:", "Group B:", "Group C:" --
#: of one time column and four fields each, side by side.
CLASSIC_BLOCKS = 3
CLASSIC_FIELDS = 4
_CLASSIC_UNITS = {"rpm": "/min"}


def classic_blocks(log: DataLog) -> List[tuple]:
    """``(group number, [channel index, ...])`` per classic block: a group's cells in
    field order, four to a block (a group of more starts another block of the same
    number)."""
    order, by = [], {}
    for i, c in enumerate(log.channels):
        if c.group not in by:
            order.append(c.group)
        by.setdefault(c.group, []).append(i)
    out = []
    for g in order:
        idx = sorted(by[g], key=lambda i: (log.channels[i].field, i))
        for k in range(0, len(idx), CLASSIC_FIELDS):
            out.append((g, idx[k:k + CLASSIC_FIELDS]))
    return out


def classic_chunks(log: DataLog) -> List[List[tuple]]:
    """The log's blocks in lots of three: what one classic log holds."""
    b = classic_blocks(log)
    return [b[k:k + CLASSIC_BLOCKS] for k in range(0, len(b), CLASSIC_BLOCKS)] or [[]]


def _classic_cell(v, t: str) -> str:
    """One data cell of a classic log: a number, ``N/A``, empty, or a switch's bytes in
    binary (``01001001 00001000``), which are written as they are, as VCDS does. Any other
    text has no number and is written as ``N/A``."""
    num, _unit = split_value_unit(t)
    if num is not None:
        return _vcds_cell(v, t)
    if _BITS_RE.match((t or "").strip()):
        return t.strip()
    if not (t or "").strip():
        return ("%g" % v) if v is not None else ""
    return "N/A"


def _write_classic_log(w, log: DataLog, blocks: Sequence[tuple], ident: str):
    started = log.stamps[0] if log.stamps else datetime.now()
    w.writerow([started.strftime("%A"), started.strftime("%d"), started.strftime("%B"),
                started.strftime("%Y"), started.strftime("%H:%M:%S")])
    # The identity is "part  description" (two spaces) when the log knows the
    # part number; a part number can itself hold single spaces, so the split
    # is at the double space. The description's cell then starts with one
    # space, which is how a reader tells the double space from a single one
    # (VCDS's own files join the two cells with one space). An identity
    # without a double space splits at its first space, as before, and comes
    # back with a single space.
    ident = (ident or "").strip()
    if "  " in ident:
        part, rest = ident.split("  ", 1)
        rest = " " + rest.strip()
    else:
        part, _sp, rest = ident.partition(" ")
        rest = rest.strip()
    w.writerow([part.strip(), "", rest, ""])
    w.writerow([])
    r4, r5, r6, r7 = [""], [""], [""], ["Marker"]
    for k in range(CLASSIC_BLOCKS):
        last = k == CLASSIC_BLOCKS - 1
        if k < len(blocks):
            g, idx = blocks[k]
            r4 += ["Group %s:" % "ABC"[k], "'%03d" % g]
            cells = list(idx) + [None] * (CLASSIC_FIELDS - len(idx))
            r5 += [""] + [_vcds_title(log.channels[i]) if i is not None else "" for i in cells]
            r7 += ["STAMP"] + [_classic_unit(log.channels[i]) if i is not None else ""
                               for i in cells]
        else:
            r4 += ["Group %s:" % "ABC"[k], " Not Running"]
            r5 += [""] * (1 + CLASSIC_FIELDS)
            r7 += ["STAMP"] + [""] * CLASSIC_FIELDS
        r4 += [] if last else [""] * (CLASSIC_FIELDS - 1)
        r6 += ["TIME"] + [""] * CLASSIC_FIELDS
    for r in (r4, r5, r6, r7):
        w.writerow(r)
    for st, vals, tx in zip(log.stamps, log.values, log.texts):
        rel = "%.2f" % (st - started).total_seconds()
        row = [""]
        for k in range(CLASSIC_BLOCKS):
            if k < len(blocks):
                row.append(rel)
                cells = list(blocks[k][1]) + [None] * (CLASSIC_FIELDS - len(blocks[k][1]))
                row += [_classic_cell(vals[i], tx[i]) if i is not None else ""
                        for i in cells]
            else:
                row += [""] * (1 + CLASSIC_FIELDS)
        w.writerow(row)


def _classic_unit(c: Channel) -> str:
    u = _vcds_unit(c)
    u = _CLASSIC_UNITS.get(u, u)
    return u if (not u or u.startswith("°")) else " " + u


def write_vcds_classic(path: str, log: DataLog,
                       logs: Optional[Sequence[Sequence[tuple]]] = None,
                       ident: Optional[str] = None):
    """The log in the classic VCDS layout. ``logs`` is the blocks of each log in the
    file (default: every block of ``log``, three to a log, one log after another: a
    reader that takes one log at a time asks which); one log, or the first few blocks, is
    ``[blocks]``. ANSI text, CRLF line ends -- as VCDS writes them."""
    ident = log.identity if ident is None else ident
    with open(path, "w", encoding="cp1252", errors="replace", newline="") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        for blocks in (logs if logs is not None else classic_chunks(log)):
            _write_classic_log(w, log, blocks, ident)


def _read_text(path: str) -> str:
    """A log file's text: UTF-8 as this app writes it, else Windows ANSI as VCDS does
    (a degree sign is one byte there)."""
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


#: what the classic layout calls "Group A:" (English, German "Block"/"Gruppe", French
#: "Groupe", Spanish/Italian "Grupo"/"Gruppo", Dutch "Groep")
_GROUP_WORDS = ("GROUP", "BLOCK", "GRUP", "GROEP")


def _is_classic(rows) -> bool:
    for i, r in enumerate(rows):
        if r and r[0].strip().upper().startswith(("MARKER", "MARKIERUNG")):
            g = rows[i - 3] if i >= 3 else []
            return len(g) > 1 and any(w in g[1].upper() for w in _GROUP_WORDS) and \
                len(r) > 1 and r[1].strip().upper() in ("STAMP", "MARKE")
    return False


#: Month names by their first three letters, in the languages VCDS writes its date line
#: in (accents folded: "März" -> "mar"). A few full names that share three letters.
_MONTH3 = {"jan": 1, "ene": 1, "gen": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4,
           "abr": 4, "avr": 4, "mai": 5, "may": 5, "mag": 5, "mei": 5, "jun": 6,
           "giu": 6, "jul": 7, "lug": 7, "aug": 8, "ago": 8, "aou": 8, "sep": 9,
           "set": 9, "oct": 10, "okt": 10, "ott": 10, "nov": 11, "dec": 12,
           "dez": 12, "dic": 12}
_MONTH_FULL = {"juin": 6, "juillet": 7, "juil": 7}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})")


def _month_number(name: str) -> Optional[int]:
    import unicodedata
    n = "".join(ch for ch in unicodedata.normalize("NFKD", (name or "").strip().lower())
                if not unicodedata.combining(ch))
    return _MONTH_FULL.get(n) or _MONTH3.get(n[:3])


def vcds_started(row) -> Optional[datetime]:
    """The start time of a VCDS file's first line, ``weekday, day, month, year, time``:
    in any of VCDS's languages ("Mittwoch,13,Mai,2020") and with whatever follows the
    seconds ("10:53:42:16365-VCID:..." in recent versions) left out. None when the line
    is not a date."""
    if len(row) < 5:
        return None
    try:
        day, year = int(row[1].strip()), int(row[3].strip())
    except ValueError:
        return None
    month = _month_number(row[2])
    t = _TIME_RE.match(row[4].strip())
    if not month or not t:
        return None
    try:
        return datetime(year, month, day, int(t.group(1)), int(t.group(2)), int(t.group(3)))
    except ValueError:
        return None


def _classic_started(row) -> Optional[datetime]:
    return vcds_started(row)


def _field_name(first: str, second: str) -> str:
    """A field's name from the two header lines it can run over (German VCDS wraps a long
    name: "Lambdasonden-" over "spannung")."""
    a, b = (first or "").strip(), (second or "").strip()
    if not b:
        return a
    if not a:
        return b
    return a + b if a.endswith("-") else a + " " + b


def _read_classic_one(rows, m: int, end: int, path: str) -> DataLog:
    """The classic log whose Marker (units) row is ``rows[m]``; its data run to ``end``."""
    def cell(r, i):
        return r[i].strip() if i < len(r) else ""
    grow, names, names2, units = rows[m - 3], rows[m - 2], rows[m - 1], rows[m]
    channels, cols = [], []                       # cols: (column of the field, block's time column)
    for j in range(CLASSIC_BLOCKS):
        gid = cell(grow, 5 * j + 2).lstrip("'")
        if not gid.isdigit():
            continue
        for k in range(CLASSIC_FIELDS):
            col = 5 * j + 2 + k
            name, unit = _field_name(cell(names, col), cell(names2, col)), cell(units, col)
            if name or unit or any(cell(r, col) for r in rows[m + 1:end]):
                channels.append(Channel(int(gid), k, name or "Group %s / Cell %d" % (gid, k + 1),
                                        unit))
                cols.append((col, 5 * j + 1))
    started = _classic_started(rows[m - 6]) if m >= 6 else None
    ident = ""
    if m >= 5:
        raw = rows[m - 5][2] if len(rows[m - 5]) > 2 else ""
        # a description cell that starts with a space was written after a
        # double space (see _write_classic_log): keep it
        sep = "  " if raw[:1].isspace() else " "
        ident = sep.join(x for x in (cell(rows[m - 5], 0), cell(rows[m - 5], 2)) if x)
    log = DataLog(channels=channels, identity=ident, path=path, layout="vcds")
    started = started or datetime.now().replace(microsecond=0)
    for r in rows[m + 1:end]:
        if len(r) < 3 or not cols:
            continue
        try:
            rel = float(cell(r, cols[0][1]))
        except ValueError:
            continue
        texts = []
        for c, (col, _t) in zip(channels, cols):
            t = cell(r, col)
            plain = bool(t) and split_value_unit(t)[0] is not None
            texts.append((t + (" " + c.unit if c.unit and plain else "")) if t else "")
        log.append(started + timedelta(seconds=rel), texts)
    return log


def _read_vcds_classic(rows, path: str) -> DataLog:
    marks = [i for i, r in enumerate(rows)
             if r and r[0].strip().upper().startswith(("MARKER", "MARKIERUNG"))]
    logs = []
    for n, m in enumerate(marks):
        # a log ends where the next one's first line (a date: a non-empty first cell) begins
        end = marks[n + 1] - 6 if n + 1 < len(marks) else len(rows)
        logs.append(_read_classic_one(rows, m, max(end, m + 1), path))
    first = logs[0]
    if len(logs) > 1 and all(l.stamps == first.stamps and l.stamps for l in logs):
        # logs of one recording, written one after another because a log holds three
        # blocks (this app's export does): their cells side by side again
        for l in logs[1:]:
            first.channels += l.channels
            first.values = [a + b for a, b in zip(first.values, l.values)]
            first.texts = [a + b for a, b in zip(first.texts, l.texts)]
    return first


def read_vcds(path: str) -> DataLog:
    rows = list(csv.reader(io.StringIO(_read_text(path), newline="")))
    if _is_classic(rows):
        return _read_vcds_classic(rows, path)
    # locate the Marker header row
    hi = next((i for i, r in enumerate(rows)
               if r and r[0].strip().lower() in ("marker", "markierung")), None)
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
        if h1[col].strip().upper() not in ("TIME", "ZEIT"):
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
    started = vcds_started(rows[0] if rows else [])
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
            # (a cell that is not a number has no unit to add: "N/A", a bit pattern)
            plain = bool(t) and split_value_unit(t)[0] is not None
            texts.append((t + (" " + c.unit if c.unit and plain else "")) if t else "")
        if rel is None:
            continue
        log.append(started + timedelta(seconds=rel), texts)
    return log


def layout_of(head: str) -> str:
    """``"kwhack"`` or ``"vcds"`` from the first few KB of a log's text."""
    if head.startswith(_MARKER) or head.lower().startswith("timestamp,"):
        return "kwhack"
    if re.search(r"(^|\n)(Marker|Markierung),", head):
        return "vcds"
    if "ADVMB" in head or re.search(r"\bG\d{3},F\d\b", head):
        return "vcds"
    return "kwhack"


def sniff(data: bytes) -> str:
    """The layout of a log held as bytes (a project's kept log): ``"vcds"``, ``"kwhack"``,
    or ``""`` when it does not look like either (the file is no data log)."""
    head = bytes(data[:4096]).decode("utf-8-sig", errors="replace")
    lay = layout_of(head)
    if lay == "vcds" or head.startswith(_MARKER) or head.lower().startswith("timestamp,"):
        return lay
    return ""


def load(path: str) -> DataLog:
    """Detect the layout and read it."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = fh.read(4096)
    if layout_of(head) == "vcds":
        return read_vcds(path)
    return read_kwhack(path)


def default_name(identity: str = "", when: Optional[datetime] = None, ext=".csv") -> str:
    when = when or datetime.now()
    pn = (identity.split("  ")[0].strip().replace(" ", "") if identity else "log") or "log"
    return "%s_%s%s" % (pn, when.strftime("%Y%m%d_%H%M%S"), ext)
