' SPDX-License-Identifier: MPL-2.0
' Copyright (c) 2026 Blair Campbell
' This Source Code Form is subject to the terms of the Mozilla Public
' License, v. 2.0. If a copy of the MPL was not distributed with this
' file, You can obtain one at https://mozilla.org/MPL/2.0/.
'
' Opens the J2534 USB driver window (j2534_driver.ps1 -Action manage) with
' administrator rights and without any console window: the Start menu entry runs
' this through wscript.exe, which has no window of its own, and the elevated
' PowerShell is started hidden by the shell itself, so nothing flashes.
Dim fso, shell, here, ps, args
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ps = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\j2534_driver.ps1"" -Action manage"
' "runas" asks for administrator rights (UAC); 0 = hidden window
CreateObject("Shell.Application").ShellExecute ps, args, here, "runas", 0
