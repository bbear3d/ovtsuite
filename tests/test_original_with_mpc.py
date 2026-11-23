# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A file that carries the EDC16's internal flash (MPC) beside the main image
(an .sgo, a PCMFlash file, a native container) can be made the new original of
a set whose original has the main image only: the MPC belongs to the linked
companion set, not to this original, so it is left out rather than refusing
the file."""
import pytest

from ovtsuite import container as C
from ovtsuite import project as ovt
from ovtsuite.platforms import edc16 as E
from ovtsuite.project import importers as imp

CAL = E.CAL_START


def _full(tag):
    b = bytearray(bytes([0xA5]) * CAL + bytes(E.CAL_SIZE))
    b[CAL + 0x3D:CAL + 0x41] = E.CAL_MARKER
    b[CAL + 0x1000:CAL + 0x1004] = E.DESCRIPTOR
    b[CAL + 0x2000] = tag
    return bytes(b)


def _with_mpc(tag):
    c = C.Container("edc16")
    c.add(C.BLOCK_EXTERNAL, _full(tag)).add(C.BLOCK_MPC, b"\x5A" * 0x81000)
    return c.to_bytes()


def test_a_file_with_the_mpc_is_a_new_original_of_a_flash_only_set():
    p = ovt.Project("x")
    s = imp.import_bytes(p, _full(1), "first.bin")
    assert set(p.state(s.id, s.roots()[0].id)) == {"flash"}
    images, source, _orig = imp.original_images(p, s.id, _with_mpc(2), "stock.edc16")
    assert set(images) == {"flash"}
    assert p.original_problem(s.id, images) == ""
    c = p.rebase_original(s.id, images, source=source)
    assert p.state(s.id, c.id)["flash"][CAL + 0x2000] == 2


def test_the_mpc_is_handed_back_to_be_offered_as_the_companion():
    from ovtsuite.project import companions as co
    p = ovt.Project("x")
    s = imp.import_bytes(p, _full(1), "first.bin")
    images, _src, _orig, extra = imp.original_parts(p, s.id, _with_mpc(2), "stock.edc16")
    assert set(images) == {"flash"} and set(extra) == {"mpc"}
    assert not co.links(p, s.id)                  # nothing linked: nothing changed
    # no MPC linked yet: a companion set of its own, linked
    assert co.take(p, s.id, "mpc", extra["mpc"], "Imported MPC") == "set"
    c = co.links(p, s.id)["mpc"]
    assert co.head_bytes(p, c) == extra["mpc"]
    # the same bytes again: nothing to do
    assert co.take(p, s.id, "mpc", extra["mpc"]) == "same"
    # other bytes of the same size: a new version of the linked one
    other = bytes([0x11]) * len(extra["mpc"])
    assert co.take(p, s.id, "mpc", other) == "version"
    assert co.head_bytes(p, c) == other
    assert len(co.links(p, s.id)) == 1
    # another size is refused, the companion untouched
    with pytest.raises(ValueError):
        co.take(p, s.id, "mpc", b"\x00" * 16)
    assert co.head_bytes(p, c) == other


def _offer(monkeypatch, answer):
    from types import SimpleNamespace
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from ovtsuite import gui_original as go
    asked = []

    def question(parent, title, text, buttons=None, default=None):
        asked.append(text)
        return (QtWidgets.QMessageBox.StandardButton.Yes if answer
                else QtWidgets.QMessageBox.StandardButton.No)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    p = ovt.Project("x")
    s = imp.import_bytes(p, _full(1), "first.bin")
    _i, _s, _o, extra = imp.original_parts(p, s.id, _with_mpc(2), "stock.edc16")
    done = go.offer_companions(None, SimpleNamespace(project=p), s.id,
                               "stock.edc16", extra)
    return p, s, done, asked


def test_the_mpc_is_asked_about_and_added_only_on_yes(monkeypatch):
    from ovtsuite.project import companions as co
    p, s, done, asked = _offer(monkeypatch, True)
    assert done == ["mpc"] and len(asked) == 1
    assert "MPC" in asked[0] and "linked to" in asked[0]
    assert "mpc" in co.links(p, s.id)


def test_the_mpc_is_left_out_on_no(monkeypatch):
    from ovtsuite.project import companions as co
    p, s, done, asked = _offer(monkeypatch, False)
    assert done == [] and len(asked) == 1
    assert not co.links(p, s.id) and len(p.sets) == 1
