# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Load-progress reporting, with no Qt anywhere near it.

Opening a 2 MB EDC16 image is not instant: the family has to be identified by
scoring fingerprints from every knowledge base, then an eight-pass matcher
places several thousand parameters. Before this, all of that happened inside
``TunerSession.__init__`` with nothing coming out, so the window sat frozen
behind a fixed "Identifying ECU…" message for the whole run and there was no
way to tell a slow load from a hung one.

A reporter is a plain ``callable(label, done, total)``: the GUI drives a
``QProgressDialog`` with it, the CLI prints, tests pass nothing. ``Reporter``
is the small piece of bookkeeping in between -- it counts the steps so callers
only have to name them.

Total step counts are per platform (``STEPS``) and deliberately approximate:
being one step out at the end is invisible, whereas a bar that never moves is
the thing being fixed.
"""

from typing import Callable, Optional

from .platforms import PLATFORMS as _PLATFORMS

#: Steps each platform's load reports, for a determinate progress bar (from
#: the base app's platform catalogue, ovtsuite.platforms).
STEPS = {p.id: p.steps for p in _PLATFORMS.values()}


class Reporter:
    """Counts named steps and forwards them to a ``callable(label, done, total)``.

    ``None`` is a valid callback -- every call becomes a no-op -- so callers
    never have to branch on whether anyone is listening.
    """

    def __init__(self, fn: Optional[Callable[[str, int, int], None]] = None,
                 total: int = 1):
        self.fn = fn
        self.total = max(1, total)
        self.done = 0

    def step(self, label: str) -> None:
        """Advance one step and report it."""
        self.done = min(self.done + 1, self.total)
        self._emit(label)

    def note(self, label: str) -> None:
        """Re-label the current step without advancing (sub-progress)."""
        self._emit(label)

    def finish(self, label: str = "") -> None:
        self.done = self.total
        self._emit(label or "Done")

    def _emit(self, label: str) -> None:
        if self.fn is not None:
            self.fn(label, self.done, self.total)

    @classmethod
    def of(cls, fn, platform: str = "edc16") -> "Reporter":
        """A reporter sized for ``platform``. ``fn`` may already be a Reporter,
        in which case it is passed through -- so a function that takes either
        can just call ``Reporter.of(progress)``."""
        if isinstance(fn, cls):
            return fn
        return cls(fn, STEPS.get(platform, 1))
