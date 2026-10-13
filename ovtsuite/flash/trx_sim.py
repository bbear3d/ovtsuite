# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bench simulator for the Aisin TRx (VW "09D" TR-60SN-class) TCU.

The counterpart to :mod:`ovtsuite.flash.ecu_sim` (the ZF 6HP28 / UDS-over-ISO-TP
simulator), for the *other* wire stack the TRx uses: **KWP2000 (ISO 14230)**
carried over either **VW TP 2.0 on CAN** or **K-line**. A J2534 device running
this answers on the bus as the TRx so a third-party tool's command/read/write
sequence can be captured without the real module (or a whole car) present. It
is a responder only -- it never flashes anything.

Same three-layer shape as ecu_sim, so the two read the same way:

* the *transport* ECU side -- :class:`Tp20Ecu` (mirrors the tester-side
  :class:`ovtsuite.flash.tp20_transport.Tp20Channel` framing exactly) and
  :class:`KLineEcu` (mirrors :mod:`ovtsuite.flash.kwp`'s KWP2000 framing) --
  each exposes ``recv(timeout_ms) -> KWP payload | None`` and ``send(payload)``;
* the *dispatch* -- :class:`Trx09dResponder`, a KWP2000 service responder;
* what the module *is* -- :class:`TrxProfile` (ids, identification blocks,
  seed/key tape, calibration geometry).

What the TRx is, measured from the real ``.sgo`` files and the SH-2 firmware
(see the project doc ``aisin_09d_container_and_flash.md``):

* **KWP2000**, service set ``10 1A 18 14 21 23 27 31 32 34 36 37 3B 3D 3E`` --
  *not* UDS (no 0x22/0x2E/UDS-0x31). readMemoryByAddress is ``23 04 <u16
  window>`` indexing a small table of flash windows, not a raw address/length.
* **Security** (0x27): exactly ONE seed/key transform in the firmware, and it
  is the VW SA2 bytecode tape stored in the ``.sgo`` metadata
  (``68 03 81 93 54 41 4A 49 4A 05 87 53 41 4A 49 49 4C`` = 3x {rotl1; add
  0x54414A49; if carry xor 0x53414A49}). Read-level and write-level share it,
  differing only by the seed the TCU emits. Verified against the firmware's own
  routine for every seed tested. Run through
  :func:`ovtsuite.flash.crypto.sa2_execute` -- the same VM the 6HP write key
  already uses.
* **Calibration image**: 0x2000..0xDFFFF populated, full image 0xE0000.

As in ecu_sim: ``serve`` mode answers reads from a supplied image so the tool
completes; ``faithful`` refuses reads as the real module does. Security keys are
checked against the tape and logged match/mismatch, but in serve mode access is
granted regardless so an unexpected key never blocks discovery.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    from . import crypto as _crypto
except ImportError:                      # flat import (e.g. inside the 32-bit bridge)
    import crypto as _crypto             # type: ignore

# ------------------------------------------------------------------ KWP2000
POS = 0x40
NR = 0x7F

SID_START_DIAG = 0x10          # startDiagnosticSession
SID_ECU_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x18            # readDiagnosticTroubleCodesByStatus
SID_READ_ECU_ID = 0x1A         # readEcuIdentification (local id)
SID_READ_DATA_LID = 0x21       # readDataByLocalIdentifier
SID_READ_MEM = 0x23            # readMemoryByAddress (TRx: 23 04 <u16 window>)
SID_SECURITY = 0x27            # securityAccess
SID_ROUTINE_START = 0x31       # startRoutineByLocalIdentifier
SID_ROUTINE_STOP = 0x32        # stopRoutineByLocalIdentifier
SID_ROUTINE_RESULT = 0x33      # requestRoutineResultsByLocalIdentifier
SID_REQ_DOWNLOAD = 0x34        # requestDownload
SID_REQ_UPLOAD = 0x35          # requestUpload
SID_TRANSFER = 0x36            # transferData
SID_TRANSFER_EXIT = 0x37       # requestTransferExit
SID_WRITE_DATA_LID = 0x3B      # writeDataByLocalIdentifier
SID_WRITE_MEM = 0x3D           # writeMemoryByAddress
SID_TESTER_PRESENT = 0x3E
SID_STOP_COMM = 0x82

# negative response codes
NRC_GENERAL_REJECT = 0x10
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNC_NOT_SUPPORTED = 0x12
NRC_WRONG_LENGTH = 0x13
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_SEQUENCE = 0x24
NRC_OUT_OF_RANGE = 0x31
NRC_SECURITY_DENIED = 0x33
NRC_INVALID_KEY = 0x35
NRC_RESPONSE_PENDING = 0x78
NRC_SERVICE_NOT_IN_SESSION = 0x7F

ERASE_ROUTINE_LID = 0xC4       # eraseFlash routine local id (knob target); see erase_delay


def nrc(sid: int, code: int) -> bytes:
    return bytes([NR, sid, code])


def _hx(b: bytes) -> str:
    return " ".join("%02X" % x for x in b)


#: The write/read SA2 tape measured in every 09D .sgo metadata block (offset
#: metastart+4, 17 bytes). Default; override per captured file if a variant
#: shows a different tape (as ZF 6HP did for one hardware revision).
TRX_SA2_TAPE = bytes.fromhex("6803819354414a494a058753414a49494c")


def trx_key(seed: bytes, tape: bytes = TRX_SA2_TAPE) -> bytes:
    """TRx seed->key: run the SA2 tape on the 4-byte big-endian seed."""
    seed32 = int.from_bytes(bytes(seed[:4]).rjust(4, b"\x00"), "big")
    return _crypto.sa2_execute(tape, seed32).to_bytes(4, "big")


# ------------------------------------------------------------------ profile
@dataclass
class TrxProfile:
    """What the TRx is, for the simulator."""
    name: str = "aisin09d"
    title: str = "Aisin TRx"
    controller: int = 0x01                  # KWP controller address (dest)
    tp20_dest: Optional[int] = None         # TP 2.0 logical address to answer;
                                            # None = answer ANY 0x200 setup (best
                                            # for capture; auto trans is typ. 0x02)
    tape: bytes = TRX_SA2_TAPE
    #: calibration geometry of the served image
    cal_base: int = 0x2000
    image_size: int = 0xE0000
    #: readEcuIdentification (0x1A) local id -> payload
    idents: Dict[int, bytes] = field(default_factory=dict)
    #: readDataByLocalIdentifier (0x21) local id -> payload
    live: Dict[int, bytes] = field(default_factory=dict)
    #: readMemoryByAddress window id (the u16 after ``23 04``) -> (base, length)
    #: in the flat image. Default: window 1 == the whole calibration region.
    windows: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    #: fixed seed to issue per requestSeed subfunction (else a default)
    seed_values: Dict[int, bytes] = field(default_factory=dict)


def aisin09d_profile() -> TrxProfile:
    return TrxProfile(
        idents={
            0x9B: b"09D927750__",            # spare-part style ident (stub)
            0x91: b"AISIN TRx SIM",
        },
        live={},
        windows={0x0001: (0x2000, 0xDE000)},
        seed_values={0x01: b"\x11\x22\x33\x44", 0x03: b"\x55\x66\x77\x88"},
    )


_PROFILE_FACTORIES: Dict[str, Callable[[], TrxProfile]] = {
    "aisin09d": aisin09d_profile,
}


def profile_names() -> List[str]:
    return list(_PROFILE_FACTORIES)


def profile(name: str) -> TrxProfile:
    return (_PROFILE_FACTORIES.get(name) or aisin09d_profile)()


# ------------------------------------------------------------------ responder
class Trx09dResponder:
    """KWP2000 service responder for the TRx, parameterised by a TrxProfile.
    ``handle(req)`` takes a bare KWP payload (service + data) and returns the
    response payload (service|0x40 + data), a negative response, or None."""

    def __init__(self, profile: TrxProfile, image: bytes = b"",
                 read_mode: str = "serve", strict_key: bool = False,
                 log: Optional[Callable[[str], None]] = None,
                 capture_dir: Optional[str] = None,
                 erase_delay_s: float = 0.0, erase_pending: bool = True,
                 pending_interval_s: float = 0.5):
        self.profile = profile
        self.image = bytes(image)
        self.read_mode = read_mode
        self.strict_key = strict_key
        self.capture_dir = capture_dir
        self.erase_delay_s = erase_delay_s
        self.erase_pending = erase_pending
        self.pending_interval_s = pending_interval_s
        self.log = log or (lambda s: None)
        self.session = 0x81
        self.seeds: Dict[int, bytes] = {}
        self.unlocked = False
        self._download: Optional[dict] = None
        self._write_count = 0

    # ---- helpers ----
    def _mem(self, base: int, length: int) -> bytes:
        img = self.image
        length = max(0, length)
        if not img:
            return bytes(length)
        out = img[base:base + length] if 0 <= base < len(img) else b""
        return bytes(out) + bytes(max(0, length - len(out)))

    def _seed_for(self, sub: int) -> bytes:
        seed = self.profile.seed_values.get(sub) or bytes([0x10 + sub, 0x20, 0x30, 0x40])
        self.seeds[sub] = bytes(seed)
        return bytes(seed)

    def _image_part_number(self) -> Optional[bytes]:
        """Extract the real VAG spare-part number (e.g. b"09D927750FF") from the
        served image's identification block. Real 09D images carry it as ASCII
        near flash 0x2050D (image offset 0x50D): "09D927750XX " then NUL. A tool
        reading 1A 9B gates on a VALID part number, so serving the authentic one
        (not a "09D927750__" placeholder) is what lets it proceed."""
        img = self.image
        if not img:
            return None
        i = img.find(b"09D9")
        if i < 0:
            return None
        j = i
        while j < len(img) and 0x20 <= img[j] < 0x7F and (j - i) < 20:
            j += 1
        pn = bytes(img[i:j]).rstrip(b" ")
        return pn or None

    #: fixed flash addresses of the identification fields inside a 09D image, as
    #: used by the firmware's ident builder at 0x242B4. The served flat image has
    #: flash address == file offset (the SGO section is placed at 0x2000), so
    #: these are used directly as image indices. Same layout across datasets.
    _ID_PN = 0x2050D       # "09D927750FF " part number (12-wide field)
    _ID_DATASET = 0x20527  # "1274" dataset suffix (4-wide field)
    _ID_SW = 0x20500       # "90P01T0I.MOT" software id (20-wide field)

    def _id_field(self, off: int, width: int) -> bytes:
        """A null-terminated ASCII field from the image, space-padded to width --
        exactly how the firmware's field copier (0x243C4) emits each field."""
        img = self.image
        if off < 0 or off >= len(img):
            return b" " * width
        end = img.find(b"\x00", off)
        s = img[off:end if 0 <= end < off + width else off + width]
        return (bytes(s) + b" " * width)[:width]

    def _ident_9b(self) -> Optional[bytes]:
        """The FULL readEcuIdentification 0x9B block, reconstructed in the order
        the firmware builder (0x242B4) assembles it: part number (12) + dataset
        (4) + coding (3, RAM) + hardware/serial (6, RAM) + software (20). The two
        RAM-sourced middle fields are runtime values not present in a static
        image; they are zero-filled here. If the tool still rejects this, a real
        1A 9B capture from an actual 09D TCU is needed for the exact coding/serial."""
        img = self.image
        if not img or img.find(b"09D9") < 0:
            return None
        pn = self._id_field(self._ID_PN, 12)
        dataset = self._id_field(self._ID_DATASET, 4)
        coding = b"\x00\x00\x00"          # 0xFFFF7097, runtime coding (unknown)
        hw_serial = b"\x00" * 6           # 0xFFFF7090, runtime hw/serial (unknown)
        sw = self._id_field(self._ID_SW, 20)
        return pn + dataset + coding + hw_serial + sw

    def _capture(self, name: str, data: bytes) -> None:
        if not self.capture_dir:
            return
        try:
            path = os.path.join(self.capture_dir, name)
            with open(path, "wb") as fh:
                fh.write(data)
            self.log("  captured %d byte(s) -> %s" % (len(data), path))
        except Exception as ex:                      # pragma: no cover - defensive
            self.log("  capture failed: %s" % ex)

    # ---- dispatch ----
    def handle(self, req: bytes) -> Optional[bytes]:
        if not req:
            return None
        s = req[0]
        if s == SID_TESTER_PRESENT:
            # KWP suppressPositiveResponse: subfunction 0x02 (or bit 0x80) -> no
            # reply, so a tester can keepalive a silent erase without injecting
            # stray frames into the stream it is capturing.
            if len(req) > 1 and (req[1] & 0x80 or req[1] == 0x02):
                return None
            return bytes([SID_TESTER_PRESENT | POS, req[1] if len(req) > 1 else 0x01])
        if s == SID_STOP_COMM:
            return bytes([SID_STOP_COMM | POS])
        if s == SID_ECU_RESET:
            return bytes([SID_ECU_RESET | POS, req[1] if len(req) > 1 else 0x01])
        if s == SID_START_DIAG:
            self.session = req[1] if len(req) > 1 else self.session
            # echo subfunction (+ any baud/timing bytes the tester sent)
            return bytes([SID_START_DIAG | POS]) + bytes(req[1:]) if len(req) > 1 \
                else bytes([SID_START_DIAG | POS, 0x81])
        if s == SID_CLEAR_DTC:
            return bytes([SID_CLEAR_DTC | POS])
        if s == SID_READ_DTC:
            return bytes([SID_READ_DTC | POS, 0x00])           # no DTCs
        if s == SID_READ_ECU_ID:
            return self._read_ident(req, SID_READ_ECU_ID, self.profile.idents)
        if s == SID_READ_DATA_LID:
            return self._read_ident(req, SID_READ_DATA_LID,
                                    {**self.profile.idents, **self.profile.live})
        if s == SID_SECURITY:
            return self._security(req)
        if s == SID_READ_MEM:
            return self._read_memory(req)
        if s == SID_REQ_DOWNLOAD:
            return self._request_download(req)
        if s == SID_TRANSFER:
            return self._transfer_data(req)
        if s == SID_TRANSFER_EXIT:
            return self._transfer_exit(req)
        if s in (SID_ROUTINE_START, SID_ROUTINE_STOP, SID_ROUTINE_RESULT):
            return self._routine(req)
        if s == SID_WRITE_DATA_LID:
            lid = req[1] if len(req) > 1 else 0
            self.log("  writeDataByLocalId 0x%02X data=%s (accepted)"
                     % (lid, bytes(req[2:]).hex()))
            return bytes([SID_WRITE_DATA_LID | POS, lid])
        if s == SID_WRITE_MEM:
            self.log("  writeMemoryByAddress %s (accepted, logged)" % bytes(req[1:]).hex())
            return bytes([SID_WRITE_MEM | POS])
        return nrc(s, NRC_SERVICE_NOT_SUPPORTED)

    #: identification localIds that should return the spare-part number
    _PN_IDENT_LIDS = (0x9B, 0x9A, 0x91)

    def _read_ident(self, req: bytes, sid: int, table: Dict[int, bytes]) -> bytes:
        if len(req) < 2:
            return nrc(sid, NRC_WRONG_LENGTH)
        lid = req[1]
        # 1A 9B: serve the FULL identification block (the tool gates on it, and
        # a bare part number is rejected -- see trxreadsimfail.txt).
        if lid == 0x9B:
            blk = self._ident_9b()
            if blk is not None:
                self.log("  readEcuId 0x9B -> full ident block %d bytes (PN %r)"
                         % (len(blk), self._image_part_number()))
                return bytes([sid | POS, lid]) + blk
        # Other identification localIds: the authentic part number from the image.
        if lid in self._PN_IDENT_LIDS:
            pn = self._image_part_number()
            if pn:
                self.log("  readEcuId 0x%02X -> part number %r (from image)" % (lid, pn.decode("latin1")))
                return bytes([sid | POS, lid]) + pn
        val = table.get(lid)
        if val is None:
            return nrc(sid, NRC_OUT_OF_RANGE)
        return bytes([sid | POS, lid]) + bytes(val)

    def _security(self, req: bytes) -> bytes:
        if len(req) < 2:
            return nrc(SID_SECURITY, NRC_WRONG_LENGTH)
        sub = req[1]
        if sub % 2 == 1:                              # requestSeed
            return bytes([SID_SECURITY | POS, sub]) + self._seed_for(sub)
        seed = self.seeds.get(sub - 1)                # sendKey
        key = bytes(req[2:])
        if seed is None:
            return nrc(SID_SECURITY, NRC_SEQUENCE)
        try:
            expected = trx_key(seed, self.profile.tape)
        except Exception as ex:                        # pragma: no cover - defensive
            self.log("  key algorithm error: %s" % ex)
            expected = None
        if expected is not None:
            ok = key == expected
            self.log("  sendKey level %02X: tool=%s expected=%s -> %s"
                     % (sub, key.hex(), expected.hex(), "MATCH" if ok else "MISMATCH"))
            if not ok and self.strict_key:
                return nrc(SID_SECURITY, NRC_INVALID_KEY)
        self.unlocked = True
        return bytes([SID_SECURITY | POS, sub])

    def _read_memory(self, req: bytes) -> bytes:
        if self.read_mode != "serve":
            return nrc(SID_READ_MEM, NRC_SERVICE_NOT_IN_SESSION)
        # Disambiguate the two 0x23 forms by LENGTH, not by req[1]:
        #   raw-address form  = 23 <addr-hi><addr-mid><addr-lo> <len>   (5 bytes)
        #   windowed form     = 23 04 <u16 window-id>                   (4 bytes)
        # The real tool only ever uses the 5-byte raw form; the firmware's 0x23
        # handler also accepts the 4-byte windowed form. These MUST be told apart
        # by length -- a 5-byte raw read of any 0x04xxxx address has addr-hi 0x04,
        # which previously got mis-parsed as the windowed form (req[1]==0x04) and
        # served the wrong data, so the tool's sequential dump died at 0x0400EE
        # (ecu-sim-20261007-210945.log). Raw form takes precedence for len >= 5.
        if len(req) >= 5:
            base = int.from_bytes(req[1:4], "big")
            length = int.from_bytes(req[4:6], "big") if len(req) >= 6 else req[4]
            length = max(0, min(length, 0x400))
            return bytes([SID_READ_MEM | POS]) + self._mem(base, length)
        if len(req) == 4 and req[1] == 0x04:            # windowed form (not seen on the wire)
            win = (req[2] << 8) | req[3]
            rng = self.profile.windows.get(win)
            if rng is None:
                self.log("  readMemoryByAddress window 0x%04X (unknown; serving cal base)" % win)
                base, length = self.profile.cal_base, 0x100
            else:
                base, length = rng
            self.log("  readMemoryByAddress window 0x%04X -> base 0x%X len 0x%X"
                     % (win, base, length))
            return bytes([SID_READ_MEM | POS]) + self._mem(base, min(length, 0x400))
        return nrc(SID_READ_MEM, NRC_WRONG_LENGTH)

    def _request_download(self, req: bytes) -> bytes:
        # KWP: 34 <addr:3> <fmt> <size:3> (layouts vary); log + accept, start a
        # capture accumulator. maxNumberOfBlockLength reply 0x74 <lenfmt> <...>.
        addr = int.from_bytes(req[1:4], "big") if len(req) >= 4 else 0
        self._download = {"addr": addr, "buf": bytearray(), "frames": 0}
        self.log("  requestDownload addr=0x%X raw=%s" % (addr, bytes(req[1:]).hex()))
        return bytes([SID_REQ_DOWNLOAD | POS, 0x00, 0xFF])     # grant ~255-byte blocks

    def _transfer_data(self, req: bytes) -> bytes:
        if self._download is None:
            return nrc(SID_TRANSFER, NRC_SEQUENCE)
        self._download["buf"] += bytes(req[1:])
        self._download["frames"] += 1
        return bytes([SID_TRANSFER | POS])

    def _transfer_exit(self, req: bytes) -> bytes:
        if self._download is not None:
            buf = bytes(self._download["buf"])
            self.log("  requestTransferExit: %d byte(s) in %d frame(s) at 0x%X"
                     % (len(buf), self._download["frames"], self._download["addr"]))
            self._write_count += 1
            self._capture("trx-write-%02d-addr%06X.bin"
                          % (self._write_count, self._download["addr"]), buf)
            # Best-effort: the 09D transferData cipher is not yet confirmed. Try
            # the ZF 6HP BCB/MILKYWAY codec (shared SGO lineage) and save the
            # plaintext too if it decodes, purely for analysis -- never assumed.
            try:
                plain = _crypto.zf6hp_bcb_decode(buf)
                self._capture("trx-write-%02d-addr%06X.bcb-plain.bin"
                              % (self._write_count, self._download["addr"]), plain)
                self.log("    (decoded as ZF6HP BCB -> %d byte(s) plaintext)" % len(plain))
            except Exception:
                pass
            self._download = None
        return bytes([SID_TRANSFER_EXIT | POS])

    def _routine(self, req: bytes) -> bytes:
        s = req[0]
        lid = req[1] if len(req) > 1 else 0
        self.log("  routine %02X localId 0x%02X args=%s"
                 % (s, lid, bytes(req[2:]).hex()))
        if s == SID_ROUTINE_RESULT:
            return bytes([s | POS, lid, 0x00])         # result: success
        return bytes([s | POS, lid])

    def is_erase(self, req: bytes) -> bool:
        return (len(req) >= 2 and req[0] == SID_ROUTINE_START
                and req[1] == ERASE_ROUTINE_LID)


# ------------------------------------------------------------------ TP 2.0 ECU
# opcodes (mirror ovtsuite.flash.tp20_transport, ECU side)
_SETUP_ID = 0x200
_BROADCAST_SETUP = 0xC0
_SETUP_OK = 0xD0
_OP_DATA_ACK = 0x0
_OP_DATA_ACK_LAST = 0x1
_OP_DATA_NOACK = 0x2
_OP_DATA_NOACK_LAST = 0x3
_OP_ACK_READY = 0xB
_OP_ACK_NOTREADY = 0x9
_OP_PARAM_REQ = 0xA0
_OP_PARAM_RESP = 0xA1
_OP_CHANNEL_TEST = 0xA3
_OP_BREAK = 0xA4
_OP_DISCONNECT = 0xA8


class _FakeMsg:
    """Minimal CAN message for re-buffering a frame into the read queue."""
    __slots__ = ("can_id", "payload", "is_tx_echo")
    def __init__(self, can_id, payload):
        self.can_id = can_id; self.payload = bytes(payload); self.is_tx_echo = False


@dataclass
class TrxTiming:
    """Bench 'unclean bus' knobs for the TP 2.0 path -- the TRx counterpart to
    ecu_sim.SimTiming, same field names/meanings so the GUI's one timing block
    drives both engines. Reproduces a slow / lossy bus so a flashing tool can be
    exercised against those conditions: a per-frame send floor, a per-response
    latency (both with +/- jitter), and occasional dropped frames / whole
    responses. A dropped frame or response is NOT lost data -- the full response
    stays cached, so the tester recovers it on its retransmit (which the dedup
    path re-sends), exactly as on a real lossy bus. All-zero = ideal timing.

    ecu_sim.SimTiming is duck-compatible here (same fields + active()/jitter()),
    so the GUI can pass its SimTiming straight through on the client path; across
    the 32-bit bridge the fields travel as a dict and are rebuilt into this."""
    cf_gap_ms: float = 0.0          # ECU's own min delay between consecutive frames
    resp_latency_ms: float = 0.0    # delay before each response's first frame
    jitter_ms: float = 0.0          # +/- uniform jitter on the two delays above
    drop_cf_prob: float = 0.0       # chance (0..1) to drop ONE frame of a response
    drop_resp_prob: float = 0.0     # chance (0..1) to drop an ENTIRE response
    stall_every: int = 0            # every Nth response, stall for stall_ms...
    stall_ms: float = 0.0           # ...to mimic an occasional long comms gap

    def active(self) -> bool:
        return bool(self.cf_gap_ms or self.resp_latency_ms or self.drop_cf_prob
                    or self.drop_resp_prob or (self.stall_every and self.stall_ms))

    def jitter(self, base_ms: float) -> float:
        import random
        if self.jitter_ms <= 0 or base_ms <= 0:
            return base_ms
        return max(0.0, base_ms + random.uniform(-self.jitter_ms, self.jitter_ms))

    def as_dict(self) -> dict:
        return {"cf_gap_ms": self.cf_gap_ms, "resp_latency_ms": self.resp_latency_ms,
                "jitter_ms": self.jitter_ms, "drop_cf_prob": self.drop_cf_prob,
                "drop_resp_prob": self.drop_resp_prob, "stall_every": self.stall_every,
                "stall_ms": self.stall_ms}

    @classmethod
    def from_dict(cls, d) -> "Optional[TrxTiming]":
        if not d:
            return None
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


def drain_transfer_batch(srv, first: bytes, cap: int = 64, idle_ms: int = 1500):
    """Continuously drain a transferData (0x36) WRITE stream from one ECU-side
    responder, ACKing each block in place -- used by the in-bridge responder so a
    streamed write is not desynced by the per-message IPC gap (a plain read needs
    none of this; it is one request per block).

    ``first`` is the first 0x36 payload already received. Each block is ACKed with
    a bare ``76`` via ``srv.send`` (transferData is always accepted in the capture
    sim, which is what makes the in-place ACK safe), its payload collected for the
    64-bit responder to capture, and the next block awaited up to ``idle_ms``.
    Collection stops at ``cap`` blocks, at a non-0x36 message (e.g. ``0x37``
    requestTransferExit), or when the stream pauses. The LAST block of a FULL
    (cap-sized) batch is deliberately left un-ACKed and reported as ``held`` so the
    tester stays flow-controlled across the batch boundary and never streams into a
    gap; the caller ACKs it before the next drain. Returns
    ``(blocks_hex, tail_hex_or_None, held)``."""
    blocks = [first.hex()]
    tail = None
    held = False
    while len(blocks) < cap:
        srv.send(b"\x76")                    # ACK the block we just took
        nxt = srv.recv(idle_ms)
        if nxt is None:
            break                            # stream paused/ended; every block ACKed
        if nxt and nxt[0] != 0x36:
            tail = nxt.hex()                 # e.g. 0x37 exit -> the caller handles it
            break
        blocks.append(nxt.hex())
    else:                                    # loop hit cap without breaking
        held = True                          # last block left un-ACKed (held)
    return blocks, tail, held


class Tp20Ecu:
    """ECU side of a VW TP 2.0 channel over a raw-CAN channel (the mirror of
    the tester-side :class:`ovtsuite.flash.tp20_transport.Tp20Channel`).

    The CAN channel need only expose ``write_can(can_id, payload)`` and
    ``read(max_msgs, timeout_ms) -> [msg]`` with ``msg.can_id`` / ``msg.payload``
    -- same contract ecu_sim relies on. ``recv``/``send`` speak bare KWP
    payloads (service + data); the 16-bit length prefix and framing live here.
    """

    #: The exact A1 timing a real VAG ECU returns (block size 0x0F, T1 0x8A=100ms,
    #: T3 0x4A=10ms), verified against Reference/Logs/VCDS-Cluster-TP2.0.log
    #: ("A1 0F 8A FF 4A FF"). Kept for reference / real-hardware parity.
    A1_PARAMS_REAL = bytes([0x0F, 0x8A, 0xFF, 0x4A, 0xFF])
    #: Bench default: T1 raised to 0xCA (= 10 x 100ms = 1s) so the tester stays
    #: patient while each ACK/response crosses the J2534 *bridge* (far slower than
    #: a real CAN bus -- our request->ACK round-trip is ~300-400ms). With the real
    #: 100ms T1 the tool retransmitted every request 3-4x and aborted the dump
    #: after ~13 blocks (ecu-sim-20261007-183056.log). 0xCA matches the T1 the
    #: tool itself advertised in its A0. Only byte 1 (T1) differs from real.
    A1_PARAMS = bytes([0x0F, 0xCA, 0xFF, 0x4A, 0xFF])

    def __init__(self, can, dest=None, ecu_rx_id: int = 0x740,
                 pad: int = 0x00, log: Optional[Callable[[str], None]] = None,
                 a1_params: Optional[bytes] = None, timing=None):
        self.can = can
        # Optional bench 'unclean bus' timing (TrxTiming / ecu_sim.SimTiming, or
        # None for ideal). Applied in send(): per-response latency/drop, per-frame
        # gap/drop -- see send(). Duck-typed (needs fields + active()/jitter()).
        self.timing = timing
        self._resp_count = 0
        # dest = the ECU logical address to answer channel-setup for. None means
        # "answer ANY 0x200 setup broadcast" -- the right default for bench
        # capture, where the tool's target address isn't known up front (an auto
        # trans is typically 0x02, but we log whatever the tool actually used).
        self.dest = dest
        self.matched_dest: Optional[int] = None   # the logical addr actually used
        self.ecu_rx_id = ecu_rx_id          # id the tester sends to us on (we assign)
        self.ecu_tx_id: Optional[int] = None  # id we send on (the tester's listen id)
        self.pad = pad
        self._log = log or (lambda s: None)
        self.a1_params = bytes(a1_params) if a1_params else self.A1_PARAMS
        self.block_size = 0x0F
        self.tx_seq = 0
        self.rx_seq = 0
        self.opened = False
        self.frames_seen = 0
        self._pending: List = []
        # retransmit handling: a real tool re-sends its request (same TP2.0
        # sequence) when it hasn't yet accepted our reply. We must NOT dispatch
        # it again -- doing so burns a fresh TX sequence per copy and desyncs the
        # counter the tool is tracking (observed: 10 86 answered 4x as seq 7/8/9/A,
        # then the read reply came as seq B while the tool still expected seq 8, so
        # it rejected every read and disconnected). Instead we re-ACK and re-send
        # the *same* cached response frames without advancing tx_seq.
        self.last_rx_first_seq: Optional[int] = None   # first-frame seq last dispatched
        self._last_tx_frames: List[bytes] = []         # raw frames of the last response

    #: when True, log every TP2.0 frame sent/received (id + hex) for wire-level
    #: debugging. Compact; left on while the sim handshake is being brought up.
    trace_frames = True

    # ---- raw frame io ----
    def _send(self, can_id: int, data: bytes):
        if self.trace_frames:
            self._log("  TXf %03X %s" % (can_id, data.hex()))
        self.can.write_can(can_id, bytes(data))

    def _next(self, timeout_ms: int, want_ids) -> Optional[Tuple[int, bytes]]:
        """Return the next frame on one of ``want_ids``. Frames read in the same
        batch that don't match (or that arrive after the one returned) are kept
        in ``self._pending`` so no frame is ever dropped."""
        end = time.time() + timeout_ms / 1000.0
        while True:
            # serve from anything already buffered first
            while self._pending:
                m = self._pending.pop(0)
                cid = getattr(m, "can_id", None)
                data = bytes(getattr(m, "payload", b""))
                if cid in want_ids and data:
                    return cid, data
            rem = max(1, int((end - time.time()) * 1000)) if timeout_ms else 0
            batch = self.can.read(max_msgs=32, timeout_ms=rem)
            for m in batch:
                if getattr(m, "is_tx_echo", False):
                    continue
                if self.trace_frames:
                    cid = getattr(m, "can_id", 0); pl = bytes(getattr(m, "payload", b""))
                    if cid in (_SETUP_ID, self.ecu_rx_id) and pl:
                        self._log("  RXf %03X %s" % (cid, pl.hex()))
                self.frames_seen += 1
                self._pending.append(m)
            if not self._pending and time.time() >= end:
                return None

    # ---- channel lifecycle ----
    def _answer_setup(self, data: bytes) -> bool:
        """Answer one 0x200 broadcast channel-setup. A real tool retries the
        setup until it accepts a D0 reply, so this is called for EVERY setup
        frame seen (not just the first) -- re-answering is harmless and fixes a
        missed-first-reply deadlock (see Scanmatik trxreadattempt.txt). Returns
        True if this was a valid setup for our address."""
        # setup request on 0x200: [addr C0 00 10 <rxlo> <rxhi> 01]
        #   addr = ECU logical address; bytes[4:6] = id the ECU must TRANSMIT on
        #   (the tester listens there). See Reference/Logs/VCDS-Cluster-TP2.0.log.
        if len(data) < 7 or data[1] != _BROADCAST_SETUP:
            return False
        addr = data[0]
        if self.dest is not None and addr != self.dest:
            self._log("TP2.0 setup for logical 0x%02X ignored (listening for 0x%02X)"
                      % (addr, self.dest))
            return False
        self.matched_dest = addr
        self.ecu_tx_id = data[4] | (data[5] << 8)     # id the tester receives on
        resp_id = _SETUP_ID + addr                    # 0x200 + logical (tool filters this)
        # D0 reply: [00 D0 <ecu_tx lo hi> <ecu_rx lo hi> 01] (byte0 = 0x00).
        resp = bytes([0x00, _SETUP_OK,
                      self.ecu_tx_id & 0xFF, (self.ecu_tx_id >> 8) & 0xFF,
                      self.ecu_rx_id & 0xFF, (self.ecu_rx_id >> 8) & 0xFF, 0x01])
        self._send(resp_id, resp)
        self.opened = True
        self.tx_seq = 0
        self.rx_seq = 0
        self.last_rx_first_seq = None
        self._last_tx_frames = []
        self._log("TP2.0 setup logical 0x%02X: TX 0x%03X %s  (tester->0x%X, ECU->0x%X)"
                  % (addr, resp_id, _hx(resp), self.ecu_rx_id, self.ecu_tx_id))
        return True

    def wait_setup(self, timeout_ms: int = 2000) -> bool:
        """Block until a 0x200 channel-setup is answered. Keeps reading past
        unrelated 0x200 traffic until a valid setup for our address lands."""
        end = time.time() + timeout_ms / 1000.0
        while time.time() < end:
            got = self._next(max(1, int((end - time.time()) * 1000)), {_SETUP_ID, self.ecu_rx_id})
            if got is None:
                return False
            cid, data = got
            if cid == _SETUP_ID and self._answer_setup(data):
                return True
        return False

    def _a1(self) -> bytes:
        return bytes([_OP_PARAM_RESP]) + self.a1_params

    def _handle_mgmt(self, data: bytes) -> str:
        """A-frame: answer a param-request (A0) or channel-test (A3) with our
        A1, report a disconnect (A8). Returns 'disc' | 'skip'."""
        b = data[0]
        if b in (_OP_PARAM_REQ, _OP_CHANNEL_TEST):     # A0 param req / A3 keep-alive
            if b == _OP_PARAM_REQ and len(data) > 1:
                self.block_size = data[1] or 0x0F
            if self.ecu_tx_id is not None:
                self._send(self.ecu_tx_id, self._a1())
                self._log("TP2.0 %s -> A1 %s"
                          % ("A0 params" if b == _OP_PARAM_REQ else "A3 test", _hx(self._a1())))
            return "skip"
        if b == _OP_DISCONNECT:
            self.opened = False
            return "disc"
        return "skip"

    # ---- KWP payload io ----
    #: once a multi-frame message has started, wait at least this long for each
    #: continuation frame regardless of the (possibly short) idle poll timeout,
    #: so a slow inter-frame gap never truncates a message mid-reassembly.
    INTER_FRAME_MS = 1500

    def recv(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Reassemble one inbound TP 2.0 message -> KWP payload, acking the
        tester's blocks. Answers channel-tests transparently. None if no
        message starts within ``timeout_ms`` (the idle poll); once one starts,
        continuation frames are awaited up to ``INTER_FRAME_MS`` each."""
        if not self.opened:
            if not self.wait_setup(max(timeout_ms, 2000)):
                return None
        # outer loop: a retransmit of the just-handled request is swallowed
        # (re-ACKed + answered from cache) so it never reaches the dispatcher.
        while True:
            acc = bytearray()
            expected = None
            first_seq = None
            end = time.time() + timeout_ms / 1000.0
            while True:
                rem = int((end - time.time()) * 1000)
                if rem <= 0:
                    return None
                # watch 0x200 the whole time: a real tool RE-SENDS the
                # channel-setup until it accepts our D0, so keep answering it.
                got = self._next(rem, {_SETUP_ID, self.ecu_rx_id})
                if got is None:
                    return None
                cid, data = got
                if cid == _SETUP_ID:
                    self._answer_setup(data)
                    continue
                op = data[0] >> 4
                if (data[0] & 0xF0) == 0xA0:
                    if self._handle_mgmt(data) == "disc":
                        return None
                    continue
                if op not in (_OP_DATA_ACK, _OP_DATA_ACK_LAST, _OP_DATA_NOACK, _OP_DATA_NOACK_LAST):
                    continue
                # progress -> extend; once started, use the generous inter-frame
                # window so a short idle poll can't truncate a multi-frame message
                end = time.time() + self.INTER_FRAME_MS / 1000.0
                if first_seq is None:
                    first_seq = data[0] & 0x0F
                acc += data[1:]
                if expected is None and len(acc) >= 2:
                    expected = int.from_bytes(acc[:2], "big")
                if op in (_OP_DATA_ACK, _OP_DATA_ACK_LAST):     # tester wants an ACK
                    self.rx_seq = (data[0] & 0x0F) + 1
                    self._send(self.ecu_tx_id, bytes([(_OP_ACK_READY << 4) | (self.rx_seq & 0x0F)]))
                if op in (_OP_DATA_ACK_LAST, _OP_DATA_NOACK_LAST):
                    break
            # retransmit of the request we last answered? re-send the cached
            # response (same seq) and keep waiting -- do NOT re-dispatch.
            if first_seq is not None and first_seq == self.last_rx_first_seq:
                self._log("  (retransmit of req seq %X -> re-ACK + re-send last response)"
                          % first_seq)
                self._resend_last()
                continue
            self.last_rx_first_seq = first_seq
            payload = bytes(acc[2:])
            return payload[:expected] if expected is not None else payload

    def _resend_last(self) -> None:
        """Re-emit the frames of the last response verbatim (same sequence), for
        a tester that re-requested because it missed our reply."""
        if self.ecu_tx_id is None:
            return
        for frame in self._last_tx_frames:
            self._send(self.ecu_tx_id, frame)

    def send(self, payload: bytes, timeout_ms: int = 2000) -> None:
        """Send one KWP payload as a TP 2.0 message, waiting for the tester's
        ACK at each block boundary (mirror of Tp20Channel.send, ECU side)."""
        if self.ecu_tx_id is None:
            return
        payload = bytes(payload)
        body = len(payload).to_bytes(2, "big") + payload
        chunks = [body[i:i + 7] for i in range(0, len(body), 7)] or [b""]
        n = len(chunks)
        built = []
        for i, chunk in enumerate(chunks):
            last = i == n - 1
            boundary = last or ((i + 1) % max(1, self.block_size) == 0)
            op = (_OP_DATA_ACK_LAST if last else _OP_DATA_ACK) if boundary else _OP_DATA_NOACK
            seq = (self.tx_seq + i) & 0x0F
            built.append((seq, bytes([(op << 4) | seq]) + chunk, boundary))
        self.tx_seq = (self.tx_seq + n) & 0x0F
        self._last_tx_frames = [frame for (_s, frame, _b) in built]
        # The full response is ALWAYS cached above, even when a 'bus' fault drops
        # frames below -- so a tester that times out and retransmits recovers the
        # whole thing via the dedup path (_resend_last), exactly as on a real
        # lossy bus. send() per frame (never a batched multi-message write: that
        # overran the bench device's TX buffer, ecu-sim-20261007-200302.log).
        self._apply_resp_timing()                       # latency / stall (whole-response)
        drop_whole = self._should_drop_response()
        if drop_whole:
            self._log("  (timing: DROPPED whole response -- tester should see no reply)")
        import random as _rnd
        t = self.timing
        summary = self.trace_frames and n > 8 and not drop_whole
        if summary:
            self._log("  TXf %03X x%d (frames) %s..%s"
                      % (self.ecu_tx_id, n, built[0][1].hex(), built[-1][1].hex()))
        for i, (seq, frame, boundary) in enumerate(built):
            send_this = not drop_whole
            # drop ONE consecutive frame mid-response (never the first): a gap the
            # tester must survive; the cached full response covers the retransmit.
            if (send_this and t is not None and i > 0 and getattr(t, "drop_cf_prob", 0)
                    and _rnd.random() < t.drop_cf_prob):
                self._log("  (timing: DROPPED frame %d/%d seq %X -- tester should time out)"
                          % (i + 1, n, seq))
                send_this = False
            if send_this:
                if self.trace_frames and not summary:
                    self._log("  TXf %03X %s" % (self.ecu_tx_id, frame.hex()))
                self.can.write_can(self.ecu_tx_id, bytes(frame))
            # inter-frame gap floor (+/- jitter): the real TCU paced ~10 ms/frame
            if t is not None and i != n - 1:
                gap = t.jitter(getattr(t, "cf_gap_ms", 0.0))
                if gap > 0:
                    time.sleep(gap / 1000.0)
            # Wait for the tester's block-ACK only at a MID-message boundary (flow
            # control). Do NOT block on the final frame's ACK: returning lets
            # recv() answer the next request a round-trip sooner. Skip the wait
            # when we dropped this boundary frame (no ACK will come).
            if boundary and i != n - 1 and send_this:
                self._await_ack((seq + 1) & 0x0F, 600)

    def _apply_resp_timing(self) -> None:
        """Per-response latency + periodic stall (whole-response bench timing)."""
        t = self.timing
        if t is None or not t.active():
            return
        self._resp_count += 1
        import random  # noqa: F401 (kept local; jitter() uses it too)
        if getattr(t, "stall_every", 0) and getattr(t, "stall_ms", 0) and \
                (self._resp_count % t.stall_every == 0):
            self._log("  (timing: stall %.0f ms before reply)" % t.stall_ms)
            time.sleep(t.stall_ms / 1000.0)
        lat = t.jitter(getattr(t, "resp_latency_ms", 0.0))
        if lat > 0:
            time.sleep(lat / 1000.0)

    def _should_drop_response(self) -> bool:
        import random as _rnd
        t = self.timing
        return bool(t is not None and getattr(t, "drop_resp_prob", 0)
                    and _rnd.random() < t.drop_resp_prob)

    def _await_ack(self, want_seq: int, timeout_ms: int) -> None:
        """Wait (briefly) for the tester's block-ACK after a response. If instead
        a DATA frame arrives (the tester re-sending its request because it hasn't
        accepted our reply yet), buffer it so the next recv() handles it rather
        than dropping it -- otherwise the tester's retransmits are lost and it
        gives up (seen in trxreadsimfail.txt: 10 86 re-sent 6x, no progress)."""
        end = time.time() + timeout_ms / 1000.0
        while time.time() < end:
            got = self._next(max(1, int((end - time.time()) * 1000)), {_SETUP_ID, self.ecu_rx_id})
            if got is None:
                return
            cid, data = got
            if cid == _SETUP_ID:
                self._pending.insert(0, _FakeMsg(cid, data)); return
            op = data[0] >> 4
            if op == _OP_ACK_READY:
                return
            if op == _OP_ACK_NOTREADY:
                continue
            if (data[0] & 0xF0) == 0xA0:
                if self._handle_mgmt(data) == "disc":
                    return
                continue
            # a DATA frame: the tester is re-requesting -> hand it back to recv()
            self._pending.insert(0, _FakeMsg(cid, data))
            return

    def close(self):
        if self.opened and self.ecu_tx_id is not None:
            try:
                self._send(self.ecu_tx_id, bytes([_OP_DISCONNECT]))
            except Exception:
                pass
        self.opened = False


# ------------------------------------------------------------------ K-line ECU
class KLineEcu:
    """ECU side of KWP2000 (ISO 14230) over a byte link.

    Mirrors :mod:`ovtsuite.flash.kwp`'s framing from the ECU's seat: read one
    addressed request frame (``fmt [dest src] [len] service data cks``) and
    write the response as ``fmt F1 <ctrl> [len] service data cks`` (ECU src =
    controller, dest = tester 0xF1). ``link`` is any object exposing
    ``read(n) -> bytes`` (blocking up to a timeout, raising on none) and
    ``write_bytes(bytes)`` -- the same KLine contract the tester side uses.

    K-line init (5-baud / fast-init) is link/hardware-specific and handled by
    the link; this layer starts once the link is up.
    """

    TESTER = 0xF1

    def __init__(self, link, controller: int = 0x01,
                 log: Optional[Callable[[str], None]] = None):
        self.k = link
        self.controller = controller
        self._log = log or (lambda s: None)
        self.frames_seen = 0

    def _read_byte(self) -> Optional[int]:
        try:
            b = self.k.read(1)
        except Exception:
            return None
        if not b:
            return None
        return b[0]

    def recv(self, timeout_ms: int = 1000) -> Optional[bytes]:
        fmt = self._read_byte()
        if fmt is None:
            return None
        hdr = bytearray([fmt])
        addressed = bool(fmt & 0x80)
        if addressed:
            hdr += self.k.read(2)                     # dest, src
        n = fmt & 0x3F
        if n == 0:
            ln = self.k.read(1)
            hdr += ln
            n = ln[0]
        body = self.k.read(n)
        self.k.read(1)                                # checksum (not enforced)
        self.frames_seen += 1
        return bytes(body)                            # service + data

    def send(self, payload: bytes, addressed: bool = True) -> None:
        payload = bytes(payload)
        n = len(payload)
        out = bytearray()
        fmt = 0x80 | (n if n < 0x40 else 0) if addressed else (n if n < 0x40 else 0)
        out.append(fmt)
        if addressed:
            out += bytes([self.TESTER, self.controller])
        if n >= 0x40:
            out.append(n & 0xFF)
        out += payload
        out.append(sum(out) & 0xFF)
        self.k.write_bytes(bytes(out))


# ------------------------------------------------------------------ pump
@dataclass
class Simulator:
    """Pump a transport (Tp20Ecu / KLineEcu), dispatch to the responder, send
    the reply, log -- the TRx counterpart to ecu_sim.Simulator."""
    io: object                                        # Tp20Ecu | KLineEcu
    responder: Trx09dResponder
    log: Callable[[str], None] = field(default=lambda s: None)
    count: int = 0

    def run(self, is_stop: Optional[Callable[[], bool]] = None, idle_ms: int = 100):
        is_stop = is_stop or (lambda: False)
        self.log("%s simulator listening (%s); %s reads"
                 % (self.responder.profile.title, type(self.io).__name__,
                    self.responder.read_mode))
        _beat = time.time()
        while not is_stop():
            try:
                req = self.io.recv(timeout_ms=idle_ms)
            except Exception as ex:
                self.log("rx error: %s" % ex)
                continue
            if req is None:
                if time.time() - _beat >= 5.0:
                    seen = getattr(self.io, "frames_seen", 0)
                    self.log("  ...listening (%d frame(s) seen, %d handled)" % (seen, self.count))
                    _beat = time.time()
                continue
            self.step(req)
            _beat = time.time()

    def step(self, req: bytes):
        self.count += 1
        self.log("RX  %s" % _hx(req))
        resp = self.responder.handle(req)
        if resp is None:
            self.log("   (no response)")
            return
        if self.responder.erase_delay_s > 0 and self.responder.is_erase(req):
            self._hold_erase(req[0])
        self.log("TX  %s" % _hx(resp))
        try:
            self.io.send(resp)
        except Exception as ex:
            self.log("tx error: %s" % ex)

    def _hold_erase(self, sid: int):
        delay = self.responder.erase_delay_s
        emit = self.responder.erase_pending
        interval = max(0.05, self.responder.pending_interval_s)
        end = time.time() + delay
        n = 0
        while time.time() < end:
            time.sleep(min(interval, max(0.0, end - time.time())))
            if emit and time.time() < end:
                try:
                    self.io.send(bytes([NR, sid, NRC_RESPONSE_PENDING]))
                    n += 1
                except Exception:
                    break
        self.log("  held erase %.1fs (%d responsePending%s)"
                 % (delay, n, "" if emit else ", silent"))


# ------------------------------------------------------------------ image load
def load_image(path: Optional[str]) -> bytes:
    """Flat image to serve reads from. Handles a 09D .sgo/.09d structurally
    (single plain outer-XOR-0xFF section placed at its address), else raw."""
    if not path:
        return b""
    data = open(path, "rb").read()
    if data[:16] == b"SGML Object File":
        try:
            return _sgo_flat_image(data)
        except Exception:
            return data
    return data


def _sgo_flat_image(data: bytes) -> bytes:
    """Structural flat image from a v2 SGO: place each section's XOR-0xFF'd blob
    at its declared address. For 09D the single section is plain (stored_len ==
    declared_len), so this is the calibration image directly. Independent of
    sgo.py's scheme/checksum gating (09D's checksum16 field is not a usable
    self-check -- see the project doc)."""
    ms = struct.unpack_from("<I", data, 0x29)[0]
    ml = struct.unpack_from("<I", data, ms)[0]
    sgolen = struct.unpack_from("<I", data, 0x2D)[0]
    bs = ms + ml + 4
    hi = 0
    secs = []
    while bs < sgolen:
        addr = int.from_bytes(data[bs:bs + 3], "big")
        declared = int.from_bytes(data[bs + 4:bs + 7], "big")
        slen = struct.unpack_from("<I", data, bs + 0x15)[0]
        blob = data[bs + 0x19: bs + 0x19 + slen]
        if slen == declared:                          # plain (uncompressed)
            secs.append((addr, bytes(b ^ 0xFF for b in blob)))
            hi = max(hi, addr + declared)
        bs += 0x19 + slen
    out = bytearray(b"\xFF" * hi)
    for addr, body in secs:
        out[addr:addr + len(body)] = body
    return bytes(out)


def image_health(image: bytes) -> Optional[str]:
    """Warn if the image looks like a partial/stripped calibration. The three
    short internal-PN 09D datasets (FF_1274/FT_1168/GJ_1095) carry the lower
    cal block but leave the shift/program tail (0x10000+) blank (0xFFFF), which
    makes a tool's readMemoryByAddress of that region return all-0xFF and be
    rejected. Only the full 09D927750__ images (HN_3403, LD_3405) are complete.
    Returns a one-line warning, or None when the tail carries data."""
    if len(image) < 0x106B0:
        return None
    tail = image[0x10000:0x106B0]
    ff = tail.count(0xFF)
    if ff > 0.95 * len(tail):
        return ("WARNING: shift/program tail (0x10000-0x106B0) is blank -- this "
                "looks like a STRIPPED partial calibration. A tool reading that "
                "region will get 0xFF and reject it. Use a full 09D927750__ "
                "image (HN_3403 / LD_3405) to bench the read.")
    return None


def main(argv=None):   # pragma: no cover - hardware/CLI entry point
    import argparse
    from . import j2534 as _j
    from . import ecu_sim as _es

    ap = argparse.ArgumentParser(
        description="Simulate an Aisin TRx (09D) TCU on CAN (TP2.0) to capture a "
                    "tool's KWP2000 sequence.")
    ap.add_argument("--dll", required=True, help="J2534 DLL for the SIM device")
    ap.add_argument("--image", help=".sgo / .09d / .bin to serve reads from")
    ap.add_argument("--baud", type=int, default=500000)
    ap.add_argument("--dest", type=lambda x: int(x, 0), default=None,
                    help="TP2.0 logical address to answer (default: any)")
    ap.add_argument("--ecu-rx", type=lambda x: int(x, 0), default=0x740)
    ap.add_argument("--mode", choices=("serve", "faithful"), default="serve")
    ap.add_argument("--strict-key", action="store_true")
    ap.add_argument("--t1", type=lambda x: int(x, 0), default=None,
                    help="override advertised A1 T1 timing byte (default 0x%02X, "
                         "bench-tuned; real ECU is 0x8A=100ms)" % Tp20Ecu.A1_PARAMS[1])
    ap.add_argument("--log")
    args = ap.parse_args(argv)
    logf = open(args.log, "w", encoding="ascii") if args.log else None

    def log(line: str):
        print(line, flush=True)
        if logf:
            logf.write(line + "\n"); logf.flush()

    prof = aisin09d_profile()
    image = load_image(args.image)
    if args.image:
        log("image: %s (%d bytes)" % (args.image, len(image)))
        _warn = image_health(image)
        if _warn:
            log(_warn)
    pt = _j.PassThru(args.dll)
    ch = pt.connect(_j.CAN, args.baud)
    _es.bus_check(ch, log=log)
    _es.set_rx_filters(ch, [_SETUP_ID, args.ecu_rx], log=log)
    a1 = None
    if args.t1 is not None:
        a1 = bytes([Tp20Ecu.A1_PARAMS[0], args.t1 & 0xFF]) + Tp20Ecu.A1_PARAMS[2:]
        log("A1 timing override: T1 = 0x%02X" % (args.t1 & 0xFF))
    io = Tp20Ecu(ch, dest=args.dest, ecu_rx_id=args.ecu_rx, log=log, a1_params=a1)
    responder = Trx09dResponder(prof, image=image, read_mode=args.mode,
                                strict_key=args.strict_key, log=log)
    sim = Simulator(io, responder, log=log)
    try:
        sim.run()
    except KeyboardInterrupt:
        log("stopped (%d requests handled)" % sim.count)
    finally:
        try:
            ch.disconnect()
        except Exception:
            pass


if __name__ == "__main__":   # pragma: no cover
    main()
