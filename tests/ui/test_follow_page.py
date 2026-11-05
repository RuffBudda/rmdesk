"""Placed items follow the tablet page as it scrolls (LiveCompose + compose.scroll_track):
moved with a clear scroll, kept still otherwise, faded and counted past the picture's ends,
and sent with their page coordinates."""
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from rmdesk.compose import build, frame as fr, scroll_fit
from rmdesk.stream.client import Frame
from rmdesk.ui.compose import messages as msg
from rmdesk.ui.live_compose import OFFSCREEN_OPACITY, LiveCompose
from tests.scroll_sim import frame_at, text_page
from tests.ui.test_live_compose import (_draw, _shaded, fw, folio_handler, make_device,  # noqa: F401
                                        make_screen, record_send)

PAGE = text_page(seed=3)
OTHER = text_page(seed=11)


def show(view, gray):
    """A new stream frame on the Screen view."""
    view.frame = Frame(1404, 1872, gray)
    view.frame_changed.emit()


def page_y(fx, scroll):
    return fr.frame_to_page(fx, 936, "portrait", layout=fr.KEYBOARD, scroll=scroll)[1]


def test_a_clear_scroll_moves_every_item_with_the_page(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    a = panel.add_ink_text("Hello")
    b = panel.add_shape("rectangle")
    a.setPos(600, 700)
    b.setPos(200, 1100)
    show(view, frame_at(PAGE, 300))  # the picture they were placed on
    xs = (a.pos().x(), b.pos().x())
    want = [page_y(x, 300) for x in xs]
    for s in (280, 230, 400, 650):
        show(view, frame_at(PAGE, s))
        assert [page_y(it.pos().x(), s) for it in (a, b)] == want  # the same page spot
    assert a.pos().y() == 700 and b.pos().y() == 1100  # only along frame x
    assert panel.track_ms < 50  # measured per frame (budget ~15 ms; generous here)
    assert not panel.page_box.isVisibleTo(panel)


def test_a_non_rigid_change_moves_nothing_and_another_page_warns(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    it = panel.add_ink_text("Hello")
    show(view, frame_at(PAGE, 300))
    x = it.pos().x()
    menu = frame_at(PAGE, 300)
    menu[300:1500, 200:700] = 255  # a panel over part of the page
    show(view, menu)
    assert it.pos().x() == x and not panel.page_box.isVisibleTo(panel)
    show(view, frame_at(OTHER, 300))  # a page turn
    assert it.pos().x() == x
    assert panel.page_box.isVisibleTo(panel) and panel.page_chip.text() == msg.PAGE_CHANGED
    assert panel.placed_items() == [it]  # the items stay where they are
    show(view, frame_at(PAGE, 320))  # back: lined up again and followed
    assert it.pos().x() == x + 20 and not panel.page_box.isVisibleTo(panel)


def test_no_items_no_tracking(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    show(view, frame_at(OTHER, 300))
    assert panel._tracker.ref is None and not panel.page_box.isVisibleTo(panel)


def _centre_on_viewport(panel, item):
    return panel.view.mapFromScene(item.mapToScene(QPointF(item.w / 2, item.h / 2)))


def test_a_held_item_does_not_move_mid_drag(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    held = panel.add_shape("rectangle")
    other = panel.add_shape("line")
    held.setPos(500, 600)
    other.setPos(500, 1200)
    show(view, frame_at(PAGE, 300))
    qtbot.mousePress(panel.view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_centre_on_viewport(panel, held))
    assert panel.scene.mouseGrabberItem() is held
    x_other = other.pos().x()
    held.setPos(held.pos() + QPointF(30, 10))  # the drag
    dropped = held.pos()
    show(view, frame_at(PAGE, 260))
    assert held.pos() == dropped  # still while held
    assert other.pos().x() == x_other - 40
    qtbot.mouseRelease(panel.view.viewport(), Qt.MouseButton.LeftButton,
                       pos=panel.view.mapFromScene(held.mapToScene(QPointF(held.w / 2,
                                                                          held.h / 2))))
    QApplication.processEvents()
    qtbot.waitUntil(lambda: panel._held is None, timeout=2000)
    assert held.pos() == dropped  # dropped where the user saw it on the current picture


def test_a_clicked_item_gets_its_shift_after_the_release(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    it = panel.add_shape("rectangle")
    it.setPos(500, 600)
    show(view, frame_at(PAGE, 300))
    at = _centre_on_viewport(panel, it)
    qtbot.mousePress(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=at)
    show(view, frame_at(PAGE, 260))
    assert it.pos().x() == 500  # not mid-hold
    qtbot.mouseRelease(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=at)
    qtbot.waitUntil(lambda: panel._held is None, timeout=2000)
    assert it.pos().x() == 460


def test_items_past_the_picture_are_faded_counted_and_marked(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.set_layout(fr.KEYBOARD)
    on = panel.add_ink_text("on")
    up = panel.add_ink_text("up")
    down1, down2 = panel.add_ink_text("d1"), panel.add_ink_text("d2")
    on.setPos(600, 800)
    up.setPos(1600, 800)  # frame x > 1404: further up the page
    down1.setPos(-500, 800)
    down2.setPos(-900, 900)
    QApplication.processEvents()
    panel._refresh_offscreen()
    assert on.opacity() == 1.0
    assert all(i.opacity() == OFFSCREEN_OPACITY for i in (up, down1, down2))
    assert panel.offscreen_counts() == {"above": 1, "below": 2}
    texts = sorted(t for _p, t in panel.view.edge_marks)
    assert texts == ["1 item above", "2 items below"]
    page = panel.view.page_rect()
    for r, _t in panel.view.mark_rects():
        assert page.contains(r)  # drawn inside the picture's edge
    assert panel.badge.text() == "4"  # they still count
    on.setPos(-400, 800)  # dragged off too
    QApplication.processEvents()
    assert sorted(t for _p, t in panel.view.edge_marks) == ["1 item above", "3 items below"]
    up.setPos(1000, 800)  # back on the picture
    QApplication.processEvents()
    assert up.opacity() == 1.0


def test_an_offscreen_item_is_sent_at_its_page_position(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.notebook.opened import OpenedPage
    from tests.ui.test_live_compose import NB, ORIG, PG2
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    monkeypatch.setattr(scroll_fit, "fit_details", lambda picture, points: {"scroll": 678.0})
    out = []
    real = build.frame_to_lines
    monkeypatch.setattr(build, "frame_to_lines",
                        lambda *a, **k: out.append((a, k, real(*a, **k))) or out[-1][2])
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = Frame(1404, 1872, frame_at(PAGE, 300))
    dev.run_handler = folio_handler("Landscape")
    panel.set_layout(fr.KEYBOARD)
    item = panel.add_ink_text("Hi")
    item.setPos(-600, 800)  # wholly past the frame's low-x end: further down the page
    spec = item.send_data()
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and (cfg.last_send is not None
                                                or bool(panel.message.text())), timeout=10000)
    assert cfg.last_send is not None, panel.message.text()
    (args, kw, lines), = out
    assert kw == {"layout": fr.KEYBOARD, "scroll": 678.0}
    model, polylines, size = spec
    placed = [pt for pl in build.place(model, polylines, size) for pt in pl]
    got = [(p.x, p.y) for ln in lines for p in ln.points]
    want = [fr.frame_to_page(x, y, "portrait", layout=fr.KEYBOARD, scroll=678.0)
            for x, y in placed]
    assert got == pytest.approx(want)
    assert min(y for _x, y in got) > 1404 + 678  # below what the frame shows
    assert calls and calls[0]["n"] == len(lines) > 0


def test_a_send_refreshes_the_cached_layout(qtbot, tmp_path, fw, monkeypatch):
    record_send(monkeypatch, tmp_path)
    monkeypatch.setattr(scroll_fit, "fit_details", lambda picture, points: {"scroll": 0.0})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = Frame(1404, 1872, frame_at(PAGE, 0))
    assert panel.layout is None
    dev.run_handler = folio_handler("Landscape")
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and cfg.last_send is not None, timeout=10000)
    assert panel.layout == fr.KEYBOARD


def test_the_layout_is_asked_on_connect(qtbot, tmp_path, fw, monkeypatch):
    monkeypatch.setattr(LiveCompose, "probe_layout_on_connect", True)
    dev = make_device()
    dev.run_handler = folio_handler("Landscape")
    view, panel, *_ = make_screen(qtbot, tmp_path, dev=dev)
    qtbot.waitUntil(lambda: panel.layout == fr.KEYBOARD, timeout=5000)


def test_a_picture_past_the_frame_is_previewed_in_the_keyboard_layout(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "a.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    item.setPos(-item.w - 100, 800)  # wholly off the frame
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert item.preview_strokes() == []  # portrait (or unknown): the frame clip, as before
    assert not panel._may_draw(item, item.send_data())
    panel.set_layout(fr.KEYBOARD)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert item.preview_strokes()  # shaded past the frame: it will be sent
    assert panel._may_draw(item, item.send_data())


def test_portrait_still_refuses_an_item_wholly_off_the_frame(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    panel.set_layout(fr.PORTRAIT_UI)
    item = panel.add_ink_text("Hi")
    item.setPos(-800, 800)
    panel.send()
    assert panel.message.text() == msg.NOTHING_TO_SEND and calls == []


def test_a_resized_item_gets_its_shift_after_the_release(qtbot, tmp_path, fw):
    """Resizing keeps the item's other corner on its spot of the page: the scroll that came
    meanwhile is applied once the corner is let go."""
    view, panel, *_ = make_screen(qtbot, tmp_path)
    show(view, frame_at(PAGE, 300))
    it = panel.add_shape("rectangle")
    it.setPos(500, 600)
    show(view, frame_at(PAGE, 300))
    r = it.handle_rect()
    corner = panel.view.mapFromScene(it.mapToScene(r.center()))
    qtbot.mousePress(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
    assert it._resizing
    show(view, frame_at(PAGE, 330))
    assert it.pos().x() == 500
    qtbot.mouseRelease(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
    qtbot.waitUntil(lambda: panel._held is None, timeout=2000)
    assert it.pos().x() == 530
