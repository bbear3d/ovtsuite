; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Optional task: the J2534 USB driver for a Tactrix OpenPort 2.0 or a Scanmatik
; SM2 -- Microsoft's own WinUSB driver bound to the cable, so OVT-Suite drives it
; directly through libusb ("USB direct"). No driver file or certificate is
; added; packaging\j2534_driver\j2534_driver.ps1 is the whole engine, the same
; one the stand-alone "J2534 USB driver" add-on installer
; (j2534_driver_installer.iss) runs.
;
; Included by installer.iss, so the full and the base installer both have it
; (tool_installer.iss does not). It is always offered, and setup looks at what is
; plugged in to decide whether it starts ticked:
;
;   * ticked  -- a supported cable is plugged in and has no J2534 driver of its
;                maker (no Tactrix / Scanmatik J2534 registration, not on another
;                driver): there is something to do and nothing to overwrite;
;   * unticked -- no supported cable is plugged in, or every one already has a
;                driver (its maker's, WinUSB's, Zadig's ...).
;
; Ticking it by hand, when it started unticked, opens a "Plug in your cable" page
; after the tasks page: it looks for the cable (Check again) and offers a box to
; replace the maker's own driver on a cable that already has one. The maker's
; driver stays installed; the replacement is recorded by the engine, and
; "USB driver (OpenPort and SM2)" in the Start menu (or Uninstall, which asks)
; puts the cable back on it (j2534_driver.ps1 -Action remove).
;
; Binding a driver needs administrator rights, which a per-user install does not
; have: the engine is started through "runas", so Windows asks (UAC). Declining
; leaves the app installed and works as "not installed".
;
; Included at the END of installer.iss (see its note on sections and [Code]); its
; [Code] uses event attributes, so its place among the other [Code] does not matter.

; The driver package's version (packaging\j2534_driver\VERSION). Setup records it once the
; cables are set up and the J2534 DLL registered, and leaves them alone while it is unchanged.
#define UsbVerFile FileOpen(AddBackslash(SourcePath) + "j2534_driver\VERSION")
#define UsbDrvVer Trim(FileRead(UsbVerFile))
#expr FileClose(UsbVerFile)

[Tasks]
Name: "usbdriver"; \
    Description: "Install OVT-Suite J2534 drivers for Tactrix OpenPort 2.0 and Scanmatik SM2 cables (asks for administrator permission)"; \
    GroupDescription: "USB drivers:"; Flags: unchecked
; Separate from the driver above: this lists the cable as a J2534 device for OTHER programs.
; OVT-Suite itself never uses that DLL (it drives the cable directly), and it needs the
; cable on WinUSB, whoever put it there.
Name: "j2534reg"; \
    Description: "Register the OVT-Suite J2534 driver for other J2534 programs (asks for administrator permission)"; \
    GroupDescription: "USB drivers:"; Flags: unchecked

; The native J2534 DLLs and the configurator (tools\j2534drv, built into j2534_driver\native by
; build.ps1 when MSYS2 is there). A RELEASE build must have all four: when the build is signed
; (/DSignTool) or /DRequireJ2534 is given, a missing one stops the compile with an error instead of
; shipping an installer whose "Register J2534 drivers" task has nothing to register. A development
; build without them still compiles (the files are left out, as before).
#define J2534Native AddBackslash(SourcePath) + "j2534_driver\native\"
#if !FileExists(J2534Native + "ovtj2534_x86.dll") || !FileExists(J2534Native + "ovtj2534_x64.dll") || !FileExists(J2534Native + "ovtj2534cfg_x86.exe") || !FileExists(J2534Native + "ovtj2534cfg_x64.exe")
  #if defined(SignTool) || defined(RequireJ2534)
    #error The native J2534 driver is incomplete: packaging\j2534_driver\native needs ovtj2534_x86.dll, ovtj2534_x64.dll, ovtj2534cfg_x86.exe and ovtj2534cfg_x64.exe (build tools\j2534drv with MSYS2 mingw32 and mingw64; build.ps1 does it).
  #else
    #define J2534Skip " skipifsourcedoesntexist"
  #endif
#else
  #define J2534Skip ""
#endif

[Files]
; the engine, kept so the Start menu entry and Uninstall can switch a cable back ...
Source: "j2534_driver\j2534_driver.ps1"; DestDir: "{app}\usbdriver"; Flags: ignoreversion
Source: "j2534_driver\j2534_driver.vbs"; DestDir: "{app}\usbdriver"; Flags: ignoreversion
Source: "ovtsuite_openport.ico"; DestDir: "{app}\usbdriver"; Flags: ignoreversion
; the native J2534 DLL for other programs and its configurator (tools\j2534drv; build.ps1 builds
; them into j2534_driver\native; see the check above for when a missing one is an error)
Source: "j2534_driver\native\*"; DestDir: "{app}\usbdriver"; Flags: ignoreversion{#J2534Skip}
; ... and the engine extracted to the temp folder for the wizard's own look at the cables
Source: "j2534_driver\j2534_driver.ps1"; DestDir: "{tmp}"; Flags: dontcopy

[Icons]
Name: "{group}\USB driver (OpenPort and SM2)"; \
    Filename: "{sys}\wscript.exe"; \
    Parameters: "//B //Nologo ""{app}\usbdriver\j2534_driver.vbs"""; \
    IconFilename: "{app}\usbdriver\ovtsuite_openport.ico"; \
    Comment: "Install, override or restore the USB driver of an OpenPort or SM2"
Name: "{group}\Register J2534 drivers (OpenPort and SM2)"; \
    Filename: "{app}\usbdriver\ovtj2534cfg_x86.exe"; \
    IconFilename: "{app}\usbdriver\ovtsuite_openport.ico"; \
    Comment: "Lists the OpenPort and SM2 that is plugged in for other J2534 programs"; \
    Check: UsbHaveConfigurator
Name: "{group}\Deregister J2534 drivers"; \
    Filename: "{app}\usbdriver\ovtj2534cfg_x86.exe"; \
    Parameters: "unregister --elevate --pause"; \
    IconFilename: "{app}\usbdriver\ovtsuite_openport.ico"; \
    Comment: "Removes the OpenPort and SM2 entries OVT-Suite made for other J2534 programs"; \
    Check: UsbHaveConfigurator

[UninstallDelete]
; the per-cable copies of the J2534 DLL the configurator makes
Type: files; Name: "{app}\usbdriver\ovtj2534_*"

[Code]
var
  UsbPage: TWizardPage;
  UsbHelp, UsbStatus, UsbReplaceNote: TNewStaticText;
  UsbButton: TNewButton;
  UsbReplaceBox: TNewCheckBox;
  UsbSurveyed, UsbSurveyDone, UsbAutoOn, UsbManual, UsbSkip, UsbUpToDate: Boolean;
  // what is plugged in, from the engine's `status`
  UsbVendorOpenPort, UsbVendorSm2: Boolean;   // Tactrix's / Scanmatik's J2534 driver is installed
  UsbToBind, UsbOther, UsbOurs, UsbCovered: Boolean;
  UsbResult: String;

function UsbPowerShell(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

// Runs the engine from the temp copy, its lines landing in OutFile. Elevated
// (UAC) for what changes a driver, plain for `status`. False when it could not
// be started or the administrator prompt was declined.
function UsbRun(Action, Extra, OutFile: String; Elevated: Boolean; var Code: Integer): Boolean;
var
  Args: String;
begin
  ExtractTemporaryFile('j2534_driver.ps1');
  DeleteFile(OutFile);
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\j2534_driver.ps1') + '" -Action ' + Action + ' ' + Extra +
    ' -Out "' + OutFile + '"';
  if Elevated then
    Result := ShellExec('runas', UsbPowerShell(), Args, '', SW_HIDE, ewWaitUntilTerminated, Code)
  else
    Result := Exec(UsbPowerShell(), Args, '', SW_HIDE, ewWaitUntilTerminated, Code);
  if not Result then
    Code := -1;
end;

// What an earlier setup recorded under HKA\Software\OVT-Suite\UsbDriver ('' when nothing).
function UsbRecorded(Name: String): String;
begin
  if not RegQueryStringValue(HKA, 'Software\OVT-Suite\UsbDriver', Name, Result) then
    Result := '';
end;

// True when Windows has an OpenPort or SM2 on the WinUSB driver -- plugged in or not: the
// device's own entry under Enum\USB names its driver service. A registry read, instant,
// where asking the engine means starting PowerShell.
function UsbWinUsbKnown(): Boolean;
var
  IDs, Insts: TArrayOfString;
  I, J: Integer;
  Svc: String;
begin
  Result := False;
  SetArrayLength(IDs, 2);
  IDs[0] := 'VID_0403&PID_CC4D';      // Tactrix OpenPort 2.0
  IDs[1] := 'VID_20A2&PID_0001';      // Scanmatik SM2
  for I := 0 to 1 do
    if RegGetSubkeyNames(HKLM, 'SYSTEM\CurrentControlSet\Enum\USB\' + IDs[I], Insts) then
      for J := 0 to GetArrayLength(Insts) - 1 do
        if RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Enum\USB\' + IDs[I] + '\' + Insts[J], 'Service', Svc) and
           (CompareText(Svc, 'WinUSB') = 0) then
        begin
          Result := True;
          Exit;
        end;
end;

// Looks at what is plugged in and what each cable already has.
procedure UsbSurvey();
var
  Lines: TArrayOfString;
  I, Code: Integer;
  Line: String;
begin
  UsbSurveyDone := True;
  UsbVendorOpenPort := False;
  UsbVendorSm2 := False;
  UsbToBind := False;
  UsbOther := False;
  UsbOurs := False;
  UsbCovered := False;
  if UsbRun('status', '', ExpandConstant('{tmp}\usb_status.txt'), False, Code) and
     LoadStringsFromFile(ExpandConstant('{tmp}\usb_status.txt'), Lines) then
  begin
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      if Pos('TACTRIX|1', Lines[I]) = 1 then UsbVendorOpenPort := True;
      if Pos('SCANMATIK|1', Lines[I]) = 1 then UsbVendorSm2 := True;
    end;
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Line := Lines[I];
      if Pos('DEVICE|', Line) <> 1 then Continue;
      if Pos('|1|', Line) = 0 then Continue;                 // not plugged in
      if Pos('|1|winusb|', Line) > 0 then UsbOurs := True
      else if Pos('|1|other|', Line) > 0 then UsbOther := True
      else if ((Pos('VID_0403', Line) > 0) and UsbVendorOpenPort) or
              ((Pos('VID_20A2', Line) > 0) and UsbVendorSm2) then UsbCovered := True
      else UsbToBind := True;                                // generic serial driver, or none
    end;
  end;
end;

// Force: look again (Check again, the replace box); otherwise reuse what the tasks page
// already found -- each look starts PowerShell and takes a second or more.
procedure UsbShowSurvey(Force: Boolean);
begin
  if Force or not UsbSurveyDone then
  begin
    UsbStatus.Caption := 'Looking for an OpenPort or SM2...';
    WizardForm.Update;
    UsbSurvey();
    UsbSurveyed := True;
  end;
  if UsbReplaceBox.Checked and (UsbOther or UsbCovered) then
    UsbStatus.Caption := 'A cable that already has its maker''s driver is plugged in. Click Next to replace that driver with this one; the maker''s driver stays installed, and "USB driver (OpenPort and SM2)" in the Start menu puts the cable back on it.'
  else if UsbToBind then
    UsbStatus.Caption := 'An OpenPort or SM2 is plugged in and ready. Click Next to install the driver for it.'
  else if UsbCovered then
    UsbStatus.Caption := 'The maker''s J2534 driver is already installed for your cable, so setup will leave it alone. Tick the box below to use this driver instead.'
  else if UsbOurs then
    UsbStatus.Caption := 'Your cable already uses this driver. Nothing more is needed; click Next.'
  else if UsbOther then
    UsbStatus.Caption := 'Your cable already has another driver, so setup will leave it alone. Tick the box below to replace it with this one.'
  else
    UsbStatus.Caption := 'No OpenPort or SM2 found yet. Plug it into a USB port, wait a few seconds, then click "Check again" -- or click Next to go on without it: the USB driver utility is installed either way, and binds the driver whenever you run it from the Start menu.';
end;

procedure UsbCheckAgainClick(Sender: TObject);
begin
  UsbShowSurvey(True);
end;

// The replace box only changes the wording, not what is plugged in.
procedure UsbReplaceBoxClick(Sender: TObject);
begin
  UsbShowSurvey(False);
end;

<event('InitializeWizard')>
procedure UsbInitializeWizard();
begin
  UsbPage := CreateCustomPage(wpSelectTasks, 'Plug in your cable',
    'Connect the OpenPort or SM2 before the driver is installed.');
  UsbHelp := TNewStaticText.Create(UsbPage);
  UsbHelp.Parent := UsbPage.Surface;
  UsbHelp.WordWrap := True;
  UsbHelp.AutoSize := False;
  UsbHelp.Left := 0;
  UsbHelp.Top := 0;
  UsbHelp.Width := UsbPage.SurfaceWidth;
  UsbHelp.Height := ScaleY(64);
  UsbHelp.Caption := 'Plug your Tactrix OpenPort 2.0 or Scanmatik SM2 into a USB port of this computer now. ' +
    'Windows lists an OpenPort as a COM port (an SM2 as an unknown device); setup then gives it the WinUSB driver.';
  UsbStatus := TNewStaticText.Create(UsbPage);
  UsbStatus.Parent := UsbPage.Surface;
  UsbStatus.WordWrap := True;
  UsbStatus.AutoSize := False;
  UsbStatus.Left := 0;
  UsbStatus.Top := ScaleY(76);
  UsbStatus.Width := UsbPage.SurfaceWidth;
  UsbStatus.Height := ScaleY(80);
  UsbStatus.Font.Style := [fsBold];
  UsbButton := TNewButton.Create(UsbPage);
  UsbButton.Parent := UsbPage.Surface;
  UsbButton.Left := 0;
  UsbButton.Top := ScaleY(168);
  UsbButton.Width := ScaleX(110);
  UsbButton.Height := ScaleY(26);
  UsbButton.Caption := 'Check again';
  UsbButton.OnClick := @UsbCheckAgainClick;
  UsbReplaceBox := TNewCheckBox.Create(UsbPage);
  UsbReplaceBox.Parent := UsbPage.Surface;
  UsbReplaceBox.Left := 0;
  UsbReplaceBox.Top := ScaleY(204);
  UsbReplaceBox.Width := UsbPage.SurfaceWidth;
  UsbReplaceBox.Height := ScaleY(20);          // TNewCheckBox cannot wrap: a short caption ...
  UsbReplaceBox.Caption := 'Replace the maker''s own driver (Tactrix''s or Scanmatik''s)';
  UsbReplaceBox.OnClick := @UsbReplaceBoxClick;
  // ... and the rest of the sentence in a label under it, which does wrap
  UsbReplaceNote := TNewStaticText.Create(UsbPage);
  UsbReplaceNote.Parent := UsbPage.Surface;
  UsbReplaceNote.WordWrap := True;
  UsbReplaceNote.AutoSize := False;
  UsbReplaceNote.Left := ScaleX(18);
  UsbReplaceNote.Top := ScaleY(226);
  UsbReplaceNote.Width := UsbPage.SurfaceWidth - ScaleX(18);
  UsbReplaceNote.Height := ScaleY(32);
  UsbReplaceNote.Caption := 'on a cable that already has one. The maker''s driver stays installed, and can be switched back from the Start menu.';
end;

// The task starts ticked only when a cable is plugged in that has nothing yet.
// Looked at once, when the tasks page first shows; what the user does after that stays.
<event('CurPageChanged')>
procedure UsbCurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpSelectTasks) and not UsbSurveyed and not WizardSilent then
  begin
    UsbSurveyed := True;
    // The driver is already in place here (recorded by an earlier setup, or a cable is on
    // WinUSB whoever put it there): no look at the cables
    // (about a second of PowerShell each) and no driver task ticked. The tasks can still be
    // ticked by hand, and the page then looks for the cable.
    UsbUpToDate := (UsbRecorded('Version') = '{#UsbDrvVer}') or UsbWinUsbKnown();
    if UsbUpToDate then
      // setup re-ticks what was ticked last time: untick it, nothing is left to install
      WizardSelectTasks('!usbdriver')
    else
    begin
      WizardForm.Cursor := crHourGlass;
      try
        UsbSurvey();
      finally
        WizardForm.Cursor := crDefault;
      end;
      UsbAutoOn := UsbToBind;
      if UsbAutoOn then
        WizardSelectTasks('usbdriver');
    end;
    // the J2534 registration points at DLLs in the app folder, which setup has just replaced:
    // redo it only when a different driver version made it
    if UsbRecorded('RegisteredVersion') = '{#UsbDrvVer}' then
      WizardSelectTasks('!j2534reg')           // registered by this version: no second UAC prompt
    else if UsbRecorded('RegisteredVersion') <> '' then
      WizardSelectTasks('j2534reg');
  end;
  if CurPageID = UsbPage.ID then
    UsbShowSurvey(False);
  if (CurPageID = wpFinished) and (UsbResult <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + UsbResult;
end;

// The page is for a task ticked by hand (it asks for the cable and offers to
// replace a driver), or for a ticked one with nothing plugged in yet. A task that
// started ticked because a cable is ready goes straight on.
<event('ShouldSkipPage')>
function UsbShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = UsbPage.ID then
  begin
    UsbManual := not UsbAutoOn;
    Result := not WizardIsTaskSelected('usbdriver') or (UsbAutoOn and UsbToBind) or WizardSilent;
  end;
end;

<event('NextButtonClick')>
function UsbNextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = UsbPage.ID then
  begin
    UsbShowSurvey(False);
    // no cable plugged in: nothing to bind now. Go on without asking -- the utility that binds
    // the driver later (Start menu) is installed either way.
    UsbSkip := not (UsbCovered or UsbToBind or UsbOurs or UsbOther);
    if UsbSkip then
      UsbResult := 'No OpenPort or SM2 was plugged in, so no driver was installed yet. The USB driver utility is installed: plug the cable in, then run "USB driver (OpenPort and SM2)" from the Start menu to give it the WinUSB driver.';
  end;
end;

// The engine's result lines -> the reason of the first failure.
function UsbFailureReason(const FileName: String): String;
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

function UsbHaveConfigurator(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\usbdriver\ovtj2534cfg_x86.exe'));
end;

// The configurator lists a J2534 entry for each cable that is plugged in and on WinUSB, so other
// J2534 programs see it. Elevated like the engine. 0 = registered, 2 = nothing to register.
function UsbRegister(): Integer;
var
  Code: Integer;
  Exe: String;
begin
  Result := 2;
  Exe := ExpandConstant('{app}\usbdriver\ovtj2534cfg_x86.exe');
  if not FileExists(Exe) then
    Exit;
  WizardForm.StatusLabel.Caption := 'Registering the J2534 drivers...';
  Sleep(1500);                                   // the cable has just been given its driver
  if ShellExec('runas', Exe, 'register', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code
  else
    Result := 3;
  if Result = 2 then
  begin
    Sleep(2500);
    if ShellExec('runas', Exe, 'register', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := Code;
  end;
  if Result = 0 then
  begin
    // only the version is kept (to redo the registration after an update); whether anything is
    // registered is read from the registry at uninstall, never from a flag
    RegWriteStringValue(HKA, 'Software\OVT-Suite\UsbDriver', 'RegisteredVersion', '{#UsbDrvVer}');
  end;
end;

procedure UsbBind();
var
  Code: Integer;
  OutFile, Why, Extra: String;
  Replace: Boolean;
begin
  OutFile := ExpandConstant('{tmp}\usb_install.txt');
  WizardForm.StatusLabel.Caption := 'Installing the USB driver...';
  Replace := UsbManual and UsbReplaceBox.Checked;
  if Replace then
    Extra := '-Replace'
  else
    Extra := '';
  if not UsbRun('install', Extra, OutFile, True, Code) then
    Code := -1;
  case Code of
    0: begin
         if UsbToBind or (Replace and (UsbOther or UsbCovered)) then
         begin
           UsbResult := 'The USB driver was installed for your cable. If OVT-Suite does not see it, unplug the cable and plug it back in. "USB driver (OpenPort and SM2)" in the Start menu switches a cable back to its own driver.';
           RegWriteDWordValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Installed', 1);
           RegWriteStringValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Version', '{#UsbDrvVer}');
         end;
       end;
    2: UsbResult := 'No OpenPort or SM2 was plugged in, so no driver was installed yet. Plug it in and run "USB driver (OpenPort and SM2)" from the Start menu.';
    5: UsbResult := 'The maker''s driver is installed, so your cable was left on it. To use the OVT-Suite driver instead, run "USB driver (OpenPort and SM2)" from the Start menu.';
    -1: UsbResult := 'The USB driver was not installed: administrator permission was not given. Run "USB driver (OpenPort and SM2)" from the Start menu to try again.';
  else
    begin
      Why := UsbFailureReason(OutFile);
      UsbResult := 'The USB driver could not be installed (' + Why + '). Run "USB driver (OpenPort and SM2)" from the Start menu to try again.';
    end;
  end;
end;

// The "j2534reg" task: list a cable that is on WinUSB as a J2534 device for other programs.
procedure UsbRegisterTask();
var
  R: Integer;
  Msg: String;
begin
  R := UsbRegister();
  case R of
    0: Msg := 'The OVT-Suite J2534 driver is registered for other J2534 programs.';
    2: Msg := 'No OpenPort or SM2 on the WinUSB driver was found, so no J2534 driver was registered. Plug the cable in and run "Register J2534 drivers (OpenPort and SM2)" from the Start menu.';
    3: Msg := 'The J2534 driver was not registered: administrator permission was not given. Run "Register J2534 drivers (OpenPort and SM2)" from the Start menu to try again.';
    4: Msg := 'The J2534 driver could not be registered completely (a file could not be copied, or one of the two registry views could not be written). Nothing it had just added was kept. Close any program that uses J2534 and run "Register J2534 drivers (OpenPort and SM2)" from the Start menu to try again; "Deregister J2534 drivers" clears what is left.';
  else
    Msg := 'The J2534 driver could not be registered. Run "Register J2534 drivers (OpenPort and SM2)" from the Start menu to try again.';
  end;
  if UsbResult <> '' then
    UsbResult := UsbResult + #13#10#13#10;
  UsbResult := UsbResult + Msg;
end;

<event('CurStepChanged')>
procedure UsbCurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('usbdriver') and not UsbSkip then
    UsbBind();
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('j2534reg') then
    UsbRegisterTask();
  // a cable already on WinUSB (set up by hand, or by an earlier version): nothing to do, so
  // note that, and the next setup skips the look
  if (CurStep = ssPostInstall) and UsbOurs and not UsbUpToDate then
    RegWriteStringValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Version', '{#UsbDrvVer}');
end;

// True when an "OVT-Suite*" key is under PassThruSupport.04.04 in the given registry view.
function UsbHasOurEntries(Root: Integer): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if RegGetSubkeyNames(Root, 'SOFTWARE\PassThruSupport.04.04', Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if CompareText(Copy(Names[I], 1, 9), 'OVT-Suite') = 0 then
      begin
        Result := True;
        Exit;
      end;
end;

function UsbAnyOurEntries(): Boolean;
begin
  Result := UsbHasOurEntries(HKLM32);
  if (not Result) and IsWin64 then
    Result := UsbHasOurEntries(HKLM64);
end;

// The configurator register makes in the machine-wide folder (the app's own copy goes with the app).
function UsbMachineConfigurator(): String;
var
  Base: String;
begin
  Base := GetEnv('CommonProgramW6432');
  if Base = '' then
    Base := GetEnv('CommonProgramFiles');
  Result := Base + '\OVT-Suite\J2534\ovtj2534cfg_x86.exe';
end;

// Uninstall: remove the J2534 registrations (whatever made them) and the machine-wide DLLs.
procedure UsbDeregisterJ2534();
var
  Code: Integer;
  Exe, Cmd, Why: String;
  Ran: Boolean;
begin
  if not UsbAnyOurEntries() then
    Exit;
  Exe := ExpandConstant('{app}\usbdriver\ovtj2534cfg_x86.exe');
  if not FileExists(Exe) then
    Exe := UsbMachineConfigurator();
  Cmd := '"' + UsbMachineConfigurator() + '" unregister';
  Code := -1;
  Ran := FileExists(Exe) and ShellExec('runas', Exe, 'unregister', '', SW_HIDE, ewWaitUntilTerminated, Code);
  if Ran and (Code = 0) and not UsbAnyOurEntries() then
    Exit;
  if not Ran then
    Why := 'administrator permission was not given, or the configurator was not found'
  else if Code = 4 then
    Why := 'one of the two registry views could not be changed (exit code 4)'
  else if Code <> 0 then
    Why := 'the configurator reported exit code ' + IntToStr(Code)
  else
    Why := 'an entry is still there';
  Log('J2534 deregistration incomplete: ' + Why);
  if not UninstallSilent then
    MsgBox('OVT-Suite''s J2534 entries could not be removed (' + Why + '), so other J2534 programs may still list them.' + #13#10#13#10 +
           'To remove them, open a Command Prompt as administrator and run:' + #13#10#13#10 + Cmd,
           mbError, MB_OK);
end;

// Uninstalling: a cable this setup put on the WinUSB driver is offered back its own
// driver (the engine's `remove`). Asked, not assumed -- other programs may use it.
<event('CurUninstallStepChanged')>
procedure UsbCurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Ps1: String;
begin
  if (CurUninstallStep = usUninstall) and
     RegValueExists(HKA, 'Software\OVT-Suite\UsbDriver', 'Installed') then
  begin
    if not UninstallSilent then
    begin
      Ps1 := ExpandConstant('{app}\usbdriver\j2534_driver.ps1');
      if FileExists(Ps1) and
         (MsgBox('Setup put your OpenPort and SM2 on the OVT-Suite USB driver (WinUSB).' + #13#10#13#10 +
                 'Switch the cable back to the driver it had before? (Windows asks for administrator permission.)',
                 mbConfirmation, MB_YESNO) = IDYES) then
        ShellExec('runas', ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
          '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Ps1 + '" -Action remove',
          '', SW_HIDE, ewWaitUntilTerminated, Code);
    end;
    RegDeleteValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Installed');
    RegDeleteKeyIfEmpty(HKA, 'Software\OVT-Suite\UsbDriver');
  end;
  // The J2534 entries live in HKLM, outside this uninstall's reach: look at the registry (both
  // views) rather than at a flag, and have the configurator remove them (asks for administrator).
  if CurUninstallStep = usUninstall then
    UsbDeregisterJ2534();
  if CurUninstallStep = usUninstall then
  begin
    RegDeleteValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Registered');       // set by older setups
    RegDeleteValue(HKA, 'Software\OVT-Suite\UsbDriver', 'RegisteredVersion');
    RegDeleteValue(HKA, 'Software\OVT-Suite\UsbDriver', 'Version');
    RegDeleteKeyIfEmpty(HKA, 'Software\OVT-Suite\UsbDriver');
  end;
end;
