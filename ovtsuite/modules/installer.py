# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Installing and removing modules.

Where installed modules live (searched in this order; for one id the newest
version wins, and an in-tree package -- a source checkout -- beats both):

* ``OVTSUITE_MODULES_DIRS`` -- extra directories (os.pathsep-separated), for
  tests and development;
* the app's own ``modules`` folder, beside the executable (what the
  installers write; machine-wide);
* the per-user modules folder (what installing from inside the app writes,
  no administrator rights needed): ``%LOCALAPPDATA%\\<app>\\modules`` on
  Windows, ``~/Library/Application Support/<app>/modules`` on macOS,
  ``$XDG_DATA_HOME/<pkg>/modules`` (``~/.local/share``) elsewhere.

An installed module is a directory ``<modules dir>/<id>/`` holding its
``manifest.json``, its ``signature`` and the payload laid out like the app's
package directory (``<id>/...``, ``data/...``).

Installing checks the package completely BEFORE anything is written --
structure, product, module API level, signature, every file's hash -- then
extracts to a temporary folder beside the target and swaps it in, so a
failed install never leaves a half-written module behind.
"""

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional

from .. import branding
from . import api as _api
from . import package as _pkg
from . import signing as _sig

ENV_DIRS = branding.ENV_PREFIX + "_MODULES_DIRS"
SYSTEM, USER = "system", "user"


class InstallError(Exception):
    pass


# ------------------------------------------------------------------ places
def system_dir() -> Optional[str]:
    """The app's own modules folder (beside the executable) -- only for a
    built app; a source checkout's modules are its in-tree packages."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "modules")
    return None


def user_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, branding.CONFIG_DIR, "modules")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", branding.APP_NAME, "modules")
    base = (os.environ.get("XDG_DATA_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, branding.PKG, "modules")


def _user_roots() -> List[str]:
    """The folders that belong to the current user: the home folder, plus on
    Windows the profile and app-data folders (which a policy can move off
    the home drive)."""
    roots = [os.path.expanduser("~")]
    if os.name == "nt":
        roots += [os.environ.get(k) for k in ("USERPROFILE", "LOCALAPPDATA", "APPDATA")]
    return [r for r in roots if r]


def _is_within(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:                  # different drives (Windows)
        return False


def app_dir_is_per_user(app_dir: Optional[str] = None) -> bool:
    """True when the app itself is installed for the current user only -- its
    folder lies inside one of the user's own folders (a per-user Inno Setup
    install under ``%LOCALAPPDATA%\\Programs``, ``~/Applications`` on macOS, a
    copy in the home folder on Linux) -- so the modules in its own
    ``modules`` folder are the user's too, not "for all users". A machine-wide
    install (Program Files, /Applications, /opt) is False. ``app_dir``
    defaults to the folder of :func:`system_dir`."""
    if app_dir is None:
        sd = system_dir()
        if sd is None:
            return False
        app_dir = os.path.dirname(sd)
    return any(_is_within(app_dir, r) for r in _user_roots())


def scope_label(scope: str, root: str = "") -> str:
    """How the Module Manager names where a module is installed."""
    if scope == SYSTEM:
        app_dir = (os.path.dirname(os.path.dirname(os.path.abspath(root)))
                   if root else None)
        return ("for you (with the app)" if app_dir_is_per_user(app_dir)
                else "for all users")
    if scope == USER:
        return "for you"
    return root


def search_dirs() -> List[tuple]:
    """``[(scope, dir)]`` in search order."""
    out = [("extra", d) for d in os.environ.get(ENV_DIRS, "").split(os.pathsep) if d]
    sd = system_dir()
    if sd:
        out.append((SYSTEM, sd))
    out.append((USER, user_dir()))
    return out


def target_dir(scope: str) -> str:
    if scope == SYSTEM:
        d = system_dir()
        if d is None:
            raise InstallError("this is not an installed copy of %s; install "
                               "modules for the current user instead"
                               % branding.APP_NAME)
        return d
    if scope == USER:
        return user_dir()
    return scope                        # an explicit directory (tests)


# ------------------------------------------------------------------ checks
@dataclass
class Checked:
    manifest: "_pkg.Manifest"
    signed_by: str                      # key id, or "" when unsigned-allowed
    opened: "_pkg.Opened"


def check(path_or_bytes, allow_unsigned: Optional[bool] = None) -> Checked:
    """Everything that decides whether a package may be installed, with the
    user-facing reason when not."""
    from .. import platforms
    op = _pkg.open_package(path_or_bytes)
    man = op.manifest
    if man.app and man.app != branding.APP_NAME:
        raise InstallError("this module is for %s, not %s" % (man.app, branding.APP_NAME))
    if man.id not in platforms.MODULES:
        raise InstallError("%s does not know a module called %r -- it needs a "
                           "newer %s" % (branding.APP_NAME, man.id, branding.APP_NAME))
    if man.module_api not in _api.SUPPORTED_API:
        raise InstallError(
            "the %s module %s was built for module API %d; this %s supports %s"
            % (man.name, man.version, man.module_api, branding.APP_NAME,
               ", ".join(map(str, _api.SUPPORTED_API))))
    if man.variant not in (_api.FULL, _api.CHECKSUM_ONLY):
        raise InstallError("unknown module variant %r" % man.variant)
    # compiled code runs on one interpreter and platform only (package.py)
    why = _pkg.incompatible(man)
    if why:
        raise InstallError(why[0].upper() + why[1:])
    ok, who = _sig.verify(op.manifest_bytes, op.signature)
    if not ok:
        allow = _sig.allow_unsigned() if allow_unsigned is None else allow_unsigned
        if not allow:
            raise InstallError("the %s module is %s; it was refused (allow "
                               "unsigned modules in the Module Manager to "
                               "install development builds)" % (man.name, who))
        who = ""
    bad = _pkg.check_files(man, op.zip.read)
    if bad:
        raise InstallError("the package is damaged: %s" % bad)
    from . import licensing
    lic_ok, why = licensing.check(man)
    if not lic_ok:
        raise InstallError(why)
    return Checked(man, who, op)


# ------------------------------------------------------------------ install
def install(path_or_bytes, scope: str = USER,
            allow_unsigned: Optional[bool] = None) -> "_pkg.Manifest":
    """Install (or replace) a module from a .ovtmod. Returns its manifest."""
    c = check(path_or_bytes, allow_unsigned)
    root = target_dir(scope)
    os.makedirs(root, exist_ok=True)
    final = os.path.join(root, c.manifest.id)
    tmp = tempfile.mkdtemp(prefix=".%s-" % c.manifest.id, dir=root)
    try:
        for rel in c.manifest.files:
            dst = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(c.opened.zip.read(rel))
        with open(os.path.join(tmp, _pkg.MANIFEST), "wb") as fh:
            fh.write(c.opened.manifest_bytes)
        if c.opened.signature:
            with open(os.path.join(tmp, _pkg.SIGNATURE), "wb") as fh:
                fh.write(c.opened.signature)
        old = None
        if os.path.exists(final):
            old = final + ".old"
            if os.path.exists(old):
                shutil.rmtree(old, ignore_errors=True)
            os.replace(final, old)
        os.replace(tmp, final)
        tmp = None
        if old:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    if c.manifest.platform and scope in (SYSTEM, USER):
        # a compiled module replaces its source: no copy of the old one,
        # here or in another modules folder, may stay readable on disk.
        # Only for the real locations -- an explicit folder (a build's
        # install-into, a test) never reaches into the user's own.
        try:
            scrub_source(c.manifest.id)
        except Exception:                # noqa: BLE001 - installed is installed
            pass
    return c.manifest


def uninstall(module_id: str, scope: str = USER) -> bool:
    """Remove an installed module; False when it was not installed there."""
    d = os.path.join(target_dir(scope), module_id)
    if not os.path.isfile(os.path.join(d, _pkg.MANIFEST)):
        return False
    gone = d + ".removing"
    os.replace(d, gone)
    shutil.rmtree(gone, ignore_errors=True)
    return True


# ------------------------------------------------------------------ discovery
@dataclass
class Installed:
    manifest: "_pkg.Manifest"
    root: str                     # the directory laid out like the package dir
    scope: str
    signed_by: str
    problem: str = ""             # why it cannot be loaded ("" = fine)


def _version_key(v: str):
    out = []
    for part in v.replace("-", ".").split("."):
        out.append((0, int(part)) if part.isdigit() else (1, part))
    return out


def read_installed(root: str, scope: str, verify_files: bool = True) -> Optional[Installed]:
    """The module installed at ``root`` (``<modules dir>/<id>``), checked."""
    mpath = os.path.join(root, _pkg.MANIFEST)
    try:
        with open(mpath, "rb") as fh:
            raw = fh.read()
        man = _pkg.Manifest.from_bytes(raw)
    except (OSError, _pkg.PackageError) as ex:
        return None if isinstance(ex, OSError) else Installed(
            _pkg.Manifest(os.path.basename(root), os.path.basename(root), "?",
                          0, "?", [], [], {}), root, scope, "", str(ex))
    sig = None
    try:
        with open(os.path.join(root, _pkg.SIGNATURE), "rb") as fh:
            sig = fh.read()
    except OSError:
        pass
    inst = Installed(man, root, scope, "")
    from .. import platforms
    if man.id != os.path.basename(root):
        inst.problem = "installed under the wrong name"
    elif man.id not in platforms.MODULES:
        inst.problem = "unknown to this %s (needs a newer version)" % branding.APP_NAME
    elif man.module_api not in _api.SUPPORTED_API:
        inst.problem = ("built for module API %d; this %s supports %s"
                        % (man.module_api, branding.APP_NAME,
                           ", ".join(map(str, _api.SUPPORTED_API))))
    elif _pkg.incompatible(man):
        # a modules folder copied from another OS, or kept across an app
        # update to another Python
        inst.problem = "built for %s -- reinstall it for this app" % _pkg.tag_words(
            man.python, man.platform)
    else:
        ok, who = _sig.verify(raw, sig)
        if ok:
            inst.signed_by = who
        elif not _sig.allow_unsigned():
            inst.problem = "%s (unsigned modules are not allowed)" % who
    if not inst.problem and verify_files:
        def read(rel):
            with open(os.path.join(root, *rel.split("/")), "rb") as fh:
                return fh.read()
        bad = _pkg.check_files(man, read)
        if bad:
            inst.problem = "damaged: %s -- reinstall it" % bad
    if not inst.problem:
        from . import licensing
        ok, why = licensing.check(man)
        if not ok:
            inst.problem = why
    return inst


def discover(verify_files: bool = True) -> Dict[str, List[Installed]]:
    """Every installed module found, by id, best candidate first."""
    found: Dict[str, List[Installed]] = {}
    for scope, d in search_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            root = os.path.join(d, n)
            if n.startswith(".") or n.endswith((".old", ".removing")) \
                    or not os.path.isdir(root):
                continue
            inst = read_installed(root, scope, verify_files)
            if inst is not None:
                found.setdefault(inst.manifest.id, []).append(inst)
    for mid, lst in found.items():
        # at one version a compiled copy beats a source one (left in a folder
        # the user cannot write, until scrub_source can remove it)
        lst.sort(key=lambda i: (i.problem == "", _version_key(i.manifest.version),
                                bool(i.manifest.platform)),
                 reverse=True)
    return found


# ------------------------------------------------------------------ scrubbing
#: What is code in an installed module's package folder.
_CODE_SUFFIX = (".py", ".pyc", ".pyo")


def carries_source(man: "_pkg.Manifest") -> bool:
    """A closed-source module's copy from before its packages were compiled:
    readable Python source in a folder."""
    return (not man.platform and _api.terms(man.license) == _api.EULA_TERMS
            and any(f.endswith(".py") for f in man.files))


def _strip_stray(root: str, man: "_pkg.Manifest") -> List[str]:
    """Delete the code files under ``root/<id>/`` that ``man`` does not list
    (``.py``, ``.pyc``, ``__pycache__``) -- what an installer laying a compiled
    package over an old folder, or Python itself, left beside it. Data,
    help, the EULA and a module setup's own files are not code: kept."""
    gone = []
    keep = set(man.files)
    top = os.path.join(root, man.id)
    for d, dirs, names in os.walk(top, topdown=False):
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if (n.endswith(_CODE_SUFFIX) or os.path.basename(d) == "__pycache__") \
                    and rel not in keep:
                os.remove(p)
                gone.append(p)
        if os.path.basename(d) == "__pycache__" and not os.listdir(d):
            os.rmdir(d)
    return gone


def _remove_copy(inst: Installed) -> str:
    """Remove a superseded source copy; how it went."""
    root = inst.root
    if os.path.isfile(os.path.join(root, "unins000.exe")):
        # put there by the module's own setup.exe: its uninstaller (and the
        # Apps & features entry it serves) stays, its code goes -- the copy
        # is then "damaged" and never loads
        n = 0
        top = os.path.join(root, inst.manifest.id)
        for d, dirs, names in os.walk(top, topdown=False):
            for f in names:
                if f.endswith(_CODE_SUFFIX):
                    os.remove(os.path.join(d, f))
                    n += 1
            if os.path.basename(d) == "__pycache__" and not os.listdir(d):
                os.rmdir(d)
        return "source removed (%d files; its uninstaller stays)" % n
    gone = root + ".removing"
    os.replace(root, gone)
    shutil.rmtree(gone, ignore_errors=True)
    if os.path.exists(gone):
        return "moved aside, not fully deleted: %s" % gone
    return "removed"


def scrub_source(module_id: str, found: Optional[Dict[str, List[Installed]]] = None,
                 dry_run: bool = False) -> List[tuple]:
    """Once a compiled copy of ``module_id`` is installed, delete every copy
    of it that still carries Python source, in every modules folder
    (:func:`search_dirs`), and any stray code beside the compiled copy.

    ``found`` is :func:`discover`'s result when the caller has it. Returns
    ``[(path, what happened)]``; a folder this user cannot write is reported
    (``"no write access: ..."``), never raised -- the installer runs this
    again elevated (``scrub-module-sources``). Nothing happens while no
    compiled, loadable copy exists: the source copy is then the module."""
    found = discover() if found is None else found
    cands = found.get(module_id, [])
    compiled = [c for c in cands if c.manifest.platform and not c.problem]
    if not compiled:
        return []
    out = []
    for c in cands:
        try:
            if carries_source(c.manifest):
                out.append((c.root, "would remove" if dry_run else _remove_copy(c)))
            elif c in compiled:
                if dry_run:
                    continue
                for p in _strip_stray(c.root, c.manifest):
                    out.append((p, "stray code removed"))
        except OSError as ex:
            out.append((c.root, "no write access: %s" % ex))
    return out


def scrub_all(dry_run: bool = False) -> List[tuple]:
    """:func:`scrub_source` for every module installed."""
    found = discover()
    out = []
    for mid in sorted(found):
        out += scrub_source(mid, found, dry_run)
    return out
