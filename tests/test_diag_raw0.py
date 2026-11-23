# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Block 000 of a distributor-pump TDI (diag/raw0) and the timing-graph
procedures: the decode matches the workshop manual's own byte = value pairs,
and the live step plots and checks against its limits."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.diag import defs as D
from ovtsuite.diag import procedures as P
from ovtsuite.diag import raw0 as R
from ovtsuite.diag.kw1281 import GroupReading


# the manual's "displayed value -> corresponds to" pairs (AGR/AHF/ALH/ASV)
@pytest.mark.parametrize("field,byte,value,tol", [
    (2, 137, 8.0, 0.1), (2, 215, 14.0, 0.2), (2, 12, -2.0, 0.1), (2, 75, 3.0, 0.1),
    (7, 80, 80.0, 0.5), (7, 35, 110.0, 0.5), (8, 182, 10.0, 0.5), (8, 50, 100.0, 0.5),
    (9, 91, 20.0, 0.5), (9, 201, 80.0, 1.2), (6, 183, 910.0, 2), (6, 224, 1110.0, 4),
    (4, 199, 39.8, 0.01), (4, 214, 42.8, 0.01), (3, 255, 100.0, 0.01),
    (10, 69, 230.0, 1), (10, 111, 370.0, 1), (1, 138, 2900.0, 30), (1, 148, 3100.0, 30),
])
def test_decode_agrees_with_the_manual(field, byte, value, tol):
    assert abs(R.VE_FIELDS[field - 1].value(byte) - value) <= tol


IDLE_OK = bytes([43, 40, 0, 20, 95, 200, 60, 120, 150, 90])


def test_check_against_the_manual():
    res = R.check(IDLE_OK, "ahf", "idle")
    assert all(ok for _i, _f, _b, ok, _s in res)
    late = bytes([43, 120, 0, 20, 95, 200, 60, 120, 150, 90])   # 6.6 deg BTDC
    bad = {i for i, _f, _b, ok, _s in R.check(late, "ahf", "idle") if ok is False}
    assert bad == {2}
    assert R.check(late, "agp", "idle")[1][3] is True           # AGP/AQM allow 7 deg
    assert R.check(IDLE_OK, "agp", "idle")[4][3] is None        # manifold pressure: ignore
    x0, x1 = R.value_band("ahf", "idle", 7)
    assert 79 < x0 < 80 and 110 < x1 < 111


class _Sess:
    ident = None
    addr = "01"

    def __init__(self, reads):
        self.reads = list(reads)

    def read_group_full(self, group):
        assert group == 0
        raw = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        return GroupReading(group=0, raw=raw)

    basic_setting_read = read_group_full


class _Pr(P.Prompter):
    def __init__(self, n=4):
        self.n, self.plot, self.shown, self.rows = n, None, [], []

    def choose(self, step_id, label, options, default=None):
        return "ahf"

    def live(self, title, read, seconds, text="", plot=None):
        self.plot = plot
        for _ in range(self.n):
            self.rows.append(read())
        return True

    def show(self, text, rows=None):
        self.shown.append((text, rows))


def _doc(pid):
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    doc = next(d for d in P.available(res, None) if d.get("id") == pid)
    doc = dict(doc)
    doc["steps"] = [s for s in doc["steps"] if "check" not in s]    # no engine to ask
    return doc


def test_the_timing_graph_procedure_plots_and_checks():
    pr = _Pr()
    run = P.ProcedureRun(_doc("procedure.engine.ve-timing-idle"), _Sess([IDLE_OK]), None, pr).run()
    assert run.ok, run.message
    assert pr.plot["x"] == 7 and pr.plot["y"] == 2 and "Coolant" in pr.plot["xlabel"]
    x0, x1, y0, y1 = pr.plot["band"]
    assert 79 < x0 < 80 and abs(y0 + 2.08) < 0.01 and abs(y1 - 2.96) < 0.01
    row = pr.rows[-1]
    assert row[1][4] == pytest.approx((40 - 38) * 0.08)              # a number to plot
    assert run.values["check.ok"] is True
    assert "within the specification" in pr.shown[-1][0]


def test_full_throttle_takes_the_reading_in_the_speed_window():
    full_ok = bytes([143, 170, 255, 205, 200, 200, 60, 120, 150, 255])
    slow = bytes([100, 90, 255, 205, 200, 200, 60, 120, 150, 255])      # below 2900 rpm
    pr = _Pr(n=4)
    run = P.ProcedureRun(_doc("procedure.engine.ve-group0-full"),
                         _Sess([slow, full_ok, slow, slow]), None, pr).run()
    assert run.ok, run.message
    assert run.values["check.ok"] is True                    # the 3000 rpm reading
    pr = _Pr(n=2)
    P.ProcedureRun(_doc("procedure.engine.ve-group0-full"), _Sess([slow]), None, pr).run()
    assert "never in the specified range" in pr.shown[-1][0]


def test_the_view_draws_the_plot(qapp=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.diag.gui_procedure import PromptView
    v = PromptView()
    v.apply("live_start", {"title": "t", "plot": {"x": 7, "y": 2, "xlabel": "C",
                                                  "ylabel": "deg", "band": [80, 110, -2, 3]}})
    assert not v.plot.isHidden()
    rows = [["000.%d" % i, "", "", "", float(i)] for i in range(1, 11)]
    v.apply("live_rows", {"rows": rows, "left": 1})
    assert v.plot.points == [(7.0, 2.0)]
    assert v.plot.inside(95, 1) and not v.plot.inside(95, 5)
    v.resize(500, 300)
    v.plot.grab()                                            # paints without error
    v.apply("live_start", {"title": "t"})
    assert v.plot.isHidden()


def test_the_graph_procedures_read_until_finished():
    """No least or longest time: the live step reads until Finish."""
    for pid in ("procedure.engine.ve-timing-idle", "procedure.engine.ve-group0-full"):
        live = next(s["live"] for s in _doc(pid)["steps"] if "live" in s)
        assert P._live_seconds(live["seconds"]) == 0
    assert P._live_seconds(None) == 15 and P._live_seconds(30) == 30
    with pytest.raises(P.ProcedureError):
        P._live_seconds("soon")


def test_an_open_ended_live_step_runs_until_finish():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.diag.gui_procedure import GuiPrompter, ProcedureDialog
    pr = GuiPrompter()
    seen, reads = [], []
    pr.ask.connect(lambda k, p: seen.append((k, p)))
    pr.ask.connect(lambda k, p: k == "live_rows" and len(reads) >= 5 and pr.answer("skip"))
    assert pr.live("t", lambda: reads.append(1) or [["000.1", "", "", "", 1.0]], 0) is True
    assert len(reads) == 5                                   # as long as it was let run
    start = next(p for k, p in seen if k == "live_start")
    assert start["open"] is True
    assert all(p["left"] is None for k, p in seen if k == "live_rows")
    d = ProcedureDialog({"name": "x", "steps": []}, "01")
    d._on_ask("live_start", {"title": "t", "open": True})
    assert d.skip_btn.text() == "Finish" and "readings" not in d.countdown.text()
    d._on_ask("live_rows", {"rows": [], "n": 3, "elapsed": 2.0, "left": None})
    assert "3 readings" in d.countdown.text()
    d._on_ask("live_start", {"title": "t", "seconds": 10})
    assert d.skip_btn.text() == "Skip the rest of this step"
    d.close()
