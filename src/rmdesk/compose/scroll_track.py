"""Follow the tablet page as it scrolls, frame by frame, so placed items stay on it. No Qt.

Keyboard layout (compose.frame): page = (fy - 936, 1404 - fx + scroll). Scrolling moves the
page rigidly along frame x, so a scroll between two stream frames is a shift along frame x
(compose.scroll_measure.shift_between's convention: f0(x) = f1(x - d), the page point at
frame x in f0 is at x - d in f1). An item placed on the page follows it by moving -d.

scroll_measure.shift_between costs ~22 ms on a full frame: too slow for every frame. Here
the page's middle band is sampled every ROW_STEP rows and averaged over DS columns; the
shift is found on that coarse picture (the same normalized correlation, one FFT per row),
then refined to the pixel at full column resolution near the coarse peak. ~1-3 ms a frame.

Tracker keeps a reference frame (the one the items were last lined up with, re-based as the
page moves on) so errors don't add up step by step, and says when the picture changed in a
way no scroll explains (a page turn, a menu): then nothing moves.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from rmdesk.compose.scroll_measure import BAND, COLS, ncc_profile

DS = 4  # frame columns per coarse sample
ROW_STEP = 8  # every 8th row of the band
MIN_OVERLAP = 300  # px of frame x two pictures must share
MIN_PEAK = 0.85  # correlation at the best shift (a rigid scroll is ~1)
MARGIN = 0.1  # the best must beat every shift more than EXCLUDE px away by this much
EXCLUDE = 40  # px
VAR_MIN = 1e-4
REFINE = DS + 2  # px either side of the coarse peak searched at full resolution
STILL_DIFF = 40 / 255  # darkness: a coarse sample that changed by more has changed
STILL_FRAC = 0.002  # share of changed samples below which two pictures are the same
CHANGED_SHARE = 0.6  # share of the inked samples (in either picture) that changed, above which,
#                      with no clear shift, the page looks different (a page turn, another
#                      notebook): the items may not match. A menu over part of it stays below
REBASE = 500  # px: past this the reference frame is moved on (enough overlap stays)

START, STILL, MOVED, UNCLEAR, CHANGED = "start", "still", "moved", "unclear", "changed"


@dataclass(frozen=True)
class Sample:
    """A frame's band, sampled: `fine` (rows, columns) and `coarse` (rows, columns / DS),
    darkness 0..1."""
    fine: np.ndarray
    coarse: np.ndarray


def sample(gray: np.ndarray) -> Sample:
    g = np.asarray(gray)[BAND[0]:BAND[1]:ROW_STEP, COLS[0]:COLS[1]]
    fine = (255.0 - g.astype(np.float32)) / 255.0
    w = fine.shape[1] // DS * DS
    coarse = fine[:, :w].reshape(fine.shape[0], -1, DS).mean(axis=2)
    return Sample(fine, coarse)


def changed_fraction(a: Sample, b: Sample) -> float:
    """Share of coarse samples that changed by more than STILL_DIFF (no shift)."""
    return float((np.abs(a.coarse - b.coarse) > STILL_DIFF).mean())


def changed_share(a: Sample, b: Sample) -> float:
    """Share of the samples inked in either picture that changed by more than STILL_DIFF:
    ~1 for another page (or a blank one), the covered part for a menu."""
    inked = (a.coarse > STILL_DIFF) | (b.coarse > STILL_DIFF)
    n = int(inked.sum())
    if n == 0:
        return 0.0
    return float((np.abs(a.coarse - b.coarse) > STILL_DIFF)[inked].sum()) / n


def _ncc_at(a: np.ndarray, b: np.ndarray, d: int) -> float:
    """Correlation of a[:, x] with b[:, x - d] over their overlap."""
    w = a.shape[1]
    lo, hi = max(0, d), min(w, w + d)
    if hi - lo < MIN_OVERLAP:
        return 0.0
    x, y = a[:, lo:hi], b[:, lo - d:hi - d]
    x = x - x.mean()
    y = y - y.mean()
    n = x.size
    va, vb = float((x * x).sum()), float((y * y).sum())
    if va <= VAR_MIN * n or vb <= VAR_MIN * n:
        return 0.0
    return float((x * y).sum()) / float(np.sqrt(va * vb))


def profile(a: Sample, b: Sample) -> tuple[np.ndarray, np.ndarray] | None:
    """The coarse correlation of a with b for every shift (ncc_profile): (d / DS, ncc)."""
    return ncc_profile(a.coarse.astype(np.float64), b.coarse.astype(np.float64),
                       MIN_OVERLAP // DS, VAR_MIN)


def shift(a: Sample, b: Sample, prof=None) -> tuple[int | None, float]:
    """(d, confidence) like scroll_measure.shift_between (a(x) = b(x - d)), on the samples:
    d None unless the best shift is clear (at least MIN_PEAK, MARGIN above every shift more
    than EXCLUDE px away). `prof`: profile(a, b), if already worked out."""
    prof = profile(a, b) if prof is None else prof
    if prof is None:
        return None, 0.0
    ds, ncc = prof
    i = int(np.argmax(ncc))
    best = float(ncc[i])
    far = np.abs(ds - ds[i]) > EXCLUDE / DS
    second = float(ncc[far].max()) if far.any() else 0.0
    conf = best - second
    if best < MIN_PEAK or conf < MARGIN:
        return None, conf
    centre = int(ds[i]) * DS
    cands = range(centre - REFINE, centre + REFINE + 1)
    scores = [_ncc_at(a.fine, b.fine, d) for d in cands]
    k = int(np.argmax(scores))
    if scores[k] < MIN_PEAK:
        return None, conf
    return cands[k], conf


@dataclass(frozen=True)
class Step:
    """What one frame did: move every placed item by `dx` along frame x; `status` one of
    START, STILL, MOVED, UNCLEAR, CHANGED."""
    dx: int
    status: str


class Tracker:
    """step(gray) per new stream frame while there are placed items; reset() when there are
    none (or the items were lined up with the picture again). `warn`: the picture changed in a
    way no scroll explains and looks like another page (cleared when the page lines up again)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.ref: Sample | None = None
        self.prev: Sample | None = None
        self.applied = 0  # the shift from ref already given to the items
        self.synced = False  # the items line up with `prev`
        self.warn = False

    def step(self, gray: np.ndarray) -> Step:
        return self.step_sample(sample(gray))

    def step_sample(self, s: Sample) -> Step:
        if self.ref is None:
            self.ref = self.prev = s
            self.applied, self.synced = 0, True
            return Step(0, START)
        prev = self.prev
        if changed_fraction(prev, s) <= STILL_FRAC:
            self.prev = s
            return Step(0, STILL)
        d, _ = shift(self.ref, s)
        if d is not None:
            delta = d - self.applied
            self.applied = d
            if abs(self.applied) > REBASE:
                self.ref, self.applied = s, 0
            self.prev, self.synced, self.warn = s, True, False
            return Step(-delta, MOVED if delta else STILL)
        if self.synced and prev is not self.ref:
            d, _ = shift(prev, s)  # the page went past the reference: step from the last
            if d is not None and d != 0:
                self.ref = self.prev = s
                self.applied, self.warn = 0, False
                return Step(-d, MOVED)
        self.prev, self.synced = s, False
        if changed_share(self.ref, s) > CHANGED_SHARE:
            self.warn = True
            return Step(0, CHANGED)
        return Step(0, UNCLEAR)
