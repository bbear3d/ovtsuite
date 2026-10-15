#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The app's release files for delta updates (docs/UPDATES.md).

    python tools/app_release.py manifest DIST_DIR --version V [--unsigned]
        write app-manifest.json (+ .sig) into the built app folder: the
        SHA-256 of every file the installer ships (not modules\\)

    python tools/app_release.py archive DIST_DIR [--to Reference/AppReleases]
        keep the build's manifest as <to>/<version>/app-manifest.json -- what
        the next release's delta is made against. A later build of the same
        version replaces it (the last build of a version is the one released)

    python tools/app_release.py delta --to DIST_DIR --out dist
            [--from VERSION_OR_PATH]... [--archive Reference/AppReleases]
            [--unsigned]
        OVTSuite-Update-<from>-to-<to>.zip for each --from (an archived
        version, or a manifest file or folder); without --from, from the
        newest archived version older than the build. Unsigned deltas are
        useless (the app refuses them), so --unsigned or a missing key only
        warns and makes none.

    python tools/app_release.py previous --version V [--archive ...]
        print the newest archived release older than V (nothing if none)

    python tools/app_release.py auto DIST_DIR --out dist [--archive ...]
            [--from VERSION_OR_PATH]... [--unsigned]
        what build.ps1 runs after every one-folder build: remove the delta
        packages a previous build left in --out, make the delta from the
        newest archived release older than this build (or each --from;
        'none' for no delta), then archive this build's manifest -- replacing
        the archived one when the same version is built again. The first
        release with nothing older archived only archives. --unsigned (or no
        key) makes no delta and archives nothing: an unsigned build is not a
        release.

"The newest archived release older than V" is the highest version in the
archive that compares lower than V (updates.compare_versions: numeric, 0.4.10
after 0.4.9), never V itself, and never a prerelease: the app chains deltas
only through non-prerelease releases (delta_update.delta_hops), so a delta
from a prerelease would strand everyone on the release before it.

The signing key is the module key (OVT_SIGNING_KEY, default
~/.ovt-signing/module-key.pem), the one SHA256SUMS.txt is signed with.
"""

import argparse
import functools
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ovtsuite import delta_update as du                         # noqa: E402
from ovtsuite import updates                                    # noqa: E402
from ovtsuite.modules import signing                            # noqa: E402

DEFAULT_ARCHIVE = os.path.join(ROOT, "Reference", "AppReleases")


def _key(unsigned: bool, what: str):
    if unsigned:
        return None
    path = os.environ.get(signing.ENV_KEY) or signing.default_key_path()
    if not os.path.exists(path):
        print("WARNING: no signing key at %s -- %s not signed (run "
              "'build_modules.py ensure-key')" % (path, what))
        return None
    return path


def archived_versions(archive: str):
    """The versions archived in ``archive`` (a folder per version holding its
    manifest), oldest first, ordered as updates.compare_versions orders them."""
    try:
        names = os.listdir(archive)
    except OSError:
        return []
    out = []
    for n in names:
        if os.path.isfile(os.path.join(archive, n, du.MANIFEST_NAME)):
            try:
                updates.parse_version(n)
            except ValueError:
                continue
            out.append(n)
    return sorted(out, key=functools.cmp_to_key(updates.compare_versions))


def is_prerelease(version: str) -> bool:
    return updates.parse_version(version)[1]


def previous_version(archive: str, version: str) -> str:
    """The release a build of ``version`` makes its delta from: the newest
    archived non-prerelease version strictly older than it ("" if none). A
    rebuild of ``version`` never picks its own archived manifest."""
    older = [v for v in archived_versions(archive)
             if not is_prerelease(v) and updates.compare_versions(v, version) < 0]
    return older[-1] if older else ""


def cmd_manifest(args):
    key = _key(args.unsigned, du.MANIFEST_NAME)
    man = du.write_manifest(args.dist, args.version, key)
    print("%s: %s %s, %d files%s" % (os.path.join(args.dist, du.MANIFEST_NAME), man.app,
                                     man.version, len(man.files),
                                     ", signed" if key else ", NOT signed"))
    return 0


def archive_manifest(dist: str, archive: str) -> str:
    """Keep the manifest of the build in ``dist`` as ``<archive>/<version>/``,
    replacing what an earlier build of the same version left there. Returns
    the folder."""
    man, _raw, sig = du.read_manifest(dist)
    dest = os.path.join(archive, man.version)
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, du.MANIFEST_NAME + ".tmp")
    shutil.copyfile(os.path.join(dist, du.MANIFEST_NAME), tmp)
    os.replace(tmp, os.path.join(dest, du.MANIFEST_NAME))
    sp = os.path.join(dest, du.MANIFEST_SIG)
    if sig is not None:
        shutil.copyfile(os.path.join(dist, du.MANIFEST_SIG), sp)
    elif os.path.exists(sp):
        os.remove(sp)
    return dest


def cmd_archive(args):
    man, _raw, _sig = du.read_manifest(args.dist)
    dest = archive_manifest(args.dist, args.to)
    print("kept %s %s as a release -> %s" % (man.app, man.version, dest))
    return 0


def _from_path(spec: str, archive: str) -> str:
    if os.path.exists(spec):
        return spec
    p = os.path.join(archive, spec)
    if os.path.isfile(os.path.join(p, du.MANIFEST_NAME)):
        return p
    sys.exit("no archived release %s (looked in %s)" % (spec, archive))


def cmd_delta(args):
    new, _raw, _sig = du.read_manifest(args.to)
    froms = [f for f in (args.frm or []) if f and f.lower() != "none"]
    if args.frm and not froms:
        print("delta packages: none asked for")
        return 0
    if not froms:
        prev = previous_version(args.archive, new.version)
        if not prev:
            print("delta packages: no archived release older than %s in %s -- none made"
                  % (new.version, args.archive))
            return 0
        froms = [prev]
    key = _key(args.unsigned, "the delta packages are")
    if key is None:
        print("WARNING: delta packages are not made unsigned (the app refuses them)")
        return 0
    for f in froms:
        path, info = du.build_delta(_from_path(f, args.archive), args.to, args.out, key)
        size = os.path.getsize(path)
        print("  %s: %d changed, %d deleted (%.1f MB)" % (
            os.path.basename(path), len(info["changed"]), len(info["deleted"]),
            size / 1048576.0))
    return 0


def cmd_previous(args):
    v = previous_version(args.archive, args.version)
    if v:
        print(v)
    return 0


def stale_deltas(out: str):
    """Every delta package in ``out`` (a build folder): an earlier build's,
    of this version or another -- the new build makes its own."""
    try:
        names = os.listdir(out)
    except OSError:
        return []
    return sorted(os.path.join(out, n) for n in names if du.parse_delta_name(n))


def plan_auto(archive: str, version: str, frm=None):
    """``(froms, note)``: the releases a build of ``version`` makes deltas
    from, and why when there are none. ``frm``: the versions or paths asked
    for (build.ps1 -DeltaFrom); 'none' for no delta. A version that is not
    older than the build is dropped -- a delta from V to V, or backwards,
    is never made."""
    asked = [part.strip() for item in (frm or []) for part in item.split(",")
             if part.strip()]
    if asked and all(f.lower() == "none" for f in asked):
        return [], "none asked for"
    asked = [f for f in asked if f.lower() != "none"]
    if asked:
        froms = []
        for f in asked:
            try:
                if updates.compare_versions(f, version) >= 0:
                    print("WARNING: no delta from %s: it is not older than %s" % (f, version))
                    continue
            except ValueError:
                pass                                # a manifest file or folder
            froms.append(f)
        return froms, ("" if froms else "none of %s is older than %s"
                       % (", ".join(asked), version))
    prev = previous_version(archive, version)
    if not prev:
        return [], ("no release older than %s is archived in %s -- %s is the first "
                    "release with an archived manifest; deltas start with the next one"
                    % (version, archive, version))
    return [prev], ""


def cmd_auto(args):
    new, _raw, _sig = du.read_manifest(args.dist)
    for p in stale_deltas(args.out):
        os.remove(p)
        print("removed an earlier build's %s" % os.path.basename(p))
    key = _key(args.unsigned, "the delta packages are")
    if key is None:
        print("delta packages: none for an unsigned build (the app refuses them); "
              "%s not archived as a release" % new.version)
        return 0
    rebuild = os.path.isfile(os.path.join(args.archive, new.version, du.MANIFEST_NAME))
    froms, note = plan_auto(args.archive, new.version, args.frm)
    if note:
        print("delta packages: %s" % note)
    for f in froms:
        path, info = du.build_delta(_from_path(f, args.archive), args.dist, args.out, key)
        print("  %s: %d changed, %d deleted (%.1f MB)" % (
            os.path.basename(path), len(info["changed"]), len(info["deleted"]),
            os.path.getsize(path) / 1048576.0))
    dest = archive_manifest(args.dist, args.archive)
    print("%s %s %s -> %s" % ("replaced the archived" if rebuild else "archived",
                              new.app, new.version, dest))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("manifest")
    s.add_argument("dist")
    s.add_argument("--version", required=True)
    s.add_argument("--unsigned", action="store_true")
    s.set_defaults(fn=cmd_manifest)
    s = sub.add_parser("archive")
    s.add_argument("dist")
    s.add_argument("--to", default=DEFAULT_ARCHIVE)
    s.set_defaults(fn=cmd_archive)
    s = sub.add_parser("delta")
    s.add_argument("--to", required=True, help="the built app folder (with its manifest)")
    s.add_argument("--from", dest="frm", action="append",
                   help="an archived version, or a manifest file/folder (repeatable)")
    s.add_argument("--out", required=True)
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.add_argument("--unsigned", action="store_true")
    s.set_defaults(fn=cmd_delta)
    s = sub.add_parser("auto")
    s.add_argument("dist", help="the built app folder (with its manifest)")
    s.add_argument("--out", required=True)
    s.add_argument("--from", dest="frm", action="append",
                   help="an archived version, a manifest file/folder, or 'none' "
                        "(repeatable, or comma-separated)")
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.add_argument("--unsigned", action="store_true")
    s.set_defaults(fn=cmd_auto)
    s = sub.add_parser("previous")
    s.add_argument("--version", required=True)
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.set_defaults(fn=cmd_previous)
    args = ap.parse_args(argv)
    for k in ("dist", "to", "out", "archive"):
        if getattr(args, k, None):
            setattr(args, k, os.path.abspath(getattr(args, k)))
    try:
        return args.fn(args) or 0
    except (du.DeltaError, OSError) as ex:
        sys.exit("app_release %s: %s" % (args.cmd, ex))


if __name__ == "__main__":
    sys.exit(main())
