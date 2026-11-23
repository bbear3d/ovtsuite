# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An imperfect K-line for the 09D simulator, to find out what a tester has to
do to ride out a bad connection during a write.

:class:`FaultyEcu` is a :class:`~ovtsuite.flash.trx_kline_sim.KLineEcu` that,
for a limited window starting at the first ``transferData`` (0x36), damages
some exchanges; before and after the window the link is clean, so a long write
only has to suffer the first few minutes. Every fault is logged
(``FAULT n kind ...``) with how many data bytes the simulated TCU has taken so
far, so a tester's recovery can be judged afterwards: a block whose reply was
lost and was then re-sent shows up as a duplicate in the captured image,
because ``transferData`` carries no block counter.

The faults:

``lost_request``   the request is not seen at all (nothing is done, no reply)
``lost_response``  the request is carried out, the reply never arrives
``bad_checksum``   the reply arrives with a wrong checksum
``truncated``      half of the reply arrives, then silence
``noise``          junk bytes ahead of an otherwise good reply
``delay``          the reply comes late (``delay_s``)
``busy``           ``7F 36 21`` (busyRepeatRequest); the block is NOT taken
``pending``        ``7F 36 78`` (responsePending), then the real reply later
``silence``        the TCU goes quiet for ``silence_s`` (requests unseen). The
                   original tool retries after ~4 s and gives up after three
                   tries (~8 s): a 10 s silence ended its write (2026-11-16),
                   so the default is 6 s -- its third try gets through

A fixed script puts one of each early in the window, so a short run covers
them all, then ``rate`` of the remaining blocks are hit at random (seeded, so a
run repeats).
"""
import random
import time
from typing import Callable, Dict, List, Optional

from .trx_kline_sim import KLineEcu, build_frame

SCRIPT = (          # (n-th transferData in the window, fault)
    (4, "lost_response"), (9, "bad_checksum"), (15, "truncated"), (22, "delay"),
    (38, "pending"), (46, "lost_request"), (55, "noise"),
    (70, "lost_response"), (85, "silence"), (110, "lost_response"),
    (111, "lost_response"),                 # two in a row: the retry is lost too
)
#: ``7F 36 21`` (busyRepeatRequest) ended the original tool's write at once on
#: the bench (2026-11-16: no retry, no reply for 8 s), so it is not in the
#: default run; ask for it (``busy=True`` / ``--busy``) to see it again.
BUSY_AT = 150
#: ``busy`` is left out on purpose, see above
RANDOM_KINDS = ("lost_request", "lost_response", "bad_checksum", "truncated",
                "noise", "delay", "pending")


class FaultPlan:
    """Decides which exchanges are damaged. ``window_s`` runs from the first
    transferData; ``rate`` is the share of later blocks hit at random."""

    def __init__(self, window_s: float = 240.0, rate: float = 0.01, seed: int = 1,
                 delay_s: float = 2.5, silence_s: float = 6.0, pending_s: float = 1.5, script=SCRIPT,
                 clock: Callable[[], float] = time.monotonic, busy: bool = False):
        self.window_s, self.rate, self.seed = window_s, rate, seed
        self.delay_s, self.silence_s, self.pending_s = delay_s, silence_s, pending_s
        self.script = dict(script)
        if busy:
            self.script.setdefault(BUSY_AT, "busy")
        self.clock = clock
        self.rng = random.Random(seed)
        self.started: Optional[float] = None
        self.n = 0                      # transferData requests seen in the window

    def active(self) -> bool:
        return self.started is not None and self.clock() - self.started < self.window_s

    def next_fault(self, service: int) -> Optional[str]:
        """The fault for this request, or None. Only transferData is damaged."""
        if service != 0x36:
            return None
        if self.started is None:
            self.started = self.clock()
        if not self.active():
            return None
        self.n += 1
        kind = self.script.get(self.n)
        if kind is None and self.rng.random() < self.rate:
            kind = self.rng.choice(RANDOM_KINDS)
        return kind


class FaultyResponder:
    """Wraps a responder; applies the request-side faults and hands the
    reply-side ones to :class:`FaultyEcu`."""

    def __init__(self, inner, plan: FaultPlan, log: Callable[[str], None]):
        self.inner, self.plan, self._log = inner, plan, log
        self.reply_fault: Optional[str] = None
        self.silent_until = 0.0
        self.faults: List[tuple] = []

    def __getattr__(self, name):               # everything else is the inner responder's
        return getattr(self.inner, name)

    def _taken(self) -> int:
        d = getattr(self.inner, "_download", None)
        return len(d["buf"]) if d else 0

    def _note(self, kind, payload, extra=""):
        self.faults.append((self.plan.n, kind))
        self._log("FAULT %d %s on %s (TCU holds %d data bytes before this)%s"
                  % (self.plan.n, kind, payload[:1].hex(), self._taken(), extra))

    def handle(self, payload: bytes) -> Optional[bytes]:
        self.reply_fault = None
        if time.monotonic() < self.silent_until:
            self._log("FAULT (silent) request %s unseen" % payload[:1].hex())
            return None
        kind = self.plan.next_fault(payload[0] if payload else -1)
        if kind is None:
            return self.inner.handle(payload)
        self._note(kind, payload)
        if kind == "lost_request":
            return None
        if kind == "busy":
            return bytes([0x7F, payload[0], 0x21])
        if kind == "silence":
            self.silent_until = time.monotonic() + self.plan.silence_s
            return None
        resp = self.inner.handle(payload)      # the block IS taken for the rest
        if kind == "lost_response":
            return None
        self.reply_fault = kind
        return resp


class FaultyEcu(KLineEcu):
    """:class:`KLineEcu` whose replies pass through the plan's damage."""

    def _send(self, payload: bytes, dest: int, src: int):
        kind = getattr(self.responder, "reply_fault", None)
        self.responder.reply_fault = None
        plan = getattr(self.responder, "plan", None)
        frame = build_frame(payload, dest, src)
        out = frame
        if kind == "bad_checksum":
            out = frame[:-1] + bytes([(frame[-1] ^ 0x5A) & 0xFF])
        elif kind == "truncated":
            out = frame[:max(1, len(frame) // 2)]
        elif kind == "noise":
            out = bytes(random.Random(len(frame)).choice((0x00, 0xFF, 0x55, 0xA5))
                        for _ in range(3)) + frame
        elif kind == "delay" and plan is not None:
            time.sleep(plan.delay_s)
        elif kind == "pending":
            pend = build_frame(bytes([0x7F, 0x36, 0x78]), dest, src)
            self.port.write_bytes(pend)
            self._drain_echo(pend)
            self.log("  TX %s  (responsePending)" % pend.hex(" "))
            time.sleep(plan.pending_s if plan is not None else 1.5)
        self.port.write_bytes(out)
        self._drain_echo(out)
        self.log("  TX %s%s" % (out.hex(" ")[:200], "" if kind is None else "  <-- %s" % kind))
