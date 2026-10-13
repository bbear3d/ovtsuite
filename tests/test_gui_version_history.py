# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Version History window: name checkpoints, merge adjacent ones.

Offscreen Qt over a real session with a few saves. Slow.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import history as H  # noqa: E402

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
PRIMARY = os.path.join(BIN, "038906016AB.bin")


@pytest.fixture
def dlg(tmp_path):
    if not os.path.exists(PRIMARY):
        pytest.skip("reference binary missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    s = TunerSession(PRIMARY)
    out = str(tmp_path / "hist.bin")
    for i in range(3):
        e = s.entities[i]
        s.edit_cells(e, [(0, 0, e.cell(0, 0) + 1)])
        s.save(out)
    d = gui.VersionHistoryDialog(s)
    yield d
    d.close()


def _select(d, rows):
    sm = d.tbl.selectionModel()
    sm.clearSelection()
    for r in rows:
        sm.select(d.tbl.model().index(r, 0),
                  QtCore.QItemSelectionModel.SelectionFlag.Select
                  | QtCore.QItemSelectionModel.SelectionFlag.Rows)


def test_lists_every_checkpoint_newest_first(dlg):
    assert dlg.tbl.rowCount() == 3
    ids = [dlg.tbl.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole)
           for r in range(3)]
    assert ids == [v["id"] for v in H.list_versions(dlg.target)]


def test_typing_a_name_names_the_checkpoint(dlg):
    vid = dlg._versions[1]["id"]
    dlg.tbl.item(1, dlg.NAME).setText("stage 1 timing")
    names = {v["id"]: H.display_label(v) for v in H.list_versions(dlg.target)}
    assert names[vid] == "stage 1 timing"
    assert dlg.tbl.item(1, dlg.NAME).text() == "stage 1 timing"


def test_only_the_name_column_is_editable(dlg):
    for c in range(dlg.tbl.columnCount()):
        editable = bool(dlg.tbl.item(0, c).flags()
                        & QtCore.Qt.ItemFlag.ItemIsEditable)
        assert editable == (c == dlg.NAME)


def test_buttons_follow_the_selection(dlg):
    _select(dlg, [0])
    assert dlg.rename_btn.isEnabled() and dlg.revert_btn.isEnabled()
    assert not dlg.merge_btn.isEnabled()
    _select(dlg, [0, 1])
    assert dlg.merge_btn.isEnabled()
    assert not dlg.revert_btn.isEnabled() and not dlg.rename_btn.isEnabled()


def test_merging_two_adjacent_rows_leaves_one(dlg, monkeypatch):
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("tune A", True)))
    _select(dlg, [0, 1])
    dlg._merge()
    assert dlg.tbl.rowCount() == 2
    assert dlg.tbl.item(0, dlg.NAME).text() == "tune A"
    assert "Merged from 2 saves" in dlg.tbl.item(0, 0).toolTip()


def test_a_gap_in_the_selection_is_explained_not_merged(dlg, monkeypatch):
    told = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("", True)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2])))
    _select(dlg, [0, 2])
    dlg._merge()
    assert dlg.tbl.rowCount() == 3
    assert told and "next to each other" in told[0]
