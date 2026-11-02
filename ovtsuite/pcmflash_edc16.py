# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The one-file "full" reads of an EDC16 (MPC564 + M58BW016) from PCMFlash.

PCMFlash saves an EDC16 read as separate files -- ``..._ext_...`` (the 2 MB
external flash), ``..._micro_...`` (the MPC564's internal flash) and
``..._eeprom_...`` (the 4 KB serial EEPROM) -- and also as ONE ``..._flash_...``
file that is those images laid end to end: the external flash first, then the
internal flash. Nothing in such a file says where one image stops; this
module finds the parts from their sizes and the descriptor every Bosch
program block carries (``FADECAFE CAFEAFFE`` at +0x3C of the block), and puts
a file back together the way it came.

The internal flash is stored as PCMFlash reads it: 0x80000 bytes of flash
(``0x80200`` with the micro's 512-byte shadow row behind it, ``0x81000`` as
the ``.mpc`` samples are padded). The app works with it as the 0x81000 image
(:data:`MPC_SIZE`) its checksum code and flasher expect; :class:`Layout`
remembers how long the file's own part was, so an export or a save gives back
exactly the length it was read with.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

EXT_SIZE = 0x200000             # the M58BW016's 2 MB
MPC_SIZE = 0x81000              # the internal flash as the app works with it
EEPROM_SIZE = 0x1000            # the 95320 serial EEPROM
#: the lengths an internal-flash part is seen with: the bare flash, with the
#: shadow row behind it (PCMFlash's micro files), and the .mpc image
MPC_STORED = (0x80000, 0x80200, 0x81000)

_DESC = b"\xFA\xDE\xCA\xFE\xCA\xFE\xAF\xFE"      # FADECAFE CAFEAFFE
_EXT_DESC_AT = 0x4003C          # the program block's descriptor in the 2 MB image
_MPC_DESC_AT = 0x403C           # ... and in the internal flash

PARTS = ("ext", "mpc", "eeprom")

#: the lengths a combined file can have (a quick test before reading one)
COMBINED_SIZES = frozenset(EXT_SIZE + m + e for m in MPC_STORED
                           for e in (0, EEPROM_SIZE))


@dataclass(frozen=True)
class Layout:
    """How the parts sit in a combined file: ``order`` (part names, first to
    last) and ``mpc_len``, the stored length of the internal-flash part."""
    order: Tuple[str, ...]
    mpc_len: int

    def sizes(self) -> Dict[str, int]:
        return {"ext": EXT_SIZE, "mpc": self.mpc_len, "eeprom": EEPROM_SIZE}

    def describe(self) -> str:
        return " + ".join("%s (0x%X)" % (n, self.sizes()[n]) for n in self.order)


@dataclass
class Split:
    """A combined file taken apart. ``mpc`` is the 0x81000 working image
    (the stored part padded with 0xFF); ``eeprom`` is None without one."""
    layout: Layout
    ext: bytes
    mpc: bytes
    eeprom: Optional[bytes]

    def parts(self) -> Dict[str, bytes]:
        """``{"flash": ..., "mpc": ..., "eeprom": ...}`` -- the project
        member names, only for the parts the file holds."""
        out = {"flash": self.ext, "mpc": self.mpc}
        if self.eeprom is not None:
            out["eeprom"] = self.eeprom
        return out


def _orders(with_eeprom: bool) -> List[Tuple[str, ...]]:
    # the order PCMFlash writes first; the others are tried only when it
    # does not fit the bytes
    names = PARTS if with_eeprom else PARTS[:2]
    first = tuple(names)
    rest = [p for p in itertools.permutations(names) if p != first]
    return [first] + rest


def _hits(data: bytes, lay: Layout) -> int:
    """How many of the two program descriptors the layout finds in place."""
    off, n = 0, 0
    for name in lay.order:
        if name == "ext" and data[off + _EXT_DESC_AT:off + _EXT_DESC_AT + 8] == _DESC:
            n += 1
        elif name == "mpc" and data[off + _MPC_DESC_AT:off + _MPC_DESC_AT + 8] == _DESC:
            n += 1
        off += lay.sizes()[name]
    return n


def detect(data: bytes) -> Optional[Layout]:
    """The layout of ``data`` if it is a combined EDC16 read, else None.

    A combined file is the 2 MB external flash and an internal-flash part
    (and perhaps the EEPROM) and nothing else, so its length is one of a
    handful; the descriptors then say which part is where (and, for the
    length that an internal image padded to 0x81000 shares with a 0x80000 one
    plus an EEPROM, whether the last 4 KB is the EEPROM at all: blank padding
    is not one). A file whose parts show neither descriptor is not taken for
    one."""
    n = len(data)
    best, best_hits = None, 0
    for mpc_len in MPC_STORED:
        for ee in (False, True):
            if n != EXT_SIZE + mpc_len + (EEPROM_SIZE if ee else 0):
                continue
            for order in _orders(ee):
                lay = Layout(order, mpc_len)
                h = _hits(data, lay)
                if h > best_hits:
                    best, best_hits = lay, h
                if h == 2:
                    break
    if best is None:
        return None
    if "eeprom" in best.order and best.mpc_len == 0x80000:
        # the same length as an 0x81000 internal image: it is the EEPROM only
        # when those 4 KB hold something
        ee = _slice(data, best, "eeprom")
        if not any(b != 0xFF for b in ee):
            return Layout(tuple(p for p in best.order if p != "eeprom"), 0x81000)
    return best


def _slice(data: bytes, lay: Layout, name: str) -> bytes:
    off = 0
    for p in lay.order:
        if p == name:
            return bytes(data[off:off + lay.sizes()[p]])
        off += lay.sizes()[p]
    raise KeyError(name)


def split(data: bytes) -> Optional[Split]:
    """``data`` taken apart, or None when it is not a combined read."""
    lay = detect(data)
    if lay is None:
        return None
    mpc = _slice(data, lay, "mpc")
    ee = _slice(data, lay, "eeprom") if "eeprom" in lay.order else None
    return Split(lay, _slice(data, lay, "ext"),
                 mpc.ljust(MPC_SIZE, b"\xFF"), ee)


def split_file(path) -> Optional[Split]:
    """The file at ``path`` taken apart, or None when it is not a combined
    read (its length alone rules out nearly every file without reading it)."""
    import os
    try:
        if os.path.getsize(path) not in COMBINED_SIZES:
            return None
        with open(path, "rb") as fh:
            return split(fh.read())
    except OSError:
        return None


def join(lay: Layout, ext: bytes, mpc: bytes,
         eeprom: Optional[bytes] = None) -> bytes:
    """The combined file for ``lay`` from its parts: ``mpc`` is cut back to
    the length the file's own internal-flash part had."""
    if len(ext) != EXT_SIZE:
        raise ValueError("the external flash must be 0x%X bytes, not 0x%X"
                         % (EXT_SIZE, len(ext)))
    if "eeprom" in lay.order and (eeprom is None or len(eeprom) != EEPROM_SIZE):
        raise ValueError("the EEPROM must be 0x%X bytes" % EEPROM_SIZE)
    mpc = bytes(mpc)[:lay.mpc_len].ljust(lay.mpc_len, b"\xFF")
    parts = {"ext": bytes(ext), "mpc": mpc, "eeprom": bytes(eeprom or b"")}
    return b"".join(parts[p] for p in lay.order)

