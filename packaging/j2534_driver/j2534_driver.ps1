# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
<#
.SYNOPSIS
  OVT-Suite J2534 USB driver: binds Microsoft's own WinUSB driver to a Tactrix
  OpenPort 2.0 (USB 0403:CC4D) or a Scanmatik SM2 (USB 20A2:0001) so OVT-Suite can
  talk to it directly through libusb.

.DESCRIPTION
  An OpenPort with no Tactrix driver installed is given Windows' generic serial
  driver (usbser, a COM port). This script does what Device Manager's "Update
  driver > Let me pick from a list > WinUSB Device" does, for each such cable that
  is plugged in: it selects winusb.inf -- the copy that ships with Windows, signed
  by Microsoft -- for that device. No driver file is added to the computer, no
  certificate is installed, nothing unsigned is loaded.

  A Scanmatik SM2 with no Scanmatik driver installed has no driver at all (Windows
  lists an unknown device that failed to install); it gets the same binding. Tactrix's
  driver is checked for the OpenPort only.

  The binding belongs to one device (its serial number), so a different cable needs
  this run again; the Start menu entry "Install J2534 USB driver" does it.

  -Action status    list the OpenPorts / SM2s and what each is bound to (no admin needed)
  -Action install   bind WinUSB to every plugged-in OpenPort or SM2 that is on the generic
                    serial driver or has none (administrator)
                    -Replace also OVERRIDES a cable that is on another driver -- Tactrix's
                    for an OpenPort, Scanmatik's (SmUsb) for an SM2, Zadig's, anything. That
                    driver stays installed, and what it was is recorded (HKLM\SOFTWARE\
                    OVT-Suite\J2534Driver\Replaced) so that `remove` can check it came back
  -Action remove    RESTORE: remove every OpenPort / SM2 whose driver is this binding, so
                    Windows gives it back its previous driver -- Tactrix's / Scanmatik's if
                    installed, else the serial driver or none -- then wait for it and say
                    whether the recorded driver really is back (administrator)
  -Action manage    the Start menu dialog: install, override another driver, or restore;
                    it looks for a plugged-in cable by itself (asks for administrator
                    rights itself)

  -Only openport|sm2  act on that kind of device only (default all)
  -Id <instance id>   act on that one device only (the DEVICE line's second field)
  -Out <file>         also write the lines below to this file (the installer reads it).

  Lines written (one per line, "|" separated):
    DEVICE|<instance id>|<1 plugged in, 0 not>|<state>|<service>|<driver inf>
         state: unbound (generic serial driver or none), winusb (this binding),
                other (another driver, e.g. Tactrix's, Scanmatik's or Zadig's: left
                alone unless -Replace)
    TACTRIX|1        a Tactrix J2534 registration exists (their driver is installed)
    SCANMATIK|1      a Scanmatik J2534 registration exists (their driver is installed)
    REPLACED|<id>|<service>|<inf>   status: this device was overridden from that driver
    J2534UNREG|<kind>|<n>           install -Replace: the maker's J2534 entry was set aside
    J2534REG|<kind>|<n>             remove: ... and put back
    OWNUNREG|<kind>|<n>             remove: our own J2534 entry for that cable was removed
    WARN|<kind>|<why>               the J2534 entry could not be set aside / put back
    BOUND|<id>  FAILED|<id>|<why>   install results
    REMOVED|<id>                    remove results: device removed and rescanned
    RESTORED|<id>|<service>|<inf>   ... and it is back on the driver it was overridden from
                                    (or, for a cable that had none, on none / the serial one)
    NOTRESTORED|<id>|<expected>|<got>  ... but it came back on something else (or not at all)
    NONE|            nothing plugged in

  Exit codes:  0 done (or nothing to do)   2 no OpenPort or SM2 plugged in
               3 needs administrator       4 a binding or a restore failed
               5 Tactrix's driver is installed (OpenPort skipped)
#>
param(
    [ValidateSet('status', 'install', 'remove', 'manage')][string]$Action = 'status',
    [switch]$Replace,
    [ValidateSet('all', 'openport', 'sm2')][string]$Only = 'all',
    [string]$Id = '',
    [string]$Out = ''
)
$ErrorActionPreference = 'Stop'
$script:Quiet = $false
$script:Results = @()

# The USB devices OVT-Suite drives directly through libusb. Kind 'serial' devices are given
# Windows' generic serial driver when nothing else claims them (and are put back in that class
# if a binding fails); Tactrix's own driver only matters for the OpenPort.
$Devices = @(
    [pscustomobject]@{ Kind = 'openport'; Name = 'OpenPort 2.0';  VidPid = 'VID_0403&PID_CC4D'; Serial = $true;  Tactrix = $true  },
    [pscustomobject]@{ Kind = 'sm2';      Name = 'Scanmatik SM2'; VidPid = 'VID_20A2&PID_0001'; Serial = $false; Tactrix = $false }
)
$ClassRoot = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class'
# What each overridden device was on, so a restore can be checked (one value per instance id,
# "service|inf|class"; '/' stands in for the backslashes of the id).
$ReplacedKey = 'HKLM:\SOFTWARE\OVT-Suite\J2534Driver\Replaced'
$WinUsbInf = Join-Path $env:windir 'INF\winusb.inf'

if ($Out) { Set-Content -LiteralPath $Out -Value '' -Encoding ASCII }
function Emit([string]$line) {
    $script:Results += $line
    if (-not $script:Quiet) { Write-Output $line }
    if ($Out) { Add-Content -LiteralPath $Out -Value $line -Encoding ASCII }
}
function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---- what is on the bus ------------------------------------------------------
# Every known device (see $Devices) that Windows has an enumeration entry for.
function Get-DeviceList {
    $list = @()
    foreach ($dev in $Devices) {
        $VidPid   = $dev.VidPid
        $EnumRoot = "HKLM:\SYSTEM\CurrentControlSet\Enum\USB\$VidPid"
        $present = @{}
        $ids = $null
        try { Initialize-Binder; $ids = @([OpDrv]::Present("USB\$VidPid\")) } catch { $ids = $null }   # fast: SetupAPI
        if ($null -eq $ids) {
            $ids = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
                     Where-Object { $_.InstanceId -like "USB\$VidPid\*" } | ForEach-Object { $_.InstanceId })
        }
        foreach ($i in $ids) { $present[$i.ToUpper()] = $true }
        if (-not (Test-Path -LiteralPath $EnumRoot)) { continue }
        foreach ($k in @(Get-ChildItem -LiteralPath $EnumRoot)) {
            $id  = "USB\$VidPid\" + $k.PSChildName
            $p   = Get-ItemProperty -LiteralPath $k.PSPath
            $svc = "$($p.Service)"
            $inf = ''
            if ($p.PSObject.Properties['Driver'] -and $p.Driver) {
                $ck = Join-Path $ClassRoot $p.Driver
                if (Test-Path -LiteralPath $ck) { $inf = "$((Get-ItemProperty -LiteralPath $ck).InfPath)" }
            }
            $state = 'other'
            # Any WinUSB binding counts: the one this engine makes (Windows' own winusb.inf) and
            # an earlier OVT-Suite / libwdi / Zadig package (oemNN.inf) are the same to libusb.
            if ($svc -ieq 'WINUSB') { $state = 'winusb' }
            elseif (-not $svc -or ($svc -ieq 'usbser' -and $inf -ieq 'usbser.inf')) { $state = 'unbound' }
            $cls = ''
            if ($p.PSObject.Properties['ClassGUID'] -and $p.ClassGUID) { $cls = "$($p.ClassGUID)" }
            $list += [pscustomobject]@{ Id = $id; Kind = $dev.Kind; Name = $dev.Name; Serial = $dev.Serial;
                                        Tactrix = $dev.Tactrix; Present = [bool]$present[$id.ToUpper()];
                                        State = $state; Service = $svc; Inf = $inf; Class = $cls }
        }
    }
    return $list
}

# -Only / -Id narrow what an action touches.
function Select-Targets($list) {
    $l = @($list)
    if ($Only -ne 'all') { $l = @($l | Where-Object { $_.Kind -eq $Only }) }
    if ($Id) { $l = @($l | Where-Object { $_.Id -ieq $Id }) }
    return $l
}

# A maker's own J2534 driver registers itself here (64- and 32-bit views).
function Test-J2534Vendor([string]$pattern) {
    foreach ($root in 'HKLM:\SOFTWARE\PassThruSupport.04.04', 'HKLM:\SOFTWARE\WOW6432Node\PassThruSupport.04.04') {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($k in @(Get-ChildItem -LiteralPath $root)) {
            $n = "$($k.PSChildName) $((Get-ItemProperty -LiteralPath $k.PSPath).Name)"
            if ($n -match $pattern) { return $true }
        }
    }
    return $false
}
function Test-Tactrix   { Test-J2534Vendor 'Tactrix|OpenPort' }
function Test-Scanmatik { Test-J2534Vendor 'Scanmatik|SM2' }

function Get-Replaced {
    $h = @{}
    if (Test-Path -LiteralPath $ReplacedKey) {
        $k = Get-Item -LiteralPath $ReplacedKey
        foreach ($n in $k.GetValueNames()) { $h[$n.Replace('/', '\').ToUpper()] = "$($k.GetValue($n))" }
    }
    return $h
}
function Set-Replaced([string]$id, [string]$svc, [string]$inf, [string]$cls) {
    if (-not (Test-Path -LiteralPath $ReplacedKey)) { [void](New-Item -Path $ReplacedKey -Force) }
    Set-ItemProperty -LiteralPath $ReplacedKey -Name $id.Replace('\', '/') -Value "$svc|$inf|$cls"
}
function Clear-Replaced([string]$id) {
    if (Test-Path -LiteralPath $ReplacedKey) {
        Remove-ItemProperty -LiteralPath $ReplacedKey -Name $id.Replace('\', '/') -ErrorAction SilentlyContinue
    }
}

# ---- the maker's J2534 registrations --------------------------------------------
# A cable that is on this driver is no longer on its maker's, so the maker's J2534 entry (the
# one other programs list: Tactrix's OpenPort 2.0, Scanmatik's SM2) would show a device that
# cannot be opened. An override sets that entry aside -- copied to our own key, then removed from
# PassThruSupport -- and a restore puts it back exactly as it was. Both registry views are
# handled explicitly (a 32-bit PowerShell would otherwise only see its own).
$SavedRoot = 'SOFTWARE\OVT-Suite\J2534Driver\Saved'
$PassThru = 'SOFTWARE\PassThruSupport.04.04'

function Open-Hklm([string]$view) {
    [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]$view)
}
function Copy-RegTree($src, $dst) {
    foreach ($n in $src.GetValueNames()) {
        $dst.SetValue($n, $src.GetValue($n, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames),
                      $src.GetValueKind($n))
    }
    foreach ($sub in $src.GetSubKeyNames()) {
        $c = $src.OpenSubKey($sub); $d = $dst.CreateSubKey($sub)
        try { Copy-RegTree $c $d } finally { $c.Close(); $d.Close() }
    }
}
# Is this PassThruSupport entry the maker's J2534 driver for that kind of cable? (Ours, vendor
# OVT-Suite, never is; neither is a Scanmatik SM3.)
function Test-MakerEntry([string]$kind, [string]$sub, $key) {
    $vendor = "$($key.GetValue('Vendor'))"; $name = "$($key.GetValue('Name'))"
    if ($vendor -ieq 'OVT-Suite') { return $false }
    if ($kind -eq 'openport') { return ("$vendor $name $sub" -match 'Tactrix|OpenPort') }
    return (($vendor -match 'Scanmatik') -and ("$name $sub" -match 'SM2'))
}
function Save-MakerRegistration([string]$kind) {
    $n = 0
    $store = Open-Hklm 'Registry64'
    try {
        foreach ($view in 'Registry64', 'Registry32') {
            $hk = Open-Hklm $view
            try {
                $root = $hk.OpenSubKey($PassThru, $true)
                if (-not $root) { continue }
                try {
                    foreach ($sub in @($root.GetSubKeyNames())) {
                        $k = $root.OpenSubKey($sub)
                        $hit = Test-MakerEntry $kind $sub $k
                        if ($hit) {
                            $d = $store.CreateSubKey("$SavedRoot\$kind\$view\$sub")
                            try { Copy-RegTree $k $d } finally { $d.Close() }
                        }
                        $k.Close()
                        if ($hit) { $root.DeleteSubKeyTree($sub); $n++ }
                    }
                } finally { $root.Close() }
            } finally { $hk.Close() }
        }
    } finally { $store.Close() }
    if ($n) { Emit "J2534UNREG|$kind|$n" }
}
# Our own J2534 entry for that kind of cable (the native DLL for other programs, ovtj2534_*.dll;
# vendor OVT-Suite). Once the cable is back on its maker's driver it cannot be opened through
# WinUSB, so the entry goes; Register J2534 drivers lists it again after a new override. The
# OVT Interface shim shares the vendor but is another DLL and is never touched.
function Test-OwnEntry([string]$kind, $key) {
    if ("$($key.GetValue('Vendor'))" -ine 'OVT-Suite') { return $false }
    $dll = [IO.Path]::GetFileName("$($key.GetValue('FunctionLibrary'))")
    if ($dll -notlike 'ovtj2534_*') { return $false }
    $name = "$($key.GetValue('Name'))"
    if ($kind -eq 'openport') { return ($name -match 'OpenPort') }
    return ($name -match 'SM2')
}
function Remove-OwnRegistration([string]$kind) {
    $n = 0
    foreach ($view in 'Registry64', 'Registry32') {
        $hk = Open-Hklm $view
        try {
            $root = $hk.OpenSubKey($PassThru, $true)
            if (-not $root) { continue }
            try {
                foreach ($sub in @($root.GetSubKeyNames())) {
                    $k = $root.OpenSubKey($sub)
                    $hit = Test-OwnEntry $kind $k
                    $k.Close()
                    if ($hit) { $root.DeleteSubKeyTree($sub); $n++ }
                }
            } finally { $root.Close() }
        } finally { $hk.Close() }
    }
    if ($n) { Emit "OWNUNREG|$kind|$n" }
}
function Restore-MakerRegistration([string]$kind) {
    $n = 0
    $store = Open-Hklm 'Registry64'
    try {
        $top = $store.OpenSubKey("$SavedRoot\$kind", $true)
        if (-not $top) { return }
        try {
            foreach ($view in @($top.GetSubKeyNames())) {
                $vk = $top.OpenSubKey($view)
                $hk = Open-Hklm $view
                try {
                    foreach ($sub in @($vk.GetSubKeyNames())) {
                        $src = $vk.OpenSubKey($sub)
                        try {
                            # the maker's own setup may have put it back already: leave that one
                            $exists = $hk.OpenSubKey("$PassThru\$sub")
                            if ($exists) { $exists.Close() }
                            else {
                                $dst = $hk.CreateSubKey("$PassThru\$sub")
                                try { Copy-RegTree $src $dst } finally { $dst.Close() }
                                $n++
                            }
                        } finally { $src.Close() }
                    }
                } finally { $hk.Close(); $vk.Close() }
            }
        } finally { $top.Close() }
        $store.DeleteSubKeyTree("$SavedRoot\$kind", $false)
    } finally { $store.Close() }
    if ($n) { Emit "J2534REG|$kind|$n" }
}

# ---- the binding (SetupAPI: what Device Manager's "Let me pick" does) ---------
function Initialize-Binder {
    if ('OpDrv' -as [type]) { return }
    $src = @'
using System; using System.ComponentModel; using System.Runtime.InteropServices; using System.Text;
public static class OpDrv {
  [StructLayout(LayoutKind.Sequential)] public struct DEVINFO { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct INSTPARAMS {
    public int cbSize; public int Flags; public int FlagsEx; public IntPtr hwndParent; public IntPtr MsgHandler;
    public IntPtr MsgContext; public IntPtr FileQueue; public UIntPtr ClassInstallReserved; public int Reserved;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string DriverPath; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DRVINFO {
    public int cbSize; public int DriverType; public UIntPtr Reserved;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string Description;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string MfgName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string ProviderName;
    public long DriverDate; public ulong DriverVersion; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DETAIL {
    public int cbSize; public int InfDateLow; public int InfDateHigh; public int CompatIDsOffset; public int CompatIDsLength;
    public UIntPtr Reserved;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string SectionName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string InfFileName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string DrvDescription;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=1)] public string HardwareID; }

  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr SetupDiGetClassDevsW(IntPtr g, string e, IntPtr h, uint f);
  [DllImport("setupapi.dll", SetLastError=true)] static extern bool SetupDiEnumDeviceInfo(IntPtr s, uint i, ref DEVINFO d);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiGetDeviceInstanceIdW(IntPtr s, ref DEVINFO d, StringBuilder id, int n, out int req);
  [DllImport("setupapi.dll", SetLastError=true)] static extern bool SetupDiSetSelectedDevice(IntPtr s, ref DEVINFO d);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiGetDeviceInstallParamsW(IntPtr s, ref DEVINFO d, ref INSTPARAMS p);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiSetDeviceInstallParamsW(IntPtr s, ref DEVINFO d, ref INSTPARAMS p);
  [DllImport("setupapi.dll", SetLastError=true)] static extern bool SetupDiBuildDriverInfoList(IntPtr s, ref DEVINFO d, int t);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiEnumDriverInfoW(IntPtr s, ref DEVINFO d, int t, int i, ref DRVINFO v);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiGetDriverInfoDetailW(IntPtr s, ref DEVINFO d, ref DRVINFO v, IntPtr detail, int size, out int req);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiSetSelectedDriverW(IntPtr s, ref DEVINFO d, ref DRVINFO v);
  [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr s, ref DEVINFO d, int prop, byte[] buf, int size);
  [DllImport("setupapi.dll", SetLastError=true)] static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
  [DllImport("newdev.dll", SetLastError=true)] static extern bool DiInstallDevice(IntPtr h, IntPtr s, ref DEVINFO d, ref DRVINFO v, int flags, out bool reboot);

  const string USBDEVICE = "{88BAE032-5A81-49f0-BC3D-A4FF138216D6}";   // WinUSB's setup class
  const string PORTS     = "{4D36E978-E325-11CE-BFC1-08002BE10318}";   // the serial driver's

  static void Fail(string what) { throw new Win32Exception(Marshal.GetLastWin32Error(), what); }

  // true when this driver node is the INF's "WinUSB Device" model (found by its hardware
  // id, not its description, which Windows translates)
  static bool IsWinUsbNode(IntPtr set, ref DEVINFO d, ref DRVINFO v) {
    const int N = 8192;
    IntPtr buf = Marshal.AllocHGlobal(N);
    try {
      Marshal.Copy(new byte[N], 0, buf, N);
      Marshal.WriteInt32(buf, 0, Marshal.SizeOf(typeof(DETAIL)));
      int req;
      if (!SetupDiGetDriverInfoDetailW(set, ref d, ref v, buf, N, out req)) {
        return v.Description.IndexOf("winusb", StringComparison.OrdinalIgnoreCase) >= 0;
      }
      int off = (int)Marshal.OffsetOf(typeof(DETAIL), "HardwareID");
      string multi = Marshal.PtrToStringUni(IntPtr.Add(buf, off), (N - off) / 2);
      foreach (string s in multi.Split('\0'))
        if (s.Equals("USB\\MS_COMP_WINUSB", StringComparison.OrdinalIgnoreCase)) return true;
      return false;
    } finally { Marshal.FreeHGlobal(buf); }
  }

  // forceClass != null: only write the device node's class GUID. Otherwise build the INF's
  // driver list for the device and install the WinUSB node: "ok", or "nolist" when the list
  // is empty (the node's class does not match the INF's yet).
  static string Attempt(string instanceId, string inf, string forceClass) {
    IntPtr set = SetupDiGetClassDevsW(IntPtr.Zero, "USB", IntPtr.Zero, 2 | 4);   // present, all classes
    if (set == new IntPtr(-1)) Fail("SetupDiGetClassDevs");
    try {
      var d = new DEVINFO(); d.cbSize = Marshal.SizeOf(typeof(DEVINFO));
      bool found = false;
      for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref d); i++) {
        var id = new StringBuilder(512); int req;
        if (SetupDiGetDeviceInstanceIdW(set, ref d, id, id.Capacity, out req) &&
            id.ToString().Equals(instanceId, StringComparison.OrdinalIgnoreCase)) { found = true; break; }
      }
      if (!found) throw new Exception("the device is no longer plugged in");
      if (forceClass != null) {
        byte[] g = Encoding.Unicode.GetBytes(forceClass + "\0");
        if (!SetupDiSetDeviceRegistryPropertyW(set, ref d, 0x8, g, g.Length)) Fail("set the device class");   // SPDRP_CLASSGUID
        return "classset";
      }
      if (!SetupDiSetSelectedDevice(set, ref d)) Fail("SetupDiSetSelectedDevice");
      var p = new INSTPARAMS(); p.cbSize = Marshal.SizeOf(typeof(INSTPARAMS));
      if (!SetupDiGetDeviceInstallParamsW(set, ref d, ref p)) Fail("SetupDiGetDeviceInstallParams");
      p.Flags |= 0x00010000;                 // DI_ENUMSINGLEINF: only this INF
      p.FlagsEx |= 0x00000800;               // DI_FLAGSEX_ALLOWEXCLUDEDDRVS
      p.DriverPath = inf;
      if (!SetupDiSetDeviceInstallParamsW(set, ref d, ref p)) Fail("SetupDiSetDeviceInstallParams");
      if (!SetupDiBuildDriverInfoList(set, ref d, 1)) Fail("SetupDiBuildDriverInfoList");   // SPDIT_CLASSDRIVER
      var v = new DRVINFO(); v.cbSize = Marshal.SizeOf(typeof(DRVINFO));
      bool have = false;
      for (int i = 0; SetupDiEnumDriverInfoW(set, ref d, 1, i, ref v); i++)
        if (IsWinUsbNode(set, ref d, ref v)) { have = true; break; }
      if (!have) return "nolist";
      if (!SetupDiSetSelectedDriverW(set, ref d, ref v)) Fail("SetupDiSetSelectedDriver");
      bool reboot;
      if (!DiInstallDevice(IntPtr.Zero, set, ref d, ref v, 0, out reboot)) Fail("DiInstallDevice");
      return "ok";
    } finally { SetupDiDestroyDeviceInfoList(set); }
  }

  // instance ids of the USB devices that are plugged in now and start with prefix
  public static string[] Present(string prefix) {
    var found = new System.Collections.Generic.List<string>();
    IntPtr set = SetupDiGetClassDevsW(IntPtr.Zero, "USB", IntPtr.Zero, 2 | 4);
    if (set == new IntPtr(-1)) Fail("SetupDiGetClassDevs");
    try {
      var d = new DEVINFO(); d.cbSize = Marshal.SizeOf(typeof(DEVINFO));
      for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref d); i++) {
        var id = new StringBuilder(512); int req;
        if (SetupDiGetDeviceInstanceIdW(set, ref d, id, id.Capacity, out req) &&
            id.ToString().StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) found.Add(id.ToString());
      }
    } finally { SetupDiDestroyDeviceInfoList(set); }
    return found.ToArray();
  }

  // restoreClass: the class the device was in before (an OpenPort on the serial driver is in
  // Ports, one on Tactrix's driver in whatever class that driver uses, an SM2 with no driver in
  // none); a failed binding puts it back, "" leaves it alone.
  public static string Bind(string instanceId, string inf, string restoreClass) {
    string r = Attempt(instanceId, inf, null);
    if (r == "nolist") {
      // A device on the serial driver is in the Ports class, whose driver list holds no WinUSB
      // node: put it in WinUSB's class, then look again on a fresh enumeration.
      Attempt(instanceId, inf, USBDEVICE);
      r = Attempt(instanceId, inf, null);
      if (r != "ok" && !string.IsNullOrEmpty(restoreClass)) { try { Attempt(instanceId, inf, restoreClass); } catch (Exception) { } }   // leave it as found
    }
    return r;
  }
}
'@
    # Compiling this costs about half a second every run; the build is kept in the temp folder,
    # named for a hash of its source, and loaded from there afterwards.
    $dll = $null
    try {
        $sha = [Security.Cryptography.SHA1]::Create()
        $h = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($src))) -replace '-', '').Substring(0, 12)
        $dll = Join-Path ([IO.Path]::GetTempPath()) "ovt_opdrv_$h.dll"
        if (-not (Test-Path -LiteralPath $dll)) {
            $tmpDll = "$dll.$PID.tmp"
            Add-Type -TypeDefinition $src -OutputAssembly $tmpDll -OutputType Library
            try { Move-Item -LiteralPath $tmpDll -Destination $dll -Force } catch { Remove-Item -LiteralPath $tmpDll -Force -ErrorAction SilentlyContinue }
        }
        Add-Type -Path $dll
    } catch { }
    if (-not ('OpDrv' -as [type])) { Add-Type -TypeDefinition $src }     # no cache: compile in memory
}

# ---- actions -----------------------------------------------------------------
# -Replace: also take over a cable that has another driver (Tactrix's, Zadig's). That
# driver stays installed; `remove` hands the cable straight back to it.
function Invoke-Install([bool]$Takeover) {
    $devs = @(Select-Targets (Get-DeviceList | Where-Object { $_.Present }))
    foreach ($d in $devs) { Emit "DEVICE|$($d.Id)|1|$($d.State)|$($d.Service)|$($d.Inf)" }
    # Tactrix's driver is for the OpenPort only: with it installed an OpenPort is left alone
    # (unless -Replace), a Scanmatik SM2 is bound as usual.
    if ((Test-Tactrix) -and -not $Takeover) {
        $skipped = @($devs | Where-Object { $_.Tactrix })
        if ($skipped) {
            Emit 'TACTRIX|1'
            $devs = @($devs | Where-Object { -not $_.Tactrix })
            if (-not $devs) { return 5 }
        }
    }
    if (-not $devs) { Emit 'NONE|'; return 2 }
    $todo = @($devs | Where-Object { $_.State -eq 'unbound' -or ($Takeover -and $_.State -eq 'other') })
    if (-not $todo) { return 0 }                    # every plugged-in device already has a driver
    if (-not (Test-Admin)) { Emit 'NEEDADMIN|'; return 3 }
    Initialize-Binder
    $failed = 0
    $unregistered = @{}
    foreach ($d in $todo) {
        try {
            # remember what an overridden driver was, before it is replaced, so a restore can be checked
            if ($d.State -eq 'other') { Set-Replaced $d.Id $d.Service $d.Inf $d.Class }
            $r = [OpDrv]::Bind($d.Id, $WinUsbInf, $d.Class)
            if ($r -eq 'ok') {
                Emit "BOUND|$($d.Id)"
                # an overridden maker's driver: its J2534 entry would list a device it cannot open
                if ($d.State -eq 'other' -and -not $unregistered.ContainsKey($d.Kind)) {
                    $unregistered[$d.Kind] = $true
                    try { Save-MakerRegistration $d.Kind } catch { Emit "WARN|$($d.Kind)|J2534 entry not set aside: $($_.Exception.Message)" }
                }
            }
            else {
                if ($d.State -eq 'other') { Clear-Replaced $d.Id }
                Emit "FAILED|$($d.Id)|$r"; $failed++
            }
        } catch {
            if ($d.State -eq 'other') { Clear-Replaced $d.Id }
            Emit "FAILED|$($d.Id)|$($_.Exception.Message)"; $failed++
        }
    }
    if ($failed) { return 4 }
    return 0
}

# Restore: drop the binding and let Windows choose the device's driver again. The original
# driver is still in the driver store (this never removed it), so it wins the ranking; wait for
# the device to come back and say whether it is on what it was overridden from.
function Invoke-Remove {
    if (-not (Test-Admin)) { Emit 'NEEDADMIN|'; return 3 }
    $mine = @(Select-Targets (Get-DeviceList | Where-Object { $_.State -eq 'winusb' }))
    $was = Get-Replaced
    $pnp = Join-Path $env:windir 'System32\pnputil.exe'
    $failed = 0
    $removed = @()
    $reregistered = @{}
    $ownGone = @{}
    foreach ($d in $mine) {
        # A WinUSB package of its own (libwdi / Zadig / an earlier OVT-Suite: oemNN.inf, not
        # Windows' winusb.inf) would be picked again when the device comes back: drop it too.
        if ($d.Inf -and $d.Inf -ine 'winusb.inf') {
            $infPath = Join-Path $env:windir "INF\$($d.Inf)"
            if ((Test-Path -LiteralPath $infPath) -and
                (Select-String -LiteralPath $infPath -Pattern '^\s*Provider\s*=\s*"?libwdi' -Quiet)) {
                & $pnp /delete-driver $d.Inf /uninstall /force | Out-Null
            }
        }
        & $pnp /remove-device $d.Id | Out-Null
        if ($LASTEXITCODE -eq 0) { Emit "REMOVED|$($d.Id)"; $removed += $d } else { Emit "FAILED|$($d.Id)|pnputil $LASTEXITCODE"; $failed++ }
    }
    if ($mine) { & $pnp /scan-devices | Out-Null }
    foreach ($d in $removed) {
        $key = $d.Id.ToUpper()
        $exp = if ($was.ContainsKey($key)) { ($was[$key] -split '\|')[0] } else { $null }
        $now = $null
        for ($i = 0; $i -lt 30 -and -not $now; $i++) {       # up to ~30 s for Windows to pick the driver
            Start-Sleep -Seconds 1
            $now = @(Get-DeviceList | Where-Object { $_.Id -ieq $d.Id -and $_.Present -and $_.State -ne 'winusb' }) | Select-Object -First 1
        }
        if (-not $now) {
            # not plugged in any more (or still enumerating): nothing to verify
            Emit "NOTRESTORED|$($d.Id)|$exp|"
            if ($null -ne $exp) { $failed++ }
        } elseif ($null -ne $exp -and $now.Service -ine $exp) {
            Emit "NOTRESTORED|$($d.Id)|$exp|$($now.Service)"; $failed++
        } else {
            Emit "RESTORED|$($d.Id)|$($now.Service)|$($now.Inf)"
            Clear-Replaced $d.Id
            if ($null -ne $exp -and -not $reregistered.ContainsKey($d.Kind)) {
                $reregistered[$d.Kind] = $true
                try { Restore-MakerRegistration $d.Kind } catch { Emit "WARN|$($d.Kind)|J2534 entry not put back: $($_.Exception.Message)" }
            }
            if (-not $ownGone.ContainsKey($d.Kind)) {
                $ownGone[$d.Kind] = $true
                try { Remove-OwnRegistration $d.Kind } catch { Emit "WARN|$($d.Kind)|our J2534 entry not removed: $($_.Exception.Message)" }
            }
        }
    }
    if ($failed) { return 4 }
    return 0
}

# ---- the window (Start menu) ---------------------------------------------------
# One small dialog for every case -- a cable on the serial driver, on Tactrix's (or
# another) driver, or already on this one. It looks again by itself every few seconds,
# so plugging a cable in shows up without a click.
function Show-Window {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    if (-not ('OpDpi' -as [type])) {
        Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class OpDpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
    }
    [void][OpDpi]::SetProcessDPIAware()
    # Its own taskbar identity (the Start menu shortcut carries the same one), so the window
    # shows this icon and does not group under PowerShell.
    try {
        if (-not ('OpShell.Native' -as [type])) {
            Add-Type -Namespace OpShell -Name Native -MemberDefinition '[DllImport("shell32.dll", CharSet=CharSet.Unicode)] public static extern int SetCurrentProcessExplicitAppUserModelID(string id);'
        }
        [void][OpShell.Native]::SetCurrentProcessExplicitAppUserModelID('OVT-Suite.J2534Driver')
    } catch { }
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $script:Quiet = $true
    $W = 'System.Windows.Forms'

    $form = New-Object "$W.Form"
    $form.Text = 'OVT-Suite J2534 USB driver'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ClientSize = New-Object System.Drawing.Size(560, 392)
    $form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
    try {
        $here = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { (Get-Location).Path }
        $ico = Join-Path $here 'ovtsuite_openport.ico'
        if (Test-Path -LiteralPath $ico) { $form.Icon = New-Object System.Drawing.Icon($ico) }
    } catch { }          # the window is fine without its icon

    $intro = New-Object "$W.Label"
    $intro.SetBounds(14, 12, 532, 44)
    $intro.Text = 'Gives a Tactrix OpenPort 2.0 or a Scanmatik SM2 the WinUSB driver that is built into Windows, so OVT-Suite can use it directly.'
    $form.Controls.Add($intro)

    $list = New-Object "$W.ListView"
    $list.SetBounds(14, 62, 532, 100)
    $list.View = 'Details'
    $list.FullRowSelect = $true
    $list.HideSelection = $false
    $list.MultiSelect = $false
    [void]$list.Columns.Add('Device', 150)
    [void]$list.Columns.Add('Driver in use', 360)
    $form.Controls.Add($list)

    $info = New-Object "$W.Label"
    $info.SetBounds(14, 168, 532, 66)
    $form.Controls.Add($info)

    $btnInstall = New-Object "$W.Button"
    $btnInstall.SetBounds(14, 242, 262, 34)
    $form.Controls.Add($btnInstall)

    $btnBack = New-Object "$W.Button"
    $btnBack.SetBounds(284, 242, 262, 34)
    $btnBack.Text = 'Restore the original driver'
    $form.Controls.Add($btnBack)

    $status = New-Object "$W.Label"
    $status.SetBounds(14, 286, 532, 60)
    $status.ForeColor = [System.Drawing.Color]::DarkGreen
    $form.Controls.Add($status)

    $btnClose = New-Object "$W.Button"
    $btnClose.SetBounds(466, 352, 80, 28)
    $btnClose.Text = 'Close'
    $btnClose.DialogResult = 'Cancel'
    $form.Controls.Add($btnClose)
    $form.CancelButton = $btnClose

    $state = @{ Needs = @(); Others = @(); Ours = @(); Busy = $false; Sig = $null; SelId = '' }

    # A friendly name for what a device is on.
    $driverName = {
        param($svc)
        if ($svc -ieq 'SmUsb') { "Scanmatik's driver (SmUsb)" } else { "another driver ($svc)" }
    }

    $refresh = {
        $ports   = @(Get-DeviceList)
        $present = @($ports | Where-Object { $_.Present })
        $was     = Get-Replaced
        # Rebuild the list only when a cable or its driver changed, and keep the selection:
        # the timer calls this every few seconds and a rebuild would drop the highlight.
        $sig = ($present | ForEach-Object { "$($_.Id)|$($_.State)|$($_.Service)" }) -join ';'
        if ($sig -ne $state.Sig) {
            $state.Sig = $sig
            $selected = if ($list.SelectedItems.Count) { $list.SelectedItems[0].Tag } else { '' }
            $list.BeginUpdate()
            $list.Items.Clear()
            foreach ($d in $present) {
                $what = switch ($d.State) {
                    'winusb'  {
                        # (not $w: variable names are case-insensitive and $W is the Forms namespace)
                        $repl = $was[$d.Id.ToUpper()]
                        if ($repl) { "OVT-Suite driver (WinUSB), overriding $(& $driverName (($repl -split '\|')[0]))" } else { 'OVT-Suite driver (WinUSB)' }
                    }
                    'unbound' { if ($d.Serial) { "Windows' generic serial driver (a COM port)" } else { 'No driver' } }
                    default   { (& $driverName $d.Service).Substring(0, 1).ToUpper() + (& $driverName $d.Service).Substring(1) }
                }
                $item = New-Object "$W.ListViewItem" ($d.Name + ' ' + ($d.Id -split '\\')[-1])
                $item.Tag = $d.Id
                [void]$item.SubItems.Add($what)
                [void]$list.Items.Add($item)
                if ($selected -and $item.Tag -ieq $selected) { $item.Selected = $true }
            }
            $list.EndUpdate()
        }
        # The buttons act on the selected row, or on every plugged-in device when none is selected.
        $state.SelId = if ($list.SelectedItems.Count) { "$($list.SelectedItems[0].Tag)" } else { '' }
        $targets = if ($state.SelId) { @($present | Where-Object { $_.Id -ieq $state.SelId }) } else { $present }
        $state.Ours   = @($targets | Where-Object { $_.State -eq 'winusb' })
        $state.Needs  = @($targets | Where-Object { $_.State -ne 'winusb' })
        $state.Others = @($targets | Where-Object { $_.State -eq 'other' })
        $lines = @()
        if (-not $present) { $lines += 'No OpenPort or SM2 is plugged in. Plug it in; this list updates by itself.' }
        if ((Test-Tactrix) -and @($present | Where-Object { $_.Tactrix })) { $lines += "Tactrix's OpenPort driver is installed on this computer." }
        if ((Test-Scanmatik) -and @($present | Where-Object { $_.Kind -eq 'sm2' })) { $lines += "Scanmatik's SM2 driver is installed on this computer." }
        if ($present.Count -gt 1) { $lines += 'Select a device to act on just that one; with none selected the buttons act on all.' }
        $info.Text = $lines -join "`r`n"
        $btnInstall.Text = if ($state.Others) { 'Override the other driver with this one' } else { 'Install the OVT-Suite driver' }
        $btnInstall.Enabled = ($state.Needs.Count -gt 0) -and -not $state.Busy
        $btnBack.Enabled    = ($state.Ours.Count -gt 0) -and -not $state.Busy
    }
    $list.Add_SelectedIndexChanged({ if (-not $state.Busy) { & $refresh } })

    $work = {
        param([scriptblock]$job)
        $state.Busy = $true
        $btnInstall.Enabled = $false; $btnBack.Enabled = $false
        $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
        $status.ForeColor = [System.Drawing.Color]::DarkGreen
        $status.Text = 'Working...'
        $form.Refresh()
        try { & $job }
        catch {
            $status.ForeColor = [System.Drawing.Color]::Firebrick
            $status.Text = "Failed: $($_.Exception.Message)"
        } finally {
            $form.Cursor = [System.Windows.Forms.Cursors]::Default
            $state.Busy = $false
            & $refresh
        }
    }

    $btnInstall.Add_Click({
        if ($state.Others) {
            $names = ($state.Others | ForEach-Object { "$($_.Name): $(& $driverName $_.Service)" }) -join "`r`n"
            $ask = "Override the driver of this device with the OVT-Suite driver?`r`n`r`n$names`r`n`r`n" +
                   "That driver stays installed, and 'Restore the original driver' puts the device back on it. " +
                   "Programs that need that driver will not see the device until you restore it."
            $r = [System.Windows.Forms.MessageBox]::Show($ask, 'OVT-Suite J2534 USB driver', 'YesNo', 'Question')
            if ($r -ne 'Yes') { return }
        }
        & $work {
            $script:Results = @()
            $script:Id = $state.SelId
            $code = Invoke-Install $true
            $n = @($script:Results | Where-Object { $_ -like 'BOUND|*' }).Count
            $bad = @($script:Results | Where-Object { $_ -like 'FAILED|*' })
            if ($code -eq 0 -and $n) {
                $status.Text = "Done: $n device(s) now use the OVT-Suite driver. If OVT-Suite does not see it, unplug the cable and plug it back in."
            } elseif ($bad) {
                $status.ForeColor = [System.Drawing.Color]::Firebrick
                $status.Text = 'The driver could not be installed: ' + (($bad[0] -split '\|', 3)[2])
            } else { $status.Text = 'Nothing needed changing.' }
        }
    })

    $btnBack.Add_Click({
        & $work {
            $script:Results = @()
            $script:Id = $state.SelId
            $code = Invoke-Remove
            $ok  = @($script:Results | Where-Object { $_ -like 'RESTORED|*' })
            $bad = @($script:Results | Where-Object { $_ -like 'NOTRESTORED|*' -or $_ -like 'FAILED|*' })
            if ($code -eq 0) {
                $back = ($ok | ForEach-Object { $p = $_ -split '\|'; if ($p[2]) { $p[2] } else { 'no driver' } }) -join ', '
                $status.Text = "Done: $($ok.Count) device(s) restored" + $(if ($back) { " (now on: $back)." } else { '.' })
            } else {
                $status.ForeColor = [System.Drawing.Color]::Firebrick
                $p = if ($bad) { $bad[0] -split '\|' } else { @() }
                $status.Text = 'The original driver did not come back for every device' +
                    $(if ($p.Count -ge 4 -and $p[0] -eq 'NOTRESTORED') { " (expected $($p[2]), got $(if ($p[3]) { $p[3] } else { 'nothing' }))." } else { '.' }) +
                    ' Unplug the cable and plug it back in, or reinstall the maker''s driver.'
            }
        }
    })

    $timer = New-Object "$W.Timer"
    $timer.Interval = 2500
    $timer.Add_Tick({ if (-not $state.Busy) { & $refresh } })
    $form.Add_Shown({ & $refresh; $timer.Start() })
    $form.Add_FormClosed({ $timer.Stop() })
    [void]$form.ShowDialog()
}

# ---- entry -------------------------------------------------------------------
if ($Action -eq 'status') {
    $was = Get-Replaced
    foreach ($d in @(Select-Targets (Get-DeviceList))) {
        Emit "DEVICE|$($d.Id)|$([int]$d.Present)|$($d.State)|$($d.Service)|$($d.Inf)"
        $key = $d.Id.ToUpper()
        if ($d.State -eq 'winusb' -and $was.ContainsKey($key)) {
            $p = $was[$key] -split '\|'
            Emit "REPLACED|$($d.Id)|$($p[0])|$($p[1])"
        }
    }
    if (Test-Tactrix) { Emit 'TACTRIX|1' }
    if (Test-Scanmatik) { Emit 'SCANMATIK|1' }
    exit 0
}

if ($Action -eq 'manage') {
    if (-not (Test-Admin)) {
        # relaunch elevated (no console window); this one is done
        $argl = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Action manage"
        try { Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $argl -Verb RunAs -WindowStyle Hidden } catch { }
        exit 0
    }
    Show-Window
    exit 0
}

$code = if ($Action -eq 'install') { Invoke-Install $Replace.IsPresent } else { Invoke-Remove }
exit $code
