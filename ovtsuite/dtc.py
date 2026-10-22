# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""DTC / fault-path model and enable-disable logic for EDC16 DSM.

In EDC16 the Diagnostic State Manager (DSM) drives every trouble code
through a *fault path* (``Dfp_<name>``). Its settings live in parallel
calibration arrays indexed by the path number; the EDC16 module locates
them from the image itself (``ovtsuite.edc16.dsm16``), so every path is
listed whether or not the matched definition names it.

Each path has four error types (Max / Min / Sig / Npl) and one OBD code and
one VAG customer code per type, so a path can carry several codes. The one
setting that decides whether the path reacts is its **fault class**
(``DSM_ClaDfp_<name>_C``), an index into the class definitions
(``DSM_Class<N>Mil/SysLamp/Flc_C``). Class 0 has no definition in any A2L
-- the classes start at 1 -- and is what the stock calibrations themselves
give paths that are not used.

Disabling a code = setting its path's class to 0. Re-enabling restores the
path's previous class; the editor remembers it (and undo restores it
exactly), and a loaded stock binary can supply the original class for a
file that was already modified.

On an EDC15 session ``DtcTable`` hands back ``edc15.dtc15.DtcTable15``
(same interface, fbw fault paths).
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# OBD-II P-code -> standard description (bundled). Extend data/obd_pcodes.json.
_PDESC: Dict[str, str] = {}


def _load_pdesc() -> Dict[str, str]:
    global _PDESC
    if not _PDESC:
        fn = os.path.join(os.path.dirname(__file__), "data", "obd_pcodes.json")
        try:
            _PDESC = json.load(open(fn, encoding="utf-8"))
        except Exception:
            _PDESC = {}
    return _PDESC


def decode_sae(raw: Optional[int]) -> str:
    """Decode a 2-byte SAE J2012 code (as stored in DSM_CDCDfp_*) to text like
    'P0101'. Empty string for 0/None."""
    if not raw:
        return ""
    letter = "PCBU"[(raw >> 14) & 0x3]
    d1 = (raw >> 12) & 0x3
    return "%s%d%03X" % (letter, d1, raw & 0x0FFF)


def vag_text(code: Optional[int]) -> str:
    """A VAG fault number as a scan tool shows it: minimum five digits."""
    return "%05d" % code if code else ""


# The value a fault path's class is set to in order to disable the code.
DISABLED_CLASS = 0

LAMP_NAMES = {"mil": "MIL", "sys": "glow-plug lamp"}
_DRIVE = {1: "", 2: " (flashing)"}

TYPE_NAMES = ("Max", "Min", "Sig", "Npl")
TYPE_TEXT = {
    "Max": "above range / too high",
    "Min": "below range / too low",
    "Sig": "signal fault",
    "Npl": "implausible",
}


@dataclass
class ClassDef:
    """Behaviour of one DSM error class."""
    number: int
    mil: Optional[int] = None        # illuminate MIL
    syslamp: Optional[int] = None    # system lamp
    flc: Optional[int] = None        # trigger events to confirm the entry (lamp on)
    #: every DSM_Class<N><field>_C value the definition places, by field
    fields: Dict[str, int] = field(default_factory=dict)

    @property
    def lamps_text(self) -> str:
        """'MIL', 'glow-plug lamp (flashing)', 'MIL + glow-plug lamp',
        'no lamp'. The system lamp is the glow-plug lamp (see
        ``DtcTable.LAMPS``); 2 = flashing in both A2L verbal tables."""
        lamps = ["%s%s" % (name, _DRIVE.get(v, " (%d)" % v))
                 for name, v in (("MIL", self.mil or 0),
                                 ("glow-plug lamp", self.syslamp or 0)) if v]
        return " + ".join(lamps) if lamps else "no lamp"

    @property
    def text(self) -> str:
        """'class 11: MIL' / 'class 2: glow-plug lamp' / 'class 1: no lamp'."""
        return "class %d: %s" % (self.number, self.lamps_text)

    @property
    def silent(self) -> bool:
        return not (self.mil or self.syslamp)

    @property
    def stores(self) -> bool:
        return bool(self.flc)


@dataclass
class FaultType:
    """One error type of a path, with its two codes."""
    kind: str                        # Max / Min / Sig / Npl
    cdc_raw: int                     # OBD word as stored (0 = none)
    cdk_raw: int                     # customer code as stored
    pcode: str                       # OBD code text ('' = none)
    vag: Optional[int]               # customer code (None = placeholder)
    vag_pcode: str                   # the P-code the customer code stands for

    @property
    def text(self) -> str:
        return TYPE_TEXT.get(self.kind, self.kind)

    @property
    def used(self) -> bool:
        return bool(self.pcode or self.vag)


@dataclass
class FaultPath:
    """One fault path: its class selector and its codes per error type."""
    name: str                        # e.g. "EGRVlv" (from Dfp_EGRVlv); "#12" unnamed
    cla: object = None               # the ClaDfp Entity (class selector)
    parts: Dict[str, object] = field(default_factory=dict)
    description: str = ""            # the definition's text, when it has one
    index: int = -1                  # DSM path number (array index)
    own: Optional[int] = None        # the path's DSM number (placeholder code)
    name_source: str = ""            # 'definition' | 'catalogue' | ''
    fmk: int = 0                     # hiding-condition selector
    types: List[FaultType] = field(default_factory=list)
    _orig_class: Optional[int] = None   # remembered before disabling
    #: the class a path had before a lamp was switched off, by lamp
    _lamp_class: Dict[str, int] = field(default_factory=dict)

    # -- codes -----------------------------------------------------------
    @property
    def named(self) -> bool:
        return not self.name.startswith("#")

    @property
    def pcodes(self) -> List[str]:
        """Every OBD code the path can set, in type order, without repeats."""
        return list(dict.fromkeys(t.pcode for t in self.types if t.pcode))

    @property
    def vag_codes(self) -> List[int]:
        """Every VAG customer code the path can set, in type order."""
        return list(dict.fromkeys(t.vag for t in self.types if t.vag))

    @property
    def stored_codes(self) -> List[int]:
        """Every customer-code word stored for the path, placeholders too --
        what an ECU fault-memory read is matched against."""
        return list(dict.fromkeys(t.cdk_raw for t in self.types if t.cdk_raw))

    @property
    def carb_code(self) -> Optional[int]:
        for t in self.types:
            if t.cdc_raw:
                return t.cdc_raw
        return None

    @property
    def pcode(self) -> str:
        """The path's first OBD-II code, e.g. 'P0101' ('' if none)."""
        p = self.pcodes
        return p[0] if p else ""

    @property
    def vag_code(self) -> Optional[int]:
        v = self.vag_codes
        return v[0] if v else None

    @property
    def vag_str(self) -> str:
        """The VAG fault number(s) as a scan tool shows them, '-' if none."""
        v = self.vag_codes
        return ", ".join(vag_text(c) for c in v) if v else "-"

    @property
    def pcode_str(self) -> str:
        p = self.pcodes
        return ", ".join(p) if p else "-"

    # -- text ------------------------------------------------------------
    def code_desc(self) -> str:
        """Human-readable fault description: the decoded fault-path name,
        else the standard OBD-II text for its first code, else the
        definition's own text."""
        if self.named:
            try:
                from .signal_names import fault_name
                hn = fault_name(self.name)
                if hn and hn.lower() != self.name.lower():
                    return hn
            except Exception:
                pass
        for pc in self.pcodes:
            d = _load_pdesc().get(pc)
            if d:
                return d
        if self.description:
            segs = [s.strip() for s in self.description.split("|")
                    if s.strip() and not s.strip().startswith("A2L:")]
            if segs:
                try:
                    from .signal_names import translate_german
                    return translate_german(segs[0])
                except Exception:
                    return segs[0]
        if self.named:
            return self.name
        return "fault path %d (no name in any definition)" % self.index

    def type_lines(self) -> List[str]:
        """One line per error type that carries a code."""
        out = []
        pd = _load_pdesc()
        for t in self.types:
            if not t.used:
                continue
            bits = []
            if t.vag:
                bits.append("VAG %s%s" % (vag_text(t.vag),
                                          " (%s)" % t.vag_pcode if t.vag_pcode
                                          and t.vag_pcode != t.pcode else ""))
            if t.pcode:
                d = pd.get(t.pcode)
                bits.append("OBD %s%s" % (t.pcode, " - %s" % d if d else ""))
            out.append("%s (%s): %s" % (t.kind, t.text, ";  ".join(bits)))
        return out

    def search_text(self) -> str:
        """Everything a user might type to find this path, lower-cased:
        names, descriptions, every code in every form a scan tool or a
        tuner would write it (00257, 257, P0101, 0x0101)."""
        bits = [self.name, self.dfp, self.code_desc(), self.description]
        for t in self.types:
            if t.pcode:
                bits.append(t.pcode)
            if t.vag:
                bits += [vag_text(t.vag), str(t.vag), "0x%04x" % t.vag,
                         t.vag_pcode]
            if t.used:
                bits.append(t.text)
        return " ".join(b for b in bits if b).lower()

    @property
    def dfp(self) -> str:
        return ("Dfp_" + self.name) if self.named else \
            "fault path %d" % self.index

    @property
    def current_class(self) -> Optional[int]:
        if self.cla is None:
            return None
        return int(round(self.cla.cell(0, 0)))


def _class_entity(session, name: str, addr: int):
    """The fault-class byte as an editable entity, for a path the matched
    definition does not place."""
    from .model import Entity
    return Entity(name="DSM_ClaDfp_%s_C" % name, kind="constant", tier=0,
                  categories=["Fault codes"],
                  description="Fault class of fault path %s (located from "
                              "the image's DSM tables)" % name,
                  units="", addr=addr, rows=1, cols=1, bits=8, signed=False,
                  equation="X", source="structure", binfile=session.binfile)


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
        self.layout = None
        self.note = ""
        self._build()

    def _build(self):
        ents = list(self.session.entities)
        for e in ents:
            nm = e.name
            if not nm.startswith("DSM_Class") or not nm.endswith("_C"):
                continue
            rest = nm[9:-2]
            num = ""
            while rest and rest[0].isdigit():
                num, rest = num + rest[0], rest[1:]
            if not num:
                continue
            cd = self.classes.setdefault(int(num), ClassDef(int(num)))
            val = int(round(e.cell(0, 0)))
            cd.fields[rest] = val
            if rest == "Mil":
                cd.mil = val
            elif rest == "SysLamp":
                cd.syslamp = val
            elif rest == "Flc":
                cd.flc = val
        try:
            from .edc16 import dsm16
        except Exception:
            self.note = "The EDC16 module is not installed."
            return
        data = bytes(self.session.binfile.data)
        lay = dsm16.locate(data, ents)
        if lay is None:
            self.note = ("No fault-path tables were found in this image (no "
                         "calibration pointer table, and the definition does "
                         "not place the fault classes).")
            return
        self.layout = lay
        raw = dsm16.read_paths(data, lay)
        dsm16.name_paths(raw, lay, ents)
        by_name = {e.name: e for e in ents if e.name.startswith("DSM_")}
        for p in raw:
            name = p.name or "#%d" % p.index
            fp = FaultPath(name, index=p.index, own=p.own,
                           name_source=p.name_source, fmk=p.fmk)
            fp.types = [FaultType(t.kind, t.cdc, t.cdk, t.pcode, t.vag,
                                  t.vag_pcode) for t in p.types]
            addr = lay.cla + p.index
            e = by_name.get("DSM_ClaDfp_%s_C" % p.name) if p.name else None
            if e is not None and e.addr == addr:
                fp.cla = e
                d = e.description.split("|")
                fp.description = (d[1].strip() if len(d) > 1
                                  else e.description)[:120]
            else:
                fp.cla = _class_entity(self.session, name, addr)
            for kind in ("CDC", "CDK", "FMK", "Inh", "Env", "Typ"):
                pe = by_name.get("DSM_%sDfp_%s_C" % (kind, p.name)) \
                    if p.name else None
                if pe is not None:
                    fp.parts[kind] = pe
            fp.parts["Cla"] = fp.cla
            self.paths[name] = fp
        if lay.definition_wrong and not any(
                fp.name_source == "definition" for fp in self.paths.values()):
            self.note = ("The matched definition's fault-path names do not "
                         "sit on this image's paths (%d of them contradict the "
                         "paths' own DSM numbers), so names come from the "
                         "reference catalogue instead." % lay.definition_wrong)

    # -- queries --------------------------------------------------------
    def class_of(self, fp: FaultPath) -> Optional[ClassDef]:
        c = fp.current_class
        if c is None:
            return None
        return self.classes.get(c)

    def is_enabled(self, fp: FaultPath) -> bool:
        """A path is 'enabled' when it has a class. A class that lights no
        lamp still enters the fault in memory -- class 1, the commonest,
        has no MIL, no system lamp and a confirmation count of 0, and its
        faults (the A/C compressor's, for one) are what a scan tool reads --
        so only class 0 is off."""
        c = fp.current_class
        return c is not None and c != DISABLED_CLASS

    def lights_mil(self, fp: FaultPath) -> bool:
        cd = self.class_of(fp)
        return bool(cd and cd.mil)

    def sorted_paths(self) -> List[FaultPath]:
        return sorted(self.paths.values(),
                      key=lambda fp: (not fp.named, fp.name.lower()
                                      if fp.named else "", fp.index))

    def find_code(self, code: int) -> List[FaultPath]:
        """Paths whose stored customer codes include ``code`` (a number read
        from the ECU's fault memory)."""
        return [fp for fp in self.paths.values() if code in fp.stored_codes]

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

    def set_many(self, fps, enabled: bool, label: str = ""):
        """Enable or disable several paths as ONE undo step."""
        with self.session.transaction(
                label or ("enable %d fault paths" if enabled
                          else "disable %d fault paths") % len(fps)):
            for fp in fps:
                if self.is_enabled(fp) != enabled:
                    self.set_enabled(fp, enabled)

    def counterpart(self, other: "DtcTable", key: str):
        """Path ``key`` of ``other`` -- paired by name; an unnamed path
        (``#N``, by number) only when both images have the same layout,
        else its number is another fault's. None when there is none."""
        fp, src = self.paths.get(key), other.paths.get(key)
        if fp is None or src is None:
            return None
        if not getattr(fp, "named", True) or not getattr(src, "named", True):
            a, b = getattr(self, "layout", None), getattr(other, "layout", None)
            if a is None or b is None or getattr(a, "n", None) != getattr(b, "n", None):
                return None
        return src

    def restore_from(self, other: "DtcTable"):
        """Adopt each path's class from a stock DtcTable (bulk re-enable),
        as ONE undo step however many paths change. Paths are paired by
        name, or by number when neither side has a name and the two images
        share a layout (:meth:`counterpart`)."""
        with self.session.transaction("restore DTC classes from stock"):
            for name, fp in self.paths.items():
                src = self.counterpart(other, name)
                if src is None or fp.cla is None:
                    continue
                want = src.current_class
                if want is not None and want != fp.current_class:
                    self.session.edit_cells(fp.cla, [(0, 0, float(want))])

    # -- the warning lamps, separately from on/off ------------------------
    #
    # A class says how each lamp is driven: ``DSM_Class<N>Mil_C`` and
    # ``DSM_Class<N>SysLamp_C``, both "0 OFF, 1 ON, 2 BLINK" (the A2L's
    # verbal tables ``Mil`` / ``SysLamp``). The system lamp IS the glow-plug
    # lamp on these engines: the A2L's ``ErLpCD_OrdGlwLamp`` is the "message
    # from the error-lamp handler to the glow-time control; drives the glow
    # lamp on system-lamp faults" (present in the P350, P379, P447, P475 and
    # P216 A2Ls). So a lamp is switched for one path by moving the path to
    # another class.
    LAMPS = ("mil", "sys")
    #: fields that say nothing about how the fault is stored
    _LAMP_FIELDS = ("Mil", "SysLamp", "Readiness")

    @staticmethod
    def _lamp(cd: ClassDef, lamp: str) -> int:
        return (cd.mil if lamp == "mil" else cd.syslamp) or 0

    def lights(self, fp: FaultPath, lamp: str) -> bool:
        cd = self.class_of(fp)
        return bool(cd and self._lamp(cd, lamp))

    def lights_glow(self, fp: FaultPath) -> bool:
        return self.lights(fp, "sys")

    def _closest_class(self, cd: ClassDef, mil: bool,
                       lamp: str = "mil") -> Optional[int]:
        """The defined class that differs from ``cd`` in ``lamp`` (on/off
        as asked) and as little else as possible: the other lamp exactly
        as it is first, then the most equal storage fields (entry/heal
        counters, driving cycles, priority...), then the lowest number.
        None when there is no such class."""
        other = "sys" if lamp == "mil" else "mil"
        # OBD readiness goes with the MIL: it is compared when the glow lamp
        # is switched (so class 12 -> 11, its MIL twin), not for the MIL
        skip = self._LAMP_FIELDS if lamp == "mil" else ("Mil", "SysLamp")
        best, key = None, None
        for n, c in self.classes.items():
            if n == DISABLED_CLASS or bool(self._lamp(c, lamp)) != mil:
                continue
            same = sum(1 for f, v in cd.fields.items()
                       if f not in skip and c.fields.get(f) == v)
            k = (self._lamp(c, other) == self._lamp(cd, other),
                 bool(self._lamp(c, other)) == bool(self._lamp(cd, other)),
                 same, -n)
            if key is None or k > key:
                best, key = n, k
        return best

    def lamp_target(self, fp: FaultPath, lamp: str, on: bool) -> Optional[int]:
        """The class :meth:`set_lamp` would give ``fp`` -- None when it
        cannot (path off, class unknown, no such class)."""
        if not self.is_enabled(fp):
            return None
        cd = self.class_of(fp)
        if cd is None or bool(self._lamp(cd, lamp)) == on:
            return None
        if on:
            # the class it had -- only if it agrees with the other lamp as
            # it is now (switched off since, it must stay off)
            back = fp._lamp_class.get(lamp)
            bc = self.classes.get(back) if back is not None else None
            other = "sys" if lamp == "mil" else "mil"
            if bc is not None and self._lamp(bc, lamp) and \
                    bool(self._lamp(bc, other)) == bool(self._lamp(cd, other)):
                return back
        return self._closest_class(cd, on, lamp)

    def can_set_lamp(self, fp: FaultPath, lamp: str) -> bool:
        cd = self.class_of(fp)
        return (cd is not None and self.is_enabled(fp)
                and self.lamp_target(fp, lamp,
                                     not self._lamp(cd, lamp)) is not None)

    def lamp_note(self, fp: FaultPath, lamp: str) -> str:
        """Why the lamp cannot be switched on its own, '' when it can."""
        if not self.is_enabled(fp):
            return "The fault is off."
        if self.class_of(fp) is None:
            return ("Class %s has no definition in this file's definition, so "
                    "which lamps it lights is unknown." % fp.current_class)
        if not self.can_set_lamp(fp, lamp):
            return "No other fault class does that."
        return ""

    def set_lamp(self, fp: FaultPath, lamp: str, on: bool) -> Optional[int]:
        """Keep the fault, but light ``lamp`` or not, by moving the path to
        the closest class that does. The class it had is remembered, so
        switching the lamp back restores it exactly. Returns the new
        class, or None when nothing changed."""
        target = self.lamp_target(fp, lamp, on)
        if target is None:
            return None
        if not on:
            fp._lamp_class[lamp] = fp.current_class
        self.session.edit_cells(fp.cla, [(0, 0, float(target))])
        return target

    def set_lamp_many(self, fps, lamp: str, on: bool):
        """:meth:`set_lamp` on several paths as ONE undo step."""
        with self.session.transaction("%s the %s on %d fault paths" % (
                "light" if on else "no", LAMP_NAMES[lamp], len(fps))):
            for fp in fps:
                self.set_lamp(fp, lamp, on)

    # the MIL by name (kept: tests, the AI tools and the CLI use these)
    def mil_target(self, fp, mil):
        return self.lamp_target(fp, "mil", mil)

    def can_set_mil(self, fp):
        return self.can_set_lamp(fp, "mil")

    def mil_note(self, fp):
        return self.lamp_note(fp, "mil")

    def set_mil(self, fp, mil):
        return self.set_lamp(fp, "mil", mil)

    def set_mil_many(self, fps, mil):
        self.set_lamp_many(fps, "mil", mil)

    def set_class(self, fp: FaultPath, number: int):
        """Give the path a class of the user's choosing."""
        if fp.cla is None:
            return
        cur = fp.current_class
        if number == DISABLED_CLASS and cur != DISABLED_CLASS:
            fp._orig_class = cur
        fp._lamp_class.clear()          # a class chosen outright: no way back
        self.session.edit_cells(fp.cla, [(0, 0, float(number))])

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

    def source_text(self) -> str:
        """Where the tables were found and how the codes read, in words."""
        lay = self.layout
        if lay is None:
            return self.note
        named = sum(1 for fp in self.paths.values() if fp.named)
        enc = ("VAG 5-digit numbers" if lay.encoding == "vag"
               else "SAE code words")
        return ("%d fault paths, found from %s; VAG codes stored as %s; "
                "%d named." % (lay.n, lay.source, enc, named))
