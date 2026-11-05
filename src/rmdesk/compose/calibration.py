"""Calibration marks at known frame points, for scripts/calibrate.py.

Portrait: five black crosses with the calibrated portrait formula.

Landscape: three candidate maps from the live frame (1404x1872, x right,
y down, as the Screen view shows it) to landscape page coordinates, each
drawn with its own pen. Evidence (fw 3.29.1.0, landscape notebook): strokes
written with the identity map showed up rotated 90 degrees and moved, so the
landscape page is probably 1872 wide x 1404 tall (x -936..936, y 0..1404)
and the tablet rotates it into the portrait framebuffer. Which way is unknown.

  "identity" (black fineliner): page = (fx - 702, fy);
      page rectangle x -702..702, y 0..1872 (the portrait formula).
  "rot90" (grey ballpoint): the frame turned 90 degrees clockwise into a
      1872x1404 image is (X, Y) = (1872 - fy, fx); centring X gives
      page = (1872 - fy - 936, fx) = (936 - fy, fx);
      page rectangle x -936..936, y 0..1404.
  "rot270" (black marker, thicker): turned counter-clockwise,
      (X, Y) = (fy, 1404 - fx), so page = (fy - 936, 1404 - fx);
      same page rectangle.

Each maps the four frame corners onto the four corners of its page
rectangle and the frame centre (702, 936) onto the rectangle's centre
((0, 936) or (0, 702)). Besides the crosses, each candidate draws an "L"
(150 px down to a corner at frame (200, 200), then 80 px right) so the
orientation can be read off the screen unambiguously. Every stroke is
clipped to its candidate's page rectangle.
"""
from __future__ import annotations

from typing import Callable

from rmscene import scene_items as si

from ..notebook import page as pagemod
from ..notebook.opened import OpenedPage
from .build import _clip, _page_line
from .frame import FRAME_H, FRAME_W, frame_to_page
from .items import PenStyle

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # x_min, y_min, x_max, y_max (page coordinates)

MARK_POINTS = [(60, 60), (1344, 60), (60, 1812), (1344, 1812), (702, 936)]
ARM = 10  # half-length of each cross stroke (strokes are 2*ARM = 20 px)
L_CORNER = (200, 200)  # frame point of the L's corner
L_DOWN = 150  # first stroke: from 150 px above the corner down to it
L_RIGHT = 80  # second stroke: from the corner 80 px to the right
_BLACK = PenStyle("fineliner", "black")
_GREY = PenStyle("ballpoint", "grey")
_MARKER = PenStyle("marker", "black")

_PX = FRAME_W / 2  # 702
_LX = FRAME_H / 2  # 936, half the landscape page width
PORTRAIT_RECT: Rect = (-_PX, 0.0, _PX, float(FRAME_H))
LANDSCAPE_RECT: Rect = (-_LX, 0.0, _LX, float(FRAME_W))


def _identity(fx: float, fy: float) -> Point:
    return (fx - _PX, fy)


def _rot90(fx: float, fy: float) -> Point:
    return ((FRAME_H - fy) - _LX, fx)


def _rot270(fx: float, fy: float) -> Point:
    return (fy - _LX, FRAME_W - fx)


# name -> (frame -> page map, its page rectangle, pen); drawn in this order
LANDSCAPE_CANDIDATES: dict[str, tuple[Callable[[float, float], Point], Rect, PenStyle]] = {
    "identity": (_identity, PORTRAIT_RECT, _BLACK),
    "rot90": (_rot90, LANDSCAPE_RECT, _GREY),
    "rot270": (_rot270, LANDSCAPE_RECT, _MARKER),
}
PEN_NAMES = {"identity": "black fineliner", "rot90": "grey ballpoint",
             "rot270": "black marker (thicker)"}


def _strokes(with_l: bool) -> list[list[Point]]:
    """The marks as frame polylines: crosses at MARK_POINTS, then the L if asked."""
    out = []
    for u, v in MARK_POINTS:
        out += [[(u - ARM, v), (u + ARM, v)], [(u, v - ARM), (u, v + ARM)]]
    if with_l:
        cx, cy = L_CORNER
        out += [[(cx, cy - L_DOWN), (cx, cy)], [(cx, cy), (cx + L_RIGHT, cy)]]
    return out


def _lines(to_page, rect: Rect, pen: PenStyle, strokes) -> list[si.Line]:
    """Map frame strokes to page coordinates and clip them to `rect`."""
    x0, y0, x1, y1 = rect
    out = []
    for pl in strokes:
        shifted = [(x - x0, y - y0) for x, y in (to_page(a, b) for a, b in pl)]
        for piece in _clip(shifted, x1 - x0, y1 - y0):
            out.append(_page_line(pen, [(x + x0, y + y0) for x, y in piece]))
    return out


def build_marks(orientation: str) -> list[si.Line]:
    """Portrait: five black crosses. Landscape: for each candidate in
    LANDSCAPE_CANDIDATES order, five crosses and the L in its pen."""
    if orientation == "portrait":
        return _lines(lambda x, y: frame_to_page(x, y, "portrait"), PORTRAIT_RECT, _BLACK,
                      _strokes(with_l=False))
    if orientation != "landscape":
        raise ValueError(f"unknown orientation: {orientation!r}")
    return [ln for to_page, rect, pen in LANDSCAPE_CANDIDATES.values()
            for ln in _lines(to_page, rect, pen, _strokes(with_l=True))]


def build_page(opened: OpenedPage) -> tuple[bytes, int]:
    lines = build_marks(opened.orientation)
    page = pagemod.load(opened.data)
    page.append_layer("rmdesk", lines)
    return page.to_bytes(), len(lines)


CONFIRM_TIMEOUT = 60


class NotCalibrationNotebook(Exception):
    """The open notebook is not a document named exactly "rmdesk test"."""


def guard(backup_dir, opened: OpenedPage) -> None:
    """Fail closed unless the backup's metadata says a document named "rmdesk test"."""
    import json
    from pathlib import Path

    from ..firmware import is_acceptance_name
    try:
        meta = json.loads((Path(backup_dir) / f"{opened.notebook_id}.metadata")
                          .read_text(encoding="utf-8"))
        ok = (meta.get("type") == "DocumentType"
              and is_acceptance_name(str(meta.get("visibleName", ""))))
    except (OSError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise NotCalibrationNotebook('refusing: the open notebook is not named "rmdesk test"')


def confirm(opened: OpenedPage, *, stdin=None, timeout: float = CONFIRM_TIMEOUT,
            out=print, select_fn=None, what: str = "calibration marks") -> bool:
    """Ask y/N on stdin; no answer within `timeout` seconds, EOF or anything but y/yes is No."""
    import select
    import sys
    stdin = stdin or sys.stdin
    select_fn = select_fn or select.select
    out(f"The tablet's screen is paused while you answer ({timeout:g} s).")
    out(f"Add {what} to “{opened.name}”, page "
        f"{opened.page_index + 1} of {opened.page_count} ({opened.orientation})? [y/N] ")
    ready, _, _ = select_fn([stdin], [], [], timeout)
    if not ready:
        return False
    return stdin.readline().strip().lower() in ("y", "yes")
