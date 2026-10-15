# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Command-line interface -- the standing proof the core stays Qt-free."""

import argparse
import os
import sys


def _edc15_identify(b):
    from .edc15 import structure15 as S, checksum15, match15
    data = bytes(b.data)
    dss = S.find_datasets(data)
    # Injection system and dataset geometry are independent (038906012FD is a
    # VP37 engine on the rev.1 "P" geometry), so report what each actually says.
    inj = S.injection_of(data)
    v3 = any(d.v3 for d in dss)
    print("platform: %s (C167, little-endian, %d KB)"
          % ("MSA15 / EDC15V (VP37 rotary pump)" if v3 else "EDC15" + {
              "PD": "P (unit injector, PD)",
              "VP37": "VM+ (VP37 rotary pump)"}.get(inj, " (injection system unknown)"),
             len(b) // 1024))
    # The geometry line used to say "checksum rev.N table". There are no tables
    # any more -- the ranges come from the image's own verify driver -- so it
    # reports the layout itself, which is also finer-grained than the old
    # two-way split: a VM+ image can sit on the P grid.
    print("geometry: %s" % checksum15.dataset_layout(data))
    fam = match15.detect_family(data, dss)
    print("family:   %s" % (fam or "unknown (no EDC15 knowledge base matches)"))
    for d in dss:
        if d.v3:
            print("dataset %d: 0x%05X-0x%05X  %-9s %s  %s"
                  % (d.index, d.start, d.end, d.label, d.sig, d.ident.strip()))
            continue
        print("dataset %d: 0x%05X-0x%05X  %-4s coding(cowFUN_DSV)=%s  %s"
              % (d.index, d.start, d.end, d.label,
                 d.dsv if d.dsv is not None else "?", d.ident.strip()))
    mt = S.find_message_table(data, dss)
    if mt:
        print("message table: 0x%05X (%d messages)" % (mt.addr, mt.count))
    da, ktg = S.find_speed_slots(data)
    if da:
        print("speed task:    code 0x%05X  slots fgwDA1=0x%04X fgwDA2=0x%04X"
              % (da[0][0], da[0][1], da[0][2]))
    if checksum15.is_supported(data):
        rep = checksum15.verify(data)
        state = "CANNOT VERIFY" if rep.unreadable else ("OK" if rep.ok else "MISMATCH")
        print("checksums:     %s (%s, %d ranges from the image's own verify driver)"
              % (state, "MSA15/EDC15V" if v3 else "V4.1", len(rep.checks)))


def _zf6hp_identify(b):
    from .zf6hp import checksum6hp, fitness
    data = bytes(b.data)
    print("platform: ZF 6HP transmission control unit (MPC5xx, big-endian)")
    choice = fitness.detect_family(data)
    print("family:   %s" % choice.describe())
    own = fitness.own_binary_of(data)
    if own is not None:
        print("          this is %s's own reference image" % own.family)
    for blk in checksum6hp.find_blocks(data):
        print("block:    %-9s @0x%06X%s"
              % (blk.tag, blk.descriptor,
                 "  (calibration data)" if blk.is_data else ""))
    try:
        rep = checksum6hp.verify(data)
        print("checksum: %s" % rep.describe())
    except Exception as ex:
        print("checksum: not derivable (%s)" % ex)
    return 0


def _zf8hp_identify(b):
    from . import zf8hp as _zf8hp
    from .zf8hp import checksum8hp as _c8
    data = bytes(b.data)
    print("platform: ZF 8HP TCU (Gen1 \"8HPXY\", ZF ITCU2, Renesas SH-2A, big-endian)")
    for k, v in sorted(_zf8hp.identify(data).items()):
        print("%-11s %s" % (k + ":", v))
    for off, h in sorted(_zf8hp.find_blocks(data).items()):
        print("block:      %s @0x%06X  len 0x%X  component %s  crc 0x%06X-0x%06X"
              % (h.name, h.base, h.length, h.component, h.crc_start, h.crc_end))
    cal = _zf8hp.calibration(data)
    if cal:
        print("pointers:   %d calibration parameters in the pointer table (unnamed)"
              % len(_zf8hp.pointer_table(cal[1], cal[0])))
    if _c8.is_supported(data):
        print(_c8.verify(data).describe())
    return 0


def cmd_identify(args):
    from .binary import BinaryFile
    from .structure import find_cal_region
    from . import sgo
    from .platforms import edc15, zf6hp, vdo as _vdo
    from .platforms.identify import identify_file
    from .modules import registry as _registry
    import os as _os
    # What the base app recognises, with or without the platform's module.
    ident = identify_file(args.binary)
    print("platform: %s" % ident.describe())
    if not _registry.can(ident.platform, "definitions"):
        print("support:  %s" % _registry.missing_text(ident.platform))
        return
    with open(args.binary, "rb") as _fh:
        _head = _fh.read(0x30)
    from . import container as _cont
    if (_cont.is_container(_head, "vdo")
            or (_os.path.getsize(args.binary) == _vdo.EEPROM_SIZE
                and _vdo.detect(open(args.binary, "rb").read()))):
        # VDO instrument cluster EEPROM (.vdo or a raw 2 KB dump)
        from .session import TunerSession
        from .vdo.session_vdo import summary
        s = TunerSession(args.binary)
        print("file:    %s  (%d bytes)" % (s.binfile.name, len(s.binfile)))
        for k, v in summary(s):
            print("%-16s %s" % (k + ":", v))
        if s.load_note:
            print("note:            %s" % s.load_note)
        return
    if sgo.detect(_head):
        with open(args.binary, "rb") as _fh:
            _raw = _fh.read()
        _container = sgo.parse(_raw, _os.path.basename(args.binary))
        print("container: v2 SGML Object File (%d sections, %d unrecognized)"
              % (len(_container.sections), len(_container.unrecognized)))
        for _s in _container.sections:
            print("  section @0x%06X len=%-8d scheme=%s"
                  % (_s.addr, _s.declared_len, _s.scheme or "UNRECOGNIZED"))
        _base, _flat = sgo.image(_container)
        b = BinaryFile.from_bytes(_flat, _os.path.basename(args.binary))
    else:
        b = BinaryFile.load(args.binary)
    print("file:    %s  (%d bytes)" % (b.name, len(b)))
    if edc15.detect(bytes(b.data)):
        return _edc15_identify(b)
    if zf6hp.detect(bytes(b.data)):
        return _zf6hp_identify(b)
    from .platforms import zf8hp as _zf8hp
    if _zf8hp.detect(bytes(b.data)):
        return _zf8hp_identify(b)
    region = find_cal_region(bytes(b.data))
    variant = "EDC16U31 (2MB)" if len(b) == 0x200000 else (
        "EDC16U1 (1MB)" if len(b) == 0x100000 else "unknown layout")
    print("variant: %s" % variant)
    if region:
        print("cal:     0x%X - 0x%X" % region)
    else:
        print("cal:     not found")



def _load(args, path=None):
    """Open a binary, echoing each load step under ``-v``.

    Identifying the family and running the eight matcher passes takes seconds
    on a 2 MB image; without this the CLI simply sits there.
    """
    from .session import TunerSession        # deferred: keeps `--help` instant

    def report(label, done, total):
        sys.stderr.write("\r\033[K[%2d/%d] %s" % (done, total, label))
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")

    s = TunerSession(path or args.binary, family=getattr(args, "family", None),
                     progress=report if getattr(args, "verbose", False) else None)
    return s

def cmd_scan(args):
    from .binary import BinaryFile
    from .structure import find_cal_region, scan
    from . import edc15
    b = BinaryFile.load(args.binary)
    if edc15.detect(bytes(b.data)):
        from .edc15 import structure15 as S
        data = bytes(b.data)
        for ds in S.find_datasets(data):
            found = S.scan15(data, ds.start, ds.span)
            maps = [f for f in found if f.kind == "map"]
            print("dataset %s 0x%05X-0x%05X: %d structures (%d maps, %d curves)"
                  % (ds.label, ds.start, ds.end, len(found), len(maps),
                     len(found) - len(maps)))
            if args.verbose:
                for f in found:
                    print("  0x%06X %-5s %dx%d  ram_ptr 0x%04X%s"
                          % (f.addr, f.kind, f.rows, f.cols, f.ptr1,
                             (" / 0x%04X" % f.ptr2) if f.kind == "map" else ""))
        return 0
    region = find_cal_region(bytes(b.data))
    found = scan(bytes(b.data), region[0], region[1])
    maps = [f for f in found if f.kind == "map"]
    curves = [f for f in found if f.kind == "curve"]
    print("cal region 0x%X-0x%X: %d structures (%d maps, %d curves)"
          % (region[0], region[1], len(found), len(maps), len(curves)))
    if args.verbose:
        for f in found:
            print("  0x%06X %-5s %dx%d" % (f.addr, f.kind, f.rows, f.cols))


def cmd_coverage(args):
    from .binary import BinaryFile
    from .coverage import measure
    from .xdf import parse_xdf
    b = BinaryFile.load(args.binary)
    xdf = parse_xdf(args.xdf)
    cov = measure(bytes(b.data), xdf)
    print("binary: %s" % b.name)
    print("xdf:    %s (%d tables, %d constants)"
          % (xdf.title, len(xdf.tables), len(xdf.constants)))
    print("cal region: 0x%X - 0x%X" % cov.region)
    print("expected scannable structures: %d" % cov.expected)
    print("found at exact address:        %d  (recall %.1f%%)"
          % (cov.exact, 100 * cov.recall))
    print("exhaustive-mode hits:          %d  (recall %.1f%%)"
          % (cov.exhaustive_exact,
             100 * cov.exhaustive_exact / cov.expected if cov.expected else 0))
    print("  ...with matching shape:      %d" % cov.shape_match)
    print("total detections:              %d" % cov.detections)
    print("  coinciding with XDF structs: %d  (precision %.1f%%)"
          % (cov.matched_detections, 100 * cov.precision))
    if args.misses:
        print("missed:")
        for m in cov.missed[:args.misses]:
            print("  " + m)


def _load_family(args):
    import os
    from .binary import BinaryFile
    from . import kb
    from .match import REF_BINARIES
    if str(getattr(args, "family", "")).startswith("zf6hp"):
        raise SystemExit(
            "'%s' is a ZF 6HP family, which uses its own matcher, not the generic "
            "reference-binary KB path this command drives.\n"
            "For 6HP use the session-based commands instead:\n"
            "  ovtsuite identify <bin>          platform + family + checksum\n"
            "  ovtsuite scan <bin>              structural map/curve count\n"
            "  ovtsuite tiers <bin>             named entities grouped by tier\n"
            "  ovtsuite export <bin> out.xdf    full named-map XDF\n"
            "...or just open the file in the GUI." % args.family)
    k = kb.load(args.family)
    refname = REF_BINARIES[args.family]
    refpath = args.refbin or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "Reference", "Binaries", refname)
    return k, BinaryFile.load(refpath)


def cmd_audit(args):
    k, ref = _load_family(args)
    from .audit import audit
    r = audit(k, bytes(ref.data))
    print("family %s vs %s: %d checked, %d ok, %d unscannable, %d findings"
          % (args.family, ref.name, r.checked, r.ok, r.unscannable,
             len(r.findings)))
    kinds = {}
    for f in r.findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    print("  " + ", ".join("%s: %d" % kv for kv in sorted(kinds.items())))
    for f in r.findings:
        if args.kind and f.kind != args.kind:
            continue
        line = "  %-9s %s @0x%X" % (f.kind, f.name, f.declared)
        if f.suggestion:
            line += " -> 0x%X" % f.suggestion
        if f.note:
            line += "  (%s)" % f.note
        print(line)


def _zf6hp_named_report(args, verb):
    """Named-map summary for a ZF 6HP binary via the session matcher (the same
    path `export` and the GUI use). The generic reference-KB commands don't
    apply to 6HP -- this gives the useful answer those callers wanted."""
    import collections
    from .tiers import tier_name
    s = _load(args)
    if s.platform != "zf6hp":
        raise SystemExit("%s is not a ZF 6HP image (detected platform: %s)."
                         % (getattr(args, "binary", "file"), s.platform))
    ents = s.entities
    maps = sum(1 for e in ents if e.kind == "map")
    curves = sum(1 for e in ents if e.kind == "curve")
    print("%s via %s (6HP %s): %d named tables (%d maps, %d curves)"
          % (s.binfile.name, verb, s.family, maps + curves, maps, curves))
    by = collections.Counter(e.tier for e in ents if e.kind in ("map", "curve"))
    for tier in sorted(by):
        print("  %3d %-26s %5d" % (tier, tier_name(tier), by[tier]))
    sample = [e.name for e in ents if e.kind in ("map", "curve")][:8]
    if sample:
        print("  e.g. " + ", ".join(sample))
    print("  (full XDF: ovtsuite export <bin> out.xdf)")


def cmd_identify_in(args):
    from .binary import BinaryFile
    from .match import match
    from .structure import find_cal_region
    if str(getattr(args, "family", "")).startswith("zf6hp"):
        return _zf6hp_named_report(args, "identify-in")
    k, ref = _load_family(args)
    tgt = BinaryFile.load(args.binary)
    region = find_cal_region(bytes(tgt.data))
    r = match(k, bytes(tgt.data), region)
    c = r.counts()
    print("%s via %s: %d anchors; items:" % (tgt.name, args.family,
                                             len(r.anchors)))
    for k2 in ("anchored", "struct", "verified", "projected"):
        print("  %-9s %5d" % (k2, c.get(k2, 0)))


def cmd_diff(args):
    from .binary import BinaryFile
    from .diff import diff
    from .match import match
    from .structure import find_cal_region
    k, ref = _load_family(args)
    a = BinaryFile.load(args.stock)
    b = BinaryFile.load(args.modified)
    region = find_cal_region(bytes(a.data))
    ident = match(k, bytes(a.data), region)
    r = diff(bytes(a.data), bytes(b.data), ident, (0, min(len(a), len(b))))
    print("%s -> %s: %d differing bytes, %d named entities touched"
          % (a.name, b.name, r.diff_bytes, len(r.entities)))
    for e in r.entities:
        ch = ("axis/structure" if e.changed_cells == -1
              else "%d/%d cells" % (e.changed_cells, e.total_cells))
        ex = ""
        if e.old_vals:
            ex = "  e.g. %g -> %g %s" % (e.old_vals[0], e.new_vals[0],
                                         e.units)
        print("  %-34s %-8s %-14s (%s)%s" % (e.name, e.kind, ch,
                                             e.source, ex))
    incal = [g for g in r.gaps if region[0] <= g[0] < region[1]]
    outcal = [g for g in r.gaps if not (region[0] <= g[0] < region[1])]
    if incal:
        print("unattributed in calibration region:")
        for g in incal:
            print("  0x%X-0x%X (%d bytes)" % (g[0], g[1], g[1] - g[0]))
    if outcal:
        print("changes outside calibration region (code/checksum): "
              "%d bytes in %d ranges, 0x%X-0x%X"
              % (sum(e - s for s, e in outcal), len(outcal),
                 min(s for s, _ in outcal), max(e for _, e in outcal)))


def cmd_export(args):
    from .session import TunerSession
    from .xdf_export import export_xdf
    from . import branding
    s = _load(args)
    title = args.title or branding.tagged(s.binfile.name)
    xml = export_xdf(s.entities + s.variant_entities(), title,
                     s.binfile.name, image_size=len(s.binfile.data),
                     platform=s.platform)
    with open(args.out, "w", encoding="ascii", errors="replace") as fh:
        fh.write(xml)
    tables = sum(1 for e in s.entities if e.kind in ("map", "curve"))
    consts = sum(1 for e in s.entities if e.kind == "constant")
    print("wrote %s: %d tables, %d constants (%s family %s%s)"
          % (args.out, tables, consts, s.platform, s.family,
             ", %d datasets" % len(s.datasets) if s.is_edc15 else ""))


def cmd_tune(args):
    """Build a tune plan, show it, and apply it only if asked and if clean.

    The default is a dry run. A plan that changes 1,580 cells across 82 shift
    schedules is not something to apply because a flag was misremembered, so
    ``--apply`` is required to write anything and ``--force`` on top of that to
    write a plan the validator objected to.
    """
    s = _load(args)
    if s.platform != "zf6hp":
        print("tune profiles exist for ZF 6HP images only (this is %s)"
              % s.platform)
        return 1
    from .zf6hp import tuning

    if args.profile == "list" or not args.profile:
        for p in tuning.available():
            print("%-10s %s" % (p.name, p.title))
        return 0
    try:
        profile = tuning.get_profile(args.profile)
    except LookupError as exc:
        print(exc)
        return 1

    print(tuning.shift_anchors(s).describe())
    plan = profile.build(s)
    plan.validate()
    print(plan.describe(limit=args.limit))

    if not args.apply:
        print("\n(dry run -- pass --apply to write it)")
        return 1 if plan.errors else 0
    if plan.errors and not args.force:
        print("\nrefusing to apply: %d error(s). Read them, then --force if "
              "you still mean it." % len(plan.errors))
        return 1
    n = plan.apply(force=args.force)
    out = args.out or args.binary
    s.save(out)
    print("\napplied %s cell(s) and wrote %s" % (n, out))
    return 0


def cmd_logtune(args):
    """Analyse data logs against an EDC16 calibration and plan corrections.

    Dry run by default, like ``tune``: the findings and the planned cell
    changes are printed, and nothing is written without ``--apply``.
    """
    s = _load(args)
    if s.platform != "edc16":
        print("log tuning supports EDC16 calibrations only (this is %s)" % s.platform)
        return 1
    from .datalog.channels import ChannelNamer
    from . import logtune

    namer = ChannelNamer(s)
    if args.presets:
        for rec in logtune.RECIPES:
            from .datalog.served import unserved_for
            groups, missing = logtune.recipe_groups(namer, rec,
                                                    avoid=unserved_for(namer))
            print("%-7s groups %-18s %s" % (rec.key, ",".join(map(str, groups)) or "-", rec.title))
            if missing:
                print("        missing in this calibration: %s" % ", ".join(missing))
            print("        " + rec.how)
        return 0
    opts = logtune.Options(lambda_target=args.lambda_target, lambda_floor=args.lambda_floor,
                           correct_pilot=not args.no_pilot, correct_smoke=not args.no_smoke)
    opts.correct_soi = args.soi
    opts.tune_warmup = not args.no_warmup
    opts.eoi_target = args.eoi_target
    opts.soi_cruise_trial = args.soi_trial
    opts.soi_trial_max_iq = args.soi_trial_max_iq
    if args.smoke_step is not None:
        opts.smoke_max_step = args.smoke_step
    runs = logtune.runs_from_paths(args.logs, namer)
    base = logtune.runs_from_paths(args.baseline or [], namer)
    res = logtune.analyse(s, runs, opts, base)
    print(logtune.to_text(res, changes_limit=args.limit))
    plan = res.plan
    if not plan.effective_writes:
        return 0
    if not args.apply:
        print("\n(dry run -- pass --apply to write it)")
        return 1 if plan.errors else 0
    if plan.errors:
        print("\nrefusing to apply: %d error(s)." % len(plan.errors))
        return 1
    n = plan.apply()
    out = args.out or args.binary
    # the label names this pass in the file's version history (history-list)
    s.save(out, label=args.label or "Tune from logs: %s" % ", ".join(
        os.path.basename(p) for p in args.logs))
    print("\napplied %s cell(s) and wrote %s" % (n, out))
    return 0


def _patch_load_both(args):
    a = _load(args, args.primary)
    b = _load(args, args.comparison)
    return a, b


def cmd_patch_diff(args):
    """Diff PRIMARY vs COMPARISON by KB identity and write every candidate
    the filters allow to a patch file. Non-interactive (the GUI is where a
    person checks individual boxes); use --tier/--name/--min-grade to scope
    it, and edit the saved JSON's "selected" flags by hand for anything
    finer."""
    from . import patch as P
    primary, comparison = _patch_load_both(args)
    entries = P.diff_entities(primary, comparison)
    if args.name:
        entries = [e for e in entries if args.name.lower() in e.name.lower()]
    if args.tier is not None:
        entries = [e for e in entries if e.tier == args.tier]
    blocks = []
    if args.gaps:
        for s, e in P.diff_gaps(primary, comparison):
            if e - s >= args.min_gap:
                blocks.append(P.build_block_entry(primary, comparison, s, e))
    pt = P.new_patch(primary, comparison, entries, blocks)
    pt.save(args.out)
    print("%s -> %s: %d parameter(s), %d undefined block(s) -> %s"
          % (primary.binfile.name, comparison.binfile.name,
             len(entries), len(blocks), args.out))
    for grade, n in sorted(pt.trust_summary().items()):
        print("  %-10s %d" % (grade, n))
    return 0


def cmd_patch_inspect(args):
    from . import patch as P
    pt = P.Patch.load(args.patch)
    print("%s -> %s  (%s -> %s)  created %s"
          % (pt.source_name, pt.comparison_name, pt.source_family,
             pt.comparison_family, pt.created))
    print("%d parameter(s), %d undefined block(s)"
          % (len(pt.entries), len(pt.blocks)))
    for e in pt.entries:
        ch = ("structure differs" if e.changed_cells is None
              else "%d/%d cells" % (e.changed_cells, e.total_cells))
        if getattr(e, "changed_axes", 0):
            ch += " + %d axis point(s)" % e.changed_axes
        print("  [%s] %-34s %-8s %-16s (%s)  %s"
              % ("x" if e.selected else " ", e.name, e.kind, ch,
                 e.source_grade, e.first_change))
    for b in pt.blocks:
        print("  [%s] block @0x%X  %d byte(s)  family=%s%s"
              % ("x" if b.selected else " ", b.addr, b.length,
                 b.source_family, "  %s" % b.note if b.note else ""))
    return 0


def cmd_patch_apply(args):
    from . import patch as P
    pt = P.Patch.load(args.patch)
    target = _load(args)
    default = P.APPLY if args.trust_all else P.DISCARD
    res = P.apply_patch(pt, target, default_disposition=default,
                        source_label=os.path.basename(args.patch),
                        fix_checksum=not args.no_checksum_fix,
                        label="apply patch %s" % os.path.basename(args.patch))
    print("applied %d, skipped %d, %d conflicting with prior edits"
          % (len(res.applied), len(res.skipped), res.conflicts))
    for name, reason in res.skipped:
        print("  SKIP %-34s %s" % (name, reason))
    for w in res.warnings:
        print("  WARN %s" % w)
    if not args.apply:
        print("\n(dry run -- pass --apply to write it)")
        return 0
    out = args.out or args.binary
    target.save(out)
    print("wrote %s" % out)
    if res.record_id:
        from . import changelog as _cl
        where = ("the file's own history" if _cl.embeds(out)
                 else os.path.splitext(out)[0] + ".log")
        print("recorded as patch %s in %s -- revert it later with "
              "'patch-revert %s --id %s --apply'"
              % (res.record_id, where, out, res.record_id))
    return 0


def cmd_patch_revert(args):
    from . import patch as P
    target = _load(args)
    records = P.list_applied_patches(target)
    if args.id is None:
        if not records:
            print("no patch-apply records found in %s's change log"
                  % target.binfile.name)
            return 0
        for r in records:
            print("%-12s %-24s %-8s %2d parameter(s) %2d block(s)  %s"
                  % (r["id"], r["ts"], "pending" if r["pending"] else "saved",
                     r.get("n_entries", 0), r.get("n_blocks", 0), r["source"]))
        return 0
    record = next((r for r in records if r["id"] == args.id), None)
    if record is None:
        print("no patch record %r found (run without --id to list them)"
              % args.id)
        return 1
    rv = P.revert_applied_patch(target, record)
    print("reverted %s: %d byte range(s) restored" % (args.id, rv.n_writes))
    for w in rv.warnings:
        print("  WARN %s" % w)
    if not args.apply:
        print("\n(dry run -- pass --apply to write it)")
        return 0
    out = args.out or args.binary
    target.save(out)
    print("wrote %s" % out)
    return 0


def cmd_history_list(args):
    from . import history as H
    target = _load(args)
    versions = H.list_versions(target)
    if not versions:
        print("no version checkpoints found for %s" % target.binfile.name)
        return 0
    for v in versions:
        print("%-12s %-24s %-8s %6d byte(s) changed  %s"
              % (v["id"], v["ts"], "pending" if v["pending"] else "saved",
                 v.get("n_bytes", 0), v.get("label", "")))
    return 0


def cmd_history_revert(args):
    from . import history as H
    target = _load(args)
    try:
        rv = H.revert_to_version(target, args.id)
    except ValueError as exc:
        print(exc)
        return 1
    print("reverted %d checkpoint(s), %d byte range(s) restored"
          % (rv.n_versions, rv.n_bytes))
    for w in rv.warnings:
        print("  WARN %s" % w)
    if not args.apply:
        print("\n(dry run -- pass --apply to write it)")
        return 0
    out = args.out or args.binary
    target.save(out)
    print("wrote %s" % out)
    return 0


def cmd_patch_diff_stock(args):
    from . import patch as P
    primary = _load(args)
    try:
        reference = P.diff_against_reference(primary, family=args.family)
    except ValueError as exc:
        print(exc)
        return 1
    entries = P.diff_entities(reference, primary)
    if args.name:
        entries = [e for e in entries if args.name.lower() in e.name.lower()]
    pt = P.new_patch(reference, primary, entries)
    pt.save(args.out)
    print("%s vs its own %s reference: %d parameter(s) differ -> %s"
          % (primary.binfile.name, primary.family, len(entries), args.out))
    for grade, n in sorted(pt.trust_summary().items()):
        print("  %-10s %d" % (grade, n))
    return 0


def cmd_patch_diff_history(args):
    from . import history as H
    from . import patch as P
    target = _load(args)
    try:
        entries_pt = P.diff_from_history(target, args.older, args.newer,
                                         include_gaps=args.gaps)
    except ValueError as exc:
        print(exc)
        return 1
    if args.name:
        entries_pt.entries = [e for e in entries_pt.entries
                              if args.name.lower() in e.name.lower()]
    entries_pt.save(args.out)
    print("%s: %s -> %s: %d parameter(s), %d undefined block(s) -> %s"
          % (target.binfile.name, entries_pt.source_name,
             entries_pt.comparison_name, len(entries_pt.entries),
             len(entries_pt.blocks), args.out))
    for grade, n in sorted(entries_pt.trust_summary().items()):
        print("  %-10s %d" % (grade, n))
    return 0


def cmd_patch_batch(args):
    import glob
    from . import patch as P
    pt = P.Patch.load(args.patch)
    paths = sorted(glob.glob(os.path.join(args.dir, args.pattern)))
    if not paths:
        print("no files matching %s in %s" % (args.pattern, args.dir))
        return 1
    default = P.APPLY if args.trust_all else P.DISCARD
    results = P.batch_apply(pt, paths, family=args.family, write=args.apply,
                            out_dir=args.out_dir,
                            default_disposition=default,
                            fix_checksum=not args.no_checksum_fix,
                            source_label=os.path.basename(args.patch))
    n_ok = 0
    for path in paths:
        res = results[path]
        if res.skipped and not res.applied and res.skipped[0][0] == path:
            print("%-40s FAILED: %s" % (os.path.basename(path), res.skipped[0][1]))
            continue
        n_ok += 1
        print("%-40s applied %3d  skipped %3d  conflicts %3d"
              % (os.path.basename(path), len(res.applied), len(res.skipped),
                 res.conflicts))
    print("\n%d/%d file(s) processed%s"
          % (n_ok, len(paths),
             "" if args.apply else " (dry run -- pass --apply to write them)"))
    return 0


def cmd_mcp(args):
    """OVT-Suite as an MCP server on stdio (docs/AI_PLAN.md)."""
    if getattr(args, "print_config", False):
        import json as _json
        from .ai.mcp_server import client_config
        print(_json.dumps(client_config(), indent=2))
        return 0
    from .ai.mcp_server import serve
    return serve()


def cmd_modules(args):
    from .platforms import MODULES
    from .modules import loader, registry
    probs = registry.problems()
    for mid, info in MODULES.items():
        spec = registry.spec(mid)
        if spec is not None:
            where = loader.module_roots().get(mid, "built in")
            state = "%s %s (%s)" % (spec.version, spec.variant, where)
        elif mid in loader.disabled():
            state = "hidden by %s" % loader.ENV
        elif mid in probs:
            state = "REFUSED: %s" % probs[mid]
        else:
            state = "not installed"
        print("%-6s %-14s %s" % (mid, info.name, state))


def cmd_install_module(args):
    if getattr(args, "gui", False):
        from . import gui
        if gui.HAVE_QT:
            return gui.run_install_module(args.package)
    from .modules import installer, package
    try:
        man = installer.install(args.package,
                                installer.SYSTEM if args.system else installer.USER,
                                allow_unsigned=True if args.allow_unsigned else None)
    except (installer.InstallError, package.PackageError, OSError) as ex:
        print("not installed: %s" % ex, file=sys.stderr)
        return 2
    print("installed %s %s (%s)" % (man.name, man.version, man.variant))


def cmd_remove_module(args):
    from .modules import installer
    if not installer.uninstall(args.module,
                               installer.SYSTEM if args.system else installer.USER):
        print("%s is not installed there" % args.module, file=sys.stderr)
        return 2
    print("removed %s" % args.module)


def cmd_gui(args):
    from .gui import run, HAVE_QT
    if not HAVE_QT:
        print("PySide6 is not installed; run: pip install PySide6")
        return 1
    return run(args.binary, args.compare,
               new_instance=getattr(args, "new_instance", False))



def cmd_cmp_overlay(args):
    """Print what the comparison grid would be built from, for one table family.

    A diagnostic for "the comparison table is not visible": it runs the same three
    steps the grid does -- ``counterpart``, ``view6hp.compare_columns`` and
    ``transfer.build_overlay`` -- and prints what each returned. If this shows an
    overlay with cells and the application shows none, the application is not
    running this code.
    """
    from .session import TunerSession
    from .transfer import build_overlay
    from .speedview import compare_columns

    primary = TunerSession(args.primary)
    comparison = TunerSession(args.comparison)
    print("primary    %s  platform=%s family=%s"
          % (os.path.basename(args.primary), primary.platform, primary.family))
    print("comparison %s  platform=%s family=%s"
          % (os.path.basename(args.comparison), comparison.platform,
             comparison.family))
    if primary.platform != comparison.platform:
        print("REFUSED: a comparison must be the same platform as the primary")
        return 1

    groups = {"shift": "shift_schedules", "tcc": "tcc_schedules"}
    want = groups if args.family == "both" else {args.family: groups[args.family]}
    bad = 0
    for label, attr in want.items():
        maps = list(getattr(primary, attr, ()) or ())
        print("\n%s: %d map(s) in the primary" % (label, len(maps)))
        if not maps:
            bad += 1
            continue
        n = min(args.count, len(maps))
        for i in range(n):
            e = maps[i]
            cp = primary.counterpart(comparison, e)
            cols = compare_columns(e, cp) if cp is not None else None
            overlay = (build_overlay(e, cp, cols)
                       if (e is not None and cp is not None) else None)
            vals = getattr(overlay, "values", None) if overlay else None
            cells = sum(1 for row in (vals or ()) for v in (row or ()) if v is not None)
            print("  [%2d] primary %dx%d scan_key=%r" % (i, e.rows, e.cols,
                                                        getattr(e, "scan_key", None)))
            print("       counterpart %s" % ("None  <-- nothing to show"
                  if cp is None else "%dx%d scan_key=%r"
                  % (cp.rows, cp.cols, getattr(cp, "scan_key", None))))
            print("       compare_columns %s" % ("None (direct)" if cols is None
                                                 else "%d entries" % len(cols)))
            print("       overlay %s, %d cell(s) populated"
                  % (type(overlay).__name__ if overlay else "None", cells))
            if cells == 0:
                bad += 1
            elif vals:
                print("       row 0 %s" % (str(vals[0])[:72]))
    print("\n%s" % ("PROBLEM: at least one family produced no comparison cells"
                    if bad else "OK: every family produced comparison cells"))
    return 1 if bad else 0


def cmd_tiers(args):
    from .session import TunerSession
    s = _load(args)
    from .tiers import tier_name
    import collections
    by = collections.Counter(e.tier for e in s.entities)
    print("%s (family %s): %d entities" % (s.binfile.name, s.family,
                                           len(s.entities)))
    for tier in sorted(by):
        print("  %3d %-26s %5d" % (tier, tier_name(tier), by[tier]))


def cmd_irom_check(args):
    """Validate a C167 internal-ROM dump. Offline -- no ECU needed."""
    from .flash import irom15
    data = open(args.dump, "rb").read()
    image = open(args.image, "rb").read() if getattr(args, "image", None) else None
    rep = irom15.check(data, image=image)
    print(rep.describe())
    if image is None:
        print()
        print("Pass --image with the flash read from the same ECU to turn this into a")
        print("decision: the image stores the checksum its boot path expects of the ROM.")
    elif rep.expected_sum is None:
        print()
        print("That image does not state an expected internal-ROM checksum, so only the")
        print("structural checks above apply.")
    if not rep.ok:
        print()
        print("Re-read before using this dump. The C167's failure mode is silent: a wrong")
        print("bus/register configuration returns every other byte doubled rather than an error.")
    return 0 if rep.ok else 1


def cmd_checksum(args):
    """Verify (and with --fix correct) a binary's checksums through the module
    that serves its platform -- the same provider the app and the flasher use,
    so a platform whose module is not installed says so instead of failing."""
    from . import mpc
    from .modules import registry
    from .platforms.identify import identify
    data = open(args.binary, "rb").read()
    # A compressed .MPC (the internal-flash image as BDM/Dimsport tools export
    # it -- XOR-0xD9'd zlib, see ovtsuite.mpc) is decoded transparently here:
    # verify/correct always work on the plain image, and --fix re-compresses
    # before writing back out, so a compressed input stays compressed.
    compressed = mpc.is_encoded(data)
    trailer = b""
    if compressed:
        data, trailer = mpc.decode(data)
    ident = identify(data)
    cs = registry.checksum(ident.platform)
    if cs is None:
        if not ident.known:
            print("checksum: unrecognised binary -- no platform claims it")
        elif ident.platform in ("hvac", "vdo"):
            print("checksum: %s images are checked in the app or the flasher "
                  "(open the file there)" % ident.name)
        else:
            print("checksum: %s -- %s" % (ident.name, registry.missing_text(
                ident.platform, "checksum correction")))
        return 1
    opts = {}
    if getattr(args, "key_image", None):
        # The internal-flash (.MPC) image carries a block signature but not the
        # modulus that verifies/re-signs it -- that lives in the paired external
        # 2 MB flash. Pass it so verify/correct can check and re-sign the .MPC.
        opts["key_image"] = open(args.key_image, "rb").read()
    # ZF 6HP: also the program/boot descriptor self-CRCs, which a code-only
    # extract has even when its calibration layout cannot be read
    program_area = bool(getattr(args, "program_area", False)
                        and getattr(cs, "find_descriptors", None) is not None
                        and cs.find_descriptors(data))
    if program_area:
        opts["program_area"] = True
    if not cs.is_supported(data) and not program_area:
        print("checksum: %s" % registry.checksum_unsupported_text(
            ident.platform, cs, data))
        return 1
    report = cs.verify(data, **opts)
    print(report.describe())
    if not args.fix:
        return 0 if report.ok else 2
    if report.ok:
        print("already valid - nothing to write")
        return 0
    fixed = cs.correct(data, **opts)
    out_data = mpc.encode(fixed, trailer) if compressed else fixed
    out = args.out or args.binary
    with open(out, "wb") as fh:
        fh.write(out_data)
    print("corrected -> %s%s" % (out, "  (re-compressed .MPC)" if compressed else ""))
    print(cs.verify(fixed, **opts).describe())
    return 0


def cmd_frf_info(args):
    from . import frf
    data = open(args.frf, "rb").read()
    if not frf.detect(data):
        print("not a recognized .frf file (outer FRF decrypt didn't yield a ZIP)")
        return 1
    c = frf.parse(data, name=os.path.basename(args.frf))
    print("file  : %s" % os.path.basename(args.frf))
    print("ecu   : %s   spare part: %s" % (c.ecu_id, c.spare_part_number))
    sw = c.ident_values.get("EI_VWAppliSoftwVersiNumbe")
    if sw:
        print("sw versions this container matches: %s" % ", ".join(sw))
    if c.sa2_tape is not None:
        known = c.sa2_tape_is_known_write_tape
        print("SA2 write tape: %s  (%s)" % (
            c.sa2_tape.hex(),
            "matches the already-solved ZF6HP_WRITE_TAPE" if known
            else "DIFFERENT from ZF6HP_WRITE_TAPE -- do not assume it works, verify separately"))
    if c.platform:
        print("platform: %s" % {"zf6hp": "ZF 6HP", "zf8hp": "ZF 8HP"}.get(c.platform, c.platform))
    print("\n  block     scheme  crc_ok  len        base       calibration")
    for b in c.blocks:
        print("  %-9s %-7s %-7s %-10d %-10s %s" % (
            b.short_name, b.scheme or "UNRECOGNIZED", b.crc_ok, b.declared_len,
            ("0x%X" % b.start_address) if b.start_address is not None else "-",
            b.is_calibration))
    if c.unrecognized:
        print("\n%d block(s) did not decode/verify -- see ovtsuite/frf.py's module docstring "
              "before trusting anything about them." % len(c.unrecognized))
    return 0


def cmd_frf_extract(args):
    from . import frf
    data = open(args.frf, "rb").read()
    c = frf.parse(data, name=os.path.basename(args.frf))
    os.makedirs(args.outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.frf))[0]
    wrote = 0
    for b in c.blocks:
        if b.plaintext is None:
            print("skipping %s: unrecognized, not extracted" % b.short_name)
            continue
        name = "%s_%s.bin" % (stem, b.short_name)
        with open(os.path.join(args.outdir, name), "wb") as fh:
            fh.write(b.plaintext)
        wrote += 1
        print("%s -> %s (%d bytes%s)" % (
            b.short_name, name, len(b.plaintext),
            ", ZF6HP calibration" if b.looks_like_zf6hp_calibration else
            (", ZF8HP calibration" if b.looks_like_zf8hp_calibration else "")))
    print("\n%d/%d block(s) extracted." % (wrote, len(c.blocks)))
    if c.unrecognized:
        print("%d block(s) skipped (unrecognized)." % len(c.unrecognized))
    return 0


def cmd_frf_image(args):
    from . import frf
    data = open(args.frf, "rb").read()
    c = frf.parse(data, name=os.path.basename(args.frf))
    im = frf.image(c)
    with open(args.out, "wb") as fh:
        fh.write(im.data)
    print("wrote %s: %d KB full flash image (%s)"
          % (args.out, im.size // 1024, c.spare_part_number or c.ecu_id))
    print("  placements: %s"
          % ", ".join("%s@0x%X" % (k, v)
                      for k, v in sorted(im.placements.items(), key=lambda kv: kv[1])))
    if im.inferred:
        print("  inferred (no descriptor): %s" % ", ".join(im.inferred))
    if im.absent:
        print("  NOT carried by the .frf (bootloader, left 0xFF): %s"
              % ", ".join("0x%X-0x%X" % r for r in im.absent))
    from .zf6hp import checksum6hp as _c6
    if _c6.is_supported(im.data):
        print("  " + _c6.verify(im.data).describe())
    from .zf8hp import checksum8hp as _c8
    if _c8.is_supported(im.data):
        print("  " + _c8.verify(im.data).describe().replace("\n", "\n  "))
    return 0


def cmd_container_info(args):
    """What a native container (.edc16 / .edc15 / .6hp / .hvac) holds, and
    whether its stored definition would be used."""
    from . import container as C
    from . import defn
    raw = open(args.path, "rb").read()
    c = C.Container.from_bytes(raw)
    print("file    : %s" % os.path.basename(args.path))
    print("flavour : %s (%s)   size %d bytes" % (c.fmt.title, c.ext, len(raw)))
    print("\n  block     type  address   bytes")
    for b in c.blocks:
        print("  %-9s %-5d 0x%06X  %d" % (b.label(), b.type, b.address, b.size))
    b = c.get(C.BLOCK_DEFN)
    if b is None:
        print("\ndefinition: none stored")
        return 0
    import json
    snap = json.loads(bytes(b.data).decode("utf-8"))
    ext = c.external()
    why = defn.check(snap, c.kind, bytes(ext)) if ext is not None else "no image"
    print("\ndefinition: %d entities, %d bytes -> %s"
          % (len(snap.get("ents", [])), b.size,
             "valid for this build" if why is None else "stale (%s)" % why))
    return 0


def cmd_dim_info(args):
    from . import dim
    from . import mpc
    chunks = dim.load(args.dim)
    data = open(args.dim, "rb").read()
    print("file  : %s" % os.path.basename(args.dim))
    print("size  : %d (0x%X)   chunks: %d\n" % (len(data), len(data), len(chunks)))
    print("  #  magic off        A        C         t1        t2        content")
    for c in chunks:
        kind, out = dim.decode_chunk(c)
        if kind == "bin":
            what = "%d bytes (0x%X)" % (len(out), len(out))
            if c.index == 1:
                what += " [%s]" % ("mpc-wrapped" if mpc.is_encoded(out) else "plain")
        else:
            txt = out.decode("latin-1")
            what = "raw: " + (txt if len(txt) < 120 else txt[:117] + "...")
        print("  %-2d %-5s 0x%06X  %-7d  %08X  %08X  %08X  %s"
              % (c.index, c.magic.decode().strip(), c.off, c.A, c.C, c.t1, c.t2, what))
    end = chunks[-1].off + dim.HDR + chunks[-1].A
    print("\nchain ends at 0x%X, file is 0x%X -> %s"
          % (end, len(data), "OK" if end == len(data) else "MISMATCH"))
    return 0


def cmd_dim_extract(args):
    from . import dim
    from . import mpc
    import json
    chunks = dim.load(args.dim)
    os.makedirs(args.outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.dim))[0]
    meta = []
    for c in chunks:
        kind, out = dim.decode_chunk(c)
        ext = dim.EXT.get(c.index, "txt" if kind == "raw" else "bin")
        name = "%s_%d.%s" % (stem, c.index, ext)
        with open(os.path.join(args.outdir, name), "wb") as fh:
            fh.write(out)
        meta.append(dict(index=c.index, type=c.type, magic=c.magic.decode(),
                         t1=c.t1, t2=c.t2, kind=kind, file=name))
        print("chunk %d -> %s (%d bytes)" % (c.index, name, len(out)))
        if c.index == 1 and mpc.is_encoded(out):
            # Convenience extra: the internal flash's OWN .mpc framing
            # decoded too, so it's immediately usable with `ovtsuite
            # checksum` or for inspection -- not part of the dim-build
            # round trip, which reads the chunk 1 file back in as-is.
            decoded = mpc.decode_auto(out)
            dname = "%s_1_decoded.bin" % stem
            with open(os.path.join(args.outdir, dname), "wb") as fh:
                fh.write(decoded)
            print("       -> %s (%d bytes, internal flash decoded)"
                  % (dname, len(decoded)))
    with open(os.path.join(args.outdir, "dim.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    print("metadata -> dim.json")
    return 0


def cmd_dim_extract_mpc(args):
    from . import dim
    chunks = dim.load(args.dim)
    if args.compressed:
        data = dim.internal_flash_mpc(chunks)
    else:
        data = dim.internal_flash(chunks)
    with open(args.out, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes, %s)"
          % (args.out, len(data), "compressed .mpc" if args.compressed else "plain image"))
    return 0


def cmd_dim_extract_eeprom(args):
    from . import dim
    chunks = dim.load(args.dim)
    data = dim.eeprom(chunks)
    with open(args.out, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes)" % (args.out, len(data)))
    return 0


def cmd_dim_build(args):
    from . import dim
    import json
    with open(os.path.join(args.indir, "dim.json")) as fh:
        meta = json.load(fh)
    chunks = []
    for m in meta:
        with open(os.path.join(args.indir, m["file"]), "rb") as fh:
            data = fh.read()
        c = dim.encode_chunk(m["index"], m["type"], m["t1"], m["t2"],
                             m["kind"], data, m["magic"].ljust(4).encode())
        chunks.append(c)
        print("chunk %d <- %s (%d bytes -> %d)" % (m["index"], m["file"], len(data), c.A))
    out_bytes = dim.build(chunks)
    with open(args.dim, "wb") as fh:
        fh.write(out_bytes)
    print("wrote %s (%d bytes)" % (args.dim, len(out_bytes)))
    ok = True
    for c, m in zip(dim.load(args.dim), meta):
        _kind, got = dim.decode_chunk(c)
        with open(os.path.join(args.indir, m["file"]), "rb") as fh:
            want = fh.read()
        good = got == want
        ok = ok and good
        print("  verify chunk %d: %s" % (c.index, "OK" if good else "FAILED"))
    return 0 if ok else 1


def _edc15_datasets(s):
    print("EDC15 datasets (each a complete calibration, selected by coding):")
    for d in s.datasets:
        dm = s.match15.datasets[d.index]
        c = dm.counts()
        print("  %-4s 0x%05X-0x%05X coding %-3s %s"
              % (d.label, d.start, d.end,
                 d.dsv if d.dsv is not None else "?", d.ident.strip()))
        print("        %d parameters placed (%s), %d unnamed structures, %d unmatched"
              % (len(dm.items), ", ".join("%s %d" % kv for kv in sorted(c.items())),
                 len(dm.unnamed), len(dm.unmatched)))
    return 0


def cmd_variants(args):
    from .session import TunerSession
    from .variant import (find_variant_tables, coding_domains, param_domain)
    from collections import OrderedDict, defaultdict
    s = _load(args)
    if s.is_edc15:
        return _edc15_datasets(s)
    vt = find_variant_tables(s)
    if not vt:
        print("no secondary/variant calibration block in this binary.")
        return 0
    methods = {v.method for v in vt}
    if methods == {"array"}:
        method = "exact pointer-array diff (full read)"
    elif "anchor" in methods:
        method = "active-array anchor alignment (exact) + axis fingerprint"
    else:
        method = "axis fingerprint (approximate)"
    differ = [v for v in vt if v.differs]
    domains = coding_domains(vt)
    print("Secondary/variant block found.")
    print("Method: %s" % method)
    print("%d variant parameters (%d differ from the primary)." % (len(vt), len(differ)))
    print("Coding domains present (by differing parameters):")
    for dom, n in domains.most_common():
        print("   - %s: %d" % (dom, n))
    if method.startswith("active-array"):
        print("   (calibration-only read: the default array is absent, so an "
              "'other / uncategorised'\n    group on a non-P350 family may include "
              "false pairings -- a full read confirms.)")
    print("\nDiffering variant parameters, grouped by coding domain:")
    by_dom = defaultdict(lambda: OrderedDict())
    for v in sorted(differ, key=lambda x: (param_domain(x.name), x.name)):
        by_dom[param_domain(v.name)].setdefault(v.name, []).append(v)
    order = [d for d, _ in domains.most_common()]
    for dom in order:
        print("\n  [%s]" % dom)
        for name, g in by_dom[dom].items():
            copies = ", ".join("0x%06X" % x.sec_z for x in g)
            unc = "  (name uncertain)" if any(x.ambiguous for x in g) else ""
            print("    %-30s %-5s %2dx%-2d  primary@0x%06X  variant@%s%s"
                  % (name, g[0].kind, g[0].rows, g[0].cols, g[0].main_z,
                     copies, unc))
    if args.all:
        print("\nAll %d variant parameters (incl. identical copies):" % len(vt))
        for v in vt:
            print("  %-30s %-5s primary@0x%06X sec@0x%06X %s%s"
                  % (v.name, v.kind, v.main_z, v.sec_z,
                     "[differs]" if v.differs else "[copy]",
                     " (uncertain)" if v.ambiguous else ""))
    return 0


def cmd_gearvn(args):
    """Gear-ratio + v/n (gear-detection) + cruise parameters - the set that
    must change when fitting taller gears so cruise still recognises the gear."""
    from .session import TunerSession
    s = _load(args)
    if s.is_edc15:
        return _edc15_gearvn(s)
    byname = {e.name: e for e in s.entities}
    groups = [
        ("Gearbox ratios (per gear)",
         ["Gearbx_rTrans1_C", "Gearbx_rTrans2_C", "Gearbx_rTrans3_C",
          "Gearbx_rTrans4_C", "Gearbx_rTrans5_C", "Gearbx_rTrans6_C",
          "Gearbx_rTransR_C"]),
        ("v/n detection bands (gear = speed/rpm window; raise for taller gears)",
         ["Gearbx_rVn1L_C", "Gearbx_rVn1H_C", "Gearbx_rVn2L_C", "Gearbx_rVn2H_C",
          "Gearbx_rVn3L_C", "Gearbx_rVn3H_C", "Gearbx_rVn4L_C", "Gearbx_rVn4H_C",
          "Gearbx_rVn5L_C", "Gearbx_rVn5H_C", "Gearbx_rVn6L_C", "Gearbx_rVn6H_C"]),
        ("Cruise / v/n tolerances",
         ["CoVeh_rVnThreshold_C", "CrCtl_rVnDvtAbs_C", "CrCtl_rVnDvtShutOff_C",
          "VehDa_rVnMax_C", "VehDa_tiVnPT1_C", "LIGov_rTransSet_CUR",
          "CrCtl_facParCor_CUR"]),
    ]
    # secondary/variant copies (auto vs manual coding), keyed by primary addr
    import dataclasses
    from . import variant as _variant
    varmap = {}
    for vt in _variant.find_variant_tables(s):
        if vt.differs:
            varmap.setdefault(vt.main_z, []).append(vt.sec_z)
    has_variant = bool(varmap)
    if has_variant:
        print("\n(a secondary coding block is present - 'variant' column is the "
              "alternate auto/manual copy)")

    def scaled(ent, addr):
        try:
            return "%.4g" % dataclasses.replace(ent, addr=addr).cell(0, 0)
        except Exception:
            return "?"

    for title, names in groups:
        print("\n%s:" % title)
        for n in names:
            e = byname.get(n)
            if not e:
                continue
            v = scaled(e, e.addr)
            u = " %s" % e.units if e.units else ""
            var = ""
            for sec in varmap.get(e.addr, []):
                var += "  | variant@0x%06X = %s" % (sec, scaled(e, sec))
            print("  %-22s 0x%06X = %-8s%s   %s%s"
                  % (n, e.addr, v, u,
                     e.description.split("|")[-1].strip()[:30], var))
    return 0


def cmd_coding(args):
    """Stored ECU coding values (USAp_dCodeTbl + inverse) and the gearbox/variant
    each one selects. Answers 'which VCDS coding corresponds to which block'."""
    from .session import TunerSession
    from .variant import coding_states
    s = _load(args)
    if s.is_edc15:
        return _edc15_datasets(s)
    states = coding_states(s)
    if not states:
        print("no coding constant (USAp_dCodeTbl) found in this binary.")
        return 0
    print("Stored coding values (decoded by digit scheme:"
          " last=transmission, 2nd-last=A/C):\n")
    print("  %-8s %-18s %-13s %s"
          % ("coding", "transmission", "A/C", "gears rTrans1..6"))
    for c in states:
        g = ", ".join("%.2f" % x for x in c["gears"]) if c["gears"] else "-"
        print("  %-8d %-18s %-13s %s"
              % (c["coding"], c["transmission"], c["ac"], g))
        if args.verbose:
            print("        0x%08X  value @0x%06X%s"
                  % (c["coding"], c["tbl_addr"],
                     "" if c["inv_ok"] else "  (inverse MISMATCH)"))
    print("\n  %d coding(s) stored. Last digit: 1=5MT 2=6MT 4=auto(TC) 6=CVT"
          " 8=DSG; 2nd-last: 3=manual A/C 7=Climatronic." % len(states))
    return 0


def _edc15_gearvn(s):
    from .edc15.gears15 import GearRatios15
    g = GearRatios15(s)
    print("EDC15 gear recognition: gear = v/n window between consecutive "
          "thresholds (raw/25600 (km/h)/rpm). Raise the thresholds for taller "
          "gearing so the gear-dependent maps and cruise still see the right gear.")
    for cp in g.copies():
        print("\n[%s]" % cp.label)
        for name, e, r, rpm in g.thresholds(cp.group):
            print("  %-10s 0x%06X = %7.5f (km/h)/rpm  (%5.0f rpm at 100 km/h)  %s"
                  % (name, e.addr, r, rpm, e.description.split("|")[0].strip()[:40]))
        for lbl, n in g.abs_layout():
            e = cp.by_name.get(n)
            if e is not None:
                print("  %-10s 0x%06X = %-8g %s" % (n, e.addr, e.cell(0, 0), lbl))
    return 0


def _edc15_vss(s, args):
    from .edc15 import vss15
    st = vss15.speed_settings(s)
    if not st.available:
        print("speed-task code signature not found - cannot locate fgwDA1/fgwDA2.")
        return 1
    print("EDC15 vehicle speed:  v = SF * pulses * 4096 / T7 ticks;  SF = fgwDA*_SF "
          "(Imp/m distance factor).")
    print("code @0x%05X: slots fgwDA1=0x%04X fgwDA2=0x%04X%s\n"
          % (st.code_addr, st.slot_da1, st.slot_da2,
             (" fgwKTG=0x%04X" % st.slot_ktg) if st.slot_ktg else ""))
    for ds in s.datasets:
        src = st.source.get(ds.label)
        sel = st.selector.get(ds.label)
        print("[%s @0x%05X]  source cowVAR_FGG=%s (%s)   active block cowFUN_FGG=%s"
              % (ds.label, ds.base, src, vss15.FGG_SOURCES.get(src, "?"),
                 {0: "fgwDA1", 1: "fgwDA2"}.get(sel, sel)))
        for b in st.blocks:
            if b.ds is not ds:
                continue
            if b.name == "fgwKTG":
                print("  %-7s 0x%05X  SFL=%d SFH=%d (tachograph self-learn bounds)"
                      % (b.name, b.addr, b.values["SFL"], b.values["SFH"]))
                continue
            v = b.values
            print("  %-7s 0x%05X %s SF=%-6d TMX=%-6d IMP=%d (%.1f pulses/rev)  "
                  "VMI=%g VMA=%g km/h  NE=%d%s"
                  % (b.name, b.addr, "*" if b.active else " ", v["SF"], v["TMX"],
                     v["IMP"], b.imp_per_rev, v["VMI"] / 100, v["VMA"] / 100,
                     v["NE"], "" if b.valid else "  (LAYOUT INVALID - skipped)"))
    ratio = None
    if getattr(args, "ratio", None):
        ratio = args.ratio
    elif getattr(args, "actual", None) and getattr(args, "reported", None):
        ratio = vss15.speed_error_ratio(args.actual, args.reported)
        print("\nspeed correction: indicated %g -> actual %g : ratio %.5f (%+.2f %%)"
              % (args.reported, args.actual, ratio, (ratio - 1) * 100))
    elif getattr(args, "old", None) and getattr(args, "new", None):
        co, cn = vss15.tyre_circumference_mm(args.old), vss15.tyre_circumference_mm(args.new)
        ratio = cn / co
        print("\ntyre %s = %.1f mm -> %s = %.1f mm : ratio %.5f (%+.2f %%)"
              % (args.old, co, args.new, cn, ratio, (ratio - 1) * 100))
    if ratio is None:
        return 0
    plan = vss15.rescale_plan(st, ratio, getattr(args, "ktg", False))
    print("\nrescale plan (%d words):" % len(plan))
    for a, _bits, old, new in plan:
        print("  0x%05X  %6d -> %6d" % (a, old, new))
    if not getattr(args, "out", None):
        print("dry run - give --out to write the patched binary.")
        return 0
    vss15.rescale(s, ratio, getattr(args, "ktg", False))
    s.save(args.out, fix_checksum=True)
    print("written %s (checksums corrected)" % args.out)
    return 0


def cmd_dtc(args):
    """Fault paths: list, or disable/enable by name (EDC15: '<SET>:<PATH>_<K>')."""
    from .session import TunerSession
    from .dtc import DtcTable
    s = _load(args)
    t = DtcTable(s)
    changed = False
    for want, names in ((False, args.disable or []), (True, args.enable or [])):
        for name in names:
            hit = [(k, fp) for k, fp in t.paths.items()
                   if name.lower() in (k.lower(), fp.name.lower())]
            if not hit:
                print("no fault path named %r" % name)
                continue
            for key, fp in hit:
                t.set_enabled(fp, want)
                changed = True
                print("%s %s" % ("enabled" if want else "disabled", key))
    sm = t.summary()
    print("%d fault paths: %d enabled, %d disabled" % (sm["total"], sm["enabled"], sm["disabled"]))
    for fp in t.sorted_paths():
        if args.filter and args.filter.lower() not in (
                fp.dfp + " " + fp.code_desc() + " " + (fp.pcode or "")).lower():
            continue
        if args.enabled and not t.is_enabled(fp):
            continue
        print("  %s %-22s %-6s %-6s %-6s %s"
              % ("on " if t.is_enabled(fp) else "off", fp.dfp, fp.vag_str,
                 fp.pcode or "-", "MIL" if t.lights_mil(fp) else "-",
                 fp.code_desc()[:70]))
    if changed and args.out:
        s.save(args.out)
        print("written %s" % args.out)
    return 0


def cmd_msblocks(args):
    """Measuring blocks as a scan tool numbers them, with resolved channel names."""
    from .session import TunerSession
    from .msblock import MeasuringBlocks
    from .signals import SignalCatalog
    s = _load(args)
    mb = MeasuringBlocks(s)
    if not mb.available:
        print("no measuring-block tables found.")
        return 1
    cat = SignalCatalog(s.family, s)
    sm = mb.summary()
    print("%d measuring blocks (%d populated)" % (sm["groups"], sm["populated"]))
    for g in (mb.populated_groups() if not args.all else mb.groups()):
        print("  block %-8s %s" % (g.number, " | ".join(cat.label(c) for c in g.channels())))
    return 0


def cmd_outtest(args):
    """Actuator / output tests the binary supports."""
    from .session import TunerSession
    from .outtest import output_tests
    s = _load(args)
    rows = output_tests(s)
    if not rows:
        print("no output-test table found.")
        return 1
    print("%d output tests:" % len(rows))
    for r in rows:
        if s.is_edc15:
            print("  %-4s #%-2d %s  VAG %s  msg 0x%04X  %-4s TV %5.1f%%  %6.0f ms  %s"
                  % (r["group"], rows.index(r) + 1, r["code_hex"], r["vag"],
                     r["demand"], r["sig"].split("/")[-1], r["tv"], r["cycle_ms"],
                     r["meaning"]))
        else:
            print("  %s  %-24s %s" % (r["code_hex"], r["sig"], r["meaning"]))
    return 0


def cmd_vss(args):
    """VSS pulse-scaling constant (VSSCD_ImpPerDist_C) and every coding-selected
    variant copy, found structurally (value-agnostic -- works on a binary already
    tuned to a non-stock pulse count)."""
    from .session import TunerSession
    from .variant import vss_pulse_settings
    s = _load(args)
    if s.is_edc15:
        return _edc15_vss(s, args)
    rows = vss_pulse_settings(s)
    if not rows:
        print("no VSSCD_ImpPerDist_C in this family's knowledge base.")
        return 0
    print("VSS pulse scaling  (v[km/h] = 3.6 * f[Hz] / ImpPerDist;"
          " ImpPerDist only drives the speedo when source = HW sensor):\n")
    print("  %-9s %-10s %-9s %-11s %s"
          % ("role", "addr", "Imp/m", "pulses/km", "source"))
    for r in rows:
        print("  %-9s 0x%06X   %-9.4f %-11d %s"
              % ("primary" if r["primary"] else "variant", r["addr"],
                 r["imp_m"], r["imp_km"], r["src"]))
    nv = sum(1 for r in rows if not r["primary"])
    print("\n  %d config(s): 1 primary + %d coding variant(s), resolved via the"
          " active-coding state array (found by address, so an already-tuned"
          " pulse count is still located). A variant's pulse count can differ"
          " from the active primary -- write both when correcting a"
          " multi-gearbox file." % (len(rows), nv))
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--flasher":
        # ECU flasher only, no main window: `ovtsuite --flasher [file]`. Handled
        # before the subcommand parser because it is a launch mode, not a command.
        from .gui import run_flasher, HAVE_QT
        if not HAVE_QT:
            print("PySide6 is not installed; run: pip install PySide6")
            return 1
        return run_flasher(argv[1] if len(argv) > 1 else None)
    if argv and argv[0] in ("--sniffer", "--logger", "--simulator"):
        # The bus sniffer / data logger on their own, the same way:
        # `ovtsuite --sniffer`, `ovtsuite --logger [binary ...]`.
        from . import gui
        if not gui.HAVE_QT:
            print("PySide6 is not installed; run: pip install PySide6")
            return 1
        if argv[0] == "--sniffer":
            return gui.run_sniffer()
        if argv[0] == "--simulator":
            return gui.run_simulator()
        return gui.run_logger(argv[1:])
    if (len(argv) == 1 and argv[0].lower().endswith(".ovtmod")
            and os.path.isfile(argv[0])):
        # a module package opened with the app (file association)
        from . import gui
        if not gui.HAVE_QT:
            return cmd_install_module(argparse.Namespace(
                package=argv[0], system=False, allow_unsigned=False, gui=False))
        return gui.run_install_module(argv[0])
    p = argparse.ArgumentParser(prog="ovtsuite")
    # -v is shared by every subcommand rather than defined on the top-level
    # parser: a subparser that redefines the same dest overwrites the parent's
    # value with its default, so `ovtsuite -v scan x.bin` would silently lose
    # the flag. As a parent parser it works in either position.
    _common = argparse.ArgumentParser(add_help=False)
    _common.add_argument("-v", "--verbose", action="store_true",
                         help="show load progress and per-item detail")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("identify", help="identify a binary", parents=[_common])
    s.add_argument("binary")
    s.set_defaults(fn=cmd_identify)

    s = sub.add_parser("scan", help="structural scan", parents=[_common])
    s.add_argument("binary")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("audit", help="audit a family KB against its reference binary", parents=[_common])
    s.add_argument("family")
    s.add_argument("--refbin", help="override reference binary path")
    s.add_argument("--kind", help="only print findings of this kind")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("match", help="identify KB entities in a target binary", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family", required=True)
    s.add_argument("--refbin")
    s.set_defaults(fn=cmd_identify_in)

    s = sub.add_parser("diff", help="named diff of two same-layout binaries", parents=[_common])
    s.add_argument("stock")
    s.add_argument("modified")
    s.add_argument("--family", required=True)
    s.add_argument("--refbin")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("cmp-overlay",
                       help="diagnose a missing comparison table (shift/TCC)",
                       parents=[_common])
    s.add_argument("primary")
    s.add_argument("comparison")
    s.add_argument("--family", choices=("shift", "tcc", "both"), default="both")
    s.add_argument("--count", type=int, default=2,
                   help="how many maps of each family to report (default 2)")
    s.set_defaults(fn=cmd_cmp_overlay)

    s = sub.add_parser("coverage", help="scan vs reference XDF", parents=[_common])
    s.add_argument("binary")
    s.add_argument("xdf")
    s.add_argument("--misses", type=int, default=0,
                   help="print up to N missed structures")
    s.set_defaults(fn=cmd_coverage)

    s = sub.add_parser("export", help="export a TunerPro XDF for a binary", parents=[_common])
    s.add_argument("binary")
    s.add_argument("out")
    s.add_argument("--family")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("tune", help="build and optionally apply a tune profile",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("--profile", default="list",
                   help="profile name, or 'list' to see them")
    s.add_argument("--family")
    s.add_argument("--apply", action="store_true",
                   help="write the plan (default is a dry run)")
    s.add_argument("--force", action="store_true",
                   help="apply even when the validator found errors")
    s.add_argument("--out", help="write to this file instead of in place")
    s.add_argument("--limit", type=int, default=12,
                   help="how many planned writes to print")
    s.set_defaults(fn=cmd_tune)

    s = sub.add_parser("logtune", help="analyse EDC16 data logs and plan tune corrections",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("logs", nargs="*", help="data logs (KWHack or VCDS CSV layout)")
    s.add_argument("--baseline", nargs="*", help="earlier logs to compare cruise consumption against")
    s.add_argument("--presets", action="store_true",
                   help="list the logging presets (measuring blocks) for this calibration")
    s.add_argument("--lambda-target", type=float, default=1.20)
    s.add_argument("--lambda-floor", type=float, default=1.12)
    s.add_argument("--no-pilot", action="store_true", help="do not correct the boost pilot map")
    s.add_argument("--no-smoke", action="store_true", help="do not correct the smoke limiter")
    s.add_argument("--soi", action="store_true",
                   help="advance start of injection where the logs show a late end of injection")
    s.add_argument("--no-warmup", action="store_true",
                   help="report warm-up rows (coolant below 70 deg C) but do not correct from them")
    s.add_argument("--eoi-target", type=float, default=8.0, help="end-of-injection target, deg ATDC")
    s.add_argument("--soi-trial", type=float, default=0.0,
                   help="economy A/B trial: advance the cruise SOI cells by this many degrees (max 2)")
    s.add_argument("--soi-trial-max-iq", type=float, default=25.0,
                   help="upper injected quantity (mg) of the --soi-trial cruise cells (5-40, default 25)")
    s.add_argument("--family")
    s.add_argument("--smoke-step", type=float, default=None,
                   help="largest fractional raise of a smoke-limit cell in one pass (default 0.08)")
    s.add_argument("--apply", action="store_true", help="write the plan (default is a dry run)")
    s.add_argument("--out", help="write to this file instead of in place")
    s.add_argument("--label", help="version-history label for the save (default names the logs)")
    s.add_argument("--limit", type=int, default=40, help="how many planned writes to print")
    s.set_defaults(fn=cmd_logtune)

    s = sub.add_parser("tiers", help="entity count by task group", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.set_defaults(fn=cmd_tiers)

    s = sub.add_parser("irom-check",
                       help="sanity-check a C167 internal-ROM dump (offline)",
                       parents=[_common])
    s.add_argument("dump", help="the 32 KB internal-ROM dump to check")
    s.add_argument("--image", help="flash image read from the same ECU; its boot "
                                   "path states the checksum a correct ROM dump "
                                   "must come to, which makes this conclusive")
    s.set_defaults(fn=cmd_irom_check)

    s = sub.add_parser("checksum",
                       help="verify or correct EDC16 flash checksums", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--fix", action="store_true",
                   help="write corrected checksums")
    s.add_argument("--out", help="output path for --fix (default: in place)")
    s.add_argument("--key-image", help="paired external 2 MB flash, so a .MPC "
                   "internal-flash image can have its block signature verified "
                   "and re-signed (the modulus lives in the external flash)")
    s.add_argument("--program-area", action="store_true",
                   help="ZF 6HP only: also verify/correct the program/boot-area "
                        "descriptor self-CRCs (zlib CRC-32), not just calibration")
    s.set_defaults(fn=cmd_checksum)

    s = sub.add_parser("frf-info", help="identify a .frf flash-delivery container and list its blocks",
                       parents=[_common])
    s.add_argument("frf")
    s.set_defaults(fn=cmd_frf_info)

    s = sub.add_parser("frf-extract",
                       help="decode a .frf's recognized blocks to loose .bin files",
                       parents=[_common])
    s.add_argument("frf")
    s.add_argument("outdir")
    s.set_defaults(fn=cmd_frf_extract)

    s = sub.add_parser("frf-image",
                       help="assemble a .frf into the full flat flash image (.bin)",
                       parents=[_common])
    s.add_argument("frf")
    s.add_argument("out")
    s.set_defaults(fn=cmd_frf_image)

    s = sub.add_parser("container-info",
                       help="list what an .edc16/.edc15/.6hp/.8hp/.hvac/.vdo container holds",
                       parents=[_common])
    s.add_argument("path")
    s.set_defaults(fn=cmd_container_info)
    s = sub.add_parser("dim-info", help="list a .dim container's chunks", parents=[_common])
    s.add_argument("dim")
    s.set_defaults(fn=cmd_dim_info)

    s = sub.add_parser("dim-extract",
                       help="export a .dim's internal flash / external flash / EEPROM as loose files",
                       parents=[_common])
    s.add_argument("dim")
    s.add_argument("outdir")
    s.set_defaults(fn=cmd_dim_extract)

    s = sub.add_parser("dim-extract-mpc",
                       help="export a .dim's internal flash as the plain 0x81000 .mpc image (or compressed)",
                       parents=[_common])
    s.add_argument("dim")
    s.add_argument("out")
    s.add_argument("--compressed", action="store_true",
                   help="write the compressed XOR-0xD9/zlib .mpc form instead of the plain 0x81000 image")
    s.set_defaults(fn=cmd_dim_extract_mpc)

    s = sub.add_parser("dim-extract-eeprom",
                       help="export a .dim's EEPROM chunk as a loose file", parents=[_common])
    s.add_argument("dim")
    s.add_argument("out")
    s.set_defaults(fn=cmd_dim_extract_eeprom)

    s = sub.add_parser("dim-build",
                       help="repack loose files (from dim-extract) into a .dim", parents=[_common])
    s.add_argument("indir", help="directory containing dim.json + the chunk files")
    s.add_argument("dim", help="output .dim path")
    s.set_defaults(fn=cmd_dim_build)

    s = sub.add_parser("variants",
                       help="list secondary/variant (dual-coded, parents=[_common]) tables")
    s.add_argument("binary")
    s.add_argument("--family")
    s.add_argument("--all", action="store_true",
                   help="also list identical copies, not just differing ones")
    s.set_defaults(fn=cmd_variants)

    s = sub.add_parser("coding",
                       help="stored ECU coding values and the variant each selects", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.set_defaults(fn=cmd_coding)

    s = sub.add_parser("gearvn",
                       help="gear-ratio + v/n + cruise params (taller-gear tuning, parents=[_common])")
    s.add_argument("binary")
    s.add_argument("--family")
    s.set_defaults(fn=cmd_gearvn)

    s = sub.add_parser("vss",
                       help="VSS pulse-scaling constant + coding variants "
                            "(EDC15: fgwDA blocks; rescale by tyre change "
                            "--old/--new, by measured speed --actual/--reported, "
                            "or --ratio, parents=[_common])")
    s.add_argument("binary")
    s.add_argument("--family")
    s.add_argument("--old", help="EDC15: current tyre size, e.g. 195/65R15")
    s.add_argument("--new", help="EDC15: new tyre size, e.g. 205/55R16")
    s.add_argument("--reported", type=float,
                   help="EDC15: speedo-indicated speed (pairs with --actual)")
    s.add_argument("--actual", type=float,
                   help="EDC15: true speed at the same moment (GPS); scales "
                        "SF by actual/reported")
    s.add_argument("--ratio", type=float, help="EDC15: scale factor directly")
    s.add_argument("--ktg", action="store_true",
                   help="EDC15: also scale fgwKTG_SFL/SFH")
    s.add_argument("--out", help="EDC15: write the rescaled binary here")
    s.set_defaults(fn=cmd_vss)

    s = sub.add_parser("dtc", help="list / enable / disable fault paths", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.add_argument("--filter", help="only paths whose name/description contains this")
    s.add_argument("--enabled", action="store_true", help="only enabled paths")
    s.add_argument("--disable", action="append", metavar="PATH",
                   help="disable a path (repeatable; EDC15: SET:PATH_K or PATH_K for all sets)")
    s.add_argument("--enable", action="append", metavar="PATH")
    s.add_argument("--out", help="write the modified binary here")
    s.set_defaults(fn=cmd_dtc)

    s = sub.add_parser("msblocks", help="measuring blocks with channel names", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.add_argument("--all", action="store_true", help="include empty blocks")
    s.set_defaults(fn=cmd_msblocks)

    s = sub.add_parser("outtest", help="actuator / output tests", parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.set_defaults(fn=cmd_outtest)

    s = sub.add_parser("patch-diff",
                       help="diff two binaries by KB identity into a patch file",
                       parents=[_common])
    s.add_argument("primary", help="baseline binary")
    s.add_argument("comparison", help="the binary to diff against the baseline")
    s.add_argument("out", help="patch file to write (.patch, plain JSON)")
    s.add_argument("--family", help="applies to BOTH binaries; omit to auto-detect each")
    s.add_argument("--name", help="only parameters whose name contains this (case-insensitive)")
    s.add_argument("--tier", type=int, help="only parameters of this task group "
                   "(group id; `tiers` lists them)")
    s.add_argument("--gaps", action="store_true",
                   help="also capture undefined-data byte ranges that differ "
                        "(same family/sub-family only -- address-literal)")
    s.add_argument("--min-gap", type=int, default=1,
                   help="skip a captured gap shorter than this many bytes (default 1)")
    s.set_defaults(fn=cmd_patch_diff)

    s = sub.add_parser("patch-inspect", help="list the entries in a patch file",
                       parents=[_common])
    s.add_argument("patch")
    s.set_defaults(fn=cmd_patch_inspect)

    s = sub.add_parser("patch-apply",
                       help="apply a patch file to a target binary",
                       parents=[_common])
    s.add_argument("patch")
    s.add_argument("binary", help="target binary")
    s.add_argument("--family")
    s.add_argument("--trust-all", action="store_true",
                   help="also apply entries/blocks whose target placement is "
                        "not established (bracketed/projected) or whose "
                        "family differs from the block's own -- confirms "
                        "every one of them at once; omit for a per-entry "
                        "dispositions file, which only the GUI writes today")
    s.add_argument("--apply", action="store_true",
                   help="write the result (default is a dry run)")
    s.add_argument("--out", help="write to this file instead of in place")
    s.add_argument("--no-checksum-fix", action="store_true",
                   help="skip recomputing the flash checksum right after applying")
    s.set_defaults(fn=cmd_patch_apply)

    s = sub.add_parser("patch-revert",
                       help="list or undo a previously applied patch, "
                            "using ONLY the change log beside the binary",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.add_argument("--id", help="the patch record id to revert (omit to list them)")
    s.add_argument("--apply", action="store_true",
                   help="write the reverted result (default is a dry run)")
    s.add_argument("--out", help="write to this file instead of in place")
    s.set_defaults(fn=cmd_patch_revert)

    s = sub.add_parser("patch-diff-stock",
                       help="diff a binary against its own family reference "
                            "binary into a patch file (what changed vs. stock)",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("out", help="patch file to write (.patch, plain JSON)")
    s.add_argument("--family")
    s.add_argument("--name", help="only parameters whose name contains this")
    s.set_defaults(fn=cmd_patch_diff_stock)

    s = sub.add_parser("patch-batch",
                       help="apply a patch file to every matching binary in a directory",
                       parents=[_common])
    s.add_argument("patch")
    s.add_argument("dir", help="directory of target binaries")
    s.add_argument("--pattern", default="*.bin",
                   help="glob pattern within DIR (default *.bin)")
    s.add_argument("--family", help="pin every target to this family instead of auto-detecting")
    s.add_argument("--trust-all", action="store_true",
                   help="apply every unestablished/cross-family entry too, on every file")
    s.add_argument("--apply", action="store_true",
                   help="write each result (default is a dry run over every file)")
    s.add_argument("--out-dir", help="write results here by basename instead of in place")
    s.add_argument("--no-checksum-fix", action="store_true")
    s.set_defaults(fn=cmd_patch_batch)

    s = sub.add_parser("patch-diff-history",
                       help="build a patch from two points in a binary's "
                            "own automatic version history, instead of a "
                            "separate comparison file",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("older", help="version id from history-list -- the OLDER endpoint")
    s.add_argument("newer", nargs="?", default=None,
                   help="version id for the NEWER endpoint; omit to diff "
                        "against the current (possibly unsaved) state")
    s.add_argument("out", help="patch file to write (.patch, plain JSON)")
    s.add_argument("--family")
    s.add_argument("--name", help="only parameters whose name contains this")
    s.add_argument("--gaps", action="store_true",
                   help="also capture undefined-data byte ranges that differ")
    s.set_defaults(fn=cmd_patch_diff_history)

    s = sub.add_parser("history-list",
                       help="list this binary's automatic version checkpoints "
                            "(from its change log alone)",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("--family")
    s.set_defaults(fn=cmd_history_list)

    s = sub.add_parser("history-revert",
                       help="revert to an earlier version checkpoint",
                       parents=[_common])
    s.add_argument("binary")
    s.add_argument("id", help="version id from history-list")
    s.add_argument("--family")
    s.add_argument("--apply", action="store_true",
                   help="write the reverted result (default is a dry run)")
    s.add_argument("--out", help="write to this file instead of in place")
    s.set_defaults(fn=cmd_history_revert)

    s = sub.add_parser("mcp", help="run as an MCP server on stdio, for AI clients",
                       parents=[_common])
    s.add_argument("--print-config", action="store_true",
                   help="print the server entry for an MCP client's config file")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("modules", help="list the modules and their state",
                       parents=[_common])
    s.set_defaults(fn=cmd_modules)

    s = sub.add_parser("install-module", help="install a .ovtmod module package",
                       parents=[_common])
    s.add_argument("package")
    s.add_argument("--system", action="store_true",
                   help="install for all users (the app's own modules folder)")
    s.add_argument("--allow-unsigned", action="store_true",
                   help="accept an unsigned development build")
    s.add_argument("--gui", action="store_true",
                   help="confirm and report in a window (what opening a .ovtmod does)")
    s.set_defaults(fn=cmd_install_module)

    s = sub.add_parser("remove-module", help="remove an installed module",
                       parents=[_common])
    s.add_argument("module")
    s.add_argument("--system", action="store_true")
    s.set_defaults(fn=cmd_remove_module)

    s = sub.add_parser("gui", help="launch the graphical editor", parents=[_common])
    s.add_argument("binary", nargs="?",
                   help="binary to open (optional; can open from the GUI)")
    s.add_argument("--compare", help="comparison binary (or project)")
    s.add_argument("--new-instance", action="store_true",
                   help="open a window of its own even when one is running "
                        "(by default the file goes to the running window)")
    s.set_defaults(fn=cmd_gui)

    if (len(argv) == 1 and argv[0] not in sub.choices
            and os.path.isfile(argv[0])):
        # `ovtsuite <file>`: a file dropped on the exe, or an association
        # without the gui verb, opens it in the GUI instead of failing as an
        # unknown subcommand (which, in the windowed build, shows nothing).
        return cmd_gui(argparse.Namespace(binary=argv[0], compare=None)) or 0
    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except ModuleNotFoundError as ex:
        # A command that needs a platform module the user has not installed.
        from .platforms import MODULES
        parts = (ex.name or "").split(".")
        if len(parts) >= 2 and parts[0] == "ovtsuite" and parts[1] in MODULES:
            print("%s: this needs the %s module, which is not installed."
                  % (args.cmd, MODULES[parts[1]].name), file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
