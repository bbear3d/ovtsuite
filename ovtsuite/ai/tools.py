# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tool registry shared by the MCP server and the in-app assistant.

A :class:`Tool` is a name, a description written for a language model, a JSON
Schema for its arguments and a handler ``handler(ctx, **arguments) -> dict``.
The base app registers its tools in :mod:`.core_tools`; an installed module
adds its own through ``ModuleSpec.ai_tools`` (a hook returning a list of
:class:`Tool`), the same way modules extend help.
"""

from __future__ import annotations

import inspect
import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    """A problem the model should read and can act on (bad id, no such map,
    unsupported binary). Reported as a tool error, not a crash."""


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    #: JSON Schema of the arguments (an "object" schema)
    params: Dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}})
    #: True when the tool only reads. Proposals are not writes to a binary,
    #: but they do add to the inbox, so propose tools set this False.
    read_only: bool = True
    title: str = ""
    #: id of the module that contributed it ("" for the base app)
    module: str = ""

    def schema(self) -> Dict[str, Any]:
        s = dict(self.params)
        s.setdefault("type", "object")
        s.setdefault("properties", {})
        return s


@dataclass
class Context:
    """What a handler works against."""
    workspace: Any
    #: who is asking ("mcp:<client name>", "assistant") -- recorded on proposals
    client: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)


class Registry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError("duplicate AI tool name %r" % tool.name)
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, params=None, *,
             read_only: bool = True, title: str = "", module: str = ""):
        """Decorator form of :meth:`add`."""
        def deco(fn):
            self.add(Tool(name=name, description=description, handler=fn,
                          params=params or {"type": "object", "properties": {}},
                          read_only=read_only, title=title, module=module))
            return fn
        return deco

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def call(self, name: str, ctx: Context, arguments: Optional[dict] = None):
        """Run a tool. Returns ``(ok, result)``: ``result`` is the handler's
        dict, or ``{"error": text}`` when ``ok`` is False."""
        tool = self._tools.get(name)
        if tool is None:
            return False, {"error": "no tool named %r" % name}
        args = dict(arguments or {})
        err = _check_args(tool, args)
        if err:
            return False, {"error": err}
        try:
            return True, tool.handler(ctx, **args)
        except ToolError as ex:
            return False, {"error": str(ex)}
        except Exception as ex:                      # a bug, not the model's
            traceback.print_exc(file=sys.stderr)
            return False, {"error": "internal error in %s: %s: %s"
                           % (name, type(ex).__name__, ex)}


def _check_args(tool: Tool, args: dict) -> str:
    """Light validation: required present, no unknown names, basic types.
    Enough to give the model a readable message instead of a TypeError."""
    s = tool.schema()
    props = s.get("properties", {})
    missing = [k for k in s.get("required", []) if k not in args]
    if missing:
        return "%s: missing required argument(s): %s" % (tool.name, ", ".join(missing))
    unknown = [k for k in args if k not in props]
    if unknown:
        return "%s: unknown argument(s): %s (expected: %s)" % (
            tool.name, ", ".join(unknown), ", ".join(props) or "none")
    types = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "array": list, "object": dict}
    for k, v in args.items():
        t = props.get(k, {}).get("type")
        py = types.get(t) if isinstance(t, str) else None
        if py is None or v is None:
            continue
        if t in ("integer", "number") and isinstance(v, bool):
            return "%s: %s must be a %s" % (tool.name, k, t)
        if not isinstance(v, py):
            return "%s: %s must be a %s" % (tool.name, k, t)
    return ""


def module_tools() -> List[Tool]:
    """Tools contributed by installed modules (``ModuleSpec.ai_tools``). A
    module whose hook fails is skipped -- it must not take the server down."""
    out: List[Tool] = []
    try:
        from ..modules import registry as _reg
        specs = _reg.installed()
    except Exception:
        return out
    for mid, spec in sorted(specs.items()):
        try:
            if getattr(spec, "ai_tools", None):
                made = spec.call("ai_tools") or ()
            else:
                # by convention: <module package>/ai_tools.py with tools() --
                # works with a module.py written for any app version, since
                # nothing new has to be passed to ModuleSpec
                made = _conventional(mid)
            for t in made:
                t.module = t.module or mid
                out.append(t)
        except Exception:
            traceback.print_exc(file=sys.stderr)
    return out


def _conventional(mid: str):
    import importlib
    import importlib.util
    from .. import branding
    name = "%s.%s.ai_tools" % (branding.PKG, mid)
    try:
        if importlib.util.find_spec(name) is None:
            return ()
    except (ImportError, ValueError):
        return ()
    mod = importlib.import_module(name)
    fn = getattr(mod, "tools", None)
    return fn() if callable(fn) else ()


def default_registry(include_modules: bool = True) -> Registry:
    """The base tools plus every installed module's."""
    from . import core_tools
    reg = Registry()
    core_tools.register(reg)
    if include_modules:
        for t in module_tools():
            if reg.get(t.name) is None:
                reg.add(t)
    return reg


def to_text(result: Any) -> str:
    """A tool result as the text a model reads: compact JSON."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                      default=str)
