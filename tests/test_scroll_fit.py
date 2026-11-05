"""Scroll of a page in the keyboard layout, found from the live frame (synthetic pages)."""
import numpy as np
import pytest

from rmdesk.compose.scroll_fit import fit_keyboard_scroll

H, W = 1872, 1404


def strokes(seed=1, n=25, page_h=2600):
    """Random handwriting-like polylines in page coordinates (x -702..702, y 0..page_h)."""
    rng = np.random.default_rng(seed)
    lines = []
    for _ in range(n):
        x, y = rng.uniform(-650, 650), rng.uniform(20, page_h - 20)
        pts = [(x, y)]
        for _ in range(rng.integers(8, 30)):
            x = float(np.clip(x + rng.normal(0, 6), -700, 700))
            y = float(np.clip(y + rng.normal(0, 6), 0, page_h))
            pts.append((x, y))
        lines.append(pts)
    return lines


def render(lines, scroll, grid=False):
    """The frame the tablet shows in the keyboard layout at `scroll` (2 px wide ink)."""
    img = np.full((H, W), 255, np.uint8)
    if grid:  # template lines every 90 px across the screen: periodic distractors
        img[::90, :] = 100
        img[:, ::90] = 100
    for pl in lines:
        for (x0, y0), (x1, y1) in zip(pl, pl[1:]):
            for t in np.linspace(0, 1, 12):
                px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                fy, fx = int(round(px + H / 2)), int(round(W - py + scroll))
                if 1 <= fy < H and 1 <= fx < W:  # only what is on screen
                    img[fy - 1:fy + 1, fx - 1:fx + 1] = 0
    return img


def points(lines):
    return np.array([p for pl in lines for p in pl])


@pytest.mark.parametrize("scroll", [0, 137, 678, 1196])
def test_finds_the_scroll(scroll):
    lines = strokes()
    got = fit_keyboard_scroll(render(lines, scroll), points(lines))
    assert got is not None and abs(got - scroll) <= 2


def test_finds_the_scroll_over_a_grid_template():
    lines = strokes(seed=3)
    got = fit_keyboard_scroll(render(lines, 500, grid=True), points(lines))
    assert got is not None and abs(got - 500) <= 2


def test_blank_or_other_picture_gives_none():
    lines = strokes()
    assert fit_keyboard_scroll(np.full((H, W), 255, np.uint8), points(lines)) is None
    other = strokes(seed=9)
    assert fit_keyboard_scroll(render(other, 300), points(lines)) is None


def test_too_little_on_screen_gives_none():
    few = [[(0.0, 100.0), (5.0, 101.0)]]
    assert fit_keyboard_scroll(render(few, 0), points(few)) is None
    assert fit_keyboard_scroll(render([], 0), np.zeros((0, 2))) is None


def test_periodic_page_is_ambiguous():
    """Identical marks every 300 px with nothing else: refuse rather than guess."""
    row = [[(x - 10.0, 0.0), (x + 10.0, 0.0)] for x in (-600.0, 0.0, 600.0)]
    lines = [[(x, y + k * 300.0) for x, y in pl] for k in range(12) for pl in row]
    assert fit_keyboard_scroll(render(lines, 300), points(lines)) is None


def test_page_points_reads_every_layer():
    from pathlib import Path
    from rmdesk.compose.scroll_fit import page_points
    data = Path("tests/fixtures/rm/user-3.29.1.0-rmdesk-test.rm").read_bytes()
    pts = page_points(data)
    assert pts.ndim == 2 and pts.shape[1] == 2 and len(pts) > 0
    assert page_points(b"not a page").shape == (0, 2)
