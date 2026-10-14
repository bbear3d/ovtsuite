#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build the base app's source tree and the module packages, incrementally.

Every build script (build.ps1, build_linux.sh, build.py) drives the module
split through this one tool, so Windows and Linux make the same decisions:

    python tools/build_modules.py plan
        what is stale: the base app (re-freeze needed) and each package

    python tools/build_modules.py stage-base [--out build/base]
        the base app's files only -> build/base/<pkg>/ (what PyInstaller
        freezes), plus build/base/external_imports.json: the libraries the
        modules import, which the frozen base must carry for them

    python tools/build_modules.py packages [--out dist/modules]
            [--modules all|id,id] [--variants full,checksum] [--force]
            [--unsigned] [--no-check]
        build every stale .ovtmod (content fingerprints, see
        ovtsuite.modules.build), sign it, and check it in a clean interpreter
        against the staged base app

    python tools/build_modules.py install-into DEST [--packages dist/modules]
            [--modules ...] [--variant full|checksum]
        lay packages out installed under DEST (DEST/<id>/...): the app's own
        modules folder for a full build, an installer's payload

    python tools/build_modules.py freeze-key --mode STR
        one hash of everything the frozen base app is made from (the staged
        base, the packaging inputs, the build mode): when it matches the last
        freeze, PyInstaller can be skipped entirely

    python tools/build_modules.py inno --full-dir D --checksum-dir D --out D
        Inno Setup include files for the installers' module components, and
        modules.json (id, name, version, variant, folder, size) for the
        build scripts

    python tools/build_modules.py installer-key --source DIR [--file F]...
            [--text S]...
        one hash of everything an installer is made from (its payload folder,
        its scripts and icons, its settings): the build scripts skip an
        installer whose key matches the one it was last built from

    python tools/build_modules.py release-sums --out dist/SHA256SUMS.txt
            [--unsigned] FILE_OR_DIR...
        the release's checksum list: the SHA-256 of every file given (a
        folder: its .exe, .ovtmod, .deb, .rpm), merged into the list already
        at --out (so the Windows and the Linux build can each add theirs), and
        its Ed25519 signature SHA256SUMS.txt.sig with the module signing key,
        which the app's updater checks (ovtsuite/updates.py)

    python tools/build_modules.py ensure-key
        create the signing key on first use (OVT_SIGNING_KEY, default
        ~/.ovt-signing/module-key.pem) and make the base app trust it (adds
        its public half to ovtsuite/data/module_keys.json -- commit that)

Fingerprints live in build/.cache/modules.json. A package is rebuilt when its
fingerprint changed or its file is missing; nothing else is touched.
"""

import argparse
import ast
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ovtsuite import branding                                   # noqa: E402
from ovtsuite.modules import api, build, package, signing       # noqa: E402

SRC = os.path.join(ROOT, branding.PKG)
CACHE = os.path.join(ROOT, "build", ".cache", "modules.json")
DEFAULT_BASE = os.path.join(ROOT, "build", "base")
DEFAULT_OUT = os.path.join(ROOT, "dist", "modules")


def _ids(arg, specs):
    if not arg or arg == "all":
        return list(specs)
    want = [x.strip() for x in arg.split(",") if x.strip()]
    bad = [x for x in want if x not in specs]
    if bad:
        sys.exit("unknown module(s): %s (known: %s)" % (", ".join(bad), ", ".join(specs)))
    return want


def _variants(arg):
    out = [v.strip() for v in (arg or "full,checksum").split(",") if v.strip()]
    for v in out:
        if v not in (api.FULL, api.CHECKSUM_ONLY):
            sys.exit("unknown variant %r (full, checksum)" % v)
    return out


def _key(args):
    if getattr(args, "unsigned", False):
        return None
    path = os.environ.get(signing.ENV_KEY) or signing.default_key_path()
    if not os.path.exists(path):
        sys.exit("no signing key at %s -- run 'build_modules.py ensure-key' "
                 "(or pass --unsigned for a development build)" % path)
    return path


# ------------------------------------------------------------------ commands
def cmd_ensure_key(args):
    path = os.environ.get(signing.ENV_KEY) or signing.default_key_path()
    if os.path.exists(path):
        pub = signing.public_hex(path)
        print("signing key: %s" % path)
    else:
        pub = signing.generate_key(path)
        print("signing key CREATED: %s -- back it up; it signs every module" % path)
    keys_path = os.path.join(SRC, "data", signing.KEYS_FILE)
    try:
        with open(keys_path, "r", encoding="utf-8") as fh:
            keys = json.load(fh)
    except (OSError, ValueError):
        keys = {"keys": []}
    if any(k.get("public") == pub for k in keys.get("keys", [])):
        print("trusted by the app (%s)" % os.path.relpath(keys_path, ROOT))
        return 0
    kid = "%s-%s" % (branding.PKG, time.strftime("%Y%m%d"))
    keys.setdefault("keys", []).append({"id": kid, "public": pub})
    tmp = keys_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, keys_path)
    print("added key %r to %s -- commit it" % (kid, os.path.relpath(keys_path, ROOT)))
    return 0


def external_imports(specs):
    """Top-level names every module package imports that are not the app
    package itself: the libraries the frozen base must carry for them."""
    names = set()
    for mid in specs:
        top = os.path.join(SRC, mid)
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                tree = ast.parse(open(os.path.join(d, f), encoding="utf-8").read())
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        names |= {a.name for a in n.names}
                    elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                        names.add(n.module)
                        # "from pkg import sub" may name a submodule
                        names |= {"%s.%s" % (n.module, a.name) for a in n.names
                                  if a.name != "*" and _is_module("%s.%s" % (n.module, a.name))}
    return sorted(x for x in names if x.split(".")[0] != branding.PKG
                  and x.split(".")[0] != "__future__")


def _is_module(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def cmd_stage_base(args):
    specs = build._specs(SRC)
    fp = build.fingerprint(SRC, specs)["base"]
    dest = os.path.join(args.out, branding.PKG)
    stamp = os.path.join(args.out, ".fingerprint")
    old = open(stamp).read().strip() if os.path.exists(stamp) else ""
    if old == fp and os.path.isdir(dest) and not args.force:
        print("base app: unchanged (%s)" % fp[:12])
    else:
        n = len(build.stage_base(SRC, dest, specs))
        with open(stamp, "w") as fh:
            fh.write(fp)
        print("base app: staged %d files (%s)" % (n, fp[:12]))
    with open(os.path.join(args.out, "external_imports.json"), "w") as fh:
        json.dump(external_imports(specs), fh, indent=0)
    return 0


def cmd_plan(args):
    specs = build._specs(SRC)
    fps = build.fingerprint(SRC, specs)
    stamps = build.stamp_load(CACHE)
    base_stamp = os.path.join(args.base, ".fingerprint")
    old_base = open(base_stamp).read().strip() if os.path.exists(base_stamp) else ""
    plan = {"base": "unchanged" if old_base == fps["base"] else "changed", "packages": {}}
    for mid in _ids(args.modules, specs):
        for v in _variants(args.variants):
            if v == api.CHECKSUM_ONLY and not specs[mid].checksum:
                continue
            key = mid if v == api.FULL else mid + "-checksum"
            out = os.path.join(args.out, build.package_name(specs[mid], v))
            fresh = build.stamp_entry(stamps, key)[0] == fps[key] and os.path.exists(out)
            plan["packages"][key] = "up to date" if fresh else "stale"
    if args.json:
        print(json.dumps(plan))
    else:
        print("base app: %s" % plan["base"])
        for k, v in plan["packages"].items():
            print("  %-16s %s" % (k, v))
    return 0


def cmd_packages(args):
    specs = build._specs(SRC)
    build.ownership(SRC, specs)                  # fails loudly on a clash
    fps = build.fingerprint(SRC, specs)
    stamps = build.stamp_load(CACHE)
    key_path = _key(args)
    base_parent = args.base
    if not args.no_check:
        # always against the CURRENT base app (incremental: a no-op when it is
        # already staged); only checking that the folder existed let packages
        # be checked against an outdated copy
        cmd_stage_base(argparse.Namespace(out=base_parent, force=False))
    os.makedirs(args.out, exist_ok=True)
    built = skipped = 0
    warnings = []
    for mid in _ids(args.modules, specs):
        spec = specs[mid]
        for v in _variants(args.variants):
            if v == api.CHECKSUM_ONLY and not spec.checksum:
                continue
            key = mid if v == api.FULL else mid + "-checksum"
            name = build.package_name(spec, v)
            out = os.path.join(args.out, name)
            if (not args.force and build.stamp_entry(stamps, key)[0] == fps[key]
                    and os.path.exists(out)):
                print("  %-34s up to date" % name)
                skipped += 1
                continue
            warn = build.version_warning(stamps, key, fps[key], spec.version)
            if warn and not any(w.startswith(mid + ":") for w in warnings):
                warnings.append(warn.replace(key + ":", mid + ":", 1))
            # an older version of the same package goes
            for old in os.listdir(args.out):
                if (old.startswith(mid + "-") and old.endswith(package.EXT) and old != name
                        and old.endswith("-checksum" + package.EXT) == (v == api.CHECKSUM_ONLY)):
                    os.remove(os.path.join(args.out, old))
            t = time.time()
            build.pack(SRC, args.out, mid, v, key_path, branding.APP_NAME, specs)
            if not args.no_check:
                ok, why = build.check_package(base_parent, branding.PKG, out, mid, v)
                if not ok:
                    os.remove(out)
                    sys.exit("%s FAILED its check against the base app:\n%s" % (name, why))
            stamps[key] = {"fp": fps[key], "version": spec.version}
            build.stamp_save(CACHE, stamps)
            built += 1
            print("  %-34s built %s (%.0f KB, %.1f s)%s" % (
                name, "" if key_path else "UNSIGNED ", os.path.getsize(out) / 1024.0,
                time.time() - t, "" if args.no_check else ", checked"))
            if args.archive:
                _archive(out, args.archive)
    print("module packages: %d built, %d up to date -> %s"
          % (built, skipped, os.path.relpath(args.out, ROOT)))
    for w in warnings:
        print("WARNING: " + w)
    return 0


def _archive(pkg_path, archive_dir):
    """Keep every released package (the compatibility test loads them all
    against newer apps). A version is archived once: an existing copy is
    never overwritten -- its files changing without a version bump is what
    the version warning is about."""
    import shutil
    os.makedirs(archive_dir, exist_ok=True)
    dest = os.path.join(archive_dir, os.path.basename(pkg_path))
    if not os.path.exists(dest):
        shutil.copy2(pkg_path, dest)
        print("  %-34s kept as a release -> %s" % ("", dest))


def cmd_install_into(args):
    from ovtsuite.modules import installer
    specs = build._specs(SRC)
    os.makedirs(args.dest, exist_ok=True)
    for mid in _ids(args.modules, specs):
        spec = specs[mid]
        if args.variant == api.CHECKSUM_ONLY and not spec.checksum:
            continue
        p = os.path.join(args.packages, build.package_name(spec, args.variant))
        if not os.path.exists(p):
            sys.exit("missing %s -- build the packages first" % p)
        man = installer.install(p, scope=args.dest, allow_unsigned=True)
        print("  %s %s %s -> %s" % (man.id, man.version, man.variant,
                                    os.path.join(args.dest, man.id)))
    return 0


PACKAGING_INPUTS = ("packaging/ovtsuite.spec", "packaging/entry.py",
                    "packaging/version_info.txt", "packaging/ovtsuite.ico",
                    "packaging/ovtsuite_flasher.ico", "packaging/ovtsuite_sniffer.ico",
                    "packaging/ovtsuite_logger.ico", "packaging/ovtsuite_simulator.ico",
                    "tools/build_modules.py")


def cmd_freeze_key(args):
    import hashlib
    specs = build._specs(SRC)
    h = hashlib.sha256(build.fingerprint(SRC, specs)["base"].encode())
    h.update(args.mode.encode())
    for rel in PACKAGING_INPUTS:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                h.update(rel.encode() + hashlib.sha256(fh.read()).digest())
    print(h.hexdigest())
    return 0


def installer_key(sources, files=(), texts=()) -> str:
    """Content hash of installer inputs: every file under each source folder
    (by relative path and contents -- not times, so re-laying out identical
    files does not count as a change), each extra file (or its absence) and
    each text (settings such as the version and compression)."""
    import hashlib
    h = hashlib.sha256(b"installer-key/1")
    for t in texts:
        h.update(b"T" + t.encode("utf-8") + b"\0")
    for f in files:
        h.update(b"F" + os.path.basename(f).encode("utf-8") + b"\0")
        try:
            with open(f, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"missing")
    for src in sources:
        h.update(b"S\0")
        if not os.path.isdir(src):
            h.update(b"missing")
            continue
        for d, dirs, names in os.walk(src):
            dirs.sort()
            for n in sorted(names):
                p = os.path.join(d, n)
                rel = os.path.relpath(p, src).replace(os.sep, "/")
                h.update(rel.encode("utf-8") + b"\0")
                fh_h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        fh_h.update(chunk)
                h.update(fh_h.digest())
    return h.hexdigest()


def cmd_installer_key(args):
    print(installer_key(args.source or (), args.file or (), args.text or ()))
    return 0


def _dir_size(d):
    return sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(d) for f in fs)


def _iss_path(p):
    return os.path.abspath(p).replace("/", "\\") if os.name == "nt" else os.path.abspath(p)


def cmd_inno(args):
    """Inno include files: [Types]/[Components]/[Files] for the full
    installer's module checklist and the tools installer's checksum modules."""
    from ovtsuite import platforms
    os.makedirs(args.out, exist_ok=True)
    rows = []
    for variant, top in ((api.FULL, args.full_dir), (api.CHECKSUM_ONLY, args.checksum_dir)):
        if not top or not os.path.isdir(top):
            continue
        for mid in platforms.MODULES:
            d = os.path.join(top, mid)
            mpath = os.path.join(d, package.MANIFEST)
            if not os.path.isfile(mpath):
                continue
            with open(mpath, "rb") as fh:
                man = package.Manifest.from_bytes(fh.read())
            rows.append({"id": mid, "name": man.name, "version": man.version,
                         "variant": man.variant, "dir": os.path.abspath(d),
                         "size": _dir_size(d),
                         "description": platforms.MODULES[mid].description})
    with open(os.path.join(args.out, "modules.json"), "w") as fh:
        json.dump(rows, fh, indent=1)

    def section(variant, types):
        mine = [r for r in rows if r["variant"] == variant]
        out = []
        if types:
            out += ["[Types]",
                    'Name: "full"; Description: "Full installation (every module)"',
                    'Name: "base"; Description: "Base app only (add modules later)"',
                    'Name: "custom"; Description: "Choose modules"; Flags: iscustom', ""]
        out += ["[Components]"]
        if types:
            out += ['Name: "app"; Description: "%s: identify, hex editor, compare, '
                    'flasher, logger, sniffer"; Types: full base custom; Flags: fixed'
                    % branding.APP_NAME]
        for r in mine:
            what = ("checksum correction only" if variant == api.CHECKSUM_ONLY
                    else r["description"])
            out.append('Name: "mod_%s"; Description: "%s module %s -- %s"; '
                       'Types: %s'
                       % (r["id"], r["name"], r["version"], what.replace('"', "'"),
                          "full custom" if types else "full compact custom"))
        out += ["", "[Files]"]
        for r in mine:
            out.append('Source: "%s\\*"; DestDir: "{app}\\modules\\%s"; '
                       'Components: mod_%s; Flags: ignoreversion recursesubdirs '
                       'createallsubdirs' % (_iss_path(r["dir"]), r["id"], r["id"]))
        return "\n".join(out) + "\n"

    with open(os.path.join(args.out, "modules_full.iss"), "w") as fh:
        fh.write("; generated by tools/build_modules.py inno -- do not edit\n")
        fh.write(section(api.FULL, True))
    with open(os.path.join(args.out, "modules_checksum.iss"), "w") as fh:
        fh.write("; generated by tools/build_modules.py inno -- do not edit\n")
        fh.write(section(api.CHECKSUM_ONLY, False))
    for r in rows:
        print("  %-6s %-8s %s  %.1f MB" % (r["id"], r["variant"], r["version"],
                                          r["size"] / 1048576.0))
    return 0


RELEASE_EXTS = (".exe", ".ovtmod", ".deb", ".rpm", ".zip", ".AppImage")


def cmd_release_sums(args):
    import hashlib
    from ovtsuite import updates
    files = []
    for p in args.files:
        if os.path.isdir(p):
            files += [os.path.join(p, n) for n in sorted(os.listdir(p))
                      if n.endswith(RELEASE_EXTS) and os.path.isfile(os.path.join(p, n))]
        elif os.path.isfile(p):
            files.append(p)
        else:
            sys.exit("no such file: %s" % p)
    try:
        with open(args.out, "rb") as fh:
            sums = updates.parse_sums(fh.read())
    except OSError:
        sums = {}
    for f in files:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sums[os.path.basename(f)] = h.hexdigest()
        print("  %s  %s" % (h.hexdigest()[:16], os.path.basename(f)))
    data = updates.format_sums(sums).encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as fh:
        fh.write(data)
    sig = args.out + ".sig"
    key = None
    if not args.unsigned:
        key = os.environ.get(signing.ENV_KEY) or signing.default_key_path()
        if not os.path.exists(key):
            print("WARNING: no signing key at %s -- %s is not signed (run "
                  "'build_modules.py ensure-key')" % (key, os.path.basename(args.out)))
            key = None
    if key is None:
        if os.path.exists(sig):
            os.remove(sig)          # it would no longer match
        print("%s: %d files, NOT signed" % (args.out, len(sums)))
        return 0
    with open(sig, "wb") as fh:
        fh.write(signing.sign(data, key))
    print("%s: %d files, signed (%s)" % (args.out, len(sums), os.path.basename(sig)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ensure-key")
    s.set_defaults(fn=cmd_ensure_key)
    s = sub.add_parser("stage-base")
    s.add_argument("--out", default=DEFAULT_BASE)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_stage_base)
    s = sub.add_parser("plan")
    s.add_argument("--out", default=DEFAULT_OUT)
    s.add_argument("--base", default=DEFAULT_BASE)
    s.add_argument("--modules", default="all")
    s.add_argument("--variants", default="full,checksum")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("packages")
    s.add_argument("--out", default=DEFAULT_OUT)
    s.add_argument("--base", default=DEFAULT_BASE)
    s.add_argument("--modules", default="all")
    s.add_argument("--variants", default="full,checksum")
    s.add_argument("--force", action="store_true")
    s.add_argument("--unsigned", action="store_true")
    s.add_argument("--no-check", action="store_true")
    s.add_argument("--archive", help="keep a copy of each new package version here "
                   "(e.g. Reference/ModuleReleases) for the compatibility test")
    s.set_defaults(fn=cmd_packages)
    s = sub.add_parser("freeze-key")
    s.add_argument("--mode", default="")
    s.set_defaults(fn=cmd_freeze_key)
    s = sub.add_parser("installer-key")
    s.add_argument("--source", action="append", help="payload folder (repeatable)")
    s.add_argument("--file", action="append", help="script, icon, include ... (repeatable)")
    s.add_argument("--text", action="append", help="a setting that shapes the output")
    s.set_defaults(fn=cmd_installer_key)

    s = sub.add_parser("inno")
    s.add_argument("--full-dir")
    s.add_argument("--checksum-dir")
    s.add_argument("--out", default=os.path.join(ROOT, "build", "installer"))
    s.set_defaults(fn=cmd_inno)
    s = sub.add_parser("release-sums")
    s.add_argument("files", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--unsigned", action="store_true")
    s.set_defaults(fn=cmd_release_sums)
    s = sub.add_parser("install-into")
    s.add_argument("dest")
    s.add_argument("--packages", default=DEFAULT_OUT)
    s.add_argument("--modules", default="all")
    s.add_argument("--variant", default=api.FULL, choices=(api.FULL, api.CHECKSUM_ONLY))
    s.set_defaults(fn=cmd_install_into)
    args = ap.parse_args(argv)
    for k in ("out", "base", "packages", "dest", "full_dir", "checksum_dir"):
        if getattr(args, k, None):
            setattr(args, k, os.path.abspath(getattr(args, k)))
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
