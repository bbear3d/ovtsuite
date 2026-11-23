# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The VDO cluster's two speeds: Low is the KWHack dialog (every block ACKed,
the software version read four ways, a ReadIdent before the unlock), High lets
the next command be the ACK, reads the version once and skips the ReadIdent.
Write verification is optional."""
import pytest

from ovtsuite.flash import kwp
from ovtsuite.flash import vdo_cluster as vc
from ovtsuite.flash.vdo_sim import SimClusterLink
from tests.test_vdo import synthetic
from tests.test_vdo_flash import _edited


@pytest.fixture(autouse=True)
def _no_wire_delays(monkeypatch):
    monkeypatch.setattr(kwp, "R6", 0)
    monkeypatch.setattr(kwp, "BLOCK_LEAD", 0)
    monkeypatch.setattr(vc.time, "sleep", lambda s: None)


class _Counting(SimClusterLink):
    """The simulator, counting the blocks the tester sends by title."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.titles = []
        self.versions = 0

    def _on_block(self, title, body):
        self.titles.append(title)
        if title == vc.BT_CUSTOM and body and body[0] == 0x84:
            self.versions += 1
        super()._on_block(title, body)


def _sim(version="VWK501MH 01.10"):
    img = synthetic("VWK501", version)
    return img, _Counting(img, version)


def test_high_speed_reads_the_same_eeprom_without_acking_each_block():
    img, slow = _sim()
    _, fast = _sim()
    assert vc.read_cluster(slow)[0] == img
    data, meta = vc.read_cluster(fast, fast=True)
    assert data == img and meta["rom_version"] == "VWK501MH 01.10"
    assert fast.ended and fast.level == 7
    reads = fast.titles.count(vc.BT_READ_EEPROM)
    assert reads == vc.EEPROM_SIZE // vc.BLOCK
    # Low ACKs every EEPROM answer; High's next command is the ACK
    assert slow.titles.count(vc.BT_ACK) >= reads
    assert fast.titles.count(vc.BT_ACK) < 20          # the login's own, no more


def test_the_software_version_is_read_once_at_high_speed_and_four_ways_at_low():
    _, slow = _sim()
    _, fast = _sim()
    vc.read_cluster(slow)
    vc.read_cluster(fast, fast=True)
    assert slow.versions == 4 and fast.versions == 1
    assert vc.BT_READ_IDENT in slow.titles
    assert vc.BT_READ_IDENT not in fast.titles


def test_high_speed_write_without_verify_reads_nothing_back():
    img, sim = _sim()
    new = _edited(img, 0x150, (3700).to_bytes(2, "little"))
    rep = vc.write_cluster(sim, new, fast=True, verify=False)
    assert bytes(sim.eeprom) == new and rep.reset is True
    assert sim.titles.count(vc.BT_READ_EEPROM) == 0
    assert rep.verified is False
    assert "not read back" in rep.describe()
    assert "verified" not in rep.describe().replace("not read back", "")


def test_verify_reads_every_block_back_and_catches_a_bad_one():
    img, sim = _sim()
    new = _edited(img, 0x150, (3700).to_bytes(2, "little"))
    rep = vc.write_cluster(sim, new, fast=True, verify=True)
    assert sim.titles.count(vc.BT_READ_EEPROM) == vc.EEPROM_SIZE // vc.BLOCK
    assert rep.verified and "verified it by reading back" in rep.describe()
    img, bad = _sim()
    bad.corrupt_write_at = {0x150}
    with pytest.raises(vc.ClusterError, match="verify failed"):
        vc.write_cluster(bad, _edited(img, 0x150, (3700).to_bytes(2, "little")),
                         fast=True, verify=True)


def test_high_speed_write_still_notices_a_refused_block():
    img, sim = _sim()
    sim.refuse_write_at = {0x40}
    with pytest.raises(vc.ClusterError, match="refused the write"):
        vc.write_cluster(sim, _edited(img, 0x150, (3700).to_bytes(2, "little")),
                         fast=True, verify=False)


def test_the_default_is_still_the_kwhack_dialog_with_verify():
    img, sim = _sim()
    new = _edited(img, 0x150, (3700).to_bytes(2, "little"))
    rep = vc.write_cluster(sim, new)
    assert rep.verified and sim.versions == 4
    assert sim.titles.count(vc.BT_READ_EEPROM) == vc.EEPROM_SIZE // vc.BLOCK


def test_high_speed_answers_each_byte_at_once_on_a_plain_serial_link():
    """The R6 wait is 0 for High on a plain link, and the R6_FAST rule where
    the link has its own settle time (J2534)."""
    _, sim = _sim()
    c = vc.VdoCluster(sim, fast=True)
    c.connect()
    assert c.kw._r6 == 0.0
    _, j = _sim()
    j.rx_settle_s = 0.0005
    c = vc.VdoCluster(j, fast=True)
    c.connect()
    assert c.kw._r6 == max(0.0, kwp.R6_FAST - 0.0005)


def test_a_4d0919035aj_cluster_is_switched_to_low_speed():
    assert vc.needs_low_speed("4D0919035AJ KOMBI+WEGFAHRSP VDO V04")
    assert vc.needs_low_speed("\xffD0919035AJ KOMBI")       # corrupted first byte
    assert not vc.needs_low_speed("1J0920826G KOMBI+WEGFAHRSP VDO V04")
    img = synthetic("VWK501", "VWK501MH 01.10")
    sim = _Counting(img, "VWK501MH 01.10")
    sim.ident = "4D0919035AJ KOMBI+WEGFAHRSP VDO V04"
    logs = []
    data, _ = vc.read_cluster(sim, log=logs.append, fast=True)
    assert data == img
    assert any("switching to Low" in m for m in logs)
    assert sim.versions == 4 and vc.BT_READ_IDENT in sim.titles   # the Low dialog
    assert sim.titles.count(vc.BT_ACK) >= vc.EEPROM_SIZE // vc.BLOCK
    other = _Counting(img, "VWK501MH 01.10")
    other.ident = "1J0920826G KOMBI+WEGFAHRSP VDO V04"
    logs = []
    vc.read_cluster(other, log=logs.append, fast=True)
    assert not any("switching to Low" in m for m in logs) and other.versions == 1


class _Deaf(SimClusterLink):
    """A line nothing answers on: every 5-baud init fails."""
    inits = 0

    def wake_up(self, *a, **k):
        _Deaf.inits += 1
        from ovtsuite.flash.transport import KLineError
        raise KLineError("J2534 5-baud init to 0x97 failed: ERR_FAILED")


def test_no_answer_to_the_init_stops_after_one_round_of_tries_with_a_clear_error():
    img = synthetic("VWK501", "VWK501MH 01.10")
    sim = _Deaf(img, "VWK501MH 01.10")
    _Deaf.inits = 0
    with pytest.raises(vc.ClusterNoAnswer, match="did not answer the 5-baud init"):
        vc.read_cluster(sim, fast=True)
    assert _Deaf.inits == 3                    # connect's own three tries, no more
    _Deaf.inits = 0
    with pytest.raises(vc.ClusterNoAnswer):
        vc.write_cluster(sim, img, fast=True)
    assert _Deaf.inits == 3
