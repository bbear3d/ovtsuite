# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Watch folder and right-click menu of OVT Converter (no Qt)."""

import os
import sys

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import shellmenu
from ovtsuite.convert.watch import Watcher, roles


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _settled(w, clock):
    """Poll twice, the settle time apart: what a file that stopped changing gives."""
    first = w.poll()
    clock.t += 5
    return first + w.poll()


def _bin(path, n=0x1000, fill=0x5A):
    path.write_bytes(bytes([fill]) * n)
    return str(path)


def test_arriving_binary_converts_once_it_settles(tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    clock = Clock()
    w = Watcher(str(inbox), C.Job(sources=[], target="hex"), clock=clock)
    _bin(inbox / "a.bin")
    assert w.poll() == []                      # still being written, maybe
    clock.t += 5
    ev = w.poll()
    assert [e.ok for e in ev] == [True]
    out = inbox / "Converted" / "a.hex"
    assert out.is_file() and ev[0].out == [str(out)]
    assert w.poll() == []                      # once only
    # its own output folder is never taken as an arrival
    clock.t += 5
    assert w.poll() == []


def test_files_already_there_wait_unless_asked(tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    _bin(inbox / "old.bin")
    clock = Clock()
    w = Watcher(str(inbox), C.Job(sources=[], target="hex"), clock=clock)
    assert _settled(w, clock) == []
    w2 = Watcher(str(inbox), C.Job(sources=[], target="hex"), existing=True, clock=clock)
    assert [e.ok for e in _settled(w2, clock)] == [True]


def test_partial_names_skipped_and_wrong_kind_reported(tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    clock = Clock()
    w = Watcher(str(inbox), C.Job(sources=[], target="xdf"), clock=clock)
    _bin(inbox / "download.bin.part")
    _bin(inbox / "car.bin")                    # a binary, but the job makes XDFs
    ev = _settled(w, clock)
    assert [(os.path.basename(e.path), e.ok) for e in ev] == [("car.bin", None)]
    assert "not used" in ev[0].text


def test_move_after_and_a_changed_file_converts_again(tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    clock = Clock()
    w = Watcher(str(inbox), C.Job(sources=[], target="srec"), after="move", clock=clock)
    _bin(inbox / "x.bin")
    assert [e.ok for e in _settled(w, clock)] == [True]
    assert (inbox / "Done" / "x.bin").is_file() and not (inbox / "x.bin").exists()
    w2 = Watcher(str(inbox), C.Job(sources=[], target="srec"), clock=clock)
    p = _bin(inbox / "y.bin")
    assert [e.ok for e in _settled(w2, clock)] == [True]
    _bin(inbox / "y.bin", fill=0x11)
    os.utime(p, (clock.t + 50, clock.t + 50))
    assert [e.ok for e in _settled(w2, clock)] == [True]


def test_binary_arriving_beside_a_fixed_definition_is_the_binary(tmp_path):
    from test_convert_romxml import ROMRAIDER
    defs = tmp_path / "ecu_defs.xml"
    defs.write_text(ROMRAIDER)
    inbox = tmp_path / "bins"
    inbox.mkdir()
    out = tmp_path / "values"
    clock = Clock()
    job = C.Job(sources=[str(defs)], target="dcm")
    w = Watcher(str(inbox), job, out_dir=str(out), clock=clock)
    assert "binary" in w.roles()["image"] and "DCM" in w.roles()["image"]
    img = bytearray(0x10000)
    img[0x2004:0x200C] = b"A2WC500R"
    (inbox / "car.bin").write_bytes(bytes(img))
    ev = _settled(w, clock)
    assert [e.ok for e in ev] == [True], [e.text for e in ev]
    assert (out / "car.dcm").is_file()


def test_refusals():
    with pytest.raises(C.ConvertError, match="output folder"):
        Watcher(".", C.Job(sources=[], target="hex"), out_dir=".")
    with pytest.raises(C.ConvertError, match="two definitions"):
        Watcher(".", C.Job(sources=[], target="diff"))
    with pytest.raises(C.ConvertError, match="No such folder"):
        Watcher("/no/such/dir", C.Job(sources=[], target="hex"))


def test_roles_in_words():
    r = roles(C.Job(sources=[], target="hex"), set())
    assert r["image"].startswith("converted") and r["definition"] == "not used"
    r = roles(C.Job(sources=["a.a2l"], target="bin", binary="b.bin"), {"definition"})
    assert r["values"].startswith("written into the binary")


def test_cli_watch_needs_a_target(tmp_path, capsys):
    from ovtsuite.cli import main
    assert main(["convert", "--watch", str(tmp_path)]) == 1
    assert "--to" in capsys.readouterr().err


def test_shell_menu_linux_files_show_for_the_converters_files_only(tmp_path, monkeypatch):
    """No entry on every file: Dolphin and GNOME by the converter's own MIME
    types (registered by extension, below every standard type), Nemo by
    extension. Written the same on any system, so tested anywhere."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(shellmenu, "_refresh_mime", lambda: None)
    legacy = shellmenu._legacy_script()
    os.makedirs(os.path.dirname(legacy))
    open(legacy, "w").close()
    where = shellmenu._linux_install()
    assert set(where) == {"GNOME Files", "Nemo", "Dolphin"}
    assert not os.path.exists(legacy)              # the old every-file script goes
    files = shellmenu._linux_files()
    dolphin = open(files["Dolphin"]).read()
    assert "all/allfiles" not in dolphin and "application/x-ovt-a2l;" in dolphin
    gnome = open(files["GNOME Files"]).read()
    assert "MimeType=application/x-ovt-a2l;" in gnome and "%F" in gnome
    nemo = open(files["Nemo"]).read()
    assert "Extensions=" in nemo and "a2l;" in nemo and "%F" in nemo
    xml = open(shellmenu._mime_package()).read()
    assert '<glob pattern="*.a2l" weight="10"/>' in xml
    assert set(shellmenu._linux_installed()) == set(where)
    assert set(shellmenu._linux_remove()) == set(where)
    assert shellmenu._linux_installed() == []
    assert not os.path.exists(shellmenu._mime_package())


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="the Windows registry")
def test_shell_menu_two_installs_never_remove_each_other(tmp_path, monkeypatch):
    """The app and the standalone converter each write their own entry; one
    shows, the other waits; uninstalling either leaves the other's."""
    import winreg
    sandbox = r"Software\OVTSuiteTest-%d" % os.getpid()
    monkeypatch.setattr(shellmenu, "_CLASSES", sandbox + r"\%s\shell")
    monkeypatch.setattr(shellmenu, "EXTENSIONS", (".a2l", ".xdf"))
    monkeypatch.setattr(shellmenu, "_user_hives", lambda: [])
    monkeypatch.setattr(shellmenu, "_win_refresh", lambda: None)
    apps = {}
    for name in ("app", "tool"):
        d = tmp_path / name
        d.mkdir()
        exe = d / "OVTConverter.exe"
        exe.write_bytes(b"MZ")
        apps[name] = [str(exe)]
    try:
        monkeypatch.setattr(shellmenu, "command", lambda: apps["app"])
        shellmenu.install()
        monkeypatch.setattr(shellmenu, "command", lambda: apps["tool"])
        shellmenu.install()
        ent = shellmenu._win_entries()
        assert len(ent) == 2
        assert sorted(h for _e, h in ent.values()) == [False, True]   # one shows
        shellmenu.remove()                         # the tool's uninstaller
        ent = shellmenu._win_entries()
        assert [e for e, _h in ent.values()] == apps["app"]
        assert not any(h for _e, h in ent.values())  # the app's shows again
        assert shellmenu.installed()
        shellmenu.install()                        # (the tool's again)
        shellmenu.remove(everywhere=True)          # the user turns it off
        assert shellmenu._win_entries() == {} and not shellmenu.installed()
    finally:
        for verb in list(shellmenu._win_entries()):
            shellmenu._win_delete_verb(verb)
        for k in (sandbox + r"\.a2l\shell", sandbox + r"\.xdf\shell",
                  sandbox + r"\.a2l", sandbox + r"\.xdf", sandbox):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass


def test_shell_menu_command_and_windows_line():
    cmd = shellmenu.command()
    assert cmd[-1] == "--converter" or cmd[0].lower().endswith(("ovtconverter.exe", "ovtconverter"))
    line = shellmenu._win_cmdline(["C:\\Program Files\\OVT\\OVTConverter.exe"])
    assert line == '"C:\\Program Files\\OVT\\OVTConverter.exe" "%1"'
    mine = "%s.%s" % (shellmenu.VERB, shellmenu._tag())
    assert all(k.endswith("\\shell\\" + mine) for k in shellmenu._win_keys())
    assert ".xml" not in shellmenu.EXTENSIONS        # no menu on every XML file


@pytest.mark.parametrize("iss", ["installer.iss", "tool_installer.iss"])
def test_installers_offer_the_menu_unticked_and_always_remove_it(iss):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    s = open(os.path.join(root, "packaging", iss), encoding="utf-8").read()
    task = s[s.index('Name: "shellmenu"'):]
    assert "Flags: unchecked" in task.split("\n\n")[0]
    assert '"convert --shell-menu install"' in s and "Tasks: shellmenu" in s
    un = s[s.index("[UninstallRun]"):]
    assert '"convert --shell-menu remove"' in un.split("\n\n")[0]
    assert "Tasks:" not in un.split("\n\n")[0]          # removed whatever added it
    # the commands the installers run are the CLI's own
    from ovtsuite.cli import main
    assert main(["convert", "--shell-menu", "status"]) == 0
