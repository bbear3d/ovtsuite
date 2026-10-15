# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Project panel > Compare with project (ovtsuite.gui_projcompare)."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import gui_projcompare as G  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture
def world(tmp_path, monkeypatch):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui_project, recent
    paths = {}
    for n in ("mine", "other", "older", "gone"):
        paths[n] = str(tmp_path / ("%s.ovt" % n))
        if n != "gone":
            open(paths[n], "wb").close()
    mine = SimpleNamespace(path=paths["mine"], title="mine")
    other = SimpleNamespace(path=paths["other"], title="Other")
    win = SimpleNamespace()
    monkeypatch.setattr(gui_project, "docs", lambda w: [mine, other])
    monkeypatch.setattr(recent, "load_recent_projects", lambda prune=True: [
        paths["other"], paths["older"], paths["mine"], paths["gone"]])
    return win, mine, paths


def test_offers_other_projects_once_never_its_own(world):
    win, mine, paths = world
    got = G.candidates(win, mine)
    assert got == [("Other  (open)", paths["other"]), ("older", paths["older"])]


def test_picking_one_opens_the_branch_and_compares_it(world, monkeypatch):
    win, mine, paths = world
    from ovtsuite import gui_files, gui_project
    tab = object()
    seen = []
    monkeypatch.setattr(gui_project, "open_branch",
                        lambda w, d, sid, bid: seen.append(("open", sid, bid))
                        or tab)
    monkeypatch.setattr(gui_files, "compare_with_file",
                        lambda w, p, ws=None: seen.append(("cmp", p, ws))
                        or True)
    m = QtWidgets.QMenu()
    sub = G.add_menu(m, win, mine, "s1", "b1")
    texts = [a.text() for a in sub.actions() if not a.isSeparator()]
    assert texts == ["Other  (open)", "older", "Browse..."]
    sub.actions()[1].trigger()
    assert seen == [("open", "s1", "b1"), ("cmp", paths["older"], tab)]
