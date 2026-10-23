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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux file managers")
def test_shell_menu_linux(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert shellmenu.installed() == []
    where = shellmenu.install()
    assert set(where) == {"GNOME Files", "Nemo", "Dolphin"}
    files = shellmenu._linux_files()
    script = open(files["GNOME Files"]).read()
    assert "--converter" in script and os.access(files["GNOME Files"], os.X_OK)
    nemo = open(files["Nemo"]).read()
    assert "Extensions=" in nemo and "a2l;" in nemo and "%F" in nemo
    assert "Exec=" in open(files["Dolphin"]).read()
    assert set(shellmenu.installed()) == set(where)
    from ovtsuite.cli import main
    assert main(["convert", "--shell-menu", "status"]) == 0
    assert "Installed in" in capsys.readouterr().out
    assert set(shellmenu.remove()) == set(where)
    assert shellmenu.installed() == [] and not any(os.path.exists(p) for p in files.values())


def test_shell_menu_command_and_windows_line():
    cmd = shellmenu.command()
    assert cmd[-1] == "--converter" or cmd[0].lower().endswith(("ovtconverter.exe", "ovtconverter"))
    line = shellmenu._win_cmdline(["C:\\Program Files\\OVT\\OVTConverter.exe"])
    assert line == '"C:\\Program Files\\OVT\\OVTConverter.exe" "%1"'
    assert all(k.endswith("\\shell\\" + shellmenu.VERB) for k in shellmenu._win_keys())
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
