# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Backwards compatibility: this app must still load modules built for older
releases of it.

Every signed module package a release build makes is kept in
``Reference/ModuleReleases`` (build.ps1 passes ``--archive``; override the
folder with ``OVT_MODULE_RELEASES``). Each one is installed into a base-only
copy of THIS app, in a clean interpreter, and every hook it declares is
imported; its checksum hook is also called. A base-app change that removes
or renames something an older module uses fails here, before a release.

Also pinned, without any archive: :meth:`ModuleSpec.call` passes a hook only
the keyword arguments it accepts, so the app can grow a hook's arguments
without breaking a module written before them.
"""
import glob
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite import branding                                # noqa: E402
from ovtsuite.modules import api, build                      # noqa: E402

SRC = os.path.join(ROOT, branding.PKG)
ARCHIVE = os.environ.get("OVT_MODULE_RELEASES",
                         os.path.join(ROOT, "Reference", "ModuleReleases"))

_LOAD = r'''
import sys
from {pkg}.modules import installer, loader, registry
man = installer.install(sys.argv[1], scope=sys.argv[2], allow_unsigned=True)
loader.mount_installed()
spec = registry.spec(man.id)
assert spec is not None, registry.problems()
for hook in ("build", "after_restore", "checksum", "load_kb"):
    if getattr(spec, hook):
        assert spec.resolve(hook) is not None, hook        # imports its code
cs = spec.call("checksum", None) if spec.checksum else None
if cs is not None:
    cs.is_supported(b"\xff" * 0x100)
print("ok %s %s %s api=%d" % (man.id, man.version, man.variant, man.module_api))
'''


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """A base-only copy of this app (no in-tree module to shadow the package)."""
    parent = tmp_path_factory.mktemp("base")
    build.stage_base(SRC, str(parent / branding.PKG))
    return str(parent)


def _load(base_parent, ovtmod, tmp):
    from ovtsuite.modules.installer import ENV_DIRS
    mods = os.path.join(str(tmp), "mods")
    os.makedirs(mods, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=base_parent, OVT_ALLOW_UNSIGNED="1",
               **{ENV_DIRS: mods})
    env.pop("OVT_DISABLE_MODULES", None)
    r = subprocess.run([sys.executable, "-c", _LOAD.format(pkg=branding.PKG),
                        os.path.abspath(ovtmod), mods],
                       env=env, cwd=mods, capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and "ok " in r.stdout, (r.stdout + r.stderr).strip()


RELEASES = sorted(glob.glob(os.path.join(ARCHIVE, "*.ovtmod")))


@pytest.mark.skipif(not RELEASES, reason="no archived module releases in %s" % ARCHIVE)
@pytest.mark.parametrize("ovtmod", RELEASES, ids=[os.path.basename(p) for p in RELEASES])
def test_every_released_module_still_loads(ovtmod, base, tmp_path):
    ok, out = _load(base, ovtmod, tmp_path)
    assert ok, out


def test_the_check_itself_on_a_package_built_now(base, tmp_path):
    pk = build.pack(SRC, str(tmp_path / "pk"), "aisin", api.FULL, None, branding.APP_NAME)
    ok, out = _load(base, pk, tmp_path)
    assert ok and "ok aisin" in out, out


def test_hooks_get_only_the_arguments_they_accept():
    def old_hook(session=None):                 # written before image= existed
        return ("old", session)

    def new_hook(session=None, image=None):
        return ("new", session, image)

    def open_hook(session=None, **kw):
        return ("open", session, kw)

    assert old_hook(**api.accepted(old_hook, {"session": 1, "image": 2})) == ("old", 1)
    assert new_hook(**api.accepted(new_hook, {"session": 1, "image": 2})) == ("new", 1, 2)
    assert open_hook(**api.accepted(open_hook, {"image": 2})) == ("open", None, {"image": 2})


def test_module_versions_are_their_own():
    specs = build._specs(SRC)
    stable = {m: specs[m].version for m in ("edc15", "edc16", "zf6hp")}
    assert all(v.split(".")[0] == "1" for v in stable.values()), stable
    for m in ("zf8hp", "aisin", "vdo", "hvac"):
        assert specs[m].version.startswith("0."), (m, specs[m].version)


def test_a_change_without_a_version_bump_is_flagged():
    stamps = {"edc16": {"fp": "aaa", "version": "1.0.0"}}
    assert build.version_warning(stamps, "edc16", "bbb", "1.0.0")      # changed, same version
    assert not build.version_warning(stamps, "edc16", "bbb", "1.0.1")  # bumped
    assert not build.version_warning(stamps, "edc16", "aaa", "1.0.0")  # unchanged
    assert not build.version_warning({"edc16": "aaa"}, "edc16", "bbb", "1.0.0")  # old cache
