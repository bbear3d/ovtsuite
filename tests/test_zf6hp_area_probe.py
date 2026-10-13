# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ZF 6HP read-area probe (library call; the dialog no longer offers it).

Why the probe exists. The reference images are calibration ONLY. The 1 MiB
buffer is sparse -- just 0x040000-0x07FFFF carries data -- and across the whole
megabyte there is not one ``blr``, ``mflr``, ``blrl`` or ``bctr`` at any
alignment. A PowerPC function essentially always returns through blr, so 256 KiB
of populated data with none of them is tables and constants, nothing
executable. That follows from READ_AREAS being (0x04..0x07): the captured
factory session only ever asked for the calibration window.

So anything needing code is unanswerable from these files, including how the
controller turns a shift-program number into a ladder address. But
readMemoryByAddress takes the area as an explicit field, so asking for other
areas is a parameter change -- which is what probe_read_areas does.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import zf6hp as Z                           # noqa: E402
from ovtsuite.flash.tp20_transport import Tp20Channel           # noqa: E402
from mock_tp20 import MockTp20Ecu                               # noqa: E402

BLR = b"\x4e\x80\x00\x20"


def _flash(answers, seen=None):
    """Zf6hpFlash over a mock TCU, already past connect/ident/unlock so these
    exercise the probe loop rather than the session preamble (which the
    captured-replay tests in test_flash_zf6hp.py already cover)."""
    def handler(req):
        req = bytes(req)
        if seen is not None:
            seen.append(req)
        if req[:2] == bytes([Z.SID_START_SESSION, Z.SESSION_EXTENDED]):
            return bytes([Z.SID_START_SESSION + 0x40, Z.SESSION_EXTENDED])
        if req and req[0] == Z.SID_READ_MEM:
            body = answers.get(req[1])
            if body is None:
                return bytes([0x7F, Z.SID_READ_MEM, 0x31])      # requestOutOfRange
            return bytes([Z.SID_READ_MEM + 0x40]) + body
        return None

    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300 + Z.DEST, ecu_rx=0x740)
    tp = Tp20Channel(ecu, dest=Z.DEST, listen_id=Z.LISTEN_ID)
    tp.open()
    fl = Z.Zf6hpFlash(tp=tp)
    fl.connect = lambda: None
    fl.read_ecu_identification = lambda: "mock TCU"
    fl._unlocked_levels = {Z.LEVEL_READ}
    fl._stop_communication = lambda: None
    return fl


def test_a_refused_area_is_an_answer_not_an_error():
    fl = _flash({4: b"\x11" * 254, 5: b"\x22" * 254})
    res = fl.probe_read_areas(areas=[0, 4, 5, 8])
    assert res[0] is None and res[8] is None
    assert res[4] == b"\x11" * 254 and res[5] == b"\x22" * 254


def test_one_short_read_per_area_and_nothing_else():
    """The safety property: this must never write, erase or start a download."""
    seen = []
    fl = _flash({a: b"\x00" * 254 for a in range(16)}, seen)
    fl.probe_read_areas(areas=list(range(16)))
    reads = [r for r in seen if r[0] == Z.SID_READ_MEM]
    assert len(reads) == 16, "exactly one read per area, not a 64 KiB sweep"
    assert all(r == bytes([Z.SID_READ_MEM, r[1], 0, 0, Z.READ_CHUNK]) for r in reads)
    assert not [r for r in seen if r[0] in (0x34, 0x36, 0x37, 0x31, 0x2F, 0x3B)], \
        "no requestDownload / transferData / erase / write may be issued"


def test_code_is_distinguished_from_tables():
    fl = _flash({0x00: BLR + b"\x7c\x08\x02\xa6" + b"\x00" * 246,
                 0x04: bytes(range(254))})
    res = fl.probe_read_areas(areas=[0x00, 0x04])
    assert "blr" in Z.ppc_markers(res[0x00])
    assert "mflr" in Z.ppc_markers(res[0x00])
    assert Z.ppc_markers(res[0x04]) == "", "a table area must not be flagged as code"


def test_cancelling_stops_the_probe():
    fl = _flash({a: b"\x00" * 254 for a in range(16)})
    with pytest.raises(Exception):
        fl.probe_read_areas(areas=list(range(16)), is_stop=lambda: True)


def test_the_reference_image_really_has_no_code():
    """The premise of the whole exercise, checked against the file itself."""
    from ovtsuite.session import TunerSession
    path = os.path.join(os.path.dirname(__file__), "..",
                        "Reference", "Tunes", "6hp19-tdi20.6hp")
    if not os.path.exists(path):
        pytest.skip("reference tune not present")
    d = bytes(TunerSession(path).binfile.data)
    assert Z.ppc_markers(d) == "", "found PowerPC instructions in a calibration-only image"
    populated = {i // Z.BANK_SIZE for i in range(0, len(d), 0x1000) if any(d[i:i + 0x1000])}
    assert populated == set(Z.READ_AREAS), (
        "populated areas %s should be exactly READ_AREAS" % sorted(populated))
