# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Requirements from recipes and your presets, and the namer overlays:
recorded cells named with no binary open, and hidden from the stock view the
baseline is measured on (so the logger's own planner never counts them)."""
from ovtsuite.datalog.blockpack import requirements as Q
from ovtsuite.datalog.blockpack.manifest import CellRecord, LayoutManifest, PresetRecord
from ovtsuite.datalog.blockpack.namer import LayoutNamer, StockNamer
from ovtsuite.datalog.userpresets import Cell, UserPreset
from ovtsuite.logtune import roles


class Base:
    """A catalogue-like namer: group -> [(label, signal, unit) | None]."""
    available = True

    def __init__(self, groups):
        self.g = groups

    def slots(self, group):
        return len(self.g.get(group) or [None] * 4)

    def resolve(self, group, field):
        cells = self.g.get(group)
        if not cells or field >= len(cells) or cells[field] is None:
            return ("Group %d / Cell %d" % (group, field + 1), "", "")
        return cells[field]

    def cell_fields(self, group):
        cells = self.g.get(group)
        if not cells:
            return list(range(self.slots(group)))
        return [f for f, c in enumerate(cells) if c is not None]

    def populated_groups(self):
        return sorted(self.g)

    def channel(self, group, field):
        from ovtsuite.datalog.logfile import Channel
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def find_signal(self, name):
        for g in sorted(self.g):
            for f in range(self.slots(g)):
                if self.resolve(g, f)[1] == name:
                    return g, f
        return None

    def describe(self):
        return "base"


def c(sig, label=None):
    return (label or sig, sig, "")


BASE = Base({1: [c("Eng_nAvrg"), c("InjCtl_qSetUnBal"), None, None],
             10: [c("Eng_nAvrg"), c("PCR_pBDes"), c("BPSCD_pFltVal"), c("PCR_rBPACD")],
             11: [c("APSCD_pVal"), c("AccPed_rChkdVal"), c("CTSCD_tClnt"), None]})


def manifest():
    m = LayoutManifest("edc16", ["03G906016LQ 1717"])
    m.cells = [CellRecord(124, 0, 0, "Eng_nAvrg", "Engine speed", "rpm", 1, 0xC8, "new", ["boost"]),
               CellRecord(124, 1, 7, "InjCtl_qSetUnBal", "Injected quantity", "mg/str", 0x24,
                          0x0A, "new", ["boost"]),
               CellRecord(1, 2, 30, "BPSCD_pFltVal", "Boost", "mbar", 0x12, 0x40, "fill", ["boost"])]
    m.presets = {"boost": PresetRecord("boost", "Boost", "c", [1, 124], [1, 10, 11])}
    return m


def test_requirements_from_a_recipe_follow_the_roles():
    rec = next(r for r in roles.RECIPES if r.key == "boost")
    reqs = Q.from_recipe(rec)
    assert [r.key for r in reqs] == list(rec.needs) + [w for w in rec.wants if w not in rec.needs]
    assert all(r.need for r in reqs[:len(rec.needs)])
    assert reqs[0].signals == tuple(roles.ROLES["rpm"])
    assert reqs[0].label == roles.ROLE_LABELS["rpm"]


def test_requirements_from_your_preset_collapse_and_pin():
    p = UserPreset("Mine", [Cell(1, 0, "Eng_nAvrg", "Engine speed"),
                            Cell(10, 0, "Eng_nAvrg", "Engine speed"),
                            Cell(10, 2, "BPSCD_pFltVal", "Boost"),
                            Cell(30, 1, "", "Something")])
    reqs = Q.from_user_preset(p)
    assert [r.key for r in reqs] == ["Eng_nAvrg", "BPSCD_pFltVal", "pin:30/2"]
    assert reqs[2].pin == (30, 1) and reqs[2].signals == ()
    k1 = Q.content_key(reqs)
    p.cells.append(Cell(11, 2, "CTSCD_tClnt", "Coolant"))
    assert Q.content_key(Q.from_user_preset(p)) != k1
    assert Q.user_key(p) == "user:Mine"


def test_baselines_are_what_the_logger_reads_today():
    rec = next(r for r in roles.RECIPES if r.key == "boost")
    assert Q.recipe_baseline(BASE, rec) == roles.recipe_groups(BASE, rec, rec.max_groups)[0]
    p = UserPreset("Mine", [Cell(1, 0, "Eng_nAvrg"), Cell(11, 2, "CTSCD_tClnt")])
    assert Q.user_baseline(BASE, p) == [1, 11]


def test_layout_namer_names_recorded_cells_with_no_binary():
    n = LayoutNamer(BASE, manifest())
    assert n.resolve(124, 1) == ("Injected quantity", "InjCtl_qSetUnBal", "mg/str")
    assert n.cell_fields(124) == [0, 1]              # a new group: recorded cells only
    assert n.cell_fields(1) == [0, 1, 2]             # stock cells plus the filled one
    assert n.resolve(1, 0)[1] == "Eng_nAvrg"         # stock cells pass through
    assert 124 in n.populated_groups()
    assert n.channel(1, 2).signal == "BPSCD_pFltVal"
    assert n.describe() == "base"                    # anything else goes to the base
    assert n.find_signal("PCR_pBDes") == (10, 1)


def test_layout_namer_limited_to_confirmed_cells():
    n = LayoutNamer(BASE, manifest(), where={(124, 0)})
    assert n.resolve(124, 1)[1] == ""
    assert n.cell_fields(124) == [0]


def test_stock_namer_hides_recorded_cells_from_the_planner():
    m = manifest()
    full = LayoutNamer(BASE, m)
    stock = StockNamer(full, [c.where for c in m.cells])
    assert 124 not in stock.populated_groups()
    assert stock.cell_fields(1) == [0, 1]
    assert stock.resolve(1, 2)[1] == ""
    # the logger's own planner, on the stock view, never picks the new group
    rec = next(r for r in roles.RECIPES if r.key == "boost")
    assert 124 not in roles.recipe_groups(stock, rec, rec.max_groups)[0]


def test_unserved_for_sees_through_the_blockpack_overlays():
    """Review fix: ``served.unserved_for`` judged the namer by its type name,
    so a StockNamer / LayoutNamer over an engine namer always gave []."""
    from ovtsuite.datalog.blockpack.manifest import LayoutManifest
    from ovtsuite.datalog.blockpack.namer import LayoutNamer, StockNamer
    from ovtsuite.datalog.served import unserved_for

    class ChannelNamer:                 # what served.py recognises, by name
        available = True

        def populated_groups(self):
            return [1, 10, 133]

        def cell_fields(self, g):
            return [0]

    class TcuNamer(ChannelNamer):
        pass
    TcuNamer.__name__ = "TransmissionNamer"
    base = ChannelNamer()
    assert unserved_for(base) == [133]
    assert unserved_for(StockNamer(base, [])) == [133]
    assert unserved_for(StockNamer(base, [(133, 0)])) == []          # hidden: not there
    assert unserved_for(LayoutNamer(base, LayoutManifest("edc16"))) == [133]
    assert unserved_for(StockNamer(TcuNamer(), [])) == []            # engine-side only
