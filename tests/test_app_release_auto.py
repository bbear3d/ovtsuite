# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""tools/app_release.py auto: what build.ps1 runs after every one-folder
build -- the delta from the previous archived release, then this build's
manifest archived (docs/UPDATES.md, "The release archive")."""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import app_release  # noqa: E402
from ovtsuite import delta_update as du  # noqa: E402
from ovtsuite import updates as up  # noqa: E402


@pytest.fixture
def key(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from ovtsuite.modules import signing
    path = str(tmp_path / "k" / "key.pem")
    pub = signing.generate_key(path)
    monkeypatch.setenv(signing.ENV_KEY, path)
    return path, {"test": pub}


def _build(root, version, files, sign=True):
    """A built app folder with its manifest, like dist\\OVTSuite."""
    for rel, data in files.items():
        p = os.path.join(str(root), *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    args = ["manifest", str(root), "--version", version]
    if not sign:
        args.append("--unsigned")
    assert app_release.main(args) == 0
    return str(root)


def _archive(arch, version, files=None):
    """An archived release (only its manifest matters)."""
    d = os.path.join(arch, version)
    os.makedirs(d, exist_ok=True)
    man = du.AppManifest(version=version, files=files or {})
    with open(os.path.join(d, du.MANIFEST_NAME), "wb") as fh:
        fh.write(man.to_bytes())


def _deltas(out):
    return sorted(n for n in os.listdir(out) if du.parse_delta_name(n))


def _auto(dist, out, arch, *extra):
    return app_release.main(["auto", dist, "--out", out, "--archive", arch] + list(extra))


# ------------------------------------------------------------------ choosing
def test_the_previous_release_is_the_highest_older_one(tmp_path):
    arch = str(tmp_path / "AppReleases")
    for v in ("0.4.8", "0.4.9", "0.4.10", "0.5.0"):
        _archive(arch, v)
    # numeric: 0.4.10 is newer than 0.4.9
    assert app_release.archived_versions(arch) == ["0.4.8", "0.4.9", "0.4.10", "0.5.0"]
    assert app_release.previous_version(arch, "0.4.11") == "0.4.10"
    assert app_release.previous_version(arch, "0.4.10") == "0.4.9"     # never itself
    assert app_release.previous_version(arch, "0.4.9") == "0.4.8"      # nor newer ones
    assert app_release.previous_version(arch, "0.4.8") == ""
    assert app_release.previous_version(arch, "0.6") == "0.5.0"
    # a folder without a manifest, or not a version, is not a release
    os.makedirs(os.path.join(arch, "0.5.5"))
    os.makedirs(os.path.join(arch, "notes"))
    assert app_release.previous_version(arch, "0.6") == "0.5.0"
    assert app_release.previous_version(str(tmp_path / "nothing"), "0.6") == ""


def test_prereleases_order_like_the_app_and_are_never_a_base(tmp_path):
    arch = str(tmp_path / "AppReleases")
    for v in ("0.4.8", "0.4.9-rc1", "0.4.9"):
        _archive(arch, v)
    # the same order updates.compare_versions gives: rc1 before its release
    assert app_release.archived_versions(arch) == ["0.4.8", "0.4.9-rc1", "0.4.9"]
    assert up.compare_versions("0.4.9-rc1", "0.4.9") < 0
    # a release after its own prerelease: from the release before, which is
    # what everyone not testing the prerelease runs (the app's chains skip
    # prerelease releases)
    assert app_release.previous_version(arch, "0.4.10") == "0.4.9"
    os.remove(os.path.join(arch, "0.4.9", du.MANIFEST_NAME))
    assert app_release.previous_version(arch, "0.4.9") == "0.4.8"
    # a prerelease is built against the last release before it
    assert app_release.previous_version(arch, "0.4.9-rc2") == "0.4.8"
    assert app_release.previous_version(arch, "0.4.9-rc1") == "0.4.8"


def test_plan_auto(tmp_path, capsys):
    arch = str(tmp_path / "AppReleases")
    froms, note = app_release.plan_auto(arch, "0.4.8")
    assert froms == [] and "first release" in note
    _archive(arch, "0.4.8")
    _archive(arch, "0.4.9")
    assert app_release.plan_auto(arch, "0.4.9") == (["0.4.8"], "")
    assert app_release.plan_auto(arch, "0.4.10") == (["0.4.9"], "")
    # -DeltaFrom: an override, comma lists allowed, 'none' for no delta
    assert app_release.plan_auto(arch, "0.4.10", ["0.4.8,0.4.9"]) == (["0.4.8", "0.4.9"], "")
    assert app_release.plan_auto(arch, "0.4.10", ["none"]) == ([], "none asked for")
    # never V to V, never backwards
    froms, note = app_release.plan_auto(arch, "0.4.9", ["0.4.9"])
    assert froms == [] and "not older" in capsys.readouterr().out
    assert app_release.plan_auto(arch, "0.4.9", ["0.4.8", "0.4.10"])[0] == ["0.4.8"]


# ------------------------------------------------------------------ the whole step
V1 = {"OVTSuite.exe": b"exe 1", "OVTFlasher.exe": b"launcher", "_internal/a.dll": b"a"}
V2 = {"OVTSuite.exe": b"exe 2", "OVTFlasher.exe": b"launcher", "_internal/a.dll": b"a"}
V2B = {"OVTSuite.exe": b"exe 2b", "OVTFlasher.exe": b"launcher", "_internal/a.dll": b"a",
       "_internal/b.dll": b"b"}


def test_the_first_release_only_archives(tmp_path, key, capsys):
    arch, out = str(tmp_path / "AppReleases"), str(tmp_path / "dist")
    os.makedirs(out)
    d = _build(tmp_path / "b1", "0.4.8", V1)
    capsys.readouterr()
    assert _auto(d, out, arch) == 0
    said = capsys.readouterr().out
    assert "0.4.8 is the first release with an archived manifest" in said
    assert _deltas(out) == []
    assert os.path.isfile(os.path.join(arch, "0.4.8", du.MANIFEST_NAME))
    assert os.path.isfile(os.path.join(arch, "0.4.8", du.MANIFEST_SIG))


def test_the_next_release_gets_its_delta_and_a_rebuild_replaces_both(tmp_path, key, capsys):
    _key, keys = key
    arch, out = str(tmp_path / "AppReleases"), str(tmp_path / "dist")
    os.makedirs(out)
    assert _auto(_build(tmp_path / "b1", "0.4.8", V1), out, arch) == 0
    assert _auto(_build(tmp_path / "b2", "0.4.9", V2), out, arch) == 0
    name = "OVTSuite-Update-0.4.8-to-0.4.9.zip"
    assert _deltas(out) == [name]
    d = du.open_delta(os.path.join(out, name), keys)
    assert d.changed == ["OVTSuite.exe"] and d.deleted == []
    first = open(os.path.join(arch, "0.4.9", du.MANIFEST_NAME), "rb").read()

    # 0.4.9 built again (files changed, version not raised yet): the delta is
    # made again from 0.4.8 -- not from the 0.4.9 the last build archived --
    # and the archived 0.4.9 manifest is this build's
    capsys.readouterr()
    b2 = _build(tmp_path / "b2b", "0.4.9", V2B)
    assert _auto(b2, out, arch) == 0
    said = capsys.readouterr().out
    assert "removed an earlier build's %s" % name in said
    assert "replaced the archived OVT-Suite 0.4.9" in said
    assert _deltas(out) == [name]
    d = du.open_delta(os.path.join(out, name), keys)
    assert d.from_version == "0.4.8" and d.changed == ["OVTSuite.exe", "_internal/b.dll"]
    du.check_payload(d)
    now = open(os.path.join(arch, "0.4.9", du.MANIFEST_NAME), "rb").read()
    assert now != first and now == open(os.path.join(b2, du.MANIFEST_NAME), "rb").read()
    assert app_release.archived_versions(arch) == ["0.4.8", "0.4.9"]

    # 0.4.10 after that: from 0.4.9 (numerically the newest older one), and
    # 0.4.9's package from the last build is not left lying in dist
    assert _auto(_build(tmp_path / "b3", "0.4.10", V2), out, arch) == 0
    assert _deltas(out) == ["OVTSuite-Update-0.4.9-to-0.4.10.zip"]


def test_an_unsigned_build_is_not_a_release(tmp_path, key, capsys):
    arch, out = str(tmp_path / "AppReleases"), str(tmp_path / "dist")
    os.makedirs(out)
    _archive(arch, "0.4.8", {"OVTSuite.exe": "0" * 64})
    stale = os.path.join(out, "OVTSuite-Update-0.4.8-to-0.4.9.zip")
    open(stale, "wb").close()
    d = _build(tmp_path / "b", "0.4.9", V2, sign=False)
    assert _auto(d, out, arch, "--unsigned") == 0
    assert "none for an unsigned build" in capsys.readouterr().out
    assert _deltas(out) == []
    assert not os.path.exists(os.path.join(arch, "0.4.9"))


def test_delta_from_none_still_archives(tmp_path, key):
    arch, out = str(tmp_path / "AppReleases"), str(tmp_path / "dist")
    os.makedirs(out)
    assert _auto(_build(tmp_path / "b1", "0.4.8", V1), out, arch) == 0
    assert _auto(_build(tmp_path / "b2", "0.4.9", V2), out, arch, "--from", "none") == 0
    assert _deltas(out) == []
    assert app_release.archived_versions(arch) == ["0.4.8", "0.4.9"]
