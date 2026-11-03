# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Export members of an ``.ovt`` project (spec section 8).

* :func:`export_raw` -- a member's bytes at any branch/checkpoint (``.bin``);
  an EDC16 calibration-only file (512 KB) comes out as the full 2 MB image.
* :func:`export_original` -- back into the delivery container it was imported
  from. Unchanged content returns the original bytes exactly; changed content
  is rebuilt for ``.dim`` (untouched chunks carried through) and ``.sgo``
  (only changed sections re-encoded); a PCMFlash one-file EDC16 read is laid
  end to end again. ``.frf`` has no writer.

The older native containers (``.edc16`` / ``.edc15`` / ``.6hp`` / ...) are
import-only: nothing exports them (the app still writes one internally, as a
tab's working file -- project.sessionbridge).
"""

from __future__ import annotations

from typing import List, Optional

from .project import Project


class ExportRefused(ValueError):
    """This export is not possible for this member/original."""


def _cid(project: Project, sid: str, branch: Optional[str],
         checkpoint: Optional[str]) -> str:
    return checkpoint or project.head(sid, branch).id


def export_raw(project: Project, sid: str, mid: str, *,
               branch: Optional[str] = None,
               checkpoint: Optional[str] = None) -> bytes:
    data = project.member_bytes(sid, mid, branch=branch,
                                checkpoint=_cid(project, sid, branch, checkpoint))
    if data is None:
        raise ExportRefused("member %r does not exist at that point" % mid)
    # a project that still holds an EDC16 calibration-only file (512 KB, from
    # before imports made it the full image) exports the full 2 MB image
    if project.set(sid).platform == "edc16" and mid == "flash":
        from ..platforms import edc16 as _e16
        if _e16.is_cal_only(data):
            return _e16.pad_cal_only(data)
    return data


def _want(project: Project, sid: str, cid: str):
    """The set's images at ``cid`` with its linked companions' current heads
    in the EEPROM / MPC parts (a flash follows its companions)."""
    from . import companions as _co
    want = dict(project.state(sid, cid))
    want.update(_co.injected(project, sid))
    return want


def export_pcmflash(project: Project, sid: str, *, branch: Optional[str] = None,
                    checkpoint: Optional[str] = None,
                    with_eeprom: bool = False) -> bytes:
    """The set as one PCMFlash ``_flash_`` file: the external flash then the
    internal flash cut to the length PCMFlash's own micro files have (0x80200),
    the EEPROM behind them when asked for and the set has one. Works for any
    EDC16 set that holds both flash parts, whatever it was imported from."""
    from .. import pcmflash_edc16 as _pcm
    cid = _cid(project, sid, branch, checkpoint)
    want = _want(project, sid, cid)
    flash, mpc = want.get("flash"), want.get("mpc")
    if flash is None or mpc is None:
        raise ExportRefused("a PCMFlash file needs both the external flash and "
                            "the internal flash (MPC); this set lacks %s"
                            % ("both" if flash is None and mpc is None else
                               "the external flash" if flash is None
                               else "the internal flash"))
    flash = export_raw(project, sid, "flash", branch=branch, checkpoint=cid)
    if len(flash) != _pcm.EXT_SIZE:
        raise ExportRefused("the external flash is 0x%X bytes; PCMFlash's is "
                            "0x%X" % (len(flash), _pcm.EXT_SIZE))
    ee = want.get("eeprom") if with_eeprom else None
    if with_eeprom and (ee is None or len(ee) != _pcm.EEPROM_SIZE):
        raise ExportRefused("the set has no 4 KB EEPROM to put in the file")
    order = ("ext", "mpc") + (("eeprom",) if ee is not None else ())
    return _pcm.join(_pcm.Layout(order, 0x80200), flash, mpc, ee)


def original_formats(project: Project, sid: str) -> List[str]:
    """The original container formats this set can be exported to."""
    return [o.format for o in project.set(sid).originals
            if o.format in ("dim", "sgo", "pcmflash")]


def export_original(project: Project, sid: str, oid: str, *,
                    branch: Optional[str] = None,
                    checkpoint: Optional[str] = None) -> bytes:
    s = project.set(sid)
    o = s.original(oid)
    if o is None:
        raise KeyError(oid)
    raw = project.original_bytes(sid, oid)
    cid = _cid(project, sid, branch, checkpoint)
    want = _want(project, sid, cid)
    if o.checkpoint:
        then = project.state(sid, o.checkpoint)
        if all(then.get(m) == want.get(m) for m in o.members):
            return raw                                   # unchanged: exact bytes
    if o.format == "dim":
        from .. import dim as _dim
        chunks = _dim.load(raw)
        if "flash" in o.members:
            chunks = _dim.replace_external_flash(chunks, want["flash"])
        if "mpc" in o.members and want.get("mpc") is not None:
            chunks = _dim.replace_internal_flash(chunks, want["mpc"])
        if "eeprom" in o.members and want.get("eeprom") is not None:
            chunks = _dim.replace_chunk(chunks, 3, want["eeprom"])
        return _dim.build(chunks)
    if o.format == "pcmflash":
        # the combined file again: the saved parts in the order and lengths
        # the file came with (ovtsuite.pcmflash_edc16)
        from .. import pcmflash_edc16 as _pcm
        lay = _pcm.detect(raw)
        if lay is None or want.get("flash") is None or want.get("mpc") is None:
            raise ExportRefused("the original PCMFlash file can no longer be "
                                "put together -- export the parts as raw "
                                "binaries instead")
        return _pcm.join(lay, want["flash"], want["mpc"], want.get("eeprom"))
    if o.format == "sgo" and s.platform == "hvac":
        # Climatronic: re-packed through hvac_image -- the flat image is based
        # at the firmware's base, and the flash loader section (declared at
        # the same address as section 1) is carried through verbatim. The
        # saved version's checksum was corrected when it was saved.
        from ..flash import hvac_image as _hi
        img = _hi.HvacImage.from_sgo(raw, name=o.filename)
        new = want["flash"]
        if len(new) != len(img.image):
            raise ExportRefused("the Climatronic image is %d bytes, the .sgo "
                                "holds %d -- export the raw binary instead"
                                % (len(new), len(img.image)))
        img.image[:] = new
        return img.to_sgo(fix_checksum=False)
    if o.format == "sgo":
        from .. import sgo as _sgo
        cont = _sgo.parse(raw, o.filename)
        if "mpc" in o.members and want.get("mpc") is not None:
            sec = next((x for x in cont.sections
                        if x.addr == 0x404000 and x.plaintext is not None), None)
            if sec is not None:
                body = want["mpc"][0x4000:0x4000 + len(sec.plaintext)]
                cont = _sgo.replace_section(cont, sec.addr, body)
        m = s.member("flash")
        cont = _sgo.apply_image(cont, m.base if m else 0, want["flash"])
        return _sgo.to_bytes(cont)
    raise ExportRefused("there is no writer for .%s -- export the raw binary "
                        "instead" % o.format)
