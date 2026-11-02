# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One CAN channel, several TP 2.0 channels at once.

:class:`ovtsuite.flash.tp20_transport.Tp20Channel` reads the device
itself and drops every frame that is not its own, so two of them on one
CAN channel would eat each other's frames. :class:`CanMux` owns the
physical channel instead: a reader thread drains the device continuously
and hands each frame to the :class:`MuxPort` whose id set contains its CAN
id; a port is what a ``Tp20Channel`` is given as its ``can``. It has the
same surface (``read`` / ``write_can`` / ``pass_ids`` / ``pass_all`` /
``clear_rx``), so the transport does not know it is sharing.

Two consequences, both deliberate:

* the device runs with a pass-all filter and the *mux* does the filtering.
  The hardware filter the flasher installs exists to keep a live bus from
  overflowing the device buffer while nobody reads; here somebody always
  reads. It also sidesteps the per-device filter limits a union of several
  channels' ids would hit;
* a port's ``read`` with a zero timeout still waits a couple of
  milliseconds on the reader's condition when its queue is empty, so a
  channel polling for its reply sleeps instead of spinning.

Each parallel channel asks the module to send on its own CAN id
(``listen_id`` 0x300, 0x301, ...), which is what the TP 2.0 channel setup
is for; the setup reply tells the port which id the module actually took,
and a module that ignores the request and answers on an id another port
already holds is reported rather than guessed around.

Qt-free; the ports are used from any number of worker threads.
"""
import threading
import time
from typing import Any, Callable, List, Optional, Set

from ..flash.transport import KLineError

Log = Callable[[str], None]

#: a port poll with timeout 0 still waits up to this long for the reader
MIN_WAIT_MS = 2.0
#: the reader blocks in the driver this long per empty poll (ctypes releases
#: the GIL inside PassThruReadMsgs), and sleeps this long after a poll that
#: returned at once (a channel object that does not block when empty)
POLL_MS = 1
IDLE_S = 0.001
#: after a frame arrived the reader polls tightly for this long (a reply's
#: further frames, our ACK's answer), then relaxes to RELAX_S between polls
TIGHT_S = 0.025
RELAX_S = 0.002
#: TP 2.0 ids a tester asks the modules to send on, one per open channel
LISTEN_BASE = 0x300
LISTEN_COUNT = 16


class MuxClosed(KLineError):
    """The shared channel is gone (device error or closed)."""


class MuxPort:
    """One subscriber's view of the shared CAN channel: frames on its ids,
    writes under the mux lock."""

    def __init__(self, mux: "CanMux", name: str = ""):
        self._mux = mux
        self.name = name
        self.ids: Optional[Set[int]] = set()        # None = every frame
        self._q: List[Any] = []
        self.closed = False
        #: set when this port asked for an id another port already holds: the
        #: module answered on a channel id that is not its own, and its frames
        #: cannot be told from the other module's. Reads on this port raise.
        self.clash: str = ""
        #: J2534 channel attributes some callers look at
        self.protocol = getattr(mux.can, "protocol", None)

    # ---- the channel surface Tp20Channel uses
    def pass_ids(self, ids, extended: bool = False):
        self.ids = set(int(i) for i in ids)
        self._mux._ids_changed(self)

    def pass_all(self, extended: bool = False):
        self.ids = None
        self._mux._ids_changed(self)

    def clear_filters(self):
        self.ids = set()

    def clear_rx(self):
        with self._mux._cv:
            self._q.clear()

    def set_config(self, **kw):
        pass

    def set_baud(self, baud):
        pass

    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        self._mux.write_can(can_id, payload, extended, timeout_ms)

    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[Any]:
        mux = self._mux
        if self.clash:
            raise KLineError("TP 2.0: %s answers on CAN id %s -- its frames cannot be told apart from that "
                             "module's; connect to these two modules one after the other" % (self.name, self.clash))
        with mux._cv:
            if not self._q and mux.error is None and not self.closed:
                mux._cv.wait(max(float(timeout_ms), MIN_WAIT_MS) / 1000.0)
            out, self._q = self._q[:max_msgs], self._q[max_msgs:]
        if not out and mux.error is not None:
            raise MuxClosed("CAN channel: %s" % mux.error)
        if not out and self.closed:
            raise MuxClosed("CAN channel: port %s is closed" % self.name)
        return out

    def close(self):
        self._mux.unsubscribe(self)

    # ---- bookkeeping
    def _accepts(self, can_id: int) -> bool:
        if self.clash:
            return False                 # the frames stay with the port that held the id first
        return self.ids is None or can_id in self.ids


class CanMux:
    """The owner of one physical CAN channel (``write_can`` / ``read`` /
    ``pass_all``), shared by :class:`MuxPort` subscribers."""

    def __init__(self, can, log: Optional[Log] = None, start: bool = True):
        self.can = can
        self._log = log or (lambda s: None)
        self._cv = threading.Condition()
        self._wlock = threading.Lock()
        self._ports: List[MuxPort] = []
        self.error: Optional[Exception] = None
        self._stop = False
        self.frames = 0                 # frames delivered
        self.dropped = 0                # frames nobody wanted
        self._clash_logged: Set[int] = set()
        try:
            can.pass_all()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._run, name="ovt-diag-canmux", daemon=True)
        if start:
            self._thread.start()

    # ---- subscribers
    def subscribe(self, name: str = "") -> MuxPort:
        p = MuxPort(self, name)
        with self._cv:
            self._ports.append(p)
        return p

    def unsubscribe(self, port: MuxPort):
        with self._cv:
            port.closed = True
            port._q.clear()
            if port in self._ports:
                self._ports.remove(port)
            self._cv.notify_all()

    def _ids_changed(self, port: MuxPort):
        """When a port asks for an id another port already holds (a module
        that answered the channel setup on an id that is not the one it was
        given), mark the newcomer: its reads raise, its channel open fails
        and the module is recorded as not reached, while the port that held
        the id first keeps its frames. Guessing which module a frame came
        from would corrupt both dialogs silently."""
        if port.ids is None:
            return
        with self._cv:
            for other in self._ports:
                if other is port or other.ids is None or other.clash:
                    continue
                for cid in sorted(port.ids & other.ids):
                    port.clash = "0x%03X (held by %s)" % (cid, other.name)
                    if cid in self._clash_logged:
                        continue
                    self._clash_logged.add(cid)
                    self._log("TP 2.0: CAN id 0x%03X is used by both %s and %s -- their frames cannot be told "
                              "apart; run those two modules one after the other" % (cid, port.name, other.name))
                    break
            if port.clash:
                self._cv.notify_all()

    @property
    def ports(self) -> int:
        return len(self._ports)

    # ---- writes
    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        if self.error is not None:
            raise MuxClosed("CAN channel: %s" % self.error)
        with self._wlock:
            self.can.write_can(can_id, payload, extended=extended, timeout_ms=timeout_ms)

    # ---- the reader
    def _run(self):
        # The mux is the channel's only reader, so it can block in the driver
        # (PassThruReadMsgs with a timeout releases the GIL) instead of
        # polling: a Python spin here would peg a core and starve the GUI and
        # the channel threads for as long as the bus carries traffic.
        while not self._stop:
            t0 = time.perf_counter()
            try:
                got = self.can.read(32, POLL_MS)
            except Exception as ex:
                with self._cv:
                    self.error = ex
                    self._cv.notify_all()
                self._log("CAN channel read failed: %s" % ex)
                return
            if got:
                self._dispatch(got)
                continue
            if time.perf_counter() - t0 < POLL_MS / 2000.0:
                time.sleep(IDLE_S)       # the channel returned at once: do not spin

    def _dispatch(self, msgs):
        with self._cv:
            for m in msgs:
                cid = getattr(m, "can_id", None)
                if cid is None:
                    continue
                taken = False
                for p in self._ports:
                    if p._accepts(cid):
                        p._q.append(m)
                        taken = True
                if taken:
                    self.frames += 1
                else:
                    self.dropped += 1
            self._cv.notify_all()

    def pump_once(self):
        """Test hook: one reader pass on the current thread (for a mux built
        with ``start=False``)."""
        got = self.can.read(32, 0)
        if got:
            self._dispatch(got)

    def close(self):
        self._stop = True
        with self._cv:
            if self.error is None:
                self.error = MuxClosed("closed")
            for p in self._ports:
                p.closed = True
            self._cv.notify_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(1.0)


class ListenIds:
    """Hands out distinct TP 2.0 listen ids (0x300...) to parallel channels
    and takes them back. Round-robin: an id handed back goes to the end of
    the line, so a module whose channel died without a disconnect (and may
    still send a channel test on it) is not given the next channel's id."""

    def __init__(self, base: int = LISTEN_BASE, count: int = LISTEN_COUNT):
        self._free = list(range(base, base + count))
        self._lock = threading.Lock()

    def take(self) -> int:
        with self._lock:
            if not self._free:
                raise KLineError("no free TP 2.0 listen id: too many channels open at once")
            return self._free.pop(0)

    def give(self, cid: int):
        with self._lock:
            if cid not in self._free:
                self._free.append(cid)

    @property
    def free(self) -> List[int]:
        with self._lock:
            return sorted(self._free)


__all__ = ["CanMux", "MuxPort", "MuxClosed", "ListenIds", "LISTEN_BASE"]
