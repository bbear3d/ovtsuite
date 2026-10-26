# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fault-code reads as records: what the report window shows and what a
project keeps (Qt-free; the window is :mod:`ovtsuite.gui_dtc_report`).

A :class:`Read` is one read of the ECU's fault memory (or one clear and the
read after it): when, which controller and ECU ident, over which cable, and
each code with its status byte, P-code, description and the fault paths of
the open file that can set it (with whether each was enabled) -- a snapshot,
so a read kept in the project still says what the file was then.

**The status byte means different things on the two protocols** (checked
in the ECU firmware, see :func:`decode_status`):

* **KW1281** (the fault-code block an EDC15 answers): the low 7 bits are
  the VAG fault type (:data:`KW1281_FAULT_TYPES`, "07 - signal too low"),
  bit 7 the sporadic flag a scan tool writes as "-10".
* **KWP2000** ``readDiagnosticTroubleCodesByStatus`` (EDC16, the gearboxes):
  a bit field -- bits 0-3 the fault symptom, bit 4 test not complete, bit 5
  stored, bit 6 present now, bit 7 warning lamp. "07-10" means nothing
  there: 0x60 is "present, stored", not fault type 96.

Which one applies comes from the protocol the code was read with
(``flash.diag.Dtc.proto``, kept per code) or, for reads kept before that
was recorded, from the controller (EDC15 = KW1281). What a number means on
one ECU family (the symptom numbers of an EDC16, which fault types an EDC15
sends) is the family module's: :data:`_PLATFORM_STATUS`.

A project keeps each read as an **attachment** (``project.attachments``) of
kind :data:`KIND`: a small JSON file, tied to the binary set it was read for,
with ``extra["kind"] = "dtc_read"``. The attachment lists are merged record
by record (``ovtsuite.project.disksync``), so reads made in two copies of a
project are all kept, and the Project panel lists them with the other
attachments.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus

#: ``Attachment.extra["kind"]`` of a kept read
KIND = "dtc_read"
#: the record format's version (in the JSON)
FORMAT = 1

#: The VAG fault types ("Fehlerarten") of a KW1281 fault record -- the low
#: 7 bits of its status byte -- as VAG scan tools name them. Source: the
#: "Fehlerarten" list of VAG-COM's fault table (KWHack/Reference/Trouble
#: Codes/5297Fehlertabelle_f_r_VAG.docx, German original; its English
#: edition is a machine translation, so the words here are translated from
#: the German). The list has no 0 and no 64-66, so those show as their
#: number; it also lists 192-194, which a 7-bit field cannot carry (bit 7
#: is the sporadic flag) and so are left out. 35 is blank in the list: the
#: type VAG gives a code whose number already says what failed (an OBD
#: P-code style 16xxx-19xxx code) -- the EDC15 sends it for nearly every
#: code (``ovtsuite.edc15.dtcstatus15``).
KW1281_FAULT_TYPES: Dict[int, str] = {
    1: "short to plus",
    2: "short to ground",
    3: "no signal",
    4: "mechanical fault",
    5: "input open",
    6: "signal too high",
    7: "signal too low",
    8: "control limit exceeded",
    9: "adaptation limit exceeded",
    10: "adaptation limit not reached",
    11: "control limit not reached",
    12: "adaptation limit (mul.) exceeded",
    13: "adaptation limit (mul.) not reached",
    14: "adaptation limit (add.) exceeded",
    15: "adaptation limit (add.) not reached",
    16: "signal outside tolerance",
    17: "control difference",
    18: "upper stop value",
    19: "lower stop value",
    20: "fault in basic setting",
    21: "front pressure build-up time too long",
    22: "front pressure release time too long",
    23: "rear pressure build-up time too long",
    24: "rear pressure release time too long",
    25: "undefined switch state",
    26: "output open",
    27: "implausible signal",
    28: "short to plus",
    29: "short to ground",
    30: "open or short to plus",
    31: "open or short to ground",
    32: "resistance too high",
    33: "resistance too low",
    34: "no fault type detected",
    35: "-",
    36: "open circuit",
    37: "defective",
    38: "output does not switch / short to plus",
    39: "output does not switch / short to ground",
    40: "short to another valve",
    41: "blocked or no voltage",
    42: "speed deviation too large",
    43: "closed",
    44: "short circuit",
    45: "connector",
    46: "leaking",
    47: "wrongly connected",
    48: "power supply",
    49: "no communication",
    50: "early position not reached",
    51: "late position not reached",
    52: "supply voltage too high",
    53: "supply voltage too low",
    54: "wrong equipment",
    55: "adaptation not carried out",
    56: "in limp-home mode",
    57: "electrical fault in circuit",
    58: "does not lock",
    59: "does not unlock",
    60: "not safed",
    61: "not unsafed",
    62: "no or wrong setting",
    63: "temperature shut-down",
    67: "target value not reached",
    68: "cylinder 1",
    69: "cylinder 2",
    70: "cylinder 3",
    71: "cylinder 4",
    72: "cylinder 5",
    73: "cylinder 6",
    74: "cylinder 7",
    75: "cylinder 8",
    76: "terminal 30 missing",
    77: "internal power supply",
    78: "missing messages",
    79: "please read fault memory",
    80: "in single-wire mode",
    81: "open",
    82: "triggered",
}
#: the old name (the table is the KW1281 one)
FAULT_TYPES = KW1281_FAULT_TYPES

PROTO_KW1281 = "kw1281"
PROTO_KWP2000 = "kwp2000"

#: KWP2000 statusOfDTC bits above the symptom nibble, as the EDC16 builds
#: them (ovtsuite.edc16.dtcstatus16 has the firmware evidence). Assumed the
#: same on the other KWP2000 controllers (gearboxes): unverified there.
KWP_NOT_TESTED = 0x10
KWP_STORED = 0x20
KWP_PRESENT = 0x40
KWP_LAMP = 0x80

#: controller -> the module that knows what its status numbers mean
#: (``decode(status, proto) -> StatusInfo or None``; imported when used, so
#: an install without that module falls back to the generic decoding)
_PLATFORM_STATUS = {"EDC15": "ovtsuite.edc15.dtcstatus15",
                    "EDC16": "ovtsuite.edc16.dtcstatus16"}

#: the default web search (``{q}`` = the URL-encoded query)
DEFAULT_SEARCH_URL = "https://www.google.com/search?q={q}"


@dataclass(frozen=True)
class SearchSite:
    """A place "Search the web" can open: ``template`` with ``{q}`` for the
    URL-encoded query; ``codes_only``: the query is the VAG code(s) alone
    (a code wiki finds its page by the number)."""
    id: str
    label: str
    template: str
    codes_only: bool = False


#: the sites, first = default. Sites with their own search use it; forums
#: whose search needs a login are searched through Google, restricted to them.
SEARCH_SITES: Tuple[SearchSite, ...] = (
    SearchSite("google", "Google", DEFAULT_SEARCH_URL),
    SearchSite("bing", "Bing", "https://www.bing.com/search?q={q}"),
    SearchSite("ddg", "DuckDuckGo", "https://duckduckgo.com/?q={q}"),
    SearchSite("tdiclub", "TDIClub forum",
               "https://www.google.com/search?q=site%3Aforums.tdiclub.com+{q}"),
    SearchSite("myturbodiesel", "MyTurboDiesel forum",
               "https://www.google.com/search?q=site%3Amyturbodiesel.com+{q}"),
    SearchSite("rosstech", "Ross-Tech wiki (VAG code)",
               "https://wiki.ross-tech.com/wiki/index.php?search={q}", codes_only=True),
)


def search_site(site_id: str) -> SearchSite:
    """The site ``site_id``, the default for an unknown one."""
    return next((s for s in SEARCH_SITES if s.id == site_id), SEARCH_SITES[0])


# ------------------------------------------------------------------ status
@dataclass(frozen=True)
class StatusInfo:
    """A status byte in words.

    ``code``: as the report's Status column shows it (``"07-10"`` for
    KW1281, ``"0x60"`` for KWP2000); ``number``: the fault type (KW1281) or
    fault symptom (KWP2000); ``name``: its words (``""`` = not known);
    ``intermittent``: KW1281 sporadic flag / KWP2000 stored but not present
    now; ``flags``: the KWP2000 bits in words; ``note``: what the number
    means on this ECU (a tooltip)."""
    proto: str
    code: str
    number: int
    name: str
    intermittent: bool
    flags: Tuple[str, ...] = ()
    note: str = ""

    @property
    def fault_type(self) -> str:
        """``"07 - signal too low"``; ``"07 - fault type 7"`` when unnamed."""
        what = "fault type" if self.proto == PROTO_KW1281 else "symptom"
        return "%02d - %s" % (self.number, self.name or "%s %d" % (what, self.number))

    @property
    def text(self) -> str:
        """The status code and its words in one line."""
        bits = [self.code, self.name or "-"]
        bits += list(self.flags)
        if self.proto == PROTO_KW1281 and self.intermittent:
            bits.append("intermittent")
        return "%s %s" % (bits[0], ", ".join(bits[1:]))


def protocol_for(controller: str = "", proto: str = "") -> str:
    """The protocol a status byte was read with: ``proto`` when known, else
    by controller -- EDC15 fault memory is read over KW1281, every other
    controller here over KWP2000. With neither (an old record, a bare
    status) it is KW1281, the VAG convention the report started with."""
    if proto in (PROTO_KW1281, PROTO_KWP2000):
        return proto
    c = (controller or "").strip().upper()
    if not c or c == "EDC15":
        return PROTO_KW1281
    return PROTO_KWP2000


def decode_kw1281(status: int, names: Optional[Dict[int, str]] = None,
                  note: str = "") -> StatusInfo:
    """A KW1281 fault record's status: fault type (low 7 bits) and sporadic
    flag (bit 7); the EDC15 module has the firmware evidence
    (``ovtsuite.edc15.dtcstatus15``)."""
    s = int(status or 0) & 0xFF
    n = s & 0x7F
    return StatusInfo(PROTO_KW1281, "%02d-%02d" % (n, (s >> 7) * 10), n,
                      (names if names is not None else KW1281_FAULT_TYPES).get(n, ""),
                      bool(s & 0x80), (), note)


def kwp_flags(status: int) -> Tuple[str, ...]:
    """The KWP2000 statusOfDTC bits 4-7 in words."""
    s = int(status or 0)
    out = ["present" if s & KWP_PRESENT else "not present now"]
    if s & KWP_STORED:
        out.append("stored")
    if s & KWP_NOT_TESTED:
        out.append("not tested since switch-on")
    if s & KWP_LAMP:
        out.append("lamp on")
    return tuple(out)


def decode_kwp2000(status: int, names: Optional[Dict[int, str]] = None,
                   note: str = "") -> StatusInfo:
    """A KWP2000 statusOfDTC: symptom (bits 0-3) and flags (bits 4-7).
    Without a family's ``names`` the symptom is only its number -- KWP2000
    leaves its meaning to the maker."""
    s = int(status or 0) & 0xFF
    n = s & 0x0F
    return StatusInfo(PROTO_KWP2000, "0x%02X" % s, n, (names or {}).get(n, ""),
                      not s & KWP_PRESENT, kwp_flags(s), note)


def decode_status(status: int, controller: str = "", proto: str = "") -> StatusInfo:
    """``status`` in words, for a code ``controller`` reported over ``proto``
    (see :func:`protocol_for`). The controller's family module decodes it
    when installed (:data:`_PLATFORM_STATUS`); otherwise the generic
    decoding of the protocol applies."""
    p = protocol_for(controller, proto)
    mod = _PLATFORM_STATUS.get((controller or "").strip().upper())
    if mod:
        try:
            import importlib
            got = importlib.import_module(mod).decode(int(status or 0), p)
            if got is not None:
                return got
        except ImportError:
            pass
    return decode_kw1281(status) if p == PROTO_KW1281 else decode_kwp2000(status)


def fault_type_number(status: int, controller: str = "", proto: str = "") -> int:
    return decode_status(status, controller, proto).number


def fault_type(status: int, controller: str = "", proto: str = "") -> str:
    """``"07 - signal too low"`` for the status byte's fault type (KW1281)
    or fault symptom (KWP2000)."""
    return decode_status(status, controller, proto).fault_type


def intermittent(status: int, controller: str = "", proto: str = "") -> bool:
    return decode_status(status, controller, proto).intermittent


def status_code(status: int, controller: str = "", proto: str = "") -> str:
    return decode_status(status, controller, proto).code


def status_text(status: int, controller: str = "", proto: str = "") -> str:
    """The status as the report writes it and in words: ``"07-10 signal too
    low, intermittent"`` (KW1281), ``"0x60 -, present, stored"`` (KWP2000)."""
    return decode_status(status, controller, proto).text


# ------------------------------------------------------------------ code words
#: how the code word an ECU reports is encoded (the values of
#: ``edc16.dsm16.ENC_KW`` / ``ENC_SAE``): the VAG 5-digit number
#: (``0x4000 + d*1024 + xyz`` for Pdxyz; every EDC15, P350/P379/P213/P216
#: EDC16) or the SAE J2012 word itself (P447, P475_83/84, late P529 EDC16:
#: 0x0103 for P0103). "" = not known (a read kept before this was recorded,
#: or no file open) -- taken as the 5-digit encoding, as before.
ENC_VAG = "vag"
ENC_SAE = "sae"


def vag_text(code: int, encoding: str = "") -> str:
    """The code word as a scan tool writes the number: five digits for a
    VAG number; for an SAE word, six digits -- the form VCDS and Ross-Tech
    give a KWP2000 ECU that reports SAE words ("000772 - Cylinder 4" /
    "P0304" on a 5K0907115 MED17 log, gist.github.com/victorhooi/0f6d40146
    acac568b92907a3db4eb713; Ross-Tech wiki page "16785/P0401/001025":
    1025 = 0x0401). A placeholder (33000 + DSM number) is shown the same way."""
    return ("%06d" if encoding == ENC_SAE else "%05d") % int(code)


def pcode_of(code: int, encoding: str = "") -> str:
    """The OBD code a reported word stands for, under ``encoding`` (as
    ``edc16.dsm16.customer_pcode``): the SAE word decoded, or the P-code a
    VAG 5-digit number stands for. '' when it stands for none."""
    from .dtc import decode_sae, pcode_of_vag5
    raw = int(code or 0)
    if encoding == ENC_SAE:
        # 0x8000..0xBFFF holds the 33000+n placeholders, not B-codes
        return decode_sae(raw) if raw and (raw < 0x8000 or raw >= 0xC000) else ""
    return pcode_of_vag5(raw)


def vag5_of(code: int, encoding: str = "") -> Optional[int]:
    """The VAG 5-digit number of a code (19456 for P3000) -- the number VAG
    gives the same fault on its older ECUs; the code itself when it is one."""
    if encoding != ENC_SAE:
        return int(code)
    pc = pcode_of(code, encoding)
    if pc[:1] == "P" and pc[1:].isdigit() and int(pc[1]) < 4:
        return 0x4000 + int(pc[1]) * 1024 + int(pc[2:])
    return None


def dtc_text(dtc, encoding: str = "", controller: str = "") -> str:
    """One stored code in a line: ``"16485 - 07-10"`` (KW1281),
    ``"000259 P0103 - status 0x60"`` (KWP2000, SAE words)."""
    code = int(getattr(dtc, "code", 0) or 0)
    si = decode_status(int(getattr(dtc, "status", 0) or 0), controller,
                       str(getattr(dtc, "proto", "") or ""))
    pc = pcode_of(code, encoding) if encoding == ENC_SAE else ""
    num = vag_text(code, encoding) + (" " + pc if pc else "")
    if si.proto == PROTO_KW1281:
        return "%s - %s" % (num, si.code)
    return "%s - status %s" % (num, si.code)


def generic_describe(code: int, encoding: str = "") -> Tuple[str, str]:
    """``(P-code, description)`` of a code no fault path of the file sets:
    the P-code the word stands for (:func:`pcode_of`), and its SAE text."""
    from .dtc import _load_pdesc
    pc = pcode_of(code, encoding)
    return pc, (_load_pdesc().get(pc, "") if pc else "")


# ------------------------------------------------------------------ records
@dataclass
class PathRef:
    """A fault path of the open file that can set a code."""
    name: str
    enabled: bool

    def to_dict(self):
        return {"name": self.name, "enabled": bool(self.enabled)}

    @classmethod
    def from_dict(cls, d):
        return cls(str(d.get("name", "")), bool(d.get("enabled", True)))


@dataclass
class CodeEntry:
    code: int
    status: int = 0
    pcode: str = ""
    desc: str = ""
    paths: List[PathRef] = field(default_factory=list)
    #: False when the read was made without a fault-path table to match
    #: against (the report then says nothing about fault paths)
    matched_known: bool = True
    #: the protocol the code was read with (:data:`PROTO_KW1281` /
    #: :data:`PROTO_KWP2000`); "" in reads kept before it was recorded
    proto: str = ""
    #: the controller that reported it, and how its code word is encoded
    #: (:data:`ENC_SAE` ...); both set from the :class:`Read`, not saved
    #: per code
    controller: str = field(default="", compare=False)
    encoding: str = field(default="", compare=False)

    @property
    def vag(self) -> str:
        return vag_text(self.code, self.encoding)

    @property
    def vag5(self) -> Optional[int]:
        """The VAG 5-digit number of the same fault (for an SAE word)."""
        return vag5_of(self.code, self.encoding)

    @property
    def status_info(self) -> StatusInfo:
        return decode_status(self.status, self.controller, self.proto)

    @property
    def fault_type(self) -> str:
        return self.status_info.fault_type

    @property
    def intermittent(self) -> bool:
        return self.status_info.intermittent

    @property
    def status_code(self) -> str:
        return self.status_info.code

    @property
    def status_words(self) -> str:
        """What the status says besides the fault type: "intermittent"
        (KW1281), "present, stored" (KWP2000)."""
        si = self.status_info
        if si.proto == PROTO_KW1281:
            return "intermittent" if si.intermittent else ""
        return ", ".join(si.flags)

    @property
    def matched(self) -> bool:
        return bool(self.paths)

    def paths_text(self) -> str:
        if not self.matched_known:
            return ""
        if not self.paths:
            return "none in this file"
        return ", ".join("%s (%s)" % (p.name, "on" if p.enabled else "off")
                         for p in self.paths)

    def to_dict(self):
        d = {"code": int(self.code), "status": int(self.status), "pcode": self.pcode,
             "desc": self.desc, "paths": [p.to_dict() for p in self.paths]}
        if not self.matched_known:
            d["matched_known"] = False
        if self.proto:
            d["proto"] = self.proto
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(int(d.get("code", 0)), int(d.get("status", 0)),
                   str(d.get("pcode", "") or ""), str(d.get("desc", "") or ""),
                   [PathRef.from_dict(p) for p in d.get("paths") or []],
                   bool(d.get("matched_known", True)), str(d.get("proto", "") or ""))


@dataclass
class Read:
    """One fault-memory read (``action`` "read"), or a clear and the read
    after it ("clear"; ``before`` = the codes the ECU had just before)."""
    codes: List[CodeEntry] = field(default_factory=list)
    ts: str = ""                         # ISO time
    action: str = "read"
    controller: str = ""                 # the connection's ECU: "EDC16", "ZF6HP"...
    ecu_ident: str = ""                  # what the ECU reported
    cable: str = ""
    transport: str = ""                  # "K-line" / "CAN"
    file: str = ""                       # the open file's name
    set_name: str = ""
    before: Optional[List[CodeEntry]] = None
    kept: str = ""                       # "" or the project it was kept in
    #: how the ECU encodes its code words (:data:`ENC_SAE`, :data:`ENC_VAG`;
    #: "" = not known: the 5-digit form). Saved, so a kept read opened
    #: without its file still writes the numbers the right way.
    encoding: str = ""

    def __post_init__(self):
        # each code decodes its status for the controller that sent it, and
        # writes its number in the ECU's encoding
        for c in list(self.codes) + list(self.before or []):
            if not c.controller:
                c.controller = self.controller
            if not c.encoding:
                c.encoding = self.encoding

    @property
    def code_numbers(self) -> List[int]:
        return [c.code for c in self.codes]

    @property
    def still_present(self) -> List[CodeEntry]:
        """After a clear: the codes that came straight back."""
        if self.before is None:
            return []
        had = {c.code for c in self.before}
        return [c for c in self.codes if c.code in had]

    @property
    def cleared(self) -> List[CodeEntry]:
        """After a clear: the codes that were stored and are gone."""
        if self.before is None:
            return []
        now = set(self.code_numbers)
        return [c for c in self.before if c.code not in now]

    @property
    def when(self) -> str:
        return (self.ts or "").replace("T", " ")[:19]

    def summary(self) -> str:
        """One line: "3 codes: 00532, 17964, 18010" / "no codes"."""
        if not self.codes:
            what = "no fault codes stored"
        else:
            what = "%d code%s: %s" % (len(self.codes), "" if len(self.codes) == 1 else "s",
                                      ", ".join(c.vag for c in self.codes))
        if self.action == "clear":
            what = "cleared, then %s" % what
        return what

    def to_dict(self) -> dict:
        d = {"kind": KIND, "format": FORMAT, "ts": self.ts, "action": self.action,
             "controller": self.controller, "ecu_ident": self.ecu_ident,
             "cable": self.cable, "transport": self.transport, "file": self.file,
             "set": self.set_name, "codes": [c.to_dict() for c in self.codes]}
        if self.before is not None:
            d["before"] = [c.to_dict() for c in self.before]
        if self.encoding:
            d["encoding"] = self.encoding
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Read":
        if not isinstance(d, dict) or d.get("kind") != KIND:
            raise ValueError("not a fault-code read")
        before = d.get("before")
        return cls([CodeEntry.from_dict(c) for c in d.get("codes") or []],
                   str(d.get("ts", "") or ""), str(d.get("action", "read") or "read"),
                   str(d.get("controller", "") or ""), str(d.get("ecu_ident", "") or ""),
                   str(d.get("cable", "") or ""), str(d.get("transport", "") or ""),
                   str(d.get("file", "") or ""), str(d.get("set", "") or ""),
                   None if before is None else [CodeEntry.from_dict(c) for c in before],
                   encoding=str(d.get("encoding", "") or ""))

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), indent=1).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Read":
        return cls.from_dict(json.loads(bytes(data).decode("utf-8")))


PathsFor = Callable[[int], Sequence[PathRef]]
Describe = Callable[[int, Sequence[PathRef]], Tuple[str, str]]


def build_read(dtcs: Iterable, *, paths_for: Optional[PathsFor] = None,
               describe: Optional[Describe] = None, before: Optional[Iterable] = None,
               action: str = "read", ts: str = "", **header) -> Read:
    """A :class:`Read` of ``dtcs`` (objects with ``code`` and ``status``,
    e.g. ``flash.diag.Dtc``, whose ``proto`` says how the status byte is
    built). ``paths_for(code)`` names the fault paths of
    the open file that set the code (None: no table to match against);
    ``describe(code, paths)`` gives ``(pcode, description)`` (the caller's
    fault-path lookups; :func:`generic_describe` when it gives nothing).
    ``encoding`` (a header field, the open file's :data:`ENC_SAE` /
    :data:`ENC_VAG`) says how the code words are to be read; matching
    (``paths_for``) is always by the raw word."""
    enc = str(header.get("encoding", "") or "")

    def entry(d):
        code = int(getattr(d, "code", 0) or 0)
        status = int(getattr(d, "status", 0) or 0)
        proto = str(getattr(d, "proto", "") or "")
        paths = list(paths_for(code)) if paths_for is not None else []
        pc = desc = ""
        if describe is not None:
            try:
                pc, desc = describe(code, paths)
            except Exception:
                pc, desc = "", ""
        if not pc or not desc:
            gp, gd = generic_describe(code, enc)
            pc, desc = pc or gp, desc or gd
        return CodeEntry(code, status, pc or "", desc or "", paths,
                         matched_known=paths_for is not None, proto=proto)
    return Read([entry(d) for d in dtcs], ts or time.strftime("%Y-%m-%dT%H:%M:%S"),
                action, before=None if before is None else [entry(d) for d in before],
                **header)


# ------------------------------------------------------------------ history
def diff(prev: Optional[Read], cur: Read) -> Tuple[set, set]:
    """``(new, gone)`` code numbers of ``cur`` against the read before it
    (nothing is new without one)."""
    if prev is None:
        return set(), set()
    a, b = set(prev.code_numbers), set(cur.code_numbers)
    return b - a, a - b


def is_read(att) -> bool:
    return (getattr(att, "extra", None) or {}).get("kind") == KIND


def keep(project, set_id: Optional[str], read: Read):
    """Keep ``read`` in ``project`` (an attachment of the set); returns it."""
    stamp = (read.ts or time.strftime("%Y-%m-%dT%H:%M:%S")).replace("-", "").replace(":", "")
    name = "fault-codes-%s.json" % stamp.replace("T", "-")
    a = project.add_attachment(name, read.to_json(), set_id=set_id, note=read.summary())
    a.extra["kind"] = KIND
    a.extra["codes"] = len(read.codes)
    return a


def history(project, set_id: Optional[str] = None) -> List[Tuple[object, Read]]:
    """The reads kept in ``project`` (for ``set_id``, when given), newest
    first, as ``(attachment, Read)``; unreadable ones are left out."""
    out = []
    for i, a in enumerate(getattr(project, "attachments", []) or []):
        if not is_read(a) or (set_id is not None and a.set != set_id):
            continue
        try:
            r = Read.from_json(project.attachment_bytes(a.id))
        except Exception:
            continue
        out.append((r.ts or a.added or "", i, a, r))
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(a, r) for _ts, _i, a, r in out]


def label(att) -> str:
    """A kept read in the Project panel: when, and how many codes."""
    n = (att.extra or {}).get("codes")
    when = (att.added or "").replace("T", " ")[:16]
    return "Fault codes %s%s" % (when, "" if n is None else " (%s code%s)" % (
        n, "" if n == 1 else "s"))


# ------------------------------------------------------------------ output
def header_lines(read: Read) -> List[str]:
    out = ["Fault code report -- %s" % read.when]
    if read.controller or read.ecu_ident:
        out.append("Controller: %s%s" % (read.controller, (" -- %s" % read.ecu_ident)
                                         if read.ecu_ident else ""))
    if read.cable or read.transport:
        out.append("Cable: %s%s" % (read.cable, (" (%s)" % read.transport)
                                    if read.transport else ""))
    if read.file or read.set_name:
        out.append("File: %s%s" % (read.file, (" (set %s)" % read.set_name)
                                   if read.set_name and read.set_name != read.file else ""))
    if read.action == "clear":
        if read.before is None:
            out.append("Codes were cleared, then read again.")
        else:
            back = read.still_present
            out.append("Cleared %d code(s); %s" % (
                len(read.before), ("still present (came straight back): %s" % ", ".join(
                    c.vag for c in back)) if back else "none came back"))
    return out


def to_text(read: Read, prev: Optional[Read] = None) -> str:
    """The report as plain text (Copy, Save as .txt, Print)."""
    new, gone = diff(prev, read)
    lines = header_lines(read)
    lines.append("")
    if not read.codes:
        lines.append("No fault codes stored.")
    for c in read.codes:
        mark = " [new]" if c.code in new else ""
        lines.append("%s  %s  %s%s" % (c.vag, c.pcode or "-", c.desc or "(no description)",
                                       mark))
        words = c.status_words
        lines.append("    status %s: %s%s" % (c.status_code, c.fault_type,
                                              ", " + words if words else ""))
        pt = c.paths_text()
        if pt:
            lines.append("    fault paths: %s" % pt)
    if gone and prev is not None:
        lines.append("")
        lines.append("Gone since the read before (%s): %s" % (
            prev.when, ", ".join(c.vag for c in prev.codes if c.code in gone)))
    return "\n".join(lines) + "\n"


CSV_HEAD = ("time", "controller", "ecu_ident", "vag_code", "pcode", "description",
            "status", "fault_type", "intermittent", "fault_paths", "change")


def to_csv(read: Read, prev: Optional[Read] = None) -> str:
    new, gone = diff(prev, read)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEAD)
    rows = [(c, "new" if c.code in new else "") for c in read.codes]
    if prev is not None:
        rows += [(c, "gone") for c in prev.codes if c.code in gone]
    for c, change in rows:
        w.writerow((read.when, read.controller, read.ecu_ident, c.vag, c.pcode, c.desc,
                    c.status_code, c.fault_type, "yes" if c.intermittent else "no",
                    "; ".join("%s=%s" % (p.name, "on" if p.enabled else "off")
                              for p in c.paths), change))
    return buf.getvalue()


# ------------------------------------------------------------------ actions
_PART = re.compile(r"\b\d[A-Z0-9]{2}\s?\d{3}\s?\d{3}\s?[A-Z]{0,3}\b")


def part_number(*texts: str) -> str:
    """The first VAG part number ("038906019FQ") in ``texts``."""
    for t in texts:
        m = _PART.search(t or "")
        if m:
            return m.group(0).replace(" ", "")
    return ""


def search_query(entries: Sequence[CodeEntry], part: str = "") -> str:
    """'"17964" P0299 "Charge pressure control..." 038906019FQ' per code,
    joined with OR for several."""
    bits = []
    for c in entries:
        q = ['"%s"' % c.vag]
        if c.pcode:
            q.append(c.pcode)
        if c.desc:
            q.append('"%s"' % c.desc.replace('"', ""))
        bits.append(" ".join(q))
    q = " OR ".join("(%s)" % b for b in bits) if len(bits) > 1 else (bits[0] if bits else "")
    return ("%s %s" % (q, part)).strip() if part else q


def site_url(site: SearchSite, entries: Sequence[CodeEntry], part: str = "") -> str:
    """The URL ``site`` opens for ``entries``."""
    q = (" ".join(c.vag for c in entries) if site.codes_only
         else search_query(entries, part))
    return search_url(q, site.template)


def search_url(query: str, template: str = "") -> str:
    """``template`` (``{q}`` = the encoded query; appended when absent) with
    ``query``."""
    t = (template or "").strip() or DEFAULT_SEARCH_URL
    q = quote_plus(query)
    return t.replace("{q}", q) if "{q}" in t else t + q


def ai_question(entries: Sequence[CodeEntry], handle: str, read: Read,
                engine: bool = True) -> str:
    """The question the report hands the AI assistant."""
    parts = code_lines(entries)
    ecu = " ".join(x for x in (read.controller, read.ecu_ident) if x)
    q = ("The %s%s reported %s stored fault code%s for tab %s: %s. "
         % ("ECU" if engine else "control unit", " (%s)" % ecu if ecu else "",
            "this" if len(entries) == 1 else "these", "" if len(entries) == 1 else "s",
            handle, "; ".join(parts)))
    q += ("For each code explain the likely causes, the checks to make on the car "
          "(wiring, sensor, actuator, measuring blocks to watch), and whether disabling "
          "its fault path in the file is appropriate or would only hide a real problem.")
    if engine:
        q += (" Look the paths up with the fault_codes tool; if I ask for a change, "
              "work it out with propose_fault_codes.")
    return q


def code_lines(entries: Sequence[CodeEntry]) -> List[str]:
    """Each code in words, for a question: number, P-code, text, status,
    the file's fault paths for it."""
    parts = []
    for c in entries:
        si = c.status_info
        words = c.status_words
        s = "%s (%s) \"%s\", status %s = %s%s" % (
            c.vag, c.pcode or "no P-code", c.desc or "no description", c.status_code,
            si.name or ("fault type %d" % si.number if si.proto == PROTO_KW1281
                        else "symptom %d" % si.number),
            ", " + words if words else "")
        if c.matched_known:
            s += "; fault paths in the file: %s" % (
                ", ".join("%s (%s)" % (p.name, "enabled" if p.enabled else "disabled")
                          for p in c.paths) if c.paths else "none match")
        parts.append(s)
    return parts


def recurring(reads: Sequence[Read]) -> Dict[int, int]:
    """Code -> in how many of ``reads`` it is stored (codes in two or more)."""
    n: Dict[int, int] = {}
    for r in reads:
        for c in set(r.code_numbers):
            n[c] = n.get(c, 0) + 1
    return {c: k for c, k in n.items() if k > 1}


def history_lines(reads: Sequence[Read]) -> List[str]:
    """``reads`` (newest first) one line each, oldest first, with what was
    new / gone against the read before and, after a clear, what came back."""
    out = []
    olds = list(reversed(list(reads)))
    for i, r in enumerate(olds):
        prev = olds[i - 1] if i else None
        new, gone = diff(prev, r)
        bits = ["%s: %s" % (r.when[:16], r.summary())]
        if new:
            bits.append("new %s" % ", ".join(vag_text(c, r.encoding) for c in sorted(new)))
        if gone:
            bits.append("gone %s" % ", ".join(vag_text(c, r.encoding)
                                              for c in sorted(gone)))
        if r.action == "clear" and r.before is not None:
            back = r.still_present
            bits.append("came straight back after the clear: %s" % (
                ", ".join(c.vag for c in back) if back else "none"))
        out.append("; ".join(bits))
    return out


def ai_report_question(read: Read, reads: Sequence[Read], handle: str,
                       engine: bool = True) -> str:
    """The whole-report question: this read, the set's earlier reads
    (``reads``, newest first, ``read`` among them or not) and the pattern."""
    allr = list(reads) if any(r is read for r in reads) else [read] + list(reads)
    ecu = " ".join(x for x in (read.controller, read.ecu_ident) if x)
    q = ("Interpret this fault code report for tab %s%s. " % (
        handle, " (%s %s)" % ("ECU" if engine else "control unit", ecu) if ecu else ""))
    if read.action == "clear":
        q += "The latest read followed a clear of the fault memory. "
    if read.codes:
        q += "The latest read (%s) found: %s. " % (read.when[:16],
                                                    "; ".join(code_lines(read.codes)))
    else:
        q += "The latest read found no stored fault codes. "
    if len(allr) > 1:
        rec = recurring(allr)
        q += ("Reads of this ECU so far, oldest first: %s. " % " | ".join(history_lines(allr)))
        if rec:
            q += ("Codes stored in more than one read: %s. " % ", ".join(
                "%s (%d reads)" % (vag_text(c, read.encoding), k)
                for c, k in sorted(rec.items())))
    else:
        q += "There are no earlier reads of this ECU in the project. "
    q += ("Explain what the pattern suggests: which faults are current, which are "
          "intermittent or came back after a clear, which look like one root cause "
          "showing as several codes, what to check on the car first, and whether "
          "disabling any fault path would be appropriate or would hide a real problem.")
    if engine:
        q += (" Look the paths up with the fault_codes tool; if I ask for a change, "
              "work it out with propose_fault_codes.")
    return q
