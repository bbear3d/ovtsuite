# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A cluster EEPROM read that the K-line garbles is repeated from a fresh session."""
import pytest

from ovtsuite.flash import vdo_cluster as V
from ovtsuite.flash.transport import KLineError, KLineTimeout, LinkStopped


class Dummy:
    def clear_rx(self):
        pass


def cluster(failures):
    """A VdoCluster whose reads fail with the given exceptions first, then return 16 bytes."""
    log = []
    c = V.VdoCluster(Dummy(), log=log.append)
    c.log_lines = log
    c.reopened = 0
    queue = list(failures)

    def read_eeprom(addr, n):
        if queue:
            raise queue.pop(0)
        return bytes([addr & 0xFF]) * n

    def reopen():
        c.reopened += 1

    c.read_eeprom, c._reopen = read_eeprom, reopen
    return c


def test_a_garbled_block_is_read_again_in_a_new_session():
    c = cluster([KLineError("KW1281: block counter 0xFC, expected 0x33")])
    data = c.dump(0, 32)
    assert len(data) == 32 and c.reopened == 1
    assert any("the line broke" in m for m in c.log_lines)


def test_gives_up_after_the_retries():
    c = cluster([KLineTimeout("K-line read timed out")] * (V.VdoCluster.READ_RETRIES + 1))
    with pytest.raises(KLineTimeout):
        c.dump(0, 16)
    assert c.reopened == V.VdoCluster.READ_RETRIES


def test_a_stop_is_not_retried():
    c = cluster([LinkStopped("stop requested")])
    with pytest.raises(LinkStopped):
        c.dump(0, 16)
    assert c.reopened == 0


def test_the_c5_dialog_is_not_reopened():
    c = cluster([KLineError("x")])
    c.c5 = True
    with pytest.raises(KLineError):
        c.dump(0, 16)
    assert c.reopened == 0


def test_a_refusal_is_reported_as_before():
    c = V.VdoCluster(Dummy())
    c.read_eeprom = lambda addr, n: None
    with pytest.raises(V.ClusterError):
        c.dump(0, 16)


def test_the_login_is_started_again_when_it_breaks(monkeypatch):
    c = V.VdoCluster(Dummy())
    calls = []

    def connect(tries=3):
        calls.append("connect")
        if len(calls) == 1:
            raise KLineError("KW1281: block end 0x58, expected 0x03")

    c.connect, c.unlock_for_eeprom, c.end = connect, lambda: calls.append("unlock"), lambda: None
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    c.open_for_eeprom()
    assert calls == ["connect", "connect", "unlock"]


def test_a_garbled_block_write_is_written_again_in_a_new_session():
    c = V.VdoCluster(Dummy(), log=lambda m: None)
    calls, reopened = [], []
    queue = [KLineError("KW1281: block end 0x46, expected 0x03")]

    def write_eeprom(addr, values):
        calls.append(addr)
        if queue:
            raise queue.pop(0)
        return True

    c.write_eeprom, c._reopen = write_eeprom, lambda: reopened.append(1)
    assert c._write_block(0x40, bytes(16)) is True
    assert calls == [0x40, 0x40] and len(reopened) == 1


def test_a_refused_write_is_not_repeated_and_a_stop_is_not_retried():
    c = V.VdoCluster(Dummy())
    calls = []
    c.write_eeprom = lambda a, v: calls.append(a) or False
    c._reopen = lambda: pytest.fail("reopened")
    assert c._write_block(0, bytes(16)) is False and calls == [0]

    def stopped(a, v):
        raise LinkStopped("stop requested")

    c.write_eeprom = stopped
    with pytest.raises(LinkStopped):
        c._write_block(0, bytes(16))


def test_write_gives_up_after_the_retries():
    c = V.VdoCluster(Dummy(), log=lambda m: None)
    reopened = []

    def write_eeprom(a, v):
        raise KLineTimeout("K-line read timed out")

    c.write_eeprom, c._reopen = write_eeprom, lambda: reopened.append(1)
    with pytest.raises(KLineTimeout):
        c._write_block(0, bytes(16))
    assert len(reopened) == V.VdoCluster.READ_RETRIES
