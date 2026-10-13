# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the AI pages (ids ``ai-...``): data/help/ai.md and ai/*.md.

The EDC16 reference binary is opened as a project and the assistant panel is
shown with a short sample conversation about its smoke limiter. The proposal
in it is a real one, made by the same tool an AI uses, so the Proposals
window and the "AI: 1 proposal" button show what a user sees. Nothing is
sent anywhere: no service is called.
"""
import os
import tempfile

from help_screenshots import Skip, shot

SMOKE = "FlMng_qPresSmoke_MAP"
RPM_FROM = 3000
HPA_FROM = 2000


def _ai_home(ctx):
    """Settings and proposals in a folder of their own, with the assistant
    switched on and a service chosen (the picture of a set-up app)."""
    d = tempfile.mkdtemp(prefix="ovt-help-ai-")
    os.environ["OVT_AI_DIR"] = d
    from ovtsuite.ai import config as C
    C.update(enabled=True, service="anthropic")
    return d


def _window(ctx):
    _ai_home(ctx)
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = w.current
    from PySide6 import QtWidgets
    sp = ws.findChild(QtWidgets.QSplitter)
    if sp is not None:
        sp.setSizes([300, 2000])
    ctx.open_table(w, SMOKE)
    from ovtsuite import gui_project
    gui_project.panel(w).collapse(remember=False)    # room for the chat
    return w, ws


def _propose(w, ws):
    """The proposal from the sample conversation: +5 % from 3000 rpm at
    2000 hPa and above. Its id."""
    from ovtsuite.ai.live import LiveWorkspace
    from ovtsuite.ai.tools import Context, default_registry
    e = next((x for x in ws.primary.entities if x.name == SMOKE), None)
    if e is None:
        raise Skip("no %s in this file" % SMOKE)
    cols = [i for i, v in enumerate(e.x_values()) if v >= RPM_FROM]
    rows = [i for i, v in enumerate(e.y_values()) if v >= HPA_FROM]
    ctx_ = Context(LiveWorkspace(w), client="assistant")       # as the panel does
    ok, r = default_registry().call("propose_change", ctx_, {
        "handle": "t1", "title": "Smoke limiter +5%% from %d rpm at high boost" % RPM_FROM,
        "rationale": "More fuel where boost is already up; the low-boost rows that "
                     "limit smoke while the turbo spools are left alone.",
        "changes": [{"map": SMOKE, "scale": 1.05, "rows": rows, "cols": cols}]})
    if not ok:
        raise Skip("the sample proposal was refused: %s" % r)
    return r["proposal"], e


@shot("ai-panel", "data/help", size=(1400, 860),
      what="The AI assistant panel beside a map, with a proposal in the chat")
def panel(ctx):
    from ovtsuite import gui_ai
    w, ws = _window(ctx)
    pid, e = _propose(w, ws)
    d = gui_ai.show_assistant(w)
    w.resizeDocks([d], [400], ctx.QtCore.Qt.Orientation.Horizontal)
    vals = e.values()
    top = vals[-1]
    peak = max(top)
    at = [int(e.x_values()[i]) for i, v in enumerate(top) if v == peak]
    xs, ys = e.x_values(), e.y_values()
    d.chat = [
        ("you", "What does this map do?"),
        ("ai", "**Max fuel vs. boost (Smoke Limiter, MAP)** caps how much fuel the engine "
               "may inject, in mg per stroke, for the boost pressure measured: engine speed "
               "across (%d–%d rpm), pressure down (%d–%d hPa). Whatever the driver asks for, "
               "injection stays under this limit, so it decides full-load fuelling while the "
               "turbo is still building boost.\n\nIn this file the limit peaks at %.1f mg at "
               "%s rpm with full boost and falls to %.1f mg by %d rpm."
               % (xs[0], xs[-1], ys[0], ys[-1], peak, "–".join(str(a) for a in at[:2]),
                  top[-1], xs[-1])),
        ("map", ("t1", SMOKE, "Max fuel vs. boost (Smoke Limiter, MAP)")),
        ("you", "Raise it 5%% from %d rpm up, at %d hPa and above." % (RPM_FROM, HPA_FROM)),
        ("ai", "Done as a proposal: the cells from %d rpm and %d hPa up, × 1.05. The safety "
               "checks found nothing to stop it. Preview it to see exactly which cells change; "
               "nothing is written until you accept it." % (RPM_FROM, HPA_FROM)),
        ("proposal", ("Smoke limiter +5%% from %d rpm at high boost" % RPM_FROM, pid)),
    ]
    d.tokens = 6240
    d._render()
    d._update_status()
    d.refresh_context()
    gui_ai.state(w).poll()
    return w


@shot("ai-proposals", "data/help", size=(1000, 640),
      what="Tools > AI > Proposals with one pending change")
def proposals(ctx):
    from ovtsuite import gui_ai
    w, ws = _window(ctx)
    _propose(w, ws)
    st = gui_ai.state(w)
    st.poll()
    st.open_proposals()
    dlg = ctx.keep(st.dialog)
    if dlg.list.count():
        dlg.list.setCurrentRow(0)
    return dlg


@shot("ai-settings", "data/help", size=(720, 640),
      what="Tools > AI > Settings: service, model, key, privacy")
def settings(ctx):
    from ovtsuite import gui_ai
    _ai_home(ctx)
    w = ctx.keep(ctx.main_window())
    return ctx.keep(gui_ai.AiSettingsDialog(gui_ai.state(w)))


@shot("ai-connect", "data/help", size=(760, 440),
      what="Tools > AI > Connect an AI app: the MCP set-up")
def connect(ctx):
    from ovtsuite import gui_ai
    _ai_home(ctx)
    w = ctx.keep(ctx.main_window())
    return ctx.keep(gui_ai.ConnectDialog(gui_ai.state(w)))
