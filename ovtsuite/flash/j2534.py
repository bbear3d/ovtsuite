# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SAE J2534-1 (PassThru) interface via the vendor DLL, through ctypes.

Devices register under ``HKLM\\SOFTWARE\\PassThruSupport.04.04\\<name>``
(64-bit DLLs) and ``HKLM\\SOFTWARE\\WOW6432Node\\PassThruSupport.04.04``
(32-bit DLLs); ``FunctionLibrary`` is the DLL path.  A DLL can only be
loaded by a Python of the same bitness -- :func:`list_devices` reports each
device's bitness so the GUI can say why one won't open.

Only the calls the tuner needs are wrapped: open/close, connect a protocol
channel (CAN raw, ISO 14230, ISO 9141, ISO 15765), pass filters, read/write
messages, SET_CONFIG ioctls, five-baud / fast init, clear buffers.  Message
timestamps are the device's microsecond counter.
"""
import ctypes
import os
import sys
from ctypes import (POINTER, Structure, byref, c_char, c_char_p, c_ubyte, c_uint32,
                    c_void_p, create_string_buffer)
from dataclasses import dataclass
from typing import List, Optional, Sequence

# protocol ids
J1850VPW, J1850PWM, ISO9141, ISO14230, CAN, ISO15765 = 1, 2, 3, 4, 5, 6
PROTOCOL_NAMES = {1: "J1850VPW", 2: "J1850PWM", 3: "ISO9141", 4: "ISO14230",
                  5: "CAN", 6: "ISO15765"}
# connect / tx flags
CAN_29BIT_ID = 0x00000100
ISO9141_NO_CHECKSUM = 0x00000200
CAN_ID_BOTH = 0x00000800
ISO9141_K_LINE_ONLY = 0x00001000
# rx status
TX_MSG_TYPE = 0x00000001
START_OF_MESSAGE = 0x00000002
RX_BREAK = 0x00000004
TX_DONE = 0x00000008
ISO15765_PADDING_ERROR = 0x00000010
# filters
PASS_FILTER, BLOCK_FILTER, FLOW_CONTROL_FILTER = 1, 2, 3
# ioctls
GET_CONFIG, SET_CONFIG, READ_VBATT, FIVE_BAUD_INIT, FAST_INIT = 1, 2, 3, 4, 5
CLEAR_TX_BUFFER, CLEAR_RX_BUFFER, CLEAR_PERIODIC_MSGS, CLEAR_MSG_FILTERS = 7, 8, 9, 0x0A
# SCONFIG parameters
DATA_RATE, LOOPBACK, NODE_ADDRESS, NETWORK_LINE = 1, 3, 4, 5
P1_MIN, P1_MAX, P2_MIN, P2_MAX, P3_MIN, P3_MAX, P4_MIN, P4_MAX = 6, 7, 8, 9, 0x0A, 0x0B, 0x0C, 0x0D
# K-line init timing, numbered as in SAE J2534-1 v04.04: W1..W5 start at 0x0E
# and W0 (added in v04.04) comes after SYNC_JUMP_WIDTH at 0x19, not before W1.
W1, W2, W3, W4, W5 = 0x0E, 0x0F, 0x10, 0x11, 0x12
TIDLE, TINIL, TWUP, PARITY, BIT_SAMPLE_POINT, SYNC_JUMP_WIDTH, W0 = 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19
T1_MAX, T2_MAX, T4_MAX, T5_MAX = 0x1A, 0x1B, 0x1C, 0x1D
ISO15765_BS, ISO15765_STMIN, DATA_BITS, FIVE_BAUD_MOD = 0x1E, 0x1F, 0x20, 0x21
BS_TX, STMIN_TX, T3_MAX, ISO15765_WFT_MAX = 0x22, 0x23, 0x24, 0x25

STATUS_NOERROR = 0
ERR_BUFFER_EMPTY = 0x10
ERR_TIMEOUT = 0x09
ERR_NAMES = {
    0x01: "ERR_NOT_SUPPORTED", 0x02: "ERR_INVALID_CHANNEL_ID", 0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER", 0x05: "ERR_INVALID_IOCTL_VALUE", 0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED", 0x08: "ERR_DEVICE_NOT_CONNECTED", 0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG", 0x0B: "ERR_INVALID_TIME_INTERVAL", 0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID", 0x0E: "ERR_DEVICE_IN_USE", 0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY", 0x11: "ERR_BUFFER_FULL", 0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID", 0x14: "ERR_CHANNEL_IN_USE", 0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID", 0x17: "ERR_NO_FLOW_CONTROL", 0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE", 0x1A: "ERR_INVALID_DEVICE_ID",
}


class J2534Error(Exception):
    pass


class PASSTHRU_MSG(Structure):
    _fields_ = [("ProtocolID", c_uint32), ("RxStatus", c_uint32), ("TxFlags", c_uint32),
                ("Timestamp", c_uint32), ("DataSize", c_uint32), ("ExtraDataIndex", c_uint32),
                ("Data", c_ubyte * 4128)]        # NOT c_char: that truncates at NUL


class SCONFIG(Structure):
    _fields_ = [("Parameter", c_uint32), ("Value", c_uint32)]


class SCONFIG_LIST(Structure):
    _fields_ = [("NumOfParams", c_uint32), ("ConfigPtr", POINTER(SCONFIG))]


class SBYTE_ARRAY(Structure):
    _fields_ = [("NumOfBytes", c_uint32), ("BytePtr", POINTER(c_char))]


@dataclass
class Device:
    name: str
    vendor: str
    dll: str
    bits: int                  # 32 / 64, from the registry hive the entry sits in

    @property
    def loadable(self) -> bool:
        return self.bits == (64 if sys.maxsize > 2 ** 32 else 32)

    @property
    def label(self) -> str:
        return "%s (%s, %d-bit)" % (self.name, self.vendor, self.bits) if self.vendor \
            else "%s (%d-bit)" % (self.name, self.bits)


@dataclass
class Msg:
    protocol: int
    rx_status: int
    tx_flags: int
    timestamp: int             # device microseconds
    data: bytes

    @property
    def is_tx_echo(self) -> bool:
        return bool(self.rx_status & TX_MSG_TYPE)

    @property
    def can_id(self) -> Optional[int]:
        if self.protocol in (CAN, ISO15765) and len(self.data) >= 4:
            return int.from_bytes(self.data[:4], "big")
        return None

    @property
    def payload(self) -> bytes:
        return self.data[4:] if self.protocol in (CAN, ISO15765) else self.data


def list_devices() -> List[Device]:
    """Registered PassThru devices (Windows only; empty elsewhere)."""
    if os.name != "nt":
        return []
    import winreg
    out = []
    for hive_path, bits in (("SOFTWARE\\PassThruSupport.04.04", 64),
                            ("SOFTWARE\\WOW6432Node\\PassThruSupport.04.04", 32)):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                k = winreg.OpenKey(root, sub)
                dll = winreg.QueryValueEx(k, "FunctionLibrary")[0]
                try:
                    name = winreg.QueryValueEx(k, "Name")[0]
                except OSError:
                    name = sub
                try:
                    vendor = winreg.QueryValueEx(k, "Vendor")[0]
                except OSError:
                    vendor = ""
                out.append(Device(str(name), str(vendor), str(dll), bits))
            except OSError:
                continue
    # 64-bit Windows registers one product in both hives with the same DLL
    # path only when the vendor ships a single-bitness DLL; keep both rows,
    # the GUI greys out the unloadable one.
    return out


class Channel:
    def __init__(self, dev: "PassThru", channel_id: int, protocol: int):
        self.dev = dev
        self.id = channel_id
        self.protocol = protocol
        self._filters: List[int] = []

    # ---- filters ----
    def pass_all(self, extended: bool = False):
        """Receive everything (mask 0). For CAN the id field must still be
        4 bytes long; for K-line protocols one byte suffices."""
        n = 4 if self.protocol in (CAN, ISO15765) else 1
        flags = CAN_29BIT_ID if extended else 0
        self.add_filter(PASS_FILTER, bytes(n), bytes(n), flags)

    def add_filter(self, ftype: int, mask: bytes, pattern: bytes, flags: int = 0,
                   flow: Optional[bytes] = None) -> int:
        m, p = self.dev._msg(self.protocol, mask, flags), self.dev._msg(self.protocol, pattern, flags)
        f = self.dev._msg(self.protocol, flow, flags) if flow is not None else None
        fid = c_uint32(0)
        self.dev._check(self.dev.lib.PassThruStartMsgFilter(
            self.id, ftype, byref(m), byref(p), byref(f) if f is not None else None, byref(fid)),
            "PassThruStartMsgFilter")
        self._filters.append(fid.value)
        return fid.value

    def clear_filters(self):
        self.dev.ioctl(self.id, CLEAR_MSG_FILTERS)
        self._filters = []

    def pass_ids(self, ids, extended: bool = False):
        """Replace all filters so the device only buffers these CAN ids.

        A J2534 CAN channel with a pass-all filter hardware-buffers the whole
        bus.  On a live vehicle that is hundreds of broadcast frames/second, and
        during a flash write (auth, erase wait, between-block sends) the tester
        does not drain reads fast enough -- the device's RX buffer overflows
        (ERR_BUFFER_OVERFLOW) and drops frames.  Narrowing the hardware filter
        to just our TP 2.0 ids (channel + setup reply) is what real tools do; the
        flood never enters the buffer, so it cannot overflow."""
        ids = list(dict.fromkeys(int(i) for i in ids))
        self.clear_filters()
        flags = CAN_29BIT_ID if extended else 0
        mask = (0x1FFFFFFF if extended else 0x7FF).to_bytes(4, "big")
        for cid in ids:
            self.add_filter(PASS_FILTER, mask, cid.to_bytes(4, "big"), flags)

    # ---- messages ----
    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[Msg]:
        arr = (PASSTHRU_MSG * max_msgs)()
        n = c_uint32(max_msgs)
        rc = self.dev.lib.PassThruReadMsgs(self.id, arr, byref(n), timeout_ms)
        if rc not in (STATUS_NOERROR, ERR_BUFFER_EMPTY, ERR_TIMEOUT):
            self.dev._check(rc, "PassThruReadMsgs")
        out = []
        for i in range(n.value):
            m = arr[i]
            out.append(Msg(m.ProtocolID, m.RxStatus, m.TxFlags, m.Timestamp,
                           bytes(m.Data[:m.DataSize])))
        return out

    def write(self, data: bytes, flags: int = 0, timeout_ms: int = 100):
        m = self.dev._msg(self.protocol, data, flags)
        n = c_uint32(1)
        self.dev._check(self.dev.lib.PassThruWriteMsgs(self.id, byref(m), byref(n), timeout_ms),
                        "PassThruWriteMsgs")

    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        self.write(can_id.to_bytes(4, "big") + bytes(payload),
                  CAN_29BIT_ID if extended else 0, timeout_ms)

    def write_can_many(self, can_id: int, payloads, extended: bool = False, timeout_ms: int = 100):
        """Send several CAN frames (all to ``can_id``) in ONE PassThruWriteMsgs
        call. For an ISO-TP block transfer where the ECU's flow control allows a
        continuous stream (BS=0, STmin=0), batching amortizes the per-call DLL/USB
        latency that otherwise paces each consecutive frame -- the dominant cost
        of a 6HP28 write over the 32-bit bridge (CF gap was ~2.4 ms/frame, all
        per-call, since the TCU asked for 0 ms). Order is preserved."""
        payloads = list(payloads)
        if not payloads:
            return
        flags = CAN_29BIT_ID if extended else 0
        pid = can_id.to_bytes(4, "big")
        arr = (PASSTHRU_MSG * len(payloads))()
        for i, p in enumerate(payloads):
            data = pid + bytes(p)
            arr[i].ProtocolID = self.protocol
            arr[i].TxFlags = flags
            arr[i].DataSize = len(data)
            ctypes.memmove(arr[i].Data, data, len(data))
        n = c_uint32(len(payloads))
        self.dev._check(self.dev.lib.PassThruWriteMsgs(self.id, arr, byref(n),
                                                       max(timeout_ms, 20 * len(payloads))),
                        "PassThruWriteMsgs")

    # ---- config ----
    def set_config(self, **params):
        """set_config(DATA_RATE=10400, P3_MIN=...) using the SCONFIG names."""
        items = [(globals()[k], int(v)) for k, v in params.items()]
        arr = (SCONFIG * len(items))(*[SCONFIG(p, v) for p, v in items])
        lst = SCONFIG_LIST(len(items), ctypes.cast(arr, POINTER(SCONFIG)))
        self.dev.ioctl(self.id, SET_CONFIG, byref(lst))

    def set_baud(self, baud: int):
        self.set_config(DATA_RATE=baud)

    def set_pwm(self, items, ioctl_id=0x8008):
        """Scanmatik SM2 vendor PWM / signal-generator ioctl (the "GPT"
        crank/cam outputs).  ``items`` is a sequence of (param_id, value)
        ints -- param ids 0xC001..0xC00A (see flash.sm2_gpt).  Sent as a
        J2534 SCONFIG_LIST via the vendor ioctl (default 0x8008)."""
        items = [(int(p), int(v) & 0xFFFFFFFF) for p, v in items]
        arr = (SCONFIG * len(items))(*[SCONFIG(p, v) for p, v in items])
        lst = SCONFIG_LIST(len(items), ctypes.cast(arr, POINTER(SCONFIG)))
        self.dev.ioctl(self.id, ioctl_id, byref(lst))

    def clear_rx(self):
        self.dev.ioctl(self.id, CLEAR_RX_BUFFER)

    def clear_tx(self):
        self.dev.ioctl(self.id, CLEAR_TX_BUFFER)

    def five_baud_init(self, address: int) -> bytes:
        """ISO 9141/14230 slow init; returns the keyword bytes."""
        inp = create_string_buffer(bytes([address]), 1)
        out = create_string_buffer(4)
        sin = SBYTE_ARRAY(1, ctypes.cast(inp, POINTER(c_char)))
        sout = SBYTE_ARRAY(4, ctypes.cast(out, POINTER(c_char)))
        self.dev.ioctl(self.id, FIVE_BAUD_INIT, byref(sin), byref(sout))
        return bytes(out.raw[:sout.NumOfBytes])

    def fast_init(self, request: bytes) -> bytes:
        m = self.dev._msg(self.protocol, request, 0)
        resp = PASSTHRU_MSG()
        resp.ProtocolID = self.protocol
        self.dev.ioctl(self.id, FAST_INIT, byref(m), byref(resp))
        return bytes(resp.Data[:resp.DataSize])

    def disconnect(self):
        try:
            self.dev.lib.PassThruDisconnect(self.id)
        finally:
            self.id = None


class PassThru:
    """One opened device. Use as a context manager."""

    def __init__(self, dll_path: str):
        try:
            self.lib = ctypes.WinDLL(dll_path) if os.name == "nt" else ctypes.CDLL(dll_path)
        except OSError as ex:
            raise J2534Error("cannot load %s: %s (32/64-bit mismatch?)" % (dll_path, ex))
        L = self.lib
        L.PassThruOpen.argtypes = [c_void_p, POINTER(c_uint32)]
        L.PassThruClose.argtypes = [c_uint32]
        L.PassThruConnect.argtypes = [c_uint32, c_uint32, c_uint32, c_uint32, POINTER(c_uint32)]
        L.PassThruDisconnect.argtypes = [c_uint32]
        L.PassThruReadMsgs.argtypes = [c_uint32, POINTER(PASSTHRU_MSG), POINTER(c_uint32), c_uint32]
        L.PassThruWriteMsgs.argtypes = [c_uint32, POINTER(PASSTHRU_MSG), POINTER(c_uint32), c_uint32]
        L.PassThruStartMsgFilter.argtypes = [c_uint32, c_uint32, POINTER(PASSTHRU_MSG),
                                             POINTER(PASSTHRU_MSG), POINTER(PASSTHRU_MSG),
                                             POINTER(c_uint32)]
        L.PassThruIoctl.argtypes = [c_uint32, c_uint32, c_void_p, c_void_p]
        L.PassThruGetLastError.argtypes = [c_char_p]
        for fn in ("PassThruOpen", "PassThruClose", "PassThruConnect", "PassThruDisconnect",
                   "PassThruReadMsgs", "PassThruWriteMsgs", "PassThruStartMsgFilter",
                   "PassThruIoctl", "PassThruGetLastError"):
            getattr(L, fn).restype = c_uint32
        self.device_id = c_uint32(0)
        self._check(L.PassThruOpen(None, byref(self.device_id)), "PassThruOpen")
        self.channels: List[Channel] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _check(self, rc, what):
        if rc != STATUS_NOERROR:
            buf = create_string_buffer(80)
            try:
                self.lib.PassThruGetLastError(buf)
                detail = buf.value.decode("latin-1", "replace")
            except Exception:
                detail = ""
            raise J2534Error("%s: %s (0x%02X) %s" % (what, ERR_NAMES.get(rc, "error"), rc, detail))

    @staticmethod
    def _msg(protocol: int, data: Optional[bytes], flags: int = 0) -> PASSTHRU_MSG:
        m = PASSTHRU_MSG()
        m.ProtocolID = protocol
        m.TxFlags = flags
        data = bytes(data or b"")
        m.DataSize = len(data)
        ctypes.memmove(m.Data, data, len(data))
        return m

    def ioctl(self, channel_or_device_id: int, ioctl_id: int, inp=None, out=None):
        self._check(self.lib.PassThruIoctl(channel_or_device_id, ioctl_id, inp, out),
                    "PassThruIoctl 0x%02X" % ioctl_id)

    def connect(self, protocol: int, baud: int, flags: int = 0) -> Channel:
        cid = c_uint32(0)
        self._check(self.lib.PassThruConnect(self.device_id, protocol, flags, baud, byref(cid)),
                    "PassThruConnect(%s)" % PROTOCOL_NAMES.get(protocol, protocol))
        ch = Channel(self, cid.value, protocol)
        self.channels.append(ch)
        return ch

    def battery_voltage(self) -> Optional[float]:
        v = c_uint32(0)
        try:
            self.ioctl(self.device_id, READ_VBATT, None, byref(v))
        except J2534Error:
            return None
        return v.value / 1000.0

    def close(self):
        for ch in self.channels:
            if ch.id is not None:
                try:
                    ch.disconnect()
                except Exception:
                    pass
        self.channels = []
        if self.device_id is not None:
            try:
                self.lib.PassThruClose(self.device_id)
            except Exception:
                pass
            self.device_id = None


# ------------------------------------------------------------ 32-bit bridge
# A 64-bit Python cannot load a 32-bit DLL, and most 04.04 devices only ship
# one.  BridgePassThru runs j2534_bridge.py under a 32-bit interpreter (or the
# frozen j2534bridge32.exe beside the app) and proxies every call over pipes.
import json
import subprocess

_runner_cache: Optional[List[str]] = None


def _bridge_script() -> str:
    """The bridge script: beside this module in the source tree, or in the
    ``bridge`` folder the spec bundles."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "j2534_bridge.py")]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cands = [os.path.join(base, "bridge", "j2534_bridge.py"),
                 os.path.join(os.path.dirname(sys.executable), "bridge", "j2534_bridge.py")] + cands
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def _is_32bit_python(exe: List[str]) -> bool:
    try:
        out = subprocess.run(exe + ["-c", "import struct;print(struct.calcsize('P')*8)"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() == "32"
    except Exception:
        return False


def find_bridge_runner() -> Optional[List[str]]:
    """Command prefix that runs the bridge in a 32-bit process, or None.
    Order: frozen ``j2534bridge32.exe`` beside the app, the ``py -3-32``
    launcher, a ``python.exe`` in the usual 32-bit install folders."""
    global _runner_cache
    if _runner_cache is not None:
        return _runner_cache or None
    found: List[str] = []
    if os.name == "nt":
        exe_dirs = [os.path.dirname(sys.executable)]
        if getattr(sys, "frozen", False):
            exe_dirs.append(getattr(sys, "_MEIPASS", exe_dirs[0]))
        for d in exe_dirs:
            p = os.path.join(d, "j2534bridge32.exe")
            if os.path.exists(p):
                found = [p]
                break
        if not found and _is_32bit_python(["py", "-3-32"]):
            found = ["py", "-3-32", _bridge_script()]
        if not found:
            roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""), "C:\\"]
            for root in roots:
                if not root or not os.path.isdir(root):
                    continue
                for sub in ("Programs\\Python", ""):
                    base = os.path.join(root, sub) if sub else root
                    try:
                        names = sorted(os.listdir(base), reverse=True)
                    except OSError:
                        continue
                    for n in names:
                        if n.lower().startswith("python") and n.endswith("-32"):
                            p = os.path.join(base, n, "python.exe")
                            if os.path.exists(p) and _is_32bit_python([p]):
                                found = [p, _bridge_script()]
                                break
                    if found:
                        break
                if found:
                    break
    _runner_cache = found
    return found or None


def open_device(dev: Device) -> "PassThru":
    """Open a registered device in-process when the bitness matches, else
    through the 32-bit bridge."""
    if dev.loadable:
        return PassThru(dev.dll)
    if dev.bits == 32 and find_bridge_runner():
        return BridgePassThru(dev.dll)
    raise J2534Error("%s is a %d-bit DLL; this Python is %d-bit and no 32-bit bridge is available "
                     "(install a 32-bit Python so 'py -3-32' works, or build j2534bridge32.exe)"
                     % (dev.name, dev.bits, 64 if sys.maxsize > 2 ** 32 else 32))


def usable(dev: Device) -> bool:
    return dev.loadable or (dev.bits == 32 and find_bridge_runner() is not None)


class BridgeChannel:
    def __init__(self, dev: "BridgePassThru", channel_id: int, protocol: int):
        self.dev, self.id, self.protocol = dev, channel_id, protocol
        self._filters: List[int] = []

    def _q(self, op, **kw):
        return self.dev._call(op, cid=self.id, **kw)

    def pass_all(self, extended: bool = False):
        n = 4 if self.protocol in (CAN, ISO15765) else 1
        self.add_filter(PASS_FILTER, bytes(n), bytes(n), CAN_29BIT_ID if extended else 0)

    def add_filter(self, ftype: int, mask: bytes, pattern: bytes, flags: int = 0,
                   flow: Optional[bytes] = None) -> int:
        fid = self._q("add_filter", ftype=ftype, mask=bytes(mask).hex(), pattern=bytes(pattern).hex(),
                      flags=flags, flow=bytes(flow).hex() if flow is not None else None)
        self._filters.append(fid)
        return fid

    def clear_filters(self):
        self._q("clear_filters")
        self._filters = []

    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[Msg]:
        rows = self._q("read", max=max_msgs, timeout=timeout_ms)
        return [Msg(p, s, f, t, bytes.fromhex(d)) for p, s, f, t, d in rows]

    def write(self, data: bytes, flags: int = 0, timeout_ms: int = 100):
        self._q("write", data=bytes(data).hex(), flags=flags, timeout=timeout_ms)

    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        self.write(can_id.to_bytes(4, "big") + bytes(payload),
                  CAN_29BIT_ID if extended else 0, timeout_ms)

    def set_config(self, **params):
        self._q("set_config", params={k: int(v) for k, v in params.items()})

    def set_baud(self, baud: int):
        self.set_config(DATA_RATE=baud)

    def set_pwm(self, items, ioctl_id=0x8008):
        self._q("set_pwm", ioctl_id=int(ioctl_id),
                items=[[int(p), int(v) & 0xFFFFFFFF] for p, v in items])

    def clear_rx(self):
        self._q("clear_rx")

    def clear_tx(self):
        self._q("clear_tx")

    def five_baud_init(self, address: int) -> bytes:
        return bytes.fromhex(self._q("five_baud_init", address=address))

    def fast_init(self, request: bytes) -> bytes:
        return bytes.fromhex(self._q("fast_init", request=bytes(request).hex()))

    def disconnect(self):
        try:
            self._q("disconnect")
        finally:
            self.id = None


class BridgePassThru:
    """:class:`PassThru` look-alike backed by a 32-bit helper process."""

    def __init__(self, dll_path: str, runner: Optional[List[str]] = None, proc=None):
        self.dll_path = dll_path
        self.channels: List[BridgeChannel] = []
        self.label = "32-bit bridge"
        if proc is None:
            runner = runner or find_bridge_runner()
            if not runner:
                raise J2534Error("no 32-bit Python found to run the J2534 bridge")
            try:
                proc = subprocess.Popen(
                    runner + [dll_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as ex:
                raise J2534Error("cannot start the J2534 bridge (%s): %s" % (" ".join(runner), ex))
        self.proc = proc
        banner = self._recv()
        self.device_id = banner.get("device_id")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _recv(self):
        line = self.proc.stdout.readline()
        if not line:
            err = b""
            try:
                err = self.proc.stderr.read() or b""
            except Exception:
                pass
            self.proc = None
            raise J2534Error("J2534 bridge exited%s" % (
                (": " + err.decode("utf-8", "replace").strip()[-400:]) if err else ""))
        r = json.loads(line.decode("utf-8"))
        if not r.get("ok"):
            raise J2534Error(r.get("err") or "bridge error")
        return r.get("r")

    def _call(self, op, **kw):
        if self.proc is None:
            raise J2534Error("J2534 bridge is not running")
        kw["op"] = op
        try:
            self.proc.stdin.write((json.dumps(kw) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as ex:
            raise J2534Error("J2534 bridge write failed: %s" % ex)
        return self._recv()

    def connect(self, protocol: int, baud: int, flags: int = 0) -> BridgeChannel:
        cid = self._call("connect", protocol=protocol, baud=baud, flags=flags)
        ch = BridgeChannel(self, cid, protocol)
        self.channels.append(ch)
        return ch

    def open_tp20(self, dest, baud, listen_id=0x300, params=None, log_path=None) -> "BridgeTp20":
        """Run a whole VW TP 2.0 channel inside the bridge (one IPC per KWP
        message, tight ACK loop in-process with the DLL)."""
        return BridgeTp20(self, dest, baud, listen_id, params, log_path)

    def open_tp20_ecu(self, baud, ecu_rx=0x740, dest=None, a1_params=None,
                      protocol=CAN, timing=None, drain_write=True) -> "BridgeTp20Ecu":
        """Run the TRx (Aisin 09D) ECU-side TP 2.0 responder transport inside the
        bridge, so recv()/send() are one IPC per KWP message (the per-frame
        write + block-ACK loop runs next to the DLL). Raises J2534Error if this
        bridge build has no trx_sim (then the caller uses its own loop).

        ``timing`` is an optional bench "unclean bus" model (a trx_sim.TrxTiming
        or ecu_sim.SimTiming, anything with ``as_dict()``); it is serialized and
        rebuilt in the bridge so response latency / stalls / frame drops apply
        next to the DLL where the real timing lives.

        ``drain_write`` (default on) lets the bridge drain a transferData (0x36)
        write stream continuously -- ACKing each block in-bridge and batching the
        payloads back -- so a streamed write isn't desynced by the per-message
        IPC gap (a read is one request per block and never needs this)."""
        io = BridgeTp20Ecu(self, baud, ecu_rx=ecu_rx, dest=dest, a1_params=a1_params,
                           protocol=protocol, timing=timing, drain_write=drain_write)
        io.open()
        return io

    def open_iso_tp(self, baud, tx_id=0x7E1, rx_id=0x7E9, func_id=0x7DF,
                    protocol=CAN, log_path=None, log=None,
                    is_stop=None) -> "BridgeIsoTp":
        """Run the tester-side ISO-TP engine IN-BRIDGE over a RAW CAN channel:
        reassembly, flow control and responsePending all happen next to the DLL,
        so each UDS request is ONE IPC instead of ~35 per-frame read round-trips.
        This is what makes a 6HP28 calibration read over the 32-bit bridge fast
        and reliable; the returned handle presents the IsoTp surface
        Zf6hpUdsReader needs. Raises J2534Error if this bridge build has no
        isotp_transport."""
        r = self._call("iso_open", protocol=protocol, baud=baud, tx_id=tx_id,
                       rx_id=rx_id, func_id=func_id, log_path=log_path)
        return BridgeIsoTp(self, tx_id=r.get("tx_id", tx_id),
                           rx_id=r.get("rx_id", rx_id), func_id=func_id,
                           log=log, is_stop=is_stop)

    def battery_voltage(self) -> Optional[float]:
        try:
            return self._call("battery_voltage")
        except J2534Error:
            return None

    def ioctl(self, *a, **k):
        raise J2534Error("raw ioctls are not proxied through the bridge")

    def close(self):
        for ch in self.channels:
            if ch.id is not None:
                try:
                    ch.disconnect()
                except J2534Error:
                    pass
        self.channels = []
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.stdin.write(b'{"op": "close"}\n')
                p.stdin.flush()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


class _BridgeIsoShim:
    """Stand-in for ``IsoTp.can`` on the in-bridge path. The real bus flush
    happens in-bridge at the start of every request (``_iso_request``), so these
    are no-ops here -- that keeps ``Zf6hpUdsReader._flush_bus`` working without
    spending an extra IPC per chunk."""
    def clear_rx(self):
        pass

    def clear_tx(self):
        pass


class BridgeIsoTp:
    """64-bit handle to the tester-side ISO-TP engine the bridge runs in-process
    next to the DLL. Presents just enough of the :class:`IsoTp` surface for
    :class:`Zf6hpUdsReader` / :class:`Zf6hpUdsClient` -- ``request`` /
    ``functional_scan`` / ``pass_ids`` / ``_pending`` / ``.can`` (clear_rx /
    clear_tx) / ``tx_id`` / ``rx_id`` -- but each UDS request is ONE IPC: the
    bridge sends it, reassembles the response and rides out responsePending next
    to the DLL. ``Zf6hpUdsClient.request`` detects the native ``request`` method
    and delegates to it, so the whole read choreography runs unchanged, fast and
    without per-frame IPC."""

    def __init__(self, bridge: "BridgePassThru", tx_id=0x7E1, rx_id=0x7E9,
                 func_id=0x7DF, log=None, is_stop=None):
        self.b = bridge
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.func_id = func_id
        self._log = log or (lambda s: None)
        self.is_stop = is_stop
        self._pending = []                  # interface parity (the bridge buffers)
        self.can = _BridgeIsoShim()

    def _err(self, ex):
        from .transport import KLineError as KE, KLineTimeout as KT
        msg = str(ex)
        low = msg.lower()
        return KT(msg) if ("timed out" in low or "timeout" in low
                           or "no response" in low) else KE(msg)

    def pass_ids(self):
        return                              # the in-bridge IsoTp drops foreign ids itself

    def request(self, payload, timeout_ms=2000, p2_star_ms=5000, overall_ms=20000,
                fail_fast_on_silence=True, can_id=None):
        """One UDS request, reassembled + responsePending-ridden IN-BRIDGE (one
        IPC). Raises KLineTimeout on no answer so the caller re-sends as usual."""
        if self.is_stop is not None and self.is_stop():
            from .transport import KLineError as KE
            raise KE("aborted (stop requested)")
        try:
            r = self.b._call("iso_request", payload=bytes(payload).hex(),
                             can_id=can_id, timeout_ms=int(timeout_ms),
                             p2_star_ms=int(p2_star_ms), overall_ms=int(overall_ms),
                             fail_fast=bool(fail_fast_on_silence))
        except J2534Error as ex:
            raise self._err(ex)
        resp = r.get("resp")
        if resp is None:
            from .transport import KLineTimeout as KT
            raise KT("ISO-TP (in-bridge): no response within %d ms" % overall_ms)
        return bytes.fromhex(resp)

    def request_with_keepalive(self, payload, keepalive_interval_s=1.0,
                               resend_interval_s=3.5, poll_ms=500, overall_ms=60000):
        """A WRITE request whose ECU may go silent (block erase), ridden out
        IN-BRIDGE in one IPC. Raises KLineTimeout on overall timeout."""
        if self.is_stop is not None and self.is_stop():
            from .transport import KLineError as KE
            raise KE("aborted (stop requested)")
        try:
            r = self.b._call("iso_request_keepalive", payload=bytes(payload).hex(),
                             keepalive_interval_s=keepalive_interval_s,
                             resend_interval_s=resend_interval_s,
                             poll_ms=int(poll_ms), overall_ms=int(overall_ms))
        except J2534Error as ex:
            raise self._err(ex)
        resp = r.get("resp")
        if resp is None:
            from .transport import KLineTimeout as KT
            raise KT("ISO-TP (in-bridge): no response within %d ms (keepalive)" % overall_ms)
        return bytes.fromhex(resp)

    def functional_scan(self, request, listen_ms=500, func_id=None):
        try:
            r = self.b._call("iso_functional", request=bytes(request).hex(),
                             listen_ms=int(listen_ms),
                             func_id=int(func_id if func_id is not None else self.func_id))
        except J2534Error as ex:
            raise self._err(ex)
        return {int(k, 16): bytes.fromhex(v) for k, v in r.items()}

    def flush(self):
        try:
            self.b._call("iso_flush")
        except J2534Error:
            pass

    def close(self):
        try:
            self.b._call("iso_close")
        except J2534Error:
            pass


class BridgeTp20:
    """64-bit handle to a TP 2.0 channel the bridge runs in-process.  Presents
    the same surface as :class:`tp20_transport.Tp20Channel` so
    :class:`tp20_transport.CanKLine` can wrap it unchanged -- but the whole
    receive/ACK loop runs inside the 32-bit bridge, so it is one IPC per KWP
    message instead of one per CAN frame (avoids the ACK-latency retransmits)."""

    def __init__(self, bridge: BridgePassThru, dest, baud, listen_id=0x300, params=None, log_path=None):
        self.b = bridge
        self.dest = dest
        self.baud = baud
        self.listen_id = listen_id
        self.rx_id = listen_id
        self.tx_id = None
        self.ecu_block_size = 0x0F
        self.ecu_interframe_ms = 0.0
        self.opened = False
        self._params = params or {}
        self._log_path = log_path

    def _err(self, ex):
        from .transport import KLineError as KE, KLineTimeout as KT
        msg = str(ex)
        return KT(msg) if ("timed out" in msg or "timeout" in msg.lower()) else KE(msg)

    def open(self):
        try:
            r = self.b._call("tp_open", protocol=CAN, baud=self.baud, dest=self.dest,
                             listen=self.listen_id, params=self._params, log_path=self._log_path)
        except J2534Error as ex:
            raise self._err(ex)
        self.tx_id = r.get("tx_id")
        self.rx_id = r.get("rx_id")
        self.ecu_block_size = r.get("ecu_block_size", 0x0F)
        self.ecu_interframe_ms = r.get("ecu_interframe_ms", 0.0)
        self.opened = True

    def reopen(self):
        """Re-run the TP 2.0 channel setup on the EXISTING bridge CAN channel (no
        device reconnect) -- used after the programming session resets the ECU's
        transport (see CanKLine.reset_channel)."""
        try:
            r = self.b._call("tp_reopen")
        except J2534Error as ex:
            raise self._err(ex)
        self.tx_id = r.get("tx_id", self.tx_id)
        self.rx_id = r.get("rx_id", self.rx_id)
        self.ecu_block_size = r.get("ecu_block_size", self.ecu_block_size)
        self.ecu_interframe_ms = r.get("ecu_interframe_ms", self.ecu_interframe_ms)
        self.opened = True

    def send(self, payload: bytes):
        try:
            self.b._call("tp_send", payload=bytes(payload).hex())
        except J2534Error as ex:
            raise self._err(ex)

    def recv(self, timeout_ms=2000) -> bytes:
        try:
            return bytes.fromhex(self.b._call("tp_recv", timeout=int(timeout_ms)))
        except J2534Error as ex:
            raise self._err(ex)

    def keepalive(self):
        try:
            self.b._call("tp_keepalive")
        except J2534Error:
            pass

    def close(self):
        try:
            self.b._call("tp_close")
        except J2534Error:
            pass
        finally:
            self.opened = False


#: The seven bench-timing fields shared by trx_sim.TrxTiming and
#: ecu_sim.SimTiming. Serializing by name (not as_dict) lets EITHER model cross
#: the bridge IPC, since SimTiming has no as_dict.
_TIMING_FIELDS = ("cf_gap_ms", "resp_latency_ms", "jitter_ms", "drop_cf_prob",
                  "drop_resp_prob", "stall_every", "stall_ms")


def _timing_to_dict(timing):
    """Flatten a bench-timing model (TrxTiming / SimTiming / any duck-type, or a
    dict already) to a plain JSON-safe dict for the bridge IPC, or None. An
    inactive model collapses to None so the bridge takes the zero-overhead path."""
    if timing is None:
        return None
    if isinstance(timing, dict):
        d = {k: timing.get(k, 0) for k in _TIMING_FIELDS}
    else:
        d = {k: getattr(timing, k, 0) for k in _TIMING_FIELDS}
    # drop it entirely if nothing is set (all zero) -- an ideal bus
    if not any(d[k] for k in _TIMING_FIELDS):
        return None
    return d


class BridgeTp20Ecu:
    """64-bit handle to a TRx ECU-side TP 2.0 responder transport the bridge runs
    in-process. Presents recv()/send() like :class:`trx_sim.Tp20Ecu`, but the
    framing + block-ACK loop (and all per-frame DLL writes) run inside the 32-bit
    bridge, so it is one IPC per KWP message rather than ~40 per 254-byte read --
    which is what lets a full read dump finish before the tool times out. The
    responder (image serving, security) stays on the 64-bit side; only transport
    moves into the bridge."""

    def __init__(self, bridge: "BridgePassThru", baud, ecu_rx=0x740, dest=None,
                 a1_params=None, protocol=CAN, timing=None, drain_write=True):
        self.b = bridge
        self.baud = baud
        self.ecu_rx = ecu_rx
        self.dest = dest
        self.a1 = bytes(a1_params) if a1_params else None
        self.protocol = protocol
        self.drain_write = bool(drain_write)
        # serialize the bench-timing model once; it crosses the IPC as a plain
        # dict and is rebuilt (TrxTiming.from_dict) inside the bridge. Built by
        # field name so ANY duck-type works -- a trx_sim.TrxTiming (has as_dict)
        # or an ecu_sim.SimTiming (same fields, no as_dict): the GUI passes its
        # SimTiming straight through here, so this must not require as_dict.
        self.timing = _timing_to_dict(timing)
        self.frames_seen = 0
        self.opened = False
        # queue of (payload, bridge_acked) when the bridge returns a batched
        # transferData drain; drained blocks were ACKed in-bridge, so the next
        # send() for each must be swallowed (see send()).
        self._q: list = []
        self._skip_next_send = False

    def open(self):
        self.b._call("tp_ecu_open", protocol=self.protocol, baud=self.baud,
                     ecu_rx=self.ecu_rx, dest=self.dest,
                     a1=(self.a1.hex() if self.a1 else None),
                     timing=self.timing, drain_write=self.drain_write)
        self.opened = True

    def recv(self, timeout_ms=100):
        # drain any queued (already-received) batch blocks first
        if self._q:
            payload, acked = self._q.pop(0)
            self._skip_next_send = acked
            return payload
        r = self.b._call("tp_ecu_recv", timeout=int(timeout_ms))
        if not isinstance(r, dict):
            self._skip_next_send = False
            return None
        self.frames_seen = r.get("frames_seen", self.frames_seen)
        if "batch" in r:
            # a transferData stream the bridge drained + ACKed in-bridge: every
            # block still goes to the 64-bit responder for capture, but its 76 is
            # already on the wire, so mark it so send() doesn't double-ACK. The
            # trailing non-0x36 message (e.g. 0x37 exit) is handled normally.
            for h in (r.get("batch") or []):
                self._q.append((bytes.fromhex(h), True))
            if r.get("tail"):
                self._q.append((bytes.fromhex(r["tail"]), False))
            if not self._q:
                self._skip_next_send = False
                return None
            payload, acked = self._q.pop(0)
            self._skip_next_send = acked
            return payload
        self._skip_next_send = False
        p = r.get("payload")
        return bytes.fromhex(p) if p else None

    def send(self, payload: bytes, timeout_ms=2000):
        # a transferData block drained in-bridge was already ACKed there; the
        # responder still returns its 76, but sending it again would be a
        # duplicate frame, so swallow exactly that one.
        if self._skip_next_send:
            self._skip_next_send = False
            return
        self.b._call("tp_ecu_send", payload=bytes(payload).hex())

    def close(self):
        try:
            self.b._call("tp_ecu_close")
        except J2534Error:
            pass
        finally:
            self.opened = False
