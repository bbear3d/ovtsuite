// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Blair Campbell
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.
//
// A standalone tool's launcher: OVTFlasher.exe, OVTSniffer.exe, OVTLogger.exe,
// OVTSimulator.exe, OVTMcp.exe. It starts
//
//     "<its folder>\OVTSuite.exe" --tool <name> <its own arguments, verbatim>
//
// and packaging/entry.py (ovtsuite/launcher.py) runs that tool. The code lives
// once, in OVTSuite.exe; a launcher is a few KB that does not change from one
// release to the next (docs/UPDATES.md, "One copy of the code").
//
// Built by tools/build_launchers.py with the C# compiler of the .NET Framework
// 4.x that ships with Windows 10/11 (csc.exe, C# 5 at most: no $"", no ?.,
// no => members). One source for every launcher: the tool, its title and
// whether it is a console program come from a generated LauncherInfo.cs, its
// icon from /win32icon.
//
// Windowed tools (csc /target:winexe): start OVTSuite.exe and exit at once.
// The tool's window belongs to OVTSuite.exe, which sets the tool's
// AppUserModelID itself, so the taskbar groups it under the tool's shortcut.
// The launcher hands on how it was started (show state, the shortcut it came
// from) so a shortcut's "Run: minimised" and its taskbar identity still apply.
//
// OVTMcp (csc /target:exe): the MCP server speaks over stdin/stdout. The
// launcher is a console program; it starts OVTSuite.exe with ITS standard
// handles (STARTF_USESTDHANDLES, made inheritable), waits, and returns the
// child's exit code. The child is in a job object that kills it when the
// launcher goes, so an MCP client that kills the launcher leaves nothing
// behind. OVTSuite.exe is a windowed (GUI-subsystem) program, but a GUI
// process given valid handles this way has them as its standard handles and
// Python opens sys.stdin/stdout on them (PyInstaller's windowed bootloader
// leaves stdio to Python: configure_c_stdio).
//
// Keep ArgumentsOf and CommandLine in step with ovtsuite/launcher.py
// (strip_program_name, child_command_line): tests/test_launcher.py pins that.

using System;
using System.ComponentModel;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

namespace OVTSuiteLauncher
{
    internal static class Program
    {
        private static int Main()
        {
            string exe;
            try
            {
                string self = Assembly.GetEntryAssembly().Location;
                exe = Path.Combine(Path.GetDirectoryName(self), LauncherInfo.MainExe);
            }
            catch (Exception ex)
            {
                return Fail("Could not find where " + LauncherInfo.Title + " is installed:\n\n" + ex.Message);
            }
            if (!File.Exists(exe))
            {
                return Fail(LauncherInfo.Title + " needs " + LauncherInfo.MainExe +
                            " in the same folder, and it is not there:\n\n" + exe +
                            "\n\nReinstall " + LauncherInfo.Title + ".");
            }
            string cmd = CommandLine(exe, LauncherInfo.Tool, ArgumentsOf(Environment.CommandLine));
            try
            {
                if (LauncherInfo.IsConsole)
                {
                    return RunAttached(exe, cmd);
                }
                return RunDetached(exe, cmd);
            }
            catch (Win32Exception ex)
            {
                return Fail("Could not start " + exe + ":\n\n" + ex.Message);
            }
        }

        // Everything after the program name, verbatim (no re-quoting): the
        // child's C runtime then splits it exactly as ours would have.
        internal static string ArgumentsOf(string cmdline)
        {
            string s = cmdline ?? "";
            int i = 0;
            if (s.Length > 0 && s[0] == '"')
            {
                int j = s.IndexOf('"', 1);
                i = j < 0 ? s.Length : j + 1;
            }
            while (i < s.Length && s[i] != ' ' && s[i] != '\t')
            {
                i++;
            }
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t'))
            {
                i++;
            }
            return s.Substring(i);
        }

        internal static string CommandLine(string exe, string tool, string args)
        {
            string line = "\"" + exe + "\" --tool " + tool;
            return args.Length > 0 ? line + " " + args : line;
        }

        // -- windowed tools: start and go ------------------------------------
        private static int RunDetached(string exe, string cmd)
        {
            Native.STARTUPINFO own;
            Native.GetStartupInfoW(out own);
            Native.STARTUPINFO si = new Native.STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(Native.STARTUPINFO));
            // how we were started: the shortcut's show state, and the
            // shortcut / AppUserModelID the shell named (taskbar identity)
            si.dwFlags = own.dwFlags & (Native.STARTF_USESHOWWINDOW | Native.STARTF_TITLEISLINKNAME |
                                        Native.STARTF_TITLEISAPPID);
            si.wShowWindow = own.wShowWindow;
            if ((si.dwFlags & (Native.STARTF_TITLEISLINKNAME | Native.STARTF_TITLEISAPPID)) != 0)
            {
                si.lpTitle = own.lpTitle;
            }
            // we were started by the user: let the tool's window come to the front
            Native.AllowSetForegroundWindow(Native.ASFW_ANY);
            Native.PROCESS_INFORMATION pi;
            if (!Native.CreateProcessW(exe, new StringBuilder(cmd), IntPtr.Zero, IntPtr.Zero, false,
                                       0, IntPtr.Zero, null, ref si, out pi))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            Native.CloseHandle(pi.hThread);
            Native.CloseHandle(pi.hProcess);
            return 0;
        }

        // -- OVTMcp: the same stdin/stdout/stderr, wait, same exit code ------
        private static int RunAttached(string exe, string cmd)
        {
            IntPtr job = KillOnCloseJob();
            Native.STARTUPINFO si = new Native.STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(Native.STARTUPINFO));
            si.dwFlags = Native.STARTF_USESTDHANDLES;
            si.hStdInput = Inheritable(Native.GetStdHandle(Native.STD_INPUT_HANDLE));
            si.hStdOutput = Inheritable(Native.GetStdHandle(Native.STD_OUTPUT_HANDLE));
            si.hStdError = Inheritable(Native.GetStdHandle(Native.STD_ERROR_HANDLE));
            Native.PROCESS_INFORMATION pi;
            if (!Native.CreateProcessW(exe, new StringBuilder(cmd), IntPtr.Zero, IntPtr.Zero, true,
                                       Native.CREATE_SUSPENDED, IntPtr.Zero, null, ref si, out pi))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            if (job != IntPtr.Zero)
            {
                // best effort: in a job that allows no nesting it simply runs unjobbed
                Native.AssignProcessToJobObject(job, pi.hProcess);
            }
            Native.ResumeThread(pi.hThread);
            Native.CloseHandle(pi.hThread);
            Native.WaitForSingleObject(pi.hProcess, Native.INFINITE);
            uint code;
            if (!Native.GetExitCodeProcess(pi.hProcess, out code))
            {
                code = 1;
            }
            Native.CloseHandle(pi.hProcess);
            GC.KeepAlive(job);
            return unchecked((int)code);
        }

        private static IntPtr Inheritable(IntPtr h)
        {
            if (h != IntPtr.Zero && h != Native.INVALID_HANDLE_VALUE)
            {
                // inherited handles usually are already; a console pseudo-handle
                // (Windows 7) refuses this and is passed on regardless
                Native.SetHandleInformation(h, Native.HANDLE_FLAG_INHERIT, Native.HANDLE_FLAG_INHERIT);
            }
            return h;
        }

        private static IntPtr KillOnCloseJob()
        {
            IntPtr job = Native.CreateJobObjectW(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                return IntPtr.Zero;
            }
            Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = Native.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int size = Marshal.SizeOf(typeof(Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buf = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(info, buf, false);
                if (!Native.SetInformationJobObject(job, Native.JobObjectExtendedLimitInformation, buf, (uint)size))
                {
                    Native.CloseHandle(job);
                    return IntPtr.Zero;
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buf);
            }
            return job;
        }

        private static int Fail(string message)
        {
            if (LauncherInfo.IsConsole)
            {
                Console.Error.WriteLine(LauncherInfo.Title + ": " + message);
            }
            else
            {
                Native.MessageBoxW(IntPtr.Zero, message, LauncherInfo.Title, Native.MB_ICONERROR);
            }
            return 1;
        }
    }

    internal static class Native
    {
        public const int STARTF_USESHOWWINDOW = 0x00000001;
        public const int STARTF_USESTDHANDLES = 0x00000100;
        public const int STARTF_TITLEISLINKNAME = 0x00000800;
        public const int STARTF_TITLEISAPPID = 0x00001000;
        public const uint CREATE_SUSPENDED = 0x00000004;
        public const int STD_INPUT_HANDLE = -10;
        public const int STD_OUTPUT_HANDLE = -11;
        public const int STD_ERROR_HANDLE = -12;
        public const uint HANDLE_FLAG_INHERIT = 0x00000001;
        public const uint INFINITE = 0xFFFFFFFF;
        public const uint ASFW_ANY = 0xFFFFFFFF;
        public const uint MB_ICONERROR = 0x00000010;
        public const int JobObjectExtendedLimitInformation = 9;
        public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        public static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct STARTUPINFO
        {
            public int cb;
            public IntPtr lpReserved;
            public IntPtr lpDesktop;
            public IntPtr lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public int dwProcessId;
            public int dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool CreateProcessW(string lpApplicationName, StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes, IntPtr lpThreadAttributes, bool bInheritHandles,
            uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory,
            ref STARTUPINFO lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern void GetStartupInfoW(out STARTUPINFO lpStartupInfo);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr GetStdHandle(int nStdHandle);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint ResumeThread(IntPtr hThread);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateJobObjectW(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetInformationJobObject(IntPtr hJob, int infoClass, IntPtr lpInfo, uint cbInfo);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("user32.dll")]
        public static extern bool AllowSetForegroundWindow(uint dwProcessId);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);
    }
}
