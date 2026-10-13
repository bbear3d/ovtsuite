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
6. End communication (block 0x06).

:func:`write_cluster` writes the whole file, as KWHack does, then reads every
block back and compares. Nothing is read from the cluster before the write
(no backup, no comparison with the cluster's own data); the file itself must
be a recognised VDO Mk4 EEPROM layout.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import kwp as _kwp
from .transport import KLineError, KLineTimeout

ADDRESS = 0x17               # KW1281 logical address of the instrument cluster
BAUD = 10400
EEPROM_SIZE = 2048
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

MAX_ACCESS_LEVEL = 7


class StopRequested(Exception):
    pass


class ClusterError(KLineError):
    pass


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
    # Only level 7 is confirmed for this one (KWHack: brute-forced from an
    # Audi C5 cluster, verified on three more pairs); 0-6 are placeholders.
    0x06: [[0, 0, 0, 0]] * 7 + [[0x2b, 0x3a, 0x58, 0x41]],
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
                 is_stop: Optional[Callable[[], bool]] = None):
        self.k = link
        self._log = log or (lambda s: None)
        self._is_stop = is_stop or (lambda: False)
        self.kw: Optional[_kwp.Kw1281] = None
        self.info = ClusterInfo()
        self._custom_unlocked = False

    def log(self, msg: str) -> None:
        self._log(msg)

    def _check_stop(self):
        if self._is_stop():
            raise StopRequested("stopped")

    # -- blocks ------------------------------------------------------------
    def _send(self, payload) -> List[bytes]:
        self.kw.send_block(list(payload))
        return self.kw._receive_blocks()

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
        for attempt in range(1, tries + 1):
            self._check_stop()
            try:
                self.k.set_baud(BAUD)
                self.log("Connecting to the cluster (KW1281 slow init, 0x17 @ %d)..."
                         % BAUD)
                version = self.k.wake_up(ADDRESS, even_parity=False)
                if version >= 2000:
                    raise ClusterError("the controller at 0x17 answered with "
                                       "KWP2000 keyword %d, not KW1281 -- not a "
                                       "VDO Mk4-era cluster" % version)
                self.kw = _kwp.Kw1281(self.k, self.log)
                blocks = self.kw.connect()
                self.info.ident_blocks = list(blocks)
                # the ident is one string cut into fixed-size blocks
                self.info.ident = " ".join("".join(
                    b[3:].decode("latin-1") for b in blocks
                    if b[2] == BT_ASCII).split())
                self.log("Connected: %s" % self.info.ident)
                return self.info
            except (KLineError, KLineTimeout) as ex:
                last = ex
                self.log("Connect attempt %d failed: %s" % (attempt, ex))
                try:
                    self.k.clear_rx()
                except Exception:
                    pass
                time.sleep(1.0)
        raise last or ClusterError("the cluster did not answer")

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
        for variation in range(4):
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

    # -- EEPROM ----------------------------------------------------------------
    def read_eeprom(self, addr: int, count: int) -> Optional[bytes]:
        blocks = self._send([BT_READ_EEPROM, count & 0xFF, (addr >> 8) & 0xFF,
                             addr & 0xFF])
        if len(blocks) == 1 and blocks[0][2] == BT_NAK:
            return None                         # permission refused
        data = self._data_blocks(blocks)
        if len(data) != 1:
            raise ClusterError("ReadEeprom returned %d blocks instead of 1"
                               % len(data))
        body = bytes(data[0][3:])
        if len(body) != count:
            raise ClusterError("ReadEeprom @0x%03X: %d bytes back, asked for %d"
                               % (addr, len(body), count))
        return body

    def write_eeprom(self, addr: int, values: bytes) -> bool:
        values = bytes(values)
        send = [BT_WRITE_EEPROM, len(values), (addr >> 8) & 0xFF,
                addr & 0xFF] + list(values)
        blocks = self._send(send)
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

    def dump(self, start: int = 0, length: int = EEPROM_SIZE,
             on_percent: Optional[Callable[[int], None]] = None,
             pct_range: Tuple[int, int] = (0, 100)) -> bytes:
        out = bytearray()
        lo, hi = pct_range
        for addr in range(start, start + length, BLOCK):
            self._check_stop()
            n = min(BLOCK, start + length - addr)
            chunk = self.read_eeprom(addr, n)
            if chunk is None:
                raise ClusterError("the cluster refused to read EEPROM @0x%03X "
                                   "(not unlocked)" % addr)
            out += chunk
            if on_percent:
                on_percent(lo + (hi - lo) * (addr + n - start) // length)
        return bytes(out)

    def end(self) -> None:
        try:
            if self.kw is not None:
                self.kw.end_communication()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# High-level operations used by the Flash dialog
# ---------------------------------------------------------------------------
def read_cluster(link, log=None, on_percent=None, is_stop=None):
    """Connect, unlock, read the whole 2 KB EEPROM. Returns ``(eeprom, meta)``
    where ``meta`` is what the cluster reported (for the ``.vdo`` container)."""
    c = VdoCluster(link, log, is_stop)
    try:
        c.connect()
        c.unlock_for_eeprom()
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

    def describe(self) -> str:
        if not self.blocks_written:
            return "The cluster already holds this EEPROM: nothing was written."
        if len(self.blocks_written) == EEPROM_SIZE // BLOCK:
            s = ("Wrote the whole EEPROM (%d blocks of 16 bytes) and verified it "
                 "by reading back." % len(self.blocks_written))
        else:
            s = ("Wrote %d block(s) of 16 bytes (%s) and verified them by reading "
                 "back." % (len(self.blocks_written),
                            ", ".join("0x%03X" % a for a in self.blocks_written)))
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


def write_cluster(link, image: bytes, log=None, on_percent=None,
                  is_stop=None) -> WriteReport:
    """Write ``image`` (2048 bytes) to the cluster -- every 16-byte block --
    and read each block back.

    Nothing is read from the cluster first (owner's decision, 2026-10-11:
    the read-first, backup and identity comparison were KWHack's behaviour).
    The only check is on the file itself: it must be a recognised VDO Mk4
    EEPROM layout."""
    from ..platforms.vdo import layout_of
    image = bytes(image)
    if len(image) != EEPROM_SIZE:
        raise ClusterError("a cluster EEPROM image is exactly 2048 bytes; this "
                           "one is %d" % len(image))
    if layout_of(image) is None:
        raise ClusterError("the file is not a recognised VDO Mk4 cluster EEPROM")
    c = VdoCluster(link, log, is_stop)
    try:
        c.connect()
        c.unlock_for_eeprom()
        version = c.info.software_version
        todo = list(range(0, EEPROM_SIZE, BLOCK))
        if log:
            log("Writing the EEPROM (%d blocks)..." % len(todo))
        failed = []
        for i, a in enumerate(todo):
            c._check_stop()
            if not c.write_eeprom(a, image[a:a + BLOCK]):
                failed.append(a)
            if on_percent:
                on_percent(60 * (i + 1) // len(todo))
        if failed:
            raise ClusterError("the cluster refused the write at %s -- the "
                               "EEPROM may be partly written; write again"
                               % ", ".join("0x%03X" % a for a in failed))
        if log:
            log("Verifying...")
        bad = []
        for i, a in enumerate(todo):
            c._check_stop()
            back = c.read_eeprom(a, BLOCK)
            if back != image[a:a + BLOCK]:
                bad.append(a)
            if on_percent:
                on_percent(60 + 40 * (i + 1) // len(todo))
        if bad:
            raise ClusterError("verify failed at %s: the cluster reads back "
                               "different bytes" % ", ".join("0x%03X" % a for a in bad))
        return WriteReport(version, todo, True)
    finally:
        c.end()
