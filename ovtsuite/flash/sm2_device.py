# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Scanmatik SM2 over libusb as a device the pickers can offer.

:class:`Sm2Device` has the face of :class:`ovtsuite.flash.openport.OpenPort`
(``connect(protocol, baud, flags)`` returning a channel, ``battery_voltage``,
``close``), so everything that already drives an OpenPort -- the flasher, the
live-data logger, diagnostics -- drives an SM2 the same way.  CAN channels are
:class:`~ovtsuite.flash.sm2can.Sm2CanChannel`, ISO 9141 / ISO 14230 ones
:class:`~ovtsuite.flash.sm2kline.Sm2KLineChannel`.

An SM2 is listed only when libusb can open it (a WinUSB driver bound to it:
Start menu > J2534 USB driver); with Scanmatik's own driver on it, the vendor
J2534 DLL entry is the way in.  One channel at a time: opening another closes
the one before.
"""
from typing import List, Optional

from . import j2534 as J
from . import sm2usb
from .sm2can import Sm2CanChannel
from .sm2kline import Sm2KLineChannel
from .sm2usb import Sm2, Sm2Error

#: picker data for an SM2 driven over libusb
SM2_USB = "sm2-usb"


class Sm2Device:
    """One opened SM2 with the OpenPort's ``connect`` face."""

    def __init__(self, sm2: Optional[Sm2] = None):
        self.sm2 = sm2 if sm2 is not None else Sm2()
        self._ch = None
        self.channels: List[object] = []

    @property
    def label(self) -> str:
        return self.sm2.label

    @property
    def firmware(self) -> str:
        info = self.sm2.info
        return info.build_name if info is not None else ""

    def connect(self, protocol: int, baud: int, flags: int = 0):
        self._close_channel()
        if protocol == J.CAN:
            ch = Sm2CanChannel(self.sm2, baud, extended=bool(flags & J.CAN_29BIT_ID),
                               background=True)
        elif protocol in (J.ISO9141, J.ISO14230):
            ch = Sm2KLineChannel(self.sm2, baud, protocol)
        else:
            raise Sm2Error("the SM2 over USB does not do protocol %d yet" % protocol)
        self._ch = ch
        self.channels.append(ch)
        return ch

    def _close_channel(self):
        ch, self._ch = self._ch, None
        if ch is not None:
            try:
                ch.disconnect()
            except Exception:
                pass

    def battery_voltage(self) -> Optional[float]:
        return self.sm2.battery_voltage()

    def ioctl(self, *a, **k):
        raise Sm2Error("raw ioctls are not available on the SM2 USB protocol")

    def close(self):
        self._close_channel()
        self.sm2.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def find_devices() -> List[str]:
    """Labels of the SM2s libusb can open."""
    return sm2usb.find_devices()


def explain_absence() -> str:
    """Why an SM2 on USB is not offered, when one is there but libusb cannot open
    it; "" when none is on USB or it is usable."""
    why = sm2usb.explain_absence()
    return why if why.startswith("an SM2 is on USB") else ""


__all__ = ["SM2_USB", "Sm2Device", "find_devices", "explain_absence"]
