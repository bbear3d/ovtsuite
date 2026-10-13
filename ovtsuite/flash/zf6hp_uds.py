# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only UDS probe for ZF 6HP28 (and other UDS-on-ISO-TP 6HP) TCUs.

Why this exists
---------------
The older 6HP-19/26 TCUs this project already flashes speak KWP2000 inside a
VW TP 2.0 channel (see :mod:`ovtsuite.flash.zf6hp` /
:mod:`ovtsuite.flash.tp20_transport`).  The newer 6HP28 TCU does NOT: its OEM
flash containers are UDS ``.frf`` bundles whose ODX names the flash job
``SinglJob_FlashJobUDS`` and reads identification with UDS
ReadDataByIdentifier (0x22), and the real control unit answers on native
ISO-TP CAN ids, not on the TP 2.0 setup id 0x200.  So the existing TP 2.0
read path can never connect to it -- it waits for a 0x200 setup reply the
6HP28 never sends.

This module adds the missing transport (ISO 15765-2, "ISO-TP") and a small
read-only UDS client on top of it, plus a :class:`Zf6hpUdsProbe` that walks a
fixed, non-destructive sequence and REPORTS what the TCU accepts:

  * default + extended diagnostic session,
  * identification (ReadDataByIdentifier of the usual VAG DIDs),
  * securityAccess requestSeed on the levels we have a key algorithm for,
    computing the key with BOTH the solved 6HP read-level formula
    (seed + 0x11C5A) and the solved write-level SA2 tape, so we learn whether
    the same seed/key carries over to UDS,
  * a tiny ReadMemoryByAddress read, and a RequestUpload (0x35) attempt,
    across a few candidate address/length formats.

It issues ONLY read-direction services: DiagnosticSessionControl (0x10),
TesterPresent (0x3E), ReadDataByIdentifier (0x22), SecurityAccess requestSeed
/ sendKey (0x27), ReadMemoryByAddress (0x23), RequestUpload (0x35) and the
TransferData (0x36) / RequestTransferExit (0x37) that pull an accepted upload.
It NEVER sends RequestDownload (0x34), WriteMemoryByAddress (0x3D), an erase
or any routine that changes state -- nothing here can modify the TCU.  Sending
a securityAccess key is itself harmless (a wrong key just increments the TCU's
retry counter); the probe sends at most one key per level to avoid tripping a
lockout, and skips sendKey entirely in ``seed_only`` mode.

Transport facts came from a real VCDS ISO-TP capture of a UDS airbag module on
the same car (Reference/Logs/VCDS-Autoscan.log, ids 0x715/0x77F): single-frame
/ first-frame / consecutive-frame framing, the tester flow-control frame
``30 00 01``, 0x55 padding to a full 8-byte CAN frame, and the ``7F xx 78``
(requestCorrectlyReceived-ResponsePending) busy reply that must be waited out
rather than treated as an error.  The 6HP28's own request/response ids
(0x7E1 / 0x7E9, functional 0x7DF) are the standard VAG powertrain pair and are
the defaults here, overridable once a real 6HP28 sniff confirms them.
"""
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    from .transport import KLineError, KLineTimeout
except Exception:                       # pragma: no cover - standalone import
    class KLineError(Exception):
        pass

    class KLineTimeout(KLineError):
        pass

try:
    from .crypto import (zf6hp_read_key, zf6hp_write_key,
                         zf6hp_bcb_encode, zf6hp_bcb_additive_checksum)
except Exception:                       # pragma: no cover - standalone import
    from crypto import (zf6hp_read_key, zf6hp_write_key,
                        zf6hp_bcb_encode, zf6hp_bcb_additive_checksum)


# The ISO-TP framing engine (and its ids / PCI constants / _precise_wait) now
# lives in its own minimal-dependency module so the 32-bit J2534 bridge can run
# it IN-PROCESS next to the DLL -- one IPC per chunk instead of ~35 per-frame
# round-trips (see isotp_transport / j2534_bridge iso_* ops). Re-exported here
# unchanged so every existing ``zf6hp_uds.IsoTp`` / ``REQ_ID`` caller still works.
from .isotp_transport import (            # noqa: F401  (public re-exports)
    IsoTp, REQ_ID, RESP_ID, FUNC_ID, PAD, _precise_wait,
    _PCI_SF, _PCI_FF, _PCI_CF, _PCI_FC)

# ---- UDS ----
POS = 0x40              # positive-response offset added to the SID
NR = 0x7F               # negative response marker
NRC_RESPONSE_PENDING = 0x78
SID_SESSION = 0x10
SID_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DATA = 0x22
SID_WRITE_DATA = 0x2E       # writeDataByIdentifier (fingerprint F15A)
SID_READ_MEM = 0x23
SID_SECURITY = 0x27
SID_TESTER_PRESENT = 0x3E
SID_REQUEST_UPLOAD = 0x35
SID_TRANSFER_DATA = 0x36
SID_TRANSFER_EXIT = 0x37
SID_ROUTINE = 0x31          # routineControl
SID_REQUEST_DOWNLOAD = 0x34
SID_DTC_SETTING = 0x85
SID_COMM_CONTROL = 0x28
ROUTINE_START = 0x01
ROUTINE_STOP = 0x02
ROUTINE_RESULTS = 0x03
# routineControl identifiers for the VAG UDS flash sequence, taken from the
# generic VAG choreography (VW_Flash lib/flash_uds.py) and the 6HP28 ODX:
#   0x0203  checkProgrammingPreconditions  -- read-only gate, no operand
#   0xFF01  checkProgrammingDependencies   -- read-only gate, no operand
#   0x0202  checksum/verify                -- checksums a RESIDENT block and
#           compares it to a supplied value; reads flash, writes nothing.
#           Data form: 01 <block_id> 00 04 <expected-checksum:4>.
#   0xFF00  eraseMemory                    -- STATE-CHANGING: never issued here.
# The write-path verify issues only the three read-only/verify routines; it
# does not erase, request a download, or transfer data.
ROUTINE_CHECK_PRECONDITIONS = 0x0203
ROUTINE_CHECK_DEPENDENCIES = 0xFF01
ROUTINE_CHECKSUM = 0x0202
ROUTINE_ERASE = 0xFF00          # documented only -- the probe never issues it
# Per-block logical identifiers, taken structurally from the 6HP28 FRF ODX
# (each DATABLOCK SEGMENT SOURCE-START-ADDRESS = 01/02/03/04 for DB_1..DB_4).
# In the VAG model erase/requestDownload/checksum address a block by this
# 1-byte id, not by a physical flash address. Cross-verified against a DQ250
# DSG FRF (0D9300011L): its ODX SOURCE-START-ADDRESS values 30/50/51 match
# VW_Flash's hardcoded dq250mqb block_identifiers {2:0x30,3:0x50,4:0x51}
# exactly -- confirming SOURCE-START-ADDRESS IS the UDS block identifier.
BLOCK_IDS = {
    0x01: "DB_1 program",
    0x02: "DB_2",
    0x03: "DB_3",
    0x04: "DB_4 calibration",
}
CAL_BASE = 0x180000      # DB_4 calibration flash base (frf.image layout)
CAL_LEN = 0x7FF80        # DB_4 length (524160)
DOWNLOAD_FORMAT = 0x11   # dfi = ODX ENCRYPT-COMPRESS-METHOD "11" (compress 1, encrypt 1)

SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

_NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported", 0x13: "incorrectMessageLengthOrFormat",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x35: "invalidKey", 0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


def nrc_name(code: int) -> str:
    return _NRC_NAMES.get(code, "0x%02X" % code)


# ISO15765 receive status bit: a frame flagged START_OF_MESSAGE is the DLL's
# "first frame arrived" indication, not the reassembled payload -- skip it.
_ISO15765_START_OF_MESSAGE = 0x00000002


class Iso15765Io:
    """Message-level transport presenting the SAME surface as :class:`IsoTp`
    (``send`` / ``recv`` / ``functional_scan`` / ``pass_ids`` / ids / ``can``),
    but backed by a J2534 or OpenPort channel connected in **ISO15765** mode.

    In ISO15765 mode the device firmware / DLL does ISO-TP segmentation, flow
    control and reassembly, so one ``write`` + one ``read`` moves a whole UDS
    message -- no per-frame work and, crucially on the 32-bit bridge, no
    per-frame IPC round-trip (the dominant cost of the raw-CAN reader). This is
    exactly how the OEM tool (Scanmatik) reads the 6HP28: PassThruConnect
    ISO15765, a flow-control filter, then one WriteMsgs + one ReadMsgs per
    chunk. Because it mirrors IsoTp's interface it is a drop-in for
    :class:`Zf6hpUdsReader` / :class:`Zf6hpUdsClient`, so every bit of the read
    choreography and the resend/flush recovery runs unchanged over it.

    The flow-control filter (responder id -> our request id) must already be set
    on ``chan`` by the caller that connected it (see
    :func:`connect_iso15765`)."""

    def __init__(self, chan, tx_id=REQ_ID, rx_id=RESP_ID, func_id=FUNC_ID,
                 log=None, is_stop=None):
        self.can = chan
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.func_id = func_id
        self._log = log or (lambda s: None)
        self.is_stop = is_stop
        self._pending = []              # unused (the DLL reassembles) -- interface parity

    # ---- id filtering: the ISO15765 flow-control filter is set at connect ----
    def pass_ids(self):
        return

    def _flush(self):
        """CLEAR_RX + CLEAR_TX, the way the OEM tool does before every request,
        so a stale/late reassembled reply can't be mistaken for this one."""
        self._pending = []
        for fn in ("clear_rx", "clear_tx"):
            f = getattr(self.can, fn, None)
            if f is not None:
                try:
                    f()
                except Exception:
                    pass

    # ---- message io (one write / one read; the DLL frames it) ----
    def send(self, payload, can_id=None):
        self._flush()
        cid = self.tx_id if can_id is None else can_id
        self.can.write(cid.to_bytes(4, "big") + bytes(payload), 0, 100)

    def recv(self, timeout_ms=2000):
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            # Serve one reassembled message from the buffer first. A single read
            # can return several messages (e.g. a responsePending then the final
            # reply); keep the rest instead of dropping them.
            while self._pending:
                m = self._pending.pop(0)
                if getattr(m, "rx_status", 0) & _ISO15765_START_OF_MESSAGE:
                    continue            # first-frame indication, not the payload
                if m.can_id is not None and m.can_id != self.rx_id:
                    continue
                data = bytes(m.payload)
                if data:
                    return data
            if self.is_stop is not None and self.is_stop():
                raise KLineError("ISO15765: aborted (stop requested)")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KLineTimeout("ISO15765: no response from TCU")
            self._pending.extend(self.can.read(8, max(1, int(remaining * 1000))))

    def functional_scan(self, request, listen_ms=500, func_id=None):
        """Functional (0x7DF) wake: single-frame request, collect replies for
        ``listen_ms`` keyed by responder id. The DLL delivers the reassembled
        response for whichever ids have a flow-control filter (we set 0x7E9), so
        on a 6HP28 this surfaces the TCU's answer -- enough to confirm routing."""
        self._flush()
        fid = self.func_id if func_id is None else func_id
        try:
            self.can.write(fid.to_bytes(4, "big") + bytes(request), 0, 100)
        except Exception as ex:          # pragma: no cover - hardware dependent
            self._log("ISO15765 functional send failed: %s" % ex)
            return {}
        done = {}
        deadline = time.monotonic() + listen_ms / 1000.0
        while time.monotonic() < deadline:
            if self.is_stop is not None and self.is_stop():
                break
            got = self.can.read(8, max(1, int((deadline - time.monotonic()) * 1000)))
            for m in got:
                if getattr(m, "rx_status", 0) & _ISO15765_START_OF_MESSAGE:
                    continue
                cid = m.can_id
                data = bytes(m.payload)
                if cid is not None and cid != fid and data:
                    done.setdefault(cid, data)
        return done


def connect_iso15765(can, rate, tx_id=REQ_ID, rx_id=RESP_ID, log=None):
    """Open an ISO15765 channel with the flow-control filter the DLL needs to
    auto-handle ISO-TP (reassemble responses from ``rx_id``, send flow control
    as ``tx_id``), and return an :class:`Iso15765Io` over it. Raises on a device
    that does not support ISO15765 (``ERR_INVALID_PROTOCOL_ID`` etc.) so the
    caller can fall back to raw CAN.

    Returns (io, channel)."""
    from . import j2534 as _j
    log = log or (lambda s: None)
    ch = can.connect(_j.ISO15765, rate)
    try:
        mask = b"\xff\xff\xff\xff"
        patt = rx_id.to_bytes(4, "big")
        flow = tx_id.to_bytes(4, "big")
        ch.add_filter(_j.FLOW_CONTROL_FILTER, mask, patt, 0, flow)
        try:
            ch.clear_rx()
        except Exception:
            pass
    except Exception:
        # Connected but couldn't set the flow-control filter: tear the channel
        # down so the caller can cleanly fall back to raw CAN on this device.
        try:
            ch.disconnect()
        except Exception:
            pass
        raise
    log("ISO15765 connected (flow-control filter 0x%03X<-0x%03X); the device "
        "handles ISO-TP." % (rx_id, tx_id))
    return Iso15765Io(ch, tx_id=tx_id, rx_id=rx_id, log=log), ch


def _raw_can_connect(can, rate):
    """Open a plain CAN channel (software ISO-TP) with promiscuous rx -- the
    proven transport, and the one that can broadcast the 0x7DF gateway wake."""
    from . import j2534 as _j
    ch = can.connect(_j.CAN, rate)
    for fn in ("pass_all", "clear_rx"):
        f = getattr(ch, fn, None)
        if f is not None:
            try:
                f()
            except Exception:
                pass
    return ch


def wake_gateway(io, log=None):
    """Open VAG gateway diagnostic routing with a FUNCTIONAL (0x7DF) broadcast
    testerPresent + default session, sent as real ISO-TP single frames. Returns
    True if any module answered the functional 10 01 (routing is open). Best
    effort: never raises.

    Must be driven over a RAW-CAN :class:`IsoTp`, not an ISO15765 channel: in
    ISO15765 mode the device only applies ISO-TP framing / receive-routing to
    ids that have a flow-control filter, and the functional id 0x7DF has none,
    so a 0x7DF broadcast sent over ISO15765 never opens gateway routing (the
    2026-10-08 car trace: functional wake over ISO15765 -> zero 0x7E9 -> connect
    failed). Over raw CAN we build the `02 3E 00` / `02 10 01` single frames
    ourselves, exactly as the working read did before the ISO15765 fast path."""
    log = log or (lambda s: None)
    try:
        io.functional_scan(bytes([SID_TESTER_PRESENT, 0x00]), listen_ms=300)
    except Exception:
        pass
    try:
        replies = io.functional_scan(bytes([SID_SESSION, 0x01]), listen_ms=800)
    except Exception as ex:                      # noqa: BLE001 - wake is best effort
        log("gateway wake: functional 10 01 failed (%s)" % ex)
        return False
    if replies:
        who = ", ".join("0x%03X" % cid for cid in sorted(replies))
        log("Gateway routing open (functional 10 01 answered by %s)." % who)
        return True
    log("No functional reply to the wake -- gateway/TCU may be asleep "
        "(ignition on?).")
    return False


def open_6hp_transport(can, rate, *, prefer_iso15765=True, log=None, is_stop=None):
    """Open the best UDS transport for a 6HP28 read/write and make sure the
    gateway is routing to the TCU first.

    Returns ``(channel, kind, assume_awake)``:
      * ``kind == "iso15765"`` -- the device/firmware does ISO-TP (one write +
        one read per request, like the OEM tool; no per-frame bridge IPC). The
        gateway is woken over a brief raw-CAN connect FIRST (see
        :func:`wake_gateway`), because an ISO15765 channel cannot be relied on
        to frame the 0x7DF wake broadcast.
      * ``kind == "raw"`` -- raw CAN + software ISO-TP (the proven path; the
        reader's own :meth:`Zf6hpUdsReader._functional_wake` opens routing, so
        no pre-wake here).
    ``assume_awake`` is True only on the ISO15765 path when the raw wake got a
    reply, telling the reader to skip a second (ineffective over ISO15765) wake
    and start the session while routing is still open on the gateway S3 timer.

    The caller taps ``channel`` and builds ``Iso15765Io(channel)`` (iso15765) or
    ``IsoTp(channel)`` (raw) over it. On any ISO15765 failure this falls back to
    raw CAN so a device without ISO15765 (or that rejects the filter) still
    works on the proven path."""
    log = log or (lambda s: None)
    if prefer_iso15765:
        awake = False
        try:
            raw = _raw_can_connect(can, rate)
            try:
                awake = wake_gateway(IsoTp(raw, log=log, is_stop=is_stop), log)
            finally:
                try:
                    raw.disconnect()
                except Exception:
                    pass
        except Exception as ex:                  # noqa: BLE001 - wake is best effort
            log("raw-CAN gateway wake skipped (%s); trying ISO15765 anyway." % ex)
        try:
            _io, ch = connect_iso15765(can, rate, log=log)
            return ch, "iso15765", awake
        except Exception as ex:                  # noqa: BLE001 - any failure -> raw fallback
            log("ISO15765 unavailable (%s); using raw CAN + software ISO-TP." % ex)
    return _raw_can_connect(can, rate), "raw", False


@dataclass
class ProbeStep:
    name: str
    request: bytes = b""
    response: bytes = b""
    ok: bool = False
    note: str = ""

    def line(self) -> str:
        req = self.request.hex(" ") if self.request else ""
        resp = self.response.hex(" ") if self.response else "<none>"
        mark = "OK " if self.ok else "-- "
        s = "%s%-34s" % (mark, self.name)
        if req:
            s += "  req %s" % req
        s += "  resp %s" % resp
        if self.note:
            s += "  (%s)" % self.note
        return s


@dataclass
class ProbeResult:
    steps: List[ProbeStep] = field(default_factory=list)
    identity: Dict[str, str] = field(default_factory=dict)
    memory: Dict[int, bytes] = field(default_factory=dict)   # addr -> bytes read
    connected: bool = False

    def report(self) -> str:
        out = ["ZF 6HP28 UDS probe -- read-only"]
        out.append("connected: %s" % ("yes" if self.connected else "NO"))
        if self.identity:
            out.append("")
            out.append("identification:")
            for did, val in self.identity.items():
                out.append("  %s  %s" % (did, val))
        out.append("")
        out.append("steps:")
        out.extend("  " + s.line() for s in self.steps)
        if self.memory:
            out.append("")
            out.append("memory read:")
            for addr, data in self.memory.items():
                out.append("  @0x%08X  %d bytes  %s%s" % (
                    addr, len(data), data[:16].hex(" "),
                    " ..." if len(data) > 16 else ""))
        return "\n".join(out)


# DIDs worth reading on a VAG TCU, label -> identifier
_DIDS = [
    ("F187 VW spare part no", 0xF187),
    ("F189 sw version", 0xF189),
    ("F191 hw part no", 0xF191),
    ("F190 VIN", 0xF190),
    ("F18C serial", 0xF18C),
    ("F197 system name", 0xF197),
    ("F1A2 ident", 0xF1A2),
    ("F1A3 hw version", 0xF1A3),
    ("0600 coding", 0x0600),
    ("0611 ASAM data", 0x0611),
]

# security levels to try: (requestSeedSub, sendKeySub, label)
_SEC_LEVELS = [
    (0x01, 0x02, "level 01/02 (write on TP2.0)"),
    (0x03, 0x04, "level 03/04 (read on TP2.0)"),
    (0x11, 0x12, "level 11/12 (UDS programming)"),
    (0x05, 0x06, "level 05/06"),
]

# ReadMemoryByAddress address/length format ids to try (ALFID: hi nibble =
# size bytes, lo nibble = address bytes) with a candidate base address.
_READ_MEM_TRIES = [
    # (ALFID, address, length). Addresses are the 6HP28's real flash layout
    # learned from the FRF descriptors: calibration @0x180000, program @0x80000,
    # boot @0x0 (see ovtsuite.frf.image). ALFID hi nibble = size bytes, lo = addr
    # bytes.
    (0x14, 0x00180000, 16),   # calibration base (DB_4)
    (0x14, 0x00080000, 16),   # program base (DB_1)
    (0x14, 0x00000000, 16),   # boot / image base
    (0x24, 0x00180000, 16),   # 2 size bytes form, calibration base
    (0x13, 0x180000, 16),     # 3 addr bytes form, calibration base
]


@dataclass
class ScanHit:
    resp_id: int
    req_id: int
    payload: bytes
    ident: str = ""
    looks_like_tcu: bool = False

    def line(self) -> str:
        return ("  resp 0x%03X  req 0x%03X  %s%s"
                % (self.resp_id, self.req_id,
                   (self.ident or self.payload.hex(" ")),
                   "   <- looks like the 6HP TCU" if self.looks_like_tcu else ""))


@dataclass
class ScanResult:
    hits: List[ScanHit] = field(default_factory=list)

    def tcu(self) -> Optional[ScanHit]:
        for h in self.hits:
            if h.looks_like_tcu:
                return h
        return None

    def report(self) -> str:
        if not self.hits:
            return ("UDS functional scan -- NO modules answered on 0x7DF.\n"
                    "Either nothing is UDS on this bus, the gateway is not routing "
                    "diagnostics, or the car is asleep. (The older 6HP TCUs are TP2.0, "
                    "not UDS -- use the ZF6HP ECU type for those.)")
        out = ["UDS functional scan -- %d module(s) answered:" % len(self.hits)]
        out.extend(h.line() for h in self.hits)
        t = self.tcu()
        if t is not None:
            out.append("")
            out.append("Likely 6HP TCU: respond 0x%03X / request 0x%03X. Run the UDS "
                       "read probe with those ids." % (t.resp_id, t.req_id))
        else:
            out.append("")
            out.append("No responder self-identified as a 6HP TCU. Pick the one that is "
                       "the transmission from the part numbers above and set its "
                       "request id for the probe.")
        return "\n".join(out)


class Zf6hpUdsClient:
    """Minimal read-only UDS client over :class:`IsoTp`."""

    def __init__(self, isotp: IsoTp, log=None):
        self.io = isotp
        self._log = log or (lambda s: None)

    def request(self, payload: bytes, timeout_ms=2000, p2_star_ms=5000,
                overall_ms=20000, fail_fast_on_silence=True) -> bytes:
        """Send one request and return the final response, transparently riding
        out responsePending (NRC 0x78). ``timeout_ms`` bounds the FIRST answer;
        once the ECU has said responsePending at least once it is known alive
        and working, so each further wait uses ``p2_star_ms`` and the whole
        exchange is allowed up to ``overall_ms``. An operation that takes more
        (or less) time than expected -- a block erase, a whole-block checksum --
        is therefore driven by the ECU's own pending keepalives, never a fixed
        delay (the 6HP19 write lesson). With ``fail_fast_on_silence`` (default)
        a timeout BEFORE any responsePending is raised at once -- right for
        ordinary requests, so a dead bus never hangs. A long op whose ECU may go
        SILENT while it works (a block erase -- the OEM tool tolerates exactly
        that, see the bench captures) passes ``fail_fast_on_silence=False`` so
        silence is waited out to ``overall_ms`` instead of aborting."""
        # In-bridge transport (BridgeIsoTp): the whole exchange -- send,
        # reassemble, ride out responsePending -- runs next to the DLL in a
        # single IPC. Delegate to it so a 6HP28 read over the 32-bit bridge is
        # one IPC per chunk instead of ~35 per-frame round-trips.
        native = getattr(self.io, "request", None)
        if callable(native):
            return native(payload, timeout_ms=timeout_ms, p2_star_ms=p2_star_ms,
                          overall_ms=overall_ms,
                          fail_fast_on_silence=fail_fast_on_silence)
        self.io.send(payload)
        deadline = time.monotonic() + overall_ms / 1000.0
        pending_seen = False
        first = True
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise KLineTimeout("UDS: no final response within %d ms" % overall_ms)
            window = min(timeout_ms if first else p2_star_ms, remaining_ms)
            first = False
            try:
                resp = self.io.recv(window)
            except KLineTimeout:
                if pending_seen or not fail_fast_on_silence:
                    continue                 # keep waiting to the overall deadline
                raise                        # ordinary request on a silent bus -- fail fast
            if len(resp) >= 3 and resp[0] == NR and resp[2] == NRC_RESPONSE_PENDING:
                pending_seen = True
                self._log("  responsePending for 0x%02X; waiting" % resp[1])
                continue
            return resp

    def request_with_keepalive(self, payload: bytes, keepalive_interval_s=1.0,
                               resend_interval_s=3.5, poll_ms=500,
                               overall_ms=60000) -> bytes:
        """Issue a request whose ECU may go SILENT while it works (a block
        erase) and ride it out exactly as the OEM tool does on this TCU (bench
        capture, 2026-10-07): poll in short windows; while no answer comes, hold
        the session open with testerPresent 3E 80 (suppressPositiveResponse, so
        it adds no frames to read) every ``keepalive_interval_s`` and RE-SEND the
        request every ``resend_interval_s`` (flushing stale rx first), until the
        ECU answers -- or ``overall_ms`` elapses. responsePending (0x78) is still
        ridden out, and defers the next resend since the ECU is proven alive."""
        # In-bridge transport: run the whole keepalive/resend loop next to the
        # DLL in one IPC (same reason as request()), so a silent erase over the
        # 32-bit bridge isn't paced by per-poll IPC.
        native = getattr(self.io, "request_with_keepalive", None)
        if callable(native):
            return native(payload, keepalive_interval_s=keepalive_interval_s,
                          resend_interval_s=resend_interval_s, poll_ms=poll_ms,
                          overall_ms=overall_ms)
        tp = bytes([SID_TESTER_PRESENT, 0x80])          # suppressed keepalive

        def _flush():
            try:
                self.io.can.clear_rx()
            except Exception:
                pass

        self.io.send(payload)
        now = time.monotonic()
        deadline = now + overall_ms / 1000.0
        last_keepalive = now
        last_resend = now
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KLineTimeout("UDS: no response within %d ms (keepalive/resend)"
                                   % overall_ms)
            try:
                resp = self.io.recv(min(poll_ms, max(1, int(remaining * 1000))))
            except KLineTimeout:
                resp = b""
            if resp:
                if len(resp) >= 3 and resp[0] == NR and resp[2] == NRC_RESPONSE_PENDING:
                    self._log("  responsePending for 0x%02X; waiting" % resp[1])
                    last_resend = time.monotonic()       # alive & working -- defer resend
                    continue
                return resp
            t = time.monotonic()
            if t - last_keepalive >= keepalive_interval_s:
                try:
                    self.io.send(tp)
                except Exception:
                    pass
                last_keepalive = t
                self._log("  testerPresent keepalive (3E 80)")
            if resend_interval_s and (t - last_resend >= resend_interval_s):
                _flush()
                self.io.send(payload)
                last_resend = t
                self._log("  silent -- re-sent request (no response yet)")

    @staticmethod
    def is_positive(resp: bytes, sid: int) -> bool:
        return bool(resp) and resp[0] == (sid | POS)

    @staticmethod
    def nrc_of(resp: bytes) -> Optional[int]:
        if len(resp) >= 3 and resp[0] == NR:
            return resp[2]
        return None


class Zf6hpUdsProbe:
    """Walk a fixed, read-only sequence and report what the TCU accepts."""

    def __init__(self, isotp: IsoTp, on_stage=None, is_stop=None, seed_only=False,
                 include_extended=False, probe_write=False, block_checksums=None):
        self.io = isotp
        self.client = Zf6hpUdsClient(isotp, log=on_stage or (lambda s: None))
        self._stage = on_stage or (lambda s: None)
        self._is_stop = is_stop or (lambda: False)
        self.seed_only = seed_only
        # The extended session's security/memory behaviour is already captured
        # (read-unlock works; 0x23/0x35 answer notSupportedInActiveSession;
        # write levels answer subFunctionNotSupported). So by default the probe
        # goes straight to the programming session, where the open unknowns are,
        # and only falls back to re-probing extended if programming is refused.
        # Set include_extended=True to re-run the extended phase anyway.
        self.include_extended = include_extended
        # probe_write: after write-security unlocks in the programming session,
        # verify the WRITE path NON-DESTRUCTIVELY -- run the two read-only
        # programming gates (0x0203 / 0xFF01) and the per-block checksum/verify
        # routine (0x0202), which checksums the RESIDENT block and compares it
        # to a supplied value (reads flash, writes nothing). eraseMemory
        # (0xFF00), requestDownload (0x34) and transferData (0x36) are NEVER
        # sent. Off by default; opt-in and gated in the GUI.
        self.probe_write = probe_write
        # optional {block_id: expected_checksum_bytes} for the 0x0202 verify;
        # absent -> a 4-byte placeholder is used (the GS19 checksum algorithm is
        # not yet confirmed, so a mismatch NRC is an expected, safe result).
        self.block_checksums = block_checksums or {}
        self._write_unlocked = False
        self.result = ProbeResult()

    def _stop_check(self):
        if self._is_stop():
            raise KLineError("probe aborted (stop requested)")

    def _record(self, name, request=b"", response=b"", ok=False, note=""):
        step = ProbeStep(name, bytes(request), bytes(response), ok, note)
        self.result.steps.append(step)
        self._stage(step.line())
        return step

    def _verdict(self, resp, sid):
        if self.client.is_positive(resp, sid):
            return True, ""
        nrc = self.client.nrc_of(resp)
        return False, ("NRC %s" % nrc_name(nrc)) if nrc is not None else "unexpected reply"

    def run(self) -> ProbeResult:
        self.io.pass_ids()
        # 1. default session / presence
        self._tester_present()
        if not self._session(SESSION_DEFAULT, "diagnosticSession default"):
            # a TCU that won't even answer default session is almost certainly
            # not on these ids / not UDS -- say so and stop early
            self._stage("No answer to default session -- the TCU is not responding "
                        "on 0x%03X/0x%03X as UDS. Check the ids or whether it is TP2.0."
                        % (self.io.tx_id, self.io.rx_id))
            return self.result
        self.result.connected = True
        # 1b. read services in the DEFAULT session. On the 6HP28, 0x23
        #     readMemoryByAddress answers notSupportedInActiveSession (0x7F) in
        #     the extended session -- i.e. it exists but belongs elsewhere --
        #     and serviceNotSupported (0x11) in programming. The default session
        #     is the only one left to try, so probe it here. (Read-level
        #     security is only offered in the extended session, so this is an
        #     unsecured attempt: a securityAccessDenied (0x33) reply would be the
        #     interesting one -- it would mean a flash read IS possible with the
        #     already-solved read key.)
        self._probe_read_memory(" [default]")
        # 2. identification -- who we are talking to (extended session, as VCDS
        #    reads these; default usually suffices too).
        self._stop_check()
        self._session(SESSION_EXTENDED, "diagnosticSession extended")
        self._read_identification()
        # 3. OPTIONAL extended-session security/memory re-probe. Off by default:
        #    the extended session is already captured (read-unlock works; 0x23/
        #    0x35 -> notSupportedInActiveSession; write levels ->
        #    subFunctionNotSupported), so re-running it only re-collects known
        #    results. Enable with include_extended=True.
        if self.include_extended:
            self._stop_check()
            self._phase(" [ext]")
        # 4. programming session (10 02): the only place the open unknowns live
        #    -- write-level security and the memory services (0x23
        #    readMemoryByAddress / 0x34-0x37). Still read-only: this changes
        #    session and issues read / seed-key requests only -- no erase,
        #    download or write.
        self._stop_check()
        if self._session(SESSION_PROGRAMMING, "diagnosticSession programming (10 02)"):
            self._phase(" [prog]")
            if self.probe_write and self._write_unlocked:
                self._stop_check()
                self._probe_write_path(" [prog]")
            elif self.probe_write:
                self._stage("Write-path verify skipped: write security did not "
                            "unlock, so the programming gates / checksum verify "
                            "were not attempted.")
        elif not self.include_extended:
            # programming refused and we skipped extended: probe extended now so
            # the run still yields something rather than coming back empty.
            self._stage("Programming session refused -- probing the extended "
                        "session instead so this run isn't empty.")
            if self._session(SESSION_EXTENDED, "diagnosticSession extended (fallback)"):
                self._phase(" [ext]")
        else:
            self._stage("Programming session refused -- the write/flash-read "
                        "services could not be probed (they are not offered in "
                        "the default/extended session).")
        return self.result

    def _phase(self, suffix):
        """Security + memory-service probing within the currently-active
        session. ``suffix`` tags each step so the report says which session it
        was tried in."""
        self._stop_check()
        self._probe_security(suffix)
        self._stop_check()
        self._probe_read_memory(suffix)
        self._probe_request_upload(suffix)

    def scan(self) -> "ScanResult":
        """Functional-broadcast discovery: ask every UDS module on the bus to
        report its spare-part number (0x22 F187) and record which CAN id each
        answers on. Flags a responder as the likely 6HP TCU when it sits on the
        powertrain trans id (0x7E9) or its part number looks like a VAG gearbox
        controller. Read-only -- a single functional read, nothing else."""
        self._stage("Functional scan on 0x%03X (22 F187)..." % FUNC_ID)
        found = self.io.functional_scan(bytes([SID_READ_DATA, 0xF1, 0x87]))
        result = ScanResult()
        for resp_id in sorted(found):
            payload = found[resp_id]
            req_id = IsoTp.req_for_response(resp_id)
            ident = ""
            if len(payload) >= 3 and payload[0] == (SID_READ_DATA | POS):
                data = payload[3:]
                ascii_val = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                ident = "%s  [%s]" % (ascii_val, data.hex(" "))
            elif payload:
                ident = payload.hex(" ")
            # heuristics: 0x7E9 is the OBD powertrain trans response; a VAG
            # transmission part number is 0x..910xxx (e.g. 0B6910258), and the
            # system string carries GS19 / 6HP on these controllers.
            up = ident.upper()
            looks = (resp_id == 0x7E9
                     or "910" in up[:16]
                     or "GS19" in up or "6HP" in up or "0B6910258" in up)
            hit = ScanHit(resp_id, req_id, bytes(payload), ident, looks)
            result.hits.append(hit)
            self._stage(hit.line())
        return result

    def _probe_write_path(self, suffix=""):
        """NON-DESTRUCTIVE write-path verification (programming session, write
        security already unlocked). It walks the VAG flash sequence as far as
        it can WITHOUT changing anything: the reversible pre-flash ritual, the
        two read-only programming gates (checkProgrammingPreconditions 0x0203
        and checkProgrammingDependencies 0xFF01), and the per-block
        checksum/verify routine (0x0202) -- which asks the ECU to checksum the
        RESIDENT block and compare it to a supplied value, reading flash but
        writing none -- then restores the bus. eraseMemory (0xFF00),
        requestDownload (0x34) and transferData (0x36) -- the only
        state-changing services -- are NEVER sent."""
        self._stage("--- write-path verify (non-destructive: no erase, no "
                    "requestDownload, no transferData) ---")

        # (1) Pre-flash ritual: quiet the bus. Fully reversible; undone in (4).
        for lbl, req in (
            ("controlDTCSetting off (85 02)", bytes([SID_DTC_SETTING, 0x02])),
            ("communicationControl disableRxTx normal (28 03 01)",
             bytes([SID_COMM_CONTROL, 0x03, 0x01])),
        ):
            self._stop_check()
            self._simple_request(lbl, req, suffix)

        # (2) Read-only programming gates. Both take no operand and only report
        #     whether a flash MAY proceed -- they neither erase nor write.
        for rid in (ROUTINE_CHECK_PRECONDITIONS, ROUTINE_CHECK_DEPENDENCIES):
            self._stop_check()
            self._run_readonly_routine(rid, b"", suffix)

        # (2b) READ-after-preconditions retry. On earlier runs 0x23 / 0x35
        #      were refused BEFORE any precondition routine ran
        #      (serviceNotSupported[InActiveSession]), so the question was
        #      whether establishing the programming preconditions changes read
        #      availability. Retry them here, now that 0x0203/0xFF01 have run.
        #      Pure reads -- no state is changed. A flip off 0x7F/0x11 (to a
        #      positive, 0x33 securityAccessDenied, or 0x22 conditionsNotCorrect)
        #      would reopen the OBD read path.
        self._stop_check()
        self._stage("retrying read services now that the programming "
                    "preconditions have been established...")
        self._probe_read_memory(" [prog, after preconditions]")
        self._probe_request_upload(" [prog, after preconditions]")

        # (3) Per-block checksum/verify (0x0202), VAG data form
        #     `01 <block_id> 00 04 <expected:4>`. The ECU checksums the RESIDENT
        #     block and compares to <expected>: it reads flash, writes nothing.
        #     The GS19's checksum algorithm/region is not yet confirmed, so
        #     <expected> is a 4-byte placeholder unless block_checksums supplies
        #     one; a mismatch / requestSequenceError here is an expected, safe
        #     outcome that still proves the routine exists and how it is gated.
        for bid, bname in BLOCK_IDS.items():
            self._stop_check()
            expected = bytes(self.block_checksums.get(bid, b"\x00\x00\x00\x00"))
            data = bytes([0x01, bid, 0x00, 0x04]) + expected
            self._run_readonly_routine(
                ROUTINE_CHECKSUM, data, suffix,
                label_extra=" verify %s (block 0x%02X)" % (bname, bid))

        # (4) Restore the bus (undo step 1). Non-destructive housekeeping so the
        #     TCU is left exactly as found even if the session lingers.
        for lbl, req in (
            ("communicationControl enableRxTx normal (28 00 01)",
             bytes([SID_COMM_CONTROL, 0x00, 0x01])),
            ("controlDTCSetting on (85 01)", bytes([SID_DTC_SETTING, 0x01])),
        ):
            self._stop_check()
            self._simple_request(lbl, req, suffix)

    def _simple_request(self, lbl, req, suffix=""):
        """Issue a single request and record it, using the service's own SID
        for the positive-response check."""
        try:
            resp = self.client.request(req)
            ok, note = self._verdict(resp, req[0])
            self._record("%s%s" % (lbl, suffix), req, resp, ok, note)
        except KLineError as ex:
            self._record("%s%s" % (lbl, suffix), req, note=str(ex))

    def _run_readonly_routine(self, rid, data=b"", suffix="", label_extra=""):
        """routineControl START (0x31 01) for a read-only / verify routine,
        then read its result (0x31 03). eraseMemory (0xFF00) is refused here:
        the probe never issues a state-changing routine."""
        if rid == ROUTINE_ERASE:
            self._record("routineStart 0x%04X (BLOCKED -- erase never sent)%s"
                         % (rid, suffix), b"", note="eraseMemory suppressed")
            return
        start_req = bytes([SID_ROUTINE, ROUTINE_START,
                           (rid >> 8) & 0xFF, rid & 0xFF]) + bytes(data)
        name = "routineStart 0x%04X%s%s" % (rid, label_extra, suffix)
        try:
            resp = self.client.request(start_req)
        except KLineError as ex:
            self._record(name, start_req, note=str(ex))
            return
        ok, note = self._verdict(resp, SID_ROUTINE)
        self._record(name, start_req, resp, ok, note)
        if not ok:
            return
        res_req = bytes([SID_ROUTINE, ROUTINE_RESULTS,
                         (rid >> 8) & 0xFF, rid & 0xFF])
        rname = "routineResults 0x%04X%s%s" % (rid, label_extra, suffix)
        try:
            rresp = self.client.request(res_req)
            rok, rnote = self._verdict(rresp, SID_ROUTINE)
            self._record(rname, res_req, rresp, rok, rnote or "routine result")
        except KLineError as ex:
            self._record(rname, res_req, note=str(ex))

    def _tester_present(self):
        try:
            resp = self.client.request(bytes([SID_TESTER_PRESENT, 0x00]))
        except KLineError as ex:
            self._record("testerPresent", bytes([SID_TESTER_PRESENT, 0x00]), note=str(ex))
            return
        ok, note = self._verdict(resp, SID_TESTER_PRESENT)
        self._record("testerPresent", bytes([SID_TESTER_PRESENT, 0x00]), resp, ok, note)

    def _session(self, session: int, label: str) -> bool:
        req = bytes([SID_SESSION, session])
        try:
            resp = self.client.request(req)
        except KLineError as ex:
            self._record(label, req, note=str(ex))
            return False
        ok, note = self._verdict(resp, SID_SESSION)
        self._record(label, req, resp, ok, note)
        return ok

    def _read_identification(self):
        for label, did in _DIDS:
            self._stop_check()
            req = bytes([SID_READ_DATA, (did >> 8) & 0xFF, did & 0xFF])
            try:
                resp = self.client.request(req)
            except KLineError as ex:
                self._record("RDBI %s" % label, req, note=str(ex))
                continue
            ok, note = self._verdict(resp, SID_READ_DATA)
            if ok and len(resp) >= 3:
                payload = resp[3:]
                ascii_val = "".join(chr(b) if 32 <= b < 127 else "." for b in payload)
                note = ascii_val.strip(".") or payload.hex(" ")
                self.result.identity[label] = "%s  [%s]" % (ascii_val, payload.hex(" "))
            self._record("RDBI %s" % label, req, resp, ok, note)

    def _probe_security(self, suffix=""):
        for req_sub, key_sub, label in _SEC_LEVELS:
            self._stop_check()
            req = bytes([SID_SECURITY, req_sub])
            try:
                resp = self.client.request(req)
            except KLineError as ex:
                self._record("sec requestSeed %s%s" % (label, suffix), req, note=str(ex))
                continue
            ok, note = self._verdict(resp, SID_SECURITY)
            seed = resp[2:] if ok else b""
            if ok and seed and not any(seed):
                note = "already unlocked (zero seed)"
            self._record("sec requestSeed %s%s" % (label, suffix), req, resp, ok, note)
            if not ok or not seed or not any(seed) or self.seed_only:
                continue
            # try sending a key computed each known way, ONE per level to avoid
            # tripping the attempt counter -- read-level formula first for the
            # read levels, SA2 tape first for the write/programming levels
            if req_sub in (0x03, 0x04):
                candidates = [("read+0x11C5A", zf6hp_read_key)]
            else:
                candidates = [("SA2 tape", zf6hp_write_key)]
            for kname, fn in candidates:
                try:
                    key = fn(bytes(seed[:4]))
                except Exception as ex:
                    self._record("  key via %s%s" % (kname, suffix), note=str(ex))
                    continue
                kreq = bytes([SID_SECURITY, key_sub]) + bytes(key)
                try:
                    kresp = self.client.request(kreq)
                except KLineError as ex:
                    self._record("  sendKey via %s%s" % (kname, suffix), kreq, note=str(ex))
                    continue
                kok, knote = self._verdict(kresp, SID_SECURITY)
                if kok and req_sub not in (0x03, 0x04):
                    self._write_unlocked = True
                self._record("  sendKey via %s%s" % (kname, suffix), kreq, kresp, kok,
                             knote or ("UNLOCKED on %s" % label if kok else ""))

    def _probe_read_memory(self, suffix=""):
        for alfid, addr, size in _READ_MEM_TRIES:
            self._stop_check()
            addr_bytes = alfid & 0x0F
            size_bytes = (alfid >> 4) & 0x0F
            req = (bytes([SID_READ_MEM, alfid])
                   + addr.to_bytes(addr_bytes, "big")
                   + size.to_bytes(size_bytes, "big"))
            name = "readMemByAddr ALFID 0x%02X @0x%X%s" % (alfid, addr, suffix)
            try:
                resp = self.client.request(req)
            except KLineError as ex:
                self._record(name, req, note=str(ex))
                continue
            ok, note = self._verdict(resp, SID_READ_MEM)
            if ok and len(resp) > 1:
                data = resp[1:]
                self.result.memory[addr] = bytes(data)
                note = "%d bytes" % len(data)
            self._record(name, req, resp, ok, note)

    def _probe_request_upload(self, suffix=""):
        # UDS RequestUpload (0x35): ECU -> tester read-out. dataFormatId 0x00
        # (no compression/encryption), ALFID 0x44 (4-byte addr, 4-byte size).
        self._stop_check()
        addr, size = 0x80000000, 0x100
        req = (bytes([SID_REQUEST_UPLOAD, 0x00, 0x44])
               + addr.to_bytes(4, "big") + size.to_bytes(4, "big"))
        try:
            resp = self.client.request(req)
        except KLineError as ex:
            self._record("requestUpload @0x%X%s" % (addr, suffix), req, note=str(ex))
            return
        ok, note = self._verdict(resp, SID_REQUEST_UPLOAD)
        self._record("requestUpload @0x%X%s" % (addr, suffix), req, resp, ok, note)
        if not ok:
            return
        # positive: 0x75 lengthFormatId maxNumberOfBlockLength...
        # pull a couple of blocks via TransferData (0x36), then close (0x37).
        pulled = bytearray()
        block = 1
        for _ in range(4):
            self._stop_check()
            treq = bytes([SID_TRANSFER_DATA, block & 0xFF])
            try:
                tresp = self.client.request(treq)
            except KLineError as ex:
                self._record("  transferData block %d%s" % (block, suffix), treq, note=str(ex))
                break
            tok, tnote = self._verdict(tresp, SID_TRANSFER_DATA)
            self._record("  transferData block %d%s" % (block, suffix), treq, tresp, tok, tnote)
            if not tok:
                break
            pulled += tresp[2:]
            block += 1
        if pulled:
            self.result.memory[addr] = bytes(pulled)
        try:
            xresp = self.client.request(bytes([SID_TRANSFER_EXIT]))
            xok, xnote = self._verdict(xresp, SID_TRANSFER_EXIT)
            self._record("  requestTransferExit%s" % suffix, bytes([SID_TRANSFER_EXIT]), xresp, xok, xnote)
        except KLineError as ex:
            self._record("  requestTransferExit%s" % suffix, bytes([SID_TRANSFER_EXIT]), note=str(ex))


# ---------------------------------------------------------------------------
# OBD calibration read -- the sequence the Scanmatik tool uses, captured on the
# bench 2026-10-07 (ecu-sim-20261007-100632 + simulatedreaduds): session 0x4F,
# read security (27 03/04, key = seed + 0x11C5A), then readMemoryByAddress
# (0x23) with ALFID 0x14 (4-byte address, 1-byte length) in 0xF0 chunks over the
# calibration window 0x180000-0x1FFFFF. Read-only; the only read the TCU offers.
READ_SESSION = 0x4F
READ_ALFID = 0x14            # addr 4 bytes (low nibble), len 1 byte (high nibble)
READ_CHUNK = 0xF0
CAL_READ_BASE = 0x180000
CAL_READ_END = 0x200000      # exclusive


class Zf6hpUdsReader:
    """Read the 6HP28 calibration over OBD (UDS/ISO-TP), the way the OEM-class
    tool does. Non-destructive: session change + security seed/key + repeated
    readMemoryByAddress, nothing else."""

    def __init__(self, isotp: IsoTp, on_stage=None, on_percent=None, is_stop=None,
                 assume_awake: bool = False):
        self.io = isotp
        self.client = Zf6hpUdsClient(isotp, log=on_stage or (lambda s: None))
        self._stage = on_stage or (lambda s: None)
        self._percent = on_percent or (lambda p: None)
        self._is_stop = is_stop or (lambda: False)
        # Set by the transport layer (see open_6hp_transport) when the gateway
        # was already woken over raw CAN. The reader then skips its own
        # functional wake -- which is a no-op over ISO15765 anyway, since the DLL
        # won't frame a 0x7DF broadcast without a flow-control filter for it
        # (2026-10-08 car trace: ISO15765 wake -> zero 0x7E9 -> connect failed)
        # -- and opens the physical session directly while gateway routing is
        # still open on its S3 timer.
        self._assume_awake = assume_awake

    def _expect(self, resp, sid, what):
        if not self.client.is_positive(resp, sid):
            nrc = self.client.nrc_of(resp)
            raise KLineError("%s failed: %s" % (
                what, nrc_name(nrc) if nrc is not None else "unexpected reply"))
        return resp

    def _unlock_read(self):
        from .crypto import zf6hp_read_key
        resp = self._expect(self.client.request(bytes([SID_SECURITY, 0x03])),
                            SID_SECURITY, "requestSeed (27 03)")
        seed = bytes(resp[2:])
        key = bytes(zf6hp_read_key(seed))
        self._expect(self.client.request(bytes([SID_SECURITY, 0x04]) + key),
                     SID_SECURITY, "sendKey (27 04)")
        self._stage("Read security unlocked (seed %s -> key %s)." % (seed.hex(), key.hex()))

    def _functional_wake(self):
        """Open the gateway's diagnostic routing the way ODIS does, BEFORE
        addressing the TCU physically: a functional (0x7DF) broadcast
        testerPresent then default-session. Many VAG gateways will not route a
        physical 0x7E1 request to a sub-module until a functional diagnostic
        session is active, so a bench read that fires 0x7E1 straight at a cold
        bus gets TX-but-no-RX (our 2026-10-07 trace: six frames out, nothing
        back). The scan listens promiscuously, so it also tells us WHICH id the
        TCU answers on. Best-effort: any reply means routing is open; no reply
        is not fatal, we still try physically after."""
        try:
            self.io.functional_scan(bytes([SID_TESTER_PRESENT, 0x00]), listen_ms=300)
        except Exception as ex:                  # noqa: BLE001 - wake is best effort
            self._stage("  functional testerPresent (3E 00 -> 0x%03X) skipped: %s"
                        % (FUNC_ID, ex))
        try:
            replies = self.io.functional_scan(bytes([SID_SESSION, 0x01]), listen_ms=800)
        except Exception as ex:                  # noqa: BLE001 - wake is best effort
            self._stage("  functional session (10 01 -> 0x%03X) skipped: %s"
                        % (FUNC_ID, ex))
            return {}
        if replies:
            who = ", ".join("0x%03X" % cid for cid in sorted(replies))
            self._stage("Functional 10 01 answered by: %s" % who)
            if self.io.rx_id in replies:
                self._stage("  TCU answered on 0x%03X -- gateway routing is open."
                            % self.io.rx_id)
        else:
            self._stage("  no functional reply -- gateway/TCU may be asleep "
                        "(ignition on?) or not routing 0x%03X." % FUNC_ID)
        return replies

    def _connect(self):
        """Bring the diagnostic link up the way ODIS does: first a FUNCTIONAL
        (0x7DF) wake so the gateway opens its routing to the TCU, then the
        physical default session on 0x7E1, retried a few times patiently. A
        bench tool that fires 0x7E1 straight at a cold bus gets no answer
        because the gateway is not yet routing to the sub-module (confirmed by
        the 2026-10-07 CAN trace: TX present, zero RX). Ends in an actionable
        error instead of a bare timeout."""
        if self._assume_awake:
            self._stage("Gateway already woken (raw-CAN functional wake); "
                        "opening the physical session directly.")
        else:
            self._functional_wake()
        last = "no reply"
        for attempt in range(3):
            if self._is_stop():
                raise KLineError("read aborted (stop requested)")
            try:
                self.client.request(bytes([SID_TESTER_PRESENT, 0x00]),
                                    timeout_ms=1500, overall_ms=1500)
            except KLineError:
                pass          # an unanswered testerPresent still wakes the link
            try:
                resp = self.client.request(bytes([SID_SESSION, 0x01]),
                                           timeout_ms=1500, overall_ms=3000,
                                           fail_fast_on_silence=False)
            except KLineError as ex:
                last = str(ex)
                self._stage("  no answer to default session (try %d/3); retrying..."
                            % (attempt + 1))
                continue
            if self.client.is_positive(resp, SID_SESSION):
                self._stage("Default session established (10 01).")
                return
            nrc = self.client.nrc_of(resp)
            last = nrc_name(nrc) if nrc is not None else "unexpected reply"
        raise KLineError(
            "No response from the TCU on 0x%03X/0x%03X (%s). Check the ignition "
            "is on and the gearbox TCU is awake, the CAN wiring and baud (500k), "
            "and that this TCU speaks UDS and not TP2.0."
            % (self.io.tx_id, self.io.rx_id, last))

    def _flush_bus(self):
        """CLEAR_RX + CLEAR_TX + drop buffered frames -- what the OEM tool does
        before every request -- so a stale or late reply can't be mistaken for
        the next one. Works for both transports (IsoTp and Iso15765Io expose
        ``_pending`` and a ``can`` with clear_rx/clear_tx)."""
        try:
            self.io._pending = []
        except Exception:
            pass
        for fn in ("clear_rx", "clear_tx"):
            f = getattr(self.io.can, fn, None)
            if f is not None:
                try:
                    f()
                except Exception:
                    pass

    def _read_chunk(self, req, addr, tries=6, attempt_ms=1500):
        """Read one 0x23 chunk the way the OEM tool (Scanmatik) survives a lossy
        link: a SHORT per-attempt timeout so a dropped reply is detected fast,
        then re-send the SAME request in the SAME session, several times, before
        giving up. (Captured Scanmatik behaviour under injected jitter: ~500 ms
        read window, same request re-sent up to 3x, session never torn down.) A
        single missed chunk must NOT abort a ~2000-chunk read -- the car reads
        died at 91% / 23% on one unanswered chunk. ``attempt_ms`` bounds the
        whole paced response (~35 frames at ~10 ms); the FIRST frame is given a
        shorter window so a fully-dropped reply is re-sent quickly. Stale/late
        frames are flushed before each resend so a tardy reply can't desync."""
        last = "no reply"
        first_ms = min(500, attempt_ms)
        for attempt in range(tries):
            if self._is_stop():
                raise KLineError("read aborted (stop requested)")
            self._flush_bus()               # CLEAR_RX + CLEAR_TX before EVERY request (OEM does this)
            try:
                resp = self.client.request(req, timeout_ms=first_ms,
                                           p2_star_ms=attempt_ms,
                                           overall_ms=attempt_ms)
            except KLineError as ex:
                last = str(ex)
            else:
                if self.client.is_positive(resp, SID_READ_MEM):
                    return resp
                nrc = self.client.nrc_of(resp)
                last = nrc_name(nrc) if nrc is not None else "unexpected reply"
            if attempt + 1 < tries:
                self._stage("  @0x%06X %s (try %d/%d); re-requesting..."
                            % (addr, last, attempt + 1, tries))
        raise KLineError("readMemoryByAddress @0x%06X failed after %d tries: %s"
                         % (addr, tries, last))

    def read_calibration(self, base=CAL_READ_BASE, end=CAL_READ_END) -> bytes:
        try:
            self.io.pass_ids()
        except Exception:
            pass
        self._connect()
        self._stage("Read session (10 %02X)..." % READ_SESSION)
        self._expect(self.client.request(bytes([SID_SESSION, READ_SESSION])),
                     SID_SESSION, "read session 0x%02X" % READ_SESSION)
        self._unlock_read()
        total = end - base
        out = bytearray()
        addr = base
        self._stage("Reading calibration 0x%06X-0x%06X (%d KiB) via 0x23 ALFID 0x%02X..."
                    % (base, end - 1, total // 1024, READ_ALFID))
        while addr < end:
            if self._is_stop():
                raise KLineError("read aborted (stop requested)")
            n = min(READ_CHUNK, end - addr)
            req = bytes([SID_READ_MEM, READ_ALFID]) + addr.to_bytes(4, "big") + bytes([n])
            resp = self._read_chunk(req, addr)
            data = bytes(resp[1:])
            if len(data) != n:
                self._stage("  note: @0x%06X asked %d got %d bytes" % (addr, n, len(data)))
                data = data[:n] if len(data) >= n else data + b"\x00" * (n - len(data))
            out += data
            addr += n
            self._percent(int((addr - base) * 100 / total))
        self._stage("Calibration read complete: %d bytes." % len(out))
        return bytes(out)


# ===========================================================================
# Full-image WRITE (program + calibration) over OBD/UDS
# ===========================================================================
# Block layout and the validity / verify-before-write model used by
# Zf6hpUdsWriter below. Everything here is STRUCTURAL: block bases/lengths are
# the Bosch "BCB Type1" header [START_ADDRESS]/[END_ADDRESS] values confirmed
# against the 8K0927158C 6HP28 FRF (see ovtsuite.frf.image, which anchors the
# same addresses), and the decision to write the program set vs calibration
# only is driven by what is actually present and valid in the supplied image --
# never by a caller flag. The per-block checksum fed to the 0x0202 verify is
# zlib.crc32 of that block's uncompressed plaintext (confirmed byte-for-byte
# against captured writes of DB_1/DB_2/DB_4; DB_3 confirmed once its true base
# 0x20080 was used). The write wire payload is the BCB stream from
# crypto.zf6hp_bcb_encode with the 6HP28 additive terminal checksum.

IMAGE_SIZE = 0x200000            # full 2 MB flash image


@dataclass(frozen=True)
class BlockSpec:
    block_id: int
    name: str
    base: int
    length: int
    kind: str                    # "program" | "descriptor" | "calibration"

    @property
    def is_cal(self) -> bool:
        return self.kind == "calibration"


GS19_BLOCKS: List[BlockSpec] = [
    BlockSpec(0x01, "DB_1", 0x080000, 0x0FFF80, "program"),
    BlockSpec(0x02, "DB_2", 0x040000, 0x03FF80, "program"),
    BlockSpec(0x03, "DB_3", 0x020080, 0x01FF80, "descriptor"),
    BlockSpec(0x04, "DB_4", 0x180000, 0x07FF80, "calibration"),
]
GS19_CAL = GS19_BLOCKS[-1]
_BCB_MAGIC = b"BCB"              # DB_3 container magic ("BCBS2103")

# 6HP28 application calibration checksum (CRC-16/CCITT-FALSE): stored big-endian
# at CAL_CKSUM_AT over [CAL_CKSUM_START, CAL_CKSUM_END) (absolute flash). Used
# ONLY to warn when an edited cal was not re-checksummed -- the write path
# transfers bytes verbatim and does not fix this.
CAL_CKSUM_AT = 0x180080
CAL_CKSUM_START = 0x180082
CAL_CKSUM_END = 0x1F67B8


def _crc16_ccitt_false(buf: bytes) -> int:
    c = 0xFFFF
    for b in buf:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def _is_blank(region: bytes) -> bool:
    return (not region
            or region.count(0xFF) == len(region)
            or region.count(0x00) == len(region))


def _encode_block(plaintext: bytes) -> bytes:
    """BCB-encode one block's plaintext for transferData, byte-for-byte as the
    OEM tool does for the 6HP28: RLE min-run 5 and the additive terminal
    checksum (verified against the 8K0927158C FRF; see crypto.zf6hp_bcb_encode)."""
    return zf6hp_bcb_encode(plaintext, min_rle_run=5,
                            terminal_checksum=zf6hp_bcb_additive_checksum(plaintext))


@dataclass
class BlockPlan:
    spec: BlockSpec
    plaintext: bytes
    present: bool
    valid: bool
    new_crc: int                             # zlib.crc32 of plaintext
    reason: str = ""                         # why excluded, if so
    resident_match: Optional[bool] = None    # True=same, False=differs, None=unknown
    will_write: bool = False
    note: str = ""


@dataclass
class WritePlan:
    cal_only: bool
    blocks: List[BlockPlan]
    warnings: List[str] = field(default_factory=list)

    @property
    def write_set(self) -> List[BlockPlan]:
        return [b for b in self.blocks if b.will_write]

    def report(self) -> str:
        head = ("Calibration-only image -- only DB_4 will be considered"
                if self.cal_only else
                "Full-program image -- program set + calibration considered")
        lines = [head]
        for b in self.blocks:
            if b.resident_match is True:
                tag = "SKIP"
            elif b.will_write:
                tag = "WRITE"
            else:
                tag = "----"
            lines.append("  %-5s id=0x%02X %8d B  crc=0x%08X  %-5s %s"
                         % (b.spec.name, b.spec.block_id, b.spec.length,
                            b.new_crc, tag, b.note or b.reason))
        for w in self.warnings:
            lines.append("  ! " + w)
        return "\n".join(lines)


def classify_image(image: bytes, log=None, force_cal_only: bool = False) -> WritePlan:
    """Decide, structurally, which blocks a supplied flat image contains and is
    allowed to write. A 2 MB image is sliced at the fixed block bases; a
    calibration-sized image (the 0x180000..0x200000 window) is treated as DB_4
    only. The program set (DB_1/DB_2/DB_3) is written ONLY when all three are
    present (non-blank) and DB_3 carries its BCB container magic -- otherwise
    the image is treated as calibration-only and just DB_4 is written.
    ``force_cal_only`` makes a full image write DB_4 ONLY, leaving the program
    set in place (the GUI "Calibration only" option). Pure analysis: no bus
    traffic."""
    log = log or (lambda s: None)
    n = len(image)
    blocks: List[BlockPlan] = []
    warnings: List[str] = []

    if n == GS19_CAL.length or n == 0x80000:
        pt = bytes(image[:GS19_CAL.length])
        present = not _is_blank(pt)
        blocks.append(BlockPlan(GS19_CAL, pt, present, present,
                                zlib.crc32(pt) & 0xFFFFFFFF,
                                "" if present else "calibration region is blank"))
        cal_only = True
    elif n >= IMAGE_SIZE:
        for spec in GS19_BLOCKS:
            pt = bytes(image[spec.base:spec.base + spec.length])
            present = not _is_blank(pt)
            if spec.name == "DB_3":
                valid = present and pt[:len(_BCB_MAGIC)] == _BCB_MAGIC
                reason = "" if valid else ("blank" if not present
                                           else "no BCB container magic (not a valid descriptor block)")
            else:
                valid = present
                reason = "" if present else "blank"
            blocks.append(BlockPlan(spec, pt, present, valid,
                                    zlib.crc32(pt) & 0xFFFFFFFF, reason))
        byname = {b.spec.name: b for b in blocks}
        full = (byname["DB_1"].present and byname["DB_2"].present
                and byname["DB_3"].present and byname["DB_3"].valid)
        cal_only = not full
        if not full:
            log("Program set not fully valid (%s) -- treating as calibration-only."
                % ", ".join("%s:%s" % (b.spec.name, "ok" if b.valid else (b.reason or "bad"))
                            for b in blocks if not b.spec.is_cal))
    else:
        raise KLineError("zf6hp UDS write: image is %d bytes; need a full %d-byte "
                         "image or a %d-byte calibration dump"
                         % (n, IMAGE_SIZE, GS19_CAL.length))

    if force_cal_only and not cal_only:
        cal_only = True
        log("Calibration-only requested -- writing DB_4 only; the program set "
            "(DB_1/DB_2/DB_3) in the image is left in place.")

    # calibration stored-checksum sanity (warning only -- verbatim write). Use
    # the AUTHORITATIVE checksum6hp verifier, which locates the CRC-16/CCITT
    # region from the DAx descriptor rather than a hardcoded window: a real cal
    # read verifies exactly (a freshly-read, unmodified cal must NOT be flagged),
    # and the fixed CAL_CKSUM_* window below was 0x68 bytes short, so it
    # false-warned on valid cals. Best-effort: never block the write.
    cal = next((b for b in blocks if b.spec.is_cal), None)
    if cal is not None and cal.present:
        cal_region = (bytes(image[GS19_CAL.base:GS19_CAL.base + 0x80000])
                      if n >= IMAGE_SIZE else bytes(image[:0x80000]))
        from ..modules import registry as _registry
        _ck6 = _registry.checksum("zf6hp")
        if _ck6 is None:
            warnings.append("calibration checksum not checked: "
                            + _registry.missing_text("zf6hp", "checksum checking"))
        else:
            try:
                rep = _ck6.verify(cal_region)
                if getattr(rep, "cal_present", False) and rep.stored != rep.computed:
                    warnings.append("calibration stored checksum 0x%04X != computed 0x%04X -- "
                                    "correct the cal checksum before writing (the write path "
                                    "transfers bytes verbatim)" % (rep.stored, rep.computed))
            except Exception:                    # noqa: BLE001 - sanity check, never fatal
                pass

    for b in blocks:
        if b.spec.is_cal:
            b.will_write = b.present
            if not b.present:
                b.reason = b.reason or "blank"
        else:
            b.will_write = (not cal_only) and b.valid
            if cal_only and not b.reason:
                b.reason = ("excluded (calibration-only requested)" if force_cal_only
                            else "excluded (calibration-only image)")
    return WritePlan(cal_only, blocks, warnings)


class Zf6hpUdsWriter:
    """Write a 6HP28 flash image over OBD/UDS. Non-cal blocks are written only
    when the supplied image carries a valid program set (see classify_image);
    otherwise only the calibration is written. Every candidate block is
    checksum-verified against the TCU's RESIDENT content (routine 0x0202)
    BEFORE erasing, and a block that already matches is skipped -- so an
    unchanged block is never re-flashed (mirrors zf6hp.write_areas' pre-write
    skip). ``plan()`` runs that verification non-destructively (the write
    probe); ``write()`` runs it and then flashes the blocks that differ.

    SIGNAL SEMANTICS (confirmed in the bench simulator, to re-confirm against a
    real TCU): the 0x0202 routine's positive reply ends in a routineStatusRecord
    byte -- 0x00 is taken as "resident matches", non-zero as "differs". A NRC or
    any malformed reply is treated as 'can't tell' and the block is written
    anyway (never skipped on doubt)."""

    FINGERPRINT_DID = 0xF15A
    # The ODIS fingerprint bytes captured on the bench (2E F1 5A 00..00 AA AA AA).
    DEFAULT_FINGERPRINT = bytes.fromhex("000000000000aaaaaa")
    # Platform hooks (the 6HP28 defaults). ovtsuite.flash.zf8hp_uds subclasses
    # this writer and swaps these three for the ZF 8HP's dfi 0x22, its
    # XOR+LZSS transferData codec and its own block map/validity rules; the
    # VAG UDS programming choreography itself is shared.
    DOWNLOAD_FORMAT = DOWNLOAD_FORMAT
    encode_block = staticmethod(_encode_block)
    write_key = staticmethod(zf6hp_write_key)
    classify = staticmethod(classify_image)

    def __init__(self, isotp: IsoTp, on_stage=None, on_percent=None, is_stop=None,
                 fingerprint: Optional[bytes] = None, op_timeout_ms: int = 60000):
        self.io = isotp
        self.client = Zf6hpUdsClient(isotp, log=on_stage or (lambda s: None))
        self._stage = on_stage or (lambda s: None)
        self._percent = on_percent or (lambda p: None)
        self._is_stop = is_stop or (lambda: False)
        self.fingerprint = fingerprint
        # erase and whole-block checksum routines can run for many seconds; they
        # are allowed up to this budget, paced by the ECU's responsePending.
        self.op_timeout_ms = op_timeout_ms
        # Silent-erase handling (mirrors the OEM tool): during a silent erase,
        # hold the session with testerPresent every keepalive_interval_s and
        # re-send the erase every resend_interval_s.
        self.keepalive_interval_s = 1.0
        self.resend_interval_s = 3.5

    # ---- low-level helpers ----
    def _stop(self):
        if self._is_stop():
            raise KLineError("write aborted (stop requested)")

    def _expect(self, resp, sid, what):
        if not self.client.is_positive(resp, sid):
            nrc = self.client.nrc_of(resp)
            raise KLineError("%s failed: %s" % (
                what, nrc_name(nrc) if nrc is not None else "unexpected reply"))
        return resp

    def _routine(self, rid, data=b"", what="", overall_ms=20000,
                 fail_fast_on_silence=True, keepalive=False):
        req = bytes([SID_ROUTINE, ROUTINE_START, (rid >> 8) & 0xFF, rid & 0xFF]) + bytes(data)
        if keepalive:
            resp = self.client.request_with_keepalive(
                req, keepalive_interval_s=self.keepalive_interval_s,
                resend_interval_s=self.resend_interval_s, overall_ms=overall_ms)
            return self._expect(resp, SID_ROUTINE, what or ("routine 0x%04X" % rid))
        return self._req_retry(req, SID_ROUTINE, what or ("routine 0x%04X" % rid),
                               overall_ms=overall_ms)

    def _quiet_bus(self):
        for req in (bytes([SID_DTC_SETTING, 0x02]), bytes([SID_COMM_CONTROL, 0x03, 0x01])):
            try:
                self.client.request(req)
            except KLineError:
                pass

    def _restore_bus(self):
        for req in (bytes([SID_COMM_CONTROL, 0x00, 0x01]), bytes([SID_DTC_SETTING, 0x01])):
            try:
                self.client.request(req)
            except KLineError:
                pass

    def _unlock_write(self, tries=5):
        last = "no reply"
        for attempt in range(tries):
            if self._is_stop():
                raise KLineError("write aborted (stop requested)")
            self._flush_bus()
            try:
                resp = self.client.request(bytes([SID_SECURITY, 0x11]),
                                           timeout_ms=800, overall_ms=4000)
                if not self.client.is_positive(resp, SID_SECURITY):
                    nrc = self.client.nrc_of(resp)
                    last = nrc_name(nrc) if nrc is not None else "no seed"
                else:
                    seed = bytes(resp[2:6])
                    key = bytes(self.write_key(seed))
                    r2 = self.client.request(bytes([SID_SECURITY, 0x12]) + key,
                                             timeout_ms=800, overall_ms=4000)
                    if self.client.is_positive(r2, SID_SECURITY):
                        self._stage("Write security unlocked (seed %s -> key %s)."
                                    % (seed.hex(), key.hex()))
                        return
                    nrc = self.client.nrc_of(r2)
                    last = nrc_name(nrc) if nrc is not None else "key rejected"
            except KLineError as ex:
                last = str(ex)
            if attempt + 1 < tries:
                self._stage("  write security (27 11/12): %s (try %d/%d); retrying..."
                            % (last, attempt + 1, tries))
        raise KLineError("write security unlock failed after %d tries: %s" % (tries, last))

    def _enter_programming(self, write_fingerprint: bool):
        try:
            self.io.pass_ids()
        except Exception:
            pass
        self._req_retry(bytes([SID_SESSION, SESSION_EXTENDED]), SID_SESSION,
                        "extended session (10 03)")
        self._routine(ROUTINE_CHECK_PRECONDITIONS, b"",
                      "checkProgrammingPreconditions (0203)")
        self._quiet_bus()
        self._req_retry(bytes([SID_SESSION, SESSION_PROGRAMMING]), SID_SESSION,
                        "programming session (10 02)")
        self._unlock_write()
        if write_fingerprint:
            fp = self.fingerprint if self.fingerprint is not None else self.DEFAULT_FINGERPRINT
            did = self.FINGERPRINT_DID
            self._req_retry(bytes([SID_WRITE_DATA, (did >> 8) & 0xFF, did & 0xFF]) + fp,
                            SID_WRITE_DATA, "writeFingerprint (2E F15A)")

    def _verify_block(self, bp: BlockPlan) -> Optional[bool]:
        """Run routine 0x0202 against the RESIDENT block with bp.new_crc as the
        expected value. Returns True (matches), False (differs), or None (could
        not tell). Sets bp.resident_match / bp.note."""
        data = bytes([0x01, bp.spec.block_id, 0x00, 0x04]) + bp.new_crc.to_bytes(4, "big")
        req = bytes([SID_ROUTINE, ROUTINE_START,
                     (ROUTINE_CHECKSUM >> 8) & 0xFF, ROUTINE_CHECKSUM & 0xFF]) + data
        try:
            resp = self.client.request(req, overall_ms=self.op_timeout_ms,
                                       fail_fast_on_silence=False)
        except KLineError as ex:
            bp.resident_match = None
            bp.note = "verify error: %s" % ex
            return None
        if not self.client.is_positive(resp, SID_ROUTINE):
            nrc = self.client.nrc_of(resp)
            bp.resident_match = None
            bp.note = "verify NRC %s -- will write" % (nrc_name(nrc) if nrc is not None else "?")
            return None
        status = resp[-1] if len(resp) >= 5 else None
        match = (status == 0x00)
        bp.resident_match = match
        bp.note = ("resident matches" if match
                   else "resident differs (status 0x%02X)" % (status if status is not None else 0xFF))
        return match

    def _flush_bus(self):
        """CLEAR_RX + CLEAR_TX before an operation, the way the OEM tool does, so
        a stale erase ack or a late reply can't be mistaken for the next step.
        Works over IsoTp and Iso15765Io alike."""
        for fn in ("clear_rx", "clear_tx"):
            f = getattr(self.io.can, fn, None)
            if f is not None:
                try:
                    f()
                except Exception:
                    pass

    def _write_block_retry(self, bp, tries=3):
        """Write one block, re-erasing and redoing it from scratch on a failure.
        Block-level retry is the SAFE recovery for a lossy link during a WRITE:
        because _write_block erases first, a retry wipes the partial write and
        starts over, so there is no half-applied transferData to reason about.
        (A blind same-bsc transferData re-send would corrupt any ECU that does
        not de-dup by block-sequence-counter, so we do not do that.)"""
        last = None
        for attempt in range(tries):
            if self._is_stop():
                raise KLineError("write aborted (stop requested)")
            try:
                self._write_block(bp)
                return
            except KLineError as ex:
                last = ex
                if attempt + 1 < tries:
                    self._stage("  %s failed (%s); re-erasing and retrying block (%d/%d)..."
                                % (bp.spec.name, ex, attempt + 1, tries))
                    self._flush_bus()
        raise last if last is not None else KLineError("write failed")

    def _req_retry(self, payload, sid, what, tries=5, timeout_ms=800,
                   p2_star_ms=2000, overall_ms=4000):
        """Send one WRITE request, re-sending the SAME bytes on a dropped or
        transient-NRC reply (for transferData that means the SAME block-sequence
        counter -- an ISO14229 retransmission the server de-dups, not a new
        chunk). Flushes RX+TX before each attempt, like the OEM tool. Use only
        for idempotent / de-duppable requests; requestDownload is recovered by a
        block-level re-erase instead."""
        last = "no reply"
        for attempt in range(tries):
            if self._is_stop():
                raise KLineError("write aborted (stop requested)")
            self._flush_bus()
            try:
                resp = self.client.request(payload, timeout_ms=timeout_ms,
                                           p2_star_ms=p2_star_ms, overall_ms=overall_ms)
            except KLineError as ex:
                last = str(ex)
            else:
                if self.client.is_positive(resp, sid):
                    return resp
                nrc = self.client.nrc_of(resp)
                last = nrc_name(nrc) if nrc is not None else "unexpected reply"
            if attempt + 1 < tries:
                self._stage("  %s: %s (try %d/%d); re-sending..."
                            % (what, last, attempt + 1, tries))
        raise KLineError("%s failed after %d tries: %s" % (what, tries, last))

    def _write_block(self, bp: BlockPlan):
        spec = bp.spec
        self._stage("Writing %s (block 0x%02X, %d bytes, crc32 0x%08X)..."
                    % (spec.name, spec.block_id, spec.length, bp.new_crc))
        self._flush_bus()               # clear before erase (OEM recovery)
        self._routine(ROUTINE_ERASE, bytes([0x01, spec.block_id]),
                      "eraseMemory block 0x%02X" % spec.block_id,
                      overall_ms=self.op_timeout_ms, keepalive=True)
        # drop any stray erase acks left by silent-mode re-sends before the
        # next phase (as the OEM tool CLEAR_RXes between phases).
        try:
            self.io.can.clear_rx()
        except Exception:
            pass
        dl = (bytes([SID_REQUEST_DOWNLOAD, self.DOWNLOAD_FORMAT, 0x41, spec.block_id])
              + spec.length.to_bytes(4, "big"))
        resp = self._expect(self.client.request(dl), SID_REQUEST_DOWNLOAD,
                            "requestDownload block 0x%02X" % spec.block_id)
        lfi = resp[1] if len(resp) > 1 else 0x20
        nb = (lfi >> 4) & 0x0F
        maxlen = (int.from_bytes(resp[2:2 + nb], "big")
                  if nb and len(resp) >= 2 + nb else 0x0802)
        chunk = max(1, maxlen - 2)                 # minus SID + blockSequenceCounter
        stream = self.encode_block(bp.plaintext)
        bsc, off = 0x01, 0
        while off < len(stream):
            self._stop()
            part = stream[off:off + chunk]
            self._req_retry(bytes([SID_TRANSFER_DATA, bsc]) + part, SID_TRANSFER_DATA,
                            "transferData #%d block 0x%02X" % (bsc, spec.block_id))
            off += len(part)
            bsc = (bsc + 1) & 0xFF
        self._req_retry(bytes([SID_TRANSFER_EXIT]), SID_TRANSFER_EXIT,
                        "transferExit block 0x%02X" % spec.block_id)
        data = bytes([0x01, spec.block_id, 0x00, 0x04]) + bp.new_crc.to_bytes(4, "big")
        csum_req = (bytes([SID_ROUTINE, ROUTINE_START,
                           (ROUTINE_CHECKSUM >> 8) & 0xFF, ROUTINE_CHECKSUM & 0xFF]) + data)
        resp = self._req_retry(csum_req, SID_ROUTINE,
                               "post-write checksum verify block 0x%02X" % spec.block_id,
                               overall_ms=self.op_timeout_ms)
        if len(resp) >= 5 and resp[-1] != 0x00:
            raise KLineError("%s post-write checksum mismatch (status 0x%02X)"
                             % (spec.name, resp[-1]))

    # ---- public API ----
    def plan(self, image: bytes) -> WritePlan:
        """Non-destructive write probe: establish the programming session and
        check each candidate block against the TCU's resident checksum, so the
        caller can see exactly what WOULD be written (and what is unchanged)
        before committing. No erase / requestDownload / transferData is sent."""
        plan = self.classify(image, self._stage)
        for w in plan.warnings:
            self._stage("WARNING: " + w)
        self._enter_programming(write_fingerprint=False)
        try:
            for b in plan.blocks:
                self._stop()
                if not b.will_write:
                    continue                      # not a candidate (excluded/blank)
                m = self._verify_block(b)
                if m is True:
                    b.will_write = False
                    b.note = "unchanged -- would skip"
                else:
                    b.will_write = True
                    b.note = "would write (%s)" % b.note
        finally:
            self._restore_bus()
        self._stage(plan.report())
        return plan

    def write(self, image: bytes, force_full: bool = False,
              cal_only: bool = False) -> WritePlan:
        """Flash the image: every present/valid candidate block is erased and
        rewritten. This TCU's 0x0202 routine is a POST-download checksum, not a
        resident-block query (it answers conditionsNotCorrect before a download),
        so there is no reliable pre-write skip of unchanged blocks on the car --
        every candidate is written. ``force_full`` is accepted for call-site
        compatibility and no longer changes behaviour. ``cal_only`` forces a full
        image to write DB_4 only (the GUI "Calibration only" option), leaving the
        program set in place. The post-write 0x0202 checksum in _write_block
        still verifies each block after it is written."""
        plan = self.classify(image, self._stage, force_cal_only=cal_only)
        for w in plan.warnings:
            self._stage("WARNING: " + w)
        candidates = [b for b in plan.blocks if b.will_write]
        if not candidates:
            self._stage("Nothing to write -- no present/valid block in the image.")
            return plan
        last_ep = None
        for _attempt in range(3):
            try:
                self._enter_programming(write_fingerprint=True)
                break
            except KLineError as ex:
                last_ep = ex
                if self._is_stop() or _attempt + 1 >= 3:
                    raise
                self._stage("  programming entry failed (%s); retrying (%d/3)..."
                            % (ex, _attempt + 1))
                self._flush_bus()
        total = sum(b.spec.length for b in candidates) or 1
        done = 0
        for b in candidates:
            self._stop()
            self._write_block_retry(b)
            b.will_write = True
            done += b.spec.length
            self._percent(int(done * 100 / total))
        self._routine(ROUTINE_CHECK_DEPENDENCIES, b"",
                      "checkProgrammingDependencies (FF01)")
        self._restore_bus()
        try:
            self.client.request(bytes([SID_RESET, 0x01]))
        except KLineError:
            pass
        self._stage("Write complete. " + plan.report())
        return plan


# ===========================================================================
# Transport auto-detection: which flash path a loaded 6HP image needs
# ===========================================================================
# A 6HP28 (GS19 / MPC562 / TC19.11) flashes over native UDS/ISO-TP (this
# module's Zf6hpUdsWriter); the older 6HP19/26 (MPC555 / TC19.x) flash over
# KWP2000/TP2.0 (ovtsuite.flash.zf6hp.Zf6hpFlash). detect_6hp_transport picks
# between them from STRUCTURAL identity the session already established -- the
# matched KB family, then the hardware banner's MCU, then (for our own read
# output) the container meta -- and only falls back to a size heuristic when an
# image carries no identity at all. It is pure (no I/O); the GUI layer supplies
# a manual override on top of it (see the unified-flasher plan).

UDS = "uds"
TP20 = "tp20"


@dataclass(frozen=True)
class TransportChoice:
    transport: str      # UDS | TP20
    confidence: str     # "high" (structural identity) | "low" (heuristic/default)
    reason: str

    @property
    def is_uds(self) -> bool:
        return self.transport == UDS


def detect_6hp_transport(family=None, controller_mcu=None, meta=None,
                         size=None) -> "TransportChoice":
    """Classify a loaded ZF 6HP image's flash transport. Inputs (any may be
    None) are all things the session already derives:
      * ``family``        -- matched KB family (e.g. ``zf6hp28`` vs ``zf6hp19_*``)
      * ``controller_mcu``-- ``MPC562`` (6HP28) / ``MPC555`` (6HP19/26) banner
      * ``meta``          -- container BLOCK_META dict (our read output tags
                             ``variant``)
      * ``size``          -- image length, last-resort only
    Structural signals win; the size fallback is marked ``low`` confidence so a
    caller can prompt or let the manual override stand."""
    # 1. Container meta the OVT read/save path writes (self-identifying).
    if meta:
        v = str(meta.get("variant") or "").lower().replace("_", "").replace("-", "")
        if "6hp28" in v:
            return TransportChoice(UDS, "high", "container meta variant=6hp28")
        if "6hp26" in v or "6hp19" in v:
            return TransportChoice(TP20, "high", "container meta variant=%s" % v)
    # 2. The KB family the session matched (works even for a bare 512 KB cal).
    fam = (family or "").lower()
    if fam == "zf6hp28":
        return TransportChoice(UDS, "high", "identified family zf6hp28 (GS19/MPC562)")
    if fam.startswith("zf6hp19") or fam == "zf6hp26":
        return TransportChoice(TP20, "high", "identified family %s" % family)
    # 3. The hardware banner's MCU (present in full images, absent in cal-only).
    mcu = (controller_mcu or "").upper()
    if mcu == "MPC562":
        return TransportChoice(UDS, "high", "controller MPC562 (6HP28 / TC19.11)")
    if mcu == "MPC555":
        return TransportChoice(TP20, "high", "controller MPC555 (6HP19/26)")
    # 4. Last resort, no identity at all: size. A 6HP28 is 2 MB (or a 512 KB
    #    cal); a 6HP19 full is 1 MB. >=1.5 MB is almost certainly a 6HP28 image.
    if size is not None and size >= 0x180000:
        return TransportChoice(UDS, "low",
                               "no identity; %d bytes (>=1.5 MB) suggests 6HP28" % size)
    return TransportChoice(TP20, "low",
                           "no 6HP28 signal; defaulting to TP2.0 (6HP19/26)")


def transport_for_session(session) -> "TransportChoice":
    """``detect_6hp_transport`` fed from a loaded TunerSession (its matched
    family, controller banner, container meta and image size). Pure read of
    attributes -- safe if any are absent."""
    meta = {}
    cont = getattr(session, "_container", None)
    if cont is not None:
        try:
            meta = cont.meta() or {}
        except Exception:
            meta = {}
    ctl = getattr(session, "controller", None)
    mcu = getattr(ctl, "mcu", "") if ctl is not None else ""
    size = None
    bf = getattr(session, "binfile", None)
    if bf is not None:
        try:
            size = len(bf.data)
        except Exception:
            size = None
    return detect_6hp_transport(family=getattr(session, "family", None),
                                controller_mcu=mcu, meta=meta, size=size)
