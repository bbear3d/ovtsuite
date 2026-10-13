# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bench ECU simulator: answer as a UDS module on a real CAN bus so a
third-party diagnostic tool's command/read sequence can be captured without the
real module (or a whole car) present.

Why this exists
---------------
A J2534 capture of a tool against a bare bench goes nowhere: with no second CAN
node nothing ACKs the tool's frames, so its first request never leaves the
controller (``WriteMsgs = TIMEOUT sent=0``) and it gives up. Put a node on the
bus that (a) ACKs and (b) answers like the target module, and the tool walks
its whole sequence -- which this module logs.

Module-agnostic
---------------
The wire/framing layer (:class:`IsoTpEcu`) and the dispatch
(:class:`EcuResponder`) are generic UDS-over-ISO-TP. What a module *is* -- its
CAN ids, identification, live DIDs and seed/key algorithm(s) -- lives in an
:class:`EcuProfile`. The ZF 6HP28 (GS19) is one profile; add a module by adding
a profile (see ``_PROFILE_FACTORIES``), no code changes. ``Gs19Responder`` is
kept as a thin alias for the 6HP28 profile.

In the default ``serve`` read mode the sim answers reads from a supplied image
so the tool completes its read loop; ``faithful`` instead refuses reads exactly
as a real module does, to confirm where a tool stalls. Security keys are
validated against the profile's algorithm and logged match/mismatch, but in
``serve`` mode access is granted regardless so an unexpected key never blocks
discovery. Nothing here touches a real ECU: it is a responder.
"""
from __future__ import annotations

import os
import random
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import crypto as _crypto


@dataclass
class SimTiming:
    """Bench knobs that reproduce the slow / lossy behaviour of the real gearbox
    TCU over the VAG gateway, so a flashing tool can be exercised against those
    exact conditions on the bench instead of in the car.

    Measured on the 2026-10-07 car reads: the TCU paced CONSECUTIVE frames at a
    steady ~10 ms regardless of the STmin the tester requested (so a 512 KiB read
    took ~12 min), and a single unanswered request once aborted a read at 91%.
    These knobs recreate that: a per-frame send floor, a per-response latency
    (with jitter), and occasional dropped frames / dropped whole responses.
    All-zero means ideal timing (the previous behaviour)."""
    cf_gap_ms: float = 0.0          # ECU's own min delay between consecutive frames
    resp_latency_ms: float = 0.0    # delay before each response's first frame
    jitter_ms: float = 0.0          # +/- uniform jitter on the two delays above
    drop_cf_prob: float = 0.0       # chance (0..1) to drop ONE consecutive frame
    drop_resp_prob: float = 0.0     # chance (0..1) to drop an ENTIRE response
    stall_every: int = 0            # every Nth response, stall for stall_ms...
    stall_ms: float = 0.0           # ...to mimic an occasional long comms gap

    def active(self) -> bool:
        return bool(self.cf_gap_ms or self.resp_latency_ms or self.drop_cf_prob
                    or self.drop_resp_prob or (self.stall_every and self.stall_ms))

    def jitter(self, base_ms: float) -> float:
        if self.jitter_ms <= 0 or base_ms <= 0:
            return base_ms
        return max(0.0, base_ms + random.uniform(-self.jitter_ms, self.jitter_ms))

# Default ISO-TP ids: tester -> ECU on 0x7E1 (functional 0x7DF); ECU -> tester
# on 0x7E9. A profile overrides these for a module on a different pair.
RX_ID = 0x7E1
TX_ID = 0x7E9
FUNC_ID = 0x7DF
PAD = 0x55

POS = 0x40
NR = 0x7F

SID_SESSION = 0x10
SID_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DATA = 0x22
SID_WRITE_DATA = 0x2E       # writeDataByIdentifier (ODIS pre-flash fingerprint)
SID_READ_MEM = 0x23
SID_SECURITY = 0x27
SID_COMM_CONTROL = 0x28
SID_TESTER_PRESENT = 0x3E
SID_REQUEST_DOWNLOAD = 0x34
SID_REQUEST_UPLOAD = 0x35
SID_TRANSFER_DATA = 0x36
SID_TRANSFER_EXIT = 0x37
SID_ROUTINE = 0x31
SID_DTC_SETTING = 0x85

NRC_RESPONSE_PENDING = 0x78
NRC_WRONG_LENGTH = 0x13
NRC_SEQUENCE = 0x24
NRC_OUT_OF_RANGE = 0x31
NRC_INVALID_KEY = 0x35
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SERVICE_NOT_IN_SESSION = 0x7F
NRC_SECURITY_DENIED = 0x33


def nrc(sid: int, code: int) -> bytes:
    return bytes([NR, sid, code])


def _stmin_seconds(stmin: int) -> float:
    if stmin <= 0x7F:
        return stmin / 1000.0
    if 0xF1 <= stmin <= 0xF9:
        return (stmin - 0xF0) / 1_000_000.0
    return 0.0


class IsoTpEcu:
    """ECU-side ISO-TP over a raw-CAN transport. ``poll`` reassembles one
    request PDU (sending the tester a flow-control CTS for multi-frame);
    ``send`` segments a response, honouring the tester's flow control."""

    def __init__(self, can, rx_id: int = RX_ID, tx_id: int = TX_ID,
                 func_id: int = FUNC_ID, pad: int = PAD,
                 log: Optional[Callable[[str], None]] = None,
                 timing: Optional["SimTiming"] = None):
        self.can = can
        self.rx_id = rx_id
        self.tx_id = tx_id
        self.func_id = func_id
        self.pad = pad
        self._log = log or (lambda s: None)
        self.timing = timing or SimTiming()
        self._asm: Optional[List] = None
        self.last_rx_id: Optional[int] = None
        self.frames_seen = 0                 # matched frames received (diagnostics)

    def _pad(self, frame: bytes) -> bytes:
        return frame + bytes([self.pad]) * (8 - len(frame)) if len(frame) < 8 else frame

    def _frames(self, timeout_ms: int):
        for m in self.can.read(max_msgs=32, timeout_ms=timeout_ms):
            cid = getattr(m, "can_id", None)
            if cid is None or cid not in (self.rx_id, self.func_id):
                continue
            if getattr(m, "is_tx_echo", False):
                continue
            data = bytes(getattr(m, "payload", b""))
            if data:
                self.frames_seen += 1
                yield cid, data

    def poll(self, timeout_ms: int = 50) -> Optional[bytes]:
        for cid, data in self._frames(timeout_ms):
            pci = data[0] >> 4
            if pci == 0x0:
                ln = data[0] & 0x0F
                self.last_rx_id = cid
                return bytes(data[1:1 + ln])
            if pci == 0x1:
                total = ((data[0] & 0x0F) << 8) | data[1]
                self._asm = [total - 6, bytearray(data[2:8])]
                self.last_rx_id = cid
                self.can.write_can(self.tx_id, self._pad(bytes([0x30, 0x00, 0x00])))
                continue
            if pci == 0x2 and self._asm is not None:
                need, buf = self._asm
                take = data[1:1 + min(7, max(0, need))]
                buf += take
                need -= len(take)
                if need <= 0:
                    self._asm = None
                    return bytes(buf)
                self._asm = [need, buf]
                continue
        return None

    def send(self, resp: bytes):
        resp = bytes(resp)
        if len(resp) <= 7:
            self.can.write_can(self.tx_id, self._pad(bytes([len(resp)]) + resp))
            return
        self.can.write_can(self.tx_id,
                           bytes([0x10 | ((len(resp) >> 8) & 0x0F), len(resp) & 0xFF]) + resp[:6])
        bs, stmin = self._await_fc()
        t = self.timing
        idx, sn, in_block = 6, 1, 0
        while idx < len(resp):
            # Bench fault injection: drop one consecutive frame. We skip its 7
            # bytes without sending them, so the message arrives 7 bytes short
            # and the tester hits a consecutive-frame timeout -- exactly the
            # mid-transfer gap we want a flashing tool to survive.
            if t.drop_cf_prob and random.random() < t.drop_cf_prob:
                self._log("  (timing: DROPPED CF sn=%d @off %d -- tester should time out)"
                          % (sn, idx))
                idx += 7
                sn = (sn + 1) & 0x0F
                in_block += 1
                continue
            cf = bytes([0x20 | (sn & 0x0F)]) + resp[idx:idx + 7]
            self.can.write_can(self.tx_id, self._pad(cf))
            idx += 7
            sn = (sn + 1) & 0x0F
            in_block += 1
            if idx < len(resp):
                # Inter-frame gap: the larger of the tester's requested STmin and
                # the ECU's own floor (the real car held ~10 ms/frame regardless).
                gap = max(_stmin_seconds(stmin), t.jitter(t.cf_gap_ms) / 1000.0)
                if gap:
                    time.sleep(gap)
                if bs and in_block >= bs:
                    bs, stmin = self._await_fc()
                    in_block = 0

    def _await_fc(self, timeout_ms: int = 400) -> Tuple[int, int]:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            for cid, data in self._frames(timeout_ms=20):
                if cid == self.rx_id and (data[0] >> 4) == 0x3:
                    fs = data[0] & 0x0F
                    if fs == 0x1:
                        deadline = time.time() + timeout_ms / 1000.0
                        continue
                    if fs == 0x2:
                        return (0, 0)
                    bs = data[1] if len(data) > 1 else 0
                    stmin = data[2] if len(data) > 2 else 0
                    return (bs, stmin)
        return (0, 0)


def set_rx_filters(ch, ids, log=None):
    """Accept only these CAN ids on a J2534 / 32-bit-bridge / OpenPort channel.
    Only the in-process vendor ``Channel`` has ``pass_ids``; the bridge channel
    and the direct OpenPort channel do NOT, so a bare ``ch.pass_ids(...)`` there
    silently does nothing and the channel then receives NOTHING. Fall back to
    per-id acceptance filters (every channel has ``add_filter``), then to
    ``pass_all``. Returns the method actually used."""
    log = log or (lambda s: None)
    ids = [int(i) for i in ids]
    tag = ", ".join("0x%03X" % i for i in ids)
    if hasattr(ch, "pass_ids"):
        try:
            ch.pass_ids(ids)
            log("RX filter: pass_ids(%s)" % tag)
            return "pass_ids"
        except Exception as ex:
            log("pass_ids failed (%s); trying per-id filters" % ex)
    if hasattr(ch, "add_filter"):
        try:
            if hasattr(ch, "clear_filters"):
                ch.clear_filters()
        except Exception:
            pass
        try:
            from . import j2534 as _j
            mask = (0x7FF).to_bytes(4, "big")
            for cid in ids:
                ch.add_filter(_j.PASS_FILTER, mask, cid.to_bytes(4, "big"), 0)
            log("RX filter: add_filter per id (%s)" % tag)
            return "add_filter"
        except Exception as ex:
            log("add_filter failed (%s); falling back to pass_all" % ex)
    try:
        ch.pass_all()
        log("RX filter: pass_all (whole bus -- may overflow on a busy bus)")
        return "pass_all"
    except Exception as ex:
        log("WARNING: no RX filter set (%s) -- the channel may receive nothing" % ex)
        return "none"


def bus_check(ch, log=None, seconds=1.5):
    """Briefly accept ALL ids and report what traffic this channel sees, so a
    silent sim ("0 matched frames") can be told apart from a device that simply
    is not on the bus. Leaves the channel with a pass-all filter cleared; the
    caller sets the real RX filter afterwards."""
    log = log or (lambda s: None)
    try:
        if hasattr(ch, "clear_filters"):
            try:
                ch.clear_filters()
            except Exception:
                pass
        ch.pass_all()
    except Exception as ex:
        log("bus check: could not pass_all (%s)" % ex)
        return
    seen = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            for m in ch.read(max_msgs=64, timeout_ms=100):
                cid = getattr(m, "can_id", None)
                if cid is not None and not getattr(m, "is_tx_echo", False):
                    seen[cid] = seen.get(cid, 0) + 1
        except Exception as ex:
            log("bus check: read error (%s)" % ex)
            break
    total = sum(seen.values())
    if total == 0:
        log("BUS CHECK: 0 frames in %.1fs with pass_all -- this device sees NO CAN "
            "traffic at all. The sim can't answer what it can't hear: check wiring "
            "/ termination, that it is on the SAME bus+connector as the tool, and "
            "the baud (500k)." % seconds)
    else:
        top = sorted(seen, key=lambda i: -seen[i])[:12]
        log("BUS CHECK: %d frames in %.1fs across %d id(s); top: %s"
            % (total, seconds, len(seen),
               ", ".join("0x%03X(%d)" % (i, seen[i]) for i in top)))
        log("  tester request id 0x7E1 seen on this device: %s"
            % ("YES" if 0x7E1 in seen
               else "NO -- the tool's request is not reaching this device"))
    try:
        if hasattr(ch, "clear_filters"):
            ch.clear_filters()
    except Exception:
        pass


@dataclass
class EcuProfile:
    """What a module *is*, for the simulator: its ids, identification, live
    DIDs and seed/key algorithm(s). Everything the dispatch needs that differs
    between modules."""
    name: str
    title: str
    rx_id: int = RX_ID
    tx_id: int = TX_ID
    func_id: int = FUNC_ID
    idents: Dict[int, bytes] = field(default_factory=dict)      # DID -> payload
    live: Dict[int, bytes] = field(default_factory=dict)        # measuring DIDs
    #: sendKey subfunction -> fn(seed_bytes) -> expected key bytes. A seed sub
    #: is the matching even-1; absent entry => accept any key (logged).
    key_fns: Dict[int, Callable[[bytes], bytes]] = field(default_factory=dict)
    #: optional fixed seed to issue per requestSeed sub (else a nonzero default)
    seed_values: Dict[int, bytes] = field(default_factory=dict)
    #: block_id -> (base, length) so the 0x0202 checksum/verify routine can
    #: compare a supplied CRC-32 against the RESIDENT block in the served image
    verify_blocks: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    #: optional read gate: when set, readMemoryByAddress / requestUpload are
    #: refused (7F serviceNotSupportedInActiveSession) outside this diagnostic
    #: session, and (33 securityAccessDenied) until the sendKey subfunction
    #: ``read_level`` has been accepted in it; a session change relocks.
    #: None = serve reads in any state (the long-standing default).
    read_session: Optional[int] = None
    read_level: Optional[int] = None
    #: optional fn(image) -> {DID: payload}: identification the module answers
    #: from its OWN served image (merged over ``idents``; re-read after a
    #: download replaces the image, as a reflashed module would)
    idents_from_image: Optional[Callable[[bytes], Dict[int, bytes]]] = None


class EcuResponder:
    """Generic UDS-over-ISO-TP responder, parameterised by an EcuProfile.
    ``handle(req)`` returns the response payload (unframed), or None."""

    def __init__(self, profile: EcuProfile, image: bytes = b"",
                 read_mode: str = "serve", strict_key: bool = False,
                 log: Optional[Callable[[str], None]] = None,
                 capture_dir: Optional[str] = None,
                 erase_delay_s: float = 0.0, erase_pending: bool = True,
                 pending_interval_s: float = 0.5):
        self.profile = profile
        self.image = bytes(image)
        self.log = log or (lambda s: None)
        self._refresh_idents()
        self.read_mode = read_mode
        self.strict_key = strict_key
        self.capture_dir = capture_dir
        # Bench knob: hold the eraseMemory (31 01 FF00) reply this many seconds,
        # optionally emitting responsePending (7F 31 78) keepalives every
        # pending_interval_s, to observe how a flashing tool tolerates erase
        # latency (silent vs keepalive). 0 = answer immediately.
        self.erase_delay_s = erase_delay_s
        self.erase_pending = erase_pending
        self.pending_interval_s = pending_interval_s
        self.session = 0x01
        self.seeds: Dict[int, bytes] = {}
        self.unlocked: set = set()
        self._upload: Optional[dict] = None
        self._download: Optional[dict] = None

    # ---- helpers ----
    def _refresh_idents(self):
        fn = self.profile.idents_from_image
        if fn is None or not self.image:
            return
        try:
            found = fn(self.image)
        except Exception as ex:                      # pragma: no cover - defensive
            self.log("  identification from image failed: %s" % ex)
            return
        if found:
            self.profile.idents.update(found)
            self.log("  identification from the served image: " + ", ".join(
                "%04X=%s" % (d, bytes(v).rstrip(b"\0").decode("latin-1")) for d, v in sorted(found.items())))

    def _mem(self, addr: int, length: int) -> bytes:
        img = self.image
        if not img:
            return bytes(length)
        if 0 <= addr < len(img):
            out = img[addr:addr + length]
        else:
            base = addr & (len(img) - 1) if (len(img) & (len(img) - 1)) == 0 else addr % len(img)
            out = img[base:base + length]
        return bytes(out) + bytes(max(0, length - len(out)))

    def _seed_for(self, sub: int) -> bytes:
        seed = self.profile.seed_values.get(sub) or bytes([0x10 + sub, 0x20, 0x30, 0x40])
        self.seeds[sub] = bytes(seed)
        return bytes(seed)

    def _expected_key(self, send_sub: int, seed: bytes) -> Optional[bytes]:
        fn = self.profile.key_fns.get(send_sub)
        if fn is None:
            return None
        try:
            return bytes(fn(seed))
        except Exception as ex:                      # pragma: no cover - defensive
            self.log("  key algorithm error: %s" % ex)
            return None

    # ---- dispatch ----
    def handle(self, req: bytes) -> Optional[bytes]:
        if not req:
            return None
        s = req[0]
        if s == SID_TESTER_PRESENT:
            # honour suppressPositiveResponse (subfunction bit 0x80): a 3E 80
            # keepalive gets NO reply, exactly as a real ECU -- so a tester can
            # hold the session alive during a silent erase without injecting
            # stray responses into the stream it is reading.
            if len(req) > 1 and (req[1] & 0x80):
                return None
            return bytes([SID_TESTER_PRESENT | POS, 0x00])
        if s == SID_RESET:
            self.log("  ECUReset (11 %02X)" % (req[1] if len(req) > 1 else 0))
            return bytes([SID_RESET | POS, req[1] if len(req) > 1 else 0x01])
        if s == SID_SESSION:
            new = req[1] if len(req) > 1 else self.session
            if self.profile.read_session is not None and new != self.session and self.unlocked:
                self.log("  session 0x%02X -> 0x%02X: security relocked" % (self.session, new))
                self.unlocked.clear()
            self.session = new
            return bytes([SID_SESSION | POS, self.session, 0x00, 0x32, 0x01, 0xF4])
        if s == SID_DTC_SETTING:
            return bytes([SID_DTC_SETTING | POS, req[1] if len(req) > 1 else 0x00])
        if s == SID_COMM_CONTROL:
            return bytes([SID_COMM_CONTROL | POS, req[1] if len(req) > 1 else 0x00])
        if s == SID_CLEAR_DTC:
            return bytes([SID_CLEAR_DTC | POS])
        if s == SID_READ_DATA:
            return self._read_did(req)
        if s == SID_WRITE_DATA:
            did = ((req[1] << 8) | req[2]) if len(req) >= 3 else 0
            self.log("  writeDataByIdentifier 0x%04X data=%s (accepted)"
                     % (did, bytes(req[3:]).hex()))
            return bytes([SID_WRITE_DATA | POS, (did >> 8) & 0xFF, did & 0xFF])
        if s == SID_SECURITY:
            return self._security(req)
        if s == SID_READ_MEM:
            return self._read_memory(req)
        if s == SID_REQUEST_UPLOAD:
            return self._request_upload(req)
        if s == SID_TRANSFER_DATA:
            if self._download is not None:
                return self._download_data(req)
            return self._transfer_data(req)
        if s == SID_TRANSFER_EXIT:
            if self._download is not None:
                self.log("  requestTransferExit: block download complete, %d byte(s) in %d frame(s)"
                         % (self._download.get("received", 0), self._download.get("frames", 0)))
                self._finish_download(self._download)
                self._download = None
                return bytes([SID_TRANSFER_EXIT | POS])
            self._upload = None
            return bytes([SID_TRANSFER_EXIT | POS])
        if s == SID_ROUTINE:
            return self._routine(req)
        if s == SID_REQUEST_DOWNLOAD:
            return self._request_download(req)
        return nrc(s, NRC_SERVICE_NOT_SUPPORTED)

    def _read_did(self, req: bytes) -> bytes:
        """ReadDataByIdentifier, one or more DIDs (ISO 14229: the reply
        carries every supported DID in request order; 31 only if none is)."""
        if len(req) < 3 or (len(req) - 1) % 2:
            return nrc(SID_READ_DATA, NRC_WRONG_LENGTH)
        out = bytearray([SID_READ_DATA | POS])
        for k in range(1, len(req), 2):
            did = (req[k] << 8) | req[k + 1]
            val = self.profile.idents.get(did)
            if val is None:
                val = self.profile.live.get(did)
            if val is not None:
                out += bytes([req[k], req[k + 1]]) + bytes(val)
        if len(out) == 1:
            return nrc(SID_READ_DATA, NRC_OUT_OF_RANGE)
        return bytes(out)

    def _security(self, req: bytes) -> bytes:
        if len(req) < 2:
            return nrc(SID_SECURITY, NRC_WRONG_LENGTH)
        sub = req[1]
        if sub % 2 == 1:                             # requestSeed
            seed = self._seed_for(sub)
            if not any(seed):                        # zero seed = level already open
                self.unlocked.add(sub + 1)
            return bytes([SID_SECURITY | POS, sub]) + seed
        seed_sub = sub - 1                           # sendKey
        seed = self.seeds.get(seed_sub)
        key = bytes(req[2:])
        if seed is None:
            return nrc(SID_SECURITY, NRC_SEQUENCE)
        expected = self._expected_key(sub, seed)
        if expected is not None:
            ok = key == expected
            self.log("  sendKey level %02X: tool=%s expected=%s -> %s"
                     % (sub, key.hex(), expected.hex(), "MATCH" if ok else "MISMATCH"))
            if not ok and self.strict_key:
                return nrc(SID_SECURITY, NRC_INVALID_KEY)
        else:
            self.log("  sendKey level %02X: tool=%s (no algorithm in profile to check)"
                     % (sub, key.hex()))
        self.unlocked.add(sub)
        return bytes([SID_SECURITY | POS, sub])

    def _read_gate(self, sid: int) -> Optional[bytes]:
        """The profile's read precondition (session + unlocked level), or None."""
        p = self.profile
        if p.read_session is not None and self.session != p.read_session:
            self.log("  0x%02X refused: session 0x%02X, reads need 0x%02X"
                     % (sid, self.session, p.read_session))
            return nrc(sid, NRC_SERVICE_NOT_IN_SESSION)
        if p.read_level is not None and p.read_level not in self.unlocked:
            self.log("  0x%02X refused: read security (27 %02X/%02X) not unlocked"
                     % (sid, p.read_level - 1, p.read_level))
            return nrc(sid, NRC_SECURITY_DENIED)
        return None

    def _read_memory(self, req: bytes) -> bytes:
        if self.read_mode != "serve":
            return nrc(SID_READ_MEM, NRC_SERVICE_NOT_IN_SESSION)
        gate = self._read_gate(SID_READ_MEM)
        if gate is not None:
            return gate
        if len(req) < 2:
            return nrc(SID_READ_MEM, NRC_WRONG_LENGTH)
        alfid = req[1]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        if len(req) < 2 + addr_len + size_len or addr_len == 0 or size_len == 0:
            return nrc(SID_READ_MEM, NRC_WRONG_LENGTH)
        addr = int.from_bytes(req[2:2 + addr_len], "big")
        length = int.from_bytes(req[2 + addr_len:2 + addr_len + size_len], "big")
        length = max(0, min(length, 0x400))
        return bytes([SID_READ_MEM | POS]) + self._mem(addr, length)

    def _request_upload(self, req: bytes) -> bytes:
        if self.read_mode != "serve":
            return nrc(SID_REQUEST_UPLOAD, NRC_SERVICE_NOT_SUPPORTED)
        gate = self._read_gate(SID_REQUEST_UPLOAD)
        if gate is not None:
            return gate
        if len(req) < 3:
            return nrc(SID_REQUEST_UPLOAD, NRC_WRONG_LENGTH)
        alfid = req[2]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        addr = int.from_bytes(req[3:3 + addr_len], "big") if addr_len else 0
        size = int.from_bytes(req[3 + addr_len:3 + addr_len + size_len], "big") if size_len else 0
        self._upload = {"addr": addr, "size": size, "pos": 0, "block": 1, "chunk": 0x100}
        return bytes([SID_REQUEST_UPLOAD | POS, 0x20, 0x01, 0x02])

    def _transfer_data(self, req: bytes) -> bytes:
        if self._upload is None:
            return nrc(SID_TRANSFER_DATA, NRC_SEQUENCE)
        bsc = req[1] if len(req) > 1 else self._upload["block"]
        st = self._upload
        n = min(st["chunk"], max(0, st["size"] - st["pos"]))
        data = self._mem(st["addr"] + st["pos"], n)
        st["pos"] += n
        st["block"] = (bsc + 1) & 0xFF
        return bytes([SID_TRANSFER_DATA | POS, bsc]) + data

    def _request_download(self, req: bytes) -> bytes:
        """requestDownload for a WRITE capture: 34 <dfi> <alfid> <block/addr>
        <size>. VAG writes address by a 1-byte block id (alfid 0x41). We log
        it and accept (nothing is really written -- this is a responder) so the
        tool proceeds into transferData and we capture the whole sequence."""
        dfi = req[1] if len(req) > 1 else 0
        alfid = req[2] if len(req) > 2 else 0
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        block = int.from_bytes(req[3:3 + addr_len], "big") if addr_len else 0
        size = int.from_bytes(req[3 + addr_len:3 + addr_len + size_len], "big") if size_len else 0
        self.log("  requestDownload: dfi=0x%02X alfid=0x%02X block/addr=0x%X size=%d (0x%X)"
                 % (dfi, alfid, block, size, size))
        self._download = {"block": block, "size": size, "received": 0,
                          "frames": 0, "buf": bytearray(), "dfi": dfi}
        self._upload = None
        return bytes([SID_REQUEST_DOWNLOAD | POS, 0x20, 0x08, 0x02])

    def _download_data(self, req: bytes) -> bytes:
        bsc = req[1] if len(req) > 1 else 0
        data = bytes(req[2:])
        st = self._download
        if bsc == st.get("last_bsc"):
            # ISO14229 same-bsc retransmission: re-send the ack, do NOT re-apply.
            self.log("  transferData bsc=0x%02X re-sent -- de-duped" % bsc)
            return bytes([SID_TRANSFER_DATA | POS, bsc])
        st["last_bsc"] = bsc
        st["received"] += len(data)
        st["frames"] += 1
        st["buf"] += data
        if st["frames"] == 1 or st["frames"] % 64 == 0:
            self.log("  transferData #%d bsc=0x%02X (+%d, total %d)"
                     % (st["frames"], bsc, len(data), st["received"]))
        return bytes([SID_TRANSFER_DATA | POS, bsc])

    def _finish_download(self, st: dict) -> None:
        """When a block's download completes, fingerprint exactly what the tool
        put on the wire: the raw transferData stream and -- when it is a BCB
        (``1A01``) stream -- the plaintext it decodes to and that plaintext's
        CRC-32 (the value the real 0x0202 routine checks). Optionally save both
        to ``capture_dir`` so a bench write can be compared byte-for-byte with
        what OVT-Suite's own encoder would produce for the same block."""
        buf = bytes(st.get("buf", b""))
        blk = st.get("block", 0)
        if not buf:
            return
        raw_crc = zlib.crc32(buf) & 0xFFFFFFFF
        self.log("  capture blk=0x%02X dfi=0x%02X raw=%d bytes raw_crc32=0x%08X"
                 % (blk, st.get("dfi", 0), len(buf), raw_crc))
        plain = None
        mi = buf.find(_crypto.ZF6HP_BCB_MARKER)
        if st.get("dfi", 0) == _crypto.ZF8HP_DFI:
            # ZF 8HP Gen1: XOR + LZSS, delimited by the requestDownload size
            try:
                plain = _crypto.zf8hp_frf_decode(buf, int(st.get("size", 0)))
                self.log("    dfi 0x22 (ZF 8HP XOR+LZSS) -> plaintext %d bytes crc32=0x%08X"
                         % (len(plain), zlib.crc32(plain) & 0xFFFFFFFF))
            except Exception as ex:
                self.log("    dfi 0x22 decode failed: %s" % ex)
        elif st.get("dfi", 0) == _crypto.ZF8HP_GEN2_DFI:
            # ZF 8HP Gen2 (ALX52): AES-128-CBC + 10-bit-distance LZSS
            try:
                plain = _crypto.zf8hp_gen2_frf_decode(buf, int(st.get("size", 0)))
                self.log("    dfi 0xAA (ZF 8HP Gen2 AES+LZSS) -> plaintext %d bytes crc32=0x%08X"
                         % (len(plain), zlib.crc32(plain) & 0xFFFFFFFF))
            except Exception as ex:
                self.log("    dfi 0xAA decode failed: %s" % ex)
        elif mi >= 0:
            try:
                plain = _crypto.zf6hp_bcb_decode(buf[mi:])
                self.log("    BCB marker @%d -> plaintext %d bytes crc32=0x%08X"
                         % (mi, len(plain), zlib.crc32(plain) & 0xFFFFFFFF))
            except Exception as ex:
                self.log("    BCB decode failed: %s" % ex)
        else:
            self.log("    (no BCB marker in stream; dfi=0x%02X)" % st.get("dfi", 0))
        # Apply the decoded block to the RESIDENT image so a subsequent 0x0202
        # verify (and a later read) see the just-written content -- makes the
        # simulator a faithful write target for end-to-end tests.
        if plain is not None:
            base, ln = self.profile.verify_blocks.get(blk, (None, None))
            if (base is not None and self.image and base + ln <= len(self.image)
                    and len(plain) == ln):
                self.image = self.image[:base] + bytes(plain) + self.image[base + ln:]
                self._refresh_idents()
                self.log("    applied %d bytes to resident image @0x%06X" % (ln, base))
        if self.capture_dir:
            try:
                os.makedirs(self.capture_dir, exist_ok=True)
                base = os.path.join(self.capture_dir, "xfer-blk0x%02X" % blk)
                with open(base + "-raw.bin", "wb") as fh:
                    fh.write(buf)
                if plain is not None:
                    with open(base + "-plain.bin", "wb") as fh:
                        fh.write(plain)
                self.log("    saved %s-raw.bin%s"
                         % (base, " + -plain.bin" if plain is not None else ""))
            except Exception as ex:
                self.log("    could not save capture: %s" % ex)

    def _routine(self, req: bytes) -> bytes:
        if len(req) < 4:
            return nrc(SID_ROUTINE, NRC_WRONG_LENGTH)
        sub, rid = req[1], (req[2] << 8) | req[3]
        data = bytes(req[4:])
        names = {0x0203: "checkProgrammingPreconditions",
                 0xFF01: "checkProgrammingDependencies",
                 0xFF00: "eraseMemory", 0x0202: "checksum/verify"}
        label = names.get(rid, "routine")
        status = 0x00
        if rid == 0x0202 and sub == 0x01:
            self.log("  *** 0x0202 %s: data=%s  <-- block/len + EXPECTED CHECKSUM ***"
                     % (label, data.hex()))
            status = self._verify_resident(data)
        elif rid == 0xFF00 and sub == 0x01:
            self.log("  0xFF00 eraseMemory: data=%s" % data.hex())
        else:
            self.log("  routineControl sub=%02X id=%04X (%s) data=%s"
                     % (sub, rid, label, data.hex()))
        # routineControl positive: echo sub + routineId + a routineStatusRecord
        # byte. For 0x0202 that byte reports the resident-vs-supplied checksum
        # verdict (0x00 match, 0x01 mismatch); for the others 0x00 = completed.
        # ODIS aborts an erase whose reply lacks the status record, so every
        # routine reply carries one.
        return bytes([SID_ROUTINE | POS, sub, req[2], req[3], status])

    def _verify_resident(self, data: bytes) -> int:
        """0x0202 routineControlOptionRecord is ``01 <block_id> 00 04 <crc:4>``:
        checksum the RESIDENT block in the served image and report 0x00 if it
        equals the supplied CRC-32, else 0x01 (so a bench write can exercise the
        skip-unchanged path). Reports 0x00 when there is no image/range to check
        against (can't fault what we can't see)."""
        try:
            bid = data[1]
            expected = int.from_bytes(data[4:8], "big")
            base, ln = self.profile.verify_blocks.get(bid, (None, None))
            if base is None or not self.image or base + ln > len(self.image):
                self.log("    0x0202 verify blk=0x%02X: no image/range -> OK" % bid)
                return 0x00
            actual = zlib.crc32(self.image[base:base + ln]) & 0xFFFFFFFF
            ok = (actual == expected)
            self.log("    0x0202 verify blk=0x%02X expected=0x%08X resident=0x%08X -> %s"
                     % (bid, expected, actual, "MATCH" if ok else "MISMATCH"))
            return 0x00 if ok else 0x01
        except Exception as ex:
            self.log("    0x0202 verify parse error: %s" % ex)
            return 0x00


# ----------------------------------------------------------------- profiles --
_GS19_IDENT: Dict[int, bytes] = {
    0xF187: b"8K0927158C ",        # VW spare part number
    0xF189: b"0020",               # software version
    0xF191: b"0B6927156 ",         # hardware part number
    0xF197: b"GS19",               # ECU / system name
    0xF19E: b"EV_TCMAL651",        # ASAM-ODX identifier
    0xF18A: b"0007",
    0xF1A2: b"0001",
}
_GS19_LIVE: Dict[int, bytes] = {
    0x0285: b"\x77", 0x0288: b"\x01", 0x0805: b"\x0A",
    0x2104: b"\x4E", 0xF40C: b"\x00\x00",
}


def gs19_profile() -> EcuProfile:
    """ZF 6HP28 (GS19) TCU -- UDS on 0x7E1/0x7E9, read key seed+0x11C5A (27 03/04),
    write key SA2 tape (27 11/12)."""
    return EcuProfile(
        name="6hp28", title="ZF 6HP28 (GS19) TCU",
        idents=dict(_GS19_IDENT), live=dict(_GS19_LIVE),
        key_fns={0x04: _crypto.zf6hp_read_key, 0x12: _crypto.zf6hp_write_key},
        seed_values={0x03: b"\x11\x22\x33\x44", 0x11: b"\x55\x66\x77\x88"},
        verify_blocks={0x01: (0x080000, 0x0FFF80), 0x02: (0x040000, 0x03FF80),
                       0x03: (0x020080, 0x01FF80), 0x04: (0x180000, 0x07FF80)})


#: Fallback identification (no image served): the real records of the
#: 8K0927158AD_1005 image (ZX8L4400). With an image the profile answers that
#: image's own records instead (zf8hp.uds_identification). F191/F1A3 (hardware
#: number / version) are not in the flash image and are not invented here.
_ZF8HP_IDENT: Dict[int, bytes] = {
    0xF187: b"8K0927158AD",                         # I14229APPL_kid_VAG_SWNr
    0xF189: b"1005",                                # version record / kid_VAG_SWIndex
    0xF197: b"0BK 20TFSIUSA",                       # kid_VAG_SysBez
    0xF19E: b"EV_TCMAL551211".ljust(26, b"\0"),     # kid_OdxIdentifier (26-byte record)
    0xF1A2: b"002022",                              # kid_OdxFileVer
}


def _zf8hp_idents(image: bytes) -> Dict[int, bytes]:
    from ..platforms import zf8hp as _zf8hp
    return _zf8hp.uds_identification(image)


#: Fallback Gen2 identification (no image served): a real 4M0 Q7 build.
_ZF8HP_GEN2_IDENT: Dict[int, bytes] = {
    0xF187: b"4M0927158AA", 0xF189: b"1009", 0xF19E: b"EV_TCMALX52011".ljust(26, b"\0"),
    0xF1A2: b"002011",
}


def zf8hp_profile() -> EcuProfile:
    """ZF 8HP (Gen1, 8K0/8R0927158 / 0BK910255) TCU -- UDS on 0x7E1/0x7E9.

    KNOWN (from the TCU's own FRFs, see ovtsuite.zf8hp): block ids 01..04 with
    their bases/lengths in the 2 MB flash (verify_blocks -- the 0x0202
    routine compares zlib.crc32 of the resident plaintext block, exactly the
    ODX CRC32F), dfi 0x22 transferData (decoded in _finish_download), and the
    write-security tape == the 6HP28's (27 11/12 via zf6hp_write_key).
    ASSUMED (Bear, 2026-10-10: the write key is shared, so the read access very
    likely is too): the 6HP28's read access -- session 0x4F, read level 27 03/04
    with key = seed + 0x11C5A (zf6hp_read_key), then 0x23 ALFID 0x14. The
    profile EXPECTS it: 0x23/0x35 are refused outside 0x4F (7F) or before the
    read level is accepted (33); with strict_key a wrong key is 35 invalidKey,
    otherwise logged as MISMATCH and let through so a tool's real exchange can
    still be captured. Serve it a 2 MB image from Reference/ZF8HP/OEM."""
    return EcuProfile(
        name="8hp", title="ZF 8HP (Gen1 AL551) TCU",
        idents=dict(_ZF8HP_IDENT), live={},
        key_fns={0x04: _crypto.zf6hp_read_key, 0x12: _crypto.zf6hp_write_key},
        seed_values={0x03: b"\x11\x22\x33\x44", 0x11: b"\x55\x66\x77\x88"},
        read_session=0x4F, read_level=0x04,
        idents_from_image=_zf8hp_idents,
        verify_blocks={0x01: (0x040000, 0x13FE00), 0x02: (0x006000, 0x001E00),
                       0x03: (0x020000, 0x01FE00), 0x04: (0x180200, 0x07FE00)})


def zf8hp_gen2_profile() -> EcuProfile:
    """ZF 8HP Gen2 (ALX52, 4M0 family: Q7/Q8 4M, A4 B9, Q5 FY) TCU -- UDS on
    0x7E1/0x7E9. Like the Gen1 "8hp" profile but the 3 MB Gen2 flash layout
    (cal 0x260200) and dfi 0xAA transferData (AES-128-CBC + LZSS, decoded in
    _finish_download). Identification is read from the served image's own
    records (zf8hp.uds_identification). Read access and the write tape are
    ASSUMED shared with Gen1 (unverified on a car). Serve it a 3 MB 4M0-family
    image assembled from a Reference/ZF8HP/SGO/ALX52 FRF."""
    return EcuProfile(
        name="8hp-gen2", title="ZF 8HP Gen2 (ALX52) TCU",
        idents=dict(_ZF8HP_GEN2_IDENT), live={},
        key_fns={0x04: _crypto.zf6hp_read_key, 0x12: _crypto.zf8hp_gen2_write_key},
        seed_values={0x03: b"\x11\x22\x33\x44", 0x11: b"\x55\x66\x77\x88"},
        read_session=0x4F, read_level=0x04,
        idents_from_image=_zf8hp_idents,
        verify_blocks={0x01: (0x050200, 0x20FE00), 0x02: (0x006200, 0x001E00),
                       0x03: (0x020200, 0x02FE00), 0x04: (0x260200, 0x09FE00)})


# ---- Aisin 8-speed (VW "TR-80SD": 0C8 / 09G) -------------------------------
# The TCU's WRITE-level SA2 tapes ship in the clear in its own FRFs (same place
# as the 6HP28's). Kept here (not crypto.py) because so far they are only needed
# by the sim; move them to crypto.py when the real flasher reuses them. Verified
# this session against the 0C8 and 09G FRFs (see project doc
# ``trx80_aisin_8speed_frf_map``). The 0C8 tape covers AL991 + AQ250; AQ450 (09G)
# differs only in its leading FOR count.
AISIN8_0C8_WRITE_TAPE = bytes.fromhex("6803819354414a494a058753414a49494c")
AISIN8_09G_WRITE_TAPE = bytes.fromhex("6805819354414a494a058753414a49494c")


def _sa2_key_fn(tape: bytes) -> Callable[[bytes], bytes]:
    """A sendKey fn for a baked SA2 tape: seed(4 bytes, BE) -> key(4 bytes, BE),
    via crypto.sa2_execute -- the same convention as crypto.zf6hp_write_key."""
    def fn(seed4: bytes) -> bytes:
        seed32 = int.from_bytes(bytes(seed4)[:4], "big")
        return _crypto.sa2_execute(tape, seed32).to_bytes(4, "big")
    return fn


#: Fallback identification for the AL991/0C8 generation (used when no image is
#: served). Real records of 0C8927750J_1781 (the plaintext image dissected this
#: session). F187=spare PN, F189=SW version, F19E=ODX ECU-variant id.
_AISIN8_0C8_IDENT: Dict[int, bytes] = {
    0xF187: b"0C8927750J",
    0xF189: b"1781",
    0xF19E: b"EV_TCMAL991031".ljust(26, b"\0"),
}
_AISIN8_09G_IDENT: Dict[int, bytes] = {
    0xF187: b"09G927158A",
    0xF189: b"3387",
    0xF19E: b"EV_TCMAQ450031".ljust(26, b"\0"),
}


def aisin8_profile() -> EcuProfile:
    """Aisin 8-speed (VW "TR-80SD", 0C8: AL991 + AQ250) TCU -- UDS on 0x7E1/0x7E9.

    KNOWN (from the TCU's own FRFs, see project doc ``trx80_aisin_8speed_frf_map``):
    2 flash blocks (01 big, 02 small); the WRITE-security SA2 tape
    (:data:`AISIN8_0C8_WRITE_TAPE`, requestSeed 0x11 / sendKey 0x12). The early
    AL991 gen is stored plaintext in its FRF, so its real flash can be served:
    block0 maps to 0x040000 (len 0x0FFFF0 checksummed), block1 to 0x005000 (len
    0x03AFF0) per the on-image checksum trailers.

    The 0x0202 verify routine CRC-32s the resident region; the early AL991 gen
    actually uses a 16-bit BYTESUM16 (algorithm not yet solved -- see the doc),
    so treat a verify result for a served AL991 image as geometry-only, not a
    pass/fail. Read access is left open (served in any state) -- the module's
    real read gating is not yet known. The later AQ250/AQ450 gens store their
    blocks compressed (``meth=a0``, not yet decoded), so only the plaintext AL991
    can be served as a real image today."""
    return EcuProfile(
        name="aisin8", title="Aisin 8-speed TR-80SD (AL991/AQ250) TCU",
        idents=dict(_AISIN8_0C8_IDENT), live={},
        key_fns={0x12: _sa2_key_fn(AISIN8_0C8_WRITE_TAPE)},
        seed_values={0x11: b"\x11\x22\x33\x44"},
        verify_blocks={0x01: (0x040000, 0x0FFFF0), 0x02: (0x005000, 0x03AFF0)})


def aisin8_aq450_profile() -> EcuProfile:
    """Aisin 8-speed (VW "TR-80SD", 09G: AQ450, newest gen) TCU -- UDS on
    0x7E1/0x7E9. Like :func:`aisin8_profile` but the 09G WRITE SA2 tape
    (:data:`AISIN8_09G_WRITE_TAPE`). Its FRF blocks are compressed (``meth=a0``,
    not yet decoded) so no real image can be served yet; the profile still
    answers session / SA2 / identification for exercising a tool's handshake.
    ``verify_blocks`` is left empty until the a0 decode gives the flash bases."""
    return EcuProfile(
        name="aisin8-aq450", title="Aisin 8-speed TR-80SD (AQ450) TCU",
        idents=dict(_AISIN8_09G_IDENT), live={},
        key_fns={0x12: _sa2_key_fn(AISIN8_09G_WRITE_TAPE)},
        seed_values={0x11: b"\x11\x22\x33\x44"})


def aisin8_image_from_frf(src) -> bytes:
    """Assemble a flat flash image from a plaintext (early AL991) Aisin FRF:
    place each uncompressed (``ENCRYPT-COMPRESS-METHOD=00``) DATA block at the
    flash base in its own checksum trailer, into a 0xFF-filled image. Returns
    ``b""`` for an FRF whose blocks are compressed (``meth=a0``) or anything that
    is not an Aisin plaintext FRF, so callers can fall back. ``src`` is a path or
    raw FRF bytes."""
    import struct as _struct
    import xml.etree.ElementTree as _ET
    try:
        from .. import frf as _frf
    except Exception:
        return b""
    data = src if isinstance(src, (bytes, bytearray)) else open(src, "rb").read()
    try:
        if not _frf.detect(bytes(data)):
            return b""
        odx, _name = _frf.unwrap(bytes(data))
        root = _ET.fromstring(odx)
    except Exception:
        return b""
    for el in root.iter():
        el.tag = el.tag.split('}')[-1]
    placed = []                                  # (base, raw)
    for fd in root.findall(".//FLASHDATA"):
        sn = fd.findtext("./SHORT-NAME") or ""
        if not sn.endswith("DATA") or "ERASE" in sn:
            continue
        meth = fd.findtext(".//ENCRYPT-COMPRESS-METHOD")
        de = fd.find(".//DATA")
        if meth != "00" or de is None or not de.text:
            continue                             # compressed (a0) or empty -> skip
        try:
            raw = bytes.fromhex(de.text.strip())
        except ValueError:
            continue
        if len(raw) < 0x10:
            continue
        base = _struct.unpack_from(">I", raw[-16:], 4)[0]   # trailer [count][start]...
        placed.append((base, raw))
    if not placed:
        return b""
    size = max(base + len(raw) for base, raw in placed)
    img = bytearray(b"\xFF" * size)
    for base, raw in placed:
        img[base:base + len(raw)] = raw
    return bytes(img)


def generic_profile() -> EcuProfile:
    """A minimal UDS module on the standard powertrain ids: answers sessions,
    tester-present, a stub identification, accepts any security key, and serves
    reads. A starting point for any ISO-TP module -- override ids and fill in
    idents/key_fns for a specific one."""
    return EcuProfile(
        name="generic", title="Generic UDS module (ISO-TP)",
        idents={0xF187: b"SIMULATED  ", 0xF189: b"0001", 0xF197: b"SIMUDS"})


#: name -> factory (fresh profile each call; profiles hold callables/dicts)
_PROFILE_FACTORIES: Dict[str, Callable[[], EcuProfile]] = {
    "6hp28": gs19_profile,
    "8hp": zf8hp_profile,
    "8hp-gen2": zf8hp_gen2_profile,
    "aisin8": aisin8_profile,
    "aisin8-aq450": aisin8_aq450_profile,
    "generic": generic_profile,
}


def profile_names() -> List[str]:
    return list(_PROFILE_FACTORIES)


def profile(name: str) -> EcuProfile:
    return (_PROFILE_FACTORIES.get(name) or _PROFILE_FACTORIES["6hp28"])()


def make_responder(name: str, **kw) -> EcuResponder:
    return EcuResponder(profile(name), **kw)


class Gs19Responder(EcuResponder):
    """Back-compat alias: the 6HP28 profile responder."""

    def __init__(self, image: bytes = b"", read_mode: str = "serve",
                 strict_key: bool = False, log: Optional[Callable[[str], None]] = None,
                 capture_dir: Optional[str] = None, erase_delay_s: float = 0.0,
                 erase_pending: bool = True, pending_interval_s: float = 0.5):
        super().__init__(gs19_profile(), image=image, read_mode=read_mode,
                         strict_key=strict_key, log=log, capture_dir=capture_dir,
                         erase_delay_s=erase_delay_s, erase_pending=erase_pending,
                         pending_interval_s=pending_interval_s)


def _hx(b: bytes) -> str:
    return " ".join("%02X" % x for x in b)


@dataclass
class Simulator:
    """Pump the ISO-TP layer, dispatch to the responder, send replies, log."""
    io: IsoTpEcu
    responder: EcuResponder
    log: Callable[[str], None] = field(default=lambda s: None)
    count: int = 0

    def run(self, is_stop: Optional[Callable[[], bool]] = None, idle_ms: int = 50):
        is_stop = is_stop or (lambda: False)
        self.log("%s simulator listening (rx 0x%03X / func 0x%03X -> tx 0x%03X); %s reads"
                 % (self.responder.profile.title, self.io.rx_id, self.io.func_id,
                    self.io.tx_id, self.responder.read_mode))
        _last_beat = time.time()
        while not is_stop():
            try:
                req = self.io.poll(timeout_ms=idle_ms)
            except Exception as ex:
                self.log("rx error: %s" % ex)
                continue
            now = time.time()
            if req is None:
                if now - _last_beat >= 5.0:
                    self.log("  ...listening (%d frame(s) seen on matched ids, "
                             "%d request(s) handled)" % (self.io.frames_seen, self.count))
                    _last_beat = now
                continue
            self.step(req)
            _last_beat = now

    def step(self, req: bytes):
        self.count += 1
        rid = self.io.last_rx_id if self.io.last_rx_id is not None else self.io.rx_id
        self.log("RX %03X  %s" % (rid, _hx(req)))
        resp = self.responder.handle(req)
        if resp is None:
            self.log("   (no response)")
            return
        # Real-car timing injection (bench): drop the whole response, add a long
        # comms stall, or delay the reply -- so a flashing tool meets the slow /
        # lossy conditions seen on the car. Erase keeps its own hold below.
        t = getattr(self.io, "timing", None)
        if t is not None and t.active():
            if t.drop_resp_prob and random.random() < t.drop_resp_prob:
                self.log("   (timing: DROPPED whole response -- tester should see no reply)")
                return
            if t.stall_every and t.stall_ms and (self.count % t.stall_every == 0):
                self.log("   (timing: stall %.0f ms before reply)" % t.stall_ms)
                time.sleep(t.stall_ms / 1000.0)
            lat = t.jitter(t.resp_latency_ms)
            if lat > 0:
                time.sleep(lat / 1000.0)
        delay = getattr(self.responder, "erase_delay_s", 0.0) or 0.0
        if delay > 0 and req[:4] == bytes([SID_ROUTINE, 0x01, 0xFF, 0x00]):
            self._hold_erase(delay)
        self.log("TX %03X  %s" % (self.io.tx_id, _hx(resp)))
        self.io.send(resp)

    def _hold_erase(self, delay: float):
        """Delay the erase reply by ``delay`` s, optionally emitting
        responsePending (7F 31 78) keepalives, to mimic a slow erase."""
        emit = getattr(self.responder, "erase_pending", True)
        interval = max(0.05, getattr(self.responder, "pending_interval_s", 0.5) or 0.5)
        end = time.time() + delay
        n = 0
        while time.time() < end:
            time.sleep(min(interval, max(0.0, end - time.time())))
            if emit and time.time() < end:
                pend = bytes([0x7F, SID_ROUTINE, NRC_RESPONSE_PENDING])
                self.log("TX %03X  %s   (responsePending)" % (self.io.tx_id, _hx(pend)))
                self.io.send(pend)
                n += 1
        self.log("  held erase %.1fs (%d responsePending frame(s)%s)"
                 % (delay, n, "" if emit else ", silent"))


def _load_image(path: Optional[str]) -> bytes:
    if not path:
        return b""
    data = open(path, "rb").read()
    try:
        from .. import frf as _frf
    except Exception:
        _frf = None
    if _frf is not None:
        try:
            if _frf.detect(data):
                img = bytes(_frf.image(_frf.parse(data, path)).data)
                if img:
                    return img
                # frf.image yields nothing for a non-ZF6HP FRF (its block decode
                # is ZF6HP-tuned). Try the Aisin plaintext assembler before
                # falling back to the raw container bytes.
                aisin = aisin8_image_from_frf(data)
                if aisin:
                    return aisin
        except Exception:
            pass
    return data


def main(argv=None):   # pragma: no cover - hardware/CLI entry point
    import argparse
    from . import j2534 as _j

    ap = argparse.ArgumentParser(
        description="Simulate a UDS module on a CAN bus to capture a tool's "
                    "command/read sequence.")
    ap.add_argument("--dll", required=True, help="J2534 DLL path for the SIM device "
                    "(NOT the device the tool under test uses)")
    ap.add_argument("--profile", choices=profile_names(), default="6hp28",
                    help="module to impersonate (default 6hp28)")
    ap.add_argument("--image", help="FRF container or raw .bin to serve reads from")
    ap.add_argument("--baud", type=int, default=500000)
    ap.add_argument("--rx", type=lambda x: int(x, 0), help="override profile rx id")
    ap.add_argument("--tx", type=lambda x: int(x, 0), help="override profile tx id")
    ap.add_argument("--func", type=lambda x: int(x, 0), help="override profile functional id")
    ap.add_argument("--mode", choices=("serve", "faithful"), default="serve")
    ap.add_argument("--strict-key", action="store_true")
    ap.add_argument("--log", help="write the decoded exchange log to this file too")
    args = ap.parse_args(argv)

    logf = open(args.log, "w", encoding="ascii") if args.log else None

    def log(line: str):
        print(line, flush=True)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    prof = profile(args.profile)
    if args.rx is not None:
        prof.rx_id = args.rx
    if args.tx is not None:
        prof.tx_id = args.tx
    if args.func is not None:
        prof.func_id = args.func
    image = _load_image(args.image)
    if args.image:
        log("image: %s (%d bytes)" % (args.image, len(image)))

    pt = _j.PassThru(args.dll)
    ch = pt.connect(_j.CAN, args.baud)
    bus_check(ch, log=log)
    set_rx_filters(ch, [prof.rx_id, prof.func_id], log=log)
    io = IsoTpEcu(ch, rx_id=prof.rx_id, tx_id=prof.tx_id, func_id=prof.func_id, log=log)
    responder = EcuResponder(prof, image=image, read_mode=args.mode,
                             strict_key=args.strict_key, log=log)
    sim = Simulator(io, responder, log=log)
    try:
        sim.run()
    except KeyboardInterrupt:
        log("stopped (%d requests handled)" % sim.count)
    finally:
        try:
            ch.disconnect()
        except Exception:
            pass
        try:
            pt.close()
        except Exception:
            pass
        if logf:
            logf.close()


if __name__ == "__main__":   # pragma: no cover
    main()
