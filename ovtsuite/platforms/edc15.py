# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC15 images: format-defined identification (base-app knowledge)."""

#: The V4.1 dataset signature every EDC15 calibration dataset starts with.
DATASET_SIG = b"\xF9\x67" + b"\xFF" * 6 + b"V4.1"


def detect(data: bytes) -> bool:
    """True when ``data`` is an EDC15 V4.1 image (>= 1 dataset signature on a
    4 KB boundary and a plausible C167 flash size)."""
    if len(data) < 0x40000 or len(data) > 0x100000:
        return False
    for off in range(0, len(data) - len(DATASET_SIG), 0x1000):
        if data[off:off + len(DATASET_SIG)] == DATASET_SIG:
            return True
    return False


def datasets(data: bytes):
    """Offsets of every V4.1 dataset signature (on 4 KB boundaries)."""
    return [off for off in range(0, len(data) - len(DATASET_SIG), 0x1000)
            if data[off:off + len(DATASET_SIG)] == DATASET_SIG]
