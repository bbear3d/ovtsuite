# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag definitions, format v1 (claude/ovt_diag_definitions_schema.md).

A *pack* is a directory of YAML/JSON documents (or its compiled
``pack.json.gz``) that says what a module's bytes mean and how to reach it:
addressing, identification, fault texts, measuring names, coding bits,
adaptation channels, actuator tests, basic settings, DIDs/routines, guided
procedures. The app bundles ``vag-generic``; platform modules may carry
their own; the user keeps local packs beside the app's settings.

Public surface::

    packs = load_all()                      # bundled + modules + user, in layering order
    res = Resolver(packs)
    ident = Identity(addr="01", protocol="kwp2000", part="03G906016LQ", sw="8449")
    res.module(ident)["variants"]           # how this module does adaptation/coding/...
    res.fault_text(ident, vag=16785)
    res.addressing("C6")                    # the module tree for a vehicle generation
    validate_pack(pack); compile_pack(dir)  # the contributor's tools
"""
from .compile import catalogue_rows, compile_pack
from .install import InstallError, install_pack, installed_user_packs, remove_pack
from .load import (bundled_dir, is_pack_dir, load_all, load_pack, load_packs, module_dirs,
                   user_dir)
from .model import FORMAT, KINDS, Doc, Identity, Match, Pack, is_entry_id, part_matches
from .resolve import Resolved, Resolver, View
from .schema import as_int, check_doc, json_schema
from .validate import Report, check_conflicts, validate_pack
from .yamlio import HAVE_YAML, DefsError

__all__ = ["FORMAT", "KINDS", "Doc", "Identity", "Match", "Pack", "Resolved", "Resolver", "View",
           "Report", "DefsError", "HAVE_YAML", "InstallError", "install_pack", "installed_user_packs", "remove_pack", "as_int", "bundled_dir", "catalogue_rows",
           "check_conflicts", "check_doc", "compile_pack", "is_entry_id", "is_pack_dir",
           "json_schema", "load_all", "load_pack", "load_packs", "module_dirs", "part_matches",
           "user_dir", "validate_pack"]
