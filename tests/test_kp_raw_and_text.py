# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The WinOLS map pack writer (definitions/winols_write.py) keeps what a
WinOLS map cannot express instead of leaving it out: a conversion that is not
a factor and an offset is written raw (as WinOLS' own DAMOS import keeps it),
text as a row of hexadecimal bytes."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.definitions import winols as W  # noqa: E402
from ovtsuite.definitions.winols_write import write_kp  # noqa: E402


def _ent(name, **kw):
    d = dict(name=name, title=name, description="", kind="constant", bits=16,
             equation="X", signed=False, units="", cols=1, rows=1, x=None,
             y=None, addr=0x100, text_kind="", is_float=False, order="row",
             categories=[], category_path=[], tier=0,
             binfile=NS(endian="<", data=bytes(0x400)))
    d.update(kw)
    return NS(**d)


def _pack(*ents):
    warns = []
    data = write_kp(ents, 0x400, "t", little_endian=True, warnings=warns)
    recs = {r.name: r for r in W.parse(data).records}
    return recs, warns


def test_a_linear_conversion_is_written_as_it_is():
    recs, warns = _pack(_ent("lin", equation="X*0.1+2", units="mV"))
    r = recs["lin"]
    assert (r.factor, r.offset, r.units) == (0.1, 2.0, "mV") and not warns
    assert "[" not in r.description


def test_a_conversion_that_is_not_linear_is_written_raw_and_said():
    recs, warns = _pack(_ent("xcwFreq", equation="100/X", units="Hz"))
    r = recs["xcwFreq"]
    assert (r.factor, r.offset, r.units, r.addr) == (1.0, 0.0, "Hz", 0x100)
    assert any("xcwFreq: written raw" in w and "100/X" in w for w in warns)
    assert r.description.endswith("[value = 100/X, where X is the number shown here]")
    assert not any("left out" in w for w in warns)


def test_text_is_a_row_of_hexadecimal_bytes():
    recs, warns = _pack(_ent("xcwBHardNr", bits=8, text_kind="ascii", cols=11,
                             addr=0x200))
    r = recs["xcwBHardNr"]
    assert (r.type, r.size, r.cols, r.rows, r.numbase) == (3, 1, 11, 1, 16)
    assert r.addr == 0x200
    assert any("xcwBHardNr: written as 11 bytes" in w for w in warns)
    assert r.description.endswith("[text, shown as hexadecimal bytes]")


def test_what_has_no_stored_form_is_still_left_out():
    recs, warns = _pack(_ent("odd", bits=24))
    assert "odd" not in recs and any("odd: left out (24-bit data)" in w for w in warns)
