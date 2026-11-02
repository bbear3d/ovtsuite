# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Procedures tab of OVT Diag (offscreen): the gallery with its category
filters and cards, the step cards of the runner driven by the real engine
(``procedures.ProcedureRun``) through ``begin_run``'s prompter, Stop /
``abort()``, the result summary, the table standard, and the dialog that
shares the prompt view.
``python -m pytest tests/test_gui_diag_procedures.py -q --slow``"""
import os
import sys
import threading
import time
import types

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtGui  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.diag import procedures as P  # noqa: E402
from ovtsuite.diag import gui_procedures_page as G  # noqa: E402


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    a.setStyle("Fusion")
    return a


def pump(app, cond=lambda: False, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


# ------------------------------------------------------------- fixtures
SRI = {"kind": "procedure", "id": "procedure.test.sri", "name": "Service interval reset", "safety": "write",
       "category": "Service", "description": "Shows current values, writes the reset, verifies by re-reading.",
       "applies": {"addr": "17"},
       "steps": [
           {"read": {"channels": ["adaptation.ch02", "adaptation.ch40"]}},
           {"choose": {"id": "mode", "label": "Choose the reset", "options": {
               "zero": {"label": "Counters to zero", "writes": {"adaptation.ch02": 0, "adaptation.ch40": 0}},
               "keep": {"label": "Only ch02", "writes": {"adaptation.ch02": 0}}}}},
           {"write": {"from": "mode", "verify": "reread"}},
           {"log": {"journal": True}}]}
LIVE = {"kind": "procedure", "id": "procedure.test.live", "name": "Live and wait", "safety": "read-only",
        "category": "TDI", "description": "A live group, a wait.", "applies": {"addr": "17"},
        "steps": [{"live": {"group": 1, "seconds": 30, "title": "Engine speed (group 001)", "text": "Hold idle."}},
                  {"show": {"text": "Last reading:", "values": ["live.1"]}},
                  {"wait": {"seconds": 30, "text": "Settling"}}]}
COND = {"kind": "procedure", "id": "procedure.test.cond", "name": "Forced regeneration", "safety": "conditions",
        "description": "Checks first.", "applies": {"addr": "01"}, "steps": [{"check": ["vehicle_stationary"]}]}
GBX = {"kind": "procedure", "id": "procedure.test.gbx", "name": "Gearbox adaptation reset", "safety": "write",
       "category": "Gearbox", "description": "Zero a channel.", "applies": {"addr": "02"},
       "steps": [{"write": {"writes": {"adaptation.ch00": 0}}}]}
NEEDLE = {"kind": "procedure", "id": "procedure.test.needle", "name": "Needle sweep", "safety": "coding",
          "description": "Coding bit.", "applies": {"addr": "17"}, "steps": [{"write": {"writes": {"coding.byte0": 1}}}]}
WEIRD = {"kind": "procedure", "id": "procedure.test.weird", "name": "Odd safety", "safety": "experimental",
         "description": "x", "steps": [{"show": "hi"}]}
BASELINE = {"kind": "procedure", "id": "procedure.test.base", "name": "Fault baseline", "safety": "read-only",
            "category": "Diagnosis", "description": "Faults.", "applies": {},
            "steps": [{"check": ["ignition_on"]}, {"read": {"faults": True}}, {"log": {"journal": True}}]}
DOCS = [SRI, LIVE, COND, GBX, NEEDLE, WEIRD, BASELINE]
ORIGINS = {"procedure.test.live": "user", "procedure.test.needle": "user", "procedure.test.base": "bundled"}
APPLICABLE = {d["id"] for d in DOCS if d["id"] != "procedure.test.gbx"}


def fake_session(values=None, rpm=1480.0):
    vals = dict(values or {2: 14300, 40: 362})
    saved = []
    s = types.SimpleNamespace(addr="17", info=types.SimpleNamespace(name="Instruments"), ident=None, saved=saved,
                              vals=vals)
    s.adaptation_read = lambda ch: types.SimpleNamespace(value=vals.get(ch, 0))

    def sv(ch, v, wsc=None):
        saved.append((ch, v))
        vals[ch] = v
    s.adaptation_save = sv
    cell = types.SimpleNamespace(decoded=(rpm, "rpm", "%d rpm" % rpm), fid=1, a=2, b=3)
    s.read_group_full = lambda g: types.SimpleNamespace(available=True, cells=[cell])
    s.group_names = lambda g: ["Engine speed"]
    return s


def run_engine(doc, prompter, sess=None, out=None):
    sess = sess or fake_session()
    out = out if out is not None else {}

    def go():
        out["result"] = P.ProcedureRun(doc, sess, None, prompter, journal=lambda **kw: None).run()
    th = threading.Thread(target=go, daemon=True)
    th.start()
    out["thread"], out["sess"] = th, sess
    return out


@pytest.fixture
def page(app):
    w = G.ProceduresPage()
    w.resize(1250, 760)
    w.set_procedures(DOCS, ORIGINS, APPLICABLE)
    w.set_context("17", "Instruments", "KWP2000 over TP 2.0", True, False, part="4F0910930")
    w.show()
    yield w
    w.abort()
    w.close()


def states(w):
    return [c.state for c in w.step_cards]


# ------------------------------------------------------------ pure helpers
def test_category_safety_and_card_text_helpers():
    assert G.category_of(SRI) == "Service" and G.category_of(LIVE) == "TDI"
    # derived: read-only -> Diagnosis, adaptation-only writes -> Service, coding -> Comfort & coding, else Other
    assert G.category_of(COND.copy() | {"safety": "read-only"}) == "Diagnosis"
    assert G.category_of({"safety": "write", "steps": [{"write": {"writes": {"adaptation.ch02": 0}}}]}) == "Service"
    assert G.category_of(NEEDLE) == "Comfort & coding"
    assert G.category_of({"safety": "actuates", "steps": []}) == "Other"
    assert G.safety_chip(SRI) == ("write", "warn") and G.safety_chip(BASELINE) == ("read-only", "ok")
    assert G.safety_chip({"safety": "safe"}) == ("safe", "ok")
    assert G.safety_chip({"safety": "conditions"}) == ("conditions", "warn")
    assert G.safety_chip(NEEDLE) == ("coding", "warn")
    assert G.safety_chip(WEIRD) == ("experimental", "neutral")
    assert G.safety_chip({}) == ("read-only", "ok")
    assert G.format_channels([2, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49]) == "02/40–49"
    assert G.format_channels([40, 41]) == "40/41"
    assert G.write_targets(SRI) == ["adaptation.ch02", "adaptation.ch40"]
    assert G.card_sub(SRI, "17", "Instruments") == "17 Instruments · adaptation ch 02/40 · 4 steps"
    assert G.card_sub(GBX).startswith("02 Transmission · adaptation ch 00 · 1 step")
    assert G.card_sub(BASELINE).startswith("Any module · faults")
    assert G.parse_plan("channel 002 Name: 150 -> 0\nchannel 040 x: - -> 5") == [
        ("ch 02", "150 → 0"), ("ch 40", "- → 5")]


def test_steps_become_cards_and_show_wait_log_fold_in():
    boost = {"steps": [{"check": ["engine_running"]}, {"show": "Group 011 is read live"},
                       {"live": {"group": 11, "seconds": 30, "title": "Boost control (group 011)"}},
                       {"show": {"text": "Last reading:", "values": ["live.11"]}},
                       {"log": {"journal": True, "note": "boost"}}]}
    cards = G.procedure_cards(boost)
    assert [(c.kind, c.title, c.indices) for c in cards] == [
        ("check", "Check conditions", [0]), ("live", "Boost control (group 011)", [1, 2, 3, 4])]
    sri = G.procedure_cards(SRI)
    assert [(c.kind, c.title) for c in sri] == [("read", "Read current values"), ("choose", "Choose the reset"),
                                                ("write", "Write, verify, journal")]
    assert sri[2].sub == "Previous values are kept in the vehicle's journal so the change can be reverted."
    assert sri[2].indices == [2, 3]                      # the log folded into the write card
    assert G.procedure_cards(BASELINE)[1].title == "Read stored faults"
    only_show = G.procedure_cards(WEIRD)
    assert [c.kind for c in only_show] == ["show"]
    # a wait after a show-forward does not overlap its neighbour
    odd = G.procedure_cards({"steps": [{"read": {"faults": True}}, {"show": "intro"}, {"wait": 1}, {"live": {"group": 1}}]})
    assert [c.indices for c in odd] == [[0], [1, 2, 3]]


# ----------------------------------------------------------------- gallery
def test_filters_and_counts(page):
    assert page.filter_names() == ["All", "Service", "Diagnosis", "TDI", "Gearbox", "Comfort & coding", "Other", "Mine"]
    texts = page.pill_texts()
    assert texts[0] == "All · 7" and texts[-1] == "Mine · 2"
    assert texts[1:-1] == ["Service", "Diagnosis", "TDI", "Gearbox", "Comfort & coding", "Other"]
    assert page.current_filter() == "All" and len(page.visible_ids()) == 7
    page.set_filter("Service")
    assert page.visible_ids() == ["procedure.test.sri"]
    page.set_filter("Other")                             # derived: conditions / an unknown safety class
    assert page.visible_ids() == ["procedure.test.cond", "procedure.test.weird"]
    page.set_filter("Diagnosis")
    assert page.visible_ids() == ["procedure.test.base"]
    page.set_filter("Comfort & coding")                  # derived: it writes coding
    assert page.visible_ids() == ["procedure.test.needle"]
    page.set_filter("TDI")
    assert page.visible_ids() == ["procedure.test.live"]
    page.set_filter("Mine")
    assert page.visible_ids() == ["procedure.test.live", "procedure.test.needle"]
    # only the visible cards are shown, plus the dashed card
    vis = [i for i, c in ((i, page.card(i)) for i in [d["id"] for d in DOCS]) if c.isVisibleTo(page)]
    assert vis == ["procedure.test.live", "procedure.test.needle"]
    assert page.new_card().isVisibleTo(page)
    page.set_filter("All")
    assert all(page.card(d["id"]).isVisibleTo(page) for d in DOCS)
    # pills come from the categories the documents carry: no TDI document -> no TDI pill
    page.set_procedures([SRI, BASELINE], {}, None)
    assert page.pill_texts() == ["All · 2", "Service", "Diagnosis", "Mine · 0"]
    page.set_filter("Mine")
    assert page.visible_ids() == []
    page.set_filter("Diagnosis")
    page.set_procedures([SRI], {}, None)                 # the Diagnosis pill is gone: back to All
    assert page.current_filter() == "All" and page.visible_ids() == ["procedure.test.sri"]
    page.set_filter("Diagnosis")                         # not a filter now: ignored
    assert page.current_filter() == "All"


def test_card_content_safety_chips_and_dimming(page):
    c = page.card("procedure.test.sri")
    assert c.title.fullText() == "Service interval reset" and c.chip_text() == "write"
    assert c.sub_text() == "17 Instruments · adaptation ch 02/40 · 4 steps"
    assert c.description().startswith("Shows current values")
    assert page.card("procedure.test.cond").chip_text() == "conditions"
    assert page.card("procedure.test.needle").chip_text() == "coding"
    assert page.card("procedure.test.weird").chip_text() == "experimental"
    assert page.card("procedure.test.weird").chip._kind == "neutral"
    assert page.card("procedure.test.base").chip._kind == "ok" and page.card("procedure.test.sri").chip._kind == "warn"
    # not for this module: dimmed, says so, still clickable to read its plan
    g = page.card("procedure.test.gbx")
    assert g.chip_text() == "Not for this module" and g.graphicsEffect() is not None and not g.applicable
    assert page.card("procedure.test.sri").graphicsEffect() is None
    # the dashed card
    n = page.new_card()
    assert n.TITLE == "+ New procedure" and "Write the steps in YAML" in n.TEXT and "record" not in n.TEXT.lower()
    got = []
    page.newProcedureRequested.connect(lambda: got.append(1))
    n.clicked.emit()
    assert got == [1]


def test_select_shows_the_plan_and_the_header(page, app):
    page.select("procedure.test.sri")
    assert page.selected_id() == "procedure.test.sri" and page.card("procedure.test.sri")._selected
    assert not page.card("procedure.test.live")._selected
    assert page.title_label.text() == "Service interval reset"
    assert page.context_label.fullText() == "17 Instruments · 4F0910930 · KWP2000 over TP 2.0"
    assert [c.spec.title for c in page.step_cards] == ["Read current values", "Choose the reset", "Write, verify, journal"]
    assert states(page) == ["pending"] * 3
    plan = page._extra[0]
    assert plan.chip.text() == "write" and "adaptation ch 02/40" in plan.writes_line.text()
    assert page.run_btn.text() == "Run" and page.run_btn.isEnabled()
    assert page.login_label.fullText() == "Login may be needed to write"
    page.select("procedure.test.base")
    assert page.login_label.fullText() == "Login not needed on this module"
    assert page._extra[0].writes_line.text() == "Writes nothing."
    page.select("no.such.procedure")                     # ignored
    assert page.selected_id() == "procedure.test.base"
    # a palette hit on a card hidden by the filter shows the whole list again
    page.set_filter("TDI")
    page.select("procedure.test.sri")
    assert page.current_filter() == "All" and page.selected_id() == "procedure.test.sri"
    # clicking a card selects it too
    page.card("procedure.test.live").clicked.emit()
    assert page.selected_id() == "procedure.test.live"
    # a write procedure's login line follows the session
    page.select("procedure.test.sri")
    page.set_context("17", "Instruments", "KWP2000", True, True)
    assert page.login_label.fullText() == "Logged in"
    explicit = dict(SRI, id="procedure.test.req", requires=["login"])
    page.set_procedures(DOCS + [explicit], ORIGINS, None)
    page.select("procedure.test.req")
    page.set_context("17", "Instruments", "KWP2000", True, False)
    assert page.login_label.fullText() == "Login required"


def test_run_button_states_and_runrequested(page):
    got = []
    page.runRequested.connect(got.append)
    page.select("procedure.test.sri")
    page.set_context("17", "Instruments", "KWP2000 over TP 2.0", False, False)
    assert page.run_btn.text() == "Connect to the module first" and not page.run_btn.isEnabled()
    page.run_btn.click()
    assert got == []
    page.set_context("17", "Instruments", "KWP2000 over TP 2.0", True, False)
    assert page.run_btn.text() == "Run" and page.run_btn.isEnabled()
    page.select("procedure.test.gbx")                    # not for this module
    assert page.run_btn.text() == "Not for this module" and not page.run_btn.isEnabled()
    page.select("procedure.test.sri")
    page.run_btn.click()
    assert len(got) == 1 and got[0]["id"] == "procedure.test.sri"
    assert not page.is_running()                         # the window answers with begin_run


# --------------------------------------------------------------- the runner
def test_begin_run_returns_a_gui_prompter_and_choose_confirm_write_flow(page, app):
    from ovtsuite.diag.gui_procedure import GuiPrompter
    page.select("procedure.test.sri")
    prompter = page.begin_run(SRI)
    assert isinstance(prompter, GuiPrompter) and isinstance(prompter, P.Prompter) and page.is_running()
    assert page.stop_btn.isVisibleTo(page) and page.stop_btn.isEnabled()
    assert states(page) == ["pending"] * 3
    out = run_engine(SRI, prompter)
    # read -> show: the first card is current, its table lists the values, Continue
    assert pump(app, lambda: page._mode == "show")
    assert states(page) == ["current", "pending", "pending"]
    assert page.run_btn.text() == "Continue" and page.run_btn.isEnabled()
    assert page.step_cards[0].slot.indexOf(page._prompt) >= 0 and page._prompt.table.rowCount() == 2
    page.run_btn.click()
    # choose: radio rows in the card, the will-write preview follows the option
    assert pump(app, lambda: page._mode == "choose")
    assert states(page) == ["done", "current", "pending"]
    card = page.step_cards[0]
    assert [p[1] for p in card.pairs.pairs] == ["14300", "362"]
    cur = page.step_cards[1]
    keys = [r.property("key") for r in page._prompt._radios]
    assert keys == ["zero", "keep"] and [r.text() for r in page._prompt._radios] == ["Counters to zero", "Only ch02"]
    assert page._prompt.table.rowCount() == 0
    assert cur.plan.text().startswith("will write: ch 02 → 0 · ch 40 → 0")
    page._prompt._radios[1].setChecked(True)
    assert cur.plan.text().startswith("will write: ch 02 → 0  (1 adaptation write")
    page._prompt._radios[0].setChecked(True)
    assert page.run_btn.text() == "Continue"
    page.run_btn.click()
    # confirm: the plan in the mono box and the Write N channels button
    assert pump(app, lambda: page._mode == "confirm")
    assert states(page) == ["done", "done", "current"]
    assert "Chosen: Counters to zero" in page.step_cards[1].sub.text()
    wc = page.step_cards[2]
    assert wc.plan.text().startswith("will write: ch 02: 14300 → 0 · ch 40: 362 → 0")
    assert "each verified by re-read" in wc.plan.text()
    assert page.run_btn.text() == "Write 2 channels" and page.alt_btn.isVisibleTo(page)
    assert page.alt_btn.text() == "Don't write"
    assert out["sess"].saved == []                       # nothing is written before the answer
    page.run_btn.click()
    assert pump(app, lambda: "result" in out)
    out["thread"].join(5)
    r = out["result"]
    assert r.ok and [(w.channel, w.before, w.wanted, w.after) for w in r.writes] == [(2, 14300, 0, 0), (40, 362, 0, 0)]
    assert out["sess"].saved == [(2, 0), (40, 0)]
    page.finish_run(r)
    assert not page.is_running() and states(page) == ["done"] * 3
    res = page._extra[-1]
    assert isinstance(res, G.ResultCard) and res.chip.text() == "Done"
    assert [c.text() for _l, c in res.write_rows] == ["verified", "verified"]
    assert [l.text() for l, _c in res.write_rows][0].startswith("ch 02")
    assert page.run_btn.text() == "Run again" and page.run_btn.isEnabled()
    got = []
    page.runRequested.connect(got.append)
    page.run_btn.click()
    assert got and got[0]["id"] == "procedure.test.sri"


def test_dont_write_cancels_and_nothing_is_written(page, app):
    prompter = page.begin_run(SRI)
    out = run_engine(SRI, prompter)
    for want in ("show", "choose", "confirm"):
        assert pump(app, lambda: page._mode == want), want
        if want != "confirm":
            page.run_btn.click()
    page.alt_btn.click()
    assert pump(app, lambda: "result" in out)
    out["thread"].join(5)
    assert out["result"].status == "cancelled" and out["sess"].saved == []
    page.finish_run(out["result"])
    assert states(page) == ["done", "done", "stopped"]
    assert page._extra[-1].chip.text() == "Stopped"


def test_show_live_wait_and_check_prompts_land_in_their_cards(page, app):
    doc = {"kind": "procedure", "id": "procedure.test.mix", "name": "Mix", "safety": "read-only",
           "steps": [{"check": ["vehicle_stationary"]}, {"live": {"group": 1, "seconds": 30, "title": "Idle",
                                                                    "text": "Hold idle."}},
                     {"show": {"text": "Last reading:", "values": ["live.1"]}},
                     {"wait": {"seconds": 30, "text": "Settling"}}]}
    page.set_procedures(DOCS + [doc], ORIGINS, None)
    page.select("procedure.test.mix")
    assert [c.spec.title for c in page.step_cards] == ["Check conditions", "Idle"]
    prompter = page.begin_run(doc)
    out = run_engine(doc, prompter)
    # the check asks the user (engine speed is not what the engine checks here): Yes / No
    assert pump(app, lambda: page._mode == "confirm")
    assert page.run_btn.text() == "Yes" and page.alt_btn.text() == "No" and page.alt_btn.isVisibleTo(page)
    assert "stationary" in page._prompt.message.text()
    page.run_btn.click()
    # live: a live table that keeps updating, a countdown, Skip
    assert pump(app, lambda: page._mode == "live")
    assert states(page) == ["done", "current"]
    assert pump(app, lambda: page._prompt.table.rowCount() == 1 and "s left" in page._prompt.countdown.text())
    assert page._prompt.table.item(0, 2).text() == "1480 rpm" and page._prompt.message.text() == "Hold idle."
    assert page.run_btn.text() == "Skip the rest of this step"
    page.run_btn.click()
    # show with the last reading (a table), same card
    assert pump(app, lambda: page._mode == "show")
    assert states(page) == ["done", "current"] and page._prompt.table.rowCount() == 1
    assert page._prompt.message.text() == "Last reading:"
    page.run_btn.click()
    # wait: a countdown, Skip
    assert pump(app, lambda: page._mode == "wait")
    assert page._prompt.message.text() == "Settling" and page._prompt.countdown.text().endswith("s")
    assert page.run_btn.text() == "Skip the rest of this step"
    page.run_btn.click()
    assert pump(app, lambda: "result" in out)
    page.finish_run(out["result"])
    assert out["result"].ok and states(page) == ["done", "done"]
    assert page.step_cards[1].pairs.pairs[0] == ("Engine speed", "1480 rpm")


def test_stop_cancels_and_a_stopped_run_never_writes(page, app):
    prompter = page.begin_run(SRI)
    out = run_engine(SRI, prompter)
    assert pump(app, lambda: page._mode == "show")
    page.run_btn.click()
    assert pump(app, lambda: page._mode == "choose")
    page.stop_btn.click()
    assert pump(app, lambda: "result" in out)
    out["thread"].join(5)
    assert prompter.stopped and out["result"].status == "cancelled" and out["sess"].saved == []
    assert page.run_btn.text() == "Working..." and not page.run_btn.isEnabled() and not page.stop_btn.isEnabled()
    page.finish_run(out["result"])
    assert not page.is_running() and states(page) == ["done", "stopped", "pending"]
    assert page.run_btn.text() == "Run again"
    # Stop while the engine is already at the write confirmation: the confirm is refused
    prompter = page.begin_run(SRI)
    out = run_engine(SRI, prompter)
    for want in ("show", "choose", "confirm"):
        assert pump(app, lambda: page._mode == want)
        if want != "confirm":
            page.run_btn.click()
    page.stop_btn.click()
    assert pump(app, lambda: "result" in out)
    assert out["result"].status == "cancelled" and out["sess"].saved == []


def test_a_stopped_prompter_never_blocks_a_later_step():
    """The engine's read step ignores the show's answer, so a stopped run
    reaches the next prompt: it must return at once, not wait for a user."""
    from ovtsuite.diag.gui_procedure import GuiPrompter
    p = GuiPrompter()
    seen = []
    p.ask.connect(lambda k, d: seen.append(k))
    p.stop()
    assert p.confirm("write?") is False and p.choose("x", "l", {"a": "A"}) is None
    assert p.show("t") is False and p.wait("w", 5) is False and p.live("t", lambda: [], 5) is False
    assert seen == []                                   # nothing is asked of a user who has gone
    # the stop consumed by a take, then another question
    p2 = GuiPrompter()
    p2.stop()
    assert p2._take() is None and p2.stopped
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("a", p2.confirm("again?")), daemon=True)
    th.start()
    th.join(2)
    assert not th.is_alive() and out["a"] is False


def test_abort_releases_a_pending_question_and_the_thread_ends(page, app):
    prompter = page.begin_run(SRI)
    out = run_engine(SRI, prompter)
    for want in ("show", "choose", "confirm"):
        assert pump(app, lambda: page._mode == want)
        if want != "confirm":
            page.run_btn.click()
    assert out["thread"].is_alive()
    page.abort()
    out["thread"].join(5)
    assert not out["thread"].is_alive(), "the engine thread hung on a pending question"
    assert out["result"].status == "cancelled" and out["sess"].saved == []
    # also on a choose, a wait and a live step
    for doc, want in ((SRI, "choose"), (LIVE, "live")):
        p = page.begin_run(doc)
        o = run_engine(doc, p)
        if doc is SRI:
            assert pump(app, lambda: page._mode == "show")
            page.run_btn.click()
        assert pump(app, lambda: page._mode == want)
        page.abort()
        o["thread"].join(5)
        assert not o["thread"].is_alive() and o["result"].status == "cancelled"
    # nothing running: abort is harmless
    page.finish_run(o["result"])
    page.abort()


def test_begin_run_again_drops_the_previous_prompter(page, app):
    p1 = page.begin_run(SRI)
    out = run_engine(SRI, p1)
    assert pump(app, lambda: page._mode == "show")
    p2 = page.begin_run(SRI)                             # the window restarted: the old engine is released
    out["thread"].join(5)
    assert not out["thread"].is_alive() and p1.stopped and p2 is not p1 and not p2.stopped
    assert states(page) == ["pending"] * 3 and page._mode == ""


def test_finish_run_summary_failed_and_not_verified(page, app):
    page.begin_run(SRI)
    res = P.RunResult(procedure_id=SRI["id"], name="x", status="failed", message="channel 002 reads 150 after the write, not 0",
                      writes=[P.Write("adaptation.ch02", 2, 150, 0, after=150), P.Write("adaptation.ch40", 40, 5, 0)],
                      notes=["engine_running: 1480 rpm"])
    page._cur = 2
    page.finish_run(res)
    card = page._extra[-1]
    assert card.chip.text() == "Failed" and "reads 150" in card.message.text()
    assert [c.text() for _l, c in card.write_rows] == ["not verified", "not re-read"]
    assert card.write_rows[0][1]._kind == "bad"
    assert [n.text() for n in card.notes] == ["engine_running: 1480 rpm"]
    assert states(page) == ["done", "done", "failed"]
    # a run that finished without any begin_run (the window restored a result)
    page2 = G.ProceduresPage()
    page2.set_procedures(DOCS, {}, None)
    page2.finish_run(P.RunResult(procedure_id=SRI["id"], name="x", status="done"))
    assert states(page2) == ["done"] * 3 and page2._extra[-1].chip.text() == "Done"
    page2.close()


def test_trace_button_and_select_while_running(page, app):
    got = []
    page.traceRequested.connect(lambda: got.append("t"))
    page.select("procedure.test.sri")
    assert page.trace_btn.text() == "Show trace while writing"
    page.trace_btn.click()
    assert got == ["t"]
    prompter = page.begin_run(SRI)
    page.select("procedure.test.live")                   # ignored while a run is on
    page.card("procedure.test.base").clicked.emit()
    assert page.selected_id() == "procedure.test.sri" and page.title_label.text() == "Service interval reset"
    page.abort()
    assert prompter.stopped


# ------------------------------------------------------- standards & themes
def test_table_standard_and_both_palettes(app):
    from ovtsuite import gui_theme
    from ovtsuite.gui_cellmarks import CellDelegate
    from PySide6 import QtCore
    old = app.palette()
    try:
        for dark in (True, False):
            if dark:
                app.setPalette(gui_theme.dark_palette())
            else:
                pal = QtGui.QPalette()
                R = QtGui.QPalette.ColorRole
                for role, col in ((R.Window, (240, 240, 240)), (R.WindowText, (20, 20, 20)), (R.Base, (255, 255, 255)),
                                  (R.Text, (20, 20, 20)), (R.Button, (240, 240, 240)), (R.ButtonText, (20, 20, 20))):
                    pal.setColor(role, QtGui.QColor(*col))
                app.setPalette(pal)
            w = G.ProceduresPage()
            w.resize(1100, 700)
            w.set_procedures(DOCS, ORIGINS, APPLICABLE)
            w.set_context("17", "Instruments", "KWP2000", True, False)
            w.select("procedure.test.sri")
            w.show()
            pr = w.begin_run(SRI)
            out = run_engine(SRI, pr)
            assert pump(app, lambda: w._mode == "show")
            img = w.grab()
            assert not img.isNull() and img.width() > 500
            tables = w.findChildren(QtWidgets.QTableWidget)
            assert tables
            for t in tables:
                assert t.alternatingRowColors()
                assert isinstance(t.itemDelegate(), CellDelegate)
            # radios / badges / chips paint in either palette
            w.run_btn.click()
            assert pump(app, lambda: w._mode == "choose")
            assert not w.grab().isNull()
            w.abort()
            out["thread"].join(5)
            w.close()
            QtCore.QCoreApplication.sendPostedEvents(None, 0)
    finally:
        app.setPalette(old)


def test_narrow_page_uses_one_column(page, app):
    page.resize(700, 700)
    pump(app, lambda: False, 0.2)
    assert page._cols == 1
    page.resize(1250, 700)
    assert pump(app, lambda: page._cols == 2)


def test_page_close_while_running_does_not_hang(app):
    w = G.ProceduresPage()
    w.set_procedures(DOCS, ORIGINS, None)
    w.set_context("17", "Instruments", "KWP2000", True, False)
    pr = w.begin_run(SRI)
    out = run_engine(SRI, pr)
    assert pump(app, lambda: w._mode == "show")
    w.abort()
    w.close()
    out["thread"].join(5)
    assert not out["thread"].is_alive()


# ------------------------------------------------------------ the dialog
def test_procedure_dialog_still_runs_a_procedure(app):
    from ovtsuite.diag.gui_procedure import ProcedureDialog
    dlg = ProcedureDialog(SRI, "17 Instruments")
    dlg.show()
    assert dlg.steps.count() == 4 and dlg.steps.item(0).text().startswith("1. read")
    out = run_engine(SRI, dlg.prompter)
    assert pump(app, lambda: dlg._mode == "show")
    assert dlg.table.rowCount() == 2 and dlg.next_btn.isVisibleTo(dlg)
    dlg.next_btn.click()
    assert pump(app, lambda: dlg._mode == "choose")
    assert [r.text() for r in dlg._radios] == ["Counters to zero", "Only ch02"] and dlg._radios[0].isChecked()
    dlg._radios[1].setChecked(True)
    dlg.next_btn.click()
    assert pump(app, lambda: dlg._mode == "confirm")
    assert dlg.yes_btn.isVisibleTo(dlg) and "channel 002" in dlg.detail.toPlainText()
    dlg.yes_btn.click()
    assert pump(app, lambda: "result" in out)
    dlg.finish(out["result"])
    assert out["sess"].saved == [(2, 0)] and dlg.next_btn.text() == "Close"
    assert dlg.table.item(0, 3).text() == "verified"
    # Stop and close release the engine
    dlg2 = ProcedureDialog(SRI, "17 Instruments")
    out2 = run_engine(SRI, dlg2.prompter)
    assert pump(app, lambda: dlg2._mode == "show")
    dlg2.reject()
    out2["thread"].join(5)
    assert not out2["thread"].is_alive() and out2["result"].status == "cancelled"
    dlg3 = ProcedureDialog(LIVE, "17 Instruments")
    out3 = run_engine(LIVE, dlg3.prompter)
    assert pump(app, lambda: dlg3._mode == "live")
    dlg3.stop_btn.click()
    out3["thread"].join(5)
    assert out3["result"].status == "cancelled"
