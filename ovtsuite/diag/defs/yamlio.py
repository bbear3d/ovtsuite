# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Reading definition documents: YAML when PyYAML is installed, JSON always.

Packs are authored in YAML (comments, less punctuation) and compiled to JSON
for shipping (:mod:`.compile`), so the frozen app works without PyYAML: a
bundled or compiled pack never needs it, only a loose YAML pack in the
user's definitions folder does. ``safe_load`` only -- a definition is data,
never an object graph.
"""
import json
import os
from typing import Any

try:
    import yaml as _yaml
    HAVE_YAML = True
except Exception:          # pragma: no cover - depends on the environment
    _yaml = None
    HAVE_YAML = False

YAML_EXTS = (".yaml", ".yml")
DOC_EXTS = YAML_EXTS + (".json",)


class DefsError(Exception):
    """A definition file that cannot be read or does not fit the format."""


def is_doc(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in DOC_EXTS


def read(path: str) -> Any:
    """Parse one document. YAML needs PyYAML; JSON never does."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return parse(text, ext, path)


def parse(text: str, ext: str = ".yaml", name: str = "<text>") -> Any:
    if ext in YAML_EXTS:
        if not HAVE_YAML:
            raise DefsError("%s: YAML packs need PyYAML (pip install pyyaml); "
                            "compiled .json packs load without it" % name)
        try:
            return _yaml.safe_load(text)
        except Exception as ex:
            raise DefsError("%s: %s" % (name, ex)) from ex
    try:
        return json.loads(text)
    except Exception as ex:
        raise DefsError("%s: %s" % (name, ex)) from ex


def dump_yaml(obj: Any) -> str:
    """YAML text for ``obj`` (the recorder writes the user's local pack this
    way). JSON when PyYAML is absent -- a JSON file is a valid document too."""
    if HAVE_YAML:
        return _yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
