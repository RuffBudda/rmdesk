"""Shaded pictures: shading engine (compose.shade) and line building (build.shade_to_lines)."""
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from rmscene import scene_items as si

from rmdesk.compose import build, shade
from rmdesk.compose.items import Box, ImageItem, PenStyle
from rmdesk.notebook import page

RM = Path(__file__).parent / "fixtures" / "rm"
FIXTURES = sorted(p.name for p in RM.glob("*.rm"))
PENCIL = PenStyle(shade.DEFAULT_SHADE_PEN, "black")


def _write(tmp_path, img, name="img.png"):
    p = tmp_path / name
    assert cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def gradient(tmp_path):
    # black on the left -> white on the right
    row = np.linspace(0, 255, 400).astype(np.uint8)
    return _write(tmp_path, np.tile(row, (200, 1)))


def _points(res):
    return [p for st in res.strokes for p in st]


def test_max_shade_width_pinned():
    # 0.9 x spacing page px, 4 tablet width units per page px (rmscene: width = px * 4)
    assert shade.WIDTH_UNITS_PER_PX == 4
    assert shade.MAX_SHADE_WIDTH(5) == pytest.approx(18.0)
    assert shade.MAX_SHADE_WIDTH(10) == pytest.approx(36.0)


def test_default_shade_pen_is_pencil():
    assert shade.DEFAULT_SHADE_PEN == "pencil"
    item = ImageItem(Path("x.png"), Box(0, 0, 10, 10), pen=PENCIL)
    res = shade.ShadeResult([[(1, 1, 10, 100), (5, 1, 10, 100)]], (10, 10), False, 5)
    assert build.shade_to_lines(item, res, PENCIL)[0].tool == si.Pen.PENCIL_2


def test_gradient_width_monotonic(gradient):
    res = shade.shade_image(gradient, 400, 200)
    pts = _points(res)
    assert pts
    buckets = {}
    for x, _y, w, _p in pts:
        buckets.setdefault(int(x // 40), []).append(w)
    means = [np.mean(buckets[k]) for k in sorted(buckets)]
    assert len(means) >= 6
    assert all(a > b for a, b in zip(means, means[1:]))


def test_blank_picture_gives_no_strokes(tmp_path):
    white = _write(tmp_path, np.full((100, 150), 255, np.uint8), "white.png")
    clear = np.zeros((100, 150, 4), np.uint8)  # fully transparent black
    transparent = _write(tmp_path, clear, "clear.png")
    for p in (white, transparent):
        res = shade.shade_image(p, 300, 200)
        assert res.strokes == []
        assert res.capped is False


def test_black_gives_full_width(tmp_path):
    p = _write(tmp_path, np.zeros((100, 100), np.uint8))
    res = shade.shade_image(p, 100, 100, spacing=5)
    pts = _points(res)
    assert pts
    for _x, _y, w, pr in pts:
        assert w == pytest.approx(shade.MAX_SHADE_WIDTH(5))
        assert pr == pytest.approx(255)
    # horizontal lines, one per 5 px
    assert len(res.strokes) == 20
    assert all(len({round(y, 3) for _x, y, _w, _p in st}) == 1 for st in res.strokes)


def _diagonal(st):
    return len({round(y, 3) for _x, y, _w, _p in st}) > 1


def test_crosshatch_only_dark(tmp_path):
    img = np.full((200, 400), 153, np.uint8)  # d = 0.4 on the left
    img[:, 200:] = 0  # d = 1.0 on the right
    p = _write(tmp_path, img)
    plain = shade.shade_image(p, 400, 200, crosshatch=False)
    hatched = shade.shade_image(p, 400, 200, crosshatch=True)
    assert not any(_diagonal(st) for st in plain.strokes)
    diag = [st for st in hatched.strokes if _diagonal(st)]
    assert diag
    for st in diag:
        for x, _y, _w, _pr in st:
            assert x >= 200 - 5
    grey = _write(tmp_path, np.full((200, 400), 153, np.uint8), "grey.png")
    assert not any(_diagonal(st) for st in shade.shade_image(grey, 400, 200, crosshatch=True).strokes)


def test_crosshatch_width_scaled(tmp_path):
    p = _write(tmp_path, np.zeros((200, 200), np.uint8))
    res = shade.shade_image(p, 200, 200, crosshatch=True)
    diag = [pt for st in res.strokes if _diagonal(st) for pt in st]
    assert diag
    # d = 1 -> (1 - 0.6) / 0.4 = 1 -> full width
    assert all(w == pytest.approx(shade.MAX_SHADE_WIDTH(5)) for _x, _y, w, _p in diag)


def test_cap_widens_spacing(tmp_path):
    # full-page pure noise: nearly every sample differs in width, so adaptive
    # sampling cannot thin it under the budget at spacing 5 (S3: a 1024x768 noise
    # picture now fits at 5, so the fixture is a full page)
    rng = np.random.default_rng(3)
    p = _write(tmp_path, rng.integers(0, 256, (1872, 1404), dtype=np.uint8), "noise.png")
    res = shade.shade_image(p, 1404, 1872)
    assert sum(len(st) for st in res.strokes) <= shade.SHADE_MAX_POINTS
    assert res.spacing_used > 5


def test_shade_max_points():
    assert shade.SHADE_MAX_POINTS == 60000


def test_mid_tone_photo_fits_at_default_spacing(tmp_path):
    x = np.linspace(30, 230, 1024, dtype=np.float32)
    rng = np.random.default_rng(7)
    img = np.tile(x, (768, 1)) + rng.normal(0, 10, (768, 1024)).astype(np.float32)
    p = _write(tmp_path, np.clip(img, 0, 255).astype(np.uint8), "midtone.png")
    res = shade.shade_image(p, 1404, 936)
    assert res.spacing_used == 5 and res.capped is False
    assert 0 < sum(len(st) for st in res.strokes) <= shade.SHADE_MAX_POINTS


def test_adaptive_sampling_flat_tone(tmp_path):
    # flat black: widths never change, so only every 12 px plus each run's ends
    p = _write(tmp_path, np.zeros((100, 101), np.uint8))
    res = shade.shade_image(p, 101, 100)
    for st in res.strokes:
        xs = [x for x, _y, _w, _p in st]
        assert xs[0] == pytest.approx(0.5) and xs[-1] == pytest.approx(100.5)
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        assert all(0 < g <= 12 for g in gaps)
        assert len(st) < 51 // 3  # far fewer than the 51 samples per row


def test_adaptive_sampling_keeps_width_steps(tmp_path):
    # left half black, right half mid grey: the width step is kept exactly
    img = np.zeros((50, 200), np.uint8)
    img[:, 100:] = 128
    p = _write(tmp_path, img)
    res = shade.shade_image(p, 200, 50)
    for st in res.strokes:
        ws = [w for _x, _y, w, _p in st]
        assert max(ws) == pytest.approx(shade.MAX_SHADE_WIDTH(5))
        assert min(ws) < 0.6 * shade.MAX_SHADE_WIDTH(5)
        # consecutive points differ by >= 1 unit or are <= 12 px apart
        for a, b in zip(st, st[1:]):
            assert abs(b[2] - a[2]) >= 1 or b[0] - a[0] <= 12 + 1e-9


def test_pencil_pen_width_and_to_lines():
    assert build.PEN_WIDTH["pencil"] == build.PEN_WIDTH["ballpoint"] == 18
    item = ImageItem(Path("x.png"), Box(0, 0, 100, 100), pen=PENCIL)
    (ln,) = build.to_lines(item, [[(0, 0), (1, 1)]], (1, 1))
    assert ln.tool == si.Pen.PENCIL_2 and ln.points[0].width == 18


def test_large_image_downscaled_fast(tmp_path):
    x = np.linspace(0, 255, 6000, dtype=np.float32)
    y = np.linspace(0, 255, 4000, dtype=np.float32)
    img = ((x[None, :] + y[:, None]) / 2).astype(np.uint8)
    p = _write(tmp_path, img, "huge.png")
    t = time.perf_counter()
    res = shade.shade_image(p, 1200, 800)
    assert time.perf_counter() - t < 1.5
    assert res.strokes
    assert all(0 <= x <= 1200 and 0 <= y <= 800 for x, y, _w, _p in _points(res))


def test_tiny_box(tmp_path, gradient):
    for bw, bh in ((20, 20), (3, 3), (1, 1), (0, 0), (500, 2)):
        res = shade.shade_image(gradient, bw, bh)
        for st in res.strokes:
            assert len(st) >= 2
        item = ImageItem(gradient, Box(100, 100, max(bw, 1), max(bh, 1)), pen=PENCIL)
        for ln in build.shade_to_lines(item, res, PENCIL):
            page._check_line(ln)


def test_strokes_inside_box(tmp_path):
    rng = np.random.default_rng(5)
    img = cv2.GaussianBlur(rng.integers(0, 256, (300, 500), dtype=np.uint8), (0, 0), 3)
    p = _write(tmp_path, img)
    for bw, bh in ((400, 400), (700, 200), (250, 150)):
        res = shade.shade_image(p, bw, bh, crosshatch=True)
        assert res.size == (bw, bh)
        pts = _points(res)
        assert pts
        for x, y, w, pr in pts:
            assert 0 <= x <= bw and 0 <= y <= bh
            assert 1 <= w <= 0xFFFF and 0 <= pr <= 255
        for st in res.strokes:
            assert len(st) >= 2


def test_fit_and_centre(tmp_path):
    # a 2:1 black picture in a square box is centred vertically
    p = _write(tmp_path, np.zeros((100, 200), np.uint8))
    res = shade.shade_image(p, 200, 200)
    ys = [y for _x, y, _w, _p in _points(res)]
    assert min(ys) >= 50 and max(ys) <= 150


def test_clip_keeps_plain_polylines():
    assert build._clip([(10, 10), (2000, 10)]) == [[(10, 10), (1404.0, 10.0)]]


def test_clip_keeps_width_pressure():
    # one stroke from box x 0 to 100 in an item hanging 50 px off the right page edge
    item = ImageItem(Path("x.png"), Box(1354, 10, 100, 100), pen=PENCIL)
    res = shade.ShadeResult([[(0, 10, 10, 100), (100, 10, 30, 200)]], (100, 100), False, 5)
    (ln,) = build.shade_to_lines(item, res, PENCIL)
    a, b = ln.points
    assert (a.x, a.y) == (1354 + page.X_MIN, 20)
    assert (a.width, a.pressure) == (10, 100)
    assert b.x == pytest.approx(-page.X_MIN)
    assert (b.width, b.pressure) == (20, 150)  # interpolated at the crossing
    assert (a.speed, a.direction) == (0, 0)


def test_rotated_shaded_item_inside_page_after_clipping(tmp_path, gradient):
    res = shade.shade_image(gradient, 600, 300)
    # near the bottom-right corner: rotated 90 degrees it hangs off the page
    item = ImageItem(gradient, Box(1000, 1700, 600, 300), rotation=90.0, pen=PENCIL)
    lines = build.shade_to_lines(item, res, PENCIL)
    pts = [p for ln in lines for p in ln.points]
    assert pts
    for p in pts:
        assert page.X_MIN <= p.x <= -page.X_MIN and 0 <= p.y <= page.PAGE_H
        assert 1 <= p.width and 0 <= p.pressure <= 255
    # rotated: the horizontal hatching lines are now vertical
    assert all(len({round(p.x, 3) for p in ln.points}) == 1 for ln in lines)


def test_rotation_carries_width_pressure():
    item = ImageItem(Path("x.png"), Box(100, 100, 100, 100), rotation=90.0, pen=PENCIL)
    res = shade.ShadeResult([[(10, 50, 7, 70), (90, 50, 9, 90)]], (100, 100), False, 5)
    (ln,) = build.shade_to_lines(item, res, PENCIL)
    assert [(p.width, p.pressure) for p in ln.points] == [(7, 70), (9, 90)]
    # (110, 150) turns 90 degrees clockwise around (150, 150) to (150, 110)
    assert (ln.points[0].x - page.X_MIN, ln.points[0].y) == pytest.approx((150, 110))


@pytest.mark.parametrize("name", FIXTURES)
def test_lines_pass_check_line_and_verify(name, gradient):
    res = shade.shade_image(gradient, 400, 200, crosshatch=True)
    item = ImageItem(gradient, Box(100, 100, 400, 200), pen=PENCIL)
    lines = build.shade_to_lines(item, res, PENCIL)
    assert lines
    for ln in lines:
        page._check_line(ln)
    orig = (RM / name).read_bytes()
    pg = page.load(orig)
    pg.append_to_rmdesk_layer(lines)
    page.verify(orig, pg.to_bytes(), len(lines), None)


def test_shade_has_no_qt():
    import ast
    tree = ast.parse(Path(shade.__file__).read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith(("PySide", "PyQt")) for m in mods)


# -- S4: shade only what can land on the page ---------------------------------

def _line_pts(lines):
    return [p for ln in lines for p in ln.points]


def test_huge_box_fast_and_visible(gradient):
    # the 2:1 picture is centred in the square box (y 5000..15000), so the box is
    # moved up 5000 px to put the picture's top-left corner on the page
    t = time.perf_counter()
    res = shade.shade_image(gradient, 20000, 20000, box_x=0, box_y=-5000, rotation=0.0)
    assert time.perf_counter() - t < 1.5
    item = ImageItem(gradient, Box(0, -5000, 20000, 20000), pen=PENCIL)
    lines = build.shade_to_lines(item, res, PENCIL)
    assert lines  # the part on the page is drawn
    assert len(_line_pts(lines)) == sum(len(st) for st in res.strokes)  # nothing off page
    centred = shade.shade_image(gradient, 20000, 20000)  # picture below the page
    assert centred.strokes == [] and centred.capped is False


def test_huge_rotated_box_fast(gradient):
    t = time.perf_counter()
    item = ImageItem(gradient, Box(-9000, -9000, 20000, 20000), rotation=30.0, pen=PENCIL)
    res = shade.shade_item(item)
    assert time.perf_counter() - t < 1.5
    lines = build.shade_to_lines(item, res, PENCIL)
    pts = _line_pts(lines)
    assert pts
    assert len(pts) == sum(len(st) for st in res.strokes)
    for p in pts:
        assert page.X_MIN <= p.x <= -page.X_MIN and 0 <= p.y <= page.PAGE_H


def test_off_page_picture_gives_no_strokes(gradient):
    item = ImageItem(gradient, Box(3000, 100, 400, 200), pen=PENCIL)
    res = shade.shade_item(item)
    assert res.strokes == [] and res.capped is False


def test_capped_never_with_zero_visible_points(tmp_path):
    rng = np.random.default_rng(11)
    p = _write(tmp_path, rng.integers(0, 256, (1872, 1404), dtype=np.uint8), "noise.png")
    for box in (Box(0, 0, 1404, 1872), Box(1400, 0, 1404, 1872), Box(-5000, -5000, 1404, 1872)):
        res = shade.shade_item(ImageItem(p, box, pen=PENCIL))
        if res.capped:
            assert res.strokes


def test_mostly_off_page_keeps_visible_strokes(tmp_path):
    # 2 pages wide: shading it all would blow the budget; only the right half is
    # on the page, and that half shades exactly like the cropped picture on the page
    x = np.linspace(40, 220, 2808, dtype=np.float32)
    rng = np.random.default_rng(9)
    img = np.clip(np.tile(x, (1872, 1)) + rng.normal(0, 6, (1872, 2808)), 0, 255).astype(np.uint8)
    full = _write(tmp_path, img, "wide.png")
    crop = _write(tmp_path, np.ascontiguousarray(img[:, 1404:]), "crop.png")
    off = ImageItem(full, Box(-1404, 0, 2808, 1872), pen=PENCIL)
    on = ImageItem(crop, Box(0, 0, 1404, 1872), pen=PENCIL)
    r_off, r_on = shade.shade_item(off), shade.shade_item(on)
    assert r_on.spacing_used == 5 and not r_on.capped
    assert r_off.spacing_used == 5 and not r_off.capped
    a = _line_pts(build.shade_to_lines(off, r_off, PENCIL))
    b = _line_pts(build.shade_to_lines(on, r_on, PENCIL))
    assert len(a) == sum(len(st) for st in r_off.strokes)
    assert abs(len(a) - len(b)) <= 0.02 * len(b)
    # same lines (rows) on the page, same tone away from the crop edge
    assert {round(p.y, 3) for p in a} == {round(p.y, 3) for p in b}
    wa = np.mean([p.width for p in a if p.x > X_EDGE])
    wb = np.mean([p.width for p in b if p.x > X_EDGE])
    assert wa == pytest.approx(wb, rel=0.01)


X_EDGE = page.X_MIN + 10


def test_pen_thickness_scales_width():
    res = shade.ShadeResult([[(1, 1, 10, 100), (5, 1, 10, 100)]], (10, 10), False, 5)
    item = ImageItem(Path("x.png"), Box(0, 0, 10, 10), pen=PENCIL)
    thick = PenStyle(shade.DEFAULT_SHADE_PEN, "black", 2.0)
    thin = PenStyle(shade.DEFAULT_SHADE_PEN, "black", 0.001)
    assert build.shade_to_lines(item, res, thick)[0].points[0].width == 20
    assert build.shade_to_lines(item, res, thin)[0].points[0].width == 1


def _logo(tmp_path, alpha=False):
    """A white square on black (the user's logo case); with alpha, transparent outside."""
    img = np.zeros((200, 200, 4 if alpha else 3), np.uint8)
    img[60:140, 60:140, :3] = 255
    if alpha:
        img[:, :, 3] = 0
        img[40:160, 40:160, 3] = 255  # opaque black frame around the white square
    return _write(tmp_path, img, "logo.png")


def _inked_area(res):
    pts = np.array(_points(res))
    return pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()


def test_invert_draws_the_light_logo_not_the_dark_background(tmp_path):
    path = _logo(tmp_path)
    plain = shade.shade_image(path, 200, 200)
    inv = shade.shade_image(path, 200, 200, invert=True)
    # plain: ink everywhere but the square; inverted: ink only inside the square
    x0, x1, y0, y1 = _inked_area(plain)
    assert x0 < 20 and x1 > 180
    x0, x1, y0, y1 = _inked_area(inv)
    assert 55 <= x0 and x1 <= 145 and 55 <= y0 and y1 <= 145


def test_invert_keeps_transparent_parts_empty(tmp_path):
    path = _logo(tmp_path, alpha=True)
    inv = shade.shade_image(path, 200, 200, invert=True)
    x0, x1, y0, y1 = _inked_area(inv)
    assert 55 <= x0 and x1 <= 145  # only the (now dark) square; the clear border stays empty


def test_invert_default_off_is_unchanged(gradient):
    a = shade.shade_image(gradient, 300, 100)
    b = shade.shade_image(gradient, 300, 100, invert=False)
    assert a.strokes == b.strokes
