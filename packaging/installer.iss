; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Inno Setup script for the OVT-Suite.
;
; build.ps1 passes the version, source folder and output name on the command
; line, so nothing here needs editing per release:
;
;   ISCC.exe /DAppVersion=0.1.0 /DSourceDir=..\dist\OVTSuite ^
;            /DOutputDir=..\dist /DOutputName=OVTSuite-0.1.0-setup ^
;            packaging\installer.iss
;
; Building by hand without those defines still works and produces a 0.0.0
; installer from dist\OVTSuite.
;
; MODULES (docs/MODULES_PLAN.md). dist\OVTSuite is the BASE app plus, under
; modules\, every module installed. This script ships the base app and, when
; build.ps1 passes /DModulesInclude=<generated .iss>, a Components page listing
; every module, all ticked ("full" installer). Without it, it is the base-only
; installer: modules come later from their own installers or from inside the
; app (Tools > Modules...).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\OVTSuite"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef OutputName
  #define OutputName "OVTSuite-setup"
#endif
; build.ps1 -FastInstaller passes lzma2/fast for quicker test builds.
#ifndef SetupCompression
  #define SetupCompression "lzma2/max"
#endif

#define AppName    "OVT-Suite"
#define AppExeName "OVTSuite.exe"
; the standalone tools' exe names, display names and taskbar ids
#include "tools.iss"

[Setup]
; A stable GUID identifies the application across versions: keep it, so an
; upgrade replaces the previous install instead of sitting alongside it.
AppId={{94760517-1A60-48C1-8353-767DA53A52EB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; An upgrade keeps the previous install's directory (UsePreviousAppDir) but not
; its Start Menu group: the Select Start Menu Folder page is never shown
; (DisableProgramGroupPage), so no group can have been chosen by hand, and a
; past release's group would keep that release's name.
UsePreviousGroup=no
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression={#SetupCompression}
SolidCompression=yes
WizardStyle=modern
; the installer wizard executable wears the app icon too
SetupIconFile=ovtsuite.ico
; Authenticode: build.ps1 -CodeSign passes /DSignTool=ovtsign and the
; /Sovtsign=... signtool command line; setup.exe and its uninstaller are signed.
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif
; Per-user by default so no elevation prompt is needed; the user can still
; choose an all-users install, which does elevate.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
AllowNoIcons=yes
; tell Explorer to refresh icons/handlers after the [Registry] associations
ChangesAssociations=yes
; Never replace the files of a running copy: setup asks to close the app and
; its tools first (AppMutex, see tools.iss), and the Restart Manager closes
; whatever else still holds a file. The in-app updater (ovtsuite/gui_update.py)
; closes the app -- asking about unsaved work -- before it starts setup.
AppMutex={#AppMutex}
CloseApplications=yes
RestartApplications=no
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
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "associate_ovt"; \
    Description: "Open OVT projects (.ovt) with {#AppName}"; \
    GroupDescription: "File associations:"
Name: "associate"; Description: "Associate &.bin files with {#AppName}"; \
    GroupDescription: "File associations:"; Flags: unchecked

[Files]
; The one-folder PyInstaller output -- the base app. Its modules\ folder is
; NOT taken whole: each module is a component of its own (below).
Source: "{#SourceDir}\*"; Excludes: "\modules"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; The .ovt project file's own icon (tools/make_file_icons.py builds it from
; ovtsuite/data/ovt_project.svg); the association below points at it.
Source: "ovt_project.ico"; DestDir: "{app}"; Flags: ignoreversion

#ifdef ModulesInclude
  #include ModulesInclude
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
; The standalone tools: each has its own launcher exe in the build (see
; tools.iss), which carries the tool's icon and opens only that tool.
Name: "{group}\{#FlasherName}"; Filename: "{app}\{#FlasherExe}"; \
    AppUserModelID: "{#FlasherAppId}"
Name: "{group}\{#SnifferName}"; Filename: "{app}\{#SnifferExe}"; \
    AppUserModelID: "{#SnifferAppId}"
Name: "{group}\{#LoggerName}"; Filename: "{app}\{#LoggerExe}"; \
    AppUserModelID: "{#LoggerAppId}"
Name: "{group}\{#SimulatorName}"; Filename: "{app}\{#SimulatorExe}"; \
    AppUserModelID: "{#SimulatorAppId}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Registry]
; The OVT project (.ovt), on by default: the extension is OVT-Suite's alone, so
; unlike .bin the installer makes itself its default handler.
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovt"; \
    ValueType: string; ValueName: ""; ValueData: "{#AppName} project"; \
    Flags: uninsdeletekey; Tasks: associate_ovt
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovt\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\ovt_project.ico"; \
    Flags: uninsdeletekey; Tasks: associate_ovt
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovt\shell\open\command"; \
    ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#AppExeName}"" gui ""%1"""; \
    Flags: uninsdeletekey; Tasks: associate_ovt
Root: HKA; Subkey: "Software\Classes\.ovt"; \
    ValueType: string; ValueName: ""; ValueData: "OVTSuite.ovt"; \
    Flags: uninsdeletevalue; Tasks: associate_ovt
Root: HKA; Subkey: "Software\Classes\.ovt"; \
    ValueType: string; ValueName: "Content Type"; \
    ValueData: "application/x-ovt-project"; \
    Flags: uninsdeletevalue; Tasks: associate_ovt
Root: HKA; Subkey: "Software\Classes\.ovt\OpenWithProgids"; \
    ValueType: string; ValueName: "OVTSuite.ovt"; ValueData: ""; \
    Flags: uninsdeletevalue; Tasks: associate_ovt

; Module packages: opening one installs it (for the current user).
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovtmod"; \
    ValueType: string; ValueName: ""; ValueData: "{#AppName} module"; \
    Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovtmod\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
    Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OVTSuite.ovtmod\shell\open\command"; \
    ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#AppExeName}"" install-module --gui ""%1"""; \
    Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.ovtmod"; \
    ValueType: string; ValueName: ""; ValueData: "OVTSuite.ovtmod"; \
    Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.ovtmod\OpenWithProgids"; \
    ValueType: string; ValueName: "OVTSuite.ovtmod"; ValueData: ""; \
    Flags: uninsdeletevalue

; Opt-in .bin association. .bin is a generic extension, so this registers a
; named ProgID and only points .bin at it when the user asks — and the whole
; key is removed on uninstall.
Root: HKA; Subkey: "Software\Classes\OVTSuite.bin"; \
    ValueType: string; ValueName: ""; ValueData: "EDC16 ECU binary"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\OVTSuite.bin\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
    Flags: uninsdeletekey; Tasks: associate
; "gui" is required: a bare path is not a subcommand, and before cli.main
; accepted one this association opened nothing (argparse exited silently).
Root: HKA; Subkey: "Software\Classes\OVTSuite.bin\shell\open\command"; \
    ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#AppExeName}"" gui ""%1"""; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.bin\OpenWithProgids"; \
    ValueType: string; ValueName: "OVTSuite.bin"; ValueData: ""; \
    Flags: uninsdeletevalue; Tasks: associate

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent
; An update started from inside the app runs silently with /RELAUNCH=1: start
; the app again when it is done (as the user who started setup, not elevated).
Filename: "{app}\{#AppExeName}"; \
    Flags: nowait runasoriginaluser; Check: WizardSilent and RelaunchRequested

[UninstallDelete]
; PyInstaller's one-folder layout leaves nothing behind, but the crash log
; written by packaging/entry.py lives outside {app}.
Type: filesandordirs; Name: "{localappdata}\OVTSuite"
; Modules in the app's own folder go with it (a module installed by its own
; installer is uninstalled first, by UninstallModules in [Code], so its entry
; in Apps & features goes too). Modules installed per user from inside the
; app are left: they may serve an OVT Tools install as well.
Type: filesandordirs; Name: "{app}\modules"

; The add-ons: optional downloads (the EDC16 boot-sector database, FTDI's USB
; driver) and marking the bundled modules' agreement accepted -- see each file.
; Included here, before [Code], because a ';' comment is not valid inside
; [Code] (their own [Code] uses event attributes, so their place does not
; matter). Each file starts with ';' comments but may END inside its [Code], so
; an empty [Files] line re-opens a section where the next one's comments are
; valid (a section may appear any number of times).
#include "addon_bootdb.iss"
[Files]
#include "addon_ftdi.iss"
#ifdef ModulesInclude
[Files]
#include "modules_eula.iss"
#endif

[Code]
// /RELAUNCH=1 on the command line (the in-app updater): start the app after a
// silent install. See [Run].
function RelaunchRequested(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

// Every past release this installer may find in the field, newest first, as
// product name and executable base name. GENERATED: tools/rename_package.py
// adds a row on each rename, and tests/test_branding.py fails if this list and
// branding.LEGACY_RELEASES disagree (the installer cannot import the package).
// The one past name is the pre-release name of the author's own early installs,
// kept so those machines upgrade cleanly into the current folder.
// branding: legacy-names-begin
function LegacyReleaseCount(): Integer;
begin
  Result := 1;
end;

function LegacyAppName(I: Integer): String;
begin
  Result := '';
  case I of
    0: Result := 'EDC16-Tuner';
  end;
end;

function LegacyExeName(I: Integer): String;
begin
  Result := '';
  case I of
    0: Result := 'EDC16Tuner';
  end;
end;

// A past release's default install directory: DefaultDirName has always been
// {autopf}\<product name>.
function OldDefaultDir(I: Integer): String;
begin
  Result := ExpandConstant('{autopf}\') + LegacyAppName(I);
end;

procedure CurPageChanged(CurPageID: Integer);
// An upgrade over a past release pre-fills the directory page with that
// release's directory (UsePreviousAppDir). Only an untouched DEFAULT directory
// of a past name is moved to the current default; a directory the user chose
// is left alone.
var
  I: Integer;
begin
  if CurPageID = wpSelectDir then
  begin
    for I := 0 to LegacyReleaseCount() - 1 do
    begin
      if CompareText(AddBackslash(WizardForm.DirEdit.Text),
                     AddBackslash(OldDefaultDir(I))) = 0 then
      begin
        WizardForm.DirEdit.Text := ExpandConstant('{autopf}\{#AppName}');
        Break;
      end;
    end;
  end;
end;

procedure RemoveOldStartMenuEntries();
// A past release's Start Menu group and desktop shortcut carry its old name,
// so Inno never treats them as this release's. Remove them, per user and for
// all users, but never the group this run installs into.
var
  I: Integer;
  name, oldUser, oldCommon, group: String;
begin
  group := ExpandConstant('{group}');
  for I := 0 to LegacyReleaseCount() - 1 do
  begin
    name := LegacyAppName(I);
    oldUser := ExpandConstant('{userprograms}\') + name;
    oldCommon := ExpandConstant('{commonprograms}\') + name;
    if CompareText(oldUser, group) <> 0 then
      DelTree(oldUser, True, True, True);
    if CompareText(oldCommon, group) <> 0 then
      DelTree(oldCommon, True, True, True);
    DeleteFile(ExpandConstant('{userdesktop}\') + name + '.lnk');
    DeleteFile(ExpandConstant('{commondesktop}\') + name + '.lnk');
  end;
end;

procedure RemoveOldInstallDir();
// A past release's default install directory left beside this install, in
// either program-files root. Never {app} itself.
var
  I: Integer;
  name, d: String;
begin
  for I := 0 to LegacyReleaseCount() - 1 do
  begin
    name := LegacyAppName(I);
    d := ExpandConstant('{commonpf}\') + name;
    if (CompareText(d, ExpandConstant('{app}')) <> 0) and DirExists(d) then
      DelTree(d, True, True, True);
    d := ExpandConstant('{localappdata}\Programs\') + name;
    if (CompareText(d, ExpandConstant('{app}')) <> 0) and DirExists(d) then
      DelTree(d, True, True, True);
  end;
end;

// branding: legacy-names-end

// What [InstallDelete] used to do for one name, for every past name. Runs at
// ssInstall, before this version's files are laid down, so an exe left behind
// under an older name cannot outlive this install. Skips any name that IS this
// build's exe -- a rename that kept the executable name would otherwise have
// this deleting the file it is about to install.
procedure RemoveOldExecutables();
var
  I: Integer;
  name: String;
begin
  for I := 0 to LegacyReleaseCount() - 1 do
  begin
    name := LegacyExeName(I);
    if CompareText(name, '{#AppExeName}') <> 0 then
      DeleteFile(ExpandConstant('{app}\') + name + '.exe');
  end;
end;

// The per-platform files of earlier versions (.edc16, .6hp ...) are no longer
// associated -- projects (.ovt) replaced them; the app still opens and imports
// them through File > Open. An update removes the associations an earlier
// version made: its own ProgIDs, and an extension's default only while it
// still names that ProgID (another program that took it over keeps it).
// Kept in step with container.FORMATS (+ backup15.EXT) by tests/test_branding.py.
function OldAssocExts(): TArrayOfString;
begin
  SetArrayLength(Result, 7);
  Result[0] := 'edc16';
  Result[1] := 'edc15';
  Result[2] := '6hp';
  Result[3] := '8hp';
  Result[4] := 'hvac';
  Result[5] := 'vdo';
  Result[6] := 'edc15bak';
end;

procedure RemoveOldAssociations();
var
  Exts: TArrayOfString;
  I: Integer;
  ProgId, Cur: String;
begin
  Exts := OldAssocExts();
  for I := 0 to GetArrayLength(Exts) - 1 do
  begin
    ProgId := 'OVTSuite.' + Exts[I];
    RegDeleteKeyIncludingSubkeys(HKA, 'Software\Classes\' + ProgId);
    if RegQueryStringValue(HKA, 'Software\Classes\.' + Exts[I], '', Cur) and
       (CompareText(Cur, ProgId) = 0) then
      RegDeleteValue(HKA, 'Software\Classes\.' + Exts[I], '');
    RegDeleteValue(HKA, 'Software\Classes\.' + Exts[I] + '\OpenWithProgids', ProgId);
    RegDeleteKeyIfEmpty(HKA, 'Software\Classes\.' + Exts[I] + '\OpenWithProgids');
    RegDeleteKeyIfEmpty(HKA, 'Software\Classes\.' + Exts[I]);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    RemoveOldExecutables();
  if CurStep = ssPostInstall then
  begin
    RemoveOldStartMenuEntries();
    RemoveOldInstallDir();
    RemoveOldAssociations();
  end;
end;

// Module installers put their own uninstaller in the module's folder; run each
// silently before the app's folder goes, so no "OVT-Suite ... module" entry is
// left behind in Apps & features pointing at nothing.
procedure UninstallModules();
var
  FindRec: TFindRec;
  Dir, Unins: String;
  Code: Integer;
begin
  Dir := ExpandConstant('{app}\modules\');
  if FindFirst(Dir + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and
           (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Unins := Dir + FindRec.Name + '\unins000.exe';
          if FileExists(Unins) then
            Exec(Unins, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                 SW_HIDE, ewWaitUntilTerminated, Code);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    UninstallModules();
end;
