# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The OVT Diag command palette overlay (offscreen): open / close / dismiss,
typing narrows, filter chips with counts, the keyboard (Up/Down skipping the
headings, Enter, Shift+Enter, Esc, Tab, Ctrl+K), clicks, prefill,
``set_items`` / ``set_values``, following the parent's size, both themes.
``QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gui_diag_palette.py -q --slow``"""
import os
import sys

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui, QtTest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _items():
    from ovtsuite.diag.palette_index import PaletteItem as P
    return [
        P("live", "Boost pressure, actual", "01", "Engine group 011 cell 3", "Ldm_pIst", value="1012 mbar",
          payload={"group": 11, "cell": 3}),
        P("live", "Boost pressure, specified", "01", "Engine group 011 cell 2", "Ldm_pSoll", value="1005 mbar",
          payload={"key": "Ldm_pSoll"}),
        P("live", "N75 boost control valve duty", "01", "Engine group 011 cell 4", value="12.4 %"),
        P("live", "Boost limit, torque limiter active flag", "01", "Engine group 015 cell 2"),
        P("live", "Coolant temperature", "01", "Engine group 001 cell 4", value="88 C"),
        P("fault", "P0299 008857 Boost pressure regulation: control range not reached", "01", "Stored",
          "P0299 008857", chip="active", chip_kind="bad"),
        P("fault", "P0235 000565 Boost pressure sensor: implausible signal", "01", "Last seen", "P0235 000565",
          chip="history", chip_kind="ok"),
        P("actuator", "Actuator test: boost pressure control valve N75", "01", "tester id 0x0C"),
        P("adaptation", "Boost adaptation reset", "01"),
        P("procedure", "Boost leak check", "", "Guided"),
        P("module", "Engine", "01", "Engine control unit"),
        P("module", "ABS", "03", "Brakes"),
        P("action", "Scan all modules"),
    ]


@pytest.fixture
def host(app):
    from ovtsuite.diag.gui_palette import CommandPalette
    parent = QtWidgets.QWidget()
    parent.resize(1200, 800)
    parent.show()
    pal = CommandPalette(parent)
    pal.sent = []
    pal.dismissed_n = [0]
    pal.activated.connect(lambda item, add: pal.sent.append((item, add)))
    pal.dismissed.connect(lambda: pal.dismissed_n.__setitem__(0, pal.dismissed_n[0] + 1))
    yield parent, pal
    parent.close()


def key(pal, k, mods=Qt.KeyboardModifier.NoModifier):
    QtTest.QTest.keyClick(pal.search_field(), k, mods)


def type_text(pal, text):
    QtTest.QTest.keyClicks(pal.search_field(), text)


def test_closed_until_popup_then_close(host):
    parent, pal = host
    assert not pal.is_open() and not pal.isVisible()
    pal.popup(_items())
    assert pal.is_open() and pal.isVisible()
    pal.close_palette()
    assert not pal.is_open() and not pal.isVisible()
    assert pal.dismissed_n[0] == 0 and not pal.sent           # a programmatic close signals nothing


def test_popup_covers_parent_and_shows_empty_query(host):
    parent, pal = host
    pal.popup(_items())
    assert pal.geometry() == parent.rect()
    assert pal.query() == ""
    texts = pal.row_texts()
    assert "MODULES" in texts and "ACTIONS" in texts
    assert {i.kind for i in pal.hit_items()} == {"module", "action", "procedure"}
    box = pal.box_geometry()
    assert abs(box.center().x() - parent.width() / 2) <= 1
    assert box.width() <= 840 and box.height() <= parent.height() * 0.8 + 1
    assert pal.search_field().hasFocus() or pal.focusProxy() is pal.search_field()


def test_typing_narrows_and_groups_under_headings(host):
    parent, pal = host
    pal.popup(_items())
    type_text(pal, "boost")
    titles = [i.title for i in pal.hit_items()]
    assert len(titles) == 9
    texts = pal.row_texts()
    assert texts[0] == "LIVE VALUES"
    assert "FAULTS" in texts and any(t.startswith("ACTUATOR TESTS") for t in texts)
    assert "Coolant temperature" not in titles
    type_text(pal, " actual")
    assert [i.title for i in pal.hit_items()] == ["Boost pressure, actual"]
    assert pal.current_item().title == "Boost pressure, actual"


def test_no_match_shows_a_message_and_no_current_row(host):
    parent, pal = host
    pal.popup(_items())
    type_text(pal, "qqqq")
    assert pal.hit_items() == [] and pal.current_item() is None
    assert "qqqq" in pal.row_texts()[0]
    key(pal, Qt.Key.Key_Return)                               # nothing to open
    assert pal.sent == [] and pal.is_open()


def test_chips_show_counts_for_kinds_with_matches(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    assert pal.chip_labels() == ["All · 9", "Live values · 4", "Faults · 2", "Adaptation · 1",
                                 "Actuator tests · 1", "Procedures · 1"]
    assert not any("Output" in t for t in pal.chip_labels())
    pal.popup(_items(), "")
    assert pal.chip_labels() == ["All · 4", "Modules · 2", "Procedures · 1", "Actions · 1"]


def test_chip_filters_but_keeps_counts_and_tab_cycles(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    key(pal, Qt.Key.Key_Tab)
    assert pal.kind_filter() == "live"
    assert {i.kind for i in pal.hit_items()} == {"live"}
    assert pal.chip_labels()[0] == "All · 9"                  # counts are over all matches
    key(pal, Qt.Key.Key_Tab)
    assert pal.kind_filter() == "fault" and len(pal.hit_items()) == 2
    key(pal, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert pal.kind_filter() == "live"
    key(pal, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert pal.kind_filter() == "all"
    key(pal, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)       # wraps round to the last chip
    assert pal.kind_filter() == "procedure"
    assert pal.is_open() and pal.sent == []                   # Tab did not leave the field


def test_chip_click_selects_and_falls_back_to_all_when_empty(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    chips = pal._chips
    chips.grab()                                              # lays the chips out
    rect = dict(chips._rects)["fault"]
    QtTest.QTest.mouseClick(chips, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            rect.center().toPoint())
    assert pal.kind_filter() == "fault"
    type_text(pal, " valve")                                  # no faults match any more
    assert pal.kind_filter() == "all"
    assert {i.kind for i in pal.hit_items()} == {"live", "actuator"}


def test_up_down_skip_headings_and_wrap(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    hits = pal.hit_items()
    assert pal.current_item() is hits[0]
    key(pal, Qt.Key.Key_Down)
    assert pal.current_item() is hits[1]
    for _ in range(3):                                         # past the end of the live values: onto a fault, not a heading
        key(pal, Qt.Key.Key_Down)
    assert pal.current_item().kind == "fault"
    key(pal, Qt.Key.Key_Up)
    assert pal.current_item().kind == "live"
    key(pal, Qt.Key.Key_End)
    assert pal.current_item() is hits[-1]
    key(pal, Qt.Key.Key_Down)                                  # wraps
    assert pal.current_item() is hits[0]
    key(pal, Qt.Key.Key_Up)
    assert pal.current_item() is hits[-1]
    key(pal, Qt.Key.Key_Home)
    assert pal.current_item() is hits[0]
    key(pal, Qt.Key.Key_PageDown)
    assert pal.current_item() is hits[min(8, len(hits) - 1)]
    key(pal, Qt.Key.Key_PageUp)
    assert pal.current_item() is hits[0]


def test_enter_activates_the_current_row_and_closes(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    key(pal, Qt.Key.Key_Down)
    key(pal, Qt.Key.Key_Return)
    assert [(i.title, a) for i, a in pal.sent] == [("Boost pressure, specified", False)]
    assert not pal.is_open() and pal.dismissed_n[0] == 0


def test_shift_enter_adds_to_live_view(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    key(pal, Qt.Key.Key_Enter, Qt.KeyboardModifier.ShiftModifier)
    assert len(pal.sent) == 1 and pal.sent[0][1] is True
    assert pal.sent[0][0].title == "Boost pressure, actual"


def test_escape_and_ctrl_k_dismiss(host):
    parent, pal = host
    pal.popup(_items())
    key(pal, Qt.Key.Key_Escape)
    assert not pal.is_open() and pal.dismissed_n[0] == 1 and not pal.sent
    pal.popup(_items())
    key(pal, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert not pal.is_open() and pal.dismissed_n[0] == 2


def test_keys_sent_to_the_overlay_reach_the_field_and_the_list(host):
    parent, pal = host
    pal.popup(_items())
    QtTest.QTest.keyClicks(pal, "abs")
    assert pal.query() == "abs"
    assert pal.current_item().title == "ABS"
    QtTest.QTest.keyClick(pal, Qt.Key.Key_Return)
    assert pal.sent and pal.sent[0][0].title == "ABS"


def test_click_row_activates_and_hover_moves_current(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    view = pal._view
    rows = pal._model.rows
    idx = pal._model.index(next(i for i, r in enumerate(rows) if r.type == "hit" and r.hit.item.kind == "fault"))
    pos = view.visualRect(idx).center()
    # a mouse-move event sent straight to the viewport: QTest.mouseMove depends
    # on where the real cursor was left by whatever test ran before
    from PySide6 import QtGui
    vp = view.viewport()
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)),
                           Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, ev)
    assert pal.current_item().kind == "fault"
    QtTest.QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert len(pal.sent) == 1 and pal.sent[0][0].kind == "fault" and pal.sent[0][1] is False
    assert not pal.is_open()


def test_click_on_a_heading_does_nothing(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    view = pal._view
    pos = view.visualRect(pal._model.index(0)).center()
    QtTest.QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert pal.is_open() and not pal.sent


def test_click_on_the_dim_dismisses_but_not_inside_the_box(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    box = pal.box_geometry()
    QtTest.QTest.mouseClick(pal, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            box.center())
    assert pal.is_open() and pal.dismissed_n[0] == 0
    QtTest.QTest.mouseClick(pal, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QtCore.QPoint(4, 4))
    assert not pal.is_open() and pal.dismissed_n[0] == 1


def test_prefill_types_the_query(host):
    parent, pal = host
    pal.popup(_items(), "p0299")
    assert pal.query() == "p0299"
    assert [i.kind for i in pal.hit_items()] == ["fault"]
    assert pal.search_field().cursorPosition() == 5
    pal.popup(_items(), "")                                     # a second popup starts afresh
    assert pal.query() == "" and pal.kind_filter() == "all"


def test_prefixes(host):
    parent, pal = host
    pal.popup(_items(), "03: ")
    assert [i.title for i in pal.hit_items()] == ["ABS"]
    pal.popup(_items(), "> scan")
    assert [i.title for i in pal.hit_items()] == ["Scan all modules"]


def test_set_items_re_searches_and_keeps_the_current_row(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    key(pal, Qt.Key.Key_Down)
    assert pal.current_item().title == "Boost pressure, specified"
    pal.set_items(_items()[:7] + _items()[8:])                  # the actuator test went away
    assert pal.current_item().title == "Boost pressure, specified"
    assert "Actuator tests · 1" not in pal.chip_labels()
    assert pal.chip_labels()[0] == "All · 8"
    pal.set_items([])
    assert pal.hit_items() == [] and pal.is_open()


def test_set_values_changes_the_value_without_re_searching(host, monkeypatch):
    from ovtsuite.diag import gui_palette as G
    parent, pal = host
    pal.popup(_items(), "boost")
    rows = {r.hit.item.title: r for r in pal._model.rows if r.type == "hit"}
    assert rows["Boost pressure, actual"].value == "1012 mbar"
    calls = []
    monkeypatch.setattr(G, "search", lambda *a, **k: calls.append(a) or None)
    pal.set_values({("01", "live", "Boost pressure, actual"): "1100 mbar",
                    ("01", "live", "Ldm_pSoll"): "990 mbar",             # payload["key"]
                    ("1", "live", (11, 3)): "1111 mbar",                 # (group, cell), short address
                    ("01", "live", "nothing like it"): "zzz"})
    assert calls == []
    assert rows["Boost pressure, actual"].value == "1111 mbar"
    assert rows["Boost pressure, specified"].value == "990 mbar"
    assert pal.current_item().value == "1012 mbar"              # the item itself is not rewritten
    monkeypatch.undo()
    type_text(pal, " act")                                       # a new search keeps the live value
    assert [r.value for r in pal._model.rows if r.type == "hit" and r.hit.item.title == "Boost pressure, actual"]         == ["1111 mbar"]


def test_follows_the_parent_resize_while_open(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    parent.resize(900, 600)
    QtTest.QTest.qWait(10)
    assert pal.geometry() == parent.rect()
    assert pal.box_geometry().width() <= 900 - 32
    assert pal.box_geometry().height() <= 600 * 0.8 + 1
    parent.resize(1400, 1000)
    assert pal.geometry() == parent.rect()
    assert abs(pal.box_geometry().center().x() - 700) <= 1


def test_many_results_use_rows_not_widgets_and_cap_each_group(host):
    from ovtsuite.diag.gui_palette import GROUP_CAP
    from ovtsuite.diag.palette_index import PaletteItem as P
    parent, pal = host
    n_children = len(pal.findChildren(QtWidgets.QWidget))
    items = [P("live", "Reading %d boost" % i, "01", "Engine", value="%d" % i) for i in range(1000)]
    pal.popup(items, "reading")
    assert len(pal.findChildren(QtWidgets.QWidget)) <= n_children + 5
    assert len(pal.hit_items()) == GROUP_CAP
    assert pal.row_texts()[-1].startswith("%d more" % (1000 - GROUP_CAP))
    assert pal.chip_labels()[0] == "All · 1000"
    key(pal, Qt.Key.Key_End)                                    # the "more" row is not selectable
    assert pal.current_item() is pal.hit_items()[-1]
    assert pal.box_geometry().height() <= 800 * 0.8 + 1


def test_rows_expose_their_text_for_accessibility(host):
    parent, pal = host
    pal.popup(_items(), "boost")
    model = pal._model
    first = model.index(1)
    text = model.data(first, Qt.ItemDataRole.AccessibleTextRole)
    assert "Boost pressure, actual" in text and "1012 mbar" in text
    assert pal._view.accessibleName() == "Results"
    assert "Boost pressure, actual" in pal._view.accessibleDescription()


@pytest.mark.parametrize("dark", [True, False])
def test_builds_and_paints_in_both_palettes(app, dark):
    from ovtsuite import gui_theme
    from ovtsuite.diag.gui_palette import CommandPalette
    old = app.palette()
    try:
        if dark:
            app.setPalette(gui_theme.dark_palette())
        else:
            pal_l = QtGui.QPalette()
            for role, col in ((QtGui.QPalette.ColorRole.Window, "#f0f0f0"), (QtGui.QPalette.ColorRole.WindowText, "#111111"),
                              (QtGui.QPalette.ColorRole.Base, "#ffffff"), (QtGui.QPalette.ColorRole.Text, "#111111")):
                pal_l.setColor(role, QtGui.QColor(col))
            app.setPalette(pal_l)
        parent = QtWidgets.QWidget()
        parent.resize(1000, 700)
        parent.show()
        p = CommandPalette(parent)
        p.popup(_items(), "boost")
        img = parent.grab().toImage()
        assert not img.isNull()
        # the dim darkens the corner of the parent compared with no overlay
        p.close_palette()
        clear = parent.grab().toImage()
        assert img.pixelColor(5, 5).lightness() < clear.pixelColor(5, 5).lightness()
        # a theme change while open repaints without error
        p.popup(_items(), "boost")
        app.setPalette(gui_theme.dark_palette() if not dark else QtGui.QPalette())
        QtTest.QTest.qWait(20)
        assert not parent.grab().toImage().isNull()
        parent.close()
    finally:
        app.setPalette(old)
