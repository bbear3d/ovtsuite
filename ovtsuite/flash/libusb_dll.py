# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Make PyUSB find libusb: the DLL that ships with the Windows build, or on
macOS / Linux the library :mod:`libusb_locate` finds (MacPorts, Homebrew, bundled).

PyUSB (and so pyftdi) locates libusb through ``ctypes.util.find_library``,
which on Windows searches ``PATH`` only. The 64-bit frozen build carries the
DLL next to its other libraries (``_internal``), and a source checkout has it
under ``packaging/vendor/libusb``; :func:`prepare` puts that folder first on
``PATH`` so the bundled copy is the one loaded, whatever else is installed.

Off Windows :func:`prepare` only installs the locator's PyUSB hook; in a 32-bit
interpreter (the shipped DLL is 64-bit; the 32-bit build exists only for the
J2534 bridge and never opens USB itself).
"""
import os
import struct
import sys
from typing import List, Optional

DLL_NAME = "libusb-1.0.dll"

_dll_dir_handle = None                      # keeps os.add_dll_directory alive


def _search_dirs() -> List[str]:
    from . import libusb_locate
    return libusb_locate.bundled_dirs()


def find_dll() -> Optional[str]:
    """Full path of the bundled libusb-1.0.dll, or None (not Windows, not a
    64-bit interpreter, or the file is not there)."""
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        return None
    for d in _search_dirs():
        p = os.path.join(d, DLL_NAME)
        if os.path.isfile(p):
            return p
    return None


def prepare() -> Optional[str]:
    """Put the bundled DLL's folder first on ``PATH`` (and on the DLL search
    path) so PyUSB loads it. Safe to call any number of times; returns the DLL
    path, or None when there is nothing to do."""
    global _dll_dir_handle
    from . import libusb_locate
    path = find_dll()
    if path is None:
        libusb_locate.install_backend()     # macOS / Linux: MacPorts, Homebrew, bundled copy
        return None
    libusb_locate.install_backend()
    folder = os.path.dirname(path)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if not parts or os.path.normcase(parts[0]) != os.path.normcase(folder):
        os.environ["PATH"] = os.pathsep.join([folder] + [p for p in parts if p])
    if _dll_dir_handle is None and hasattr(os, "add_dll_directory"):
        try:
            _dll_dir_handle = os.add_dll_directory(folder)
        except OSError:
            pass
    return path
