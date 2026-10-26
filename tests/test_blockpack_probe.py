# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The probe on the bytes the ECUs really sent (docs/LOGGING_BLOCKS_PLAN.md,
Phase 0): the LQ's group 17 from Reference/Logs/VCDS-Autoscan.log, an empty
HVAC group from HVACallmeasuringblocks.log, and the EDC15 KW1281 empty slot
``11 20 20`` from the GN's cell builder (0A:A746)."""
from ovtsuite.datalog.blockpack.probe import GroupExpectation, check, check_group

KWP_EMPTY = (0x25, 0x00)
KW1281_EMPTY = (0x11, 0x20)


def cells(hexstr, strip_pad=True):
    """The cells ``flash.diag.parse_cells`` makes of a KWP2000 0x61 body
    (trailing 0x25 cells stripped)."""
    try:
        from ovtsuite.flash.diag import parse_cells
        return parse_cells(bytes.fromhex(hexstr), drop_echo=False) if strip_pad else None
    except Exception:
        b = bytes.fromhex(hexstr)
        out = [(b[i], b[i + 1], b[i + 2]) for i in range(0, len(b) - 2, 3)]
        while strip_pad and out and out[-1][0] == 0x25:
            out.pop()
        return out


LQ_G17 = "10ff00 10ff46 10ff60 10ff60 250000 250000 250000 250000".replace(" ", "")
HVAC_EMPTY = "25007e 25007e 25007e 25007e".replace(" ", "")


def raw(hexstr):
    b = bytes.fromhex(hexstr.replace(" ", ""))
    return [(b[i], b[i + 1], b[i + 2]) for i in range(0, len(b) - 2, 3)]


def test_lq_group_17_matches_its_esb_fingerprints():
    exp = GroupExpectation(17, tuple((s, (0x10, 0xFF)) for s in range(4)))
    assert check_group(cells(LQ_G17), exp, KWP_EMPTY) is None


def test_a_cell_written_into_the_padded_half_fails_when_the_ecu_lacks_it():
    # group 17 as the stock ECU sends it, but a cell was built in slot 5
    exp = GroupExpectation(17, ((4, (0x01, 0xC8)),))
    why = check_group(cells(LQ_G17), exp, KWP_EMPTY)
    assert why and "missing" in why                # the padding was stripped
    why = check_group(raw(LQ_G17), exp, KWP_EMPTY)  # ...or arrives as a pad cell
    assert why and "empty" in why


def test_empty_group_and_refusal_fail():
    exp = GroupExpectation(120, ((0, (0x01, 0xC8)),))
    assert "padding" in check_group(cells(HVAC_EMPTY), exp, KWP_EMPTY)
    assert "refused" in check_group(None, exp, KWP_EMPTY)


def test_kw1281_empty_block_answers_four_cells_and_still_fails():
    empty_block = raw("112020 112020 112020 112020")
    exp = GroupExpectation(36, ((0, (0x01, 0xC8)), (1, (0x07, 0x0A))))
    why = check_group(empty_block, exp, KW1281_EMPTY)
    assert why and "empty" in why


def test_formula_and_scaling_mismatch():
    got = raw("01c832 070a10")
    assert "formula" in check_group(got, GroupExpectation(1, ((0, (0x05, 0xC8)),)), KW1281_EMPTY)
    assert "scaling" in check_group(got, GroupExpectation(1, ((0, (0x01, 0x64)),)), KW1281_EMPTY)


def test_text_formula_ignores_the_length_byte():
    # LQ group 81: formula 5F, A = text length 0x28, while the ESB norm is 0x01
    got = raw("5f2858")
    assert check_group(got, GroupExpectation(81, ((0, (0x5F, None)),)), KWP_EMPTY) is None


def test_answer_only_groups_and_the_overall_result():
    read = {1: raw("01c832"), 120: None}
    res = check(read, [GroupExpectation(1), GroupExpectation(120, ((0, (0x01, 0xC8)),))],
                KWP_EMPTY)
    assert not res.ok and set(res.failed) == {120}
    assert "block 120" in res.describe()
    ok = check({1: raw("01c832")}, [GroupExpectation(1)], KWP_EMPTY)
    assert ok.ok and ok.checked == [1]


def test_a_filled_stock_group_that_answers_stays_usable_for_its_own_cells():
    """Review fix: on an ECU without the build, a filled stock group answers
    its stock cells and pads where cells were added. It fails the probe but
    must NOT be avoided by the usual blocks -- they read its stock cells."""
    from ovtsuite.datalog.blockpack.manifest import CellRecord, LayoutManifest
    from ovtsuite.datalog.blockpack.probe import expectations, unserved
    m = LayoutManifest("edc16", cells=[CellRecord(10, 4, 7, "x", fid=0x01, a=0xC8, kind="fill"),
                                       CellRecord(121, 0, 8, "y", fid=0x01, a=0xC8)])
    read = {10: [(0x01, 0xC8, 0x20)] * 4,             # stock cells, the added one stripped
            121: None,                                # a new group the ECU refuses
            1: []}                                    # read as it is: answered nothing
    res = check(read, expectations(m, [1, 10, 121]), KWP_EMPTY)
    assert set(res.failed) == {1, 10, 121}
    assert unserved(res, read, m) == [1, 121]
