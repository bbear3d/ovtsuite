# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The live workspace over open tabs, end to end through the bridge and the
MCP server, with real sessions (slow) but no Qt: the tabs are stand-ins with
the attributes the live workspace reads."""

import json
import os

import pytest

from ovtsuite.ai import bridge as B
from ovtsuite.ai import mcp_server as M
from ovtsuite.ai import proposals as P
from ovtsuite.ai.live import LiveWorkspace

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "Reference", "Binaries", "038906016Q.Bin")
TUNED = os.path.join(ROOT, "Reference", "Binaries", "038906016Q-Stg2-MPPS Read.Bin")
SMOKE = "FlMng_qPresSmoke_MAP"

if not (os.path.isfile(BIN) and os.path.isfile(TUNED)):
    pytest.skip("reference binaries not present", allow_module_level=True)


class Tab:
    def __init__(self, primary, comparison=None):
        self.primary, self.comparison = primary, comparison
        self.project_ref = None
        self.cmp_version = None
        self.cmp_link = None
        self.selected = None

    def tab_label(self):
        return self.primary.binfile.name

    def is_dirty(self):
        return bool(self.primary.modified)

    def _current_entity(self):
        return self.selected

    def tables(self):
        return []


class Win:
    def __init__(self, tabs):
        self.tabs = tabs

    def workspaces(self):
        return list(self.tabs)

    @property
    def current(self):                     # a property, as on MainWindow
        return self.tabs[0] if self.tabs else None


@pytest.fixture(scope="module")
def sessions():
    from ovtsuite.session import TunerSession
    return TunerSession(BIN), TunerSession(TUNED)


@pytest.fixture
def live(sessions, tmp_path, monkeypatch):
    monkeypatch.setenv(B.ENV_DIR, str(tmp_path))
    monkeypatch.setenv(P.ENV_DIR, str(tmp_path))
    prim, cmp = sessions
    tab = Tab(prim, cmp)
    tab.selected = next(e for e in prim.entities if e.name == SMOKE)
    win = Win([tab])
    ws = LiveWorkspace(win)
    srv = B.BridgeServer(ws)
    srv.start()
    m = M.Server(workspace=None)
    m.handle_line(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "pytest"}}}))
    yield m, win, tab, ws
    srv.stop()


def call(m, name, **args):
    r = json.loads(m.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}})))["result"]
    data = json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"]
    return (not r["isError"]), data


def test_tabs_are_targets(live):
    m, win, tab, ws = live
    ok, r = call(m, "list_open")
    assert ok and r["live"] is True
    (t,) = r["open"]
    assert t["handle"] == "t1" and t["tab"] is True and t["unsaved_edits"] is False
    ok, v = call(m, "current_view")
    assert ok and v == {"live": True, "active_tab": "t1", "tabs": ["t1"],
                        "selected_map": SMOKE, "open_tables": []}
    ok, g = call(m, "get_map", handle="t1", map=SMOKE)
    assert ok and g["comparison"] is not None
    ok, d = call(m, "diff", handle="t1", limit=3)
    assert ok and d["changed"] > 0
    ok, e = call(m, "set_comparison", handle="t1", path=TUNED)
    assert not ok and "in the app" in e


def test_unsaved_edits_are_seen(live):
    m, win, tab, ws = live
    e = tab.selected
    before = e.values()
    tab.primary.edit_cells(e, [(0, 0, before[0][0] + 1)])
    try:
        ok, g = call(m, "get_map", handle="t1", map=SMOKE, comparison=False)
        assert g["values"][0][0] == pytest.approx(before[0][0] + 1, abs=0.02)
        ok, r = call(m, "list_open")
        assert r["open"][0]["unsaved_edits"] is True
    finally:
        tab.primary.undo()


def test_proposal_for_a_tab_and_closed_tab(live):
    m, win, tab, ws = live
    ok, r = call(m, "propose_change", handle="t1", title="smoke +1", rationale="r",
                 changes=[{"map": SMOKE, "add": 1}])
    assert ok, r
    prop = P.load(r["proposal"])
    assert prop["client"] == "mcp:pytest" and prop["target"]["tab"] is True
    ident = ws.get("t1").identity()
    assert P.match(prop, ident) == (True, "")
    ok, lst = call(m, "list_proposals", handle="t1")
    assert [p["id"] for p in lst["proposals"]] == [r["proposal"]]
    win.tabs.clear()                                  # the tab is closed
    ok, err = call(m, "identify", handle="t1")
    assert not ok and "no open tab" in err


def test_disk_files_still_open_beside_tabs(live):
    m, win, tab, ws = live
    ok, r = call(m, "open_file", path=TUNED)
    assert ok and r["opened"][0]["handle"].startswith("h")
    ok, r = call(m, "list_open")
    assert [t["handle"][0] for t in r["open"]] == ["t", "h"]
