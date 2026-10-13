# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Make a hang or a crash leave a trace.

The GUI had no logging, no ``sys.excepthook`` and no ``faulthandler``, so when
it stopped responding there was nothing to look at afterwards -- the only
report possible was "it hung", which is not something anyone can act on.

Three mechanisms, all writing to one file next to the app's settings:

* **faulthandler** catches a hard crash (a fault inside Qt, a stack overflow)
  and writes the C-level traceback as the process dies.
* **an excepthook** catches a Python exception that escapes a Qt slot. Qt does
  not propagate those -- by default they print to a console the packaged build
  does not have, and the app carries on in a half-broken state.
* **a watchdog** is the one that answers "it hung": the GUI thread bumps a
  counter on a timer, and a daemon thread that sees the counter stop moving
  dumps every thread's stack. A hang then names the line it is stuck on.

Nothing here changes behaviour when everything is working; the watchdog thread
is a sleeping daemon and the timer is one integer increment a second.
"""
from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import traceback

#: Seconds the GUI thread may be unresponsive before stacks are dumped. Long
#: enough that a legitimately slow load (a ZF 6HP match is ~10 s) is not
#: reported, short enough to catch a hang while the user is still watching.
STALL_SECONDS = 30.0

#: Once stalled, dump this many times, this far apart. One snapshot cannot
#: tell a runaway loop from a deadlock; three can. If the stack moves between
#: them the app is spinning, if it is identical it is blocked, and the memory
#: line beside each says whether something is also allocating -- which is the
#: difference between a loop that will finish and one that will die.
STALL_DUMPS = 3
STALL_INTERVAL = 10.0

_TICK = 1.0

_log_path = None
_log_file = None
_last_dump = 0.0


def log_path() -> str:
    """Where the diagnostic log lives, beside the app's other user data."""
    global _log_path
    if _log_path is None:
        try:
            from .recent import _config_dir
            base = _config_dir()          # same place as the recent-files list
        except Exception:
            base = os.path.join(os.path.expanduser("~"), ".ovtsuite")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.path.expanduser("~")
        _log_path = os.path.join(base, "diagnostics.log")
    return _log_path


def _open_log():
    global _log_file
    if _log_file is None:
        try:
            _log_file = open(log_path(), "a", buffering=1, encoding="utf-8",
                             errors="replace")
        except Exception:
            _log_file = sys.stderr
    return _log_file


def memory_mb():
    """Working set in MB right now, or None where it cannot be read cheaply.

    Current, not peak: the question this answers beside a stall dump is
    whether the process is *still* allocating, which a high-water mark cannot
    say.
    """
    try:
        if os.name == "nt":
            return _memory_mb_windows()
        # ru_maxrss is the high-water mark, so prefer the live figure.
        with open("/proc/self/statm", "rb") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0)
    except Exception:
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return rss / 1024.0 if sys.platform != "darwin" \
                else rss / (1024.0 ** 2)
        except Exception:
            return None


def _memory_mb_windows():
    """GetProcessMemoryInfo, with the handle declared.

    Written once without argtypes, which is why the first stall dump off a
    real machine said "memory 0 MB": GetCurrentProcess returns the
    pseudo-handle (HANDLE)-1, ctypes' default restype is a 32-bit int, and
    passing that truncated value where a 64-bit HANDLE is expected fails the
    call. The return value was not checked either, so the failure was silent
    and the untouched WorkingSetSize read as zero.
    """
    import ctypes
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    # psapi.dll forwards to kernel32 on current Windows; K32... is the
    # direct export where the forwarder is absent.
    try:
        api = ctypes.WinDLL("psapi", use_last_error=True)
        fn = api.GetProcessMemoryInfo
    except (OSError, AttributeError):
        fn = k32.K32GetProcessMemoryInfo
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    fn.restype = wintypes.BOOL

    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return pmc.WorkingSetSize / (1024.0 * 1024.0)


def write(text: str) -> None:
    fh = _open_log()
    try:
        fh.write("\n===== %s  %s\n%s\n"
                 % (datetime.datetime.now().isoformat(timespec="seconds"),
                    os.path.basename(sys.argv[0] or "ovtsuite"), text))
        fh.flush()
    except Exception:
        pass


def _excepthook(exc_type, exc, tb):
    write("Unhandled exception:\n"
          + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.__excepthook__(exc_type, exc, tb)


class _Watchdog(threading.Thread):
    """Dump every thread's stack when the GUI thread stops ticking."""

    def __init__(self, stall=STALL_SECONDS):
        super().__init__(name="ovt-watchdog", daemon=True)
        self.stall = stall
        self.beat = 0
        self._seen = -1
        self._since = 0.0
        self._stop = threading.Event()

    def tick(self):
        self.beat += 1

    def run(self):
        import time
        while not self._stop.wait(_TICK):
            if self.beat != self._seen:
                self._seen, self._since = self.beat, time.time()
                continue
            if self._since and time.time() - self._since > self.stall:
                self._since = time.time()          # once per stall window
                self._dump_series()

    def _dump_series(self):
        """Snapshot the stacks several times so a spin is distinguishable
        from a block, with memory beside each."""
        import time
        for i in range(STALL_DUMPS):
            mem = memory_mb()
            buf = []
            for tid, frame in sys._current_frames().items():
                buf.append("--- thread %s" % tid)
                buf.extend(traceback.format_stack(frame))
            write("UI unresponsive for ~%.0fs -- snapshot %d of %d%s\n%s"
                  % (self.stall + i * STALL_INTERVAL, i + 1, STALL_DUMPS,
                     "" if mem is None else "  (memory %.0f MB)" % mem,
                     "".join(buf)))
            if i + 1 < STALL_DUMPS and self._stop.wait(STALL_INTERVAL):
                return


def install(app=None):
    """Turn all three on. Safe to call twice; returns the log path."""
    global _last_dump
    try:
        faulthandler.enable(file=_open_log())
    except Exception:
        pass
    if sys.excepthook is not _excepthook:
        sys.excepthook = _excepthook
    try:
        threading.excepthook = lambda a: write(
            "Unhandled exception in thread %s:\n" % a.thread_name
            + "".join(traceback.format_exception(
                a.exc_type, a.exc_value, a.exc_traceback)))
    except Exception:
        pass
    if app is not None:
        try:
            from PySide6 import QtCore
            dog = _Watchdog()
            dog.start()
            timer = QtCore.QTimer(app)
            timer.timeout.connect(dog.tick)
            timer.start(int(_TICK * 1000))
            app._ovt_watchdog = (dog, timer)          # keep them alive
        except Exception:
            pass
    return log_path()
