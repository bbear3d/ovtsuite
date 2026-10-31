# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where offset 0 of an image sits in a definition's addresses when nothing in
the image says (ovtsuite/definitions/model.py ``Definition.base_by_evidence``):
the definition's guess stands unless it leaves the tables outside the image."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.definitions.model import Definition, Storage, TableDef  # noqa: E402

SIZE = 0x80000


def _defn(addrs, guess):
    d = Definition(type="dam")
    d.base_for = lambda size: guess(size)
    for i, a in enumerate(addrs):
        d.tables.append(TableDef(name="T%d" % i, kind="constant",
                                 z=Storage(addr=a, bits=16)))
    return d


def test_a_512k_guess_that_leaves_every_table_outside_is_not_taken():
    # a DAMOS with no memory segments: "a 512 KB image at 0x80000", but every
    # table address is below 0x80000
    d = _defn([0x3949A + 2 * i for i in range(50)], lambda size: size)
    assert d.base_of(SIZE) == 0x80000                      # the guess
    assert d.base_in(bytes(SIZE)) == 0
    assert "0x0" in d.base_note and "outside" in d.base_note


def test_a_guess_that_holds_the_tables_stands():
    d = _defn([0x80100 + 2 * i for i in range(50)], lambda size: size)
    assert d.base_in(bytes(SIZE)) == 0x80000 and not d.base_note


def test_a_definition_with_no_addresses_keeps_its_guess():
    d = _defn([], lambda size: size)
    assert d.base_in(bytes(SIZE)) == 0x80000
