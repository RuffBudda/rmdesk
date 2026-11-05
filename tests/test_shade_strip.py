from pathlib import Path

import pytest

from rmdesk.compose import shade, shade_strip
from rmdesk.notebook import page
from rmdesk.notebook.opened import OpenedPage

RM = Path(__file__).parent / "fixtures" / "rm"
TOOL_NAMES = {"fineliner": "FINELINER_2", "ballpoint": "BALLPOINT_2", "pencil": "PENCIL_2"}


def _opened(orientation="portrait"):
    return OpenedPage("nb", "rmdesk test", "pg", 0, 1, orientation,
                      (RM / "Normal_AB.rm").read_bytes())


def test_blocks_stacked_centred_with_gaps():
    boxes = shade_strip.block_boxes()
    assert [p for p, _ in boxes] == ["fineliner", "ballpoint", "pencil"]
    for _, b in boxes:
        assert (b.w, b.h) == (600, 120)
        assert b.x == (1404 - 600) / 2
    for (_, a), (_, b) in zip(boxes, boxes[1:]):
        assert b.y - shade_strip.LABEL_H - (a.y + a.h) == 40  # label row sits in the gap's end
    top, bottom = boxes[0][1].y - shade_strip.LABEL_H, boxes[-1][1].y + 120
    assert top + (1872 - bottom) == pytest.approx(2 * top)  # vertically centred


def test_strip_has_three_pens_and_labels():
    lines = shade_strip.build_strip()
    tools = {ln.tool.name for ln in lines}
    assert {"FINELINER_2", "BALLPOINT_2", "PENCIL_2"} <= tools
    # labels sit above each block, left-aligned with it
    for pen, box in shade_strip.block_boxes():
        label = [ln for ln in lines if ln.tool.name == "FINELINER_2"
                 and all(box.y - shade_strip.LABEL_H <= p.y <= box.y for p in ln.points)
                 and min(p.y for p in ln.points) < box.y]
        assert label
        xs = [p.x for ln in label for p in ln.points]
        assert min(xs) >= box.x + page.X_MIN - 1
        assert min(xs) <= box.x + page.X_MIN + 5
    # every line lies inside a block or its label row; each pen shades its own block
    boxes = shade_strip.block_boxes()
    for ln in lines:
        ys = [p.y for p in ln.points]
        assert any(b.y - shade_strip.LABEL_H - 1e-6 <= min(ys) and max(ys) <= b.y + b.h + 1e-6
                   for _, b in boxes)
    for pen, box in boxes:
        body = [ln for ln in lines if ln.tool.name == TOOL_NAMES[pen]
                and box.y - 1e-6 <= min(p.y for p in ln.points)
                and max(p.y for p in ln.points) <= box.y + box.h + 1e-6]
        assert body
        for ln in body:
            assert all(box.x + page.X_MIN - 1e-6 <= p.x <= box.x + box.w + page.X_MIN + 1e-6
                       for p in ln.points)


def test_lines_valid_and_point_budget():
    lines = shade_strip.build_strip()
    for ln in lines:
        page._check_line(ln)
    assert sum(len(ln.points) for ln in lines) < shade.SHADE_MAX_POINTS // 2


def test_build_page_verifies():
    orig = _opened().data
    new, n = shade_strip.build_page(_opened())
    assert n > 0
    page.verify(orig, new, n, None)


def test_landscape_refused():
    with pytest.raises(shade_strip.LandscapePage, match="landscape"):
        shade_strip.build_page(_opened("landscape"))


def test_confirm_prompt_names_the_action():
    import io

    from rmdesk.compose.calibration import confirm
    out = []
    confirm(_opened(), stdin=io.StringIO("n\n"), out=out.append,
            select_fn=lambda r, w, x, t: (r, [], []), what="the pen test strip")
    assert any("Add the pen test strip to" in o for o in out)
    out.clear()
    confirm(_opened(), stdin=io.StringIO("n\n"), out=out.append,
            select_fn=lambda r, w, x, t: (r, [], []))
    assert any("Add calibration marks to" in o for o in out)
