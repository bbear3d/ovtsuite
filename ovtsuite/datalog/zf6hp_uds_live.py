# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Live data from a ZF 6HP (GS19) transmission over UDS/TP2.0 -- the 6HP28.

The 6HP28 does **not** serve the ``readDataByLocalIdentifier (0x21)`` measuring
blocks the 6HP19 logger (``datalog.gui_log`` "ZF6HP" + ``zf6hp.msblock6hp``)
reads.  Its live data is a single ``readDataByIdentifier`` block, DID ``04A1``,
carrying every signal at once.  The record layout below was read straight off
the car's own diagnostic session (``Reference/Logs/VCDS-UDS-Trans.log``), not
from any label database:

  * TP2.0 channel to logical address 0x02 (the transmission), then the tester
    polls ``22 04 A1`` in the default session -- no StartDiagnosticSession is
    needed (the capture's ``1A 9F`` identification request NAKs and the tester
    simply goes on to the RDBI poll).
  * The positive response is ``62 04 A1`` followed by a 164-byte block = **41
    fixed-width records of 4 bytes each**::

        +--------+-----------+--------+---------------+
        | raw(1) | signal(1) | sub(1) | scale-type(1) |
        +--------+-----------+--------+---------------+

    ``signal`` is the controller's own signal id (0x01..0x5A), one per record;
    ``raw`` is the current one-byte reading; ``sub`` in {0x00,0x01,0x05} and
    ``scale-type`` in {0x00,0x01,0x02,0x03,0x05,0x0d} together select the
    physical scaling.

This module reads the block **raw**: every record becomes a channel showing its
one-byte reading, labelled by signal id and scale type.  The raw-to-physical
scale factor per (signal, scale-type) is *not* guessed here -- a single
stationary capture cannot pin it.  It is meant to be calibrated from one short
driving log with known RPM / road-speed / temperature anchors, after which
``SCALE`` below is filled in and the labels in ``SIGNAL_NAMES`` confirmed.
Until then the logger produces exactly the capture needed to do that: correct
raw counts now, named physical units once the drive settles them.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..flash import diag as _diag
from ..flash import kwp as _kwp
from ..flash.diag import KLineError, KLineTimeout
from .logfile import Channel

#: DID of the all-in-one measuring block.
BLOCK_DID = 0x04A1
REC_LEN = 4
#: Synthetic measuring-block number the single 04A1 block is presented under,
#: so the existing group/cell logger machinery can carry it unchanged.  0xA1
#: echoes the DID's low byte; nothing in the protocol uses it.
GROUP = 0xA1


@dataclass
class MeasRec:
    """One 4-byte record out of the 04A1 block."""
    signal_id: int
    sub: int
    scale_type: int
    raw: int


def decode_block(body: bytes) -> List[MeasRec]:
    """Split a 04A1 block body (the bytes after the ``62 04 A1`` echo) into its
    fixed-width records.  A short tail that is not a whole record is ignored
    rather than raised -- a truncated read should still yield the records that
    did arrive."""
    out: List[MeasRec] = []
    data = bytes(body)
    for i in range(0, len(data) - REC_LEN + 1, REC_LEN):
        r = data[i:i + REC_LEN]
        out.append(MeasRec(signal_id=r[1], sub=r[2], scale_type=r[3], raw=r[0]))
    return out


# --------------------------------------------------------------- signal names
#: Signal id -> short human name, as far as the stationary capture allows.
#: Deliberately empty of guesses for now so no wrong label is presented as
#: fact; unknown ids fall back to ``signal 0xNN``.  Filled once a driving
#: capture confirms which id tracks RPM, road speed, ATF temperature, etc.
SIGNAL_NAMES: Dict[int, str] = {}

#: (signal_id, scale_type) -> (scale, offset, unit): physical = raw*scale+offset.
#: Empty until calibrated from a driving log; absence means "report raw count".
SCALE: Dict[Tuple[int, int], Tuple[float, float, str]] = {}

#: Canonical record order as the 6HP28 firmware emits it (from the 04A1
#: capture).  Used only to give the browse list and the log columns a stable
#: order and stable field indices; the live read still labels each cell by the
#: signal id it actually carried, so a car that differs logs correct raw values
#: under a possibly-different label rather than silently mis-ordered.
BLOCK_ORDER: List[int] = [
    0x1f, 0x31, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x35, 0x43, 0x3a,
    0x2c, 0x2a, 0x49, 0x34, 0x30, 0x29, 0x07, 0x2f, 0x19, 0x03, 0x13, 0x1e,
    0x1d, 0x0a, 0x05, 0x01, 0x15, 0x02, 0x06, 0x0c, 0x0b, 0x1a, 0x1c, 0x52,
    0x5a, 0x53, 0x16, 0x3b, 0x54,
]

#: signal id -> (sub, scale-type) as emitted in the 04A1 block (from the car
#: capture).  Fixed per signal by firmware, so it labels the log column up
#: front -- a raw capture then records signal id AND its scale type, which is
#: what a later calibration pass needs to turn raw counts into physical units.
SIG_TYPE: Dict[int, Tuple[int, int]] = {
    0x01: (5, 0x00), 0x02: (1, 0x00), 0x03: (1, 0x03), 0x05: (1, 0x00),
    0x06: (1, 0x00), 0x07: (1, 0x03), 0x0a: (0, 0x03), 0x0b: (0, 0x05),
    0x0c: (0, 0x00), 0x13: (1, 0x03), 0x15: (0, 0x00), 0x16: (0, 0x05),
    0x19: (1, 0x03), 0x1a: (0, 0x05), 0x1c: (0, 0x05), 0x1d: (0, 0x03),
    0x1e: (0, 0x03), 0x1f: (5, 0x0d), 0x20: (5, 0x01), 0x21: (1, 0x01),
    0x22: (5, 0x01), 0x23: (5, 0x01), 0x24: (5, 0x01), 0x25: (5, 0x01),
    0x26: (1, 0x01), 0x29: (0, 0x01), 0x2a: (1, 0x01), 0x2c: (1, 0x01),
    0x2f: (0, 0x03), 0x30: (0, 0x01), 0x31: (1, 0x01), 0x34: (1, 0x01),
    0x35: (0, 0x01), 0x3a: (0, 0x01), 0x3b: (0, 0x01), 0x43: (0, 0x01),
    0x49: (0, 0x01), 0x52: (5, 0x02), 0x53: (5, 0x02), 0x54: (1, 0x02),
    0x5a: (1, 0x02),
}


def scale_value(signal_id: int, scale_type: int, raw: int) -> Tuple[Optional[float], str]:
    """(value, unit): the calibrated physical value when known, else the raw
    count with no unit.  Raw is always a real number so strip charts plot and
    the log file stores a column either way."""
    sc = SCALE.get((signal_id, scale_type))
    if sc is None:
        return float(raw), ""
    scale, offset, unit = sc
    return raw * scale + offset, unit


# ------------------------------------------------------------------- one cell
class LiveCell:
    """A decoded record dressed as the ``diag.Cell`` the logger reads: the loop
    only ever touches ``.text`` and ``.value``."""
    __slots__ = ("signal_id", "sub", "scale_type", "raw", "_value", "_unit")

    def __init__(self, rec: "MeasRec"):
        self.signal_id = rec.signal_id
        self.sub = rec.sub
        self.scale_type = rec.scale_type
        self.raw = rec.raw
        self._value, self._unit = scale_value(rec.signal_id, rec.scale_type, rec.raw)

    @property
    def value(self) -> Optional[float]:
        return self._value

    @property
    def unit(self) -> str:
        return self._unit

    @property
    def text(self) -> str:
        if self._unit:
            return "%g %s" % (self._value, self._unit)
        return "%g" % self._value


# ---------------------------------------------------------------- the session
class Zf6hpUdsLiveSession(_diag.DiagSession):
    """A :class:`diag.DiagSession` that reads the 6HP28's 04A1 block.

    Only the three things that differ from the 6HP19 TCU path are overridden:
    the connect skips the measuring-block session setup and goes straight to
    the RDBI poll the car itself uses; ``read_group`` issues ``22 04 A1`` and
    decodes the 41 records; and memory reads are declared unsupported so the
    logger's AST probe does not run against a transmission.  Keep-alive, close
    and reconnect are inherited unchanged -- they already drive ``self.kwp``.
    """
    #: read by gui_log._probe_memory to skip the readMemoryByAddress probe.
    supports_memory_read = False
    #: ChannelNamer reads this; no binary family is involved.
    family = None

    def connect(self, speed="Low", cap_high=False, is_stop=None):
        self._speed, self._cap = speed, cap_high
        self.kw = None
        self.baud = 0
        # Open the TP 2.0 channel if the link built by the logger has not yet
        # (mirrors diag._connect_tcu: a bridge TP handle is lazy, a local
        # Tp20Channel starts closed).
        tp = getattr(self.k, "tp", None)
        if tp is not None and not getattr(tp, "opened", True):
            self.log("Opening the TP 2.0 channel to the transmission...")
            self.k.wake_up()
        d = _kwp.Kwp2000(self.k, 0x10, self.log)
        d.P3, d.P4 = 0.025, 0.0
        d.fast = True
        self.kwp = d
        self.identity = self._read_identity() or "ZF 6HP (GS19) transmission"
        self.log("Transmission: %s" % self.identity)
        # Prove the block answers before the dialog says "connected".
        self.read_block()
        self.baud = 500000
        return self.baud

    def _read_identity(self) -> str:
        """VW spare-part (F187) and hardware (F191) numbers, when served."""
        parts = []
        for did in (0xF187, 0xF191):
            try:
                body = self.kwp.send_receive(0x22, bytes([did >> 8, did & 0xFF])).body
            except (_kwp.NegativeResponse, KLineError, KLineTimeout):
                continue
            if len(body) >= 2 and body[0] == (did >> 8) and body[1] == (did & 0xFF):
                body = body[2:]
            txt = body.decode("latin-1").strip("\x00 ").strip()
            if txt:
                parts.append(txt)
        return " ".join(parts)

    def read_block(self) -> bytes:
        """The 04A1 block body (the 164 bytes after the ``62 04 A1`` echo)."""
        msg = self.kwp.send_receive(0x22, bytes([BLOCK_DID >> 8, BLOCK_DID & 0xFF]))
        body = bytes(msg.body)
        if len(body) >= 2 and body[0] == (BLOCK_DID >> 8) and body[1] == (BLOCK_DID & 0xFF):
            body = body[2:]
        return body

    def read_group(self, group: int) -> Optional[List["LiveCell"]]:
        try:
            body = self.read_block()
        except _kwp.NegativeResponse:
            return None
        return [LiveCell(r) for r in decode_block(body)]

    def read_groups(self, groups: Sequence[int]) -> Dict[int, Optional[List["LiveCell"]]]:
        # One block read serves every channel; they all live in GROUP.
        cells = self.read_group(GROUP)
        return {g: cells for g in groups}

    def read_memory(self, addr, length, addr_bytes=3):
        return None


# ------------------------------------------------------------------- the namer
class Blocks6hpUds:
    """Names the 04A1 block's cells for the logger, the way ``Blocks6hp`` names
    the 6HP19 groups -- the same small interface (``available`` / ``slots`` /
    ``resolve`` / ``channel`` / ``populated_groups`` / ``group_summary``) so
    ``gui_log`` can hold it without special-casing."""
    session = None

    @property
    def available(self) -> bool:
        return True

    def slots(self, group: int) -> int:
        return len(BLOCK_ORDER)

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        if not 0 <= field < len(BLOCK_ORDER):
            return ("Cell %d" % (field + 1), "", "")
        sid = BLOCK_ORDER[field]
        name = SIGNAL_NAMES.get(sid)
        key = "zf6hp_%02x" % sid
        st = SIG_TYPE.get(sid)
        tag = " [t%X]" % st[1] if st else ""
        label = (("%s (0x%02X%s)" % (name, sid, tag)) if name
                 else ("6HP signal 0x%02X%s" % (sid, tag)))
        return label, key, ""

    def channel(self, group: int, field: int) -> Channel:
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def populated_groups(self) -> List[int]:
        return [GROUP]

    def group_summary(self, group: int) -> str:
        return "6HP28 live data (%d signals)" % len(BLOCK_ORDER)
