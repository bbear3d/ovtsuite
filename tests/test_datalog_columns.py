# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's cell tables: a narrow check-box Log column, columns resized
by dragging their headings, and the full channel name as a tooltip (a long
name is cut short in its cell)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


def _dialog():
    from ovtsuite import gui as _g
    if not _g.HAVE_QT:
        pytest.skip("PySide6 not available")
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.datalog.gui_log import DataLogDialog

    class _Win(QtWidgets.QMainWindow):
        primary = None
    d = DataLogDialog(_Win())
    d.conn.ecu_box.setCurrentText("ZF6HP")       # names cells with no binary
    return app, d


def test_columns_are_resizable_and_the_log_column_is_narrow():
    from PySide6 import QtWidgets
    _app, d = _dialog()
    Interactive = QtWidgets.QHeaderView.Interactive
    for table in (d.pane.browse, d.pane.sel):
        hdr = table.horizontalHeader()
        assert all(hdr.sectionResizeMode(i) == Interactive
                   for i in range(table.columnCount()))
        assert hdr.stretchLastSection()
    log_w = d.pane.browse.horizontalHeader().sectionSize(0)
    assert log_w < 64                                # a check box, no more
    assert d.pane.browse.horizontalHeader().sectionSize(1) >= 200


def test_cell_names_carry_their_full_text_as_a_tooltip():
    _app, d = _dialog()
    assert d.pane.browse.rowCount() > 0
    for r in range(d.pane.browse.rowCount()):
        name = d.pane.browse.item(r, 1)
        assert name.toolTip().split("\n")[0] == name.text()
    for i in range(1, d.preset_box.count()):
        d.preset_box.setCurrentIndex(i)
        d._apply_preset()
        if d._channels:
            break
    assert d.pane.sel.rowCount() == len(d._channels) > 0
    for r in range(d.pane.sel.rowCount()):
        it = d.pane.sel.item(r, 1)
        assert it.toolTip().split("\n")[0] == it.text()


def test_refreshing_the_selection_keeps_dragged_widths():
    _app, d = _dialog()
    d.pane.sel.horizontalHeader().resizeSection(1, 333)
    for i in range(1, d.preset_box.count()):
        d.preset_box.setCurrentIndex(i)
        d._apply_preset()
    assert d.pane.sel.horizontalHeader().sectionSize(1) == 333


def test_log_viewer_headings_have_the_channel_name_as_tooltip():
    from PySide6 import QtCore
    from ovtsuite.datalog import logfile as lf
    from ovtsuite.datalog.logview import LogTableModel
    from datetime import datetime
    log = lf.DataLog(channels=[lf.Channel(1, 0, "A rather long channel name (Sig_x)")],
                     stamps=[datetime(2026, 1, 1)], values=[[1.0]])
    m = LogTableModel()
    m.set_log(log)
    assert m.headerData(1, QtCore.Qt.Horizontal, QtCore.Qt.ToolTipRole) \
        == "A rather long channel name (Sig_x)"
    assert m.headerData(0, QtCore.Qt.Horizontal, QtCore.Qt.ToolTipRole) == "Time (s)"


def test_every_column_is_in_view_at_the_dialog_s_size():
    """The Cell column fills the room the pane has rather than a fixed
    width, so Log, Cell and Value all show without scrolling sideways."""
    app, d = _dialog()
    d.resize(1022, 792)
    d.show()
    for _ in range(5):
        app.processEvents()
    for table in (d.pane.browse, d.pane.sel):
        hdr = table.horizontalHeader()
        used = sum(hdr.sectionSize(i) for i in range(table.columnCount()))
        assert used <= table.viewport().width() + 1, [hdr.sectionSize(i) for i in range(3)]
        assert hdr.sectionSize(table.columnCount() - 1) >= 60
        assert hdr.sectionSize(1) >= 100
    d.close()
