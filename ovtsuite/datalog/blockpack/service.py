# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Between a binary and the planner: what the dialog, the logger and the
measuring-block window ask. No Qt.

* :class:`Builder` -- for one open binary: its presets (shipped and yours),
  what each reads today, :meth:`~Builder.plan`, :meth:`~Builder.generate`
  (write + record, one undo step) and :meth:`~Builder.remove`.
* :func:`optimised_for` -- the logger's question: does this preset have an
  optimised layout here, and which groups does it read?
* :func:`report_key` -- the identity a connected ECU reported, in the form
  the per-user store files manifests under.
* :func:`logging_tags` -- which cells of a binary were built for which
  presets (the measuring-block window's tags).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import manifest as _m
from . import requirements as _q
from .model import Allocation, PlanItem
from .namer import LayoutNamer, StockNamer
from .planner import allocate, source_picker
from .provider import build_layout, provider_for

#: about how long one group read takes, for the rate estimate when nothing
#: was measured: the P475 LQ over TP 2.0 (Reference/Logs/ecu-lq-stock_*),
#: an EDC15 over KW1281 at 9600 baud (J2534EDC15Logging.txt)
PER_GROUP_MS = {"edc16": 32.0, "edc15": 340.0}

#: platform -> the logger's controller name (``ConnectionBar.ecu()``)
ECU_OF_PLATFORM = {"edc16": "EDC16", "edc15": "EDC15"}
PLATFORM_OF_ECU = {v: k for k, v in ECU_OF_PLATFORM.items()}

RECIPE, USER = "recipe", "user"
#: a record of blocks built for a preset that is no longer offered (one of
#: yours deleted since): listed so its cells can still be removed
ORPHAN = "orphan"


def estimate_hz(platform: str, groups: int, per_group_ms: Optional[float] = None) -> float:
    ms = per_group_ms or PER_GROUP_MS.get(platform, 100.0)
    return 1000.0 / (ms * max(1, groups))


def recipes_for_ecu(ecu: str, namer) -> list:
    """The shipped presets the logger offers for ``ecu`` (as its ``_recipes``)."""
    from ...logtune import roles as _r
    if ecu == "EDC16":
        return list(_r.RECIPES)
    if ecu == "EDC15":
        return _r.recipes_for(namer, tuple(_r.RECIPES) + tuple(_r.EDC15_RECIPES))
    return []


@dataclass
class PresetEntry:
    key: str                      # recipe key, or "user:<name>"
    title: str
    kind: str                     # RECIPE / USER
    reqs: list
    content: str
    obj: Any = None               # the Recipe or the UserPreset

    @property
    def is_user(self) -> bool:
        return self.kind == USER

    @property
    def is_orphan(self) -> bool:
        return self.kind == ORPHAN


def entries_for(ecu: str, namer, prefs=None) -> List[PresetEntry]:
    out = []
    for r in recipes_for_ecu(ecu, namer):
        reqs = _q.from_recipe(r)
        out.append(PresetEntry(r.key, r.title, RECIPE, reqs, _q.content_key(reqs), r))
    if prefs is not None:
        from .. import userpresets as _up
        try:
            mine = _up.load(prefs, ecu)
        except Exception:
            mine = []
        for p in mine:
            reqs = _q.from_user_preset(p)
            out.append(PresetEntry(p.data, p.name, USER, reqs, _q.content_key(reqs), p))
    return out


def baseline_of(entry: PresetEntry, namer, avoid: Sequence[int] = ()) -> List[int]:
    if entry.is_user:
        return _q.user_baseline(namer, entry.obj, avoid)
    return _q.recipe_baseline(namer, entry.obj, avoid)


class Builder:
    """The optimised-block work on one open binary.

    ``project_set`` is the tab's ``BinarySet`` (its ``extra`` keeps the
    manifest); ``prefs`` the settings the per-user store and your presets
    live in. Either may be None."""

    def __init__(self, session, project_set=None, prefs=None):
        self.session = session
        self.project_set = project_set
        self.prefs = prefs
        self.provider = provider_for(session)
        self.platform = getattr(session, "platform", "") or ""
        self.ecu = ECU_OF_PLATFORM.get(self.platform, "")
        self.namer = None
        if self.provider is not None:
            from ..channels import ChannelNamer
            self.namer = ChannelNamer(session)
        self.manifest = self._load()

    @property
    def available(self) -> bool:
        return self.provider is not None and self.namer is not None and \
            bool(getattr(self.namer, "available", False))

    # ---- the record ----
    def _load(self) -> Optional[_m.LayoutManifest]:
        m = None
        if self.project_set is not None:
            m = _m.load_extra(getattr(self.project_set, "extra", None))
        if m is None and self.prefs is not None and self.provider is not None:
            for k in self.identity_keys():
                m = _m.load_for_identity(self.prefs, self.platform, k)
                if m is not None:
                    # Filed under the identity, so possibly built in ANOTHER
                    # file with this part number (the tuned one, while this
                    # is a stock copy): only this file's own record if the
                    # file holds some of it. Otherwise its presets would read
                    # "changed since", and Remove would drop the other file's
                    # record from your settings.
                    if m.platform != self.platform or \
                            not m.valid_cells(self.provider.value_at):
                        m = None
                        continue
                    break
        if m is not None and m.platform != self.platform:
            return None
        return m

    def identity_keys(self) -> List[str]:
        try:
            return list(self.provider.identity().keys)
        except Exception:
            return []

    def valid_where(self) -> Set[Tuple[int, int]]:
        if self.manifest is None or self.provider is None:
            return set()
        return self.manifest.valid_cells(self.provider.value_at)

    def stock_namer(self):
        return StockNamer(self.namer, self.valid_where())

    def named(self):
        """The binary's namer with the recorded cells named from the record."""
        if self.manifest is None:
            return self.namer
        return LayoutNamer(self.namer, self.manifest, self.valid_where())

    def _store(self, m: Optional[_m.LayoutManifest]) -> None:
        self.manifest = m if (m is not None and m.presets) else None
        if self.project_set is not None:
            _m.store_extra(self.project_set.extra, self.manifest)
        if self.prefs is not None and m is not None:
            try:
                _m.save_for_identity(self.prefs, m)
            except Exception:
                pass

    # ---- presets ----
    def entries(self) -> List[PresetEntry]:
        if not self.available:
            return []
        out = entries_for(self.ecu, self.namer, self.prefs)
        if self.manifest is not None:
            have = {e.key for e in out}
            for key, pr in self.manifest.presets.items():
                if key not in have:
                    out.append(PresetEntry(key, pr.title or key, ORPHAN, [], pr.content))
        return out

    def baseline(self, entry: PresetEntry, stock=None) -> List[int]:
        from ..served import unserved_for
        stock = stock if stock is not None else self.stock_namer()
        return baseline_of(entry, stock, unserved_for(stock))

    def status(self, entry: PresetEntry) -> str:
        """"current" (built and in the file), "stale" (the preset changed
        since), "broken" (cells changed since, by hand or undo), ORPHAN (the
        preset is gone: only Remove applies) or ""."""
        m = self.manifest
        if m is None or entry.key not in m.presets:
            return ""
        if entry.is_orphan:
            return ORPHAN
        if m.presets[entry.key].content != entry.content:
            return "stale"
        ok = self.valid_where()
        if all(c.where in ok for c in m.preset_cells(entry.key)):
            return "current"
        return "broken"

    # ---- what limits the analysis ----
    def _name_coverage(self) -> Tuple[int, int]:
        """(named, total) cells the binary's populated groups carry."""
        named = total = 0
        namer = self.namer
        if namer is None:
            return 0, 0
        try:
            for g in namer.populated_groups():
                for f in namer.cell_fields(g):
                    total += 1
                    if namer.resolve(g, f)[1]:
                        named += 1
        except Exception:
            return 0, 0
        return named, total

    def blocked(self) -> Optional[str]:
        """Why this binary cannot be analysed at all (None when it can): the
        window shows it instead of "no gain"."""
        if not self.available:
            return "its measuring blocks cannot be read"
        named, total = self._name_coverage()
        if total and not named:
            return ("none of its %d measuring-block cells has a signal name (no signal "
                    "catalogue for this software), so no preset can be matched to its blocks"
                    % total)
        return None

    def limits(self) -> List[str]:
        """What limits the analysis of this binary, in words: the provider's
        own notes (tables the definition leaves out, found or not; reserved
        groups unknown) and cells without a signal name."""
        out: List[str] = []
        if self.provider is None:
            return out
        try:
            notes = getattr(self.provider, "notes", None)
            out += [str(n) for n in (notes() if callable(notes) else ()) or ()]
        except Exception:
            pass
        try:
            unknown = self.provider.reserved_groups() is None
        except Exception:
            unknown = True
        if unknown and not any("keeps for itself" in n for n in out):
            out.append("Which blocks the ECU keeps for itself is unknown for this file, so no "
                       "block is changed.")
        named, total = self._name_coverage()
        if total and 0 < named < total:
            out.append("%d of the %d cells in use have no signal name; presets use named cells "
                       "only." % (total - named, total))
        why = self.blocked()
        if why:
            out.append("Not analysed: %s." % why)
        return out

    def plan(self, keys: Sequence[str], fill: bool = True) -> Allocation:
        """Plan the presets ``keys`` afresh: their own recorded cells count as
        free again, everyone else's stay and may be reused."""
        keys = list(keys)
        entries = [e for e in self.entries() if e.key in keys and not e.is_orphan]
        keys = [k for k in keys if any(e.key == k for e in entries)]
        why = self.blocked()
        if why:
            from .model import NoGain
            out = Allocation()
            out.skipped = [NoGain(e.key, e.title, [], "not analysed: %s" % why)
                           for e in entries]
            return out
        stock = self.stock_namer()
        layout = build_layout(self.provider, self.namer, self.manifest, release=keys)
        if not fill:
            layout.reserved |= set(g for g in layout.cells)
        items = [PlanItem(e.key, e.title, tuple(e.reqs), tuple(self.baseline(e, stock)))
                 for e in entries]
        return allocate(items, layout, source_picker(self.provider.sources))

    def generate(self, alloc: Allocation, label: str = "Build logging blocks") -> int:
        """Write ``alloc`` and record it, as ONE undo step. Cells the
        re-planned presets had and no longer use go back to empty. Returns
        the number of cells changed."""
        if not alloc.plans:
            return 0
        from ... import branding
        planned = [p.key for p in alloc.plans]
        valid = self.valid_where()
        writes = alloc.writes
        new_where = {(w.group, w.slot) for w in writes}
        drop = []
        released = set()
        if self.manifest is not None:
            released = {c.where for c in self.manifest.owned_only_by(planned)
                        if c.where in valid}
            drop = [(c.group, c.slot, c.value) for c in self.manifest.owned_only_by(planned)
                    if c.where in valid and c.where not in new_where]
        # Never over a cell of the file's own: every write goes to a slot that
        # is empty now, or that holds a cell built earlier for these presets.
        # (A plan made before the file changed is refused, nothing written.)
        from .provider import FREE as _FREE
        for w in writes:
            try:
                st = list(self.provider.slot_states(w.group))[w.slot]
            except Exception:
                st = None
            if st != _FREE and (w.group, w.slot) not in released:
                raise ValueError("block %d cell %d is not empty in the file any more: "
                                 "plan again" % (w.group, w.slot + 1))
        n = 0
        before = self._top()
        with self.session.transaction(label):
            if drop:
                n += self.provider.restore(drop, label)
            n += self.provider.write(writes, label)
        self._name_step(before, label)
        contents = {e.key: e.content for e in self.entries()}
        keep = self.valid_where()
        m = _m.LayoutManifest.from_allocation(
            alloc, self.platform, self.identity_keys(), contents,
            app="%s %s" % (branding.APP_NAME, branding.__version__),
            previous=self.manifest, keep=keep, empty=self.provider.empty_cell())
        self._store(m)
        return n

    def remove(self, keys: Sequence[str], label: str = "Remove logging blocks") -> int:
        """Empty the cells only ``keys`` read (where the file still holds what
        was written) and drop them from the record. One undo step."""
        if self.manifest is None:
            return 0
        valid = self.valid_where()
        cells = [(c.group, c.slot, c.value) for c in self.manifest.owned_only_by(keys)
                 if c.where in valid]
        before = self._top()
        n = self.provider.restore(cells, label) if cells else 0
        self._name_step(before, label)
        self._store(self.manifest.without(keys))
        # cells, not per-dataset writes (an EDC15 cell is one per dataset)
        return len(cells) if n else 0

    def _top(self):
        stack = getattr(self.session, "undo_stack", None)
        try:
            return stack.top() if stack is not None else None
        except Exception:
            return None

    def _name_step(self, before, label: str) -> None:
        """A transaction of one command keeps that command's own label ("edit
        UACC_RSigBlkGrp_MAP"); the Edit menu should say what was done."""
        top = self._top()
        if top is not None and top is not before:
            try:
                top.label = label
            except Exception:
                pass


# ---------------------------------------------------------------- logger
def report_key(platform: str, identity: str) -> str:
    """A connected ECU's identity as the per-user store files it: EDC16
    "PART SW" (the catalogue key, from the 1A 9B record), EDC15 the reported
    text (matched by prefix against each dataset's ident)."""
    from .. import msbcatalogue as _cat
    if platform == "edc16":
        pv = _cat.edc16_identity(identity or "")
        return _cat.edc16_key(*pv) if pv else (identity or "")
    return identity or ""


@dataclass
class Optimised:
    key: str
    groups: List[int]
    baseline: List[int]
    manifest: _m.LayoutManifest
    where: Set[Tuple[int, int]] = field(default_factory=set)
    #: True: the open binary holds every recorded cell; False: only the
    #: per-user record says so, and the probe has to confirm it
    in_binary: bool = False

    def namer(self, base):
        return LayoutNamer(base, self.manifest, self.where)


def optimised_for(manifest: Optional[_m.LayoutManifest], key: str, content: str,
                  value_at=None) -> Optional[Optimised]:
    """The preset's optimised layout, when it has a current one: with
    ``value_at`` (a binary is open) only while the binary still holds every
    recorded cell it reads."""
    if manifest is None or key not in manifest.presets:
        return None
    pr = manifest.presets[key]
    if pr.content != content:
        return None
    mine = manifest.preset_cells(key)
    if value_at is not None:
        valid = manifest.valid_cells(value_at)
        if any(c.where not in valid for c in mine):
            return None
        return Optimised(key, list(pr.groups), list(pr.baseline), manifest,
                         {c.where for c in manifest.cells if c.where in valid}, True)
    return Optimised(key, list(pr.groups), list(pr.baseline), manifest,
                     {c.where for c in manifest.cells}, False)


def logging_tags(session, project_set=None, prefs=None) -> Dict[Tuple[int, int], List[str]]:
    """(group, slot) -> titles of the presets that read a cell built for
    logging, for every recorded cell the binary still holds."""
    b = Builder(session, project_set, prefs)
    if b.manifest is None or b.provider is None:
        return {}
    ok = b.valid_where()
    titles = {k: p.title for k, p in b.manifest.presets.items()}
    return {c.where: [titles.get(k, k) for k in c.presets]
            for c in b.manifest.cells if c.where in ok}


__all__ = ["PER_GROUP_MS", "ECU_OF_PLATFORM", "PLATFORM_OF_ECU", "RECIPE", "USER", "ORPHAN",
           "estimate_hz", "recipes_for_ecu", "PresetEntry", "entries_for", "baseline_of",
           "Builder", "report_key", "Optimised", "optimised_for", "logging_tags"]
