# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Reading packs: a directory of YAML/JSON documents, or a compiled
``pack.json.gz``; and where the app's packs live.

Three origins, in the resolver's layering order (user beats module beats
bundled): the pack bundled with the app (``ovtsuite/diag/data/definitions``),
packs an installed ``.ovtmod`` carries (its ``definitions/`` directory), and
the user's own packs beside the app's settings (``<config>/diag/definitions``).
"""
import gzip
import hashlib
import json
import os
import zlib
from typing import Any, Dict, Iterable, List, Optional

from . import yamlio
from .model import FORMAT, KINDS, Doc, Match, Pack
from .yamlio import DefsError

MANIFEST_NAMES = ("pack.yaml", "pack.yml", "pack.json")
#: an installed pack's replaced copy (``<id>.previous``): kept, never loaded
PREVIOUS_SUFFIX = ".previous"
COMPILED_NAME = "pack.json.gz"


def bundled_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data", "definitions")


def user_dir() -> str:
    """The user's local packs: ``<config>/diag/definitions`` (decision 3 of
    the schema doc: beside the app's settings, every OS)."""
    from ...flash.headunits import config_dir
    return os.path.join(config_dir(), "diag", "definitions")


def installed_module_ids(errors: Optional[list] = None) -> List[str]:
    """Ids of the platform modules the app has (source tree or installed
    ``.ovtmod``), sorted; empty when the module registry is unavailable --
    then said in ``errors`` (``(where, message)``) when the caller passes
    a list, never silently."""
    try:
        from ...modules import registry as _reg
        return sorted(_reg.installed())
    except Exception as ex:
        if errors is not None:
            errors.append(("modules", "module registry unavailable: %s" % ex))
        return []


def module_package_dirs(mid: str) -> List[str]:
    """The directories that make up the package ``ovtsuite.<mid>`` -- the
    source tree's, or the mounted module root's -- without importing it."""
    try:
        import importlib.util
        from ... import branding as _branding
        spec = importlib.util.find_spec("%s.%s" % (_branding.PKG, mid))
    except Exception:
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    return list(spec.submodule_search_locations)


def module_dirs(errors: Optional[list] = None) -> List[str]:
    """``definitions/`` directories inside the installed platform modules
    (``ovtsuite/<id>/definitions/``), in module-id order -- in the source
    tree or mounted from an installed ``.ovtmod`` alike, since a mounted
    module's root joins the package path. Each holds one pack directory per
    pack (``edc16-faults/pack.yaml`` ...); a module without one is skipped."""
    out: List[str] = []
    for mid in installed_module_ids(errors):
        for base in module_package_dirs(mid):
            d = os.path.join(base, "definitions")
            if os.path.isdir(d) and d not in out:
                out.append(d)
    return out


def _manifest_path(path: str) -> Optional[str]:
    for n in MANIFEST_NAMES:
        p = os.path.join(path, n)
        if os.path.isfile(p):
            return p
    return None


def is_pack_dir(path: str) -> bool:
    return os.path.isdir(path) and (_manifest_path(path) is not None
                                    or os.path.isfile(os.path.join(path, COMPILED_NAME)))


def _pack_from_manifest(man: Dict[str, Any], path: str, origin: str, where: str = "") -> Pack:
    """A :class:`Pack` from a manifest mapping; ``where`` names the file in
    messages (the manifest, or the compiled pack it was read from)."""
    where = where or path
    if not isinstance(man, dict):
        raise DefsError("%s: the manifest is not a mapping" % where)
    fmt = man.get("format")
    if fmt != FORMAT:
        raise DefsError("%s: format %r; this app reads format %d" % (where, fmt, FORMAT))
    pid = man.get("id")
    if pid is None or pid == "":
        pid = os.path.basename(os.path.normpath(path))
    if not isinstance(pid, str):
        raise DefsError("%s: 'id' must be text, not %s" % (where, type(pid).__name__))
    version = man.get("version", "0.0.0")
    if version is None or version == "":
        version = "0.0.0"
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise DefsError("%s: 'version' must be text like \"1.2.0\" (quote it)" % where)
    requires = man.get("requires") or {}
    if not isinstance(requires, dict):
        raise DefsError("%s: 'requires' must be a mapping (ovtdiag: \">=0.1\"), not %s"
                        % (where, type(requires).__name__))
    depends = man.get("depends") or []
    if isinstance(depends, str):
        depends = [depends]
    if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
        raise DefsError("%s: 'depends' must be a list of pack ids" % where)
    coverage = man.get("coverage") or {}
    if not isinstance(coverage, dict):
        raise DefsError("%s: 'coverage' must be a mapping" % where)
    return Pack(id=pid, version=str(version),
                title=str(man.get("title", "") or ""), publisher=str(man.get("publisher", "") or ""),
                license=str(man.get("license", "") or ""), requires={str(k): str(v) for k, v in requires.items()},
                depends=list(depends), coverage=dict(coverage),
                path=path, origin=origin)


#: earlier spellings, accepted on load and normalised
_KIND_ALIASES = {"output_tests": "actuator_tests"}
_VARIANT_ALIASES = {"output_tests": "actuator_tests"}


def _doc_from_body(body: Any, path: str) -> Doc:
    if not isinstance(body, dict):
        raise DefsError("%s: a document is a mapping with a 'kind'" % path)
    if body.get("kind") in _KIND_ALIASES:
        body["kind"] = _KIND_ALIASES[body["kind"]]
    if body.get("kind") == "module":
        var = body.get("variants")
        if isinstance(var, dict):
            for old, new in _VARIANT_ALIASES.items():
                if old in var and new not in var:
                    var[new] = var.pop(old)
        sec = body.get("security")
        if isinstance(sec, dict):
            for k in ("functions_needing_security", "functions_needing_login"):
                if isinstance(sec.get(k), list):
                    sec[k] = [_VARIANT_ALIASES.get(x, x) for x in sec[k]]
    kind = body.get("kind")
    if kind not in KINDS:
        raise DefsError("%s: kind %r is not one of %s" % (path, kind, ", ".join(KINDS)))
    return Doc(kind=str(kind), match=Match.from_doc(body.get("match")), body=body, path=path)


def iter_doc_files(path: str) -> Iterable[str]:
    """Every document file under a pack directory, in a stable order, the
    manifest excluded; ``data/`` is for binary catalogues, not documents."""
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "data")
        for fn in sorted(files):
            if fn in MANIFEST_NAMES or fn == COMPILED_NAME or fn.startswith("."):
                continue
            if yamlio.is_doc(fn):
                yield os.path.join(root, fn)


def source_files(path: str) -> List[str]:
    """A pack directory's source files: the manifest, then every document
    file; empty when it has no manifest."""
    man_path = _manifest_path(path)
    if man_path is None:
        return []
    return [man_path] + list(iter_doc_files(path))


def source_hash(path: str) -> str:
    """A digest of a pack directory's sources -- each file's relative path
    and content, line ends normalised so a Windows checkout agrees with the
    build that compiled it -- or "" when it has no manifest. Stored in the
    compiled form (``source_hash``) and compared on load: a compiled pack
    beside sources it was not built from is stale, whatever the file times
    say (a checkout does not keep them)."""
    files = source_files(path)
    if not files:
        return ""
    h = hashlib.sha256()
    for fp in files:
        h.update(os.path.relpath(fp, path).replace(os.sep, "/").encode("utf-8") + b"\0")
        with open(fp, "rb") as fh:
            h.update(fh.read().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def source_mtime(path: str) -> Optional[float]:
    """The newest modification time of a pack directory's source files --
    the manifest and every document file -- or None when it has none."""
    newest: Optional[float] = None
    for fp in source_files(path):
        try:
            m = os.path.getmtime(fp)
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def _compiled_matches_sources(path: str, compiled: Pack) -> bool:
    """Whether a directory's compiled pack stands for its sources: by the
    source hash it carries when it has one, else (an older compiled pack)
    by being at least as new as every source file -- editing any one
    document makes the sources win again, not only editing ``pack.yaml``."""
    if not source_files(path):
        return True                         # nothing else to read
    if compiled.source_hash:
        return compiled.source_hash == source_hash(path)
    src = source_mtime(path)
    comp = os.path.join(path, COMPILED_NAME)
    return src is None or os.path.getmtime(comp) >= src


def compiled_is_current(path: str) -> bool:
    """Whether loading the directory would take its ``pack.json.gz`` (see
    :func:`_compiled_matches_sources`); False without one or when it does
    not read."""
    comp = os.path.join(path, COMPILED_NAME)
    if not os.path.isfile(comp):
        return False
    try:
        return _compiled_matches_sources(path, load_compiled(comp))
    except DefsError:
        return False


def load_sources(path: str, origin: str = "user") -> Pack:
    """A pack from its directory's YAML/JSON documents only, never from a
    compiled form beside them (what compiling reads)."""
    path = os.path.abspath(path)
    man_path = _manifest_path(path)
    if man_path is None:
        raise DefsError("%s: no pack.yaml (or pack.json) -- not a definitions pack" % path)
    pack = _pack_from_manifest(yamlio.read(man_path), path, origin, man_path)
    for fp in iter_doc_files(path):
        body = yamlio.read(fp)
        if body is None:
            continue
        pack.docs.append(_doc_from_body(body, os.path.relpath(fp, path)))
    return pack


def load_pack(path: str, origin: str = "user") -> Pack:
    """A pack from its directory (YAML/JSON documents) or from its compiled
    ``pack.json.gz`` when that is all the directory holds. When both exist
    the compiled form is taken only when it stands for these sources
    (:func:`_compiled_matches_sources`); an edited document, in any file,
    makes the sources load."""
    path = os.path.abspath(path)
    if os.path.isfile(path):
        return load_compiled(path, origin)
    comp = os.path.join(path, COMPILED_NAME)
    if os.path.isfile(comp):
        if not source_files(path):
            return load_compiled(comp, origin)
        try:
            compiled = load_compiled(comp, origin)
        except DefsError:
            compiled = None                 # unreadable beside sources: the sources it is
        if compiled is not None and _compiled_matches_sources(path, compiled):
            return compiled
    return load_sources(path, origin)


def load_compiled(path: str, origin: str = "user") -> Pack:
    try:
        with gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, EOFError, ValueError, UnicodeDecodeError, zlib.error) as ex:
        # gzip's BadGzipFile is an OSError; a truncated member an EOFError; a
        # corrupt deflate stream a zlib.error; bad JSON a ValueError
        raise DefsError("%s: not a readable compiled pack (%s)" % (path, ex)) from ex
    if not isinstance(raw, dict) or "pack" not in raw:
        raise DefsError("%s: not a compiled pack" % path)
    pack = _pack_from_manifest(raw["pack"], os.path.dirname(path), origin, path)
    pack.source_hash = str(raw.get("source_hash") or "")
    docs = raw.get("docs") or []
    if not isinstance(docs, list):
        raise DefsError("%s: 'docs' must be a list" % path)
    for d in docs:
        if not isinstance(d, dict):
            raise DefsError("%s: a compiled document is a mapping with 'body'" % path)
        pack.docs.append(_doc_from_body(d.get("body"), str(d.get("path", "") or "")))
    return pack


def load_packs(dirs: Iterable[str], origin: str = "user", errors: Optional[list] = None) -> List[Pack]:
    """Every pack directory directly under each of ``dirs``, or the dir itself
    when it is a pack. Unreadable packs are skipped and reported in
    ``errors`` (``(path, message)``) rather than failing the whole load."""
    out = []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        cands = [d] if is_pack_dir(d) else [os.path.join(d, n) for n in sorted(os.listdir(d))
                                            if not n.endswith(PREVIOUS_SUFFIX)]
        for c in cands:
            if not is_pack_dir(c):
                continue
            try:
                out.append(load_pack(c, origin))
            except Exception as ex:          # one bad pack never takes the others down
                if errors is not None:
                    errors.append((c, "%s%s" % ("" if isinstance(ex, (DefsError, OSError))
                                                else type(ex).__name__ + ": ", ex)))
    return out


def load_all(errors: Optional[list] = None, user: bool = True) -> List[Pack]:
    """The app's packs in layering order: bundled, modules, user."""
    packs = load_packs([bundled_dir()], "bundled", errors)
    packs += load_packs(module_dirs(errors), "module", errors)
    if user:
        packs += load_packs([user_dir()], "user", errors)
    return packs
