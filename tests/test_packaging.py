# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The build's module split: incremental decisions, installer inputs, Qt trim.

Fast and offline -- nothing is frozen here (the build scripts do that and
smoke-test the result). What is pinned:

* a change to one module's file re-fingerprints that module only; a change to
  a base file re-fingerprints the base only;
* the base-only staging never carries a module's file;
* the generated Inno includes list every module installed, as components;
* the installer scripts keep the module folder out of the base payload and
  take the generated includes;
* the Qt trim drops the big unused libraries and none the app needs.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite import branding                                # noqa: E402
from ovtsuite.modules import api, build                      # noqa: E402

SRC = os.path.join(ROOT, branding.PKG)
TOOL = os.path.join(ROOT, "tools", "build_modules.py")


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A private copy of the package source to modify."""
    dst = str(tmp_path_factory.mktemp("src") / branding.PKG)
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _touch(path, text="\n# changed\n"):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def test_a_module_change_rebuilds_that_module_only(tree):
    before = build.fingerprint(tree)
    _touch(os.path.join(tree, "zf8hp", "labels8hp.py"))
    after = build.fingerprint(tree)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"zf8hp"}, changed        # its checksum variant does not use it


def test_a_checksum_change_rebuilds_both_variants(tree):
    before = build.fingerprint(tree)
    _touch(os.path.join(tree, "zf8hp", "checksum8hp.py"))
    after = build.fingerprint(tree)
    assert {k for k in before if before[k] != after[k]} == {"zf8hp", "zf8hp-checksum"}


def test_a_base_change_refreezes_the_base_only(tree):
    before = build.fingerprint(tree)
    _touch(os.path.join(tree, "hexview.py") if os.path.exists(
        os.path.join(tree, "hexview.py")) else os.path.join(tree, "recent.py"))
    after = build.fingerprint(tree)
    assert {k for k in before if before[k] != after[k]} == {"base"}


def test_module_data_belongs_to_its_module(tree):
    before = build.fingerprint(tree)
    _touch(os.path.join(tree, "data", "vdo_vwk501.json"), " ")
    after = build.fingerprint(tree)
    # the VDO checksum variant carries the ROM definitions too
    assert {k for k in before if before[k] != after[k]} == {"vdo", "vdo-checksum"}


def test_staged_base_has_no_module_file(tmp_path):
    out = str(tmp_path / branding.PKG)
    copied = build.stage_base(SRC, out)
    owners = build.ownership(SRC)
    assert copied and all(owners[r] == "" for r in copied)
    assert not any(r.split("/")[0] in ("zf8hp", "edc16", "vdo") for r in copied)


def test_staging_updates_in_place_and_leaves_pycache(tmp_path):
    """Re-staging never deletes the tree (Windows keeps freshly written
    byte-code locked): stale files go, changed files are replaced,
    ``__pycache__`` stays."""
    out = tmp_path / branding.PKG
    build.stage_base(SRC, str(out))
    cache = out / "autotune" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "x.cpython-313.pyc").write_bytes(b"pyc")
    (out / "zf8hp").mkdir()
    (out / "zf8hp" / "stray.py").write_text("module file")          # not the base's
    (out / "recent.py").write_text("outdated")                       # a changed file
    build.stage_base(SRC, str(out))
    assert (cache / "x.cpython-313.pyc").exists()
    assert not (out / "zf8hp").exists()
    assert (out / "recent.py").read_bytes() == open(os.path.join(SRC, "recent.py"), "rb").read()


def test_plan_reports_json(tmp_path):
    r = subprocess.run([sys.executable, TOOL, "plan", "--json", "--out",
                        str(tmp_path / "pk"), "--base", str(tmp_path / "base")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    assert plan["base"] == "changed"
    assert plan["packages"]["zf8hp"] == "stale"
    assert "zf8hp-checksum" in plan["packages"]


def test_inno_includes_list_installed_modules(tmp_path):
    from ovtsuite.modules import installer
    pk = str(tmp_path / "pk")
    for v in (api.FULL, api.CHECKSUM_ONLY):
        build.pack(SRC, pk, "aisin", v, None, branding.APP_NAME)
    full, cks = str(tmp_path / "full"), str(tmp_path / "cks")
    installer.install(os.path.join(pk, build.package_name(build._specs(SRC)["aisin"], api.FULL)),
                      scope=full, allow_unsigned=True)
    installer.install(os.path.join(pk, build.package_name(build._specs(SRC)["aisin"],
                                                          api.CHECKSUM_ONLY)),
                      scope=cks, allow_unsigned=True)
    out = str(tmp_path / "inno")
    r = subprocess.run([sys.executable, TOOL, "inno", "--full-dir", full,
                        "--checksum-dir", cks, "--out", out],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    inc = open(os.path.join(out, "modules_full.iss")).read()
    assert "[Types]" in inc and 'Name: "mod_aisin"' in inc and "Flags: fixed" in inc
    assert 'DestDir: "{app}\\modules\\aisin"' in inc
    inc = open(os.path.join(out, "modules_checksum.iss")).read()
    assert "[Types]" not in inc and "checksum correction only" in inc
    rows = json.load(open(os.path.join(out, "modules.json")))
    assert {(r["id"], r["variant"]) for r in rows} == {("aisin", "full"), ("aisin", "checksum")}


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_installers_keep_modules_out_of_the_base_payload():
    text = _read("packaging", "installer.iss")
    assert re.search(r'^Source: "\{#SourceDir\}\\\*"; Excludes: "\\modules"', text, re.M)
    tools = _read("packaging", "tool_installer.iss")
    assert '#define Excl "\\modules"' in tools
    for text in (text, tools):
        assert "#include ModulesInclude" in text
    mod = _read("packaging", "module_installer.iss")
    assert "AppId={#AppName}.Module.{#ModuleId}" in mod
    assert "function InitializeSetup" in mod          # refuses without the base app


def test_the_qt_trim_drops_the_unused_and_keeps_the_needed():
    spec = _read("packaging", "ovtsuite.spec")
    drop = re.search(r"QT_DROP_FILES = \((.*?)\n\)", spec, re.S).group(1)
    for gone in ("opengl32sw", "qt6quick", "qt6qml", "qt6pdf", "qt6virtualkeyboard"):
        assert '"%s' % gone in drop, gone
    for kept in ("qt6core", "qt6gui", "qt6widgets", "qt6svg", "qt6opengl.", "qt6dbus",
                 "qt6network"):
        assert '"%s' % kept not in drop, kept
    # one window per user (gui_instance) needs QtNetwork: a bundle without
    # it did not open at all
    excl = re.search(r"EXCLUDES = \[(.*?)\n\]", spec, re.S).group(1)
    assert '"PySide6.QtNetwork"' not in excl


#: Inno Setup preprocessor directives (ISPP). A line whose first non-blank
#: character is "#" is read as one of these -- "#13#10 + '...'" continuing a
#: [Code] string broke the module installer ("Unknown preprocessor directive").
ISPP = {"define", "undef", "ifdef", "ifndef", "ifexist", "ifnexist", "if", "elif",
        "else", "endif", "include", "error", "pragma", "emit", "expr", "insert",
        "append", "file", "sub", "endsub", "for", "dim", "redim", "preproc"}


def test_no_iss_line_starts_with_a_non_directive_hash():
    pk = os.path.join(ROOT, "packaging")
    bad = []
    for name in sorted(os.listdir(pk)):
        if not name.endswith(".iss"):
            continue
        for n, line in enumerate(open(os.path.join(pk, name), encoding="utf-8"), 1):
            m = re.match(r"\s*#\s*([A-Za-z]*)", line)
            if m and m.group(1).lower() not in ISPP:
                bad.append("%s:%d: %s" % (name, n, line.strip()))
    assert not bad, "\n".join(bad)


def test_installer_key_ignores_times_and_sees_content(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import build_modules as B
    d = tmp_path / "payload"
    d.mkdir()
    (d / "a.txt").write_text("one")
    k1 = B.installer_key([str(d)], texts=["1.0.0"])
    os.utime(d / "a.txt", (1, 1))                       # touched, same bytes
    assert B.installer_key([str(d)], texts=["1.0.0"]) == k1
    assert B.installer_key([str(d)], texts=["1.0.1"]) != k1
    (d / "a.txt").write_text("two")
    assert B.installer_key([str(d)], texts=["1.0.0"]) != k1


def test_the_build_skips_unchanged_installers():
    ps = _read("build.ps1")
    assert "installer-key" in ps
    assert "$RebuildInstallers" in ps and "up to date - its inputs have not changed" in ps
    sh = _read("build_linux.sh")
    assert "build_deb" in sh and "--rebuild-packages" in sh
