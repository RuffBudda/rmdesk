"""Raster picture -> shaded hatching strokes (pure, Qt-free).

A picture is drawn as horizontal hatching lines (optionally a 45-degree
cross-hatch over the darkest parts). Each point carries its own width and
pressure, so darker parts get thicker, firmer lines.

Coordinates are BOX pixels: (0..box_w, 0..box_h), the picture fitted into the
box keeping its aspect ratio and centred (the same fit-and-centre as
`build.place` uses for pictures). `build.shade_to_lines` places, rotates,
clips and maps them to the page.

Only the part of the box that can land on the page is shaded (ruling S4): the
box's page position and rotation are given, the raster is cropped to the
visible region (never more than RASTER_MAX_PX pixels), and only samples that
land on the page become points, so the point budget is spent on visible ink.

"The page" is the frame rectangle (0..PAGE_W, 0..PAGE_H) by default. In the keyboard layout
items may lie past the frame's ends along x and still be on the (scrolled) page: shading
inside `with region(KEYBOARD_REGION):` counts those parts as on the page (line art too: it
uses the same `_View`). The region is per thread and per context (a ContextVar).
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..notebook.page import PAGE_H, PAGE_W
from .items import ImageItem

# (x, y, width, pressure): x/y in box pixels, width/pressure in tablet units
Stroke = list[tuple[float, float, float, float]]

# Provisional (ruling S1): the pen the shaded strokes are drawn with until the
# device test strip has compared the tools on a real tablet.
DEFAULT_SHADE_PEN = "pencil"

SPACING_MIN, SPACING_MAX, SPACING_DEFAULT = 3, 12, 5
CONTRAST_MIN, CONTRAST_MAX = 0.5, 2.0
D_MIN = 0.06  # darkness below this is paper: no ink
CROSS_THRESHOLD = 0.6  # cross-hatch only where darkness exceeds this
POINT_STEP = 2  # px between samples along a line
# Adaptive sampling (ruling S3): along a run a sample is kept only when its
# width differs from the last kept point by >= WIDTH_STEP tablet units, or it
# is >= KEEP_EVERY_PX px past the last kept point; each run's first and last
# samples are always kept. Same tone, far fewer points.
WIDTH_STEP = 1.0
KEEP_EVERY_PX = 12
# Point budget for one shaded picture (ruling S3): larger than the old
# line-picture cap of 20000; real handwriting pages carry far more points.
SHADE_MAX_POINTS = 60000
# Largest raster ever processed (ruling S4): twice the page area. A crop
# bigger than this (a huge box rotated so the page meets it diagonally) is
# rasterised at reduced resolution; sample positions stay the same.
RASTER_MAX_PX = 2 * PAGE_W * PAGE_H

# Where box points count as "on the page" (x0, y0, x1, y1), box = frame coordinates.
FRAME_REGION = (0.0, 0.0, float(PAGE_W), float(PAGE_H))
# Keyboard layout: the page's parts past either end of the frame along x are kept (the send
# clips to the page in page coordinates). Finite, so rotated corners stay real numbers.
KEYBOARD_REGION = (-1e6, 0.0, 1e6, float(PAGE_H))
_REGION: ContextVar[tuple[float, float, float, float]] = ContextVar("shade_region",
                                                                   default=FRAME_REGION)


@contextmanager
def region(rect: tuple[float, float, float, float]):
    """Shade (and line-art) with `rect` as the page region while inside the block."""
    token = _REGION.set(tuple(float(v) for v in rect))
    try:
        yield
    finally:
        _REGION.reset(token)


# Tablet width units per page pixel. rmscene reads version-1 point widths
# (stored as float pixels) as `int(round(px * 4))` and writes them back as
# `width / 4`, so si.Point.width is in quarter pixels. `build._page_line` writes
# `PEN_WIDTH` straight into si.Point.width in these same units (fineliner 16,
# i.e. 4 px).
WIDTH_UNITS_PER_PX = 4
WIDTH_MIN, WIDTH_MAX = 1, 0xFFFF  # page._check_line: 0..0xFFFF; 0 would be invisible
PRESSURE_MIN, PRESSURE_MAX = 0, 0xFF  # page._check_line


def MAX_SHADE_WIDTH(spacing: float) -> float:
    """Point width (tablet units) of a fully black line at `spacing` px.

    The darkest line covers 0.9 of the gap to the next line (so neighbouring
    black lines just do not merge): `spacing * 0.9` page px, times
    WIDTH_UNITS_PER_PX. Spacing 5 -> 18.
    """
    return spacing * 0.9 * WIDTH_UNITS_PER_PX


@dataclass
class ShadeResult:
    strokes: list[Stroke]
    size: tuple[int, int]  # the box (width, height) the strokes are laid out in
    capped: bool  # True if only part of the picture fits under SHADE_MAX_POINTS
    spacing_used: int  # may be wider than asked for, to keep under SHADE_MAX_POINTS


def shade_item(item: ImageItem, spacing: int = SPACING_DEFAULT, contrast: float = 1.0,
               crosshatch: bool = False, invert: bool = False) -> ShadeResult:
    """`shade_image` for an item: its picture, box size, page position and rotation."""
    return shade_image(item.path, int(round(item.box.w)), int(round(item.box.h)), spacing,
                       contrast, crosshatch, box_x=item.box.x, box_y=item.box.y,
                       rotation=item.rotation, invert=invert)


def shade_image(path: Path, box_w: int, box_h: int, spacing: int = SPACING_DEFAULT,
                contrast: float = 1.0, crosshatch: bool = False, *,
                box_x: float = 0.0, box_y: float = 0.0, rotation: float = 0.0,
                invert: bool = False) -> ShadeResult:
    """Shade the picture at `path` into a box_w x box_h box.

    (box_x, box_y) is the box's top-left on the page (page pixels, 0..PAGE_W)
    and `rotation` its rotation in degrees clockwise around the box centre,
    as in `ImageItem`; the defaults put the box unrotated at the page origin.
    Only samples landing on the page become points; a box entirely off the
    page gives no strokes (capped False). Raises ValueError if the file
    cannot be read. A blank (white or fully transparent) picture gives no
    strokes. `invert` swaps light and dark in the picture itself (a white logo on a dark
    background is drawn as the logo); transparent parts stay empty either way.
    """
    spacing = int(min(max(int(spacing), SPACING_MIN), SPACING_MAX))
    contrast = float(min(max(contrast, CONTRAST_MIN), CONTRAST_MAX))
    size = (int(box_w), int(box_h))
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError(f"cannot read image: {path}")
    empty = ShadeResult([], size, False, spacing)
    if box_w < 1 or box_h < 1:
        return empty

    h, w = raw.shape[:2]
    scale = min(box_w / w, box_h / h)
    tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    ox, oy = (box_w - tw) / 2, (box_h - th) / 2
    view = _View(box_w, box_h, box_x, box_y, rotation)

    # visible region in fitted-image pixels
    vx0, vy0, vx1, vy1 = view.visible_box()
    fx0, fx1 = max(0.0, vx0 - ox), min(float(tw), vx1 - ox)
    fy0, fy1 = max(0.0, vy0 - oy), min(float(th), vy1 - oy)
    if fx1 <= fx0 or fy1 <= fy0:
        return empty
    region = (int(math.floor(fx0)), int(math.floor(fy0)),
              int(math.ceil(fx1)), int(math.ceil(fy1)))
    pad = SPACING_MAX + 2  # so blurs near the crop edge see the real neighbours
    crop = (max(0, region[0] - pad), max(0, region[1] - pad),
            min(tw, region[2] + pad), min(th, region[3] + pad))
    area = (crop[2] - crop[0]) * (crop[3] - crop[1])
    r = min(1.0, math.sqrt(RASTER_MAX_PX / area))
    raster = _Raster(_darkness(raw, tw, th, crop, r, contrast, invert), crop, r)

    sp = spacing
    while True:
        passes = _passes(raster, tw, th, region, sp, crosshatch, view, ox, oy)
        picks = [_pick(widths, rows, starts, ends)
                 for _xs, _ys, _d, widths, rows, starts, ends in passes]
        total = sum(len(idx) for runs in picks for _r, idx in runs)
        if total <= SHADE_MAX_POINTS or sp >= SPACING_MAX:
            break
        sp += 1

    strokes: list[Stroke] = []
    count = 0
    capped = False
    for (xs, ys, darkness, widths, *_runs), runs in zip(passes, picks):
        for r_, idx in runs:
            if count + len(idx) > SHADE_MAX_POINTS:
                capped = True
                break
            strokes.append([(float(xs[r_, k] + ox), float(ys[r_, k] + oy),
                             float(widths[r_, k]), float(darkness[r_, k] * 255.0))
                            for k in idx])
            count += len(idx)
        if capped:
            break
    return ShadeResult(strokes, size, capped and bool(strokes), sp)


class _View:
    """Where box pixels land on the page (same rotation as `place.rotate_polylines`)."""

    def __init__(self, box_w, box_h, box_x, box_y, rotation):
        self.bx, self.by = float(box_x), float(box_y)
        self.cx, self.cy = box_w / 2, box_h / 2
        self.box_w, self.box_h = box_w, box_h
        self.turned = rotation % 360.0 != 0.0
        self.region = _REGION.get()
        rad = math.radians(rotation)
        self.c, self.s = math.cos(rad), math.sin(rad)

    def on_page(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Mask of box points (x, y) that land on the page rectangle."""
        if self.turned:
            dx, dy = x - self.cx, y - self.cy
            x, y = self.cx + dx * self.c - dy * self.s, self.cy + dx * self.s + dy * self.c
        px, py = x + self.bx, y + self.by
        x0, y0, x1, y1 = self.region
        return (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)

    def visible_box(self) -> tuple[float, float, float, float]:
        """Box-pixel bounding box of the page turned back into the box, clipped to the box."""
        xs, ys = [], []
        x0, y0, x1, y1 = self.region
        for qx, qy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            dx, dy = qx - self.bx - self.cx, qy - self.by - self.cy
            if self.turned:  # inverse rotation
                dx, dy = dx * self.c + dy * self.s, -dx * self.s + dy * self.c
            xs.append(self.cx + dx)
            ys.append(self.cy + dy)
        return (max(0.0, min(xs)), max(0.0, min(ys)),
                min(float(self.box_w), max(xs)), min(float(self.box_h), max(ys)))


class _Raster:
    """Darkness of the fitted picture over `crop` (fitted px), stored at resolution r."""

    def __init__(self, d: np.ndarray, crop, r: float):
        self.d, self.x0, self.y0, self.r = d, crop[0], crop[1], r

    def blurred(self, kw: float, kh: float) -> np.ndarray:
        k = (max(1, int(round(kw * self.r))), max(1, int(round(kh * self.r))))
        return cv2.blur(self.d, k, borderType=cv2.BORDER_REFLECT)

    def index(self, xi: np.ndarray, yi: np.ndarray):
        """Raster indices of fitted pixels (xi, yi)."""
        h, w = self.d.shape
        ix = np.clip(((xi - self.x0 + 0.5) * self.r).astype(np.int64), 0, w - 1)
        iy = np.clip(((yi - self.y0 + 0.5) * self.r).astype(np.int64), 0, h - 1)
        return ix, iy


def _darkness(raw: np.ndarray, tw: int, th: int, crop, r: float,
              contrast: float, invert: bool = False) -> np.ndarray:
    """Darkness 0..1 (float32) of the picture fitted to tw x th, over `crop` at resolution r.

    Alpha over white, grey, contrast around mid-grey. The source is first
    shrunk (INTER_AREA) to at most the raster's resolution or 2x the target,
    then the crop alone is resampled, so a huge box never builds a huge raster.
    """
    img = raw
    if img.dtype == np.uint16:
        img = (img // 257).astype(np.uint8)
    elif img.dtype != np.uint8:
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    h, w = img.shape[:2]
    # Huge photos: shrink to at most 2x the target first so the rest stays cheap.
    if w > 2 * tw or h > 2 * th:
        img = cv2.resize(img, (min(w, 2 * tw), min(h, 2 * th)), interpolation=cv2.INTER_AREA)
    img = img.astype(np.float32)
    flip = (lambda g: 255.0 - g) if invert else (lambda g: g)  # before alpha: clear stays white
    if img.ndim == 2:
        grey = flip(img)
    elif img.shape[2] == 4:
        a = img[:, :, 3] / 255.0
        grey = flip(cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)) * a + 255.0 * (1.0 - a)
    elif img.shape[2] == 3:
        grey = flip(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    else:
        grey = flip(img[:, :, 0])
    # down to the raster's resolution with area averaging (no aliasing)
    rw, rh = max(1, int(round(tw * r))), max(1, int(round(th * r)))
    if grey.shape[1] > rw or grey.shape[0] > rh:
        grey = cv2.resize(grey, (rw, rh), interpolation=cv2.INTER_AREA)
    gh, gw = grey.shape
    x0, y0, x1, y1 = crop
    out_w = max(1, int(math.ceil((x1 - x0) * r)))
    out_h = max(1, int(math.ceil((y1 - y0) * r)))
    # raster pixel (u, v) centre -> fitted (x0 + (u + .5) / r) -> grey pixel coordinate
    sx, sy = gw / (tw * r), gh / (th * r)
    m = np.array([[sx, 0.0, (x0 + 0.5 / r) * gw / tw - 0.5],
                  [0.0, sy, (y0 + 0.5 / r) * gh / th - 0.5]], np.float64)
    grey = cv2.warpAffine(grey, m, (out_w, out_h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)
    grey = np.clip((grey - 127.5) * contrast + 127.5, 0.0, 255.0)
    return (1.0 - grey / 255.0).astype(np.float32)


def _passes(raster: _Raster, tw: int, th: int, region, spacing: int, crosshatch: bool,
            view: _View, ox: float, oy: float):
    """Sample grids for each hatching pass: (xs, ys, darkness, width, run rows, starts, ends).

    Grids are (lines x points along the line) in fitted-picture pixels, laid
    out over the whole picture (so cropping never moves a line) but limited
    to `region`; a run is a stretch of consecutive inked samples on one line
    that land on the page, at least 2 long.
    """
    x_lo, y_lo, x_hi, y_hi = region
    max_w = MAX_SHADE_WIDTH(spacing)
    out = []

    # Primary: horizontal lines every `spacing` px, each point the mean
    # darkness of its 2 x spacing cell.
    rows = np.arange(spacing // 2, th, spacing)
    if rows.size == 0:
        rows = np.array([th // 2])
    rows = rows[(rows >= y_lo) & (rows < y_hi)]
    cols = np.arange(0, tw, POINT_STEP)
    cols = cols[(cols >= x_lo) & (cols < x_hi)]
    if rows.size and cols.size:
        band = raster.blurred(POINT_STEP, spacing)
        ix, iy = raster.index(cols, rows)
        dv = band[np.ix_(iy, ix)]
        xs = np.broadcast_to(cols[None, :] + 0.5, dv.shape)
        ys = np.broadcast_to(rows[:, None] + 0.5, dv.shape)
        widths = np.clip(dv * max_w, WIDTH_MIN, WIDTH_MAX)
        mask = (dv > D_MIN) & view.on_page(xs + ox, ys + oy)
        out.append((xs, ys, np.clip(dv, 0, 1), widths) + _runs(mask))

    if crosshatch:
        # 45-degree lines x - y = c, `spacing` px apart (c steps spacing * sqrt 2),
        # a point every POINT_STEP px along the line (x steps POINT_STEP / sqrt 2).
        cs = np.arange(-(th - 1), tw, spacing * math.sqrt(2))
        cs = cs[(cs >= x_lo - y_hi - 1) & (cs <= x_hi - y_lo + 1)]
        xg = np.arange(0, tw, POINT_STEP / math.sqrt(2))
        xg = xg[(xg >= x_lo) & (xg < x_hi)]
        if cs.size and xg.size:
            area = raster.blurred(spacing, spacing)
            X = np.broadcast_to(xg[None, :], (cs.size, xg.size))
            Y = X - cs[:, None]
            valid = (Y >= max(0, y_lo)) & (Y <= min(th - 1, y_hi))
            xi = np.clip(X.astype(np.int64), 0, tw - 1)
            yi = np.clip(np.floor(Y).astype(np.int64), 0, th - 1)
            ix, iy = raster.index(xi, yi)
            dc = np.where(valid, area[iy, ix], 0.0)
            scaled = np.clip((dc - CROSS_THRESHOLD) / (1.0 - CROSS_THRESHOLD), 0.0, 1.0)
            widths = np.clip(scaled * max_w, WIDTH_MIN, WIDTH_MAX)
            xs, ys = X + 0.5, Y + 0.5
            mask = valid & (dc > CROSS_THRESHOLD) & view.on_page(xs + ox, ys + oy)
            out.append((xs, ys, np.clip(dc, 0, 1), widths) + _runs(mask))
    return out


def _pick(widths: np.ndarray, rows, starts, ends) -> list[tuple[int, list[int]]]:
    """Per run, in reading order: (row, sample indices kept by adaptive sampling)."""
    every = max(1, math.ceil(KEEP_EVERY_PX / POINT_STEP))
    out = []
    for r, s, e in zip(rows.tolist(), starts.tolist(), ends.tolist()):
        ws = widths[r, s:e].tolist()
        idx = [s]
        last_w, last_k = ws[0], 0
        for k in range(1, len(ws) - 1):
            w = ws[k]
            if abs(w - last_w) >= WIDTH_STEP or k - last_k >= every:
                idx.append(s + k)
                last_w, last_k = w, k
        idx.append(e - 1)
        out.append((r, idx))
    return out


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Runs of True along each row, at least 2 long: (rows, start cols, end cols exclusive)."""
    n_rows, n_cols = mask.shape
    padded = np.zeros((n_rows, n_cols + 2), np.int8)
    padded[:, 1:-1] = mask
    diff = np.diff(padded, axis=1)
    rows, starts = np.nonzero(diff == 1)
    _rows, ends = np.nonzero(diff == -1)  # same rows, same (row-major) order as the starts
    keep = (ends - starts) >= 2
    return rows[keep], starts[keep], ends[keep]
