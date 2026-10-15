# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Placing a definition (A2L, DAMOS, WinOLS, XDF) on a binary with evidence
for every table (ovtsuite.definitions.place).

The measure: a definition of one software placed on another software of the
same project, against that software's own definition as the truth. A table
shown must be where the truth has it; what nothing confirms is left out."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
A2L = os.path.join(ROOT, "Reference", "A2L")
BIN = os.path.join(ROOT, "Reference", "Binaries")
E15 = os.path.join(ROOT, "Reference", "EDC15")
FK = os.path.join(A2L, "03G906016FK_9971_399899_P379_U85B6_EDC16U1_5.41")
AA = os.path.join(A2L, "03G906016AA_5928_370480_P379_U85B2_EDC16U1_5.41")
BKC = os.path.join(A2L, "2007_VW_DAMOS_Golf_Original_03G906016B__5645_0281011478_"
                   "369564_BKC_1.9_104.7PS.bin.ols")


def _read(path):
    if not os.path.exists(path):
        pytest.skip("%s not present" % os.path.basename(path))
    with open(path, "rb") as fh:
        return fh.read()


def _defn(path):
    from ovtsuite.definitions import load
    return load(_read(path), None, os.path.basename(path))


def _offsets(defn, data, pl=None, base=None):
    """name -> file offset of each table's values, as placed (``pl``) or
    as the definition itself puts it."""
    from ovtsuite.definitions import place as P
    by = pl.by_index() if pl is not None else None
    if base is None:
        base = pl.base if pl is not None else defn.base_in(data)
    out = {}
    for i, t in enumerate(defn.tables):
        shift = 0
        if by is not None:
            p = by.get(i)
            if p is None or not p.shown:
                continue
            shift = p.shift
        r = P._resolved(t, P._reader(data, base, shift))
        if r is not None and r.z.addr is not None:
            out[t.name] = r.z.addr - base + shift
    return out


def _score(src, image, truth, reference=None, tbase=None, alias=(0,)):
    from ovtsuite.definitions import place as P
    sd, td = _defn(src), _defn(truth)
    data = _read(image)
    ref = P.reference_from_file(_read(reference), sd) if reference else None
    pl = P.place(sd, data, reference=ref)
    got = _offsets(sd, data, pl)
    want = _offsets(td, data, base=tbase)
    both = [n for n in got if n in want]
    wrong = [n for n in both if want[n] - got[n] not in alias]
    return pl, len(both), wrong


def test_the_definition_of_this_software_places_every_table():
    from ovtsuite.definitions import place as P
    defn = _defn(FK + ".a2l")
    data = _read(os.path.join(BIN, "03G906016FK_U85B6_from_A2L_HEX.bin"))
    pl = P.place(defn, data)
    c = pl.counts()
    assert pl.base == 0x800000 and pl.fit > 0.99 and pl.established
    assert c["moved"] == c["region"] == 0 and c["unverified"] <= 3
    # exactly where the definition puts them
    got, plain = _offsets(defn, data, pl), _offsets(defn, data)
    assert got and all(plain[k] == v for k, v in got.items())


def test_another_software_with_the_original_hex():
    """FK's A2L and HEX on AA (U85B2): parts of the calibration moved by
    -12, -24 and -6860 bytes; the HEX's bytes find them."""
    pl, n, wrong = _score(FK + ".a2l", os.path.join(BIN, "03G906016AA_U85B2_from_A2L_HEX.bin"),
                          AA + ".a2l", reference=FK + ".HEX")
    c = pl.counts()
    assert c["moved"] > 3000 and n > 8500
    assert len(wrong) <= 0.002 * n, wrong[:20]


def test_another_software_by_structure_alone():
    """Without the HEX: the A2L's stored point counts and axes, and the
    records laid out in sequence. Fewer are placed; what is shown is right."""
    pl, n, wrong = _score(FK + ".a2l", os.path.join(BIN, "03G906016AA_U85B2_from_A2L_HEX.bin"),
                          AA + ".a2l")
    assert n > 6000
    assert len(wrong) <= 0.005 * n, wrong[:20]
    assert pl.counts()["unverified"] > 1000          # left out, not guessed


def test_a_winols_project_brings_its_own_image():
    """BKC's OLS carries its 1.5 MB image: on FK's 1 MB binary the bytes
    place its maps (the OLS's file offsets are 0x70000 off)."""
    pl, n, wrong = _score(BKC, os.path.join(BIN, "03G906016FK_U85B6_from_A2L_HEX.bin"),
                          FK + ".a2l")
    assert pl.reference and n > 7000
    assert len(wrong) <= 0.002 * n, wrong[:20]


def test_another_generation_places_only_what_its_own_evidence_does():
    """MSA15 V4G's DAMOS on a VA5 image: the record headers hold no more
    often than by chance, so nothing is placed by a header alone -- and the
    few tables shown are right (either dataset)."""
    pl, n, wrong = _score(os.path.join(E15, "028906021CK_A030V4G_MSA15.DAM"),
                          os.path.join(BIN, "EDC15", "038906018AK.bin"),
                          os.path.join(E15, "038906018AK_A030VA5_MSA15.DAM"),
                          alias=(0, 0x38000, -0x38000))
    assert not pl.established
    shown = sum(v for g, v in pl.counts().items() if g != "unverified")
    assert shown < 30 and not wrong


def test_an_xdf_is_placed_as_it_states_unless_it_does_not_fit():
    """An XDF states nothing about its records but its axes: on its own
    binary every table is shown where it says; on another software (the axes
    rise no more often than by chance) all are left out."""
    from ovtsuite.definitions import place as P
    defn = _defn(os.path.join(E15, "038906012A.xdf"))
    own = P.place(defn, _read(os.path.join(BIN, "EDC15", "038906012A.bin")))
    assert own.established and own.base == 0
    assert own.counts()["unverified"] == 0
    assert own.counts()["stated"] > 4000
    other = P.place(defn, _read(os.path.join(BIN, "EDC15", "038906012CP.bin")))
    assert not other.established
    assert all(p.grade == "unverified" for p in other.items)


def test_a_relocated_definition_reads_the_moved_tables(monkeypatch, tmp_path):
    """xdf_session: the tables nothing confirms are left out, or shown in a
    group of their own when asked; every moved one says so."""
    from ovtsuite import xdf_session
    from ovtsuite.definitions import place as P
    monkeypatch.setattr(P, "_cache_dir", lambda: str(tmp_path))
    data = _read(FK + ".a2l")
    image = _read(os.path.join(BIN, "03G906016AA_U85B2_from_A2L_HEX.bin"))
    hexf = _read(FK + ".HEX")
    defn, pl = xdf_session.placed_definition(data, "a2l", image, {}, hexf)
    assert len(defn.tables) == sum(1 for p in pl.items if p.shown)

    class Bin:
        endian = defn.byte_order()

        def __init__(self, d):
            self.data = bytearray(d)
    ents = xdf_session.entities(defn, Bin(image))
    truth = _offsets(_defn(AA + ".a2l"), image)
    moved = [e for e in ents if "Moved" in e.note]
    assert len(moved) > 1000
    assert sum(1 for e in moved if truth.get(e.name, e.addr) != e.addr) <= 5
    shown, _pl = xdf_session.placed_definition(data, "a2l", image, {"unverified": True}, hexf)
    ents = xdf_session.entities(shown, Bin(image))
    odd = [e for e in ents if e.category_path[0] == xdf_session.UNVERIFIED]
    assert 0 < len(odd) <= pl.counts()["unverified"]
    assert all(e.note.startswith("UNVERIFIED") or "\nUNVERIFIED" in e.note for e in odd)


def test_a_placement_is_kept_and_still_fits_an_edited_image(monkeypatch, tmp_path):
    from ovtsuite.definitions import place as P
    monkeypatch.setattr(P, "_cache_dir", lambda: str(tmp_path))
    defn = _defn(FK + ".a2l")
    data = _read(os.path.join(BIN, "03G906016AA_U85B2_from_A2L_HEX.bin"))
    pl = P.placed(defn, data, key="fk")
    assert os.listdir(str(tmp_path))
    back = P.from_dict(P.to_dict(pl))
    assert [(p.index, p.shift, p.grade) for p in back.items] == \
        [(p.index, p.shift, p.grade) for p in pl.items]
    # a value edited: the same placement, from the cache
    edited = bytearray(data)
    e = next(p for p in pl.items if p.grade == "moved")
    a = defn.tables[e.index].z.addr - pl.base + e.shift
    edited[a] ^= 0x01
    assert P.fits(defn, bytes(edited), pl)
    P._MEMO.clear()
    called = []
    monkeypatch.setattr(P, "place", lambda *a, **k: called.append(1))
    again = P.placed(defn, bytes(edited), key="fk")
    assert not called and len(again.items) == len(pl.items)
    # another software is not this placement
    other = _read(os.path.join(BIN, "03G906016FK_U85B6_from_A2L_HEX.bin"))
    assert not P.fits(defn, other, pl)


def test_the_project_keeps_the_options_and_the_original_image():
    from ovtsuite.project import Project
    from ovtsuite.project import importers as _imp
    import tempfile
    proj = Project("bench")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bench.bin")
        with open(p, "wb") as fh:
            fh.write(bytes(0x2000))
        proj, s = _imp.import_file(p, proj)
    m = s.members[0]
    proj.add_definition(s.id, m.id, "x.a2l", b"/begin PROJECT", "a2l")
    proj.set_definition_options(s.id, m.id, {"base": 0x800000, "unverified": True})
    proj.set_definition_reference(s.id, m.id, "x.hex", b":00000001FF")
    assert m.definition["place"] == {"base": 0x800000, "unverified": True}
    assert proj.definition_reference(s.id, m.id) == b":00000001FF"
    path = m.definition["reference"]
    # a new definition drops the old one's original image
    proj.add_definition(s.id, m.id, "y.a2l", b"/begin PROJECT", "a2l")
    assert path not in proj._blobs and proj.definition_reference(s.id, m.id) is None
    proj.set_definition_use(s.id, m.id, True)
    assert m.definition["use"] == "instead"
    proj.set_definition_use(s.id, m.id, False)
    assert "use" not in m.definition
    proj.set_definition_reference(s.id, m.id, "y.hex", b"x")
    proj.remove_definition(s.id, m.id)
    assert not any("/reference/" in k for k in proj._blobs)


def test_intel_hex_is_read_at_its_own_addresses():
    from ovtsuite.definitions import place as P
    from ovtsuite.definitions.model import Definition
    text = (b":020000040080 7A\n".replace(b" ", b"") +
            b":0400100001020304E2\n:00000001FF\n")
    img, base = P.reference_from_file(text, Definition(type="a2l"))
    assert base == 0x800010 and img == b"\x01\x02\x03\x04"


@pytest.mark.slow
def test_the_placement_dialog_reports_and_keeps_its_choices(tmp_path, monkeypatch):
    try:
        from PySide6 import QtWidgets
    except Exception as exc:                        # pragma: no cover
        pytest.skip("PySide6 is not usable here: %s" % exc)
    from ovtsuite import gui_defplace
    from ovtsuite.definitions import place as P
    monkeypatch.setattr(P, "_cache_dir", lambda: str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = gui_defplace.PlacementDialog(
        None, name="FK.a2l", kind="a2l", data=_read(FK + ".a2l"),
        image=_read(os.path.join(BIN, "03G906016AA_U85B2_from_A2L_HEX.bin")),
        target="ECU")
    assert dlg.compute()
    assert not gui_defplace.nothing_to_review(dlg.placement)
    assert "original image" in dlg._advice.text()
    dlg._show.setChecked(True)
    dlg._fixed.setChecked(True)
    dlg._offset.setText("0x800018")
    assert dlg.result_options() == {"base": 0x800018, "unverified": True}
    assert dlg.compute() and dlg.placement.override and dlg.placement.base == 0x800018
    assert dlg._hide_builtin.isHidden()               # no module defines it
    app.processEvents()
    # a binary a module defines: the choice comes back with the options
    monkeypatch.setenv("OVT_NO_PLACE_PROMPT", "1")
    got = gui_defplace.review(
        None, name="FK.a2l", kind="a2l", data=_read(FK + ".a2l"),
        image=_read(os.path.join(BIN, "03G906016FK_U85B6_from_A2L_HEX.bin")),
        target="ECU", builtin=True)
    assert got is not None and got[0]["hide_builtin"] is True
