# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC16 images: format-defined identification (base-app knowledge).

An EDC16 image carries its calibration-block descriptor, ``CAFEAFFE``, in
every external-flash image and in the decoded MPC internal flash. That is the
positive test: EDC16 used to be the fallback for anything nothing else
claimed, which forced unrelated files through the EDC16 matcher and failed
the load. Now a binary is EDC16 only when it says so.
"""

#: The EDC16 calibration-block descriptor marker.
DESCRIPTOR = b"\xCA\xFE\xAF\xFE"


def detect(data: bytes) -> bool:
    return data.find(DESCRIPTOR) != -1


#: The calibration-area marker, 0xCAFECADE stored little-endian. Every VW
#: EDC16U1/U31/U34 calibration starts with it (0x18003D on a 2 MB image).
CAL_MARKER = b"\xDE\xCA\xFE\xCA"


def find_cal_marker(data: bytes, start: int = 0, end: int = None) -> int:
    """Offset of the calibration marker within ``[start, end)``, or -1."""
    return data.find(CAL_MARKER, start, len(data) if end is None else end)
