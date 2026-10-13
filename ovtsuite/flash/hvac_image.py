# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r"""Read / write / flash Audi Climatronic (4F/4L 09100 43x, Fujitsu F2MC-16LX)
HVAC firmware images -- the container side, which the base app owns because
the flasher needs it for every Climatronic whether or not the HVAC module is
installed.

All container handling goes through :mod:`ovtsuite.sgo` -- OVT-Suite's OWN,
clean-room SGO codec (the one already shipped for the auto-trans / EDC16
families). NO sgopack (GPL) code is imported, ported or copied here.

What this module adds on top of :mod:`ovtsuite.sgo`:

  1. HVAC-shaped image assembly. ``sgo.image()`` flattens to a 0-based image
     capped at ``MAIN_IMAGE_MAX`` (0x400000) -- right for the EDC16 families,
     but the HVAC's sections live at 0xFA0000/0xFC0000+, so they'd be dropped.
     Here the flash sections are assembled into a flat image based at the
     firmware's own base, and exposed 0-based (image offset = flash - base),
     matching how the .bin dumps and the disassembly are addressed.

  2. The embedded flash LOADER. The HVAC .sgo carries the F2MC flash driver as
     a small section declared at the SAME address as the full section that
     follows it (verified from ODISHVACflash.log for 4F0910043B: the loader is
     flashed first, then section 1 erases over it). That overlap is why the
     loader is kept OUT of the editable flat image but emitted FIRST, verbatim,
     in the flashable block list.

  3. OVT's own ``.hvac`` formats (the legacy ``OVTHVAC`` layout and the native
     container), which keep the stock per-section digests for a selective
     flash.

The firmware-keyed variable map and the internal flash checksum are the 4F/4L
HVAC module's (:mod:`ovtsuite.hvac.firmware`). ``fix_checksum=True`` asks that
module to correct the checksum and fails with :class:`HvacImageError` when it
is not installed.
"""
import hashlib
import re
import struct
from typing import List, Tuple, Union

from .. import sgo as _sgo

# Default base for the classic C6 (4F) 256 KB layout; per-firmware base is
# derived from the container's own sections.
FLASH_BASE = 0xFC0000

OWN_MAGIC = b"OVTHVAC\x01"
OWN_VERSION = 2   # v2 adds per-section baseline digests



class HvacImageError(Exception):
    pass


def _load_bytes(data_or_path: Union[bytes, bytearray, str]) -> bytes:
    if isinstance(data_or_path, (bytes, bytearray)):
        return bytes(data_or_path)
    with open(data_or_path, "rb") as f:
        return f.read()


# ---------------------------------------------------- structural identity ----
_IDENT_RE = re.compile(r"^(?P<part>.+?)_+(?P<ver>[0-9A-Za-z]{3,4})$")


def sgo_ident(data: bytes) -> Tuple[str, str, str]:
    """(part_number, version, filename) from the SGO's own header fields
    (filename @0x31 XOR 0xFF -- a format-defined field, not a guess). Returns
    ("","",fn) if it can't be split into part+version."""
    raw = bytes(b ^ 0xFF for b in data[0x31:0x31 + 40])
    fn = raw.split(b"\x00")[0].split(b"\xff")[0].decode("latin1", "replace").strip()
    stem = re.sub(r"\.sg[mo]$", "", fn, flags=re.I)
    m = _IDENT_RE.match(stem)
    if m:
        return m.group("part"), m.group("ver"), fn
    return "", "", fn


def is_hvac_container(container) -> bool:
    """A Climatronic SGO: every decoded section is plain (crypt=0 / XOR-0xFF)
    and the flash sits in the high F2MC window (>=0xF00000, well above the
    EDC16/6HP address space and the 0x800000 mirror). The AES cluster
    (crypt=1) and the EDC16/6HP families do not match."""
    secs = [s for s in container.sections if s.plaintext is not None]
    if not secs:
        return False
    if any(s.scheme != "plain" for s in secs):
        return False
    return max(s.addr for s in secs) >= 0xF00000


# --------------------------------------------------------- section helpers ---
def _loader_sections(sections) -> list:
    """A loader section is one whose flash span is fully contained inside a
    larger section's span (the F2MC driver sits at the base address, inside the
    full 64 KB section also at that address). Structural, not a length guess."""
    out = []
    for s in sections:
        if s.plaintext is None:
            continue
        for o in sections:
            if o is s or o.plaintext is None:
                continue
            if o.addr <= s.addr and s.end <= o.end and o.declared_len > s.declared_len:
                out.append(s)
                break
    return out



def _section_digests(image, section_map, base) -> list:
    """Per-section sha256 of ``image`` (sliced by section_map). Used to record a
    baseline ("stock") fingerprint so a later flash can skip sections that were
    never edited -- a deterministic diff that needs no on-module checksum."""
    out = []
    for addr, ln in section_map:
        off = addr - base
        out.append(hashlib.sha256(bytes(image[off:off + ln])).digest())
    return out


class HvacImage:
    """An HVAC firmware image plus its loader and container. ``image`` is a
    flat bytearray based at ``base`` (image offset = flash - base) holding ONLY
    the flash sections (not the loader). ``origin`` says where it came from
    ("sgo", "own", "native" or "image"); ``fwmap`` is the HVAC module's
    firmware map once :func:`ovtsuite.hvac.firmware.attach` has run (None
    without the module)."""

    def __init__(self, image, loader=None, container=None, section_map=None,
                 loader_addr=None, base=FLASH_BASE, name="", part="", version="",
                 fwmap=None, origin="image"):
        self.base = base
        self.image = bytearray(image)
        self.loader = None if loader is None else bytes(loader)
        self.loader_addr = base if loader_addr is None else loader_addr
        self.container = container
        self.section_map = list(section_map) if section_map else []
        self.name = name
        self.part = part
        self.version = version
        self.fwmap = fwmap
        self.origin = origin
        # Baseline ("stock") per-section digests for the deterministic skip-diff.
        # `baseline_digests` is the fingerprint of the ORIGINAL sections; edits
        # mutate `self.image` but never this. `baseline_trusted` is True ONLY when
        # the baseline was LOADED from our own .hvac file (which stores the stock
        # fingerprint separately from the edited image). For a .sgo the two are
        # the same bytes, so it is NOT a trustworthy skip reference -> flash all.
        self.baseline_digests = None
        self.baseline_trusted = False

    # ---- constructors -------------------------------------------------------
    @classmethod
    def load(cls, data_or_path, name="") -> "HvacImage":
        data = _load_bytes(data_or_path)
        nm = name or (data_or_path if isinstance(data_or_path, str) else "")
        if data[:8] == OWN_MAGIC:
            return cls.from_own(data, name=nm)
        from .. import container as _c
        if _c.is_container(data, "hvac"):
            return cls.from_container(_c.Container.from_bytes(data), name=nm)
        if _sgo.detect(data):
            return cls.from_sgo(data, name=nm)
        return cls.from_image(data, name=nm)

    @classmethod
    def from_sgo(cls, data_or_path, name="") -> "HvacImage":
        data = _load_bytes(data_or_path)
        nm = name or (data_or_path if isinstance(data_or_path, str) else "")
        part, ver, _fn = sgo_ident(data)
        c = _sgo.parse(data, name=nm)
        return cls.from_sgo_container(c, name=nm, part=part, version=ver)

    @classmethod
    def from_container(cls, c, name="", part="", version="") -> "HvacImage":
        """From a parsed Climatronic .sgo (:class:`ovtsuite.sgo.SgoContainer`)
        or a native ``hvac`` container (:class:`ovtsuite.container.Container`).
        (These were two methods of the same name, so the second hid the first
        and every .sgo raised TypeError.)"""
        if hasattr(c, "sections"):
            return cls.from_sgo_container(c, name=name, part=part, version=version)
        return cls.from_native(c, name=name)

    @classmethod
    def from_sgo_container(cls, c, name="", part="", version="") -> "HvacImage":
        bad = [s.index for s in c.sections if s.scheme != "plain" and s.plaintext is None]
        if any(s.scheme not in ("plain", None) for s in c.sections):
            raise HvacImageError(
                "SGO has non-plain sections -- not an HVAC (Climatronic) file "
                "(the AES cluster and EDC16/6HP families are handled elsewhere)")
        loaders = _loader_sections(c.sections)
        flash = [s for s in c.sections if s not in loaders and s.plaintext is not None]
        if not flash:
            raise HvacImageError("no decodable flash sections in SGO")
        base = min(s.addr for s in flash)
        hi = max(s.end for s in flash)
        img = bytearray(b"\xFF" * (hi - base))
        for s in flash:
            img[s.addr - base:s.end - base] = s.plaintext
        loader = loaders[0].plaintext if loaders else None
        laddr = loaders[0].addr if loaders else base
        smap = [(s.addr, s.declared_len) for s in flash]
        obj = cls(img, loader, c, smap, laddr, base, name=name,
                  part=part, version=version, origin="sgo")
        obj.baseline_digests = _section_digests(img, smap, base)   # stock fingerprint
        return obj

    @classmethod
    def from_image(cls, data_or_path, loader=None, base=FLASH_BASE,
                   section_map=None, name="", part="") -> "HvacImage":
        data = _load_bytes(data_or_path)
        nm = name or (data_or_path if isinstance(data_or_path, str) else "")
        if section_map is None:
            section_map = [(base + o, l) for o, l in
                           ((0x00000, 0x10000), (0x10000, 0x10000),
                            (0x20000, 0x10000), (0x30000, 0x08000),
                            (0x38000, 0x02000))]
        ldr = _load_bytes(loader) if isinstance(loader, str) else loader
        return cls(data, ldr, None, section_map, base, base, name=nm,
                   part=part, origin="image")

    @classmethod
    def from_own(cls, data_or_path, name="") -> "HvacImage":
        data = _load_bytes(data_or_path)
        if data[:8] != OWN_MAGIC:
            raise HvacImageError("not an OVT HVAC image (bad magic)")
        ver, flags = struct.unpack_from("<HH", data, 8)
        if ver not in (1, 2):
            raise HvacImageError("unsupported OVT HVAC image version %d" % ver)
        base = struct.unpack_from("<I", data, 12)[0]
        p = 16
        laddr, llen = struct.unpack_from("<II", data, p); p += 8
        loader = data[p:p + llen] if (flags & 1) else None
        p += llen
        plen = struct.unpack_from("<H", data, p)[0]; p += 2
        part = data[p:p + plen].decode("latin1", "replace"); p += plen
        nsec = struct.unpack_from("<H", data, p)[0]; p += 2
        smap = []
        for _ in range(nsec):
            a, l = struct.unpack_from("<II", data, p); p += 8
            smap.append((a, l))
        digests = None
        if flags & 2:                          # v2: per-section stock baseline digests
            digests = []
            for _ in range(nsec):
                digests.append(data[p:p + 32]); p += 32
        ilen = struct.unpack_from("<I", data, p)[0]; p += 4
        img = data[p:p + ilen]
        obj = cls(img, loader, None, smap, laddr, base,
                  name=name or (data_or_path if isinstance(data_or_path, str) else ""),
                  part=part, origin="own")
        if digests is not None:
            obj.baseline_digests = digests
            obj.baseline_trusted = True         # a .hvac's baseline IS the stock reference
        return obj

    # ---- checksum (the HVAC module's) ----------------------------------------
    def _fix_checksum(self) -> None:
        """Correct the internal flash checksum through the 4F/4L HVAC module
        (a no-op for a firmware whose checksum it has not mapped)."""
        from ..modules import registry as _registry
        cs = _registry.checksum("hvac", image=self)
        if cs is None:
            raise HvacImageError(
                "Correcting the Climatronic checksum needs the 4F/4L HVAC module "
                "(%s)" % _registry.missing_text("hvac", "checksum correction"))
        self.image[:] = cs.correct(bytes(self.image))

    # ---- outputs ------------------------------------------------------------
    def to_blocks(self, fix_checksum: bool = True):
        """Ordered flashable ``[(addr, data)]``: loader first (verbatim), then
        each flash section from the current image -- the captured ODIS order."""
        if fix_checksum:
            self._fix_checksum()
        blocks = []
        if self.loader is not None:
            blocks.append((self.loader_addr, bytes(self.loader)))
        for addr, ln in self.section_map:
            off = addr - self.base
            blocks.append((addr, bytes(self.image[off:off + ln])))
        return blocks

    def to_sgo(self, fix_checksum: bool = True) -> bytes:
        if self.container is None:
            raise HvacImageError(
                "no source SGO container -- save as our own format (.hvac)")
        if fix_checksum:
            self._fix_checksum()
        data = bytes(self.image)
        new = _sgo.apply_image(self.container, self.base, data)
        lidx = {s.index for s in _loader_sections(self.container.sections)}
        orig = {s.index: s for s in self.container.sections}
        new.sections = [orig[s.index] if s.index in lidx else s for s in new.sections]
        return _sgo.to_bytes(new)

    def to_own(self, fix_checksum: bool = True) -> bytes:
        if fix_checksum:
            self._fix_checksum()
        # Persist the STOCK baseline digests so a later flash of this .hvac can
        # skip sections that were never edited (deterministic diff, no on-module
        # checksum). Baseline persists from the original load; edits don't touch
        # it. If we somehow have none, omit them (-> a reload flashes all).
        digests = self.baseline_digests
        if digests is not None and len(digests) != len(self.section_map):
            digests = None
        flags = (1 if self.loader is not None else 0) | (2 if digests is not None else 0)
        out = bytearray(OWN_MAGIC)
        out += struct.pack("<HH", OWN_VERSION, flags)
        out += struct.pack("<I", self.base)
        ldr = self.loader or b""
        out += struct.pack("<II", self.loader_addr, len(ldr)) + ldr
        pb = (self.part or "").encode("latin1", "replace")
        out += struct.pack("<H", len(pb)) + pb
        out += struct.pack("<H", len(self.section_map))
        for addr, ln in self.section_map:
            out += struct.pack("<II", addr, ln)
        if digests is not None:
            for d in digests:
                out += bytes(d[:32]).ljust(32, b"\x00")
        out += struct.pack("<I", len(self.image)) + bytes(self.image)
        return bytes(out)

    # ---- native container (.hvac v3) ----------------------------------------
    def fill_container(self, c, fix_checksum: bool = True):
        """Write this image into an ``ovtsuite.container.Container`` of kind
        ``hvac``: the flash image, the loader (when there is one) and a META
        block with the part, base, section map and stock baseline digests --
        everything the legacy ``to_own`` layout carried, now compressed and
        CRC-checked, with room for EEPROM/MPC/history/definition blocks."""
        from .. import container as _c
        if fix_checksum:
            self._fix_checksum()
        digests = self.baseline_digests
        if digests is not None and len(digests) != len(self.section_map):
            digests = None
        c.add(_c.BLOCK_EXTERNAL, bytes(self.image), name="image")
        if self.loader is not None:
            c.add(_c.BLOCK_LOADER, bytes(self.loader), address=self.loader_addr)
        else:
            c.remove(_c.BLOCK_LOADER)
        meta = c.meta()
        meta.update({
            "part": self.part or "", "version": self.version or "",
            "base": self.base, "loader_addr": self.loader_addr,
            "sections": [[a, l] for a, l in self.section_map],
        })
        if digests is not None:
            meta["baseline"] = [bytes(d[:32]).ljust(32, b"\x00").hex()
                                for d in digests]
        else:
            meta.pop("baseline", None)
        c.set_meta(meta)
        return c

    def to_native(self, fix_checksum: bool = True) -> bytes:
        from .. import container as _c
        return self.fill_container(_c.Container("hvac"), fix_checksum).to_bytes()

    @classmethod
    def from_native(cls, c, name="") -> "HvacImage":
        """Rebuild from a native ``hvac`` container (see ``fill_container``)."""
        from .. import container as _c
        img = c.external()
        if img is None:
            raise HvacImageError(".hvac has no image block")
        meta = c.meta()
        ld = c.get(_c.BLOCK_LOADER)
        base = int(meta.get("base", FLASH_BASE))
        smap = [(int(a), int(l)) for a, l in meta.get("sections", [])]
        part = meta.get("part", "")
        obj = cls(img, ld.data if ld else None, None, smap,
                  int(meta.get("loader_addr", ld.address if ld else base)),
                  base, name=name, part=part, version=meta.get("version", ""),
                  origin="native")
        bl = meta.get("baseline")
        if bl:
            obj.baseline_digests = [bytes.fromhex(h) for h in bl]
            obj.baseline_trusted = True     # a .hvac's baseline IS the stock reference
        return obj

    def changed_section_addrs(self):
        """Set of flash addresses whose CURRENT section bytes differ from the
        stock baseline -- the sections a selective flash must write. Returns
        None when there is no trustworthy baseline (a .sgo, or a from_image),
        meaning 'flash all'. Trustworthy only for a .hvac, whose stored baseline
        is the stock the module is assumed to hold. Compare AFTER any checksum
        recompute so section 1 (which carries 0xFCFFF4) shows as changed."""
        if not self.baseline_trusted or not self.baseline_digests:
            return None
        if len(self.baseline_digests) != len(self.section_map):
            return None
        cur = _section_digests(self.image, self.section_map, self.base)
        return {addr for (addr, _ln), cd, bd
                in zip(self.section_map, cur, self.baseline_digests) if cd != bd}

    def save(self, path: str, fix_checksum: bool = True) -> None:
        low = path.lower()
        if low.endswith(".sgo"):
            data = self.to_sgo(fix_checksum)
        elif low.endswith(".hvac"):
            data = self.to_native(fix_checksum)
        else:
            if fix_checksum:
                self._fix_checksum()
            data = bytes(self.image)
        with open(path, "wb") as f:
            f.write(data)

    def summary(self) -> str:
        return ("HVAC %s %s: base 0x%06X, %d flash sections%s"
                % (self.part or "?", self.version or "", self.base,
                   len(self.section_map), " + loader" if self.loader is not None else ""))
