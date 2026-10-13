# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line byte-stream framing for a passive serial sniffer.

A K-line is one half-duplex wire, so a second FTDI cable's RX sees every byte
both sides send, in order.  This module turns that timestamped byte stream
into frames:

* KWP2000 (ISO 14230): ``[fmt][dest][src][len?][service][data...][cks]`` --
  fmt bits 7-6 = 0b10/0b11 with address bytes, 0b00 without; low 6 bits are
  the length (service + data) or 0 when a length byte follows; the checksum
  is the 8-bit sum of everything before it.  A frame is accepted only when
  its checksum matches; otherwise the stream resyncs one byte later.
* the slow-init answer ``55 KB1 KB2`` followed by the tester's ~KB2 and the
  ECU's ~address is reported as an ``init`` event (the 5-baud pulse itself is
  not a UART byte and never appears).
* KW1281 (EDC15 at 9600/10400): every byte is answered with its complement
  by the other side, except the 0x03 block end; blocks are
  ``[len][counter][title][data...][03]``.  Framing strips the complements.

Link-speed changes are structural: ``10 <session> <speed>`` answered by
``50 <session> <speed>`` switches the ECU to the rate the speed byte encodes
per ISO 14230-2, ``(mantissa + 32) * 2^exponent * 200`` with exponent =
bits 7-5 and mantissa = bits 4-0 (0x50 -> 38400, 0x87 -> 124800, 0xA7 ->
249600).  :class:`Framer` reports the new rate so the sniffer can follow.
"""
from dataclasses import dataclass
from typing import List, Optional

GAP_S = 0.050              # silence that ends a partial frame


def speed_byte_to_baud(b: int) -> int:
    """ISO 14230-2 baud-rate identifier -> bit/s."""
    return ((b & 0x1F) + 32) * (1 << (b >> 5)) * 200


@dataclass
class KFrame:
    t: float
    kind: str                  # kwp | kw1281 | init | raw
    data: bytes                # kwp: header+body (no checksum); kw1281: block; raw: bytes
    ok: bool = True
    note: str = ""


def _kwp_parse(buf: bytes, i: int):
    """Try to parse a KWP2000 frame at buf[i]; (end, frame_bytes) or None
    when incomplete (-1) / invalid (None)."""
    n = len(buf)
    if i >= n:
        return -1, None
    fmt = buf[i]
    mode = fmt & 0xC0
    if mode == 0x40:
        return None, None                          # reserved address mode
    hdr = 3 if mode else 1
    ln = fmt & 0x3F
    pos = i + hdr
    if ln == 0:
        if pos >= n:
            return -1, None
        ln = buf[pos]
        if ln == 0:
            return None, None
        pos += 1
    end = pos + ln + 1                              # + checksum
    if end > n:
        return -1, None
    if (sum(buf[i:end - 1]) & 0xFF) != buf[end - 1]:
        return None, None
    if mode and (buf[i + 1] == 0 or buf[i + 2] == 0):
        return None, None                          # addresses are never 0
    return end, bytes(buf[i:end - 1])


class Framer:
    def __init__(self, mode: str = "auto"):
        self.mode = mode                            # auto | kwp | kw1281 | raw
        self.buf = bytearray()
        self.t_first = None
        self.t_last = None
        self.kw_counter = None
        self.pending_speed = None                   # (session, speed) awaiting 0x50
        self.new_baud: Optional[int] = None         # set when a switch was seen

    # ---- feed ----
    def feed(self, t: float, data: bytes) -> List[KFrame]:
        out: List[KFrame] = []
        if self.buf and self.t_last is not None and t - self.t_last > GAP_S:
            out += self._flush_raw()
        if not self.buf:
            self.t_first = t
        self.buf += data
        self.t_last = t
        out += self._drain()
        return out

    def idle(self, t: float) -> List[KFrame]:
        """Call when the line has been quiet: flushes an unfinishable tail."""
        if self.buf and self.t_last is not None and t - self.t_last > GAP_S:
            return self._flush_raw()
        return []

    def _flush_raw(self):
        fr = KFrame(self.t_first, "raw", bytes(self.buf), False, "unframed")
        self.buf = bytearray()
        return [fr]

    def _drain(self) -> List[KFrame]:
        out = []
        while self.buf:
            b = self.buf
            # slow-init answer: 55 KB1 KB2 ~KB2 [~addr]  (0x55 is never a KWP
            # format byte, so wait for the keyword; the idle flush drops a
            # lone 0x55 that nothing follows)
            if b[0] == 0x55 and len(b) < 4:
                break
            if b[0] == 0x55 and (b[1] & 0x80 or b[2] & 0x80 or b[1] in (0x01, 0x8A)):
                kb = (b[1] & 0x7F) | ((b[2] & 0x7F) << 7)
                take = 4 if b[3] == ((~b[2]) & 0xFF) else 3
                if kb >= 2000 and len(b) >= 5 and take == 4:
                    take = 5                        # ECU's ~address follows
                elif kb >= 2000 and take == 4 and len(b) < 5:
                    break
                fr = KFrame(self.t_first, "init", bytes(b[:take]), True,
                            "slow init: keyword %d (%s)" % (kb, "KWP2000" if kb >= 2000 else "KW1281"))
                del b[:take]
                self.kw_counter = None
                out.append(fr)
                continue
            if self.mode in ("auto", "kwp"):
                end, frame = _kwp_parse(bytes(b), 0)
                if end == -1:
                    break                           # need more bytes
                if end is not None:
                    fr = KFrame(self.t_first, "kwp", frame)
                    self._note_speed(frame, fr)
                    del b[:end]
                    out.append(fr)
                    continue
            if self.mode in ("auto", "kw1281"):
                r = self._kw1281(b)
                if r == -1:
                    break
                if r is not None:
                    out.append(r)
                    continue
            # nothing parses at this byte: emit it as raw and resync
            out.append(KFrame(self.t_first, "raw", bytes(b[:1]), False, "resync"))
            del b[0]
        if not self.buf:
            self.t_first = None
        return out

    def _kw1281(self, b: bytearray):
        """Strip complement echoes: byte, ~byte, byte, ~byte ... 03."""
        if len(b) < 2:
            return -1
        if b[1] != ((~b[0]) & 0xFF):
            return None
        length = b[0]
        if length < 3 or length > 40:
            return None
        # ``length`` bytes (len, counter, title, data) each echoed with its
        # complement, then the 0x03 block end, which is not acknowledged
        need = length * 2 + 1
        if len(b) < need:
            return -1
        vals = []
        for k in range(length):
            j = 2 * k
            if b[j + 1] != ((~b[j]) & 0xFF):
                return None
            vals.append(b[j])
        if b[2 * length] != 0x03:
            return None
        blk = bytes(vals)                           # len, counter, title, data...
        del b[:need]
        return KFrame(self.t_first, "kw1281", blk)

    def _note_speed(self, frame: bytes, fr: KFrame):
        hdr = 3 if frame[0] & 0xC0 else 1
        if (frame[hdr - 1] & 0x3F) == 0:
            hdr += 1
        body = frame[hdr:]
        if len(body) >= 3 and body[0] == 0x10:
            self.pending_speed = (body[1], body[2])
        elif len(body) >= 2 and body[0] == 0x50:
            speed = body[2] if len(body) >= 3 else (
                self.pending_speed[1] if self.pending_speed and self.pending_speed[0] == body[1] else None)
            if speed is not None and speed != 0:
                self.new_baud = speed_byte_to_baud(speed)
                fr.note = "link speed -> %d baud (speed byte 0x%02X)" % (self.new_baud, speed)
            self.pending_speed = None
