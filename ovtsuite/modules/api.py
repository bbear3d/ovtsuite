# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The contract between the base app and a module.

A module is a package ``<app package>.<id>`` (the id IS the package name)
whose ``module.py`` defines ``MODULE = ModuleSpec(...)``. Hooks are strings
relative to the module's own package (``".session6hp:build"``), resolved only
when used, so reading a spec never imports the module's heavy code.

``MODULE_API`` is the base app's module API level. A module declares the
level it was built for; the base loads any module whose level it supports
(``SUPPORTED_API``), so a base-only release does not force every module to
be rebuilt (Bear, 2026-10-10: "Module API level").

A module's ``version`` is its own, independent of the app's (Bear,
2026-10-10): stable modules are 1.x, the rest 0.x. Bump it in the module's
``module.py`` whenever its files change; the build warns when they changed
and the version did not.

Backwards compatibility (newer app, older module) -- the rules the base app
keeps, so an installed module keeps working across app updates:

* ``SUPPORTED_API`` only ever grows; a level is dropped only after a release
  that warned about it, and never silently.
* A hook is called through :meth:`ModuleSpec.call`, which passes only the
  keyword arguments the module's function accepts: the app can add an
  argument without breaking a module written before it existed.
* A capability the app added later is looked up with ``getattr`` and a
  fallback (e.g. a provider method), never assumed present.
* ``ModuleSpec`` fields only get added, with defaults; an older module's
  ``module.py`` always still constructs one.
* Released packages are kept (``Reference/ModuleReleases``) and
  ``tests/test_module_compat.py`` loads every one of them against the
  current app.
"""

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

#: The API level this base app implements.
MODULE_API = 1
#: Every module API level this base app can load.
SUPPORTED_API = (1,)

#: Capabilities a module can provide.
CHECKSUM = "checksum"          # verify / correct (all the flasher needs)
DEFINITIONS = "definitions"    # parameters, maps, the editor tree
TOOLS = "tools"                # platform tools (EEPROM editor, cluster tools ...)

#: Module variants. A "checksum" variant carries only the checksum code.
FULL = "full"
CHECKSUM_ONLY = "checksum"

#: The terms a module is published under: ``ModuleSpec.license["terms"]``,
#: an SPDX id. Without one, a module is closed source under the module EULA
#: (``modules/EULA.txt``), which the build puts in its package as
#: ``<id>/EULA.txt``; an open-source module keeps its licence text in its own
#: folder as ``<id>/LICENSE.txt``.
EULA_TERMS = "LicenseRef-OVT-Module-EULA"


def terms(license: Optional[dict]) -> str:
    """The SPDX id a spec's or manifest's ``license`` dict declares."""
    return (license or {}).get("terms") or EULA_TERMS


def eula_path(mid: str) -> str:
    """Where a closed module's EULA sits inside its package."""
    return "%s/EULA.txt" % mid


def license_path(mid: str) -> str:
    """Where an open-source module's licence text sits inside its package."""
    return "%s/LICENSE.txt" % mid


class ChecksumError(Exception):
    """A provider cannot work out this image's checksum (layout not
    recognised). Callers treat it as "unsupported", never as a crash."""


class NoDefinition(Exception):
    """Raised by a module's ``build`` when it recognises the platform but has
    no definition for this particular binary; the session then opens it in
    the universal tools with the reason."""


@dataclass(frozen=True)
class ModuleSpec:
    id: str                               # == package name under ovtsuite
    name: str
    version: str
    platforms: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    module_api: int = MODULE_API
    variant: str = FULL
    #: ``build(session, family, progress)`` -- populate a TunerSession.
    build: Optional[str] = None
    #: ``after_restore(session)`` -- re-derive what a stored definition skips.
    after_restore: Optional[str] = None
    #: ``checksum(session=None) -> ChecksumProvider | None``.
    checksum: Optional[str] = None
    #: ``load_kb(family, delta) -> kb`` for stored-definition restore
    #: (ovtsuite.defn); None when the platform has no shareable definition.
    load_kb: Optional[str] = None
    #: ``eeprom() -> layout`` -- the ECU EEPROM this module knows, for the
    #: base app's EEPROM editor (the protocol is in ``ovtsuite.gui_eeprom``).
    eeprom: Optional[str] = None
    #: ``ai_tools() -> [ovtsuite.ai.tools.Tool]`` -- tools this module gives
    #: the AI assistant and the MCP server (docs/AI_PLAN.md). Optional: a
    #: module built before it existed simply contributes none.
    ai_tools: Optional[str] = None
    #: Data files this module owns: globs relative to the app's ``data``
    #: directory. Its code is its package (``<app>/<id>/``). Every data file
    #: belongs to exactly one module or to the base app -- the build checks.
    data: Tuple[str, ...] = ()
    #: Data files the checksum-only variant needs (usually none).
    checksum_data: Tuple[str, ...] = ()
    #: Licensing slot (not enforced yet): {"required": False}, plus "terms"
    #: (see :data:`EULA_TERMS`) for a module published as open source.
    license: dict = field(default_factory=lambda: {"required": False})

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def resolve(self, hook: str) -> Any:
        """Import and return the object a hook names (None if unset)."""
        ref = getattr(self, hook)
        if not ref:
            return None
        mod, _, attr = ref.partition(":")
        from .. import branding
        obj = importlib.import_module(mod, "%s.%s" % (branding.PKG, self.id))
        for part in attr.split(".") if attr else ():
            obj = getattr(obj, part)
        return obj

    def call(self, hook: str, *args, **kw) -> Any:
        """Call a hook, passing only the keyword arguments its function
        accepts (all of them when it takes ``**kwargs``): a newer app can pass
        more than an older module knows about. None when the hook is unset."""
        fn = self.resolve(hook)
        if fn is None:
            return None
        return fn(*args, **accepted(fn, kw))


def accepted(fn, kw: dict) -> dict:
    """The part of ``kw`` that ``fn`` accepts."""
    if not kw:
        return kw
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return kw
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return kw
    names = {p.name for p in params
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    return {k: v for k, v in kw.items() if k in names}


class ChecksumProvider:
    """What a module's ``checksum`` hook returns.

    Every platform's checksum code already exposes this shape as plain
    functions; a provider adapts it and owns the platform's options, so
    callers pass the same keyword options everywhere and never branch on the
    platform:

    * ``digest`` (EDC16) -- also regenerate the KWP2000 content digest;
    * ``program_area`` (ZF 6HP) -- also rewrite the descriptor self-CRCs;
    * ``key_image`` (EDC16 MPC) -- the external image the MPC is keyed to.

    Unknown options are ignored. ``verify`` returns a report with ``.ok`` and
    ``.describe()``; ``correct`` returns the corrected bytes.
    """

    name = "checksum"

    def is_supported(self, data) -> bool:
        raise NotImplementedError

    def verify(self, data, **opts):
        raise NotImplementedError

    def correct(self, data, **opts) -> bytes:
        raise NotImplementedError

    def exempt_ranges(self, data, **opts):
        return []

    def unsupported_text(self, data) -> str:
        """Why ``data`` has no checksum this provider can check, in words for
        the user (shown when :meth:`is_supported` is False). "" lets the
        caller say it generically -- see
        :func:`ovtsuite.modules.registry.checksum_unsupported_text`."""
        return ""


class FunctionsProvider(ChecksumProvider):
    """A provider over a checksum module that exposes ``is_supported`` /
    ``verify`` / ``correct`` (/ ``exempt_ranges``) taking no options."""

    def __init__(self, funcs, name="checksum"):
        self._f = funcs
        self.name = name

    def is_supported(self, data) -> bool:
        return bool(self._f.is_supported(data))

    def verify(self, data, **opts):
        return self._f.verify(data)

    def correct(self, data, **opts) -> bytes:
        return self._f.correct(data)

    def exempt_ranges(self, data, **opts):
        fn = getattr(self._f, "exempt_ranges", None)
        return fn(data) if fn is not None else []

    def unsupported_text(self, data) -> str:
        fn = getattr(self._f, "unsupported_text", None)
        return fn(data) if fn is not None else ""


class SimpleReport:
    """A checksum report for providers whose code returns a bare bool/dict."""

    def __init__(self, ok: bool, text: str):
        self.ok = bool(ok)
        self._text = text

    def describe(self) -> str:
        return self._text
