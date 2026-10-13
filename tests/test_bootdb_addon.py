# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The EDC16 boot-sector database is an add-on: never in the app, a module or
an installer payload, found wherever it was put (ovtsuite.bootdb)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import bootdb, branding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = branding.ENV_PREFIX + "_BOOTDB"


def test_not_in_the_package():
    assert not os.path.exists(os.path.join(ROOT, branding.PKG, "data", bootdb.DB_NAME))


def test_found_through_the_environment(tmp_path, monkeypatch):
    src = os.path.join(ROOT, "addons", bootdb.DB_NAME)
    if not os.path.isfile(src):
        import pytest
        pytest.skip("no boot DB in addons/")
    dst = tmp_path / bootdb.DB_NAME
    shutil.copyfile(src, dst)
    monkeypatch.setenv(ENV, str(dst))
    monkeypatch.setattr(bootdb, "_cache", None)
    assert bootdb.path() == str(dst) and bootdb.available()
    assert bootdb.load().parts()


def test_missing_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "nowhere.bin"))
    monkeypatch.setattr(bootdb, "search_dirs", lambda: [str(tmp_path)])
    assert bootdb.path() is None and not bootdb.available()
    assert bootdb.DB_NAME in bootdb.missing_text()


def test_the_installer_only_downloads_it():
    with open(os.path.join(ROOT, "packaging", "addon_bootdb.iss"), encoding="utf-8") as fh:
        iss = fh.read()
    assert '{tmp}\\edc16_bootdb.bin' in iss and "external" in iss
    assert "#ifdef BootDbUrl" in iss
