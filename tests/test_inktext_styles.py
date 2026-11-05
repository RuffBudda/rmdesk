"""Ink text fonts, bold, italic and alignment (compose/inktext.text_to_polylines)."""
import json
import math
from pathlib import Path

import pytest

from rmdesk.compose import inktext
from rmdesk.compose.inktext import text_to_polylines

GOLDEN = Path(__file__).parent / "fixtures" / "inktext_default.json"


def _xs(lines):
    return [x for pl in lines for x, _ in pl]


def _ys(lines):
    return [y for pl in lines for _, y in pl]


def _same(a, b):
    assert len(a) == len(b)
    for pa, pb in zip(a, b):
        assert len(pa) == len(pb)
        for (x1, y1), (x2, y2) in zip(pa, pb):
            assert x1 == pytest.approx(x2, abs=1e-9) and y1 == pytest.approx(y2, abs=1e-9)


@pytest.mark.parametrize("case", json.loads(GOLDEN.read_text()), ids=lambda c: c["text"][:12])
def test_defaults_identical_to_before(case):
    """Recorded from the pre-style text_to_polylines: defaults (and explicit defaults) match."""
    for res in (text_to_polylines(case["text"], case["size"]),
                text_to_polylines(case["text"], case["size"], font="sans", bold=False,
                                  italic=False, align="left")):
        _same(res.lines, case["lines"])
        assert res.skipped == case["skipped"]


def test_face_mapping():
    f = inktext.face
    assert f("sans", False, False) == ("futural", False, False)
    assert f("sans", True, False) == ("futuram", False, False)
    assert f("sans", False, True) == ("futural", False, True)  # no italic: sheared
    assert f("sans", True, True) == ("futuram", False, True)
    assert f("serif", False, False) == ("timesr", False, False)
    assert f("serif", True, False) == ("timesrb", False, False)
    assert f("serif", False, True) == ("timesi", False, False)
    assert f("serif", True, True) == ("timesib", False, False)
    assert f("script", False, False) == ("scripts", False, False)
    assert f("script", True, False) == ("scriptc", False, False)
    assert f("script", False, True) == ("scripts", False, True)
    assert f("gothic", False, False) == ("gothiceng", False, False)
    assert f("gothic", True, False) == ("gothiceng", True, False)  # no bold: second pass
    assert f("gothic", True, True) == ("gothiceng", True, True)
    with pytest.raises(ValueError):
        f("comic", False, False)


@pytest.mark.parametrize("font", inktext.FONTS)
def test_each_font_draws_and_differs(font):
    res = text_to_polylines("Hag", 40, font=font)
    assert res.lines and not res.skipped
    if font != "sans":
        assert res.lines != text_to_polylines("Hag", 40).lines


def test_bold_uses_the_bold_face():
    plain = text_to_polylines("Hello", 40)
    bold = text_to_polylines("Hello", 40, bold=True)
    assert bold.lines != plain.lines
    assert sum(len(p) for p in bold.lines) > sum(len(p) for p in plain.lines)


def test_synthesized_bold_is_a_second_offset_pass():
    plain = text_to_polylines("Hi", 40, font="gothic")
    bold = text_to_polylines("Hi", 40, font="gothic", bold=True)
    assert len(bold.lines) == 2 * len(plain.lines)
    d = inktext.BOLD_OFFSET * 40
    first, second = bold.lines[:len(plain.lines)], bold.lines[len(plain.lines):]
    _same(first, plain.lines)
    _same(second, [[(x + d, y) for x, y in pl] for pl in plain.lines])


def test_synthesized_italic_shears_about_12_degrees():
    plain = text_to_polylines("l", 100)
    it = text_to_polylines("l", 100, italic=True)
    # 'l' is one vertical stroke: sheared, its top moves right of its bottom by h * tan(12)
    (x0, y0), (x1, y1) = plain.lines[0][0], plain.lines[0][-1]
    assert x0 == pytest.approx(x1)
    (a0, b0), (a1, b1) = it.lines[0][0], it.lines[0][-1]
    assert b0 == pytest.approx(y0) and b1 == pytest.approx(y1)
    slope = (a1 - a0) / (b0 - b1)  # x gained per pixel up
    assert slope == pytest.approx(math.tan(math.radians(inktext.ITALIC_SHEAR_DEG)), rel=1e-6)
    assert inktext.ITALIC_SHEAR_DEG == pytest.approx(12)
    assert min(_xs(it.lines)) >= 0


def test_real_italic_face_is_not_sheared():
    assert text_to_polylines("Hi", 40, font="serif", italic=True).lines != \
        text_to_polylines("Hi", 40, font="serif").lines
    assert inktext.face("serif", False, True)[2] is False


def _line_rows(res, size, rows):
    """x extents of each text row (by y band)."""
    out = []
    for r in range(rows):
        top = r * inktext.LINE_HEIGHT * size
        xs = [x for pl in res.lines for x, y in pl if top - 0.2 * size <= y <= top + 1.3 * size]
        out.append((min(xs), max(xs)))
    return out


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_alignment_per_line(align):
    text = "Wide line here\nab"
    res = text_to_polylines(text, 40, align=align)
    left = text_to_polylines(text, 40)
    (w0, w1), (s0, s1) = _line_rows(left, 40, 2)
    (_a0, a1), (b0, b1) = _line_rows(res, 40, 2)
    assert a1 == pytest.approx(w1)  # the widest line sets the box width and stays put
    shift = {"left": 0, "center": (w1 - s1) / 2, "right": w1 - s1}[align]
    assert b0 == pytest.approx(s0 + shift) and b1 == pytest.approx(s1 + shift)
    assert max(_xs(res.lines)) == pytest.approx(max(_xs(left.lines)))


def test_unknown_alignment_refused():
    with pytest.raises(ValueError):
        text_to_polylines("x", 40, align="justify")


def test_styles_scale_linearly_with_size():
    """The scene item scales strokes made at one size; every style must stay linear."""
    for kw in ({}, {"bold": True, "font": "gothic"}, {"italic": True}, {"align": "right"},
               {"font": "script", "italic": True, "bold": True, "align": "center"}):
        a = text_to_polylines("Ab\nc", 40, **kw)
        b = text_to_polylines("Ab\nc", 100, **kw)
        _same(b.lines, [[(x * 2.5, y * 2.5) for x, y in pl] for pl in a.lines])


def test_skipped_reported_for_every_font():
    for font in inktext.FONTS:
        assert text_to_polylines("a 🙂", 40, font=font).skipped == ["🙂"]


def test_caret_stops():
    res = text_to_polylines("ab\nc", 40)
    assert len(res.stops) == 2 and len(res.stops[0]) == 3 and len(res.stops[1]) == 2
    assert res.stops[0][0] == 0 and res.stops[0][1] < res.stops[0][2]
    right = text_to_polylines("ab\nc", 40, align="right")
    assert right.stops[1][0] > res.stops[1][0]
    empty = text_to_polylines("", 40)
    assert empty.stops == [[0.0]]
