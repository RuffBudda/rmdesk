"""Following the page with the finger and the picture together (compose.scroll_fuse): a drag on
a template-only page, the picture correcting the finger, a flick's momentum, taps, page turns,
pinches, and the portrait layout as before."""
import numpy as np
import pytest

from rmdesk.compose import scroll_fuse as sf
from rmdesk.compose import scroll_track as st
from rmdesk.compose.frame import KEYBOARD, frame_to_page
from rmdesk.remote.touch_watch import DOWN, MOVE, UP, ZOOM, TouchEvent
from tests.scroll_sim import add_dots, add_lines, blank_page, frame_at, text_page


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def dots():
    return add_dots(blank_page())


@pytest.fixture(scope="module")
def text():
    return add_dots(text_page(seed=5))


def follower(keyboard=True):
    c = Clock()
    f = sf.Follower(clock=c)
    f.keyboard = keyboard
    return f, c


def drag(f, c, x0, x1, y=900.0, steps=10, dt=0.02, t0=0.0, lift=True):
    """A one-finger drag from frame x x0 to x1 (tablet time from t0, the clock along)."""
    f.touch(TouchEvent(DOWN, x0, y, t=t0))
    last = x0
    for i in range(1, steps + 1):
        x = x0 + (x1 - x0) * i / steps
        c.t += dt
        f.touch(TouchEvent(MOVE, x, y, x - last, 0.0, t0 + dt * i))
        last = x
    if lift:
        f.touch(TouchEvent(UP, x1, y, t=t0 + dt * steps))


def settle(f, c, gray):
    c.t += 1.0
    return f.step(gray)


def page_y(fx, scroll):
    return frame_to_page(fx, 936, "portrait", layout=KEYBOARD, scroll=scroll)[1]


def test_the_picture_alone_cant_follow_a_dot_page(dots):
    tr = st.Tracker()
    tr.step(frame_at(dots, 300))
    assert tr.step(frame_at(dots, 437)).dx != 137  # periodic: ambiguous (the reason for this)


def test_template_period_is_found(dots):
    p = sf.template_period(st.sample(frame_at(dots, 300)))
    assert p is not None and abs(p - 50) <= sf.DS
    assert sf.window_for(p) < 25
    assert sf.template_period(st.sample(frame_at(add_lines(blank_page(), 70), 0))) in (68, 72)


def test_a_drag_on_a_dot_page_moves_items_exactly_with_the_finger(dots):
    f, c = follower()
    f.step(frame_at(dots, 300))
    drag(f, c, 500, 637)  # the finger 137 px toward higher frame x: the page scrolls on 137
    step = f.step(frame_at(dots, 437))
    assert step.dx == 137 and f.last_how == sf.NEAR and not f.warn
    assert settle(f, c, frame_at(dots, 437)).dx == 0
    assert f.pred is None  # settled: back to the picture alone


def test_the_sign_keeps_the_page_point_under_the_finger(dots):
    """A drag toward decreasing frame x scrolls toward the page's top (scroll falls)."""
    f, c = follower()
    f.step(frame_at(dots, 600))
    fx = 900.0
    want = page_y(fx, 600)
    drag(f, c, 900, 690)
    step = f.step(frame_at(dots, 390))
    assert step.dx == -210 and page_y(fx + step.dx, 390) == want


def test_a_drag_followed_frame_by_frame_with_the_picture_lagging(dots):
    f, c = follower()
    f.step(frame_at(dots, 300))
    total = 0
    f.touch(TouchEvent(DOWN, 400, 900, t=0))
    x = 400
    for k in range(1, 8):  # 30 px per event, a frame after each
        c.t += 0.03
        f.touch(TouchEvent(MOVE, x + 30, 900, 30, 0, 0.03 * k))
        x += 30
        total += f.step(frame_at(dots, 300 + 30 * k)).dx
    f.touch(TouchEvent(UP, x, 900, t=0.3))
    total += f.step(frame_at(dots, 510)).dx
    total += settle(f, c, frame_at(dots, 510)).dx
    assert total == 210


def test_the_picture_corrects_a_finger_6_px_off(text):
    f, c = follower()
    f.step(frame_at(text, 300))
    drag(f, c, 500, 631)  # the finger says 131
    step = f.step(frame_at(text, 437))  # the page went 137
    assert step.dx == 137 and f.last_how == sf.NEAR
    settle(f, c, frame_at(text, 437))
    drag(f, c, 700, 650)  # the next drag starts from where the page is
    assert f.step(frame_at(text, 387)).dx == -50


def test_a_flick_coasts_on_with_the_picture(dots):
    f, c = follower()
    f.step(frame_at(dots, 300))
    s, total = 300, 0
    f.touch(TouchEvent(DOWN, 300, 900, t=0))
    x = 300
    for k in range(1, 3):  # fast: 100 px per 0.05 s (2000 px/s)
        c.t += 0.05
        x += 100
        f.touch(TouchEvent(MOVE, x, 900, 100, 0, 0.05 * k))
        s += 100
        total += f.step(frame_at(dots, s)).dx
    f.touch(TouchEvent(UP, x, 900, t=0.1))
    assert f.coast is not None
    for step in (80, 60, 45, 30, 15, 5):  # momentum: the page goes on by itself
        c.t += 0.05
        s += step
        total += f.step(frame_at(dots, s)).dx
    total += settle(f, c, frame_at(dots, s)).dx
    assert total == s - 300 == 435
    assert f.coast is None and f.pred is None and not f.warn


def test_a_slow_lift_does_not_coast(dots):
    f, c = follower()
    f.step(frame_at(dots, 300))
    drag(f, c, 500, 560, steps=10, dt=0.05)  # 120 px/s
    assert f.coast is None
    assert f.step(frame_at(dots, 360)).dx == 60


def test_a_tap_moves_nothing(dots, text):
    f, c = follower()
    f.step(frame_at(dots, 300))
    drag(f, c, 500, 506, steps=3)  # under TAP_PX: a tap
    assert f.pred is None and f.drag is None
    assert f.step(frame_at(dots, 300)).dx == 0 and not f.warn
    f, c = follower()
    f.step(frame_at(text, 300))
    drag(f, c, 500, 506, steps=3)
    assert f.step(frame_at(text, 330)).dx == 30  # the picture alone, as before


def test_lateral_travel_does_not_scroll(dots):
    f, c = follower()
    f.step(frame_at(dots, 300))
    f.touch(TouchEvent(DOWN, 500, 400, t=0))
    for k in range(1, 11):
        c.t += 0.02
        f.touch(TouchEvent(MOVE, 500, 400 + 30 * k, 0, 30, 0.02 * k))
    f.touch(TouchEvent(UP, 500, 700, t=0.2))
    assert f.pred == f.tracker.applied == 0
    assert f.step(frame_at(dots, 300)).dx == 0


def test_a_page_turn_with_no_finger_still_warns(text):
    f, c = follower()
    f.step(frame_at(text, 300))
    step = f.step(frame_at(text_page(seed=11), 300))
    assert step.dx == 0 and step.status == st.CHANGED and f.warn


def test_a_page_turn_during_a_drag_warns(text):
    f, c = follower()
    f.step(frame_at(text, 300))
    drag(f, c, 500, 600)
    step = f.step(frame_at(text_page(seed=11), 400))
    assert step.dx == 0 and f.warn and f.last_how == sf.LOST


def test_a_pinch_is_not_followed_and_warns_on_a_changed_picture(text):
    f, c = follower()
    g0 = frame_at(text, 300)
    f.step(g0)
    f.touch(TouchEvent(ZOOM))
    assert f.zoomed and f.pred is None
    idx = (702 + (np.arange(1404) - 702) / 2).astype(int)
    zoomed = g0[:, idx]  # the picture scaled up 2x round the middle
    step = f.step(zoomed)
    assert step.dx == 0 and f.warn


def test_portrait_ignores_the_finger(dots, text):
    f, c = follower(keyboard=False)
    f.step(frame_at(dots, 300))
    drag(f, c, 500, 637)
    assert f.pred is None
    assert f.step(frame_at(dots, 437)).dx != 137  # as before: the picture alone
    f, c = follower(keyboard=False)
    f.step(frame_at(text, 300))
    drag(f, c, 500, 637)
    assert f.step(frame_at(text, 330)).dx == 30  # a clear shift still moves the items


def test_a_long_drag_rebases_and_adds_up(text):
    f, c = follower()
    f.step(frame_at(text, 0))
    total = 0
    for i in range(1, 9):  # 8 drags of 150: past REBASE
        drag(f, c, 400, 550)
        total += f.step(frame_at(text, 150 * i)).dx
        total += settle(f, c, frame_at(text, 150 * i)).dx
    assert total == 1200


def test_cost_stays_in_budget(dots):
    import time
    f, c = follower()
    f.step(frame_at(dots, 300))
    frames = [frame_at(dots, 300 + 20 * k) for k in range(1, 11)]
    f.touch(TouchEvent(DOWN, 400, 900, t=0))
    t0 = time.perf_counter()
    for k, g in enumerate(frames, 1):
        c.t += 0.03
        f.touch(TouchEvent(MOVE, 400 + 20 * k, 900, 20, 0, 0.03 * k))
        assert f.step(g).dx == 20
    per = (time.perf_counter() - t0) / len(frames) * 1000
    assert per < 30  # ms (the picture tracker's own budget is ~15 on the PC)
