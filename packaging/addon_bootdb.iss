; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; Optional download: the EDC16 boot-sector database add-on (addons\README.md).
;
; Included at the END of installer.iss and tool_installer.iss (flasher builds).
; It is not part of the installer's payload: when the user ticks the task, the
; file is downloaded from BootDbUrl on the Ready page's Next and checked against
; BootDbSha256; a failed or skipped download leaves the app working without it.
; build.ps1 defines BootDbUrl / BootDbSha256 / BootDbSizeMB from
; addons\addons.json -- without a URL nothing here is compiled in.
; Needs Inno Setup 6.1 or newer (CreateDownloadPage).

#ifdef BootDbUrl
#ifndef BootDbSizeMB
  #define BootDbSizeMB "1"
#endif

[Tasks]
Name: "dl_bootdb"; \
    Description: "Download the EDC16 boot-sector database (about {#BootDbSizeMB} MB) for the flasher's virtual boot read"; \
    GroupDescription: "Optional downloads:"; Flags: unchecked

[Files]
Source: "{tmp}\edc16_bootdb.bin"; DestDir: "{app}\addons"; Tasks: dl_bootdb; \
    Flags: external skipifsourcedoesntexist ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\addons"

[Code]
var
  BootDbPage: TDownloadWizardPage;

<event('InitializeWizard')>
procedure BootDbInitializeWizard();
begin
  BootDbPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    'Downloading the EDC16 boot-sector database...', nil);
end;

<event('NextButtonClick')>
function BootDbNextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('dl_bootdb') then
  begin
    BootDbPage.Clear;
    BootDbPage.Add('{#BootDbUrl}', 'edc16_bootdb.bin', '{#BootDbSha256}');
    BootDbPage.Show;
    try
      try
        BootDbPage.Download;
      except
        if not BootDbPage.AbortedByUser then
          SuppressibleMsgBox('The EDC16 boot-sector database could not be downloaded:' + #13#10#13#10 +
            GetExceptionMessage + #13#10#13#10 +
            'Setup will continue without it. The flasher works without it; only its ' +
            'virtual boot read needs it. You can run Setup again later to retry.',
            mbInformation, MB_OK, IDOK);
      end;
    finally
      BootDbPage.Hide;
    end;
  end;
end;
#endif
