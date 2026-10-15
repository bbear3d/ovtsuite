# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
<#
.SYNOPSIS
    Builds the OVT-Suite Windows executable.

.DESCRIPTION
    Finds a usable Python 3.10+ interpreter, creates (or reuses) an isolated
    build environment, installs the runtime dependencies plus PyInstaller,
    syncs the Windows version resource with ovtsuite.__version__, runs the test
    suite, and produces:

        dist\OVTSuite\OVTSuite.exe    (default, one-folder)
        dist\OVTSuite.exe                (with -OneFile)

    One-folder starts in well under a second and is the layout to ship in a
    zip. One-file is a single portable .exe but unpacks ~100 MB to %TEMP% on
    every launch, so it takes several seconds to start.

    MODULES (docs\MODULES_PLAN.md). The executable is the BASE app; every
    control-module module is a signed package of its own:

        dist\modules\<id>-<version>.ovtmod            full module
        dist\modules\<id>-<version>-checksum.ovtmod   checksum-only variant

    and dist\OVTSuite\modules\ holds every module installed, so
    dist\OVTSuite\OVTSuite.exe runs as the full app. Only what changed is
    rebuilt: a module's package when its files change, the frozen base app
    (PyInstaller) only when the base app's files or the packaging change.

    When Inno Setup is installed, the build is also packaged into

        dist\OVTSuite-<version>-setup.exe          full: base + a module checklist

    and, with -Installers, into any of

        dist\OVTSuite-Base-<version>-setup.exe     base app only
        dist\module-installers\OVTSuite-<Module>-<module version>-setup.exe
                                                   one per module (-Installers modules)
        dist\module-installers\OVTSuite-<Module>-Checksum-<module version>-setup.exe
                                                   (-Installers modules-checksum)
        dist\OVTFlasher-<version>-setup.exe   dist\OVTSniffer-<version>-setup.exe
        dist\OVTLogger-<version>-setup.exe    dist\OVTTools-<version>-setup.exe

    A module installer refuses to install unless OVT-Suite or OVT Tools is
    installed. The tool installers offer every module's checksum variant.

    Interpreter discovery, in order:
      1. -Python <path>                    explicit, always wins
      2. $env:OVTSUITE_PYTHON
      3. .venv\Scripts\python.exe          this project's own venv
      4. uv                                'uv python find'
      5. py launcher                       'py --list-paths'
      6. python / python3 on PATH          (Store alias stubs are skipped)
      7. Well-known install directories    Program Files, LocalAppData, C:\

    Repeat builds are incremental. Each step below is skipped when its inputs
    have not changed since the last build:

      interpreter discovery   the existing .build-venv is reused as is
      dependency install      a stamp of the requirement list, kept in the env
      layout catalogue        tools\generate_layouts.py --if-stale (hashes of
                              every binary, module and KB file it read)
      PyInstaller analysis    build\ is kept, so PyInstaller's own dependency
                              tracking reuses whatever has not changed
      32-bit J2534 bridge     same, under build\bridge32
      version resource        only rewritten when the version changes

    -Clean throws all of it away; -Update re-runs just the dependency step.
    Each step prints how long it took, and the total is shown at the end.

.PARAMETER Python
    Path to a specific python.exe to build with. Skips discovery.

.PARAMETER ListPython
    Print every interpreter found by discovery, then exit without building.

.PARAMETER NoUv
    Use 'python -m venv' + pip even when uv is available.

.PARAMETER OneFile
    Produce a single self-extracting .exe instead of a folder.

.PARAMETER Console
    Keep a console window attached (shows tracebacks and print output).

.PARAMETER Test
    Run the pytest suite before building. Tests are OFF by default (they take
    a while and a lot of memory); pass -Test for a build you intend to ship.

.PARAMETER SkipTests
    Deprecated: tests are already off by default. Kept so old command lines
    keep working; wins over -Test if both are given.

.PARAMETER InnoSetup
    Path to ISCC.exe. Skips Inno Setup discovery.

.PARAMETER NoInstaller
    Do not build any installer even if Inno Setup is available.

.PARAMETER Installers
    Which installers to build, comma-separated (default: full):
      full      OVT-Suite with a Components page listing every module, all
                ticked ('main' is the old name for it)
      base      OVT-Suite, base app only
      modules   one installer per module (full variant)
      modules-checksum   one installer per module, checksum-only variant
      flasher   OVT Flasher only        sniffer   OVT Sniffer only
      logger    OVT Logger only         tools     all three tools, no main app
    The tool installers ship the same build minus the other tools' launchers
    and the full modules (OVTSuite.exe included: every launcher starts it),
    install to their own folder, and upgrade/uninstall on their own, so they
    can sit beside the main app or replace it.

.PARAMETER Modules
    Which modules to package, comma-separated module ids, or all (default):
    edc16, edc15, zf6hp, zf8hp, aisin, vdo, hvac.

.PARAMETER BaseOnly
    Build the base app only: no module packages, and dist\OVTSuite runs with
    no modules installed (what a base-only user sees).

.PARAMETER Unsigned
    Do not sign the module packages (development only: the built app refuses
    unsigned modules unless "Allow unsigned modules" is ticked in Tools >
    Modules, or OVT_ALLOW_UNSIGNED=1). By default the build signs with the key
    in $env:OVT_SIGNING_KEY, or %USERPROFILE%\.ovt-signing\module-key.pem,
    creating it on the first build (back it up) and adding its public half to
    ovtsuite\data\module_keys.json (commit that file).

.PARAMETER CodeSign
    Authenticode-sign the executables and every installer (and its
    uninstaller) with signtool.exe. The signing options come from
    $env:OVT_CODESIGN_ARGS, e.g.
        /sha1 <certificate thumbprint> /fd sha256 /tr http://timestamp.digicert.com /td sha256
    or, for Azure Trusted Signing,
        /fd sha256 /tr http://timestamp.acs.microsoft.com /td sha256 /dlib <path>\Azure.CodeSigning.Dlib.dll /dmdf <path>\metadata.json
    signtool.exe is found on PATH, in the Windows SDK, or at $env:OVT_SIGNTOOL.
    Implied when OVT_CODESIGN_ARGS is set.

    Every build also writes dist\SHA256SUMS.txt (the SHA-256 of the installers
    and module packages it made, merged into the list already there) and,
    unless -Unsigned, its Ed25519 signature dist\SHA256SUMS.txt.sig with the
    module signing key. Upload both with the release: the app's update check
    verifies every download against them.

    DELTA UPDATES (docs\UPDATES.md). Every one-folder build writes
    dist\OVTSuite\app-manifest.json (+ .sig): the SHA-256 of every file the
    installer ships. A signed build then, automatically (no switch needed):

      * removes the delta packages an earlier build left in dist\,
      * makes dist\OVTSuite-Update-<previous>-to-<version>.zip, where
        <previous> is the highest release archived in Reference\AppReleases
        that is older than this version (never this version itself, never a
        prerelease): the files that changed, signed,
      * keeps this build's manifest as Reference\AppReleases\<version>\,
        replacing the one an earlier build of the same version left there.

    So building the same version again (files changed, version not raised
    yet) rebuilds its delta against the new build and replaces its archived
    manifest. With nothing older archived (0.4.8, the first release with an
    archived manifest) it only archives; deltas start with the next release.
    Upload the delta with the release; installed copies update with a chain
    of these instead of the full installer.

.PARAMETER DeltaFrom
    Override which releases to make delta packages from: versions archived in
    Reference\AppReleases (e.g. 0.4.8,0.4.9 -- a direct delta from an older
    release shortens chains), manifest files/folders, or 'none'. Default: the
    newest archived release older than this build. Skipped with -Unsigned.

.PARAMETER NoDelta
    Make no delta packages and leave Reference\AppReleases alone: a test
    build that is not going to be released, of a version already archived.

.PARAMETER Clean
    Delete build\, dist\ and the build environment before starting, so every
    step runs from scratch.

.PARAMETER Update
    Re-run the dependency install even though the requirement list has not
    changed, upgrading each package to the newest version its pin allows.

.PARAMETER FastInstaller
    Compress the installer with lzma2/fast instead of lzma2/max: a larger
    setup.exe, built much faster. For testing installer changes, not releases.

.PARAMETER HelpShots
    Regenerate the screenshots in the help pages from the real dialogs
    (tools\help_screenshots.py) before freezing, so the pictures match this
    build's interface. Recipes whose reference file is not in Reference\ are
    skipped. Off by default: run it when a dialog changed.

.PARAMETER RebuildInstallers
    Rebuild every requested installer even when its inputs have not changed.
    By default an installer is skipped when its payload folder, the packaging
    scripts and icons, and its settings (version, compression, module list)
    are all exactly what its existing setup.exe was built from.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Test
    .\build.ps1 -ListPython
    .\build.ps1 -Python 'C:\Program Files\Python313\python.exe'
    .\build.ps1 -OneFile -Clean
    .\build.ps1 -NoInstaller
    .\build.ps1 -Installers full,base,modules
    .\build.ps1 -Modules zf8hp -Installers modules     # just the ZF 8HP installer
    .\build.ps1 -BaseOnly -Installers base
    .\build.ps1 -Installers full,flasher,logger
    .\build.ps1 -Installers tools -FastInstaller
    .\build.ps1 -FastInstaller
    .\build.ps1 -Update
    .\build.ps1 -InnoSetup 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$ListPython,
    [switch]$NoUv,
    [switch]$OneFile,
    [switch]$Console,
    [switch]$Test,
    [switch]$SkipTests,
    [switch]$Clean,
    [string]$InnoSetup,
    [switch]$NoInstaller,
    [ValidateSet('full', 'main', 'base', 'modules', 'modules-checksum',
                 'flasher', 'sniffer', 'logger', 'tools')]
    [string[]]$Installers = @('full'),
    [string[]]$Modules = @('all'),
    [switch]$BaseOnly,
    [switch]$Unsigned,
    [switch]$CodeSign,
    [switch]$Update,
    [switch]$FastInstaller,
    [switch]$RebuildInstallers,
    [switch]$HelpShots,
    [string[]]$DeltaFrom = @(),
    [switch]$NoDelta
)
# 'main' is the old name of the full installer
$Installers = @($Installers | ForEach-Object { if ($_ -eq 'main') { 'full' } else { $_ } } |
                Select-Object -Unique)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# PowerShell 7.3+ turns native-command stderr into a terminating error when
# $ErrorActionPreference is 'Stop'. Probing interpreters writes to stderr all
# the time, so opt out - exit codes are checked explicitly below. Windows
# PowerShell 5.1 has no such switch; Invoke-Native below handles it there.
if (Test-Path Variable:\PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# PowerShell 7.3+ on Windows escapes embedded quotes when building a native
# command line. Ask for it explicitly so behaviour does not depend on the host.
if (Test-Path Variable:\PSNativeCommandArgumentPassing) {
    $PSNativeCommandArgumentPassing = 'Standard'
}

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvDir  = Join-Path $Root '.build-venv'
$Spec    = Join-Path $Root 'packaging\ovtsuite.spec'
$DistDir = Join-Path $Root 'dist'
$WorkDir = Join-Path $Root 'build'
$MinVersion = [version]'3.10'

# Every step reports how long the previous one took, so it is obvious where
# a slow build is spending its time.
$BuildClock = [System.Diagnostics.Stopwatch]::StartNew()
$script:StepStart = $null
function Write-StepTime {
    if ($null -ne $script:StepStart) {
        $secs = ($BuildClock.Elapsed - $script:StepStart).TotalSeconds
        Write-Host ("    ({0:N1} s)" -f $secs) -ForegroundColor DarkGray
    }
    $script:StepStart = $BuildClock.Elapsed
}
function Write-Step($msg) { Write-StepTime; Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg"   -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "    $msg"   -ForegroundColor Yellow }

function Clear-Tree([string]$Path) {
    <#
      Delete a build tree, robustly.

      A plain Remove-Item (or PyInstaller's own --clean) dies with WinError 5
      when anything holds a handle in the tree (a running OVTSuite.exe, an
      Explorer window, an antivirus scan, or a folder-sync engine) or leaves a
      file read-only - which is exactly the "Access is denied:
      build\ovtsuite\localpycs" failure. So: drop read-only, retry transient
      locks, and as a last resort move a stubborn tree aside so the build can
      still proceed.
    #>
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue |
        ForEach-Object { try { $_.Attributes = 'Normal' } catch {} }
    $cleared = $false
    for ($i = 1; $i -le 5 -and -not $cleared; $i++) {
        try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop; $cleared = $true }
        catch { Start-Sleep -Milliseconds (400 * $i) }
    }
    if (-not $cleared -and (Test-Path -LiteralPath $Path)) {
        $stale = "$Path.stale-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
        try {
            Move-Item -LiteralPath $Path -Destination $stale -ErrorAction Stop
            Write-Warn "$Path was locked; moved it aside to $stale - delete it whenever"
        } catch {
            throw ("$Path is locked by another process (Explorer window, antivirus, " +
                   'or folder sync). Close whatever has it open and re-run the build.')
        }
    }
}

# Dependency stamps. A stamp file inside each build environment lists every
# requirement string that has been installed into it; when the wanted list is
# already covered, pip/uv is not run at all (even a no-op 'pip install' goes to
# the package index, and 'pip install --upgrade pip' always does). The stamp
# lives INSIDE the env, so deleting or -Clean-ing the env invalidates it.
function Test-DepsCurrent([string]$Stamp, [string[]]$Wanted) {
    if ($Update) { return $false }
    if (-not (Test-Path -LiteralPath $Stamp)) { return $false }
    $have = @(Get-Content -LiteralPath $Stamp -ErrorAction SilentlyContinue)
    foreach ($w in $Wanted) { if ($have -notcontains $w) { return $false } }
    return $true
}
function Save-DepsStamp([string]$Stamp, [string[]]$Wanted) {
    $have = @()
    if (Test-Path -LiteralPath $Stamp) { $have = @(Get-Content -LiteralPath $Stamp) }
    $all = @(@($have) + @($Wanted) | Where-Object { $_ } | Sort-Object -Unique)
    Set-Content -LiteralPath $Stamp -Value $all -Encoding ASCII
}

# --------------------------------------------------------------------------
# Interpreter probing
# --------------------------------------------------------------------------

# The probe is written to a temp .py file rather than passed with -c.
#
# Windows PowerShell 5.1 (and PowerShell 7 in "Legacy" argument-passing mode)
# does not escape double quotes when it builds the command line for a native
# executable, so an argument like
#     import sys;print("%d" % sys.version_info[0])
# reaches python with its quotes stripped and dies with
#     File "<string>", line 1 ... SyntaxError
# A file path carries no quotes, so this works identically on 5.1 and 7.x.
$ProbeSource = @'
import struct
import sys

print("%d.%d.%d|%d|%s" % (sys.version_info[0], sys.version_info[1],
                          sys.version_info[2], struct.calcsize("P") * 8,
                          sys.executable))
'@

$ProbeFile = Join-Path ([System.IO.Path]::GetTempPath()) 'ovtsuite_pyprobe.py'
Set-Content -Path $ProbeFile -Value $ProbeSource -Encoding ASCII

function Invoke-Native {
    <#
      Run a native command and return @{Out; Code}, never throwing and never
      letting stderr become a terminating error.

      Windows PowerShell 5.1 has no $PSNativeCommandUseErrorActionPreference,
      and with $ErrorActionPreference = 'Stop' a native command that writes to
      stderr raises NativeCommandError. Probing interpreters writes to stderr
      constantly, so the preference is lowered for the duration of the call
      and stderr is discarded.
    #>
    param([string]$Exe, [string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        $out = & $Exe @Arguments 2>$null
        return @{ Out = $out; Code = $LASTEXITCODE }
    } catch {
        return @{ Out = $null; Code = -1; Error = $_.Exception.Message }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-NativeStreaming {
    <#
      Run a native command with its output going straight to the console, and
      return the exit code. Same stderr-under-'Stop' guard as Invoke-Native;
      pip, uv and PyInstaller all log to stderr in normal operation.

      Both streams are sent to the host explicitly. A bare `& $Exe` inside a
      function makes the tool's STDOUT part of the function's return value, so
      a caller got every output line plus the code: `$code -ne 0` was then true
      for any tool that prints to stdout (ISCC does), and the "failed (exit
      ...)" message swallowed the whole log while the real error scrolled away.
    #>
    param([string]$Exe, [string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-PythonExe {
    <#
      Returns [pscustomobject]@{Path; Version; Bits; Ok; Why} for a candidate
      python.exe, or $null if the path is obviously not one. Never throws.
    #>
    param([string]$Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        # Might still be a bare command name resolvable on PATH.
        $viaPath = @(Get-Command $Candidate -CommandType Application -ErrorAction SilentlyContinue) |
                   Select-Object -First 1
        if ($viaPath -and $viaPath.Path) {
            $Candidate = $viaPath.Path
        } else {
            return [pscustomobject]@{
                Path = $Candidate; Version = $null; Bits = 0
                Ok = $false; Why = 'no such file'
            }
        }
    }

    # Windows "App Execution Alias" stubs live under WindowsApps and are
    # zero-byte reparse points; running one opens the Microsoft Store.
    if ($Candidate -like '*\WindowsApps\*') {
        try {
            $item = Get-Item -LiteralPath $Candidate -ErrorAction Stop
            if ($item.Length -eq 0) {
                return [pscustomobject]@{
                    Path = $Candidate; Version = $null; Bits = 0
                    Ok = $false; Why = 'Microsoft Store alias stub (not a real interpreter)'
                }
            }
        } catch { }
    }

    $result = Invoke-Native $Candidate @($ProbeFile)
    $out = $result.Out
    $code = $result.Code

    if ($result.ContainsKey('Error')) {
        return [pscustomobject]@{
            Path = $Candidate; Version = $null; Bits = 0
            Ok = $false; Why = "could not run: $($result.Error)"
        }
    }
    if ($code -ne 0 -or -not $out) {
        return [pscustomobject]@{
            Path = $Candidate; Version = $null; Bits = 0
            Ok = $false; Why = "probe exited $code (not a working interpreter)"
        }
    }

    # $out may be an array (stderr lines interleaved); take the line that parses.
    $line = @($out) | Where-Object { $_ -is [string] -and $_ -match '^\d+\.\d+\.\d+\|\d+\|' } | Select-Object -Last 1
    if (-not $line) {
        return [pscustomobject]@{
            Path = $Candidate; Version = $null; Bits = 0
            Ok = $false; Why = 'unrecognised probe output'
        }
    }

    $parts = $line.Split('|')
    $ver   = [version]$parts[0]
    $bits  = [int]$parts[1]
    $real  = $parts[2]

    $ok  = $true
    $why = 'ok'
    if ($ver -lt $MinVersion) { $ok = $false; $why = "Python $ver is older than $MinVersion" }
    elseif ($bits -ne 64)     { $ok = $false; $why = "$bits-bit (PySide6 needs 64-bit)" }

    return [pscustomobject]@{
        Path = $real; Version = $ver; Bits = $bits; Ok = $ok; Why = $why
    }
}

function Get-PythonCandidates {
    <# Every path worth probing, best guesses first. #>
    $paths = [System.Collections.Generic.List[string]]::new()
    function Add-Candidate([string]$p) {
        if ([string]::IsNullOrWhiteSpace($p)) { return }
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return }
        $full = (Resolve-Path -LiteralPath $p).Path
        if (-not $paths.Contains($full)) { $paths.Add($full) }
    }

    # 2. environment override
    if ($env:OVTSUITE_PYTHON) { Add-Candidate $env:OVTSUITE_PYTHON }

    # 3. this project's own venv — already the right version, and the one the
    #    developer has been running the tests with.
    Add-Candidate (Join-Path $Root '.venv\Scripts\python.exe')

    # 4. uv, which is how this project's venv was created.
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        foreach ($spec in @('>=3.10', '3.13', '3.12', '3.11', '3.10')) {
            $r = Invoke-Native $uv.Source @('python', 'find', $spec)
            if ($r.Code -eq 0 -and $r.Out) {
                foreach ($f in @($r.Out)) { Add-Candidate ([string]$f).Trim() }
            }
        }
    }

    # 5. the py launcher — ask it where the interpreters actually are rather
    #    than shelling through 'py -3.x' (which fails opaquely when the
    #    launcher is present but the version is not registered).
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $r = Invoke-Native $py.Source @('--list-paths')
        if ($r.Code -eq 0 -and $r.Out) {
            foreach ($row in @($r.Out)) {
                $text = [string]$row
                # e.g. " -V:3.13 *        C:\Program Files\Python313\python.exe"
                $m = [regex]::Match($text, '(?<path>[A-Za-z]:\\.*python\.exe)\s*$')
                if ($m.Success) { Add-Candidate $m.Groups['path'].Value }
            }
        }
    }

    # 6. plain PATH lookups
    foreach ($name in @('python', 'python3')) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Path) { Add-Candidate $cmd.Path }
        }
    }

    # 6b. PEP 514 registry entries — every python.org / Store install writes
    #     HK(LM|CU)\Software\Python\PythonCore\<ver>[-32]\InstallPath, so this
    #     finds interpreters that are neither on PATH nor known to the py
    #     launcher (e.g. a 32-bit Python installed with both options unticked,
    #     which is all the J2534 bridge build needs).
    $regRoots = @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )
    foreach ($rr in $regRoots) {
        try {
            Get-ChildItem -Path $rr -ErrorAction SilentlyContinue | ForEach-Object {
                $ip = Get-ItemProperty -Path (Join-Path $_.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                if ($ip) {
                    $exe = if ($ip.ExecutablePath) { $ip.ExecutablePath } elseif ($ip.'(default)') { Join-Path $ip.'(default)' 'python.exe' } else { $null }
                    if ($exe) { Add-Candidate $exe }
                }
            }
        } catch { }
    }

    # 7. well-known install locations, newest first
    $roots = @(
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}",
        "$env:LOCALAPPDATA\Programs\Python",
        'C:\'
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    foreach ($r in $roots) {
        try {
            Get-ChildItem -LiteralPath $r -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Add-Candidate (Join-Path $_.FullName 'python.exe') }
        } catch { }
    }

    return $paths
}

function Find-SignTool {
    <#
      Locate signtool.exe (Windows SDK) for -CodeSign: $env:OVT_SIGNTOOL, PATH,
      then the newest x64 one under Windows Kits\10\bin. Returns $null if none.
    #>
    if ($env:OVT_SIGNTOOL -and (Test-Path -LiteralPath $env:OVT_SIGNTOOL -PathType Leaf)) {
        return $env:OVT_SIGNTOOL
    }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Path) { return $onPath.Path }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path -LiteralPath $kits) {
        $found = @(Get-ChildItem -LiteralPath $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending)
        if ($found.Count) { return $found[0].FullName }
    }
    return $null
}

function Find-InnoSetup {
    <#
      Locate ISCC.exe (the Inno Setup command-line compiler). Returns the path
      or $null; a missing Inno Setup is a skipped step, never a failed build.
    #>
    if ($InnoSetup) {
        if (Test-Path -LiteralPath $InnoSetup -PathType Leaf) { return $InnoSetup }
        Write-Warn "-InnoSetup path does not exist: $InnoSetup"
        return $null
    }
    if ($env:OVTSUITE_ISCC -and (Test-Path -LiteralPath $env:OVTSUITE_ISCC -PathType Leaf)) {
        return $env:OVTSUITE_ISCC
    }

    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Path) { return $onPath.Path }

    # The installer records where it put itself.
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 5_is1',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 5_is1'
    )
    foreach ($key in $keys) {
        try {
            $location = (Get-ItemProperty -Path $key -ErrorAction Stop).InstallLocation
        } catch { continue }
        if (-not $location) { continue }
        $candidate = Join-Path $location 'ISCC.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }

    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        foreach ($version in @('Inno Setup 6', 'Inno Setup 5')) {
            $candidate = Join-Path (Join-Path $root $version) 'ISCC.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}


function Resolve-Python {
    $results = [System.Collections.Generic.List[object]]::new()
    foreach ($c in Get-PythonCandidates) {
        $r = Test-PythonExe $c
        if ($r) { $results.Add($r) }
    }
    return $results
}

# Full discovery (uv, py launcher, PATH, registry, directory scans, and a probe
# run of every candidate) is only needed to CREATE an environment, so it runs
# lazily, at most once, and not at all when both environments already exist.
$script:FoundCache = $null
function Get-Found {
    if ($null -eq $script:FoundCache) { $script:FoundCache = @(Resolve-Python) }
    return $script:FoundCache
}

# --------------------------------------------------------------------------
# 0. Sanity
# --------------------------------------------------------------------------
if (-not (Test-Path $Spec)) {
    throw "Spec file not found: $Spec (run this script from the repo root)"
}

if ($Clean) {
    Write-Step 'Cleaning'
    foreach ($d in @($DistDir, $WorkDir, $EnvDir)) {
        if (Test-Path $d) { Clear-Tree $d; Write-Ok "removed $d" }
    }
}

# --------------------------------------------------------------------------
# 1. Locate Python
# --------------------------------------------------------------------------
Write-Step 'Locating Python'
$edition = if (Test-Path Variable:\PSEdition) { $PSEdition } else { 'Desktop' }
Write-Ok "PowerShell $($PSVersionTable.PSVersion) ($edition)"

$EnvPy = Join-Path $EnvDir 'Scripts\python.exe'
$found = @()

if ($ListPython) {
    $found = @(Get-Found)
    if (-not $found -or $found.Count -eq 0) {
        Write-Warn 'no candidates found at all'
    }
    foreach ($f in $found) {
        $mark = if ($f.Ok) { 'OK  ' } else { 'skip' }
        $ver  = if ($f.Version) { "$($f.Version) $($f.Bits)-bit" } else { '?' }
        $col  = if ($f.Ok) { 'Green' } else { 'DarkGray' }
        Write-Host ("    [{0}] {1,-22} {2}" -f $mark, $ver, $f.Path) -ForegroundColor $col
        if (-not $f.Ok) { Write-Host ("           {0}" -f $f.Why) -ForegroundColor DarkGray }
    }
    return
}

$PyExe = $null
$ReuseEnv = $false
if ($Python) {
    $explicit = Test-PythonExe $Python
    if (-not $explicit -or -not $explicit.Ok) {
        $why = if ($explicit) { $explicit.Why } else { 'not a usable path' }
        Write-Host "    -Python is not usable: $why" -ForegroundColor Red
        Write-Host "    $Python" -ForegroundColor Red
        throw '-Python is not a usable interpreter.'
    }
    $PyExe = $explicit.Path
    Write-Ok "using -Python: $PyExe (Python $($explicit.Version))"
} elseif (Test-Path $EnvPy) {
    # The build environment already exists: it is the interpreter everything
    # below runs, so one probe of it replaces the whole discovery pass.
    $envProbe = Test-PythonExe $EnvPy
    if ($envProbe -and $envProbe.Ok) {
        $ReuseEnv = $true
        Write-Ok "reusing build environment (Python $($envProbe.Version), $($envProbe.Bits)-bit); discovery skipped"
        Write-Ok $EnvDir
    } else {
        $why = if ($envProbe) { $envProbe.Why } else { 'not runnable' }
        Write-Warn "existing build environment is unusable ($why) - recreating it"
        Clear-Tree $EnvDir
    }
}

if (-not $PyExe -and -not $ReuseEnv) {
    $found = @(Get-Found)
    $usable = @($found | Where-Object { $_.Ok })
    if ($usable.Count -gt 0) {
        $best = $usable | Sort-Object Version -Descending | Select-Object -First 1
        $PyExe = $best.Path
        Write-Ok "found Python $($best.Version) ($($best.Bits)-bit)"
        Write-Ok $PyExe
    }
}

if (-not $PyExe -and -not $ReuseEnv) {
    # Print the diagnosis as readable lines. A multi-line 'throw' gets folded
    # into PowerShell's exception formatter and becomes unreadable.
    Write-Host ''
    Write-Host "    No usable 64-bit Python $MinVersion+ was found." -ForegroundColor Red
    Write-Host ''
    Write-Host '    Searched, in order:' -ForegroundColor DarkGray
    Write-Host '      -Python argument' -ForegroundColor DarkGray
    Write-Host '      $env:OVTSUITE_PYTHON' -ForegroundColor DarkGray
    Write-Host '      .venv\Scripts\python.exe' -ForegroundColor DarkGray
    Write-Host '      uv python find' -ForegroundColor DarkGray
    Write-Host '      py --list-paths' -ForegroundColor DarkGray
    Write-Host '      python / python3 on PATH' -ForegroundColor DarkGray
    Write-Host '      Program Files, LocalAppData\Programs\Python, C:\Python3*' -ForegroundColor DarkGray

    if ($found -and @($found).Count -gt 0) {
        Write-Host ''
        Write-Host '    Candidates found but rejected:' -ForegroundColor Yellow
        foreach ($f in $found) {
            $ver = if ($f.Version) { "$($f.Version) $($f.Bits)-bit" } else { 'unknown' }
            Write-Host ("      {0,-20} {1}" -f $ver, $f.Path) -ForegroundColor Yellow
            Write-Host ("      {0,-20} {1}" -f '', $f.Why) -ForegroundColor DarkGray
        }
    } else {
        Write-Host ''
        Write-Host '    No candidates were found at all.' -ForegroundColor Yellow
    }

    Write-Host ''
    Write-Host '    Fix it with any of:' -ForegroundColor Cyan
    Write-Host '      .\build.ps1 -ListPython'
    Write-Host "      .\build.ps1 -Python 'C:\Program Files\Python313\python.exe'"
    Write-Host "      `$env:OVTSUITE_PYTHON = 'C:\Program Files\Python313\python.exe'"
    Write-Host ''
    throw 'No usable Python interpreter - see the list above.'
}

# --------------------------------------------------------------------------
# 2. Build environment
# --------------------------------------------------------------------------
$uvCmd = if ($NoUv) { $null } else { Get-Command uv -ErrorAction SilentlyContinue }

if (-not (Test-Path $EnvPy)) {
    Write-Step 'Creating build environment'
    if ($uvCmd) {
        $code = Invoke-NativeStreaming $uvCmd.Source @('venv', '--python', $PyExe, $EnvDir)
    } else {
        $code = Invoke-NativeStreaming $PyExe @('-m', 'venv', $EnvDir)
    }
    if ($code -ne 0) { throw 'build environment creation failed' }
    Write-Ok $EnvDir
}
if (-not (Test-Path $EnvPy)) { throw "build environment is missing $EnvPy" }

Write-Step 'Checking dependencies'
# cryptography: module signatures (Ed25519) and the ZF 8HP Gen2 AES codec
$deps = @('PySide6>=6.6', 'numpy>=1.24', 'pyserial>=3.5', 'pyusb>=1.2', 'pyftdi>=0.55',
          'cryptography>=41', 'pyinstaller>=6.6')
if ($Test -and -not $SkipTests) { $deps += 'pytest' }
$DepsStamp = Join-Path $EnvDir '.ovtsuite-deps'
if (Test-DepsCurrent $DepsStamp $deps) {
    Write-Ok 'requirements unchanged since the last install - skipped (-Update to refresh)'
} else {
    $upgrade = if ($Update) { @('--upgrade') } else { @() }
    if ($uvCmd) {
        $code = Invoke-NativeStreaming $uvCmd.Source (@('pip', 'install', '--python', $EnvPy) + $upgrade + $deps)
    } else {
        Invoke-NativeStreaming $EnvPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet') | Out-Null
        $code = Invoke-NativeStreaming $EnvPy (@('-m', 'pip', 'install', '--quiet') + $upgrade + $deps)
    }
    if ($code -ne 0) { throw 'dependency installation failed' }
    Save-DepsStamp $DepsStamp $deps
}

$r = Invoke-Native $EnvPy @('-m', 'PyInstaller', '--version')
$PiVer = if ($r.Out) { ([string](@($r.Out) | Select-Object -Last 1)).Trim() } else { 'unknown' }
Write-Ok "PyInstaller $PiVer"

# --------------------------------------------------------------------------
# 3. Sync the Windows version resource with ovtsuite.__version__
# --------------------------------------------------------------------------
Write-Step 'Syncing version resource'
# Again via a file, not -c, so the quoting survives Windows PowerShell 5.1.
$VersionProbe = Join-Path ([System.IO.Path]::GetTempPath()) 'ovtsuite_version.py'
Set-Content -Path $VersionProbe -Encoding ASCII -Value @'
import sys

sys.path.insert(0, sys.argv[1])
import ovtsuite

print(ovtsuite.__version__)
'@
$r = Invoke-Native $EnvPy @($VersionProbe, $Root)
$AppVersion = if ($r.Out) { ([string](@($r.Out) | Select-Object -Last 1)).Trim() } else { '' }
Remove-Item $VersionProbe -ErrorAction SilentlyContinue
if ($r.Code -ne 0 -or $AppVersion -notmatch '^\d') {
    throw "could not read ovtsuite.__version__ (exit $($r.Code), got: '$AppVersion')"
}
Write-Ok "ovtsuite.__version__ = $AppVersion"

$vparts = [System.Collections.Generic.List[string]]::new()
foreach ($p in $AppVersion.Split('.')) { $vparts.Add(($p -replace '\D', '')) }
while ($vparts.Count -lt 4) { $vparts.Add('0') }
$vtuple  = '(' + (($vparts[0..3]) -join ', ') + ')'
$vstring = ($vparts[0..3]) -join '.'

$VerFile = Join-Path $Root 'packaging\version_info.txt'
$VerOrig = Get-Content -Raw $VerFile
$txt = $VerOrig
$txt = [regex]::Replace($txt, 'filevers=\([^)]*\)', "filevers=$vtuple")
$txt = [regex]::Replace($txt, 'prodvers=\([^)]*\)', "prodvers=$vtuple")
$txt = [regex]::Replace($txt, "StringStruct\('FileVersion', '[^']*'\)", "StringStruct('FileVersion', '$vstring')")
$txt = [regex]::Replace($txt, "StringStruct\('ProductVersion', '[^']*'\)", "StringStruct('ProductVersion', '$vstring')")
# Only write when something changed: the file is an input of PyInstaller's
# EXE step, so touching it every build would force that step to re-run.
if ($txt -ceq $VerOrig) {
    Write-Ok "version resource already $vstring"
} else {
    Set-Content -Path $VerFile -Value $txt -Encoding UTF8 -NoNewline
    Write-Ok "version resource set to $vstring"
}

# --------------------------------------------------------------------------
# 3b. Layout catalogue: every full EDC16 binary in Reference\Binaries is
#     examined so calibration-only reads can be tied to a known layout.
#     Reference\Binaries is not shipped; when absent the checked-in
#     ovtsuite\data\layouts.json.gz is used as is. --if-stale makes the
#     tool skip the (slow) scan when none of the binaries, the ovtsuite
#     modules it used, or the KB data has changed since the last run; its
#     dependency record lives in build\.cache, so -Clean forces a rescan.
# --------------------------------------------------------------------------
if (Test-Path (Join-Path $Root 'Reference\Binaries')) {
    Write-Step 'Layout catalogue (Reference\Binaries)'
    # The tool reports each binary on stdout, which Invoke-NativeStreaming
    # would return alongside the exit code; run it directly with the same
    # stderr guard and echo its output indented.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $EnvPy (Join-Path $Root 'tools\generate_layouts.py') --if-stale 2>&1 | ForEach-Object { Write-Ok "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw 'layout catalogue generation failed' }
} else {
    Write-Step 'Skipping layout catalogue (no Reference\Binaries; using the checked-in one)'
}

# --------------------------------------------------------------------------
# 3c. Help screenshots (-HelpShots): every picture in the help pages, made
#     from the real dialogs with this machine's look (docs\HELP.md).
# --------------------------------------------------------------------------
if ($HelpShots) {
    Write-Step 'Help screenshots'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $EnvPy (Join-Path $Root 'tools\help_screenshots.py') 2>&1 | ForEach-Object { Write-Ok "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { Write-Warn 'some help screenshots failed - the old pictures were kept' }
}

# --------------------------------------------------------------------------
# 3c. Module packages (only the stale ones are rebuilt -- see
#     tools\build_modules.py). The base app is staged here too; the spec
#     freezes that staged copy, never the source tree's package.
# --------------------------------------------------------------------------
$BuildModules = Join-Path $Root 'tools\build_modules.py'
$AppRelease   = Join-Path $Root 'tools\app_release.py'
$AppArchive   = Join-Path $Root 'Reference\AppReleases'
$ModulesOut   = Join-Path $DistDir 'modules'
$ModuleList   = ($Modules -join ',')
if ($ModuleList -eq '') { $ModuleList = 'all' }

function Invoke-AppRelease([string[]]$Arguments, [string]$What) {
    # tools\app_release.py (delta updates): streamed like build_modules.py
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $EnvPy $AppRelease @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            if ($line.StartsWith('WARNING: ')) { Write-Warn $line.Substring(9) } else { Write-Ok $line }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

function Write-AppManifest {
    # dist\OVTSuite\app-manifest.json: after everything that changes a shipped
    # file (the bridge copy, Authenticode), before the installers pack it.
    # Deterministic, so an unchanged build does not rebuild its installers.
    $a = @('manifest', (Join-Path $DistDir 'OVTSuite'), '--version', $AppVersion)
    if ($Unsigned) { $a += '--unsigned' }
    Invoke-AppRelease $a 'app manifest'
}

function Invoke-BuildModules([string[]]$Arguments, [string]$What) {
    # streamed and indented, with the same stderr guard as the layout step
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $EnvPy $BuildModules @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            if ($line.StartsWith('WARNING: ')) { Write-Warn $line.Substring(9) } else { Write-Ok $line }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

if ($BaseOnly) {
    Write-Step 'Skipping module packages (-BaseOnly)'
} else {
    if (-not $Unsigned) {
        Write-Step 'Module signing key'
        Invoke-BuildModules @('ensure-key') 'signing key setup'
    }
    Write-Step "Module packages ($ModuleList)"
    $pkArgs = @('packages', '--out', $ModulesOut, '--modules', $ModuleList)
    # Signed packages are releases: each new version is kept in
    # Reference\ModuleReleases, which tests\test_module_compat.py loads
    # against every newer app (backwards compatibility).
    if ($Unsigned) { $pkArgs += '--unsigned' }
    else { $pkArgs += @('--archive', (Join-Path $Root 'Reference\ModuleReleases')) }
    Invoke-BuildModules $pkArgs 'module packaging'
}

# --------------------------------------------------------------------------
# 4. Tests
# --------------------------------------------------------------------------
if ($Test -and -not $SkipTests) {
    Write-Step 'Running tests'
    $TestLog = Join-Path $Root 'build-tests.log'
    Push-Location $Root
    try {
        # Tee to a log as well as the console: when a build fails on someone
        # else's machine, "tests failed" on its own is not diagnosable.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        & $EnvPy -m pytest -rA --tb=long --color=no 2>&1 |
            Tee-Object -FilePath $TestLog
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous

        if ($code -ne 0) {
            Write-Host ''
            Write-Host '    Failing tests:' -ForegroundColor Red
            Select-String -Path $TestLog -Pattern '^(FAILED|ERROR) ' |
                ForEach-Object { Write-Host "      $($_.Line)" -ForegroundColor Red }
            Write-Host ''
            Write-Host "    Full output: $TestLog" -ForegroundColor Yellow
            Write-Host '    Re-run just the tests with:' -ForegroundColor DarkGray
            Write-Host "      & '$EnvPy' -m pytest -rA" -ForegroundColor DarkGray
            Write-Host ''
            throw 'tests failed - aborting build (drop -Test to build without them)'
        }
        Write-Ok "test log: $TestLog"
    } finally { Pop-Location }
} elseif ($SkipTests) {
    Write-Step 'Skipping tests (-SkipTests)'
} else {
    Write-Step 'Skipping tests (default; pass -Test to run them)'
}

# --------------------------------------------------------------------------
# 5. Build
# --------------------------------------------------------------------------
$layout = if ($OneFile) { 'one-file' } else { 'one-folder' }
$window = if ($Console) { 'console' } else { 'windowed' }
Write-Step "Building ($layout, $window)"

$env:OVTSUITE_ONEFILE = if ($OneFile) { '1' } else { '0' }
$env:OVTSUITE_CONSOLE = if ($Console) { '1' } else { '0' }

# build\ is KEPT between builds: it holds PyInstaller's analysis cache, which
# it re-validates itself (every module, binary and data file it collected is
# checked against its last build) and only redoes what changed. Only the
# dist\OVTSuite output folder is cleared beforehand, since COLLECT rebuilds it
# anyway and a locked file in there is the usual cause of a failed build.
$running = @(Get-Process OVTSuite -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    throw ('OVTSuite.exe is still running (PID ' +
           (($running | ForEach-Object Id) -join ', ') +
           ') - close it and re-run the build.')
}

# Switching layout (-OneFile), window mode (-Console), PyInstaller version or
# build interpreter discards the cache rather than trusting PyInstaller to
# reconcile two different kinds of build in one work folder.
$CacheDir  = Join-Path $WorkDir '.cache'
$ModeStamp = Join-Path $CacheDir 'pyinstaller-mode'
$MainWork  = Join-Path $WorkDir 'ovtsuite'
$BuildMode = "$layout|$window|PyInstaller $PiVer|$EnvPy"
$LastMode  = if (Test-Path $ModeStamp) { ([string](Get-Content -Raw $ModeStamp)).Trim() } else { '' }
if ((Test-Path $MainWork) -and $LastMode -ne $BuildMode) {
    Write-Ok 'build mode changed since the last build - discarding the PyInstaller cache'
    Clear-Tree $MainWork
} elseif (Test-Path $MainWork) {
    Write-Ok 'reusing the PyInstaller cache in build\ovtsuite (-Clean to discard it)'
}

# The base app is re-frozen only when something it is made from changed: its
# own files (not a module's), the packaging inputs, or the build mode. A change
# to a module alone rebuilds that module's package above and nothing here.
$FreezeStamp = Join-Path $CacheDir 'frozen-base'
$r = Invoke-Native $EnvPy @($BuildModules, 'freeze-key', '--mode', $BuildMode)
$FreezeKey = if ($r.Code -eq 0 -and $r.Out) { ([string](@($r.Out) | Select-Object -Last 1)).Trim() } else { '' }
$LastFreeze = if (Test-Path $FreezeStamp) { ([string](Get-Content -Raw $FreezeStamp)).Trim() } else { '' }
$ExeBuilt = if ($OneFile) { Join-Path $DistDir 'OVTSuite.exe' } else { Join-Path $DistDir 'OVTSuite\OVTSuite.exe' }
if ($FreezeKey -and $FreezeKey -eq $LastFreeze -and (Test-Path $ExeBuilt)) {
    Write-Ok 'base app unchanged since the last build - PyInstaller skipped'
} else {
    if (-not $OneFile) { Clear-Tree (Join-Path $DistDir 'OVTSuite') }
    Push-Location $Root
    try {
        # PyInstaller logs to stderr in normal operation. No --clean: build\ is
        # kept on purpose (see above), and --clean's rmtree of localpycs is the
        # fragile step that used to fail.
        $code = Invoke-NativeStreaming $EnvPy @(
            '-m', 'PyInstaller', $Spec, '--noconfirm',
            '--distpath', $DistDir, '--workpath', $WorkDir)
        if ($code -ne 0) { throw 'PyInstaller failed' }
        New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
        Set-Content -LiteralPath $ModeStamp -Value $BuildMode -Encoding UTF8
        if ($FreezeKey) { Set-Content -LiteralPath $FreezeStamp -Value $FreezeKey -Encoding UTF8 }
    } finally {
        Pop-Location
        Remove-Item Env:\OVTSUITE_ONEFILE -ErrorAction SilentlyContinue
        Remove-Item Env:\OVTSUITE_CONSOLE -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------------------------
# 5a. Tool launchers: OVTFlasher.exe, OVTSniffer.exe, OVTLogger.exe,
#     OVTSimulator.exe, OVTMcp.exe -- a few KB each, starting
#     "OVTSuite.exe --tool <name>" (packaging\launcher\Launcher.cs). Compiled
#     with the .NET Framework's own csc.exe only when their inputs change, and
#     the cached (signed, after -CodeSign) copies reused, so an unchanged
#     launcher keeps its bytes and stays out of the delta packages.
# --------------------------------------------------------------------------
$BuildLaunchers = Join-Path $Root 'tools\build_launchers.py'
$LauncherCache  = Join-Path $WorkDir 'launchers'
function Invoke-BuildLaunchers([string[]]$Arguments, [string]$What) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        & $EnvPy $BuildLaunchers @Arguments 2>&1 | ForEach-Object { Write-Ok "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}
if (-not $OneFile) {
    Write-Step 'Tool launchers'
    Invoke-BuildLaunchers @('build', '--into', (Join-Path $DistDir 'OVTSuite'),
                            '--cache', $LauncherCache) 'tool launchers'
}

# --------------------------------------------------------------------------
# 5b. 32-bit J2534 bridge (j2534bridge32.exe)
# --------------------------------------------------------------------------
# Most J2534 04.04 devices (Scanmatik 2 PRO, ...) ship only a 32-bit DLL, which
# the 64-bit app cannot load in-process. ovtsuite/flash/j2534_bridge.py runs
# the DLL in a 32-bit helper and proxies calls over pipes; freezing it here
# with a 32-bit Python (if one is installed) puts j2534bridge32.exe beside the
# app so users need nothing else. Without a 32-bit Python the app falls back
# to 'py -3-32' on the user's machine at run time. Never a failed build.
if (-not $OneFile) {
    $Env32      = Join-Path $Root '.build-venv32'
    $EnvPy32    = Join-Path $Env32 'Scripts\python.exe'
    $BridgeWork = Join-Path $WorkDir 'bridge32'
    $BridgeDist = Join-Path $BridgeWork 'dist'

    # An existing 32-bit env is used directly; discovery only runs to make one.
    $py32 = $null
    $env32Ok = $false
    if (Test-Path $EnvPy32) {
        $p = Test-PythonExe $EnvPy32
        if ($p -and $p.Bits -eq 32 -and $p.Version -and $p.Version -ge $MinVersion) {
            $py32 = $p
            $env32Ok = $true
        }
    }
    if (-not $py32) {
        $py32 = @(Get-Found | Where-Object { $_.Bits -eq 32 -and $_.Version -and $_.Version -ge $MinVersion }) |
                Sort-Object Version -Descending | Select-Object -First 1
    }

    if ($py32) {
        Write-Step "Building 32-bit J2534 bridge (Python $($py32.Version) 32-bit)"
        try {
            if (-not $env32Ok) {
                if (Test-Path $Env32) { Clear-Tree $Env32 }
                $r = Invoke-Native $py32.Path @('-m', 'venv', $Env32)
                if ($r.Code -ne 0) { throw "venv creation failed ($($r.Code))" }
            }
            $deps32  = @('pyinstaller>=6.6')
            $stamp32 = Join-Path $Env32 '.ovtsuite-deps'
            if (Test-DepsCurrent $stamp32 $deps32) {
                Write-Ok 'requirements unchanged - skipped'
            } else {
                $upgrade32 = if ($Update) { @('--upgrade') } else { @() }
                $r = Invoke-Native $EnvPy32 (@('-m', 'pip', 'install', '--quiet') + $upgrade32 + $deps32)
                if ($r.Code -ne 0) { throw "pip install failed ($($r.Code))" }
                Save-DepsStamp $stamp32 $deps32
            }
            # Built into build\bridge32 (kept between builds, like the main
            # app) rather than straight into dist\OVTSuite, which is cleared
            # every build: PyInstaller then finds nothing changed and skips the
            # rebuild, and the cached exe is simply copied into place.
            $bridgeSrc = Join-Path $Root 'ovtsuite\flash\j2534_bridge.py'
            $code = Invoke-NativeStreaming $EnvPy32 @(
                '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
                '--name', 'j2534bridge32',
                '--paths', (Join-Path $Root 'ovtsuite\flash'), '--paths', $Root,
                '--distpath', $BridgeDist,
                '--workpath', $BridgeWork,
                '--specpath', $BridgeWork,
                $bridgeSrc)
            if ($code -ne 0) { throw "PyInstaller (32-bit) failed ($code)" }
            $bridgeExe = Join-Path $BridgeDist 'j2534bridge32.exe'
            if (-not (Test-Path -LiteralPath $bridgeExe)) { throw "$bridgeExe is missing" }
            Copy-Item -LiteralPath $bridgeExe -Destination (Join-Path $DistDir 'OVTSuite') -Force
            Write-Ok (Join-Path $DistDir 'OVTSuite\j2534bridge32.exe')
        } catch {
            Write-Warn "32-bit bridge not built: $($_.Exception.Message)"
            Write-Warn "32-bit-only J2534 DLLs will need 'py -3-32' on the target machine"
        }
    } else {
        Write-Step 'Skipping 32-bit J2534 bridge (no 32-bit Python installed)'
        Write-Ok "install a 32-bit Python 3 (python.org, 'Windows installer (32-bit)') to bundle j2534bridge32.exe"
    }
}

# --------------------------------------------------------------------------
# 5c. Modules installed into the build: dist\OVTSuite\modules (so the built
#     app runs as the full app, and the full installer's components come from
#     there) and the checksum variants for the tool installers.
# --------------------------------------------------------------------------
$InstallerWork = Join-Path $WorkDir 'installer'
$ChecksumDir   = Join-Path $InstallerWork 'checksum-modules'
$AppModules    = Join-Path $DistDir 'OVTSuite\modules'
if (-not $OneFile) {
    if (Test-Path $AppModules) { Clear-Tree $AppModules }
    if (Test-Path $ChecksumDir) { Clear-Tree $ChecksumDir }
    if (-not $BaseOnly) {
        Write-Step 'Installing the modules into the build'
        Invoke-BuildModules @('install-into', $AppModules, '--packages', $ModulesOut,
                              '--modules', $ModuleList, '--variant', 'full') 'module install'
        Invoke-BuildModules @('install-into', $ChecksumDir, '--packages', $ModulesOut,
                              '--modules', $ModuleList, '--variant', 'checksum') 'module install'
    }
    Invoke-BuildModules @('inno', '--full-dir', $AppModules, '--checksum-dir', $ChecksumDir,
                          '--out', $InstallerWork) 'installer module lists'
}

# --------------------------------------------------------------------------
# 5d. App manifest (delta updates, docs\UPDATES.md)
# --------------------------------------------------------------------------
if (-not $OneFile) {
    Write-Step 'App manifest'
    Write-AppManifest
}

# --------------------------------------------------------------------------
# 6. Installer (Inno Setup)
# --------------------------------------------------------------------------
$Installer = $null
$InstallersBuilt = @()
if ($NoInstaller) {
    Write-Step 'Skipping installer (-NoInstaller)'
} elseif ($OneFile) {
    Write-Step 'Skipping installer'
    Write-Ok 'the installer packages the one-folder build; omit -OneFile for it'
} else {
    $Iscc = Find-InnoSetup
    if (-not $Iscc) {
        Write-Step 'Skipping installers'
        Write-Warn 'Inno Setup not found - skipping the installer.'
        Write-Ok 'Install it from https://jrsoftware.org/isdl.php, or pass'
        Write-Ok "  .\build.ps1 -InnoSetup 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'"
    } else {
        # lzma2/max over the whole ~120 MB folder is most of this step's time;
        # -FastInstaller trades installer size for speed while iterating.
        $Compression = if ($FastInstaller) { 'lzma2/fast' } else { 'lzma2/max' }
        $Built = Join-Path $DistDir 'OVTSuite'
        # the per-module installers get a folder of their own
        $ModuleInstallers = Join-Path $DistDir 'module-installers'
        $AllExes = @('OVTSuite.exe', 'OVTFlasher.exe', 'OVTSniffer.exe', 'OVTLogger.exe')
        $FullInc = Join-Path $InstallerWork 'modules_full.iss'
        $CksInc  = Join-Path $InstallerWork 'modules_checksum.iss'
        $ModRows = @()
        $ModJson = Join-Path $InstallerWork 'modules.json'
        # Windows PowerShell 5.1's ConvertFrom-Json emits a JSON array as ONE
        # object; wrapping that in @() made a one-element array holding every
        # row, so $m.id was every module id at once. Re-enumerating it through
        # the pipeline gives one row per element on 5.1 and 7 alike.
        if (Test-Path $ModJson) {
            $parsed = Get-Content -Raw $ModJson | ConvertFrom-Json
            $ModRows = @($parsed | ForEach-Object { $_ })
        }
        # An installer is rebuilt only when what it is made from changed: its
        # payload folder, the packaging scripts and icons, and its settings.
        # The key of the inputs it was last built from sits beside the build.
        $StampDir = Join-Path $InstallerWork 'stamps'
        New-Item -ItemType Directory -Force -Path $StampDir | Out-Null
        $PackagingFiles = @(Get-ChildItem -LiteralPath (Join-Path $Root 'packaging') -File |
            Where-Object { $_.Extension -in @('.iss', '.ico', '.bmp') } |
            Sort-Object Name | ForEach-Object { $_.FullName })
        # the licence texts the installers show (LicenseFile / InfoBeforeFile)
        $PackagingFiles += @((Join-Path $Root 'LICENSE'), (Join-Path $Root 'ovtsuite\modules\EULA.txt'))
        # Optional downloads the installers offer (addons\README.md): never in a
        # payload -- the installer fetches the file from its url and checks it
        # against the SHA-256 of the copy in addons\. No url, no offer.
        $AddonDefs = @()
        $AddonCfg = Join-Path $Root 'addons\addons.json'
        $BootDbFile = Join-Path $Root 'addons\edc16_bootdb.bin'
        if (Test-Path -LiteralPath $AddonCfg) {
            $bd = (Get-Content -Raw -LiteralPath $AddonCfg | ConvertFrom-Json).edc16_bootdb
            if ($bd -and $bd.url -and (Test-Path -LiteralPath $BootDbFile)) {
                $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BootDbFile).Hash.ToLower()
                $mb = [math]::Max(1, [math]::Ceiling((Get-Item -LiteralPath $BootDbFile).Length / 1MB))
                $AddonDefs = @("/DBootDbUrl=$($bd.url)", "/DBootDbSha256=$hash", "/DBootDbSizeMB=$mb")
            } elseif ($bd) {
                Write-Warn 'addons\addons.json has no url for edc16_bootdb - the installers will not offer its download'
            }
        }
        # FTDI's USB driver (packaging\addon_ftdi.iss): downloaded from FTDI, never
        # shipped. The hash is the one pinned in addons.json, else that of a copy
        # of the zip dropped into addons\ (git-ignored); with neither, no offer.
        $FtdiDefs = @()
        if (Test-Path -LiteralPath $AddonCfg) {
            $fd = (Get-Content -Raw -LiteralPath $AddonCfg | ConvertFrom-Json).ftdi_driver
            if ($fd -and $fd.url) {
                $fdHash = "$($fd.sha256)".ToLower()
                $fdMb = if ($fd.size_mb) { $fd.size_mb } else { 2 }
                $fdFile = if ($fd.file) { Join-Path $Root ('addons\' + $fd.file) } else { '' }
                if (-not $fdHash -and $fdFile -and (Test-Path -LiteralPath $fdFile)) {
                    $fdHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $fdFile).Hash.ToLower()
                    $fdMb = [math]::Max(1, [math]::Ceiling((Get-Item -LiteralPath $fdFile).Length / 1MB))
                }
                if ($fdHash) {
                    $FtdiDefs = @("/DFtdiUrl=$($fd.url)", "/DFtdiSha256=$fdHash",
                                  "/DFtdiVersion=$($fd.version)", "/DFtdiSizeMB=$fdMb")
                } else {
                    Write-Warn ('addons\addons.json has no sha256 for ftdi_driver and addons\' + $fd.file +
                                ' is not there - the installers will not offer FTDI''s driver')
                }
            }
        }
        # Authenticode (-CodeSign / OVT_CODESIGN_ARGS): the app's own exes are
        # signed in place before they are packed (an exe already validly signed
        # is left alone, so an unchanged build does not rebuild its installers),
        # and ISCC signs each setup.exe and its uninstaller through the SignTool
        # directive (#ifdef SignTool in the .iss files).
        $SignDefs = @()
        if ($CodeSign -or $env:OVT_CODESIGN_ARGS) {
            $SignTool = Find-SignTool
            if (-not $env:OVT_CODESIGN_ARGS) { throw '-CodeSign needs $env:OVT_CODESIGN_ARGS (see the help)' }
            if (-not $SignTool) { throw '-CodeSign: signtool.exe not found (install the Windows SDK or set OVT_SIGNTOOL)' }
            Write-Step 'Authenticode signing'
            $signArgs = @($env:OVT_CODESIGN_ARGS -split '\s+' | Where-Object { $_ })
            foreach ($exe in @(Get-ChildItem -LiteralPath $Built -Filter '*.exe' -File)) {
                if ((Get-AuthenticodeSignature -LiteralPath $exe.FullName).Status -eq 'Valid') {
                    Write-Ok "already signed: $($exe.Name)"
                    continue
                }
                $code = Invoke-NativeStreaming $SignTool (@('sign') + $signArgs + @($exe.FullName))
                if ($code -ne 0) { throw "signtool failed on $($exe.Name) (exit $code)" }
            }
            # the signed launchers become the cached ones: the next build
            # copies the same bytes, and they stay out of the next delta
            Invoke-BuildLaunchers @('adopt', '--from', $Built, '--cache', $LauncherCache) 'tool launchers'
            # the signatures changed the exes: the manifest must say so
            Write-AppManifest
            # ISCC: /S<name>=<command>, $q a quote, $f the file to sign
            $SignDefs = @('/DSignTool=ovtsign',
                          ('/Sovtsign=$q' + $SignTool + '$q sign ' + ($signArgs -join ' ') + ' $f'))
        }
        # every installer to build: script, setup name, launchers it needs, defines
        $Jobs = @()
        foreach ($which in $Installers) {
            switch ($which) {
                'full' {
                    $defs = @() + $AddonDefs + $FtdiDefs
                    if (-not $BaseOnly -and (Test-Path $FullInc)) { $defs += "/DModulesInclude=$FullInc" }
                    $Jobs += ,@('full', 'installer.iss', "OVTSuite-$AppVersion-setup", $AllExes, $defs, $Built, $DistDir)
                }
                'base' {
                    $Jobs += ,@('base', 'installer.iss', "OVTSuite-Base-$AppVersion-setup", $AllExes, (@() + $AddonDefs + $FtdiDefs), $Built, $DistDir)
                }
                { $_ -in @('modules', 'modules-checksum') } {
                    $variant = if ($which -eq 'modules') { 'full' } else { 'checksum' }
                    $rows = @($ModRows | Where-Object { $_.variant -eq $variant })
                    if (-not $rows) { Write-Warn "no $variant module packages in this build - $which skipped" }
                    foreach ($m in $rows) {
                        $nm = ($m.name -replace '[^A-Za-z0-9]', '')
                        $sfx = if ($variant -eq 'checksum') { '-Checksum' } else { '' }
                        $defs = @("/DModuleId=$($m.id)", "/DModuleName=$($m.name)",
                                  "/DModuleVersion=$($m.version)", "/DVariant=$variant")
                        # a closed-source module's licence agreement (modules\EULA.txt,
                        # put in its package by the build): the installer asks to accept it
                        $eula = Join-Path (Join-Path $m.dir $m.id) 'EULA.txt'
                        if (Test-Path -LiteralPath $eula) { $defs += "/DModuleEula=$((Resolve-Path -LiteralPath $eula).Path)" }
                        $Jobs += ,@("module $($m.id) $($m.version) ($variant)", 'module_installer.iss',
                                    "OVTSuite-$nm$sfx-$($m.version)-setup", @(), $defs, $m.dir,
                                    $ModuleInstallers)
                        # an older version's installer of the same module goes
                        if (Test-Path -LiteralPath $ModuleInstallers) {
                            Get-ChildItem -LiteralPath $ModuleInstallers -Filter "OVTSuite-$nm$sfx-*-setup.exe" |
                                Where-Object { $_.Name -ne "OVTSuite-$nm$sfx-$($m.version)-setup.exe" -and
                                               ($sfx -or $_.Name -notlike "OVTSuite-$nm-Checksum-*") } |
                                Remove-Item -Force
                        }
                    }
                }
                default {
                    # every launcher starts OVTSuite.exe: a tool installer ships it too
                    $exe = @{ flasher = @('OVTSuite.exe', 'OVTFlasher.exe');
                              sniffer = @('OVTSuite.exe', 'OVTSniffer.exe');
                              logger = @('OVTSuite.exe', 'OVTLogger.exe');
                              tools = @('OVTSuite.exe', 'OVTFlasher.exe', 'OVTSniffer.exe', 'OVTLogger.exe') }[$which]
                    $base = @{ flasher = 'OVTFlasher'; sniffer = 'OVTSniffer'; logger = 'OVTLogger';
                               tools = 'OVTTools' }[$which]
                    $defs = @("/DTool=$which") + $FtdiDefs
                    if ($which -in @('flasher', 'tools')) { $defs += $AddonDefs }
                    if (-not $BaseOnly -and (Test-Path $CksInc)) { $defs += "/DModulesInclude=$CksInc" }
                    $Jobs += ,@($which, 'tool_installer.iss', "$base-$AppVersion-setup", $exe, $defs, $Built, $DistDir)
                }
            }
        }
        foreach ($job in $Jobs) {
            $which, $iss, $SetupName, $needs, $defs, $source, $OutDir = $job
            New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
            Write-Step "Building installer: $which"
            Write-Ok $Iscc
            Write-Ok "compression: $Compression"
            $Iss = Join-Path $Root "packaging\$iss"
            if (-not (Test-Path $Iss)) { throw "installer script not found: $Iss" }
            # Name a missing input here rather than as an ISCC error halfway
            # through compression. A checkout reached through a cloud-sync drive
            # can lag behind the machine that wrote a new file.
            $gone = @($needs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Built $_)) })
            if ($gone.Count) {
                throw ("the build has no " + ($gone -join ', ') + " - the tool launchers come " +
                       "from tools\build_launchers.py (step 'Tool launchers'); rebuild without -OneFile")
            }
            # every literal Source file and every .ico either script names
            $inputs = @(Get-ChildItem -LiteralPath (Split-Path $Iss) -Filter '*.iss' |
                Select-String -Pattern '^Source:\s*"([^"{]+)"', '([A-Za-z0-9_.-]+\.ico)\b' -AllMatches |
                ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value } |
                Sort-Object -Unique)
            $missing = @($inputs | Where-Object {
                -not (Test-Path -LiteralPath (Join-Path (Split-Path $Iss) $_)) })
            if ($missing.Count) {
                throw ("installer input file(s) missing from packaging\: " + ($missing -join ', ') +
                       ". If this folder is a synced copy, let the sync finish and rebuild.")
            }

            $isccArgs = @(
                "/DAppVersion=$AppVersion",
                "/DSetupCompression=$Compression",
                "/DSourceDir=$source",
                "/DOutputDir=$OutDir",
                "/DOutputName=$SetupName") + $defs + $SignDefs

            $made = Join-Path $OutDir "$SetupName.exe"
            $stamp = Join-Path $StampDir "$SetupName.key"
            $keyArgs = @('installer-key', '--source', $source, '--text', ($isccArgs -join '|'),
                         '--text', $iss)
            foreach ($f in $PackagingFiles) { $keyArgs += @('--file', $f) }
            # an include file named by a define (the module component lists)
            foreach ($d in $defs) {
                if ($d -match '^/D[^=]+=(.+)$' -and (Test-Path -LiteralPath $Matches[1] -PathType Leaf)) {
                    $keyArgs += @('--file', $Matches[1])
                }
            }
            $r = Invoke-Native $EnvPy (@($BuildModules) + $keyArgs)
            $key = if ($r.Code -eq 0 -and $r.Out) { "$(@($r.Out)[-1])".Trim() } else { '' }
            $old = if (Test-Path -LiteralPath $stamp) { (Get-Content -Raw -LiteralPath $stamp).Trim() } else { '' }
            if ($key -and -not $RebuildInstallers -and $key -eq $old -and (Test-Path -LiteralPath $made)) {
                Write-Ok "up to date - its inputs have not changed ($SetupName.exe)"
                $InstallersBuilt += $made
                if (-not $Installer) { $Installer = $made }
                continue
            }
            # a failed or interrupted build must never leave a stamp that
            # vouches for an older setup.exe
            if (Test-Path -LiteralPath $stamp) { Remove-Item -LiteralPath $stamp -Force }

            $code = Invoke-NativeStreaming $Iscc ($isccArgs + @($Iss))
            if ($code -ne 0) { throw "Inno Setup failed (exit $code) - its error is the last lines above" }

            if (-not (Test-Path $made)) {
                throw "Inno Setup reported success but $made is missing"
            }
            if ($key) { Set-Content -LiteralPath $stamp -Value $key -Encoding ASCII }
            $InstallersBuilt += $made
            if (-not $Installer) { $Installer = $made }
        }
    }
}

# --------------------------------------------------------------------------
# 6a. Delta update packages: dist\OVTSuite-Update-<from>-to-<version>.zip, and
#     this build's manifest kept in Reference\AppReleases for the next one
# --------------------------------------------------------------------------
$DeltasBuilt = @()
if (-not $OneFile -and (Test-Path (Join-Path $DistDir 'OVTSuite\app-manifest.json'))) {
    if ($NoDelta) {
        Write-Step 'Skipping delta update packages (-NoDelta; Reference\AppReleases left alone)'
    } else {
        # tools\app_release.py auto: drops the earlier build's packages from
        # dist\, makes the delta from the newest archived release OLDER than
        # this version (a rebuild of this version never makes one from itself),
        # then archives this build's manifest, replacing that of an earlier
        # build of the same version. Unsigned: none made, nothing archived.
        $title = 'Delta update packages'
        if ($Unsigned) { $title += ' (none: -Unsigned, the app refuses unsigned ones)' }
        Write-Step $title
        $dArgs = @('auto', (Join-Path $DistDir 'OVTSuite'), '--out', $DistDir, '--archive', $AppArchive)
        foreach ($f in @($DeltaFrom | ForEach-Object { "$_" -split ',' } | Where-Object { "$_".Trim() })) {
            $dArgs += @('--from', $f.Trim())
        }
        if ($Unsigned) { $dArgs += '--unsigned' }
        Invoke-AppRelease $dArgs 'delta update packages'
        # this version's packages only (auto removed every earlier one)
        $DeltasBuilt = @(Get-ChildItem -LiteralPath $DistDir -Filter "OVTSuite-Update-*-to-$AppVersion.zip" -File |
                         ForEach-Object { $_.FullName })
    }
}

# --------------------------------------------------------------------------
# 6b. Release checksums: dist\SHA256SUMS.txt (+ .sig, the module signing key)
# --------------------------------------------------------------------------
# What the in-app update check verifies downloads against (ovtsuite\updates.py).
# Entries are merged by file name, so build_linux.sh can add its packages to the
# same list; upload SHA256SUMS.txt and SHA256SUMS.txt.sig with the release.
$SumFiles = @($InstallersBuilt) + @($DeltasBuilt)
if (-not $BaseOnly -and (Test-Path $ModulesOut)) {
    $SumFiles += @(Get-ChildItem -LiteralPath $ModulesOut -Filter '*.ovtmod' | ForEach-Object { $_.FullName })
}
if ($SumFiles.Count) {
    Write-Step 'Release checksums'
    $sumArgs = @('release-sums', '--out', (Join-Path $DistDir 'SHA256SUMS.txt'))
    if ($Unsigned) { $sumArgs += '--unsigned' }
    Invoke-BuildModules ($sumArgs + $SumFiles) 'release checksums'
}

# --------------------------------------------------------------------------
# 6. Report
# --------------------------------------------------------------------------
$Target = if ($OneFile) {
    Join-Path $DistDir 'OVTSuite.exe'
} else {
    Join-Path $DistDir 'OVTSuite\OVTSuite.exe'
}
if (-not (Test-Path $Target)) {
    throw "Build reported success but $Target is missing"
}

$size = (Get-Item $Target).Length / 1MB
Write-Step 'Done'
Write-Host ("    total build time {0:N1} s" -f $BuildClock.Elapsed.TotalSeconds) -ForegroundColor Green
Write-Host ("    {0}  ({1:N1} MB)" -f $Target, $size) -ForegroundColor Green
if (-not $OneFile) {
    $total = (Get-ChildItem (Join-Path $DistDir 'OVTSuite') -Recurse -File |
              Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host ("    folder total: {0:N1} MB" -f $total) -ForegroundColor Green
}
if (-not $BaseOnly -and (Test-Path $ModulesOut)) {
    foreach ($pk in @(Get-ChildItem -LiteralPath $ModulesOut -Filter '*.ovtmod' | Sort-Object Name)) {
        Write-Host ("    {0}  ({1:N1} MB)" -f $pk.FullName, ($pk.Length / 1MB)) -ForegroundColor Green
    }
}
foreach ($made in @($InstallersBuilt) + @($DeltasBuilt)) {
    $isize = (Get-Item $made).Length / 1MB
    Write-Host ("    {0}  ({1:N1} MB)" -f $made, $isize) -ForegroundColor Green
}
Write-Host "`n    Smoke test:  & '$Target'" -ForegroundColor DarkGray
foreach ($made in $InstallersBuilt) {
    Write-Host "    Installer:   & '$made'" -ForegroundColor DarkGray
}
