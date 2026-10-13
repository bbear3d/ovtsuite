# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The connection bar must not overwrite remembered settings while it builds.

Reported twice: the logger "STILL isn't saving cable/transport/speed per ECU".
Per-scope keying (2026-10-09) did not fix it because the keys were never the
problem. ConnectionBar.__init__ calls _ecu_changed() BEFORE restore_settings(),
and _ecu_changed() called save_settings() -- so every dialog open wrote the
default transport, default controller and default port over the stored values,
and the restore two lines later read back the defaults it had just written.

Qt does not load in this workspace (see ovtsuite.gui.HAVE_QT), so these read the
source and the AST rather than driving widgets. That is the point: the defect was
an ORDER-OF-CALLS bug, which is exactly what source structure can prove.
"""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LOG = os.path.join(ROOT, "ovtsuite", "datalog", "gui_log.py")
FLASH = os.path.join(ROOT, "ovtsuite", "flash", "gui_flash.py")


def tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def cls(path, name):
    for n in ast.walk(tree(path)):
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    raise AssertionError("%s not found in %s" % (name, path))


def meth(path, klass, name):
    for n in cls(path, klass).body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("%s.%s not found" % (klass, name))


def code(node, path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    seg = ast.get_source_segment(src, node) or ""
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        doc = body[0].value.value
        seg = seg.replace(doc, "")          # strip the docstring that DESCRIBES the bug
    return seg


# ---------------------------------------------------------------- the gate
def test_save_settings_is_gated_on_ready():
    """Without this gate the construction-time call destroys the stored values."""
    src = code(meth(LOG, "ConnectionBar", "save_settings"), LOG)
    assert "self._ready" in src
    assert "not self._ready" in src


def test_init_clears_ready_before_building_anything():
    init = meth(LOG, "ConnectionBar", "__init__")
    assigns = []
    for n in init.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            t = n.targets[0]
            if isinstance(t, ast.Attribute) and t.attr in ("_ready", "_restoring", "_scope"):
                assigns.append(t.attr)
    assert "_ready" in assigns, "__init__ must set _ready"
    # it must be set before the widgets are wired, i.e. among the first few
    assert assigns.index("_ready") <= 2


def test_ready_is_opened_only_after_the_restore_and_the_platform_default():
    """The gate must lift LAST, so neither the restore nor the tab-derived
    controller default is mistaken for a user choice."""
    init = meth(LOG, "ConnectionBar", "__init__")
    order = []
    for n in ast.walk(init):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("restore_settings", "default_from_platform", "_ecu_changed"):
                order.append((n.lineno, n.func.attr))
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            t = n.targets[0]
            if isinstance(t, ast.Attribute) and t.attr == "_ready" \
                    and isinstance(n.value, ast.Constant) and n.value.value is True:
                order.append((n.lineno, "_ready=True"))
    order.sort()
    names = [n for _, n in order]
    assert names[-1] == "_ready=True", names
    for req in ("_ecu_changed", "restore_settings", "default_from_platform"):
        assert names.index(req) < names.index("_ready=True")


def test_the_destructive_call_order_still_exists_so_the_gate_is_load_bearing():
    """_ecu_changed() before restore_settings() is deliberate (it builds the
    speed list). This test fails if someone 'fixes' it by reordering and drops
    the gate, which would reintroduce the bug by a different route."""
    init = meth(LOG, "ConnectionBar", "__init__")
    lines = {}
    for n in ast.walk(init):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            lines.setdefault(n.func.attr, n.lineno)
    assert lines["_ecu_changed"] < lines["restore_settings"]
    assert "not self._ready" in code(meth(LOG, "ConnectionBar", "save_settings"), LOG)


def test_ecu_changed_still_saves_for_real_user_changes():
    src = code(meth(LOG, "ConnectionBar", "_ecu_changed"), LOG)
    assert "save_settings" in src, "a hand-picked controller must still persist"


def test_settings_are_flushed():
    """QSettings buffers; an app closed hard otherwise loses the write."""
    assert "s.sync()" in code(meth(LOG, "ConnectionBar", "save_settings"), LOG)


# ------------------------------------------------- automatic defaults stay quiet
def test_set_ecu_quietly_suppresses_persistence():
    src = code(meth(LOG, "ConnectionBar", "set_ecu_quietly"), LOG)
    assert "_ready" in src and "finally" in src, "must restore the flag even on error"


def test_second_dual_pane_pairs_itself_without_persisting():
    src = code(meth(LOG, "ConnPane", "__init__"), LOG)
    assert "set_ecu_quietly" in src
    assert "ecu_box.setCurrentIndex" not in src, \
        "forcing the box directly would persist the pairing as a user choice"


def test_dtc_dialog_does_not_share_the_first_panes_scope():
    with open(LOG, encoding="utf-8") as fh:
        src = fh.read()
    assert 'ConnectionBar(window, scope="dtc")' in src
    assert "ConnectionBar(window)\n" not in src, "default scope would collide with pane a"


# ------------------------------------------------------------------ the flasher
def test_flasher_keys_the_cable_per_controller_and_transport():
    src = code(meth(FLASH, "FlashDialog", "_port_pref_key"), FLASH) \
        if _has(FLASH, "FlashDialog", "_port_pref_key") else ""
    assert "port/%s/%s" in src


def _has(path, klass, name):
    try:
        meth(path, klass, name)
        return True
    except AssertionError:
        return False


def test_flasher_falls_back_to_the_old_flat_key():
    src = code(meth(FLASH, "FlashDialog", "_want_port_for"), FLASH)
    assert 's.value("port")' in src, "an upgrade must keep the selected cable"


def test_flasher_reselects_the_cable_when_the_transport_changes():
    src = code(meth(FLASH, "FlashDialog", "_transport_changed"), FLASH)
    assert "_want_port_for" in src
    assert src.index("_want_port_for") < src.index("_refresh_ports"), \
        "the pending port must be set BEFORE the rescan consumes it"


def test_flasher_still_writes_the_flat_key_for_backward_compatibility():
    src = code(meth(FLASH, "FlashDialog", "_save_settings"), FLASH)
    assert 's.setValue("port"' in src
    assert "_port_pref_key(" in src
