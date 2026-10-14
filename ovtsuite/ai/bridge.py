# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The live bridge: the running app answers AI tool calls about its open tabs.

The app listens on a loopback TCP port (127.0.0.1 only) and writes where, plus
a random token, to ``<config dir>/ai/live.json`` -- readable by this user
only. ``ovtsuite mcp`` reads that file; when the app is running it forwards
every tool call there, so the AI sees the open tabs and its proposals reach
the app at once. When it is not, the MCP server works on files from disk on
its own. TCP rather than a named pipe / Unix socket: the same code on
Windows, Linux and macOS, and nothing beyond the standard library.

Wire format: one JSON object per line, both ways. The first request must be
``{"hello": <token>}``; anything else closes the connection. Then
``{"id": n, "op": "call", "tool": name, "args": {...}}`` ->
``{"id": n, "ok": bool, "result": {...}}``.

Nothing here knows about Qt. The app passes ``run_on_gui(fn)``, which runs a
callable on its GUI thread and returns the result; every call that touches an
open tab goes through it, while files the AI opens from disk are loaded on the
bridge's own thread so the window never stalls on them.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
from typing import Callable, Optional

from .tools import Context, Registry

ENV_DIR = "OVT_AI_DIR"
FILE = "live.json"
#: Tools that read the app's tab list or view: always on the GUI thread.
GUI_TOOLS = ("list_open", "current_view", "list_proposals", "show_map",
             "show_category", "filter_tree")


def live_path() -> str:
    override = os.environ.get(ENV_DIR)
    if override:
        root = override
    else:
        from ..recent import _config_dir
        root = os.path.join(_config_dir(), "ai")
    return os.path.join(root, FILE)


def _write_private(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def read_live() -> Optional[dict]:
    try:
        with open(live_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and d.get("port") and d.get("token") else None
    except (OSError, ValueError):
        return None


# ===================================================================== server
class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.server.bridge._conns.add(self.connection)   # type: ignore[attr-defined]

    def finish(self):
        self.server.bridge._conns.discard(self.connection)   # type: ignore[attr-defined]
        try:
            super().finish()
        except OSError:
            pass

    def handle(self):
        srv: "BridgeServer" = self.server.bridge          # type: ignore[attr-defined]
        try:
            first = self.rfile.readline(65536)
            hello = json.loads(first.decode("utf-8")) if first else {}
        except ValueError:
            return
        tok = hello.get("hello") if isinstance(hello, dict) else None
        if not isinstance(tok, str) or not hmac.compare_digest(tok, srv.token):
            self._send({"ok": False, "error": "bad token"})
            return
        client = str(hello.get("client") or "mcp")[:80]
        self._send({"ok": True, "app": srv.app, "version": srv.version,
                    "pid": os.getpid()})
        while True:
            line = self.rfile.readline(1 << 22)
            if not line:
                return
            try:
                req = json.loads(line.decode("utf-8"))
            except ValueError:
                self._send({"id": None, "ok": False, "result": {"error": "bad request"}})
                continue
            rid = req.get("id")
            if req.get("op") != "call":
                self._send({"id": rid, "ok": False,
                            "result": {"error": "unknown op %r" % req.get("op")}})
                continue
            ok, result = srv.call(req.get("tool"), req.get("args") or {}, client)
            self._send({"id": rid, "ok": ok, "result": result})

    def _send(self, obj):
        self.wfile.write((json.dumps(obj, default=str, separators=(",", ":"))
                          + "\n").encode("utf-8"))
        self.wfile.flush()


#: how often the server loop looks whether it is being stopped (seconds)
POLL_S = 0.05


class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False


class BridgeServer:
    """Runs in the app. ``workspace`` is the live workspace (open tabs plus
    files the AI opened); ``run_on_gui(fn)`` runs ``fn`` on the GUI thread."""

    def __init__(self, workspace, registry: Optional[Registry] = None,
                 run_on_gui: Optional[Callable] = None,
                 on_activity: Optional[Callable[[str, str], None]] = None):
        from .. import branding
        from .tools import default_registry
        self.workspace = workspace
        self.registry = registry or default_registry()
        self.run_on_gui = run_on_gui or (lambda fn: fn())
        self.on_activity = on_activity
        self.token = secrets.token_hex(24)
        self.app = branding.APP_NAME
        self.version = branding.__version__
        self._srv = None
        self._thread = None
        self.port = None
        self._lock = threading.Lock()
        self._conns = set()

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> int:
        self._srv = _TCPServer(("127.0.0.1", 0), _Handler)
        self._srv.bridge = self                           # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        # a short poll: stop() (on exit) waits for the loop to notice
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={"poll_interval": POLL_S},
                                        name="ovt-ai-bridge", daemon=True)
        self._thread.start()
        _write_private(live_path(), {"port": self.port, "token": self.token,
                                     "pid": os.getpid(), "app": self.app,
                                     "version": self.version,
                                     "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return self.port

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        for c in list(self._conns):         # connected clients too
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self._conns.clear()
        # remove the discovery file only if it is still ours
        cur = read_live()
        if cur and cur.get("token") == self.token:
            try:
                os.remove(live_path())
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._srv is not None

    # -- calls ------------------------------------------------------------------
    def call(self, tool, args, client: str = "mcp"):
        if not isinstance(tool, str) or not isinstance(args, dict):
            return False, {"error": "bad call"}
        ctx = Context(workspace=self.workspace, client="mcp:%s" % client)
        if self.on_activity is not None:
            try:
                self.on_activity(client, tool)
            except Exception:
                pass
        if self._on_gui(tool, args):
            box = {}

            def run():
                box["r"] = self.registry.call(tool, ctx, args)
            try:
                self.run_on_gui(run)
            except Exception as ex:
                return False, {"error": "the app could not run %s: %s" % (tool, ex)}
            return box.get("r", (False, {"error": "the app did not answer"}))
        with self._lock:                 # one disk load at a time
            return self.registry.call(tool, ctx, args)

    def _on_gui(self, tool, args) -> bool:
        if tool in GUI_TOOLS:
            return True
        if tool == "open_file":
            return False
        h = args.get("handle")
        is_tab = getattr(self.workspace, "is_tab", None)
        return bool(h and is_tab is not None and is_tab(h))


# ===================================================================== client
class BridgeClient:
    """Used by ``ovtsuite mcp``: forwards tool calls to the running app."""

    def __init__(self, client: str = "mcp", timeout: float = 600.0):
        self.client = client
        self.timeout = timeout
        self._sock = None
        self._rf = None
        self._n = 0
        self.info = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        live = read_live()
        if not live:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", int(live["port"])), timeout=3)
        except OSError:
            return False
        s.settimeout(self.timeout)
        rf = s.makefile("rb")
        try:
            s.sendall((json.dumps({"hello": live["token"], "client": self.client})
                       + "\n").encode("utf-8"))
            reply = json.loads(rf.readline(65536).decode("utf-8") or "{}")
        except (OSError, ValueError):
            s.close()
            return False
        if not reply.get("ok"):
            s.close()
            return False
        self._sock, self._rf, self.info = s, rf, reply
        return True

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = self._rf = None

    def call(self, tool: str, args: dict):
        """``(ok, result)``; raises ConnectionError when the app is gone."""
        with self._lock:
            if self._sock is None and not self.connect():
                raise ConnectionError("the app is not running")
            self._n += 1
            msg = {"id": self._n, "op": "call", "tool": tool, "args": args}
            try:
                self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
                line = self._rf.readline(1 << 26)
            except OSError as ex:
                self.close()
                raise ConnectionError(str(ex))
            if not line:
                self.close()
                raise ConnectionError("the app closed the connection")
            rep = json.loads(line.decode("utf-8"))
            return bool(rep.get("ok")), rep.get("result") or {}
