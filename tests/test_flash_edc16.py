# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""End-to-end EDC16 read/write state-machine tests against a mock ECU.

The mock speaks the exact address-less EDC16 wire protocol (security addressed,
transfer address-less), holds a 512 KB calibration flash, and decrypts the
transferData stream with the same keystream the port encrypts with -- so a
write-then-read round-trips the calibration bytes, exercising every frame the
port emits and every response it parses, with no hardware.
"""
import os
import pytest

from ovtsuite.flash import edc16 as E
from ovtsuite.flash.crypto import keystream
from ovtsuite.flash.transport import KLineTimeout

#: Slow: these drive the real protocol timing, including its waits.
pytestmark = pytest.mark.slow



class MockEcu:
    """A fake serial port + EDC16 ECU behind it (half-duplex: writes are echoed;
    complete requests trigger responses)."""

    def __init__(self, cal=None):
        self.rx = bytearray()          # bytes the tester will read
        self.parse = bytearray()       # accumulated request bytes (post-echo)
        self.baudrate = 10400
        self.timeout = 8.0
        self.dtr = False
        self.rts = False
        self.break_condition = False
        self._broke = False
        self.flash = bytearray(cal if cal is not None else b"\x11" * E.CAL_SIZE)
        self._dl_start = None
        self._dl_pos = 0
        self.corrupt = []              # absolute addresses of packets sent damaged

    # pyserial-ish surface
    def flush(self):
        pass

    def close(self):
        pass

    def reset_input_buffer(self):
        # after a 5-baud bit-bang, the wakeup clears rx then reads the keyword
        self.rx.clear()
        if self._broke:
            self._broke = False
            self.rx += bytes([0x55, 0xEF, 0x8F])   # sync + KW 2031 (EF 8F)

    def __setattr__(self, k, v):
        if k == "break_condition" and v:
            object.__setattr__(self, "_broke", True)
        object.__setattr__(self, k, v)

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.rx += data                # echo (half-duplex)
        self.parse += data
        self._consume()

    # ---- request recognition ----
    def _respond(self, frame_wo_cks):
        f = bytes(frame_wo_cks)
        s = 0
        for b in f:
            s = (s + b) & 0xFF
        self.rx += f + bytes([s])

    def _consume(self):
        p = self.parse
        # keyword-msb complement (0x70) during wakeup -> send addr complement 0xFE
        if len(p) == 1 and p[0] == 0x70:
            self.rx += bytes([0xFE])
            p.clear(); return
        # try to match complete frames by scanning; requests here are short.
        b = bytes(p)

        def took(n):
            del p[:n]

        # security (addressed)
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x27, 0x03]) and len(b) >= 6:  # LVL3
            seed = bytes([0x83, 0xF1, 0x10, 0x67, 0x03, 0x00, 0x01, 0x00, 0x02])
            self.rx += seed + bytes([_ck(seed)]); took(6); return
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x27, 0x01]) and len(b) >= 6:  # LVL1
            seed = bytes([0x83, 0xF1, 0x10, 0x67, 0x01, 0xAC, 0x7B, 0x56, 0xD1])
            self.rx += seed + bytes([_ck(seed)]); took(6); return
        if b[:5] == bytes([0x86, 0x10, 0xF1, 0x27, 0x04]) and len(b) >= 10:  # LVL3 key
            self._respond([0x83, 0xF1, 0x10, 0x67, 0x04, 0x34]); took(10); return
        if b[:5] == bytes([0x86, 0x10, 0xF1, 0x27, 0x02]) and len(b) >= 10:  # LVL1 key
            self._respond([0x83, 0xF1, 0x10, 0x67, 0x02, 0x34]); took(10); return
        # 1A 9C erase-sig ident -- checksum-valid addressed frame (8C F1 10 + 12
        # data + cks): 5A 9C at data[0], erase sig 0C 5E CF CF 65 0B at data[6:12]
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C]) and len(b) >= 6:
            self._respond([0x8C, 0xF1, 0x10,
                           0x5A, 0x9C, 0x00, 0x28, 0x21, 0x00,
                           0x0C, 0x5E, 0xCF, 0xCF, 0x65, 0x0B]); took(6); return
        # 1A 9B identification: this ECU does not offer it (7F 1A 12)
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9B]) and len(b) >= 6:
            self._respond([0x83, 0xF1, 0x10, 0x7F, 0x1A, 0x12]); took(6); return
        # session enter: 82 10 F1 10 <sub> (+cks) or 83 10 F1 10 <sub> <speed> (+cks)
        if b[:4] == bytes([0x82, 0x10, 0xF1, 0x10]) and len(b) >= 6:
            sub = b[4]
            self._respond([0x83, 0xF1, 0x10, 0x50, sub, 0x00]); took(6); return
        if b[:4] == bytes([0x83, 0x10, 0xF1, 0x10]) and len(b) >= 7:
            # refuse fast baud (no ack) so the port falls back to 10400 at our rate
            took(7); return
        # requestUpload (read): 08 35 18 00 00 00 08 00 00 (+cks)
        if b[:2] == bytes([0x08, 0x35]) and len(b) >= 10:
            self._respond([0x02, 0x75, 0xFF]); took(10)
            self._up_pos = ((b[2] << 16) | (b[3] << 8) | b[4]) - E.CAL_START
            self._up_end = self._up_pos + ((b[6] << 16) | (b[7] << 8) | b[8])
            return
        # transferData read request: 01 36 37 -> emit exactly ONE packet
        if b[:3] == bytes([0x01, 0x36, 0x37]):
            self._emit_read_packet(); took(3); return
        # accessTiming
        if b[:3] == bytes([0x02, 0x83, 0x00]) and len(b) >= 4:
            self._respond([0xC3, 0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]); took(4); return
        if b[:2] == bytes([0x07, 0x83]) and len(b) >= 9:
            self._respond([0xC3, 0x03]); took(9); return
        # 31 C5 checksum routine: 0A 31 C5 s s s e e e c c (+cks)
        if b[:3] == bytes([0x0A, 0x31, 0xC5]) and len(b) >= 12:
            start = (b[3] << 16) | (b[4] << 8) | b[5]
            want = (b[9] << 8) | b[10]
            self._pending_c5 = (start, want)
            self._respond([0x71, 0xC5]); took(12); return
        if b[:3] == bytes([0x02, 0x33, 0xC5]) and len(b) >= 4:
            start, want = getattr(self, "_pending_c5", (0, 0))
            cur = _sum16(self._plain_of(start))
            self._respond([0x73, 0xC5, 0x00] if cur == want else [0x7F, 0x33, 0x40])
            took(4); return
        # requestDownload: 08 34 s s s 02 z z z (+cks)
        if b[:2] == bytes([0x08, 0x34]) and len(b) >= 10:
            self._dl_start = (b[2] << 16) | (b[3] << 8) | b[4]
            self._dl_pos = 0
            dl_size = (b[6] << 16) | (b[7] << 8) | b[8]
            self._dl_ks = keystream(0, dl_size)       # continuous block keystream
            self._respond([0x74, 0xFD]); took(10); return
        # erase: 0E 31 C4 s s s e e e sig(6) (+cks)
        if b[:3] == bytes([0x0E, 0x31, 0xC4]) and len(b) >= 16:
            s = self._dl_start
            for i in range(min(self._blocklen(s), E.CAL_SIZE)):
                self.flash[(s - E.CAL_START) + i] = 0xFF
            self._respond([0x71, 0xC4]); took(16); return
        if b[:3] == bytes([0x02, 0x33, 0xC4]) and len(b) >= 4:
            self._respond([0x73, 0xC4]); took(4); return
        # transferData write: 00 <len> 36 <data...> <cks>
        if b[:1] == bytes([0x00]) and len(b) >= 2 and b[1] not in (0x01,) and len(b) >= 3 + (b[1] - 1) + 1 and b[2] == 0x36:
            dlen = b[1] - 1
            data = b[3:3 + dlen]
            self._apply_download(data)
            self._respond_raw([0x00, 0x01, 0x76, 0x77]); took(3 + dlen + 1); return
        # transferExit: 01 37
        if b[:2] == bytes([0x01, 0x37]) and len(b) >= 3:
            self._respond([0x77]); took(3); return
        # finalize: 02 11 01 / 01 82
        if b[:3] == bytes([0x02, 0x11, 0x01]) and len(b) >= 4:
            self._respond([0x7F, 0x11, 0x90]); took(4); return
        if b[:2] == bytes([0x01, 0x82]) and len(b) >= 3:
            self._respond([0xC2]); took(3); return
        # close (read): 81 10 F1 82
        if b[:4] == bytes([0x81, 0x10, 0xF1, 0x82]) and len(b) >= 5:
            self.rx += bytes([0x81, 0xF1, 0x10, 0xC2, 0x44]); took(5); return

    # ---- helpers ----
    def _respond_raw(self, seq):
        self.rx += bytes(seq)

    def _blocklen(self, start):
        return E._BLOCK[7][3] if start == 0x1C0000 else E._BLOCK[6][3]

    def _plain_of(self, start):
        off = start - E.CAL_START
        end = 0x1FE000 - E.CAL_START if start == 0x1C0000 else 0x1C0000 - E.CAL_START
        return bytes(self.flash[off:end])

    def _apply_download(self, cipher):
        ks = self._dl_ks[self._dl_pos:self._dl_pos + len(cipher)]
        plain = bytes(c ^ k for c, k in zip(cipher, ks))
        off = (self._dl_start - E.CAL_START) + self._dl_pos
        self.flash[off:off + len(plain)] = plain
        self._dl_pos += len(cipher)

    def _emit_read_packet(self):
        # one packet per 01 36 request: full = 00 FF 76 <254> cks; final short =
        # LEN 76 <data> cks (length in the format byte).
        data = bytes(self.flash[:self._up_end])
        pos = self._up_pos
        chunk = data[pos:pos + 254]
        self._up_pos += len(chunk)
        full = len(chunk) == 254 and self._up_pos < len(data)
        if full:
            header = bytes([0x00, 0xFF, 0x76])
        else:
            header = bytes([len(chunk) + 1, 0x76])       # counts the SID
        cks = (_ck(chunk) + _ck(header)) & 0xFF
        if self.corrupt and self.corrupt[0] == pos + E.CAL_START:
            # the line drops a bit: one data byte arrives wrong
            self.corrupt.pop(0)
            chunk = bytes([chunk[0] ^ 0x10]) + chunk[1:]
        self.rx += header + chunk + bytes([cks])


def _ck(data):
    s = 0
    for b in data:
        s = (s + b) & 0xFF
    return s


def _sum16(data):
    return sum(data) & 0xFFFF


class _Link:
    """Minimal KLine-compatible wrapper over MockEcu for the port to drive."""
    def __init__(self, ecu):
        self.port = ecu
        self._t = 8.0
        self.last_sync = -1

    @property
    def read_timeout(self):
        return self._t

    @read_timeout.setter
    def read_timeout(self, v):
        self._t = v

    def set_baud(self, b):
        self.port.baudrate = b

    def set_break(self, on):
        self.port.break_condition = on

    def clear_rx(self):
        self.port.reset_input_buffer()

    def read_byte(self):
        b = self.port.read(1)
        if not b:
            raise KLineTimeout("timeout")
        return b[0]

    def read(self, n):
        out = bytearray()
        while len(out) < n:
            c = self.port.read(n - len(out))
            if not c:
                raise KLineTimeout("timeout")
            out += c
        return bytes(out)

    def write_raw(self, data):
        self.port.write(data)

    def write_byte(self, b):
        self.port.write(bytes([b]))
        self.port.read(1)               # echo

    def write_bytes(self, data):
        self.port.write(data)
        self.port.read(len(data))       # echoes

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        # mimic KLine.wake_up: bit-bang (just toggle break), clear, read keyword
        self.set_break(False)
        self.set_break(True)            # marks _broke
        self.clear_rx()                 # seeds sync+keyword
        sync = self.read_byte()
        self.last_sync = sync
        assert sync == 0x55
        lsb = self.read_byte(); msb = self.read_byte()
        version = ((msb & 0x7F) << 7) + (lsb & 0x7F)
        self.write_byte((~msb) & 0xFF)
        if version >= 2000:
            from ovtsuite.flash.transport import adjust_parity
            comp = self.port.read(1)
            assert comp and comp[0] == ((~adjust_parity(address, even_parity)) & 0xFF)
        return version


def _make(cal=None):
    ecu = MockEcu(cal)
    return E.Edc16Flash(_Link(ecu)), ecu


def test_read_roundtrip_padded_image():
    cal = bytes(((i * 7 + 3) & 0xFF) for i in range(E.CAL_SIZE))
    flash, ecu = _make(bytearray(cal))
    img = flash.read_flash(speed="Low", allow_fast=False)
    assert len(img) == E.FULL_SIZE
    assert img[:E.CAL_START] == b"\xFF" * E.CAL_START      # 0xFF pad
    assert img[E.CAL_START:] == cal                        # cal region intact


def test_read_rereads_corrupt_packets():
    # two packets arrive damaged in the single K-line upload, and the first
    # re-read of one of their 64 KB chunks is damaged again
    cal = bytes(((i * 7 + 3) & 0xFF) for i in range(E.CAL_SIZE))
    flash, ecu = _make(bytearray(cal))
    a, b = E.CAL_START + 254 * 10, E.CAL_START + 254 * 1500
    ecu.corrupt = [a, b, b - b % 0x10000 + 254 * 3]
    logs = []
    flash._log = logs.append
    img = flash.read_flash(speed="Low", allow_fast=False)
    assert img[E.CAL_START:] == cal
    assert sum("Corrupt packet" in m for m in logs) == 3
    assert not ecu.corrupt


def test_write_then_read_roundtrips():
    # start the ECU flash all-0x22; write an image whose cal region differs.
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_SIZE):
        image[E.CAL_START + i] = (i * 13 + 5) & 0xFF
    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=False)
    # the ECU flash should now equal the written cal region for the programmed
    # ranges (block6 full 0x40000, block7 0x3E000; top 8 KB stays erased 0xFF)
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])          # block6
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])  # block7


def _diff_image():
    image = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_SIZE):
        image[E.CAL_START + i] = (i * 13 + 5) & 0xFF
    return image


def test_write_prime_fast_then_slow_reconnect():
    # fast_init_prime=True: prime with a fast-init attempt whose 0x85 speed negotiation
    # is refused, wait out P3max, then reconnect SLOW and write (the CAN-init flow).
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = _diff_image()
    flash_w._settle = lambda *a, **kw: None           # skip the real P3max wait

    fast = {"n": 0}
    real_fast = flash_w._try_connect_fast
    flash_w._try_connect_fast = lambda: (fast.__setitem__("n", fast["n"] + 1) or real_fast())

    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=True,
                        fast_init_prime=True)

    assert fast["n"] == 1                              # ECU was primed with fast init once
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])          # block6 written
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])  # block7


def test_write_auto_primes_when_cold_slow_ignored():
    # AUTO (fast_init_prime=None): a CAN-init ECU ignores the cold slow init; the write
    # path detects that (slow connect raises), primes with fast init, then reconnects slow.
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = _diff_image()
    flash_w._settle = lambda *a, **kw: None

    real_connect = flash_w.connect
    conn = {"n": 0}

    def flaky_connect(*a, **kw):
        conn["n"] += 1
        if conn["n"] == 1:                            # first (cold slow) init ignored
            raise E.KLineError("cold slow init ignored (CAN-init ECU)")
        return real_connect(*a, **kw)

    flash_w.connect = flaky_connect
    fast = {"n": 0}
    real_fast = flash_w._try_connect_fast
    flash_w._try_connect_fast = lambda: (fast.__setitem__("n", fast["n"] + 1) or real_fast())

    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=True,
                        fast_init_prime=None)

    assert conn["n"] >= 3                              # cold-slow (fail) -> fast prime -> slow reconnect
    assert fast["n"] == 1                              # auto-detected the need to prime
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])


def test_write_prime_teardown_stopcomm_avoids_p3max():
    # prime_teardown=True (default): the fast session is ended with an explicit
    # StopCommunication (0x82) and the slow init follows after only a short idle --
    # the long P3max wait is NOT used.
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = _diff_image()
    settles = []
    flash_w._settle = lambda s, is_stop=None: settles.append(s)
    stop = {"n": 0}
    real_stop = flash_w._try_quiet_stop
    flash_w._try_quiet_stop = lambda: (stop.__setitem__("n", stop["n"] + 1) or real_stop())

    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=True,
                        fast_init_prime=True)              # prime_teardown defaults True

    assert stop["n"] >= 1                                  # explicit StopComm teardown happened
    assert E._TEARDOWN_IDLE_S in settles                   # short idle used
    assert E._P3MAX_SETTLE_S not in settles                # long P3max wait avoided
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])


def test_write_prime_legacy_p3max_when_teardown_off():
    # prime_teardown=False: no explicit teardown; wait out the fast session's P3max.
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = _diff_image()
    settles = []
    flash_w._settle = lambda s, is_stop=None: settles.append(s)
    stop = {"n": 0}
    real_stop = flash_w._try_quiet_stop
    flash_w._try_quiet_stop = lambda: (stop.__setitem__("n", stop["n"] + 1) or real_stop())

    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=True,
                        fast_init_prime=True, prime_teardown=False)

    assert stop["n"] == 0                                  # no explicit teardown
    assert E._P3MAX_SETTLE_S in settles                    # legacy: waited out P3max
    assert bytes(ecu.flash)[:0x40000] == bytes(image[0x180000:0x1C0000])


def test_write_prime_trim_skips_prime_auth():
    # prime_trim=True: prime with the fast StartCommunication only -- no erase-sig /
    # auth / 0x85 attempt before the teardown.  lvl1 auth then runs ONCE (the real
    # write session); the full prime would run it twice (prime + write).
    flash_w, ecu = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = _diff_image()
    flash_w._settle = lambda *a, **kw: None
    auth = {"n": 0}
    real_auth = flash_w._lvl1_auth
    flash_w._lvl1_auth = lambda: (auth.__setitem__("n", auth["n"] + 1) or real_auth())

    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=True,
                        fast_init_prime=True, prime_trim=True)

    assert auth["n"] == 1                                  # no prime auth; only the write auth
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])


def test_write_skips_unchanged_block():
    # ECU already holds exactly the image's cal -> both blocks 0x31 C5 match -> skip
    image = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_SIZE):
        image[E.CAL_START + i] = (i * 3) & 0xFF
    flash_w, ecu = _make(bytearray(image[E.CAL_START:]))
    stages = []
    flash_w.write_flash(image, on_stage=stages.append, speed="Low", allow_fast=False)
    assert any("unchanged" in s for s in stages)


class _ZeroSigEcu(MockEcu):
    """An ECU whose 1A 9C carries no erase signature (zeros at data[6:12]) and
    whose 31 C4 erase wants the six bytes of its 1A 9B identification -- the
    038906016K of Reference/Logs/SpecialEraseRejected.txt and the MPPS write
    of the same car (MPPSspecialerasefailuresuccess.log)."""

    SIG = bytes([0x02, 0x80, 0x71, 0x25, 0x38, 0x1D])
    IDENT_9B = (b"038906016K  " + b"6483" + bytes([0x03, 0x00, 0x00, 0x48]) + SIG
                + b"R4 2,0L EDC G000SG  ")

    def __init__(self, cal=None):
        super().__init__(cal)
        self.erase_sigs = []

    def _consume(self):
        b = bytes(self.parse)
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C]) and len(b) >= 6:
            self._respond([0x8C, 0xF1, 0x10, 0x5A, 0x9C] + [0x00] * 10)
            del self.parse[:6]; return
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9B]) and len(b) >= 6:
            data = bytes([0x5A, 0x9B]) + self.IDENT_9B
            self._respond([0x80, 0xF1, 0x10, len(data)] + list(data))
            del self.parse[:6]; return
        if b[:3] == bytes([0x0E, 0x31, 0xC4]) and len(b) >= 16:
            self.erase_sigs.append(b[9:15])
            if b[9:15] != self.SIG:
                self._respond([0x7F, 0x31, 0x33])          # securityAccessDenied
                del self.parse[:16]; return
        super()._consume()


def test_erase_signature_comes_from_1a_9b_when_1a_9c_has_none():
    ecu = _ZeroSigEcu(bytearray(b"\x22" * E.CAL_SIZE))
    flash = E.Edc16Flash(_Link(ecu))
    flash.write_flash(_diff_image(), force_full=True, speed="Low", allow_fast=False)
    assert ecu.erase_sigs and all(s == _ZeroSigEcu.SIG for s in ecu.erase_sigs)
    written = bytes(ecu.flash)
    assert written[0x40000:0x40000 + 0x3E000] == bytes(_diff_image()[0x1C0000:0x1FE000])


def test_erase_signature_parser_needs_the_9b_layout():
    reply = bytes([0x5A, 0x9B]) + _ZeroSigEcu.IDENT_9B
    assert E.erase_sig_from_ident_9b(reply) == _ZeroSigEcu.SIG
    # an ECU that writes with its 9C signature has a different 9B field: only
    # used when 9C gives zeros
    assert E.erase_sig_from_ident_9b(reply[:20]) is None             # too short
    assert E.erase_sig_from_ident_9b(bytes([0x5A, 0x9B]) + b"\x01" * 40) is None
    zero = bytearray(reply); zero[22:28] = bytes(6)
    assert E.erase_sig_from_ident_9b(bytes(zero)) is None


class _WantsDefaultSig(_ZeroSigEcu):
    """An untested ECU: a non-zero 1A 9C signature it nevertheless refuses,
    taking only another candidate -- here the 9B one. No part number is
    involved; the refusal itself sends the flasher to the next candidate."""

    def _consume(self):
        b = bytes(self.parse)
        if b[:5] == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9C]) and len(b) >= 6:
            self._respond([0x8C, 0xF1, 0x10, 0x5A, 0x9C, 0x00, 0x12, 0x11, 0x00,
                           0x00, 0x01, 0x02, 0x03, 0x04, 0x05])
            del self.parse[:6]; return
        super()._consume()


def test_a_refused_erase_signature_is_retried_with_the_other_candidate():
    ecu = _WantsDefaultSig(bytearray(bytes([0x22]) * E.CAL_SIZE))
    flash = E.Edc16Flash(_Link(ecu))
    flash.write_flash(_diff_image(), force_full=True, speed="Low", allow_fast=False)
    # the 9C one first, refused once; the 9B one from then on, every block
    assert ecu.erase_sigs[0] == bytes([0x00, 0x01, 0x02, 0x03, 0x04, 0x05])
    assert all(s == _ZeroSigEcu.SIG for s in ecu.erase_sigs[1:]) and len(ecu.erase_sigs) >= 3
    written = bytes(ecu.flash)
    assert written[0x40000:0x40000 + 0x3E000] == bytes(_diff_image()[0x1C0000:0x1FE000])


class _RefusesBlockEcu(MockEcu):
    """An ECU that refuses every written block: its checksum routine answers
    7F 33 22 (conditionsNotCorrect) on every poll -- what a 038906016K did
    to an image whose content digest was stale
    (Reference/Logs/EDC16Checksumfailwritefail.txt)."""

    def __init__(self, cal=None, stop_after=None):
        super().__init__(cal)
        self.polls = 0
        self.stop_after = stop_after
        self.stop = False

    def _consume(self):
        b = bytes(self.parse)
        if b[:3] == bytes([0x02, 0x33, 0xC5]) and len(b) >= 4 and self._dl_start is not None:
            self.polls += 1
            if self.stop_after is not None and self.polls >= self.stop_after:
                self.stop = True
            self._respond([0x7F, 0x33, 0x22])
            del self.parse[:4]; return
        super()._consume()


def test_a_refused_block_fails_the_write_with_the_reason(monkeypatch):
    """7F 33 22 on every poll after a block is written is the ECU refusing
    it: the write stops with the reason, after VERIFY_REFUSED_AFTER_S of
    them -- not a minute of polling per block and a dead session after."""
    monkeypatch.setattr(E, "VERIFY_REFUSED_AFTER_S", 0.5)
    ecu = _RefusesBlockEcu(bytearray(b"\x22" * E.CAL_SIZE))
    flash = E.Edc16Flash(_Link(ecu))
    with pytest.raises(E.KLineError) as ei:
        flash.write_flash(_diff_image(), force_full=True, speed="Low", allow_fast=False)
    msg = str(ei.value)
    assert "rejected the block" in msg and "checksums" in msg and "7F 33 22" in msg


def test_cancel_stops_a_write_waiting_on_the_checksum_routine(monkeypatch):
    monkeypatch.setattr(E, "VERIFY_REFUSED_AFTER_S", 60.0)
    ecu = _RefusesBlockEcu(bytearray(b"\x22" * E.CAL_SIZE), stop_after=3)
    flash = E.Edc16Flash(_Link(ecu))
    with pytest.raises(E.StopRequested):
        flash.write_flash(_diff_image(), force_full=True, speed="Low", allow_fast=False,
                          is_stop=lambda: ecu.stop)
