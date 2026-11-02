# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The CAN gateway's installation list.

A C6 gateway (4F0907468x, "GW-BEM 5CAN-M") answers
``readDataByCommonIdentifier 22 04 A1`` with one four-byte record per
module it knows::

    <module address> <TP 2.0 logical address> <status> <bus>

e.g. ``17 07 05 03``: instruments, channel setup at dest 0x07, status 0x05,
bus 0x03. That is the module address table of the car as the car holds
it, so a scan reads it first and visits what is actually installed instead
of trying every address of a generic table.

Everything here comes from one source: Bear's own sniffer capture of a
workshop-tool autoscan on his A6 C6 (``Reference/Logs/VCDS-Autoscan.log``,
2026-09-20), which holds the list twice -- before and after the tool
cleared every module's faults. What the two readings and the scan in
between establish, and nothing more, is decoded here:

* every module whose status is ``00`` was absent from the car (the tool
  visited none of them and none of the 24 modules it did visit had a zero
  status) -- ``installed`` is "status not zero";
* bit 2 of the status (``0x04``) was set exactly on the modules that
  carried faults the clear removed, and dropped on them afterwards
  (``05 -> 01``, ``15 -> 05``, ``06 -> 02``) -- ``faults_flagged``;
* the bus byte groups the modules the way the car's networks do: ``00``
  on engine, gearbox, ABS, airbag, steering angle; ``01`` on the comfort
  modules (doors, HVAC, seat memory, central electrics); ``04`` on radio,
  navigation, telephone, sound. The other values (``03`` instruments,
  ``05``, ``0D`` the gateway itself) are shown as numbers.

The remaining status bits (``0x02``, ``0x10``) changed in that capture too
but not in a way one capture pins down; they are kept raw.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: the DID the C6 gateway serves its installation list on
DID_INSTALLATION_LIST = 0x04A1
RECORD_LEN = 4
STATUS_FAULTS = 0x04

BUS_NAMES = {0x00: "drive CAN", 0x01: "comfort CAN", 0x04: "infotainment"}


@dataclass
class GatewayEntry:
    addr: str                    # two hex digits, the module address
    dest: int                    # TP 2.0 logical address (channel setup byte)
    status: int
    bus: int
    name: str = ""               # from the address table when known

    @property
    def installed(self) -> bool:
        return self.status != 0

    @property
    def faults_flagged(self) -> bool:
        return bool(self.status & STATUS_FAULTS)

    @property
    def bus_name(self) -> str:
        return BUS_NAMES.get(self.bus, "bus 0x%02X" % self.bus)

    @property
    def status_text(self) -> str:
        if not self.installed:
            return "not installed"
        words = ["installed"]
        if self.faults_flagged:
            words.append("faults flagged")
        rest = self.status & ~STATUS_FAULTS & 0xFF
        if rest not in (0x00, 0x01):
            words.append("status 0x%02X" % self.status)
        return ", ".join(words)

    def to_dict(self) -> Dict[str, Any]:
        return {"addr": self.addr, "dest": self.dest, "status": self.status, "bus": self.bus, "name": self.name}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GatewayEntry":
        return cls(addr=str(d.get("addr", "00")), dest=int(d.get("dest", 0)), status=int(d.get("status", 0)),
                   bus=int(d.get("bus", 0)), name=str(d.get("name", "")))


@dataclass
class InstallationList:
    entries: List[GatewayEntry] = field(default_factory=list)
    raw: bytes = b""
    gateway_addr: str = "19"

    @property
    def installed(self) -> List[GatewayEntry]:
        return [e for e in self.entries if e.installed]

    def entry(self, addr: str) -> Optional[GatewayEntry]:
        addr = addr.upper()
        for e in self.entries:
            if e.addr == addr:
                return e
        return None

    def by_dest(self, dest: int) -> Optional[GatewayEntry]:
        for e in self.entries:
            if e.dest == dest:
                return e
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"gateway_addr": self.gateway_addr, "raw": self.raw.hex(),
                "entries": [e.to_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstallationList":
        out = cls(gateway_addr=str(d.get("gateway_addr", "19")))
        try:
            out.raw = bytes.fromhex(d.get("raw", "") or "")
        except ValueError:
            out.raw = b""
        out.entries = [GatewayEntry.from_dict(e) for e in d.get("entries") or []]
        return out


def parse_installation_list(data: bytes, names: Optional[Dict[str, str]] = None) -> InstallationList:
    """``data`` is the DID's payload (after ``62 04 A1``): whole four-byte
    records; a trailing partial record is ignored. ``names`` fills each
    entry's module name from the address table."""
    data = bytes(data)
    names = names or {}
    out = InstallationList(raw=data)
    for i in range(0, len(data) - RECORD_LEN + 1, RECORD_LEN):
        addr, dest, status, bus = data[i:i + RECORD_LEN]
        a = "%02X" % addr
        out.entries.append(GatewayEntry(addr=a, dest=dest, status=status, bus=bus, name=names.get(a, "")))
    return out


__all__ = ["GatewayEntry", "InstallationList", "parse_installation_list", "DID_INSTALLATION_LIST",
           "BUS_NAMES", "STATUS_FAULTS"]
