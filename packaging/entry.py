# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Frozen entry point for OVT-Suite.

Sets a HiDPI-friendly policy, then hands off to the CLI (which dispatches to
the GUI for `ovtsuite gui ...`, or with no arguments launches the GUI file
picker). A crash log is written next to the executable.
"""

import os
import sys
import traceback


def _tool_for_exe(executable):
    """The standalone tool an executable launches, by its file name
    (``OVTFlasher.exe`` -> ``"flasher"``), or None for the main app."""
    from ovtsuite import branding
    # either separator: the same check runs on a Windows path from anywhere
    name = os.path.splitext(executable.replace("\\", "/").rsplit("/", 1)[-1])[0].lower()
    for tool, exe in branding.TOOL_EXES.items():
        if exe.lower() == name:
            return tool
    return None


def _is_mcp_exe(executable):
    from ovtsuite import branding
    name = os.path.splitext(executable.replace("\\", "/").rsplit("/", 1)[-1])[0]
    return name.lower() == branding.MCP_EXE.lower()


def _main():
    argv = sys.argv[1:]
    # OVTMcp.exe: the MCP server for AI clients. A console exe, because the
    # windowed OVTSuite.exe has no usable stdin/stdout to speak the protocol on.
    if _is_mcp_exe(sys.executable):
        from ovtsuite.cli import main
        return main(["mcp"] + argv)
    # OVTFlasher.exe / OVTSniffer.exe / OVTLogger.exe: the same bundle under
    # another name, so the tool-only installers can ship a launcher that opens
    # nothing but its tool -- arguments are passed to it (a file to write, logs
    # to name cells from), never to the main window.
    tool = _tool_for_exe(sys.executable)
    if tool is not None:
        from ovtsuite.cli import main
        return main(["--" + tool] + argv)
    # bare launch (double-clicked exe) -> open the main window empty. The user
    # loads a binary from File > Open (Ctrl+O) or File > Open Recent; forcing a
    # file dialog up front is what blocked the window from ever opening on its
    # own, and constructing the window around a session here also bypassed the
    # recent-files tracking that File > Open performs.
    if not argv:
        try:
            from ovtsuite.gui import run, HAVE_QT
        except Exception:
            HAVE_QT = False
        if HAVE_QT:
            return run()
        argv = ["--help"]
    from ovtsuite.cli import main
    return main(argv)


_fault_file = None


def _enable_fault_log():
    """Record native crashes. A segfault in Qt, a GL driver or a library mix-up
    kills the process before the ``except`` in :func:`main` can run, so it left
    no trace at all; faulthandler writes the Python stack of every thread at the
    moment of the crash. Kept in the per-user config dir beside the crash log."""
    global _fault_file
    import faulthandler
    dirs = []
    try:
        from ovtsuite.diagnostics import log_path
        dirs.append(os.path.dirname(log_path()))
    except Exception:
        pass
    dirs.append(os.path.expanduser("~"))
    for d in dirs:
        try:
            _fault_file = open(os.path.join(d, "ovtsuite-fault.log"), "w")
            _fault_file.write("# native-crash trace of the last run; empty below "
                              "this line means it did not crash\n")
            _fault_file.flush()
            faulthandler.enable(file=_fault_file, all_threads=True)
            return
        except Exception:
            continue


def main():
    _enable_fault_log()
    try:
        sys.exit(_main() or 0)
    except SystemExit:
        raise
    except Exception:
        for log in _crash_log_candidates():
            try:
                with open(log, "w") as fh:
                    traceback.print_exc(file=fh)
            except Exception:
                continue
            print("crash log written to %s" % log, file=sys.stderr)
            break
        traceback.print_exc()
        _show_crash(traceback.format_exc())
        sys.exit(1)


def _show_crash(text):
    """A windowed build has no console: without this a start that fails
    just does not open, with nothing on screen and nothing on the command
    line. Show the error (best effort -- Qt itself may be what failed)."""
    try:
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        box = QtWidgets.QMessageBox()
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle("Could not start")
        last = text.strip().splitlines()[-1] if text.strip() else "unknown error"
        box.setText("The app could not start:\n\n%s" % last)
        box.setDetailedText(text)
        box.exec()
        del app
    except Exception:
        pass


def _crash_log_candidates():
    """Beside the executable first (where it has always been on Windows), then the
    per-user config dir the diagnostics log uses: an install to /opt or Program
    Files is read-only, and a crash log that cannot be written is lost."""
    name = "ovtsuite-crash.log"
    yield os.path.join(os.path.dirname(sys.executable), name)
    try:
        from ovtsuite.diagnostics import log_path
        yield os.path.join(os.path.dirname(log_path()), name)
    except Exception:
        pass
    yield os.path.join(os.path.expanduser("~"), name)


if __name__ == "__main__":
    main()
