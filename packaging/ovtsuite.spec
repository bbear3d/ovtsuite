# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""PyInstaller spec for the OVT-Suite Windows executable.

Build with ``build.ps1`` (recommended) or directly::

    pyinstaller packaging/ovtsuite.spec --noconfirm            # one-folder
    OVTSUITE_ONEFILE=1 pyinstaller packaging/ovtsuite.spec   # single .exe

Two environment switches let the one spec serve both layouts and debugging:

    OVTSUITE_ONEFILE=1   bundle everything into a single .exe
    OVTSUITE_CONSOLE=1   keep a console window (shows tracebacks / output)

It freezes the BASE app only: a copy of the package without any module's
files, staged by ``tools/build_modules.py stage-base`` into build/base (or
``OVTSUITE_BASE_DIR``). The modules are packaged separately (.ovtmod) and
installed into the build's modules folder by the build scripts
(docs/MODULES_PLAN.md).
"""

import json
import os
import sys
from pathlib import Path

# ``SPECPATH`` is injected by PyInstaller; fall back for direct execution.
ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

ONEFILE = os.environ.get("OVTSUITE_ONEFILE", "") not in ("", "0")
CONSOLE = os.environ.get("OVTSUITE_CONSOLE", "") not in ("", "0")

# -- the BASE app only ---------------------------------------------------------
# The executable carries the base app; every control-module module ships as its
# own .ovtmod (docs/MODULES_PLAN.md). So the package is frozen from a staged
# copy holding only the base app's files -- tools/build_modules.py stage-base,
# re-run here so the spec works whichever script calls it (it is a no-op when
# nothing changed). The source tree's package must not be found instead.
BASE = Path(os.environ.get("OVTSUITE_BASE_DIR") or (ROOT / "build" / "base")).resolve()
import subprocess as _sp
_r = _sp.run([sys.executable, str(ROOT / "tools" / "build_modules.py"), "stage-base",
              "--out", str(BASE)])
if _r.returncode != 0:
    raise SystemExit("staging the base app failed")
sys.path.insert(0, str(BASE))
for _m in [m for m in sys.modules if m.split(".")[0] in ("ovtsuite",)]:
    del sys.modules[_m]
from ovtsuite import branding as _branding                      # from BASE
PKG = _branding.PKG
APP_VERSION = _branding.__version__
PKG_DIR = BASE / PKG

# The licence files of exactly what this build freezes (the interpreter's own
# and every bundled package's), plus the THIRD-PARTY-NOTICES document built
# from ovtsuite/credits.py -- into the staged copy, so they ship in data/.
_r = _sp.run([sys.executable, str(ROOT / "tools" / "collect_licenses.py"),
              "--out", str(PKG_DIR)])
if _r.returncode != 0:
    raise SystemExit("collecting the licence notices failed")
NOTICES = PKG_DIR / "data" / "licenses" / "THIRD-PARTY-NOTICES.txt"

block_cipher = None

# Ship the base app's data directory (the module packages carry their own),
# sub-folders included (data/licenses).
DATA_DIR = PKG_DIR / "data"
datas = []
for _d, _dirs, _files in os.walk(DATA_DIR):
    _dirs[:] = [x for x in _dirs if x != "__pycache__"]
    _rel = os.path.relpath(_d, PKG_DIR)
    datas += [(os.path.join(_d, f), os.path.join(PKG, _rel)) for f in _files]

# The 32-bit J2534 bridge as plain source files (bridge/), so that when
# build.ps1 could not freeze j2534bridge32.exe the app can still run the
# bridge under a 32-bit Python found on the user's machine ('py -3-32').
FLASH_DIR = PKG_DIR / "flash"
datas += [(str(FLASH_DIR / "j2534_bridge.py"), "bridge"),
          (str(FLASH_DIR / "j2534.py"), "bridge"),
          (str(FLASH_DIR / "tp20_transport.py"), "bridge"),
          # isotp_transport is the tester-side ISO-TP engine the bridge runs
          # IN-PROCESS for the 6HP28 read (one IPC per chunk); it must ship
          # beside j2534_bridge.py or the iso_* ops fall back to the slow path.
          (str(FLASH_DIR / "isotp_transport.py"), "bridge")]

# Matching uses fingerprints baked into the knowledge base (ovtsuite/data/
# *.json.gz), so NO reference binary is needed at runtime and none are shipped
# -- the app is self-contained on the KB, and carries no copyrighted firmware.

# Variant/secondary table names are baked into the knowledge base too (see
# kb.a2l_names / tools/generate_kb.py), so NO A2L is needed at runtime and none
# is shipped. The app carries no copyrighted A2L, XDF, or firmware -- only the
# generated ovtsuite/data KB.

# EVERY base-app module is frozen, not only those the analyser sees imported:
# an installed module imports base code (the KB engine, the matcher, catalogue,
# groups ...) that nothing in the base app may import by itself, and a frozen
# app cannot load what it does not carry. Same for the libraries the modules
# import (external_imports.json, written by stage-base from the module sources).
def _base_modules():
    out = []
    for d, dirs, files in os.walk(PKG_DIR):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "data")]
        rel = os.path.relpath(d, BASE).replace(os.sep, ".")
        for f in files:
            if f.endswith(".py"):
                out.append(rel if f == "__init__.py" else rel + "." + f[:-3])
    return sorted(out)


try:
    with open(BASE / "external_imports.json") as _fh:
        MODULE_IMPORTS = json.load(_fh)
except (OSError, ValueError):
    MODULE_IMPORTS = []

HIDDEN = _base_modules() + MODULE_IMPORTS + [
    "serial",
    "serial.tools.list_ports",
    "serial.serialwin32",
    # OpenPort 2.0 over USB (pyusb) and the pyftdi FTDI route; both optional
    # at runtime, both imported lazily.
    "usb", "usb.core", "usb.util", "usb.backend.libusb1",
    "pyftdi", "pyftdi.ftdi", "pyftdi.serialext",
    # module signatures (Ed25519) and the ZF 8HP Gen2 AES codec
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.ciphers",
]

# Qt ships far more than this app touches; trimming keeps the bundle small and
# cold-start fast. (UPX is left off — it is known to corrupt Qt6 DLLs.)
EXCLUDES = [
    "tkinter", "unittest", "pydoc", "doctest", "pdb", "lib2to3",
    "test", "distutils", "setuptools", "pip",
    "matplotlib", "pandas", "scipy", "numpy", "PIL",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    # (QtNetwork stays: one window per user talks over a local socket --
    # ovtsuite/gui_instance.py)
    "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtDesigner",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "shiboken6.support",
]

# FTDI's D2XX library is NOT bundled: the Windows installers offer to download
# and install FTDI's own driver package instead (packaging/addon_ftdi.iss), and
# ovtsuite/flash/ftdi.py loads the copy that package (or Windows Update) puts
# in System32.
binaries = []

# -- one copy of the code ------------------------------------------------------
# The code is in ONE place: the PYZ archive appended to OVTSuite.exe (the
# default). The standalone tools' exes (OVTFlasher.exe ... OVTMcp.exe) are not
# made here: they are tiny launchers that start "OVTSuite.exe --tool <name>"
# (packaging/launcher/Launcher.cs, compiled and copied into dist\OVTSuite by
# build.ps1 through tools/build_launchers.py; packaging/entry.py dispatches).
# Every PyInstaller EXE() would embed its own copy of the PYZ (PyInstaller 6
# adds the PYZ to each EXE's PKG; MERGE() shares binaries and data, never the
# PYZ), and noarchive -- thousands of .pyc files in _internal -- slowed every
# start. docs/UPDATES.md, "One copy of the code".

ICON = ROOT / "packaging" / "ovtsuite.ico"
VERSION_FILE = ROOT / "packaging" / "version_info.txt"

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(BASE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# -- Qt: drop what the app never loads ------------------------------------------
# Excluding a Qt *Python* module above does not stop its DLLs arriving: PySide6's
# hook collects plugins (the virtual keyboard input context, the PDF image
# format, QML tooling ...) that drag Qt Quick, QML, Pdf and the software OpenGL
# rasteriser in behind them -- ~45 MB the app never touches (it is a QtWidgets
# app: no QML, no Quick, no PDF, no OpenGL widget, no translations). They are
# filtered here by name, at file level; tests/test_packaging.py pins the list.
QT_DROP_FILES = (
    "opengl32sw",                         # Mesa software OpenGL fallback (20 MB)
    "qt6quick", "qt6qml", "qt6qmlmodels", "qt6qmlmeta", "qt6qmlworkerscript",
    "qt6pdf", "qt6virtualkeyboard", "qt6openglwidgets",
    "libqt6quick", "libqt6qml", "libqt6pdf", "libqt6virtualkeyboard",
    "libqt6openglwidgets",
    # the TUIO touch and VNC plugins (Qt Network itself stays: the one-window
    # hand-over, gui_instance.py, uses its local socket)
    "qtuiotouchplugin", "libqtuiotouchplugin", "libqvnc", "qvnc",
)
QT_DROP_DIRS = ("/translations/", "/qml/", "/plugins/platforminputcontexts/",
                "/plugins/virtualkeyboard/", "/plugins/qmltooling/",
                "/plugins/imageformats/qpdf", "/plugins/imageformats/libqpdf")


def _qt_unused(dest):
    d = "/" + dest.replace("\\", "/").lower()
    if not ("/pyside6/" in d or "/qt6/" in d or os.path.basename(d).startswith(("qt6", "libqt6"))
            or "opengl32sw" in d):
        return False
    base = os.path.basename(d)
    return base.startswith(QT_DROP_FILES) or any(x in d for x in QT_DROP_DIRS)


_before = len(a.binaries) + len(a.datas)
a.binaries = [e for e in a.binaries if not _qt_unused(e[0])]
a.datas = [e for e in a.datas if not _qt_unused(e[0])]
print("ovtsuite.spec: left out %d unused Qt files" % (_before - len(a.binaries) - len(a.datas)))

# -- Linux: ship only what the Python environment provides ---------------------
# PyInstaller copies every shared library the bundle links against, including
# the BUILD machine's own OS libraries: libstdc++, fontconfig/freetype, X11/xcb,
# glib, dbus, even the whole GTK3 stack behind Qt's GTK theme plugin. On the
# target, Qt then loads that machine's GL driver, libxcb-cursor and friends --
# built against the target's newer copies of those same libraries -- and two
# incompatible versions in one process segfault at startup.
#
# So every library resolved from an OS library directory (the dynamic loader's
# own search path, from ldconfig) is left out and taken from the target, which
# the .deb / .rpm declare as dependencies (build_linux.sh derives them from the
# bundle's NEEDED entries). Kept: libpython, and whatever the Python side itself
# links -- an extension module of a distro Python (_ssl, _ctypes, ...) needs
# exactly the libssl/libffi it was built with, which a newer distro may lack.
def _os_lib_dirs():
    import subprocess
    dirs = {"/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/local/lib"}
    env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/sbin:/usr/sbin")
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True,
                             env=env).stdout
    except OSError:
        out = ""
    for line in out.splitlines():
        if "=>" in line:
            dirs.add(os.path.dirname(line.split("=>", 1)[1].strip()))
    return {os.path.realpath(d) for d in dirs}


def _needed(path):
    import subprocess
    try:
        out = subprocess.run(["objdump", "-p", path], capture_output=True,
                             text=True).stdout
    except OSError:
        return []
    return [ln.split()[1] for ln in out.splitlines()
            if ln.strip().startswith("NEEDED")]


def _drop_os_libraries(binaries):
    sysdirs = _os_lib_dirs()

    def from_os(src):
        return os.path.dirname(os.path.realpath(src)) in sysdirs

    def is_libpython(dest):
        return os.path.basename(dest).startswith("libpython")

    by_name = {}
    for e in binaries:
        by_name.setdefault(os.path.basename(e[0]), e)
    # everything the Python side reaches: libpython and every extension module
    # that is not part of the Qt bindings
    stack = [e for e in binaries if is_libpython(e[0]) or (
        e[2] == "EXTENSION" and not e[0].startswith(("PySide6", "shiboken6")))]
    python_side, seen = set(), set()
    while stack:
        e = stack.pop()
        for soname in _needed(e[1]):
            dep = by_name.get(soname)
            if dep is not None and dep[0] not in seen:
                seen.add(dep[0])
                python_side.add(dep[0])
                stack.append(dep)
    kept, dropped = [], []
    for e in binaries:
        if from_os(e[1]) and not is_libpython(e[0]) and e[0] not in python_side:
            dropped.append(os.path.basename(e[0]))
        else:
            kept.append(e)
    print("ovtsuite.spec: left %d OS libraries to the target system: %s"
          % (len(dropped), " ".join(sorted(dropped))))
    return kept


if sys.platform.startswith("linux"):
    a.binaries = _drop_os_libraries(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    name="OVTSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        runtime_tmpdir=None,
        **exe_kwargs,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, upx_exclude=[],
        name="OVTSuite",
    )

# THIRD-PARTY-NOTICES.txt beside the executable, where every installer (full,
# base, tool-only) and the Linux package pick it up with the rest of the folder.
import shutil as _shutil
_out = Path(DISTPATH) / ("" if ONEFILE else exe_kwargs["name"])
if NOTICES.is_file() and _out.is_dir():
    _shutil.copyfile(NOTICES, _out / NOTICES.name)
