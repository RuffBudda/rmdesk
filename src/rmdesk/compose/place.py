"""Rotation of placed polylines (pure, Qt-free): shared by the live preview and the send path."""
from __future__ import annotations

import math

Polyline = list[tuple[float, float]]

SNAP_DEG = 15.0


def normalise_deg(deg: float) -> float:
    """Angle in [0, 360)."""
    r = float(deg) % 360.0
    return 0.0 if r >= 360.0 else r


def snap_angle(deg: float, free: bool = False, step: float = SNAP_DEG) -> float:
    """`deg` snapped to the nearest `step` degrees (unless `free`), normalised to [0, 360)."""
    d = float(deg) if free else round(float(deg) / step) * step
    return normalise_deg(d)


def rotate_polylines(lines: list[Polyline], cx: float, cy: float, deg: float) -> list[Polyline]:
    """Rotate `lines` by `deg` degrees around (cx, cy).

    Frame/page coordinates are y-down, so a positive angle turns the picture clockwise
    as seen on screen: at 90 degrees (cx + 10, cy) maps to (cx, cy + 10).
    """
    if deg % 360.0 == 0.0:
        return [list(pl) for pl in lines]
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[(cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c)
             for x, y in pl] for pl in lines]
