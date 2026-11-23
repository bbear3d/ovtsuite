# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scanmatik SM2 (and SM2 Pro) over USB, without the vendor DLL -- libusb.

The SM2 enumerates as USB ``20A2:0001`` with one vendor-class interface and a
bulk pair (OUT 0x02, IN 0x81, 64-byte packets).  Scanmatik's Windows stack is a
kernel driver (``smusb``) plus a 32-bit J2534 DLL; the DLL does all of the
J2534 work and the device only sees the packets below.  Talking to those
packets from pyusb gives the same device on Linux and macOS, and on Windows
once a WinUSB/libusb driver is bound to it.

What is established (read from ``smj2534.dll``, ``smusb64.sys`` and the three
firmware images in Reference/Driver, identical in all three):

* Frame, both directions: ``[b0][len u16 LE][chk]`` + ``len`` payload bytes with
  ``chk = (b0 + len_lo + len_hi + 0x55) & 0xFF``.  From the host ``b0`` is the
  command; from the device it is a status (0 = ok, 0xFE = unsolicited event,
  anything else an error code that the DLL maps onto J2534 errors).
* ``0x86`` ping: payload = four bytes (the DLL sends a millisecond clock),
  echoed back.  ``0x83`` device info: answered with the layout of
  :class:`Info`.
* ``0x80``..``0x82`` and ``0x8B`` are the firmware-update sequence.  The DLL
  runs it by itself when the device's build is not on its list, so a stock DLL
  re-flashes an SM2 it does not know.  This module never sends them.

The J2534 layer (channel set-up, filters, message TX/RX) is *not* here yet:
see ``docs/SM2_USB_PROTOCOL.md`` for what is known and what is open.  Until
then :meth:`Sm2.command` is the raw door, and ``python -m ovtsuite.flash.sm2usb``
is the bench tool for it.

The USB layer is a two-method transport (``write`` / ``read``) so tests drive
the framing without hardware, exactly like :mod:`ovtsuite.flash.openport`.
"""
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from . import libusb_dll
from . import openport as _op

libusb_dll.prepare()          # PyUSB loads the bundled libusb (Windows) or the one libusb_locate finds (macOS/Linux)

VID, PID = 0x20A2, 0x0001
DEFAULT_EP_OUT, DEFAULT_EP_IN = 0x02, 0x81
MAX_PAYLOAD = 0xFFFF

# host -> device
CMD_PING = 0x86
CMD_INFO = 0x83
CMD_VBATT = 0x20          # reply: u16 LE millivolts + 2 more bytes (seen 0xBF; not named)
#: Firmware-update commands. Never sent by this module: see the docstring.
FIRMWARE_UPDATE_CMDS = frozenset((0x80, 0x81, 0x82, 0x8B))
# device -> host status byte
ST_OK = 0x00
ST_EVENT = 0xFE                        # unsolicited message; the DLL queues it and reads on

#: Commands the firmware dispatches (the same 42 in builds A78730 / v31 / A111788;
#: A78730 adds 0x25).  Value = the firmware's own "state" requirement for it.
#: See docs/SM2_USB_PROTOCOL.md.
FIRMWARE_COMMANDS = {
    0x1C: 0, 0x1D: 0, 0x1E: 0, 0x50: 0, 0x51: 0, 0x52: 0,
    0x01: 1, 0x02: 1, 0x04: 1, 0x05: 1, 0x03: 1, 0x06: 1, 0x07: 1, 0x08: 1, 0x10: 1,
    0x09: 2, 0x0A: 3, 0x0B: 3, 0x28: 3, 0x0C: 0, 0x0D: 3, 0x0E: 0, 0x0F: 0, 0x53: 0,
    0x11: 0, 0x12: 0, 0x16: 0, 0x13: 1, 0x14: 1, 0x17: 1, 0x24: 1, 0x18: 1, 0x19: 1,
    0x1A: 1, 0x1B: 0, 0x23: 0, 0x1F: 0, 0x20: 0, 0x21: 1, 0x22: 0, 0x26: 1, 0x27: 0,
}


class Sm2Error(Exception):
    pass


def _timer_period(on: bool):
    """Windows only: ask for a 1 ms system timer.  At the default 15.6 ms tick a USB
    round trip occasionally stalls for 15-25 ms (measured on the bench: 6 of 56000 polls
    over 10 ms, none with 1 ms), which is longer than a KW1281 controller waits for the
    tester's complement byte -- it then resends the block and the exchange falls apart."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if on else winmm.timeEndPeriod)(1)
    except Exception:
        pass


# ------------------------------------------------------------------ framing
def checksum(b0: int, length: int) -> int:
    return (b0 + (length & 0xFF) + ((length >> 8) & 0xFF) + 0x55) & 0xFF


def pack(b0: int, payload: bytes = b"") -> bytes:
    """One frame: header + payload."""
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise Sm2Error("payload of %d bytes does not fit one frame" % len(payload))
    n = len(payload)
    return bytes([b0 & 0xFF, n & 0xFF, n >> 8, checksum(b0, n)]) + payload


@dataclass
class Packet:
    status: int                  # first header byte: ST_OK / ST_EVENT / an error code
    payload: bytes = b""

    @property
    def ok(self) -> bool:
        return self.status == ST_OK


class Stream:
    """Splits the bulk-IN byte stream into frames; a bad header is skipped one
    byte at a time, which is how the DLL resynchronises too."""

    def __init__(self):
        self.buf = bytearray()
        self.skipped = 0

    def feed(self, data: bytes):
        self.buf += data

    def next(self) -> Optional[Packet]:
        b = self.buf
        while len(b) >= 4:
            n = b[1] | (b[2] << 8)
            if b[3] != checksum(b[0], n):
                del b[0]
                self.skipped += 1
                continue
            if len(b) < 4 + n:
                return None
            pkt = Packet(b[0], bytes(b[4:4 + n]))
            del b[:4 + n]
            return pkt
        return None


# --------------------------------------------------------------- device info
@dataclass
class Info:
    """Reply to ``0x83``.  Field meaning is from the DLL's parser; the three
    16-byte strings and the 32-bit words other than ``build`` are not yet named
    (``raw`` keeps everything)."""
    build: int = 0               # build number, decimal digits as hex nibbles: 0xA111788 -> "A111788"
    word1: int = 0
    word2: int = 0
    word3: int = 0
    model: int = 0               # 1..3 = SM2 family, 5 = SM3 (not supported here); 4 is invalid
    flag: int = 0
    strings: List[str] = field(default_factory=list)
    raw: bytes = b""

    @property
    def build_name(self) -> str:
        return "%X" % self.build

    @staticmethod
    def parse(payload: bytes) -> "Info":
        p = bytes(payload)
        info = Info(raw=p)
        if len(p) >= 4:
            info.build = struct.unpack_from("<I", p, 0)[0]
        if len(p) >= 12:
            info.word1, info.word2 = struct.unpack_from("<II", p, 4)
        if len(p) >= 16:
            info.word3 = struct.unpack_from("<I", p, 12)[0]
        if len(p) >= 17:
            info.model = p[16]
        if len(p) >= 18:
            info.flag = p[17]
        for off in (18, 34, 50, 66):
            if len(p) >= off + 16:
                info.strings.append(p[off:off + 16].split(b"\0", 1)[0].decode("latin-1"))
        return info


# ----------------------------------------------------------------- transport
class UsbTransport(_op.UsbTransport):
    """pyusb bulk endpoints of one SM2: the same claim/detach logic as the
    OpenPort's, with the SM2's ids. (The bulk pair is found by scanning the
    configuration; 0x02 / 0x81 is only what the firmware descriptor says.)"""

    def __init__(self, dev=None):
        try:
            import usb.core
        except Exception as ex:                                # pragma: no cover
            raise Sm2Error("pyusb is not installed (%s)" % ex)
        if dev is None:
            dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise Sm2Error("no SM2 found on USB (%04X:%04X)" % (VID, PID))
        import usb.util
        try:
            dev.get_active_configuration()
        except NotImplementedError:
            raise Sm2Error("libusb cannot open this SM2: no WinUSB/libusb driver is bound to it "
                           "(Scanmatik's smusb driver, if installed, owns it). Windows: run "
                           "Start menu > J2534 USB driver")
        except usb.core.USBError:
            pass                                    # unconfigured: the base class sets it up
        try:
            super().__init__(dev)
        except _op.OpenPortError as ex:
            try:                                    # the base class released what it took
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            raise Sm2Error(str(ex).replace("OpenPort", "SM2"))

    def read(self, timeout_ms: int) -> bytes:
        """ONE bulk packet (``wMaxPacketSize``), or ``b""`` on timeout.  A longer
        read only completes on a short packet, and an SM2 reply that is an exact
        multiple of 64 bytes (a poll answer) is not followed by one: the read would
        wait out its timeout and pyusb would drop the partial data.  The caller
        (:meth:`Sm2.read_packet`) hands each packet to the stream parser, which
        frames by the header's length."""
        try:
            return bytes(self.ep_in.read(max(1, self.ep_in.wMaxPacketSize or 64),
                                         timeout=max(1, timeout_ms)))
        except self.usb.core.USBTimeoutError:
            return b""
        except self.usb.core.USBError as ex:
            if getattr(ex, "errno", None) in (110, 60) or "timed out" in str(ex).lower():
                return b""
            raise Sm2Error("USB read failed: %s" % ex)

    @property
    def label(self) -> str:
        try:
            sn = self.usb.util.get_string(self.dev, self.dev.iSerialNumber) or ""
        except Exception:
            sn = ""
        return "SM2 (USB %04X:%04X%s)" % (VID, PID, (", S/N " + sn) if sn else "")


def find_devices() -> List[str]:
    """Labels of the SM2s currently on USB that libusb can open."""
    try:
        import usb.core
        import usb.util
    except Exception:
        return []
    out = []
    try:
        for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or []:
            if not _op._openable(dev):
                continue
            try:
                sn = usb.util.get_string(dev, dev.iSerialNumber) or ""
            except Exception:
                sn = ""
            out.append("SM2 bus %d addr %d%s" % (dev.bus, dev.address, (" S/N " + sn) if sn else ""))
    except Exception:
        pass
    return out


def explain_absence() -> str:
    """Why :func:`find_devices` found nothing, in words a user can act on --
    "" when an SM2 is on the bus and openable."""
    try:
        import usb.core
    except Exception:
        return "pyusb is not installed"
    try:
        devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or [])
    except usb.core.NoBackendError:
        from . import libusb_locate
        return libusb_locate.not_found_message()
    except Exception as ex:
        return "USB scan failed: %s" % ex
    if not devs:
        return "no SM2 on USB (%04X:%04X)" % (VID, PID)
    if not any(_op._openable(d) for d in devs):
        return ("an SM2 is on USB but no WinUSB/libusb driver is bound to it (Windows: "
                "run the OVT-Suite J2534 USB driver add-on, Start menu > OpenPort USB "
                "driver, which also binds the SM2); Scanmatik's own driver (smusb) and "
                "its J2534 DLL keep the cable for themselves")
    return ""


# -------------------------------------------------------------------- device
class Sm2:
    """One opened SM2. Use as a context manager."""

    def __init__(self, transport=None, *, handshake: bool = True):
        self.t = transport if transport is not None else UsbTransport()
        self._stream = Stream()
        self.events: Deque[Packet] = deque()        # unsolicited (0xFE) frames, oldest first
        self.info: Optional[Info] = None
        self.lock = threading.RLock()               # one command exchange at a time (a poller thread may share it)
        self._desync = False                        # a command timed out: its reply may still arrive
        self._token_seq = 0
        self._timer = False
        _timer_period(True)
        self._timer = True
        if handshake:
            try:
                self.flush()
                self.ping()
                self.info = self.read_info()
            except Exception:
                self.close()
                raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def label(self) -> str:
        base = getattr(self.t, "label", "SM2")
        return "%s fw %s" % (base, self.info.build_name) if self.info and self.info.build else base

    # ---- low level
    def flush(self, quiet_ms: int = 30, limit_ms: int = 500):
        """Throw away whatever the device already queued (stale replies from a
        previous program)."""
        deadline = time.monotonic() + limit_ms / 1000.0
        while time.monotonic() < deadline:
            if not self.t.read(quiet_ms):
                break
        self._stream = Stream()

    def send(self, cmd: int, payload: bytes = b""):
        if cmd in FIRMWARE_UPDATE_CMDS:
            raise Sm2Error("command 0x%02X is part of the firmware-update sequence; "
                           "this driver never sends it" % cmd)
        self.t.write(pack(cmd, payload))

    def read_packet(self, timeout_ms: int = 1000) -> Optional[Packet]:
        """Next non-event frame, or None on timeout. Events are queued on ``events``."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            pkt = self._stream.next()
            if pkt is not None:
                if pkt.status == ST_EVENT:
                    self.events.append(pkt)
                    continue
                return pkt
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            data = self.t.read(max(1, int(left * 1000)))
            if data:
                self._stream.feed(data)

    def command(self, cmd: int, payload: bytes = b"", timeout_ms: int = 1000) -> Packet:
        """Send one command and return its reply frame (status + payload).
        ``Sm2Error`` on timeout; a non-zero status is returned, not raised --
        which codes mean what is still being mapped.

        A reply frame carries a status, not the command it answers, so a reply
        that arrives after its command timed out cannot be told from the next
        command's.  After a timeout the next command therefore first drains the
        input and resynchronises with a ping whose token must come back
        (:meth:`resync`); stale frames in front of it are dropped."""
        with self.lock:
            if self._desync:
                self.resync()
            self.send(cmd, payload)
            pkt = self.read_packet(timeout_ms)
            if pkt is None:
                self._desync = True
        if pkt is None:
            raise Sm2Error("no reply to command 0x%02X" % cmd)
        return pkt

    def resync(self, timeout_ms: int = 1000):
        """Drop whatever is queued from the device and prove the next frame is a
        reply to what is sent next: ping with a fresh token and discard every
        non-event frame until it is echoed.  ``Sm2Error`` (and the flag stays set,
        so the next command tries again) if the device does not answer."""
        with self.lock:
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:          # drain; events are kept
                data = self.t.read(1)
                if not data:
                    break
                self._stream.feed(data)
                while True:
                    pkt = self._stream.next()
                    if pkt is None:
                        break
                    if pkt.status == ST_EVENT:
                        self.events.append(pkt)
            self._stream = Stream()
            self._token_seq = (self._token_seq + 1) & 0xFFFF
            token = struct.pack(">HH", self._token_seq, int(time.monotonic() * 1000) & 0xFFFF)
            self.send(CMD_PING, token)
            end = time.monotonic() + timeout_ms / 1000.0
            while True:
                left = int((end - time.monotonic()) * 1000)
                pkt = self.read_packet(left) if left > 0 else None
                if pkt is None:
                    raise Sm2Error("SM2 does not answer after a timed-out command")
                if pkt.payload == token:
                    break                               # anything before it was stale
            self._desync = False

    # ---- the verified commands
    def ping(self, timeout_ms: int = 1000) -> int:
        """0x86: the device echoes the four-byte clock value. Returns the round trip in ms."""
        token = struct.pack(">I", int(time.monotonic() * 1000) & 0xFFFFFFFF)
        t0 = time.monotonic()
        pkt = self.command(CMD_PING, token, timeout_ms)
        if pkt.payload != token:
            raise Sm2Error("SM2 ping answered with %r, expected %r" % (pkt.payload, token))
        return int(round((time.monotonic() - t0) * 1000))

    def read_info(self) -> Info:
        pkt = self.command(CMD_INFO, b"", 2000)
        if not pkt.ok:
            raise Sm2Error("SM2 refused the info request (status 0x%02X)" % pkt.status)
        info = Info.parse(pkt.payload)
        if info.model == 4:
            raise Sm2Error("device reports an unsupported model code 4")
        return info

    def battery_voltage(self) -> Optional[float]:
        """Volts on the OBD battery pin (command 0x20, the DLL's READ_VBATT), or None.
        Checked on a real SM2 Pro: 0F 15 BF 00 is the 5391 mV the DLL reported."""
        try:
            pkt = self.command(CMD_VBATT, b"", 500)
        except Sm2Error:
            return None
        if not pkt.ok or len(pkt.payload) < 2:
            return None
        return struct.unpack_from("<H", pkt.payload, 0)[0] / 1000.0

    def close(self):
        if self._timer:
            self._timer = False
            _timer_period(False)
        try:
            self.t.close()
        except Exception:
            pass


# ----------------------------------------------------------------------- CLI
def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m ovtsuite.flash.sm2usb",
        description="Bench tool for the SM2 USB protocol: open, ping, print the device info, "
                    "optionally send one raw command.")
    ap.add_argument("--list", action="store_true", help="list SM2s on USB and exit")
    ap.add_argument("--cmd", help="raw command byte to send after the handshake (hex), e.g. 0x1c")
    ap.add_argument("--payload", default="", help="its payload (hex)")
    ap.add_argument("--timeout", type=int, default=1000, help="reply timeout in ms")
    ap.add_argument("--no-handshake", action="store_true", help="skip ping + info")
    a = ap.parse_args(argv)
    if a.list:
        devs = find_devices()
        for d in devs:
            print(d)
        if not devs:
            print(explain_absence() or "no SM2 found")
        return 0 if devs else 1
    try:
        dev = Sm2(handshake=not a.no_handshake)
    except Sm2Error as ex:
        print("SM2: %s" % ex, file=sys.stderr)
        return 2
    with dev:
        print(dev.label)
        if dev.info:
            i = dev.info
            print("  build %s (0x%08X)  model %d  flag %d" % (i.build_name, i.build, i.model, i.flag))
            print("  words %08X %08X %08X" % (i.word1, i.word2, i.word3))
            print("  strings %r" % (i.strings,))
            print("  raw %s" % i.raw.hex(" "))
        if not a.no_handshake:
            v = dev.battery_voltage()
            print("  battery pin %s" % ("%.3f V" % v if v is not None else "n/a"))
        if a.cmd:
            cmd = int(a.cmd, 0)
            if cmd not in FIRMWARE_COMMANDS and cmd not in (CMD_PING, CMD_INFO):
                print("  note: 0x%02X is not in the firmware's command table" % cmd)
            pkt = dev.command(cmd, bytes.fromhex(a.payload), a.timeout)
            print("  0x%02X -> status 0x%02X  %d bytes  %s" % (cmd, pkt.status, len(pkt.payload),
                                                              pkt.payload.hex(" ")))
        for ev in dev.events:
            print("  event %s" % ev.payload.hex(" "))
    return 0


__all__ = ["VID", "PID", "Sm2", "Sm2Error", "Info", "Packet", "Stream", "UsbTransport",
           "pack", "checksum", "find_devices", "explain_absence", "FIRMWARE_COMMANDS",
           "FIRMWARE_UPDATE_CMDS"]

if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(_main())
