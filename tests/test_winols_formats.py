# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS layouts found in a 191-project corpus: one sample of each in
Reference/WinOLS-formats (git-ignored, like the rest of Reference/).

Every sample must parse to the record count it declares, and a project's
list must end exactly where its folder section starts."""

import os
import struct

import pytest

from ovtsuite.definitions import winols as W

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "Reference", "WinOLS-formats")
SAMPLES = {
    "f303_NissanGTR_Hitachi.ols": 303,
    "f373_BMW_F46_MED17.2.3.ols": 373,
    "f440_Peugeot308_VD46.kp": 440,
    "f440_M235i_translated_axis_texts.ols": 440,
    "f479_Simos18_display_flags.ols": 479,
    "org9_Renault_Valeo_V50.kp": None,
}


def _data(name):
    p = os.path.join(DIR, name)
    if not os.path.isfile(p):
        pytest.skip("sample missing: %s" % p)
    with open(p, "rb") as fh:
        return fh.read()


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_parses_to_its_declared_count(name):
    d = _data(name)
    wf = W.parse(d)
    if SAMPLES[name]:
        assert wf.header.version == SAMPLES[name]
    assert wf.problems == []
    assert len(wf.records) == wf.declared > 0


@pytest.mark.parametrize("name", [n for n in sorted(SAMPLES) if n.endswith(".ols")])
def test_project_list_ends_at_the_folder_section(name):
    d = _data(name)
    h = W.read_header(d)
    p = d.find(W.RECORDS_MAGIC, h.end)
    start = p + 4 + (1 if h.version >= 200 else 0)
    count = struct.unpack_from("<I", d, start)[0]
    recs, end, prof, probs = W.read_records(d, start + 4, count, h.version)
    assert not probs and d[end:end + 4] == W.FOLDERS_MAGIC


def test_translated_axis_texts_are_kept():
    wf = W.parse(_data("f440_M235i_translated_axis_texts.ols"))
    rec = next(r for r in wf.records if r.name == "KF_MODE_MDMAX_DKG")
    assert len(rec.x.texts) == 10 and all(isinstance(t, str) for t in rec.x.texts)


def test_one_byte_elements_of_an_unlisted_organisation_are_read():
    d = W.load(_data("org9_Renault_Valeo_V50.kp"))
    assert not any("organisation" in w for w in d.warnings)
    assert sum(1 for t in d.tables if t.z.bits == 8) > 2000
