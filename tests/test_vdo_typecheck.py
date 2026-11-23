# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VDO cluster write: the file-type vs cluster-type check (with the session
kept alive while the user decides), the C5 reset that is never answered, and
the C5 login's tolerance. No hardware: the KW1281 simulator or small fakes."""
import threading

import pytest

from ovtsuite.flash import kwp
from ovtsuite.flash import vdo_cluster as vc
from ovtsuite.flash.vdo_sim import SimClusterLink
from ovtsuite.flash.transport import KLineError, KLineTimeout
from ovtsuite.platforms import vdo as pv
from tests.test_vdo import synthetic


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(kwp, "R6", 0)
    monkeypatch.setattr(kwp, "BLOCK_LEAD", 0)
    monkeypatch.setattr(vc.time, "sleep", lambda s: None)
    orig = vc.VdoCluster.start_keep_alive

    def quick(self, interval=0.02):
        return orig(self, interval)
    monkeypatch.setattr(vc.VdoCluster, "start_keep_alive", quick)


def c5_image():
    d = bytearray(vc.EEPROM_SIZE)
    for i in range(pv.C5_IMMO_COPIES):
        a = pv.C5_IMMO_ADDR + i * pv.C5_IMMO_STRIDE
        d[a:a + 14] = b"VWZ7Z0B6127535"
    return bytes(d)


def wait(sec):
    threading.Event().wait(sec)


def test_c5_image_is_recognised():
    assert pv.layout_of(c5_image()) == "C5"


def test_matching_type_never_asks():
    img = synthetic("VWK501", "VWK501MH 01.10")
    sim = SimClusterLink(img, "VWK501MH 01.10")
    asked = []
    rep = vc.write_cluster(sim, img, confirm_mismatch=lambda *a: asked.append(a))
    assert not asked and not rep.cancelled and sim.writes


def test_mismatch_without_a_prompt_writes_nothing():
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    with pytest.raises(vc.ClusterTypeMismatch):
        vc.write_cluster(sim, c5_image())
    assert sim.writes == [] and sim.ended


def test_mismatch_cancel_keeps_session_alive_and_writes_nothing():
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    seen = []

    def confirm(file_t, cl_t, ident):
        seen.append((file_t, cl_t))
        wait(0.3)
        return False
    rep = vc.write_cluster(sim, c5_image(), confirm_mismatch=confirm)
    assert rep.cancelled and rep.blocks_written == [] and sim.writes == []
    assert "C5" in seen[0][0] and "Mk4" in seen[0][1]
    assert sim.keepalives >= 2          # the session was held open meanwhile
    assert sim.ended and "Nothing was written" in rep.describe()


def test_mismatch_override_writes():
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    img = c5_image()

    def confirm(*a):
        wait(0.1)
        return True
    rep = vc.write_cluster(sim, img, confirm_mismatch=confirm)
    assert not rep.cancelled and bytes(sim.eeprom) == img
    assert sim.writes == list(range(0, vc.EEPROM_SIZE, vc.BLOCK))


def test_connection_lost_while_waiting_is_reopened(monkeypatch):
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    img = c5_image()
    reopened = []
    orig_reopen = vc.VdoCluster._reopen

    def reopen(self):
        reopened.append(1)
        orig_reopen(self)
    monkeypatch.setattr(vc.VdoCluster, "_reopen", reopen)

    def dead(self):
        raise KLineTimeout("no answer")
    monkeypatch.setattr(vc.VdoCluster, "keep_alive", dead)

    def confirm(*a):
        wait(0.2)
        return True
    rep = vc.write_cluster(sim, img, confirm_mismatch=confirm)
    assert reopened == [1] and bytes(sim.eeprom) == img and rep.verified


def test_connection_lost_then_cancel_is_not_reopened(monkeypatch):
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    reopened = []
    monkeypatch.setattr(vc.VdoCluster, "_reopen", lambda self: reopened.append(1))
    monkeypatch.setattr(vc.VdoCluster, "keep_alive",
                        lambda self: (_ for _ in ()).throw(KLineError("gone")))

    def confirm(*a):
        wait(0.1)
        return False
    rep = vc.write_cluster(sim, c5_image(), confirm_mismatch=confirm)
    assert rep.cancelled and not reopened and sim.writes == []


def test_keep_alive_stop_is_prompt_and_idempotent():
    sim = SimClusterLink(synthetic("VWK501"), "VWK501MH 01.10")
    c = vc.VdoCluster(sim)
    c.open_for_eeprom()
    ka = c.start_keep_alive(0.01)
    wait(0.1)
    ka.stop()
    ka.stop()
    assert ka.sent >= 1 and not ka.lost and not ka._t.is_alive()


# -- C5 reset / login ---------------------------------------------------------
class FakeC5Link:
    def __init__(self, answers=()):
        self.sent = []
        self.rx = list(answers)
        self.baud = []
        self.parity = []

    def write_bytes(self, b):
        self.sent.append(bytes(b))

    def read_byte(self):
        if not self.rx:
            raise KLineTimeout("no answer")
        return self.rx.pop(0)

    def set_baud(self, b):
        self.baud.append(b)

    def set_parity(self, p):
        self.parity.append(p)

    def clear_rx(self):
        pass


def test_c5_reset_the_cluster_never_answers():
    link = FakeC5Link()
    c = vc.VdoCluster(link)
    c.c5 = True
    assert c.reset() is True
    assert len(link.sent) == 1 and link.sent[0][2:4] == bytes([vc.C5_LOGIN, vc.CMD_RESET])


def test_c5_reset_send_failure_is_reported_unsent():
    class Dead(FakeC5Link):
        def write_bytes(self, b):
            raise KLineError("line down")
    c = vc.VdoCluster(Dead())
    c.c5 = True
    assert c.reset() is False


class FakeKw:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send_block(self, p):
        self.sent.append(list(p))

    def receive_block(self):
        t = self.replies.pop(0)
        return bytes([3, 0, t])


def _c5_with(kw_replies, is_stop=None, monkeypatch=None):
    log = []
    link = FakeC5Link()
    c = vc.VdoCluster(link, log=log.append, is_stop=is_stop)
    c.c5 = True
    c.kw = FakeKw(kw_replies)
    answers = iter([
        [0xD1, 4, vc.C5_HELLO, 0],                      # hello
        [0xD1, 4, vc.C5_ACK, 0],                        # login accepted
        [0xD1, 5, vc.C5_LOGIN, 7, 0],                   # access level 7
    ])
    c._c5_exchange = lambda body: next(answers)
    return c, link, log


def test_c5_login_survives_an_odd_reply_after_one_password_was_accepted():
    # unlock-additional NAK, first password ACK, second an unexpected block
    c, link, log = _c5_with([vc.BT_NAK, vc.BT_ACK, 0x55])
    c._c5_unlock()
    assert c.info.unlocked and link.baud == [19200]
    assert any("ignored" in m for m in log)


def test_c5_login_odd_reply_before_any_acceptance_still_fails():
    c, _link, _log = _c5_with([vc.BT_NAK, 0x55])
    with pytest.raises(vc.ClusterError, match="unexpected block"):
        c._c5_unlock()


def test_c5_baud_settle_wait_honours_stop(monkeypatch):
    state = {"stop": False}
    c, link, _log = _c5_with([vc.BT_NAK, vc.BT_ACK, vc.BT_ACK],
                             is_stop=lambda: state["stop"])
    orig = link.set_baud

    def set_baud(b):
        orig(b)
        state["stop"] = True               # Stop pressed during the settle wait
    link.set_baud = set_baud
    c._check_stop = lambda: (_ for _ in ()).throw(vc.StopRequested("stopped")) \
        if state["stop"] else None
    with pytest.raises(vc.StopRequested):
        c._c5_unlock()


def test_chassis_table_has_no_audi_entry_and_passat_is_b5():
    assert pv.CHASSIS[0x1] == pv.CHASSIS[0xA] == "B5"
    assert "Audi" not in " ".join(pv.CHASSIS.values())
    assert pv.CHASSIS[0x0] != pv.CHASSIS[0x1]
