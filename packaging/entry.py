# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Frozen entry point for OVT-Suite.

Sets a HiDPI-friendly policy, then hands off to the CLI (which dispatches to
the GUI for `ovtsuite gui ...`, or with no arguments launches the GUI file
picker). A crash log is written next to the executable.

``OVTSuite.exe --tool <name> [args...]`` runs one standalone tool instead:
what the Windows launchers (OVTFlasher.exe, ..., OVTMcp.exe; ovtsuite/
launcher.py) start. ``--apply-update`` is the delta-update helper.
"""

import os
import sys
import traceback


def _tool_from_argv(argv):
    """``(tool, rest)`` for ``OVTSuite.exe --tool <name> [args...]`` -- what
    the launchers OVTFlasher.exe ... OVTMcp.exe start (ovtsuite/launcher.py,
    packaging/launcher/Launcher.cs) -- else ``(None, argv)``. Raises
    launcher.UnknownTool."""
    from ovtsuite import launcher
    return launcher.split_tool_argv(argv)


def _main():
    from ovtsuite import launcher
    try:
        tool, argv = _tool_from_argv(sys.argv[1:])
    except launcher.UnknownTool as ex:
        _report_usage_error(str(ex))
        return 2
    launcher.set_current(tool)
    # OVTFlasher.exe / OVTSniffer.exe / OVTLogger.exe / OVTSimulator.exe: the
    # tool alone, with no main window -- the launcher's arguments are passed to
    # it (a file to write, logs to name cells from). OVTMcp.exe: the MCP server
    # for AI clients, on the stdin/stdout the console launcher hands down.
    # The tool's AppUserModelID and icon are set by its window code from the
    # tool name (gui._run_tool), as when the tool had an exe of its own.
    if tool is not None:
        from ovtsuite.cli import main
        return main(launcher.cli_argv(tool, argv))
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


_app_mutex = None


def _hold_app_mutex():
    """Windows: hold the named mutex the installers' AppMutex looks for, so
    setup (an update included) waits until every copy of the app and its
    tools has closed. Released by Windows when the process exits."""
    global _app_mutex
    try:
        from ovtsuite.updates import hold_app_mutex
        _app_mutex = hold_app_mutex()
    except Exception:
        pass


def _apply_update(argv):
    """``OVTSuite.exe --apply-update <plan.json>``: the delta-update helper
    (ovtsuite/delta_update.py), started by the app from a temporary copy of
    its folder. It must NOT hold the app mutex -- it waits for every copy of
    the app to release it -- and opens no window."""
    from ovtsuite.delta_update import helper_main
    return helper_main(argv)


def _report_usage_error(message):
    """A bad --tool: on stderr, and on screen (a windowed exe has no console)."""
    print("%s: %s" % (os.path.basename(sys.executable), message), file=sys.stderr)
    if getattr(sys, "frozen", False):
        _show_crash(message, title="Not a tool")


def _is_mcp(argv):
    try:
        from ovtsuite import launcher
        return launcher.split_tool_argv(argv)[0] == launcher.MCP
    except Exception:
        return False


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--apply-update":
        sys.exit(_apply_update(sys.argv[2:]))
    _enable_fault_log()
    _hold_app_mutex()
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
        # the MCP server runs headless for an AI client: stderr is its log
        if not _is_mcp(sys.argv[1:]):
            _show_crash(traceback.format_exc())
        sys.exit(1)


def _show_crash(text, title="Could not start"):
    """A windowed build has no console: without this a start that fails
    just does not open, with nothing on screen and nothing on the command
    line. Show the error (best effort -- Qt itself may be what failed)."""
    try:
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        box = QtWidgets.QMessageBox()
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
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
