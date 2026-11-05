"""Stream-frame coordinates -> reMarkable page coordinates.

The Screen view frame is 1404x1872 (frame x right, y down). Page x runs
-702..702 (0 is the horizontal centre), y 0..1872.
"""
from __future__ import annotations

from dataclasses import dataclass

FRAME_W = 1404
FRAME_H = 1872
_HALF = FRAME_W / 2

Point = tuple[float, float]


KEYBOARD = "keyboard"  # Type Folio attached: the tablet shows portrait pages turned, 1:1, scrolled
PORTRAIT_UI = "portrait"


def frame_to_page(x: float, y: float, orientation: str, *, layout: str = PORTRAIT_UI,
                  scroll: float = 0.0) -> Point:
    """Map a frame point to page coordinates for the given page orientation and tablet layout.

    Portrait page, portrait layout: (x - 702, y). Device calibration, 2026-11-03,
    fw 3.29.1.0, notebook "rmdesk test": crosses drawn with this formula landed exactly
    on the four corners and centre of the live framebuffer.

    Portrait page, keyboard layout (Type Folio attached; the tablet logs "Setting new
    orientation Landscape"): the page is shown a quarter turn round, at 1:1, scrolled by
    `scroll` page pixels: (y - 936, 1404 - x + scroll). Device calibration, 2026-11-04,
    fw 3.29.1.0, "rmdesk test": a labelled grid of crosses at known page points landed on
    these frame points to within a pixel, unscrolled and scrolled.

    Landscape pages are not calibrated and raise NotImplementedError. Unknown
    orientation or layout strings raise ValueError.
    """
    if orientation == "portrait":
        if layout == PORTRAIT_UI:
            return (x - _HALF, y)
        if layout == KEYBOARD:
            return (y - FRAME_H / 2, FRAME_W - x + scroll)
        raise ValueError(f"unknown layout: {layout!r}")
    if orientation == "landscape":
        raise NotImplementedError("landscape not calibrated")
    raise ValueError(f"unknown orientation: {orientation!r}")


def polylines_to_page(lines: list[list[Point]], orientation: str, *, layout: str = PORTRAIT_UI,
                      scroll: float = 0.0) -> list[list[Point]]:
    return [[frame_to_page(x, y, orientation, layout=layout, scroll=scroll) for x, y in pl]
            for pl in lines]


@dataclass(frozen=True)
class Anchored:
    """Keyboard layout, placement inside a text-anchored group (see compose.scroll_fit).

    The group's points are local: page x = local x + origin_x (its anchor_origin_x) and
    page y = local y + the y of the anchor's text line. That y is unknown, but the live frame
    gives `const`: a local point is shown at frame (fx, fy) = (const - local_y, x + 936)."""
    const: float
    origin_x: float


def frame_to_anchored(x: float, y: float, a: Anchored) -> Point:
    """A frame point (keyboard layout) -> local coordinates of the anchored group `a`:
    (y - 936 - origin_x, const - x). The inverse of what scroll_fit fits."""
    return (y - FRAME_H / 2 - a.origin_x, a.const - x)
