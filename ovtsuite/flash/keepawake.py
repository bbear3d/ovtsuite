# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Keep the computer from sleeping / blanking the display while it is talking
to a control unit: a flash read or write, or a connected data-logging session.

Bug report, 2026-09-21: a real ZF6HP read timed out about 8 seconds after
Windows blanked the screen on a 5-minute idle timer -- nothing in the GUI
worker asked Windows to stay awake, so a long read or write (several
minutes, see zf6hp-write-speed.md / zf6hp-flash-transport.md) is exactly
the kind of "no mouse/keyboard activity" stretch the idle timer is meant
to catch, and once the display and then the system suspend, the J2534
device and its USB link stop being serviced and the transport times out.
2026-09-30: the data logger has the same exposure -- a drive log is many
minutes with the laptop untouched on the passenger seat -- so its worker
holds the same lock for as long as it is connected (datalog/gui_log.py).

How, per platform (all best-effort: failing to hold off sleep must never
abort a flash or a log, so every failure degrades to "no hold"):

* Windows -- kernel32 ``SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)``. The state belongs to the
  CALLING THREAD, so enter the context manager on the worker thread that
  does the talking (both callers do, in ``QThread.run``).
* macOS -- ``caffeinate -d -i -s -w <our pid>``: display, idle and (on AC)
  system sleep held while the child runs; ``-w`` also ends it if OVT-Suite
  dies, so a crash can never leave the Mac unable to sleep.
* Linux -- ``systemd-inhibit --what=idle:sleep`` around a ``sleep`` child;
  logind drops the inhibitor when that child is killed. Where systemd isn't
  present the call simply isn't made.
"""
import contextlib
import os
import shutil
import subprocess
import sys

from .. import branding

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

WHY = "%s is talking to a control unit" % branding.APP_NAME


def _hold_command(why: str):
    """The helper process that holds the lock on macOS / Linux, or None."""
    if sys.platform == "darwin":
        exe = shutil.which("caffeinate") or ("/usr/bin/caffeinate"
                                             if os.path.exists("/usr/bin/caffeinate") else None)
        return [exe, "-d", "-i", "-s", "-w", str(os.getpid())] if exe else None
    if sys.platform.startswith("linux"):
        exe = shutil.which("systemd-inhibit")
        sleeper = shutil.which("sleep")
        if exe and sleeper:
            return [exe, "--what=idle:sleep", "--who=" + branding.APP_NAME, "--why=" + why,
                    "--mode=block", sleeper, "infinity"]
    return None


@contextlib.contextmanager
def prevent_sleep(why: str = WHY):
    """Hold off idle/system sleep and display shutoff for the ``with`` block,
    and always release it on exit -- success, exception or cancel."""
    if sys.platform.startswith("win"):
        kernel32 = None
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        except Exception:
            kernel32 = None
        try:
            yield
        finally:
            if kernel32 is not None:
                try:
                    kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                except Exception:
                    pass
        return

    proc = None
    cmd = _hold_command(why)
    if cmd:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True)
        except Exception:
            proc = None
    try:
        yield
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
