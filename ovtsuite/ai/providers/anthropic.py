# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Claude, through Anthropic's Messages API."""

from __future__ import annotations

from typing import List

from .base import Call, Provider, ProviderError, SERVER, Turn, Usage, tool_result_text

VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    kind = "anthropic"

    def _headers(self):
        return {"x-api-key": self.key, "anthropic-version": VERSION}

    def list_models(self) -> List[str]:
        out = self.http.request("GET", self.base_url + "/v1/models?limit=100",
                                self._headers())
        return [m.get("id") for m in out.get("data", []) if m.get("id")]

    @staticmethod
    def wire_messages(messages):
        out = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)      # roles must alternate
            else:
                out.append({"role": role, "content": list(blocks)})
        for m in messages:
            if m["role"] == "user":
                push("user", [{"type": "text", "text": m["text"]}])
            elif m["role"] == "assistant":
                if m.get("raw") is not None and m.get("raw_kind") == "anthropic":
                    push("assistant", _replayable(m["raw"]))
                    continue
                blocks = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for c in m.get("calls", []):
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name,
                                   "input": c.args})
                push("assistant", blocks or [{"type": "text", "text": ""}])
            elif m["role"] == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": r["id"],
                               "content": tool_result_text(r["content"]),
                               "is_error": bool(r.get("is_error"))}
                              for r in m["results"]])
        return out

    def _body(self, system, messages, tools, max_tokens):
        wire_tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["schema"]} for t in tools]
        if wire_tools:
            # the tool list and the system prompt are the same every turn:
            # cache them (prompt caching)
            wire_tools[-1] = dict(wire_tools[-1], cache_control={"type": "ephemeral"})
        body = {"model": self.model, "max_tokens": int(max_tokens),
                "system": [{"type": "text", "text": system,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": self.wire_messages(messages)}
        if wire_tools:
            body["tools"] = wire_tools
        return body

    def turn(self, system, messages, tools, max_tokens=4096) -> Turn:
        out = self.http.request("POST", self.base_url + "/v1/messages",
                                self._headers(), self._body(system, messages, tools, max_tokens))
        return self._parse(out)

    def _parse(self, out) -> Turn:
        if out.get("type") == "error":
            raise ProviderError(SERVER, str(out.get("error")))
        text, calls = [], []
        for b in out.get("content", []):
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                calls.append(Call(b["id"], b["name"], b.get("input") or {}))
        u = out.get("usage", {}) or {}
        usage = Usage(input=int(u.get("input_tokens", 0))
                      + int(u.get("cache_creation_input_tokens", 0) or 0)
                      + int(u.get("cache_read_input_tokens", 0) or 0),
                      output=int(u.get("output_tokens", 0)),
                      cached=int(u.get("cache_read_input_tokens", 0) or 0))
        return Turn(text="".join(text), calls=calls, raw=out.get("content", []),
                    usage=usage, stop=out.get("stop_reason", ""))

    def stream_turn(self, system, messages, tools, max_tokens, on_text) -> Turn:
        import json as _json
        body = dict(self._body(system, messages, tools, max_tokens), stream=True)
        kind, got = self.http.stream("POST", self.base_url + "/v1/messages",
                                     self._headers(), body)
        if kind == "json":
            return self._parse(got)
        # blocks by their index (deltas name it); thinking blocks are kept
        # whole -- text AND signature -- because a tool-use turn must send
        # them back unchanged
        blocks, partial, usage, stop = {}, {}, {}, ""
        for ev, data in got:
            try:
                d = _json.loads(data)
            except ValueError:
                continue
            t = d.get("type", ev)
            if t == "message_start":
                usage.update((d.get("message") or {}).get("usage") or {})
            elif t == "content_block_start":
                i = d.get("index", len(blocks))
                b = dict(d.get("content_block") or {})
                if b.get("type") in ("tool_use", "server_tool_use"):
                    b["input"] = {}
                    partial[i] = []
                blocks[i] = b
            elif t == "content_block_delta":
                i = d.get("index", max(blocks) if blocks else 0)
                b = blocks.setdefault(i, {"type": "text", "text": ""})
                delta = d.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    b["text"] = b.get("text", "") + delta.get("text", "")
                    on_text(delta.get("text", ""))
                elif dt == "input_json_delta":
                    partial.setdefault(i, []).append(delta.get("partial_json", ""))
                elif dt == "thinking_delta":
                    b["thinking"] = b.get("thinking", "") + delta.get("thinking", "")
                elif dt == "signature_delta":
                    b["signature"] = b.get("signature", "") + delta.get("signature", "")
                elif dt == "citations_delta":
                    b.setdefault("citations", []).append(delta.get("citation"))
            elif t == "content_block_stop":
                i = d.get("index", max(blocks) if blocks else 0)
                if i in partial:
                    raw = "".join(partial.pop(i)) or "{}"
                    try:
                        blocks[i]["input"] = _json.loads(raw)
                    except ValueError:
                        blocks[i]["input"] = {"_unparsed": raw}
            elif t == "message_delta":
                stop = (d.get("delta") or {}).get("stop_reason") or stop
                usage.update(d.get("usage") or {})
            elif t == "error":
                raise ProviderError(SERVER, str(d.get("error")))
        return self._parse({"content": [blocks[i] for i in sorted(blocks)],
                            "stop_reason": stop, "usage": usage})


def _replayable(blocks):
    """An earlier assistant turn as it may be sent back. A thinking block
    is only accepted whole (its text and signature); one that arrived
    incomplete cannot be verified by the service, so it is left out rather
    than failing the request. Everything else goes back unchanged."""
    out = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "thinking":
            if not b.get("signature") or "thinking" not in b:
                continue
        out.append(b)
    return out or [{"type": "text", "text": ""}]
