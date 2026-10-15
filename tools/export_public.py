#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Publish the open-source parts of this repository, and keep licence headers.

This repository is private and holds everything: the base app (open source,
MPL-2.0), the VDO cluster module (open source, MPL-2.0, its own repository)
and the closed-source control-unit modules (the module EULA,
``ovtsuite/modules/EULA.txt``). Its history carries all of them, so it is
never pushed anywhere public. This tool copies the open parts into separate
trees whose history starts fresh:

    python tools/export_public.py base DEST [--init] [--force]
        the base app: every file build.ownership() gives the base app, plus
        the root files, packaging, docs, tools and tests listed below

    python tools/export_public.py vdo DEST [--init] [--force]
        the VDO cluster module: ovtsuite/vdo/, the data it owns, its tests

    python tools/export_public.py headers [--check]
        put the SPDX licence header on every source file in THIS tree (MPL-2.0
        for base and open-module files, the EULA for closed-module files);
        --check lists the files that lack the right one and exits 1

    python tools/export_public.py list base|vdo
        print what an export would contain, and nothing else

An export copies the working tree's files (tracked, or new and not ignored),
gives every source file its header, and refuses to finish if anything that
must stay private would go out (see ``_leaks``). DEST must be empty, or an
earlier export (``--force`` replaces everything in it but ``.git``, so the
public clone can be refreshed and its changes reviewed with ``git diff``).
``--init`` makes DEST a new git repository with the export staged; nothing
is committed or pushed.

What to include outside ``ovtsuite/`` is listed here, not guessed: a file a
list does not name stays private. Review the "check before publishing" lines
an export prints; they name base files derived from reference material.
"""

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite import branding, credits                          # noqa: E402
from ovtsuite.modules import api, build                         # noqa: E402

PKG = branding.PKG
SRC_PKG = os.path.join(ROOT, PKG)

# --------------------------------------------------------------- what goes out
#: Files outside the package that the base export carries (fnmatch globs on
#: repository paths). The package itself goes by build.ownership().
BASE_FILES = [
    "LICENSE", "LEGAL.md", "README.md", "CONTRIBUTING.md", "AGENTS.md",
    "pyproject.toml", "build.ps1", "build_linux.sh",
    ".gitattributes", ".gitignore", ".github/FUNDING.yml",
    "packaging/*",
    "addons/README.md", "addons/addons.json",
    "docs/HELP.md", "docs/MODULES_PLAN.md", "docs/MODULE_SDK_DESIGN.md",
    "docs/OVT_CONTAINER.md", "docs/CONTAINERS.md", "docs/SEARCH.md",
    "docs/DEFINITION_CACHE.md", "docs/UPDATES.md", "docs/images/*",
    "tools/__init__.py", "tools/build_modules.py", "tools/collect_licenses.py",
    "tools/app_release.py", "tools/build_launchers.py",
    "tools/export_public.py", "tools/rename_package.py",
    "tools/make_file_icons.py", "tools/help_screenshots.py", "tools/help_shots/*",
    "tools/build_wiki.py", "tools/generate_layouts.py", "tools/build_search_index.py",
    "tests/conftest.py", "tests/mock_*.py", "tests/logtune_sim.py",
]

#: Tests the base export leaves out although they import no module package:
#: they need a module's data or behaviour to pass.
BASE_TESTS_EXCLUDE = [
    "tests/test_p350_u8a7_kb.py",
    "tests/test_aisin09d.py",
    "tests/test_datalog_memchan.py",       # reads a private probe tool
    "tests/test_export_public.py",
]

#: Files of the VDO export outside the module's own package and data.
VDO_FILES = [
    "tests/test_vdo.py", "tests/test_vdo_flash.py", "tests/test_vdo_sweep.py",
]
VDO_ID = "vdo"

#: Never published, whatever a list above says.
NEVER = [
    "*.bin", "*.ori", "*.frf", "*.sgo", "*.odx", "*.a2l", "*.xdf", "*.kp",
    "*.ols", "*.dam", "*.hex", "*.s19", "*.pem", "*.p12", "*.pfx", "*.tgz",
    "*.zip", "*.cab", "*.7z", "*.tmp",
    "Reference/*", "tunes/*", "generated/*", "_incoming/*", "_to_delete/*",
    "_session_backups/*", "Claude outputs/*", "dist/*", "build/*",
    "packaging/drivers/*",
]
#: Allowed despite NEVER (the .frf outer keystream, published by VW_Flash).
ALLOWED = ["%s/data/frf.key" % PKG]
#: Text that must never appear in an exported file.
SECRET_TEXT = re.compile(rb"-----BEGIN (?:ENCRYPTED |OPENSSH |RSA |EC )?PRIVATE KEY-----")

#: Base files that are fine to publish only if their source material allows:
#: printed as a reminder by every base export.
#: (The de_en_* label tables were reviewed and are published: Bear,
#: 2026-10-12.)
REVIEW = []

# --------------------------------------------------------------- headers
_URL = "https://mozilla.org/MPL/2.0/"
_YEAR = credits.FIRST_YEAR
MPL_HEADER = [
    "SPDX-License-Identifier: MPL-2.0",
    "Copyright (c) %d %s" % (_YEAR, credits.AUTHOR),
    "This Source Code Form is subject to the terms of the Mozilla Public",
    "License, v. 2.0. If a copy of the MPL was not distributed with this",
    "file, You can obtain one at %s." % _URL,
]
EULA_HEADER = [
    "SPDX-License-Identifier: %s" % api.EULA_TERMS,
    "Copyright (c) %d %s. All rights reserved." % (_YEAR, credits.AUTHOR),
    "Closed source: licensed under the module licence agreement",
    "(ovtsuite/modules/EULA.txt), not under the MPL.",
]
#: Comment marker per source type (a file of any other type gets no header).
COMMENT = {".py": "#", ".spec": "#", ".ps1": "#", ".sh": "#", ".iss": ";"}


def _known_header_lines():
    out = set()
    for lines in (MPL_HEADER, EULA_HEADER):
        for c in set(COMMENT.values()):
            out.update("%s %s" % (c, ln) for ln in lines)
    return out


def header_for(owner: str, specs) -> list:
    """MPL for the base app and open-source modules, else the EULA."""
    if owner and api.terms(specs[owner].license) == api.EULA_TERMS:
        return EULA_HEADER
    return MPL_HEADER


def with_header(text: str, lines: list, comment: str) -> str:
    """``text`` with exactly one licence header, after any shebang / encoding
    line. A header already there (either kind) is replaced, so moving a file
    between open and closed is one run."""
    nl = "\r\n" if "\r\n" in text else "\n"
    body = text.split(nl) if text else []
    top = []
    while body and len(top) < 2 and (body[0].startswith("#!") or re.match(
            r"#.*coding[:=]", body[0])):
        top.append(body.pop(0))
    known = _known_header_lines()
    while body and body[0] in known:
        body.pop(0)
    head = ["%s %s" % (comment, ln) for ln in lines]
    return nl.join(top + head + body) + ("" if body else nl)


def has_header(text: str, lines: list, comment: str) -> bool:
    want = ["%s %s" % (comment, ln) for ln in lines]
    got = text.splitlines()
    while got and (got[0].startswith("#!") or re.match(r"#.*coding[:=]", got[0])):
        got.pop(0)
    return got[:len(want)] == want


# --------------------------------------------------------------- selection
def _git_files() -> list:
    """Tracked files plus new ones git does not ignore, that exist on disk."""
    out = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT).decode("utf-8")
    return sorted(p for p in set(out.split("\0"))
                  if p and os.path.isfile(os.path.join(ROOT, p)))


def _match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def _module_import(specs):
    ids = "|".join(sorted(specs))
    pkgs = PKG
    return re.compile(
        r"\b(?:%s)[./](?:%s)\b|from\s+(?:%s|\.\.?)\s+import\s+[^\n]*\b(?:%s)\b"
        % (pkgs, ids, PKG, ids))


def select(which: str):
    """(paths to export, {path: owner}) for 'base' or 'vdo'."""
    specs = build._specs(SRC_PKG)
    owners = build.ownership(SRC_PKG, specs)
    files = _git_files()
    out = []
    pkg_owner = {}
    imports = _module_import(specs)
    for p in files:
        if p.startswith(PKG + "/"):
            rel = p[len(PKG) + 1:]
            if rel not in owners:
                continue                       # __pycache__, .tmp: not walked
            owner = owners[rel]
            pkg_owner[p] = owner
            if (which == "base" and not owner) or (which == VDO_ID and owner == VDO_ID):
                out.append(p)
            continue
        if which == VDO_ID:
            if _match(p, VDO_FILES):
                out.append(p)
                pkg_owner[p] = VDO_ID
            continue
        if p.startswith("tests/") and p.endswith(".py") and not _match(p, BASE_FILES):
            if p in BASE_TESTS_EXCLUDE:
                continue
            with open(os.path.join(ROOT, p), encoding="utf-8", errors="replace") as fh:
                if imports.search(fh.read()):
                    continue
            out.append(p)
            pkg_owner[p] = ""
            continue
        if _match(p, BASE_FILES):
            out.append(p)
            pkg_owner[p] = ""
    return sorted(out), pkg_owner, specs


def _leaks(which: str, paths, owners) -> list:
    """Everything that must not go out in this export."""
    bad = []
    for p in paths:
        if _match(p, NEVER) and p not in ALLOWED:
            bad.append("%s: a private kind of file" % p)
        owner = owners.get(p, "")
        if which == "base" and owner:
            bad.append("%s: belongs to the %s module" % (p, owner))
        if which == VDO_ID and owner != VDO_ID:
            bad.append("%s: not part of the VDO module" % p)
        with open(os.path.join(ROOT, p), "rb") as fh:
            if SECRET_TEXT.search(fh.read()):
                bad.append("%s: contains a private key" % p)
    return bad


def _warnings(which: str, paths) -> list:
    out = []
    if not branding.SOURCE_URL:
        out.append("branding.SOURCE_URL is empty: set it to the public repository's "
                   "URL (MPL-2.0 section 3.2 needs the binaries to say where the "
                   "source is)")
    eula = os.path.join(SRC_PKG, "modules", "EULA.txt")
    with open(eula, encoding="utf-8") as fh:
        if "[JURISDICTION]" in fh.read():
            out.append("ovtsuite/modules/EULA.txt still says [JURISDICTION]")
    if which == "base":
        for glob, why in REVIEW:
            hits = [p for p in paths if fnmatch.fnmatchcase(p, glob)]
            if hits:
                out.append("check before publishing: %s (%d file(s)) -- %s"
                           % (glob, len(hits), why))
    return out


# --------------------------------------------------------------- writing
def _clear(dest: str) -> None:
    """Every file in ``dest`` but ``.git`` goes; then the folders left empty.
    A folder Windows will not remove (Explorer, a sync client or an antivirus
    holding it open: "Access is denied") is left empty -- the copy refills
    it, and git does not track empty folders anyway."""
    for name in os.listdir(dest):
        if name == ".git":
            continue
        p = os.path.join(dest, name)
        if os.path.isdir(p) and not os.path.islink(p):
            for d, _dirs, files in os.walk(p):
                for f in files:
                    os.remove(os.path.join(d, f))
            for d, _dirs, _files in sorted(os.walk(p), key=lambda w: -len(w[0])):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        else:
            os.remove(p)


def _copy(paths, owners, specs, dest: str) -> None:
    for p in paths:
        src = os.path.join(ROOT, p)
        dst = os.path.join(dest, *p.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ext = os.path.splitext(p)[1]
        if ext in COMMENT:
            with open(src, encoding="utf-8", newline="") as fh:
                text = fh.read()
            lines = header_for(owners.get(p, ""), specs)
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(with_header(text, lines, COMMENT[ext]))
        else:
            shutil.copy2(src, dst)


VDO_README = """# OVT-Suite VDO Cluster module

The VDO instrument-cluster module for [OVT-Suite]({source}): cluster EEPROM
definitions and checksums, the speedometer and needle-sweep tools, and the
sweep-patch library.

## Licence

This module is free software under the Mozilla Public License 2.0 (`LICENSE`,
and `ovtsuite/vdo/LICENSE.txt`, which travels in the built package).

The needle-sweep patches in `ovtsuite/data/cluster_sweep_patches.json` are
third-party work, published unchanged under their authors' own licences
(LGPL-3.0, MIT, Apache-2.0, or freely with permission); each patch names its
author, licence and source, and the licence texts are the
`ovtsuite/data/cluster_sweep_LICENSE-*.txt` files beside it. Those licences,
not the MPL, govern the patches. They run on the cluster, not in the app.

## Building

The module is laid out exactly as it sits inside the OVT-Suite source tree.
Copy this repository's files over a checkout of the base app (same paths),
then:

    python tools/build_modules.py packages --modules vdo --unsigned

That writes `dist/modules/vdo-<version>.ovtmod`. A module you build is not
signed with the official key, so the app loads it only with **Allow unsigned
modules (development)** ticked in Tools > Modules... (or `OVT_ALLOW_UNSIGNED=1`).

Run its tests from the combined tree:

    python -m pytest tests/test_vdo.py tests/test_vdo_sweep.py tests/test_vdo_flash.py -q

## Trademarks and legal use

VDO and Continental, Volkswagen and Audi are trademarks of their owners; this
project is not affiliated with or endorsed by them. Editing a cluster's EEPROM
can make it unusable and can change the odometer; altering a vehicle's recorded
mileage is illegal in most countries. See `LEGAL.md` in the base app.
"""


def _write_vdo_extras(dest: str) -> None:
    with open(os.path.join(dest, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(VDO_README.format(source=branding.SOURCE_URL or "the base app"))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(dest, "LICENSE"))
    with open(os.path.join(dest, ".gitignore"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("__pycache__/\n*.py[cod]\n.pytest_cache/\ndist/\nbuild/\n")


def export(which: str, dest: str, force: bool = False, init: bool = False) -> int:
    paths, owners, specs = select(which)
    bad = _leaks(which, paths, owners)
    if bad:
        print("REFUSED -- these would go out:\n  " + "\n  ".join(bad))
        return 1
    dest = os.path.abspath(dest)
    if os.path.realpath(dest).startswith(os.path.realpath(ROOT) + os.sep) or \
            os.path.realpath(dest) == os.path.realpath(ROOT):
        print("REFUSED -- DEST must be outside this repository")
        return 1
    os.makedirs(dest, exist_ok=True)
    left = [n for n in os.listdir(dest) if n != ".git"]
    if left and not force:
        print("REFUSED -- %s is not empty (use --force to replace an earlier export)" % dest)
        return 1
    _clear(dest)
    _copy(paths, owners, specs, dest)
    if which == VDO_ID:
        _write_vdo_extras(dest)
    print("exported %d files to %s" % (len(paths), dest))
    for w in _warnings(which, paths):
        print("WARNING: " + w)
    if init:
        if not os.path.isdir(os.path.join(dest, ".git")):
            subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=dest)
        subprocess.check_call(["git", "add", "-A"], cwd=dest)
        print("git repository ready in %s, export staged (nothing committed)" % dest)
    return 0


def headers(check: bool) -> int:
    """Header every source file of the package (by owner) and every other
    source file an export publishes. Private tools and tests are left alone."""
    specs = build._specs(SRC_PKG)
    owners = build.ownership(SRC_PKG, specs)
    public = {}
    for which in ("base", VDO_ID):
        paths, own, _s = select(which)
        public.update((p, own.get(p, "")) for p in paths)
    wrong = []
    for p in _git_files():
        ext = os.path.splitext(p)[1]
        if ext not in COMMENT:
            continue
        if p.startswith(PKG + "/"):
            rel = p[len(PKG) + 1:]
            if rel not in owners:
                continue
            owner = owners[rel]
        elif p in public:
            owner = public[p]
        else:
            continue
        lines = header_for(owner, specs)
        path = os.path.join(ROOT, p)
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        if has_header(text, lines, COMMENT[ext]):
            continue
        wrong.append(p)
        if not check:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(with_header(text, lines, COMMENT[ext]))
    if check:
        if wrong:
            print("missing or wrong licence header:\n  " + "\n  ".join(wrong))
            return 1
        print("every source file has its licence header")
        return 0
    print("headed %d file(s)" % len(wrong))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("base", VDO_ID):
        s = sub.add_parser(name)
        s.add_argument("dest")
        s.add_argument("--force", action="store_true")
        s.add_argument("--init", action="store_true")
    s = sub.add_parser("headers")
    s.add_argument("--check", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("which", choices=("base", VDO_ID))
    a = ap.parse_args(argv)
    if a.cmd == "headers":
        return headers(a.check)
    if a.cmd == "list":
        paths, owners, _specs = select(a.which)
        print("\n".join(paths))
        bad = _leaks(a.which, paths, owners)
        for b in bad:
            print("LEAK: " + b)
        return 1 if bad else 0
    return export(a.cmd, a.dest, a.force, a.init)


if __name__ == "__main__":
    sys.exit(main())
