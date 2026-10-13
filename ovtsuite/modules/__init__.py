# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The module system: what is installed, and how the base app reaches it.

* :mod:`.api` -- the contract a module implements (``ModuleSpec``, the
  checksum provider interface, ``MODULE_API``).
* :mod:`.registry` -- which modules are installed and which one serves a
  platform; the only way base code should reach module code.
* :mod:`.loader` -- finding modules (in-tree packages today, signed
  ``.ovtmod`` packages next) and hiding disabled ones.

See docs/MODULES_PLAN.md.
"""
