; SPDX-License-Identifier: MPL-2.0
; Copyright (c) 2026 Blair Campbell
; This Source Code Form is subject to the terms of the Mozilla Public
; License, v. 2.0. If a copy of the MPL was not distributed with this
; file, You can obtain one at https://mozilla.org/MPL/2.0/.
; The bundled modules' licence agreement, accepted on the License page of an
; installer that carries modules (installer.iss and tool_installer.iss with
; ModulesInclude: LicenseFile is the module EULA there). After installing,
; every installed module that carries the agreement is marked accepted --
; <app>\modules\<id>\<id>\EULA.accepted holding the agreement's SHA-256, as
; module_installer.iss writes it -- so the app does not ask again at its first
; start (ovtsuite/modules/eula.py).

[Code]
<event('CurStepChanged')>
procedure ModulesEulaCurStepChanged(CurStep: TSetupStep);
var
  FindRec: TFindRec;
  Dir, Pkg: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Dir := ExpandConstant('{app}\modules\');
  if FindFirst(Dir + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and
           (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Pkg := Dir + FindRec.Name + '\' + FindRec.Name + '\';
          if FileExists(Pkg + 'EULA.txt') then
            SaveStringToFile(Pkg + 'EULA.accepted', GetSHA256OfFile(Pkg + 'EULA.txt'), False);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;
