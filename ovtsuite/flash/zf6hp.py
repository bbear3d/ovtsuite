# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 6HP TCU flash read/write over CAN (VW TP 2.0 transport, J2534 -- target
OpenPort 2.0; K-line UNCONFIRMED for this TCU, not implemented here).

Protocol decoded from two real CAN captures of a 3rd-party flasher tool doing a
full read and a full write of a 4F9910156A_0020 (6HP-19A, 3.0TFSI RdW) TCU --
Reference/Logs/``6HP-19A CAN Bus Read.log`` / ``...Write.log`` -- reassembled
with :mod:`ovtsuite.sniff.tp20` and cross-checked byte-for-byte: the read
reconstructs identically to the tool's own ``..._maps-sniffed.bin`` (see
project memory ``zf6hp-flash-transport``), and both logs' request/response
sequences are replayed verbatim in tests/test_flash_zf6hp.py against a
:class:`tests.mock_tp20.MockTp20Ecu`.

TRANSPORT: the TCU speaks the SAME VW TP2.0-over-CAN mechanics as EDC16
(channel setup on CAN id 0x200, A0/A1/A3/A8 control, 0x/1x/2x/3x data framing,
Bx/9x ack) -- see :mod:`ovtsuite.flash.tp20_transport`, reused unchanged here
via :class:`Tp20Channel` directly (dest 0x02, tester listens on 0x300). No
K-line facade is needed: this module talks bare KWP payloads straight over
the TP 2.0 channel, unlike :mod:`ovtsuite.flash.edc16` which has to speak
through the K-line-shaped :class:`CanKLine` for a state machine written
against a real K-line first.

READ (fully implemented, area-general): plain KWP2000 -- readEcuIdentification,
securityAccess level (0x03, 0x04), startDiagnosticSession 0x86, then bulk
``readMemoryByAddress`` (SID 0x23) in <=254-byte chunks. Request format
verified by full reconstruction against the reference binary:
``23 <area:1> <addr:2 BE> <len:1>`` where ``addr`` is a 16-bit offset inside a
64 KiB window selected by ``area`` -- i.e. absolute address = area*0x10000 +
addr. The captured session read areas 0x04-0x07 (CAN addresses
0x040000-0x07FFFF, the TCU's "maps" calibration window); :func:`read_areas`
takes the area list as a parameter, defaulting to that range.

WRITE (mechanics implemented; requestDownload/erase params CAPTURED verbatim,
verify/checksum params now DERIVED -- see below): startDiagnosticSession 0x85
(refused until security), securityAccess level (0x01, 0x02) TWICE (once to
enter the session, once more -- fresh seed -- before the first download:
bench-confirmed, not a guess), then per area: requestDownload (SID 0x34),
startRoutineByLocalId 0xC4 (erase, KWP routineControl addressed by local id
rather than EDC16's raw ``31 C4``), transferData (SID 0x36, <=254-byte
chunks), requestTransferExit (0x37), startRoutineByLocalId 0xC5
(verify/checksum -- see :func:`_checksum_routine_params` and
:func:`ovtsuite.flash.crypto.zf6hp_area_checksum`: fully decoded as
``0xC5 + start:3B + end:3B + checksum:2B`` where checksum is
``zlib.crc32(plaintext) & 0xFFFF``, and reused BEFORE erasing too, to skip an
area entirely if it already matches -- see write_areas' docstring). The
requestDownload/erase parameter BYTES beyond the leading area-index byte and
the erase address range are NOT decoded (see module docstring continuation
below) --
_AREA_TEMPLATES below are the exact bytes captured for areas 0x04 and 0x05 (the
only two areas the capture wrote; 0x06/0x07 were read-only in this operation),
used verbatim. Extending to other areas, other images, or other TCU software
needs those bytes decoded first -- don't guess them.

TWO DELIBERATELY DEFERRED SECRETS -- BOTH SECURITY LEVELS NOW SOLVED, ONE
ITEM STILL OPEN (see project memory ``zf6hp-flash-transport`` for the full
history):

1. security_key_fn(level, seed) -> key -- the seed/key algorithm for each
   security level. BOTH LEVELS SOLVED (2026-09-21):
   - READ level (0x03/0x04): plain seed + 0x11C5A, 32-bit wraparound (see
     :func:`ovtsuite.flash.crypto.zf6hp_read_key`) -- recovered by
     cryptanalysis of 4 independent real seed/key pairs.
   - WRITE level (0x01/0x02): a VW "SA2" bytecode tape run against the seed
     (see :func:`ovtsuite.flash.crypto.zf6hp_write_key`) -- NOT a closed-form
     transform (every standard family was tried and ruled out first); found
     unencrypted in the raw .sgo flash-delivery container's own metadata
     block and verified 18/18 against real captured write seed/key pairs.
   :data:`default_security_key_fn` implements both and is used automatically
   if no ``security_key_fn`` is supplied; pass your own only if you need to
   override it (e.g. a different TCU hardware variant with a different write
   tape -- see the SA2 finding in project memory for the one known exception).
2. encrypt_fn(area_plaintext: bytes) -> bytes -- CIPHER IDENTIFIED, ENCODER
   BUILT, WIRED IN AS THE DEFAULT (2026-09-21). Encryption isn't an optional
   extra for a 6HP write any more than it is for EDC16's
   (:func:`ovtsuite.flash.edc16.Edc16Flash.write_flash` calls
   :func:`ovtsuite.flash.crypto.encrypt` unconditionally, no opt-in) -- it's
   the only way transferData is ever sent, so :class:`Zf6hpFlash` now
   defaults ``encrypt_fn`` the same way it already defaults
   ``security_key_fn``. The transferData bytes for one area (SID 0x36
   payloads concatenated in wire order, SID stripped) ARE the .sgo
   flash-delivery container's own "BCB" compressed-record format, XORed
   with the fixed key ``b"MILKYWAY"`` (the SAME literal key the container's
   crypt=1 sections use, not per-file) -- with NO extra whole-file XOR 0xFF
   layer on the wire (that part is unique to the .sgo file at rest).
   Confirmed by fully decoding both written areas of a real capture
   (Reference/Logs/6hp-write9.log) with
   :func:`ovtsuite.flash.crypto.zf6hp_bcb_decode` and diffing against the
   reference calibration
   (Reference/Logs/4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin):
   zero byte differences over the full decoded length of both areas (65408
   bytes @ 0x040000, 196480 bytes @ 0x050000). This was Bear's hypothesis,
   directly verified -- see project memory ``zf6hp-flash-transport``.

   :func:`ovtsuite.flash.crypto.zf6hp_bcb_encode` (wrapped here as
   :func:`default_encrypt_fn`, and used automatically by
   :class:`Zf6hpFlash` when no ``encrypt_fn`` is supplied) builds new
   BCB-compressed+XORed bytes from plaintext, and round-trips through
   ``zf6hp_bcb_decode`` for every plaintext tried so far, including both
   real reference-bin areas AND a full synthetic ``write_areas()`` run
   through a mock ECU (see tests/test_flash_crypto.py and
   tests/test_flash_zf6hp_write_mechanics.py). Two things about it remain
   genuinely open, not settled by that testing: the specific literal/RLE
   split it picks has NOT been shown to be what pcmflash itself would
   produce for the same plaintext -- only that it is A valid encoding under
   the record format as understood from decoding real captures; and the
   format's own terminal checksum record is written as the fixed 0x000000
   both real captures actually contain, NOT the additive-sum formula the
   .sgo unpacker tool documents (which real captures don't match either --
   see ``zf6hp_bcb_decode``'s docstring). What a real TCU firmware decoder
   actually validates on write is therefore still unconfirmed -- pass your
   own ``encrypt_fn`` to override if that turns out to matter for a real
   bench write (the same escape hatch ``security_key_fn`` already offers
   for a hardware variant with a different SA2 tape).

   The per-area plumbing this needed IS done: ``write_areas`` calls
   ``encrypt_fn`` ONCE per area on the area's full plaintext (matching how
   the real cipher compresses+XORs an entire area as one continuous
   stream) and chunks *that stream's output* into 254-byte transferData
   wire envelopes -- it no longer calls ``encrypt_fn`` per 254-byte
   plaintext slice (that never matched the real format: envelope
   boundaries have no relation to the plaintext's own chunk boundaries).

Reading a real TCU needs neither open item: :func:`Zf6hpFlash.read_areas`
only ever unlocks the (solved) read level. Writing now unlocks and
transfers new calibration content with no caller-side setup beyond
supplying the plaintext -- both ``security_key_fn`` and ``encrypt_fn``
default automatically, same pattern as EDC16.
"""
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence

from .crypto import zf6hp_area_checksum, zf6hp_bcb_encode, zf6hp_read_key, zf6hp_write_key
from .tp20_transport import Tp20Channel, Tp20Params
from .transport import KLineError, KLineTimeout

# ---------------------------------------------------------------- addressing
DEST = 0x02                     # TCU logical address (EDC16 uses 0x01)
LISTEN_ID = 0x300               # id we ask the TCU to send to us on (matches capture)

READ_AREAS = (0x04, 0x05, 0x06, 0x07)   # captured read: the "maps" calibration window
WRITE_AREAS = (0x04, 0x05)              # captured write: the only areas actually written
BANK_SIZE = 0x10000                     # 64 KiB per area index

# The plaintext length a real write actually transfers per area -- NOT
# BANK_SIZE. Verified against TWO independent real write captures this
# session (6HP-19A CAN Bus Write.log's ciphertext lengths converted back to
# plaintext, and 6hp-write9.log's BCB-decoded plaintext), both cross-checked
# byte-exact against the reference bin:
#   0x04 -> 65408 (= BANK_SIZE - 128, matching the known "last 128 bytes
#           unused" pattern also seen on read)
#   0x05 -> 196480 (= 3*BANK_SIZE - 128 -- one write-area 0x05 covers what
#           read treats as THREE separate 64 KiB read-areas, 0x05/0x06/0x07,
#           in a single pass; see _AREA_TEMPLATES' erase/verify params, which
#           cover the same 0x05-0x07 span for area 0x05 alone)
WRITE_AREA_LENGTH = {0x04: 65408, 0x05: 196480}

IMAGE_SIZE = 0x100000                   # 1 MiB -- the TCU's full addressable image. Only
                                         # READ_AREAS is ever actually requested from the ECU
                                         # (no capture on file shows the real tool reading
                                         # anything outside 0x04-0x07 either); read_image()
                                         # pads the rest to this size with 0x00, matching the
                                         # other tool's own output exactly -- confirmed by
                                         # inspecting a real "other tool" read (TCU.bin): its
                                         # un-read areas are all 0x00, not 0xFF.

# ---------------------------------------------------------------- KWP SIDs
SID_START_SESSION = 0x10
SID_ECU_RESET = 0x11
SID_READ_ECU_ID = 0x1A
SID_SECURITY = 0x27
SID_READ_MEM = 0x23
SID_START_ROUTINE = 0x31
SID_ROUTINE_RESULTS = 0x33
SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_TRANSFER_EXIT = 0x37
SID_STOP_COMM = 0x82
POS = 0x40                      # positive-response offset (service | 0x40)
NRC_RESPONSE_PENDING = 0x78

SESSION_PROGRAMMING = 0x85      # write path (matches captured "10 85")
SESSION_EXTENDED = 0x86         # read path (matches captured "10 86")

LEVEL_READ = (0x03, 0x04)       # requestSeed/sendKey subfunctions the read path uses
LEVEL_WRITE = (0x01, 0x02)      # requestSeed/sendKey subfunctions the write path uses

ROUTINE_ERASE = 0xC4
ROUTINE_VERIFY = 0xC5

READ_CHUNK = 0xFE                # 254 bytes/request, matches the capture throughout

#: PowerPC instructions that mark an area as CODE rather than another table
#: region. A function essentially always returns through blr, so finding any of
#: these in an area's first bytes is the practical test.
_PPC_MARKERS = {b"\x4e\x80\x00\x20": "blr", b"\x7c\x08\x02\xa6": "mflr",
                b"\x4e\x80\x00\x21": "blrl", b"\x4e\x80\x04\x20": "bctr",
                b"\x7c\x08\x03\xa6": "mtlr", b"\x4c\x00\x00\x64": "rfi"}


def ppc_markers(buf: bytes) -> str:
    """Comma-separated PowerPC instructions found in ``buf``, or "" for none."""
    return ", ".join(sorted({n for p, n in _PPC_MARKERS.items() if p in buf}))


TRANSFER_CHUNK = 254             # transferData payload bytes/frame (msg len 255 incl SID)


# Exact bytes captured for requestDownload (SID 0x34) and startRoutineByLocalId
# 0xC4 (erase), per area -- content-INDEPENDENT (no calibration data feeds into
# either: requestDownload's params are a fixed transfer-format descriptor, and
# erase always wipes the whole 64 KiB bank regardless of what will be written
# into it -- erase_params' end address 0x04FFFF/0x07FFFF covers the WHOLE bank,
# not just WRITE_AREA_LENGTH), so these are used verbatim, same as EDC16's own
# fixed erase signature (Edc16Flash._erase_sig). area 0x04/0x05 only (the only
# ones the capture wrote). Beyond the leading area-index byte and the erase
# address range, these bytes remain UNDECODED -- don't guess further into them.
#
# startRoutineByLocalId 0xC5 (verify / checksum-query) is NOT in this table:
# unlike erase, its params DO depend on calibration content (a checksum of the
# plaintext -- see :func:`_checksum_routine_params` and
# :func:`ovtsuite.flash.crypto.zf6hp_area_checksum`), so they're computed fresh
# per call rather than replayed from one captured calibration.
@dataclass(frozen=True)
class _AreaWriteTemplate:
    download_params: bytes      # after SID 0x34
    erase_params: bytes         # after SID 0x31 (includes the 0xC4 local id)


_AREA_TEMPLATES: Dict[int, "_AreaWriteTemplate"] = {
    0x04: _AreaWriteTemplate(
        download_params=bytes([0x04, 0x00, 0x00, 0x11, 0x00, 0xFF, 0x80]),
        erase_params=bytes([0xC4, 0x04, 0x00, 0x00, 0x04, 0xFF, 0xFF,
                            0x00, 0x01, 0x02, 0x03, 0x04, 0x05]),
    ),
    0x05: _AreaWriteTemplate(
        download_params=bytes([0x05, 0x00, 0x00, 0x11, 0x02, 0xFF, 0x80]),
        erase_params=bytes([0xC4, 0x05, 0x00, 0x00, 0x07, 0xFF, 0xFF,
                            0x00, 0x01, 0x02, 0x03, 0x04, 0x05]),
    ),
}


def _checksum_routine_params(area: int, plaintext: bytes) -> bytes:
    """Build startRoutineByLocalId 0xC5's params fresh for whatever plaintext
    is about to be (or was just) written to ``area``: ``0xC5 + start:3B-BE +
    end:3B-BE(inclusive) + checksum:2B-BE`` where checksum is
    :func:`ovtsuite.flash.crypto.zf6hp_area_checksum` of ``plaintext`` --
    decoded from the real capture bytes, see that function's docstring for
    the evidence. Used BOTH to ask "does the TCU already have this content"
    before erasing (see :func:`Zf6hpFlash._area_already_matches`) and to
    verify what was actually just written afterward -- the same routine
    either way, just called at a different point with a different target
    checksum (the OLD per-area templates hardcoded the post-write checksum
    for the one calibration originally captured, which silently produced a
    false "verify failed" for any other calibration; this replaces that)."""
    start = area * BANK_SIZE
    end = start + len(plaintext) - 1
    checksum = zf6hp_area_checksum(plaintext)
    return bytes([ROUTINE_VERIFY,
                  (start >> 16) & 0xFF, (start >> 8) & 0xFF, start & 0xFF,
                  (end >> 16) & 0xFF, (end >> 8) & 0xFF, end & 0xFF,
                  (checksum >> 8) & 0xFF, checksum & 0xFF])


SecurityKeyFn = Callable[[int, bytes], bytes]
EncryptFn = Callable[[bytes], bytes]


def default_security_key_fn(req_sub: int, seed: bytes) -> bytes:
    """The default ``security_key_fn``: handles both known security levels --
    READ (requestSeed 0x03, see :func:`ovtsuite.flash.crypto.zf6hp_read_key`)
    and WRITE (requestSeed 0x01, see
    :func:`ovtsuite.flash.crypto.zf6hp_write_key`) -- and raises
    :class:`SecurityKeyRequired` for anything else (e.g. a TCU hardware
    variant that turns out to need a different write tape -- see the SA2
    finding in project memory ``zf6hp-flash-transport``). Used automatically
    by :class:`Zf6hpFlash` when no ``security_key_fn`` is supplied; pass your
    own to override for a variant this doesn't cover."""
    if req_sub == LEVEL_READ[0]:
        return zf6hp_read_key(seed)
    if req_sub == LEVEL_WRITE[0]:
        return zf6hp_write_key(seed)
    raise SecurityKeyRequired(
        "zf6hp: no known algorithm for security level 0x%02X yet (only read "
        "0x%02X and write 0x%02X are solved) -- seed was %s; supply your own "
        "security_key_fn if you have it" % (req_sub, LEVEL_READ[0], LEVEL_WRITE[0], seed.hex()))


class SecurityKeyRequired(KLineError):
    """Raised when a security level must be unlocked but no ``security_key_fn``
    was supplied (or it returned nothing usable). Both known levels (read and
    write) are solved and covered by :func:`default_security_key_fn` -- this
    only fires for an unrecognized level, e.g. a TCU hardware variant with a
    different write tape (see the SA2 finding in project memory
    ``zf6hp-flash-transport`` for where the write tape actually came from --
    the ECU's own raw .sgo flash-delivery container, not a binary disassembly
    or a BDM/JTAG dump)."""


def default_encrypt_fn(area_plaintext: bytes) -> bytes:
    """The default ``encrypt_fn``: BCB-encode one area's full plaintext into
    the real transferData wire format (see
    :func:`ovtsuite.flash.crypto.zf6hp_bcb_encode`). Used automatically by
    :class:`Zf6hpFlash` when no ``encrypt_fn`` is supplied -- same pattern
    as :func:`default_security_key_fn`, and matching how EDC16's own
    ``write_flash`` calls its ``encrypt()`` unconditionally (see module
    docstring item 2): a 6HP write cannot go out unencrypted any more than
    an EDC16 one can. Pass your own only to override, e.g. once a bench
    write turns up a firmware detail this encoder's literal/RLE choices or
    checksum-record value don't actually match (see module docstring item 2
    for the specifics still open)."""
    return zf6hp_bcb_encode(area_plaintext)


class WriteDataUnavailable(KLineError):
    """Raised by write_areas if ``encrypt_fn`` somehow ends up ``None`` --
    normally unreachable now that :class:`Zf6hpFlash` defaults it to
    :func:`default_encrypt_fn` the same way it defaults
    ``security_key_fn``; this only fires if a caller constructs
    ``Zf6hpFlash`` some other way that bypasses ``__init__``'s default, or
    sets ``.encrypt_fn = None`` directly afterward."""


class Zf6hpFlash:
    """Read/write session against one ZF 6HP TCU over a CAN channel.

    ``can`` is anything exposing ``write_can(can_id, payload)`` and
    ``read(max_msgs, timeout_ms) -> [msg]`` with ``msg.can_id``/``msg.payload``
    -- :class:`ovtsuite.flash.j2534.Channel`, :class:`ovtsuite.flash.openport.OpenPortChannel`,
    and :class:`tests.mock_tp20.MockTp20Ecu` all qualify, same as
    :mod:`ovtsuite.flash.tp20_transport` requires.
    """

    def __init__(self, can=None, log=None, security_key_fn: Optional[SecurityKeyFn] = None,
                 encrypt_fn: Optional[EncryptFn] = None, tp=None):
        """``can`` is a raw CAN channel (write_can/read/pass_*), from which a
        :class:`Tp20Channel` is built (dest 0x02, listen 0x300 -- see module
        docstring). Alternatively pass ``tp`` -- an already constructed TP 2.0
        channel (e.g. the ``.tp`` of a CanKLine the GUI already opened for this
        ECU address) -- to reuse it instead of opening a second one, the same
        shortcut :class:`ovtsuite.flash.climatronic.Climatronic` offers. When
        reusing a ``tp``, its dest is whatever it was already opened with, not
        necessarily 0x02."""
        self._log = log or (lambda s: None)
        if tp is not None:
            self.tp = tp
        elif can is not None:
            self.tp = Tp20Channel(can, dest=DEST, listen_id=LISTEN_ID,
                                  params=Tp20Params(), log=log)
        else:
            raise KLineError("zf6hp: needs either a CAN channel or a tp channel")
        self.security_key_fn = security_key_fn or default_security_key_fn
        self.encrypt_fn = encrypt_fn or default_encrypt_fn
        self._unlocked_levels = set()
        #: True once the TCU has answered any KWP request (positive or not).
        #: False after a failure means it never answered at all -- the channel
        #: setup failed or the requests went unanswered -- which the GUI's
        #: automatic read takes as "not a TP 2.0 TCU" and tries UDS instead.
        self.answered = False

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
        """Send one KWP payload, return the (non-pending) response payload.
        ``timeout_ms``, when given, overrides the transport's own default
        per-receive wait for JUST this call (see the erase-result poll in
        write_areas() for why: racing a fresh request against one whose
        answer may still be in flight is worse than waiting longer for the
        first one -- 2026-09-21, round 8)."""
        self.tp.send(payload)
        for _ in range(64):
            resp = self.tp.recv(timeout_ms) if timeout_ms is not None else self.tp.recv()
            self.answered = True
            if absorb_pending and len(resp) == 3 and resp[0] == 0x7F and resp[2] == NRC_RESPONSE_PENDING:
                self.log("responsePending for service 0x%02X; waiting" % resp[1])
                continue
            return resp
        raise KLineTimeout("zf6hp: stuck in responsePending")

    @staticmethod
    def _check_positive(resp: bytes, sid: int, context: str):
        if not resp or resp[0] != (sid | POS):
            nrc = resp[2] if len(resp) >= 3 and resp[0] == 0x7F else None
            raise KLineError("zf6hp: %s failed (got %s%s)" % (
                context, resp.hex(" ") if resp else "<empty>",
                ", NRC 0x%02X" % nrc if nrc is not None else ""))

    # ---- security ----
    def unlock(self, level: Sequence[int]):
        """Run one securityAccess seed/key exchange. ``level`` is
        (requestSeedSub, sendKeySub), e.g. LEVEL_READ or LEVEL_WRITE."""
        req_sub, key_sub = level
        if self.security_key_fn is None:
            raise SecurityKeyRequired(
                "zf6hp: no security_key_fn supplied for level (0x%02X, 0x%02X)" % level)
        resp = self._request(bytes([SID_SECURITY, req_sub]))
        self._check_positive(resp, SID_SECURITY, "securityAccess requestSeed 0x%02X" % req_sub)
        seed = resp[2:]
        # Bug fix (2026-09-21, round 3 of the write-path saga): a seed of all
        # zero bytes is the standard KWP2000/ISO 14230 signal that the ECU
        # considers this security level ALREADY unlocked -- the client must
        # not send a key at all. Confirmed directly on real hardware: after
        # this code's own reconnect (see write_areas()) re-requested the
        # WRITE-level seed a few hundred ms after the first exchange had
        # already unlocked it, the TCU replied with seed 00 00 00 00 (raw
        # CAN: id=0x300 "67 01 00 00 00 00"); the previous code computed a
        # key from that zero seed anyway (0x00000000, since the SA2 cipher
        # maps a zero seed to a zero key) and sent it, and the TCU rejected
        # it with NRC 0x12 subFunctionNotSupported on the sendKey subfunction
        # specifically -- i.e. it wasn't expecting a key at all right then.
        # None of the 9 real reference write captures exhibit this (their
        # second exchange always got a genuine fresh nonzero seed), so this
        # is evidently state-dependent (how recently the level was actually
        # unlocked), not something to special-case by write round -- treating
        # a zero seed as "already unlocked" is correct for however/whenever
        # it happens.
        if seed and not any(seed):
            self._unlocked_levels.add(level)
            self.log("zf6hp: security level (0x%02X,0x%02X) already unlocked "
                      "(zero seed) -- no key sent." % level)
            return
        key = self.security_key_fn(req_sub, bytes(seed))
        if not key:
            raise SecurityKeyRequired(
                "zf6hp: security_key_fn returned nothing for level 0x%02X, seed %s"
                % (req_sub, seed.hex()))
        resp = self._request(bytes([SID_SECURITY, key_sub]) + bytes(key))
        self._check_positive(resp, SID_SECURITY, "securityAccess sendKey 0x%02X" % key_sub)
        self._unlocked_levels.add(level)
        self.log("zf6hp: security level (0x%02X,0x%02X) unlocked." % level)

    def _start_session(self, session: int) -> bool:
        """Try startDiagnosticSession; return True on success, False on
        securityAccessDenied (caller should unlock and retry)."""
        resp = self._request(bytes([SID_START_SESSION, session]))
        if resp and resp[0] == (SID_START_SESSION | POS):
            return True
        if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x33:   # securityAccessDenied
            return False
        self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x%02X" % session)
        return True   # unreachable; _check_positive raises on failure

    # ---- read ----
    def read_ecu_identification(self, ident_id: int = 0x9B) -> bytes:
        resp = self._request(bytes([SID_READ_ECU_ID, ident_id]))
        self._check_positive(resp, SID_READ_ECU_ID, "readEcuIdentification 0x%02X" % ident_id)
        return bytes(resp[2:])

    def probe_read_areas(self, areas: Sequence[int] = tuple(range(0x10)),
                         on_stage=None, is_stop=None) -> Dict[int, Optional[bytes]]:
        """Which 64 KiB areas will this TCU let us read? ``{area: first 254 bytes}``,
        or ``None`` for an area it refuses.

        Why this exists. The reference images are calibration ONLY: the 1 MiB
        buffer is sparse, just 0x040000-0x07FFFF carries data, and across the
        whole megabyte there is not one ``blr``, ``mflr``, ``blrl`` or ``bctr``
        at any alignment. A PowerPC function essentially always returns through
        blr, so 256 KiB of populated data with none of them is tables and
        constants, nothing executable. That follows from READ_AREAS being
        (0x04..0x07) -- the captured factory session only ever asked for the
        calibration window (see read_image's docstring: the reference bins are
        "just a calibration read padded out the same way").

        So anything that needs code is unanswerable from these files, including
        how the controller turns a shift-program number into a shift-ladder
        address. But the protocol does not restrict us: readMemoryByAddress is
        ``23 <area> <addr:2 BE> <len:1>`` with the area as an explicit field, so
        asking for other areas is a parameter change, not protocol work.

        This issues exactly ONE 254-byte read per area and nothing else -- no
        requestDownload, no transferData, no erase, no write of any kind. An
        out-of-range area NAKs, which :meth:`read_areas` already treats as an
        ordinary outcome, so a refusal is an answer rather than a failure.
        """
        self.connect()
        try:
            self.read_ecu_identification()
            if LEVEL_READ not in self._unlocked_levels:
                self.unlock(LEVEL_READ)
            resp = self._request(bytes([SID_START_SESSION, SESSION_EXTENDED]))
            self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x86")

            out: Dict[int, Optional[bytes]] = {}
            for area in areas:
                if is_stop and is_stop():
                    raise KLineError("zf6hp: area probe cancelled")
                req = bytes([SID_READ_MEM, area, 0x00, 0x00, READ_CHUNK])
                resp = self._request(req)
                if len(resp) >= 3 and resp[0] == 0x7F:
                    out[area] = None
                    msg = "area 0x%02X (0x%06X): refused, %s" % (area, area * BANK_SIZE,
                                                                 resp.hex(" "))
                else:
                    out[area] = bytes(resp[1:])
                    msg = "area 0x%02X (0x%06X): %d bytes%s" % (
                        area, area * BANK_SIZE, len(resp) - 1,
                        "  CODE: " + ppc_markers(out[area]) if ppc_markers(out[area]) else "")
                self.log("zf6hp: " + msg)
                if on_stage:
                    on_stage(msg)
            return out
        finally:
            self._stop_communication()

    def read_areas(self, areas: Sequence[int] = READ_AREAS,
                   on_percent=None, is_stop=None) -> Dict[int, bytes]:
        """Read each area's full 64 KiB window. Returns {area: 64KiB bytes},
        0xFF-filled where the TCU never had that offset populated (matches the
        captured read: area 0x04's last 128 bytes were never read -- see
        module docstring / project memory)."""
        self.connect()
        try:
            self.read_ecu_identification()
            if (0x03, 0x04) not in self._unlocked_levels:
                self.unlock(LEVEL_READ)
            resp = self._request(bytes([SID_START_SESSION, SESSION_EXTENDED]))
            self._check_positive(resp, SID_START_SESSION, "startDiagnosticSession 0x86")

            out: Dict[int, bytearray] = {a: bytearray(b"\xFF" * BANK_SIZE) for a in areas}
            total_chunks = sum((BANK_SIZE + READ_CHUNK - 1) // READ_CHUNK for _ in areas)
            done = 0
            for area in areas:
                # GUI status fix (2026-09-21): the log()/stage callback never fired
                # again after unlock() during a read -- write_areas() has its own
                # per-stage log() calls (see its stage() helper) but read_areas()
                # previously had none for the ordinary case, only on an out-of-range
                # NAK below, so the GUI's status label looked frozen on "security
                # level unlocked" for the whole read even though on_percent (the
                # progress bar) kept advancing normally. One line per area, not per
                # chunk (a chunk is ~254 bytes, ~258 per area -- logging every one
                # would flood the label far faster than it could be read).
                self.log("zf6hp: reading area 0x%02X..." % area)
                addr = 0
                while addr < BANK_SIZE:
                    if is_stop and is_stop():
                        raise KLineError("zf6hp: read cancelled")
                    length = min(READ_CHUNK, BANK_SIZE - addr)
                    req = bytes([SID_READ_MEM, area, (addr >> 8) & 0xFF, addr & 0xFF, length])
                    resp = self._request(req)
                    if len(resp) >= 3 and resp[0] == 0x7F:
                        # matches the capture: the TCU NAKs a read past its populated
                        # range (securityAccessDenied on an out-of-range probe before
                        # unlock; out-of-range afterwards would NAK too) -- stop this
                        # area rather than guess the rest is 0xFF forever.
                        self.log("zf6hp: area 0x%02X addr 0x%04X NAK'd (%s); stopping area"
                                 % (area, addr, resp.hex(" ")))
                        break
                    self._check_positive(resp, SID_READ_MEM,
                                         "readMemoryByAddress area 0x%02X addr 0x%04X" % (area, addr))
                    data = resp[1:]
                    out[area][addr:addr + len(data)] = data
                    addr += len(data) if data else length
                    done += 1
                    if on_percent:
                        on_percent(min(99, int(100 * done / total_chunks)))
            if on_percent:
                on_percent(100)
            return {a: bytes(buf) for a, buf in out.items()}
        finally:
            self._stop_communication()

    def read_image(self, areas: Sequence[int] = READ_AREAS,
                   on_percent=None, is_stop=None) -> bytes:
        """The areas laid out into a full IMAGE_SIZE (1 MiB) buffer, area*BANK_SIZE
        offset each -- directly comparable in SIZE to the reference binaries,
        which are themselves just a calibration read padded out the same way
        (see module docstring). Bytes outside ``areas`` are 0x00 (matching the
        other tool's own padding, not 0xFF) since they were never requested at
        all; 0xFF is reserved for bytes genuinely attempted-but-unread *within*
        a read area (see read_areas())."""
        banks = self.read_areas(areas, on_percent=on_percent, is_stop=is_stop)
        buf = bytearray(IMAGE_SIZE)   # zero-filled
        for area, data in banks.items():
            buf[area * BANK_SIZE:(area + 1) * BANK_SIZE] = data
        return bytes(buf)

    # ---- write ----
    def write_image(self, image: bytes, areas: Sequence[int] = WRITE_AREAS,
                    on_percent=None, on_stage=None, is_stop=None,
                    force_full: bool = False):
        """Convenience wrapper mirroring read_image(): slice a full IMAGE_SIZE
        (1 MiB) buffer into per-area plaintext using WRITE_AREA_LENGTH (NOT
        BANK_SIZE -- a write area's real length differs from a read area's,
        see WRITE_AREA_LENGTH's docstring) and hand it to write_areas(). Each
        area's slice starts at the same area*BANK_SIZE offset read_image()
        uses, so a full image built by read_image() (or loaded from the same
        1 MiB file layout) slices back out correctly. ``force_full`` is
        passed straight through to write_areas() -- see its docstring for
        what it skips."""
        if len(image) < IMAGE_SIZE:
            raise KLineError("zf6hp: write_image needs a full %d-byte image (got %d)"
                             % (IMAGE_SIZE, len(image)))
        images = {}
        for area in areas:
            length = WRITE_AREA_LENGTH[area]
            base = area * BANK_SIZE
            images[area] = bytes(image[base:base + length])
        self.write_areas(images, on_percent=on_percent, on_stage=on_stage,
                         is_stop=is_stop, force_full=force_full)

    def _area_checksum_matches(self, area: int, plaintext: bytes) -> bool:
        """True if the TCU currently reports its flash content in ``area``'s
        range as already matching a checksum of ``plaintext`` -- the SAME
        0x31/0xC5 routine write_areas() already calls to verify a write it
        just performed (see :func:`_checksum_routine_params` and
        :func:`ovtsuite.flash.crypto.zf6hp_area_checksum` for the request
        format and checksum algorithm, and their docstrings for the
        evidence/open questions). Raises KLineError on any malformed or
        negative KWP exchange, same as every other request this class
        makes -- callers deciding whether to SKIP a write (as opposed to
        confirming one that just happened) should catch that and treat it
        as "can't tell", not as a reason to abort; see write_areas'
        pre-write probe, which does exactly that."""
        params = _checksum_routine_params(area, plaintext)
        resp = self._request(bytes([SID_START_ROUTINE]) + params)
        self._check_positive(resp, SID_START_ROUTINE, "checksum routine area 0x%02X" % area)
        resp = self._request(bytes([SID_ROUTINE_RESULTS, ROUTINE_VERIFY]))
        self._check_positive(resp, SID_ROUTINE_RESULTS, "checksum result area 0x%02X" % area)
        return resp[2:3] == b"\x00"

    def write_areas(self, images: Dict[int, bytes], on_percent=None, on_stage=None,
                    is_stop=None, force_full: bool = False):
        """Write each area in ``images`` (area -> plaintext calibration bytes for
        that area). Only areas with a captured template (WRITE_AREAS: 0x04,
        0x05) are supported -- see module docstring. Requires both
        ``security_key_fn`` and ``encrypt_fn``; raises SecurityKeyRequired /
        WriteDataUnavailable up front (before touching the TCU) if either is
        missing, rather than failing partway through an erase.

        Unless ``force_full``, each area is checked against the TCU's own
        checksum-query routine BEFORE erasing (see _area_checksum_matches)
        and skipped entirely -- no erase, no transfer, no post-write verify
        -- if it already matches; this is a per-AREA skip (only two areas
        exist: 0x04, 0x05), not a per-sector one, because that's the only
        granularity the protocol gives us (see project memory
        zf6hp-flash-transport's GUI WRITE WIRING section for why a finer
        read-back-and-diff approach was rejected as too slow). If the probe
        itself fails for any reason (KLineError -- the mismatch/failure
        response shape is unconfirmed against real hardware, see
        zf6hp_area_checksum's docstring), that's treated as "can't tell" and
        the area is written normally rather than the whole write aborting --
        worst case this behaves exactly like force_full for that area."""
        unknown = set(images) - set(_AREA_TEMPLATES)
        if unknown:
            raise KLineError("zf6hp: no captured write template for area(s) %s (only %s supported)"
                             % ([hex(a) for a in sorted(unknown)],
                                [hex(a) for a in sorted(_AREA_TEMPLATES)]))
        if self.security_key_fn is None:
            raise SecurityKeyRequired("zf6hp: write_areas needs security_key_fn")
        if self.encrypt_fn is None:
            raise WriteDataUnavailable("zf6hp: write_areas needs encrypt_fn for transferData")

        def stage(label):
            if on_stage:
                on_stage(label)
            self.log(label)

        self.connect()
        try:
            stage("Entering reprogramming mode")
            if not self._start_session(SESSION_PROGRAMMING):
                self.unlock(LEVEL_WRITE)
                if not self._start_session(SESSION_PROGRAMMING):
                    raise KLineError("zf6hp: startDiagnosticSession 0x85 refused after unlock")
            stage("Passing Security Checks")
            # Real-world bug (found 2026-09-21 from two failed bench writes'
            # CAN logs). FIRST fix attempt (superseded, see below) added a
            # blind 0.8s delay here on the theory the ECU just needed a
            # settling period -- Bear re-tested (with a fresh power cycle,
            # ruling out leftover state from a prior attempt) and it failed
            # the SAME way: the second "securityAccess 01" still got no
            # answer, just a TP2.0 disconnect.
            #
            # The delay was fixing the wrong thing. Re-reading the RAW CAN
            # frames of a real capture (6HP-19A CAN Bus Write.log), not just
            # the decoded KWP messages, shows what actually happens in that
            # ~0.6-1.5s gap: the ECU sends its OWN TP2.0 disconnect (0xA8)
            # right after "startDiagnosticSession + 85" (independent of
            # anything the tester does), the tester acks it, and then a
            # COMPLETE FRESH TP2.0 channel setup happens (new setup/setup-ack/
            # parameter negotiation) BEFORE the second securityAccess request
            # -- not a wait on the same channel. Entering the programming
            # session evidently makes the ECU drop the transport layer itself
            # (matches `Tp20Channel._reset()`'s own docstring, describing the
            # identical pattern for a different platform: "let the ECU fully
            # tear the old channel down before the new setup"). Our two failed
            # attempts kept sending on the now-dead original channel, which
            # the ECU either can't route or explicitly rejects.
            #
            # Fix: reconnect for real. `Tp20Channel.open()` (what `connect()`
            # already calls for the FIRST channel) does exactly the real
            # capture's disconnect-wait-resetup dance via its own `_reset()`
            # -- reusing it here instead of a blind sleep. Nothing about the
            # key exchange itself was ever wrong: the first exchange in both
            # failed logs got a genuine positive response, confirming the
            # write-level key algorithm is correct on real hardware; this was
            # purely a transport-layer gap.
            #
            # Bug fix (2026-09-21, round 2): a bare self.tp.open() is only
            # correct when self.tp is a local Tp20Channel. Zf6hpFlash is
            # constructed with the RAW channel object (gui_flash.py's
            # _run_zf6hp() does `tp = getattr(link, "tp", None)`), and for
            # real hardware (Bear's OpenPort 2.0) that object is a
            # j2534.BridgeTp20, whose open() tries to fully reconnect the
            # underlying device -- which fails with "Channel in use" since
            # the device connection is already open (confirmed by Bear's
            # real-hardware retest: the CAN log showed write_areas()'s
            # exception-triggered `finally: self._stop_communication()`
            # firing instead of a second security exchange ever being
            # attempted). BridgeTp20 has a separate reopen() that re-runs
            # the TP2.0 channel setup on the EXISTING CAN channel, with no
            # device reconnect -- exactly what's needed here. This mirrors
            # the dispatch already built (and real-hardware-verified for
            # EDC16's CAN write path) in tp20_transport.py's
            # CanKLine.reset_channel(), which edc16.py already relies on.
            reopen = getattr(self.tp, "reopen", None)
            if reopen is not None:
                reopen()                      # bridge: re-run TP2.0 setup on the EXISTING CAN channel
            else:
                self.tp.open()                # local Tp20Channel: open() re-resets + re-negotiates
            self.unlock(LEVEL_WRITE)          # a second, fresh exchange -- matches the capture

            areas = [a for a in WRITE_AREAS if a in images]
            for i, area in enumerate(areas):
                tpl = _AREA_TEMPLATES[area]
                data = images[area]

                if not force_full:
                    stage("Checking area %d" % (i + 1))
                    try:
                        unchanged = self._area_checksum_matches(area, data)
                    except KLineError as ex:
                        self.log("zf6hp: checksum probe for area 0x%02X failed (%s) "
                                 "-- writing normally" % (area, ex))
                        unchanged = False
                    if unchanged:
                        stage("Area %d unchanged (checksum match) - skipped" % (i + 1))
                        continue

                stage("Erasing area %d" % (i + 1))
                resp = self._request(bytes([SID_REQUEST_DOWNLOAD]) + tpl.download_params)
                self._check_positive(resp, SID_REQUEST_DOWNLOAD, "requestDownload area 0x%02X" % area)

                resp = self._request(bytes([SID_START_ROUTINE]) + tpl.erase_params)
                self._check_positive(resp, SID_START_ROUTINE, "start erase routine area 0x%02X" % area)

                # Bug fix (2026-09-21, round 7 of the write-path saga): a real
                # erase can take several seconds -- confirmed on real hardware
                # (Bear's first attempt to actually reach this step): the
                # erase-result poll below wasn't answered until ~3.4s after
                # being sent, well past the transport's ~2s per-request
                # timeout (Tp20Channel/BridgeTp20 default poll_timeout_ms).
                # A single `_request()` here timed out (KLineTimeout, raised
                # by the transport's recv()), which propagated straight out
                # of write_areas() into its own `finally: self._stop_
                # communication()` -- flooding the TCU with stopCommunication
                # retries while it was STILL busy erasing. EDC16's own erase
                # routine (edc16.py's _erase_block()) hits this exact same
                # "erase takes longer than one poll" problem against real
                # EDC16 hardware and solves it by RE-SENDING the results
                # request in a loop against a generous overall deadline
                # (60s) -- round 7 ported that loop SHAPE here.
                #
                # Bug fix (2026-09-21, round 8): round 7's loop used the raw
                # channel's short ~2s default per-attempt wait, so on real
                # hardware it gave up on the FIRST poll (whose real answer
                # was still in flight, arriving ~3.4s in) and fired a SECOND,
                # brand-new "33 C4" request while the TCU was still trying to
                # deliver the first one's answer. TP2.0 has no support for
                # overlapping requests here: Tp20Channel._await_ack() (what
                # the second send() waits on) doesn't consume or ACK an
                # incoming DATA frame while it's itself waiting to send --
                # it just keeps retransmitting the new request against
                # repeated "ACK (not ready)" replies -- so the TCU's real,
                # legitimate answer to the FIRST request got stuck
                # (retransmitted 3x by the TCU, never acknowledged by us)
                # and the TCU disconnected the whole TP2.0 channel a few
                # hundred ms later. That's the "ECU disconnected" Bear
                # reported -- a collision this fix's own retry created, not
                # one that was already there.
                # What EDC16's identical-looking loop doesn't have to worry
                # about: CanKLine (which edc16.py's self.k is) defaults
                # read_timeout to 8.0s -- see CanKLine.__init__ -- so each of
                # ITS attempts already waits long enough to catch a ~3-4s
                # real erase in one try, and a resend is the rare exception
                # there rather than the norm. That 8s precedent didn't carry
                # over when round 7 ported the loop's SHAPE, since
                # Zf6hpFlash talks to the raw channel directly and
                # _request() had no way to ask for a longer wait -- now it
                # does (see _request()'s new timeout_ms param): give each
                # attempt 8s, matching CanKLine's proven default, so the
                # measured ~3.4s real erase time is covered by ONE attempt
                # in the ordinary case and a second request is never racing
                # a first one still in flight.
                deadline = time.monotonic() + 60.0
                resp = b""
                while True:
                    if is_stop and is_stop():
                        raise KLineError("zf6hp: write cancelled")
                    try:
                        resp = self._request(bytes([SID_ROUTINE_RESULTS, ROUTINE_ERASE]), timeout_ms=8000)
                    except KLineTimeout:
                        resp = b""
                    if resp and resp[0] == (SID_ROUTINE_RESULTS | POS) and len(resp) > 1 and resp[1] == ROUTINE_ERASE:
                        break
                    if time.monotonic() >= deadline:
                        raise KLineError("zf6hp: erase area 0x%02X did not complete in 60s" % area)
                    time.sleep(0.1)
                self._check_positive(resp, SID_ROUTINE_RESULTS, "erase result area 0x%02X" % area)
                if resp[2:3] != b"\x00":
                    raise KLineError("zf6hp: erase area 0x%02X reported failure %s"
                                     % (area, resp[2:].hex(" ")))

                stage("Transferring area %d" % (i + 1))
                # encrypt_fn runs ONCE against the area's FULL plaintext -- the real
                # cipher compresses+XORs the whole area as one continuous stream (see
                # module docstring item 2), so wire chunking happens on ITS output,
                # not on 254-byte slices of the plaintext.
                cipher_stream = bytes(self.encrypt_fn(data))
                total = max(1, len(cipher_stream))
                for off in range(0, len(cipher_stream), TRANSFER_CHUNK):
                    if is_stop and is_stop():
                        raise KLineError("zf6hp: write cancelled")
                    chunk = cipher_stream[off:off + TRANSFER_CHUNK]
                    resp = self._request(bytes([SID_TRANSFER_DATA]) + chunk)
                    self._check_positive(resp, SID_TRANSFER_DATA,
                                         "transferData area 0x%02X offset 0x%X" % (area, off))
                    if on_percent:
                        on_percent(min(99, int(100 * (off + len(chunk)) / total)))

                resp = self._request(bytes([SID_TRANSFER_EXIT]))
                self._check_positive(resp, SID_TRANSFER_EXIT, "requestTransferExit area 0x%02X" % area)

                stage("Verification")
                # Same routine as the pre-write probe above, now pointed at the
                # plaintext actually written -- computed fresh (NOT the old fixed
                # per-area template) so this verifies correctly for ANY
                # calibration, not just the one originally captured (a stale
                # hardcoded checksum here would falsely report "verify failed"
                # on every successful write of different content -- see
                # zf6hp_area_checksum's docstring / project memory's GUI WRITE
                # WIRING section for why this was a real, if latent, bug).
                if not self._area_checksum_matches(area, data):
                    raise KLineError("zf6hp: verify area 0x%02X reported checksum mismatch after write"
                                     % area)
            if on_percent:
                on_percent(100)
            stage("Completed successfully")
        finally:
            self._stop_communication()

    def _stop_communication(self):
        try:
            self._request(bytes([SID_STOP_COMM]), absorb_pending=False)
        except Exception:
            pass


__all__ = ["Zf6hpFlash", "SecurityKeyRequired", "WriteDataUnavailable",
          "default_security_key_fn", "default_encrypt_fn",
          "READ_AREAS", "WRITE_AREAS", "BANK_SIZE", "IMAGE_SIZE", "WRITE_AREA_LENGTH",
          "LEVEL_READ", "LEVEL_WRITE",
          "DEST", "LISTEN_ID"]
