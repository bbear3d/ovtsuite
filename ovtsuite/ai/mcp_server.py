# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT-Suite as an MCP server (Model Context Protocol), over stdio.

Hand-written JSON-RPC 2.0 -- newline-delimited messages on stdin/stdout --
so it needs nothing beyond the standard library and runs on Python 3.9 (the
official MCP SDK needs 3.10). Any MCP client can launch it: Claude, ChatGPT,
Cursor, VS Code, LM Studio, ...::

    ovtsuite mcp            (from a source/pip install)
    OVTMcp.exe              (Windows bundle: a console launcher)

stdout carries protocol messages only. Anything else the app prints while
loading a binary is sent to stderr, which clients show as the server's log.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .tools import Context, Registry, default_registry, to_text

#: Protocol revisions this server speaks, newest first.
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: First revision with ``structuredContent`` in tool results.
_STRUCTURED_FROM = "2025-06-18"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = (
    -32700, -32600, -32601, -32602, -32603)


class Server:
    def __init__(self, registry: Optional[Registry] = None, workspace=None,
                 log=None, live: bool = True):
        if workspace is None:
            from .workspace import FileWorkspace
            workspace = FileWorkspace()
        self.registry = registry or default_registry()
        self.ctx = Context(workspace=workspace, client="mcp")
        self.version = SUPPORTED[0]
        self.initialized = False
        self._log = log or sys.stderr
        #: forwards tool calls to the running app (its open tabs); None: files only
        self.bridge = None
        if live:
            from .bridge import BridgeClient
            self.bridge = BridgeClient()
        self._was_live = None

    # -- message handling --------------------------------------------------------
    def handle_line(self, line: str) -> Optional[str]:
        """One input line -> the line to write back (None for notifications)."""
        line = line.strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError as ex:
            return _dump(_error(None, PARSE_ERROR, "parse error: %s" % ex))
        if isinstance(msg, list):                    # 2025-03-26 batches
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return _dump(out) if out else None
        r = self.handle(msg)
        return _dump(r) if r is not None else None

    def handle(self, msg: Any) -> Optional[dict]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:
            return None                              # a response to us: none expected
        params = msg.get("params") or {}
        is_note = "id" not in msg
        fn = getattr(self, "_m_" + method.replace("/", "_"), None)
        if fn is None:
            return None if is_note else _error(mid, METHOD_NOT_FOUND,
                                               "method not found: %s" % method)
        try:
            result = fn(params)
        except _RpcError as ex:
            return None if is_note else _error(mid, ex.code, str(ex))
        except Exception as ex:                      # never kill the server
            import traceback
            traceback.print_exc(file=self._log)
            return None if is_note else _error(mid, INTERNAL, "%s: %s" % (type(ex).__name__, ex))
        if is_note:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    # -- methods -----------------------------------------------------------------
    def _m_initialize(self, p):
        from .. import branding
        from .core_tools import INSTRUCTIONS
        want = p.get("protocolVersion")
        self.version = want if want in SUPPORTED else SUPPORTED[0]
        client = (p.get("clientInfo") or {}).get("name") or "client"
        self.ctx.client = "mcp:%s" % client
        if self.bridge is not None:
            self.bridge.client = client
        return {"protocolVersion": self.version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ovt-suite", "title": branding.APP_NAME,
                               "version": branding.__version__},
                "instructions": INSTRUCTIONS}

    def _m_notifications_initialized(self, p):
        self.initialized = True

    def _m_notifications_cancelled(self, p):
        pass                                         # calls run to completion

    def _m_ping(self, p):
        return {}

    def _m_logging_setLevel(self, p):
        return {}

    def _m_tools_list(self, p):
        tools = []
        for t in self.registry.all():
            d = {"name": t.name, "description": t.description,
                 "inputSchema": t.schema()}
            if self.version >= "2025-03-26":
                d["annotations"] = {"title": t.title or t.name,
                                    "readOnlyHint": t.read_only,
                                    "destructiveHint": False,
                                    "idempotentHint": t.read_only,
                                    "openWorldHint": False}
            if t.title and self.version >= _STRUCTURED_FROM:
                d["title"] = t.title
            tools.append(d)
        return {"tools": tools}

    def _m_tools_call(self, p):
        name = p.get("name")
        if not isinstance(name, str):
            raise _RpcError(INVALID_PARAMS, "tools/call needs a tool name")
        if self.registry.get(name) is None:
            raise _RpcError(INVALID_PARAMS, "unknown tool: %s" % name)
        args = p.get("arguments") or {}
        if not isinstance(args, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        ok, result = self._call(name, args)
        out = {"content": [{"type": "text",
                            "text": to_text(result) if ok else result.get("error", "error")}],
               "isError": not ok}
        if ok and isinstance(result, dict) and self.version >= _STRUCTURED_FROM:
            out["structuredContent"] = json.loads(to_text(result))
        return out

    def _call(self, name, args):
        """In the running app when it shares its tabs, else here on files."""
        if self.bridge is not None:
            from .bridge import read_live
            if self.bridge.connected or read_live():
                try:
                    r = self.bridge.call(name, args)
                    self._mode(True)
                    return r
                except (ConnectionError, ValueError) as ex:
                    self.bridge.close()
                    self._log.write("ovt-suite: app not reachable (%s); "
                                    "working on files\n" % ex)
        self._mode(False)
        return self.registry.call(name, self.ctx, args)

    def _mode(self, live: bool) -> None:
        if live != self._was_live:
            self._was_live = live
            self._log.write("ovt-suite: %s\n" % (
                "connected to the running app" if live else "working on files from disk"))
            self._log.flush()

    # clients probe these even when the capability is absent
    def _m_resources_list(self, p):
        return {"resources": []}

    def _m_resources_templates_list(self, p):
        return {"resourceTemplates": []}

    def _m_prompts_list(self, p):
        return {"prompts": []}


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(mid, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def launch_command():
    """``(command, args)`` an MCP client runs to start this server: the
    console launcher beside a frozen app, else this Python with ``-m``."""
    import os
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        from .. import branding
        launcher = os.path.join(os.path.dirname(exe), branding.MCP_EXE +
                                (".exe" if os.name == "nt" else ""))
        if os.path.isfile(launcher):
            return launcher, []
        return exe, ["mcp"]
    from .. import branding
    return exe, ["-m", branding.PKG, "mcp"]


def client_config() -> dict:
    """The ``mcpServers`` entry most MCP clients' settings files take."""
    cmd, args = launch_command()
    return {"mcpServers": {"ovt-suite": {"command": cmd, "args": args}}}


def serve(stdin=None, stdout=None, server: Optional[Server] = None) -> int:
    """Run until stdin closes."""
    if stdin is None:
        if sys.stdin is None:
            sys.stderr.write("ovtsuite mcp: no stdin (start it from an MCP client)\n")
            return 2
        stdin = _text(sys.stdin)
    if stdout is None:
        stdout = _text(sys.stdout)
        # nothing but protocol on stdout: stray prints go to the log
        sys.stdout = sys.stderr
    server = server or Server()
    for line in stdin:
        reply = server.handle_line(line)
        if reply is not None:
            stdout.write(reply + "\n")
            stdout.flush()
    return 0


def _text(stream):
    """The stream as UTF-8 text with \\n line ends on every platform."""
    import io
    buf = getattr(stream, "buffer", None)
    if buf is None:
        return stream
    if stream is sys.stdin:
        return io.TextIOWrapper(buf, encoding="utf-8", newline="\n")
    return io.TextIOWrapper(buf, encoding="utf-8", newline="\n", write_through=True)
