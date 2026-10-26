# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A read's name and project information come from what the binary says of
itself (platforms.identify.read_name / read_info)."""
from ovtsuite.platforms import edc15 as P15
from ovtsuite.platforms.identify import Identity, read_info, read_name


def test_edc15_dataset_ident_is_parsed():
    blob = b"\xC3" * 64 + b"038906012BD 1,9l R4 EDC  SG  2507 28SA4038" + b"\xC3" * 64
    assert P15.ident(blob) == {"part": "038906012BD", "system": "1,9l R4 EDC",
                               "software": "2507"}
    assert P15.ident(b"\xC3" * 256) == {}


def test_name_and_information_from_the_facts():
    i = Identity("edc15", "structure", {"part": "038906012BD", "software": "2507",
                                        "system": "1,9l R4 EDC",
                                        "variant": "3 dataset(s)"})
    assert read_name(i, "ECU-read-1") == "EDC15 038906012BD 2507"
    assert read_info(i) == {"ecu": "Bosch EDC15 038906012BD",
                            "notes": "From the read: software 2507, 1,9l R4 EDC."}


def test_without_a_part_the_given_name_stands():
    assert read_name(Identity("vdo", "structure", {"family": "x layout"}),
                     "Cluster-1") == "Cluster-1"
    assert read_name(Identity("unknown", "none"), "ECU-read-1") == "ECU-read-1"
    assert read_info(Identity("unknown", "none")) == {}
