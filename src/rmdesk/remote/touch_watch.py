"""Read the tablet's touch events: what the finger (or rmdesk's own Control) does. No Qt.

rmdesk reads "pt_mt" (`cat /dev/input/eventN`, found by name) while items are placed, to know
when a one-finger drag scrolls the page (compose.scroll_fuse). Read only: nothing is sent.
evdev hands every event to every reader, so the touches Control writes come back here too.

The stream is `struct input_event` on the 32-bit tablet: 16 bytes, `<llHHi` (seconds,
microseconds, type, code, value). Multitouch protocol B: ABS_MT_SLOT picks a slot (the kernel
only sends it when the slot changes), ABS_MT_TRACKING_ID >= 0 puts a finger down in it, -1
lifts it, ABS_MT_POSITION_X/Y move it (a value equal to the last is never sent, so positions
are kept per slot across fingers), SYN_REPORT ends one report. SYN_DROPPED (the reader fell
behind) loses events up to the next SYN_REPORT: the gesture then counts as unknown.

Touch to frame (remote.input.frame_to_touch, inverted): fx = touch x, fy = 1871 - touch y.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from rmdesk.remote.input import (ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_SLOT,
                                 ABS_MT_TRACKING_ID, EV_ABS, EV_SYN, SYN_REPORT, TOUCH_MAX_Y)

SYN_DROPPED = 3
EVENT = struct.Struct("<llHHi")
EVENT_SIZE = EVENT.size  # 16

DOWN, MOVE, UP, ZOOM, CANCEL = "down", "move", "up", "zoom", "cancel"


@dataclass(frozen=True)
class Report:
    """The touch state at one SYN_REPORT: `t` tablet time (s), `fingers` how many are down,
    `points` slot -> (touch x, touch y) of those down with a known position, `ids` slot ->
    tracking id, `dropped` events were lost just before it."""
    t: float
    fingers: int
    points: dict
    ids: dict
    dropped: bool = False


class EventParser:
    """feed(bytes) -> the Reports completed by those bytes. Takes any chunking: a partial
    event waits in the buffer for the rest."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.slot = 0  # the kernel says the slot only when it changes; 0 until it does
        self._ids: dict[int, int] = {}  # slot -> tracking id of the finger down in it
        self._pos: dict[int, list] = {}  # slot -> [x, y] (None until seen)
        self._dropping = False
        self._dropped = False

    def feed(self, data: bytes) -> list[Report]:
        self._buf += data
        n = len(self._buf) // EVENT_SIZE * EVENT_SIZE
        if n == 0:
            return []
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        out: list[Report] = []
        for sec, usec, type_, code, value in EVENT.iter_unpack(chunk):
            if type_ == EV_SYN:
                if code == SYN_DROPPED:
                    self._dropping = self._dropped = True
                elif code == SYN_REPORT:
                    if self._dropping:  # the state is unknown: forget the fingers
                        self._dropping = False
                        self._ids.clear()
                    out.append(self._report(sec + usec / 1e6))
                continue
            if self._dropping or type_ != EV_ABS:
                continue
            if code == ABS_MT_SLOT:
                self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value < 0:
                    self._ids.pop(self.slot, None)
                else:
                    self._ids[self.slot] = value
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                p = self._pos.setdefault(self.slot, [None, None])
                p[0 if code == ABS_MT_POSITION_X else 1] = value
        return out

    def _report(self, t: float) -> Report:
        points = {}
        for slot in self._ids:
            p = self._pos.get(slot)
            if p is not None and p[0] is not None and p[1] is not None:
                points[slot] = (p[0], p[1])
        dropped, self._dropped = self._dropped, False
        return Report(t, len(self._ids), points, dict(self._ids), dropped)


def touch_to_frame(x: float, y: float) -> tuple[float, float]:
    return float(x), float(TOUCH_MAX_Y - y)


@dataclass(frozen=True)
class TouchEvent:
    """A one-finger gesture step in frame pixels: DOWN at (fx, fy); MOVE by (dx, dy) to
    (fx, fy); UP where it was last. ZOOM: two or more fingers (a pinch: not followed); CANCEL:
    events were lost. `t` tablet time (s)."""
    kind: str
    fx: float = 0.0
    fy: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    t: float = 0.0


IDLE, DRAG, OTHER = "idle", "drag", "other"


@dataclass
class Gesture:
    """Reports -> TouchEvents. One finger: DOWN, a MOVE per report it moved in, UP. A second
    finger at any point makes the gesture ZOOM (said once) and nothing more is said until
    every finger is up; lost events (CANCEL) the same."""
    state: str = IDLE
    _id: int | None = None
    _last: tuple | None = field(default=None)

    def report(self, r: Report) -> list[TouchEvent]:
        out: list[TouchEvent] = []
        if r.dropped and self.state != OTHER:
            if self.state == DRAG:
                out.append(TouchEvent(CANCEL, *self._last, t=r.t))
            self.state = OTHER
        if self.state == OTHER:
            if r.fingers == 0:
                self.state = IDLE
            return out
        if r.fingers >= 2:
            self.state = OTHER
            out.append(TouchEvent(ZOOM, t=r.t))
            return out
        if self.state == DRAG:
            if r.fingers == 0:
                self.state = IDLE
                out.append(TouchEvent(UP, *self._last, t=r.t))
                return out
            slot, tid = next(iter(r.ids.items()))
            if tid != self._id:  # lifted and another finger down in one report
                out.append(TouchEvent(UP, *self._last, t=r.t))
                self.state = IDLE
            else:
                p = r.points.get(slot)
                if p is not None:
                    fx, fy = touch_to_frame(*p)
                    if (fx, fy) != self._last:
                        lx, ly = self._last
                        self._last = (fx, fy)
                        out.append(TouchEvent(MOVE, fx, fy, fx - lx, fy - ly, r.t))
                return out
        if r.fingers == 1:  # IDLE
            slot, tid = next(iter(r.ids.items()))
            p = r.points.get(slot)
            if p is not None:
                self.state, self._id = DRAG, tid
                self._last = touch_to_frame(*p)
                out.append(TouchEvent(DOWN, *self._last, t=r.t))
        return out
