# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tactrix OpenPort 2.0 (and its clones) over USB, without the vendor DLL.

The OpenPort is a J2534 device whose firmware speaks a small ASCII command
protocol on two bulk endpoints (USB ``0403:CC4D``).  The Windows J2534 DLL
is only a thin translator for it, so speaking the protocol directly from
pyusb gives the same device on Linux and macOS -- and on Windows too when a
WinUSB/libusb driver is bound to its data interface (Zadig: "Interface 1" --
see :class:`UsbTransport`), although there the vendor DLL
through :mod:`ovtsuite.flash.j2534` is the normal route.

Protocol (from the BSD-licensed reference driver, NikolaKozina/j2534):

* open: ``\\r\\n\\r\\nati\\r\\n`` -> ``ari <firmware>``, then ``ata`` -> ``aro``;
  close ``atz``.
* ``ato<proto> <flags> <baud> 0`` opens a channel (channel id = protocol id),
  ``atc<chan>`` closes it; ``ats<chan> <param> <value>`` is SET_CONFIG,
  ``atf<chan> <type> <flags> <len>`` + mask + pattern [+ flow] starts a
  filter (``arf <id>``), ``att<chan> <len> <flags>`` + bytes transmits,
  ``aty<chan> <len> 0`` + bytes performs a fast init, ``atr 16`` reads the
  battery pin (``arr ... <mV>``).  Errors come back as ``are <J2534 code>``.
* received messages are binary packets ``a r <chan> <len> <type> <ts:4>
  <data>``: ``len`` counts type + timestamp + data, so a packet is ``len + 4``
  bytes; types 0x80 / 0xA0 start an RX / loopback message, 0x00 / 0x20 carry
  data (CAN: 4-byte id + data), 0x40 / 0x44 / 0x60 end it, 0x10 is TX-done.
  CAN data packets are complete messages; K-line data packets have no
  timestamp and their bytes accumulate between the start and end
  indications.

:class:`OpenPort` / :class:`OpenPortChannel` mirror :class:`j2534.PassThru`
/ :class:`j2534.Channel` so the sniffer, logging and flashing code can hold
either.  The USB layer is a two-method transport (``write`` / ``read``) so
tests drive the parser without hardware.
"""
import time
from collections import deque
from typing import Deque, List, Optional

from . import j2534 as J

VID, PID = 0x0403, 0xCC4D
MAX_LEN = 4128

# packet types
TX_DONE = 0x10
TX_LB_START_IND = 0xA0
NORM_MSG_START_IND = 0x80
TX_LB_MSG = 0x20
NORM_MSG = 0x00
RX_MSG_END_IND = 0x40
EXT_ADDR_MSG_END_IND = 0x44
LB_MSG_END_IND = 0x60
# Text replies are ``ar<letter>...\r\n``; anything else after "ar" is a channel
# digit (a binary packet). A letter missing here is not a line to this parser,
# so its reply is thrown away byte by byte -- which is what happened to 'arw'
# (5-baud init) before it was listed. The vendor DLL's reply dispatch
# (op20pt32.dll, 0x10018036) also knows b/m/p, answers to commands this driver
# never sends; they stay out, since every extra letter is one more way for
# stray bytes to look like the start of a line.
_TEXT_REPLIES = b"oeifgrysw"         # aro are ari arf arg arr ary ars arw


class OpenPortError(J.J2534Error):
    pass


# ------------------------------------------------------------- transports
class UsbTransport:
    """pyusb bulk endpoints of one OpenPort.

    The OpenPort 2.0 enumerates as CDC-ACM: interface 0 is the communications
    interface (one interrupt IN endpoint, 0x81) and the data channel this
    protocol runs on is interface 1 (bulk OUT 0x02 / IN 0x82). Neither the
    interface nor the endpoints are assumed: the active configuration is scanned
    for the interface that carries a bulk IN + OUT pair, and that one is claimed.
    On Linux ``cdc_acm`` binds the device (it appears as /dev/ttyACM*), so the
    kernel driver is detached from that interface first and reattached on close.
    """

    def __init__(self, dev=None):
        try:
            import usb.core
            import usb.util
        except Exception as ex:                     # pragma: no cover
            raise OpenPortError("pyusb is not installed (%s)" % ex)
        self.usb = usb
        if dev is None:
            dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise OpenPortError("no OpenPort 2.0 found on USB (0403:CC4D)")
        self.dev = dev
        self.iface = None
        self._reattach = False
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            # Unconfigured. Only then: SET_CONFIGURATION on a configured device is
            # a reset on Linux, and is refused while a kernel driver holds it.
            dev.set_configuration()
            cfg = dev.get_active_configuration()
        intf, self.ep_in, self.ep_out = find_bulk_interface(cfg, usb.util)
        if intf is None:
            raise OpenPortError("OpenPort: no interface has a bulk IN + OUT endpoint pair "
                                "(%s)" % describe_configuration(cfg, usb.util))
        n = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(n):
                dev.detach_kernel_driver(n)
                self._reattach = True
        except (NotImplementedError, usb.core.USBError):
            pass                                    # Windows / no driver bound / macOS
        try:
            usb.util.claim_interface(dev, n)
        except usb.core.USBError as ex:
            raise OpenPortError(
                "could not claim OpenPort interface %d (%s). Check USB access (the udev "
                "rule for 0403:cc4d) and that no other program holds the cable." % (n, ex))
        if intf.bAlternateSetting:
            intf.set_altsetting()
        self.iface = n
        self.chunk = max(64, self.ep_in.wMaxPacketSize)

    def write(self, data: bytes):
        self.ep_out.write(data, timeout=2000)

    def read(self, timeout_ms: int) -> bytes:
        try:
            return bytes(self.ep_in.read(self.chunk * 8, timeout=max(1, timeout_ms)))
        except self.usb.core.USBTimeoutError:
            return b""
        except self.usb.core.USBError as ex:
            if getattr(ex, "errno", None) in (110, 60) or "timed out" in str(ex).lower():
                return b""
            raise OpenPortError("USB read failed: %s" % ex)

    def close(self):
        try:
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        if self._reattach:                          # give /dev/ttyACM* back
            self._reattach = False
            try:
                self.dev.attach_kernel_driver(self.iface)
            except Exception:
                pass

    @property
    def label(self) -> str:
        try:
            sn = self.usb.util.get_string(self.dev, self.dev.iSerialNumber) or ""
        except Exception:
            sn = ""
        return "OpenPort 2.0 (USB 0403:CC4D%s)" % ((", S/N " + sn) if sn else "")


def find_bulk_interface(cfg, util):
    """``(interface, ep_in, ep_out)`` for the first interface (alt setting) in the
    configuration descriptor that has both a bulk IN and a bulk OUT endpoint, or
    ``(None, None, None)``. ``util`` is :mod:`usb.util` (passed in so tests can
    run on descriptor stand-ins)."""
    for intf in cfg:
        ep_in = ep_out = None
        for ep in intf:
            if util.endpoint_type(ep.bmAttributes) != util.ENDPOINT_TYPE_BULK:
                continue
            if util.endpoint_direction(ep.bEndpointAddress) == util.ENDPOINT_IN:
                ep_in = ep_in or ep
            else:
                ep_out = ep_out or ep
        if ep_in is not None and ep_out is not None:
            return intf, ep_in, ep_out
    return None, None, None


def describe_configuration(cfg, util) -> str:
    """One line per interface -- class and endpoints -- for error messages."""
    kinds = {util.ENDPOINT_TYPE_BULK: "bulk", util.ENDPOINT_TYPE_INTR: "intr",
             util.ENDPOINT_TYPE_ISO: "iso", util.ENDPOINT_TYPE_CTRL: "ctrl"}
    parts = []
    for intf in cfg:
        eps = ", ".join("%s %s 0x%02X" % (
            kinds.get(util.endpoint_type(ep.bmAttributes), "?"),
            "IN" if util.endpoint_direction(ep.bEndpointAddress) == util.ENDPOINT_IN else "OUT",
            ep.bEndpointAddress) for ep in intf)
        parts.append("if %d/alt %d class %d: %s" % (
            intf.bInterfaceNumber, intf.bAlternateSetting, intf.bInterfaceClass, eps or "no endpoints"))
    return "; ".join(parts) or "no interfaces"


def explain_absence() -> str:
    """Why :func:`find_devices` found nothing, in words a user can act on --
    "" when an OpenPort is on the bus. find_devices() swallows every error so
    a scan never breaks a device picker; this says which one it was."""
    try:
        import usb.core
    except Exception:
        return "pyusb is not installed"
    try:
        devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or [])
    except usb.core.NoBackendError:
        return ("libusb-1.0 not found (Linux: install the libusb-1.0-0 package; "
                "macOS: brew install libusb)")
    except Exception as ex:
        return "USB scan failed: %s" % ex
    return "" if devs else "no OpenPort 2.0 on USB (0403:CC4D)"


def find_devices() -> List[str]:
    """Labels of the OpenPorts currently on USB (empty without pyusb)."""
    try:
        import usb.core
        import usb.util
    except Exception:
        return []
    out = []
    try:
        for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or []:
            try:
                sn = usb.util.get_string(dev, dev.iSerialNumber) or ""
            except Exception:
                sn = ""
            out.append("OpenPort 2.0 bus %d addr %d%s" % (dev.bus, dev.address, (" S/N " + sn) if sn else ""))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- parser
class _Stream:
    """Splits the bulk-in byte stream into text lines and message packets."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data

    def next(self):
        """('text', line_str) | ('pkt', chan, type, ts, payload) | None."""
        b = self.buf
        while True:
            if len(b) < 3:
                return None
            if b[0] != 0x61 or b[1] != 0x72:                  # 'a' 'r'
                del b[0]
                continue
            c = b[2]
            if c in _TEXT_REPLIES:
                end = b.find(b"\r\n")
                if end < 0:
                    return None
                line = bytes(b[:end]).decode("ascii", "replace")
                del b[:end + 2]
                return ("text", line)
            if 0x30 <= c <= 0x39:                              # channel digit
                if len(b) < 4:
                    return None
                ln = b[3]
                total = ln + 4
                if len(b) < total:
                    return None
                ptype = b[4]
                body = bytes(b[5:total])           # timestamp (when present) + data
                del b[:total]
                return ("pkt", c - 0x30, ptype, body)
            del b[0]                                            # not a packet

    def take_raw(self, n: int) -> bytes:
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


# --------------------------------------------------------------- channel
class OpenPortChannel:
    def __init__(self, dev: "OpenPort", protocol: int, flags: int = 0):
        self.dev = dev
        self.id = protocol                          # firmware: channel id == protocol id
        self.protocol = protocol
        self.flags = flags
        self._filters: List[int] = []

    # ---- filters ----
    def pass_all(self, extended: bool = False):
        n = 4 if self.protocol in (J.CAN, J.ISO15765) else 1
        self.add_filter(J.PASS_FILTER, bytes(n), bytes(n), J.CAN_29BIT_ID if extended else 0)

    def add_filter(self, ftype: int, mask: bytes, pattern: bytes, flags: int = 0,
                   flow: Optional[bytes] = None) -> int:
        if len(mask) != len(pattern) or len(mask) > 12:
            raise OpenPortError("filter mask/pattern must have equal length <= 12")
        cmd = b"atf%d %d %d %d\r\n" % (self.id, ftype, flags, len(mask)) + bytes(mask) + bytes(pattern)
        if flow is not None:
            cmd += bytes(flow)
        line = self.dev._cmd(cmd, "arf")
        try:
            fid = int(line.split()[1])
        except (IndexError, ValueError):
            raise OpenPortError("bad filter reply %r" % line)
        self._filters.append(fid)
        return fid

    def clear_filters(self):
        for fid in list(self._filters):
            try:
                self.dev._cmd(b"atk%d %d\r\n" % (self.id, fid))
            except OpenPortError:
                pass
        self._filters = []

    # ---- messages ----
    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[J.Msg]:
        return self.dev._read_msgs(self.id, max_msgs, timeout_ms)

    def write(self, data: bytes, flags: int = 0, timeout_ms: int = 100):
        data = bytes(data)
        if not data or len(data) > MAX_LEN:
            raise OpenPortError("invalid message size %d" % len(data))
        self.dev._send(b"att%d %d %d\r\n" % (self.id, len(data), flags) + data)

    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        # native protocol has no confirm-transmission wait to control either way --
        # timeout_ms is accepted (so Tp20Channel._tx's uniform call signature works
        # here too) and simply unused.
        self.write(can_id.to_bytes(4, "big") + bytes(payload), J.CAN_29BIT_ID if extended else 0, timeout_ms)

    # ---- config ----
    def set_config(self, **params):
        for k, v in params.items():
            self.dev._cmd(b"ats%d %d %d\r\n" % (self.id, getattr(J, k), int(v)))

    def set_baud(self, baud: int):
        self.set_config(DATA_RATE=baud)

    def clear_rx(self):
        self.dev._pending = deque(m for m in self.dev._pending if m[0] != self.id)

    def clear_tx(self):
        pass

    def five_baud_init(self, address: int) -> bytes:
        """ISO 9141 / KWP 5-baud (slow) init; returns the two key bytes, the same
        contract as the vendor DLL's ``FIVE_BAUD_INIT`` (j2534.Channel).

        The open reference driver never implemented it; the firmware command is
        read from Tactrix's op20pt32.dll, the DLL the Windows slow init runs
        through. PassThruIoctl hands channel ioctls to a table indexed by id-1;
        entry 4 (FIVE_BAUD_INIT) calls 0x10019E70, which accepts exactly one
        address byte, sends ``atw<chan> <address>`` and waits 5000 ms. The reply
        parser's 'w' branch (0x10018A86) reads ``arw<chan> <kb1> <kb2>`` with
        "%d %d %d" and keeps the second and third numbers as the key bytes.
        """
        line = self.dev._cmd(b"atw%d %d\r\n" % (self.id, address & 0xFF), "arw",
                             timeout_ms=6000)
        parts = line[3:].split()            # "<chan> <kb1> <kb2>"
        try:
            return bytes([int(parts[1]) & 0xFF, int(parts[2]) & 0xFF])
        except (IndexError, ValueError):
            raise OpenPortError("bad 5-baud init reply %r" % line)

    def fast_init(self, request: bytes) -> bytes:
        request = bytes(request)
        line = self.dev._cmd(b"aty%d %d 0\r\n" % (self.id, len(request)) + request, "ary")
        try:
            n = int(line.split()[1])
        except (IndexError, ValueError):
            raise OpenPortError("bad fast-init reply %r" % line)
        return self.dev._read_raw(n, 500)

    def disconnect(self):
        try:
            self.dev._cmd(b"atc%d\r\n" % self.id)
        finally:
            self.id = None


# ---------------------------------------------------------------- device
class OpenPort:
    """One opened OpenPort. Use as a context manager."""

    def __init__(self, transport=None):
        self.t = transport if transport is not None else UsbTransport()
        self._stream = _Stream()
        self._pending: Deque = deque()             # (chan, Msg) ready for read()
        self._partial = {}                          # chan -> [rx_status, ts, bytearray]
        self.firmware = ""
        self.channels: List[OpenPortChannel] = []
        line = self._cmd(b"\r\n\r\nati\r\n", "ari", timeout_ms=2000)
        self.firmware = line[4:].strip()
        self._cmd(b"ata\r\n", timeout_ms=2000)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def label(self) -> str:
        base = getattr(self.t, "label", "OpenPort 2.0")
        return "%s fw %s" % (base, self.firmware) if self.firmware else base

    # ---- low level ----
    def _send(self, data: bytes):
        self.t.write(data)

    def _pump(self, timeout_ms: int) -> bool:
        """One bulk read into the stream; False on timeout."""
        data = self.t.read(timeout_ms)
        if not data:
            return False
        self._stream.feed(data)
        return True

    def _cmd(self, data: bytes, expect: str = "aro", timeout_ms: int = 2000) -> str:
        """Send and wait for the ``expect`` reply line (``are`` raises).
        Message packets that arrive meanwhile are queued."""
        self._send(data)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            item = self._stream.next()
            if item is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OpenPortError("OpenPort: no %r reply to %r" % (expect, data[:12]))
                self._pump(int(left * 1000))
                continue
            if item[0] == "pkt":
                self._on_packet(*item[1:])
                continue
            line = item[1]
            if line.startswith("are"):
                try:
                    code = int(line[4:].split()[0])
                except (IndexError, ValueError):
                    code = -1
                raise OpenPortError("OpenPort: %s (%s)" % (J.ERR_NAMES.get(code, "error %d" % code),
                                                          data[:12].decode("ascii", "replace").strip()))
            if line.startswith(expect):
                return line
            # some other reply (e.g. a late aro): keep waiting

    def _read_raw(self, n: int, timeout_ms: int) -> bytes:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while len(self._stream.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0 or not self._pump(int(left * 1000)):
                break
        return self._stream.take_raw(n)

    def _on_packet(self, chan: int, ptype: int, body: bytes):
        kline = chan in (J.ISO9141, J.ISO14230)
        if kline and ptype in (NORM_MSG, TX_LB_MSG):
            ts, payload = 0, body                   # K-line data packets carry no timestamp
        else:
            ts = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
            payload = body[4:]
        if ptype == TX_DONE:
            self._pending.append((chan, J.Msg(chan, J.TX_DONE, 0, ts, payload)))
        elif ptype in (NORM_MSG_START_IND, TX_LB_START_IND):
            status = J.TX_MSG_TYPE if ptype == TX_LB_START_IND else 0
            if kline:
                self._partial[chan] = [status, ts, bytearray(payload)]
            elif chan == J.ISO15765:
                self._pending.append((chan, J.Msg(chan, status | J.START_OF_MESSAGE, 0, ts, payload)))
            # plain CAN: the start indication carries nothing the data packet lacks
        elif ptype in (NORM_MSG, TX_LB_MSG):
            status = J.TX_MSG_TYPE if ptype == TX_LB_MSG else 0
            if kline:
                part = self._partial.get(chan)
                if part is None:
                    part = self._partial[chan] = [status, ts, bytearray()]
                part[2] += payload
            else:
                self._pending.append((chan, J.Msg(chan, status, 0, ts, payload)))
        elif ptype in (RX_MSG_END_IND, EXT_ADDR_MSG_END_IND, LB_MSG_END_IND):
            if kline:
                part = self._partial.pop(chan, None)
                if part is not None:
                    self._pending.append((chan, J.Msg(chan, part[0], 0, ts or part[1], bytes(part[2]))))
            elif payload:
                status = J.TX_MSG_TYPE if ptype == LB_MSG_END_IND else 0
                self._pending.append((chan, J.Msg(chan, status, 0, ts, payload)))
        # unknown types are dropped

    def _drain(self):
        while True:
            item = self._stream.next()
            if item is None:
                return
            if item[0] == "pkt":
                self._on_packet(*item[1:])
            # stray text replies outside a command are ignored

    def _read_msgs(self, chan: int, max_msgs: int, timeout_ms: int) -> List[J.Msg]:
        deadline = time.monotonic() + timeout_ms / 1000.0
        out: List[J.Msg] = []
        while True:
            self._drain()
            keep: Deque = deque()
            for c, m in self._pending:
                if c == chan and len(out) < max_msgs:
                    out.append(m)
                else:
                    keep.append((c, m))
            self._pending = keep
            if len(out) >= max_msgs:
                return out
            # gather whatever the device already has; block only while empty
            left = deadline - time.monotonic()
            wait = 1 if out else int(max(0, left) * 1000)
            if (left <= 0 and not out) or not self._pump(wait):
                return out

    # ---- public ----
    def connect(self, protocol: int, baud: int, flags: int = 0) -> OpenPortChannel:
        self._cmd(b"ato%d %d %d 0\r\n" % (protocol, flags, baud))
        ch = OpenPortChannel(self, protocol, flags)
        self.channels.append(ch)
        return ch

    def battery_voltage(self) -> Optional[float]:
        try:
            line = self._cmd(b"atr 16\r\n", "arr")
            return int(line.split()[-1]) / 1000.0
        except (OpenPortError, ValueError, IndexError):
            return None

    def ioctl(self, *a, **k):
        raise OpenPortError("raw ioctls are not available on the USB protocol")

    def close(self):
        for ch in self.channels:
            if ch.id is not None:
                try:
                    ch.disconnect()
                except OpenPortError:
                    pass
        self.channels = []
        try:
            self._send(b"atz\r\n")
        except Exception:
            pass
        try:
            self.t.close()
        except Exception:
            pass


__all__ = ["VID", "PID", "OpenPort", "OpenPortChannel", "OpenPortError", "UsbTransport",
           "find_devices"]
