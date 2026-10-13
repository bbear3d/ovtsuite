# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Host for the ECU flasher when it runs on its own (``ovtsuite --flasher``).

FlashDialog was written against the main window: it reads ``primary`` (the file
to write) and ``_eeprom`` (a held EDC15 EEPROM), and hands finished reads back
with ``load_from_image`` / ``load_backup``. This object provides that same small
surface without a main window, so the dialog needs no second code path for the
flashing itself:

* the file to write is chosen in the dialog (``open_path``) rather than borrowed
  from whatever tab happens to be open;
* a finished read cannot open in a tab, so it is saved to a file instead.
"""

import datetime
import os
import tempfile

from PySide6 import QtWidgets

from . import backup15 as _backup
from .. import branding as _branding
from .. import recent as _recent


# Session platform -> the FlashDialog "ECU" entry that flashes it. A platform
# missing here (unknown, or one with no flasher) leaves the saved choice alone.
PLATFORM_TO_ECU = {
    "edc16": "EDC16",
    "edc15": "EDC15",
    "zf6hp": "ZF6HP",
    "hvac": "Climatronic",
    "vdo": "Cluster (VDO K-line)",
}


def ecu_for_platform(platform):
    """The flasher module for a session platform, or None when there is none."""
    return PLATFORM_TO_ECU.get(platform or "")


class StandaloneFlashHost:
    """Stands in for the main window as FlashDialog's ``window``."""

    standalone = True
    dialog_parent = None        # a top-level window of its own, not owned by another

    def __init__(self):
        self.primary = None
        self._eeprom = None
        #: (project path, set id, branch id, version id) when the file to
        #: write came out of an .ovt project -- marked as flashed after a write
        self._project_src = None

    # -- the file to write ---------------------------------------------------
    def open_project(self, path, parent=None):
        """An .ovt: pick which of its binaries (and which version) to write,
        when there is a choice."""
        from .. import gui_ovt_file as _of
        got = _of.open_version(path, parent, title="Write from a project",
                               text="Which binary of the project, and which "
                                    "version, to write?")
        if got is None:
            return False
        session = got.session
        eeprom = got.state.get("eeprom")
        if eeprom is None and session.has_eeprom():
            eeprom = dict(session.region_buffers()).get(session.EEPROM_REGION)
        self.primary = session
        self._eeprom = bytes(eeprom) if eeprom is not None else None
        self._project_src = (got.path, got.sid, got.bid, got.cid)
        _recent.set_last_dir(os.path.dirname(got.path))
        return True

    def open_path(self, path, parent=None):
        """Load ``path`` as the file to write. True when it loaded; on failure
        the reason is shown and whatever was loaded before stays loaded."""
        from ..session import TunerSession
        from ..project import is_project
        if is_project(path):
            return self.open_project(path, parent)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as ex:
            QtWidgets.QMessageBox.critical(parent, "Open file", str(ex))
            return False
        eeprom = None
        try:
            if _backup.is_backup(data[:8]):
                # A full EDC15 backup is flash + EEPROM: the flash is what gets
                # parsed as a session, the EEPROM is held for "Write flash + EEPROM".
                flash, eeprom, _variant = _backup.parse(data)
                fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="ovt-flasher-")
                os.close(fd)
                try:
                    with open(tmp, "wb") as fh:
                        fh.write(flash)
                    session = TunerSession(tmp)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                session.path = path
                session.binfile.name = os.path.basename(path)
            else:
                session = TunerSession(path)
                if session.has_eeprom():
                    # a native .edc15 carries its EEPROM: the same "Write
                    # flash + EEPROM" as a backup
                    eeprom = dict(session.region_buffers()).get(
                        session.EEPROM_REGION)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(parent, "Open file",
                                           "%s could not be loaded:\n%s"
                                           % (os.path.basename(path), ex))
            return False
        self.primary = session
        self._eeprom = bytes(eeprom) if eeprom is not None else None
        self._project_src = None
        _recent.set_last_dir(os.path.dirname(os.path.abspath(path)))
        return True

    def flash_written(self, what=""):
        """After a successful write from a project: mark that version in the
        .ovt as the last flashed one -- unless the main app has the project
        open (its own save would overwrite the mark; it is skipped, and
        said)."""
        if not self._project_src:
            return False
        from .. import gui_ovt_file as _of
        path, sid, bid, cid = self._project_src
        tool = "%s: %s" % (_branding.TOOL_NAMES["flasher"], what)
        return bool(_of.update_project(
            path, lambda p: p.mark_flashed(sid, bid, cid, tool=tool),
            title="Flashed",
            what="the \"last flashed\" mark (the write itself is done)"))

    def before_flash_write(self):
        return True

    def load_eeprom_read(self, data, name):
        """An EEPROM read: saved like any read (an .ovt by default)."""
        return self.load_from_image(data, name)

    # -- what the dialog hands back -------------------------------------------
    def load_from_image(self, data, name, parent=None, eeprom=None):
        """A finished read. There is no tab to open it in, so ask where to
        put it: an .ovt project by default (the read is its original), or the
        raw image as read. Returns True when it was saved."""
        from ..project import Project, import_read
        from .. import container as _cont
        filters = ["OVT project (*.ovt)", "Raw image as read (*.bin)",
                   "All files (*)"]
        default = os.path.join(_recent.get_last_dir(), name + ".ovt")
        path, chosen = QtWidgets.QFileDialog.getSaveFileName(
            parent, "Save the read", default, ";;".join(filters))
        if not path:
            return False
        if not os.path.splitext(path)[1]:
            path += ".bin" if chosen == filters[1] else ".ovt"
        try:
            if path.lower().endswith(".ovt"):
                proj = Project(name)
                import_read(proj, data, name, eeprom=eeprom,
                            source={"tool": "OVT Flasher"})
                proj.save(path)
            else:
                raw = bytes(data)
                if _cont.format_for_magic(raw[:8]) is not None:
                    raw = _cont.Container.from_bytes(raw).external() or raw
                with open(path, "wb") as fh:
                    fh.write(raw)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(parent, "Save the read",
                                           "%s could not be saved:\n%s"
                                           % (os.path.basename(path), ex))
            return False
        _recent.set_last_dir(os.path.dirname(path))
        return True

    def load_backup(self, flash, eeprom, path=None, name=None):
        """A full backup (flash + EEPROM): saved like any read, as one
        project."""
        return self.load_from_image(
            flash, name or "ECU-backup-%s"
            % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), eeprom=eeprom)
