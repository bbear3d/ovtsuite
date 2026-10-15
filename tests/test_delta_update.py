# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Delta updates (ovtsuite.delta_update, tools/app_release.py): the app
manifest, building and checking a delta package, chaining releases,
pre-flight, applying to a fake app folder with rollback, the helper's plan,
and the packaging side. No network, no Windows needed."""

import hashlib
import json
import os
import re
import sys
import zipfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ovtsuite import branding  # noqa: E402
from ovtsuite import delta_update as du  # noqa: E402
from ovtsuite import updates as up  # noqa: E402


# ------------------------------------------------------------------ fixtures
V1 = {"OVTSuite.exe": b"exe 1", "OVTFlasher.exe": b"flasher 1",
      "_internal/ovtsuite/a.pyc": b"a 1", "_internal/ovtsuite/b.pyc": b"b",
      "_internal/ovtsuite/old.pyc": b"old", "_internal/python313.dll": b"py"}
V2 = {"OVTSuite.exe": b"exe 2", "OVTFlasher.exe": b"flasher 2",
      "_internal/ovtsuite/a.pyc": b"a 2", "_internal/ovtsuite/b.pyc": b"b",
      "_internal/ovtsuite/new/c.pyc": b"c", "_internal/python313.dll": b"py"}
V3 = {"OVTSuite.exe": b"exe 3", "OVTFlasher.exe": b"flasher 3",
      "_internal/ovtsuite/a.pyc": b"a 3", "_internal/ovtsuite/b.pyc": b"b",
      "_internal/ovtsuite/old.pyc": b"old again", "_internal/python313.dll": b"py"}


def _lay(root, files, extras=True):
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    if extras:
        # never part of the manifest: the modules, the uninstaller, a crash log
        for rel, data in (("modules/edc16/manifest.json", b"{}"), ("unins000.exe", b"u"),
                          ("unins000.dat", b"d"), ("ovtsuite-crash.log", b"x")):
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
    return str(root)


def _tree(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = open(p, "rb").read()
    return out


@pytest.fixture
def keypair(tmp_path):
    pytest.importorskip("cryptography")
    from ovtsuite.modules import signing
    path = str(tmp_path / "k" / "key.pem")
    pub = signing.generate_key(path)
    return path, {"test": pub}


@pytest.fixture
def releases(tmp_path, keypair):
    """Three built versions with manifests, and the deltas 1->2, 2->3."""
    key, keys = keypair
    dists = {}
    for v, files in (("0.5.0", V1), ("0.5.1", V2), ("0.5.2", V3)):
        d = _lay(tmp_path / ("dist-" + v), files)
        du.write_manifest(d, v, key)
        dists[v] = d
    out = str(tmp_path / "out")
    d12, _ = du.build_delta(dists["0.5.0"], dists["0.5.1"], out, key)
    d23, _ = du.build_delta(dists["0.5.1"], dists["0.5.2"], out, key)
    return dists, d12, d23, keys, key


# ------------------------------------------------------------------ manifest
def test_manifest_lists_the_shipped_files_only(tmp_path, keypair):
    key, keys = keypair
    d = _lay(tmp_path / "dist", V1)
    man = du.write_manifest(d, "0.5.0", key)
    assert set(man.files) == set(V1)
    assert man.files["OVTSuite.exe"] == hashlib.sha256(b"exe 1").hexdigest()
    raw = open(os.path.join(d, du.MANIFEST_NAME), "rb").read()
    sig = open(os.path.join(d, du.MANIFEST_SIG), "rb").read()
    from ovtsuite.modules import signing
    assert signing.verify(raw, sig, keys)[0]
    # deterministic: an unchanged build writes the same bytes (installer keys)
    du.write_manifest(d, "0.5.0", key)
    assert open(os.path.join(d, du.MANIFEST_NAME), "rb").read() == raw
    # unsigned: the stale signature goes
    du.write_manifest(d, "0.5.0", None)
    assert not os.path.exists(os.path.join(d, du.MANIFEST_SIG))
    m2, raw2, sig2 = du.read_manifest(d)
    assert m2.files == man.files and sig2 is None and m2.app == branding.APP_NAME


def test_safe_paths():
    for ok in ("OVTSuite.exe", "_internal/x/y.pyc", "a.log.d/x"):
        assert du.safe_rel(ok), ok
    for bad in ("", "/abs", "../x", "a/../b", "a\\b", "C:/x", "modules/edc16/x",
                "Modules/x", du.MANIFEST_NAME, du.BACKUP_DIR + "/x", "unins000.exe",
                "crash.log", "a//b"):
        assert not du.safe_rel(bad), bad


def test_the_release_keys_are_the_base_apps():
    from ovtsuite.modules import signing
    assert du.base_keys() and du.base_keys() == signing.trusted_keys()


def test_delta_names():
    n = du.delta_name("0.4.8", "0.4.10")
    assert n == "OVTSuite-Update-0.4.8-to-0.4.10.zip"
    assert du.parse_delta_name(n) == ("0.4.8", "0.4.10")
    assert du.parse_delta_name("OVTSuite-0.4.8-setup.exe") is None


# ------------------------------------------------------------------ building / opening
def test_delta_holds_only_what_changed(releases):
    dists, d12, _d23, keys, _key = releases
    with zipfile.ZipFile(d12) as z:
        names = set(z.namelist())
        info = json.loads(z.read(du.DELTA_JSON))
    assert info["from"] == "0.5.0" and info["to"] == "0.5.1"
    assert info["changed"] == sorted(["OVTSuite.exe", "OVTFlasher.exe",
                                      "_internal/ovtsuite/a.pyc",
                                      "_internal/ovtsuite/new/c.pyc"])
    assert info["deleted"] == ["_internal/ovtsuite/old.pyc"]
    assert info["from_files"]["_internal/ovtsuite/new/c.pyc"] is None
    assert info["from_files"]["OVTSuite.exe"] == hashlib.sha256(b"exe 1").hexdigest()
    assert set(info["to_manifest"]["files"]) == set(V2)
    assert {du.PAYLOAD + r for r in info["changed"]} <= names
    assert du.PAYLOAD + "_internal/ovtsuite/b.pyc" not in names        # unchanged
    assert not any("modules/" in n or "unins" in n for n in names)
    d = du.open_delta(d12, keys)
    du.check_payload(d)
    assert d.from_version == "0.5.0" and d.to_version == "0.5.1" and d.signed_by == "test"


def test_a_stale_manifest_fails_the_build(tmp_path, keypair):
    key, _keys = keypair
    a = _lay(tmp_path / "a", V1)
    b = _lay(tmp_path / "b", V2)
    du.write_manifest(a, "0.5.0", key)
    du.write_manifest(b, "0.5.1", key)
    with open(os.path.join(b, "OVTSuite.exe"), "wb") as fh:
        fh.write(b"changed after the manifest")
    with pytest.raises(du.DeltaError, match="manifest"):
        du.build_delta(a, b, str(tmp_path / "out"), key)
    assert not os.listdir(str(tmp_path / "out"))


def _rezip(src, dst, change):
    with zipfile.ZipFile(src) as z:
        items = {n: z.read(n) for n in z.namelist()}
    change(items)
    with zipfile.ZipFile(dst, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    return dst


def test_tampered_or_unsigned_deltas_are_refused(releases, tmp_path):
    _dists, d12, _d23, keys, _key = releases
    t = str(tmp_path / "t.zip")

    def edit_json(items):
        j = json.loads(items[du.DELTA_JSON])
        j["deleted"] = []
        items[du.DELTA_JSON] = json.dumps(j).encode()
    with pytest.raises(du.DeltaError, match="not verifiable"):
        du.open_delta(_rezip(d12, t, edit_json), keys)
    with pytest.raises(du.DeltaError, match="not signed"):
        du.open_delta(_rezip(d12, t, lambda it: it.pop(du.DELTA_SIG)), keys)
    with pytest.raises(du.DeltaError, match="not verifiable"):
        du.open_delta(d12, {"other": "00" * 32})

    def extra(items):
        items[du.PAYLOAD + "evil.dll"] = b"x"
    with pytest.raises(du.DeltaError, match="contents"):
        du.open_delta(_rezip(d12, t, extra), keys)

    def damage(items):
        items[du.PAYLOAD + "OVTSuite.exe"] = b"not the signed one"
    d = du.open_delta(_rezip(d12, t, damage), keys)       # the list is still signed
    with pytest.raises(du.DeltaError, match="damaged"):
        du.check_payload(d)

    def manifest(items):
        items[du.MANIFEST_NAME] = items[du.MANIFEST_NAME] + b" "
    with pytest.raises(du.DeltaError, match="manifest"):
        du.open_delta(_rezip(d12, t, manifest), keys)
    with open(t, "wb") as fh:
        fh.write(b"not a zip")
    with pytest.raises(du.DeltaError):
        du.open_delta(t, keys)


def test_a_signed_delta_naming_a_path_outside_the_app_is_refused(tmp_path, keypair):
    key, keys = keypair
    from ovtsuite.modules import signing
    info = {"format": du.FORMAT, "app": branding.APP_NAME, "from": "1", "to": "2",
            "from_files": {"../evil.exe": None}, "changed": ["../evil.exe"],
            "deleted": [], "to_manifest": {"version": "2", "files": {"../evil.exe": "0"}},
            "manifest_sha256": "0"}
    raw = json.dumps(info).encode()
    p = str(tmp_path / "x.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(du.DELTA_JSON, raw)
        z.writestr(du.DELTA_SIG, signing.sign(raw, key))
        z.writestr(du.PAYLOAD + "../evil.exe", b"x")
    with pytest.raises(du.DeltaError, match="outside"):
        du.open_delta(p, keys)


# ------------------------------------------------------------------ chains
def _rel(version, *assets):
    return up.Release.from_json({
        "tag_name": "v" + version, "name": version, "body": "", "html_url": "",
        "assets": [{"name": n, "size": s, "browser_download_url": "https://x.invalid/" + n}
                   for n, s in assets]})


def test_find_chain_takes_the_fewest_steps():
    rels = [
        _rel("0.5.3", ("OVTSuite-Update-0.5.2-to-0.5.3.zip", 30),
             ("OVTSuite-Update-0.5.0-to-0.5.3.zip", 500)),
        _rel("0.5.2", ("OVTSuite-Update-0.5.1-to-0.5.2.zip", 20)),
        _rel("0.5.1", ("OVTSuite-Update-0.5.0-to-0.5.1.zip", 10)),
        _rel("0.5.0"),
    ]
    c = du.find_chain(rels, "0.5.0", "0.5.3")
    assert [(h.from_version, h.to_version) for h in c.hops] == [("0.5.0", "0.5.3")]
    assert c.size == 500 and c.steps == 1
    # without the direct one: three steps, in order
    rels[0].assets = rels[0].assets[:1]
    c = du.find_chain(rels, "0.5.0", "0.5.3")
    assert [h.to_version for h in c.hops] == ["0.5.1", "0.5.2", "0.5.3"]
    assert c.size == 60 and c.hops[0].release.version == "0.5.1"
    # from the middle
    assert [h.to_version for h in du.find_chain(rels, "0.5.1", "0.5.3").hops] == ["0.5.2", "0.5.3"]
    # a gap: no chain
    rels[1].assets = []
    assert du.find_chain(rels, "0.5.0", "0.5.3") is None
    assert du.find_chain(rels, "0.5.3", "0.5.3") is None


def test_find_chain_ignores_misplaced_and_prerelease_deltas():
    pre = _rel("0.5.1", ("OVTSuite-Update-0.5.0-to-0.5.1.zip", 10))
    pre.prerelease = True
    # a delta to another version than its release is not trusted to be that version
    odd = _rel("0.5.1", ("OVTSuite-Update-0.5.0-to-0.5.9.zip", 10))
    assert du.find_chain([pre], "0.5.0", "0.5.1") is None
    assert du.find_chain([odd], "0.5.0", "0.5.1") is None
    # backwards "updates" are not edges
    back = _rel("0.5.1", ("OVTSuite-Update-0.5.2-to-0.5.1.zip", 10))
    assert du.delta_hops([back]) == []


def test_choose_the_delta_or_the_installer():
    c = du.Chain([du.Hop(None, up.Asset("x", 10, ""), "1", "2")])
    assert du.choose(c, 100, True) is c
    assert du.choose(c, 100, False) is None                 # all users: the installer
    assert du.choose(c, 5, True) is None                    # bigger than the installer
    assert du.choose(c, 0, True) is c                       # no installer to compare
    assert du.choose(c, 100, True, failed_version="2", target="2") is None
    assert du.choose(c, 100, True, failed_version="1.9", target="2") is c
    assert du.choose(None, 100, True) is None


def test_fetch_releases_pages_and_stops_early():
    pages = {1: [{"tag_name": "v0.5.%d" % i, "assets": []} for i in range(200, 100, -1)],
             2: [{"tag_name": "v0.5.%d" % i, "assets": []} for i in range(100, 0, -1)],
             3: []}
    asked = []

    def get(url):
        m = re.search(r"per_page=100&page=(\d+)$", url)
        asked.append(int(m.group(1)))
        return pages[int(m.group(1))]
    rels = up.fetch_releases(get=get)
    assert len(rels) == 200 and asked == [1, 2, 3]
    asked.clear()
    # the installed version is on page 1: older pages are not needed
    up.fetch_releases(older_than="0.5.150", get=get)
    assert asked == [1]
    with pytest.raises(up.UpdateError):
        up.fetch_releases(get=lambda u: {"message": "rate limited"})
    assert up.releases_url(2).endswith("/releases?per_page=100&page=2")


# ------------------------------------------------------------------ compose / preflight
def test_compose_folds_a_chain(releases):
    dists, d12, d23, keys, _key = releases
    deltas = [du.open_delta(d12, keys), du.open_delta(d23, keys)]
    c = du.compose(deltas, "0.5.0")
    assert c.start == "0.5.0" and c.end == "0.5.2"
    # c.pyc came in 0.5.1 and went in 0.5.2; old.pyc went and came back
    assert "_internal/ovtsuite/new/c.pyc" in c.deletes
    assert c.writes["_internal/ovtsuite/old.pyc"] == 1
    assert c.writes["OVTSuite.exe"] == 1 and c.writes["OVTFlasher.exe"] == 1
    assert c.expect["OVTSuite.exe"] == hashlib.sha256(b"exe 1").hexdigest()
    assert c.final_files == du.read_manifest(dists["0.5.2"])[0].files
    with pytest.raises(du.DeltaError, match="starts at"):
        du.compose(deltas, "0.4.9")
    with pytest.raises(du.DeltaError, match="follow on"):
        du.compose([deltas[1], deltas[0]], "0.5.1")


def test_compose_catches_a_chain_that_does_not_fit(releases, tmp_path, keypair):
    """A delta built from another build of 0.5.1 than the one the step
    before installs: its from_files are not what that step leaves."""
    dists, d12, _d23, keys, key = releases
    other = _lay(tmp_path / "other-0.5.1", dict(V2, **{"_internal/ovtsuite/a.pyc": b"rebuilt"}))
    du.write_manifest(other, "0.5.1", key)
    bad, _ = du.build_delta(other, dists["0.5.2"], str(tmp_path / "o2"), key)
    with pytest.raises(du.DeltaError, match="a.pyc"):
        du.compose([du.open_delta(d12, keys), du.open_delta(bad, keys)], "0.5.0")


def test_preflight_checks_the_installed_files(releases, tmp_path):
    dists, d12, d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    comp = du.preflight(app, [d12, d23], "0.5.0", keys)
    assert comp.end == "0.5.2"
    # a file the update replaces was changed by hand: the full installer instead
    with open(os.path.join(app, "_internal", "ovtsuite", "a.pyc"), "wb") as fh:
        fh.write(b"patched")
    with pytest.raises(du.DeltaError, match="a.pyc"):
        du.preflight(app, [d12, d23], "0.5.0", keys)
    # one it does not touch may be anything
    app2 = _lay(tmp_path / "app2", dict(V1, **{"_internal/ovtsuite/b.pyc": b"whatever"}))
    du.preflight(app2, [d12], "0.5.0", keys)
    os.remove(os.path.join(app2, "OVTSuite.exe"))
    with pytest.raises(du.DeltaError, match="missing"):
        du.preflight(app2, [d12], "0.5.0", keys)


# ------------------------------------------------------------------ apply
def test_apply_a_chain_to_an_app_folder(releases, tmp_path):
    dists, d12, d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    du.write_manifest(app, "0.5.0", None)
    before = _tree(app)
    comp = du.preflight(app, [d12, d23], "0.5.0", keys)
    seen = []
    du.apply(app, comp, registry=lambda v: seen.append(v))
    after = _tree(app)
    for rel, data in V3.items():
        assert after[rel] == data, rel
    assert "_internal/ovtsuite/new/c.pyc" not in after
    assert not os.path.isdir(os.path.join(app, "_internal", "ovtsuite", "new"))
    # what the update never touches is left alone
    for rel in ("modules/edc16/manifest.json", "unins000.exe", "unins000.dat"):
        assert after[rel] == before[rel]
    # the new manifest, signed, and nothing left behind
    assert du.read_manifest(app)[0].version == "0.5.2"
    assert du.read_manifest(app)[2] is not None
    assert not os.path.exists(os.path.join(app, du.BACKUP_DIR))
    assert not os.path.exists(os.path.join(app, du.STAGE_DIR))
    assert seen == ["0.5.2"]


def test_a_failed_apply_puts_everything_back(releases, tmp_path, monkeypatch):
    """Every file move of the apply fails in turn: each time the folder ends
    up exactly as it was."""
    dists, d12, d23, keys, _key = releases
    real = os.replace
    n = {"i": 0, "fail": 0}

    def flaky(a, b):
        n["i"] += 1
        if n["i"] == n["fail"]:
            raise OSError("disk full")
        return real(a, b)
    monkeypatch.setattr(du.os, "replace", flaky)
    app = _lay(tmp_path / "count", V1)
    du.apply(app, du.preflight(app, [d12, d23], "0.5.0", keys))
    moves = n["i"]
    assert moves >= 10
    for fail in range(1, moves + 1):
        app = _lay(tmp_path / ("app%d" % fail), V1)
        du.write_manifest(app, "0.5.0", None)
        before = _tree(app)
        comp = du.preflight(app, [d12, d23], "0.5.0", keys)
        n.update(i=0, fail=fail)
        with pytest.raises(du.DeltaError, match="restored|nothing was changed"):
            du.apply(app, comp)
        after = _tree(app)
        leftovers = {k for k in after if k.startswith((du.BACKUP_DIR, du.STAGE_DIR))}
        assert {k: v for k, v in after.items() if k not in leftovers} == before, fail
        assert not os.path.exists(os.path.join(app, du.STAGE_DIR))


def test_a_failed_registry_update_rolls_back_too(releases, tmp_path):
    dists, d12, _d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    before = _tree(app)
    comp = du.preflight(app, [d12], "0.5.0", keys)

    def registry(v):
        raise OSError("access denied")
    with pytest.raises(du.DeltaError, match="restored"):
        du.apply(app, comp, registry=registry)
    assert {k: v for k, v in _tree(app).items() if not k.startswith(du.BACKUP_DIR)} == before


def test_apply_retries_a_file_in_use(releases, tmp_path, monkeypatch):
    dists, d12, _d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    comp = du.preflight(app, [d12], "0.5.0", keys)
    real = os.replace
    busy = {"n": 2}

    def scanned(a, b):
        if busy["n"]:
            busy["n"] -= 1
            raise PermissionError(32, "in use")
        return real(a, b)
    monkeypatch.setattr(du.os, "replace", scanned)
    monkeypatch.setattr(du.time, "sleep", lambda s: None)
    du.apply(app, comp)
    assert _tree(app)["OVTSuite.exe"] == b"exe 2"


def test_apply_refuses_files_changed_since_the_preflight(releases, tmp_path):
    dists, d12, _d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    comp = du.preflight(app, [d12], "0.5.0", keys)
    with open(os.path.join(app, "OVTSuite.exe"), "wb") as fh:
        fh.write(b"something else")
    with pytest.raises(du.DeltaError, match="changed"):
        du.apply(app, comp)
    assert _tree(app)["OVTSuite.exe"] == b"something else"


# ------------------------------------------------------------------ the helper
def test_runner_links_what_stays_and_copies_what_changes(releases, tmp_path):
    dists, d12, _d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    du.write_manifest(app, "0.5.0", None)
    comp = du.preflight(app, [d12], "0.5.0", keys)
    dest = str(tmp_path / "runner")
    du.prepare_runner(app, dest, comp.touched | {"OVTSuite.exe"})
    got = _tree(dest)
    assert set(got) == set(V1)                  # no modules, uninstaller, manifest, log
    same = os.stat(os.path.join(app, "_internal", "python313.dll")).st_ino
    assert os.stat(os.path.join(dest, "_internal", "python313.dll")).st_ino == same
    for rel in ("OVTSuite.exe", "_internal/ovtsuite/a.pyc"):
        a = os.path.join(app, *rel.split("/"))
        b = os.path.join(dest, *rel.split("/"))
        assert os.stat(a).st_ino != os.stat(b).st_ino and open(b, "rb").read() == V1[rel]


def test_helper_applies_a_plan_and_reports(releases, tmp_path, monkeypatch):
    dists, d12, d23, keys, _key = releases
    monkeypatch.setattr(du, "base_keys", lambda: keys)
    started = []
    monkeypatch.setattr(du, "_detached", lambda cmd, cwd: started.append(cmd))
    app = _lay(tmp_path / "app", V1)
    folder = str(tmp_path / "dl")
    os.makedirs(folder)
    res = str(tmp_path / "cfg" / du.RESULT_NAME)
    relaunch = os.path.join(app, "OVTSuite.exe")
    plan = du.write_plan(folder, app, relaunch, 0, [d12, d23], "0.5.0", "0.5.2",
                         result=res)
    assert du.helper_main([plan]) == 0
    assert _tree(app)["OVTSuite.exe"] == b"exe 3"
    r = du.pop_result(res)
    assert r["ok"] and "0.5.2" in r["message"] and not os.path.exists(res)
    assert started == [[relaunch]]
    assert "OK:" in open(os.path.join(folder, "delta-update.log")).read()
    # run again: the files are no longer 0.5.0's -- nothing changes, it says why
    started.clear()
    assert du.helper_main([plan]) == 1
    r = du.pop_result(res)
    assert not r["ok"] and "OVTSuite.exe" in r["message"]
    assert _tree(app)["OVTSuite.exe"] == b"exe 3"
    assert started == [[relaunch]]              # the app comes back either way
    # module installers to run after: their batch file starts the app instead
    app2 = _lay(tmp_path / "app2", V1)
    plan2 = du.write_plan(folder, app2, relaunch, 0, [d12], "0.5.0", "0.5.1",
                          then=os.path.join(folder, "install-update.cmd"), result=res)
    started.clear()
    assert du.helper_main([plan2]) == 0
    assert started[0][-1].endswith("install-update.cmd") and started[0][1:3] == ["/d", "/c"]
    assert du.helper_main([]) == 2 and du.helper_main([str(tmp_path / "none.json")]) == 2


def test_run_plan_checks_where_the_chain_ends(releases, tmp_path):
    dists, d12, _d23, keys, _key = releases
    app = _lay(tmp_path / "app", V1)
    plan = {"app_dir": app, "deltas": [d12], "from": "0.5.0", "to": "0.5.2", "pid": 0}
    r = du.run_plan(plan, lambda s: None, keys=keys, wait=False)
    assert not r["ok"] and "0.5.1" in r["message"]
    assert _tree(app)["OVTSuite.exe"] == b"exe 1"


def test_cleanup_after_update(tmp_path, monkeypatch):
    app = str(tmp_path / "app")
    for d in (du.BACKUP_DIR, du.STAGE_DIR):
        os.makedirs(os.path.join(app, d, "x"))
    tmp = tmp_path / "tmp"
    os.makedirs(tmp / (du.RUNNER_PREFIX + "0.5.2") / "_internal")
    os.makedirs(tmp / "keep-me")
    monkeypatch.setattr(du, "runner_root", lambda: str(tmp))
    du.cleanup_after_update(app)
    assert os.listdir(app) == [] and os.listdir(tmp) == ["keep-me"]
    assert du.pop_result(str(tmp_path / "missing.json")) is None


class _FakeWinreg:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_SET_VALUE = 1, 2
    REG_SZ, REG_DWORD = 1, 4

    def __init__(self, keys):
        self.keys = keys            # {(root, path): {name: (value, type)}}
        self.fail_on = None

    class _K:
        def __init__(self, vals):
            self.vals = vals

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, res=0, access=0):
        if (root, path) not in self.keys:
            raise OSError("no key")
        return self._K(self.keys[(root, path)])

    def QueryValueEx(self, k, name):
        if name not in k.vals:
            raise OSError("no value")
        return k.vals[name]

    def SetValueEx(self, k, name, _res, typ, val):
        if name == self.fail_on:
            raise OSError("denied")
        k.vals[name] = (val, typ)

    def DeleteValue(self, k, name):
        del k.vals[name]


def test_uninstall_entry_shows_the_new_version(tmp_path):
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % up.INNO_APP_ID
    app = str(tmp_path / "OVT-Suite")
    vals = {"InstallLocation": (app + "\\", 1), "DisplayVersion": ("0.5.0", 1),
            "DisplayName": ("OVT-Suite 0.5.0", 1), "VersionMajor": (0, 4),
            "VersionMinor": (5, 4)}
    wr = _FakeWinreg({("HKCU", key): dict(vals)})
    undo = du.update_uninstall_entry(app, "0.6.1", winreg=wr)
    got = wr.keys[("HKCU", key)]
    assert got["DisplayVersion"][0] == "0.6.1"
    assert got["DisplayName"][0] == "%s 0.6.1" % branding.APP_NAME
    assert got["VersionMajor"][0] == 0 and got["VersionMinor"][0] == 6
    undo()
    assert wr.keys[("HKCU", key)] == vals
    # another folder's entry is not this install's
    assert du.update_uninstall_entry(str(tmp_path / "elsewhere"), "0.6.1", winreg=wr) is None
    # a failure half-way puts back what was written
    wr.fail_on = "DisplayName"
    with pytest.raises(OSError):
        du.update_uninstall_entry(app, "0.6.1", winreg=wr)
    assert wr.keys[("HKCU", key)] == vals
    assert du.update_uninstall_entry(app, "0.6.1", winreg=_FakeWinreg({})) is None


# ------------------------------------------------------------------ install kinds
def test_a_tool_install_is_not_the_apps_install(tmp_path):
    app = tmp_path / "OVT-Suite"
    tools = tmp_path / "OVT Flasher"
    for d, exe in ((app, "OVTSuite.exe"), (tools, "OVTFlasher.exe")):
        os.makedirs(d)
        for n in (exe, "unins000.exe"):
            (d / n).write_bytes(b"x")
    (app / du.MANIFEST_NAME).write_text("{}")
    assert up.classify_install(str(app)) == "inno"
    # OVT Flasher / OVT Tools: an uninstaller but no app manifest (it has
    # OVTSuite.exe too since the launchers start it) -- the app's setup would
    # install the whole app somewhere else
    (tools / "OVTSuite.exe").write_bytes(b"x")
    assert up.classify_install(str(tools)) == "tools"
    assert up.classify_install(str(tools), registered=True) == "inno"
    assert up.classify_install(str(tmp_path)) == "frozen"


# ------------------------------------------------------------------ the build tool
def test_app_release_tool(tmp_path, keypair, monkeypatch, capsys):
    import app_release
    key, keys = keypair
    monkeypatch.setenv("OVT_SIGNING_KEY", key)
    arch = str(tmp_path / "AppReleases")
    a = _lay(tmp_path / "a", V1)
    b = _lay(tmp_path / "b", V2)
    assert app_release.main(["manifest", a, "--version", "0.5.0"]) == 0
    assert app_release.main(["archive", a, "--to", arch]) == 0
    assert os.path.isfile(os.path.join(arch, "0.5.0", du.MANIFEST_NAME))
    assert os.path.isfile(os.path.join(arch, "0.5.0", du.MANIFEST_SIG))
    assert app_release.main(["manifest", b, "--version", "0.5.1"]) == 0
    out = str(tmp_path / "dist")
    # no --from: the newest archived release older than the build
    assert app_release.main(["delta", "--to", b, "--out", out, "--archive", arch]) == 0
    made = os.path.join(out, "OVTSuite-Update-0.5.0-to-0.5.1.zip")
    du.check_payload(du.open_delta(made, keys))
    capsys.readouterr()
    assert app_release.main(["previous", "--version", "0.5.1", "--archive", arch]) == 0
    assert capsys.readouterr().out.strip() == "0.5.0"
    assert app_release.main(["previous", "--version", "0.5.0", "--archive", arch]) == 0
    assert capsys.readouterr().out.strip() == ""
    # unsigned: no delta at all
    os.remove(made)
    assert app_release.main(["delta", "--to", b, "--out", out, "--archive", arch,
                             "--unsigned"]) == 0
    assert not os.path.exists(made)
    assert app_release.main(["delta", "--to", b, "--out", out, "--from", "none"]) == 0
    with pytest.raises(SystemExit):
        app_release.main(["delta", "--to", b, "--out", out, "--archive", arch,
                          "--from", "0.4.0"])
    # the archive sorts numerically
    os.makedirs(os.path.join(arch, "0.5.10"))
    with open(os.path.join(arch, "0.5.10", du.MANIFEST_NAME), "w") as fh:
        fh.write("{}")
    assert app_release.previous_version(arch, "0.5.11") == "0.5.10"
    assert app_release.previous_version(arch, "0.5.9") == "0.5.0"


# ------------------------------------------------------------------ packaging
def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_the_exes_share_one_copy_of_the_code():
    spec = _read("packaging", "ovtsuite.spec")
    # the code is in OVTSuite.exe's archive, once; the tool exes are launchers
    # built outside PyInstaller (tests/test_launcher.py)
    assert "noarchive=False," in spec and "_tool_exes" not in spec
    assert spec.count("Analysis(") == 1 and spec.count("COLLECT(") == 1


def test_the_helper_runs_before_anything_else_in_the_exe():
    entry = _read("packaging", "entry.py")
    main = entry[entry.index("def main():"):]
    apply_at = main.index('"--apply-update"')
    assert apply_at < main.index("_hold_app_mutex()")       # never holds the mutex
    assert apply_at < main.index("_enable_fault_log()")
    assert du.APPLY_ARG == "--apply-update"


def test_installers_clean_up_after_delta_updates():
    iss = _read("packaging", "installer.iss")
    inst = iss[iss.index("[InstallDelete]"):iss.index("[UninstallDelete]")]
    assert 'Name: "{app}\\_internal"' in inst
    assert not [ln for ln in inst.splitlines() if ln.startswith("Type:") and "modules" in ln]
    unin = iss[iss.index("[UninstallDelete]"):]
    for name in ("{app}\\_internal", "{app}\\" + du.BACKUP_DIR, "{app}\\" + du.STAGE_DIR,
                 "{app}\\" + du.MANIFEST_NAME):
        assert 'Name: "%s"' % name in unin, name
    tools = _read("packaging", "tool_installer.iss")
    assert ",\\app-manifest.json,\\app-manifest.json.sig" in tools


def test_build_script_makes_and_publishes_the_deltas():
    ps = _read("build.ps1")
    assert "[string[]]$DeltaFrom" in ps
    assert ps.index("Write-AppManifest\n") < ps.index("# 6. Installer (Inno Setup)")
    assert "$SumFiles = @($InstallersBuilt) + @($DeltasBuilt)" in ps
    # automatic: app_release.py auto picks the previous release and archives
    assert "$dArgs = @('auto', " in ps and "[switch]$NoDelta" in ps
    assert '-Filter "OVTSuite-Update-*-to-$AppVersion.zip"' in ps
    # the manifest is written again after Authenticode changed the exes
    sign = ps[ps.index("'Authenticode signing'"):ps.index("$SignDefs = @('/DSignTool")]
    assert "Write-AppManifest" in sign


# ------------------------------------------------------------------ the window side
def _qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:
        pytest.skip("PySide6 is not usable here: %s" % exc)
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _offer(chain, base_size=50_000_000):
    rel = up.Release.from_json({
        "tag_name": "v0.5.2", "name": "0.5.2", "body": "* new", "html_url": "",
        "assets": [{"name": "OVTSuite-Base-0.5.2-setup.exe", "size": base_size,
                    "browser_download_url": "https://x.invalid/base"}]})
    off = up.make_offer(rel, "0.5.0", [])
    off.delta = chain
    return off


def _chain(d12, d23):
    rels = [_rel("0.5.2", ("OVTSuite-Update-0.5.1-to-0.5.2.zip", os.path.getsize(d23))),
            _rel("0.5.1", ("OVTSuite-Update-0.5.0-to-0.5.1.zip", os.path.getsize(d12)))]
    return du.find_chain(rels, "0.5.0", "0.5.2")


@pytest.mark.slow
def test_dialog_offers_the_update_packages(releases, monkeypatch, isolated_qsettings):
    _qt()
    from ovtsuite import gui_update
    _dists, d12, d23, _keys, _key = releases
    off = _offer(_chain(d12, d23))
    monkeypatch.setattr(up, "install_mode", lambda d: up.CURRENT_USER)
    dlg = gui_update.UpdateDialog(off, "inno")
    assert dlg.delta is off.delta and dlg.can_install()
    by = dlg.rows()[0][3]
    assert by.startswith("update packages: 2 steps, ") and by.endswith(" MB")
    assert "2 update packages" in dlg.note.text() and "instead of the" in dlg.note.text()
    # an all-users install, a release whose delta failed here: the installer
    monkeypatch.setattr(up, "install_mode", lambda d: up.ALL_USERS)
    assert gui_update.UpdateDialog(off, "inno").rows()[0][3] == "app installer"
    monkeypatch.setattr(up, "install_mode", lambda d: up.CURRENT_USER)
    gui_update._settings().setValue(up.DELTA_FAILED_KEY, "0.5.2")
    assert gui_update.UpdateDialog(off, "inno").delta is None
    # a standalone tool's install is told to use its own setup
    t = gui_update.UpdateDialog(off, "tools")
    assert not t.can_install() and "tool" in t.note.text()


def _drive_install(monkeypatch, gui_update, app_dir, files, tmp_path):
    """Fake the downloads, the questions and the closing for
    gui_update.install; returns what it launches and downloads."""
    from PySide6 import QtWidgets
    launched = {}
    calls = []

    def fake_download(parent, offer, assets, owners=None, strict=False, quiet=False):
        calls.append([a.name for a in assets])
        return [(a, files[a.name], "signed checksum list") for a in assets]
    monkeypatch.setattr(gui_update, "_download", fake_download)
    monkeypatch.setattr(gui_update, "_app_dir", lambda: app_dir)
    monkeypatch.setattr(up, "install_mode", lambda d: up.CURRENT_USER)
    monkeypatch.setattr(up, "download_dir", lambda v: str(tmp_path / "dl"))
    monkeypatch.setattr(up, "other_instances", lambda: [])
    monkeypatch.setattr(gui_update, "_close_everything", lambda: True)
    monkeypatch.setattr(du, "runner_root", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(du, "launch", lambda cmd: launched.setdefault("helper", cmd))
    monkeypatch.setattr(up, "launch_chain",
                        lambda inst, folder: launched.setdefault("installers", inst))
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton", lambda self: [
        b for b in self.buttons()
        if self.buttonRole(b) == QtWidgets.QMessageBox.ButtonRole.AcceptRole][0])
    monkeypatch.setattr(QtWidgets.QApplication.instance(), "quit", lambda: None)
    return launched, calls


FILES = ("OVTSuite-Update-0.5.0-to-0.5.1.zip", "OVTSuite-Update-0.5.1-to-0.5.2.zip",
         "OVTSuite-Base-0.5.2-setup.exe")


@pytest.mark.slow
def test_install_with_update_packages(releases, tmp_path, monkeypatch, isolated_qsettings):
    app = _qt()
    from ovtsuite import gui_update
    _dists, d12, d23, keys, _key = releases
    monkeypatch.setattr(du, "base_keys", lambda: keys)
    monkeypatch.setattr(branding, "__version__", "0.5.0")
    app_dir = _lay(tmp_path / "app", V1)
    files = dict(zip(FILES, (d12, d23, str(tmp_path / "setup.exe"))))
    launched, calls = _drive_install(monkeypatch, gui_update, app_dir, files, tmp_path)
    q = app.quitOnLastWindowClosed()
    try:
        assert gui_update.install(None, _offer(_chain(d12, d23))) is True
    finally:
        app.setQuitOnLastWindowClosed(q)
    # only the packages were fetched; the helper runs from its own copy
    assert calls == [list(FILES[:2])]
    cmd = launched["helper"]
    assert cmd[0] == os.path.join(str(tmp_path / "tmp"), du.RUNNER_PREFIX + "0.5.2",
                                  "OVTSuite.exe")
    assert cmd[1] == du.APPLY_ARG and os.path.isfile(cmd[0])
    plan = json.load(open(cmd[2]))
    assert plan["from"] == "0.5.0" and plan["to"] == "0.5.2"
    assert plan["deltas"] == [d12, d23] and plan["app_dir"] == os.path.abspath(app_dir)
    # nothing changed yet: the helper does that once the app has gone
    assert _tree(app_dir)["OVTSuite.exe"] == b"exe 1"
    r = du.run_plan(dict(plan, pid=0), lambda s: None, keys=keys, wait=False)
    assert r["ok"] and _tree(app_dir)["OVTSuite.exe"] == b"exe 3"


@pytest.mark.slow
def test_install_falls_back_to_the_installer(releases, tmp_path, monkeypatch,
                                             isolated_qsettings):
    app = _qt()
    from ovtsuite import gui_update
    _dists, d12, d23, keys, _key = releases
    monkeypatch.setattr(du, "base_keys", lambda: keys)
    monkeypatch.setattr(branding, "__version__", "0.5.0")
    # a file the update replaces was changed here
    app_dir = _lay(tmp_path / "app", dict(V1, **{"OVTSuite.exe": b"patched"}))
    files = dict(zip(FILES, (d12, d23, str(tmp_path / "setup.exe"))))
    launched, calls = _drive_install(monkeypatch, gui_update, app_dir, files, tmp_path)
    q = app.quitOnLastWindowClosed()
    try:
        assert gui_update.install(None, _offer(_chain(d12, d23))) is True
    finally:
        app.setQuitOnLastWindowClosed(q)
    assert calls == [list(FILES[:2]), [FILES[2]]]
    assert "helper" not in launched
    assert [p for p, _m in launched["installers"]] == [files[FILES[2]]]
    assert "OVTSuite.exe" in gui_update.last_fallback


@pytest.mark.slow
def test_the_result_is_reported_at_the_next_start(tmp_path, monkeypatch, isolated_qsettings):
    _qt()
    from PySide6 import QtWidgets
    from ovtsuite import gui_update
    res = str(tmp_path / "r.json")
    monkeypatch.setattr(du, "result_path", lambda: res)
    monkeypatch.setattr(du, "cleanup_after_update", lambda d: None)
    shown = []

    class Bar:
        def showMessage(self, text, ms):
            shown.append(text)

    class Win:
        def statusBar(self):
            return Bar()

        def isVisible(self):
            return False
    du.write_result(res, {"ok": True, "to": "0.5.2", "message": "updated to 0.5.2"})
    gui_update.report_last_update(Win())
    assert shown == ["updated to 0.5.2"] and not os.path.exists(res)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(gui_update.QtCore.QTimer, "singleShot", lambda ms, fn: fn())
    du.write_result(res, {"ok": False, "to": "0.5.2", "message": "disk full", "log": "x.log"})
    gui_update.report_last_update(Win())
    assert "disk full" in warned[0] and "full installer" in warned[0]
    assert gui_update._settings().value(up.DELTA_FAILED_KEY) == "0.5.2"
    gui_update.report_last_update(Win())                    # once only
    assert len(warned) == 1
