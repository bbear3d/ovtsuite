# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checksums already known to be right are not worked out again: the editor's
background verify, a save and the flasher share what they found
(ovtsuite.checksum_memo), and the EDC16 content digest is never optional."""
import os
import types

import pytest

from ovtsuite import checksum_memo as memo


class _Cs:
    """A provider that counts its work. ``bad`` bytes are 'corrected' to
    ``good``; anything else is already right."""
    def __init__(self, bad=b"BAD!", good=b"GOOD"):
        self.bad, self.good = bad, good
        self.verifies, self.corrects, self.opts = 0, 0, []

    def is_supported(self, data):
        return True

    def verify(self, data, **opts):
        self.verifies += 1
        self.opts.append(("verify", dict(opts)))
        return types.SimpleNamespace(ok=bytes(data) != self.bad)

    def correct(self, data, **opts):
        self.corrects += 1
        self.opts.append(("correct", dict(opts)))
        return self.good if bytes(data) == self.bad else bytes(data)


@pytest.fixture(autouse=True)
def _clean():
    memo.clear()
    yield
    memo.clear()


def test_the_memo_knows_the_bytes_the_provider_and_the_options():
    cs = _Cs()
    assert not memo.settled(cs, b"x" * 100, {"digest": True})
    memo.mark(cs, b"x" * 100, {"digest": True})
    assert memo.settled(cs, b"x" * 100, {"digest": True})
    assert not memo.settled(cs, b"x" * 99 + b"y", {"digest": True})     # edited
    assert not memo.settled(cs, b"x" * 100, {"digest": False})          # another question
    other = type("Other", (_Cs,), {})()
    assert not memo.settled(other, b"x" * 100, {"digest": True})        # another provider


def test_options_that_are_not_plain_values_are_not_remembered():
    cs = _Cs()
    memo.mark(cs, b"abc", {"key_image": b"\x00" * 8})
    assert not memo.settled(cs, b"abc", {"key_image": b"\x00" * 8})


def test_the_memo_is_bounded():
    cs = _Cs()
    for i in range(40):
        memo.mark(cs, bytes([i]) * 8, {})
    assert not memo.settled(cs, bytes([0]) * 8, {})
    assert memo.settled(cs, bytes([39]) * 8, {})


# ------------------------------------------------------------------ session
def _session_stub(cs, data):
    from ovtsuite.session import TunerSession as T
    s = types.SimpleNamespace(
        binfile=types.SimpleNamespace(data=bytearray(data)),
        correct_program_area=False, last_checksum=None,
        checksum_provider=lambda: cs, _checksum_cache=None)
    s._checksum_opts = types.MethodType(T._checksum_opts, s)
    s._checksum_key = types.MethodType(T._checksum_key, s)
    s.checksum_status = types.MethodType(T.checksum_status, s)
    s.correct_checksums = types.MethodType(T.correct_checksums, s)
    return s


def test_the_session_checks_the_digest_always():
    from ovtsuite.session import TunerSession as T
    s = types.SimpleNamespace(correct_program_area=False)
    assert T._checksum_opts(s)["digest"] is True
    assert not hasattr(T, "write_digest")
    assert "write_digest" not in T.correct_checksums.__code__.co_varnames
    cs = _Cs()
    st = _session_stub(cs, b"fine")
    st.checksum_status()
    assert cs.opts == [("verify", {"digest": True, "program_area": False})]


def test_a_valid_image_found_by_the_background_check_is_not_corrected_again():
    cs = _Cs()
    s = _session_stub(cs, b"fine")
    assert s.checksum_status().ok
    assert memo.settled(cs, b"fine", s._checksum_opts())
    rep, changed = s.correct_checksums()
    assert changed is False and rep.ok
    assert cs.corrects == 0 and cs.verifies == 1       # only the one verify


def test_correcting_an_image_that_needs_it_remembers_the_result():
    cs = _Cs()
    s = _session_stub(cs, b"BAD!")
    rep, changed = s.correct_checksums()
    assert changed and bytes(s.binfile.data) == b"GOOD"
    assert memo.settled(cs, b"GOOD", s._checksum_opts())
    n = cs.corrects
    s.correct_checksums()
    assert cs.corrects == n                            # not again


# ------------------------------------------------------------------ flasher
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _flasher_stub(app):
    from PySide6 import QtWidgets
    from ovtsuite.flash.gui_flash import FlashDialog as F
    st = types.SimpleNamespace(_window=types.SimpleNamespace(primary=None),
                               stage=QtWidgets.QLabel())
    st._checksum_opts = types.MethodType(F._checksum_opts, st)
    return st, types.MethodType(F._correct_and_check, st)


def test_write_does_one_correction_for_an_image_already_corrected(app):
    st, cc = _flasher_stub(app)
    cs = _Cs()
    assert cc("EDC16", cs, b"fine") == b"fine"
    assert cs.corrects == 1                            # was two
    assert cs.opts[0][1]["digest"] is True


def test_write_does_two_corrections_only_for_an_image_that_changes(app):
    st, cc = _flasher_stub(app)
    cs = _Cs()
    assert cc("EDC16", cs, b"BAD!") == b"GOOD"
    assert cs.corrects == 2                            # fix, then prove it stable


def test_write_does_no_correction_for_an_image_found_right_already(app):
    st, cc = _flasher_stub(app)
    cs = _Cs()
    memo.mark(cs, b"fine", st._checksum_opts())
    assert cc("EDC16", cs, b"fine") == b"fine"
    assert cs.corrects == 0
    # and what a write corrects is remembered for the next one
    assert cc("EDC16", cs, b"BAD!") == b"GOOD"
    n = cs.corrects
    assert cc("EDC16", cs, b"GOOD") == b"GOOD" and cs.corrects == n


def test_write_refuses_a_correction_that_does_not_settle(app):
    st, cc = _flasher_stub(app)

    class Flip(_Cs):
        def correct(self, data, **opts):
            self.corrects += 1
            return bytes(data)[::-1] if self.corrects < 3 else bytes(data)
    with pytest.raises(RuntimeError, match="same when corrected twice"):
        cc("EDC16", Flip(), b"abc")


def test_write_forces_the_digest_even_if_the_window_says_otherwise(app):
    st, _cc = _flasher_stub(app)
    st._window.primary = types.SimpleNamespace(
        _checksum_opts=lambda: {"digest": False, "program_area": False})
    assert st._checksum_opts()["digest"] is True
    assert st._checksum_opts({"digest": False})["digest"] is True
