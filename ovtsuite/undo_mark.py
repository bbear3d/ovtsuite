# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Apply / Cancel for a tool window that writes its edits to the session as
it goes (the fault-code window).

The window's edits land in the session straight away, each as its own undo
step, so the grid, the comparison shading and every other view stay current.
An :class:`UndoMark` remembers which of the undo stack's commands the window
made since it opened or since its last **Apply**:

* :meth:`UndoMark.apply` folds them into ONE undo step (one per Apply);
* :meth:`UndoMark.discard` takes them back -- off the top of the stack, as
  though they were never made, when nothing else was edited in between;
  otherwise as one new step that puts back what the window changed, leaving
  the other edits (and the rest of the history) as they are.
"""
import contextlib

from .session import Command, CompoundCommand


def _key(cmds):
    return [id(c) for c in cmds]


class UndoMark:
    """The commands a window made on ``session`` since the mark."""

    def __init__(self, session, label="edit"):
        self.session = session
        self.label = label
        self._mine = []

    @property
    def _stack(self):
        return self.session.undo_stack

    # -- recording ---------------------------------------------------------
    @contextlib.contextmanager
    def track(self, label=None):
        """Run an edit as one undo step and remember it as this window's.
        Inside another transaction it joins that one (and is not tracked)."""
        stack = self._stack
        before = stack.top()
        with self.session.transaction(label or self.label):
            yield
        top = stack.top()
        if top is not None and top is not before and \
                all(top is not c for c in self._mine):
            self._mine.append(top)

    def active(self):
        """This window's commands still in effect (not undone elsewhere)."""
        done = {id(c) for c in self._stack._done}
        return [c for c in self._mine if id(c) in done]

    def has_pending(self) -> bool:
        return bool(self.active())

    def _on_top(self, cmds) -> bool:
        done = self._stack._done
        n = len(cmds)
        return 0 < n <= len(done) and _key(done[-n:]) == _key(cmds)

    @staticmethod
    def _net(cmds):
        """(addr, bits) -> [first old, last new] over ``cmds`` in order."""
        out = {}
        for c in cmds:
            for addr, bits, old, new in c.writes:
                k = (addr, bits)
                if k in out:
                    out[k][1] = new
                else:
                    out[k] = [old, new]
        return out

    # -- Apply ---------------------------------------------------------------
    def apply(self, label=None) -> int:
        """Fold the window's commands into one undo step (two when the file
        was saved in between, so undo still reaches the saved state) and
        move the mark to now. Returns how many commands were folded."""
        cmds = self.active()
        self._mine = []
        if not cmds or not self._on_top(cmds):
            # undone in part, or other edits in between: each stays its own
            # step (merging across another edit would reorder history)
            return len(cmds)
        stack = self._stack
        net = self._net(cmds)
        if all(c is not stack._clean for c in cmds) and \
                all(o == n for o, n in net.values()):
            # switched and switched back: nothing changed, no undo step
            del stack._done[-len(cmds):]
            stack.revision += 1
            return len(cmds)
        if len(cmds) == 1:
            return 1
        groups = [[]]
        for c in cmds:
            groups[-1].append(c)
            if c is stack._clean:
                groups.append([])
        groups = [g for g in groups if g]
        del stack._done[-len(cmds):]
        for g in groups:
            if len(g) == 1:
                stack._done.append(g[0])
                continue
            comp = CompoundCommand(label or self.label, g)
            if g[-1] is stack._clean:
                stack._clean = comp
            stack._done.append(comp)
        stack.revision += 1
        return len(cmds)

    # -- Cancel --------------------------------------------------------------
    def discard(self) -> int:
        """Put back every byte the window changed since the mark, and move
        the mark to now. Returns how many commands were taken back."""
        cmds = self.active()
        self._mine = []
        if not cmds:
            return 0
        stack = self._stack
        session = self.session
        if self._on_top(cmds):
            for c in reversed(cmds):
                c.revert(session.binfile)
            del stack._done[-len(cmds):]
            if any(c is stack._clean for c in cmds):
                stack._clean_lost = True        # the saved bytes are gone
            if stack._undone:
                # redo would replay edits made on top of the discarded ones
                if any(c is stack._clean for c in stack._undone):
                    stack._clean_lost = True
                stack._undone.clear()
            stack.revision += 1
            follow = getattr(session, "follow_layout", None)
            if follow is not None:
                follow()
            return len(cmds)
        # other edits came in between: one new step undoing ours, leaving a
        # byte alone when something else has written it since
        writes = []
        for (addr, bits), (old, new) in self._net(cmds).items():
            cur = session.binfile.read(addr, bits, False)
            if cur == new and cur != old:
                writes.append((addr, bits, cur, old))
        if writes:
            session._record(Command("cancel: %s" % self.label, writes),
                            "cancel %s: %d write(s)" % (self.label, len(writes)))
        return len(cmds)
