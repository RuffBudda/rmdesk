"""Reading the tablet's touch events (remote.touch_watch): input_event parsing in any chunking,
multitouch slots, one-finger drags in frame pixels, pinches flagged."""
import struct

from rmdesk.remote import input as ri
from rmdesk.remote import touch_watch as tw
from rmdesk.remote.touch_watch import CANCEL, DOWN, MOVE, UP, ZOOM

EV = struct.Struct("<llHHi")


def ev(type_, code, value, t=0.0):
    sec = int(t)
    return EV.pack(sec, int(round((t - sec) * 1e6)), type_, code, value)


def abs_(code, value, t=0.0):
    return ev(ri.EV_ABS, code, value, t)


def syn(t=0.0):
    return ev(ri.EV_SYN, ri.SYN_REPORT, 0, t)


def down(slot, tid, x, y, t=0.0):
    return (abs_(ri.ABS_MT_SLOT, slot, t) + abs_(ri.ABS_MT_TRACKING_ID, tid, t)
            + abs_(ri.ABS_MT_POSITION_X, x, t) + abs_(ri.ABS_MT_POSITION_Y, y, t))


def events(data: bytes, chunk: int | None = None):
    p, g = tw.EventParser(), tw.Gesture()
    out = []
    step = chunk or len(data) or 1
    for i in range(0, len(data), step):
        for r in p.feed(data[i:i + step]):
            out += g.report(r)
    return out


def test_partial_chunks_give_the_same_reports():
    data = down(0, 7, 100, 200, 1.0) + syn(1.0) + abs_(ri.ABS_MT_POSITION_X, 130, 1.02) \
        + syn(1.02) + abs_(ri.ABS_MT_TRACKING_ID, -1, 1.04) + syn(1.04)
    whole = tw.EventParser().feed(data)
    for chunk in (1, 3, 7, 15, 16, 17, 33):
        p = tw.EventParser()
        got = []
        for i in range(0, len(data), chunk):
            got += p.feed(data[i:i + chunk])
        assert got == whole, chunk
    assert [r.fingers for r in whole] == [1, 1, 0]
    assert whole[0].points == {0: (100, 200)} and whole[1].points == {0: (130, 200)}
    assert whole[1].t == 1.02


def test_a_report_ends_only_at_syn():
    p = tw.EventParser()
    assert p.feed(down(0, 1, 10, 20)) == []  # no SYN yet: nothing reported
    (r,) = p.feed(syn())
    assert r.fingers == 1 and r.points == {0: (10, 20)}


def test_two_slots_and_tracking_ids():
    p = tw.EventParser()
    (r1,) = p.feed(down(0, 5, 100, 100) + syn())
    (r2,) = p.feed(down(1, 6, 300, 400) + syn())
    assert r2.fingers == 2 and r2.points == {0: (100, 100), 1: (300, 400)}
    # the kernel says the slot only when it changes: this move is slot 1's
    (r3,) = p.feed(abs_(ri.ABS_MT_POSITION_Y, 450) + syn())
    assert r3.points[1] == (300, 450) and r3.points[0] == (100, 100)
    (r4,) = p.feed(abs_(ri.ABS_MT_SLOT, 0) + abs_(ri.ABS_MT_TRACKING_ID, -1) + syn())
    assert r4.fingers == 1 and r4.ids == {1: 6}
    (r5,) = p.feed(abs_(ri.ABS_MT_SLOT, 1) + abs_(ri.ABS_MT_TRACKING_ID, -1) + syn())
    assert r5.fingers == 0 and r5.points == {}
    # a new finger in slot 0 at the same spot: the kernel drops the unchanged position
    (r6,) = p.feed(abs_(ri.ABS_MT_SLOT, 0) + abs_(ri.ABS_MT_TRACKING_ID, 9) + syn())
    assert r6.points == {0: (100, 100)}


def test_a_drag_in_frame_pixels_with_the_right_sign():
    """fx = touch x, fy = 1871 - touch y (frame_to_touch inverted)."""
    data = down(0, 3, 400, 1871 - 900, 0.0) + syn(0.0)
    for i in range(1, 4):
        data += abs_(ri.ABS_MT_POSITION_X, 400 + 20 * i, 0.01 * i) + syn(0.01 * i)
    data += abs_(ri.ABS_MT_POSITION_Y, 1871 - 905, 0.05) + syn(0.05)
    data += abs_(ri.ABS_MT_TRACKING_ID, -1, 0.06) + syn(0.06)
    got = events(data, chunk=5)
    assert [e.kind for e in got] == [DOWN, MOVE, MOVE, MOVE, MOVE, UP]
    assert (got[0].fx, got[0].fy) == (400, 900)
    assert [e.dx for e in got[1:4]] == [20, 20, 20]  # toward higher frame x
    assert got[4].dx == 0 and got[4].dy == 5  # frame y grows as touch y falls
    assert (got[5].fx, got[5].fy) == (460, 905)
    assert got[2].t == 0.02


def test_frame_to_touch_round_trips():
    for fx, fy in ((0, 0), (1403, 1871), (702, 936), (100, 1500)):
        assert tw.touch_to_frame(*ri.frame_to_touch(fx, fy)) == (fx, fy)


class _Bytes:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def pause(self, _s):
        pass

    def close(self):
        pass


def test_a_pinch_is_flagged_and_says_nothing_more():
    w = _Bytes()
    ri.InputSession(w).pinch(2.0)
    got = events(w.data)
    assert [e.kind for e in got] == [ZOOM]
    # a finger after both are up is a new gesture
    got = events(w.data + down(0, 99, 500, 500) + syn() + abs_(ri.ABS_MT_TRACKING_ID, -1) + syn())
    assert [e.kind for e in got] == [ZOOM, DOWN, UP]


def test_a_second_finger_mid_drag_turns_it_into_a_zoom():
    data = down(0, 1, 100, 100) + syn() + abs_(ri.ABS_MT_POSITION_X, 150) + syn() \
        + down(1, 2, 600, 600) + syn() + abs_(ri.ABS_MT_POSITION_X, 700) + syn() \
        + abs_(ri.ABS_MT_TRACKING_ID, -1) + syn() + abs_(ri.ABS_MT_SLOT, 0) \
        + abs_(ri.ABS_MT_POSITION_X, 400) + syn() + abs_(ri.ABS_MT_TRACKING_ID, -1) + syn()
    assert [e.kind for e in events(data)] == [DOWN, MOVE, ZOOM]


def test_controls_own_swipe_reads_back_as_a_drag():
    """Control writes the same events the reader sees (evdev passes writes to every reader)."""
    w = _Bytes()
    ri.InputSession(w).swipe([(300.0, 900.0), (350.0, 900.0), (420.0, 900.0)])
    got = events(w.data, chunk=11)
    assert [e.kind for e in got] == [DOWN, MOVE, MOVE, UP]
    assert got[0].fx == 300 and got[0].fy == 900
    assert sum(e.dx for e in got if e.kind == MOVE) == 120


def test_lost_events_cancel_the_gesture():
    data = down(0, 1, 100, 100) + syn() + abs_(ri.ABS_MT_POSITION_X, 150) + syn() \
        + ev(ri.EV_SYN, tw.SYN_DROPPED, 0) + abs_(ri.ABS_MT_POSITION_X, 900) + syn() \
        + abs_(ri.ABS_MT_POSITION_X, 950) + syn()
    got = events(data)
    assert [e.kind for e in got] == [DOWN, MOVE, CANCEL]
