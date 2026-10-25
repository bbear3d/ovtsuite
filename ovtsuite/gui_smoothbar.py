# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A progress bar that moves smoothly.

Work reports how far it is in a few coarse steps (a phase starting, a
phase done). Shown as they come, the bar jumps and then stands still. This
bar eases towards each reported share instead, and while a phase runs --
when the work said where the phase ends (:meth:`set_expect`) -- creeps
towards that end, slowing as it nears it and never reaching it, so it never
has to go back. It repaints on a timer (about 30 times a second at most),
however often the work reports, and only from the GUI thread: connect a
worker's signals to :meth:`set_fraction` / :meth:`set_expect` (queued).

It turns busy (indeterminate) only when the work has said nothing at all
for a while after :meth:`start`."""

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets


class SmoothProgressBar(QtWidgets.QProgressBar):
    #: the bar's steps (fine enough that small shares show)
    STEPS = 1000
    #: repaint interval, ms (~30 per second)
    TICK = 33
    #: how quickly the shown share closes on a reported one, s
    EASE = 0.12
    #: the least speed while easing, share per s
    MIN_SPEED = 0.3
    #: how slowly it creeps towards a phase's end while waiting, s
    CREEP = 1.6
    #: how far into the gap to a phase's end a creep may go at most
    CREEP_MAX = 0.85
    #: no word from the work this long after start: busy, ms
    BUSY_AFTER = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, self.STEPS)
        self.setValue(0)
        # a full-height bar with the percentage in it, in any style: the
        # Windows 11 style draws a progress bar as a thin line whatever the
        # widget's height, so the bar is drawn by a style sheet in the
        # palette's colours (redone when the theme changes)
        self.setTextVisible(True)
        self.setFormat("%p%")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._sheet()
        self._target = 0.0                  # the share reported done
        self._expect = 0.0                  # where the phase under way ends
        self._shown = 0.0                   # the share drawn
        self._heard = True                  # the work has reported since start
        self._last = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.TICK)
        self._timer.timeout.connect(self._tick)
        self._busy_timer = QtCore.QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.timeout.connect(self._go_busy)

    def _sheet(self) -> None:
        pal = self.palette()
        role = QtGui.QPalette.ColorRole
        base = pal.color(role.Base).name()
        edge = pal.color(role.Mid).name()
        fill = pal.color(role.Highlight).name()
        text = pal.color(role.Text).name()
        self.setStyleSheet(
            "QProgressBar { border: 1px solid %s; border-radius: 4px; "
            "background: %s; color: %s; text-align: center; } "
            "QProgressBar::chunk { background: %s; border-radius: 3px; }"
            % (edge, base, text, fill))

    def changeEvent(self, ev):
        if ev.type() == QtCore.QEvent.Type.PaletteChange and                 not getattr(self, "_in_sheet", False):
            self._in_sheet = True           # (the sheet changes the palette)
            try:
                self._sheet()
            finally:
                self._in_sheet = False
        super().changeEvent(ev)

    def fit_height(self, factor: float = 2.0) -> None:
        """A fixed height ``factor`` times the font's: scales with it."""
        self.setFixedHeight(int(round(self.fontMetrics().height() * factor)))

    # --------------------------------------------------------- reporting
    def start(self) -> None:
        """Empty, waiting for the work's first report."""
        self.reset_to(0.0)
        self._heard = False
        self._busy_timer.start(self.BUSY_AFTER)

    def set_fraction(self, f: float) -> None:
        """The share done (0..1); a lower share than before is ignored."""
        f = max(0.0, min(1.0, float(f)))
        self._hear()
        if f > self._target:
            self._target = f
        self._run()

    def set_expect(self, f: float) -> None:
        """The share the phase now under way ends at (0..1)."""
        f = max(0.0, min(1.0, float(f)))
        self._hear()
        if f > self._expect:
            self._expect = f
        self._run()

    def finish(self, animate: bool = True) -> None:
        """Done: full (eased there unless ``animate`` is False)."""
        self._hear()
        self._target = self._expect = 1.0
        if animate:
            self._run()
        else:
            self.reset_to(1.0)

    def reset_to(self, f: float = 0.0) -> None:
        """Shown at ``f`` straight away (no easing): empty after an error,
        empty for the next run."""
        self._timer.stop()
        self._busy_timer.stop()
        self._heard = True
        self.setRange(0, self.STEPS)
        f = max(0.0, min(1.0, float(f)))
        self._target = self._expect = self._shown = f
        self.setValue(int(round(f * self.STEPS)))

    def animating(self) -> bool:
        """Still easing towards a reported share."""
        return self._timer.isActive() and self._shown < self._target

    def target(self) -> float:
        return self._target

    # ---------------------------------------------------------- drawing
    def _hear(self):
        if not self._heard:
            self._heard = True
            self._busy_timer.stop()
        if self.maximum() == 0:             # was busy: a bar again
            self.setRange(0, self.STEPS)
            self.setValue(int(round(self._shown * self.STEPS)))

    def _go_busy(self):
        if not self._heard:
            self.setRange(0, 0)

    def _run(self):
        if not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()

    def _limit(self) -> float:
        """How far a creep may go: part of the way to the phase's end."""
        if self._expect <= self._target:
            return self._target
        return self._target + self.CREEP_MAX * (self._expect - self._target)

    def _tick(self):
        now = time.monotonic()
        dt = max(0.0, min(0.25, now - self._last))
        self._last = now
        s = self._shown
        if s < self._target:
            # eased, with a least speed so it arrives rather than nearing
            # forever
            s += max((self._target - s) * (1.0 - math.exp(-dt / self.EASE)),
                     self.MIN_SPEED * dt)
            if self._target - s < 0.5 / self.STEPS:
                s = self._target
        else:
            lim = self._limit()
            if s < lim:
                s += (lim - s) * (1.0 - math.exp(-dt / self.CREEP))
        if s > self._shown:
            self._shown = s
            v = int(s * self.STEPS + 1e-9)
            if v > self.value():
                self.setValue(v)
        # stop once at the target with nothing (visible) left to creep
        if self._shown >= self._target and \
                (self._limit() - self._shown) * self.STEPS < 1.0:
            self._timer.stop()
