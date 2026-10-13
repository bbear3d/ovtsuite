# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A save can name what it changed, so iterations are distinguishable.

``history.log_version`` always accepted a ``label``, but ``TunerSession.save``
never passed one, so every checkpoint read "save" and the only thing separating
two passes over an image was a timestamp. That is why tuning across several
passes was being done as numbered files instead -- and numbered files caused a
real loss: one was generated from the wrong parent and silently dropped 5,040
cells of earlier work, with nothing in the filename to say which parent it had.
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite import history                                    # noqa: E402


def _image(tmp_path, name="img.bin", size=0x2000):
    p = tmp_path / name
    p.write_bytes(bytes(range(256)) * (size // 256))
    return str(p)


class _FakeSession:
    """The two things ``log_version`` and ``list_versions`` actually touch."""

    def __init__(self, path):
        self.path = path
        self.change_log = []

    def log(self, line):
        self.change_log.append(line)


def test_a_label_reaches_the_checkpoint(tmp_path):
    s = _FakeSession(_image(tmp_path))
    old = b"\x00" * 64
    new = b"\x00" * 32 + b"\x01" * 32
    rec = history.log_version(s, old, new, label="converter: raise engagement")
    assert rec is not None
    assert rec["label"] == "converter: raise engagement"
    assert history.list_versions(s)[0]["label"] == "converter: raise engagement"


def test_without_a_label_it_still_says_something(tmp_path):
    s = _FakeSession(_image(tmp_path))
    rec = history.log_version(s, b"\x00" * 8, b"\x01" * 8)
    assert rec["label"] == "save"


def test_no_checkpoint_when_nothing_changed(tmp_path):
    s = _FakeSession(_image(tmp_path))
    assert history.log_version(s, b"\xaa" * 16, b"\xaa" * 16, label="x") is None
    assert not history.list_versions(s)


def test_two_passes_are_told_apart_by_label(tmp_path):
    s = _FakeSession(_image(tmp_path))
    history.log_version(s, b"\x00" * 16, b"\x01" * 16, label="shift schedules")
    history.log_version(s, b"\x01" * 16, b"\x02" * 16, label="converter lockup")
    labels = [v["label"] for v in history.list_versions(s)]
    assert labels == ["converter lockup", "shift schedules"], \
        "most recent first, and each pass names itself"


def test_save_passes_its_label_through(tmp_path):
    """The gap this closes: the parameter has to reach ``log_version``."""
    import inspect
    from ovtsuite.session import TunerSession
    sig = inspect.signature(TunerSession.save)
    assert "label" in sig.parameters, \
        "TunerSession.save must accept a label or the history is unnavigable"
    src = inspect.getsource(TunerSession.save)
    assert "label=label" in src.replace(" ", "").replace("\n", ""), \
        "save accepts a label but does not hand it to log_version"


@pytest.mark.slow
def test_a_real_session_records_labelled_checkpoints(tmp_path):
    """End to end on an actual image, since that is where it is used."""
    from conftest import zf6hp_path
    from ovtsuite.session import TunerSession
    src = zf6hp_path("3.0TFSI-Stock.bin")
    work = tmp_path / "work.bin"
    work.write_bytes(open(src, "rb").read())
    S = TunerSession(str(work))
    # change something harmless and well inside the calibration
    at = len(S.binfile.data) // 2
    S.binfile.data[at] = (S.binfile.data[at] + 1) & 0xFF
    S.save(label="first pass")
    S2 = TunerSession(str(work))
    S2.binfile.data[at + 1] = (S2.binfile.data[at + 1] + 1) & 0xFF
    S2.save(label="second pass")
    labels = [v["label"] for v in history.list_versions(TunerSession(str(work)))]
    assert "second pass" in labels and "first pass" in labels, labels
