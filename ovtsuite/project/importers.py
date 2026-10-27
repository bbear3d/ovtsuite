# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bring legacy files into an ``.ovt`` project (spec section 8).

Every importer is structural -- a container's own magic and fields decide what
it is, never the extension or the size -- and keeps the source file
byte-for-byte as an *original* whenever it is a delivery container
(``.sgo``, ``.dim``, ``.frf``), so it can be exported again losslessly.

Legacy change logs (the ``history`` block of a native container, or a
``.log`` sidecar) are migrated into a checkpoint chain (spec 4.3).
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from .model import Member, new_id
from .project import Project

Images = Dict[str, bytes]

#: definition file extensions packaged with a raw member
DEFINITION_EXTS = (".xdf", ".a2l", ".kp", ".ols", ".dam")

#: legacy TunerSession region names -> member ids
_REGION_MEMBER = {"MPC (internal flash)": "mpc", "EEPROM": "eeprom"}


class ImportRefused(ValueError):
    """The file is understood but can't be imported structurally here."""


# ------------------------------------------------------------ identification
def _identify(data: bytes, kind: Optional[str] = None, hvac: bool = False):
    from ..platforms.identify import identify
    return identify(data, container_kind=kind, hvac=hvac)


def _module_ref(platform: str) -> Dict[str, str]:
    from ..platforms import module_for
    mid = module_for(platform)
    if not mid:
        return {}
    ver = ""
    try:
        from ..modules import registry
        spec = registry.spec(mid)
        ver = getattr(spec, "version", "") or ""
    except Exception:
        pass
    return {"id": mid, "version": ver}


def _endian(platform: str) -> str:
    # The same rule BinaryFile uses: EDC15 (C167) is little-endian, the
    # MPC5xx/ZF platforms big-endian.
    return "little" if platform == "edc15" else "big"


def _set_name(platform: str, filename: str) -> str:
    """A new set's name: the file it came from, without its extension
    ("03G906016LQ", "5.0TFSI-Stock") -- a project with two engine files
    tells them apart by name, not "ECU" / "ECU 2". Without a file name, what
    the set is (ECU, TCU, Cluster, HVAC)."""
    from ..platforms import platform as _p
    from .model import PLATFORM_SET_KIND
    stem = os.path.splitext(os.path.basename(filename or ""))[0].strip()
    if stem:
        return stem
    kind = PLATFORM_SET_KIND.get(platform)
    if kind in ("ecu", "tcu"):
        return kind.upper()
    if kind == "cluster":
        return "Cluster"
    if kind == "hvac":
        return "HVAC"
    return _p(platform).short


# ------------------------------------------------------------ legacy history
def _version_records(text: str) -> List[dict]:
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if line.startswith("VERSION "):
            try:
                out.append(json.loads(line[len("VERSION "):]))
            except ValueError:
                continue
    return out


def _undo_ranges(buf: bytearray, ranges) -> bool:
    for start, old_hex, new_hex in ranges:
        old, new = bytes.fromhex(old_hex), bytes.fromhex(new_hex)
        if bytes(buf[start:start + len(new)]) != new:
            return False
        buf[start:start + len(new)] = old
    return True


def legacy_chain(current: Images, log_text: str) -> List[Tuple[dict, Images]]:
    """``[(record, images_after_that_save)]`` oldest first, plus the state
    before the first save as ``({}, images)`` at index 0 -- recovered by
    walking the legacy ``VERSION`` records newest -> oldest from ``current``.

    Stops (keeping what it recovered) at the first record that does not fit
    the bytes, so a log that drifted from its file never invents a state."""
    recs = _version_records(log_text)
    states: List[Tuple[dict, Images]] = []
    cur = {k: bytearray(v) for k, v in current.items()}
    # A record's regions name the old TunerSession region labels.
    for rec in reversed(recs):
        states.append((rec, {k: bytes(v) for k, v in cur.items()}))
        ok = "flash" in cur and _undo_ranges(cur["flash"], rec.get("ranges") or [])
        for name, ranges in (rec.get("regions") or {}).items():
            mid = _REGION_MEMBER.get(name)
            if not ok or mid not in cur:
                ok = False
                break
            ok = _undo_ranges(cur[mid], ranges)
        if not ok:
            # this record does not fit: the oldest state we can trust is the
            # one after it, so drop it and stop.
            after = states.pop()[1]
            cur = {k: bytearray(v) for k, v in after.items()}
            break
    states.append(({}, {k: bytes(v) for k, v in cur.items()}))
    states.reverse()
    return states


def _add_history(project: Project, sid: str, images: Images, log_text: str,
                 *, root_kind: str, root_label: str, source: dict,
                 branch: str) -> Tuple[str, str]:
    """Root + migrated checkpoints; returns (head checkpoint id, branch id).

    Each migrated save keeps the rest of its log entry (edit summaries,
    ``PATCH`` records -- what Remove patch reads) in its checkpoint; text that
    belongs to no migrated save is kept as an attachment."""
    from .sessionbridge import _blocks_of
    chain = legacy_chain(images, log_text) if log_text else [({}, images)]
    migrated = {r.get("id") for r, _s in chain[1:]}
    by_id, rest = {}, []
    for block in _blocks_of(log_text or ""):
        ids = []
        for line in block:
            if line.startswith("VERSION "):
                try:
                    ids.append(json.loads(line[8:]).get("id"))
                except ValueError:
                    pass
        lines = [l for l in block if not l.startswith("VERSION ")]
        hit = next((i for i in ids if i in migrated), None)
        if hit is not None:
            by_id.setdefault(hit, []).extend(lines)
        else:
            rest.extend(block)
    c, b = project.add_root(sid, chain[0][1], kind=root_kind, label=root_label,
                            source=source, branch=branch)
    head = c.id
    for rec, state in chain[1:]:
        lines = by_id.get(rec.get("id"), [])
        kind = "patch" if any(l.startswith("PATCH ") for l in lines) else "save"
        got = project.commit(sid, b.id, state, kind=kind,
                             label=rec.get("name") or rec.get("label") or "save",
                             source={"legacy_id": rec.get("id", ""),
                                     "legacy_ts": rec.get("ts", "")},
                             log=lines)
        if got is not None:
            got.ts = rec.get("ts") or got.ts
            head = got.id
    # the file's own bytes are the authority for the head
    final = project.commit(sid, b.id, images, kind="import",
                           label="current content")
    if final is not None:
        head = final.id
    if any(l.strip() for l in rest):
        project.add_attachment("legacy-history.log",
                               ("\n".join(rest) + "\n").encode("utf-8"),
                               set_id=sid, note="change log migrated from %s"
                               % source.get("file", "the imported file"))
    return head, b.id


# ------------------------------------------------------------- the importers
def _new_or_existing_set(project, into_set, platform, filename, identity):
    if into_set is not None:
        return project.set(into_set)
    return project.add_set(_set_name(platform, filename), platform=platform,
                           identity=identity)


def _ensure_members(project, s, specs: List[Member]):
    for m in specs:
        if s.member(m.id) is None:
            project.add_member(s.id, m)


def _member(mid, role, platform, base=0, meta=None, name=""):
    return Member(id=mid, role=role, name=name, platform=platform,
                  module=_module_ref(platform), base=base,
                  endian=_endian(platform), meta=dict(meta or {}))


def _import_native(project, data, filename, into_set, branch, sidecar_log):
    from .. import container as _c
    cont = _c.Container.from_bytes(data)
    ext = cont.external() or b""
    ident = _identify(ext, cont.kind, hvac=cont.kind == "hvac")
    platform = ident.platform
    s = _new_or_existing_set(project, into_set, platform, filename, ident.facts)
    role_of = {_c.BLOCK_EXTERNAL: "flash", _c.BLOCK_BOOT: "boot",
               _c.BLOCK_PROGRAM: "program", _c.BLOCK_CAL: "cal",
               _c.BLOCK_MPC: "mpc", _c.BLOCK_EEPROM: "eeprom",
               _c.BLOCK_LOADER: "loader"}
    images: Images = {}
    specs = []
    meta = cont.meta()
    for blk in cont.blocks:
        role = role_of.get(blk.type)
        if role is None:
            continue
        specs.append(_member(role, role, platform, base=blk.address,
                             meta=meta if role == "flash" else None))
        images[role] = bytes(blk.data)
    _ensure_members(project, s, specs)
    log = cont.get(_c.BLOCK_LOG)
    text = bytes(log.data).decode("utf-8", "replace") if log else (sidecar_log or "")
    head, bid = _add_history(project, s.id, images, text, root_kind="import",
                             root_label="Imported %s" % filename,
                             source={"file": filename, "format": cont.kind},
                             branch=branch)
    defn = cont.get(_c.BLOCK_DEFN)
    if defn is not None:
        project.set_cache(s.id, bid, bytes(defn.data))
    return s


def _import_dim(project, data, filename, into_set, branch):
    from .. import dim as _dim
    chunks = _dim.load(data)
    images: Images = {"flash": _dim.external_flash(chunks)}
    try:
        mpc = _dim.internal_flash(chunks)
        if mpc:
            images["mpc"] = mpc
    except Exception:                     # e.g. a .dim without chunk 1
        pass
    try:
        ee = _dim.eeprom(chunks)
        if ee:
            images["eeprom"] = ee
    except Exception:
        pass
    ident = _identify(images["flash"])
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    _ensure_members(project, s, [
        _member(k, k, ident.platform) for k in images])
    c, b = project.add_root(s.id, images, kind="import",
                            label="Imported %s" % filename,
                            source={"file": filename, "format": "dim"},
                            branch=branch)
    project.add_original(s.id, filename, "dim", data, checkpoint=c.id,
                         members=list(images))
    return s


def _import_sgo(project, data, filename, into_set, branch):
    from .. import sgo as _sgo
    cont = _sgo.parse(data, filename)
    try:
        from ..flash import hvac_image as _hi
        is_hvac = _hi.is_hvac_container(cont)
    except Exception:
        is_hvac = False
    if is_hvac:
        return _import_hvac_sgo(project, data, cont, filename, into_set, branch)
    base, flat = _sgo.image(cont)
    ident = _identify(flat)
    if not ident.known and getattr(cont, "needs_module", ""):
        from ..platforms.identify import Identity
        ident = Identity(cont.needs_module, "container")
    if not flat:
        raise ImportRefused(
            "%s: no section of this .sgo decodes here%s" % (
                filename, " -- install the %s module" % cont.needs_module
                if getattr(cont, "needs_module", "") else ""))
    images: Images = {"flash": bytes(flat)}
    sec = next((x for x in cont.sections
                if x.addr == 0x404000 and x.plaintext is not None), None)
    if sec is not None:                   # EDC16U31 internal flash (see session)
        buf = bytearray(b"\xFF" * 0x81000)
        buf[0x4000:0x4000 + len(sec.plaintext)] = sec.plaintext
        images["mpc"] = bytes(buf)
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    _ensure_members(project, s, [_member("flash", "flash", ident.platform,
                                         base=base)] +
                    ([_member("mpc", "mpc", ident.platform, base=0x400000)]
                     if "mpc" in images else []))
    c, b = project.add_root(s.id, images, kind="import",
                            label="Imported %s" % filename,
                            source={"file": filename, "format": "sgo"},
                            branch=branch)
    project.add_original(s.id, filename, "sgo", data, checkpoint=c.id,
                         members=list(images))
    if getattr(cont, "unrecognized", None):
        project.report.warnings.append(
            "%s: %d of %d .sgo sections did not decode and are kept only in "
            "the original" % (filename, len(cont.unrecognized), len(cont.sections)))
    return s


def _import_hvac_sgo(project, data, cont, filename, into_set, branch):
    """A Climatronic .sgo: its flash sections sit at 0xFA0000/0xFC0000+, past
    the 0-based image every other .sgo flattens into, so it is assembled by
    :mod:`ovtsuite.flash.hvac_image` -- the flat image based at the
    firmware's own base, the embedded flash loader as a member of its own,
    and the part, section map and stock section digests as the flash
    member's meta (what a native .hvac carried; the selective write needs
    them)."""
    from ..flash import hvac_image as _hi
    part, ver, _fn = _hi.sgo_ident(data)
    img = _hi.HvacImage.from_sgo_container(cont, name=filename, part=part,
                                           version=ver)
    ident = _identify(bytes(img.image), hvac=True)
    meta = {"part": img.part or "", "version": img.version or "",
            "base": img.base, "loader_addr": img.loader_addr,
            "sections": [[a, n] for a, n in img.section_map]}
    if img.baseline_digests:
        meta["baseline"] = [bytes(d).hex() for d in img.baseline_digests]
    images: Images = {"flash": bytes(img.image)}
    specs = [_member("flash", "flash", ident.platform, base=img.base, meta=meta)]
    if img.loader is not None:
        images["loader"] = bytes(img.loader)
        specs.append(_member("loader", "loader", ident.platform,
                             base=img.loader_addr))
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    _ensure_members(project, s, specs)
    c, b = project.add_root(s.id, images, kind="import",
                            label="Imported %s" % filename,
                            source={"file": filename, "format": "sgo"},
                            branch=branch)
    project.add_original(s.id, filename, "sgo", data, checkpoint=c.id,
                         members=list(images))
    return s


def _import_frf(project, data, filename, into_set, branch):
    from .. import frf as _frf
    cont = _frf.parse(data, filename)
    try:
        flat = _frf.image(cont).data
    except Exception:
        cal = [b for b in cont.blocks if getattr(b, "is_calibration", False)]
        if not cal or cal[0].plaintext is None:
            raise ImportRefused("%s: no calibration block decodes" % filename)
        flat = cal[0].plaintext
    ident = _identify(flat)
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    _ensure_members(project, s, [_member("flash", "flash", ident.platform)])
    c, b = project.add_root(s.id, {"flash": bytes(flat)}, kind="import",
                            label="Imported %s" % filename,
                            source={"file": filename, "format": "frf"},
                            branch=branch)
    project.add_original(s.id, filename, "frf", data, checkpoint=c.id,
                         members=["flash"])
    return s


def _import_backup15(project, data, filename, into_set, branch):
    """An old KWHack ``.edc15bak`` (flash + EEPROM): imported only -- the
    project keeps both as members; nothing writes the format any more."""
    from ..flash import backup15 as _bk
    flash, eeprom, _variant = _bk.parse(data)
    ident = _identify(bytes(flash))
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    role = "flash" if ident.known else "raw"
    _ensure_members(project, s, [
        _member("flash", role, ident.platform, name="" if ident.known else filename),
        _member("eeprom", "eeprom", ident.platform)])
    project.add_root(s.id, {"flash": bytes(flash), "eeprom": bytes(eeprom)},
                     kind="import", label="Imported %s" % filename,
                     source={"file": filename, "format": "edc15bak"},
                     branch=branch)
    return s


def _import_raw(project, data, filename, into_set, branch, sidecar_log,
                definition):
    ident = _identify(data)
    if not ident.known:
        # A 16-bit bench/BDM read of a ZF 5HP flash is stored with the two
        # bytes of every word exchanged, so it identifies as "unknown" as-is.
        # Identify the NATIVE image so the member records the right platform
        # (the bytes are kept as they came -- the session deswaps them on open,
        # TunerSession._byte_swapped, and saves back swapped). Without this the
        # member was platform "unknown": its module never engaged, so an
        # auto-attached sidecar definition was applied instead of the module's
        # structural maps and -- placed in the wrong byte order -- showed none.
        from ..platforms import zf5hp as _z5p
        if _z5p.byte_swapped(data):
            ident = _identify(_z5p.swap16(data))
    s = _new_or_existing_set(project, into_set, ident.platform, filename,
                             ident.facts)
    role = "flash" if ident.known else "raw"
    mid = "flash"
    if s.member(mid) is None:
        project.add_member(s.id, _member(mid, role, ident.platform,
                                         name="" if ident.known else
                                         os.path.basename(filename)))
    _add_history(project, s.id, {mid: bytes(data)}, sidecar_log or "",
                 root_kind="import", root_label="Imported %s" % filename,
                 source={"file": filename, "format": "bin"}, branch=branch)
    if definition is not None:
        dname, ddata = definition
        project.add_definition(s.id, mid, dname, ddata,
                               definition_type(ddata) or None)
    return s


def ols_versions(data: bytes) -> List[Tuple[str, str, int]]:
    """The binaries a WinOLS project ``data`` embeds, the original first:
    ``[(name, comment, size)]``; [] for anything that is not a project with
    a binary. What :func:`import_bytes` takes as ``ols_versions`` indexes."""
    from ..definitions import winols as _wo
    try:
        if not _wo.is_winols(data):
            return []
        wf = _wo.scan_images(data) or _wo.parse(data)
    except Exception:                                   # noqa: BLE001
        return []
    if wf.kind != "ols":
        return []
    return [(v.name or ("Original" if i == 0 else "Version %d" % i),
             v.comment, len(v.data)) for i, v in enumerate(wf.versions)]


def _ols_picks(wf, which) -> List[int]:
    """Indexes into ``wf.versions`` for ``which``: None / ``"original"`` the
    original only, ``"all"`` every version, or the indexes to import."""
    n = len(wf.versions)
    if which is None or which == "original":
        return [0]
    if which == "all":
        return list(range(n))
    if isinstance(which, int):
        which = [which]
    picks = sorted({int(i) for i in which})
    if not picks or picks[0] < 0 or picks[-1] >= n:
        raise ImportRefused("the WinOLS project holds %d version%s; "
                            "%s is not one of them"
                            % (n, "" if n == 1 else "s", list(which)))
    return picks


def _import_ols(project, data, filename, into_set, branch, which=None):
    """A WinOLS project: its embedded binary becomes the set's root (named
    after the WinOLS version it holds), the project itself is packaged as
    the set's definition (its maps open the binary in the main view when no
    module knows it), and the vehicle and control-unit details entered in
    WinOLS fill the project information that is still empty.

    A project can embed several versions of the binary (the original and
    what was made from it). ``which`` says which go in: the original only
    (default), ``"all"``, or the indexes of the ones to import. The first
    of them is the set's root, each later one a version (a checkpoint) on
    its branch, in the order WinOLS holds them."""
    from ..definitions import winols
    wf = winols.parse(data)
    if wf.kind != "ols":
        raise ImportRefused(
            "%s is a WinOLS map pack: it describes a binary but holds none. "
            "Open the binary and attach it as its definition." % filename)
    if not wf.image:
        raise ImportRefused("%s: the WinOLS project holds no binary" % filename)
    picks = _ols_picks(wf, which)
    two = None
    if len(wf.segments) == 2:
        # an image WinOLS put together from two memories (flash + the
        # processor's internal flash, e.g. an EDC16 with its MPC)
        (a0, e0), (a1, e1) = wf.segments
        first = _identify(bytes(wf.versions[picks[0]].data)[a0:e0 + 1])
        if first.known and first.platform == "edc16":
            two = ((a0, e0), (a1, e1))

    def split(image: bytes) -> Images:
        if two is None:
            return {"flash": image}
        (a0, e0), (a1, e1) = two
        return {"flash": image[a0:e0 + 1], "mpc": image[a1:e1 + 1]}

    image = bytes(wf.versions[picks[0]].data)
    images = split(image)
    ident = _identify(images["flash"] if two else image)
    platform = ident.platform
    s = _new_or_existing_set(project, into_set, platform, filename, ident.facts)
    specs = [_member("flash", "flash" if ident.known else "raw", platform,
                     name="" if ident.known else os.path.basename(filename))]
    if "mpc" in images:
        specs.append(_member("mpc", "mpc", platform, base=0x400000))
    _ensure_members(project, s, specs)

    def vname(i: int) -> str:
        v = wf.versions[i]
        return v.name or (wf.header.current_version if i == 0 else
                          "Version %d" % i)

    def src(i: int) -> dict:
        d = {"file": filename, "format": "ols",
             "winols_version": wf.header.version}
        if len(wf.versions) > 1:
            d["version"] = vname(i)
        return d

    first = picks[0]
    label = ("WinOLS: %s" % vname(first) if vname(first)
             else "Imported %s" % filename)
    _c, b = project.add_root(s.id, images, kind="import", label=label,
                             source=src(first), branch=branch)
    for i in picks[1:]:
        project.commit(s.id, b.id, split(bytes(wf.versions[i].data)),
                       kind="import", label="WinOLS: %s" % vname(i),
                       source=src(i))
    project.add_definition(s.id, "flash", filename, data, "ols")
    info = winols.project_info(wf.header)
    fresh = {k: v for k, v in info.items() if not project.info.get(k)}
    if fresh:
        project.set_info(fresh)
    for p in wf.problems:
        project.report.warnings.append("%s: %s" % (filename, p))
    return s


def definition_type(data: bytes) -> Optional[str]:
    """The definition type of ``data`` (xdf / a2l / kp / ols / dam) from its
    structure, or None."""
    from ..definitions import detect
    try:
        return detect(data)
    except Exception:
        return None


# --------------------------------------------------------------- public API
def import_bytes(project: Project, data: bytes, filename: str, *,
                 into_set: Optional[str] = None, branch: str = "",
                 sidecar_log: Optional[str] = None,
                 definition: Optional[Tuple[str, bytes]] = None,
                 ols_versions=None):
    """Import one file's bytes into ``project``; returns the set it went to.

    ``into_set`` adds the file to an existing set as a new branch with its own
    root (e.g. a stock ``.sgo`` next to a read of a tuned car); otherwise a
    new set is created. ``definition`` is ``(filename, bytes)`` of an XDF /
    A2L / KP / OLS to package with a raw member. ``ols_versions``: which
    binaries of a WinOLS project go in -- the original only (default),
    ``"all"`` (the original as the root, the rest as versions of it), or a
    list of indexes (see :func:`ols_versions`).
    """
    from .. import container as _c
    from .. import dim as _dim
    from .. import frf as _frf
    from .. import sgo as _sgo
    from . import archive as _ar
    data = bytes(data)
    branch = branch or ("Stock" if into_set is None else
                        os.path.splitext(os.path.basename(filename))[0])
    if _ar.is_project(data):
        raise ImportRefused("%s is already an OVT project -- open it instead"
                            % filename)
    if data[:8] == b"OVTHVAC\x01":
        # the first .hvac layout: rebuilt as the native container it became
        # (image, loader, sections and stock digests carried over unchanged)
        from ..flash import hvac_image as _hi
        try:
            img = _hi.HvacImage.from_own(data, name=filename)
            data = img.to_native(fix_checksum=False)
        except Exception as e:
            raise ImportRefused("%s is an old-format .hvac that could not be "
                                "read (%s)" % (filename, e))
    if _c.format_for_magic(data[:8]) is not None:
        return _import_native(project, data, filename, into_set, branch,
                              sidecar_log)
    from ..flash import backup15 as _bk
    if _bk.is_backup(data[:8]):
        return _import_backup15(project, data, filename, into_set, branch)
    if data[:4] in _dim.MAGICS:
        return _import_dim(project, data, filename, into_set, branch)
    if _sgo.detect(data[:0x30]):
        return _import_sgo(project, data, filename, into_set, branch)
    if _frf.detect(data[:4]):
        return _import_frf(project, data, filename, into_set, branch)
    from ..definitions import winols as _wo
    if _wo.is_winols(data):
        return _import_ols(project, data, filename, into_set, branch,
                           ols_versions)
    dtype = definition_type(data[:65536])
    if dtype in ("xdf", "a2l", "dam"):
        raise ImportRefused(
            "%s is a definition (%s): it describes a binary but holds none. "
            "Open the binary and attach it as its definition."
            % (filename, dtype.upper()))
    return _import_raw(project, data, filename, into_set, branch, sidecar_log,
                       definition)


def import_read(project: Project, data: bytes, name: str, *,
                eeprom: Optional[bytes] = None, source: Optional[dict] = None,
                into_set: Optional[str] = None):
    """A read from a module, as a set whose root is the read itself (kind
    ``read`` -- the as-read original). ``data`` is the flash image, or a
    native container a reader produced (a VDO cluster read); ``eeprom`` an
    EEPROM read alongside the flash (an EDC15 full backup). ``into_set``
    adds it to an existing set as a new branch with its own root. Returns
    the set."""
    from .. import container as _c
    import time as _t
    label = "Read %s" % _t.strftime("%Y-%m-%d %H:%M")
    src = dict(source or {})
    if _c.format_for_magic(bytes(data[:8])) is not None and eeprom is None:
        s = import_bytes(project, data, name, into_set=into_set,
                         branch="" if into_set is None else label)
        b = s.branches[-1]
        root = s.root_of(b.head)
        root.kind, root.label, root.source = "read", label, src
        return s
    ident = _identify(bytes(data))
    s = _new_or_existing_set(project, into_set, ident.platform, name, ident.facts)
    role = "flash" if ident.known else "raw"
    specs = [_member("flash", role, ident.platform,
                     name="" if ident.known else name)]
    images = {"flash": bytes(data)}
    if eeprom is not None:
        specs.append(_member("eeprom", "eeprom", ident.platform))
        images["eeprom"] = bytes(eeprom)
    _ensure_members(project, s, specs)
    project.add_root(s.id, images, kind="read", label=label, source=src,
                     branch="Stock" if into_set is None else label)
    return s


def import_file(path: str, project: Optional[Project] = None, *,
                into_set: Optional[str] = None, branch: str = "",
                definition_path: Optional[str] = None, ols_versions=None):
    """Import the file at ``path``; returns ``(project, set)``.

    A ``.log`` beside the file is migrated as its history. A definition with
    the same base name (``x.bin`` + ``x.xdf``) is packaged automatically for
    a raw binary, or pass ``definition_path``.
    """
    if project is None:
        project = Project(os.path.splitext(os.path.basename(path))[0])
    with open(path, "rb") as fh:
        data = fh.read()
    side = os.path.splitext(path)[0] + ".log"
    log = None
    if os.path.exists(side):
        with open(side, encoding="utf-8", errors="replace", newline="") as fh:
            log = fh.read()
    definition = None
    dpath = definition_path
    if dpath is None:
        for ext in DEFINITION_EXTS:
            for cand in (os.path.splitext(path)[0] + ext,
                         os.path.splitext(path)[0] + ext.upper()):
                if os.path.exists(cand):
                    dpath = cand
                    break
            if dpath:
                break
    if dpath:
        with open(dpath, "rb") as fh:
            definition = (os.path.basename(dpath), fh.read())
        if definition_type(definition[1]) is None:
            definition = None           # not a definition after all
    s = import_bytes(project, data, os.path.basename(path), into_set=into_set,
                     branch=branch, sidecar_log=log, definition=definition,
                     ols_versions=ols_versions)
    return project, s


__all__ = ["import_bytes", "import_file", "ols_versions", "import_read", "legacy_chain", "ImportRefused",
           "DEFINITION_EXTS", "definition_type", "new_id"]
