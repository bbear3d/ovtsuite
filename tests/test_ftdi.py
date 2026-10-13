# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Direct FTDI backend: D2XX port over a fake library, KLine routing."""
import ctypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import ftdi as F  # noqa: E402
from ovtsuite.flash import transport as T  # noqa: E402


class FakeD2xx:
    """Records every FT_* call; FT_Read serves a scripted byte queue."""

    def __init__(self):
        self.calls = []
        self.rx = bytearray()
        self.tx = bytearray()
        self.fail_open = False

    def _rec(self, name, *args):
        self.calls.append((name,) + args)
        return 0

    def FT_CreateDeviceInfoList(self, pn):
        pn._obj.value = 2
        return 0

    def FT_GetDeviceInfoList(self, nodes, pn):
        nodes[0].SerialNumber = b"A1B2C3D4"
        nodes[0].Description = b"FT232R USB UART"
        nodes[1].SerialNumber = b""
        nodes[1].Description = b"USB <-> Serial"
        nodes[1].Flags = F.FT_FLAGS_OPENED
        return 0

    def FT_OpenEx(self, serial, flags, ph):
        if self.fail_open:
            return 2
        self._rec("FT_OpenEx", serial.value, flags)
        ph._obj.value = 0x1234
        return 0

    def FT_Open(self, idx, ph):
        self._rec("FT_Open", idx)
        ph._obj.value = 0x5678
        return 0

    def FT_Read(self, h, buf, n, pgot):
        take = self.rx[:n]
        del self.rx[:len(take)]
        ctypes.memmove(buf, bytes(take), len(take))
        pgot._obj.value = len(take)
        return 0

    def FT_Write(self, h, buf, n, pw):
        self.tx += bytes(ctypes.string_at(buf, n))
        pw._obj.value = n
        return 0

    def FT_GetQueueStatus(self, h, pn):
        pn._obj.value = len(self.rx)
        return 0

    def __getattr__(self, name):
        if name.startswith("FT_"):
            return lambda *a: self._rec(name, *a[1:])
        raise AttributeError(name)


def _install(monkeypatch, lib):
    monkeypatch.setattr(F, "_d2xx_lib", lib)
    monkeypatch.setattr(F, "_d2xx_tried", True)


def test_list_and_names(monkeypatch):
    _install(monkeypatch, FakeD2xx())
    monkeypatch.setattr(F, "list_pyftdi_devices", lambda: [])
    ports = F.list_d2xx_devices()
    assert ports[0][0] == "FTDI:A1B2C3D4" and "FT232R" in ports[0][1]
    assert ports[1][0] == "FTDIIDX:1" and "no serial" in ports[1][1] and "in use" in ports[1][1]
    assert F.list_ftdi_ports() == ports
    assert F.is_ftdi_name("FTDI:A1B2C3D4") and F.is_ftdi_name("ftdi://0x403:0x6001:1:4/1")
    assert not F.is_ftdi_name("COM4") and not F.is_ftdi_name("/dev/ttyUSB0")


def test_d2xx_port_setup_and_io(monkeypatch):
    lib = FakeD2xx()
    _install(monkeypatch, lib)
    p = F.D2xxPort("FTDI:A1B2C3D4", baudrate=10400, timeout=1.5)
    names = [c[0] for c in lib.calls]
    opened = [c for c in lib.calls if c[0] == "FT_OpenEx"]
    assert opened and opened[0][1] == b"A1B2C3D4" and names.index("FT_OpenEx") < names.index("FT_SetBaudRate")
    assert ("FT_SetLatencyTimer", F.LATENCY_MS) in lib.calls
    assert ("FT_SetBaudRate", 10400) in lib.calls
    assert ("FT_SetTimeouts", 1500, 1500) in lib.calls
    assert ("FT_SetDataCharacteristics", 8, 0, 0) in lib.calls
    assert "FT_SetDtr" in names and "FT_ClrRts" in names
    p.baudrate = 124800
    assert p.baudrate == 124800 and ("FT_SetBaudRate", 124800) in lib.calls
    p.break_condition = True
    p.break_condition = False
    assert "FT_SetBreakOn" in names or ("FT_SetBreakOn",) in lib.calls
    lib.rx += b"\x55\xef\x8f"
    assert p.in_waiting == 3
    assert p.read(2) == b"\x55\xef" and p.read(5) == b"\x8f" and p.read(1) == b""
    assert p.write(b"\x10\x89") == 2 and lib.tx == b"\x10\x89"
    p.reset_input_buffer()
    assert ("FT_Purge", F.FT_PURGE_RX) in lib.calls
    p.close()
    assert ("FT_Close",) in lib.calls and not p.is_open
    q = F.D2xxPort("FTDIIDX:1")
    assert ("FT_Open", 1) in lib.calls
    q.close()


def test_kline_routes_to_direct_port(monkeypatch):
    lib = FakeD2xx()
    _install(monkeypatch, lib)
    k = T.KLine("FTDI:A1B2C3D4", baud=10400, timeout=2.0)
    assert k.direct and isinstance(k.port, F.D2xxPort)
    lib.rx += b"\x81\x10\xf1\x3e" + b"\xc0"          # echo of what we send
    k.write_bytes(b"\x81\x10\xf1\x3e")
    assert lib.tx == b"\x81\x10\xf1\x3e"
    assert k.read(1) == b"\xc0"
    k.set_baud(38400)
    assert ("FT_SetBaudRate", 38400) in lib.calls
    k.read_timeout = 0.5
    assert ("FT_SetTimeouts", 500, 2000) in lib.calls
    k.close()
    assert ("FT_Close",) in lib.calls


def test_open_errors(monkeypatch):
    lib = FakeD2xx()
    lib.fail_open = True
    _install(monkeypatch, lib)
    try:
        F.D2xxPort("FTDI:NOPE")
        assert False
    except F.FtdiError as ex:
        assert "DEVICE_NOT_FOUND" in str(ex)
    _install(monkeypatch, None)
    try:
        T.KLine("FTDI:NOPE")
        assert False
    except T.KLineError as ex:
        assert "D2XX" in str(ex)
