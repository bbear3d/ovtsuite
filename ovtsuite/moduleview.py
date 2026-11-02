# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The session as a module's hooks see it.

A definition file loaded beside a module's own tables (a WinOLS project's
maps, an XDF, an A2L: :func:`ovtsuite.xdf_session.add_definition`) puts its
entities in the same ``session.entities`` list, ungrouped and marked
``source == "definition"``. The module's datasets (EDC15: Automatic, Manual 1,
Manual 2) are grouped; the definition's tables are not, and usually name only
some of the tables a calculation needs. A hook that builds one model per group
of ``session.entities`` -- End of injection, the dyno chart, fault codes,
measuring blocks -- then found an extra, incomplete dataset (the empty group's
name, "EDC15"), listed it first and reported tables missing that the module's
own datasets have.

So the hooks that read entities per dataset (:data:`ENTITY_HOOKS`) are given a
:class:`ModuleView` of the session: the same session, with ``entities``
holding what the module defines. The definition's entities are left out
whenever the module has any of its own; a binary described by a definition
file alone (the module defines nothing) keeps them. Every other attribute and
method is the session's, so a hook edits through the real session.
"""

from __future__ import annotations

from typing import List

#: ``source`` of an entity that came from a definition file
DEFINITION_SOURCE = "definition"

#: Hooks of :class:`ovtsuite.modules.api.ModuleSpec` that take the session and
#: build per-dataset models from its entities
ENTITY_HOOKS = ("injection", "dyno", "faults", "msblock_layout")


def own_entities(entities) -> List[object]:
    """``entities`` without a definition file's, unless nothing else is left."""
    ents = list(entities or [])
    own = [e for e in ents if getattr(e, "source", "") != DEFINITION_SOURCE]
    return own or ents


class ModuleView:
    """``session`` with ``entities`` = :func:`own_entities` (see the module
    docstring). Reads and writes of anything else go to the session."""

    def __init__(self, session):
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_key", None)
        object.__setattr__(self, "_own", [])

    @property
    def entities(self) -> List[object]:
        ents = self._session.entities
        key = (id(ents), len(ents))
        if key != self._key:                  # replaced or grown: refilter
            object.__setattr__(self, "_own", own_entities(ents))
            object.__setattr__(self, "_key", key)
        return self._own

    @entities.setter
    def entities(self, value) -> None:
        self._session.entities = value

    def __getattr__(self, name):
        return getattr(self._session, name)

    def __setattr__(self, name, value) -> None:
        setattr(self._session, name, value)

    def __repr__(self) -> str:
        return "<module view of %r>" % (self._session,)


def viewed(session):
    """``session`` as a module's hook should get it (anything that has no
    entity list, or is a view already, is returned as it is)."""
    if session is None or isinstance(session, ModuleView) \
            or not isinstance(getattr(session, "entities", None), list):
        return session
    return ModuleView(session)
