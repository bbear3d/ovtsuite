# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP in the base app: structural identification, and the auto byte-swap
round-trip (a 16-bit bench/BDM read saved back in its original order)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
_NATIVE = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP24",
                       "4B0927156FB_RS6_5HP24_deswapped.bin")


def _native():
    if not os.path.exists(_NATIVE):
        pytest.skip("4B0927156FB_RS6_5HP24_deswapped.bin not present")
    with open(_NATIVE, "rb") as fh:
        return fh.read()


def test_identifies_native_and_detects_a_swapped_read():
    from ovtsuite.platforms import zf5hp as P
    from ovtsuite.platforms.identify import identify
    d = _native()
    assert P.detect(d) and not P.byte_swapped(d)
    assert P.byte_swapped(P.swap16(d)) and not P.detect(P.swap16(d))
    idn = identify(d)
    assert idn.platform == "zf5hp" and idn.module == "zf5hp"
    assert idn.facts.get("family") == "5HP24" and idn.facts.get("variant") == "AG5 01L"


def test_a_byte_swapped_5hp_read_opens_and_saves_as_it_came(tmp_path):
    """A 16-bit-swapped bench read opens in the TCU's native big-endian order,
    is editable with its checksum corrected there, and is written back
    byte-swapped exactly as it came in."""
    from ovtsuite.platforms import zf5hp as P
    from ovtsuite.session import TunerSession
    from ovtsuite.modules import registry
    d = _native()
    p = tmp_path / "swapped.bin"
    p.write_bytes(P.swap16(d))

    s = TunerSession(str(p))
    assert s._byte_swapped and "byte-swapped" in (s.load_note or "")
    assert s.binary_identity.platform == "zf5hp"
    assert bytes(s.binfile.data) == d                      # shown native
    assert len(s.entities) == 162                          # the bundled def placed

    off = 0x70410                                          # a calibration byte
    s.binfile.write(off, s.binfile.read(off, bits=8) ^ 0x40, bits=8)
    out = tmp_path / "out.bin"
    s.save(str(out), backup=False, write_log=False)

    back = out.read_bytes()
    assert back[0x8000:0x8008] == b"TB104050"              # saved byte-swapped
    assert P.swap16(back) == bytes(s.binfile.data)         # same order it came
    cs = registry.checksum("zf5hp")
    assert cs.verify(P.swap16(back)).ok                    # corrected natively
