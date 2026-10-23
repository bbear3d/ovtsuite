# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The base app / module split (docs/MODULES_PLAN.md).

What is pinned:

* the base app names every platform from the bytes alone, with no module
  installed, and claims nothing it cannot positively recognise;
* the base app's own packages never import a module package;
* every in-tree module declares itself correctly and its hooks resolve;
* with modules hidden (``OVT_DISABLE_MODULES``) a binary still opens -- in
  the universal tools, with the reason -- and the checksum is not touched;
* a module's checksum is reached through the registry, never directly.

Fast: synthetic images, plus one subprocess per isolation case.
"""
import ast
import os
import struct
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ovtsuite import platforms                                  # noqa: E402
from ovtsuite.platforms import aisin, identify as ident_mod     # noqa: E402
from ovtsuite.modules import api, registry                      # noqa: E402


# ---------------------------------------------------------------- synthetic
@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def edc16_image():
    d = bytearray(b"\xFF" * 0x80000)
    d[0x4000:0x4008] = b"\xFA\xDE\xCA\xFE\xCA\xFE\xAF\xFE"
    return bytes(d)


def edc15_image():
    d = bytearray(b"\xFF" * 0x80000)
    d[0x10000:0x1000C] = platforms.edc15.DATASET_SIG
    return bytes(d)


def aisin_09d_image(base=0, size=0xE0000):
    d = bytearray(b"\x11" * size)
    struct.pack_into(">IIII", d, aisin.D09_DESC - base,
                     1, aisin.D09_START, aisin.D09_END, 0)
    return bytes(d)


def tr80_image():
    d = bytearray(b"\xFF" * 0x140000)
    for off, start, end in aisin.TR80_TRAILERS:
        struct.pack_into(">IIII", d, off, 1, start, end, 0x1234)
    return bytes(d)


# ---------------------------------------------------------------- identify
@pytest.mark.parametrize("data, want", [
    (edc16_image(), "edc16"),
    (edc15_image(), "edc15"),
    (aisin_09d_image(), "aisin"),
    (aisin_09d_image(base=0x2000, size=0xDE000), "aisin"),
    (tr80_image(), "aisin"),
    (b"\x00" * 0x80000, "unknown"),
    (os.urandom(0x1000), "unknown"),
], ids=lambda v: v if isinstance(v, str) else "image")
# (short ids: the default puts the whole image in the test's name, which
# pytest copies into an environment variable -- over Windows' 32 KB limit)
def test_identify_from_structure(data, want):
    got = ident_mod.identify(data)
    assert got.platform == want
    assert got.known == (want != "unknown")


def test_unknown_is_not_forced_into_edc16():
    """EDC16 used to be the fallback for anything unclaimed; now it needs its
    own descriptor (Bear, 2026-10-10)."""
    blank = b"\x5A" * 0x200000
    assert ident_mod.identify(blank).platform == "unknown"
    assert ident_mod.identify(edc16_image()).platform == "edc16"


def test_container_kind_wins_except_edc16():
    assert ident_mod.identify(b"\x00" * 64, container_kind="zf6hp").platform == "zf6hp"
    # an .edc16 may wrap any ECU image: sniffed, not trusted
    assert ident_mod.identify(edc15_image(), container_kind="edc16").platform == "edc15"
    assert ident_mod.identify(b"\x00" * 64, hvac=True).platform == "hvac"


def test_identity_names_the_module():
    i = ident_mod.identify(aisin_09d_image())
    assert i.module == "aisin" and i.module_name == "Aisin TR-xx"
    assert "TR-60SN" in i.describe()
    assert ident_mod.identify(b"").module == ""


def test_catalogue_is_consistent():
    for p in platforms.PLATFORMS.values():
        assert p.module == "" or p.module in platforms.MODULES, p
    for m in platforms.MODULES.values():
        for pid in m.platforms:
            assert platforms.PLATFORMS[pid].module == m.id


# ---------------------------------------------------------------- the seam
BASE_ONLY = ("platforms", "modules", "project", "definitions")   # never import a module
MODULE_PKGS = set(platforms.MODULES)


def _module_imports(path, pkgparts):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkgparts[:len(pkgparts) - (n.level - 1)]
                parts = base + (n.module.split(".") if n.module else [])
            elif (n.module or "").startswith("ovtsuite."):
                parts = n.module.split(".")[1:]
            else:
                continue
            names = parts[:1] if parts else [a.name for a in n.names]
            for top in names:
                if top in MODULE_PKGS:
                    yield n.lineno, top
        elif isinstance(n, ast.Import):
            for a in n.names:
                bits = a.name.split(".")
                if bits[0] == "ovtsuite" and len(bits) > 1 and bits[1] in MODULE_PKGS:
                    yield n.lineno, bits[1]


@pytest.mark.parametrize("pkg", BASE_ONLY)
def test_base_packages_never_import_a_module(pkg):
    """The registry reaches module code by name (importlib) through a spec
    hook; nothing in the base-only packages imports one statically. (The
    one sanctioned exception is speedview's guarded view lookup, which lives
    outside these packages.)"""
    root = os.path.join(ROOT, "ovtsuite", pkg)
    bad = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, os.path.join(ROOT, "ovtsuite")).replace(
                    os.sep, "/")        # (the exemption below is written with /)
                pkgparts = rel.split("/")[:-1]
                bad += ["%s:%d -> %s" % (rel, ln, top)
                        for ln, top in _module_imports(p, pkgparts)]
    # platforms/vdo_container.py looks up the VDO module's identify() inside
    # a try/except ImportError -- a guarded optional enrichment.
    bad = [b for b in bad if not b.startswith("platforms/vdo_container.py")]
    assert not bad, bad


def test_every_in_tree_module_declares_itself():
    found = registry.installed()
    assert set(found) == MODULE_PKGS, registry.problems()
    for mid, spec in found.items():
        assert spec.id == mid
        assert spec.module_api in api.SUPPORTED_API
        assert set(spec.platforms) <= set(platforms.PLATFORMS)
        for hook in ("build", "after_restore", "checksum", "load_kb", "faults"):
            if getattr(spec, hook):
                assert callable(spec.resolve(hook)), (mid, hook)
        if spec.has(api.DEFINITIONS):
            assert spec.build, mid
        if spec.has(api.CHECKSUM):
            assert spec.checksum, mid


def test_aisin_checksum_through_the_registry():
    cs = registry.checksum("aisin")
    img = aisin_09d_image()
    assert cs.is_supported(img)
    assert not cs.verify(img).ok
    fixed = cs.correct(img)
    assert cs.verify(fixed).ok
    assert len(fixed) == len(img)
    # the write payload (based at 0x2000) is identified but not corrected
    assert not cs.is_supported(aisin_09d_image(base=0x2000, size=0xDE000))


def test_missing_text_reads_right():
    assert "No OVT-Suite module" in registry.missing_text("unknown")
    assert "no definitions" in registry.missing_text("aisin")


# ---------------------------------------------------------------- isolation
def _run(code, disabled="all", tmp=None):
    env = dict(os.environ, OVT_DISABLE_MODULES=disabled,
               PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp or ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_base_runs_with_every_module_hidden(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(edc16_image())
    out = _run(r'''
import sys
from ovtsuite.modules import registry
from ovtsuite.session import TunerSession
import ovtsuite.speedview, ovtsuite.defn, ovtsuite.progress, ovtsuite.frf, ovtsuite.sgo
assert registry.installed() == {}, registry.installed()
s = TunerSession(%r)
assert s.platform == "edc16" and s.support == "none", (s.platform, s.support)
assert s.entities == [] and s.checksum_status() is None
assert s.correct_checksums() == (None, False)
assert "Install the Bosch EDC16 module" in s.unsupported_reason
s.edit_bytes(0x10, b"\x01\x02")
s.save(backup=False)
assert open(%r, "rb").read()[0x10:0x12] == b"\x01\x02"
for mid in ("edc15", "edc16", "zf6hp", "zf8hp", "vdo", "hvac", "aisin"):
    assert not any(m == "ovtsuite." + mid or m.startswith("ovtsuite.%%s." %% mid)
                   for m in sys.modules), mid
print("ok")
''' % (str(p), str(p)))
    assert out.strip() == "ok"


def test_one_module_hidden_leaves_the_rest(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(aisin_09d_image())
    out = _run(r'''
from ovtsuite.modules import registry
from ovtsuite.session import TunerSession
assert "zf6hp" not in registry.installed() and "aisin" in registry.installed()
s = TunerSession(%r)
assert s.platform == "aisin" and s.support == "checksum", s.support
assert s.checksum_status() is not None
print("ok")
''' % str(p), disabled="zf6hp")
    assert out.strip() == "ok"


def test_unknown_binary_opens(tmp_path):
    p = tmp_path / "u.bin"
    p.write_bytes(b"\x5A" * 0x1000)
    from ovtsuite.session import TunerSession
    s = TunerSession(str(p))
    assert s.platform == "unknown" and s.support == "none"
    assert s.entities == [] and s.checksum_provider() is None
    assert "No OVT-Suite module" in s.unsupported_reason
    from ovtsuite import fileinfo
    rows = dict(fileinfo.rows_for_session(s))
    assert rows["Identified as"] == "Unrecognised binary"


# ---------------------------------------------------------------- ownership
def test_every_data_file_has_at_most_one_owner():
    import fnmatch
    data = os.path.join(ROOT, "ovtsuite", "data")
    specs = registry.installed()
    for name in os.listdir(data):
        owners = [m for m, s in specs.items()
                  if any(fnmatch.fnmatchcase(name, g) for g in s.data)]
        assert len(owners) <= 1, (name, owners)


def test_hidden_module_data_is_hidden_too():
    out = _run(r'''
from ovtsuite import datafiles, kb
assert datafiles.path("zf8hp_auak14c0.json.gz") is None
assert datafiles.path("frf.key") is not None          # base data stays
assert datafiles.owner("p350.json.gz") == "edc16"
assert kb.available() == [], kb.available()            # EDC16 KBs are its data
print("ok")
''', disabled="zf8hp,edc16")
    assert out.strip() == "ok"


# Every entry point of the base app must import with any module hidden: the
# flasher, logger, sniffer and simulator are module-independent, and the main
# window only reaches module code for a session its module built.
_ENTRY = r'''
import importlib, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
names = ["ovtsuite.session", "ovtsuite.cli", "ovtsuite.speedview",
         "ovtsuite.flash.edc16", "ovtsuite.flash.edc15", "ovtsuite.flash.zf6hp_uds",
         "ovtsuite.flash.zf8hp_uds", "ovtsuite.flash.vdo_cluster",
         "ovtsuite.flash.aisin09d", "ovtsuite.flash.ecu_sim", "ovtsuite.flash.irom15",
         "ovtsuite.datalog.channels", "ovtsuite.sniff.capture", "ovtsuite.logtune",
         "ovtsuite.autotune", "ovtsuite.patch", "ovtsuite.diff", "ovtsuite.xdf_export"]
try:
    import PySide6.QtWidgets   # noqa: F401
    qt = True
except Exception:
    qt = False
if qt:
    names += ["ovtsuite.gui", "ovtsuite.flash.gui_flash", "ovtsuite.flash.gui_sim",
              "ovtsuite.datalog.gui_log", "ovtsuite.sniff.gui_sniff",
              "ovtsuite.logtune.gui_logtune"]
for n in names:
    importlib.import_module(n)
if qt:
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    w = gui.MainWindow()
    w.close_discarding()
print("ok")
'''


@pytest.mark.parametrize("hidden", sorted(MODULE_PKGS) + ["all"])
def test_base_entry_points_import_with_a_module_hidden(hidden):
    assert _run(_ENTRY, disabled=hidden).strip().endswith("ok")
