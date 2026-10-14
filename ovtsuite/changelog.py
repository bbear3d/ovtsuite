# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a binary's change log lives -- the record of edits, applied patches
(``PATCH`` records, :mod:`ovtsuite.patch`) and automatic version checkpoints
(``VERSION`` records, :mod:`ovtsuite.history`).

* A native container (``.edc16``, ``.edc15``, ``.6hp``, ``.hvac``) carries it INSIDE the file, as its own block
  (:data:`ovtsuite.container.BLOCK_LOG`): the history travels with the
  tune, a copy of the file is a copy of its history, and there is no second
  file to lose or mismatch.
* Every other file (plain ``.bin``, ``.dim``, ``.sgo``, ...) keeps the
  ``<name>.log`` text file beside it, as before.

The text is the same either way -- ``# <time>  saved <file>`` headers and
two-space-indented entries -- so every reader parses both with one code path.

Migration: an ``.edc16`` that has no log block yet but does have a ``.log``
beside it (written before the log moved inside) reads that ``.log``; its
next save folds the whole text into the container and retires the ``.log``.
"""
import os
from datetime import datetime
from typing import List, Optional

from . import container as _c


def sidecar_path(path: str) -> str:
    return os.path.splitext(path)[0] + ".log"


def embeds(path: Optional[str]) -> bool:
    """True when ``path`` is a native container (``.edc16`` / ``.edc15`` /
    ``.6hp`` / ``.hvac``) -- its log lives inside. An old-format ``.hvac``
    has no log block and is not one (it keeps a ``.log`` until its next save
    folds the text in)."""
    if not path:
        return False
    f = _c.format_for_ext(path)
    return f is not None and _c.is_container(path, f.kind)


def _block_text(container) -> Optional[str]:
    b = container.get(_c.BLOCK_LOG)
    if b is None:
        return None
    return bytes(b.data).decode("utf-8", "replace")


def read_text(path: Optional[str]) -> str:
    """The saved change-log text for the file at ``path`` ("" when none)."""
    if not path:
        return ""
    if embeds(path):
        try:
            text = _block_text(_c.Container.load(path))
        except (OSError, ValueError):
            text = None
        if text is not None:
            return text
    side = sidecar_path(path)
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8", newline="") as fh:
                return fh.read()
        except OSError:
            return ""
    return ""


def read_lines(path: Optional[str]) -> List[str]:
    text = read_text(path)
    return text.split("\n") if text else []


def format_entry(entries, saved_name: str) -> str:
    """One save's worth of log text, exactly as the ``.log`` file has it."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = ["# %s  saved %s\n" % (stamp, saved_name)]
    out.extend("  %s\n" % line for line in entries)
    return "".join(out)


def block_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def retire_sidecar(path: str) -> None:
    """After a container has taken its ``.log`` inside: remove the sidecar,
    but only once the container on disk really holds that text."""
    side = sidecar_path(path)
    if not os.path.exists(side):
        return
    try:
        with open(side, encoding="utf-8", newline="") as fh:
            old = fh.read()
        inside = _block_text(_c.Container.load(path)) or ""
        if inside.startswith(old):
            os.remove(side)
    except (OSError, ValueError):
        pass                            # best effort: a leftover .log is harmless


def rewrite(path: str, text: str, durable: bool = True) -> None:
    """Replace the saved log text of the file at ``path`` (renaming or
    merging checkpoints). For a container only its log block changes -- the
    file's other blocks are carried over byte for byte from disk, never
    from an editor's unsaved state -- and the write is atomic.
    ``durable=False``: a scratch file (a project tab's working file) --
    written uncompressed and not flushed to the platter."""
    if embeds(path):
        c = _c.Container.load(path)
        c.add(_c.BLOCK_LOG, block_bytes(text))
        _atomic_write(path, c.to_bytes(compress=durable), durable)
        retire_sidecar(path)
        return
    _atomic_write(sidecar_path(path), text.encode("utf-8"), durable)


def _atomic_write(path: str, data: bytes, durable: bool = True) -> None:
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)) or ".",
                       ".%s.tmp" % os.path.basename(path))
    with open(tmp, "wb") as fh:
        fh.write(data)
        if durable:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, path)
