# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A procedure document's optional ``category`` (the Procedures tab's filter
pills): the schema takes it as text, the bundled procedures carry one."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.diag import defs as D  # noqa: E402
from ovtsuite.diag.defs import schema as S  # noqa: E402

DOC = {"kind": "procedure", "id": "procedure.test.cat", "name": "Cat", "safety": "read-only",
       "steps": [{"show": "x"}]}


def test_category_is_a_known_text_field():
    assert "category" in S.SPECS["procedure"]["top"] and S.SPECS["procedure"]["top"]["category"] == "str"
    assert S.check_doc(dict(DOC, category="Diagnosis"), "t.yaml") == []
    bad = S.check_doc(dict(DOC, category=5), "t.yaml")
    assert any("'category' must be" in m for m in bad)
    # a document without one is still valid, and the published JSON schema lists it
    assert S.check_doc(dict(DOC), "t.yaml") == []
    assert S.json_schema("procedure")["properties"]["category"] == {"type": "string"}


def test_bundled_procedures_validate_and_carry_a_category():
    packs = D.load_packs([D.bundled_dir()], "bundled")
    res = D.Resolver(packs)
    procs = {r.entry["id"]: r.entry for r in res.procedures(None)}
    assert {"procedure.engine.boost-check", "procedure.engine.egr-check", "procedure.engine.snapshot",
            "procedure.module.fault-baseline"} <= set(procs)
    for pid, body in procs.items():
        assert S.check_doc(dict(body, kind="procedure"), pid) == [], pid
        assert body.get("category") == "Diagnosis", pid
