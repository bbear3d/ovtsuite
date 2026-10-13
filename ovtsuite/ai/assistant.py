# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The in-app assistant: a conversation with one AI service that can use the
same tools an MCP client gets (:mod:`.core_tools` + module tools).

* Which service: the one chosen in AI settings; with none chosen, a local
  server (Ollama / LM Studio) when one runs -- free, offline, private.
* Everything sent to a cloud service goes through :mod:`.redact` first
  (on by default), and every request is kept in :data:`SENT` -- the "what
  was sent" log the settings dialog shows (never the key).
* Offline with a cloud service and a local server running: the question is
  answered locally instead, and the reply says so (setting
  ``fallback_local``).
* Token use is added to the running totals in the AI settings.

No Qt: the panel (phase 4) drives :meth:`Assistant.ask` from a worker thread
and receives :class:`Event` callbacks.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

from . import config as _cfg
from . import keystore as _keys
from . import redact as _redact
from .providers import (AUTH, OFFLINE, SERVER, SPECS, HttpClient, Provider, ProviderError,
                        Usage, detect_local, make)
from .tools import Context, Registry, default_registry, to_text

#: the last requests sent to any AI service, newest last ("what was sent")
SENT: Deque[dict] = collections.deque(maxlen=60)
_SENT_LOCK = threading.Lock()


def _log_sent(service: str, rec: dict) -> None:
    rec = dict(rec, service=service)
    with _SENT_LOCK:
        SENT.append(rec)


def sent_log() -> List[dict]:
    with _SENT_LOCK:
        return list(SENT)


class NotConfigured(Exception):
    """No service chosen and no local server found: the user must pick one."""


@dataclass
class Event:
    kind: str                     # "tool" | "tool_result" | "text" | "notice"
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    service: str
    model: str
    usage: Usage
    tool_calls: int = 0
    notices: List[str] = field(default_factory=list)
    redacted: int = 0
    #: parameters the reply names: [(handle, id, title)], for links
    maps: List[tuple] = field(default_factory=list)
    #: proposals made while answering
    proposals: List[str] = field(default_factory=list)


def system_prompt(extra: str = "") -> str:
    from .core_tools import INSTRUCTIONS
    s = INSTRUCTIONS + (
        "\n\nYou are the assistant built into the app. The user sees your reply in a side panel. "
        "Be concise and specific; quote values with units; name parameters by their title "
        "and give their id once. For questions about using the app itself, use search_help "
        "and read_help and answer from the help pages. For data logs use log_summary first, "
        "then log_window for detail. For a goal that spans several maps or systems, work in "
        "steps: read the maps involved, try the changes with check_changes until there are no "
        "errors, then file them with propose_plan (or propose_change for one system). When it "
        "helps the user see what you mean, pull the table up with show_map.")
    return s + ("\n\n" + extra if extra else "")


def make_provider(service: str, cfg: Optional[dict] = None, *, model: str = "",
                  on_send: Optional[Callable] = None) -> Provider:
    cfg = cfg if cfg is not None else _cfg.load()
    spec = SPECS[service]
    key = _keys.get_key(service) or ""
    if spec.needs_key and not key:
        raise NotConfigured("%s needs an API key (Tools > AI > Settings)" % spec.label)
    http = HttpClient(timeout=180, on_send=on_send)
    return make(service, key=key, model=model or _cfg.model_for(cfg, service),
                base_url=_cfg.base_url_for(cfg, service), http=http)


def choose_service(cfg: dict) -> str:
    """The configured service, or a running local server, or NotConfigured."""
    s = cfg.get("service") or ""
    if s in SPECS:
        return s
    found = detect_local()
    if found:
        sid, models = found[0]
        if models and not _cfg.model_for(cfg, sid):
            cfg.setdefault("models", {})[sid] = models[0]
        return sid
    raise NotConfigured("No AI service is set up. Open Tools > AI > Settings to "
                        "choose one -- or start Ollama or LM Studio on this PC "
                        "for a free, offline one.")


class Assistant:
    def __init__(self, workspace, *, registry: Optional[Registry] = None,
                 cfg: Optional[dict] = None, service: Optional[str] = None,
                 provider: Optional[Provider] = None,
                 on_event: Optional[Callable[[Event], None]] = None,
                 call_tool: Optional[Callable] = None):
        self.cfg = cfg if cfg is not None else _cfg.load()
        self.registry = registry or default_registry()
        self.ctx = Context(workspace=workspace, client="assistant")
        self.on_event = on_event
        self.messages: List[dict] = []
        self._provider = provider
        self._service = service or (provider.spec.id if provider else None)
        self.cancelled = threading.Event()
        #: how a tool runs -- the panel routes tab tools to the GUI thread
        self.call_tool = call_tool or (lambda name, args: self.registry.call(
            name, self.ctx, args))
        #: map id -> (handle, title), every parameter a tool has shown
        self.seen_maps = {}

    # -- service --------------------------------------------------------------------
    @property
    def provider(self) -> Provider:
        if self._provider is None:
            if self._service is None:
                self._service = choose_service(self.cfg)
            self._provider = make_provider(self._service, self.cfg,
                                           on_send=self._on_send(self._service))
        if not self._provider.model:
            models = self._provider.list_models()
            if not models:
                raise NotConfigured("%s offers no models" % self._provider.label)
            self._provider.model = models[0]
        return self._provider

    def use_settings(self, cfg: Optional[dict] = None) -> None:
        """The AI settings changed: the next turn goes to the service and
        model they name now. The conversation is kept -- it is stored in a
        provider-neutral form, so another service can carry it on."""
        self.cfg = cfg if cfg is not None else _cfg.load()
        self._provider = None
        self._service = None

    def _on_send(self, service):
        return lambda rec: _log_sent(service, rec)

    @property
    def redacting(self) -> bool:
        p = self._provider
        local = p is not None and p.spec.local
        return bool(self.cfg.get("redact", True)) and not local

    def _emit(self, kind, text="", **data):
        if self.on_event is not None:
            try:
                self.on_event(Event(kind, text, data))
            except Exception:
                pass

    def _tools(self) -> List[dict]:
        return [{"name": t.name, "description": t.description, "schema": t.schema()}
                for t in self.registry.all()]

    # -- the loop ---------------------------------------------------------------------
    def ask(self, question: str, context: str = "") -> Reply:
        """Answer ``question``, using tools as needed. ``context`` is what
        the panel knows (active tab, selected map) and is added to the
        system prompt for this question only."""
        self.cancelled.clear()
        notices: List[str] = []
        try:
            return self._ask(question, context, notices)
        except ProviderError as ex:
            if ex.kind != OFFLINE or not self.cfg.get("fallback_local", True) \
                    or (self._provider is not None and self._provider.spec.local):
                raise
            found = detect_local()
            if not found:
                raise
            sid, models = found[0]
            label = SPECS[sid].label
            note = "Offline: answered by %s instead." % label
            notices.append(note)
            self._emit("notice", note)
            self._service = sid
            self._provider = make(sid, model=_cfg.model_for(self.cfg, sid) or
                                  (models[0] if models else ""),
                                  base_url=_cfg.base_url_for(self.cfg, sid),
                                  http=HttpClient(timeout=180, on_send=self._on_send(sid)))
            # the unanswered question was already appended: drop it and retry
            while self.messages and self.messages[-1]["role"] != "user":
                self.messages.pop()
            if self.messages:
                self.messages.pop()
            return self._ask(question, context, notices)

    #: answers that mean "busy right now, try again" (Anthropic 529
    #: overloaded, Gemini / OpenAI 503 high demand, gateway hiccups)
    TRANSIENT = (500, 502, 503, 504, 529)
    #: seconds to wait before each retry
    RETRY_WAITS = (3, 8, 20)

    def _complete(self, p, system, tools):
        """One model turn, tried again a few times when the service is only
        momentarily overloaded -- unless part of the answer was already
        shown (it would be shown twice) or the user pressed Stop."""
        stream = (self.on_event is not None and self.cfg.get("stream", True))
        waits = list(self.RETRY_WAITS)
        while True:
            shown = []

            def on_text(piece):
                shown.append(piece)
                self._emit("delta", piece)
            try:
                return p.complete(system, self.messages, tools,
                                  max_tokens=int(self.cfg.get("max_tokens", 4096)),
                                  on_text=on_text if stream else None)
            except ProviderError as ex:
                if ex.kind != SERVER or ex.status not in self.TRANSIENT or shown or not waits:
                    raise
                wait = waits.pop(0)
                self._emit("notice", "%s is busy (%d); trying again in %d s..."
                           % (p.label, ex.status, wait))
                if self.cancelled.wait(wait):
                    raise ProviderError("cancelled", "stopped")

    def _ask(self, question, context, notices) -> Reply:
        p = self.provider
        total = Usage()
        redacted = 0
        q = question
        if self.redacting:
            q, n = _redact.text(question)
            redacted += n
        self.messages.append({"role": "user", "text": q})
        system = system_prompt(context)
        tools = self._tools()
        calls_made = 0
        made: List[str] = []
        max_steps = int(self.cfg.get("max_steps", 12))
        for step in range(max_steps):
            if self.cancelled.is_set():
                raise ProviderError("cancelled", "stopped")
            turn = self._complete(p, system, tools)
            total.add(turn.usage)
            try:
                _cfg.record_usage(p.spec.id, p.model, turn.usage)
            except Exception:
                pass
            self.messages.append({"role": "assistant", "text": turn.text,
                                  "calls": turn.calls, "raw": turn.raw,
                                  "raw_kind": p.kind})
            if turn.text and turn.calls:
                self._emit("text", turn.text, partial=True)
            if not turn.calls:
                self._emit("text", turn.text)
                return Reply(turn.text, p.spec.id, p.model, total, calls_made,
                             notices, redacted, self._named(turn.text), made)
            results = []
            for c in turn.calls:
                calls_made += 1
                self._emit("tool", c.name, args=c.args)
                ok, res = self.call_tool(c.name, c.args)
                if ok:
                    self._collect(c.args.get("handle"), res)
                    if c.name == "propose_change" and isinstance(res, dict):
                        made.append(res.get("proposal", ""))
                    elif c.name == "propose_plan" and isinstance(res, dict):
                        made.extend(st.get("proposal", "") for st in res.get("steps", []))
                if self.redacting:
                    res, n = _redact.scrub(res)
                    redacted += n
                self._emit("tool_result", c.name, ok=ok)
                results.append({"id": c.id, "name": c.name, "content": to_text(res),
                                "is_error": not ok})
            self.messages.append({"role": "tool", "results": results})
        msg = "Stopped after %d tool rounds without a final answer." % max_steps
        return Reply(msg, p.spec.id, p.model, total, calls_made, notices, redacted,
                     [], made)

    def _collect(self, handle, res) -> None:
        """Remember every parameter summary a tool returned."""
        if isinstance(res, dict):
            if isinstance(res.get("id"), str) and "kind" in res and "title" in res:
                self.seen_maps[res["id"]] = (handle, res["title"])
            for v in res.values():
                self._collect(handle, v)
        elif isinstance(res, list):
            for v in res:
                self._collect(handle, v)

    def _named(self, text: str) -> List[tuple]:
        """The seen parameters the answer names, by id or by title."""
        out = []
        low = (text or "").lower()
        for mid, (handle, title) in self.seen_maps.items():
            if not handle:
                continue
            t = (title or "").lower()
            # a catalogue title carries its variant after " — " ("... — gears
            # 1-6 — coding 150034"); an answer usually names just the base
            base = t.split(" \u2014 ", 1)[0].strip()
            if mid.lower() in low or (t and t in low) or (len(base) > 6 and base in low):
                out.append((handle, mid, title))
        return out[:30]

    def reset(self) -> None:
        self.messages.clear()
