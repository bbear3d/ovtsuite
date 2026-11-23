# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC17 CP14 identify / probe / read over TP 2.0 (ovtsuite.flash.edc17_read).

The identification replies are the ones sniffed from a third-party tool on a
Jetta TDI EDC17CP14 (capture_20261115_190854.log); the 7F 35 52 answers are
what the ECU gave that tool for every memory request.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from mock_tp20 import MockTp20Ecu  # noqa: E402
from ovtsuite.flash import crypto as C  # noqa: E402
from ovtsuite.flash import edc16 as E  # noqa: E402
from ovtsuite.flash import edc17_read as X  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from ovtsuite.flash.edc16 import KLineError  # noqa: E402

#: the real seed/key pair from the bench run (see SA2_TAPE_CP14's docstring)
_WRITE_SEED = 0x4F271309
_WRITE_KEY = C.sa2_execute(X.SA2_TAPE_CP14, _WRITE_SEED)

_SYS = (b"37/1/EDC17_CP14/5/P1152//C1152K3ZL///" + bytes(65))        # 102 bytes
_REPLIES = {
    0x97: bytes.fromhex("43 4a 41 41"),
    0x8A: _SYS,
    0x90: b"3VWWL7AJ9EM253143",
    0x9B: (b"03L997030N  9983" + bytes.fromhex("03 00 c3 98 00 00 00")
           + b"FZLR4 2.0l TDI G000SG  "),
    0x9C: bytes.fromhex("00 0b 07 00 00 00 00") + b"FZL01.14.:2",
}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(TT, "_precise_wait", lambda s: None)


def _ecu(memory=None, refuse=0x52, write_refuse=0x90, session=None):
    """The ECU of the capture. ``memory``: {addr: bytes} it will upload (as
    one 0x76 packet); with None every 35 is refused 7F 35 52 like the capture
    (read session) or 7F 35 90 (write session, also seen on the bench, after
    27 01/02 succeeds -- so what unlocks an actual upload is still unknown)."""
    st = {"pos": None, "left": 0, "uploads": [], "session": session or [0x81]}

    def handler(req):
        s = req[0]
        if s == 0x1A:
            return bytes([0x5A, req[1]]) + _REPLIES[req[1]]
        if s == 0x27 and req[1] == 0x03:
            return bytes.fromhex("67 03 5d ba 74 e8")
        if s == 0x27 and req[1] == 0x04:
            return bytes.fromhex("67 04 34") if req[2:] == bytes.fromhex("5d ba a4 b1") \
                else bytes([0x7F, 0x27, 0x35])
        if s == 0x27 and req[1] == 0x01:                  # write level: 27 01/02, SA2 tape
            return bytes([0x67, 0x01]) + _WRITE_SEED.to_bytes(4, "big")
        if s == 0x27 and req[1] == 0x02:
            got = int.from_bytes(req[2:6], "big")
            return bytes([0x67, 0x02, 0x34]) if got == _WRITE_KEY else bytes([0x7F, 0x27, 0x35])
        if s == 0x10:
            st["session"][0] = req[1]
            return bytes([0x50, req[1]])
        if s == 0x35:
            addr = int.from_bytes(req[1:4], "big")
            size = int.from_bytes(req[5:8], "big")
            refusal = write_refuse if st["session"][0] == 0x85 else refuse
            if memory is None or addr not in memory:
                return bytes([0x7F, 0x35, refusal])
            st["pos"], st["left"], st["off"] = addr, size, 0
            st["uploads"].append((addr, size))
            return b"\x75\xff"
        if s == 0x36:
            n = min(254, st["left"])
            data = memory[st["pos"]][st["off"]:st["off"] + n]
            st["off"] += n
            st["left"] -= n
            return b"\x76" + data
        if s == 0x37:
            return b"\x77"
        if s == 0x23:
            return bytes([0x7F, 0x23, refuse])
        if s == 0x82:
            return b"\xc2"
        return bytes([0x7F, s, 0x11])
    return MockTp20Ecu(handler), st


def test_lvl3_key_is_the_one_in_the_capture():
    assert E.edc16_lvl3_key(bytes.fromhex("5dba74e8")) == 0x5DBAA4B1


def test_identify_matches_the_tools_report():
    ecu, _ = _ecu()
    fl = X.make_flash(ecu)
    i = X.identify(fl)
    assert i.vin == "3VWWL7AJ9EM253143"
    assert i.part_number == "03L997030N"
    assert i.software == "9983"
    assert i.engine_code == "CJAA"
    assert i.family == "EDC17_CP14"
    assert i.system_type == "R4 2.0l TDI G000SG"
    assert (i.flash_attempts, i.flash_completed) == (11, 7)
    assert "Flash attempts:" in i.summary()


def test_probe_reports_the_refusal_the_tool_saw():
    ecu, _ = _ecu()
    fl = X.make_flash(ecu)
    res = X.probe(fl, addrs=(0x0, 0x800000))
    up = [r for r in res if r.method.startswith("35")]
    assert len(up) == 2 and not any(r.ok for r in up)
    assert all("7F 35 52" in r.detail for r in up)


def test_write_level_unlock_matches_the_bench_run():
    """27 01 seed 4F271309 -> (via SA2_TAPE_CP14) key BFD0E7E8 -> 27 02 access
    granted: the exact exchange from the real EDC17CP14 on the bench."""
    assert _WRITE_KEY == 0xBFD0E7E8
    ecu, _ = _ecu()
    fl = X.make_flash(ecu)
    fl.connect()
    X.open_write_session(fl)                 # raises on any refusal


def test_probe_in_write_session_gets_the_other_refusal():
    ecu, _ = _ecu()
    fl = X.make_flash(ecu)
    res = X.probe(fl, addrs=(0x0,), session="write")
    up = [r for r in res if r.method.startswith("35")]
    assert len(up) == 1 and not up[0].ok
    assert "7F 35 90" in up[0].detail


def test_read_refuses_to_return_an_all_ff_image():
    ecu, _ = _ecu()
    fl = X.make_flash(ecu)
    with pytest.raises(KLineError, match="refused every upload"):
        X.read_image(fl, banks=(X.Bank("t", 0x0, 0x8000, 0),), upload_chunk=0x4000)


def test_read_assembles_both_banks_when_the_ecu_answers():
    a = bytes((i * 3 + 1) & 0xFF for i in range(0x4000))
    b = bytes((i * 5 + 7) & 0xFF for i in range(0x4000))
    mem = {0x000000: a, 0x800000: b}
    ecu, st = _ecu(memory=mem)
    fl = X.make_flash(ecu)
    banks = (X.Bank("A", 0x000000, 0x4000, 0), X.Bank("B", 0x800000, 0x4000, X.BANK_B_OFFSET))
    out = X.read_image(fl, banks=banks, upload_chunk=0x4000, reopen=False)
    img = out["image"]
    assert len(img) == X.IMAGE_SIZE
    assert img[:0x4000] == a
    assert img[X.BANK_B_OFFSET:X.BANK_B_OFFSET + 0x4000] == b
    assert out["ok"] == 0x8000 and not out["padded"]
    assert st["uploads"] == [(0x0, 0x4000), (0x800000, 0x4000)]


def test_a_single_bank_ecu_does_not_give_a_complete_looking_image():
    """The ECU answers bank A and refuses bank B (a single-bank part): the
    4 MB file is half 0xFF padding, and the result says so -- ``complete`` is
    False, ``padded_bytes`` counts it and ``warning`` names the range -- so a
    caller never saves it as a full read."""
    a = bytes((i * 3 + 1) & 0xFF for i in range(0x4000))
    ecu, _ = _ecu(memory={0x000000: a})
    fl = X.make_flash(ecu)
    stages = []
    banks = (X.Bank("A", 0x000000, 0x4000, 0), X.Bank("B", 0x800000, 0x4000, X.BANK_B_OFFSET))
    out = X.read_image(fl, banks=banks, upload_chunk=0x4000, reopen=False,
                       on_stage=stages.append)
    assert len(out["image"]) == X.IMAGE_SIZE
    assert out["image"][:0x4000] == a
    assert out["image"][X.BANK_B_OFFSET:X.BANK_B_OFFSET + 0x4000] == b"\xFF" * 0x4000
    assert out["ok"] == 0x4000 and out["padded_bytes"] == 0x4000
    assert out["complete"] is False
    assert "INCOMPLETE READ" in out["warning"] and "0x800000-0x803FFF" in out["warning"]
    assert any(m.startswith("WARNING: INCOMPLETE READ") for m in stages)


def test_a_complete_read_has_no_warning():
    a = bytes(range(256)) * 64
    ecu, _ = _ecu(memory={0x000000: a})
    fl = X.make_flash(ecu)
    out = X.read_image(fl, banks=(X.Bank("A", 0x0, 0x4000, 0),), upload_chunk=0x4000,
                       reopen=False)
    assert out["complete"] is True and out["warning"] is None and out["padded_bytes"] == 0
