# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Module packages end to end: build, sign, install into a base-only app.

A base-only copy of the app is staged once (ovtsuite.modules.build, the
build's own code), small modules are packed from this tree, and every check
runs in a fresh interpreter against that copy -- the situation of a user who
installed the base app and then a module. What is pinned:

* a signed package installs, mounts, and gives full support (or checksum-only
  support for the checksum variant);
* unsigned, tampered, wrong-API and wrong-product packages are refused with a
  reason, and a module damaged after install is not loaded;
* installing one variant replaces the other; removing works;
* the checksum variant runs without any definition file.
"""
import dataclasses
import json
import os
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite import branding                               # noqa: E402
from ovtsuite.modules import api, build, package, signing   # noqa: E402

SRC = os.path.join(ROOT, "ovtsuite")
PKG = branding.PKG


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """(base_parent, key_path, packages dir) -- a staged base-only app that
    trusts a fresh test key."""
    pytest.importorskip("cryptography")
    t = tmp_path_factory.mktemp("modworld")
    base_parent = str(t / "base")
    build.stage_base(SRC, os.path.join(base_parent, PKG))
    key = str(t / "key" / "test.pem")
    pub = signing.generate_key(key)
    with open(os.path.join(base_parent, PKG, "data", signing.KEYS_FILE), "w") as fh:
        json.dump({"keys": [{"id": "test-key", "public": pub}]}, fh)
    pk = str(t / "pk")
    for mid in ("aisin", "edc15", "vdo"):
        for v in (api.FULL, api.CHECKSUM_ONLY):
            build.pack(SRC, pk, mid, v, key, branding.APP_NAME)
    return base_parent, key, pk


def _pkg(world, mid, variant=api.FULL):
    spec = build._specs(SRC)[mid]
    return os.path.join(world[2], build.package_name(spec, variant))


def _run(world, code, mods, extra_env=None):
    env = dict(os.environ, PYTHONPATH=world[0], OVTSUITE_MODULES_DIRS=mods)
    env.pop("OVT_DISABLE_MODULES", None)
    env.pop("OVT_ALLOW_UNSIGNED", None)
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=mods,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_the_staged_base_has_no_module_code(world):
    base = os.path.join(world[0], PKG)
    for mid in ("edc15", "edc16", "zf6hp", "zf8hp", "vdo", "hvac", "aisin"):
        assert not os.path.exists(os.path.join(base, mid)), mid
    data = os.listdir(os.path.join(base, "data"))
    assert not [n for n in data if n.startswith(("zf8hp_", "p350", "edc15_", "vdo_"))]
    assert "frf.key" in data


def test_install_mount_and_use(world, tmp_path):
    mods = str(tmp_path)
    out = _run(world, r'''
from {pkg}.modules import installer, registry, loader
assert registry.installed() == {{}}
m = installer.install(%r, scope=%r)
assert m.variant == "full"
loader.mount_installed()
s = registry.spec("aisin")
assert s is not None and s.version == m.version, registry.problems()
assert loader.installed_records()["aisin"].signed_by == "test-key"
img = bytearray(b"\x11" * 0xE0000)
import struct
struct.pack_into(">IIII", img, 0x203A0, 1, 0x2000, 0xDFFFF, 0)
cs = registry.checksum("aisin")
assert cs.verify(cs.correct(bytes(img))).ok
print("ok")
'''.format(pkg=PKG) % (_pkg(world, "aisin"), mods), mods)
    assert out.endswith("ok")


def test_a_fresh_process_mounts_what_is_installed(world, tmp_path):
    mods = str(tmp_path)
    _run(world, "from %s.modules import installer; installer.install(%r, scope=%r)"
         % (PKG, _pkg(world, "vdo"), mods), mods)
    out = _run(world, r'''
from {pkg}.modules import registry, loader
from {pkg} import datafiles
assert registry.spec("vdo").variant == "full", registry.problems()
assert datafiles.path("vdo_vwk501.json") is not None
assert loader.module_roots()["vdo"].startswith(%r)
print("ok")
'''.format(pkg=PKG) % mods, mods)
    assert out.endswith("ok")


def test_checksum_variant_has_no_definitions(world, tmp_path):
    mods = str(tmp_path)
    out = _run(world, r'''
from {pkg}.modules import installer, registry, loader
installer.install(%r, scope=%r)
loader.mount_installed()
s = registry.spec("edc15")
assert s.variant == "checksum" and not s.has("definitions") and s.build is None
assert registry.checksum("edc15") is not None
assert "checksum-only" in registry.missing_text("edc15")
import os
pkgdir = os.path.join(%r, "edc15", "edc15")
assert os.path.exists(os.path.join(pkgdir, "checksum15.py"))
assert not os.path.exists(os.path.join(pkgdir, "kb15.py"))
print("ok")
'''.format(pkg=PKG) % (_pkg(world, "edc15", api.CHECKSUM_ONLY), mods, mods), mods)
    assert out.endswith("ok")


def test_one_variant_replaces_the_other(world, tmp_path):
    mods = str(tmp_path)
    out = _run(world, r'''
from {pkg}.modules import installer
installer.install(%r, scope=%r)
m = installer.install(%r, scope=%r)
found = installer.discover()["edc15"]
assert len(found) == 1 and found[0].manifest.variant == "full"
assert installer.uninstall("edc15", %r)
assert "edc15" not in installer.discover()
print("ok")
'''.format(pkg=PKG) % (_pkg(world, "edc15", api.CHECKSUM_ONLY), mods,
                       _pkg(world, "edc15"), mods, mods), mods)
    assert out.endswith("ok")


def _rewrite(src, dst, edit_manifest=None, edit_file=None, drop_sig=False):
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w") as zo:
        for n in zi.namelist():
            b = zi.read(n)
            if n == package.MANIFEST and edit_manifest:
                d = json.loads(b)
                edit_manifest(d)
                b = json.dumps(d).encode()
            elif n == package.SIGNATURE and drop_sig:
                continue
            elif edit_file and n == edit_file[0]:
                b = edit_file[1](b)
            zo.writestr(n, b)


@pytest.mark.parametrize("how, why", [
    ("unsigned", "unsigned"),
    ("tampered_file", "damaged"),
    ("tampered_manifest", "not signed by a trusted key"),
])
def test_bad_packages_are_refused(world, tmp_path, how, why):
    src = _pkg(world, "aisin")
    bad = str(tmp_path / "bad.ovtmod")
    if how == "unsigned":
        _rewrite(src, bad, drop_sig=True)
    elif how == "tampered_file":
        _rewrite(src, bad, edit_file=("aisin/module.py", lambda b: b + b"\n# x\n"))
    else:
        _rewrite(src, bad, edit_manifest=lambda d: d.update(version="9.9.9"))
    mods = str(tmp_path / "m")
    os.makedirs(mods)
    out = _run(world, r'''
from {pkg}.modules import installer
try:
    installer.install(%r, scope=%r)
except installer.InstallError as ex:
    print("refused:", ex)
'''.format(pkg=PKG) % (bad, mods), mods)
    assert out.startswith("refused:") and why in out, out


def test_unsigned_allowed_when_the_user_says_so(world, tmp_path):
    bad = str(tmp_path / "dev.ovtmod")
    _rewrite(_pkg(world, "aisin"), bad, drop_sig=True)
    mods = str(tmp_path / "m")
    os.makedirs(mods)
    out = _run(world, r'''
from {pkg}.modules import installer, loader, registry
installer.install(%r, scope=%r)
loader.mount_installed()
assert registry.spec("aisin") is not None
print("ok")
'''.format(pkg=PKG) % (bad, mods), mods, {"OVT_ALLOW_UNSIGNED": "1"})
    assert out.endswith("ok")


def test_a_module_for_another_api_level_is_refused(world, tmp_path):
    spec = dataclasses.replace(build._specs(SRC)["aisin"], module_api=99)
    p = str(tmp_path / "future.ovtmod")
    package.build(spec, SRC, p, api.FULL, world[1], app=branding.APP_NAME)
    mods = str(tmp_path / "m")
    os.makedirs(mods)
    out = _run(world, r'''
from {pkg}.modules import installer
try:
    installer.install(%r, scope=%r)
except installer.InstallError as ex:
    print("refused:", ex)
'''.format(pkg=PKG) % (p, mods), mods)
    assert "module API 99" in out


def test_damage_after_install_stops_the_module_loading(world, tmp_path):
    mods = str(tmp_path)
    _run(world, "from %s.modules import installer; installer.install(%r, scope=%r)"
         % (PKG, _pkg(world, "aisin"), mods), mods)
    with open(os.path.join(mods, "aisin", "aisin", "module.py"), "a") as fh:
        fh.write("\n# edited\n")
    out = _run(world, r'''
from {pkg}.modules import registry
assert registry.spec("aisin") is None
print(registry.problems()["aisin"])
'''.format(pkg=PKG), mods)
    assert "damaged" in out


def test_build_verifies_a_checksum_package(world):
    ok, out = build.check_package(world[0], PKG,
                                  _pkg(world, "vdo", api.CHECKSUM_ONLY),
                                  "vdo", api.CHECKSUM_ONLY)
    assert ok, out


def test_module_manager_installs_from_a_file(world, tmp_path):
    pytest.importorskip("PySide6")
    mods = str(tmp_path / "user")
    out = _run(world, r'''
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication([])
QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, %r)
QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
told = []
QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: told.append(a[2]) or 0)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: told.append("WARN " + a[2]) or 0)
from {pkg}.modules import installer
installer.user_dir = lambda: %r          # keep the test out of the real profile
from {pkg} import gui_modules
from {pkg}.modules import registry
dlg = gui_modules.ModuleManagerDialog()
row = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())].index("Aisin TR-xx")
assert dlg.table.item(row, 1).text() == "not installed"
assert gui_modules.install_from_file(dlg, %r) == "aisin", told
dlg.refresh()
assert dlg.table.item(row, 1).text().startswith("installed"), dlg.table.item(row, 1).text()
assert dlg.table.item(row, 4).text() == "test-key"
assert registry.checksum("aisin") is not None
assert any("ready" in t for t in told), told
print("ok")
'''.format(pkg=PKG) % (str(tmp_path / "qs"), mods, _pkg(world, "aisin")), str(tmp_path))
    assert out.endswith("ok")
