# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS ``.olsx`` (encrypted export): the cipher, the container, the readers.

The synthetic tests build an .olsx with the cipher's inverse written out here;
the real-file ones use the EDC17 Tiguan export in Reference/ (git-ignored)."""

import hashlib
import os
import struct
import zlib

import pytest

from ovtsuite.definitions import olsx
from ovtsuite.definitions.model import DefinitionError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIGUAN = os.path.join(
    ROOT, "Reference", "EDC17", "DAMOS", "EDC17CP14_P598_C598G3WB",
    "WinOLS (VW Tiguan (Original) - G3WBPKBS1000-03L906022MK-8441-CAGA).olsx")
STUB = b"\x0b\x00\x00\x00WinOLS File\x00" + struct.pack("<I", 597) + b"\x00" * 300

M = 0xFFFFFFFF
D = 0x9E3779B9


def _encipher(buf: bytes, key: bytes = olsx.KEY) -> bytes:
    """The inverse of :func:`olsx.decipher` (what WinOLS does when it exports)."""
    n = len(buf) // 4
    v = list(struct.unpack("<%dI" % n, buf[:n * 4]))
    k = struct.unpack("<4I", key)
    s = 0
    for _ in range(6 + 52 // n):
        s = (s + D) & M
        e = (s >> 2) & 3
        for p in range(n):
            z = v[p - 1]
            v[p] = (v[p] + ((((z << 4) ^ (z >> 5)) + z) ^ (k[(p & 3) ^ e] + s))) & M
    return struct.pack("<%dI" % n, *v) + buf[n * 4:]


def _export(ols: bytes, key: bytes = olsx.KEY) -> bytes:
    stream = zlib.compress(ols, 9)
    return _encipher(stream + hashlib.md5(stream).digest(), key)


def _stub_with_size_residue(r: int) -> bytes:
    """A stub whose export is ``r`` bytes beyond a whole number of words."""
    noise = hashlib.sha256(b"x").digest() + hashlib.sha256(b"y").digest()
    for extra in range(len(noise)):         # each byte of noise adds one to the stream
        ols = STUB + noise[:extra]
        if len(_export(ols)) % 4 == r:
            return ols
    raise AssertionError("no stub found")


@pytest.mark.parametrize("r", [0, 1, 2, 3])
def test_a_synthetic_export_is_read_back(r):
    # a size that is not a multiple of 4 leaves the last 1 - 3 bytes as they are
    ols = _stub_with_size_residue(r)
    blob = _export(ols)
    assert len(blob) % 4 == r
    assert olsx.decrypt(blob) == ols


def test_both_implementations_agree():
    blob = _export(STUB * 40)
    try:
        import numpy  # noqa: F401
    except ImportError:
        pytest.skip("numpy is not installed")
    assert olsx._decrypt_py(blob) == olsx._decrypt_np(blob)


def test_another_key_is_reported_as_protected():
    blob = _export(STUB, key=bytes(range(16)))
    with pytest.raises(DefinitionError, match="password"):
        olsx.decrypt(blob)


def test_noise_and_short_files_are_refused():
    with pytest.raises(DefinitionError):
        olsx.decrypt(bytes(range(256)) * 20)
    with pytest.raises(DefinitionError):
        olsx.decrypt(b"short")


def test_a_damaged_stream_fails_the_checksum():
    stream = zlib.compress(STUB, 9)
    blob = _encipher(stream + b"\x00" * 16)         # a wrong md5
    with pytest.raises(DefinitionError, match="checksum"):
        olsx.decrypt(blob)


def test_definitions_load_reads_an_olsx_by_its_type():
    from ovtsuite import definitions
    assert "olsx" in definitions.TYPES
    with pytest.raises(DefinitionError):
        definitions.load(_export(b"not a winols file"), "olsx")


@pytest.mark.slow
def test_the_tiguan_export_is_a_597_project():
    if not os.path.isfile(TIGUAN):
        pytest.skip("sample missing: %s" % TIGUAN)
    from ovtsuite.definitions import winols
    out = olsx.decrypt(open(TIGUAN, "rb").read())
    assert winols.is_winols(out) and winols.format_version(out) == 597
    assert len(out) == 28084637
    wf = winols.parse(out)
    assert wf.problems == [] and len(wf.records) == wf.declared > 20000


@pytest.mark.slow
def test_the_tiguan_export_loads_as_a_definition_and_imports():
    if not os.path.isfile(TIGUAN):
        pytest.skip("sample missing: %s" % TIGUAN)
    from ovtsuite import definitions
    from ovtsuite.project import importers
    from ovtsuite.project import Project
    data = open(TIGUAN, "rb").read()
    defn = definitions.load(data, "olsx")
    assert len(defn.tables) > 20000 and defn.info["model"] == "Tiguan"
    proj = Project("t")
    s = importers.import_bytes(proj, data, os.path.basename(TIGUAN))
    assert s is not None
