# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the base app's general pages (welcome, main window, help)."""
from help_screenshots import shot


@shot("main-window", "data/help", size=(1280, 780),
      what="The main window with an EDC16 file open and a map showing")
def main_window(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = w.current
    # the first map in the tree: something with a table to show
    e = next((e for e in ws.primary.entities if getattr(e, "kind", "") == "map"), None)
    if e is not None:
        ws.open_table(e)
    return w


@shot("help-window", "data/help", size=(1000, 640), what="The help window")
def help_window(ctx):
    from ovtsuite import gui_help
    gui_help._window = None
    w = gui_help.HelpWindow(None)
    w.show_page("flashing")
    return ctx.keep(w)
