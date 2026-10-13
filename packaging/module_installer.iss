; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Inno Setup script for ONE module (docs/MODULES_PLAN.md): the full or the
; checksum-only variant of e.g. the ZF 8HP module.
;
; build.ps1 -Installers modules,modules-checksum passes, per module:
;
;   ISCC.exe /DModuleId=zf8hp /DModuleName="ZF 8HP" /DModuleVersion=0.1.0 ^
;            /DVariant=full /DSourceDir=..\dist\OVTSuite\modules\zf8hp ^
;            /DOutputDir=..\dist\module-installers /DOutputName=OVTSuite-ZF8HP-0.1.0-setup ^
;            packaging\module_installer.iss
;
; SourceDir is the module laid out INSTALLED (manifest.json, signature,
; <id>\..., data\...), exactly what tools/build_modules.py install-into makes.
;
; A module installs only beside an installed OVT-Suite or OVT Tools (Bear,
; 2026-10-10): InitializeSetup looks for either product's uninstall entry and
; refuses without one. Where it goes:
;   * installing for the current user (the default, no elevation): the
;     per-user modules folder, %LOCALAPPDATA%\OVT-Suite\modules\<id> -- read by
;     OVT-Suite AND OVT Tools, so one install serves both;
;   * installing for all users (elevated): the modules folder of the
;     all-users OVT-Suite / OVT Tools install that was found.
; The app re-verifies the module's signature and every file each time it
; starts; this installer only places the files.
; Both variants of one module share the AppId, so installing one replaces the
; other, and an upgrade replaces the previous version.

#ifndef ModuleId
  #error ModuleId is required (/DModuleId=zf8hp)
#endif
#ifndef ModuleName
  #define ModuleName ModuleId
#endif
#ifndef ModuleVersion
  #define ModuleVersion "0.0.0"
#endif
#ifndef Variant
  #define Variant "full"
#endif
#ifndef SourceDir
  #error SourceDir is required (the module laid out installed)
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef SetupCompression
  #define SetupCompression "lzma2/max"
#endif

#define AppName "OVT-Suite"
#define ConfigDir "OVT-Suite"
#define SuiteGuid "{94760517-1A60-48C1-8353-767DA53A52EB}"
; OVT Tools / Flasher installs (tool_installer.iss); they use a module's checksum
#define ToolsGuid   "{F8C48995-BA97-43C5-AF25-531EE44B3A17}"
#define FlasherGuid "{E1DD9158-B43B-4FBB-B7C2-E286F8C7ED5B}"

#if Variant == "checksum"
  #define ProductName AppName + " " + ModuleName + " module (checksum only)"
#else
  #define ProductName AppName + " " + ModuleName + " module"
#endif
#ifndef OutputName
  #define OutputName StringChange(AppName + "-" + ModuleName, " ", "") + "-" + ModuleVersion + "-setup"
#endif

[Setup]
AppId={#AppName}.Module.{#ModuleId}
AppName={#ProductName}
AppVersion={#ModuleVersion}
AppVerName={#ProductName} {#ModuleVersion}
VersionInfoVersion={#ModuleVersion}
AppPublisher={#AppName}
DefaultDirName={code:ModuleDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
CreateUninstallRegKey=yes
UninstallDisplayName={#ProductName} {#ModuleVersion}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression={#SetupCompression}
SolidCompression=yes
WizardStyle=modern
SetupIconFile=ovtsuite.ico
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; the module folder is replaced whole: a file a newer version dropped must not
; linger, and the app refuses a module whose files do not match its manifest
DirExistsWarning=no
; A closed-source module's licence agreement (ovtsuite\modules\EULA.txt, which
; the build puts in the package as <id>\EULA.txt): build.ps1 passes its path,
; and the wizard asks the user to accept it. Open-source modules have none.
#ifdef ModuleEula
LicenseFile={#ModuleEula}
#endif

[Messages]
WelcomeLabel2=This will install the [name/ver] on your computer.%n%nIt adds support for one kind of control module to {#AppName}, which must already be installed (OVT Tools also uses a module's checksum correction).

[InstallDelete]
Type: filesandordirs; Name: "{app}\{#ModuleId}"
Type: filesandordirs; Name: "{app}\data"
Type: files; Name: "{app}\manifest.json"
Type: files; Name: "{app}\signature"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; what the app wrote beside the module (byte-code for another Python version)
Type: filesandordirs; Name: "{app}\{#ModuleId}"

[Code]
var
  BaseDir: String;

function FindInstall(Root: Integer; const Guid: String): String;
var
  Loc: String;
begin
  Result := '';
  if RegQueryStringValue(Root,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + Guid + '_is1',
       'InstallLocation', Loc) and DirExists(Loc) then
    Result := RemoveBackslash(Loc);
end;

// The base install this module belongs to: OVT-Suite first, then OVT Tools
// or OVT Flasher (the products that correct checksums). Per-user installs are
// in HKCU, all-users installs in HKLM.
function FindBase(Root: Integer): String;
begin
  Result := FindInstall(Root, '{#SuiteGuid}');
  if Result = '' then Result := FindInstall(Root, '{#ToolsGuid}');
  if Result = '' then Result := FindInstall(Root, '{#FlasherGuid}');
end;

function InitializeSetup(): Boolean;
var
  User, Machine: String;
begin
  User := FindBase(HKCU);
  Machine := FindBase(HKLM);
  if IsAdminInstallMode then
    BaseDir := Machine
  else if User <> '' then
    BaseDir := User
  else
    BaseDir := Machine;
  Result := (User <> '') or (Machine <> '');
  if not Result then
    MsgBox('{#AppName} is not installed on this computer.' + #13#10 + #13#10 +
           'Install {#AppName} (or OVT Tools) first, then run this module''s ' +
           'installer again.', mbError, MB_OK)
  else if IsAdminInstallMode and (Machine = '') then
  begin
    // (a line must never START with #13: the preprocessor reads any line
    // beginning with # as one of its directives)
    MsgBox('{#AppName} is installed for your user only, not for all users.' + #13#10 +
           'Run this installer again and choose to install for you only.',
           mbError, MB_OK);
    Result := False;
  end;
end;

function ModuleDir(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := BaseDir + '\modules\{#ModuleId}'
  else
    Result := ExpandConstant('{localappdata}\{#ConfigDir}\modules\{#ModuleId}');
end;

#ifdef ModuleEula
// The wizard showed the licence agreement and the user accepted it: say so
// beside the module (ovtsuite/modules/eula.py), so the app does not ask again
// at its first start. The SHA-256 ties the acceptance to this agreement text.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Eula: String;
begin
  Eula := ExpandConstant('{app}\{#ModuleId}\EULA.txt');
  if (CurStep = ssPostInstall) and FileExists(Eula) then
    SaveStringToFile(ExpandConstant('{app}\{#ModuleId}\EULA.accepted'),
                     GetSHA256OfFile(Eula), False);
end;
#endif
