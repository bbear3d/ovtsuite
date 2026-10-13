# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pair this laptop with a KW-Dash head unit: type the PIN once, keep the token.

Its own module rather than part of the flash dialog because the logging dialog's
connection bar wants the same picker and the same button; one place for the
wording is one place to get it right.
"""

from PySide6 import QtCore, QtWidgets

from . import discover, headunits


class _Sweep(QtCore.QThread):
    """The scan, off the GUI thread.

    Off it because the sweep joins its workers, and a dialog that stops repainting
    for two seconds looks like one that has crashed.  Each answer is signalled as
    it arrives rather than at the end, so the list fills while the rest of the
    addresses are still being tried -- and the head unit that IS the gateway,
    which is the hot-spotting case, appears almost immediately.
    """

    found = QtCore.Signal(object)
    done = QtCore.Signal()

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self._port = int(port)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            discover.sweep(port=self._port, on_found=self.found.emit,
                           should_stop=lambda: self._stop)
        except Exception:                            # noqa: BLE001 - a scan may not take the dialog down
            pass
        self.done.emit()


class PairHeadUnitDialog(QtWidgets.QDialog):
    """Address, PIN, and a name to call it afterwards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair a head unit")
        self.unit = None

        self._sweep = None

        form = QtWidgets.QFormLayout(self)

        self.found = QtWidgets.QListWidget()
        self.found.setMaximumHeight(96)
        self.found.itemSelectionChanged.connect(self._chose)
        form.addRow("Found:", self.found)

        self.scan = QtWidgets.QPushButton("Scan")
        self.scan.clicked.connect(self._scan)
        form.addRow("", self.scan)

        self.host = QtWidgets.QLineEdit()
        self.host.setPlaceholderText("192.168.1.42")
        form.addRow("Address:", self.host)

        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(headunits.DEFAULT_PORT)
        form.addRow("Port:", self.port)

        self.pin = QtWidgets.QLineEdit()
        self.pin.setMaxLength(6)
        self.pin.setPlaceholderText("000000")
        form.addRow("PIN:", self.pin)

        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("Golf")
        form.addRow("Call it:", self.name)

        note = QtWidgets.QLabel(
            "Open Pass-through on the head unit and tap “Pair a new device”.\n"
            "It shows an address and a six-digit PIN; type both here.\n\n"
            "The PIN is needed once. It is never sent — it signs a challenge over the\n"
            "head unit's own certificate, so a laptop pretending to be it cannot pass\n"
            "yours on. After this, connecting is automatic.")
        note.setWordWrap(True)
        form.addRow(note)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        form.addRow(self.status)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._pair)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._buttons = buttons

    # ------------------------------------------------------------- scanning
    def _scan(self):
        """Start a sweep, or stop the one that is running."""
        if self._sweep is not None:
            self._sweep.stop()
            return

        self.found.clear()
        self.scan.setText("Stop")
        self.status.setText("Looking for head units on this network...")

        self._sweep = _Sweep(self.port.value(), self)
        self._sweep.found.connect(self._add)
        self._sweep.done.connect(self._scanned)
        self._sweep.start()

    def _add(self, unit):
        item = QtWidgets.QListWidgetItem(unit.label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, unit)
        self.found.addItem(item)
        if self.found.count() == 1:
            self.found.setCurrentItem(item)          # the usual case is exactly one

    def _scanned(self):
        self._sweep = None
        self.scan.setText("Scan")
        if self.found.count():
            self.status.setText(
                "Pick one, then type the PIN from its screen."
                if self.found.count() > 1 else
                "Type the PIN from its screen.")
        else:
            # Everything a scan cannot reach has the same fix, so say that rather
            # than guessing between a sleeping head unit and a hostile network.
            self.status.setText(
                "Nothing answered. Open Pass-through on the head unit, and check "
                "both machines are on the same Wi-Fi \u2014 or type the address it "
                "shows, which works either way.")

    def _chose(self):
        item = self.found.currentItem()
        if item is None:
            return
        unit = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.host.setText(unit.host)
        self.port.setValue(unit.port)
        if unit.paired:
            self.status.setText(
                "Already paired with this one. Pairing again replaces the key, "
                "which is what to do if it stopped connecting.")

    def closeEvent(self, event):
        if self._sweep is not None:
            self._sweep.stop()
            self._sweep.wait(2000)
        super().closeEvent(event)

    # ------------------------------------------------------------- pairing
    def _pair(self):
        host = self.host.text().strip()
        pin = self.pin.text().strip()

        if not host or not pin:
            self.status.setText("An address and a PIN, both from the head unit's screen.")
            return

        self._buttons.setEnabled(False)
        self.status.setText("Connecting...")
        QtWidgets.QApplication.processEvents()

        try:
            unit, client = headunits.pair(
                host, self.port.value(), pin, self.name.text().strip())
        except Exception as ex:                      # noqa: BLE001 - shown to the user
            # Wrong PIN, wrong address, head unit asleep: all the same shape to a
            # driver, and all fixed by looking at the dashboard again.
            self.status.setText(str(ex))
            self._buttons.setEnabled(True)
            return

        try:
            client.close()
        except Exception:                            # noqa: BLE001
            pass

        self.unit = unit
        self.accept()


def pair_head_unit(parent=None):
    """Run the dialog. Returns the new head unit, or None if nothing was paired."""
    dialog = PairHeadUnitDialog(parent)
    if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        return dialog.unit
    return None


__all__ = ["PairHeadUnitDialog", "pair_head_unit"]
