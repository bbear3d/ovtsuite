# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fitted hardware in a project (ovtsuite.project.hardware): which hardware a
version is calibrated for, what the car had when, and how a setup tool's
record follows the history."""

import random

from ovtsuite import project as ovt
from ovtsuite.project import hardware as H


def _bench():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    flash = bytearray(random.Random(1).randbytes(0x4000))
    root, stock = p.add_root(s.id, {"flash": bytes(flash)}, kind="read", label="Read")
    return p, s, stock, flash


def _save(p, s, b, flash, off, label):
    flash[off] ^= 0xFF
    return p.commit(s.id, b.id, {"flash": bytes(flash)}, label=label)


def test_a_setup_belongs_to_the_versions_saved_after_it():
    p, s, stock, flash = _bench()
    root = stock.head
    st1 = p.fork(s.id, "Stage 1")
    a = _save(p, s, st1, flash, 0x10, "a")
    # Injector setup's Apply on top of "a" (its edits not saved yet)
    rec = H.record_setup(p, s.id, st1.id, H.VE_PUMP, "11 mm pump head",
                         {"plunger_mm": 11.0, "nozzle": "p520"})
    assert rec.pending and rec.checkpoint == a.id and rec.source == H.FROM_SETUP
    assert H.for_checkpoint(p, s.id, a.id) == []                 # "a" is not set up for it
    assert H.for_checkpoint(p, s.id, a.id, unsaved=True)[0].id == rec.id
    b = _save(p, s, st1, flash, 0x20, "b")
    assert H.for_checkpoint(p, s.id, b.id)[0].id == rec.id
    assert H.for_checkpoint(p, s.id, root) == []                 # the original
    # the same setup applied again adds nothing
    assert H.record_setup(p, s.id, st1.id, H.VE_PUMP, "11 mm pump head",
                          {"plunger_mm": 11.0, "nozzle": "p520"}) is None
    # another build later wins on the versions after it, not before
    rec2 = H.record_setup(p, s.id, st1.id, H.VE_PUMP, "12 mm", {"plunger_mm": 12.0})
    c = _save(p, s, st1, flash, 0x30, "c")
    assert H.for_checkpoint(p, s.id, c.id)[0].id == rec2.id
    assert H.for_checkpoint(p, s.id, b.id)[0].id == rec.id
    # another branch from the original knows nothing of it
    eco = p.fork(s.id, "Eco", at=root)
    e = _save(p, s, eco, flash, 0x40, "e")
    assert H.for_checkpoint(p, s.id, e.id) == []
    assert H.differ(H.for_checkpoint(p, s.id, c.id), H.for_checkpoint(p, s.id, b.id)) \
        == [H.VE_PUMP]
    assert H.differ(H.for_checkpoint(p, s.id, e.id), []) == []


def test_what_the_car_had_when():
    p, s, stock, _flash = _bench()
    H.add(p, s.id, H.VE_PUMP, "AFN nozzles", {"nozzle": "p520"}, since="2026-05-01")
    H.add(p, s.id, H.VE_PUMP, "Bosio 764", {"nozzle": "pp764"}, since="2026-09-10T12:00:00")
    H.add(p, s.id, H.PD_INJECTORS, "nothing to do with it", since="2026-01-01")
    assert H.at(p, s.id, "2026-04-30T23:00:00", H.VE_PUMP) == []
    assert H.at(p, s.id, "2026-05-01T08:00:00", H.VE_PUMP)[0].summary == "AFN nozzles"
    assert H.at(p, s.id, "2026-09-10T11:59:00", H.VE_PUMP)[0].summary == "AFN nozzles"
    assert H.at(p, s.id, "2026-09-11", H.VE_PUMP)[0].summary == "Bosio 764"
    assert {r.kind for r in H.current(p, s.id)} == {H.VE_PUMP, H.PD_INJECTORS}


def test_recorded_as_fitted_first_then_set_up():
    """"The car has it" recorded before the calibration is made: Apply then
    gives that record its version, and keeps the date it was fitted."""
    p, s, stock, flash = _bench()
    rec = H.record_fitted(p, s.id, stock.id, H.VE_PUMP, "11 mm", {"plunger_mm": 11.0},
                          calibrated=False, since="2026-10-01")
    assert rec.checkpoint is None
    assert H.for_checkpoint(p, s.id, stock.head, unsaved=True) == []
    got = H.record_setup(p, s.id, stock.id, H.VE_PUMP, "11 mm", {"plunger_mm": 11.0})
    assert got.id == rec.id and got.checkpoint == stock.head and got.since == "2026-10-01"
    assert len(H.records(p, s.id)) == 1


def test_kept_in_the_file_and_read_by_an_older_reader():
    p, s, stock, _flash = _bench()
    H.add(p, s.id, H.VE_PUMP, "11 mm pump head", {"plunger_mm": 11.0},
          checkpoint=stock.head, note="fitted by the shop")
    raw = p.set(s.id).extra[H.KEY]
    raw.append({"id": "later1", "kind": "turbo", "summary": "GTB2260", "boost": "a later field"})
    raw.append({"no": "id"})                                     # unreadable: skipped, kept
    q = ovt.Project.from_bytes(p.to_bytes())
    recs = H.records(q, s.id)
    assert sorted(r.kind for r in recs) == sorted([H.VE_PUMP, "turbo"])
    assert next(r for r in recs if r.kind == "turbo").extra == {"boost": "a later field"}
    assert len(q.set(s.id).extra[H.KEY]) == 3
    r = next(r for r in recs if r.kind == H.VE_PUMP)
    assert "calibrated in version 'Read'" in H.describe(r, q, s.id)
    H.update(q, s.id, r.id, note="")
    H.remove(q, s.id, "later1")
    assert len(H.records(q, s.id)) == 1


def test_hand_entered_records_compare_by_their_words():
    a = H.Fitted("a", H.PD_INJECTORS, "Firad 1043HF +50%")
    b = H.Fitted("b", H.PD_INJECTORS, "firad 1043HF +50% ")
    c = H.Fitted("c", H.PD_INJECTORS, "Stock PD100")
    assert H.differ([a], [b]) == [] and H.differ([a], [c]) == [H.PD_INJECTORS]


def test_the_versions_where_the_setup_changes():
    """setup_changes marks the version a setup starts at (the one after the
    checkpoint for a pending record), not the ones that only inherit it."""
    p, s, stock, flash = _bench()
    a = _save(p, s, stock, flash, 0x10, "a")
    H.record_setup(p, s.id, stock.id, H.PD_INJECTORS, "PD150 injectors", {"preset": "pd150"})
    assert H.setup_changes(p, s.id, a.id) == []
    assert [r.summary for r in H.setup_changes(p, s.id, a.id, unsaved=True)] \
        == ["PD150 injectors"]
    b = _save(p, s, stock, flash, 0x20, "b")
    c = _save(p, s, stock, flash, 0x30, "c")
    assert [r.summary for r in H.setup_changes(p, s.id, b.id)] == ["PD150 injectors"]
    assert H.setup_changes(p, s.id, c.id) == []
    assert H.setup_changes(p, s.id, c.id, unsaved=True) == []
    assert H.setup_changes(p, s.id, stock.head) == []
    assert H.setup_changes(p, s.id, None) == []
    # a record made on a saved version, by hand: that version itself
    H.add(p, s.id, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"}, checkpoint=c.id)
    assert [r.summary for r in H.setup_changes(p, s.id, c.id)] == ["PD130 injectors"]


def test_the_status_line_says_when_the_car_and_the_version_differ():
    p, s, stock, flash = _bench()
    assert H.status_text(p, s.id, stock.head) == ("", "")
    H.record_fitted(p, s.id, stock.id, H.PD_INJECTORS, "PD150 injectors",
                    {"preset": "pd150"}, calibrated=False, since="2026-10-01")
    line, tip = H.status_text(p, s.id, stock.head)
    assert line == "Fitted: PD150 injectors — this version is not set up for it"
    assert "fitted 2026-10-01" in tip and "what the car came with" in tip
    H.record_setup(p, s.id, stock.id, H.PD_INJECTORS, "PD150 injectors", {"preset": "pd150"})
    assert H.status_text(p, s.id, stock.head, unsaved=True)[0] == "Fitted: PD150 injectors"
    b = _save(p, s, stock, flash, 0x20, "b")
    line, tip = H.status_text(p, s.id, b.id)
    assert line == "Fitted: PD150 injectors" and "This version is set up for it." in tip
    # other injectors fitted since: the version is still set up for the PD150s
    H.add(p, s.id, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"}, since="2026-10-20")
    st = H.status(p, s.id, b.id)
    assert st["differ"] == [H.PD_INJECTORS]
    assert H.status_text(p, s.id, b.id)[0] == \
        "Fitted: PD130 injectors — this version is set up for: PD150 injectors"


def _three(p, s, flash):
    st1 = p.fork(s.id, "Stage 1")
    return st1, [_save(p, s, st1, flash, off, name)
                 for off, name in ((0x10, "a"), (0x20, "b"), (0x30, "c"))]


def test_merging_versions_moves_a_record_to_the_merged_one():
    """A record on a version merged away goes to the newest, not pending:
    "a and later" and "after a" both mean "the merged version and later"."""
    import pytest
    p, s, stock, flash = _bench()
    st1, (a, b, c) = _three(p, s, flash)
    on = H.add(p, s.id, H.VE_PUMP, "11 mm", {"plunger_mm": 11.0}, checkpoint=a.id)
    after = H.add(p, s.id, H.PD_INJECTORS, "PD150", {"preset": "pd150"},
                  checkpoint=b.id, pending=True)
    keep = H.add(p, s.id, "turbo", "GTB2260", checkpoint=stock.head)
    p.merge_checkpoints(s.id, [a.id, b.id, c.id])
    assert H.get(p, s.id, on.id).checkpoint == c.id and not H.get(p, s.id, on.id).pending
    assert H.get(p, s.id, after.id).checkpoint == c.id
    assert not H.get(p, s.id, after.id).pending
    assert H.get(p, s.id, keep.id).checkpoint == stock.head
    assert {r.id for r in H.for_checkpoint(p, s.id, c.id)} == {on.id, after.id, keep.id}
    assert {r.id for r in H.for_checkpoint(p, s.id, stock.head)} == {keep.id}
    q = ovt.Project.from_bytes(p.to_bytes())
    assert H.get(q, s.id, on.id).checkpoint == c.id
    # pending on the newest itself stays pending; records on the newest stay
    p2, s2, stock2, flash2 = _bench()
    _st, (a2, b2, c2) = _three(p2, s2, flash2)
    top = H.add(p2, s2.id, H.VE_PUMP, "x", {"n": 1}, checkpoint=c2.id, pending=True)
    p2.merge_checkpoints(s2.id, [a2.id, b2.id])
    assert H.get(p2, s2.id, top.id).checkpoint == c2.id and H.get(p2, s2.id, top.id).pending
    with pytest.raises(ValueError):                       # an original still refuses
        p2.set(s2.id).originals.append(ovt.Original(id="o", filename="f", format="raw",
                                                    checkpoint=b2.id))
        p2.merge_checkpoints(s2.id, [b2.id, c2.id])


def test_merging_versions_that_would_change_the_setup_is_refused():
    """Two records of one kind in the run: merged, both sit on one version
    and the tie goes by when they were recorded. Refused when that is not
    the one that was nearer the newest; allowed when it is."""
    import pytest
    p, s, stock, flash = _bench()
    st1, (a, b, c) = _three(p, s, flash)
    old = H.add(p, s.id, H.VE_PUMP, "11 mm", {"plunger_mm": 11.0}, checkpoint=a.id)
    new = H.add(p, s.id, H.VE_PUMP, "12 mm", {"plunger_mm": 12.0}, checkpoint=b.id)
    H.update(p, s.id, old.id, recorded="2026-12-01T00:00:00")   # recorded last
    with pytest.raises(ValueError, match="Hardware records"):
        p.merge_checkpoints(s.id, [a.id, b.id, c.id])
    assert p.set(s.id).checkpoint(a.id) is not None           # nothing changed
    assert H.get(p, s.id, old.id).checkpoint == a.id
    H.update(p, s.id, old.id, recorded="2026-01-01T00:00:00")
    p.merge_checkpoints(s.id, [a.id, b.id, c.id])
    assert H.for_checkpoint(p, s.id, c.id)[0].id == new.id
