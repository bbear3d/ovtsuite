# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Running several capture sources at once (a K-line cable and a CAN device,
say): a shared wall clock, a device clock that follows it without drifting,
and a small reorder window that merges the sources' records by time.

* :class:`SharedClock` -- one epoch (``time.time()`` at start) plus
  ``perf_counter`` offsets, so every source stamps on the same monotonic
  timeline whatever the system clock does mid-run.
* :class:`DeviceClock` -- maps a J2534 device's 32-bit microsecond timestamps
  onto the shared clock.  Anchoring once at the first frame lets the device's
  crystal drift away from the PC's over a long capture, so the anchor is
  nudged toward the shared clock as frames arrive (the smallest delay seen in
  a window is the best estimate of the read latency).  The 32-bit counter
  wraps every ~71 minutes; that is unwrapped here.  A device that gives no
  usable timestamp (the OpenPort's K-line packets carry 0) has no hardware
  clock to map: its records are stamped with the host time of the read.
* :class:`Reorderer` -- sources deliver in bursts at different delays, so
  records are held for a short window and released in time order.
"""
import heapq
import itertools
import time
from typing import Callable, List, Optional

REORDER_WINDOW_S = 0.10


class SharedClock:
    def __init__(self, wall: Optional[float] = None, perf: Optional[Callable[[], float]] = None):
        self._perf = perf or time.perf_counter
        self.wall0 = time.time() if wall is None else wall
        self.perf0 = self._perf()

    def now(self) -> float:
        return self.wall0 + (self._perf() - self.perf0)


class DeviceClock:
    """Device timestamp (microseconds, 32-bit wrapping) -> shared-clock seconds."""
    WRAP = 1 << 32
    WINDOW_S = 2.0               # re-anchor this often

    def __init__(self):
        self._ts0 = None
        self._t0 = 0.0
        self._last_raw = 0
        self._wraps = 0
        self._win_start = None
        self._win_min = None
        self._last_u = None          # last raw timestamp seen, to spot a stuck counter
        self._same_n = 0             # how many in a row repeated it
        self._same_since = 0.0       # host time the run of repeats began
        self.no_clock = False        # timestamps are 0 / not advancing: use host time

    STUCK_N = 4                      # repeats ...
    STUCK_S = 0.25                   # ... over at least this long = no clock

    def _unwrap(self, ts: int) -> int:
        ts = int(ts) & (self.WRAP - 1)
        if self._ts0 is not None and ts < self._last_raw and self._last_raw - ts > self.WRAP // 2:
            self._wraps += 1
        self._last_raw = ts
        return ts + self._wraps * self.WRAP

    def map(self, ts: int, now: float) -> float:
        """``ts``: device microseconds of a message; ``now``: shared-clock time
        at which the batch holding it was read (an upper bound for it)."""
        u = self._unwrap(ts)
        if self._last_u is not None and u == self._last_u:
            self._same_n += 1
            if u == 0 or (self._same_n >= self.STUCK_N and now - self._same_since > self.STUCK_S):
                self.no_clock = True
        else:
            if self.no_clock:                    # it moves again: anchor afresh
                self._ts0 = None
                self.no_clock = False
            self._same_n = 0
            self._same_since = now
        self._last_u = u
        if self.no_clock:
            return now
        if self._ts0 is None:
            self._ts0, self._t0 = u, now
            self._win_start = now
        t = self._t0 + (u - self._ts0) / 1e6
        lag = now - t
        if self._win_min is None or lag < self._win_min:
            self._win_min = lag
        if now - self._win_start >= self.WINDOW_S:
            # move half-way toward "the fastest frame arrived with zero delay"
            self._t0 += self._win_min * 0.5
            self._win_start, self._win_min = now, None
        return self._t0 + (u - self._ts0) / 1e6


class Reorderer:
    """Holds records for ``window`` seconds and releases them sorted by time."""

    def __init__(self, window: float = REORDER_WINDOW_S):
        self.window = window
        self._heap: list = []
        self._seq = itertools.count()

    def __len__(self):
        return len(self._heap)

    def add(self, recs):
        for r in recs:
            heapq.heappush(self._heap, (r.t, next(self._seq), r))

    def release(self, now: float) -> List:
        """Records at least ``window`` old as of ``now`` (sorted)."""
        out = []
        limit = now - self.window
        while self._heap and self._heap[0][0] <= limit:
            out.append(heapq.heappop(self._heap)[2])
        return out

    def drain(self) -> List:
        out = []
        while self._heap:
            out.append(heapq.heappop(self._heap)[2])
        return out
