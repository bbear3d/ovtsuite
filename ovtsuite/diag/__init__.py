# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag: the standalone vehicle diagnostic tool (``ovtsuite --diag``).

Scope, decisions and the definitions format: docs/OVT_DIAG.md (the JSON
schema of each document kind: ``ovtsuite diag-defs schema``).

OVT Diag is a program of its own (its own window, installer and taskbar
identity), not a tool of the OVT-Suite main window.

Everything here except :mod:`gui_diag` and :mod:`standalone` is Qt-free and
usable from the CLI, the tests and the MCP tools. The protocol engines are the
suite's existing ones (:mod:`ovtsuite.flash.diag`, the K-line and TP 2.0
links); this package adds the generic session surface, the vehicle, the
garage, the journal and the definitions that say what a module's bytes mean.
"""
