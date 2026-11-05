"""Basic shapes for Compose (compose/shapes): rectangle, line, arrow and sticky note."""
import ast
import math
from pathlib import Path

import pytest

from rmdesk.compose import build, inktext, shapes
from rmdesk.compose.frame import KEYBOARD, frame_to_page
from rmdesk.compose.items import Box, PenStyle, ShapeItem
from rmdesk.compose.place import rotate_polylines
from rmdesk.compose.shapes import ShapeOptions, shape_polylines
from rmdesk.notebook import page

RM = Path(__file__).parent / "fixtures" / "rm"
FIXTURES = sorted(p.name for p in RM.glob("*.rm"))


def _inside(pls, w, h, eps=1e-9):
    return all(-eps <= x <= w + eps and -eps <= y <= h + eps for pl in pls for x, y in pl)


def test_shapes_has_no_qt():
    tree = ast.parse(Path(shapes.__file__).read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith(("PySide", "PyQt")) for m in mods)


def test_kinds():
    assert shapes.KINDS == ("rectangle", "line", "arrow", "note")
    with pytest.raises(ValueError):
        shape_polylines("circle", 10, 10)


# -- rectangle ---------------------------------------------------------------------

def test_rectangle_is_one_closed_outline():
    (pl,) = shape_polylines("rectangle", 300, 200)
    assert pl[0] == pl[-1] == (0.0, 0.0)
    assert set(pl) == {(0.0, 0.0), (300.0, 0.0), (300.0, 200.0), (0.0, 200.0)}
    assert len(pl) == 5


def test_rectangle_default_is_outline_only():
    assert ShapeOptions().fill is False
    assert len(shape_polylines("rectangle", 300, 200, ShapeOptions())) == 1


def test_rectangle_fill_hatches_inside_the_box_only():
    w, h = 300.0, 200.0
    pls = shape_polylines("rectangle", w, h, ShapeOptions(fill=True))
    outline, hatch = pls[0], pls[1:]
    assert outline[0] == outline[-1]
    assert len(hatch) > 20
    assert _inside(hatch, w, h)
    for a, b in hatch:  # straight 45-degree strokes, x + y constant
        assert a[0] + a[1] == pytest.approx(b[0] + b[1])
        assert math.dist(a, b) > 0
    # the shading's default spacing (5 px) between neighbouring hatch lines
    assert ShapeOptions().hatch == 5
    cs = sorted(a[0] + a[1] for a, _b in hatch)
    gaps = {round((q - p) / math.sqrt(2), 6) for p, q in zip(cs, cs[1:])}
    assert gaps == {5.0}
    # the whole box is covered: the first and last lines are within one spacing of the corners
    assert cs[0] / math.sqrt(2) <= 5 and (w + h - cs[-1]) / math.sqrt(2) <= 5


def test_rectangle_hatch_spacing_option():
    a = shape_polylines("rectangle", 200, 200, ShapeOptions(fill=True, hatch=10))
    b = shape_polylines("rectangle", 200, 200, ShapeOptions(fill=True, hatch=5))
    assert len(b) > len(a) > 1


# -- line and arrow ----------------------------------------------------------------

def test_line_runs_along_the_box_middle():
    assert shape_polylines("line", 400, 40) == [[(0.0, 20.0), (400.0, 20.0)]]


def test_arrow_head_angle_and_size():
    w, h = 400.0, 40.0
    shaft, head = shape_polylines("arrow", w, h, ShapeOptions(thickness=1.0))
    assert shaft == [(0.0, 20.0), (w, 20.0)]
    a, tip, b = head
    assert tip == (w, 20.0)
    for p in (a, b):
        ang = math.degrees(math.atan2(abs(p[1] - tip[1]), tip[0] - p[0]))
        assert ang == pytest.approx(shapes.HEAD_ANGLE)
    assert a[1] < tip[1] < b[1]
    size = math.dist(a, tip)
    assert size == pytest.approx(math.dist(b, tip))
    assert size == pytest.approx(shapes.arrow_head(w, 1.0))


def test_arrow_head_grows_with_thickness_and_length_and_is_clamped():
    hd = shapes.arrow_head
    assert hd(400, 2.0) > hd(400, 1.0)  # thicker: larger
    assert hd(300, 1.0) > hd(100, 1.0)  # longer: larger
    assert hd(5000, 1.0) == hd(10000, 1.0)  # clamped above (per thickness)
    assert hd(5000, 1.0) == pytest.approx(shapes.HEAD_MAX * 1.0)
    assert hd(60, 3.0) <= 0.5 * 60  # never more than half the line
    assert hd(400, 0.5) >= shapes.HEAD_MIN * 0.5


def test_double_arrow_has_a_head_at_both_ends():
    w, h = 300.0, 40.0
    pls = shape_polylines("arrow", w, h, ShapeOptions(double=True))
    assert len(pls) == 3
    shaft, end, start = pls
    assert shaft == [(0.0, 20.0), (w, 20.0)]
    assert end[1] == (w, 20.0) and start[1] == (0.0, 20.0)
    for a, tip, b in (end, start):
        ang = math.degrees(math.atan2(abs(a[1] - tip[1]), abs(tip[0] - a[0])))
        assert ang == pytest.approx(shapes.HEAD_ANGLE)
    assert start[0][0] > 0 and end[0][0] < w  # the heads point outwards
    assert math.dist(start[0], start[1]) == pytest.approx(math.dist(end[0], end[1]))


# -- sticky note ---------------------------------------------------------------

def test_note_outline_and_fold():
    w, h = 360.0, 300.0
    f = shapes.note_fold(w, h)
    assert 0 < f < min(w, h) / 2
    outline, fold = shape_polylines("note", w, h)
    assert outline[0] == outline[-1]
    assert outline[:-1] == [(0.0, 0.0), (w - f, 0.0), (w, f), (w, h), (0.0, h)]
    assert (w, 0.0) not in outline  # the corner is folded down
    assert fold == [(w - f, 0.0), (w - f, f), (w, f)]  # the fold triangle's inner edges
    assert _inside([outline, fold], w, h)


def test_note_text_area_is_clear_of_the_fold():
    w, h = 360.0, 300.0
    x, y, iw, ih = shapes.note_text_rect(w, h)
    assert x > 0 and iw == pytest.approx(w - 2 * x)
    assert y >= shapes.note_fold(w, h)  # the text starts below the fold
    assert y + ih <= h


# -- inktext wrapping --------------------------------------------------------------

def test_text_wraps_to_max_width():
    text = "the quick brown fox jumps over the lazy dog"
    one = inktext.text_to_polylines(text, 30)
    assert len(one.stops) == 1
    res = inktext.text_to_polylines(text, 30, max_width=200)
    assert len(res.stops) > 2
    assert max(x for pl in res.lines for x, _ in pl) <= 200
    # each visual row starts at a text position; rows join back to the text
    assert res.row_starts[0] == 0 and res.row_starts == sorted(res.row_starts)
    assert len(res.row_starts) == len(res.stops)
    starts = res.row_starts + [len(text)]
    rows = [text[a:b] for a, b in zip(starts, starts[1:])]
    assert "".join(rows) == text
    assert all(len(st) == len(r) + 1 for st, r in zip(res.stops, rows))
    assert all(r.endswith(" ") for r in rows[:-1])  # broken after a space


def test_wrap_keeps_hard_breaks_and_breaks_long_words():
    res = inktext.text_to_polylines("ab\nWWWWWWWWWWWW", 30, max_width=100)
    assert res.row_starts[:2] == [0, 3]
    assert len(res.row_starts) > 2
    assert max(x for pl in res.lines for x, _ in pl) <= 100


@pytest.mark.parametrize("bold,italic,align", [(True, True, "left"), (False, True, "right"),
                                               (False, False, "center")])
def test_wrapped_styles_stay_inside(bold, italic, align):
    res = inktext.text_to_polylines("Wide words wrap within the width", 30, bold=bold,
                                    italic=italic, align=align, max_width=180)
    xs = [x for pl in res.lines for x, _ in pl]
    assert min(xs) >= 0 and max(xs) <= 180


def test_no_max_width_row_starts_follow_line_breaks():
    res = inktext.text_to_polylines("ab\ncd", 20)
    assert res.row_starts == [0, 3]


# -- send path ---------------------------------------------------------------------

def _all_kinds():
    # all inside frame y 234..1638: the page's width in the keyboard layout (nothing is cut)
    return [ShapeItem("rectangle", Box(100, 300, 300, 200), fill=True),
            ShapeItem("rectangle", Box(500, 350, 200, 120)),
            ShapeItem("line", Box(150, 900, 400, 40), rotation=30),
            ShapeItem("arrow", Box(300, 1200, 400, 40), rotation=315),
            ShapeItem("arrow", Box(600, 1500, 300, 40), double=True),
            ShapeItem("note", Box(700, 400, 360, 300), rotation=15, text="hi")]


def _local(item):
    return shape_polylines(item.kind, item.box.w, item.box.h, item.options())


@pytest.mark.parametrize("layout,scroll", [("portrait", 0.0), (KEYBOARD, 0.0), (KEYBOARD, 350.0)])
def test_sent_points_are_the_preview_mapped_once(layout, scroll):
    for item in _all_kinds():
        local = _local(item)
        b = item.box
        preview = rotate_polylines([[(b.x + x, b.y + y) for x, y in pl] for pl in local],
                                   b.x + b.w / 2, b.y + b.h / 2, item.rotation)
        want = [frame_to_page(x, y, "portrait", layout=layout, scroll=scroll)
                for pl in preview for x, y in pl]
        lines = build.frame_to_lines(item, local, None, "portrait", layout=layout,
                                     scroll=scroll)
        got = [(p.x, p.y) for ln in lines for p in ln.points]
        assert len(got) == len(want), item.kind
        for g, w_ in zip(got, want):
            assert g == pytest.approx(w_, abs=1e-6)


def test_shape_options_carry_thickness_from_the_pen():
    item = ShapeItem("arrow", Box(0, 0, 400, 40), pen=PenStyle(thickness=2.0), double=True)
    assert item.options() == ShapeOptions(fill=False, double=True, thickness=2.0)


@pytest.mark.parametrize("name", FIXTURES)
def test_pages_with_shapes_verify(name):
    lines = []
    for item in _all_kinds():
        lines += build.frame_to_lines(item, _local(item), None, "portrait")
    assert lines
    for ln in lines:
        page._check_line(ln)
    orig = (RM / name).read_bytes()
    pg = page.load(orig)
    pg.append_to_rmdesk_layer(lines)
    page.verify(orig, pg.to_bytes(), len(lines), None)


def test_text_gaps_merge_padded_rows():
    rows = [[(0, 10), (50, 30)], [(5, 28), (40, 45)], [(0, 100), (9, 110)]]
    assert shapes.text_gaps(rows, pad=2) == [(8, 47), (98, 112)]
    assert shapes.text_gaps([]) == []


def test_hatch_with_gaps_is_cut_and_without_is_unchanged():
    full = shape_polylines("rectangle", 200, 100, ShapeOptions(fill=True))
    assert shape_polylines("rectangle", 200, 100, ShapeOptions(fill=True), []) == full
    cut = shape_polylines("rectangle", 200, 100, ShapeOptions(fill=True), [(40.0, 60.0)])
    assert len(cut) > len(full)
    assert all(not (40 + 1e-9 < y < 60 - 1e-9) for pl in cut[1:] for _, y in pl)
    assert all(abs((a[0] + a[1]) - (b[0] + b[1])) < 1e-9 for pl in cut[1:]
               for a, b in zip(pl, pl[1:]))  # every piece still on its 45-degree line
    assert _inside(cut, 200, 100)


def test_rectangle_text_area_has_no_fold():
    x, y, iw, ih = shapes.note_text_rect(400, 280, fold=False)
    assert (x, y) == (shapes.NOTE_PAD, shapes.NOTE_PAD)
    assert (iw, ih) == (400 - 2 * shapes.NOTE_PAD, 280 - 2 * shapes.NOTE_PAD)
    assert shapes.note_text_rect(400, 280)[1] > y
