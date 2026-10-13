# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""AI settings, kept as JSON beside the proposals (no Qt: the MCP server and
the tests read them too). Keys are never here -- see :mod:`.keystore`."""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Optional

ENV_DIR = "OVT_AI_DIR"
FILE = "settings.json"

DEFAULTS = {
    #: the in-app assistant is off until the user turns it on (opt-in)
    "enabled": False,
    #: version of the "AI can make mistakes" notice the user acknowledged
    #: (0 = never); shown again if its wording changes materially
    "notice_accepted": 0,
    #: service id (ovtsuite.ai.providers.SPECS); "" = a local server if one
    #: runs, else ask
    "service": "",
    "models": {},                  # service id -> model id
    "base_urls": {},               # service id -> address (custom / non-default)
    #: VIN / immobiliser data scrubbed from everything sent (cloud services)
    "redact": True,
    #: offline with a cloud service: answer with a local model if one runs
    "fallback_local": True,
    "max_steps": 24,               # tool rounds per question (plans need more)
    "max_tokens": 4096,
    #: show the reply as it is written (falls back by itself where a service
    #: does not stream)
    "stream": True,
    "usage": {},                   # service -> model -> {input, output, cached, requests}
}

_lock = threading.Lock()


def ai_dir() -> str:
    override = os.environ.get(ENV_DIR)
    if override:
        return override
    from ..recent import _config_dir
    return os.path.join(_config_dir(), "ai")


def path() -> str:
    return os.path.join(ai_dir(), FILE)


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            cfg.update(d)
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    with _lock:
        os.makedirs(ai_dir(), exist_ok=True)
        tmp = path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        os.replace(tmp, path())


def update(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg


def record_usage(service: str, model: str, usage) -> None:
    """Add one request's tokens to the running totals."""
    with _lock:
        cfg = load()
        u = cfg.setdefault("usage", {}).setdefault(service, {}).setdefault(
            model or "?", {"input": 0, "output": 0, "cached": 0, "requests": 0})
        u["input"] += int(getattr(usage, "input", 0))
        u["output"] += int(getattr(usage, "output", 0))
        u["cached"] += int(getattr(usage, "cached", 0))
        u["requests"] += 1
    save(cfg)


def model_for(cfg: dict, service: str) -> str:
    return (cfg.get("models") or {}).get(service, "")


def base_url_for(cfg: dict, service: str) -> str:
    return (cfg.get("base_urls") or {}).get(service, "")
