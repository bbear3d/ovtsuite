# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The AI service adapters against local stand-in servers speaking each wire
format (Anthropic Messages, OpenAI Chat Completions, Gemini generateContent):
the tool loop, errors, offline fallback, redaction, the sent log, keys and
usage. No network, no binaries -- fast."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ovtsuite import branding
from ovtsuite.ai import assistant as A
from ovtsuite.ai import config as C
from ovtsuite.ai import keystore as K
from ovtsuite.ai import providers as PV
from ovtsuite.ai.tools import Registry, Tool
from ovtsuite.ai.workspace import FileWorkspace

VIN = "WAUZZZ4F28N012345"


class Fake:
    """One server; ``kind`` picks the wire format it speaks."""

    def __init__(self, kind, status=200):
        self.kind, self.status, self.requests = kind, status, []
        self.error_body = {"error": {"message": "nope"}}
        self.busy = 0                   # answer this many POSTs "busy" first
        self.thinks = False             # DeepSeek: reasoning_content, to be sent back
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                fake.requests.append(("GET", self.path, dict(self.headers), None))
                if fake.status != 200:
                    return self._send(fake.status, fake.error_body)
                if fake.kind == "gemini":
                    return self._send(200, {"models": [
                        {"name": "models/gem-1", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]})
                self._send(200, {"data": [{"id": "m-1"}, {"id": "m-2:free"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                fake.requests.append(("POST", self.path, dict(self.headers), body))
                if fake.busy:
                    fake.busy -= 1
                    return self._send(503, {"error": {"message": "high demand"}})
                bad = fake.refuse(body)
                if bad:
                    return self._send(400, {"type": "error", "error": {
                        "type": "invalid_request_error", "message": bad}})
                if fake.status != 200:
                    return self._send(fake.status, fake.error_body)
                self._send(200, getattr(fake, "_" + fake.kind)(body))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()

    def refuse(self, body):
        """What the real API refuses: Anthropic wants every thinking block
        sent back whole (text and signature)."""
        if self.kind == "openai" and self.thinks:
            for i, m in enumerate(body.get("messages", [])):
                if m.get("role") == "assistant" and m.get("tool_calls") \
                        and not m.get("reasoning_content"):
                    return ("Missing `reasoning_content` field in the assistant message at "
                            "message index %d" % i)
        if self.kind != "anthropic":
            return ""
        for i, m in enumerate(body.get("messages", [])):
            for j, b in enumerate(m.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "thinking" and (
                        not b.get("thinking") or not b.get("signature")):
                    return ("messages.%d.content.%d.thinking: each thinking block must "
                            "contain thinking" % (i, j))
        return ""

    # -- canned answers: first ask for a tool, then answer --------------------------
    def _anthropic(self, body):
        done = any(b.get("type") == "tool_result" for m in body["messages"]
                   for b in m["content"])
        if not done:
            return {"content": [{"type": "thinking", "thinking": "Need the handle first.",
                                 "signature": "sig-abc"},
                                {"type": "text", "text": "Let me look."},
                                {"type": "tool_use", "id": "tu1", "name": "where",
                                 "input": {"handle": "t1"}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 10,
                              "cache_read_input_tokens": 50}}
        return {"content": [{"type": "text", "text": "All good."}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 120, "output_tokens": 5}}

    def _openai(self, body):
        done = any(m["role"] == "tool" for m in body["messages"])
        if not done:
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "where",
                                                "arguments": "{\"handle\": \"t1\"}"}}]}
            if self.thinks:
                msg["reasoning_content"] = "Check where first."
            return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 8,
                              "prompt_tokens_details": {"cached_tokens": 40}}}
        return {"choices": [{"message": {"role": "assistant", "content": "All good."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 4}}

    def _gemini(self, body):
        done = any("functionResponse" in p for c in body["contents"] for p in c["parts"])
        if not done:
            return {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "where", "args": {"handle": "t1"}},
                 "thoughtSignature": "sig123"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 7}}
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": "All good."}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 75, "candidatesTokenCount": 3}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(C.ENV_DIR, str(tmp_path))
    monkeypatch.setenv("OVT_AI_KEYSTORE", "file")
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    A.SENT.clear()
    seen = []
    reg = Registry()

    def where(ctx, handle=""):
        seen.append(handle)
        return {"handle": handle, "vin": VIN, "note": "car %s" % VIN}
    reg.add(Tool("where", "Where things are", where,
                 {"type": "object", "properties": {"handle": {"type": "string"}}}))
    return reg, seen


def _assistant(reg, service, url, *, key="sk-test-123", cfg=None):
    if key:
        K.set_key(service, key)
    base = url + {"anthropic": "", "gemini": "/v1beta"}.get(PV.SPECS[service].kind, "/v1")
    C.update(service=service, base_urls={service: base})
    return A.Assistant(FileWorkspace(), registry=reg, cfg=cfg or C.load())


@pytest.mark.parametrize("service,kind", [("anthropic", "anthropic"),
                                          ("openai", "openai"),
                                          ("gemini", "gemini"),
                                          ("groq", "openai")])
def test_tool_loop_each_wire_format(env, service, kind):
    reg, seen = env
    fake = Fake(kind)
    try:
        a = _assistant(reg, service, fake.url)
        r = a.ask("Is car %s ok?" % VIN)
    finally:
        fake.close()
    assert r.text == "All good." and r.tool_calls == 1 and seen == ["t1"]
    assert r.model in ("m-1", "gem-1")              # picked from the service's list
    assert r.usage.input > 0 and r.usage.output > 0
    posts = [q for q in fake.requests if q[0] == "POST"]
    first, second = posts[0][3], posts[1][3]
    blob = json.dumps(second)
    assert VIN not in json.dumps(first) and VIN not in blob     # redacted both ways
    assert "[VIN]" in json.dumps(first) and "[redacted]" in blob
    hdrs = {k.lower(): v for k, v in posts[0][2].items()}
    if kind == "anthropic":
        assert hdrs["x-api-key"] == "sk-test-123" and hdrs["anthropic-version"]
        assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert second["messages"][-1]["content"][0]["type"] == "tool_result"
    elif kind == "openai":
        assert hdrs["authorization"] == "Bearer sk-test-123"
        assert second["messages"][-1]["role"] == "tool"
        assert second["messages"][-1]["tool_call_id"] == "c1"
    else:
        assert hdrs["x-goog-api-key"] == "sk-test-123"
        # the model turn goes back exactly as received (thought signature)
        assert second["contents"][1]["parts"][0]["thoughtSignature"] == "sig123"
        fr = second["contents"][2]["parts"][0]["functionResponse"]
        assert fr["name"] == "where" and fr["response"]["vin"] == "[redacted]"
    # the sent log has the requests, never the key
    log = json.dumps(A.sent_log())
    assert "sk-test-123" not in log and len(A.sent_log()) >= 3
    u = C.load()["usage"][service]
    assert sum(v["requests"] for v in u.values()) == 2


def test_errors_are_classified(env):
    reg, _ = env
    for status, kind in ((401, PV.AUTH), (429, PV.RATE), (500, PV.SERVER), (400, PV.BAD)):
        fake = Fake("openai", status=status)
        try:
            with pytest.raises(PV.ProviderError) as ei:
                _assistant(reg, "openai", fake.url).ask("hi")
            assert ei.value.kind == kind
        finally:
            fake.close()
    a = _assistant(reg, "openai", "http://127.0.0.1:9")    # nothing listens
    a.cfg["fallback_local"] = False
    with pytest.raises(PV.ProviderError) as ei:
        a.ask("hi")
    assert ei.value.kind == PV.OFFLINE


def test_the_app_names_itself_and_a_firewall_block_is_not_a_bad_key(env):
    """Groq's Cloudflare answers Python's default User-Agent with 403 / error
    1010 ("browser_signature_banned"): the app sends its own name, and such a
    403 is reported as a firewall block, not as a refused API key."""
    reg, _ = env
    fake = Fake("openai")
    try:
        _assistant(reg, "openai", fake.url).ask("hi")
        ua = fake.requests[0][2].get("User-Agent", "")
        assert ua.startswith(branding.APP_NAME.replace(" ", "") + "/")
        assert "urllib" not in ua.lower()
        fake.status = 403
        fake.error_body = {"title": "Error 1010: Access denied", "status": 403,
                           "error_code": 1010, "error_name": "browser_signature_banned",
                           "error_category": "access_denied", "cloudflare_error": True}
        with pytest.raises(PV.ProviderError) as ei:
            _assistant(reg, "openai", fake.url).ask("hi")
        assert ei.value.kind == PV.BLOCKED
        assert "not about the API key" in str(ei.value)
        assert "browser_signature_banned" in str(ei.value)
        fake.error_body = {"error": {"message": "Invalid API Key"}}  # a real refusal
        with pytest.raises(PV.ProviderError) as ei:
            _assistant(reg, "openai", fake.url).ask("hi")
        assert ei.value.kind == PV.AUTH
    finally:
        fake.close()


def test_a_busy_service_is_asked_again(env, monkeypatch):
    """A 503 / 529 "overloaded" is retried after a pause (with a note in the
    panel); a service that stays busy still ends in the error."""
    reg, _ = env
    monkeypatch.setattr(A.Assistant, "RETRY_WAITS", (0.01, 0.01))
    fake = Fake("openai")
    try:
        fake.busy = 2
        notes = []
        a = _assistant(reg, "openai", fake.url)
        a.on_event = lambda ev: notes.append(ev.text) if ev.kind == "notice" else None
        assert a.ask("hi").text == "All good."
        assert sum("busy (503)" in n for n in notes) == 2
        fake.busy = 5
        with pytest.raises(PV.ProviderError) as ei:
            _assistant(reg, "openai", fake.url).ask("hi")
        assert ei.value.kind == PV.SERVER and ei.value.status == 503
    finally:
        fake.close()


def test_changed_settings_carry_the_chat_to_the_new_service(env):
    """Tools > AI > Settings while a chat is open: the next question goes to
    the service chosen now, with the conversation so far."""
    reg, _ = env
    old, new = Fake("gemini"), Fake("anthropic")
    try:
        a = _assistant(reg, "gemini", old.url)
        a.ask("first")
        n_old = len(old.requests)
        K.set_key("anthropic", "sk-ant-1")
        C.update(service="anthropic", base_urls={"anthropic": new.url})
        a.use_settings()
        assert a.ask("second").service == "anthropic"
        assert len(old.requests) == n_old
        sent = next(r[3] for r in new.requests if r[0] == "POST")["messages"]
        assert "first" in json.dumps(sent[0])            # the earlier question...
        assert "All good." in json.dumps(sent)           # ...and its answer
        assert "second" in json.dumps(sent[-1])
    finally:
        old.close()
        new.close()


def test_an_incomplete_thinking_block_is_left_out():
    from ovtsuite.ai.providers.anthropic import AnthropicProvider
    msgs = [{"role": "user", "text": "hi"},
            {"role": "assistant", "raw_kind": "anthropic", "text": "x", "calls": [],
             "raw": [{"type": "thinking", "thinking": "", "signature": ""},
                     {"type": "text", "text": "x"}]}]
    wire = AnthropicProvider.wire_messages(msgs)
    assert wire[1]["content"] == [{"type": "text", "text": "x"}]


@pytest.mark.parametrize("streamed", [False, True])
def test_deepseek_gets_its_reasoning_back(env, streamed):
    """DeepSeek's thinking mode: with tools, every earlier answer's
    reasoning_content must be sent back (else 400) -- but only to the server
    that wrote it."""
    reg, seen = env
    fake = StreamFake("openai") if streamed else Fake("openai")
    fake.thinks = True
    try:
        a = _assistant(reg, "deepseek", fake.url)
        if streamed:
            a.on_event = lambda ev: None
        assert a.ask("hi").text == "All good." and seen == ["t1"]
        posts = [q for q in fake.requests if q[0] == "POST"]
        back = posts[1][3]["messages"][2]
        assert back["role"] == "assistant" and back["reasoning_content"] == "Check where first."
        assert "_server" not in json.dumps(posts[1][3])
    finally:
        fake.close()
    from ovtsuite.ai.providers.openai_compat import OpenAIProvider
    other = OpenAIProvider.wire_messages("s", a.messages, "https://elsewhere/v1")
    assert all("reasoning_content" not in m for m in other)


def test_offline_falls_back_to_a_local_model(env, monkeypatch):
    reg, seen = env
    local = Fake("openai")
    try:
        cfg = C.update(base_urls={"ollama": local.url + "/v1"})
        monkeypatch.setattr(PV.SPECS["ollama"], "base_url", local.url + "/v1")
        a = _assistant(reg, "anthropic", "http://127.0.0.1:9")
        r = a.ask("hi")
    finally:
        local.close()
    assert r.service == "ollama" and r.text == "All good."
    assert r.notices and "Offline" in r.notices[0]
    assert r.redacted == 0                       # local: nothing leaves the PC


def test_no_service_and_no_local_server(env, monkeypatch):
    reg, _ = env
    monkeypatch.setattr(A, "detect_local", lambda: [])
    a = A.Assistant(FileWorkspace(), registry=reg, cfg=C.load())
    with pytest.raises(A.NotConfigured):
        a.ask("hi")
    K.delete_key("anthropic")
    C.update(service="anthropic")
    a = A.Assistant(FileWorkspace(), registry=reg, cfg=C.load())
    with pytest.raises(A.NotConfigured):
        a.ask("hi")


def test_local_server_is_the_default(env, monkeypatch):
    reg, _ = env
    local = Fake("openai")
    try:
        monkeypatch.setattr(PV.SPECS["lmstudio"], "base_url", local.url + "/v1")
        monkeypatch.setattr(PV.SPECS["ollama"], "base_url", "http://127.0.0.1:9/v1")
        a = A.Assistant(FileWorkspace(), registry=reg, cfg=C.load())   # nothing chosen
        r = a.ask("hi")
    finally:
        local.close()
    assert r.service == "lmstudio" and r.model == "m-1"


def test_keystore_file_backend_and_environment(env, monkeypatch):
    assert K.backend() == "file"
    assert K.get_key("openai") is None and K.key_source("openai") == ""
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert K.get_key("openai") == "from-env" and K.key_source("openai") == "environment"
    K.set_key("openai", "stored-1")
    assert K.get_key("openai") == "stored-1" and K.key_source("openai") == "stored"
    K.set_key("openai", "")                      # clearing
    assert K.get_key("openai") == "from-env"
    import os
    if os.name == "posix":
        assert oct(os.stat(K._file_path()).st_mode & 0o777) == "0o600"


def test_model_lists():
    fake = Fake("gemini")
    try:
        p = PV.make("gemini", key="k", base_url=fake.url + "/v1beta")
        assert p.list_models() == ["gem-1"]           # generateContent only
    finally:
        fake.close()
    from ovtsuite.ai.providers.gemini import gemini_schema
    s = gemini_schema({"type": "object", "additionalProperties": False,
                       "properties": {"a": {"type": "array"}}})
    assert s == {"type": "object", "properties": {"a": {"type": "array",
                                                        "items": {"type": "string"}}}}


# -- streamed replies -------------------------------------------------------

class StreamFake(Fake):
    """The same canned answers, sent as a server-sent-event stream the way
    each service streams, split into small pieces."""

    def __init__(self, kind):
        super().__init__(kind)
        fake = self
        orig = self.srv.RequestHandlerClass

        class H(orig):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                fake.requests.append(("POST", self.path, dict(self.headers), body))
                bad = fake.refuse(body)
                if bad:
                    return self._send(400, {"type": "error", "error": {
                        "type": "invalid_request_error", "message": bad}})
                if not (body.get("stream") or "streamGenerate" in self.path):
                    return self._send(200, getattr(fake, "_" + fake.kind)(body))
                events = getattr(fake, "_sse_" + fake.kind)(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev, data in events:
                    if ev:
                        self.wfile.write(("event: %s\n" % ev).encode())
                    self.wfile.write(("data: %s\n\n" % data).encode())
                    self.wfile.flush()
        self.srv.RequestHandlerClass = H

    def _sse_anthropic(self, body):
        full = self._anthropic(body)
        yield "message_start", json.dumps({"type": "message_start",
                                           "message": {"usage": {"input_tokens": 11}}})
        for i, b in enumerate(full["content"]):
            if b["type"] == "thinking":
                yield "content_block_start", json.dumps({
                    "type": "content_block_start", "index": i,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                for k in range(0, len(b["thinking"]), 5):
                    yield "content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": i,
                        "delta": {"type": "thinking_delta", "thinking": b["thinking"][k:k + 5]}})
                yield "content_block_delta", json.dumps({
                    "type": "content_block_delta", "index": i,
                    "delta": {"type": "signature_delta", "signature": b["signature"]}})
            elif b["type"] == "text":
                yield "content_block_start", json.dumps({"type": "content_block_start", "index": i,
                                                         "content_block": {"type": "text", "text": ""}})
                for k in range(0, len(b["text"]), 3):
                    yield "content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": i,
                        "delta": {"type": "text_delta", "text": b["text"][k:k + 3]}})
            else:
                yield "content_block_start", json.dumps({"type": "content_block_start", "index": i,
                                                         "content_block": {"type": "tool_use", "id": b["id"],
                                                                           "name": b["name"], "input": {}}})
                js = json.dumps(b["input"])
                for k in range(0, len(js), 4):
                    yield "content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": i,
                        "delta": {"type": "input_json_delta", "partial_json": js[k:k + 4]}})
            yield "content_block_stop", json.dumps({"type": "content_block_stop", "index": i})
        yield "message_delta", json.dumps({"type": "message_delta",
                                           "delta": {"stop_reason": full["stop_reason"]},
                                           "usage": {"output_tokens": 7}})
        yield "message_stop", json.dumps({"type": "message_stop"})

    def _sse_openai(self, body):
        full = self._openai(body)["choices"][0]["message"]
        r = full.get("reasoning_content") or ""
        for k in range(0, len(r), 4):
            yield "", json.dumps({"choices": [{"delta": {"reasoning_content": r[k:k + 4]}}]})
        if full.get("tool_calls"):
            tc = full["tool_calls"][0]
            args = tc["function"]["arguments"]
            yield "", json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": tc["id"], "function": {"name": tc["function"]["name"],
                                                          "arguments": ""}}]}}]})
            for k in range(0, len(args), 3):
                yield "", json.dumps({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": args[k:k + 3]}}]}}]})
        else:
            for k in range(0, len(full["content"]), 2):
                yield "", json.dumps({"choices": [{"delta": {"content": full["content"][k:k + 2]}}]})
        yield "", json.dumps({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        yield "", "[DONE]"

    def _sse_gemini(self, body):
        full = self._gemini(body)
        parts = full["candidates"][0]["content"]["parts"]
        for p in parts:
            if "text" in p:
                for k in range(0, len(p["text"]), 3):
                    yield "", json.dumps({"candidates": [{"content": {"role": "model", "parts": [
                        {"text": p["text"][k:k + 3]}]}}]})
            else:
                yield "", json.dumps({"candidates": [{"content": {"role": "model", "parts": [p]}}]})
        yield "", json.dumps({"candidates": [{"content": {"role": "model", "parts": []},
                                              "finishReason": "STOP"}],
                              "usageMetadata": full["usageMetadata"]})


@pytest.mark.parametrize("service,kind", [("anthropic", "anthropic"), ("openai", "openai"),
                                          ("gemini", "gemini"), ("lmstudio", "openai")])
def test_streamed_replies(env, service, kind):
    reg, seen = env
    fake = StreamFake(kind)
    pieces = []
    try:
        a = _assistant(reg, service, fake.url, key="" if service == "lmstudio" else "k")
        a.on_event = lambda ev: pieces.append(ev.text) if ev.kind == "delta" else None
        r = a.ask("hi")
    finally:
        fake.close()
    assert r.text == "All good." and seen == ["t1"]          # the tool call survived
    assert "".join(pieces).endswith("All good.") and len(pieces) > 2
    posts = [q for q in fake.requests if q[0] == "POST"]
    if kind == "gemini":
        assert "streamGenerateContent" in posts[0][1]
        # the streamed model turn goes back whole, thought signature included
        assert posts[1][3]["contents"][1]["parts"][0]["thoughtSignature"] == "sig123"
    else:
        assert posts[0][3]["stream"] is True
    if kind == "anthropic":
        # the thinking block went back whole: text and signature
        back = posts[1][3]["messages"][1]["content"][0]
        assert back == {"type": "thinking", "thinking": "Need the handle first.",
                        "signature": "sig-abc"}
        assert all(q[3].get("stream") for q in posts)          # still streaming
    assert r.usage.output > 0


def test_a_server_that_ignores_stream_still_works(env):
    reg, seen = env
    fake = Fake("openai")                                     # answers JSON, never SSE
    try:
        a = _assistant(reg, "openai", fake.url)
        pieces = []
        a.on_event = lambda ev: pieces.append(ev.text) if ev.kind == "delta" else None
        r = a.ask("hi")
    finally:
        fake.close()
    assert r.text == "All good." and pieces == []
