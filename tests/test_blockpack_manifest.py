# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The layout manifest: validity against the binary, its two stores, and the
planner's snapshot of a binary (``provider.build_layout``)."""
import json

from ovtsuite.datalog.blockpack import (EMPTY, CellSource, Layout, PlanItem, Requirement,
                                        allocate, source_picker)
from ovtsuite.datalog.blockpack import manifest as M
from ovtsuite.datalog.blockpack.provider import FREE, NONE, USED, LayoutIdentity, build_layout

EMPTY_ID = 255


class FakeProvider:
    """Four-slot groups 1..10 held as values; 255 is the empty id."""
    platform = "fake"

    def __init__(self, values, reserved=(), names=None):
        self.values = {g: list(v) for g, v in values.items()}
        self._reserved = set(reserved)
        self.names = names or {}            # value -> signal

    def slots(self):
        return 4

    def group_range(self):
        return range(1, 11)

    def slot_states(self, g):
        row = self.values.get(g, [EMPTY_ID] * 4)
        return [FREE if v == EMPTY_ID else USED for v in row]

    def reserved_groups(self):
        return set(self._reserved)

    def free_sort_key(self, g):
        return -g

    def sources(self, signal):
        return [CellSource(v, s, (0x01, v)) for v, s in sorted(self.names.items()) if s == signal]

    def value_at(self, g, s):
        return self.values.get(g, [EMPTY_ID] * 4)[s]

    def empty_cell(self):
        return (0x25, 0x00)

    def write(self, writes, label):
        for w in writes:
            self.values.setdefault(w.group, [EMPTY_ID] * 4)[w.slot] = w.source.value
        return len(writes)

    def restore(self, cells, label):
        n = 0
        for g, s, v in cells:
            if self.value_at(g, s) == v:
                self.values[g][s] = EMPTY_ID
                n += 1
        return n

    def identity(self):
        return LayoutIdentity("fake", ("PART SW1",))


class Namer:
    def __init__(self, prov):
        self.prov = prov

    def resolve(self, g, f):
        v = self.prov.value_at(g, f)
        return ("x", self.prov.names.get(v, ""), "")


NAMES = {1: "rpm", 2: "iq", 3: "boost", 4: "bspec", 5: "clt"}


def R(k, need=True):
    return Requirement(k, (k,), need, k)


def setup():
    prov = FakeProvider({1: [1, 2, 255, 255], 2: [1, 3, 9, 9], 3: [4, 9, 9, 9]},
                        reserved={7}, names=NAMES)
    lay = build_layout(prov, Namer(prov))
    item = PlanItem("boost", "Boost", (R("rpm"), R("iq"), R("boost"), R("bspec")), (1, 2, 3))
    al = allocate([item], lay, source_picker(prov.sources))
    return prov, lay, al


def test_build_layout_sees_free_reserved_and_empty_cells():
    prov, lay, _ = setup()
    assert lay.free == [10, 9, 8, 6, 5, 4]          # highest first, 7 reserved
    assert lay.cells[1] == ["rpm", "iq", EMPTY, EMPTY]
    assert 7 not in lay.free and 7 not in lay.cells


def test_reserved_unknown_reads_groups_as_they_are_and_changes_none():
    """A provider that cannot tell which groups are reserved returns None:
    every group is then reserved, none free, nothing fillable -- and a
    preset that cannot gain by reading groups as they are gets no plan."""
    class Unknown(FakeProvider):
        def reserved_groups(self):
            return None
    prov = Unknown({1: [1, 2, 255, 255], 2: [1, 3, 9, 9], 3: [4, 9, 9, 9]}, names=NAMES)
    lay = build_layout(prov, Namer(prov))
    assert lay.reserved_unknown and lay.free == []
    assert not any(lay.fillable(g) for g in lay.cells)
    item = PlanItem("boost", "Boost", (R("rpm"), R("iq"), R("boost"), R("bspec")), (1, 2, 3))
    al = allocate([item], lay, source_picker(prov.sources))
    assert not al.plans and not al.writes
    assert "unknown" in al.skipped[0].reason


def test_manifest_round_trip_and_validity_follow_the_binary():
    prov, lay, al = setup()
    plan = al.plans[0]
    assert len(plan.groups) == 1                     # group 1 filled with boost, bspec
    prov.write(al.writes, "build")
    m = M.LayoutManifest.from_allocation(al, "fake", ["PART SW1"], {"boost": "c1"})
    m2 = M.LayoutManifest.from_json(json.loads(m.dumps()))
    assert m2.to_json() == m.to_json()
    assert m.valid_presets(prov.value_at) == {"boost"}
    assert m.valid_presets(prov.value_at, {"boost": "c2"}) == set()   # preset edited
    # undo: the cells go back to empty, and the preset stops counting
    prov.restore([(c.group, c.slot, c.value) for c in m.cells], "undo")
    assert m.valid_presets(prov.value_at) == set()


def test_rebuilding_releases_only_the_presets_own_cells():
    prov, lay, al = setup()
    prov.write(al.writes, "build")
    m = M.LayoutManifest.from_allocation(al, "fake", ["PART SW1"], {"boost": "c1"})
    kept = build_layout(prov, Namer(prov), m, release=[])
    assert kept.cells[1][2:] == ["boost", "bspec"]   # still cells: reusable
    freed = build_layout(prov, Namer(prov), m, release=["boost"])
    assert freed.cells[1][2:] == [EMPTY, EMPTY]


def test_previous_presets_are_carried_over_when_another_is_planned():
    prov, lay, al = setup()
    prov.write(al.writes, "build")
    m1 = M.LayoutManifest.from_allocation(al, "fake", ["PART SW1"], {"boost": "c1"})
    lay2 = build_layout(prov, Namer(prov), m1, release=[])
    item = PlanItem("other", "Other", (R("rpm"), R("clt")), (1, 3))
    prov.names[5] = "clt"
    prov.values[3][1] = 5                            # give group 3 a clt cell
    lay2 = build_layout(prov, Namer(prov), m1, release=[])
    al2 = allocate([item], lay2, source_picker(prov.sources))
    m2 = M.LayoutManifest.from_allocation(al2, "fake", ["PART SW1"], {"other": "c2"},
                                          previous=m1, keep=m1.valid_cells(prov.value_at))
    assert set(m2.presets) == {"boost", "other"}
    assert m2.without(["boost"]).presets.keys() == {"other"}


def test_project_extra_store():
    prov, lay, al = setup()
    m = M.LayoutManifest.from_allocation(al, "fake", ["PART SW1"], {"boost": "c1"})
    extra = {"hardware": [1]}
    M.store_extra(extra, m)
    assert M.load_extra(extra).to_json() == m.to_json()
    assert extra["hardware"] == [1]                  # other fields untouched
    M.store_extra(extra, None)
    assert M.EXTRA_KEY not in extra and M.load_extra(extra) is None
    assert M.load_extra({M.EXTRA_KEY: {"format": 99}}) is None


class Prefs:
    def __init__(self):
        self.d = {}

    def value(self, k, default=""):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v

    def sync(self):
        pass


def test_per_user_store_by_reported_identity():
    prov, lay, al = setup()
    m = M.LayoutManifest.from_allocation(al, "edc15", ["038906012GN 1,9l R4 EDC  SG  4308"],
                                         {"boost": "c1"})
    p = Prefs()
    M.save_for_identity(p, m)
    # KW1281 identity blocks can split a word: whitespace is ignored
    got = M.load_for_identity(p, "EDC15", "038906012GN1,9lR4EDC SG 4308")
    assert got is not None and set(got.presets) == {"boost"}
    assert M.load_for_identity(p, "edc15", "038906012N  1,9l R4 EDC  SG 4308") is None
    M.save_for_identity(p, m.without(["boost"]))     # nothing left: removed
    assert M.load_for_identity(p, "edc15", "038906012GN 1,9l R4 EDC  SG  4308") is None


def test_canon_compares_edc15_values_however_they_are_held():
    assert M.canon((("Manual", 3), ("Automatic", 7))) == {"Automatic": 7, "Manual": 3}
    assert M.canon({"Manual": 3, "Automatic": 7}) == M.canon((("Automatic", 7), ("Manual", 3)))
    assert M.canon(44.0) == 44


def test_renaming_your_preset_carries_its_layout_over():
    """Review fix: your preset's key is its name, so a rename used to orphan
    its optimised blocks (and the window could no longer remove them)."""
    prov, lay, al = setup()
    m = M.LayoutManifest.from_allocation(al, "edc15", ["038906012GN 1,9l R4 EDC  SG  4308"],
                                         {"boost": "c1"})
    old = next(iter(m.presets))
    extra = {}
    M.store_extra(extra, m)
    p = Prefs()
    M.save_for_identity(p, m)
    assert M.rename_preset("edc15", old, "user:New", "New", [extra], p) == 2
    for got in (M.load_extra(extra),
                M.load_for_identity(p, "edc15", "038906012GN 1,9l R4 EDC  SG  4308")):
        assert old not in got.presets and got.presets["user:New"].title == "New"
        assert got.presets["user:New"].content == m.presets[old].content
        assert all(old not in c.presets for c in got.cells)
        assert any("user:New" in c.presets for c in got.cells)
    # another platform's records are left alone
    assert M.rename_preset("edc16", "user:New", "user:X", "X", [extra], p) == 0
