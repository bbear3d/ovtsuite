# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which USB devices are plugged in right now, for the J2534 device pickers.

The ``PassThruSupport.04.04`` registry lists every *installed* J2534 driver, plugged in
or not. :func:`device_present` answers, per driver, whether its cable is on the USB bus:

* ``True``  -- a USB device with one of the driver's known VID/PIDs is present;
* ``False`` -- the driver has known VID/PIDs and none is present (the picker hides it);
* ``None``  -- unknown: no VID/PID is known for it (a shim DLL such as OVT Interface, a
  Bluetooth/Ethernet cable, a vendor not in :data:`KNOWN`). The picker still lists it,
  dimmed, so it stays selectable.

The scan is one SetupAPI enumeration of the *present* USB nodes (a few ms, no device is
opened and no vendor DLL loaded) and is cached for :data:`TTL` seconds, so opening a
picker repeatedly costs nothing. :func:`invalidate` forces the next call to rescan.
"""
import os
import re
import threading
import time
from typing import Optional, Set, Tuple

#: seconds a scan is reused
TTL = 3.0

#: (vendor/name substring, lower-case) -> USB (VID, PID) pairs that cable shows up as.
#: A DLL matches the first row whose text is in its "vendor name" string.
KNOWN = (
    ("tactrix", ((0x0403, 0xCC4D),)),
    ("openport", ((0x0403, 0xCC4D),)),
    # Techstream Mini VCI: an FTDI FT232 -- shares 0403:6001 with every FTDI cable, so
    # this means "an FTDI serial device is present", the best the USB ids can say.
    ("techstream", ((0x0403, 0x6001),)),
    ("denso", ((0x0403, 0x6001),)),
)

#: drivers that never sit directly on a USB device: always selectable
ALWAYS = ("ovt-suite", "ovt interface")

_lock = threading.Lock()
_cache: Tuple[float, Set[Tuple[int, int]]] = (0.0, set())
_ID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)


def _scan() -> Set[Tuple[int, int]]:
    """VID/PIDs of the USB devices present now (empty off Windows or on any failure)."""
    if os.name != "nt":
        return set()
    import ctypes
    from ctypes import wintypes
    try:
        sa = ctypes.WinDLL("setupapi", use_last_error=True)
        DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4

        class SP_DEVINFO_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("Guid", ctypes.c_byte * 16),
                        ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

        sa.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        sa.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                            wintypes.HWND, wintypes.DWORD]
        sa.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             ctypes.POINTER(SP_DEVINFO_DATA)]
        sa.SetupDiGetDeviceInstanceIdW.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(SP_DEVINFO_DATA),
                                                   wintypes.LPWSTR, wintypes.DWORD,
                                                   ctypes.POINTER(wintypes.DWORD)]
        sa.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        h = sa.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
        if h in (None, wintypes.HANDLE(-1).value):
            return set()
        out: Set[Tuple[int, int]] = set()
        try:
            buf = ctypes.create_unicode_buffer(512)
            i = 0
            while True:
                d = SP_DEVINFO_DATA()
                d.cbSize = ctypes.sizeof(d)
                if not sa.SetupDiEnumDeviceInfo(h, i, ctypes.byref(d)):
                    break
                i += 1
                if sa.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(d), buf, 512, None):
                    m = _ID.search(buf.value)
                    if m:
                        out.add((int(m.group(1), 16), int(m.group(2), 16)))
        finally:
            sa.SetupDiDestroyDeviceInfoList(h)
        return out
    except Exception:
        return set()


def present_ids() -> Set[Tuple[int, int]]:
    """The cached set of present (VID, PID) pairs, rescanned when older than TTL."""
    global _cache
    with _lock:
        if time.monotonic() - _cache[0] > TTL or _cache[0] == 0.0:
            _cache = (time.monotonic(), _scan())
        return set(_cache[1])


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = (0.0, set())


def device_present(vendor: str, name: str) -> Optional[bool]:
    """True / False / None as described in the module docstring."""
    text = ("%s %s" % (vendor, name)).lower()
    if any(a in text for a in ALWAYS):
        return True
    for key, ids in KNOWN:
        if key in text:
            have = present_ids()
            return any(i in have for i in ids)
    return None
