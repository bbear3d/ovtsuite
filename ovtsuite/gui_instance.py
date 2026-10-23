# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One window per user: a second start hands its file to the first.

Double-clicking a project while OVT-Suite is open used to start a second
copy of the app, and both could save the same project. Now the first window
listens on a local socket named after the user; a later start connects to
it, sends the file it was given (or nothing, to bring the window forward)
and exits. ``gui --new-instance`` (File > New window) starts a window of its
own anyway -- the project locks (gui_files.lock_project) keep two windows
off one project.

A tool can have a channel of its own (``kind``): OVT Converter uses one, so
files right-clicked together ("Convert with OVT Converter") all land in one
converter window.
"""

from __future__ import annotations

import getpass
import hashlib
import os
from typing import Callable, Optional, Sequence, Union

from PySide6 import QtCore

from . import branding

try:                    # a build without QtNetwork just runs one window each
    from PySide6 import QtNetwork
except Exception:       # pragma: no cover - depends on the bundle
    QtNetwork = None


def server_name(kind: str = "") -> str:
    """The socket of the user's window (``kind``: a tool's own, e.g.
    ``"converter"``; empty: the main app's)."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    seed = "%s|%s" % (branding.APP_NAME, user) + ("|" + kind if kind else "")
    tag = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return "ovtsuite-%s%s" % (kind + "-" if kind else "", tag)


def hand_over(path: Union[None, str, Sequence[str]], timeout_ms: int = 400,
              ack_ms: int = 2000, name: Optional[str] = None) -> bool:
    """Give ``path`` (or several paths) to a window already running. True
    when one took it. ``name``: the channel (default: the main app's)."""
    if QtNetwork is None:
        return False
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(name or server_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    paths = [path] if isinstance(path, str) or path is None else list(path)
    msg = "\n".join(os.path.abspath(p) for p in paths if p) + "\n"
    sock.write(msg.encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    # the window answers "ok" from its event loop: a window that hung (or a
    # socket left by one that died) never does, and this start goes on to
    # open a window of its own instead of exiting silently
    reply = b""
    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while not reply.endswith(b"\n") and deadline.elapsed() < ack_ms:
        if sock.waitForReadyRead(max(1, ack_ms - deadline.elapsed())):
            reply += bytes(sock.readAll())
        elif sock.state() != QtNetwork.QLocalSocket.LocalSocketState.ConnectedState:
            break
    sock.abort()
    return reply.strip() == b"ok"


def listen(win, open_path: Callable[[str], object], name: Optional[str] = None):
    """Take files from later starts: each opens in ``win`` (which comes to
    the front). Returns the server, or None when it could not listen."""
    if QtNetwork is None:
        return None
    name = name or server_name()
    server = QtNetwork.QLocalServer(win)
    if not server.listen(name):
        # a socket left by a window that crashed (on Unix it stays behind)
        QtNetwork.QLocalServer.removeServer(name)
        if not server.listen(name):
            return None

    def _take():
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            buf = bytearray()

            def _read(s=sock, b=buf):
                b.extend(bytes(s.readAll()))
                if b.endswith(b"\n"):
                    _done(s, b)

            def _done(s, b):
                for line in bytes(b).decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    win.showNormal() if win.isMinimized() else None
                    win.raise_()
                    win.activateWindow()
                    if line:
                        QtCore.QTimer.singleShot(
                            0, lambda p=line: open_path(p))
                s.write(b"ok\n")
                s.flush()
                s.disconnectFromServer()

            sock.readyRead.connect(_read)
            if sock.bytesAvailable():
                _read()
    server.newConnection.connect(_take)
    win.__dict__["_ovt_instance_server"] = server
    return server


__all__ = ["server_name", "hand_over", "listen"]
