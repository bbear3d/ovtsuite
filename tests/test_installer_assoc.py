# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The installer's opt-in .ols association (packaging/installer.iss)."""
import os
import re

ISS = os.path.join(os.path.dirname(__file__), "..", "packaging", "installer.iss")


def _text():
    with open(ISS, encoding="utf-8") as fh:
        return fh.read()


def _logical_lines(text):
    """Inno lines with their ``\\`` continuations joined."""
    return re.sub(r"\\\r?\n\s*", " ", text).splitlines()


def test_the_ols_association_is_a_task_that_is_off_by_default():
    task = next(l for l in _logical_lines(_text()) if l.startswith('Name: "associate_ols"'))
    assert "unchecked" in task
    assert "File associations:" in task and ".ols" in task


def test_the_ols_entries_belong_to_the_task_and_are_removed_on_uninstall():
    lines = [l for l in _logical_lines(_text()) if "OVTSuite.ols" in l or r"Classes\.ols" in l]
    assert len(lines) == 4
    for l in lines:
        assert "Tasks: associate_ols" in l, l
        assert "uninsdelete" in l, l
    # the project's own key is removed whole; the .ols extension only gets an
    # "Open with" entry, so WinOLS's registration is neither changed nor lost
    assert any(r"Classes\.ols\OpenWithProgids" in l and "uninsdeletevalue" in l for l in lines)
    assert not any(re.search(r'Subkey: "Software\\Classes\\\.ols"', l) for l in lines)


def test_opening_an_ols_runs_the_gui_with_the_path():
    cmd = next(l for l in _logical_lines(_text())
               if r"OVTSuite.ols\shell\open\command" in l)
    assert '"{app}\\{#AppExeName}"" gui ""%1""' in cmd
