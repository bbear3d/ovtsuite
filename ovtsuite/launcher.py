# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Windows tool launchers' side of the main exe.

``OVTFlasher.exe``, ``OVTSniffer.exe``, ``OVTLogger.exe``,
``OVTSimulator.exe`` and ``OVTMcp.exe`` are tiny .NET Framework programs
(``packaging/launcher/Launcher.cs``, built by ``tools/build_launchers.py``)
that start ``OVTSuite.exe --tool <name> <their own arguments>`` from their
own folder. ``packaging/entry.py`` reads that here and runs the tool exactly
as the full-size tool exes used to: the same CLI entry (``--flasher``, ...,
``mcp``), the tool's AppUserModelID and icon (set by the tool's window code
from the tool name), the app mutex, the same crash and fault logs.

Qt-free and import-light: it runs before anything else in the frozen app.
"""

import os
import sys
from typing import List, Optional, Tuple

from . import branding

#: ``OVTSuite.exe --tool <name> [args...]``: must be the first argument.
TOOL_ARG = "--tool"

#: The MCP server's tool name (``OVTMcp.exe``; ``ovtsuite mcp``).
MCP = "mcp"


class UnknownTool(ValueError):
    """``--tool`` without a name, or with one no launcher has."""


def tool_exes() -> dict:
    """Every launcher: tool name -> exe base name (no ``.exe``)."""
    out = dict(branding.TOOL_EXES)
    out[MCP] = branding.MCP_EXE
    return out


def split_tool_argv(argv: List[str]) -> Tuple[Optional[str], List[str]]:
    """``["--tool", "flasher", "car.bin"]`` -> ``("flasher", ["car.bin"])``;
    ``--tool=flasher`` works too. Anything else is ``(None, argv)``: only a
    FIRST argument selects a tool, so a file named ``--tool`` further along
    is passed through untouched. Raises :class:`UnknownTool`."""
    argv = list(argv)
    if not argv:
        return None, argv
    first = argv[0]
    if first == TOOL_ARG:
        if len(argv) < 2:
            raise UnknownTool("%s needs a tool name (%s)" % (TOOL_ARG, ", ".join(sorted(tool_exes()))))
        name, rest = argv[1], argv[2:]
    elif first.startswith(TOOL_ARG + "="):
        name, rest = first[len(TOOL_ARG) + 1:], argv[1:]
    else:
        return None, argv
    tool = name.strip().lower()
    if tool not in tool_exes():
        raise UnknownTool("no such tool: %r (one of %s)" % (name, ", ".join(sorted(tool_exes()))))
    return tool, rest


def cli_argv(tool: str, rest: List[str]) -> List[str]:
    """The ``ovtsuite`` command line a tool runs: what the full-size tool
    exes ran when started under their own names."""
    if tool == MCP:
        return ["mcp"] + list(rest)
    return ["--" + tool] + list(rest)


# ------------------------------------------------------------------ this process
_current: Optional[str] = None


def set_current(tool: Optional[str]) -> None:
    """packaging/entry.py: this process runs ``tool`` (None: the app)."""
    global _current
    _current = tool


def current() -> Optional[str]:
    return _current


def launcher_path(tool: str, exe_dir: Optional[str] = None) -> str:
    """Where ``tool``'s launcher sits: beside the main exe."""
    exe_dir = exe_dir or os.path.dirname(os.path.abspath(sys.executable))
    return os.path.join(exe_dir, tool_exes()[tool] + ".exe")


def relaunch_exe(executable: Optional[str] = None, tool: Optional[str] = None) -> str:
    """The program to start this app again with after an update: the tool's
    launcher when this process runs a tool through one (so OVT Flasher comes
    back as OVT Flasher, not as the main app), else the exe itself."""
    executable = os.path.abspath(executable or sys.executable)
    tool = _current if tool is None else tool
    if tool and tool in tool_exes():
        p = launcher_path(tool, os.path.dirname(executable))
        if os.path.isfile(p):
            return p
    return executable


# ------------------------------------------------------------------ the C# side
def strip_program_name(cmdline: str) -> str:
    """The arguments of a raw Windows command line: everything after the
    program name, verbatim. The launcher passes its own arguments on this way
    (``Launcher.cs`` ``ArgumentsOf``, which this mirrors and the tests pin):
    no re-quoting, so OVTSuite.exe's C runtime splits them exactly as the
    launcher's would have. The program name follows the rule for argv[0]
    (no backslash escapes): a leading quoted part up to the next quote, then
    up to the first space or tab; the blanks after it are dropped."""
    s = cmdline or ""
    i = 0
    if s.startswith('"'):
        j = s.find('"', 1)
        i = len(s) if j < 0 else j + 1
    while i < len(s) and s[i] not in " \t":
        i += 1
    while i < len(s) and s[i] in " \t":
        i += 1
    return s[i:]


def child_command_line(exe: str, tool: str, args: str) -> str:
    """The command line a launcher starts (``Launcher.cs`` ``CommandLine``):
    the main exe quoted, ``--tool <name>``, then its own arguments verbatim."""
    line = '"%s" %s %s' % (exe, TOOL_ARG, tool)
    return line + (" " + args if args else "")
