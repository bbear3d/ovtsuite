# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT-Suite (Old VAG Tuner Suite): Bosch EDC16, Bosch EDC15 and ZF 6HP
VW-group ECU and TCU binaries, in one tool.

Each platform is a sibling package -- ``edc15`` today, ``zf6hp`` next -- behind
one Qt-free ``TunerSession`` seam, one ``Entity`` model and one GUI. Every
product and package name lives in :mod:`ovtsuite.branding`; nothing else in the
tree spells them out, so renaming the suite stays a single command
(``tools/rename_package.py``).

Architecture and several modules are adapted from the VAG-6HP-Tuner project
(hp6tuner): the Qt-free session seam, big-endian binary access, and the XDF
anchor-matching approach.
"""

from .branding import __version__      # noqa: F401  (the one source of truth)

# Apply OVT_DISABLE_MODULES before anything imports a module package (and,
# from the next phase, mount installed .ovtmod packages).
from .modules import loader as _loader   # noqa: E402

_loader.install()
del _loader
