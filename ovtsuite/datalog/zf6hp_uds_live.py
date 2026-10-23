# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Live data from a ZF 6HP28 (GS19, 8K0927158 / 8K0997749 / 8R0927158) over UDS.

The 6HP28 does **not** serve the ``readDataByLocalIdentifier (0x21)`` measuring
blocks the 6HP19 logger reads, and it does not answer TP 2.0 at all: in the
car's own diagnostic session (``Reference/Logs/VCDS-UDS-Trans.log``) ten TP 2.0
channel requests to the transmission (0x02) go unanswered and the tester then
talks UDS over ISO-TP, **0x7E1 -> 0x7E9, routed by the gateway** -- the same
route the flasher uses (``flash.zf6hp_uds``).  This module uses that transport:
in-bridge ISO-TP on the 32-bit bridge, ISO15765 on a direct device (raw CAN +
software ISO-TP as the fallback), with the functional gateway wake first.

Live values are ``readDataByIdentifier (0x22)`` DIDs, listed in the
application's own DID table (8K0927158J: 0x46B20, 164 records of 12 bytes; the
same table, with the same widths, in the C, 8K0997749/A and 8R0927158H
software):

    [u16 DID low][u16 DID high][u8 flag][3 x 0][u32 descriptor or 0]

* flag 0 -- a **RAM DID**: the descriptor is ``[ffffffff][count<<24][type]
  [RAM address]`` and the response is ``count`` elements of ``type`` (1 = u8,
  2 = u16 big-endian; 3 / 6 are strings and records, not logged).
* flag 0x14 -- a **code-built DID** (no descriptor).  Unless named below, the
  whole response is one raw cell.
* flag 0x18 -- the **F4xx** DIDs: OBD mode-01 PIDs served as UDS DIDs, scaled
  per SAE J1979.

``NAMED`` maps the DIDs the car capture identifies (see its docstring).

Each DID is presented to the logger as its own *group* (group number = DID),
with one cell per element, so channels round-trip through the log file as
``G<DID>/F<n>``.
"""
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..flash import diag as _diag
from ..flash.diag import KLineError, KLineTimeout
from .logfile import Channel

SESSION_EXTENDED = 0x03
#: NRCs meaning "this DID is not served" (requestOutOfRange,
#: subFunction/serviceNotSupported): stop asking for it this connection.
NRC_UNSUPPORTED = (0x31, 0x11, 0x12)
#: DIDs per multi-DID ReadDataByIdentifier request (ISO 14229 allows several;
#: kept small so one response stays a short TP 2.0 transfer).
BATCH = 8

# ------------------------------------------------------------- the DID table
#: RAM DIDs that log as numbers: DID -> (count, type).  From the DID table's
#: descriptors (types 1/2 only; identification/F1xx and constants left out).
RAM_DIDS: Dict[int, Tuple[int, int]] = {
    0x0288: (1, 1), 0x0301: (1, 1), 0x0407: (4, 2), 0x0408: (4, 2),
    0x0801: (1, 2), 0x0802: (1, 2), 0x0803: (1, 2), 0x0804: (1, 2),
    0x1001: (1, 2), 0x1002: (1, 2), 0x1003: (1, 2), 0x1004: (1, 2),
    0x1005: (1, 2), 0x1006: (1, 2), 0x1008: (1, 1), 0x1009: (1, 1),
    0x100A: (1, 1), 0x100B: (1, 1), 0x100C: (1, 1), 0x100D: (1, 1),
    0x100E: (1, 1), 0x1016: (1, 2), 0x1017: (1, 2), 0x1018: (1, 2),
    0x1019: (1, 1), 0x101A: (1, 2), 0x101B: (1, 2), 0x101C: (1, 2),
    0x101D: (1, 2), 0x101E: (1, 1), 0x101F: (1, 2), 0x1020: (1, 1),
    0x1021: (1, 2), 0x1022: (1, 2), 0x1023: (1, 1), 0x1024: (1, 2),
    0x1025: (1, 2), 0x1026: (1, 2), 0x1027: (1, 2), 0x1028: (1, 2),
    0x1029: (1, 2), 0x102A: (1, 2), 0x102B: (1, 1), 0x102C: (1, 1),
    0x102D: (1, 1), 0x102E: (1, 1), 0x102F: (1, 1), 0x1030: (1, 1),
    0x1031: (1, 1), 0x1032: (1, 1), 0x1033: (1, 1), 0x1034: (1, 2),
    0x2002: (1, 2), 0x2096: (1, 1), 0x2104: (1, 1), 0x210D: (1, 2),
    0x2110: (1, 2), 0x2111: (1, 2), 0x2112: (1, 1), 0x2113: (1, 1),
    0x2114: (1, 1), 0x2119: (1, 1), 0x211A: (1, 2), 0x212F: (1, 2),
    0x2153: (4, 1), 0x218D: (1, 1),
}

#: What the firmware's own writers show about a RAM DID -- structure, not a
#: name.  Shown in the group list so a raw column is not mistaken for a sensor.
RAM_NOTES: Dict[int, str] = {}
for _k in range(7):
    RAM_NOTES[0x1024 + _k] = "rolling history, slot %d of 7" % (_k + 1)
for _d in (0x1001, 0x1002, 0x1003, 0x1004, 0x1005):
    RAM_NOTES[_d] = "12-bit packed fields"
for _d in (0x102D, 0x102E, 0x102F, 0x1030, 0x1031, 0x1032):
    RAM_NOTES[_d] = "flag byte"
RAM_NOTES[0x1033] = RAM_NOTES[0x1034] = "counter"
RAM_NOTES[0x101D] = "same variable as 0x2110"
RAM_NOTES[0x2110] = "same variable as 0x101D"
RAM_NOTES[0x101F] = "same variable as 0x211A"
RAM_NOTES[0x211A] = "same variable as 0x101F"

#: Code-built DIDs (flag 0x14) that are not identification: one raw cell each.
CODE_DIDS: List[int] = [
    0x0100, 0x0102, 0x0285, 0x0302, 0x0303, 0x0304, 0x0305, 0x0405, 0x040F,
    0x0805, 0x1000, 0x1007, 0x100F, 0x1010, 0x1011, 0x1012, 0x1013, 0x1014,
    0x1015, 0x1035, 0x1036, 0x12FC, 0x2102, 0x2103, 0x2105, 0x2106, 0x2108,
    0x210B, 0x210C, 0x210E, 0x210F, 0x2115, 0x2116, 0x2117, 0x2118, 0x211B,
    0x211C, 0x211D, 0x211E, 0x211F, 0x2120, 0x2121, 0x2122, 0x2123, 0x2124,
    0x2125, 0x2126, 0x2127, 0x2128, 0x2129, 0x212A, 0x212B, 0x212C, 0x212D,
    0x212E, 0x2130, 0x2131,
]


def _pct(b):
    return b[0] * 100.0 / 255.0


#: F4xx DIDs: DID -> (name, unit, data bytes, decode).  SAE J1979 PID data.
OBD_DIDS: Dict[int, Tuple[str, str, int, Callable[[bytes], float]]] = {
    0xF40C: ("Engine speed", "rpm", 2, lambda b: ((b[0] << 8) | b[1]) / 4.0),
    0xF40D: ("Vehicle speed", "km/h", 1, lambda b: float(b[0])),
    0xF405: ("Coolant temperature", "°C", 1, lambda b: b[0] - 40.0),
    0xF404: ("Calculated load", "%", 1, _pct),
    0xF443: ("Absolute load", "%", 2, lambda b: ((b[0] << 8) | b[1]) * 100.0 / 255.0),
    0xF411: ("Throttle position", "%", 1, _pct),
    0xF449: ("Accelerator pedal D", "%", 1, _pct),
    0xF442: ("Control module voltage", "V", 2, lambda b: ((b[0] << 8) | b[1]) / 1000.0),
    0xF430: ("Warm-ups since codes cleared", "", 1, lambda b: float(b[0])),
    0xF431: ("Distance since codes cleared", "km", 2, lambda b: float((b[0] << 8) | b[1])),
}

#: DIDs identified from the car's own diagnostic session
#: (Reference/Logs/VCDS-UDS-Trans.log, 2026-10-06): the tester adds one DID to
#: its poll loop per measuring value ticked, in list order, so the final
#: 12-request cycle lines up one-to-one with the 12 values ticked.  Lengths are
#: the response lengths on the car (and agree with the DID table where it fixes
#: them: 0288 and 2104 are u8 RAM DIDs).  Wording is ours.
#:   DID -> (name, unit, scale, data bytes); physical = raw * scale.  scale None
#:   = scaling not established yet, so the raw count is logged and labelled so.
#: 0285's 0.1 V/count is from the engine-off sample (0x77 = 11.9 V against
#: F442's 11.93 V at the same moment).  0805 is one status byte the tester
#: shows as several brake-signal flags; which bit is which is not established.
NAMED: Dict[int, Tuple[str, str, Optional[float], int]] = {
    0x2102: ("Transmission input speed", "", None, 2),
    0x2103: ("Transmission output speed", "", None, 2),
    0x2106: ("Road speed (transmission)", "", None, 2),
    0x2104: ("ATF temperature", "", None, 1),
    0x0285: ("Ignition supply voltage (terminal 15)", "V", 0.1, 1),
    0x0288: ("Ignition (terminal 15) on", "", 1.0, 1),
    0x0805: ("Brake signal flags", "", None, 1),
}
#: F442 is the module's battery supply (terminal 30) on this controller.
OBD_DIDS[0xF442] = ("Battery supply voltage (terminal 30)",) + OBD_DIDS[0xF442][1:]

#: Browse / log order: the identified signals first, then the other physical
#: anchors, then RAM, then the remaining code-built DIDs.
_FIRST = [0xF40C, 0x2102, 0x2103, 0x2106, 0x2104, 0xF405, 0x0285, 0xF442,
          0x0288, 0x0805]
DIDS: List[int] = list(dict.fromkeys(_FIRST + list(OBD_DIDS) + sorted(RAM_DIDS)
                                     + list(CODE_DIDS)))
_TYPE_NAME = {1: "u8", 2: "u16"}


def did_length(did: int) -> Optional[int]:
    """Response data length in bytes when it is known, else None."""
    if did in OBD_DIDS:
        return OBD_DIDS[did][2]
    if did in NAMED:
        return NAMED[did][3]
    if did in RAM_DIDS:
        n, t = RAM_DIDS[did]
        return n * t
    return None


# ------------------------------------------------------------------- one cell
class LiveCell:
    """One decoded value dressed as the ``diag.Cell`` the logger reads: the
    loop only ever touches ``.text`` and ``.value``."""
    __slots__ = ("value", "unit", "text")

    def __init__(self, value: Optional[float], unit: str = "", text: Optional[str] = None):
        self.value = value
        self.unit = unit
        if text is None:
            text = ("%g %s" % (value, unit) if unit else "%g" % value) if value is not None else "N/A"
        self.text = text


def decode_did(did: int, data: bytes) -> List[LiveCell]:
    """The cells of one DID's data (the bytes after the 2-byte DID echo)."""
    data = bytes(data)
    if did in OBD_DIDS:
        _name, unit, n, fn = OBD_DIDS[did]
        if len(data) < n:
            return [LiveCell(None)]
        return [LiveCell(round(fn(data[:n]), 3), unit)]
    if did in NAMED:
        _name, unit, scale, n = NAMED[did]
        if len(data) < n:
            return [LiveCell(None)]
        raw = int.from_bytes(data[:n], "big")
        if scale is None:
            return [LiveCell(float(raw))]
        return [LiveCell(round(raw * scale, 3), unit)]
    if did in RAM_DIDS:
        count, width = RAM_DIDS[did]
        out = []
        for i in range(count):
            b = data[i * width:(i + 1) * width]
            out.append(LiveCell(float(int.from_bytes(b, "big"))) if len(b) == width
                       else LiveCell(None))
        return out
    # code-built: the whole response, as hex; a number when it fits in 4 bytes
    if not data:
        return [LiveCell(None, text="(empty)")]
    val = float(int.from_bytes(data, "big")) if len(data) <= 4 else None
    return [LiveCell(val, text=data.hex(" ").upper())]


# ------------------------------------------------------------------ transport
class UdsLink:
    """A request/response handle on the transmission: UDS over ISO-TP 0x7E1 ->
    0x7E9 through the gateway, on whichever transport the device supports.
    ``request`` returns the final response (responsePending ridden out)."""

    def __init__(self, io, channel=None, kind=""):
        from ..flash import zf6hp_uds as _uds
        self.io = io
        self.channel = channel
        self.kind = kind
        self._client = _uds.Zf6hpUdsClient(io)

    def request(self, payload: bytes, timeout_ms: int = 1000) -> bytes:
        return self._client.request(bytes(payload), timeout_ms=timeout_ms,
                                    p2_star_ms=5000, overall_ms=10000)

    def close(self):
        for obj, fn in ((self.io, "close"), (self.channel, "disconnect")):
            f = getattr(obj, fn, None) if obj is not None else None
            if f is not None:
                try:
                    f()
                except Exception:
                    pass
        self.io = self.channel = None


def open_link(can, rate: int, prefer_iso15765: bool = True, log=None,
              is_stop=None) -> UdsLink:
    """Open the transmission's UDS route on an opened J2534 device ``can``,
    the way the flasher's 6HP28 read does: in-bridge ISO-TP on the 32-bit
    bridge; otherwise ISO15765 (the device frames ISO-TP) or raw CAN with
    software ISO-TP.  The gateway is woken with the functional testerPresent /
    default-session broadcast first, as it will not route 0x7E1 to the
    transmission on a cold bus."""
    from ..flash import j2534 as _j
    from ..flash import zf6hp_uds as _uds
    log = log or (lambda s: None)
    if isinstance(can, _j.BridgePassThru):
        try:
            io = can.open_iso_tp(rate, tx_id=_uds.REQ_ID, rx_id=_uds.RESP_ID,
                                 func_id=_uds.FUNC_ID, log=log, is_stop=is_stop)
        except Exception as ex:          # an old bridge build without iso_* ops
            log("In-bridge ISO-TP unavailable (%s); using the per-frame path." % ex)
        else:
            _uds.wake_gateway(io, log)
            return UdsLink(io, None, "in-bridge ISO-TP")
    ch, kind, _awake = _uds.open_6hp_transport(can, rate, prefer_iso15765=prefer_iso15765,
                                               log=log, is_stop=is_stop)
    if kind == "iso15765":
        return UdsLink(_uds.Iso15765Io(ch, log=log, is_stop=is_stop), ch, "ISO15765")
    io = _uds.IsoTp(ch, log=log, is_stop=is_stop)
    _uds.wake_gateway(io, log)
    return UdsLink(io, ch, "raw CAN")


# ---------------------------------------------------------------- the session
class Zf6hpUdsLiveSession:
    """The logger's session for the 6HP28: the surface ``gui_log``'s worker
    drives (``connect`` / ``read_group(s)`` / ``keepalive`` / ``reconnect`` /
    ``close`` / ``identity`` / ``baud``) over :class:`UdsLink`.

    ``opener(prefer_iso15765) -> UdsLink`` opens the route; the connect enters
    the extended session (``10 03``, as the car's own session does; the default
    session is kept if refused), reads F187/F191 and proves a DID answers.  An
    ISO15765 route that does not bring the session up is retried once on raw
    CAN, as the flasher does.  ``read_group(did)`` is ``22 <DID>``;
    ``read_groups`` packs DIDs of known length into multi-DID requests, falling
    back to one per request when the transmission will not."""
    #: read by gui_log._probe_memory to skip the readMemoryByAddress probe.
    supports_memory_read = False
    is_kwp2000 = False
    #: ChannelNamer reads this; no binary family is involved.
    family = None

    def __init__(self, opener: Callable[[bool], UdsLink], ecu="ZF6HP-UDS", log=None):
        self._opener = opener
        self.ecu = ecu
        self._log = log or (lambda s: None)
        self.link: Optional[UdsLink] = None
        self.identity = ""
        self.baud = 0
        self.session = 0x01
        self.unsupported = set()
        self._batch_ok: Optional[bool] = None
        self._is_stop = None

    @classmethod
    def over_can(cls, can, rate: int, log=None, ecu="ZF6HP-UDS"):
        """A session on an opened J2534 device (what the logger's worker uses)."""
        holder = {}

        def opener(prefer_iso):
            return open_link(can, rate, prefer_iso, log=log,
                             is_stop=lambda: bool(holder["s"]._is_stop and holder["s"]._is_stop()))
        holder["s"] = sess = cls(opener, ecu, log)
        return sess

    def log(self, msg):
        self._log(msg)

    def _check_stop(self, is_stop=None):
        is_stop = is_stop if is_stop is not None else self._is_stop
        if is_stop is not None and is_stop():
            raise _diag.StopRequested()

    # ---- requests ----
    def _req(self, payload: bytes, timeout_ms: int = 1000) -> bytes:
        if self.link is None:
            raise KLineError("not connected")
        resp = self.link.request(payload, timeout_ms=timeout_ms)
        if not resp:
            raise KLineTimeout("no response to 0x%02X" % payload[0])
        return resp

    @staticmethod
    def _nrc(resp: bytes, sid: int) -> Optional[int]:
        if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == sid:
            return resp[2]
        return None

    def _rdbi(self, did: int):
        """(data, nrc): one DID's data with the echo stripped, or its NRC."""
        resp = self._req(b"\x22" + did.to_bytes(2, "big"))
        nrc = self._nrc(resp, 0x22)
        if nrc is not None:
            return None, nrc
        if resp[:1] != b"\x62":
            raise KLineError("unexpected response %s to 22 %04X" % (resp[:3].hex(), did))
        body = resp[1:]
        if body[:2] == did.to_bytes(2, "big"):
            body = body[2:]
        return body, None

    # ---- connect ----
    def connect(self, speed="Low", cap_high=False, is_stop=None):
        self._is_stop = is_stop
        self.baud = 0
        self.close()
        self.link = self._opener(True)
        self.log("Transmission route: %s, 0x7E1 -> 0x7E9 through the gateway."
                 % self.link.kind)
        try:
            self._open_session()
        except KLineTimeout:
            if self.link.kind != "ISO15765":
                raise
            self._check_stop()
            self.log("ISO15765 brought no session up; retrying on raw CAN.")
            self.close()
            self.link = self._opener(False)
            self._open_session()
        self._check_stop()
        self.identity = self._read_identity() or "ZF 6HP28 (GS19) transmission"
        self.log("Transmission: %s" % self.identity)
        # Prove the DIDs answer before the dialog says "connected".
        for did in (0xF40C, 0xF405, 0xF442):
            self._check_stop()
            if self.read_group(did) is not None:
                break
        else:
            raise KLineError("the transmission answered no ReadDataByIdentifier "
                             "request (F40C, F405, F442)")
        self.baud = 500000
        return self.baud

    def _open_session(self):
        self._check_stop()
        resp = self._req(bytes([0x10, SESSION_EXTENDED]), timeout_ms=1500)
        nrc = self._nrc(resp, 0x10)
        if nrc is None and resp[:1] == b"\x50":
            self.session = SESSION_EXTENDED
        else:
            self.session = 0x01
            self.log("Extended session refused (NRC 0x%02X); reading in the default "
                     "session." % (nrc or 0))

    def reconnect(self, is_stop=None):
        return self.connect(is_stop=is_stop if is_stop is not None else self._is_stop)

    def _read_identity(self) -> str:
        """VW spare-part (F187) and hardware (F191) numbers, when served."""
        parts = []
        for did in (0xF187, 0xF191):
            self._check_stop()
            try:
                body, nrc = self._rdbi(did)
            except KLineError:
                continue
            if body:
                txt = bytes(body).decode("latin-1").strip("\x00 ").strip()
                if txt:
                    parts.append(txt)
        return " ".join(parts)

    # ---- live data ----
    def read_group(self, group: int) -> Optional[List[LiveCell]]:
        if group in self.unsupported:
            return None
        body, nrc = self._rdbi(group)
        if nrc is not None:
            if nrc in NRC_UNSUPPORTED:
                self.unsupported.add(group)
            return None
        return decode_did(group, body)

    def _read_batch(self, dids: List[int]) -> Optional[Dict[int, List[LiveCell]]]:
        """Several fixed-length DIDs in one request; None when the transmission
        will not answer multi-DID reads (the caller then reads them singly)."""
        resp = self._req(b"\x22" + b"".join(d.to_bytes(2, "big") for d in dids))
        nrc = self._nrc(resp, 0x22)
        if nrc == 0x31:
            return {}                           # none of them is served
        if nrc is not None or resp[:1] != b"\x62":
            return None                         # read them singly: per-DID NRCs
        body = resp[1:]
        out: Dict[int, List[LiveCell]] = {}
        i = 0
        while i + 2 <= len(body):
            did = int.from_bytes(body[i:i + 2], "big")
            n = did_length(did) if did in dids else None
            if n is None or i + 2 + n > len(body):
                return None                     # not a record walk we can trust
            out[did] = decode_did(did, body[i + 2:i + 2 + n])
            i += 2 + n
        if i != len(body):
            return None
        # A controller that only ever answers the first DID of a list is not
        # doing multi-DID reads at all: go one at a time rather than lose the rest.
        if len(dids) > 1 and list(out) == dids[:1]:
            return None
        return out

    def read_groups(self, groups: Sequence[int],
                    is_stop=None) -> Dict[int, Optional[List[LiveCell]]]:
        out: Dict[int, Optional[List[LiveCell]]] = {}
        want = [g for g in groups if g not in self.unsupported]
        fixed = [g for g in want if did_length(g) is not None]
        if self._batch_ok is not False and len(fixed) > 1:
            for i in range(0, len(fixed), BATCH):
                self._check_stop(is_stop)
                part = fixed[i:i + BATCH]
                res = self._read_batch(part)
                if res is None:
                    self._batch_ok = False
                    break
                self._batch_ok = True
                for g in part:
                    out[g] = res.get(g)
        for g in groups:
            if g in out:
                continue
            self._check_stop(is_stop)
            out[g] = self.read_group(g)
        return out

    def keepalive(self):
        # UDS TesterPresent, answered (3E 00), so a dead route shows up here.
        self._req(b"\x3e\x00")

    def read_memory(self, addr, length, addr_bytes=3):
        return None

    def close(self):
        if self.link is not None:
            self.link.close()
        self.link = None


# ------------------------------------------------------------------- the namer
class Blocks6hpUds:
    """Names the 6HP28 DIDs for the logger, the way ``Blocks6hp`` names the
    6HP19 groups -- the same small interface (``available`` / ``slots`` /
    ``resolve`` / ``channel`` / ``populated_groups`` / ``group_summary``) plus
    ``group_label`` for a hex DID in the group list."""
    session = None

    @property
    def available(self) -> bool:
        return True

    def slots(self, group: int) -> int:
        if group in RAM_DIDS and group not in NAMED:
            return RAM_DIDS[group][0]
        return 1 if (group in OBD_DIDS or group in NAMED or group in CODE_DIDS) else 0

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        key = "zf6hp28_%04x" % group
        if group in OBD_DIDS and field == 0:
            name, unit, _n, _fn = OBD_DIDS[group]
            return "%s (DID %04X)" % (name, group), key, unit
        if group in NAMED and field == 0:
            name, unit, scale, _n = NAMED[group]
            if scale is None:
                return "%s (raw, DID %04X)" % (name, group), key, ""
            return "%s (DID %04X)" % (name, group), key, unit
        if group in RAM_DIDS and 0 <= field < RAM_DIDS[group][0]:
            count, t = RAM_DIDS[group]
            idx = ("[%d]" % (field + 1)) if count > 1 else ""
            if count > 1:
                key += "_%d" % (field + 1)
            return "6HP28 DID %04X%s (%s)" % (group, idx, _TYPE_NAME.get(t, "raw")), key, ""
        if group in CODE_DIDS and field == 0:
            return "6HP28 DID %04X (raw)" % group, key, ""
        return ("DID %04X / Cell %d" % (group, field + 1), "", "")

    def channel(self, group: int, field: int) -> Channel:
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def populated_groups(self) -> List[int]:
        return list(DIDS)

    def group_summary(self, group: int) -> str:
        if group in OBD_DIDS:
            return OBD_DIDS[group][0]
        if group in NAMED:
            name, _u, scale, _n = NAMED[group]
            return name + (" (raw)" if scale is None else "")
        if group in RAM_DIDS:
            count, t = RAM_DIDS[group]
            kind = ("%d x %s" % (count, _TYPE_NAME[t])) if count > 1 else _TYPE_NAME[t]
            note = RAM_NOTES.get(group)
            return "RAM %s%s" % (kind, (", " + note) if note else "")
        if group in CODE_DIDS:
            return "computed, raw bytes"
        return ""

    def group_label(self, group: int) -> str:
        summ = self.group_summary(group)
        return ("%04X - %s" % (group, summ)) if summ else "DID %04X" % group
