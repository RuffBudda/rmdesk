"""Follow the page with the finger and the picture together, so placed items stay on it. No Qt.

compose.scroll_track follows the page from the picture alone. Where the picture is ambiguous
(only a dot or line template on screen: every template period lines up as well as the true
shift) it can't follow; the tablet's touch events (remote.touch_watch) can.

Keyboard layout (compose.frame): page = (fy - 936, 1404 - fx + scroll). A one-finger drag
scrolls the page 1:1 with the finger along frame x: the page point under the finger stays
under it, so a drag of dx frame px moves the page content (and the items) by +dx, i.e. the
tracker's shift d (ref(x) = now(x - d)) changes by -dx. (scroll_measure: a swipe toward
decreasing frame x scrolls toward the page's top.) Travel along frame y doesn't scroll; a
finger that moves less than TAP_PX is a tap; two fingers are a pinch (zoom: not followed).

Each new frame, one answer moves the items (the touch events only update the prediction):
1. With no finger news (no drag, nothing predicted, no momentum): the picture alone
   (scroll_track.Tracker), as before. Portrait layout: always this.
2. A drag is going on or the picture hasn't caught up with one yet: the finger's travel since
   the reference frame is the predicted shift. The picture refines it: the correlation with
   the reference is searched only within +-window of the prediction (window = WINDOW_MAX, or
   less than half the template period when the reference shows one), and a clear peak there
   is taken. Else a clear shift anywhere (the picture alone, a page with text: it lags the
   finger, and the picture is what the items are drawn on). Else the prediction alone,
   unless the picture looks like another page (nothing lines up near the prediction and most
   of the ink changed): then nothing moves and the page-changed warning is raised.
3. After a fast lift (a flick, FLICK_V) the page may keep going by itself: the picture alone
   follows it, each frame searched near where the lift's speed, dying away (COAST_TAU), takes
   it (never short of where the finger left it); a frame nothing lines up near, or the same
   as the last (the page stopped), moves nothing.
With a prediction every frame is lined up, even one that looks the same as the last (on a
template page a scroll by whole periods does). Once a frame is the same as the last SETTLE_S
after the last touch, the prediction is dropped (back to 1).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

import numpy as np

from rmdesk.compose import scroll_track as st
from rmdesk.compose.scroll_track import (CHANGED, CHANGED_SHARE, DS, MIN_OVERLAP, MOVED, START,
                                         STILL, STILL_FRAC, UNCLEAR, Sample, Step)
from rmdesk.remote.touch_watch import CANCEL, DOWN, MOVE, UP, ZOOM, TouchEvent

TAP_PX = 10  # frame px a finger must move before it is a drag, not a tap
WINDOW_MAX = 25  # px either side of the prediction the picture may correct it by
WINDOW_MIN = DS + 2
PERIOD_MIN, PERIOD_MAX = 12, 240  # px: template periods looked for in the reference
PERIOD_PEAK = 0.5  # autocorrelation a repeat must reach to count as a template period
NEAR_PEAK = 0.6  # correlation a refined peak must reach
NEAR_MARGIN = 0.1  # ... beating everything else in the window more than NEAR_EXCLUDE px away
NEAR_EXCLUDE = 8
LOST_PEAK = 0.4  # below this near the prediction (and most ink changed): another page
FLICK_V = 600.0  # frame px/s along x at the lift: the page may coast on
FLICK_SPAN = 0.08  # s of finger movement the lift velocity is measured over
COAST_TAU = 0.15  # s: a coasting page's speed falls as exp(-t / COAST_TAU) (a guess: check)
COAST_MAX = 2.0  # s a coast is followed at most
SETTLE_S = 0.4  # s after the last touch before a still picture ends the prediction
FORCE_REBASE = st.COLS[1] - st.COLS[0] - MIN_OVERLAP - 50  # px: the reference is lost past this

NEAR, FAR, FINGER, LOST = "near", "far", "finger", "lost"


def template_period(s: Sample) -> int | None:
    """The repeat of a template in the picture along frame x (px), or None: the first local
    maximum of its autocorrelation past PERIOD_MIN that reaches PERIOD_PEAK."""
    prof = st.profile(s, s)
    if prof is None:
        return None
    ds, ncc = prof
    lo, hi = PERIOD_MIN // DS, PERIOD_MAX // DS
    for i in np.flatnonzero((ds >= lo) & (ds <= hi)):
        if i + 1 < len(ncc) and ncc[i] >= PERIOD_PEAK and ncc[i] >= ncc[i - 1] \
                and ncc[i] >= ncc[i + 1]:
            return int(ds[i]) * DS
    return None


def window_for(period: int | None) -> int:
    if period is None:
        return WINDOW_MAX
    return max(WINDOW_MIN, min(WINDOW_MAX, period // 2 - DS // 2 - 1))


def locate(ref: Sample, s: Sample, pred: int, window: int, prof=None) -> tuple[int | None, str]:
    """Where `s` lines up with `ref` near the predicted shift `pred` (scroll_track's d):
    (d, NEAR) a clear peak within +-window; (d, FAR) else a clear shift anywhere; (None,
    FINGER) nothing clear, take the prediction; (None, LOST) nothing lines up near it and most
    of the ink changed: not this page."""
    prof = st.profile(ref, s) if prof is None else prof
    near_best = 0.0
    if prof is not None:
        ds, ncc = prof
        dpx = ds * DS
        inside = np.abs(dpx - pred) <= window + DS
        if inside.any():
            idx = np.flatnonzero(inside)
            i = int(idx[np.argmax(ncc[idx])])
            near_best = float(ncc[i])
            far = idx[np.abs(dpx[idx] - dpx[i]) > NEAR_EXCLUDE]
            second = float(ncc[far].max()) if far.size else 0.0
            if near_best >= NEAR_PEAK and near_best - second >= NEAR_MARGIN:
                c = int(dpx[i])
                cands = [d for d in range(c - st.REFINE, c + st.REFINE + 1)
                         if abs(d - pred) <= window]
                if cands:
                    scores = [st._ncc_at(ref.fine, s.fine, d) for d in cands]
                    k = int(np.argmax(scores))
                    edge = abs(cands[k] - pred) == window and len(cands) > 1
                    if scores[k] >= NEAR_PEAK and not edge:
                        return cands[k], NEAR
        d, _ = st.shift(ref, s, prof)
        if d is not None:
            return d, FAR
    if near_best < LOST_PEAK and st.changed_share(ref, s) > CHANGED_SHARE:
        return None, LOST
    return None, FINGER


class Follower:
    """scroll_track.Tracker with the finger: touch(event) for each TouchEvent (GUI thread),
    step(gray) per new stream frame while there are items, reset() when there are none.
    `keyboard`: the tablet shows the keyboard layout (else the finger is ignored)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.keyboard = False
        self.tracker = st.Tracker()
        self.reset()

    # the picture tracker's state, as before
    @property
    def ref(self) -> Sample | None:
        return self.tracker.ref

    @property
    def warn(self) -> bool:
        return self.tracker.warn

    def reset(self) -> None:
        self.tracker.reset()
        self._clear_finger()
        self.zoomed = False  # a pinch since the page last lined up
        self.last_how: str | None = None

    def _clear_finger(self) -> None:
        self.drag: dict | None = None
        self.pred: float | None = None  # the predicted shift from the reference (Tracker's d)
        self.coast: dict | None = None
        self.last_touch = -1e9

    # -- the finger -----------------------------------------------------------------------
    def touch(self, ev: TouchEvent) -> None:
        if not self.keyboard or self.tracker.ref is None:
            return
        now = self.clock()
        self.last_touch = now
        if ev.kind == DOWN:
            self.coast = None
            self.drag = {"x0": ev.fx, "y0": ev.fy, "travel": 0.0, "moving": False,
                         "samples": deque([(ev.t, ev.fx)])}
        elif ev.kind == MOVE and self.drag is not None:
            g = self.drag
            g["travel"] += ev.dx
            g["samples"].append((ev.t, ev.fx))
            while len(g["samples"]) > 2 and g["samples"][0][0] < ev.t - FLICK_SPAN:
                g["samples"].popleft()
            if g["moving"]:
                self.pred -= ev.dx
            elif np.hypot(ev.fx - g["x0"], ev.fy - g["y0"]) >= TAP_PX:
                g["moving"] = True
                base = self.pred if self.pred is not None else self.tracker.applied
                self.pred = base - g["travel"]
        elif ev.kind == UP:
            g, self.drag = self.drag, None
            v = self._velocity(g) if g is not None and g["moving"] else 0.0
            if abs(v) >= FLICK_V:
                self.coast = {"until": now + COAST_MAX, "t0": now, "t_prev": now, "v": v}
        elif ev.kind in (ZOOM, CANCEL):
            self.drag, self.coast = None, None
            if ev.kind == ZOOM:
                self.zoomed = True
                self.pred = None  # the page was zoomed: no finger travel means a scroll now

    @staticmethod
    def _velocity(g: dict) -> float:
        (t0, x0), (t1, x1) = g["samples"][0], g["samples"][-1]
        return (x1 - x0) / (t1 - t0) if t1 - t0 > 1e-3 else 0.0

    def _guided(self) -> bool:
        if not self.keyboard:
            return False
        if self.coast is not None and self.clock() > self.coast["until"]:
            self.coast = None
            self.pred = None
        if self.drag is not None and self.drag["moving"]:
            return True
        return self.coast is not None or (self.pred is not None
                                          and round(self.pred) != self.tracker.applied)

    # -- the picture ------------------------------------------------------------------------
    def step(self, gray: np.ndarray) -> Step:
        s = st.sample(gray)
        tr = self.tracker
        if tr.ref is None or not self._guided():
            step = tr.step_sample(s)
            if step.status == START:
                self._clear_finger()
            self.pred = None
            if step.status == MOVED:
                self.zoomed = False
            if self.zoomed and step.status == UNCLEAR:
                tr.warn = True  # zoomed: the picture no longer matches the items
            self.last_how = None
            return step
        return self._guided_step(s)

    def _window(self, ref: Sample) -> int:
        cached = getattr(self, "_win_for", None)
        if cached is None or cached[0] is not ref:
            self._win_for = (ref, window_for(template_period(ref)))
        return self._win_for[1]

    def _guided_step(self, s: Sample) -> Step:
        tr = self.tracker
        # the same picture as the last needn't mean the page stayed: on a template page a
        # scroll by whole periods looks the same. It only says when the page has settled
        settled = (self.drag is None and self.clock() - self.last_touch >= SETTLE_S
                   and st.changed_fraction(tr.prev, s) <= STILL_FRAC)
        step = self._locate_step(s)
        if settled:
            self.pred, self.coast = None, None  # the picture is the page again
        return step

    def _locate_step(self, s: Sample) -> Step:
        tr = self.tracker
        coasting = self.coast is not None and self.drag is None
        finger = int(round(self.pred)) if self.pred is not None else tr.applied
        pred = finger
        if coasting:
            if st.changed_fraction(tr.prev, s) <= STILL_FRAC:  # no finger: the page stopped
                tr.prev = s
                self.last_how = None
                return Step(0, STILL)
            # the page goes at least where the finger took it, and may run on
            c, now = self.coast, self.clock()
            k0, k1 = (np.exp(-(t - c["t0"]) / COAST_TAU) for t in (c["t_prev"], now))
            c["t_prev"] = now
            run = tr.applied - int(round(c["v"] * COAST_TAU * (k0 - k1)))
            if (finger - run) * c["v"] > 0:  # further along the flick (d falls as v > 0)
                pred = run
        d, how = locate(tr.ref, s, pred, self._window(tr.ref))
        self.last_how = how
        if how == LOST:
            tr.prev, tr.synced, tr.warn = s, False, True
            return Step(0, CHANGED)
        if how == FINGER:
            if coasting:  # momentum is followed by the picture only
                tr.prev, tr.synced = s, False
                return Step(0, UNCLEAR)
            d = pred
        delta = d - tr.applied
        tr.applied = d
        tr.prev, tr.synced, tr.warn = s, how != FINGER, False
        self.zoomed = False
        if abs(tr.applied) > (st.REBASE if how != FINGER else FORCE_REBASE):
            if self.pred is not None:
                self.pred -= tr.applied
            tr.ref, tr.applied = s, 0
        return Step(-delta, MOVED if delta else STILL)
