# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The libusb-1.0.dll the 64-bit Windows build ships (packaging/vendor/libusb).

Pinned: the vendored DLL is the one recorded in ``SHA256`` and is a 64-bit
build; the spec bundles it for the 64-bit Windows build only and drops any
other libusb the build machine happens to have; ``libusb_dll.prepare`` puts its
folder first on PATH so PyUSB loads it; the licence texts the credits name
exist; and a device libusb cannot open (Windows, not on a WinUSB driver) is
not listed as a direct OpenPort.
"""
import hashlib
import os
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

VENDOR = os.path.join(ROOT, "packaging", "vendor", "libusb")
DLL = os.path.join(VENDOR, "libusb-1.0.dll")


def test_vendored_dll_matches_its_pin_and_is_64_bit():
    with open(os.path.join(VENDOR, "SHA256"), encoding="ascii") as f:
        pinned, name = f.read().split()
    assert name == "libusb-1.0.dll"
    data = open(DLL, "rb").read()
    assert hashlib.sha256(data).hexdigest() == pinned
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", data, pe + 4)[0] == 0x8664          # AMD64


def test_the_spec_bundles_it_for_64_bit_windows_and_drops_strays():
    spec = open(os.path.join(ROOT, "packaging", "ovtsuite.spec"), encoding="utf-8").read()
    assert 'sys.platform == "win32" and sys.maxsize > 2 ** 32' in spec
    assert "binaries.append((str(_LIBUSB)" in spec
    assert "_stray_libusb" in spec


def test_credits_name_libusb_and_its_licence_texts_exist():
    from ovtsuite import credits
    c = next(c for c in credits.CREDITS if c.name == "libusb")
    assert c.license == "LGPL-2.1-or-later"
    assert c.files and all(os.path.isfile(p) for p in c.file_paths())
    assert not any("libusb-win32" in x.name for x in credits.CREDITS)


def test_find_dll_is_windows_64_bit_only(monkeypatch):
    from ovtsuite.flash import libusb_dll
    monkeypatch.setattr(sys, "platform", "linux")
    assert libusb_dll.find_dll() is None and libusb_dll.prepare() is None
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(struct, "calcsize", lambda fmt: 4)               # 32-bit interpreter
    assert libusb_dll.find_dll() is None


def test_prepare_puts_the_dll_folder_first_on_path(monkeypatch, tmp_path):
    from ovtsuite.flash import libusb_dll
    (tmp_path / "libusb-1.0.dll").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(struct, "calcsize", lambda fmt: 8)
    monkeypatch.setattr(libusb_dll, "_search_dirs", lambda: [str(tmp_path / "nope"), str(tmp_path)])
    monkeypatch.setattr(libusb_dll, "_dll_dir_handle", object())         # skip add_dll_directory
    monkeypatch.setenv("PATH", os.pathsep.join(["elsewhere", "other"]))
    assert libusb_dll.prepare() == str(tmp_path / "libusb-1.0.dll")
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path) and parts[1:] == ["elsewhere", "other"]
    libusb_dll.prepare()                                                  # idempotent
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path)) == 1


@pytest.mark.skipif(sys.platform != "win32" or struct.calcsize("P") != 8,
                    reason="the bundled DLL is Windows 64-bit")
def test_pyusb_loads_the_bundled_dll():
    usb_backend = pytest.importorskip("usb.backend.libusb1")
    from ovtsuite.flash import libusb_dll
    libusb_dll.prepare()
    assert usb_backend.get_backend() is not None


class _Dev:
    def __init__(self, exc=None):
        self.exc = exc

    def get_active_configuration(self):
        if self.exc:
            raise self.exc


def test_a_device_libusb_cannot_open_is_not_a_direct_openport(monkeypatch):
    from ovtsuite.flash import openport
    monkeypatch.setattr(sys, "platform", "win32")
    assert openport._openable(_Dev()) is True
    assert openport._openable(_Dev(NotImplementedError("not supported"))) is False
    assert openport._openable(_Dev(OSError("access"))) is True           # opens, then says why
    monkeypatch.setattr(sys, "platform", "linux")
    assert openport._openable(_Dev(NotImplementedError())) is True
