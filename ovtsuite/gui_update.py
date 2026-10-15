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
a source checkout, a standalone tool's own install) is told where the release
is.

When the releases between this version and the new one each carry a delta
package (:mod:`ovtsuite.delta_update`), a per-user install downloads that
chain instead of the full installer -- usually a few MB -- checks it against
the installed files before anything closes, and has a helper apply it after
the app has quit. Anything that does not check out falls back to the full
installer; a failed apply is rolled back and reported at the next start.
"""

import os
import sys
import threading
import time
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from . import delta_update as _du
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
    try:
        report_last_update(win)
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
    kind = _up.install_kind()

    def work():
        try:
            rel = _up.fetch_latest()
            offer = _up.make_offer(rel, branding.__version__, installed)
            if offer.base and kind == "inno":
                offer.delta = find_delta(rel)
            relay.finished.emit(offer, None)
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


def find_delta(rel):
    """The chain of delta packages from this version to ``rel``, or None --
    never an error: the full installer is always there instead."""
    try:
        rels = _up.fetch_releases(older_than=branding.__version__)
        if not any(r.tag == rel.tag for r in rels):
            rels.append(rel)
        return _du.find_chain(rels, branding.__version__, rel.version)
    except Exception:                       # noqa: BLE001
        return None


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def delta_for(offer):
    """The delta chain this install will use for ``offer``, or None for the
    full installer (see :func:`delta_update.choose`)."""
    chain = getattr(offer, "delta", None)
    if chain is None or not offer.base:
        return None
    try:
        failed = str(_settings().value(_up.DELTA_FAILED_KEY, "") or "")
    except Exception:                       # noqa: BLE001
        failed = ""
    base = _up.base_asset(offer.release)
    per_user = _up.install_mode(_app_dir()) == _up.CURRENT_USER
    return _du.choose(chain, base.size if base is not None else 0, per_user,
                      failed, offer.release.version)


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1048576.0)


def report_last_update(win) -> None:
    """At start-up: what the last delta update did (its helper left a
    result). Success shows in the status bar; a failure -- the old version
    was restored -- is said, and that release is installed with the full
    installer next time."""
    r = _du.pop_result()
    try:
        _du.cleanup_after_update(_app_dir())
    except Exception:                       # noqa: BLE001
        pass
    if not r:
        return
    if r.get("ok"):
        try:
            win.statusBar().showMessage(str(r.get("message") or "Update installed."), 20000)
        except Exception:                   # noqa: BLE001
            pass
        return
    try:
        _settings().setValue(_up.DELTA_FAILED_KEY, str(r.get("to") or ""))
    except Exception:                       # noqa: BLE001
        pass
    msg = ("The update to %s %s could not be installed: %s.\n\nHelp > Check for "
           "Updates installs it with the full installer instead.%s"
           % (branding.APP_NAME, r.get("to") or "", str(r.get("message") or "").rstrip("."),
              "\n\nDetails: %s" % r["log"] if r.get("log") else ""))
    QtCore.QTimer.singleShot(0, lambda: QtWidgets.QMessageBox.warning(
        _alive(win), "Update", msg))


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
        self.delta = delta_for(offer) if kind == "inno" else None
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
            by = "app installer" if base is not None else "-"
            if self.delta is not None:
                n = self.delta.steps
                by = "update package%s: %d step%s, %s" % (
                    "s" if n > 1 else "", n, "s" if n > 1 else "", _mb(self.delta.size))
            out.append((branding.APP_NAME, o.current, o.release.version, by))
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
        if (self.offer.base and _up.base_asset(self.offer.release) is None
                and self.delta is None):
            return False
        return self.offer.base or any(u.method for u in self.offer.modules)

    def _note_text(self) -> str:
        if self.kind == "source":
            return ("You are running %s from source or a Python install: update "
                    "it the way you installed it (git pull, pip install -U)."
                    % branding.APP_NAME)
        if self.kind == "tools":
            return ("This is a standalone tool's own install: download its new "
                    "setup program from the release page and run it.")
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
        if self.delta is not None:
            base = _up.base_asset(self.offer.release)
            return ("Download and install fetches %s (%s%s), checks them "
                    "against the files you have, then closes %s -- asking about "
                    "unsaved work first -- updates it and starts it again."
                    % ("the update package" if self.delta.steps == 1 else
                       "%d update packages" % self.delta.steps, _mb(self.delta.size),
                       " instead of the %s installer" % _mb(base.size) if base is not None
                       else "", branding.APP_NAME))
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


def _download(parent, offer, assets, owners=None, strict=False,
              quiet=False) -> Optional[List[Tuple[object, str, str]]]:
    """Download and verify ``assets`` with a progress dialog: ``[(asset,
    path, how verified)]``, or None (cancelled or failed; said so unless
    ``quiet``). ``owners``: asset name -> the release whose checksum list
    vouches for it (default: the offer's). ``strict``: only a signed
    checksum list will do."""
    global last_error
    last_error = ""
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
            cache = {}

            def sums_of(rel):
                if rel.tag not in cache:
                    cache[rel.tag] = _up.load_checksums(rel)
                return cache[rel.tag]
            sums = sums_of(offer.release)
            got, out = 0, []
            for a in assets:
                if stop.is_set():
                    raise _up.UpdateError("cancelled")
                path, how = _up.download(
                    a, folder, sums_of((owners or {}).get(a.name, offer.release)),
                    progress=lambda n, base=got: relay.progress.emit(
                        (base + n) // scale, assets.index(a)),
                    cancelled=stop.is_set)
                if strict and how != "signed checksum list":
                    raise _up.UpdateError("%s is not in a signed checksum list" % a.name)
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
        last_error = box["e"]
        if box["e"] != "cancelled" and not quiet:
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


last_error = ""
#: why the last delta update fell back to the full installer ("" = it did not)
last_fallback = ""


def _fetch_delta(parent, offer, chain):
    """Download the chain and check it against this install, while the app
    is still open: the folded change (delta_update.Composite), None to fall
    back to the full installer, or False when the user cancelled."""
    global last_fallback
    hops = chain.hops
    got = _download(parent, offer, [h.asset for h in hops],
                    owners={h.asset.name: h.release for h in hops}, strict=True, quiet=True)
    if got is None:
        if last_error == "cancelled":
            return False
        last_fallback = last_error
        return None
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
    try:
        return _du.preflight(_app_dir(), [p for _a, p, _h in got], branding.__version__)
    except (_du.DeltaError, OSError) as ex:
        last_fallback = str(ex)
        return None
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()


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
    global last_fallback
    base, mods = _plan(offer)
    last_fallback = ""
    comp = None
    chain = delta_for(offer)
    if chain is not None:
        comp = _fetch_delta(parent, offer, chain)
        if comp is False:
            return False
        if comp is not None:
            base = None                 # the delta replaces the app's installer
        elif base is None:
            QtWidgets.QMessageBox.warning(
                parent, "Update", "The update packages cannot be used here (%s), "
                "and this release has no installer for this copy; download it "
                "from the release page." % last_fallback.rstrip("."))
            return False
    assets = ([base] if base is not None else []) + [a for _u, a in mods]
    if not assets and comp is None:
        return False
    got = []
    if assets:
        got = _download(parent, offer, assets)
        if got is None:
            return False
    paths = {a.name: p for a, p, _h in got}
    packages = [(u, paths[a.name]) for u, a in mods if u.method == "package"]
    app_dir = _app_dir()
    installers = ([(paths[base.name], _up.install_mode(app_dir))] if base is not None
                  else []) + \
        [(paths[a.name], _up.install_mode(u.mod.root or app_dir))
         for u, a in mods if u.method == "setup"]
    folder = _up.download_dir(offer.release.version)

    if not installers and comp is None:
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

    helper = None
    if comp is not None:
        # the helper's copy of the app and its plan, while nothing is closed
        # yet: a failure here leaves everything as it was
        try:
            then = _up.write_chain(installers, folder) if installers else ""
            helper = _du.prepare(app_dir, comp, folder, then)
        except OSError as ex:
            QtWidgets.QMessageBox.warning(
                parent, "Install update", "Could not prepare the update: %s" % ex)
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
        if helper is not None:
            _du.launch(helper)
        else:
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
