# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The J2534 USB driver task of the full and base installers
(packaging/addon_usbdriver.iss, included by packaging/installer.iss).

Offline: pins what the wizard promises -- the task is offered unticked and set
by what is plugged in, a hand-ticked task asks for the cable and offers a
reversible replace, the engine is shipped, and the engine's status lines are the
ones the wizard parses.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


BS = chr(92)
ADDON = _read("packaging", "addon_usbdriver.iss")


def test_both_installers_include_it_and_the_tool_installers_do_not():
    assert '#include "addon_usbdriver.iss"' in _read("packaging", "installer.iss")
    assert "addon_usbdriver" not in _read("packaging", "tool_installer.iss")


def test_the_task_is_unticked_until_a_cable_without_a_driver_is_found():
    assert re.search(r'Name: "usbdriver";.*?Flags: unchecked', ADDON, re.S)
    # looked at once, when the tasks page first shows, never in a silent install
    assert "CurPageID = wpSelectTasks" in ADDON and "not WizardSilent" in ADDON
    assert "UsbAutoOn := UsbToBind" in ADDON and "WizardSelectTasks('usbdriver')" in ADDON
    # a cable the maker's J2534 driver covers, or one on another driver, is not "to bind"
    assert "Pos('TACTRIX|1'" in ADDON and "Pos('SCANMATIK|1'" in ADDON
    assert "VID_0403" in ADDON and "VID_20A2" in ADDON and "|1|other|" in ADDON


def test_ticking_it_by_hand_asks_for_the_cable_and_offers_a_reversible_replace():
    assert "CreateCustomPage(wpSelectTasks, 'Plug in your cable'" in ADDON
    assert "UsbManual := not UsbAutoOn" in ADDON
    assert "Replace := UsbManual and UsbReplaceBox.Checked" in ADDON and "'-Replace'" in ADDON
    # no cable plugged in: goes on without a prompt, the utility being installed either way
    assert "MsgBox('No OpenPort or SM2 was found" not in ADDON
    assert "The USB driver utility is installed" in ADDON
    # the replacement is undone by the engine: Start menu entry, and Uninstall asks
    assert 'Name: "{group}' + BS + 'USB driver (OpenPort and SM2)"' in ADDON
    assert "-Action remove" in ADDON and "UninstallSilent" in ADDON


def test_the_engine_is_shipped_and_run_elevated():
    for f in ("j2534_driver.ps1", "j2534_driver.vbs"):
        assert os.path.isfile(os.path.join(ROOT, "packaging", "j2534_driver", f))
        assert ('Source: "j2534_driver' + BS + f + '"; DestDir: "{app}' + BS + 'usbdriver"') in ADDON
    assert "ShellExec('runas'" in ADDON
    # the lines it parses are ones the engine writes
    engine = _read("packaging", "j2534_driver", "j2534_driver.ps1")
    for tag in ("DEVICE|", "TACTRIX|1", "SCANMATIK|1", "FAILED|"):
        assert tag in engine
    assert "-Replace" in engine


def test_the_task_is_documented():
    assert "Install OVT-Suite J2534 drivers" in _read("ovtsuite", "data", "help", "installing.md")


def test_an_up_to_date_driver_is_not_looked_for_or_reinstalled():
    """Setup records the driver package version; while it matches, the cables are not surveyed
    and the J2534 registration is redone only for a different version."""
    assert r'FileOpen(AddBackslash(SourcePath) + "j2534_driver\VERSION")' in ADDON
    # ... or a cable is already on WinUSB, read from the registry (no PowerShell)
    assert "UsbUpToDate := (UsbRecorded('Version') = '{#UsbDrvVer}') or UsbWinUsbKnown()" in ADDON
    assert r"Enum\USB\' + IDs[I]" in ADDON and "CompareText(Svc, 'WinUSB')" in ADDON
    # setup re-ticks last time's tasks: an up-to-date driver is unticked, not just not ticked
    assert "WizardSelectTasks('!usbdriver')" in ADDON and "WizardSelectTasks('!j2534reg')" in ADDON
    assert "UsbRecorded('RegisteredVersion') = '{#UsbDrvVer}'" in ADDON
    # the records go with the uninstall
    assert "'RegisteredVersion')" in ADDON
    assert r"RegDeleteValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Version')" in ADDON
