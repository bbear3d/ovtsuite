# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""MOVBZ/MOVBS Rw,Rb (C0/D0 mn): the byte register is the HIGH nibble.
038906019GQ 0x3963A reads ``C0 84`` right after ``MOVB RL4,[R0+#15H]`` and
then indexes with R4: it is MOVBZ R4,RL4."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.c166dis import decode_all, format_listing  # noqa: E402


def test_movbz_operands():
    out = format_listing(decode_all(bytes([0xC0, 0x84, 0xC0, 0x8D, 0xD0, 0x21]), 0, 0))
    assert "MOVBZ   R4, RL4" in out
    assert "MOVBZ   R13, RL4" in out
    assert "MOVBS   R1, RL1" in out
