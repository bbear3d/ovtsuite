# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Direct FTDI access for K-line cables (the KWHack ``FtdiInterface`` port).

Going through the OS serial driver leaves the chip's *latency timer* at its
16 ms default: the FT232 holds received bytes until its USB buffer fills or
the timer expires, so every K-line reply -- and every echo the tester has to
drain before it can send the next byte -- costs up to 16 ms.  KWHack talks to
the chip directly and sets the timer to 2 ms; that is what makes its
KWP2000 sessions snappy.  Two routes give the same thing here, both exposing
the pyserial ``Serial`` surface :class:`ovtsuite.flash.transport.KLine`
already drives (``read`` / ``write`` / ``flush`` / ``timeout`` / ``baudrate`` /
``break_condition`` / ``dtr`` / ``rts`` / ``reset_input_buffer`` / ``close``):

* :class:`D2xxPort` -- FTDI's own D2XX library through ``ctypes``.  The
  app does not ship it: FTDI's driver package installs ``ftd2xx64.dll`` in
  System32 along with the kernel bus driver (``ftdibus.sys``), whether it
  comes from Windows Update when a cable is first plugged in or from the
  Windows installer's optional "FTDI USB driver" download.  A copy placed
  beside the executable is still used when System32 has none.  Port names
  ``FTDI:<serial>`` (``FT_OpenEx`` by serial number) and ``FTDIIDX:<n>``
  (``FT_Open`` by enumeration index, for cables with a blank serial -- common
  on cheap KKL cables, and the same rule KWHack applies).
* :class:`pyftdi` route -- the BSD-licensed ``pyftdi`` package speaks the
  chip's USB vendor protocol through libusb, bypassing the OS driver entirely
  (Linux, macOS; what KWHack's ``FtdiLibUsbInterface`` does for cables Apple's
  driver allow-list ignores).  Port names are pyftdi URLs,
  ``ftdi://<vid>:<pid>:<bus>:<addr>/1``.  On Windows pyftdi needs a WinUSB
  driver bound to the cable, which would take it away from the VCP driver, so
  it is not offered there.

:func:`open_port` picks the route from the name; :func:`list_ftdi_ports`
enumerates both so the port pickers can show "FTDI direct" entries beside the
ordinary COM / tty ports.  Everything is optional -- when neither library is
present the lists are simply empty and pyserial keeps working as before.
"""
import ctypes
import os
import sys
from typing import List, Optional, Tuple

LATENCY_MS = 2                       # KWHack: FT_SetLatencyTimer(handle, 2)
FTDI_VID = 0x0403
# Non-standard PIDs the D2XX driver / pyftdi must be told about (KWHack
# KnownFtdiDevices.NonStandard).
KNOWN_NONSTANDARD = [(0x0403, 0xFA24, "Ross-Tech HEX-USB")]
D2XX_PREFIXES = ("FTDI:", "FTDIIDX:")
PYFTDI_PREFIX = "ftdi://"


def is_ftdi_name(name: str) -> bool:
    n = name or ""
    return n.upper().startswith(D2XX_PREFIXES) or n.lower().startswith(PYFTDI_PREFIX)


# =================================================================== D2XX
class FtdiError(Exception):
    pass


_FT_STATUS = {
    0: "OK", 1: "INVALID_HANDLE", 2: "DEVICE_NOT_FOUND", 3: "DEVICE_NOT_OPENED",
    4: "IO_ERROR", 5: "INSUFFICIENT_RESOURCES", 6: "INVALID_PARAMETER",
    7: "INVALID_BAUD_RATE", 8: "DEVICE_NOT_OPENED_FOR_ERASE",
    9: "DEVICE_NOT_OPENED_FOR_WRITE", 10: "FAILED_TO_WRITE_DEVICE",
    11: "EEPROM_READ_FAILED", 12: "EEPROM_WRITE_FAILED", 13: "EEPROM_ERASE_FAILED",
    14: "EEPROM_NOT_PRESENT", 15: "EEPROM_NOT_PROGRAMMED", 16: "INVALID_ARGS",
    17: "NOT_SUPPORTED", 18: "OTHER_ERROR", 19: "DEVICE_LIST_NOT_READY",
}
FT_OPEN_BY_SERIAL_NUMBER = 1
FT_PURGE_RX, FT_PURGE_TX = 1, 2
FT_BITS_8, FT_STOP_BITS_1, FT_PARITY_NONE, FT_FLOW_NONE = 8, 0, 0, 0
FT_FLAGS_OPENED = 1


class FT_DEVICE_LIST_INFO_NODE(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
                ("ID", ctypes.c_uint32), ("LocId", ctypes.c_uint32),
                ("SerialNumber", ctypes.c_char * 16), ("Description", ctypes.c_char * 64),
                ("ftHandle", ctypes.c_void_p)]


_d2xx_lib = None
_d2xx_tried = False


def _bundled_dirs() -> List[str]:
    """Where a user-supplied copy may sit: beside the frozen exe, else the
    source tree's packaging/drivers (neither is shipped)."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
        dirs.append(os.path.dirname(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(os.path.dirname(os.path.dirname(here)), "packaging", "drivers"))
    return dirs


def _lib_names() -> List[str]:
    """Candidates in load order: the copy the FTDI driver package installed
    (System32), then a copy placed beside the app.

    On 64-bit Windows FTDI's driver (ftdibus.inf, CDM 2.12) copies the 64-bit
    ``ftd2xx64.dll`` into System32 RENAMED ``ftd2xx.dll`` (the 32-bit one goes
    to SysWOW64), so a 64-bit process must try ``ftd2xx.dll`` too: looking
    only for ``ftd2xx64.dll`` found nothing once the bundled copy was gone. A
    32-bit process's System32 is SysWOW64, which holds the 32-bit one."""
    if sys.platform.startswith("win"):
        names = (["ftd2xx64.dll", "ftd2xx.dll"] if sys.maxsize > 2 ** 32
                 else ["ftd2xx.dll"])
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        return ([os.path.join(sysdir, n) for n in names]
                + [os.path.join(d, n) for d in _bundled_dirs() for n in names] + names)
    if sys.platform == "darwin":
        name = "libftd2xx.dylib"
        return ([name, "/usr/local/lib/" + name, "/opt/homebrew/lib/" + name]
                + [os.path.join(d, name) for d in _bundled_dirs()])
    return ["libftd2xx.so", "ftd2xx.so", "/usr/local/lib/libftd2xx.so"]


def d2xx() -> Optional[ctypes.CDLL]:
    """The D2XX library, loaded once, or None when it is not installed."""
    global _d2xx_lib, _d2xx_tried
    if _d2xx_tried:
        return _d2xx_lib
    _d2xx_tried = True
    loader = ctypes.WinDLL if sys.platform.startswith("win") else ctypes.CDLL
    for name in _lib_names():
        if os.sep in name and not os.path.exists(name):
            continue
        try:
            lib = loader(name)
        except OSError:
            continue
        _setup_prototypes(lib)
        _d2xx_lib = lib
        break
    return _d2xx_lib


def _setup_prototypes(lib):
    H, U32, PU32, U8, U16 = (ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.c_uint8, ctypes.c_uint16)
    protos = {
        "FT_CreateDeviceInfoList": [PU32],
        "FT_GetDeviceInfoList": [ctypes.POINTER(FT_DEVICE_LIST_INFO_NODE), PU32],
        "FT_Open": [ctypes.c_int, ctypes.POINTER(H)],
        "FT_OpenEx": [ctypes.c_void_p, U32, ctypes.POINTER(H)],
        "FT_Close": [H],
        "FT_SetBaudRate": [H, U32],
        "FT_SetDataCharacteristics": [H, U8, U8, U8],
        "FT_SetFlowControl": [H, U16, U8, U8],
        "FT_SetDtr": [H], "FT_ClrDtr": [H], "FT_SetRts": [H], "FT_ClrRts": [H],
        "FT_SetTimeouts": [H, U32, U32],
        "FT_SetLatencyTimer": [H, U8],
        "FT_Purge": [H, U32],
        "FT_SetBreakOn": [H], "FT_SetBreakOff": [H],
        "FT_Read": [H, ctypes.c_void_p, U32, PU32],
        "FT_Write": [H, ctypes.c_void_p, U32, PU32],
        "FT_GetQueueStatus": [H, PU32],
        "FT_SetVIDPID": [U32, U32],
    }
    for name, args in protos.items():
        fn = getattr(lib, name, None)
        if fn is None:
            continue
        fn.argtypes = args
        fn.restype = ctypes.c_uint32


def _check(status: int, what: str):
    if status != 0:
        raise FtdiError("%s failed: FT_%s" % (what, _FT_STATUS.get(status, str(status))))


def _register_nonstandard(lib):
    """Linux/macOS D2XX only sees FTDI's own PIDs unless told otherwise."""
    if sys.platform.startswith("win") or not hasattr(lib, "FT_SetVIDPID"):
        return
    for vid, pid, _ in KNOWN_NONSTANDARD:
        try:
            lib.FT_SetVIDPID(vid, pid)
        except Exception:
            pass
    env_vid, env_pid = os.environ.get("FTDI_VID"), os.environ.get("FTDI_PID")
    if env_vid and env_pid:
        try:
            lib.FT_SetVIDPID(int(env_vid, 0), int(env_pid, 0))
        except Exception:
            pass


def list_d2xx_devices() -> List[Tuple[str, str]]:
    """(port name, label) for every device the D2XX driver enumerates."""
    lib = d2xx()
    if lib is None:
        return []
    out = []
    try:
        _register_nonstandard(lib)
        n = ctypes.c_uint32(0)
        if lib.FT_CreateDeviceInfoList(ctypes.byref(n)) != 0 or n.value == 0:
            return []
        nodes = (FT_DEVICE_LIST_INFO_NODE * n.value)()
        if lib.FT_GetDeviceInfoList(nodes, ctypes.byref(n)) != 0:
            return []
        for i in range(n.value):
            nd = nodes[i]
            serial = nd.SerialNumber.decode("ascii", "replace").strip()
            desc = nd.Description.decode("ascii", "replace").strip() or "FTDI device"
            busy = " (in use)" if nd.Flags & FT_FLAGS_OPENED else ""
            if serial:
                name = "FTDI:" + serial
                label = "%s — %s, S/N %s (FTDI direct)%s" % (name, desc, serial, busy)
            else:
                name = "FTDIIDX:%d" % i
                label = "%s — %s, no serial (FTDI direct)%s" % (name, desc, busy)
            out.append((name, label))
    except Exception:
        return out
    return out


class D2xxPort:
    """pyserial-shaped wrapper over a D2XX handle."""

    def __init__(self, name: str, baudrate: int = 10400, timeout: float = 8.0,
                 write_timeout: Optional[float] = None, **_ignored):
        lib = d2xx()
        if lib is None:
            raise FtdiError("FTDI D2XX library not found (ftd2xx64.dll / libftd2xx); "
                            "install FTDI's driver (ftdichip.com, D2XX/VCP) or use the "
                            "cable's COM port.")
        self._lib = lib
        self._h = ctypes.c_void_p()
        self.name = name
        _register_nonstandard(lib)
        up = name.upper()
        if up.startswith("FTDIIDX:"):
            _check(lib.FT_Open(int(name[8:]), ctypes.byref(self._h)), "FT_Open(%s)" % name[8:])
        elif up.startswith("FTDI:"):
            serial = name[5:].encode("ascii")
            _check(lib.FT_OpenEx(ctypes.c_char_p(serial), FT_OPEN_BY_SERIAL_NUMBER,
                                 ctypes.byref(self._h)), "FT_OpenEx(%s)" % name[5:])
        else:
            raise FtdiError("not an FTDI port name: %r" % name)
        self._baud = 0
        self._timeout = timeout
        self._wtimeout = write_timeout if write_timeout is not None else timeout
        self._break = False
        self._dtr = self._rts = False
        try:
            self.baudrate = baudrate
            _check(lib.FT_SetDataCharacteristics(self._h, FT_BITS_8, FT_STOP_BITS_1, FT_PARITY_NONE),
                   "FT_SetDataCharacteristics")
            _check(lib.FT_SetFlowControl(self._h, FT_FLOW_NONE, 0, 0), "FT_SetFlowControl")
            self.rts = False
            self.dtr = True
            self.timeout = timeout
            _check(lib.FT_SetLatencyTimer(self._h, LATENCY_MS), "FT_SetLatencyTimer")
            self.reset_input_buffer()
        except Exception:
            self.close()
            raise

    # -- settings --
    @property
    def baudrate(self) -> int:
        return self._baud

    @baudrate.setter
    def baudrate(self, baud: int):
        _check(self._lib.FT_SetBaudRate(self._h, int(baud)), "FT_SetBaudRate(%d)" % baud)
        self._baud = int(baud)

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, seconds: Optional[float]):
        self._timeout = seconds
        rd = int((seconds if seconds is not None else 0) * 1000)
        wr = int((self._wtimeout if self._wtimeout is not None else 0) * 1000)
        _check(self._lib.FT_SetTimeouts(self._h, rd, wr), "FT_SetTimeouts")

    @property
    def write_timeout(self) -> Optional[float]:
        return self._wtimeout

    @write_timeout.setter
    def write_timeout(self, seconds: Optional[float]):
        self._wtimeout = seconds
        self.timeout = self._timeout

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, on: bool):
        _check((self._lib.FT_SetBreakOn if on else self._lib.FT_SetBreakOff)(self._h), "FT_SetBreak")
        self._break = bool(on)

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, on: bool):
        _check((self._lib.FT_SetDtr if on else self._lib.FT_ClrDtr)(self._h), "FT_SetDtr")
        self._dtr = bool(on)

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, on: bool):
        _check((self._lib.FT_SetRts if on else self._lib.FT_ClrRts)(self._h), "FT_SetRts")
        self._rts = bool(on)

    @property
    def in_waiting(self) -> int:
        n = ctypes.c_uint32(0)
        _check(self._lib.FT_GetQueueStatus(self._h, ctypes.byref(n)), "FT_GetQueueStatus")
        return n.value

    @property
    def is_open(self) -> bool:
        return bool(self._h)

    # -- I/O --
    def read(self, size: int = 1) -> bytes:
        """Like pyserial: up to ``size`` bytes, fewer when the timeout expires.
        D2XX blocks a read of N bytes until all N arrive or the timeout
        elapses (returning what it has), which is exactly pyserial's rule."""
        if size <= 0:
            return b""
        buf = (ctypes.c_ubyte * size)()
        got = ctypes.c_uint32(0)
        _check(self._lib.FT_Read(self._h, buf, size, ctypes.byref(got)), "FT_Read")
        return bytes(buf[:got.value])

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if not data:
            return 0
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        wrote = ctypes.c_uint32(0)
        _check(self._lib.FT_Write(self._h, buf, len(data), ctypes.byref(wrote)), "FT_Write")
        if wrote.value != len(data):
            raise FtdiError("FT_Write wrote %d of %d bytes" % (wrote.value, len(data)))
        return wrote.value

    def flush(self):
        pass                                    # FT_Write is synchronous

    def reset_input_buffer(self):
        _check(self._lib.FT_Purge(self._h, FT_PURGE_RX), "FT_Purge")

    def reset_output_buffer(self):
        _check(self._lib.FT_Purge(self._h, FT_PURGE_TX), "FT_Purge")

    def close(self):
        if self._h:
            try:
                self._lib.FT_ClrDtr(self._h)
            except Exception:
                pass
            try:
                self._lib.FT_Close(self._h)
            finally:
                self._h = ctypes.c_void_p()


# ================================================================= pyftdi
def _pyftdi():
    if sys.platform.startswith("win"):
        return None                             # would need WinUSB instead of the VCP driver
    try:
        import importlib
        from pyftdi.ftdi import Ftdi
        importlib.import_module("pyftdi.serialext")       # registers the ftdi:// scheme
    except Exception:
        return None
    for vid, pid, name in KNOWN_NONSTANDARD:
        try:
            Ftdi.add_custom_product(vid, pid, name.lower().replace(" ", "-"))
        except Exception:
            pass
    return Ftdi


def list_pyftdi_devices() -> List[Tuple[str, str]]:
    Ftdi = _pyftdi()
    if Ftdi is None:
        return []
    out = []
    try:
        for desc, iface in Ftdi.list_devices():
            if iface != 1:
                continue
            name = "ftdi://0x%04x:0x%04x:%d:%d/1" % (desc.vid, desc.pid, desc.bus or 0, desc.address or 0)
            label = "%s — %s%s (FTDI via libusb)" % (
                name, desc.description or "FTDI device",
                (", S/N " + desc.sn) if desc.sn else "")
            out.append((name, label))
    except Exception:
        return out
    return out


def open_pyftdi(url: str, baudrate: int = 10400, timeout: float = 8.0,
                write_timeout: Optional[float] = None, **_ignored):
    Ftdi = _pyftdi()
    if Ftdi is None:
        raise FtdiError("pyftdi is not available on this platform")
    from pyftdi.serialext import serial_for_url
    port = serial_for_url(url, baudrate=baudrate, timeout=timeout,
                          write_timeout=write_timeout if write_timeout is not None else timeout)
    try:
        port.udev.set_latency_timer(LATENCY_MS)
        port.udev.set_dynamic_latency(LATENCY_MS, LATENCY_MS, 0)   # keep it fixed
    except Exception:
        pass
    return port


# ================================================================== facade
def list_ftdi_ports() -> List[Tuple[str, str]]:
    """Every directly-drivable FTDI cable, D2XX entries first."""
    out = list_d2xx_devices()
    if not out:
        out = list_pyftdi_devices()
    return out


def open_port(name: str, baudrate: int = 10400, timeout: float = 8.0,
              write_timeout: Optional[float] = None):
    """Open an ``FTDI:`` / ``FTDIIDX:`` / ``ftdi://`` port; pyserial-shaped."""
    if name.lower().startswith(PYFTDI_PREFIX):
        return open_pyftdi(name, baudrate, timeout, write_timeout)
    return D2xxPort(name, baudrate, timeout, write_timeout)


def linux_latency_hint(port_name: str) -> Optional[str]:
    """For a Linux ttyUSB on ftdi_sio: the sysfs file that holds the latency
    timer, so the caller can lower it (needs write permission)."""
    if not sys.platform.startswith("linux") or not port_name.startswith("/dev/ttyUSB"):
        return None
    p = "/sys/bus/usb-serial/devices/%s/latency_timer" % os.path.basename(port_name)
    return p if os.path.exists(p) else None


def try_lower_sysfs_latency(port_name: str) -> bool:
    p = linux_latency_hint(port_name)
    if not p:
        return False
    try:
        with open(p, "w") as fh:
            fh.write(str(LATENCY_MS))
        return True
    except Exception:
        return False


__all__ = ["LATENCY_MS", "FtdiError", "D2xxPort", "is_ftdi_name", "list_ftdi_ports",
           "list_d2xx_devices", "list_pyftdi_devices", "open_port", "open_pyftdi",
           "try_lower_sysfs_latency", "d2xx"]
