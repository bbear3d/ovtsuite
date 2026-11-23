# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Direct CH340 / CH341 USB-serial access through libusb (pyusb), bypassing the OS driver.

Why: Apple's built-in CH340 driver (``AppleUSBCHCOM``, macOS 11+) corrupts any
burst of more than ~32 received bytes -- measured on the bench against an EDC16:
packets of up to 32 data bytes arrive clean at 124800 baud, 40 bytes and up
arrive with bytes lost or garbled, so every 254-byte upload packet at the
Medium / High speeds fails.  Talking to the chip ourselves (vendor control
requests for baud/line/modem, bulk endpoints for data) with a thread that
keeps the bulk-in pipe drained does not have that limit.

The register protocol follows the FreeBSD / NetBSD ``uchcom`` driver
(BSD-2-Clause, (c) 2007 Takanori Watanabe and The NetBSD Foundation, Inc.).
This module is an independent Python implementation; it is not derived from
any GPL source.  A port name is
``ch34x://VID:PID:SERIAL-or-BUS:ADDR``; :func:`list_ch34x_devices` yields them
and :class:`Ch34xPort` is pyserial-shaped (what :class:`transport.KLine` needs).
"""
import re
import sys
import threading
import time
from typing import List, Optional, Tuple

from .. import realtime as _realtime

CH34X_PREFIX = "ch34x://"
VID = 0x1A86
#: product ids of the CH340/CH341 serial bridges (7523 = CH340, 5523 = CH341)
PIDS = (0x7523, 0x5523)

_REQ_GET_VERSION = 0x5F
_REQ_READ_REG = 0x95
_REQ_WRITE_REG = 0x9A
_REQ_RESET = 0xA1               # also carries baud + line control (see _set_baud_lcr)
_REQ_SET_DTRRTS = 0xA4
_REG_BREAK1, _REG_LCR1 = 0x05, 0x18
_REG_BPS_PRE, _REG_BPS_DIV, _REG_BPS_PAD = 0x12, 0x13, 0x0F
_DTR_MASK, _RTS_MASK = 0x20, 0x40
_BRK_MASK = 0x01
_LCR1_RX, _LCR1_TX, _LCR1_PARENB, _LCR1_PAREVEN, _LCR1_CS8 = 0x80, 0x40, 0x08, 0x10, 0x03
_CTL = 0x08 | 0x04 | 0x90       # request-value low byte: "T | CL | CT"
_USB_TIMEOUT_MS = 1000
_READ_CHUNK = 512
_SETTLE_S = 0.03
_RX_POLL_MS = 100


class Ch34xError(Exception):
    pass


def is_ch34x_name(name: str) -> bool:
    return (name or "").lower().startswith(CH34X_PREFIX)


def _usb():
    try:
        from . import libusb_locate
        libusb_locate.install_backend()     # MacPorts / Homebrew / bundled libusb
    except Exception:
        pass
    try:
        import usb.core
        import usb.util
        return usb
    except Exception:
        return None


def divisor_for(rate: int):
    """``(divisor, factor)`` for ``rate``: the chip divides 12 MHz by one of four
    prescalers (/2, /16, /128, /1024 -> ``divisor`` 3..0) and then by
    ``256 - factor``.  Nearest-integer rounding keeps the error under half a step."""
    rate = int(rate)
    if rate <= 0 or rate > 2000000:
        raise Ch34xError("baud rate %d is not supported" % rate)
    if rate > 23529:
        clk, div = 6000000, 3
    elif rate > 2941:
        clk, div = 750000, 2
    elif rate > 367:
        clk, div = 93750, 1
    else:
        clk, div = 11719, 0
    factor = 256 - int(round(float(clk) / rate))
    if rate == 921600:
        div, factor = 7, 243
    return div, max(0, min(255, factor))


def _direct_supported() -> bool:
    """The direct-libusb path is for macOS (and is unused on Windows).  On Linux
    the OS serial port (ch341 / ttyUSB) works, so the cable is not offered here."""
    return not (sys.platform.startswith("win") or sys.platform.startswith("linux"))


def _port_path(d) -> str:
    """The USB port path (hub ports from the root, e.g. ``1.4.2``), or ''.  It
    stays the same when the cable is replugged into the same socket."""
    try:
        nums = getattr(d, "port_numbers", None)
        return ".".join(str(int(n)) for n in nums) if nums else ""
    except Exception:
        return ""


def _split_ident(ident: str):
    """``(ident, port path)`` of a port name's last field: ``SERIAL@p1.4.2`` or
    ``BUS:ADDR@p1.4.2``.  Names saved before the path was added have no ``@p``."""
    m = re.match(r"^(.*)@p([0-9.]+)$", ident)
    if m:
        return m.group(1), m.group(2)
    return ident, ""


def list_ch34x_devices() -> List[Tuple[str, str]]:
    """(port name, label) for each CH340/CH341 bridge libusb can see."""
    if not _direct_supported():
        return []
    usb = _usb()
    if usb is None:
        return []
    out = []
    try:
        for pid in PIDS:
            for d in usb.core.find(find_all=True, idVendor=VID, idProduct=pid) or ():
                try:
                    sn = d.serial_number
                except Exception:
                    sn = None
                ident = sn if sn else "%x:%x" % (d.bus or 0, d.address or 0)
                path = _port_path(d)
                if path:
                    ident += "@p" + path
                name = "%s%04x:%04x:%s" % (CH34X_PREFIX, VID, pid, ident)
                out.append((name, "%s — CH34x USB serial%s (direct via libusb)"
                            % (name, (", S/N " + sn) if sn else "")))
    except Exception:
        return out
    return out


def _find_device(name: str):
    if not _direct_supported():
        raise Ch34xError("the direct CH34x access is not used on this system; "
                         "choose the cable's serial port instead")
    usb = _usb()
    if usb is None:
        raise Ch34xError("pyusb / libusb is not available")
    m = re.match(r"(?i)^ch34x://([0-9a-f]+):([0-9a-f]+):(.*)$", name)
    if not m:
        raise Ch34xError("not a CH34x port name: %r" % name)
    vid, pid = int(m.group(1), 16), int(m.group(2), 16)
    ident, path = _split_ident(m.group(3))
    devs = list(usb.core.find(find_all=True, idVendor=vid, idProduct=pid) or ())
    for d in devs:                              # the cable's own serial number
        try:
            sn = d.serial_number
        except Exception:
            sn = None
        if sn and sn == ident:
            return d
    if path:                                    # the same socket, after a replug
        for d in devs:
            if _port_path(d) == path:
                return d
    for d in devs:                              # the same bus address
        if ident == "%x:%x" % (d.bus or 0, d.address or 0):
            return d
    if len(devs) == 1:                          # replugged elsewhere: the only one
        return devs[0]
    if devs:
        raise Ch34xError("%d CH34x cables are connected and %s is none of them; "
                         "select the cable again in the port list" % (len(devs), name))
    raise Ch34xError("no CH34x cable found for %s" % name)


class Ch34xPort:
    """pyserial-shaped port on a CH340/CH341, driven over libusb."""

    def __init__(self, name: str, baudrate: int = 10400, timeout: Optional[float] = 8.0,
                 write_timeout: Optional[float] = None, **_ignored):
        import usb.core
        import usb.util
        self._usb_core, self._usb_util = usb.core, usb.util
        self.name = name
        self._dev = _find_device(name)
        self._timeout = timeout
        self._wtimeout = write_timeout if write_timeout is not None else timeout
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._cancel = False
        self._err = None
        self._stop = False
        self._claimed = False
        self._baud = 0
        self._lcr = _LCR1_RX | _LCR1_TX | _LCR1_CS8
        self._mcr = 0
        self._break = False
        self.prompt_rx = False          # see _set_baud_lcr
        self._thread = None
        try:
            self._open(baudrate)
        except Exception:
            self.close()
            raise

    # ---- USB plumbing ----
    def _open(self, baudrate: int):
        dev = self._dev
        try:
            dev.set_configuration()
        except Exception:
            pass                                # already configured (or the OS owns it)
        try:
            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            self._intf = intf.bInterfaceNumber
            self._ep_in = self._ep_out = None
            for ep in intf:
                if self._usb_util.endpoint_type(ep.bmAttributes) != self._usb_util.ENDPOINT_TYPE_BULK:
                    continue
                if self._usb_util.endpoint_direction(ep.bEndpointAddress) == self._usb_util.ENDPOINT_IN:
                    self._ep_in = ep.bEndpointAddress
                else:
                    self._ep_out = ep.bEndpointAddress
            if self._ep_in is None or self._ep_out is None:
                raise Ch34xError("CH34x bulk endpoints not found")
            self._usb_util.claim_interface(dev, self._intf)
            self._claimed = True
        except Ch34xError:
            raise
        except Exception as ex:
            raise Ch34xError(
                "cannot take the CH34x cable from the OS driver (%s); "
                "unplug and replug it, or use its serial port instead" % ex)
        self.version = self._ctrl_in(_REQ_GET_VERSION, 0, 0, 8)[0]
        # the chip's default configuration, as the BSD driver sets it on open
        self._ctrl_out(_REQ_RESET, 0, 0)
        self._write_reg(_REG_BPS_PRE, 0x82, _REG_BPS_DIV, 0xD9)
        self._write_reg(0x2C, 0x07, _REG_BPS_PAD, 0)
        self._set_baud_lcr(baudrate, self._lcr)
        self._baud = int(baudrate)
        self._set_handshake()
        self._thread = threading.Thread(target=self._reader, name="ch34x-rx", daemon=True)
        self._thread.start()

    def _ctrl_out(self, req, value, index):
        try:
            self._dev.ctrl_transfer(0x40, req, value & 0xFFFF, index & 0xFFFF, None, _USB_TIMEOUT_MS)
        except Exception as ex:
            raise Ch34xError("CH34x control request %02X failed: %s" % (req, ex))

    def _ctrl_in(self, req, value, index, size):
        try:
            return bytes(self._dev.ctrl_transfer(0xC0, req, value & 0xFFFF, index & 0xFFFF,
                                                 size, _USB_TIMEOUT_MS))
        except Exception as ex:
            raise Ch34xError("CH34x control request %02X failed: %s" % (req, ex))

    def _write_reg(self, reg1, val1, reg2, val2):
        self._ctrl_out(_REQ_WRITE_REG, reg1 | (reg2 << 8), val1 | (val2 << 8))

    def _read_regs(self, reg1, reg2):
        r = self._ctrl_in(_REQ_READ_REG, reg1 | (reg2 << 8), 0, 8)
        return r[0], r[1]

    def _set_baud_lcr(self, baud: int, lcr: int):
        div, factor = divisor_for(baud)
        # The BSD driver always sets bit 7 of the divisor byte ("pass received
        # bytes on at once").  On a version 0x31 chip measured here it delays
        # every received byte by ~22 ms (echo of one byte: 23 ms with the bit,
        # 1.5 ms without), which breaks the ECU's inter-byte timing at the
        # 10400 baud init -- but at 30000 baud and up it is what keeps the
        # chip from losing bytes in the long 0x00/0xFF runs of a flash image.
        # ``prompt_rx`` (set by a flasher that writes byte by byte and waits for
        # each echo, e.g. EDC15's loader handshake) keeps the bit clear up to
        # 200000 baud: with it every echo takes ~23 ms and the ECU gives up
        # between bytes.  A bulk read without it loses bytes now and then (a
        # packet stalls for the 8 s timeout), so the flasher switches back with
        # :meth:`set_prompt_rx` once the loader runs.
        limit = 200000 if self.prompt_rx else 30000
        bit7 = 0x80 if (self.version < 0x30 or baud >= limit) else 0
        self._ctrl_out(_REQ_RESET, _CTL | (lcr << 8), ((factor << 8) | div | bit7))
        time.sleep(_SETTLE_S)              # bytes sent at once after the change are lost

    def set_prompt_rx(self, on: bool):
        """Switch between prompt delivery (``True``: every received byte is
        handed over at once, for a caller that writes byte by byte and waits for
        each echo) and batched delivery (``False``: the chip's default above
        30000 baud, which loses fewer bytes in a long bulk transfer).  Takes
        effect at once, so do it while the line is idle."""
        on = bool(on)
        if on == self.prompt_rx:
            return
        self.prompt_rx = on
        if self._dev is not None:
            self._set_baud_lcr(self._baud, self._lcr)

    def _set_handshake(self):
        self._ctrl_out(_REQ_SET_DTRRTS, (~self._mcr) & 0xFF, 0)

    def _raw_bulk_in(self):
        """Read the bulk-in pipe with libusb directly, or None when pyusb's
        libusb1 internals are not available.  pyusb's ``read`` raises on a
        timeout and throws away bytes that arrived in the instant the transfer
        was cancelled; ``libusb_bulk_transfer`` reports how many it got, so
        those bytes are kept."""
        try:
            import ctypes
            import usb.backend.libusb1 as lb
            be = lb.get_backend()
            lib = be.lib
            handle = self._dev._ctx.handle.handle
            buf = (ctypes.c_ubyte * _READ_CHUNK)()
            got = ctypes.c_int(0)
            ep = self._ep_in

            def read(timeout_ms):
                rc = lib.libusb_bulk_transfer(handle, ep, buf, _READ_CHUNK,
                                              ctypes.byref(got), timeout_ms)
                if rc not in (0, -7):                    # 0 ok, -7 timeout
                    raise Ch34xError("CH34x read failed (libusb error %d)" % rc)
                return bytes(buf[:got.value])
            return read
        except Exception:
            return None

    def _reader(self):
        """Keep the bulk-in pipe polled, so the chip's FIFO never overruns."""
        _realtime.begin_thread()
        try:
            self._reader_loop()
        finally:
            _realtime.end_thread()

    def _reader_loop(self):
        raw = self._raw_bulk_in()
        while not self._stop:
            try:
                if raw is not None:
                    data = raw(_RX_POLL_MS)
                else:
                    data = self._dev.read(self._ep_in, _READ_CHUNK, _RX_POLL_MS)
            except self._usb_core.USBTimeoutError:
                continue
            except Exception as ex:
                if not self._stop:
                    with self._cv:
                        self._err = ex
                        self._cv.notify_all()
                return
            if data:
                with self._cv:
                    self._buf.extend(bytes(data))
                    self._cv.notify_all()
            elif raw is None:
                time.sleep(0.005)       # an empty read must not spin the thread

    # ---- pyserial surface ----
    @property
    def baudrate(self) -> int:
        return self._baud

    @baudrate.setter
    def baudrate(self, baud: int):
        self._set_baud_lcr(int(baud), self._lcr)
        self._baud = int(baud)

    @property
    def timeout(self) -> Optional[float]:
        return self._timeout

    @timeout.setter
    def timeout(self, seconds: Optional[float]):
        self._timeout = seconds

    @property
    def write_timeout(self) -> Optional[float]:
        return self._wtimeout

    @write_timeout.setter
    def write_timeout(self, seconds: Optional[float]):
        self._wtimeout = seconds

    @property
    def parity(self) -> str:
        if not self._lcr & _LCR1_PARENB:
            return "N"
        return "E" if self._lcr & _LCR1_PAREVEN else "O"

    @parity.setter
    def parity(self, letter: str):
        letter = letter.upper()
        if letter not in ("N", "E", "O"):
            raise Ch34xError("unsupported parity %r" % letter)
        lcr = _LCR1_RX | _LCR1_TX | _LCR1_CS8
        if letter != "N":
            lcr |= _LCR1_PARENB | (_LCR1_PAREVEN if letter == "E" else 0)
        self._lcr = lcr
        self._set_baud_lcr(self._baud, lcr)

    @property
    def dtr(self) -> bool:
        return bool(self._mcr & _DTR_MASK)

    @dtr.setter
    def dtr(self, on: bool):
        self._mcr = (self._mcr | _DTR_MASK) if on else (self._mcr & ~_DTR_MASK)
        self._set_handshake()

    @property
    def rts(self) -> bool:
        return bool(self._mcr & _RTS_MASK)

    @rts.setter
    def rts(self, on: bool):
        self._mcr = (self._mcr | _RTS_MASK) if on else (self._mcr & ~_RTS_MASK)
        self._set_handshake()

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, on: bool):
        on = bool(on)
        brk1, brk2 = self._read_regs(_REG_BREAK1, _REG_LCR1)
        if on:
            brk1 &= ~_BRK_MASK
            brk2 &= ~_LCR1_TX
        else:
            brk1 |= _BRK_MASK
            brk2 |= _LCR1_TX
        self._write_reg(_REG_BREAK1, brk1, _REG_LCR1, brk2)
        self._break = on

    @property
    def in_waiting(self) -> int:
        with self._cv:
            return len(self._buf)

    @property
    def is_open(self) -> bool:
        return self._dev is not None and not self._stop

    def read(self, size: int = 1) -> bytes:
        if size <= 0:
            return b""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        with self._cv:
            self._cancel = False
            while len(self._buf) < size:
                if self._err is not None:
                    if self._buf:
                        break
                    raise Ch34xError("CH34x read failed: %s" % self._err)
                if self._cancel:
                    break
                if deadline is None:
                    self._cv.wait(0.5)
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cv.wait(left)
            n = min(size, len(self._buf))
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def cancel_read(self):
        with self._cv:
            self._cancel = True
            self._cv.notify_all()

    def _bulk_write(self, data: bytes):
        try:
            tmo = _USB_TIMEOUT_MS if self._wtimeout is None else max(1, int(self._wtimeout * 1000))
            self._dev.write(self._ep_out, data, tmo)
        except Exception as ex:
            raise Ch34xError("CH34x write failed: %s" % ex)

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if data:
            self._bulk_write(data)
        return len(data)

    def flush(self):
        pass                                    # bulk writes are synchronous

    def reset_input_buffer(self):
        with self._cv:
            self._buf.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self._stop = True
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(0.5)
        dev, self._dev = self._dev, None
        if dev is None:
            return
        try:
            if self._claimed:
                self._usb_util.release_interface(dev, self._intf)
        except Exception:
            pass
        try:
            self._usb_util.dispose_resources(dev)
        except Exception:
            pass


def open_ch34x(name: str, baudrate: int = 10400, timeout: float = 8.0,
               write_timeout: Optional[float] = None) -> Ch34xPort:
    return Ch34xPort(name, baudrate, timeout, write_timeout)


__all__ = ["CH34X_PREFIX", "Ch34xError", "Ch34xPort", "divisor_for", "is_ch34x_name",
           "list_ch34x_devices", "open_ch34x"]
