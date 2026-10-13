# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Who and what the app is built on: the credits and licence notices.

One Qt-free registry, read by the About dialog (Help > About) and by the build,
which writes the same text as ``THIRD-PARTY-NOTICES.txt`` beside the
executable and into the Linux package's ``copyright`` file.

Three kinds of entry:

* **Incorporated code** -- source from another project that was ported or
  copied into this one (or binary blobs taken from it). Each one's licence
  requires its copyright notice and licence text to travel with every copy.
* **Runtime components** -- libraries the frozen build ships: Python, Qt /
  PySide6, the USB and serial libraries. (FTDI's D2XX driver is not shipped:
  the Windows installers offer to download FTDI's own package.)
* **Acknowledgements** -- projects that were read or cross-checked but from
  which no code was taken. Not a licence obligation; credit where it is due.

Data a *module* brings (the VDO cluster sweep patches) is credited by the
module's own data: :func:`module_credits` reads it from wherever the module is
installed, so the credits follow what is actually installed.

Licence texts live in ``data/licenses``. The build adds the exact licence
files of the Python runtime and the bundled Python packages it froze
(``tools/collect_licenses.py`` -> ``data/licenses/bundled``), so those always
match the versions shipped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import branding

#: The copyright holder of the app itself.
AUTHOR = "Blair Campbell"
#: First year of publication (the About box shows ``first-current``).
FIRST_YEAR = 2026

LICENSE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "licenses")
#: Licence files the build collected from the frozen Python environment.
BUNDLED_DIR = os.path.join(LICENSE_DIR, "bundled")

INCORPORATED = "Incorporated code"
RUNTIME = "Runtime components"
MODULE_DATA = "Module data"
THANKS = "Acknowledgements"
GROUPS = (INCORPORATED, RUNTIME, MODULE_DATA, THANKS)


@dataclass(frozen=True)
class Credit:
    name: str
    #: what the app uses it for, in one line
    what: str
    copyright: str
    #: SPDX expression ("" for an acknowledgement that carries no obligation)
    license: str
    group: str
    url: str = ""
    #: licence text files under ``data/licenses`` (or a module's data folder)
    files: Tuple[str, ...] = ()
    #: where in the app it is used (module id, "base", or "")
    where: str = "base"
    note: str = ""
    #: directory the ``files`` live in (None = :data:`LICENSE_DIR`)
    root: Optional[str] = None
    #: Python distributions whose licence files the build collects for this
    #: entry (``"python"`` = the interpreter's own LICENSE)
    dists: Tuple[str, ...] = ()
    #: shipped texts used only when the build collected nothing for ``dists``
    fallback: Tuple[str, ...] = ()

    def file_paths(self) -> List[str]:
        """Every licence text file for this entry that applies: its own
        files, the ones the build collected for its distributions, else its
        fallback copies."""
        d = self.root or LICENSE_DIR
        out = [os.path.join(d, f) for f in self.files]
        got = [p for dist in self.dists for p in bundled_for(dist)]
        out += got or [os.path.join(LICENSE_DIR, f) for f in self.fallback]
        return out


#: Everything the base app incorporates or ships. Order is display order.
CREDITS: Tuple[Credit, ...] = (
    # -- incorporated code ---------------------------------------------------
    Credit(
        "kw1281test",
        "KW1281 / KWP2000 dialogs and diagnostics, the FTDI K-line interface, "
        "EDC15 flash, boot-mode and seed/key algorithms with their C167 "
        "loaders, and the VDO cluster unlock and key finder (ported via "
        "KWHack, the author's fork)",
        "Copyright (c) 2021 Greg Menounos and the kw1281test contributors",
        "MIT", INCORPORATED, "https://github.com/gmenounos/kw1281test",
        ("MIT-kw1281test.txt",)),
    Credit(
        "ecu-tool (via kw1281test)",
        "The EDC15 whole-chip flash loader (Edc15FlashLoader.bin, EDC15P.FLD), "
        "the EDC15 flash sequence and the EDC15 seed/key algorithm, as carried "
        "and credited by kw1281test",
        "fjvva and the ecu-tool contributors",
        "MIT", INCORPORATED, "https://github.com/fjvva/ecu-tool",
        ("MIT-kw1281test.txt",),
        note="Used under kw1281test's MIT licence, which carries this code."),
    Credit(
        "j2534 (Tactrix OpenPort 2.0 driver for Linux)",
        "The OpenPort 2.0 USB command protocol used to drive the cable "
        "without the vendor DLL (Linux, macOS)",
        "Copyright (c) 2025, NikolaKozina, dshultzca",
        "BSD-3-Clause", INCORPORATED, "https://github.com/NikolaKozina/j2534",
        ("BSD-3-Clause-j2534.txt",)),
    Credit(
        "VW_Flash",
        "The .frf container's outer decryption and its key file (frf.key)",
        "Copyright (c) 2022-2024, Brian Ledbetter and contributors",
        "BSD-2-Clause", INCORPORATED, "https://github.com/bri3d/VW_Flash",
        ("BSD-2-Clause-VW_Flash.txt",),
        note="Only VW_Flash's BSD-licensed code is used; its GPL-licensed lzss "
             "sub-directory is not."),
    Credit(
        "Lucide icons",
        "The toolbar and menu icons (a selection of the SVG files, "
        "lucide-static 0.469.0, in data/icons)",
        "Copyright (c) Cole Bemis 2013-2022 as part of Feather (MIT); "
        "Copyright (c) Lucide Contributors 2022",
        "ISC", INCORPORATED, "https://lucide.dev/",
        ("ISC-Lucide.txt",)),
    # -- runtime components ---------------------------------------------------
    Credit(
        "Python", "The language runtime the app is frozen with",
        "Copyright (c) 2001 Python Software Foundation; All Rights Reserved",
        "PSF-2.0", RUNTIME, "https://www.python.org/",
        dists=("python",), fallback=("Python.txt",),
        note="Python's own licence file also covers the libraries the "
             "runtime bundles (OpenSSL, libffi, zlib, bzip2, liblzma, expat, "
             "mpdecimal)."),
    Credit(
        "Qt 6", "The user interface toolkit",
        "Copyright (C) The Qt Company Ltd. and other contributors",
        "LGPL-3.0-only", RUNTIME, "https://www.qt.io/",
        ("Qt-third-party.txt", "LGPL-3.0.txt", "GPL-3.0.txt")),
    Credit(
        "Qt for Python (PySide6, Shiboken6)", "Python bindings for Qt",
        "Copyright (C) The Qt Company Ltd.",
        "LGPL-3.0-only", RUNTIME, "https://www.qt.io/qt-for-python",
        ("LGPL-3.0.txt", "GPL-3.0.txt")),
    Credit(
        "cryptography", "Module signatures (Ed25519) and the ZF 8HP AES codec",
        "Copyright (c) Individual contributors",
        "Apache-2.0 OR BSD-3-Clause", RUNTIME, "https://cryptography.io/",
        dists=("cryptography",), fallback=("Apache-2.0.txt",),
        note="Includes OpenSSL (Apache-2.0) and Rust crates under MIT / "
             "Apache-2.0; their notices are in the package's own licence "
             "files."),
    Credit(
        "cffi and pycparser", "cryptography's C bindings",
        "Copyright (c) Armin Rigo, Maciej Fijalkowski; Copyright (c) Eli Bendersky",
        "MIT-0 AND BSD-3-Clause", RUNTIME, "https://cffi.readthedocs.io/",
        dists=("cffi", "pycparser")),
    Credit(
        "pySerial", "Serial-port K-line cables",
        "Copyright (c) 2001-2020 Chris Liechti",
        "BSD-3-Clause", RUNTIME, "https://github.com/pyserial/pyserial",
        dists=("pyserial",), fallback=("BSD-3-Clause-pyserial.txt",)),
    Credit(
        "PyUSB", "USB access to the OpenPort 2.0 and FTDI cables",
        "Copyright 2009-2017 Wander Lairson Costa; 2009-2021 PyUSB contributors",
        "BSD-3-Clause", RUNTIME, "https://github.com/pyusb/pyusb", dists=("pyusb",)),
    Credit(
        "PyFtdi", "Direct FTDI access on Linux and macOS",
        "Copyright (c) Emmanuel Blot and contributors",
        "BSD-3-Clause", RUNTIME, "https://github.com/eblot/pyftdi", dists=("pyftdi",)),
    Credit(
        "libusb-win32", "USB back end for PyUSB on Windows (libusb0.dll)",
        "Copyright (c) Stephan Meyer, Travis Robinson and contributors",
        "LGPL-3.0-or-later", RUNTIME, "https://sourceforge.net/projects/libusb-win32/",
        ("libusb-win32.txt", "LGPL-3.0.txt", "GPL-3.0.txt")),
    Credit(
        "PyInstaller bootloader", "Starts the frozen program",
        "Copyright (c) 2010-2023, PyInstaller Development Team",
        "GPL-2.0-or-later WITH Bootloader-exception", RUNTIME,
        "https://pyinstaller.org/", dists=("pyinstaller",),
        note="The bootloader exception allows distributing programs built "
             "with it under any licence."),
    # -- acknowledgements (no code taken) ------------------------------------
    Credit(
        "sa2_seed_key",
        "Cross-checked against while implementing VW's SA2 seed/key bytecode",
        "Copyright (c) 2022 Brian Ledbetter (bri3d)", "MIT", THANKS,
        "https://github.com/bri3d/sa2_seed_key", ("MIT-sa2_seed_key.txt",), where=""),
    Credit(
        "UnlockECU",
        "Cross-checked against while implementing VW's SA2 seed/key bytecode",
        "Copyright (c) 2020 JinGen Lim (jglim)", "MIT", THANKS,
        "https://github.com/jglim/UnlockECU", ("MIT-UnlockECU.txt",), where=""),
)


def copyright_line() -> str:
    import datetime
    year = datetime.date.today().year
    span = str(FIRST_YEAR) if year <= FIRST_YEAR else "%d-%d" % (FIRST_YEAR, year)
    return "Copyright © %s %s." % (span, AUTHOR)


#: The base app's own licence (SPDX) and its text under ``data/licenses``.
#: Modules carry their own: a closed module its EULA.txt, an open one its
#: LICENSE.txt, in the module's folder.
APP_LICENSE = "MPL-2.0"
APP_LICENSE_FILE = "MPL-2.0.txt"


def license_line() -> str:
    """One line for the About box and the notices: the licence and, once
    published, where the source is."""
    line = "Free software under the Mozilla Public License 2.0."
    if branding.SOURCE_URL:
        line += " Source code: %s" % branding.SOURCE_URL
    return line


TRADEMARKS = (
    "Not affiliated with, endorsed by or associated with Volkswagen AG, AUDI AG, "
    "Robert Bosch GmbH, ZF Friedrichshafen AG, Aisin Corporation, Continental "
    "(VDO), Tactrix, FTDI or Ross-Tech. Their names, trademarks and part numbers "
    "are used only to describe compatibility and belong to their owners.")

LEGAL_USE = (
    "Modifying an engine or transmission controller can damage the vehicle and "
    "may affect its warranty, insurance, emissions compliance and "
    "roadworthiness. Removing, disabling or defeating emissions controls on a "
    "vehicle used on public roads is illegal in many countries, including under "
    "the US Clean Air Act and EU and UK type-approval law. Use this software "
    "only where and how the law allows; you are responsible for that.")

WARRANTY = "This software is provided as is, without warranty of any kind."

DISCLAIMER = "\n\n".join((TRADEMARKS, LEGAL_USE, WARRANTY))


# ----------------------------------------------------------------- modules
#: Licence label for work published freely with no licence stated: credited to
#: the site it was published on.
NO_LICENSE = "None stated"


def _site(url: str) -> str:
    """``https://www.example.com/a/b`` -> ``example.com``."""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _sweep_credits(path: str, where: str) -> List[Credit]:
    """One credit per author and licence of the VDO sweep patches."""
    try:
        with open(path, encoding="utf-8") as fh:
            patches = json.load(fh).get("patches", [])
    except (OSError, ValueError):
        return []
    root = os.path.dirname(path)
    groups: Dict[tuple, list] = {}
    for p in patches:
        groups.setdefault((p.get("author", "?"), p.get("license", "")), []).append(p)
    out = []
    for (author, lic), ps in groups.items():
        first = ps[0]
        roms = sorted({p.get("rom_version", "") for p in ps if p.get("rom_version")})
        sources = []
        for p in ps:
            if p.get("source") and p["source"] not in sources:
                sources.append(p["source"])
        unlicensed = not lic or lic.lower() == "none stated"
        spdx = NO_LICENSE if unlicensed else lic
        note = first.get("credit", "")
        if unlicensed:
            site = _site(sources[0]) if sources else ""
            note = (note + " Published freely without a licence or warranty%s; "
                    "credited to its source." % (" on " + site if site else "")).strip()
        if len(sources) > 1:
            note = (note + " Sources: " + ", ".join(sources[1:]) + ".").strip()
        lf = first.get("license_file", "")
        out.append(Credit(
            "%s (cluster patch)" % author,
            "Needle / LED sweep or DSG-needle patch for %s" % (", ".join(roms) or "a VDO ROM"),
            first.get("copyright", ""), spdx, MODULE_DATA, sources[0] if sources else "",
            (lf,) if lf else (), where=where, note=note, root=root))
    return out


def module_credits() -> List[Credit]:
    """Credits carried by installed modules' data (empty when none is).

    A module credits third-party data by shipping it with its own attribution
    fields -- today the VDO module's ``cluster_sweep_patches.json`` -- or a
    ``*_credits.json`` file holding a list of objects with :class:`Credit`'s
    field names. Read from wherever the owning module is installed."""
    try:
        from . import datafiles
    except Exception:          # pragma: no cover - a broken install
        return []
    out: List[Credit] = []
    p = datafiles.path("cluster_sweep_patches.json")
    if p:
        out += _sweep_credits(p, datafiles.owner("cluster_sweep_patches.json") or "base")
    for d in datafiles.dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith("_credits.json"):
                continue
            try:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    rows = json.load(fh)
            except (OSError, ValueError):
                continue
            for r in rows if isinstance(rows, list) else []:
                try:
                    out.append(Credit(
                        r["name"], r.get("what", ""), r.get("copyright", ""),
                        r.get("license", ""), r.get("group", MODULE_DATA),
                        r.get("url", ""), tuple(r.get("files", ())),
                        where=datafiles.owner(n) or "base", note=r.get("note", ""),
                        root=d))
                except (KeyError, TypeError):
                    continue
    return out


def all_credits() -> List[Credit]:
    return list(CREDITS) + module_credits()


# ---------------------------------------------------------------- licences
#: separates the distribution name from the file name in a collected file
SEP = "__"


def bundled_licenses() -> List[Tuple[str, str]]:
    """``(dist, path)`` of every licence file the build collected from the
    frozen environment, named ``<dist>__<file>.txt`` (empty when running
    from source)."""
    out = []
    if os.path.isdir(BUNDLED_DIR):
        for n in sorted(os.listdir(BUNDLED_DIR)):
            p = os.path.join(BUNDLED_DIR, n)
            if os.path.isfile(p) and SEP in n:
                out.append((n.split(SEP, 1)[0], p))
    return out


def bundled_for(dist: str) -> List[str]:
    key = dist.lower().replace("-", "_")
    return [p for d, p in bundled_licenses() if d.lower().replace("-", "_") == key]


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def license_text(credit: Credit) -> str:
    """The licence text(s) for one credit, joined; a pointer when none ships."""
    parts = []
    for p in credit.file_paths():
        t = read_text(p)
        if t:
            parts.append(t.rstrip())
    if not parts:
        if not credit.license:
            return "No licence obligation: credited as an acknowledgement."
        if credit.license == NO_LICENSE:
            return ("Published freely by its author without a stated licence or "
                    "warranty, and credited here to its source%s."
                    % (": " + credit.url if credit.url else ""))
        return ("Licensed under %s. The licence text is included with the "
                "package in the program's installation folder%s."
                % (credit.license, " and at " + credit.url if credit.url else ""))
    return ("\n\n" + "-" * 72 + "\n\n").join(parts)


def missing_files(credits=None) -> List[str]:
    """Shipped licence files a credit names that are not present (for the
    tests; files the build collects are not checked here)."""
    out = []
    for c in credits if credits is not None else CREDITS:
        d = c.root or LICENSE_DIR
        out += [os.path.join(d, f) for f in c.files
                if not os.path.isfile(os.path.join(d, f))]
        out += [os.path.join(LICENSE_DIR, f) for f in c.fallback
                if not os.path.isfile(os.path.join(LICENSE_DIR, f))]
    return out


def notices_text(credits=None, with_texts: bool = True) -> str:
    """The whole THIRD-PARTY-NOTICES document."""
    credits = credits if credits is not None else all_credits()
    rule = "=" * 78
    lines = ["%s (%s) %s" % (branding.APP_NAME, branding.APP_LONG, branding.__version__),
             copyright_line(), license_line(), "",
             "This program includes or is built with the third-party components "
             "listed below. Each is used under its own licence, reproduced or "
             "referenced here; those terms govern those components.", ""]
    for g in GROUPS:
        group = [c for c in credits if c.group == g]
        if not group:
            continue
        lines += [rule, g.upper(), rule, ""]
        for c in group:
            lines.append(c.name)
            lines.append("  " + c.what)
            if c.copyright:
                lines.append("  " + c.copyright)
            lines.append("  Licence: %s" % (c.license or "none (acknowledgement)"))
            if c.url:
                lines.append("  " + c.url)
            if c.note:
                lines.append("  " + c.note)
            lines.append("")
    lines += [rule, "DISCLAIMER", rule, "", DISCLAIMER, ""]
    if with_texts:
        seen = set()
        lines += [rule, "LICENCE TEXTS", rule, ""]
        own = os.path.join(LICENSE_DIR, APP_LICENSE_FILE)
        if os.path.isfile(own):
            lines += ["-" * 78, "%s licence (%s)" % (branding.APP_NAME, APP_LICENSE),
                      "-" * 78, "", read_text(own).rstrip(), ""]
        for c in credits:
            for p in c.file_paths():
                key = (os.path.basename(p), read_text(p)[:200])
                if key in seen or not os.path.isfile(p):
                    continue
                seen.add(key)
                lines += ["-" * 78, os.path.basename(p), "-" * 78, "",
                          read_text(p).rstrip(), ""]
        claimed = {os.path.normcase(os.path.abspath(p))
                   for c in credits for p in c.file_paths()}
        for _dist, p in bundled_licenses():
            if os.path.normcase(os.path.abspath(p)) in claimed:
                continue
            lines += ["-" * 78, os.path.basename(p), "-" * 78, "",
                      read_text(p).rstrip(), ""]
    return "\n".join(lines) + "\n"


def write_notices(path: str, credits=None) -> str:
    text = notices_text(credits)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path
