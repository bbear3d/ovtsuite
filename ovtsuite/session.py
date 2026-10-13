# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TunerSession: the Qt-free document object (hp6tuner pattern).

Owns the binary, the identified & built entities, and the undo stack.
``cli.py`` and the tests are the standing proof it imports no Qt.
"""

import contextlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .binary import BinaryFile
from .model import Entity
from .progress import Reporter, STEPS
from . import dim as _dim
from . import sgo as _sgo
from . import catalogue as _catalogue
from . import groups as _groups
from . import container as _cont
from . import frf as _frf_mod
from . import defn as _defn
from . import platforms as _platforms
from .platforms.identify import Identity as _Identity, identify as _identify
from .modules import api as _modapi
from .modules import registry as _registry

_REF_DIR = None   # resolved lazily; overridable for tests/packaging


def _ref_dir() -> str:
    global _REF_DIR
    if _REF_DIR is None:
        _REF_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Reference", "Binaries")
    return _REF_DIR


def set_ref_dir(path: str) -> None:
    global _REF_DIR
    _REF_DIR = path


def __getattr__(name):
    # EDC16 family identification moved to the EDC16 module
    # (ovtsuite.edc16.session16); these names stay importable from here for
    # tools and tests, resolved only when asked for so the base app never
    # imports the module just by importing the session.
    if name in ("detect_family", "_PROJECT_FAMILIES", "_DATASET_ID_SUBFAMILY"):
        from .edc16 import session16
        return getattr(session16, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


class Command:
    """One undoable edit: a list of ``(addr, bits, old_raw, new_raw)``."""

    def __init__(self, label: str, writes):
        self.label = label
        self.writes = list(writes)

    def apply(self, binfile: BinaryFile):
        for addr, bits, _old, new in self.writes:
            binfile.write(addr, new, bits)

    def revert(self, binfile: BinaryFile):
        for addr, bits, old, _new in self.writes:
            binfile.write(addr, old, bits)

    def merge(self, other: "Command") -> bool:
        """Absorb ``other`` if it continues this edit, keeping the ORIGINAL
        old values so one undo reverts the pair.

        Only same-label edits to the same addresses merge -- that is the hex
        editor typing the second nibble of a byte, which should be one undo
        step, not two. Anything else is a separate action.
        """
        if other.label != self.label:
            return False
        mine = {(a, b) for a, b, _o, _n in self.writes}
        if {(a, b) for a, b, _o, _n in other.writes} != mine:
            return False
        latest = {(a, b): n for a, b, _o, n in other.writes}
        self.writes = [(a, b, o, latest[(a, b)]) for a, b, o, _n in self.writes]
        return True

    def __repr__(self):
        return "<Command %r %d write(s)>" % (self.label, len(self.writes))


class CompoundCommand(Command):
    """Several commands applied and reverted as one.

    An auto-tune plan or a bulk regenerate is one action to the user, so it is
    one undo step. ``apply`` rolls back whatever already succeeded if a later
    part raises, so a half-applied plan can never reach the binary.
    """

    def __init__(self, label: str, commands):
        super().__init__(label, [])
        self.commands = list(commands)
        for c in self.commands:
            self.writes.extend(c.writes)

    def apply(self, binfile: BinaryFile):
        done = []
        try:
            for c in self.commands:
                c.apply(binfile)
                done.append(c)
        except Exception:
            for c in reversed(done):
                c.revert(binfile)
            raise

    def revert(self, binfile: BinaryFile):
        for c in reversed(self.commands):
            c.revert(binfile)

    def merge(self, other):
        return False

    def __len__(self):
        return len(self.commands)


class RegionCommand(Command):
    """An undoable byte edit into a region working buffer (MPC / EEPROM),
    rather than the external image.

    The external flash is the live ``binfile``; the MPC internal flash and the
    EEPROM live in their own working ``bytearray`` inside the session, folded
    back into the container only on save. So an edit to one of those regions
    still needs to be undoable and to count toward the document's modified
    state, but it must NOT touch ``binfile``. ``apply``/``revert`` therefore
    ignore the ``binfile`` the undo stack hands them and write into the
    captured ``target`` buffer instead. Everything else -- the undo stack, the
    clean mark, coalescing of two-nibble typing -- works unchanged, because a
    RegionCommand is just a Command whose writes land somewhere else."""

    def __init__(self, label, target, writes):
        super().__init__(label, writes)
        self._target = target

    def apply(self, binfile):
        for addr, _bits, _old, new in self.writes:
            self._target[addr] = new & 0xFF

    def revert(self, binfile):
        for addr, _bits, old, _new in self.writes:
            self._target[addr] = old & 0xFF

    def merge(self, other):
        # Only merge with another edit to the SAME region buffer, else two
        # regions that share a label+address would coalesce by mistake.
        if not isinstance(other, RegionCommand) or other._target is not self._target:
            return False
        return super().merge(other)


class CommandStack:
    """Undo/redo with coalescing, a bounded history, and an honest clean mark.

    The clean mark is the COMMAND that was on top when the file was last
    saved, not a count. Counting is subtly wrong: save with two commands
    done, undo one, then make a different edit and the count is two again --
    so the document reports itself unmodified while its bytes differ from
    disk, and closing it loses the change without a prompt.

    ``limit`` bounds memory on a long session. If the clean command is evicted
    the mark becomes unreachable and the document stays modified for good,
    which is the safe direction to fail in.
    """

    def __init__(self, limit: int = 500):
        self.limit = limit
        self._done: List[Command] = []
        self._undone: List[Command] = []
        self._clean: Optional[Command] = None      # None == the empty state
        self._clean_lost = False
        self.revision = 0                          # bumps on every change

    # -- state ---------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_label(self) -> Optional[str]:
        return self._done[-1].label if self._done else None

    def redo_label(self) -> Optional[str]:
        return self._undone[-1].label if self._undone else None

    def top(self) -> Optional[Command]:
        """The most recently done command, or None. A read-only peek --
        ``ovtsuite.patch`` uses it to persist what a ``transaction()`` block
        actually wrote, so a patch apply can be found and reverted later
        from the change log alone."""
        return self._done[-1] if self._done else None

    @property
    def is_clean(self) -> bool:
        if self._clean_lost:
            return False
        top = self._done[-1] if self._done else None
        return top is self._clean

    def mark_clean(self) -> None:
        self._clean = self._done[-1] if self._done else None
        self._clean_lost = False

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()
        self._clean = None
        self._clean_lost = False
        self.revision += 1

    def __len__(self):
        return len(self._done)

    # -- mutation ------------------------------------------------------
    def push(self, cmd: Command, coalesce: bool = False) -> Command:
        """Record an ALREADY-APPLIED command. Returns the command the caller
        should treat as current (the previous one, when merged)."""
        self.revision += 1
        # The redo branch is being discarded; if the clean state lived there
        # it is no longer reachable by undoing.
        if self._undone and self._clean is not None                 and any(c is self._clean for c in self._undone):
            self._clean_lost = True
        self._undone.clear()
        if coalesce and self._done:
            prev = self._done[-1]
            # never merge across a save, or undoing to the clean state
            # becomes impossible
            if prev is not self._clean and prev.merge(cmd):
                return prev
        self._done.append(cmd)
        if len(self._done) > self.limit:
            dropped = self._done.pop(0)
            if dropped is self._clean:
                self._clean_lost = True
        return cmd

    def undo(self, binfile: BinaryFile) -> Optional[Command]:
        if not self._done:
            return None
        cmd = self._done.pop()
        cmd.revert(binfile)
        self._undone.append(cmd)
        self.revision += 1
        return cmd

    def redo(self, binfile: BinaryFile) -> Optional[Command]:
        if not self._undone:
            return None
        cmd = self._undone.pop()
        cmd.apply(binfile)
        self._done.append(cmd)
        self.revision += 1
        return cmd


class TunerSession:
    def __init__(self, path: str, family: Optional[str] = None,
                 ref_path: Optional[str] = None, progress=None):
        # A .dim (Dimsport BDM-dump container, see ovtsuite.dim) bundles the
        # external flash we tune ALONGSIDE internal flash + EEPROM chunks we
        # don't touch. Detected by content (the container's own magic), not
        # by extension, same as the EDC15/ZF6HP platform sniffing below.
        # self._dim_chunks stays None for every other input; save() below
        # only repacks a .dim when it is set.
        self._dim_chunks = None
        # A .sgo ("SGML Object File" v2) container -- the factory
        # flash-delivery format for ZF 6HP TCUs and EDC16 ECUs alike (see
        # ovtsuite.sgo). Detected by content (its own magic), same as .dim
        # above. self._sgo_container stays None for every other input;
        # save() below only repacks a .sgo when it is set. A section this
        # module couldn't decode (ovtsuite.sgo.SgoContainer.unrecognized)
        # is surfaced on self.sgo_warning rather than silently dropped.
        self._sgo_container = None
        self.sgo_warning = None
        #: A note the platform's loader wants the user to read about this
        #: file (how its parameters were found, an unverified mapping, ...).
        #: Not a container problem, so kept apart from ``sgo_warning``.
        self.load_note = None
        self._frf_container = None
        self.frf_warning = None
        # A native container (.edc16 / .edc15 / .6hp / .hvac, see
        # ovtsuite.container): main image + optional MPC / EEPROM / history /
        # definition blocks. Detected by magic; its main (EXTERNAL) block
        # becomes binfile, the rest is kept here so the flasher and the hex
        # editor can reach internal flash and EEPROM too.
        self._container = None
        # Working byte buffers for the non-external regions (MPC internal
        # flash, EEPROM) while they are being edited in the hex view. A
        # region is copied in here lazily on its first edit; save() folds
        # any that are present back into the loaded container. Keyed by the
        # region name region_buffers() exposes.
        self._region_edits = {}
        self._region_saved = {}       # region bytes as last saved (history)
        self._hvac = None            # HvacImage when this .sgo is a Climatronic
        with open(path, "rb") as _fh:
            _head = _fh.read(0x30)   # sgo.detect() needs this many to check
        if _head[:4] in _dim.MAGICS:
            self._dim_chunks = _dim.load(path)
            self.binfile = BinaryFile.from_bytes(
                _dim.external_flash(self._dim_chunks), os.path.basename(path))
        elif _head[:8] == b"OVTHVAC\x01":
            # OVT-Suite's own HVAC format (.hvac): a repacked Climatronic image
            # plus the flash loader and the stock baseline digests. There is no
            # .sgo container to parse -- reconstruct the HvacImage directly and
            # expose its flat image 0-based little-endian (same as the .sgo
            # Climatronic branch below).
            from .flash import hvac_image as _hvac_img
            with open(path, "rb") as _fh:
                _raw = _fh.read()
            self._hvac = _hvac_img.HvacImage.from_own(
                _raw, name=os.path.basename(path))
            self.binfile = BinaryFile.from_bytes(
                bytes(self._hvac.image), os.path.basename(path), endian="<")
        elif _sgo.detect(_head):
            with open(path, "rb") as _fh:
                _raw = _fh.read()
            self._sgo_container = _sgo.parse(_raw, os.path.basename(path))
            from .flash import hvac_image as _hvac_img
            if _hvac_img.is_hvac_container(self._sgo_container):
                # Climatronic: sections live in the high F2MC window
                # (0xFA0000/0xFC0000+), which sgo.image() would drop (it is
                # 0-based and capped at 0x400000). Assemble the flat image at
                # the firmware's own base, exposed 0-based (little-endian), and
                # keep the HvacImage for variable mapping + save/flash.
                _part, _ver, _fn = _hvac_img.sgo_ident(_raw)
                self._hvac = _hvac_img.HvacImage.from_container(
                    self._sgo_container, name=os.path.basename(path),
                    part=_part, version=_ver)
                self.binfile = BinaryFile.from_bytes(
                    bytes(self._hvac.image), os.path.basename(path), endian="<")
            else:
                _base, _flat = _sgo.image(self._sgo_container)
                if self._sgo_container.needs_module:
                    from .modules import registry as _reg
                    self.sgo_warning = (
                        "This .sgo holds an EDC16 image. Its sections are decoded "
                        "only when the EDC16 module's checksum confirms the decode, "
                        "so they are shown as 0xFF filler here. %s"
                        % _reg.missing_text(self._sgo_container.needs_module,
                                            "this file"))
                elif self._sgo_container.unrecognized:
                    self.sgo_warning = (
                        "%d of %d sections in this .sgo could not be decoded "
                        "(unrecognized cipher) and are shown as 0xFF filler: %s"
                        % (len(self._sgo_container.unrecognized),
                           len(self._sgo_container.sections),
                           ", ".join("0x%06X" % s.addr
                                     for s in self._sgo_container.unrecognized)))
                self.binfile = BinaryFile.from_bytes(_flat, os.path.basename(path))
        elif _frf_mod.detect(_head):
            # Newer UDS .frf flash-delivery container (6HP28 & other UDS 6HP).
            # It carries four blocks (boot/program/.../calibration); the
            # calibration block (DB_4) is the one with real tunable 6HP maps,
            # so open THAT as the working image -- the same bytes
            # `ovtsuite frf-extract` writes and `export` already matches 2000+
            # named tables from. Read-only: there is no .frf repacker, so
            # save() refuses a .frf path (see save_refusal) and the image is
            # saved with Save As as a native container or a plain .bin.
            with open(path, "rb") as _fh:
                _raw = _fh.read()
            self._frf_container = _frf_mod.parse(_raw, os.path.basename(path))
            # ZF 6HP (DB_4 recognized by zf6hp.detect) or ZF 8HP (every block
            # carries ZF's own descriptor, see ovtsuite.zf8hp).
            _cal = [b for b in self._frf_container.blocks
                    if getattr(b, "is_calibration", False)]
            if not _cal:
                raise ValueError(
                    "%s is a .frf container but has no recognizable ZF 6HP "
                    "or ZF 8HP calibration block to open." % os.path.basename(path))
            # Assemble the FULL flash image at the blocks' true addresses (the
            # 2 MB layout a real OBD read produces), not just the calibration
            # block, so the opened binary matches what other tools work with.
            try:
                _im = _frf_mod.image(self._frf_container)
                _absent = "; ".join("0x%X-0x%X" % r for r in _im.absent) or "none"
                _inf = ", ".join(_im.inferred) or "none"
                self.frf_warning = (
                    "Assembled the full %d KB image from this .frf (blocks at "
                    "their flash addresses). The .frf does not carry the "
                    "bootloader (%s), left 0xFF; block placement inferred "
                    "without a descriptor: %s. Read-only: use Save As to save "
                    "it as a container or a .bin."
                    % (_im.size // 1024, _absent, _inf))
                self.binfile = BinaryFile.from_bytes(
                    _im.data, os.path.basename(path))
            except Exception as _ex:
                # No descriptor-anchored block to position a full image: fall
                # back to opening the calibration block alone.
                if len(_cal) > 1:
                    _cal.sort(key=lambda b: len(b.plaintext), reverse=True)
                self.frf_warning = (
                    "Opened the calibration block (%s) from this .frf; could "
                    "not assemble a full image (%s). Read-only: use Save As to "
                    "save it as a container or a .bin." % (_cal[0].short_name, _ex))
                self.binfile = BinaryFile.from_bytes(
                    bytes(_cal[0].plaintext), os.path.basename(path))
        elif _cont.format_for_magic(_head) is not None:
            with open(path, "rb") as _fh:
                _raw = _fh.read()
            self._container = _cont.Container.from_bytes(_raw)
            _ext = self._container.external()
            if _ext is None:
                raise ValueError("%s has no main-image block"
                                 % self._container.ext)
            if self._container.kind == "hvac":
                from .flash import hvac_image as _hvac_img
                self._hvac = _hvac_img.HvacImage.from_container(
                    self._container, name=os.path.basename(path))
                self.binfile = BinaryFile.from_bytes(
                    bytes(self._hvac.image), os.path.basename(path), endian="<")
            else:
                self.binfile = BinaryFile.from_bytes(
                    bytes(_ext), os.path.basename(path))
        else:
            self.binfile = BinaryFile.load(path)
        self.path = path
        data = bytes(self.binfile.data)
        # Platform, from format-defined markers rather than the file size --
        # see ovtsuite.platforms.identify for the tests and their order. A
        # native container's declared kind wins over sniffing the bytes (an
        # .edc16 may legitimately wrap any ECU image, so only a SPECIFIC
        # container kind is trusted). Nothing is forced into a platform by
        # elimination: a binary no test claims is "unknown".
        kind = self._container.kind if self._container is not None else None
        #: What the base app can say about this binary without any module
        #: (ovtsuite.platforms.identify.Identity).
        self.binary_identity = _identify(data, container_kind=kind,
                                         hvac=self._hvac is not None)
        _needs = getattr(self._sgo_container, "needs_module", "")
        if _needs and not self.binary_identity.known:
            # an EDC16 .sgo opened without the EDC16 module (see sgo.parse)
            self.binary_identity = _Identity(_needs, "container")
        platform = self.binary_identity.platform
        if platform == "hvac" and self._hvac is None:
            platform = "unknown"     # an .hvac container that held no image
        #: The installed module serving this platform (None: not installed,
        #: or nothing supports the binary). See ovtsuite.modules.registry.
        self.module = _registry.for_platform(platform)
        #: "full" (definitions), "checksum" (a checksum-only module that can
        #: check THIS binary's checksum) or "none" (hex editor and the
        #: universal tools only).
        self.support = ("full" if self.module is not None
                        and self.module.has(_modapi.DEFINITIONS) else
                        "checksum" if self._module_checks(platform, data)
                        else "none")
        #: Why the parameter tree is empty, for the GUI/CLI to say; None when
        #: the platform's module built the tree.
        self.unsupported_reason = None
        # Size the bar before the first report: the platforms take a different
        # number of steps, and a dialog that starts at /13 and switches to /7
        # jumps backwards in front of the user.
        rep = Reporter(progress, STEPS.get(platform, 1)
                       if self.support == "full" else 1)
        rep.step("Reading %s" % os.path.basename(path))
        self.platform = platform
        self.family = None
        self.kb = None
        self.result = None
        self.region = (0, len(data))
        self.datasets = []            # EDC15 only
        self.match15 = None           # EDC15 only
        self.message_table = None     # EDC15 only
        self.entities: List[Entity] = []
        self.by_key: Dict[tuple, Entity] = {}
        self.family_choice = None     # ZF 6HP only
        #: Where the model came from: "none" (built from the bytes), "used"
        #: (read from the container's stored definition, see ovtsuite.defn),
        #: or the reason a stored definition was passed over.
        self.defn_state = "none"
        restored = False
        if self.support != "full":
            self.unsupported_reason = (
                "%s. %s" % (self.binary_identity.describe(),
                            self._limited_text(platform, data)))
        else:
            if (self._container is not None and family is None
                    and _defn.supports(platform)):
                why = _defn.restore(self, self._container, data, rep)
                restored = why is None
                self.defn_state = "used" if restored else why
            if restored:
                # A restored snapshot skips the platform build, so anything
                # that build DERIVES rather than stores is re-derived here.
                self.module.call("after_restore", self)
            else:
                try:
                    self.module.call("build", self, family, progress=rep)
                except _modapi.NoDefinition as ex:
                    # The module is here but has nothing for this software:
                    # open it in the universal tools rather than refusing it.
                    self.entities, self.by_key = [], {}
                    self.support = ("checksum" if self._module_checks(
                        platform, data) else "none")
                    self.unsupported_reason = "%s. %s." % (
                        self.binary_identity.describe(), (str(ex)[:1].upper() + str(ex)[1:]).rstrip(".")
                        if str(ex) else "No definition matches this binary")
        self.undo_stack = CommandStack()
        self.change_log: List[str] = []
        self._txn = None              # open transaction(), see transaction()
        self.last_checksum = None
        # When True, EDC16 2 MB save/correct also regenerates the content digest
        # required to flash edited blocks over KWP2000/OBD (see checksum.correct(
        # ..., digest=True)). Defaults True so saved tunes are flash-ready; the
        # digest is the canonical MPPS-identical trailer and is harmless on the
        # bench/BDM path. Set False to leave the trailer untouched.
        self.write_digest = True
        # When True a ZF 6HP save/correct ALSO rewrites the program/boot-area
        # descriptor self-CRCs (zlib CRC-32), not just the calibration checksum.
        # Off by default: a calibration tune never changes a 28-byte descriptor
        # record, so there is nothing to correct; enable it only when editing
        # program/boot bytes or a descriptor's identity. Trusted offsets come
        # from the unedited original so a re-stamped descriptor is still fixed.
        # See ovtsuite/zf6hp/checksum6hp.py.
        self.correct_program_area = False
        self._variant_entities = None
        # pristine bytes as loaded, for modified-vs-original reporting
        self.original = bytes(self.binfile.data)
        # bytes as last written to self.path (starts equal to `original` --
        # nothing has been saved yet); save() diffs against this to log an
        # automatic version checkpoint (see ovtsuite.history) and advances
        # it, so each checkpoint only ever covers what changed THAT save.
        self._last_saved_snapshot = self.original
        rep.finish("%d parameters" % len(self.entities))

    def __getattr__(self, name):
        # Attributes a restored definition left to decode on first use
        # (ovtsuite.defn.LAZY). Only reached when normal lookup fails.
        lazy = self.__dict__.get("_lazy")
        if lazy and name in lazy:
            val = lazy.pop(name)()
            setattr(self, name, val)
            return val
        raise AttributeError("%s has no attribute %r"
                             % (type(self).__name__, name))

    # -- editing --------------------------------------------------------
    def edit_cells(self, entity: Entity, cells) -> Command:
        """cells: iterable of (row, col, display_value). Undoable."""
        writes = []
        # An entity that knows its own byte writes (a VDO cluster coding bit
        # inside a shared byte, or a value stored in several copies) supplies
        # them, so the undo stack holds exactly what changed.
        cell_writes = getattr(entity, "cell_writes", None)
        for r, c, val in cells:
            if cell_writes is not None:
                writes.extend(cell_writes(r, c, val))
                continue
            addr = entity.addr + entity.index(r, c) * entity.step
            old = self.binfile.read(addr, entity.bits, False)
            if entity.is_float:
                # the undo stack stores raw machine words, so an IEEE-754 cell
                # goes in as its 32-bit pattern like any other write
                import struct as _s
                enc = _s.unpack(">I", _s.pack(
                    ">f", float(entity.eq.encode(val))))[0]
                writes.append((addr, entity.bits, old, enc))
                continue
            raw = int(round(entity.eq.encode(val)))
            lo = -(1 << (entity.bits - 1)) if entity.signed else 0
            hi = ((1 << (entity.bits - 1)) - 1 if entity.signed
                  else (1 << entity.bits) - 1)
            raw = max(lo, min(hi, raw))
            enc = raw + (1 << entity.bits) if raw < 0 else raw
            writes.append((addr, entity.bits, old, enc))
        cmd = Command("edit %s" % entity.name, writes)
        return self._record(cmd, "%s: %d cell(s)" % (entity.name, len(writes)))

    def edit_values(self, pairs, label="edit"):
        """Write several single-cell constants as ONE undoable command.

        ``pairs`` is an iterable of ``(entity, display_value)``. Used by the
        gear-ratio auto-generate, so regenerating the whole v/n set reverts in a
        single undo."""
        writes = []
        for entity, val in pairs:
            if entity is None:
                continue
            addr = entity.addr
            old = self.binfile.read(addr, entity.bits, False)
            raw = int(round(entity.eq.encode(float(val))))
            lo = -(1 << (entity.bits - 1)) if entity.signed else 0
            hi = ((1 << (entity.bits - 1)) - 1 if entity.signed
                  else (1 << entity.bits) - 1)
            raw = max(lo, min(hi, raw))
            enc = raw + (1 << entity.bits) if raw < 0 else raw
            writes.append((addr, entity.bits, old, enc))
        if not writes:
            return None
        cmd = Command(label, writes)
        return self._record(cmd, "%s: %d value(s)" % (label, len(writes)))

    def scale_entity(self, entity: Entity, factor: float) -> Command:
        cells = [(r, c, entity.cell(r, c) * factor)
                 for r in range(entity.rows) for c in range(entity.cols)]
        return self.edit_cells(entity, cells)

    def edit_axis(self, ax, i: int, display_value: float):
        """Write one axis breakpoint (display units) at index ``i``. Undoable
        (routes through edit_bytes). Returns the Command or None if unchanged."""
        import struct
        en = self.binfile.endian
        if getattr(ax, "is_float", False):
            val = float(ax.eq.encode(display_value))
            raw = struct.pack(en + "f", val)
        else:
            step = ax.bits // 8
            v = int(round(ax.eq.encode(display_value)))
            lo = -(1 << (ax.bits - 1))
            hi = (1 << ax.bits) - 1
            v = max(lo, min(hi, v))
            enc = v + (1 << ax.bits) if v < 0 else v
            raw = enc.to_bytes(step, "big" if en == ">" else "little")
        return self.edit_bytes(ax.addr + i * len(raw), raw)

    def edit_bytes(self, addr: int, new_bytes,
                   coalesce: bool = False) -> Optional[Command]:
        """Raw byte edit at ``addr`` (an undoable 8-bit write per changed byte).
        Returns the Command, or None if nothing actually changed. Used by the
        hex editor; integrates with undo/redo, save and checksum correction."""
        writes = []
        for i, nb in enumerate(bytes(new_bytes)):
            a = addr + i
            if a >= len(self.binfile.data):
                break
            old = self.binfile.data[a]
            if old != (nb & 0xFF):
                writes.append((a, 8, old, nb & 0xFF))
        if not writes:
            return None
        if not getattr(self, "_structure_edited", False):
            # A byte outside every cell and breakpoint may be a count word or
            # header: the entity table no longer necessarily describes the
            # image, so no definition snapshot may be saved from it.
            try:
                spans = _defn.param_spans(list(self.entities) + (
                    list(self.variant_entities()) if self.platform == "edc16" else []))
                if any(_defn.outside_params(spans, a) for a, _b, _o, _n in writes):
                    self._structure_edited = True
            except Exception:
                self._structure_edited = True
        cmd = Command("edit bytes @0x%X" % addr, writes)
        # coalesce: typing the two nibbles of a byte is one edit, not two
        return self._record(cmd, "bytes @0x%X: %d byte(s)" % (addr, len(writes)),
                            coalesce=coalesce)

    # -- the undo stack -------------------------------------------------
    def _record(self, cmd: Command, summary: str = "",
                coalesce: bool = False) -> Command:
        """Apply ``cmd``, push it, and note it in the change log.

        Inside a :meth:`transaction` the command is applied but held back, so
        the whole group lands as one undo step."""
        cmd.apply(self.binfile)
        if self._txn is not None:
            self._txn.append(cmd)
            if summary:
                self.log(summary)
            return cmd
        kept = self.undo_stack.push(cmd, coalesce=coalesce)
        if summary and kept is cmd:
            self.log(summary)
        return kept

    @contextlib.contextmanager
    def transaction(self, label: str):
        """Group every edit made inside into one undo step.

        A dialog that writes a value, its source selector and a type byte has
        performed ONE action as far as the user is concerned, but it makes
        three ``edit_bytes`` calls -- and three presses of Ctrl+Z to walk back,
        with two of them leaving the ECU in a state the dialog would never
        write on purpose. Inside this block those become one
        :class:`CompoundCommand`.

        If the body raises, everything already applied is reverted, so a
        dialog that fails halfway cannot leave a partial write behind.
        """
        if self._txn is not None:                 # nested: join the outer one
            yield
            return
        self._txn = []
        try:
            yield
        except Exception:
            for c in reversed(self._txn):
                c.revert(self.binfile)
            self._txn = None
            raise
        cmds, self._txn = self._txn, None
        if not cmds:
            return
        if len(cmds) == 1:
            self.undo_stack.push(cmds[0])
        else:
            self.undo_stack.push(CompoundCommand(label, cmds))

    def log(self, message: str) -> None:
        """Add a line to the change log written beside the saved file."""
        self.change_log.append(message)

    def push(self, cmd: Command, summary: str = "") -> Command:
        """Record a command a helper module already applied (edc15.vss15)."""
        kept = self.undo_stack.push(cmd)
        if summary:
            self.log(summary)
        return kept

    def undo(self) -> Optional[str]:
        cmd = self.undo_stack.undo(self.binfile)
        return cmd.label if cmd is not None else None

    def redo(self) -> Optional[str]:
        cmd = self.undo_stack.redo(self.binfile)
        return cmd.label if cmd is not None else None

    @property
    def can_undo(self) -> bool:
        return self.undo_stack.can_undo

    @property
    def can_redo(self) -> bool:
        return self.undo_stack.can_redo

    @property
    def revision(self) -> int:
        """Bumps on every change -- a cheap cache key for derived views."""
        return self.undo_stack.revision

    @property
    def modified(self) -> bool:
        return not self.undo_stack.is_clean

    # "An EDC15 session" means one its module built: platform-specific code
    # (measuring blocks, DTCs, gears, the 6HP views ...) reads the model the
    # module made, so a limited session -- module not installed -- is none
    # of these, whatever its platform. ``platform`` still names it.
    @property
    def is_edc15(self) -> bool:
        return self.platform == "edc15" and self.support == "full"

    @property
    def is_zf6hp(self) -> bool:
        return self.platform == "zf6hp" and self.support == "full"

    @property
    def is_zf8hp(self) -> bool:
        return self.platform == "zf8hp" and self.support == "full"

    def checksum_provider(self):
        """This platform's checksum provider (:class:`ovtsuite.modules.api.
        ChecksumProvider`), or None when no installed module provides one --
        the platform's module is not installed, or the binary is unknown."""
        return _registry.checksum(self.platform, self)

    def _module_checks(self, platform, data) -> bool:
        """Whether ``platform``'s module can check THIS binary's checksum --
        not merely whether it has a checksum for the platform: the Aisin
        module handles the TR-60SN (09D) checksum only, so a TR-80SD image
        is no "checksum only" file. Decided by the provider's own
        ``is_supported``; a provider that cannot answer this early (it needs
        more of the session) keeps the module's word, as before."""
        if not _registry.can(platform, _modapi.CHECKSUM):
            return False
        try:
            cs = _registry.checksum(platform, self)
            return cs is not None and bool(cs.is_supported(data))
        except Exception:                  # noqa: BLE001 - cannot tell yet
            return True

    def _limited_text(self, platform, data) -> str:
        """What would support this binary, and -- when its module is here
        but cannot check this binary's checksum -- why not."""
        text = _registry.missing_text(platform,
                                      checksum=self.support == "checksum")
        if self.support == "none" and _registry.can(platform, _modapi.CHECKSUM):
            try:
                why = _registry.checksum_unsupported_text(
                    platform, _registry.checksum(platform, self), data)
            except Exception:              # noqa: BLE001 - a nicety
                why = ""
            if why:
                text = "%s %s" % (text, why)
        return text

    def _checksum_opts(self, write_digest=None, program_area=None) -> dict:
        """The options every provider understands (each ignores the rest):
        EDC16 regenerates the KWP2000 content digest, ZF 6HP also rewrites
        the program/boot descriptor self-CRCs."""
        return {"digest": self.write_digest if write_digest is None else write_digest,
                "program_area": (self.correct_program_area if program_area is None
                                 else program_area)}

    def _checksum_key(self, cs):
        """What a verify report depends on: the provider, its options and
        the bytes themselves (hashed -- a save can correct checksums in
        place without moving ``revision``)."""
        import hashlib
        data = self.binfile.data
        opts = tuple(sorted(self._checksum_opts().items()))
        # by class: checksum_provider() makes a fresh provider on each call
        return (type(cs), id(data), len(data), opts,
                hashlib.blake2b(data, digest_size=16).digest())

    def checksum_status(self):
        """Verify report for the current bytes, or None if unsupported.

        Remembered for the bytes it was made from: verifying an EDC16 image
        sums every block of it, and the status bar asks after every edit."""
        cs = self.checksum_provider()
        if cs is None:
            return None
        key = self._checksum_key(cs)
        cached = getattr(self, "_checksum_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        rep = None
        try:
            if cs.is_supported(self.binfile.data):
                rep = cs.verify(self.binfile.data, **self._checksum_opts())
        except _modapi.ChecksumError:
            rep = None
        self._checksum_cache = (key, rep)
        return rep

    def checksum_status_known(self) -> bool:
        """Whether :meth:`checksum_status` would answer from what it
        remembers (no verify needed) for the current bytes."""
        cs = self.checksum_provider()
        if cs is None:
            return True
        cached = getattr(self, "_checksum_cache", None)
        return cached is not None and cached[0] == self._checksum_key(cs)

    def checksum_exempt_ranges(self):
        """Byte ranges in ``self.binfile.data`` this platform's checksum
        module considers a checksum/digest/signature slot -- values that
        legitimately change whenever anything ELSE in the block they cover
        changes, so a raw byte-level diff (``ovtsuite.patch.
        diff_outside_calibration``/``diff_gaps``) should not report them as
        a real content difference. Best-effort: ``[]`` for an unsupported
        image, a platform whose module is not installed, or a provider with
        no exempt ranges.
        """
        cs = self.checksum_provider()
        if cs is None:
            return []
        try:
            if not cs.is_supported(self.binfile.data):
                return []
            return cs.exempt_ranges(self.binfile.data, **self._checksum_opts())
        except Exception:
            return []

    def correct_checksums(self, write_digest: Optional[bool] = None,
                          program_area: Optional[bool] = None):
        """Recompute the flash checksums in place; return (report_before,
        changed). No-op (changed=False) when unsupported or already valid --
        including when the platform's module is not installed.

        For 2 MB EDC16 images the content digest is also regenerated when
        ``write_digest`` (or, if not given, ``self.write_digest``) is True --
        needed to flash edited blocks over KWP2000/OBD.
        """
        cs = self.checksum_provider()
        if cs is None:
            return None, False
        opts = self._checksum_opts(write_digest, program_area)
        try:
            if not cs.is_supported(self.binfile.data):
                return None, False
            before = cs.verify(self.binfile.data, **opts)
            fixed = bytes(cs.correct(self.binfile.data, **opts))
            changed = bytes(self.binfile.data) != fixed
            if changed:
                if len(fixed) == len(self.binfile.data):
                    self.binfile.data = bytearray(fixed)
                else:
                    n = min(len(self.binfile.data), len(fixed))
                    self.binfile.data[:n] = fixed[:n]
            self.last_checksum = cs.verify(self.binfile.data, **opts)
            return before, changed
        except _modapi.ChecksumError:
            return None, False

    def eeprom_checksum_status(self):
        """The EDC16 EEPROM region's checksum state (``identified``, ``ok``,
        ``bad``, ``describe()``), or None when this file carries no EDC16
        EEPROM or no installed module can check one. The layout is identified
        on the EEPROM as loaded / last saved, by its own block checksums."""
        if self.platform != "edc16":
            return None
        try:
            if not self.has_eeprom():
                return None
            cs = self.checksum_provider()
            if cs is None or not hasattr(cs, "eeprom_status"):
                return None
            cur = dict(self.region_buffers()).get(self.EEPROM_REGION)
            if cur is None:
                return None
            return cs.eeprom_status(cur, self.region_saved(self.EEPROM_REGION))
        except Exception:                  # noqa: BLE001 - a report, not a gate
            return None

    def correct_eeprom_checksums(self, edited_only: bool = False) -> bool:
        """Recompute the EDC16 EEPROM region's block checksums in its working
        buffer (written with the file on save). Only when the layout is
        positively identified -- an EEPROM that is not is left exactly as it
        is. ``edited_only``: only when the region was edited since it was
        loaded / last saved. True when bytes changed."""
        if edited_only:
            buf = self._region_edits.get(self.EEPROM_REGION)
            if buf is None or bytes(buf) == self.region_saved(self.EEPROM_REGION):
                return False
        st = self.eeprom_checksum_status()
        if st is None or not st.identified or st.ok:
            return False
        buf = self.region_edit_buffer(self.EEPROM_REGION)
        if buf is None:
            return False
        fixed = self.checksum_provider().eeprom_correct(
            bytes(buf), self.region_saved(self.EEPROM_REGION))
        if len(fixed) != len(buf) or fixed == bytes(buf):
            return False
        buf[:] = fixed                     # in place: the hex view shares it
        return True

    def variant_entities(self) -> List[Entity]:
        """Editable Entity clones for the secondary/variant tables (cached).

        Each is a copy of its primary twin re-pointed at the secondary data
        address, named "<primary> (variant @0x...)" and filed under a
        'Variant / secondary' category, so it exports and edits like any table.
        Empty for binaries with no secondary block.
        """
        if getattr(self, "_variant_entities", None) is not None:
            return self._variant_entities
        if self.platform in ("hvac", "vdo"):
            self._variant_entities = []
            return []
        if self.is_edc15:
            # datasets are first-class categories on EDC15; nothing hidden
            self._variant_entities = []
            return []
        import dataclasses
        from . import variant as _variant
        by_z = {e.addr: e for e in self.entities}
        out = []
        tables = list(_variant.find_variant_tables(self))
        try:
            codings = _variant.copy_codings(self, tables)
        except Exception:                       # never cost the variants
            codings = {}
        for vt in tables:
            # EVERY copy, identical or not: a copy is the table those codings
            # read, so one left out could not be edited for them at all. The
            # copies of supporting maps go behind "Advanced" like their main
            # copies (groups.is_advanced, via base_tier).
            twin = by_z.get(vt.main_z)
            if twin is None:
                continue
            # All second copies live together in their own group, filed by
            # the group and heading their main copy has -- so the coding
            # variants are one place to look, laid out like the rest.
            cats = ["Coding variants"] + list(twin.categories)
            twin_path = list(twin.category_path or ()) or [
                _catalogue.heading_text(
                    twin.categories[0] if twin.categories else "", twin.name)]
            path = [_groups.group_name(twin.tier)] + twin_path
            out.append(dataclasses.replace(
                twin, addr=vt.sec_z, name="%s (variant @0x%X)" % (twin.name, vt.sec_z),
                tier=_groups.CODING, base_tier=twin.tier,
                source="variant", categories=cats, category_path=path,
                plain=twin.plain,
                # which coding selects this copy, read from the pointer array
                coding=(_variant.coding_text(codings.get(vt.sec_z))
                        or "coding variant"),
                # the twin's selector note describes the MAIN block's curve
                set_note="", selector=""))
            # ... and the primary says which coding(s) keep it, so the pair
            # reads both ways in the tree
            main_txt = _variant.coding_text(codings.get(vt.main_z))
            if main_txt:
                twin.coding = main_txt
        # in the order of their main copies: by group, then heading
        rank = {id(e): i for i, e in enumerate(self.entities)}
        twin_of = {v.addr: by_z.get(vt.main_z)
                   for v, vt in zip(out, [t for t in tables
                                          if by_z.get(t.main_z) is not None])}
        out.sort(key=lambda v: (rank.get(id(twin_of.get(v.addr)), 1 << 30),
                                v.addr))
        self._variant_entities = out
        return out

    def has_mpc(self):
        """Cheap check: does the loaded container carry an MPC internal flash?"""
        if self._dim_chunks is not None:
            return True
        if self._container is not None:
            return self._container.mpc() is not None
        sc = self._sgo_container
        return bool(sc and any(x.addr == 0x404000 and x.plaintext is not None
                               for x in sc.sections))

    def has_eeprom(self):
        """Cheap check: does the loaded container carry an EEPROM block?"""
        if self._container is not None:
            return self._container.eeprom() is not None
        return self._dim_chunks is not None

    # Region names as region_buffers() exposes them; save() folds edits back
    # into the container by these keys.
    MPC_REGION = "MPC (internal flash)"
    EEPROM_REGION = "EEPROM"

    def _container_regions(self):
        """Ordered [(name, bytes)] of the flash regions the LOADED CONTAINER
        carries, straight from the source (ignores any in-progress edits).
        Always includes the external image; adds the MPC internal flash and/or
        EEPROM when the container (.dim / .sgo / .edc16) carries them."""
        regions = [("External flash", bytes(self.binfile.data))]
        mpc = None
        try:
            if self._dim_chunks is not None:
                from . import dim as _dim
                mpc = _dim.internal_flash(self._dim_chunks)
            elif self._container is not None:
                mpc = self._container.mpc()
            elif self._sgo_container is not None:
                sec = next((x for x in self._sgo_container.sections
                            if x.addr == 0x404000 and x.plaintext is not None), None)
                if sec is not None:
                    buf = bytearray(b"\xFF" * 0x81000)
                    buf[0x4000:0x4000 + len(sec.plaintext)] = sec.plaintext
                    mpc = bytes(buf)
        except Exception:
            mpc = None
        if mpc:
            regions.append((self.MPC_REGION, bytes(mpc)))
        ee = None
        try:
            if self._dim_chunks is not None:
                from . import dim as _dim
                try:
                    ee = _dim.eeprom(self._dim_chunks)
                except Exception:
                    ee = None
            elif self._container is not None:
                ee = self._container.eeprom()
        except Exception:
            ee = None
        if ee:
            regions.append((self.EEPROM_REGION, bytes(ee)))
        return regions

    def region_names(self):
        """Names of the non-main regions this file carries (EEPROM, MPC)."""
        return [n for n, _b in self._container_regions()
                if n != "External flash"]

    def region_saved(self, name):
        """The bytes of region ``name`` as last saved (or as loaded) -- what
        its edits are measured against for the version history; None when
        the file has no such region."""
        got = self._region_saved.get(name)
        if got is not None:
            return got
        for rn, rb in self._container_regions():
            if rn == name:
                return bytes(rb)
        return None

    def region_buffers(self):
        """Ordered [(name, bytes)] of the flash regions this loaded source
        carries, for the hex editor's region switcher, with any in-progress
        edits overlaid so switching away and back (or reopening the editor)
        shows the edited bytes rather than the pristine container copy."""
        out = []
        for name, data in self._container_regions():
            edited = self._region_edits.get(name)
            out.append((name, bytes(edited) if edited is not None else data))
        return out

    def region_editable(self, name):
        """True if region ``name`` can be edited in place and folded back into
        the loaded container on save. The external flash edits through
        ``edit_bytes`` (it is the live image), not through here; MPC is
        save-backable from .dim / .sgo / .edc16, EEPROM from .dim / .edc16."""
        if name == self.MPC_REGION:
            return (self._dim_chunks is not None
                    or self._container is not None
                    or self._sgo_container is not None)
        if name == self.EEPROM_REGION:
            return (self._dim_chunks is not None
                    or self._container is not None)
        return False

    def region_edit_buffer(self, name):
        """The live working ``bytearray`` for region ``name``, created lazily
        from the container's current bytes on first access, or None if the
        region is not present / not editable. The hex model points its live
        view at this same object so RegionCommand edits show immediately."""
        if not self.region_editable(name):
            return None
        buf = self._region_edits.get(name)
        if buf is not None:
            return buf
        base = None
        for rn, rb in self._container_regions():
            if rn == name:
                base = bytearray(rb)
                break
        if base is None:
            return None
        self._region_edits[name] = base
        return base

    def edit_region(self, name, off, new_bytes, coalesce=False):
        """Raw undoable byte edit into a non-external region working buffer.
        Mirrors :meth:`edit_bytes` but the writes land in the region's own
        buffer (see :class:`RegionCommand`), so undo/redo, the modified mark
        and coalescing all work while the external image is untouched. Returns
        the Command, or None if nothing changed."""
        buf = self.region_edit_buffer(name)
        if buf is None:
            return None
        writes = []
        for i, nb in enumerate(bytes(new_bytes)):
            a = off + i
            if a >= len(buf):
                break
            old = buf[a]
            if old != (nb & 0xFF):
                writes.append((a, 8, old, nb & 0xFF))
        if not writes:
            return None
        cmd = RegionCommand("edit %s @0x%X" % (name, off), buf, writes)
        return self._record(cmd, "%s @0x%X: %d byte(s)" % (name, off, len(writes)),
                            coalesce=coalesce)

    def _mpc_corrected(self, mpc, key_image):
        """Recompute the edited MPC internal flash's additive checksum for
        write-back. If the image carries no recognisable KN descriptor
        (correct() then raises), the user's bytes are written as typed --
        a non-standard internal image should not block the save."""
        cs = self.checksum_provider()
        if cs is None:
            return bytes(mpc)
        try:
            return cs.correct(bytes(mpc), key_image=key_image)
        except _modapi.ChecksumError:
            return bytes(mpc)

    def save(self, path: Optional[str] = None, fix_checksum: bool = True,
             backup: bool = True, write_log: bool = True,
             label: str = "") -> str:
        """Write the image, atomically.

        A tune is hours of work and the only copy is usually the file being
        overwritten, so the bytes go to a temporary file in the same
        directory, are flushed to the platter, and are then moved into place
        with ``os.replace`` -- an atomic rename on every platform we target.
        Losing power or hitting a full disk mid-write leaves the previous file
        intact rather than a truncated one.

        ``backup`` keeps a ``.bak`` of the ORIGINAL, once: the first save is
        the one that can still be regretted, and refreshing it on every
        subsequent save would quietly overwrite the only pristine copy. A
        backup is a courtesy, not a precondition, so it never fails the save.
        Saving over an existing native container (.edc16 / .edc15 / .6hp /
        .hvac) makes none: its version history is inside the file.
        """
        import os as _os
        out = path or self.path
        # Refuse a target this session cannot be written as BEFORE anything
        # changes: correcting checksums and logging the version checkpoint
        # below are side effects a refused save must not leave behind.
        why = self.save_refusal(out)
        if why:
            raise ValueError(why)
        if fix_checksum:
            self.correct_checksums()
            # an edited EDC16 EEPROM region (.edc16 / .dim) gets its block
            # checksums too -- only when its layout is positively identified
            self.correct_eeprom_checksums(edited_only=True)
        # Containers that recompute a checksum while packing (HVAC) only do
        # so when this platform's module provides one: with it absent the
        # bytes are saved exactly as edited.
        pack_fix = bool(fix_checksum) and self.checksum_provider() is not None

        data = bytes(self.binfile.data)
        # The flat image the editor works on. Version checkpoints are byte
        # ranges IN THIS IMAGE (history.revert_to_version writes them back
        # through edit_bytes), so they must be diffed here -- before a .dim /
        # .edc16 / .sgo / .hvac container packs (and, for .edc16, zlib-
        # compresses) it into file bytes whose offsets mean nothing to the
        # editor. Diffing the container made every checkpoint of a container
        # file garbage and its first save a whole-file delta.
        flat = data
        if write_log:
            # Automatic version checkpoint: everything that changed since
            # this path was last saved (or, first time, since it was
            # loaded). Logged into change_log here, BEFORE the log is
            # written (into an .edc16 below, or the .log beside any other
            # file after the write), so it lands in the same save's entry --
            # see ovtsuite.history.
            # ``label`` is what makes the history navigable: without it every
            # checkpoint reads "save" and the only way to tell iterations apart
            # is the timestamp.
            from . import history as _history
            _history.log_version(self, self._last_saved_snapshot, flat,
                                 label=label,
                                 regions=_history.region_changes(self))
        #: True when this save carries the change log INSIDE the file (an
        #: .edc16 container) rather than in a .log beside it
        log_inside = False
        if self._dim_chunks is not None and out.lower().endswith(".dim"):
            # Re-pack: external flash gets the tuned/corrected bytes; internal
            # flash, EEPROM and any metadata chunk are carried through
            # byte-for-byte unchanged (see ovtsuite.dim.replace_external_flash).
            # Gated on the OUTPUT path still being a .dim -- opened from one
            # and saved/Save-As back to a .dim path (the default), this
            # round-trips the container; an explicit Save As to a .bin
            # exports just the tuned external flash instead.
            # Fold any in-place MPC / EEPROM edits back into the container
            # chunks first, so they ride along in the rebuilt .dim. The MPC
            # image's additive checksum is recomputed (the external image is
            # the key source for its signature block).
            _mpc = self._region_edits.get(self.MPC_REGION)
            if _mpc is not None:
                self._dim_chunks = _dim.replace_internal_flash(
                    self._dim_chunks, self._mpc_corrected(_mpc, data))
            _ee = self._region_edits.get(self.EEPROM_REGION)
            if _ee is not None:
                self._dim_chunks = _dim.replace_chunk(self._dim_chunks, 3, bytes(_ee))
            data = _dim.build(_dim.replace_external_flash(self._dim_chunks, data))
        elif _cont.format_for_ext(out) is not None:
            # Native container (.edc16 / .edc15 / .6hp / .hvac): the main
            # image gets the tuned/corrected bytes; MPC, EEPROM, the change
            # log and the definition ride along, refreshed from any in-place
            # edits. A session that came from a plain file becomes a
            # container the first time it is saved under one of these names.
            data = self._native_bytes(out, data, write_log)
            log_inside = True
        elif self._hvac is not None and self.platform == "hvac" and out.lower().endswith(".sgo"):
            # Climatronic: route the repack through hvac_image so (a) the flat
            # image is based at the firmware's own flash base (not 0), (b) the
            # embedded flash-loader section is passed through byte-verbatim (its
            # address overlaps section 1), and (c) the internal 0xFCFFF4 flash
            # checksum is recomputed. Sync the editor's bytes in first.
            n = min(len(self._hvac.image), len(data))
            self._hvac.image[:n] = data[:n]
            data = self._hvac.to_sgo(fix_checksum=pack_fix)
            self._sgo_container = self._hvac.container
        elif self._sgo_container is not None and out.lower().endswith(".sgo"):
            # Re-pack: only sections whose decoded content actually changed
            # get re-encoded (ovtsuite.sgo.apply_image diffs against each
            # section's own last-known plaintext), so an untouched section
            # round-trips byte-for-byte and an edited one is re-encoded with
            # its own scheme. Gated on the OUTPUT path still being .sgo, the
            # same convention as .dim above -- Save As to a .bin exports the
            # tuned flat image instead (see export_sgo_binary in the GUI).
            patched = _sgo.apply_image(self._sgo_container, 0, data)
            # An edited MPC section is re-encoded in place (apply_image's
            # flat image is 0-based and does not reach the 0x404000 MPC
            # window, so it is spliced separately, checksum recomputed).
            _mpc = self._region_edits.get(self.MPC_REGION)
            if _mpc is not None:
                _sec = next((x for x in patched.sections
                             if x.addr == 0x404000 and x.plaintext is not None), None)
                if _sec is not None:
                    _img = self._mpc_corrected(_mpc, data)
                    _win = _img[0x4000:0x4000 + len(_sec.plaintext)]
                    patched = _sgo.replace_section(patched, 0x404000, _win)
            data = _sgo.to_bytes(patched)
            self._sgo_container = patched
        directory = _os.path.dirname(_os.path.abspath(out)) or "."
        tmp = _os.path.join(directory, ".%s.tmp" % _os.path.basename(out))
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            _os.fsync(fh.fileno())
        if backup and log_inside and _os.path.exists(out):
            # A native container already keeps its own version history with
            # every save, so it needs no .bak -- but only when what is being
            # replaced IS one. Converting a plain image (or upgrading a
            # legacy .hvac) in place still keeps the original beside it.
            try:
                with open(out, "rb") as _fh:
                    if _cont.format_for_magic(_fh.read(8)) is not None:
                        backup = False
            except OSError:
                pass
        if backup and _os.path.exists(out):
            bak = out + ".bak"
            if not _os.path.exists(bak):
                try:
                    import shutil
                    shutil.copy2(out, bak)
                except OSError:
                    pass                # best effort; never blocks the save
        _os.replace(tmp, out)
        self._last_saved_snapshot = flat
        # what the EEPROM / MPC working buffers held when written: the
        # baseline the next save's history record is measured from
        for _n, _b in self._region_edits.items():
            self._region_saved[_n] = bytes(_b)

        self.undo_stack.mark_clean()
        if path:                        # Save As: the document is now this file
            self.path = path
            self.binfile.name = _os.path.basename(path)
        if log_inside:
            if write_log:
                self.change_log = []    # written into the file just now
            from . import changelog as _cl
            _cl.retire_sidecar(out)
        elif write_log:
            self.write_change_log(out)
        return out

    def save_refusal(self, path: Optional[str] = None) -> Optional[str]:
        """Why :meth:`save` would refuse to write this session to ``path``
        (default: its own path), or None when it can. The output format is
        chosen by the path's extension, as everywhere in :meth:`save`:

        * ``.frf`` -- there is no .frf writer (the container is read-only,
          see the loader), so writing the flat image under that name would
          leave a file whose name no longer matches its contents. A session
          opened from a .frf is saved with Save As (a native container or
          a .bin) instead.
        * a native container (.edc16 / .edc15 / .6hp / ...) of ANOTHER
          platform -- it cannot hold this image.

        Callers (the GUI's Save / Save As) may ask first to steer the user
        before anything happens; :meth:`save` checks it again itself, before
        any side effect."""
        out = path or self.path or ""
        if out.lower().endswith(".frf"):
            native = _cont.format_for_kind(self.platform)
            from .branding import APP_NAME
            return ("%s is a .frf file, which %s reads but cannot "
                    "write -- use Save As to save this image as %s.bin "
                    "instead." % (os.path.basename(out), APP_NAME,
                                  "a %s or a " % native.ext if native else "a "))
        fmt = _cont.format_for_ext(out)
        if fmt is not None and fmt.kind != self.platform:
            return ("%s files hold %s images, but this is a %s image -- save "
                    "it as .%s instead" % (fmt.ext, fmt.kind, self.platform,
                                           (_cont.format_for_kind(self.platform)
                                            or fmt).ext.lstrip(".")))
        return None

    def _native_bytes(self, out, data, write_log):
        """The bytes of this session as the native container ``out`` names."""
        fmt = _cont.format_for_ext(out)
        why = self.save_refusal(out)
        if why:                         # save() refused it already, up front
            raise ValueError(why)
        c = self._container
        # A file that is not a native container yet (.bin, .dim, .sgo) brings
        # the regions it carries -- a .dim's internal flash and EEPROM, a
        # .sgo's internal flash -- into the new container, edited or not.
        # Read before self._container is replaced below: the regions are
        # derived from the source the session was opened from.
        carried = dict(self.region_buffers()) if c is None else {}
        if c is None or c.kind != fmt.kind:
            fresh = _cont.Container(fmt)
            if c is not None:           # carry what is not platform-specific
                for b in c.blocks:
                    if b.type in (_cont.BLOCK_MPC, _cont.BLOCK_EEPROM):
                        fresh.add(b.type, b.data, b.address, b.name)
            c = fresh
            self._container = c
        if fmt.kind == "hvac":
            n = min(len(self._hvac.image), len(data))
            self._hvac.image[:n] = data[:n]
            self._hvac.fill_container(
                c, fix_checksum=self.checksum_provider() is not None)
        else:
            c.add(_cont.BLOCK_EXTERNAL, data)
        if fmt.kind == "vdo":
            from .platforms import vdo_container as _cvdo
            _cvdo.fill_meta(c, self)
        elif fmt.kind == "zf8hp":
            from .platforms import zf8hp_container as _c8hp
            _c8hp.fill_meta(c, self)
        _mpc = self._region_edits.get(self.MPC_REGION)
        if _mpc is None:
            _mpc = carried.get(self.MPC_REGION)
        if _mpc is not None:
            c.add(_cont.BLOCK_MPC, self._mpc_corrected(_mpc, data)
                  if fmt.kind == "edc16" else bytes(_mpc))
        _ee = self._region_edits.get(self.EEPROM_REGION)
        if _ee is None:
            _ee = carried.get(self.EEPROM_REGION)
        if _ee is not None:
            c.add(_cont.BLOCK_EEPROM, bytes(_ee))
        # The resolved parameter table rides along (ovtsuite.defn), so the
        # next open skips identification and matching. Built from the bytes
        # being written, so it describes exactly this file.
        blob = (_defn.build(self, data)
                if fmt.kind == self.platform else None)
        if blob is not None:
            c.add(_cont.BLOCK_DEFN, blob)
        else:
            c.remove(_cont.BLOCK_DEFN)
        # The change log / version history rides inside the container
        # (ovtsuite.changelog). Always re-read from the file on disk: a
        # checkpoint renamed or merged since this file was opened is
        # rewritten there, and the copy loaded with the file is stale. A
        # document's history follows it through Save As; an older file whose
        # history is still in a .log beside it is folded in.
        from . import changelog as _cl
        text = _cl.read_text(self.path)
        if write_log and self.change_log:
            text += _cl.format_entry(self.change_log, os.path.basename(out))
        if text:
            c.add(_cont.BLOCK_LOG, _cl.block_bytes(text))
        return c.to_bytes()

    def sibling(self, data: bytes, name: Optional[str] = None,
                verify: bool = False) -> "TunerSession":
        """Another image of THIS file (a saved version, a patched copy) as its
        own session, sharing this one's definition instead of identifying and
        matching it again -- the model is "what is where", and a different
        revision of the same file has the same layout. Parameter cells are
        read from ``data``; nothing is shared that either session writes to.

        The caller vouches that ``data`` is the same file (same length);
        a different length raises ValueError. Only platforms whose
        definition can be shared (EDC16, EDC15, ZF 6HP; a ZF session's
        schedule lists are re-pointed at the new image's copies). An image
        whose structure differs from this one's (the snapshot's own
        ``mask_digest`` test) raises ValueError, and the caller loads it in
        full.

        ``verify`` applies that structure test; it is off by default because
        a saved version of this very file differs from it in checksum bytes
        outside any parameter, which the test would (rightly, for an
        unknown file) call a different structure."""
        import copy
        import dataclasses
        if not _defn.supports(self.platform):
            raise NotImplementedError(
                "%s sessions have no shareable definition" % self.platform)
        if len(data) != len(self.binfile.data):
            raise ValueError("a sibling image must be the same size "
                             "(%d, not %d bytes)" % (len(data),
                                                     len(self.binfile.data)))
        for k in list(getattr(self, "_lazy", None) or ()):
            getattr(self, k)               # settle lazily decoded state first
        if verify:
            ents = list(self.entities)
            if self.platform == "edc16":
                ents += list(self.variant_entities())
            if (_defn.mask_digest(bytes(data), ents)
                    != _defn.mask_digest(bytes(self.binfile.data), ents)):
                raise ValueError(
                    "the image's structure differs from this file's")
        new = copy.copy(self)              # shares kb / match result / tables
        new.__dict__.pop("_lazy", None)
        bf = BinaryFile.from_bytes(bytes(data),
                                   name or self.binfile.name,
                                   endian=getattr(self.binfile, "endian", ">"))
        new.binfile = bf
        remap = {}

        def fresh(e):
            n = remap.get(id(e))
            if n is None:
                n = dataclasses.replace(e, binfile=bf)
                # attributes that are not dataclass fields (a scanned
                # schedule's ``scan_key``, cached labels) are identity too
                for k, v in vars(e).items():
                    if k not in vars(n):
                        vars(n)[k] = v
                remap[id(e)] = n
            return n

        new.entities = [fresh(e) for e in self.entities]
        new.by_key = {k: fresh(e) for k, e in self.by_key.items()}
        new._variant_entities = None
        # Anything else that holds this session's entities (a ZF session's
        # shift / TCC schedule lists and their scan caches) must point at the
        # new image's copies, or it would read the old image's cells.
        from .model import Entity as _Entity
        for k, v in list(vars(new).items()):
            if k in ("entities", "by_key", "binfile", "_variant_entities"):
                continue
            if isinstance(v, (list, tuple)) and any(
                    isinstance(x, _Entity) for x in v):
                vars(new)[k] = type(v)(fresh(x) if isinstance(x, _Entity)
                                       else x for x in v)
        # lookup indexes built over the OLD entities; rebuilt on demand
        for k in [k for k in vars(new) if k in (
                "_key_by_id", "_scan_by_key", "_scan_n", "_name_idx",
                "_name_by_name", "_name_n", "_struct_by_key", "_struct_n")
                or k.endswith(("_index", "_idx"))]:
            vars(new).pop(k, None)
        new.undo_stack = CommandStack()
        new.change_log = []
        new._txn = None
        new.last_checksum = None
        new._region_edits = {}
        new._region_saved = {}
        new._container = None
        new.defn_state = "shared"
        new.original = bytes(data)
        new._last_saved_snapshot = new.original
        return new

    def write_change_log(self, path: str) -> None:
        """Append the pending change log beside the saved file, and clear it.

        A tuned file that comes back six months later is otherwise a wall of
        changed bytes with no record of intent. Best effort: a log that cannot
        be written must never cost the user their save.
        """
        if not self.change_log:
            return
        import os as _os
        from datetime import datetime
        target = _os.path.splitext(path)[0] + ".log"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("# %s  saved %s\n" % (stamp, _os.path.basename(path)))
                for line in self.change_log:
                    fh.write("  %s\n" % line)
        except OSError:
            return                      # keep the entries for the next save
        self.change_log = []

    # -- comparison -----------------------------------------------------
    def _key_of(self) -> Dict[int, tuple]:
        """``id(entity) -> its by_key key``, built once.

        ``counterpart`` used to find an entity's key by walking ``by_key``
        until the value matched, which is O(n) per call and O(n^2) over a
        whole-file diff -- 5,519 entities on a ZF 6HP image.
        """
        idx = getattr(self, "_key_by_id", None)
        if idx is None or len(idx) != len(self.by_key):
            idx = {id(e): k for k, e in self.by_key.items()}
            self._key_by_id = idx
        return idx

    @staticmethod
    def _struct_key(entity) -> tuple:
        """Identity for an entity the knowledge base never named.

        Address plus geometry, which is what a structurally detected table
        has instead of a name: two calibrations of the same software put the
        same table at the same offset with the same shape. When the layouts
        differ the key simply does not match and the pairing fails, which is
        the honest outcome -- it never invents a counterpart.
        """
        key = getattr(entity, "scan_key", None)
        if key is not None:
            # A structurally detected schedule carries its place in the run,
            # which survives a different layout; its address does not.
            return ("\x00scan",) + tuple(key)
        return ("\x00struct", entity.addr, entity.rows, entity.cols,
                entity.bits)

    def _struct_index(self) -> Dict[tuple, Entity]:
        idx = getattr(self, "_struct_by_key", None)
        if idx is None:
            named = set(id(e) for e in self.by_key.values())
            idx = {}
            for e in self.entities:
                if id(e) not in named:
                    idx.setdefault(self._struct_key(e), e)
            self._struct_by_key = idx
        return idx

    def _scan_index(self) -> Dict[tuple, Entity]:
        """``scan_key -> entity`` over EVERY entity that carries one.

        Deliberately independent of ``by_key`` and of whether the entity has a
        name. ``_struct_index`` indexes only entities missing from ``by_key``,
        because a byte address is the identity of last resort for something the
        knowledge base never named -- but a scan key is not a fallback, it is a
        real identity, and a schedule keeps it whether or not a definition also
        names it.

        That distinction matters between two files of different provenance. A
        .6hp container opened from its stored definition has its schedules
        scanned and unnamed; an OEM image identified against its own definition
        may have the same schedules named and therefore in ``by_key``. Both carry
        the same scan key, and routing the lookup through ``_struct_index`` made
        the named side invisible to the unnamed one.
        """
        idx = getattr(self, "_scan_by_key", None)
        if idx is None or getattr(self, "_scan_n", -1) != len(self.entities):
            idx = {}
            for e in self.entities:
                key = getattr(e, "scan_key", None)
                if key is not None:
                    idx.setdefault(("\x00scan",) + tuple(key), e)
            self._scan_by_key = idx
            self._scan_n = len(self.entities)
        return idx

    def _name_index(self) -> Dict[str, Entity]:
        """``symbol -> entity`` for named entities missing from ``by_key``.

        ``by_key`` is built from the matcher's items, so an entity added to the
        tree afterwards is absent from it however well identified it is. The
        identification block is the case that matters: ``_id_entities`` appends
        17 fields per ZF 6HP image, each carrying its A2L symbol, and none of
        them reach ``by_key``.

        Without this they fell through to ``_struct_key``, which is a byte
        address -- and the identification block does not sit at the same address
        in two calibrations. Between ``8E1910156C__0040`` and
        ``4F2910156R__0050`` it is 610 bytes apart, so all 17 fields paired
        against nothing and the Hardware/Software and Variant coding groups came
        up blank whenever an OEM binary was opened as a comparison.

        A symbol is a layout-independent identity, which is the principle
        ``by_key`` already rests on; pairing on it is correct wherever one
        exists, and absent where the other calibration does not carry the field.
        """
        idx = getattr(self, "_name_by_name", None)
        if idx is None or getattr(self, "_name_n", -1) != len(self.entities):
            named = set(id(e) for e in self.by_key.values())
            idx = {}
            for e in self.entities:
                if e.name and id(e) not in named:
                    idx.setdefault(e.name, e)
            self._name_by_name = idx
            self._name_n = len(self.entities)
        return idx

    def counterpart(self, other: "TunerSession",
                    entity: Entity) -> Optional[Entity]:
        """The same parameter in another session.

        Identity is the KB name plus reference address, never a byte address
        -- so two calibrations of different structure still pair correctly.

        **Entities the KB never named are paired structurally.** They are not
        in ``by_key`` at all, so before this they had no counterpart and were
        silently invisible to every comparison: on a ZF 6HP image that is all
        82 shift schedules and the torque-converter tables -- the only tables
        anyone loads a comparison to look at. "Show only what differs" hid
        them and a generated patch left them out.

        The empty-name fallback that used to sit here is gone. An entity with
        no name has no identity, and ``x.name == name`` with ``name == ""``
        matches the first unnamed entity in the other file -- which on a 6HP
        image means comparing one shift schedule against a different one.
        """
        # A coding-switched table pairs with the copy the SAME coding selects
        # in the other file -- a second copy is named after its own address,
        # so by name it never paired at all.
        if getattr(entity, "coding", "") or getattr(entity, "source", "") == "variant":
            hit = self._coding_counterpart(other, entity)
            if hit is not None or getattr(entity, "source", "") == "variant":
                return hit
        # A scan key first, for either side: it is a real identity and it does
        # not care whether the knowledge base also named the table.
        mine = getattr(entity, "scan_key", None)
        if mine is not None:
            hit = other._scan_index().get(("\x00scan",) + tuple(mine))
            if hit is not None:
                return hit
        key = self._key_of().get(id(entity))
        if key is not None:
            hit = other.by_key.get(key)
            if hit is not None:
                return hit
            name = key[0]
            if not name:
                return None
            hit = next((x for x in other.entities if x.name == name), None)
            if hit is not None:
                return hit
            return other._by_group_alias(name)
        # Named but not in by_key -- an entity added to the tree after the index
        # was built, which is every identification field. Its symbol identifies
        # it; its byte address does not, so this must come before the structural
        # key rather than after it.
        if entity.name:
            hit = other._name_index().get(entity.name)
            if hit is not None:
                return hit
            hit = next((x for x in other.entities
                        if x.name == entity.name), None)
            if hit is not None:
                return hit
            hit = other._by_group_alias(entity.name)
            if hit is not None:
                return hit
        return other._struct_index().get(self._struct_key(entity))

    def _coding_counterpart(self, other: "TunerSession",
                            entity: Entity) -> Optional[Entity]:
        """The copy of ``entity``'s table that the same coding selects in
        ``other``.

        The candidates are ``other``'s main copy of the table and its second
        copies (``variant_entities``), each carrying the codings that select
        it, read from that file's own pointer array. Strongest first:

        1. the copy whose codings overlap ``entity``'s most -- 038906016Q's
           "150031/158031" copy pairs with 038997016R's "150031" copy;
        2. where ``other`` does not switch this table at all, its single main
           copy -- every coding uses it;
        3. otherwise the copy for the same transmission (the last coding
           digit, VAG scheme): Q's "158034 (automatic)" copy against R, which
           stores no 158034, pairs with R's automatic main copy.

        None when ``other`` has no such table.
        """
        from . import variant as _variant
        base = _variant_base(entity.name)
        idx = getattr(other, "_coding_idx", None)
        if idx is None:
            idx = {}
            try:
                for v in other.variant_entities():
                    idx.setdefault(_variant_base(v.name), []).append(v)
            except Exception:
                pass
            other._coding_idx = idx
        main = next((x for x in other.entities if x.name == base), None) \
            or other._by_group_alias(base)
        copies = list(idx.get(base, ()))
        if main is None and not copies:
            return None
        if not copies:
            return main                     # not switched there: one table
        mine = _coding_values(getattr(entity, "coding", ""))
        cands = ([main] if main is not None else []) + copies

        def overlap(x):
            return len(mine & _coding_values(getattr(x, "coding", "")))

        best = max(cands, key=overlap)
        if overlap(best) > 0:
            return best

        def tx(codes):
            return {_variant.decode_coding(c)[0] for c in codes} - {None, ""}

        want = tx(mine)
        same = [x for x in cands
                if want & tx(_coding_values(getattr(x, "coding", "")))]
        return same[0] if len(same) == 1 else None

    def _by_group_alias(self, name: str) -> Optional[Entity]:
        """The entity holding ``name`` under its other ASAP2 spelling.

        Bosch names a table that shares its axes with others a *group* map or
        curve (``_GMAP``/``_GCUR``) and a stand-alone one ``_MAP``/``_CUR``;
        which one a project used is a property of the A2L, not of the
        parameter. The P350 (038906016Q, BHW) calls the driver's wish maps
        ``AccPed_trqEng1_GMAP``, the P475 (LQ) ``AccPed_trqEng1_MAP`` -- the
        same table, which paired against nothing, so an LQ compared with a
        P350 showed no comparison for any driver's wish map. No dataset holds
        both spellings of one name, so the swap is unambiguous; an exact name
        always wins (it is tried first by the caller).
        """
        for a, b in (("_GMAP", "_MAP"), ("_MAP", "_GMAP"),
                     ("_GCUR", "_CUR"), ("_CUR", "_GCUR")):
            if name.endswith(a):
                alt = name[:-len(a)] + b
                return next((x for x in self.entities if x.name == alt), None)
        return None


def _variant_base(name: str) -> str:
    """``Gearbx_rTrans1_C`` for ``Gearbx_rTrans1_C (variant @0x189692)``."""
    return name.split(" (variant @", 1)[0]


def _coding_values(text: str) -> set:
    """{150031, 158031} from "coding 150031/158031 (5-speed manual)"."""
    import re
    m = re.match(r"coding ([\d/]+)", text or "")
    return {int(c) for c in m.group(1).split("/") if c} if m else set()


def entity_ref(entity) -> Optional[dict]:
    """A durable reference to an entity, for remembering it across a restart.

    The same identity question ``counterpart`` answers, asked across time rather
    than across files: a symbol where there is one, otherwise the entity's
    position in its structurally detected family.

    Returns None only when there is genuinely nothing to find it by again.
    Before this, the tab history simply skipped every entity without a name --
    which on a ZF 6HP image is all 82 shift schedules and all 12 TCC maps, the
    only tables anyone keeps open, so reopening a file never brought them back.
    """
    if entity is None:
        return None
    name = getattr(entity, "name", None) or ""
    scan = getattr(entity, "scan_key", None)
    if not name and scan is None:
        return None
    return {"name": name, "addr": getattr(entity, "addr", None),
            "scan": list(scan) if scan is not None else None}


def find_by_ref(session, ref) -> Optional[Entity]:
    """The entity an ``entity_ref`` names, in this session, or None.

    The scan key is tried first and on its own terms: it survives a different
    address, which a reopened file may well have if it was edited meanwhile.
    """
    if session is None or not isinstance(ref, dict):
        return None
    scan = ref.get("scan")
    if scan:
        hit = session._scan_index().get(("\x00scan",) + tuple(scan))
        if hit is not None:
            return hit
    name = ref.get("name") or ""
    if not name:
        return None
    pool = list(session.entities)
    try:
        pool += list(session.variant_entities())
    except Exception:
        pass
    hits = [e for e in pool if e.name == name]
    addr = ref.get("addr")
    for e in hits:
        if addr is not None and getattr(e, "addr", None) == addr:
            return e
    return hits[0] if hits else None


#: How much a placement's provenance is worth, strongest first. Used only to
#: decide which of two files' answers to keep when both describe the same
#: layout; it does not change how either was arrived at.
GRADE_RANK = {
    "anchored": 6, "verified": 6, "struct": 6, "measured": 6,
    "placed": 5,          # the program's own pointer table put it here
    "bracketed": 4, "segmented": 3, "projected": 2, "carried": 1, "": 0,
}


def reconcile_placements(primary, comparison) -> int:
    """Make two calibrations of the SAME layout agree on where a symbol is.

    A symbol has one address in one piece of software. When the primary and the
    comparison resolve it differently, at least one is wrong -- and because the
    comparison view reads each side at its own placement, the overlay then
    shows two unrelated tables under one name. That is what made a retune look
    as though it had rewritten ``zf_mes_*``, ``zf_ASS_FZFWB`` and
    ``KF_WTSGLS12``, none of which it had touched.

    Why this rather than a better scanner: the disagreement is genuinely
    ambiguous. On the 3.0 TFSI pair the moved symbols are one interleaved
    double chain -- ``KF_TPHSP*`` and ``KF_WTSGLS*`` competing for the same
    structures -- slid by a single slot, and both files carry the same 162
    colliding addresses, so neither answer is internally distinguishable.
    Measured alternatives both cost more than they returned: excluding every
    placed table's payload took transfer accuracy from 0.8577/0.8710 to
    0.8051/0.8419 and still left 9 symbols moving, and requiring a longer
    monotonic run before trusting an 8-bit parse dropped exact geometry from
    0.998 to 0.838.

    So the placements are not recomputed; the two sides are simply required to
    give one answer, taking the better-evidenced of the two. Applied ONLY when
    the layout signatures match -- a genuinely different calibration keeps its
    own addresses, which is the case the two-address path exists for.

    Returns how many symbols were brought into line.
    """
    a = getattr(primary, "layout6", None)
    b = getattr(comparison, "layout6", None)
    if a is None or b is None or a.digest != b.digest:
        return 0

    theirs = {}
    for e in comparison.entities:
        n = (e.name or "")
        if n and getattr(e, "addr", None) is not None:
            theirs.setdefault(n, e)
    moved = 0
    for e in primary.entities:
        n = (e.name or "")
        if not n or getattr(e, "addr", None) is None:
            continue
        other = theirs.get(n)
        if other is None or other.addr == e.addr:
            continue
        mine = GRADE_RANK.get(getattr(e, "source", ""), 0)
        yours = GRADE_RANK.get(getattr(other, "source", ""), 0)
        # The stronger grade wins. On a tie the primary's stands: it is the
        # file being edited, so its addresses are the ones the user is acting
        # on, and the result does not depend on which side was opened first.
        if yours > mine:
            _repoint_entity(e, other.addr, getattr(other, "source", ""))
        else:
            _repoint_entity(other, e.addr, getattr(e, "source", ""))
        moved += 1
    return moved


def _repoint_entity(e, addr: int, source: str) -> None:
    """Move an entity, carrying its axes with it."""
    delta = addr - e.addr
    if not delta:
        return
    for ax in ("x", "y"):
        axis = getattr(e, ax, None)
        if axis is not None and getattr(axis, "addr", None) is not None:
            axis.addr += delta
    e.addr = addr
    end = getattr(e, "end", None)
    if end is not None:
        try:
            e.end = end + delta
        except Exception:
            pass
    if source:
        try:
            e.source = source
        except Exception:
            pass
