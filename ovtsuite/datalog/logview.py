# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's Log file tab, off the GUI thread.

Opening a log used to parse the whole CSV and build one QTableWidgetItem per
cell inside the GUI thread: a 9130-row, 13-channel engine log is ~119,000
items, which is what hung the window on Stop (and capped the table at 4000
rows to keep it bearable). Now the file is read on a worker thread
(:class:`LogLoader`) and the table is a view over the parsed rows
(:class:`LogTableModel`): Qt asks for the cells it paints and nothing else,
so every row is shown and opening costs the same for any length.
"""
import threading
from typing import List, Optional

from PySide6 import QtCore

from . import logfile as _lf


class LogTableModel(QtCore.QAbstractTableModel):
    """Read-only rows of a :class:`logfile.DataLog`: "Time (s)" (seconds from
    the first row, three decimals), then each channel's text as logged."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log: Optional[_lf.DataLog] = None
        self._secs: List[float] = []
        self._heads: List[str] = []

    def set_log(self, log: Optional[_lf.DataLog], secs=None):
        self.beginResetModel()
        self._log = log
        self._secs = list(secs) if secs is not None else (log.seconds() if log else [])
        self._heads = ["Time (s)"] + ([c.title for c in log.channels] if log else [])
        self.endResetModel()

    def log(self):
        return self._log

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid() or self._log is None:
            return 0
        return self._log.rows

    def columnCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._heads) if self._log is not None else 1

    def text(self, row: int, col: int) -> str:
        """The cell as shown -- also what the column widths are measured on."""
        if col == 0:
            return "%.3f" % self._secs[row] if row < len(self._secs) else ""
        texts = self._log.texts[row]
        return texts[col - 1] if col - 1 < len(texts) else ""

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or self._log is None:
            return None
        if role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole):
            return self.text(index.row(), index.column())
        return None

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.ToolTipRole and orientation == QtCore.Qt.Horizontal:
            # the channel's whole name: a heading is cut to its column's width
            return self._heads[section] if 0 <= section < len(self._heads) else None
        if role != QtCore.Qt.DisplayRole:
            return None
        if orientation == QtCore.Qt.Horizontal:
            if 0 <= section < len(self._heads):
                return self._heads[section]
            return "Time (s)" if section == 0 else None
        return str(section + 1)

    def flags(self, index):
        if not index.isValid():
            return QtCore.Qt.NoItemFlags
        return QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable


class LoadedLog:
    """What the loader hands back: the log and what the chart needs, all
    computed on the worker so the GUI thread only assigns."""
    __slots__ = ("path", "log", "secs", "columns")

    def __init__(self, path, log, secs, columns):
        self.path, self.log, self.secs, self.columns = path, log, secs, columns


def parse(path) -> LoadedLog:
    """Read ``path`` and precompute the chart's series (any thread)."""
    log = _lf.load(path)
    return LoadedLog(path, log, log.seconds(),
                     [log.column(i) for i in range(len(log.channels))])


class LogLoader(QtCore.QObject):
    """Parses logs on worker threads; only the newest request is answered.

    ``loaded(LoadedLog)`` / ``failed(path, message)`` arrive on the thread
    this object lives in (the GUI thread). A request made while an older one
    is still reading supersedes it: a dual stop asks for two logs back to
    back, and only the second is wanted."""
    loaded = QtCore.Signal(object)
    failed = QtCore.Signal(str, str)
    _done = QtCore.Signal(int, object, str, str)     # token, result, path, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0
        self._done.connect(self._deliver)

    def load(self, path):
        self._token += 1
        token = self._token

        def work():
            try:
                res, err = parse(path), ""
            except Exception as ex:
                res, err = None, str(ex) or ex.__class__.__name__
            try:
                self._done.emit(token, res, path, err)
            except RuntimeError:
                pass                                # the dialog is gone
        threading.Thread(target=work, name="log-view-parse", daemon=True).start()
        return token

    @QtCore.Slot(int, object, str, str)
    def _deliver(self, token, res, path, err):
        if token != self._token:
            return                                  # superseded
        if res is None:
            self.failed.emit(path, err)
        else:
            self.loaded.emit(res)
