# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The messages of an EDC15 fast-logging session, kept with the project.

The session's messages (the connect steps, the engine-running ``10 89`` refusal, the
slow-row reports, the totals at the end) only ever reached the logger's one-line status
label, where each replaced the last. :class:`SessionNotes` collects them while the
session runs; :func:`keep` puts the text into the project the binary came from, as an
attachment (Project panel > Attachments), or :func:`write_beside` writes it next to a
log that was recorded to a file.
"""
import os
import time
from datetime import datetime
from typing import List, Optional, Tuple

#: How often the repeating rate line (``... @ 3.1 Hz (...)``, emitted twice a second)
#: is kept: it is the history of the rate and the request statistics, not a message.
RATE_EVERY = 5.0
#: A session is a few hundred lines; a very long one stops collecting here.
MAX_LINES = 20000


def is_rate_line(msg: str) -> bool:
    return " @ " in msg and "Hz (" in msg


class SessionNotes:
    """Collects the "EDC15-fast" messages of one connection."""

    def __init__(self):
        self.reset()

    def reset(self, header=()):
        self.lines: List[Tuple[datetime, str]] = []
        self.header = list(header)
        self.started = datetime.now()
        self._last_rate = float("-inf")
        self._full = False

    def __bool__(self):
        return bool(self.lines)

    def add(self, msg: str, now: Optional[float] = None) -> None:
        if not msg.startswith("EDC15-fast"):
            return
        now = time.monotonic() if now is None else now
        if is_rate_line(msg):
            if now - self._last_rate < RATE_EVERY:
                return
            self._last_rate = now
        if len(self.lines) >= MAX_LINES:
            if not self._full:
                self._full = True
                self.lines.append((datetime.now(), "(too many messages: the rest is not kept)"))
            return
        self.lines.append((datetime.now(), msg))

    def filename(self) -> str:
        return "fastlog_session_%s.txt" % self.started.strftime("%Y%m%d_%H%M%S")

    def text(self) -> str:
        out = ["EDC15 fast-logging session, started %s" % self.started.strftime("%Y-%m-%d %H:%M:%S")]
        out += self.header
        out.append("")
        for when, msg in self.lines:
            body = msg[len("EDC15-fast:"):].strip() if msg.startswith("EDC15-fast:") else msg
            out.append("%s  %s" % (when.strftime("%H:%M:%S.%f")[:-3], body))
        return "\n".join(out) + "\n"

    def take(self) -> Optional[Tuple[str, bytes]]:
        """``(filename, bytes)`` of what was collected, and start afresh; None when
        there is nothing to keep (so a second call, or a session of another ECU, keeps
        nothing)."""
        if not self.lines:
            return None
        got = (self.filename(), self.text().encode("utf-8"))
        self.reset(self.header)
        return got


def keep(window, ws, filename: str, data: bytes, parent=None, done=None):
    """Put ``data`` into the project tab ``ws`` came from as an attachment, with no
    question asked. True when it went in, False when there is no project (or it
    failed), None when the standalone logger is writing it into the .ovt on a worker
    thread -- ``done(kept)`` follows on the GUI thread."""
    ref = getattr(ws, "project_ref", None)
    if ref is None or not data:
        return False
    later = getattr(window, "keep_attachment_async", None)
    if later is not None:                   # the standalone logger
        return later(ws, filename, data, parent, done)
    try:
        from .. import gui_project as _gp
        ref.doc.project.add_attachment(filename, bytes(data), set_id=ref.sid)
        for t in ref.doc.tabs(ws.win):
            ws.win._workspace_changed(t)
        _gp.refresh_panel(ws.win)
        _gp.autosave_soon(ws.win, ref.doc)
    except Exception:                       # noqa: BLE001
        return False
    return True


def write_beside(log_path: str, filename: str, data: bytes) -> Optional[str]:
    """The notes as a file in the folder of the log recorded to a file; its path, or
    None when it could not be written."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(log_path)), filename)
        with open(path, "wb") as fh:
            fh.write(data)
        return path
    except OSError:
        return None
