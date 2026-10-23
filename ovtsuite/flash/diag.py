# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Diagnostic session for live data and fault codes over K-line.

Ported from KWHack's ``Tester.Kwp2000Diag`` / ``LiveDataLoggingViewModel``:

EDC16 (KWP2000): 5-baud slow init at 10400 -> ``startDiagnosticSession 89``
(the ECU answers readEcuIdentification in the default state but ignores
measuring-block reads until a session is open) -> optional link-speed raise
``10 89 <speed>`` with the flashing speed bytes (0x50 = 38400, 0x87 = 124800,
0xA7 = 244898), ack-gated and validated with one group read (reverted on
silence; CH340 cables are capped at 124800) -> measuring blocks via
``readDataByLocalIdentifier 21 <group>`` whose body is the KW1281 3-byte
(formula, A, B) cell stream padded to 8 cells with ``25 00 00`` -> fault codes
``18 02 FF 00`` (count, then DTC-hi/DTC-lo/status; the DTC word is the
5-digit VAG code) / ``14 FF 00`` to clear -> ``3E`` testerPresent keep-alive.

EDC15 (KW1281): 5-baud init to 0x01 (odd parity) at 9600 -> identification
blocks (ASCII ident + coding/WSC block) -> ``29 <group>`` GroupRead answered
by ``E7`` (same cell triples; anything else, NAK included, means the group is
unavailable) -> ``07`` fault codes answered by ``FC`` (3-byte records,
``FF FF 88`` = none) / ``05`` to clear -> ACK keep-alive. 9600 is the only
rate: every EDC15/MSA15 image's KW1281 code loads S0BG = 0x33 (500 kHz / 52
= 9615 baud at 16 MHz) and the calibration has no baud for it -- 10400
(S0BG 0x2F) belongs to the separate 5-baud responder that answers with the
KWP2000 key bytes 6B 8F. Its KWP2000 handler (every image that has one, VM+
through late P+) serves startCommunication, startDiagnosticSession 85/89 and
the OBD modes 01-04/06/07/09 and nothing else -- no 21, so no measuring
blocks. So EDC15 is
KW1281 only, and a KWP2000 answer is reported, not followed. There is no
link-speed switch in KW1281; the logger uses KWHack's fast KW1281 timing. On
a J2534 device KW1281 needs the byte-stream link
:class:`~ovtsuite.flash.kline_j2534_raw.J2534RawKLine` (the logger's
``kline_over_j2534`` picks it).

Cells are decoded with the standard VAG measuring-block formula table
(blafusel.de KW1281 formulas, as in KWHack's ``SensorValue``) into a numeric
value plus unit; the raw triple is kept for the log.
"""
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import kwp as _kwp
from .transport import KLineError, KLineTimeout, LinkStopped

SPEEDS = {"Low": (0x00, 10400), "Medium": (0x87, 124800), "High": (0xA7, 244898)}
#: The only rate EDC15 KW1281 runs at (S0BG 0x33, hard-coded; see above).
EDC15_BAUD = 9600
#: Sync bytes a 9600-baud read makes of the EDC15's 10400-baud KWP2000 reply
#: -- what the flasher's soft-brick check keys on (flash/edc15.py).
EDC15_KWP_SYNC_AT_9600 = (0x95, 0xB5)
BT_ASCII = 0xF6                 # KW1281 identification / ASCII data block
BT_GROUP_READ_RESPONSE = 0xE7
PAD_FORMULA = 0x25          # unused formula id the ECU pads fixed 8-cell records with
CH34X_VID = 0x1A86


class StopRequested(Exception):
    pass


#: Read timeout while a session closes: stopCommunication is one frame, and a
#: Disconnect must not sit out the 8 s connect timeout waiting for its echo.
CLOSE_READ_TIMEOUT = 0.5


# ---------------------------------------------------------------- formulas
def _deg_tdc(a, b, atdc_if_high):
    d = abs(b - 127) * 0.01 * a
    late = (b > 127) if atdc_if_high else (b < 128)
    return d, "°" + ("ATDC" if late else "BTDC")


def decode_cell(fid: int, a: int, b: int):
    """(value, unit, text). value is None for non-numeric formulas (bit
    fields, text, states); text is always the display string."""
    f = fid
    if f == PAD_FORMULA and a == 0 and b == 0:
        # An EDC16 group record is always eight slots; an unused one is sent
        # as 25 00 00. Trailing ones are dropped by parse_cells, but one in
        # the middle (a group whose own row is empty and whose +127 row is
        # not) keeps its place so the cells after it keep their numbers.
        return None, "", "(empty)"
    if f == 1:
        v, u = 0.2 * a * b, "rpm"
    elif f == 2:
        v, u = a * 0.002 * b, "%"
    elif f == 3:
        v, u = 0.002 * a * b, "Deg"
    elif f == 4:
        v, u = _deg_tdc(a, b, True)
    elif f == 5:
        v, u = a * (b - 100) * 0.1, "°C"
    elif f == 6:
        v, u = 0.001 * a * b, "V"
    elif f == 7:
        v, u = 0.01 * a * b, "km/h"
    elif f == 8:
        v, u = 0.1 * a * b, ""
    elif f == 9:
        v, u = (b - 127) * 0.02 * a, "Deg"
    elif f == 10:
        return None, "", ("Cold" if b == 0 else "Warm")
    elif f == 11:
        v, u = 0.0001 * a * (b - 128) + 1, ""
    elif f == 12:
        v, u = 0.001 * a * b, "Ω"
    elif f == 13:
        v, u = (b - 127) * 0.001 * a, "mm"
    elif f == 14:
        v, u = 0.005 * a * b, "bar"
    elif f == 15:
        v, u = 0.01 * a * b, "ms"
    elif f == 16:
        return None, "", "%s %s" % (format(a, "08b"), format(b, "08b"))
    elif f == 17:
        # Two ASCII characters. The 6HP19 reports its engaged gear this way
        # ("3 ", "R "): the text is the characters without padding, and when
        # they spell a whole number that number is the value, so it charts.
        txt = (chr(a) + chr(b)).strip()
        return (float(txt) if txt.isdigit() else None), "", txt
    elif f == 18:
        v, u = 0.04 * a * b, "mbar"
    elif f == 19:
        v, u = a * b * 0.01, "l"
    elif f == 20:
        v, u = a * (b - 128) / 128.0, "%"
    elif f == 21:
        v, u = 0.001 * a * b, "V"
    elif f == 22:
        v, u = 0.001 * a * b, "ms"
    elif f == 23:
        v, u = b / 256.0 * a, "%"
    elif f == 24:
        v, u = 0.001 * a * b, "A"
    elif f == 25:
        v, u = b * 1.421 + a / 182.0, "g/s"
    elif f == 26:
        v, u = b - a, "C"
    elif f == 27:
        v, u = _deg_tdc(a, b, False)
    elif f == 28:
        v, u = b - a, ""
    elif f == 29:
        return None, "", ("Map 1" if b < a else "Map 2")
    elif f == 30:
        v, u = b / 12.0 * a, "Deg k/w"
    elif f == 31:
        v, u = b / 2560.0 * a, ""
    elif f == 32:
        v, u = (b - 256 if b > 128 else b), ""
    elif f == 33:
        v, u = (100.0 * b if a == 0 else 100.0 * b / a), "%"
    elif f == 34:
        v, u = (b - 128) * 0.01 * a, "kW"
    elif f == 35:
        v, u = 0.01 * a * b, "l/h"
    elif f == 36:
        v, u = a * 2560 + b * 10, "km"
    elif f == 38:
        v, u = (b - 128) * 0.001 * a, "Deg k/w"
    elif f == 39:
        # VAG tools print these three "mg/H": H is Hub, the stroke -- an
        # injected (or air) quantity per stroke, never milligrams per hour.
        v, u = b / 256.0 * a, "mg/stroke"
    elif f == 40:
        v, u = b * 0.1 + 25.5 * a - 400, "A"
    elif f == 41:
        v, u = b + a * 255, "Ah"
    elif f == 42:
        v, u = b * 0.1 + 25.5 * a - 400, "Kw"
    elif f == 43:
        v, u = b * 0.1 + 25.5 * a, "V"
    elif f == 44:
        return None, "", "%02d:%02d" % (a, b)
    elif f == 45:
        v, u = 0.1 * a * b / 100.0, ""
    elif f == 46:
        v, u = (a * b - 3200) * 0.0027, "Deg k/w"
    elif f == 47:
        v, u = (b - 128) * a, "ms"
    elif f == 48:
        v, u = b + a * 255, ""
    elif f == 49:
        v, u = b / 4.0 * a * 0.1, "mg/stroke"
    elif f == 50:
        v, u = ((b - 128) / 0.01 if a == 0 else (b - 128) / (0.01 * a)), "mbar"
    elif f == 51:
        v, u = ((b - 128) / 255.0) * a, "mg/stroke"
    elif f == 52:
        v, u = b * 0.02 * a - a, "Nm"
    elif f == 53:
        v, u = (b - 128) * 1.4222 + 0.006 * a, "g/s"
    elif f == 54:
        v, u = a * 256 + b, ""
    elif f == 55:
        v, u = a * b / 200.0, "s"
    elif f == 56:
        v, u = a * 256 + b, "WSC"
    elif f == 57:
        v, u = a * 256 + b + 65536, "WSC"
    elif f == 58:
        v, u = (1.0225 * (256 - b) if b > 128 else 1.0225 * b), "/s"
    elif f == 59:
        v, u = (a * 256 + b) / 32768.0, ""
    elif f == 60:
        v, u = (a * 256 + b) * 0.01, "sec"
    elif f == 61:
        v, u = ((b - 128) if a == 0 else (b - 128) / a), ""
    elif f == 62:
        v, u = 0.256 * a * b, "S"
    elif f == 63:
        return None, "", '"%s%s"?' % (chr(a), chr(b))
    elif f == 64:
        v, u = a + b, "Ω"
    elif f == 65:
        v, u = 0.01 * a * (b - 127), "mm"
    elif f == 66:
        v, u = (a * b) / 511.12, "V"
    elif f == 67:
        v, u = 640 * a + b * 2.5, "Deg"
    elif f == 68:
        v, u = (256 * a + b) / 7.365, "deg/s"
    elif f == 69:
        v, u = (256 * a + b) * 0.3254, "Bar"
    elif f == 70:
        v, u = (256 * a + b) * 0.192, "m/s²"
    elif f == 177:
        # 16-bit offset-binary spanning BOTH operand bytes, value in Nm:
        #   v = (a << 8 | b) - 0x8000
        # Unlike every formula above it, a and b are not independent operands --
        # on the GS19's group 9 field 0, a only ever took 127/128/129 while b ran
        # the full 0..255, which is one 16-bit quantity centred on 0x8000, not two
        # scalars. Read that way it correlates with pedal at r = +0.970 over 3776
        # samples and spans -51..+300 Nm: negative in overrun, +288 mean at 60-70%
        # pedal, against a BHW's ~320 Nm. Read as separate operands, b alone
        # correlates at -0.051 -- i.e. not at all.
        #
        # The structural confirmation is the intervention maps: KF_MEHAUPT*'s
        # input-torque axis is in Nm and spans -78..+300, the same physical
        # quantity over the same range as this channel. NOTE it was derived from
        # logged data plus that axis agreement, NOT from the controller's own
        # formula code -- the TCU's program flash is unreadable (every area
        # outside the calibration window refuses a read), so the usual route was
        # not available.
        v, u = (a << 8 | b) - 0x8000, "Nm"
    else:
        return None, "", "(%d %d %d)" % (fid, a, b)
    txt = ("%g" % v) if f in (1, 7, 36, 41, 48, 54, 56, 57, 177) else ("%.1f" % v)
    return float(v), u, (txt + (" " + u if u else "")).strip()


@dataclass
class Cell:
    fid: int
    a: int
    b: int

    @property
    def decoded(self):
        return decode_cell(self.fid, self.a, self.b)

    @property
    def value(self) -> Optional[float]:
        return self.decoded[0]

    @property
    def unit(self) -> str:
        return self.decoded[1]

    @property
    def text(self) -> str:
        return self.decoded[2]


def parse_cells(body: bytes, drop_echo: bool = True) -> List[Cell]:
    data = bytes(body)
    if drop_echo and len(data) % 3 == 1:
        data = data[1:]
    cells = [Cell(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data) - 2, 3)]
    while cells and cells[-1].fid == PAD_FORMULA:
        cells.pop()
    return cells


@dataclass
class Dtc:
    code: int                 # 5-digit VAG code (KWP2000 DTC word / KW1281 record)
    status: int

    @property
    def text(self):
        return "%05d - %02d-%02d" % (self.code, self.status & 0x7F, (self.status >> 7) * 10)


def cable_is_ch34x(port_name: str) -> bool:
    """CH340/CH341 USB adapters can't hold the ~245 kbaud High rate."""
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port_name:
                return (p.vid == CH34X_VID) or ("CH34" in (p.description or "").upper())
    except Exception:
        pass
    return False


def kw1281_identity(blocks: Sequence[bytes]) -> Tuple[str, Optional[int], Optional[int]]:
    """``(text, coding, workshop code)`` from a KW1281 wake-up's blocks.

    The text is the ASCII identification blocks only, as KWHack's
    ``ControllerInfo.AsciiText`` has it: one string the ECU cut into
    fixed-length blocks (so pieces are joined with nothing between them, then
    runs of spaces collapsed), each character's top bit dropped -- on the
    first byte it is the "more data via ReadIdent" flag, not a character.
    An ASCII block whose first data byte is 0x00 is not text but the
    coding/WSC block: ``(b4*256 + b5) / 2`` is the software coding and
    ``b6*256 + b7`` the workshop code, which overflows into the low bit of b5.
    """
    text = []
    coding = wsc = None
    for b in blocks:
        if len(b) < 4 or b[2] != BT_ASCII:
            continue
        if b[3] == 0x00:
            if len(b) >= 8:
                coding = (b[4] * 256 + b[5]) // 2
                wsc = b[6] * 256 + b[7] + (65536 if b[5] & 1 else 0)
            continue
        text.append("".join(chr(x & 0x7F) for x in b[3:]))
    return " ".join("".join(text).split()), coding, wsc


_EDC15_KWP_MODE = (
    "The ECU answered with %s instead of KW1281: that is the EDC15's "
    "KWP2000 mode, which serves only flash programming and generic OBD -- no "
    "measuring blocks. If it was "
    "just disconnected or flashed, switch the ignition off for a few seconds and "
    "connect again; if it keeps answering this way it is stuck in its boot "
    "loader -- recover it with the flasher.")


# ---------------------------------------------------------------- session
class DiagSession:
    """One diagnostic connection; drive it from a single worker thread."""

    def __init__(self, link, ecu="EDC16", log=None, fast_kw1281=False):
        self.k = link
        self.ecu = ecu
        self._log = log or (lambda s: None)
        self.kwp: Optional[_kwp.Kwp2000] = None
        self.kw: Optional[_kwp.Kw1281] = None
        self.baud = 0
        self.identity = ""
        self.ident_blocks: List[bytes] = []
        #: EDC15 KW1281 software coding / workshop code from the wake-up's
        #: coding block (None when the ECU sent none)
        self.coding: Optional[int] = None
        self.workshop_code: Optional[int] = None
        #: KW1281 fast timing (1 ms R6, no block lead) -- the logger's choice,
        #: as KWHack's live data always uses it
        self.fast_kw1281 = bool(fast_kw1281)

    def log(self, msg):
        self._log(msg)

    @property
    def is_kwp2000(self):
        return self.kwp is not None

    # ---- connect ----
    def connect(self, speed="Medium", cap_high=False, is_stop=None):
        self._speed, self._cap = speed, cap_high
        self.kwp = self.kw = None
        self._arm_stop(is_stop)
        if self.ecu == "EDC15":
            self._connect_kw1281(is_stop)
        elif self.ecu in ("ZF6HP", "Aisin TRx"):
            # Both are KWP2000-over-TP2.0 TCUs with no K-line: same connect
            # (session 0x86, ident 1A 9B, no security, no baud raise). The only
            # difference is the TP2.0 logical address, which rides on the link
            # the caller built (dest 0x02 for the Aisin 09D, 0x01-class for ZF).
            self._connect_tcu(is_stop)
        else:
            self._connect_kwp2000(speed, cap_high, is_stop)
        return self.baud

    def _arm_stop(self, is_stop):
        """Hand ``is_stop`` to every layer that waits or retries, so a stop
        lands within one frame wait rather than after a whole connect: the
        link's wake-up retries (K-line, J2534 K-line), the CAN link's
        responsePending wait, a local TP 2.0 channel's frame waits. A hook the
        owner of the link already installed is left alone -- the flasher sets
        its own on a channel it shares with this session."""
        self._is_stop = is_stop
        if is_stop is None:
            return
        for obj in (self.k, getattr(self.k, "tp", None)):
            if obj is not None and hasattr(obj, "is_stop") and obj.is_stop is None:
                try:
                    obj.is_stop = is_stop
                except Exception:
                    pass

    def _check_stop(self, is_stop=None):
        is_stop = is_stop if is_stop is not None else getattr(self, "_is_stop", None)
        if is_stop is not None and is_stop():
            raise StopRequested()

    def _new_kwp(self, controller=0x10):
        d = _kwp.Kwp2000(self.k, controller, self.log)
        d.is_stop = getattr(self, "_is_stop", None)
        return d

    def reconnect(self, is_stop=None):
        """Wake the ECU again after a dropout, with the same speed request."""
        self.k.clear_rx()
        return self.connect(getattr(self, "_speed", "Low"), getattr(self, "_cap", False), is_stop)

    def _connect_tcu(self, is_stop=None):
        """Open a diagnostic session on a ZF 6HP (GS19) TCU, over CAN/TP2.0.

        Three deliberate differences from :meth:`_connect_kwp2000`, all of
        them things the EDC16 path does that would be wrong here:

        * **No slow init.** The TCU has no K-line on any car this tool has
          seen (``flash/zf6hp.py``), so there is no 5-baud address to send.
          The TP 2.0 channel setup still has to happen, though -- see below.
        * **Session 0x86, not 0x89.** 0x86 is what the flash path already uses
          against this ECU and what it answers to.
        * **No baud raise.** ``_raise_baud`` is a K-line service; over TP2.0
          the link speed is the CAN bitrate and the request is meaningless.

        Whether the TCU then answers ``readDataByLocalIdentifier (0x21)`` is
        NOT assumed -- nothing in the definitions says it does. Use
        :meth:`probe_groups` to find out on the bench before relying on it.
        """
        self.baud = 0
        # Open the TP 2.0 channel if whoever built the link has not already.
        # This used to be assumed: the flash path constructs a Tp20Channel and
        # calls open() on it itself (``flash/zf6hp.py``), so by the time this
        # ran the channel was negotiated. Nothing guarantees that. The datalog
        # path hands us a freshly built CanKLine, and *every* BridgeTp20 is a
        # lazy handle that does not touch the device until open() -- so the
        # first request went out with no channel behind it:
        #   * 32-bit bridge: the bridge's own TP state is still None, and it
        #     answers "'NoneType' object has no attribute 'send'", which
        #     surfaced as "Could not connect: ..." in the logging dialog;
        #   * direct DLL / OpenPort: tx_id was never negotiated, so the frame
        #     went to CAN id None.
        # The EDC16 path never hit this only because _connect_kwp2000 calls
        # wake_up(), which opens the channel as a side effect. Do the same
        # here, but only when it is needed, so the flash path's already-open
        # channel is not torn down and re-negotiated underneath it.
        tp = getattr(self.k, "tp", None)
        if tp is not None and not getattr(tp, "opened", True):
            self.log("Opening the TP 2.0 channel to the TCU...")
            self._check_stop(is_stop)
            self.k.wake_up()            # CanKLine.wake_up: clears buffers, tp.open()
        d = self._new_kwp(0x10)
        d.P3, d.P4 = 0.025, 0.0
        d.fast = True
        self.kwp = d
        self._check_stop(is_stop)
        d.send_receive(0x10, b"\x86")
        self.log("TCU diagnostic session open (10 86).")
        self._check_stop(is_stop)
        try:
            rec = d.send_receive(0x1A, b"\x9B").body
            self.identity = format_identity(rec)
            self.log("TCU: %s" % self.identity)
        except (KLineError, KLineTimeout):
            self.identity = ""

    def probe_groups(self, first=1, last=0xFE, service=0x21):
        """Which measuring-block groups this ECU actually answers.

        Returns ``[(group, payload)]``. Written for the TCU, where the answer
        is genuinely unknown -- the ZF definitions name no measuring blocks,
        so the only way to find out what ``0x21`` yields is to ask. A negative
        response or a timeout means "not this group" and is skipped rather
        than raised, because most groups will not exist.
        """
        out = []
        for g in range(first, last + 1):
            try:
                rec = self.kwp.send_receive(service, bytes([g]))
            except (KLineError, KLineTimeout):
                continue
            body = getattr(rec, "body", b"")
            if body:
                out.append((g, bytes(body)))
                self.log("group %d: %s" % (g, body.hex(" ")))
        return out

    def _connect_kwp2000(self, speed, cap_high, is_stop=None):
        self.k.set_baud(10400)
        self.log("Connecting (slow init at 10400)...")
        self._check_stop(is_stop)
        version = self.k.wake_up(0x01, even_parity=False)
        if version < 2000:
            raise KLineError("The ECU answered with KW %d, not KWP2000" % version)
        self.baud = 10400
        d = self._new_kwp(0x10)                       # EDC16 physical address (83 10 F1 ...)
        d.P3, d.P4 = 0.025, 0.0          # fast timing: batched send, short gap
        d.fast = True
        self.kwp = d
        time.sleep(0.05)
        self._check_stop(is_stop)
        d.send_receive(0x10, b"\x89")
        self.log("Diagnostic session open (10 89).")
        self._check_stop(is_stop)
        try:
            rec = d.send_receive(0x1A, b"\x9B").body
            self.identity = format_identity(rec)
            self.log("ECU: %s" % self.identity)
        except LinkStopped:
            raise
        except (KLineError, KLineTimeout):
            self.identity = ""
        self._check_stop(is_stop)
        self._raise_baud(speed, cap_high)

    def _raise_baud(self, speed, cap_high):
        speed_byte, baud = SPEEDS.get(speed, SPEEDS["Medium"])
        if speed_byte == 0 or baud <= self.baud:
            return
        if speed_byte == 0xA7 and cap_high:
            self.log("CH340/CH341 cable: High isn't reliable, using Medium (124800).")
            speed_byte, baud = SPEEDS["Medium"]
        d = self.kwp
        try:
            d.send_receive(0x10, bytes([0x89, speed_byte]))
        except (KLineError, KLineTimeout):
            self.log("ECU refused %d baud; staying at %d." % (baud, self.baud))
            return
        time.sleep(0.075)
        self.k.set_baud(baud)
        try:
            self.k.clear_rx()
            d.send_receive(0x21, b"\x01")            # prove it still answers
            self.baud = baud
            self.log("Link speed -> %d baud." % baud)
        except (KLineError, KLineTimeout):
            self.k.set_baud(10400)
            self.log("No answer at %d baud; reverted to 10400." % baud)

    def _connect_kw1281(self, is_stop):
        """Wake an EDC15 in KW1281: 5-baud address 0x01 with odd parity (the
        address it answers to is its calibration's ``xcwSGADR``), at 9600.

        A KWP2000 answer is refused with the reason. On an EDC15 that is the
        flash-programming responder (key bytes 6B 8F at 10400), not a
        diagnostic session: it does not serve measuring blocks. At 9600 its
        reply usually arrives as a 0x95/0xB5 sync byte instead, so that is
        recognised too.
        """
        self._check_stop(is_stop)
        self.k.set_baud(EDC15_BAUD)
        self.log("Connecting (KW1281, 5-baud init at %d)..." % EDC15_BAUD)
        try:
            version = self.k.wake_up(0x01, even_parity=False)
        except LinkStopped:
            raise
        except (KLineError, KLineTimeout) as ex:
            if getattr(self.k, "last_sync", -1) in EDC15_KWP_SYNC_AT_9600:
                got = "a 10400-baud reply (sync 0x%02X at 9600)" % self.k.last_sync
                raise KLineError(_EDC15_KWP_MODE % got) from ex
            raise
        if version >= 2000:
            raise KLineError(_EDC15_KWP_MODE % ("KWP2000 key bytes (KW %d)" % version))
        self._continue_kw1281(EDC15_BAUD)

    def _continue_kw1281(self, baud):
        kw = _kwp.Kw1281(self.k, self.log, fast=self.fast_kw1281)
        self.ident_blocks = kw.connect()
        self.kw = kw
        self.baud = baud
        self.identity, self.coding, self.workshop_code = kw1281_identity(self.ident_blocks)
        tail = ("  (coding %05d, WSC %05d)" % (self.coding, self.workshop_code)
                if self.coding is not None else "")
        self.log("Connected (KW1281) at %d: %s%s" % (baud, self.identity, tail))

    # ---- live data ----
    def read_group(self, group: int) -> Optional[List[Cell]]:
        """Cells of one measuring block, or None when the ECU has no such
        group (KWP2000 NAK / KW1281 NAK)."""
        if self.kwp is not None:
            try:
                msg = self.kwp.send_receive(0x21, bytes([group & 0xFF]))
            except _kwp.NegativeResponse:
                return None
            return parse_cells(msg.body)
        kw = self.kw
        kw.send_block([0x29, group & 0xFF])
        blk = kw.receive_block()
        if blk[2] != BT_GROUP_READ_RESPONSE:
            # NAK, or a block this reader does not decode (the 02 text form,
            # an ACK): the group is unavailable, as KWHack's GroupReadMany has
            # it. The dialog is still in step -- the block arrived whole with
            # the right counter -- so this is no reason to drop the session.
            return None
        return parse_cells(blk[3:], drop_echo=False)

    def read_memory(self, addr: int, length: int, addr_bytes: int = 3):
        """``readMemoryByAddress (0x23)``: raw RAM, bypassing the measuring
        blocks entirely.

        Why this exists: nineteen of the AST (gearbox speed-request) signals
        in BPW.a2l have an ECU_ADDRESS but **no measuring descriptor**, so
        ``tools/kwp_find_signal.py`` reports every one of them as
        address-read only. They cannot be put in a group without patching
        code, which makes 0x23 the only route to the whole chain -- the
        requested synchronisation speed, its clamped value, the controller
        deviation and the intervention torque.

        Returns the bytes, or ``None`` when the ECU refuses the service or
        the address. A refusal is an ordinary answer here, not an error:
        plenty of EDC16 calibrations implement 0x23 only for the flash
        region, or not at all. KW1281 has no equivalent, so this is
        KWP2000-only.
        """
        if self.kwp is None:
            return None
        if addr_bytes not in (2, 3, 4):
            raise ValueError("addr_bytes must be 2, 3 or 4")
        if not 1 <= length <= 0xFF:
            raise ValueError("length must be 1..255")
        if not 0 <= addr < (1 << (8 * addr_bytes)):
            # The probe walks 3, 4 then 2 bytes looking for a width the ECU
            # accepts, and every AST address is above 0x7F0000, so width 2
            # is always reached and must fail readably rather than with an
            # OverflowError out of to_bytes.
            raise ValueError("0x%X does not fit in %d address byte(s)" % (addr, addr_bytes))
        req = addr.to_bytes(addr_bytes, "big") + bytes([length])
        try:
            msg = self.kwp.send_receive(0x23, req)
        except (_kwp.NegativeResponse, KLineError):
            return None
        body = msg.body
        # Some stacks echo the address back ahead of the data; trust the
        # length the caller asked for and take it from the end when the
        # reply is longer, rather than guessing an echo width.
        if len(body) > length:
            body = body[-length:]
        return body if len(body) == length else None

    def read_groups(self, groups: Sequence[int],
                    is_stop=None) -> Dict[int, Optional[List[Cell]]]:
        """Every group in ``groups``. ``is_stop`` (default: the one given to
        :meth:`connect`) is checked between groups, so a logger reading a
        dozen blocks per row stops within one block, not one row."""
        out = {}
        for g in groups:
            self._check_stop(is_stop)
            out[g] = self.read_group(g)
        return out

    def keepalive(self):
        if self.kwp is not None:
            self.kwp.send_receive(0x3E, b"")
        else:
            self.kw.send_block([_kwp.BT_ACK])
            self.kw.receive_block()

    # ---- fault codes ----
    def read_dtcs(self) -> List[Dtc]:
        if self.kwp is not None:
            body = self.kwp.send_receive(0x18, b"\x02\xFF\x00").body
            return _parse_dtcs(body, counted=True)
        kw = self.kw
        kw.send_block([0x07])
        out = []
        while True:
            blk = kw.receive_block()
            if blk[2] in (_kwp.BT_ACK, _kwp.BT_NAK):
                break
            if blk[2] == 0xFC:
                out += _parse_dtcs(blk[3:], counted=False)
            kw.send_block([_kwp.BT_ACK])
        return out

    def clear_dtcs(self):
        if self.kwp is not None:
            self.kwp.send_receive(0x14, b"\xFF\x00")
            return
        kw = self.kw
        kw.send_block([0x05])
        while True:
            blk = kw.receive_block()
            if blk[2] in (_kwp.BT_ACK, _kwp.BT_NAK):
                break
            kw.send_block([_kwp.BT_ACK])

    # ---- teardown ----
    def close(self):
        """End the session, briefly. The read timeout drops to
        :data:`CLOSE_READ_TIMEOUT` first: K-line stopCommunication only waits
        for its own echo, and a dead line must not hold a Disconnect for the
        connect-time timeout. Over TP 2.0 stopCommunication is skipped -- it
        is a full request/response there, the ECU may be the reason we are
        closing, and the channel disconnect (A8) the link sends on close ends
        the session anyway."""
        try:
            k = self.k
            if getattr(k, "read_timeout", None) is not None:
                k.read_timeout = min(k.read_timeout, CLOSE_READ_TIMEOUT)
        except Exception:
            pass
        try:
            if self.kwp is not None:
                if not getattr(self.k, "is_tp20", False):
                    self.kwp.send(0x82, b"")           # stopCommunication
            elif self.kw is not None:
                self.kw.end_communication()
        except Exception:
            pass
        self.kwp = None
        self.kw = None


def _parse_dtcs(body: bytes, counted: bool) -> List[Dtc]:
    data = bytes(body)
    if counted and len(data) >= 1 and len(data) == 1 + data[0] * 3:
        data = data[1:]
    out = []
    for i in range(0, len(data) - 2, 3):
        code = (data[i] << 8) | data[i + 1]
        status = data[i + 2]
        if code == 0xFFFF and status == 0x88:          # KW1281 "no faults"
            continue
        if code == 0:
            continue
        out.append(Dtc(code, status))
    return out


def format_identity(record: bytes) -> str:
    """readEcuIdentification(9B): part#(12) SW(4) 03 coding(3) WSC(6) text."""
    def printable(chunk):
        s = "".join(chr(c) if 0x20 <= c < 0x7F else " " for c in chunk)
        return " ".join(s.split())
    rec = bytes(record)
    if len(rec) >= 27 and rec[0] == 0x9B:
        parts = [printable(rec[1:13])]
        sw = printable(rec[13:17])
        if sw:
            parts.append("SW " + sw)
        desc = printable(rec[27:])
        if desc:
            parts.append(desc)
        return "  ".join(parts)
    return printable(rec)
