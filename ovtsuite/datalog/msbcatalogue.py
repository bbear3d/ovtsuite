# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring-block names by ECU identity, for when no binary is open.

:class:`~ovtsuite.datalog.channels.ChannelNamer` names logged cells from the
calibration in front of it. Without one, the logger still knows which
software it is talking to -- the ECU says so on connect -- so the names can
come from a catalogue built offline from factory binaries
(``tools/build_msblock_catalogue.py``), filed under the identity fields the
ECU serves:

* EDC16 (KWP2000 ``1A 9B``): the VAG part number and the 4-character software
  version. In the image they are the PART and VER fields of the fixed-width
  identification record (``fileinfo.ident16``).
* EDC15 (KW1281 identification blocks): the active dataset's ident string,
  e.g. ``038906012GN 1,9l R4 EDC  SG  4308`` -- part number, gearbox token
  (SG/AG) and software version. Each dataset of an image is its own entry, so
  the ident picks the dataset the ECU is coded to. KW1281 sends the text in
  fixed-length blocks that may split a word, so it is compared with
  whitespace removed.

A lookup takes the first of:

1. ``exact``   -- the same part number and software version (EDC15: the same
   whole ident);
2. ``part``    -- the same part number (EDC15: and gearbox token, when the
   catalogue has that one) on another software version. Where the part's
   software versions differ, the map most of them share is used;
3. ``generic`` -- the platform's common layout: each cell named only when
   most part numbers that populate its group agree on it.

A binary always wins over the catalogue -- the catalogue describes factory
calibrations, and an edited group map only shows in the file.

Each platform's catalogue ships with its module (``<platform>_msblocks.json.gz``
in the module's data directory)::

    {"format": 2, "platform": "edc16",
     "labels":  [[label, signal, unit], ...],        # shared, deduplicated
     "maps":    [{"<group>": [label index | -1, ...]}, ...],   # -1 = unnamed
     "ecus":    {"<key>": {"map": i, "part": "...", "sw": "...", "gear": "..",
                           "family": "...", "sources": [...]}},
     "generic": i}                                   # map index, or -1

Only populated groups are stored; a group's list length is its slot count.
"""
import gzip
import json
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from .logfile import Channel

FORMAT = 2

#: Logger controller -> catalogue platform.
PLATFORMS = {"EDC16": "edc16", "EDC15": "edc15"}

#: Generic layout: a group is in it when at least this share of part numbers
#: populate it, and a cell is named when at least this share of those agree.
GENERIC_GROUP_SHARE = 1.0 / 3.0
GENERIC_CELL_SHARE = 0.5

_EDC16_ID = re.compile(r"\s*(\S+)\s+SW\s+(\S+)")
_GEAR = re.compile(r"(?:EDC|TDI)([A-Z]G)")   # "EDC SG", V6 "TDI SG"          # on whitespace-free ident text


def file_name(platform: str) -> str:
    return "%s_msblocks.json.gz" % platform


# ------------------------------------------------------------------ keys
def edc16_key(part: str, sw_version: str) -> str:
    return "%s %s" % (part.strip(), sw_version.strip())


def edc15_key(ident: str) -> str:
    """A dataset ident with its whitespace runs collapsed (as stored)."""
    return " ".join(ident.split())


def _squash(text: str) -> str:
    return "".join((text or "").split())


def key_fields(platform: str, key: str) -> Dict[str, str]:
    """``part`` / ``sw`` / ``gear`` of a catalogue key."""
    if platform == "edc16":
        part, _, sw = key.partition(" ")
        return {"part": part, "sw": sw, "gear": ""}
    toks = key.split()
    gm = _GEAR.search(_squash(key))
    return {"part": toks[0] if toks else "", "sw": toks[-1] if len(toks) > 1 else "",
            "gear": gm.group(1) if gm else ""}


def edc16_identity(identity: str) -> Optional[Tuple[str, str]]:
    """(part, sw) from a connected ECU's identity string, as
    ``flash.diag.format_identity`` writes it ("PART  SW VER  text"). None when
    the record was not a 9B identification (no "SW" field)."""
    m = _EDC16_ID.match(identity or "")
    return (m.group(1), m.group(2)) if m else None


# ------------------------------------------------------------- catalogue
class Catalogue:
    def __init__(self, doc: dict):
        if int(doc.get("format", 0)) != FORMAT:
            raise ValueError("unsupported measuring-block catalogue format %r"
                             % doc.get("format"))
        self.platform = doc.get("platform", "")
        self.labels: List[list] = doc.get("labels", [])
        self.maps: List[Dict[str, List[int]]] = doc.get("maps", [])
        self.ecus: Dict[str, dict] = doc.get("ecus", {})
        self.generic = int(doc.get("generic", -1))
        self._squashed = {_squash(k): k for k in self.ecus}
        self._by_part: Dict[str, List[str]] = defaultdict(list)
        for k, e in self.ecus.items():
            self._by_part[e.get("part", "")].append(k)

    # ---- matching ----
    def find(self, identity: str) -> Tuple[Optional[str], str]:
        """``(key, how)`` for a reported identity: ``how`` is "exact",
        "part" or "generic" (key None, and None again when there is no
        generic layout)."""
        if self.platform == "edc16":
            got = edc16_identity(identity)
            if got:
                key = edc16_key(*got)
                if key in self.ecus:
                    return key, "exact"
                if got[0] in self._by_part:
                    return self._representative(self._by_part[got[0]]), "part"
        elif identity:
            have = _squash(identity)
            # the ident blocks start with the dataset ident; coding and
            # workshop blocks may follow, so a key is a prefix. Every key ends
            # in its software version, so of two matching keys only the
            # longer one is right.
            hits = [sq for sq in self._squashed if sq and have.startswith(sq)]
            if hits:
                return self._squashed[max(hits, key=len)], "exact"
            part = self._edc15_part(have)
            if part:
                keys = self._by_part[part]
                gm = _GEAR.search(have)
                same = [k for k in keys if gm and self.ecus[k].get("gear") == gm.group(1)]
                return self._representative(same or keys), "part"
        return None, "generic"

    def _edc15_part(self, have: str) -> str:
        """The longest catalogued part number the ident starts with. The ident
        goes on with the displacement ("1,9l"), so a part followed by another
        letter is a longer, different part number."""
        best = ""
        for part in self._by_part:
            if part and have.startswith(part) and len(part) > len(best):
                nxt = have[len(part):len(part) + 1]
                if not (nxt.isalpha() and nxt.isupper()):
                    best = part
        return best

    def _representative(self, keys: List[str]) -> str:
        """Of one part's entries, the one whose map most of them share; then
        the one naming most cells; then the highest key (deterministic)."""
        votes = Counter(self.ecus[k]["map"] for k in keys)

        def named(k):
            return sum(1 for cells in self.maps[self.ecus[k]["map"]].values()
                       for i in cells if i >= 0)
        return max(keys, key=lambda k: (votes[self.ecus[k]["map"]], named(k), k))

    # ---- namers ----
    def _groups(self, m: int) -> Dict[int, List[Optional[tuple]]]:
        raw = {int(g): [tuple(self.labels[i]) if i >= 0 else None for i in cells]
               for g, cells in self.maps[m].items()}
        return fold_extension_rows(raw) if self.platform == "edc16" else raw

    def namer(self, key: Optional[str], how: str = "exact",
              reported: str = "") -> Optional["CatalogueNamer"]:
        if key is None:
            if self.generic < 0:
                return None
            return CatalogueNamer("", self.platform, self._groups(self.generic),
                                  match="generic", reported=reported)
        ent = self.ecus[key]
        return CatalogueNamer(key, self.platform, self._groups(ent["map"]),
                              ent.get("family", ""), match=how, reported=reported)


def fold_extension_rows(groups: Dict[int, List[Optional[tuple]]]
                        ) -> Dict[int, List[Optional[tuple]]]:
    """EDC16: put map rows 128..254 where the ECU sends them -- cells 5-8 of
    group ``row - 127`` (:mod:`ovtsuite.msblock`). Catalogues built before
    that was known file those rows as groups of their own; a catalogue built
    since already has 8-cell groups and passes through unchanged."""
    from ..msblock import EXTENSION_OFFSET, LAST_GROUP, SLOTS_PER_ROW
    out = {g: list(c) for g, c in groups.items() if g <= LAST_GROUP}
    for g, cells in groups.items():
        if g <= LAST_GROUP:
            continue
        base = g - EXTENSION_OFFSET
        if base < 1:
            continue
        row = list(out.get(base, []))
        if len(row) > SLOTS_PER_ROW:
            continue                       # already folded (new-format entry)
        row += [None] * (SLOTS_PER_ROW - len(row))
        out[base] = row + list(cells[:SLOTS_PER_ROW])
    return out


_cache: Dict[str, Catalogue] = {}


def load(platform: str) -> Optional[Catalogue]:
    """The installed catalogue for ``platform`` (None when its module, or the
    file, is not installed). Only a catalogue that loaded is cached, so one
    installed while the app runs (a module added in-app) is found on the next
    lookup rather than after a restart."""
    if platform in _cache:
        return _cache[platform]
    cat = None
    try:
        from .. import datafiles
        p = datafiles.path(file_name(platform))
        if p:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                cat = Catalogue(json.load(fh))
    except Exception:
        cat = None
    if cat is not None:
        _cache[platform] = cat
    return cat


def namer_for(ecu: str, identity: str = "") -> Optional["CatalogueNamer"]:
    """A namer for the controller ``ecu`` ("EDC16", "EDC15"): the best
    catalogue match for the ``identity`` it reported, or -- with no identity
    yet, or no entry for it -- the platform's generic layout. None only when
    the controller has no catalogue installed."""
    platform = PLATFORMS.get(ecu)
    cat = load(platform) if platform else None
    if cat is None:
        return None
    key, how = cat.find(identity or "")
    return cat.namer(key, how, identity or "")


# ----------------------------------------------------------------- namer
class CatalogueNamer:
    """:class:`~ovtsuite.datalog.channels.ChannelNamer`'s interface over one
    catalogue entry (or the generic layout, ``match == "generic"``)."""
    session = None

    def __init__(self, key: str, platform: str,
                 groups: Dict[int, List[Optional[tuple]]], family: str = "",
                 match: str = "exact", reported: str = ""):
        self.key = key
        self.platform = platform
        self.family = family
        self.match = match
        self.reported = reported
        self._groups = groups

    @property
    def fields(self) -> Dict[str, str]:
        return key_fields(self.platform, self.key) if self.key else {}

    def describe(self) -> str:
        """Where the names come from, for the logger's hint line."""
        plat = self.platform.upper()
        if self.match == "exact":
            return "the factory calibration of %s" % self.key
        if self.match == "part":
            f = self.fields
            return ("the factory calibration of %s (software %s; that exact software "
                    "is not in the catalogue)" % (f.get("part"), f.get("sw")))
        return ("the generic %s layout (%s)"
                % (plat, "no catalogue entry for this part number" if self.reported
                   else "connect to match the ECU's own part number"))

    @property
    def available(self) -> bool:
        return bool(self._groups)

    def slots(self, group: int) -> int:
        cells = self._groups.get(group)
        return len(cells) if cells else 4

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        cells = self._groups.get(group)
        if cells is None or field >= len(cells) or cells[field] is None:
            return ("Group %d / Cell %d" % (group, field + 1), "", "")
        label, signal, unit = cells[field]
        if not label.startswith("Group "):
            from ..signal_names import sentence_case
            label = sentence_case(label)
        return (label, signal, unit)

    def cell_fields(self, group: int) -> List[int]:
        """The cells of ``group`` that carry a signal (see
        ``ChannelNamer.cell_fields``); an empty cell is stored as None."""
        cells = self._groups.get(group)
        if not cells:
            return list(range(self.slots(group)))
        return [f for f, c in enumerate(cells) if c is not None]

    def channel(self, group: int, field: int) -> Channel:
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def populated_groups(self) -> List[int]:
        return sorted(self._groups)

    def group_summary(self, group: int) -> str:
        cells = self._groups.get(group) or []
        return ", ".join(self.resolve(group, f)[0].split(" (")[0]
                         for f, c in enumerate(cells)
                         if c is not None and not c[0].startswith("Group "))

    def find_signal(self, name: str) -> Optional[Tuple[int, int]]:
        for grp in sorted(self._groups):
            for f in range(self.slots(grp)):
                if self.resolve(grp, f)[1] == name:
                    return grp, f
        return None


# --------------------------------------------------------------- building
def entries_from_session(session) -> List[Tuple[str, str, Dict[int, list]]]:
    """``[(key, family, {group: [(label, signal, unit) | None, ...]})]`` for
    one loaded image: one entry for an EDC16 image, one per dataset for an
    EDC15 one. ``[]`` when the image carries no structured identification or
    no measuring-block map -- nothing is keyed on a guessed identity."""
    from .channels import ChannelNamer
    out = []
    family = str(getattr(session, "family", "") or "")
    if getattr(session, "is_edc15", False):
        for ds in getattr(session, "datasets", []) or []:
            ident = (getattr(ds, "ident", "") or "").strip()
            if not ident:
                continue
            n = ChannelNamer(session, dataset=ds.label)
            if n.available:
                out.append((edc15_key(ident), family, _cells(n)))
        return out
    from .. import fileinfo
    ident = fileinfo.ident16(bytes(session.binfile.data))
    if "ident_addr" not in ident or not ident.get("sw_version"):
        return out              # no identification record: no key to file it under
    n = ChannelNamer(session)
    if n.available:
        out.append((edc16_key(ident["part"], ident["sw_version"]), family, _cells(n)))
    return out


def _cells(namer) -> Dict[int, list]:
    groups = {}
    for g in namer.populated_groups():
        row = []
        for f in range(namer.slots(g)):
            label, signal, unit = namer.resolve(g, f)
            generic = label == "Group %d / Cell %d" % (g, f + 1)
            row.append(None if generic else (label, signal, unit))
        groups[int(g)] = row
    return groups


class Builder:
    """Collects entries and writes a catalogue. Two sources under one key
    that disagree on any cell make that key ambiguous, and it is left out (a
    lookup for it then falls to the part's other software, or the generic
    layout)."""

    def __init__(self, platform: str):
        self.platform = platform
        self._entries: Dict[str, dict] = {}
        self.conflicts: Dict[str, List[str]] = {}

    def add(self, key: str, family: str, groups: Dict[int, list], source: str):
        if key in self.conflicts:
            self.conflicts[key].append(source)
            return
        have = self._entries.get(key)
        if have is None:
            self._entries[key] = {"family": family, "groups": groups, "sources": [source]}
        elif have["groups"] == groups:
            if source not in have["sources"]:
                have["sources"].append(source)
        else:
            self.conflicts[key] = have["sources"] + [source]
            del self._entries[key]

    def __len__(self):
        return len(self._entries)

    def _generic(self) -> Dict[int, list]:
        """One vote per part number (its representative software, chosen as
        a lookup would) for every cell."""
        by_part: Dict[str, List[str]] = defaultdict(list)
        for k in self._entries:
            by_part[key_fields(self.platform, k)["part"]].append(k)
        reps = []
        for keys in by_part.values():
            sigs = Counter(json.dumps(self._entries[k]["groups"], sort_keys=True) for k in keys)
            best = max(keys, key=lambda k: (sigs[json.dumps(self._entries[k]["groups"],
                                                            sort_keys=True)], k))
            reps.append(self._entries[best]["groups"])
        out: Dict[int, list] = {}
        if not reps:
            return out
        for g in sorted({g for r in reps for g in r}):
            rows = [r[g] for r in reps if g in r]
            if len(rows) < GENERIC_GROUP_SHARE * len(reps):
                continue
            width = Counter(len(r) for r in rows).most_common(1)[0][0]
            row = []
            for f in range(width):
                votes = Counter()
                for r in rows:
                    c = r[f] if f < len(r) else None
                    if c is not None and (c[1] or self.platform == "edc15"):
                        # EDC15 signals are per-image descriptor numbers:
                        # the label is what means the same thing everywhere
                        votes[c[0] if self.platform == "edc15" else tuple(c)] += 1
                cell = None
                if votes:
                    top, n = votes.most_common(1)[0]
                    if n >= GENERIC_CELL_SHARE * len(rows):
                        if self.platform == "edc15":
                            sigs = {r[f][1] for r in rows
                                    if f < len(r) and r[f] is not None and r[f][0] == top}
                            sig = next(iter(sigs)) if len(sigs) == 1 else ""
                            cell = (top, "" if sig.startswith("xcwGrp") else sig, "")
                        else:
                            cell = top
                row.append(cell)
            if any(c is not None for c in row):
                out[g] = row
        return out

    def document(self) -> dict:
        labels: List[tuple] = []
        lidx: Dict[tuple, int] = {}
        maps: List[Dict[str, List[int]]] = []
        midx: Dict[str, int] = {}

        def add_map(groups) -> int:
            m = {}
            for g in sorted(groups):
                row = []
                for c in groups[g]:
                    if c is None:
                        row.append(-1)
                        continue
                    c = tuple(c)
                    if c not in lidx:
                        lidx[c] = len(labels)
                        labels.append(c)
                    row.append(lidx[c])
                m[str(g)] = row
            sig = json.dumps(m, sort_keys=True)
            if sig not in midx:
                midx[sig] = len(maps)
                maps.append(m)
            return midx[sig]

        ecus = {}
        for key in sorted(self._entries):
            ent = self._entries[key]
            rec = {"map": add_map(ent["groups"]), "family": ent["family"],
                   "sources": sorted(ent["sources"])}
            rec.update(key_fields(self.platform, key))
            ecus[key] = rec
        gen = self._generic()
        generic = add_map(gen) if gen else -1       # may add labels: first
        return {"format": FORMAT, "platform": self.platform,
                "labels": [list(l) for l in labels], "maps": maps, "ecus": ecus,
                "generic": generic}

    def write(self, path: str) -> dict:
        doc = self.document()
        with gzip.GzipFile(path, "wb", mtime=0) as gz:     # reproducible bytes
            gz.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":"),
                                sort_keys=True).encode("utf-8"))
        return doc
