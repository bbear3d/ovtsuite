; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; The standalone tools, shared by installer.iss (shortcuts beside the main app)
; and tool_installer.iss (the tools without it). Every value here must equal its
; counterpart in ovtsuite/branding.py -- TOOL_EXES, TOOL_NAMES and the
; *_APP_ID ids -- which tests/test_branding.py checks, since Inno Setup cannot
; import the package.
;
; The exes are small launchers the Windows build puts beside OVTSuite.exe
; (packaging/launcher/Launcher.cs, tools/build_launchers.py): each starts
; "OVTSuite.exe --tool <name>", and packaging/entry.py runs only that tool. The AppUserModelIDs are what the tool
; process sets too: a shortcut carrying the same id is what gives the tool its
; own taskbar button and makes a pin relaunch the tool, not the main app.

#define FlasherExe   "OVTFlasher.exe"
#define SnifferExe   "OVTSniffer.exe"
#define LoggerExe    "OVTLogger.exe"
#define SimulatorExe  "OVTSimulator.exe"
#define ConverterExe  "OVTConverter.exe"

#define FlasherName  "OVT Flasher"
#define SnifferName  "OVT Sniffer"
#define LoggerName   "OVT Logger"
#define SimulatorName "OVT Simulator"
#define ConverterName "OVT Converter"

#define FlasherAppId "OVTSuite.Flasher"
#define SnifferAppId "OVTSuite.Sniffer"
#define LoggerAppId  "OVTSuite.Logger"
#define SimulatorAppId "OVTSuite.Simulator"
#define ConverterAppId "OVTSuite.Converter"

; The named mutex every exe of the bundle holds while it runs (branding.APP_MUTEX,
; created in packaging/entry.py): AppMutex makes setup wait for -- and ask the
; user to close -- every running copy before it replaces files.
#define AppMutex "OVTSuiteAppRunning"
