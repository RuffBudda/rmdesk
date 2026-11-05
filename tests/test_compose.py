from pathlib import Path

import cv2
import numpy as np
import pytest
from rmscene import scene_items as si

from rmdesk.compose import build, inktext, shade
from rmdesk.compose.items import Box, ImageItem, InkTextItem, PenStyle
from rmdesk.notebook import page
from rmdesk.notebook.page import X_MIN

RM = Path(__file__).parent / "fixtures" / "rm"


@pytest.fixture
def diagram(tmp_path):
    img = np.full((600, 800), 255, np.uint8)
    cv2.rectangle(img, (100, 100), (500, 400), 0, 4)
    cv2.line(img, (100, 100), (700, 500), 0, 4)
    cv2.circle(img, (600, 200), 80, 0, 4)
    p = tmp_path / "diagram.png"
    cv2.imwrite(str(p), img)
    return p


def _pts(lines):
    return [p for ln in lines for p in ln.points]


def _bbox(lines):
    pts = _pts(lines)
    xs = [p.x - X_MIN for p in pts]
    ys = [p.y for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _frame(w, h):
    """The outline of a w x h picture as unit-square polylines (longest side = 1)."""
    k = 1.0 / max(w, h)
    return [[(0, 0), (w * k, 0), (w * k, h * k), (0, h * k), (0, 0)]]


def test_wide_image_fit_and_centred():
    lines = build.to_lines(ImageItem(Path("x.png"), Box(100, 100, 900, 900)), _frame(900, 300),
                           (900, 300))
    x0, y0, x1, y1 = _bbox(lines)
    assert x0 == pytest.approx(100, abs=1e-6) and x1 == pytest.approx(1000, abs=1e-6)
    assert (y0 + y1) / 2 == pytest.approx(550, abs=1e-6)
    assert y1 - y0 == pytest.approx(300, abs=1e-6)


def test_4x3_image_fills_4x3_box():
    lines = build.to_lines(ImageItem(Path("x.png"), Box(100, 100, 400, 300)), _frame(800, 600),
                           (800, 600))
    x0, y0, x1, y1 = _bbox(lines)
    assert (x1 - x0) == pytest.approx(400, abs=1e-6)
    assert (y1 - y0) == pytest.approx(300, abs=1e-6)


def test_lines_clipped_to_page():
    item = InkTextItem("x", Box(1200, 1700, 900, 900))
    lines = build.to_lines(item, [[(0, 0), (500, 500)], [(500, 0), (600, 0)]])
    pts = _pts(lines)
    assert pts
    for p in pts:
        assert -702 <= p.x <= 702 and 0 <= p.y <= 1872
    # the stroke crossing the edge is kept, cut at the page border
    assert len(lines) == 1 and lines[0].points[-1].y == pytest.approx(1872)


def test_stroke_leaving_and_reentering_is_split():
    item = InkTextItem("x", Box(0, 0, 100, 100))
    lines = build.to_lines(item, [[(10, 10), (10, -50), (20, -50), (20, 10)]])
    assert len(lines) == 2


def test_ink_text_size():
    res = inktext.text_to_polylines("H", 40)
    ys = [y for ln in res.lines for _, y in ln]
    assert 30 <= max(ys) - min(ys) <= 45


def test_ink_text_unknown_glyphs():
    res = inktext.text_to_polylines("Hi 🙂 你 🙂", 40)
    assert res.skipped == ["🙂", "你"]
    assert res.lines


def test_ink_text_line_height():
    res = inktext.text_to_polylines("H\nH", 40)
    ys = [y for ln in res.lines for _, y in ln]
    assert max(ys) - min(ys) == pytest.approx(56 + 40, abs=3)


def test_pen_mapping():
    item = InkTextItem("x", Box(0, 0, 100, 100), pen=PenStyle("marker", "grey", 2.0))
    lines = build.to_lines(item, [[(0, 0), (10, 10)]])
    assert lines[0].tool == si.Pen.MARKER_2
    assert lines[0].color == si.PenColor.GRAY
    assert lines[0].points[0].width == round(build.PEN_WIDTH["marker"] * 2.0)
    assert isinstance(lines[0].points[0].pressure, int)


def test_width_minimum_one():
    item = InkTextItem("x", Box(0, 0, 10, 10), pen=PenStyle("fineliner", "black", 0.001))
    assert build.to_lines(item, [[(0, 0), (1, 1)]])[0].points[0].width == 1


def test_shaded_diagram_appends_and_verifies(diagram):
    item = ImageItem(diagram, Box(100, 100, 600, 450), pen=PenStyle("pencil"))
    lines = build.shade_to_lines(item, shade.shade_item(item), item.pen)
    assert lines
    orig = (RM / "Normal_AB.rm").read_bytes()
    pg = page.load(orig)
    pg.append_layer("rmdesk", lines)
    page.verify(orig, pg.to_bytes(), len(lines), None)


# -- live compose: frame coordinates -> page lines ------------------------------

def test_frame_lines_point_at_frame_60_60_lands_at_page_minus_642_60():
    # an ink item whose box starts at frame (50, 50); its first stroke point is at (10, 10)
    item = InkTextItem("x", Box(50, 50, 100, 100))
    (line,) = build.frame_to_lines(item, [[(10, 10), (20, 20)]], None, "portrait")
    assert (line.points[0].x, line.points[0].y) == (-642.0, 60.0)  # not shifted twice
    assert (line.points[1].x, line.points[1].y) == (-632.0, 70.0)


def test_frame_lines_landscape_not_calibrated():
    item = InkTextItem("x", Box(50, 50, 100, 100))
    with pytest.raises(NotImplementedError, match="landscape not calibrated"):
        build.frame_to_lines(item, [[(10, 10), (20, 20)]], None, "landscape")


def test_frame_lines_match_to_lines_for_portrait():
    item = InkTextItem("x", Box(1200, 1700, 900, 900))
    pls = [[(0, 0), (500, 500)], [(500, 0), (600, 0)]]
    a = build.frame_to_lines(item, pls, None, "portrait")
    b = build.to_lines(item, pls)
    assert [[(p.x, p.y) for p in ln.points] for ln in a] == \
        [[(p.x, p.y) for p in ln.points] for ln in b]


def test_frame_lines_clipped_to_page_bounds():
    # an item hanging off the bottom-right edge of the frame
    item = InkTextItem("x", Box(1300, 1800, 300, 300))
    lines = build.frame_to_lines(item, [[(0, 0), (300, 300)], [(200, 0), (250, 0)]],
                                 None, "portrait")
    pts = _pts(lines)
    assert pts and len(lines) == 1
    for p in pts:
        assert -702 <= p.x <= 702 and 0 <= p.y <= 1872
    assert lines[0].points[-1].x == pytest.approx(702) or lines[0].points[-1].y == pytest.approx(1872)


def test_frame_lines_unknown_orientation_raises():
    with pytest.raises(ValueError):
        build.frame_to_lines(InkTextItem("x", Box(0, 0, 10, 10)), [[(1, 1), (2, 2)]],
                             None, "sideways")


def test_has_lines_on_page():
    inside = InkTextItem("x", Box(10, 10, 10, 10))
    outside = InkTextItem("x", Box(2000, 10, 10, 10))
    assert build.has_lines_on_page(inside, [[(0, 0), (5, 5)]], None)
    assert not build.has_lines_on_page(outside, [[(0, 0), (5, 5)]], None)


def test_build_has_no_unused_box_import():
    import ast
    src = Path(build.__file__).read_text()
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "Box" in names or "Box" not in src


def test_frame_shade_lines_match_shade_lines_for_portrait(diagram):
    item = ImageItem(diagram, Box(900, 1500, 600, 450), pen=PenStyle("pencil"), rotation=20)
    res = shade.shade_item(item)
    a = build.frame_shade_to_lines(item, res, item.pen, "portrait")
    b = build.shade_to_lines(item, res, item.pen)
    assert a and [[(p.x, p.y, p.width, p.pressure) for p in ln.points] for ln in a] == \
        [[(p.x, p.y, p.width, p.pressure) for p in ln.points] for ln in b]


@pytest.mark.parametrize("orientation, exc", [("landscape", NotImplementedError),
                                              ("sideways", ValueError)])
def test_frame_shade_lines_refuse_other_orientations(diagram, orientation, exc):
    item = ImageItem(diagram, Box(100, 100, 300, 200))
    with pytest.raises(exc):
        build.frame_shade_to_lines(item, shade.shade_item(item), item.pen, orientation)
