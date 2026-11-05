import pytest

from rmdesk.compose import frame
from rmdesk.compose import calibration as cal
from rmdesk.compose.calibration import (LANDSCAPE_CANDIDATES, MARK_POINTS, PEN_NAMES,
                                        build_marks, build_page)
from rmdesk.notebook import page as pagemod
from rmdesk.notebook.opened import OpenedPage

FIXTURE = "tests/fixtures/rm/user-3.29.1.0-rmdesk-test.rm"


def test_portrait_formula():
    assert frame.frame_to_page(0, 0, "portrait") == (-702, 0)
    assert frame.frame_to_page(1404, 1872, "portrait") == (702, 1872)
    assert frame.frame_to_page(702, 936, "portrait") == (0, 936)


def test_landscape_is_not_calibrated():
    with pytest.raises(NotImplementedError, match="landscape not calibrated"):
        frame.frame_to_page(60, 60, "landscape")
    with pytest.raises(NotImplementedError):
        frame.polylines_to_page([[(0, 0)]], "landscape")


@pytest.mark.parametrize("bad", ["Portrait", "square", "", "rot180"])
def test_unknown_orientation_raises(bad):
    with pytest.raises(ValueError, match="unknown orientation"):
        frame.frame_to_page(0, 0, bad)


def test_polylines_to_page_maps_every_point():
    out = frame.polylines_to_page([[(0, 0), (1404, 1872)], [(702, 936)]], "portrait")
    assert out == [[(-702, 0), (702, 1872)], [(0, 936)]]


_RECTS = {"identity": (-702, 0, 702, 1872), "rot90": (-936, 0, 936, 1404),
          "rot270": (-936, 0, 936, 1404)}


def _r(p):
    return (round(p[0], 6), round(p[1], 6))


@pytest.mark.parametrize("name", ["identity", "rot90", "rot270"])
def test_landscape_candidates_map_corners_and_centre(name):
    f, rect, _pen = LANDSCAPE_CANDIDATES[name]
    assert rect == _RECTS[name]
    x0, y0, x1, y1 = rect
    corners = [_r(f(u, v)) for u, v in [(0, 0), (1404, 0), (0, 1872), (1404, 1872)]]
    assert set(corners) == {(x0, y0), (x1, y0), (x0, y1), (x1, y1)}
    assert len(set(corners)) == 4
    assert _r(f(702, 936)) == ((x0 + x1) / 2, (y0 + y1) / 2)


def test_candidate_formulas_and_pens():
    c = LANDSCAPE_CANDIDATES
    assert list(c) == ["identity", "rot90", "rot270"]
    assert c["identity"][0](100, 200) == frame.frame_to_page(100, 200, "portrait")
    # rot90: frame top-left -> page top-right; frame top-right -> page bottom-right
    assert c["rot90"][0](0, 0) == (936, 0) and c["rot90"][0](1404, 0) == (936, 1404)
    # rot270: frame top-left -> page bottom-left; frame top-right -> page top-left
    assert c["rot270"][0](0, 0) == (-936, 1404) and c["rot270"][0](1404, 0) == (-936, 0)
    assert [(p.tool, p.color) for _f, _r2, p in c.values()] == [
        ("fineliner", "black"), ("ballpoint", "grey"), ("marker", "black")]
    assert set(PEN_NAMES) == set(c)


def _opened(orientation):
    data = open(FIXTURE, "rb").read()
    return OpenedPage("nb", "rmdesk test", "pg", 0, 1, orientation, data)


def test_marks_portrait_five_crosses_black():
    lines = build_marks("portrait")
    assert len(MARK_POINTS) == 5
    assert len(lines) == 10
    assert all(l.color.name == "BLACK" for l in lines)
    for l in lines[-2:]:
        assert len(l.points) == 2
    mid = (sum(p.x for p in lines[-2].points) / 2, sum(p.y for p in lines[-2].points) / 2)
    assert mid == (0.0, 936.0)


def _pts(line):
    return [(p.x, p.y) for p in line.points]


def test_marks_landscape_three_candidates_with_l():
    lines = build_marks("landscape")
    assert len(lines) == 3 * 12  # 5 crosses (10 strokes) + an L (2 strokes) each
    groups = [lines[i:i + 12] for i in range(0, 36, 12)]
    tools = [{(l.tool.name, l.color.name) for l in g} for g in groups]
    assert tools == [{("FINELINER_2", "BLACK")}, {("BALLPOINT_2", "GRAY")},
                     {("MARKER_2", "BLACK")}]
    for name, g in zip(LANDSCAPE_CANDIDATES, groups):
        f, (x0, y0, x1, y1), _pen = LANDSCAPE_CANDIDATES[name]
        for l in g:
            assert all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in _pts(l))
        down, right = g[-2], g[-1]
        assert _pts(down) == [f(200, 50), f(200, 200)]
        assert _pts(right) == [f(200, 200), f(280, 200)]
        centre_h = g[8]  # the fifth cross is the centre one
        mid = tuple(sum(c) / 2 for c in zip(*_pts(centre_h)))
        assert _r(mid) == ((x0 + x1) / 2, (y0 + y1) / 2)


def test_marks_marker_is_thicker():
    lines = build_marks("landscape")
    assert lines[24].points[0].width > lines[0].points[0].width


def test_marks_are_clipped_to_the_candidate_rectangle():
    f, rect, pen = LANDSCAPE_CANDIDATES["rot90"]
    # a stroke from frame (-100, 100) to (100, 100) leaves rot90's page at page y = 0
    (line,) = cal._lines(f, rect, pen, [[(-100, 100), (100, 100)]])
    assert _pts(line) == [(836.0, 0.0), (836.0, 100.0)]
    assert cal._lines(f, rect, pen, [[(-50, 10), (-10, 10)]]) == []


def test_marks_unknown_orientation_raises():
    with pytest.raises(ValueError):
        build_marks("square")


@pytest.mark.parametrize("orientation, n", [("portrait", 10), ("landscape", 36)])
def test_build_page_verifies_for_both_orientations(orientation, n):
    op = _opened(orientation)
    new, got = build_page(op)
    assert got == n
    pagemod.verify(op.data, new, n, None)


def test_build_page_appends_verifiable_layer():
    op = _opened("portrait")
    new, n = build_page(op)
    assert n == 10
    pagemod.verify(op.data, new, n, None)
    assert pagemod.load(new) is not None


import io
import json

from rmdesk.compose.calibration import NotCalibrationNotebook, confirm, guard


class _In(io.StringIO):
    pass


def _ask(text, ready=True):
    out = []
    r = confirm(_opened("portrait"), stdin=_In(text), out=out.append,
                select_fn=lambda *a: ([a[0][0]] if ready else [], [], []))
    return r, out


def test_confirm_yes_and_no():
    assert _ask("y\n")[0] is True
    assert _ask("YES\n")[0] is True
    assert _ask("\n")[0] is False
    assert _ask("n\n")[0] is False
    assert _ask("")[0] is False  # EOF


def test_confirm_timeout_is_no_and_warns():
    r, out = _ask("y\n", ready=False)
    assert r is False
    assert "paused" in out[0] and "60" in out[0]
    assert "page 1 of 1 (portrait)" in out[1]


def _backup(tmp_path, meta):
    if meta is not None:
        (tmp_path / "nb.metadata").write_text(meta)
    return tmp_path


@pytest.mark.parametrize("meta", [
    json.dumps({"type": "DocumentType", "visibleName": "rmdesk test"}),
    json.dumps({"type": "DocumentType", "visibleName": " rmdesk test "}),
])
def test_guard_accepts_test_document(tmp_path, meta):
    guard(_backup(tmp_path, meta), _opened("portrait"))


@pytest.mark.parametrize("meta", [
    json.dumps({"type": "DocumentType", "visibleName": "Other"}),
    json.dumps({"type": "CollectionType", "visibleName": "rmdesk test"}),
    json.dumps({"visibleName": "rmdesk test"}),
    "not json",
    None,
])
def test_guard_refuses_everything_else(tmp_path, meta):
    with pytest.raises(NotCalibrationNotebook):
        guard(_backup(tmp_path, meta), _opened("portrait"))
