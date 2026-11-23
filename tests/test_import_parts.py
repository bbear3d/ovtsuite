# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Importing into a flash set that links an EEPROM / MPC: compare first
(ovtsuite.project.importparts). Synthetic images, no Qt."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.project import Project  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402
from ovtsuite.project import companions as co  # noqa: E402
from ovtsuite.project import importparts as ip  # noqa: E402
from test_eeprom_pairing import eeprom15, flash15  # noqa: E402
from test_companions import mpc_image  # noqa: E402


def setup(vin=b"WVWZZZ1JZ3W000001"):
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    imp.pair_eeprom(p, fl.id, eeprom15(vin=vin), "car.bin")
    return p, fl


def read_into(p, fl, vin):
    held = {}
    imp.import_read(p, flash15(), "read", eeprom=eeprom15(vin=vin),
                    into_set=fl.id, held=held)
    return held


def test_identical_part_is_listed_but_nothing_to_ask():
    p, fl = setup()
    held = read_into(p, fl, b"WVWZZZ1JZ3W000001")
    assert set(held) == {"eeprom"}
    (v,) = ip.compare(p, fl.id, held)
    assert v.kind == ip.IDENTICAL and v.choices() == []
    assert "identical" in v.text
    assert ip.pending(p, fl.id, held) == []
    # the linked one is untouched
    assert co.injected(p, fl.id)["eeprom"] == eeprom15()


def test_no_held_parts_without_link_or_without_into_set():
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    held = {}
    imp.import_read(p, flash15(), "read", eeprom=eeprom15(), into_set=fl.id,
                    held=held)
    assert held == {}                     # nothing linked yet
    held = {}
    imp.import_read(p, flash15(), "read2", eeprom=eeprom15(), held=held)
    assert held == {}                     # a new set: no into_set


def test_different_vin_is_a_different_identity_with_all_choices_keep_default():
    p, fl = setup()
    held = read_into(p, fl, b"WVWZZZ1JZ3W000002")
    (v,) = ip.pending(p, fl.id, held)
    assert v.kind == ip.IDENTITY and v.identity == "different"
    assert v.text.startswith("different identity")
    assert v.warn_immobiliser
    assert v.choices() == [ip.KEEP, ip.VERSION, ip.SEPARATE]
    assert ip.apply(p, fl.id, [v], {})[0].how == ip.KEEP
    assert co.injected(p, fl.id)["eeprom"] == eeprom15()


def test_new_version_of_the_linked_companion():
    p, fl = setup()
    held = read_into(p, fl, b"WVWZZZ1JZ3W000002")
    (v,) = ip.pending(p, fl.id, held)
    c = v.linked
    n_before = len(p.sets)
    (a,) = ip.apply(p, fl.id, [v], {"eeprom": ip.VERSION}, "donor.bin")
    assert a.how == ip.VERSION and a.touched == [c.id] and fl.id in a.flashes
    assert len(p.sets) == n_before
    assert co.injected(p, fl.id)["eeprom"] == held["eeprom"]
    assert co.links(p, fl.id)["eeprom"].id == c.id
    labels = [x.label for x in p.set(c.id).checkpoints] \
        if hasattr(p.set(c.id), "checkpoints") else []
    assert not labels or "Imported from donor.bin" in labels


def test_separate_companion_is_linked_and_old_stays():
    p, fl = setup()
    old = co.links(p, fl.id)["eeprom"]
    held = read_into(p, fl, b"WVWZZZ1JZ3W000002")
    (v,) = ip.pending(p, fl.id, held)
    (a,) = ip.apply(p, fl.id, [v], {"eeprom": ip.SEPARATE}, "donor.bin")
    assert a.how == ip.SEPARATE and a.flashes == [fl.id]
    new = co.links(p, fl.id)["eeprom"]
    assert new.id != old.id and a.companion == new.id
    assert co.head_bytes(p, new) == held["eeprom"]
    assert co.head_bytes(p, old) == eeprom15()
    assert old in p.sets and not co.flashes_using(p, old.id)


def test_size_mismatch_offers_no_version_and_refuses_it():
    p, fl = setup()
    v = ip.compare(p, fl.id, {"eeprom": b"\x00" * 4096})[0]
    assert v.kind == ip.SIZE and not v.can_version
    assert ip.VERSION not in v.choices() and "cannot become a version" in v.text
    with pytest.raises(ValueError):
        ip.apply(p, fl.id, [v], {"eeprom": ip.VERSION})
    assert co.injected(p, fl.id)["eeprom"] == eeprom15()


def test_shared_flashes_are_named_and_refreshed():
    p, fl = setup()
    fl2 = imp.import_bytes(p, flash15(), "flash2.bin")
    co.link(p, fl2.id, co.links(p, fl.id)["eeprom"].id)
    held = read_into(p, fl, b"WVWZZZ1JZ3W000002")
    (v,) = ip.pending(p, fl.id, held)
    assert v.others == [fl2.name]
    (a,) = ip.apply(p, fl.id, [v], {"eeprom": ip.VERSION})
    assert set(a.flashes) == {fl.id, fl2.id}
    assert co.injected(p, fl2.id)["eeprom"] == held["eeprom"]


def test_mpc_verdicts():
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    fl.platform                          # an EDC15 has no MPC: nothing to say
    assert ip.compare(p, fl.id, {"mpc": mpc_image()}) == []


def test_mpc_same_size_different_software():
    from test_companions import mpc_image as m
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    fl.platform = "edc16"                # let it link an MPC companion
    co.attach(p, fl.id, "mpc", m(), base=0x400000)
    (v,) = ip.compare(p, fl.id, {"mpc": m(sw=b"P475AAXF")})
    assert v.kind == ip.IDENTITY and "software" in v.text
    assert not v.warn_immobiliser        # an MPC carries no immobiliser data
    (same,) = ip.compare(p, fl.id, {"mpc": m()})
    assert same.kind == ip.IDENTICAL
