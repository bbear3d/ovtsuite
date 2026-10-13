# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 6HP TCU images: format-defined identification (base-app knowledge).

The ZF identification block, the EDC16-descriptor exclusion and the
shift-schedule family are what make a file a 6HP image; nothing here needs
the 6HP definitions, so identification works with the ZF 6HP module absent.
"""

import re

from . import zf6hp_structure as _s

#: The ZF identification block (hp6tuner's H0068 anchor). Deliberately
#: alphanumeric: a 6HP28 image carries "JL MQX2JL" where a 6HP19 carries
#: "JL 1000JL", and a digits-only pattern misses the former outright.
_IDENT = re.compile(rb"JL [0-9A-Z]{2,8}JL")

#: The EDC16 calibration-block descriptor. Present in every EDC16 image and in
#: no ZF 6HP one, so it is the negative half of the test below.
_EDC16_DESCRIPTOR = b"\xCA\xFE\xAF\xFE"

#: Sizes seen: a 512 KB calibration extract, 1 MB (6HP19), 2 MB+ (6HP28).
_MIN_SIZE = 0x20000
_MAX_SIZE = 0x400000


def detect(data: bytes) -> bool:
    """True when ``data`` is a ZF 6HP TCU image.

    Format-defined tests, not heuristics. The EDC16 calibration descriptor
    must be absent; then either the ZF identification block is present, or
    the image carries a run of shift schedules.

    The second test is there because the first one was not enough, and the
    enlarged OEM corpus is what showed it. Two 448 KB D3 A8 images carry no
    identification block at all -- no marker, no variant-coding block, none
    of the conventions the rest of the corpus has -- and they are complete
    calibrations: 32 shift schedules and 4 TCC maps in contiguous runs,
    8-bit. Without a second test the marker rejected them, the file fell
    through to the EDC16 path, and the load failed outright. A whole class of
    image could not be opened.

    The structural test costs nothing in false positives: **zero** shift-form
    maps appear in any of the sixteen EDC16 and ten EDC15 reference images, at
    either width. A run of consecutive index-run maps is not something an ECU
    image produces by accident, which is exactly why the marker could be
    dropped as a requirement rather than merely supplemented.
    """
    if not _MIN_SIZE <= len(data) <= _MAX_SIZE:
        return False
    if data.find(_EDC16_DESCRIPTOR) != -1:
        return False
    if _IDENT.search(data) is not None:
        return True
    return has_shift_family(data)


#: A shift schedule's index run starts at 1, a TCC schedule's at 0 -- that is
#: the family split, and it is in the data rather than in a shape list.
SHIFT_BASE = 1

#: A shift schedule is 12 columns by 10 rows on all 50 images measured. The
#: floor here is well under that and is a filter, not an assertion: it keeps
#: short base-1 runs (gear lists, mode tables) out of the family without
#: requiring the family to keep the shape it happens to have.
MIN_SHIFT_COLS = 10
MIN_SHIFT_ROWS = 8


#: Consecutive shift schedules that make an image a ZF 6HP image. The
#: smallest family seen is 32 and the largest 104, so eight is a long way
#: below anything real; what it has to clear is the other direction, and the
#: measurement there is unambiguous -- **zero** shift-form maps in any of the
#: sixteen EDC16 and ten EDC15 reference images, at either width. A run of
#: consecutive index-run maps is not something an ECU image produces by
#: accident.
MIN_SCHEDULES = 8


def has_shift_family(data: bytes, least: int = MIN_SCHEDULES) -> bool:
    """Does this image carry a run of shift schedules?

    The second format-defined way to recognise a 6HP image, and the only one
    that works on the two 448 KB images in the OEM corpus: they carry no
    identification block at all, so the marker test rejected them and the
    whole file fell through to the EDC16 path, where it failed to load. They
    are complete calibrations -- 32 schedules and 4 TCC maps in contiguous
    runs -- and the format says so without the marker's help.

    Stops at the first run long enough rather than measuring the family, so
    a 6HP image answers in milliseconds. A file that is not one is walked
    once per width, which is the price of not turning a load into a guess.
    """
    for bits, step in ((16, 2), (8, 1)):
        run, end = 0, -1
        for a in range(0, len(data) - 8, step):
            f = _s.try_shift_map(data, a, bits)
            if f is None or f.index_base != SHIFT_BASE \
                    or f.cols < MIN_SHIFT_COLS or f.rows < MIN_SHIFT_ROWS:
                continue
            run = run + 1 if end >= 0 and f.addr - end <= 8 else 1
            end = f.end
            if run >= least:
                return True
    return False
