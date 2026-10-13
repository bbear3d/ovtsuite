# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Logging the ZF 6HP28 (GS19) over UDS/TP2.0 -- the 04A1 live block.

Unlike the 6HP19 "ZF6HP" logger (readDataByLocalIdentifier 0x21 groups), the
6HP28 serves one readDataByIdentifier block, DID 04A1, of 41 fixed-width
records.  These pin the decode against the real car capture
(Reference/Logs/VCDS-UDS-Trans.log) and the live session end-to-end against a
mock TP2.0 TCU, with no Qt dependency.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import zf6hp as Z                              # noqa: E402
from ovtsuite.flash.tp20_transport import CanKLine, Tp20Channel    # noqa: E402
from mock_tp20 import MockTp20Ecu                                  # noqa: E402
from ovtsuite.datalog import zf6hp_uds_live as L                   # noqa: E402

# The exact 62 04A1 response body (164 bytes) read off the car.
BLOCK = bytes.fromhex(
    "191f050d05310101092005014621010142220501522305016224050172250501"
    "3626010106350001694300013d3a0001082c0101162a01016c49000  16d340101"
    "263000016529000117070103182f0003531901030303010304130103 1b1e0003"
    "101d0003220a000315050100010105001115000002020100550601001  40c0000"
    "130b00055c1a00053c1c000556520502 775a0102475305022016  0005303b0001"
    "2e540102".replace(" ", ""))
assert len(BLOCK) == 164


# ----------------------------------------------------------------- decode --
def test_decode_block_is_41_records_in_firmware_order():
    recs = L.decode_block(BLOCK)
    assert len(recs) == 41
    assert [r.signal_id for r in recs] == L.BLOCK_ORDER
    # record layout: raw, signal, sub, scale-type -- spot-check the first few
    assert (recs[0].signal_id, recs[0].sub, recs[0].scale_type, recs[0].raw) == (0x1f, 5, 0x0d, 25)
    assert (recs[3].signal_id, recs[3].raw) == (0x21, 70)


def test_decode_ignores_a_short_tail():
    recs = L.decode_block(BLOCK + b"\x00\x11")      # 2 trailing bytes, not a record
    assert len(recs) == 41


def test_live_cell_reports_raw_when_uncalibrated():
    c = L.LiveCell(L.decode_block(BLOCK)[3])         # signal 0x21, raw 70
    assert c.value == 70.0 and c.unit == "" and c.text == "70"


# ------------------------------------------------------------------ namer --
def test_namer_lists_one_group_of_41_named_cells():
    nm = L.Blocks6hpUds()
    assert nm.available and nm.populated_groups() == [L.GROUP]
    assert nm.slots(L.GROUP) == 41
    label, signal, unit = nm.resolve(L.GROUP, 3)
    # self-documenting: signal id AND its fixed scale type in the column label
    assert signal == "zf6hp_21" and "0x21" in label and "[t1]" in label
    ch = nm.channel(L.GROUP, 0)
    assert ch.group == L.GROUP and ch.field == 0 and not ch.is_memory


# ------------------------------------------------------------- live session
REPLIES = {
    b"\x22\x04\xa1": b"\x62\x04\xa1" + BLOCK,
    b"\x22\xf1\x87": b"\x62\xf1\x87" + b"09G927158GT ",
    b"\x22\xf1\x91": b"\x62\xf1\x91" + b"GS19",
}


def _link(dest=Z.DEST, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(bytes(req))
        return REPLIES.get(bytes(req).lower())
    ecu = MockTp20Ecu(handler, dest=dest, ecu_tx=0x300 + dest, ecu_rx=0x740)
    tp = Tp20Channel(ecu, dest=dest, listen_id=Z.LISTEN_ID)
    tp.open()
    return CanKLine(tp=tp, dest=dest), ecu


def test_session_goes_straight_to_rdbi_and_reads_the_block():
    seen = []
    link, _ecu = _link(seen=seen)
    s = L.Zf6hpUdsLiveSession(link, "ZF6HP-UDS")
    s.connect()
    # No StartDiagnosticSession/0x21 group setup -- straight to RDBI like the car.
    assert all(not r.lower().startswith(b"\x10") for r in seen)
    assert any(r.lower().startswith(b"\x22\x04\xa1") for r in seen)
    assert "09G927158GT" in s.identity
    cells = s.read_group(L.GROUP)
    assert len(cells) == 41
    assert cells[3].signal_id == 0x21 and cells[3].value == 70.0
    # read_groups maps the single block onto every requested group id
    assert set(s.read_groups([L.GROUP]).keys()) == {L.GROUP}


def test_session_declines_memory_reads():
    link, _ecu = _link()
    s = L.Zf6hpUdsLiveSession(link, "ZF6HP-UDS")
    assert s.supports_memory_read is False
    assert s.read_memory(0x180000, 16) is None
