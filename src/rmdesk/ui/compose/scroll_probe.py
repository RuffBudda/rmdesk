"""Measure a typed-only page's scroll in the keyboard layout by swiping it to its top.

compose.scroll_measure does the maths (TopFinder: the shifts, the stop rule); this drives it
on the GUI thread without blocking: borrow touch from Control (RemoteControl.borrow_touch),
take a fresh frame (resync, wait for the next frame, bounded), swipe the page toward its top,
let it settle, take the next frame, ... until the page stops moving. Then `finished(scroll)`:
how far the picture the items were placed on (F0) was scrolled. Anything unclear: `failed`
(message, details), and nothing more is swiped.

Input only, before any tablet job: it never runs while a job holds the slot (the borrowed
touch is taken back the moment one starts), and the finger is always lifted at the end.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from rmdesk.compose import scroll_measure as sm
from rmdesk.connection import BUSY_MSG
from rmdesk.ui.compose import messages as msg

log = logging.getLogger(__name__)

SETTLE_MS = 900  # after a swipe is queued: the swipe (~0.2 s) and the page's redraw
FRAME_TIMEOUT_MS = 3000  # a resync's frame
START_WAIT_MS = 15000  # the page to show (after xochitl restarted it may take a while)
RETRY_MS = 500  # between looks while waiting for the page to show
DEADLINE_MS = 25000  # the whole swiping, from the first swipe

_MESSAGES = {sm.UNCLEAR: msg.MEASURE_UNCLEAR, sm.MOVED: msg.MEASURE_MOVED,
             sm.BOUND: msg.MEASURE_BOUND, sm.NO_EFFECT: msg.MEASURE_NO_EFFECT}


def _save_measure_debug(f0, prev, frame, error) -> None:
    """A refused measuring: keep the pictures (this PC only, newest 3) to see why."""
    try:
        import cv2
        import json
        from datetime import datetime
        from rmdesk.ui import live_compose
        d = live_compose.DEBUG_DIR
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        d.chmod(0o700)
        stamp = datetime.now().strftime("measure-%Y%m%d-%H%M%S")
        for name, img in (("f0", f0), ("prev", prev), ("frame", frame)):
            if img is not None:
                cv2.imwrite(str(d / f"{stamp}-{name}.png"), img)
        info = {"error": str(error)}
        if prev is not None:
            info["shift"] = sm.shift_details(prev, frame)
        (d / f"{stamp}.json").write_text(json.dumps(info, indent=1, default=str))
        for f in d.iterdir():
            f.chmod(0o600)
        stamps = sorted({f.name.split("-f0")[0].split("-prev")[0].split("-frame")[0]
                         .split(".")[0] for f in d.glob("measure-*")})
        for old in stamps[:-3]:
            for f in d.glob(old + "*"):
                f.unlink(missing_ok=True)
    except Exception:
        log.warning("could not save the measuring debug files", exc_info=True)


class ScrollProbe(QObject):
    """One measurement. `touch` has borrow_touch(ready, fail) / return_touch() (RemoteControl);
    `frames` has `frame` (with .gray), a `frame_changed` signal and refresh() (ScreenView);
    `busy()` names a tablet job holding the slot, or None."""

    progress = Signal(str)
    finished = Signal(int)
    failed = Signal(str, str)  # message, details

    def __init__(self, f0: np.ndarray, touch, frames, busy: Callable[[], str | None],
                 parent: QObject | None = None, *, settle_ms: int = SETTLE_MS,
                 frame_timeout_ms: int = FRAME_TIMEOUT_MS, start_wait_ms: int = START_WAIT_MS,
                 retry_ms: int = RETRY_MS, deadline_ms: int = DEADLINE_MS,
                 max_swipes: int = sm.MAX_SWIPES, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.f0, self.touch, self.frames, self.busy = f0, touch, frames, busy
        self._settle_ms, self._retry_ms = settle_ms, retry_ms
        self._start_wait = start_wait_ms / 1000.0
        self._max_swipes, self._clock = max_swipes, clock
        self.finder: sm.TopFinder | None = None
        self.session = None
        self.swiped = False
        self.done = False
        self._started_at = 0.0
        self._want: Callable[[np.ndarray], None] | None = None
        self._before = None
        self._frame_timer = self._timer(frame_timeout_ms, self._frame_timed_out)
        self._settle = self._timer(settle_ms, lambda: self._fetch(self._on_swipe_frame))
        self._retry = self._timer(retry_ms, lambda: self._fetch(self._on_start_frame))
        self._deadline = self._timer(deadline_ms, lambda: self._fail(sm.BOUND, "time"))
        frames.frame_changed.connect(self._on_frame)

    def _timer(self, ms: int, slot) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(slot)
        return t

    # -- the steps --
    def start(self) -> None:
        try:
            self.finder = sm.TopFinder(self.f0, max_swipes=self._max_swipes)
        except sm.MeasureError as e:  # nothing to line up: no need to touch the tablet
            self._fail(e.code, e.detail)
            return
        self.progress.emit(msg.MEASURING)
        self.touch.borrow_touch(self._ready, self._no_touch)

    def _no_touch(self, reason: str) -> None:
        if self.done:
            return
        if self.session is None:  # never had touch: refuse as before this existed
            self._end()
            self.failed.emit(msg.SCROLL_NO_STROKES, reason)
            return
        self._end()  # taken back mid-way (a job started, Control stopped, the writer died)
        self.failed.emit(self._scrolled(msg.MEASURE_MOVED), reason)

    def _ready(self, session) -> None:
        if self.done:
            self.touch.return_touch()
            return
        self.session = session
        self._started_at = self._clock()
        self._fetch(self._on_start_frame)

    def _fetch(self, then: Callable[[np.ndarray], None]) -> None:
        """A fresh frame: resync, then the next frame that arrives (bounded)."""
        if self.done:
            return
        self._want, self._before = then, self.frames.frame
        self._frame_timer.start()
        self.frames.refresh()

    def _on_frame(self) -> None:
        f = self.frames.frame
        if self.done or self._want is None or f is None or f is self._before:
            return
        then, self._want = self._want, None
        self._frame_timer.stop()
        then(np.asarray(f.gray))

    def _frame_timed_out(self) -> None:
        if self.done or self._want is None:
            return
        waiting_to_start = self._want == self._on_start_frame
        self._want = None
        if waiting_to_start and self._clock() - self._started_at < self._start_wait:
            self._retry.start()  # xochitl may still be coming back
            return
        self._end()
        self.failed.emit(self._scrolled(msg.MEASURE_NO_PICTURE), "no frame")

    def _on_start_frame(self, gray: np.ndarray) -> None:
        if self.finder.begin(gray):
            self._deadline.start()
            self._checking = True
            self._swipe()
        elif self._clock() - self._started_at < self._start_wait:
            self._retry.start()
        else:
            self._fail(sm.UNCLEAR, "the page didn't show")

    def _swipe(self) -> None:
        if self.done:
            return
        job = self.busy()
        if job is not None:
            self._end()
            self.failed.emit(self._scrolled(BUSY_MSG.format(job)), "job")
            return
        if self.session.touching:  # someone holds the screen through Control
            self._fail(sm.MOVED, "touching")
            return
        # down, slow moves, a still moment, up (always lifted): the page stops where the
        # finger stopped, so frames before and after overlap
        pts = sm.check_points() if getattr(self, "_checking", False) else sm.swipe_points()
        self.session.swipe(pts, step=sm.SWIPE_STEP_S, hold=sm.SWIPE_HOLD_S)
        self.swiped = True
        self._settle.start()

    def _on_swipe_frame(self, gray: np.ndarray) -> None:
        prev = self.finder.prev
        if getattr(self, "_checking", False):  # the short swipe down: did the page move?
            try:
                self.finder.after_check(gray)
            except sm.MeasureError as e:
                _save_measure_debug(self.f0, prev, gray, e)
                self._fail(e.code, e.detail)
                return
            if not self.finder.check_moved and not getattr(self, "_rechecked", False):
                # the tablet can ignore the first gesture sent to it (e.g. just after a
                # restart): one more check swipe before measuring
                self._rechecked = True
                log.warning("check swipe moved nothing: once more")
                self._swipe()
                return
            self._checking = False
            self._swipe()
            return
        try:
            r = self.finder.after_swipe(gray)
        except sm.MeasureError as e:
            _save_measure_debug(self.f0, prev, gray, e)
            self._fail(e.code, e.detail)
            return
        if r == sm.TOP:
            scroll = int(self.finder.scroll)
            log.warning("measured scroll %s in %s swipes", scroll, self.finder.swipes)
            self._end()
            self.finished.emit(scroll)
        else:
            self._swipe()

    # -- the end --
    def _scrolled(self, text: str) -> str:
        return f"{text} {msg.MEASURE_SCROLLED}" if self.swiped else text

    def _fail(self, code: str, detail: str) -> None:
        if self.done:
            return
        log.warning("scroll measuring refused: %s %s", code, detail)
        self._end()
        self.failed.emit(self._scrolled(_MESSAGES.get(code, msg.MEASURE_MOVED)), detail)

    def _end(self) -> None:
        self.done = True
        self._want = None
        for t in (self._frame_timer, self._settle, self._retry, self._deadline):
            t.stop()
        try:
            self.frames.frame_changed.disconnect(self._on_frame)
        except (RuntimeError, TypeError):
            pass
        if self.session is not None:
            self.session.touch_up()
        self.touch.return_touch()

    def cancel(self) -> None:
        """Stop now (the view is going): nothing is emitted."""
        if not self.done:
            self._end()
