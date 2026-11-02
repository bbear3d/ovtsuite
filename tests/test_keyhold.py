# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A shortcut held down is one undo step (session.CommandStack.hold,
gui_keyhold)."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ovtsuite.session import Command, CommandStack  # noqa: E402


class Bin:
    def __init__(self):
        self.mem = {}

    def write(self, addr, value, bits):
        self.mem[addr] = value


def _edit(stack, b, addr, old, new, label="Step up"):
    b.write(addr, new, 8)
    stack.push(Command(label, [(addr, 8, old, new)]))


def test_the_edits_of_a_held_key_are_one_undo_step():
    s, b = CommandStack(), Bin()
    _edit(s, b, 1, 0, 1, "Typed")
    s.hold()
    for i in range(5):                                   # five repeats
        _edit(s, b, 2, i, i + 1)
    s.release()
    _edit(s, b, 2, 5, 6, "Typed again")
    assert len(s) == 3
    s.undo(b)
    assert b.mem[2] == 5                                 # the later edit only
    s.undo(b)
    assert b.mem[2] == 0                                 # all five repeats at once
    s.redo(b)
    assert b.mem[2] == 5
    assert s.undo_label() == "Step up"


def test_without_a_hold_every_edit_is_a_step():
    s, b = CommandStack(), Bin()
    for i in range(3):
        _edit(s, b, 2, i, i + 1)
    assert len(s) == 3


def test_a_new_hold_is_a_new_step_and_other_edits_end_a_run():
    s, b = CommandStack(), Bin()
    s.hold()
    _edit(s, b, 2, 0, 1)
    _edit(s, b, 2, 1, 2)
    s.release()
    s.hold()                                             # the key pressed again
    _edit(s, b, 2, 2, 3)
    _edit(s, b, 2, 3, 4)
    s.release()
    assert len(s) == 2
    s.hold()
    _edit(s, b, 2, 4, 5)
    s.undo(b)                                            # an undo mid-hold ...
    _edit(s, b, 2, 4, 9)                                 # ... starts a step of its own
    assert len(s) == 3 and s.undo_label() == "Step up"
    s.undo(b)
    assert len(s) == 2                                   # that step alone


def test_a_save_in_the_middle_is_not_merged_across():
    s, b = CommandStack(), Bin()
    s.hold()
    _edit(s, b, 2, 0, 1)
    s.mark_clean()
    _edit(s, b, 2, 1, 2)
    assert len(s) == 2 and not s.is_clean
    s.undo(b)
    assert s.is_clean                                    # back to the saved state


def test_only_a_shortcut_that_a_key_press_caused_holds():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui_keyhold
    w = gui_keyhold.watch()
    w.end()
    s = CommandStack()
    w.t_press = 0.0                                      # no key pressed lately
    assert not gui_keyhold.begin(s) and not s._holding   # a menu click
    w.t_press = time.monotonic()                         # a key press just now
    assert gui_keyhold.begin(s) and s._holding
    assert gui_keyhold.begin(s) and s._holding           # the next repeat
    gui_keyhold.end()                                    # the key released
    assert not s._holding
    assert app is not None
