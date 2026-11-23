; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Inno Setup script for the OVT-Suite J2534 USB driver add-on.
;
; A Tactrix OpenPort 2.0 on a computer with no Tactrix driver is given Windows'
; generic serial driver (a COM port); a Scanmatik SM2 with no Scanmatik driver gets
; none at all. This add-on gives either cable Microsoft's own WinUSB driver instead -- the copy built into Windows, selected the way Device
; Manager's "Let me pick from a list" does -- so OVT-Suite can use it directly
; through libusb ("USB direct"). No driver file is added, no certificate is
; installed: packaging\j2534_driver\j2534_driver.ps1 is the whole engine.
;
;   ISCC.exe /DDriverVersion=0.1.0 /DSourceDir=packaging\j2534_driver ^
;            /DOutputDir=dist\module-installers ^
;            /DOutputName=OVTSuite-J2534Driver-0.1.0-setup ^
;            packaging\j2534_driver_installer.iss
;
; (build.ps1 -Installers j2534-driver does this.)
;
; The wizard asks the user to PLUG THE CABLE IN before it binds the driver, and
; looks for it (Check again). It skips the binding by itself when Tactrix's driver
; is installed or the cable already has a driver; "Start menu > J2534 USB driver"
; (the same script, interactive) installs, takes a cable over from another driver
; (Tactrix's, which stays installed) or switches it back at any time -- a different
; cable (the binding belongs to one serial number) needs it too. Uninstalling this
; add-on switches every cable it bound back.

#ifndef DriverVersion
  #define DriverVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "j2534_driver"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\module-installers"
#endif
#ifndef OutputName
  #define OutputName "OVTSuite-J2534Driver-" + DriverVersion + "-setup"
#endif
#ifndef SetupCompression
  #define SetupCompression "lzma2/max"
#endif

#define ProductName "OVT-Suite J2534 USB driver"
#define ScriptName  "j2534_driver.ps1"

[Setup]
AppId=OVT-Suite.J2534Driver
AppName={#ProductName}
AppVersion={#DriverVersion}
AppVerName={#ProductName} {#DriverVersion}
AppPublisher=OVT-Suite
VersionInfoVersion={#DriverVersion}
DefaultDirName={autopf}\OVT-Suite OpenPort driver
DefaultGroupName=OVT-Suite
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression={#SetupCompression}
SolidCompression=yes
WizardStyle=modern
SetupIconFile=ovtsuite_openport.ico
UninstallDisplayIcon={app}\ovtsuite_openport.ico
UninstallDisplayName={#ProductName}
; Binding a driver needs administrator rights, so the whole setup runs elevated.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif

[Messages]
WelcomeLabel2=This gives a Tactrix OpenPort 2.0 or a Scanmatik SM2 cable the WinUSB driver that is built into Windows, so [name] programs can use it directly ("USB direct"), without the maker's driver. An OpenPort stops being a COM port.%n%nSetup will ask you to plug the cable in first.

[Files]
; the engine, installed (the Start menu entry and the uninstaller run it) ...
Source: "{#SourceDir}\{#ScriptName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\j2534_driver.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "ovtsuite_openport.ico"; DestDir: "{app}"; Flags: ignoreversion
; ... and extracted to the temp folder for the wizard's own checks before install
Source: "{#SourceDir}\{#ScriptName}"; DestDir: "{tmp}"; Flags: dontcopy

[Icons]
Name: "{group}\J2534 USB driver"; \
    Filename: "{sys}\wscript.exe"; \
    Parameters: "//B //Nologo ""{app}\j2534_driver.vbs"""; \
    IconFilename: "{app}\ovtsuite_openport.ico"; \
    AppUserModelID: "OVT-Suite.J2534Driver"; \
    Comment: "Install, override or restore the USB driver of an OpenPort or SM2"

[Registry]
; what the engine records when it overrides a maker's driver (so a restore can be checked)
Root: HKLM; Subkey: "SOFTWARE\OVT-Suite\J2534Driver"; Flags: uninsdeletekey

[UninstallRun]
; every cable this add-on bound goes back to the driver it had before
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\{#ScriptName}"" -Action remove"; \
    Flags: runhidden waituntilterminated; RunOnceId: "RemoveUsbBinding"

[Code]
var
  CablePage: TWizardPage;
  CableHelp, CableStatus: TNewStaticText;
  CableButton: TNewButton;
  ReplaceBox: TNewCheckBox;
  SkipBinding: Boolean;
  HaveTactrix, HaveToBind, HaveOther, HaveOurs, TactrixCable: Boolean;
  ResultText: String;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

// Runs the engine from the temp copy; its lines land in OutFile. False when
// PowerShell could not start.
function RunEngine(Action, Extra, OutFile: String; var Code: Integer): Boolean;
begin
  ExtractTemporaryFile('{#ScriptName}');
  DeleteFile(OutFile);
  Result := Exec(PowerShellExe(),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\{#ScriptName}') + '" -Action ' + Action + ' ' + Extra + ' -Out "' + OutFile + '"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
  if not Result then
    Code := -1;
end;

// Looks at what is plugged in (the engine's `status`).
procedure Survey();
var
  Lines: TArrayOfString;
  I, Code: Integer;
begin
  HaveTactrix := False;
  HaveToBind := False;
  HaveOther := False;
  HaveOurs := False;
  TactrixCable := False;
  if RunEngine('status', '', ExpandConstant('{tmp}\op_status.txt'), Code) and
     LoadStringsFromFile(ExpandConstant('{tmp}\op_status.txt'), Lines) then
  begin
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos('TACTRIX|1', Lines[I]) = 1 then HaveTactrix := True;
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos('DEVICE|', Lines[I]) = 1 then
      begin
        // with Tactrix's driver installed an OpenPort (VID_0403) is left alone; an SM2 is not
        if HaveTactrix and (Pos('VID_0403', Lines[I]) > 0) then
        begin
          if Pos('|1|', Lines[I]) > 0 then TactrixCable := True;
        end
        else
        begin
          if Pos('|1|unbound|', Lines[I]) > 0 then HaveToBind := True;
          if Pos('|1|other|', Lines[I]) > 0 then HaveOther := True;
          if Pos('|1|winusb|', Lines[I]) > 0 then HaveOurs := True;
        end;
      end;
  end;
end;

procedure ShowSurvey();
begin
  CableStatus.Caption := 'Looking for an OpenPort or SM2...';
  WizardForm.Update;
  Survey();
  if ReplaceBox.Checked and (HaveOther or TactrixCable) then
    CableStatus.Caption := 'A cable that already has the maker''s driver is plugged in. Click Next to replace that driver with this one; the maker''s driver stays installed, and "J2534 USB driver" in the Start menu puts the cable back on it.'
  else if HaveToBind then
    CableStatus.Caption := 'An OpenPort or SM2 is plugged in and ready. Click Next to install the driver for it.'
  else if TactrixCable then
    CableStatus.Caption := 'Tactrix''s OpenPort driver is already installed on this computer, so setup will leave your OpenPort on it. (To use this driver instead, run "J2534 USB driver" from the Start menu afterwards.)'
  else if HaveOurs then
    CableStatus.Caption := 'Your cable already uses this driver. Nothing more is needed; click Next.'
  else if HaveOther then
    CableStatus.Caption := 'Your cable already has another driver, so setup will leave it alone. (To switch it, run "J2534 USB driver" from the Start menu afterwards.)'
  else
    CableStatus.Caption := 'No OpenPort or SM2 found yet. Plug it into a USB port, wait a few seconds, then click "Check again".';
end;

procedure CheckAgainClick(Sender: TObject);
begin
  ShowSurvey();
end;

procedure ReplaceBoxClick(Sender: TObject);
begin
  ShowSurvey();
end;

procedure InitializeWizard();
begin
  CablePage := CreateCustomPage(wpWelcome, 'Plug in your cable',
    'Connect the OpenPort or SM2 before the driver is installed.');
  CableHelp := TNewStaticText.Create(CablePage);
  CableHelp.Parent := CablePage.Surface;
  CableHelp.WordWrap := True;
  CableHelp.AutoSize := False;
  CableHelp.Left := 0;
  CableHelp.Top := 0;
  CableHelp.Width := CablePage.SurfaceWidth;
  CableHelp.Height := ScaleY(64);
  CableHelp.Caption := 'Plug your Tactrix OpenPort 2.0 or Scanmatik SM2 into a USB port of this computer now. ' +
    'Windows lists an OpenPort as a COM port (an SM2 as an unknown device); setup then gives it the WinUSB driver.';
  CableStatus := TNewStaticText.Create(CablePage);
  CableStatus.Parent := CablePage.Surface;
  CableStatus.WordWrap := True;
  CableStatus.AutoSize := False;
  CableStatus.Left := 0;
  CableStatus.Top := ScaleY(76);
  CableStatus.Width := CablePage.SurfaceWidth;
  CableStatus.Height := ScaleY(80);
  CableStatus.Font.Style := [fsBold];
  CableButton := TNewButton.Create(CablePage);
  CableButton.Parent := CablePage.Surface;
  CableButton.Left := 0;
  CableButton.Top := ScaleY(168);
  CableButton.Width := ScaleX(110);
  CableButton.Height := ScaleY(26);
  CableButton.Caption := 'Check again';
  CableButton.OnClick := @CheckAgainClick;
  ReplaceBox := TNewCheckBox.Create(CablePage);
  ReplaceBox.Parent := CablePage.Surface;
  ReplaceBox.Left := 0;
  ReplaceBox.Top := ScaleY(204);
  ReplaceBox.Width := CablePage.SurfaceWidth;
  ReplaceBox.Height := ScaleY(34);
  ReplaceBox.Caption := 'Also replace the maker''s own driver (Tactrix''s or Scanmatik''s) on a cable that already has one';
  ReplaceBox.OnClick := @ReplaceBoxClick;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = CablePage.ID then
    ShowSurvey();
  if (CurPageID = wpFinished) and (ResultText <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + ResultText;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = CablePage.ID then
  begin
    ShowSurvey();
    // nothing to bind and nothing already set up: ask before going on without the cable
    if not (TactrixCable or HaveToBind or HaveOurs or HaveOther) then
      if MsgBox('No OpenPort or SM2 was found.' + #13#10#13#10 +
                'Plug it in and choose Yes to look again, or choose No to install without ' +
                'it (run "J2534 USB driver" from the Start menu when the cable is plugged in).',
                mbConfirmation, MB_YESNO) = IDYES then
        Result := False
      else
        SkipBinding := True;
  end;
end;

// The engine's result lines -> a sentence for the Finish page.
function FailureReason(const FileName: String): String;
var
  Lines: TArrayOfString;
  I, P: Integer;
begin
  Result := '';
  if LoadStringsFromFile(FileName, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos('FAILED|', Lines[I]) = 1 then
      begin
        Result := Lines[I];
        P := Pos('|', Result);
        Delete(Result, 1, P);
        P := Pos('|', Result);
        Delete(Result, 1, P);
        Exit;
      end;
end;

procedure BindCable();
var
  Code: Integer;
  OutFile, Why, Extra: String;
begin
  OutFile := ExpandConstant('{tmp}\op_install.txt');
  WizardForm.StatusLabel.Caption := 'Installing the USB driver...';
  if ReplaceBox.Checked then
    Extra := '-Replace'
  else
    Extra := '';
  if not RunEngine('install', Extra, OutFile, Code) then
    Code := -1;
  case Code of
    0: if HaveToBind or (ReplaceBox.Checked and (HaveOther or TactrixCable)) then
         ResultText := 'The USB driver was installed for your cable. If OVT-Suite does not see it, unplug the cable and plug it back in.';
    2: ResultText := 'No OpenPort or SM2 was plugged in, so no driver was installed yet. Plug it in and run "J2534 USB driver" from the Start menu.';
    5: ResultText := 'Tactrix''s driver is installed, so your cable was left on it. To use the OVT-Suite driver instead, run "J2534 USB driver" from the Start menu.';
  else
    begin
      Why := FailureReason(OutFile);
      ResultText := 'The USB driver could not be installed (' + Why + '). Run "J2534 USB driver" from the Start menu to try again.';
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not SkipBinding then
    BindCable();
end;
