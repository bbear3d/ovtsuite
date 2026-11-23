# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The parameter filter takes a coding as a scan tool shows it (P447)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
MP = os.path.join(BIN, "03G906021MP_1322_touran_ori.bin")
Q = os.path.join(BIN, "038906016Q.Bin")
needs_ref = pytest.mark.skipif(not os.path.exists(MP) or not os.path.exists(Q),
                               reason="reference files not present")


def _window(path):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from ovtsuite.gui import MainWindow
    from ovtsuite.session import TunerSession
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(TunerSession(path), None)
    app.processEvents()
    return app, w


def _gear_rows(w):
    ws = getattr(w, "ws", None) or w
    main = next(e for e in w.primary.entities if e.name == "Gearbx_rTrans1_C")
    copy = next(e for e in w.primary.variant_entities()
                if e.name.startswith("Gearbx_rTrans1_C "))
    return ws, ws._item_of[id(main)], ws._item_of[id(copy)]


@pytest.mark.slow
@needs_ref
def test_a_scan_tool_coding_filters_to_its_variants_copy():
    """03G906021MP: variant 3 (and 19) read the main block's gear ratios,
    variants 2 and 18 the second copy. A car's coding as a scan tool shows it
    carries function bits on top of the variant (786437, 524290): the filter
    keeps the copy that coding selects and no other."""
    app, w = _window(MP)
    ws, main, copy = _gear_rows(w)
    try:
        ws.filter.setText("coding 786437")
        app.processEvents()
        assert not main.isHidden() and copy.isHidden()
        ws.filter.setText("coding 0524290")             # padded, as a tool does
        app.processEvents()
        assert main.isHidden() and not copy.isHidden()
        ws.filter.setText("4980737")                     # variant 19, a bare number
        app.processEvents()
        assert not main.isHidden() and copy.isHidden()
        ws.filter.setText("coding 786438")
        app.processEvents()
        assert not main.isHidden()
        ws.filter.setText("")
        app.processEvents()
        assert not main.isHidden() and not copy.isHidden()
    finally:
        w.close()


@pytest.mark.slow
@needs_ref
def test_the_filter_does_not_round_a_p350_coding():
    """038906016Q holds its coding as shown: 158031 finds the manual copy,
    158032 finds nothing."""
    app, w = _window(Q)
    ws, main, copy = _gear_rows(w)
    try:
        ws.filter.setText("coding 158031")
        app.processEvents()
        assert main.isHidden() and not copy.isHidden()
        ws.filter.setText("coding 158032")
        app.processEvents()
        assert main.isHidden() and copy.isHidden()
    finally:
        w.close()
