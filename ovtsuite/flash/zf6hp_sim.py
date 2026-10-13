# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Back-compat shim. The bench simulator is now module-agnostic and lives in
:mod:`ovtsuite.flash.ecu_sim`; this re-exports the names earlier code and tests
used. New code should import from ecu_sim directly."""
from .ecu_sim import (  # noqa: F401
    RX_ID, TX_ID, FUNC_ID, PAD,
    IsoTpEcu, EcuProfile, EcuResponder, Gs19Responder, Simulator,
    profile, profile_names, make_responder, gs19_profile, generic_profile,
    _load_image,
)
