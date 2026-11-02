# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Installing OVT Diag definition packs: from a folder, a .zip and a compiled
pack; validation before copying; replacing keeps the previous copy aside and
the loader never loads that copy; the CLI verbs."""
import os
import zipfile

import pytest

from ovtsuite.diag import defs as D
from ovtsuite.diag.defs import install as I

MANIFEST = "format: 1\nid: %s\nversion: %s\ntitle: t\npublisher: p\nlicense: CC-BY-SA-4.0\n"
FAULTS = ("format: 1\nkind: faults\nmatch: {addr: '01'}\n"
          "codes:\n- {id: %s.fault.v00001, vag: 1, text: %s}\n")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _pack(root, pid="p1", version="1.0.0", text="one", faults=FAULTS):
    _write(os.path.join(root, "pack.yaml"), MANIFEST % (pid, version))
    _write(os.path.join(root, "faults.yaml"), faults % (pid, text))
    return root


def _zip(src_dir, out, prefix=""):
    with zipfile.ZipFile(out, "w") as zf:
        for d, _, names in os.walk(src_dir):
            for n in names:
                p = os.path.join(d, n)
                zf.write(p, prefix + os.path.relpath(p, src_dir).replace(os.sep, "/"))
    return out


def test_install_from_folder_zip_and_compiled(tmp_path):
    user = str(tmp_path / "user")
    src = _pack(str(tmp_path / "src" / "p1"))
    pack, dest, warnings = D.install_pack(src, user)
    assert pack.id == "p1" and dest == os.path.join(user, "p1") and warnings == []
    assert os.path.isfile(os.path.join(dest, "faults.yaml"))
    # a zip whose files sit under one top folder (how a folder is zipped)
    z = _zip(_pack(str(tmp_path / "src" / "p2"), "p2"), str(tmp_path / "p2.zip"), "p2-1.0.0/")
    pack, dest, _ = D.install_pack(z, user)
    assert pack.id == "p2" and os.path.isfile(os.path.join(dest, "pack.yaml"))
    # a compiled pack on its own
    comp = D.compile_pack(_pack(str(tmp_path / "src" / "p3"), "p3"), str(tmp_path / "p3.json.gz"))
    pack, dest, _ = D.install_pack(comp, user)
    assert pack.id == "p3" and os.path.isfile(os.path.join(dest, "pack.json.gz"))
    loaded = D.load_packs([user], "user")
    assert [p.id for p in loaded] == ["p1", "p2", "p3"]
    assert [p.id for p in D.installed_user_packs(user)] == ["p1", "p2", "p3"]


def test_install_replaces_and_keeps_previous_unloaded(tmp_path):
    user = str(tmp_path / "user")
    D.install_pack(_pack(str(tmp_path / "a"), "p1", "1.0.0", "one"), user)
    pack, dest, _ = D.install_pack(_pack(str(tmp_path / "b"), "p1", "1.1.0", "two"), user)
    assert pack.version == "1.1.0"
    prev = os.path.join(user, "p1" + I.PREVIOUS_SUFFIX)
    assert os.path.isdir(prev) and D.load_pack(prev).version == "1.0.0"
    loaded = D.load_packs([user], "user")
    assert [(p.id, p.version) for p in loaded] == [("p1", "1.1.0")]
    res = D.Resolver(loaded)
    assert res.fault_text(D.Identity(addr="01", protocol="kwp2000"), vag=1) == "two"
    # a third install drops the older previous copy, keeps the newest
    D.install_pack(_pack(str(tmp_path / "c"), "p1", "1.2.0", "three"), user)
    assert D.load_pack(prev).version == "1.1.0"
    assert D.remove_pack("p1", user) and not os.path.exists(prev) and not os.path.exists(dest)
    assert not D.remove_pack("p1", user)


def test_install_refuses_invalid_and_unsafe(tmp_path):
    user = str(tmp_path / "user")
    bad = _pack(str(tmp_path / "bad"), "bad", faults="format: 1\nkind: faults\nmatch: {addr: '01'}\n"
                "codes:\n- {id: %s.fault.v00001, vag: 1, note: %s}\n")   # no text -> error
    with pytest.raises(D.InstallError) as ex:
        D.install_pack(bad, user)
    assert "does not validate" in str(ex.value) and not os.path.exists(os.path.join(user, "bad"))
    pack, _, _ = D.install_pack(bad, user, force=True)
    assert pack.id == "bad"
    with pytest.raises(D.InstallError):
        D.install_pack(str(tmp_path), user)                          # not a pack
    with pytest.raises(D.InstallError):
        D.install_pack(str(tmp_path / "missing.zip"), user)
    # a zip member escaping the folder is refused before extraction
    evil = str(tmp_path / "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("pack.yaml", MANIFEST % ("evil", "1.0.0"))
        zf.writestr("../escape.yaml", "x: 1\n")
    with pytest.raises(D.InstallError) as ex:
        D.install_pack(evil, user)
    assert "escapes" in str(ex.value)
    assert not os.path.exists(str(tmp_path / "escape.yaml"))


def test_install_plain_json_compiled_pack_is_gzipped(tmp_path):
    """A compiled pack handed over as plain ``.json`` lands as a real
    ``pack.json.gz`` (what the name says), and loads."""
    import gzip
    user = str(tmp_path / "user")
    comp = D.compile_pack(_pack(str(tmp_path / "src" / "pj"), "pj"), str(tmp_path / "pj.json"))
    assert comp.endswith(".json")
    with open(comp, "rb") as fh:
        assert fh.read(2) != b"\x1f\x8b"
    pack, dest, _ = D.install_pack(comp, user)
    installed = os.path.join(dest, "pack.json.gz")
    with open(installed, "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"
    with gzip.open(installed, "rt", encoding="utf-8") as fh:
        assert '"pj"' in fh.read()
    assert [p.id for p in D.load_packs([user], "user")] == ["pj"]
    assert D.Resolver(D.load_packs([user], "user")).fault_text(D.Identity(addr="01"), vag=1) == "one"


def test_install_refuses_a_downgrade_without_force(tmp_path):
    user = str(tmp_path / "user")
    D.install_pack(_pack(str(tmp_path / "a"), "p1", "1.10.0", "ten"), user)
    with pytest.raises(D.InstallError) as ex:
        D.install_pack(_pack(str(tmp_path / "b"), "p1", "1.9.0", "nine"), user)
    assert "older than the installed 1.10.0" in str(ex.value)
    assert D.load_pack(os.path.join(user, "p1")).version == "1.10.0"
    assert not os.path.exists(os.path.join(user, "p1" + I.PREVIOUS_SUFFIX))     # nothing set aside
    # the same version is a reinstall, a newer one an upgrade, --force a downgrade
    assert D.install_pack(_pack(str(tmp_path / "c"), "p1", "1.10.0", "ten again"), user)[0].version == "1.10.0"
    assert D.install_pack(_pack(str(tmp_path / "d"), "p1", "1.10.1", "patch"), user)[0].version == "1.10.1"
    assert D.install_pack(_pack(str(tmp_path / "e"), "p1", "1.9.0", "nine"), user, force=True)[0].version == "1.9.0"
    assert I.version_key("1.0.0-rc1") < I.version_key("1.0.0") < I.version_key("1.0.1") < I.version_key("1.10.0")


def test_install_permission_error_is_an_install_error(tmp_path, monkeypatch):
    user = str(tmp_path / "user")
    D.install_pack(_pack(str(tmp_path / "a"), "p1", "1.0.0", "one"), user)

    def locked(src, dst):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    monkeypatch.setattr(I.os, "replace", locked)
    with pytest.raises(D.InstallError) as ex:
        D.install_pack(_pack(str(tmp_path / "b"), "p1", "1.1.0", "two"), user)
    assert "open elsewhere" in str(ex.value)
    monkeypatch.undo()
    assert D.load_pack(os.path.join(user, "p1")).version == "1.0.0"           # still the installed one


def test_cli_install_list_remove(tmp_path, capsys, monkeypatch):
    pytest.importorskip("ovtsuite.convert.cli")          # the CLI's parser needs the whole tree
    from ovtsuite import cli
    user = str(tmp_path / "user")
    src = _pack(str(tmp_path / "src" / "p1"))
    assert cli.main(["diag-defs", "install", src, "-o", user]) == 0
    out = capsys.readouterr().out
    assert "installed p1 1.0.0" in out
    monkeypatch.setattr(D, "user_dir", lambda: user)
    monkeypatch.setattr("ovtsuite.diag.defs.load.user_dir", lambda: user)
    assert cli.main(["diag-defs", "list"]) == 0
    out = capsys.readouterr().out
    assert "vag-generic" in out and "p1" in out and user in out
    assert cli.main(["diag-defs", "remove", "p1", "-o", user]) == 0
    assert cli.main(["diag-defs", "remove", "p1", "-o", user]) == 1
    assert cli.main(["diag-defs", "install", str(tmp_path), "-o", user]) == 1
    assert "error:" in capsys.readouterr().out
