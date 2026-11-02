# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The VAG KWP2000 function set over the suite's KWP2000 dialog, in the
wire forms Bear's K-line captures of a workshop tool against an EDC16
(038997016T) established, as KWHack implements them
(``docs/KWP2000-Sniffing.md``, ``Tester.Kwp2000Diag.cs``):

* adaptation, actuator tests and basic settings all run through
  ``startRoutineByLocalIdentifier`` (0x31) with the routine family
  B8 (enter) / B9 (select, value, advance) / BA (read) / BB (save), told
  apart by a two-byte selector: ``01 03`` adaptation, ``01 02`` actuator
  test, ``00 <group>`` basic setting; ``32 B8 <selector>`` leaves;
* coding and login are one record written with
  ``writeDataByLocalIdentifier 0x9A``: the module's own 6-byte workshop
  field and 4-byte software version (read live from ``1A 9B`` and echoed,
  or the write is refused), ``03``, then three bytes -- the coding, or
  ``0x800000 | code`` for a login;
* security access is ``27 03`` -> seed, ``27 04`` -> key = seed + code;
* a fault's freeze frame is ``12 00 04 <dtc>`` -> ``52 00`` and the same
  (formula, A, B) cell stream the measuring blocks use, closed by
  ``04 <dtc>``;
* the C6 body modules and the gateway (Bear's autoscan capture,
  ``Reference/Logs/VCDS-Autoscan.log``) refuse ``1A 9B`` and serve their
  identification as data identifiers: ``22 F1 87`` part number, ``F1 89``
  software version, ``F1 91`` hardware part number, ``F1 A3`` hardware
  version, ``F1 97`` system name, ``F1 A5`` the 6-byte workshop field,
  ``06 00`` the (long) coding with ``06 01`` its length, and on the gateway
  ``04 A1`` the installation list;
* the 6-byte workshop field packs WSC (bits 0-16), importer (17-26) and
  equipment (27-46), big-endian.

Which modules use these forms is a definitions question (the module
document's ``variants``), not this file's: it implements the forms.
Qt-free.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..flash import diag as _diag
from ..flash import kwp as _kwp
from ..flash.transport import KLineError

SVC_FREEZE = 0x12
SVC_IDENT = 0x1A
SVC_READ_DID = 0x22
SVC_SECURITY = 0x27
SVC_START_ROUTINE = 0x31
SVC_STOP_ROUTINE = 0x32
SVC_WRITE_LOCAL = 0x3B
ROUTINE_ENTER = 0xB8
ROUTINE_SELECT = 0xB9
ROUTINE_READ = 0xBA
ROUTINE_SAVE = 0xBB
SEL_ADAPTATION = bytes([0x01, 0x03])
SEL_ACTUATOR = bytes([0x01, 0x02])
#: the actuator id that ends the sequence (same scheme as KW1281's 04 AB)
ACTUATOR_END = 0x04AB


class Refused(Exception):
    """A negative response: the module refused the request (NRC kept)."""

    def __init__(self, what: str, nrc: Optional[int] = None):
        super().__init__("%s refused%s" % (what, (" (NRC 0x%02X %s)" % (nrc, _nrc_name(nrc))) if nrc is not None else ""))
        self.nrc = nrc


def _nrc_name(nrc: int) -> str:
    return {0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
            0x21: "busy", 0x22: "conditionsNotCorrect", 0x31: "requestOutOfRange",
            0x33: "securityAccessDenied", 0x35: "invalidKey", 0x36: "exceededNumberOfAttempts",
            0x37: "requiredTimeDelayNotExpired", 0x78: "responsePending", 0x80: "serviceNotSupportedInActiveSession"
            }.get(nrc, "")


@dataclass
class Adaptation:
    channel: int
    value: int
    status: int = 0
    cells: List[_diag.Cell] = field(default_factory=list)   # measured / min / max / default, as served
    raw: bytes = b""


@dataclass
class ActuatorStep:
    id: int
    raw: bytes
    ended: bool


@dataclass
class Wsc:
    wsc: int
    importer: int = 0
    equipment: int = 0

    @classmethod
    def decode(cls, six: bytes) -> "Wsc":
        v = int.from_bytes(bytes(six[:6]), "big")
        return cls(v & 0x1FFFF, (v >> 17) & 0x3FF, (v >> 27) & 0xFFFFF)

    def encode(self) -> bytes:
        if not 0 <= self.wsc <= 0x1FFFF or not 0 <= self.importer <= 0x3FF or not 0 <= self.equipment <= 0xFFFFF:
            raise ValueError("WSC max 131071, importer max 1023, equipment max 1048575")
        v = (self.wsc & 0x1FFFF) | ((self.importer & 0x3FF) << 17) | ((self.equipment & 0xFFFFF) << 27)
        return v.to_bytes(6, "big")


def _cells(data: bytes) -> List[_diag.Cell]:
    data = bytes(data)
    n = len(data) - len(data) % 3
    return _diag.parse_cells(data[:n], drop_echo=False)


class Kwp2000Commands:
    def __init__(self, d: _kwp.Kwp2000, log=None):
        self.d = d
        self._log = log or (lambda s: None)

    # ------------------------------------------------------------- helpers
    def _call(self, what: str, service: int, body: bytes) -> bytes:
        try:
            return bytes(self.d.send_receive(service, bytes(body)).body)
        except _kwp.NegativeResponse as ex:
            raise Refused(what, getattr(ex, "nrc", None)) from ex

    def routine(self, rid: int, args: bytes, what: str = "") -> bytes:
        """``31 <rid> <args>``; the response body after 0x71 (rid echo first)."""
        return self._call(what or "routine 0x%02X" % rid, SVC_START_ROUTINE, bytes([rid]) + bytes(args))

    def stop_routine(self, rid: int, args: bytes, what: str = "") -> bytes:
        return self._call(what or "stop routine 0x%02X" % rid, SVC_STOP_ROUTINE, bytes([rid]) + bytes(args))

    def enumerate_functions(self) -> bytes:
        """``31 B8 00 00``: the module's channel/function map (opaque here;
        every routine function starts with it)."""
        return self.routine(ROUTINE_ENTER, b"\x00\x00", "function enumeration")

    def read_ident(self, option: int = 0x9B) -> bytes:
        return self._call("readEcuIdentification 0x%02X" % option, SVC_IDENT, bytes([option]))

    # ---------------------------------------------------------- adaptation
    def adaptation_enter(self):
        self.enumerate_functions()
        self.routine(ROUTINE_ENTER, SEL_ADAPTATION, "adaptation start")

    def adaptation_select(self, channel: int):
        self.routine(ROUTINE_SELECT, SEL_ADAPTATION + bytes([channel & 0xFF]),
                     "adaptation channel %d select" % channel)

    def adaptation_read(self, channel: int) -> Adaptation:
        body = self.routine(ROUTINE_READ, SEL_ADAPTATION, "adaptation read")
        # 71 BA 01 03 <status> 03 <val:2> <n> <n triples>
        if len(body) < 7:
            raise KLineError("KWP2000: short adaptation response (%d bytes)" % len(body))
        status = body[3]
        value = (body[5] << 8) | body[6]
        cells = _cells(body[8:]) if len(body) > 8 else []
        return Adaptation(channel=channel, value=value, status=status, cells=cells, raw=body)

    def adaptation_test(self, channel: int, value: int) -> Adaptation:
        self.routine(ROUTINE_SELECT, SEL_ADAPTATION + bytes([(value >> 8) & 0xFF, value & 0xFF]),
                     "adaptation test value")
        return self.adaptation_read(channel)

    def adaptation_save(self, channel: int, value: int, wsc6: bytes) -> Adaptation:
        self.routine(ROUTINE_SELECT, SEL_ADAPTATION + bytes([(value >> 8) & 0xFF, value & 0xFF]),
                     "adaptation test value")
        self.routine(ROUTINE_SAVE, SEL_ADAPTATION + bytes([(value >> 8) & 0xFF, value & 0xFF]) + bytes(wsc6[:6]),
                     "adaptation save")
        return self.adaptation_read(channel)

    def adaptation_exit(self):
        try:
            self.stop_routine(ROUTINE_ENTER, SEL_ADAPTATION, "adaptation exit")
        except Refused:
            pass

    # ------------------------------------------------------ actuator tests
    def actuator_enter(self):
        self.enumerate_functions()
        self.routine(ROUTINE_ENTER, SEL_ACTUATOR, "actuator test start")

    def actuator_status(self) -> Optional[int]:
        """The id of the actuator the module currently has selected, or None
        while it has not settled: ``71 BA 01 02 03 03 <id:2>``."""
        body = self.routine(ROUTINE_READ, SEL_ACTUATOR, "actuator status")
        if len(body) > 6 and body[3] == 0x03 and body[4] == 0x03:
            return (body[5] << 8) | body[6]
        return None

    def actuator_current(self, settle: int = 8) -> ActuatorStep:
        aid = None
        raw = b""
        for _ in range(settle):
            body = self.routine(ROUTINE_READ, SEL_ACTUATOR, "actuator status")
            raw = body
            if len(body) > 6 and body[3] == 0x03 and body[4] == 0x03:
                aid = (body[5] << 8) | body[6]
                break
        if aid is None:
            return ActuatorStep(id=-1, raw=raw, ended=True)
        return ActuatorStep(id=aid, raw=raw, ended=(aid == ACTUATOR_END))

    def actuator_advance(self):
        """``31 B9 01 02``: the module activates / moves to the next actuator
        of its sequence -- this switches real outputs."""
        self.routine(ROUTINE_SELECT, SEL_ACTUATOR, "actuator advance")

    def actuator_exit(self):
        try:
            self.stop_routine(ROUTINE_ENTER, SEL_ACTUATOR, "actuator test exit")
        except Refused:
            pass

    # ------------------------------------------------------ basic settings
    def basic_enter(self, group: int):
        try:
            self.enumerate_functions()
        except Refused:
            pass
        self.routine(ROUTINE_ENTER, bytes([0x00, group & 0xFF]), "basic setting %d start" % group)

    def basic_read(self, group: int) -> List[_diag.Cell]:
        """``71 BA 00 <group> <n> <n triples>``."""
        body = self.routine(ROUTINE_READ, bytes([0x00, group & 0xFF]), "basic setting %d read" % group)
        return _cells(body[4:]) if len(body) > 4 else []

    def basic_exit(self, group: int):
        try:
            self.stop_routine(ROUTINE_ENTER, bytes([0x00, group & 0xFF]), "basic setting exit")
        except Refused:
            pass

    # ---------------------------------------------------- coding and login
    def record_fields(self) -> Tuple[bytes, bytes]:
        """``(wsc6, sw4)`` as the module serves them in ``1A 9B``; the
        ``0x9A`` write must echo them."""
        rec = self.read_ident(0x9B)
        if len(rec) < 27 or rec[0] != 0x9B:
            raise KLineError("KWP2000: the 1A 9B record is %d bytes; cannot build the 0x9A write" % len(rec))
        return bytes(rec[21:27]), bytes(rec[13:17])

    def write_9a(self, value3: bytes, wsc6: Optional[bytes] = None) -> bytes:
        own6, sw4 = self.record_fields()
        record = bytes(wsc6 if wsc6 is not None else own6) + sw4 + b"\x03" + bytes(value3[:3])
        return self._call("write 0x9A record", SVC_WRITE_LOCAL, b"\x9A" + record)

    def set_coding(self, coding: int, wsc: Optional[Wsc] = None) -> None:
        if not 0 <= coding <= 0xFFFFFF:
            raise ValueError("a KWP2000 coding is 0..16777215")
        self.write_9a(coding.to_bytes(3, "big"), wsc.encode() if wsc is not None else None)

    def login(self, code: int, wsc: Optional[Wsc] = None) -> None:
        """The login form of the 0x9A record: ``0x800000 | code``."""
        if not 0 <= code <= 0xFFFF:
            raise ValueError("a login code is 0..65535")
        self.write_9a((0x800000 | code).to_bytes(3, "big"), wsc.encode() if wsc is not None else None)

    # ---------------------------------------------------- security access
    def security_access(self, code: int, mode: int = 0x03) -> bool:
        """``27 <mode>`` -> seed; ``27 <mode+1>`` key = seed + code (32-bit).
        True when the module accepts the key (or serves an all-zero seed:
        already unlocked); False on a negative response to the key."""
        seed_body = self._call("security access seed", SVC_SECURITY, bytes([mode]))
        seed = bytes(seed_body[1:5])
        if not seed or not any(seed):
            return True
        key = (int.from_bytes(seed, "big") + int(code)) & 0xFFFFFFFF
        try:
            self._call("security access key", SVC_SECURITY, bytes([mode + 1]) + key.to_bytes(4, "big"))
        except Refused as ex:
            self._log("Security access denied (%s)" % ex)
            return False
        return True

    # ------------------------------------------------- data identifiers (22)
    def read_did(self, did: int) -> bytes:
        """``22 <did:2>`` -> the identifier's data (the ``62 <did>`` echo is
        checked and stripped). One identifier per request: a module answers
        a combined request with the records run together and no lengths,
        which cannot be split without knowing every record -- so this layer
        never asks for more than one."""
        req = bytes([(did >> 8) & 0xFF, did & 0xFF])
        body = self._call("read identifier 0x%04X" % did, SVC_READ_DID, req)
        if len(body) < 2 or bytes(body[:2]) != req:
            raise KLineError("KWP2000: identifier 0x%04X answered with %s" % (did, bytes(body[:2]).hex(" ")))
        return bytes(body[2:])

    def read_did_text(self, did: int) -> str:
        """An ASCII identifier, trimmed; the module's own bytes otherwise."""
        data = self.read_did(did)
        return "".join(chr(c) for c in data if 0x20 <= c < 0x7F).strip()

    def read_dids(self, dids: Dict[str, int]) -> Dict[str, bytes]:
        """Several identifiers by name; one the module refuses is left out."""
        out: Dict[str, bytes] = {}
        for name, did in dids.items():
            try:
                out[name] = self.read_did(int(did))
            except Refused as ex:
                self._log("identifier %s (0x%04X): %s" % (name, int(did), ex))
        return out

    def installation_list(self, did: int = 0x04A1) -> bytes:
        """The gateway's installation list records (see :mod:`.gateway`)."""
        return self.read_did(did)

    # --------------------------------------------------------------- faults
    def freeze_frame(self, dtc: int) -> List[_diag.Cell]:
        """``12 00 04 <dtc>``: the fault's environment data as measuring
        cells; the closing ``04 <dtc>`` triple is dropped."""
        try:
            body = self._call("freeze frame", SVC_FREEZE, bytes([0x00, 0x04, (dtc >> 8) & 0xFF, dtc & 0xFF]))
        except Refused:
            return []
        data = bytes(body[1:]) if body and body[0] == 0x00 else bytes(body)
        if len(data) >= 3 and data[-3] == 0x04 and ((data[-2] << 8) | data[-1]) == dtc:
            data = data[:-3]
        return _cells(data)
