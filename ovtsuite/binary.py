# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Binary access for ECU images.

Adapted from hp6tuner.binary: the MPC5xx (EDC16) stores multi-byte values
big-endian, same as the ZF 6HP TCUs, so ``endian`` defaults to ``">"``.
EDC15 (C167) images are little-endian; they are opened with ``endian="<"``
and every read/write honours that byte order. Generalized to 8/16/32-bit
reads including signed.
"""

import os
import struct


class BinaryFile:
    """A mutable view of an ECU binary image."""

    def __init__(self, data: bytes, name: str = "", endian: str = ">"):
        self.data = bytearray(data)
        self.name = name
        self.endian = endian          # ">" big-endian (EDC16), "<" little (EDC15)

    @classmethod
    def load(cls, path, endian: str = None):
        with open(path, "rb") as fh:
            data = fh.read()
        return cls.from_bytes(data, os.path.basename(path), endian)

    @classmethod
    def from_bytes(cls, data: bytes, name: str, endian: str = None):
        """Like :meth:`load`, but from bytes already in hand -- e.g. a
        chunk decoded out of a .dim container (see ``ovtsuite.dim``), where
        there is no single file on disk to read."""
        if endian is None:
            from .platforms.edc15 import detect as _is_edc15
            endian = "<" if _is_edc15(data) else ">"
        return cls(data, name=name, endian=endian)

    @property
    def little_endian(self) -> bool:
        return self.endian == "<"

    def __len__(self) -> int:
        return len(self.data)

    # -- reads ---------------------------------------------------------------
    def u8(self, addr: int) -> int:
        return self.data[addr]

    def u16(self, addr: int) -> int:
        return struct.unpack_from(self.endian + "H", self.data, addr)[0]

    def u32(self, addr: int) -> int:
        return struct.unpack_from(self.endian + "I", self.data, addr)[0]

    def u16s(self, addr: int, count: int):
        return list(struct.unpack_from(self.endian + "%dH" % count, self.data, addr))

    def read(self, addr: int, bits: int = 16, signed: bool = False) -> int:
        """Read one element of the given width."""
        if bits == 8:
            v = self.data[addr]
        elif bits == 32:
            v = self.u32(addr)
        else:
            v = self.u16(addr)
        if signed:
            half = 1 << (bits - 1)
            if v >= half:
                v -= 1 << bits
        return v

    def read_many(self, addr: int, count: int, bits: int = 16,
                  signed: bool = False):
        step = bits // 8
        return [self.read(addr + i * step, bits, signed) for i in range(count)]

    # -- writes --------------------------------------------------------------
    def write(self, addr: int, value: int, bits: int = 16) -> None:
        """Write one raw (already-encoded) unsigned element."""
        if bits == 8:
            self.data[addr] = int(value) & 0xFF
        elif bits == 32:
            struct.pack_into(self.endian + "I", self.data, addr, int(value) & 0xFFFFFFFF)
        else:
            struct.pack_into(self.endian + "H", self.data, addr, int(value) & 0xFFFF)

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(bytes(self.data))
