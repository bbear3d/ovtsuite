# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line (ISO 14230 / KWP2000) over a J2534 device.

A J2534 device (e.g. Scanmatik) performs the K-line init in hardware, then runs
the *exact same* KWP2000 flash/diagnostic routine the serial path uses -- because
it presents the same :class:`KLine` facade (``wake_up``/``read``/``write_*``/
``set_baud`` ...), just carried over a J2534 channel instead of a serial port.

Two init modes (``fast_init``):
  * FAST (default) -- ISO 14230 StartCommunication (service 0x81) over an
    ISO14230 channel; hardware fast-init pulse.
  * SLOW -- 5-baud slow init over an **ISO9141** channel with the KWP byte-timing
    ODIS uses (address 0x01 -> keybytes EF 8F on the P475).  Confirmed possible
    over J2534 from an ODIS K-line capture (2026-09-24); the whole flash then runs
    on that ISO9141 channel.  Selected by constructing with ``fast_init=False``
    (or via edc16._connect_slow, which sets ``_fast=False``).

Verified against the real MPPS K-line captures (Reference/Logs/mpps-can-k-*.log):
``81 10 F1 81`` -> ``83 F1 10 C1 EF 8F`` (fast init, keyword EF 8F), then idents,
``27 03/04`` security, ``10 86 <speed>`` session with a baud switch, then the
transfer at the higher baud.

The device is driven message-at-a-time: each written KWP frame is one ISO 14230
message out, the ECU's reply is one message in.  A KWP2000 responsePending
(``7F <svc> 78``) is absorbed here (the ECU sends it as its own message, then the
real reply as another) so it never desyncs the caller -- same reason CanKLine
does it over TP 2.0.
"""
import time
from typing import Optional

from . import j2534 as J

try:
    from .transport import KLineError, KLineTimeout
except Exception:                                   # pragma: no cover
    class KLineError(Exception):
        pass

    class KLineTimeout(KLineError):
        pass

# ISO 14230 StartCommunication request (fmt 0x81, tgt 0x10 engine, src 0xF1
# tester, service 0x81) + checksum -- what MPPS sends for fast init.
START_COMM = bytes([0x81, 0x10, 0xF1, 0x81])

# 5-baud slow-init address for VAG EDC16 K-line (ODIS: address 0x01 -> EF 8F).
SLOW_INIT_ADDR = 0x01

# KWP byte-timing ODIS sets on the ISO9141 channel before FIVE_BAUD_INIT, from
# the P475 K-line capture (config IDs 0x03, 0x0C, 0x0E-0x11, 0x13-0x15, 0x21).
# Names are the J2534-1 v04.04 ones; the logging shim used to print 0x0E-0x26
# one name too early (0x0E as "W0", 0x21 as "DATA_BITS" ...), which is how an
# earlier version came to send DATA_BITS (0x20) = 1 -- one UART data bit, and
# SMSTATUS_INVALID_RESPONSE from the device.
# Best-effort: every set is wrapped in try/except, so a device that rejects one
# still inits.
_SLOW_INIT_CONFIG = {
    "LOOPBACK": 1,
    "P4_MIN": 0x02,
    "W1": 0x1F4,          # 500 ms: max wait for the 0x55 sync after the address
    "W2": 0xE7, "W3": 0xE7,
    "W4": 0x1E,           # 30 ms: KB2 -> inverted KB2
    "TIDLE": 0x4B0,       # 1200 ms bus idle before the address byte
    "TINIL": 0x19,        # 25 ms (spec default)
    "TWUP": 0x32,         # 50 ms (spec default)
    "FIVE_BAUD_MOD": 0x01,  # tester sends inverted KB2, no inverted address back
}

# A J2534 device clocks the K-line UART from its OWN base clock via an integer/
# fractional divisor, so it can only produce a fixed grid of actual rates.  The
# ECU's High rate (speed byte 0xA7) is ~244898 -- which is 3_000_000/12.25, an
# EXACT FTDI sub-integer divisor, so an FTDI-based interface (what MPPS/KWHack use)
# hits it dead-on.  A device whose grid has no point inside the ECU's ~2% UART
# window can't talk at High no matter what baud we REQUEST: it snaps the request to
# its nearest divisor.  On a Scanmatik 2 Pro that snap lands just outside the
# window -- 244898/240000/250000 went silent, and a requested 245000 (only 0.04%
# off the ECU) came back BIT-CORRUPT, the signature of the device's actual clock
# sitting at the edge of tolerance.  So this is a per-DEVICE baud-generator limit,
# not a J2534-protocol one; on the SM2 Pro High is effectively unreachable and the
# EDC16 flow falls back to Medium (124800, a clean divisor that works).
#
# The list is therefore a SHORT set of distinct, plausible targets, not a fine
# sweep (fine steps are futile -- a coarse grid snaps 245000 and 245050 to the same
# divisor): the exact FTDI value first, then round rates a different device's grid
# might land on cleanly, widest last.  A device with a finer grid or an FTDI clock
# hits an early one; the SM2 Pro exhausts them fast and drops to Medium.  Discovery
# needs a FRESH fast init per candidate (a wrong rate corrupts a live session);
# edc16._establish_session drives that loop.
J2534_HIGH_BAUD_CANDIDATES = [
    244898,                     # exact FTDI/KWHack rate (3e6/12.25); FTDI devices hit it
    245000,                     # round; closest the SM2 Pro got (bit-corrupt = tolerance edge)
    244800, 245200,             # near neighbours for a slightly-offset grid
    244000, 246000,             # coarser round targets another device may snap onto cleanly
    240000, 250000,             # far round outliers (~2% off), last resort
]

# discovered high bauds cached for this app run, keyed by the session speed byte
# (so the fresh-init sweep runs once, not on every connect)
_DISCOVERED_HIGH_BAUD = {}


def _cks(frame: bytes) -> int:
    return sum(frame) & 0xFF


#: Length of a complete K-line KWP frame at the front of a buffer, or None
#: (shared with the bridge, which assembles replies the same way).
_complete_frame_len = J.kline_frame_len
kline_split = J.kline_split


def _kwp_payload(frame: bytes) -> bytes:
    """Extract service+data from a K-line frame (addressed 0x8x or address-less
    length-in-fmt / separate-length forms).  Tolerant of a checksum trailer."""
    if not frame:
        return b""
    fmt = frame[0]
    i = 3 if fmt & 0x80 else 1
    n = fmt & 0x3F
    if n == 0 and len(frame) > i:
        n = frame[i]
        i += 1
    return bytes(frame[i:i + n]) if n else bytes(frame[i:-1] or frame[i:])


def _is_response_pending(frame: bytes) -> bool:
    p = _kwp_payload(frame)
    return len(p) >= 3 and p[0] == 0x7F and p[2] == 0x78


def _version_from_keyword(resp: bytes) -> int:
    """KWP protocol version from a StartCommunication reply.  Floors at 0x2000 so
    the EDC16 flow (which only requires >= 2000) proceeds on any successful
    fast init, regardless of how the device surfaces the keyword bytes."""
    kw = b""
    if 0xC1 in resp:                                # ...C1 KW1 KW2...
        idx = resp.index(0xC1)
        kw = resp[idx + 1:idx + 3]
    elif len(resp) >= 2:
        kw = resp[-2:]
    if len(kw) == 2:
        version = ((kw[1] & 0x7F) << 7) + (kw[0] & 0x7F)
        if version >= 2000:
            return version
    return 0x2000


def _version_from_keybytes(resp: bytes) -> int:
    """Protocol version from a 5-baud init's key bytes. They come first (a
    device may pad the buffer after them, or put the 0x55 sync before), and
    KW1281 (01 8A) is reported as itself: it is no KWP2000 controller, and
    flooring it to 2000 sends the caller down the wrong path."""
    kb = bytes(resp)
    first = 1 if len(kb) >= 3 and kb[0] == 0x55 else 0
    if len(kb) - first >= 2:
        version = ((kb[first + 1] & 0x7F) << 7) + (kb[first] & 0x7F)
        if version == 1281 or version >= 2000:
            return version
    return _version_from_keyword(kb)


class J2534KLine:
    """K-line look-alike (the subset edc16.py / diag.py use) over a J2534
    ISO 14230 channel, connecting with hardware fast init."""

    is_kline = True

    def __init__(self, dev, baud: int = 10400, log=None, fast_init: bool = True,
                 allow_slow_fallback: bool = True, on_trace=None):
        # ``dev`` is an already-opened j2534.PassThru or BridgePassThru.
        self._dev = dev
        self._ch = None
        self._baud0 = baud
        self._log = log or (lambda s: None)
        self._trace = on_trace          # optional (dir, bytes) wire-log hook
        self._fast = fast_init
        self._allow_slow = allow_slow_fallback
        self.direct = True
        self._timeout = 8.0
        self._tx = bytearray()          # bytes written, not yet a whole frame
        self._rx = bytearray()          # bytes ready to be read back
        self._carry = bytearray()       # received after a reply frame: the next one's
        self.last_sync = 0x55
        self._ch_proto = None           # protocol the current channel is open on
        self.is_stop = None             # optional callable()->bool; init retries give up
        self.init_is_stop = None        # the same, for the init retries only (the flasher)

    # -- channel --------------------------------------------------------------
    def _connect_channel(self):
        """Open the J2534 channel for the CURRENT init mode.

        FAST: ISO14230 (hardware fast-init pulse).  SLOW: ISO9141 with the KWP
        byte-timing ODIS uses for 5-baud init -- some stacks only perform
        FIVE_BAUD_INIT on an ISO9141 channel, and ODIS ran the whole P475 flash
        on ISO9141."""
        if self._fast:
            proto = J.ISO14230
            flags = J.ISO9141_NO_CHECKSUM | J.ISO9141_K_LINE_ONLY  # our own cks
            self._ch = self._dev.connect(proto, self._baud0, flags)
            try:
                self._ch.pass_all()                             # receive ECU replies
            except Exception:
                pass
            for cfg in ({"P3_MIN": 0}, {"P4_MIN": 0}, {"LOOPBACK": 0}):
                try:
                    self._ch.set_config(**cfg)                  # best-effort timing loosen
                except Exception:
                    pass
        else:
            proto = J.ISO9141
            flags = J.ISO9141_NO_CHECKSUM                       # ODIS used flags 0x200
            self._ch = self._dev.connect(proto, self._baud0, flags)
            try:
                self._ch.pass_all()
            except Exception:
                pass
            try:
                self._ch.set_config(DATA_RATE=self._baud0)
            except Exception:
                pass
            for name, val in _SLOW_INIT_CONFIG.items():
                try:
                    self._ch.set_config(**{name: val})          # each best-effort
                except Exception:
                    pass
        self._ch_proto = proto

    # -- lifecycle ------------------------------------------------------------
    def wake_up(self, address: int = 0x01, even_parity: bool = False, tries: int = 3) -> int:
        self._tx.clear()
        self._rx.clear()
        self._carry.clear()
        # (Re)open the channel on the protocol this init mode needs -- if the mode
        # was toggled (fast<->slow) after an earlier connect, the old channel is on
        # the wrong protocol and must be dropped first.
        want_proto = J.ISO14230 if self._fast else J.ISO9141
        if self._ch is not None and self._ch_proto != want_proto:
            try:
                self._ch.disconnect()
            except Exception:
                pass
            self._ch = None
        if self._ch is None:
            self._connect_channel()
        # StartCommunication targets the KWP controller address 0x10 (as MPPS
        # does: 81 10 F1 81), NOT the 5-baud init address (0x01) -- using 0x01
        # here made the ECU never answer (the fast init "failed").
        req = START_COMM + bytes([_cks(START_COMM)])
        last = None
        for attempt in range(1, tries + 1):
            if any(f is not None and f() for f in (self.is_stop, self.init_is_stop)):
                raise KLineError("J2534 K-line init: stop requested")
            try:
                self._ch.clear_rx()
                if self._fast:
                    if self._trace:
                        self._trace("tx", req)
                    resp = self._ch.fast_init(req)
                    if self._trace and resp:
                        self._trace("rx", bytes(resp))
                    self._log("J2534 K-line fast init -> %s" % (bytes(resp).hex() or "(no data)"))
                else:
                    resp = self._ch.five_baud_init(address)
                    self._log("J2534 K-line 5-baud init -> %s" % (bytes(resp).hex() or "(no data)"))
                    return _version_from_keybytes(bytes(resp))
                return _version_from_keyword(bytes(resp))
            except Exception as ex:                             # noqa: BLE001
                last = ex
                # one hardware fast init can miss; optionally fall back to 5-baud
                # slow init -- which needs an ISO9141 channel, so switch mode and
                # reopen before trying it.
                if self._fast and self._allow_slow and attempt == tries:
                    try:
                        self._fast = False
                        try:
                            if self._ch is not None:
                                self._ch.disconnect()
                        except Exception:
                            pass
                        self._ch = None
                        self._connect_channel()
                        resp = self._ch.five_baud_init(SLOW_INIT_ADDR)
                        self._log("J2534 K-line fast init failed; 5-baud (ISO9141) "
                                  "fallback ok -> %s" % (bytes(resp).hex() or "(no data)"))
                        return _version_from_keybytes(bytes(resp))
                    except Exception as ex2:                   # noqa: BLE001
                        last = ex2
                        self._fast = True                       # restore for retry
                if attempt < tries:
                    time.sleep(0.4)
        raise KLineError("J2534 K-line init failed: %s" % last)

    def close(self):
        if self._ch is not None:
            try:
                self._ch.disconnect()
            except Exception:
                pass
            self._ch = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- port knobs -----------------------------------------------------------
    @property
    def read_timeout(self) -> float:
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds: float):
        self._timeout = seconds

    def set_baud(self, baud: int):
        if self._ch is not None:
            self._ch.set_baud(baud)                            # device switches K-line baud
        else:
            self._baud0 = int(baud)                            # the next channel opens at it

    def set_tx_interbyte(self, p4_min: int):
        """Set the tester TX inter-byte time (P4_MIN). The 5-baud slow-init config
        uses P4_MIN=2, which spaces every transmitted byte ~2 ms apart -- fine for
        small frames but it makes a 253-byte TransferData take ~0.5 s (a full write
        ~1 h). Dropping to 0 after the session is up transmits back-to-back at the
        negotiated baud (the fast-init read channel already uses P4_MIN=0), cutting
        the write time by ~an order of magnitude. Best-effort."""
        if self._ch is None:
            return
        try:
            self._ch.set_config(P4_MIN=int(p4_min))
        except Exception:
            pass

    def set_session_timing(self):
        """Once the session is up: no inter-byte gap on sends (P4_MIN 0), no
        wait after the ECU's reply before the next send (P3_MIN 0 -- the
        ISO9141 default 55 ms held every TransferData that long), and a reply
        ends after 1 ms of silence (P1_MAX 2 -- ISO9141 does not frame by
        length, so the 20 ms default delayed every ack by 20 ms). A reply the
        short P1_MAX splits is put back together in :meth:`_recv_message`.
        Each setting is best-effort."""
        if self._ch is None:
            return
        for cfg in ({"P4_MIN": 0}, {"P3_MIN": 0}, {"P1_MAX": 2}):
            try:
                self._ch.set_config(**cfg)
            except Exception:
                pass

    # -- high-baud discovery support (driven by edc16._find_high_baud) --------
    def high_baud_candidates(self):
        return list(J2534_HIGH_BAUD_CANDIDATES)

    def cached_high_baud(self, speed_byte):
        return _DISCOVERED_HIGH_BAUD.get(speed_byte)

    def set_cached_high_baud(self, speed_byte, baud):
        _DISCOVERED_HIGH_BAUD[speed_byte] = baud

    def reset_link(self):
        """Drop the ISO 14230 channel and reopen it fresh at 10400, so the next
        wake_up() is a clean fast init -- required between high-baud discovery
        attempts (a wrong rate leaves the ECU/channel out of sync; the fast-init
        pulse then re-initialises the ECU regardless of the rate it was at)."""
        if self._ch is not None:
            try:
                self._ch.set_baud(10400)
            except Exception:
                pass
            try:
                self._ch.disconnect()
            except Exception:
                pass
            self._ch = None
        self._tx.clear()
        self._rx.clear()
        self._carry.clear()

    def set_break(self, on: bool):
        pass                                                   # init pulse is the device's job

    def clear_rx(self):
        self._rx.clear()
        self._carry.clear()
        if self._ch is not None:
            try:
                self._ch.clear_rx()
            except Exception:
                pass

    # -- writes: buffer, flush whole frames as ISO 14230 messages -------------
    def write_raw(self, data: bytes):
        # raw write: leave the (synthesized) echo readable, as the serial K-line
        # would -- edc16 reads it back after write_raw
        self._feed(bytes(data), echo=True)

    def write_bytes(self, data: bytes):
        self._feed(bytes(data), echo=False)

    def write_byte(self, b: int):
        self._feed(bytes([b & 0xFF]), echo=False)

    def _feed(self, data: bytes, echo: bool):
        self._tx += data
        while True:
            n = _complete_frame_len(self._tx)
            if n is None:
                return
            frame = bytes(self._tx[:n])
            del self._tx[:n]
            self._exchange(frame, echo)

    def _exchange(self, frame: bytes, echo: bool):
        if self._trace:
            self._trace("tx", frame)
        # PassThruWriteMsgs blocks up to this timeout for the frame to clock out.
        # A slow-init K-line session can effectively stay at 10400 baud (the SM2
        # ISO9141 channel ignores the Medium switch, as ODIS's own flash did), so a
        # full 253-byte TransferData frame needs ~250 ms -- far past the old fixed
        # 100 ms, which timed out with sent=0 and aborted the write. Scale the
        # timeout to the frame size (~1 ms/byte at 10400) with generous headroom.
        write_to = max(1000, len(frame) * 4)
        if echo:
            self._rx += frame                                  # half-duplex echo (synthesized)
        resp = self._recv_message(frame, write_to)             # device sends on K-line
        if self._trace:
            self._trace("rx", resp)
        self._rx += resp

    def _recv_message(self, frame: bytes = None, write_to: int = 1000) -> bytes:
        """Send ``frame`` (if any) and return one ECU reply frame, absorbing
        responsePending (7F <svc> 78) and any TX echo the device reflects. A
        channel with ``kline_exchange`` (the 32-bit bridge's, or the direct
        one) sends and collects the reply in one call: through the bridge that
        is one round trip per frame instead of one per read.

        The reply is found as :func:`j2534.kline_split` finds it: an unflagged
        echo of ``frame`` and stray bytes ahead of the reply are dropped, and
        bytes after it (a second frame in the same buffer) are kept for the
        next call, as a serial port would keep them, until :meth:`clear_rx`."""
        deadline = time.monotonic() + self._timeout
        pending = 0
        buf = bytearray(self._carry)
        self._carry.clear()
        xchg = getattr(self._ch, "kline_exchange", None)
        tx = frame                                             # sent once, on the first pass
        req = frame                                            # what the reply answers
        while True:
            if tx is not None or kline_split(buf, req) is None:
                left = int(max(1, (deadline - time.monotonic()) * 1000))
                if xchg is not None:
                    msgs = xchg(tx, write_to, left, expect=req)
                else:
                    if tx is not None:
                        self._ch.write(tx, timeout_ms=write_to)
                    msgs = self._ch.read(1, left)
                tx = None
                for m in msgs:
                    if getattr(m, "is_tx_echo", False):
                        continue                               # device's own TX reflection
                    data = bytes(m.payload)
                    if data:                                   # not a zero-length indication
                        buf += data
            # a short P1_MAX can hand one frame over in pieces: wait for all of it
            at = kline_split(buf, req, final=time.monotonic() >= deadline)
            if at is None:
                if time.monotonic() >= deadline:
                    if buf:
                        return bytes(buf)                      # what came, as before
                    raise KLineTimeout("J2534 K-line: no response")
                continue
            got = bytes(buf[at[0]:at[1]])
            del buf[:at[1]]                                    # the rest: the next frame
            if _is_response_pending(got):
                if self._trace:
                    self._trace("rx", got)                     # log absorbed responsePending frames
                pending += 1
                if pending > 64:
                    raise KLineTimeout("J2534 K-line: stuck in responsePending")
                deadline = time.monotonic() + self._timeout    # 0x78 extends the wait
                continue
            self._carry += buf
            return got

    # -- reads: serve from the reassembled byte stream ------------------------
    def read_byte(self) -> int:
        if not self._rx:
            raise KLineTimeout("J2534 K-line: no data")
        return self._rx.pop(0)

    def read(self, n: int) -> bytes:
        if len(self._rx) < n:
            out = bytes(self._rx)
            self._rx.clear()
            raise KLineTimeout("J2534 K-line: short read (%d/%d)" % (len(out), n))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def read_complement(self, b: int):
        actual = self.read_byte()
        if actual != ((~b) & 0xFF):
            raise KLineError("J2534 K-line: complement mismatch")


__all__ = ["J2534KLine", "START_COMM"]
