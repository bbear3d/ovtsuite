# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Rename the package and the product, mechanically and completely.

The suite is expected to be renamed again, so the rename is a tool rather than
a one-off edit. It rewrites ``<pkg>/branding.py``, moves the package directory
and the branded packaging files, and replaces every occurrence of the old names
in the source, build and packaging files -- deliberately NOT in ``docs/``,
which is a historical record and should keep saying what it said at the time.

Usage:
    python tools/rename_package.py --to newpkg --app-name "New Name" \
                                   [--exe NewName] [--org "New Name"] \
                                   [--dry-run]

Afterwards:
    python -m pytest tests/test_branding.py     # no stray literals
    python -m pytest tests                      # nothing else moved

The compatibility shim at ``<oldpkg>/__init__.py`` is regenerated so that
``import <oldpkg>`` keeps working for one release; delete that directory when
you no longer need it.
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Extensions worth rewriting. Binary and reference material is never touched.
TEXT_EXT = (".py", ".toml", ".spec", ".ps1", ".iss", ".cfg", ".txt", ".md",
            ".json", ".bat", ".sh")

#: Trees to rewrite. ``docs`` is excluded on purpose (see the module docstring);
#: ``README.md`` is handled separately because it is user-facing.
INCLUDE_DIRS = ("tools", "tests", "packaging")
INCLUDE_FILES = ("pyproject.toml", "build.py", "build.ps1", "README.md")

SKIP_DIRS = {".git", "build", "dist", "docs", "Reference", "generated",
             "tunes", "_to_delete", "__pycache__", ".pytest_cache",
             ".build-venv", ".build-venv32", ".venv", "Claude outputs"}


def _run(cmd, dry):
    print("  $", " ".join(cmd))
    if not dry:
        subprocess.run(cmd, cwd=ROOT, check=True)


def _read_branding(pkg):
    path = os.path.join(ROOT, pkg, "branding.py")
    src = open(path, encoding="utf-8").read()

    def grab(name):
        m = re.search(r'^%s = "([^"]*)"' % name, src, re.M)
        return m.group(1) if m else None
    return dict(PKG=grab("PKG"), APP_NAME=grab("APP_NAME"),
                EXE_NAME=grab("EXE_NAME"), ENV_PREFIX=grab("ENV_PREFIX"),
                ORG=grab("ORG")), src, path


def _markers(src):
    """The legacy-region markers, read out of branding.py's own source.

    Read rather than imported for the same reason the names are: this tool has
    to work on a tree that is mid-rename, where importing the package may not.
    """
    def grab(name):
        m = re.search(r'^%s = "([^"]*)"' % name, src, re.M)
        return m.group(1) if m else None
    return grab("LEGACY_BEGIN"), grab("LEGACY_END")


def _substitute(text, pairs, begin, end, path):
    """Replace the names, line by line, except inside a declared legacy region.

    A blanket replace over the whole file was wrong, and quietly so. The
    installer names the PREVIOUS install in order to remove it -- its old
    directory, its old Start Menu group, its old exe -- and branding.py names
    the previous config location in order to read it. Rename those and the
    cleanup points at the install that is being laid down: the group it deletes
    becomes the group it just created, which is a failure this installer's own
    comments record having already been caused once. Those regions declare
    themselves in the file; everything outside them still gets rewritten.
    """
    if not begin or not end:
        return "".join(_replaced(line, pairs) for line in text.splitlines(True))

    out, inside, opened = [], False, 0
    for line in text.splitlines(True):
        if begin in line:
            inside, opened = True, opened + 1
        elif end in line:
            if not inside:
                raise SystemExit(
                    "%s: a legacy-names region ends without beginning" % path)
            inside = False
            out.append(line)
            continue
        out.append(line if inside else _replaced(line, pairs))
    if inside:
        raise SystemExit(
            "%s: a legacy-names region is never closed, so the rest of the file "
            "would silently keep its old names" % path)
    return "".join(out)


def _replaced(line, pairs):
    for o, n in pairs:
        line = line.replace(o, n)
    return line


def legacy_extended(text, old, begin, end):
    """``branding.py`` source with the OUTGOING release prepended to ``LEGACY_RELEASES``.

    The legacy region stops a rename from rewriting the past names, which is necessary
    and not sufficient: left alone the list keeps naming the FIRST release forever and
    forgets every name in between. Rename twice and the settings written under the
    middle name are orphaned -- no error, the app just comes up with defaults and the
    user's configuration is gone. So each rename records the release it is leaving.

    Newest first, matching the order the fallbacks are read in, and idempotent: a
    release already listed is not added again.
    """
    import ast

    span = _region(text, begin, end, "LEGACY_RELEASES = (")
    if span is None:
        return text

    body = text[span[0]:span[1]]
    at = body.find("LEGACY_RELEASES = (")

    opened = body.index("(", at)
    closed = _match_paren(body, opened)
    if closed is None:
        return text

    try:
        current = list(ast.literal_eval(body[opened:closed + 1]))
    except (ValueError, SyntaxError):
        return text

    row = (old["APP_NAME"], old["PKG"], old["EXE_NAME"], old["ORG"])
    if row in current:
        return text

    current.insert(0, row)
    indent = body[body.rfind("\n", 0, at) + 1:at]
    rendered = ("LEGACY_RELEASES = (\n"
                + "".join("%s    %r,\n" % (indent, tuple(r)) for r in current)
                + indent + ")")
    return text[:span[0]] + body[:at] + rendered + body[closed + 1:] + text[span[1]:]


def _region(text, begin, end, anchor):
    """Offsets of the marked region CONTAINING ``anchor``, markers excluded.

    The region containing it, not the first region in the file: in ``branding.py`` the
    first begin/end pair is the two lines that DEFINE the markers, and taking that one
    silently found an empty region and changed nothing -- a rename that reported
    success and quietly dropped half its job.
    """
    if not begin or not end:
        return None

    at = 0
    while True:
        opened = text.find(begin, at)
        if opened < 0:
            return None
        opened = text.find("\n", opened)
        if opened < 0:
            return None
        closed = text.find(end, opened)
        if closed < 0:
            return None
        span = (opened + 1, text.rfind("\n", 0, closed) + 1)
        if anchor in text[span[0]:span[1]]:
            return span
        at = closed + len(end)


def _match_paren(text, at):
    depth = 0
    for i in range(at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def installer_extended(text, old, begin, end):
    """``installer.iss`` with the OUTGOING release added to its generated cleanup list.

    The installer cannot import ``branding``, so the same list has to be written into
    it. Before this it named exactly ONE past install, so after a second rename there
    would have been a release in the field that nothing cleaned up. The guard against
    the two lists drifting is a test comparing them, since nothing else can.
    """
    if not begin or not end or begin not in text:
        return text

    app, exe = old["APP_NAME"], old["EXE_NAME"]
    names = _iss_cases(text, "LegacyAppName")
    exes = _iss_cases(text, "LegacyExeName")
    if names is None or exes is None or app in names:
        return text

    names, exes = [app] + names, [exe] + exes
    text = _iss_replace_cases(text, "LegacyAppName", names)
    text = _iss_replace_cases(text, "LegacyExeName", exes)
    return _iss_replace_count(text, len(names))


def _iss_cases(text, function):
    """The strings a generated ``case I of`` accessor returns, in index order."""
    m = re.search(r"function %s\(I: Integer\): String;.*?case I of(.*?)\n  end;"
                  % function, text, re.S)
    if m is None:
        return None

    found = {}
    for index, value in re.findall(r"(\d+):\s*Result\s*:=\s*'([^']*)'", m.group(1)):
        found[int(index)] = value
    return [found[k] for k in sorted(found)] if found else None


def _iss_replace_cases(text, function, values):
    body = "".join("    %d: Result := '%s';\n" % (i, v) for i, v in enumerate(values))
    return re.sub(r"(function %s\(I: Integer\): String;.*?case I of\n)(.*?)(  end;)"
                  % function, lambda m: m.group(1) + body + m.group(3),
                  text, count=1, flags=re.S)


def _iss_replace_count(text, count):
    return re.sub(r"(function LegacyReleaseCount\(\): Integer;\nbegin\n  Result := )\d+",
                  lambda m: m.group(1) + str(count), text, count=1)


def _targets(pkg):
    """Every text file whose contents may be rewritten."""
    out = []
    for base in (pkg,) + INCLUDE_DIRS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            out += [os.path.join(dp, f) for f in fn if f.endswith(TEXT_EXT)]
    out += [os.path.join(ROOT, f) for f in INCLUDE_FILES
            if os.path.exists(os.path.join(ROOT, f))]
    return sorted(set(out))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--to", required=True, help="new package (import) name")
    p.add_argument("--app-name", required=True, help='new product name, e.g. "OVT-Suite"')
    p.add_argument("--exe", help="new executable base name (default: app name without punctuation)")
    p.add_argument("--org", help="new publisher / QSettings organisation (default: app name)")
    p.add_argument("--from", dest="frm", help="current package name (default: read from the tree)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    old_pkg = a.frm
    if old_pkg is None:
        cand = [d for d in os.listdir(ROOT)
                if os.path.exists(os.path.join(ROOT, d, "branding.py"))]
        if len(cand) != 1:
            p.error("could not find the package (candidates: %s); pass --from" % cand)
        old_pkg = cand[0]
    old, _src, bpath = _read_branding(old_pkg)
    begin, end = _markers(_src)

    new = dict(PKG=a.to, APP_NAME=a.app_name,
               EXE_NAME=a.exe or re.sub(r"[^A-Za-z0-9]", "", a.app_name),
               ENV_PREFIX=re.sub(r"[^A-Za-z0-9]", "", a.app_name).upper(),
               ORG=a.org or a.app_name)

    # Longest first, so "OVTSuite" is not half-eaten by a shorter key.
    pairs = sorted(((old[k], new[k]) for k in old if old[k] and old[k] != new[k]),
                   key=lambda kv: -len(kv[0]))
    if not pairs:
        print("nothing to rename")
        return 0
    print("renaming:")
    for o, n in pairs:
        print("   %-14s -> %s" % (o, n))

    changed = 0
    for path in _targets(old_pkg):
        try:
            txt = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        new_txt = _substitute(txt, pairs, begin, end, path)
        if new_txt != txt:
            changed += 1
            print("   ~", os.path.relpath(path, ROOT))
            if not a.dry_run:
                open(path, "w", encoding="utf-8").write(new_txt)
    print("%d file(s) rewritten" % changed)

    # Everything that has to remember the PREVIOUS install now learns the name this
    # rename is leaving behind, or the chain only ever remembers its first link.
    iss = os.path.join(ROOT, "packaging", "installer.iss")
    for path, extend, what in ((bpath, legacy_extended, "config fallbacks"),
                               (iss, installer_extended, "cleanup list")):
        if not os.path.exists(path):
            continue
        try:
            before = open(path, encoding="utf-8").read()
        except OSError:
            continue
        after = extend(before, old, begin, end)
        if after == before:
            continue
        print("   ~ %s (%s now also name %s)"
              % (os.path.relpath(path, ROOT), what, old["APP_NAME"]))
        if not a.dry_run:
            open(path, "w", encoding="utf-8").write(after)

    # move the package and the branded packaging files
    moves = [(old_pkg, a.to)]
    for ext in (".spec", ".ico", "_flasher.ico", "_sniffer.ico", "_logger.ico",
                "_simulator.ico"):
        src = os.path.join("packaging", old["PKG"] + ext)
        if os.path.exists(os.path.join(ROOT, src)):
            moves.append((src, os.path.join("packaging", a.to + ext)))
    for src, dst in moves:
        if src == dst:
            continue
        _run(["git", "mv", src, dst], a.dry_run)

    # regenerate the compatibility shim
    if not a.dry_run:
        write_shim(old_pkg, a.to, old["APP_NAME"], new["APP_NAME"])
    print("done. Now run: python -m pytest tests")
    return 0


SHIM = '''"""Compatibility shim: ``{old}`` is now :mod:`{new}` ({newapp}).

The package was renamed when {oldapp} became {newapp}. ``import {old}`` keeps
working -- the name is bound to the real package object, so ``{old}.__version__``
and ``{old}.branding`` behave exactly as before.

``import {old}.submodule`` deliberately does NOT work, and raises with the
replacement spelled out. Aliasing submodules is the obvious thing to try and it
is a trap: the import machinery re-executes ``submodule.py`` under the second
name, leaving two module objects and two copies of every class, so an ``Entity``
built through one name fails ``isinstance`` against the other -- a bug that
surfaces far from its cause. A loud error at the import line is worth more than
a silent duplicate.

Delete this directory once the release that needed it is out.
"""

import importlib
import importlib.abc
import sys
import warnings

_OLD = "{old}"
_NEW = "{new}"

warnings.warn("%s has been renamed to %s; update your imports" % (_OLD, _NEW),
              DeprecationWarning, stacklevel=2)


class _RenamedFinder(importlib.abc.MetaPathFinder):
    """Turn ``import {old}.x`` into an error naming ``{new}.x``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        raise ImportError(
            "%s was renamed to %s. Import %s instead of %s -- the old name is "
            "kept only as a package alias, because aliasing submodules would "
            "load them twice and give you two incompatible copies of every "
            "class." % (_OLD, _NEW, _NEW + fullname[len(_OLD):], fullname))


if not any(isinstance(f, _RenamedFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _RenamedFinder())

sys.modules[__name__] = importlib.import_module(_NEW)
'''


def write_shim(old_pkg, new_pkg, old_app, new_app):
    d = os.path.join(ROOT, old_pkg)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(SHIM.format(old=old_pkg, new=new_pkg,
                             oldapp=old_app, newapp=new_app))


if __name__ == "__main__":
    sys.exit(main())
