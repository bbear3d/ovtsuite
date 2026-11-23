# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VDO instrument cluster (VW Mk4 / B5 era) EEPROM read and write over K-line.

A port of KWHack's working cluster path (its vendored kw1281test
``VdoCluster`` / ``VdoKeyFinder`` / ``KW1281Dialog.ReadEeprom/WriteEeprom``
and ``Tester.UnlockControllerForEepromReadWrite``) onto this app's own
K-line transports (:class:`ovtsuite.flash.transport.KLine` serial/FTDI,
:class:`ovtsuite.flash.kline_j2534.J2534KLine`) and KW1281 block dialog
(:class:`ovtsuite.flash.kwp.Kw1281`).

Session, exactly as KWHack drives it:

1. 5-baud slow init to address **0x17** at 10400 baud (KW1281; the cluster
   has no fast init), receive and ACK the identification blocks.
2. ReadIdent (block 0x00) until no more ident data is flagged.
3. Unlock: custom block 0x1B ``80 01 02 03 04`` (enables custom commands
   0x81-0xAF), ReadSoftwareVersion ``84 00..03`` ("VWK501MH 01.10"), then
   the ROM version's unlock code ``9D <4 bytes>`` (falling back to the
   generic list when the version is not tabled).
4. If the access level (``96 04``) is not 7: seed ``96 01`` -> key
   (VdoKeyFinder) -> ``96 02 <7 bytes>``.
5. EEPROM: ReadEeprom ``19 <count> <hi> <lo>`` / WriteEeprom
   ``1A <count> <hi> <lo> <data>`` in 16-byte blocks; a NAK is a permission
   refusal.
6. After a *write* that read back clean: Reset (custom 0x82), so the cluster
   starts on the EEPROM it was just given instead of running on what it
   loaded at power-up. Otherwise (a read) End communication (block 0x06).

:func:`write_cluster` writes the whole file, as KWHack does, then reads every
block back and compares. Nothing is read from the cluster before the write
(no backup, no comparison with the cluster's own data); the file itself must
be a recognised VDO Mk4 EEPROM layout.

Audi C5-family clusters (4B0920 / 4Z7920 / 8D0920 / 8Z0920 in the ident) NAK
the whole custom-block command set, so steps 3-5 differ for them: see "Audi C5
path" in :class:`VdoCluster` (kw1281test ``AudiC5Cluster``). The path is
chosen from the ident at :meth:`VdoCluster.connect`.

Two speeds (the flasher's Speed list). **Low** is the KWHack dialog above to
the letter: every data block is ACKed and the cluster's own ACK read before the
next command, the extended-command login reads the software version four ways,
and a ReadIdent runs before the unlock. **High** is what a genuine tool's
traffic shows (captured on a VWK503MH, 2026-11-17): the next ``19`` / ``1A``
command *is* the ACK of the previous ``EF`` / ``F9`` reply, so a block costs one
round trip instead of two (a 2 KB read drops from about 100 s to about 50 s),
the version is read once, and there is no ReadIdent. The wire is otherwise the
same, and so is the cluster's own byte pace (about 16 ms a byte), which is what
is left.
"""
from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import kwp as _kwp
from .transport import KLineError, KLineTimeout, LinkStopped

ADDRESS = 0x17               # KW1281 logical address of the instrument cluster
BAUD = 10400
EEPROM_SIZE = 2048
CONNECT_RETRY_QUIET_S = 1.0    # between failed inits (see connect)
BLOCK = 16                   # bytes per ReadEeprom / WriteEeprom block (KWHack)

# KW1281 block titles (kw1281test BlockTitle.cs)
BT_READ_IDENT = 0x00
BT_END = 0x06
BT_ACK = 0x09
BT_NAK = 0x0A
BT_READ_EEPROM = 0x19
BT_WRITE_EEPROM = 0x1A
BT_CUSTOM = 0x1B
BT_READ_EEPROM_RESP = 0xEF
BT_ASCII = 0xF6
BT_WRITE_EEPROM_RESP = 0xF9

#: VDO custom command 0x82 -- reset the cluster (KWHack's ``CustomReset``). It
#: is above 0x80, so :meth:`VdoCluster.custom` sends the extended-command
#: unlock first unless the session has already done so (it has, by the time
#: an EEPROM write is finished).
CMD_RESET = 0x82

MAX_ACCESS_LEVEL = 7


# Audi C5-family cluster dialog (kw1281test AudiC5Cluster)
C5_FAMILY_PREFIXES = ("4B0920", "4Z7920", "8D0920", "8Z0920")
C5_START = 0xD1
C5_ACK, C5_HELLO, C5_LOGIN, C5_READ, C5_WRITE = 0x06, 0x49, 0x53, 0x72, 0x77
C5_CHUNK = 8
KEEPALIVE_S = 1.5              # idle ACK period while the user is asked something
C5_BAUD_SETTLE_S = 2.0         # the cluster's quiet time after the baud switch
_C5_KW_PASSWORDS = ("loginas9", "n7KB2Qat")
_C5_LOGIN_PASSWORDS = ("19xDR8xS", "vdokombi", "w10kombi", "w10serie")


#: Clusters that need the Low speed's pacing (a 25 ms lead before each block
#: and the ACK after each reply): kwp.BLOCK_LEAD was put in for 4D0919035AJ, and
#: High's immediate answers are untested on it. VdoCluster.connect drops a High
#: session to Low when the ident carries one of these.
LOW_SPEED_PARTS = ("4D0919035AJ",)


def needs_low_speed(ident: str) -> bool:
    """True when the ident carries a part number in :data:`LOW_SPEED_PARTS`
    (matched without its first character too: the ident's first byte is often
    corrupted)."""
    s = (ident or "").replace(" ", "")
    return any(p in s or p[1:] in s for p in LOW_SPEED_PARTS)


def is_c5_ident(ident: str) -> bool:
    """True when the ident carries a C5-family part number.

    The ident's first byte is often corrupted, so the prefix is also matched
    without its first character -- but only at the very start of the ident
    and only when that first character is not a digit. A VW part number
    always starts with a digit, so a leading digit is a genuine character of a
    different part (3B0920... is a Passat B5 cluster, 4D0920... an A8 one),
    not a damaged "4B0" / "8D0"."""
    s = (ident or "").replace(" ", "")
    for p in C5_FAMILY_PREFIXES:
        if p in s:
            return True
        if len(s) > 1 and not s[0].isdigit() and s[1:].startswith(p[1:]):
            return True
    return False


class KeepAlive:
    """Sends :meth:`VdoCluster.keep_alive` on a timer from a helper thread.
    The caller must not touch the link until :meth:`stop` has returned (it
    joins the thread, so no exchange is half done). ``lost`` is set, with
    the error in ``error``, when an exchange failed: the session is gone."""

    def __init__(self, cluster: "VdoCluster", interval: float = KEEPALIVE_S):
        self._c = cluster
        self._interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.error: Optional[BaseException] = None
        self.sent = 0
        self._t = threading.Thread(target=self._run, name="vdo-keepalive",
                                   daemon=True)

    @property
    def lost(self) -> bool:
        with self._lock:
            return self.error is not None

    def start(self) -> None:
        self._t.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._c.keep_alive()
                with self._lock:
                    self.sent += 1
            except Exception as ex:          # noqa: BLE001 - line gone
                with self._lock:
                    self.error = ex
                return

    def stop(self) -> None:
        self._stop.set()
        if self._t.is_alive() and self._t is not threading.current_thread():
            self._t.join()


class StopRequested(Exception):
    pass


class ClusterError(KLineError):
    pass


class ClusterTypeMismatch(ClusterError):
    """The file's layout (Mk4 or C5) is not the connected cluster's type, and
    nothing was given to ask the user whether to write anyway."""


class ClusterNoAnswer(ClusterError):
    """The 5-baud init got no answer at all, on every try: nothing is listening
    on the line (wiring, ignition, the wrong adapter). Starting the session
    over would only repeat the same silence, so it is not retried."""


# ---------------------------------------------------------------------------
# VdoKeyFinder (kw1281test, as vendored in KWHack)
# ---------------------------------------------------------------------------
_SECRETS = {
    0x01: [[0xe5, 0x7c, 0x20, 0xb3], [0x67, 0xb8, 0xf0, 0xe2],
           [0x59, 0xd0, 0x4f, 0xcb], [0x46, 0x83, 0xb6, 0x27],
           [0xc9, 0xde, 0xe3, 0xca], [0x7f, 0x50, 0x44, 0xbc],
           [0x4b, 0xd0, 0x7f, 0xad], [0x55, 0x16, 0xa8, 0x94]],
    0x03: [[0x4c, 0x29, 0x92, 0x1b], [0x42, 0x0a, 0x0b, 0x66],
           [0x1c, 0x4c, 0x91, 0x4d], [0xe2, 0xfd, 0xa2, 0x28],
           [0x48, 0x34, 0x58, 0x71], [0xb1, 0xf5, 0xd0, 0xb8],
           [0xac, 0xfc, 0x5e, 0x6c], [0x98, 0xe1, 0x56, 0x5f]],
    0x09: [[0xa7, 0xd2, 0xe9, 0x8d], [0xe6, 0xfa, 0x9e, 0xba],
           [0x63, 0x92, 0xe3, 0x08], [0x55, 0x3e, 0x68, 0x24],
           [0x03, 0x2a, 0x70, 0xdc], [0xe7, 0xb4, 0x71, 0x86],
           [0x4f, 0x58, 0xcd, 0x81], [0xfd, 0x8e, 0x31, 0x96]],
    0x0D: [[0xc9, 0x18, 0xe6, 0x6e], [0x69, 0xc3, 0x08, 0xcd],
           [0x37, 0x15, 0xd3, 0x23], [0xe1, 0xe1, 0xa9, 0x3b],
           [0x19, 0x74, 0x72, 0x18], [0x08, 0x2b, 0x49, 0x1a],
           [0x82, 0xd1, 0x7d, 0x50], [0x0a, 0x5b, 0x41, 0x4f]],
    # Only level 7 is confirmed for this one; 0-6 are placeholders. KWHack's
    # value here was [0x2b, 0x3a, 0x58, 0x41], claimed "brute-forced from an
    # Audi C5 cluster, verified on three more pairs" -- but it never actually
    # raised a real C5 cluster's access level (own_initiative 2026-11-15: see
    # VdoCluster's C5 path). The last byte below (0x71, was 0x41) is corrected
    # from 5 real (seed, key) pairs sniffed from a genuine tool unlocking a
    # real 4B0920981P cluster, each verified end to end by a full matching
    # EEPROM read. Whether 0x41 was a transcription slip or a different
    # secret for some other ROM/unit that also emits this seed suffix is open
    # -- the table is keyed only by the seed suffix, not by ROM version, so
    # that can't be ruled out. 0x71 is what this project's own hardware needs.
    0x06: [[0, 0, 0, 0]] * 7 + [[0x2b, 0x3a, 0x58, 0x71]],
    0x0B: [[0x47, 0x36, 0x9a, 0xbb], [0xad, 0x4e, 0x61, 0x44],
           [0xd3, 0xd6, 0x42, 0x59], [0x13, 0x6f, 0x43, 0x74],
           [0xfc, 0xb8, 0x59, 0x2e], [0x09, 0x58, 0x9d, 0x7f],
           [0x24, 0x27, 0xc3, 0x9d], [0x87, 0xed, 0x34, 0x63]],
}


def _sbc(minuend: int, subtrahend: int, carry: bool) -> Tuple[int, bool]:
    """6502 SBC: minuend - subtrahend - (not carry)."""
    r = minuend - subtrahend - (0 if carry else 1)
    return r & 0xFF, r >= 0


def _ror(v: int, carry: bool) -> Tuple[int, bool]:
    return ((v >> 1) | (0x80 if carry else 0)) & 0xFF, bool(v & 1)


def _rol(v: int, carry: bool) -> Tuple[int, bool]:
    return ((v << 1) | (1 if carry else 0)) & 0xFF, bool(v & 0x80)


def _scramble(w: List[int]) -> None:
    w[4] = w[0]
    w[0] = w[1]
    w[1] = w[3]
    w[3] = w[2]
    w[2] = w[4]


def _calculate_key(seed4: Sequence[int], secret: Sequence[int]) -> List[int]:
    work = [seed4[0], seed4[1], seed4[2], seed4[3], 0, 0]
    sec = list(secret)
    _scramble(work)
    y = work[0] & 0x07
    temp = y + 1
    a = 0x01
    for _ in range(y):                      # LeftRotate(0x01, y), no carry in
        a, _c = _rol(a, bool(a & 0x80))
    while True:
        set_ = ((sec[0] ^ sec[1] ^ sec[2] ^ sec[3]) & 0x40) != 0
        sec[3] = (sec[3] | a) if set_ else (sec[3] & (a ^ 0xFF))
        carry = bool(sec[0] & 0x01)         # RightRotateFirst4Bytes(buf, 1)
        sec[3], carry = _ror(sec[3], carry)
        sec[2], carry = _ror(sec[2], carry)
        sec[1], carry = _ror(sec[1], carry)
        sec[0], _c = _ror(sec[0], carry)
        temp -= 1
        if temp == 0:
            break
    for x in range(2):
        work[4] = work[0]
        work[0] ^= work[2]
        work[5] = work[1]
        work[1] ^= work[3]
        work[3] = work[5]
        work[2] = work[4]
        n = work[2] & 0x07                  # LeftRotateFirstTwoBytes
        while n > 0:
            carry = bool(work[1] & 0x80)
            work[0], carry = _rol(work[0], carry)
            work[1], _c = _rol(work[1], carry)
            n -= 1
        yy = x << 1
        work[0], carry = _sbc(work[0], sec[yy], True)
        work[1], _c = _sbc(work[1], sec[yy + 1], carry)
    _scramble(work)
    return work[:4]


def find_key(seed: bytes, access_level: int = MAX_ACCESS_LEVEL,
             log: Optional[Callable[[str], None]] = None) -> bytes:
    """10-byte seed block -> the 7-byte key block for ``96 02``."""
    seed = bytes(seed)
    if len(seed) != 10:
        raise ClusterError("unexpected seed length %d (expected 10)" % len(seed))
    table = _SECRETS.get(seed[8]) if seed[9] == 0x00 else None
    if table is None:
        if log:
            log("Unexpected seed suffix $%02X $%02X; trying the $01 secrets"
                % (seed[8], seed[9]))
        table = _SECRETS[0x01]
    key = _calculate_key([seed[1], seed[3], seed[5], seed[7]], table[access_level])
    return bytes([access_level, key[0], key[1], 0x00, key[2], 0x00, key[3]])


# ---------------------------------------------------------------------------
# Unlock codes (kw1281test VdoCluster.GetClusterUnlockCodes, KWHack copy)
# ---------------------------------------------------------------------------
#: ROM version -> unlock code (kw1281test VdoCluster.GetClusterUnlockCodes,
#: KWHack's copy; generated from that C# source, not retyped).
_UNLOCK_BY_VERSION = {
    'VT5P07MH 09.00': [0x00, 0x07, 0x43, 0x35],
    'VAT500LL 01.00': [0x01, 0x04, 0x3D, 0x35],
    'VAT500LL 01.20': [0x01, 0x04, 0x3D, 0x35],
    'VAT500MH 01.10': [0x01, 0x04, 0x3D, 0x35],
    'VAT500MH 01.20': [0x01, 0x04, 0x3D, 0x35],
    '$01 $00 $14 $01': [0x01, 0x08, 0x05, 0x02],
    'V798MLA 01.00': [0x02, 0x03, 0x05, 0x09],
    '$00 $00 $13 $01': [0x09, 0x06, 0x05, 0x02],
    'VSQX01LM 01.00': [0x31, 0x39, 0x34, 0x46],
    'VCLM09MH $00 $09': [0x32, 0x31, 0x36, 0x31],
    'VCB07LL  09.00': [0x33, 0x34, 0x46, 0x4A],
    'VKQ501HH 09.00': [0x34, 0x3F, 0x43, 0x39],
    'VQMJ07HH 08.40': [0x34, 0x3F, 0x43, 0x39],
    'VQMJ07LM 08.40': [0x34, 0x3F, 0x43, 0x39],
    'VQMJ07LM 09.00': [0x34, 0x3F, 0x43, 0x39],
    'VQMJ06LM 09.00': [0x35, 0x3D, 0x47, 0x3E],
    'SS5501LM 00.80': [0x36, 0x3B, 0x36, 0x3D],
    'SS5501ML 00.80': [0x36, 0x3B, 0x36, 0x3D],
    'VWK501LL 00.88': [0x36, 0x3D, 0x3E, 0x47],
    'VWK501MH 00.88': [0x36, 0x3D, 0x3E, 0x47],
    'VWK501LL 01.00': [0x36, 0x3D, 0x3E, 0x47],
    'VWK501MH 01.00': [0x36, 0x3D, 0x3E, 0x47],
    'VT5X02LL 09.40': [0x36, 0x3F, 0x45, 0x42],
    'VQMJ09HH 05.10': [0x37, 0x42, 0x47, 0x43],
    'VWK502MH 09.00': [0x38, 0x37, 0x3E, 0x31],
    'VT5X02LL 09.00': [0x38, 0x39, 0x3A, 0x47],
    'S599CAA  01.00': [0x38, 0x3F, 0x40, 0x35],
    'V599HLA  00.91': [0x38, 0x3F, 0x40, 0x35],
    'V599HLA  01.00': [0x38, 0x3F, 0x40, 0x35],
    'V599LLA  00.91': [0x38, 0x3F, 0x40, 0x35],
    'V599LLA  01.00': [0x38, 0x3F, 0x40, 0x35],
    'V599LLA  03.00': [0x38, 0x3F, 0x40, 0x35],
    'V599MLA  01.00': [0x38, 0x3F, 0x40, 0x35],
    'V599MLA  03.00': [0x38, 0x3F, 0x40, 0x35],
    'MPV300LL 04.00': [0x38, 0x47, 0x34, 0x3A],
    'MPV501MH 01.00': [0x38, 0x47, 0x34, 0x3A],
    'VWK501MH 00.92': [0x39, 0x34, 0x34, 0x40],
    'VWK501MH 01.10': [0x39, 0x34, 0x34, 0x40],
    'VBK700LL 00.96': [0x3A, 0x39, 0x31, 0x43],
    'VBK700LL 01.00': [0x3A, 0x39, 0x31, 0x43],
    'VBKX00MH 01.00': [0x3A, 0x39, 0x31, 0x43],
    'MPV300LL 02.00': [0x3B, 0x47, 0x03, 0x02],
    'SS5501LM 01.00': [0x3C, 0x34, 0x47, 0x35],
    'SS5501ML 01.00': [0x3C, 0x34, 0x47, 0x35],
    'VSQX01LM 01.20': [0x3D, 0x36, 0x40, 0x36],
    'S599CAA  00.80': [0x3D, 0x39, 0x3B, 0x35],
    'KB5M07HH 09.00': [0x3E, 0x35, 0x3D, 0x3A],
    'VWK503LL 09.00': [0x3E, 0x35, 0x3D, 0x3A],
    'VWK503MH 09.00': [0x3E, 0x35, 0x3D, 0x3A],
    'VMMJ08MH 09.00': [0x3E, 0x47, 0x3D, 0x48],
    'MPV300LL 00.90': [0x3F, 0x38, 0x43, 0x38],
    'MPV500LL 00.90': [0x3F, 0x38, 0x43, 0x38],
    'SS5500LM 01.00': [0x40, 0x39, 0x39, 0x38],
    'VSQX01LM 01.10': [0x43, 0x43, 0x3D, 0x37],
    'MPV300LL 03.00': [0x43, 0x43, 0x43, 0x39],
    'KPQMLA` $01': [0x47, 0x3B, 0x31, 0x3F],
    'K5MJ07HH 09.00': [0x47, 0x3F, 0x39, 0x44],
    'K5MJ07LM 08.10': [0x47, 0x3F, 0x39, 0x44],
    'K5MJ07LM 09.00': [0x47, 0x3F, 0x39, 0x44],
}

#: Tried in order when the cluster's version is not tabled above
#: (kw1281test VdoCluster.ClusterUnlockCodes, generated from source).
_GENERIC_UNLOCK = [bytes.fromhex(h) for h in (
    "00000000 00000302 00010302 00020302 00020907 00030302 00030402 00040302 "
    "00040607 00050302 00060302 00070204 00070308 00074335 00080204 01000302 "
    "01000905 01010004 01010005 01010006 01010007 01010008 01010009 01010100 "
    "01010101 01010102 01010103 01010104 01010105 01010106 01010302 01010307 "
    "01010508 01010709 01020302 01030302 01040202 01040302 01043D35 01050302 "
    "01050608 01053D35 01060002 01060200 01060302 01060402 01070003 01080205 "
    "01080300 01080502 02000302 02000601 02010302 02020302 02020401 02020902 "
    "02030302 02030509 02040002 02040302 02050002 02050302 02050609 02050801 "
    "02060302 02060609 02090206 02090402 02090403 02323B37 03000302 03000307 "
    "03000701 03010302 03020302 03020502 03030302 03030804 03030903 03040302 "
    "03050302 03060302 03080205 04000302 04010302 04010308 04020302 04020606 "
    "04030302 04040302 04040904 04050302 04050502 04060302 04070007 05000302 "
    "05010302 05010408 05020302 05020309 05020902 05030302 05040302 05050302 "
    "05050809 05050905 05060302 05080502 06000202 06000300 06000302 06010302 "
    "06020302 06030302 06040302 06040701 06050302 06060302 06060906 06090102 "
    "06090309 06090503 07000302 07000604 07010302 07020302 07030302 07030503 "
    "07040302 07050302 07060302 07070904 07070907 08000302 08010302 08010605 "
    "08020104 08020302 08020305 08030302 08040202 08040302 08050302 08060302 "
    "08060706 08080908 09000302 09010107 09010302 09020302 09020606 09030302 "
    "09030906 09040302 09050203 09050302 09050508 09060302 09060409 09060502 "
    "09090302 09090909 31393446 31443543 32313631 32373E31 3334464A 343F4339 "
    "353B393D 353C313C 353D0401 353D473E 35403F38 35433138 3547343C 363B363D "
    "363D3E47 363F4542 3640363D 37393C47 373B3202 373D4343 37424743 38343437 "
    "38373E31 38393940 38393A47 383F4035 3843383F 3847343A 39343440 39434343 "
    "3A313136 3A344738 3A393143 3A394143 3A3B353C 3A3B354C 3A3D353E 3B333E37 "
    "3B3A373E 3B462310 3B46231B 3B46231D 3B470302 3C313C35 3C344735 3D364036 "
    "3D393B35 3E353D3A 3E354330 3E354339 3E354340 3E354341 3E354342 3E354343 "
    "3E354344 3E393143 3E393540 3E394334 3E3F4035 3E473D48 3F313B47 3F384338 "
    "3F43353E 40303E39 40343439 40393938 4043353E 4143353E 4243353E 42453F36 "
    "4331393A 4343353E 43433D37 43434339 4345313D 4443353E 45393443 473A3938 "
    "473B313F 473C3937 473E3D36 473F3944 "
).split()]


def unlock_codes(version: Optional[str]) -> List[bytes]:
    code = _UNLOCK_BY_VERSION.get(version or "")
    return [bytes(code)] if code is not None else list(_GENERIC_UNLOCK)


def software_version_string(body: bytes) -> str:
    """kw1281test SoftwareVersionToString: 'VWK501MH 01.10' from the 10-byte
    variation-0 reply (ASCII, then minor, major); mixed dump otherwise."""
    body = bytes(body)
    if 9 <= len(body) <= 10:
        return "%s %02X.%02X" % (body[:-2].decode("latin-1"), body[-1], body[-2])
    return " ".join(chr(b) if 0x20 <= b < 0x7F else "$%02X" % b for b in body)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------
@dataclass
class ClusterInfo:
    ident: str = ""
    ident_blocks: List[bytes] = field(default_factory=list)
    software_version: str = ""
    version_blocks: Dict[int, bytes] = field(default_factory=dict)
    unlocked: bool = False
    unlock_code: Optional[bytes] = None
    access_level: Optional[int] = None

    def meta(self) -> dict:
        m = {"ident": self.ident, "read_at": datetime.datetime.now()
             .isoformat(timespec="seconds"), "source": "cluster read"}
        if self.software_version:
            m["rom_version"] = self.software_version
            m["rom_source"] = "cluster"
        if self.version_blocks:
            m["sw_blocks"] = {str(k): v.hex() for k, v in
                              sorted(self.version_blocks.items())}
        return m


class VdoCluster:
    """One KW1281 session with a VDO cluster. Drive from one worker thread."""

    def __init__(self, link, log: Optional[Callable[[str], None]] = None,
                 is_stop: Optional[Callable[[], bool]] = None,
                 fast: bool = False):
        self.k = link
        #: High speed: one round trip per EEPROM block, a shorter login (see
        #: the module docstring). Low is the KWHack dialog as it was.
        self.fast = bool(fast)
        self._log = log or (lambda s: None)
        self._is_stop = is_stop or (lambda: False)
        self.kw: Optional[_kwp.Kw1281] = None
        self.info = ClusterInfo()
        self._custom_unlocked = False
        self.c5 = False              # Audi C5-family dialog (chosen at connect)

    def log(self, msg: str) -> None:
        self._log(msg)

    def _check_stop(self):
        if self._is_stop():
            raise StopRequested("stopped")

    # -- blocks ------------------------------------------------------------
    def _send(self, payload) -> List[bytes]:
        self.kw.send_block(list(payload))
        return self.kw._receive_blocks()

    def _send_one(self, payload) -> List[bytes]:
        """Send a command and read its one answer, without ACKing it: the next
        command sent is the ACK (High speed's EEPROM blocks). A cluster ACK or
        NAK comes back as the answer, as it does from :meth:`_send`."""
        self.kw.send_block(list(payload))
        return [self.kw.receive_block()]

    @staticmethod
    def _data_blocks(blocks: List[bytes]) -> List[bytes]:
        return [b for b in blocks if b[2] not in (BT_ACK, BT_NAK)]

    def custom(self, payload) -> List[bytes]:
        payload = list(payload)
        if payload and payload[0] > 0x80 and not self._custom_unlocked:
            self.log("Unlocking additional custom commands")
            self._send([BT_CUSTOM, 0x80, 0x01, 0x02, 0x03, 0x04])
            self._custom_unlocked = True
        return self._send([BT_CUSTOM] + payload)

    # -- connect -------------------------------------------------------------
    def connect(self, tries: int = 3) -> ClusterInfo:
        last = None
        woke = False
        for attempt in range(1, tries + 1):
            self._check_stop()
            try:
                self.k.set_baud(BAUD)
                self.log("Connecting to the cluster (KW1281 slow init, 0x17 @ %d)..."
                         % BAUD)
                version = self.k.wake_up(ADDRESS, even_parity=False)
                woke = True
                if version >= 2000:
                    raise ClusterError("the controller at 0x17 answered with "
                                       "KWP2000 keyword %d, not KW1281 -- not a "
                                       "VDO Mk4-era cluster" % version)
                # High speed answers each byte at once on a plain serial link
                # (a genuine tool's pace: 0.37 s a block, against 0.43 s with
                # R6 at 1 ms); a link that hands bytes over in its own time
                # (J2534's rx_settle_s) keeps the R6_FAST rule.
                r6 = 0.0 if self.fast and not getattr(
                    self.k, "rx_settle_s", 0.0) else None
                self.kw = _kwp.Kw1281(self.k, self.log, fast=self.fast, r6=r6)
                blocks = self.kw.connect()
                self.info.ident_blocks = list(blocks)
                # the ident is one string cut into fixed-size blocks
                self.info.ident = " ".join("".join(
                    b[3:].decode("latin-1") for b in blocks
                    if b[2] == BT_ASCII).split())
                self.log("Connected: %s" % self.info.ident)
                self.c5 = is_c5_ident(self.info.ident)
                if self.fast and needs_low_speed(self.info.ident):
                    self.fast = False
                    self.kw.fast = False             # its pacing: lead and R6 back
                    self.kw._r6_fixed = None
                    self.log("This cluster needs the Low speed's pacing: "
                             "switching to Low")
                if self.c5:
                    self.log("Audi C5-family cluster: using its own login "
                             "and EEPROM dialog")
                return self.info
            except (KLineError, KLineTimeout) as ex:
                last = ex
                self.log("Connect attempt %d failed: %s" % (attempt, ex))
                try:
                    self.k.clear_rx()
                except Exception:
                    pass
                # No quiet time is needed: on the bench the cluster answered an init
                # 0-45 s after an abandoned session, 8 in a row 1 s apart, and right
                # after EDC16 fast and 5-baud inits (the EDC16 is the unit that needs
                # a quiet line, see kline_j2534.FAST_TO_SLOW_QUIET_S)
                if attempt < tries:
                    time.sleep(CONNECT_RETRY_QUIET_S)
        if not woke:
            raise ClusterNoAnswer(
                "The cluster did not answer the 5-baud init (%s). Check the K-line "
                "wiring and that the ignition is on." % (last or "no reply"))
        raise last or ClusterError("the cluster did not answer")

    # -- keep-alive ----------------------------------------------------------
    def keep_alive(self) -> None:
        """One idle ACK block, answered by the cluster, so it does not drop
        the session while the user decides something. Raises on a dead line."""
        if self.c5:
            self._c5_exchange([C5_ACK])
            return
        self.kw.send_block([BT_ACK])
        self.kw.receive_block()

    def start_keep_alive(self, interval: float = KEEPALIVE_S) -> "KeepAlive":
        ka = KeepAlive(self, interval)
        ka.start()
        return ka

    def read_ident(self) -> List[str]:
        """ReadIdent until the cluster stops flagging more data (KWHack does
        this before every unlock)."""
        out = []
        for _ in range(8):
            blocks = self._send([BT_READ_IDENT])
            asc = [b for b in blocks if b[2] == BT_ASCII]
            out.extend(b[3:].decode("latin-1").strip() for b in asc)
            if not any(len(b) > 3 and b[3] > 0x7F for b in asc):
                break
        return out

    # -- unlock --------------------------------------------------------------
    def read_software_version(self) -> str:
        self.info.version_blocks = {}
        # Only variation 0 is the version string. KWHack also reads 1..3 and
        # keeps them in the container's metadata; nothing uses them, and a
        # genuine tool does not ask. High speed asks once.
        for variation in ((0,) if self.fast else range(4)):
            for b in self._data_blocks(self.custom([0x84, variation])):
                self.info.version_blocks[variation] = bytes(b[3:])
        body = self.info.version_blocks.get(0)
        self.info.software_version = (software_version_string(body)
                                      if body is not None else "")
        self.log("Cluster software version: %s"
                 % (self.info.software_version or "(not returned)"))
        return self.info.software_version

    def unlock(self) -> bool:
        version = self.read_software_version()
        if not version:
            return False
        codes = unlock_codes(version)
        if len(codes) > 1:
            self.log("No unlock code tabled for %s; trying the %d known codes"
                     % (version, len(codes)))
        for code in codes:
            self._check_stop()
            resp = self.custom([0x9D] + list(code))
            if len(resp) != 1:
                raise ClusterError("unexpected %d-block reply to the unlock request"
                                   % len(resp))
            if resp[0][2] == BT_ACK:
                self.info.unlock_code = bytes(code)
                self.log("Unlock code for %s accepted: %s" % (version, code.hex(" ")))
                return True
            if resp[0][2] != BT_NAK:
                raise ClusterError("non-ACK/NAK 0x%02X to the unlock request"
                                   % resp[0][2])
        return False

    def access_level(self) -> Optional[int]:
        data = self._data_blocks(self.custom([0x96, 0x04]))
        if len(data) == 1 and len(data[0]) > 3 and data[0][2] == BT_CUSTOM:
            self.info.access_level = data[0][3]
            self.log("Access level %d" % self.info.access_level)
            return self.info.access_level
        self.log("Access level unknown")
        return None

    def seed_key(self) -> None:
        data = self._data_blocks(self.custom([0x96, 0x01]))
        if len(data) != 1 or data[0][2] != BT_CUSTOM:
            raise ClusterError("the cluster did not return a seed")
        seed = bytes(data[0][3:])
        key = find_key(seed, MAX_ACCESS_LEVEL, self.log)
        self.log("Seed %s -> key %s" % (seed.hex(" "), key.hex(" ")))
        self.custom([0x96, 0x02] + list(key))

    def unlock_for_eeprom(self) -> None:
        """Tester.UnlockControllerForEepromReadWrite for the cluster."""
        if self.c5:
            self._c5_unlock()
            return
        if not self.fast:                  # a genuine tool goes straight to the unlock
            self.read_ident()
        ok = self.unlock()
        if not ok:
            self.log("Unknown cluster software version: EEPROM access will "
                     "likely fail.")
        if self.access_level() == MAX_ACCESS_LEVEL:
            self.info.unlocked = True
            self.log("Cluster unlocked for EEPROM access (seed/key not needed)")
            return
        self.seed_key()
        if self.access_level() != MAX_ACCESS_LEVEL:
            raise ClusterError("the cluster refused the seed/key login "
                               "(software %s)" % (self.info.software_version or "?"))
        self.info.unlocked = True
        self.log("Cluster unlocked for EEPROM access")

    # -- Audi C5 path ------------------------------------------------------------
    # KW1281 custom-block login at 10400 baud (the cluster NAKs the Mk4 custom
    # commands), then 19200 baud even parity and D1-framed blocks:
    # ``D1 <len> <title> <body> <xor>``, no counter, no complement handshake.
    def _c5_unlock(self) -> None:
        self.kw.send_block([BT_CUSTOM, 0x80, 0x01, 0x02, 0x03, 0x04])
        blk = self.kw.receive_block()
        if blk[2] != BT_ACK:
            self.log("Unlock-additional-commands answered 0x%02X (a NAK is "
                     "normal on this cluster)" % blk[2])
        ok = False
        for pw in _C5_KW_PASSWORDS:
            self._check_stop()
            self.log("Sending the custom login block")
            self.kw.send_block([BT_CUSTOM, 0x80] + list(pw.encode()))
            blk = self.kw.receive_block()
            if blk[2] == BT_NAK:
                continue
            if blk[2] != BT_ACK:
                if ok:
                    # one password already logged in: an odd answer to the
                    # other one must not undo that
                    self.log("Custom login: block 0x%02X to the other "
                             "password after one was accepted; ignored"
                             % blk[2])
                    continue
                raise ClusterError("unexpected block 0x%02X to the custom login"
                                   % blk[2])
            ok = True
        if not ok:
            raise ClusterError("the cluster refused both custom logins")
        self.log("Switching to 19200 baud, even parity")
        self.k.set_baud(19200)
        self._set_parity("E")
        self.k.clear_rx()
        self._sleep_stoppable(C5_BAUD_SETTLE_S)
        blk = self._c5_exchange([C5_HELLO])
        if blk[2] != C5_HELLO:
            self.log("Warning: hello answered 0x%02X" % blk[2])
        for pw in _C5_LOGIN_PASSWORDS:
            self._check_stop()
            self.log("Sending the login request")
            blk = self._c5_exchange([C5_LOGIN, 0x9D] + list(pw.encode()))
            if blk[2] == C5_ACK:
                break
        else:
            raise ClusterError("the cluster refused every login password")
        level = self._c5_access_level()
        if level is not None and level != MAX_ACCESS_LEVEL:
            blk = self._c5_exchange([C5_LOGIN, 0x96, 0x01])
            if blk[2] == C5_LOGIN and len(blk) == 14:
                seed = bytes(blk[3:13])
                key = find_key(seed, MAX_ACCESS_LEVEL, self.log)
                self.log("Seed %s -> key %s" % (seed.hex(" "), key.hex(" ")))
                blk = self._c5_exchange([C5_LOGIN, 0x96, 0x02] + list(key))
                if blk[2] != C5_ACK:
                    self.log("Warning: the key was not accepted")
                self._c5_access_level()
            else:
                self.log("Warning: unexpected reply to the seed request")
        self.info.unlocked = True

    def _sleep_stoppable(self, seconds: float, slice_s: float = 0.1) -> None:
        """Sleep, but notice a Stop request within ``slice_s``."""
        end = time.monotonic() + seconds
        while True:
            self._check_stop()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(slice_s, left))

    def _set_parity(self, p: str) -> None:
        set_parity = getattr(self.k, "set_parity", None)
        if set_parity is not None:
            set_parity(p)                    # J2534KLine / J2534RawKLine
            return
        port = getattr(self.k, "port", None)
        if port is None or not hasattr(port, "parity"):
            raise ClusterError("this K-line interface cannot switch to even "
                               "parity, which Audi C5 clusters need after "
                               "their login")
        port.parity = p

    def _c5_send(self, body) -> None:
        """Write one D1 block without reading the cluster's answer."""
        frame = [C5_START, len(body) + 3] + list(body)
        ck = 0
        for b in frame:
            ck ^= b
        self.k.write_bytes(bytes(frame + [ck]))

    def _c5_exchange(self, body) -> List[int]:
        self._c5_send(body)
        hdr = self.k.read_byte()
        size = self.k.read_byte()
        blk = [hdr, size] + [self.k.read_byte() for _ in range(max(0, size - 2))]
        ck = 0
        for b in blk:
            ck ^= b
        if hdr != C5_START or ck:
            raise ClusterError("bad block from the cluster: %s"
                               % bytes(blk).hex(" "))
        return blk

    def _c5_access_level(self) -> Optional[int]:
        blk = self._c5_exchange([C5_LOGIN, 0x96, 0x04])
        if blk[2] == C5_LOGIN and len(blk) > 3:
            self.info.access_level = blk[3]
            self.log("Access level %d" % blk[3])
            return blk[3]
        self.log("Access level unknown")
        return None

    def _c5_read(self, addr: int, count: int) -> Optional[bytes]:
        out = bytearray()
        for a in range(addr, addr + count, C5_CHUNK):
            n = min(C5_CHUNK, addr + count - a)
            blk = self._c5_exchange([C5_READ, n, (a >> 8) & 0xFF, a & 0xFF])
            if blk[2] != C5_READ or len(blk) - 4 != n:
                return None
            out += bytes(blk[3:-1])
        return bytes(out)

    def _c5_write(self, addr: int, values: bytes) -> bool:
        for o in range(0, len(values), C5_CHUNK):
            a = addr + o
            part = values[o:o + C5_CHUNK]
            blk = self._c5_exchange([C5_WRITE, len(part), (a >> 8) & 0xFF,
                                     a & 0xFF] + list(part))
            if blk[2] != C5_ACK:
                self.log("WriteEeprom @0x%03X refused" % a)
                return False
        return True

    # -- EEPROM ----------------------------------------------------------------
    def read_eeprom(self, addr: int, count: int) -> Optional[bytes]:
        if self.c5:
            return self._c5_read(addr, count)
        send = self._send_one if self.fast else self._send
        blocks = send([BT_READ_EEPROM, count & 0xFF, (addr >> 8) & 0xFF,
                       addr & 0xFF])
        if len(blocks) == 1 and blocks[0][2] == BT_NAK:
            return None                         # permission refused
        data = self._data_blocks(blocks)
        if len(data) != 1:
            raise ClusterError("ReadEeprom returned %d blocks instead of 1"
                               % len(data))
        if data[0][2] != BT_READ_EEPROM_RESP:
            # a stray block (a repeat of an earlier answer, an ident block)
            # must never be taken for EEPROM data; _read_block retries it
            raise ClusterError("ReadEeprom @0x%03X: block 0x%02X is not the "
                               "read response" % (addr, data[0][2]))
        body = bytes(data[0][3:])
        if len(body) != count:
            raise ClusterError("ReadEeprom @0x%03X: %d bytes back, asked for %d"
                               % (addr, len(body), count))
        return body

    def write_eeprom(self, addr: int, values: bytes) -> bool:
        values = bytes(values)
        if self.c5:
            return self._c5_write(addr, values)
        send = [BT_WRITE_EEPROM, len(values), (addr >> 8) & 0xFF,
                addr & 0xFF] + list(values)
        blocks = (self._send_one if self.fast else self._send)(send)
        if len(blocks) == 1 and blocks[0][2] == BT_NAK:
            self.log("WriteEeprom @0x%03X refused" % addr)
            return False
        data = self._data_blocks(blocks)
        if len(data) != 1 or data[0][2] != BT_WRITE_EEPROM_RESP:
            self.log("WriteEeprom @0x%03X: unexpected reply" % addr)
            return False
        if bytes(data[0][3:]) != bytes(send[1:5]):
            self.log("WriteEeprom @0x%03X: reply does not echo the request" % addr)
            return False
        return True

    #: how many times a block is read again, each time after re-opening the session, when
    #: the K-line garbles it (a stray byte, a repeated block, a timeout)
    READ_RETRIES = 3

    def _reopen(self) -> None:
        """Start the session again after a garbled exchange: the cluster is
        still in its old one until it times out or is ended, so it is ended
        quietly, the line left alone for a moment, then the whole 5-baud init,
        login and unlock run again."""
        try:
            self.k.clear_rx()
        except Exception:
            pass
        self.end()
        time.sleep(2.0)
        self.kw = None
        self._custom_unlocked = False
        # connect and unlock, starting over a bounded number of times when the
        # unlock breaks too (a Stop ends it at once)
        self.open_for_eeprom()

    def open_for_eeprom(self) -> None:
        """Connect and unlock for EEPROM access, starting over from a fresh session when
        the exchange breaks on the way (:data:`READ_RETRIES` times)."""
        for attempt in range(self.READ_RETRIES + 1):
            try:
                self.connect()
                self.unlock_for_eeprom()
                return
            except (LinkStopped, ClusterNoAnswer):
                raise
            except (KLineError, KLineTimeout) as ex:
                if attempt >= self.READ_RETRIES:
                    raise
                self._check_stop()
                self.log("The log-in broke (%s); starting again (%d of %d)"
                         % (ex, attempt + 1, self.READ_RETRIES))
                try:
                    self.k.clear_rx()
                except Exception:
                    pass
                self.end()
                time.sleep(2.0)
                self.kw = None
                self._custom_unlocked = False

    def _read_block(self, addr: int, n: int) -> Optional[bytes]:
        """One EEPROM read, again from a fresh session when the exchange breaks.
        Reads only: a write is never repeated on its own. A stop request or a
        refusal (None) is passed on as it is. The Audi C5 dialog re-logs-in
        with a parity change and is not re-opened here."""
        for attempt in range(self.READ_RETRIES + 1):
            try:
                return self.read_eeprom(addr, n)
            except LinkStopped:
                raise
            except (KLineError, KLineTimeout) as ex:
                if self.c5 or attempt >= self.READ_RETRIES:
                    raise
                self._check_stop()
                self.log("EEPROM @0x%03X: the line broke (%s); opening a new session "
                         "and reading it again (%d of %d)" % (addr, ex, attempt + 1,
                                                              self.READ_RETRIES))
                self._reopen()
        return None

    def _write_block(self, addr: int, values: bytes) -> bool:
        """One EEPROM block write, again from a fresh session when the exchange
        breaks. Writing the same 16 bytes to the same address twice leaves the
        same EEPROM, so this repeat is safe (the KWHack port never repeated a
        write; a refusal is still reported as it is, not repeated). The C5
        dialog is not re-opened here."""
        for attempt in range(self.READ_RETRIES + 1):
            try:
                return self.write_eeprom(addr, values)
            except LinkStopped:
                raise
            except (KLineError, KLineTimeout) as ex:
                if self.c5 or attempt >= self.READ_RETRIES:
                    raise
                self._check_stop()
                self.log("EEPROM @0x%03X: the line broke (%s); opening a new session "
                         "and writing it again (%d of %d)" % (addr, ex, attempt + 1,
                                                              self.READ_RETRIES))
                self._reopen()
        return False

    def dump(self, start: int = 0, length: int = EEPROM_SIZE,
             on_percent: Optional[Callable[[int], None]] = None,
             pct_range: Tuple[int, int] = (0, 100)) -> bytes:
        out = bytearray()
        lo, hi = pct_range
        for addr in range(start, start + length, BLOCK):
            self._check_stop()
            n = min(BLOCK, start + length - addr)
            chunk = self._read_block(addr, n)
            if chunk is None:
                raise ClusterError("the cluster refused to read EEPROM @0x%03X "
                                   "(not unlocked)" % addr)
            out += chunk
            if on_percent:
                on_percent(lo + (hi - lo) * (addr + n - start) // length)
        return bytes(out)

    def reset(self) -> bool:
        """Ask the cluster to reset itself (custom command 0x82). True when
        the request went out. A cluster that reboots at once may never answer
        it, so an answer is not required -- only that the block was sent; the
        dialog is over either way, so the session is not ended afterwards
        (a restarting cluster would not hear it)."""
        self._check_stop()
        if self.c5:
            # The real tool sends this only after a write, never after a
            # plain read (confirmed from a genuine tool's traffic): a LOGIN
            # block whose single data byte is 0x82, same as the Mk4 custom
            # reset command but inside the D1 framing.
            sent = False
            try:
                self.log("Resetting the cluster...")
                self._c5_send([C5_LOGIN, CMD_RESET])
                sent = True                  # on the wire; an answer is optional
                try:
                    hdr = self.k.read_byte()
                    size = self.k.read_byte()
                    rest = [self.k.read_byte() for _ in range(max(0, size - 2))]
                    if hdr == C5_START and len(rest) >= 1 and rest[0] != C5_ACK:
                        self.log("Warning: reset answered 0x%02X" % rest[0])
                except (KLineError, KLineTimeout):
                    pass                     # a cluster that reboots says nothing
            except (KLineError, KLineTimeout, AttributeError) as ex:
                if not sent:
                    self.log("The reset request could not be sent: %s" % ex)
            return sent
        sent = False
        try:
            self.log("Resetting the cluster...")
            if not self._custom_unlocked:            # (an EEPROM session has)
                self._send([BT_CUSTOM, 0x80, 0x01, 0x02, 0x03, 0x04])
                self._custom_unlocked = True
            self.kw.send_block([BT_CUSTOM, CMD_RESET])
            sent = True
            try:
                self.kw._receive_blocks()               # ACK/NAK, if it still can
            except (KLineError, KLineTimeout):
                pass
        except (KLineError, KLineTimeout, AttributeError) as ex:
            if not sent:
                self.log("The reset request could not be sent: %s" % ex)
        if sent:
            self.kw = None                      # nothing more to say to it
        return sent

    def end(self) -> None:
        if self.c5:
            try:
                self._set_parity("N")
                self.k.set_baud(BAUD)
            except Exception:
                pass
            return
        try:
            if self.kw is not None:
                self.kw.end_communication()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# High-level operations used by the Flash dialog
# ---------------------------------------------------------------------------
def read_cluster(link, log=None, on_percent=None, is_stop=None, fast=False):
    """Connect, unlock, read the whole 2 KB EEPROM. Returns ``(eeprom, meta)``
    where ``meta`` is what the cluster reported (for the ``.vdo`` container).
    ``fast`` is High speed (one round trip a block)."""
    c = VdoCluster(link, log, is_stop, fast=fast)
    try:
        c.open_for_eeprom()
        if log:
            log("Reading the EEPROM (2048 bytes)...")
        data = c.dump(on_percent=on_percent)
        return data, c.info.meta()
    finally:
        c.end()


@dataclass
class WriteReport:
    software_version: str
    blocks_written: List[int]
    verified: bool
    changed_params: List[str] = field(default_factory=list)
    #: the cluster was sent a reset after the verify (None: not tried)
    reset: Optional[bool] = None
    #: the user cancelled at the wrong-cluster-type warning; nothing written
    cancelled: bool = False

    def describe(self) -> str:
        if self.cancelled:
            return ("Cancelled: the file and the connected cluster are of "
                    "different types. Nothing was written.")
        s = self._written()
        if self.reset:
            s += ("\n\nThe cluster was sent a reset and restarts on the new "
                  "EEPROM.")
        elif self.reset is False:
            s += ("\n\nThe reset request could not be sent: switch the "
                  "cluster's ignition off and on to make it use the new "
                  "EEPROM.")
        return s

    def _written(self) -> str:
        if not self.blocks_written:
            return "The cluster already holds this EEPROM: nothing was written."
        then = (" and verified it by reading back" if self.verified
                else "; the cluster confirmed each block as it was written, "
                     "but it was not read back")
        if len(self.blocks_written) == EEPROM_SIZE // BLOCK:
            s = ("Wrote the whole EEPROM (%d blocks of 16 bytes)%s."
                 % (len(self.blocks_written), then))
        else:
            s = ("Wrote %d block(s) of 16 bytes (%s)%s."
                 % (len(self.blocks_written),
                    ", ".join("0x%03X" % a for a in self.blocks_written), then))
        if self.changed_params:
            s += "\n\nChanged: " + ", ".join(self.changed_params)
        return s


def _vdo_module():
    """The VDO module's definitions (None when it is not installed)."""
    try:
        from ..vdo import defs, identify, session_vdo
    except ImportError:
        return None
    return defs, identify, session_vdo


def _differing_blocks(current: bytes, image: bytes) -> List[str]:
    return ["bytes @0x%03X" % a for a in range(0, EEPROM_SIZE, BLOCK)
            if bytes(current[a:a + BLOCK]) != bytes(image[a:a + BLOCK])]


def changed_parameters(current: bytes, image: bytes) -> List[str]:
    """What ``image`` changes, by parameter name -- or, without the VDO
    module, by 16-byte block."""
    from ..binary import BinaryFile
    mod = _vdo_module()
    if mod is None:
        return _differing_blocks(current, image)
    _defs, _ident_mod, _sv = mod
    ident = _ident_mod.identify(image) or _ident_mod.identify(current)
    if ident is None:
        return []
    defn = _defs.get(ident.family)
    a, b = BinaryFile(current, endian="<"), BinaryFile(image, endian="<")
    return [ea.name for ea, eb in zip(_sv.build_entities(a, defn, ident),
                                      _sv.build_entities(b, defn, ident))
            if ea.values() != eb.values()]


def _type_name(c5: bool) -> str:
    return "Audi C5 family" if c5 else "VW Mk4 (VWK501/503 layout)"


def _confirm_type_mismatch(c: "VdoCluster", file_c5: bool, log,
                           confirm) -> bool:
    """The file is the other family's EEPROM. Ask (``confirm``), keeping the
    session alive meanwhile; True to go on, reopening the session first if
    it dropped while the user decided."""
    file_t, cl_t = _type_name(file_c5), _type_name(c.c5)
    msg = ("The file is a %s EEPROM but the connected cluster is %s (%s)."
           % (file_t, cl_t, c.info.ident or "ident unknown"))
    if log:
        log("Warning: " + msg)
    if confirm is None:
        raise ClusterTypeMismatch(msg + " Nothing was written.")
    ka = c.start_keep_alive()
    try:
        go = bool(confirm(file_t, cl_t, c.info.ident))
    finally:
        ka.stop()
    if not go:
        if log:
            log("Cancelled at the cluster-type warning; nothing written")
        return False
    if ka.lost:
        if log:
            log("The session dropped while waiting (%s); opening it again"
                % ka.error)
        c._reopen()
    return True


def write_cluster(link, image: bytes, log=None, on_percent=None,
                  is_stop=None, fast=False, verify=True,
                  confirm_mismatch: Optional[Callable[[str, str, str], bool]]
                  = None) -> WriteReport:
    """Write ``image`` (2048 bytes) to the cluster -- every 16-byte block --
    and, with ``verify``, read each block back (Advanced in the flasher; it
    takes as long again as the write). ``fast`` is High speed (one round trip a
    block). Without ``verify`` the cluster's per-block ``F9`` reply, which
    echoes the request, is the only confirmation.

    Nothing is read from the cluster first (owner's decision, 2026-10-11:
    the read-first, backup and identity comparison were KWHack's behaviour).
    The only check is on the file itself: it must be a recognised VDO Mk4
    EEPROM layout.

    Once connected, the file's layout is compared with the cluster's type
    (from its ident). On a mismatch nothing is written unless
    ``confirm_mismatch(file_type, cluster_type, ident)`` returns True; it is
    called from this thread while a keep-alive thread holds the session open,
    and without it the mismatch raises :class:`ClusterTypeMismatch`. A
    declined mismatch returns a report with ``cancelled`` set."""
    from ..platforms.vdo import layout_of
    image = bytes(image)
    if len(image) != EEPROM_SIZE:
        raise ClusterError("a cluster EEPROM image is exactly 2048 bytes; this "
                           "one is %d" % len(image))
    layout = layout_of(image)
    if layout is None:
        raise ClusterError("the file is not a recognised VDO Mk4 cluster EEPROM")
    c = VdoCluster(link, log, is_stop, fast=fast)
    try:
        c.open_for_eeprom()                  # a broken log-in starts over, as a read's does
        if (layout == "C5") != c.c5:
            if not _confirm_type_mismatch(c, layout == "C5", log,
                                          confirm_mismatch):
                return WriteReport(c.info.software_version, [], False,
                                   cancelled=True)
        version = c.info.software_version
        todo = list(range(0, EEPROM_SIZE, BLOCK))
        if log:
            log("Writing the EEPROM (%d blocks)..." % len(todo))
        failed = []
        for i, a in enumerate(todo):
            c._check_stop()
            if not c._write_block(a, image[a:a + BLOCK]):
                failed.append(a)
            if on_percent:
                on_percent((60 if verify else 100) * (i + 1) // len(todo))
        if failed:
            raise ClusterError("the cluster refused the write at %s -- the "
                               "EEPROM may be partly written; write again"
                               % ", ".join("0x%03X" % a for a in failed))
        bad = []
        if verify and log:
            log("Verifying...")
        for i, a in enumerate(todo if verify else ()):
            c._check_stop()
            back = c._read_block(a, BLOCK)
            if back != image[a:a + BLOCK]:
                bad.append(a)
            if on_percent:
                on_percent(60 + 40 * (i + 1) // len(todo))
        if bad:
            raise ClusterError("verify failed at %s: the cluster reads back "
                               "different bytes" % ", ".join("0x%03X" % a for a in bad))
        # Written and read back clean: reset it so it runs on the new EEPROM.
        # Last, and never a reason to call a good write a failure.
        return WriteReport(version, todo, bool(verify), reset=c.reset())
    finally:
        c.end()
