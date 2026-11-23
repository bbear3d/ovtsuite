# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flasher's trace: one global switch, one file, one size limit.

Tools > Options > Flasher turns it on, names the file and caps its size. While
it is on, every flash or EEPROM read and write (every ECU, every cable) writes
what crosses the wire -- CAN frames, K-line bytes -- and the flasher's own
status lines to that file, so a failed session can be inspected afterwards.
It is off unless the user turns it on.

The file is in the :mod:`ovtsuite.sniff.capture` format (the status lines are
``#`` comments, which the reader skips), so it loads straight back into the Bus
Sniffer. It is appended to from one session to the next; when it reaches the
size limit it is renamed ``<file>.1`` (the older ``.1`` becomes ``.2`` ...),
and only the newest few are kept, so the trace never grows past
``limit x (old files kept + 1)``. Tracing is best effort: a full disk or a
file another program holds shut ends the trace, never the flash.

* :func:`begin` opens a session (or returns None while tracing is off); the
  flasher's worker owns it for one job.
* :class:`FlashTrace` is that session. :meth:`~FlashTrace.tap` wraps a CAN
  channel (:class:`TracedChannel`), :meth:`~FlashTrace.kline_cb` is the
  ``on_trace`` hook of the K-line links, :meth:`~FlashTrace.note` takes a status
  line, and :meth:`~FlashTrace.bridge_trace` has what the 32-bit J2534 bridge
  needs to write its own file (it is another process, so for the length of the
  job its CAN frames go to ``<file>-bridge.<ext>``, limited the same way; when
  the job ends, :meth:`~FlashTrace.close` adds them to the trace and deletes
  that file -- one file is left).
* :class:`TraceFile` is the rotating file.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, Optional

from .. import branding

#: the options (declared in :mod:`ovtsuite.options`, page "Flasher")
KEY_ON = "flash/trace_on"
KEY_FILE = "flash/trace_file"
KEY_MAX_MB = "flash/trace_max_mb"
KEY_KEEP = "flash/trace_keep"

#: the option defaults (the declarations in options.py use these)
DEFAULT_MAX_MB = 25
DEFAULT_KEEP = 3
#: the smallest limit a file is given, whatever it is asked for
MIN_BYTES = 4096
#: unwritten lines are flushed at least this often (and every FLUSH_LINES):
#: not per line -- the write sits in the CAN receive loop, where one slow
#: flush can cost a TP 2.0 acknowledgement
FLUSH_LINES = 64
FLUSH_SECONDS = 0.5
#: J2534 CAN_29BIT_ID, the flag bit in a received message's rx_status
_CAN_29BIT_ID = 0x00000100


# ------------------------------------------------------------------ where
def documents_dir() -> str:
    """The user's Documents folder, even when it is redirected (OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 \
                    and buf.value:                      # CSIDL_PERSONAL
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def default_path() -> str:
    """``Documents\\<app>\\Flash trace\\flash-trace.log``."""
    return os.path.join(documents_dir(), branding.APP_NAME or "OVT",
                        "Flash trace", "flash-trace.log")


def bridge_file(path: str) -> str:
    """The file the 32-bit bridge writes its CAN frames to for ``path``."""
    stem, ext = os.path.splitext(path)
    return stem + "-bridge" + (ext or ".log")


# ------------------------------------------------------------------ the file
class TraceFile:
    """An append-only text file that rotates at ``max_bytes``: the full file
    becomes ``<path>.1``, the older ones move up, and only ``keep`` of them
    stay (``keep`` 0: the full file is just emptied). Safe to call from
    several threads; it never raises -- after a failure it drops lines."""

    def __init__(self, path: str, max_bytes: int, keep: int = DEFAULT_KEEP):
        self.path = os.path.abspath(path)
        self.max_bytes = max(MIN_BYTES, int(max_bytes))
        self.keep = max(0, int(keep))
        self.rotations = 0
        self.error: Optional[str] = None        # why it stopped, if it did
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        self._unflushed = 0
        self._last_flush = time.monotonic()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._open("a")

    # -- writing --------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._fh is not None

    def write(self, line: str) -> None:
        data = line + "\n"
        n = len(data.encode("utf-8"))
        with self._lock:
            if self._fh is None:
                return
            try:
                if self._size and self._size + n > self.max_bytes:
                    self._rotate()
                    if self._fh is None:
                        return
                self._fh.write(data)
                self._size += n
                self._unflushed += 1
                now = time.monotonic()
                if (self._unflushed >= FLUSH_LINES
                        or now - self._last_flush >= FLUSH_SECONDS):
                    self._fh.flush()
                    self._unflushed = 0
                    self._last_flush = now
            except (OSError, ValueError) as ex:
                self._fail(ex)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._unflushed = 0
                except (OSError, ValueError) as ex:
                    self._fail(ex)

    def close(self) -> None:
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except (OSError, ValueError):
                pass

    # -- internals (the lock is held) ---------------------------------------
    def _open(self, mode: str) -> None:
        self._fh = open(self.path, mode, encoding="utf-8", newline="\n")
        try:
            self._size = os.path.getsize(self.path)
        except OSError:
            self._size = 0

    def _fail(self, ex) -> None:
        self.error = str(ex)
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def _old(self, i: int) -> str:
        return "%s.%d" % (self.path, i)

    def _rotate(self) -> None:
        full = self._size
        self._fh.close()
        self._fh = None
        try:
            if self.keep == 0:
                os.remove(self.path)
            else:
                for i in range(self.keep, 0, -1):
                    src = self._old(i - 1) if i > 1 else self.path
                    if os.path.exists(src):
                        os.replace(src, self._old(i))
            # a smaller keep than before leaves older files behind: drop them
            i = self.keep + 1
            while os.path.exists(self._old(i)):
                os.remove(self._old(i))
                i += 1
            self._open("w")
        except OSError:
            # the file will not move (another program has it open): empty it
            # in place, which is still within the limit
            try:
                self._open("w")
            except OSError as ex:
                self._fail(ex)
                return
        self.rotations += 1
        head = "# continued: the previous %d bytes are in %s\n" % (
            full, os.path.basename(self._old(1)) if self.keep else "(not kept)")
        self._fh.write(head)
        self._size = len(head.encode("utf-8"))


# ------------------------------------------------------------------ session
def _stamp() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + (".%03d" % int(t % 1 * 1000))


class FlashTrace:
    """One flash job's use of the trace file."""

    def __init__(self, path: str, max_bytes: int, keep: int, title: str = ""):
        self.file = TraceFile(path, max_bytes, keep)
        self.path = self.file.path
        self.max_bytes = self.file.max_bytes
        self.keep = self.file.keep
        self.frames = 0                     # CAN frames written
        self.kbytes = 0                     # K-line bytes written
        self._bridge_used = False
        self.file.write("# ==== %s flash trace -- %s -- %s ====" % (
            branding.APP_NAME, title or "session",
            time.strftime("%Y-%m-%d %H:%M:%S")))

    # -- what goes in ---------------------------------------------------------
    def note(self, text: str) -> None:
        """A status line (a ``#`` comment: captures skip it)."""
        stamp = _stamp()
        for ln in str(text).splitlines() or [""]:
            self.file.write("# %s %s" % (stamp, ln))

    def can(self, tx: bool, can_id: int, payload, extended: bool = False) -> None:
        ident = ("%08X" % can_id) if extended else ("%03X" % (can_id or 0))
        self.frames += 1
        self.file.write("(%.6f) %s %s#%s" % (
            time.time(), "can-tx" if tx else "can", ident, bytes(payload).hex().upper()))

    def kline(self, direction: str, data) -> None:
        self.kbytes += len(data)
        self.file.write("(%.6f) %s 0#%s" % (
            time.time(), "kraw-tx" if direction == "tx" else "kraw",
            bytes(data).hex().upper()))

    # -- what it plugs into ---------------------------------------------------
    def tap(self, channel) -> "TracedChannel":
        """``channel`` (a CAN channel) that writes every frame to the trace."""
        return TracedChannel(channel, self)

    def kline_cb(self) -> Callable[[str, bytes], None]:
        """The ``on_trace(direction, data)`` hook of the K-line links."""
        def cb(direction: str, data: bytes) -> None:
            try:
                self.kline(direction, data)
            except Exception:
                pass
        return cb

    def bridge_trace(self) -> Dict[str, object]:
        """Keyword arguments for ``open_tp20`` / ``open_iso_tp``: the 32-bit
        bridge writes the CAN frames it carries to its own file, which
        :meth:`close` adds to the trace."""
        self._bridge_used = True
        return {"log_path": bridge_file(self.path),
                "log_limits": {"log_max_bytes": self.max_bytes,
                               "log_keep": self.keep}}

    def merge_bridge(self) -> int:
        """Add the bridge's frames (its file and the older ones it rotated) to
        the trace, oldest first, then delete them; returns how many. Call it
        once the bridge has exited (the file is then closed). Frames from a job
        that never merged (a crash) are picked up too. If the files cannot be
        read or written they are left where they are and the trace says so.
        Only the newest ``max_bytes`` of frames are added: the trace's own
        limit holds, and older frames would only rotate out again."""
        base = bridge_file(self.path)
        files = [p for p in ["%s.%d" % (base, i) for i in range(self.keep + 3, 0, -1)]
                 + [base] if os.path.exists(p)]
        if not files:
            return 0
        n = 0
        try:
            sizes = [os.path.getsize(p) for p in files]
            while len(files) > 1 and sum(sizes) > self.max_bytes:
                files.pop(0)                    # oldest first: these would rotate out
                sizes.pop(0)
            self.file.write("# ---- CAN frames from the 32-bit bridge: %s ----"
                            % os.path.basename(base))
            for p in files:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.rstrip("\r\n")
                        if line and not line.startswith("#"):
                            self.file.write(line)
                            n += 1
        except OSError as ex:
            self.note("(the bridge's CAN frames stay in %s: %s)" % (base, ex))
            return n
        self.frames += n
        for p in [base] + ["%s.%d" % (base, i) for i in range(1, self.keep + 3)]:
            try:
                os.remove(p)
            except OSError:
                pass
        return n

    def close(self) -> None:
        if self._bridge_used or os.path.exists(bridge_file(self.path)):
            try:
                self.merge_bridge()
            except Exception:                   # noqa: BLE001 - best effort
                pass
        self.file.write("# ==== end: %d CAN frames, %d K-line bytes ====" % (
            self.frames, self.kbytes))
        self.file.close()


class TracedChannel:
    """A CAN channel that writes every frame it carries to a trace (or to
    nothing: ``trace`` None still counts ``tx`` and ``rx``, the tally a read
    that "got no response" is diagnosed from). Everything else goes to the
    real channel unchanged -- it only watches the bus, never changes it."""

    def __init__(self, channel, trace: Optional[FlashTrace] = None):
        self._c = channel
        self._trace = trace
        self.protocol = getattr(channel, "protocol", None)
        self.tx = 0
        self.rx = 0

    @property
    def frames(self) -> int:
        return self.tx + self.rx

    def _log(self, tx: bool, can_id, payload, extended: bool = False) -> None:
        if tx:
            self.tx += 1
        else:
            self.rx += 1
        if self._trace is not None:
            try:
                self._trace.can(tx, int(can_id or 0), payload, extended)
            except Exception:
                pass

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        self._log(True, can_id, payload, extended)
        return self._c.write_can(can_id, payload, extended, timeout_ms)

    def write_can_many(self, can_id, payloads, extended=False, timeout_ms=100):
        payloads = list(payloads)
        for p in payloads:
            self._log(True, can_id, p, extended)
        return self._c.write_can_many(can_id, payloads, extended, timeout_ms)

    def write(self, data, flags=0, timeout_ms=100):
        # an ISO15765 / raw message: <4-byte can id><payload>
        d = bytes(data)
        cid = int.from_bytes(d[:4], "big") if len(d) >= 4 else 0
        self._log(True, cid, d[4:])
        return self._c.write(d, flags, timeout_ms)

    def read(self, max_msgs=16, timeout_ms=100):
        msgs = self._c.read(max_msgs, timeout_ms)
        for m in msgs:
            if getattr(m, "is_tx_echo", False):
                continue                    # our own frame looped back: the write logged it
            # the 29-bit flag of a RECEIVED frame is in rx_status (tx_flags belongs to
            # frames we send and is 0 on a received one)
            self._log(False, getattr(m, "can_id", None) or 0,
                      getattr(m, "payload", b"") or b"",
                      bool(getattr(m, "rx_status", 0) & _CAN_29BIT_ID))
        return msgs

    def __getattr__(self, name):            # pass_all, clear_rx, set_config ...
        return getattr(self._c, name)


# ------------------------------------------------------------------ the switch
def settings() -> Dict[str, object]:
    """The options, as :func:`begin` reads them."""
    from .. import options as _opt
    return {"on": bool(_opt.get(KEY_ON)),
            "file": _opt.get(KEY_FILE) or default_path(),
            "max_mb": int(_opt.get(KEY_MAX_MB)),
            "keep": int(_opt.get(KEY_KEEP))}


def begin(title: str, log: Optional[Callable[[str], None]] = None) -> Optional[FlashTrace]:
    """The trace of one flash job, or None while tracing is off (or the file
    cannot be opened: ``log`` is told why, and the job carries on)."""
    try:
        s = settings()
    except Exception:
        return None
    if not s["on"]:
        return None
    try:
        tr = FlashTrace(str(s["file"]), int(s["max_mb"]) * 1024 * 1024,
                        int(s["keep"]), title)
    except OSError as ex:
        if log:
            log("(flash trace off: %s)" % ex)
        return None
    if log:
        log("Flash trace -> %s (up to %d MB, %d older file(s) kept)"
            % (tr.path, int(s["max_mb"]), tr.keep))
    return tr


__all__ = ["KEY_ON", "KEY_FILE", "KEY_MAX_MB", "KEY_KEEP", "DEFAULT_MAX_MB",
           "DEFAULT_KEEP", "default_path", "bridge_file", "documents_dir",
           "TraceFile", "FlashTrace", "TracedChannel", "settings", "begin"]
