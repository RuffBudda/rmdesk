"""Placement references found from the live frame in the keyboard layout: the page's absolute
strokes (scroll) and each text anchor's handwriting groups (frame-x constant C)."""
import io
import os
from pathlib import Path

import numpy as np
import pytest
import rmscene
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock, SceneGroupItemBlock, SceneLineItemBlock, SceneTreeBlock, TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from rmdesk.compose import scroll_fit
from rmdesk.compose.scroll_fit import (
    choose_reference, fit_references, reference_points,
)

H, W = 1872, 1404
A1, A2, A3 = CrdtId(1, 900), CrdtId(1, 901), CrdtId(1, 902)
TOP = CrdtId(0, 281474976710654)


def scribble(seed, n=12, x0=-600.0, x1=600.0, y0=-150.0, y1=150.0, steps=(8, 30)):
    rng = np.random.default_rng(seed)
    lines = []
    for _ in range(n):
        x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
        pts = [(x, y)]
        for _ in range(rng.integers(*steps)):
            x = float(np.clip(x + rng.normal(0, 6), x0, x1))
            y = float(np.clip(y + rng.normal(0, 6), y0, y1))
            pts.append((x, y))
        lines.append(pts)
    return lines


def cross(cx, cy, r=20.0):
    return [[(cx - r, cy), (cx + r, cy)], [(cx, cy - r), (cx, cy + r)]]


def render(groups, img=None):
    """Draw groups of (absolute page x, local y) polylines at their frame-x constant C:
    fx = C - local_y, fy = x + 936 (2 px ink)."""
    if img is None:
        img = np.full((H, W), 255, np.uint8)
    for const, lines in groups:
        for pl in lines:
            for (x0, y0), (x1, y1) in zip(pl, pl[1:]):
                for t in np.linspace(0, 1, 24):
                    px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                    fy, fx = int(round(px + H / 2)), int(round(const - py))
                    if 1 <= fy < H and 1 <= fx < W:
                        img[fy - 1:fy + 1, fx - 1:fx + 1] = 0
    return img


def _line(pts):
    return si.Line(color=si.PenColor.BLACK, tool=si.Pen.FINELINER_2,
                   points=[si.Point(float(x), float(y), 0, 0, 16, 220) for x, y in pts],
                   thickness_scale=1.0, starting_length=0.0)


class PageBuilder:
    """A minimal v6 page: Layer 1 with absolute lines and anchored sub-groups."""

    def __init__(self):
        self._n = 20
        self.layer = CrdtId(0, 11)
        self.trees = [SceneTreeBlock(self.layer, CrdtId(0, 0), True, CrdtId(0, 1))]
        self.nodes = [
            TreeNodeBlock(si.Group(CrdtId(0, 1), label=LwwValue(CrdtId(0, 0), ""),
                                   visible=LwwValue(CrdtId(0, 0), True))),
            TreeNodeBlock(si.Group(self.layer, label=LwwValue(CrdtId(0, 12), "Layer 1"),
                                   visible=LwwValue(CrdtId(0, 0), True))),
        ]
        self.gitems = [SceneGroupItemBlock(CrdtId(0, 1), CrdtSequenceItem(
            CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, self.layer))]
        self.lines = []
        self._tail = {self.layer: CrdtId(0, 0)}

    def _id(self):
        self._n += 1
        return CrdtId(1, self._n)

    def absolute(self, lines):
        for pl in lines:
            self._add(self.layer, pl)
        return self

    def anchored(self, anchor, lines, origin_x=-464.0, visible=True):
        g = self._id()
        item = self._id()
        self.trees.append(SceneTreeBlock(g, CrdtId(0, 0), True, self.layer))
        self.nodes.append(TreeNodeBlock(si.Group(
            g, label=LwwValue(CrdtId(0, 0), ""), visible=LwwValue(CrdtId(0, 0), visible),
            anchor_id=LwwValue(self._id(), anchor), anchor_type=LwwValue(self._id(), 2),
            anchor_threshold=LwwValue(self._id(), 35.74803161621094),
            anchor_origin_x=LwwValue(g, origin_x))))
        self.gitems.append(SceneGroupItemBlock(self.layer, CrdtSequenceItem(
            item, self._tail[self.layer], CrdtId(0, 0), 0, g)))
        self._tail[self.layer] = item
        self._tail[g] = CrdtId(0, 0)
        for pl in lines:  # local coordinates: x - origin_x
            self._add(g, [(x - origin_x, y) for x, y in pl])
        return self

    def _add(self, parent, pl):
        iid = self._id()
        self.lines.append(SceneLineItemBlock(parent, CrdtSequenceItem(
            iid, self._tail[parent], CrdtId(0, 0), 0, _line(pl))))
        self._tail[parent] = iid

    def data(self):
        import uuid
        buf = io.BytesIO()
        write_blocks(buf, [AuthorIdsBlock({1: uuid.UUID(int=1)}), *self.trees, *self.nodes,
                           *self.gitems, *self.lines])
        return buf.getvalue()


def test_reference_points_split_absolute_and_anchored():
    logo = scribble(1, n=3)
    page = PageBuilder().absolute(cross(0, 500)).anchored(A1, logo).anchored(
        A1, cross(300, 0), origin_x=-200.0).anchored(A2, cross(-300, 10)).data()
    refs = reference_points(page)
    assert set(refs) == {None, A1, A2}
    assert len(refs[None]) == 4
    # anchored points: x + anchor_origin_x (absolute page x), y local; groups sharing an
    # anchor are combined, each with its own origin
    got = refs[A1]
    want = np.array([p for pl in logo + cross(300, 0) for p in pl])
    assert np.allclose(got, want, atol=1e-3)
    assert np.allclose(refs[A2], [p for pl in cross(-300, 10) for p in pl], atol=1e-3)


def test_reference_points_skip_hidden_groups_and_bad_pages():
    page = PageBuilder().anchored(A1, cross(0, 0), visible=False).data()
    assert A1 not in reference_points(page)
    assert reference_points(b"not a page") == {}


def test_each_anchor_found_at_its_own_constant():
    """Two anchors at known C (and no absolute strokes): each fit finds its own C."""
    g1, g2 = scribble(2, n=14), scribble(3, n=14, y0=-80, y1=80)
    picture = render([(820, g1), (300, g2)])
    page = PageBuilder().anchored(A1, g1).anchored(A2, g2).data()
    fits = fit_references(picture, page)
    assert abs(fits[A1]["const"] - 820) <= 2
    assert abs(fits[A2]["const"] - 300) <= 2
    assert fits[None]["const"] is None  # nothing unanchored to line up
    assert fits[A1]["best_hit"] >= scroll_fit.MIN_HIT


def test_ambiguous_group_is_not_confident():
    """Identical marks repeated along frame x with nothing else: refuse that anchor only."""
    row = [seg for k in range(4) for seg in cross(0, k * 250.0)]
    g1 = scribble(4, n=14)
    picture = render([(900, g1), (1300, row)])
    page = PageBuilder().anchored(A1, g1).anchored(A3, cross(0, 0)).data()
    fits = fit_references(picture, page)
    assert fits[A3]["const"] is None and fits[A3]["reason"]
    assert abs(fits[A1]["const"] - 900) <= 2


def test_absolute_strokes_still_give_the_scroll():
    """Unanchored lines: C = 1404 + scroll, local y = absolute y."""
    lines = scribble(5, n=25, y0=20, y1=2400)
    picture = render([(W + 678, lines)])
    fits = fit_references(picture, PageBuilder().absolute(lines).data())
    assert fits[None]["scroll"] is not None and abs(fits[None]["scroll"] - 678) <= 2
    assert abs(fits[None]["const"] - (W + 678)) <= 2


def test_misplaced_anchor_text_line_does_not_spoil_the_fit():
    """The device case: the anchors' text lines sit where rmc doesn't expect them; each group
    is fitted on its own, so every C is still found."""
    groups = [(A1, scribble(6, n=12, x0=-600, x1=-100), 1150),
              (A2, scribble(7, n=12, x0=-50, x1=300), 640),
              (A3, scribble(8, n=12, x0=350, x1=650), 210)]
    picture = render([(c, g) for _, g, c in groups])
    b = PageBuilder()
    for a, g, _ in groups:
        b.anchored(a, g)
    fits = fit_references(picture, b.data())
    for a, _, c in groups:
        assert fits[a]["const"] is not None and abs(fits[a]["const"] - c) <= 2


def _fit(const, lo, hi, hit=0.95):
    return {"const": const, "fx": (lo, hi) if const is not None else None, "best_hit": hit}


def test_choose_prefers_the_absolute_fit():
    fits = {None: _fit(1500, 0, 1404), A1: _fit(600, 500, 700)}
    assert choose_reference(fits, (550, 650)) == (None, fits[None])


def test_choose_nearest_confident_anchor_in_frame_x():
    fits = {None: _fit(None, 0, 0), A1: _fit(1100, 1000, 1200), A2: _fit(400, 300, 500),
            A3: _fit(None, 0, 0)}
    assert choose_reference(fits, (550, 650))[0] == A2
    assert choose_reference(fits, (900, 950))[0] == A1
    assert choose_reference(fits, (1100, 1150))[0] == A1  # overlapping wins
    # an unconfident anchor is never chosen, however near
    fits[A3] = {"const": None, "fx": None}
    assert choose_reference(fits, (0, 10))[0] == A2


def test_choose_nothing_confident():
    assert choose_reference({None: _fit(None, 0, 0), A1: _fit(None, 0, 0)}, (0, 10)) is None
    assert choose_reference({}, (0, 10)) is None


# -- the device's debug capture (local only: it holds the user's page) ----------------

DEBUG = Path(os.environ.get(
    "RMDESK_FIT_DEBUG",
    Path.home() / ".local/share/rmdesk/debug/fit-20261104-144814"))


@pytest.mark.skipif(not DEBUG.with_suffix(".rm").exists(), reason="no local debug capture")
def test_real_debug_capture_finds_the_logo_anchor():
    import cv2
    gray = cv2.imread(str(DEBUG.with_suffix(".png")), cv2.IMREAD_GRAYSCALE)
    data = DEBUG.with_suffix(".rm").read_bytes()
    fits = fit_references(gray, data)
    assert fits[None]["const"] is None  # every stroke on this page is text-anchored
    confident = {k: f for k, f in fits.items() if k is not None and f["const"] is not None}
    logo = CrdtId(1, 1247)
    assert logo in confident and abs(confident[logo]["const"] - 517) <= 3
    assert confident[logo]["best_hit"] - confident[logo]["next_hit"] >= scroll_fit.MARGIN
    # items placed over the picture's middle are anchored to the logo's text
    assert choose_reference(fits, (400, 600))[0] == logo


@pytest.mark.skipif(not DEBUG.with_suffix(".rm").exists(), reason="no local debug capture")
def test_real_debug_capture_builds_a_verified_anchored_group():
    import cv2
    from rmdesk.compose import build
    from rmdesk.compose.frame import KEYBOARD, Anchored
    from rmdesk.compose.items import Box, InkTextItem, PenStyle
    from rmdesk.notebook import page
    gray = cv2.imread(str(DEBUG.with_suffix(".png")), cv2.IMREAD_GRAYSCALE)
    data = DEBUG.with_suffix(".rm").read_bytes()
    key, fit = choose_reference(fit_references(gray, data), (400, 600))
    p = page.load(data)
    tmpl = p.anchor_template(key)
    item = InkTextItem(text="x", box=Box(400, 900, 200, 200), size=10, pen=PenStyle())
    lines = build.frame_to_lines(item, [[(10, 10), (190, 190)], [(10, 190), (190, 10)]], None,
                                 "portrait", layout=KEYBOARD,
                                 anchored=Anchored(fit["const"], tmpl.anchor_origin_x.value))
    p.append_anchored_group(lines, key)
    new = p.to_bytes()
    page.verify(data, new, len(lines), None)
    # frame (410, 910) -> local (910 - 936 + 464, C - 410)
    first = lines[0].points[0]
    assert (first.x, first.y) == (910 - 936 + 464, fit["const"] - 410)


def test_text_pseudo_anchors_are_never_chosen():
    """A new group on the top/bottom-of-text pseudo-anchors is kept but not drawn by xochitl
    (fw 3.29.1.0): never place by them; a real text-character anchor still works."""
    from rmscene.tagged_block_common import CrdtId
    from rmdesk.compose.scroll_fit import choose_reference, is_special_anchor
    bottom, top, char = CrdtId(0, 2**48 - 1), CrdtId(0, 2**48 - 2), CrdtId(1, 57)
    assert is_special_anchor(bottom) and is_special_anchor(top) and not is_special_anchor(char)
    good = {"const": 2123.0, "fx": (827, 1403), "best_hit": 0.95}
    assert choose_reference({bottom: good, top: good}, (0, 800)) is None
    assert choose_reference({bottom: good, char: dict(good, fx=(100, 200))}, (0, 800))[0] == char
