# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.ovt`` project container -- base app, no module imports.

See docs/OVT_CONTAINER.md. Typical use::

    from ovtsuite import project as ovt

    proj, ecu = ovt.import_file("03G906016LQ.sgo")      # new project
    ovt.import_file("6hp19.bin", proj)                  # second set (TCU)
    stage1 = proj.fork(ecu.id, "Stage 1")
    proj.commit(ecu.id, stage1.id, {"flash": tuned}, label="more boost")
    proj.save("A6.ovt")

    proj = ovt.Project.load("A6.ovt")
    stock = proj.state(ecu.id, ecu.roots()[0].id)       # the as-read original
"""

from . import autosave
from .archive import (EXT, FEATURES, LoadReport, ProjectError, Signature,
                      UnsupportedFeature, is_project)
from .delta import DeltaError
from .exporters import (ExportRefused, export_original, export_raw,
                        original_formats)
from .importers import (ImportRefused, import_bytes, import_file, import_read,
                        legacy_chain)
from .model import (INFO_FIELDS, KINDS, ROLES, SET_KINDS, Attachment, BinarySet,
                    Branch, Checkpoint, Member, Original)
from .project import HistoryError, Project

load = Project.load

__all__ = [
    "EXT", "FEATURES", "INFO_FIELDS", "KINDS", "ROLES", "SET_KINDS",
    "Attachment", "BinarySet", "Branch", "Checkpoint", "Member", "Original",
    "Project", "LoadReport", "Signature",
    "ProjectError", "UnsupportedFeature", "HistoryError", "DeltaError",
    "ImportRefused", "ExportRefused",
    "is_project", "load", "import_bytes", "import_file", "import_read",
    "legacy_chain",
    "export_raw", "export_original", "original_formats",
    "autosave",
]
