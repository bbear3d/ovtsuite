# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The live bridge between the running app and ``ovtsuite mcp``: discovery
file, token check, call routing, and falling back to files when the app is
gone. No binaries, no Qt -- fast."""

import json
import os
import socket

import pytest

from ovtsuite.ai import bridge as B
from ovtsuite.ai import mcp_server as M
from ovtsuite.ai import proposals as P
from ovtsuite.ai.tools import Registry, Tool
from ovtsuite.ai.workspace import FileWorkspace


class _LiveWS(FileWorkspace):
    live = True

    def is_tab(self, h):
        return str(h).startswith("t")

    def view(self):
        return {"live": True, "active_tab": "t1"}


@pytest.fixture
def aidir(tmp_path, monkeypatch):
    monkeypatch.setenv(B.ENV_DIR, str(tmp_path))
    monkeypatch.setenv(P.ENV_DIR, str(tmp_path))
    return tmp_path


def _registry(calls):
    reg = Registry()

    def where(ctx, handle=None):
        calls.append(("where", handle, ctx.client))
        return {"live": bool(getattr(ctx.workspace, "live", False))}
    reg.add(Tool("where", "x", where,
                 {"type": "object", "properties": {"handle": {"type": "string"}}}))
    return reg


def test_discovery_file_and_token(aidir):
    calls, gui = [], []
    srv = B.BridgeServer(_LiveWS(), registry=_registry(calls),
                         run_on_gui=lambda fn: (gui.append(1), fn())[1])
    srv.start()
    try:
        live = B.read_live()
        assert live["port"] == srv.port and live["token"] == srv.token
        if os.name == "posix":
            assert oct(os.stat(B.live_path()).st_mode & 0o777) == "0o600"
        # wrong token: refused
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        s.sendall(b'{"hello":"nope"}\n')
        assert json.loads(s.makefile("rb").readline())["ok"] is False
        s.close()
        c = B.BridgeClient(client="pytest")
        assert c.connect()
        ok, r = c.call("where", {"handle": "t1"})
        assert ok and r == {"live": True}
        assert gui                                   # a tab call ran on the GUI thread
        n = len(gui)
        ok, r = c.call("where", {"handle": "h1"})
        assert ok and len(gui) == n                  # a disk target did not
        assert calls[-1][2] == "mcp:pytest"
        ok, r = c.call("nope", {})
        assert not ok and "no tool" in r["error"]
        c.close()
    finally:
        srv.stop()
    assert B.read_live() is None                     # removed on stop


def test_mcp_server_routes_to_app_and_falls_back(aidir):
    calls = []
    srv = B.BridgeServer(_LiveWS(), registry=_registry(calls))
    srv.start()
    m = M.Server(registry=_registry(calls), workspace=FileWorkspace())
    m.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "pytest"}}}))
    req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "where", "arguments": {}}})
    r = json.loads(m.handle_line(req))
    assert json.loads(r["result"]["content"][0]["text"]) == {"live": True}
    srv.stop()                                       # the app closes
    r = json.loads(m.handle_line(req))
    assert json.loads(r["result"]["content"][0]["text"]) == {"live": False}


def test_stale_discovery_file_means_files(aidir):
    B._write_private(B.live_path(), {"port": 1, "token": "x"})   # nothing listens
    calls = []
    m = M.Server(registry=_registry(calls), workspace=FileWorkspace())
    r = json.loads(m.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "where", "arguments": {}}})))
    assert json.loads(r["result"]["content"][0]["text"]) == {"live": False}


def test_proposal_matching_rules():
    prop = {"target": {"kind": "project-set", "project_uid": "u", "set_id": "s",
                       "branch_id": "b1", "branch_name": "stage 1",
                       "base_checkpoint": "c1"}}
    same = {"kind": "project-set", "project_uid": "u", "set_id": "s",
            "branch_id": "b1", "base_checkpoint": "c1"}
    assert P.match(prop, same) == (True, "")
    moved = dict(same, base_checkpoint="c2")
    assert P.match(prop, moved)[0] and "earlier version" in P.match(prop, moved)[1]
    other = dict(same, branch_id="b2", branch_name="stock")
    assert P.match(prop, other)[0] and "stage 1" in P.match(prop, other)[1]
    assert P.match(prop, dict(same, set_id="x"))[0] is False
    fprop = {"target": {"kind": "file", "path": "/a/b.bin", "sha256": "1"}}
    assert P.match(fprop, {"kind": "file", "path": "/a/b.bin", "sha256": "1"}) == (True, "")
    assert "changed" in P.match(fprop, {"kind": "file", "path": "/a/b.bin", "sha256": "2"})[1]
    assert P.match(fprop, {"kind": "file", "path": "/c.bin", "sha256": "1"})[0]
    assert P.match(fprop, {"kind": "file", "path": "/c.bin", "sha256": "3"})[0] is False


def test_resolve_keeps_a_record(aidir):
    P.save({"id": "abc123", "status": P.PENDING, "title": "t", "target": {},
            "patch": {"entries": []}})
    assert [p["id"] for p in P.all_pending()] == ["abc123"]
    P.resolve("abc123", "rejected", "not wanted")
    assert P.all_pending() == []
    rec = json.load(open(os.path.join(P.inbox_dir(), "rejected", "abc123.json")))
    assert rec["status"] == "rejected" and rec["resolution"] == "not wanted"
