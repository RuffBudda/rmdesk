"""Swiping a typed-only page to its top to measure its scroll (ui.compose.scroll_probe), with a
simulated tablet: scripted frames that follow the swipes."""
import numpy as np
import pytest
from PySide6.QtCore import QObject, QTimer, Signal

from rmdesk.compose import scroll_measure as sm
from rmdesk.stream.client import Frame
from rmdesk.ui.compose import messages as msg
from rmdesk.ui.compose.scroll_probe import ScrollProbe
from tests.scroll_sim import add_dots, blank_page, frame_at, text_page

FAST = dict(settle_ms=1, frame_timeout_ms=200, start_wait_ms=300, retry_ms=5,
            deadline_ms=5000)


class Tablet(QObject):
    """The Screen view's frame source over a simulated page: refresh() delivers the frame at
    the current scroll; `edit(gray, n)` can change the n-th frame after a swipe."""
    frame_changed = Signal()

    def __init__(self, page, scroll, deliver=True):
        super().__init__()
        self.page, self.scroll, self.deliver = page, scroll, deliver
        self.frame = None
        self.refreshes = 0
        self.swipes = 0
        self.edit = None
        self.blank_first = 0

    def refresh(self):
        self.refreshes += 1
        if self.deliver:
            QTimer.singleShot(0, self._send)

    def _send(self):
        if self.blank_first > 0:
            self.blank_first -= 1
            gray = np.full((1872, 1404), 255, np.uint8)
        else:
            gray = frame_at(self.page, int(self.scroll))
        if self.edit is not None:
            gray = self.edit(gray, self.swipes)
        self.frame = Frame(1404, 1872, gray)
        self.frame_changed.emit()


class Session:
    def __init__(self, tablet, step=None):
        self.tablet, self.touching, self.swiped, self.ups = tablet, False, [], 0
        self.step = step  # the scroll a swipe moves by (None: as far as the finger)

    def swipe(self, pts, **kw):
        self.swiped.append(list(pts))
        t = self.tablet
        t.swipes += 1
        down = pts[-1][0] > pts[0][0]  # the check swipe goes the other way (down the page)
        if self.step is None:
            t.scroll = sm.scroll_after_swipe(t.scroll, pts)
        else:
            t.scroll = max(0, t.scroll + (self.step if down else -self.step))

    def touch_up(self):
        self.ups += 1


class Touch:
    def __init__(self, session=None, refuse=None):
        self.session, self.refuse = session, refuse
        self.borrowed = self.returned = 0

    def borrow_touch(self, ready, fail):
        self.borrowed += 1
        if self.refuse:
            fail(self.refuse)
        else:
            ready(self.session)

    def return_touch(self):
        self.returned += 1


@pytest.fixture(scope="module")
def page():
    return text_page(seed=11)


def run(qtbot, probe):
    out = {}
    probe.finished.connect(lambda s: out.update(scroll=s))
    probe.failed.connect(lambda m, d: out.update(message=m, details=d))
    probe.progress.connect(lambda t: out.update(progress=t))
    probe.start()
    qtbot.waitUntil(lambda: bool(out.keys() & {"scroll", "message"}), timeout=10000)
    return out


def make(page, scroll, f0_scroll=None, busy=lambda: None, step=None, **kw):
    tablet = Tablet(page, scroll)
    session = Session(tablet, step)
    touch = Touch(session)
    f0 = frame_at(page, scroll if f0_scroll is None else f0_scroll)
    probe = ScrollProbe(f0, touch, tablet, busy, **{**FAST, **kw})
    return probe, tablet, session, touch


def test_it_swipes_until_the_page_stops_and_measures_the_scroll(qtbot, page):
    probe, tablet, session, touch = make(page, 1300)
    out = run(qtbot, probe)
    assert out["scroll"] == 1300 and out["progress"] == msg.MEASURING
    assert tablet.scroll == 0
    # SWIPE_LENGTH (400) px per swipe: four that move (400, 400, 400, 100), then two that
    # move nothing
    import math
    # first the short check swipe down the page (+200), then the swipes up
    assert session.swiped[0] == sm.check_points()
    assert len(session.swiped) == 1 + math.ceil((1300 + sm.CHECK_LENGTH) / sm.SWIPE_LENGTH) \
        + sm.STILL_NEEDED
    for pts in session.swiped[1:]:
        assert pts == sm.swipe_points()
    assert touch.borrowed == 1 and touch.returned == 1 and session.ups >= 1  # finger lifted
    assert tablet.refreshes == 1 + len(session.swiped)  # a fresh frame before each decision


def test_a_page_already_at_the_top_measures_zero(qtbot, page):
    probe, tablet, session, _ = make(page, 0)
    out = run(qtbot, probe)
    # check down (+200), one swipe back up, then still
    assert out["scroll"] == 0 and len(session.swiped) == 2 + sm.STILL_NEEDED


def test_it_waits_for_the_page_to_show(qtbot, page):
    """After xochitl restarted: blank frames first, then the page at another scroll."""
    probe, tablet, session, _ = make(page, 200, f0_scroll=700)
    tablet.blank_first = 3
    out = run(qtbot, probe)
    assert out["scroll"] == 700


def test_the_bound_refuses(qtbot, page):
    probe, tablet, session, touch = make(page, 2100, step=100, max_swipes=4)
    out = run(qtbot, probe)
    assert "scroll" not in out
    assert out["message"].startswith(msg.MEASURE_BOUND) and msg.MEASURE_SCROLLED in out["message"]
    assert len(session.swiped) == 1 + 4 and touch.returned == 1  # the check swipe + 4


def test_the_time_bound_refuses(qtbot, page):
    probe, tablet, session, _ = make(page, 2100, step=100, settle_ms=60, deadline_ms=150)
    out = run(qtbot, probe)
    assert out["message"].startswith(msg.MEASURE_BOUND) and len(session.swiped) < sm.MAX_SWIPES


def test_a_non_rigid_change_refuses(qtbot, page):
    """The user's own touch (or a menu) changes the picture: refuse, swipe no more."""
    probe, tablet, session, touch = make(page, 1000)

    def panel(gray, swipes):
        if swipes >= 1:
            gray = gray.copy()
            gray[300:1500, 200:900] = 255
        return gray
    tablet.edit = panel
    out = run(qtbot, probe)
    assert out["message"].startswith(msg.MEASURE_MOVED) and len(session.swiped) == 1
    assert touch.returned == 1


def test_a_finger_held_through_control_refuses(qtbot, page):
    probe, tablet, session, _ = make(page, 400)
    session.touching = True
    out = run(qtbot, probe)
    assert out["message"] == msg.MEASURE_MOVED and session.swiped == []


def test_no_touch_refuses_as_before(qtbot, page):
    tablet = Tablet(page, 300)
    touch = Touch(refuse="Couldn't find the tablet's touch screen.")
    probe = ScrollProbe(frame_at(page, 300), touch, tablet, lambda: None, **FAST)
    out = run(qtbot, probe)
    assert out["message"] == msg.SCROLL_NO_STROKES
    assert out["details"] == "Couldn't find the tablet's touch screen."
    assert tablet.refreshes == 0


def test_a_tablet_job_stops_it(qtbot, page):
    jobs = iter([None, "sending to the tablet"])
    probe, tablet, session, _ = make(page, 1300, busy=lambda: next(jobs, "x"))
    out = run(qtbot, probe)
    assert "sending to the tablet" in out["message"] and len(session.swiped) == 1


def test_an_ambiguous_picture_refuses_without_touching(qtbot):
    pg = add_dots(blank_page())
    probe, tablet, session, touch = make(pg, 300)
    out = run(qtbot, probe)
    assert out["message"] == msg.MEASURE_UNCLEAR
    assert touch.borrowed == 0 and session.swiped == [] and tablet.refreshes == 0


def test_no_frames_refuses(qtbot, page):
    probe, tablet, session, _ = make(page, 300)
    tablet.deliver = False
    out = run(qtbot, probe)
    assert out["message"] == msg.MEASURE_NO_PICTURE and session.swiped == []


def test_cancel_emits_nothing_and_returns_the_touch(qtbot, page):
    probe, tablet, session, touch = make(page, 1300, settle_ms=200)
    seen = []
    probe.finished.connect(seen.append)
    probe.failed.connect(lambda m, d: seen.append(m))
    probe.start()
    qtbot.waitUntil(lambda: len(session.swiped) == 1, timeout=5000)
    probe.cancel()
    qtbot.wait(400)
    assert seen == [] and touch.returned == 1 and len(session.swiped) == 1


def test_an_ignored_first_check_swipe_is_tried_once_more(qtbot, page):
    """Seen on the device: the tablet ignored the first gesture; one more check swipe."""
    probe, tablet, session, touch = make(page, 900)
    real = session.swipe
    ignored = []

    def swipe(pts, **kw):
        if not ignored:  # the first gesture is lost
            ignored.append(pts)
            session.swiped.append(list(pts))
            tablet.swipes += 1
            return
        real(pts, **kw)
    session.swipe = swipe
    out = run(qtbot, probe)
    assert out.get("scroll") == 900, out
    assert session.swiped[0] == sm.check_points() and session.swiped[1] == sm.check_points()
