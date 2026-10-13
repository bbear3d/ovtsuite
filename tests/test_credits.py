# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The credits and licence notices (ovtsuite.credits) -- Qt-free.

Every incorporated project and shipped runtime component must carry its
licence text with every copy of the app; these tests fail when a credit names
a licence file that is not in the tree, or when one of the projects the code
says it was ported from has no credit.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import branding, credits as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_named_licence_file_ships():
    assert C.missing_files() == []


def test_every_credit_is_complete():
    for c in C.CREDITS:
        assert c.group in C.GROUPS, c.name
        assert c.name and c.what and c.copyright, c.name
        if c.group != C.THANKS:
            assert c.license, "%s has no licence" % c.name
            assert c.file_paths() or c.dists, "%s ships no licence text" % c.name


def test_ported_projects_are_credited():
    """Projects the source says it ported code from, by the name it uses."""
    names = " ".join(c.name for c in C.CREDITS)
    for project in ("kw1281test", "ecu-tool", "j2534", "VW_Flash"):
        assert project in names


def test_license_text_is_the_real_text():
    by = {c.name.split()[0]: c for c in C.CREDITS}
    assert "Greg Menounos" in C.license_text(by["kw1281test"])
    assert "Permission is hereby granted" in C.license_text(by["kw1281test"])
    assert "NikolaKozina" in C.license_text(by["j2534"])
    assert "Brian Ledbetter" in C.license_text(by["VW_Flash"])
    qt = C.license_text(by["Qt"])
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in qt
    assert "GNU GENERAL PUBLIC LICENSE" in qt            # LGPLv3 needs the GPLv3 text too


def test_notices_document():
    text = C.notices_text(list(C.CREDITS))
    assert text.startswith(branding.APP_NAME)
    assert C.AUTHOR in text
    for c in C.CREDITS:
        assert c.name in text
    assert "LICENCE TEXTS" in text and "DISCLAIMER" in text
    # each licence text once, however many credits share it
    assert text.count("GNU LESSER GENERAL PUBLIC LICENSE\n") == 1


def test_bundled_licences_are_read_by_distribution(tmp_path, monkeypatch):
    d = tmp_path / "bundled"
    d.mkdir()
    (d / "pyusb__LICENSE.txt").write_text("Copyright 2009 Wander Lairson Costa")
    (d / "python__LICENSE.txt").write_text("PSF LICENSE AGREEMENT FOR PYTHON")
    monkeypatch.setattr(C, "BUNDLED_DIR", str(d))
    by = {c.name: c for c in C.CREDITS}
    assert "Wander Lairson Costa" in C.license_text(by["PyUSB"])
    py = C.license_text(by["Python"])
    assert "PSF LICENSE AGREEMENT" in py and len(by["Python"].file_paths()) == 1


def test_module_credits_follow_the_installed_sweep_patches():
    from ovtsuite import datafiles
    if not datafiles.path("cluster_sweep_patches.json"):
        import pytest
        pytest.skip("the VDO module is not installed")
    owner = datafiles.owner("cluster_sweep_patches.json")
    mod = [c for c in C.module_credits() if c.where == owner]
    assert mod and all(c.group == C.MODULE_DATA for c in mod)
    authors = " ".join(c.name for c in mod)
    for who in ("gmenounos", "Feche", "hayperek", "SigurdMyhre"):
        assert who in authors
    with open(datafiles.path("cluster_sweep_patches.json"), encoding="utf-8") as fh:
        n = {(p.get("author"), p.get("license")) for p in json.load(fh)["patches"]}
    assert len(mod) == len(n)          # one credit per author and licence
    for c in mod:
        assert C.missing_files([c]) == [], c.name


def test_collect_licenses_writes_bundled_and_notices(tmp_path):
    import shutil
    import subprocess
    pkg = tmp_path / branding.PKG
    shutil.copytree(os.path.join(ROOT, branding.PKG), pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.json.gz"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "collect_licenses.py"),
                        "--out", str(pkg)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lic = pkg / "data" / "licenses"
    notices = (lic / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "kw1281test" in notices
    assert any(n.startswith("python__") for n in os.listdir(lic / "bundled")) or \
        "missing: python" in r.stdout
    # nothing granted to Qt *commercial* licensees is presented as our licence
    assert not [n for n in os.listdir(lic / "bundled") if re.search("commercial", n, re.I)]


def test_unlicensed_patches_are_credited_to_their_site():
    import tempfile
    rows = {"patches": [{"name": "sweep", "rom_version": "X 01.00", "author": "Someone",
                         "license": "None stated", "license_file": "",
                         "copyright": "Someone", "credit": "By Someone.",
                         "source": "https://www.example.com/sweep.html"}]}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cluster_sweep_patches.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        (c,) = C._sweep_credits(p, "vdo")
    assert c.license == C.NO_LICENSE
    assert "example.com" in c.note
    assert "example.com/sweep.html" in C.license_text(c)
