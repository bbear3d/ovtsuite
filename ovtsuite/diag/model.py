# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What OVT Diag talks about: a vehicle, its modules, their faults, a scan.

Plain data with ``to_dict``/``from_dict`` so the garage can store it as JSON
and the GUI, the report and the tests can build it without a car.
"""
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .defs.model import Identity, _norm_addr


@dataclass
class Vehicle:
    id: str                              # garage folder name: a slug the user never sees
    name: str = ""                       # "Audi A6 (4F) 2006"
    vin: str = ""
    generation: str = ""                 # addressing.generations key, or ""
    notes: str = ""
    created: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Vehicle":
        return cls(id=str(d.get("id", "")), name=str(d.get("name", "")), vin=str(d.get("vin", "")),
                   generation=str(d.get("generation", "")), notes=str(d.get("notes", "")),
                   created=float(d.get("created", 0) or 0))

    @property
    def label(self) -> str:
        return self.name or self.vin or self.id


@dataclass
class Fault:
    """One stored fault as the module reported it."""
    code: int                            # the reported word
    status: int = 0
    proto: str = "kwp2000"               # kw1281 | kwp2000 | uds
    vag: Optional[int] = None            # VAG 5-digit number when that is what the code is
    sae: str = ""                        # P0299 etc. when known
    text: str = ""                       # from the definitions / the standard P-code list
    status_text: str = ""                # decoded status words (ovtsuite.dtc_report)
    intermittent: bool = False
    freeze: Dict[str, Any] = field(default_factory=dict)
    freeze_raw: List[str] = field(default_factory=list)

    @property
    def display_code(self) -> str:
        parts = []
        if self.vag is not None:
            parts.append("%05d" % self.vag)
        if self.sae:
            parts.append(self.sae)
        return " · ".join(parts) if parts else "%04X" % self.code

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fault":
        return cls(code=int(d.get("code", 0)), status=int(d.get("status", 0)),
                   proto=d.get("proto", "kwp2000"), vag=d.get("vag"), sae=d.get("sae", ""),
                   text=d.get("text", ""), status_text=d.get("status_text", ""),
                   intermittent=bool(d.get("intermittent", False)),
                   freeze=dict(d.get("freeze") or {}), freeze_raw=list(d.get("freeze_raw") or []))


@dataclass
class ModuleInfo:
    """A control module as the vehicle tree shows it: the address-table row
    plus what identification said once the module answered."""
    addr: str
    name: str = ""
    protocol: str = ""                   # kw1281 | kwp2000 | uds | "" (not reached)
    transport: str = ""                  # kline | can
    part: str = ""
    sw: str = ""
    coding: Optional[int] = None
    wsc: Optional[int] = None
    identity_text: str = ""
    platform: str = ""
    reached: Optional[bool] = None       # None = not tried
    error: str = ""
    faults: List[Fault] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    #: a long coding (``22 06 00`` on the C6 body modules) as hex, "" when the
    #: module has the three-byte / short form only
    coding_bytes: str = ""

    def __post_init__(self):
        self.addr = _norm_addr(self.addr) or self.addr

    @property
    def identity(self) -> Identity:
        return Identity(addr=self.addr, protocol=self.protocol or None, part=self.part or None,
                        sw=self.sw or None, platform=self.platform or None)

    @property
    def fault_count(self) -> int:
        return len(self.faults)

    @property
    def coding_text(self) -> str:
        """The coding as the module reports it: the long form as hex bytes,
        the short form zero-padded (5 digits KW1281, 7 digits KWP2000)."""
        if self.coding_bytes:
            return " ".join(self.coding_bytes[i:i + 2] for i in range(0, len(self.coding_bytes), 2)).upper()
        if self.coding is None:
            return ""
        return ("%05d" if self.protocol == "kw1281" else "%07d") % self.coding

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["faults"] = [f.to_dict() for f in self.faults]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModuleInfo":
        m = cls(addr=d.get("addr", "00"), name=d.get("name", ""), protocol=d.get("protocol", ""),
                transport=d.get("transport", ""), part=d.get("part", ""), sw=d.get("sw", ""),
                coding=d.get("coding"), wsc=d.get("wsc"), identity_text=d.get("identity_text", ""),
                platform=d.get("platform", ""), reached=d.get("reached"), error=d.get("error", ""),
                extra=dict(d.get("extra") or {}), coding_bytes=str(d.get("coding_bytes", "") or ""))
        m.faults = [Fault.from_dict(f) for f in d.get("faults") or []]
        return m


@dataclass
class ScanResult:
    vehicle_id: str
    when: float = field(default_factory=time.time)
    interface: str = ""
    transport: str = ""
    generation: str = ""
    modules: List[ModuleInfo] = field(default_factory=list)
    duration_s: float = 0.0
    trace_path: str = ""
    #: the gateway's installation list when the scan read one
    #: (:class:`ovtsuite.diag.gateway.InstallationList` as a dict), else None
    gateway: Optional[Dict[str, Any]] = None
    #: how the modules were visited: "table" (every row), "gateway" (the
    #: modules the gateway lists as installed)
    basis: str = "table"
    #: the user stopped the scan before every module was visited; the rows
    #: not visited are in ``modules`` with ``reached=None`` and a reason
    stopped: bool = False

    @property
    def reached(self) -> List[ModuleInfo]:
        return [m for m in self.modules if m.reached]

    @property
    def visited(self) -> List[ModuleInfo]:
        """The modules the scan actually tried (reached or not)."""
        return [m for m in self.modules if m.reached is not None]

    @property
    def fault_total(self) -> int:
        return sum(m.fault_count for m in self.modules if m.reached)

    def module(self, addr: str) -> Optional[ModuleInfo]:
        addr = _norm_addr(addr)
        for m in self.modules:
            if m.addr == addr:
                return m
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"vehicle_id": self.vehicle_id, "when": self.when, "interface": self.interface,
                "transport": self.transport, "generation": self.generation,
                "duration_s": self.duration_s, "trace_path": self.trace_path,
                "gateway": self.gateway, "basis": self.basis, "stopped": self.stopped,
                "modules": [m.to_dict() for m in self.modules]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScanResult":
        s = cls(vehicle_id=d.get("vehicle_id", ""), when=float(d.get("when", 0)),
                interface=d.get("interface", ""), transport=d.get("transport", ""),
                generation=d.get("generation", ""), duration_s=float(d.get("duration_s", 0)),
                trace_path=d.get("trace_path", ""), gateway=d.get("gateway"),
                basis=str(d.get("basis", "table") or "table"), stopped=bool(d.get("stopped", False)))
        s.modules = [ModuleInfo.from_dict(m) for m in d.get("modules") or []]
        return s


@dataclass
class ModuleDiff:
    addr: str
    name: str
    new_faults: List[Fault] = field(default_factory=list)
    cleared_faults: List[Fault] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)     # "coding 0000071 -> 0000072", "software 8449 -> 8450"
    newly_reached: bool = False
    lost: bool = False

    @property
    def empty(self) -> bool:
        return not (self.new_faults or self.cleared_faults or self.changes or self.newly_reached or self.lost)


@dataclass
class ScanDiff:
    before: ScanResult
    after: ScanResult
    modules: List[ModuleDiff] = field(default_factory=list)

    @property
    def new_faults(self) -> int:
        return sum(len(m.new_faults) for m in self.modules)

    @property
    def cleared_faults(self) -> int:
        return sum(len(m.cleared_faults) for m in self.modules)

    @property
    def changed(self) -> List[ModuleDiff]:
        return [m for m in self.modules if not m.empty]


def diff_scans(before: ScanResult, after: ScanResult) -> ScanDiff:
    """What changed between two scans of the same vehicle, per module:
    faults that appeared or went away (by code), identity fields that
    changed, modules newly reached or no longer answering."""
    out = ScanDiff(before, after)
    addrs = sorted({m.addr for m in before.modules} | {m.addr for m in after.modules})
    for a in addrs:
        b, n = before.module(a), after.module(a)
        md = ModuleDiff(addr=a, name=(n or b).name)
        if (b is None or not b.reached) and (n is not None and n.reached):
            md.newly_reached = True
        if (b is not None and b.reached) and (n is None or not n.reached):
            md.lost = True
        if b is not None and n is not None and b.reached and n.reached:
            bc = {f.code: f for f in b.faults}
            nc = {f.code: f for f in n.faults}
            md.new_faults = [nc[c] for c in nc if c not in bc]
            md.cleared_faults = [bc[c] for c in bc if c not in nc]
            for label, x, y in (("part", b.part, n.part), ("software", b.sw, n.sw),
                                ("coding", b.coding_text, n.coding_text), ("workshop code", b.wsc, n.wsc)):
                if x != y and (x or y):
                    md.changes.append("%s %s -> %s" % (label, _fmt(label, x), _fmt(label, y)))
        out.modules.append(md)
    return out


def _fmt(label: str, v: Any) -> str:
    if v is None or v == "":
        return "-"
    if label == "workshop code" and isinstance(v, int):
        return "%05d" % v
    return str(v)
