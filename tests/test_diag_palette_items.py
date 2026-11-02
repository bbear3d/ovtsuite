# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What the command palette searches (``ovtsuite.diag.palette_items``): each
kind built from a hand-made context, no Qt and no car.
``python -m pytest tests/test_diag_palette_items.py -q``"""
import time

from ovtsuite.diag import defs as D
from ovtsuite.diag import palette_items as PI
from ovtsuite.diag.model import Fault, ModuleInfo
from ovtsuite.diag.palette_index import KINDS, PaletteItem


def _pack():
    def doc(kind, addr, body):
        return D.Doc(kind, D.Match.from_doc({"addr": addr}), dict(body, kind=kind))
    docs = [
        doc("coding", "19", {"style": "long", "bytes": [
            {"index": 2, "bits": [{"id": "coding.b2.bit3", "bit": 3, "name": "Rain sensor fitted",
                                   "type": "bool"},
                                  {"id": "coding.b2.bit4", "bit": 4, "name": ""}]}]}),
        doc("adaptation", "17", {"channels": [
            {"id": "adaptation.ch02", "channel": 2, "name": "Distance since service", "unit": "km"},
            {"id": "adaptation.ch40", "channel": 40, "name": "Max distance", "unit": "km"}]}),
        doc("actuator_tests", "01", {"tests": [
            {"id": "actuator.egr", "tester_id": 7, "name": "EGR valve"},
            {"id": "actuator.nameless"}]}),
        doc("procedure", "01", {"id": "procedure.engine.snapshot", "name": "Engine snapshot",
                                "description": "Freeze a set of values", "safety": "read-only",
                                "applies": {"addr": "01"}, "steps": [{"show": "a"}, {"show": "b"}, {"show": "c"}]}),
    ]
    return D.Pack(id="t", docs=docs)


def _mods():
    eng = ModuleInfo("01", "Engine", protocol="kwp2000", transport="can", part="03G906016LQ",
                     identity_text="03G906016LQ", platform="edc16", reached=True,
                     extra={"system_name": "EDC16U31"})
    eng.faults = [Fault(code=0x0414, status=0x2F, vag=1044, sae="P0299", text="Charge pressure too low",
                        status_text="stored · 2 occurrences"),
                  Fault(code=0x2299, vag=8857, text="", intermittent=True)]
    gw = ModuleInfo("19", "Gateway", protocol="uds", transport="uds", part="1K0907530", reached=True)
    cl = ModuleInfo("17", "Instruments", protocol="kw1281", transport="kline", part="4F0910930", reached=True,
                    extra={"baud": 9600})
    off = ModuleInfo("08", "Climatronic", reached=False, error="no answer")
    untried = ModuleInfo("09", "Central electrics")
    return [eng, gw, cl, off, untried]


def _ctx(**kw):
    base = dict(
        resolver=D.Resolver([_pack()]), generation="C6", modules=_mods(),
        rows_by_addr={"01": {"addr": "01", "name": "Engine"}},
        lives={"01"}, readings={("01", 11, 3): ("1012", "mbar")},
        names_for=lambda a: {11: ["Engine speed", "Boost setpoint", "Boost actual", None]} if a == "01" else {},
        signals_for=lambda a: {(11, 3): "Ldm_pIst"} if a == "01" else {},
        selected_addr="01")
    base.update(kw)
    return PI.Context(**base)


def _kind(items, kind):
    return [i for i in items if i.kind == kind]


def test_every_item_is_a_valid_palette_item_of_a_known_kind():
    items = PI.build_items(_ctx())
    assert items and all(isinstance(i, PaletteItem) and i.kind in KINDS and i.title for i in items)
    assert {i.kind for i in items} == set(KINDS)
    assert all(isinstance(i.payload, dict) for i in items)


def test_modules_are_the_reached_ones_with_a_fault_chip():
    mods = {i.addr: i for i in _kind(PI.build_items(_ctx()), "module")}
    assert sorted(mods) == ["01", "17", "19"]                      # not the unreached, not the untried
    eng = mods["01"]
    assert eng.title == "Engine" and eng.subtitle == "03G906016LQ · EDC16U31 · KWP2000 over TP 2.0"
    assert eng.chip == "2" and eng.chip_kind == "bad"
    assert mods["19"].chip == "" and mods["19"].subtitle.endswith("UDS")
    assert mods["17"].subtitle.endswith("KW1281 @ 9600")


def test_live_items_name_every_named_cell_with_signal_and_value():
    live = {(i.addr, i.payload["group"], i.payload["cell"]): i for i in _kind(PI.build_items(_ctx()), "live")}
    assert sorted(live) == [("01", 11, 1), ("01", 11, 2), ("01", 11, 3)]     # the unnamed 4th cell is no item
    i3 = live[("01", 11, 3)]
    assert i3.title == "Boost actual"
    assert i3.subtitle == "Engine · group 011 cell 3 · Ldm_pIst from the factory catalogue"
    assert "Ldm_pIst" in i3.keywords and i3.value == "1012 mbar"
    assert live[("01", 11, 1)].value == "" and "catalogue" not in live[("01", 11, 1)].subtitle


def test_fault_items_carry_every_form_of_the_code():
    faults = _kind(PI.build_items(_ctx()), "fault")
    assert [f.addr for f in faults] == ["01", "01"]
    a, b = faults
    assert a.title == "Charge pressure too low" and a.subtitle == "Engine · stored · 2 occurrences"
    for form in ("01044", "001044", "P0299", "0414", "0x0414"):
        assert form in a.keywords.split(), form
    assert a.chip == "active" and a.chip_kind == "bad"
    assert b.title == "(no text)" and b.chip == "history" and b.chip_kind == "ok"
    assert "008857" in b.keywords.split() and "08857" in b.keywords.split()
    assert a.payload["code"] == 0x0414 and a.payload["addr"] == "01" and a.payload["index"] == 0


def test_coding_adaptation_actuator_come_from_the_definitions():
    items = PI.build_items(_ctx())
    coding = _kind(items, "coding")
    assert len(coding) == 1                                       # the nameless bit is no item
    assert coding[0].title == "Rain sensor fitted" and coding[0].addr == "19"
    assert coding[0].subtitle == "Gateway · byte 2 bit 3" and coding[0].payload["byte"] == 2 and coding[0].payload["bit"] == 3
    adapt = _kind(items, "adaptation")
    assert [(a.title, a.subtitle, a.payload["channel"], a.payload["id"]) for a in adapt] == [
        ("Distance since service", "Instruments · channel 02", 2, "adaptation.ch02"),
        ("Max distance", "Instruments · channel 40", 40, "adaptation.ch40")]
    act = _kind(items, "actuator")
    assert [a.title for a in act] == ["Actuator test: EGR valve"]      # no 'Output test'; the unnamed one is skipped
    assert act[0].addr == "01" and act[0].payload["id"] == "actuator.egr" and "0x0007" in act[0].subtitle
    assert "output" not in " ".join(a.title.lower() for a in act)


def test_procedures_come_from_the_resolver_and_are_listed_once():
    procs = _kind(PI.build_items(_ctx()), "procedure")
    assert [p.title for p in procs] == ["Procedure: Engine snapshot"]
    p = procs[0]
    assert p.subtitle == "Guided · 3 steps · read-only · Engine" and p.payload == {
        "id": "procedure.engine.snapshot", "addr": "01", "key": "procedure.engine.snapshot"}


def test_a_procedure_for_several_modules_is_one_item_naming_them():
    doc = {"id": "p.all", "name": "Fault baseline", "safety": "read-only", "steps": [{"show": "x"}]}
    ctx = _ctx(procedures_for=lambda a: [doc], selected_addr="17")
    procs = _kind(PI.build_items(ctx), "procedure")
    assert len(procs) == 1
    assert procs[0].subtitle == "Guided · 1 step · read-only · Engine, Instruments, Gateway"
    assert procs[0].addr == "17"                                  # the selected module when it is one of them
    many = [ModuleInfo("%02X" % n, "M%d" % n, reached=True) for n in range(1, 8)]
    procs = _kind(PI.build_items(_ctx(modules=many, procedures_for=lambda a: [doc])), "procedure")
    assert len(procs) == 1 and procs[0].subtitle.endswith("7 modules")


def test_actions_follow_the_state():
    def acts(**kw):
        return {i.payload["action"]: i for i in _kind(PI.build_items(_ctx(**kw)), "action")}
    a = acts()
    assert set(a) <= set(PI.ACTIONS) and {"scan_all", "disconnect", "disconnect_all", "open_journal", "export_html",
                                           "export_pdf", "export_text", "open_tools", "open_obd", "open_garage",
                                           "open_gateway", "trace_toggle", "install_pack", "reload_definitions",
                                           "open_definitions_folder"} <= set(a)
    assert "connect" not in a and "stop_scan" not in a             # 01 is connected; no scan running
    assert a["disconnect"].title == "Disconnect 01 Engine" and a["trace_toggle"].title == "Start trace recording"
    b = acts(lives=set(), selected_addr="17", recording=True, scanning=True)
    assert b["connect"].title == "Connect 17 Instruments" and "disconnect" not in b and "disconnect_all" not in b
    assert b["trace_toggle"].title == "Stop trace recording" and "stop_scan" in b
    assert "connect" not in acts(selected_addr="")


def test_value_key_addresses_the_item():
    items = PI.build_items(_ctx())
    live = next(i for i in items if i.kind == "live" and i.payload["cell"] == 3)
    assert PI.value_key(live) == ("01", "live", "11.3")
    keys = [PI.value_key(i) for i in items if i.kind == "live"]
    assert len(set(keys)) == len(keys)


def test_a_failing_source_contributes_nothing_and_the_rest_still_build():
    def boom(_a):
        raise RuntimeError("no names")

    class BadResolver:
        def coding_doc(self, ident):
            raise RuntimeError("bad coding")

        def view(self, kind, ident):
            raise RuntimeError("bad " + kind)

        def procedures(self, ident=None):
            raise RuntimeError("bad procedures")

    items = PI.build_items(_ctx(names_for=boom, resolver=BadResolver()))
    kinds = {i.kind for i in items}
    assert kinds == {"module", "fault", "action"}
    # an empty context builds the actions only
    assert {i.kind for i in PI.build_items(PI.Context())} == {"action"}
    # a module the context cannot describe is skipped, not fatal
    assert PI.build_items(PI.Context(modules=[None]))


def test_modules_without_names_give_no_live_items_and_dupes_collapse():
    ctx = _ctx(names_for=lambda a: {}, modules=_mods() + [ModuleInfo("01", "Engine", reached=True)])
    items = PI.build_items(ctx)
    assert not _kind(items, "live")
    assert len(_kind(items, "module")) == 3                      # one per address


def test_a_twenty_module_car_builds_quickly():
    mods = [ModuleInfo("%02X" % n, "Module %d" % n, protocol="kwp2000", part="1K0%06d" % n, reached=True)
            for n in range(1, 21)]
    for m in mods:
        m.faults = [Fault(code=1000 + k, vag=1000 + k, text="Fault %d" % k) for k in range(5)]
    cells = {g: ["Cell %d.%d" % (g, c) for c in range(1, 5)] for g in range(1, 101)}
    ctx = _ctx(modules=mods, names_for=lambda a: cells, signals_for=lambda a: {(1, 1): "sig"},
               lives=set(), readings={})
    t = time.perf_counter()
    items = PI.build_items(ctx)
    dt = time.perf_counter() - t
    assert len(_kind(items, "live")) == 20 * 400 and len(_kind(items, "fault")) == 100
    assert dt < 0.2, "built %d items in %.0f ms" % (len(items), dt * 1000)
