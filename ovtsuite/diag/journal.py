# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The write journal: every change OVT Diag makes to a vehicle, with the
value before and after, so any entry can be reverted and a scan diff can
explain a change by the entry that made it.

One JSON object per line in ``<config>/diag/garage/<vehicle-id>/journal.jsonl``
-- append-only, human-readable, greppable. P0 records fault clears (before =
the codes that were there); the P1/P2 writes (adaptation, coding, basic
settings) record their values through the same :meth:`Journal.record`.
"""
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from . import garage as _garage


@dataclass
class Entry:
    when: float
    addr: str                       # module address
    module: str                     # module name
    function: str                   # "clear_faults" | "adaptation" | "coding" | "basic_setting" | ...
    entry_id: str = ""              # the definitions entry id (adaptation.ch02), when one applies
    before: Any = None
    after: Any = None
    verified: Optional[bool] = None # re-read matched what was written
    note: str = ""
    reverted: bool = False
    seq: int = 0                    # position in the file, for revert bookkeeping

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Entry":
        return cls(when=float(d.get("when", 0)), addr=str(d.get("addr", "")),
                   module=str(d.get("module", "")), function=str(d.get("function", "")),
                   entry_id=str(d.get("entry_id", "")), before=d.get("before"), after=d.get("after"),
                   verified=d.get("verified"), note=str(d.get("note", "")),
                   reverted=bool(d.get("reverted", False)), seq=int(d.get("seq", 0)))

    @property
    def summary(self) -> str:
        what = self.entry_id or self.function
        if self.function == "clear_faults":
            n = len(self.before or [])
            return "%s %s: cleared %d fault%s" % (self.addr, self.module, n, "" if n == 1 else "s")
        return "%s %s: %s %s -> %s" % (self.addr, self.module, what, _fmt(self.before), _fmt(self.after))


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


class Journal:
    def __init__(self, vehicle_id: str, path: Optional[str] = None):
        self.vehicle_id = vehicle_id
        self.path = path or os.path.join(_garage.root(), vehicle_id, "journal.jsonl")

    def entries(self) -> List[Entry]:
        out = []
        if not os.path.isfile(self.path):
            return out
        with open(self.path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = Entry.from_dict(json.loads(line))
                except ValueError:
                    continue
                e.seq = i
                out.append(e)
        return out

    def record(self, addr: str, module: str, function: str, before: Any = None, after: Any = None,
               entry_id: str = "", verified: Optional[bool] = None, note: str = "") -> Entry:
        e = Entry(when=time.time(), addr=addr, module=module, function=function, entry_id=entry_id,
                  before=before, after=after, verified=verified, note=note)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        return e

    def mark_reverted(self, seq: int) -> None:
        """Rewrite the file with entry ``seq`` flagged reverted (the revert
        itself is a new entry recorded by the caller)."""
        entries = self.entries()
        for e in entries:
            if e.seq == seq:
                e.reverted = True
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def since(self, when: float) -> List[Entry]:
        return [e for e in self.entries() if e.when >= when]

    def for_module(self, addr: str) -> List[Entry]:
        return [e for e in self.entries() if e.addr == addr]
