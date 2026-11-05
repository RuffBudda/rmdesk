"""Raster picture -> clean line-art pen strokes (pure, Qt-free).

For diagrams, logos, charts and screenshots of drawings: the picture's lines
are drawn once each, as even pen strokes along their centre, instead
of the engraving-style shading of `shade`.

Pipeline (the same fit, centre, visible-region crop, alpha over white and invert as
`shade.shade_image`), on a raster SUPERSAMPLE x the box resolution (bounded by
RASTER_MAX_PX) so thin anti-aliased text thins into clean centre lines:

1. Ink: darkness above Otsu's level for the whole picture, times `threshold`
   (1.0 = Otsu; lower picks up fainter lines, higher keeps only the darkest),
   plus faint text (light grey labels) found with a local rule (`ink_mask`).
   Specks smaller than a Detail-based area are dropped.
2. Solid parts: ink where a disc THICK_FACTOR pen widths across fits (a
   morphological opening, from the distance transform) is a filled region. It
   would thin into a messy skeleton, so it is drawn as its outline instead:
   cv2.findContours on the region shrunk by half a pen width, so the pen's outer
   edge lands on the shape's edge.
3. Everything else is thinned to 1 px centre lines (Guo-Hall thinning:
   cv2.ximgproc.thinning when opencv-contrib is there, else the numpy version
   here; not Zhang-Suen, which erases diagonal strokes of even width), walked into
   pixel chains split at junctions and line ends, thinning spurs pruned (no longer
   than the stroke's half width), and the chains joined across a junction where
   they run on nearly straight (a cross is two strokes, not four).
4. Text: a 4 px pen fills in a letter 6 px tall, so marks up to TEXT_MAX_PX
   (letters, digits, small symbols) are drawn with a pen as wide as their own
   strokes: bold letters as centre lines at their stroke width, thin-stroked ones
   as their outlines with a 1 px pen (which keep every feature of a small letter).
   Every mark is kept whole, however small; dots and full stops stay dots.
5. Douglas-Peucker (cv2.approxPolyDP) with a Detail-based tolerance, finer for
   small marks.

Sharp text (optional, `sharptext`): before steps 2-4, the words Tesseract recognises in the
source picture are erased from the ink (their glyph pixels only, never a box border or
arrow running through) and drawn again as Hershey sans letters fitted into each word's
box, with a fine pen; a 6 px label then reads as letters instead of a blur.

Output is a `shade.ShadeResult` whose points all carry the same pressure and the
pen's width (finer for text; constant along each stroke), so `build.shade_to_lines`
/ `build.frame_shade_to_lines` place, rotate, clip and map them exactly as they do
shaded strokes. Points stay under
shade.SHADE_MAX_POINTS: the tolerance is raised until they fit, and only if
that is not enough is the rest dropped (`capped`).
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .items import ImageItem
from .shade import (D_MIN, RASTER_MAX_PX, SHADE_MAX_POINTS, WIDTH_UNITS_PER_PX, ShadeResult,
                    Stroke, _darkness, _View)

DETAIL_MIN, DETAIL_MAX, DETAIL_DEFAULT = 1, 5, 3
THRESHOLD_MIN, THRESHOLD_MAX = 0.5, 2.0
# The pen line-art pictures start with (clean, even strokes; shading keeps the pencil).
DEFAULT_LINEART_PEN = "fineliner"
# Per Detail level 1..5: Douglas-Peucker tolerance (px), shortest piece kept (px, along
# the line) and smallest ink speck kept (px area).
_EPS = {1: 2.0, 2: 1.5, 3: 1.0, 4: 0.75, 5: 0.5}
_MIN_LEN = {1: 16.0, 2: 10.0, 3: 6.0, 4: 4.0, 5: 2.0}
_SPECK = {1: 24, 2: 12, 3: 0.75, 4: 0.5, 5: 0.25}
CORNER_K = 0.45  # ink this close (x the disc radius) to a solid region belongs to it
DOT_LEN = 1.0  # a dot is a stroke this long (px); the pen's width makes it round
DOTS_FROM = 3  # Detail from which marks too small to be lines are kept as dots
THICK_FACTOR = 4  # ink wider than this many pen widths is a filled region: outlined
OTSU_SIDE = 512  # Otsu's level is taken over the whole picture at most this long side
CUT_MIN, CUT_MAX = 0.12, 0.95  # the ink level stays inside this darkness range
JOIN_DOT = -0.7  # join two branches across a junction when they turn less than ~45 degrees
DIR_PX = 8  # a branch's direction at a junction: towards its point this far along
EPS_GROWTH = 1.6  # over the point budget: the tolerance grows by this, up to EPS_TRIES times
EPS_TRIES = 10
# Supersampling: the picture is thresholded and thinned at up to this many raster pixels
# per box pixel (bounded by RASTER_MAX_PX), so thin anti-aliased text strokes thin into
# clean centre lines; the chains are scaled back to box px.
SUPERSAMPLE = 4.0
# Faint text (see ink_mask): box px windows for the local peak and paper, the smallest
# local contrast taken as ink (x threshold^2: at 1.5 mid-grey is dropped again), and the
# largest faint mark added (a glyph; bigger faint marks are fills or grid lines).
FAINT_PEAK_PX = 1.5
FAINT_BG_PX = 6.0
FAINT_FLOOR = 0.2
FAINT_MAX_PX = 40.0
SPUR_K = 1.0  # a spur: a line end this many stroke half widths or less from a junction
SPUR_MIN_PX = 1.0  # ...or this many box px
LOOP_K = 3.0  # a loop at a junction shorter than this many half widths is noise
EPS_SIZE = 24.0  # marks smaller than this (box px) get a proportionally finer tolerance
EPS_SIZE_MIN = 0.2  # ...down to this fraction of it
TEXT_MAX_PX = 40.0  # marks up to this long (box px) are drawn with a pen fitted to their strokes
TEXT_PEN_MIN_PX = 1.5  # ...never finer than this (box px)
OUTLINE_STROKE_MAX_PX = 2.0  # ...and if their strokes are no wider (box px), as outlines
OUTLINE_PEN_PX = 1.0  # drawn with this pen (box px)
DOT_ROUND = 1.5  # a dot: a mark no longer than this many of its own stroke widths
SLIVER_PX = 0.5  # pieces shorter than this (box px) are not drawn (the pen covers them)
_N8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def lineart_item(item: ImageItem, detail: int = DETAIL_DEFAULT, threshold: float = 1.0,
                 invert: bool = False, sharp_text: bool = False) -> ShadeResult:
    """`lineart_image` for an item: its picture, box, page position, rotation and pen."""
    from .build import PEN_WIDTH, PRESSURE  # build imports shade; no cycle with this module
    width = PEN_WIDTH[item.pen.tool]
    return lineart_image(item.path, int(round(item.box.w)), int(round(item.box.h)), detail,
                         threshold, box_x=item.box.x, box_y=item.box.y, rotation=item.rotation,
                         invert=invert, width=width, pressure=PRESSURE,
                         line_px=width * item.pen.thickness / WIDTH_UNITS_PER_PX,
                         sharp_text=sharp_text)


def lineart_image(path: Path, box_w: int, box_h: int, detail: int = DETAIL_DEFAULT,
                  threshold: float = 1.0, *, box_x: float = 0.0, box_y: float = 0.0,
                  rotation: float = 0.0, invert: bool = False, width: float = 16.0,
                  pressure: float = 220.0, line_px: float = 4.0,
                  sharp_text: bool = False) -> ShadeResult:
    """Line art of the picture at `path` in a box_w x box_h box.

    Box position/rotation as `shade.shade_image` (only what lands on the page is
    drawn). Every point is (x, y, width, `pressure`): `width` in tablet units
    before the pen thickness (build multiplies that in), `line_px` the drawn pen
    width in page px (thickness applied) that decides which ink counts as a filled
    region. Text-sized marks get a finer width, `width` x their pen / `line_px`
    (constant along each stroke), so they are drawn at their own stroke width. Raises ValueError if the file cannot be read; a blank picture gives
    no strokes. `spacing_used` is 0 (there are no shading lines).

    `sharp_text`: words Tesseract recognises in the picture (`sharptext`, cached per
    picture) are not thinned into lines: their glyphs are erased from the raster and each word is drawn
    in the Hershey sans face fitted into its own box, with a pen fitted to its height.
    Words taller than sharptext.SHARP_MAX_PX once fitted are drawn as line art as before.
    """
    detail = int(min(max(int(detail), DETAIL_MIN), DETAIL_MAX))
    threshold = float(min(max(threshold, THRESHOLD_MIN), THRESHOLD_MAX))
    size = (int(box_w), int(box_h))
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError(f"cannot read image: {path}")
    empty = ShadeResult([], size, False, 0)
    if box_w < 1 or box_h < 1:
        return empty

    h, w = raw.shape[:2]
    scale = min(box_w / w, box_h / h)
    tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    ox, oy = (box_w - tw) / 2, (box_h - th) / 2
    view = _View(box_w, box_h, box_x, box_y, rotation)
    vx0, vy0, vx1, vy1 = view.visible_box()
    fx0, fx1 = max(0.0, vx0 - ox), min(float(tw), vx1 - ox)
    fy0, fy1 = max(0.0, vy0 - oy), min(float(th), vy1 - oy)
    if fx1 <= fx0 or fy1 <= fy0:
        return empty
    line_px = max(float(line_px), 1.0)
    pad = int(math.ceil(THICK_FACTOR * line_px)) + 4  # solid parts are judged with context
    crop = (max(0, int(math.floor(fx0)) - pad), max(0, int(math.floor(fy0)) - pad),
            min(tw, int(math.ceil(fx1)) + pad), min(th, int(math.ceil(fy1)) + pad))
    area = (crop[2] - crop[0]) * (crop[3] - crop[1])
    r = min(SUPERSAMPLE, math.sqrt(RASTER_MAX_PX / area))

    level = ink_level(raw, tw, th, invert)
    if level is None:
        return empty
    cut = min(max(level * threshold, CUT_MIN), CUT_MAX)
    d = _darkness(raw, tw, th, crop, r, 1.0, invert)
    ink = ink_mask(d, cut, threshold, r)
    if not ink.any():
        return empty
    ink = _drop_specks(ink, _SPECK[detail] * r * r)
    text: list[tuple[np.ndarray, float]] = []
    if sharp_text:
        text = _sharp_text(path, raw, invert, tw / w, th / h, ox, oy, crop, r, ink, line_px,
                           view)

    # raster pixel (u, v) centre -> box pixels
    def to_box(pts: np.ndarray) -> np.ndarray:
        out = np.empty_like(pts, dtype=np.float64)
        out[:, 0] = ox + crop[0] + (pts[:, 0] + 0.5) / r
        out[:, 1] = oy + crop[1] + (pts[:, 1] + 0.5) / r
        return out

    chains = [(to_box(c), closed, size / r, pen / r)
              for c, closed, size, pen in centre_lines(ink, line_px * r, detail, r)]
    eps = _EPS[detail]  # box px, like the chains
    for _ in range(EPS_TRIES):
        strokes = _simplified(chains, eps, view, text)
        total = sum(len(s) for s, _pen in strokes)
        if total <= SHADE_MAX_POINTS:
            break
        eps *= EPS_GROWTH
    out: list[Stroke] = []
    count, capped = 0, False
    for s, pen in strokes:
        if count + len(s) > SHADE_MAX_POINTS:
            capped = True
            break
        w = float(width) if pen >= line_px else float(width) * pen / line_px
        out.append([(float(x), float(y), w, float(pressure)) for x, y in s])
        count += len(s)
    return ShadeResult(out, size, capped and bool(out), 0)


def _sharp_text(path: Path, raw: np.ndarray, invert: bool, kx: float, ky: float, ox: float,
                oy: float, crop, r: float, ink: np.ndarray, line_px: float,
                view: _View) -> list[tuple[np.ndarray, float]]:
    """The recognised words that land in the crop, erased from `ink` (in place) and drawn:
    (polyline in box px, pen in box px) pieces on the page. `kx`, `ky`: fitted px per
    source px."""
    from . import sharptext
    rects, out = [], []
    cx0, cy0, cx1, cy1 = (ox + crop[0], oy + crop[1], ox + crop[2], oy + crop[3])
    for wd in sharptext.recognise(path, raw, invert):
        x0, y0 = ox + wd.x * kx, oy + wd.y * ky
        x1, y1 = x0 + wd.w * kx, y0 + wd.h * ky
        if y1 - y0 > sharptext.SHARP_MAX_PX or x1 <= cx0 or x0 >= cx1 or y1 <= cy0 \
                or y0 >= cy1:
            continue
        pen = sharptext.word_pen(y1 - y0, OUTLINE_PEN_PX, line_px)
        lines = sharptext.word_lines(wd.text, x0, y0, x1, y1, pen)
        if not lines:
            continue
        rects.append(((x0 - cx0) * r, (y0 - cy0) * r, (x1 - cx0) * r, (y1 - cy0) * r))
        for p in lines:
            _on_page(p, pen, view, out)
    sharptext.erase_words(ink, rects, r)
    return out


def ink_level(raw: np.ndarray, tw: int, th: int, invert: bool = False) -> float | None:
    """Otsu's darkness level (0..1) over the whole fitted picture, or None if it is blank.

    Taken over the whole picture (at most OTSU_SIDE px) rather than the visible
    crop, so moving the picture never changes which lines count as ink."""
    k = min(1.0, OTSU_SIDE / max(tw, th))
    sw, sh = max(1, int(round(tw * k))), max(1, int(round(th * k)))
    d = _darkness(raw, sw, sh, (0, 0, sw, sh), 1.0, 1.0, invert)
    if float(d.max()) <= D_MIN:
        return None
    level, _ = cv2.threshold((d * 255.0).astype(np.uint8), 0, 255,
                             cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return float(level) / 255.0


def ink_mask(d: np.ndarray, cut: float, threshold: float = 1.0, r: float = 1.0) -> np.ndarray:
    """Which raster pixels are ink: darker than the global `cut`, plus faint text.

    Faint text (light grey labels below Otsu's level) is found with a local rule:
    a pixel is ink if it is darker than half way from the local paper (the
    lightest level within FAINT_BG_PX) to the local peak (the darkest within
    FAINT_PEAK_PX), where that contrast is at least FAINT_FLOOR x threshold^2. The
    peak window is small, so next to a black line the local cut is half of
    black and the line does not thicken. Only marks the global cut missed and
    no longer than FAINT_MAX_PX either way (glyphs, not grey fills or grid lines)
    are added. `r`: raster pixels per box pixel."""
    ink = d > cut
    floor = FAINT_FLOOR * threshold * threshold
    if float(d.max()) < floor:
        return ink

    def k(px: float, shape: int) -> np.ndarray:
        n = 2 * max(1, int(round(px * r))) + 1
        return cv2.getStructuringElement(shape, (n, n))

    peak = cv2.dilate(d, k(FAINT_PEAK_PX, cv2.MORPH_ELLIPSE))
    paper = cv2.erode(d, k(FAINT_BG_PX, cv2.MORPH_RECT))
    contrast = peak - paper
    faint = (contrast >= floor) & (d > paper + 0.5 * contrast) & ~ink
    if not faint.any():
        return ink
    n, labels, stats, _ = cv2.connectedComponentsWithStats((faint | ink).astype(np.uint8),
                                                           connectivity=8)
    side = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT])
    small = side <= FAINT_MAX_PX * r
    small[0] = False
    return ink | (faint & small[labels])


def _drop_specks(ink: np.ndarray, min_area: float) -> np.ndarray:
    if min_area <= 1:
        return ink
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    return keep[labels]


# -- solid regions and centre lines -------------------------------------------------------

def centre_lines(ink: np.ndarray, line_px: float, detail: int = DETAIL_DEFAULT,
                 r: float = 1.0) -> list[tuple[np.ndarray, bool, float, float]]:
    """Pixel chains (N x 2 float, x/y raster pixel indices): whether each is closed, the size
    (longest side) of the mark it belongs to and the pen width to draw it with (both
    raster px).

    Filled regions wider than THICK_FACTOR x line_px (raster px) become their
    outlines; the rest of the ink its 1 px centre lines, walked into chains. Every
    mark is kept whole, however small (a letter's short strokes are part of the
    letter), except that below Detail DOTS_FROM marks shorter than the Detail's
    shortest piece are dropped. A small round mark (a full stop, the dot of an i) is
    a dot. Marks up to TEXT_MAX_PX (letters, digits, small symbols) are drawn with a
    pen as wide as their own strokes (TEXT_PEN_MIN_PX..line_px): a 4 px pen fills in
    a letter 6 px tall. Bigger marks use line_px. `r`: raster pixels per box pixel
    (the Detail and text sizes are box px)."""
    radius = THICK_FACTOR * line_px / 2
    u8 = ink.astype(np.uint8)
    dist = cv2.distanceTransform(np.pad(u8, 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
    core = dist > radius
    chains: list[tuple[np.ndarray, bool, float, float]] = []
    thin = ink
    if core.any():
        k = int(math.ceil(radius))
        disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
        solid = (cv2.dilate(core.astype(np.uint8), disc) > 0) & ink
        # the opening rounds a solid's corners: the ink left there (all within
        # radius * (sqrt 2 - 1) of the solid) is part of the solid, not a line
        near = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, 5)
        solid = ink & (near <= CORNER_K * radius + 1)
        thin = ink & ~solid
        chains += [(p, closed, size, line_px)
                   for p, closed, size in _outlines(solid, line_px, _MIN_LEN[detail] * r)]
    if not thin.any():
        return chains
    thin = _drop_specks(thin, _SPECK[detail] * r * r)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(thin.astype(np.uint8),
                                                                connectivity=8)
    size = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]).astype(float)
    size[0] = np.inf  # (a point between a mark's pixels: as a big mark)
    skel = skeletonise(thin)
    ys, xs = np.nonzero(skel)
    lab = labels[ys, xs]
    # stroke width of each mark: twice the mean distance to the paper along its centre line
    # (the distance at a w px stroke's centre pixel is about (w + 1) / 2)
    on = np.bincount(lab, minlength=n).astype(float)
    mean_d = np.bincount(lab, weights=dist[ys, xs], minlength=n) / np.maximum(on, 1)
    thick = np.zeros(n)
    np.maximum.at(thick, labels[thin], dist[thin])
    stroke = np.maximum(2 * np.where(on > 0, mean_d, thick) - 1, 1.0)
    pen = np.where(size <= TEXT_MAX_PX * r, np.clip(stroke, TEXT_PEN_MIN_PX * r, line_px),
                   line_px)
    pen[0] = line_px
    # dots: round marks (no longer than DOT_ROUND stroke widths, or than the finest pen)
    if detail >= DOTS_FROM:
        drop = (size <= np.maximum(DOT_ROUND * (2 * thick - 1), TEXT_PEN_MIN_PX * r))
    else:
        drop = size <= _MIN_LEN[detail] * r
    drop[0] = False
    if drop.any():
        if detail >= DOTS_FROM:
            h = DOT_LEN * r / 2
            for i in np.nonzero(drop)[0].tolist():
                x, y = cents[i]
                chains.append((np.array([[x - h, y], [x + h, y]]), False, float(size[i]),
                               float(min(max(size[i], TEXT_PEN_MIN_PX * r), line_px))))
        skel = skel & ~drop[labels]
    # thin-stroked small marks (regular-weight text): their outlines with a fine pen
    fine = (size <= TEXT_MAX_PX * r) & (stroke <= OUTLINE_STROKE_MAX_PX * r) & ~drop
    fine[0] = False
    if fine.any():
        chains += _glyph_outlines(thin & fine[labels], skel & fine[labels], labels, size,
                                  min(OUTLINE_PEN_PX * r, line_px), r)
        skel = skel & ~fine[labels]
    for p, closed in _skeleton_chains(skel, detail, dist, r):
        q = np.clip(np.round(p[len(p) // 2]).astype(np.int64), 0,
                    [thin.shape[1] - 1, thin.shape[0] - 1])
        i = labels[q[1], q[0]]
        chains.append((p, closed, float(size[i]), float(pen[i])))
    return chains


def _glyph_outlines(mask: np.ndarray, skel: np.ndarray, labels: np.ndarray, size: np.ndarray,
                    pen: float, r: float) -> list[tuple[np.ndarray, bool, float, float]]:
    """Outlines (outer and holes) of small thin-stroked marks, to draw with a `pen` px pen
    (raster px). Two walls of a fine pen fill a stroke up to about twice its width, and
    the outline keeps every feature of a small letter (a "t"'s top, an "e"'s eye) that
    a centre line would lose. The outline is inset by half the pen, so the drawn walls
    cover the letter and no more; the mark's centre line (`skel`) is kept in the inset
    shape, so a part thinner than the pen is still drawn (as a single line) and never
    cut off."""
    u8 = mask.astype(np.uint8)
    k = int(round(pen / 2))
    if k >= 1:
        shrunk = cv2.erode(u8, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1,) * 2))
        u8 = ((shrunk > 0) | skel).astype(np.uint8)
    contours, _ = cv2.findContours(u8, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        p = c.reshape(-1, 2).astype(np.float64)
        sz = float(size[labels[int(p[0, 1]), int(p[0, 0])]])
        if len(p) < 3:  # a pixel or two: a dot
            x, y = p.mean(axis=0)
            h = DOT_LEN * r / 2
            out.append((np.array([[x - h, y], [x + h, y]]), False, sz, pen))
        else:
            out.append((p, True, sz, pen))
    return out


def _outlines(solid: np.ndarray, line_px: float,
              min_len: float = 0.0) -> list[tuple[np.ndarray, bool, float]]:
    """Closed outlines of the filled regions, shrunk by half a pen width (if they survive it);
    outlines shorter than `min_len` (raster px) are dropped."""
    u8 = solid.astype(np.uint8)
    k = int(round(line_px / 2))
    if k >= 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
        shrunk = cv2.erode(u8, kernel)
        if shrunk.any():
            u8 = shrunk
    contours, _ = cv2.findContours(u8, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        if len(c) < 3 or cv2.arcLength(c, True) < min_len:
            continue
        x, y, w, h = cv2.boundingRect(c)
        out.append((c.reshape(-1, 2).astype(np.float64), True, float(max(w, h))))
    return out


def skeletonise(mask: np.ndarray) -> np.ndarray:
    """1 px centre lines of a boolean mask (Guo-Hall thinning; cv2.ximgproc when there)."""
    ximgproc = getattr(cv2, "ximgproc", None)
    if ximgproc is not None and hasattr(ximgproc, "thinning"):
        out = ximgproc.thinning(mask.astype(np.uint8) * 255,
                                thinningType=ximgproc.THINNING_GUOHALL)
        return out > 0
    return guo_hall(mask)


def guo_hall(mask: np.ndarray) -> np.ndarray:
    """Guo-Hall thinning in numpy, the same rule as cv2.ximgproc's (the fallback without
    opencv-contrib). Not Zhang-Suen: that wipes out diagonal strokes of even width
    (the arm of a small "k" vanishes); Guo-Hall keeps them."""
    img = np.pad(mask.astype(bool), 1)
    while True:
        changed = False
        for step in (0, 1):
            p = img
            p2, p3, p4 = p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:]
            p5, p6, p7 = p[2:, 2:], p[2:, 1:-1], p[2:, :-2]
            p8, p9 = p[1:-1, :-2], p[:-2, :-2]
            u8 = lambda x: x.astype(np.uint8)  # noqa: E731
            c = (u8(~p2 & (p3 | p4)) + u8(~p4 & (p5 | p6)) + u8(~p6 & (p7 | p8))
                 + u8(~p8 & (p9 | p2)))
            n1 = u8(p9 | p2) + u8(p3 | p4) + u8(p5 | p6) + u8(p7 | p8)
            n2 = u8(p2 | p3) + u8(p4 | p5) + u8(p6 | p7) + u8(p8 | p9)
            n = np.minimum(n1, n2)
            m = ((p6 | p7 | ~p9) & p8) if step == 0 else ((p2 | p3 | ~p5) & p4)
            remove = p[1:-1, 1:-1] & (c == 1) & (n >= 2) & (n <= 3) & ~m
            if remove.any():
                img[1:-1, 1:-1] &= ~remove
                changed = True
        if not changed:
            return img[1:-1, 1:-1].copy()


def _unstair(skel: np.ndarray) -> np.ndarray:
    """Drop staircase corners: a pixel whose only neighbours are two 4-neighbours at a right
    angle (and the pixel between them) joins nothing they don't already join diagonally.
    Left in, every step of a diagonal line would look like a junction."""
    img = np.pad(skel.astype(bool), 1)
    for a, b in (((-1, 0), (0, 1)), ((0, 1), (1, 0)), ((1, 0), (0, -1)), ((0, -1), (-1, 0))):
        c = img[1:-1, 1:-1]
        nb = {o: img[1 + o[0]:img.shape[0] - 1 + o[0], 1 + o[1]:img.shape[1] - 1 + o[1]]
              for o in _N8}
        diag = (a[0] + b[0], a[1] + b[1])
        others = np.zeros_like(c)
        for o in _N8:
            if o not in (a, b, diag):
                others |= nb[o]
        img[1:-1, 1:-1] = c & ~(nb[a] & nb[b] & ~others)
    return img[1:-1, 1:-1]


def _skeleton_chains(skel: np.ndarray, detail: int, dist: np.ndarray | None = None,
                     r: float = 1.0) -> list[tuple[np.ndarray, bool]]:
    """Walk a 1 px skeleton into chains: branches between line ends and junctions, short
    spurs pruned, branches joined across junctions where they run on nearly straight.

    A spur is what thinning leaves at a thick line's corner: no longer than about the
    stroke's half width there (`dist`, the ink's distance transform), so that is what
    is pruned (SPUR_K half widths), never a fixed length that would eat a small
    letter's arms. `r`: raster pixels per box pixel."""
    if not skel.any():
        return []
    if dist is None:
        dist = cv2.distanceTransform(np.pad(skel.astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
    sk = np.pad(_unstair(skel).astype(np.uint8), 1)
    W = sk.shape[1]
    deg = cv2.filter2D(sk, -1, np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8),
                       borderType=cv2.BORDER_CONSTANT)
    node = (sk > 0) & (deg != 2)
    n_nodes, labels = cv2.connectedComponents(node.astype(np.uint8), connectivity=8)
    flat_sk = sk.ravel().astype(bool)
    flat_lab = labels.ravel()
    offs = [dy * W + dx for dy, dx in _N8]
    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    centre = np.zeros((n_nodes, 2))
    cnt = np.bincount(lab, minlength=n_nodes).astype(np.float64)
    centre[:, 0] = np.bincount(lab, weights=xs, minlength=n_nodes)
    centre[:, 1] = np.bincount(lab, weights=ys, minlength=n_nodes)
    centre[1:] /= np.maximum(cnt[1:, None], 1)
    members: dict[int, list[int]] = {}
    for y, x, l_ in zip(ys.tolist(), xs.tolist(), lab.tolist()):
        members.setdefault(l_, []).append(y * W + x)
    # stroke half width at each node (raster px): the widest point of the ink there
    half = np.zeros(n_nodes)
    np.maximum.at(half, lab, np.pad(dist, 1)[ys, xs])
    spur_at = np.maximum(SPUR_K * half, SPUR_MIN_PX * r)

    visited = np.zeros(flat_sk.size, bool)
    # edges: [pixel list (flat), start node, end node]; node 0 = none (a pure loop)
    edges: list[list] = []
    for l_, pix in members.items():
        for p in pix:
            for o in offs:
                q = p + o
                if not flat_sk[q] or flat_lab[q] == l_ or visited[q]:
                    continue
                if flat_lab[q]:  # two node clusters touching: cannot happen (8-connected)
                    continue
                path, prev, cur, end = [q], p, q, 0
                visited[q] = True
                while True:
                    nxt = [cur + o2 for o2 in offs if flat_sk[cur + o2] and cur + o2 != prev]
                    stop = [n for n in nxt if flat_lab[n]]
                    if stop:
                        end = int(flat_lab[stop[0]])
                        break
                    nxt = [n for n in nxt if not visited[n]]
                    if not nxt:
                        break
                    prev, cur = cur, nxt[0]
                    visited[cur] = True
                    path.append(cur)
                edges.append([path, l_, end])
    # pure loops (no line end or junction anywhere on them)
    rest = np.nonzero(flat_sk & ~visited & (flat_lab == 0))[0]
    for s in rest.tolist():
        if visited[s]:
            continue
        path, prev, cur = [s], -1, s
        visited[s] = True
        while True:
            nxt = [cur + o for o in offs if flat_sk[cur + o] and cur + o != prev
                   and not visited[cur + o]]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            visited[cur] = True
            path.append(cur)
        edges.append([path + [s], 0, 0])

    def pts(path: list[int]) -> np.ndarray:
        a = np.asarray(path, np.int64)
        return np.stack([(a % W) - 1, (a // W) - 1], axis=1).astype(np.float64)

    # each branch: its points with the node centres at its ends
    branches = []
    for path, a, b in edges:
        p = pts(path)
        if a:
            p = np.vstack([centre[a] - 1, p])
        if b:
            p = np.vstack([p, centre[b] - 1])
        branches.append([p, a, b])

    def ends_at(n: int) -> list[tuple[int, int]]:
        return [(i, e) for i, br in enumerate(branches) if br is not None
                for e in (0, 1) if br[1 + e] == n]

    # tiny loops inside a junction (a pixel touching two pixels of the same node) are noise;
    # the eye of an "e" or a small "o" (a hole in the ink) is longer than LOOP_K half widths
    branches = [br if not (br[1] and br[1] == br[2]
                           and _length(br[0]) < LOOP_K * spur_at[br[1]] / SPUR_K) else None
                for br in branches]
    # prune spurs: a short branch from a line end into a junction
    degree = np.zeros(n_nodes, int)
    for br in branches:
        if br is not None:
            degree[br[1]] += 1
            degree[br[2]] += 1
    for i, br in enumerate(branches):
        if br is None:
            continue
        p, a, b = br
        if a and b and (
                (degree[a] == 1 and degree[b] >= 3 and _length(p) < spur_at[b])
                or (degree[b] == 1 and degree[a] >= 3 and _length(p) < spur_at[a])):
            branches[i] = None
            degree[a] -= 1
            degree[b] -= 1

    # pair branch ends at each node: two ends always; at a junction the straightest pairs
    partner: dict[tuple[int, int], tuple[int, int]] = {}
    at: dict[int, list[tuple[int, int]]] = {}
    for i, br in enumerate(branches):
        if br is None:
            continue
        for e in (0, 1):
            if br[1 + e]:
                at.setdefault(br[1 + e], []).append((i, e))
    for n, ends in at.items():
        if len(ends) == 2:
            partner[ends[0]], partner[ends[1]] = ends[1], ends[0]
            continue
        if len(ends) < 2:
            continue
        dirs = {end: _direction(branches[end[0]][0], end[1], DIR_PX * r) for end in ends}
        pairs = sorted((float(np.dot(dirs[u], dirs[v])), u, v)
                       for k, u in enumerate(ends) for v in ends[k + 1:])
        for dot, u, v in pairs:
            if dot > JOIN_DOT:
                break
            if u not in partner and v not in partner:
                partner[u], partner[v] = v, u

    # chains: follow the pairings from an unpaired end; what is left are closed loops
    used = [br is None for br in branches]
    chains: list[tuple[np.ndarray, bool]] = []

    def follow(i: int, e: int) -> tuple[np.ndarray, bool]:
        parts, start = [], (i, e)
        while True:
            used[i] = True
            p = branches[i][0]
            p = p if e == 0 else p[::-1]
            parts.append(p if not parts else p[1:])
            nxt = partner.get((i, 1 - e))
            if nxt is None:
                return np.vstack(parts), False
            if nxt == start:
                return np.vstack(parts), True
            i, e = nxt
            if used[i]:
                return np.vstack(parts), False

    for i, br in enumerate(branches):
        if br is None or used[i]:
            continue
        for e in (0, 1):
            if (i, e) not in partner:
                p, closed = follow(i, e)
                chains.append((p, closed or (br[1] == 0 and br[2] == 0)))
                break
    for i, br in enumerate(branches):
        if br is not None and not used[i]:
            p, closed = follow(i, 0)
            chains.append((p, closed or (br[1] == 0 and br[2] == 0)))
    return chains


def _direction(p: np.ndarray, end: int, along: float = DIR_PX) -> np.ndarray:
    """Unit vector from a branch's end at a node into the branch (towards its point
    `along` px in)."""
    q = p if end == 0 else p[::-1]
    steps = np.cumsum(np.hypot(*np.diff(q, axis=0).T)) if len(q) > 1 else np.zeros(0)
    k = int(np.searchsorted(steps, along)) + 1 if steps.size else 0
    v = q[min(k, len(q) - 1)] - q[0]
    n = float(np.hypot(*v))
    return v / n if n > 0 else np.zeros(2)


def _length(p: np.ndarray) -> float:
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


def _simplified(chains, eps: float, view: _View,
                extra: list[tuple[np.ndarray, float]] = ()) -> list[tuple[np.ndarray, float]]:
    """Douglas-Peucker each chain (box px; the tolerance shrinks with the size of the mark
    it belongs to, so a small letter keeps its curves), drop slivers, keep only segments
    with an end on the page (build clips the rest exactly), top to bottom. `extra`:
    pieces already on the page (sharp text), sorted in with the rest."""
    out = []
    for p, closed, size, pen in chains:
        dot = len(p) == 2 and _length(p) <= DOT_LEN * 1.01  # a dot (centre_lines)
        if len(p) < 2 or (_length(p) < SLIVER_PX and not dot):
            continue
        if closed and len(p) >= 4 and np.allclose(p[0], p[-1]):
            p = p[:-1]
        e = eps * min(1.0, max(EPS_SIZE_MIN, size / EPS_SIZE))
        s = cv2.approxPolyDP(p.astype(np.float32).reshape(-1, 1, 2), e, closed)
        s = s.reshape(-1, 2).astype(np.float64)
        if closed and len(s) >= 2:
            s = np.vstack([s, s[:1]])
        if len(s) < 2:
            s = p[[0, -1]]
        _on_page(s, pen, view, out)
    out += extra
    out.sort(key=lambda sp: (float(sp[0][:, 1].min()), float(sp[0][:, 0].min())))
    return out


def _on_page(s: np.ndarray, pen: float, view: _View, out: list) -> None:
    """Append the runs of `s`'s segments that have an end on the page to `out`."""
    on = view.on_page(s[:, 0], s[:, 1])
    seg = on[:-1] | on[1:]
    if seg.all():
        out.append((s, pen))
        return
    k = 0
    while k < len(seg):
        if not seg[k]:
            k += 1
            continue
        j = k
        while j < len(seg) and seg[j]:
            j += 1
        out.append((s[k:j + 1], pen))
        k = j


# -- default style by content ---------------------------------------------------------------

GUESS_SIDE = 256  # the content guess looks at the picture at most this long side
GUESS_WHITE = 0.5  # at least this share of the picture is paper (grey >= GUESS_PAPER)
GUESS_PAPER = 225
GUESS_LEVELS = 4  # at most this many other grey levels (of 16) hold >= GUESS_SHARE each
GUESS_SHARE = 0.02


def looks_like_diagram(path: Path) -> bool:
    """A quick guess: mostly white with few distinct grey levels (a diagram, logo, chart or
    drawing) rather than a photo. Transparent parts count as white. Raises ValueError if
    the file cannot be read."""
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError(f"cannot read image: {path}")
    h, w = raw.shape[:2]
    k = min(1.0, GUESS_SIDE / max(w, h))
    small = cv2.resize(raw, (max(1, round(w * k)), max(1, round(h * k))),
                       interpolation=cv2.INTER_NEAREST)  # keeps the levels (no blending)
    grey = (1.0 - _darkness(small, small.shape[1], small.shape[0],
                            (0, 0, small.shape[1], small.shape[0]), 1.0, 1.0)) * 255.0
    paper = grey >= GUESS_PAPER
    if paper.mean() < GUESS_WHITE:
        return False
    hist = np.bincount((grey[~paper] // 16).astype(np.int64).ravel(), minlength=16)
    return int((hist / grey.size >= GUESS_SHARE).sum()) <= GUESS_LEVELS
