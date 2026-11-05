"""Line-art pictures: the engine (compose.lineart) and its lines through build's shade path."""
import ast
from pathlib import Path

import cv2
import numpy as np
import pytest

from rmdesk.compose import build, lineart, shade
from rmdesk.compose.items import Box, ImageItem, PenStyle
from rmdesk.notebook import page

RM = Path(__file__).parent / "fixtures" / "rm"
FIXTURES = sorted(p.name for p in RM.glob("*.rm"))
FINE = PenStyle("fineliner", "black")


def _write(tmp_path, img, name="img.png"):
    p = tmp_path / name
    assert cv2.imwrite(str(p), img)
    return p


def _white(w=200, h=200):
    return np.full((h, w), 255, np.uint8)


def _closed(st, tol=1.0):
    return len(st) >= 4 and np.hypot(st[0][0] - st[-1][0], st[0][1] - st[-1][1]) <= tol


def _dist_to_rect(x, y, x0, y0, x1, y1):
    if x0 <= x <= x1 and y0 <= y <= y1:
        return min(x - x0, x1 - x, y - y0, y1 - y)
    dx = max(x0 - x, 0, x - x1)
    dy = max(y0 - y, 0, y - y1)
    return float(np.hypot(dx, dy))


def test_box_outline_is_one_closed_polyline(tmp_path):
    img = _white()
    cv2.rectangle(img, (40, 40), (160, 160), 0, 3)
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200)
    assert len(res.strokes) == 1
    st = res.strokes[0]
    assert _closed(st) and len(st) <= 9  # four (slightly cut) corners, closed
    for x, y, _w, _p in st:
        assert _dist_to_rect(x, y, 40.5, 40.5, 160.5, 160.5) <= 2.5


def test_cross_is_two_strokes(tmp_path):
    img = _white()
    cv2.line(img, (20, 100), (180, 100), 0, 3)
    cv2.line(img, (100, 20), (100, 180), 0, 3)
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200)
    assert len(res.strokes) == 2
    for st in res.strokes:
        (x0, y0), (x1, y1) = st[0][:2], st[-1][:2]
        assert np.hypot(x1 - x0, y1 - y0) > 140  # each runs right across


def test_thick_filled_square_is_its_outline(tmp_path):
    img = _white()
    cv2.rectangle(img, (50, 50), (150, 150), 0, -1)
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200, line_px=4.0)
    assert len(res.strokes) == 1
    st = res.strokes[0]
    assert _closed(st) and len(st) <= 7
    for x, y, _w, _p in st:  # inset by about half a pen width: the pen edge is the shape edge
        d = _dist_to_rect(x, y, 50, 50, 151, 151)
        assert 0.5 <= d <= 4.0


def test_thin_diagonal_is_one_simplified_line(tmp_path):
    img = _white()
    cv2.line(img, (20, 30), (180, 170), 0, 2, cv2.LINE_AA)
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200)
    assert len(res.strokes) == 1
    st = res.strokes[0]
    assert 2 <= len(st) <= 3
    assert np.hypot(st[-1][0] - st[0][0], st[-1][1] - st[0][1]) > 190


def test_constant_width_and_pressure(tmp_path):
    img = _white()
    cv2.circle(img, (100, 100), 60, 0, 3)
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200, width=16, pressure=220)
    assert res.strokes and res.spacing_used == 0 and not res.capped
    assert {(w, p) for st in res.strokes for _x, _y, w, p in st} == {(16.0, 220.0)}


def test_item_uses_the_pen_width_and_thickness(tmp_path):
    img = _white()
    cv2.line(img, (20, 100), (180, 100), 0, 3)
    p = _write(tmp_path, img)
    item = ImageItem(p, Box(0, 0, 200, 200), pen=PenStyle("ballpoint", "black", 2.0))
    res = lineart.lineart_item(item)
    assert {w for st in res.strokes for _x, _y, w, _p in st} == {build.PEN_WIDTH["ballpoint"]}
    lines = build.shade_to_lines(item, res, item.pen)
    assert {pt.width for ln in lines for pt in ln.points} == {build.PEN_WIDTH["ballpoint"] * 2}
    assert {pt.pressure for ln in lines for pt in ln.points} == {build.PRESSURE}


def test_points_stay_in_the_box(tmp_path):
    img = _white(300, 150)
    cv2.rectangle(img, (0, 0), (299, 149), 0, 4)  # ink right up to the edges
    cv2.line(img, (0, 0), (299, 149), 0, 3)
    res = lineart.lineart_image(_write(tmp_path, img), 400, 400)  # letterboxed vertically
    pts = [p for st in res.strokes for p in st]
    assert pts
    for x, y, _w, _p in pts:
        assert 0 <= x <= 400 and 100 - 1 <= y <= 300 + 1


def test_blank_and_transparent_give_nothing(tmp_path):
    white = _write(tmp_path, _white(), "white.png")
    clear = _write(tmp_path, np.zeros((100, 150, 4), np.uint8), "clear.png")
    for p in (white, clear):
        res = lineart.lineart_image(p, 300, 200)
        assert res.strokes == [] and res.capped is False


def test_transparent_parts_stay_empty(tmp_path):
    img = np.zeros((200, 200, 4), np.uint8)  # transparent black everywhere...
    cv2.line(img, (20, 100), (180, 100), (0, 0, 0, 255), 3)  # ...one opaque black line
    res = lineart.lineart_image(_write(tmp_path, img), 200, 200)
    assert len(res.strokes) == 1
    assert all(abs(y - 100.5) < 2 for _x, y, _w, _p in res.strokes[0])
    inv = lineart.lineart_image(_write(tmp_path, img, "b.png"), 200, 200, invert=True)
    assert inv.strokes == []  # inverted, the black line is white; the clear rest stays clear


def test_invert_draws_light_lines_on_dark(tmp_path):
    img = np.zeros((200, 200), np.uint8)
    cv2.rectangle(img, (40, 40), (160, 160), 255, 3)
    p = _write(tmp_path, img)
    res = lineart.lineart_image(p, 200, 200, invert=True)
    assert len(res.strokes) == 1 and _closed(res.strokes[0])
    plain = lineart.lineart_image(p, 200, 200)
    assert plain.strokes != res.strokes  # not inverted: the dark ground is a filled region


def test_threshold_picks_up_faint_lines(tmp_path):
    img = _white()
    cv2.line(img, (20, 50), (180, 50), 0, 3)
    cv2.line(img, (20, 150), (180, 150), 150, 3)  # a faint grey line
    p = _write(tmp_path, img)
    strict = lineart.lineart_image(p, 200, 200, threshold=1.5)
    loose = lineart.lineart_image(p, 200, 200, threshold=0.5)
    assert len(strict.strokes) == 1 and len(loose.strokes) == 2


def test_detail_drops_small_bits(tmp_path):
    img = _white()
    cv2.line(img, (20, 100), (180, 100), 0, 3)
    cv2.line(img, (100, 40), (108, 40), 0, 2)  # a short dash
    p = _write(tmp_path, img)
    coarse = lineart.lineart_image(p, 200, 200, detail=1)
    fine = lineart.lineart_image(p, 200, 200, detail=5)
    assert len(coarse.strokes) == 1 and len(fine.strokes) == 2


def test_cap_raises_simplification_then_caps(tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    img = _white(600, 600)
    for _ in range(200):
        c = tuple(int(v) for v in rng.integers(20, 580, 2))
        cv2.circle(img, c, int(rng.integers(5, 40)), 0, 1)
    p = _write(tmp_path, img)
    full = lineart.lineart_image(p, 600, 600, detail=5)
    n = sum(len(s) for s in full.strokes)
    monkeypatch.setattr(lineart, "SHADE_MAX_POINTS", int(n * 0.7))
    simpler = lineart.lineart_image(p, 600, 600, detail=5)
    assert not simpler.capped and sum(len(s) for s in simpler.strokes) <= n * 0.7
    monkeypatch.setattr(lineart, "SHADE_MAX_POINTS", 50)
    capped = lineart.lineart_image(p, 600, 600, detail=5)
    assert capped.capped and 0 < sum(len(s) for s in capped.strokes) <= 50


def test_only_the_visible_part_is_drawn(tmp_path):
    img = _white(400, 400)
    cv2.line(img, (10, 100), (390, 100), 0, 3)
    cv2.line(img, (10, 300), (390, 300), 0, 3)
    p = _write(tmp_path, img)
    # box mostly below the page: only the top line (y ~ page 1872 - 100) lands on it
    res = lineart.lineart_image(p, 400, 400, box_x=100, box_y=page.PAGE_H - 200)
    assert len(res.strokes) == 1 and all(y < 200 for _x, y, _w, _p in res.strokes[0])
    off = lineart.lineart_image(p, 400, 400, box_x=5000, box_y=0)
    assert off.strokes == [] and off.capped is False


def test_numpy_thinning_matches_opencv():
    img = np.zeros((120, 160), np.uint8)
    cv2.rectangle(img, (20, 20), (140, 100), 1, 5)
    cv2.line(img, (20, 100), (140, 20), 1, 4)
    cv2.putText(img, "k?", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 1, 2, cv2.LINE_AA)
    mask = img > 0
    ours = lineart.guo_hall(mask)
    assert 0 < ours.sum() < mask.sum() / 3
    if hasattr(cv2, "ximgproc"):
        ref = cv2.ximgproc.thinning(mask.astype(np.uint8) * 255,
                                    thinningType=cv2.ximgproc.THINNING_GUOHALL) > 0
        assert (ours == ref).all()


def test_small_diagonal_strokes_survive_thinning(tmp_path):
    img = _white(120, 60)
    cv2.putText(img, "k", (20, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2, cv2.LINE_AA)
    res = lineart.lineart_image(_write(tmp_path, img), 120, 60)
    # the k's stem and both arms: right of the stem there is ink above and below the waist
    pts = [(x, y) for st in res.strokes for x, y, _w, _p in st]
    right = [(x, y) for x, y in pts if x > 30]
    assert any(y < 32 for _x, y in right) and any(y > 40 for _x, y in right)


def test_numpy_fallback_gives_the_same_strokes(tmp_path, monkeypatch):
    img = _white()
    cv2.rectangle(img, (40, 40), (160, 160), 0, 3)
    cv2.line(img, (20, 100), (180, 100), 0, 3)
    p = _write(tmp_path, img)
    a = lineart.lineart_image(p, 200, 200)
    monkeypatch.setattr(lineart.cv2, "ximgproc", None, raising=False)
    b = lineart.lineart_image(p, 200, 200)
    assert len(a.strokes) == len(b.strokes) > 0


def _flowchart(path: Path, w=800, h=600) -> Path:
    img = np.full((h, w, 3), 255, np.uint8)
    f = cv2.FONT_HERSHEY_SIMPLEX
    for (x, y, label) in ((60, 60, "Start"), (60, 260, "Check"), (60, 460, "Done")):
        cv2.rectangle(img, (x, y), (x + 220, y + 90), (0, 0, 0), 3)
        cv2.putText(img, label, (x + 50, y + 58), f, 1.1, (0, 0, 0), 2, cv2.LINE_AA)
    for y in (150, 350):
        cv2.arrowedLine(img, (170, y), (170, y + 110), (0, 0, 0), 3, cv2.LINE_AA, tipLength=0.15)
    pts = np.array([[560, 230], [700, 305], [560, 380], [420, 305]], np.int32)
    cv2.polylines(img, [pts], True, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, "ok?", (530, 315), f, 1.0, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.arrowedLine(img, (280, 305), (420, 305), (0, 0, 0), 3, cv2.LINE_AA, tipLength=0.1)
    cv2.rectangle(img, (600, 470), (700, 540), (40, 40, 40), -1)  # a filled block
    assert cv2.imwrite(str(path), img)
    return path


def test_flowchart_is_quick_and_compact(tmp_path):
    import time
    p = _flowchart(tmp_path / "flow.png")
    t = time.perf_counter()
    res = lineart.lineart_image(p, 800, 600)
    assert time.perf_counter() - t < 3.0
    assert 10 < len(res.strokes) < 400 and not res.capped
    assert sum(len(s) for s in res.strokes) < 5000


@pytest.mark.parametrize("name", FIXTURES)
def test_lines_pass_check_line_and_verify(name, tmp_path):
    p = _flowchart(tmp_path / "flow.png")
    item = ImageItem(p, Box(100, 100, 800, 600), pen=FINE, rotation=20)
    res = lineart.lineart_item(item)
    lines = build.shade_to_lines(item, res, FINE)
    assert lines
    for ln in lines:
        page._check_line(ln)
    orig = (RM / name).read_bytes()
    pg = page.load(orig)
    pg.append_to_rmdesk_layer(lines)
    page.verify(orig, pg.to_bytes(), len(lines), None)


def test_guess_diagram_or_photo(tmp_path):
    assert lineart.looks_like_diagram(_flowchart(tmp_path / "flow.png"))
    rng = np.random.default_rng(0)
    photo = cv2.GaussianBlur(rng.integers(0, 256, (300, 400), dtype=np.uint8), (0, 0), 6)
    photo = cv2.normalize(photo, None, 0, 255, cv2.NORM_MINMAX)
    assert not lineart.looks_like_diagram(_write(tmp_path, photo, "photo.png"))
    row = np.linspace(0, 255, 400).astype(np.uint8)  # a gradient: many levels
    assert not lineart.looks_like_diagram(_write(tmp_path, np.tile(row, (200, 1)), "g.png"))
    logo = np.zeros((200, 200, 4), np.uint8)  # transparent with a black ring: a diagram
    cv2.circle(logo, (100, 100), 60, (0, 0, 0, 255), 6)
    assert lineart.looks_like_diagram(_write(tmp_path, logo, "logo.png"))
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"nope")
        lineart.looks_like_diagram(bad)


def test_lineart_has_no_qt():
    tree = ast.parse(Path(lineart.__file__).read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith(("PySide", "PyQt")) for m in mods)
    assert shade.SHADE_MAX_POINTS == lineart.SHADE_MAX_POINTS  # the same budget


# -- text in charts ----------------------------------------------------------------------

# A 700 x 450 "Impact and Effort Matrix" drawn with QPainter (Liberation Sans, anti-aliased):
# bold 16 px title, light grey (#bbb) bold 18 px cell labels, 12 px notes, 13-14 px
# vertical axis labels, an 11 px #555 source line, and a 2 px grid. Box px = picture px.
TEXT_CHART = Path(__file__).parent / "fixtures" / "lineart" / "text_chart.png"
_TEXT = {  # region (x0, y0, x1, y1) -> what is written there
    "title": ((220, 8, 480, 32), "Impact and Effort Matrix"),
    "grey_q1": ((140, 88, 330, 122), "Quick Wins"),
    "grey_q2": ((430, 88, 670, 122), "Major Projects"),
    "grey_q4": ((430, 258, 670, 292), "Thankless Tasks"),
    "note_q1": ((130, 130, 340, 152), "Do these first: high value, little work"),
    "note_q2": ((430, 130, 670, 152), "Plan carefully, worth the effort"),
    "vert_hi": ((48, 80, 72, 180), "High Impact"),
    "vert_lo": ((48, 250, 72, 350), "Low Impact"),
    "axis": ((14, 180, 36, 260), "Impact"),
    "low": ((180, 392, 290, 412), "Low Effort"),
    "source": ((550, 28, 675, 46), "Source: team survey, n = 42."),
}
_SMALL = ("note_q1", "note_q2", "vert_hi", "vert_lo", "source")
_GREY = ("grey_q1", "grey_q2", "grey_q4")


def _drawn(res, w, h, s=4):
    """Where the strokes put ink, drawn with each stroke's own pen width (1 px = 4 units)."""
    c = np.zeros((h * s, w * s), np.uint8)
    for st in res.strokes:
        pen = st[0][2] / shade.WIDTH_UNITS_PER_PX
        p = np.round(np.array([[x * s, y * s] for x, y, *_ in st]) * 16).astype(np.int32)
        cv2.polylines(c, [p.reshape(-1, 1, 2)], False, 255, max(1, int(round(pen * s))),
                      cv2.LINE_AA, shift=4)
    return cv2.resize(c, (w, h), interpolation=cv2.INTER_AREA) > 100


def _holes(mask):
    """Counters (enclosed paper regions) in a mask: the eyes of o, e, a, d..."""
    n, _lab, st, _ = cv2.connectedComponentsWithStats((~mask).astype(np.uint8), connectivity=4)
    return int((st[1:, cv2.CC_STAT_AREA] >= 2).sum()) - 1


@pytest.fixture(scope="module")
def text_chart():
    res = lineart.lineart_image(TEXT_CHART, 700, 450)
    src = 255 - cv2.imread(str(TEXT_CHART), cv2.IMREAD_GRAYSCALE).astype(int)
    return res, src > 40, _drawn(res, 700, 450)


def test_text_chart_glyphs_are_not_blobs(text_chart):
    # readability proxy: the drawn ink stays on the letters (a 4 px pen on 6 px letters
    # fills them in: before, only 87-92 % of the small text's drawn ink was on a letter)
    # and covers them; the letters keep their counters (before: 2 of 38 in small text)
    res, src, drawn = text_chart
    near = lambda m: cv2.dilate(m.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0  # noqa
    src_near, drawn_near = near(src), near(drawn)
    kept = total = 0
    for name, ((x0, y0, x1, y1), _txt) in _TEXT.items():
        s, d = src[y0:y1, x0:x1], drawn[y0:y1, x0:x1]
        on_letters = (d & src_near[y0:y1, x0:x1]).sum() / d.sum()
        covered = (s & drawn_near[y0:y1, x0:x1]).sum() / s.sum()
        assert on_letters >= (0.97 if name in _SMALL else 0.93), name
        assert covered >= 0.95, name
        if name in _SMALL:
            kept += min(_holes(d), _holes(s))
            total += _holes(s)
    assert kept >= 0.5 * total


def test_text_chart_light_grey_labels_come_out(text_chart):
    res, _src, _drawn_ = text_chart
    for name in _GREY:
        (x0, y0, x1, y1), txt = _TEXT[name]
        inside = [st for st in res.strokes
                  if all(x0 <= x <= x1 and y0 <= y <= y1 for x, y, *_ in st)]
        letters = len(txt.replace(" ", ""))
        assert letters * 0.6 <= len(inside) <= letters * 3, name  # whole letters, not crumbs


def test_text_chart_strokes_stay_on_the_picture_ink(text_chart):
    res, src, _drawn_ = text_chart
    far = cv2.distanceTransform((~src).astype(np.uint8), cv2.DIST_L2, 5)
    for st in res.strokes:
        for x, y, *_ in st:
            assert far[min(int(y), 449), min(int(x), 699)] <= 2.5


def test_text_chart_grid_stays_single_clean_strokes(text_chart):
    res, _src, _drawn_ = text_chart
    long_ = [np.array(st) for st in res.strokes
             if max(np.ptp([p[0] for p in st]), np.ptp([p[1] for p in st])) > 100]
    assert len(long_) == 3  # the frame, the two dividers
    frame = max(long_, key=len)
    assert _closed([tuple(p) for p in frame]) and len(frame) <= 9
    assert all(len(a) == 2 for a in long_ if a is not frame)
    assert all(set(a[:, 2]) == {16.0} for a in long_)  # the picture's pen
    # letters are drawn finer than the pen, never wider; pressure stays constant
    widths = {p[2] for st in res.strokes for p in st}
    assert min(widths) >= 4.0 and max(widths) == 16.0
    assert {p[3] for st in res.strokes for p in st} == {220.0}


def test_full_page_text_chart_is_quick(tmp_path):
    import time
    img = cv2.imread(str(TEXT_CHART))
    img = cv2.resize(img, (1404, 903), interpolation=cv2.INTER_CUBIC)
    page_img = np.full((1872, 1404, 3), 255, np.uint8)
    page_img[:903], page_img[940:1843] = img, img
    p = _write(tmp_path, page_img)
    t = time.perf_counter()
    res = lineart.lineart_image(p, 1404, 1872)
    assert time.perf_counter() - t < 2.0  # about 0.35 s here
    assert not res.capped and sum(len(s) for s in res.strokes) < 12000


def test_faint_text_found_without_thickening_black_lines(tmp_path):
    img = _white(300, 120)
    cv2.line(img, (10, 20), (290, 20), 0, 3)
    cv2.putText(img, "grey", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 187, 2, cv2.LINE_AA)
    p = _write(tmp_path, img)
    res = lineart.lineart_image(p, 300, 120)
    line = [st for st in res.strokes if all(abs(y - 20.5) < 1.5 for _x, y, *_ in st)]
    assert len(line) == 1 and len(line[0]) == 2  # one straight stroke at the line's centre
    grey = [st for st in res.strokes if all(50 <= y <= 90 for _x, y, *_ in st)]
    assert len(grey) >= 4  # g, r, e, y
    strict = lineart.lineart_image(p, 300, 120, threshold=1.5).strokes
    assert len(strict) == 1 and all(abs(y - 20.5) < 1.5 for _x, y, *_ in strict[0])  # no grey


def test_small_marks_keep_dots_and_short_strokes(tmp_path):
    img = _white(120, 60)
    cv2.line(img, (30, 20), (30, 40), 0, 1, cv2.LINE_AA)  # a small "t": stem...
    cv2.line(img, (27, 26), (33, 26), 0, 1, cv2.LINE_AA)  # ...and short crossbar
    cv2.line(img, (60, 28), (60, 40), 0, 1, cv2.LINE_AA)  # an "i": stem...
    cv2.circle(img, (60, 23), 1, 0, -1, cv2.LINE_AA)  # ...and its dot
    cv2.circle(img, (80, 39), 1, 0, -1, cv2.LINE_AA)  # a full stop
    res = lineart.lineart_image(_write(tmp_path, img), 120, 60)
    pts = [(x, y) for st in res.strokes for x, y, *_ in st]
    assert any(x < 29 and abs(y - 26.5) < 1.5 for x, y in pts)  # crossbar, left of the stem
    assert any(x > 32 and abs(y - 26.5) < 1.5 for x, y in pts)  # and right of it
    for cx, cy in ((60.5, 23.5), (80.5, 39.5)):
        assert any(np.hypot(x - cx, y - cy) < 1.5 for x, y in pts)
