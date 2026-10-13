# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The MCP server's protocol handling and the tool registry -- no binaries,
so fast. Whole-session tool behaviour is in test_ai_tools.py (slow)."""

import json
import os
import subprocess
import sys

from ovtsuite.ai import mcp_server as M
from ovtsuite.ai.tools import Context, Registry, Tool, ToolError, default_registry
from ovtsuite.ai.workspace import FileWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _req(mid, method, params=None):
    d = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        d["params"] = params
    return json.dumps(d)


def _server():
    return M.Server(registry=default_registry(include_modules=False),
                    workspace=FileWorkspace(), live=False)


def test_initialize_negotiates_version():
    s = _server()
    r = json.loads(s.handle_line(_req(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"}})))
    res = r["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "ovt-suite"
    assert "propose_change" in res["instructions"]
    assert s.ctx.client == "mcp:test-client"
    # an unknown revision gets the newest one we speak
    r = json.loads(s.handle_line(_req(2, "initialize", {"protocolVersion": "1999-01-01"})))
    assert r["result"]["protocolVersion"] == M.SUPPORTED[0]


def test_notifications_get_no_reply():
    s = _server()
    assert s.handle_line(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/initialized"})) is None
    assert s.handle_line(json.dumps({"jsonrpc": "2.0", "method": "no/such"})) is None


def test_errors():
    s = _server()
    assert json.loads(s.handle_line("{not json"))["error"]["code"] == M.PARSE_ERROR
    assert json.loads(s.handle_line(_req(3, "no/such")))["error"]["code"] == M.METHOD_NOT_FOUND
    r = json.loads(s.handle_line(_req(4, "tools/call", {"name": "nope"})))
    assert r["error"]["code"] == M.INVALID_PARAMS


def test_tools_list_and_annotations():
    s = _server()
    s.handle_line(_req(1, "initialize", {"protocolVersion": "2025-06-18"}))
    tools = json.loads(s.handle_line(_req(2, "tools/list")))["result"]["tools"]
    names = {t["name"]: t for t in tools}
    for n in ("open_file", "identify", "find_maps", "get_map", "diff",
              "history", "checksum_status", "propose_change", "list_proposals"):
        assert n in names
    assert names["get_map"]["annotations"]["readOnlyHint"] is True
    assert names["propose_change"]["annotations"]["readOnlyHint"] is False
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
    # nothing that writes a binary or flashes is offered
    assert not any(k in n for n in names for k in ("flash", "write", "save"))


def test_tool_errors_are_results_not_crashes():
    s = _server()
    s.handle_line(_req(1, "initialize", {"protocolVersion": "2025-06-18"}))
    r = json.loads(s.handle_line(_req(2, "tools/call",
                                      {"name": "identify", "arguments": {"handle": "h9"}})))
    assert r["result"]["isError"] is True
    assert "nothing is open" in r["result"]["content"][0]["text"]
    r = json.loads(s.handle_line(_req(3, "tools/call",
                                      {"name": "get_map", "arguments": {"handle": 1}})))
    assert r["result"]["isError"] is True          # bad type + missing map
    r = json.loads(s.handle_line(_req(4, "tools/call", {
        "name": "open_file", "arguments": {"path": "/no/such/file.bin"}})))
    assert r["result"]["isError"] and "no such file" in r["result"]["content"][0]["text"]


def test_structured_content_only_on_newer_revisions():
    reg = Registry()
    reg.add(Tool("echo", "echo", lambda ctx, x=1: {"x": x},
                 {"type": "object", "properties": {"x": {"type": "integer"}}}))
    for ver, want in (("2024-11-05", False), ("2025-06-18", True)):
        s = M.Server(registry=reg, workspace=FileWorkspace(), live=False)
        s.handle_line(_req(1, "initialize", {"protocolVersion": ver}))
        r = json.loads(s.handle_line(_req(2, "tools/call",
                                          {"name": "echo", "arguments": {"x": 5}})))
        assert ("structuredContent" in r["result"]) is want
        assert json.loads(r["result"]["content"][0]["text"]) == {"x": 5}


def test_registry_validation_and_tool_error():
    reg = Registry()

    def boom(ctx):
        raise ToolError("readable problem")
    reg.add(Tool("boom", "x", boom))
    ok, r = reg.call("boom", Context(workspace=None))
    assert not ok and r["error"] == "readable problem"
    ok, r = reg.call("boom", Context(workspace=None), {"extra": 1})
    assert not ok and "unknown argument" in r["error"]


def test_stdio_end_to_end(tmp_path):
    """The real entry point over pipes: what an MCP client actually does."""
    msgs = "\n".join([
        _req(1, "initialize", {"protocolVersion": "2025-06-18",
                               "clientInfo": {"name": "pytest"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _req(2, "tools/list"),
        _req(3, "tools/call", {"name": "list_open", "arguments": {}}),
    ]) + "\n"
    env = dict(os.environ, OVT_AI_DIR=str(tmp_path), PYTHONPATH=ROOT)
    p = subprocess.run([sys.executable, "-m", "ovtsuite", "mcp"], input=msgs.encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       cwd=ROOT, env=env, timeout=120)
    lines = [json.loads(x) for x in p.stdout.decode().splitlines() if x.strip()]
    assert [m["id"] for m in lines] == [1, 2, 3], p.stderr.decode()[-2000:]
    assert lines[2]["result"]["isError"] is False
