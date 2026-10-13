# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Serial-port filtering (flash/ports.py) and the USB OpenPort as a J2534 K-line device.

Bear: "COM port selection on Linux lists a bunch of non com port devices" -- the
32 /dev/ttyS* placeholders the 8250 driver always creates (newer kernels put
them on the serial-base bus, so pyserial's own "platform" filter misses them),
Bluetooth rfcomm links, and so on. A port is now listed only when it is
USB-backed or the kernel reports real UART hardware behind it (KWHack's rule).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import ports  # noqa: E402


class P:
    """A pyserial ListPortInfo stand-in."""

    def __init__(self, device, vid=None, pid=None, description="n/a"):
        self.device, self.vid, self.pid, self.description = device, vid, pid, description


def test_usb_backed_ports_are_listed_everywhere(monkeypatch):
    for plat in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", plat)
        assert ports.check(P("/dev/ttyUSB0", 0x0403, 0x6001)).listed
        assert ports.check(P("/dev/ttyUSB1", 0x1A86, 0x7523)).listed      # CH340


def test_openport_tty_is_not_a_serial_cable(monkeypatch):
    """It is CDC-ACM, so it has a tty node, but it does not speak serial K-line:
    it is offered as its own J2534 device instead."""
    for plat in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", plat)
        c = ports.check(P("/dev/ttyACM0", 0x0403, 0xCC4D))
        assert not c.listed and "J2534" in c.reason


def test_linux_non_usb_ports_need_uart_hardware(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for hw, listed in ((True, True), (False, False), (None, False)):
        monkeypatch.setattr(ports, "_uart_hardware", lambda dev, hw=hw: hw)
        assert ports.check(P("/dev/ttyS0")).listed is listed


def test_macos_lists_only_usb_backed_ports(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not ports.check(P("/dev/cu.Bluetooth-Incoming-Port")).listed
    assert not ports.check(P("/dev/cu.wlan-debug")).listed


def test_windows_ports_are_not_filtered(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert ports.check(P("COM1")).listed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="TIOCGSERIAL is Linux")
def test_the_kernel_probe_rejects_a_tty_without_a_uart():
    """A pseudo-terminal is a tty with no UART behind it -- the real probe, on a
    real kernel, must not call it hardware."""
    master, slave = os.openpty()
    try:
        assert ports._uart_hardware(os.ttyname(slave)) is not True
    finally:
        os.close(master)
        os.close(slave)
    assert ports._uart_hardware("/dev/does-not-exist") is None


# -- the USB OpenPort in the K-line picker ------------------------------------
@pytest.fixture
def gf(monkeypatch):
    pytest.importorskip("PySide6")
    from ovtsuite.flash import gui_flash, openport
    monkeypatch.setattr(openport, "find_devices", lambda: ["OpenPort 2.0 bus 1 addr 7"])
    monkeypatch.setattr(gui_flash, "list_ports", lambda: [("/dev/ttyUSB0", "/dev/ttyUSB0")])
    monkeypatch.setattr(gui_flash, "_j2534_devices", lambda: [])
    monkeypatch.setattr(gui_flash, "remote_devices", lambda: [])
    return gui_flash


def test_usb_openport_is_offered_for_kline(gf):
    """Linux/macOS have no J2534 registry, so the K-line picker never offered the
    OpenPort; it does now, and it routes as a J2534 device (hardware fast init,
    Medium speed cap)."""
    devs = gf.kline_devices()
    assert [d for d, _ in devs] == ["/dev/ttyUSB0", gf.OPENPORT_USB]
    assert "OpenPort" in devs[1][1] and "J2534" in devs[1][1]
    assert gf.is_j2534(gf.OPENPORT_USB) and not gf.is_j2534("/dev/ttyUSB0")
    assert gf.cable_allows_high(gf.OPENPORT_USB) is False


def test_open_j2534_opens_the_usb_openport(gf, monkeypatch):
    from ovtsuite.flash import openport
    opened = []
    monkeypatch.setattr(openport, "OpenPort", lambda: opened.append(1) or "dev")
    assert gf.open_j2534(gf.OPENPORT_USB) == "dev" and opened == [1]


def test_missing_openport_says_why(monkeypatch):
    from ovtsuite.flash import openport
    usb_core = pytest.importorskip("usb.core")

    def no_backend(**kw):
        raise usb_core.NoBackendError("No backend available")
    monkeypatch.setattr(usb_core, "find", no_backend)
    assert "libusb" in openport.explain_absence()
    monkeypatch.setattr(usb_core, "find", lambda **kw: iter(()))
    assert "no OpenPort" in openport.explain_absence()
