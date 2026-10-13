# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the app learned about a file: identification strings plus parse
statistics, as (label, value) rows for the post-load info dialog.

EDC16: every 256 KB flash block starts with a Bosch block header whose
ASCII field at +0x10 is the 10-digit Bosch software number followed by the
8-character dataset/project id (e.g. ``1037400916P475AAXW``); the KWP2000
identification record is a fixed-width record in block 7:
``FIELD1(12) 00*8 PART(12) 00 VER(4) 00 NAME(20)``.  The SECOND 12-byte field
is the definitive VAG part number in EVERY family; the FIRST field varies -- a
VAG hardware number on 997-software parts (e.g. 038906016L before 038997016R),
the Bosch number on 906-hardware parts (e.g. 0281012554 before 038906016AB), or
the same part again on the 03G/U34 datasets -- so the record is anchored on the
SECOND field, not the first (anchoring on the first is why p350 showed no part).
EDC15: each dataset carries its own ident string (part number, system name,
SG/AG, Bosch number, software version) at a fixed dataset offset.
"""
import re
from collections import Counter

# VAG part number, 12 bytes incl trailing spaces (e.g. "038906016AB ", "03G906016LQ ").
_PN = rb"0[0-9][0-9A-Z]9[0-9]{2}[0-9]{3}[A-Z ]{3}"
_PN_ONLY = re.compile(_PN + rb"$")
# First field = any 12-char printable run (Bosch number, VAG hardware number, or
# PN), then a fixed 8-byte gap (usually nulls, but some datasets carry a couple of
# config/checksum bytes there -- e.g. 038997016T), then the definitive part number.
_IDENT16 = re.compile(rb"([0-9A-Z][ -~]{11})[\x00-\xff]{8}(" + _PN
                      + rb")\x00([0-9A-Z]{4})\x00([ -~]{20})")
_HDR16 = re.compile(rb"([0-9]{10})([0-9A-Z]{8})")


def ident16(data: bytes, region=None) -> dict:
    """Identification strings of an EDC16 image.

    Keys (present when found): ``part`` (the ECU/VAG part number -- the second
    record field, reliable across p350/p475/U34), ``hw_part`` (first field when
    it is itself a VAG part number), ``bosch_num`` (first field when it is the
    Bosch number), ``sw_version``, ``system``, ``ident_addr``, plus ``bosch_sw``
    / ``dataset_id`` from the block header."""
    out = {}
    cal = max(0, len(data) - 0x80000)          # the last 512 KB: what a read holds
    start = region[0] if region else cal
    for base in (start & ~0x3FFFF, cal, cal + 0x40000, 0x40000, 0x0):
        m = _HDR16.match(data, base + 0x10) if base + 0x20 <= len(data) else None
        if m:
            out["bosch_sw"] = m.group(1).decode()
            out["dataset_id"] = m.group(2).decode()
            pm = re.match(r"P\d{3}", out["dataset_id"])
            if pm:
                out["project"] = pm.group(0)           # P350 / P447 / P475 ...
            break
    sysm = re.search(rb"EDC16U\d+", data)              # ECU generation from descriptor
    if sysm:
        out["ecu_system"] = sysm.group().decode()
    m = _IDENT16.search(data, start & ~0x3FFFF)
    if m:
        field1 = m.group(1).decode().strip()
        part = m.group(2).decode().strip()
        out["part"] = part                                  # canonical VAG part number
        if _PN_ONLY.match(m.group(1)):
            hw = field1
            if hw != part:                                  # hardware number distinct from part
                out["hw_part"] = hw
        else:
            out["bosch_num"] = field1                       # first field is the Bosch number
        out["sw_version"] = m.group(3).decode()
        out["system"] = m.group(4).decode().strip()
        out["ident_addr"] = m.start()
    else:
        # Fallback: no structured record (odd/edited layout) -- surface the most
        # common lone VAG part number in the calibration region so a part still shows.
        hits = Counter(x.group().decode().strip()
                       for x in re.finditer(_PN, data[start & ~0x3FFFF:]))
        if hits:
            out["part"] = hits.most_common(1)[0][0]
    return out


def rows_for_session(session, eeprom=None):
    """(label, value) rows describing a loaded session."""
    data = bytes(session.binfile.data)
    rows = [("File", session.binfile.name), ("Size", "%d bytes (%d KB)" % (len(data), len(data) // 1024))]
    cont = getattr(session, "_container", None)
    if cont is not None:
        held = ", ".join(b.label() for b in cont.blocks)
        rows.append(("Container", "%s  (%s)" % (cont.fmt.title, held)))
        st = getattr(session, "defn_state", "none")
        if getattr(session, "platform", "") != "vdo":   # no snapshot needed
            rows.append(("Definition", "stored - opened without identification"
                         if st == "used" else
                         "not used (%s) - re-save to store it for faster "
                         "opening" % st))
    if getattr(session, "support", "full") != "full":
        # The platform's module is not installed (or has no definition for
        # this binary): say what the base app recognised, and why there is no
        # parameter tree. Nothing below may run -- it reads module state.
        ident = getattr(session, "binary_identity", None)
        if ident is not None:
            rows.append(("Identified as", ident.describe()))
        rows.append(("Support", "checksum only" if session.support == "checksum"
                     else "hex editor and universal tools"))
        if getattr(session, "unsupported_reason", None):
            rows.append(("Why", session.unsupported_reason))
        if session.support == "checksum":
            try:
                cs = session.checksum_status()
            except Exception:
                cs = None
            if cs is not None:
                rows.append(("Checksum", cs.describe()))
        return rows
    if getattr(session, "platform", "") == "vdo":
        # VDO instrument cluster EEPROM: identity, ROM build, key facts.
        from .vdo.session_vdo import summary as _vdo_summary
        rows.extend(_vdo_summary(session))
        return rows
    if getattr(session, "is_edc15", False):
        rows.append(("Platform", "EDC15 (C167)"))
        rows.append(("Family / KB", str(session.family)))
        dss = getattr(session, "datasets", []) or []
        rows.append(("Datasets", str(len(dss))))
        for d in dss:
            ident = (getattr(d, "ident", "") or "").strip()
            dsv = getattr(d, "dsv", None)
            rows.append(("  dataset %s" % getattr(d, "label", getattr(d, "index", "?")),
                         "0x%05X  coding %s  %s" % (d.start, dsv if dsv is not None else "?", ident)))
        try:
            from .edc15 import structure15 as S
            mt = S.find_message_table(data, list(dss))
            if mt:
                rows.append(("Message table", "0x%05X, %d messages" % (mt.addr, mt.count)))
        except Exception:
            pass
    elif getattr(session, "is_zf6hp", False):
        # This branch did not exist, and the else below is the EDC16 one -- so
        # a ZF 6HP image fell into it and was described by EDC16 rules. At
        # exactly 1 MB that printed "EDC16U1 (1 MB)" for a 6HP19A gearbox.
        # The device, when the image names it, rather than the generic family.
        # It is the one field that separates a TC19.11/MPC562 6HP28 from a
        # TC19.x/MPC555 image, which size and schedule count do not: both are
        # ~2 MB with 98-100 schedules.
        ctl = getattr(session, "controller", None)
        rows.append(("Platform",
                     "ZF 6HP (%s, big-endian)"
                     % (str(ctl) if ctl else "MPC5xx")))
        model = getattr(session, "model", None)
        if model is not None and model.gearbox:
            rows.append(("Transmission", model.gearbox))
            if model.part:
                rows.append(("Part number", model.part))
            if model.variant:
                rows.append(("Calibration variant", model.variant))
            if model.designation:
                rows.append(("System designation", model.designation))
        else:
            rows.append(("Transmission", "not recognised"))
        # Say plainly whether the definition IS this image's, because every
        # placement in the session means something different depending on it.
        own = getattr(session, "family_is_own", False)
        rows.append(("Knowledge base",
                     "%s  (%s)" % (session.family,
                                   "this image's own definition" if own
                                   else "transferred — not this image's own")))
        # A definition passed over for naming a different device is worth
        # saying out loud: otherwise the only visible effect is a transfer
        # where the user expected an own definition, with no reason given.
        refused = getattr(session, "controller_refused", None) or []
        if refused:
            # Grouped by reason: ten families refusing an MPC562 image all
            # refuse it for the identical reason, and listing that ten times
            # buries the one fact worth reading.
            by_why = {}
            for fam, why in refused:
                by_why.setdefault(why, []).append(fam)
            rows.append(("Definitions declined",
                         "; ".join("%d (%s)" % (len(fams), why)
                                   for why, fams in by_why.items())))
        num = getattr(session, "zf_numbering", None)
        if num is not None:
            rows.append(("Shift schedules", num.describe()))
        elif getattr(session, "shift_schedules", None):
            rows.append(("Shift schedules",
                         "%d, named by the definition"
                         % (len(session.shift_schedules) // 2)))
        region = getattr(session, "region", None)
        if region:
            rows.append(("Calibration region", "0x%X - 0x%X" % tuple(region)))
        # The program's own parameter directory, where the image carries its
        # program (pointerdir6hp): what it indexed, and what it did to the
        # transferred placements. Said plainly because it changes what the
        # tree shows -- guesses dropped, unnamed parameters added.
        pdir = getattr(session, "pointer_directory", None)
        if pdir is not None:
            v = pdir.verdict
            if v.entries:
                rows.append(("Parameter directory",
                             "%s: %d parameters pointed to by the program, "
                             "%.0f%% of %d anchored structures begin at one"
                             % ("adopted" if v.admitted else "ignored",
                                v.entries, 100 * v.agree, v.sample)))
            else:
                rows.append(("Parameter directory", v.reason))
        audit = getattr(session, "directory_audit", None)
        if audit is not None:
            rows.append(("Directory check", audit.describe()))
        tr = getattr(session, "directory_transfer", None)
        if tr is not None and tr.predicted:
            rows.append(("Names by module table", tr.describe()))
            n_un = sum(1 for e in getattr(session, "entities", ())
                       if (e.name or "").startswith("zf_unnamed_"))
            if n_un:
                rows.append(("Unnamed parameters",
                             "%d reached by the program but named by no "
                             "definition (under Unnamed)" % n_un))
    elif getattr(session, "is_zf8hp", False):
        # No branch existed: an 8HP fell into the EDC16 else below and a
        # 2 MB image was described as "EDC16U31/U34 (2 MB)".
        from .zf8hp.session8hp import summary as _zf8hp_summary
        rows.extend(_zf8hp_summary(session))
    elif getattr(session, "platform", "") == "hvac":
        him = getattr(session, "_hvac", None)
        rows.append(("Platform", "Climatronic HVAC (Fujitsu F2MC-16LX)"))
        if him is not None:
            rows.append(("Part / version",
                         ("%s %s" % (him.part or "unknown", him.version or "")).strip()))
            rows.append(("Flash base", "0x%06X" % him.base))
            rows.append(("Flash sections", "%d%s" % (
                len(him.section_map),
                " + loader" if him.loader is not None else "")))
            fw = getattr(him, "fwmap", None)
            if fw is None or fw.seat is None:
                rows.append(("Variable map", "not located for this part/version"))
            elif fw.auto:
                rows.append(("Variable map",
                             "AUTO-DETECTED by structural analysis "
                             "(verify offsets before flashing)"))
            else:
                rows.append(("Variable map", "verified registry entry"))
    elif getattr(session, "platform", "edc16") not in ("", "edc16"):
        # A platform with definitions but no branch of its own here: name it
        # rather than describe it by the EDC16 rules below.
        from . import platforms as _platforms
        rows.append(("Platform", _platforms.platform(session.platform).name))
        if getattr(session, "family", None):
            rows.append(("Family", str(session.family)))
    else:
        size = len(data)
        region = getattr(session, "region", None)
        info = ident16(data, region)
        gen = info.get("ecu_system")                        # EDC16U31 / EDC16U34
        if size == 0x200000:
            variant = "%s (2 MB)" % gen if gen else "EDC16U31/U34 (2 MB)"
        elif size == 0x100000:
            variant = "EDC16U1 (1 MB)"
        else:
            variant = "unknown layout"
        rows.append(("Platform", variant))
        # session.family is the calibration-FINGERPRINT match; the dataset project
        # is the authoritative generation.  Flag a disagreement (e.g. a U34/P447
        # dataset whose maps fingerprint closest to p350) rather than trusting it.
        proj = info.get("project")
        fam = str(session.family)
        _fam_proj = {"p350": "P350", "p475_83": "P475", "p475_84_mf": "P475",
                     "p475_84_hq": "P475", "p447": "P447"}
        if proj and _fam_proj.get(fam) and _fam_proj[fam] != proj:
            rows.append(("Family / KB", "%s  (fingerprint match; dataset is %s -- "
                         "parameters may be misplaced, no KB for this project)" % (fam, proj)))
        else:
            rows.append(("Family / KB", fam))
        if proj:
            rows.append(("Dataset project", proj))
        if region:
            rows.append(("Calibration region", "0x%X - 0x%X" % tuple(region)))
        if "part" in info:
            rows.append(("ECU part number", info["part"]))
            if "hw_part" in info:
                rows.append(("Hardware part number", info["hw_part"]))
            if "bosch_num" in info:
                rows.append(("Bosch number", info["bosch_num"]))
            if "sw_version" in info:
                rows.append(("Software version", info["sw_version"]))
            if "system" in info:
                rows.append(("System", info["system"]))
        if "bosch_sw" in info:
            rows.append(("Bosch software number", info["bosch_sw"]))
            rows.append(("Dataset id", info["dataset_id"]))
        try:
            from . import layout as _layout
            res = getattr(session, "result", None)
            lay = getattr(res, "layout", None)
            pt = _layout.pointer_table(data, region[0] if region else None)
            if pt is not None:
                rows.append(("Pointer table", "%d calibration blocks, %d redirected by coding%s"
                             % (pt.count, pt.redirected,
                                "" if pt.from_default else " (calibration read: active copy)")))
            known, ident = _layout.identify(data, session.family)
            if known is not None:
                rows.append(("Known layout", "%s to %s (Bosch SW %s, %s)" % (
                    "identical" if ident else "aligned", known.file, known.sw, known.dataset)))
            if lay is not None:
                rows.append(("Layout vs reference", "identical pointer table" if lay.identical else
                             "%d of %d blocks aligned; %d exact blocks cover %d of %d bytes"
                             % (lay.paired, lay.ref_count, len(lay.blocks), lay.covered, lay.span)))
                if res.conflicts:
                    rows.append(("  fingerprint conflicts", "%d anchor(s) overruled by the pointer table: %s"
                                 % (len(res.conflicts), ", ".join(a.name for a in res.conflicts[:5]))))
            elif pt is not None:
                rows.append(("Layout vs reference", "pointer table does not align (different software)"))
        except Exception:
            pass
        try:
            from . import variant as _variant
            vts = _variant.find_variant_tables(session)
            if vts:
                secs = _variant.find_secondary_regions(data, region[0]) if region else []
                ndiff = sum(1 for v in vts if v.differs)
                rows.append(("Variant coding blocks", "%d secondary block(s): %d variant parameters, %d differ"
                             % (len(secs), len(vts), ndiff)))
                doms = _variant.coding_domains(vts)
                if doms:
                    rows.append(("  selected by", ", ".join("%s (%d)" % (k, n) for k, n in doms.most_common())))
            else:
                rows.append(("Variant coding blocks", "none"))
        except Exception:
            pass
    ents = session.entities
    kinds = Counter(getattr(e, "kind", "?") for e in ents)
    rows.append(("Parameters", "%d (%s)" % (
        len(ents), ", ".join("%d %s" % (n, k) for k, n in kinds.most_common()))))
    prov = Counter(getattr(e, "source", "?") for e in ents)
    order = ["anchored", "struct", "verified", "placed", "bracketed", "segmented",
             "projected", "carried"]
    rows.append(("Placement", ", ".join(
        "%d %s" % (prov[k], k) for k in order + sorted(set(prov) - set(order)) if prov.get(k))))
    if prov.get("projected"):
        rows.append(("  projected", "%d parameter(s) placed by the offset model only - "
                     "check before writing" % prov["projected"]))
    try:
        rep = session.checksum_status()
        if rep is None:
            rows.append(("Checksums", "no supported layout"))
        else:
            rows.append(("Checksums",
                         "cannot verify" if getattr(rep, "unreadable", "")
                         else "valid" if rep.ok
                         else "MISMATCH (corrected on save)"))
    except Exception:
        pass
    if eeprom is not None:
        try:
            from .edc15 import eeprom15 as E
            rows.append(("EEPROM", "512 bytes loaded alongside"))
            rows.append(("  VIN", E.get(eeprom, E.FIELD_BY_KEY["vin"])))
            rows.append(("  Immobiliser", "%s (%s)" % (E.get(eeprom, E.FIELD_BY_KEY["immo_status"]),
                                                       E.get(eeprom, E.FIELD_BY_KEY["immo_number"]))))
            rows.append(("  Odometer", "%.2f km" % E.get(eeprom, E.FIELD_BY_KEY["odometer"])))
        except Exception:
            pass
    return rows
