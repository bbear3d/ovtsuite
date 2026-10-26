# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VW TP 2.0 transport over a CAN channel, presenting the K-line surface.

The EDC16 flash state machine (:mod:`ovtsuite.flash.edc16`) and the live-data
diagnostics (:mod:`ovtsuite.flash.diag`) are written against a half-duplex
K-line: they build fully-framed KWP2000 messages (``fmt [dest src] [len]
service data cks``) and read the echo + the ECU's framed reply back as a byte
stream.  On CAN the very same KWP services run inside VW's TP 2.0 transport
(confirmed frame-for-frame against real Scanmatik captures in Reference/Logs).

Rather than fork those validated state machines, this module gives them a
:class:`CanKLine` that quacks exactly like :class:`ovtsuite.flash.transport.KLine`
but carries each message over a :class:`Tp20Channel`:

* a written K-line frame is stripped to its bare KWP payload (service+data)
  and sent as one TP 2.0 message;
* the ECU's TP 2.0 reply is re-framed as the K-line frame the caller expects
  (same addressing mode as the request, correct checksum), preceded by the
  echo of the sent frame -- so ``read``/``read_byte`` see precisely the byte
  stream a real K-line would have returned.

The CAN side needs only a channel object exposing ``write_can(can_id,
payload)`` and ``read(max, timeout_ms) -> [msg]`` with ``msg.can_id`` /
``msg.payload`` -- both :class:`ovtsuite.flash.j2534.Channel` and
:class:`ovtsuite.flash.openport.OpenPortChannel` already do.

TP 2.0 timing note (see docs): the ECU declares its own inter-frame time in
the A1 parameter response and paces upload frames at that rate regardless of
what the tester allows, so throughput is frame-rate bound (7 data bytes per
CAN frame).  :class:`Tp20Params` advertises the minimum inter-frame time and a
large block size in our A0 request and drops intermediate ACK stalls; whether
the ECU honours a faster rate is ECU/gateway dependent and is left tunable.
"""
import time
from dataclasses import dataclass
from typing import Optional

try:
    from .transport import KLineError, KLineTimeout
except Exception:                       # standalone import (inside the 32-bit bridge)
    try:
        from transport import KLineError, KLineTimeout
    except Exception:
        class KLineError(Exception):
            pass

        class KLineTimeout(KLineError):
            pass

SETUP_ID = 0x200
POLL_MS = 5                    # historical device-read wait, no longer used as a
                               # PassThruReadMsgs timeout (see _next_frame's docstring,
                               # 2026-09-21) -- kept only so old references/comments
                               # elsewhere still resolve; _POLL_SPIN_MS is the real knob now.
_POLL_SPIN_MS = 0.3            # our OWN wait between empty non-blocking read polls
BROADCAST_SETUP = 0xC0          # channel setup opcode (request)
SETUP_OK = 0xD0                 # positive setup response (0xD1..0xD8 = refused)

# data-frame opcodes (high nibble); low nibble = sequence 0..15
OP_DATA_ACK = 0x0               # more data, waiting for ACK
OP_DATA_ACK_LAST = 0x1          # last frame of message, ACK expected
OP_DATA_NOACK = 0x2             # more data, no ACK
OP_DATA_NOACK_LAST = 0x3        # last frame, no ACK
OP_ACK_READY = 0xB              # ACK, ready for next (low nibble = next seq)
OP_ACK_NOTREADY = 0x9           # ACK, not ready yet
# connection-management opcodes (full byte)
OP_PARAM_REQ = 0xA0             # setup: parameter request (tester -> ECU)
OP_PARAM_RESP = 0xA1           # setup: parameter response; ALSO the reply to a
                               # channel-test (the ECU sends A3, we answer A1)
OP_CHANNEL_TEST = 0xA3         # "channel test" keep-alive -> answer with A1
OP_BREAK = 0xA4               # break / abort -- do NOT send this as a keep-alive
OP_DISCONNECT = 0xA8

RETX_MS = 200                   # retransmit interval for a not-yet-accepted boundary frame
NOTREADY_RETX_MS = 10           # a 9x "not ready" wants a PROMPT re-nudge, not a full
                                # RETX_MS wait: this ECU flow-controls nearly every
                                # TransferData block with 9x and won't send a ready
                                # unprompted, so waiting 200 ms per chunk was the ~5x
                                # transfer slowdown vs ODIS. On-car this ECU not-readies the FIRST ack-window
                                # of nearly every chunk (still finalizing the prior
                                # write) then answers our re-nudge ~immediately, so a
                                # tight 10 ms re-nudge recovers each far faster.


def _timing_byte(ms: float) -> int:
    """Encode a TP 2.0 timing byte: top 2 bits = scale, low 6 = value."""
    for scale, unit in ((0, 0.1), (1, 1.0), (2, 10.0), (3, 100.0)):
        v = int(round(ms / unit))
        if 0 <= v <= 0x3F:
            return (scale << 6) | v
    return 0xFF


def _timing_ms(b: int) -> float:
    return (b & 0x3F) * (0.1, 1.0, 10.0, 100.0)[b >> 6]


_clock = time.perf_counter       # send pacing clock (a seam for the tests)

#: Tp20Params.send_ms may undercut the ECU's advertised T3 by at most this
#: much: 0.8 ms against a programming channel's 1 ms, but 8 ms -- not 0.8 --
#: against a default-session channel that declares 10 ms.
SEND_MS_MIN_FRACTION = 0.8


def _precise_wait(seconds: float):
    """Wait ``seconds``, accurately, even for sub-millisecond durations.

    Speed fix (2026-09-21): ``time.sleep()`` on Windows -- where this
    module's real caller (the 32-bit J2534 bridge, see j2534_bridge.py)
    always runs -- commonly overshoots a short sleep by 1-2ms+, because the
    OS scheduler's default timer granularity (~15.6ms) dominates any
    requested delay well under that. Measured directly on Bear's real
    hardware: Tp20Channel.send()'s inter-frame gap was intended to be
    ecu_interframe_ms/t3_ms (as low as 0.5-0.8ms after the T3 speed fix),
    but the ACTUAL measured gap between consecutive CAN frames was ~2.4ms --
    a real reference capture of a working commercial-tool write to the same
    TCU family achieves ~0.73ms per frame with a comparably low advertised
    T3, so the ~1.6ms of unaccounted-for time here is a strong suspect.
    Busy-waiting on a monotonic clock is precise to microseconds regardless
    of OS scheduler granularity, at the cost of spinning a CPU core for
    that (very short, sub-2ms) span -- an easy trade for a flash write.
    Kept as a plain time.sleep() for anything longer, since OS granularity
    stops mattering once the requested wait is a healthy multiple of it,
    and there's no reason to burn CPU on a multi-millisecond-plus wait.
    """
    if seconds <= 0:
        return
    if seconds < 0.002:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            pass
    else:
        time.sleep(seconds)


@dataclass
class Tp20Params:
    """Timing we advertise to the ECU (A0).  Defaults push for the fastest
    the ECU will grant: minimum inter-frame time, largest block size, no
    intermediate ACK stalls."""
    block_size: int = 0x0F          # frames per ACK window we grant the ECU
    t1_ms: float = 100.0            # ACK timeout
    t3_ms: float = 5.0              # inter-frame time we advertise (0x32) -- matches
                                    # VCDS's known-good HVAC session; advertising the
                                    # old 0.1 ms (0x32->0x01) coincided with the ECU
                                    # flow-controlling our requests with 0x9x not-ready.
                                    # Actual send pacing follows the ECU's A1 anyway.
    ack_every: int = 15             # how often WE ACK inbound frames (BS)
    ignore_ecu_floor: bool = False  # if True, pace sends at t3_ms alone -- skip the
                                    # max(ecu_interframe_ms, t3_ms) clamp below. OFF by
                                    # default (the clamp is the safe, conservative
                                    # behaviour that protected the Climatronic/HVAC ECU).
                                    # ZF6HP-specific evidence (2026-09-21): the real
                                    # reference tool's own capture shows the SAME 6HP TCU
                                    # declaring 0.8ms back to it, yet it still paces at
                                    # ~0.73ms (below that floor) with no flow-control --
                                    # so for this ECU specifically, honoring only our own
                                    # advertised t3_ms is proven safe on real hardware.
    inter_send_ms: float = 0.0      # proactive gap BEFORE each message we send (a precise
                                    # busy-wait). The captured ODIS flash leaves ~4 ms
                                    # between a TransferData's 0x76 and the next chunk's
                                    # first frame; that breather lets this HVAC ECU finish
                                    # the prior chunk's flash write so it does NOT
                                    # flow-control the next with 9x not-ready. Set ~4 ms
                                    # for Climatronic to avoid the not-ready storm at the
                                    # source (vs merely recovering from it via NOTREADY).
    tx_ack_every: int = 0           # request an ACK every N frames WE send (0 = use the
                                    # ECU's advertised block size). The captured ODIS
                                    # flash requests an ack every ~4 frames, far below
                                    # the ECU's BS=15; acking only every 15 overran this
                                    # HVAC ECU and it flow-controlled nearly every block
                                    # with 9x not-ready (each costing a wait + an
                                    # ECU-directed resend). Acking sooner is always legal
                                    # (BS is a maximum) and keeps the module in sync.
    pace_ecu: bool = False          # if True, pace sends at the ECU's OWN advertised
                                    # inter-frame time (ecu_interframe_ms, parsed from
                                    # its A1) instead of the max(ecu, t3_ms) clamp --
                                    # i.e. as fast as the module said it wants, no
                                    # slower. We still ADVERTISE the safe t3_ms in A0
                                    # (a0() below), so this only speeds our SENDS to the
                                    # module's own number; it never advertises an
                                    # aggressive value. Falls back to the clamp until
                                    # the A1 is parsed (ecu_interframe_ms>0). Used for
                                    # Climatronic (module advertises ~1 ms; the 5 ms
                                    # clamp made our TransferData ~5x too slow).
    send_ms: float = 0.0            # >0: pace sends at this gap, below the ECU's
                                    # advertised T3 if smaller (the ZF6HP-style "ignore the
                                    # floor" without also changing the T3 we ADVERTISE) --
                                    # but never below SEND_MS_MIN_FRACTION of that T3.
    pace_from_start: bool = False   # measure the gap from the START of the previous frame's
                                    # write, not from when the write call returned: the
                                    # device call (~0.6 ms on a Scanmatik) then counts
                                    # toward the gap instead of adding to it.
    backoff_on_flow: bool = False   # safety net for the faster levels: the first 9x
                                    # not-ready / resend request / unanswered ACK during
                                    # a send drops pacing back to the ECU's advertised T3
                                    # (measured after the write returns) for the rest of
                                    # this channel's life, reopens included, and logs it.

    def a0(self) -> bytes:
        return bytes([OP_PARAM_REQ, self.block_size & 0xFF,
                      _timing_byte(self.t1_ms), 0xFF, _timing_byte(self.t3_ms), 0xFF])


class Tp20Channel:
    """One TP 2.0 connection to an ECU logical address over a CAN channel."""

    def __init__(self, can, dest=0x01, listen_id=0x300, params: Optional[Tp20Params] = None,
                 log=None, poll_timeout_ms=2000):
        self.can = can
        self.dest = dest
        self.listen_id = listen_id          # id we ask the ECU to send to us on
        self.tx_id = None                   # id we send to (from setup response)
        self.rx_id = listen_id
        self.params = params or Tp20Params()
        self._log = log or (lambda s: None)
        self.poll_timeout_ms = poll_timeout_ms
        self.tx_seq = 0
        self.rx_seq = 0
        self.ecu_interframe_ms = 0.0
        self.ecu_block_size = 0x0F
        self._pending = []                  # CAN msgs read but not yet consumed
        self.opened = False
        self._hw_filtered = False           # device-side id filter installed?
        self.is_stop = None                 # optional callable()->bool; abort waits when true
        # backoff_on_flow: kept across open()/_reset() -- once the ECU has pushed
        # back, the faster pacing stays off for the rest of the session
        self.backed_off = False
        self.flow_events = 0                # 9x / resend requests / ACK timeouts seen
        self.notes = []                     # log lines for a caller without a log (bridge)

    # ---- low-level CAN ----
    def _tx(self, data: bytes, timeout_ms: int = 100):
        """Send one raw CAN frame. ``timeout_ms`` is threaded straight to the
        underlying PassThruWriteMsgs call: per SAE J2534, a nonzero Timeout
        blocks the call until the frame is actually confirmed transmitted (or
        the timeout elapses), while 0 queues it and returns immediately. See
        send()'s NOACK branch -- most transferData frames don't need that
        confirm-wait, since we already pace sends ourselves via
        _precise_wait(gap); waiting again inside the driver call just adds a
        USB round trip neither the ECU nor our own protocol needs. Boundary
        frames (an ACK is coming regardless) keep the default blocking write.
        NOTE (2026-09-21): unverified on real hardware yet -- if this
        misbehaves (some J2534 DLLs don't follow the queue-on-zero
        convention), it fails fast and obviously (a hang or immediate error
        on the very first frame), not silently, so it's a safe experiment to
        revert."""
        self.can.write_can(self.tx_id, data, timeout_ms=timeout_ms)

    def _tx_setup(self, can_id: int, data: bytes):
        self.can.write_can(can_id, data)

    def _keep_ids(self, target):
        """CAN ids whose frames we might still consume: the id we're waiting
        for, our channel's rx id, and the setup-response id.  Anything else on
        the bus -- the powertrain broadcast flood (0x280/0x380/0x480/... ),
        which on a live vehicle runs to hundreds of frames per second -- is
        noise to us and must be dropped, never buffered (see _next_frame)."""
        return (target, self.rx_id, SETUP_ID + self.dest)

    def _next_frame(self, timeout_ms: int, want_id=None) -> Optional[bytes]:
        """Return the next CAN payload on our rx id (or want_id), or None on
        timeout.

        Only frames on an id we might consume are buffered; every other id is
        discarded on the spot.  This is not an optimisation -- it is what keeps
        a long transfer alive.  The channel runs with a pass-all filter, so
        each device read also hauls in the vehicle's broadcast traffic
        (~350 frames/s on ~7 ids during a real read).  If those were buffered
        they would never be consumed, `_pending` would grow without bound, and
        the linear scan below would turn every frame receipt into an O(n) walk
        of accumulated garbage -- ACK latency then climbs until the ECU
        retransmits and disconnects.  Dropping them keeps `_pending` tiny.

        Speed fix (2026-09-21, read-path investigation): this used to issue
        the device read as ``self.can.read(16, up-to-POLL_MS-ms)`` -- a
        BLOCKING PassThruReadMsgs call with a short (<=5ms) timeout, on the
        theory that keeping the device-side wait short keeps ACK latency low.
        Measured directly from a real read log: the actual gap between
        consecutive incoming CAN frames clustered overwhelmingly (~2/3 of
        ~40,000 samples, in one real read) at ~15.5ms, not the requested
        <=5ms -- the SAME Windows OS-scheduler-quantum overshoot already
        found and fixed on the send side (see ``_precise_wait``'s docstring)
        was happening again here, inside the J2534 DLL's own blocking wait
        for PassThruReadMsgs. This loop runs for the entire duration of
        every read AND every write's ACK-wait (``_await_ack()`` calls this
        too), so that ~15.5ms floor was costing far more overall than any
        single request's latency -- it explains a plain KWP2000 read's
        ~630 B/s (matches the real reference tool's own read capture almost
        exactly, so the ECU itself is not the slow part here) far better
        than ECU processing time does, once you look at raw frame arrival
        gaps instead of only the request/response cycle time.
        FIX: poll with ``timeout_ms=0`` -- per SAE J2534 this returns
        immediately with whatever is already buffered rather than blocking
        -- and, only when nothing was there, wait a short, ACCURATE interval
        ourselves via ``_precise_wait`` before polling again: the same "do
        the waiting ourselves, don't trust the driver's blocking wait for a
        short duration" principle as the write-side fix, applied to
        receiving. This keeps the CPU busier while a transfer is in flight
        (a real busy-poll, not a blocking wait) -- a disclosed trade for
        speed, not a free lunch. NOT yet bench-verified against Bear's
        hardware."""
        target = want_id if want_id is not None else self.rx_id
        keep = self._keep_ids(target)
        deadline = time.monotonic() + timeout_ms / 1000.0
        polled = False
        while True:
            if self.is_stop is not None and self.is_stop():
                raise KLineError("TP2.0: aborted (stop requested)")
            for i, m in enumerate(self._pending):
                if m.can_id == target:
                    del self._pending[i]
                    return m.payload
            if time.monotonic() >= deadline and polled:   # always poll at least once
                return None
            polled = True
            got = self.can.read(16, 0)                    # non-blocking poll (see docstring)
            if got:
                self._pending.extend(m for m in got if m.can_id in keep)
            elif time.monotonic() < deadline:
                _precise_wait(_POLL_SPIN_MS / 1000.0)

    # ---- setup ----
    def _install_rx_filter(self):
        """Restrict the CAN channel to our TP 2.0 ids (setup reply + the id the
        ECU sends on) so the vehicle's broadcast flood never reaches the device
        RX buffer -- otherwise it overflows during a write's non-reading phases
        (ERR_BUFFER_OVERFLOW).  No-op on channels without hardware filtering
        (mock/replay); those rely on the software drop in _next_frame."""
        pass_ids = getattr(self.can, "pass_ids", None)
        if pass_ids is None:
            return
        ids = sorted({SETUP_ID + self.dest, self.listen_id, self.rx_id})
        try:
            pass_ids(ids)
            self._hw_filtered = True
            self._log("TP2.0: device filter -> %s" % ", ".join(hex(i) for i in ids))
        except Exception as ex:
            self._hw_filtered = False
            self._log("TP2.0: hardware id filter unavailable (%s); software drop only" % ex)
            try:                                            # keep receiving *something*
                if hasattr(self.can, "pass_all"):
                    self.can.pass_all()
            except Exception:
                pass

    def _reset(self):
        """Clear per-connection state so open() can re-setup cleanly (reconnect)."""
        if self.opened and self.tx_id is not None:
            try:
                self._tx(bytes([OP_DISCONNECT]))            # drop any half-open channel
            except Exception:
                pass
            # let the ECU fully tear the old channel down before the new setup --
            # MPPS waits ~220ms here; sending the 0x200 setup too soon makes the
            # ECU hand out a fresh tester id (0x741) and never answer our A0
            # (Writeattempt.log: re-establish after the programming session hung
            # because the setup raced the ECU's A8 ack).
            time.sleep(0.25)
        self.opened = False
        self.tx_seq = 0
        self.rx_seq = 0
        self.tx_id = None
        self.rx_id = self.listen_id
        self._pending = []
        try:                                                # drain stale CAN frames
            for _ in range(8):
                if not self.can.read(32, 1):
                    break
        except Exception:
            pass

    def open(self):
        self._reset()
        self._install_rx_filter()                           # cut the broadcast flood at the device
        setup = bytes([self.dest, BROADCAST_SETUP, 0x00, 0x10,
                       self.listen_id & 0xFF, (self.listen_id >> 8) & 0xFF, 0x01])
        for attempt in range(3):
            self._tx_setup(SETUP_ID, setup)
            resp = self._next_frame(self.poll_timeout_ms, want_id=SETUP_ID + self.dest)
            if resp is not None and len(resp) >= 7:
                if (resp[1] & 0xF0) != SETUP_OK:
                    raise KLineError("TP2.0 channel setup refused (0x%02X)" % resp[1])
                self.rx_id = resp[2] | (resp[3] << 8)       # id the ECU sends on
                self.tx_id = resp[4] | (resp[5] << 8)       # id we send to
                self._log("TP2.0 channel: ECU->%s tester->%s" % (hex(self.rx_id), hex(self.tx_id)))
                if self.rx_id not in (SETUP_ID + self.dest, self.listen_id):
                    self._install_rx_filter()               # ECU sends on a different id: admit it too
                self._negotiate()
                self.opened = True
                return
            time.sleep(0.05)
        raise KLineTimeout("TP2.0 channel setup: no response from ECU 0x%02X" % self.dest)

    def _negotiate(self):
        self._tx(self.params.a0())
        for _ in range(6):                                  # skip keep-alives until A1
            resp = self._next_frame(self.poll_timeout_ms)
            if resp is None:
                break
            if resp[0] == OP_PARAM_RESP:
                self.ecu_block_size = resp[1]
                self.ecu_interframe_ms = _timing_ms(resp[4])
                self._log("TP2.0 params: ECU BS %d, interframe %.1f ms" %
                          (self.ecu_block_size, self.ecu_interframe_ms))
                return
            if self._classify(resp) == "disc":
                raise KLineError("TP2.0: ECU disconnected during params")
        raise KLineTimeout("TP2.0 parameter response missing")

    def _a1(self) -> bytes:
        """Our parameters expressed as an A1 response (reply to a channel test)."""
        return bytes([OP_PARAM_RESP]) + self.params.a0()[1:]

    def keepalive(self):
        # the tester keeps the channel alive by answering the ECU's channel
        # tests (see _classify); it does not itself initiate one on this ECU
        self._next_frame(self.poll_timeout_ms)

    def _classify(self, frame: bytes) -> str:
        """Categorise an inbound frame, answering channel-tests.
        Returns 'data' | 'ack' | 'notready' | 'skip' | 'disc'."""
        op = frame[0] >> 4
        if op in (OP_DATA_ACK, OP_DATA_ACK_LAST, OP_DATA_NOACK, OP_DATA_NOACK_LAST):
            return "data"
        if op == OP_ACK_READY:
            return "ack"
        if op == OP_ACK_NOTREADY:
            return "notready"
        if op == 0xA:                               # connection-management frame
            b = frame[0]
            if b == OP_CHANNEL_TEST:                # A3 keep-alive -> answer A1 (NOT A4!)
                self._tx(self._a1())
            elif b == OP_DISCONNECT:                # A8
                return "disc"
            return "skip"                           # A0/A1/A4: ignore
        return "skip"

    # ---- message send ----
    def send(self, payload: bytes):
        """Send one KWP payload (service+data) as a TP 2.0 message.

        Frames carry a 4-bit sequence; the ECU acks with the sequence it wants
        NEXT. A gatewayed VAG module (HVAC 0x2C) will silently drop a frame it
        wasn't ready for -- typically a non-boundary (no-ACK) continuation
        arriving right on the heels of its own multi-frame reply -- and then, at
        the block ACK, keep inviting that earlier sequence. So the retransmit
        unit is the whole block, not just its last frame: retain every frame and,
        whenever the ECU's ack points at a sequence we already sent, resend from
        that sequence onward. Retransmitting only the boundary frame deadlocks
        (climatronic-canlog-20260921-161506: seq3 dropped, seq4 resent 300x while
        the ECU asked for seq3 the whole time -> security access never unlocked)."""
        if self.params.inter_send_ms:
            _precise_wait(self.params.inter_send_ms / 1000.0)   # inter-chunk breather
        payload = bytes(payload)
        body = len(payload).to_bytes(2, "big") + payload        # 16-bit length prefix
        chunks = [body[i:i + 7] for i in range(0, len(body), 7)] or [b""]
        # Build every frame with its sequence number up front, so a boundary ACK
        # can resend any earlier frame the ECU asks for (see _await_ack).
        n = len(chunks)
        built = []                                  # [(seq, frame_bytes, boundary), ...]
        for i, chunk in enumerate(chunks):
            last = i == n - 1
            # ask for an ACK on the final frame and at each block-size boundary
            _win = self.params.tx_ack_every or self.ecu_block_size
            boundary = last or ((i + 1) % max(1, _win) == 0)
            op = (OP_DATA_ACK_LAST if last else OP_DATA_ACK) if boundary else OP_DATA_NOACK
            seq = (self.tx_seq + i) & 0x0F
            built.append((seq, bytes([(op << 4) | seq]) + chunk, boundary))
        self.tx_seq = (self.tx_seq + n) & 0x0F
        for i, (seq, frame, boundary) in enumerate(built):
            gap, from_start = self._send_gap()      # per frame: a back-off applies at once
            t0 = _clock()
            self._tx(frame, timeout_ms=100 if boundary else 0)
            if boundary:
                self._await_ack(built, i)
            elif gap:
                _precise_wait(gap - (_clock() - t0) if from_start else gap)

    def _send_gap(self):
        """(seconds between our frames, measured from the write's start?)."""
        p = self.params
        ecu = self.ecu_interframe_ms
        if self.backed_off:                         # the ECU pushed back: its own pace
            return (ecu if ecu > 0 else max(ecu, p.t3_ms)) / 1000.0, False
        if p.send_ms > 0 and ecu > 0:
            gap = max(p.send_ms, ecu * SEND_MS_MIN_FRACTION)
        elif p.pace_ecu and ecu > 0:
            gap = ecu                               # the module's own advertised pace
        elif p.ignore_ecu_floor:
            gap = p.t3_ms
        else:
            gap = max(ecu, p.t3_ms)
        return gap / 1000.0, p.pace_from_start

    def _flow_event(self, what: str):
        """The ECU pushed back during a send (9x, a resend request, no ACK).
        With ``backoff_on_flow`` the first one ends the faster pacing."""
        self.flow_events += 1
        if not self.params.backoff_on_flow or self.backed_off:
            return
        self.backed_off = True
        msg = ("TP2.0: ECU %s -- pacing backed off to its advertised %.1f ms for the "
               "rest of the session" % (what, self.ecu_interframe_ms))
        self.notes.append(msg)
        self._log(msg)

    def take_notes(self):
        out, self.notes = self.notes, []
        return out

    def _await_ack(self, built, boundary_idx: int):
        # Wait for the ECU to ACCEPT the block ending at ``built[boundary_idx]``.
        # ``built`` is [(seq, frame_bytes, boundary), ...] for the whole message.
        # The ack's sequence nibble is the frame the ECU wants NEXT:
        #   0xB, nibble == boundary_seq+1  -> block accepted, ready for next (done)
        #   0xB, nibble == a seq we sent   -> "(re)send from that sequence onward"
        #   0xB, nibble == boundary_seq    -> ready, resend this frame
        #   0x9x (not-ready)               -> busy/holding, resend on the tick
        # A gatewayed VAG module NAKs then only advances once the frame(s) it
        # wants are retransmitted -- it won't emit a ready unprompted. So resend
        # from the requested sequence on each advancing-but-not-done ack, plus a
        # periodic boundary-frame nudge, bounded by an overall deadline + stop hook.
        # Keep-alives (A3) are answered in _classify and don't extend the deadline.
        boundary_seq = built[boundary_idx][0]
        want = (boundary_seq + 1) & 0x0F
        seq_to_idx = {s: k for k, (s, _f, _b) in enumerate(built[:boundary_idx + 1])}

        def resend_from(seq):
            idx = seq_to_idx.get(seq, 0)            # unknown seq: resend from block start
            for _s, f, _b in built[idx:boundary_idx + 1]:
                self._tx(f)

        overall = time.monotonic() + max(self.poll_timeout_ms, 5000) / 1000.0
        next_retx = time.monotonic() + RETX_MS / 1000.0
        while True:
            if self.is_stop is not None and self.is_stop():
                raise KLineError("TP2.0: aborted (stop requested)")
            now = time.monotonic()
            if now >= overall:
                raise KLineTimeout("TP2.0: frame not accepted (no advancing ACK)")
            if now >= next_retx:
                self._flow_event("left a block unacknowledged")
                self._tx(built[boundary_idx][1])    # nudge with the boundary frame
                next_retx = now + RETX_MS / 1000.0
            budget = int((min(next_retx, overall) - now) * 1000)
            resp = self._next_frame(max(1, budget))
            if resp is None:
                continue                            # tick: retransmit at next_retx
            op = resp[0] >> 4
            seqn = resp[0] & 0x0F
            if op == OP_ACK_READY:
                if seqn == want:
                    return                          # block accepted, ready for next
                self._flow_event("asked for a resend")
                resend_from(seqn)                   # ECU wants this seq (re)sent
                next_retx = time.monotonic() + RETX_MS / 1000.0
                continue
            if op == OP_ACK_NOTREADY:
                self._flow_event("answered not-ready (9x)")
                # ECU got the block but isn't ready to ack yet, and won't emit a
                # ready unprompted -- re-nudge SOON (not after the full RETX_MS).
                next_retx = min(next_retx,
                                time.monotonic() + NOTREADY_RETX_MS / 1000.0)
                continue
            if self._classify(resp) == "disc":
                raise KLineError("TP2.0: ECU disconnected")
            # data or skip (keep-alive answered inside _classify): keep waiting

    # ---- message receive ----
    def recv(self, timeout_ms=None) -> bytes:
        """Reassemble one complete inbound TP 2.0 message -> KWP payload."""
        timeout_ms = timeout_ms if timeout_ms is not None else self.poll_timeout_ms
        acc = bytearray()
        expected = None
        # Overall deadline for the whole message. Channel-test keep-alives (A3/A1)
        # are answered inside _classify and MUST NOT extend it -- otherwise an ECU
        # that ACKs a request but never sends the response (e.g. HVAC 0x2C replying
        # 0x92 to 27 03, then only A3 forever) spins here indefinitely. Real data
        # progress resets the deadline so a legit multi-frame reply isn't cut off.
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise KLineTimeout("TP2.0: message receive timed out")
            frame = self._next_frame(remaining)
            if frame is None:
                raise KLineTimeout("TP2.0: message receive timed out")
            kind = self._classify(frame)             # answers keep-alives itself
            if kind == "disc":
                raise KLineError("TP2.0: ECU disconnected")
            if kind != "data":
                continue                             # params / keep-alive / stray ack
            deadline = time.monotonic() + timeout_ms / 1000.0   # progress -> extend
            op = frame[0] >> 4
            acc += frame[1:]
            ack_expected = op in (OP_DATA_ACK, OP_DATA_ACK_LAST)
            last = op in (OP_DATA_ACK_LAST, OP_DATA_NOACK_LAST)
            if expected is None and len(acc) >= 2:
                expected = int.from_bytes(acc[:2], "big")
            # ACK only when the ECU asks (op 0/1); it drives the block window
            if ack_expected:
                self.rx_seq = (frame[0] & 0x0F) + 1
                self._tx(bytes([(OP_ACK_READY << 4) | (self.rx_seq & 0x0F)]))
            if last:
                break
        payload = bytes(acc[2:])
        if expected is not None:
            payload = payload[:expected]
        return payload

    def close(self):
        if self.opened and self.tx_id is not None:
            try:
                self._tx(bytes([OP_DISCONNECT]))
            except Exception:
                pass
        self.opened = False


# ---------------------------------------------------------------- KLine facade
def _frame_payload(frame: bytes):
    """Strip a K-line KWP frame to (addressed, payload) where payload is
    service+data.  Accepts addressed (fmt&0x80) and address-less frames."""
    if not frame:
        return False, b""
    fmt = frame[0]
    addressed = bool(fmt & 0x80)
    i = 3 if addressed else 1
    n = fmt & 0x3F
    if n == 0:
        n = frame[i]
        i += 1
    return addressed, bytes(frame[i:i + n])


def _reframe(addressed: bool, payload: bytes) -> bytes:
    """Wrap a KWP payload as the K-line response frame edc16.py / diag.py
    expect (ECU->tester: dest 0xF1, src 0x10 when addressed).

    The EDC16 uses two address-less framings over K-line and the readers are
    picky about which: ``requestUpload`` answers in the short form
    ``02 75 FF`` (length in the format byte), while ``transferData`` answers in
    the long form ``00 <len> 76 ...`` even for a one-byte ack (``00 01 76 77``).
    Since CAN carries no framing, pick the form by the response service:
    short only for the 0x75 reply, long otherwise -- which is what the read
    (fmt&0x3F==0) and write-ack (00 01 76 77) readers both want."""
    n = len(payload)
    out = bytearray()
    short = addressed or (payload[:1] == b"\x75")           # 0x75 = requestUpload +ve
    if addressed:
        out.append(0x80 | (n if (short and n < 0x40) else 0))
        out += b"\xF1\x10"
    else:
        out.append(n if (short and n < 0x40) else 0)
    if not (short and n < 0x40):
        out.append(n & 0xFF)
    out += payload
    out.append(sum(out) & 0xFF)
    return bytes(out)


class CanKLine:
    """K-line look-alike (the subset edc16.py / diag.py use) over TP 2.0."""

    is_tp20 = True                      # marker: this link carries KWP over TP 2.0, not K-line
    #   lets the EDC16 flow skip K-line-only steps (e.g. accessTimingParameter,
    #   whose P2/P3 byte timing the TP 2.0 transport negotiates itself).

    def __init__(self, can=None, dest=0x01, params: Optional[Tp20Params] = None, log=None, tp=None):
        # ``tp`` lets the caller supply a ready TP channel -- e.g. a BridgeTp20
        # that runs the whole receive/ACK loop inside the 32-bit bridge (no
        # per-frame IPC), instead of a local Tp20Channel over the CAN channel.
        self.tp = tp if tp is not None else Tp20Channel(can, dest=dest, params=params, log=log)
        if tp is not None and log is not None and getattr(tp, "log", False) is None:
            tp.log = log                # a BridgeTp20 relays the bridge's notes (back-off) here
        self._log = log or (lambda s: None)
        self.direct = True
        self._timeout = 8.0
        self._tx = bytearray()          # bytes written but not yet a whole frame
        self._rx = bytearray()          # bytes ready to be read back
        self.last_sync = 0x55
        #: optional callable()->bool; a responsePending wait gives up when true.
        #: (A local Tp20Channel has its own ``tp.is_stop`` for its frame waits.)
        self.is_stop = None

    # -- lifecycle --
    def wake_up(self, address=0x01, even_parity=False, tries=3):
        # the TP 2.0 channel dest is fixed at construction (the ECU logical
        # address); the KWP controller address rides inside the frames
        self._tx.clear()                # drop any half-assembled frame / stale echo
        self._rx.clear()
        self.tp.open()                  # re-setup (open() resets TP state for reconnect)
        return 0x2000                   # KWP2000 (>=2000); edc16/diag only check >=2000

    def reset_channel(self):
        """Tear down and re-establish the TP 2.0 channel WITHOUT re-entering the
        KWP session.  Entering the EDC16 programming session (10 85) resets the
        ECU's transport: MPPS sends A8, re-runs the 0x200 channel setup + A0/A1
        params, then re-authenticates, before any programming command -- on the
        stale channel the ECU A8-disconnects the first request
        (EDC16MPPSWrite.log)."""
        self._tx.clear()
        self._rx.clear()
        reopen = getattr(self.tp, "reopen", None)
        if reopen is not None:
            reopen()                    # bridge: re-run setup on the existing CAN channel
        else:
            self.tp.open()              # local Tp20Channel: open() re-resets + re-negotiates

    def close(self):
        try:
            self.tp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- port knobs (no-ops / trivial over CAN) --
    @property
    def read_timeout(self):
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds):
        self._timeout = seconds

    def set_baud(self, baud):
        pass

    def set_break(self, on):
        pass

    def clear_rx(self):
        self._rx.clear()

    # -- writes: buffer, flush whole frames as TP2.0 messages --
    def write_raw(self, data: bytes):
        # raw write: the half-duplex echo is left on the wire for the caller
        # to read back (edc16 does this deliberately after write_raw)
        self._feed(bytes(data), echo=True)

    def write_bytes(self, data: bytes):
        self._feed(bytes(data), echo=False)     # KLine.write_bytes drains the echo

    def write_byte(self, b: int):
        self._feed(bytes([b & 0xFF]), echo=False)

    def _feed(self, data: bytes, echo: bool):
        self._tx += data
        while True:
            n = self._complete_frame_len(self._tx)
            if n is None:
                return
            frame = bytes(self._tx[:n])
            del self._tx[:n]
            self._exchange(frame, echo)

    @staticmethod
    def _complete_frame_len(buf: bytes):
        """Length of a complete K-line frame at the front of buf, or None."""
        if not buf:
            return None
        fmt = buf[0]
        i = 3 if fmt & 0x80 else 1
        n = fmt & 0x3F
        if n == 0:
            if len(buf) <= i:
                return None
            n = buf[i]
            i += 1
        total = i + n + 1               # + checksum
        return total if len(buf) >= total else None

    def _exchange(self, frame: bytes, echo: bool):
        addressed, payload = _frame_payload(frame)
        if not payload:
            return
        self.tp.send(payload)
        if echo:                                # raw write: echo readable
            self._rx += frame
        resp = self._recv_final()
        self._rx += _reframe(addressed, resp)

    def _recv_final(self) -> bytes:
        """Receive the ECU's response, transparently absorbing KWP2000
        responsePending (``7F <service> 78``).

        The ECU emits responsePending while it works on a slow service -- e.g.
        computing the ``1A 9C`` erase signature or a security seed -- and sends
        the real response some seconds later, unsolicited.  A K-line byte stream
        handles this inline; over TP 2.0 each message is discrete, so if we
        returned the pending frame the delayed real response would arrive with
        nobody reading it.  It would then be picked up as the answer to the NEXT
        request, drifting the sequence numbers until the ECU retransmits and
        disconnects (the Writeattempt.log failure).  So wait for the real one
        here.  Each pending frame refreshes the wait (0x78 = keep waiting, like
        P3max), bounded so a stuck ECU can't loop forever."""
        for _ in range(64):
            if self.is_stop is not None and self.is_stop():
                raise KLineError("CanKLine: aborted (stop requested)")
            resp = self.tp.recv(int(self._timeout * 1000))
            if len(resp) == 3 and resp[0] == 0x7F and resp[2] == 0x78:
                self._log("KWP responsePending (7F %02X 78); awaiting real response" % resp[1])
                continue
            return resp
        raise KLineTimeout("CanKLine: ECU stayed in responsePending")

    # -- reads: serve from the reassembled byte stream --
    def read_byte(self) -> int:
        if not self._rx:
            raise KLineTimeout("CanKLine: no data")
        return self._rx.pop(0)

    def read(self, n: int) -> bytes:
        if len(self._rx) < n:
            out = bytes(self._rx)
            self._rx.clear()
            raise KLineTimeout("CanKLine: short read (%d/%d)" % (len(out), n))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def read_complement(self, b: int):
        actual = self.read_byte()
        if actual != ((~b) & 0xFF):
            raise KLineError("CanKLine: complement mismatch")


__all__ = ["Tp20Channel", "Tp20Params", "CanKLine", "SETUP_ID"]
