"""Auto catch-up: one resync 2.5 s after the last frame of a burst (rmdesk.stream.catchup)."""
from __future__ import annotations

from rmdesk.stream.catchup import DELAY, CatchUp


class FakeTimer:
    """Single-shot timer on a fake clock: start(s) (re)arms, stop() disarms."""

    def __init__(self):
        self.now = 0.0
        self.deadline: float | None = None
        self.callback = None

    def start(self, seconds: float) -> None:
        self.deadline = self.now + seconds

    def stop(self) -> None:
        self.deadline = None

    def advance(self, seconds: float) -> None:
        end = self.now + seconds
        while self.deadline is not None and self.deadline <= end:
            self.now, self.deadline = self.deadline, None
            self.callback()
        self.now = end


def make():
    t = FakeTimer()
    calls = []
    c = CatchUp(lambda: calls.append(t.now), t.start, t.stop, clock=lambda: t.now)
    t.callback = c.fire
    return c, t, calls


def test_delay_is_two_and_a_half_seconds():
    assert DELAY == 2.5


def test_one_resync_2_5_s_after_the_last_frame():
    c, t, calls = make()
    c.frame()
    t.advance(2.4)
    assert calls == []
    t.advance(0.1)
    assert calls == [2.5]
    t.advance(60)
    assert calls == [2.5]  # once, not repeating


def test_none_while_frames_keep_coming():
    c, t, calls = make()
    for _ in range(20):
        c.frame()
        t.advance(1.0)
    assert calls == []
    t.advance(2.5)
    assert calls == [21.5]  # 2.5 s after the last frame (t = 19)


def test_catch_up_full_frame_does_not_schedule_another():
    c, t, calls = make()
    c.frame()
    t.advance(2.5)
    assert len(calls) == 1
    c.frame()  # the full frame the resync produced
    t.advance(10)
    assert len(calls) == 1


def test_resumes_after_a_new_burst():
    c, t, calls = make()
    c.frame()
    t.advance(2.5)
    c.frame()  # catch-up's own full frame
    t.advance(5)
    c.frame()  # new input on the tablet
    t.advance(1)
    c.frame()
    t.advance(2.5)
    assert calls == [2.5, 11.0]


def test_manual_refresh_full_frame_does_not_schedule_a_catch_up():
    c, t, calls = make()
    c.frame()
    t.advance(1)
    c.resynced()  # the user pressed Refresh
    assert t.deadline is None  # the pending catch-up is moot
    c.frame()  # the refresh's full frame
    t.advance(10)
    assert calls == []


def test_reset_cancels_and_forgets():
    c, t, calls = make()
    c.frame()
    c.reset()  # stream stopped
    t.advance(10)
    assert calls == []
    c.frame()
    t.advance(2.5)
    assert calls == [12.5]


def test_awaiting_full_frame_expires_after_3_s():
    # the resync produced no frame (offline, 429 retry...): a frame much later is a new burst
    c, t, calls = make()
    c.frame()
    t.advance(2.5)
    assert calls == [2.5]
    t.advance(3.1)
    c.frame()  # not the resync's full frame: it is new input
    t.advance(2.5)
    assert calls == [2.5, 8.1]


def test_manual_resync_wait_expires_too():
    c, t, calls = make()
    c.resynced()
    t.advance(3.1)
    c.frame()
    t.advance(2.5)
    assert calls == [5.6]


def test_frame_within_3_s_is_still_the_resyncs_own():
    c, t, calls = make()
    c.resynced()
    t.advance(2.9)
    c.frame()
    t.advance(10)
    assert calls == []


def test_has_no_qt():
    import subprocess
    import sys
    from pathlib import Path
    code = ("import sys, rmdesk.stream.catchup; "
            "sys.exit(any(m.startswith('PySide6') for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code],
                          cwd=Path(__file__).parent.parent / "src").returncode == 0
