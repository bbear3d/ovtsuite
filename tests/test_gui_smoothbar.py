# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The smooth progress bar (offscreen): eased, creeping through a long
step, never going back, busy only with no word at all.
``python -m pytest tests/test_gui_smoothbar.py -q --slow``"""

import os
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _spin(app, secs):
    t = time.time()
    while time.time() - t < secs:
        app.processEvents()
        time.sleep(0.005)


def test_eases_creeps_and_never_goes_back(app):
    from ovtsuite.gui_smoothbar import SmoothProgressBar
    b = SmoothProgressBar()
    seen = []
    b.valueChanged.connect(seen.append)
    b.start()
    b.set_fraction(0.3)
    assert b.value() == 0                     # eased there, not jumped
    _spin(app, 0.6)
    assert b.value() == 300
    eased = [v for v in seen if 0 < v < 300]
    assert len(eased) >= 5
    # a long step said to end at 0.6: creeps on, short of its end
    b.set_expect(0.6)
    _spin(app, 1.0)
    assert 300 < b.value() < 600
    b.set_fraction(0.1)                       # lower: ignored
    _spin(app, 0.1)
    assert b.value() > 300
    b.finish()
    _spin(app, 0.6)
    assert b.value() == b.maximum() and not b.animating()
    assert seen == sorted(seen)
    b.reset_to(0.0)
    assert b.value() == 0


def test_busy_only_with_no_word(app):
    from ovtsuite.gui_smoothbar import SmoothProgressBar
    b = SmoothProgressBar()
    b.start()
    b.set_fraction(0.0)                       # an estimate: a bar
    _spin(app, b.BUSY_AFTER / 1000.0 + 0.2)
    assert b.maximum() == b.STEPS
    b.start()                                  # nothing said
    _spin(app, b.BUSY_AFTER / 1000.0 + 0.2)
    assert b.maximum() == 0
    b.set_fraction(0.5)                        # a bar again
    assert b.maximum() == b.STEPS


def test_height_follows_the_font(app):
    from PySide6 import QtGui
    from ovtsuite.gui_smoothbar import SmoothProgressBar
    b = SmoothProgressBar()
    b.fit_height(2.0)
    h = b.height()
    assert h == round(b.fontMetrics().height() * 2)
    f = QtGui.QFont(b.font())
    f.setPointSizeF(f.pointSizeF() * 2)
    b.setFont(f)
    b.fit_height(2.0)
    assert b.height() > h
