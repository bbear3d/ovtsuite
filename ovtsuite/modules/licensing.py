# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Per-module licensing: the hook, not yet a policy.

Every module manifest carries a ``license`` object (``{"required": false}``
today). Installing and loading a module call :func:`check`; a module that
says it needs a licence is refused with a clear reason until a licensing
scheme exists, so turning one on later is a change here, not a format change.
"""

from typing import Tuple


def check(manifest) -> Tuple[bool, str]:
    lic = getattr(manifest, "license", None) or {}
    if not lic.get("required"):
        return True, ""
    return False, ("the %s module needs a licence, and this version of the "
                   "app cannot check one yet" % getattr(manifest, "name", "?"))
