# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VDO cluster: the Mk4 read's reply-title check, the bounded retry of the
unlock inside a re-opened session, and the Audi C5 path (ident detection,
login, D1 exchange, read, write, reset) against a small mock C5 cluster.
No hardware."""
import pytest

from ovtsuite.flash import kwp
from ovtsuite.flash import vdo_cluster as vc
from ovtsuite.flash.transport import KLineError, KLineTimeout, LinkStopped
from tests.test_vdo import synthetic
from tests.test_vdo_typecheck import FakeKw, c5_image


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(kwp, "R6", 0)
    monkeypatch.setattr(kwp, "BLOCK_LEAD", 0)
    monkeypatch.setattr(vc.time, "sleep", lambda s: None)


# -- V7: the read response title ---------------------------------------------
def _cluster_with_blocks(blocks):
    c = vc.VdoCluster(object(), log=lambda m: None)
    c._send = lambda payload: list(blocks)
    return c


def _blk(title, body):
    return bytes([len(body) + 3, 0, title]) + bytes(body)


def test_read_rejects_a_block_that_is_not_the_read_response():
    c = _cluster_with_blocks([_blk(vc.BT_ASCII, b"A" * 16)])
    with pytest.raises(vc.ClusterError, match="not the read response"):
        c.read_eeprom(0, 16)


def test_read_accepts_the_read_response():
    c = _cluster_with_blocks([_blk(vc.BT_READ_EEPROM_RESP, bytes(range(16)))])
    assert c.read_eeprom(0, 16) == bytes(range(16))


def test_a_stray_block_is_retried_in_a_new_session_and_never_taken_as_data():
    c = vc.VdoCluster(object(), log=lambda m: None)
    replies = [[_blk(vc.BT_ASCII, b"B" * 16)],
               [_blk(vc.BT_READ_EEPROM_RESP, b"\x11" * 16)]]
    c._send = lambda payload: replies.pop(0)
    reopened = []
    c._reopen = lambda: reopened.append(1)
    assert c.dump(0, 16) == b"\x11" * 16 and reopened == [1]


def test_a_stray_block_every_time_gives_up():
    c = vc.VdoCluster(object(), log=lambda m: None)
    c._send = lambda payload: [_blk(vc.BT_ASCII, b"B" * 16)]
    c._reopen = lambda: None
    with pytest.raises(vc.ClusterError):
        c.dump(0, 16)


# -- V8: the unlock inside _reopen is retried ---------------------------------
class _Link:
    def clear_rx(self):
        pass


def _reopen_cluster(unlock_failures):
    c = vc.VdoCluster(_Link(), log=lambda m: None)
    c.end = lambda: None
    calls = {"connect": 0, "unlock": 0}
    queue = list(unlock_failures)

    def connect(tries=3):
        calls["connect"] += 1

    def unlock():
        calls["unlock"] += 1
        if queue:
            raise queue.pop(0)
    c.connect, c.unlock_for_eeprom = connect, unlock
    return c, calls


def test_reopen_retries_a_failed_unlock():
    c, calls = _reopen_cluster([KLineError("refused"), KLineTimeout("none")])
    c._reopen()
    assert calls["unlock"] == 3 and calls["connect"] == 3


def test_reopen_gives_up_after_the_bound():
    n = vc.VdoCluster.READ_RETRIES + 1
    c, calls = _reopen_cluster([KLineError("refused")] * (n + 5))
    with pytest.raises(KLineError):
        c._reopen()
    assert calls["unlock"] == n


def test_reopen_honours_stop_during_the_retries():
    state = {"stop": False}
    c = vc.VdoCluster(_Link(), log=lambda m: None, is_stop=lambda: state["stop"])
    c.end = lambda: None
    c.connect = lambda tries=3: None
    unlocks = []

    def unlock():
        unlocks.append(1)
        state["stop"] = True
        raise KLineError("refused")
    c.unlock_for_eeprom = unlock
    with pytest.raises(vc.StopRequested):
        c._reopen()
    assert len(unlocks) == 1


def test_reopen_does_not_retry_a_stop_from_the_link():
    c, calls = _reopen_cluster([LinkStopped("stop requested")])
    with pytest.raises(LinkStopped):
        c._reopen()
    assert calls["unlock"] == 1


# -- V9: the C5 path -----------------------------------------------------------
class MockC5Link:
    """A byte-level Audi C5 cluster after its 10400 login: D1 blocks, 8-byte
    reads and writes, a 2 KB EEPROM."""
    NAK = 0x0A

    def __init__(self, eeprom=None, passwords=("vdokombi",), level=7):
        self.eeprom = bytearray(eeprom or bytes(vc.EEPROM_SIZE))
        self.passwords = list(passwords)
        self.level = level
        self.rx = []
        self.baud = []
        self.parity = []
        self.frames = []
        self.resets = 0
        self.garble = False          # next reply: wrong checksum
        self.silent = False          # no answer to anything

    # transport
    def set_baud(self, b):
        self.baud.append(b)

    def set_parity(self, p):
        self.parity.append(p)

    def clear_rx(self):
        self.rx.clear()

    def read_byte(self):
        if not self.rx:
            raise KLineTimeout("no answer")
        return self.rx.pop(0)

    @staticmethod
    def _frame(body):
        f = [0xD1, len(body) + 3] + list(body)
        ck = 0
        for b in f:
            ck ^= b
        return f + [ck]

    def write_bytes(self, data):
        d = list(data)
        ck = 0
        for b in d:
            ck ^= b
        assert d[0] == 0xD1 and d[1] == len(d) and ck == 0, d
        body = d[2:-1]
        self.frames.append(body)
        if self.silent:
            return
        reply = self._answer(body)
        if reply is None:
            return
        f = self._frame(reply)
        if self.garble:
            f[-1] ^= 0xFF
            self.garble = False
        self.rx.extend(f)

    def _answer(self, b):
        t = b[0]
        if t == vc.C5_HELLO:
            return [vc.C5_HELLO]
        if t == vc.C5_ACK:
            return [vc.C5_ACK]
        if t == vc.C5_LOGIN:
            if b[1] == 0x9D:
                pw = bytes(b[2:]).decode()
                return [vc.C5_ACK] if pw in self.passwords else [self.NAK]
            if b[1] == 0x96 and b[2] == 0x04:
                return [vc.C5_LOGIN, self.level]
            if b[1] == vc.CMD_RESET:
                self.resets += 1
                return None
            return [self.NAK]
        if t == vc.C5_READ:
            n, a = b[1], (b[2] << 8) | b[3]
            return [vc.C5_READ] + list(self.eeprom[a:a + n])
        if t == vc.C5_WRITE:
            n, a = b[1], (b[2] << 8) | b[3]
            self.eeprom[a:a + n] = bytes(b[4:4 + n])
            return [vc.C5_ACK]
        return [self.NAK]


def c5_cluster(link, kw_replies=(vc.BT_NAK, vc.BT_ACK, vc.BT_ACK), log=None):
    c = vc.VdoCluster(link, log=log or (lambda m: None))
    c.c5 = True
    c.kw = FakeKw(kw_replies)
    return c


def test_is_c5_ident_real_prefixes():
    for ident in ("4B0920900J", "4B0 920 900 J", "8D0920900K KOMBI+WEGFAHRS",
                  "4Z7920900A", "8Z0920950B", "8D0920930"):
        assert vc.is_c5_ident(ident), ident


def test_is_c5_ident_tolerates_a_corrupted_first_byte():
    assert vc.is_c5_ident("\x7fB0920900J")          # "4" lost
    assert vc.is_c5_ident("xZ7920900A")


def test_is_c5_ident_rejects_other_clusters():
    for ident in ("", None, "1J0920927A", "VWK501MH 01.10", "4D0919035AJ",
                  "8E0920900", "6N0920900"):
        assert not vc.is_c5_ident(ident), ident


def test_is_c5_ident_rejects_3b0_and_other_digit_led_lookalikes():
    """A leading digit is a real part-number character, not a damaged one:
    3B0920... (Passat B5) and 4D0920... (A8) must not match as C5."""
    for ident in ("3B0920820", "3B0 920 820 A", "3B0920900", "4D0920900",
                  "7D0920902", "1B0920900"):
        assert not vc.is_c5_ident(ident), ident
    # the shortened form is only taken at the very start of the ident
    assert not vc.is_c5_ident("VWK 3B0920820")
    assert not vc.is_c5_ident("ab B0920900")


def test_c5_exchange_frames_and_checks_the_answer():
    link = MockC5Link()
    c = c5_cluster(link)
    blk = c._c5_exchange([vc.C5_HELLO])
    assert blk[2] == vc.C5_HELLO and link.frames == [[vc.C5_HELLO]]


def test_c5_exchange_rejects_a_bad_checksum_and_a_bad_header():
    link = MockC5Link()
    link.garble = True
    c = c5_cluster(link)
    with pytest.raises(vc.ClusterError, match="bad block"):
        c._c5_exchange([vc.C5_HELLO])
    link.rx[:] = [0x55, 4, vc.C5_HELLO, 0x55 ^ 4 ^ vc.C5_HELLO]
    with pytest.raises(vc.ClusterError, match="bad block"):
        c._c5_exchange([vc.C5_HELLO])


def test_c5_exchange_times_out_when_the_cluster_is_silent():
    link = MockC5Link()
    link.silent = True
    with pytest.raises(KLineTimeout):
        c5_cluster(link)._c5_exchange([vc.C5_HELLO])


def test_c5_unlock_logs_in_at_19200_even_parity():
    link = MockC5Link(passwords=("w10kombi",))
    log = []
    c = c5_cluster(link, log=log.append)
    c._c5_unlock()
    assert c.info.unlocked and c.info.access_level == 7
    assert link.baud == [19200] and link.parity == ["E"]
    tried = [bytes(f[2:]).decode() for f in link.frames
             if f[0] == vc.C5_LOGIN and f[1] == 0x9D]
    assert tried == ["19xDR8xS", "vdokombi", "w10kombi"]     # until one is accepted
    # the 10400 stage sent the unlock-additional block and the two passwords
    assert len(c.kw.sent) == 3 and c.kw.sent[1][:2] == [vc.BT_CUSTOM, 0x80]


def test_c5_unlock_fails_when_every_login_password_is_refused():
    c = c5_cluster(MockC5Link(passwords=()))
    with pytest.raises(vc.ClusterError, match="refused every login"):
        c._c5_unlock()


def test_c5_unlock_fails_when_both_custom_logins_are_refused():
    link = MockC5Link()
    c = c5_cluster(link, kw_replies=(vc.BT_NAK, vc.BT_NAK, vc.BT_NAK))
    with pytest.raises(vc.ClusterError, match="both custom logins"):
        c._c5_unlock()
    assert link.baud == []                    # never got to 19200


def test_c5_unlock_does_seed_key_when_the_level_is_low(monkeypatch):
    link = MockC5Link(level=3)
    seen = []
    orig = link._answer

    def answer(b):
        if b[0] == vc.C5_LOGIN and b[1] == 0x96 and b[2] == 0x01:
            seen.append("seed")
            return [vc.C5_LOGIN] + list(range(10))
        if b[0] == vc.C5_LOGIN and b[1] == 0x96 and b[2] == 0x02:
            seen.append("key")
            return [vc.C5_ACK]
        return orig(b)
    link._answer = answer
    monkeypatch.setattr(vc, "find_key", lambda seed, lvl, log: bytes(4))
    c = c5_cluster(link)
    c._c5_unlock()
    assert seen == ["seed", "key"] and c.info.unlocked


def test_c5_read_in_8_byte_chunks():
    img = bytes(range(256)) * 8
    link = MockC5Link(img)
    c = c5_cluster(link)
    assert c._c5_read(0x20, 16) == img[0x20:0x30]
    reads = [f for f in link.frames if f[0] == vc.C5_READ]
    assert [(f[1], f[2] << 8 | f[3]) for f in reads] == [(8, 0x20), (8, 0x28)]
    assert c.read_eeprom(0x100, 16) == img[0x100:0x110]       # dispatch via c5


def test_c5_read_returns_none_on_a_refusal():
    link = MockC5Link()
    link._answer = lambda b: [link.NAK]
    assert c5_cluster(link)._c5_read(0, 16) is None


def test_c5_write_in_8_byte_chunks_and_refusal():
    link = MockC5Link()
    c = c5_cluster(link)
    data = bytes(range(16))
    assert c._c5_write(0x40, data) is True
    assert bytes(link.eeprom[0x40:0x50]) == data
    writes = [f for f in link.frames if f[0] == vc.C5_WRITE]
    assert [(f[1], f[2] << 8 | f[3]) for f in writes] == [(8, 0x40), (8, 0x48)]
    log = []
    link2 = MockC5Link()
    link2._answer = lambda b: [link2.NAK]
    assert c5_cluster(link2, log=log.append)._c5_write(0, data) is False
    assert any("refused" in m for m in log)


def test_c5_reset_sends_login_82_and_tolerates_an_answer():
    link = MockC5Link()
    c = c5_cluster(link)
    link._answer = lambda b: [vc.C5_ACK]
    assert c.reset() is True and link.frames[-1] == [vc.C5_LOGIN, vc.CMD_RESET]
    log = []
    link2 = MockC5Link()
    c2 = c5_cluster(link2, log=log.append)
    assert c2.reset() is True and link2.resets == 1 and not log[1:]


def test_c5_reset_warns_on_an_odd_answer():
    link = MockC5Link()
    log = []
    c = c5_cluster(link, log=log.append)
    link._answer = lambda b: [link.NAK]
    assert c.reset() is True
    assert any("Warning: reset answered" in m for m in log)


def test_c5_end_restores_the_line():
    link = MockC5Link()
    c5_cluster(link).end()
    assert link.parity == ["N"] and link.baud == [vc.BAUD]


def _connected_c5(monkeypatch, link):
    def connect(self, tries=3):
        self.kw = FakeKw([vc.BT_NAK, vc.BT_ACK, vc.BT_ACK])
        self.c5 = True
        self.info.ident = "4B0920900J"
        return self.info
    monkeypatch.setattr(vc.VdoCluster, "connect", connect)
    monkeypatch.setattr(vc.VdoCluster, "start_keep_alive",
                        lambda self, interval=0.02: vc.KeepAlive(self, 0.02))


def test_write_cluster_c5_end_to_end_with_verify_and_reset(monkeypatch):
    link = MockC5Link()
    _connected_c5(monkeypatch, link)
    img = c5_image()
    rep = vc.write_cluster(link, img, verify=True)
    assert bytes(link.eeprom) == img and rep.verified and rep.reset
    assert link.resets == 1 and link.baud[-1] == vc.BAUD


def test_write_cluster_c5_without_verify_still_resets(monkeypatch):
    link = MockC5Link()
    _connected_c5(monkeypatch, link)
    img = c5_image()
    rep = vc.write_cluster(link, img, verify=False)
    assert bytes(link.eeprom) == img and not rep.verified and rep.reset
    assert not [f for f in link.frames if f[0] == vc.C5_READ]
    assert "not read back" in rep.describe()


def test_mk4_file_on_a_c5_cluster_is_refused_without_override(monkeypatch):
    link = MockC5Link()
    _connected_c5(monkeypatch, link)
    with pytest.raises(vc.ClusterTypeMismatch, match="C5"):
        vc.write_cluster(link, synthetic("VWK501"))
    assert not [f for f in link.frames if f[0] == vc.C5_WRITE] and link.resets == 0


def test_mk4_file_on_a_c5_cluster_cancel_and_override(monkeypatch):
    link = MockC5Link()
    _connected_c5(monkeypatch, link)
    img = synthetic("VWK501")
    seen = []
    rep = vc.write_cluster(link, img, confirm_mismatch=lambda *a: seen.append(a) or False)
    assert rep.cancelled and not [f for f in link.frames if f[0] == vc.C5_WRITE]
    assert "Mk4" in seen[0][0] and "C5" in seen[0][1] and seen[0][2] == "4B0920900J"
    rep = vc.write_cluster(link, img, confirm_mismatch=lambda *a: True)
    assert not rep.cancelled and bytes(link.eeprom) == img
