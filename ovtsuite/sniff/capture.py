# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Capture files: one frame per line, candump-log compatible::

    (1757160000.123456) can 200#01C0000310000101
    (1757160000.124001) can-tx 300#A00F8AFF4AFF
    (1757160010.500000) kline 8110F13E

``can`` / ``can-tx`` (our own transmissions, when the device echoes them) /
``kline`` (a framed K-line message) / ``kraw`` (unframed K-line bytes from a
passive serial tap; the burst lines of KWHack's ``kline_sniffer.py`` logs load
as these too); the id is hex (29-bit ids have 8 digits), the data
hex without separators.  Plain candump logs (``(t) can0 200#...``) load too.
"""
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

_LINE = re.compile(r"^\((\d+(?:\.\d+)?)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)\s*$")
# KWHack Tools/kline_sniffer.py burst lines: "[t=31428.695716]    5 bytes: 82 10 F1 10 89"
_KLINE_SNIFFER = re.compile(r"^\[t=(\d+(?:\.\d+)?)\]\s+\d+\s+bytes:\s*([0-9A-Fa-f ]+)\s*$")


@dataclass
class Record:
    t: float
    bus: str                       # "can" | "kline"
    can_id: int                    # 0 for K-line
    data: bytes
    tx: bool = False
    extended: bool = False
    kind: str = ""                 # framing result for derived K-line rows (not stored)
    note: str = ""

    @property
    def line(self) -> str:
        bus = self.bus + ("-tx" if self.tx else "")
        if self.bus == "can":
            ident = ("%08X" % self.can_id) if self.extended else ("%03X" % self.can_id)
        else:
            ident = "0"
        return "(%.6f) %s %s#%s" % (self.t, bus, ident, self.data.hex().upper())


def parse_line(line: str) -> Optional[Record]:
    m = _KLINE_SNIFFER.match(line.strip())
    if m:                                   # raw K-line bytes, unframed
        return Record(float(m.group(1)), "kraw", 0, bytes.fromhex(m.group(2).replace(" ", "")))
    m = _LINE.match(line.strip())
    if not m:
        return None
    t = float(m.group(1))
    bus = m.group(2).lower()
    tx = bus.endswith("-tx")
    if tx:
        bus = bus[:-3]
    ident_s, data_s = m.group(3), m.group(4)
    data = bytes.fromhex(data_s) if data_s else b""
    if bus == "kraw":
        return Record(t, "kraw", 0, data, tx)
    if bus.startswith("k"):
        return Record(t, "kline", 0, data, tx)
    ident = int(ident_s, 16)
    return Record(t, "can", ident, data, tx, extended=len(ident_s) > 3)


def read(path: str) -> List[Record]:
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            r = parse_line(line)
            if r is not None:
                out.append(r)
    return out


def write(path: str, records: Iterable[Record], header: str = ""):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        if header:
            for ln in header.splitlines():
                fh.write("# " + ln + "\n")
        for r in records:
            fh.write(r.line + "\n")


class Writer:
    """Streams records to a file as they arrive."""

    def __init__(self, path: str, header: str = ""):
        self.fh = open(path, "w", encoding="utf-8", newline="\n")
        if header:
            for ln in header.splitlines():
                self.fh.write("# " + ln + "\n")
        self.count = 0

    def add(self, r: Record):
        self.fh.write(r.line + "\n")
        self.count += 1
        if self.count % 50 == 0:
            self.fh.flush()

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass
