# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build-time module work: who owns what, staging the base app, packing
modules, and deciding what needs rebuilding. Used by ``tools/build_modules.py``
(and so by build.ps1 / build_linux.sh / build.py) and by the tests; never by
the running app.

* :func:`ownership` -- every file under the package directory is the base
  app's or exactly one module's; a file claimed twice, or a module package
  file outside its own folder, fails the build (a structural guarantee, not a
  convention).
* :func:`stage_base` -- copy the package directory WITHOUT any module's files:
  what the base-only app is frozen from.
* :func:`fingerprint` -- a content hash per module (its files + the module
  API level + variant) and one for the base; a module is rebuilt only when
  its hash changes, the app re-frozen only when the base's does.
* :func:`pack` -- write ``<id>-<version>[-checksum].ovtmod``.
* :func:`check_checksum_variant` -- import a checksum-only package in a clean
  interpreter against a base-only copy and use its checksum hook, so a
  checksum module can never ship with a missing import.
"""

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from . import api as _api
from . import package as _package

SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = (".pyc", ".pyo", ".tmp")


def _specs(src_pkg: str) -> Dict[str, "_api.ModuleSpec"]:
    """Every module's spec, read from the source tree's module.py files."""
    import importlib.util
    from .. import platforms
    out = {}
    for mid in platforms.MODULES:
        path = os.path.join(src_pkg, mid, "module.py")
        if not os.path.isfile(path):
            continue
        # module.py only imports branding and modules.api (relative), so it
        # is loaded as part of the running app package, from this source tree
        name = "%s.%s.module" % (__package__.split(".")[0], mid)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out[mid] = mod.MODULE
    return out


def _walk(src_pkg: str) -> List[str]:
    out = []
    for d, dirs, names in os.walk(src_pkg):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for n in sorted(names):
            if n.endswith(SKIP_SUFFIX):
                continue
            out.append(os.path.relpath(os.path.join(d, n), src_pkg).replace(os.sep, "/"))
    return out


def ownership(src_pkg: str, specs=None) -> Dict[str, str]:
    """relpath -> owner ("" = base app) for every file under ``src_pkg``.
    Raises ValueError listing every file claimed by two owners."""
    specs = specs if specs is not None else _specs(src_pkg)
    owners: Dict[str, str] = {}
    clash = []
    for rel in _walk(src_pkg):
        claim = []
        top = rel.split("/")[0]
        if top in specs:
            claim.append(top)
        if rel.startswith("data/"):
            name = rel[len("data/"):]
            claim += [m for m, s in specs.items()
                      if any(fnmatch.fnmatchcase(name, g) for g in s.data)]
        claim = sorted(set(claim))
        if len(claim) > 1:
            clash.append("%s: %s" % (rel, ", ".join(claim)))
        owners[rel] = claim[0] if claim else ""
    if clash:
        raise ValueError("files owned by more than one module:\n  " + "\n  ".join(clash))
    return owners


def stage_base(src_pkg: str, out_pkg: str, specs=None) -> List[str]:
    """Bring ``out_pkg`` to exactly the base app's files (relpaths returned).

    In place, not delete-and-copy: a file is copied only when it is new or its
    bytes differ, and a file that is no longer a base file is removed.
    ``__pycache__`` folders are left alone -- byte-code beside an unchanged
    source is harmless, and on Windows they are the folders an antivirus scan
    or a Python process still holds open, so deleting the whole tree failed
    with "Access is denied" (Bear's build, 2026-10-10)."""
    owners = ownership(src_pkg, specs)
    want = [rel for rel, who in owners.items() if not who]
    keep = set(want)
    os.makedirs(out_pkg, exist_ok=True)
    # remove what is no longer the base app's (a module's file, a deleted file)
    for d, dirs, names in os.walk(out_pkg, topdown=True):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, out_pkg).replace(os.sep, "/")
            if rel not in keep:
                _remove(p)
    for rel in want:
        src = os.path.join(src_pkg, *rel.split("/"))
        dst = os.path.join(out_pkg, *rel.split("/"))
        if os.path.isfile(dst) and _same(src, dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            _remove(dst)
        shutil.copy2(src, dst)
    _prune_empty(out_pkg)
    return want


def _same(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def _remove(path: str, tries: int = 8) -> None:
    """Delete one file, retrying while Windows reports it in use (an
    antivirus scan or an indexer opens freshly written files briefly). A
    read-only file is made writable first."""
    import stat
    import time
    delay = 0.1
    for attempt in range(tries):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            if attempt == tries - 1:
                raise PermissionError(
                    "cannot delete %s -- another program has it open (an editor, a "
                    "running copy of the app, or an antivirus scan); close it and "
                    "build again" % path)
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _prune_empty(top: str) -> None:
    """Remove folders left empty (never ``__pycache__``, never ``top``)."""
    for d, dirs, names in os.walk(top, topdown=False):
        if d == top or os.path.basename(d) in SKIP_DIRS:
            continue
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass                       # in use or not empty: harmless to keep


def _hash(src_pkg: str, rels, extra: str) -> str:
    h = hashlib.sha256(extra.encode())
    for rel in sorted(rels):
        h.update(rel.encode() + b"\0")
        with open(os.path.join(src_pkg, *rel.split("/")), "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def fingerprint(src_pkg: str, specs=None) -> Dict[str, str]:
    """``{"base": h, "<id>": h, "<id>-checksum": h}`` content hashes."""
    specs = specs if specs is not None else _specs(src_pkg)
    owners = ownership(src_pkg, specs)
    out = {"base": _hash(src_pkg, [r for r, w in owners.items() if not w],
                         "api=%d" % _api.MODULE_API)}
    # The version is NOT hashed: it is in the package's file name, so a bump
    # makes a new package anyway, and leaving it out lets the build tell
    # "files changed" apart from "version bumped" (see version_warnings).
    for mid, spec in specs.items():
        # a closed module's package also carries the module EULA, which lives
        # outside its files: a changed agreement must repackage it too (it did
        # not, and EDC16 1.0.8 shipped the agreement's draft text)
        # a compiled package is per interpreter and platform: building on
        # another must not count as up to date (nor as "files changed")
        tag = native_tag(spec)
        eula = "|native=" + tag if tag else ""
        if _api.terms(spec.license) == _api.EULA_TERMS:
            with open(_package.EULA_FILE, "rb") as fh:
                eula += "|eula=" + hashlib.sha256(fh.read()).hexdigest()
        out[mid] = _hash(src_pkg, _package.payload(src_pkg, spec, _api.FULL),
                         "api=%d|full%s" % (spec.module_api, eula))
        if spec.checksum:
            out[mid + "-checksum"] = _hash(
                src_pkg, _package.payload(src_pkg, spec, _api.CHECKSUM_ONLY),
                "api=%d|checksum%s" % (spec.module_api, eula))
    return out


def stamp_entry(stamps: Dict, key: str) -> Tuple[str, str]:
    """``(fingerprint, version)`` last packaged for ``key`` (older caches
    stored the fingerprint alone)."""
    v = stamps.get(key)
    if isinstance(v, dict):
        return v.get("fp", ""), v.get("version", "")
    return (v or ""), ""


def version_warning(stamps: Dict, key: str, fp: str, version: str) -> str:
    """A line when ``key``'s files changed since it was last packaged but its
    version did not ("" otherwise): two different packages would carry the
    same version number."""
    old_fp, old_ver = stamp_entry(stamps, key)
    if old_fp and old_ver and old_fp != fp and old_ver == version:
        return ("%s: its files changed since %s was packaged, but its version is "
                "still %s -- bump the version in %s/module.py before releasing"
                % (key, version, version, key.split("-")[0]))
    return ""


def native_tag(spec) -> str:
    """``cp313-win_amd64`` for a module whose packages ship compiled code
    (see :mod:`.package`), "" for one that ships source."""
    if not _package.is_native(spec):
        return ""
    return "%s-%s" % (_package.python_tag(), _package.platform_tag())


def package_name(spec, variant: str) -> str:
    """``<id>-<version>[-checksum][-cp313-win_amd64].ovtmod`` -- a compiled
    package names the interpreter and platform it is for, so the Windows and
    the Linux build of one version can sit side by side (in dist/modules,
    among a release's assets)."""
    py, _, plat = native_tag(spec).partition("-")
    return _package.file_name(spec.id, spec.version, variant, py, plat)


def pack(src_pkg: str, out_dir: str, mid: str, variant: str,
         key_path: Optional[str], app: str, specs=None,
         cache_dir: Optional[str] = None, report: Optional[dict] = None,
         native: Optional[bool] = None) -> str:
    """Build one package; ``report`` receives the files a compiled package
    carries as byte-code instead, ``native`` overrides whether it is
    compiled (see :func:`.package.build`)."""
    specs = specs if specs is not None else _specs(src_pkg)
    spec = specs[mid]
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, package_name(spec, variant))
    _package.build(spec, src_pkg, out, variant, key_path, app=app,
                   native=native, cache_dir=cache_dir, report=report)
    return out


# ------------------------------------------------------------------ verify
_CHECK = r'''
import os, sys
from {pkg}.modules import installer, registry
installer.install(sys.argv[1], scope=sys.argv[2], allow_unsigned=True)
from {pkg}.modules import loader
loader.mount_installed()
spec = registry.spec(sys.argv[3])
assert spec is not None, registry.problems()
assert spec.variant == sys.argv[4], spec.variant
hook = spec.resolve("checksum")             # imports the checksum code
cs = hook(None)                              # None: needs a session (HVAC)
if cs is not None:
    cs.is_supported(b"\xff" * 0x100)        # reach the checksum code itself
man = loader.manifest(sys.argv[3])
if man.platform:
    # a compiled package: no source in it, and its code is what really ran
    root = os.path.join(sys.argv[2], sys.argv[3], sys.argv[3])
    src = [os.path.join(d, n) for d, _, ns in os.walk(root) for n in ns
           if n.endswith(".py")]
    assert not src, "source in a compiled package: %s" % src
    from importlib.machinery import EXTENSION_SUFFIXES as EXT
    # what was loaded from the install folder (a module may alias itself to
    # base-app code: sys.modules[__name__] = <the base module>)
    top = os.path.normcase(os.path.realpath(root)) + os.sep
    ran = dict((k, getattr(v, "__file__", "") or "") for k, v in list(sys.modules.items())
               if k.startswith("{pkg}.%s." % sys.argv[3]))
    ran = dict((k, f) for k, f in ran.items()
               if os.path.normcase(os.path.realpath(f)).startswith(top))
    assert ran, "no module code was imported"
    bad = [k for k, f in ran.items() if not f.endswith(tuple(EXT) + (".pyc",))]
    assert not bad, "not compiled: %s" % bad
    print("compiled: %d of %d imported modules native"
          % (sum(f.endswith(tuple(EXT)) for f in ran.values()), len(ran)))
print("ok")
'''


def check_package(base_parent: str, pkg_name: str, ovtmod: str, mid: str,
                  variant: str) -> Tuple[bool, str]:
    """Install ``ovtmod`` into a scratch folder of a base-only app staged at
    ``base_parent/<pkg_name>`` and exercise its checksum hook in a fresh
    interpreter. ``(ok, output)``."""
    base_parent, ovtmod = os.path.abspath(base_parent), os.path.abspath(ovtmod)
    with tempfile.TemporaryDirectory() as mods:
        from .installer import ENV_DIRS
        # no byte-code written into the staged base: it is the tree the next
        # stage-base updates, and files a check just wrote are what Windows
        # keeps locked
        env = dict(os.environ, PYTHONPATH=base_parent, PYTHONDONTWRITEBYTECODE="1",
                   OVT_ALLOW_UNSIGNED="1", **{ENV_DIRS: mods})
        env.pop("OVT_DISABLE_MODULES", None)
        # The user's own modules folder (installer.user_dir) must not be seen:
        # a module installed there is mounted first, so the check tested THAT
        # copy -- a full EDC16 installed for the user failed the checksum-only
        # package's check, and would pass a broken full one (Bear's build,
        # 2026-10-13). Point every platform's per-user location at the scratch
        # folder, where nothing but the package under test is installed.
        home = os.path.join(mods, "_home")
        env.update(LOCALAPPDATA=home, XDG_DATA_HOME=home)
        if sys.platform == "darwin":
            env["HOME"] = home
        r = subprocess.run(
            [sys.executable, "-c", _CHECK.format(pkg=pkg_name),
             ovtmod, mods, mid, variant],
            env=env, capture_output=True, text=True, timeout=300, cwd=mods)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0 and out.endswith("ok"), out


def stamp_load(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def stamp_save(path: str, stamps: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stamps, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
