# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Moved to :mod:`ovtsuite.edc15.eeprom_gui` (the EDC15 EEPROM editor is part of the Bosch EDC15 module).

This name stays importable: it IS that module.
"""
import sys

from .edc15 import eeprom_gui as _moved

sys.modules[__name__] = _moved
