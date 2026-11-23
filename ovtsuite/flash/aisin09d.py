# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Aisin TRx (VW "09D" TR-60SN-class) TCU flash transport over CAN TP 2.0.

The 09D wire protocol is KWP2000 / ISO-14230 over VW TP 2.0 (NOT UDS) -- the
same transport the ZF 6HP flasher already uses -- so this mirrors
:class:`ovtsuite.flash.zf6hp.Zf6hpFlash` closely. What differs is the 0x23
readMemoryByAddress *form*: 09D uses a flat 24-bit address
``23 <addr-hi><addr-mid><addr-lo> <len>`` and the tool dumps the WHOLE 1 MiB
flash sequentially (0x000000..0x100000 in 0xFE-byte blocks), where 6HP uses the
same 5-byte shape but treats req[1] as a 64 KiB bank index and only reads its
calibration banks. All of this was captured end-to-end and byte-verified against
``trx_sim`` (the bench simulator) -- see project doc
``claude/aisin_09d_container_and_flash.md``.

READ is implemented and proven. WRITE is now implemented too: the bench
write-capture proved transferData is PLAINTEXT (no cipher), the in-image
checksum at 0x203AC is SOLVED (:func:`compute_checksum` -- an additive BE32
word-sum over [0x2000,0xDFFFF] that must total K=0x000AA804), and the exact
0x34/0x31/0x36/0x37 framing + 0xC5 verify are reproduced VERBATIM from the real
tool's capture (Reference/Logs/ecu-sim-20261007-231906.log). It is validated
against ``trx_sim``; a real-TCU bench confirmation is still wanted (the sim can't
exercise a real erase time or a programming-session channel drop), so the GUI
keeps 09D read-only until Bear confirms on the bench -- call
:meth:`Aisin09dFlash.write_image` directly to test. See project doc
``claude/aisin_09d_container_and_flash.md`` §6.
"""
from __future__ import annotations

import struct
import time
from typing import Dict, Optional, Sequence

try:
    from .transport import KLineError, KLineTimeout
except ImportError:                      # flat import (e.g. inside the 32-bit bridge)
    from transport import KLineError, KLineTimeout     # type: ignore
try:
    from .tp20_transport import Tp20Channel, Tp20Params
except ImportError:
    from tp20_transport import Tp20Channel, Tp20Params  # type: ignore
try:
    from . import crypto as _crypto
except ImportError:
    import crypto as _crypto             # type: ignore

#: TP 2.0 logical address the TCU answers at (captured: the tool set up logical
#: 0x02 -- same address class as ZF 6HP). The tester listens on 0x300 and the
#: TCU is assigned 0x740, matching the bench capture.
DEST = 0x02
LISTEN_ID = 0x300

IMAGE_SIZE = 0x100000              # 1 MiB -- the full addressable TCU flash
READ_CHUNK = 0xFE                  # 254 bytes/request, exactly as the tool uses

SID_START_SESSION = 0x10
SID_READ_ECU_ID = 0x1A
SID_READ_MEM = 0x23
SID_SECURITY = 0x27                # securityAccess (write only)
SID_START_ROUTINE = 0x31          # startRoutineByLocalId
SID_ROUTINE_RESULTS = 0x33        # requestRoutineResultsByLocalId
SID_REQ_DOWNLOAD = 0x34           # requestDownload
SID_TRANSFER_DATA = 0x36          # transferData
SID_TRANSFER_EXIT = 0x37          # requestTransferExit
SID_STOP_COMM = 0x82               # the tool ends the session with this
POS = 0x40
NRC_RESPONSE_PENDING = 0x78

SESSION_EXTENDED = 0x86            # read session (captured "10 86")
SESSION_PROGRAMMING = 0x85        # write session (captured "10 85")
IDENT_PN = 0x9B                    # readEcuIdentification localId -> spare-part number

# ---- write-sequence constants, taken VERBATIM from the real tool's captured
# write of 09D927750HN_WRITETEST against the sim (Reference/Logs/
# ecu-sim-20261007-231906.log). These are content-INDEPENDENT (the cal region
# and erase range are fixed for the 09D), so they are reproduced as captured:
ROUTINE_ERASE = 0xC4              # startRoutine local id: erase flash
ROUTINE_VERIFY = 0xC5             # startRoutine local id: checksum verify
WRITE_REGION = (0x2000, 0xDFFFF)  # the cal region written (0xDE000 bytes)
WRITE_CHUNK = 0x80                # 128 data bytes per transferData frame (captured)
#: requestDownload params after SID 0x34 -> "34 00 20 00 00 0D E0 00":
#: addr=0x002000 (3B), 0x00, size=0x0DE000 (3B).
_DL_PARAMS = bytes([0x00, 0x20, 0x00, 0x00, 0x0D, 0xE0, 0x00])
#: erase routine params after SID 0x31 -> "31 C4 00 20 00 0D FF FF 00 01 02 03 04 05":
#: localId 0xC4, start=0x002000 (3B), end=0x0DFFFF (3B), 6-byte erase signature.
_ERASE_PARAMS = bytes([ROUTINE_ERASE, 0x00, 0x20, 0x00, 0x0D, 0xFF, 0xFF,
                       0x00, 0x01, 0x02, 0x03, 0x04, 0x05])

#: The SA2 seed/key tape carried in every 09D .sgo metadata block, verified to
#: reproduce the firmware's own key routine. Read does not need security (the
#: captured read used no 0x27), but the write path will -- kept here so the
#: write implementation, when its cipher is confirmed, has the key to hand.
SA2_TAPE = bytes.fromhex("6803819354414a494a058753414a49494c")


def trx_key(seed: bytes, tape: bytes = SA2_TAPE) -> bytes:
    """The 4-byte key for a 4-byte seed, via the shipped SA2 VM + the 09D tape."""
    return _crypto.sa2_execute(tape, int.from_bytes(bytes(seed), "big")).to_bytes(4, "big")


# ---- flash checksum (SOLVED 2026-10-08 from bench write-test captures) -----
# The 09D verifies a 32-bit ADDITIVE checksum (not a CRC) over the calibration
# region [0x2000, 0xDFFFF] inclusive. A 16-byte descriptor at 0x203A0 holds
# [count=1][start=0x2000][end=0xDFFFF][checksum]; the 4 checksum bytes are the
# last word, at 0x203AC (big-endian). Integrity holds when the sum of every
# big-endian 32-bit word across the region -- the checksum word INCLUDED --
# equals the firmware constant CKSUM_K. To correct a modified image, set the
# checksum word so the total hits CKSUM_K.
#
# Proof it is a sum, not a CRC: bench write-test captures showed a given bit
# flip produced the SAME checksum delta at offsets 0x2000, 0x2004, 0x070000 and
# 0xDFFFC -- position-INDEPENDENT (a CRC's delta is position-dependent). The
# delta is additive and negated (word down by d -> checksum up by d). K is
# identical across 09D927750HN_3403 and LD_3405, and the formula reproduces the
# ECU's own checksum on the bench readback (v4: 0xF552D94B). See project doc
# claude/aisin_09d_container_and_flash.md.
CKSUM_K = 0x000AA804           # required region word-sum (firmware constant)
CKSUM_DESC_OFFSET = 0x203A0    # [count=1][start][end][checksum]
CKSUM_OFFSET = 0x203AC         # the 4 checksum bytes (BE32) == descriptor + 12
CKSUM_START = 0x2000           # region start (inclusive)
CKSUM_END = 0xDFFFF            # region end (inclusive)


def _cksum_descriptor(image: bytes):
    """(start, end, cksum_offset) read from the on-image descriptor, falling
    back to the fixed 09D layout so a layout shift is tolerated."""
    o = CKSUM_DESC_OFFSET
    if len(image) >= o + 16:
        count, start, end = struct.unpack_from(">III", image, o)
        if count == 1 and 0 <= start < end < len(image):
            return start, end, o + 12
    return CKSUM_START, CKSUM_END, CKSUM_OFFSET


def compute_checksum(image: bytes) -> int:
    """The 32-bit checksum word the 09D expects for ``image``:
    ``C = (CKSUM_K - sum(BE32 words over [start, end] except the checksum
    word)) mod 2**32``. ``image`` must be the full flash image (addresses are
    absolute)."""
    start, end, cks = _cksum_descriptor(image)
    if len(image) < end + 1:
        raise KLineError("aisin09d: image too small for checksum region "
                         "(need 0x%X bytes, have 0x%X)" % (end + 1, len(image)))
    s = 0
    for o in range(start, end + 1, 4):
        if o == cks:
            continue
        s = (s + struct.unpack_from(">I", image, o)[0]) & 0xFFFFFFFF
    return (CKSUM_K - s) & 0xFFFFFFFF


def apply_checksum(image: bytes) -> bytes:
    """Return a copy of ``image`` with the 09D checksum word corrected."""
    start, end, cks = _cksum_descriptor(image)
    out = bytearray(image)
    struct.pack_into(">I", out, cks, compute_checksum(out))
    return bytes(out)


def verify_checksum(image: bytes) -> bool:
    """True when the stored 09D checksum is valid (region word-sum == K)."""
    start, end, _ = _cksum_descriptor(image)
    if len(image) < end + 1:
        return False
    s = 0
    for o in range(start, end + 1, 4):
        s = (s + struct.unpack_from(">I", image, o)[0]) & 0xFFFFFFFF
    return s == CKSUM_K


class WriteNotConfirmed(KLineError):
    """Kept for callers that catch it. :meth:`Aisin09dFlash.write_image` no
    longer raises it: transferData is plaintext and the 09D checksum is solved
    (:func:`compute_checksum`); the write is held back only in the GUI until it
    is confirmed on a real TCU."""


#: K-line (ISO 14230) 5-baud init address of the TCU. Taken from the bench
#: shim of the original tool (FIVE_BAUD_INIT, address 0x02); not yet confirmed
#: with this flasher on a real car.
K_ADDRESS = 0x02
#: The address the TCU's requests are framed to. It is not the 5-baud address:
#: an EDC16 woken at 0x01 is asked at 0x10 (``83 10 F1 ...``). A 09D woken at
#: 0x02 answered a header with NO addresses (``02 1A 9B ck``) on the bench and
#: replied from 0x1A (``83 F1 1A 7F 1A 12``), and 0x1A is also the target in the
#: original tool's ``82 1A F1 1A 9B``. Not answered at 0x02, 0x10 or 0x20.
K_REQUEST_ADDRESS = 0x1A
#: Header forms tried after the wake-up, in this order, to find the one the TCU
#: answers: None = no address bytes at all, else addressed to that address.
HEADER_CANDIDATES = (None, K_REQUEST_ADDRESS, 0x10)
#: What a fast init's StartCommunication is addressed to (EDC16: 0x10).
FAST_INIT_TARGET = K_REQUEST_ADDRESS
#: K-line baud escalation, the same speed bytes the EDC16 path sends
#: (``10 <session> <byte>``; see ovtsuite.flash.diag.SPEEDS).
KLINE_SPEEDS = {"Low": (0x00, 10400), "Medium": (0x87, 124800), "High": (0xA7, 244898)}
#: The early K-line 09D may have a 512 KiB flash (cal 0x10000..0x7FFFF) rather
#: than the CAN part's 1 MiB -- see :mod:`ovtsuite.flash.trx_kline_sim`. A
#: K-line read does not assume either: ``read_image(image_size=None)`` reads up
#: to 1 MiB and returns 512 KiB when the part refuses 0x80000 as out of range
#: (or wraps); a timeout there is retried and then an error, never a cut.
SMALL_IMAGE_SIZE = 0x80000
KLINE_IMAGE_SIZE = None                # automatic (1 MiB or 512 KiB)
#: asks for the first block past 512 KiB, in automatic size: a timeout is
#: retried this many times before the read fails (it never cuts the image)
_EDGE_TRIES = 3
#: requestOutOfRange: the one answer that says a 512 KiB part has ended
NRC_OUT_OF_RANGE = 0x31


class KLineKwpChannel:
    """The same ``open/close/send/recv`` face as a TP 2.0 channel, over a K-line
    link (:class:`ovtsuite.flash.transport.KLine` or the J2534 K-line link).

    The 09D speaks the same KWP2000 requests on both buses; only the carrier
    differs. ``send`` frames a payload as ISO 14230 and ``recv`` unframes one
    response, so :class:`Aisin09dFlash` runs unchanged. The 5-baud wake-up is
    done by :meth:`open`; there is no baud raise (the flash is read at the
    link's 10400)."""

    def __init__(self, link, address: int = K_ADDRESS, log=None,
                 request_address: int = K_REQUEST_ADDRESS):
        from .kwp import Kwp2000                 # lazy: the flat/bridge import has no package
        self.link = link
        self.address = address                   # 5-baud init address
        self.request_address = request_address   # what requests (and fast init) go to
        self._log = log or (lambda s: None)
        self.kwp = Kwp2000(link, controller=request_address, log=log)
        self.kwp.fast = True                     # whole frame per write, echo drained once
        self._woken = False
        self.is_stop = None

    @property
    def is_stop(self):
        return self.kwp.is_stop

    @is_stop.setter
    def is_stop(self, fn):
        self.kwp.is_stop = fn

    def open(self):
        if not self._woken:
            if hasattr(self.link, "fast_target"):
                self.link.fast_target = self.request_address   # StartCommunication target
            self.link.wake_up(address=self.address)           # 5-baud address, if it comes to that
            self._woken = True
            mode = getattr(self.link, "last_init", None)
            if mode is None:                      # the J2534 link: its mode flag
                mode = "fast" if getattr(self.link, "_fast", False) else "slow"
            # Both inits leave the TCU answering requests framed to its
            # controller address (like the EDC16's 0x10), not to the 5-baud one --
            # or to a header with no addresses. Find which, by asking.
            self.kwp.controller = self.request_address
            self.log("aisin09d: K-line up by %s init" % mode)
            self._detect_header()

    @property
    def addressless(self) -> bool:
        """True once the TCU has answered a header with no address bytes."""
        return self.kwp.addressless

    def _detect_header(self):
        """Ask ``1A 9B`` in each header form of :data:`HEADER_CANDIDATES` until
        the TCU answers (any reply, even a negative one, shows the form is
        understood) and frame everything that follows that way. If nothing
        answers the default stays, and the real request fails with its own
        error."""
        found = self.kwp.detect_header(HEADER_CANDIDATES, (0x1A, bytes([IDENT_PN])))
        if found is None:
            self.kwp.controller = self.request_address
            self.log("aisin09d: no header form was answered; using 0x%02X"
                     % self.request_address)
            return
        cand, msg = found
        self.log("aisin09d: the TCU answers %s%s" % (
            "a header without addresses" if cand is None else "requests addressed to 0x%02X" % cand,
            "" if msg.src is None else " (replying from 0x%02X)" % msg.src))

    def log(self, msg):
        self._log(msg)

    #: entering the programming session does not drop a K-line; there is
    #: nothing to re-open
    def reopen(self):
        pass

    def close(self):
        self._woken = False                      # the link itself belongs to the caller

    def send(self, payload: bytes):
        payload = bytes(payload)
        self.kwp.send(payload[0], payload[1:])         # the dialog frames it as detected

    def recv(self, timeout_ms: Optional[int] = None) -> bytes:
        saved = None
        if timeout_ms is not None:
            try:
                saved = self.link.read_timeout
                self.link.read_timeout = max(0.1, timeout_ms / 1000.0)
            except Exception:
                saved = None
        try:
            msg = self.kwp.receive()
        finally:
            if saved is not None:
                self.link.read_timeout = saved
        return bytes([msg.service]) + msg.body


class Aisin09dFlash:
    """Read (and, once confirmed, write) one Aisin TRx 09D TCU over CAN TP 2.0
    or K-line.

    ``can`` is a raw CAN channel (write_can/read/pass_*), from which a
    :class:`Tp20Channel` is built; or pass ``tp`` -- an already-open TP 2.0
    channel (e.g. the ``.tp`` of a CanKLine the GUI opened for this address) --
    to reuse it, the same shortcut ZF 6HP offers. ``kline`` is an open K-line
    link instead (see :class:`KLineKwpChannel`).
    """

    def __init__(self, can=None, log=None, tp=None, kline=None, speed="Low"):
        self._log = log or (lambda s: None)
        #: K-line only: "Low" (stay at 10400), "Medium" or "High" (try to raise)
        self.speed = speed
        if kline is not None:
            self.tp = KLineKwpChannel(kline, log=log)
        elif tp is not None:
            self.tp = tp
        elif can is not None:
            self.tp = Tp20Channel(can, dest=DEST, listen_id=LISTEN_ID,
                                  params=Tp20Params(), log=log)
        else:
            raise KLineError("aisin09d: needs a CAN channel, a tp channel or a K-line link")

    def log(self, msg):
        self._log(msg)

    # ---- transport ----
    def connect(self):
        self.tp.open()

    def close(self):
        self.tp.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, payload: bytes, absorb_pending: bool = True,
                 timeout_ms: Optional[int] = None) -> bytes:
        """Send one KWP payload, return the (non-pending) response payload."""
        self.tp.send(payload)
        for _ in range(64):
            resp = self.tp.recv(timeout_ms) if timeout_ms is not None else self.tp.recv()
            if absorb_pending and len(resp) == 3 and resp[0] == 0x7F and resp[2] == NRC_RESPONSE_PENDING:
                self.log("responsePending for service 0x%02X; waiting" % resp[1])
                continue
            return resp
        raise KLineTimeout("aisin09d: stuck in responsePending")

    @staticmethod
    def _check_positive(resp: bytes, sid: int, context: str):
        if not resp or resp[0] != (sid | POS):
            nrc = resp[2] if len(resp) >= 3 and resp[0] == 0x7F else None
            raise KLineError("aisin09d: %s failed (got %s%s)" % (
                context, resp.hex(" ") if resp else "<empty>",
                ", NRC 0x%02X" % nrc if nrc is not None else ""))

    def _stop_communication(self):
        try:
            self._request(bytes([SID_STOP_COMM]), absorb_pending=False)
        except Exception:
            pass

    def read_ecu_identification(self, ident_id: int = IDENT_PN) -> bytes:
        """1A 9B -> the full identification block (spare-part number etc.).
        Informative; the tool issues it before the read. Non-fatal on NAK."""
        try:
            resp = self._request(bytes([SID_READ_ECU_ID, ident_id]))
            if resp and resp[0] == (SID_READ_ECU_ID | POS):
                return bytes(resp[2:])
            self.log("aisin09d: readEcuId 0x%02X not answered (%s)"
                     % (ident_id, resp.hex(" ") if resp else "<empty>"))
        except Exception as ex:
            self.log("aisin09d: readEcuId 0x%02X skipped (%s)" % (ident_id, ex))
        return b""

    def _start_read_session(self):
        """startDiagnosticSession 0x86, sent ONCE. On a K-line link asked for
        more than 10400 baud the same request carries the speed byte (the way
        the EDC16 path does it), our end switches, and the TCU must answer a
        request at the new rate; a refusal of the speed byte falls back to the
        plain request (the only case it is sent twice), and a failed check
        at the new rate tells the TCU to leave the high baud (see
        :meth:`_leave_high_baud`) or fails the read cleanly."""
        link = getattr(self.tp, "link", None)
        speed_byte, baud = KLINE_SPEEDS.get(self.speed, KLINE_SPEEDS["Low"])
        if link is None or speed_byte == 0:
            resp = self._request(bytes([SID_START_SESSION, SESSION_EXTENDED]))
            self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x86")
            return
        try:
            resp = self._request(bytes([SID_START_SESSION, SESSION_EXTENDED, speed_byte]))
            self._check_positive(resp, SID_START_SESSION, "baud raise")
        except (KLineError, KLineTimeout) as ex:
            self.log("aisin09d: TCU refused %d baud (%s); staying at 10400." % (baud, ex))
            resp = self._request(bytes([SID_START_SESSION, SESSION_EXTENDED]))
            self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x86")
            return
        time.sleep(0.075)
        link.set_baud(baud)
        try:
            link.clear_rx()
            resp = self._request(bytes([SID_READ_ECU_ID, IDENT_PN]))
            self._check_positive(resp, SID_READ_ECU_ID, "check at %d baud" % baud)
            self.log("aisin09d: link speed -> %d baud." % baud)
        except (KLineError, KLineTimeout):
            self.log("aisin09d: no answer at %d baud; asking the TCU to leave it." % baud)
            self._leave_high_baud(link, baud)

    def _leave_high_baud(self, link, baud):
        """The TCU switched to ``baud`` the moment it accepted the session, so
        putting only OUR end back to 10400 would leave the two on different
        rates. While still at ``baud``, ask it to return (the session request
        with speed byte 0, which the EDC16 path uses for 10400 too); then take
        our end back and prove the TCU answers at 10400. If it does not, fail
        the read with a clear message instead of reading on a dead link."""
        try:
            self._request(bytes([SID_START_SESSION, SESSION_EXTENDED, 0x00]),
                          absorb_pending=False, timeout_ms=500)
        except (KLineError, KLineTimeout):
            pass                                  # it may not hear us either
        time.sleep(0.075)
        link.set_baud(10400)
        try:
            link.clear_rx()
        except Exception:
            pass
        try:
            resp = self._request(bytes([SID_READ_ECU_ID, IDENT_PN]))
            self._check_positive(resp, SID_READ_ECU_ID, "check back at 10400 baud")
        except (KLineError, KLineTimeout) as ex:
            raise KLineError("aisin09d: the TCU did not answer at %d baud and is not "
                             "answering at 10400 either (%s); it may still be at the high "
                             "baud -- switch the ignition off and on, then read at Low speed"
                             % (baud, ex))
        self.log("aisin09d: the TCU is back at 10400 baud; the read carries on there.")

    # ---- read ----
    def read_image(self, on_percent=None, is_stop=None,
                   image_size: Optional[int] = IMAGE_SIZE) -> bytes:
        """Dump the whole flash (0x000000..image_size) exactly as the tool does:
        startDiagnosticSession 0x86, then flat 24-bit readMemoryByAddress in
        0xFE blocks, the last block trimmed to land on the end. No security
        (the captured read used none). Returns an ``image_size`` byte image;
        a region the TCU NAKs is left 0xFF and stops the dump there.

        ``image_size=None`` is automatic: read up to 1 MiB and give back 512 KiB
        when that is all the part has -- it refuses 0x80000 as out of range
        (a definite answer), or its address space wraps so the second half
        repeats the first. A timeout there is retried and then fails the read:
        a silent link is not taken as the end of the flash."""
        auto = image_size is None
        if auto:
            image_size = IMAGE_SIZE
        self.connect()
        try:
            pn = self.read_ecu_identification()
            if pn:
                try:
                    self.log("aisin09d: TCU PN %r" % pn[:16].decode("latin1").strip())
                except Exception:
                    pass
            self._start_read_session()

            buf = bytearray(b"\xFF" * image_size)
            total = (image_size + READ_CHUNK - 1) // READ_CHUNK
            done = 0
            addr = 0
            while addr < image_size:
                if is_stop and is_stop():
                    raise KLineError("aisin09d: read cancelled")
                length = min(READ_CHUNK, image_size - addr)
                if auto and addr < SMALL_IMAGE_SIZE < addr + length:
                    length = SMALL_IMAGE_SIZE - addr     # a block never straddles 512 KiB
                req = bytes([SID_READ_MEM, (addr >> 16) & 0xFF,
                             (addr >> 8) & 0xFF, addr & 0xFF, length])
                at_edge = auto and addr == SMALL_IMAGE_SIZE
                tries = _EDGE_TRIES if at_edge else 1
                for attempt in range(tries):
                    try:
                        resp = self._request(req)
                        break
                    except KLineError as ex:
                        # a timeout at the 512 KiB edge is NOT the end of the
                        # part: a stray lost frame looks the same. Ask again;
                        # only a definite refusal (below) ends the image there.
                        if not at_edge:
                            raise
                        if attempt + 1 >= tries:
                            raise KLineError(
                                "aisin09d: no answer at 0x%06X after %d tries (%s); a "
                                "silent link is not taken as the end of a 512 KiB part. "
                                "Read again, or choose the 512 KiB size." % (addr, tries, ex))
                        self.log("aisin09d: no answer at 0x%06X (try %d of %d); asking again"
                                 % (addr, attempt + 1, tries))
                if len(resp) >= 3 and resp[0] == 0x7F:
                    if at_edge and resp[2] != NRC_OUT_OF_RANGE:
                        raise KLineError("aisin09d: addr 0x%06X refused (%s): not the "
                                         "out-of-range answer of a 512 KiB part's end"
                                         % (addr, resp.hex(" ")))
                    self.log("aisin09d: addr 0x%06X NAK'd (%s); stopping read"
                             % (addr, resp.hex(" ")))
                    break
                self._check_positive(resp, SID_READ_MEM,
                                     "readMemoryByAddress addr 0x%06X" % addr)
                data = resp[1:]
                buf[addr:addr + len(data)] = data
                addr += len(data) if data else length
                done += 1
                if on_percent:
                    on_percent(min(99, int(100 * done / total)))
            if on_percent:
                on_percent(100)
            if auto:
                half = SMALL_IMAGE_SIZE
                if addr == half:                       # the part ended at 512 KiB
                    buf = buf[:half]
                elif addr >= image_size and buf[half:] == buf[:half] and any(
                        b != 0xFF for b in buf[:half]):
                    self.log("aisin09d: second half repeats the first; a 512 KiB "
                             "part whose address space wraps")
                    buf = buf[:half]
            return bytes(buf)
        finally:
            self._stop_communication()

    # ---- security (write only) ----
    def _unlock(self):
        """KWP securityAccess: requestSeed (0x27 01) -> sendKey (0x27 02) with
        the SA2-derived key (:func:`trx_key`). Captured write used level 01/02;
        a zero/empty seed means already-unlocked and sendKey is skipped."""
        resp = self._request(bytes([SID_SECURITY, 0x01]))
        self._check_positive(resp, SID_SECURITY, "securityAccess requestSeed 01")
        seed = bytes(resp[2:])
        if not seed or not any(seed):
            return                                   # already unlocked / no seed
        key = trx_key(seed)
        resp = self._request(bytes([SID_SECURITY, 0x02]) + key)
        self._check_positive(resp, SID_SECURITY, "securityAccess sendKey 02")

    # ---- write ----
    def write_image(self, image: bytes, on_percent=None, on_stage=None,
                    is_stop=None, force_full: bool = False):
        """Flash a full 09D calibration image, reproducing the real tool's
        captured write sequence (Reference/Logs/ecu-sim-20261007-231906.log):

            10 85                 programmingSession
            (reopen TP2.0 channel -- the ECU may drop it entering 0x85)
            27 01 / 27 02 <key>   securityAccess (SA2 tape, see trx_key)
            34 <_DL_PARAMS>       requestDownload (cal 0x2000, len 0xDE000)
            31 C4 <_ERASE_PARAMS> erase 0x2000..0xDFFFF  (held, responsePending)
            33 C4 -> 73 C4 00     poll erase complete
            36 <128B> * N         transferData, PLAINTEXT, no block counter
            37                    requestTransferExit
            31 C5 <rng><ck16>     verify: checksum low-16 of the written cal
            33 C5 -> 73 C5 00     poll verify ok
            82                    stopCommunication

        The image's 0x203AC checksum is corrected first (:func:`apply_checksum`)
        and its low word is handed to the 0xC5 verify routine, so the TCU's own
        integrity check passes. transferData has NO cipher (bench-proven) and no
        block-sequence counter; a block is resent on NAK/timeout (the TP2.0 layer
        retransmits dropped frames beneath this). ``force_full`` is accepted for
        signature parity with the ZF6HP writer but is a no-op here (the 09D has a
        single cal region, always written in full). ``image`` must cover at least
        0x0..0xDFFFF (a full 1 MiB read-image or the 0xE0000 cal image).

        NOTE: validated against ``trx_sim``; confirm against a real TCU on the
        bench (the sim cannot exercise a real erase time or a programming-session
        channel drop) before relying on it. The GUI keeps 09D read-only until
        that bench confirmation; call this directly to test."""
        region_start, region_end = WRITE_REGION
        need = region_end + 1
        if len(image) < need:
            raise KLineError("aisin09d: write_image needs an image through 0x%X "
                             "(got 0x%X)" % (need, len(image)))
        image = apply_checksum(bytes(image))         # correct 0x203AC first
        ck16 = compute_checksum(image) & 0xFFFF
        data = image[region_start:region_end + 1]     # 0xDE000 plaintext cal bytes

        def stage(label):
            if on_stage:
                on_stage(label)
            self.log(label)

        self.connect()
        try:
            stage("Entering programming session")
            resp = self._request(bytes([SID_START_SESSION, SESSION_PROGRAMMING]))
            self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x85")
            # Entering the programming session can make the ECU tear down the
            # TP2.0 channel (seen on the sibling ZF6HP path); re-run TP2.0 setup
            # on the EXISTING CAN channel before security. Harmless if it didn't.
            reopen = getattr(self.tp, "reopen", None) or getattr(self.tp, "open", None)
            if reopen is not None:
                try:
                    reopen()
                except Exception as ex:
                    self.log("aisin09d: channel reopen after 0x85 skipped (%s)" % ex)

            stage("Security access")
            self._unlock()

            stage("requestDownload")
            resp = self._request(bytes([SID_REQ_DOWNLOAD]) + _DL_PARAMS)
            self._check_positive(resp, SID_REQ_DOWNLOAD, "requestDownload")

            stage("Erasing flash")
            resp = self._request(bytes([SID_START_ROUTINE]) + _ERASE_PARAMS)
            self._check_positive(resp, SID_START_ROUTINE, "start erase routine 0xC4")
            # A real erase takes seconds and answers with responsePending; poll
            # the result against a generous deadline (mirrors the ZF6HP writer).
            deadline = time.monotonic() + 60.0
            while True:
                if is_stop and is_stop():
                    raise KLineError("aisin09d: write cancelled")
                try:
                    resp = self._request(bytes([SID_ROUTINE_RESULTS, ROUTINE_ERASE]),
                                         timeout_ms=8000)
                except KLineTimeout:
                    resp = b""
                if (resp and resp[0] == (SID_ROUTINE_RESULTS | POS)
                        and len(resp) > 1 and resp[1] == ROUTINE_ERASE):
                    break
                if time.monotonic() >= deadline:
                    raise KLineError("aisin09d: erase did not complete in 60s")
                time.sleep(0.1)
            if len(resp) > 2 and resp[2] != 0x00:
                raise KLineError("aisin09d: erase reported failure %s" % resp[2:].hex(" "))

            stage("Transferring calibration")
            total = len(data)
            for off in range(0, total, WRITE_CHUNK):
                if is_stop and is_stop():
                    raise KLineError("aisin09d: write cancelled")
                chunk = data[off:off + WRITE_CHUNK]
                # Resend this block on a NAK/timeout. transferData carries no
                # block-sequence counter, so a resend is just the same 36+data.
                last = None
                for _ in range(4):
                    try:
                        resp = self._request(bytes([SID_TRANSFER_DATA]) + chunk)
                    except KLineTimeout as ex:
                        last = ex
                        continue
                    if resp and resp[0] == (SID_TRANSFER_DATA | POS):
                        break
                    last = resp
                else:
                    detail = (last.hex(" ") if isinstance(last, (bytes, bytearray))
                              else str(last))
                    raise KLineError("aisin09d: transferData failed at 0x%06X (%s)"
                                     % (region_start + off, detail))
                if on_percent:
                    on_percent(min(99, int(100 * (off + len(chunk)) / total)))

            stage("Finishing transfer")
            resp = self._request(bytes([SID_TRANSFER_EXIT]))
            self._check_positive(resp, SID_TRANSFER_EXIT, "requestTransferExit")

            stage("Verifying checksum")
            verify = bytes([ROUTINE_VERIFY, 0x00, 0x20, 0x00, 0x0D, 0xFF, 0xFF,
                            (ck16 >> 8) & 0xFF, ck16 & 0xFF])
            resp = self._request(bytes([SID_START_ROUTINE]) + verify)
            self._check_positive(resp, SID_START_ROUTINE, "start verify routine 0xC5")
            resp = self._request(bytes([SID_ROUTINE_RESULTS, ROUTINE_VERIFY]))
            self._check_positive(resp, SID_ROUTINE_RESULTS, "verify result 0xC5")
            if len(resp) > 2 and resp[2] != 0x00:
                raise KLineError("aisin09d: post-write checksum verify reported "
                                 "failure %s" % resp[2:].hex(" "))
            if on_percent:
                on_percent(100)
        finally:
            self._stop_communication()


__all__ = ["Aisin09dFlash", "KLineKwpChannel", "K_ADDRESS", "KLINE_IMAGE_SIZE",
           "WriteNotConfirmed", "trx_key",
           "compute_checksum", "apply_checksum", "verify_checksum",
           "CKSUM_K", "CKSUM_OFFSET", "CKSUM_START", "CKSUM_END",
           "WRITE_REGION", "WRITE_CHUNK", "SESSION_PROGRAMMING",
           "DEST", "LISTEN_ID", "IMAGE_SIZE", "READ_CHUNK", "SA2_TAPE"]
