# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which modules are installed, and which one serves a platform.

Base code reaches module code ONLY through here::

    mod = registry.for_platform("zf6hp")       # ModuleSpec, or None
    cs = registry.checksum("zf6hp", session)   # ChecksumProvider, or None

A module that is absent (not installed, disabled, or built for an API level
this base does not support) simply is not in ``installed()``; callers get
``None`` and say so to the user (:func:`missing_text`).
"""

import importlib
from typing import Dict, List, Optional, Tuple

from .. import platforms as _platforms
from ..branding import PKG as _PKG
from . import api as _api
from . import loader as _loader

_cache: Optional[Dict[str, _api.ModuleSpec]] = None
_problems: Dict[str, str] = {}


def reset() -> None:
    """Forget what was discovered (after modules are installed, removed or
    hidden). Caches elsewhere that depend on which modules exist are reset
    here too."""
    import sys
    global _cache
    _cache = None
    _problems.clear()
    sv = sys.modules.get(_PKG + ".speedview")
    if sv is not None:
        sv._EXT = False


def _discover() -> Dict[str, _api.ModuleSpec]:
    found: Dict[str, _api.ModuleSpec] = {}
    for mid in _platforms.MODULES:
        try:
            spec = importlib.import_module(".%s.module" % mid, _PKG).MODULE
        except ModuleNotFoundError as ex:
            # absent, or hidden by the loader -- but a module whose own code
            # is broken must not pass for "not installed" silently
            if ex.name and not ex.name.startswith("%s.%s" % (_PKG, mid)) \
                    and ex.name != _PKG:
                _problems[mid] = "failed to load: %s" % ex
            continue
        except Exception as ex:                  # noqa: BLE001 - report, skip
            _problems[mid] = "failed to load: %s" % ex
            continue
        if spec.id != mid:
            _problems[mid] = "declares id %r" % spec.id
            continue
        man = _loader.manifest(mid)
        if man is not None:
            # an installed module: its manifest says which variant and
            # version this copy is (module.py is the same in both variants)
            import dataclasses
            spec = dataclasses.replace(spec, version=man.version)
            if man.variant == _api.CHECKSUM_ONLY:
                from .package import checksum_only
                spec = checksum_only(spec)
        if spec.module_api not in _api.SUPPORTED_API:
            _problems[mid] = ("built for module API %d; this app supports %s"
                              % (spec.module_api, ", ".join(map(str, _api.SUPPORTED_API))))
            continue
        found[mid] = spec
    return found


def installed() -> Dict[str, _api.ModuleSpec]:
    global _cache
    if _cache is None:
        _cache = _discover()
    return _cache


def problems() -> Dict[str, str]:
    """Modules present but refused, and why (for the Module Manager)."""
    installed()
    out = _loader.problems()
    out.update(_problems)
    return out


def is_installed(module_id: str) -> bool:
    return module_id in installed()


def spec(module_id: str) -> Optional[_api.ModuleSpec]:
    return installed().get(module_id)


def for_platform(pid: str) -> Optional[_api.ModuleSpec]:
    """The installed module serving platform ``pid``, or None."""
    mid = _platforms.module_for(pid)
    return installed().get(mid) if mid else None


def can(pid: str, capability: str) -> bool:
    m = for_platform(pid)
    return m is not None and m.has(capability)


def eeprom_layouts() -> List[Tuple[_api.ModuleSpec, object]]:
    """``[(spec, layout)]``: the EEPROM layout of every installed module
    that has one (``ModuleSpec.eeprom``). A hook that fails is left out."""
    out = []
    for m in installed().values():
        if not getattr(m, "eeprom", None) or m.variant != _api.FULL:
            continue
        try:
            lay = m.call("eeprom")
        except Exception:                   # noqa: BLE001 - one module's fault
            continue
        if lay is not None:
            out.append((m, lay))
    return out


def checksum(pid: str, session=None, **kw) -> Optional[_api.ChecksumProvider]:
    """The checksum provider for platform ``pid`` (None when no installed
    module provides one). ``session`` is passed to providers that need more
    than the bytes (the VDO definition, the HVAC firmware map); ``kw`` go to
    the module's hook as they are (HVAC takes ``image=``, an HvacImage or a
    raw flash read, for the flasher)."""
    m = for_platform(pid)
    if m is None or not m.has(_api.CHECKSUM) or not m.checksum:
        return None
    return m.call("checksum", session, **kw)


#: What each platform's checksum code needs to recognise an image, for the
#: "no supported checksum" message (platforms not listed get a generic line).
_CHECKSUM_NEEDS = {
    "edc16": "a 1 MB EDC16U1 or 2 MB EDC16U31/U34 image with a 0xCAFECADE marker",
    "edc15": "a 512 KB EDC15 V4.1 image",
    "zf6hp": "a ZF 6HP image whose data-block boundaries are mapped",
}


def checksum_unsupported_text(pid: str, provider=None, data=None) -> str:
    """Why the installed module cannot check this image's checksum, naming
    the platform and module: the provider's own reason when it gives one
    (e.g. the HVAC module: this firmware's checksum is not mapped yet), else
    what the platform's checksum code needs."""
    if provider is not None:
        fn = getattr(provider, "unsupported_text", None)
        try:
            why = fn(data) if fn is not None else ""
        except Exception:                  # a reason is a nicety, never fatal
            why = ""
        if why:
            return why
    p = _platforms.platform(pid)
    mod = _platforms.MODULES[p.module].name if p.module else ""
    head = ("This %s image has no checksum layout the %s module supports"
            % (p.name, mod) if mod else
            "This image has no supported flash checksum")
    need = _CHECKSUM_NEEDS.get(pid)
    return "%s%s." % (head, " (need %s)" % need if need else "")


def missing_text(pid: str, what: str = "full support",
                 checksum: bool = True) -> str:
    """"Install the ZF 8HP module for full support." -- or why that cannot
    help (unknown platform, module present but refused). ``checksum=False``
    when the installed module cannot check THIS binary's checksum (an Aisin
    TR-80SD: the module has the 09D checksum only), so it is not called
    "checksum only"."""
    p = _platforms.platform(pid)
    if not p.module:
        from ..branding import APP_NAME
        return "No %s module supports this binary." % APP_NAME
    info = _platforms.MODULES[p.module]
    why = problems().get(p.module)
    if why and p.module not in installed():
        return "The %s module is installed but %s." % (info.name, why)
    m = installed().get(p.module)
    if m is not None and not m.has(_api.DEFINITIONS):
        if m.variant == _api.CHECKSUM_ONLY:
            return ("The %s module installed here is the checksum-only variant; "
                    "install the full %s module for %s." % (info.name, info.name, what))
        return ("The %s module has no definitions for this binary yet"
                "%s." % (info.name, " (checksum only)"
                          if checksum and m.has(_api.CHECKSUM) else ""))
    return "Install the %s module for %s." % (info.name, what)
