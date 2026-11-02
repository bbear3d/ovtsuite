# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The session a module's per-dataset hooks get (ovtsuite/moduleview.py): a
definition file loaded beside the module's own tables is not a dataset."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import moduleview  # noqa: E402
from ovtsuite.modules.api import ModuleSpec  # noqa: E402


def _ent(name, group, source="kb"):
    return types.SimpleNamespace(name=name, group=group, source=source)


def _session(*ents):
    return types.SimpleNamespace(entities=list(ents), family="p_pd", marker=1)


OWN = [_ent("zmwP_KL_P", "Automatic"), _ent("fnwSWGK0KR", "Automatic")]
DEF = [_ent("zmwP_KL_P", "", "definition"), _ent("fnwSWGK_KL", "", "definition")]


def test_the_view_leaves_a_definition_files_entities_out():
    v = moduleview.viewed(_session(*OWN, *DEF))
    assert v.entities == OWN
    assert v.family == "p_pd" and v.marker == 1        # all else is the session's


def test_a_definition_alone_keeps_its_entities():
    assert moduleview.viewed(_session(*DEF)).entities == DEF


def test_the_view_follows_the_session_and_writes_through():
    s = _session(*OWN)
    v = moduleview.viewed(s)
    assert v.entities == OWN
    s.entities = OWN + DEF                              # a definition added later
    assert v.entities == OWN
    s.entities = DEF + OWN[:1]
    assert v.entities == OWN[:1]
    v.marker = 2
    assert s.marker == 2
    v.entities = list(DEF)
    assert s.entities == DEF


def test_only_a_session_with_an_entity_list_is_wrapped():
    assert moduleview.viewed(None) is None
    plain = types.SimpleNamespace(x=1)
    assert moduleview.viewed(plain) is plain
    v = moduleview.viewed(_session(*OWN))
    assert moduleview.viewed(v) is v


def _hook(session):
    return [e.name + "@" + e.group for e in session.entities]


def test_the_per_dataset_hooks_get_the_view_and_the_others_do_not():
    spec = ModuleSpec(id="x", name="x", version="1", platforms=(),
                      capabilities=(), injection=__name__ + ":_hook",
                      dyno=__name__ + ":_hook", features=__name__ + ":_hook")
    s = _session(*OWN, *DEF)
    assert spec.call("injection", s) == ["zmwP_KL_P@Automatic", "fnwSWGK0KR@Automatic"]
    assert spec.call("dyno", s) == ["zmwP_KL_P@Automatic", "fnwSWGK0KR@Automatic"]
    assert len(spec.call("features", s)) == 4           # not a per-dataset hook
