# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""DTC / Fault-Path model and enable-disable logic for EDC16 DSM.

In EDC16 the Diagnostic State Manager (DSM) drives every trouble code
through a *fault path* (``Dfp_<name>``). Each path has a handful of
calibration constants; the one that governs whether the code reacts is the
**error class** ``DSM_ClaDfp_<name>_C``. That class number indexes a class
definition (``DSM_Class<N>*_C``) whose ``Mil`` / ``SysLamp`` / ``Flc``
fields decide lamp illumination and whether the fault is stored.

Disabling a code = pointing its fault path at a silent class (Mil=0,
SysLamp=0, no store). The conventional "off" value is class 0 (no
reaction). Re-enabling restores the path's previous class; the editor
remembers it (and undo restores it exactly), and a loaded stock binary can
supply the original class for a file that was already modified.

This mirrors the 6HP project's DTC editor, adapted to DSM's fault-path model.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# OBD-II P-code -> standard description (bundled). Extend data/obd_pcodes.json.
_PDESC: Dict[str, str] = {}
# Canonical VAG customer-code lookup (P-code -> VAG, fault-name -> VAG), built
# from the p350 reference binaries where the CDK block decodes correctly. Used
# to recover VAG codes on families whose CDK block is misanchored.
_PCODE2VAG: Dict[str, int] = {}
_NAME2VAG: Dict[str, int] = {}
_VAG_LOADED = False


def _load_pdesc() -> Dict[str, str]:
    global _PDESC
    if not _PDESC:
        fn = os.path.join(os.path.dirname(__file__), "data", "obd_pcodes.json")
        try:
            _PDESC = json.load(open(fn, encoding="utf-8"))
        except Exception:
            _PDESC = {}
    return _PDESC


def _load_vag_maps():
    global _PCODE2VAG, _NAME2VAG, _VAG_LOADED
    if _VAG_LOADED:
        return
    _VAG_LOADED = True
    fn = os.path.join(os.path.dirname(__file__), "data", "vag_codes.json")
    try:
        d = json.load(open(fn, encoding="utf-8"))
        _PCODE2VAG = {k: int(v) for k, v in d.get("pcode2vag", {}).items()}
        _NAME2VAG = {k: int(v) for k, v in d.get("name2vag", {}).items()}
    except Exception:
        _PCODE2VAG, _NAME2VAG = {}, {}


def decode_sae(raw: Optional[int]) -> str:
    """Decode a 2-byte SAE J2012 code (as stored in DSM_CDCDfp_*) to text like
    'P0101'. Empty string for 0/None."""
    if not raw:
        return ""
    letter = "PCBU"[(raw >> 14) & 0x3]
    d1 = (raw >> 12) & 0x3
    return "%s%d%03X" % (letter, d1, raw & 0x0FFF)

# The value a fault path's class is set to in order to disable the code.
DISABLED_CLASS = 0

_DFP_RE = re.compile(r"^DSM_(\w+?)Dfp_(.+?)_C$")
_CLASS_RE = re.compile(r"^DSM_Class(\d+)(\w+)_C$")


@dataclass
class ClassDef:
    """Behaviour of one DSM error class."""
    number: int
    mil: Optional[int] = None        # illuminate MIL
    syslamp: Optional[int] = None    # system lamp
    flc: Optional[int] = None        # fault-entry confirmation (>0 => stores)

    @property
    def silent(self) -> bool:
        return not (self.mil or self.syslamp)

    @property
    def stores(self) -> bool:
        return bool(self.flc)


@dataclass
class FaultPath:
    """One trouble code: its fault path and the constants behind it."""
    name: str                        # e.g. "EGRVlv" (from Dfp_EGRVlv)
    cla: object = None               # the ClaDfp Entity (class selector)
    parts: Dict[str, object] = field(default_factory=dict)
    description: str = ""
    carb_code: Optional[int] = None     # CDC: raw SAE/OBD code (2-byte)
    vag_code: Optional[int] = None      # resolved customer (VAG) code
    vag_raw: Optional[int] = None       # CDK value as stored in this binary
    vag_source: str = "stored"          # 'stored' | 'p-code table' | 'name table'
    _orig_class: Optional[int] = None   # remembered before disabling

    @property
    def pcode(self) -> str:
        """OBD-II code, e.g. 'P0101' (empty if the path has no SAE code)."""
        return decode_sae(self.carb_code)

    @property
    def vag_str(self) -> str:
        """VAG fault number as a scan tool shows it: a minimum-5-digit,
        zero-padded decimal (e.g. 476 -> '00476'). '-' when the path has no
        recoverable customer code. A trailing '*' marks a code recovered from
        the canonical reference table because this binary's stored value was
        unreliable (see ``vag_source``)."""
        if not self.vag_code:
            return "-"
        star = "" if self.vag_source == "stored" else "*"
        return "%05d%s" % (self.vag_code, star)

    def code_desc(self) -> str:
        """Human-readable fault description: the standard OBD-II text for this
        path's P-code when known, else a decoded fault-path name, else the
        raw ECU description."""
        pc = self.pcode
        d = _load_pdesc().get(pc)
        if d:
            return d
        try:
            from .signal_names import fault_name
            hn = fault_name(self.name)
            if hn and hn.lower() != self.name.lower():
                return hn
        except Exception:
            pass
        # last resort: the ECU's own (pipe-delimited) description, main segment,
        # rendered to English where it is German
        if self.description:
            segs = [s.strip() for s in self.description.split("|")
                    if s.strip() and not s.strip().startswith("A2L:")]
            if segs:
                try:
                    from .signal_names import translate_german
                    return translate_german(segs[0])
                except Exception:
                    return segs[0]
        return self.description or self.name

    @property
    def dfp(self) -> str:
        return "Dfp_" + self.name

    @property
    def current_class(self) -> Optional[int]:
        if self.cla is None:
            return None
        return int(round(self.cla.cell(0, 0)))


class DtcTable:
    """All fault paths in a session, with class-behaviour resolution.

    On an EDC15 session this constructor hands back an
    ``edc15.dtc15.DtcTable15`` (same interface, fbw fault paths)."""

    def __new__(cls, session):
        if getattr(session, "is_edc15", False):
            from .edc15.dtc15 import DtcTable15
            return DtcTable15(session)
        return super().__new__(cls)

    def __init__(self, session):
        self.session = session
        self.classes: Dict[int, ClassDef] = {}
        self.paths: Dict[str, FaultPath] = {}
        self._build()

    def _build(self):
        # class definitions
        for e in self.session.entities:
            m = _CLASS_RE.match(e.name)
            if not m:
                continue
            num = int(m.group(1))
            field_ = m.group(2)
            cd = self.classes.setdefault(num, ClassDef(num))
            val = int(round(e.cell(0, 0)))
            if field_ == "Mil":
                cd.mil = val
            elif field_ == "SysLamp":
                cd.syslamp = val
            elif field_ == "Flc":
                cd.flc = val
        # fault paths
        for e in self.session.entities:
            m = _DFP_RE.match(e.name)
            if not m:
                continue
            kind, path = m.group(1), m.group(2)
            fp = self.paths.setdefault(path, FaultPath(path))
            fp.parts[kind] = e
            if kind == "Cla":
                fp.cla = e
                if not fp.description:
                    d = e.description.split("|")
                    fp.description = (d[1].strip() if len(d) > 1
                                      else e.description)[:120]
            elif kind == "CDC":
                v = int(round(e.cell(0, 0)))
                fp.carb_code = v or None
            elif kind == "CDK":
                v = int(round(e.cell(0, 0)))
                fp.vag_raw = v or None
        # keep only paths that actually have a class selector
        self.paths = {k: v for k, v in self.paths.items()
                      if v.cla is not None}
        self._resolve_vag()

    def _resolve_vag(self):
        """Set each path's ``vag_code`` (and ``vag_source``).

        The DSM customer-code (CDK) block decodes correctly on some firmware
        families and is misanchored on others (notably 03G p475_83, where the
        stored values are implausible). We detect which case this binary is in
        by checking how well its stored codes agree with the canonical
        P-code -> VAG reference: if agreement is high the stored values are
        trusted verbatim; if low, every code is recovered from the reference
        (by P-code first, then by fault-path name), falling back to the stored
        value only where the reference has no entry."""
        _load_vag_maps()
        ok = tot = 0
        for fp in self.paths.values():
            ref = _PCODE2VAG.get(fp.pcode) if fp.pcode else None
            if ref is not None and fp.vag_raw is not None:
                tot += 1
                if fp.vag_raw == ref:
                    ok += 1
        reliable = (tot == 0) or (ok / tot >= 0.5)
        self.vag_block_reliable = reliable
        for fp in self.paths.values():
            if reliable:
                fp.vag_code, fp.vag_source = fp.vag_raw, "stored"
                continue
            ref = _PCODE2VAG.get(fp.pcode) if fp.pcode else None
            if ref is not None:
                fp.vag_code, fp.vag_source = ref, "p-code table"
            elif fp.name in _NAME2VAG:
                fp.vag_code, fp.vag_source = _NAME2VAG[fp.name], "name table"
            else:
                # unreliable block, no reference match: the stored value is
                # known-bad, so report it as unknown rather than mislead.
                fp.vag_code, fp.vag_source = None, "unknown"

    # -- queries --------------------------------------------------------
    def class_of(self, fp: FaultPath) -> Optional[ClassDef]:
        c = fp.current_class
        if c is None:
            return None
        return self.classes.get(c)

    def is_enabled(self, fp: FaultPath) -> bool:
        """A code is 'enabled' when its class is not the disabled value and
        the class actually reacts (lights a lamp or stores)."""
        c = fp.current_class
        if c is None or c == DISABLED_CLASS:
            return False
        cd = self.classes.get(c)
        if cd is None:
            return True          # unknown class: assume active
        return not cd.silent or cd.stores

    def lights_mil(self, fp: FaultPath) -> bool:
        cd = self.class_of(fp)
        return bool(cd and cd.mil)

    def sorted_paths(self) -> List[FaultPath]:
        return [self.paths[k] for k in sorted(self.paths)]

    # -- edits (undoable through the session) --------------------------
    def set_enabled(self, fp: FaultPath, enabled: bool,
                    enable_class: Optional[int] = None):
        """Enable or disable a code by rewriting its class selector."""
        if fp.cla is None:
            return
        cur = fp.current_class
        if enabled:
            target = enable_class if enable_class is not None \
                else fp._orig_class
            if target is None or target == DISABLED_CLASS:
                # nothing to restore to; leave a sensible active default
                target = self._default_active_class()
            self.session.edit_cells(fp.cla, [(0, 0, float(target))])
        else:
            if cur != DISABLED_CLASS:
                fp._orig_class = cur
            self.session.edit_cells(fp.cla, [(0, 0, float(DISABLED_CLASS))])

    def restore_from(self, other: "DtcTable"):
        """Adopt each path's class from a stock DtcTable (bulk re-enable),
        as ONE undo step however many paths change."""
        with self.session.transaction("restore DTC classes from stock"):
            for name, fp in self.paths.items():
                src = other.paths.get(name)
                if src is None or fp.cla is None:
                    continue
                want = src.current_class
                if want is not None and want != fp.current_class:
                    self.session.edit_cells(fp.cla, [(0, 0, float(want))])

    def _default_active_class(self) -> int:
        # a class that lights the MIL and stores, preferring a low number
        for n in sorted(self.classes):
            cd = self.classes[n]
            if cd.mil and cd.stores:
                return n
        return 4

    def summary(self) -> Dict[str, int]:
        en = sum(1 for fp in self.paths.values() if self.is_enabled(fp))
        return {"total": len(self.paths), "enabled": en,
                "disabled": len(self.paths) - en}
