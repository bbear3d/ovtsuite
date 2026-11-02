# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Generic OBD-II (SAE J1979 / ISO 15031-5): modes 01-0A, readiness, the
standard PIDs, emissions fault codes -- over 11-bit CAN (ISO 15765-4,
functional request 0x7DF, every emissions module answers on 0x7E8..0x7EF)
and over K-line (ISO 9141-2 with the 5-baud init at 0x33 and the
``68 6A F1`` header, or ISO 14230-4 KWP with fast or slow init and the
``C2 33 F1`` header; the key bytes decide which).

Nothing here is VAG-specific: the PID formulas, the DTC letter/number form
and the readiness bits are the public standard. The module answers are
kept per responder (CAN id or K-line source address) because a car has
several emissions modules (engine, gearbox) and they answer separately.

Qt-free; the window and the assistant tools drive :class:`ObdSession`.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..flash.transport import KLineTimeout

Log = Callable[[str], None]

FUNC_ID = 0x7DF
RESP_IDS = range(0x7E8, 0x7F0)
#: OBD functional address on K-line and the tester's own
OBD_ADDR = 0x33
TESTER = 0xF1
#: ISO 9141-2 key-byte pairs (08 08, 94 94) as the K-line facades report
#: them (``(kb2 & 0x7F) << 7 | (kb1 & 0x7F)``)
ISO9141_VERSIONS = (1032, 2580)


# ----------------------------------------------------------------- decoding
def _pct(a):
    return a * 100.0 / 255.0


def _trim(a):
    return (a - 128) * 100.0 / 128.0


def _temp(a):
    return a - 40


def _u16(d):
    return (d[0] << 8) | d[1]


@dataclass
class Pid:
    pid: int
    name: str
    size: int                               # data bytes in the answer
    unit: str = ""
    decode: Optional[Callable[[bytes], Any]] = None   # bytes -> value (number, text, dict)

    def value(self, data: bytes):
        data = bytes(data)
        if self.decode is None:
            return data.hex(" ").upper()
        try:
            return self.decode(data)
        except Exception:
            return data.hex(" ").upper()


FUEL_SYSTEM = {0: "off", 1: "open loop (cold)", 2: "closed loop", 4: "open loop (load / decel)",
               8: "open loop (fault)", 16: "closed loop with fault"}
OBD_STANDARD = {1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I", 5: "not OBD compliant",
                6: "EOBD", 7: "EOBD and OBD-II", 8: "EOBD and OBD", 9: "EOBD, OBD and OBD-II", 10: "JOBD",
                11: "JOBD and OBD-II", 12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II", 17: "EMD", 18: "EMD+",
                19: "HD OBD-C", 20: "HD OBD", 21: "WWH OBD", 23: "HD EOBD-I", 24: "HD EOBD-I N", 25: "HD EOBD-II",
                26: "HD EOBD-II N", 28: "OBDBr-1", 29: "OBDBr-2", 30: "KOBD", 31: "IOBD I", 32: "IOBD II",
                33: "HD EOBD-IV"}
FUEL_TYPE = {0: "not available", 1: "petrol", 2: "methanol", 3: "ethanol", 4: "diesel", 5: "LPG", 6: "CNG",
             7: "propane", 8: "electric", 9: "bifuel petrol", 10: "bifuel methanol", 11: "bifuel ethanol",
             12: "bifuel LPG", 13: "bifuel CNG", 14: "bifuel propane", 15: "bifuel electric",
             16: "bifuel electric/combustion", 17: "hybrid petrol", 18: "hybrid ethanol", 19: "hybrid diesel",
             20: "hybrid electric", 21: "hybrid electric/combustion", 22: "hybrid regenerative", 23: "bifuel diesel"}

SPARK_MONITORS = ("catalyst", "heated catalyst", "evaporative system", "secondary air", "A/C refrigerant",
                  "oxygen sensor", "oxygen sensor heater", "EGR / VVT")
COMPRESSION_MONITORS = ("NMHC catalyst", "NOx / SCR aftertreatment", "-", "boost pressure", "-",
                        "exhaust gas sensor", "PM filter", "EGR / VVT")
COMMON_MONITORS = ("misfire", "fuel system", "comprehensive components")


def decode_monitor_status(d: bytes) -> Dict[str, Any]:
    """PID 01 (and 41): MIL, stored DTC count, which monitors the module
    has and which have completed since the codes were cleared."""
    a, b, c, dd = d[0], d[1], d[2], d[3]
    compression = bool(b & 0x08)
    out: Dict[str, Any] = {"mil": bool(a & 0x80), "dtc_count": a & 0x7F,
                           "ignition": "compression" if compression else "spark", "monitors": []}
    for i, name in enumerate(COMMON_MONITORS):
        if b & (1 << i):
            out["monitors"].append({"name": name, "complete": not (b & (1 << (i + 4)))})
    names = COMPRESSION_MONITORS if compression else SPARK_MONITORS
    for i, name in enumerate(names):
        if name != "-" and c & (1 << i):
            out["monitors"].append({"name": name, "complete": not (dd & (1 << i))})
    return out


def _o2_2(d):
    return {"voltage_v": d[0] / 200.0, "stft_pct": None if d[1] == 0xFF else _trim(d[1])}


def _o2_wide_v(d):
    return {"lambda": _u16(d[0:2]) / 32768.0, "voltage_v": _u16(d[2:4]) / 8192.0}


def _o2_wide_i(d):
    return {"lambda": _u16(d[0:2]) / 32768.0, "current_ma": _u16(d[2:4]) / 256.0 - 128}


def _bits(d):
    return " ".join(format(x, "08b") for x in d)


PIDS: Dict[int, Pid] = {}


def _p(pid, name, size, unit="", decode=None):
    PIDS[pid] = Pid(pid, name, size, unit, decode)


for _base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
    _p(_base, "PIDs supported %02X-%02X" % (_base + 1, _base + 0x20), 4, "", _bits)
_p(0x01, "Monitor status since DTCs cleared", 4, "", decode_monitor_status)
_p(0x02, "Freeze frame DTC", 2, "", lambda d: decode_dtc(d) if any(d) else "none")
_p(0x03, "Fuel system status", 2, "", lambda d: ", ".join(FUEL_SYSTEM.get(x, "0x%02X" % x) for x in d if x))
_p(0x04, "Calculated engine load", 1, "%", lambda d: _pct(d[0]))
_p(0x05, "Engine coolant temperature", 1, "°C", lambda d: _temp(d[0]))
_p(0x06, "Short term fuel trim, bank 1", 1, "%", lambda d: _trim(d[0]))
_p(0x07, "Long term fuel trim, bank 1", 1, "%", lambda d: _trim(d[0]))
_p(0x08, "Short term fuel trim, bank 2", 1, "%", lambda d: _trim(d[0]))
_p(0x09, "Long term fuel trim, bank 2", 1, "%", lambda d: _trim(d[0]))
_p(0x0A, "Fuel pressure (gauge)", 1, "kPa", lambda d: 3 * d[0])
_p(0x0B, "Intake manifold absolute pressure", 1, "kPa", lambda d: d[0])
_p(0x0C, "Engine speed", 2, "rpm", lambda d: _u16(d) / 4.0)
_p(0x0D, "Vehicle speed", 1, "km/h", lambda d: d[0])
_p(0x0E, "Timing advance", 1, "° BTDC", lambda d: d[0] / 2.0 - 64)
_p(0x0F, "Intake air temperature", 1, "°C", lambda d: _temp(d[0]))
_p(0x10, "Mass air flow", 2, "g/s", lambda d: _u16(d) / 100.0)
_p(0x11, "Throttle position", 1, "%", lambda d: _pct(d[0]))
_p(0x12, "Commanded secondary air status", 1, "", lambda d: {1: "upstream", 2: "downstream of catalyst",
                                                              4: "from the outside / off", 8: "pump commanded on"
                                                              }.get(d[0], "0x%02X" % d[0]))
_p(0x13, "Oxygen sensors present (2 banks)", 1, "", _bits)
for _i in range(8):
    _p(0x14 + _i, "Oxygen sensor %d (bank %d sensor %d)" % (_i + 1, _i // 4 + 1, _i % 4 + 1), 2, "", _o2_2)
_p(0x1C, "OBD standard", 1, "", lambda d: OBD_STANDARD.get(d[0], "0x%02X" % d[0]))
_p(0x1D, "Oxygen sensors present (4 banks)", 1, "", _bits)
_p(0x1E, "Auxiliary input status (PTO)", 1, "", lambda d: "active" if d[0] & 1 else "inactive")
_p(0x1F, "Run time since engine start", 2, "s", _u16)
_p(0x21, "Distance travelled with MIL on", 2, "km", _u16)
_p(0x22, "Fuel rail pressure (relative to manifold vacuum)", 2, "kPa", lambda d: _u16(d) * 0.079)
_p(0x23, "Fuel rail gauge pressure", 2, "kPa", lambda d: _u16(d) * 10)
for _i in range(8):
    _p(0x24 + _i, "Oxygen sensor %d (wide range, voltage)" % (_i + 1), 4, "", _o2_wide_v)
_p(0x2C, "Commanded EGR", 1, "%", lambda d: _pct(d[0]))
_p(0x2D, "EGR error", 1, "%", lambda d: _trim(d[0]))
_p(0x2E, "Commanded evaporative purge", 1, "%", lambda d: _pct(d[0]))
_p(0x2F, "Fuel tank level", 1, "%", lambda d: _pct(d[0]))
_p(0x30, "Warm-ups since codes cleared", 1, "", lambda d: d[0])
_p(0x31, "Distance since codes cleared", 2, "km", _u16)
_p(0x32, "Evaporative system vapour pressure", 2, "Pa",
   lambda d: (_u16(d) - 65536 if _u16(d) & 0x8000 else _u16(d)) / 4.0)
_p(0x33, "Barometric pressure", 1, "kPa", lambda d: d[0])
for _i in range(8):
    _p(0x34 + _i, "Oxygen sensor %d (wide range, current)" % (_i + 1), 4, "", _o2_wide_i)
for _i, _lab in enumerate(("bank 1 sensor 1", "bank 2 sensor 1", "bank 1 sensor 2", "bank 2 sensor 2")):
    _p(0x3C + _i, "Catalyst temperature, %s" % _lab, 2, "°C", lambda d: _u16(d) / 10.0 - 40)
_p(0x41, "Monitor status this drive cycle", 4, "", decode_monitor_status)
_p(0x42, "Control module voltage", 2, "V", lambda d: _u16(d) / 1000.0)
_p(0x43, "Absolute load value", 2, "%", lambda d: _u16(d) * 100.0 / 255.0)
_p(0x44, "Commanded equivalence ratio", 2, "λ", lambda d: _u16(d) / 32768.0)
_p(0x45, "Relative throttle position", 1, "%", lambda d: _pct(d[0]))
_p(0x46, "Ambient air temperature", 1, "°C", lambda d: _temp(d[0]))
_p(0x47, "Absolute throttle position B", 1, "%", lambda d: _pct(d[0]))
_p(0x48, "Absolute throttle position C", 1, "%", lambda d: _pct(d[0]))
_p(0x49, "Accelerator pedal position D", 1, "%", lambda d: _pct(d[0]))
_p(0x4A, "Accelerator pedal position E", 1, "%", lambda d: _pct(d[0]))
_p(0x4B, "Accelerator pedal position F", 1, "%", lambda d: _pct(d[0]))
_p(0x4C, "Commanded throttle actuator", 1, "%", lambda d: _pct(d[0]))
_p(0x4D, "Time run with MIL on", 2, "min", _u16)
_p(0x4E, "Time since codes cleared", 2, "min", _u16)
_p(0x4F, "Maximum values (λ, O2 V, O2 mA, MAP)", 4, "", lambda d: {"lambda": d[0], "o2_v": d[1], "o2_ma": d[2], "map_kpa": d[3] * 10})
_p(0x50, "Maximum MAF", 4, "g/s", lambda d: d[0] * 10)
_p(0x51, "Fuel type", 1, "", lambda d: FUEL_TYPE.get(d[0], "0x%02X" % d[0]))
_p(0x52, "Ethanol fuel", 1, "%", lambda d: _pct(d[0]))
_p(0x53, "Absolute evaporative system vapour pressure", 2, "kPa", lambda d: _u16(d) / 200.0)
_p(0x5A, "Relative accelerator pedal position", 1, "%", lambda d: _pct(d[0]))
_p(0x5B, "Hybrid battery pack remaining life", 1, "%", lambda d: _pct(d[0]))
_p(0x5C, "Engine oil temperature", 1, "°C", lambda d: _temp(d[0]))
_p(0x5D, "Fuel injection timing", 2, "°", lambda d: _u16(d) / 128.0 - 210)
_p(0x5E, "Engine fuel rate", 2, "l/h", lambda d: _u16(d) / 20.0)
_p(0x5F, "Emission requirements", 1, "", lambda d: "0x%02X" % d[0])
_p(0x61, "Driver's demand engine torque", 1, "%", lambda d: d[0] - 125)
_p(0x62, "Actual engine torque", 1, "%", lambda d: d[0] - 125)
_p(0x63, "Engine reference torque", 2, "Nm", _u16)
_p(0x64, "Engine percent torque data", 5, "%", lambda d: {"idle": d[0] - 125, "point1": d[1] - 125, "point2": d[2] - 125,
                                                            "point3": d[3] - 125, "point4": d[4] - 125})
_p(0x66, "Mass air flow sensor", 5, "g/s", lambda d: {"sensor_a": _u16(d[1:3]) / 32.0, "sensor_b": _u16(d[3:5]) / 32.0})
_p(0x67, "Engine coolant temperature (2 sensors)", 3, "°C", lambda d: {"sensor_1": _temp(d[1]), "sensor_2": _temp(d[2])})
_p(0x68, "Intake air temperature (2 sensors)", 3, "°C", lambda d: {"sensor_1": _temp(d[1]), "sensor_2": _temp(d[2])})
_p(0x6B, "Exhaust gas recirculation temperature", 5, "°C", lambda d: d.hex(" ").upper())
_p(0x7F, "Engine run time", 13, "s", lambda d: {"total": int.from_bytes(d[1:5], "big"),
                                                 "idle": int.from_bytes(d[5:9], "big"),
                                                 "pto": int.from_bytes(d[9:13], "big")})
_p(0xA6, "Odometer", 4, "km", lambda d: int.from_bytes(d, "big") / 10.0)

#: mode 09 vehicle information
INFO_TYPES = {0x00: ("Info types supported", "bits"), 0x02: ("VIN", "ascii"), 0x04: ("Calibration ID", "ascii16"),
              0x06: ("Calibration verification numbers", "hex4"), 0x08: ("In-use performance tracking (spark)", "raw"),
              0x0A: ("ECU name", "ascii"), 0x0B: ("In-use performance tracking (compression)", "raw")}

DTC_LETTERS = "PCBU"


def decode_dtc(two: bytes) -> str:
    """Two bytes -> ``P0299`` (letter from the top two bits, then the
    hex digits)."""
    a, b = two[0], two[1]
    return "%s%d%X%02X" % (DTC_LETTERS[a >> 6], (a >> 4) & 0x3, a & 0x0F, b)


def encode_dtc(code: str) -> bytes:
    code = code.strip().upper()
    a = (DTC_LETTERS.index(code[0]) << 6) | ((int(code[1]) & 0x3) << 4) | int(code[2], 16)
    return bytes([a, int(code[3:5], 16)])


def supported_pids(mode: int, bitmaps: Dict[int, bytes]) -> List[int]:
    """The PIDs a module says it supports, from its ``00 / 20 / 40 ...``
    bitmap answers (each bit, MSB first, is the next PID after the base)."""
    out = []
    for base, data in sorted(bitmaps.items()):
        bits = int.from_bytes(bytes(data[:4]).ljust(4, b"\0"), "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                out.append(base + 1 + i)
    return out


# ------------------------------------------------------------------ results
@dataclass
class ObdReply:
    """One module's answer to one request."""
    module: int                      # CAN response id or K-line source address
    mode: int
    pid: Optional[int]
    data: bytes                      # after the mode (+pid) echo
    raw: bytes = b""

    @property
    def module_label(self) -> str:
        if self.module >= 0x100:
            n = self.module - 0x7E8
            return "CAN 0x%03X%s" % (self.module, " (engine)" if n == 0 else " (gearbox)" if n == 1 else "")
        return "K-line 0x%02X" % self.module


@dataclass
class Dtc:
    code: str                        # "P0299"
    module: int
    kind: str                        # stored | pending | permanent
    raw: bytes = b""


@dataclass
class ObdSnapshot:
    protocol: str = ""
    modules: List[int] = field(default_factory=list)
    status: Dict[int, Dict[str, Any]] = field(default_factory=dict)      # module -> decoded PID 01
    pids: Dict[int, Dict[int, Any]] = field(default_factory=dict)        # module -> {pid: value}
    dtcs: List[Dtc] = field(default_factory=list)
    vin: str = ""
    info: Dict[int, Dict[str, Any]] = field(default_factory=dict)        # module -> {type: value}
    when: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"protocol": self.protocol, "modules": list(self.modules), "when": self.when,
                "status": {str(k): v for k, v in self.status.items()},
                "pids": {str(k): {"%02X" % p: v for p, v in d.items()} for k, d in self.pids.items()},
                "dtcs": [{"code": d.code, "module": d.module, "kind": d.kind} for d in self.dtcs],
                "vin": self.vin, "info": {str(k): v for k, v in self.info.items()}}


# ------------------------------------------------------------- transports
class ObdTransport:
    """``request(mode, pid, extra) -> [ObdReply]`` over something."""
    protocol = ""

    def connect(self):
        return self.protocol

    def request(self, mode: int, pid: Optional[int] = None, extra: bytes = b"",
                listen_ms: int = 300) -> List[ObdReply]:
        raise NotImplementedError

    def close(self):
        pass


class ObdCan(ObdTransport):
    """ISO 15765-4: a functional single-frame request to 0x7DF, every
    emissions module's answer collected (the suite's ISO-TP engine does the
    multi-frame reassembly and the per-responder flow control)."""
    protocol = "ISO 15765-4 (CAN 11-bit, 500 kbit/s)"

    def __init__(self, can, log: Optional[Log] = None, is_stop=None):
        from ..flash.isotp_transport import IsoTp
        self.can = can
        self.tp = IsoTp(can, tx_id=FUNC_ID, rx_id=0x7E8, log=log, is_stop=is_stop)
        self._log = log or (lambda s: None)
        self.is_stop = is_stop
        #: modules seen so far; a request stops listening once they all answered
        self.known: set = set()

    def connect(self):
        # Only the emissions modules' answers (0x7E8..0x7EF): a pass-all
        # J2534 channel hardware-buffers the whole bus and overflows while the
        # tester is not reading, which is the normal state between polls.
        pi = getattr(self.can, "pass_ids", None)
        if pi is not None:
            try:
                pi(list(RESP_IDS))
                return self.protocol
            except Exception:
                pass
        pa = getattr(self.can, "pass_all", None)
        if pa is not None:
            try:
                pa()
            except Exception:
                pass
        return self.protocol

    def _collect(self, req: bytes, listen_ms: int) -> Dict[int, bytes]:
        """A functional request and every answer on 0x7E8..0x7EF -- the
        ISO-TP engine's scan loop with an early exit once every module
        heard from before has answered (a PID then costs one P2, not the
        whole listen window)."""
        from ..flash import isotp_transport as _it
        tp = self.tp
        clear = getattr(self.can, "clear_rx", None)
        if clear is not None:
            try:
                clear()
            except Exception:
                pass
        tp._pending = []
        tp._write_frame(bytes([len(req)]) + req, can_id=FUNC_ID)
        partial: Dict[int, list] = {}
        done: Dict[int, bytes] = {}
        deadline = time.monotonic() + listen_ms / 1000.0
        while time.monotonic() < deadline:
            if self.is_stop is not None and self.is_stop():
                break
            if self.known and self.known <= set(done) and not partial:
                break
            got = self.can.read(16, 0)
            if not got:
                _it._precise_wait(0.0005)
                continue
            for m in got:
                cid, d = m.can_id, bytes(m.payload)
                if not d or cid not in RESP_IDS:
                    continue
                pci = d[0] >> 4
                if pci == 0:
                    done.setdefault(cid, bytes(d[1:1 + (d[0] & 0x0F)]))
                elif pci == 1:
                    n = ((d[0] & 0x0F) << 8) | d[1]
                    partial[cid] = [n, bytearray(d[2:])]
                    tp._write_frame(bytes([0x30, tp.tx_block_size, tp.tx_st_min_ms]), can_id=cid - 8)
                    deadline = max(deadline, time.monotonic() + 1.0)
                elif pci == 2 and cid in partial:
                    exp, buf = partial[cid]
                    buf += d[1:]
                    if len(buf) >= exp:
                        done.setdefault(cid, bytes(buf[:exp]))
                        del partial[cid]
        return done

    def request(self, mode, pid=None, extra=b"", listen_ms=300):
        req = bytes([mode]) + (bytes([pid]) if pid is not None else b"") + bytes(extra)
        got = self._collect(req, listen_ms)
        self.known |= set(got)
        out = []
        for cid, data in sorted(got.items()):
            if cid not in RESP_IDS or not data:
                continue
            if data[0] == 0x7F:
                nrc = data[2] if len(data) > 2 else 0
                if nrc not in (0x12, 0x31):          # "not supported" answers are the normal way to probe
                    self._log("OBD 0x%03X: mode %02X refused (NRC 0x%02X)" % (cid, mode, nrc))
                continue
            if data[0] != 0x40 + mode:
                continue
            body = data[1:]
            if pid is not None:
                if not body or body[0] != pid:
                    continue
                body = body[1:]
            out.append(ObdReply(cid, mode, pid, bytes(body), bytes(data)))
        return out


class ObdKLine(ObdTransport):
    """ISO 9141-2 / ISO 14230-4 over the suite's K-line facade: the 5-baud
    (or fast) init at the OBD functional address 0x33, then ``68 6A F1``
    messages (9141-2, lengths from the PID table) or ``C2 33 F1`` messages
    (14230, length in the format byte). Every module that answers is
    returned, told apart by its source address."""

    def __init__(self, k, log: Optional[Log] = None, fast_init: bool = False):
        self.k = k
        self._log = log or (lambda s: None)
        self.fast_init = fast_init
        self.version = 0
        self.protocol = ""

    def connect(self):
        try:
            self.k.set_baud(10400)
        except Exception:
            pass
        if self.fast_init and hasattr(self.k, "prefer_fast_init"):
            self.k.prefer_fast_init = True
        # ISO 9141-2 / 14230-4 send the plain 0x33 pattern. The facade's
        # wake_up applies the KW1281 convention (7 bits + odd parity), which
        # would turn 0x33 (four ones) into 0xB3; asking for even parity
        # leaves 0x33 as it is.
        self.version = int(self.k.wake_up(OBD_ADDR, even_parity=True))
        if self.version in ISO9141_VERSIONS:
            self.protocol = "ISO 9141-2 (K-line, key bytes %s)" % ("08 08" if self.version == 1032 else "94 94")
        else:
            self.protocol = "ISO 14230-4 KWP (K-line)"
        time.sleep(0.06)                        # W4 before the first request
        try:
            self.k.clear_rx()
        except Exception:
            pass
        self._log("OBD-II: %s" % self.protocol)
        return self.protocol

    @property
    def iso9141(self) -> bool:
        return self.version in ISO9141_VERSIONS

    def _frame(self, payload: bytes) -> bytes:
        if self.iso9141:
            out = bytearray([0x68, 0x6A, TESTER]) + payload
        else:
            out = bytearray([0xC0 | len(payload), OBD_ADDR, TESTER]) + payload
        out.append(sum(out) & 0xFF)
        return bytes(out)

    def _read_message(self, expect_len: Optional[int]) -> Optional[Tuple[int, bytes]]:
        """One response message -> (source, payload) or None on timeout (a
        message cut short by the line going quiet counts as none)."""
        try:
            h = self.k.read_byte()
        except KLineTimeout:
            return None
        try:
            return self._read_message_body(h, expect_len)
        except KLineTimeout:
            return None

    def _read_message_body(self, h: int, expect_len: Optional[int]) -> Optional[Tuple[int, bytes]]:
        if self.iso9141:
            self.k.read_byte()                   # 6B (tester)
            src = self.k.read_byte()
            n = expect_len if expect_len is not None else 0
            body = bytearray()
            if n:
                body += self.k.read(n)
            else:                                # unknown length: read until the line goes quiet
                saved = self.k.read_timeout
                self.k.read_timeout = 0.03
                try:
                    while True:
                        try:
                            body.append(self.k.read_byte())
                        except KLineTimeout:
                            break
                finally:
                    self.k.read_timeout = saved
                if body:
                    return src, bytes(body[:-1])     # the last byte was the checksum
                return src, b""
            self.k.read_byte()                   # checksum
            return src, bytes(body)
        fmt = h
        src = 0
        if fmt & 0x80:
            self.k.read_byte()                   # target (the tester)
            src = self.k.read_byte()
        n = fmt & 0x3F
        if n == 0:
            n = self.k.read_byte()
        body = self.k.read(n) if n else b""
        self.k.read_byte()                       # checksum
        return src, bytes(body)

    def request(self, mode, pid=None, extra=b"", listen_ms=300):
        payload = bytes([mode]) + (bytes([pid]) if pid is not None else b"") + bytes(extra)
        expect = None
        if self.iso9141:
            if mode in (0x01, 0x02) and pid is not None and pid in PIDS:
                expect = 2 + PIDS[pid].size + (1 if mode == 0x02 else 0)
            elif mode in (0x03, 0x07, 0x0A):
                expect = 7
            elif mode == 0x04:
                expect = 1
            elif mode == 0x09:
                # 49 <type> <sequence> + 4 data bytes per message: reading
                # by length keeps the next chunk's header out of this body
                expect = 7
        self.k.clear_rx()
        self.k.write_bytes(self._frame(payload))
        out = []
        saved = self.k.read_timeout
        self.k.read_timeout = max(0.2, listen_ms / 1000.0)
        try:
            while True:
                msg = self._read_message(expect)
                if msg is None:
                    break
                src, body = msg
                self.k.read_timeout = 0.1        # further modules answer within P2
                if not body:
                    continue
                if body[0] == 0x7F:
                    nrc = body[2] if len(body) > 2 else 0
                    if nrc not in (0x12, 0x31):
                        self._log("OBD 0x%02X: mode %02X refused (NRC 0x%02X)" % (src, mode, nrc))
                    continue
                if body[0] != 0x40 + mode:
                    continue
                data = body[1:]
                if pid is not None:
                    if not data or data[0] != pid:
                        continue
                    data = data[1:]
                out.append(ObdReply(src, mode, pid, bytes(data), bytes(body)))
        finally:
            self.k.read_timeout = saved
        return out

    def close(self):
        try:
            if not self.iso9141:
                self.k.write_bytes(self._frame(b"\x82"))     # stopCommunication
        except Exception:
            pass


# -------------------------------------------------------------- the session
class ObdSession:
    """Modes 01-0A on top of a transport; results decoded with the PID
    table and kept per module."""

    def __init__(self, transport: ObdTransport, log: Optional[Log] = None):
        self.t = transport
        self._log = log or (lambda s: None)
        self.protocol = ""
        self._supported: Dict[Tuple[int, int], List[int]] = {}

    def connect(self) -> str:
        self.protocol = self.t.connect() or self.t.protocol
        return self.protocol

    def close(self):
        self.t.close()

    # ---- supported PIDs
    def supported(self, mode: int = 0x01) -> Dict[int, List[int]]:
        """``{module: [pid, ...]}`` from the bitmap PIDs, following the
        chain while a module says the next bitmap exists."""
        bitmaps: Dict[int, Dict[int, bytes]] = {}
        base = 0x00
        while base <= 0xC0:
            replies = self.t.request(mode, base)
            if not replies:
                break
            more = False
            for r in replies:
                bitmaps.setdefault(r.module, {})[base] = r.data[:4]
                if len(r.data) >= 4 and r.data[3] & 0x01:
                    more = True
            if not more:
                break
            base += 0x20
        out = {m: supported_pids(mode, b) for m, b in bitmaps.items()}
        for m, pids in out.items():
            self._supported[(m, mode)] = pids
        return out

    # ---- live PIDs
    def read_pid(self, pid: int, mode: int = 0x01) -> List[ObdReply]:
        return self.t.request(mode, pid)

    def read_pids(self, pids: List[int], mode: int = 0x01) -> Dict[int, Dict[int, Any]]:
        """``{module: {pid: decoded value}}``."""
        out: Dict[int, Dict[int, Any]] = {}
        for pid in pids:
            for r in self.t.request(mode, pid):
                p = PIDS.get(pid)
                out.setdefault(r.module, {})[pid] = p.value(r.data) if p else r.data.hex(" ").upper()
        return out

    def status(self) -> Dict[int, Dict[str, Any]]:
        """PID 01 per module: MIL, DTC count, readiness monitors."""
        out = {}
        for r in self.t.request(0x01, 0x01):
            if len(r.data) >= 4:
                out[r.module] = decode_monitor_status(r.data)
        return out

    # ---- fault codes
    def dtcs(self, kinds=("stored", "pending", "permanent")) -> List[Dtc]:
        modes = {"stored": 0x03, "pending": 0x07, "permanent": 0x0A}
        out: List[Dtc] = []
        for kind in kinds:
            mode = modes[kind]
            for r in self.t.request(mode, None, listen_ms=500):
                data = r.data
                # ISO 15765-4 answers carry a count byte first; K-line answers do not
                if r.module >= 0x100 and data:
                    n, data = data[0], data[1:]
                    data = data[:n * 2]
                for i in range(0, len(data) - 1, 2):
                    two = data[i:i + 2]
                    if two == b"\x00\x00":
                        continue
                    out.append(Dtc(decode_dtc(two), r.module, kind, bytes(two)))
        return out

    def clear(self) -> List[int]:
        """Mode 04: clear emissions codes and freeze frames; the modules that
        confirmed."""
        return [r.module for r in self.t.request(0x04, None, listen_ms=1500)]

    def freeze_frame(self, frame: int = 0) -> Dict[int, Dict[int, Any]]:
        """Mode 02: the freeze frame's DTC and the PIDs it stores."""
        out: Dict[int, Dict[int, Any]] = {}
        sup = {}
        for base in (0x00, 0x20, 0x40):
            replies = self.t.request(0x02, base, bytes([frame]))
            if not replies:
                break
            for r in replies:
                sup.setdefault(r.module, {})[base] = r.data[1:5] if len(r.data) >= 5 else r.data
            if not any(len(r.data) >= 5 and r.data[4] & 1 for r in replies):
                break
        pids_by_module = {m: supported_pids(0x02, b) for m, b in sup.items()}
        wanted = sorted({p for ps in pids_by_module.values() for p in ps} | {0x02})
        for pid in wanted:
            for r in self.t.request(0x02, pid, bytes([frame])):
                data = r.data[1:] if r.data else b""     # frame number echo first
                p = PIDS.get(pid)
                out.setdefault(r.module, {})[pid] = p.value(data) if p else data.hex(" ").upper()
        return out

    # ---- vehicle information
    def info(self) -> Dict[int, Dict[str, Any]]:
        """Mode 09: VIN, calibration ids, CVNs, ECU name -- per module."""
        out: Dict[int, Dict[str, Any]] = {}
        for itype, (name, form) in INFO_TYPES.items():
            if itype == 0x00:
                continue
            for module, replies in self._by_module(self.t.request(0x09, itype, listen_ms=600)).items():
                out.setdefault(module, {})[name] = self._info_value(replies, form)
        return out

    def vin(self) -> str:
        for replies in self._by_module(self.t.request(0x09, 0x02, listen_ms=600)).values():
            v = self._info_value(replies, "ascii")
            if v:
                return v
        return ""

    @staticmethod
    def _by_module(replies) -> Dict[int, List[ObdReply]]:
        out: Dict[int, List[ObdReply]] = {}
        for r in replies:
            out.setdefault(r.module, []).append(r)
        return out

    @staticmethod
    def _info_value(replies, form: str):
        """The item's bytes from a module's mode 09 answer. Over CAN that is
        one message (count byte first); over K-line one message per 4-byte
        chunk, each after a sequence byte, in order -- joined here, so a
        17-character VIN is five messages, not the last four characters."""
        if isinstance(replies, ObdReply):
            replies = [replies]
        if not replies:
            return "" if form == "ascii" else []
        module = replies[0].module
        if module >= 0x100:
            data = bytes(replies[-1].data)
            if data:
                data = data[1:]                   # the message count byte
        else:
            chunks = []
            for r in replies:
                d = bytes(r.data)
                if form in ("ascii", "ascii16", "hex4") and d:
                    chunks.append((d[0], d[1:]))  # (sequence byte, 4 data bytes)
                else:
                    chunks.append((len(chunks), d))
            chunks.sort(key=lambda c: c[0])
            data = b"".join(c[1] for c in chunks)
        if form == "ascii":
            return "".join(chr(c) for c in data if 0x20 <= c < 0x7F).strip()
        if form == "ascii16":
            return [data[i:i + 16].decode("ascii", "replace").strip("\0 ") for i in range(0, len(data), 16)]
        if form == "hex4":
            return [data[i:i + 4].hex().upper() for i in range(0, len(data), 4)]
        return data.hex(" ").upper()

    # ---- everything at once
    def snapshot(self, live_pids: Optional[List[int]] = None, with_info: bool = True) -> ObdSnapshot:
        snap = ObdSnapshot(protocol=self.protocol)
        snap.status = self.status()
        snap.modules = sorted(snap.status)
        sup = self.supported(0x01)
        for m, pids in sup.items():
            if m not in snap.modules:
                snap.modules.append(m)
        snap.modules.sort()
        wanted = live_pids if live_pids is not None else sorted(
            {p for ps in sup.values() for p in ps if p in PIDS and p & 0x1F != 0 and p not in (0x01, 0x41)})
        snap.pids = self.read_pids(wanted)
        snap.dtcs = self.dtcs()
        if with_info:
            snap.info = self.info()
            for d in snap.info.values():
                if d.get("VIN"):
                    snap.vin = d["VIN"]
                    break
        return snap


def format_value(pid: int, value: Any) -> str:
    p = PIDS.get(pid)
    unit = p.unit if p else ""
    if isinstance(value, float):
        return (("%d %s" % (value, unit)) if value.is_integer() else ("%.2f %s" % (value, unit))).strip()
    if isinstance(value, dict):
        return ", ".join("%s %s" % (k, (("%.3f" % v) if isinstance(v, float) else v)) for k, v in value.items())
    return ("%s %s" % (value, unit)).strip()


__all__ = ["PIDS", "Pid", "ObdSession", "ObdCan", "ObdKLine", "ObdReply", "ObdSnapshot", "Dtc", "decode_dtc",
           "encode_dtc", "supported_pids", "decode_monitor_status", "format_value", "FUNC_ID", "OBD_ADDR"]
