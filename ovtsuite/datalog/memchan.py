# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Channels read straight out of RAM, for signals no measuring block can show.

Nineteen signals on the EDC16's AST path -- the gearbox speed-synchronisation
request and everything the controller does with it -- have an ECU_ADDRESS in
BPW.a2l and **no measuring descriptor**, which ``tools/kwp_find_signal.py``
confirms one by one. No descriptor means no block cell can ever be pointed at
them, so editing a group would not help. ``readMemoryByAddress (0x23)`` is the
only route short of patching code.

That matters because of what the dual log settled: across 26 downshifts, 20 of
them at a shut throttle, the gearbox requested nothing on any channel a block
can see, while the same drive caught four upshift interventions and a drive-off
one. Either the TCU never asks on a downshift, or it does and the ECU drops it
because AST is switched off -- and the only signal that tells those apart is
``FrmMng_nDesAST``, which is address-read only.

Addresses and types come from ``Reference/A2L/BPW.a2l``, the P475 family's own
source, not from an inferred delta off another family's A2L. (That delta --
AB + 0x482E -- was tried and is wrong by 2 to 4 bytes on most symbols, which
reads as plausible nonsense rather than as an error.)
"""
from typing import List, NamedTuple

from .logfile import Channel


class MemSignal(NamedTuple):
    name: str                   # A2L measurement name
    addr: int
    width: int
    signed: bool
    scale: float                # raw -> physical
    unit: str
    note: str


#: The AST chain, in the order the mechanism runs: what the gearbox asked for,
#: what the clamp did to it, what the controller made of the error, what came
#: out, and the switches that gate the whole thing.
AST_CHAIN: List[MemSignal] = [
    MemSignal("FrmMng_nDesAST", 0x7F99CA, 2, True, 1.0, "rpm",
              "the TCU's synchronisation speed request -- THE signal in question"),
    MemSignal("FrmMng_tiDesAST", 0x7F9A20, 2, True, 1.0, "ms", "synchronisation time"),
    MemSignal("FrmMng_stAST", 0x7F99FC, 2, False, 1.0, "-", "AST intervention status"),
    MemSignal("FrmMng_stCurInf", 0x7F942E, 1, False, 1.0, "-", "speed-influence status"),
    MemSignal("FrmMng_rCurInf", 0x7F99E8, 2, True, 1.0, "%", "speed influence sent on CAN"),
    MemSignal("FrmMng_nDes_mp", 0x802DC8, 2, False, 1.0, "rpm", "engine desired speed"),
    MemSignal("Gearbx_stAST", 0x7F9A58, 2, False, 1.0, "-", "gearbox AST status"),
    MemSignal("Gearbx_nSyncTmp_mp", 0x802EBC, 2, True, 1.0, "rpm", "calculated AST desired speed"),
    MemSignal("Gearbx_nASTSyntmp_mp", 0x802EB6, 2, True, 1.0, "rpm",
              "CLAMPED desired speed -- nASTMin/nASTMax bite here"),
    MemSignal("Gearbx_nSynGovDev_mp", 0x802EBE, 2, True, 1.0, "rpm", "controller deviation"),
    MemSignal("Gearbx_trqPGvnrOut_mp", 0x802EE2, 2, True, 0.1, "Nm", "P controller output"),
    MemSignal("Gearbx_trqASTInc_mp", 0x802ECC, 2, True, 0.1, "Nm",
              "AST intervention torque, after plausibility"),
    MemSignal("Gearbx_trqAST_mp", 0x802ED4, 2, True, 0.1, "Nm", "AST intervention torque"),
    MemSignal("Gearbx_trqASTMax_mp", 0x802ECE, 2, True, 0.1, "Nm", "AST torque ceiling"),
    MemSignal("Gearbx_trqPreCtl_mp", 0x802EE8, 2, True, 0.1, "Nm", "AST pre-control torque"),
    MemSignal("Gearbx_stTIILim_mp", 0x802EC1, 1, False, 1.0, "-", "AST limitation state"),
    MemSignal("Gearbx_swtType", 0x7F94AB, 1, False, 1.0, "-",
              "gearbox type 0=MT 1=AT 2=AST 3=CVT"),
    MemSignal("Gearbx_swtDemtype", 0x7F94AA, 1, False, 1.0, "-", "TSC enable switch output"),
    MemSignal("Gearbx_stTSCErr_mp", 0x802EC2, 1, False, 1.0, "-",
              "TSC error (intervention time exceeded)"),
]

#: Signals that ALSO appear in a measuring block, so an address read of them can
#: be checked against the block the logger already knows how to read. Without a
#: control like this a wrong decode -- an echoed address, the wrong endianness,
#: a two-byte read of a one-byte signal -- produces numbers that look entirely
#: reasonable. ``(signal, addr, width, signed, group, slot)``.
CONTROLS = [
    ("VSSCD_v", 0x7F9D00, 2, False, 6, 0),
    ("Gearbx_trqDec", 0x7F9A5E, 2, True, 9, 2),
]

#: Only the P475 family (03G906016LQ/KN, EDC16U31) was read from BPW.a2l, so the
#: addresses are only meaningful there. Offering them against another family
#: would read whatever happens to live at those addresses.
FAMILIES = ("p475_83", "p475")


def available_for(session) -> bool:
    """True when this binary's family is one the addresses were taken from."""
    fam = (getattr(session, "family", None) or "")
    return any(str(fam).lower().startswith(f) for f in FAMILIES)


def decode(sig: MemSignal, raw) -> float:
    if raw is None or len(raw) < sig.width:
        return None
    v = int.from_bytes(raw[:sig.width], "big", signed=sig.signed)
    return v * sig.scale


def text(sig: MemSignal, value) -> str:
    if value is None:
        return "N/A"
    if sig.unit == "-":
        return "0x%0*X" % (sig.width * 2, int(value))
    return "%g %s" % (value, sig.unit)


def channels(signals=None) -> List[Channel]:
    """``Channel`` objects the logger and the log file can carry."""
    out = []
    for s in (signals if signals is not None else AST_CHAIN):
        out.append(Channel(group=0, field=0, label="%s (%s)" % (s.note, s.name),
                           unit="" if s.unit == "-" else s.unit, signal=s.name,
                           addr=s.addr, width=s.width, signed=s.signed, scale=s.scale))
    return out


def signal_for(ch: Channel):
    """The MemSignal a memory Channel came from, by address."""
    for s in AST_CHAIN:
        if s.addr == ch.addr and s.width == ch.width:
            return s
    return MemSignal(ch.signal or "mem", ch.addr, ch.width or 1, bool(ch.signed),
                     ch.scale or 1.0, ch.unit or "-", "")
