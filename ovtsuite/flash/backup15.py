# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""KWHack ``.edc15bak`` full-ECU backup container (flash + EEPROM).

Byte-compatible with KWHack's ``Edc15CloneFile`` (format version 1)::

    "KWHEC15B"  8 bytes magic
    u8          format version (1)
    u8          Edc15FlashVM.Variant  (P=0, V=1, VM=2)
    i32 LE      flash length, then the flash bytes
    i32 LE      EEPROM length (512), then the EEPROM bytes
    32 bytes    SHA-256 over everything above
"""
import hashlib
import struct

MAGIC = b"KWHEC15B"
VERSION = 1
EXT = ".edc15bak"
VARIANTS = {"P": 0, "V": 1, "VM": 2}
_VARIANT_NAMES = {v: k for k, v in VARIANTS.items()}
_HASH = 32
EEPROM_LEN = 512


class BackupError(ValueError):
    pass


def is_backup(data) -> bool:
    return len(data) >= len(MAGIC) and bytes(data[:len(MAGIC)]) == MAGIC


def build(flash, eeprom, variant="V") -> bytes:
    flash, eeprom = bytes(flash), bytes(eeprom)
    if len(eeprom) != EEPROM_LEN:
        raise BackupError("EEPROM must be %d bytes; got %d" % (EEPROM_LEN, len(eeprom)))
    payload = (MAGIC + bytes([VERSION, VARIANTS[variant]])
               + struct.pack("<i", len(flash)) + flash
               + struct.pack("<i", len(eeprom)) + eeprom)
    return payload + hashlib.sha256(payload).digest()


def parse(data):
    """Return (flash, eeprom, variant_name); raises BackupError."""
    data = bytes(data)
    hdr = len(MAGIC) + 2 + 4
    if len(data) < hdr + _HASH:
        raise BackupError("File is too short to be a valid .edc15bak backup.")
    if not is_backup(data):
        raise BackupError("File does not have the KWHack .edc15bak header.")
    version, variant = data[8], data[9]
    if version != VERSION:
        raise BackupError("Unsupported .edc15bak format version %d." % version)
    if variant not in _VARIANT_NAMES:
        raise BackupError("File's stored ECU variant is invalid.")
    flash_len = struct.unpack_from("<i", data, 10)[0]
    flash_start = hdr
    if flash_len < 0 or flash_start + flash_len + 4 > len(data):
        raise BackupError("File's flash section length is invalid, or the file is truncated.")
    flash = data[flash_start:flash_start + flash_len]
    ee_len_off = flash_start + flash_len
    ee_len = struct.unpack_from("<i", data, ee_len_off)[0]
    ee_start = ee_len_off + 4
    if ee_len < 0 or ee_start + ee_len + _HASH > len(data):
        raise BackupError("File's EEPROM section length is invalid, or the file is truncated.")
    eeprom = data[ee_start:ee_start + ee_len]
    payload_len = ee_start + ee_len
    if payload_len + _HASH != len(data):
        raise BackupError("File has unexpected trailing data -- it may be corrupted.")
    if data[payload_len:] != hashlib.sha256(data[:payload_len]).digest():
        raise BackupError("File's checksum doesn't match its contents -- it may be corrupted.")
    if ee_len != EEPROM_LEN:
        raise BackupError("Expected a %d-byte EEPROM section, found %d bytes." % (EEPROM_LEN, ee_len))
    return flash, eeprom, _VARIANT_NAMES[variant]
