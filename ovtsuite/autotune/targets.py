# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Moved to :mod:`ovtsuite.zf6hp.tuning.targets6hp` (shift-map targets are ZF 6HP knowledge; the plan/validate framework here is shared).

This name stays importable: it IS that module.
"""
import sys

from ..zf6hp.tuning import targets6hp as _moved

sys.modules[__name__] = _moved
