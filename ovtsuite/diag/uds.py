# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Generic UDS (ISO 14229) for OVT Diag, over ISO 15765-2 (ISO-TP).

The newer control units -- the ZF 6HP28 and 8HP gearboxes, the C6 airbag,
and any module whose coding is the ``22 06 00`` long form -- speak UDS on
native ISO-TP CAN ids, not KW1281/KWP2000. This is the read/diagnose side
of that: the services OVT Diag needs, decoded against the module's
definitions (``dids`` / ``routines`` / ``coding`` / ``faults``):

* ``10`` diagnostic session, ``11`` ECU reset, ``3E`` tester present;
* ``22`` read data by identifier -- identification (``F1 87`` part, ``F1 89``
  software, ``F1 91`` hardware, ``F1 97`` system name, ``F1 A5`` the
  workshop field, ``06 00`` long coding) and any live DID;
* ``2E`` write data by identifier -- long coding and other writable DIDs;
* ``19`` read DTC information (sub-function ``02`` by status mask) and
  ``14`` clear;
* ``31`` routine control (``01`` start / ``02`` stop / ``03`` results);
* ``2F`` input/output control by identifier (actuator tests);
* ``27`` security access -- seed from ``27 <2n-1>``, key from a named
  algorithm (:mod:`.udskey`), sent with ``27 <2n>``; the key algorithm is
  the suite's own, never reimplemented here.

The transport is the suite's :class:`ovtsuite.flash.isotp_transport.IsoTp`
(or the 6HP28 read client's in-bridge handle), reached through a thin
:class:`UdsLink`. The ``22``/``2E`` value codec follows the ``dids``
definition types and scale kinds; with no definition a value is shown raw.

Qt-free; driven from one worker thread.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..flash.transport import KLineError, KLineTimeout

Log = Callable[[str], None]

POS = 0x40
NR = 0x7F
NRC_PENDING = 0x78

SID_SESSION = 0x10
SID_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DID = 0x22
SID_SECURITY = 0x27
SID_COMM_CONTROL = 0x28
SID_WRITE_DID = 0x2E
SID_IO_CONTROL = 0x2F
SID_ROUTINE = 0x31
SID_TESTER_PRESENT = 0x3E
SID_DTC_SETTING = 0x85

SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

ROUTINE_START = 0x01
ROUTINE_STOP = 0x02
ROUTINE_RESULTS = 0x03

IO_RETURN = 0x00
IO_RESET = 0x01
IO_FREEZE = 0x02
IO_ADJUST = 0x03

#: identification DIDs (ISO 14229-1 / VAG F1xx), name per DID
IDENT_DIDS = {0xF187: "part", 0xF189: "sw", 0xF191: "hw_part", 0xF19E: "odx_name",
              0xF1A2: "odx_version", 0xF197: "system_name", 0xF1A3: "hw_version",
              0xF1A5: "wsc_field", 0xF190: "vin", 0xF18C: "serial", 0x0600: "coding"}
DID_CODING = 0x0600
DID_CODING_LEN = 0x0601

_NRC_NAMES = {0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
              0x13: "incorrectMessageLengthOrFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
              0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x31: "requestOutOfRange",
              0x33: "securityAccessDenied", 0x35: "invalidKey", 0x36: "exceededNumberOfAttempts",
              0x37: "requiredTimeDelayNotExpired", 0x70: "uploadDownloadNotAccepted", 0x78: "responsePending",
              0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession",
              0x81: "rpmTooHigh", 0x83: "engineIsRunning", 0x87: "shiftLeverNotInPark"}


def nrc_name(nrc: int) -> str:
    return _NRC_NAMES.get(nrc, "NRC 0x%02X" % nrc)


class Refused(KLineError):
    """A negative response; the NRC is kept."""

    def __init__(self, what: str, nrc: Optional[int] = None):
        super().__init__("%s refused%s" % (what, (" (0x%02X %s)" % (nrc, nrc_name(nrc))) if nrc is not None else ""))
        self.nrc = nrc


# -------------------------------------------------------------- DTC status
#: ISO 14229-1 Annex D status-of-DTC bits
DTC_STATUS_BITS = ("testFailed", "testFailedThisOp", "pendingDTC", "confirmedDTC",
                   "testNotCompletedSinceClear", "testFailedSinceClear",
                   "testNotCompletedThisOp", "warningIndicatorRequested")


def decode_dtc_status(status: int) -> Dict[str, Any]:
    flags = [name for i, name in enumerate(DTC_STATUS_BITS) if status & (1 << i)]
    words = []
    if status & 0x08:
        words.append("confirmed")
    elif status & 0x04:
        words.append("pending")
    if status & 0x01:
        words.append("present")
    elif status & 0x20:
        words.append("stored")
    if status & 0x80:
        words.append("warning lamp")
    return {"byte": status, "text": "0x%02X %s" % (status, ", ".join(words) if words else "passed"),
            "present": bool(status & 0x01), "confirmed": bool(status & 0x08), "pending": bool(status & 0x04),
            "flags": flags}


def decode_dtc_word(three: bytes) -> str:
    """A 3-byte UDS DTC -> the VAG 5-digit number is not in the bytes; UDS
    DTCs are shown as their 6-hex-digit ISO form (the standard P-code is the
    high byte's letter + digits when it is a 2-byte J1979 code zero-extended)."""
    a, b, c = three[0], three[1], three[2]
    return "%02X%02X%02X" % (a, b, c)


def dtc_to_sae(three: bytes) -> str:
    """UDS 3-byte DTC -> SAE ``P0299`` form (ISO 15031-6: the first two bytes
    are the J1979 code, the third a failure-type byte shown after a dash)."""
    from .obd import decode_dtc
    base = decode_dtc(bytes(three[:2]))
    return base if three[2] == 0 else "%s-%02X" % (base, three[2])


# ---------------------------------------------------------------- DID codec
def _signed(v: int, bits: int) -> int:
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def decode_did_value(data: bytes, spec: Optional[Dict[str, Any]]) -> Tuple[Any, str]:
    """``(value, text)`` for a DID's data bytes under its definition entry
    (``type`` + ``scale``). None spec: raw hex."""
    data = bytes(data)
    if not spec:
        return data.hex(" ").upper(), data.hex(" ").upper()
    t = str(spec.get("type") or "bytes")
    unit = str(spec.get("unit") or "")
    if t == "ascii":
        s = "".join(chr(c) for c in data if 0x20 <= c < 0x7F).strip()
        return s, s
    if t == "bcd":
        s = "".join("%02X" % b for b in data)
        return s, s
    if t == "bytes":
        return data.hex(" ").upper(), data.hex(" ").upper()
    width = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4}.get(t, len(data))
    raw = int.from_bytes(data[:width] or b"\0", "big")
    if t.startswith("s"):
        raw = _signed(raw, width * 8)
    value: Any = raw
    scale = spec.get("scale")
    if isinstance(scale, dict):
        kind = str(scale.get("kind") or "linear")
        if kind == "linear":
            value = raw * float(scale.get("factor", 1)) + float(scale.get("offset", 0))
            unit = str(scale.get("unit") or unit)
        elif kind == "table":
            tbl = scale.get("table") or {}
            txt = None
            for k in (raw, str(raw), "0x%X" % raw, "0x%02X" % raw, hex(raw)):
                if k in tbl:
                    txt = tbl[k]
                    break
            return (txt if txt is not None else raw), (str(txt) if txt is not None else str(raw))
        elif kind == "bitfield":
            bits = [name for i, name in (scale.get("bits") or {}).items() if raw & (1 << int(i))] \
                if isinstance(scale.get("bits"), dict) else [n for i, n in enumerate(scale.get("bits") or []) if raw & (1 << i)]
            return bits, (", ".join(bits) if bits else "none")
    if isinstance(value, float):
        txt = ("%.3f" % value).rstrip("0").rstrip(".")
    else:
        txt = str(value)
    return value, (txt + (" " + unit if unit else "")).strip()


def encode_did_value(value: Any, spec: Dict[str, Any], length: Optional[int] = None) -> bytes:
    """Bytes to write for a DID, from a typed value and its definition
    (the inverse of :func:`decode_did_value` for the writable types)."""
    t = str(spec.get("type") or "bytes")
    n = int(spec.get("length") or length or 0)
    if t == "ascii":
        b = str(value).encode("ascii", "replace")
        return b.ljust(n, b"\0") if n else b
    if t in ("bytes", "bcd"):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        b = bytes.fromhex(str(value).replace(" ", ""))
        return b.ljust(n, b"\0") if n else b
    width = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4}.get(t, n or 1)
    raw = int(value)
    scale = spec.get("scale")
    if isinstance(scale, dict) and str(scale.get("kind") or "linear") == "linear":
        raw = round((float(value) - float(scale.get("offset", 0))) / float(scale.get("factor", 1)))
    if raw < 0:
        raw += 1 << (width * 8)
    return int(raw).to_bytes(width, "big")


# --------------------------------------------------------------- results
@dataclass
class UdsDtc:
    code: str                     # ISO 6-hex-digit form
    sae: str                      # Pxxxx-nn form
    status: int
    status_info: Dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass
class DidReading:
    did: int
    name: str
    value: Any
    text: str
    raw: bytes


# --------------------------------------------------------------- transport
class UdsLink:
    """A request/response handle: ``request(payload) -> response`` over
    ISO-TP, riding out responsePending. Wraps the suite's UDS client so the
    in-bridge transport (one IPC per exchange) is used when present."""

    def __init__(self, io, owners=(), log: Optional[Log] = None):
        from ..flash.zf6hp_uds import Zf6hpUdsClient
        self.io = io
        self._owners = list(owners)
        self._client = Zf6hpUdsClient(io, log)
        self._log = log or (lambda s: None)
        self.is_stop = None

    def request(self, payload: bytes, timeout_ms: int = 2000, overall_ms: int = 20000) -> bytes:
        return self._client.request(bytes(payload), timeout_ms=timeout_ms, p2_star_ms=5000,
                                     overall_ms=overall_ms)

    def close(self):
        try:
            close = getattr(self.io, "close", None)
            if close:
                close()
        except Exception:
            pass
        for o in reversed(self._owners):
            # a J2534 / OpenPort channel ends with disconnect(), a device with close()
            fn = getattr(o, "close", None) or getattr(o, "disconnect", None)
            try:
                if fn is not None:
                    fn()
            except Exception:
                pass


# ---------------------------------------------------------------- the client
class UdsClient:
    """The services OVT Diag uses, decoded against a module's definitions.

    ``resolver``/``ident`` let it name DIDs, decode faults and codings from
    the pack; without them it still works, showing raw values. ``key_lookup``
    maps a security algorithm name to seed->key (default :mod:`.udskey`)."""

    def __init__(self, link: UdsLink, resolver=None, ident=None, log: Optional[Log] = None,
                 key_lookup: Optional[Callable[[str, bytes], Optional[bytes]]] = None):
        self.link = link
        self.resolver = resolver
        self.ident = ident
        self._log = log or (lambda s: None)
        if key_lookup is None:
            from .udskey import key_for
            key_lookup = key_for
        self._key_for = key_lookup
        self.session = SESSION_DEFAULT
        self._did_defs: Optional[Dict[int, Dict[str, Any]]] = None

    # ---- low level
    def _request(self, sid: int, body: bytes, what: str, overall_ms: int = 20000) -> bytes:
        resp = self.link.request(bytes([sid]) + bytes(body), overall_ms=overall_ms)
        if not resp:
            raise KLineTimeout("%s: no response" % what)
        if resp[0] == NR:
            raise Refused(what, resp[2] if len(resp) >= 3 else None)
        if resp[0] != (sid | POS):
            raise KLineError("%s: unexpected response 0x%02X" % (what, resp[0]))
        return bytes(resp[1:])

    # ---- definitions
    def _dids(self) -> Dict[int, Dict[str, Any]]:
        if self._did_defs is None:
            self._did_defs = {}
            if self.resolver is not None and self.ident is not None:
                from .defs.schema import as_int
                for r in self.resolver.view("dids", self.ident).entries.values():
                    did = as_int(r.entry.get("did"), None)
                    if did is not None:
                        self._did_defs[did] = r.entry
        return self._did_defs

    def did_spec(self, did: int) -> Optional[Dict[str, Any]]:
        return self._dids().get(did)

    # ---- session / presence
    def open_session(self, session: int = SESSION_EXTENDED) -> bytes:
        body = self._request(SID_SESSION, bytes([session]), "diagnostic session 0x%02X" % session)
        self.session = session
        return body

    def tester_present(self):
        """Keep the session alive: ``3E 00`` and its ``7E 00`` answer. (``3E 80``
        would suppress the answer, and the request path reads that silence as
        a dead link -- which is how a session used to end a second after it
        opened.)"""
        self._request(SID_TESTER_PRESENT, b"\x00", "tester present", overall_ms=1500)

    def ecu_reset(self, kind: int = 0x01):
        return self._request(SID_RESET, bytes([kind]), "ECU reset 0x%02X" % kind)

    # ---- identification
    def read_did_raw(self, did: int) -> bytes:
        body = self._request(SID_READ_DID, bytes([(did >> 8) & 0xFF, did & 0xFF]), "read DID 0x%04X" % did)
        if len(body) < 2 or (body[0] << 8 | body[1]) != did:
            raise KLineError("read DID 0x%04X: echo %s" % (did, body[:2].hex()))
        return bytes(body[2:])

    def read_did(self, did: int) -> DidReading:
        raw = self.read_did_raw(did)
        spec = self.did_spec(did)
        name = (spec.get("name") if spec else None) or IDENT_DIDS.get(did, "DID 0x%04X" % did)
        value, text = decode_did_value(raw, spec)
        return DidReading(did, name, value, text, raw)

    def identity(self) -> Dict[str, Any]:
        """The identification DIDs that answer, named."""
        out: Dict[str, Any] = {}
        for did, key in IDENT_DIDS.items():
            if did == DID_CODING:
                continue
            try:
                raw = self.read_did_raw(did)
            except Refused:
                continue
            if key == "wsc_field" and len(raw) == 6:
                from .kwp2000 import Wsc
                w = Wsc.decode(raw)
                out["wsc"], out["importer"], out["equipment"] = w.wsc, w.importer, w.equipment
                continue
            out[key] = "".join(chr(c) for c in raw if 0x20 <= c < 0x7F).strip() or raw.hex().upper()
        return out

    # ---- coding (long)
    def read_coding(self, did: int = DID_CODING) -> bytes:
        return self.read_did_raw(did)

    def write_coding(self, data: bytes, did: int = DID_CODING) -> bytes:
        return self.write_did_raw(did, bytes(data))

    def write_did_raw(self, did: int, data: bytes) -> bytes:
        body = self._request(SID_WRITE_DID, bytes([(did >> 8) & 0xFF, did & 0xFF]) + bytes(data),
                             "write DID 0x%04X" % did)
        return body

    # ---- live DIDs
    def read_group(self, dids: List[int]) -> List[DidReading]:
        return [self.read_did(d) for d in dids]

    # ---- faults
    def read_dtcs(self, status_mask: int = 0xFF) -> List[UdsDtc]:
        """``19 02 <mask>``: stored DTCs with their status."""
        body = self._request(SID_READ_DTC, bytes([0x02, status_mask]), "read DTC information")
        # 59 02 <statusAvailabilityMask> then records of <dtc:3> <status:1>
        data = body[2:] if len(body) >= 2 else b""
        out = []
        for i in range(0, len(data) - 3, 4):
            three, st = data[i:i + 3], data[i + 3]  # <dtc:3><status:1>
            if three == b"\x00\x00\x00":
                continue
            code = decode_dtc_word(three)
            sae = dtc_to_sae(three)
            text = self._fault_text(three, sae)
            out.append(UdsDtc(code, sae, st, decode_dtc_status(st), text))
        return out

    def _fault_text(self, three: bytes, sae: str) -> str:
        if self.resolver is not None and self.ident is not None:
            r = self.resolver.fault(self.ident, sae=sae.split("-")[0] or None)
            if r is not None:
                return str(r.entry.get("text", ""))
        try:
            from .. import dtc as _dtc
            return _dtc._load_pdesc().get(sae.split("-")[0], "")
        except Exception:
            return ""

    def clear_dtcs(self, group: int = 0xFFFFFF):
        self._request(SID_CLEAR_DTC, bytes([(group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF]),
                      "clear DTCs")

    # ---- routines
    def routine(self, rid: int, data: bytes = b"", sub: int = ROUTINE_START, overall_ms: int = 30000) -> bytes:
        body = self._request(SID_ROUTINE, bytes([sub, (rid >> 8) & 0xFF, rid & 0xFF]) + bytes(data),
                             "routine 0x%04X sub 0x%02X" % (rid, sub), overall_ms=overall_ms)
        return body[3:] if len(body) >= 3 else body

    def routine_results(self, rid: int) -> bytes:
        return self.routine(rid, sub=ROUTINE_RESULTS)

    # ---- input/output control (actuator tests)
    def io_control(self, did: int, mode: int, state: bytes = b"") -> bytes:
        body = self._request(SID_IO_CONTROL, bytes([(did >> 8) & 0xFF, did & 0xFF, mode]) + bytes(state),
                             "io control 0x%04X" % did)
        return body[3:] if len(body) >= 3 else body

    def io_return(self, did: int):
        return self.io_control(did, IO_RETURN)

    # ---- security access
    def security_access(self, algorithm: str, level: int = 3) -> bool:
        """Seed from ``27 <level>``, key from the named algorithm, sent with
        ``27 <level+1>``. An all-zero seed = already unlocked (True). An
        unknown algorithm name is reported, not guessed."""
        seed = self._request(SID_SECURITY, bytes([level]), "security access seed (level %d)" % level)[1:]
        if not seed or not any(seed):
            return True
        key = self._key_for(algorithm, bytes(seed))
        if key is None:
            raise Refused("security access: no key algorithm %r in OVT Diag" % algorithm)
        try:
            self._request(SID_SECURITY, bytes([level + 1]) + bytes(key), "security access key (level %d)" % (level + 1))
        except Refused as ex:
            if ex.nrc in (0x35, 0x36):
                self._log("Security access denied (%s)" % ex)
                return False
            raise
        return True


__all__ = ["UdsClient", "UdsLink", "UdsDtc", "DidReading", "Refused", "decode_dtc_status", "decode_did_value",
           "encode_did_value", "IDENT_DIDS", "DID_CODING", "SESSION_EXTENDED", "nrc_name"]
