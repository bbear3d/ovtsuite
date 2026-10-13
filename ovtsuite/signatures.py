# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Moved to :mod:`ovtsuite.edc16.signatures` (EDC16 code-signature
constants are part of the Bosch EDC16 module).

This name stays importable: it IS that module.
"""
import sys

from .edc16 import signatures as _moved

sys.modules[__name__] = _moved
