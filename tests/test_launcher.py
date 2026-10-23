# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Windows tool launchers (docs/UPDATES.md, "One copy of the code"):
``OVTSuite.exe --tool <name>`` dispatch in packaging/entry.py, the argument
hand-down the C# launcher does (mirrored in ovtsuite/launcher.py), the
relaunch target, the spec and installers, and tools/build_launchers.py with a
stand-in compiler. No Qt, no Windows."""

import importlib.util
import os
import re
import stat
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ovtsuite import branding  # noqa: E402
from ovtsuite import cli  # noqa: E402
from ovtsuite import launcher  # noqa: E402
from ovtsuite import updates as up  # noqa: E402


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def _entry():
    path = os.path.join(ROOT, "packaging", "entry.py")
    spec = importlib.util.spec_from_file_location("ovt_frozen_entry_launcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_current_tool():
    launcher.set_current(None)
    yield
    launcher.set_current(None)


# ------------------------------------------------------------------ parsing
def test_every_launcher_has_a_tool():
    exes = launcher.tool_exes()
    assert exes == dict(branding.TOOL_EXES, mcp=branding.MCP_EXE)
    assert set(exes) == {"flasher", "sniffer", "logger", "simulator", "converter", "mcp"}


@pytest.mark.parametrize("argv, tool, rest", [
    (["--tool", "flasher"], "flasher", []),
    (["--tool", "logger", "a.bin", "b b.bin"], "logger", ["a.bin", "b b.bin"]),
    (["--tool=sniffer", "--x"], "sniffer", ["--x"]),
    (["--tool", "MCP"], "mcp", []),
    (["--tool", "simulator", "--tool", "x"], "simulator", ["--tool", "x"]),
    ([], None, []),
    (["gui", "--tool", "flasher"], None, ["gui", "--tool", "flasher"]),   # first only
    (["car.bin"], None, ["car.bin"]),
])
def test_split_tool_argv(argv, tool, rest):
    assert launcher.split_tool_argv(argv) == (tool, rest)


@pytest.mark.parametrize("argv", [["--tool"], ["--tool", "editor"], ["--tool="],
                                  ["--tool", "ovtflasher"]])
def test_an_unknown_tool_is_refused(argv):
    with pytest.raises(launcher.UnknownTool):
        launcher.split_tool_argv(argv)


def test_cli_argv_is_what_the_tool_exes_ran():
    assert launcher.cli_argv("flasher", ["car.bin"]) == ["--flasher", "car.bin"]
    assert launcher.cli_argv("simulator", []) == ["--simulator"]
    assert launcher.cli_argv("mcp", ["--x"]) == ["mcp", "--x"]
    # and the CLI knows every one of them
    src = _read("ovtsuite", "cli.py")
    for tool in branding.TOOL_EXES:
        assert '"--%s"' % tool in src, tool
    assert '"mcp"' in src


# ------------------------------------------------------------------ packaging/entry.py
@pytest.mark.parametrize("argv, want", [
    (["--tool", "logger", "car.bin", "--x y"], ["--logger", "car.bin", "--x y"]),
    (["--tool", "flasher"], ["--flasher"]),
    (["--tool", "sniffer"], ["--sniffer"]),
    (["--tool", "simulator"], ["--simulator"]),
    (["--tool", "mcp"], ["mcp"]),
    (["--tool=mcp", "--verbose"], ["mcp", "--verbose"]),
    (["info", "car.bin"], ["info", "car.bin"]),
])
def test_entry_dispatches_on_the_tool(monkeypatch, argv, want):
    entry = _entry()
    seen = []
    monkeypatch.setattr(cli, "main", lambda a: seen.append(a) or 0)
    monkeypatch.setattr(sys, "argv", [r"C:\OVT\OVTSuite.exe"] + argv)
    assert entry._main() == 0
    assert seen == [want]
    tool = launcher.split_tool_argv(argv)[0]
    assert launcher.current() == tool


def test_entry_refuses_an_unknown_tool(monkeypatch, capsys):
    entry = _entry()
    monkeypatch.setattr(cli, "main", lambda a: pytest.fail("ran %r" % a))
    monkeypatch.setattr(sys, "argv", [r"C:\OVT\OVTSuite.exe", "--tool", "nope"])
    assert entry._main() == 2
    assert "no such tool: 'nope'" in capsys.readouterr().err


def test_the_exe_name_no_longer_picks_the_tool(monkeypatch):
    """The launchers are not copies of the app any more: an exe called
    OVTLogger.exe that is the app (an old build's leftover) runs the app."""
    entry = _entry()
    seen = []
    monkeypatch.setattr(cli, "main", lambda a: seen.append(a) or 0)
    monkeypatch.setattr(sys, "executable", r"C:\OVT\OVTLogger.exe")
    monkeypatch.setattr(sys, "argv", [r"C:\OVT\OVTLogger.exe", "info", "car.bin"])
    assert entry._main() == 0 and seen == [["info", "car.bin"]]


def test_the_helper_and_the_mutex_order_is_kept():
    entry = _read("packaging", "entry.py")
    main = entry[entry.index("def main():"):]
    assert main.index('"--apply-update"') < main.index("_hold_app_mutex()")
    # an MCP server crash is logged, never a dialog nobody sees
    assert "if not _is_mcp(sys.argv[1:]):" in main


# ------------------------------------------------------------------ hand-down
@pytest.mark.parametrize("cmdline, args", [
    (r'"C:\Program Files\OVT-Suite\OVTFlasher.exe" car.bin', "car.bin"),
    (r'"C:\Program Files\OVT-Suite\OVTFlasher.exe"', ""),
    (r'"C:\Program Files\OVT-Suite\OVTFlasher.exe"   "a b.bin"  c', '"a b.bin"  c'),
    (r'C:\OVT\OVTLogger.exe "x y.bin" --z', '"x y.bin" --z'),
    (r'OVTMcp.exe', ""),
    (r'OVTMcp	--x', "--x"),                                    # a tab
    (r'"C:\a b\OVTMcp.exe"x --y', "--y"),                       # argv[0] runs on
    (r'"C:\a b\OVTFlasher.exe" "C:\dir with \"quote\".bin" \\srv\x',
     r'"C:\dir with \"quote\".bin" \\srv\x'),                   # verbatim
    ("", ""),
])
def test_the_launcher_hands_on_its_arguments_verbatim(cmdline, args):
    assert launcher.strip_program_name(cmdline) == args


def test_the_child_command_line():
    exe = r"C:\Program Files\OVT-Suite\OVTSuite.exe"
    assert launcher.child_command_line(exe, "flasher", "") == \
        r'"C:\Program Files\OVT-Suite\OVTSuite.exe" --tool flasher'
    assert launcher.child_command_line(exe, "logger", '"a b.bin" c') == \
        r'"C:\Program Files\OVT-Suite\OVTSuite.exe" --tool logger "a b.bin" c'


def test_the_csharp_launcher_matches_the_python_mirror():
    """Launcher.cs cannot be compiled here: hold its text to the rules the
    Python mirror is tested against, and to C# 5 (the .NET Framework csc)."""
    cs = _read("packaging", "launcher", "Launcher.cs")
    args = cs[cs.index("internal static string ArgumentsOf"):cs.index("internal static string CommandLine")]
    assert "s[0] == '\"'" in args and "s.IndexOf('\"', 1)" in args
    assert "s[i] != ' ' && s[i] != '\\t'" in args
    line = cs[cs.index("internal static string CommandLine"):cs.index("// -- windowed tools")]
    assert '"\\"" + exe + "\\" --tool " + tool' in line
    assert "Environment.CommandLine" in cs
    # the MCP launcher: own std handles, waits, exit code, job kill-on-close
    for needle in ("STARTF_USESTDHANDLES", "STD_INPUT_HANDLE", "STD_OUTPUT_HANDLE",
                   "STD_ERROR_HANDLE", "WaitForSingleObject", "GetExitCodeProcess",
                   "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", "CREATE_SUSPENDED",
                   "HANDLE_FLAG_INHERIT", "AllowSetForegroundWindow", "STARTF_TITLEISAPPID"):
        assert needle in cs, needle
    # C# 5: no interpolated strings, no null-conditional, no expression bodies
    code = "\n".join(ln for ln in cs.splitlines() if not ln.lstrip().startswith("//"))
    assert '$"' not in code and "?." not in code
    assert not re.search(r"\)\s*=>", code)
    assert "LauncherInfo.Tool" in cs and "LauncherInfo.IsConsole" in cs


# ------------------------------------------------------------------ relaunch
def test_a_tool_comes_back_as_the_tool(tmp_path):
    exe = tmp_path / "OVTSuite.exe"
    exe.write_bytes(b"x")
    assert launcher.relaunch_exe(str(exe)) == str(exe)
    # running OVT Flasher through its launcher
    assert launcher.relaunch_exe(str(exe), tool="flasher") == str(exe)   # no launcher there
    (tmp_path / "OVTFlasher.exe").write_bytes(b"l")
    assert launcher.relaunch_exe(str(exe), tool="flasher") == str(tmp_path / "OVTFlasher.exe")
    launcher.set_current("flasher")
    assert launcher.relaunch_exe(str(exe)) == str(tmp_path / "OVTFlasher.exe")


def test_the_update_paths_relaunch_through_the_launcher(tmp_path, monkeypatch):
    exe = tmp_path / "OVTSuite.exe"
    exe.write_bytes(b"x")
    (tmp_path / "OVTLogger.exe").write_bytes(b"l")
    monkeypatch.setattr(sys, "executable", str(exe))
    launcher.set_current("logger")
    bat = up.write_chain([str(tmp_path / "OVTSuite-0.5.0-setup.exe")], str(tmp_path / "dl"))
    text = open(bat, encoding="utf-8").read()
    assert "OVTLogger.exe" in text
    src = _read("ovtsuite", "delta_update.py")
    assert "relaunch = relaunch_exe()" in src


def test_a_tool_install_is_told_apart_by_the_manifest(tmp_path):
    app, tools = tmp_path / "OVT-Suite", tmp_path / "OVT Flasher"
    for d in (app, tools):
        os.makedirs(d)
        # both have the main exe now: every launcher starts it
        for n in ("OVTSuite.exe", "OVTFlasher.exe", "unins000.exe"):
            (d / n).write_bytes(b"x")
    (app / "app-manifest.json").write_text("{}")
    assert up.classify_install(str(app)) == "inno"
    assert up.classify_install(str(tools)) == "tools"
    assert up.classify_install(str(tools), registered=True) == "inno"


# ------------------------------------------------------------------ spec and installers
def test_the_code_is_in_one_archive():
    spec = _read("packaging", "ovtsuite.spec")
    assert "noarchive=False," in spec and "NOARCHIVE" not in spec
    assert "_tool_exes" not in spec
    # one exe: the one-file and the one-folder variant of it
    assert len(re.findall(r"^\s*exe = EXE\(", spec, re.M)) == 2
    assert spec.count("COLLECT(") == 1 and "exe, a.binaries, a.zipfiles, a.datas," in spec


def test_tool_installers_ship_the_main_exe():
    tools = _read("packaging", "tool_installer.iss")
    assert '#define Excl "\\modules"' in tools
    assert "MainExe" not in re.sub(r";.*", "", tools.split("[Setup]")[0]).replace(
        '#define MainExe "OVTSuite.exe"', "")
    ps = _read("build.ps1")
    assert "flasher = @('OVTSuite.exe', 'OVTFlasher.exe')" in ps


def test_build_script_builds_the_launchers_before_the_manifest():
    ps = _read("build.ps1")
    at = ps.index("Invoke-BuildLaunchers @('build', '--into'")
    assert at < ps.index("# 5d. App manifest")
    assert at < ps.index("'Authenticode signing'")
    sign = ps[ps.index("'Authenticode signing'"):ps.index("$SignDefs = @('/DSignTool")]
    # the signed launchers are kept, then the manifest says what was signed
    assert sign.index("@('adopt'") < sign.index("Write-AppManifest")


# ------------------------------------------------------------------ tools/build_launchers.py
def test_launcher_specs():
    import build_launchers as bl
    specs = {s["tool"]: s for s in bl.launchers()}
    assert set(specs) == set(launcher.tool_exes())
    for tool, s in specs.items():
        assert s["exe"] == launcher.tool_exes()[tool] + ".exe"
        assert os.path.isfile(s["icon"]), s["icon"]
        assert s["console"] == (tool == "mcp")
    assert specs["flasher"]["title"] == "OVT Flasher"
    assert specs["flasher"]["icon"].endswith("ovtsuite_flasher.ico")
    assert specs["mcp"]["icon"].endswith(os.sep + "ovtsuite.ico")


def test_launcher_constants_and_command():
    import build_launchers as bl
    spec = {"tool": "mcp", "exe": "OVTMcp.exe", "title": 'A "q" \\ b',
            "console": True, "icon": r"C:\x\ovtsuite.ico"}
    src = bl.constants_source(spec)
    assert 'public const string Tool = "mcp";' in src
    assert 'public const string Title = "A \\"q\\" \\\\ b";' in src
    assert 'public const string MainExe = "OVTSuite.exe";' in src
    assert "public const bool IsConsole = true;" in src
    # the launcher's own version, never the app's: an unchanged launcher keeps its bytes
    assert '[assembly: AssemblyFileVersion("%s")]' % bl.LAUNCHER_VERSION in src
    assert branding.__version__ not in src.replace(bl.LAUNCHER_VERSION, "")
    assert "Copyright" in src
    cmd = bl.csc_command("csc.exe", spec, "L.cs", "G.cs", "out.exe")
    assert "/target:exe" in cmd and "/win32icon:C:\\x\\ovtsuite.ico" in cmd
    assert "/out:out.exe" in cmd and cmd[-2:] == ["L.cs", "G.cs"]
    assert "/reference:System.dll" in cmd
    assert "/target:winexe" in bl.csc_command("csc", dict(spec, console=False), "a", "b", "c")


def _fake_csc(tmp_path):
    """A stand-in csc.exe: writes /out: with a fresh "build" every time,
    like the real one (no /deterministic), and counts its runs."""
    p = tmp_path / "csc"
    p.write_text("#!%s\n"
                 "import sys, os, time\n"
                 "out = [a[5:] for a in sys.argv if a.startswith('/out:')][0]\n"
                 "open(out, 'wb').write(b'MZ' + repr(time.time()).encode() + "
                 "' '.join(sys.argv[1:]).encode())\n"
                 "open(os.environ['CSC_LOG'], 'a').write('x')\n" % sys.executable)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    if os.name == "nt":                 # no #! there: a .cmd runs the script
        cmd = tmp_path / "csc.cmd"
        cmd.write_text('@"%s" "%s" %%*\n' % (sys.executable, p))
        return str(cmd)
    return str(p)


def test_build_compiles_only_what_changed_and_adopts_signed_ones(tmp_path, monkeypatch):
    import build_launchers as bl
    csc, log = _fake_csc(tmp_path), tmp_path / "runs"
    monkeypatch.setenv("CSC_LOG", str(log))
    cache, app = str(tmp_path / "cache"), tmp_path / "app"
    app.mkdir()
    src = tmp_path / "Launcher.cs"
    src.write_text(_read("packaging", "launcher", "Launcher.cs"))
    common = ["--cache", cache, "--source", str(src)]
    n = len(bl.launchers())
    assert bl.main(["build", "--into", str(app), "--csc", csc] + common) == 0
    assert len(log.read_text()) == n
    first = {s["exe"]: (app / s["exe"]).read_bytes() for s in bl.launchers()}
    assert all(b.startswith(b"MZ") for b in first.values())
    # again, nothing changed: no compiler run, the same bytes
    (app / "OVTFlasher.exe").unlink()
    assert bl.main(["build", "--into", str(app), "--csc", csc] + common) == 0
    assert len(log.read_text()) == n
    assert {e: (app / e).read_bytes() for e in first} == first
    # "signed" in the app folder: adopt keeps it, the next build copies it back
    signed = first["OVTMcp.exe"] + b"SIGNATURE"
    (app / "OVTMcp.exe").write_bytes(signed)
    assert bl.main(["adopt", "--from", str(app)] + common) == 0
    (app / "OVTMcp.exe").unlink()
    assert bl.main(["build", "--into", str(app), "--csc", csc] + common) == 0
    assert (app / "OVTMcp.exe").read_bytes() == signed and len(log.read_text()) == n
    # the source changes: every launcher is compiled again; adopt of a stale
    # launcher does not overwrite the new one
    src.write_text(src.read_text() + "\n// changed\n")
    assert bl.main(["adopt", "--from", str(app)] + common) == 0
    assert bl.main(["build", "--into", str(app), "--csc", csc] + common) == 0
    assert len(log.read_text()) == 2 * n
    assert (app / "OVTMcp.exe").read_bytes() != signed


def test_build_without_a_compiler_says_where_it_looked(tmp_path, monkeypatch):
    import build_launchers as bl
    monkeypatch.setenv("OVT_CSC", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(bl, "find_csc", lambda: None)
    with pytest.raises(SystemExit) as ei:
        bl.main(["build", "--cache", str(tmp_path / "c")])
    assert "v4.0.30319" in str(ei.value)
