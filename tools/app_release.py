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
        keep the build's manifest as <to>/<version>/app-manifest.json (the
        version's last build) and under <to>/<version>/builds/<time>/ (every
        build, so the one that was published can be found later)

    python tools/app_release.py sync [--archive ...]
        find the releases published on GitHub and mark each as released in
        the archive: <version>/released/ gets the manifest of the last build
        archived before the release's installer was uploaded. A version
        re-released (its installer uploaded again) is marked again, from the
        build before the new upload. Run by 'auto'; harmless to repeat.

    python tools/app_release.py mark-released VERSION [--build TIME] [--archive ...]
        mark a version released by hand (no network, or a release published
        elsewhere): from build TIME (a folder under builds/), else the last
        build

    python tools/app_release.py releases [--archive ...]
        list the archive: each version's builds and which one was released

    python tools/app_release.py delta --to DIST_DIR --out dist
            [--from VERSION_OR_PATH]... [--archive Reference/AppReleases]
            [--unsigned]
        OVTSuite-Update-<from>-to-<to>.zip for each --from (an archived
        version, or a manifest file or folder); without --from, from the
        newest RELEASED version older than the build. Unsigned deltas are
        useless (the app refuses them), so --unsigned or a missing key only
        warns and makes none.

    python tools/app_release.py previous --version V [--archive ...]
        print the newest released version older than V (nothing if none)

    python tools/app_release.py auto DIST_DIR --out dist [--archive ...]
            [--from VERSION_OR_PATH]... [--unsigned]
        what build.ps1 runs after every one-folder build: sync the published
        releases (above; --offline skips it), remove the delta packages a
        previous build left in --out, make the delta from the newest
        RELEASED version older than this build (or each --from; 'none' for
        no delta), then archive this build's manifest. A version that was
        built but never published is never a delta's base: users cannot
        have it. --unsigned (or no key) makes no delta and archives nothing:
        an unsigned build is not a release.

"The newest released version older than V" is the highest version in the
archive marked released (see sync) that compares lower than V (updates.compare_versions: numeric, 0.4.10
after 0.4.9), never V itself, and never a prerelease: the app chains deltas
only through non-prerelease releases (delta_update.delta_hops), so a delta
from a prerelease would strand everyone on the release before it.

The signing key is the module key (OVT_SIGNING_KEY, default
~/.ovt-signing/module-key.pem), the one SHA256SUMS.txt is signed with.
"""

import argparse
import calendar
import functools
import json
import os
import shutil
import sys
import time

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


RELEASED = "released"              # <version>/released/: the manifest as published
RELEASE_INFO = "release.json"       # in it: when, from which build, how it is known
BUILDS = "builds"                   # <version>/builds/<time>/: every build's manifest


def released_dir(archive: str, version: str) -> str:
    return os.path.join(archive, version, RELEASED)


def release_info(archive: str, version: str) -> dict:
    """What marked ``version`` released ({} when it is not)."""
    d = released_dir(archive, version)
    if not os.path.isfile(os.path.join(d, du.MANIFEST_NAME)):
        return {}
    try:
        with open(os.path.join(d, RELEASE_INFO), encoding="utf-8") as fh:
            return json.load(fh) or {"source": "?"}
    except (OSError, ValueError):
        return {"source": "?"}


def released_versions(archive: str):
    """The archived versions marked released, oldest first."""
    return [v for v in archived_versions(archive) if release_info(archive, v)]


def previous_version(archive: str, version: str) -> str:
    """The release a build of ``version`` makes its delta from: the newest
    RELEASED non-prerelease version strictly older than it ("" if none). A
    version that was built (and archived) but never published is skipped --
    nobody can be running it -- and a rebuild of ``version`` never picks its
    own manifest."""
    older = [v for v in released_versions(archive)
             if not is_prerelease(v) and updates.compare_versions(v, version) < 0]
    return older[-1] if older else ""


def unreleased_between(archive: str, prev: str, version: str):
    """Versions archived between ``prev`` and ``version`` that were never
    released (worth a line in the build log: they are skipped)."""
    out = []
    for v in archived_versions(archive):
        if release_info(archive, v):
            continue
        if updates.compare_versions(v, version) < 0 and (
                not prev or updates.compare_versions(v, prev) > 0):
            out.append(v)
    return out


def cmd_manifest(args):
    key = _key(args.unsigned, du.MANIFEST_NAME)
    man = du.write_manifest(args.dist, args.version, key)
    print("%s: %s %s, %d files%s" % (os.path.join(args.dist, du.MANIFEST_NAME), man.app,
                                     man.version, len(man.files),
                                     ", signed" if key else ", NOT signed"))
    return 0


def _copy_manifest(src_dir: str, dest: str) -> None:
    """``src_dir``'s manifest (and signature, or none) into ``dest``."""
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, du.MANIFEST_NAME + ".tmp")
    shutil.copyfile(os.path.join(src_dir, du.MANIFEST_NAME), tmp)
    os.replace(tmp, os.path.join(dest, du.MANIFEST_NAME))
    sp = os.path.join(dest, du.MANIFEST_SIG)
    if os.path.isfile(os.path.join(src_dir, du.MANIFEST_SIG)):
        shutil.copyfile(os.path.join(src_dir, du.MANIFEST_SIG), sp)
    elif os.path.exists(sp):
        os.remove(sp)


def _stamp(t: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def _parse_stamp(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y%m%dT%H%M%SZ")))


def archive_manifest(dist: str, archive: str, now: float = None) -> str:
    """Keep the manifest of the build in ``dist`` as ``<archive>/<version>/``
    (the version's last build) and as ``<version>/builds/<time>/`` (every
    build: which one was published is settled when it is -- see
    :func:`sync`). A version's ``released/`` manifest is never touched here.
    Returns the version's folder."""
    man, _raw, _sig = du.read_manifest(dist)
    dest = os.path.join(archive, man.version)
    _copy_manifest(dist, dest)
    t = time.time() if now is None else now
    b = os.path.join(dest, BUILDS, _stamp(t))
    while os.path.exists(b):                       # two builds in one second
        t += 1
        b = os.path.join(dest, BUILDS, _stamp(t))
    _copy_manifest(dist, b)
    return dest


def builds(archive: str, version: str):
    """``[(time, folder)]`` of every archived build of ``version``, oldest
    first. An archive from before builds were kept has only the version's
    own manifest: that counts as one build, at its file's time."""
    out = []
    bd = os.path.join(archive, version, BUILDS)
    try:
        names = os.listdir(bd)
    except OSError:
        names = []
    for n in names:
        if os.path.isfile(os.path.join(bd, n, du.MANIFEST_NAME)):
            try:
                out.append((_parse_stamp(n), os.path.join(bd, n)))
            except ValueError:
                continue
    if not out:
        top = os.path.join(archive, version)
        f = os.path.join(top, du.MANIFEST_NAME)
        if os.path.isfile(f):
            out.append((os.path.getmtime(f), top))
    return sorted(out)


def mark_released(archive: str, version: str, published: float = None,
                  source: str = "manual", build: str = "") -> str:
    """Mark ``version`` released: ``released/`` gets the manifest of build
    ``build`` (a builds/ folder name), else of the last build archived
    before ``published`` (a time; None: the last build). Returns the build's
    name; raises ``du.DeltaError`` when there is no such build."""
    bs = builds(archive, version)
    if build:
        pick = [b for b in bs if os.path.basename(b[1]) == build]
    elif published is not None:
        pick = [b for b in bs if b[0] <= published]
    else:
        pick = bs
    if not pick:
        raise du.DeltaError(
            "no archived build of %s %s" % (version, ("named %s" % build) if build else
                                             "from before its release" if published
                                             else "at all"))
    t, folder = pick[-1]
    dest = released_dir(archive, version)
    _copy_manifest(folder, dest)
    info = {"version": version, "source": source,
            "build": os.path.basename(folder) if os.path.basename(folder) != version
            else "(the version's manifest)", "built": _stamp(t)}
    if published is not None:
        info["published"] = _stamp(published)
    with open(os.path.join(dest, RELEASE_INFO), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    return info["build"]


def _iso(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def published_releases(get=None, max_pages: int = 10):
    """``[(version, time)]`` of every published (non-draft) release on
    GitHub. The time is when its app installer was uploaded -- uploaded again
    for a re-release of the same number -- else when it was published."""
    get = get or (lambda u: updates._get_json(u, 20.0))
    out = []
    for page in range(1, max_pages + 1):
        rows = get(updates.releases_url(page))
        if not isinstance(rows, list):
            raise updates.UpdateError("GitHub answered with something that is "
                                      "not a release list")
        for j in rows:
            if j.get("draft"):
                continue
            tag = str(j.get("tag_name") or "")
            v = tag[1:] if tag[:1] in ("v", "V") else tag
            try:
                updates.parse_version(v)
            except ValueError:
                continue
            when = None
            for a in j.get("assets") or []:
                n = str(a.get("name") or "")
                if n.startswith("OVTSuite-Base-") and n.endswith("-setup.exe") \
                        and a.get("updated_at"):
                    when = max(when or 0.0, _iso(a["updated_at"]))
            if when is None and j.get("published_at"):
                when = _iso(j["published_at"])
            if when is not None:
                out.append((v, when))
        if len(rows) < updates.PER_PAGE:
            break
    return out


def sync(archive: str, releases) -> list:
    """Mark every archived version published in ``releases`` (from
    :func:`published_releases`) released, from the build before its upload.
    One already marked for the same upload is left; one uploaded again (a
    re-release) is marked again. Returns lines for the build log."""
    log = []
    have = set(archived_versions(archive))
    for v, when in releases:
        if v not in have:
            continue
        info = release_info(archive, v)
        if info.get("source") == "github" and info.get("published") == _stamp(when):
            continue
        try:
            b = mark_released(archive, v, when, "github")
        except du.DeltaError as ex:
            log.append("WARNING: %s is published but %s; mark it with "
                       "'app_release.py mark-released %s --build ...'" % (v, ex, v))
            continue
        log.append("%s %s released (published %s, build %s)" % (
            "re-marked" if info else "marked", v, _stamp(when), b))
    return log


def cmd_archive(args):
    man, _raw, _sig = du.read_manifest(args.dist)
    dest = archive_manifest(args.dist, args.to)
    print("kept %s %s as a release -> %s" % (man.app, man.version, dest))
    return 0


def _from_path(spec: str, archive: str) -> str:
    """A version's manifest as released (or, with a warning, as last built
    when it was never marked released), or a manifest file / folder."""
    if os.path.exists(spec):
        return spec
    r = released_dir(archive, spec)
    if os.path.isfile(os.path.join(r, du.MANIFEST_NAME)):
        return r
    p = os.path.join(archive, spec)
    if os.path.isfile(os.path.join(p, du.MANIFEST_NAME)):
        print("WARNING: %s is not marked released; the delta is made from its "
              "last build" % spec)
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
        return [], ("no released version older than %s is archived in %s -- %s is "
                    "the first release with an archived manifest (or none is marked "
                    "released: 'app_release.py sync'); deltas start with the next one"
                    % (version, archive, version))
    return [prev], ""


def _sync_now(archive: str, offline: bool) -> None:
    if offline:
        print("releases: not synced (--offline); using what the archive knows")
        return
    try:
        rels = published_releases()
    except updates.UpdateError as ex:
        print("WARNING: could not read the published releases (%s); using what "
              "the archive knows" % ex)
        return
    for line in sync(archive, rels):
        print(line)


def cmd_sync(args):
    _sync_now(args.archive, False)
    return cmd_releases(args)


def cmd_mark(args):
    b = mark_released(args.archive, args.version, None, "manual", args.build or "")
    print("marked %s released (build %s)" % (args.version, b))
    return 0


def cmd_releases(args):
    for v in archived_versions(args.archive):
        info = release_info(args.archive, v)
        bs = builds(args.archive, v)
        print("%-10s %s  (%d build%s)" % (
            v, ("released: build %s, published %s (%s)" % (
                info.get("build", "?"), info.get("published", "?"), info.get("source", "?"))
                if info else "not released"), len(bs), "" if len(bs) == 1 else "s"))
    return 0


def cmd_auto(args):
    new, _raw, _sig = du.read_manifest(args.dist)
    _sync_now(args.archive, getattr(args, "offline", False))
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
    if froms and not args.frm:
        skipped = unreleased_between(args.archive, froms[0], new.version)
        if skipped:
            print("delta packages: from %s, the last release -- %s %s built but "
                  "never released" % (froms[0], ", ".join(skipped),
                                      "was" if len(skipped) == 1 else "were"))
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
    s.add_argument("--offline", action="store_true",
                   help="do not read the published releases from GitHub first")
    s.set_defaults(fn=cmd_auto)
    s = sub.add_parser("sync")
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("mark-released")
    s.add_argument("version")
    s.add_argument("--build", help="a folder name under <version>/builds/")
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.set_defaults(fn=cmd_mark)
    s = sub.add_parser("releases")
    s.add_argument("--archive", default=DEFAULT_ARCHIVE)
    s.set_defaults(fn=cmd_releases)
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
