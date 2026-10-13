# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Climatronic (VAG 4F/4L 910 043x, Fujitsu F2MC-16LX) OBD read + identify over CAN.

KWP2000 (ISO 14230) services carried inside VW TP 2.0, over a raw CAN channel.
This reuses the suite's transport and device layers wholesale:

* :class:`ovtsuite.flash.tp20_transport.Tp20Channel` -- the TP 2.0 connection
  (channel setup 0x200, A0/A1 params, sequenced data frames, ACK window,
  channel-test keep-alives).  We talk KWP payloads to it directly
  (``send``/``recv``) rather than through the K-line reframing facade, since a
  Fujitsu module has none of the EDC16's 0x75/0x76 framing quirks.
* :mod:`ovtsuite.flash.j2534` on Windows (registered PassThru devices) and
  :mod:`ovtsuite.flash.openport` (OpenPort 2.0 over USB) everywhere else, for
  the CAN interface -- exactly the split the EDC16 CAN path uses.

Security access is the Climatronic family's additive seed->key, reverse
engineered from the firmware (4F0910043B SW0080, 4L0910043D SW0071,
4L0910043 SW0060 -- all three carry the identical
``MOVW A,#const; ZEXTW; ADDL A,<seed>; CMPL A,<key>`` sequence, only the seed's
RAM address differs):

    key32 = (seed32 + CONST) & 0xFFFFFFFF        # seed/key both 4 bytes, big-endian

with three level constants (0x3AF7 primary, 0x17327, 0xC8E1).  Requesting the
seed is ``27 03``; sending the key is ``27 04``.  The seed is dynamic (built
from a free-running timer), so it is read fresh each session.

SCOPE: this module is READ + IDENTIFY only -- ReadECUIdentification (0x1A),
SecurityAccess (0x27) and the flash READ.  It never erases or writes flash.  A
dump carries no brick risk; it is also how the transport + seed/key chain is
validated against the real module before any write path is added.

READ PATH (confirmed on the car 2026-09-21): connect + ident + SecurityAccess
work; the read-level key is (seed + 0x3AF7), VERIFIED against a real pair (seed
FDE010A4 -> key FDE04B9B accepted).  ReadMemoryByAddress (0x23) is NOT supported
here (7F 23 11 serviceNotSupported); the module answers the KWP2000 UPLOAD
sequence instead (service dispatch FD5F3A matches 0x35/0x36/0x37).  The dump uses
RequestUpload `35 <addr:3> 00 <size:3>` -> repeated TransferData `36` -> `76
<block>` -> RequestTransferExit `37`, mirroring the suite's proven EDC16 upload
format.  The exact upload/block framing is bench-confirmable -- a wrong shape just
NAKs; the next on-car log refines it.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ---------------------------------------------------------------- security key
# The HVAC seed/key is additive: key32 = (seed32 + CONST) & 0xFFFFFFFF, 4 big-
# endian bytes.  GROUND TRUTH from the sendKey verify routine FE7DDD (disassembled
# 2026-09-21): it copies the received key and compares it to `seed + CONST` for
# THREE constants, accepting the one that matches and setting a per-level flag:
#   seed + 0x3AF7  -> D:0x1A46   (primary; the level the dump uses)
#   seed + 0x17327 -> D:0x1A45   (higher level)
#   seed + 0xC8E1  -> D:0x1A44   (higher level)
# The ECU accepts ONLY these three keys.  Writing/erase is gated on a higher
# level (D:0x1A46/1A45 tested at FCB38D/FCB3B0 -> NRC 0x33 if not set), i.e.
# level2/level3 -- so the WRITE key is (seed + 0x17327) or (seed + 0xC8E1), both
# already in the firmware; exactly which one the flash-program path needs is the
# only open bit.  (The factory SGO's 6-byte metadata field decodes under our
# unverified SA2 opcode 0x93 to seed+0x05814A05, but the ECU never checks that
# constant -- it appears nowhere in the fully-disassembled firmware or loader --
# unverified SA2 opcode 0x93 to seed+0x05814A05.  UPDATE (2026-09-22): a captured
# ODIS flash (Reference/Logs/ODISHVACflash.log) PROVED seed+0x05814A05 IS the WRITE
# key -- but for the PROGRAMMING-session SecurityAccess (0x27 01/02 after 10 85), a
# separate level from the read key below (seed 0090C040 -> key 06120A45 accepted on
# the car).  See WRITE_KEY_CONST / compute_write_key.)  Constants identical on
# 4F0910043B, 4L0910043D, 4L0910043.
KEY_CONSTS = {
    "primary": 0x00003AF7,      # read/dump level (D:0x1A46)
    "level2": 0x00017327,       # higher level (D:0x1A45) -- write/erase candidate
    "level3": 0x0000C8E1,       # higher level (D:0x1A44) -- write/erase candidate
}


def compute_key(seed4: bytes, level: str = "primary") -> bytes:
    """Seed->key (SecurityAccess 0x03/0x04): (seed + KEY_CONSTS[level]) mod 2^32,
    4 big-endian bytes.  ``primary`` (0x3AF7) is the read/dump level; a write needs
    ``level2``/``level3`` (the higher levels FCB38D/FCB3B0 gate)."""
    if len(seed4) != 4:
        raise ValueError("Climatronic seed must be 4 bytes, got %d" % len(seed4))
    seed32 = int.from_bytes(bytes(seed4), "big")
    key32 = (seed32 + KEY_CONSTS[level]) & 0xFFFFFFFF
    return key32.to_bytes(4, "big")


# ---- programming-session (flash) SecurityAccess -----------------------------
# CAPTURED + car-verified (Reference/Logs/ODISHVACflash.log): the programming
# session (StartDiagnosticSession 0x85) uses SecurityAccess 0x27 01/02 with
# key = seed + 0x05814A05 -- exactly the SGO metadata SA2 tape (93 05814A05 4C).
# This is a SEPARATE level from the normal-session read key (0x27 03/04, +0x3AF7):
# on the car, seed 0x0090C040 -> key 0x06120A45 was accepted (67 02 34).
WRITE_KEY_CONST = 0x05814A05


def compute_write_key(seed4: bytes) -> bytes:
    """Programming-session seed->key (0x27 01/02): (seed + 0x05814A05) mod 2^32."""
    if len(seed4) != 4:
        raise ValueError("Climatronic seed must be 4 bytes, got %d" % len(seed4))
    return (((int.from_bytes(bytes(seed4), "big") + WRITE_KEY_CONST)
             & 0xFFFFFFFF)).to_bytes(4, "big")


# ---------------------------------------------------------------- flash layout
FLASH_BASE = 0xFC0000           # F2MC-16LX flash window start
FLASH_SIZE = 0x040000           # 256 KB (4F0910043B / 4L0910043 SW parts)
# The firmware byte-sum checksum is the 4F/4L HVAC module's
# (ovtsuite.hvac.firmware); see dump_checksum() below.

# TP 2.0 logical address of the Climatronic.  NOTE: 0x08 is the KW1281/KWP
# address ("Auto HVAC"); over CAN TP 2.0 through the gateway the module answers
# the channel-setup at dest 0x2C -- confirmed by a VCDS->HVAC sniff
# (Reference/Logs/VCDS-HVAC.log): `200#2CC0...` -> reply on 0x22C, ident
# `4F0910043B 0080 KLIMABETAETIGUNG`.  The gateway itself is 0x1F there.
DEST_HVAC = 0x2C
CAN_RATE = 500000               # powertrain/comfort CAN bit rate for TP 2.0


class ClimatronicError(Exception):
    pass


class NegativeResponse(ClimatronicError):
    NRC = {0x10: "generalReject", 0x11: "serviceNotSupported",
           0x12: "subFunctionNotSupported", 0x22: "conditionsNotCorrect",
           0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
           0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
           0x37: "requiredTimeDelayNotExpired", 0x78: "responsePending"}

    def __init__(self, service: int, nrc: int):
        self.service = service
        self.nrc = nrc
        super().__init__("KWP2000 NAK for 0x%02X: 0x%02X (%s)"
                         % (service, nrc, self.NRC.get(nrc, "?")))


# ---------------------------------------------------------------- device layer
@dataclass
class Interface:
    """A usable CAN interface: a J2534 device (Windows) or OpenPort 2.0."""
    kind: str                   # "j2534" | "openport"
    label: str
    device: object = None       # j2534.Device on Windows; None for openport
    loadable: bool = True

    def open(self):
        """Open the hardware and return a raw CAN channel (write_can/read/pass_*)."""
        if self.kind == "j2534":
            from . import j2534
            dev = j2534.open_device(self.device)
            ch = dev.connect(j2534.CAN, CAN_RATE)
            ch.pass_all()
            ch.clear_rx()
            return dev, ch
        from . import j2534
        from .openport import OpenPort
        dev = OpenPort()
        ch = dev.connect(j2534.CAN, CAN_RATE)
        ch.pass_all()
        ch.clear_rx()
        return dev, ch


def list_interfaces() -> List[Interface]:
    """Every usable CAN interface, best first.

    Windows: the registered J2534 PassThru devices, loadable ones first, then
    sorted by name (an OpenPort's own J2534 DLL shows up here like any other).
    Other platforms: OpenPort 2.0 over USB via pyusb (the Windows J2534 registry
    does not exist there).
    """
    out: List[Interface] = []
    if os.name == "nt":
        from . import j2534
        devs = j2534.list_devices()
        devs.sort(key=lambda d: (not d.loadable, d.name.lower()))
        for d in devs:
            out.append(Interface("j2534", d.label, device=d, loadable=d.loadable))
    else:
        try:
            from .openport import labels
            found = labels()
        except Exception as ex:                     # pyusb missing / no device
            found = []
            out_err = str(ex)
        for lbl in (found or []):
            out.append(Interface("openport", lbl or "OpenPort 2.0 (Tactrix)"))
        if not found:
            # Represent the OpenPort path even when enumeration is empty, so a
            # connected-but-unenumerated cable can still be tried explicitly.
            out.append(Interface("openport", "OpenPort 2.0 (Tactrix)", loadable=True))
    return out


def open_interface(prefer: Optional[str] = None, log=None) -> Tuple[Interface, object, object]:
    """Pick an interface (``prefer`` = case-insensitive label substring) and open
    it.  Returns ``(interface, device, channel)``.  Raises if none is usable."""
    log = log or (lambda s: None)
    candidates = list_interfaces()
    if not candidates:
        raise ClimatronicError(
            "No CAN interface found (Windows: install a J2534 driver; "
            "other OS: connect an OpenPort 2.0 and install pyusb).")
    chosen = None
    if prefer:
        p = prefer.lower()
        chosen = next((i for i in candidates if p in i.label.lower()), None)
        if chosen is None:
            raise ClimatronicError("No interface matches %r; have: %s"
                                   % (prefer, ", ".join(i.label for i in candidates)))
    else:
        chosen = next((i for i in candidates if i.loadable), candidates[0])
    log("Interface: %s" % chosen.label)
    dev, ch = chosen.open()
    return chosen, dev, ch


# ---------------------------------------------------------------- CAN traffic log
class CanTap:
    """Debug wrapper around a raw CAN channel that logs every TX and RX frame.

    Presents the same surface a Tp20Channel uses (write_can / read / pass_all /
    pass_ids / clear_rx / pass), so it drops in transparently:
    ``tp.can = CanTap(tp.can, emit)``.  ``emit`` is a callable taking one text
    line.  Timestamps are ms since the tap was created.  Read-only w.r.t. the
    bus — it only observes; it never injects or drops frames.
    """
    def __init__(self, chan, emit):
        self._c = chan
        self._emit = emit
        self.frames = 0

    def _line(self, tx, can_id, payload, extended=False):
        # Same wire format as the bridge's _FrameTap and sniff.capture:
        #   (<epoch>) can-tx <ID>#<HEX>   /   (<epoch>) can <ID>#<HEX>
        import time as _t
        self.frames += 1
        ident = ("%08X" % can_id) if extended else ("%03X" % (can_id or 0))
        try:
            self._emit("(%.6f) %s %s#%s" % (_t.time(), "can-tx" if tx else "can",
                                            ident, bytes(payload).hex().upper()))
        except Exception:
            pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        self._line(True, can_id, payload, extended)
        return self._c.write_can(can_id, payload, extended, timeout_ms)

    def write(self, data, flags=0, timeout_ms=100):
        # ISO15765 / raw message write: data is <4-byte can id><payload>. Log it
        # as a TX frame so an ISO15765 read (which writes whole messages, not
        # per-frame) still shows TX in the trace.
        d = bytes(data)
        cid = int.from_bytes(d[:4], "big") if len(d) >= 4 else 0
        self._line(True, cid, d[4:])
        return self._c.write(d, flags, timeout_ms)

    def read(self, max_msgs=32, timeout_ms=100):
        msgs = self._c.read(max_msgs, timeout_ms)
        for m in msgs:
            self._line(False, getattr(m, "can_id", None), getattr(m, "payload", b""),
                       bool(getattr(m, "tx_flags", 0) & 0x100))
        return msgs

    def pass_all(self, *a, **k):
        return self._c.pass_all(*a, **k)

    def pass_ids(self, *a, **k):
        return self._c.pass_ids(*a, **k)

    def clear_rx(self, *a, **k):
        fn = getattr(self._c, "clear_rx", None)
        return fn(*a, **k) if fn else None

    def __getattr__(self, name):          # delegate anything else to the real channel
        return getattr(self._c, name)


# ---------------------------------------------------------------- KWP over TP2.0
class Climatronic:
    """Read/identify session with a Climatronic over TP 2.0 on a CAN channel."""

    def __init__(self, channel=None, dest: int = DEST_HVAC, log=None,
                 timeout_ms: int = 2000, tp=None):
        """``channel`` is a raw CAN channel (write_can/read/pass_*), from which a
        :class:`Tp20Channel` is built.  Alternatively pass ``tp`` -- an already
        constructed TP 2.0 channel (e.g. the ``.tp`` of a CanKLine the GUI opened,
        or a bridge TP channel) -- to reuse it instead of opening a second one."""
        self._log = log or (lambda s: None)
        self.timeout_ms = timeout_ms
        self.unlocked = False
        if tp is not None:
            self.tp = tp
        elif channel is not None:
            from .tp20_transport import Tp20Channel
            self.tp = Tp20Channel(channel, dest=dest, log=self._log,
                                  poll_timeout_ms=timeout_ms)
        else:
            raise ClimatronicError("Climatronic needs either a CAN channel or a tp channel")

    # -- lifecycle --
    def open(self):
        dest = getattr(self.tp, "dest", None)
        self._log("Opening TP 2.0 channel to HVAC%s..."
                  % ("" if dest is None else " (dest 0x%02X)" % dest))
        self.tp.open()

    def close(self):
        try:
            self.tp.close()
        except Exception:
            pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one KWP request/response, absorbing responsePending --
    def request(self, sid: int, data: bytes = b"") -> bytes:
        self.tp.send(bytes([sid]) + bytes(data))
        for _ in range(64):
            resp = self.tp.recv(self.timeout_ms)
            if not resp:
                raise ClimatronicError("empty response to 0x%02X" % sid)
            if resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x78:
                continue                             # responsePending: wait for the real one
            break
        if resp[0] == 0x7F:
            raise NegativeResponse(sid, resp[2] if len(resp) >= 3 else 0)
        if resp[0] != (sid | 0x40):
            raise ClimatronicError("unexpected response 0x%02X to service 0x%02X"
                                   % (resp[0], sid))
        return resp[1:]

    # -- services --
    def start_session(self, sub: int = 0x89) -> bytes:
        """StartDiagnosticSession.  Default 0x89 (extended); pass 0x85 for the
        programming session if the module gates read/security to it."""
        return self.request(0x10, [sub])

    def tester_present(self):
        try:
            self.request(0x3E, [0x01])
        except ClimatronicError:
            pass

    def read_ecu_identification(self, record: int = 0x9B) -> bytes:
        """ReadECUIdentification (0x1A).  0x9B is the standard VAG identification
        record; returns the raw record bytes."""
        return self.request(0x1A, [record])

    def unlock(self, level: str = "primary") -> Tuple[bytes, bytes]:
        """SecurityAccess: request seed (27 03), send key (27 04). Returns
        (seed4, key4).  Raises NegativeResponse(0x35) on a wrong key."""
        body = self.request(0x27, [0x03])            # -> [0x03, s0, s1, s2, s3]
        seed = bytes(body[1:5])
        if len(seed) != 4:
            raise ClimatronicError("unexpected seed length: %s" % body.hex())
        if int.from_bytes(seed, "big") == 0:
            self._log("Seed is zero -> already unlocked.")
            self.unlocked = True
            return seed, b""
        key = compute_key(seed, level)
        self._log("Seed %s -> key %s (%s + 0x%X)"
                  % (seed.hex(), key.hex(), "seed", KEY_CONSTS[level]))
        self.request(0x27, [0x04] + list(key))       # +ve 67 04 or raises
        self.unlocked = True
        self._log("Security access granted (level %s)." % level)
        return seed, key

    def read_memory(self, addr: int, size: int,
                    addr_bytes: int = 3, len_bytes: int = 1) -> bytes:
        """ReadMemoryByAddress (0x23).  NOT supported by this module -- a real
        read of 0x23 returned 7F 23 11 (serviceNotSupported), so the dump uses the
        RequestUpload sequence below instead.  Kept for other modules / reference."""
        req = list(addr.to_bytes(addr_bytes, "big")) + list(size.to_bytes(len_bytes, "big"))
        return self.request(0x23, req)

    # -- flash read via the KWP2000 upload sequence (0x35/0x36/0x37) --
    # This module answers 0x35/0x36/0x37 (service dispatch FD5F3A) but NOT 0x23.
    # Request framing mirrors the suite's proven EDC16 KWP2000 upload
    # (edc16.Edc16Flash._request_upload): `35 <addr:3> 00 <size:3>` -> 75 ...,
    # then repeated `36` -> `76 <block>`, then `37` -> 77.  Bench-confirmable:
    # a wrong shape just NAKs (harmless on a read); the first on-car log refines it.
    def request_upload(self, addr: int, size: int) -> bytes:
        """RequestUpload (0x35): `35 <addr:3> 00 <size:3>`.  Returns the 0x75 body
        (may carry a max-block-length descriptor)."""
        req = list(addr.to_bytes(3, "big")) + [0x00] + list(size.to_bytes(3, "big"))
        return self.request(0x35, req)

    def transfer_read(self) -> bytes:
        """TransferData (0x36) with no data -> one 0x76 <block> of upload data."""
        return self.request(0x36)

    def request_transfer_exit(self) -> bytes:
        """RequestTransferExit (0x37)."""
        try:
            return self.request(0x37)
        except ClimatronicError as ex:
            self._log("(transferExit not clean: %s)" % ex)
            return b""

    def dump(self, base: int = FLASH_BASE, size: int = FLASH_SIZE,
             upload_chunk: int = 0x1000, on_percent=None, is_stop=None) -> bytes:
        """Read [base, base+size) via the KWP2000 upload sequence, one
        RequestUpload per ``upload_chunk`` bytes (chunked like the EDC16 CAN path,
        which drops a single large upload), streaming 0x36 TransferData blocks in
        between.  ``on_percent(int)`` / ``is_stop()->bool`` optional."""
        out = bytearray()
        n = size
        last = -1
        while len(out) < n:
            if is_stop and is_stop():
                raise ClimatronicError("dump stopped")
            want = min(upload_chunk, n - len(out))
            self.request_upload(base + len(out), want)
            got = bytearray()
            guard = 0
            while len(got) < want:
                if is_stop and is_stop():
                    raise ClimatronicError("dump stopped")
                blk = self.transfer_read()
                if not blk:
                    guard += 1
                    if guard > 3:
                        raise ClimatronicError("empty upload block at 0x%06X"
                                               % (base + len(out) + len(got)))
                    continue
                got.extend(blk)
                self.tp.keepalive()
            self.request_transfer_exit()
            out.extend(got[:want])
            pct = len(out) * 100 // n
            if on_percent and pct != last:
                on_percent(pct)
                last = pct
        return bytes(out[:n])

    # ================= FLASH WRITE (programming session) =================
    # Exact sequence captured from a real ODIS flash + car-verified
    # (Reference/Logs/ODISHVACflash.log, stock v80):
    #   10 85               programming session
    #   27 01 / 27 02       write security, key = seed + 0x05814A05
    #   per block:
    #     34 <addr:3> 00 <size:3>                    RequestDownload   -> 74
    #     31 C4 <start:3> <end:3> 00 00 00 00 00 2E  erase (end incl.) -> 71 C4 01
    #     33 C4                                       StopRoutine       -> 73 C4 00
    #     36 <=128 B                x N               TransferData      -> 76
    #     37                                          TransferExit      -> 77
    #     31 C5 <start:3> <end:3> <sum16:2>           checksum verify   -> 71 C5
    #     33 C5                                       StopRoutine       -> 73 C5 00
    # Block order: loader (634 B @ FC0000) first, then FC0000/FD0000/FE0000 (64K),
    # FF0000 (32K), FF8000 (8K).  31 C5 arg = 16-bit big-endian sum of the block
    # (verified against all six sections of the v80 image).
    ERASE_TAIL = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x2E])  # constant per capture
    XFER_BLOCK = 102                                          # 0x36 payload size
    #  matches the captured ODIS flash (102-byte TransferData payloads); 128 was
    #  accepted on-car too, but matching the proven capture removes it as a variable.

    def _reconnect(self, timeout_s: float = 12.0) -> None:
        """Re-establish the TP2.0 channel after an ECU-initiated A8.  CAPTURED
        behaviour: the module DROPS the channel (sends A8 itself) after a session
        change (10 85) and after each erase (31 C4) -- it goes off-bus for ~1-2 s
        to erase/re-init -- so we must re-run the channel setup and only then poll
        the result.  The programming session + security survive on the ECU side
        (the capture resumes with 33 C4 / 27, never re-sending 10 85), so this
        re-runs ONLY the 0x200 setup + A0/A1 params.  Retries until the ECU is back.

        Bridge fix (2026-09-22): re-run the TP2.0 setup on the EXISTING CAN
        channel, never a device-level reconnect.  On the 32-bit bridge path
        (``j2534.BridgeTp20``, Bear's OpenPort 2.0) ``tp.open()`` runs a fresh
        ``PassThruConnect`` -- but this bridge process already has the CAN
        channel connected (from the first ``Climatronic.open()``), so a second
        connect fails with J2534 ERR_CHANNEL_IN_USE, and the retry loop just
        spins on it for the whole 12 s (seen on real hardware: after 50 85 the
        read-probe/test-write stalled 12 s, then tore down -- the CAN log ends
        exactly there).  ``BridgeTp20.reopen()`` re-runs the TP2.0 channel
        setup on the existing CAN channel with no device reconnect -- the same
        dispatch the ZF6HP write path already uses (zf6hp.py) and that
        ``CanKLine.reset_channel`` builds on for EDC16.  A local ``Tp20Channel``
        has no ``reopen`` and its ``open()`` already does the setup on its
        existing raw channel, so it stays on ``open()``."""
        import time as _t
        deadline = _t.time() + timeout_s
        last = None
        reopen = getattr(self.tp, "reopen", None)     # bridge: setup-only, no reconnect
        _resetup = reopen if reopen is not None else self.tp.open
        while _t.time() < deadline:
            try:
                _resetup()                            # 200 setup + params; drains stale A8
                self._log("  channel reconnected")
                return
            except Exception as ex:                   # noqa: BLE001 (transport error type varies)
                last = ex
                _t.sleep(0.3)
        raise ClimatronicError("reconnect failed after %.0fs: %s" % (timeout_s, last))

    def enter_programming_session(self, reconnect: bool = True) -> bytes:
        """StartDiagnosticSession 0x85 -> 50 85 ..  Exposes 0x34/0x36/0x37 and the
        C4/C5 routines the normal session does not.  The ECU disconnects right
        after (session re-init), so reconnect before the next request."""
        body = self.request(0x10, [0x85])
        self._log("programming session: 50 %s" % body.hex())
        if reconnect:
            self._reconnect()
        return body

    def unlock_write(self) -> Tuple[bytes, bytes]:
        """Programming-session SecurityAccess (0x27 01/02); key = seed+0x05814A05.
        Returns (seed4, key4); raises NegativeResponse on a bad key."""
        body = self.request(0x27, [0x01])            # -> [01, s0..s3]
        seed = bytes(body[1:5])
        if len(seed) != 4:
            raise ClimatronicError("unexpected write seed: %s" % body.hex())
        if int.from_bytes(seed, "big") == 0:
            self._log("write seed zero -> already unlocked")
            self.unlocked = True
            return seed, b""
        key = compute_write_key(seed)
        self._log("write seed %s -> key %s (seed + 0x%X)"
                  % (seed.hex(), key.hex(), WRITE_KEY_CONST))
        self.request(0x27, [0x02] + list(key))       # +ve 67 02 or raises
        self.unlocked = True
        self._log("Programming security granted.")
        return seed, key

    def request_download(self, addr: int, size: int) -> bytes:
        """RequestDownload (0x34): 34 <addr:3> 00 <size:3> -> 74 .."""
        req = list(addr.to_bytes(3, "big")) + [0x00] + list(size.to_bytes(3, "big"))
        return self.request(0x34, req)

    def _routine(self, rid: int, params) -> None:
        """StartRoutineByLocalId (0x31 <rid> ..) then StopRoutine (0x33 <rid>) to
        read the result -- the capture always pairs them, and 0x33 is what surfaces
        an erase/checksum failure as a negative response."""
        self.request(0x31, [rid] + list(params))     # 71 <rid> .. or raises
        self.request(0x33, [rid])                     # 73 <rid> 00 or raises

    def erase_start(self, start: int, end: int) -> None:
        """Start the erase routine: 0x31 C4 <start:3> <end:3> 00 00 00 00 00 2E ->
        71 C4 01 (routine STARTED).  The ECU then drops the channel to erase; poll
        the result with erase_poll() AFTER reconnecting."""
        self.request(0x31, [0xC4] + list(start.to_bytes(3, "big"))
                     + list(end.to_bytes(3, "big")) + list(self.ERASE_TAIL))

    #: Overall budget for a single sector erase to complete (it runs off-bus).
    ERASE_WAIT_S = 30.0
    #: Settle time to wait AFTER the erase channel-drop (A8) BEFORE reconnecting.
    #: The captured ODIS flash waits ~2.1 s after every erase A8 (measured:
    #: 2.06-2.16 s across all 6 blocks) and only then re-runs the 0x200 setup +
    #: 33 C4 -- with security still intact. Reconnecting sooner (~0.27 s, the
    #: first on-car write attempt) caught the module before it finished
    #: erasing/re-validating, and 33 C4 came back 7F 33 33 (securityAccessDenied).
    ERASE_SETTLE_S = 2.2

    def erase_poll(self) -> None:
        """StopRoutine 0x33 C4 -> 73 C4 00: read the erase result after reconnect."""
        self.request(0x33, [0xC4])

    def _await_erase_done(self, timeout_s: Optional[float] = None) -> None:
        """Wait for a 31 C4 erase to finish and read its result, robustly.

        The ECU drops the channel (A8) to erase off-bus and returns when done, so
        the base case is reconnect -> 33 C4 -> 73 C4 00. But a real erase can
        outlast the captured ~1-2 s (a worn sector, a slower module), and the
        module can come back on-bus while still finishing (a busy NAK) or drop
        the channel a second time. This reconnects once, then re-reads the erase
        result on a bounded deadline, re-establishing the channel if it drops
        again -- mirroring the ZF6HP write path's erase-completion loop. It only
        RE-READS the result (33 C4 / StopRoutine is non-destructive) and never
        re-issues the erase, so a retry can't double-erase. A NAK that is NOT a
        busy/not-ready code is a real erase failure and is surfaced at once."""
        deadline = time.time() + (self.ERASE_WAIT_S if timeout_s is None else timeout_s)
        # Let the erase actually finish before reconnecting -- see ERASE_SETTLE_S.
        time.sleep(min(self.ERASE_SETTLE_S, max(0.0, deadline - time.time())))
        self._reconnect(timeout_s=max(2.0, deadline - time.time()))   # module back on bus
        last = None
        reunlocked = 0
        while time.time() < deadline:
            try:
                self.erase_poll()                     # 33 C4 -> 73 C4 00
                return
            except NegativeResponse as nr:
                if nr.nrc == 0x33 and reunlocked < 2:
                    # securityAccessDenied: the programming session persists across
                    # the erase reconnect (ODIS never re-sends 10 85), but if this
                    # module dropped security on the channel teardown, re-establish
                    # it (27 01/02) and retry -- a safe, self-healing fallback on
                    # top of the ERASE_SETTLE_S timing fix.
                    reunlocked += 1
                    self._log("  erase poll: securityAccessDenied -- re-unlocking "
                              "(attempt %d)" % reunlocked)
                    try:
                        self.unlock_write()
                    except Exception as ex:           # noqa: BLE001
                        last = ex
                    continue
                # still finishing: 0x21 busyRepeatRequest / 0x22 conditionsNotCorrect
                # / 0x23 routineNotComplete / 0x78 responsePending (already absorbed).
                if nr.nrc not in (0x21, 0x22, 0x23, 0x78):
                    raise                             # a genuine erase failure
                last = nr
                time.sleep(0.3)
            except Exception as ex:                   # channel dropped again mid-poll
                last = ex
                try:
                    self._reconnect(timeout_s=max(2.0, deadline - time.time()))
                except Exception as rex:
                    last = rex
        raise ClimatronicError(
            "erase did not complete within %.0fs (last: %s)"
            % ((self.ERASE_WAIT_S if timeout_s is None else timeout_s), last))

    def checksum_block(self, start: int, end: int, sum16: int) -> None:
        """Checksum-verify routine 0x31 C5 <start:3> <end:3> <sum16:2> (end incl.);
        the module compares its own flash sum to sum16 and NAKs on mismatch."""
        self._routine(0xC5, list(start.to_bytes(3, "big"))
                      + list(end.to_bytes(3, "big")) + list(sum16.to_bytes(2, "big")))

    def transfer_write(self, data: bytes, on_percent=None, is_stop=None) -> None:
        """TransferData (0x36) the block in <=XFER_BLOCK chunks -> 76 each.

        Streams chunks back-to-back: each ``request(0x36)`` sends the data and
        reads its 0x76, and that recv() already answers the ECU's A3 channel
        keep-alives (Tp20Channel._classify). An explicit per-chunk keepalive()
        was REMOVED (2026-09-22): keepalive() does a blocking _next_frame() up to
        poll_timeout_ms, so it stalled ~1 s per chunk waiting for the next A3 --
        throttling the transfer to ~128 B/s (~16x slower than the ODIS capture's
        ~1.5 KB/s / 54 ms per chunk). On real hardware that slowness tripped the
        ECU's inter-TransferData timeout and it dropped the channel (A8) ~7 s into
        section 1. With the stall gone the transfer streams at the module's pace."""
        n = len(data)
        blk = self.XFER_BLOCK
        for i in range(0, n, blk):
            if is_stop and is_stop():
                raise ClimatronicError("write stopped")
            self.request(0x36, list(data[i:i + blk]))    # 76 or raises (recv answers A3)
            if on_percent:
                on_percent(min(i + blk, n), n)

    def flash_block(self, addr: int, data: bytes, on_percent=None, is_stop=None) -> None:
        """One RequestDownload -> erase -> TransferData -> exit -> checksum cycle
        for a single flash block.  Every step is response-checked; a NAK raises."""
        size = len(data)
        end = addr + size - 1
        s16 = sum(data) & 0xFFFF
        self._log("  block 0x%06X..0x%06X (%d B) sum=0x%04X" % (addr, end, size, s16))
        self.request_download(addr, size)             # 34 -> 74
        self.erase_start(addr, end)                   # 31 C4 -> 71 C4 01 (erase starts)
        self._await_erase_done()                      # reconnect + 33 C4, bounded/robust
        # Status: without this, the last log line during the whole multi-second
        # transfer is _reconnect()'s "channel reconnected", so the GUI status sat
        # on "channel reconnected" while actively writing. Announce the transfer
        # so the status reflects what's happening (the bar moves via on_percent).
        self._log("  writing 0x%06X (%d KB)..." % (addr, max(1, size // 1024)))
        self.transfer_write(data, on_percent=on_percent, is_stop=is_stop)  # 36..
        self.request(0x37)                            # TransferExit -> 77
        self.checksum_block(addr, end, s16)           # 31 C5 / 33 C5 (verifies, no drop)
        self._log("  block 0x%06X OK (checksum verified)" % addr)

    @staticmethod
    def _mandatory_blocks(blocks) -> set:
        """Indices that must ALWAYS be written, never skipped: any block whose
        flash range overlaps another block's.  On the HVAC that is the loader
        (0xFC0000, 634 B) and section 1 (0xFC0000, 64 KB) -- the loader is the
        flash driver the resident boot runs, flashed into section 1's sector and
        then overwritten by section 1, so the two are inseparable.  Detected
        structurally (overlap), not hardcoded, so it holds for any block list."""
        spans = [(a, a + len(d)) for a, d in blocks]
        mand = set()
        for i, (ai, ei) in enumerate(spans):
            for j, (aj, ej) in enumerate(spans):
                if i != j and ai < ej and aj < ei:
                    mand.add(i); mand.add(j)
        return mand

    def flash_write(self, blocks, program_session=True, changed=None,
                    on_block=None, on_percent=None, is_stop=None,
                    start_at: int = 0, on_skip=None, finalize=True) -> None:
        """Flash an ordered (addr, data) block list (loader first, then sections)
        using the captured ODIS sequence.

        ``changed`` (default None) drives the SELECTIVE skip:
          * None -> write EVERY block (the proven full write).
          * a set of flash ADDRESSES -> only those sections were edited vs the
            stock baseline, so any OTHER non-mandatory block is UNCHANGED and is
            SKIPPED. This is a deterministic image diff
            (``HvacImage.changed_section_addrs``), not an on-module check -- it
            assumes the module currently holds that baseline (true when flashing
            an edit onto the stock it was made from). Mandatory blocks (loader +
            section 1, the overlapping pair) are always written; section 1 always
            appears in ``changed`` anyway (it carries the 0xFCFFF4 checksum).

        ``start_at`` resumes from block N (skips earlier blocks by index).
        ``on_skip(idx, addr, reason)`` reports skips. The channel must be open."""
        if program_session:
            self.enter_programming_session()
            self.unlock_write()
        total = len(blocks)
        mandatory = self._mandatory_blocks(blocks)
        grand = sum(len(d) for _, d in blocks) or 1     # total bytes, for global %
        cum = 0
        written = skipped = 0
        for idx, (addr, data) in enumerate(blocks):
            if idx < start_at:
                self._log("skip block %d @ 0x%06X (resume)" % (idx, addr))
                cum += len(data)
                if on_percent:
                    on_percent(cum, grand)
                continue
            if is_stop and is_stop():
                raise ClimatronicError("write stopped")
            # Deterministic selective skip: a non-mandatory block whose section
            # is UNCHANGED vs the stock baseline (not in `changed`) is skipped.
            if changed is not None and idx not in mandatory and addr not in changed:
                skipped += 1
                self._log("[%d/%d] 0x%06X unchanged vs baseline -- SKIP"
                          % (idx + 1, total, addr))
                if on_skip:
                    on_skip(idx, addr, "unchanged")
                cum += len(data)
                if on_percent:
                    on_percent(cum, grand)
                continue
            if on_block:
                on_block(idx, total, addr, len(data))
            self._log("[%d/%d] flashing 0x%06X (%d B)..." % (idx + 1, total, addr, len(data)))
            _base = cum
            self.flash_block(
                addr, data, is_stop=is_stop,
                on_percent=((lambda d, t, b=_base: on_percent(b + d, grand))
                            if on_percent else None))
            cum += len(data)
            written += 1
        if program_session and finalize:
            self.finalize_programming()
        self._log("Flash write complete (%d written, %d skipped, of %d blocks%s)."
                  % (written, skipped, total,
                     "" if not start_at else "; resumed at %d" % start_at))

    def finalize_programming(self) -> None:
        """Close the programming session so the module reboots into the freshly
        flashed application. Mirrors the captured ODIS tail after the last
        section's 33 C5 verify: `20` StopDiagnosticSession (the module NAKs it
        7F 20 90 -- harmless), then `82` StopCommunication -> C2. The channel A8
        teardown then happens in the caller's tp.close().

        WITHOUT this the module stays in the resident bootloader after the flash:
        unresponsive to normal operation but still accepting re-flashes (exactly
        the 2026-09-22 symptom after a write that ended on 33 C5 + a bare A8).
        Both requests are best-effort -- a NAK or timeout here does not undo a
        successful, fully-verified flash, so we never raise out of finalize."""
        for sid, name in ((0x20, "StopDiagnosticSession"), (0x82, "StopCommunication")):
            try:
                self.request(sid)
                self._log("  %s (0x%02X): OK" % (name, sid))
            except NegativeResponse as nr:
                self._log("  %s (0x%02X): NAK 0x%02X (expected for 0x20; continuing)"
                          % (name, sid, nr.nrc))
            except Exception as ex:                   # noqa: BLE001 (transport varies)
                self._log("  %s (0x%02X): %s (continuing)" % (name, sid, ex))

    def test_write_access(self, verify_blocks=None):
        """NON-DESTRUCTIVE dry-run on the real module: enter the programming
        session and complete the write-security handshake (10 85 -> 27 01/02),
        then optionally checksum-VERIFY current flash regions (31 C5) -- and STOP
        before any erase (31 C4) or transfer (0x36).  Nothing is written.

        This exercises the entire write path except the two destructive steps:
        it proves the channel, the programming session, the write key
        (seed+0x05814A05) and (with verify_blocks) that the module's current flash
        matches the image you intend to write -- region by region -- WITHOUT
        touching a single byte.  ``verify_blocks`` = the (addr, data) list; each
        region's expected 16-bit sum is sent via 31 C5 and the ECU NAKs on a
        mismatch (so no NAK == that region already matches your image).

        Returns dict: seed, key, session_ok, and per-region verify results.
        NOTE: whether 31 C5 works standalone (no preceding 34/erase) is not proven
        from the ODIS capture -- if a region check NAKs with 0x31/0x22 rather than
        a checksum mismatch, the module may require the download context; that is
        reported, not treated as a real mismatch."""
        out = {"session_ok": False, "seed": None, "key": None, "verify": []}
        self.enter_programming_session()              # 10 85 (+ reconnect)
        out["session_ok"] = True
        seed, key = self.unlock_write()               # 27 01/02
        out["seed"] = seed.hex() if seed else None
        out["key"] = key.hex() if key else None
        for addr, data in (verify_blocks or []):
            end = addr + len(data) - 1
            s16 = sum(data) & 0xFFFF
            try:
                self.checksum_block(addr, end, s16)   # 31 C5 / 33 C5
                self._log("  verify 0x%06X..0x%06X sum=0x%04X: MATCH" % (addr, end, s16))
                out["verify"].append((addr, end, s16, "match"))
            except NegativeResponse as nr:
                self._log("  verify 0x%06X..0x%06X sum=0x%04X: NAK (%s)" % (addr, end, s16, nr))
                out["verify"].append((addr, end, s16, "nak:0x%02X" % nr.nrc))
        self._log("Write-access test complete (NO flash modified).")
        return out

    def read_probe(self, base: int = FLASH_BASE, n: int = 16):
        """NON-DESTRUCTIVE read test.  The captured flash uses only 0x34/0x36/0x37
        (download/write); no read service was ever seen.  But the PROGRAMMING
        session (10 85) exposes services the normal session lacks, so this tries
        the two candidate READ services THERE -- 0x35 RequestUpload and 0x23
        ReadMemoryByAddress -- for a few bytes and reports what each returns.  A
        positive response carrying data means an OBD read IS possible.  Reads
        only; nothing is erased or written.  Returns a dict of probe -> result."""
        out = {"session_ok": False, "seed": None, "key": None, "probes": {}}
        self.enter_programming_session()               # 10 85 (+ reconnect)
        out["session_ok"] = True
        seed, key = self.unlock_write()                # 27 01/02
        out["seed"] = seed.hex() if seed else None
        out["key"] = key.hex() if key else None
        # -- 0x35 RequestUpload + 0x36 TransferData --
        try:
            up = self.request_upload(base, n)          # 35 <addr:3> 00 <size:3> -> 75 ..
            blk = self.transfer_read()                 # 36 -> 76 <data>
            try:
                self.request_transfer_exit()
            except Exception:
                pass
            out["probes"]["0x35_upload"] = (("DATA", blk.hex()) if blk
                                            else ("empty75", up.hex()))
            self._log("  0x35 upload @0x%06X: %s" % (base, out["probes"]["0x35_upload"]))
        except NegativeResponse as nr:
            out["probes"]["0x35_upload"] = ("nak", "0x%02X" % nr.nrc)
            self._log("  0x35 upload: NAK 0x%02X" % nr.nrc)
        except Exception as ex:
            out["probes"]["0x35_upload"] = ("err", str(ex))
            self._log("  0x35 upload: err %s" % ex)
            try:
                self._reconnect()                      # a failed upload may drop the channel
            except Exception:
                pass
        # -- 0x23 ReadMemoryByAddress --
        try:
            mem = self.read_memory(base, n)            # 23 <addr:3> <len:1> -> 63 <data>
            out["probes"]["0x23_readmem"] = ("DATA", bytes(mem).hex())
            self._log("  0x23 readMemory @0x%06X: DATA %s" % (base, bytes(mem).hex()))
        except NegativeResponse as nr:
            out["probes"]["0x23_readmem"] = ("nak", "0x%02X" % nr.nrc)
            self._log("  0x23 readMemory: NAK 0x%02X" % nr.nrc)
        except Exception as ex:
            out["probes"]["0x23_readmem"] = ("err", str(ex))
            self._log("  0x23 readMemory: err %s" % ex)
        self._log("Read probe complete (NO flash modified).")
        return out


# ---------------------------------------------------------------- checksum
def dump_checksum(image: bytes) -> str:
    """One line on a flash read's firmware checksum, from the 4F/4L HVAC
    module ("" when it is not installed). Read-only; never rewrites."""
    from ..modules import registry as _registry
    cs = _registry.checksum("hvac", image=bytes(image))
    if cs is None:
        return ""
    return cs.verify(bytes(image)).describe()


# ------------------------------------------------- SGO / image -> flash blocks
# Captured section map (ODISHVACflash.log): loader first, then the 5 sections.
IMAGE_SECTION_MAP = [(0x00000, 0x10000), (0x10000, 0x10000), (0x20000, 0x10000),
                     (0x30000, 0x08000), (0x38000, 0x02000)]


def hvac_sgo_blocks(path: str):
    """Parse a plain (crypt=0) HVAC .sgo into the ordered (addr, data) flash-block
    list ODIS uses -- loader first, then firmware sections.  Delegates to
    :mod:`ovtsuite.flash.hvac_image` (which uses OVT-Suite's own SGO codec,
    :mod:`ovtsuite.sgo` -- NOT the GPL sgopack) so there is one blessed SGO
    parser in the app.  ``fix_checksum=False``: a file is flashed exactly as it
    sits on disk (its checksum was fixed when it was saved), never silently
    rewritten here."""
    from .hvac_image import HvacImage, HvacImageError
    try:
        return HvacImage.from_sgo(path).to_blocks(fix_checksum=False)
    except HvacImageError as ex:
        raise ClimatronicError(str(ex))


def image_blocks(image: bytes, loader: bytes, base: int = FLASH_BASE):
    """Build the ordered flash-block list from a flat 256 KB image + the loader
    blob, using the captured section map (loader, then 64K x3, 32K, 8K)."""
    blocks = [(base, bytes(loader))]
    for off, ln in IMAGE_SECTION_MAP:
        blocks.append((base + off, bytes(image[off:off + ln])))
    return blocks


# ---------------------------------------------------------------- bus probe
# TP 2.0 channel-setup broadcast id (fixed) + request opcode.  Verified against a
# captured ZF-6HP session: the setup frame is `<dest> C0 00 10 00 03 01` sent to
# 0x200, the reply lands on 0x200+dest, and the working comm ids are read out of
# the reply.  Shared by the cluster module (imports these) and the GUI.
SETUP_ID = 0x200
SETUP_REQ = 0xC0


def probe_channel(chan, dest: int = DEST_HVAC, seconds: float = 2.0,
                  listen_id: int = 0x300, emit=None, frame_log=None) -> dict:
    """Diagnostic, read-only.  On an ALREADY-OPEN raw CAN channel (``chan`` with
    write_can/read/pass_all), remove any id filter, send ONE TP 2.0 channel-setup
    broadcast to 0x200 for ``dest``, and watch every frame for ``seconds``.

    A silent connect is ambiguous -- wrong bus, wrong baud, or the gateway
    answering on an id the normal path's hardware filter drops.  This removes the
    filter and the guessing: it shows, structurally, whether the bus is alive at
    the channel's current baud and what (if anything) replies to the setup and on
    which id.  ``emit(str)`` gets summary lines; ``frame_log(str)`` (optional) gets
    every frame in sniffer wire format.  Returns a summary dict (``_probe``=True)."""
    import time as _t
    emit = emit or (lambda s: None)
    setup = bytes([dest & 0xFF, SETUP_REQ, 0x00, 0x10,
                   listen_id & 0xFF, (listen_id >> 8) & 0xFF, 0x01])
    for fn in ("pass_all", "clear_rx"):
        try:
            getattr(chan, fn)()
        except Exception:
            pass
    seen = {}                                       # can_id -> [count, first_payload_hex]
    emit("Probe: setup 0x200#%s (dest 0x%02X), listening %.1fs..."
         % (setup.hex().upper(), dest, seconds))
    chan.write_can(SETUP_ID, setup)
    deadline = _t.time() + seconds
    while _t.time() < deadline:
        for m in (chan.read(32, 50) or []):
            cid = getattr(m, "can_id", None)
            pl = bytes(getattr(m, "payload", b""))
            rec = seen.setdefault(cid, [0, pl.hex().upper()])
            rec[0] += 1
            if frame_log:
                frame_log("(%.6f) can %03X#%s" % (_t.time(), cid or 0, pl.hex().upper()))
    reply_id = SETUP_ID + dest
    reply = seen.get(reply_id)
    total = sum(v[0] for v in seen.values())
    summary = {
        "_probe": True, "dest": dest, "setup": setup.hex().upper(),
        "frames_total": total,
        "unique_ids": {("0x%03X" % (k or 0)): v for k, v in
                       sorted(seen.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))},
        "setup_reply_id": "0x%03X" % reply_id,
        "setup_replied": reply is not None,
        "reply_payload": (reply[1] if reply else None),
    }
    if reply is not None:
        emit("Setup REPLY on 0x%03X: %s" % (reply_id, reply[1]))
    elif total == 0:
        emit("No frames at all -> wrong bus or wrong baud (try 100000 for C6 comfort CAN).")
    else:
        emit("Bus alive (%d frames) but nothing on 0x%03X -> module not on this bus, "
             "or gateway not routing TP2.0 to dest 0x%02X (try 100000 / comfort CAN)."
             % (total, reply_id, dest))
    return summary


def probe(device: Optional[str] = None, dest: int = DEST_HVAC,
          baud: int = CAN_RATE, seconds: float = 2.0, log=None) -> dict:
    """CLI helper: open an interface, (re)connect the raw CAN channel at ``baud``,
    and run :func:`probe_channel`.  Read-only; opens and closes its own device."""
    log = log or (lambda s: None)
    _iface, dev, ch = open_interface(device, log=log)
    try:
        try:
            ch.close()
        except Exception:
            pass
        from . import j2534 as _j
        raw = dev.connect(_j.CAN, baud)
        log("Probe: bus @ %d bps, dest 0x%02X" % (baud, dest))
        return probe_channel(raw, dest=dest, seconds=seconds, emit=log, frame_log=log)
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---------------------------------------------------------------- CLI
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="ovtsuite-climatronic",
        description="Read/identify a VAG Climatronic (4F/4L 910 043x) over CAN "
                    "(J2534 on Windows, OpenPort 2.0 elsewhere). Read-only.")
    ap.add_argument("--list", action="store_true", help="list CAN interfaces and exit")
    ap.add_argument("--device", help="interface label substring to select")
    ap.add_argument("--dest", type=lambda s: int(s, 0), default=DEST_HVAC,
                    help="TP2.0 logical address (default 0x2C = HVAC over TP2.0; "
                         "0x08 is the KWP address, not the TP2.0 one)")
    ap.add_argument("--baud", type=lambda s: int(s, 0), default=CAN_RATE,
                    help="CAN bit rate (default 500000; try 100000 for C6 comfort CAN)")
    ap.add_argument("--probe", action="store_true",
                    help="diagnostic: pass-all, send one 0x200 setup, log every frame "
                         "for a couple of seconds, then exit")
    ap.add_argument("--probe-seconds", type=float, default=2.0)
    ap.add_argument("--session", type=lambda s: int(s, 0), default=0x89,
                    help="diagnostic session sub-function (default 0x89; try 0x85)")
    ap.add_argument("--level", choices=list(KEY_CONSTS), default="primary")
    ap.add_argument("--no-unlock", action="store_true", help="skip security access")
    ap.add_argument("--id", action="store_true", help="read ECU identification (0x1A 0x9B)")
    ap.add_argument("--dump", metavar="FILE", help="dump flash to FILE")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=FLASH_BASE)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=FLASH_SIZE)
    ap.add_argument("--chunk", type=lambda s: int(s, 0), default=0x1000,
                    help="RequestUpload chunk size (bytes per 0x35), default 0x1000")
    ap.add_argument("--flash-sgo", metavar="FILE",
                    help="FLASH a plain HVAC .sgo (loader + sections) via the "
                         "programming session")
    ap.add_argument("--flash-image", metavar="FILE",
                    help="FLASH a flat 256KB image (requires --loader)")
    ap.add_argument("--loader", metavar="FILE",
                    help="loader blob file for --flash-image")
    ap.add_argument("--start-at", type=int, default=0,
                    help="resume flashing from block N (recovery after a failed write)")
    ap.add_argument("--write", action="store_true",
                    help="ARM the flash: actually write (default is a dry-run plan)")
    ap.add_argument("--test-write", action="store_true",
                    help="NON-DESTRUCTIVE live test: programming session + write "
                         "security + checksum-verify current flash vs the file; "
                         "NO erase/write. Needs --flash-sgo/--flash-image.")
    args = ap.parse_args(argv)

    log = lambda s: print(s, flush=True)            # noqa: E731

    if args.list:
        for i in list_interfaces():
            print("%-40s [%s%s]" % (i.label, i.kind, "" if i.loadable else ", not loadable"))
        return 0

    if args.probe:
        s = probe(args.device, dest=args.dest, baud=args.baud,
                  seconds=args.probe_seconds, log=log)
        log("--- probe summary --- bus dest 0x%02X, setup 0x200#%s" % (s["dest"], s["setup"]))
        log("frames: %d; ids: %s" % (s["frames_total"], ", ".join(s["unique_ids"]) or "(none)"))
        log("setup reply on %s: %s" % (s["setup_reply_id"], "YES" if s["setup_replied"] else "no"))
        return 0

    # ---- flash write (programming session) ----
    if args.flash_sgo or args.flash_image:
        if args.flash_sgo:
            blocks = hvac_sgo_blocks(args.flash_sgo)
            src = args.flash_sgo
        else:
            if not args.loader:
                log("--flash-image requires --loader"); return 2
            img = open(args.flash_image, "rb").read()
            ldr = open(args.loader, "rb").read()
            blocks = image_blocks(img, ldr, args.base)
            src = "%s + %s" % (args.flash_image, args.loader)
        log("Flash plan from %s -- %d blocks:" % (src, len(blocks)))
        for i, (a, dta) in enumerate(blocks):
            log("  [%d] 0x%06X  %6d B  sum=0x%04X%s"
                % (i, a, len(dta), sum(dta) & 0xFFFF,
                   "  (SKIP, resume)" if i < args.start_at else ""))
        if not args.write and not args.test_write:
            log("DRY RUN -- no data sent. Re-run with --test-write (non-destructive "
                "live check) or --write to flash (programming session 10 85, key "
                "seed+0x05814A05).")
            return 0
        if args.test_write:
            _iface, dev, ch = open_interface(args.device, log=log)
            try:
                hvac = Climatronic(ch, dest=args.dest, log=log)
                hvac.open()
                res = hvac.test_write_access(verify_blocks=blocks)
                log("--- write-access test (NON-DESTRUCTIVE) ---")
                log("programming session: %s; seed=%s key=%s"
                    % ("OK" if res["session_ok"] else "FAIL", res["seed"], res["key"]))
                for a, e, s, r in res["verify"]:
                    log("  region 0x%06X..0x%06X sum=0x%04X -> %s" % (a, e, s, r))
                log("(no flash was modified)")
            finally:
                for c in (locals().get("hvac"), locals().get("dev")):
                    try:
                        c and c.close()
                    except Exception:
                        pass
            return 0
        _iface, dev, ch = open_interface(args.device, log=log)
        try:
            hvac = Climatronic(ch, dest=args.dest, log=log)
            hvac.open()
            hvac.flash_write(blocks, start_at=args.start_at,
                             on_percent=lambda done, tot:
                                 log("    %d/%d B" % (done, tot))
                                 if done == tot or done % 4096 == 0 else None)
            log("FLASH COMPLETE.")
        finally:
            try:
                hvac.close()
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass
        return 0

    _iface, dev, ch = open_interface(args.device, log=log)
    try:
        hvac = Climatronic(ch, dest=args.dest, log=log)
        hvac.open()
        try:
            log("Session 0x%02X..." % args.session)
            hvac.start_session(args.session)
        except ClimatronicError as ex:
            log("(session start: %s -- continuing)" % ex)
        if args.id:
            try:
                rec = hvac.read_ecu_identification()
                log("ECU ID (0x9B): %s" % rec.hex())
                printable = bytes(b if 32 <= b < 127 else 0x2E for b in rec)
                log("        ascii: %s" % printable.decode("latin-1"))
            except ClimatronicError as ex:
                log("ECU ID read failed: %s" % ex)
        if not args.no_unlock:
            seed, key = hvac.unlock(args.level)
            if key:
                log("Unlocked: seed=%s key=%s" % (seed.hex(), key.hex()))
        if args.dump:
            log("Dumping 0x%06X..0x%06X (%d bytes)..."
                % (args.base, args.base + args.size - 1, args.size))
            data = hvac.dump(args.base, args.size, args.chunk,
                             on_percent=lambda p: log("  %d%%" % p) if p % 10 == 0 else None)
            with open(args.dump, "wb") as fh:
                fh.write(data)
            log("Wrote %d bytes to %s" % (len(data), args.dump))
            line = dump_checksum(data)
            log(line or "Checksum: not checked (the 4F/4L HVAC module is not installed)")
    finally:
        try:
            hvac.close()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
