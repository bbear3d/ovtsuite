# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Help > Check for Updates..., the start-up check, and installing an update.

The check runs on a worker thread (never blocking the window); the start-up
check waits a few seconds, runs at most once a day, stays silent when the
network is down and leaves out a release the user chose to skip. The manual
check does neither.

Installing (Windows, a copy installed by its setup.exe, and only when the
user clicks **Download and install**): the files are downloaded and verified
(:mod:`ovtsuite.updates`); then every window is closed the normal way, so
unsaved work is asked about -- Cancel there, or a save that fails, stops the
update and keeps the downloads for next time. Only then are module packages
installed in the app, the installers started (by a small batch file that
waits for this process to exit) and the app quits. Every other copy (Linux,
a source checkout) is told where the release is.
"""

import os
import sys
import threading
import time
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from . import updates as _up

STARTUP_DELAY_MS = 6000
ENV_OFF = branding.ENV_PREFIX + "_NO_UPDATE_CHECK"


class _Relay(QtCore.QObject):
    """Carries a worker thread's result to the GUI thread (a queued signal:
    the relay lives in the GUI thread)."""
    finished = QtCore.Signal(object, object)
    progress = QtCore.Signal(int, int)


_busy = False
_keep = []                      # relays and threads alive until they report


def _settings():
    from .appsettings import settings
    return settings()


def _alive(w) -> Optional[QtWidgets.QWidget]:
    try:
        return w if w is not None and w.isVisible() else None
    except RuntimeError:            # deleted
        return None


# ------------------------------------------------------------------ checking
def schedule_startup_check(win) -> None:
    """Called once the main window is up: the automatic check, if it is on
    and due, a few seconds later."""
    try:
        _up.cleanup_downloads(branding.__version__)
    except Exception:
        pass
    if os.environ.get(ENV_OFF, "") not in ("", "0"):
        return
    from . import options
    try:
        if not options.get(_up.OPTION_KEY):
            return
        if not _up.check_due(_settings().value(_up.LAST_CHECK_KEY)):
            return
    except Exception:
        return
    QtCore.QTimer.singleShot(STARTUP_DELAY_MS, lambda: check(win, manual=False))


def check_now(win) -> None:
    """Help > Check for Updates..."""
    check(win, manual=True)


def check(win, manual: bool) -> None:
    global _busy
    if _busy:
        if manual:
            QtWidgets.QMessageBox.information(
                win, "Check for updates", "A check for updates is already running.")
        return
    try:
        installed = _up.installed_modules()
    except Exception:
        installed = []
    _busy = True
    try:
        _settings().setValue(_up.LAST_CHECK_KEY, time.time())
    except Exception:
        pass
    relay = _Relay()
    _keep.append(relay)
    if manual:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)

    def work():
        try:
            rel = _up.fetch_latest()
            relay.finished.emit(_up.make_offer(rel, branding.__version__, installed), None)
        except _up.UpdateError as ex:
            relay.finished.emit(None, str(ex))
        except Exception as ex:          # noqa: BLE001 - reported, never fatal
            relay.finished.emit(None, "unexpected error: %s" % ex)

    def done(offer, error):
        global _busy
        _busy = False
        if relay in _keep:
            _keep.remove(relay)
        if manual:
            QtWidgets.QApplication.restoreOverrideCursor()
        _report(win, manual, offer, error)

    relay.finished.connect(done, QtCore.Qt.ConnectionType.QueuedConnection)
    threading.Thread(target=work, name="update-check", daemon=True).start()


def _report(win, manual, offer, error, tries=0):
    parent = _alive(win)
    if error:
        if manual:
            QtWidgets.QMessageBox.warning(
                parent, "Check for updates",
                "Could not check for updates: %s.\n\nThe releases are also at\n%s"
                % (error.rstrip("."), _up.releases_page()))
        return
    skipped = "" if manual else str(_settings().value(_up.SKIP_KEY, "") or "")
    if not _up.should_show(offer, manual, skipped):
        if manual:
            QtWidgets.QMessageBox.information(
                parent, "Check for updates",
                "%s %s is the latest version.%s" % (
                    branding.APP_NAME, branding.__version__,
                    "" if offer is None or offer.release.usable else
                    " (The newest release is a pre-release; it is not offered.)"))
        return
    # the automatic check never pops up over another dialog: try again later
    if not manual and QtWidgets.QApplication.activeModalWidget() is not None and tries < 20:
        QtCore.QTimer.singleShot(15000, lambda: _report(win, manual, offer, error, tries + 1))
        return
    show_offer(parent, offer)


# ------------------------------------------------------------------ the dialog
class UpdateDialog(QtWidgets.QDialog):
    """What a newer release brings: the app's and modules' new versions, the
    release notes, and what to do about it. ``choice`` is ``"install"``,
    ``"skip"``, ``"later"`` (also Escape) after it closes."""

    def __init__(self, offer, kind: str, parent=None):
        super().__init__(parent)
        self.offer = offer
        self.kind = kind
        self.choice = "later"
        rel = offer.release
        self.setWindowTitle(branding.title("Update available"))
        self.resize(680, 560)
        v = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QLabel()
        head.setTextFormat(QtCore.Qt.TextFormat.RichText)
        if offer.base:
            head.setText("<b>%s %s is available.</b> You have %s."
                         % (branding.APP_NAME, rel.version, branding.__version__))
        else:
            head.setText("<b>Module updates are available</b> in %s %s."
                         % (branding.APP_NAME, rel.version))
        head.setWordWrap(True)
        v.addWidget(head)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Update", "Installed", "New", "Installed by"))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for row in self.rows():
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, text in enumerate(row):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.table.setMaximumHeight(self.table.verticalHeader().length()
                                    + hh.height() + 6)
        v.addWidget(self.table)

        v.addWidget(QtWidgets.QLabel("What's new:"))
        self.notes = QtWidgets.QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(rel.body or "_No release notes._")
        v.addWidget(self.notes, 1)

        self.note = QtWidgets.QLabel(self._note_text())
        self.note.setWordWrap(True)
        v.addWidget(self.note)

        bb = QtWidgets.QDialogButtonBox()
        self.install_btn = None
        if self.can_install():
            self.install_btn = bb.addButton("Download and install",
                                            QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            self.install_btn.clicked.connect(lambda: self._done("install"))
            self.install_btn.setDefault(True)
        page = bb.addButton("Open release page", QtWidgets.QDialogButtonBox.ButtonRole.HelpRole)
        page.clicked.connect(self.open_page)
        skip = bb.addButton("Skip this version",
                            QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
        skip.clicked.connect(lambda: self._done("skip"))
        later = bb.addButton("Remind me later", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        later.clicked.connect(lambda: self._done("later"))
        v.addWidget(bb)

    def rows(self) -> List[Tuple[str, str, str, str]]:
        o = self.offer
        out = []
        how = {"package": "module package", "setup": "module installer", "": "-"}
        if o.base:
            base = _up.base_asset(o.release)
            out.append((branding.APP_NAME, o.current, o.release.version,
                        "app installer" if base is not None else "-"))
        for u in o.modules:
            out.append(("%s module%s" % (u.mod.name, " (checksum only)"
                                         if u.mod.variant == "checksum" else ""),
                        u.mod.version, u.version, how[u.method]))
        if self.kind != "inno":
            out = [r[:3] + ("-",) for r in out]
        return out

    def can_install(self) -> bool:
        if self.kind != "inno":
            return False
        if self.offer.base and _up.base_asset(self.offer.release) is None:
            return False
        return self.offer.base or any(u.method for u in self.offer.modules)

    def _note_text(self) -> str:
        if self.kind == "source":
            return ("You are running %s from source or a Python install: update "
                    "it the way you installed it (git pull, pip install -U)."
                    % branding.APP_NAME)
        if self.kind != "inno":
            return ("Download the new version from the release page and install "
                    "it as before (on Linux: apt install or dnf install the new "
                    "package).")
        if not self.can_install():
            return ("This release has no installer for this copy; download it "
                    "from the release page.")
        if not self.offer.base and all(u.method in ("package", "")
                                       for u in self.offer.modules):
            return ("Download and install fetches the module packages, checks "
                    "them and installs them; restart %s to use them."
                    % branding.APP_NAME)
        return ("Download and install fetches the files, checks them, then "
                "closes %s -- asking about unsaved work first -- installs the "
                "update and starts %s again." % (branding.APP_NAME, branding.APP_NAME))

    def open_page(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.offer.release.html_url))

    def _done(self, choice):
        self.choice = choice
        if choice == "later":
            self.reject()
        else:
            self.accept()


def show_offer(parent, offer) -> None:
    kind = _up.install_kind()
    dlg = UpdateDialog(offer, kind, parent)
    dlg.exec()
    if dlg.choice == "skip":
        _settings().setValue(_up.SKIP_KEY, offer.key)
    elif dlg.choice == "install":
        install(parent, offer)


# ------------------------------------------------------------------ install
def _plan(offer):
    """``(base asset or None, [(ModuleUpdate, asset)])``."""
    base = _up.base_asset(offer.release) if offer.base else None
    mods = []
    for u in offer.modules:
        m = u.method
        if m:
            mods.append((u, u.package if m == "package" else u.setup))
    return base, mods


def _download(parent, offer, assets) -> Optional[List[Tuple[object, str, str]]]:
    """Download and verify ``assets`` with a progress dialog: ``[(asset,
    path, how verified)]``, or None (cancelled or failed; said so)."""
    total = sum(max(a.size, 1) for a in assets)
    scale = max(1, total // 1000000)            # QProgressBar holds an int
    dlg = QtWidgets.QProgressDialog("Downloading the update...", "Cancel", 0,
                                    max(1, total // scale), parent)
    dlg.setWindowTitle(branding.title("Downloading update"))
    dlg.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    relay = _Relay()
    stop = threading.Event()
    loop = QtCore.QEventLoop()
    box = {}
    folder = _up.download_dir(offer.release.version)

    def work():
        try:
            sums = _up.load_checksums(offer.release)
            got, out = 0, []
            for a in assets:
                if stop.is_set():
                    raise _up.UpdateError("cancelled")
                path, how = _up.download(
                    a, folder, sums,
                    progress=lambda n, base=got: relay.progress.emit(
                        (base + n) // scale, assets.index(a)),
                    cancelled=stop.is_set)
                got += max(a.size, 1)
                out.append((a, path, how))
            relay.finished.emit((out, sums), None)
        except _up.UpdateError as ex:
            relay.finished.emit(None, str(ex))
        except Exception as ex:                  # noqa: BLE001
            relay.finished.emit(None, "unexpected error: %s" % ex)

    def progress(value, idx):
        dlg.setValue(min(value, dlg.maximum()))
        dlg.setLabelText("Downloading %s (%d of %d)..."
                         % (assets[idx].name, idx + 1, len(assets)))

    def finished(result, error):
        box["r"], box["e"] = result, error
        loop.quit()

    relay.progress.connect(progress, QtCore.Qt.ConnectionType.QueuedConnection)
    relay.finished.connect(finished, QtCore.Qt.ConnectionType.QueuedConnection)
    dlg.canceled.connect(stop.set)
    dlg.show()
    threading.Thread(target=work, name="update-download", daemon=True).start()
    loop.exec()
    dlg.close()
    if box.get("e"):
        if box["e"] != "cancelled":
            QtWidgets.QMessageBox.warning(
                parent, "Update", "The update was not installed: %s." % box["e"].rstrip("."))
        return None
    out, sums = box["r"]
    loose = [a.name for a, _p, how in out if not how]
    if loose:
        why = sums.note or "no checksum was published"
        if QtWidgets.QMessageBox.warning(
                parent, "Update",
                "These files could not be checked against a published "
                "checksum (%s):\n\n%s\n\nTheir size is right, and a module "
                "package is still checked against its own signature. Install "
                "them anyway?" % (why, "\n".join(loose)),
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return None
    return out


def _close_everything() -> bool:
    """Close every window of this process the normal way (each asks about
    its unsaved work). False -- and nothing more is closed -- as soon as one
    refuses."""
    app = QtWidgets.QApplication.instance()
    try:
        from .gui import MainWindow
    except Exception:
        MainWindow = ()                         # noqa: N806
    tops = [w for w in app.topLevelWidgets() if w.isVisible()]
    mains = [w for w in tops if MainWindow and isinstance(w, MainWindow)]
    for w in mains + [w for w in tops if w not in mains]:
        try:
            if w.isVisible() and not w.close():
                return False
        except RuntimeError:                    # closed with its parent
            continue
    return True


def install(parent, offer) -> bool:
    """Download, verify and install ``offer``. True when the app is now
    quitting for the installers."""
    base, mods = _plan(offer)
    assets = ([base] if base is not None else []) + [a for _u, a in mods]
    if not assets:
        return False
    got = _download(parent, offer, assets)
    if got is None:
        return False
    paths = {a.name: p for a, p, _h in got}
    packages = [(u, paths[a.name]) for u, a in mods if u.method == "package"]
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    installers = ([(paths[base.name], _up.install_mode(app_dir))] if base is not None
                  else []) + \
        [(paths[a.name], _up.install_mode(u.mod.root or app_dir))
         for u, a in mods if u.method == "setup"]
    folder = _up.download_dir(offer.release.version)

    if not installers:
        # module packages only: installed now, used after a restart
        errors = _install_packages(packages)
        _say_packages(parent, packages, errors, restart=True)
        return False

    ask = QtWidgets.QMessageBox(parent)
    ask.setIcon(QtWidgets.QMessageBox.Icon.Question)
    ask.setWindowTitle(branding.title("Install update"))
    ask.setText("Install the update now? %s will close." % branding.APP_NAME)
    ask.setInformativeText(
        "You are asked about any unsaved work first. When the installer has "
        "finished, %s starts again." % branding.APP_NAME)
    now = ask.addButton("Install now", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    ask.addButton("Later", QtWidgets.QMessageBox.ButtonRole.RejectRole)
    ask.exec()
    if ask.clickedButton() is not now:
        _kept(parent, folder)
        return False
    others = _up.other_instances()
    if others:
        QtWidgets.QMessageBox.warning(
            parent, "Install update",
            "Other copies of %s or its tools are running:\n\n%s\n\nClose them, "
            "then choose Help > Check for Updates again; the downloaded files "
            "are kept." % (branding.APP_NAME, "\n".join(
                "%s (process %d)" % o for o in others)))
        return False

    app = QtWidgets.QApplication.instance()
    quit_last = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)    # closing windows must not end us yet
    if not _close_everything():
        app.setQuitOnLastWindowClosed(quit_last)
        _kept(_alive(parent), folder, cancelled=True)
        return False
    errors = _install_packages(packages)
    if errors:
        _say_packages(None, packages, errors, restart=False)
    try:
        _up.launch_chain(installers, folder)
    except OSError as ex:
        QtWidgets.QMessageBox.critical(
            None, "Install update", "Could not start the installer: %s\n\nRun it "
            "yourself from\n%s" % (ex, folder))
        app.setQuitOnLastWindowClosed(quit_last)
        app.quit()
        return False
    app.quit()
    return True


def _install_packages(packages) -> List[str]:
    """Install module packages for this user (each is checked first:
    signature, every file). The errors, one line each."""
    if not packages:
        return []
    from .modules import installer as _inst
    from .modules import package as _pkg
    errors = []
    for u, path in packages:
        try:
            _inst.install(path, scope=_inst.USER)
        except (OSError, _inst.InstallError, _pkg.PackageError) as ex:
            errors.append("%s module: %s" % (u.mod.name, ex))
    return errors


def _say_packages(parent, packages, errors, restart):
    if errors:
        QtWidgets.QMessageBox.warning(
            parent, "Update", "Not every module was updated:\n\n%s" % "\n".join(errors))
    if restart and len(errors) < len(packages):
        QtWidgets.QMessageBox.information(
            parent, "Update", "The module updates are installed. Restart %s to "
            "use them -- the versions already loaded stay in use until then."
            % branding.APP_NAME)


def _kept(parent, folder, cancelled=False):
    QtWidgets.QMessageBox.information(
        parent, "Update",
        "%sThe downloaded files are kept in\n%s\n\nHelp > Check for Updates "
        "installs them without downloading them again."
        % ("The update was cancelled; nothing was installed.\n\n" if cancelled else "",
           folder))
