# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""diff_outside_calibration: raw byte differences outside the calibration
region (the program/boot area sharing the same image), surfaced by
"Show differences" in addition to the identity-based parameter diff.

Slow: each case loads a full TunerSession.
"""

import os

import pytest

from ovtsuite.session import TunerSession
from ovtsuite import patch as P

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016AB.bin")),
    reason="reference files not present")

PRIMARY = "038906016AB.bin"


def _session():
    path = os.path.join(BIN, PRIMARY)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % PRIMARY)
    return TunerSession(path)


@needs_ref
def test_no_program_area_diff_between_identical_loads():
    a = _session()
    b = _session()
    assert P.diff_outside_calibration(a, b) == []


@needs_ref
def test_finds_a_byte_changed_before_the_calibration_region():
    a = _session()
    b = _session()
    lo, _hi = a.region
    assert lo > 0, "test needs a nonzero calibration-region start"
    addr = lo - 1
    orig = bytes(b.binfile.data[addr:addr + 1])
    flipped = bytes([orig[0] ^ 0xFF])
    b.edit_bytes(addr, flipped)

    rows = P.diff_outside_calibration(a, b)
    assert any(label == "Program/boot area" and r_addr <= addr < r_addr + length
              for label, r_addr, length in rows)


@needs_ref
def test_finds_a_byte_changed_after_the_calibration_region():
    a = _session()
    b = _session()
    _lo, hi = a.region
    assert hi < len(a.binfile.data), "test needs room after the calibration region"
    addr = hi
    orig = bytes(b.binfile.data[addr:addr + 1])
    flipped = bytes([orig[0] ^ 0xFF])
    b.edit_bytes(addr, flipped)

    rows = P.diff_outside_calibration(a, b)
    assert any(label == "Program/boot area" and r_addr <= addr < r_addr + length
              for label, r_addr, length in rows)


@needs_ref
def test_a_change_inside_the_calibration_region_is_not_reported():
    a = _session()
    b = _session()
    lo, hi = a.region
    assert hi > lo
    addr = lo
    orig = bytes(b.binfile.data[addr:addr + 1])
    flipped = bytes([orig[0] ^ 0xFF])
    b.edit_bytes(addr, flipped)

    rows = P.diff_outside_calibration(a, b)
    assert rows == []


@needs_ref
def test_length_mismatch_is_skipped_not_guessed(tmp_path):
    a = _session()
    b = _session()
    out = str(tmp_path / "shorter.bin")
    with open(out, "wb") as fh:
        fh.write(bytes(b.binfile.data)[:-4])   # truncate -- different length
    # a length-mismatched "comparison" isn't a real TunerSession target here;
    # exercise the guard directly against two byte-strings of different length
    # via a couple of tiny stand-ins.
    class _Stub:
        def __init__(self, data, region):
            self.binfile = type("B", (), {"data": data})()
            self.region = region
    short = _Stub(bytes(b.binfile.data)[:-4], a.region)
    assert P.diff_outside_calibration(a, short) == []


# ---------------------------------------------------------------------------
# .dim / .sgo / .edc16 containers: MPC (internal flash) and EEPROM regions,
# read uniformly through TunerSession.region_buffers() (see diff_outside_
# calibration's docstring for why that's the right seam to hook).
# ---------------------------------------------------------------------------

DIM_A = os.path.join(BIN, "Encoded-MPC", "stock.dim")
DIM_B = os.path.join(BIN, "Encoded-MPC", "test.dim")
needs_dim_pair = pytest.mark.skipif(
    not (os.path.exists(DIM_A) and os.path.exists(DIM_B)),
    reason="reference .dim pair not present")

SGO_PATH = os.path.join(os.path.dirname(__file__), "..", "Reference",
                        "_incoming", "03G906016LQ_0971.sgo")
needs_sgo = pytest.mark.skipif(
    not os.path.exists(SGO_PATH), reason="reference .sgo not present")


@needs_dim_pair
def test_dim_container_reports_mpc_and_eeprom_differences():
    a = TunerSession(DIM_A)
    b = TunerSession(DIM_B)
    rows = P.diff_outside_calibration(a, b)
    labels = {label for label, _addr, _len in rows}
    # two genuinely different .dim tunes -- expect real differences in both
    # the MPC (internal flash) and EEPROM regions, not just external flash.
    assert "MPC (internal flash)" in labels
    assert "EEPROM" in labels


@needs_dim_pair
def test_dim_container_regions_match_a_session_identical_to_itself():
    a = TunerSession(DIM_A)
    a2 = TunerSession(DIM_A)
    assert P.diff_outside_calibration(a, a2) == []


@needs_sgo
def test_sgo_container_reports_mpc_differences(tmp_path):
    from ovtsuite import sgo as _sgo

    with open(SGO_PATH, "rb") as fh:
        raw = fh.read()
    c = _sgo.parse(raw, "03G906016LQ_0971.sgo")
    mpc_sec = next((s for s in c.sections
                    if s.addr == 0x404000 and s.plaintext is not None), None)
    assert mpc_sec is not None, "test needs an .sgo with a decodable MPC section"

    flipped = bytearray(mpc_sec.plaintext)
    flipped[0] ^= 0xFF
    c2 = _sgo.replace_section(c, 0x404000, bytes(flipped))
    out = str(tmp_path / "modified.sgo")
    with open(out, "wb") as fh:
        fh.write(_sgo.to_bytes(c2))

    a = TunerSession(SGO_PATH)
    b = TunerSession(out)
    rows = P.diff_outside_calibration(a, b)
    labels = {label for label, _addr, _len in rows}
    assert "MPC (internal flash)" in labels
    # this format never carries EEPROM -- region_buffers() shouldn't invent one
    assert "EEPROM" not in labels


# ---------------------------------------------------------------------------
# checksum exemption + block filtering (diff_gaps / diff_outside_calibration)
# ---------------------------------------------------------------------------

def test_diff_gaps_excludes_checksum_bytes_by_default():
    a = _session()
    b = _session()
    excl = P.diff_gaps(a, b)
    incl = P.diff_gaps(a, b, exclude_checksums=False)
    assert len(excl) <= len(incl)


@needs_ref
def test_diff_gaps_with_real_comparison_drops_a_checksum_covered_gap():
    comparison_path = os.path.join(BIN, "038997016R_remap.bin")
    if not os.path.exists(comparison_path):
        pytest.skip("comparison reference binary not present")
    a = _session()
    b = TunerSession(comparison_path)
    excl = P.diff_gaps(a, b)
    incl = P.diff_gaps(a, b, exclude_checksums=False)
    assert len(excl) < len(incl)


def test_diff_outside_calibration_blocks_filter_restricts_labels():
    a = _session()
    b = _session()
    rows = P.diff_outside_calibration(a, b, blocks=[P.PROGRAM_BLOCK])
    assert all(label == P.PROGRAM_BLOCK for label, _addr, _len in rows)


def test_diff_outside_calibration_empty_blocks_yields_nothing():
    a = _session()
    b = _session()
    assert P.diff_outside_calibration(a, b, blocks=[]) == []


@needs_dim_pair
def test_diff_outside_calibration_eeprom_checksum_exclusion():
    a = TunerSession(DIM_A)
    b = TunerSession(DIM_B)
    excl = P.diff_outside_calibration(a, b, blocks=[P.EEPROM_BLOCK])
    incl = P.diff_outside_calibration(a, b, blocks=[P.EEPROM_BLOCK],
                                      exclude_checksums=False)
    assert len(excl) < len(incl)


@needs_dim_pair
def test_diff_outside_calibration_mpc_block_filter_excludes_other_labels():
    a = TunerSession(DIM_A)
    b = TunerSession(DIM_B)
    rows = P.diff_outside_calibration(a, b, blocks=[P.MPC_BLOCK])
    assert rows and all(label == P.MPC_BLOCK for label, _addr, _len in rows)
