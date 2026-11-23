# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Find libusb-1.0 (and other native libraries) and make PyUSB / pyftdi use it.

PyUSB locates libusb through ``ctypes.util.find_library``, which on macOS looks
only in the default dyld locations (``/usr/local/lib``, ``/usr/lib``, and
whatever ``DYLD_*`` variables survive -- none do under the hardened runtime), so
a libusb installed by MacPorts (``/opt/local/lib``) or Homebrew on Apple silicon
(``/opt/homebrew/lib``) is not found. This module searches those places itself
and hands the result to PyUSB:

* :func:`find_libusb` -- full path of a libusb-1.0 library, or None (cached);
* :func:`install_backend` -- makes ``usb.backend.libusb1.get_backend`` (which
  ``usb.core.find`` and pyftdi both call) use that path; idempotent, never raises;
* :func:`find_native` -- the same search for any other library (libftd2xx);
* :func:`not_found_message` -- one platform-aware "libusb not found" text.

Search order: ``OVT_LIBUSB`` / ``LIBUSB_PATH`` (a file or a folder), the
``DYLD_*`` / ``LD_LIBRARY_PATH`` folders, the ``libusb_path`` app setting, the
copy bundled with the app, the usual package-manager folders of the platform,
and finally ``ctypes.util.find_library``.
"""
import ctypes
import ctypes.util
import glob
import os
import struct
import sys
from typing import Iterable, List, Optional, Sequence

ENV_VARS = ("OVT_LIBUSB", "LIBUSB_PATH")
SETTING_KEY = "libusb_path"

DLL_NAME = "libusb-1.0.dll"

_MAC_DIRS = ["/opt/local/lib", "/opt/homebrew/lib", "/opt/homebrew/opt/libusb/lib",
             "/usr/local/lib", "/usr/local/opt/libusb/lib"]
_MAC_GLOBS = ["/opt/homebrew/Cellar/libusb/*/lib", "/usr/local/Cellar/libusb/*/lib"]
_LINUX_DIRS = ["/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
               "/usr/lib/arm-linux-gnueabihf", "/usr/lib64", "/usr/lib", "/usr/local/lib"]

_cache = {"done": False, "path": None}    # only a found library is cached, never a miss
_skipped: List[tuple] = []                # (path, why) of candidates that exist but do not load
_installed = {"done": False}
_orig_get_backend = {"fn": None}


def library_names() -> List[str]:
    """File names tried for libusb-1.0 on this platform."""
    if sys.platform == "darwin":
        return ["libusb-1.0.dylib", "libusb-1.0.0.dylib"]
    if sys.platform.startswith("win"):
        return [DLL_NAME]
    return ["libusb-1.0.so.0", "libusb-1.0.so"]


def _env_dirs(*names: str) -> List[str]:
    out: List[str] = []
    for n in names:
        for p in os.environ.get(n, "").split(os.pathsep):
            if p:
                out.append(p)
    return out


def bundled_dirs() -> List[str]:
    """Folders a copy shipped with the app may sit in (frozen build, macOS
    bundle, or the source tree's packaging/vendor/libusb)."""
    dirs: List[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        contents = os.path.dirname(exe_dir)
        dirs += [os.path.join(exe_dir, "_internal"), exe_dir]
        if sys.platform == "darwin":
            dirs += [os.path.join(contents, "Frameworks"), os.path.join(contents, "Resources")]
    here = os.path.dirname(os.path.abspath(__file__))
    vendor = os.path.normpath(os.path.join(here, "..", "..", "packaging", "vendor", "libusb"))
    dirs += [os.path.join(vendor, "macos"), vendor]
    return dirs


def _setting_dirs() -> List[str]:
    """The ``libusb_path`` app setting.  Read through QSettings directly, so it works
    whether or not the GUI has imported PySide6 yet; without PySide6, no setting."""
    try:
        from .. import appsettings
        v = appsettings.settings().value(SETTING_KEY, "")
        return [str(v)] if v else []
    except Exception:
        return []


def _system_dirs() -> List[str]:
    if sys.platform == "darwin":
        dirs = list(_MAC_DIRS)
        for g in _MAC_GLOBS:
            dirs += sorted(glob.glob(g), reverse=True)
        return dirs
    if sys.platform.startswith("linux"):
        return list(_LINUX_DIRS)
    return []


def candidate_dirs() -> List[str]:
    """Every folder searched for libusb, in order (duplicates removed)."""
    dirs = (_env_dirs(*ENV_VARS)
            + _env_dirs("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH")
            + _setting_dirs() + bundled_dirs() + _system_dirs())
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _loadable(path: str) -> Optional[str]:
    """None if ``path`` loads in this process, else why not (typically a library of
    another architecture: an x86_64 Homebrew libusb under an arm64 Python)."""
    try:
        ctypes.CDLL(path)
        return None
    except OSError as ex:
        return str(ex).splitlines()[0] if str(ex) else "cannot be loaded"
    except Exception as ex:                          # noqa: BLE001
        return str(ex) or "cannot be loaded"


def _candidates(names: Sequence[str], dirs: Iterable[str]) -> List[str]:
    out: List[str] = []
    for d in dirs:
        if os.path.isfile(d):                       # an explicit file (OVT_LIBUSB=/x/libusb.dylib)
            out.append(d)
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def _search(names: Sequence[str], dirs: Iterable[str], skipped: Optional[list] = None) -> Optional[str]:
    """First candidate that actually loads (so the process's own architecture wins
    over a foreign-architecture copy found earlier); the ones that do not load are
    appended to ``skipped`` as ``(path, why)``."""
    for p in _candidates(names, dirs):
        why = _loadable(p)
        if why is None:
            return p
        if skipped is not None:
            skipped.append((p, why))
    return None


def _locate_libusb() -> Optional[str]:
    del _skipped[:]
    if sys.platform.startswith("win") and struct.calcsize("P") != 8:
        return None                                 # the bundled DLL is 64-bit only
    found = _search(library_names(), candidate_dirs(), _skipped)
    if found:
        return found
    for n in ("usb-1.0", "libusb-1.0", "usb"):
        try:
            p = ctypes.util.find_library(n)
        except Exception:
            p = None
        if p:
            why = _loadable(p)
            if why is None:
                return p
            _skipped.append((p, why))
    return None


def find_libusb(refresh: bool = False) -> Optional[str]:
    """Path (or loadable name) of a libusb-1.0 library that loads in this process,
    or None.  A found path is cached; a miss is not (the library may be installed
    while the app runs), so every call after a miss searches again."""
    if not refresh and _cache["done"] and _cache["path"]:
        return _cache["path"]
    try:
        path = _locate_libusb()
    except Exception:
        path = None
    _cache["done"], _cache["path"] = bool(path), path
    return path


def find_library_for_pyusb(name: str) -> Optional[str]:
    """``find_library`` callback for PyUSB: ``name`` is the short name it asks
    for ("usb-1.0", "libusb-1.0", ...); answer with the located path, else
    whatever ``ctypes.util.find_library`` says."""
    p = find_libusb()
    if p:
        return p
    try:
        return ctypes.util.find_library(name)
    except Exception:
        return None


def get_backend():
    """A PyUSB libusb-1.0 backend using the located library, or None."""
    try:
        import usb.backend.libusb1 as lb
        fn = _orig_get_backend["fn"] or lb.get_backend
        return fn(find_library=find_library_for_pyusb)
    except Exception:
        return None


def install_backend() -> Optional[str]:
    """Make PyUSB (and so pyftdi) use the located libusb for the rest of the
    process. Idempotent; never raises. Returns the library path or None."""
    path = find_libusb(refresh=not _cache["path"])   # retry after an earlier miss
    if _installed["done"]:
        return path
    try:
        import usb.backend.libusb1 as lb
    except Exception:
        return path                                  # no pyusb: nothing to patch (retried later)
    try:
        orig = lb.get_backend
        _orig_get_backend["fn"] = orig

        def get_backend(find_library=None):
            return orig(find_library=find_library or find_library_for_pyusb)

        get_backend.__wrapped__ = orig
        lb.get_backend = get_backend
        _installed["done"] = True
    except Exception:
        pass
    return path


def find_native(libname: str, names: Sequence[str],
                extra_dirs: Sequence[str] = ()) -> Optional[str]:
    """Locate another native library (``libftd2xx``): ``names`` are the file
    names tried in the app's own folders, ``extra_dirs``, the DYLD/LD paths and
    the platform's package-manager folders; then ``ctypes.util.find_library``."""
    dirs = (list(extra_dirs) + _env_dirs("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
                                         "LD_LIBRARY_PATH")
            + bundled_dirs()
            + _system_dirs())
    seen, uniq = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    found = _search(names, [d for d in uniq if not os.path.isfile(d)])
    if found:
        return found
    try:
        return ctypes.util.find_library(libname)
    except Exception:
        return None


def not_found_message() -> str:
    """The text shown when no libusb-1.0 can be loaded (contains 'libusb')."""
    if sys.platform == "darwin":
        how = ("macOS: install it with `brew install libusb` (Homebrew) or "
               "`sudo port install libusb` (MacPorts), then restart the app")
    elif sys.platform.startswith("win"):
        how = "Windows: libusb-1.0.dll is missing from the installation; reinstall the app"
    else:
        how = "Linux: install the libusb-1.0-0 package (e.g. `sudo apt install libusb-1.0-0`)"
    find_libusb(refresh=True)                      # it may have been installed since the last look
    dirs = [d for d in candidate_dirs() if os.path.isdir(d)]
    shown = ", ".join(dirs[:6]) + (", ..." if len(dirs) > 6 else "")
    skipped = ""
    if _skipped:
        skipped = "; found but not usable: " + ", ".join("%s (%s)" % (p, why) for p, why in _skipped[:4])
    return ("libusb-1.0 not found (%s; searched: %s%s; or point the OVT_LIBUSB environment "
            "variable at the library file or its folder)"
            % (how, shown or "no usual folder exists", skipped))


def explain_absence() -> str:
    """Why no libusb is usable, in words a user can act on, or "" when one loads.
    Searches again first, so a library installed since the app started is noticed."""
    if find_libusb(refresh=True):
        return ""
    return not_found_message()
