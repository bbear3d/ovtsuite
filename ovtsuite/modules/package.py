# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.ovtmod`` package: one module, ready to install.

A zip holding::

    manifest.json   what the module is, and the SHA-256 of every file below
    signature       Ed25519 over manifest.json's exact bytes (see .signing)
    <id>/...        the module's package: Python source (an open-source
                    module), or compiled code (a closed one, see below)
    data/...        the data files it owns

laid out exactly like the app's own package directory, so an installed module
(extracted to ``<modules dir>/<id>/``) is found by the normal import machinery
and its code finds its data next to itself. Two variants per module:

* ``full`` -- the whole package and every data file the module owns;
* ``checksum`` -- only what its checksum hook needs (the import closure of
  that hook, computed here from the source, never listed by hand), for users
  who flash but do not edit.

Both share one id: installing one replaces the other.

A closed-source module (under the module EULA) ships no Python source: its
code is compiled to native extension modules (:mod:`.native`, Cython), and
what cannot be compiled goes as byte-code only (``<name>.pyc`` where the
source would be). Both are tied to one CPython version and the extension
modules to one OS and machine, so such a package is tagged -- the manifest's
``python`` (``cp313``) and ``platform`` (``win_amd64``), and its file name
``<id>-<ver>[-checksum]-cp313-win_amd64.ovtmod`` -- and an app that runs
another refuses it (:func:`incompatible`). An open-source module's package
carries its source (plus byte-code for this interpreter) and no tags: it
runs anywhere.
"""

import ast
import datetime
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import sysconfig
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
    #: the interpreter and the OS/machine the package's code is compiled for
    #: (``cp313``, ``win_amd64``); "" = any (source: an open-source module).
    #: Not ``platforms``, which are the ECU platforms the module serves.
    python: str = ""
    platform: str = ""

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


# ------------------------------------------------------------------ tags
def python_tag() -> str:
    """``cp313``: the interpreter (implementation, major and minor version)
    compiled module code is tied to."""
    impl = {"cpython": "cp"}.get(sys.implementation.name, sys.implementation.name)
    return "%s%d%d" % (impl, sys.version_info[0], sys.version_info[1])


def platform_tag() -> str:
    """``win_amd64``, ``linux_x86_64``, ``macosx_arm64``: the OS and machine
    an extension module runs on (wheel-style, without the macOS version)."""
    plat = sysconfig.get_platform()
    if plat.startswith("macosx-"):
        plat = "macosx-" + plat.rsplit("-", 1)[-1]
    return plat.replace("-", "_").replace(".", "_")


_OS_WORDS = {"win_amd64": "Windows (64-bit)", "win32": "Windows (32-bit)",
             "win_arm64": "Windows (ARM)", "linux_x86_64": "Linux (x86-64)",
             "linux_aarch64": "Linux (ARM64)", "macosx_arm64": "macOS (Apple silicon)",
             "macosx_x86_64": "macOS (Intel)"}


def tag_words(py: str, plat: str) -> str:
    """``cp313``, ``win_amd64`` -> "Python 3.13 on Windows (64-bit)"."""
    m = re.match(r"cp(\d)(\d+)$", py or "")
    pyw = "Python %s.%s" % m.groups() if m else (py or "any Python")
    return "%s on %s" % (pyw, _OS_WORDS.get(plat, plat)) if plat else pyw


def incompatible(man: "Manifest") -> str:
    """"" when this interpreter can run ``man``'s code, else why not, in
    words for the user."""
    if (man.python and man.python != python_tag()) \
            or (man.platform and man.platform != platform_tag()):
        return ("this %s module package is built for %s, but this app runs %s "
                "-- install the package built for it (its file name ends "
                "-%s-%s%s)" % (man.name, tag_words(man.python, man.platform),
                               tag_words(python_tag(), platform_tag()),
                               python_tag(), platform_tag(), EXT))
    return ""


def is_native(spec: "_api.ModuleSpec") -> bool:
    """Whether ``spec``'s packages ship compiled code instead of source: a
    closed-source module (under the module EULA)."""
    return _api.terms(spec.license) == _api.EULA_TERMS


#: ``<id>-<version>[-checksum][-<python>-<platform>].ovtmod``
NAME_RX = re.compile(r"^([a-z0-9_]+)-(\d+(?:\.\d+)*)(-checksum)?"
                     r"(?:-([a-z]+\d+)-([a-z0-9_]+))?\.ovtmod$")


def file_name(mid: str, version: str, variant: str, py: str = "", plat: str = "") -> str:
    return "%s-%s%s%s%s" % (mid, version,
                            "-checksum" if variant == _api.CHECKSUM_ONLY else "",
                            "-%s-%s" % (py, plat) if py and plat else "", EXT)


def parse_name(name: str):
    """``(id, version, variant, python, platform)`` of a package's file name
    (the tags "" when untagged), or None when it is not one."""
    m = NAME_RX.match(name)
    if not m:
        return None
    return (m.group(1), m.group(2), _api.CHECKSUM_ONLY if m.group(3) else _api.FULL,
            m.group(4) or "", m.group(5) or "")


def name_fits(name: str) -> bool:
    """Whether a package called ``name`` can run here: untagged, or tagged
    for this interpreter and platform."""
    p = parse_name(name)
    return p is not None and (not p[3] or (p[3], p[4]) == (python_tag(), platform_tag()))


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


def _sourceless(rel: str, source: bytes) -> Tuple[str, bytes]:
    """``(relpath, bytes)`` of ``rel`` as byte-code that replaces its source:
    ``<dir>/<name>.pyc`` (what CPython's sourceless loader reads), naming
    the source file in tracebacks. Unchecked-hash based, so the bytes depend
    on the source alone."""
    import py_compile
    import tempfile
    from .. import branding
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, os.path.basename(rel))
        with open(src, "wb") as fh:
            fh.write(source)
        cfile = os.path.join(tmp, "out.pyc")
        py_compile.compile(src, cfile=cfile, dfile="%s/%s" % (branding.PKG, rel),
                           doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        with open(cfile, "rb") as fh:
            return rel[:-3] + ".pyc", fh.read()


def _native(blobs: Dict[str, bytes], cache_dir: Optional[str],
            report: Optional[dict]) -> None:
    """Replace every ``.py`` in ``blobs`` with its compiled form, in place."""
    from .. import branding
    from . import native
    srcs = {rel: b for rel, b in blobs.items() if rel.endswith(".py")}
    compiled, fallback = native.compile_sources(srcs, branding.PKG, cache_dir=cache_dir)
    for rel in srcs:
        del blobs[rel]
    blobs.update(compiled)
    for rel in fallback:
        pyc, data = _sourceless(rel, srcs[rel])
        blobs[pyc] = data
    if report is not None:
        report.update(fallback)


def build(spec: "_api.ModuleSpec", src_root: str, out_path: str,
          variant: str = _api.FULL, key_path: Optional[str] = None,
          app: str = "", compile_pyc: bool = True,
          native: Optional[bool] = None, cache_dir: Optional[str] = None,
          report: Optional[dict] = None) -> Manifest:
    """Write ``out_path`` (a .ovtmod). ``src_root`` is the app's package
    directory in the source tree. Unsigned when ``key_path`` is None.

    ``native`` (default: :func:`is_native`) compiles the code and ships no
    source; ``report`` (a dict) then receives ``{relpath: reason}`` for each
    file that went as byte-code instead, and ``cache_dir`` keeps compiled
    files for the next build (:func:`.native.compile_sources`). Otherwise
    the source ships, with its byte-code too when ``compile_pyc``."""
    if native is None:
        native = is_native(spec)
    files = payload(src_root, spec, variant)
    blobs = {}
    for rel in files:
        with open(os.path.join(src_root, rel), "rb") as fh:
            blobs[rel] = fh.read()
    if _api.terms(spec.license) == _api.EULA_TERMS:
        with open(EULA_FILE, "rb") as fh:
            blobs[_api.eula_path(spec.id)] = fh.read()
    if native:
        _native(blobs, cache_dir, report)
    elif compile_pyc:
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
                   built=datetime.datetime.now().isoformat(timespec="seconds"),
                   python=python_tag() if native else "",
                   platform=platform_tag() if native else "")
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

