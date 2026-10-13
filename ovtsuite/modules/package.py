# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.ovtmod`` package: one module, ready to install.

A zip holding::

    manifest.json   what the module is, and the SHA-256 of every file below
    signature       Ed25519 over manifest.json's exact bytes (see .signing)
    <id>/...        the module's package (Python source)
    data/...        the data files it owns

laid out exactly like the app's own package directory, so an installed module
(extracted to ``<modules dir>/<id>/``) is found by the normal import machinery
and its code finds its data next to itself. Two variants per module:

* ``full`` -- the whole package and every data file the module owns;
* ``checksum`` -- only what its checksum hook needs (the import closure of
  that hook, computed here from the source, never listed by hand), for users
  who flash but do not edit.

Both share one id: installing one replaces the other.
"""

import ast
import datetime
import fnmatch
import hashlib
import io
import json
import os
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import api as _api

FORMAT = 1
EXT = ".ovtmod"
MANIFEST = "manifest.json"
SIGNATURE = "signature"


class PackageError(Exception):
    """Not a usable .ovtmod (malformed, tampered, or for another app)."""


@dataclass
class Manifest:
    id: str
    name: str
    version: str
    module_api: int
    variant: str
    capabilities: List[str]
    platforms: List[str]
    files: Dict[str, str]                     # relpath -> sha256 hex
    license: dict = field(default_factory=lambda: {"required": False})
    app: str = ""                             # product it was built for
    built: str = ""
    format: int = FORMAT

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), indent=1, sort_keys=True).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Manifest":
        try:
            d = json.loads(raw.decode("utf-8"))
            if d.get("format") != FORMAT:
                raise PackageError("unsupported package format %r" % d.get("format"))
            return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        except (ValueError, TypeError, KeyError) as ex:
            raise PackageError("unreadable manifest: %s" % ex) from ex

    @property
    def size(self) -> int:
        return len(self.files)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(rel: str, mid: str) -> bool:
    """Payload paths are confined to the module's package and data dir."""
    parts = rel.split("/")
    return (".." not in parts and not rel.startswith("/") and "\\" not in rel
            and len(parts) >= 2 and parts[0] in (mid, "data"))


# ------------------------------------------------------------------ what goes in
def _package_files(src_root: str, mid: str) -> List[str]:
    out = []
    top = os.path.join(src_root, mid)
    for d, dirs, names in os.walk(top):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for n in sorted(names):
            if n.endswith((".pyc", ".pyo")):
                continue
            out.append(os.path.relpath(os.path.join(d, n), src_root).replace(os.sep, "/"))
    return out


def _data_files(src_root: str, globs) -> List[str]:
    d = os.path.join(src_root, "data")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return ["data/" + n for n in names
            if os.path.isfile(os.path.join(d, n))
            and any(fnmatch.fnmatchcase(n, g) for g in globs)]


def _imports_in(nodes, cur_pkg: List[str]) -> Set[str]:
    """Package-relative module paths (``zf6hp/checksum6hp``) imported anywhere
    inside ``nodes``."""
    out = set()
    for top in nodes:
        for n in ast.walk(top):
            if isinstance(n, ast.ImportFrom) and n.level:
                base = cur_pkg[:len(cur_pkg) - (n.level - 1)]
                mod = base + (n.module.split(".") if n.module else [])
                out.add("/".join(mod))
                for a in n.names:
                    out.add("/".join(mod + [a.name]))
    return out


def checksum_closure(src_root: str, spec: "_api.ModuleSpec") -> List[str]:
    """Every file of the module's package its checksum hook can reach.

    Starts from the hook function itself (not the whole ``module.py``, whose
    other hooks would drag in the definitions) plus the classes and functions
    of ``module.py`` it names, then follows every import -- top-level and
    deferred -- through the package's own files."""
    mid = spec.id
    if not spec.checksum:
        raise PackageError("%s has no checksum hook" % mid)
    mod_ref, _, attr = spec.checksum.partition(":")
    hook_file = mod_ref.lstrip(".").replace(".", "/") + ".py"
    path = os.path.join(src_root, mid, hook_file)
    tree = ast.parse(open(path, encoding="utf-8").read())
    defs = {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    want, todo = set(), [attr]
    while todo:
        name = todo.pop()
        if name in want or name not in defs:
            continue
        want.add(name)
        todo += [n.id for n in ast.walk(defs[name]) if isinstance(n, ast.Name)]
    files = {"%s/__init__.py" % mid, "%s/%s" % (mid, hook_file)}
    pkg_of_hook = [mid] + hook_file.split("/")[:-1]
    frontier = {p for p in _imports_in([defs[n] for n in want], pkg_of_hook)}
    seen = set()
    while frontier:
        m = frontier.pop()
        if m in seen:
            continue
        seen.add(m)
        if not m.startswith(mid + "/"):
            continue                    # base-app code: always present
        for cand in (m + ".py", m + "/__init__.py"):
            p = os.path.join(src_root, cand)
            if not os.path.isfile(p):
                continue
            files.add(cand)
            parts = cand[:-3].split("/")
            # every package on the way is imported too
            for i in range(1, len(parts)):
                init = "/".join(parts[:i]) + "/__init__.py"
                if os.path.isfile(os.path.join(src_root, init)):
                    files.add(init)
                    frontier.add("/".join(parts[:i]))
            t = ast.parse(open(p, encoding="utf-8").read())
            frontier |= _imports_in([t], parts[:-1])
    return sorted(files)


def payload(src_root: str, spec: "_api.ModuleSpec", variant: str) -> List[str]:
    """Relative paths of every file the package carries."""
    if variant == _api.FULL:
        files = _package_files(src_root, spec.id) + _data_files(src_root, spec.data)
    elif variant == _api.CHECKSUM_ONLY:
        files = checksum_closure(src_root, spec) + _data_files(src_root, spec.checksum_data)
    else:
        raise PackageError("unknown variant %r" % variant)
    # an open-source module's licence text travels with both variants
    lic = _api.license_path(spec.id)
    if lic not in files and os.path.isfile(os.path.join(src_root, lic)):
        files.append(lic)
    bad = [f for f in files if not _safe(f, spec.id)]
    if bad:
        raise PackageError("files outside the module: %s" % bad)
    return files


#: The module EULA every closed-source module carries (api.EULA_TERMS).
EULA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EULA.txt")


# ------------------------------------------------------------------ build / read
def _compiled(rel: str, source: bytes) -> Tuple[str, bytes]:
    """``(relpath, bytes)`` of the byte-compiled ``rel`` for THIS interpreter,
    hash-checked (PEP 552): valid whatever the file times are after
    extraction, so an installed module -- often in a folder the user cannot
    write -- never has to be recompiled at start-up. Another interpreter
    version ignores it and compiles the source."""
    import importlib.util
    import py_compile
    import tempfile
    d, name = os.path.split(rel)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, name)
        with open(src, "wb") as fh:
            fh.write(source)
        cfile = importlib.util.cache_from_source(src)
        py_compile.compile(src, cfile=cfile, doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        with open(cfile, "rb") as fh:
            data = fh.read()
    pyc = "/".join(x for x in (d, "__pycache__", os.path.basename(cfile)) if x)
    return pyc, data


def build(spec: "_api.ModuleSpec", src_root: str, out_path: str,
          variant: str = _api.FULL, key_path: Optional[str] = None,
          app: str = "", compile_pyc: bool = True) -> Manifest:
    """Write ``out_path`` (a .ovtmod). ``src_root`` is the app's package
    directory in the source tree. Unsigned when ``key_path`` is None."""
    files = payload(src_root, spec, variant)
    blobs = {}
    for rel in files:
        with open(os.path.join(src_root, rel), "rb") as fh:
            blobs[rel] = fh.read()
    if _api.terms(spec.license) == _api.EULA_TERMS:
        with open(EULA_FILE, "rb") as fh:
            blobs[_api.eula_path(spec.id)] = fh.read()
    if compile_pyc:
        for rel in [f for f in files if f.endswith(".py")]:
            pyc, data = _compiled(rel, blobs[rel])
            blobs[pyc] = data
    files = sorted(blobs)
    caps = list(spec.capabilities) if variant == _api.FULL else [_api.CHECKSUM]
    man = Manifest(id=spec.id, name=spec.name, version=spec.version,
                   module_api=spec.module_api, variant=variant,
                   capabilities=caps, platforms=list(spec.platforms),
                   files={rel: sha256(b) for rel, b in blobs.items()},
                   license=dict(spec.license), app=app,
                   built=datetime.datetime.now().isoformat(timespec="seconds"))
    raw = man.to_bytes()
    sig = None
    if key_path:
        from . import signing
        sig = signing.sign(raw, key_path)
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(MANIFEST, raw)
        if sig is not None:
            z.writestr(SIGNATURE, sig)
        for rel in files:
            z.writestr(rel, blobs[rel])
    os.replace(tmp, out_path)
    return man


@dataclass
class Opened:
    manifest: Manifest
    manifest_bytes: bytes
    signature: Optional[bytes]
    zip: zipfile.ZipFile


def open_package(path_or_bytes) -> Opened:
    """Open and structurally check a .ovtmod (signature NOT yet checked)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(path_or_bytes)
                            if isinstance(path_or_bytes, (bytes, bytearray))
                            else path_or_bytes)
    except (zipfile.BadZipFile, OSError) as ex:
        raise PackageError("not a module package: %s" % ex) from ex
    names = set(z.namelist())
    if MANIFEST not in names:
        raise PackageError("no manifest -- not a module package")
    raw = z.read(MANIFEST)
    man = Manifest.from_bytes(raw)
    sig = z.read(SIGNATURE) if SIGNATURE in names else None
    payload_names = names - {MANIFEST, SIGNATURE}
    if payload_names != set(man.files):
        raise PackageError("package contents do not match its manifest")
    bad = [f for f in man.files if not _safe(f, man.id)]
    if bad:
        raise PackageError("files outside the module: %s" % bad[:3])
    return Opened(man, raw, sig, z)


def check_files(man: Manifest, read) -> Optional[str]:
    """None when every file ``read(relpath) -> bytes`` returns matches the
    manifest, else what is wrong."""
    for rel, want in man.files.items():
        try:
            got = sha256(read(rel))
        except (OSError, KeyError):
            return "missing %s" % rel
        if got != want:
            return "%s has been modified" % rel
    return None


def checksum_only(spec: "_api.ModuleSpec") -> "_api.ModuleSpec":
    """The spec as its checksum-only variant runs it."""
    import dataclasses
    return dataclasses.replace(
        spec, variant=_api.CHECKSUM_ONLY, capabilities=(_api.CHECKSUM,),
        build=None, after_restore=None, load_kb=None, data=spec.checksum_data)

