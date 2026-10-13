# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The undo stack and the save path — the two places a tune can be lost.

Both had defects worth naming. ``modified`` compared the length of the undo
list against a saved COUNT, so save-two, undo-one, edit-something-else reported
the document unmodified while its bytes differed from disk; closing it would
have discarded the edit without a prompt. And ``save`` wrote straight over the
target, so a full disk or a power cut mid-write left a truncated binary where
the only copy of a tune used to be.
"""

import os

import pytest

from ovtsuite.session import Command, CommandStack, CompoundCommand, TunerSession

REF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "Reference", "Binaries")


class FakeBin:
    """Just enough BinaryFile for the stack: byte writes at an address."""

    def __init__(self, n=16):
        self.data = bytearray(n)

    def write(self, addr, value, bits=8):
        self.data[addr] = value & 0xFF


def w(addr, old, new):
    return Command("edit bytes @0x%X" % addr, [(addr, 8, old, new)])


def _bin(name):
    p = os.path.join(REF, name)
    if not os.path.exists(p):
        pytest.skip("reference binary %s not present" % name)
    return p


# -- the clean mark ---------------------------------------------------------

def test_clean_mark_survives_undo_and_redo():
    b, s = FakeBin(), CommandStack()
    c1 = w(0, 0, 1); c1.apply(b); s.push(c1)
    c2 = w(1, 0, 2); c2.apply(b); s.push(c2)
    s.mark_clean()
    assert s.is_clean
    s.undo(b)
    assert not s.is_clean
    s.redo(b)
    assert s.is_clean, "redoing back to the saved state is clean again"


def test_editing_a_different_branch_after_undo_is_still_modified():
    """The bug this replaces: a COUNT-based clean mark called this clean,
    because the stack was two deep again -- with different contents."""
    b, s = FakeBin(), CommandStack()
    c1 = w(0, 0, 1); c1.apply(b); s.push(c1)
    c2 = w(1, 0, 2); c2.apply(b); s.push(c2)
    s.mark_clean()
    s.undo(b)                       # back to one command
    c3 = w(2, 0, 3); c3.apply(b); s.push(c3)   # a DIFFERENT second command
    assert len(s) == 2
    assert not s.is_clean, "the bytes differ from disk; this must prompt to save"


def test_undoing_to_the_empty_state_is_clean_when_nothing_was_saved():
    b, s = FakeBin(), CommandStack()
    assert s.is_clean
    c1 = w(0, 0, 1); c1.apply(b); s.push(c1)
    assert not s.is_clean
    s.undo(b)
    assert s.is_clean


def test_clean_mark_lost_to_eviction_stays_modified():
    """Failing towards 'modified' is the safe direction: the alternative is
    losing work silently."""
    b, s = FakeBin(64), CommandStack(limit=3)
    for i in range(3):
        c = w(i, 0, i + 1); c.apply(b); s.push(c)
    s.mark_clean()
    for i in range(3, 6):
        c = w(i, 0, i + 1); c.apply(b); s.push(c)
    assert len(s) == 3, "history is bounded"
    for _ in range(3):
        s.undo(b)
    assert not s.is_clean, "the saved state was evicted; never claim clean again"


# -- coalescing -------------------------------------------------------------

def test_two_nibbles_of_one_byte_are_one_undo():
    b, s = FakeBin(), CommandStack()
    first = w(4, 0x00, 0xA0); first.apply(b); s.push(first, coalesce=True)
    second = w(4, 0xA0, 0xAB); second.apply(b); s.push(second, coalesce=True)
    assert len(s) == 1
    assert b.data[4] == 0xAB
    s.undo(b)
    assert b.data[4] == 0x00, "one undo reverts to before the first nibble"


def test_coalescing_refuses_across_a_save():
    b, s = FakeBin(), CommandStack()
    first = w(4, 0x00, 0xA0); first.apply(b); s.push(first, coalesce=True)
    s.mark_clean()
    second = w(4, 0xA0, 0xAB); second.apply(b); s.push(second, coalesce=True)
    assert len(s) == 2, "merging across a save makes the clean state unreachable"
    s.undo(b)
    assert s.is_clean and b.data[4] == 0xA0


def test_different_edits_do_not_merge():
    b, s = FakeBin(), CommandStack()
    a = w(4, 0, 1); a.apply(b); s.push(a, coalesce=True)
    c = w(5, 0, 2); c.apply(b); s.push(c, coalesce=True)
    assert len(s) == 2


# -- compound ---------------------------------------------------------------

def test_compound_is_one_undo_step():
    b, s = FakeBin(), CommandStack()
    comp = CompoundCommand("bulk", [w(0, 0, 1), w(1, 0, 2), w(2, 0, 3)])
    comp.apply(b); s.push(comp)
    assert list(b.data[:3]) == [1, 2, 3] and len(s) == 1
    s.undo(b)
    assert list(b.data[:3]) == [0, 0, 0]


def test_compound_rolls_back_when_a_part_raises():
    """A half-applied plan must never reach the binary."""
    class Boom(Command):
        def apply(self, binfile):
            raise RuntimeError("no")

    b = FakeBin()
    comp = CompoundCommand("bulk", [w(0, 0, 1), Boom("boom", []), w(2, 0, 3)])
    with pytest.raises(RuntimeError):
        comp.apply(b)
    assert list(b.data[:3]) == [0, 0, 0], "the first write must be rolled back"


def test_revision_bumps_on_every_change():
    b, s = FakeBin(), CommandStack()
    r0 = s.revision
    c = w(0, 0, 1); c.apply(b); s.push(c)
    r1 = s.revision
    s.undo(b)
    s.redo(b)
    assert r0 < r1 < s.revision, "derived caches key off this"


# -- the real session and its save path -------------------------------------

def test_session_save_is_atomic_and_keeps_one_backup(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    original = target.read_bytes()

    s = TunerSession(str(target))
    e = next(x for x in s.entities if x.kind == "map" and not x.signed)
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    assert s.modified
    s.save()
    assert not s.modified

    assert (tmp_path / "work.bin.bak").read_bytes() == original, \
        "the backup must hold the file as it was before the first save"
    assert not any(p.name.startswith(".work.bin.tmp") for p in tmp_path.iterdir()), \
        "the temporary file must not survive the rename"

    s.edit_cells(e, [(0, 1, e.cell(0, 1) + 1)])
    s.save()
    assert (tmp_path / "work.bin.bak").read_bytes() == original, \
        "a later save must not overwrite the pristine backup"


def test_session_writes_a_change_log_next_to_the_file(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    e = next(x for x in s.entities if x.kind == "map" and not x.signed)
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
    s.save()
    log = tmp_path / "work.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert e.name in text and "saved work.bin" in text
    assert s.change_log == [], "entries are consumed once written"


def test_hex_edits_coalesce_through_the_session(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    addr = s.region[0] + 0x40
    before = s.binfile.data[addr]
    s.edit_bytes(addr, bytes([(before & 0x0F) | 0xA0]), coalesce=True)
    s.edit_bytes(addr, bytes([0xAB]), coalesce=True)
    assert len(s.undo_stack) == 1
    s.undo()
    assert s.binfile.data[addr] == before


# -- transactions -----------------------------------------------------------

def test_transaction_groups_several_writes_into_one_undo(tmp_path):
    """A dialog that writes a value, its source selector and a type byte has
    performed one action; three presses of Ctrl+Z to walk it back would leave
    the ECU in states the dialog would never write on purpose."""
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    a = s.region[0] + 0x80
    before = bytes(s.binfile.data[a:a + 3])

    with s.transaction("three writes, one action"):
        s.edit_bytes(a, bytes([before[0] ^ 0xFF]))
        s.edit_bytes(a + 1, bytes([before[1] ^ 0xFF]))
        s.edit_bytes(a + 2, bytes([before[2] ^ 0xFF]))

    assert len(s.undo_stack) == 1
    assert bytes(s.binfile.data[a:a + 3]) != before
    s.undo()
    assert bytes(s.binfile.data[a:a + 3]) == before


def test_transaction_rolls_back_when_the_body_raises(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    a = s.region[0] + 0x80
    before = bytes(s.binfile.data[a:a + 2])

    with pytest.raises(RuntimeError):
        with s.transaction("half a dialog"):
            s.edit_bytes(a, bytes([before[0] ^ 0xFF]))
            raise RuntimeError("the dialog gave up")

    assert bytes(s.binfile.data[a:a + 2]) == before, "no partial write survives"
    assert len(s.undo_stack) == 0
    assert not s.modified


def test_empty_transaction_records_nothing(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    with s.transaction("nothing to do"):
        pass
    assert len(s.undo_stack) == 0 and not s.modified


def test_nested_transactions_join_the_outer_one(tmp_path):
    src = _bin("038906016AB.bin")
    target = tmp_path / "work.bin"
    target.write_bytes(open(src, "rb").read())
    s = TunerSession(str(target))
    a = s.region[0] + 0x80
    with s.transaction("outer"):
        s.edit_bytes(a, bytes([s.binfile.data[a] ^ 0xFF]))
        with s.transaction("inner"):
            s.edit_bytes(a + 1, bytes([s.binfile.data[a + 1] ^ 0xFF]))
    assert len(s.undo_stack) == 1
