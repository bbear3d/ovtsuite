# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""AI integration: one tool layer for the MCP server and the in-app assistant.

See docs/AI_PLAN.md. Nothing here imports Qt or any vendor SDK: the MCP
protocol and the provider HTTP APIs are written on the standard library so
the package runs on Python 3.9 and on every platform the app builds for.

The rules every tool keeps:

* values come from the module's definitions (named entities, decoded through
  their equations) -- never guessed from raw bytes;
* nothing writes a binary: a change is a *proposal* (an ``ovtsuite.patch``
  Patch) the user accepts in the app;
* nothing flashes.
"""
