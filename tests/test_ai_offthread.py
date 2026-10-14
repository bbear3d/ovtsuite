# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The heavy AI tools off the GUI thread: a snapshot of the tab is taken
through ``Context.on_gui`` and the work is done on copies of the bytes;
parsed logs and file hashes are kept; Stop cuts a streamed reply; the
"what was sent" log is capped. Small stand-in sessions, no Qt."""

import http.server
import os
import threading
import time

import pytest

from ovtsuite.ai import logs as L
from ovtsuite.ai.live import LiveWorkspace
from ovtsuite.ai.tools import Context, default_registry
from ovtsuite.binary import BinaryFile
from ovtsuite.model import Entity


def _ent(name, addr, bf):
    return Entity(name=name, kind="curve", tier=1, categories=["Fuel"],
                  description="the %s curve" % name, units="mg", addr=addr, rows=1, cols=4,
                  bits=8, signed=False, equation="X", source="anchored", binfile=bf)


class Sess:
    NAMES = ("AlphaCurve", "BravoCurve", "CharlieCurve")

    def __init__(self, data):
        self.binfile = BinaryFile(bytes(data), "x.bin")
        self.entities = [_ent(n, i * 4, self.binfile) for i, n in enumerate(self.NAMES)]
        self.platform = "edc16"
        self.modified = False

    def variant_entities(self):
        return []

    def counterpart(self, other, e):
        return next((x for x in other.entities if x.name == e.name), None)

    def checksum_provider(self):
        return None


class Tab:
    def __init__(self, primary, comparison=None):
        self.primary, self.comparison = primary, comparison
        self.project_ref = None
        self.cmp_version = None

    def tab_label(self):
        return "x.bin"

    def is_dirty(self):
        return False

    def _current_entity(self):
        return None

    def tables(self):
        return []


class Win:
    def __init__(self, tabs):
        self.tabs = tabs

    def workspaces(self):
        return list(self.tabs)

    @property
    def current(self):
        return self.tabs[0]


@pytest.fixture
def env():
    base = bytes(range(12))
    edited = bytearray(base)
    edited[4] = 99                                  # BravoCurve differs
    prim, cmp = Sess(edited), Sess(base)
    ws = LiveWorkspace(Win([Tab(prim, cmp)]))
    hops = []

    def on_gui(fn):
        """The GUI thread: the snapshot is taken, then the user edits
        CharlieCurve straight after -- the tool must not see that edit."""
        hops.append(threading.current_thread().name)
        r = fn()
        prim.binfile.data[8] = 77
        return r
    ctx = Context(ws, client="pytest", on_gui=on_gui)
    return default_registry(include_modules=False), ctx, prim, hops


def _call(env, name, **args):
    reg, ctx, _p, _h = env
    ok, r = reg.call(name, ctx, args)
    assert ok, r
    return r


def test_diff_computes_on_the_snapshot(env):
    r = _call(env, "diff", handle="t1")
    assert [m["id"] for m in r["maps"]] == ["BravoCurve"]      # not CharlieCurve
    assert r["changed"] == 1 and env[3] == ["MainThread"]
    # the live session was edited after the snapshot: the next call sees it
    r = _call(env, "diff", handle="t1")
    assert sorted(m["id"] for m in r["maps"]) == ["BravoCurve", "CharlieCurve"]


def test_list_maps_changed_only_and_find_maps(env):
    r = _call(env, "list_maps", handle="t1", changed_only=True)
    assert [m["id"] for m in r["maps"]] == ["BravoCurve"]
    r = _call(env, "list_maps", handle="t1")
    assert r["total"] == 3
    r = _call(env, "find_maps", handle="t1", query="charlie")
    assert r["results"] and r["results"][0]["id"] == "CharlieCurve"
    assert len(env[3]) == 3                          # one hop each, nothing more


def test_snapshot_entities_read_copied_bytes():
    from ovtsuite.ai.snapshot import Frozen
    s = Sess(bytes(range(12)))
    e = s.entities[1]
    fz = Frozen()
    fz.add(s.entities)
    s.binfile.data[4] = 200
    assert fz.entity(e).values() == [[4, 5, 6, 7]]
    assert e.values() == [[200, 5, 6, 7]]
    assert fz.entity(e) is fz.entity(e)
    with pytest.raises(TypeError):                  # the copy cannot be written
        fz.entity(e).binfile.data[0] = 1


def test_parsed_logs_are_kept(tmp_path, monkeypatch):
    L.clear_cache()
    loads = []
    monkeypatch.setattr(L, "load_path", lambda p: loads.append(p) or object())
    f = tmp_path / "a.csv"
    f.write_text("t,x\n0,1\n")
    a = L.load_path_cached(str(f))
    assert L.load_path_cached(str(f)) is a and len(loads) == 1
    f.write_text("t,x\n0,1\n1,2\n")                  # a new version of the file
    os.utime(str(f), ns=(time.time_ns() + 10 ** 9,) * 2)
    assert L.load_path_cached(str(f)) is not a and len(loads) == 2
    # project logs: by attachment and content
    monkeypatch.setattr(L, "load_bytes", lambda d, n: loads.append(n) or object())
    b = L.load_bytes_cached(b"abc", "log.csv", "p/1")
    assert L.load_bytes_cached(b"abc", "log.csv", "p/1") is b
    assert L.load_bytes_cached(b"abd", "log.csv", "p/1") is not b
    for i in range(L.CACHE_SIZE + 1):               # the oldest are let go
        L.load_bytes_cached(b"x%d" % i, "log.csv", "p/%d" % i)
    assert L.load_bytes_cached(b"abc", "log.csv", "p/1") is not b
    L.clear_cache()


def test_summary_is_worked_out_once(monkeypatch):
    calls = []
    monkeypatch.setattr(L, "summary", lambda log, name="": calls.append(1) or {"rows": 3})

    class Log:
        pass
    lg = Log()
    a = L.summary_cached(lg)
    a["rows"] = 0                                   # a copy: the kept one is safe
    assert L.summary_cached(lg) == {"rows": 3} and len(calls) == 1


def test_file_hash_is_kept_per_version(tmp_path, monkeypatch):
    from ovtsuite.ai import workspace as W
    f = tmp_path / "b.bin"
    f.write_bytes(b"one")
    h1 = W.file_sha256(str(f))
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    assert W.file_sha256(str(f)) == h1 and not opened
    f.write_bytes(b"two!")
    assert W.file_sha256(str(f)) != h1 and opened


def test_sent_log_is_capped(monkeypatch):
    from ovtsuite.ai import assistant as A
    monkeypatch.setattr(A, "SENT", A.collections.deque(maxlen=60))
    monkeypatch.setattr(A, "SENT_MAX_BYTES", 1000)
    for i in range(5):
        A._log_sent("ollama", {"n": i, "size": 400})
    assert [r["n"] for r in A.sent_log()] == [3, 4]


class _Slow(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i in range(100):
                self.wfile.write(b"data: {\"n\": %d}\n\n" % i)
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def test_stop_cuts_a_streamed_reply():
    from ovtsuite.ai.providers import HttpClient, ProviderError
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        http_ = HttpClient(timeout=30)
        http_.cancel = threading.Event()
        kind, events = http_.stream("POST", "http://127.0.0.1:%d/" % srv.server_address[1],
                                    {}, {"x": 1})
        assert kind == "sse"
        got = []
        t0 = time.time()
        with pytest.raises(ProviderError) as ex:
            for _ev, data in events:
                got.append(data)
                if len(got) == 2:
                    http_.cancel.set()
                    http_.abort()
        assert ex.value.kind == "cancelled"
        assert len(got) == 2 and time.time() - t0 < 5
    finally:
        srv.shutdown()
        srv.server_close()
