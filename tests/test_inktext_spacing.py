"""Ink text line spacing, letter spacing and the extra fonts (compose/inktext)."""
import json
from pathlib import Path

import pytest

from rmdesk.compose import inktext
from rmdesk.compose.inktext import text_to_polylines

GOLDEN = Path(__file__).parent / "fixtures" / "inktext_default.json"


def _same(a, b):
    assert len(a) == len(b)
    for pa, pb in zip(a, b):
        assert len(pa) == len(pb)
        for (x1, y1), (x2, y2) in zip(pa, pb):
            assert x1 == pytest.approx(x2, abs=1e-9) and y1 == pytest.approx(y2, abs=1e-9)


@pytest.mark.parametrize("case", json.loads(GOLDEN.read_text()), ids=lambda c: c["text"][:12])
def test_explicit_default_spacing_is_byte_identical(case):
    res = text_to_polylines(case["text"], case["size"], line_spacing=1.0, letter_spacing=0.0)
    assert json.loads(json.dumps(res.lines)) == case["lines"]  # exact floats
    assert res.skipped == case["skipped"]
    plain = text_to_polylines(case["text"], case["size"])
    assert res.lines == plain.lines and res.stops == plain.stops


def test_spacing_defaults_and_ranges():
    assert inktext.LINE_SPACING_DEFAULT == 1.0 and inktext.LETTER_SPACING_DEFAULT == 0.0
    assert (inktext.LINE_SPACING_MIN, inktext.LINE_SPACING_MAX) == (0.8, 3.0)
    assert (inktext.LETTER_SPACING_MIN, inktext.LETTER_SPACING_MAX) == (-0.1, 0.5)
    assert inktext.line_pitch(40) == pytest.approx(inktext.LINE_HEIGHT * 40)
    assert inktext.line_pitch(40, 2.0) == pytest.approx(2 * inktext.LINE_HEIGHT * 40)


@pytest.mark.parametrize("ls", [0.8, 1.5, 3.0])
def test_line_spacing_changes_the_line_pitch(ls):
    one = text_to_polylines("H", 40)
    two = text_to_polylines("H\nH", 40, line_spacing=ls)
    base = text_to_polylines("H\nH", 40)
    n = len(one.lines)
    first, second = two.lines[:n], two.lines[n:]
    _same(first, one.lines)  # the first line does not move
    pitch = inktext.LINE_HEIGHT * ls * 40
    _same(second, [[(x, y + pitch) for x, y in pl] for pl in one.lines])
    assert max(y for pl in two.lines for _, y in pl) != \
        pytest.approx(max(y for pl in base.lines for _, y in pl))


def test_letter_spacing_widens_each_advance():
    plain = text_to_polylines("abc d", 40)
    wide = text_to_polylines("abc d", 40, letter_spacing=0.25)
    extra = 0.25 * 40
    st, sw = plain.stops[0], wide.stops[0]
    for i in range(len(st)):
        assert sw[i] == pytest.approx(st[i] + i * extra)
    # each glyph moves right by its index times the extra
    for ch_i, ch in enumerate("abc d"):
        if ch == " ":
            continue
        g = text_to_polylines(ch, 40)
        xs_plain = min(x for pl in g.lines for x, _ in pl)
        assert any(abs(x - (xs_plain + st[ch_i] + ch_i * extra)) < 1e-6
                   for pl in wide.lines for x, _ in pl)
    assert max(x for pl in wide.lines for x, _ in pl) == pytest.approx(
        max(x for pl in plain.lines for x, _ in pl) + 4 * extra)


def test_negative_letter_spacing_tightens():
    plain = text_to_polylines("abcd", 40)
    tight = text_to_polylines("abcd", 40, letter_spacing=-0.1)
    assert max(x for pl in tight.lines for x, _ in pl) == pytest.approx(
        max(x for pl in plain.lines for x, _ in pl) - 3 * 0.1 * 40)


def test_letter_spacing_ignores_skipped_characters():
    a = text_to_polylines("a🙂b", 40, letter_spacing=0.5)
    b = text_to_polylines("ab", 40, letter_spacing=0.5)
    assert a.lines == b.lines and a.skipped == ["🙂"]
    assert a.stops[0] == [b.stops[0][0], b.stops[0][1], b.stops[0][1], b.stops[0][2]]


@pytest.mark.parametrize("align", ["center", "right"])
def test_alignment_with_letter_and_line_spacing(align):
    res = text_to_polylines("Wide line\nab", 40, align=align, letter_spacing=0.2, line_spacing=2)
    left = text_to_polylines("Wide line\nab", 40, letter_spacing=0.2, line_spacing=2)
    pitch = inktext.line_pitch(40, 2)

    def ext(r, row):
        xs = [x for pl in r.lines for x, y in pl if row * pitch - 10 <= y <= row * pitch + 55]
        return min(xs), max(xs)

    (_, w1), (s0, s1) = ext(left, 0), ext(left, 1)
    (_, a1), (b0, b1) = ext(res, 0), ext(res, 1)
    shift = {"center": (w1 - s1) / 2, "right": w1 - s1}[align]
    assert a1 == pytest.approx(w1)
    assert b0 == pytest.approx(s0 + shift) and b1 == pytest.approx(s1 + shift)
    assert res.stops[1][0] == pytest.approx(left.stops[1][0] + shift)


def test_spacing_scales_linearly_with_size():
    for kw in ({"line_spacing": 2.2}, {"letter_spacing": 0.35}, {"letter_spacing": -0.1},
               {"line_spacing": 0.8, "letter_spacing": 0.5, "italic": True, "bold": True,
                "font": "cursive", "align": "center"}):
        a = text_to_polylines("Ab\nc d", 40, **kw)
        b = text_to_polylines("Ab\nc d", 100, **kw)
        _same(b.lines, [[(x * 2.5, y * 2.5) for x, y in pl] for pl in a.lines])
        for ra, rb in zip(a.stops, b.stops):
            assert rb == pytest.approx([x * 2.5 for x in ra])


NEW = ("cursive", "gothicita", "greek", "cyrillic")


def test_new_fonts_listed_in_order():
    assert inktext.FONTS == ("sans", "serif", "script", "gothic") + NEW


def test_new_font_faces():
    f = inktext.face
    assert f("cursive", False, False) == ("cursive", False, False)
    assert f("cursive", True, True) == ("cursive", True, True)  # synthesized both
    assert f("gothicita", False, False) == ("gothicita", False, False)
    assert f("gothicita", True, False) == ("gothicita", True, False)
    assert f("greek", False, False) == ("greeks", False, False)
    assert f("greek", True, False) == ("greekc", False, False)  # the real complex face
    assert f("greek", True, True) == ("greekc", False, True)
    assert f("cyrillic", False, False) == ("cyrillic", False, False)
    assert f("cyrillic", True, False) == ("cyrillic", True, False)


@pytest.mark.parametrize("font", NEW)
def test_new_fonts_render_and_report_skipped(font):
    res = text_to_polylines("Hab 1", 40, font=font)
    assert res.lines and res.skipped == []
    assert res.lines != text_to_polylines("Hab 1", 40).lines
    assert text_to_polylines("a 🙂", 40, font=font).skipped == ["🙂"]
    for b in (False, True):
        for i in (False, True):
            assert text_to_polylines("Hi", 40, font=font, bold=b, italic=i).lines


def test_greek_symbols_face_has_no_y_or_z():
    """greeks maps Latin keys to Greek glyphs and has none for y/z: they are reported."""
    assert text_to_polylines("xyz", 40, font="greek").skipped == ["y", "z"]
