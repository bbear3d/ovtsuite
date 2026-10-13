# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Collect the licence files of what a build ships, into the staged base app.

    python tools/collect_licenses.py --out build/base/ovtsuite

Run by packaging/ovtsuite.spec right after ``build_modules.py stage-base``,
with the build environment's own interpreter, so what it copies is exactly the
Python runtime and package versions being frozen:

* the interpreter's ``LICENSE.txt`` (it also covers what CPython bundles:
  OpenSSL, libffi, zlib, bzip2, liblzma, expat, mpdecimal ...);
* every licence file the bundled distributions declare in their metadata.

Files land in ``<out>/data/licenses/bundled`` as ``<dist>__<file>.txt``
(``ovtsuite.credits`` reads them by that name). Then the whole notices
document -- the credits plus every licence text -- is written to
``<out>/data/licenses/THIRD-PARTY-NOTICES.txt``; the spec copies it beside the
executable and build_linux.sh installs it as the package's copyright file.
"""

import argparse
import os
import re
import sys
import sysconfig

#: Distributions the frozen app carries (packaging/ovtsuite.spec HIDDEN and
#: their dependencies). A missing one is reported, not fatal: the Linux build
#: may lack the Windows-only ones.
SHIPPED = ("cryptography", "cffi", "pycparser", "pyserial", "pyusb", "pyftdi",
           "pyinstaller", "shiboken6", "PySide6_Essentials")

#: Licence files that are not a grant to the user of this build (PySide6's
#: wheels carry only the Qt *commercial* licence text; Qt is used under the
#: LGPL, whose text ships in data/licenses).
SKIP = re.compile(r"commercial", re.I)

LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|authors?)([._-].*)?$", re.I)


def _python_license():
    cands = [os.path.join(sys.base_prefix, "LICENSE.txt"),
             os.path.join(sys.base_prefix, "LICENSE"),
             os.path.join(sysconfig.get_paths()["stdlib"], "LICENSE.txt"),
             "/usr/share/doc/python%d.%d/copyright" % sys.version_info[:2],
             "/usr/share/doc/python%d/copyright" % sys.version_info[0]]
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def _dist_files(name):
    from importlib import metadata
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    out = []
    for f in dist.files or ():
        base = os.path.basename(str(f))
        parts = str(f).replace("\\", "/").split("/")
        in_meta = parts[0].endswith(".dist-info")
        if not in_meta or SKIP.search(base):
            continue
        if "licenses" in parts or LICENSE_NAME.match(base):
            p = str(dist.locate_file(f))
            if os.path.isfile(p):
                out.append(p)
    return out


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def collect(out_pkg):
    dest = os.path.join(out_pkg, "data", "licenses", "bundled")
    os.makedirs(dest, exist_ok=True)
    for n in os.listdir(dest):
        os.remove(os.path.join(dest, n))
    written, missing = [], []
    p = _python_license()
    if p:
        _copy(p, os.path.join(dest, "python__LICENSE.txt"))
        written.append("python")
    else:
        missing.append("python (LICENSE.txt)")
    for name in SHIPPED:
        files = _dist_files(name)
        if files is None:
            missing.append(name)
            continue
        key = name.lower().replace("-", "_")
        seen = set()
        for f in files:
            label = os.path.basename(f)
            if label.lower() in seen:
                label = _safe(os.path.basename(os.path.dirname(f)) + "_" + label)
            seen.add(label.lower())
            _copy(f, os.path.join(dest, "%s__%s.txt" % (key, _safe(label))))
        written.append(name)
    return written, missing


def _copy(src, dst):
    with open(src, "rb") as fh:
        data = fh.read()
    with open(dst, "wb") as fh:
        fh.write(data)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True,
                    help="the staged package directory (build/base/<pkg>)")
    a = ap.parse_args(argv)
    written, missing = collect(a.out)
    # write the notices with the STAGED package's credits module
    sys.path.insert(0, os.path.dirname(os.path.abspath(a.out)))
    pkg = os.path.basename(os.path.abspath(a.out))
    import importlib
    credits = importlib.import_module(pkg + ".credits")
    path = os.path.join(a.out, "data", "licenses", "THIRD-PARTY-NOTICES.txt")
    # modules are not part of the base app's notices: each module brings its own
    credits.write_notices(path, list(credits.CREDITS))
    print("collect_licenses: %s; missing: %s; wrote %s"
          % (", ".join(written), ", ".join(missing) or "none", path))
    gaps = credits.missing_files()
    if gaps:
        print("collect_licenses: WARNING -- licence files named in credits.py "
              "are missing:\n  " + "\n  ".join(gaps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
