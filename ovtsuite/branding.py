# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Every name the product wears, in one place.

The suite has been renamed once already (``edc16tuner`` -> ``ovtsuite``) and may
be renamed again, so no other module in the tree may contain the package name,
the product name or the executable name as a literal. Inside the package that
is enforced by using relative imports only; everywhere else -- the GUI titles,
``QSettings`` scopes, the config directory, the XDF export banner, the
PyInstaller spec, the installer script -- read the constants below.

``tools/rename_package.py`` performs the mechanical rename and rewrites this
file; ``tests/test_branding.py`` fails if a stray literal reappears.

Renaming is not free for users, so two migrations are built in:

* ``LEGACY_RELEASES`` -- every name the product has shipped under, newest
  first. ``LEGACY_CONFIG_DIRS`` (older config directories, read when the
  current one has no file yet, so recent-file lists survive) and
  ``LEGACY_QSETTINGS`` (older ``QSettings`` scopes, read once for the same
  reason) are derived from it, and the installer's own cleanup list is
  generated from it.

Neither is ever written to.
"""

#: Import name of the package. Must equal this directory's name.
PKG = "ovtsuite"

#: Product name, as shown to the user (window titles, dialogs, docs).
APP_NAME = "OVT-Suite"

#: Expanded form, for about boxes and installers.
APP_LONG = "Old VAG Tuner Suite"

#: One-line description (installer, package metadata).
APP_DESC = ("Cross-platform tuner for Bosch EDC16, EDC15 and ZF 6HP "
            "VW-group ECU and TCU binaries")

#: Where the base app's source code is published. MPL-2.0 (section 3.2) asks
#: every executable copy to say where its source is: the About box and the
#: notices file print it. Set it before the first public release.
SOURCE_URL = "https://github.com/bbear3d/ovtsuite"

#: Base name of the built executable, with no extension. No spaces or
#: punctuation: it becomes a filename, a PyInstaller target and an Inno Setup id.
EXE_NAME = "OVTSuite"

#: Windows AppUserModelIDs of the standalone tools (``--flasher``, ``--sniffer``,
#: ``--logger``). Each installer shortcut ("OVT Flasher", "OVT Sniffer", "OVT
#: Logger") carries the same id as its process, so every tool gets its own
#: taskbar button with its own icon, and pinning one relaunches that tool rather
#: than the main app.
FLASHER_APP_ID = EXE_NAME + ".Flasher"
SNIFFER_APP_ID = EXE_NAME + ".Sniffer"
LOGGER_APP_ID = EXE_NAME + ".Logger"
SIMULATOR_APP_ID = EXE_NAME + ".Simulator"

#: The standalone tools' own executables and display names. The Windows build
#: puts one small launcher exe per tool beside the main one (packaging/launcher/
#: Launcher.cs): it starts ``OVTSuite.exe --tool <name>`` and packaging/entry.py
#: runs only that tool (ovtsuite/launcher.py). They are what the shortcuts point
#: at, and what the tool-only installers (packaging/tool_installer.iss) ship
#: with OVTSuite.exe.
TOOL_EXES = {"flasher": "OVTFlasher", "sniffer": "OVTSniffer", "logger": "OVTLogger",
             "simulator": "OVTSimulator"}
TOOL_NAMES = {"flasher": "OVT Flasher", "sniffer": "OVT Sniffer", "logger": "OVT Logger",
              "simulator": "OVT Simulator"}

#: Console launcher for the MCP server (``ovtsuite mcp``) in the Windows
#: bundle: the main exe is windowed, so an MCP client starts this console
#: program, which runs ``OVTSuite.exe --tool mcp`` on its own stdin/stdout.
MCP_EXE = "OVTMcp"

#: Windows: the named mutex every exe of the bundle holds while it runs
#: (packaging/entry.py), and the installers' AppMutex (packaging/tools.iss,
#: module_installer.iss): setup waits until no copy is running.
APP_MUTEX = EXE_NAME + "AppRunning"

#: Environment-variable prefix for build switches (``<PREFIX>_ONEFILE`` ...).
ENV_PREFIX = "OVTSUITE"

#: Publisher, and the QSettings organisation.
ORG = "OVT-Suite"

#: (organisation, application) passed to QSettings.
QSETTINGS = (ORG, APP_NAME)

#: Directory name under the user's config location (%APPDATA% / ~/.config).
CONFIG_DIR = APP_NAME

#: Markers for a region whose OLD product names are load-bearing and must
#: survive the next rename. An installer that removes the PREVIOUS install has
#: to name it; a config fallback that reads the PREVIOUS location has to name
#: that. Rewriting those names does not just make a comment stale -- it turns
#: the cleanup on the current install and the fallback on the current file.
#: tools/rename_package.py leaves lines between these two alone, and
#: tests/test_branding.py does not scan them. They are matched anywhere in a
#: line, so any comment syntax will carry them.
LEGACY_BEGIN = "branding: legacy-names-begin"
LEGACY_END = "branding: legacy-names-end"

#: Every release the product has shipped under, NEWEST FIRST, as
#: ``(product name, package name, executable base name, QSettings organisation)``.
#: This is the one list; everything else about the past is derived from it, and
#: ``tools/rename_package.py`` prepends a row on each rename. The same rename also
#: writes the product and executable names into ``packaging/installer.iss``, which
#: cannot import this module -- ``tests/test_branding.py`` fails if the two disagree.
# branding: legacy-names-begin -- the names below are the OLD ones on purpose.
LEGACY_RELEASES = (
    ("EDC16-Tuner", "edc16tuner", "EDC16Tuner", "EDC16-Tuner"),
)
# branding: legacy-names-end

#: Read-only config directories, newest first. Never written to. Both spellings of
#: each release are listed because the Windows build used the product name
#: (%APPDATA%\\EDC16-Tuner) and every other platform the package name
#: (~/.config/edc16tuner).
LEGACY_CONFIG_DIRS = tuple(
    name for app, pkg, _exe, _org in LEGACY_RELEASES for name in (app, pkg))

#: Read-only ``QSettings`` (organisation, application) scopes, same order.
LEGACY_QSETTINGS = tuple(
    (org, app) for app, _pkg, _exe, org in LEGACY_RELEASES)

__version__ = "0.4.10"


def title(subject: str = "") -> str:
    """Window title: the product name, or ``"OVT-Suite - <subject>"``."""
    return "%s — %s" % (APP_NAME, subject) if subject else APP_NAME


def tagged(name: str) -> str:
    """``"<name> (OVT-Suite)"`` -- for exported document titles."""
    return "%s (%s)" % (name, APP_NAME)
