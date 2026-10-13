# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The AI services the assistant can use, and how to make one.

Three wire formats cover them all: Anthropic's Messages API (Claude),
Google's Gemini API, and OpenAI's Chat Completions -- which OpenAI itself,
Groq, OpenRouter, Ollama, LM Studio and most other servers speak. A service
is a :class:`~.base.Spec` (address, whether it needs a key, where to get
one, what to know about it); adding one is a line here.

Local servers (Ollama, LM Studio) need no key, cost nothing, work offline
and keep everything on the PC: :func:`detect_local` finds them, and the
assistant uses one by default when no key has been entered.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .base import (AUTH, BAD, BLOCKED, OFFLINE, RATE, SERVER, Call, HttpClient, Provider,
                   ProviderError, Spec, Turn, Usage)

SPECS: Dict[str, Spec] = {s.id: s for s in (
    Spec("anthropic", "Claude (Anthropic)", "anthropic", "https://api.anthropic.com",
         key_url="https://platform.claude.com", env="ANTHROPIC_API_KEY",
         note="Paid, per use. Your requests are not used for training."),
    Spec("openai", "OpenAI", "openai", "https://api.openai.com/v1",
         key_url="https://platform.openai.com/api-keys", env="OPENAI_API_KEY",
         note="Paid, per use."),
    Spec("gemini", "Google Gemini", "gemini",
         "https://generativelanguage.googleapis.com/v1beta",
         key_url="https://aistudio.google.com/apikey", env="GEMINI_API_KEY",
         note="Has a free tier with a free key, but Google states that free-tier "
              "content is used to improve its products: your calibrations would "
              "be part of that. The paid tier is not."),
    Spec("groq", "Groq", "openai", "https://api.groq.com/openai/v1",
         key_url="https://console.groq.com/keys", env="GROQ_API_KEY",
         note="Free tier with a free key (no card); daily request limits."),
    Spec("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1",
         key_url="https://openrouter.ai/keys", env="OPENROUTER_API_KEY",
         note="One key for many models. Models ending in \":free\" cost nothing "
              "(a free key; a small daily request limit)."),
    Spec("deepseek", "DeepSeek", "openai", "https://api.deepseek.com",
         key_url="https://platform.deepseek.com/api_keys", env="DEEPSEEK_API_KEY",
         note="Paid, per use. DeepSeek stores what you send in the "
              "People's Republic of China and may use it to train its models (you can "
              "ask privacy@deepseek.com to opt out)."),
    Spec("ollama", "Ollama (on this PC)", "openai", "http://127.0.0.1:11434/v1",
         needs_key=False, local=True, key_url="https://ollama.com/download",
         note="Free, offline, nothing leaves this PC. Needs Ollama running and a "
              "model that supports tools; a capable PC helps."),
    Spec("lmstudio", "LM Studio (on this PC)", "openai", "http://127.0.0.1:1234/v1",
         needs_key=False, local=True, key_url="https://lmstudio.ai",
         note="Free, offline, nothing leaves this PC. Start LM Studio's local "
              "server with a model that supports tools."),
    Spec("custom", "Other OpenAI-compatible server", "openai", "",
         needs_key=False,
         note="Any server that speaks OpenAI's Chat Completions API. Give its "
              "address (ending in /v1) and a key if it needs one."),
)}

LOCAL = tuple(s.id for s in SPECS.values() if s.local)


def adapter(kind: str):
    if kind == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider
    if kind == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider
    from .openai_compat import OpenAIProvider
    return OpenAIProvider


def make(spec_id: str, *, key: str = "", model: str = "", base_url: str = "",
         http: Optional[HttpClient] = None) -> Provider:
    spec = SPECS.get(spec_id)
    if spec is None:
        raise ValueError("unknown AI service %r" % spec_id)
    return adapter(spec.kind)(spec, key=key, model=model, base_url=base_url, http=http)


def detect_local(timeout: float = 0.8, http: Optional[HttpClient] = None
                 ) -> List[Tuple[str, List[str]]]:
    """Local servers that answer now, with their models: ``[(id, models)]``."""
    found = []
    for sid in LOCAL:
        p = make(sid, http=http or HttpClient(timeout=timeout))
        try:
            models = p.list_models()
        except ProviderError:
            continue
        found.append((sid, models))
    return found


__all__ = ["SPECS", "LOCAL", "make", "detect_local", "adapter", "Spec", "Provider",
           "ProviderError", "Turn", "Call", "Usage", "HttpClient",
           "OFFLINE", "AUTH", "RATE", "SERVER", "BAD", "BLOCKED"]
