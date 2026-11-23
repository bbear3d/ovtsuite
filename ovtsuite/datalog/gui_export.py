# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""**Export As...** for a data log: one window with the list of formats, the options the
chosen format has, then the file to write. The formats and their writers are in
:mod:`.export`; this is only the asking."""
import os
import tempfile
from typing import List, Optional

from PySide6 import QtCore, QtWidgets

from .. import recent as _recent
from . import export as _ex
from . import logfile as _lf

_KEY_FORMAT = "export/format"
_KEY_GROUPS = "export/groups"


def _remembered(key, default):
    try:
        return _recent.get_setting(key, default) or default
    except Exception:                                   # noqa: BLE001 -- a convenience
        return default


def _remember(key, value):
    try:
        _recent.set_setting(key, value)
    except Exception:                                   # noqa: BLE001
        pass


class ExportDialog(QtWidgets.QDialog):
    """Which format, and how. :meth:`choice` is ``(format key, Options)`` after OK."""

    def __init__(self, log: "_lf.DataLog", parent=None, key: Optional[str] = None,
                 options: Optional[_ex.Options] = None):
        super().__init__(parent)
        self.setWindowTitle("Export As")
        self.log = log
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("Write this log as:"))
        self.formats = QtWidgets.QListWidget()
        for f in _ex.FORMATS:
            it = QtWidgets.QListWidgetItem(f.title)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, f.key)
            self.formats.addItem(it)
        self.formats.setMaximumHeight(self.formats.sizeHintForRow(0) * len(_ex.FORMATS) + 8)
        v.addWidget(self.formats)
        self.blurb = QtWidgets.QLabel()
        self.blurb.setWordWrap(True)
        self.blurb.setMinimumHeight(self.blurb.fontMetrics().height() * 4)
        v.addWidget(self.blurb)

        self.classic_box = QtWidgets.QGroupBox("Options for the classic layout")
        cv = QtWidgets.QVBoxLayout(self.classic_box)
        self.plan = QtWidgets.QLabel()
        self.plan.setWordWrap(True)
        cv.addWidget(self.plan)
        self.groups_logs = QtWidgets.QRadioButton(
            "One file, one log after another (a viewer asks which log to open)")
        self.groups_files = QtWidgets.QRadioButton("Several files, one for each three groups")
        gg = QtWidgets.QButtonGroup(self)
        for b in (self.groups_logs, self.groups_files):
            gg.addButton(b)
            cv.addWidget(b)
        cv.addSpacing(6)
        cv.addWidget(QtWidgets.QLabel(
            "A switch's bytes in binary (01001001 00001000) are written as they are, "
            "as VCDS does."))
        v.addWidget(self.classic_box)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Choose file...")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        opt = options or _ex.Options(_remembered(_KEY_GROUPS, "logs"))
        (self.groups_logs if opt.groups == "logs" else self.groups_files).setChecked(True)
        want = key or _remembered(_KEY_FORMAT, _ex.DEFAULT_KEY)
        row = next((i for i, f in enumerate(_ex.FORMATS) if f.key == want), 0)
        self.formats.currentRowChanged.connect(self._format_changed)
        self.formats.setCurrentRow(row)
        self._format_changed(row)
        self.resize(560, self.sizeHint().height())

    def _key(self) -> str:
        r = self.formats.currentRow()
        return _ex.FORMATS[r if r >= 0 else 0].key

    def _format_changed(self, _row):
        key = self._key()
        self.blurb.setText(_ex.by_key(key).blurb)
        classic = key == "vcds_classic"
        self.classic_box.setEnabled(classic)
        plan = _ex.classic_plan(self.log)
        many = len(plan) > 1
        self.groups_files.setEnabled(classic and many)
        self.groups_logs.setEnabled(classic and many)
        if many:
            self.plan.setText(
                "This log has %d groups of cells, so it becomes %d logs: %s." % (
                    sum(len(p) for p in plan), len(plan),
                    "; ".join("groups " + ", ".join(str(g) for g in p) for p in plan)))
        else:
            self.plan.setText("This log has %d group(s): it fits one classic log." %
                              len(plan[0]) if plan[0] else "This log has no cells.")

    def choice(self):
        groups = "logs" if self.groups_logs.isChecked() else "files"
        return self._key(), _ex.Options(groups)


def export_log_as(parent, log: "_lf.DataLog", base: str, raw: Optional[bytes] = None
                  ) -> Optional[List[str]]:
    """Ask which format and where, write ``log``; the paths written, or None when the
    user backed out. ``base`` is the log's name (no extension). ``raw`` is the log's own
    file when it has one in this app's layout: that format is then a plain copy."""
    dlg = ExportDialog(log, parent)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    key, opt = dlg.choice()
    fmt = _ex.by_key(key)
    path, _f = QtWidgets.QFileDialog.getSaveFileName(
        parent, "Export data log",
        os.path.join(_recent.get_last_dir("log"), base + fmt.suffix + ".csv"),
        "CSV data log (*.csv);;All files (*)")
    if not path:
        return None
    if not os.path.splitext(path)[1]:
        path += ".csv"
    _remember(_KEY_FORMAT, key)
    _remember(_KEY_GROUPS, opt.groups)
    try:
        if key == "ovt" and raw is not None and log.layout == "kwhack":
            with open(path, "wb") as fh:
                fh.write(raw)
            written = [path]
        else:
            written = _ex.export(path, log, key, opt)
    except OSError as ex:
        QtWidgets.QMessageBox.critical(parent, "Export", str(ex))
        return None
    _recent.set_last_dir(path, "log")
    return written


def export_stored_log(parent, filename: str, raw: bytes) -> Optional[str]:
    """Export a log kept in a project (its bytes): the same dialog. The path written
    (the first, for a log split into files), or None."""
    fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="ovt-export-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        try:
            log = _lf.load(tmp)
        except Exception as ex:                          # noqa: BLE001 -- not a data log
            QtWidgets.QMessageBox.warning(parent, "Export",
                                          "%s is not a data log this app can read: %s" % (
                                              filename, ex))
            return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    got = export_log_as(parent, log, os.path.splitext(filename)[0], raw)
    return got[0] if got else None
