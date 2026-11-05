"""Polylines -> reMarkable si.Line strokes placed in a page box."""
from __future__ import annotations

from rmscene import scene_items as si

from ..notebook.page import PAGE_H, PAGE_W, X_MIN
from .frame import KEYBOARD, PORTRAIT_UI, Anchored, frame_to_page, polylines_to_page
from .items import ImageItem, InkTextItem, PenStyle
from .place import Polyline, rotate_polylines
from .shade import (PRESSURE_MAX, PRESSURE_MIN, WIDTH_MAX, WIDTH_MIN, ShadeResult,
                    Stroke)

_TOOL = {"fineliner": si.Pen.FINELINER_2, "ballpoint": si.Pen.BALLPOINT_2,
         "marker": si.Pen.MARKER_2,
         "pencil": si.Pen.PENCIL_2}  # shaded pictures only (shade.DEFAULT_SHADE_PEN)
_COLOR = {"black": si.PenColor.BLACK, "grey": si.PenColor.GRAY, "white": si.PenColor.WHITE}

# Point widths read from real strokes (median point width of the first stroke
# of that pen):
#   fineliner: tests/fixtures/rm/Normal_A_stroke_2_layers_v3.3.2.rm (FINELINER_2, 17 pts) -> 7;
#              Normal_A_stroke_2_layers.rm has constant 16 on its FINELINER_2 lines -> 16 used.
#   ballpoint: tests/fixtures/rm/More_color_highlight_shader_v3.15.4.2.rm (BALLPOINT_2 black, 46 pts) -> 18.
#   marker:    NO MARKER_2 stroke exists in any fixture; 30 is an estimate (unverified).
#   pencil:    provisional until the device test strip -> the ballpoint's 18.
PEN_WIDTH = {"fineliner": 16, "ballpoint": 18, "marker": 30, "pencil": 18}
# Median pressure of the fineliner strokes in the same two fixtures (~221).
PRESSURE = 220
FRAME_HALF_H = PAGE_H / 2  # keyboard layout: page x = frame y - 936
_ANY_SCROLL = 1e7  # has_lines_on_page: a scroll so large nothing is above the page's top


def place(item: ImageItem | InkTextItem, polylines: list[Polyline],
          size: tuple[int, int] | None = None) -> list[Polyline]:
    """Polylines placed in item.box, in box coordinates (0..PAGE_W, 0..PAGE_H), unclipped.

    Images: `polylines` are in the unit square (longest source side = 1) and
    `size` is the source (width, height); the image is scaled to fit the box
    keeping its aspect ratio and centred. Ink text: pixel units, offset by box.
    The item's rotation is applied last, around the box centre (before any clipping).
    """
    box = item.box
    if isinstance(item, ImageItem):
        w, h = size or (1, 1)
        scale = min(box.w / w, box.h / h)
        longest = float(max(w, h))
        ox = box.x + (box.w - w * scale) / 2
        oy = box.y + (box.h - h * scale) / 2
        k = scale * longest
        placed = [[(ox + x * k, oy + y * k) for x, y in pl] for pl in polylines]
    else:
        placed = [[(box.x + x, box.y + y) for x, y in pl] for pl in polylines]
    return rotate_polylines(placed, box.x + box.w / 2, box.y + box.h / 2, item.rotation)


def to_lines(item: ImageItem | InkTextItem, polylines: list[Polyline],
             size: tuple[int, int] | None = None) -> list[si.Line]:
    """Place polylines in item.box (box coordinates) and clip them to the page."""
    return [_line(item.pen, piece) for pl in place(item, polylines, size) for piece in _clip(pl)]


def frame_to_lines(item: ImageItem | InkTextItem, polylines: list[Polyline],
                   size: tuple[int, int] | None, orientation: str, *,
                   layout: str = PORTRAIT_UI, scroll: float = 0.0,
                   anchored: Anchored | None = None) -> list[si.Line]:
    """Lines for an item whose box is in live-frame coordinates (1404x1872, unrotated).

    Portrait layout: the frame rectangle is the page rectangle in box coordinates, so the
    placed polylines are clipped there first, then mapped once with
    `frame.polylines_to_page` (which already subtracts 702) and built without
    any further shift.

    Keyboard layout: the frame shows only part of a longer page, and placed items follow
    the page as it scrolls (ui.live_compose), so an item may lie past the frame's ends
    (frame x < 0: further down the page; > 1404: further up). Nothing is clipped to the
    frame: the mapped pieces are clipped in page coordinates instead, to the page width and
    y >= 0 (no bottom: a page grows), so the parts outside the visible frame are kept.

    `anchored` (keyboard layout only; `scroll` is then unused): the lines are for a new
    group anchored like an existing one, in its local coordinates (`_to_anchored`).
    """
    _check_anchored(orientation, layout, anchored)
    pieces = _frame_pieces(place(item, polylines, size), layout)
    if anchored is not None:
        return [_page_line(item.pen, pl) for pl in _to_anchored(pieces, anchored)]
    mapped = polylines_to_page(pieces, orientation, layout=layout, scroll=scroll)
    if layout != PORTRAIT_UI:
        mapped = _clip_page_width(mapped)
    return [_page_line(item.pen, pl) for pl in mapped]


def _frame_pieces(placed: list, layout: str) -> list:
    """Placed polylines (box = frame coordinates) cut to what the layout may send: the frame
    rectangle in the portrait layout; all of them (with 2+ points) in the keyboard layout,
    whose page is clipped after mapping (`_clip_page_width`)."""
    if layout == PORTRAIT_UI:
        return [piece for pl in placed for piece in _clip(pl)]
    return [list(pl) for pl in placed if len(pl) >= 2]


def _check_anchored(orientation: str, layout: str, anchored: Anchored | None) -> None:
    if anchored is None:
        return
    frame_to_page(0.0, 0.0, orientation, layout=layout)  # refuse before building anything
    if layout != KEYBOARD or orientation != "portrait":
        raise ValueError("anchored placement is only calibrated in the keyboard layout")


def _to_anchored(pieces: list, a: Anchored) -> list:
    """Frame pieces (x, y, extras...) -> local pieces of the anchored group `a`.

    Mapped first to (absolute page x, local y) = (fy - 936, C - fx), clipped to the page
    width on that absolute x (local y is unbounded: it is relative to a text line), then
    made local in x: x - origin_x. Extra components ride along."""
    mapped = [[(pt[1] - FRAME_HALF_H, a.const - pt[0], *pt[2:]) for pt in pl] for pl in pieces]
    clipped = _clip_page_width(mapped, y_min=float("-inf"))
    return [[(pt[0] - a.origin_x, *pt[1:]) for pt in pl] for pl in clipped]


def has_lines_on_page(item: ImageItem | InkTextItem, polylines: list[Polyline],
                      size: tuple[int, int] | None = None, *,
                      layout: str = PORTRAIT_UI) -> bool:
    """Whether any part of the placed item lies on the page (after clipping). Keyboard
    layout: within the page's width anywhere along the page (the scroll isn't known yet)."""
    placed = place(item, polylines, size)
    if layout == PORTRAIT_UI:
        return any(_clip(pl) for pl in placed)
    mapped = polylines_to_page(_frame_pieces(placed, layout), "portrait", layout=layout,
                               scroll=_ANY_SCROLL)
    return bool(_clip_page_width(mapped))


def shade_to_lines(item: ImageItem, result: ShadeResult, pen: PenStyle,
                   clip: bool = True) -> list[si.Line]:
    """Page lines for shaded strokes (`shade.shade_image`) placed in item.box.

    The strokes are in result.size box pixels; they are placed in item.box the
    way `place` places an image of that size (identity plus the box offset when
    the box has that size; otherwise fitted, centred and widths scaled with
    it), rotated by item.rotation around the box centre (ruling S2: x/y turn,
    width/pressure ride along), clipped to the page with width/pressure
    interpolated at the crossings, and mapped to page coordinates once
    (x + X_MIN, as `_line`). pen.thickness multiplies the shaded widths
    (clamped to the valid range). Shade with `shade.shade_item(item)` so only
    the part of the box on the page was shaded. `clip` False: not clipped to the page (the
    keyboard layout's preview: the send clips in page coordinates).
    """
    return [_shade_line(pen, piece) for piece in _shade_pieces(item, result, pen, clip)]


def frame_shade_to_lines(item: ImageItem, result: ShadeResult, pen: PenStyle,
                         orientation: str, *, layout: str = PORTRAIT_UI,
                         scroll: float = 0.0,
                         anchored: Anchored | None = None) -> list[si.Line]:
    """`shade_to_lines` for an item whose box is in live-frame coordinates (1404x1872).

    Like `frame_to_lines`: in the portrait layout clipped to the frame (= page) rectangle
    in box coordinates, in the keyboard layout not (clipped to the page width and y >= 0
    after mapping), then each point mapped once with `frame.frame_to_page`
    (width/pressure ride along). Shade keyboard-layout items with the frame's region
    widened (`shade.region(KEYBOARD_REGION)`) so their parts past the frame have strokes. Landscape raises NotImplementedError, an
    unknown orientation ValueError, as `frame_to_page` does. `anchored`: as frame_to_lines.
    """
    frame_to_page(0.0, 0.0, orientation, layout=layout)  # refuse before building anything
    _check_anchored(orientation, layout, anchored)
    clip = layout == PORTRAIT_UI
    if anchored is not None:
        return [_shade_page_line(pen, piece)
                for piece in _to_anchored(_shade_pieces(item, result, pen, clip), anchored)]
    mapped = [[(*frame_to_page(x, y, orientation, layout=layout, scroll=scroll), w, p)
               for x, y, w, p in piece] for piece in _shade_pieces(item, result, pen, clip)]
    if layout != PORTRAIT_UI:
        mapped = _clip_page_width(mapped)
    return [_shade_page_line(pen, piece) for piece in mapped]


def _clip_page_width(pieces: list, y_min: float = 0.0) -> list:
    """Clip page-coordinate pieces (x X_MIN..-X_MIN, y >= y_min; extra components ride along)
    to the page width. y has no upper bound: a scrolled page can be longer than 1872."""
    out = []
    for pl in pieces:
        shifted = [(pt[0] - X_MIN, *pt[1:]) for pt in pl]
        for piece in _clip(shifted, PAGE_W, float("inf"), y_min):
            out.append([(pt[0] + X_MIN, *pt[1:]) for pt in piece])
    return out


def _shade_pieces(item: ImageItem, result: ShadeResult, pen: PenStyle,
                  clip: bool = True) -> list[Stroke]:
    """Shaded strokes placed in item.box, rotated and clipped (unless `clip` is False: the
    keyboard layout clips later, in page coordinates): (x, y, width, pressure) in box
    coordinates, widths scaled with the box and pen.thickness."""
    sw, sh = result.size
    if sw < 1 or sh < 1 or not result.strokes:
        return []
    longest = float(max(sw, sh))
    k = min(item.box.w / sw, item.box.h / sh)
    unit = [[(x / longest, y / longest) for x, y, _w, _p in st] for st in result.strokes]
    placed = place(item, unit, (sw, sh))
    pieces = []
    for st, xy in zip(result.strokes, placed):
        pts = [(x, y, w * k * pen.thickness, p) for (x, y), (_x, _y, w, p) in zip(xy, st)]
        if clip:
            pieces += _clip(pts)
        elif len(pts) >= 2:
            pieces.append(pts)
    return pieces


def _shade_line(pen: PenStyle, pts: Stroke) -> si.Line:
    """A line from box-coordinate points carrying their own width/pressure."""
    return _shade_page_line(pen, [(x + X_MIN, y, w, p) for x, y, w, p in pts])


def _shade_page_line(pen: PenStyle, pts: Stroke) -> si.Line:
    """A line from page-coordinate points carrying their own width/pressure, used as is."""
    points = [si.Point(x=float(x), y=float(y), speed=0, direction=0,
                       width=min(max(int(round(w)), WIDTH_MIN), WIDTH_MAX),
                       pressure=min(max(int(round(p)), PRESSURE_MIN), PRESSURE_MAX))
              for x, y, w, p in pts]
    return si.Line(color=_COLOR[pen.color], tool=_TOOL[pen.tool], points=points,
                   thickness_scale=1.0, starting_length=0.0)


def _clip(pl, w: float = PAGE_W, h: float = PAGE_H, y0: float = 0.0) -> list:
    """Split a polyline where it leaves the rectangle (0..w, 0..h), the page by default.

    Each segment is clipped (Liang-Barsky) so the crossing point is
    interpolated; consecutive clipped segments are rejoined into one piece.
    Pieces with fewer than 2 points are dropped. Points are (x, y, ...) tuples:
    any extra components (shaded strokes: width, pressure) are interpolated
    at the crossing like x and y. `y0`: the rectangle's top (0; -inf for no top).
    """
    pieces: list[Polyline] = []
    cur: Polyline = []
    for a, b in zip(pl, pl[1:]):
        seg = _clip_segment(a, b, w, h, y0)
        if seg is None:
            if len(cur) >= 2:
                pieces.append(cur)
            cur = []
            continue
        p0, p1 = seg
        if cur and cur[-1] == p0:
            cur.append(p1)
        else:
            if len(cur) >= 2:
                pieces.append(cur)
            cur = [p0, p1]
    if len(cur) >= 2:
        pieces.append(cur)
    return pieces


def _clip_segment(a, b, w: float = PAGE_W, h: float = PAGE_H, y0: float = 0.0):
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0]), (dx, w - a[0]), (-dy, a[1] - y0), (dy, h - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    p0 = a if t0 == 0.0 else _lerp(a, b, t0)
    p1 = b if t1 == 1.0 else _lerp(a, b, t1)
    return p0, p1


def _lerp(a, b, t: float) -> tuple:
    return tuple(u + (v - u) * t for u, v in zip(a, b))


def _line(pen: PenStyle, pl: Polyline) -> si.Line:
    """A line from box coordinates (x 0..PAGE_W)."""
    return _page_line(pen, [(x + X_MIN, y) for x, y in pl])


def _page_line(pen: PenStyle, pl: Polyline) -> si.Line:
    """A line from page coordinates (x X_MIN..-X_MIN), used as is."""
    width = max(1, int(round(PEN_WIDTH[pen.tool] * pen.thickness)))
    points = [si.Point(x=float(x), y=float(y), speed=0, direction=0,
                       width=width, pressure=PRESSURE) for x, y in pl]
    return si.Line(color=_COLOR[pen.color], tool=_TOOL[pen.tool], points=points,
                   thickness_scale=1.0, starting_length=0.0)
