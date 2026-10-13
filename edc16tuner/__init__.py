# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Compatibility shim: ``edc16tuner`` is now :mod:`ovtsuite` (OVT-Suite).

The package was renamed when EDC16-Tuner became OVT-Suite. ``import edc16tuner`` keeps
working -- the name is bound to the real package object, so ``edc16tuner.__version__``
and ``edc16tuner.branding`` behave exactly as before.

``import edc16tuner.submodule`` deliberately does NOT work, and raises with the
replacement spelled out. Aliasing submodules is the obvious thing to try and it
is a trap: the import machinery re-executes ``submodule.py`` under the second
name, leaving two module objects and two copies of every class, so an ``Entity``
built through one name fails ``isinstance`` against the other -- a bug that
surfaces far from its cause. A loud error at the import line is worth more than
a silent duplicate.

Delete this directory once the release that needed it is out.
"""

import importlib
import importlib.abc
import sys
import warnings

_OLD = "edc16tuner"
_NEW = "ovtsuite"

warnings.warn("%s has been renamed to %s; update your imports" % (_OLD, _NEW),
              DeprecationWarning, stacklevel=2)


class _RenamedFinder(importlib.abc.MetaPathFinder):
    """Turn ``import edc16tuner.x`` into an error naming ``ovtsuite.x``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        raise ImportError(
            "%s was renamed to %s. Import %s instead of %s -- the old name is "
            "kept only as a package alias, because aliasing submodules would "
            "load them twice and give you two incompatible copies of every "
            "class." % (_OLD, _NEW, _NEW + fullname[len(_OLD):], fullname))


if not any(isinstance(f, _RenamedFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _RenamedFinder())

sys.modules[__name__] = importlib.import_module(_NEW)
