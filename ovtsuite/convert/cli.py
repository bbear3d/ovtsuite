# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""``ovtsuite convert``: the converter from the command line.

    ovtsuite convert BPW.a2l --to xdf --bin car.bin -o BPW.xdf
    ovtsuite convert defs/ --to json -o out/            (a folder: batch)
    ovtsuite convert in.hex --to bin --size 0x200000
    ovtsuite convert x.xdf --preset english.json
    ovtsuite convert SC4800.a2l --bin 49AEfF.s19 --to dcm      (export values)
    ovtsuite convert values.cdfx --to dcm                      (convert values)
    ovtsuite convert SC4800.a2l delta.dcm --bin in.s19 --to srec -o out.s19
                                                         (apply values)
    ovtsuite convert FK.a2l --bin AA.bin --port --reference FK.hex --to xdf
                                     (port onto another software version)
    ovtsuite convert old.a2l new.a2l --to diff -o changes.csv   (compare)
    ovtsuite convert map.xdf --bin stock.bin --patch-with tuned.bin -o map.xdf
                                        (an XDF with the tune as a patch)
    ovtsuite convert ecu_defs.xml --list-roms --bin car.bin
                                        (the ROMs of a RomRaider file)
    ovtsuite convert ecu_defs.xml --rom A2WC522N --to xdf -o A2WC522N.xdf
    ovtsuite convert --watch inbox/ --to xdf -o out/        (a watch folder)
    ovtsuite convert BPW.a2l --watch bins/ --to dcm    (each binary's values)
    ovtsuite convert --shell-menu install     ("Convert with OVT Converter")

Exit status: 0 done, 1 an error, 2 written but something was not expressed
fully (see the report beside the output) -- unless ``--allow-loss``.
"""

from __future__ import annotations

import json
import os
import sys

from . import DEF_OUT, DIFF, EXT, IMG, VAL, ConvertError, Job, run, sniff, sniff_file


def _int(s):
    return int(str(s), 0)


def add_parser(sub, parents=()):
    s = sub.add_parser(
        "convert", parents=list(parents),
        help="convert definitions (XDF, A2L, DAM, OLS, KP, JSON, CSV), "
             "calibration values (DCM, CDFX) and binaries (BIN, Intel HEX, "
             "S-record) with no project open")
    s.add_argument("sources", nargs="*", help="files, or a folder to convert "
                   "every recognised file in (with --watch: the files every arriving "
                   "file is converted with, e.g. a definition)")
    s.add_argument("--to", dest="target", choices=DEF_OUT + IMG + VAL + (DIFF,),
                   help="target format (default: from -o's extension)")
    s.add_argument("-o", "--out", help="output file (or folder, for a batch)")
    s.add_argument("--bin", dest="binary", help="optional binary: settles the "
                   "base, sizes stored in the image and signedness")
    s.add_argument("--base", type=_int, help="definition address of image offset 0")
    s.add_argument("--size", type=_int, help="image size in bytes")
    s.add_argument("--title")
    s.add_argument("--dynamic", choices=("max", "skip"), default=None,
                   help="tables whose sizes the image stores, with no binary "
                   "(default: max, or the preset's)")
    s.add_argument("--category", action="append", default=[],
                   help="keep only tables in this category (repeatable)")
    s.add_argument("--pattern", help="keep only tables whose name, symbol or "
                   "description matches this regular expression")
    s.add_argument("--translate", choices=("builtin", "online"))
    s.add_argument("--provider", help="online translator (default: the one "
                   "chosen in Options)")
    s.add_argument("--from-lang", default="auto")
    s.add_argument("--to-lang", default="en")
    s.add_argument("--translate-mode", choices=("replace", "bilingual", "keep"),
                   default="replace")
    s.add_argument("--checksum", default="none",
                   help="XDF checksum block: none, keep, auto, a descriptor id, "
                   "tp:<plugin id>:<method> for an installed TunerPro plugin "
                   "(see --list-checksums), or dll:<path>")
    s.add_argument("--list-checksums", action="store_true",
                   help="list the checksum plugins installed modules offer, and "
                   "the TunerPro plugins installed on this PC")
    s.add_argument("--a2l-addresses", choices=("offset", "ecu"), default=None,
                   help="A2L: file offsets or ECU addresses (default: offset, "
                   "or the preset's)")
    s.add_argument("--no-indexed", action="store_true",
                   help="XDF: leave out value blocks without an axis")
    s.add_argument("--record", type=int, help="HEX / S-record bytes per record")
    s.add_argument("--swap16", action="store_true", help="byte-swap 16-bit words")
    s.add_argument("--port", action="store_true",
                   help="the binary is another software version than the definition "
                   "was made for: place every table in it by its own evidence")
    s.add_argument("--reference", help="with --port: the image the definition was "
                   "made for (its HEX / S19 / BIN); finds tables that moved by their bytes")
    s.add_argument("--unverified", choices=("category", "skip"), default="category",
                   help="with --port: tables nothing confirms go into the 'Unverified' "
                   "category (default) or are left out")
    s.add_argument("--rom", metavar="XMLID",
                   help="RomRaider XML (many ROMs in one file): the ROM to use, by "
                   "its xmlid (default: the one whose id string is in --bin)")
    s.add_argument("--list-roms", action="store_true",
                   help="list the ROMs of each RomRaider source (and, with --bin, "
                   "which one it is) and exit")
    s.add_argument("--patch-with", metavar="CHANGED",
                   help="XDF: add a TunerPro patch that turns the binary into this "
                   "changed one")
    s.add_argument("--patch-title", help="the patch's name (default: from the file)")
    s.add_argument("--patch-split", choices=("one", "table"), default="one",
                   help="one patch, or one per changed table")
    s.add_argument("--patch-outside", choices=("keep", "skip"), default="keep",
                   help="bytes outside every table (checksums, code): in the patch "
                   "(default) or left out")
    s.add_argument("--fix-checksums", action="store_true",
                   help="image targets: correct the checksums afterwards (through "
                   "the installed module for the platform)")
    s.add_argument("--crop", nargs=2, type=_int, metavar=("START", "END"))
    s.add_argument("--fill", type=_int)
    s.add_argument("--no-check", action="store_true",
                   help="skip reading the result back")
    s.add_argument("--allow-loss", action="store_true",
                   help="exit 0 even when something was not expressed fully")
    s.add_argument("--watch", metavar="FOLDER",
                   help="watch a folder: convert every file that arrives in it (to "
                   "-o, default FOLDER/Converted) until Ctrl+C")
    s.add_argument("--watch-after", choices=("leave", "move"), default="leave",
                   help="with --watch: leave a converted file where it is, or move it "
                   "to FOLDER/Done")
    s.add_argument("--watch-existing", action="store_true",
                   help="with --watch: also convert the files already there")
    s.add_argument("--watch-subfolders", action="store_true",
                   help="with --watch: watch the folders inside it too")
    s.add_argument("--shell-menu", choices=("install", "remove", "status"),
                   help="add or remove \"Convert with OVT Converter\" in the file "
                   "manager's right-click menu (opt-in; no file associations)")
    s.add_argument("--preset", help="a saved job (JSON) to start from")
    s.add_argument("--save-preset", help="save this job as a preset and exit")
    s.set_defaults(fn=cmd_convert)
    return s


def job_from_args(a) -> Job:
    base = {}
    if a.preset:
        with open(a.preset, encoding="utf-8") as fh:
            base = json.load(fh)
    job = Job.from_dict(dict(base, sources=a.sources, target=a.target or base.get("target", "")))
    if a.out:
        job.out = a.out
    for k in ("binary", "base", "size", "title"):
        if getattr(a, k) is not None:
            setattr(job, k, getattr(a, k))
    if a.dynamic is not None:               # a preset's own choice stands
        job.dynamic = a.dynamic
    if a.category or a.pattern:
        job.filters = dict(job.filters)
        if a.category:
            job.filters["categories"] = a.category
        if a.pattern:
            job.filters["pattern"] = a.pattern
    if a.translate:
        job.translate = {"backend": a.translate, "provider": a.provider or "",
                         "source": a.from_lang, "target": a.to_lang,
                         "mode": a.translate_mode}
    if a.checksum != "none" or not job.checksum:
        job.checksum = a.checksum
    o = dict(job.options)
    if a.a2l_addresses is not None:
        o["a2l_addresses"] = a.a2l_addresses
    if a.no_indexed:
        o["indexed"] = False
    if a.record:
        o["record"] = a.record
    if a.swap16:
        o["swap16"] = True
    if getattr(a, "fix_checksums", False):
        o["checksum"] = True
    if getattr(a, "patch_with", None):
        o["patch_with"] = a.patch_with
        o["patch_split"] = a.patch_split
        o["patch_outside"] = a.patch_outside
        if a.patch_title:
            o["patch_title"] = a.patch_title
    if getattr(a, "rom", None):
        o["rom"] = a.rom
    if getattr(a, "port", False):
        o["port"] = True
        o["unverified"] = a.unverified
        if a.reference:
            o["reference"] = a.reference
    if a.crop:
        o["crop"] = list(a.crop)
    if a.fill is not None:
        o["fill"] = a.fill
    job.options = o
    if a.no_check:
        job.self_check = False
    return job


def _target_of(job: Job) -> str:
    if job.target:
        return job.target
    out = (job.out or "").lower()
    for fmt, ext in sorted(EXT.items(), key=lambda kv: -len(kv[1])):
        if out.endswith(ext):
            return fmt
    if out.endswith(".s28") or out.endswith(".s37") or out.endswith(".mot"):
        return "srec"
    if out.endswith(".json"):
        return "json"
    if out.endswith(".cdf"):
        return "cdfx"
    raise ConvertError("Give --to, or an output name with a known extension.")


def _expand(job: Job):
    """One job per source file when a source is a folder (batch)."""
    files = []
    for s in job.sources:
        if os.path.isdir(s):
            for n in sorted(os.listdir(s)):
                p = os.path.join(s, n)
                if os.path.isfile(p):
                    files.append(p)
        else:
            files.append(s)
    if len(files) == len(job.sources) and not any(os.path.isdir(s) for s in job.sources):
        return [job]
    jobs = []
    outdir = job.out or "."
    os.makedirs(outdir, exist_ok=True)
    for p in files:
        try:
            kind, _t = sniff_file(p, 1 << 16)
        except OSError:
            continue
        if job.target in IMG:
            want = kind == "image"
        elif job.target in VAL:
            want = kind == "values" or (kind == "definition" and bool(job.binary))
        else:
            want = kind == "definition"
        if not want:
            continue
        j = Job.from_dict(job.to_dict())
        j.sources = [p]
        stem = os.path.splitext(os.path.basename(p))[0]
        j.out = os.path.join(outdir, stem + EXT[job.target])
        jobs.append(j)
    return jobs


def _list_roms(a) -> int:
    from . import fmt_romxml
    image = None
    if a.binary:
        from . import image as _image
        try:
            with open(a.binary, "rb") as fh:
                image = _image.read(fh.read()).flat()
        except Exception as exc:                                # noqa: BLE001
            print("error: %s: %s" % (a.binary, exc), file=sys.stderr)
            return 1
    found = False
    for src in a.sources:
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        if not fmt_romxml.is_romraider(data):
            print("%s: not a RomRaider definition (one ROM per file, or another "
                  "format)" % src)
            continue
        found = True
        hit = fmt_romxml.identify(data, image) if image is not None else None
        roms = fmt_romxml.rom_ids(data)
        print("%s: %d ROMs" % (src, len(roms)))
        for xid, idstr, addr, desc in roms:
            mark = "*" if hit == xid else " "
            where = ("%s @ 0x%X" % (idstr, addr)) if idstr and addr is not None else "-"
            print(" %s %-20s %-28s %s" % (mark, xid, where, desc))
        if image is not None:
            print("  this binary: %s" % (hit or "none of them (or more than one) -- "
                                         "give --rom"))
    return 0 if found else 1


def _watch(a) -> int:
    from .watch import Watcher, loop
    job = job_from_args(a)
    job.out = ""
    if not job.target:
        print("error: give --to (or a preset that names the target)", file=sys.stderr)
        return 1
    try:
        w = Watcher(a.watch, job, out_dir=a.out, after=a.watch_after,
                    existing=a.watch_existing, recursive=a.watch_subfolders)
    except ConvertError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    loop(w)
    return 0


def _shell_menu(action: str) -> int:
    from . import shellmenu
    try:
        if action == "install":
            done = shellmenu.install()
            print("Added \"%s\" to: %s" % (shellmenu.LABEL, ", ".join(done)))
        elif action == "remove":
            done = shellmenu.remove()
            print("Removed from: %s" % (", ".join(done) or "nothing (it was not there)"))
        else:
            where = shellmenu.installed()
            print("Installed in: %s" % ", ".join(where) if where else "Not installed.")
    except shellmenu.ShellMenuError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_convert(a):
    if a.list_checksums:
        from .checksums import descriptors, installed_plugins
        for d in descriptors():
            print("%-24s %s  (%s)" % (d.id, d.name, ", ".join(d.families)))
        for p in installed_plugins():
            for i, m in enumerate(p.methods):
                print("%-48s %s: %s" % (p.choice(i), p.name, m))
        return 0
    if getattr(a, "list_roms", False):
        return _list_roms(a)
    if getattr(a, "shell_menu", None):
        return _shell_menu(a.shell_menu)
    if getattr(a, "watch", None):
        return _watch(a)
    if not a.sources:
        print("error: give the files to convert", file=sys.stderr)
        return 1
    job = job_from_args(a)
    try:
        job.target = _target_of(job)
    except ConvertError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if a.save_preset:
        d = job.to_dict()
        d.pop("sources", None)
        d.pop("out", None)
        with open(a.save_preset, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        print("preset saved: %s" % a.save_preset)
        return 0
    status = 0
    for j in _expand(job):
        try:
            res = run(j)
        except (ConvertError, ValueError, OSError) as exc:
            print("error: %s: %s" % (", ".join(j.sources), exc), file=sys.stderr)
            status = 1
            continue
        print("%s -> %s: %d tables" % (", ".join(j.sources), j.out, res.tables_out)
              if res.tables_out or res.tables_in else
              "%s -> %s" % (", ".join(j.sources), j.out))
        if res.placement:
            print("  placed: %s" % ", ".join("%d %s" % (n, g) for g, n in res.placement.items() if n))
        for v in res.values[:6]:
            print("  %s" % v)
        for w in res.warnings[:10]:
            print("  note: %s" % w)
        if res.loss:
            print("  %d tables not expressed fully (report: %s.convert.txt)"
                  % (len(res.loss), j.out))
        if res.check:
            print("  read-back check: %d differences" % len(res.check))
        if res.lossy and not a.allow_loss and status == 0:
            status = 2
    return status
