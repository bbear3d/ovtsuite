# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Writing a project's ``.ovt`` on a worker thread while the window waits.

:func:`run` calls a function on a background thread and waits for it in a
local event loop: the window keeps painting (it does not go "Not
responding" on a big project or a slow disk), but takes no keyboard or mouse
input until the write is done, so nothing can change under it. A "Saving..."
box appears when the write takes longer than a moment. The caller gets the
function's result, or its exception, exactly as if it had been called
directly -- so a refused save (changed on disk) and a failed one are handled
as before.

Whatever the worker reads must have been frozen first on the GUI thread
(:meth:`ovtsuite.project.Project.prepare_save`); the worker never touches Qt.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

from PySide6 import QtCore, QtWidgets

T = TypeVar("T")

#: the "Saving..." box shows only for a write that takes longer than this
SHOW_AFTER_MS = 400
#: how often the waiting loop looks whether the worker is done
POLL_MS = 15


def saving(win) -> bool:
    """A project of ``win`` is being written right now."""
    return bool(win is not None and win.__dict__.get("_ovt_saving"))


def run(win, fn: Callable[[], T], text: str = "Saving project...") -> T:
    """``fn()`` on a worker thread; the GUI waits without blocking (see the
    module). Called off the GUI thread, or with no application, it simply
    calls ``fn``."""
    app = QtWidgets.QApplication.instance()
    if app is None or QtCore.QThread.currentThread() is not app.thread():
        return fn()
    box: dict = {}

    def work():
        try:
            box["result"] = fn()
        except BaseException as ex:         # handed to the GUI thread
            box["error"] = ex

    # not a daemon: an exit while it runs still waits for the file
    worker = threading.Thread(target=work, name="ovt-save")
    loop = QtCore.QEventLoop()
    poll = QtCore.QTimer()
    poll.setInterval(POLL_MS)
    poll.timeout.connect(lambda: worker.is_alive() or loop.quit())
    dlg = _dialog(win, text)
    show = QtCore.QTimer()
    show.setSingleShot(True)
    show.timeout.connect(dlg.show)
    d = win.__dict__ if win is not None else {}
    d["_ovt_saving"] = d.get("_ovt_saving", 0) + 1
    try:
        worker.start()
        poll.start()
        show.start(SHOW_AFTER_MS)
        # user input waits (Qt keeps it) until the write is done
        loop.exec(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        worker.join()                   # (the loop also ends on app exit)
    finally:
        poll.stop()
        show.stop()
        dlg.hide()
        dlg.deleteLater()
        d["_ovt_saving"] -= 1
    if "error" in box:
        raise box["error"]
    return box.get("result")


class SavingDialog(QtWidgets.QDialog):
    """A "Saving..." box with a busy bar and no buttons -- a write cannot
    stop half-way."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Saving")
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint, False)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(text))
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 0)                  # busy
        bar.setTextVisible(False)
        lay.addWidget(bar)
        self.setMinimumWidth(320)

    def reject(self):
        pass                                # Esc does not close it


def _dialog(win, text) -> SavingDialog:
    return SavingDialog(text, win)
