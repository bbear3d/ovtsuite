# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The product and package names live in one module, and stay there.

OVT-Suite has been renamed once (``edc16tuner`` -> ``ovtsuite``) and is expected
to be renamed again, so ``tools/rename_package.py`` has to be able to do the job
mechanically. That only holds while no other module puts a name into a value the
program uses. These tests fail the moment one does.

Prose is deliberately allowed: comments and docstrings naming the product read
better than an f-string, and the rename tool rewrites them along with everything
else. ``docs/`` is exempt entirely -- it is a historical record and should keep
saying what it said at the time.
"""

import ast
import os
import re
import warnings

import pytest

from ovtsuite import branding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, branding.PKG)

#: Names that must not reach a runtime value. The old ones are listed too: a
#: half-finished rename is exactly what this test exists to catch.
FORBIDDEN = re.compile(
    r"edc16tuner|EDC16Tuner|EDC16TUNER|EDC16-Tuner|OVT-Suite|OVTSuite|OVTSUITE")
STALE = re.compile(r"edc16tuner|EDC16Tuner|EDC16TUNER|EDC16-Tuner")

SKIP_DIRS = {"__pycache__", "data", "drivers"}
#: branding.py defines the names; the other two name them on purpose.
EXEMPT = {"branding.py", "rename_package.py", "test_branding.py"}


def _without_legacy(text, path):
    """``text`` with any declared legacy-names region removed.

    A file is allowed to name an OLD product where naming it is the point -- an
    installer removing the previous install, a config fallback reading the
    previous location. Such a region says so in the file, and both this test and
    tools/rename_package.py honour it. An unbalanced region would silently
    exempt the rest of a file, so it is an error rather than a free pass.
    """
    out, inside = [], False
    for line in text.splitlines(True):
        if branding.LEGACY_BEGIN in line:
            assert not inside, "%s: a legacy-names region opens twice" % path
            inside = True
            continue
        if branding.LEGACY_END in line:
            assert inside, "%s: a legacy-names region ends without beginning" % path
            inside = False
            continue
        if not inside:
            out.append(line)
    assert not inside, (
        "%s: a legacy-names region is never closed, so the rest of the file "
        "would go unchecked" % path)
    return "".join(out)


def _sources(root):
    """Importable ``.py`` files under ``root``. A file whose stem is not a
    valid identifier (``gui-1.py``) can never be imported, so it is dead weight
    rather than code, and is not held to this rule."""
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f.endswith(".py") and f not in EXEMPT and f[:-3].isidentifier():
                yield os.path.join(dp, f)


def _name_literals(path):
    """Forbidden names reaching a value the program uses, docstrings excluded.

    A name in a window title, a QSettings scope, a config path or a file header
    is what a rename silently misses; a name in prose is not.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            hits += FORBIDDEN.findall(node.value)
    return sorted(set(hits))


def test_package_directory_matches_branding():
    assert os.path.isdir(PKG_DIR), "branding.PKG must name this package's directory"


def test_no_hardcoded_names_in_the_package():
    offenders = {os.path.relpath(p, ROOT): h
                 for p in _sources(PKG_DIR) for h in [_name_literals(p)] if h}
    assert not offenders, (
        "these modules put a product or package name into a value the program "
        "uses; import `from . import branding` and use branding.APP_NAME / PKG "
        "/ QSETTINGS / CONFIG_DIR instead:\n%s" % offenders)


def test_no_stale_names_in_tools_and_tests():
    """tools and tests may name the package -- they import it -- but must not
    carry the OLD names, which would mean a half-finished rename."""
    offenders = {}
    for base in ("tools", "tests"):
        for path in _sources(os.path.join(ROOT, base)):
            hits = STALE.findall(
                _without_legacy(open(path, encoding="utf-8").read(), path))
            if hits:
                offenders[os.path.relpath(path, ROOT)] = sorted(set(hits))
    assert not offenders, "stale names left behind by a rename:\n%s" % offenders


def test_no_stale_names_in_build_and_packaging():
    offenders = {}
    # README.md is exempt: it documents the rename on purpose, old name and all.
    files = [os.path.join(ROOT, f) for f in
             ("pyproject.toml", "build.py", "build.ps1", "build_linux.sh")]
    pkg = os.path.join(ROOT, "packaging")
    files += [os.path.join(pkg, f) for f in os.listdir(pkg)
              if f.endswith((".spec", ".iss", ".txt"))]
    for path in files:
        if not os.path.exists(path):
            continue
        hits = STALE.findall(_without_legacy(
            open(path, encoding="utf-8", errors="replace").read(), path))
        if hits:
            offenders[os.path.relpath(path, ROOT)] = sorted(set(hits))
    assert not offenders, "build/packaging still references the old name:\n%s" % offenders


def _rename_tool():
    """``tools/rename_package.py`` loaded from its path -- ``tools`` is not a
    package, and the tool deliberately does not import the package it renames."""
    import importlib.util

    path = os.path.join(ROOT, "tools", "rename_package.py")
    spec = importlib.util.spec_from_file_location("rename_package", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_rename_tool_leaves_a_legacy_region_alone():
    """The other half of the exemption, and the half that actually bites.

    A test that merely ignores these names would let the rename tool rewrite
    them: the installer would then delete the Start Menu group it had just
    created, under the new name, with nothing to catch it.
    """
    tool = _rename_tool()
    text = (
        "AppName=Old-Name\n"
        "%s\n"
        "DelTree('{userprograms}\\Old-Name')\n"
        "%s\n"
        "UninstallDisplayName=Old-Name\n" % (branding.LEGACY_BEGIN, branding.LEGACY_END))
    out = tool._substitute(text, [("Old-Name", "New-Name")],
                           branding.LEGACY_BEGIN, branding.LEGACY_END, "sample.iss")

    assert "AppName=New-Name" in out, "outside the region the rename still happens"
    assert "UninstallDisplayName=New-Name" in out, "on both sides of it"
    assert "DelTree('{userprograms}\\Old-Name')" in out, (
        "the old name inside the region is what the cleanup needs")
    assert "New-Name')" not in out


def test_an_unclosed_legacy_region_is_an_error_not_a_free_pass():
    tool = _rename_tool()
    text = "AppName=Old-Name\n%s\nDelTree('Old-Name')\n" % branding.LEGACY_BEGIN
    with pytest.raises(SystemExit):
        tool._substitute(text, [("Old-Name", "New-Name")],
                         branding.LEGACY_BEGIN, branding.LEGACY_END, "sample.iss")

    with pytest.raises(AssertionError):
        _without_legacy(text, "sample.iss")


def test_the_marked_files_declare_balanced_regions():
    """Named rather than discovered: a file that loses its markers should fail
    here, not quietly start being scanned (or quietly stop)."""
    marked = [os.path.join(ROOT, branding.PKG, "branding.py"),
              os.path.join(ROOT, "packaging", "installer.iss")]
    for path in marked:
        text = open(path, encoding="utf-8", errors="replace").read()
        assert branding.LEGACY_BEGIN in text, "%s lost its legacy marker" % path
        _without_legacy(text, path)


ISS = os.path.join(ROOT, "packaging", "installer.iss")


def test_a_rename_records_the_release_it_leaves_behind():
    """The legacy region is necessary but not sufficient.

    Protected from rewriting and otherwise left alone, the past-names list keeps naming
    the FIRST release forever. Rename twice and the settings written under the middle
    name are orphaned -- silently: the app comes up with defaults. So each rename
    prepends the release it is leaving, newest first, which is the order ``recent`` and
    ``appsettings`` read them in.
    """
    tool = _rename_tool()
    text = (
        "%s\n"
        "LEGACY_RELEASES = (\n"
        "    (\"EDC16-Tuner\", \"edc16tuner\", \"EDC16Tuner\", \"EDC16-Tuner\"),\n"
        ")\n"
        "%s\n"
        "APP_NAME = \"OVT-Suite\"\n" % (branding.LEGACY_BEGIN, branding.LEGACY_END))
    old = dict(PKG="ovtsuite", APP_NAME="OVT-Suite", EXE_NAME="OVTSuite", ORG="OVT-Suite")

    out = tool.legacy_extended(text, old, branding.LEGACY_BEGIN, branding.LEGACY_END)
    releases = _releases(out)

    assert releases == [("OVT-Suite", "ovtsuite", "OVTSuite", "OVT-Suite"),
                        ("EDC16-Tuner", "edc16tuner", "EDC16Tuner", "EDC16-Tuner")], releases
    assert "APP_NAME = \"OVT-Suite\"" in out, "outside the region nothing is touched"

    again = tool.legacy_extended(out, old, branding.LEGACY_BEGIN, branding.LEGACY_END)
    assert _releases(again) == releases, "running it twice must not double a release"


def test_the_region_is_found_by_what_it_holds_not_by_being_first():
    """branding.py's first begin/end pair is the two lines that DEFINE the markers.

    Taking that one found an empty region, changed nothing, and reported success -- a
    rename that silently dropped half its job. This is that file's real shape.
    """
    tool = _rename_tool()
    text = (
        "LEGACY_BEGIN = \"%s\"\n"
        "LEGACY_END = \"%s\"\n"
        "# %s\n"
        "LEGACY_RELEASES = (\n"
        "    (\"EDC16-Tuner\", \"edc16tuner\", \"EDC16Tuner\", \"EDC16-Tuner\"),\n"
        ")\n"
        "# %s\n" % (branding.LEGACY_BEGIN, branding.LEGACY_END,
                     branding.LEGACY_BEGIN, branding.LEGACY_END))
    old = dict(PKG="ovtsuite", APP_NAME="OVT-Suite", EXE_NAME="OVTSuite", ORG="OVT-Suite")

    out = tool.legacy_extended(text, old, branding.LEGACY_BEGIN, branding.LEGACY_END)
    assert len(_releases(out)) == 2, out


def test_a_rename_also_teaches_the_installer():
    """The installer cannot import branding, so the tool writes the list into it too."""
    tool = _rename_tool()
    before = open(ISS, encoding="utf-8").read()
    old = dict(PKG="ovtsuite", APP_NAME="OVT-Suite", EXE_NAME="OVTSuite", ORG="OVT-Suite")

    after = tool.installer_extended(before, old, branding.LEGACY_BEGIN, branding.LEGACY_END)
    names = tool._iss_cases(after, "LegacyAppName")
    exes = tool._iss_cases(after, "LegacyExeName")

    assert names[0] == "OVT-Suite" and exes[0] == "OVTSuite", (names, exes)
    assert names[1:] == tool._iss_cases(before, "LegacyAppName"), "the older ones stay, in order"
    assert _iss_count(after) == len(names), "the count the Pascal loops use is updated"

    again = tool.installer_extended(after, old, branding.LEGACY_BEGIN, branding.LEGACY_END)
    assert tool._iss_cases(again, "LegacyAppName") == names, "and it is idempotent"


def test_the_installer_and_branding_agree_on_the_past():
    """The drift guard, and the only one available.

    ``installer.iss`` is Inno Setup script -- it cannot import the package, so the list
    of past releases exists twice. A rename writes both; this fails if anything ever
    writes only one.
    """
    tool = _rename_tool()
    text = open(ISS, encoding="utf-8").read()
    names = tool._iss_cases(text, "LegacyAppName")
    exes = tool._iss_cases(text, "LegacyExeName")

    assert names == [r[0] for r in branding.LEGACY_RELEASES], names
    assert exes == [r[2] for r in branding.LEGACY_RELEASES], exes
    assert _iss_count(text) == len(branding.LEGACY_RELEASES), "the loop bound matches the list"


#: Inno Setup calls these itself; nothing in the script refers to them.
ISS_EVENTS = {"InitializeSetup", "InitializeWizard", "CurPageChanged", "CurStepChanged",
              "NextButtonClick", "BackButtonClick", "ShouldSkipPage", "CurUninstallStepChanged",
              "InitializeUninstall", "DeinitializeSetup", "UpdateReadyMemo"}


def test_every_routine_in_the_installer_is_called():
    """No Inno Setup here, so a dead routine is the one thing a test can still catch.

    It matters more than tidiness: an accessor sitting unused next to the migration code
    invites someone to "finish" the job by calling it, and in this script the thing that
    looks unfinished -- redirecting the Start Menu group -- is the failure its own
    comments record having already been caused once.
    """
    text = open(ISS, encoding="utf-8").read()
    code = text[text.index("[Code]"):]
    defined = re.findall(r"^(?:function|procedure)\s+(\w+)", code, re.M)
    assert defined, "the installer has no [Code] routines"

    for name in defined:
        if name in ISS_EVENTS:
            continue
        calls = len(re.findall(r"\b%s\s*\(" % name, code))
        assert calls > 1, "%s is defined in installer.iss and never called" % name


def test_the_derived_fallbacks_follow_the_release_list():
    """LEGACY_CONFIG_DIRS and LEGACY_QSETTINGS are derived, not maintained by hand."""
    assert branding.LEGACY_CONFIG_DIRS == tuple(
        n for app, pkg, _exe, _org in branding.LEGACY_RELEASES for n in (app, pkg))
    assert branding.LEGACY_QSETTINGS == tuple(
        (org, app) for app, _pkg, _exe, org in branding.LEGACY_RELEASES)


def _releases(source):
    """The ``LEGACY_RELEASES`` literal in ``source``."""
    import ast

    m = re.search(r"LEGACY_RELEASES = \((.*?)\n\)", source, re.S)
    assert m, "LEGACY_RELEASES is not assigned in:\n%s" % source
    return list(ast.literal_eval("(" + m.group(1) + ")"))


def _iss_count(text):
    m = re.search(r"function LegacyReleaseCount\(\): Integer;\s*begin\s*Result := (\d+)", text)
    assert m, "LegacyReleaseCount is not in the installer"
    return int(m.group(1))


def test_the_fallbacks_are_read_newest_first():
    """The order the rename writes them in is the order they are consumed in."""
    from ovtsuite import appsettings, recent

    assert [os.path.basename(p) for p in recent._legacy_dirs()] \
        == list(branding.LEGACY_CONFIG_DIRS)
    assert appsettings._legacy_scopes() == [tuple(s) for s in branding.LEGACY_QSETTINGS]


def test_version_has_one_source():
    import ovtsuite
    assert ovtsuite.__version__ is branding.__version__


def test_exe_name_is_filename_safe():
    assert re.fullmatch(r"[A-Za-z0-9_]+", branding.EXE_NAME), branding.EXE_NAME
    assert re.fullmatch(r"[A-Z0-9_]+", branding.ENV_PREFIX), branding.ENV_PREFIX


def test_legacy_settings_are_read_only_fallbacks():
    """The old scopes must stay listed, or users silently lose their
    recent-file lists and saved preferences on upgrade."""
    assert ("EDC16-Tuner", "EDC16-Tuner") in branding.LEGACY_QSETTINGS
    assert "EDC16-Tuner" in branding.LEGACY_CONFIG_DIRS
    assert branding.CONFIG_DIR not in branding.LEGACY_CONFIG_DIRS


def test_title_helpers():
    assert branding.title() == branding.APP_NAME
    assert branding.title("x.bin") == "%s — x.bin" % branding.APP_NAME
    assert branding.tagged("x.bin") == "x.bin (%s)" % branding.APP_NAME


# ---------------------------------------------------------------------------
# Carrying user data across the rename
# ---------------------------------------------------------------------------

def test_recent_lists_read_forward_from_the_old_config_dir(tmp_path, monkeypatch):
    """A rename must not empty the recent-files menu: reads fall back to the
    previous name's directory, writes always land in the current one."""
    from ovtsuite import recent

    monkeypatch.setattr(recent, "_base_dir", lambda: str(tmp_path))
    old = tmp_path / branding.LEGACY_CONFIG_DIRS[0]
    old.mkdir()
    target = tmp_path / "some.bin"
    target.write_bytes(b"\x00")
    (old / "recent.json").write_text('["%s"]' % target.as_posix(), encoding="utf-8")

    assert recent.load_recent() == [os.path.abspath(str(target))]

    other = tmp_path / "other.bin"
    other.write_bytes(b"\x00")
    recent.add_recent(str(other))
    cur = tmp_path / (branding.CONFIG_DIR if os.name == "nt" else branding.PKG)
    assert (cur / "recent.json").exists(), "the write must land in the CURRENT dir"
    assert not (old / "recent.json").read_text(encoding="utf-8").count("other.bin"), \
        "the legacy copy must never be written to"


def test_settings_read_forward_from_the_old_config_dir(tmp_path, monkeypatch):
    from ovtsuite import recent

    monkeypatch.setattr(recent, "_base_dir", lambda: str(tmp_path))
    old = tmp_path / branding.LEGACY_CONFIG_DIRS[0]
    old.mkdir()
    (old / "settings.json").write_text('{"last_dir": "/tmp"}', encoding="utf-8")
    assert recent.get_setting("last_dir") == "/tmp"


def test_qsettings_migration_is_declared_and_one_shot():
    """The Qt-side carry-over exists, reads the legacy scopes, and never
    writes to them."""
    from ovtsuite import appsettings

    src = open(os.path.join(ROOT, branding.PKG, "appsettings.py"), encoding="utf-8").read()
    assert "LEGACY_QSETTINGS" in src
    assert appsettings._legacy_scopes() == [tuple(s) for s in branding.LEGACY_QSETTINGS]
    # the only setValue calls target `cur`, never `old`
    assert "old.setValue" not in src


def test_ovt_projects_are_associated_and_open_in_the_gui():
    """The installer's default-on association is the .ovt project alone; the
    older per-platform files are no longer associated, and an update removes
    the associations earlier versions made for every one of them. Every open
    command goes through the ``gui`` verb -- a bare ``"%1"`` is parsed as an
    unknown subcommand."""
    from ovtsuite import container
    from ovtsuite.flash import backup15
    from ovtsuite.project import archive
    text = open(ISS, encoding="utf-8").read()
    pid = "%s%s" % (branding.EXE_NAME, archive.EXT)
    assert 'Subkey: "Software\\Classes\\%s"; \\' % archive.EXT in text
    assert 'ValueData: "%s"' % pid in text
    task = re.search(r'Name: "associate_ovt";(?:[^\n]*\\\n)*[^\n]*', text).group(0)
    assert "GroupDescription" in task and "unchecked" not in task   # on by default
    ovt = [l for l in text.splitlines() if "Tasks: associate_ovt" in l]
    assert ovt and len(ovt) == 6                        # the .ovt keys only
    legacy = [f.ext for f in container.FORMATS.values()] + [backup15.EXT]
    for ext in legacy:
        assert 'Subkey: "Software\\Classes\\%s"' % ext not in text, ext
    body = re.search(r"function OldAssocExts\(\).*?\nend;", text, re.S).group(0)
    removed = re.findall(r"Result\[\d+\] := '([^']+)';", body)
    assert "SetArrayLength(Result, %d)" % len(removed) in body
    assert sorted("." + e for e in removed) == sorted(legacy)
    assert "RemoveOldAssociations();" in text
    for cmd in re.findall(r'ValueData: """\{app\}\\\{#AppExeName\}""([^;]*);', text):
        # a module package installs instead (cli install-module --gui)
        assert cmd.strip().startswith(('gui ', 'install-module --gui ')), cmd


TOOLS_ISS = os.path.join(ROOT, "packaging", "tools.iss")
TOOL_ISS = os.path.join(ROOT, "packaging", "tool_installer.iss")


def _iss_defines(path):
    return dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"', open(path, encoding="utf-8").read(),
                           re.M))


@pytest.mark.parametrize("tool, app_id", [
    ("flasher", branding.FLASHER_APP_ID),
    ("sniffer", branding.SNIFFER_APP_ID),
    ("logger", branding.LOGGER_APP_ID),
])
def test_each_tool_is_the_same_in_the_package_and_the_installers(tool, app_id):
    """Each standalone tool's launcher exe, display name and Windows
    AppUserModelID are defined twice -- ovtsuite.branding for the build and the
    process, packaging/tools.iss for both installers -- and must agree: a
    shortcut whose id differs from its process's falls back to the main app's
    taskbar button, and one pointing at an exe the build did not emit is dead.
    The installers cannot import the package, so this test holds them
    together."""
    cap = tool.capitalize()
    d = _iss_defines(TOOLS_ISS)
    assert d[cap + "Exe"] == branding.TOOL_EXES[tool] + ".exe"
    assert d[cap + "Name"] == branding.TOOL_NAMES[tool]
    assert d[cap + "AppId"] == app_id
    main = open(ISS, encoding="utf-8").read()
    assert '#include "tools.iss"' in main
    assert 'Filename: "{app}\\{#%sExe}"' % cap in main
    assert 'AppUserModelID: "{#%sAppId}"' % cap in main
    tools = open(TOOL_ISS, encoding="utf-8").read()
    assert '#include "tools.iss"' in tools
    assert '#if Tool == "%s"' % tool in tools or '#elif Tool == "%s"' % tool in tools
    assert 'Filename: "{app}\\{#%sExe}"' % cap in tools
    # the launcher's icon: embedded in the exe (packaging/) and the window's (data/)
    ico = "%s_%s.ico" % (branding.PKG, tool)
    for d in ("packaging", os.path.join(branding.PKG, "data")):
        assert os.path.exists(os.path.join(ROOT, d, ico)), d


def test_tool_installers_leave_the_main_app_out():
    text = open(TOOL_ISS, encoding="utf-8").read()
    assert '#define Excl "\\" + MainExe' in text
    assert 'Excludes: "{#Excl}"' in text
    assert _iss_defines(TOOL_ISS)["MainExe"] == branding.EXE_NAME + ".exe"
    # four products, four distinct, fixed AppIds -- and none is the main app's
    guids = re.findall(r'#define ProductGuid "([0-9A-F-]{36})"', text)
    main_id = re.search(r"AppId=\{\{([0-9A-F-]{36})\}", open(ISS, encoding="utf-8").read()).group(1)
    assert len(guids) == 4 and len(set(guids)) == 4 and main_id not in guids
