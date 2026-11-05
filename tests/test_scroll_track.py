"""Placed items follow the page as it scrolls: the per-frame tracker (compose.scroll_track)
and the build keeping parts past the frame's ends in the keyboard layout (compose.build)."""
import statistics
import time

import numpy as np
import pytest

from rmdesk.compose import build, scroll_track as st
from rmdesk.compose import shade
from rmdesk.compose.frame import KEYBOARD, PORTRAIT_UI, Anchored, frame_to_page
from rmdesk.compose.items import Box, ImageItem, InkTextItem, PenStyle
from rmdesk.compose.shade import ShadeResult
from tests.scroll_sim import add_dots, add_lines, blank_page, frame_at, text_page


@pytest.fixture(scope="module")
def page():
    return text_page(seed=3)


def _page_y(fx, scroll):
    return frame_to_page(fx, 936, "portrait", layout=KEYBOARD, scroll=scroll)[1]


# -- the tracker ------------------------------------------------------------------------

@pytest.mark.parametrize("scrolls", [[300, 290, 250, 200, 140, 600, 610],
                                     [0, 40, 400, 900, 880, 300],
                                     [700, 699, 698, 700, 1300, 1290]])
def test_known_scrolls_move_items_by_exactly_as_much(page, scrolls):
    """An item kept at frame x + every step's dx stays on the same spot of the page."""
    tr = st.Tracker()
    assert tr.step(frame_at(page, scrolls[0])) == st.Step(0, st.START)
    fx = 900.0
    want = _page_y(fx, scrolls[0])
    for s0, s1 in zip(scrolls, scrolls[1:]):
        step = tr.step(frame_at(page, s1))
        assert step.dx == s1 - s0, (s0, s1, step)
        fx += step.dx
        assert _page_y(fx, s1) == want  # the item's page position is unchanged
        assert not tr.warn


def test_the_direction_follows_the_page(page):
    """Scrolling down the page (scroll grows) moves its content toward higher frame x."""
    tr = st.Tracker()
    tr.step(frame_at(page, 100))
    assert tr.step(frame_at(page, 160)).dx == 60
    assert tr.step(frame_at(page, 130)).dx == -30


def test_a_long_scroll_in_small_steps_adds_up_exactly(page):
    tr = st.Tracker()
    tr.step(frame_at(page, 0))
    total = sum(tr.step(frame_at(page, s)).dx for s in range(25, 2501, 25))
    assert total == 2500


def test_a_fast_scroll_past_the_reference_steps_from_the_last_frame(page):
    tr = st.Tracker()
    tr.step(frame_at(page, 0))
    moved = [tr.step(frame_at(page, s)).dx for s in (450, 900, 1350, 1800)]
    assert moved == [450, 450, 450, 450]


def test_an_unchanged_picture_moves_nothing(page):
    tr = st.Tracker()
    f = frame_at(page, 300)
    tr.step(f)
    assert tr.step(f.copy()) == st.Step(0, st.STILL)


def test_a_blank_page_moves_nothing():
    pg = blank_page()
    tr = st.Tracker()
    tr.step(frame_at(pg, 0))
    assert tr.step(frame_at(pg, 120)).dx == 0 and not tr.warn


@pytest.mark.parametrize("template", [add_lines, add_dots])
def test_a_repeating_template_moves_nothing_and_warns(template):
    """Only a template on screen: a scroll can't be told from its period, so nothing moves;
    everything inked changed, so the items may no longer match (said)."""
    pg = template(blank_page())
    tr = st.Tracker()
    tr.step(frame_at(pg, 0))
    step = tr.step(frame_at(pg, 120))
    assert step.dx == 0 and tr.warn


def test_a_non_rigid_change_moves_nothing(page):
    """A menu over part of the page: nothing moves, and it is not another page."""
    tr = st.Tracker()
    f0 = frame_at(page, 300)
    tr.step(f0)
    f1 = f0.copy()
    f1[300:1500, 200:700] = 255
    f1[300:1500, 200:220] = 0
    step = tr.step(f1)
    assert step == st.Step(0, st.UNCLEAR) and not tr.warn
    # the menu closes: the page lines up with the reference again, still nothing to move
    assert tr.step(f0.copy()).dx == 0
    # and a scroll after that is followed
    assert tr.step(frame_at(page, 340)).dx == 40


def test_another_page_warns_and_moves_nothing(page):
    tr = st.Tracker()
    tr.step(frame_at(page, 300))
    other = text_page(seed=11)
    step = tr.step(frame_at(other, 300))
    assert step == st.Step(0, st.CHANGED) and tr.warn
    assert tr.step(frame_at(other, 350)).dx == 0  # its scroll isn't followed either
    assert tr.warn
    # back on the first page, where the items belong: the warning goes, the scroll is followed
    step = tr.step(frame_at(page, 320))
    assert step.dx == 20 and not tr.warn


def test_the_cost_per_frame_stays_within_budget(page):
    """Budget ~15 ms per frame; measured ~2-6 ms here. A generous bound: no flaky CI."""
    tr = st.Tracker()
    frames = [frame_at(page, s) for s in range(300, 0, -10)]
    tr.step(frames[0])
    times = []
    for f in frames[1:]:
        t0 = time.perf_counter()
        tr.step(f)
        times.append((time.perf_counter() - t0) * 1000)
    assert statistics.median(times) < 30, times


def test_the_coarse_shift_agrees_with_shift_between(page):
    from rmdesk.compose.scroll_measure import shift_between
    for s0, s1 in ((300, 0), (120, 450), (10, 9)):
        a, b = frame_at(page, s0), frame_at(page, s1)
        assert st.shift(st.sample(a), st.sample(b))[0] == shift_between(a, b)[0] == s0 - s1


# -- the build keeps what is past the frame (keyboard layout) -----------------------------

PEN = PenStyle("fineliner")


def _item(x, w=200.0):
    return InkTextItem(text="x", box=Box(x, 800, w, 100), size=10, pen=PEN)


@pytest.mark.parametrize("x", [-600.0, -150.0, 1500.0, 1300.0])
def test_keyboard_lines_past_the_frame_keep_their_page_y(x):
    """An item at frame x < 0 (further down the page) or > 1404 (further up) is sent at the
    page y frame_to_page gives it, not cut at the frame's edge."""
    item = _item(x)
    stroke = [(0.0, 50.0), (200.0, 50.0)]  # local: along frame x, at frame y 850
    scroll = 900.0
    lines = build.frame_to_lines(item, [stroke], None, "portrait", layout=KEYBOARD,
                                 scroll=scroll)
    ys = sorted(p.y for ln in lines for p in ln.points)
    want = sorted(frame_to_page(x + sx, 850, "portrait", layout=KEYBOARD, scroll=scroll)[1]
                  for sx, _ in stroke)
    assert ys == pytest.approx(want)
    assert all(p.x == pytest.approx(850 - 936) for ln in lines for p in ln.points)


def test_keyboard_lines_above_the_page_top_are_cut_there():
    """y >= 0 still holds: at scroll 100, frame x 1504 is the page's top."""
    item = _item(1400.0, w=300.0)
    lines = build.frame_to_lines(item, [[(0.0, 50.0), (300.0, 50.0)]], None, "portrait",
                                 layout=KEYBOARD, scroll=100.0)
    ys = [p.y for ln in lines for p in ln.points]
    assert min(ys) == pytest.approx(0.0) and max(ys) == pytest.approx(104.0)


def test_portrait_still_clips_to_the_frame():
    item = _item(-150.0)
    lines = build.frame_to_lines(item, [[(0.0, 50.0), (200.0, 50.0)]], None, "portrait")
    xs = sorted(p.x for ln in lines for p in ln.points)
    assert xs == pytest.approx([-702.0, -702.0 + 50.0])  # frame x 0..50 only
    assert not build.has_lines_on_page(_item(-600.0), [[(0.0, 50.0), (200.0, 50.0)]])


def test_has_lines_on_page_in_the_keyboard_layout():
    far = _item(-3000.0)
    assert build.has_lines_on_page(far, [[(0.0, 50.0), (200.0, 50.0)]], layout=KEYBOARD)
    beside = InkTextItem(text="x", box=Box(500, 1700, 100, 100), size=10, pen=PEN)
    assert not build.has_lines_on_page(beside, [[(0.0, 50.0), (100.0, 50.0)]],
                                       layout=KEYBOARD)  # frame y > 1638: off the page width


@pytest.mark.parametrize("x", [-400.0, 1500.0])
def test_the_anchored_path_keeps_them_too(x):
    a = Anchored(const=1200.0, origin_x=-464.0)
    item = _item(x)
    lines = build.frame_to_lines(item, [[(0.0, 50.0), (200.0, 50.0)]], None, "portrait",
                                 layout=KEYBOARD, anchored=a)
    got = sorted((p.x, p.y) for ln in lines for p in ln.points)
    want = sorted((850 - 936 + 464, 1200.0 - fx) for fx in (x, x + 200))
    assert got == pytest.approx(want)
    # the same as the absolute mapping, shifted (one scroll for every item)
    absolute = build.frame_to_lines(item, [[(0.0, 50.0), (200.0, 50.0)]], None, "portrait",
                                    layout=KEYBOARD, scroll=1000.0)
    ys = sorted(p.y for ln in absolute for p in ln.points)
    assert sorted(g[1] for g in got) == pytest.approx([y + 1200.0 - 2404 for y in ys])


def _picture(tmp_path):
    import cv2
    img = np.full((100, 200), 255, np.uint8)
    img[20:80, 20:180] = 0
    path = tmp_path / "p.png"
    cv2.imwrite(str(path), img)
    return path


def test_a_shaded_picture_past_the_frame_is_shaded_and_sent_with_the_wide_region(tmp_path):
    path = _picture(tmp_path)
    item = ImageItem(path=path, box=Box(-500, 800, 400, 200), pen=PenStyle("pencil"))
    narrow = shade.shade_item(item)
    assert narrow.strokes == []  # the frame region: nothing of it is on the frame
    with shade.region(shade.KEYBOARD_REGION):
        wide = shade.shade_item(item)
    assert wide.strokes
    lines = build.frame_shade_to_lines(item, wide, item.pen, "portrait", layout=KEYBOARD,
                                       scroll=300.0)
    ys = [p.y for ln in lines for p in ln.points]
    # frame x -500..-100 at scroll 300: page y 1804..2204
    assert lines and min(ys) >= 1804 - 1 and max(ys) <= 2204 + 1
    assert build.frame_shade_to_lines(item, wide, item.pen, "portrait",
                                      layout=PORTRAIT_UI) == []  # portrait: the frame clip


def test_the_shading_region_is_per_block():
    assert shade._REGION.get() == shade.FRAME_REGION
    with shade.region(shade.KEYBOARD_REGION):
        assert shade._REGION.get() == shade.KEYBOARD_REGION
    assert shade._REGION.get() == shade.FRAME_REGION
