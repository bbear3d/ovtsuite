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
* the checksum variant runs without any definition file;
* a closed module's package carries compiled code and no source, tagged for
  this interpreter and platform, and one tagged for another is refused.
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
from ovtsuite.modules import api, build, native, package, signing   # noqa: E402

SRC = os.path.join(ROOT, "ovtsuite")
PKG = branding.PKG
#: Closed modules are compiled (Cython and a C compiler); without them this
#: file still checks everything else, on source packages.
NATIVE = not native.available()
needs_native = pytest.mark.skipif(not NATIVE, reason=native.available() or "")


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
            # the whole EDC15 package only shows one variant replacing the
            # other: compiling its 30-odd files would add minutes for nothing
            nat = None if NATIVE and (mid, v) != ("edc15", api.FULL) else False
            build.pack(SRC, pk, mid, v, key, branding.APP_NAME, native=nat)
    return base_parent, key, pk


def _pkg(world, mid, variant=api.FULL):
    spec = build._specs(SRC)[mid]
    return os.path.join(world[2], build.package_name(spec, variant))


def _member(path, prefix):
    """The one file in package ``path`` whose name starts with ``prefix``
    (``aisin/module.`` -> its source, byte-code or extension module)."""
    with zipfile.ZipFile(path) as z:
        got = [n for n in z.namelist() if n.startswith(prefix)]
    assert len(got) == 1, got
    return got[0]


def _run(world, code, mods, extra_env=None):
    env = dict(os.environ, PYTHONPATH=world[0], OVTSUITE_MODULES_DIRS=mods)
    # never the user's own modules folder: a module installed there is found
    # too (build.check_package does the same)
    home = os.path.join(os.path.dirname(world[0]), "home")
    env.update(LOCALAPPDATA=home, XDG_DATA_HOME=home)
    env.pop("OVT_DISABLE_MODULES", None)
    env.pop("OVT_ALLOW_UNSIGNED", None)
    env.update(extra_env or {})
    cwd = mods.split(os.pathsep)[0]          # (several modules folders: the first)
    os.makedirs(cwd, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=cwd,
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
import os, sys
pkgdir = os.path.join(%r, "edc15", "edc15")
names = os.listdir(pkgdir)
assert [n for n in names if n.startswith("checksum15.")], names
assert not [n for n in names if n.startswith("kb15.")], names
mod = sys.modules["{pkg}.edc15.checksum15"]
assert mod.__file__.endswith(%r), mod.__file__
print("ok")
'''.format(pkg=PKG) % (_pkg(world, "edc15", api.CHECKSUM_ONLY), mods, mods,
                       native.ext_suffix() if NATIVE else ".py"), mods)
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
        _rewrite(src, bad, edit_file=(_member(src, "aisin/module."),
                                      lambda b: b + b"\n# x\n"))
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
    rel = _member(_pkg(world, "aisin"), "aisin/module.")
    with open(os.path.join(mods, "aisin", *rel.split("/")), "ab") as fh:
        fh.write(b"\n# edited\n")
    out = _run(world, r'''
from {pkg}.modules import registry
assert registry.spec("aisin") is None
print(registry.problems()["aisin"])
'''.format(pkg=PKG), mods)
    assert "damaged" in out


@needs_native
def test_a_closed_module_ships_compiled_code_and_no_source(world):
    """The module EULA's modules go as extension modules (byte-code for a
    package ``__init__``), tagged for this interpreter and platform; an
    open-source one keeps its source and runs anywhere."""
    for variant in (api.FULL, api.CHECKSUM_ONLY):
        path = _pkg(world, "aisin", variant)
        assert os.path.basename(path).endswith(
            "-%s-%s.ovtmod" % (package.python_tag(), package.platform_tag()))
        op = package.open_package(path)
        names = op.zip.namelist()
        code = [n for n in names if n.startswith("aisin/") and n.endswith(
            (".py", ".pyc", native.ext_suffix()))]
        assert not [n for n in code if n.endswith(".py")], code
        assert "aisin/__init__.pyc" in code
        assert "aisin/module%s" % native.ext_suffix() in code
        assert "aisin/EULA.txt" in names
        assert ("aisin/help/index.md" in names) == (variant == api.FULL)
        assert (op.manifest.python, op.manifest.platform) == (
            package.python_tag(), package.platform_tag())
    op = package.open_package(_pkg(world, "vdo"))
    assert not op.manifest.python and not op.manifest.platform
    assert "vdo/module.py" in op.zip.namelist()
    assert build.check_package(world[0], PKG, _pkg(world, "aisin"), "aisin", api.FULL)[0]


@pytest.mark.parametrize("tags, words", [
    ({"platform": "linux_aarch64"}, "Linux (ARM64)"),
    ({"python": "cp312"}, "Python 3.12"),
])
def test_a_package_for_another_platform_is_refused(world, tmp_path, tags, words):
    src = _pkg(world, "aisin")
    bad = str(tmp_path / "other.ovtmod")
    _rewrite(src, bad, edit_manifest=lambda d: d.update(
        dict({"python": package.python_tag(), "platform": package.platform_tag()}, **tags)))
    mods = str(tmp_path / "m")
    os.makedirs(mods)
    out = _run(world, r'''
from {pkg}.modules import installer
try:
    installer.install(%r, scope=%r, allow_unsigned=True)
except installer.InstallError as ex:
    print("refused:", ex)
'''.format(pkg=PKG) % (bad, mods), mods)
    assert out.startswith("refused:") and words in out and "built for" in out, out
    assert not os.listdir(mods)


def test_an_installed_module_for_another_platform_is_not_loaded(world, tmp_path):
    """A modules folder copied from another OS (or kept across an app built
    on another Python) is reported, not imported."""
    mods = str(tmp_path)
    _run(world, "from %s.modules import installer; installer.install(%r, scope=%r)"
         % (PKG, _pkg(world, "aisin"), mods), mods)
    mpath = os.path.join(mods, "aisin", package.MANIFEST)
    with open(mpath) as fh:
        d = json.load(fh)
    d.update(python="cp399", platform="linux_x86_64")
    with open(mpath, "w") as fh:
        json.dump(d, fh)
    out = _run(world, r'''
from {pkg}.modules import registry
assert registry.spec("aisin") is None
print(registry.problems()["aisin"])
'''.format(pkg=PKG), mods)
    assert "built for Python 3.99 on Linux (x86-64)" in out, out


@pytest.fixture(scope="module")
def legacy(world, tmp_path_factory):
    """An Aisin package as released before modules were compiled: signed,
    untagged, Python source in it."""
    pk = str(tmp_path_factory.mktemp("legacy"))
    return build.pack(SRC, pk, "aisin", api.FULL, world[1], branding.APP_NAME,
                      native=False)


def _tree_code(root):
    return sorted(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
                  for d, _, ns in os.walk(root) for n in ns
                  if n.endswith((".py", ".pyc")) or os.path.basename(d) == "__pycache__")


@needs_native
def test_installing_a_compiled_module_scrubs_every_source_copy(world, legacy, tmp_path):
    """An update to a compiled module leaves no copy of the old source on
    disk: not in the folder it replaces (the per-user one), not in another
    modules folder (an older all-users install), and a copy put there by the
    module's own setup.exe loses its code but keeps its uninstaller."""
    extra, setup_dir = str(tmp_path / "extra"), str(tmp_path / "bysetup")
    env = {"LOCALAPPDATA": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "home")}
    out = _run(world, r'''
import os
from {pkg}.modules import installer
for scope in (installer.USER, %r, %r):
    installer.install(%r, scope=scope)
open(os.path.join(%r, "aisin", "unins000.exe"), "wb").close()
user = os.path.join(installer.user_dir(), "aisin")
assert os.path.isfile(os.path.join(user, "aisin", "module.py"))
assert installer.scrub_source("aisin") == []        # no compiled copy yet: kept
m = installer.install(%r, scope=installer.USER)
assert m.platform
print("ok")
'''.format(pkg=PKG) % (extra, setup_dir, legacy, setup_dir, _pkg(world, "aisin")),
        os.pathsep.join([extra, setup_dir]), env)
    assert out.endswith("ok"), out
    user = os.path.join(str(tmp_path / "home"), branding.CONFIG_DIR, "modules", "aisin")
    assert not [p for p in _tree_code(user) if p.endswith(".py")]
    assert "aisin/__init__.pyc" in _tree_code(user)
    assert not os.path.exists(os.path.join(extra, "aisin"))          # removed
    left = os.path.join(setup_dir, "aisin")
    assert _tree_code(left) == []
    assert os.path.isfile(os.path.join(left, "unins000.exe"))
    assert os.path.isfile(os.path.join(left, "aisin", "help", "index.md"))


@needs_native
def test_the_app_scrubs_source_left_behind_at_start(world, legacy, tmp_path):
    """A compiled copy laid down without the scrub (an installer, a folder
    copy) next to an old source one: the next start removes the source copy
    and any stray code beside the compiled one -- help and data stay. While
    only the source copy exists, nothing is touched."""
    old, new = str(tmp_path / "old"), str(tmp_path / "new")
    env = {"LOCALAPPDATA": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "home")}
    dirs = os.pathsep.join([old, new])
    _run(world, "from %s.modules import installer; installer.install(%r, scope=%r)"
         % (PKG, legacy, old), dirs, env)
    _run(world, "from %s.modules import registry; assert registry.spec('aisin')"
         % PKG, dirs, env)
    assert os.path.isfile(os.path.join(old, "aisin", "aisin", "module.py"))
    _run(world, "from %s.modules import installer; installer.install(%r, scope=%r)"
         % (PKG, _pkg(world, "aisin"), new), dirs, env)
    stray = os.path.join(new, "aisin", "aisin", "module.py")
    with open(stray, "w") as fh:
        fh.write("# the old source, laid over by hand\n")
    os.makedirs(os.path.join(new, "aisin", "aisin", "__pycache__"))
    with open(os.path.join(new, "aisin", "aisin", "__pycache__", "x.cpython-313.pyc"), "wb"):
        pass
    out = _run(world, r'''
import sys
from {pkg}.modules import registry, loader
s = registry.spec("aisin")
assert s is not None and loader.manifest("aisin").platform, registry.problems()
print(sys.modules["{pkg}.aisin.module"].__file__)
'''.format(pkg=PKG), dirs, env)
    assert out.endswith(native.ext_suffix()), out
    assert not os.path.exists(os.path.join(old, "aisin"))
    assert _tree_code(os.path.join(new, "aisin")) == ["aisin/__init__.pyc"]
    assert os.path.isfile(os.path.join(new, "aisin", "aisin", "help", "index.md"))


@needs_native
def test_the_installers_scrub_command(world, legacy, tmp_path):
    """``scrub-module-sources`` (what setup runs, with its own rights) lists
    with --dry-run and then removes."""
    old, new = str(tmp_path / "old"), str(tmp_path / "new")
    env = {"LOCALAPPDATA": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "home")}
    # one process: both copies arrive after its start (whose own scrub is
    # the previous test's), into explicit folders (no install-time scrub)
    out = _run(world, r'''
import os
from {pkg}.modules import installer
from {pkg}.cli import main
installer.install(%r, scope=%r)
installer.install(%r, scope=%r)
assert main(["scrub-module-sources", "--dry-run"]) == 0
assert os.path.isdir(os.path.join(%r, "aisin"))
assert main(["scrub-module-sources"]) == 0
assert not os.path.exists(os.path.join(%r, "aisin"))
assert main(["scrub-module-sources"]) == 0
print("ok")
'''.format(pkg=PKG) % (legacy, old, _pkg(world, "aisin"), new, old, old),
        os.pathsep.join([old, new]), env)
    assert "would remove" in out and "no module source to remove" in out, out


def test_release_scrub_finds_the_source_downloads():
    """tools/build_modules.py scrub-releases: what it would delete from the
    GitHub releases -- the closed modules' source packages, their setup.exe
    and .deb of releases before compiled packages; never a compiled package,
    the base app, or an open-source module."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import build_modules as bm
    tag = "%s-%s" % (package.python_tag(), package.platform_tag())

    def rel(t, *names):
        return {"tag_name": t, "assets": [{"name": n, "id": i} for i, n in enumerate(names)]}
    releases = [
        rel("v0.6.0", "edc16-1.0.16-%s.ovtmod" % tag, "edc16-1.0.16-checksum-%s.ovtmod" % tag,
            "OVTSuite-BoschEDC16-1.0.16-setup.exe", "ovtsuite-module-edc16_1.0.16-1_amd64.deb",
            "edc15-1.0.13.ovtmod", "OVTSuite-Base-0.6.0-setup.exe", "OVTSuite-0.6.0-setup.exe"),
        rel("v0.4.9", "edc15-1.0.12.ovtmod", "edc16-1.0.13-checksum.ovtmod",
            "OVTSuite-Base-0.4.9-setup.exe", "OVTSuite-BoschEDC15-1.0.12-setup.exe",
            "OVTSuite-BoschEDC16-Checksum-1.0.13-setup.exe",
            "ovtsuite-module-edc16_1.0.13-1_all.deb", "ovtsuite-module-zf5hp_0.1.0-1_all.deb",
            "zf5hp-0.1.0.ovtmod", "OVTSuite-ZF5HP-0.1.0-setup.exe", "zf6hp-1.0.12.ovtmod",
            "OVTSuite-0.4.9-setup.exe", "SHA256SUMS.txt"),
    ]
    got = {(t, n) for t, n, _i, _w in bm.source_assets(releases)}
    assert got == {
        ("v0.6.0", "edc15-1.0.13.ovtmod"),          # untagged: source, wherever it is
        ("v0.4.9", "edc15-1.0.12.ovtmod"), ("v0.4.9", "edc16-1.0.13-checksum.ovtmod"),
        ("v0.4.9", "OVTSuite-BoschEDC15-1.0.12-setup.exe"),
        ("v0.4.9", "OVTSuite-BoschEDC16-Checksum-1.0.13-setup.exe"),
        ("v0.4.9", "ovtsuite-module-edc16_1.0.13-1_all.deb")}
    bundles = {(t, n) for t, n, _i, _w in bm.source_assets(releases, bundles=True)} - got
    assert bundles == {("v0.4.9", "OVTSuite-0.4.9-setup.exe")}
    more = {n for _t, n, _i, _w in bm.source_assets(releases, ("zf6hp",))}
    assert more == {"zf6hp-1.0.12.ovtmod"}


def test_package_names_carry_the_tags():
    n = package.file_name("edc16", "1.0.15", api.CHECKSUM_ONLY, "cp313", "win_amd64")
    assert n == "edc16-1.0.15-checksum-cp313-win_amd64.ovtmod"
    assert package.parse_name(n) == ("edc16", "1.0.15", api.CHECKSUM_ONLY, "cp313", "win_amd64")
    assert package.parse_name("vdo-0.1.8.ovtmod") == ("vdo", "0.1.8", api.FULL, "", "")
    assert package.parse_name("zf8hp-0.1.9-linux_x86_64.ovtmod") is None
    assert package.name_fits("vdo-0.1.8.ovtmod")
    here = package.file_name("aisin", "1.0", api.FULL, package.python_tag(),
                             package.platform_tag())
    assert package.name_fits(here)
    assert not package.name_fits(package.file_name("aisin", "1.0", api.FULL, "cp312",
                                                   package.platform_tag()))
    specs = build._specs(SRC)
    assert build.package_name(specs["vdo"], api.FULL) == "vdo-%s.ovtmod" % specs["vdo"].version
    assert build.package_name(specs["edc16"], api.FULL).endswith(
        "-%s-%s.ovtmod" % (package.python_tag(), package.platform_tag()))


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
# a closed module's licence is accepted before it installs (modal)
gui_modules.ModuleLicenceDialog.exec = lambda self: QtWidgets.QDialog.DialogCode.Accepted
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
