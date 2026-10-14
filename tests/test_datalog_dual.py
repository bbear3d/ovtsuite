# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Dual logging: one logger dialog driving two controllers at once.

Why it exists: the questions that matter span both boxes. The transmission
commands a shift and the engine answers it, and a shift start inferred from a
TCU ratio change says nothing about what the engine did in the same hundred
milliseconds. Engine on K-line (pin 7) and transmission on CAN (6/14) are
separate physical layers, so neither contends with the other, each keeps its
full rate, and both workers stamp from the same clock -- which is the whole
reason the two files can be lined up afterwards.

The structural tests below run anywhere. The behavioural ones need a working
QtWidgets, which the tuning box and the Linux side of this workspace both
lack (no libEGL), so they skip there and run on a machine that has it.
"""
import ast
import io
import os

import pytest

from ovtsuite import gui as _g

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "ovtsuite", "datalog", "gui_log.py")

# gui_log imports QtWidgets at module scope, so the structural tests below read
# the source instead of importing it -- which is the point: they have to run on
# the boxes where the dialog cannot be built at all.

needs_qt = pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")


# ----------------------------------------------------- structural, no Qt
@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _class_attrs(name):
    """Attributes a class sets, reads, and defines as methods."""
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    assigned, read, methods = set(), set(), set()
    for n in cls.body:
        if isinstance(n, ast.FunctionDef):
            methods.add(n.name)
        # class-scope state: the signals and the controller map
        if isinstance(n, ast.Assign):
            assigned.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            assigned.add(n.target.id)
    for node in ast.walk(cls):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)
    return assigned, read, methods


# Qt gives every widget these; they are not the class's own state.
_INHERITED = {
    "setParent", "setToolTip", "isActiveWindow", "update", "setWindowTitle",
    "setWindowIcon", "resize", "isVisible", "raise_", "activateWindow",
    "setWindowModality", "show", "parent", "layout", "setLayout", "window",
}


@pytest.mark.parametrize("cls", ["ConnPane", "DataLogDialog"])
def test_no_attribute_was_left_behind_by_the_split(cls):
    """The per-connection state moved out of the dialog into ConnPane. The
    classic way that breaks is a method that came along still reaching for
    state that stayed behind -- ``self.status`` on the pane, say, or
    ``self._channels`` on the dialog. Each class must set everything it reads.

    This runs without Qt, which matters: on the boxes where this code is
    edited the dialog cannot be constructed at all, so a dangling attribute
    would otherwise surface only on the car."""
    assigned, read, methods = _class_attrs(cls)
    known = assigned | methods | _INHERITED
    assert sorted(a for a in read if a not in known) == []


def _class_dict(cls_name, attr):
    """A dict literal assigned at class scope, read without importing."""
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    for n in cls.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == attr for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError("%s.%s not found" % (cls_name, attr))


def test_the_two_controller_maps_are_inverses():
    """``ConnectionBar.PLATFORM_ECU`` picks the controller for a loaded binary;
    ``ConnPane.PLATFORM_OF_ECU`` picks the binary for a chosen controller. If
    they ever disagree, a pane would default to one controller and then name
    its cells and its log file from the other's calibration."""
    fwd = _class_dict("ConnectionBar", "PLATFORM_ECU")      # platform -> ECU
    rev = _class_dict("ConnPane", "PLATFORM_OF_ECU")        # ECU -> platform
    assert {v: k for k, v in fwd.items()} == rev


@pytest.mark.parametrize("name", ["conn", "preset_box", "preset_hint", "path_edit",
                                  "group_box", "_namer", "_channels", "_worker"])
def test_the_dialog_still_answers_for_the_active_connection(name):
    """Callers and tests written against the single-connection dialog say
    ``dlg.preset_box``; with two panes that has to mean the one in front
    rather than breaking."""
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "DataLogDialog")
    props = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)
             and any(isinstance(d, ast.Name) and d.id == "property"
                     for d in n.decorator_list)}
    assert name in props


def test_the_pane_owns_the_worker_lifecycle():
    """``retire_thread`` on end moved with the worker. A QThread destroyed
    while still running takes the process with it, so whoever owns the worker
    must own the retire."""
    assigned, read, methods = _class_attrs("ConnPane")
    assert {"_on_ended", "shutdown", "connect_now", "disconnect_now"} <= methods
    src = io.open(SRC, encoding="utf-8").read()
    pane = src[src.index("class ConnPane"):src.index("class DataLogDialog")]
    assert pane.count("retire_thread(") == 2        # _on_ended and shutdown
    # bound the dialog at the next top-level class: EcuTask below it retires
    # its own thread, legitimately, and would otherwise be counted here
    after = src[src.index("class DataLogDialog"):]
    dlg = after[:after.index("\nclass ")] if "\nclass " in after else after
    assert "retire_thread(" not in dlg


# ------------------------------------------------------- behavioural, Qt
@pytest.fixture
def dual(request):
    if not _g.HAVE_QT:
        pytest.skip("needs a working PySide6 QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    from ovtsuite.datalog import gui_log as GL
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # the remembered Dual logging box: a file of this test's own
    tmp = request.getfixturevalue("tmp_path")
    mp = request.getfixturevalue("monkeypatch")
    mp.setattr(GL, "_prefs", lambda: QtCore.QSettings(
        str(tmp / "prefs.ini"), QtCore.QSettings.Format.IniFormat))

    class _Bin:
        def __init__(self, name):
            self.name = name

    class _Sess:
        def __init__(self, platform, name):
            self.platform = platform
            self.binfile = _Bin(name)

    class _Ws:
        def __init__(self, sess):
            self.primary = sess

    class _Win(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self._ws = [_Ws(_Sess("edc16", "ecu-lq-stg1.edc16")),
                        _Ws(_Sess("zf6hp", "6hp19-tdi20.6hp"))]

        def workspaces(self):
            return list(self._ws)

        @property
        def current(self):
            return self._ws[0]

        @property
        def primary(self):
            return self._ws[0].primary

    win = _Win()
    dlg = GL.DataLogDialog(win)
    yield app, dlg
    dlg.close()


@needs_qt
def test_it_opens_with_one_connection(dual):
    """Dual logging is opt-in: nothing changes for anyone logging one box."""
    _app, d = dual
    assert len(d.panes) == 1
    assert d.dual_chk.isChecked() is False
    assert d.m_connect.text() == "Connect"


@needs_qt
def test_the_checkbox_adds_the_other_controller(dual):
    """The second pane defaults to the other side of the pair, so turning it
    on is usually the only click needed."""
    _app, d = dual
    first = d.panes[0].conn.ecu()
    d.dual_chk.setChecked(True)
    assert len(d.panes) == 2
    assert d.panes[1].conn.ecu() != first
    assert {"EDC16", "ZF6HP"} == {p.conn.ecu() for p in d.panes}


@needs_qt
def test_turning_it_off_removes_the_tab_again(dual):
    _app, d = dual
    d.dual_chk.setChecked(True)
    d.dual_chk.setChecked(False)
    assert len(d.panes) == 1
    assert all(d.tabs.widget(i) is not d._second for i in range(d.tabs.count()))


@needs_qt
def test_each_pane_binds_to_its_own_binary_not_the_front_tab(dual):
    """The old dialog followed whichever tab was in front, which was fine with
    one connection and silently wrong with two: the transmission pane would
    name its cells from the engine file just because that tab was selected."""
    _app, d = dual
    d.dual_chk.setChecked(True)
    ecu = next(p for p in d.panes if p.conn.ecu() == "EDC16")
    tcu = next(p for p in d.panes if p.conn.ecu() == "ZF6HP")
    assert ecu._primary().binfile.name == "ecu-lq-stg1.edc16"
    assert tcu._primary().binfile.name == "6hp19-tdi20.6hp"


@needs_qt
def test_the_two_log_files_cannot_collide(dual):
    """Two sessions writing one path would interleave rows from two buses into
    one file and lose both."""
    _app, d = dual
    d.dual_chk.setChecked(True)
    for p in d.panes:
        p._set_default_path()
    paths = [p.path_edit.text() for p in d.panes]
    assert all(paths)
    assert len(set(paths)) == 2
    assert "ecu-lq-stg1" in paths[0] and "6hp19-tdi20" in paths[1]


@needs_qt
def test_the_master_button_speaks_in_the_plural_only_when_it_should(dual):
    _app, d = dual
    assert d.m_connect.text() == "Connect"
    d.dual_chk.setChecked(True)
    assert d.m_connect.text() == "Connect all"


@needs_qt
def test_the_master_controls_fan_out_to_every_pane(dual):
    """No hardware here, so the panes are stubbed; what is under test is that
    the master row reaches both of them and skips the ones it should."""
    _app, d = dual
    d.dual_chk.setChecked(True)
    calls = []

    for p in d.panes:
        p.is_connected = lambda: True
        p.is_logging = lambda: True
        p._start = lambda quiet=False, _p=p: (calls.append(("start", _p.title())), True)[1]
        p._stop_logging = lambda finish=True, _p=p: calls.append(("stop", _p.title()))
        p.disconnect_now = lambda _p=p: calls.append(("disc", _p.title()))

    d._master_start()
    d._master_stop()
    d._master_connect()
    assert [c[0] for c in calls] == ["start", "start", "stop", "stop", "disc", "disc"]
    assert {c[1] for c in calls} == {"EDC16", "ZF6HP"}


@needs_qt
def test_a_pane_with_nothing_ticked_does_not_stop_the_other(dual):
    """Each file is its own, so a half-configured side should log what it can
    and say so rather than refusing both."""
    _app, d = dual
    d.dual_chk.setChecked(True)
    started = []
    a, b = d.panes
    a.is_connected = lambda: True
    b.is_connected = lambda: True
    a._start = lambda quiet=False: (started.append("a"), True)[1]
    b._start = lambda quiet=False: False        # nothing ticked
    d._master_start()
    assert started == ["a"]
    assert "only" in d.status.text()


@needs_qt
def test_the_status_line_says_which_box_spoke(dual):
    """'Group 213: not available on this ECU' is useless when two are open."""
    _app, d = dual
    d.panes[0]._say("hello")
    assert d.status.text() == "hello"            # one connection: no prefix
    d.dual_chk.setChecked(True)
    d.panes[1]._say("hello")
    assert d.status.text() == "%s: hello" % d.panes[1].title()


@needs_qt
def test_the_dual_logging_box_is_remembered(dual):
    """Ticked last time -> both connections again next time (the first one in
    front); unticked -> one."""
    from ovtsuite.datalog import gui_log as GL
    _app, d = dual
    d.dual_chk.setChecked(True)
    again = GL.DataLogDialog(d._window)
    try:
        assert again.dual_chk.isChecked() and len(again.panes) == 2
        assert again.tabs.currentIndex() == 0
        again.dual_chk.setChecked(False)
    finally:
        again.close()
    third = GL.DataLogDialog(d._window)
    try:
        assert not third.dual_chk.isChecked() and len(third.panes) == 1
    finally:
        third.close()


# ------------------------------------- the pane must be constructible, no Qt
def _construction_order_problems(cls_name, entry="__init__"):
    """Walk the construction path in source order, following calls to the
    class's own methods, and report any ``self.<attr>`` read before it is
    assigned.

    This exists because of a real bug: ``_build_body`` called ``_fill_groups``
    before creating ``src_hint``, and ``_fill_groups`` ends in
    ``_update_src_hint``, which reads it -- so constructing a pane raised
    AttributeError. The plain "is it set anywhere in the class" check passes
    that happily, and QtWidgets does not load on the machines where this file
    is edited, so nothing else would have caught it before the car.
    """
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    defined = set(methods) | set(_INHERITED)
    for n in cls.body:
        if isinstance(n, ast.Assign):
            defined.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined.add(n.target.id)

    problems, stack = [], []

    def self_attrs(node, ctx):
        return [x.attr for x in ast.walk(node)
                if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                and x.value.id == "self" and isinstance(x.ctx, ctx)]

    def run(fn):
        if fn.name in stack or len(stack) > 12:
            return                                  # recursion guard
        stack.append(fn.name)
        for stmt in fn.body:
            # calls to our own methods happen before this statement's own reads
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in methods):
                    run(methods[node.func.attr])
            for a in self_attrs(stmt, ast.Load):
                if a not in defined:
                    problems.append("%s reads self.%s before it is set" % (fn.name, a))
            defined.update(self_attrs(stmt, ast.Store))
        stack.pop()

    run(methods[entry])
    return problems


def test_a_connection_pane_can_actually_be_built():
    assert _construction_order_problems("ConnPane") == []


def test_the_dialog_can_actually_be_built():
    assert _construction_order_problems("DataLogDialog") == []


def _method_code(cls_name, meth):
    """The executable source of one method, docstring removed.

    Needed because these assertions are about what the code does, and the
    docstrings here deliberately name the thing that used to be wrong -- a
    plain substring check on the text fails on its own explanation."""
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == meth)
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(st) for st in body)


# --------------------------------------------- stopping a dual log (no Qt)
def _src():
    return io.open(SRC, encoding="utf-8").read()


def test_stop_does_not_build_the_viewer_table_inline():
    """Stop used to land straight in show_log, which allocates one
    QTableWidgetItem per cell: ~119,000 for a 9130-row engine log and another
    ~66,000 for the transmission log beside it, then measures the text of
    every one. Two connections did that twice inside the click handler, which
    is the hang, and the memory spike behind the crashes."""
    code = _method_code("DataLogDialog", "_load_view")
    assert "show_log" not in code, "_load_view must queue, not load"
    assert "singleShot" in code, "the load has to come off the handler"


def test_the_queued_load_is_coalesced():
    """A dual stop fires log_finished twice. Without coalescing the viewer
    would read and render both logs back to back for no benefit."""
    s = _src()
    assert "_view_queued" in s
    body = s[s.index("    def _drain_view(self"):]
    body = body[:body.index("\n    def ")]
    assert "_view_queued = False" in body


def test_the_table_is_a_view_over_the_parsed_log():
    """The 4000-row cap existed because the table built one item per cell
    in the GUI thread. It is a model now, and the file is parsed on a worker:
    nothing per cell is built, so every row is shown."""
    s = _src()
    assert "VIEW_ROW_BUDGET" not in s
    assert "QTableWidgetItem" not in s[s.index("class DataLogDialog"):]
    body = s[s.index("    def show_log(self"):]
    body = body[:body.index("\n    def ")]
    assert "view_model.set_log" in body
    assert "resizeColumnsToContents" not in body, "that scans every cell"
    drain = _method_code("DataLogDialog", "_drain_view")
    assert "_lf.load" not in drain, "the parse belongs on the loader's thread"


def test_logging_state_is_not_read_off_a_button():
    """_set_logging_ui disables the stop button partway through the stop, so a
    master Stop iterating the panes saw the first one as already stopped while
    it still held an open file."""
    code = _method_code("ConnPane", "is_logging")
    assert "stop_btn" not in code
    assert "self._logging" in code


def test_closing_asks_both_workers_before_waiting_on_either():
    """shutdown() waits up to 5 s, so two in series could sit for ten seconds
    looking hung. A CAN teardown is the slow one and both can do it at once."""
    s = _src()
    body = s[s.index("    def closeEvent(self, ev):"):]
    body = body[:body.index("super().closeEvent")]
    assert body.index("request_stop") < body.index("wind_down(")
    # one shared deadline for all of them, then the transport is cut
    wd = _method_code("ConnPane", "wind_down")
    assert wd.count("CLOSE_WAIT_S") == 1 and "_abort_worker" in wd


# ------------------------------------- remembering transport and cable (no Qt)
def test_each_pane_gets_its_own_settings_scope():
    """With one flat conn/* group both panes restored the same keys, so the
    transmission pane came up on the engine's cable and whichever connected
    last overwrote the other's transport."""
    s = _src()
    assert 'ConnPane(window, scope="a")' in s
    assert 'scope="b")' in s
    assert "ConnectionBar(window, scope=scope)" in s


def test_the_cable_is_keyed_by_controller_and_transport():
    s = _src()
    body = s[s.index("    def save_settings(self"):]
    body = body[:body.index("    def _restore_port_for")]
    assert 'self._key("port", ecu, tr)' in body, "one port key per (ecu, transport)"


def test_transport_is_restored_before_the_controller():
    """Choosing a transport rebuilds both the controller list and the port
    list, so restoring it after either would discard them."""
    s = _src()
    body = s[s.index("    def restore_settings(self"):]
    body = body[:body.index("    def _migrate_flat_settings")]
    assert body.index('findData(tr)') < body.index('setCurrentText(ecu)')


def test_selections_persist_without_connecting():
    """save_settings used to be called only from connect_now, so a transport
    picked and then not connected was forgotten -- most of why it never
    seemed to stick."""
    s = _src()
    assert "self.can_rate.currentIndexChanged.connect(lambda *_: self.save_settings())" in s
    rp = s[s.index("    def _remember_port(self"):]
    rp = rp[:rp.index("\n    def ")]
    assert "save_settings()" in rp


def test_restoring_does_not_write_back_while_it_runs():
    """Every widget now saves on change, so a restore that did not guard
    would overwrite the very keys it is reading."""
    s = _src()
    body = s[s.index("    def save_settings(self"):]
    body = body[:body.index("    def _restore_port_for")]
    # The guard also covers the construction-time gate added on 2026-10-10
    # (``not self._ready``), so match the intent rather than one spelling.
    assert "self._restoring" in body and "return" in body
    guard = body[body.index("if self._restoring"):]
    assert guard[:guard.index("\n")].endswith(":")
    assert "return" in guard[:guard.index("from ..flash")]


def test_old_flat_settings_are_migrated_once():
    """An upgrade must not lose the cable the user already had selected."""
    s = _src()
    assert "_migrate_flat_settings" in s
    body = s[s.index("    def _migrate_flat_settings"):]
    body = body[:body.index("\n    def ")]
    assert 'self._key("migrated")' in body
    assert '"conn/transport"' in body and '"conn/port"' in body
