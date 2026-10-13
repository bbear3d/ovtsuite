# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Catalogue every full (2 MB / 1 MB) EDC16 reference binary's calibration
layout into ``ovtsuite/data/layouts.json.gz``.

For each binary: family, Bosch software number + dataset id (block header),
hardware/software part numbers (KWP ident record), calibration region and
the complete parameter-block pointer table read from the linker's *default*
array -- which only a full read contains.  At runtime a bare 512 KB
calibration read is aligned to its family reference's table (exact block
placement) and, when its software number and table match a catalogued
binary, tied to that full binary.

Usage:  python tools/generate_layouts.py  [--bin-dir=DIR] [--if-stale]

Only the binaries are needed (no XDFs); run it whenever a full binary is added
to Reference/Binaries so the shipped catalogue grows with the collection.

--if-stale (what build.ps1 passes) skips the whole scan when nothing that can
change the result has changed.  Each run records a dependency file
(build/.cache/layouts.deps.json) holding the SHA-1 of every input it actually
used: this script, every ovtsuite module that was loaded while cataloguing,
every file in ovtsuite/data (the KB detect_family reads), and every candidate
binary.  The next --if-stale run re-hashes that same set -- plus the CURRENT
binary and data-file listings, so an added or removed file counts -- and only
re-catalogues on a difference.

The .gz is written deterministically (gzip mtime 0) and only when its
decompressed content actually changes, so an unchanged catalogue leaves the
file -- and git status -- untouched.
"""
import glob
import gzip
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ovtsuite import layout                              # noqa: E402
from ovtsuite.fileinfo import ident16                    # noqa: E402
from ovtsuite.match import REF_BINARIES                  # noqa: E402
from ovtsuite.session import detect_family               # noqa: E402
from ovtsuite.structure import find_cal_region           # noqa: E402


BIN_EXTS = (".bin", ".ori", ".rom", "")


def _candidates(bin_dir):
    """Files in bin_dir the catalogue looks at (same filter as the scan)."""
    return [p for p in sorted(glob.glob(os.path.join(bin_dir, "*")), key=str.lower)
            if not os.path.isdir(p)
            and os.path.splitext(p)[1].lower() in BIN_EXTS]


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(root, path):
    return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")


def _fingerprint(root, bin_dir, out, modules):
    """{relative path: sha1} over every input that can change the output.

    ``modules`` is the ovtsuite module list recorded by the previous run (a
    module that has since disappeared hashes as None, which forces a rebuild).
    """
    paths = set(os.path.join(root, m) for m in modules)
    paths.add(os.path.abspath(__file__))
    data_dir = os.path.join(root, "ovtsuite", "data")
    out_abs = os.path.normcase(os.path.abspath(out))
    for p in glob.glob(os.path.join(data_dir, "*")):
        if os.path.isfile(p) and os.path.normcase(os.path.abspath(p)) != out_abs:
            paths.add(p)
    paths.update(_candidates(bin_dir))
    fp = {}
    for p in paths:
        try:
            fp[_rel(root, p)] = _sha1(p)
        except OSError:
            fp[_rel(root, p)] = None
    return fp


def _loaded_modules(root):
    """Source files of every ovtsuite module imported so far, root-relative."""
    mods = set()
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f and (name == "ovtsuite" or name.startswith("ovtsuite.")):
            mods.add(_rel(root, f))
    return sorted(mods)


def _read_deps(depfile):
    try:
        with open(depfile, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_atomic(path, data):
    # temp + replace: never leaves a half-written file, and works on mounts
    # that refuse in-place writes to hard-linked files.
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    bin_dir = os.path.join(root, "Reference", "Binaries")
    if_stale = False
    for a in sys.argv[1:]:
        if a.startswith("--bin-dir="):
            bin_dir = a.split("=", 1)[1]
        elif a == "--if-stale":
            if_stale = True
    bin_dir = os.path.abspath(bin_dir)
    out = os.path.join(root, "ovtsuite", "data", "layouts.json.gz")
    depfile = os.path.join(root, "build", ".cache", "layouts.deps.json")

    if if_stale:
        deps = _read_deps(depfile)
        if (deps and os.path.exists(out)
                and deps.get("bin_dir") == _rel(root, bin_dir)
                and deps.get("inputs") == _fingerprint(
                    root, bin_dir, out, deps.get("modules", []))):
            print("layouts up to date (%d inputs unchanged) -- skipped"
                  % len(deps["inputs"]))
            return
        print("layouts stale%s -- re-cataloguing"
              % ("" if deps else " (no previous dependency record)"))

    rows = []
    _name_to_family = {v: k for k, v in REF_BINARIES.items()}
    global _NAME_TO_FAMILY
    _NAME_TO_FAMILY = _name_to_family
    for path in _candidates(bin_dir):
        data = open(path, "rb").read()
        if len(data) not in (0x100000, 0x200000):
            continue
        pt = layout.pointer_table(data)
        if pt is None or not pt.from_default:
            print("skip %s: no default pointer array (not a full read?)"
                  % os.path.basename(path))
            continue
        region = find_cal_region(data)
        fam = detect_family(data, region) if region else None
        # A file that IS a family's own named reference binary (REF_BINARIES)
        # is tagged as that family even if detect_family's own candidate scan
        # can't reach it -- e.g. a sub-family deliberately excluded from
        # kb.FAMILIES so it can't compete in detect_family's scoring (see
        # session.py _DATASET_ID_SUBFAMILY). Being the literal named
        # reference is stronger, exact evidence than the general scan.
        name = os.path.basename(path)
        ref_family = _NAME_TO_FAMILY.get(name)
        if ref_family is not None:
            fam = ref_family
        if fam is None:
            print("skip %s: no KB family" % os.path.basename(path))
            continue
        info = ident16(data, region)
        rows.append({
            "family": fam, "file": name,
            "sw": info.get("bosch_sw", ""), "dataset": info.get("dataset_id", ""),
            "hw_part": info.get("hw_part", ""), "sw_part": info.get("sw_part", ""),
            "region": list(region), "primaries": pt.primaries,
            "reference": REF_BINARIES.get(fam) == name,
        })
        print("%-24s %-11s sw=%s ds=%s  %d blocks, %d redirected%s" % (
            name, fam, rows[-1]["sw"], rows[-1]["dataset"], pt.count,
            pt.redirected, "  [reference]" if rows[-1]["reference"] else ""))
    for fam, ref in REF_BINARIES.items():
        if not any(r["family"] == fam and r["reference"] for r in rows):
            print("WARNING: reference binary %s for %s not catalogued" % (ref, fam))
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    try:
        with gzip.open(out, "rb") as fh:
            existing = fh.read()
    except (OSError, EOFError):
        existing = None
    if existing == payload:
        print("%d layouts -> %s unchanged (file left as is)" % (len(rows), _rel(root, out)))
    else:
        _write_atomic(out, gzip.compress(payload, mtime=0))
        print("%d layouts -> %s (%d KB)"
              % (len(rows), _rel(root, out), os.path.getsize(out) // 1024))

    # Record exactly what this run depended on, for the next --if-stale.
    modules = _loaded_modules(root)
    os.makedirs(os.path.dirname(depfile), exist_ok=True)
    _write_atomic(depfile, json.dumps({
        "bin_dir": _rel(root, bin_dir),
        "modules": modules,
        "inputs": _fingerprint(root, bin_dir, out, modules),
    }, indent=1, sort_keys=True).encode("utf-8"))


if __name__ == "__main__":
    main()
