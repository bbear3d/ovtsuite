# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Compiling a closed module's code for its package: no Python source ships.

Every ``.py`` file of a closed-source module becomes a native extension
module (Cython; ``.pyd`` on Windows, ``.so`` elsewhere) beside where the
source would be, so the normal import machinery loads it from the mounted
install folder exactly as it loaded the source. A file Cython cannot turn
into one goes as byte-code only (``<name>.pyc`` in place of the source --
what CPython's sourceless loader reads), with the reason, so the build can
say which files are less protected:

* a package's ``__init__.py`` -- always byte-code: an extension module as a
  package's ``__init__`` needs the package name in its init function and
  path handling Cython only half supports, and an ``__init__`` holds the
  package docstring and re-exports, nothing worth protecting;
* any file Cython (or the C compiler) rejects -- byte-code, with the error.

Both forms are tied to one CPython version (and the extension modules to
one OS and machine), so the package says which in its manifest
(:func:`.package.python_tag`, :func:`.package.platform_tag`) and the
installer refuses a package built for another.

Compiled code keeps Python semantics: annotations are NOT used as C types
(``annotation_typing=False``), so an ``x: int`` still accepts a bool or a
numpy integer exactly as the source did; docstrings stay (the AI tools and
help read them); tracebacks name the module's ``.py`` file and line.

Build-time only (``ovtsuite.modules.package.build``); never imported by the
running app, and kept out of the frozen one (``BUILD_ONLY`` in
``packaging/ovtsuite.spec``: setuptools and Cython drag in the excluded
distutils).
"""

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
from typing import Dict, Optional, Tuple

#: What the per-file results say for a file kept as byte-code on purpose.
INIT_REASON = "package __init__: kept as byte-code"


def ext_suffix() -> str:
    return sysconfig.get_config_var("EXT_SUFFIX") or (".pyd" if os.name == "nt" else ".so")


def available() -> str:
    """"" when this interpreter can compile modules, else why not."""
    try:
        import Cython                                    # noqa: F401
    except ImportError:
        return ("Cython is not installed in %s (pip install cython) -- closed "
                "modules are compiled to native code" % sys.executable)
    try:
        import setuptools                                # noqa: F401
    except ImportError:
        return "setuptools is not installed in %s (pip install setuptools)" % sys.executable
    return ""


# Runs in a fresh interpreter (setuptools patches distutils and the
# compiler environment process-wide): cythonize each file on its own -- a
# file Cython rejects is reported, not fatal -- then compile the C of all of
# them at once (parallel), falling back to one at a time to find a file the
# C compiler rejects.
_DRIVER = r'''
import io, json, os, sys
root, pkg, jobs, listing, out = sys.argv[1:6]
jobs = int(jobs)
os.chdir(root)
from setuptools import Distribution, Extension
from Cython.Build import cythonize
from Cython.Compiler import Options
Options.docstrings = True
Options.c_line_in_traceback = False
DIRECTIVES = {"language_level": 3, "binding": True, "annotation_typing": False,
              "embedsignature": False, "emit_code_comments": False}
link = ["-s"] if sys.platform.startswith("linux") else []
rels = json.load(open(listing))
failed, exts = {}, {}
for rel in rels:
    name = ".".join([pkg] + rel[:-3].split("/"))
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        exts[rel] = cythonize([Extension(name, [pkg + "/" + rel], extra_link_args=link)],
                              compiler_directives=DIRECTIVES, quiet=True,
                              force=True)[0]
    except BaseException as ex:          # CompileError, or SystemExit from Cython
        # Cython prints the error ("file.py:58:15: local variable ...")
        said = [ln for ln in sys.stderr.getvalue().splitlines()
                if ln.startswith(pkg + "/" + rel + ":")]
        failed[rel] = "Cython: %s" % (said[0].split(":", 3)[-1].strip() if said
                                      else str(ex).strip() or type(ex).__name__)
        if said:
            failed[rel] += " (line %s)" % said[0].split(":")[1]
    finally:
        sys.stderr = err

# A Python built with gcc (uv's, python.org's) names gcc-only flags in its
# CFLAGS / LDSHARED; where cc is an older clang (clang 10 on Ubuntu 20.04)
# every file then fails with "unknown argument". Drop them for clang.
GCC_ONLY = ("-fstack-clash-protection", "-mno-omit-leaf-frame-pointer")
if os.name != "nt":
    import subprocess, sysconfig
    from distutils import sysconfig as dsc
    cc = (os.environ.get("CC") or sysconfig.get_config_var("CC") or "cc").split()[0]
    try:
        clang = "clang" in subprocess.run([cc, "--version"], capture_output=True,
                                          text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        clang = False
    if clang:
        for cv in (sysconfig.get_config_vars(), dsc.get_config_vars()):
            for k, v in list(cv.items()):
                if isinstance(v, str) and any(f in v for f in GCC_ONLY):
                    cv[k] = " ".join(w for w in v.split() if w not in GCC_ONLY)

def build(lst):
    d = Distribution({"ext_modules": lst, "script_name": "build", "script_args": []})
    cmd = d.get_command_obj("build_ext")
    cmd.inplace = True
    cmd.parallel = jobs
    cmd.ensure_finalized()
    cmd.run()

try:
    build(list(exts.values()))
except BaseException:
    for rel, ext in list(exts.items()):
        try:
            build([ext])
        except BaseException as ex:
            failed[rel] = "C compiler: %s" % (str(ex).strip().splitlines() or [type(ex).__name__])[-1]
json.dump(failed, open(out, "w"))
'''


def _cache_key(pkg: str, rel: str, data: bytes) -> str:
    import Cython
    h = hashlib.sha256(("native/1|%s|%s|%s|%s|%s\0" % (
        Cython.__version__, sys.version, sysconfig.get_platform(), pkg, rel)).encode())
    h.update(_DRIVER.encode())
    h.update(data)
    return h.hexdigest()


def compile_sources(sources: Dict[str, bytes], pkg: str, jobs: int = 0,
                    timeout: int = 3600, cache_dir: Optional[str] = None
                    ) -> Tuple[Dict[str, bytes], Dict[str, str]]:
    """Compile module files to extension modules.

    ``sources`` maps package-relative paths (``edc16/fsw_dpf.py``) to their
    bytes; every package on the way must be among them (its
    ``__init__.py``), as in a module's payload. ``pkg`` is the app package
    (the compiled modules are named ``<pkg>.<id>....``).

    Returns ``(compiled, fallback)``: ``{"edc16/fsw_dpf<EXT_SUFFIX>": bytes}``
    and ``{relpath: reason}`` for every source NOT compiled (the caller
    ships those as byte-code). Raises RuntimeError when compiling cannot run
    at all (no Cython, no C compiler for any file).

    ``cache_dir`` keeps each compiled file by the hash of its source (and
    the compiler, interpreter and platform), so a file compiled for one
    package -- the full variant -- is not compiled again for another (the
    checksum-only variant) or for an unchanged file in the next build."""
    why = available()
    if why:
        raise RuntimeError(why)
    want = sorted(r for r in sources if r.endswith(".py")
                  and os.path.basename(r) != "__init__.py")
    fallback = {r: INIT_REASON for r in sources
                if r.endswith(".py") and os.path.basename(r) == "__init__.py"}
    suffix = ext_suffix()
    compiled = {}
    keys = {}
    if cache_dir:
        for rel in list(want):
            keys[rel] = _cache_key(pkg, rel, sources[rel])
            p = os.path.join(cache_dir, keys[rel] + suffix)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    compiled[rel[:-3] + suffix] = fh.read()
                want.remove(rel)
    if not want:
        return compiled, fallback
    with tempfile.TemporaryDirectory(prefix="ovtnative-") as tmp:
        top = os.path.join(tmp, pkg)
        os.makedirs(top)
        # the app package itself: Cython derives a module's full name from the
        # __init__.py files above it
        with open(os.path.join(top, "__init__.py"), "wb"):
            pass
        for rel, data in sources.items():
            if not rel.endswith(".py"):
                continue
            p = os.path.join(top, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
        listing = os.path.join(tmp, "files.json")
        result = os.path.join(tmp, "failed.json")
        with open(listing, "w") as fh:
            json.dump(want, fh)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run(
            [sys.executable, "-c", _DRIVER, tmp, pkg, str(jobs or os.cpu_count() or 1),
             listing, result],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(result):
            raise RuntimeError("compiling failed:\n%s" % (r.stdout + r.stderr)[-4000:])
        with open(result) as fh:
            failed = json.load(fh)
        for rel in want:
            out = os.path.join(top, *rel.split("/"))[:-3] + suffix
            if rel in failed:
                fallback[rel] = failed[rel]
            elif os.path.isfile(out):
                with open(out, "rb") as fh:
                    compiled[rel[:-3] + suffix] = data = fh.read()
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                    tmp_p = os.path.join(cache_dir, keys[rel] + ".tmp")
                    with open(tmp_p, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp_p, os.path.join(cache_dir, keys[rel] + suffix))
            else:
                fallback[rel] = "no extension module produced"
        if not compiled:
            raise RuntimeError("no file could be compiled -- is a C compiler "
                               "installed?\n%s" % "\n".join(
                                   "  %s: %s" % kv for kv in sorted(fallback.items())))
    return compiled, fallback
