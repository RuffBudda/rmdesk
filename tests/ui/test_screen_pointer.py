"""One Screen view with a two-way pointer (Control / Place), the header of clusters with its
in-window hints, the Control focus outline, Control in Expand, and Device opening from the
connection pill."""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QThreadPool
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import QApplication, QPushButton

from rmdesk import safety
from rmdesk.config import Config
from rmdesk.connection import Connection
from rmdesk.firmware import FirmwareInfo
from rmdesk.remote import input as ri
from rmdesk.stream.client import Frame
from rmdesk.ui import remote_control as rc
from rmdesk.ui import screen_view as sv
from rmdesk.ui.screen_view import ScreenView
from rmdesk.ui.theme import TOKENS
from rmdesk.ui.widgets.cluster import Cluster
from tests.fakes import FakeDevice
from tests.ui.test_remote_control import (PROC_ALL, FakeWriter, key, move, press, release,
                                          send_mouse)
from tests.ui.test_screen_view import Creds, FakeClient, FakePen, NoSecrets


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class FwConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.29.1.0", "SN123", 6)  # Place allowed ("full")


def make(qtbot, control_default=True):
    def factory(h, p):
        dev = FakeDevice({ri.PROC_DEVICES: PROC_ALL.encode()})
        dev.xochitl_active = True
        return dev
    conn = FwConnection(Config(), factory=factory, secrets=NoSecrets(), sleep=lambda s: None,
                        persist=lambda c: None)
    conn.connect()
    made: list[FakeWriter] = []

    def wf(dev, path, on_dead):
        w = FakeWriter(path, on_dead)
        made.append(w)
        return w
    view = ScreenView(conn, keep_default=False, control_default=control_default,
                      client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: "running", secrets=Creds(), catchup_delay=60.0,
                      input_writer_factory=wf, cfg=Config(), save_config=lambda c: None)
    qtbot.addWidget(view)
    view.resize(900, 1100)
    view.show()
    QApplication.processEvents()
    view.frame = Frame(1404, 1872, np.full((1872, 1404), 255, np.uint8))
    return view, conn, made


def sent(made) -> int:
    return sum(len(w.items) for w in made)


def item_point(view, item):
    """The middle of `item` on the picture, in widget (= viewport) pixels."""
    c = item.mapToScene(item.boundingRect().center())
    return QPointF(view.canvas.mapFromScene(c))


def wait_control(qtbot, view):
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)


# ---------------------------------------------------------------- the pointer

def test_control_is_the_default_pointer(qtbot):
    view, conn, made = make(qtbot)
    assert view.pointer == "control" and view.control_btn.isChecked()
    assert view.pointer_seg.index() == 0
    wait_control(qtbot, view)
    assert view.control.active and view.zoom_reset_btn.isVisible()
    assert not view.compose.toolbar.isVisible() and not view.canvas.isInteractive()


def test_look_is_gone_the_switch_is_control_and_place(qtbot):
    view, conn, made = make(qtbot)
    assert [b.text() for b in view.pointer_seg.buttons] == ["Control", "Place"]
    assert not hasattr(view, "look_btn") and sv.POINTERS == ["control", "place"]
    assert not hasattr(sv, "LOOK_TIP") and not hasattr(view, "set_control")
    with pytest.raises(ValueError):
        view.set_pointer("look")
    wait_control(qtbot, view)
    view.place_btn.click()
    assert view.pointer == "place" and view.pointer_seg.index() == 1
    assert not view.control.active and view.compose.toolbar.isVisible()
    view.control_btn.click()
    assert view.pointer == "control" and view.pointer_seg.index() == 0
    qtbot.waitUntil(lambda: view.control.active, timeout=3000)
    assert not view.compose.toolbar.isVisible()


def test_control_not_chosen_sends_nothing_and_items_are_not_interactive(qtbot):
    view, conn, made = make(qtbot, control_default=False)
    panel = view.compose
    item = panel.add_shape("rectangle")
    panel.scene.clearSelection()
    assert view.pointer == "control" and not view.canvas.isInteractive()
    assert not panel.toolbar.isVisible() and not view.zoom_reset_btn.isEnabled()
    p = item_point(view, item)
    before = item.pos()
    vp = view.canvas.viewport()
    send_mouse(vp, QEvent.Type.MouseButtonPress, p)
    move(vp, p + QPointF(60, 40))
    release(vp, p + QPointF(60, 40))
    key(view.canvas, Qt.Key.Key_A, text="a")
    QApplication.processEvents()
    assert made == [] and not view.control.active  # nothing went to the tablet
    assert not item.isSelected() and item.pos() == before  # and the item didn't move


def test_control_sends_and_items_stay_put(qtbot):
    view, conn, made = make(qtbot)
    wait_control(qtbot, view)
    panel = view.compose
    item = panel.add_shape("rectangle")
    panel.scene.clearSelection()
    assert view.pointer == "control" and view.control.active
    before = item.pos()
    p = item_point(view, item)
    vp = view.canvas.viewport()  # real mouse events arrive on the viewport
    press(vp, p)
    release(vp, p)
    touch = next(w for w in made if w.path.endswith("event2"))
    evs = touch.events()
    assert evs and (3, 0x39, -1) in evs  # a tap went out (finger down and up)
    assert not item.isSelected() and item.pos() == before
    assert panel.placed_items() == [item]


def test_place_edits_items_and_sends_no_input(qtbot):
    view, conn, made = make(qtbot)
    wait_control(qtbot, view)
    panel = view.compose
    view.place_btn.click()
    assert view.pointer == "place" and not view.control.active
    assert all(w.closed for w in made)  # Control let go of the tablet
    n = sent(made)
    item = panel.add_shape("rectangle")
    panel.scene.clearSelection()
    assert panel.toolbar.isVisible() and view.canvas.isInteractive()
    before = item.pos()
    p = item_point(view, item)
    vp = view.canvas.viewport()
    send_mouse(vp, QEvent.Type.MouseButtonPress, p)
    assert item.isSelected()
    move(vp, p + QPointF(50, 30))
    release(vp, p + QPointF(50, 30))
    assert item.pos() != before  # dragged
    key(view.canvas, Qt.Key.Key_A, text="a")
    QApplication.processEvents()
    assert sent(made) == n and not view.control.active  # nothing to the tablet


def test_typing_into_ink_text_only_in_place(qtbot):
    view, conn, made = make(qtbot, control_default=False)
    panel = view.compose
    view.place_btn.click()
    ink = panel.new_ink_text()
    assert ink is not None and ink.editing
    QApplication.sendEvent(view.canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_H,
                                                  Qt.KeyboardModifier.NoModifier, "h"))
    assert ink.text == "h"
    view.control_btn.click()  # leaving Place ends the typing and clears the selection
    assert not ink.editing and not panel._selected() and not panel.format_bar.isVisible()
    QApplication.sendEvent(view.canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_I,
                                                  Qt.KeyboardModifier.NoModifier, "i"))
    assert ink.text == "h" and panel.placed_items() == [ink]
    ink.start_editing()  # even if something starts editing: no keys outside Place
    assert view.canvas.editing_item() is None


def test_items_persist_across_pointer_changes(qtbot):
    view, conn, made = make(qtbot)
    panel = view.compose
    view.set_pointer("place")
    rect = panel.add_shape("rectangle")
    ink = panel.add_ink_text("Hello")
    rect.setPos(100, 200)
    want = {id(rect): rect.pos(), id(ink): ink.pos()}
    for name in ("control", "place", "control", "place", "control"):
        view.set_pointer(name)
        QApplication.processEvents()
        items = panel.placed_items()
        assert {id(i) for i in items} == set(want), name
        assert all(i.pos() == want[id(i)] for i in items), name
        assert all(i.scene() is panel.scene and i.isVisible() for i in items), name


def test_send_is_visible_with_every_pointer_when_items_exist(qtbot):
    view, conn, made = make(qtbot)
    panel = view.compose
    for name in ("control",):
        view.set_pointer(name)
        assert not view.send_btn.isVisible(), name  # nothing to send yet
        assert not view.send_cluster.isVisible(), name  # nor an empty cluster
    view.set_pointer("place")
    assert view.send_btn.isVisible() and view.send_btn.property("variant") == "primary"
    panel.add_ink_text("One")
    for name in ("control", "place"):
        view.set_pointer(name)
        QApplication.processEvents()
        assert view.send_btn.isVisible() and view.send_btn.isEnabled(), name
        assert panel.badge.isVisible() and panel.badge.text() == "1", name
        assert view.send_btn.parentWidget() is view.send_cluster, name
        assert view.header.isAncestorOf(view.send_btn), name
        primary = [b for b in view.findChildren(QPushButton)
                   if b.isVisible() and b.property("variant") == "primary"]
        assert primary == ([view.send_btn] if name == "place" else [view.full_btn]), name
    assert view.undo_btn.isVisible() and view.undo_btn.accessibleName() == "Undo last send"


def test_send_from_control_claims_the_job_and_releases_control(qtbot, monkeypatch):
    gate, seen = threading.Event(), []

    def slow(dev, build, confirm, **kw):
        seen.append(1)
        gate.wait(5)
        raise safety.Cancelled("not sent")
    monkeypatch.setattr(safety, "send_to_open_page", slow)
    view, conn, made = make(qtbot)
    wait_control(qtbot, view)
    panel = view.compose
    panel.add_ink_text("Hi")
    assert view.pointer == "control" and view.control.active
    view.send_btn.click()
    try:
        # (with Control on, Send first checks the open page, reads only, before the job)
        qtbot.waitUntil(lambda: conn.current_job == "sending to the tablet", timeout=5000)
        assert not view.control.active and all(w.closed for w in made)
        assert "sending to the tablet" in view.control_chip.text()
        n = sent(made)
        press(view.canvas, QPointF(300, 300))
        release(view.canvas, QPointF(300, 300))
        assert sent(made) == n  # nothing goes out while the send holds the slot
    finally:
        gate.set()
    qtbot.waitUntil(lambda: conn.current_job is None and not panel.busy, timeout=5000)
    assert seen == [1]
    qtbot.waitUntil(lambda: view.control.active, timeout=3000)  # Control comes back after


# ---------------------------------------------------------------- the focus outline

def _accent_on_left_edge(view) -> bool:
    img = view.canvas.grab().toImage()
    g, _ = view._canvas_geometry()
    x, y = int(g.target.left()) + 1, int(g.target.center().y())
    c = img.pixelColor(x, y)
    a = QColor(TOKENS["accent"])
    return abs(c.red() - a.red()) < 40 and abs(c.green() - a.green()) < 40 \
        and abs(c.blue() - a.blue()) < 40


def test_focus_outline_only_in_control_with_focus(qtbot):
    view, conn, made = make(qtbot)
    view.window().activateWindow()
    wait_control(qtbot, view)
    view.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(view.canvas.hasFocus, timeout=2000)
    assert view.canvas.outline_visible() and _accent_on_left_edge(view)
    view.refresh_btn.setFocus(Qt.FocusReason.TabFocusReason)  # focus leaves the picture
    qtbot.waitUntil(lambda: not view.canvas.hasFocus(), timeout=2000)
    assert not view.canvas.outline_visible() and not _accent_on_left_edge(view)
    view.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(view.canvas.hasFocus, timeout=2000)
    assert view.canvas.outline_visible()
    view.set_pointer("place")  # Place: the picture may have focus, but keys edit the items
    view.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(view.canvas.hasFocus, timeout=2000)
    assert not view.canvas.outline_visible() and not _accent_on_left_edge(view)


# ---------------------------------------------------------------- zoom

def test_minus_and_plus_are_gone_1x_and_ctrl_0_stay(qtbot):
    view, conn, made = make(qtbot)
    wait_control(qtbot, view)
    texts = {b.text() for b in view.findChildren(QPushButton)}
    assert "1x" in texts and not texts & {"+", "-", "−"}
    assert view.zoom_reset_btn.isVisible() and view.zoom_reset_btn.isEnabled()
    assert "Ctrl+plus" not in rc.CONTROL_TIP and "Ctrl+0" in rc.CONTROL_TIP
    calls = []
    view.control.zoom = lambda f: calls.append(("zoom", f)) or True
    view.control.zoom_reset = lambda: calls.append(("reset",)) or True
    C = Qt.KeyboardModifier.ControlModifier
    for k in (Qt.Key.Key_Minus, Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_0):
        key(view.canvas, k, mods=C)
        key(view.canvas, k, down=False, mods=C)
    assert calls == [("reset",)]
    view.set_pointer("place")
    assert not view.zoom_reset_btn.isVisible()


# ---------------------------------------------------------------- header layout

def test_header_is_one_compact_row_of_icon_buttons(qtbot):
    view, conn, made = make(qtbot)
    icons = (view.refresh_btn, view.keep_btn, view.rotate_btn, view.shot_btn)
    assert not view.present_btn.isVisible()  # Present was dropped: Expand is the main button
    assert view.full_btn.text() == "Expand" and view.full_btn.property("variant") == "primary"
    for b in icons:
        assert b.text() == "" and not b.icon().isNull(), b.accessibleName()
        assert b.toolTip() and b.accessibleName()
    assert [b.accessibleName() for b in icons] == ["Refresh", "Keep refreshing", "Rotate",
                                                   "Screenshot"]
    view.resize(1400, 900)
    QApplication.processEvents()
    row = [w for w in (view.pointer_seg, view.zoom_reset_btn, *icons, view.full_btn)
           if w.isVisible()]
    assert len({w.geometry().center().y() for w in row}) == 1  # one row when there's room
    view.set_pointer("place")
    QApplication.processEvents()
    panel = view.compose
    tools = [panel.add_image_btn, panel.add_ink_btn, *panel.shape_btns.values(),
             panel.remove_btn]
    assert all(b.text() == "" and b.toolTip() and b.accessibleName() for b in tools)
    assert len({b.mapTo(view, b.rect().center()).y() for b in tools}) == 1


def test_selected_item_extras_show_only_for_that_item(qtbot):
    view, conn, made = make(qtbot)
    panel = view.compose
    view.set_pointer("place")
    QApplication.processEvents()
    extras = (panel.shade_controls, panel.shade_options, panel.shape_controls)
    assert not any(w.isVisible() for w in extras)
    rect = panel.add_shape("rectangle")
    assert panel.fill_check.isVisible() and not panel.double_check.isVisible()
    assert not panel.shade_controls.isVisible()
    panel.scene.clearSelection()
    assert not any(w.isVisible() for w in extras)
    rect.setSelected(True)
    view.set_pointer("control")  # leaving Place clears the selection: the extras go
    view.set_pointer("place")
    assert not any(w.isVisible() for w in extras)


def test_narrow_main_window_wraps_the_screen_header(qtbot):
    from rmdesk.ui.app import MainWindow
    view, conn, made = make(qtbot)
    win = MainWindow(conn, Config(), save_config=lambda c: None)
    qtbot.addWidget(win)
    win.resize(700, 700)
    win.show()
    win.show_view("Screen")
    s = win.screen_view
    for name in ("control", "place"):
        s.set_pointer(name)
        if name == "place":
            s.compose.add_ink_text("Hi")
        QApplication.processEvents()
        qtbot.wait(20)
        assert win.width() == 700
        ws = [w for w in s.header.findChildren(QPushButton) if w.isVisible()]
        ws += [s.pointer_seg]
        if name == "place":
            ws += [w for w in s.compose.tools_row.findChildren(QPushButton) if w.isVisible()]
            ws += [w for w in (s.compose.tool_combo, s.compose.color_combo,
                               s.compose.thick_spin) if w.isVisible()]
        for w in ws:
            r = w.mapTo(win, w.rect().topLeft())
            assert r.x() >= 0 and r.x() + w.width() <= win.width(), (name, w.accessibleName())
            assert w.width() >= w.minimumSizeHint().width(), (name, w.accessibleName())
        clusters = [w for w in s.header.findChildren(Cluster) if w.isVisible()]
        assert len({w.y() for w in clusters}) > 1  # wrapped
        for cl in clusters:  # as units: every button of a cluster on its cluster's row
            assert cl.x() >= 0 and cl.x() + cl.width() <= s.header.width(), cl.accessibleName()
            for b in cl.findChildren(QPushButton):
                if b.isVisible():
                    assert cl.rect().contains(b.geometry()) or \
                        cl.rect().contains(QRect(b.mapTo(cl, QPoint(0, 0)), b.size())), \
                        (cl.accessibleName(), b.accessibleName())
        assert s.canvas.height() >= 200


def test_tab_order_pointer_then_actions_then_picture(qtbot):
    view, conn, made = make(qtbot)
    wait_control(qtbot, view)
    chain, w = [], view.control_btn
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [view.control_btn, view.place_btn, view.zoom_reset_btn,
            view.refresh_btn, view.keep_btn, view.rotate_btn, view.shot_btn, view.full_btn,
            view.canvas]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    for x in want:
        assert x.focusPolicy() & Qt.FocusPolicy.TabFocus, x


# ---------------------------------------------------------------- clusters

def test_header_is_four_clusters_8_inside_16_between(qtbot):
    view, conn, made = make(qtbot)
    view.compose.add_ink_text("Hi")  # the send cluster shows
    view.resize(1400, 900)
    QApplication.processEvents()
    clusters = [view.pointer_cluster, view.picture_cluster, view.output_cluster,
                view.send_cluster]
    assert [c.accessibleName() for c in clusters] == ["Pointer", "Picture", "Output", "Send"]
    assert view.pointer_seg.parentWidget() is view.pointer_cluster
    assert view.zoom_reset_btn.parentWidget() is view.pointer_cluster
    assert [w.parentWidget() for w in (view.refresh_btn, view.keep_btn, view.rotate_btn)] \
        == [view.picture_cluster] * 3
    assert [w.parentWidget() for w in (view.shot_btn, view.full_btn)] \
        == [view.output_cluster] * 2
    assert [w.parentWidget() for w in (view.undo_btn, view.send_btn)] == [view.send_cluster] * 2
    for c in clusters:
        assert c.property("cluster") and c.isVisible()
        m = c.layout().contentsMargins()
        assert (m.left(), m.top(), m.right(), m.bottom()) == (4, 4, 4, 4)
        assert c.layout().spacing() == 8 and c.height() == 44, c.accessibleName()
    # one row, 16 px between the first three; the send cluster at the right end
    assert len({c.y() for c in clusters}) == 1
    a, b, c3, d = clusters
    assert b.x() - (a.x() + a.width()) == 16 and c3.x() - (b.x() + b.width()) == 16
    assert d.x() + d.width() == view.header.width()
    # every button is 36 px tall, Present a labelled primary
    for w in (view.control_btn, view.place_btn, view.zoom_reset_btn, view.refresh_btn,
              view.keep_btn, view.rotate_btn, view.shot_btn, view.full_btn,
              view.undo_btn, view.send_btn):
        assert w.height() == 36, w.accessibleName()
    assert view.full_btn.text() == "Expand" and not view.full_btn.icon().isNull()
    assert view.full_btn.property("variant") == "primary"


def test_place_row_is_clusters(qtbot):
    view, conn, made = make(qtbot)
    panel = view.compose
    view.set_pointer("place")
    QApplication.processEvents()
    assert panel.add_image_btn.parentWidget() is panel.add_tools
    assert all(b.parentWidget() is panel.add_tools for b in panel.shape_btns.values())
    assert panel.remove_btn.parentWidget() is panel.remove_tools
    assert panel.tool_combo.parentWidget() is panel.pen_controls
    for c in (panel.add_tools, panel.remove_tools, panel.pen_controls, panel.shade_controls,
              panel.shade_options, panel.shape_controls):
        assert isinstance(c, Cluster) and c.layout().spacing() == 8
    assert panel.tools_row.layout().spacing() == 16


# ---------------------------------------------------------------- the in-window hint

def _enter(w):
    from PySide6.QtGui import QEnterEvent
    p = QPointF(w.width() / 2, w.height() / 2)
    QApplication.sendEvent(w, QEnterEvent(p, p, QPointF(w.mapToGlobal(p))))


def _leave(w):
    QApplication.sendEvent(w, QEvent(QEvent.Type.Leave))


def test_hint_shows_on_hover_and_clears_on_leave(qtbot):
    view, conn, made = make(qtbot)
    hint = view.hint
    assert hint.text() == "" and not hint.label.isVisible()
    want = {view.refresh_btn: "Refresh the picture", view.rotate_btn: "Rotate the view",
            view.shot_btn: "Save a screenshot",
            view.full_btn: "Expand: hide everything but the picture (F11)",
            view.zoom_reset_btn: "1x: reset the tablet's zoom"}
    for w, text in want.items():
        _enter(w)
        assert hint.text() == text and hint.label.isVisible(), text
        assert hint.label.parentWidget() is view  # in the window, not a popup
        assert not hint.label.isWindow()
        lab = hint.label.geometry()
        assert lab.top() >= w.mapTo(view, QPoint(0, w.height())).y()  # just under it
        assert lab.left() >= 0 and lab.right() < view.width()
        _leave(w)
        assert hint.text() == "" and not hint.label.isVisible(), text
        assert w.toolTip()  # the normal tooltip stays too
    view.compose.add_ink_text("Hi")
    QApplication.processEvents()
    _enter(view.undo_btn)
    assert hint.text() == "Undo last send"
    _leave(view.undo_btn)


def test_hint_for_the_place_tools(qtbot):
    view, conn, made = make(qtbot)
    panel = view.compose
    view.set_pointer("place")
    QApplication.processEvents()
    want = [(panel.add_image_btn, "Add picture"), (panel.add_ink_btn, "Add ink text"),
            (panel.add_rect_btn, "Rectangle"), (panel.add_line_btn, "Line"),
            (panel.add_arrow_btn, "Arrow"), (panel.add_note_btn, "Sticky note"),
            (panel.remove_btn, "Remove selected")]
    for w, text in want:
        _enter(w)
        assert view.hint.text() == text, text
        _leave(w)
        assert view.hint.text() == "", text


def test_hint_shows_on_keyboard_focus(qtbot):
    view, conn, made = make(qtbot)
    view.window().activateWindow()
    view.rotate_btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(view.rotate_btn.hasFocus, timeout=2000)
    assert view.hint.text() == "Rotate the view"
    view.shot_btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(view.shot_btn.hasFocus, timeout=2000)
    assert view.hint.text() == "Save a screenshot"
    view.canvas.setFocus(Qt.FocusReason.TabFocusReason)  # focus leaves the header
    qtbot.waitUntil(lambda: not view.shot_btn.hasFocus(), timeout=2000)
    assert view.hint.text() == ""


def test_keep_refreshing_hint_says_its_state(qtbot):
    view, conn, made = make(qtbot)  # keep_default=False
    assert not view.keep_btn.isChecked()
    _enter(view.keep_btn)
    assert view.hint.text() == "Keep refreshing: off"
    view.keep_btn.click()  # changes while hovered: the hint follows
    assert view.keep_btn.isChecked() and view.hint.text() == "Keep refreshing: on"
    assert view.keep_btn.accessibleDescription() == "Keep refreshing: on"
    view.set_keep_refreshing(False)  # K in Present
    assert view.hint.text() == "Keep refreshing: off"
    _leave(view.keep_btn)
    assert view.hint.text() == ""


def test_keep_refreshing_on_is_an_accent_icon_on_a_tint(qtbot):
    from rmdesk.ui.theme import qss
    QApplication.instance().setStyleSheet(qss())
    try:
        view, conn, made = make(qtbot)
        b = view.keep_btn

        def colours():
            img = b.grab().toImage()
            bg = img.pixelColor(3, b.height() // 2)
            icon = b.icon().pixmap(16).toImage()
            ink = [icon.pixelColor(x, y) for x in range(16) for y in range(16)
                   if icon.pixelColor(x, y).alpha() > 200]
            return bg, ink[0]
        bg_off, ink_off = colours()
        view.set_keep_refreshing(True)
        QApplication.processEvents()
        bg_on, ink_on = colours()
        accent = QColor(TOKENS["accent-text"])
        assert ink_on.rgb() == accent.rgb() and ink_off.rgb() != accent.rgb()
        assert bg_on.red() > bg_on.green() + 10 and bg_on != bg_off  # accent-tinted
    finally:
        QApplication.instance().setStyleSheet("")


# ---------------------------------------------------------------- Control in Expand

def test_expand_keeps_control_with_the_picture_focused(qtbot):
    from PySide6.QtTest import QTest
    view, conn, made = make(qtbot)
    view.window().activateWindow()
    wait_control(qtbot, view)
    view.refresh_btn.setFocus(Qt.FocusReason.TabFocusReason)  # focus somewhere in the header
    view.set_fullscreen(True)
    assert view.fullscreen and not view.header.isVisible()
    assert view.control.active and view.pointer == "control"
    qtbot.waitUntil(view.canvas.hasFocus, timeout=2000)  # the picture, not the ScreenView
    assert QApplication.focusWidget() is view.canvas
    assert view.canvas.outline_visible() and _accent_on_left_edge(view)
    # a tap and a key go to the tablet while expanded
    touch = next(w for w in made if w.path.endswith("event2"))
    kb = next(w for w in made if w.path.endswith("event3"))
    n_touch, n_kb = len(touch.items), len(kb.items)
    vp = view.canvas.viewport()
    p = QPointF(vp.width() / 2, vp.height() / 2)
    press(vp, p)
    release(vp, p)
    assert len(touch.items) > n_touch and (3, 0x39, -1) in touch.events()
    QTest.keyClick(view.canvas, Qt.Key.Key_A)
    assert len(kb.items) > n_kb
    # nothing appears over the picture on a mouse move (Esc or F11 leave)
    move(vp, p + QPointF(5, 5))
    assert not view.exit_full_btn.isVisible()
    # Esc (reserved: never sent) comes up from the picture and leaves Expand
    n_kb = len(kb.items)
    QTest.keyClick(view.canvas, Qt.Key.Key_Escape)
    assert not view.fullscreen and view.header.isVisible()
    assert len(kb.items) == n_kb  # Esc did not reach the tablet
    assert view.control.active and view.canvas.hasFocus()  # leaving kept Control on


def test_expand_from_place_switches_to_control_and_esc_leaves(qtbot):
    from PySide6.QtTest import QTest
    from rmdesk.ui.app import MainWindow
    view, conn, made = make(qtbot)
    view.hide()  # only the main window's Screen view from here
    win = MainWindow(conn, Config(), save_config=lambda c: None)
    qtbot.addWidget(win)
    win.show()
    win.activateWindow()
    win.show_view("Screen")
    s = win.screen_view
    s.set_pointer("place")
    win.toggle_fullscreen()  # F11 from Place: Control, expanded
    assert s.fullscreen and s.pointer == "control" and not s.header.isVisible()
    qtbot.waitUntil(lambda: s.control.active, timeout=3000)
    qtbot.waitUntil(s.canvas.hasFocus, timeout=2000)
    QTest.keyClick(s.canvas, Qt.Key.Key_Escape)
    assert not s.fullscreen and s.header.isVisible() and s.control.active
    s.control.stop()  # Control can't run: Esc still leaves, from the view itself
    win.toggle_fullscreen()
    assert s.fullscreen and not s.control.active
    qtbot.waitUntil(lambda: QApplication.focusWidget() is s, timeout=2000)
    QTest.keyClick(s, Qt.Key.Key_Escape)
    assert not s.fullscreen


def test_hinted_buttons_show_no_second_popup_tooltip(qtbot):
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QHelpEvent
    view, conn, made = make(qtbot)
    e = QHelpEvent(QEvent.Type.ToolTip, QPoint(5, 5), view.refresh_btn.mapToGlobal(QPoint(5, 5)))
    assert view.hint.eventFilter(view.refresh_btn, e) is True  # swallowed
    assert view.refresh_btn.toolTip()  # kept for accessibility
