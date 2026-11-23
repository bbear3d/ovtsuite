# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The native J2534 driver DLL (tools/j2534drv): one DLL for an SM2 and an OpenPort.

What runs without hardware: the C unit tests (software ISO 15765-2 against a scripted ECU, the
OpenPort backend against a fake device) are compiled with MSYS2's 32-bit mingw-w64 when it is
there (skipped otherwise), the export table matches J2534-1 v04.04, and the installer / build
wiring is in place.  ``OVT_SM2_BENCH=1`` additionally opens the DLL on a real SM2 that has the
WinUSB driver (32-bit Python needed to load it).
"""
import os
import re
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRV = os.path.join(ROOT, "tools", "j2534drv")
GCC32 = r"C:\msys64\mingw32\bin\gcc.exe"

EXPORTS = ["PassThruOpen", "PassThruClose", "PassThruConnect", "PassThruDisconnect",
           "PassThruReadMsgs", "PassThruWriteMsgs", "PassThruStartPeriodicMsg",
           "PassThruStopPeriodicMsg", "PassThruStartMsgFilter", "PassThruStopMsgFilter",
           "PassThruSetProgrammingVoltage", "PassThruReadVersion", "PassThruGetLastError",
           "PassThruIoctl"]


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_the_export_table_is_the_j2534_04_04_api():
    names = re.findall(r"^\s+(PassThru\w+)\s*$", _read("tools", "j2534drv", "ovtj2534.def"), re.M)
    assert sorted(names) == sorted(EXPORTS)
    api = _read("tools", "j2534drv", "api.c")
    for n in EXPORTS:
        assert re.search(r"EXPORT long WINAPI %s\(" % n, api), n


def test_one_dll_serves_both_cables_and_the_configurator_registers_what_is_there():
    cfg = _read("tools", "j2534drv", "config.c")
    assert "OVT-Suite SM2 (USB)" in cfg and "OVT-Suite OpenPort 2.0 (USB)" in cfg
    # a 32-bit DLL is registered in the 32-bit registry view, a 64-bit one in the 64-bit view
    assert "KEY_WOW64_32KEY" in cfg and "KEY_WOW64_64KEY" in cfg
    # it registers a cable only when WinUSB can open it (usb_find), unless asked for --all
    assert "usb_find(" in cfg and "--all" in cfg and "--prune" in cfg
    # the DLL's own file name says which cable its registry entry is for
    api = _read("tools", "j2534drv", "api.c")
    assert '"sm2"' in api and '"openport"' in api
    for name in ("name", "serial"):
        assert name in _read("tools", "j2534drv", "ovt.h")


def test_installer_ships_and_registers_the_dll_and_the_build_makes_it():
    iss = _read("packaging", "addon_usbdriver.iss")
    assert "j2534_driver" in iss and "native" in iss and "skipifsourcedoesntexist" in iss
    assert "ovtj2534cfg_x86.exe" in iss and "'register'" in iss and "'unregister'" in iss
    assert "ovtj2534_*" in iss                                     # per-cable copies are cleaned up
    build = _read("build.ps1")
    assert "tools" + chr(92) + "j2534drv" in build and "build.sh" in build
    assert "packaging/j2534_driver/native/" in _read(".gitignore")


@pytest.mark.skipif(not os.path.isfile(GCC32), reason="MSYS2 mingw32 gcc not installed")
@pytest.mark.parametrize("test_src,extra", [("isotp_test.c", ["isotp.c"]),
                                            ("openport_test.c", ["openport.c", "-lwinmm"])])
def test_c_unit_tests(tmp_path, test_src, extra):
    exe = str(tmp_path / (test_src[:-2] + ".exe"))
    cmd = [GCC32, "-O1", "-Wall", "-std=gnu11", "-o", exe, os.path.join(DRV, test_src)]
    cmd += [os.path.join(DRV, e) if e.endswith(".c") else e for e in extra] + ["-static-libgcc"]
    env = dict(os.environ, PATH=os.path.dirname(GCC32) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=DRV, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    run = subprocess.run([exe], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout
    assert "all passed" in run.stdout
    assert "FAIL" not in run.stdout


@pytest.mark.skipif(os.environ.get("OVT_SM2_BENCH") != "1" or struct.calcsize("P") != 4,
                    reason="bench test: OVT_SM2_BENCH=1, 32-bit Python, an SM2 on WinUSB")
def test_bench_the_dll_opens_a_real_sm2():
    sys.path.insert(0, ROOT)
    from ovtsuite.flash import j2534 as J
    dll = os.path.join(DRV, "ovtj2534_x86.dll")
    assert os.path.isfile(dll), "run tools/j2534drv/build.sh first"
    dev = J.PassThru(dll)
    try:
        v = dev.battery_voltage()
        assert v is not None and 0 <= v < 40
        ch = dev.connect(J.CAN, 500000)
        ch.pass_all()
        ch.read(4, 200)
        ch.disconnect()
    finally:
        dev.close()


def test_own_j2534_entries_are_left_out_of_the_pickers(monkeypatch):
    """OVT-Suite's own J2534 DLL is for third-party programs; the app uses its direct interface."""
    import sys, types
    if sys.platform != "win32":
        import pytest
        pytest.skip("Windows registry")
    from ovtsuite.flash import j2534
    import winreg
    entries = {"OVT-Suite SM2 (USB)": "OVT-Suite", "Other": "Vendor X"}

    class K:
        def __init__(self, n): self.n = n
    def open_key(root, path, *a):
        if isinstance(root, K) or "WOW6432Node" in path:
            if isinstance(root, K): return K(path)
            raise OSError
        return K("root")
    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumKey", lambda k, i: list(entries)[i] if i < len(entries) else (_ for _ in ()).throw(OSError()))
    def query(k, name):
        return {"FunctionLibrary": "x.dll", "Name": k.n, "Vendor": entries[k.n]}[name]
    monkeypatch.setattr(winreg, "QueryValueEx", lambda k, n: (query(k, n),))
    assert [d.name for d in j2534.list_devices()] == ["Other"]
    assert len(j2534.list_devices(include_own=True)) == 2
