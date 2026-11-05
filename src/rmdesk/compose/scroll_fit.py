"""Find how far a page is scrolled in the tablet's keyboard layout, from the live frame.

xochitl doesn't save a page's verticalScroll when it is stopped, so the saved value can be
stale or missing at send time. Instead, the page's own strokes (page coordinates, read from
its .rm) are slid along the page's y axis over the live frame (what the user placed items
on) until they land on dark pixels; that offset is the scroll.

Keyboard layout (compose.frame): page = (fy - 936, 1404 - fx + scroll), so a page point
(px, py) is at frame (fx, fy) = (1404 - py + scroll, px + 936). No Qt here.

Text-anchored handwriting: on a page with typed text, xochitl keeps strokes in sub-groups
anchored to a text character (anchor_id). Their points are relative: absolute x = x +
anchor_origin_x, absolute y = y + the y of the anchor's text line, which only xochitl's text
layout knows. So each anchor is a reference of its own: its groups' points (absolute x,
local y) lie at frame (fx, fy) = (C - local_y, x + 936) for one unknown constant C, found by
the same slide-and-hit search. Unanchored lines are the reference None, with C = 1404 +
scroll and local y = absolute y (fit_details). See fit_references / choose_reference.
"""
from __future__ import annotations

import io
import logging

import numpy as np

DARK = 128  # frame grey below this is ink (strokes and template lines)
MIN_POINTS = 40  # page points on screen needed to trust a fit
MIN_HIT = 0.85  # share of on-screen points that must land on dark pixels at the best offset
MARGIN = 0.05  # the best must beat any offset more than EXCLUDE px away by this much
EXCLUDE = 40  # px: a stroke still overlaps itself when slid a little along its own length
MAX_POINTS = 20000


def fit_keyboard_scroll(gray: np.ndarray, points: np.ndarray,
                        max_scroll: float | None = None) -> float | None:
    return fit_details(gray, points, max_scroll)["scroll"]


def fit_details(gray: np.ndarray, points: np.ndarray,
                max_scroll: float | None = None) -> dict:
    """fit_keyboard_scroll with its working: scroll (or None), best/next share of hits, points
    on screen and why it refused, for diagnosing a failed match."""
    out = {"scroll": None, "points": int(len(points)), "on_screen": 0, "best": None,
           "best_hit": 0.0, "next_hit": 0.0, "reason": "", "fx": None}
    r = _fit(gray, points, max_scroll, out)
    out["scroll"] = r
    return out


def _fit(gray: np.ndarray, points: np.ndarray, max_scroll, out: dict) -> float | None:
    """The scroll (page px) that best lays `points` (N x 2 page coordinates) on the dark
    pixels of `gray` (frame, rows = y 0..1871, cols = x 0..1403), or None when no offset is
    clearly right (too few strokes on screen, or two offsets fit about as well)."""
    w = gray.shape[1]
    py = np.asarray(points, dtype=np.float64).reshape(-1, 2)[:, 1]
    top = float(max_scroll if max_scroll is not None else max(0.0, py.max() if py.size else 0))
    const = _fit_const(gray, points, w + np.arange(0, int(top) + 1), out)
    if out["best"] is not None:
        out["best"] -= w  # reported as a scroll, as before
    return None if const is None else float(const - w)


def _fit_const(gray: np.ndarray, points: np.ndarray, consts: np.ndarray,
               out: dict) -> float | None:
    """The frame-x constant C, out of `consts`, that best lays `points` (N x 2: absolute page
    x, local y) on the dark pixels of `gray` at frame (C - local_y, x + 936), or None when no
    C is clearly right. Fills out's best/best_hit/next_hit/on_screen/reason, and "fx": the
    frame-x range of the on-screen points at the best C."""
    h, w = gray.shape
    if points.size == 0:
        out["reason"] = "the page has no strokes rmdesk can read"
        return None
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) > MAX_POINTS:
        pts = pts[np.linspace(0, len(pts) - 1, MAX_POINTS).astype(int)]
    fy = np.rint(pts[:, 0] + h / 2).astype(int)  # frame row (y) from page x
    keep = (fy >= 0) & (fy < h)
    pts, fy = pts[keep], fy[keep]
    if len(pts) < MIN_POINTS:
        out["reason"] = "too few strokes across the visible width"
        return None
    py = pts[:, 1]
    dark = gray < DARK
    offsets = np.asarray(consts, dtype=int)
    hits = np.zeros(len(offsets))
    seen = np.zeros(len(offsets))
    base = np.rint(-py).astype(int)  # frame column at C = 0
    for i, s in enumerate(offsets):
        fx = base + s
        on = (fx >= 0) & (fx < w)
        n = int(on.sum())
        seen[i] = n
        if n:
            hits[i] = dark[fy[on], fx[on]].sum()
    frac = np.where(seen >= MIN_POINTS, hits / np.maximum(seen, 1), 0.0)
    best = int(np.argmax(frac))
    out["best"], out["best_hit"] = int(offsets[best]), float(frac[best])
    out["on_screen"] = int(seen[best])
    fx = base + offsets[best]
    fx = fx[(fx >= 0) & (fx < w)]
    out["fx"] = (int(fx.min()), int(fx.max())) if fx.size else None
    far = np.abs(offsets - offsets[best]) > EXCLUDE
    out["next_hit"] = float(frac[far].max()) if far.any() else 0.0
    if frac[best] < MIN_HIT:
        out["reason"] = "the page's strokes don't match the picture"
        return None
    if far.any() and frac[best] - frac[far].max() < MARGIN:
        out["reason"] = "two scroll positions fit about as well"
        return None
    return float(offsets[best])


def _read_tree(data: bytes):
    """The page's scene tree, or None if it can't be read."""
    from rmscene import read_tree
    log = logging.getLogger("rmscene")
    level = log.level
    log.setLevel(logging.ERROR)  # newer-format blocks only warn; their points still count
    try:
        return read_tree(io.BytesIO(data))
    except Exception:
        return None
    finally:
        log.setLevel(level)


def page_points(data: bytes) -> np.ndarray:
    """Every stroke point (x, y) on a .rm page, all layers; empty if it can't be read."""
    from rmscene import scene_items as si
    tree = _read_tree(data)
    if tree is None:
        return np.zeros((0, 2))

    def lines(g):
        for ch in g.children.values():
            if isinstance(ch, si.Group):
                yield from lines(ch)
            elif isinstance(ch, si.Line):
                yield ch
    pts = [(p.x, p.y) for ln in lines(tree.root) for p in ln.points]
    return np.array(pts, dtype=np.float64) if pts else np.zeros((0, 2))


def reference_points(data: bytes) -> dict:
    """The page's stroke points by placement reference: None -> the unanchored lines' points
    (x, y) in page coordinates; an anchor_id (CrdtId) -> the points of every group anchored
    to it, as (x + that group's anchor_origin_x, local y). Lines in hidden groups (not on
    screen) and lines under more than one anchored group (offsets that would add up) are left
    out. {} if the page can't be read."""
    from rmscene import scene_items as si
    tree = _read_tree(data)
    if tree is None:
        return {}
    out: dict = {}

    def walk(g, anchor, origin_x, nested):
        if g.visible is not None and g.visible.value is False:
            return
        if g.anchor_id is not None:
            if anchor is not None or g.anchor_origin_x is None:
                nested = True
            anchor, origin_x = g.anchor_id.value, (
                g.anchor_origin_x.value if g.anchor_origin_x is not None else 0.0)
        for ch in g.children.values():
            if isinstance(ch, si.Group):
                walk(ch, anchor, origin_x, nested)
            elif isinstance(ch, si.Line) and not nested:
                out.setdefault(anchor, []).extend((p.x + origin_x, p.y) for p in ch.points)

    walk(tree.root, None, 0.0, False)
    return {k: np.array(v, dtype=np.float64) for k, v in out.items() if v}


def fit_anchor(gray: np.ndarray, points: np.ndarray) -> dict:
    """fit_details for one anchor's points (absolute x, local y): "const" is the frame-x
    constant C (fx = C - local_y) or None, with the same working and thresholds."""
    out = {"const": None, "points": int(len(points)), "on_screen": 0, "best": None,
           "best_hit": 0.0, "next_hit": 0.0, "reason": "", "fx": None}
    if len(points):
        w = gray.shape[1]
        ly = np.asarray(points, dtype=np.float64)[:, 1]
        # every C that puts some point on screen
        consts = np.arange(int(np.floor(ly.min())), int(np.ceil(ly.max())) + w + 1)
    else:
        consts = np.zeros(0)
    c = _fit_const(gray, points, consts, out)
    out["const"] = None if c is None else float(c)
    return out


def fit_references(gray: np.ndarray, data: bytes) -> dict:
    """Every placement reference on the page with its fit against the live frame.

    {None: fit_details of the unanchored lines plus "const" = 1404 + scroll (None if not
    confident), anchor_id: fit_anchor of that anchor's groups}. The None entry is always
    there (with no points when every stroke is anchored or the page is empty)."""
    refs = reference_points(data)
    w = gray.shape[1]
    absolute = dict(fit_details(gray, refs.get(None, np.zeros((0, 2)))))
    scroll = absolute.get("scroll")
    absolute["const"] = None if scroll is None else float(scroll) + w
    out = {None: absolute}
    for key, pts in refs.items():
        if key is not None:
            out[key] = fit_anchor(gray, pts)
    return out


TOP_OF_TEXT_PART2 = 281474976710654  # CrdtId(0, 2**48 - 2): "top of the typed text"
BOTTOM_OF_TEXT_PART2 = 281474976710655  # CrdtId(0, 2**48 - 1): "bottom of the typed text"


def is_special_anchor(key) -> bool:
    """xochitl's two pseudo-anchors (top/bottom of the typed text), not a text character."""
    return (getattr(key, "part1", None) == 0
            and getattr(key, "part2", None) in (TOP_OF_TEXT_PART2, BOTTOM_OF_TEXT_PART2))


def choose_reference(fits: dict, items_fx: tuple[float, float]):
    """The reference to place new items by: (key, fit) or None if none is confident.

    The unanchored (absolute) fit if it is confident; otherwise the confident anchor whose
    strokes are nearest the items in frame x (`items_fx` = the items' (min, max) frame x):
    the smallest gap between the two frame-x ranges (0 when they overlap), then the nearest
    centres, then the higher hit share. The top/bottom-of-text pseudo-anchors are never chosen:
    a new group anchored to them is kept in the file but not drawn by the tablet."""
    if None in fits and fits[None].get("const") is not None:
        return None, fits[None]
    lo, hi = items_fx
    best = None
    for key, f in fits.items():
        if key is None or f.get("const") is None or not f.get("fx"):
            continue
        if is_special_anchor(key):
            continue  # xochitl keeps but never draws a new group on these (seen on fw 3.29.1.0)
        flo, fhi = f["fx"]
        gap = max(0.0, flo - hi, lo - fhi)
        centre = abs((flo + fhi) / 2 - (lo + hi) / 2)
        rank = (gap, centre, -f.get("best_hit", 0.0))
        if best is None or rank < best[0]:
            best = (rank, key, f)
    return None if best is None else (best[1], best[2])
