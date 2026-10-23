# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A2L export (ovtsuite.a2l_export) and the experimental WinOLS map pack
export (ovtsuite.definitions.winols_write), each checked by reading what it
wrote back with the app's own reader: every parameter at its address, in its
shape and format, with its conversion and its axes."""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.binary import BinaryFile  # noqa: E402
from ovtsuite.definitions import a2l, winols  # noqa: E402
from ovtsuite.definitions.winols_write import write_kp  # noqa: E402
from ovtsuite.a2l_export import export_a2l  # noqa: E402
from ovtsuite.equation import Equation  # noqa: E402
from ovtsuite.model import Axis, Entity  # noqa: E402

from conftest import BINARIES  # noqa: E402


def _ent(name, kind, addr, rows=1, cols=1, bits=16, signed=False, eq="X",
         x=None, y=None, order="col", units="", **kw):
    return Entity(name=name, kind=kind, tier=1, categories=["Test"],
                  description="the %s" % name, units=units, addr=addr, rows=rows,
                  cols=cols, bits=bits, signed=signed, equation=eq,
                  source="verified", x=x, y=y, order=order, **kw)


def _image():
    img = bytearray(0x400)
    struct.pack_into(">h", img, 0x10, -60)                     # VAL
    struct.pack_into(">4H", img, 0x20, 100, 200, 300, 400)     # CUR axis
    struct.pack_into(">4H", img, 0x28, 1, 2, 3, 4)             # CUR
    struct.pack_into(">3H", img, 0x40, 10, 20, 30)             # MAP X
    struct.pack_into(">2h", img, 0x50, -5, 5)                  # MAP Y (signed)
    struct.pack_into(">6H", img, 0x60, 1, 2, 3, 4, 5, 6)       # MAP
    img[0x80:0x83] = bytes([7, 8, 9])                          # IDX
    struct.pack_into(">2H", img, 0x90, 1000, 2000)             # RAT
    img[0xA0:0xA4] = b"ABCD"                                   # TXT
    return bytes(img)


def _entities():
    bf = BinaryFile.from_bytes(_image(), "t.bin", endian=">")
    ents = [
        _ent("VAL", "constant", 0x10, signed=True, eq="X*0.1", units="Nm"),
        _ent("CUR", "curve", 0x28, cols=4, eq="X*2+1",
             x=Axis(addr=0x20, count=4, equation="X*10", units="1/min")),
        _ent("MAP", "map", 0x60, rows=2, cols=3, eq="(X)/(8192)",
             x=Axis(addr=0x40, count=3), y=Axis(addr=0x50, count=2, signed=True)),
        _ent("MAPFIX", "map", 0x60, rows=2, cols=3, order="row",
             x=Axis(addr=None, count=3, values=[1.0, 2.0, 3.0], units="gear"),
             y=Axis(addr=0x50, count=2, signed=True)),
        _ent("IDX", "indexed", 0x80, rows=3, cols=1, bits=8),
        _ent("RAT", "curve", 0x90, cols=2, eq="1000000/(X+10)",
             x=Axis(addr=None, count=2, values=[0.0, 1.0])),
        _ent("TXT", "constant", 0xA0, cols=4, bits=8, text_kind="ascii"),
    ]
    for e in ents:
        e.binfile = bf
    return ents


def _same_eq(a_text, b_text):
    a, b = Equation(a_text), Equation(b_text)
    for raw in (0, 1, 7, 100, 1000):
        x, y = a.decode(raw), b.decode(raw)
        if x == x and y == y and abs(x - y) > 1e-6 * max(1.0, abs(y)):
            return False
    return True


def test_a2l_reads_back_as_written():
    ents = _entities()
    warnings = []
    text = export_a2l(ents, "test", image_size=0x400, platform="edc16",
                      warnings=warnings)
    assert "ASAP2_VERSION" in text and a2l.is_a2l(text.encode())
    d = a2l.load(text.encode())
    assert not d.warnings
    by = {t.name: t for t in d.tables}
    assert set(by) == {"VAL", "CUR", "MAP", "MAPFIX", "IDX", "RAT", "TXT"}
    # a part number (ASCII) reads back as text, its characters counted
    assert by["TXT"].text_kind == "ascii" and by["TXT"].cols == 4
    for e in ents[:-1]:
        t = by[e.name]
        assert t.z.addr == e.addr and t.z.bits == e.bits and t.z.signed == e.signed
        assert _same_eq(t.z.equation_text(), e.equation), e.name
    assert (by["MAP"].rows, by["MAP"].cols, by["MAP"].order) == (2, 3, "col")
    assert by["MAPFIX"].order == "row"
    assert by["CUR"].x.values.addr == 0x20
    assert _same_eq(by["CUR"].x.values.equation_text(), "X*10")
    assert by["MAP"].y.values.addr == 0x50 and by["MAP"].y.values.signed
    assert by["MAPFIX"].x.fixed == [1.0, 2.0, 3.0]
    assert (by["IDX"].rows, by["IDX"].cols) == (3, 1)
    assert "RAT_FUNC" in text, "a rational conversion stays exact"
    assert text.count("/begin AXIS_PTS ") == 3, "one per stored axis, shared"
    assert "FUNCTION" in text and not warnings


def test_kp_reads_back_as_written():
    ents = _entities()
    warnings = []
    data = write_kp(ents, 0x400, title="test", warnings=warnings)
    assert winols.is_winols(data) and winols.format_version(data) == 292
    d = winols.load(data)
    assert not d.warnings
    by = {t.name: t for t in d.tables}
    assert set(by) == {"VAL", "CUR", "MAP", "MAPFIX", "IDX"}
    assert any("RAT" in w for w in warnings) and any("TXT" in w for w in warnings)
    for name in by:
        e = next(x for x in ents if x.name == name)
        t = by[name]
        assert t.z.addr == e.addr and t.z.bits == e.bits and t.z.signed == e.signed
        assert _same_eq(t.z.equation_text(), e.equation), name
    # EDC16's column-major map goes out turned: rows and axes swapped
    m = by["MAP"]
    assert (m.rows, m.cols) == (3, 2)
    assert m.x.values.addr == 0x50 and m.y.values.addr == 0x40
    assert (by["MAPFIX"].rows, by["MAPFIX"].cols) == (2, 3)
    assert by["MAPFIX"].x.fixed == [1.0, 2.0, 3.0]
    assert by["CUR"].x.values.addr == 0x20


@pytest.mark.slow
def test_a_real_image_round_trips_through_both():
    path = os.path.join(BINARIES, "03G906016LQ.bin")
    if not os.path.exists(path):
        pytest.skip("reference binary missing")
    from ovtsuite.session import TunerSession
    s = TunerSession(path)
    ents = [e for e in s.entities + list(s.variant_entities())
            if e.kind in ("map", "curve", "constant", "indexed") and not e.text_kind]
    size = len(s.binfile.data)
    d = a2l.load(export_a2l(ents, "LQ", size, platform="edc16").encode())
    got = {t.z.addr for t in d.tables}
    assert len(got & {e.addr for e in ents}) >= len({e.addr for e in ents}) - 5
    k = winols.load(write_kp(ents, size))
    assert len(k.tables) == len(ents)
