# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared fixtures. Chiefly: where the ZF 6HP reference material lives.

Seven test modules each worked this out for themselves, with slightly
different spellings of the same path. One definition now, in
``Reference/ZF6HP/`` beside the EDC15/EDC16 binaries, and still overridable
with ``ZF6HP_REFERENCE``. The old location -- the VAG-6HP-Tuner tree next door
-- is used as a fallback so a checkout that predates the copy still runs.
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A new project asks for its vehicle details in a modal dialog; the suite
# runs unattended, so it is off unless a test turns it on (gui_project).
os.environ.setdefault("OVT_NO_INFO_PROMPT", "1")

# The GUI tests run on Qt's offscreen platform, which ships no fonts: every
# character is drawn as a wide placeholder box, so text measured ~twice its
# real width and the layout tests "found" a 1418 px window that is 782 px
# with real fonts. Give it the system's fonts (before any QApplication).
if os.name == "nt" and os.environ.get("WINDIR"):
    os.environ.setdefault("QT_QPA_FONTDIR",
                          os.path.join(os.environ["WINDIR"], "Fonts"))

def _zf_reference() -> str:
    override = os.environ.get("ZF6HP_REFERENCE")
    if override:
        return os.path.abspath(override)
    here = os.path.join(ROOT, "Reference", "ZF6HP")
    if os.path.isdir(here):
        return here
    return os.path.abspath(
        os.path.join(os.path.dirname(ROOT), "VAG-6HP-Tuner", "Reference"))


#: The ZF 6HP binaries and definitions.
ZF6HP_REFERENCE = _zf_reference()

#: The EDC15/EDC16 binaries, which do live here.
BINARIES = os.path.join(ROOT, "Reference", "Binaries")


def zf6hp_path(name: str) -> str:
    """Absolute path to a ZF reference file, skipping the test if absent."""
    path = os.path.join(ZF6HP_REFERENCE, name)
    if not os.path.exists(path):
        pytest.skip("no ZF 6HP reference material (%s)" % ZF6HP_REFERENCE)
    return path


@pytest.fixture(scope="session")
def zf6hp_reference():
    if not os.path.isdir(ZF6HP_REFERENCE):
        pytest.skip("no ZF 6HP reference material (%s)" % ZF6HP_REFERENCE)
    return ZF6HP_REFERENCE


# --- the app's settings, kept out of the real ones -------------------------
#
# ``QSettings(org, app)`` -- how the app opens its settings everywhere -- is
# always the native store, the registry on Windows: ``QSettings.setPath`` and
# ``setDefaultFormat`` do not reach it. A test that ticks a box or picks a
# transport therefore changed the user's own settings (the logger's first tab
# opened on CAN after a test run), and the next test started from whatever
# the last one left. This fixture sends every settings object a test makes,
# except one opened on an explicit file, to an INI file of the test's own.

#
# A GUI test module turns it on for all its tests with
#
#     @pytest.fixture(autouse=True)
#     def _own_settings(isolated_qsettings):
#         pass
#
# and a module-scoped window fixture asks for ``isolated_qsettings_module``.

def _redirect_qsettings(mp, path):
    """Send ``QSettings(...)`` -- all but ``QSettings(file, format)`` -- to
    the INI file ``path`` (nothing without PySide6). The app's own files in
    the config folder -- the recent-projects and comparison lists, the last
    folders, AI settings, crash recovery -- go beside it too
    (``recent._config_dir``, which they all read it through): a test that
    opens a project must not put it in the user's File > Open Recent.
    (``APPDATA`` itself is left alone: Python finds the user's own
    site-packages through it, which a test's subprocess needs.)"""
    cfg = os.path.join(os.path.dirname(path), "config")
    os.makedirs(cfg, exist_ok=True)
    from ovtsuite import recent
    mp.setattr(recent, "_config_dir", lambda: cfg)
    mp.setattr(recent, "_legacy_dirs", lambda: [])
    try:
        from PySide6 import QtCore
    except ImportError:
        return None
    real = QtCore.QSettings
    while getattr(real, "_ovt_isolated", False):
        real = real.__mro__[1]                  # nested: wrap the real class

    class _Isolated(real):
        _ovt_isolated = True

        def __init__(self, *a, **k):
            if len(a) >= 2 and isinstance(a[0], str) and \
                    isinstance(a[1], real.Format):
                super().__init__(*a, **k)           # QSettings(file, format)
            else:
                super().__init__(path, real.Format.IniFormat)
    mp.setattr(QtCore, "QSettings", _Isolated)
    return path


@pytest.fixture
def isolated_qsettings(tmp_path, monkeypatch):
    return _redirect_qsettings(monkeypatch, str(tmp_path / "qsettings.ini"))


@pytest.fixture(scope="module")
def isolated_qsettings_module(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    path = _redirect_qsettings(
        mp, str(tmp_path_factory.mktemp("qsettings") / "qsettings.ini"))
    yield path
    mp.undo()


# --- fast by default, everything on request --------------------------------
#
# The suite reached fifteen minutes, which is long enough that it stopped
# being run and started being skipped -- the worst of both. So it is split,
# and the split is measured rather than guessed: `pytest --durations` was run
# once over the whole suite and the modules that dominate it are marked.
#
# What makes a test slow here is not the assertion, it is the fixture. A
# `TunerSession` on a 1 MB image matches ~2,500 definition entries against it
# and then solves regions; a dozen of those is ten minutes, and the module
# that does it forty times is most of the suite by itself. Tests that exercise
# a function rather than a whole load are all fast, and they are the ones a
# change usually breaks first.
#
#     pytest                 the fast half: seconds, run this constantly
#     pytest --slow          everything
#     pytest -m slow         only the expensive ones
#
# A skipped slow test says so with its reason, rather than being deselected
# and silently vanishing from the count -- the point is to know what was not
# run.

SLOW_HELP = "run the slow tests too (whole-image sessions, GUI, solver)"


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", default=False,
                     help=SLOW_HELP)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: builds whole sessions or drives the GUI; minutes "
                   "rather than seconds. Run with --slow.")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow") or config.getoption("-m") == "slow":
        return
    skip = pytest.mark.skip(reason="slow: run with --slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
