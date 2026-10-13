# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Google Gemini, through the Gemini API's generateContent."""

from __future__ import annotations

import json
from typing import List

from .base import Call, Provider, ProviderError, BAD, Turn, Usage

#: Schema keys Gemini's function declarations accept (an OpenAPI subset)
_KEEP = {"type", "description", "properties", "required", "items", "enum",
         "format", "nullable", "minimum", "maximum", "minItems", "maxItems"}


def gemini_schema(s):
    """A JSON Schema reduced to what Gemini accepts; arrays always get items."""
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k not in _KEEP:
            continue
        if k == "properties":
            out[k] = {n: gemini_schema(p) for n, p in v.items()}
        elif k == "items":
            out[k] = gemini_schema(v)
        else:
            out[k] = v
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "string"}
    return out


class GeminiProvider(Provider):
    kind = "gemini"

    def _headers(self):
        return {"x-goog-api-key": self.key}

    def list_models(self) -> List[str]:
        out = self.http.request("GET", self.base_url + "/models?pageSize=200",
                                self._headers())
        ids = []
        for m in out.get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                ids.append(m.get("name", "").split("/", 1)[-1])
        return [i for i in ids if i]

    @staticmethod
    def wire_contents(messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "parts": [{"text": m["text"]}]})
            elif m["role"] == "assistant":
                if m.get("raw") is not None and m.get("raw_kind") == "gemini":
                    out.append(m["raw"])               # thought signatures intact
                    continue
                parts = [{"text": m["text"]}] if m.get("text") else []
                for c in m.get("calls", []):
                    parts.append({"functionCall": {"name": c.name, "args": c.args}})
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif m["role"] == "tool":
                parts = []
                for r in m["results"]:
                    content = r["content"]
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except ValueError:
                            pass
                    resp = content if isinstance(content, dict) else {"result": content}
                    if r.get("is_error"):
                        resp = {"error": content}
                    fr = {"name": r["name"], "response": resp}
                    if r.get("id") and not str(r["id"]).startswith("gemini_"):
                        fr["id"] = r["id"]
                    parts.append({"functionResponse": fr})
                out.append({"role": "user", "parts": parts})
        return out

    def _body(self, system, messages, tools, max_tokens):
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": self.wire_contents(messages),
                "generationConfig": {"maxOutputTokens": int(max_tokens)}}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"],
                 "parameters": gemini_schema(t["schema"])} for t in tools]}]
        return body

    def _model(self):
        return self.model if not self.model.startswith("models/") else self.model[7:]

    def turn(self, system, messages, tools, max_tokens=4096) -> Turn:
        out = self.http.request("POST", "%s/models/%s:generateContent" % (
            self.base_url, self._model()), self._headers(),
            self._body(system, messages, tools, max_tokens))
        return self._parse(out)

    def _parse(self, out) -> Turn:
        cands = out.get("candidates") or []
        if not cands:
            fb = out.get("promptFeedback", {})
            raise ProviderError(BAD, "no answer (%s)" % (fb.get("blockReason") or "empty reply"))
        content = cands[0].get("content") or {"role": "model", "parts": []}
        text, calls = [], []
        for i, p in enumerate(content.get("parts", [])):
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(Call(fc.get("id") or "gemini_%d" % i, fc.get("name", ""),
                                  fc.get("args") or {}))
            elif "text" in p and not p.get("thought"):
                text.append(p["text"])
        u = out.get("usageMetadata", {}) or {}
        usage = Usage(input=int(u.get("promptTokenCount", 0) or 0),
                      output=int(u.get("candidatesTokenCount", 0) or 0),
                      cached=int(u.get("cachedContentTokenCount", 0) or 0))
        content.setdefault("role", "model")
        return Turn(text="".join(text), calls=calls, raw=content, usage=usage,
                    stop=cands[0].get("finishReason", ""))

    def stream_turn(self, system, messages, tools, max_tokens, on_text) -> Turn:
        kind, got = self.http.stream(
            "POST", "%s/models/%s:streamGenerateContent?alt=sse" % (self.base_url, self._model()),
            self._headers(), self._body(system, messages, tools, max_tokens))
        if kind == "json":
            return self._parse(got)
        parts, usage, finish, feedback = [], {}, "", {}
        for _ev, data in got:
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if d.get("usageMetadata"):
                usage = d["usageMetadata"]
            if d.get("promptFeedback"):
                feedback = d["promptFeedback"]
            for c in d.get("candidates") or []:
                finish = c.get("finishReason") or finish
                for p in (c.get("content") or {}).get("parts", []):
                    if "text" in p and not p.get("thought") and parts and \
                            "text" in parts[-1] and not parts[-1].get("thought") and \
                            set(p) == {"text"} and set(parts[-1]) == {"text"}:
                        parts[-1]["text"] += p["text"]      # one text part, as unstreamed
                    else:
                        parts.append(dict(p))
                    if "text" in p and not p.get("thought") and p["text"]:
                        on_text(p["text"])
        out = {"usageMetadata": usage}
        if parts:
            out["candidates"] = [{"content": {"role": "model", "parts": parts},
                                  "finishReason": finish}]
        else:
            out["promptFeedback"] = feedback
        return self._parse(out)
