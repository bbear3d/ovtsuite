# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The J2534 USB driver add-on (packaging/j2534_driver, its installer).

Offline: nothing is installed here. What is pinned: the installer script and the
engine it ships exist and refer to each other; the wizard asks for the cable
before it binds; the engine's actions and result lines are the ones the installer
parses; the engine takes only Microsoft's inbox WinUSB (no driver file, no
certificate); build.ps1 builds it; and on Windows the engine parses as PowerShell
and its read-only ``status`` runs.
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "packaging")
ENGINE = os.path.join(PKG, "j2534_driver", "j2534_driver.ps1")


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_the_installer_ships_the_engine_and_asks_for_the_cable_first():
    iss = _read("packaging", "j2534_driver_installer.iss")
    assert os.path.isfile(ENGINE)
    assert re.search(r'^Source: "\{#SourceDir\}\\\{#ScriptName\}"; DestDir: "\{app\}"', iss, re.M)
    assert "#define ScriptName  \"j2534_driver.ps1\"" in iss
    assert "PrivilegesRequired=admin" in iss
    # the plug-in page sits right after Welcome, and Next without a cable asks first
    assert "CreateCustomPage(wpWelcome, 'Plug in your cable'" in iss
    assert "No OpenPort or SM2 was found." in iss
    # the binding runs after the files are installed, and uninstalling undoes it
    assert "CurStep = ssPostInstall" in iss
    assert "-Action remove" in iss and "[UninstallRun]" in iss
    # the Start menu entry is the management window, opened elevated and with no console
    # window: wscript runs a launcher that starts the hidden elevated PowerShell itself
    assert 'Filename: "{sys}\\wscript.exe"' in iss and "j2534_driver.vbs" in iss
    vbs = _read("packaging", "j2534_driver", "j2534_driver.vbs")
    assert '"runas"' in vbs and "-Action manage" in vbs and "-WindowStyle Hidden" in vbs


def test_the_icon_is_complete_and_used_everywhere_it_should_be():
    from PIL import Image
    ico = os.path.join(PKG, "ovtsuite_openport.ico")
    im = Image.open(ico)
    assert sorted(im.info["sizes"]) == [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]
    iss = _read("packaging", "j2534_driver_installer.iss")
    assert "SetupIconFile=ovtsuite_openport.ico" in iss
    assert 'Source: "ovtsuite_openport.ico"; DestDir: "{app}"' in iss
    # the shortcut and the window share one taskbar identity, so the window shows the icon
    assert 'AppUserModelID: "OVT-Suite.J2534Driver"' in iss
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    assert "SetCurrentProcessExplicitAppUserModelID('OVT-Suite.J2534Driver')" in ps
    assert "ovtsuite_openport.ico" in ps


def test_the_installer_parses_the_lines_the_engine_writes():
    iss = _read("packaging", "j2534_driver_installer.iss")
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    for token in ("TACTRIX|1", "|1|unbound|", "|1|other|", "|1|winusb|", "FAILED|"):
        assert token in iss, token
    for token in ("DEVICE|", "TACTRIX|1", "BOUND|", "FAILED|", "REMOVED|", "NONE|"):
        assert token in ps, token
    assert "'unbound'" in ps and "'winusb'" in ps and "'other'" in ps


def test_the_engine_uses_only_the_windows_winusb_inf():
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    # (the C# string in the script escapes the backslash: "USB\\MS_COMP_WINUSB")
    assert "winusb.inf" in ps and "MS_COMP_WINUSB" in ps
    # no downloads, no certificates, no unsigned driver
    for banned in ("Invoke-WebRequest", "DownloadFile", "Import-Certificate", "certutil",
                   "New-SelfSignedCertificate", "bcdedit"):
        assert banned not in ps, banned
    # switching over from another driver is opt-in
    assert "[switch]$Replace" in ps


def test_the_engine_is_versioned_and_built_by_build_ps1():
    ver = _read("packaging", "j2534_driver", "VERSION").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver)
    build = _read("build.ps1")
    assert "'j2534-driver'" in build
    assert "j2534_driver_installer.iss" in build
    assert "OVTSuite-J2534Driver-$drvVer-setup" in build


def test_the_inno_script_has_no_ispp_lines_inside_code():
    # a [Code] line that starts with '#' is read as a preprocessor directive
    iss = _read("packaging", "j2534_driver_installer.iss")
    code = iss.split("[Code]", 1)[1]
    assert not re.search(r"^\s*#", code, re.M)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_the_engine_parses_and_status_runs():
    parse = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             "'%s',[ref]$null,[ref]$e); if($e){$e|%%{$_.Message}; exit 1}" % ENGINE)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", parse],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", ENGINE, "-Action", "status"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    for line in r.stdout.splitlines():
        assert line.startswith(("DEVICE|", "TACTRIX|", "SCANMATIK|", "REPLACED|")), line


def test_the_one_add_on_also_binds_the_scanmatik_sm2():
    # the SM2 (USB 20A2:0001) shares the OpenPort add-on: same engine, installer, Start menu
    # entry; no second shortcut
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    iss = _read("packaging", "j2534_driver_installer.iss")
    assert "VID_0403&PID_CC4D" in ps and "VID_20A2&PID_0001" in ps
    # Tactrix's driver is the OpenPort's only: it must not stop an SM2 being bound, and a
    # failed SM2 binding has no serial class to put back
    assert "Tactrix = $true" in ps and "Tactrix = $false" in ps
    assert "Where-Object { -not $_.Tactrix }" in ps
    # a failed binding puts the device back in the class it was in (Ports for an OpenPort on the
    # serial driver, Tactrix's own class, none for an SM2) -- never a hard-coded one
    assert "string restoreClass" in ps and "[OpDrv]::Bind($d.Id, $WinUsbInf, $d.Class)" in ps
    assert len(re.findall(r'^Name: "\{group\}', iss, re.M)) == 1
    assert "SM2" in iss
    # the wizard leaves a Tactrix-owned OpenPort alone but still offers to bind an SM2
    assert "VID_0403" in iss and "TactrixCable" in iss


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_status_lists_each_device_by_its_own_usb_id():
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", ENGINE, "-Action", "status"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    for line in r.stdout.splitlines():
        if line.startswith("DEVICE|"):
            assert re.match(r"DEVICE\|USB\\VID_(0403&PID_CC4D|20A2&PID_0001)\\[^|]+\|[01]\|"
                            r"(unbound|winusb|other)\|", line), line


def test_override_and_restore_of_the_makers_drivers_is_recorded_and_checked():
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    iss = _read("packaging", "j2534_driver_installer.iss")
    # an overridden driver (Tactrix's, Scanmatik's SmUsb, Zadig's) is written down before it is
    # replaced and forgotten if the binding fails
    assert "if ($d.State -eq 'other') { Set-Replaced $d.Id $d.Service $d.Inf $d.Class }" in ps
    assert ps.count("Clear-Replaced $d.Id") >= 3
    assert r"HKLM:\SOFTWARE\OVT-Suite\J2534Driver\Replaced" in ps
    # restore removes the device, rescans, waits, and says whether the recorded driver is back
    for token in ("RESTORED|", "NOTRESTORED|", "REPLACED|", "SCANMATIK|1", "/remove-device",
                  "/scan-devices", "$now.Service -ine $exp"):
        assert token in ps, token
    # only the binding is touched: the maker's driver is never uninstalled or deleted
    assert "Remove-Item -Recurse" not in ps
    # ... except a libwdi/Zadig WinUSB package the device is on (it would be picked again
    # when the device comes back): deleted only after its Provider line says libwdi, and
    # only that one package
    assert ps.count("/delete-driver") == 1
    assert ps.index('Provider\\s*=\\s*"?libwdi') < ps.index("/delete-driver")
    # actions can be limited to one kind of device or one device
    assert "[ValidateSet('all', 'openport', 'sm2')][string]$Only" in ps and "[string]$Id" in ps
    assert ps.count("Select-Targets") >= 4
    # the installer has the opt-in, passes -Replace, and removes the record on uninstall
    assert "ReplaceBox" in iss and "'-Replace'" in iss
    assert "uninsdeletekey" in iss and "J2534Driver" in iss


def test_the_window_acts_on_the_selected_device_and_names_the_driver_it_overrides():
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    assert "$state.SelId" in ps and "$script:Id = $state.SelId" in ps
    assert "Scanmatik's driver (SmUsb)" in ps
    assert "Restore the original driver" in ps and "Override the other driver" in ps


def test_the_add_on_is_called_the_j2534_usb_driver_everywhere():
    iss = _read("packaging", "j2534_driver_installer.iss")
    build = _read("build.ps1")
    assert 'AppId=OVT-Suite.J2534Driver' in iss
    assert '"OVTSuite-J2534Driver-" + DriverVersion + "-setup"' in iss
    assert "OVTSuite-J2534Driver-$drvVer-setup" in build
    assert "'j2534-driver'" in build
    for f in ("j2534_driver.ps1", "j2534_driver.vbs", "VERSION"):
        assert os.path.isfile(os.path.join(PKG, "j2534_driver", f)), f
    assert not os.path.exists(os.path.join(PKG, "openport_driver"))


def test_overriding_a_makers_driver_sets_its_j2534_entry_aside_and_a_restore_puts_it_back():
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    # both registry views explicitly (a 32-bit PowerShell only sees its own), copied before removed
    assert "Registry64" in ps and "Registry32" in ps
    assert ps.index("Copy-RegTree $k $d") < ps.index("$root.DeleteSubKeyTree($sub)")
    # only the maker's entry: never ours (vendor OVT-Suite) and not a Scanmatik SM3
    assert "-ieq 'OVT-Suite'" in ps and "'SM2'" in ps
    # set aside after a takeover binds; put back after a restore comes back on its own driver
    assert "Save-MakerRegistration $d.Kind" in ps and "Restore-MakerRegistration $d.Kind" in ps
    assert "J2534UNREG|" in ps and "J2534REG|" in ps


def test_a_restore_also_removes_our_own_j2534_entry_but_not_the_shim():
    ps = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    assert "Remove-OwnRegistration $d.Kind" in ps and "OWNUNREG|" in ps
    # ours = vendor OVT-Suite AND an ovtj2534_*.dll; the OVT Interface shim (OvtInterface_*.dll) is not
    assert "-notlike 'ovtj2534_*'" in ps
