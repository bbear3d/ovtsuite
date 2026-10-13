# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 external-flash read/write over K-line (direct KWP2000 programming).

Ported from KWHack ``EDC16/Edc16FlashVM.cs`` and ``docs/EDC16-Write-Protocol-Map.md``
(both bench-validated on a 038997016T / EDC16U31). Reads/writes the 512 KB
calibration window 0x180000-0x1FFFFF; the lower 1.5 MB (code) is not reachable
over K-line. Security access is ADDRESSED; the transfer phase is ADDRESS-LESS.

Speeds map to KWHack's: Low 38400 (0x50), Medium 124800 (0x87), High 244898 (0xA7).
All progress/stop hooks are optional and safe to omit.
"""
import re
import time

from .transport import KLineError, KLineTimeout
from .crypto import (edc16_lvl1_key, edc16_lvl3_key, edc16_lvl5_key,
                     edc16_lvl9_key, encrypt)

CAL_START = 0x180000
CAL_SIZE = 0x080000
FULL_SIZE = 0x200000

# TP 2.0 logical address of the engine ECU over CAN, for the later CAN-only
# EDC16 variants (see project notes: edc16-kline-can-coexist.md). Standard VAG
# engine-ECU address -- unlike ZF6HP (0x02) / Climatronic (0x2C) / Cluster
# (0x07, see cluster.py's own DEST_CLUSTER for why that ISN'T 0x17), 0x01 is
# not something a K-line EDC16 session ever negotiates, so it lives here
# rather than being derived from anything K-line-related in this module.
CAN_DEST = 0x01

SPEEDS = {"Low": (0x50, 38400), "Medium": (0x87, 124800), "High": (0xA7, 244898)}

# Between two J2534 high-baud discovery candidates the ECU must return to its
# default 10400 idle state before the next fast init, or that init gets no reply
# and the device call blocks ("hangs on Connecting").  When a candidate acks the
# speed switch, the ECU jumps to ITS high baud (e.g. 244898) regardless of the
# device rate we picked; dropping the channel then leaves the K-line idle, and
# the ECU abandons the session after its P3max timeout (KWP2000 default 5000 ms)
# and falls back to 10400.  So wait a hair over that before re-initialising.
HIGH_BAUD_SETTLE_S = 5.5

# After a fast-init WRITE prime the ECU's fast-init session must return to idle
# before it will accept a fresh 5-baud slow init that grants the 0x85 download
# session.  Two ways to get there:
#   * TEARDOWN (default): send StopCommunication (0x82 -> C2) to end the fast
#     session explicitly, then wait only a short bus-idle.  Fast and clean; the
#     ECU is captured ACKing 0x82 in the write-finalize sequence.  If the slow
#     init still isn't accepted, we fall back to the P3max wait below.
#   * LEGACY: no teardown -- just wait out the fast session's P3max inactivity
#     timeout (KWP2000 default 5000 ms) so it dies on its own.  A hair over P3max.
_P3MAX_SETTLE_S = 5.5
_TEARDOWN_IDLE_S = 0.3          # bus-idle after StopComm before the slow re-init

# Bench-tunable prime defaults (write_flash resolves None kwargs to these):
_PRIME_TEARDOWN = True          # True = StopComm teardown + short idle (with P3max fallback);
                                # False = legacy blind P3max wait
_PRIME_TRIM = False             # True = prime with the fast StartCommunication ONLY
                                # (skip erase-sig / lvl1-auth / the refused 0x85);
                                # False = full prime driven through the refused 0x85

# per-block (start, checksum/verify end, erase end, download size)
_BLOCK = {7: (0x1C0000, 0x1FDFFF, 0x1FFFFF, 0x3E000),
          6: (0x180000, 0x1BFFFF, 0x1BFFFF, 0x40000)}

_TRANSFER_CHUNK = 0xF8          # 248 data bytes; header length byte = 0xF9
_DEFAULT_ERASE_SIG = bytes([0x0C, 0x5E, 0xCF, 0xCF, 0x65, 0x0B])

# KWP2000 / UDS negative-response codes seen on the EDC16 download path.  Decoded
# from a NAK frame so a failed TransferData reports WHY, not just "expected 0x01".
_NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-responsePending",
    0x7F: "serviceNotSupportedInActiveSession",
}


def _nrc_name(code):
    return _NRC_NAMES.get(code, "NRC 0x%02X" % code)


def _cks(data) -> int:
    s = 0
    for b in data:
        s = (s + b) & 0xFF
    return s


def _contains(buf, *seq) -> bool:
    seq = bytes(seq)
    return seq in bytes(buf) if seq else False


def _index_of_pair(buf, a, b) -> int:
    for i in range(len(buf) - 1):
        if buf[i] == a and buf[i + 1] == b:
            return i
    return -1


# Which dataset PROJECTS accept a cold 5-baud slow init (so the 0x85 download
# session can be opened directly) vs. need a fast-init prime first.  The project
# is the 8-char dataset id in the Bosch block header (e.g. "P350U8A9",
# "P475AAXE", "P447HAS7") -- a format-defined field present in every image.  It
# is a more reliable, generation-aware signal than calibration-fingerprint family
# matching, which can mistake a later U34/P447 dataset for p350 because the maps
# overlap.  P350 (EDC16U31 1.9/2.0L) responds to a cold slow init; the P475
# lineage (later CAN-init OS) does not and must be primed.  Any OTHER project
# (P447/U34 and anything unseen) is left UNKNOWN -> AUTO probe, never assumed.
_SLOW_INIT_PROJECTS = ("P350", "P447")
_FAST_PRIME_PROJECTS = ("P475",)


def image_project(image):
    """The dataset project code of an EDC16 image (e.g. 'P350', 'P475', 'P447'),
    read from the Bosch block-header dataset id, or None if not found."""
    try:
        from ..fileinfo import ident16
        ds = ident16(bytes(image)).get("dataset_id") or ""
    except Exception:
        return None
    m = re.match(r"P\d{3}", ds.upper())
    return m.group(0) if m else None


def fast_prime_from_image(image):
    """Decide the K-line write init strategy from the image, structurally.

    Returns a value suitable as ``write_flash(fast_init_prime=...)``:
      * ``False`` -- project accepts a cold slow init (P350, and P447/BRM,
        confirmed responding to K-line slow init on the bench) -> slow init only.
      * ``True``  -- later CAN-init project (P475) -> fast-init prime first.
      * ``None``  -- project unknown/unseen (a newer U34/generation, garbage,
        or unreadable)
        -> AUTO fallback (try a cold slow init, prime only if the ECU ignores it).

    Uses the dataset project code (a format-defined header field), so it needs no
    DAMOS, no part-number string, and no fingerprint guess -- and a new generation
    is safely handled as UNKNOWN rather than mislabelled from overlapping maps."""
    proj = image_project(image)
    if proj in _SLOW_INIT_PROJECTS:
        return False
    if proj in _FAST_PRIME_PROJECTS:
        return True
    return None


class StopRequested(Exception):
    pass


class Edc16Flash:
    def __init__(self, link, log=None):
        self.k = link
        self._log = log or (lambda s: None)
        self._fast = False
        self._erase_sig = _DEFAULT_ERASE_SIG
        self._max_block_len = 0            # ECU-declared TransferData max (0x74 <FD>)

    def log(self, msg):
        self._log(msg)

    # ---- framed I/O ----
    def _send(self, payload):
        """Send payload + trailing checksum. Batched at high speed; paced
        per-byte at the slow handshake speed (mirrors WriteByteDelayed)."""
        frame = bytes(payload) + bytes([_cks(payload)])
        if self._fast:
            self.k.write_bytes(frame)
        else:
            for b in frame:
                self.k.write_byte(b)
                time.sleep(0.005)

    def _read_available(self, max_bytes=32):
        """Drain a response with no fixed length: block for the first byte, then
        read with a short timeout until it goes quiet."""
        out = bytearray([self.k.read_byte()])
        saved = self.k.read_timeout
        self.k.read_timeout = 0.05
        try:
            while len(out) < max_bytes:
                try:
                    out.append(self.k.read_byte())
                except KLineTimeout:
                    break
        finally:
            self.k.read_timeout = saved
        return bytes(out)

    def _read_response(self, max_bytes=32):
        try:
            return self._read_available(max_bytes)
        except KLineTimeout:
            return b""

    def _check_rec(self, expected):
        b = self.k.read_byte()
        if b != expected:
            raise KLineError("EDC16: expected 0x%02X, got 0x%02X" % (expected, b))
        return b

    def _verify_expected(self, expected):
        """Read the exact expected sequence + checksum, tolerating a leading
        responsePending NAK (83 F1 10 7F <svc> 78 <cks>)."""
        expected = bytes(expected)
        while True:
            first = self.k.read_byte()
            if first != expected[0]:
                if first == 0x83:
                    rest = self.k.read(6)
                    if rest[0] == 0xF1 and rest[1] == 0x10 and rest[2] == 0x7F \
                            and rest[4] == 0x78:
                        continue                     # response pending; wait for real
                    raise KLineError("EDC16: NAK-shaped frame 83 %s" % rest.hex())
                raise KLineError(
                    "EDC16: expected 0x%02X, got 0x%02X" % (expected[0], first))
            for i in range(1, len(expected)):
                b = self.k.read_byte()
                if b != expected[i]:
                    raise KLineError(
                        "EDC16: mismatch at %d: expected 0x%02X got 0x%02X"
                        % (i, expected[i], b))
            cksb = self.k.read_byte()
            if cksb != _cks(expected):
                raise KLineError("EDC16: handshake checksum mismatch")
            return

    # ---- connect / auth ----
    def connect(self):
        self.log("Connecting to EDC16...")          # init method is the link's (5-baud / fast / TP2.0)
        version = self.k.wake_up(0x01, even_parity=False)
        if version < 2000:
            raise KLineError("Unexpected EDC16 wakeup version %d" % version)
        self.log("Connected (KW %d)." % version)

    def _read_erase_signature(self):
        time.sleep(0.025)
        self.k.clear_rx()
        self._send(bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C]))
        acc = bytearray()
        deadline = time.time() + 3.0
        saved = self.k.read_timeout
        self.k.read_timeout = 0.4
        try:
            while time.time() < deadline:
                try:
                    acc.append(self.k.read_byte())
                except KLineTimeout:
                    if _index_of_pair(acc, 0x5A, 0x9C) >= 0:
                        break
        finally:
            self.k.read_timeout = saved
        pos = _index_of_pair(acc, 0x5A, 0x9C)
        if pos >= 0 and pos + 12 <= len(acc):
            self._erase_sig = bytes(acc[pos + 6:pos + 12])
            self.log("EDC16 erase signature (1A 9C): %s" % self._erase_sig.hex())
        else:
            self.log("WARNING: couldn't read erase signature; using default.")
        self.k.clear_rx()

    def _lvl3_auth(self):
        time.sleep(0.025)
        self._send(bytes([0x82, 0x10, 0xF1, 0x27, 0x03]))
        seed = self.k.read(10)
        if _cks(seed[:9]) != seed[9]:
            raise KLineError("EDC16 LVL3 seed checksum mismatch")
        combined = edc16_lvl3_key(seed[5:9])
        frame = bytes([0x86, 0x10, 0xF1, 0x27, 0x04,
                       (combined >> 24) & 0xFF, (combined >> 16) & 0xFF,
                       (combined >> 8) & 0xFF, combined & 0xFF])
        frame += bytes([_cks(frame)])
        time.sleep(0.025)
        self.k.write_raw(frame)
        self.k.read(len(frame))                      # echo
        self._verify_expected(bytes([0x83, 0xF1, 0x10, 0x67, 0x04, 0x34]))

    def _lvl1_auth(self):
        time.sleep(0.025)
        self.k.clear_rx()
        self._send(bytes([0x82, 0x10, 0xF1, 0x27, 0x01]))
        seed = self.k.read(10)
        if _cks(seed[:9]) != seed[9]:
            raise KLineError("EDC16 LVL1 seed checksum mismatch")
        key = edc16_lvl1_key(seed[5:9])
        frame = bytes([0x86, 0x10, 0xF1, 0x27, 0x02]) + key
        frame += bytes([_cks(frame)])
        time.sleep(0.025)
        self.k.write_raw(frame)
        self.k.read(len(frame))                      # echo
        self._verify_expected(bytes([0x83, 0xF1, 0x10, 0x67, 0x02, 0x34]))

    def _lvl9_auth(self):
        """SecurityAccess level 9 (27 09 requestSeed / 27 0A sendKey).

        Key algorithm reversed statically from the ECU binary (validator
        0x145AC): 35-round MSB-first LFSR over the 4-byte seed with polynomial
        0x0A24C4C1.  See crypto.edc16_lvl9_key.  This is the boot/low-region
        read+write unlock (grant_bit 1)."""
        time.sleep(0.025)
        self.k.clear_rx()
        self._send(bytes([0x82, 0x10, 0xF1, 0x27, 0x09]))
        seed = self.k.read(10)
        if _cks(seed[:9]) != seed[9]:
            raise KLineError("EDC16 LVL9 seed checksum mismatch")
        combined = edc16_lvl9_key(seed[5:9])
        frame = bytes([0x86, 0x10, 0xF1, 0x27, 0x0A,
                       (combined >> 24) & 0xFF, (combined >> 16) & 0xFF,
                       (combined >> 8) & 0xFF, combined & 0xFF])
        frame += bytes([_cks(frame)])
        time.sleep(0.025)
        self.k.write_raw(frame)
        self.k.read(len(frame))                      # echo
        self._verify_expected(bytes([0x83, 0xF1, 0x10, 0x67, 0x0A, 0x34]))

    def _lvl5_auth(self, k5=None):
        """SecurityAccess level 5 (27 05 requestSeed / 27 06 sendKey).

        key = (seed + K5) & 0xFFFFFFFF where K5 is the per-ECU 16-bit coding value
        at RAM 0x7FEA0B (coding struct +0x27).  If k5 is not supplied it is read
        live from the ECU via readMemoryByAddress (needs a read level already
        active).  See crypto.edc16_lvl5_key."""
        if k5 is None:
            data = self._read_memory_by_address(0x7FE9E4, 0x40)
            k5 = (data[0x27] << 8) | data[0x28]
        time.sleep(0.025)
        self.k.clear_rx()
        self._send(bytes([0x82, 0x10, 0xF1, 0x27, 0x05]))
        seed = self.k.read(10)
        if _cks(seed[:9]) != seed[9]:
            raise KLineError("EDC16 LVL5 seed checksum mismatch")
        combined = edc16_lvl5_key(seed[5:9], k5)
        frame = bytes([0x86, 0x10, 0xF1, 0x27, 0x06,
                       (combined >> 24) & 0xFF, (combined >> 16) & 0xFF,
                       (combined >> 8) & 0xFF, combined & 0xFF])
        frame += bytes([_cks(frame)])
        time.sleep(0.025)
        self.k.write_raw(frame)
        self.k.read(len(frame))                      # echo
        self._verify_expected(bytes([0x83, 0xF1, 0x10, 0x67, 0x06, 0x34]))

    # ---- session + speed ----
    def _wait_speed_ack(self, session, timeout_ms):
        deadline = time.time() + timeout_ms / 1000.0
        prev, have = 0, False
        saved = self.k.read_timeout
        self.k.read_timeout = 0.5
        try:
            while time.time() < deadline:
                try:
                    b = self.k.read_byte()
                except KLineTimeout:
                    continue
                if have and prev == 0x50 and b == session:
                    self.k.read_timeout = 0.06
                    for _ in range(8):
                        try:
                            self.k.read_byte()
                        except KLineTimeout:
                            break
                    return True
                prev, have = b, True
        finally:
            self.k.read_timeout = saved
        return False

    def _enter_session(self, session_sub, speed, allow_fast, baud_override=None,
                       verify=False, slow_fallback=True):
        if allow_fast:
            speed_byte, baud = SPEEDS[speed]
            if baud_override is not None:
                baud = baud_override                    # discovery: test this exact device rate
            time.sleep(0.075)
            self.k.clear_rx()
            # A message-based link (J2534/CAN) waits for the ack inside _send and
            # raises on no-reply; a serial link just writes and _wait_speed_ack
            # polls.  Either way, treat a missing/late ack as "baud refused" and
            # fall back to 10400 instead of aborting.
            try:
                self._send(bytes([0x83, 0x10, 0xF1, 0x10, session_sub, speed_byte]))
                acked = self._wait_speed_ack(session_sub, 3000)
            except KLineTimeout:
                acked = False
            if acked:
                time.sleep(0.075)
                self.k.set_baud(baud)
                self._fast = True
                if verify:
                    self._verify_baud()                 # raises if the ECU isn't clean at this rate
                self.log("Link speed -> %d baud." % baud)
                return
            if verify:                                  # discovery attempt: no fallback, just fail
                raise KLineError("no speed ack for baud %d" % baud)
            if not slow_fallback:
                # Prime pass (write path): the refused speed negotiation is the reliable
                # signal that this ECU won't grant the session over fast init.  Don't
                # spend the 10400 fallback's ack timeout -- bail so the caller re-inits slow.
                raise KLineError(
                    "EDC16 refused the 0x%02X session speed negotiation over fast init" % session_sub)
            self.log("ECU refused %d baud; entering session at 10400." % baud)
            self.k.clear_rx()
        time.sleep(0.075)
        self.k.clear_rx()
        self._send(bytes([0x82, 0x10, 0xF1, 0x10, session_sub]))
        if not self._wait_speed_ack(session_sub, 3000):
            raise KLineError(
                "EDC16 could not enter session 0x%02X" % session_sub)
        self.log("Entered session 0x%02X (10400)." % session_sub)

    @staticmethod
    def _kline_frame_ok(buf):
        """Find a complete, checksum-valid addressed KWP response frame
        (fmt 8x, tgt F1, src 10) anywhere in ``buf`` and return its data field,
        else None.  A marginal baud corrupts at least one byte, so the frame's own
        running checksum stops adding up -- that mismatch is what rejects the rate.
        Scans every offset so leading garbage or a shifted length can't fool it."""
        b = bytes(buf)
        for i in range(max(0, len(b) - 3)):
            fmt = b[i]
            if (fmt & 0xC0) != 0x80 or b[i + 1] != 0xF1 or b[i + 2] != 0x10:
                continue
            n = fmt & 0x3F
            j = i + 3
            if n == 0:                              # length carried in a separate byte
                if j >= len(b):
                    continue
                n = b[j]
                j += 1
            end = j + n                             # index of the checksum byte
            if n == 0 or end >= len(b):
                continue
            if _cks(b[i:end]) == b[end]:
                return b[j:end]
        return None

    def _verify_baud(self, rounds=8):
        """Confirm the device rate matches the ECU's after a high-baud switch.  A
        SINGLE 1A 9C ident can pass by luck at a MARGINAL rate (the short frame
        happens to land clean) and then the real, much longer transfer gets a
        corrupt byte and the ECU drops the session (silence).  So repeat the ident
        several times and require EVERY reply to be a checksum-valid 5A 9C frame:
        a marginal rate fails at least one round (a bad byte or a dropped reply),
        only a genuinely clean rate passes them all -- discovery then keeps
        sweeping to the neighbouring rate that is actually clean.  Short per-round
        timeout so a dead candidate is rejected fast, not after the 8 s default."""
        saved = self.k.read_timeout
        self.k.read_timeout = 0.6
        try:
            for _ in range(rounds):
                self.k.clear_rx()
                try:
                    self._send(bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C]))
                    r = self._read_response(32)
                except KLineError:
                    raise KLineError("baud verify: no reply")
                data = self._kline_frame_ok(r)
                if data is None:
                    raise KLineError("baud verify: corrupt/short frame (%s)"
                                     % bytes(r[:12]).hex())
                if _index_of_pair(data, 0x5A, 0x9C) < 0:
                    raise KLineError("baud verify: no 5A 9C in reply (%s)"
                                     % bytes(data[:8]).hex())
        finally:
            self.k.read_timeout = saved

    def _settle(self, seconds, is_stop=None):
        """Sleep ``seconds`` in small slices so a pending Cancel is honoured
        promptly instead of only after the full wait."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if is_stop and is_stop():
                raise StopRequested("stopped")
            time.sleep(min(0.1, max(0.0, deadline - time.time())))

    def _set_init_mode(self, fast):
        """Force the K-line link to fast (ISO14230) or 5-baud slow init for the next
        connect().  Serial KLine uses ``prefer_fast_init``; J2534KLine uses ``_fast``."""
        k = self.k
        if getattr(k, "is_kline", False):           # J2534KLine
            try:
                k._fast = bool(fast)
            except Exception:
                pass
        else:                                        # serial KLine
            try:
                k.prefer_fast_init = bool(fast)
            except Exception:
                pass

    def _try_connect_fast(self):
        """Fast-init connect for the write-path PRIME.  Returns True if the ECU woke,
        False if it didn't (never raises)."""
        self._set_init_mode(True)
        try:
            self.connect()
            return True
        except KLineError:
            return False

    def _connect_slow(self):
        """5-baud SLOW-init connect (drops back to 10400 first).  Raises if the ECU
        does not wake -- a later CAN-init EDC16 ignores a cold slow init."""
        self._set_init_mode(False)
        self._fast = False
        for fn in (lambda: self.k.set_baud(10400), self.k.clear_rx):
            try:
                fn()
            except Exception:
                pass
        self.connect()

    def _establish_session(self, session_sub, speed, allow_fast, prepare, is_stop=None):
        """connect + auth (via ``prepare``) + enter the session.  For a J2534
        K-line link at a high baud, the device rate that matches the ECU is
        unknown and can only be tested with a FRESH init per candidate (a wrong
        rate corrupts the live session), so retry the whole prepare+enter at each
        candidate until one verifies; cache the winner for the app run.

        Between candidates the ECU must fall back from its high baud to 10400 (it
        jumps to its own high rate the moment it acks the switch), so drop the
        channel and wait out its session timeout (HIGH_BAUD_SETTLE_S) before the
        next fast init -- without that the re-init got no reply and the device
        call blocked ('hangs on Connecting').  ``is_stop`` makes both the sweep
        and that settle cancellable."""
        kl = self.k
        # Only HIGH needs the sweep: its device rate is the unknown marginal one
        # (~245000).  Medium (124800) and Low (38400) have exact clean divisors the
        # device hits directly, so they take the normal fast path -- never the sweep.
        hi = (allow_fast and speed == "High" and getattr(kl, "is_kline", False)
              and hasattr(kl, "high_baud_candidates"))
        if not hi:
            prepare()
            self._enter_session(session_sub, speed, allow_fast)
            return
        speed_byte = SPEEDS[speed][0]
        cached = kl.cached_high_baud(speed_byte)
        if cached is not None:
            prepare()
            self._enter_session(session_sub, speed, True, baud_override=cached)
            return
        self.log("Finding the J2534 high-speed baud (fresh init per candidate)...")
        saved_to = self.k.read_timeout
        for i, baud in enumerate(kl.high_baud_candidates()):
            if is_stop and is_stop():
                raise StopRequested("stopped during baud discovery")
            try:
                self.k.read_timeout = 1.0               # a dead candidate must fail fast
                kl.reset_link()                         # drop channel -> K-line goes idle
                if i > 0:
                    # let the ECU time out its session back to 10400 before we
                    # fast-init again (the previous candidate switched it high)
                    self.log("Waiting %.1fs for the ECU to reset before the next rate..."
                             % HIGH_BAUD_SETTLE_S)
                    self._settle(HIGH_BAUD_SETTLE_S, is_stop)
                prepare()
                self._enter_session(session_sub, speed, True, baud_override=baud, verify=True)
                self.k.read_timeout = saved_to
                kl.set_cached_high_baud(speed_byte, baud)
                self.log("J2534 high-speed baud = %d." % baud)
                return
            except StopRequested:
                raise
            except KLineError:
                continue
            finally:
                self.k.read_timeout = saved_to
        if is_stop and is_stop():
            raise StopRequested("stopped during baud discovery")
        # No clean High rate on this device -> fall back to Medium (124800), NOT
        # 10400: Medium has an exact divisor the device hits reliably and is ~12x
        # faster than 10400.  _enter_session at Medium itself drops to 10400 only
        # if the ECU refuses the 124800 switch.
        self.log("No J2534 high baud matched; falling back to Medium (124800).")
        kl.reset_link()
        self._settle(HIGH_BAUD_SETTLE_S, is_stop)
        prepare()
        self._enter_session(session_sub, "Medium", True)

    # ---- read ----
    def _read_memory_by_address(self, addr, size):
        """readMemoryByAddress (0x23) of `size` bytes at 24-bit `addr`.  Returns
        the raw bytes on a positive 0x63 response; raises KLineError with the
        decoded NRC on 7F 23 xx.  Non-destructive (a pure read).

        The ECU maps the 24-bit address as region_table[addr>>18] + (addr &
        0xFFFFF) (handler 0xB1360, table @ r14-0x700, RAM-built).  If that table
        is identity-mapped the address is effectively raw; if not, the returned
        bytes are from a different physical address -- which the caller can tell
        from whether the content looks right."""
        size &= 0xFF
        if not self._fast:
            time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x05, 0x23,
                          (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, size]))
        deadline = time.time() + 5.0
        while True:
            r = self._read_response(max_bytes=size + 12)
            if not r:
                raise KLineError("readMemoryByAddress: no response")
            j = _index_of_pair(r, 0x7F, 0x23)
            if j >= 0:
                nrc = r[j + 2] if j + 2 < len(r) else 0
                if nrc == 0x78 and time.time() < deadline:
                    continue                              # responsePending
                raise KLineError("readMemoryByAddress rejected: 7F 23 %02X (%s)"
                                 % (nrc, _nrc_name(nrc)))
            k = bytes(r).find(b"\x63")
            if k >= 0:
                data = r[k + 1:k + 1 + size]
                if len(data) >= size:
                    return bytes(data[:size])
                # short read: drain a little more then return what we have
                more = self._read_response(max_bytes=size)
                return bytes((r[k + 1:] + more)[:size])
            raise KLineError("readMemoryByAddress: unexpected response %s" % r.hex())

    def _request_upload(self, addr, size):
        """requestUpload(0x35) for [addr, addr+size); positive ack 02 75 FF, a
        NAK is 03 7F 35 <NRC>.  Decodes the NRC instead of dying on the length
        byte ("expected 0x02, got 0x03"), so a refused range names WHY."""
        if not self._fast:
            time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x08, 0x35,
                          (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, 0x00,
                          (size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF]))
        deadline = time.time() + 5.0
        while True:
            b0 = self.k.read_byte()
            if b0 == 0x02:                               # positive: 02 75 FF <cks>
                d1 = self.k.read_byte(); d2 = self.k.read_byte(); self.k.read_byte()
                if d1 == 0x75 and d2 == 0xFF:
                    return
                raise KLineError("EDC16 requestUpload: unexpected 02 %02X %02X"
                                 % (d1, d2))
            if b0 == 0x83:                               # responsePending header
                rest = self.k.read(6)
                if len(rest) >= 5 and rest[2] == 0x7F and rest[4] == 0x78                         and time.time() < deadline:
                    continue
                raise KLineError("EDC16 requestUpload: NAK-shaped 83 %s"
                                 % rest.hex())
            if b0 == 0x03:                               # negative: 03 7F 35 <NRC>
                d = self.k.read(3)
                nrc = d[2] if len(d) >= 3 else 0
                raise KLineError("EDC16 requestUpload rejected: 7F 35 %02X (%s)"
                                 % (nrc, _nrc_name(nrc)))
            raise KLineError("EDC16 requestUpload: bad response 0x%02X" % b0)

    def read_flash(self, on_percent=None, on_stage=None, is_stop=None,
                   speed="Medium", allow_fast=True, upload_chunk=None):
        """Read the 512 KB cal region; returns a full 2 MB image (0xFF pad +
        cal @0x180000), matching MPPS.

        ``upload_chunk`` (bytes) reads the region as a sequence of separate
        requestUpload transfers of that size -- required over CAN/TP 2.0, where
        this ECU refuses a single 512 KB upload (it drops the session after
        ~29 KB); MPPS reads it as 8 x 64 KB uploads by address.  K-line uses a
        single upload (``None``)."""
        if on_stage:
            on_stage("Connecting...")

        def _prepare():
            self.connect()
            self.log("Requesting security access (read)...")
            self._lvl3_auth()
        self._establish_session(0x86, speed, allow_fast, _prepare, is_stop)
        time.sleep(0.075)

        image = bytearray(b"\xFF" * CAL_START)
        chunk = upload_chunk or CAL_SIZE
        addrs = list(range(CAL_START, FULL_SIZE, chunk))
        for ci, addr in enumerate(addrs):
            size = min(chunk, FULL_SIZE - addr)
            if on_stage:
                if len(addrs) > 1:
                    on_stage("Reading 0x%06X-0x%06X (%d/%d)..." %
                             (addr, addr + size - 1, ci + 1, len(addrs)))
                else:
                    on_stage("Reading calibration (0x180000-0x1FFFFF)...")
            self._request_upload(addr, size)
            done = [False]
            region = bytearray()
            while not done[0]:
                if is_stop and is_stop():
                    self._try_quiet_stop()
                    raise StopRequested("read stopped")
                region.extend(self._read_flash_packet(done))
                pct = min(100, (len(image) - CAL_START + len(region)) * 100 // CAL_SIZE)
                if on_percent:
                    on_percent(pct)
            image.extend(region)
            if upload_chunk:                    # per-chunk transferExit, like MPPS
                try:
                    self._request_transfer_exit()
                except KLineError as ex:
                    self.log("(transferExit after chunk not clean: %s)" % ex)
        # pad to full 2 MB (final packet may leave us short/over the exact size)
        if len(image) < FULL_SIZE:
            image.extend(b"\xFF" * (FULL_SIZE - len(image)))
        del image[FULL_SIZE:]
        try:
            self._close_ecu()
        except Exception as ex:
            self.log("(read complete; stopComm not clean: %s)" % ex)
        return bytes(image)

    def _read_flash_packet(self, done):
        if not self._fast:
            time.sleep(0.015)
        self.k.write_bytes(bytes([0x01, 0x36, 0x37]))
        fmt = self.k.read_byte()
        length = fmt & 0x3F
        if length == 0:
            length = self.k.read_byte()
            self._check_rec(0x76)
            header = bytes([fmt, length, 0x76])
        else:
            self._check_rec(0x76)
            header = bytes([fmt, 0x76])
        if length != 0xFF:
            done[0] = True
        data_len = max(0, length - 1)
        data = self.k.read(data_len) if data_len else b""
        cksb = self.k.read_byte()
        if cksb != ((_cks(data) + _cks(header)) & 0xFF):
            raise KLineError("EDC16 read: packet checksum mismatch")
        return data

    # ---- write ----
    def _end_fast_session(self, is_stop, teardown=True):
        """Return the ECU to bus-idle after a fast-init prime so a fresh slow init
        is accepted.  ``teardown``: send StopCommunication (0x82 -> C2) then wait a
        short bus-idle (fast, clean); otherwise wait out the fast session's P3max
        inactivity timeout (legacy).  Always drops us back to 10400 slow mode."""
        self._fast = False
        try:
            self.k.set_baud(10400)
        except Exception:
            pass
        if teardown:
            self.log("Ending fast session (StopCommunication 0x82)...")
            self._try_quiet_stop()                      # 0x82 -> C2, tolerant of no reply
            self._settle(_TEARDOWN_IDLE_S, is_stop)
        else:
            self.log("Waiting out fast session P3max timeout...")
            self._settle(_P3MAX_SETTLE_S, is_stop)

    def _open_kline_write_session(self, speed, allow_fast, fast_init_prime, is_stop, on_stage,
                                  prime_teardown=True, prime_trim=False):
        """Open the EDC16 DOWNLOAD (0x85) programming session over K-line.

        The 0x85 session is granted only through a SLOW-init session, so slow init is
        the default write path (proven on K-line EDC16s).  A later CAN-init EDC16 that
        ignores a cold slow init needs a fast-init PRIME first: the fast-init wake
        conditions the ECU, then -- once the fast session is ended -- a fresh slow init
        is accepted and grants 0x85.  There is NO fast-init-only write: the fast 0x85
        request is refused even where no prime is needed (per the kw1281test reference).

        ``fast_init_prime`` is normally derived from the OS family by write_flash
        (P350 -> False, any P475 -> True): True = always prime first; False = slow
        only, never prime.  None = AUTO fallback for an UNRECOGNISED family only --
        try a cold slow init and prime only if the ECU ignores it.

        Prime tuning (bench-testable):
          ``prime_teardown`` True  -> end the fast session with StopCommunication and
            slow-init after a short idle, falling back to the P3max wait if that slow
            init isn't accepted.  False -> legacy blind P3max wait.
          ``prime_trim`` True -> prime with the fast StartCommunication ONLY (skip the
            erase-sig / auth / refused-0x85 round-trips); False -> full prime."""
        def _auth_and_enter():
            self._read_erase_signature()
            self.log("Requesting security access (write)...")
            self._lvl1_auth()
            self._enter_session(0x85, speed, allow_fast)

        if fast_init_prime is None:
            # AUTO: a cold slow init that wakes the ECU means it accepts slow init (no
            # prime).  If it does not wake, this is a CAN-init ECU -> prime it.
            try:
                self._connect_slow()
                _auth_and_enter()
                return
            except KLineError:
                self.log("Cold slow init not accepted; ECU needs a fast-init prime.")
                need_prime = True
        else:
            need_prime = bool(fast_init_prime)

        if need_prime and self._try_connect_fast():
            if on_stage:
                on_stage("Priming ECU with fast init...")
            if prime_trim:
                # The fast StartCommunication alone may be enough to wake the K-line
                # stack; skip the wasted erase-sig / auth / refused-0x85 round-trips.
                self.log("Trimmed prime: fast StartCommunication only.")
            else:
                self._read_erase_signature()
                self.log("Requesting security access (write, prime)...")
                self._lvl1_auth()
                try:
                    self._enter_session(0x85, speed, allow_fast, slow_fallback=False)
                    return                              # rare: ECU granted 0x85 over fast init
                except KLineError:
                    self.log("Fast-init 0x85 refused (expected).")
            # End the fast session, then reconnect with slow init.
            self._end_fast_session(is_stop, teardown=prime_teardown)
            if on_stage:
                on_stage("Reconnecting with slow init...")
            if prime_teardown:
                # Fast path: after an explicit teardown the slow init should take
                # immediately.  If it doesn't, wait out P3max and retry below.
                try:
                    self._connect_slow()
                    _auth_and_enter()
                    return
                except KLineError:
                    self.log("Slow init after teardown refused; waiting P3max and retrying...")
                    self._settle(_P3MAX_SETTLE_S, is_stop)

        # Slow (re)connect grants 0x85 -- after the prime, the CAN-init ECU now accepts it.
        self._connect_slow()
        _auth_and_enter()

    def write_flash(self, image, on_percent=None, on_stage=None, is_stop=None,
                    force_full=False, speed="Medium", allow_fast=True,
                    fast_init_prime="auto", prime_teardown=None, prime_trim=None):
        if len(image) < FULL_SIZE:
            raise ValueError("EDC16 write needs a full 2 MB image")
        # Prime tuning (bench-testable); None -> module defaults so a bench run can
        # flip _PRIME_TEARDOWN / _PRIME_TRIM without touching callers.
        if prime_teardown is None:
            prime_teardown = _PRIME_TEARDOWN
        if prime_trim is None:
            prime_trim = _PRIME_TRIM
        # Binary-driven init strategy: the OS *family* decides slow-vs-fast, and
        # the app identifies it structurally from the image (no DAMOS, no part
        # number).  Callers may override with an explicit True/False/None.
        if fast_init_prime == "auto":
            fast_init_prime = fast_prime_from_image(image)
        # Encrypt both blocks BEFORE opening the session.  Encryption is pure
        # local CPU (~1-2 s) and needs no ECU data; doing it after _enter_session
        # left the TP 2.0 channel idle that long with nothing servicing it, and
        # the ECU dropped the connection (Writeattempt.log: A8 after a 1.5 s gap
        # between the session response and requestDownload).  K-line tolerated the
        # same pause because accessTimingParameter (0x83) had stretched P3max --
        # which we (correctly) don't send over CAN, so the channel must simply
        # never sit idle across a long local step.
        if on_stage:
            on_stage("Encrypting...")
        block7, cks7 = encrypt(image, 0x1C0000, 0x1FE000)
        block6, cks6 = encrypt(image, 0x180000, 0x1C0000)

        if on_stage:
            on_stage("Connecting...")

        if getattr(self.k, "is_tp20", False):
            # CAN/TP2.0 write path -- unchanged (read path also unchanged).  Over CAN,
            # entering the programming session (10 85) RESETS the ECU's transport AND
            # security, so after 50 85 00 MPPS tears the channel down (A8), re-establishes
            # it (0x200 setup + A0/A1) and re-authenticates before any programming command.
            def _prepare():
                self.connect()
                self._read_erase_signature()
                self.log("Requesting security access (write)...")
                self._lvl1_auth()
            self._establish_session(0x85, speed, allow_fast, _prepare, is_stop)
            self.log("Re-establishing channel + security (programming session)...")
            reset = getattr(self.k, "reset_channel", None)
            if reset is not None:
                reset()
            self._lvl1_auth()
        else:
            # K-line write: init strategy chosen from the OS family (above).
            self.log("Init strategy: %s" % (
                "slow init (family accepts it)" if fast_init_prime is False
                else "fast-init prime (later CAN-init family)" if fast_init_prime is True
                else "auto (unknown family): slow init, prime only if ignored"))
            self._open_kline_write_session(speed, allow_fast, fast_init_prime, is_stop, on_stage,
                                           prime_teardown=prime_teardown, prime_trim=prime_trim)
            self._set_access_timing()

        if is_stop and is_stop():
            self._finalize()
            raise StopRequested("stopped before block 7")
        self._write_block(7, block7, cks7, force_full, on_stage, on_percent)
        if is_stop and is_stop():
            self._finalize()
            raise StopRequested("stopped after block 7")
        self._write_block(6, block6, cks6, force_full, on_stage, on_percent)
        self._finalize()
        if on_stage:
            on_stage("Done.")

    def _write_block(self, blockno, enc, checksum, force_full, on_stage, on_percent):
        start, cks_end, erase_end, dl_size = _BLOCK[blockno]
        if not force_full and self._block_matches(start, cks_end, checksum):
            if on_stage:
                on_stage("Block %d unchanged (checksum match) - skipped." % blockno)
            if on_percent:
                on_percent(100)
            return
        self._request_download(start, dl_size)
        if on_stage:
            on_stage("Erasing block %d..." % blockno)
        self._erase_block(start, erase_end)
        if on_stage:
            on_stage("Writing block %d..." % blockno)
        self._transfer_data(enc, on_percent)
        self._request_transfer_exit()
        self._verify_block(start, cks_end, checksum)
        self.log("Block %d written and verified." % blockno)

    def _send_checksum_routine(self, start, end, checksum):
        time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x0A, 0x31, 0xC5,
                          (start >> 16) & 0xFF, (start >> 8) & 0xFF, start & 0xFF,
                          (end >> 16) & 0xFF, (end >> 8) & 0xFF, end & 0xFF,
                          (checksum >> 8) & 0xFF, checksum & 0xFF]))

    def _poll_checksum(self):
        # 0x31 C5 STARTS a checksum routine; the verdict comes from polling 0x33 C5
        # -> 73 C5 <status> (status 0 = match, nonzero = mismatch).  ODIS polls this
        # after EVERY block (capture 2026-09-24: 31 C5 -> 71 C5 -> 33 C5 -> 73 C5 00),
        # and it answered on the first poll.  7F 33 22/23 = still computing; silence
        # (KLineTimeout on the coupled transport) = ECU busy -> keep polling.  The
        # earlier "this ECU NAKs 33 C5 forever" belief (bench 2026-09-23) was wrong
        # -- it polled without a confirmed 71 C5 start / at bad timing.
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(0.05)
            try:
                self._send(bytes([0x02, 0x33, 0xC5]))
                r = self._read_response()
            except KLineTimeout:
                continue                                 # ECU busy computing -- keep polling
            idx = _index_of_pair(r, 0x73, 0xC5)
            if idx >= 0:
                return "mismatch" if (idx + 2 < len(r) and r[idx + 2] != 0) else "match"
            if _contains(r, 0x7F, 0x33, 0x40):
                return "mismatch"
            # 7F 33 22/23 (routine in progress) or any other NAK -> keep polling
        return "unconfirmed"

    def _block_matches(self, start, end, checksum):
        self._send_checksum_routine(start, end, checksum)
        r = self._read_response()
        if _contains(r, 0x7F, 0x31) and not _contains(r, 0x7F, 0x31, 0x78):
            return False                                 # routine rejected
        return self._poll_checksum() == "match"

    def _verify_block(self, start, end, checksum):
        # 0x31 C5 STARTS a checksum routine -- 71 C5 means "started", NOT the
        # verdict.  The verdict comes from polling 0x33 C5 -> 73 C5 <status>, which
        # ODIS does after every block (capture 2026-09-24).  Polling is REQUIRED,
        # not optional: the ECU allows one active routine at a time, so the C5
        # routine MUST be drained (33 C5 -> 73 C5) before the next block'"'"'s 31 C4
        # erase.  The old code returned at 71 C5 without polling, leaving the routine
        # active; the following erase then collided with it and the ECU went dead
        # silent for 180 s (bench 2026-09-24, boot -> program).
        self._send_checksum_routine(start, end, checksum)
        r = self._read_response()
        if _contains(r, 0x7F, 0x31) and not _contains(r, 0x7F, 0x31, 0x78):
            self.log("WARNING: post-write checksum verify REJECTED at 0x%06X: %s "
                     "-- read back before trusting this write." % (start, r.hex()))
            return
        res = self._poll_checksum()
        if res == "match":
            return                                       # verified OK
        if res == "mismatch":
            self.log("WARNING: post-write checksum MISMATCH at 0x%06X; read back "
                     "before trusting this write." % start)
        else:
            self.log("Note: checksum verify unconfirmed at 0x%06X (routine did not "
                     "return 73 C5); write completed." % start)

    def _checksum_probe(self, start, end, checksum, log):
        """VERBOSE 0x31 C5 check: logs the raw routine ack + each 0x33 C5 result
        frame + the decoded status, and returns 'match'/'mismatch'/'timeout'.
        Used to see EXACTLY what the ECU returns for a checksum query (e.g. why a
        pre-write skip check does or doesn't match)."""
        self._send_checksum_routine(start, end, checksum)
        ack = self._read_response()
        log("  0x31 C5 @0x%06X..0x%06X expect=0x%04X  ack=%s"
            % (start, end, checksum, ack.hex() or "(none)"))
        deadline = time.time() + 8
        last = None
        reps = 0
        naks = 0
        def flush():
            if last is not None and reps > 1:
                log("  0x33 C5 (poll) -> %s  (x%d)" % (last, reps))
        while time.time() < deadline:
            time.sleep(0.04)
            self._send(bytes([0x02, 0x33, 0xC5]))
            r = self._read_response()
            idx = _index_of_pair(r, 0x73, 0xC5)
            if idx >= 0:
                flush()
                status = r[idx + 2] if idx + 2 < len(r) else None
                log("  0x33 C5 result=%s  (73 C5 status=%s)"
                    % (r.hex(), "0x%02X" % status if status is not None else "?"))
                return "match" if status == 0 else "mismatch"
            if _contains(r, 0x7F, 0x33, 0x40):
                flush()
                log("  0x33 C5 -> 7F 33 40 (verdict/deny): %s" % r.hex())
                return "mismatch"
            h = r.hex()
            if h == last:
                reps += 1
            else:
                flush(); last = h; reps = 1
                if h:
                    log("  0x33 C5 (poll) -> %s" % h)
            if _contains(r, 0x7F, 0x33):
                naks += 1
                if naks >= 40:
                    flush()
                    log("  0x33 C5 -> ECU keeps NAKing the results poll (routine "
                        "never returns 73 C5) -> UNCONFIRMED.")
                    return "unconfirmed"
        flush()
        log("  0x33 C5 -> timeout (no 73 C5) -> UNCONFIRMED.")
        return "unconfirmed"

    def _seed_probe_one(self, subfunc):
        """Request a SecurityAccess seed for <subfunc> (0x27 <sf>) WITHOUT sending a
        key -- non-destructive security-level enumeration.  Classify:
          'seed:<hex>'   -> 67 <sf> <seed>: level EXISTS (nonzero seed returned)
          'open:<hex>'   -> 67 <sf> 00 00 : already unlocked / no seed needed
          'nak:7F 27 XX' -> refused (XX=12 subFunctionNotSupported, 11, 22, 35, ...)
          'none:<hex>'   -> unexpected/blank response
        No key is ever sent, so nothing unlocks and nothing is written."""
        subfunc &= 0xFF
        time.sleep(0.03)
        self.k.clear_rx()
        try:
            self._send(bytes([0x82, 0x10, 0xF1, 0x27, subfunc]))
            r = self._read_response()
        except KLineTimeout:
            return "none:timeout"
        i = _index_of_pair(r, 0x67, subfunc)
        if i >= 0:
            seed = r[i + 2:i + 6]
            if seed and any(seed):
                return "seed:" + seed.hex()
            return "open:" + (seed.hex() or "0000")
        j = _index_of_pair(r, 0x7F, 0x27)
        if j >= 0:
            nrc = r[j + 2] if j + 2 < len(r) else 0
            return "nak:7F 27 %02X" % nrc
        return "none:" + (r.hex() or "(no response)")

    def _checksum_probe_classify(self, start, end, checksum=0):
        """Send 0x31 C5 <start> <end> <checksum> and poll 0x33 C5, classifying the
        RANGE (independent of the checksum value):
          'accepted:match'      -> 73 C5 00
          'accepted:mismatch:XX'-> 73 C5 XX (valid range, computed; value differs)
          'rejected:7F 31 XX'   -> the routine start itself was refused
          'rejected:7F 33 22'   -> conditionsNotCorrect: NOT a valid checksum range
          'rejected:7F 33 40'   -> deny verdict
          'unconfirmed'         -> neither within the deadline
        Distinguishes 7F 33 23 (routineNotComplete = still computing -> keep
        polling) from 7F 33 22 (rejected -> return).  Non-destructive: 0x31 C5 only
        computes/compares a sum; it erases and writes nothing."""
        self._send_checksum_routine(start, end, checksum)
        ack = self._read_response()
        if _contains(ack, 0x7F, 0x31) and not _contains(ack, 0x7F, 0x31, 0x78):
            i = _index_of_pair(ack, 0x7F, 0x31)
            nrc = ack[i + 2] if i >= 0 and i + 2 < len(ack) else 0
            return "rejected:7F 31 %02X" % nrc
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.05)
            try:
                self._send(bytes([0x02, 0x33, 0xC5]))
                r = self._read_response()
            except KLineTimeout:
                continue
            idx = _index_of_pair(r, 0x73, 0xC5)
            if idx >= 0:
                st = r[idx + 2] if idx + 2 < len(r) else None
                return "accepted:match" if st == 0 else "accepted:mismatch:%02X" % (st or 0)
            j = _index_of_pair(r, 0x7F, 0x33)
            if j >= 0 and j + 2 < len(r):
                nrc = r[j + 2]
                if nrc == 0x23:
                    continue                      # routineNotComplete -> still computing
                if nrc == 0x22:
                    return "rejected:7F 33 22"    # conditionsNotCorrect -> not a valid range
                if nrc == 0x40:
                    return "rejected:7F 33 40"
                # any other NRC: keep polling a little, it may resolve
        return "unconfirmed"

    def _request_download(self, start, size):
        time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x08, 0x34,
                          (start >> 16) & 0xFF, (start >> 8) & 0xFF, start & 0xFF,
                          0x02,
                          (size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF]))
        r = self._read_response()
        if _contains(r, 0x7F, 0x34) or not _contains(r, 0x74):
            raise KLineError("EDC16 requestDownload not accepted: %s" % r.hex())
        # Honour the max TransferData block length the ECU declares in the byte
        # after the 0x74 positive response (e.g. "74 FD" -> 0xFD).  Bosch
        # internal-flash (MPC) loaders require every 0x36 block to EQUAL this max;
        # a short block is rejected with NRC 0x74 (illegalByteCount).  ODIS sends
        # 0xFD-sized (252-data) blocks for every region incl. the MPC at 0x404000;
        # our old fixed 0xF8 (248) worked on the lenient program loader but was
        # refused by the MPC loader.  Fall back to the module default if absent.
        _i = bytes(r).find(b"\x74")
        self._max_block_len = (r[_i + 1] if 0 <= _i < len(r) - 1 and r[_i + 1]
                               else 0)

    def _erase_block(self, start, erase_end):
        time.sleep(0.03)
        self.k.clear_rx()
        s = self._erase_sig
        erase_cmd = (bytes([0x0E, 0x31, 0xC4,
                            (start >> 16) & 0xFF, (start >> 8) & 0xFF, start & 0xFF,
                            (erase_end >> 16) & 0xFF, (erase_end >> 8) & 0xFF,
                            erase_end & 0xFF]) + s)
        # IMPORTANT: this transport COUPLES send+receive -- self._send() writes the
        # frame and then blocks in _recv_message() for the ECU's reply, raising
        # KLineTimeout (~8 s) if none arrives.  During a LARGE erase the ECU can go
        # silent for far longer than 8 s: both the 0x31 C4 start-ack (0x71 C4) and
        # the 0x33 C4 progress polls.  The program block (0x140000) after a boot
        # write did exactly this -- the ECU answered neither the start-ack nor the
        # first poll within 8 s -- and because the erase exchanges were not guarded,
        # KLineTimeout propagated and tore the session down mid-erase.  So EVERY
        # erase exchange tolerates KLineTimeout and simply re-polls.  (Boot never
        # tripped this only because it answered every poll inside the window.)
        try:
            self._send(erase_cmd)                 # 0x31 C4; reply (0x71 C4) captured here
            r = self._read_response()
        except KLineTimeout:
            r = b""                               # start-ack slow/silent -> poll instead
        if _contains(r, 0x7F, 0x31) and not _contains(r, 0x7F, 0x31, 0x78):
            raise KLineError("EDC16 erase rejected: %s" % r.hex())
        # Completion poll (0x33 C4 -> 0x73 C4 when done, 7F 33 23 while in progress,
        # SILENCE while the ECU is too busy to answer).  Worst case from the ODIS
        # capture is the boot erase at ~61 s; the oversized program erase runs long
        # too.  Keep polling through KLineTimeouts until 0x73 C4 or the deadline;
        # each poll keeps the session alive when the ECU is responsive, and a
        # generous ceiling only bounds a genuinely hung erase.
        ERASE_TIMEOUT_S = 180
        deadline = time.time() + ERASE_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.1)
            try:
                self._send(bytes([0x02, 0x33, 0xC4]))
                rr = self._read_response()
            except KLineTimeout:
                continue                          # ECU busy erasing -- not answering yet
            if _contains(rr, 0x73, 0xC4):
                return
        raise KLineError("EDC16 erase did not complete in %ds" % ERASE_TIMEOUT_S)

    def _read_download_ack(self, sid=0x36):
        """Read one 00-header response frame to a download-session request and
        raise a DECODED negative response on a NAK.

        Positive TransferData ack is ``00 01 76 77`` (fmt, len, 0x76, cks); a NAK
        is ``00 03 7F 36 <NRC> <cks>``.  The old byte-by-byte check died at the
        length byte ("expected 0x01, got 0x03") and threw away the NRC -- which is
        the byte that says why the ECU rejected the block."""
        pos_sid = (sid + 0x40) & 0xFF
        deadline = time.time() + 5.0
        while True:
            fmt = self.k.read_byte()
            if fmt != 0x00:
                raise KLineError(
                    "EDC16 transferData: bad response format 0x%02X" % fmt)
            length = self.k.read_byte()
            payload = self.k.read(length)
            _cksb = self.k.read_byte()               # frame checksum (not re-verified)
            if len(payload) >= 1 and payload[0] == pos_sid:
                return                               # positive ack
            if len(payload) >= 3 and payload[0] == 0x7F and payload[1] == sid:
                nrc = payload[2]
                if nrc == 0x78 and time.time() < deadline:
                    time.sleep(0.02)                 # responsePending -> wait for real
                    continue
                raise KLineError(
                    "EDC16 transferData rejected: 7F 36 %02X (%s)"
                    % (nrc, _nrc_name(nrc)))
            raise KLineError(
                "EDC16 transferData: unexpected response %s" % payload.hex())

    def _transfer_data(self, data, on_percent):
        pos = 0
        last = -1
        n = len(data)
        # Data bytes per 0x36 frame = (ECU max block length) - 1 (the 0x36 SID).
        # The ECU set _max_block_len from the preceding 0x34 response; match it
        # exactly (ODIS-identical), falling back to the module default. All blocks
        # are full-size except the final remainder.
        maxlen = self._max_block_len or (_TRANSFER_CHUNK + 1)
        data_per = max(1, maxlen - 1)
        while pos < n:
            chunk = data[pos:pos + data_per]
            frame = bytes([0x00, len(chunk) + 1, 0x36]) + chunk
            frame += bytes([_cks(frame)])
            self.k.write_bytes(frame)
            self._read_download_ack(0x36)
            pos += len(chunk)
            pct = pos * 100 // n if n else 100
            if on_percent and pct != last:
                on_percent(pct)
                last = pct

    def _request_transfer_exit(self):
        time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x01, 0x37]))
        r = self._read_response()
        if _contains(r, 0x7F, 0x37) or not _contains(r, 0x77):
            raise KLineError("EDC16 transferExit not accepted: %s" % r.hex())

    def _set_access_timing(self):
        time.sleep(0.03)
        self.k.clear_rx()
        self._send(bytes([0x02, 0x83, 0x00]))
        self._read_response()
        time.sleep(0.03)
        # ODIS-observed setParameters values for this family (was 00 FF 00 FF 00).
        self._send(bytes([0x07, 0x83, 0x03, 0x00, 0x38, 0x00, 0x14, 0x00]))
        r = self._read_response()
        if not _contains(r, 0xC3, 0x03):
            self.log("(accessTimingParameter not acked as expected; continuing.)")

    def _finalize(self):
        self._fast = self._fast  # keep speed for teardown pacing
        try:
            time.sleep(0.05)
            self.k.clear_rx()
            self._send(bytes([0x02, 0x11, 0x01]))   # ecuReset (ECU NAKs 7F 11 90)
            self._read_response()
            time.sleep(0.03)
            self._send(bytes([0x01, 0x82]))         # stopCommunication
            self._read_response()
        except Exception as ex:
            self.log("(write complete; teardown not clean: %s)" % ex)

    def _close_ecu(self):
        time.sleep(0.01)
        self._send(bytes([0x81, 0x10, 0xF1, 0x82]))
        for exp in (0x81, 0xF1, 0x10, 0xC2, 0x44):
            self._check_rec(exp)

    def _try_quiet_stop(self):
        try:
            self._close_ecu()
        except Exception:
            pass
