# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger's width must not depend on a project's name or a cable's description:
a long "Keep the log in the project ..." check box (a check box never wraps) once made
the dialog wider than the screen, cutting off the Value columns."""
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
    return app, DataLogDialog(_Win())


def test_a_long_project_name_does_not_widen_the_dialog(monkeypatch):
    from types import SimpleNamespace
    app, d = _dialog()
    d.show()
    app.processEvents()
    pane = d.pane
    before = d.minimumSizeHint().width()
    name = "Audi A6 allroad 2.5TDI 180HP 4Z7907401B fastlog (Audi A6 allroad 2.5TDI 180HP 4Z7907401B 0281010897 366613)"
    ref = SimpleNamespace(doc=SimpleNamespace(title=name), set=SimpleNamespace(name=name))
    monkeypatch.setattr(pane, "_project_ref", lambda: (ref, None))
    pane._sync_destination()
    app.processEvents()
    assert pane.keep_in_project.text().startswith("Keep the log in the project")
    assert name in pane.keep_in_project.toolTip()
    assert d.minimumSizeHint().width() <= before + 40
    d.close()


def test_a_long_cable_description_does_not_widen_the_dialog():
    app, d = _dialog()
    d.show()
    app.processEvents()
    before = d.minimumSizeHint().width()
    pane = d.pane
    pane.conn.port_box.addItem("FTDI:A50285BI — FT232R USB UART, S/N A50285BI (FTDI direct) and more text " * 2, "x")
    app.processEvents()
    assert d.minimumSizeHint().width() <= before + 40
    d.close()


def test_the_cell_table_needs_no_sideways_scroll_at_the_smallest_dialog():
    app, d = _dialog()
    d.show()
    d.resize(d.minimumSizeHint())
    for _ in range(5):
        app.processEvents()
    for table in (d.pane.browse, d.pane.sel):
        hdr = table.horizontalHeader()
        assert table.horizontalScrollBar().maximum() == 0, [hdr.sectionSize(i) for i in range(3)]
        assert hdr.sectionSize(table.columnCount() - 1) >= 100        # the Value column
    d.close()
