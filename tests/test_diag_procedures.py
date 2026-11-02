# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P4: guided procedures -- the step engine against the scripted
KW1281 module (adaptation writes, journal, verification) and the bundled
read-only engine procedures over the TP 2.0 mock."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_kw1281 import Link, MockKw1281Module  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402

from ovtsuite.flash import tp20_transport as TT
from ovtsuite.diag import defs as D
from ovtsuite.diag import link as L
from ovtsuite.diag import procedures as P
from ovtsuite.diag.session import ModuleSession


class Script(P.Prompter):
    """Answers in order; records what it was asked."""

    def __init__(self, choices=(), confirms=(), live_ok=True):
        self.choices, self.confirms, self.live_ok = list(choices), list(confirms), live_ok
        self.asked = []
        self.shown = []
        self.live_rows = []

    def choose(self, step_id, label, options, default=None):
        self.asked.append(("choose", step_id, sorted(options)))
        return self.choices.pop(0) if self.choices else default

    def confirm(self, text, detail=""):
        self.asked.append(("confirm", text, detail))
        return self.confirms.pop(0) if self.confirms else True

    def show(self, text, rows=None):
        self.shown.append((text, rows))

    def live(self, title, read, seconds, text=""):
        for _ in range(3):
            self.live_rows.append(read())
        return self.live_ok

    def wait(self, text, seconds):
        self.asked.append(("wait", seconds))
        return True


def _resolver(extra_docs=()):
    packs = D.load_packs([D.bundled_dir()], "bundled")
    if extra_docs:
        from ovtsuite.diag.defs.model import Pack, Doc, Match
        p = Pack(id="test-local", version="0", title="", publisher="", license="", requires={}, depends=[],
                 coverage={}, path="", origin="user")
        for body in extra_docs:
            p.docs.append(Doc(kind=body["kind"], match=Match.from_doc(body.get("match")), body=body, path="t.yaml"))
        packs.append(p)
    return D.Resolver(packs)


SRI = {
    "kind": "procedure", "id": "procedure.test.sri", "name": "Test service reset", "safety": "write",
    "applies": {"addr": "17"},
    "steps": [
        {"check": ["ignition_on"]},
        {"read": {"channels": ["adaptation.ch02", "adaptation.ch40"]}},
        {"choose": {"id": "mode", "label": "How?", "options": {
            "zero": {"label": "Counters to zero", "writes": {"adaptation.ch02": 0, "adaptation.ch40": 0}},
            "keep": {"label": "Only ch02", "writes": {"adaptation.ch02": 0}}}}},
        {"write": {"from": "mode", "verify": "reread"}},
        {"wait": {"seconds": 0}},
        {"log": {"journal": True}},
    ],
}


def _kw_session(ecu):
    res = _resolver([SRI])
    row = res.module_address("17", None)
    link = L.Link(Link(ecu), [], "kline")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.connect()
    return sess, res


def test_write_procedure_reads_chooses_confirms_writes_and_journals():
    ecu = MockKw1281Module()
    sess, res = _kw_session(ecu)
    sess.login(12345)
    journal = []
    pr = Script(choices=["zero"], confirms=[True])
    docs = P.available(res, sess.ident)
    doc = next(d for d in docs if d["id"] == "procedure.test.sri")
    run = P.ProcedureRun(doc, sess, res, pr, journal=lambda **kw: journal.append(kw)).run()
    assert run.ok, run.message
    assert run.values["adaptation.ch02"] == 150 and run.values["mode"] == "zero"
    assert [(w.channel, w.before, w.wanted, w.after) for w in run.writes] == [(2, 150, 0, 0), (40, 300, 0, 0)]
    assert all(w.verified for w in run.writes)
    assert ecu.saved[-2:] == [(2, 0, 12345), (40, 0, 12345)]
    # the confirmation showed exactly what would be written, before anything was
    conf = next(a for a in pr.asked if a[0] == "confirm")
    assert "channel 002" in conf[2] and "150 -> 0" in conf[2] and "channel 040" in conf[2]
    assert [j["function"] for j in journal] == ["adaptation", "adaptation", "procedure"]
    assert journal[0]["entry_id"] == "adaptation.ch02" and journal[0]["verified"] is True
    assert journal[-1]["entry_id"] == "procedure.test.sri" and journal[-1]["after"]["mode"] == "zero"
    sess.close()


def test_write_refused_without_confirmation_and_failed_verification():
    ecu = MockKw1281Module()
    sess, res = _kw_session(ecu)
    sess.login(12345)
    doc = next(d for d in P.available(res, sess.ident) if d["id"] == "procedure.test.sri")
    run = P.ProcedureRun(doc, sess, res, Script(choices=["zero"], confirms=[False])).run()
    assert run.status == "cancelled" and not run.writes and ecu.saved == []
    # a module that takes the save but keeps the old value: the re-read catches it
    sess.adaptation_save = lambda ch, val, wsc=None: None
    doc2 = dict(doc)
    doc2["steps"] = [{"write": {"writes": {"adaptation.ch02": 0}, "verify": "reread"}}]
    journal = []
    run2 = P.ProcedureRun(doc2, sess, res, Script(confirms=[True]), journal=lambda **kw: journal.append(kw)).run()
    assert run2.status == "failed" and "reads 150 after the write" in run2.message
    assert journal and journal[0]["verified"] is False               # the attempt is on record
    sess.close()


def test_unknown_entry_and_cancelled_choice():
    ecu = MockKw1281Module()
    sess, res = _kw_session(ecu)
    doc = {"kind": "procedure", "id": "procedure.test.x", "name": "x",
           "steps": [{"read": {"channels": ["adaptation.mystery"]}}]}
    run = P.ProcedureRun(doc, sess, res, Script()).run()
    assert run.status == "failed" and "adaptation.mystery" in run.message
    doc = {"kind": "procedure", "id": "procedure.test.y", "name": "y",
           "steps": [{"choose": {"id": "m", "options": {"a": {"label": "A"}}}}]}
    run = P.ProcedureRun(doc, sess, res, Script(choices=[None]), ).run()
    assert run.status == "cancelled"
    sess.close()


def _engine_tp20(rpm_b=0x25):
    part = b"03G906016LQ "

    def handler(req):
        svc = req[0]
        if svc == 0x10:
            return b"\x50" + req[1:]
        if svc == 0x3E:
            return b"\x7E"
        if req == b"\x1A\x9B":
            return b"\x5A\x9B" + part + b"0971\x03\x00\x00\x4C" + bytes(6) + b"R4"
        if svc == 0x18:
            return b"\x58\x00"
        if svc == 0x21:
            g = req[1]
            if g in (1, 3, 11):
                return b"\x61" + bytes([g]) + bytes([1, 200, rpm_b, 5, 100, 130, 14, 50, 200, 2, 100, 50])
            return b"\x7F\x21\x12"
        return b"\x7F" + bytes([svc, 0x11])
    return handler


def test_bundled_engine_procedures_apply_and_run_read_only():
    res = _resolver()
    row = res.module_address("01", "C6")
    link = L.Link(TT.CanKLine(MockTp20Ecu(_engine_tp20(), dest=0x01), dest=0x01), [], "can")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.connect()
    sess.info.platform = "edc16"                      # as the catalogue would set it
    ids = sorted(d["id"] for d in P.available(res, sess.ident))
    assert "procedure.engine.boost-check" in ids and "procedure.engine.egr-check" in ids
    assert "procedure.engine.snapshot" in ids and "procedure.module.fault-baseline" in ids
    # not for a gearbox
    gbx = sorted(d["id"] for d in P.available(res, D.Identity(addr="02", protocol="kwp2000")))
    assert "procedure.engine.boost-check" not in gbx and "procedure.module.fault-baseline" in gbx
    boost = next(d for d in P.available(res, sess.ident) if d["id"] == "procedure.engine.boost-check")
    assert boost["safety"] == "read-only"
    pr = Script()
    journal = []
    run = P.ProcedureRun(boost, sess, res, pr, journal=lambda **kw: journal.append(kw)).run()
    assert run.ok, run.message
    assert run.notes and run.notes[0].startswith("engine_running: 1480 rpm")   # 0.2*200*37
    assert len(pr.live_rows) == 3 and pr.live_rows[0][0][0] == "011.1"
    assert journal[-1]["function"] == "procedure" and journal[-1]["entry_id"] == "procedure.engine.boost-check"
    # engine off: the precondition fails on the module's own rpm cell
    link2 = L.Link(TT.CanKLine(MockTp20Ecu(_engine_tp20(rpm_b=0), dest=0x01), dest=0x01), [], "can")
    sess2 = ModuleSession(link2, row, res, generation="C6")
    sess2.connect()
    run2 = P.ProcedureRun(boost, sess2, res, Script()).run()
    assert run2.status == "failed" and "engine speed is 0 rpm" in run2.message
    base = next(d for d in P.available(res, sess.ident) if d["id"] == "procedure.module.fault-baseline")
    pr3 = Script()
    run3 = P.ProcedureRun(base, sess, res, pr3, journal=lambda **kw: journal.append(kw)).run()
    assert run3.ok and pr3.shown[0][1] == [["", "no stored faults", "", ""]] and run3.values["faults"] == []
    sess.close()
    sess2.close()
