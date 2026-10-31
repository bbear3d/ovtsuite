# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One undo step for a key held down.

A keyboard shortcut that edits (Edit > Cells: step, +/-1 %, interpolate...)
fires again for every auto-repeat of the key, so holding Alt+Up to raise a
selection by 40 steps made 40 undo steps. From the press that starts it to the
release of the key, the edits of such a shortcut are ONE step
(:meth:`ovtsuite.session.CommandStack.hold`).

:func:`begin` is called by the shortcut's handler, with the undo stack its
edit goes to, before the edit is made. It holds only when a key really is
down -- the shortcut fired within a fraction of a second of a key press -- so
the same action chosen from the menu, or clicked twice, is a step each time.
The hold ends at the first key release that is not an auto-repeat (the key
or a modifier let go), when the application loses the focus, or when no
repeat has come for a while (a release that was never delivered).
"""

from __future__ import annotations

import time
from typing import List

from PySide6 import QtCore, QtWidgets

#: a shortcut fires within this long of the key press that caused it
PRESS_WINDOW = 0.25
#: a hold with no repeat for this long is ended (the release was lost)
EXPIRY_MS = 5000


class _Watch(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self.t_press = 0.0
        self.stacks: List[object] = []
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.end)
        app.installEventFilter(self)

    def eventFilter(self, obj, ev):
        t = ev.type()
        E = QtCore.QEvent.Type
        if t in (E.ShortcutOverride, E.KeyPress):
            self.t_press = time.monotonic()
        elif t == E.KeyRelease:
            if not ev.isAutoRepeat():
                self.end()
        elif t in (E.WindowDeactivate, E.ApplicationDeactivate):
            self.end()
        return False

    def begin(self, stack) -> bool:
        if stack is None or time.monotonic() - self.t_press > PRESS_WINDOW:
            return False
        if not any(s is stack for s in self.stacks):
            stack.hold()
            self.stacks.append(stack)
        self.timer.start(EXPIRY_MS)
        return True

    def end(self) -> None:
        self.timer.stop()
        stacks, self.stacks = self.stacks, []
        for s in stacks:
            try:
                s.release()
            except Exception:                           # noqa: BLE001
                pass


_WATCH = None


def watch() -> _Watch:
    """The application's key watcher (made on first use)."""
    global _WATCH
    app = QtWidgets.QApplication.instance()
    if _WATCH is None or _WATCH.parent() is not app:
        _WATCH = _Watch(app)
    return _WATCH


def begin(stack) -> bool:
    """A shortcut's edit is about to be made into undo stack ``stack``:
    group it with the repeats of the same keypress (True: a key is down)."""
    return watch().begin(stack)


def end() -> None:
    """Close any hold now."""
    if _WATCH is not None:
        _WATCH.end()
