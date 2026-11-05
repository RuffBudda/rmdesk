"""Auto catch-up for goMarkableStream's input-driven frames.

goMarkableStream v1.4.0 only sends frames while there is input (2 s after a touch, 300 ms
after pen lift — docs/stream-protocol.md "When frames are sent"). The tablet often finishes
drawing after that window, so the last picture rmdesk got is stale. A new /stream connection
always starts with a full frame, so DELAY seconds after the last frame of a burst we ask the
client to resync once. The full frame that resync produces must not schedule another one;
the next frame after it (new input on the tablet) starts a new burst.

Qt-free: the caller supplies a single-shot timer (start(seconds) re-arms it, stop() disarms)
and calls fire() when it expires. The Screen view uses a QTimer. `clock` (monotonic seconds)
is injectable for tests.
"""
from __future__ import annotations

import time
from typing import Callable

DELAY = 2.5  # seconds after the last frame
FULL_FRAME_WAIT = 3.0  # a resync's full frame later than this never came (offline, 429...)


class CatchUp:
    def __init__(self, resync: Callable[[], None], start_timer: Callable[[float], None],
                 stop_timer: Callable[[], None], delay: float = DELAY,
                 clock: Callable[[], float] = time.monotonic):
        self._resync, self._start, self._stop = resync, start_timer, stop_timer
        self.delay, self._clock = delay, clock
        self._awaiting_full = False  # a resync is in flight; its full frame is not a burst
        self._resync_at = 0.0

    def frame(self) -> None:
        """A frame arrived."""
        if self._awaiting_full:
            self._awaiting_full = False
            if self._clock() - self._resync_at <= FULL_FRAME_WAIT:
                return  # the resync's own full frame
            # else the resync produced no frame: this one is new input on the tablet
        self._start(self.delay)

    def fire(self) -> None:
        """The timer expired: the burst is over."""
        self._awaiting_full, self._resync_at = True, self._clock()
        self._resync()

    def resynced(self) -> None:
        """A resync was requested elsewhere (Refresh): its full frame is fresh already."""
        self._stop()
        self._awaiting_full, self._resync_at = True, self._clock()

    def reset(self) -> None:
        """The stream stopped: cancel and forget."""
        self._stop()
        self._awaiting_full = False
