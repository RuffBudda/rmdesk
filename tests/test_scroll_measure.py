"""Measuring a typed-only page's scroll by scrolling it to its top (compose.scroll_measure)."""
import numpy as np
import pytest

from rmdesk.compose import frame as fr
from rmdesk.compose import scroll_measure as sm
from tests.scroll_sim import (add_dots, add_lines, add_text, blank_page, frame_at, text_page)


@pytest.fixture(scope="module")
def page():
    return text_page(seed=3)


# -- shift_between ----------------------------------------------------------------------

def test_no_shift_is_zero_and_confident(page):
    f = frame_at(page, 400)
    d, conf = sm.shift_between(f, f.copy())
    assert d == 0 and conf >= sm.MARGIN


@pytest.mark.parametrize("s0", [7, 120, 333, 700, 1000])
def test_typed_text_bands_give_the_scroll(page, s0):
    """F0 at scroll s0, F1 at the top: the shift is the scroll."""
    d, conf = sm.shift_between(frame_at(page, s0), frame_at(page, 0))
    assert d == s0 and conf >= sm.MARGIN


def test_the_sign_follows_the_frame_mapping(page):
    """F0(x) = F1(x - d): F0 shows page y = 1404 - x + s0, F1 the same page y at x - s0."""
    s0, s1 = 150, 520
    d, _ = sm.shift_between(frame_at(page, s0), frame_at(page, s1))
    assert d == s0 - s1 == -370
    # the same page point through compose.frame lands d columns apart
    x0 = 900.0
    _, py = fr.frame_to_page(x0, 936, "portrait", layout=fr.KEYBOARD, scroll=s0)
    _, py1 = fr.frame_to_page(x0 - d, 936, "portrait", layout=fr.KEYBOARD, scroll=s1)
    assert py == py1


@pytest.mark.parametrize("s0", [230, 250, 500])  # 250 and 500: whole periods of the dots
def test_a_periodic_dot_template_plus_text(s0):
    pg = add_text(add_dots(blank_page()), seed=5)
    d, conf = sm.shift_between(frame_at(pg, s0), frame_at(pg, 0))
    assert d == s0 and conf >= sm.MARGIN


def test_lined_paper_plus_text():
    pg = add_text(add_lines(blank_page()), seed=6)
    d, _ = sm.shift_between(frame_at(pg, 280), frame_at(pg, 0))
    assert d == 280


@pytest.mark.parametrize("template", [add_dots, add_lines])
def test_an_all_template_page_is_ambiguous(template):
    pg = template(blank_page())
    d, _ = sm.shift_between(frame_at(pg, 250), frame_at(pg, 0))
    assert d is None


def test_a_blank_page_is_ambiguous():
    pg = blank_page()
    assert sm.shift_between(frame_at(pg, 100), frame_at(pg, 0))[0] is None


def test_a_non_rigid_change_is_refused(page):
    """Something else changed on screen (a menu, a touch that drew or selected): no shift."""
    f0, f1 = frame_at(page, 300), frame_at(page, 0)
    f1[300:1500, 200:900] = 255  # a big panel covered part of the page
    f1[300:1500, 200:220] = 0
    assert sm.shift_between(f0, f1)[0] is None


def test_too_little_overlap_is_refused(page):
    """Shifted by more than a screen less MIN_OVERLAP: nothing in common to line up."""
    assert sm.shift_between(frame_at(page, 1300), frame_at(page, 0))[0] is None


# -- frames_still -----------------------------------------------------------------------

def test_frames_still(page):
    f = frame_at(page, 0)
    assert sm.frames_still(f, f.copy())
    g = f.copy()
    g[900:903, 700:703] = 0  # a few pixels (a cursor)
    assert sm.frames_still(f, g)
    assert not sm.frames_still(f, frame_at(page, 4))


# -- the swipe --------------------------------------------------------------------------

def test_the_swipe_moves_the_finger_toward_decreasing_frame_x():
    pts = sm.swipe_points()
    xs = [x for x, _ in pts]
    assert xs == sorted(xs, reverse=True) and xs[0] - xs[-1] == sm.SWIPE_LENGTH
    assert all(y == sm.SWIPE_ROW for _, y in pts)
    assert sm.PAGE_ROWS[0] < sm.SWIPE_ROW < sm.PAGE_ROWS[1]  # on the page, not its edge
    assert 0 < xs[-1] and xs[0] < fr.FRAME_W - 200  # clear of the top of the screen


def test_the_swipe_scrolls_toward_the_page_top():
    """The page follows the finger: the page point under it at the start is still under it at
    the end, which needs a smaller scroll (closer to the top)."""
    pts = sm.swipe_points()
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    s = 900.0
    s_after = sm.scroll_after_swipe(s, pts)
    assert s_after == s - sm.SWIPE_LENGTH
    under_start = fr.frame_to_page(x0, y0, "portrait", layout=fr.KEYBOARD, scroll=s)
    under_end = fr.frame_to_page(x1, y1, "portrait", layout=fr.KEYBOARD, scroll=s_after)
    assert under_start == under_end
    assert sm.scroll_after_swipe(100.0, pts) == 0.0  # never above the top


# -- the stop rule: TopFinder -----------------------------------------------------------

def _run(finder, page, scrolls):
    """Feed the frames at these scrolls, one per swipe; the last result."""
    r = None
    for s in scrolls:
        r = finder.after_swipe(frame_at(page, s))
    return r


def test_top_finder_sums_the_moves_until_the_page_stops(page):
    f0 = frame_at(page, 1500)  # more than a screen down: only the chain can measure it
    t = sm.TopFinder(f0)
    assert t.begin(frame_at(page, 1500))
    assert _run(t, page, [900, 300]) == sm.MOVING
    assert t.after_swipe(frame_at(page, 0)) == sm.MOVING
    assert t.after_swipe(frame_at(page, 0)) == sm.STILL  # one still frame is not enough
    assert t.after_swipe(frame_at(page, 0)) == sm.TOP
    assert t.scroll == 1500 and t.swipes == 5


def test_top_finder_already_at_the_top(page):
    f0 = frame_at(page, 0)
    t = sm.TopFinder(f0)
    assert t.begin(f0.copy())
    t.after_check(frame_at(page, sm.CHECK_LENGTH))  # the check swipe moved it down the page
    assert _run(t, page, [0] * (1 + sm.STILL_NEEDED)) == sm.TOP
    assert t.scroll == 0


def test_top_finder_refuses_when_no_swipe_ever_moved_the_page(page):
    """Seen on the device: swipes that didn't take effect read as 'already at the top'
    (scroll 0) and the items landed far off. Nothing moving at all is now refused."""
    f0 = frame_at(page, 900)
    t = sm.TopFinder(f0)
    assert t.begin(f0.copy())
    t.after_check(f0.copy())  # the check swipe didn't move it
    with pytest.raises(sm.MeasureError) as e:
        _run(t, page, [900] * sm.STILL_NEEDED)
    assert e.value.code == sm.NO_EFFECT


def test_top_finder_starts_from_a_different_scroll(page):
    """After xochitl restarted the page may show at another scroll than F0."""
    t = sm.TopFinder(frame_at(page, 600))
    assert t.begin(frame_at(page, 200))
    assert _run(t, page, [0, 0, 0]) == sm.TOP
    assert t.scroll == 600


def test_top_finder_waits_for_the_page_to_show(page):
    t = sm.TopFinder(frame_at(page, 300))
    assert not t.begin(np.full((fr.FRAME_H, fr.FRAME_W), 255, np.uint8))  # still starting
    assert t.begin(frame_at(page, 300))


def test_top_finder_refuses_an_ambiguous_first_picture():
    pg = add_dots(blank_page())
    with pytest.raises(sm.MeasureError) as e:
        sm.TopFinder(frame_at(pg, 300))
    assert e.value.code == sm.UNCLEAR


def test_top_finder_refuses_a_non_rigid_change(page):
    t = sm.TopFinder(frame_at(page, 800))
    assert t.begin(frame_at(page, 800))
    f = frame_at(page, 300)
    f[300:1500, 200:900] = 255
    with pytest.raises(sm.MeasureError) as e:
        t.after_swipe(f)
    assert e.value.code == sm.MOVED


def test_top_finder_refuses_moving_the_wrong_way(page):
    t = sm.TopFinder(frame_at(page, 300))
    assert t.begin(frame_at(page, 300))
    with pytest.raises(sm.MeasureError) as e:
        t.after_swipe(frame_at(page, 600))
    assert e.value.code == sm.MOVED


def test_top_finder_bound(page):
    t = sm.TopFinder(frame_at(page, 2000), max_swipes=3)
    assert t.begin(frame_at(page, 2000))
    with pytest.raises(sm.MeasureError) as e:
        for s in (1600, 1200, 800, 400):
            t.after_swipe(frame_at(page, s))
    assert e.value.code == sm.BOUND and t.swipes == 3


def test_top_finder_cross_checks_the_direct_shift(page, monkeypatch):
    """When F0 and the top overlap, the direct shift must agree with the chain."""
    t = sm.TopFinder(frame_at(page, 400))
    assert t.begin(frame_at(page, 400))
    t.after_swipe(frame_at(page, 0))
    t.after_swipe(frame_at(page, 0))
    real = sm.shift_between
    monkeypatch.setattr(sm, "shift_between",
                        lambda a, b: (real(a, b)[0] + 30, 1.0) if a is t.f0 else real(a, b))
    with pytest.raises(sm.MeasureError) as e:
        t.after_swipe(frame_at(page, 0))
    assert e.value.code == sm.MOVED


def test_stream_noise_rows_are_left_out_of_the_line_up():
    """Rows of random noise (goMarkableStream after a restart) change between frames; the
    shift is still found from the page rows."""
    import numpy as np
    from rmdesk.compose import scroll_measure as sm
    rng = np.random.default_rng(3)
    page = np.full((1872, 1404 + 400), 255, np.uint8)
    for _ in range(400):  # handwriting-like marks
        r, c = rng.integers(0, 1872), rng.integers(0, 1404 + 380)
        page[r:r + 3, c:c + 20] = 0
    f0 = page[:, 200:200 + 1404].copy()
    f1 = page[:, 200 - 137:200 - 137 + 1404].copy()  # scrolled 137 px along frame x
    for f in (f0, f1):  # noise rows inside the compared band, different in each frame
        f[400:520] = rng.integers(0, 2, (120, 1404)).astype(np.uint8) * 255
    assert sm.noise_rows(f0).sum() > 0
    d, conf = sm.shift_between(f0, f1)
    assert d is not None and abs(abs(d) - 137) <= 1


def test_one_swipe_to_the_top_with_little_overlap_is_still_measured():
    """Seen on the device: a single swipe reached the page's top, a 1123 px jump leaving
    only ~280 px shared. A near-perfect match on that small overlap is accepted."""
    import numpy as np
    from rmdesk.compose import scroll_measure as sm
    rng = np.random.default_rng(7)
    W = 1404
    page = np.full((1872, W + 1123), 255, np.uint8)
    page[:, ::104] = 150
    page[::104, :] = 150
    for _ in range(500):  # writing on the part both pictures share and beyond
        r, c = rng.integers(300, 1600), rng.integers(0, W + 1100)
        page[r:r + 4, c:c + 30] = 0
    # frame x runs up the page (page y = 1404 - fx + scroll): the top is the high-x end
    f0 = page[:, 0:W].copy()  # scrolled 1123 down the page
    f1 = page[:, 1123:1123 + W].copy()  # at its top
    r = sm.shift_details(f0, f1)
    assert r["d"] is not None and abs(abs(r["d"]) - 1123) <= 1
    t = sm.TopFinder(f0)
    assert t.begin(f0)
    assert t.after_swipe(f1) == "moving"
    t.after_swipe(f1)
    assert t.after_swipe(f1) == sm.TOP and abs(t.scroll - 1123) <= 1
