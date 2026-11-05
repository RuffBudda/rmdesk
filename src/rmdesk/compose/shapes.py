"""Basic shapes as polylines in their box's coordinates (pure, Qt-free).

The live preview and the send draw the same polylines: a shape gives them in box
coordinates (0..w, 0..h) and compose.build places, rotates, clips and maps them like any
other item's strokes.

    rectangle  a closed outline; with `fill`, 45-degree hatching inside, `hatch` px apart
    line       along the middle of the box, from its left edge to its right edge: the box
               width is the length and the item's rotation its direction
    arrow      a line with an open head at the right end (`double`: at both ends)
    rectangle  (also) may hold typed text, laid out like a note's but without the fold
               (`note_text_rect(..., fold=False)`); `gaps` keep the hatching off the text rows
    note       a sticky note: an outline with the top-right corner folded down (the
               diagonal and the fold triangle's two inner edges); its text goes inside
               `note_text_rect`
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .place import Polyline

KINDS = ("rectangle", "line", "arrow", "note")
LABELS = {"rectangle": "Rectangle", "line": "Line", "arrow": "Arrow", "note": "Sticky note"}
LINE_H = 40.0  # a line's box height, frame pixels: something to grab, not drawn
DEFAULT_SIZE = {"rectangle": (400.0, 280.0), "line": (400.0, LINE_H), "arrow": (400.0, LINE_H),
                "note": (440.0, 340.0)}
HATCH_SPACING = 5  # px between hatch lines: the shading's default (shade.SPACING_DEFAULT)
HEAD_ANGLE = 28.0  # degrees between the shaft and each side of an arrowhead
HEAD_FRAC = 0.2  # head side length as a fraction of the arrow's length...
HEAD_MIN, HEAD_MAX = 10.0, 28.0  # ...clamped to these, times the pen thickness
NOTE_PAD = 16.0  # around a sticky note's text, frame pixels
TEXT_BAND_PAD = 8.0  # hatching stays this far (px) from a text row above and below it
NOTE_FOLD_FRAC, NOTE_FOLD_MIN, NOTE_FOLD_MAX = 0.12, 12.0, 40.0


@dataclass(frozen=True)
class ShapeOptions:
    fill: bool = False  # rectangle: hatched inside
    double: bool = False  # arrow: a head at both ends
    hatch: float = HATCH_SPACING
    thickness: float = 1.0  # the pen's: an arrowhead grows with it


def arrow_head(length: float, thickness: float = 1.0) -> float:
    """Length of each side of an arrowhead: proportional to the arrow's length, clamped to
    HEAD_MIN..HEAD_MAX times the pen thickness, and never more than half the arrow."""
    t = max(float(thickness), 0.0)
    size = min(max(HEAD_FRAC * length, HEAD_MIN * t), HEAD_MAX * t)
    return min(size, 0.5 * length)


def note_fold(w: float, h: float) -> float:
    """Side of a sticky note's folded corner."""
    return min(max(NOTE_FOLD_FRAC * min(w, h), NOTE_FOLD_MIN), NOTE_FOLD_MAX)


def note_text_rect(w: float, h: float, fold: bool = True) -> tuple[float, float, float, float]:
    """(x, y, width, height) of the area a sticky note's text is wrapped into: NOTE_PAD
    inside the edges, and below the folded corner (a rectangle, `fold=False`, has none)."""
    top = note_fold(w, h) + 6.0 if fold else NOTE_PAD
    return NOTE_PAD, top, max(w - 2 * NOTE_PAD, 1.0), max(h - top - NOTE_PAD, 1.0)


def text_gaps(lines: list[Polyline], pad: float = TEXT_BAND_PAD) -> list[tuple[float, float]]:
    """The y bands (top, bottom) of text strokes, `pad` px wider each way and merged where
    they touch: the rows a filled rectangle's hatching must keep out of."""
    spans = sorted((min(y for _, y in pl) - pad, max(y for _, y in pl) + pad)
                   for pl in lines if pl)
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def shape_polylines(kind: str, w: float, h: float, options: ShapeOptions = ShapeOptions(),
                    gaps: list[tuple[float, float]] = ()) -> list[Polyline]:
    """`gaps`: y bands a rectangle's hatching skips (text_gaps)."""
    w, h = float(w), float(h)
    if kind == "rectangle":
        out = [[(0.0, 0.0), (w, 0.0), (w, h), (0.0, h), (0.0, 0.0)]]
        if options.fill:
            out += _hatch(w, h, float(options.hatch), gaps)
        return out
    if kind == "line":
        return [[(0.0, h / 2), (w, h / 2)]]
    if kind == "arrow":
        y = h / 2
        size = arrow_head(w, options.thickness)
        dx = size * math.cos(math.radians(HEAD_ANGLE))
        dy = size * math.sin(math.radians(HEAD_ANGLE))
        out = [[(0.0, y), (w, y)], [(w - dx, y - dy), (w, y), (w - dx, y + dy)]]
        if options.double:
            out.append([(dx, y - dy), (0.0, y), (dx, y + dy)])
        return out
    if kind == "note":
        f = note_fold(w, h)
        return [[(0.0, 0.0), (w - f, 0.0), (w, f), (w, h), (0.0, h), (0.0, 0.0)],
                [(w - f, 0.0), (w - f, f), (w, f)]]
    raise ValueError(f"unknown shape {kind!r}")


def _hatch(w: float, h: float, spacing: float,
           gaps: list[tuple[float, float]] = ()) -> list[Polyline]:
    """45-degree lines x + y = c, `spacing` px apart, cut to the box (0..w, 0..h) and, where
    `gaps` (y bands) cross them, into pieces."""
    step = max(spacing, 1.0) * math.sqrt(2)
    out = []
    k = 0
    while True:
        c = step * (k + 0.5)
        if c >= w + h:
            return out
        x0, x1 = max(0.0, c - h), min(w, c)
        if x1 > x0:
            for a, b in _cut(x0, x1, [(c - gb, c - ga) for ga, gb in gaps]):
                out.append([(a, c - a), (b, c - b)])
        k += 1


def _cut(x0: float, x1: float, holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[x0, x1] without the (sorted or not) `holes`: the pieces that remain."""
    out = []
    at = x0
    for a, b in sorted(holes):
        if b <= at or a >= x1:
            continue
        if a > at:
            out.append((at, a))
        at = max(at, b)
    if at < x1:
        out.append((at, x1))
    return out
