# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""C167 internal boot ROM (IROM) dumps: structural checks, no hardware needed.

The EDC15's application code lives in the external flash, but a large part of
the firmware does not: it calls into the C167's own 32 KB internal boot ROM,
mapped at CPU-bus 0x000000-0x007FFF. Counting ``CALLS 00H, <off>`` across the
code area of the reference images gives ~1000 calls per image into segment 0,
**99.4-99.8% of them at offsets <= 0x7FFF** and clustered below 0x3840 --
which is what fixes the ROM's size and says the library really is there. The
V4.1 checksum worker the verify driver calls (``CALLS 00H, 1584H``, i.e.
``sub_1584``) is one of those routines; :func:`ovtsuite.flash.loaders.
loader_checksum` is its already-ported form.

Reading it needs no boot mode and no boot pin: the running firmware has the ROM
mapped (it is executing out of it), so the ordinary K-line sector loader's
arbitrary-address read reaches it -- see
:meth:`ovtsuite.flash.edc15.Edc15Flash.read_irom`.

This module only looks at a dump after the fact. The checks exist because the
known failure mode of a misconfigured C167 read is NOT an error: it silently
returns every other byte, doubled, e.g. ``fa fa 70 70 fa fa 04 04`` where the
real ROM has ``fa 70 fa 04``. That is easy to mistake for a valid dump, so
:func:`check` looks for it explicitly.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: The internal ROM's CPU-bus window.
IROM_BASE = 0x000000
IROM_SIZE = 0x8000

#: C167 interrupt/trap vectors are 4-byte slots at the start of the ROM. Measured
#: against a real EDC15 ROM read (sum 0x3796): slot 0 is the reset vector and is a
#: ``JMPS`` into segment 0, but the slots after it are NOT all JMPS -- a handler
#: commonly spans two slots (``MOV R7,#<error code>`` then ``JMPS 00H,02D6H``),
#: and the interrupt vectors further in are ``BCLR IEN`` + ``JMPR``. So a run of
#: leading JMPS entries is NOT a property of a genuine dump; only the reset vector
#: is. An earlier version of this module asserted the run and called a
#: checksum-verified dump SUSPECT because of it.
JMPS = 0xFA
_VECTOR_STRIDE = 4


@dataclass
class Report:
    size: int
    version_sum: int                  #: 16-bit additive sum -- how ROM versions are told apart
    vector_entries: int               #: leading 4-byte entries that start with JMPS
    doubled_fraction: float           #: 1.0 == the every-other-byte-doubled failure
    filler_fraction: float
    #: ``(r1, r6)`` this dump produces under the ECU's own worker, and what the
    #: flash image said to expect -- both None when no image was supplied.
    rom_sum: Optional[Tuple[int, int]] = None
    expected_sum: Optional[Tuple[int, int]] = None
    problems: List[str] = field(default_factory=list)
    #: Observations that are not faults. Structural shape goes here, because the
    #: ECU's own stated checksum outranks any guess about what a ROM looks like.
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def verified(self) -> bool:
        """True only when the dump was checked against an image that states the
        expected value, and matched. Structural checks alone cannot prove a dump
        is right -- this can."""
        return (self.expected_sum is not None
                and self.rom_sum == self.expected_sum)

    def describe(self) -> str:
        if self.verified:
            verdict = "VERIFIED against the ECU's own stated checksum"
        elif self.ok:
            verdict = "looks sound (no stated checksum to compare against)"
        else:
            verdict = "SUSPECT"
        lines = [
            "C167 internal ROM dump: %s" % verdict,
            "  size            %d bytes (0x%X)" % (self.size, self.size),
            "  16-bit sum      0x%04X   (the identifier used to tell ROM versions apart)"
            % self.version_sum,
            "  vector entries  %d leading JMPS entries" % self.vector_entries,
            "  doubled bytes   %.3f of words have both bytes equal" % self.doubled_fraction,
            "  filler          %.3f of bytes are 0x00/0xFF" % self.filler_fraction,
        ]
        if self.rom_sum is not None:
            lines.append("  ECU checksum    %04X/%04X" % self.rom_sum)
        if self.expected_sum is not None:
            lines.append("  image expects   %04X/%04X   -> %s"
                         % (self.expected_sum[0], self.expected_sum[1],
                            "MATCH: this is the ROM the image was built against"
                            if self.verified else "MISMATCH"))
        for p in self.problems:
            lines.append("  PROBLEM: %s" % p)
        for n in self.notes:
            lines.append("  note: %s" % n)
        return "\n".join(lines)


def version_sum(data) -> int:
    """16-bit additive sum of the dump -- the form the community uses to
    distinguish internal-ROM versions from each other."""
    return sum(bytes(data)) & 0xFFFF


def doubled_fraction(data) -> float:
    """Fraction of 16-bit words whose two bytes are equal.

    A correct read of real code sits near 1/256. The misconfigured-bus read
    returns each byte twice, so it comes out at or near 1.0.
    """
    data = bytes(data)
    n = len(data) // 2
    if not n:
        return 0.0
    return sum(1 for i in range(n) if data[2 * i] == data[2 * i + 1]) / n


def filler_fraction(data) -> float:
    data = bytes(data)
    if not data:
        return 0.0
    return sum(1 for b in data if b in (0x00, 0xFF)) / len(data)


def vector_entries(data) -> int:
    """Leading 4-byte vector entries that start with a ``JMPS`` opcode."""
    data = bytes(data)
    n = 0
    while (n + 1) * _VECTOR_STRIDE <= len(data) and data[n * _VECTOR_STRIDE] == JMPS:
        n += 1
    return n


def rom_checksum(data) -> Tuple[int, int]:
    """``(r1, r6)`` from a zero-seeded run of the ECU's own checksum worker over
    the dump -- the same quantity the EDC15 boot path computes over its internal
    ROM."""
    from ..edc15.v41 import run       # the EDC15 module's checksum worker
    data = bytes(data)
    end = len(data) & ~1
    word = lambda a: data[a] | (data[a + 1] << 8)
    r6, r1, _ = run(word, 0, end, 0, 0)
    return r1, r6


def reset_vector_ok(data) -> bool:
    """True when slot 0 is ``JMPS 00H, xxxx`` -- the C167 reset vector.

    Unlike a count of leading JMPS slots, this is a property of the architecture
    rather than of one ROM build: the reset vector sits at 0x0000 and the EDC15
    ROM's is ``FA 00 74 03`` (``JMPS 00H, 0374H``).
    """
    data = bytes(data)
    return len(data) >= 4 and data[0] == JMPS and data[1] == 0x00


def check(data, image=None) -> Report:
    """Structural verdict on an internal-ROM dump.

    Pass ``image`` -- any EDC15 flash image that carries the ROM self-test -- to
    turn the structural guesses into a decision: the boot path checksums the ROM
    and compares it with a dword stored in flash, so the image states exactly what
    a correct dump must come to. See :func:`ovtsuite.edc15.v41.irom_check`.

    The image need not come from the same ECU, or even the same engine family: all
    17 EDC15 reference images -- VP37 and PD, both verify-driver generations --
    carry this check and state the **same** value, so they are all running the same
    internal ROM. A dump off any one of them is the ROM all of them use.
    """
    data = bytes(data)
    rep = Report(size=len(data), version_sum=version_sum(data),
                 vector_entries=vector_entries(data),
                 doubled_fraction=doubled_fraction(data),
                 filler_fraction=filler_fraction(data))
    v41 = None
    if image is not None:
        # Comparing against the value the image states needs the EDC15
        # module's emulation of the ECU's checksum worker; the structural
        # checks below do not.
        try:
            from ..edc15 import v41
        except ImportError:
            rep.notes.append("not compared with the flash image's stated ROM "
                             "checksum: the Bosch EDC15 module is not installed")
    if v41 is not None:
        site = v41.irom_check(image)
        if site is not None:
            rep.expected_sum = site.expected
            if len(data) >= site.length:
                # hash exactly the span the image's own check hashes
                rep.rom_sum = rom_checksum(data[:site.length])
        if rep.expected_sum is not None and not rep.verified:
            rep.problems.append(
                "does not match the checksum the flash image states for its "
                "internal ROM (%04X/%04X computed, %04X/%04X expected): either "
                "the read is wrong or this ROM is not the one the ECU runs"
                % ((rep.rom_sum or (0, 0)) + rep.expected_sum))
    if len(data) != IROM_SIZE:
        rep.problems.append(
            "expected 0x%X bytes, got 0x%X" % (IROM_SIZE, len(data)))
    if rep.doubled_fraction > 0.5:
        rep.problems.append(
            "every-other-byte-doubled read (%.0f%% of words have both bytes equal): the "
            "read returned each byte twice, which is the classic wrong bus/register "
            "configuration. Re-read; do not use this dump."
            % (100 * rep.doubled_fraction))
    if rep.filler_fraction > 0.9:
        rep.problems.append(
            "%.0f%% of the dump is 0x00/0xFF -- looks like an unmapped or unread window"
            % (100 * rep.filler_fraction))

    # Shape. The reset vector is a format property and worth a problem on its own
    # when there is nothing better to go on; everything softer is a note. A dump
    # the ECU's own checksum vouches for is correct whatever its shape looks like,
    # so never contradict a match.
    shape = []
    if not reset_vector_ok(data):
        shape.append(
            "slot 0 is %s, not a JMPS into segment 0 -- the C167 reset vector is, so "
            "this may not be the ROM (or the start address was wrong)"
            % (data[:4].hex(" ") if len(data) >= 4 else "missing"))
    if rep.vector_entries < 1:
        shape.append("no leading JMPS vector entry")
    if rep.verified:
        rep.notes.extend(shape)
    else:
        rep.problems.extend(shape)
    return rep
