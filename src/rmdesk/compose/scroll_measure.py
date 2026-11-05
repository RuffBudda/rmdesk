"""Measure how far a page is scrolled in the keyboard layout by scrolling it to its top. No Qt.

A page with typed text but no handwriting gives compose.scroll_fit nothing to lay over the
live picture. Instead rmdesk swipes the page to its top (the UI does that: ui.compose.
scroll_probe) and measures how far the picture moved on the way, here.

Keyboard layout (compose.frame): page = (fy - 936, 1404 - fx + scroll), so a page point is
shown at frame x = 1404 - page y + scroll. Scrolling moves everything on the page (typed
text, the template, handwriting) rigidly along frame x. Two frames of the same page at
scrolls s_a and s_b satisfy F_a(x) = F_b(x - (s_a - s_b)): shift_between finds that
d = s_a - s_b. At the top the scroll is 0, so the shift from the picture the items were placed
on (F0) to the top is F0's scroll.

The finger drags the page with it: a swipe from frame x x0 to x1 changes the scroll by
x1 - x0. Toward the page's top (scroll down to 0) is toward decreasing frame x: the screen's
top as shown (where the tablet puts its zoom label) is the high-x side.
"""
from __future__ import annotations

import numpy as np

from rmdesk.compose.frame import FRAME_H, FRAME_W

PAGE_ROWS = (234, 1638)  # frame rows the page's width covers (page x -702..702)
BAND = (400, 1472)  # rows compared: the middle of the page's width (page x -536..536)
COLS = (40, FRAME_W - 40)  # columns compared: clear of anything the tablet puts at the edges
ROW_STEP = 2  # every other row: plenty, and twice as fast
MIN_OVERLAP = 300  # px of frame x two pictures must share to be lined up
SMALL_OVERLAP = 120  # a big jump (one swipe reaching the page's top) can leave less shared:
SMALL_PEAK = 0.97  # then only a near-perfect match counts
SMALL_MARGIN = 0.25
MIN_PEAK = 0.85  # correlation at the best shift: a rigid scroll is ~1, a changed picture less
MARGIN = 0.1  # the best must beat every shift more than EXCLUDE px away by this much
EXCLUDE = 40  # px: a shift a little off still correlates with itself
VAR_MIN = 1e-4  # darkness variance below this: an empty stretch, nothing to line up

STILL_DIFF = 40  # grey levels: a pixel that changed by more has changed
STILL_FRAC = 0.002  # share of changed pixels below which two pictures are the same
STILL_TOL = 1  # px: a confident shift this small counts as not moving
STILL_NEEDED = 2  # swipes in a row that move nothing: the page is at its top
MAX_SWIPES = 20  # swipes before giving up (shorter swipes: more of them)
CHAIN_TOL = 3  # px (+1 per move) the summed moves may differ from the direct shift

SWIPE_ROW = FRAME_H // 2  # the middle of the page's width
SWIPE_START_X = 1000  # well below the top of the screen as shown (high frame x)
SWIPE_LENGTH = 400  # px of frame x per swipe: each move overlaps the last by plenty
SWIPE_STEPS = 16
SWIPE_STEP_S = 0.025  # seconds between moves: a slow, deliberate drag
SWIPE_HOLD_S = 0.25  # the finger stays still this long before lifting: no fling

NO_EFFECT = "no_effect"  # the swipes didn't move the page at all (not working, or ignored)
UNCLEAR = "unclear"  # the picture has too little (or only a repeating template) to line up
MOVED = "moved"  # the picture changed in a way a scroll can't explain
BOUND = "bound"  # the page didn't reach its top within the swipes allowed

MOVING, STILL, TOP = "moving", "still", "top"


class MeasureError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


NOISE_JUMP = 128  # grey levels between neighbouring pixels counted as a jump
NOISE_ROW = 0.2  # share of jumps above which a row is stream noise, not page content


def noise_rows(gray: np.ndarray) -> np.ndarray:
    """Per BAND row (every ROW_STEP): True where the row is stream noise. After xochitl
    restarts, goMarkableStream can show rows of random black/white until the tablet redraws
    them; they change between frames and would break the line-up. A page row (ink, grid,
    text) has few neighbour-to-neighbour jumps; noise has many."""
    g = np.asarray(gray)[BAND[0]:BAND[1]:ROW_STEP, COLS[0]:COLS[1]].astype(np.int16)
    jumps = np.abs(np.diff(g, axis=1)) > NOISE_JUMP
    return jumps.mean(axis=1) > NOISE_ROW


def _darkness(gray: np.ndarray, keep: np.ndarray | None = None) -> np.ndarray:
    g = np.asarray(gray)[BAND[0]:BAND[1]:ROW_STEP, COLS[0]:COLS[1]]
    d = (255.0 - g.astype(np.float64)) / 255.0
    return d if keep is None else d[keep]


def ncc_profile(a: np.ndarray, b: np.ndarray, min_overlap: int = MIN_OVERLAP,
                var_min: float = VAR_MIN) -> tuple[np.ndarray, np.ndarray] | None:
    """Normalized cross-correlation of two equal-shape (rows, columns) arrays for every shift
    d along the columns with at least `min_overlap` columns in common: (d, ncc), ncc[i] the
    correlation of a[:, x] with b[:, x - d[i]] over the overlap (0 where either side is
    flat). None if the arrays are narrower than `min_overlap`. One FFT per row."""
    rows, w = a.shape
    lim = w - min_overlap
    if lim < 0:
        return None
    n_fft = 1 << int(np.ceil(np.log2(2 * w)))
    spec = (np.fft.rfft(a, n_fft, axis=1) * np.conj(np.fft.rfft(b, n_fft, axis=1))).sum(0)
    cc = np.fft.irfft(spec, n_fft)  # cc[d] = sum over rows and x of a[x] * b[x - d]
    d = np.arange(-lim, lim + 1)
    sab = cc[d % n_fft]
    ca = np.concatenate([[0.0], np.cumsum(a.sum(0))])
    caa = np.concatenate([[0.0], np.cumsum((a * a).sum(0))])
    cb = np.concatenate([[0.0], np.cumsum(b.sum(0))])
    cbb = np.concatenate([[0.0], np.cumsum((b * b).sum(0))])
    lo, hi = np.maximum(0, d), np.minimum(w, w + d)  # the overlap, in a's columns
    n = rows * (hi - lo)
    sa, saa = ca[hi] - ca[lo], caa[hi] - caa[lo]
    sb, sbb = cb[hi - d] - cb[lo - d], cbb[hi - d] - cbb[lo - d]
    va, vb = saa - sa * sa / n, sbb - sb * sb / n
    ok = (va > var_min * n) & (vb > var_min * n)
    ncc = np.where(ok, (sab - sa * sb / n) / np.sqrt(np.where(ok, va * vb, 1.0)), 0.0)
    return d, ncc


def shift_details(f0: np.ndarray, f1: np.ndarray) -> dict:
    """The working of shift_between: d (None if unclear), best and second correlation, and
    confidence = best - second."""
    out = {"d": None, "best": 0.0, "second": 0.0, "confidence": 0.0, "noise_rows": 0}
    keep = ~(noise_rows(f0) | noise_rows(f1))  # rows of stream noise in either: left out
    out["noise_rows"] = int((~keep).sum())
    if keep.sum() < 20:
        return out
    a, b = _darkness(f0, keep), _darkness(f1, keep)
    for overlap, peak, margin in ((MIN_OVERLAP, MIN_PEAK, MARGIN),
                                  (SMALL_OVERLAP, SMALL_PEAK, SMALL_MARGIN)):
        prof = ncc_profile(a, b, min_overlap=overlap)
        if prof is None:
            continue
        d, ncc = prof
        i = int(np.argmax(ncc))
        best = float(ncc[i])
        far = np.abs(d - d[i]) > EXCLUDE
        second = float(ncc[far].max()) if far.any() else 0.0
        out.update(best=best, second=second, confidence=best - second, overlap=overlap)
        if best >= peak and best - second >= margin:
            out["d"] = int(d[i])
            return out
    return out


def shift_between(f0: np.ndarray, f1: np.ndarray) -> tuple[int | None, float]:
    """(d, confidence): f0's picture is f1's moved d px along frame x (f0(x) = f1(x - d)),
    from the correlation of the page's middle band (rows BAND) along frame x. d is None
    unless the best shift is clear: a correlation of at least MIN_PEAK that beats every shift
    more than EXCLUDE px away by MARGIN (a blank page, a page of nothing but a repeating
    template, or a picture that changed other than by scrolling all fail this)."""
    r = shift_details(f0, f1)
    return r["d"], r["confidence"]


def frames_still(a: np.ndarray, b: np.ndarray) -> bool:
    """The two pictures are the same (the page didn't move): almost no pixel of the page's
    band changed by more than STILL_DIFF grey levels."""
    keep = ~(noise_rows(a) | noise_rows(b))  # stream noise changes on its own: left out
    if keep.sum() < 20:
        return False
    x = np.asarray(a)[BAND[0]:BAND[1]:ROW_STEP, COLS[0]:COLS[1]][keep].astype(np.int16)
    y = np.asarray(b)[BAND[0]:BAND[1]:ROW_STEP, COLS[0]:COLS[1]][keep].astype(np.int16)
    return float((np.abs(x - y) > STILL_DIFF).mean()) <= STILL_FRAC


CHECK_LENGTH = 200  # px of the first, opposite swipe (down the page): proves swipes work


def check_points() -> list[tuple[float, float]]:
    """A short swipe the other way (the page toward its end), before the measuring: a page in
    the keyboard layout always has room to scroll one way or the other, so if neither this
    nor the swipes up move the picture, the swipes aren't reaching the page."""
    start = SWIPE_START_X - SWIPE_LENGTH
    return [(float(start + CHECK_LENGTH * i / 8), float(SWIPE_ROW)) for i in range(9)]


def swipe_points() -> list[tuple[float, float]]:
    """One downward swipe (the page toward its top): along the middle of the page's width,
    from SWIPE_START_X toward decreasing frame x by SWIPE_LENGTH, in SWIPE_STEPS moves."""
    return [(float(SWIPE_START_X - SWIPE_LENGTH * i / SWIPE_STEPS), float(SWIPE_ROW))
            for i in range(SWIPE_STEPS + 1)]


def scroll_after_swipe(scroll: float, points) -> float:
    """The scroll after the page followed the finger through `points` (never above the top)."""
    pts = list(points)
    return max(0.0, scroll + (pts[-1][0] - pts[0][0]))


class TopFinder:
    """The stop rule and the sum, frame by frame (the UI swipes and fetches the frames).

    TopFinder(f0) refuses (MeasureError UNCLEAR) a picture that can't be lined up even with
    itself. begin(frame) takes the first fresh picture: False until it lines up with f0 (the
    page may still be coming back). Then after_swipe(frame) once per swipe: MOVING (the page
    moved toward its top, rigidly), STILL (it didn't move) or TOP (STILL_NEEDED swipes in a
    row moved nothing: `scroll` is set). Raises MeasureError MOVED for any change a scroll
    toward the top can't explain, BOUND after max_swipes without reaching the top."""

    def __init__(self, f0: np.ndarray, max_swipes: int = MAX_SWIPES):
        self.f0 = f0
        self.max_swipes = max_swipes
        if shift_between(f0, f0)[0] != 0:
            raise MeasureError(UNCLEAR, "the picture can't be lined up with itself")
        self.prev: np.ndarray | None = None
        self.total = 0
        self.moves = 0
        self.swipes = 0
        self.still = 0
        self.scroll: int | None = None
        self.check_moved = False

    def begin(self, frame: np.ndarray) -> bool:
        d, _ = shift_between(self.f0, frame)
        if d is None:
            return False
        self.prev, self.total = frame, d
        return True

    def after_check(self, frame: np.ndarray) -> None:
        """The frame after the short check swipe down the page (check_points): it moved the
        page toward its end (negative d), or nothing if the page was already at its end."""
        if self.prev is None:
            raise RuntimeError("begin() first")
        if frames_still(self.prev, frame):
            d = 0
        else:
            d, _ = shift_between(self.prev, frame)
            if d is None:
                raise MeasureError(MOVED, "check swipe: not a scroll")
            if d > STILL_TOL:
                raise MeasureError(MOVED, f"check swipe: moved {d} px the wrong way")
        self.check_moved = abs(d) > STILL_TOL
        self.total += d
        self.prev = frame

    def after_swipe(self, frame: np.ndarray) -> str:
        if self.prev is None:
            raise RuntimeError("begin() first")
        self.swipes += 1
        if frames_still(self.prev, frame):
            d = 0
        else:
            d, _ = shift_between(self.prev, frame)
            if d is None:
                raise MeasureError(MOVED, f"swipe {self.swipes}: not a scroll")
            if d < -STILL_TOL:
                raise MeasureError(MOVED, f"swipe {self.swipes}: moved {d} px the wrong way")
        self.total += d
        self.prev = frame
        if abs(d) <= STILL_TOL:
            self.still += 1
        else:
            self.still = 0
            self.moves += 1
        if self.still >= STILL_NEEDED:
            self._finish(frame)
            return TOP
        if self.swipes >= self.max_swipes:
            raise MeasureError(BOUND, f"{self.swipes} swipes")
        return STILL if self.still else MOVING

    def _finish(self, top: np.ndarray) -> None:
        if self.moves == 0 and not self.check_moved:
            # nothing ever moved: "at the top" can't be told from "swipes not working"
            raise MeasureError(NO_EFFECT, "no swipe moved the page")
        if self.total < -STILL_TOL:
            raise MeasureError(MOVED, f"F0 above the top ({self.total} px)")
        if self.total <= COLS[1] - COLS[0] - MIN_OVERLAP:  # F0 and the top still overlap
            direct, _ = shift_between(self.f0, top)
            if direct is not None and abs(direct - self.total) > CHAIN_TOL + self.moves:
                raise MeasureError(MOVED, f"moves add up to {self.total}, direct {direct}")
        self.scroll = max(0, int(self.total))
