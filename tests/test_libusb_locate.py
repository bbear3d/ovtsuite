# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ovtsuite.flash.libusb_locate: finding libusb in MacPorts / Homebrew / the
bundle, the OVT_LIBUSB override, the PyUSB hook and the not-found text."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite.flash import libusb_locate as L  # noqa: E402


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A macOS-like machine with nothing installed and a clean environment."""
    monkeypatch.setattr(sys, "platform", "darwin")
    for v in L.ENV_VARS + ("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(L, "bundled_dirs", lambda: [])
    monkeypatch.setattr(L, "_setting_dirs", lambda: [])
    monkeypatch.setattr(L, "_system_dirs", lambda: [])
    monkeypatch.setattr(L.ctypes.util, "find_library", lambda n: None)
    monkeypatch.setattr(L, "_loadable", lambda path: None)      # the fake files are not real libraries
    monkeypatch.setitem(L._cache, "done", False)
    monkeypatch.setitem(L._cache, "path", None)
    monkeypatch.setattr(L, "_skipped", [])
    return tmp_path


def _lib(d, name="libusb-1.0.dylib"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x")
    return str(p)


def test_macports_dir_is_searched_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dirs = L._system_dirs()
    assert dirs.index("/opt/local/lib") < dirs.index("/usr/local/lib")
    assert "/opt/homebrew/lib" in dirs


def test_finds_a_library_in_a_macports_style_dir(mac, monkeypatch):
    p = _lib(mac / "opt" / "local" / "lib")
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(mac / "opt" / "local" / "lib")])
    assert L.find_libusb(refresh=True) == p
    assert L.find_library_for_pyusb("usb-1.0") == p


def test_env_override_beats_system_dirs_file_or_folder(mac, monkeypatch):
    sysd = mac / "sys"
    _lib(sysd)
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(sysd)])
    custom = _lib(mac / "custom", "libusb-1.0.0.dylib")
    monkeypatch.setenv("OVT_LIBUSB", str(mac / "custom"))
    assert L.find_libusb(refresh=True) == custom
    monkeypatch.setenv("OVT_LIBUSB", custom)
    assert L.find_libusb(refresh=True) == custom


def test_dyld_path_beats_system_dirs(mac, monkeypatch):
    _lib(mac / "sys")
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(mac / "sys")])
    p = _lib(mac / "dyld")
    monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", str(mac / "dyld"))
    assert L.find_libusb(refresh=True) == p


def test_not_found_is_none_and_message_is_helpful(mac):
    assert L.find_libusb(refresh=True) is None
    msg = L.not_found_message()
    assert "libusb" in msg and "brew install libusb" in msg
    assert "sudo port install libusb" in msg and "OVT_LIBUSB" in msg


def test_not_found_message_per_platform(monkeypatch):
    for plat, word in (("linux", "apt"), ("win32", "reinstall")):
        monkeypatch.setattr(sys, "platform", plat)
        m = L.not_found_message()
        assert "libusb" in m and word in m


def test_find_native_uses_given_dirs(mac):
    p = _lib(mac / "d", "libftd2xx.dylib")
    assert L.find_native("ftd2xx", ["libftd2xx.dylib"], [str(mac / "d")]) == p
    assert L.find_native("ftd2xx", ["libnope.dylib"], [str(mac / "d")]) is None


def test_install_backend_hooks_pyusb_once_and_never_raises(mac, monkeypatch):
    lb = pytest.importorskip("usb.backend.libusb1")
    seen = []
    fake = lambda find_library=None: seen.append(find_library) or "be"      # noqa: E731
    monkeypatch.setattr(lb, "get_backend", fake)
    # restored at teardown: install_backend() remembers the function it wrapped, and a
    # leftover fake there would answer every later L.get_backend() in the session
    monkeypatch.setitem(L._orig_get_backend, "fn", None)
    monkeypatch.setitem(L._installed, "done", False)
    p = _lib(mac / "x")
    monkeypatch.setenv("OVT_LIBUSB", p)
    L.find_libusb(refresh=True)
    assert L.install_backend() == p
    assert L._orig_get_backend["fn"] is fake
    assert lb.get_backend() == "be"
    assert seen[-1](None) == p                        # PyUSB's find_library callback
    hooked = lb.get_backend
    L.install_backend()                               # idempotent
    assert lb.get_backend is hooked
    # a real L.get_backend() goes through the wrapped function with our callback
    n = len(seen)
    assert L.get_backend() == "be"
    assert len(seen) == n + 1 and seen[-1] is L.find_library_for_pyusb


def test_a_library_that_does_not_load_is_skipped_and_reported(mac, monkeypatch):
    bad = _lib(mac / "intel")
    good = _lib(mac / "arm")
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(mac / "intel"), str(mac / "arm")])
    monkeypatch.setattr(L, "_loadable", lambda p: "incompatible architecture" if p == bad else None)
    assert L.find_libusb(refresh=True) == good
    assert (bad, "incompatible architecture") in L._skipped
    monkeypatch.setattr(L, "_loadable", lambda p: "incompatible architecture")
    assert L.find_libusb(refresh=True) is None
    msg = L.explain_absence()
    assert "not usable" in msg and bad in msg and "incompatible architecture" in msg


def test_a_miss_is_not_cached_so_a_later_install_is_found(mac, monkeypatch):
    d = mac / "late"
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(d)])
    assert L.find_libusb() is None
    p = _lib(d)
    assert L.find_libusb() == p                       # no refresh needed after a miss
    p2 = _lib(mac / "env", "libusb-1.0.0.dylib")
    monkeypatch.setenv("OVT_LIBUSB", p2)
    assert L.find_libusb() == p                       # a success is cached
    assert L.find_libusb(refresh=True) == p2


def test_explain_absence_searches_again(mac, monkeypatch):
    d = mac / "later"
    monkeypatch.setattr(L, "_system_dirs", lambda: [str(d)])
    assert "libusb" in L.explain_absence()
    _lib(d)
    assert L.explain_absence() == ""


def test_the_setting_is_read_without_the_gui_having_imported_qt(monkeypatch):
    qtcore = pytest.importorskip("PySide6.QtCore")
    monkeypatch.delitem(sys.modules, "PySide6", raising=False)

    class _S:
        def __init__(self, *a):
            pass

        def value(self, key, default=None):
            return "/somewhere/libusb" if key == L.SETTING_KEY else default

    monkeypatch.setattr(qtcore, "QSettings", _S)
    assert L._setting_dirs() == ["/somewhere/libusb"]
