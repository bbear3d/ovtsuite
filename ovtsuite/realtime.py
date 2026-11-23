# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Keep timing-sensitive protocol I/O on time while the app is in the background.

A flash or a log is a stream of small waits (5-75 ms gaps between frames, a
2 ms FTDI latency timer, short spin waits). When the window loses focus the OS
stretches exactly those: macOS App Nap and timer coalescing and a background
QoS, Windows' 15.6 ms timer tick and EcoQoS power throttling, Linux's default
scheduler. A write that was fine in the foreground then fails in the
background. ``realtime_session`` asks each OS not to do that, for as long as
the ``with`` block lasts.

Enter it INSIDE the worker thread's ``run()`` (some settings belong to the
calling thread); helper threads that only poll a device call ``begin_thread()``
at the top of their function and ``end_thread()`` when done.

Everything here is best-effort and ctypes only: every step is in try/except
and a failure degrades to "no change"; nothing in this module may raise or
abort a flash. Sessions are refcounted, so overlapping sessions (a log running
while a flash starts) share ONE process-wide activity and timer request.

Per platform:

* macOS -- ``NSProcessInfo beginActivityWithOptions:reason:`` (UserInitiated |
  LatencyCritical | IdleSystemSleepDisabled, + display sleep disabled) which
  turns off App Nap and timer coalescing; the calling thread gets QoS
  USER_INTERACTIVE. ``OVT_RT_MACH=1`` additionally puts the thread on a Mach
  time-constraint policy (off by default).
* Windows -- ``timeBeginPeriod(1)``, thread priority HIGHEST, EcoQoS opt-out
  for the process (reverted when the last session ends), and keepawake's
  SetThreadExecutionState.
* Linux -- SCHED_RR priority 1 (or a lower nice) when permitted, else nothing.

Priority is for the thread that needs frame timing, not for what it spawns:
on Linux a thread created from a SCHED_RR thread would inherit SCHED_RR, and
a looping helper at real-time priority can starve the GUI, so the policy is
set with SCHED_RESET_ON_FORK (children start at SCHED_OTHER). A plain nice
value cannot be reset that way and is inherited, but it is only ever a mild
-5. On Windows a new thread always starts at normal priority. On macOS a
thread started from a QoS-boosted one inherits that QoS, so spawn helpers
before entering the session, or give them ``begin_thread()`` of their own.

Process-wide requests are tracked per session: a ``keep_awake=False`` session
(timing only) does not block idle system sleep or the display; the activity is
rebuilt when the strongest requirement among the open sessions changes.

Tests replace ``_backend`` (see ``Backend``) to observe the calls.
"""
import contextlib
import os
import sys
import threading

ACTIVITY_LATENCY_CRITICAL = 0xFF00000000              # NSActivityLatencyCritical
ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED = 1 << 20
# NSActivityUserInitiatedAllowingIdleSystemSleep: UserInitiated minus the sleep bit
ACTIVITY_USER_INITIATED_ALLOWING_SLEEP = 0x00FFFFFF & ~ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED
ACTIVITY_USER_INITIATED = ACTIVITY_USER_INITIATED_ALLOWING_SLEEP | ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED
ACTIVITY_IDLE_DISPLAY_SLEEP_DISABLED = 1 << 40
QOS_CLASS_USER_INTERACTIVE = 0x21
THREAD_PRIORITY_HIGHEST = 2

_lock = threading.RLock()
_sessions = 0                  # open time-critical sessions
_awake = 0                     # ...of which also want the machine kept awake
_token = None
_token_awake = False           # whether the live activity was built for _awake > 0
_enabled = os.environ.get("OVT_RT", "1") != "0"
_log = []                      # last few best-effort failures, for describe()


def _note(what: str, ex: BaseException = None):
    try:
        _log.append("%s: %s" % (what, ex) if ex is not None else what)
        del _log[:-20]
    except Exception:
        pass


def _mach_wanted() -> bool:
    return os.environ.get("OVT_RT_MACH", "") == "1"


class Backend:
    """The platform calls. Every method is best-effort and returns a token (or
    None) for the matching ``*_end``; subclass or monkeypatch to observe."""

    # ---- process-wide
    def process_begin(self, why: str, display: bool, system: bool = True):
        """``display``/``system``: also hold off display / idle system sleep."""
        return None

    def process_end(self, token):
        pass

    # ---- the calling thread
    def thread_begin(self):
        return None

    def thread_end(self, state):
        pass


class _MacBackend(Backend):
    def __init__(self):
        import ctypes
        import ctypes.util
        self.c = ctypes
        self.objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        addr = ctypes.cast(self.objc.objc_msgSend, ctypes.c_void_p).value
        P, U64 = ctypes.c_void_p, ctypes.c_uint64
        proto = ctypes.CFUNCTYPE
        self._m0 = proto(P, P, P)(addr)                 # id f(id, SEL)
        self._m1p = proto(P, P, P, P)(addr)             # id f(id, SEL, id)
        self._m1c = proto(P, P, P, ctypes.c_char_p)(addr)
        self._mact = proto(P, P, P, U64, P)(addr)       # id f(id, SEL, u64, id)
        self._mvoid1 = proto(None, P, P, P)(addr)       # void f(id, SEL, id)
        self._mvoid0 = proto(None, P, P)(addr)
        self.sel = lambda n: self.objc.sel_registerName(n)
        self.cls = lambda n: self.objc.objc_getClass(n)

    def process_begin(self, why, display, system=True):
        pool = self._m0(self._m0(self.cls(b"NSAutoreleasePool"), self.sel(b"alloc")),
                        self.sel(b"init"))
        try:
            info = self._m0(self.cls(b"NSProcessInfo"), self.sel(b"processInfo"))
            reason = self._m1c(self.cls(b"NSString"), self.sel(b"stringWithUTF8String:"),
                               why.encode("utf-8", "replace"))
            opts = ACTIVITY_USER_INITIATED_ALLOWING_SLEEP | ACTIVITY_LATENCY_CRITICAL
            if system:
                opts |= ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED
            if display:
                opts |= ACTIVITY_IDLE_DISPLAY_SLEEP_DISABLED
            tok = self._mact(info, self.sel(b"beginActivityWithOptions:reason:"), opts, reason)
            if tok:
                tok = self._m0(tok, self.sel(b"retain"))
            return tok or None
        finally:
            self._mvoid0(pool, self.sel(b"drain"))

    def process_end(self, token):
        if not token:
            return
        info = self._m0(self.cls(b"NSProcessInfo"), self.sel(b"processInfo"))
        self._mvoid1(info, self.sel(b"endActivity:"), token)
        self._mvoid0(token, self.sel(b"release"))

    def thread_begin(self):
        c = self.c
        prev = c.c_uint(0)
        prio = c.c_int(0)
        lib = self.libsys
        lib.pthread_self.restype = c.c_void_p
        try:
            lib.pthread_get_qos_class_np(c.c_void_p(lib.pthread_self()), c.byref(prev), c.byref(prio))
        except Exception:
            pass
        lib.pthread_set_qos_class_self_np.argtypes = [c.c_uint, c.c_int]
        lib.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
        mach = self._mach_begin() if _mach_wanted() else None
        return (prev.value, mach)

    def _mach_begin(self):
        c = self.c

        class TBI(c.Structure):
            _fields_ = [("numer", c.c_uint32), ("denom", c.c_uint32)]

        class TC(c.Structure):
            _fields_ = [("period", c.c_uint32), ("computation", c.c_uint32),
                        ("constraint", c.c_uint32), ("preemptible", c.c_int32)]
        lib = self.libsys
        tb = TBI()
        lib.mach_timebase_info(c.byref(tb))
        ticks = lambda ms: int(ms * 1e6 * tb.denom / max(tb.numer, 1))   # noqa: E731
        pol = TC(ticks(5), ticks(0.5), ticks(2), 1)
        lib.mach_thread_self.restype = c.c_uint
        thr = lib.mach_thread_self()
        lib.thread_policy_set.argtypes = [c.c_uint, c.c_int, c.c_void_p, c.c_uint]
        lib.thread_policy_set(thr, 2, c.byref(pol), 4)          # THREAD_TIME_CONSTRAINT_POLICY
        return thr

    def thread_end(self, state):
        prev, mach = state or (0, None)
        c = self.c
        if mach is not None:
            self.libsys.thread_policy_set.argtypes = [c.c_uint, c.c_int, c.c_void_p, c.c_uint]
            self.libsys.thread_policy_set(mach, 1, None, 0)     # THREAD_STANDARD_POLICY
        if prev:
            self.libsys.pthread_set_qos_class_self_np.argtypes = [c.c_uint, c.c_int]
            self.libsys.pthread_set_qos_class_self_np(prev, 0)


class _WinBackend(Backend):
    def __init__(self):
        import ctypes
        self.c = ctypes
        self.winmm = ctypes.windll.winmm
        self.k32 = ctypes.windll.kernel32

    def _power_throttling(self, control, state):
        c = self.c

        class PPTS(c.Structure):
            _fields_ = [("Version", c.c_ulong), ("ControlMask", c.c_ulong), ("StateMask", c.c_ulong)]
        st = PPTS(1, control, state)
        self.k32.GetCurrentProcess.restype = c.c_void_p
        self.k32.SetProcessInformation.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_ulong]
        return self.k32.SetProcessInformation(self.k32.GetCurrentProcess(), 4,
                                              c.byref(st), c.sizeof(st))

    def process_begin(self, why, display, system=True):
        self.winmm.timeBeginPeriod(1)
        try:    # opt this process out of EcoQoS / power throttling
            self._power_throttling(1, 0)          # execution speed control, not throttled
        except Exception as ex:
            _note("EcoQoS opt-out", ex)
        return True

    def process_end(self, token):
        if token:
            self.winmm.timeEndPeriod(1)
            try:    # back to what the system decides (control mask 0)
                self._power_throttling(0, 0)
            except Exception as ex:
                _note("EcoQoS revert", ex)

    def thread_begin(self):
        c = self.c
        self.k32.GetCurrentThread.restype = c.c_void_p
        self.k32.SetThreadPriority.argtypes = [c.c_void_p, c.c_int]
        h = self.k32.GetCurrentThread()
        prev = self.k32.GetThreadPriority(c.c_void_p(h))
        self.k32.SetThreadPriority(c.c_void_p(h), THREAD_PRIORITY_HIGHEST)
        return prev

    def thread_end(self, state):
        c = self.c
        self.k32.GetCurrentThread.restype = c.c_void_p
        self.k32.SetThreadPriority.argtypes = [c.c_void_p, c.c_int]
        if state is not None and state != 0x7FFFFFFF:
            self.k32.SetThreadPriority(c.c_void_p(self.k32.GetCurrentThread()), int(state))


class _LinuxBackend(Backend):
    def thread_begin(self):
        try:
            # pid 0 = the calling thread only; RESET_ON_FORK so threads it
            # starts are SCHED_OTHER, not real-time (see the module docstring)
            flags = os.SCHED_RR | getattr(os, "SCHED_RESET_ON_FORK", 0)
            os.sched_setscheduler(0, flags, os.sched_param(1))
            return ("rr", None)
        except Exception:
            pass
        try:
            tid = threading.get_native_id()
            old = os.getpriority(os.PRIO_PROCESS, tid)
            os.setpriority(os.PRIO_PROCESS, tid, max(old - 5, -20))
            return ("nice", old)
        except Exception:
            return None

    def thread_end(self, state):
        if not state:
            return
        kind, old = state
        try:
            if kind == "rr":
                os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
            else:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), old)
        except Exception:
            pass


def _make_backend() -> Backend:
    try:
        if sys.platform == "darwin":
            return _MacBackend()
        if sys.platform.startswith("win"):
            return _WinBackend()
        if sys.platform.startswith("linux"):
            return _LinuxBackend()
    except Exception as ex:
        _note("backend", ex)
    return Backend()


_backend = _make_backend()


def _rebuild(why: str):
    """(Re)make the process-wide activity for the current requirements."""
    global _token, _token_awake
    if _token is not None:
        tok, _token = _token, None
        try:
            _backend.process_end(tok)
        except Exception as ex:
            _note("process_end", ex)
    want = _awake > 0
    try:
        _token = _backend.process_begin(why, want, want)
    except Exception as ex:
        _token = None
        _note("process_begin", ex)
    _token_awake = want


def _acquire(why: str, awake: bool):
    """Count a session. The first one starts the activity; a later one that
    needs the machine awake when the live activity does not rebuilds it."""
    global _sessions, _awake
    with _lock:
        _sessions += 1
        if awake:
            _awake += 1
        if not _enabled:
            return
        if _sessions == 1 or (_awake > 0) != _token_awake:
            _rebuild(why)


def _release(awake: bool = True, why: str = ""):
    global _sessions, _awake, _token, _token_awake
    with _lock:
        _sessions = max(_sessions - 1, 0)
        if awake:
            _awake = max(_awake - 1, 0)
        if _sessions == 0:
            _awake = 0
            tok, _token, _token_awake = _token, None, False
            try:
                _backend.process_end(tok)
            except Exception as ex:
                _note("process_end", ex)
        elif _enabled and (_awake > 0) != _token_awake:
            _rebuild(why)           # the strongest requirement went away


_tls = threading.local()


def begin_thread():
    """QoS / priority for the calling thread only (a helper that polls a device).
    Pair with ``end_thread()``; nests per thread; never raises."""
    try:
        depth = getattr(_tls, "depth", 0)
        _tls.depth = depth + 1
        if depth == 0 and _enabled:
            _tls.state = _backend.thread_begin()
    except Exception as ex:
        _note("thread_begin", ex)


def end_thread():
    try:
        depth = getattr(_tls, "depth", 0)
        if depth <= 0:
            return
        _tls.depth = depth - 1
        if depth == 1:
            state, _tls.state = getattr(_tls, "state", None), None
            if _enabled:
                _backend.thread_end(state)
    except Exception as ex:
        _note("thread_end", ex)


@contextlib.contextmanager
def realtime_session(why: str = "OVT-Suite is talking to a control unit", *,
                     keep_awake: bool = True, time_critical: bool = True):
    """Hold the OS to prompt timing (and, with ``keep_awake``, off sleep) for the block."""
    stack = contextlib.ExitStack()
    try:
        if keep_awake:
            try:
                from .flash import keepawake
                stack.enter_context(keepawake.prevent_sleep(why))
            except Exception as ex:
                _note("keepawake", ex)
        if time_critical:
            _acquire(why, keep_awake)
            stack.callback(_release, keep_awake, why)
            begin_thread()
            stack.callback(end_thread)
    except Exception as ex:
        _note("session", ex)
    try:
        yield
    finally:
        try:
            stack.close()
        except Exception as ex:
            _note("session close", ex)


def describe() -> dict:
    """Snapshot for tests and diagnostics."""
    with _lock:
        return {"platform": sys.platform, "enabled": _enabled, "sessions": _sessions,
                "active": _token is not None, "backend": type(_backend).__name__,
                "thread_depth": getattr(_tls, "depth", 0), "mach": _mach_wanted(),
                "notes": list(_log)}
