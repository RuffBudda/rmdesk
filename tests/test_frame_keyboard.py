"""Keyboard layout (Type Folio attached) frame -> page mapping, from the 2026-11-04
calibration: a grid of crosses at known page points, screenshots taken with the Screen
view rotated 270 (screen sx = frame y, sy = 1404 - frame x)."""
import pytest

from rmdesk.compose import build
from rmdesk.compose.frame import KEYBOARD, PORTRAIT_UI, frame_to_page
from rmdesk.compose.items import Box, InkTextItem, PenStyle


def from_screen(sx, sy):
    return 1404 - sy, sx  # frame (x, y) for a point on the rotated-270 screenshot


# (page point, where it showed on the screenshot, scroll)
SEEN = [((-600, 100), (336, 100), 0), ((0, 100), (935, 100), 0), ((600, 100), (1535, 100), 0),
        ((-600, 1300), (336, 1300), 0), ((600, 1000), (1535, 1000), 0),
        ((-600, 700), (336, 22), 678), ((0, 1300), (935, 622), 678), ((600, 1800), (1535, 1122), 678)]


@pytest.mark.parametrize("page,screen,scroll", SEEN)
def test_keyboard_layout_matches_the_calibration(page, screen, scroll):
    x, y = frame_to_page(*from_screen(*screen), "portrait", layout=KEYBOARD, scroll=scroll)
    assert abs(x - page[0]) <= 2 and abs(y - page[1]) <= 2


def test_portrait_layout_unchanged():
    assert frame_to_page(702, 936, "portrait") == (0, 936)
    assert frame_to_page(702, 936, "portrait", layout=PORTRAIT_UI, scroll=500) == (0, 936)


def test_unknown_layout_and_landscape():
    with pytest.raises(ValueError):
        frame_to_page(0, 0, "portrait", layout="sideways")
    with pytest.raises(NotImplementedError):
        frame_to_page(0, 0, "landscape", layout=KEYBOARD)


def test_keyboard_lines_are_clipped_to_the_page_width():
    """The keyboard layout's frame is wider than the page: margins either side are cut."""
    item = InkTextItem(text="x", box=Box(0, 0, 1404, 1872), size=10, pen=PenStyle("fineliner"))
    # a stroke along the frame's y axis = across the page's whole frame width (x -936..936)
    lines = build.frame_to_lines(item, [[(700, 0), (700, 1872)]], None, "portrait",
                                 layout=KEYBOARD, scroll=100)
    xs = [p.x for l in lines for p in l.points]
    ys = [p.y for l in lines for p in l.points]
    assert min(xs) >= -702 - 1e-6 and max(xs) <= 702 + 1e-6
    assert all(abs(y - (1404 - 700 + 100)) < 1e-6 for y in ys)


# -- text-anchored placement: points local to an anchored group ------------------------

def test_anchored_frame_point_to_local():
    from rmdesk.compose.frame import Anchored, frame_to_anchored
    a = Anchored(const=517.0, origin_x=-464.0)
    # local x = page x - origin_x = (fy - 936) + 464; local y = C - fx
    assert frame_to_anchored(400, 1000, a) == (1000 - 936 + 464, 517 - 400)
    assert frame_to_anchored(600, 236, a) == (236 - 936 + 464, -83)  # local y can be < 0


def test_anchored_lines_are_local_and_clipped_on_absolute_x():
    from rmdesk.compose.frame import Anchored
    a = Anchored(const=517.0, origin_x=-464.0)
    item = InkTextItem(text="x", box=Box(0, 0, 1404, 1872), size=10, pen=PenStyle("fineliner"))
    lines = build.frame_to_lines(item, [[(700, 0), (700, 1872)]], None, "portrait",
                                 layout=KEYBOARD, anchored=a)
    xs = [p.x for l in lines for p in l.points]
    ys = [p.y for l in lines for p in l.points]
    # clipped to the page width on the absolute x (-702..702), then made local
    assert abs(min(xs) - (-702 + 464)) < 1e-6 and abs(max(xs) - (702 + 464)) < 1e-6
    assert all(abs(y - (517 - 700)) < 1e-6 for y in ys)  # negative local y is kept


def test_anchored_lines_match_the_absolute_mapping_shifted():
    """For one frame polyline, anchored = absolute at scroll C - 1404, with x - origin_x."""
    from rmdesk.compose.frame import Anchored
    item = InkTextItem(text="x", box=Box(100, 200, 300, 300), size=10, pen=PenStyle("ballpoint"))
    pl = [[(150, 300), (250, 420), (380, 460)]]
    a = Anchored(const=900.0, origin_x=-300.0)
    got = build.frame_to_lines(item, pl, None, "portrait", layout=KEYBOARD, anchored=a)
    ref = build.frame_to_lines(item, pl, None, "portrait", layout=KEYBOARD, scroll=900 - 1404)
    assert [[(p.x, p.y) for p in l.points] for l in got] == \
        [[(p.x + 300, p.y) for p in l.points] for l in ref]


def test_anchored_needs_the_keyboard_layout():
    from rmdesk.compose.frame import Anchored
    item = InkTextItem(text="x", box=Box(0, 0, 100, 100), size=10, pen=PenStyle("fineliner"))
    with pytest.raises(ValueError):
        build.frame_to_lines(item, [[(1, 1), (5, 5)]], None, "portrait",
                             layout=PORTRAIT_UI, anchored=Anchored(500.0, -464.0))


def test_anchored_shaded_lines_are_local():
    from rmdesk.compose.frame import Anchored
    from rmdesk.compose.items import ImageItem
    from rmdesk.compose.shade import ShadeResult
    from pathlib import Path
    item = ImageItem(path=Path("x.png"), box=Box(100, 300, 100, 100))
    result = ShadeResult(strokes=[[(10.0, 10.0, 8.0, 200.0), (90.0, 10.0, 8.0, 200.0)]],
                         size=(100, 100), capped=False, spacing_used=4)
    a = Anchored(const=700.0, origin_x=-464.0)
    got = build.frame_shade_to_lines(item, result, PenStyle("pencil"), "portrait",
                                     layout=KEYBOARD, anchored=a)
    pts = [(p.x, p.y) for l in got for p in l.points]
    assert pts == [(310 - 936 + 464, 700 - 110), (310 - 936 + 464, 700 - 190)]
