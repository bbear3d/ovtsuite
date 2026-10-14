# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The update check (ovtsuite.updates, ovtsuite.gui_update): versions,
which assets are offered (the app and every installed module), the signed
checksum list, verifying downloads, the once-a-day rule, the installer
chain and the installer scripts' side. Fake release JSON only: no network."""

import hashlib
import io
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from ovtsuite import branding  # noqa: E402
from ovtsuite import updates as up  # noqa: E402


# ------------------------------------------------------------------ fixtures
def _asset(name, size=10, digest=""):
    return {"name": name, "size": size, "digest": digest,
            "browser_download_url": "https://example.invalid/dl/" + name}


def _release(tag="v0.4.7", assets=(), prerelease=False, draft=False, body="* new"):
    return {"tag_name": tag, "name": "OVT-Suite " + tag[1:], "body": body,
            "html_url": "https://github.com/bbear3d/ovtsuite/releases/tag/" + tag,
            "prerelease": prerelease, "draft": draft, "assets": list(assets)}


RELEASE = _release(assets=[
    _asset("OVTSuite-0.4.7-setup.exe", 300),
    _asset("OVTSuite-Base-0.4.7-setup.exe", 100),
    _asset("ovtsuite_0.4.7-1_amd64.deb"),
    _asset("OVTSuite-BoschEDC16-1.0.11-setup.exe"),
    _asset("edc16-1.0.11.ovtmod"),
    _asset("edc16-1.0.11-checksum.ovtmod"),
    _asset("OVTSuite-BoschEDC16-Checksum-1.0.11-setup.exe"),
    _asset("ovtsuite-module-edc16_1.0.11-1_all.deb"),
    _asset("OVTSuite-BoschEDC15-1.0.10-setup.exe"),
    _asset("edc15-1.0.10.ovtmod"),
    _asset("OVTSuite-ZF6HP-0.3.0-setup.exe"),
    _asset("SHA256SUMS.txt"),
])


def _mod(mid, name, version, variant="full", by_setup=False):
    return up.InstalledModule(mid, name, version, variant, "/x/" + mid, by_setup)


# ------------------------------------------------------------------ versions
def test_versions_compare_numerically():
    assert up.parse_version("v0.4.10") == ((0, 4, 10), False)
    assert up.parse_version("0.5.0-rc1") == ((0, 5, 0), True)
    assert up.parse_version("1.0b2")[1] is True
    assert up.parse_version("1.0+build3")[1] is False
    assert up.compare_versions("1.0.10", "1.0.9") == 1
    assert up.compare_versions("0.4.6", "0.4.6.0") == 0
    assert up.compare_versions("0.4", "0.4.1") == -1
    assert up.compare_versions("0.5.0-rc1", "0.5.0") == -1
    assert up.is_newer("v0.4.7", "0.4.6")
    assert not up.is_newer("0.4.6", "0.4.6")
    assert not up.is_newer("garbage", "0.4.6")
    with pytest.raises(ValueError):
        up.parse_version("latest")


def test_repo_and_urls_from_source_url():
    assert up.repo_slug("https://github.com/bbear3d/ovtsuite") == ("bbear3d", "ovtsuite")
    assert up.repo_slug("https://github.com/a/b.git/") == ("a", "b")
    assert up.latest_url() == "https://api.github.com/repos/%s/%s/releases/latest" \
        % up.repo_slug(branding.SOURCE_URL)
    with pytest.raises(ValueError):
        up.repo_slug("https://gitlab.com/a/b")


def test_release_parsing_and_prereleases():
    rel = up.Release.from_json(RELEASE)
    assert rel.version == "0.4.7" and rel.usable
    assert rel.asset("edc16-1.0.11.ovtmod").size == 10
    assert not up.Release.from_json(_release(prerelease=True)).usable
    assert not up.Release.from_json(_release(draft=True)).usable
    assert not up.Release.from_json(_release(tag="v0.5.0-rc1")).usable
    with pytest.raises(up.UpdateError):
        up.Release.from_json({"message": "Not Found"})
    # a prerelease is never offered, even though its number is higher
    off = up.make_offer(up.Release.from_json(_release(tag="v9.0.0", prerelease=True)),
                        "0.4.6", [])
    assert not off.any


# ------------------------------------------------------------------ assets
def test_base_installer_prefers_base_only_then_full():
    rel = up.Release.from_json(RELEASE)
    assert up.base_asset(rel).name == "OVTSuite-Base-0.4.7-setup.exe"
    j = dict(RELEASE, assets=[a for a in RELEASE["assets"]
                              if not a["name"].startswith("OVTSuite-Base-")])
    assert up.base_asset(up.Release.from_json(j)).name == "OVTSuite-0.4.7-setup.exe"
    j = dict(RELEASE, assets=[_asset("ovtsuite_0.4.7-1_amd64.deb")])
    assert up.base_asset(up.Release.from_json(j)) is None


def test_module_updates_match_installed_modules_and_variants():
    rel = up.Release.from_json(RELEASE)
    inst = [_mod("edc16", "Bosch EDC16", "1.0.10"),
            _mod("edc15", "Bosch EDC15", "1.0.10"),           # same version: none
            _mod("zf6hp", "ZF 6HP", "0.2.9", "checksum"),     # no checksum asset
            _mod("aisin", "Aisin TR-xx", "0.1.0")]            # not in the release
    ups = {u.mod.id: u for u in up.module_updates(rel, inst)}
    assert set(ups) == {"edc16"}
    u = ups["edc16"]
    assert u.version == "1.0.11"
    assert u.package.name == "edc16-1.0.11.ovtmod"
    assert u.setup.name == "OVTSuite-BoschEDC16-1.0.11-setup.exe"
    assert u.method == "package"
    # the checksum-only variant gets the checksum-only files
    c = up.module_updates(rel, [_mod("edc16", "Bosch EDC16", "1.0.9", "checksum")])[0]
    assert c.package.name == "edc16-1.0.11-checksum.ovtmod"
    assert c.setup.name == "OVTSuite-BoschEDC16-Checksum-1.0.11-setup.exe"
    # installed by its setup.exe: updated by the setup.exe (in-app installing
    # would replace the folder its uninstaller lives in)
    s = up.module_updates(rel, [_mod("edc16", "Bosch EDC16", "1.0.10", by_setup=True)])[0]
    assert s.method == "setup"
    # only a setup.exe in the release
    z = up.module_updates(rel, [_mod("zf6hp", "ZF 6HP", "0.2.9")])[0]
    assert z.package is None and z.method == "setup"


def test_offer_skip_and_manual():
    rel = up.Release.from_json(RELEASE)
    inst = [_mod("edc16", "Bosch EDC16", "1.0.10")]
    off = up.make_offer(rel, "0.4.6", inst)
    assert off.base and off.any and len(off.modules) == 1
    assert off.key == "v0.4.7|edc16=1.0.11"
    assert up.should_show(off, manual=False)
    assert not up.should_show(off, manual=False, skipped=off.key)
    assert up.should_show(off, manual=True, skipped=off.key)        # manual ignores skip
    # a module added to the skipped release is offered again
    more = up.make_offer(rel, "0.4.6", inst + [_mod("zf6hp", "ZF 6HP", "0.2.0")])
    assert up.should_show(more, manual=False, skipped=off.key)
    # up to date: app and modules
    same = up.make_offer(rel, "0.4.7", [_mod("edc16", "Bosch EDC16", "1.0.11")])
    assert not same.any and not up.should_show(same, manual=True)
    # modules only
    mods = up.make_offer(rel, "0.4.7", inst)
    assert not mods.base and mods.any


def test_check_due_once_a_day():
    day = up.CHECK_INTERVAL
    assert up.check_due(None, now=1000.0)
    assert up.check_due("", now=1000.0)
    assert up.check_due("garbage", now=1000.0)
    assert not up.check_due(1000.0, now=1000.0 + day - 1)
    assert up.check_due(1000.0, now=1000.0 + day)
    assert up.check_due("1000.0", now=1000.0 + day + 5)        # QSettings gives a string
    assert up.check_due(5000.0, now=1000.0)                      # clock went back


# ------------------------------------------------------------------ checksums
def test_sums_parse_and_format():
    h1, h2 = "a" * 64, "B" * 64
    text = "# comment\n%s  OVTSuite-Base-0.4.7-setup.exe\n%s *dist/edc16-1.0.11.ovtmod\n" \
           "\nnot a line\n" % (h1, h2)
    sums = up.parse_sums(text.encode())
    assert sums == {"OVTSuite-Base-0.4.7-setup.exe": h1, "edc16-1.0.11.ovtmod": h2.lower()}
    assert up.parse_sums(up.format_sums(sums)) == sums


@pytest.fixture
def keypair(tmp_path):
    pytest.importorskip("cryptography")
    from ovtsuite.modules import signing
    path = str(tmp_path / "k" / "key.pem")
    pub = signing.generate_key(path)
    return path, {"test": pub}


def _rel_with_sums(data, sig):
    assets = [_asset("x.exe", 3), _asset(up.SUMS_NAME, len(data))]
    if sig is not None:
        assets.append(_asset(up.SIG_NAME, len(sig)))
    blobs = {"https://example.invalid/dl/" + up.SUMS_NAME: data,
             "https://example.invalid/dl/" + up.SIG_NAME: sig}
    return up.Release.from_json(_release(assets=assets)), blobs.__getitem__


def test_signed_checksum_list(keypair):
    from ovtsuite.modules import signing
    path, keys = keypair
    data = up.format_sums({"x.exe": hashlib.sha256(b"abc").hexdigest()}).encode()
    rel, fetch = _rel_with_sums(data, signing.sign(data, path))
    cs = up.load_checksums(rel, fetch, keys)
    assert cs.signed and cs.sums["x.exe"] == hashlib.sha256(b"abc").hexdigest()
    # a list changed after signing is refused outright
    rel, fetch = _rel_with_sums(data + b"\n", signing.sign(data, path))
    with pytest.raises(up.UpdateError, match="signature"):
        up.load_checksums(rel, fetch, keys)
    # signed by a key the app does not trust
    rel, fetch = _rel_with_sums(data, signing.sign(data, path))
    with pytest.raises(up.UpdateError):
        up.load_checksums(rel, fetch, {"other": "00" * 32})
    # unsigned: used, with a note
    rel, fetch = _rel_with_sums(data, None)
    cs = up.load_checksums(rel, fetch, keys)
    assert not cs.signed and "not signed" in cs.note and cs.sums
    # no list at all
    cs = up.load_checksums(up.Release.from_json(_release(assets=[_asset("x.exe")])),
                           fetch, keys)
    assert cs.sums == {} and "no " + up.SUMS_NAME in cs.note


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(payload):
    return lambda url: _Resp(payload)


def test_download_verifies_size_and_hash(tmp_path):
    good = b"installer bytes"
    h = hashlib.sha256(good).hexdigest()
    a = up.Asset("x.exe", len(good), "https://example.invalid/x.exe")
    signed = up.Checksums({"x.exe": h}, True)
    path, how = up.download(a, str(tmp_path), signed, opener=_opener(good))
    assert how == "signed checksum list" and open(path, "rb").read() == good
    # present and good: not fetched again
    path, how = up.download(a, str(tmp_path), signed, opener=lambda u: 1 / 0)
    assert how
    # wrong bytes, right size
    a2 = up.Asset("y.exe", len(good), "u")
    with pytest.raises(up.UpdateError, match="SHA-256"):
        up.download(a2, str(tmp_path), up.Checksums({"y.exe": h}),
                    opener=_opener(b"X" * len(good)))
    assert not os.path.exists(tmp_path / "y.exe") and not os.path.exists(tmp_path / "y.exe.part")
    # wrong size
    with pytest.raises(up.UpdateError, match="bytes"):
        up.download(up.Asset("z.exe", 99, "u"), str(tmp_path), signed, opener=_opener(good))
    # no list: GitHub's digest
    a3 = up.Asset("w.exe", len(good), "u", "sha256:" + h)
    assert up.download(a3, str(tmp_path), up.Checksums(), opener=_opener(good))[1] \
        == "GitHub's checksum"
    a4 = up.Asset("v.exe", len(good), "u", "sha256:" + "0" * 64)
    with pytest.raises(up.UpdateError):
        up.download(a4, str(tmp_path), up.Checksums(), opener=_opener(good))
    # nothing vouches for it: kept, but reported unverified ("")
    a5 = up.Asset("u.exe", len(good), "u")
    assert up.download(a5, str(tmp_path), up.Checksums(), opener=_opener(good))[1] == ""
    # cancelled
    with pytest.raises(up.UpdateError, match="cancelled"):
        up.download(up.Asset("c.exe", len(good), "u"), str(tmp_path), signed,
                    opener=_opener(good), cancelled=lambda: True)


def test_release_sums_tool(tmp_path, keypair, monkeypatch):
    path, keys = keypair
    d = tmp_path / "dist"
    d.mkdir()
    (d / "OVTSuite-Base-0.4.7-setup.exe").write_bytes(b"one")
    (d / "edc16-1.0.11.ovtmod").write_bytes(b"two")
    (d / "notes.txt").write_bytes(b"skip me")
    out = str(d / up.SUMS_NAME)
    env = dict(os.environ, OVT_SIGNING_KEY=path)
    tool = os.path.join(ROOT, "tools", "build_modules.py")
    r = subprocess.run([sys.executable, tool, "release-sums", "--out", out, str(d)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    # a second build (Linux) adds its file to the same list
    deb = tmp_path / "ovtsuite_0.4.7-1_amd64.deb"
    deb.write_bytes(b"three")
    r = subprocess.run([sys.executable, tool, "release-sums", "--out", out, str(deb)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    data = open(out, "rb").read()
    sums = up.parse_sums(data)
    assert set(sums) == {"OVTSuite-Base-0.4.7-setup.exe", "edc16-1.0.11.ovtmod",
                         "ovtsuite_0.4.7-1_amd64.deb"}
    assert sums["edc16-1.0.11.ovtmod"] == hashlib.sha256(b"two").hexdigest()
    ok, who = up.verify_sums(data, open(out + ".sig", "rb").read(), keys)
    assert ok and who == "test"
    # --unsigned removes a signature that would no longer match
    r = subprocess.run([sys.executable, tool, "release-sums", "--out", out, "--unsigned",
                        str(deb)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and not os.path.exists(out + ".sig")


# ------------------------------------------------------------------ Windows side
def test_chain_script_waits_installs_and_relaunches():
    base = r"C:\Temp\OVTSuite-update-0.4.7\OVTSuite-Base-0.4.7-setup.exe"
    mod = r"C:\Temp\OVTSuite-update-0.4.7\OVTSuite-BoschEDC16-1.0.11-setup.exe"
    app = r"C:\Users\a%b\AppData\Local\Programs\OVT-Suite\OVTSuite.exe"
    s = up.chain_script([base], app, 4242)
    assert 'PID eq 4242' in s and "\r\n" in s
    line = [x for x in s.splitlines() if base in x][0]
    assert line.startswith('start "" /wait "%s"' % base)
    for arg in up.INSTALLER_ARGS + (up.RELAUNCH_ARG,):
        assert arg in line
    assert '"C:\\Users\\a%%b' in s                     # % doubled for cmd
    # base then a module installer: the script relaunches, not setup
    s = up.chain_script([base, mod], app, 1)
    lines = s.splitlines()
    assert up.RELAUNCH_ARG not in s
    assert lines.index([x for x in lines if base in x][0]) < \
        lines.index([x for x in lines if mod in x][0])
    assert lines[-2].startswith('start "" "C:\\Users')
    # nothing to run (module packages only): just the restart
    s = up.chain_script([], app, 1)
    assert "/wait" not in s and 'start ""' in s


def test_other_instances_from_tasklist():
    text = ('"OVTSuite.exe","100","Console","1","120,000 K"\n'
            '"OVTFlasher.exe","200","Console","1","90,000 K"\n'
            '"OVTMcp.exe","300","Console","1","40,000 K"\n'
            '"notepad.exe","400","Console","1","4,000 K"\n')
    assert up.parse_tasklist(text, 100) == [("OVTFlasher.exe", 200), ("OVTMcp.exe", 300)]


def _iss_defines(path):
    return dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"',
                           open(path, encoding="utf-8").read(), re.M))


def test_installers_wait_for_the_running_app_and_relaunch():
    pk = os.path.join(ROOT, "packaging")
    assert _iss_defines(os.path.join(pk, "tools.iss"))["AppMutex"] == branding.APP_MUTEX
    assert _iss_defines(os.path.join(pk, "module_installer.iss"))["AppMutex"] \
        == branding.APP_MUTEX
    main = open(os.path.join(pk, "installer.iss"), encoding="utf-8").read()
    assert "AppMutex={#AppMutex}" in main and "CloseApplications=yes" in main
    assert "Check: WizardSilent and RelaunchRequested" in main
    assert "function RelaunchRequested(): Boolean;" in main
    assert "{param:RELAUNCH|0}" in main
    assert up.RELAUNCH_ARG == "/RELAUNCH=1"
    assert "AppMutex={#AppMutex}" in open(os.path.join(pk, "module_installer.iss"),
                                          encoding="utf-8").read()
    # every exe of the bundle takes the mutex at start-up
    assert "hold_app_mutex" in open(os.path.join(pk, "entry.py"), encoding="utf-8").read()
    # the app id the updater looks for in the registry is the installer's
    assert "AppId={{%s" % up.INNO_APP_ID[1:] in main


def test_install_kind_and_mutex_off_windows(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert up.install_kind() == "source"
    if sys.platform != "win32":
        assert up.hold_app_mutex() is None
        assert up.other_instances() == []


def test_cleanup_keeps_newer_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(up.tempfile, "gettempdir", lambda: str(tmp_path))
    for v in ("0.4.5", "0.4.6", "0.4.7"):
        os.makedirs(up.download_dir(v))
    up.cleanup_downloads("0.4.6")
    assert sorted(os.listdir(tmp_path)) == ["OVTSuite-update-0.4.7"]


def test_option_on_the_general_page():
    from ovtsuite import options
    o = options.BY_KEY[up.OPTION_KEY]
    assert o.page == options.GENERAL and o.default is True and o.kind == "bool"


# ------------------------------------------------------------------ the dialog
@pytest.mark.slow
def test_update_dialog(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:
        pytest.skip("PySide6 is not usable here: %s" % exc)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui_update
    rel = up.Release.from_json(RELEASE)
    off = up.make_offer(rel, "0.4.6", [_mod("edc16", "Bosch EDC16", "1.0.10")])
    dlg = gui_update.UpdateDialog(off, "inno")
    assert dlg.can_install() and dlg.install_btn is not None
    assert dlg.rows() == [(branding.APP_NAME, "0.4.6", "0.4.7", "app installer"),
                          ("Bosch EDC16 module", "1.0.10", "1.0.11", "module package")]
    assert "new" in dlg.notes.toPlainText()
    for kind in ("frozen", "source"):
        d = gui_update.UpdateDialog(off, kind)
        assert not d.can_install() and d.install_btn is None
    # a release with no Windows installer for the app cannot be installed here
    j = dict(RELEASE, assets=[a for a in RELEASE["assets"] if "OVTSuite-" not in a["name"]
                              or "Bosch" in a["name"]])
    off2 = up.make_offer(up.Release.from_json(j), "0.4.6", [])
    assert not gui_update.UpdateDialog(off2, "inno").can_install()
    # the buttons
    dlg._done("skip")
    assert dlg.choice == "skip"
    plan = gui_update._plan(off)
    assert plan[0].name == "OVTSuite-Base-0.4.7-setup.exe"
    assert [a.name for _u, a in plan[1]] == ["edc16-1.0.11.ovtmod"]


@pytest.mark.slow
def test_check_runs_off_the_gui_thread_and_reports(monkeypatch, isolated_qsettings):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as exc:
        pytest.skip("PySide6 is not usable here: %s" % exc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import threading
    from ovtsuite import gui_update
    main = threading.get_ident()
    seen, shown, said = [], [], []

    def fetch():
        seen.append(threading.get_ident())
        return up.Release.from_json(RELEASE)
    monkeypatch.setattr(up, "fetch_latest", fetch)
    monkeypatch.setattr(up, "installed_modules", lambda: [])
    monkeypatch.setattr(gui_update, "show_offer", lambda parent, offer: shown.append(offer))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: said.append(a[2]))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: said.append(a[2]))

    def run(manual):
        gui_update.check(None, manual)
        for _ in range(200):
            app.processEvents()
            if not gui_update._busy:
                break
            QtCore.QThread.msleep(10)
        app.processEvents()

    monkeypatch.setattr(branding, "__version__", "0.4.6")
    run(manual=False)
    assert seen and seen[0] != main
    assert len(shown) == 1 and shown[0].release.version == "0.4.7"
    # once a day: the check stored its time
    assert not up.check_due(gui_update._settings().value(up.LAST_CHECK_KEY))
    # skipped: the automatic check is quiet, the manual one shows it
    gui_update._settings().setValue(up.SKIP_KEY, shown[0].key)
    run(manual=False)
    assert len(shown) == 1
    run(manual=True)
    assert len(shown) == 2
    # up to date: only the manual check says so
    monkeypatch.setattr(branding, "__version__", "0.4.7")
    run(manual=False)
    assert not said
    run(manual=True)
    assert "latest version" in said[-1]
    # offline: quiet automatically, an error when asked

    def offline():
        raise up.UpdateError("could not reach GitHub: no route")
    monkeypatch.setattr(up, "fetch_latest", offline)
    run(manual=False)
    assert len(said) == 1
    run(manual=True)
    assert "could not reach GitHub" in said[-1]


def test_installers_go_where_the_old_version_is(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    assert up.install_mode(str(tmp_path / "Programs" / "OVT-Suite")) == up.CURRENT_USER
    assert up.install_mode("/opt/elsewhere") == up.ALL_USERS
    s = up.chain_script([(r"C:\t\OVTSuite-Base-0.4.7-setup.exe", up.ALL_USERS),
                         (r"C:\t\OVTSuite-ZF6HP-0.3.0-setup.exe", up.CURRENT_USER)],
                        r"C:\a\OVTSuite.exe", 7)
    lines = s.splitlines()
    assert "/ALLUSERS" in [x for x in lines if "Base" in x][0]
    assert "/CURRENTUSER" in [x for x in lines if "ZF6HP" in x][0]
