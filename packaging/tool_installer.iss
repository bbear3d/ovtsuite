; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Inno Setup script for the standalone tools WITHOUT the main OVT-Suite app:
; one tool (flasher, sniffer, logger, converter), or all of them together (tools).
;
; build.ps1 -Installers flasher,sniffer,logger,converter,tools passes the tool and the
; usual version / folder / output defines:
;
;   ISCC.exe /DTool=flasher /DAppVersion=0.3.0 /DSourceDir=..\dist\OVTSuite ^
;            /DOutputDir=..\dist /DOutputName=OVTFlasher-0.3.0-setup ^
;            packaging\tool_installer.iss
;
; It ships the same one-folder build as the main installer, minus the other
; tools' launchers, the full modules and the app manifest. OVTSuite.exe IS
; shipped: the tool exes are small launchers that start "OVTSuite.exe --tool
; <name>" from their own folder (packaging/launcher/Launcher.cs), and the code
; lives in that one exe. No shortcut points at it: what is installed opens its
; tool. Each product has its own AppId, so it installs, upgrades and
; uninstalls on its own, beside or without the main app.
;
; MODULES: the tools work for every control module, but correcting a checksum
; needs that module (docs/MODULES_PLAN.md). When build.ps1 passes
; /DModulesInclude=<generated .iss>, every module's small checksum-only variant
; is offered as a component, all ticked; the full modules (definitions, the
; editor) are for OVT-Suite and are not offered here.

#ifndef Tool
  #define Tool "flasher"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\OVTSuite"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef SetupCompression
  #define SetupCompression "lzma2/max"
#endif

#define MainExe "OVTSuite.exe"
#include "tools.iss"

#define WithFlasher (Tool == "flasher" || Tool == "tools")
#define WithSniffer (Tool == "sniffer" || Tool == "tools")
#define WithLogger  (Tool == "logger"  || Tool == "tools")
#define WithConverter (Tool == "converter" || Tool == "tools")

; Per product: name, a stable AppId (never change one: it is what makes the
; next version an upgrade), the exe it is "about", and the setup's own icon.
#if Tool == "flasher"
  #define ProductName FlasherName
  #define ProductGuid "E1DD9158-B43B-4FBB-B7C2-E286F8C7ED5B"
  #define ProductExe  FlasherExe
  #define ProductIcon "ovtsuite_flasher.ico"
#elif Tool == "sniffer"
  #define ProductName SnifferName
  #define ProductGuid "2D8987A3-4BB9-453E-B874-E656EFC3E541"
  #define ProductExe  SnifferExe
  #define ProductIcon "ovtsuite_sniffer.ico"
#elif Tool == "logger"
  #define ProductName LoggerName
  #define ProductGuid "F1DAC2AA-B8F8-4E30-97C3-23FD67A925C8"
  #define ProductExe  LoggerExe
  #define ProductIcon "ovtsuite_logger.ico"
#elif Tool == "converter"
  #define ProductName ConverterName
  #define ProductGuid "6B1E3C52-9F4D-4A71-8E2B-3D5C7A90F1E4"
  #define ProductExe  ConverterExe
  #define ProductIcon "ovtsuite_converter.ico"
#elif Tool == "tools"
  #define ProductName "OVT Tools"
  #define ProductGuid "F8C48995-BA97-43C5-AF25-531EE44B3A17"
  #define ProductExe  FlasherExe
  #define ProductIcon "ovtsuite.ico"
#else
  #error Tool must be flasher, sniffer, logger, converter or tools
#endif

#ifndef OutputName
  #define OutputName StringChange(ProductName, " ", "") + "-setup"
#endif

; A single tool's shortcut goes straight in the Start menu; the three together
; get a folder of their own.
#if Tool == "tools"
  #define ShortcutDir "{group}"
#else
  #define ShortcutDir "{autoprograms}"
#endif

; Everything in the build except the launchers of tools this product does not
; include -- never MainExe, which every launcher starts. Leading "\" anchors
; each to the build's top level.
#define Excl "\modules"
#if !WithFlasher
  #define Excl Excl + ",\" + FlasherExe
#endif
#if !WithSniffer
  #define Excl Excl + ",\" + SnifferExe
#endif
#if !WithLogger
  #define Excl Excl + ",\" + LoggerExe
#endif
#if !WithConverter
  #define Excl Excl + ",\" + ConverterExe
#endif
; (\modules above: the build's modules folder holds every FULL module; this
; product takes only their checksum variants, as components -- ModulesInclude)
; the app manifest describes the whole app (delta updates, docs/UPDATES.md);
; a tool install is not updated that way -- its own setup replaces it
#define Excl Excl + ",\app-manifest.json,\app-manifest.json.sig"

[Setup]
AppId={{{#ProductGuid}}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#ProductName}
DefaultGroupName={#ProductName}
UsePreviousGroup=no
DisableProgramGroupPage=yes
UninstallDisplayName={#ProductName} {#AppVersion}
UninstallDisplayIcon={app}\{#ProductExe}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression={#SetupCompression}
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#ProductIcon}
; Authenticode: build.ps1 -CodeSign passes /DSignTool=ovtsign and the
; /Sovtsign=... signtool command line; setup.exe and its uninstaller are signed.
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AllowNoIcons=yes
; The licence. With modules on board (the full installer, the tools' checksum
; modules) the License page is the module agreement, which must be accepted,
; and OVT-Suite's own MPL-2.0 follows as information; without, the MPL-2.0.
#ifdef ModulesInclude
LicenseFile=..\ovtsuite\modules\EULA.txt
InfoBeforeFile=..\LICENSE
#else
LicenseFile=..\LICENSE
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create &desktop shortcuts"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked
#if WithConverter
Name: "shellmenu"; \
    Description: "Add ""Convert with {#ConverterName}"" to the right-click menu"; \
    GroupDescription: "Right-click menu:"; Flags: unchecked
#endif

[Files]
Source: "{#SourceDir}\*"; Excludes: "{#Excl}"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

#ifdef ModulesInclude
  #include ModulesInclude
#endif

[UninstallDelete]
Type: filesandordirs; Name: "{app}\modules"

[Icons]
#if WithFlasher
Name: "{#ShortcutDir}\{#FlasherName}"; Filename: "{app}\{#FlasherExe}"; \
    AppUserModelID: "{#FlasherAppId}"
Name: "{autodesktop}\{#FlasherName}"; Filename: "{app}\{#FlasherExe}"; \
    AppUserModelID: "{#FlasherAppId}"; Tasks: desktopicon
#endif
#if WithSniffer
Name: "{#ShortcutDir}\{#SnifferName}"; Filename: "{app}\{#SnifferExe}"; \
    AppUserModelID: "{#SnifferAppId}"
Name: "{autodesktop}\{#SnifferName}"; Filename: "{app}\{#SnifferExe}"; \
    AppUserModelID: "{#SnifferAppId}"; Tasks: desktopicon
#endif
#if WithLogger
Name: "{#ShortcutDir}\{#LoggerName}"; Filename: "{app}\{#LoggerExe}"; \
    AppUserModelID: "{#LoggerAppId}"
Name: "{autodesktop}\{#LoggerName}"; Filename: "{app}\{#LoggerExe}"; \
    AppUserModelID: "{#LoggerAppId}"; Tasks: desktopicon
#endif
#if WithConverter
Name: "{#ShortcutDir}\{#ConverterName}"; Filename: "{app}\{#ConverterExe}"; \
    AppUserModelID: "{#ConverterAppId}"
Name: "{autodesktop}\{#ConverterName}"; Filename: "{app}\{#ConverterExe}"; \
    AppUserModelID: "{#ConverterAppId}"; Tasks: desktopicon
#endif

[Run]
; A compiled checksum module replaces its source copy everywhere (see
; installer.iss, the same entry).
Filename: "{app}\{#MainExe}"; Parameters: "scrub-module-sources"; \
    Flags: runhidden waituntilterminated

#if WithConverter
[Run]
; The right-click menu (task above): the same code as the converter's own
; More menu and "ovtsuite convert --shell-menu", for the user who ran setup.
; No file association -- a menu entry only.
Filename: "{app}\{#MainExe}"; Parameters: "convert --shell-menu install"; \
    Flags: runhidden waituntilterminated runasoriginaluser; Tasks: shellmenu

[UninstallRun]
; Always: the menu may have been added from the converter window too, and must
; not be left pointing at a removed program.
Filename: "{app}\{#MainExe}"; Parameters: "convert --shell-menu remove"; \
    Flags: runhidden waituntilterminated; RunOnceId: "OVTShellMenu"
#endif

#if Tool != "tools"
[Run]
Filename: "{app}\{#ProductExe}"; Description: "Launch {#ProductName}"; \
    Flags: nowait postinstall skipifsilent
#endif

; The add-ons: optional downloads (the EDC16 boot-sector database for flasher
; builds, FTDI's USB driver for every tool that talks to a K-line cable) and
; marking the bundled modules' agreement accepted -- see each file. Each file
; starts with ';' comments but may END inside its [Code], where they are not
; valid, so an empty [Files] line re-opens a section first (a section may
; appear any number of times).
#if WithFlasher
#include "addon_bootdb.iss"
#endif
[Files]
#include "addon_ftdi.iss"
#ifdef ModulesInclude
[Files]
#include "modules_eula.iss"
#endif
