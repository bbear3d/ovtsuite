; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Optional download: FTDI's USB driver package (CDM, WHQL certified) for FTDI
; K-line cables (addons\README.md), from Microsoft's Update Catalog: the same
; package Windows Update installs, signed by Microsoft. ftdichip.com cannot be
; used: its Cloudflare check answers 403 to anything that is not a browser.
;
; FTDI's driver is NOT shipped in any installer. When it is missing from this
; computer the installer offers it as a task, ticked; the .cab is downloaded
; (FtdiUrl) on the Ready page's Next and checked against FtdiSha256,
; and after the app's files are installed it is unpacked and added with
; pnputil, which asks for administrator permission (UAC) even in a per-user
; install. A failed, refused or skipped install leaves the app working: the
; cable's COM port still works, and Windows Update installs the same driver
; when a cable is plugged in.
;
; Included at the END of installer.iss and tool_installer.iss. build.ps1
; defines FtdiUrl / FtdiSha256 / FtdiVersion / FtdiSizeMB from
; addons\addons.json -- without both a url and a hash nothing here is
; compiled in. Needs Inno Setup 6.1 or newer (CreateDownloadPage).

#ifdef FtdiUrl
#ifndef FtdiSizeMB
  #define FtdiSizeMB "2"
#endif
#ifndef FtdiVersion
  #define FtdiVersion ""
#endif

[Tasks]
Name: "dl_ftdi"; \
    Description: "Download and install FTDI's USB driver {#FtdiVersion} (about {#FtdiSizeMB} MB, from Microsoft's driver catalog; asks for administrator permission) for FTDI K-line cables"; \
    GroupDescription: "Optional downloads:"; Check: FtdiDriverMissing

[Code]
var
  FtdiPage: TDownloadWizardPage;

// The driver is there when its D2XX library is in System32 or its bus driver
// service is registered (64-bit install mode: neither path is redirected).
function FtdiDriverMissing(): Boolean;
begin
  Result := not FileExists(ExpandConstant('{sys}\ftd2xx.dll')) and
            not RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\FTDIBUS');
end;

<event('InitializeWizard')>
procedure FtdiInitializeWizard();
begin
  FtdiPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    'Downloading FTDI''s USB driver...', nil);
end;

<event('NextButtonClick')>
function FtdiNextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('dl_ftdi') then
  begin
    FtdiPage.Clear;
    FtdiPage.Add('{#FtdiUrl}', 'ftdi_cdm.cab', '{#FtdiSha256}');
    FtdiPage.Show;
    try
      try
        FtdiPage.Download;
      except
        if not FtdiPage.AbortedByUser then
          SuppressibleMsgBox('FTDI''s USB driver could not be downloaded:' + #13#10#13#10 +
            GetExceptionMessage + #13#10#13#10 +
            'Setup will continue without it. FTDI cables still work through their ' +
            'COM port, and Windows Update usually installs the driver when a cable ' +
            'is plugged in. It is also at ftdichip.com (Drivers > VCP / D2XX).',
            mbInformation, MB_OK, IDOK);
      end;
    finally
      FtdiPage.Hide;
    end;
  end;
end;

// Unpacks the .cab beside itself (expand.exe keeps its amd64\ and i386\
// folders) and adds every .inf in it with pnputil, elevated. Paths come from $PSScriptRoot, so nothing has to be quoted into
// the script. Exit codes: pnputil's own, 2 = unpacking failed, 1223 = the
// administrator prompt was declined.
function FtdiScript(): String;
begin
  Result :=
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    '$cab = Join-Path $PSScriptRoot ''ftdi_cdm.cab''' + #13#10 +
    '$dir = Join-Path $PSScriptRoot ''ftdi_cdm''' + #13#10 +
    'New-Item -ItemType Directory -Force -Path $dir | Out-Null' + #13#10 +
    '& (Join-Path $env:SystemRoot ''System32\expand.exe'') -F:* $cab $dir | Out-Null' + #13#10 +
    'if (-not (Test-Path (Join-Path $dir ''ftdibus.inf''))) { exit 2 }' + #13#10 +
    '$pnp = Join-Path $env:SystemRoot ''System32\pnputil.exe''' + #13#10 +
    '$arg = ''/add-driver "'' + $dir + ''\*.inf" /subdirs /install''' + #13#10 +
    'try {' + #13#10 +
    '  $p = Start-Process -FilePath $pnp -ArgumentList $arg -Verb RunAs -Wait -PassThru -WindowStyle Hidden' + #13#10 +
    '  exit $p.ExitCode' + #13#10 +
    '} catch { exit 1223 }' + #13#10;
end;

procedure FtdiInstall();
var
  Ps1: String;
  Code: Integer;
begin
  if not FileExists(ExpandConstant('{tmp}\ftdi_cdm.cab')) then
    Exit;
  WizardForm.StatusLabel.Caption := 'Installing FTDI''s USB driver...';
  Ps1 := ExpandConstant('{tmp}\ftdi_install.ps1');
  if not SaveStringToFile(Ps1, FtdiScript(), False) then
    Exit;
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Ps1 + '"',
              '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  // 0 = installed, 3010 = installed (restart needed), 259 = added to the
  // driver store with no cable plugged in to install it on
  if Code = 3010 then
    SuppressibleMsgBox('FTDI''s USB driver is installed. Restart Windows before using an FTDI cable.',
      mbInformation, MB_OK, IDOK)
  else if (Code <> 0) and (Code <> 259) then
    SuppressibleMsgBox('FTDI''s USB driver was not installed (code ' + IntToStr(Code) + ').' + #13#10#13#10 +
      'FTDI cables still work through their COM port. To install the driver ' +
      'later, run Setup again or download it from ftdichip.com (Drivers > VCP / D2XX).',
      mbInformation, MB_OK, IDOK);
end;

<event('CurStepChanged')>
procedure FtdiCurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('dl_ftdi') then
    FtdiInstall();
end;
#endif
