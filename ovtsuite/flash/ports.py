# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which of the OS's serial ports are real cables.

pyserial lists every serial node it finds. On Linux that includes the 32
``/dev/ttyS*`` placeholders the 8250 driver always creates whether or not a
UART exists (pyserial used to drop them as "platform" devices, but newer
kernels put them on the "serial-base" bus, so they leak through), plus
Bluetooth ``rfcomm`` links and the like; on macOS, Bluetooth and internal
debug ports. None of those can drive a K-line.

A port is listed when something structural says a cable is behind it:

* it is USB-backed -- pyserial found a USB parent device (``vid`` set; read
  from sysfs on Linux, from IOKit on macOS), or
* on Linux, the kernel reports real UART hardware behind it: ``TIOCGSERIAL``
  returns a type other than ``PORT_UNKNOWN``. Same probe KWHack and
  setserial/udev use.

Windows lists only ports a driver created, so nothing is filtered there.

The Tactrix OpenPort 2.0 is USB-backed too (CDC-ACM, so ``/dev/ttyACM*`` on
Linux, ``cu.usbmodem*`` on macOS), but it is not a serial K-line cable: it is
offered as its own J2534 device (``openport.py``), so its tty node is left out.
"""
import os
import sys
from typing import List, NamedTuple, Optional

#: Tactrix OpenPort 2.0 (openport.VID / openport.PID).
OPENPORT_IDS = (0x0403, 0xCC4D)


class PortCheck(NamedTuple):
    info: object          # serial.tools.list_ports_common.ListPortInfo
    listed: bool
    reason: str           # why it is or is not listed, for diagnostics


def _uart_hardware(device: str) -> Optional[bool]:
    """Linux: does the kernel report a real UART behind this tty node?

    True / False from ``TIOCGSERIAL``; None when it cannot be asked (the node
    cannot be opened -- permissions, in use -- or the driver has no such
    ioctl, as with Bluetooth rfcomm). Opened non-blocking and without becoming
    the controlling terminal, and closed straight away."""
    try:
        import fcntl
        import struct
        import termios
    except ImportError:
        return None
    req = getattr(termios, "TIOCGSERIAL", 0x541E)
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return None
    try:
        buf = bytearray(128)            # struct serial_struct; only `int type` is read
        fcntl.ioctl(fd, req, buf, True)
        return struct.unpack_from("i", buf, 0)[0] != 0     # 0 == PORT_UNKNOWN
    except OSError:
        return None
    finally:
        os.close(fd)


def check(p) -> PortCheck:
    """Whether one pyserial port entry is a cable worth offering, and why."""
    vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
    if (vid, pid) == OPENPORT_IDS:
        return PortCheck(p, False, "OpenPort 2.0: offered as a J2534 device, not a serial port")
    if sys.platform.startswith("win"):
        return PortCheck(p, True, "Windows port")
    if vid is not None:
        return PortCheck(p, True, "USB %04X:%04X" % (vid, pid or 0))
    if sys.platform.startswith("linux"):
        hw = _uart_hardware(p.device)
        if hw:
            return PortCheck(p, True, "UART hardware")
        if hw is False:
            return PortCheck(p, False, "no UART hardware behind it (kernel placeholder)")
        return PortCheck(p, False, "not USB, and the kernel reports no UART behind it")
    return PortCheck(p, False, "not USB-backed (Bluetooth or internal port)")


def scan() -> List[PortCheck]:
    """Every OS serial port with its verdict (see :func:`check`)."""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return [check(p) for p in sorted(list_ports.comports(), key=lambda p: p.device)]


def cable_ports():
    """The pyserial entries of the ports that are real cables."""
    return [c.info for c in scan() if c.listed]
