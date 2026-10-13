# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OpenAI's Chat Completions API, and every service that speaks it: Groq,
OpenRouter, DeepSeek, Ollama, LM Studio, or any other OpenAI-compatible
server."""

from __future__ import annotations

import json
from typing import List

from .base import Call, Provider, ProviderError, BAD, Turn, Usage, tool_result_text


class OpenAIProvider(Provider):
    kind = "openai"

    def _headers(self):
        h = {}
        if self.key:
            h["Authorization"] = "Bearer %s" % self.key
        if "openrouter.ai" in self.base_url:
            from ... import branding
            h["X-Title"] = branding.APP_NAME           # shown in their usage list
        return h

    def list_models(self) -> List[str]:
        out = self.http.request("GET", self.base_url + "/models", self._headers())
        data = out.get("data", out.get("models", []))
        return [m.get("id") or m.get("name") for m in data if isinstance(m, dict)
                and (m.get("id") or m.get("name"))]

    @staticmethod
    def wire_messages(system, messages, server: str = ""):
        """``server`` (the address in use): an earlier answer's reasoning
        goes back only to the server that wrote it -- DeepSeek's thinking
        mode requires it with tools; another server could refuse the field."""
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("text") or None}
                if m.get("calls"):
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name,
                                                       "arguments": json.dumps(c.args)}}
                                         for c in m["calls"]]
                elif msg["content"] is None:
                    msg["content"] = ""
                raw = m.get("raw")
                if (m.get("raw_kind") == "openai" and isinstance(raw, dict)
                        and isinstance(raw.get("reasoning_content"), str)
                        and raw.get("_server") == server):
                    msg["reasoning_content"] = raw["reasoning_content"]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"],
                                "content": tool_result_text(r["content"])})
        return out

    def _body(self, system, messages, tools, max_tokens):
        body = {"model": self.model,
                "messages": self.wire_messages(system, messages, self.base_url)}
        if tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": t["name"], "description": t["description"],
                                           "parameters": t["schema"]}} for t in tools]
        # OpenAI's own API takes max_completion_tokens on current models;
        # compatible servers take max_tokens
        if "api.openai.com" in self.base_url:
            body["max_completion_tokens"] = int(max_tokens)
        else:
            body["max_tokens"] = int(max_tokens)
        return body

    def turn(self, system, messages, tools, max_tokens=4096) -> Turn:
        out = self.http.request("POST", self.base_url + "/chat/completions",
                                self._headers(), self._body(system, messages, tools, max_tokens))
        return self._parse(out)

    def _parse(self, out) -> Turn:
        choices = out.get("choices") or []
        if not choices:
            raise ProviderError(BAD, "no answer in the reply: %s" % json.dumps(out)[:300])
        msg = choices[0].get("message", {}) or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed": fn.get("arguments")}
            if not isinstance(args, dict):
                args = {"_unparsed": args}
            calls.append(Call(tc.get("id") or "call_%d" % i, fn.get("name", ""), args))
        u = out.get("usage", {}) or {}
        det = u.get("prompt_tokens_details") or {}
        usage = Usage(input=int(u.get("prompt_tokens", 0) or 0),
                      output=int(u.get("completion_tokens", 0) or 0),
                      cached=int(det.get("cached_tokens", 0) or 0))
        content = msg.get("content")
        if isinstance(content, list):                  # some servers use parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        raw = dict(msg, _server=self.base_url)        # which server wrote it
        return Turn(text=content or "", calls=calls, raw=raw, usage=usage,
                    stop=choices[0].get("finish_reason", ""))

    def stream_turn(self, system, messages, tools, max_tokens, on_text) -> Turn:
        body = dict(self._body(system, messages, tools, max_tokens), stream=True)
        if "api.openai.com" in self.base_url:
            body["stream_options"] = {"include_usage": True}
        kind, got = self.http.stream("POST", self.base_url + "/chat/completions",
                                     self._headers(), body)
        if kind == "json":
            return self._parse(got)
        text, reasoning, calls, usage, finish = [], [], {}, {}, ""
        for _ev, data in got:
            if data.strip() == "[DONE]":
                break
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if d.get("error"):
                raise ProviderError(BAD, str(d["error"]))
            if d.get("usage"):
                usage = d["usage"]
            for ch in d.get("choices") or []:
                delta = ch.get("delta") or {}
                think = delta.get("reasoning_content")
                if isinstance(think, str) and think:
                    reasoning.append(think)
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    text.append(piece)
                    on_text(piece)
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", len(calls))
                    c = calls.setdefault(i, {"id": "", "type": "function",
                                             "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        c["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        c["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        c["function"]["arguments"] += fn["arguments"]
                finish = ch.get("finish_reason") or finish
        msg = {"role": "assistant", "content": "".join(text) or None}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [calls[k] for k in sorted(calls)]
        return self._parse({"choices": [{"message": msg, "finish_reason": finish}],
                            "usage": usage})
